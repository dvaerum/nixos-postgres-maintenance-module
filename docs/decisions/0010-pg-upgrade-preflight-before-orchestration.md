# 0010: `pg_upgrade` preflight/validation first, orchestration deferred

## Decision

`src/collation_guard/upgrade.py` implements the detection, validation,
and transfer-mode-resolution logic a major-version `pg_upgrade` needs
*before* anything irreversible happens: reading the real on-disk
`PG_VERSION`, validating it against the configured old package,
deciding whether an upgrade is even needed, and resolving a requested
transfer mode (`copy`/`clone`/`link`/`auto`) against the two real
filesystem preflight checks each mode actually requires. The actual
`initdb`/`pg_upgrade` invocation, the NixOS option surface
(`services.postgresqlCollationGuard.upgrade.*`), and the systemd unit
wiring are deliberately **not** part of this decision — see "What's
still open" below.

Every function in `upgrade.py` is pure or near-pure (reads real
on-disk/filesystem state, makes no irreversible change), so all 28
tests in `tests/test_upgrade.py` run in the fast pytest tier with no
second real Postgres binary needed. The orchestration itself will be
proven separately against a real two-major-version cluster, the same
split `0008` already established for `icuDriftTest`: fast, logic-level
tests live in `tests/*.py`; a real, binary-swapping heavy test lives
under `tests/nixos/` as its own `packages.<name>`, run by hand, not
wired into `nix flake check`/CI.

## Why a major-version upgrade belongs in this project at all

This project's entire reason for existing (`nixpkgs#318777`) is that
Postgres refuses to start once its on-disk collation catalogs disagree
with the collation library actually linked into the running binary —
and a `pg_upgrade` to a new major version is one of the most common
ways that disagreement gets introduced in the first place: the new
major version's Postgres binary can easily be built against a
different glibc or ICU than the one the old cluster was initialized
under. `0003`'s glibc stamp is explicitly designed to survive exactly
this (`COMMENT ON DATABASE postgres`, "survives `pg_dumpall`/
`pg_upgrade`"), and the main guard already runs on every boot before
`postgresql.target` regardless of *why* the collation library changed
— so the post-upgrade cluster is already covered by the existing guard
on its first boot under the new binary. What's missing is the upgrade
step itself: nixpkgs's own `services.postgresql` module has no
built-in major-version-upgrade mechanism at all (confirmed against
search.nixos.org's option index — no `upgrade` option exists under
`services.postgresql`), leaving it as an entirely manual, undocumented
dance (stop the service, run `pg_upgrade` by hand, swap the package,
migrate the data directory) that every NixOS Postgres operator either
already knows or has to rediscover. Driving that transition safely is
the same kind of gap this project already closes for collation drift,
just one step earlier in the cluster's lifecycle.

## On-disk `PG_VERSION` is authoritative, never the configured package

`validate_old_version()` raises `VersionMismatchError` the moment
`<datadir>/PG_VERSION` disagrees with the configured `oldPackage`'s
`psqlSchema` — the same principle `0009` already established for
`ConnectionLimitLockdownManager`'s original-limit restore: the real
on-disk state is the only thing this project ever trusts once
something irreversible is about to run. A misconfigured `oldPackage`
(or a cluster that's secretly a different major version than the
module assumes) must fail loudly here, before `pg_upgrade` itself ever
starts, not partway through it. A *missing* `PG_VERSION` (no cluster
there yet) is deliberately a distinct, non-error state —
`upgrade.enable` configured ahead of a cluster's first-ever boot must
stay a no-op, not a crash.

## Four transfer modes, and why `auto` never substitutes `link`

`resolve_transfer_mode()` accepts `copy`, `clone`, `link`, or `auto`,
each running its own preflight before `pg_upgrade` ever sees it:

- **`copy`** (`pg_upgrade`'s own default) needs no filesystem
  relationship between the old and new data directories, only enough
  free space for a full independent duplicate — `check_disk_space()`'s
  `2.0x` multiplier is that mode's real requirement: the old cluster's
  current on-disk size, duplicated, while the original stays fully
  intact alongside it.
- **`clone`** and **`link`** both require the old and new data
  directories to share a filesystem (`same_filesystem()`, matching
  `st_dev`); `clone` additionally requires reflink/copy-on-write
  support, which `reflink_supported()` verifies by actually attempting
  one (`cp --reflink=always` against a throwaway probe file) rather
  than inferring it from a filesystem-type or kernel-version table —
  the same empirical-over-inferred philosophy `0008` already applied
  to ICU/glibc multi-versioning: a hardcoded compatibility list drifts
  out of date (new filesystems and kernels gain reflink support,
  distro configs vary) independently of this project's own release
  cycle, while an actual attempt never can.
- **`auto`** tries `clone` first (both checks must pass) and falls
  back to `copy` (after `copy`'s own disk-space preflight) if either
  fails — but it **never** falls back to `link`. `link` hard-links
  every file between the old and new data directories: once the new
  cluster starts writing, the old cluster's files are silently
  corrupted too, with no way back. That's acceptable only as an
  explicit, deliberate request from whoever configured `transferMode`
  directly — never a substitution `auto` is allowed to make on the
  caller's behalf. An explicitly requested `clone` or `link` that
  fails its own preflight raises `TransferModeUnavailableError` rather
  than silently falling back to a different mode, for the same reason.

## What's still open

This ADR covers the preflight/validation slice only. Deliberately not
yet decided, and not implemented:

- The actual `initdb`/`pg_upgrade` invocation and its own failure
  handling.
- The `services.postgresqlCollationGuard.upgrade.*` option surface
  (`enable`, `oldPackage`, `transferMode`, and whatever else the
  orchestration itself ends up needing) and its systemd unit ordering
  relative to the existing collation guard.
- A heavy-tier `tests/nixos/*.nix` test against a real two-major-version
  cluster, exposed as its own `packages.pgUpgradeTest` — same pattern
  as `0008`'s `icuDriftTest`, not wired into `checks`/CI.
- `README.md` documentation of the feature once the above exists.
