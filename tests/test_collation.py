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


def test_process_database_reindex_failure_is_isolated_per_relation(
    pg_dsn: str, test_db: str
) -> None:
    """One table with an unrepairable duplicate key must not block
    reindexing every other, perfectly fixable table in the same
    database -- the real-world gap this design corrects for (REINDEX
    DATABASE aborts entirely on the first bad index; see
    docs/decisions/0002). Regression test for cycle 4."""
    with _connect(pg_dsn, test_db) as conn:
        conn.execute("CREATE TABLE good_table (id serial PRIMARY KEY, name text)")
        conn.execute("INSERT INTO good_table (name) VALUES ('alpha'), ('beta')")

        conn.execute("CREATE TABLE bad_table (id serial PRIMARY KEY, name text UNIQUE)")
        conn.execute("INSERT INTO bad_table (name) VALUES ('alpha')")
        conn.commit()

        # Inject a duplicate that bypasses the unique index: mark it
        # "not ready" so DML stops maintaining it, insert the
        # conflicting row, then mark it ready again without rebuilding
        # it -- the index now silently disagrees with the heap. REINDEX
        # has to rescan the heap from scratch, so it's the first thing
        # that actually notices. Same technique already proven live
        # against the real production cluster.
        conn.execute(
            "UPDATE pg_index SET indisready = false "
            "WHERE indexrelid = 'bad_table_name_key'::regclass"
        )
        conn.commit()
        conn.execute("INSERT INTO bad_table (name) VALUES ('alpha')")
        conn.commit()
        conn.execute(
            "UPDATE pg_index SET indisready = true "
            "WHERE indexrelid = 'bad_table_name_key'::regclass"
        )
        conn.commit()

        _fake_stale(conn, test_db)
        conn.commit()

        result = process_database(conn)

        assert not result.ok
        assert result.reindexed == ["good_table"]
        assert result.failed == ["bad_table"]
        # A partial failure means the database's content hasn't been
        # fully verified under the current collation -- its recorded
        # version must stay stale, not get marked current on a
        # technicality.
        assert database_collation_is_stale(conn)
