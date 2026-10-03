# hooks.onFailure (whole-run), driven through a real failure -- the
# same REINDEX-failure corruption technique as reindex-failure.nix --
# to prove it fires via the postgresql-collation-guard-on-failure.service
# companion unit (systemd's OnFailure=, a separate process), not just
# that it evaluates.
{ nixosModule }:
let
  examples = import ../../examples;
in
{
  name = "collation-guard-on-failure-hook";

  containers.machine =
    { ... }:
    {
      imports = [
        nixosModule.nixosModules.default
        examples.onFailureHook
      ];
      services.postgresql.enable = true;
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

    # The companion unit fires asynchronously via OnFailure=, from a
    # separate process -- wait for its marker rather than the unit's
    # own (non-RemainAfterExit) active state, which clears the instant
    # its oneshot ExecStart finishes.
    machine.wait_for_file("/run/postgresql-collation-guard/whole-run-onfailure-marker")

    marker = machine.succeed("cat /run/postgresql-collation-guard/whole-run-onfailure-marker")
    assert "ERROR=postgres.widgets: REINDEX failed" in marker, marker
    assert '"success": false' in marker, marker
    assert '"widgets"' in marker, marker
  '';
}
