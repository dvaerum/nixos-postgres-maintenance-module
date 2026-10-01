"""Fast tier: LockdownManager -- the single-writer thread that owns the
pg_hba.conf include file and all lock/unlock SQL, so the existing
ThreadPoolExecutor workers never race on either. See
docs/decisions/0007 for why pg_hba.conf (not ALTER DATABASE ...
CONNECTION LIMIT) and why a single writer thread.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from collation_guard.lockdown import LockdownManager


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
