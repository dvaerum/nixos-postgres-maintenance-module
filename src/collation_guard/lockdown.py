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

import json
import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

_RELOAD_CONFIRM_TIMEOUT = 2.0


def _hba_quote(name: str) -> str:
    """Quotes one database name for pg_hba.conf's comma-separated
    database field. A database name is runtime-discovered catalog
    data (pg_database.datname), not a trusted literal, and pg_hba.conf
    treats a bare comma or whitespace as a field separator -- written
    unquoted, a name containing one would either silently evade its
    own lockdown (never matched by any token in the list) or break the
    line's parsing for every other database sharing this file.
    Double-quoting matches Postgres's own hba.c tokenizer (next_token()):
    inside a quoted field, comma/whitespace are literal, and "" is an
    escaped literal quote -- the same rule as pg_ident.conf.
    A bare newline has no representation this single-line writer can
    produce correctly, so it's rejected outright rather than silently
    mis-quoted into a line that could fail open."""
    if "\n" in name:
        raise ValueError(f"database name contains a newline, cannot be locked safely: {name!r}")
    return '"' + name.replace('"', '""') + '"'


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
        self._error: Exception | None = None
        # Guards self._error together with the queue put/drain that
        # must stay atomic with it -- see _submit()/_run()'s own
        # comments for the exact race this closes.
        self._state_lock = threading.Lock()
        # One dedicated connection, used only by this thread -- tagged
        # the same as every other guard connection so the termination
        # sweep below can tell it apart from a connection this manager
        # should actually terminate.
        self._conn = psycopg.connect(
            host=host,
            port=port,
            dbname="postgres",
            application_name="collation-guard",
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
        # Checking self._error and enqueuing must be one atomic step:
        # without the lock, a caller could pass the check just before
        # a failure, then not actually enqueue until AFTER _run()'s
        # except-branch has already drained the (then-empty) queue and
        # exited -- orphaning the request forever (done.wait() below
        # has no timeout). Whichever of this or _run()'s except-branch
        # acquires the lock first is guaranteed to fully happen before
        # the other: either the enqueue lands before the drain (so the
        # drain finds and releases it), or the error is set and the
        # drain completes before this check runs (so the check catches
        # it and never enqueues at all).
        with self._state_lock:
            if self._error is not None:
                raise self._error
            request = _LockRequest(dbname, action)
            self._queue.put(request)
        request.done.wait()
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                break
            try:
                if request.action == "lock":
                    self._locked.add(request.dbname)
                else:
                    self._locked.discard(request.dbname)
                self._write_file()
                _reload_and_confirm(self._conn)
                if request.action == "lock":
                    # pg_terminate_backend:
                    # https://www.postgresql.org/docs/17/functions-admin.html
                    # pg_stat_activity columns:
                    # https://www.postgresql.org/docs/17/monitoring-stats.html
                    self._conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND application_name <> 'collation-guard'",
                        (request.dbname,),
                    )
            except Exception as exc:
                with self._state_lock:
                    self._error = exc
                    self._drain_with_error()
                request.done.set()
                return
            request.done.set()

    def _drain_with_error(self) -> None:
        """Called once this thread is dead (about to `return` from
        _run() after an unrecoverable failure) -- unblocks every other
        caller already waiting in _submit() on a request still sitting
        in the queue, since nothing is left to dequeue them."""
        while True:
            try:
                queued = self._queue.get_nowait()
            except queue.Empty:
                return
            if queued is not None:
                queued.done.set()

    def _write_file(self) -> None:
        if not self._locked:
            try:
                os.remove(self._lockdown_path)
            except FileNotFoundError:
                pass
            return
        dblist = ",".join(_hba_quote(name) for name in sorted(self._locked))
        with open(self._lockdown_path, "w") as f:
            f.write(f"local   {dblist}   all                reject\n")
            f.write(f"host    {dblist}   all   0.0.0.0/0    reject\n")
            f.write(f"host    {dblist}   all   ::/0         reject\n")


def _reload_and_confirm(conn: psycopg.Connection) -> None:
    """pg_reload_conf() only requests a reload (SIGHUP) -- the
    postmaster re-reads pg_hba.conf asynchronously, so a SQL call
    returning doesn't mean the new rules are in effect yet (confirmed
    empirically: a brand-new connection could occasionally still
    succeed/fail against the stale rules for a short window
    afterward). pg_conf_load_time() is the authoritative signal
    Postgres itself updates once a reload has genuinely completed --
    poll until it changes before returning.
    https://www.postgresql.org/docs/17/functions-admin.html (Server
    Signaling Functions, pg_reload_conf) and
    https://www.postgresql.org/docs/17/functions-info.html
    (pg_conf_load_time)."""
    before = conn.execute("SELECT pg_conf_load_time()").fetchone()
    conn.execute("SELECT pg_reload_conf()")
    deadline = time.monotonic() + _RELOAD_CONFIRM_TIMEOUT
    while time.monotonic() < deadline:
        after = conn.execute("SELECT pg_conf_load_time()").fetchone()
        if after != before:
            return
        time.sleep(0.001)
    raise RuntimeError(
        f"pg_hba.conf reload did not complete within {_RELOAD_CONFIRM_TIMEOUT}s "
        "(pg_conf_load_time() never advanced)"
    )


def cleanup_lockdown_file(host: str, port: str, lockdown_path: str) -> None:
    """Unconditional crash-recovery step, called from the --on-failure
    entry point independently of any LockdownManager (which is dead by
    the time this runs, or never existed in this process at all): if
    the lockdown file exists, delete it and reload -- regardless of
    which/how many databases were locked at the moment of the crash.
    A missing file is a silent no-op, not an error -- the common case,
    since most crashes happen with nothing locked at all."""
    try:
        os.remove(lockdown_path)
    except FileNotFoundError:
        return
    with psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        application_name="collation-guard",
        autocommit=True,
        prepare_threshold=None,
    ) as conn:
        _reload_and_confirm(conn)


class ConnectionLimitLockdownManager:
    """Fallback connectionLockdown mechanism for PostgreSQL < 16, which
    predates pg_hba.conf's include_if_exists directive that
    LockdownManager depends on (see docs/decisions/0009). Locks a
    database via `ALTER DATABASE ... CONNECTION LIMIT 0` instead of a
    pg_hba.conf rule -- takes effect immediately on statement commit
    (no pg_reload_conf()/pg_conf_load_time() confirmation dance needed,
    unlike LockdownManager, since there's no asynchronous config
    reload in between). Same single-writer-thread shape as
    LockdownManager for the same race-free guarantee (see its own
    docstring and docs/decisions/0007's "Single-writer thread, not
    per-worker file access").

    Weaker than LockdownManager by one documented margin: CONNECTION
    LIMIT is not enforced against superuser connections
    (https://www.postgresql.org/docs/17/sql-createdatabase.html,
    CONNECTION LIMIT notes) -- a real, standing gap on PostgreSQL < 16,
    not a bug. See docs/decisions/0009 for why this is still worth
    having over no protection at all."""

    def __init__(self, host: str, port: str, state_path: str) -> None:
        self._state_path = state_path
        # dbname -> the CONNECTION LIMIT it had before this run ever
        # locked it -- restored verbatim on unlock() rather than a
        # hardcoded -1, so a deployment's own pre-existing custom limit
        # (if any) survives a lock/unlock cycle unchanged.
        self._original_limits: dict[str, int] = {}
        self._queue: queue.Queue[_LockRequest | None] = queue.Queue()
        self._error: Exception | None = None
        self._state_lock = threading.Lock()
        self._conn = psycopg.connect(
            host=host,
            port=port,
            dbname="postgres",
            application_name="collation-guard",
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
        self._queue.put(None)
        self._thread.join()
        self._conn.close()

    # Identical shape to LockdownManager._submit()/_run()/
    # _drain_with_error() -- same atomicity requirement (checking
    # self._error and enqueuing must be one step), same reasoning, not
    # repeated here.
    def _submit(self, dbname: str, action: str) -> None:
        with self._state_lock:
            if self._error is not None:
                raise self._error
            request = _LockRequest(dbname, action)
            self._queue.put(request)
        request.done.wait()
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                break
            try:
                if request.action == "lock":
                    self._lock_one(request.dbname)
                else:
                    self._unlock_one(request.dbname)
            except Exception as exc:
                with self._state_lock:
                    self._error = exc
                    self._drain_with_error()
                request.done.set()
                return
            request.done.set()

    def _drain_with_error(self) -> None:
        while True:
            try:
                queued = self._queue.get_nowait()
            except queue.Empty:
                return
            if queued is not None:
                queued.done.set()

    def _lock_one(self, dbname: str) -> None:
        if dbname not in self._original_limits:
            # Recorded (and persisted) *before* the ALTER below changes
            # the live value -- a crash between these two steps must
            # never lose track of what to restore.
            row = self._conn.execute(
                "SELECT datconnlimit FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            # Only reachable for a dbname the caller just connected to
            # (see docs/decisions/0007's "connect first, lock second"
            # ordering) -- a missing row would mean it was dropped in
            # the instant between that connection and this lock().
            assert row is not None, f"pg_database has no row for {dbname!r}"
            self._original_limits[dbname] = row[0]
            self._write_state()
        self._conn.execute(
            sql.SQL("ALTER DATABASE {} CONNECTION LIMIT 0").format(sql.Identifier(dbname))
        )
        # pg_terminate_backend/pg_stat_activity, application_name
        # exclusion: identical reasoning to LockdownManager._run()'s
        # own termination sweep (docs/decisions/0007's
        # "application_name exclusion" section) -- not repeated here.
        self._conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND application_name <> 'collation-guard'",
            (dbname,),
        )

    def _unlock_one(self, dbname: str) -> None:
        original = self._original_limits.pop(dbname, -1)
        self._conn.execute(
            sql.SQL("ALTER DATABASE {} CONNECTION LIMIT {}").format(
                sql.Identifier(dbname), sql.Literal(original)
            )
        )
        self._write_state()

    def _write_state(self) -> None:
        if not self._original_limits:
            try:
                os.remove(self._state_path)
            except FileNotFoundError:
                pass
            return
        # Atomic write (temp file + os.replace, same directory as the
        # target -- /run is tmpfs, so always the same filesystem):
        # unlike LockdownManager's pg_hba.conf line writer, where a
        # torn write is just an inert extra rule Postgres parses
        # permissively, this file is JSON this project's own
        # cleanup_connection_limit_lockdown() must successfully parse
        # to know what to restore -- a write killed mid-flight must
        # never leave a corrupt file behind.
        state_dir = os.path.dirname(self._state_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".connection-limit-lockdown-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._original_limits, f)
            os.replace(tmp_path, self._state_path)
        except BaseException:
            os.remove(tmp_path)
            raise


def cleanup_connection_limit_lockdown(host: str, port: str, state_path: str) -> None:
    """Unconditional crash-recovery step for the
    ConnectionLimitLockdownManager fallback (see docs/decisions/0009),
    mirroring cleanup_lockdown_file()'s role for the pg_hba.conf
    mechanism: restore every database recorded in the state file to
    its original CONNECTION LIMIT. A missing file is a silent no-op,
    same as cleanup_lockdown_file() -- the common case, since most
    crashes happen with nothing locked at all.

    Deliberately does NOT delete the state file before (or instead of)
    successfully reconnecting and restoring every entry, unlike
    cleanup_lockdown_file()'s own unconditional-delete-first order:
    that file's mere presence changes how Postgres itself behaves on
    its next read of pg_hba.conf, so deleting it is itself the safety
    action, independent of whether the follow-up reload succeeds. Here
    the dangerous state (a database's live CONNECTION LIMIT) lives
    inside the already-running cluster, not on disk -- deleting this
    file before successfully restoring every entry would permanently
    lose the one record of what to restore it to, with nothing left to
    retry against on this unit's next invocation."""
    try:
        with open(state_path) as f:
            original_limits: dict[str, int] = json.load(f)
    except FileNotFoundError:
        return
    with psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        application_name="collation-guard",
        autocommit=True,
        prepare_threshold=None,
    ) as conn:
        for dbname, limit in original_limits.items():
            conn.execute(
                sql.SQL("ALTER DATABASE {} CONNECTION LIMIT {}").format(
                    sql.Identifier(dbname), sql.Literal(limit)
                )
            )
    os.remove(state_path)


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
