# Heaviest tier: a real, reproducible drifted-ICU cluster, not just a
# simulated collversion mismatch -- see docs/decisions/0008 for the
# full "why" and docs/learnings/partition-repair-testing.md for the
# history of what this closes (README's former "No real-drift test
# coverage for row-level partition repair" limitation).
#
# Mechanism, empirically validated against real ICU 72/73 builds
# before being encoded here (docs/decisions/0008): two distinct
# Postgres binaries, each linked against a different ICU release
# (postgresqlIcu72/73 in flake.nix) build/populate a partitioned table
# under one binary, then the *exact same* on-disk $PGDATA is started
# under the *other* binary -- the only way to get a genuinely different
# ICU comparison routine loaded against identical on-disk data.
# Patching the shared library in place instead of swapping the whole
# binary does not work: ICU bakes its release number into every
# exported symbol name (confirmed empirically: patchelf-swapping
# icu73's .so under an icu72-linked postgres binary fails with
# "undefined symbol: uloc_countAvailable_72").
#
# The string pair and locale (U+2018 vs U+201A, a custom ICU "de"-
# locale collation) reproduce ICU-22544: ICU 72->73 changed root/
# language-collation sort order for hundreds of locales *without*
# bumping collversion for many of them -- exactly this project's own
# documented blind spot (README "Known limitations"). Using that exact
# pair means this test closes the row-repair gap by way of the
# hardest case: a real misplaced row that Postgres's own
# collversion-based drift detection cannot see at all, so the only
# reason partitions.py's misplaced_rows() still catches it is that it
# re-checks the row against a live comparison
# (pg_get_partition_constraintdef), never the stored version number.
# U&'...' Unicode string escapes keep the SQL here plain ASCII --
# U&'A\2018B' is 'A' + U+2018 + 'B', U&'A\201A2' is 'A' + U+201A + '2'.
#
# Not wired into `checks` (see docs/decisions/0008 for why): exposed
# as its own `packages.icuDriftTest` output instead, run by hand via
# `nix build .#icuDriftTest -L`.
{
  postgresqlIcu72,
  postgresqlIcu73,
  collationGuardPackage,
}:
{
  name = "collation-guard-icu-drift";

  containers.machine =
    { ... }:
    {
      # Deliberately not services.postgresql.enable -- this test never
      # goes through the module's own managed cluster or systemd unit,
      # only the `postgres` OS user initdb/pg_ctl need (non-root, same
      # as every other postgres invocation in this project's tests).
      users.groups.postgres = { };
      users.users.postgres = {
        isSystemUser = true;
        group = "postgres";
        home = "/var/empty";
      };
    };

  testScript = ''
    start_all()

    pgdata = "/tmp/icu-drift-pgdata"
    sockdir = "/tmp/icu-drift-sock"
    port = "5490"
    pg72 = "${postgresqlIcu72}"
    pg73 = "${postgresqlIcu73}"
    guard = "${collationGuardPackage}"

    machine.succeed(f"mkdir -p {sockdir} && chown postgres:postgres {sockdir}")
    machine.succeed(
        f"runuser -u postgres -- {pg72}/bin/initdb --pgdata={pgdata} "
        "--locale=en_US.UTF-8 --encoding=UTF8 --auth=trust --no-sync -U postgres"
    )

    def pg_start(pg: str) -> None:
        machine.succeed(
            f"runuser -u postgres -- {pg}/bin/pg_ctl -D {pgdata} -l {pgdata}.log "
            f"-o '-p {port} -c unix_socket_directories={sockdir} -c listen_addresses=' start"
        )

    def pg_stop(pg: str) -> None:
        machine.succeed(f"runuser -u postgres -- {pg}/bin/pg_ctl -D {pgdata} -m immediate stop")

    def psql(pg: str, statement: str) -> str:
        # Double-quoted at the bash level so the SQL's own single-quoted
        # string literals don't need escaping -- same convention as
        # reindex-failure.nix.
        return machine.succeed(
            f"""runuser -u postgres -- {pg}/bin/psql -h {sockdir} -p {port} -d postgres """
            f"""-U postgres -v ON_ERROR_STOP=1 -tAc "{statement}" """
        )

    # --- Phase 1: build the partitioned table under ICU 72 -----------------
    pg_start(pg72)

    psql(pg72, "CREATE COLLATION drift_de (provider = icu, locale = 'de');")
    psql(pg72, "CREATE TABLE events (k text COLLATE drift_de, id int) PARTITION BY RANGE (k);")
    psql(
        pg72,
        "CREATE TABLE events_p1 PARTITION OF events "
        "FOR VALUES FROM (MINVALUE) TO (U&'A\\201A2');",
    )
    psql(
        pg72,
        "CREATE TABLE events_p2 PARTITION OF events FOR VALUES FROM (U&'A\\201A2') TO (MAXVALUE);",
    )
    psql(pg72, "INSERT INTO events (k, id) VALUES (U&'A\\2018B', 1);")

    placement = psql(pg72, "SELECT tableoid::regclass FROM events WHERE id = 1;").strip()
    assert placement == "events_p1", f"expected events_p1 under icu72, got {placement!r}"

    pg_stop(pg72)

    # --- Phase 2: swap to ICU 73 against the SAME on-disk data -------------
    pg_start(pg73)

    collversion = psql(
        pg73, "SELECT collversion FROM pg_collation WHERE collname = 'drift_de';"
    ).strip()
    assert collversion == "153.120", (
        "expected drift_de's collversion to be UNCHANGED between icu72 and icu73 "
        f"(ICU-22544's blind spot -- the whole point of this test), got {collversion!r}"
    )

    misplaced_before = psql(
        pg73,
        "SELECT count(*) FROM events_p1 WHERE NOT (k < U&'A\\201A2' COLLATE drift_de);",
    ).strip()
    assert misplaced_before == "1", (
        "expected the row to be genuinely misplaced under icu73's live comparison "
        f"(collversion never changed), got count={misplaced_before!r}"
    )

    # --- Phase 3: run the REAL collation-guard CLI end-to-end --------------
    repair_output = machine.succeed(
        f"runuser -u postgres -- env PGHOST={sockdir} PGPORT={port} "
        "GLIBC_LOCALES_PATH=unused-in-icu-drift-test "
        "COLLATION_GUARD_PARTITION_REPAIR_ENABLE=true COLLATION_GUARD_MAX_REPAIR_ATTEMPTS=10 "
        f"{guard}/bin/collation-guard 2>&1"
    )
    assert "partition repair: postgres.public.events -- moved 1 row(s)" in repair_output, (
        f"expected the guard's own log to report the repair, got:\n{repair_output}"
    )

    placement_after = psql(pg73, "SELECT tableoid::regclass FROM events WHERE id = 1;").strip()
    assert (
        placement_after == "events_p2"
    ), f"expected the row moved to events_p2 after repair, got {placement_after!r}"

    misplaced_after = psql(
        pg73,
        "SELECT count(*) FROM events_p1 WHERE NOT (k < U&'A\\201A2' COLLATE drift_de) "
        "UNION ALL "
        "SELECT count(*) FROM events_p2 WHERE NOT (k >= U&'A\\201A2' COLLATE drift_de);",
    )
    for line in misplaced_after.strip().splitlines():
        assert (
            line.strip() == "0"
        ), f"expected zero misplaced rows left in either partition, got:\n{misplaced_after}"

    total = psql(pg73, "SELECT count(*) FROM events;").strip()
    assert total == "1", f"expected no data loss across the repair, got count={total!r}"

    pg_stop(pg73)
  '';
}
