"""Fast tier: LockdownManager -- the single-writer thread that owns the
pg_hba.conf include file and all lock/unlock SQL, so the existing
ThreadPoolExecutor workers never race on either. See
docs/decisions/0007 for why pg_hba.conf (not ALTER DATABASE ...
CONNECTION LIMIT) and why a single writer thread.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from collation_guard import lockdown
from collation_guard.lockdown import LockdownManager, _LockRequest


def _host_port(pg_dsn: str) -> tuple[str, str]:
    parts = dict(item.split("=", 1) for item in pg_dsn.split())
    return parts["host"], parts["port"]


@pytest.fixture
def manager(pg_dsn: str, lockdown_conf_path: Path):
    host, port = _host_port(pg_dsn)
    m = LockdownManager(host, port, str(lockdown_conf_path))
    try:
        yield m
    finally:
        m.stop()
        lockdown_conf_path.unlink(missing_ok=True)


def _can_connect(host: str, port: str, dbname: str, user: str | None = None) -> bool:
    conninfo = f"host={host} port={port} dbname={dbname}"
    if user is not None:
        conninfo += f" user={user}"
    try:
        with psycopg.connect(conninfo, prepare_threshold=None):
            return True
    except psycopg.OperationalError:
        return False


def test_lock_two_databases_unlock_one_leaves_the_other_locked(
    manager: LockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    host, port = _host_port(pg_dsn)
    name_a, name_b = "cg_lockdown_test_dba", "cg_lockdown_test_dbb"
    for name in (name_a, name_b):
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute("DROP ROLE IF EXISTS cg_lockdown_test_role2")
    admin_conn.execute("CREATE ROLE cg_lockdown_test_role2 LOGIN")
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name_a}" TO cg_lockdown_test_role2')
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name_b}" TO cg_lockdown_test_role2')
    admin_conn.execute("GRANT CONNECT ON DATABASE postgres TO cg_lockdown_test_role2")
    try:
        manager.lock(name_a)
        manager.lock(name_b)
        assert not _can_connect(host, port, name_a, "cg_lockdown_test_role2")
        assert not _can_connect(host, port, name_b, "cg_lockdown_test_role2")
        # unrelated databases are never touched by the lockdown file
        assert _can_connect(host, port, "postgres", "cg_lockdown_test_role2")

        manager.unlock(name_a)
        assert _can_connect(host, port, name_a, "cg_lockdown_test_role2")
        assert not _can_connect(host, port, name_b, "cg_lockdown_test_role2")

        manager.unlock(name_b)
        assert _can_connect(host, port, name_b, "cg_lockdown_test_role2")
    finally:
        admin_conn.execute("REVOKE CONNECT ON DATABASE postgres FROM cg_lockdown_test_role2")
        for name in (name_a, name_b):
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_lockdown_test_role2")


def test_stop_drains_in_flight_requests_before_closing(
    manager: LockdownManager,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    lockdown_conf_path: Path,
) -> None:
    """Queues several requests directly (bypassing the blocking public
    lock()/unlock() API, which can't itself leave anything "in flight")
    and calls stop() immediately -- proves every request already
    queued ahead of stop()'s own sentinel is fully applied, and the
    thread has actually exited, before stop() returns."""
    host, port = _host_port(pg_dsn)
    name = "cg_lockdown_test_stop_drain"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    requests = [_LockRequest(name, "lock") for _ in range(3)]
    try:
        for request in requests:
            manager._queue.put(request)
        manager.stop()

        assert all(request.done.is_set() for request in requests)
        assert not manager._thread.is_alive()
        assert not _can_connect(host, port, name)
    finally:
        lockdown_conf_path.unlink(missing_ok=True)
        admin_conn.execute("SELECT pg_reload_conf()")
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_concurrent_lock_unlock_from_multiple_threads_is_race_free(
    manager: LockdownManager, pg_dsn: str, admin_conn: psycopg.Connection, lockdown_conf_path: Path
) -> None:
    """More callers than maxParallelDatabases would ever actually use,
    hammering lock()/unlock() for distinct databases concurrently --
    proves the single-writer-thread design eliminates the file-write
    race, not just "usually works." Each worker's own lock()/unlock()
    round-trips are independently verified via real connection
    attempts (not internal state), and by the end every database is
    unlocked again -- the only well-formed resting state."""
    host, port = _host_port(pg_dsn)
    names = [f"cg_lockdown_test_concurrent_{i}" for i in range(8)]
    for name in names:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute(f'CREATE DATABASE "{name}"')
    barrier = threading.Barrier(len(names))

    def worker(name: str) -> None:
        for _ in range(3):
            barrier.wait()
            manager.lock(name)
            assert not _can_connect(host, port, name)
            manager.unlock(name)
            assert _can_connect(host, port, name)

    try:
        with ThreadPoolExecutor(max_workers=len(names)) as executor:
            futures = [executor.submit(worker, name) for name in names]
            for f in futures:
                f.result()

        # every database ended unlocked -- the only well-formed resting
        # state for the file (absent, not an empty/stale reject line).
        assert not lockdown_conf_path.exists()
    finally:
        for name in names:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_lock_terminates_existing_sessions_but_excludes_the_guards_own(
    manager: LockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    """The regression test for the exact self-inflicted-harm bug caught
    during design review: a naive pid <> pg_backend_pid() exclusion
    would only spare the one connection issuing the terminate query,
    killing any OTHER guard-owned connection to the same database
    (e.g. postgres itself, when it happens to be a C.UTF-8 database and
    the guard holds 2-3 simultaneous connections to it)."""
    host, port = _host_port(pg_dsn)
    name = "cg_lockdown_test_termination"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    try:
        tagged = psycopg.connect(
            f"host={host} port={port} dbname={name} application_name=collation-guard",
            prepare_threshold=None,
        )
        untagged = psycopg.connect(f"host={host} port={port} dbname={name}", prepare_threshold=None)
        try:
            manager.lock(name)

            assert tagged.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(psycopg.OperationalError):
                untagged.execute("SELECT 1")
        finally:
            tagged.close()
            untagged.close()
    finally:
        manager.unlock(name)
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_lock_raises_instead_of_hanging_when_reload_confirmation_fails(
    manager: LockdownManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lockdown,
        "_reload_and_confirm",
        lambda conn: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    request = _LockRequest("some_db", "lock")
    manager._queue.put(request)
    assert request.done.wait(timeout=5), "manager thread hung instead of signaling failure"

    # poisoned: a later request fails fast, doesn't touch the dead thread
    with pytest.raises(RuntimeError):
        manager.lock("another_db")


def test_lock_then_unlock_a_single_database(
    manager: LockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_lockdown_test_single"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute("CREATE ROLE cg_lockdown_test_role LOGIN")
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cg_lockdown_test_role')
    try:
        # before lock(): a plain, non-superuser connection succeeds
        with psycopg.connect(
            f"host={host} port={port} dbname={name} user=cg_lockdown_test_role",
            prepare_threshold=None,
        ):
            pass

        manager.lock(name)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                f"host={host} port={port} dbname={name} user=cg_lockdown_test_role",
                prepare_threshold=None,
            )
        # the manager's own connection is unaffected throughout
        row = manager._conn.execute("SELECT 1").fetchone()
        assert row == (1,)

        manager.unlock(name)
        with psycopg.connect(
            f"host={host} port={port} dbname={name} user=cg_lockdown_test_role",
            prepare_threshold=None,
        ):
            pass
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_lockdown_test_role")
