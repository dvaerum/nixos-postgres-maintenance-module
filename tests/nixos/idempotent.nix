# Cycle 2: a second run against an already-stamped cluster is a clean
# no-op. Deliberately restarts just the guard unit, not the whole
# machine -- avoids `machine.shutdown()`/reboot, which pays a real,
# measurable unmount cost per bind-mounted store path (nixpkgs' own
# `grafana` test was patched 2026-09-20 specifically to stop doing
# that).
{ nixosModule }:
{
  name = "collation-guard-idempotent";

  containers.machine =
    { ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresqlCollationGuard.enable = true;
    };

  testScript = ''
    start_all()
    machine.wait_for_unit("postgresql.target")

    stamp_before = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = 'postgres';" """
    ).strip()

    machine.systemctl("restart postgresql-collation-guard.service")
    machine.wait_for_unit("postgresql-collation-guard.service")

    stamp_after = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = 'postgres';" """
    ).strip()
    assert stamp_after == stamp_before, f"stamp changed on a no-op run: {stamp_before!r} -> {stamp_after!r}"

    # No "reindexing" log line the second time -- the stamp already
    # matched, so the C.UTF-8 path should have been a pure no-op, not
    # just happened to leave the same final value.
    journal = machine.succeed(
        "journalctl -u postgresql-collation-guard.service --no-pager -o cat"
    )
    assert journal.count("reindexing C.UTF-8 databases") == 1, (
        f"expected exactly one 'reindexing C.UTF-8 databases' log line (from the first run only), "
        f"got {journal.count('reindexing C.UTF-8 databases')}:\n{journal}"
    )
  '';
}
