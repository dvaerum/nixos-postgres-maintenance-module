"""Fast tier: partition-bounds drift detection.

Cycle 6 of the TDD sequence -- the pure catalog applicability filter,
no actual repair yet (that's cycles 7+).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from collation_guard.partitions import partition_repair_candidates


@pytest.fixture
def test_db(admin_conn: psycopg.Connection, pg_dsn: str) -> Iterator[psycopg.Connection]:
    name = "cg_test_cycle6"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    parts = dict(item.split("=", 1) for item in pg_dsn.split())
    dsn = f"host={parts['host']} port={parts['port']} dbname={name}"
    conn = psycopg.connect(dsn, prepare_threshold=None)
    try:
        yield conn
    finally:
        conn.close()
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_partition_repair_candidates_flags_only_non_c_collated_keys(
    test_db: psycopg.Connection,
) -> None:
    conn = test_db
    # Explicit C collation: immune, Postgres never versions it.
    conn.execute('CREATE TABLE c_keyed (id serial, k text COLLATE "C") PARTITION BY RANGE (k)')
    # No explicit COLLATE: uses the database's default collation, which
    # is a real (non-C) libc locale here (see conftest.py's cluster
    # locale) -- this is the case that can actually drift.
    conn.execute("CREATE TABLE libc_keyed (id serial, k text) PARTITION BY RANGE (k)")
    # Not partitioned at all: must never be flagged regardless of its
    # own collation.
    conn.execute("CREATE TABLE plain_table (id serial, k text)")
    # Partitioned on a non-collatable key: must never be flagged.
    conn.execute("CREATE TABLE int_keyed (id serial, k integer) PARTITION BY RANGE (k)")
    conn.commit()

    assert partition_repair_candidates(conn) == [("public", "libc_keyed")]


def test_partition_repair_candidates_is_empty_with_no_partitioned_tables(
    test_db: psycopg.Connection,
) -> None:
    assert partition_repair_candidates(test_db) == []
