{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.postgresqlCollationGuard;
  pgCfg = config.services.postgresql;

  contextFile = "/run/postgresql-collation-guard/last-run.json";
  lockdownFile = "/run/postgresql-collation-guard/lockdown.conf";

  # Needed whenever EITHER onFailure hooks are configured OR
  # connectionLockdown is enabled (its default) -- a crash mid-lock
  # must still be cleaned up even if nobody configured a notification
  # hook at all, since OnFailure= is the only way a dead process can
  # trigger anything for itself.
  needsOnFailureUnit = cfg.hooks.onFailure != [ ] || cfg.connectionLockdown.enable;

  # Extracted to its own file (nixosModule/validate-hook.nix) so it's
  # testable in isolation -- see tests/validate-hook.nix.
  validateHook = import ./validate-hook.nix;

  hookToJSON = hook: {
    inherit (hook)
      path
      args
      environment
      environmentFile
      ;
    blockOnFailure = hook.blockOnFailure;
  };

  validatedHookList = optionPath: hooks: map (validateHook optionPath) hooks;

  # All seven hook lists, no asymmetry -- same shape, same validation,
  # serialized once and read by both entry points (the main run and
  # --on-failure) via the same COLLATION_GUARD_HOOKS_FILE.
  hooksFile = pkgs.writeText "collation-guard-hooks.json" (
    builtins.toJSON {
      preStart = map hookToJSON (validatedHookList "preStart" cfg.hooks.preStart);
      onSuccess = map hookToJSON (validatedHookList "onSuccess" cfg.hooks.onSuccess);
      onFailure = map hookToJSON (validatedHookList "onFailure" cfg.hooks.onFailure);
      postRun = map hookToJSON (validatedHookList "postRun" cfg.hooks.postRun);
      perDatabase = {
        preStart = map hookToJSON (validatedHookList "perDatabase.preStart" cfg.hooks.perDatabase.preStart);
        onSuccess = map hookToJSON (
          validatedHookList "perDatabase.onSuccess" cfg.hooks.perDatabase.onSuccess
        );
        onFailure = map hookToJSON (
          validatedHookList "perDatabase.onFailure" cfg.hooks.perDatabase.onFailure
        );
      };
    }
  );
in
{
  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      # /run is tmpfs; systemd-tmpfiles-setup.service (which applies this)
      # runs at early boot, well before postgresql-collation-guard.service
      # -- the directory exists by the time the guard needs to write the
      # context file, with no explicit ordering dependency required.
      "d /run/postgresql-collation-guard 0750 ${pgCfg.superUser} postgres - -"
    ];

    # include_if_exists is re-resolved on every pg_reload_conf(), unlike
    # hba_file itself (fixed at server start, and NixOS points it at a
    # read-only Nix store path) -- this is what lets LockdownManager
    # reject/restore access at runtime with no restart. The file
    # doesn't exist on a normal day, so this is a silent no-op unless
    # something is actually locked. mkBefore so it's checked ahead of
    # every other rule (first match wins in pg_hba.conf). See
    # docs/decisions/0007.
    services.postgresql.authentication = lib.mkIf cfg.connectionLockdown.enable (
      lib.mkBefore "include_if_exists ${lockdownFile}"
    );

    systemd.services.postgresql-collation-guard = {
      description = "Reindex/refresh any database whose collation library version changed, and repair drifted text-partition bounds";

      requires = [ "postgresql.service" ];
      after = [ "postgresql.service" ];
      before = [ "postgresql-setup.service" ];

      environment = {
        PGHOST = "/run/postgresql"; # the postgresql module's own fixed default unix_socket_directories entry
        PGPORT = toString pgCfg.settings.port;
        GLIBC_LOCALES_PATH = "${config.i18n.glibcLocales}";
        COLLATION_GUARD_PARTITION_REPAIR_ENABLE = lib.boolToString cfg.partitionRepair.enable;
        COLLATION_GUARD_MAX_REPAIR_ATTEMPTS = toString cfg.partitionRepair.maxRepairAttempts;
        COLLATION_GUARD_MAX_PARALLEL_DATABASES = toString cfg.maxParallelDatabases;
        COLLATION_GUARD_CONTEXT_FILE = contextFile;
        COLLATION_GUARD_HOOKS_FILE = "${hooksFile}";
        COLLATION_GUARD_LOCKDOWN_FILE = lockdownFile;
        COLLATION_GUARD_CONNECTION_LOCKDOWN_ENABLE = lib.boolToString cfg.connectionLockdown.enable;
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = pgCfg.superUser;
        Group = "postgres";
        # preStart/onSuccess/postRun are invoked by the guard itself
        # now, not via ExecStartPre/ExecStartPost/ExecStopPost -- see
        # docs/decisions/0006 for why unifying them under one
        # in-process mechanism (shared hook shape, shared
        # environment-merge/collision rules) was worth losing
        # systemd's free orchestration of them.
        ExecStart = lib.getExe cfg.package;
      };
    }
    // lib.optionalAttrs needsOnFailureUnit {
      unitConfig.OnFailure = [ "postgresql-collation-guard-on-failure.service" ];
    };

    # Separate companion unit, triggered via the main unit's
    # OnFailure= above -- this fires on ANY failure mode (non-zero
    # exit, crash, kill, timeout), not just a clean non-zero exit a
    # plain ExecStopPost could also observe, which is the one thing a
    # plain post-run hook can't guarantee. Its ExecStart is a second,
    # equally small entry point into the same binary (--on-failure):
    # first an unconditional lockdown-file cleanup (independent of
    # whether any onFailure hooks are configured at all -- see
    # docs/decisions/0007), then the exact same run_hook()/
    # merge_environment() code as every other stage for any configured
    # onFailure hooks -- not a separate implementation.
    systemd.services."postgresql-collation-guard-on-failure" = lib.mkIf needsOnFailureUnit {
      description = "On-failure hooks for postgresql-collation-guard.service";
      environment = {
        COLLATION_GUARD_HOOKS_FILE = "${hooksFile}";
        COLLATION_GUARD_CONTEXT_FILE = contextFile;
        COLLATION_GUARD_LOCKDOWN_FILE = lockdownFile;
        PGHOST = "/run/postgresql";
        PGPORT = toString pgCfg.settings.port;
      };
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${lib.getExe cfg.package} --on-failure";
      };
    };

    # postgresql-setup is defined upstream; these list options merge with
    # its own definition rather than replacing it.
    systemd.services.postgresql-setup = {
      requires = [ "postgresql-collation-guard.service" ];
      after = [ "postgresql-collation-guard.service" ];
    };
  };
}
