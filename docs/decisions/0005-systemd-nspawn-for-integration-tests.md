# 0005: `systemd-nspawn` containers, not QEMU VMs, for integration tests

## Decision

The slow-tier integration tests (`tests/nixos/*.nix`) use
`pkgs.testers.nixosTest`'s `containers = { ... }` backend
(systemd-nspawn), not the default `nodes = { ... }` (QEMU) backend.

## Why

Every scenario these tests need to prove is pure systemd unit ordering
and Postgres SQL behavior -- no kernel modules, no SUID binaries, no
graphical output. A full QEMU VM boot is unnecessary weight for that.

Confirmed safe as the default for `services.postgresql` specifically,
not assumed: `nixos/tests/hedgedoc.nix` and `nixos/tests/grafana/basic.nix`
already run `services.postgresql` (the same `Type=notify` unit, both
TCP and UNIX-socket clients) under this exact backend in nixpkgs' own
CI. This project's own tests (`tests/nixos/reindex-failure.nix` in
particular, which deliberately corrupts a running cluster's catalog
state and drives `psql` directly) reproduce the same patterns that
reference tests already exercise.

## Corrections found by actually using it, not from the feature's docs

- **`sudo` does not work inside the container.** Confirmed as an open
  constraint from the feature's own PR discussion
  (nixpkgs#478109). Every test script uses `runuser -u postgres --`
  instead of `sudo -u postgres`.
- **Avoid `machine.shutdown()` mid-test.** Container shutdown pays a
  real, measurable unmount cost per bind-mounted store path -- nixpkgs'
  own `nixos/tests/grafana` test was patched (2026-09-20) specifically
  to stop doing this. `idempotent.nix` restarts just the guard unit
  instead of rebooting the whole container.
- **Stopping units above `postgresql.service` also stops
  `postgresql.service` itself.** Confirmed empirically in
  `reindex-failure.nix`: there's no `StopWhenUnneeded=`-style isolation
  keeping Postgres running once everything above it in the dependency
  chain is stopped. Tests that need to inject state into a *running*
  cluster before re-triggering the chain have to stop everything, then
  explicitly restart `postgresql.service` on its own first.
- **A local variable named `log` inside `testScript` silently breaks
  the driver's own static type checker.** `log` is a name reserved by
  the NixOS test driver's own `testScript` namespace (its structured
  logger object); shadowing it with an unrelated local variable
  produces a confusing `AbstractLogger has no attribute ...` type
  error pointing at the *wrong* line. Avoid the name entirely in test
  scripts.

## Fallback not yet needed

If a future scenario hits one of the documented container limitations
(restricted systemd hardening directives in particular, since that's
the one `services.postgresql`'s own unit could plausibly touch), that
test should fall back to `nodes = { ... }` (plain QEMU) instead --
nothing written so far has needed this.
