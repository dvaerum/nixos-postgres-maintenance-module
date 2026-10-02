# A preStart hook with blockOnFailure = true -- the one hook point
# that aborts the entire run before any database is ever examined.
# Exercised end-to-end by tests/nixos/pre-start-hook.nix: proves the
# guard's own unit fails immediately and zero databases are ever
# processed, not just that the hook itself ran.
{ pkgs, lib, ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;

    hooks.preStart = [
      {
        path = lib.getExe (
          pkgs.writeShellApplication {
            name = "collation-guard-prestart-marker";
            text = ''
              printf 'STAGE=%s\n' "$COLLATION_GUARD_STAGE" > /tmp/collation-guard-prestart-marker
              exit 1
            '';
          }
        );
        args = [ ];
        environment = { };
        environmentFile = null;
        blockOnFailure = true;
      }
    ];
  };
}
