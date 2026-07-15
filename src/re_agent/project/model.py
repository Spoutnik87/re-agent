"""Immutable models used by generic project provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class AnalysisMetadata:
    schema_version: str
    backend: str
    binary_sha256: str
    abi_manifest_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: PurePosixPath
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    name: str
    binary_sha256: str
    snapshot_manifest_sha256: str
    project_fingerprint: str
