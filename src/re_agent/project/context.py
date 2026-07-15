"""Verification of owned project snapshots before project-mode use."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from re_agent.contracts import AbiManifest, VerifiedContract, load_verified_manifest
from re_agent.project.model import ProjectIdentity
from re_agent.project.snapshot import canonical_json, inventory_snapshot, load_json, manifest_document, sha256_bytes


@dataclass(frozen=True, slots=True)
class VerifiedProjectContext:
    root: Path
    identity: ProjectIdentity
    snapshot_root: Path
    abi_manifest_path: Path
    verified_abi_manifest: VerifiedContract[AbiManifest]


def load_verified_project(root: Path) -> VerifiedProjectContext:
    document = load_json(root / "project.id")
    required = {"format_version", "name", "binary_sha256", "snapshot_manifest_sha256", "project_fingerprint"}
    if set(document) != required or document["format_version"] != 1:
        raise ValueError("invalid project.id")
    values = tuple(
        document[key] for key in ("name", "binary_sha256", "snapshot_manifest_sha256", "project_fingerprint")
    )
    if not all(isinstance(value, str) for value in values):
        raise ValueError("invalid project.id")
    identity = ProjectIdentity(*values)
    for digest in (identity.binary_sha256, identity.snapshot_manifest_sha256, identity.project_fingerprint):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid project.id digest")
    expected_fingerprint = sha256_bytes(
        canonical_json(
            {
                "domain": "re-agent-project-v1",
                "binary_sha256": identity.binary_sha256,
                "snapshot_manifest_sha256": identity.snapshot_manifest_sha256,
            }
        )
    )
    if identity.project_fingerprint != expected_fingerprint:
        raise ValueError("project fingerprint mismatch")
    snapshot = root / "snapshots" / identity.project_fingerprint
    metadata, files = inventory_snapshot(snapshot)
    inventory_bytes = canonical_json(manifest_document(files))
    if metadata.binary_sha256 != identity.binary_sha256:
        raise ValueError("project binary identity mismatch")
    if (
        sha256_bytes(inventory_bytes) != identity.snapshot_manifest_sha256
        or (snapshot / "snapshot.sha256").read_bytes() != inventory_bytes
    ):
        raise ValueError("snapshot inventory mismatch")
    abi_manifest_path = snapshot / metadata.abi_manifest_path
    manifest, raw_sha256, canonical_sha256 = load_verified_manifest(abi_manifest_path)
    verified_manifest = VerifiedContract(manifest, abi_manifest_path, raw_sha256, canonical_sha256)
    return VerifiedProjectContext(root, identity, snapshot, abi_manifest_path, verified_manifest)
