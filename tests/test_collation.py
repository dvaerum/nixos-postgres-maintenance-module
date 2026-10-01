"""Fast tier: Postgres-tracked collation mismatch -> reindex + refresh.

Cycle 3 of the TDD sequence in docs/decisions -- see collation.py's own
module docstring for why this reindexes per-relation rather than
calling `REINDEX DATABASE` once.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from collation_guard.collation import (
    database_collation_is_stale,
    process_database,
)


def _fake_stale(conn: psycopg.Connection, database: str) -> None:
    """Simulate a drifted glibc by corrupting the recorded version --
    the same technique already rehearsed live against the real
    production cluster earlier in this project's design work."""
    conn.execute(
        "UPDATE pg_database SET datcollversion = 'not-the-real-version' WHERE datname = %s",
        (database,),
    )


@pytest.fixture
def test_db(admin_conn: psycopg.Connection, pg_dsn: str) -> Iterator[str]:
    name = "cg_test_cycle3"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    # LOCALE_PROVIDER libc + an explicit non-C locale: Postgres only
    # records a datcollversion at all for a real libc locale -- C/POSIX
    # are never versioned (confirmed against PG16 source), so a C-locale
    # database could never exercise this path.
    admin_conn.execute(
        f'CREATE DATABASE "{name}" LOCALE_PROVIDER libc LOCALE \'en_US.UTF-8\' '
        f"TEMPLATE template0"
    )
    try:
        yield name
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _connect(pg_dsn: str, dbname: str) -> psycopg.Connection:
    parts = dict(item.split("=", 1) for item in pg_dsn.split())
    dsn = f"host={parts['host']} port={parts['port']} dbname={dbname}"
    return psycopg.connect(dsn, prepare_threshold=None)


def test_process_database_is_noop_when_not_stale(pg_dsn: str, test_db: str) -> None:
    with _connect(pg_dsn, test_db) as conn:
        assert not database_collation_is_stale(conn)
        result = process_database(conn)
        assert result.ok
        assert result.reindexed == []
        assert result.failed == []


def test_process_database_reindexes_and_refreshes_on_mismatch(pg_dsn: str, test_db: str) -> None:
    with _connect(pg_dsn, test_db) as conn:
        conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        conn.execute("CREATE INDEX widgets_name_idx ON widgets (name)")
        conn.execute("INSERT INTO widgets (name) VALUES ('alpha'), ('beta'), ('gamma')")
        conn.commit()

        _fake_stale(conn, test_db)
        conn.commit()

        assert database_collation_is_stale(conn)

        result = process_database(conn)

        assert result.ok
        assert result.reindexed == ["widgets"]
        assert result.failed == []
        assert not database_collation_is_stale(conn)


def test_process_database_is_idempotent_after_refresh(pg_dsn: str, test_db: str) -> None:
    with _connect(pg_dsn, test_db) as conn:
        conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        conn.commit()
        _fake_stale(conn, test_db)
        conn.commit()

        first = process_database(conn)
        assert first.ok
        assert first.reindexed == ["widgets"]

        second = process_database(conn)
        assert second.ok
        assert second.reindexed == []
        assert second.failed == []
