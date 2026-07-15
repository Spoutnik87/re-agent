"""Separate lifecycle protocol; it intentionally does not extend REBackend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BackendHealth:
    ok: bool
    version: str


@dataclass(frozen=True, slots=True)
class BackendFingerprint:
    sha256: str


class AnalysisLifecycleBackend(Protocol):
    def health_check(self) -> BackendHealth: ...
    def fingerprint(self) -> BackendFingerprint: ...
    def provision_workspace(self, binary: Path, workspace: Path) -> None: ...
    def analyze_export(self, workspace: Path, output: Path) -> Path: ...
