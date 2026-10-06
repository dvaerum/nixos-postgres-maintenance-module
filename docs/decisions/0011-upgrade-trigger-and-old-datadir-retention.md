# 0011: Explicit opt-in trigger, idempotent by construction, old dataDir kept by default

## Decision

Two decisions about the orchestration `0010` deferred:

1. **`upgrade.enable` must be explicitly set `true`.** The module never
   runs `pg_upgrade` just because the configured `package`'s major
   differs from what's on disk — unlike the existing collation guard
   (which is always-on by design), this is a one-way data migration,
   not an idempotent repair, and must never fire as a side effect of
   an unrelated package bump.
2. **The old data directory is kept after a successful upgrade, by
   default forever.** `upgrade.oldDataDirRetentionDays` (nullable
   int, default `null`) governs automatic cleanup: `null` means never
   auto-delete; `0` means delete it the moment `main.run()` first
   proves the new cluster is actually reachable (a live connection
   succeeds); any positive `N` deletes it `N` days after completion
   via a separate timer, not the upgrade unit itself.

## Why an explicit flag, not "versions differ" like the main guard

The main collation guard is deliberately always-on (`0007`, `0009`):
every stage it runs is either a no-op (nothing stale) or a repair of
something already broken, safe to repeat every boot indefinitely.
`pg_upgrade` has neither property — it's a real migration with its own
maintenance-window, extension-compatibility, and disk-space
considerations (`0010`), run once per major-version transition, not
something to trigger as a side effect of whatever else changed in the
same `nixos-rebuild switch` that happened to also bump a package
version (a security point release within the same major must never be
mistaken for a trigger in the first place, but even a deliberate major
bump deserves a conscious acknowledgment, not an automatic reaction).

## Idempotent once genuinely complete, not by requiring `enable` to be toggled off

`upgrade.enable = true` is a persistent declaration of intent ("this
host should end up on the new major"), not a one-shot command — it can
stay `true` indefinitely across any number of subsequent
`nixos-rebuild switch` runs without re-attempting anything, once a
*genuinely completed* attempt is on record.

**This wasn't quite true as first built, and a critical review caught
it before it shipped to anyone running real data through it.** The
original gate was simply "does the new data directory already have a
`PG_VERSION` file" — but `initdb_new_cluster()` writes that file
*before* `pg_upgrade` itself is even attempted. If `pg_upgrade` then
failed (a real, expected failure mode — incompatible extensions,
catalog issues, disk exhaustion mid-copy), the *next* boot would see
`PG_VERSION` already there and silently return "nothing to do" —
permanently. `postgresql.service` would then start cleanly against an
empty, half-migrated cluster, with zero signal anything had ever gone
wrong. A genuinely dangerous false "success."

The fix: one state file
(`/var/lib/postgresql-collation-guard/upgrade-completed.json`),
written in two steps. `record_upgrade_attempt_started()` runs
immediately before `initdb_new_cluster()` ever touches the new data
directory; `record_upgrade_attempt_completed()` runs only once
`pg_upgrade` has actually succeeded. `run_upgrade()`'s gate now reads:

- New data directory has a `PG_VERSION` **and** a completed attempt on
  record → genuinely done, no-op (the normal case on every boot after
  a successful migration).
- New data directory has a `PG_VERSION` but the recorded attempt was
  never completed → `IncompleteUpgradeError`, raised on **every**
  subsequent invocation — never silently retried (`pg_upgrade` cannot
  resume into an already-touched target; its own documentation is
  explicit that a failed run needs a fresh target directory, not an
  in-place retry), and never silently treated as done. An operator has
  to inspect and remove the new data directory before the next boot
  can attempt the migration again.
- New data directory has a `PG_VERSION` with **no** attempt record at
  all → a pre-existing cluster entirely unrelated to this feature
  (e.g. `upgrade.enable` was turned on after the new version was
  already running normally) — a genuine, safe no-op.

A second, related gap closed by the same review: `old_datadir ==
new_datadir` (a real misconfiguration — `oldDataDir` and the configured
`services.postgresql.dataDir` happening to coincide) used to be
silently absorbed by the first bullet above, since the identical path's
own `PG_VERSION` would read as "already upgraded." `run_upgrade()` now
checks for this explicitly, first, before anything else, raising
`IdenticalDataDirectoriesError`.

## Why the old dataDir is kept by default

`pg_upgrade`'s own documentation explicitly recommends keeping the old
cluster until the new one is verified in production — the whole
operation is irreversible the moment the new cluster starts accepting
writes under `--link`/`--clone` (`0010`), and even under `--copy` (the
one mode that leaves the old cluster's files completely untouched),
deleting the only remaining way to inspect or roll back to the
pre-upgrade state the instant the migration finishes trades a one-time
disk-space saving for a permanent loss of recourse if something the
upgrade didn't cover (an incompatible extension, an application bug
surfaced only by the new major) shows up after the fact.

## A time-gated retention window, not an unbounded default

Keeping the old dataDir forever by default is the safe default, not
the assumed-forever answer — real deployments do eventually want the
disk space back once the new cluster is confirmed solid.
`oldDataDirRetentionDays` makes that an explicit, calendar-time-based
decision rather than a manual `rm -rf` someone has to remember:

- `null` (default): never auto-delete. The operator reclaims the space
  manually, whenever they're actually ready.
- `0`: delete the moment the new cluster is first proven reachable —
  "no waiting period, I'm already confident." **Deliberately not
  handled inside `run_upgrade()`/the upgrade unit itself** — that unit
  runs strictly `Before = ["postgresql.service"]`, before either
  cluster has ever started, so it can never prove the new cluster
  actually comes up. The original design did delete it right there,
  inline, immediately after `pg_upgrade` reported success; a critical
  review caught that this trades a one-time disk-space saving for
  *total* data loss if `postgresql.service` subsequently failed to
  start for any unrelated reason (a bad `postgresql.settings` value, a
  missing extension `.so` introduced in the same `nixos-rebuild
  switch`) — both clusters gone, with nothing left to roll back to.
  The deletion now happens in `main.run()` (the main collation guard's
  own entry point, which only ever runs *after* `postgresql.service`
  has started) — specifically right after its first real connection to
  the new cluster succeeds. Idempotent and cheap to call on every boot
  after the first (the directory is simply already gone).
- `N > 0`: delete `N` days after the upgrade completed.

Neither case ever removes an *incomplete* attempt's old data directory
— see the previous section's `IncompleteUpgradeError`: that data is the
only copy left after a failed migration, and forcing manual inspection
is the whole point, not an unattended `rm -rf` racing an operator
trying to diagnose the failure.

The `N > 0` case can't be checked by the upgrade unit itself (it runs
once, opt-in, not every boot per the decision above) — elapsed
calendar time has to be evaluated independently of any particular boot,
so cleanup is a separate `systemd.timers` entry that compares the
current time against a completion timestamp recorded when the upgrade
finished, not a repeat invocation of the upgrade unit.
