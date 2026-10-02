{ lib, ... }:
let
  inherit (lib) mkOption mkEnableOption types;

  hookType = types.submodule {
    options = {
      path = mkOption {
        type = types.path;
        description = ''
          Executable to run, e.g. `lib.getExe pkgs.curl` or an explicit
          path into a derivation's own `/bin` directory.
        '';
      };

      args = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Extra arguments passed to the hook executable.";
      };

      environment = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = ''
          Inline environment variables for this hook. Merged with
          `environmentFile` and the stage's own default variables
          (`COLLATION_GUARD_STAGE`/`DATABASE`/`CONTEXT`/`ERROR`) -- a key
          defined by more than one of those three sources is a hard
          error at run time (`EnvironmentCollisionError`), never a
          silent override. `COLLATION_GUARD_CONTEXT` is always present
          and always valid JSON, on every stage (an empty `{}` where
          there's nothing yet to report) -- no need to check whether it
          exists before parsing it. See docs/decisions/0006.
        '';
      };

      environmentFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          `EnvironmentFile`-style `KEY=VALUE` file (e.g. a sops secret
          path), merged with `environment` and the stage's own default
          variables under the same no-collision rule.
        '';
      };

      blockOnFailure = mkOption {
        type = types.nullOr types.bool;
        default = null;
        description = ''
          Whether a non-zero exit from this hook should be treated as a
          failure of whatever it's attached to -- aborting the whole run
          immediately for `preStart`; skipping just that one database
          for `perDatabase.preStart`; adding an extra failure entry
          (which can flip an otherwise-successful run's exit code) for
          every other stage, including the `onFailure` companion unit's
          own reported status. No default -- must be set explicitly, or
          evaluation throws naming the option path. See the hook-point
          descriptions below for exactly what each stage blocks.
        '';
      };
    };
  };
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

    maxParallelDatabases = mkOption {
      type = types.ints.positive;
      default = 4;
      description = ''
        How many databases to process concurrently (bounded, not
        unbounded -- all databases share the same Postgres instance's
        disk I/O, shared buffers, and WAL writer, so an unbounded
        parallelism could make things slower, not faster, on a cluster
        with many databases).
      '';
    };

    connectionLockdown = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Reject new connections to a database for exactly the
          duration it's actively being reindexed/repaired, and
          terminate any session already connected to it at that
          moment -- closing the gap where a client not itself ordered
          after `postgresql-setup.service`/`postgresql.target` (local
          or remote, since `postgresql.service` is already accepting
          connections by the time this guard runs) could otherwise
          connect against inconsistent state. A database with nothing
          to fix is never locked. Default `true` since this closes a
          real correctness gap, but every existing deployment gets this
          behavior on the next upgrade with no config change -- turn it
          off here if there's a specific reason to allow concurrent
          connections during the guard's run. See
          docs/decisions/0007-connection-lockdown-during-repair.md.
        '';
      };
    };

    partitionRepair = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Also check text-keyed, non-`C`/`POSIX`-collated partition bounds
          and repair any row that's drifted into the wrong physical
          partition under the new collation. See
          docs/decisions/0004-cross-partition-update-for-partition-repair.md.
        '';
      };

      maxRepairAttempts = mkOption {
        type = types.ints.positive;
        default = 1000;
        description = ''
          Safety cap on the repair loop for one partitioned table, where
          each attempt is a full pass over every leaf partition, repeated
          until a pass moves nothing. Capped rather than single-pass
          because moving a misplaced row into a different partition can
          require that partition's own next pass to re-check it, and
          there's no proof this always converges in one sweep (see
          docs/learnings/partition-repair-testing.md). Hitting the cap
          fails loudly instead of spinning forever.
        '';
      };
    };

    hooks = {
      preStart = mkOption {
        type = types.listOf hookType;
        default = [ ];
        description = ''
          Run, in order, before the guard examines any database (e.g. to
          take a pre-emptive backup). `blockOnFailure = true` + a
          non-zero exit aborts the whole run immediately, before
          enumerating or connecting to any database.
        '';
      };

      onSuccess = mkOption {
        type = types.listOf hookType;
        default = [ ];
        description = ''
          Run, in order, once -- only when every database processed with
          zero failures (unlike `postRun`, which always runs regardless
          of outcome). `blockOnFailure = true` + a non-zero exit adds a
          failure to an otherwise-clean run, flipping its exit code to 1.
        '';
      };

      onFailure = mkOption {
        type = types.listOf hookType;
        default = [ ];
        description = ''
          Run after the guard fails or crashes outright -- even if the
          guard's own process is killed, via the unit's `OnFailure=`
          dependency, not just a clean non-zero exit (the one guarantee
          a dead process can't arrange for itself). The *trigger* stays
          systemd-native; the hooks themselves use the exact same
          mechanism as every other stage. `blockOnFailure = true` + a
          non-zero exit makes the companion unit itself report failed
          status (visible to `systemctl --failed` and anything
          monitoring systemd unit health) -- there's nothing left to
          block booting at this point, the main run has already failed.
        '';
      };

      postRun = mkOption {
        type = types.listOf hookType;
        default = [ ];
        description = ''
          Run, in order, after the guard finishes -- always, whether it
          succeeded or failed, and after `onSuccess` if that also ran.
          `blockOnFailure = true` + a non-zero exit adds a failure even
          after every database already finished cleanly, which can flip
          an otherwise-clean run's exit code to 1.
        '';
      };

      perDatabase = {
        preStart = mkOption {
          type = types.listOf hookType;
          default = [ ];
          description = ''
            Run, in order, before the guard examines *each* database
            (e.g. a per-database backup) -- `COLLATION_GUARD_DATABASE`
            names which one. `blockOnFailure = true` + a non-zero exit
            skips processing of that one database entirely (no reindex,
            no partition repair, not counted as processed); every other
            database in the same run is unaffected.
          '';
        };

        onSuccess = mkOption {
          type = types.listOf hookType;
          default = [ ];
          description = ''
            Run after a database's own processing succeeds.
            `blockOnFailure = true` + a non-zero exit adds a failure for
            that database even though its actual Postgres processing was
            clean -- for a notification that's itself load-bearing.
          '';
        };

        onFailure = mkOption {
          type = types.listOf hookType;
          default = [ ];
          description = ''
            Run after a database's own processing fails.
            `COLLATION_GUARD_ERROR` carries a summary of what failed.
            `blockOnFailure = true` + a non-zero exit adds a second,
            distinct failure entry alongside the database's original one
            -- both visible independently.
          '';
        };
      };
    };
  };
}
