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


def _start_cluster(
    tmp_path_factory: pytest.TempPathFactory,
    label: str,
    locale: str,
    port: int,
    lockdown_conf_path: Path,
) -> Iterator[str]:
    data_dir: Path = tmp_path_factory.mktemp(f"pgdata-{label}")
    socket_dir: Path = tmp_path_factory.mktemp(f"pgsocket-{label}")

    subprocess.run(
        [
            "initdb",
            "--pgdata",
            str(data_dir),
            f"--locale={locale}",
            "--encoding=UTF8",
            "--auth=trust",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Mirrors the include_if_exists line the real NixOS module installs
    # ahead of every other rule (lib.mkBefore) -- the file doesn't exist
    # yet, so this is a no-op until a test actually writes to it via
    # LockdownManager.
    hba_path = data_dir / "pg_hba.conf"
    hba_path.write_text(f"include_if_exists {lockdown_conf_path}\n{hba_path.read_text()}")

    subprocess.run(
        [
            "pg_ctl",
            "start",
            "--pgdata",
            str(data_dir),
            "--log",
            str(data_dir / "postgres.log"),
            "--options",
            f"-p {port} -c unix_socket_directories={socket_dir} -c listen_addresses=",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    dsn = f"host={socket_dir} port={port} dbname=postgres"
    try:
        _wait_until_ready(dsn)
        yield dsn
    finally:
        subprocess.run(
            ["pg_ctl", "stop", "--pgdata", str(data_dir), "--mode=immediate"],
            check=False,
            capture_output=True,
        )


@pytest.fixture(scope="session")
def lockdown_conf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where the main test cluster's pg_hba.conf expects a
    LockdownManager-written file -- mirrors the path the real NixOS
    module points include_if_exists at in production. Session-scoped,
    same lifetime as pg_dsn's cluster; the file itself need not exist
    (include_if_exists tolerates that)."""
    return tmp_path_factory.mktemp("lockdown") / "lockdown.conf"


@pytest.fixture(scope="session")
def pg_dsn(
    tmp_path_factory: pytest.TempPathFactory, lockdown_conf_path: Path
) -> Iterator[str]:
    """The main test cluster. Initialized with a real libc locale, not
    C: template0 and `postgres` must carry a real, non-NULL
    datcollversion to exercise the Postgres-tracked mismatch path at
    all, and to be refreshable -- REFRESH COLLATION VERSION rejects any
    transition where recorded vs. actual versions disagree on
    NULL-ness (confirmed in PG16's dbcommands.c), which a C-locale
    cluster can never satisfy."""
    yield from _start_cluster(tmp_path_factory, "main", "en_US.UTF-8", _PORT, lockdown_conf_path)


@pytest.fixture(scope="session")
def c_locale_pg_dsn(
    tmp_path_factory: pytest.TempPathFactory, lockdown_conf_path: Path
) -> Iterator[str]:
    """A second, separate C-locale cluster -- needed only to reproduce
    the 'invalid collation version change' defensive-handling path.
    Once template0 itself carries a real (non-NULL) version, Postgres
    refuses to create *any* database with a differing version from it
    (confirmed: "template database "template0" has a collation
    version, but no actual collation version could be determined"), so
    that failure mode can only be reproduced via template0 in a
    cluster whose default locale is C from the start."""
    yield from _start_cluster(
        tmp_path_factory, "c-locale", "C", _PORT + 1, tmp_path_factory.mktemp("lockdown-c") / "x"
    )


@pytest.fixture(scope="session")
def c_utf8_lockdown_conf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("lockdown-cutf8") / "lockdown.conf"


@pytest.fixture(scope="session")
def c_utf8_pg_dsn(
    tmp_path_factory: pytest.TempPathFactory, c_utf8_lockdown_conf_path: Path
) -> Iterator[str]:
    """A third, separate cluster whose default locale is C.UTF-8 from
    initdb -- needed only to reproduce the glibc-stamp phase's
    self-inflicted-harm regression: `postgres` (the database) is itself
    C.UTF-8 here, so it's a real member of c_utf8_databases(), letting
    a test prove the lock/unlock wiring there doesn't kill the admin
    connection held throughout the phase, or the per-database
    connection opened for `postgres` specifically."""
    yield from _start_cluster(
        tmp_path_factory, "c-utf8", "C.UTF-8", _PORT + 2, c_utf8_lockdown_conf_path
    )


@pytest.fixture
def admin_conn(pg_dsn: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection to the cluster's default `postgres` database --
    for CREATE/DROP DATABASE, which can't run inside a transaction block."""
    with psycopg.connect(pg_dsn, autocommit=True, prepare_threshold=None) as conn:
        yield conn


@pytest.fixture
def c_locale_admin_conn(c_locale_pg_dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(c_locale_pg_dsn, autocommit=True, prepare_threshold=None) as conn:
        yield conn
