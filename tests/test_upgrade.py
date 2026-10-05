"""Fast tier: upgrade.py's detection/validation/preflight logic for
docs/decisions/0010 (PostgreSQL major-version upgrade support). Pure
functions wherever possible -- no second real Postgres binary needed
here; the full initdb/pg_upgrade orchestration is proven separately
against a real two-major-version cluster (see the heavy-tier
packages.pgUpgradeTest, not wired into this fast tier -- same
precedent as docs/decisions/0008's icuDriftTest).
"""

from __future__ import annotations

import subprocess
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
