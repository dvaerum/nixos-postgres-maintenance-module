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
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DatabaseResult:
    """Outcome of processing one database's Postgres-tracked collations."""

    reindexed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    refresh_error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.refresh_error is None


def database_collation_is_stale(conn: psycopg.Connection) -> bool:
    """True if the connection's current database's own default collation
    no longer matches what's actually loaded.
    https://www.postgresql.org/docs/17/functions-admin.html -- Table 9.102,
    pg_database_collation_actual_version()."""
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
    database whose recorded version no longer matches what's loaded.
    https://www.postgresql.org/docs/17/functions-admin.html -- Table 9.102,
    pg_collation_actual_version()."""
    rows = conn.execute(
        """
        SELECT collname
        FROM pg_collation
        WHERE collversion IS NOT NULL
          AND collversion IS DISTINCT FROM pg_collation_actual_version(oid)
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def is_database_stale(conn: psycopg.Connection) -> bool:
    """True if this database has any Postgres-tracked collation work
    outstanding -- its own default collation or any named collation.
    The one shared predicate both process_database()'s own gate and
    main.py's lock decision call, so they can't drift apart (see
    docs/decisions/0007)."""
    return database_collation_is_stale(conn) or bool(stale_named_collations(conn))


def user_tables(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(schema, table) for every ordinary user table -- excludes the
    system catalogs, which `REINDEX DATABASE` itself also never touches
    (confirmed in the PG16 REINDEX docs: "Recreate all indexes within
    the current database, except system catalogs"), and excludes a
    partitioned table's own entry (relkind 'p', at any partitioning
    depth): it holds no physical storage or indexes of its own -- only
    its leaf partitions (relkind 'r', already included here) do -- and
    `REINDEX TABLE` on a partitioned table parent needs multiple
    internal transactions, which fails outright inside this project's
    per-relation transaction ("REINDEX TABLE cannot run inside a
    transaction block"). Confirmed empirically building the ICU-drift
    test (docs/decisions/0008): this hit unconditionally for any
    partitioned table caught up in a stale-collation reindex, unrelated
    to ICU specifically. `pg_tables` can't be used here any more since
    its own definition includes relkind 'p' rows with no relkind column
    exposed to filter them back out, hence the join against pg_class
    directly."""
    rows = conn.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY n.nspname, c.relname
        """
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def reindex_table(conn: psycopg.Connection, schema: str, table: str) -> None:
    """https://www.postgresql.org/docs/17/sql-reindex.html"""
    conn.execute(
        sql.SQL("REINDEX TABLE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    )


def refresh_database_collation_version(conn: psycopg.Connection, database: str) -> None:
    """https://www.postgresql.org/docs/17/sql-alterdatabase.html"""
    conn.execute(
        sql.SQL("ALTER DATABASE {} REFRESH COLLATION VERSION").format(sql.Identifier(database))
    )


def refresh_named_collation_version(conn: psycopg.Connection, collation: str) -> None:
    """https://www.postgresql.org/docs/17/sql-altercollation.html"""
    conn.execute(sql.SQL("ALTER COLLATION {} REFRESH VERSION").format(sql.Identifier(collation)))


def template0_collation_is_stale(conn: psycopg.Connection) -> bool:
    """`template0` disallows direct connections (`datallowconn = false`),
    but its recorded collation version can still be read from any other
    connection in the same cluster -- `pg_database` is a shared catalog,
    not per-database."""
    row = conn.execute(
        """
        SELECT datcollversion IS NOT NULL
           AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
        FROM pg_database
        WHERE datname = 'template0'
        """
    ).fetchone()
    return bool(row is not None and row[0])


def refresh_template0(conn: psycopg.Connection) -> None:
    """`template0` carries no user objects, so there's nothing to
    reindex -- just refresh its recorded version. `ALTER DATABASE`
    has no same-database restriction (confirmed against PG16's
    `AlterDatabaseRefreshColl()` source), so this can run from any
    connection in the cluster, same as the stale-check above."""
    refresh_database_collation_version(conn, "template0")


def _safe_refresh(
    conn: psycopg.Connection, description: str, refresh: Callable[[], None]
) -> str | None:
    """Run a REFRESH COLLATION VERSION call, catching the one
    documented failure mode: Postgres rejects any refresh where the
    recorded and actual versions disagree on NULL-ness ("invalid
    collation version change", confirmed in PG16's
    `dbcommands.c:AlterDatabaseRefreshColl()` -- `elog(ERROR, ...)` when
    exactly one of old/new version is NULL). Low probability in
    practice (would need a foreign-provider version string, or a
    locale/provider actually changing out from under a database), but
    cheap to handle defensively rather than assume success. Returns an
    error message on failure, None on success."""
    try:
        refresh()
    except psycopg.Error as exc:
        logger.error("REFRESH COLLATION VERSION failed for %s: %s", description, exc)
        conn.rollback()
        return str(exc)
    else:
        conn.commit()
        return None


def reindex_all_user_tables(conn: psycopg.Connection) -> DatabaseResult:
    """Reindex every user table in the connection's current database
    independently -- not one `REINDEX DATABASE` call, which aborts
    entirely on the first bad index and would leave every other,
    perfectly fixable table untouched (a real production incident, see
    docs/decisions/0002). Shared by process_database() (triggered by a
    Postgres-tracked version mismatch) and the C.UTF-8 stamp path in
    main.py (triggered unconditionally by a stale stamp, since Postgres
    records no version for C/C.*/POSIX at all to check against).
    """
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

    return DatabaseResult(reindexed=reindexed, failed=failed)


def process_database(
    conn: psycopg.Connection, *, already_reindexed: bool = False
) -> DatabaseResult:
    """Check and repair the connection's current database's
    Postgres-tracked collation versions. Only refreshes the recorded
    version(s) once every table reindexed cleanly: a partial failure
    means the database's content hasn't been fully verified under the
    current collation, so its recorded version should stay stale, not
    be marked current on a technicality.

    `already_reindexed=True` is for a database whose tables were just
    reindexed by a *different* trigger in the same run -- main.py's
    glibc-stamp phase, which reindexes every C.UTF-8 database
    unconditionally. A database can be C.UTF-8-default *and* have a
    separately-stale named collation at once (a normal configuration),
    so without this, both triggers would independently reindex the
    same tables. Skips the redundant REINDEX pass but still performs
    the version refresh below, since the glibc-stamp phase never does
    that for a named collation -- only process_database() does.
    """
    if not is_database_stale(conn):
        return DatabaseResult()

    stale_collations = stale_named_collations(conn)
    if already_reindexed:
        reindexed, failed = [], []
    else:
        result = reindex_all_user_tables(conn)
        reindexed, failed = result.reindexed, result.failed

    if not failed:
        refresh_error: str | None = None
        if database_collation_is_stale(conn):
            dbname = conn.info.dbname
            refresh_error = _safe_refresh(
                conn,
                f"database {dbname}",
                partial(refresh_database_collation_version, conn, dbname),
            )
        for name in stale_collations:
            if refresh_error is not None:
                break
            refresh_error = _safe_refresh(
                conn,
                f"collation {name}",
                partial(refresh_named_collation_version, conn, name),
            )
        return DatabaseResult(reindexed=reindexed, failed=failed, refresh_error=refresh_error)

    return DatabaseResult(reindexed=reindexed, failed=failed)


GLIBC_STAMP_PREFIX = "collation-guard:glibcLocales="
_STAMP_LINE_RE = re.compile(rf"^{re.escape(GLIBC_STAMP_PREFIX)}(\S+)$", re.MULTILINE)


def connectable_databases(conn: psycopg.Connection) -> list[str]:
    """Every database in the cluster that can actually be connected to
    -- template0 is deliberately excluded (datallowconn is always false
    for it; see process_template0() for its own, connection-free path).
    """
    rows = conn.execute("SELECT datname FROM pg_database WHERE datallowconn").fetchall()
    return [str(r[0]) for r in rows]


def _database_comment(conn: psycopg.Connection, database: str) -> str | None:
    row = conn.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
        (database,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def glibc_stamp(conn: psycopg.Connection) -> str | None:
    """The glibcLocales store path recorded the last time the C.UTF-8
    stamp was successfully advanced, or None if never recorded (a
    brand-new cluster, or one that predates this guard). Stored as one
    line within a COMMENT ON the `postgres` database; the store path,
    not a bare version string, is the comparison key -- see
    docs/decisions/0003 for why (storage choice, and the nixpkgs#245360
    incident that ruled out a version string).

    Found by pattern within the comment, not by assuming the comment is
    entirely ours -- see set_glibc_stamp()."""
    comment = _database_comment(conn, "postgres")
    if comment is None:
        return None
    match = _STAMP_LINE_RE.search(comment)
    return match.group(1) if match else None


def set_glibc_stamp(conn: psycopg.Connection, glibc_locales_path: str) -> None:
    """Advances the stamp without disturbing anything else already in
    the comment: if something else (a DBA note, another tool) left a
    comment on `postgres`, our line is appended after it on a first
    write and updated in place (not duplicated) on every write after
    that -- never prepended, never replacing what's there. Postgres has
    no native append/merge for COMMENT ON (it always sets the full
    text), so this reads the current comment first."""
    comment = _database_comment(conn, "postgres") or ""
    new_line = f"{GLIBC_STAMP_PREFIX}{glibc_locales_path}"
    if _STAMP_LINE_RE.search(comment):
        updated = _STAMP_LINE_RE.sub(new_line, comment)
    elif comment:
        updated = f"{comment}\n{new_line}"
    else:
        updated = new_line

    # COMMENT ON is a utility statement -- Postgres's grammar requires a
    # literal string here, not a bind parameter (confirmed empirically:
    # `IS %s` raises a syntax error), so the value is escaped via
    # sql.Literal instead of the usual parameterized query.
    conn.execute(sql.SQL("COMMENT ON DATABASE postgres IS {}").format(sql.Literal(updated)))
    conn.commit()


def c_utf8_databases(conn: psycopg.Connection) -> list[str]:
    """Databases using a C.* libc locale -- Postgres never records a
    version for these at all (confirmed: get_collation_actual_version()
    returns NULL for C/C.*/POSIX regardless of library version), so
    only the glibc stamp above can catch a behavior change (glibc 2.35,
    2022, changed C.UTF-8's actual behavior; see README.md)."""
    rows = conn.execute(
        """
        SELECT datname FROM pg_database
        WHERE datallowconn
          AND datlocprovider = 'c'
          AND (datcollate ILIKE 'C.%' OR datctype ILIKE 'C.%')
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def process_template0(conn: psycopg.Connection) -> str | None:
    """Refresh `template0`'s recorded collation version if stale.
    `template0` carries no user objects, so there's nothing to
    reindex -- this is refresh-only. Returns an error message if the
    refresh itself failed defensively (see `_safe_refresh`), None if no
    refresh was needed or it succeeded."""
    if not template0_collation_is_stale(conn):
        return None
    return _safe_refresh(conn, "template0", lambda: refresh_template0(conn))
