# nixos-postgres-maintenance-module

A NixOS module + CLI that closes
[nixpkgs#318777](https://github.com/NixOS/nixpkgs/issues/318777):
Postgres refuses to start once its on-disk collation library version no
longer matches what's recorded in its catalogs. This runs before
`postgresql.target` comes up, detects that drift, and repairs it —
reindexing affected relations, refreshing the recorded version, and
relocating any row that's drifted into the wrong text-keyed partition —
rather than leaving the cluster down until a human intervenes.

## Why this exists

PostgreSQL versions each collation it uses (`pg_database.datcollversion`,
`pg_collation.collversion`) so it can detect when the underlying
collation library (glibc, ICU) changed underneath it — a changed sort
order silently corrupts indexes and can misplace rows in a
collation-keyed partitioned table. Nixpkgs itself declined to fix this
at the distribution level (`nixpkgs#318777`, closed won't-fix: "there's
nothing we can do" absent a proper mechanism). This project is that
mechanism, packaged so any NixOS host running Postgres can use it.

`C`/`C.*`/`POSIX` locales are a separate problem: Postgres never
records a version for them at all, so a glibc upgrade that changes
`C.UTF-8`'s own behavior (it has happened — glibc 2.35, 2022) is
otherwise invisible. This project tracks that separately via a stamp —
see `docs/decisions/0003-store-path-not-version-string-for-the-c-utf-8-stamp.md`.

## How it works

```mermaid
flowchart TD
    A[postgresql.service starts] --> B[postgresql-collation-guard.service]
    B --> PRE[preStart hooks]
    PRE -->|blockOnFailure fails| X[exit 1]
    PRE --> C{template0 stale?}
    C -- yes --> D[refresh template0's version<br/>no user objects, refresh-only]
    C -- no --> E
    D --> E{glibc stamp stale<br/>or missing?}
    E -- yes --> F[reindex every C.* database<br/>per relation, independently]
    F --> G{all reindexed cleanly?}
    G -- yes --> H[advance the stamp]
    G -- no --> X
    H --> I
    E -- no --> I["for each database<br/>(parallel, bounded by maxParallelDatabases)"]

    subgraph perdb["per-database worker"]
        DPRE[perDatabase.preStart hook] -->|blockOnFailure fails| SKIP[skip this database]
        DPRE --> J{Postgres-tracked<br/>mismatch?}
        J -- yes --> K[reindex every user table<br/>per relation, independently]
        K --> L{all reindexed cleanly?}
        L -- yes --> M[refresh database +<br/>named collation versions]
        L -- no --> DFAIL
        M --> N
        J -- no --> N{partition repair<br/>enabled?}
        N -- yes --> O[find candidate partitioned tables<br/>non-C/POSIX collated key]
        O --> P[for each leaf: find misplaced rows<br/>pg_get_partition_constraintdef]
        P --> Q{rows found?}
        Q -- yes --> R[cross-partition UPDATE via root table<br/>DISABLE/ENABLE TRIGGER USER]
        R --> SCAP{clean, or<br/>safety cap hit?}
        SCAP -- clean --> DOK[perDatabase.onSuccess hook]
        SCAP -- capped --> DFAIL[perDatabase.onFailure hook]
        Q -- no --> DOK
        N -- no --> DOK
    end

    I --> perdb
    perdb --> T[merge results, sorted]
    T --> SUCC{run succeeded?}
    SUCC -- yes --> ONSUCC[onSuccess hooks]
    ONSUCC --> POST
    SUCC -- no --> POST[postRun hooks, always]
    POST --> U{exit 0 or 1}
    U -- 0 --> V[postgresql-setup.service]
    V --> W[postgresql.target]
    U -- 1 --> X
    X -.OnFailure=.-> OF["collation-guard --on-failure<br/>(separate process, runs even on crash)"]
    OF --> ONFAIL[onFailure hooks]
    X -.blocks.-> V
```

A single relation/row failure never blocks the rest: every reindex and
every repair is isolated per-relation so one bad index or one
unrepairable row doesn't stop the whole run from fixing everything else
(see `docs/decisions/0002-per-relation-not-per-database-reindex.md`) —
but *any* failure anywhere still fails the overall run loudly, which
blocks `postgresql-setup.service` and `postgresql.target`. That's
deliberate: a reindex or repair failure is almost always a real
constraint violation that needs a human decision, not something safe to
silently skip past.

Databases are processed concurrently, bounded by
`services.postgresqlCollationGuard.maxParallelDatabases` (default 4) —
the work is I/O-bound (waiting on Postgres over a socket), so this cuts
boot time on a multi-database cluster without needing to raise it
further; see `docs/decisions/0006-hooks-and-parallel-per-database-processing.md`
for why it's bounded rather than unbounded.

## Usage

```nix
{
  inputs.nixos-postgres-maintenance-module.url = "github:dvaerum/nixos-postgres-maintenance-module";

  # in your NixOS configuration:
  imports = [ inputs.nixos-postgres-maintenance-module.nixosModules.default ];
  services.postgresqlCollationGuard.enable = true;
}
```

See `docs/options.md` (generated via `generate-doc.nix`) for the full
option reference, or `nixosModule/options.nix` directly.

### Hooks

Seven hook points, all sharing the same shape -- the original four,
whole-run hooks (`preStart`, `onSuccess`, `onFailure`, `postRun`), plus
a `perDatabase.{preStart,onSuccess,onFailure}` tier that knows which
database it's about:

```nix
services.postgresqlCollationGuard.hooks.perDatabase.onFailure = [
  {
    path = lib.getExe pkgs.curl;
    args = [ "-X" "POST" "https://hooks.example.com/notify" "-d" "@-" ];
    environmentFile = config.sops.secrets."notify-webhook-token".path;
    blockOnFailure = false; # must be set explicitly -- no default
  }
];
```

Every hook gets `COLLATION_GUARD_STAGE` and, where relevant,
`COLLATION_GUARD_DATABASE`/`COLLATION_GUARD_CONTEXT` (JSON)/
`COLLATION_GUARD_ERROR` (a plain one-line summary, no JSON parsing
needed, on failure-shaped stages). `environmentFile` and inline
`environment` merge with those -- any variable name defined by more
than one source is a hard error (`EnvironmentCollisionError`) before
the hook ever runs, never a silent override.

`blockOnFailure` always does something real and specific to that hook
point (abort the run, skip one database, add a failure that can flip
an otherwise-clean exit code, or -- for `onFailure` -- make the
companion systemd unit itself report failed status) -- see
`docs/decisions/0006-hooks-and-parallel-per-database-processing.md`
for the full table. `onFailure` is triggered by systemd's `OnFailure=`
(the one guarantee that survives the guard process being killed or
crashing outright), but the hooks themselves run through the exact
same mechanism as every other stage, from a second `collation-guard
--on-failure` entry point into the same binary.

### CLI

```
collation-guard           # the real run -- reads PGHOST/PGPORT/GLIBC_LOCALES_PATH/
                           # COLLATION_GUARD_* from the environment, as the systemd unit sets them
collation-guard --dry-run # list partition-repair candidates across the cluster, change nothing
```

## Known limitations

- **ICU's `collversion` doesn't always change when behavior does.**
  ICU 72→73 changed root-collation sort order for 331+ locales without
  bumping `collversion` (ICU-22544) — this guard's entire
  Postgres-tracked detection mechanism has a blind spot specific to ICU
  collations as a result. Not fixable without a full data
  re-verification regardless of version match (a much bigger, different
  feature); documented as an accepted gap. Only relevant if a consumer
  uses the `icu` provider.
- **Partition repair assumes exclusive access to the cluster.** It runs
  in the pre-boot window before `postgresql-setup`/`postgresql.target`
  activate, so no application ever has a connection open yet in the
  normal case — but a documented, still-open PG deadlock pattern exists
  if this module is ever invoked against an already-live cluster with
  concurrent writers.
- **No real-drift test coverage for row-level partition repair.** A
  genuinely misplaced row only exists because the same collation's
  comparison behavior changed between validation time and now (an
  actual glibc/ICU version change) — not reproducible in a
  single-locale test sandbox. See
  `docs/learnings/partition-repair-testing.md` for what was tried and
  why; the test suite instead proves the mechanism's load-bearing parts
  separately.

See `docs/decisions/` for the full reasoning behind each piece, and
`docs/learnings/` for other non-obvious findings from building this.

## Future work

- **Blocking external (non-systemd-managed) clients during the run.**
  `postgresql-setup.service`/`postgresql.target` and anything ordered
  after them on *this* host correctly wait for the guard, but a client
  connecting from somewhere else entirely -- another host on the
  network, anything outside this host's own systemd dependency graph --
  has no reason to wait and can connect while the guard is still
  reindexing or mid-repair, against inconsistent state. Not designed or
  implemented yet; no clear mechanism chosen (candidates to look into:
  temporarily tightening `pg_hba.conf`, a connection-limiting setting,
  or some way to advertise "still under maintenance" that external
  tooling could check) -- flagged here as a real gap worth solving, not
  solved.

## Development

```
nix develop         # python3, psycopg, pytest, ruff, mypy, postgresql on PATH
pytest               # fast tier: logic tests against an ephemeral initdb/pg_ctl cluster
nix flake check      # fast tier + slow (systemd-nspawn) tier + the Nix package build
nix fmt              # format both Nix and Python
nix-build generate-doc.nix && cp result docs/options.md   # regenerate the option reference
```

See `AGENTS.md` for the full contributor/agent workflow, including the
versioning policy.

## Documentation

- `docs/decisions/` — one ADR per real design decision, with sources.
- `docs/learnings/` — cross-cutting operational knowledge (known
  limitations, gotchas) not tied to a single decision.
- `docs/options.md` — generated NixOS option reference.
