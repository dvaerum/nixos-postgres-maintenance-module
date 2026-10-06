"""PostgreSQL major-version upgrade: detection, validation, preflight
checks, and orchestration for pg_upgrade (docs/decisions/0010, 0011).
Most functions below are pure or near-pure (read real on-disk/
filesystem state, make no irreversible change) and fully covered by
the fast pytest tier with no second real Postgres binary needed.
initdb_new_cluster()/run_pg_upgrade() are the exception -- they invoke
real subprocesses -- but their own argv-construction and error-handling
contract is still proven here against a mocked subprocess.run; a real
initdb/pg_upgrade invocation against an actual two-major-version
cluster is proven separately (see the heavy-tier packages.pgUpgradeTest,
not wired into this fast tier -- same precedent as docs/decisions/0008's
icuDriftTest).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)



class VersionMismatchError(Exception):
    """Raised when the real on-disk PG_VERSION disagrees with the
    configured oldPackage's psqlSchema. The on-disk state is always
    authoritative -- a misconfigured oldPackage (or a cluster that's
    secretly a different version than assumed) must never be silently
    trusted, since everything downstream of this check is irreversible
    (pg_upgrade)."""


class InsufficientDiskSpaceError(Exception):
    """Raised when there isn't enough free space for --copy mode's full,
    independent duplication of the old cluster's data alongside the
    still-intact original."""


class TransferModeUnavailableError(Exception):
    """Raised when an EXPLICITLY requested transfer mode ("clone" or
    "link") fails its own preflight requirement (same filesystem,
    reflink support). Deliberately never silently substituted for a
    different mode -- only "auto" is allowed to make that call on the
    caller's behalf (see resolve_transfer_mode)."""


class InitdbFailedError(Exception):
    """Raised when the new cluster's own initdb (run against an empty
    new data directory, before pg_upgrade touches anything) exits
    non-zero."""


class PgUpgradeFailedError(Exception):
    """Raised when the real pg_upgrade binary exits non-zero. Includes
    stderr: pg_upgrade's own exit status alone names none of the actual
    incompatibility (a leftover extension, a catalog mismatch) an
    operator needs to see to fix it."""


class IdenticalDataDirectoriesError(Exception):
    """Raised when old_datadir and new_datadir resolve to the exact
    same path -- a real misconfiguration (oldDataDir and the configured
    services.postgresql.dataDir happen to coincide), caught explicitly
    and FIRST: without this check, new_datadir's own PG_VERSION (really
    the old cluster's) would otherwise be silently read as "already
    upgraded" by the idempotency gate below, absorbing the
    misconfiguration into a silent no-op with zero diagnostic."""


class IncompleteUpgradeError(Exception):
    """Raised when new_datadir already exists because a PREVIOUS
    pg_upgrade attempt got as far as initdb_new_cluster() but never
    recorded a completed attempt -- i.e. run_pg_upgrade() itself failed
    or the process was killed before it could finish. pg_upgrade cannot
    resume into an already-touched target directory (confirmed in its
    own documentation: a failed/interrupted run must be retried against
    a freshly-initialized cluster, not resumed in place), so this is
    never silently retried and never silently treated as "already
    done" -- both would either risk a second pg_upgrade run against an
    inconsistent target or a false, permanent "success" with the new
    cluster left empty/partial and postgresql.service starting on top
    of it with zero signal anything is wrong. Raised on EVERY
    subsequent invocation until an operator manually inspects and
    removes new_datadir."""


def read_pg_version(datadir: str) -> str | None:
    """Reads the single-line major version number from <datadir>/PG_VERSION.
    A missing file means an uninitialized/fresh data directory -- not an
    error, just "nothing to validate against yet": the caller decides what
    that means (e.g. upgrade.enable with no existing cluster is a no-op,
    not a crash)."""
    try:
        with open(os.path.join(datadir, "PG_VERSION")) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def validate_old_version(datadir: str, expected_schema: str) -> None:
    """Raises VersionMismatchError if datadir's on-disk PG_VERSION exists
    and disagrees with expected_schema. A missing PG_VERSION (no cluster
    there yet) is not a mismatch -- it's a distinct, non-error state; the
    caller decides what to do with it (see read_pg_version's own
    docstring)."""
    actual = read_pg_version(datadir)
    if actual is not None and actual != expected_schema:
        raise VersionMismatchError(
            f"{datadir}/PG_VERSION says {actual!r}, but the configured "
            f"oldPackage is PostgreSQL {expected_schema!r} -- refusing to "
            "proceed. The configured oldPackage must match what is actually "
            "on disk before any upgrade can safely run."
        )


def upgrade_needed(old_schema: str, new_schema: str) -> bool:
    """True when the configured old and new psqlSchema majors differ --
    the normal-boot, nothing-to-do case (new_schema == old_schema) is
    the common path and must stay a cheap, side-effect-free check."""
    return old_schema != new_schema


def same_filesystem(path_a: str, path_b: str) -> bool:
    """True when both paths resolve to the same mounted filesystem
    (matching st_dev) -- pg_upgrade's own --link/--clone both require
    this, and it's unrelated to whether either path even exists yet
    for --clone's own reflink probe (see reflink_supported)."""
    return os.stat(path_a).st_dev == os.stat(path_b).st_dev


def reflink_supported(dir_a: str, dir_b: str) -> bool:
    """Empirically probes whether dir_a/dir_b's filesystem supports
    reflink/COW cloning (pg_upgrade's --clone mode) by actually
    attempting one, via `cp --reflink=always` on a throwaway file --
    more reliable than a hardcoded filesystem-type/kernel-version list,
    since that list itself drifts (new filesystems/kernels gain
    support, distro configs vary) independently of this project's own
    release cycle."""
    probe_src = os.path.join(dir_a, ".collation-guard-reflink-probe-src")
    probe_dst = os.path.join(dir_b, ".collation-guard-reflink-probe-dst")
    try:
        with open(probe_src, "wb") as f:
            f.write(b"collation-guard reflink probe\n")
        result = subprocess.run(
            ["cp", "--reflink=always", probe_src, probe_dst],
            capture_output=True,
        )
        return result.returncode == 0
    finally:
        for path in (probe_src, probe_dst):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def _directory_size(path: str) -> int:
    """Total size in bytes of every regular file under path, recursively.
    Plain os.walk + os.path.getsize rather than shelling out to `du` --
    an exact byte count with no subprocess/output-parsing of its own,
    and one less external dependency for an estimate this project can
    compute directly."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                # A file that vanished between listing and stat (e.g. a
                # concurrent WAL rotation) shouldn't abort what is
                # inherently an estimate, not a precise accounting.
                continue
    return total


def check_disk_space(
    old_datadir: str, new_datadir_parent: str, *, required_multiplier: float = 2.0
) -> None:
    """Raises InsufficientDiskSpaceError if new_datadir_parent's
    filesystem doesn't have at least required_multiplier times
    old_datadir's current on-disk size free. That multiplier is --copy
    transfer mode's real requirement: a full independent duplicate
    written alongside the still-intact original, not a replacement."""
    old_size = _directory_size(old_datadir)
    needed = int(old_size * required_multiplier)
    free = shutil.disk_usage(new_datadir_parent).free
    if free < needed:
        raise InsufficientDiskSpaceError(
            f"{new_datadir_parent} has {free} bytes free, but --copy mode "
            f"needs ~{needed} bytes ({required_multiplier}x {old_datadir}'s "
            f"current {old_size} bytes) to keep the old cluster fully "
            "intact alongside the new one."
        )


def resolve_transfer_mode(requested: str, *, old_datadir: str, new_datadir_parent: str) -> str:
    """Resolves the configured transferMode ("copy" | "clone" | "link" |
    "auto") to an actual pg_upgrade flag ("copy" | "clone" | "link"),
    running each mode's own preflight requirement first (see
    docs/decisions/0010).

    "auto" tries "clone" (same filesystem + a real reflink probe) and
    falls back to "copy" (after copy's own disk-space preflight) if
    either check fails -- but never falls back to "link": that mode's
    "no undo once the new cluster starts" story means it only ever runs
    on an explicit, deliberate request, never a substitution "auto"
    makes on the caller's behalf.

    An EXPLICIT (non-"auto") request that fails its own preflight
    raises TransferModeUnavailableError rather than silently
    substituting a different mode."""
    if requested == "copy":
        check_disk_space(old_datadir, new_datadir_parent)
        return "copy"

    if requested == "clone":
        if not same_filesystem(old_datadir, new_datadir_parent):
            raise TransferModeUnavailableError(
                f"--clone requires {old_datadir} and {new_datadir_parent} to be "
                "on the same filesystem, but they are not."
            )
        if not reflink_supported(old_datadir, new_datadir_parent):
            raise TransferModeUnavailableError(
                f"--clone requires reflink/copy-on-write support, which the "
                f"filesystem backing {old_datadir}/{new_datadir_parent} does "
                "not provide."
            )
        return "clone"

    if requested == "link":
        if not same_filesystem(old_datadir, new_datadir_parent):
            raise TransferModeUnavailableError(
                f"--link requires {old_datadir} and {new_datadir_parent} to be "
                "on the same filesystem, but they are not."
            )
        return "link"

    if requested == "auto":
        if same_filesystem(old_datadir, new_datadir_parent) and reflink_supported(
            old_datadir, new_datadir_parent
        ):
            return "clone"
        check_disk_space(old_datadir, new_datadir_parent)
        return "copy"

    raise ValueError(f"unknown transfer mode: {requested!r}")


def initdb_new_cluster(
    new_bindir: str,
    new_datadir: str,
    superuser: str,
    *,
    initdb_args: list[str] | None = None,
) -> None:
    """Initializes the new cluster's empty data directory via the new
    binary's own initdb, mirroring nixpkgs's own postgresql.service
    preStart invocation (same -U, same extra initdbArgs) exactly -- the
    resulting fresh cluster must be indistinguishable from one NixOS
    would have initialized itself, since pg_upgrade() below migrates
    the old cluster's actual data into it afterward, not around it."""
    args = [
        os.path.join(new_bindir, "initdb"),
        "-D",
        new_datadir,
        "-U",
        superuser,
        *(initdb_args or []),
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise InitdbFailedError(
            f"initdb for the new cluster at {new_datadir} failed (exit "
            f"{result.returncode}): {result.stderr.strip()}"
        )


_TRANSFER_MODE_FLAGS: dict[str, str | None] = {"copy": None, "clone": "--clone", "link": "--link"}


def run_pg_upgrade(
    *,
    old_bindir: str,
    new_bindir: str,
    old_datadir: str,
    new_datadir: str,
    transfer_mode: str,
    superuser: str,
    jobs: int | None = None,
) -> None:
    """Invokes the real pg_upgrade binary (shipped alongside new_bindir)
    to migrate old_datadir's cluster into new_datadir, already
    initialized by initdb_new_cluster() above. transfer_mode must
    already be resolved to a concrete pg_upgrade flag by
    resolve_transfer_mode() -- "auto" has no pg_upgrade equivalent, so a
    caller that forgets to resolve it first fails loudly here with a
    ValueError, not a literal, nonsense "--auto" argv token passed
    through to the real binary. "copy" is pg_upgrade's own default and
    needs no flag at all.

    Runs with cwd=new_datadir: pg_upgrade writes its own log files
    (pg_upgrade_internal.log, loadable_libraries.txt, ...) into the
    current directory, and new_datadir is guaranteed writable by
    whichever user is running this (initdb_new_cluster() just created
    it), unlike wherever this process happened to be started from.
    """
    if transfer_mode not in _TRANSFER_MODE_FLAGS:
        raise ValueError(
            f"unknown transfer mode for pg_upgrade: {transfer_mode!r} -- must already be "
            'resolved to "copy", "clone", or "link" by resolve_transfer_mode()'
        )

    args = [
        os.path.join(new_bindir, "pg_upgrade"),
        "--old-bindir",
        old_bindir,
        "--new-bindir",
        new_bindir,
        "--old-datadir",
        old_datadir,
        "--new-datadir",
        new_datadir,
        "--username",
        superuser,
    ]
    flag = _TRANSFER_MODE_FLAGS[transfer_mode]
    if flag is not None:
        args.append(flag)
    if jobs is not None:
        args.extend(["--jobs", str(jobs)])

    result = subprocess.run(args, capture_output=True, text=True, cwd=new_datadir)
    if result.returncode != 0:
        raise PgUpgradeFailedError(
            f"pg_upgrade from {old_datadir} to {new_datadir} failed (exit "
            f"{result.returncode}): {result.stderr.strip()}"
        )


@dataclass(frozen=True, slots=True)
class UpgradeAttempt:
    """One upgrade attempt's recorded state -- written in two steps by
    run_upgrade() below: record_upgrade_attempt_started() right before
    initdb_new_cluster() ever touches new_datadir, then
    record_upgrade_attempt_completed() only once run_pg_upgrade() has
    actually succeeded. completed_at is None for an attempt that
    started but never finished -- the one signal that tells a
    genuinely-completed upgrade apart from new_datadir merely existing
    because a previous attempt got partway through before failing (see
    IncompleteUpgradeError)."""

    old_datadir: str
    started_at: datetime
    completed_at: datetime | None


def _write_state_atomically(state_file: str, payload: dict[str, object]) -> None:
    """Shared atomic-write helper (temp file + os.replace, same
    directory) -- same pattern as 0009's connection-limit state file: a
    process killed mid-write must never leave a corrupt file behind for
    a later read (the very next boot's idempotency gate, or the
    retention timer) to choke on."""
    state_dir = os.path.dirname(state_file) or "."
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".upgrade-attempt-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, state_file)
    except BaseException:
        os.remove(tmp_path)
        raise


def record_upgrade_attempt_started(
    state_file: str, old_datadir: str, *, now: datetime | None = None
) -> None:
    """Written BEFORE initdb_new_cluster() ever touches new_datadir --
    the one fact run_upgrade() needs on a later invocation to tell "a
    previous attempt started here" apart from "new_datadir just
    happens to already exist for some unrelated reason" (see
    run_upgrade()'s own docstring)."""
    moment = now if now is not None else datetime.now(UTC)
    _write_state_atomically(
        state_file,
        {"old_datadir": old_datadir, "started_at": moment.isoformat(), "completed_at": None},
    )


def record_upgrade_attempt_completed(state_file: str, *, now: datetime | None = None) -> None:
    """Updates the SAME record record_upgrade_attempt_started() wrote,
    setting completed_at -- called only once run_pg_upgrade() has
    actually succeeded. Preserves the original old_datadir/started_at
    rather than re-deriving them, so the one record always reflects
    this attempt's full history."""
    existing = read_upgrade_attempt(state_file)
    assert existing is not None, (
        f"{state_file} has no started attempt to complete -- "
        "record_upgrade_attempt_started() must run first"
    )
    moment = now if now is not None else datetime.now(UTC)
    _write_state_atomically(
        state_file,
        {
            "old_datadir": existing.old_datadir,
            "started_at": existing.started_at.isoformat(),
            "completed_at": moment.isoformat(),
        },
    )


def read_upgrade_attempt(state_file: str) -> UpgradeAttempt | None:
    """None when no attempt has ever started (the common case, and the
    state before any upgrade.enable transition has ever run) -- not an
    error, same "missing means nothing to report yet" shape as
    read_pg_version() above."""
    try:
        with open(state_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    return UpgradeAttempt(
        old_datadir=data["old_datadir"],
        started_at=datetime.fromisoformat(data["started_at"]),
        completed_at=(
            datetime.fromisoformat(data["completed_at"]) if data["completed_at"] else None
        ),
    )


def old_datadir_due_for_cleanup(
    completed_at: datetime, retention_days: int | None, *, now: datetime | None = None
) -> bool:
    """Pure predicate for docs/decisions/0011's oldDataDirRetentionDays
    window: None means "keep forever" (never due); 0 means due the
    moment the upgrade completed (no waiting period); N>0 means due N
    days after completed_at. Evaluated against elapsed *calendar* time,
    not a repeat run of the upgrade unit itself -- that unit only ever
    runs once per transition (docs/decisions/0011), so a separate timer
    is what calls this, independent of any particular boot."""
    if retention_days is None:
        return False
    moment = now if now is not None else datetime.now(UTC)
    return moment >= completed_at + timedelta(days=retention_days)


def run_upgrade(
    *,
    old_bindir: str,
    new_bindir: str,
    old_datadir: str,
    new_datadir: str,
    old_schema: str,
    new_schema: str,
    superuser: str,
    transfer_mode: str = "auto",
    jobs: int | None = None,
    initdb_args: list[str] | None = None,
    completion_state_file: str,
) -> bool:
    """Top-level entry point for the postgresql-collation-guard-upgrade
    unit (ordered Before=["postgresql.service"] -- see
    docs/decisions/0011), tying every piece above together. Returns
    True iff an upgrade actually ran (and succeeded); False for every
    no-op case below.

    old_datadir == new_datadir is checked FIRST and raises
    IdenticalDataDirectoriesError -- a real misconfiguration that, if
    checked anywhere later, would already have been silently absorbed
    by the idempotency gate below (new_datadir's PG_VERSION -- really
    the OLD cluster's own, since they're the same path -- would read as
    "already upgraded").

    Idempotent by construction once genuinely complete: new_datadir
    already having its own PG_VERSION AND a matching completed attempt
    on record means an upgrade already ran here -- upgrade.enable can
    stay true indefinitely across any number of subsequent boots with
    no repeat effect (docs/decisions/0011). But new_datadir having a
    PG_VERSION with NO completed attempt on record is NOT treated the
    same way:
    - if an attempt was started but never completed, this raises
      IncompleteUpgradeError every single time, rather than either
      silently treating a failed migration as successful (the exact
      gap this closes -- see IncompleteUpgradeError's own docstring)
      or silently retrying pg_upgrade against an already-touched
      target it cannot resume into.
    - if there's no recorded attempt at all, new_datadir's PG_VERSION
      came from somewhere entirely unrelated to this feature (e.g.
      upgrade.enable was turned on after the new version was already
      running normally) -- nothing to upgrade, a genuine no-op.

    A missing old_datadir (no PG_VERSION at all) is a distinct no-op:
    a brand-new host with upgrade.enable configured ahead of its very
    first boot has nothing to upgrade *from* -- the upstream
    postgresql.service preStart initdb's new_datadir itself in that
    case, exactly as if upgrade.enable were false.

    validate_old_version() still runs before the upgrade_needed() check
    below, even though neither raises anything irreversible on its own
    -- an old cluster that's secretly the wrong version must be caught
    immediately, before anything downstream (even the no-op path) ever
    assumes old_schema is trustworthy.

    Does NOT handle oldDataDirRetentionDays at all (including the "0 --
    delete immediately" case) -- that's main.run()'s job now, called
    only after a live connection to the new cluster has actually
    succeeded (see docs/decisions/0011's own correction: deleting the
    only remaining copy of the real data before the new cluster has
    even been proven to start once was a real gap in the original
    design).
    """
    if old_datadir == new_datadir:
        raise IdenticalDataDirectoriesError(
            f"old and new data directories must not be the same path ({old_datadir}) -- "
            "this is almost certainly a misconfiguration (upgrade.oldDataDir and the "
            "configured services.postgresql.dataDir resolved to the same value)."
        )

    if read_pg_version(new_datadir) is not None:
        attempt = read_upgrade_attempt(completion_state_file)
        if attempt is None:
            return False
        if attempt.completed_at is None:
            raise IncompleteUpgradeError(
                f"{new_datadir} already exists from a pg_upgrade attempt that started at "
                f"{attempt.started_at.isoformat()} but never completed -- pg_upgrade cannot "
                f"resume into an already-touched target directory. Inspect {new_datadir}, "
                "remove it once you're sure nothing there is worth keeping, then reboot to "
                "retry."
            )
        return False

    if read_pg_version(old_datadir) is None:
        return False

    validate_old_version(old_datadir, old_schema)

    if not upgrade_needed(old_schema, new_schema):
        return False

    resolved_mode = resolve_transfer_mode(
        transfer_mode,
        old_datadir=old_datadir,
        new_datadir_parent=os.path.dirname(new_datadir),
    )

    record_upgrade_attempt_started(completion_state_file, old_datadir)
    initdb_new_cluster(new_bindir, new_datadir, superuser, initdb_args=initdb_args)
    run_pg_upgrade(
        old_bindir=old_bindir,
        new_bindir=new_bindir,
        old_datadir=old_datadir,
        new_datadir=new_datadir,
        transfer_mode=resolved_mode,
        superuser=superuser,
        jobs=jobs,
    )
    record_upgrade_attempt_completed(completion_state_file)

    return True


def cleanup_old_datadir_if_due(
    completion_state_file: str, retention_days: int | None, *, now: datetime | None = None
) -> bool:
    """The separate timer's own job (docs/decisions/0011): removes the
    retained old data directory once oldDataDirRetentionDays's *positive*
    window has elapsed. Can't be folded into run_upgrade() above --
    that function only ever runs once, at upgrade time, while a
    positive retention window is defined entirely in terms of calendar
    time elapsing *afterward*, independent of any particular boot.
    (retention_days=0 is cleanup_old_datadir_now_if_zero_retention()'s
    job instead -- see that function's own docstring for why.)

    An incomplete attempt (completed_at is None) is NEVER eligible for
    cleanup, regardless of retention_days or how much calendar time has
    passed: it's the only copy of the real data left after a failed
    migration, and IncompleteUpgradeError is what's supposed to force
    manual intervention -- not an unattended rm -rf racing against an
    operator trying to diagnose the failure.

    Returns True iff the directory was actually removed this call;
    False for every no-op case: nothing recorded yet, not yet
    completed, not due yet, or already removed by an earlier call of
    this same timer (rmtree's own FileNotFoundError here is the
    expected steady state afterward, not an error)."""
    attempt = read_upgrade_attempt(completion_state_file)
    if attempt is None or attempt.completed_at is None:
        return False
    if not old_datadir_due_for_cleanup(attempt.completed_at, retention_days, now=now):
        return False
    try:
        shutil.rmtree(attempt.old_datadir)
    except FileNotFoundError:
        return False
    return True


def cleanup_old_datadir_now_if_zero_retention(
    completion_state_file: str, retention_days: int | None
) -> bool:
    """The retention_days=0 ("delete immediately") case
    (docs/decisions/0011) -- called by main.run(), AFTER it has already
    proven the new cluster is up and reachable (a live connection
    succeeded), NOT inline inside run_upgrade() itself. run_upgrade()
    runs strictly before postgresql.service ever starts; deleting the
    only remaining copy of the real data before the new cluster has
    even been proven to start once -- which the original design did --
    trades a one-time disk-space saving for total data loss if
    postgresql.service then fails to start for any unrelated reason
    (a bad setting, a missing extension introduced in the same
    nixos-rebuild switch).

    Same incomplete-attempt guard as cleanup_old_datadir_if_due() above
    -- never removes an attempt that started but never completed.
    Idempotent: a no-op on every boot after the first (rmtree's own
    FileNotFoundError), so it's safe to call unconditionally on every
    run() regardless of whether this is the first boot after the
    upgrade or the hundredth.

    A real removal failure is logged (not silently swallowed like the
    original inline version this replaced) -- an operator relying on
    retention=0 to reclaim disk space needs to know when it isn't
    actually happening."""
    if retention_days != 0:
        return False
    attempt = read_upgrade_attempt(completion_state_file)
    if attempt is None or attempt.completed_at is None:
        return False
    try:
        shutil.rmtree(attempt.old_datadir)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning(
            "failed to remove retained old data directory %s (oldDataDirRetentionDays=0)",
            attempt.old_datadir,
            exc_info=True,
        )
        return False
    return True

