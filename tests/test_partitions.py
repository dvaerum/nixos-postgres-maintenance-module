"""Fast tier: partition-bounds drift detection.

Cycle 6 of the TDD sequence -- the pure catalog applicability filter,
no actual repair yet (that's cycles 7+).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from collation_guard.partitions import misplaced_rows, partition_repair_candidates, repair_row


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


# NOTE: there is no legitimate SQL/tool-level way to get a
# constraint-violating row into an already-attached partition on a
# single real Postgres instance -- confirmed the hard way (see
# docs/learnings/partition-repair-testing.md): ATTACH PARTITION always
# re-scans and rejects, a matching `NOT VALID` CHECK constraint does
# NOT skip that scan (that trick only avoids double-scanning when
# paired with an explicit prior `VALIDATE CONSTRAINT`, which itself
# would reject a genuinely bad row -- there's no bypass, by design),
# and pg_surgery (the real contrib tool for forcing corrupt data into a
# heap) only ships heap_force_freeze/heap_force_kill, not a force
# insert. A truly misplaced row only exists in reality because the
# *same* collation's comparison behavior changed between validation
# time and now (an actual glibc/ICU version change) -- not reproducible
# in a single-locale sandbox. So cycle 7 proves its two load-bearing
# halves separately instead of one end-to-end fake-drift reproduction:
# detection against a clean partition, and the row-movement mechanism
# repair_row() depends on, proven with a real value change.
#
# That same probing also caught a real design bug: an UPDATE issued
# directly against the CHILD relation does not get cross-partition
# routing at all -- it just re-checks that child's own bound and
# rejects the row outright. Routing only happens when the UPDATE goes
# through the top-level partitioned (root) table, identifying the
# physical row via tableoid + ctid. repair_row() takes root_table for
# exactly this reason.


@pytest.fixture
def partitioned_db(test_db: psycopg.Connection) -> psycopg.Connection:
    conn = test_db
    conn.execute("CREATE TABLE parent (id serial, k text) PARTITION BY RANGE (k)")
    conn.execute("CREATE TABLE child_a PARTITION OF parent FOR VALUES FROM (MINVALUE) TO ('m')")
    conn.execute("CREATE TABLE child_b PARTITION OF parent FOR VALUES FROM ('m') TO (MAXVALUE)")
    conn.execute("INSERT INTO parent (k) VALUES ('apple')")
    conn.commit()
    return conn


def test_misplaced_rows_is_empty_for_a_correctly_placed_row(
    partitioned_db: psycopg.Connection,
) -> None:
    assert misplaced_rows(partitioned_db, "public", "child_a") == []


def test_repair_row_is_a_noop_for_an_already_correct_row(
    partitioned_db: psycopg.Connection,
) -> None:
    conn = partitioned_db
    (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()

    repair_row(conn, "public", "parent", "child_a", ["k"], ctid)
    conn.commit()

    row = conn.execute("SELECT k FROM child_a WHERE k = 'apple'").fetchone()
    assert row is not None


def test_cross_partition_update_through_root_moves_a_row_to_its_correct_partition(
    partitioned_db: psycopg.Connection,
) -> None:
    """Proves the mechanism repair_row() depends on: an UPDATE issued
    through the top-level ROOT table (identifying the physical row via
    tableoid + ctid, exactly as repair_row() does) triggers Postgres's
    cross-partition UPDATE row movement (PG11+) when the new value no
    longer satisfies the row's current partition. This was the key
    uncertain assumption in repair_row()'s design -- confirmed here
    with a real value change, since an actual stale-comparison scenario
    (same value, different collation behavior) can't be constructed in
    a single-locale sandbox (see the NOTE above)."""
    conn = partitioned_db
    (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()

    conn.execute("ALTER TABLE child_a DISABLE TRIGGER USER")
    try:
        conn.execute(
            "UPDATE parent SET k = 'zebra' WHERE tableoid = 'child_a'::regclass AND ctid = %s::tid",
            (ctid,),
        )
    finally:
        conn.execute("ALTER TABLE child_a ENABLE TRIGGER USER")
    conn.commit()

    # Still reachable through the parent -- nothing was lost.
    row = conn.execute("SELECT k FROM parent WHERE k = 'zebra'").fetchone()
    assert row is not None

    # And it physically moved to the partition that actually matches it.
    in_a = conn.execute("SELECT k FROM child_a WHERE k = 'zebra'").fetchone()
    in_b = conn.execute("SELECT k FROM child_b WHERE k = 'zebra'").fetchone()
    assert in_a is None
    assert in_b is not None


def test_repair_row_does_not_fire_user_triggers(partitioned_db: psycopg.Connection) -> None:
    """Cycle 8: repair_row()'s internal UPDATE must not fire a
    user-defined trigger as a side effect -- it's maintenance, not an
    application-level write. Proven with a counter trigger, not just
    trusted from reading DISABLE TRIGGER USER's docs."""
    conn = partitioned_db
    conn.execute("CREATE TABLE trigger_calls (n integer NOT NULL)")
    conn.execute("INSERT INTO trigger_calls VALUES (0)")
    conn.execute(
        """
        CREATE FUNCTION count_trigger() RETURNS trigger AS $$
        BEGIN
            UPDATE trigger_calls SET n = n + 1;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.execute(
        "CREATE TRIGGER count_updates BEFORE UPDATE ON child_a FOR EACH ROW "
        "EXECUTE FUNCTION count_trigger()"
    )
    conn.commit()

    (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()
    repair_row(conn, "public", "parent", "child_a", ["k"], ctid)
    conn.commit()

    (count,) = conn.execute("SELECT n FROM trigger_calls").fetchone()
    assert count == 0

    # Sanity check: the trigger genuinely works and this isn't a
    # vacuous pass -- a plain UPDATE outside of repair_row() must still
    # increment it. Re-fetch ctid: repair_row's own UPDATE already gave
    # the row a new tuple version (MVCC creates one even for an
    # unchanged value), so the old ctid no longer points at the live row.
    (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()
    conn.execute("UPDATE child_a SET k = k WHERE ctid = %s::tid", (ctid,))
    conn.commit()
    (count_after_plain_update,) = conn.execute("SELECT n FROM trigger_calls").fetchone()
    assert count_after_plain_update == 1


def test_repair_row_does_not_needlessly_cascade_an_unchanged_foreign_key(
    partitioned_db: psycopg.Connection,
) -> None:
    """Cycle 8's FK-enforcement angle: an incoming ON UPDATE CASCADE
    foreign key is enforced via Postgres's own internally-generated
    constraint trigger on `parent` -- not a user trigger, so DISABLE
    TRIGGER USER never touches it (this is what makes USER, not ALL,
    load-bearing: see BUG #18516 in repair_row()'s docstring). Postgres
    additionally skips re-firing that constraint trigger at all when
    the referenced key's value hasn't actually changed (confirmed via
    research, verified here): the referencing row must not be
    physically rewritten by repair_row()'s self-assignment UPDATE."""
    conn = partitioned_db
    conn.execute("ALTER TABLE parent ADD CONSTRAINT parent_k_unique UNIQUE (k)")
    conn.execute(
        "CREATE TABLE referencing (id serial PRIMARY KEY, parent_k text "
        "REFERENCES parent (k) ON UPDATE CASCADE)"
    )
    conn.execute("INSERT INTO referencing (parent_k) VALUES ('apple')")
    conn.commit()

    (xmin_before,) = conn.execute(
        "SELECT xmin FROM referencing WHERE parent_k = 'apple'"
    ).fetchone()

    (ctid,) = conn.execute("SELECT ctid FROM child_a WHERE k = 'apple'").fetchone()
    repair_row(conn, "public", "parent", "child_a", ["k"], ctid)
    conn.commit()

    row = conn.execute("SELECT xmin FROM referencing WHERE parent_k = 'apple'").fetchone()
    assert row is not None
    assert row[0] == xmin_before
