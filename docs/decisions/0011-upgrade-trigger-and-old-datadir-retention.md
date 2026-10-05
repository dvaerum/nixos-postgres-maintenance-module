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
   auto-delete; `0` means delete immediately once the upgrade's own
   post-upgrade verification passes; any positive `N` deletes it `N`
   days after completion via a separate timer, not the upgrade unit
   itself.

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

## Idempotent by construction, not by requiring `enable` to be toggled off

`upgrade.enable = true` is a persistent declaration of intent ("this
host should end up on the new major"), not a one-shot command — it can
stay `true` indefinitely across any number of subsequent
`nixos-rebuild switch` runs without re-attempting anything, because the
orchestration's own top-level gate is simply whether the *new*
`dataDir` already has a `PG_VERSION` file: `initdb`/`pg_upgrade` only
ever run against a data directory that doesn't exist yet
(`read_pg_version()`, already implemented in `0010`, returns `None`
for exactly that case). No separate "already ran" marker is needed,
and no requirement to remember to flip `enable` back off after a
successful run.

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
- `0`: delete immediately once the upgrade's own post-upgrade
  verification passes — "no waiting period, I'm already confident."
- `N > 0`: delete `N` days after the upgrade completed.

The `N > 0` case can't be checked by the upgrade unit itself (it runs
once, opt-in, not every boot per the decision above) — elapsed
calendar time has to be evaluated independently of any particular boot,
so cleanup is a separate `systemd.timers` entry that compares the
current time against a completion timestamp recorded when the upgrade
finished, not a repeat invocation of the upgrade unit.
