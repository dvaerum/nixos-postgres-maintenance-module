# A perDatabase.onFailure hook, driven end-to-end by
# tests/nixos/examples.nix: writes the three env vars this hook point
# guarantees (COLLATION_GUARD_DATABASE/ERROR/CONTEXT) to a marker file
# under the guard's own /run directory instead of curl-ing a real
# webhook, so the wiring is provable inside a container with no
# network access. Not /tmp: PrivateTmp=true on the unit isolates that
# into a private namespace invisible to the test's own shell.
{ pkgs, lib, ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;

    hooks.perDatabase.onFailure = [
      {
        path = lib.getExe (
          pkgs.writeShellApplication {
            name = "collation-guard-onfailure-marker";
            text = ''
              {
                printf 'DATABASE=%s\n' "$COLLATION_GUARD_DATABASE"
                printf 'ERROR=%s\n' "$COLLATION_GUARD_ERROR"
                printf 'CONTEXT=%s\n' "$COLLATION_GUARD_CONTEXT"
              } > /run/postgresql-collation-guard/onfailure-marker
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
