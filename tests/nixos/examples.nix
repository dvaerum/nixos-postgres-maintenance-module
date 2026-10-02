# Proves the examples/ directory is more than prose: every example
# module is imported into one real container config here, so a
# rename/removal of an option any of them uses fails `nix flake check`
# immediately (the eval-level floor), and the perDatabase.onFailure
# example additionally gets driven through a real database_failure (the
# same indisready/duplicate-key corruption technique as
# reindex-failure.nix) to prove its marker file actually receives the
# env vars the hook point promises -- not just that it evaluates.
{ nixosModule }:
let
  examples = import ../../examples;
in
{
  name = "collation-guard-examples";

  containers.machine =
    { ... }:
    {
      imports = [
        nixosModule.nixosModules.default
        examples.minimal
        examples.tuning
        examples.connectionLockdownDisabled
        examples.perDatabaseOnFailureHook
      ];
      services.postgresql.enable = true;
    };

  testScript = ''
    start_all()
    machine.wait_for_unit("postgresql.target")

    # tuning.nix / connection-lockdown-disabled.nix: prove the values
    # actually reached the real systemd unit's environment, not just
    # that the module evaluated.
    unit_environment = machine.succeed(
        "systemctl show postgresql-collation-guard.service --property=Environment --value"
    )
    assert "COLLATION_GUARD_MAX_PARALLEL_DATABASES=8" in unit_environment, unit_environment
    assert "COLLATION_GUARD_MAX_REPAIR_ATTEMPTS=5000" in unit_environment, unit_environment
    assert "COLLATION_GUARD_CONNECTION_LOCKDOWN_ENABLE=false" in unit_environment, unit_environment

    # per-database-onfailure-hook.nix: drive a real database_failure,
    # same corruption technique as reindex-failure.nix (toggle
    # indisready off, insert a duplicate that bypasses the unique
    # index, toggle it back on, fake a Postgres-tracked collation
    # mismatch so the guard attempts a REINDEX on its next run).
    machine.systemctl(
        "stop postgresql.target postgresql-setup.service "
        "postgresql-collation-guard.service postgresql.service"
    )
    machine.systemctl("start postgresql.service")
    machine.wait_for_unit("postgresql.service")

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

    # Expected to fail -- capture via execute(), not succeed().
    _, _ = machine.execute("systemctl start postgresql.target")

    guard_state = machine.succeed(
        "systemctl is-active postgresql-collation-guard.service || true"
    ).strip()
    assert guard_state == "failed", f"expected the guard unit to be 'failed', got {guard_state!r}"

    marker = machine.succeed("cat /tmp/collation-guard-onfailure-marker")
    assert "DATABASE=postgres" in marker, marker
    assert "ERROR=widgets: REINDEX failed" in marker, marker
    assert "CONTEXT=" in marker and '"failures"' in marker and '"widgets"' in marker, marker
  '';
}
