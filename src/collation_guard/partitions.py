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

from dataclasses import dataclass, field

import psycopg
from psycopg import sql


@dataclass(frozen=True, slots=True)
class PartitionRepairResult:
    """Outcome of repairing every misplaced row across one partitioned
    table's direct children."""

    repaired: int = 0
    skipped_ruled_children: list[str] = field(default_factory=list)
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.exhausted


def partition_children(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    """Direct partition children of `table`, via pg_inherits -- not
    recursive (a sub-partitioned child is itself a partitioned table
    with its own children, handled separately; see cycle 11)."""
    rows = conn.execute(
        """
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.inhparent = %s::regclass AND n.nspname = %s
        ORDER BY c.relname
        """,
        (f"{schema}.{table}", schema),
    ).fetchall()
    return [str(r[0]) for r in rows]


def repair_partition_table(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    key_columns: list[str],
    max_attempts: int = 100,
) -> PartitionRepairResult:
    """Repair every misplaced row across every direct child of `table`,
    looping until a full pass finds nothing left to fix or a safety cap
    is hit. A capped loop rather than one pass: repairing a row moves
    it to a *different* partition, which that partition's own next scan
    will need to re-examine, and the project has no prior art to trust
    that this always converges in one pass (see
    docs/learnings/partition-repair-testing.md). A ruled child is
    skipped entirely (see has_rule()) and excluded from every pass, not
    just logged once.
    """
    children = [c for c in partition_children(conn, schema, table) if not has_rule(conn, schema, c)]
    skipped = [c for c in partition_children(conn, schema, table) if has_rule(conn, schema, c)]
    repaired = 0

    for _attempt in range(max_attempts):
        found_any = False
        for child in children:
            for ctid in misplaced_rows(conn, schema, child):
                found_any = True
                repair_row(conn, schema, table, child, key_columns, ctid)
                conn.commit()
                repaired += 1
        if not found_any:
            return PartitionRepairResult(repaired=repaired, skipped_ruled_children=skipped)

    return PartitionRepairResult(repaired=repaired, skipped_ruled_children=skipped, exhausted=True)


def has_rule(conn: psycopg.Connection, schema: str, table: str) -> bool:
    """True if `table` has any user-defined RULE (pg_rewrite) attached.
    Postgres has no DISABLE RULE equivalent to DISABLE TRIGGER USER --
    there's no way to wrap a repair so a rule's side effects stay
    suppressed the way repair_row() suppresses triggers. The guard
    skips a ruled relation entirely (log it, let a human decide) rather
    than risk firing arbitrary rule actions during an internal
    maintenance write. True legacy rule-based manual partitioning can't
    appear here at all -- it's a structurally different, mutually
    exclusive mechanism from declarative pg_partitioned_table
    partitioning -- so in practice this only catches a genuinely
    unusual custom rule someone added to a partition child directly.
    """
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_rewrite WHERE ev_class = %s::regclass)",
        (f"{schema}.{table}",),
    ).fetchone()
    return bool(row is not None and row[0])


def misplaced_rows(conn: psycopg.Connection, schema: str, child: str) -> list[object]:
    """ctid of every row in `child` that no longer satisfies its own
    partition bound constraint, checked directly against
    `pg_get_partition_constraintdef()` -- not by provoking Postgres's
    own ATTACH-validation error and parsing it. Confirmed against
    PG16's tablecmds.c: that error ("partition constraint of relation
    ... is violated by some row") names only the table, never the row
    -- there is no DETAIL with row values to key a fix off of.
    """
    row = conn.execute(
        "SELECT pg_get_partition_constraintdef(%s::regclass)",
        (f"{schema}.{child}",),
    ).fetchone()
    if row is None or row[0] is None:
        return []
    constraint = row[0]
    rows = conn.execute(
        sql.SQL("SELECT ctid FROM {}.{} WHERE NOT ({})").format(
            sql.Identifier(schema), sql.Identifier(child), sql.SQL(constraint)
        )
    ).fetchall()
    return [r[0] for r in rows]


def repair_row(
    conn: psycopg.Connection,
    schema: str,
    root_table: str,
    child: str,
    key_columns: list[str],
    ctid: object,
) -> None:
    """Force Postgres's own cross-partition UPDATE row movement (PG11+)
    to relocate one misplaced row to wherever it actually belongs under
    the current collation: an UPDATE of a partition key column to its
    own unchanged value still re-evaluates the row against every
    sibling partition's bounds, and moves it (DELETE + INSERT under the
    hood) if its current partition no longer matches.

    Must be issued against `root_table` (the top-level partitioned
    table), identifying the physical row via `tableoid` + `ctid` --
    confirmed empirically that an UPDATE issued directly against the
    child relation does NOT get cross-partition routing at all; it only
    re-checks that child's own bound and rejects the row outright if it
    fails, never considering siblings (see
    docs/learnings/partition-repair-testing.md).

    DISABLE/ENABLE TRIGGER USER brackets this so that internal
    DELETE+INSERT doesn't fire user-defined triggers as a side effect
    of what is, from the application's point of view, a no-op write --
    USER (not ALL) deliberately leaves FK/constraint-enforcement
    triggers active throughout (see docs/decisions/0004, and the real
    bug this distinction avoids: BUG #18516, where DISABLE TRIGGER ALL
    silently dropped FK enforcement with no re-validation on re-enable).
    """
    child_table = sql.Identifier(schema, child)
    set_clause = sql.SQL(", ").join(
        sql.SQL("{0} = {0}").format(sql.Identifier(c)) for c in key_columns
    )
    conn.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(child_table))
    try:
        conn.execute(
            sql.SQL("UPDATE {} SET {} WHERE tableoid = %s::regclass AND ctid = %s::tid").format(
                sql.Identifier(schema, root_table), set_clause
            ),
            (f"{schema}.{child}", ctid),
        )
    finally:
        conn.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER USER").format(child_table))


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
