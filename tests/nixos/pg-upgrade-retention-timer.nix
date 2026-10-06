# Heavy-tier coverage for the positive-retention cleanup path
# (docs/decisions/0011) -- cleanup_old_datadir_if_due() and
# --upgrade-cleanup are unit-tested with mocked/explicit timestamps,
# and the systemd.timers/.services pair in nixosModule/config.nix is
# wired, but neither had ever been exercised through the REAL service
# against a REAL completed-upgrade state file before this test.
#
# Doesn't wait a literal day for the timer to fire (OnUnitActiveSec =
# "1d") or fake the whole VM's system clock (which could have side
# effects elsewhere, e.g. TLS validation) -- instead backdates the
# real completion-state file's own completed_at field directly
# (simulating "a day already passed"), then triggers the cleanup
# SERVICE the timer would eventually start, proving the real systemd
# unit + real Python cleanup_old_datadir_if_due() codepath end to end.
#
# Not wired into `checks` -- same reasoning as pg-upgrade.nix. Run by
# hand via `nix build .#pgUpgradeRetentionTimerTest -L`.
{
  postgresqlOld,
  postgresqlNew,
  nixosModule,
}:
{
  name = "collation-guard-pg-upgrade-retention-timer";

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
        oldDataDirRetentionDays = 1;
      };
      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  testScript = ''
    start_all()

    old_datadir = "/var/lib/postgresql/${postgresqlOld.psqlSchema}"
    pg_old = "${postgresqlOld}"
    state_file = "/var/lib/postgresql-collation-guard/upgrade-completed.json"

    machine.succeed("mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql")
    machine.succeed(
        f"runuser -u postgres -- {pg_old}/bin/initdb --pgdata={old_datadir} "
        "--locale=en_US.UTF-8 --encoding=UTF8 --auth=trust --no-sync -U postgres"
    )
    machine.succeed("systemctl start postgresql.target")
    machine.wait_for_unit("postgresql.target")

    upgrade_result = machine.succeed(
        "systemctl show postgresql-collation-guard-upgrade.service --property=Result --value"
    ).strip()
    assert upgrade_result == "success", upgrade_result

    # The timer exists and is wired, since oldDataDirRetentionDays is a
    # positive number -- confirmed directly against the real unit, not
    # just the eval-level config tree.
    machine.succeed("systemctl list-timers postgresql-collation-guard-upgrade-cleanup.timer")

    # Not due yet: a real migration that just completed moments ago,
    # against a 1-day retention window, must NOT be cleaned up by a
    # manual trigger of the same service the timer would eventually run.
    machine.succeed("systemctl start postgresql-collation-guard-upgrade-cleanup.service")
    machine.succeed(f"test -e {old_datadir}/PG_VERSION")

    # Backdate the real completion stamp to simulate the retention
    # window having elapsed -- same old_datadir, same started_at, only
    # completed_at moved into the past.
    backdated = "2020-01-01T00:00:00+00:00"
    machine.succeed(
        "runuser -u postgres -- bash -c '"
        f"cat > {state_file} <<EOF\n"
        f'{{"old_datadir": "{old_datadir}", "started_at": "{backdated}", '
        f'"completed_at": "{backdated}"}}\n'
        "EOF'"
    )

    machine.succeed("systemctl start postgresql-collation-guard-upgrade-cleanup.service")
    cleanup_result = machine.succeed(
        "systemctl show postgresql-collation-guard-upgrade-cleanup.service --property=Result --value"
    ).strip()
    assert cleanup_result == "success", cleanup_result

    # The real, only-after-backdating removal -- the actual property
    # under test.
    machine.fail(f"test -e {old_datadir}")

    # The new cluster (what actually matters) is completely unaffected
    # by the old directory's removal.
    machine.succeed("systemctl is-active postgresql.service")
  '';
}
