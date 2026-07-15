"""Focused tests for snapshot inventory — ABI path safety and root reparse-point rejection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from re_agent.project import snapshot as _snapshot_mod
from re_agent.project.model import SnapshotFile
from re_agent.project.snapshot import (
    SnapshotError,
    _is_reparse_point,
    _validate_abi_path,
    inventory_snapshot,
)

# Number of JSON files in a _make_snapshot() snapshot (excluding snapshot.sha256).
_SNAPSHOT_FILE_COUNT = 3  # abi.json + analysis-metadata.json + sub/extra.json


# ── helpers ───────────────────────────────────────────────────────────────


def _make_snapshot(root: Path, *, abi_manifest_path: str = "abi.json") -> Path:
    """Create a minimal valid snapshot directory and return *root*."""
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "extra.json").write_text('{"extra": true}', encoding="utf-8")
    (root / abi_manifest_path).write_text(
        json.dumps({"version": "1.0.0", "architecture": "x86", "symbols": []}),
        encoding="utf-8",
    )
    metadata = {
        "schema_version": "1",
        "backend": "offline-export",
        "binary_sha256": "a" * 64,
        "abi_manifest_path": abi_manifest_path,
    }
    (root / "analysis-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


def _entries_for(root: Path) -> tuple[SnapshotFile, ...]:
    """Return inventory entries that would be produced for *root*.

    Mirrors the logic in ``inventory_snapshot`` so tests can supply a
    matching tuple to ``_validate_abi_path`` without calling the full
    inventory pipeline.
    """
    entries: list[SnapshotFile] = []
    for item in sorted(root.rglob("*")):
        if item.is_dir() or not item.is_file():
            continue
        if item.name == "analysis-metadata.json":
            continue
        rel = PurePosixPath(item.relative_to(root).as_posix())
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        entries.append(SnapshotFile(rel, digest, item.stat().st_size))
    return tuple(entries)


# ── _validate_abi_path ────────────────────────────────────────────────────


class TestValidateAbiPath:
    """Direct unit tests for the new ABI path validation function."""

    def test_valid_path(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        result = _validate_abi_path(root, "abi.json", entries)
        assert result == PurePosixPath("abi.json")

    def test_valid_nested_path(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap", abi_manifest_path="sub/abi.json")
        entries = _entries_for(root)
        result = _validate_abi_path(root, "sub/abi.json", entries)
        assert result == PurePosixPath("sub/abi.json")

    def test_drive_letter_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="drive letter"):
            _validate_abi_path(root, "C:/abi.json", entries)

    def test_drive_letter_lowercase_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="drive letter"):
            _validate_abi_path(root, "d:abi.json", entries)

    def test_backslash_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="backslash"):
            _validate_abi_path(root, "sub\\abi.json", entries)

    def test_unc_forward_slashes_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="UNC path"):
            _validate_abi_path(root, "//server/share/abi.json", entries)

    def test_unc_backslash_path_rejected(self, tmp_path: Path) -> None:
        """A path starting with ``\\\\`` (UNC) contains backslashes and is rejected.

        The backslash check fires first, so the error is ``backslash``
        not ``UNC``, which is fine — both are correct safety rejections.
        """
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="backslash"):
            _validate_abi_path(root, "\\\\server\\share\\abi.json", entries)

    def test_absolute_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="absolute"):
            _validate_abi_path(root, "/etc/abi.json", entries)

    def test_current_dir_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="root directory itself"):
            _validate_abi_path(root, ".", entries)

    def test_parent_traversal_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="parent traversal"):
            _validate_abi_path(root, "../outside/abi.json", entries)

    def test_parent_traversal_nested_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="parent traversal"):
            _validate_abi_path(root, "sub/../../outside/abi.json", entries)

    def test_missing_from_inventory_rejected(self, tmp_path: Path) -> None:
        """A real file that is absent from the inventory entries is rejected."""
        root = _make_snapshot(tmp_path / "snap")
        (root / "ghost.json").write_text('{"ghost": true}', encoding="utf-8")
        # Build entries that *exclude* ghost.json.
        all_entries = _entries_for(root)
        filtered = tuple(e for e in all_entries if str(e.path) != "ghost.json")
        with pytest.raises(SnapshotError, match="not in snapshot inventory"):
            _validate_abi_path(root, "ghost.json", filtered)

    def test_nonexistent_path_caught_by_resolve(self, tmp_path: Path) -> None:
        """A non-existent path fails ``resolve(strict=True)`` before the inventory check."""
        root = _make_snapshot(tmp_path / "snap")
        entries = _entries_for(root)
        with pytest.raises(SnapshotError, match="cannot be resolved"):
            _validate_abi_path(root, "nonexistent.json", entries)


# ── inventory_snapshot — ABI path integration ─────────────────────────────


class TestInventorySnapshotAbiPath:
    """The new validation runs as part of ``inventory_snapshot``."""

    def test_valid_snapshot_passes(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        metadata, files = inventory_snapshot(root)
        assert metadata.abi_manifest_path == PurePosixPath("abi.json")
        assert len(files) == _SNAPSHOT_FILE_COUNT

    def test_valid_nested_abi_passes(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap", abi_manifest_path="sub/abi.json")
        metadata, files = inventory_snapshot(root)
        assert metadata.abi_manifest_path == PurePosixPath("sub/abi.json")
        assert len(files) == _SNAPSHOT_FILE_COUNT

    def test_drive_letter_fails_inventory(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        md = json.loads((root / "analysis-metadata.json").read_text(encoding="utf-8"))
        md["abi_manifest_path"] = "C:/abi.json"
        (root / "analysis-metadata.json").write_text(json.dumps(md), encoding="utf-8")
        with pytest.raises(SnapshotError, match="drive letter"):
            inventory_snapshot(root)

    def test_backslash_fails_inventory(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        md = json.loads((root / "analysis-metadata.json").read_text(encoding="utf-8"))
        md["abi_manifest_path"] = "sub\\abi.json"
        (root / "analysis-metadata.json").write_text(json.dumps(md), encoding="utf-8")
        with pytest.raises(SnapshotError, match="backslash"):
            inventory_snapshot(root)

    def test_unc_fails_inventory(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        md = json.loads((root / "analysis-metadata.json").read_text(encoding="utf-8"))
        md["abi_manifest_path"] = "//server/share/abi.json"
        (root / "analysis-metadata.json").write_text(json.dumps(md), encoding="utf-8")
        with pytest.raises(SnapshotError, match="UNC path"):
            inventory_snapshot(root)

    def test_path_not_in_inventory_fails(self, tmp_path: Path) -> None:
        """ABI path references a non-existent file — caught by resolve containment."""
        root = _make_snapshot(tmp_path / "snap")
        md = json.loads((root / "analysis-metadata.json").read_text(encoding="utf-8"))
        md["abi_manifest_path"] = "ghost.json"
        (root / "analysis-metadata.json").write_text(json.dumps(md), encoding="utf-8")
        with pytest.raises(SnapshotError, match="cannot be resolved"):
            inventory_snapshot(root)

    def test_parent_traversal_fails_inventory(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        md = json.loads((root / "analysis-metadata.json").read_text(encoding="utf-8"))
        md["abi_manifest_path"] = "../outside/abi.json"
        (root / "analysis-metadata.json").write_text(json.dumps(md), encoding="utf-8")
        with pytest.raises(SnapshotError, match="parent traversal"):
            inventory_snapshot(root)


# ── inventory_snapshot — root reparse-point rejection ─────────────────


class TestInventorySnapshotRootReparsePoint:
    """Root reparse-point check (mocks ``_is_reparse_point``)."""

    def test_root_symlink_is_rejected(self, tmp_path: Path) -> None:
        """Symlink root was already rejected — confirm unchanged."""
        real = tmp_path / "real"
        real.mkdir()
        _make_snapshot(real)
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("no privilege to create symlinks on this host")
        with pytest.raises(SnapshotError, match="real directory"):
            inventory_snapshot(link)

    def test_root_junction_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A root with REPARSE_POINT attribute is rejected.

        We mock ``_is_reparse_point`` to return ``True`` for the root path
        and delegate to the real implementation for all other paths.
        """
        root = _make_snapshot(tmp_path / "snap")
        target_root = root.resolve()
        real_fn = _snapshot_mod._is_reparse_point

        def _selective(p: Path) -> bool:
            return True if p.resolve() == target_root else real_fn(p)

        monkeypatch.setattr(_snapshot_mod, "_is_reparse_point", _selective)
        with pytest.raises(SnapshotError, match="reparse point"):
            inventory_snapshot(root)

    def test_root_not_reparse_point_passes(self, tmp_path: Path) -> None:
        """A normal directory (no reparse) passes the check on any platform."""
        root = _make_snapshot(tmp_path / "snap")
        metadata, files = inventory_snapshot(root)
        assert metadata.abi_manifest_path == PurePosixPath("abi.json")
        assert len(files) == _SNAPSHOT_FILE_COUNT


# ── _is_reparse_point helper ──────────────────────────────────────


class TestIsReparsePoint:
    """Direct unit tests for the portable reparse-point helper."""

    def test_returns_false_on_current_platform(self, tmp_path: Path) -> None:
        """On any platform, a regular directory is never classified as a reparse point."""
        d = tmp_path / "plain_dir"
        d.mkdir()
        assert _is_reparse_point(d) is False


# ── child reparse-point rejection ─────────────────────────────────────────


class TestInventorySnapshotChildReparsePoints:
    """Child reparse points were already checked — confirm unchanged."""

    def test_child_symlink_rejected(self, tmp_path: Path) -> None:
        root = _make_snapshot(tmp_path / "snap")
        (root / "abi.json").unlink()
        outside = tmp_path / "outside.json"
        outside.write_text('{"fake": true}', encoding="utf-8")
        try:
            (root / "abi.json").symlink_to(outside)
        except OSError:
            pytest.skip("no privilege to create symlinks on this host")
        with pytest.raises(SnapshotError, match="links are not allowed|reparse points are not allowed"):
            inventory_snapshot(root)

    def test_junction_dir_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A directory child with REPARSE_POINT attribute is rejected.

        We mock ``_is_reparse_point`` to return ``True`` for the target child
        directory only.
        """
        root = _make_snapshot(tmp_path / "snap")
        reparse_dir = root / "evil_link_dir"
        reparse_dir.mkdir()

        target = reparse_dir.resolve()
        real_fn = _snapshot_mod._is_reparse_point

        def _selective(p: Path) -> bool:
            return True if p.resolve() == target else real_fn(p)

        monkeypatch.setattr(_snapshot_mod, "_is_reparse_point", _selective)
        with pytest.raises(SnapshotError, match="links are not allowed|reparse points are not allowed"):
            inventory_snapshot(root)
