{ config, lib, ... }:
let
  cfg = config.services.postgresqlCollationGuard;
  pgCfg = config.services.postgresql;

  contextFile = "/run/postgresql-collation-guard/last-run.json";

  # Each hook executable is invoked as `<exe> <stage>`, not with a
  # per-stage env var -- systemd's `Environment=` is unit-wide, so a
  # single var can't vary across ExecStartPre/Post/StopPost of the same
  # unit. The stage name as argv[1] plus the shared context file (below)
  # gives every hook the same two inputs regardless of which stage fired
  # it.
  mkHookCommands = stage: hooks: map (pkg: "${lib.getExe pkg} ${stage}") hooks;
in
{
  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      # /run is tmpfs; systemd-tmpfiles-setup.service (which applies this)
      # runs at early boot, well before postgresql-collation-guard.service
      # -- the directory exists by the time any hook or the guard itself
      # needs to read/write the context file, with no explicit ordering
      # dependency required.
      "d /run/postgresql-collation-guard 0750 ${pgCfg.superUser} postgres - -"
    ];

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
        COLLATION_GUARD_CONTEXT_FILE = contextFile;
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = pgCfg.superUser;
        Group = "postgres";
        ExecStartPre = mkHookCommands "pre_start" cfg.hooks.preStart;
        ExecStart = lib.getExe cfg.package;
        ExecStartPost = mkHookCommands "on_success" cfg.hooks.onSuccess;
        ExecStopPost = mkHookCommands "post_run" cfg.hooks.postRun;
      };
    }
    // lib.optionalAttrs (cfg.hooks.onFailure != [ ]) {
      unitConfig.OnFailure = [ "postgresql-collation-guard-on-failure.service" ];
    };

    # Separate companion unit for the onFailure hook, triggered via the
    # main unit's OnFailure= above -- this fires on ANY failure mode
    # (non-zero exit, crash, kill, timeout), not just a clean non-zero
    # exit a plain ExecStopPost could also observe, which is the one
    # thing a plain post-run hook can't guarantee.
    systemd.services."postgresql-collation-guard-on-failure" = lib.mkIf (cfg.hooks.onFailure != [ ]) {
      description = "On-failure hooks for postgresql-collation-guard.service";
      environment.COLLATION_GUARD_CONTEXT_FILE = contextFile;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = mkHookCommands "on_failure" cfg.hooks.onFailure;
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
