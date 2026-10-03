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

          **Never put a secret (token, password, API key) in here.**
          Every value is serialized by this module into a JSON file
          built into the Nix store -- world-readable, and pushed to any
          configured binary cache -- regardless of `connectionLockdown`
          or anything else. Use `environmentFile` below for anything
          sensitive.
        '';
      };

      environmentFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          `EnvironmentFile`-style `KEY=VALUE` file, merged with
          `environment` and the stage's own default variables under the
          same no-collision rule. This is where a secret belongs: only
          the *path* is written into the Nix store, never the file's
          contents, so point it at a decrypted sops-nix/agenix secret
          (e.g. `config.sops.secrets."my-hook-token".path`) or
          equivalent -- never at a plain file checked into this
          repository or a Nix store path itself (which would defeat the
          whole point, since store paths are world-readable).
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

    onFailureService = {
      user = mkOption {
        type = types.str;
        default = "postgres";
        description = ''
          OS user the `postgresql-collation-guard-on-failure` companion
          unit (triggered via the main unit's `OnFailure=`) runs as.
          Defaults to match `services.postgresql.superUser` (also
          "postgres" unless overridden) -- the same Postgres superuser
          the main unit itself runs as, since this unit's job is
          identical in kind (running the exact same hook mechanism,
          plus the crash-recovery lockdown-file cleanup, which connects
          to Postgres as this user) -- override only for a deployment-
          specific reason, e.g. a hardened setup that runs `onFailure`
          hooks under a dedicated, more restricted account.
        '';
      };

      group = mkOption {
        type = types.str;
        default = "postgres";
        description = ''
          OS group the `postgresql-collation-guard-on-failure` companion
          unit runs as. See `user` above for why it defaults to match
          the main unit.
        '';
      };
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
          connect against inconsistent state. A database with no stale
          collation and no partition-repair-eligible table is never
          locked -- eligibility is a structural applicability check,
          not proof a row is actually misplaced, so an eligible
          database can still be locked and found clean. Default `true`
          since this closes a real correctness gap, but every existing
          deployment gets this behavior on the next upgrade with no
          config change -- turn it off here if there's a specific
          reason to allow concurrent connections during the guard's
          run. See
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
        default = 3;
        description = ''
          Safety cap on the repair loop for one partitioned table, where
          each attempt is a full pass over every leaf partition, repeated
          until a pass moves nothing. Two passes provably suffice given
          `connectionLockdown` excludes every other writer during repair
          (the default) -- see
          docs/learnings/partition-repair-convergence.md for the full
          reasoning. The default of 3 is that proven bound plus one
          pass of margin: hitting the cap is a signal something
          unexpected is happening, not evidence the table just needed
          more time, so it fails loudly rather than retrying up to some
          much larger number.
        '';
      };
    };

    hooks = {
      timeoutSec = mkOption {
        type = types.ints.positive;
        default = 90;
        description = ''
          Per-hook timeout, applied to every one of the seven hook
          points uniformly. A hook has no inherent bound on how long
          it can run, and every hook-running call in this project
          blocks synchronously on it -- a hung hook (a stuck webhook
          script, a misconfigured notifier) would otherwise stall this
          oneshot unit, and hence `postgresql.target`, indefinitely.
          Defaults to systemd's own `DefaultTimeoutStartSec` (90s) --
          the bound a hung hook was already implicitly subject to via
          the unit's own start timeout, now enforced per-hook instead
          and explicit rather than relying on that systemd default.
        '';
      };

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
