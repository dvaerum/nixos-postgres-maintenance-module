"""Fast tier: ConnectionLimitLockdownManager -- the PostgreSQL < 16
fallback for connectionLockdown (docs/decisions/0009), used instead of
LockdownManager's pg_hba.conf mechanism when the configured
services.postgresql.package predates PostgreSQL 16's include_if_exists
directive. Same single-writer-thread shape as LockdownManager (see its
own tests in test_lockdown.py); this file only covers what's actually
different: ALTER DATABASE ... CONNECTION LIMIT instead of a pg_hba.conf
rule, the documented superuser-bypass gap, and the on-disk state file
that makes crash recovery possible without a running manager.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from collation_guard import lockdown
from collation_guard.lockdown import ConnectionLimitLockdownManager, _LockRequest
from conftest import _host_port


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "connection-limit-lockdown.json"


@pytest.fixture
def manager(pg_dsn: str, state_path: Path):
    host, port = _host_port(pg_dsn)
    m = ConnectionLimitLockdownManager(host, port, str(state_path))
    try:
        yield m
    finally:
        m.stop()
        state_path.unlink(missing_ok=True)


def _can_connect(host: str, port: str, dbname: str, user: str | None = None) -> bool:
    conninfo = f"host={host} port={port} dbname={dbname}"
    if user is not None:
        conninfo += f" user={user}"
    try:
        with psycopg.connect(conninfo, prepare_threshold=None):
            return True
    except psycopg.OperationalError:
        return False


def test_lock_then_unlock_a_single_database(
    manager: ConnectionLimitLockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_single"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role")
    admin_conn.execute("CREATE ROLE cg_cl_lockdown_test_role LOGIN")
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cg_cl_lockdown_test_role')
    try:
        assert _can_connect(host, port, name, "cg_cl_lockdown_test_role")

        manager.lock(name)
        assert not _can_connect(host, port, name, "cg_cl_lockdown_test_role")
        # the manager's own connection is unaffected throughout
        assert manager._conn.execute("SELECT 1").fetchone() == (1,)

        manager.unlock(name)
        assert _can_connect(host, port, name, "cg_cl_lockdown_test_role")
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role")


def test_lock_does_not_reject_a_superuser_connection(
    manager: ConnectionLimitLockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    """The documented, real gap this fallback has relative to
    LockdownManager's pg_hba.conf mechanism (see docs/decisions/0009 and
    0007's own CONNECTION LIMIT notes): CONNECTION LIMIT is never
    enforced against superuser connections
    (https://www.postgresql.org/docs/17/sql-createdatabase.html).
    Proven directly, not just asserted in a docstring -- a new
    superuser connection must still succeed while a non-superuser one
    is rejected against the exact same locked database."""
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_superuser_gap"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role2")
    admin_conn.execute("CREATE ROLE cg_cl_lockdown_test_role2 LOGIN")
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cg_cl_lockdown_test_role2')
    try:
        manager.lock(name)
        assert not _can_connect(host, port, name, "cg_cl_lockdown_test_role2")
        # admin_conn's own session connects with no explicit user --
        # the initdb bootstrap superuser (trust auth) -- so a *new*
        # superuser connection attempt is the right vehicle to prove
        # the gap, not just reuse of an already-open session.
        assert _can_connect(host, port, name)
    finally:
        manager.unlock(name)
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role2")


def test_unlock_restores_the_original_connection_limit_not_unlimited(
    manager: ConnectionLimitLockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    """A deployment's own pre-existing, deliberately-set CONNECTION
    LIMIT must survive a lock/unlock cycle unchanged -- unlock() must
    restore the recorded original value, not hardcode -1 (unlimited)."""
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_custom_limit"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute(f'ALTER DATABASE "{name}" CONNECTION LIMIT 7')
    try:
        manager.lock(name)
        (limit_while_locked,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        assert limit_while_locked == 0

        manager.unlock(name)
        (limit_after_unlock,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        assert limit_after_unlock == 7
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_lock_terminates_existing_sessions_but_excludes_the_guards_own(
    manager: ConnectionLimitLockdownManager, pg_dsn: str, admin_conn: psycopg.Connection
) -> None:
    """Same termination-sweep requirement as LockdownManager (see its
    own test of the same name in test_lockdown.py and
    docs/decisions/0007's "application_name exclusion" section) --
    CONNECTION LIMIT alone only blocks *new* connection attempts, so
    an already-open session must be explicitly terminated too."""
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_termination"
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


def test_lock_then_unlock_removes_the_state_file(
    manager: ConnectionLimitLockdownManager,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    state_path: Path,
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_state_file"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    try:
        assert not state_path.exists()
        manager.lock(name)
        assert json.loads(state_path.read_text()) == {name: -1}

        manager.unlock(name)
        # every database ended unlocked -- the only well-formed resting
        # state for the file (absent, not an empty JSON object).
        assert not state_path.exists()
    finally:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_concurrent_lock_unlock_from_multiple_threads_is_race_free(
    manager: ConnectionLimitLockdownManager,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    state_path: Path,
) -> None:
    """Non-superuser throughout -- a superuser connection attempt would
    pass regardless of lock state (see
    test_lock_does_not_reject_a_superuser_connection), which would mask
    the very race this test exists to catch."""
    host, port = _host_port(pg_dsn)
    names = [f"cg_cl_lockdown_test_concurrent_{i}" for i in range(8)]
    admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_concurrent")
    admin_conn.execute("CREATE ROLE cg_cl_lockdown_test_role_concurrent LOGIN")
    for name in names:
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute(f'CREATE DATABASE "{name}"')
        admin_conn.execute(
            f'GRANT CONNECT ON DATABASE "{name}" TO cg_cl_lockdown_test_role_concurrent'
        )
    barrier = threading.Barrier(len(names))

    def worker(name: str) -> None:
        for _ in range(3):
            barrier.wait()
            manager.lock(name)
            assert not _can_connect(host, port, name, "cg_cl_lockdown_test_role_concurrent")
            manager.unlock(name)
            assert _can_connect(host, port, name, "cg_cl_lockdown_test_role_concurrent")

    try:
        with ThreadPoolExecutor(max_workers=len(names)) as executor:
            futures = [executor.submit(worker, name) for name in names]
            for f in futures:
                f.result()

        assert not state_path.exists()
    finally:
        for name in names:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_concurrent")


def test_stop_drains_in_flight_requests_before_closing(
    manager: ConnectionLimitLockdownManager,
    pg_dsn: str,
    admin_conn: psycopg.Connection,
    state_path: Path,
) -> None:
    host, port = _host_port(pg_dsn)
    name = "cg_cl_lockdown_test_stop_drain"
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_stop_drain")
    admin_conn.execute("CREATE ROLE cg_cl_lockdown_test_role_stop_drain LOGIN")
    admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cg_cl_lockdown_test_role_stop_drain')
    requests = [_LockRequest(name, "lock") for _ in range(3)]
    try:
        for request in requests:
            manager._queue.put(request)
        manager.stop()

        assert all(request.done.is_set() for request in requests)
        assert not manager._thread.is_alive()
        # non-superuser throughout -- see concurrent test above
        assert not _can_connect(host, port, name, "cg_cl_lockdown_test_role_stop_drain")
    finally:
        state_path.unlink(missing_ok=True)
        admin_conn.execute(f'ALTER DATABASE "{name}" CONNECTION LIMIT -1')
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_stop_drain")


def test_concurrent_submit_survives_a_mid_stream_failure_without_hanging(
    manager: ConnectionLimitLockdownManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same TOCTOU race as LockdownManager's own test of the same name
    (test_lockdown.py) -- shared _submit()/_run() shape, same risk."""
    real_write_state = ConnectionLimitLockdownManager._write_state
    call_count = {"n": 0}

    def flaky_write_state(self: ConnectionLimitLockdownManager) -> None:
        call_count["n"] += 1
        if call_count["n"] == 10:
            raise RuntimeError("injected failure")
        real_write_state(self)

    monkeypatch.setattr(ConnectionLimitLockdownManager, "_write_state", flaky_write_state)
    names = [f"cg_cl_lockdown_test_race_{i}" for i in range(16)]

    def worker(name: str) -> None:
        try:
            manager.lock(name)
            manager.unlock(name)
        except Exception:
            pass  # a raised error is fine here -- a hang is not

    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        futures = [executor.submit(worker, name) for name in names]
        for f in futures:
            f.result(timeout=10)  # TimeoutError here = a hang = a real failure


def test_cleanup_connection_limit_lockdown_restores_from_a_crash(
    pg_dsn: str, admin_conn: psycopg.Connection, state_path: Path
) -> None:
    """Simulates a crash mid-lock: a state file on disk recording two
    locked databases' original limits, with no running manager at all
    (it's dead by the time --on-failure runs, or never existed in this
    process) -- cleanup_connection_limit_lockdown() must restore both
    independently of any LockdownManager-equivalent object."""
    host, port = _host_port(pg_dsn)
    name_a, name_b = "cg_cl_lockdown_test_crash_a", "cg_cl_lockdown_test_crash_b"
    admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_crash")
    admin_conn.execute("CREATE ROLE cg_cl_lockdown_test_role_crash LOGIN")
    for name, _limit in ((name_a, -1), (name_b, 3)):
        admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute(f'CREATE DATABASE "{name}"')
        admin_conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO cg_cl_lockdown_test_role_crash')
        admin_conn.execute(f'ALTER DATABASE "{name}" CONNECTION LIMIT 0')
    state_path.write_text(json.dumps({name_a: -1, name_b: 3}))
    try:
        # sanity check: the simulated crash really did leave both locked
        # -- queried directly (not via a connection attempt), since a
        # superuser connection would bypass CONNECTION LIMIT entirely
        # and prove nothing (see test_lock_does_not_reject_a_superuser_
        # connection above).
        (limit_a_before,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name_a,)
        ).fetchone()
        (limit_b_before,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name_b,)
        ).fetchone()
        assert limit_a_before == 0
        assert limit_b_before == 0

        lockdown.cleanup_connection_limit_lockdown(host, port, str(state_path))

        assert not state_path.exists()
        (limit_a,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name_a,)
        ).fetchone()
        (limit_b,) = admin_conn.execute(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s", (name_b,)
        ).fetchone()
        assert limit_a == -1
        assert limit_b == 3
        assert _can_connect(host, port, name_a, "cg_cl_lockdown_test_role_crash")
        assert _can_connect(host, port, name_b, "cg_cl_lockdown_test_role_crash")
    finally:
        state_path.unlink(missing_ok=True)
        for name in (name_a, name_b):
            admin_conn.execute(f'ALTER DATABASE "{name}" CONNECTION LIMIT -1')
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin_conn.execute("DROP ROLE IF EXISTS cg_cl_lockdown_test_role_crash")


def test_cleanup_connection_limit_lockdown_is_a_no_op_when_no_file_exists(state_path: Path) -> None:
    assert not state_path.exists()
    lockdown.cleanup_connection_limit_lockdown("unused", "unused", str(state_path))
    assert not state_path.exists()


def test_cleanup_connection_limit_lockdown_leaves_the_file_when_postgres_is_unreachable(
    state_path: Path,
) -> None:
    """Deliberately asymmetric with cleanup_lockdown_file(), which
    removes its file unconditionally before even attempting to
    reconnect (see docs/decisions/0009 and cleanup_lockdown_file's own
    docstring for why that's safe there): here, the dangerous state (a
    database's live CONNECTION LIMIT) lives inside the cluster, not on
    disk, so the state file must survive a failed connection attempt --
    deleting it first would permanently lose the only record of what
    to restore, with nothing left to retry against."""
    state_path.write_text(json.dumps({"some_db": -1}))

    with pytest.raises(psycopg.OperationalError):
        lockdown.cleanup_connection_limit_lockdown("127.0.0.1", "1", str(state_path))

    assert state_path.exists()
    assert json.loads(state_path.read_text()) == {"some_db": -1}
