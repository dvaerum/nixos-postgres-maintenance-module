# hooks.onFailure (whole-run): triggered via the main unit's systemd
# OnFailure= dependency, through a separate `collation-guard
# --on-failure` entry point -- the one guarantee that survives the
# guard process being killed or crashing outright, not just a clean
# non-zero exit. Exercised end-to-end by
# tests/nixos/on-failure-hook.nix, driven through a real REINDEX
# failure so the companion unit actually fires, not just evaluates.
{ pkgs, lib, ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;

    hooks.onFailure = [
      {
        path = lib.getExe (
          pkgs.writeShellApplication {
            name = "collation-guard-onfailure-whole-run-marker";
            text = ''
              {
                printf 'ERROR=%s\n' "$COLLATION_GUARD_ERROR"
                printf 'CONTEXT=%s\n' "$COLLATION_GUARD_CONTEXT"
              } > /tmp/collation-guard-whole-run-onfailure-marker
            '';
          }
        );
        args = [ ];
        environment = { };
        environmentFile = null;
        blockOnFailure = false; # must be set explicitly -- no default
      }
    ];
  };
}
