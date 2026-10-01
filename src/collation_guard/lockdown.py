"""Connection lockdown during repair: reject new connections to a
database while it's being reindexed/repaired, for exactly the
databases/duration that need it.

One dedicated thread (LockdownManager) is the sole owner of the
pg_hba.conf include file and all lock/unlock SQL -- the existing
ThreadPoolExecutor workers (in main.py) never touch either directly,
they send lock()/unlock() requests over a queue instead. This is what
lets "one file, one crash-cleanup action" hold regardless of how many
databases are locked at any moment, with no write races across
threads. See docs/decisions/0007 for the full design, including why
pg_hba.conf was chosen over ALTER DATABASE ... CONNECTION LIMIT.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field

import psycopg


@dataclass(frozen=True, slots=True)
class _LockRequest:
    dbname: str
    action: str  # "lock" or "unlock"
    done: threading.Event = field(default_factory=threading.Event)


class LockdownManager:
    """lock()/unlock() block the calling thread until genuinely
    applied (file written, reloaded, and -- for lock() -- any other
    existing session on that database terminated), so a caller never
    starts mutating a database before it's actually locked."""

    def __init__(self, host: str, port: str, lockdown_path: str) -> None:
        self._lockdown_path = lockdown_path
        self._locked: set[str] = set()
        self._queue: queue.Queue[_LockRequest | None] = queue.Queue()
        # One dedicated connection, used only by this thread -- tagged
        # the same as every other guard connection so the termination
        # sweep below can tell it apart from a connection this manager
        # should actually terminate.
        self._conn = psycopg.connect(
            f"host={host} port={port} dbname=postgres application_name=collation-guard",
            autocommit=True,
            prepare_threshold=None,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def lock(self, dbname: str) -> None:
        self._submit(dbname, "lock")

    def unlock(self, dbname: str) -> None:
        self._submit(dbname, "unlock")

    def stop(self) -> None:
        """Drains any requests already queued before closing -- a
        request submitted strictly before stop() is guaranteed to be
        fully applied before this returns, since the sentinel is
        processed strictly after everything queued ahead of it."""
        self._queue.put(None)
        self._thread.join()
        self._conn.close()

    def _submit(self, dbname: str, action: str) -> None:
        request = _LockRequest(dbname, action)
        self._queue.put(request)
        request.done.wait()

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                break
            if request.action == "lock":
                self._locked.add(request.dbname)
            else:
                self._locked.discard(request.dbname)
            self._write_file()
            self._conn.execute("SELECT pg_reload_conf()")
            if request.action == "lock":
                self._conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND application_name <> 'collation-guard'",
                    (request.dbname,),
                )
            request.done.set()

    def _write_file(self) -> None:
        if not self._locked:
            try:
                os.remove(self._lockdown_path)
            except FileNotFoundError:
                pass
            return
        dblist = ",".join(sorted(self._locked))
        with open(self._lockdown_path, "w") as f:
            f.write(f"local   {dblist}   all                reject\n")
            f.write(f"host    {dblist}   all   0.0.0.0/0    reject\n")
            f.write(f"host    {dblist}   all   ::/0         reject\n")


class NullLockdownManager:
    """services.postgresqlCollationGuard.connectionLockdown.enable =
    false -- every call a no-op, so main.py never needs to branch on
    whether lockdown is enabled."""

    def lock(self, dbname: str) -> None:
        pass

    def unlock(self, dbname: str) -> None:
        pass

    def stop(self) -> None:
        pass
