"""Entry point: orchestrates collation.py + partitions.py across every
database in the cluster. Driven entirely by environment variables set
by the NixOS module (see nixosModule/config.nix) -- there is no config
file. --dry-run is the only user-facing CLI flag; --on-failure and
--upgrade are internal, invoked only by their own companion systemd
units (see run_on_failure()/run_upgrade_entrypoint() below).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

import psycopg

from . import collation, lockdown, partitions, upgrade
from .hooks import Hook, HooksConfig, load_hooks, run_hook

logger = logging.getLogger("collation_guard")

# Whichever connectionLockdown mechanism is in play (or none at all) --
# selected once in run() based on COLLATION_GUARD_LOCKDOWN_MECHANISM
# (set by the Nix module from the configured PostgreSQL version; see
# docs/decisions/0009), then threaded through unchanged everywhere a
# manager is passed around.
LockdownManagerLike = (
    lockdown.LockdownManager
    | lockdown.ConnectionLimitLockdownManager
    | lockdown.NullLockdownManager
)


@dataclass
class Failure:
    database: str
    relation: str
    error: str


@dataclass
class RunReport:
    """Outcome of one full run, across every database in the cluster --
    also the shape written to COLLATION_GUARD_CONTEXT_FILE for hooks
    (see nixosModule/config.nix) to read."""

    databases_processed: list[str] = field(default_factory=list)
    databases_repaired: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failures


def _summarize_failures(report: RunReport) -> str:
    return "; ".join(f"{f.database}.{f.relation}: {f.error}" for f in report.failures)


def _report_context(report: RunReport) -> dict[str, object]:
    """The RunReport-shape dict exposed as COLLATION_GUARD_CONTEXT on
    every global hook stage, and written to COLLATION_GUARD_CONTEXT_FILE
    -- one definition, shared by both, so the shape can't drift between
    the two."""
    return {
        "databases_processed": report.databases_processed,
        "databases_repaired": sorted(report.databases_repaired),
        "failures": [asdict(f) for f in report.failures],
        "success": report.success,
    }


def _mark_repaired(report: RunReport, dbname: str) -> None:
    """The same database can be repaired twice in one run (a collation
    reindex and a partition repair) -- dedup at the one place duplicates
    are created, rather than at every reader of databases_repaired."""
    if dbname not in report.databases_repaired:
        report.databases_repaired.append(dbname)


def _connect(host: str, port: str, dbname: str) -> psycopg.Connection:
    # dbname is a runtime-discovered, catalog-sourced identifier (see
    # collation.connectable_databases()), not a trusted literal -- it
    # must never be spliced into a conninfo *string* (libpq's own
    # tokenizer would re-interpret a crafted database name, e.g. one
    # containing `options='...'`, as extra connection parameters for
    # this superuser connection). Passing it as a keyword argument
    # instead routes it through psycopg's make_conninfo()/_param_escape(),
    # which quotes/escapes it correctly.
    return psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        application_name="collation-guard",
        prepare_threshold=None,
    )


@contextmanager
def _locked_connection(
    host: str,
    port: str,
    dbname: str,
    manager: LockdownManagerLike,
    needs_lock: Callable[[psycopg.Connection], bool],
) -> Iterator[psycopg.Connection]:
    with _connect(host, port, dbname) as conn:
        locked = needs_lock(conn)
        if locked:
            manager.lock(dbname)
        try:
            yield conn
        finally:
            if locked:
                manager.unlock(dbname)


def _apply_collation_result(
    result: collation.DatabaseResult,
    dbname: str,
    report: RunReport,
    *,
    error_suffix: str = "",
) -> bool:
    """Turns a collation.DatabaseResult into RunReport entries -- the
    one place this happens, shared by the per-database path and
    _process_glibc_stamp's own reindex call (error_suffix distinguishes
    the two in failure messages; refresh_error is always None from the
    glibc-stamp path, since reindex_all_user_tables never sets it --
    only process_database does)."""
    if result.reindexed:
        logger.info(
            "%s: reindexed %d table(s) for a Postgres-tracked collation mismatch: %s",
            dbname,
            len(result.reindexed),
            ", ".join(result.reindexed),
        )
        _mark_repaired(report, dbname)
    for table in result.failed:
        report.failures.append(
            Failure(database=dbname, relation=table, error=f"REINDEX failed{error_suffix}")
        )
    if result.refresh_error is not None:
        report.failures.append(
            Failure(database=dbname, relation=dbname, error=result.refresh_error)
        )
    return result.ok


def _apply_partition_repair(
    conn: psycopg.Connection, dbname: str, max_attempts: int, report: RunReport
) -> bool:
    ok = True
    for schema, table in partitions.partition_repair_candidates(conn):
        key_columns = partitions.partition_key_columns(conn, schema, table)
        result = partitions.repair_partition_table(conn, schema, table, key_columns, max_attempts)
        if result.repaired:
            logger.info(
                "partition repair: %s.%s.%s -- moved %d row(s) to their correct partition",
                dbname,
                schema,
                table,
                result.repaired,
            )
            _mark_repaired(report, dbname)
        for child in result.skipped_ruled_children:
            logger.warning(
                "partition repair: %s.%s.%s has a RULE attached -- skipping, no automated "
                "way to suppress a rule's side effects the way DISABLE TRIGGER USER does",
                dbname,
                schema,
                child,
            )
        if not result.ok:
            ok = False
            report.failures.append(
                Failure(
                    database=dbname,
                    relation=f"{schema}.{table}",
                    error=f"partition repair gave up after {max_attempts} attempts",
                )
            )
    return ok


def _process_database(
    host: str,
    port: str,
    dbname: str,
    partition_repair_enabled: bool,
    max_attempts: int,
    hooks: HooksConfig,
    manager: LockdownManagerLike,
    hook_timeout_sec: float,
    glibc_reindexed: bool = False,
) -> RunReport:
    """Processes one database in complete isolation, returning its own
    RunReport rather than mutating a shared one -- lets run() call this
    from multiple threads (one per database) with no locking: each
    worker only ever touches its own report, and run() merges every
    worker's result into the real report sequentially, back in the
    main thread, once every worker has finished.

    Contrast with _process_glibc_stamp: that phase runs once,
    sequentially, before the ThreadPoolExecutor starts, so it mutates
    the shared report directly -- there's no concurrent writer to race
    against. A new phase function should follow whichever convention
    matches where it actually runs, not copy this one by default."""
    report = RunReport()

    if not _run_hooks(
        hooks.per_database.pre_start,
        "database_pre_start",
        report,
        timeout_sec=hook_timeout_sec,
        database=dbname,
        context={},
    ):
        # blockOnFailure=true: skip this database entirely -- no
        # process_database, no partition repair, not added to
        # databases_processed. Other databases are unaffected.
        return report

    def needs_lock(conn: psycopg.Connection) -> bool:
        # Cheap, read-only checks decide whether this database needs
        # locking at all, reusing this project's own existing detection
        # logic rather than inventing a new one. A stale collation
        # stamp is real detected drift, but partition_repair_candidates()
        # is only an applicability filter -- an eligible table, not a
        # confirmed misplaced row (see its own docstring) -- so a
        # database can still be locked here and found clean.
        locked = collation.is_database_stale(conn)
        if partition_repair_enabled:
            locked = locked or bool(partitions.partition_repair_candidates(conn))
        return locked

    # Everything below -- the lock decision, the actual collation/
    # partition work -- runs inside a ThreadPoolExecutor worker (see
    # run()). An uncaught exception here would propagate out via
    # future.result(), crashing the whole run and silently dropping
    # every other database's already-completed work (the same failure
    # mode round 2 fixed for hook invocation specifically; this is the
    # rest of the function).
    try:
        with _locked_connection(host, port, dbname, manager, needs_lock) as conn:
            collation_result = collation.process_database(
                conn, already_reindexed=glibc_reindexed
            )
            collation_ok = _apply_collation_result(collation_result, dbname, report)
            reindexed = collation_result.reindexed
            partition_ok = True
            if partition_repair_enabled:
                partition_ok = _apply_partition_repair(conn, dbname, max_attempts, report)
        report.databases_processed.append(dbname)
        ok = collation_ok and partition_ok
    except Exception as exc:
        # Full exception text (which, for a constraint-violation error
        # from the repair UPDATE, includes Postgres's own DETAIL line --
        # real row/column values) is logged here, not put in
        # Failure.error: the latter reaches every configured hook's
        # environment, including third-party notification hooks, where
        # that data was never meant to go. The journal is this
        # project's one accepted channel for this detail (same
        # reasoning already applied to the REINDEX-failure path, which
        # only ever reports a fixed "REINDEX failed" to hooks).
        logger.warning("%s: processing raised: %s", dbname, exc, exc_info=True)
        report.failures.append(
            Failure(
                database=dbname,
                relation=dbname,
                error="processing raised an unexpected exception (see journal for details)",
            )
        )
        ok = False

    if ok:
        _run_hooks(
            hooks.per_database.on_success,
            "database_success",
            report,
            timeout_sec=hook_timeout_sec,
            database=dbname,
            context={"reindexed": reindexed},
        )
    else:
        error = "; ".join(f"{f.relation}: {f.error}" for f in report.failures)
        _run_hooks(
            hooks.per_database.on_failure,
            "database_failure",
            report,
            timeout_sec=hook_timeout_sec,
            database=dbname,
            error=error,
            context={
                "failures": [{"relation": f.relation, "error": f.error} for f in report.failures]
            },
        )

    return report


def _process_glibc_stamp(
    host: str,
    port: str,
    glibc_locales_path: str,
    report: RunReport,
    manager: LockdownManagerLike,
) -> set[str]:
    """Runs once, sequentially, before run()'s ThreadPoolExecutor
    starts -- mutates the shared `report` directly (same as run()'s
    own template0/pre_start handling), which is safe here specifically
    because nothing else is writing to it concurrently yet. Contrast
    with _process_database, which is isolated for exactly the opposite
    reason (see its own docstring).

    Returns the set of databases cleanly reindexed here -- not ones
    recorded as a failure, which still get a normal retry via
    _process_database below -- so a database that's ALSO
    independently stale via a named collation (a database can be both
    at once) isn't reindexed a second time for work this phase already
    did; see collation.process_database's already_reindexed param."""
    with _connect(host, port, "postgres") as admin_conn:
        stamp = collation.glibc_stamp(admin_conn)
        if stamp == glibc_locales_path:
            return set()

        logger.info(
            "glibc locale data changed (%s -> %s), reindexing C.UTF-8 databases",
            stamp or "not yet recorded",
            glibc_locales_path,
        )
        c_utf8_dbs = collation.c_utf8_databases(admin_conn)
        all_ok = True
        cleanly_reindexed: set[str] = set()
        for dbname in c_utf8_dbs:
            # Connect BEFORE locking, same pattern as _process_database
            # -- a new connection attempt made AFTER dbname is already
            # locked would itself be rejected by pg_hba.conf, since a
            # `reject` rule matches on database/user/address only;
            # application_name isn't known until after authentication,
            # so it can't exempt a brand-new connection the way it
            # exempts an existing session from the termination sweep.
            # This matters specifically when dbname == "postgres" (if
            # it happens to be C.UTF-8): admin_conn and this conn are
            # then two separate, already-open, application_name-tagged
            # connections to the same database being locked -- both
            # survive the termination sweep, but only because neither
            # had to be (re)established while the lock was active.
            #
            # There's no per-database staleness to check here either
            # (Postgres never versions C/C.*/POSIX at all) -- once the
            # stamp itself is stale, every member of c_utf8_dbs
            # genuinely gets reindexed.
            with _locked_connection(host, port, dbname, manager, lambda _conn: True) as conn:
                result = collation.reindex_all_user_tables(conn)
                if _apply_collation_result(
                    result, dbname, report, error_suffix=" (C.UTF-8 stamp check)"
                ):
                    cleanly_reindexed.add(dbname)
                else:
                    all_ok = False

        # Only advance the stamp once every C.UTF-8 database reindexed
        # cleanly -- otherwise the next run must retry, not silently skip.
        if all_ok:
            collation.set_glibc_stamp(admin_conn, glibc_locales_path)

        return cleanly_reindexed


# Failure.database value for a failure that isn't about any specific
# database -- a global (preStart/onSuccess/postRun) hook failing.
# template0 failures use its own literal name instead (see run()) since
# it isn't a lockable/connectable database either, but is still a
# specific, nameable thing.
GLOBAL = "(global)"


def _run_hooks(
    hooks: list[Hook],
    stage: str,
    report: RunReport,
    *,
    timeout_sec: float,
    database: str | None = None,
    context: dict[str, object] | None = None,
    error: str | None = None,
) -> bool:
    """Runs a list of hooks for one stage -- global (database=None,
    reported under GLOBAL) or per-database. Returns False if a
    blockOnFailure=true hook failed (the caller decides what that means
    -- abort now for preStart, skip-this-database for
    perDatabase.preStart, just an extra failure entry for the rest),
    True otherwise. Every failing hook is recorded, not just the first
    one, and every configured hook still runs regardless of an earlier
    one's outcome.

    A hook that RAISES (EnvironmentCollisionError, a bad path) is
    treated the same as a non-zero exit, not left to propagate -- for
    a perDatabase.* hook this runs inside a ThreadPoolExecutor worker,
    and an uncaught exception there crashes the entire run via
    future.result(), silently dropping every other database's
    already-completed work (see docs/decisions/0006's per-database
    isolation guarantee)."""
    ok = True
    for hook in hooks:
        try:
            result = run_hook(
                hook,
                stage=stage,
                timeout_sec=timeout_sec,
                database=database,
                context=context,
                error=error,
            )
        except Exception as exc:
            logger.warning(
                "%s hook %s raised for %s: %s",
                stage,
                hook.path,
                database or GLOBAL,
                exc,
                exc_info=True,
            )
            if hook.block_on_failure:
                report.failures.append(
                    Failure(
                        database=database or GLOBAL,
                        relation=stage,
                        error=f"{stage} hook {hook.path} raised: {exc}",
                    )
                )
                ok = False
            continue
        if result.ok:
            continue
        logger.warning(
            "%s hook %s failed for %s: %s", stage, hook.path, database or GLOBAL, result.stderr
        )
        if result.block_on_failure:
            report.failures.append(
                Failure(
                    database=database or GLOBAL,
                    relation=stage,
                    error=f"{stage} hook {hook.path} failed (exit {result.returncode})",
                )
            )
            ok = False
    return ok


def _cleanup_stale_lockdown_state(
    host: str,
    port: str,
    *,
    lockdown_path: str | None,
    connection_limit_state_path: str | None,
) -> None:
    """Unconditional, run-at-the-top-of-every-real-run self-heal for
    whatever a *previous* process left behind -- not just the
    OnFailure= companion unit's own cleanup (run_on_failure()). Needed
    because that companion unit only fires on a failure systemd itself
    observes (non-zero exit, crash, kill, timeout); a genuine
    machine-level crash (power loss, kernel panic) skips it entirely,
    and the very next thing to run afterward, on the next boot, is
    this guard itself (`before = ["postgresql-setup.service"]`) -- the
    one place guaranteed to run before anything else touches Postgres.

    This matters asymmetrically for the two mechanisms (docs/decisions
    0007 and 0009): the pg_hba.conf lockdown file lives on tmpfs
    (/run) and already self-heals for free on a reboot (no file ->
    include_if_exists silently no-ops, and there's no other persisted
    state to restore) -- cleaning it up here too is just defense in
    depth. The connection_limit state file's *dangerous* state
    (pg_database.datconnlimit) is real, durable Postgres catalog data
    that survives a reboot -- so its state file must ALSO survive one
    (see nixosModule/config.nix, which deliberately places it under
    /var/lib, not /run) for this function to ever have anything to
    restore after a hard crash. Each cleanup is independently
    defensive (a failure here must never block the real run below,
    same reasoning as run_on_failure's own docstring) and already a
    no-op when its own path is None or its own file doesn't exist."""
    if lockdown_path is not None:
        try:
            lockdown.cleanup_lockdown_file(host, port, lockdown_path)
        except Exception as exc:
            logger.warning(
                "stale pg_hba lockdown cleanup at startup raised: %s", exc, exc_info=True
            )

    if connection_limit_state_path is not None:
        try:
            lockdown.cleanup_connection_limit_lockdown(host, port, connection_limit_state_path)
        except Exception as exc:
            logger.warning(
                "stale connection-limit lockdown cleanup at startup raised: %s",
                exc,
                exc_info=True,
            )


def run(
    host: str,
    port: str,
    *,
    glibc_locales_path: str,
    partition_repair_enabled: bool,
    max_repair_attempts: int,
    hooks: HooksConfig | None = None,
    max_parallel_databases: int = 4,
    dry_run: bool = False,
    lockdown_path: str | None = None,
    connection_lockdown_enabled: bool = True,
    lockdown_mechanism: str = "pg_hba",
    connection_limit_state_path: str | None = None,
    hook_timeout_sec: float = 90,
    upgrade_completion_state_file: str | None = None,
    upgrade_old_datadir_retention_days: int | None = None,
) -> RunReport:
    report = RunReport()
    hooks = hooks or HooksConfig()

    if dry_run:
        # No LockdownManager here -- a real one would open its own
        # admin connection and spawn a background thread (see
        # lockdown.py) purely to sit unused: dry-run never locks
        # anything, and COLLATION_GUARD_LOCKDOWN_FILE is always set in
        # production regardless of connectionLockdown.enable.
        with _connect(host, port, "postgres") as admin_conn:
            databases = collation.connectable_databases(admin_conn)
        for dbname in databases:
            with _connect(host, port, dbname) as conn:
                for schema, table in partitions.partition_repair_candidates(conn):
                    print(f"{dbname}.{schema}.{table}")
        return report

    # Must run before anything below ever considers locking a database
    # again this run -- see _cleanup_stale_lockdown_state's own
    # docstring for why this can't just rely on the OnFailure=
    # companion unit.
    _cleanup_stale_lockdown_state(
        host,
        port,
        lockdown_path=lockdown_path,
        connection_limit_state_path=connection_limit_state_path,
    )

    # lockdown_mechanism picks which of the two real managers to build
    # when connectionLockdown is enabled -- "pg_hba" (PostgreSQL 16+,
    # docs/decisions/0007) or "connection_limit" (the PostgreSQL < 16
    # fallback, docs/decisions/0009). Falls back to NullLockdownManager
    # if the mechanism's own required path wasn't actually supplied --
    # same defensive shape the pre-existing pg_hba branch already had.
    manager: LockdownManagerLike
    if not connection_lockdown_enabled:
        manager = lockdown.NullLockdownManager()
    elif lockdown_mechanism == "connection_limit" and connection_limit_state_path is not None:
        manager = lockdown.ConnectionLimitLockdownManager(host, port, connection_limit_state_path)
    elif lockdown_path is not None:
        manager = lockdown.LockdownManager(host, port, lockdown_path)
    else:
        manager = lockdown.NullLockdownManager()

    try:
        if not _run_hooks(
            hooks.pre_start,
            "pre_start",
            report,
            timeout_sec=hook_timeout_sec,
            context=_report_context(report),
        ):
            return report

        with _connect(host, port, "postgres") as admin_conn:
            databases = collation.connectable_databases(admin_conn)
            template0_error = collation.process_template0(admin_conn)
            if template0_error is not None:
                report.failures.append(
                    Failure(database="template0", relation="template0", error=template0_error)
                )

        # Only reached once a live connection to THIS (the new, post-
        # upgrade) cluster has actually succeeded above -- the
        # verification oldDataDirRetentionDays=0's "delete immediately"
        # promise depends on (docs/decisions/0011). Deliberately not
        # inside upgrade.run_upgrade() itself, which runs strictly
        # before postgresql.service ever starts and so can never prove
        # the new cluster actually comes up. A no-op on every run after
        # the first (idempotent -- see
        # cleanup_old_datadir_now_if_zero_retention's own docstring),
        # and a no-op entirely when upgrade_completion_state_file is
        # unset (upgrade.enable was never turned on).
        if upgrade_completion_state_file is not None:
            upgrade.cleanup_old_datadir_now_if_zero_retention(
                upgrade_completion_state_file, upgrade_old_datadir_retention_days
            )

        glibc_reindexed = _process_glibc_stamp(host, port, glibc_locales_path, report, manager)

        # Each worker gets its own psycopg.Connection and its own
        # RunReport (see _process_database's docstring) -- no shared
        # mutable state between threads, so no locking needed.
        # Submitted in sorted order and merged in sorted order too, so
        # the final report is deterministic regardless of which thread
        # actually finishes first.
        with ThreadPoolExecutor(max_workers=max_parallel_databases) as executor:
            futures = [
                executor.submit(
                    _process_database,
                    host,
                    port,
                    dbname,
                    partition_repair_enabled,
                    max_repair_attempts,
                    hooks,
                    manager,
                    hook_timeout_sec,
                    dbname in glibc_reindexed,
                )
                for dbname in sorted(databases)
            ]
            for future in futures:
                sub_report = future.result()
                report.databases_processed.extend(sub_report.databases_processed)
                for dbname in sub_report.databases_repaired:
                    _mark_repaired(report, dbname)
                report.failures.extend(sub_report.failures)

        report.databases_processed.sort()
        report.databases_repaired.sort()

        if report.success:
            _run_hooks(
                hooks.on_success,
                "on_success",
                report,
                timeout_sec=hook_timeout_sec,
                context=_report_context(report),
            )

        # postRun fires regardless of outcome -- unlike onSuccess, which is
        # deliberately gated on a clean run.
        post_run_error = None if report.success else _summarize_failures(report)
        _run_hooks(
            hooks.post_run,
            "post_run",
            report,
            timeout_sec=hook_timeout_sec,
            context=_report_context(report),
            error=post_run_error,
        )

        return report
    finally:
        manager.stop()


def _recover_last_context(context_file: str) -> tuple[dict[str, object], str]:
    """Reads the context file the previous run wrote (if any) to
    recover what failed. A missing or unreadable file -- e.g. a crash
    on the very first-ever run, before anything was ever written --
    falls back to a generic message and an empty dict (not None, for
    the same "COLLATION_GUARD_CONTEXT is always valid JSON" guarantee
    every other stage gives) rather than raising."""
    try:
        with open(context_file) as f:
            data: dict[str, object] = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}, "collation-guard failed or crashed (no prior run context available)"

    failures = data.get("failures", [])
    assert isinstance(failures, list)
    if not failures:
        return data, "collation-guard failed or crashed (no failures recorded in last context)"
    error = "; ".join(f"{f['database']}.{f['relation']}: {f['error']}" for f in failures)
    return data, error


def run_on_failure(
    hooks_file: str,
    context_file: str,
    *,
    lockdown_path: str | None = None,
    connection_limit_state_path: str | None = None,
    host: str | None = None,
    port: str | None = None,
    hook_timeout_sec: float = 90,
) -> int:
    """Second entry point, invoked by the postgresql-collation-guard-
    on-failure.service companion unit after the main run failed or
    crashed outright (systemd's OnFailure=, the one guarantee a dead
    process can't arrange for itself). Reads the same hooks file as the
    main run and runs hooks.onFailure through the exact same
    run_hook() as every other stage -- no separate implementation.

    Also unconditionally cleans up whichever connectionLockdown
    mechanism's state was left behind by a crash mid-lock, independent
    of any configured onFailure hooks -- see
    lockdown.cleanup_lockdown_file() (pg_hba.conf mechanism) and
    lockdown.cleanup_connection_limit_lockdown() (the PostgreSQL < 16
    fallback, docs/decisions/0009). Both run unconditionally and
    independently of each other: the on-failure unit doesn't need to
    know which mechanism the main run was actually configured with,
    since each cleanup is already a no-op when its own path is None or
    its own file doesn't exist -- only one of the two ever has
    anything to do on a given deployment. Each is None in a
    direct/test invocation without the corresponding env var set; both
    are always set by the real on-failure unit (see run()'s dry-run
    branch above for the same fact about COLLATION_GUARD_LOCKDOWN_FILE).

    Every cleanup step and loading the hooks file are wrapped
    defensively: a failure in any of them (Postgres itself unreachable
    -- exactly the scenario that triggers this unit when
    postgresql.service fails to start -- or a corrupt hooks file) must
    never crash this entry point itself (see docs/decisions/0006).
    That guarantee is asymmetric, though: a cleanup failure is logged
    and the configured onFailure hooks still run afterward, but a
    hooks-file load failure is fatal to the hook run itself -- there's
    nothing left to execute -- so this returns 1 immediately with zero
    onFailure hooks run in that case."""
    if lockdown_path is not None:
        assert host is not None and port is not None
        try:
            lockdown.cleanup_lockdown_file(host, port, lockdown_path)
        except Exception as exc:
            logger.warning(
                "pg_hba lockdown cleanup during --on-failure raised: %s", exc, exc_info=True
            )

    if connection_limit_state_path is not None:
        assert host is not None and port is not None
        try:
            lockdown.cleanup_connection_limit_lockdown(host, port, connection_limit_state_path)
        except Exception as exc:
            logger.warning(
                "connection-limit lockdown cleanup during --on-failure raised: %s",
                exc,
                exc_info=True,
            )


    try:
        hooks = load_hooks(hooks_file)
    except Exception as exc:
        logger.warning(
            "failed to load hooks file during --on-failure: %s", exc, exc_info=True
        )
        return 1

    context, error = _recover_last_context(context_file)
    report = RunReport()
    _run_hooks(
        hooks.on_failure,
        "on_failure",
        report,
        timeout_sec=hook_timeout_sec,
        context=context,
        error=error,
    )
    return 0 if report.success else 1


def run_upgrade_entrypoint(
    *,
    old_bindir: str,
    new_bindir: str,
    old_datadir: str,
    new_datadir: str,
    old_schema: str,
    new_schema: str,
    superuser: str,
    completion_state_file: str,
    transfer_mode: str = "auto",
    jobs: int | None = None,
    initdb_args: list[str] | None = None,
) -> int:
    """Third entry point, invoked by the postgresql-collation-guard-
    upgrade.service unit -- ordered Before=["postgresql.service"]
    (docs/decisions/0011), strictly before either the old or new
    cluster's own postgres process ever starts. Unlike run()/
    run_on_failure() above, this never opens a live connection at all:
    pg_upgrade manages both clusters' startup/shutdown internally.

    Deliberately does NOT handle oldDataDirRetentionDays -- not even
    the "0, delete immediately" case. That's run()'s job now, called
    only after a live connection to the new cluster has actually
    succeeded: deleting the only remaining copy of the real data
    before the new cluster has even been proven to start once (what
    this unit did before this fix) trades a one-time disk-space saving
    for total data loss if postgresql.service then fails to start for
    any unrelated reason.

    A VersionMismatchError/IdenticalDataDirectoriesError/
    IncompleteUpgradeError raised by upgrade.run_upgrade() here is a
    real, actionable misconfiguration or failure and is deliberately
    allowed to propagate as an uncaught exception -- the same "crash
    loud, let the journal and non-zero exit code carry it" behavior as
    every other hard failure in this project, not swallowed into a
    quiet return 1."""
    upgraded = upgrade.run_upgrade(
        old_bindir=old_bindir,
        new_bindir=new_bindir,
        old_datadir=old_datadir,
        new_datadir=new_datadir,
        old_schema=old_schema,
        new_schema=new_schema,
        superuser=superuser,
        transfer_mode=transfer_mode,
        jobs=jobs,
        initdb_args=initdb_args,
        completion_state_file=completion_state_file,
    )
    if upgraded:
        logger.info("pg_upgrade completed: %s -> %s", old_datadir, new_datadir)
    else:
        logger.info("upgrade.enable is set but no upgrade was needed -- nothing to do")
    return 0


def run_upgrade_cleanup_entrypoint(completion_state_file: str, retention_days: int | None) -> int:
    """Fourth entry point, invoked by the separate
    postgresql-collation-guard-upgrade-cleanup.timer/.service pair
    (docs/decisions/0011) -- independent of any particular boot, since
    a positive oldDataDirRetentionDays window is defined in terms of
    elapsed calendar time, not a repeat run of the upgrade unit itself.
    Always returns 0: "not due yet" and "nothing was ever recorded" are
    both ordinary, expected outcomes on most ticks, not failures."""
    removed = upgrade.cleanup_old_datadir_if_due(completion_state_file, retention_days)
    if removed:
        logger.info("removed the retained old PostgreSQL data directory (retention window elapsed)")
    return 0



def _write_context_file(path: str, report: RunReport) -> None:
    # Best-effort: a hook-context write failure is a hook-delivery
    # problem, not evidence the guard itself failed -- the exit code
    # below is still driven entirely by report.success, not by this.
    try:
        with open(path, "w") as f:
            json.dump(_report_context(report), f)
    except OSError:
        logger.warning("could not write context file %s", path, exc_info=True)


def _required_env(name: str) -> str:
    """A missing required environment variable is only ever reachable
    by someone invoking collation-guard directly with a hand-edited
    environment -- the NixOS module itself always sets every one of
    these (see nixosModule/config.nix). Still worth a clear,
    actionable message naming exactly which variable is missing,
    rather than a bare KeyError traceback that doesn't say so."""
    try:
        return os.environ[name]
    except KeyError:
        raise SystemExit(
            f"collation-guard: required environment variable {name} is not set"
        ) from None


def _int_env(name: str) -> int:
    """Same reasoning as _required_env() -- a malformed value here is
    only reachable via manual invocation, but the error should still
    name the offending variable and its actual (invalid) value rather
    than a bare ValueError."""
    value = os.environ[name]
    try:
        return int(value)
    except ValueError:
        raise SystemExit(
            f"collation-guard: environment variable {name}={value!r} is not a valid integer"
        ) from None


def _json_env(name: str) -> list[str]:
    """Only ever used for COLLATION_GUARD_UPGRADE_INITDB_ARGS, always a
    JSON array of strings (nixosModule/config.nix's own
    builtins.toJSON cfg.upgrade.initdbArgs) -- typed for that one real
    caller rather than the fully general `object` json.loads() itself
    returns."""
    value = os.environ[name]
    try:
        return json.loads(value)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"collation-guard: environment variable {name}={value!r} is not valid JSON: {exc}"
        ) from None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="collation-guard: %(message)s")

    parser = argparse.ArgumentParser(prog="collation-guard")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list partition-repair candidates across the cluster without changing anything",
    )
    parser.add_argument(
        "--on-failure",
        action="store_true",
        help=(
            "internal: invoked by the postgresql-collation-guard-on-failure.service "
            "companion unit after the main run failed or crashed -- not meant to be run "
            "directly"
        ),
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help=(
            "internal: invoked by the postgresql-collation-guard-upgrade.service unit "
            "before postgresql.service starts -- not meant to be run directly"
        ),
    )
    parser.add_argument(
        "--upgrade-cleanup",
        action="store_true",
        help=(
            "internal: invoked by the postgresql-collation-guard-upgrade-cleanup.timer's "
            "own service -- not meant to be run directly"
        ),
    )
    args = parser.parse_args()

    hook_timeout_sec = float(os.environ.get("COLLATION_GUARD_HOOK_TIMEOUT_SEC", "90"))

    if args.upgrade_cleanup:
        retention_env = os.environ.get("COLLATION_GUARD_UPGRADE_OLD_DATADIR_RETENTION_DAYS")
        return run_upgrade_cleanup_entrypoint(
            completion_state_file=_required_env("COLLATION_GUARD_UPGRADE_COMPLETION_STATE_FILE"),
            retention_days=_int_env("COLLATION_GUARD_UPGRADE_OLD_DATADIR_RETENTION_DAYS")
            if retention_env
            else None,
        )

    if args.upgrade:
        return run_upgrade_entrypoint(
            old_bindir=_required_env("COLLATION_GUARD_UPGRADE_OLD_BINDIR"),
            new_bindir=_required_env("COLLATION_GUARD_UPGRADE_NEW_BINDIR"),
            old_datadir=_required_env("COLLATION_GUARD_UPGRADE_OLD_DATADIR"),
            new_datadir=_required_env("COLLATION_GUARD_UPGRADE_NEW_DATADIR"),
            old_schema=_required_env("COLLATION_GUARD_UPGRADE_OLD_SCHEMA"),
            new_schema=_required_env("COLLATION_GUARD_UPGRADE_NEW_SCHEMA"),
            superuser=os.environ.get("COLLATION_GUARD_UPGRADE_SUPERUSER", "postgres"),
            completion_state_file=_required_env("COLLATION_GUARD_UPGRADE_COMPLETION_STATE_FILE"),
            transfer_mode=os.environ.get("COLLATION_GUARD_UPGRADE_TRANSFER_MODE", "auto"),
            jobs=_int_env("COLLATION_GUARD_UPGRADE_JOBS")
            if os.environ.get("COLLATION_GUARD_UPGRADE_JOBS")
            else None,
            initdb_args=_json_env("COLLATION_GUARD_UPGRADE_INITDB_ARGS")
            if os.environ.get("COLLATION_GUARD_UPGRADE_INITDB_ARGS")
            else None,
        )

    if args.on_failure:
        return run_on_failure(
            os.environ["COLLATION_GUARD_HOOKS_FILE"],
            os.environ["COLLATION_GUARD_CONTEXT_FILE"],
            lockdown_path=os.environ.get("COLLATION_GUARD_LOCKDOWN_FILE"),
            connection_limit_state_path=os.environ.get(
                "COLLATION_GUARD_CONNECTION_LIMIT_STATE_FILE"
            ),
            host=os.environ.get("PGHOST"),
            port=os.environ.get("PGPORT"),
            hook_timeout_sec=hook_timeout_sec,
        )

    hooks_file = os.environ.get("COLLATION_GUARD_HOOKS_FILE")
    hooks = load_hooks(hooks_file) if hooks_file else None

    upgrade_retention_env = os.environ.get("COLLATION_GUARD_UPGRADE_OLD_DATADIR_RETENTION_DAYS")

    report = run(
        os.environ["PGHOST"],
        os.environ["PGPORT"],
        glibc_locales_path=os.environ["GLIBC_LOCALES_PATH"],
        partition_repair_enabled=os.environ["COLLATION_GUARD_PARTITION_REPAIR_ENABLE"] == "true",
        max_repair_attempts=int(os.environ["COLLATION_GUARD_MAX_REPAIR_ATTEMPTS"]),
        max_parallel_databases=int(os.environ.get("COLLATION_GUARD_MAX_PARALLEL_DATABASES", "4")),
        hooks=hooks,
        dry_run=args.dry_run,
        lockdown_path=os.environ.get("COLLATION_GUARD_LOCKDOWN_FILE"),
        connection_lockdown_enabled=(
            os.environ.get("COLLATION_GUARD_CONNECTION_LOCKDOWN_ENABLE", "true") == "true"
        ),
        lockdown_mechanism=os.environ.get("COLLATION_GUARD_LOCKDOWN_MECHANISM", "pg_hba"),
        connection_limit_state_path=os.environ.get("COLLATION_GUARD_CONNECTION_LIMIT_STATE_FILE"),
        hook_timeout_sec=hook_timeout_sec,
        upgrade_completion_state_file=os.environ.get(
            "COLLATION_GUARD_UPGRADE_COMPLETION_STATE_FILE"
        ),
        upgrade_old_datadir_retention_days=(
            int(upgrade_retention_env) if upgrade_retention_env else None
        ),
    )

    if args.dry_run:
        return 0

    context_file = os.environ.get("COLLATION_GUARD_CONTEXT_FILE")
    if context_file:
        _write_context_file(context_file, report)

    if report.success:
        logger.info(
            "ok -- %d database(s) processed, %d repaired",
            len(report.databases_processed),
            len(report.databases_repaired),
        )
        return 0

    for failure in report.failures:
        logger.error("%s.%s: %s", failure.database, failure.relation, failure.error)
    logger.error("%d failure(s) -- see above", len(report.failures))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
