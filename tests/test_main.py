"""Fast tier: main.run() orchestration end-to-end against the real
ephemeral cluster -- collation.py + partitions.py wired together, same
as the real systemd ExecStart does, just without systemd itself.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import psycopg
import pytest

from collation_guard import collation, main, partitions
from collation_guard.hooks import Hook, HooksConfig, PerDatabaseHooks


def _host_port(pg_dsn: str) -> tuple[str, str]:
    parts = dict(item.split("=", 1) for item in pg_dsn.split())
    return parts["host"], parts["port"]


@pytest.fixture
def failing_database(admin_conn: psycopg.Connection, pg_dsn: str) -> Iterator[str]:
    """A real database with a genuine REINDEX failure -- the same
    indisready duplicate-key reproduction used throughout this project
    (see test_collation.py's cycle 4)."""
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_failing_database"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text UNIQUE)")
            conn.execute("INSERT INTO widgets (name) VALUES ('alpha')")
            conn.commit()
            conn.execute(
                "UPDATE pg_index SET indisready = false "
                "WHERE indexrelid = 'widgets_name_key'::regclass"
            )
            conn.commit()
            conn.execute("INSERT INTO widgets (name) VALUES ('alpha')")
            conn.commit()
            conn.execute(
                "UPDATE pg_index SET indisready = true "
                "WHERE indexrelid = 'widgets_name_key'::regclass"
            )
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()
        yield name
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_connect_tags_the_connection_with_application_name(pg_dsn: str) -> None:
    """Every connection the guard opens is tagged -- this is what lets
    the lockdown termination sweep tell "another guard connection to
    the same database" apart from "someone else's connection," instead
    of relying on a single PID (see docs/decisions/0007)."""
    host, port = _host_port(pg_dsn)
    with main._connect(host, port, "postgres") as conn:
        row = conn.execute("SELECT current_setting('application_name')").fetchone()
    assert row == ("collation-guard",)


def test_run_is_a_clean_success_on_an_unremarkable_cluster(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
    )

    assert report.success
    assert "postgres" in report.databases_processed
    assert report.failures == []


def test_run_is_idempotent_once_the_glibc_stamp_is_set(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    kwargs = dict(
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
    )

    first = main.run(host, port, **kwargs)
    second = main.run(host, port, **kwargs)

    assert first.success
    assert second.success
    assert second.databases_repaired == []


def test_run_fixes_a_genuine_postgres_tracked_mismatch(
    pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_mismatch"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, label text)")
            conn.execute("INSERT INTO widgets (label) VALUES ('a'), ('b')")
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()

        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-locales",
            partition_repair_enabled=True,
            max_repair_attempts=10,
        )

        assert report.success
        assert name in report.databases_repaired
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_records_a_database_repaired_only_once_across_collation_and_partition_fixes(
    pg_dsn: str, admin_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database can be repaired twice in one run -- once for a stale
    named collation, once for a partition fix -- but
    report.databases_repaired must record it only once (see
    _mark_repaired in main.py)."""
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_double_repair"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE COLLATION cg_double_repair_collation (locale = 'en_US.UTF-8')")
            conn.execute("CREATE TABLE parent (id serial, k text) PARTITION BY RANGE (k)")
            conn.execute(
                "CREATE TABLE child_a PARTITION OF parent FOR VALUES FROM (MINVALUE) TO ('m')"
            )
            conn.execute(
                "CREATE TABLE child_b PARTITION OF parent FOR VALUES FROM ('m') TO (MAXVALUE)"
            )
            conn.execute("INSERT INTO parent (k) VALUES ('apple')")
            conn.commit()
            conn.execute(
                "UPDATE pg_collation SET collversion = 'not-the-real-version' "
                "WHERE collname = 'cg_double_repair_collation'"
            )
            conn.commit()

        # Simulate one misplaced row being found and repaired -- a
        # *genuinely* misplaced row can't be constructed in this
        # single-locale fast tier (same constraint documented in
        # tests/test_partitions.py above partitioned_db); the real
        # multi-build drift is covered by tests/nixos/icu-drift.nix.
        call_state = {"done": False}
        real_misplaced_rows = partitions.misplaced_rows

        def fake_misplaced_rows(conn: psycopg.Connection, schema: str, child: str) -> list[object]:
            if not call_state["done"] and child == "child_a":
                call_state["done"] = True
                (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()
                return [ctid]
            return real_misplaced_rows(conn, schema, child)

        monkeypatch.setattr(partitions, "misplaced_rows", fake_misplaced_rows)

        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-locales",
            partition_repair_enabled=True,
            max_repair_attempts=10,
        )

        assert report.success
        assert report.databases_repaired.count(name) == 1
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_dry_run_lists_partition_candidates_without_repairing(
    pg_dsn: str, admin_conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_dry_run"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE events (id serial, k text) PARTITION BY RANGE (k)")
            conn.commit()

        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-locales",
            partition_repair_enabled=True,
            max_repair_attempts=10,
            dry_run=True,
        )

        assert report.databases_processed == []
        out = capsys.readouterr().out
        assert f"{name}.public.events" in out
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_prestart_hook_blocking_failure_aborts_before_any_database(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    failing_hook = Hook(
        path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=True
    )

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(pre_start=[failing_hook]),
    )

    assert not report.success
    assert report.databases_processed == []


def test_run_onsuccess_hook_fires_on_a_clean_run_and_blocking_failure_flips_exit(
    pg_dsn: str,
) -> None:
    host, port = _host_port(pg_dsn)
    failing_hook = Hook(
        path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=True
    )

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(on_success=[failing_hook]),
    )

    # the actual database work was clean -- only the onSuccess hook
    # itself failed, and that alone flips the overall result
    assert "postgres" in report.databases_processed
    assert not report.success


def test_run_onsuccess_hook_does_not_fire_when_a_database_already_failed(
    pg_dsn: str, failing_database: str, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    marker = tmp_path / "onsuccess-fired"
    onsuccess_hook = Hook(path=sys.executable, args=["-c", f"open({str(marker)!r}, 'w')"])

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(on_success=[onsuccess_hook]),
    )

    assert not report.success
    assert not marker.exists(), "onSuccess hook must not fire when a database failed"


def test_run_prestart_hook_non_blocking_failure_still_processes_databases(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    failing_hook = Hook(
        path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=False
    )

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(pre_start=[failing_hook]),
    )

    assert report.success
    assert "postgres" in report.databases_processed


_FAIL_FOR_TEMPLATE1 = (
    "import os, sys; sys.exit(1 if os.environ['COLLATION_GUARD_DATABASE'] == 'template1' else 0)"
)


def test_run_per_database_prestart_blocking_failure_skips_only_that_database(
    pg_dsn: str,
) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", _FAIL_FOR_TEMPLATE1], block_on_failure=True)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(pre_start=[hook])),
    )

    assert not report.success
    assert "template1" not in report.databases_processed
    assert "postgres" in report.databases_processed


def test_run_per_database_prestart_non_blocking_failure_still_processes_it(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", _FAIL_FOR_TEMPLATE1], block_on_failure=False)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(pre_start=[hook])),
    )

    assert report.success
    assert "template1" in report.databases_processed
    assert "postgres" in report.databases_processed


def test_run_per_database_onsuccess_blocking_failure_adds_failure_despite_clean_processing(
    pg_dsn: str,
) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", _FAIL_FOR_TEMPLATE1], block_on_failure=True)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_success=[hook])),
    )

    # template1's own Postgres processing was perfectly clean -- it's
    # still "processed," just not "success" overall, because its
    # onSuccess hook itself failed.
    assert "template1" in report.databases_processed
    assert not report.success
    assert any(f.database == "template1" for f in report.failures)


def test_run_per_database_onsuccess_non_blocking_failure_leaves_database_successful(
    pg_dsn: str,
) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", _FAIL_FOR_TEMPLATE1], block_on_failure=False)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_success=[hook])),
    )

    assert "template1" in report.databases_processed
    assert report.success


def test_run_per_database_onsuccess_hook_crash_does_not_lose_the_already_completed_work(
    pg_dsn: str,
) -> None:
    """A hook that RAISES (not just exits non-zero) -- here, a bad
    path raising FileNotFoundError from subprocess.run -- in the
    on_success stage, after this database's real collation/partition
    processing already completed, must not crash the whole run and
    must not lose that already-completed database from
    databases_processed. Regression test: _run_hooks previously let
    any exception from run_hook() propagate all the way out of
    run()."""
    host, port = _host_port(pg_dsn)
    crashing_hook = Hook(path="/nonexistent/collation-guard-test-hook", block_on_failure=True)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_success=[crashing_hook])),
    )

    assert not report.success
    assert "postgres" in report.databases_processed


def test_run_per_database_onfailure_blocking_failure_adds_a_second_distinct_failure(
    pg_dsn: str, failing_database: str
) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=True)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_failure=[hook])),
    )

    db_failures = [f for f in report.failures if f.database == failing_database]
    # the original REINDEX failure, plus a second, distinct entry for
    # the failed onFailure hook itself -- both visible independently.
    assert len(db_failures) == 2
    assert any(f.relation == "database_failure" for f in db_failures)


def test_run_per_database_onfailure_non_blocking_failure_leaves_just_the_original(
    pg_dsn: str, failing_database: str
) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=False)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_failure=[hook])),
    )

    db_failures = [f for f in report.failures if f.database == failing_database]
    assert len(db_failures) == 1


def test_run_per_database_onfailure_does_not_fire_on_a_successful_database(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=True)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_failure=[hook])),
    )

    assert report.success


def test_run_postrun_hook_blocking_failure_flips_an_otherwise_clean_run(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(
        path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=True
    )

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(post_run=[hook]),
    )

    assert "postgres" in report.databases_processed
    assert not report.success


def test_run_postrun_hook_non_blocking_failure_leaves_clean_run_clean(pg_dsn: str) -> None:
    host, port = _host_port(pg_dsn)
    hook = Hook(
        path=sys.executable, args=["-c", "import sys; sys.exit(1)"], block_on_failure=False
    )

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(post_run=[hook]),
    )

    assert report.success


def test_run_postrun_hook_fires_even_when_a_database_already_failed(
    pg_dsn: str, failing_database: str, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    marker = tmp_path / "postrun-fired"
    hook = Hook(path=sys.executable, args=["-c", f"open({str(marker)!r}, 'w')"])

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(post_run=[hook]),
    )

    assert not report.success
    assert marker.exists(), "postRun must fire regardless of outcome, unlike onSuccess"


def _hooks_file_with_one_on_failure_hook(tmp_path, hook_path: str, *hook_args: str) -> str:
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "onFailure": [
                    {
                        "path": hook_path,
                        "args": list(hook_args),
                        "environment": {},
                        "environmentFile": None,
                        "blockOnFailure": True,
                    }
                ]
            }
        )
    )
    return str(path)


def test_run_on_failure_reads_last_context_and_fires_hooks(tmp_path) -> None:
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(
            {
                "databases_processed": ["postgres"],
                "databases_repaired": [],
                "failures": [
                    {"database": "mydb", "relation": "widgets", "error": "REINDEX failed"}
                ],
                "success": False,
            }
        )
    )
    out = tmp_path / "recorded-error.txt"
    script = (
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['COLLATION_GUARD_ERROR'])"
    )
    hooks_file = _hooks_file_with_one_on_failure_hook(
        tmp_path, sys.executable, "-c", script, str(out)
    )

    exit_code = main.run_on_failure(hooks_file, str(context_file))

    assert exit_code == 0
    assert "mydb.widgets: REINDEX failed" in out.read_text()


def test_run_on_failure_falls_back_to_a_generic_error_when_no_context_file_exists(
    tmp_path,
) -> None:
    """A crash on the very first-ever run, before anything was ever
    written -- must not itself crash."""
    missing_context_file = tmp_path / "does-not-exist.json"
    out = tmp_path / "recorded-error.txt"
    script = (
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['COLLATION_GUARD_ERROR'])"
    )
    hooks_file = _hooks_file_with_one_on_failure_hook(
        tmp_path, sys.executable, "-c", script, str(out)
    )

    exit_code = main.run_on_failure(hooks_file, str(missing_context_file))

    assert exit_code == 0
    assert out.read_text()  # some generic, non-empty message


def test_run_on_failure_blocking_hook_failure_makes_the_process_exit_nonzero(tmp_path) -> None:
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(
            {"databases_processed": [], "databases_repaired": [], "failures": [], "success": False}
        )
    )
    hooks_file = _hooks_file_with_one_on_failure_hook(
        tmp_path, sys.executable, "-c", "import sys; sys.exit(1)"
    )

    exit_code = main.run_on_failure(hooks_file, str(context_file))

    assert exit_code == 1


def _context_dump_hook(out_path, *, block_on_failure: bool = False) -> Hook:
    """A hook that dumps its own COLLATION_GUARD_CONTEXT verbatim --
    proves run_hook() actually receives it, not just that main.py built
    a dict somewhere."""
    script = "import os, sys; open(sys.argv[1], 'w').write(os.environ['COLLATION_GUARD_CONTEXT'])"
    return Hook(
        path=sys.executable, args=["-c", script, str(out_path)], block_on_failure=block_on_failure
    )


def test_run_prestart_context_is_the_empty_report_shape(pg_dsn: str, tmp_path) -> None:
    host, port = _host_port(pg_dsn)
    out = tmp_path / "context.json"

    main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(pre_start=[_context_dump_hook(out)]),
    )

    assert json.loads(out.read_text()) == {
        "databases_processed": [],
        "databases_repaired": [],
        "failures": [],
        "success": True,
    }


def test_run_onsuccess_context_reflects_the_completed_run(pg_dsn: str, tmp_path) -> None:
    host, port = _host_port(pg_dsn)
    out = tmp_path / "context.json"

    main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(on_success=[_context_dump_hook(out)]),
    )

    context = json.loads(out.read_text())
    assert "postgres" in context["databases_processed"]
    assert context["failures"] == []
    assert context["success"] is True


def test_run_postrun_context_reflects_failures_when_the_run_failed(
    pg_dsn: str, failing_database: str, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    out = tmp_path / "context.json"

    main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(post_run=[_context_dump_hook(out)]),
    )

    context = json.loads(out.read_text())
    assert context["success"] is False
    assert any(f["database"] == failing_database for f in context["failures"])


def test_run_per_database_prestart_context_is_empty(pg_dsn: str, tmp_path) -> None:
    host, port = _host_port(pg_dsn)
    out = tmp_path / "context.json"

    main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(pre_start=[_context_dump_hook(out)])),
    )

    assert json.loads(out.read_text()) == {}


def test_run_per_database_success_context_lists_reindexed_relations(
    pg_dsn: str, admin_conn: psycopg.Connection, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_context_success"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    # Every database's per-database onSuccess hook fires in this run
    # (none of them fail), concurrently across worker threads -- write
    # one file per database (named from COLLATION_GUARD_DATABASE) so
    # concurrent hook subprocesses never race on the same path.
    script = (
        "import os; "
        "open(os.path.join(os.environ['OUT_DIR'], os.environ['COLLATION_GUARD_DATABASE']), 'w')"
        ".write(os.environ['COLLATION_GUARD_CONTEXT'])"
    )
    hook = Hook(path=sys.executable, args=["-c", script], environment={"OUT_DIR": str(tmp_path)})
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, label text)")
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()

        main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-locales",
            partition_repair_enabled=True,
            max_repair_attempts=10,
            hooks=HooksConfig(per_database=PerDatabaseHooks(on_success=[hook])),
        )

        context = json.loads((tmp_path / name).read_text())
        assert context == {"reindexed": ["widgets"]}
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_per_database_failure_context_lists_the_failed_relation(
    pg_dsn: str, failing_database: str, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    out = tmp_path / "context.json"

    main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        hooks=HooksConfig(per_database=PerDatabaseHooks(on_failure=[_context_dump_hook(out)])),
    )

    assert json.loads(out.read_text()) == {
        "failures": [{"relation": "widgets", "error": "REINDEX failed"}]
    }


def test_run_on_failure_context_is_the_recovered_report_shape(tmp_path) -> None:
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(
            {
                "databases_processed": ["postgres"],
                "databases_repaired": [],
                "failures": [{"database": "mydb", "relation": "widgets", "error": "boom"}],
                "success": False,
            }
        )
    )
    out = tmp_path / "recorded-context.json"
    hooks_file = _hooks_file_with_one_on_failure_hook(
        tmp_path,
        sys.executable,
        "-c",
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['COLLATION_GUARD_CONTEXT'])",
        str(out),
    )

    main.run_on_failure(hooks_file, str(context_file))

    assert json.loads(out.read_text())["databases_processed"] == ["postgres"]


def test_run_on_failure_context_falls_back_to_empty_dict_with_no_context_file(tmp_path) -> None:
    missing_context_file = tmp_path / "does-not-exist.json"
    out = tmp_path / "recorded-context.json"
    hooks_file = _hooks_file_with_one_on_failure_hook(
        tmp_path,
        sys.executable,
        "-c",
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['COLLATION_GUARD_CONTEXT'])",
        str(out),
    )

    main.run_on_failure(hooks_file, str(missing_context_file))

    assert json.loads(out.read_text()) == {}


def test_run_locks_only_a_database_that_actually_needs_a_fix(
    pg_dsn: str, admin_conn: psycopg.Connection, lockdown_conf_path
) -> None:
    """A clean database's pre-existing connection must never be touched
    -- proves lock() is never called for a database with nothing to
    fix, via real connection survival, not internal state."""
    host, port = _host_port(pg_dsn)
    clean_name = "cg_main_test_lockdown_clean"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{clean_name}"')
    admin_conn.execute(f'CREATE DATABASE "{clean_name}"')
    try:
        clean_conn = psycopg.connect(
            f"host={host} port={port} dbname={clean_name}", prepare_threshold=None
        )
        try:
            main.run(
                host,
                port,
                glibc_locales_path="/nix/store/test-glibc-locales",
                partition_repair_enabled=True,
                max_repair_attempts=10,
                lockdown_path=str(lockdown_conf_path),
            )
            # never terminated: the clean database was never locked
            assert clean_conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            clean_conn.close()
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{clean_name}"')


def test_run_locks_and_unlocks_a_database_that_needs_a_fix(
    pg_dsn: str, admin_conn: psycopg.Connection, lockdown_conf_path
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_lockdown_needs_fix"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, label text)")
            conn.execute("INSERT INTO widgets (label) VALUES ('a'), ('b')")
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()

        untagged = psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None)
        try:
            main.run(
                host,
                port,
                glibc_locales_path="/nix/store/test-glibc-locales",
                partition_repair_enabled=True,
                max_repair_attempts=10,
                lockdown_path=str(lockdown_conf_path),
            )

            # terminated at some point during the run (locked, since a
            # fix was genuinely needed), and immediately reconnectable
            # again once the run (and this database's own unlock)
            # finished
            with pytest.raises(psycopg.OperationalError):
                untagged.execute("SELECT 1")
            with psycopg.connect(
                f"host={host} port={port} dbname={name}", prepare_threshold=None
            ):
                pass
        finally:
            # already terminated server-side in the real (green) case
            # -- closing a dead connection is a safe no-op
            untagged.close()
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_glibc_stamp_phase_locks_postgres_itself_without_self_harm(
    c_utf8_pg_dsn: str, c_utf8_lockdown_conf_path
) -> None:
    """The direct regression test for the bug this plan's design review
    caught: `postgres` itself is C.UTF-8 on this cluster, so the
    glibc-stamp phase locks it -- a naive pid <> pg_backend_pid()
    exclusion would have killed either admin_conn (held open for the
    whole phase) or the per-database conn opened for "postgres"
    specifically, both alive at once. The run must still complete
    successfully."""
    host, port = _host_port(c_utf8_pg_dsn)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
        lockdown_path=str(c_utf8_lockdown_conf_path),
    )

    assert report.success
    assert "postgres" in report.databases_processed


def test_run_connection_lockdown_disabled_is_a_true_no_op(
    pg_dsn: str, admin_conn: psycopg.Connection, lockdown_conf_path
) -> None:
    """connectionLockdown.enable = false: even with a real
    lockdown_path configured (as the Nix module always would), and a
    database that genuinely needs a fix, NullLockdownManager must be a
    true no-op -- no file ever written, no pg_terminate_backend call,
    not just "disabled but still probed." Proven the same way as the
    other lockdown behaviors: via a real, untagged connection that must
    survive untouched throughout."""
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_lockdown_disabled"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, label text)")
            conn.execute("INSERT INTO widgets (label) VALUES ('a'), ('b')")
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()

        untagged = psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None)
        try:
            report = main.run(
                host,
                port,
                glibc_locales_path="/nix/store/test-glibc-locales",
                partition_repair_enabled=True,
                max_repair_attempts=10,
                lockdown_path=str(lockdown_conf_path),
                connection_lockdown_enabled=False,
            )

            assert report.success
            assert name in report.databases_repaired
            assert not lockdown_conf_path.exists()
            # never terminated -- the lock was never actually requested
            assert untagged.execute("SELECT 1").fetchone() == (1,)
        finally:
            untagged.close()
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_on_failure_cleans_up_an_active_lockdown_file(
    pg_dsn: str, admin_conn: psycopg.Connection, lockdown_conf_path, tmp_path
) -> None:
    """Simulates a crash mid-lock (an active reject rule, loaded, with
    no running LockdownManager) -- the --on-failure entry point must
    clean it up unconditionally, independent of any configured
    onFailure hooks."""
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_lockdown_cleanup"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    lockdown_conf_path.write_text(f"local   {name}   all   reject\n")
    admin_conn.execute("SELECT pg_reload_conf()")
    try:
        # sanity check: the simulated crash really did leave it locked
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None)

        hooks_file = tmp_path / "hooks.json"
        hooks_file.write_text("{}")
        context_file = tmp_path / "context.json"
        context_file.write_text(json.dumps({"failures": [], "success": True}))

        main.run_on_failure(
            str(hooks_file),
            str(context_file),
            lockdown_path=str(lockdown_conf_path),
            host=host,
            port=port,
        )

        assert not lockdown_conf_path.exists()
        with psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None):
            pass
    finally:
        lockdown_conf_path.unlink(missing_ok=True)
        admin_conn.execute("SELECT pg_reload_conf()")
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_on_failure_lockdown_cleanup_is_a_no_op_when_no_file_exists(
    tmp_path, lockdown_conf_path
) -> None:
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text("{}")
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({"failures": [], "success": True}))

    exit_code = main.run_on_failure(
        str(hooks_file),
        str(context_file),
        lockdown_path=str(lockdown_conf_path),
        host="unused",
        port="unused",
    )

    assert exit_code == 0


def test_run_on_failure_lockdown_cleanup_crash_does_not_block_the_hooks(tmp_path) -> None:
    """Regression test: cleanup_lockdown_file can raise (Postgres
    itself unreachable -- exactly the scenario that triggers this
    companion unit via OnFailure= when postgresql.service itself fails
    to start, per nixosModule/config.nix's `requires =
    ["postgresql.service"]`). run_on_failure must still run the
    configured onFailure hooks instead of crashing before it ever
    reaches them."""
    lockdown_path = tmp_path / "lockdown.conf"
    lockdown_path.write_text("some stale rule\n")  # exists -> cleanup doesn't short-circuit

    marker = tmp_path / "onfailure-ran"
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "onFailure": [
                    {
                        "path": sys.executable,
                        "args": ["-c", f"open({str(marker)!r}, 'w').close()"],
                        "blockOnFailure": False,
                        "environment": {},
                        "environmentFile": None,
                    }
                ],
            }
        )
    )

    main.run_on_failure(
        str(hooks_file),
        str(tmp_path / "context.json"),  # missing -> _recover_last_context's own default path
        lockdown_path=str(lockdown_path),
        host="127.0.0.1",
        port="1",  # nothing listens on port 1 -- the connect itself must fail
    )

    assert marker.exists()


def test_run_processes_many_databases_correctly_under_real_concurrency(
    pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    """More databases than worker threads, forcing real queuing, with
    a mix of clean and genuinely-failing databases -- proves one
    database's failure doesn't block or corrupt another's result, and
    the merged report is complete and deterministically sorted
    regardless of which thread actually finished first."""
    host, port = _host_port(pg_dsn)
    clean_names = [f"cg_parallel_clean_{i}" for i in range(4)]
    broken_name = "cg_parallel_broken"
    all_names = [*clean_names, broken_name]

    for name in all_names:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    try:
        for name in clean_names:
            admin_conn.execute(f'CREATE DATABASE "{name}"')

        admin_conn.execute(
            f'CREATE DATABASE "{broken_name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
            f"TEMPLATE template0"
        )
        with psycopg.connect(
            f"host={host} port={port} dbname={broken_name}", prepare_threshold=None
        ) as conn:
            conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text UNIQUE)")
            conn.execute("INSERT INTO widgets (name) VALUES ('alpha')")
            conn.commit()
            conn.execute(
                "UPDATE pg_index SET indisready = false "
                "WHERE indexrelid = 'widgets_name_key'::regclass"
            )
            conn.commit()
            conn.execute("INSERT INTO widgets (name) VALUES ('alpha')")
            conn.commit()
            conn.execute(
                "UPDATE pg_index SET indisready = true "
                "WHERE indexrelid = 'widgets_name_key'::regclass"
            )
            conn.commit()
            conn.execute(
                "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
                "WHERE datname = current_database()"
            )
            conn.commit()

        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-locales",
            partition_repair_enabled=True,
            max_repair_attempts=10,
            max_parallel_databases=2,  # fewer workers than databases below
        )

        for name in [*all_names, "postgres", "template1"]:
            assert name in report.databases_processed

        assert report.databases_processed == sorted(report.databases_processed)
        assert report.databases_repaired == sorted(report.databases_repaired)

        broken_failures = [f for f in report.failures if f.database == broken_name]
        assert len(broken_failures) == 1
        for name in clean_names:
            assert not any(f.database == name for f in report.failures)
    finally:
        for name in all_names:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_exception_during_per_database_processing_does_not_crash_the_whole_run(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: round 2 only hook-proofed _process_database --
    the real Postgres work beside the hooks (lock decision,
    collation.process_database, partition repair) had no exception
    guard, so a raise there still crashed run() via future.result(),
    losing every other database's already-completed work."""
    host, port = _host_port(pg_dsn)
    real_process_database = collation.process_database

    def boom(
        conn: psycopg.Connection, *, already_reindexed: bool = False
    ) -> collation.DatabaseResult:
        if conn.info.dbname == "template1":
            raise RuntimeError("boom")
        return real_process_database(conn, already_reindexed=already_reindexed)

    monkeypatch.setattr(collation, "process_database", boom)

    report = main.run(
        host,
        port,
        glibc_locales_path="/nix/store/test-glibc-locales",
        partition_repair_enabled=True,
        max_repair_attempts=10,
    )

    assert not report.success
    assert "template1" not in report.databases_processed
    assert "postgres" in report.databases_processed


def test_apply_collation_result_marks_repaired_and_logs_on_reindexed() -> None:
    """Direct unit test for a pure, dependency-free function -- round
    1 extracted _apply_collation_result specifically to be the one
    shared mapper from DatabaseResult to RunReport entries, but
    nothing exercised it directly afterward; every case was only
    reachable transitively through a real-cluster run()-level test."""
    report = main.RunReport()
    result = collation.DatabaseResult(reindexed=["widgets"], failed=[])

    ok = main._apply_collation_result(result, "mydb", report)

    assert ok is True
    assert report.databases_repaired == ["mydb"]
    assert report.failures == []


def test_apply_collation_result_appends_failure_per_failed_table_with_suffix() -> None:
    report = main.RunReport()
    result = collation.DatabaseResult(reindexed=[], failed=["widgets", "gadgets"])

    ok = main._apply_collation_result(
        result, "mydb", report, error_suffix=" (C.UTF-8 stamp check)"
    )

    assert ok is False
    assert [f.error for f in report.failures] == [
        "REINDEX failed (C.UTF-8 stamp check)",
        "REINDEX failed (C.UTF-8 stamp check)",
    ]
    assert report.databases_repaired == []


def test_apply_collation_result_appends_refresh_error_failure() -> None:
    report = main.RunReport()
    result = collation.DatabaseResult(
        reindexed=["widgets"], failed=[], refresh_error="invalid collation version change"
    )

    ok = main._apply_collation_result(result, "mydb", report)

    assert ok is False
    assert report.databases_repaired == ["mydb"]  # the reindex itself succeeded
    assert any(f.error == "invalid collation version change" for f in report.failures)


def test_main_passes_max_parallel_databases_env_var_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> main.RunReport:
        captured.update(kwargs)
        return main.RunReport()

    monkeypatch.setattr(main, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["collation-guard"])
    monkeypatch.setenv("PGHOST", "x")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("GLIBC_LOCALES_PATH", "/nix/store/x")
    monkeypatch.setenv("COLLATION_GUARD_PARTITION_REPAIR_ENABLE", "true")
    monkeypatch.setenv("COLLATION_GUARD_MAX_REPAIR_ATTEMPTS", "100")
    monkeypatch.setenv("COLLATION_GUARD_MAX_PARALLEL_DATABASES", "7")

    assert main.main() == 0
    assert captured["max_parallel_databases"] == 7


def test_run_dedupes_databases_repaired_across_glibc_stamp_and_worker_paths(
    c_utf8_pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A C.UTF-8 database can be marked repaired on two DIFFERENT
    RunReport instances in the same run: once on the top-level report
    from _process_glibc_stamp() (which mutates it directly), once on
    its own per-worker report from _process_database() (merged into
    the top-level report only afterward, via plain list concatenation
    in run()). _mark_repaired()'s dedup only sees duplicates within a
    single report instance -- it can't see across that merge boundary.
    The final report.databases_repaired must still have exactly one
    entry for such a database, not two."""
    host, port = _host_port(c_utf8_pg_dsn)
    name = "cg_main_test_glibc_and_partition_dup"
    with psycopg.connect(
        f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')  # inherits the cluster's C.UTF-8 default

    with psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None) as conn:
        conn.execute("CREATE TABLE parent (id serial, k text) PARTITION BY RANGE (k)")
        conn.execute(
            "CREATE TABLE child_a PARTITION OF parent FOR VALUES FROM (MINVALUE) TO ('m')"
        )
        conn.execute("CREATE TABLE child_b PARTITION OF parent FOR VALUES FROM ('m') TO (MAXVALUE)")
        conn.execute("INSERT INTO parent (k) VALUES ('apple')")
        conn.commit()

    # Simulate one misplaced row being found and repaired -- see the
    # same-named test above for why a *genuinely* misplaced row can't
    # be constructed in this single-locale fast tier.
    call_state = {"done": False}
    real_misplaced_rows = partitions.misplaced_rows

    def fake_misplaced_rows(conn: psycopg.Connection, schema: str, child: str) -> list[object]:
        if not call_state["done"] and child == "child_a":
            call_state["done"] = True
            (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()
            return [ctid]
        return real_misplaced_rows(conn, schema, child)

    monkeypatch.setattr(partitions, "misplaced_rows", fake_misplaced_rows)

    try:
        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-and-partition-dup",
            partition_repair_enabled=True,
            max_repair_attempts=10,
        )

        assert report.success
        assert report.databases_repaired.count(name) == 1
    finally:
        with psycopg.connect(
            f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_does_not_reindex_twice_for_a_database_both_glibc_and_named_collation_stale(
    c_utf8_pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A C.UTF-8-default database can ALSO have a separately-stale
    named (e.g. ICU) collation object -- a normal, supported Postgres
    configuration. Before this fix, _process_glibc_stamp's
    unconditional reindex (triggered by the C.UTF-8 default) and
    _process_database's own is_database_stale-triggered reindex
    (triggered by the named collation) both fired for the same
    database, reindexing every user table twice."""
    host, port = _host_port(c_utf8_pg_dsn)
    name = "cg_main_test_glibc_and_named_collation_dup"
    with psycopg.connect(
        f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')  # inherits the cluster's C.UTF-8 default

    with psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None) as conn:
        conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        conn.execute("CREATE COLLATION cg_test_collation (locale = 'en_US.UTF-8')")
        conn.commit()
        conn.execute(
            "UPDATE pg_collation SET collversion = 'not-the-real-version' "
            "WHERE collname = 'cg_test_collation'"
        )
        conn.commit()

    call_counts: dict[str, int] = {}
    real_reindex = collation.reindex_all_user_tables

    def counting_reindex(conn: psycopg.Connection) -> collation.DatabaseResult:
        call_counts[conn.info.dbname] = call_counts.get(conn.info.dbname, 0) + 1
        return real_reindex(conn)

    monkeypatch.setattr(collation, "reindex_all_user_tables", counting_reindex)

    try:
        report = main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-and-named-collation-dup",
            partition_repair_enabled=False,
            max_repair_attempts=10,
        )

        assert report.success
        assert call_counts.get(name) == 1
    finally:
        with psycopg.connect(
            f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_run_glibc_stamp_reindex_failure_does_not_mark_database_already_reindexed(
    c_utf8_pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the cleanly_reindexed branch (main.py's
    _process_glibc_stamp): a database whose C.UTF-8-triggered reindex
    FAILS must still be retried (not skipped as already_reindexed) by
    the later, named-collation-triggered pass. Round 3's own double-
    reindex test only proved the all-succeed path -- confirmed nothing
    would previously catch that branch's if/else being swapped."""
    host, port = _host_port(c_utf8_pg_dsn)
    name = "cg_main_test_glibc_partial_failure_retry"
    with psycopg.connect(
        f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
    ) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')

    with psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None) as conn:
        conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        conn.execute("CREATE COLLATION cg_test_collation (locale = 'en_US.UTF-8')")
        conn.commit()
        conn.execute(
            "UPDATE pg_collation SET collversion = 'not-the-real-version' "
            "WHERE collname = 'cg_test_collation'"
        )
        conn.commit()

    call_counts: dict[str, int] = {}
    already_failed_once = {name: False}
    real_reindex = collation.reindex_all_user_tables

    def flaky_reindex(conn: psycopg.Connection) -> collation.DatabaseResult:
        dbname = conn.info.dbname
        call_counts[dbname] = call_counts.get(dbname, 0) + 1
        if dbname == name and not already_failed_once[name]:
            already_failed_once[name] = True
            return collation.DatabaseResult(reindexed=[], failed=["widgets"])
        return real_reindex(conn)

    monkeypatch.setattr(collation, "reindex_all_user_tables", flaky_reindex)

    try:
        main.run(
            host,
            port,
            glibc_locales_path="/nix/store/test-glibc-partial-failure-retry",
            partition_repair_enabled=False,
            max_repair_attempts=10,
        )

        # The glibc-stamp phase's own attempt failed -- a second,
        # genuine attempt via _process_database must have happened
        # (not been skipped as already_reindexed).
        assert call_counts.get(name) == 2
    finally:
        with psycopg.connect(
            f"host={host} port={port} dbname=postgres", autocommit=True, prepare_threshold=None
        ) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
