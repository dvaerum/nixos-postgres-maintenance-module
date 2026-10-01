"""Partition-bounds drift: applicability filter and repair.

A RANGE/LIST-partitioned table's partition bounds are validated once,
at ATTACH/CREATE time, under whatever collation sort order was active
then. If the underlying collation library later changes sort order,
previously-correct bounds can silently misplace rows relative to where
a fresh comparison would route them -- the only text-partitioning
failure mode a plain REINDEX can't fix, since indexes are rebuilt from
the heap but partition routing itself is not re-validated by REINDEX.

C/POSIX-keyed partitions are immune: Postgres never versions those
collations at all (see collation.py's module docstring), so there is
no "drift" to detect for them in the first place.
"""

from __future__ import annotations

import psycopg


def partition_repair_candidates(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(schema, table) for every partitioned table with at least one
    partition-key column whose collation is real (not C/POSIX, and not
    the "no collation" case for non-text key types) -- a pure catalog
    lookup, no row data touched. This is the applicability filter
    only: it says nothing about whether any row is actually misplaced,
    only whether the table is the kind where that's even possible.
    """
    rows = conn.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_partitioned_table pt
        JOIN pg_class c ON c.oid = pt.partrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND EXISTS (
              SELECT 1
              FROM unnest(pt.partcollation::oid[]) AS collid
              JOIN pg_collation col ON col.oid = collid
              WHERE col.collname NOT IN ('C', 'POSIX')
          )
        ORDER BY n.nspname, c.relname
        """
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]
