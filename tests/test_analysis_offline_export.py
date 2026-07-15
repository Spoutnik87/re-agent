"""Tests for OfflineExportBackend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from re_agent.analysis.offline_export import OfflineExportBackend, OfflineExportError

# ── fixtures ──────────────────────────────────────────────────────────────


def _populate_snapshot(root: Path, *, version: str = "1.0.0") -> None:
    """Create a minimal valid analysis snapshot at *root*."""
    root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": version,
        "backend": "offline-export",
        "binary_sha256": "a" * 64,
        "abi_manifest_path": "abi_manifest.json",
    }
    (root / "analysis-metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    abi = {
        "format_version": 1,
        "version": version,
        "architecture": "x86",
        "pointer_size": 4,
        "symbols": [
            {
                "address": 4096,
                "name": "func_a",
                "signature": "void func_a()",
                "calling_convention": "cdecl",
                "output_path": "mod_a.cpp",
            }
        ],
    }
    (root / "abi_manifest.json").write_text(json.dumps(abi, sort_keys=True) + "\n", encoding="utf-8")
    data = {"func": "0x1000", "name": "func_a"}
    (root / "func_0x1000.json").write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")


# ── health_check ──────────────────────────────────────────────────────────


class TestHealthCheck:
    def test_ok_with_valid_source(self, tmp_path: Path) -> None:
        _populate_snapshot(tmp_path / "source")
        backend = OfflineExportBackend(tmp_path / "source")
        health = backend.health_check()
        assert health.ok
        assert health.version == "1.0.0"

    def test_ok_with_custom_version(self, tmp_path: Path) -> None:
        _populate_snapshot(tmp_path / "source", version="2.0.0-alpha")
        backend = OfflineExportBackend(tmp_path / "source")
        health = backend.health_check()
        assert health.ok
        assert health.version == "2.0.0-alpha"

    def test_not_ok_when_source_missing(self, tmp_path: Path) -> None:
        backend = OfflineExportBackend(tmp_path / "nonexistent")
        health = backend.health_check()
        assert not health.ok
        assert health.version == ""

    def test_not_ok_when_metadata_missing(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir(parents=True)
        (source / "abi_manifest.json").write_text("{}", encoding="utf-8")
        backend = OfflineExportBackend(source)
        health = backend.health_check()
        assert not health.ok

    def test_not_ok_when_metadata_corrupt(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir(parents=True)
        (source / "analysis-metadata.json").write_text("not json", encoding="utf-8")
        backend = OfflineExportBackend(source)
        health = backend.health_check()
        assert not health.ok


# ── fingerprint ───────────────────────────────────────────────────────────


class TestFingerprint:
    def test_returns_sha256_of_metadata(self, tmp_path: Path) -> None:
        _populate_snapshot(tmp_path / "source")
        backend = OfflineExportBackend(tmp_path / "source")
        fp = backend.fingerprint()
        import hashlib

        meta_bytes = (tmp_path / "source" / "analysis-metadata.json").read_bytes()
        expected = hashlib.sha256(meta_bytes).hexdigest()
        assert fp.sha256 == expected

    def test_raises_when_metadata_missing(self, tmp_path: Path) -> None:
        backend = OfflineExportBackend(tmp_path / "source")
        with pytest.raises(OfflineExportError, match="analysis-metadata.json not found"):
            backend.fingerprint()


# ── provision_workspace ───────────────────────────────────────────────────


class TestProvisionWorkspace:
    def test_copies_and_revalidates(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        _populate_snapshot(source)
        backend = OfflineExportBackend(source)

        workspace = tmp_path / "workspace"
        backend.provision_workspace(binary=tmp_path / "bogus.bin", workspace=workspace)

        # All source files exist in workspace
        assert (workspace / "analysis-metadata.json").is_file()
        assert (workspace / "abi_manifest.json").is_file()
        assert (workspace / "func_0x1000.json").is_file()

        # Workspace is a valid snapshot (inventory passes)
        from re_agent.project.snapshot import inventory_snapshot

        meta, files = inventory_snapshot(workspace)
        assert meta.schema_version == "1.0.0"

    def test_rejects_existing_workspace(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        _populate_snapshot(source)
        backend = OfflineExportBackend(source)

        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)

        with pytest.raises(OfflineExportError, match="workspace already exists"):
            backend.provision_workspace(binary=tmp_path / "bogus.bin", workspace=workspace)

    def test_rejects_invalid_source(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir(parents=True)
        (source / "analysis-metadata.json").write_text('{"bad": "data"}', encoding="utf-8")
        backend = OfflineExportBackend(source)
        workspace = tmp_path / "workspace"

        with pytest.raises(OfflineExportError, match="analysis-metadata.json has invalid"):
            backend.provision_workspace(binary=tmp_path / "bogus.bin", workspace=workspace)

    def test_copy_verification_catches_corruption(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        _populate_snapshot(source)

        backend = OfflineExportBackend(source)
        workspace = tmp_path / "workspace"
        backend.provision_workspace(binary=tmp_path / "bogus.bin", workspace=workspace)

        # All expected files present
        assert {p.name for p in workspace.rglob("*.json")} == {
            "analysis-metadata.json",
            "abi_manifest.json",
            "func_0x1000.json",
        }


# ── analyze_export ────────────────────────────────────────────────────────


class TestAnalyzeExport:
    def test_raises_not_implemented(self, tmp_path: Path) -> None:
        _populate_snapshot(tmp_path / "source")
        backend = OfflineExportBackend(tmp_path / "source")

        with pytest.raises(NotImplementedError, match="cannot run analysis"):
            backend.analyze_export(workspace=tmp_path / "ws", output=tmp_path / "out")
