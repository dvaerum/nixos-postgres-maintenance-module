# Index of the example scenarios in this directory -- each file is a
# complete, self-contained NixOS module fragment for
# services.postgresqlCollationGuard.*, readable on its own. This just
# gives tests/nixos/examples.nix (and anything else) a stable name to
# import by, instead of a bare relative path per file.
{
  minimal = ./minimal.nix;
  tuning = ./tuning.nix;
  connectionLockdownDisabled = ./connection-lockdown-disabled.nix;
  perDatabaseOnFailureHook = ./per-database-onfailure-hook.nix;
}
