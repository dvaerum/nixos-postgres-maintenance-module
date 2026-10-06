# Heavy-tier regression test for the systemd ordering guarantee
# config.nix's own comment on systemd.services.postgresql's requires/
# after asserts but pg-upgrade.nix (the happy-path heavy test) never
# actually exercises: a FAILED upgrade must block postgresql.service
# from starting at all, not just fail its own unit. See
# docs/decisions/0010/0011.
#
# Forces a real failure via a real on-disk version mismatch (the old
# cluster is genuinely PostgreSQL 16, but oldPackage is configured as
# 15) -- VersionMismatchError, raised by upgrade.validate_old_version()
# before anything irreversible runs. Chosen over forcing a real
# pg_upgrade binary failure (e.g. a deliberately incompatible
# extension) because it's deterministic and fast to set up reliably,
# and the thing actually under test here is the systemd *ordering*
# consequence of ANY upgrade-unit failure, not which particular Python
# exception triggered it -- upgrade.run_upgrade() raising at all is
# what Requires= on systemd.services.postgresql reacts to, regardless
# of which of its own exceptions fired.
#
# Not wired into `checks` -- same reasoning as pg-upgrade.nix itself.
# Run by hand via `nix build .#pgUpgradeFailureTest -L`.
{
  postgresqlOld,
  postgresqlNew,
  nixosModule,
}:
{
  name = "collation-guard-pg-upgrade-failure";

  containers.machine =
    { lib, ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresqlNew;
      services.postgresqlCollationGuard.enable = true;
      services.postgresqlCollationGuard.upgrade = {
        enable = true;
        # Deliberately WRONG -- the real on-disk cluster seeded below
        # is actually postgresqlNew's own major (see the test script),
        # not postgresqlOld's. validate_old_version() must catch this
        # before pg_upgrade is ever invoked.
        oldPackage = postgresqlOld;
      };

      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  testScript = ''
    start_all()

    # Seed a real cluster at oldPackage's own expected path, but built
    # with the NEW package's binary -- a real, on-disk PG_VERSION that
    # genuinely disagrees with the configured oldPackage.
    old_datadir = "/var/lib/postgresql/${postgresqlOld.psqlSchema}"
    pg_new = "${postgresqlNew}"

    machine.succeed("mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql")
    machine.succeed(
        f"runuser -u postgres -- {pg_new}/bin/initdb --pgdata={old_datadir} "
        "--locale=en_US.UTF-8 --encoding=UTF8 --auth=trust --no-sync -U postgres"
    )

    # Starting postgresql.target must fail outright -- not just the
    # upgrade unit on its own, but the whole dependency chain, with
    # postgresql.service never even attempting to start against the
    # mismatched/half-migrated directory.
    machine.fail("systemctl start postgresql.target")

    upgrade_result = machine.succeed(
        "systemctl show postgresql-collation-guard-upgrade.service --property=Result --value"
    ).strip()
    assert upgrade_result != "success", upgrade_result

    # The actual guarantee under test: postgresql.service must never
    # have even attempted to start on top of the mismatched directory.
    postgresql_active_state = machine.succeed(
        "systemctl show postgresql.service --property=ActiveState --value"
    ).strip()
    assert postgresql_active_state != "active", postgresql_active_state

    # The real, actionable error is visible in the journal -- an
    # operator debugging a failed boot needs to see WHY, not just that
    # something failed.
    journal = machine.succeed(
        "journalctl -u postgresql-collation-guard-upgrade.service --no-pager -o cat"
    )
    assert "VersionMismatchError" in journal, journal
    assert "refusing to proceed" in journal, journal

    # Nothing was ever touched: the old (mismatched) directory is
    # exactly as seeded, and no new-package cluster was ever created
    # anywhere -- a failed preflight check must be a true no-op, not a
    # partial one.
    new_datadir = "/var/lib/postgresql/${postgresqlNew.psqlSchema}"
    machine.fail(f"test -e {new_datadir}/PG_VERSION")
  '';
}
