"""Entry point: orchestrates collation.py + partitions.py across every
database in the cluster. Driven entirely by environment variables set
by the NixOS module (see nixosModule/config.nix) -- there is no config
file, and --dry-run is the only CLI flag.
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

from . import collation, lockdown, partitions
from .hooks import Hook, HooksConfig, load_hooks, run_hook

logger = logging.getLogger("collation_guard")


@dataclass
class Failure:
    database: str
    relation: str
    error: str


@dataclass
class RunReport:
    """Outcome of one full run, across every database in the cluster --
    also the shape written to COLLATION_GUARD_CONTEXT_FILE for hooks
    (see nixosModule/options.nix) to read."""

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
    return psycopg.connect(
        f"host={host} port={port} dbname={dbname} application_name=collation-guard",
        prepare_threshold=None,
    )


@contextmanager
def _locked_connection(
    host: str,
    port: str,
    dbname: str,
    manager: lockdown.LockdownManager | lockdown.NullLockdownManager,
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
    result: collation.DatabaseResult, dbname: str, report: RunReport
) -> bool:
    if result.reindexed:
        logger.info(
            "%s: reindexed %d table(s) for a Postgres-tracked collation mismatch: %s",
            dbname,
            len(result.reindexed),
            ", ".join(result.reindexed),
        )
        _mark_repaired(report, dbname)
    for table in result.failed:
        report.failures.append(Failure(database=dbname, relation=table, error="REINDEX failed"))
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
    manager: lockdown.LockdownManager | lockdown.NullLockdownManager,
) -> RunReport:
    """Processes one database in complete isolation, returning its own
    RunReport rather than mutating a shared one -- lets run() call this
    from multiple threads (one per database) with no locking: each
    worker only ever touches its own report, and run() merges every
    worker's result into the real report sequentially, back in the
    main thread, once every worker has finished."""
    report = RunReport()

    if not _run_hooks(
        hooks.per_database.pre_start, "database_pre_start", report, database=dbname, context={}
    ):
        # blockOnFailure=true: skip this database entirely -- no
        # process_database, no partition repair, not added to
        # databases_processed. Other databases are unaffected.
        return report

    def needs_lock(conn: psycopg.Connection) -> bool:
        # Cheap, read-only checks decide whether this database needs
        # locking at all -- a database with nothing to fix is never
        # locked (no lock()/unlock() call, no entry in the lockdown
        # file), matching this project's own existing detection logic
        # rather than inventing a new one.
        locked = collation.is_database_stale(conn)
        if partition_repair_enabled:
            locked = locked or bool(partitions.partition_repair_candidates(conn))
        return locked

    with _locked_connection(host, port, dbname, manager, needs_lock) as conn:
        collation_result = collation.process_database(conn)
        collation_ok = _apply_collation_result(collation_result, dbname, report)
        partition_ok = True
        if partition_repair_enabled:
            partition_ok = _apply_partition_repair(conn, dbname, max_attempts, report)

    report.databases_processed.append(dbname)

    if collation_ok and partition_ok:
        _run_hooks(
            hooks.per_database.on_success,
            "database_success",
            report,
            database=dbname,
            context={"reindexed": collation_result.reindexed},
        )
    else:
        error = "; ".join(f"{f.relation}: {f.error}" for f in report.failures)
        _run_hooks(
            hooks.per_database.on_failure,
            "database_failure",
            report,
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
    manager: lockdown.LockdownManager | lockdown.NullLockdownManager,
) -> None:
    with _connect(host, port, "postgres") as admin_conn:
        stamp = collation.glibc_stamp(admin_conn)
        if stamp == glibc_locales_path:
            return

        logger.info(
            "glibc locale data changed (%s -> %s), reindexing C.UTF-8 databases",
            stamp or "not yet recorded",
            glibc_locales_path,
        )
        c_utf8_dbs = collation.c_utf8_databases(admin_conn)
        all_ok = True
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
                if result.reindexed:
                    _mark_repaired(report, dbname)
                for table in result.failed:
                    all_ok = False
                    report.failures.append(
                        Failure(
                            database=dbname,
                            relation=table,
                            error="REINDEX failed (C.UTF-8 stamp check)",
                        )
                    )

        # Only advance the stamp once every C.UTF-8 database reindexed
        # cleanly -- otherwise the next run must retry, not silently skip.
        if all_ok:
            collation.set_glibc_stamp(admin_conn, glibc_locales_path)


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
    one's outcome."""
    ok = True
    for hook in hooks:
        result = run_hook(hook, stage=stage, database=database, context=context, error=error)
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
) -> RunReport:
    report = RunReport()
    hooks = hooks or HooksConfig()
    manager: lockdown.LockdownManager | lockdown.NullLockdownManager = (
        lockdown.LockdownManager(host, port, lockdown_path)
        if lockdown_path is not None and connection_lockdown_enabled
        else lockdown.NullLockdownManager()
    )

    try:
        if not dry_run and not _run_hooks(
            hooks.pre_start, "pre_start", report, context=_report_context(report)
        ):
            return report

        with _connect(host, port, "postgres") as admin_conn:
            databases = collation.connectable_databases(admin_conn)

            if dry_run:
                for dbname in databases:
                    with _connect(host, port, dbname) as conn:
                        for schema, table in partitions.partition_repair_candidates(conn):
                            print(f"{dbname}.{schema}.{table}")
                return report

            template0_error = collation.process_template0(admin_conn)
            if template0_error is not None:
                report.failures.append(
                    Failure(database="template0", relation="template0", error=template0_error)
                )

        _process_glibc_stamp(host, port, glibc_locales_path, report, manager)

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
            _run_hooks(hooks.on_success, "on_success", report, context=_report_context(report))

        # postRun fires regardless of outcome -- unlike onSuccess, which is
        # deliberately gated on a clean run.
        post_run_error = None if report.success else _summarize_failures(report)
        _run_hooks(
            hooks.post_run,
            "post_run",
            report,
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
    host: str | None = None,
    port: str | None = None,
) -> int:
    """Second entry point, invoked by the postgresql-collation-guard-
    on-failure.service companion unit after the main run failed or
    crashed outright (systemd's OnFailure=, the one guarantee a dead
    process can't arrange for itself). Reads the same hooks file as the
    main run and runs hooks.onFailure through the exact same
    run_hook() as every other stage -- no separate implementation.

    Also unconditionally cleans up a lockdown file left behind by a
    crash mid-lock, independent of any configured onFailure hooks --
    see lockdown.cleanup_lockdown_file(). A no-op when lockdown_path is
    None (connectionLockdown.enable = false)."""
    if lockdown_path is not None:
        assert host is not None and port is not None
        lockdown.cleanup_lockdown_file(host, port, lockdown_path)

    hooks = load_hooks(hooks_file)
    context, error = _recover_last_context(context_file)
    report = RunReport()
    _run_hooks(hooks.on_failure, "on_failure", report, context=context, error=error)
    return 0 if report.success else 1



def _write_context_file(path: str, report: RunReport) -> None:
    # Best-effort: a hook-context write failure is a hook-delivery
    # problem, not evidence the guard itself failed -- the exit code
    # below is still driven entirely by report.success, not by this.
    try:
        with open(path, "w") as f:
            json.dump(_report_context(report), f)
    except OSError:
        logger.warning("could not write context file %s", path, exc_info=True)


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
    args = parser.parse_args()

    if args.on_failure:
        return run_on_failure(
            os.environ["COLLATION_GUARD_HOOKS_FILE"],
            os.environ["COLLATION_GUARD_CONTEXT_FILE"],
            lockdown_path=os.environ.get("COLLATION_GUARD_LOCKDOWN_FILE"),
            host=os.environ.get("PGHOST"),
            port=os.environ.get("PGPORT"),
        )

    hooks_file = os.environ.get("COLLATION_GUARD_HOOKS_FILE")
    hooks = load_hooks(hooks_file) if hooks_file else None

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
