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

from collation_guard import main
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
