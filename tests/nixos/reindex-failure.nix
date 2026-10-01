# Cycle 4 (systemd half): a REINDEX failure must actually propagate
# through postgresql-setup.service and block postgresql.target --
# not just fail the guard's own unit in isolation. Real reproduction of
# the same technique already rehearsed live against the real production
# cluster and proven at the fast tier (test_collation.py): toggle
# pg_index.indisready off, insert a duplicate that bypasses the unique
# index, toggle it back on -- REINDEX then finds the duplicate and
# fails.
#
# Corruption has to be injected into an already-running cluster (there's
# no data to corrupt before Postgres has started once), so this stops
# the whole chain and brings postgresql.service back up standalone to
# inject it -- cheaper than a full container reboot (see idempotent.nix's
# note on avoiding machine.shutdown()) while still exercising the real
# systemd dependency chain from a clean stopped state. Confirmed
# empirically that stopping just the three units above postgresql.service
# isn't enough: postgresql.service gets stopped right along with them
# (no StopWhenUnneeded= isolation here), so it has to be stopped and
# restarted explicitly too, not assumed to stay up.
{ nixosModule }:
{
  name = "collation-guard-reindex-failure";

  containers.machine =
    { ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresqlCollationGuard.enable = true;

      # A stand-in for a real consumer (e.g. an application service) --
      # proves the block actually prevents something downstream from
      # starting, not just that the guard's own unit shows "failed".
      systemd.services.stub-consumer = {
        description = "Stand-in for a service that depends on postgresql.target";
        requires = [ "postgresql.target" ];
        after = [ "postgresql.target" ];
        serviceConfig.Type = "oneshot";
        script = "true";
      };
    };

  testScript = ''
    start_all()
    machine.wait_for_unit("postgresql.target")

    machine.systemctl(
        "stop postgresql.target postgresql-setup.service "
        "postgresql-collation-guard.service postgresql.service"
    )
    machine.systemctl("start postgresql.service")
    machine.wait_for_unit("postgresql.service")

    # Inject a duplicate that bypasses the unique index, then fake a
    # Postgres-tracked collation mismatch so the guard actually attempts
    # a REINDEX on its next run. Each statement is its own psql
    # invocation, double-quoted at the bash level so the SQL's own
    # single-quoted string literals don't need escaping.
    for statement in [
        "CREATE TABLE widgets (id serial PRIMARY KEY, name text UNIQUE);",
        "INSERT INTO widgets (name) VALUES ('alpha');",
        "UPDATE pg_index SET indisready = false WHERE indexrelid = 'widgets_name_key'::regclass;",
        "INSERT INTO widgets (name) VALUES ('alpha');",
        "UPDATE pg_index SET indisready = true WHERE indexrelid = 'widgets_name_key'::regclass;",
        "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
        "WHERE datname = current_database();",
    ]:
        machine.succeed(f"""runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "{statement}" """)

    # Starting the top of the chain cascades through Requires= down to
    # postgresql.service automatically -- no need to start it by hand.
    # Expected to fail: capture via execute(), not succeed().
    _, _ = machine.execute("systemctl start postgresql.target")

    guard_state = machine.succeed(
        "systemctl is-active postgresql-collation-guard.service || true"
    ).strip()
    assert guard_state == "failed", f"expected the guard unit to be 'failed', got {guard_state!r}"

    setup_state = machine.succeed("systemctl is-active postgresql-setup.service || true").strip()
    assert setup_state != "active", f"postgresql-setup.service should be blocked, got {setup_state!r}"

    target_state = machine.succeed("systemctl is-active postgresql.target || true").strip()
    assert target_state != "active", f"postgresql.target should be blocked, got {target_state!r}"

    # And the real point: a downstream consumer must not start either.
    _, _ = machine.execute("systemctl start stub-consumer.service")
    stub_state = machine.succeed("systemctl is-active stub-consumer.service || true").strip()
    assert stub_state != "active", f"stub-consumer.service should be blocked too, got {stub_state!r}"
  '';
}
