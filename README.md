# nixos-postgres-maintenance-module

A NixOS module + CLI that closes
[nixpkgs#318777](https://github.com/NixOS/nixpkgs/issues/318777):
Postgres refuses to start once its on-disk collation library version no
longer matches what's recorded in its catalogs. This runs before
`postgresql.target` comes up, detects that drift, and repairs it —
reindexing affected relations and refreshing the recorded version —
rather than leaving the cluster down until a human intervenes.

**Status: early development.** The Postgres-tracked collation-mismatch
detection/repair path (`src/collation_guard/collation.py`) is
implemented and covered by its fast-tier test suite. The C.UTF-8 stamp
check, `template0` handling, partition-bounds repair, and the NixOS
systemd wiring are still being built out — see `docs/decisions/` as
those land for the design reasoning behind each piece, and the project
plan for the full roadmap.

## Why this exists

PostgreSQL versions each collation it uses (`pg_database.datcollversion`,
`pg_collation.collversion`) so it can detect when the underlying
collation library (glibc, ICU) changed underneath it — a changed sort
order silently corrupts indexes and can misplace rows in a
collation-keyed partitioned table. Nixpkgs itself declined to fix this
at the distribution level (`nixpkgs#318777`, closed won't-fix: "there's
nothing we can do" absent a proper mechanism). This project is that
mechanism, packaged so any NixOS host running Postgres can use it.

## Usage

```nix
{
  inputs.nixos-postgres-maintenance-module.url = "github:dvaerum/nixos-postgres-maintenance-module";

  # in your NixOS configuration:
  imports = [ inputs.nixos-postgres-maintenance-module.nixosModules.default ];
  services.postgresqlCollationGuard.enable = true;
}
```

See `docs/options.md` (generated) for the full option reference once
available, or `nixosModule/options.nix` in the meantime.

## Development

```
nix develop        # python3, psycopg, pytest, ruff, mypy, postgresql on PATH
pytest              # fast tier: logic tests against an ephemeral initdb/pg_ctl cluster
nix flake check     # fast tier + slow (systemd-nspawn) tier + the Nix package build
nix fmt             # format both Nix and Python
```

See `AGENTS.md` for the full contributor/agent workflow, including the
versioning policy.

## Documentation

- `docs/decisions/` — one ADR per real design decision, with sources.
- `docs/learnings/` — cross-cutting operational knowledge (known
  limitations, gotchas) not tied to a single decision.
