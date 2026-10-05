# Heaviest tier for the pg_upgrade feature (docs/decisions/0010, 0011):
# a real PostgreSQL major-version migration across two genuinely
# different binaries, proving the systemd wiring in nixosModule/config.nix
# actually drives a real initdb + pg_upgrade invocation in the right
# order -- not just the Python orchestration logic, already proven
# against mocked subprocess calls in tests/test_upgrade.py.
#
# Not wired into `checks` -- same reasoning as docs/decisions/0008's
# icuDriftTest: a real multi-minute two-cluster migration is not a
# logic-level check, even though (unlike icu-drift's custom ICU
# override) both old/new packages here are ordinary, already-cached
# nixpkgs postgresql_NN derivations. Run by hand via
# `nix build .#pgUpgradeTest -L`.
#
# Mechanism: nothing in the postgresql/postgresql-setup/collation-guard/
# collation-guard-upgrade chain is allowed to auto-start at container
# boot (systemd.targets.postgresql.wantedBy masked below) -- the OLD
# cluster has to be seeded manually first, at the exact path this
# module's own oldDataDir default expects to find it, or
# run_upgrade()'s own idempotency gate (the NEW data directory already
# having a PG_VERSION) would make the real upgrade a permanent no-op
# before it ever got a chance to run for real. Once seeded, starting
# postgresql.target for the first time pulls in the REAL chain in the
# REAL order: postgresql-collation-guard-upgrade.service (this
# project's new unit) -> postgresql.service (the now-migrated NEW
# cluster) -> postgresql-collation-guard.service (the existing guard)
# -> postgresql-setup.service.
{
  postgresqlOld,
  postgresqlNew,
  nixosModule,
}:
{
  name = "collation-guard-pg-upgrade";

  containers.machine =
    { lib, ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresqlNew;
      services.postgresqlCollationGuard.enable = true;
      services.postgresqlCollationGuard.upgrade = {
        enable = true;
        oldPackage = postgresqlOld;
      };

      # Nothing in the chain above auto-starts -- see this file's own
      # top comment for why.
      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  testScript = ''
    start_all()

    old_datadir = "/var/lib/postgresql/${postgresqlOld.psqlSchema}"
    pg_old = "${postgresqlOld}"

    # --- Seed a real OLD cluster at exactly the path this module's own
    # oldDataDir default expects -- same manual initdb/pg_ctl pattern as
    # icu-drift.nix's own phase 1, just across two genuinely different
    # major versions instead of two ICU-only overrides of the same one.
    machine.succeed("mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql")
    machine.succeed(
        f"runuser -u postgres -- {pg_old}/bin/initdb --pgdata={old_datadir} "
        "--locale=en_US.UTF-8 --encoding=UTF8 --auth=trust --no-sync -U postgres"
    )
    machine.succeed(
        f"runuser -u postgres -- {pg_old}/bin/pg_ctl -D {old_datadir} "
        f"-l {old_datadir}.log -o '-c unix_socket_directories=/tmp' start"
    )
    machine.succeed(
        f"""runuser -u postgres -- {pg_old}/bin/psql -h /tmp -d postgres -U postgres """
        """-v ON_ERROR_STOP=1 -c "CREATE TABLE upgrade_probe (id int, note text);" """
        """-c "INSERT INTO upgrade_probe VALUES (1, 'migrated-by-pg-upgrade');" """
    )
    machine.succeed(f"runuser -u postgres -- {pg_old}/bin/pg_ctl -D {old_datadir} -m fast stop")

    # --- Trigger the REAL chain -- proving the systemd ordering itself
    # (docs/decisions/0011), not just the Python logic in isolation.
    machine.succeed("systemctl start postgresql.target")
    machine.wait_for_unit("postgresql.target")

    # The upgrade unit actually ran a real migration, not a skipped
    # idempotent no-op.
    upgrade_result = machine.succeed(
        "systemctl show postgresql-collation-guard-upgrade.service --property=Result --value"
    ).strip()
    assert upgrade_result == "success", upgrade_result

    # The migrated data is really there, under the NEW package's own
    # cluster -- queried through the normal services.postgresql socket,
    # not the manual one used to seed the old cluster above.
    row = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT note FROM upgrade_probe WHERE id = 1;" """
    ).strip()
    assert row == "migrated-by-pg-upgrade", row

    # The old data directory is kept by default (oldDataDirRetentionDays
    # left unset above -- docs/decisions/0011).
    machine.succeed(f"test -e {old_datadir}/PG_VERSION")

    # A completion stamp was recorded for the retention-timer machinery
    # (tested directly, with mocked time, in tests/test_upgrade.py) to
    # depend on later.
    machine.succeed("test -e /var/lib/postgresql-collation-guard/upgrade-completed.json")

    # The main collation guard and postgresql-setup both still reached
    # the migrated cluster afterward -- the rest of the existing chain
    # (docs/decisions/0007) is unaffected by the new unit ahead of it.
    machine.succeed("systemctl is-active postgresql-collation-guard.service")
    machine.succeed("systemctl is-active postgresql-setup.service")
  '';
}
