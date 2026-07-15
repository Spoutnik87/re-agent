"""Offline analysis lifecycle adapter — consumes a pre-exported Ghidra snapshot."""

from __future__ import annotations

import shutil
from pathlib import Path

from re_agent.analysis.lifecycle import BackendFingerprint, BackendHealth
from re_agent.project.snapshot import (
    SnapshotError,
    inventory_snapshot,
    load_json,
    sha256_file,
)


class OfflineExportError(ValueError):
    """Raised when an offline export operation fails."""


class OfflineExportBackend:
    """Backend that consumes a pre-exported Ghidra analysis directory.

    No binary analysis is performed — the snapshot is assumed to already
    exist on disk.  The class validates the inventory on every public call
    so callers always work with a trusted view.

    Args:
        export_path: Path to an existing analysis snapshot directory
            containing ``analysis-metadata.json`` and valid ``*.json``
            snapshot files.
    """

    def __init__(self, export_path: Path) -> None:
        self._export = export_path.resolve()

    # ── public protocol ─────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Return health based on presence and parseability of metadata."""
        meta = self._export / "analysis-metadata.json"
        if not meta.is_file():
            return BackendHealth(ok=False, version="")
        try:
            raw = load_json(meta)
            version = str(raw.get("schema_version", ""))
            return BackendHealth(ok=True, version=version)
        except (SnapshotError, OSError):
            return BackendHealth(ok=False, version="")

    def fingerprint(self) -> BackendFingerprint:
        """Return the SHA-256 of ``analysis-metadata.json``.

        Raises:
            OfflineExportError: If the metadata file is missing.
        """
        meta = self._export / "analysis-metadata.json"
        if not meta.is_file():
            raise OfflineExportError("analysis-metadata.json not found — cannot fingerprint")
        return BackendFingerprint(sha256=sha256_file(meta))

    def provision_workspace(self, binary: Path, workspace: Path) -> None:
        """Validate the snapshot inventory and copy all JSON files to *workspace*.

        The copy is performed safely:
        1.  Full inventory validation via ``inventory_snapshot()``.
        2.  Copy to a temporary staging directory.
        3.  Re-validate the staged snapshot via ``inventory_snapshot()``.
        4.  Atomic rename of staging → *workspace*.

        Args:
            binary: Ignored by the offline backend (the binary was already
                analysed externally).  Kept for protocol compatibility.
            workspace: Destination directory.  Must not exist (raises if it
                does).

        Raises:
            OfflineExportError: On validation failure, copy error, or if
                *workspace* already exists.
        """
        if workspace.exists():
            raise OfflineExportError(f"workspace already exists: {workspace}")

        try:
            meta, files = inventory_snapshot(self._export)
        except SnapshotError as exc:
            raise OfflineExportError(str(exc)) from exc

        # Stage in a sibling temporary directory for atomicity.
        stage = workspace.parent / f".{workspace.name}.stage"
        stage.mkdir(parents=True, exist_ok=False)

        try:
            for entry in files:
                source = self._export / entry.path
                dest = stage / entry.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                actual = sha256_file(dest)
                if actual != entry.sha256:
                    raise OfflineExportError(
                        f"copy verification failed for {entry.path}: expected {entry.sha256}, got {actual}"
                    )

            # Re-validate the staged copy via the same inventory logic.
            try:
                staged_meta, staged_files = inventory_snapshot(stage)
            except SnapshotError as exc:
                raise OfflineExportError(f"staged snapshot invalid: {exc}") from exc

            if staged_meta != meta or staged_files != files:
                raise OfflineExportError("staged snapshot metadata mismatch")

            stage.replace(workspace)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def analyze_export(self, workspace: Path, output: Path) -> Path:
        """Not supported by the offline backend.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "OfflineExportBackend cannot run analysis; "
            "the snapshot was pre-exported.  Use provision_workspace() to "
            "copy it into a project workspace."
        )
