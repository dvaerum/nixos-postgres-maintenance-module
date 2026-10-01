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
from dataclasses import asdict, dataclass, field

import psycopg

from . import collation, partitions
from .hooks import Hook, HooksConfig, run_hook

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


def _connect(host: str, port: str, dbname: str) -> psycopg.Connection:
    return psycopg.connect(f"host={host} port={port} dbname={dbname}", prepare_threshold=None)


def _repair_partitions_in(
    conn: psycopg.Connection, dbname: str, max_attempts: int, report: RunReport
) -> None:
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
            report.databases_repaired.append(dbname)
        for child in result.skipped_ruled_children:
            logger.warning(
                "partition repair: %s.%s.%s has a RULE attached -- skipping, no automated "
                "way to suppress a rule's side effects the way DISABLE TRIGGER USER does",
                dbname,
                schema,
                child,
            )
        if not result.ok:
            report.failures.append(
                Failure(
                    database=dbname,
                    relation=f"{schema}.{table}",
                    error=f"partition repair gave up after {max_attempts} attempts",
                )
            )


def _process_database(
    host: str,
    port: str,
    dbname: str,
    partition_repair_enabled: bool,
    max_attempts: int,
    hooks: HooksConfig,
    report: RunReport,
) -> None:
    if not _run_hooks(
        hooks.per_database.pre_start, "database_pre_start", report, database=dbname
    ):
        # blockOnFailure=true: skip this database entirely -- no
        # process_database, no partition repair, not added to
        # databases_processed. Other databases are unaffected.
        return

    with _connect(host, port, dbname) as conn:
        result = collation.process_database(conn)
        if result.reindexed:
            logger.info(
                "%s: reindexed %d table(s) for a Postgres-tracked collation mismatch: %s",
                dbname,
                len(result.reindexed),
                ", ".join(result.reindexed),
            )
            report.databases_repaired.append(dbname)
        for table in result.failed:
            report.failures.append(
                Failure(database=dbname, relation=table, error="REINDEX failed")
            )
        if result.refresh_error is not None:
            report.failures.append(
                Failure(database=dbname, relation=dbname, error=result.refresh_error)
            )

        if partition_repair_enabled:
            _repair_partitions_in(conn, dbname, max_attempts, report)

    report.databases_processed.append(dbname)


def _process_glibc_stamp(host: str, port: str, glibc_locales_path: str, report: RunReport) -> None:
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
            with _connect(host, port, dbname) as conn:
                result = collation.reindex_all_user_tables(conn)
                if result.reindexed:
                    report.databases_repaired.append(dbname)
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
# database -- a global (preStart/onSuccess/postRun) hook failing, or
# template0.
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
    dry_run: bool = False,
) -> RunReport:
    report = RunReport()
    hooks = hooks or HooksConfig()

    if not dry_run and not _run_hooks(hooks.pre_start, "pre_start", report):
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

    _process_glibc_stamp(host, port, glibc_locales_path, report)

    for dbname in databases:
        _process_database(
            host, port, dbname, partition_repair_enabled, max_repair_attempts, hooks, report
        )

    if report.success:
        _run_hooks(hooks.on_success, "on_success", report)

    return report


def _write_context_file(path: str, report: RunReport) -> None:
    # Best-effort: a hook-context write failure is a hook-delivery
    # problem, not evidence the guard itself failed -- the exit code
    # below is still driven entirely by report.success, not by this.
    try:
        with open(path, "w") as f:
            json.dump(
                {
                    "databases_processed": report.databases_processed,
                    "databases_repaired": sorted(set(report.databases_repaired)),
                    "failures": [asdict(f) for f in report.failures],
                    "success": report.success,
                },
                f,
            )
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
    args = parser.parse_args()

    report = run(
        os.environ["PGHOST"],
        os.environ["PGPORT"],
        glibc_locales_path=os.environ["GLIBC_LOCALES_PATH"],
        partition_repair_enabled=os.environ["COLLATION_GUARD_PARTITION_REPAIR_ENABLE"] == "true",
        max_repair_attempts=int(os.environ["COLLATION_GUARD_MAX_REPAIR_ATTEMPTS"]),
        dry_run=args.dry_run,
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
            len(set(report.databases_repaired)),
        )
        return 0

    for failure in report.failures:
        logger.error("%s.%s: %s", failure.database, failure.relation, failure.error)
    logger.error("%d failure(s) -- see above", len(report.failures))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
