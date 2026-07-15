"""Atomic project provisioning from an analysis snapshot."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from re_agent.project.context import load_verified_project
from re_agent.project.model import ProjectIdentity
from re_agent.project.publish import DestinationExistsError, publish_directory
from re_agent.project.snapshot import canonical_json, inventory_snapshot, manifest_document, sha256_bytes, sha256_file


class ProvisionError(ValueError):
    """Raised for a fail-closed provisioning failure."""


def _identity(name: str, binary_sha256: str, manifest_sha256: str) -> ProjectIdentity:
    fingerprint = sha256_bytes(
        canonical_json(
            {
                "domain": "re-agent-project-v1",
                "binary_sha256": binary_sha256,
                "snapshot_manifest_sha256": manifest_sha256,
            }
        )
    )
    return ProjectIdentity(name, binary_sha256, manifest_sha256, fingerprint)


def provision_project(*, binary: Path, analysis: Path, output: Path, name: str) -> ProjectIdentity:
    if not binary.is_file() or binary.is_symlink():
        raise ProvisionError("binary must be a regular file")
    try:
        metadata, files = inventory_snapshot(analysis)
    except ValueError as exc:
        raise ProvisionError(str(exc)) from exc
    binary_sha = sha256_file(binary)
    if binary_sha != metadata.binary_sha256:
        raise ProvisionError("fingerprint mismatch")
    manifest = canonical_json(manifest_document(files))
    identity = _identity(name, binary_sha, sha256_bytes(manifest))
    if output.exists():
        try:
            existing = load_verified_project(output).identity
        except (OSError, ValueError) as exc:
            raise ProvisionError("destination already exists and is not a project") from exc
        if existing == identity:
            return identity
        raise ProvisionError("destination already exists with a different identity")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        snapshot = stage / "snapshots" / identity.project_fingerprint
        snapshot.mkdir(parents=True)
        for entry in files:
            source = analysis / entry.path
            destination = snapshot / entry.path
            if source.is_symlink() or not source.is_file() or sha256_file(source) != entry.sha256:
                raise ProvisionError(f"analysis changed while copying: {entry.path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            if sha256_file(destination) != entry.sha256:
                raise ProvisionError(f"snapshot copy verification failed: {entry.path}")
        staged_metadata, staged_files = inventory_snapshot(snapshot)
        if staged_metadata != metadata or staged_files != files:
            raise ProvisionError("staged snapshot verification failed")
        (snapshot / "snapshot.sha256").write_bytes(manifest)
        (stage / "project.id").write_bytes(canonical_json({"format_version": 1, **asdict(identity)}))
        try:
            publish_directory(stage, output)
        except DestinationExistsError:
            raise ProvisionError(f"destination already exists (concurrent creation): {output}") from None
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return identity
