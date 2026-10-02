# Minimal usage -- turn the guard on, accept every other default.
# Mirrors the README's own first example; proven to still evaluate
# against the current option schema by tests/nixos/examples.nix.
{ ... }:
{
  services.postgresqlCollationGuard.enable = true;
}
