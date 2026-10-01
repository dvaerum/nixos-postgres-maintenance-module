"""Postgres-tracked collation-version mismatch: detection and repair.

Covers everything Postgres itself records a version for: a database's
own default collation (`pg_database.datcollversion`) and any named
collation object (`pg_collation.collversion`). Both are NULL for `C`,
`C.*`, and `POSIX` -- Postgres never versions those at all (confirmed
against PG16's `get_collation_actual_version()` source) -- so this
module naturally does nothing for them. See docs/decisions/0003 for
the separate C.UTF-8 stamp mechanism that covers that gap, and
docs/decisions/0002 for why this reindexes per-relation rather than
calling `REINDEX DATABASE` once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DatabaseResult:
    """Outcome of processing one database's Postgres-tracked collations."""

    reindexed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def database_collation_is_stale(conn: psycopg.Connection) -> bool:
    """True if the connection's current database's own default collation
    no longer matches what's actually loaded."""
    row = conn.execute(
        """
        SELECT datcollversion IS NOT NULL
           AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
        FROM pg_database
        WHERE datname = current_database()
        """
    ).fetchone()
    return bool(row is not None and row[0])


def stale_named_collations(conn: psycopg.Connection) -> list[str]:
    """Names of named (non-default) collation objects in the current
    database whose recorded version no longer matches what's loaded."""
    rows = conn.execute(
        """
        SELECT collname
        FROM pg_collation
        WHERE collversion IS NOT NULL
          AND collversion IS DISTINCT FROM pg_collation_actual_version(oid)
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def user_tables(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(schema, table) for every ordinary user table -- excludes the
    system catalogs, which `REINDEX DATABASE` itself also never touches
    (confirmed in the PG16 REINDEX docs: "Recreate all indexes within
    the current database, except system catalogs")."""
    rows = conn.execute(
        """
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY schemaname, tablename
        """
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def reindex_table(conn: psycopg.Connection, schema: str, table: str) -> None:
    conn.execute(
        sql.SQL("REINDEX TABLE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    )


def refresh_database_collation_version(conn: psycopg.Connection, database: str) -> None:
    conn.execute(
        sql.SQL("ALTER DATABASE {} REFRESH COLLATION VERSION").format(sql.Identifier(database))
    )


def refresh_named_collation_version(conn: psycopg.Connection, collation: str) -> None:
    conn.execute(sql.SQL("ALTER COLLATION {} REFRESH VERSION").format(sql.Identifier(collation)))


def process_database(conn: psycopg.Connection) -> DatabaseResult:
    """Check and repair the connection's current database.

    Reindexes every user table independently -- not one `REINDEX
    DATABASE` call, which aborts entirely on the first bad index and
    would leave every other, perfectly fixable table untouched (a real
    production incident, see docs/decisions/0002). Only refreshes the
    recorded collation version(s) once every table reindexed cleanly:
    a partial failure means the database's content hasn't been fully
    verified under the current collation, so its recorded version
    should stay stale, not be marked current on a technicality.
    """
    if not database_collation_is_stale(conn) and not stale_named_collations(conn):
        return DatabaseResult()

    stale_collations = stale_named_collations(conn)
    reindexed: list[str] = []
    failed: list[str] = []

    for schema, table in user_tables(conn):
        try:
            reindex_table(conn, schema, table)
        except psycopg.Error as exc:
            logger.error(
                "REINDEX failed for %s.%s (likely a real constraint violation under "
                "the stale collation -- this table's version will NOT be refreshed): %s",
                schema,
                table,
                exc,
            )
            conn.rollback()
            failed.append(table)
        else:
            conn.commit()
            reindexed.append(table)

    if not failed:
        if database_collation_is_stale(conn):
            refresh_database_collation_version(conn, conn.info.dbname)
        for name in stale_collations:
            refresh_named_collation_version(conn, name)
        conn.commit()

    return DatabaseResult(reindexed=reindexed, failed=failed)
