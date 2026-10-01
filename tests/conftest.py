"""Fast-tier fixture: a real, ephemeral PostgreSQL cluster (initdb +
pg_ctl in a tmpdir), no systemd/containers/VM at all. Session-scoped --
starting a cluster is expensive, creating/dropping individual databases
within it is cheap, so tests share one cluster and get their own fresh
database each.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_PORT = 5432  # fixed is fine: the socket lives in a per-session tmpdir,
# so the socket *path* (not the port number) is what's actually unique --
# no TCP listener is opened at all (listen_addresses=''), so there's no
# real port to collide on either.


def _wait_until_ready(dsn: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=1):
                return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.1)
    raise TimeoutError(f"postgres did not become ready within {timeout}s") from last_error


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    data_dir: Path = tmp_path_factory.mktemp("pgdata")
    socket_dir: Path = tmp_path_factory.mktemp("pgsocket")

    subprocess.run(
        [
            "initdb",
            "--pgdata",
            str(data_dir),
            "--locale=C",
            "--encoding=UTF8",
            "--auth=trust",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        [
            "pg_ctl",
            "start",
            "--pgdata",
            str(data_dir),
            "--log",
            str(data_dir / "postgres.log"),
            "--options",
            f"-p {_PORT} -c unix_socket_directories={socket_dir} -c listen_addresses=",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    dsn = f"host={socket_dir} port={_PORT} dbname=postgres"
    try:
        _wait_until_ready(dsn)
        yield dsn
    finally:
        subprocess.run(
            ["pg_ctl", "stop", "--pgdata", str(data_dir), "--mode=immediate"],
            check=False,
            capture_output=True,
        )


@pytest.fixture
def admin_conn(pg_dsn: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection to the cluster's default `postgres` database --
    for CREATE/DROP DATABASE, which can't run inside a transaction block."""
    with psycopg.connect(pg_dsn, autocommit=True, prepare_threshold=None) as conn:
        yield conn
