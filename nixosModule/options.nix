{ lib, ... }:
let
  inherit (lib) mkOption mkEnableOption types;
in
{
  options.services.postgresqlCollationGuard = {
    enable = mkEnableOption "the Postgres collation-drift guard";

    package = mkOption {
      type = types.package;
      description = ''
        The `collation-guard` package to run. Wired automatically to this
        flake's own `packages.<system>.default` by `nixosModules.default` --
        override only to test a different build.
      '';
    };

    partitionRepair = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Also check text-keyed, non-`C`/`POSIX`-collated partition bounds
          and repair any row that's drifted into the wrong physical
          partition under the new collation. See
          docs/decisions/0004-detach-attach-partition-repair.md.
        '';
      };

      maxRepairAttempts = mkOption {
        type = types.ints.positive;
        default = 1000;
        description = ''
          Safety cap on the detach/fix/attach retry loop per partitioned
          table, so a pathological number of misplaced rows fails loudly
          instead of spinning forever.
        '';
      };
    };

    hooks = {
      preStart = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Executables run, in order, before the guard examines any
          database (e.g. to take a pre-emptive backup). Each must exit 0;
          a non-zero exit aborts the guard before any check or repair
          runs. Invoked with no arguments; JSON context is passed via the
          `COLLATION_GUARD_CONTEXT` environment variable
          (`{"stage": "pre_start"}`).
        '';
      };

      onSuccess = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Executables run, in order, after the guard completes with no
          failures (e.g. to notify success or prune old pre-start
          backups). `COLLATION_GUARD_CONTEXT` carries
          `{"stage": "on_success", "databases_processed": [...], "databases_repaired": [...]}`.
        '';
      };

      onFailure = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Executables run, in order, after the guard fails (e.g. to alert
          on-call or trigger a restore). Runs even if the guard's own
          process is killed or crashes, via the unit's `OnFailure=`
          dependency -- not just on a clean non-zero exit.
          `COLLATION_GUARD_CONTEXT` carries
          `{"stage": "on_failure", "failures": [{"database": ..., "relation": ..., "error": ...}, ...]}`.
        '';
      };

      postRun = mkOption {
        type = types.listOf types.package;
        default = [ ];
        description = ''
          Executables run, in order, after the guard finishes -- always,
          whether it succeeded or failed (e.g. to emit a single
          run-completed metric/notification regardless of outcome).
          `COLLATION_GUARD_CONTEXT` carries
          `{"stage": "post_run", "success": true|false}`.
        '';
      };
    };
  };
}
