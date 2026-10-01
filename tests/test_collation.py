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
    c_utf8_databases,
    connectable_databases,
    database_collation_is_stale,
    glibc_stamp,
    process_database,
    process_template0,
    set_glibc_stamp,
    template0_collation_is_stale,
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


def test_template0_refresh_without_connecting_to_it(admin_conn: psycopg.Connection) -> None:
    """template0 disallows direct connections (datallowconn = false);
    the stale-check and refresh both have to run from a connection to
    a different database entirely -- here, the admin connection to
    `postgres`. The test cluster is initialized with a real libc locale
    (see conftest.py), so template0 carries a genuine non-NULL
    datcollversion -- faking it to a different non-NULL string is a
    realistic "glibc was upgraded" drift, unlike a NULL<->non-NULL
    transition, which Postgres itself refuses to refresh. Cycle 5 of
    the TDD sequence."""
    admin_conn.execute(
        "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
        "WHERE datname = 'template0'"
    )
    assert template0_collation_is_stale(admin_conn)

    error = process_template0(admin_conn)

    assert error is None
    assert not template0_collation_is_stale(admin_conn)


def test_process_template0_is_noop_when_not_stale(admin_conn: psycopg.Connection) -> None:
    assert not template0_collation_is_stale(admin_conn)
    assert process_template0(admin_conn) is None


def test_process_template0_refresh_failure_is_reported_defensively(
    c_locale_admin_conn: psycopg.Connection,
) -> None:
    """Real reproduction of Postgres's 'invalid collation version
    change' guard: on a C-locale cluster, template0's actual collation
    version is always NULL, so faking a non-NULL recorded version
    creates a NULL-vs-non-NULL mismatch that REFRESH COLLATION VERSION
    rejects outright. process_template0 must catch this, not crash,
    and report it rather than silently losing the failure."""
    c_locale_admin_conn.execute(
        "UPDATE pg_database SET datcollversion = 'not-the-real-version' "
        "WHERE datname = 'template0'"
    )
    assert template0_collation_is_stale(c_locale_admin_conn)

    try:
        error = process_template0(c_locale_admin_conn)

        assert error is not None
        assert "invalid collation version change" in error
        # the failed REFRESH must roll back cleanly, not leave the
        # recorded version in a half-updated state
        assert template0_collation_is_stale(c_locale_admin_conn)
    finally:
        # c_locale_pg_dsn is a session-scoped, shared cluster -- reset
        # the corruption injected above so later tests in this session
        # see a pristine template0 again, not whatever state this test
        # happened to leave behind.
        c_locale_admin_conn.execute(
            "UPDATE pg_database SET datcollversion = NULL WHERE datname = 'template0'"
        )


def test_glibc_stamp_is_none_when_never_recorded(admin_conn: psycopg.Connection) -> None:
    assert glibc_stamp(admin_conn) is None


def test_set_glibc_stamp_round_trips(admin_conn: psycopg.Connection) -> None:
    set_glibc_stamp(admin_conn, "/nix/store/abc123-glibc-locales-2.42")
    assert glibc_stamp(admin_conn) == "/nix/store/abc123-glibc-locales-2.42"

    # advancing it again must replace, not append
    set_glibc_stamp(admin_conn, "/nix/store/def456-glibc-locales-2.42")
    assert glibc_stamp(admin_conn) == "/nix/store/def456-glibc-locales-2.42"


def test_c_utf8_databases_flags_only_c_dot_locales(
    c_locale_admin_conn: psycopg.Connection,
) -> None:
    # Needs the dedicated C-locale cluster: the main cluster's template0
    # carries a real (non-NULL) version (see conftest.py), and Postgres
    # refuses to create a differently-versioned database from a
    # versioned template0 (same constraint documented in
    # test_process_template0_refresh_failure_is_reported_defensively).
    conn = c_locale_admin_conn
    for name in ("cg_cutf8", "cg_libc"):
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    conn.execute(
        "CREATE DATABASE cg_cutf8 LOCALE_PROVIDER libc LOCALE 'C.UTF-8' TEMPLATE template0"
    )
    conn.execute(
        "CREATE DATABASE cg_libc LOCALE_PROVIDER libc LOCALE 'en_US.UTF-8' TEMPLATE template0"
    )
    try:
        found = c_utf8_databases(conn)
        assert "cg_cutf8" in found
        assert "cg_libc" not in found
    finally:
        conn.execute('DROP DATABASE IF EXISTS "cg_cutf8"')
        conn.execute('DROP DATABASE IF EXISTS "cg_libc"')


@pytest.fixture
def icu_test_db(c_locale_admin_conn: psycopg.Connection, c_locale_pg_dsn: str) -> Iterator[str]:
    """ICU is a genuinely different provider from every libc-based
    fixture above -- datlocprovider='i', its own independent version
    namespace (e.g. '153.128', the underlying ICU library version),
    unrelated to glibc's. Needs the dedicated C-locale cluster, same
    reason as test_c_utf8_databases_flags_only_c_dot_locales: the main
    cluster's template0 gets REFRESH COLLATION VERSION'd by
    test_template0_refresh_without_connecting_to_it earlier in this
    file, and creating a cross-provider (icu) database from that
    *refreshed* template0 then fails with a genuine, confirmed Postgres
    error ("template database 'template0' has a collation version
    mismatch") that a pristine, never-refreshed template0 (this
    cluster) doesn't hit -- confirmed empirically both ways."""
    name = "cg_test_icu"
    c_locale_admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    c_locale_admin_conn.execute(
        f"CREATE DATABASE \"{name}\" LOCALE_PROVIDER icu ICU_LOCALE 'en-US' TEMPLATE template0"
    )
    try:
        yield name
    finally:
        c_locale_admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_process_database_is_noop_when_not_stale_icu(
    c_locale_pg_dsn: str, icu_test_db: str
) -> None:
    """Proves the Postgres-tracked mismatch path works under the icu
    provider too, not just libc -- the normal case where datcollversion
    genuinely does track the real ICU library version, as opposed to
    the documented ICU-22544-style blind spot (README's 'Known
    limitations') where it doesn't. Before this test, every test in
    this file ran exclusively under libc."""
    with _connect(c_locale_pg_dsn, icu_test_db) as conn:
        assert not database_collation_is_stale(conn)
        result = process_database(conn)
        assert result.ok
        assert result.reindexed == []
        assert result.failed == []


def test_process_database_reindexes_and_refreshes_on_mismatch_icu(
    c_locale_pg_dsn: str, icu_test_db: str
) -> None:
    with _connect(c_locale_pg_dsn, icu_test_db) as conn:
        conn.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        conn.execute("CREATE INDEX widgets_name_idx ON widgets (name)")
        conn.execute("INSERT INTO widgets (name) VALUES ('alpha'), ('beta'), ('gamma')")
        conn.commit()

        _fake_stale(conn, icu_test_db)
        conn.commit()

        assert database_collation_is_stale(conn)

        result = process_database(conn)

        assert result.ok
        assert result.reindexed == ["widgets"]
        assert result.failed == []
        assert not database_collation_is_stale(conn)


def test_c_utf8_databases_excludes_icu(
    c_locale_admin_conn: psycopg.Connection, icu_test_db: str
) -> None:
    # datlocprovider='i', not 'c' -- never matches c_utf8_databases()'s
    # query regardless of its locale string.
    assert icu_test_db not in c_utf8_databases(c_locale_admin_conn)


@pytest.fixture
def posix_test_db(
    c_locale_admin_conn: psycopg.Connection, c_locale_pg_dsn: str
) -> Iterator[str]:
    """POSIX is byte-for-byte identical to C as far as Postgres's own
    versioning is concerned (datcollversion stays NULL, same as a plain
    C database) -- this project's module docstring already states this
    is true for 'C/C.*/POSIX' collectively, but nothing exercised the
    POSIX spelling specifically until now. Uses the C-locale cluster
    for the same reason icu_test_db does -- see its docstring."""
    name = "cg_test_posix"
    c_locale_admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    c_locale_admin_conn.execute(
        f"CREATE DATABASE \"{name}\" LOCALE_PROVIDER libc LOCALE 'POSIX' TEMPLATE template0"
    )
    try:
        yield name
    finally:
        c_locale_admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_process_database_is_noop_for_posix_locale(
    c_locale_pg_dsn: str, posix_test_db: str
) -> None:
    with _connect(c_locale_pg_dsn, posix_test_db) as conn:
        assert not database_collation_is_stale(conn)
        result = process_database(conn)
        assert result.ok
        assert result.reindexed == []
        assert result.failed == []


def test_c_utf8_databases_excludes_posix(
    c_locale_admin_conn: psycopg.Connection, posix_test_db: str
) -> None:
    # datcollate='POSIX' doesn't match the 'C.%' pattern -- distinct
    # from the C.UTF-8 case already covered above.
    assert posix_test_db not in c_utf8_databases(c_locale_admin_conn)


def test_connectable_databases_excludes_template0(admin_conn: psycopg.Connection) -> None:
    found = connectable_databases(admin_conn)
    assert "postgres" in found
    assert "template0" not in found
    assert "template1" in found


def test_glibc_stamp_appends_to_a_pre_existing_third_party_comment(
    admin_conn: psycopg.Connection,
) -> None:
    """A comment left by anything else (a DBA note, another tool) must
    survive -- COMMENT ON has no native append, so set_glibc_stamp()
    has to read-modify-write rather than blindly overwrite."""
    try:
        admin_conn.execute("COMMENT ON DATABASE postgres IS 'do not drop -- owned by DBA team'")

        set_glibc_stamp(admin_conn, "/nix/store/abc-glibc-locales-2.42")

        comment = admin_conn.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = 'postgres'"
        ).fetchone()[0]
        assert "do not drop -- owned by DBA team" in comment
        assert glibc_stamp(admin_conn) == "/nix/store/abc-glibc-locales-2.42"
    finally:
        # admin_conn's cluster is session-scoped and shared -- reset so
        # later tests (e.g. asserting glibc_stamp() is None when never
        # recorded) see a pristine postgres database again.
        admin_conn.execute("COMMENT ON DATABASE postgres IS NULL")


def test_glibc_stamp_updates_in_place_without_duplicating_on_second_write(
    admin_conn: psycopg.Connection,
) -> None:
    try:
        admin_conn.execute("COMMENT ON DATABASE postgres IS 'do not drop -- owned by DBA team'")
        set_glibc_stamp(admin_conn, "/nix/store/abc-glibc-locales-2.42")

        set_glibc_stamp(admin_conn, "/nix/store/def-glibc-locales-2.43")

        comment = admin_conn.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = 'postgres'"
        ).fetchone()[0]
        assert "do not drop -- owned by DBA team" in comment
        assert comment.count("collation-guard:glibcLocales=") == 1
        assert glibc_stamp(admin_conn) == "/nix/store/def-glibc-locales-2.43"
    finally:
        admin_conn.execute("COMMENT ON DATABASE postgres IS NULL")
