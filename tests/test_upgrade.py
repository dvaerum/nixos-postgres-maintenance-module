"""Fast tier: upgrade.py's detection/validation/preflight/orchestration
logic for docs/decisions/0010 and 0011 (PostgreSQL major-version
upgrade support). Pure functions wherever possible; the orchestration
functions (initdb_new_cluster/run_pg_upgrade) mock subprocess.run
throughout -- what's proven here is the argv/error-handling contract,
not a real initdb/pg_upgrade binary. The full orchestration against a
real two-major-version cluster is proven separately (see the
heavy-tier packages.pgUpgradeTest, not wired into this fast tier --
same precedent as docs/decisions/0008's icuDriftTest).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from collation_guard import upgrade


def test_read_pg_version_returns_the_single_line_major_version(tmp_path: Path) -> None:
    (tmp_path / "PG_VERSION").write_text("15\n")
    assert upgrade.read_pg_version(str(tmp_path)) == "15"


def test_read_pg_version_returns_none_when_missing(tmp_path: Path) -> None:
    """A missing PG_VERSION means an uninitialized/fresh data directory --
    not an error, just "nothing to validate against yet"."""
    assert upgrade.read_pg_version(str(tmp_path)) is None


def test_read_pg_version_strips_surrounding_whitespace(tmp_path: Path) -> None:
    (tmp_path / "PG_VERSION").write_text("16\n\n")
    assert upgrade.read_pg_version(str(tmp_path)) == "16"


def test_validate_old_version_passes_when_on_disk_matches_expected(tmp_path: Path) -> None:
    (tmp_path / "PG_VERSION").write_text("15\n")
    upgrade.validate_old_version(str(tmp_path), "15")  # must not raise


def test_validate_old_version_raises_on_mismatch(tmp_path: Path) -> None:
    """The real on-disk version is authoritative -- a misconfigured
    oldPackage (or a cluster that's secretly a different version than
    assumed) must never be silently trusted, since what follows is
    irreversible (pg_upgrade)."""
    (tmp_path / "PG_VERSION").write_text("15\n")
    with pytest.raises(upgrade.VersionMismatchError, match="15.*16|16.*15"):
        upgrade.validate_old_version(str(tmp_path), "16")


def test_validate_old_version_is_a_no_op_when_no_cluster_exists_yet(tmp_path: Path) -> None:
    """A missing PG_VERSION is not a mismatch -- it's "no old cluster
    to validate against," a distinct, non-error state the caller
    decides what to do with (e.g. upgrade.enable configured ahead of
    the first-ever boot)."""
    upgrade.validate_old_version(str(tmp_path), "15")  # must not raise


def test_upgrade_needed_true_when_schemas_differ() -> None:
    assert upgrade.upgrade_needed("15", "16") is True


def test_upgrade_needed_false_when_schemas_match() -> None:
    assert upgrade.upgrade_needed("16", "16") is False


def test_same_filesystem_true_for_two_dirs_on_the_same_mount(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert upgrade.same_filesystem(str(a), str(b)) is True


def test_same_filesystem_false_across_distinct_mounts(tmp_path: Path) -> None:
    # /dev and a regular tmp_path directory are essentially guaranteed to
    # be on different filesystems in any real environment (devtmpfs vs.
    # whatever backs tmp_path) -- a cheap, portable way to get two
    # genuinely different st_dev values without needing to mount anything.
    assert upgrade.same_filesystem("/dev", str(tmp_path)) is False


def test_reflink_supported_true_when_the_probe_copy_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empirically probes via `cp --reflink=always` rather than trusting a
    hardcoded filesystem/kernel-version list -- that list itself drifts
    (new filesystems/kernels gain support, distro configs vary). The
    subprocess call is mocked here since real reflink support is
    filesystem-dependent and can't be guaranteed in a test environment;
    see test_reflink_supported_runs_without_error_in_this_real_environment
    below for the real (outcome-agnostic) smoke test."""
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    assert upgrade.reflink_supported(str(tmp_path), str(tmp_path)) is True
    assert recorded["args"][:2] == ["cp", "--reflink=always"]


def test_reflink_supported_false_when_the_probe_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, returncode=1)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    assert upgrade.reflink_supported(str(tmp_path), str(tmp_path)) is False


def test_reflink_supported_cleans_up_its_probe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # Simulate what a real `cp` would do on failure -- no destination
        # file created -- so cleanup must tolerate a missing dst.
        return subprocess.CompletedProcess(args, returncode=1)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.reflink_supported(str(tmp_path), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_reflink_supported_runs_without_error_in_this_real_environment(tmp_path: Path) -> None:
    """Outcome-agnostic (reflink support is filesystem-dependent and not
    guaranteed in any given test environment) -- just proves the real,
    unmocked probe doesn't itself crash and returns a real bool."""
    result = upgrade.reflink_supported(str(tmp_path), str(tmp_path))
    assert isinstance(result, bool)
    assert list(tmp_path.iterdir()) == []


def test_check_disk_space_passes_when_plenty_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    (old_datadir / "base.dat").write_bytes(b"x" * 1000)
    new_parent = tmp_path / "new_parent"
    new_parent.mkdir()

    monkeypatch.setattr(
        upgrade.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=10**12, used=0, free=10**12),
    )
    upgrade.check_disk_space(str(old_datadir), str(new_parent))  # must not raise


def test_check_disk_space_raises_when_not_enough_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    (old_datadir / "base.dat").write_bytes(b"x" * 1000)
    new_parent = tmp_path / "new_parent"
    new_parent.mkdir()

    # required_multiplier defaults to 2x -- 1000 bytes old data needs
    # ~2000 bytes free; only 500 is available.
    monkeypatch.setattr(
        upgrade.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=10**12, used=0, free=500),
    )
    with pytest.raises(upgrade.InsufficientDiskSpaceError):
        upgrade.check_disk_space(str(old_datadir), str(new_parent))


def test_check_disk_space_sums_nested_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old data directory has real subdirectories (base/, pg_wal/,
    etc.) -- the size estimate must walk recursively, not just sum the
    top-level entries."""
    old_datadir = tmp_path / "old"
    (old_datadir / "base" / "1").mkdir(parents=True)
    (old_datadir / "base" / "1" / "16384").write_bytes(b"x" * 5000)
    (old_datadir / "pg_wal").mkdir()
    (old_datadir / "pg_wal" / "000000010000000000000001").write_bytes(b"x" * 5000)
    new_parent = tmp_path / "new_parent"
    new_parent.mkdir()

    # Total nested data is 10000 bytes; default 2x multiplier needs
    # ~20000. Exactly 15000 free must still fail.
    monkeypatch.setattr(
        upgrade.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=10**12, used=0, free=15000),
    )
    with pytest.raises(upgrade.InsufficientDiskSpaceError):
        upgrade.check_disk_space(str(old_datadir), str(new_parent))


def _patch_preflight(
    monkeypatch: pytest.MonkeyPatch, *, same_fs: bool, reflink: bool, plenty_of_space: bool = True
) -> None:
    def fake_check_disk_space(*args: object, **kwargs: object) -> None:
        if not plenty_of_space:
            raise upgrade.InsufficientDiskSpaceError("no space")

    monkeypatch.setattr(upgrade, "same_filesystem", lambda a, b: same_fs)
    monkeypatch.setattr(upgrade, "reflink_supported", lambda a, b: reflink)
    monkeypatch.setattr(upgrade, "check_disk_space", fake_check_disk_space)


def test_resolve_transfer_mode_copy_runs_its_own_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=False, reflink=False, plenty_of_space=True)
    result = upgrade.resolve_transfer_mode("copy", old_datadir="/old", new_datadir_parent="/new")
    assert result == "copy"


def test_resolve_transfer_mode_copy_raises_on_insufficient_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=False, reflink=False, plenty_of_space=False)
    with pytest.raises(upgrade.InsufficientDiskSpaceError):
        upgrade.resolve_transfer_mode("copy", old_datadir="/old", new_datadir_parent="/new")


def test_resolve_transfer_mode_clone_succeeds_when_preflight_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=True, reflink=True)
    result = upgrade.resolve_transfer_mode("clone", old_datadir="/old", new_datadir_parent="/new")
    assert result == "clone"


def test_resolve_transfer_mode_clone_raises_loudly_when_not_same_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EXPLICIT (non-"auto") request that fails its own preflight must
    raise, not silently substitute a different mode -- only "auto" is
    allowed to make that substitution on the caller's behalf."""
    _patch_preflight(monkeypatch, same_fs=False, reflink=True)
    with pytest.raises(upgrade.TransferModeUnavailableError, match="filesystem"):
        upgrade.resolve_transfer_mode("clone", old_datadir="/old", new_datadir_parent="/new")


def test_resolve_transfer_mode_clone_raises_loudly_when_reflink_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=True, reflink=False)
    with pytest.raises(upgrade.TransferModeUnavailableError, match="reflink"):
        upgrade.resolve_transfer_mode("clone", old_datadir="/old", new_datadir_parent="/new")


def test_resolve_transfer_mode_link_succeeds_when_same_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=True, reflink=False)
    result = upgrade.resolve_transfer_mode("link", old_datadir="/old", new_datadir_parent="/new")
    assert result == "link"


def test_resolve_transfer_mode_link_raises_loudly_when_not_same_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=False, reflink=False)
    with pytest.raises(upgrade.TransferModeUnavailableError, match="filesystem"):
        upgrade.resolve_transfer_mode("link", old_datadir="/old", new_datadir_parent="/new")


def test_resolve_transfer_mode_auto_prefers_clone_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=True, reflink=True)
    result = upgrade.resolve_transfer_mode("auto", old_datadir="/old", new_datadir_parent="/new")
    assert result == "clone"


def test_resolve_transfer_mode_auto_falls_back_to_copy_when_clone_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_preflight(monkeypatch, same_fs=False, reflink=False, plenty_of_space=True)
    result = upgrade.resolve_transfer_mode("auto", old_datadir="/old", new_datadir_parent="/new")
    assert result == "copy"


def test_resolve_transfer_mode_auto_never_falls_back_to_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"auto" only ever resolves to clone or copy -- link's "no undo once
    the new cluster starts" story means it must only ever run on an
    explicit, deliberate request (docs/decisions/0010)."""
    _patch_preflight(monkeypatch, same_fs=True, reflink=False, plenty_of_space=False)
    with pytest.raises(upgrade.InsufficientDiskSpaceError):
        upgrade.resolve_transfer_mode("auto", old_datadir="/old", new_datadir_parent="/new")


def test_resolve_transfer_mode_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="nonsense"):
        upgrade.resolve_transfer_mode("nonsense", old_datadir="/old", new_datadir_parent="/new")


# -- initdb_new_cluster() / run_pg_upgrade(): orchestration (docs/decisions/0010,
# docs/decisions/0011) -- subprocess is always mocked here, same as
# reflink_supported()'s own tests above: a real pg_upgrade/initdb invocation
# needs a second real Postgres major-version binary, proven separately by the
# heavy-tier packages.pgUpgradeTest (not yet implemented), not this fast tier.


def test_initdb_new_cluster_invokes_the_new_binarys_own_initdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.initdb_new_cluster("/new/bin", "/new/data", "postgres")
    assert recorded["args"] == [
        "/new/bin/initdb",
        "-D",
        "/new/data",
        "-U",
        "postgres",
    ]


def test_initdb_new_cluster_passes_through_extra_initdb_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors nixpkgs's own postgresql.service preStart, which passes
    services.postgresql.initdbArgs through unchanged -- the resulting
    fresh cluster must be indistinguishable from one NixOS would have
    initialized itself, so the same extra args have to reach initdb
    here too."""
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.initdb_new_cluster(
        "/new/bin", "/new/data", "postgres", initdb_args=["--data-checksums"]
    )
    assert recorded["args"][-1] == "--data-checksums"


def test_initdb_new_cluster_raises_with_stderr_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.InitdbFailedError, match="boom"):
        upgrade.initdb_new_cluster("/new/bin", "/new/data", "postgres")


def test_run_pg_upgrade_builds_the_expected_argv_for_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"copy" is pg_upgrade's own default -- no mode flag at all, unlike
    "clone"/"link" below."""
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run_pg_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir="/old/data",
        new_datadir="/new/data",
        transfer_mode="copy",
        superuser="postgres",
    )
    assert recorded["args"] == [
        "/new/bin/pg_upgrade",
        "--old-bindir",
        "/old/bin",
        "--new-bindir",
        "/new/bin",
        "--old-datadir",
        "/old/data",
        "--new-datadir",
        "/new/data",
        "--username",
        "postgres",
    ]
    # Writes its own log files alongside the new cluster, not wherever
    # the calling process happened to start -- the new datadir is
    # guaranteed writable (initdb_new_cluster() just created it).
    assert recorded["kwargs"]["cwd"] == "/new/data"


def test_run_pg_upgrade_adds_the_clone_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run_pg_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir="/old/data",
        new_datadir="/new/data",
        transfer_mode="clone",
        superuser="postgres",
    )
    assert "--clone" in recorded["args"]


def test_run_pg_upgrade_adds_the_link_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run_pg_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir="/old/data",
        new_datadir="/new/data",
        transfer_mode="link",
        superuser="postgres",
    )
    assert "--link" in recorded["args"]


def test_run_pg_upgrade_passes_jobs_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run_pg_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir="/old/data",
        new_datadir="/new/data",
        transfer_mode="copy",
        superuser="postgres",
        jobs=4,
    )
    assert recorded["args"][-2:] == ["--jobs", "4"]


def test_run_pg_upgrade_rejects_auto_as_an_unresolved_mode() -> None:
    """"auto" is resolve_transfer_mode()'s own job to resolve away --
    pg_upgrade itself has no such mode, so a caller that forgets to
    resolve it first must fail loudly, not silently pass "auto" through
    as a literal (nonsense) argv token."""
    with pytest.raises(ValueError, match="auto"):
        upgrade.run_pg_upgrade(
            old_bindir="/old/bin",
            new_bindir="/new/bin",
            old_datadir="/old/data",
            new_datadir="/new/data",
            transfer_mode="auto",
            superuser="postgres",
        )


def test_run_pg_upgrade_raises_with_stderr_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="catalog mismatch")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.PgUpgradeFailedError, match="catalog mismatch"):
        upgrade.run_pg_upgrade(
            old_bindir="/old/bin",
            new_bindir="/new/bin",
            old_datadir="/old/data",
            new_datadir="/new/data",
            transfer_mode="copy",
            superuser="postgres",
        )


# -- Upgrade-attempt tracking + time-gated old-dataDir retention
# (docs/decisions/0010, 0011) -- one state file, written in two steps:
# record_upgrade_attempt_started() before initdb ever touches
# new_datadir, record_upgrade_attempt_completed() only once pg_upgrade
# has actually succeeded. The gap between those two writes is exactly
# what distinguishes "a previous attempt started but never finished"
# from "genuinely done" -- see run_upgrade()'s own tests further down.


def test_record_attempt_started_then_read_round_trips(tmp_path: Path) -> None:
    state_file = str(tmp_path / "upgrade-attempt.json")
    started_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, "/old/data", now=started_at)

    result = upgrade.read_upgrade_attempt(state_file)
    assert result is not None
    assert result.old_datadir == "/old/data"
    assert result.started_at == started_at
    assert result.completed_at is None


def test_record_attempt_completed_sets_completed_at_without_losing_old_datadir(
    tmp_path: Path,
) -> None:
    state_file = str(tmp_path / "upgrade-attempt.json")
    started_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    completed_at = datetime(2026, 1, 15, 12, 5, 0, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, "/old/data", now=started_at)
    upgrade.record_upgrade_attempt_completed(state_file, now=completed_at)

    result = upgrade.read_upgrade_attempt(state_file)
    assert result is not None
    assert result.old_datadir == "/old/data"
    assert result.started_at == started_at
    assert result.completed_at == completed_at


def test_read_upgrade_attempt_returns_none_when_missing(tmp_path: Path) -> None:
    assert upgrade.read_upgrade_attempt(str(tmp_path / "nope.json")) is None


def test_record_upgrade_attempt_started_writes_atomically(tmp_path: Path) -> None:
    """Temp file + os.replace, same directory -- same pattern as 0009's
    connection-limit state file: a process killed mid-write must never
    leave a corrupt file behind for a later read to choke on."""
    state_file = str(tmp_path / "upgrade-attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, "/old/data")
    assert list(tmp_path.iterdir()) == [tmp_path / "upgrade-attempt.json"]


def test_record_upgrade_attempt_completed_also_writes_atomically(tmp_path: Path) -> None:
    state_file = str(tmp_path / "upgrade-attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, "/old/data")
    upgrade.record_upgrade_attempt_completed(state_file)
    assert list(tmp_path.iterdir()) == [tmp_path / "upgrade-attempt.json"]


def test_old_datadir_due_for_cleanup_never_when_retention_is_none() -> None:
    completed_at = datetime(2020, 1, 1, tzinfo=UTC)
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    assert upgrade.old_datadir_due_for_cleanup(completed_at, None, now=far_future) is False


def test_old_datadir_due_for_cleanup_immediately_when_retention_is_zero() -> None:
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert upgrade.old_datadir_due_for_cleanup(completed_at, 0, now=completed_at) is True


def test_old_datadir_due_for_cleanup_before_the_window_elapses() -> None:
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    nine_days_later = completed_at + timedelta(days=9)
    assert upgrade.old_datadir_due_for_cleanup(completed_at, 10, now=nine_days_later) is False


def test_old_datadir_due_for_cleanup_once_the_window_elapses() -> None:
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    ten_days_later = completed_at + timedelta(days=10)
    assert upgrade.old_datadir_due_for_cleanup(completed_at, 10, now=ten_days_later) is True


# -- run_upgrade(): the top-level entry point the
# postgresql-collation-guard-upgrade.service unit calls (docs/decisions/0011).
# Real tmp_path filesystem state drives the (cheap) gating logic
# (read_pg_version/validate_old_version/read_upgrade_attempt); only the
# actually-external pieces (resolve_transfer_mode's own preflight,
# initdb_new_cluster, run_pg_upgrade) are mocked. retention/cleanup is no
# longer run_upgrade()'s concern at all -- see
# cleanup_old_datadir_now_if_zero_retention()'s own tests further down for
# why that moved to main.run(), after a verified live connection.


def _make_cluster(path: Path, schema: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "PG_VERSION").write_text(f"{schema}\n")


def test_run_upgrade_is_a_noop_when_new_datadir_already_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent by construction (docs/decisions/0011): upgrade.enable
    can stay true across any number of subsequent boots with no repeat
    effect, once a genuinely-completed attempt is on record."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")
    _make_cluster(new_datadir, "16")
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    called: list[str] = []
    monkeypatch.setattr(upgrade, "initdb_new_cluster", lambda *a, **k: called.append("initdb"))
    monkeypatch.setattr(upgrade, "run_pg_upgrade", lambda *a, **k: called.append("pg_upgrade"))

    result = upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=state_file,
    )
    assert result is False
    assert called == []


def test_run_upgrade_is_a_noop_when_new_datadir_predates_any_upgrade_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """new_datadir can have a PG_VERSION with NO attempt record at all --
    e.g. upgrade.enable was turned on after the new version was already
    running normally, independent of this feature entirely. Nothing to
    upgrade; must stay a safe no-op, not an error (see gap: this is
    deliberately distinct from test_run_upgrade_raises_when_new_datadir_
    exists_from_an_incomplete_attempt below, which DOES raise)."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")
    _make_cluster(new_datadir, "16")
    # No attempt.json at all -- new_datadir's PG_VERSION came from
    # somewhere entirely unrelated to this feature.

    called: list[str] = []
    monkeypatch.setattr(upgrade, "initdb_new_cluster", lambda *a, **k: called.append("initdb"))

    result = upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=str(tmp_path / "attempt.json"),
    )
    assert result is False
    assert called == []


def test_run_upgrade_raises_when_new_datadir_exists_from_an_incomplete_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE critical-severity gap this closes: a previous attempt ran
    initdb_new_cluster() (which writes new_datadir/PG_VERSION) and then
    run_pg_upgrade() failed -- new_datadir now has a PG_VERSION, but no
    completed_at was ever recorded. Before this fix, the NEXT run_upgrade()
    call would see "new_datadir has PG_VERSION" and silently return False,
    permanently treating a failed migration as a successful one --
    postgresql.service would then start cleanly against an empty/partial
    new cluster with zero signal anything was ever wrong. Must now raise
    loudly instead, every single time, until an operator intervenes."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")
    _make_cluster(new_datadir, "16")  # as if initdb_new_cluster() already ran
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    # Deliberately NOT completed -- this is the failed-midway state.

    called: list[str] = []
    monkeypatch.setattr(upgrade, "initdb_new_cluster", lambda *a, **k: called.append("initdb"))
    monkeypatch.setattr(upgrade, "run_pg_upgrade", lambda *a, **k: called.append("pg_upgrade"))

    with pytest.raises(upgrade.IncompleteUpgradeError, match=str(new_datadir)):
        upgrade.run_upgrade(
            old_bindir="/old/bin",
            new_bindir="/new/bin",
            old_datadir=str(old_datadir),
            new_datadir=str(new_datadir),
            old_schema="15",
            new_schema="16",
            superuser="postgres",
            completion_state_file=state_file,
        )
    # Must raise on EVERY call, not retry automatically -- pg_upgrade
    # itself requires a truly fresh target, not a half-touched one.
    assert called == []


def test_run_upgrade_incomplete_attempt_error_keeps_raising_on_repeated_calls(
    tmp_path: Path,
) -> None:
    """Not a one-shot raise-then-clear -- every subsequent invocation
    must keep raising until an operator manually removes new_datadir,
    confirming this can never silently resolve itself."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")
    _make_cluster(new_datadir, "16")
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))

    for _ in range(3):
        with pytest.raises(upgrade.IncompleteUpgradeError):
            upgrade.run_upgrade(
                old_bindir="/old/bin",
                new_bindir="/new/bin",
                old_datadir=str(old_datadir),
                new_datadir=str(new_datadir),
                old_schema="15",
                new_schema="16",
                superuser="postgres",
                completion_state_file=state_file,
            )


def test_run_upgrade_raises_when_old_and_new_datadir_are_identical(tmp_path: Path) -> None:
    """A real misconfiguration (oldDataDir and the configured
    services.postgresql.dataDir resolving to the same path) must be
    caught explicitly and FIRST -- before this fix, the identical path
    meant new_datadir's own PG_VERSION (really the old cluster's) was
    silently treated as "already upgraded", absorbing the
    misconfiguration into a no-op with zero diagnostic."""
    same_dir = tmp_path / "same"
    _make_cluster(same_dir, "15")

    with pytest.raises(upgrade.IdenticalDataDirectoriesError, match=str(same_dir)):
        upgrade.run_upgrade(
            old_bindir="/old/bin",
            new_bindir="/new/bin",
            old_datadir=str(same_dir),
            new_datadir=str(same_dir),
            old_schema="15",
            new_schema="16",
            superuser="postgres",
            completion_state_file=str(tmp_path / "attempt.json"),
        )


def test_run_upgrade_is_a_noop_when_there_is_no_old_cluster_yet(tmp_path: Path) -> None:
    """A brand-new host with upgrade.enable turned on ahead of the very
    first boot -- nothing to upgrade *from*, not an error. The upstream
    postgresql.service preStart initdb's new_datadir itself, same as if
    upgrade.enable were false."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    result = upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=str(tmp_path / "attempt.json"),
    )
    assert result is False


def test_run_upgrade_raises_on_old_version_mismatch(tmp_path: Path) -> None:
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "14")  # disagrees with the configured old_schema below
    with pytest.raises(upgrade.VersionMismatchError):
        upgrade.run_upgrade(
            old_bindir="/old/bin",
            new_bindir="/new/bin",
            old_datadir=str(old_datadir),
            new_datadir=str(new_datadir),
            old_schema="15",
            new_schema="16",
            superuser="postgres",
            completion_state_file=str(tmp_path / "attempt.json"),
        )


def test_run_upgrade_is_a_noop_when_old_and_new_schema_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "16")

    called: list[str] = []
    monkeypatch.setattr(upgrade, "initdb_new_cluster", lambda *a, **k: called.append("initdb"))

    result = upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="16",
        new_schema="16",
        superuser="postgres",
        completion_state_file=str(tmp_path / "attempt.json"),
    )
    assert result is False
    assert called == []


def test_run_upgrade_happy_path_calls_everything_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")

    call_order: list[str] = []

    def fake_resolve(requested: str, *, old_datadir: str, new_datadir_parent: str) -> str:
        call_order.append("resolve")
        return "clone"

    monkeypatch.setattr(upgrade, "resolve_transfer_mode", fake_resolve)
    monkeypatch.setattr(upgrade, "initdb_new_cluster", lambda *a, **k: call_order.append("initdb"))
    monkeypatch.setattr(
        upgrade, "run_pg_upgrade", lambda *a, **k: call_order.append("pg_upgrade")
    )

    state_file = str(tmp_path / "attempt.json")
    result = upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=state_file,
    )
    assert result is True
    assert call_order == ["resolve", "initdb", "pg_upgrade"]

    # The attempt was recorded both as started (before initdb) and
    # completed (after pg_upgrade) -- not just a bare "it worked" bool.
    attempt = upgrade.read_upgrade_attempt(state_file)
    assert attempt is not None
    assert attempt.old_datadir == str(old_datadir)
    assert attempt.completed_at is not None


def test_run_upgrade_records_attempt_started_before_initdb_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If initdb itself crashes the whole process (not just raises a
    caught exception -- e.g. OOM-killed), the started record must
    already be on disk, or a retry could never distinguish this from
    "nothing was ever attempted" and skip the IncompleteUpgradeError
    guard entirely."""
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")
    state_file = str(tmp_path / "attempt.json")

    monkeypatch.setattr(upgrade, "resolve_transfer_mode", lambda *a, **k: "copy")

    def fake_initdb(*a: object, **k: object) -> None:
        # At the moment initdb is invoked, the attempt must already be
        # on disk as "started".
        attempt = upgrade.read_upgrade_attempt(state_file)
        assert attempt is not None
        assert attempt.completed_at is None
        # Mirror the real initdb_new_cluster() side effect so the
        # idempotency gate behaves realistically for this test.
        _make_cluster(new_datadir, "16")

    monkeypatch.setattr(upgrade, "initdb_new_cluster", fake_initdb)
    monkeypatch.setattr(upgrade, "run_pg_upgrade", lambda *a, **k: None)

    upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=state_file,
    )


def test_run_upgrade_passes_resolved_mode_jobs_and_initdb_args_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_datadir = tmp_path / "old"
    new_datadir = tmp_path / "new"
    _make_cluster(old_datadir, "15")

    monkeypatch.setattr(upgrade, "resolve_transfer_mode", lambda *a, **k: "link")

    initdb_recorded: dict[str, object] = {}
    pg_upgrade_recorded: dict[str, object] = {}
    monkeypatch.setattr(
        upgrade, "initdb_new_cluster", lambda *a, **k: initdb_recorded.update(kwargs=k, args=a)
    )
    monkeypatch.setattr(upgrade, "run_pg_upgrade", lambda **k: pg_upgrade_recorded.update(k))

    upgrade.run_upgrade(
        old_bindir="/old/bin",
        new_bindir="/new/bin",
        old_datadir=str(old_datadir),
        new_datadir=str(new_datadir),
        old_schema="15",
        new_schema="16",
        superuser="postgres",
        completion_state_file=str(tmp_path / "attempt.json"),
        transfer_mode="link",
        jobs=4,
        initdb_args=["--data-checksums"],
    )
    assert pg_upgrade_recorded["transfer_mode"] == "link"
    assert pg_upgrade_recorded["jobs"] == 4
    assert initdb_recorded["kwargs"]["initdb_args"] == ["--data-checksums"]


# -- cleanup_old_datadir_if_due(): the separate timer-driven cleanup
# (docs/decisions/0011) -- real filesystem state throughout, no mocking
# needed since shutil.rmtree against a real tmp_path directory is cheap
# and exercises the actual removal path directly.


def test_cleanup_old_datadir_if_due_is_a_noop_when_nothing_recorded(tmp_path: Path) -> None:
    result = upgrade.cleanup_old_datadir_if_due(str(tmp_path / "nope.json"), 10)
    assert result is False


def test_cleanup_old_datadir_if_due_is_a_noop_before_the_window_elapses(tmp_path: Path) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir), now=completed_at)
    upgrade.record_upgrade_attempt_completed(state_file, now=completed_at)

    result = upgrade.cleanup_old_datadir_if_due(
        state_file, 10, now=completed_at + timedelta(days=5)
    )
    assert result is False
    assert old_datadir.exists()


def test_cleanup_old_datadir_if_due_removes_the_directory_once_due(tmp_path: Path) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    (old_datadir / "PG_VERSION").write_text("15\n")
    state_file = str(tmp_path / "attempt.json")
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir), now=completed_at)
    upgrade.record_upgrade_attempt_completed(state_file, now=completed_at)

    result = upgrade.cleanup_old_datadir_if_due(
        state_file, 10, now=completed_at + timedelta(days=10)
    )
    assert result is True
    assert not old_datadir.exists()


def test_cleanup_old_datadir_if_due_is_a_noop_when_retention_is_none(tmp_path: Path) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    result = upgrade.cleanup_old_datadir_if_due(state_file, None)
    assert result is False
    assert old_datadir.exists()


def test_cleanup_old_datadir_if_due_is_a_noop_for_an_incomplete_attempt(tmp_path: Path) -> None:
    """A failed/incomplete upgrade's old_datadir must NEVER be cleaned
    up by the timer, regardless of retention_days or how much calendar
    time has passed -- it's the only copy of the real data left, and
    IncompleteUpgradeError (see run_upgrade()'s own tests) is what's
    supposed to force manual intervention, not an unattended rm -rf."""
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    started_at = datetime(2020, 1, 1, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir), now=started_at)
    # Deliberately never completed.

    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    result = upgrade.cleanup_old_datadir_if_due(state_file, 0, now=far_future)
    assert result is False
    assert old_datadir.exists()


def test_cleanup_old_datadir_if_due_handles_an_already_removed_directory_gracefully(
    tmp_path: Path,
) -> None:
    """A second timer tick after the directory was already removed by
    the first one must not raise -- rmtree's own FileNotFoundError is
    exactly the expected steady state here, not an error."""
    old_datadir = tmp_path / "old"  # deliberately never created
    state_file = str(tmp_path / "attempt.json")
    completed_at = datetime(2026, 1, 1, tzinfo=UTC)
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir), now=completed_at)
    upgrade.record_upgrade_attempt_completed(state_file, now=completed_at)

    result = upgrade.cleanup_old_datadir_if_due(state_file, 0, now=completed_at)
    assert result is False


# -- cleanup_old_datadir_now_if_zero_retention(): the retention_days=0
# ("delete immediately") case (docs/decisions/0011) -- called by
# main.run(), AFTER it has already proven the new cluster is up and
# reachable (a live connection succeeded), not inline inside
# run_upgrade() itself (which runs strictly before postgresql.service
# ever starts -- deleting the only remaining copy of the real data
# before the new cluster has been proven to even start is the gap this
# closes; see docs/decisions/0011's "A positive-retention cleanup..."
# section).


def test_cleanup_now_if_zero_retention_removes_the_directory(tmp_path: Path) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    result = upgrade.cleanup_old_datadir_now_if_zero_retention(state_file, 0)
    assert result is True
    assert not old_datadir.exists()


def test_cleanup_now_if_zero_retention_is_a_noop_for_any_other_retention_value(
    tmp_path: Path,
) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    for retention in (None, 1, 30):
        assert upgrade.cleanup_old_datadir_now_if_zero_retention(state_file, retention) is False
    assert old_datadir.exists()


def test_cleanup_now_if_zero_retention_is_a_noop_when_nothing_completed_yet(
    tmp_path: Path,
) -> None:
    assert (
        upgrade.cleanup_old_datadir_now_if_zero_retention(str(tmp_path / "nope.json"), 0) is False
    )


def test_cleanup_now_if_zero_retention_never_removes_an_incomplete_attempts_directory(
    tmp_path: Path,
) -> None:
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    # Deliberately never completed.

    assert upgrade.cleanup_old_datadir_now_if_zero_retention(state_file, 0) is False
    assert old_datadir.exists()


def test_cleanup_now_if_zero_retention_handles_an_already_removed_directory_gracefully(
    tmp_path: Path,
) -> None:
    old_datadir = tmp_path / "old"  # deliberately never created
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    assert upgrade.cleanup_old_datadir_now_if_zero_retention(state_file, 0) is False


def test_cleanup_now_if_zero_retention_logs_and_swallows_a_real_removal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A removal failure (e.g. a stale NFS handle, a permission issue)
    must be visible in the journal, not silently discarded -- an
    operator relying on retention=0 to reclaim disk space needs to know
    when it isn't actually happening."""
    old_datadir = tmp_path / "old"
    old_datadir.mkdir()
    state_file = str(tmp_path / "attempt.json")
    upgrade.record_upgrade_attempt_started(state_file, str(old_datadir))
    upgrade.record_upgrade_attempt_completed(state_file)

    def fake_rmtree(path: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(upgrade.shutil, "rmtree", fake_rmtree)

    with caplog.at_level("WARNING"):
        result = upgrade.cleanup_old_datadir_now_if_zero_retention(state_file, 0)
    assert result is False
    assert any("denied" in r.message or "old" in r.message for r in caplog.records)
