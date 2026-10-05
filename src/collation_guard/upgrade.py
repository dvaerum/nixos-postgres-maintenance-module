"""PostgreSQL major-version upgrade: detection, validation, and preflight
checks for whether/how to run pg_upgrade (docs/decisions/0010). Deliberately
split from the actual initdb/pg_upgrade orchestration (not yet implemented
here): every function below is pure or near-pure (reads real on-disk/
filesystem state, makes no irreversible change), so it's fully covered by
the fast pytest tier with no second real Postgres binary needed -- the
orchestration itself is proven separately against a real two-major-version
cluster (see the heavy-tier packages.pgUpgradeTest, not wired into this
fast tier -- same precedent as docs/decisions/0008's icuDriftTest).
"""

from __future__ import annotations

import os
import shutil
import subprocess


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

