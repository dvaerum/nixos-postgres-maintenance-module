"""Fast tier: main.run() orchestration end-to-end against the real
ephemeral cluster -- collation.py + partitions.py wired together, same
as the real systemd ExecStart does, just without systemd itself.
"""

from __future__ import annotations

import sys

import psycopg
import pytest

from collation_guard import main
from collation_guard.hooks import Hook, HooksConfig


def _host_port(pg_dsn: str) -> tuple[str, str]:
    parts = dict(item.split("=", 1) for item in pg_dsn.split())
    return parts["host"], parts["port"]


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
    pg_dsn: str, admin_conn: psycopg.Connection, tmp_path
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_main_test_onsuccess_gating"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        with psycopg.connect(
            f"host={host} port={port} dbname={name}", prepare_threshold=None
        ) as conn:
            # same real reproduction used in test_collation.py's cycle 4:
            # a duplicate that bypasses the unique index, so REINDEX
            # genuinely fails.
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
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


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
