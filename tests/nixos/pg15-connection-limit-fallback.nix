# The direct regression test for the production incident that drove
# docs/decisions/0009: postgresqlCollationGuard used to inject
# `include_if_exists <lockdownFile>` into pg_hba.conf unconditionally,
# regardless of the configured PostgreSQL version. include_if_exists
# was only added in PostgreSQL 16
# (https://www.postgresql.org/docs/16/release-16.html#RELEASE-16-PG-HBA)
# -- on PostgreSQL 15 (and older) every startup hit FATAL: invalid
# connection type "include_if_exists" and crash-looped forever, since
# the same broken line was regenerated on every boot. Fixed by
# selecting a different connectionLockdown mechanism (ALTER DATABASE
# ... CONNECTION LIMIT, docs/decisions/0009) based on the configured
# services.postgresql.package, not a flat unconditional pg_hba.conf
# line.
#
# This test pins services.postgresql.package = pkgs.postgresql_15 --
# the exact version from the incident report -- with
# connectionLockdown left at its default (true, unchanged). If this
# regresses, postgresql.target never reaches active at all (same as
# every other cycle-0 test in this suite), which is the one failure
# mode that actually matters here.
#
# Does NOT attempt to simulate a genuine machine-level crash (power
# loss) here -- see docs/learnings/nspawn-crash-simulation.md for why
# that was tried and confirmed not to work with this project's own
# nspawn container test backend (ADR 0005). The self-heal logic that
# guarantee depends on (_cleanup_stale_lockdown_state(), called at the
# top of every run()) is instead proven directly against a real
# ephemeral Postgres cluster in tests/test_main.py
# (test_run_restores_a_stale_connection_limit_state_before_doing_new_work).
{ nixosModule, postgresql15 }:
{
  name = "collation-guard-pg15-connection-limit-fallback";

  containers.machine =
    { ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresql15;
      services.postgresqlCollationGuard.enable = true;
    };

  testScript = ''
    start_all()

    # The regression itself: this used to crash-loop forever on
    # PostgreSQL 15 and postgresql.target would never reach active.
    machine.wait_for_unit("postgresql.target")
    machine.wait_for_unit("postgresql-collation-guard.service")

    # Mechanism selection actually reached the real systemd unit, not
    # just the module's own eval-level config tree (see
    # tests/nixos/examples.nix for the same kind of check).
    unit_environment = machine.succeed(
        "systemctl show postgresql-collation-guard.service --property=Environment --value"
    )
    assert "COLLATION_GUARD_LOCKDOWN_MECHANISM=connection_limit" in unit_environment, unit_environment
    # The crash-survival guarantee docs/decisions/0009 depends on
    # requires this file to live outside /run (tmpfs, wiped on every
    # reboot) -- see nixosModule/config.nix's own comment on
    # connectionLimitStateFile for why. Checked directly against the
    # real unit, not assumed from the Nix config tree.
    assert "COLLATION_GUARD_CONNECTION_LIMIT_STATE_FILE=/var/lib/" in unit_environment, (
        unit_environment
    )

    # The actual generated pg_hba.conf (not just the module's config
    # tree) must never carry the PostgreSQL-16-only directive on this
    # version -- queried through Postgres itself (hba_file), the
    # authoritative source, not assumed from the Nix side.
    hba_file = machine.succeed(
        """runuser -u postgres -- psql -tAc "SHOW hba_file;" """
    ).strip()
    hba_contents = machine.succeed(f"cat {hba_file}")
    assert "include_if_exists" not in hba_contents, hba_contents
  '';
}
