"""Immutable, canonical evidence for bounded build orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class TargetCheckpoint:
    """Checkpoint for one manifest target, including its complete ABI identity."""

    address: int
    name: str
    status: str
    source_sha256: str = ""
    output_sha256: str = ""
    diagnostic: str = ""
    signature: str = ""
    calling_convention: str = ""
    output_path: str = ""
    input_sha256: str = ""
    generated_sha256: str = ""
    object_sha256: str = ""
    verdicts: tuple[str, ...] = ()

    def key(self) -> tuple[int, str]:
        return (self.address, self.name)

    def as_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "name": self.name,
            "status": self.status,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
            "diagnostic": self.diagnostic,
            "signature": self.signature,
            "calling_convention": self.calling_convention,
            "output_path": self.output_path,
            "input_sha256": self.input_sha256,
            "generated_sha256": self.generated_sha256,
            "object_sha256": self.object_sha256,
            "verdicts": list(self.verdicts),
        }


@dataclass(frozen=True, slots=True)
class BuildEvidence:
    """Content-addressed build evidence bound to a project and manifest."""

    project_fingerprint: str
    manifest_sha256: str
    recipe_sha256: str
    targets: tuple[TargetCheckpoint, ...]
    output_path: str = ""
    output_sha256: str = ""
    toolchain_sha256: str = ""
    schema_version: int = 1
    evidence_sha256: str = ""
    run_id: str = ""
    source_coverage: tuple[tuple[int, str], ...] = ()
    object_coverage: tuple[tuple[int, str], ...] = ()
    stdout: str = ""
    stderr: str = ""
    exit_status: int | None = None
    timed_out: bool = False
    artifact_sha256: str = ""
    inspection_output_sha256: str = ""
    compiler_sha256: str = ""
    partial: bool = False

    def as_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "manifest_sha256": self.manifest_sha256,
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "project_fingerprint": self.project_fingerprint,
            "recipe_sha256": self.recipe_sha256,
            "schema_version": self.schema_version,
            "targets": [target.as_dict() for target in sorted(self.targets, key=lambda t: t.key())],
            "toolchain_sha256": self.toolchain_sha256,
            "run_id": self.run_id,
            "source_coverage": [list(item) for item in coverage(self.source_coverage)],
            "object_coverage": [list(item) for item in coverage(self.object_coverage)],
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_status": self.exit_status,
            "timed_out": self.timed_out,
            "artifact_sha256": self.artifact_sha256,
            "inspection_output_sha256": self.inspection_output_sha256,
            "compiler_sha256": self.compiler_sha256,
            "partial": self.partial,
        }
        if include_hash:
            data["evidence_sha256"] = self.evidence_sha256
        return data

    def with_hash(self) -> BuildEvidence:
        return replace(self, evidence_sha256=_digest(self.as_dict(include_hash=False)))

    def coverage(self) -> tuple[tuple[int, str], ...]:
        return tuple(target.key() for target in sorted(self.targets, key=lambda t: t.key()))

    def to_json(self) -> bytes:
        return _canonical(self.as_dict())


def coverage(targets: Iterable[tuple[int, str] | TargetCheckpoint]) -> tuple[tuple[int, str], ...]:
    """Return deterministic unique ``(address, name)`` coverage."""
    keys = {item.key() if isinstance(item, TargetCheckpoint) else (int(item[0]), str(item[1])) for item in targets}
    return tuple(sorted(keys))


def validate_evidence(
    evidence: BuildEvidence,
    expected_targets: Iterable[tuple[int, str]],
    *,
    project_fingerprint: str | None = None,
    manifest_sha256: str | None = None,
    recipe_sha256: str | None = None,
    toolchain_sha256: str | None = None,
    expected_checkpoints: Iterable[TargetCheckpoint] | None = None,
) -> None:
    """Reject malformed, stale, duplicate, or incomplete publishable evidence."""
    _validate_run_evidence(evidence)
    if evidence.partial or evidence.timed_out or evidence.exit_status != 0:
        raise ValueError("build evidence does not record a successful build")
    if not _DIGEST.fullmatch(evidence.artifact_sha256) or not _DIGEST.fullmatch(evidence.output_sha256):
        raise ValueError("artifact and output hashes are required for publication")
    if evidence.inspection_output_sha256 and not _DIGEST.fullmatch(evidence.inspection_output_sha256):
        raise ValueError("malformed inspection output hash")
    if not isinstance(evidence, BuildEvidence) or evidence.schema_version != 1:
        raise ValueError("unsupported build evidence")
    for field in (evidence.project_fingerprint, evidence.manifest_sha256, evidence.recipe_sha256):
        if not isinstance(field, str) or not _DIGEST.fullmatch(field):
            raise ValueError("build evidence contains an invalid identity digest")
    if evidence.evidence_sha256 != _digest(evidence.as_dict(include_hash=False)):
        raise ValueError("stale build evidence hash")
    if not evidence.run_id:
        raise ValueError("build evidence requires a run_id")
    for digest in (evidence.compiler_sha256, evidence.artifact_sha256, evidence.inspection_output_sha256):
        if digest and not _DIGEST.fullmatch(digest):
            raise ValueError("malformed build identity or artifact digest")
    if not _DIGEST.fullmatch(evidence.compiler_sha256) or not _DIGEST.fullmatch(evidence.toolchain_sha256):
        raise ValueError("compiler and toolchain identities are required")
    for target in evidence.targets:
        if isinstance(target.address, bool) or not isinstance(target.address, int) or target.address < 0:
            raise ValueError("malformed target address in build evidence")
        if (
            not isinstance(target.name, str)
            or not target.name
            or not isinstance(target.status, str)
            or not target.status
        ):
            raise ValueError("malformed target checkpoint in build evidence")
        if not target.signature or not target.calling_convention or not target.output_path:
            raise ValueError("incomplete target manifest identity")
        if "MANIFEST_BOUND" not in target.verdicts or "COMPILE_PASS" not in target.verdicts:
            raise ValueError("target evidence lacks required success verdicts")
        for digest in (target.source_sha256, target.output_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("malformed target digest in build evidence")
        for digest in (target.input_sha256, target.generated_sha256, target.object_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("required target artifact hash is missing or malformed")
    for digest in (evidence.output_sha256, evidence.toolchain_sha256):
        if digest and not _DIGEST.fullmatch(digest):
            raise ValueError("malformed build output digest")
    expected = coverage(expected_targets)
    actual = evidence.coverage()
    if len(actual) != len(evidence.targets):
        raise ValueError("build evidence contains duplicate targets")
    if actual != expected:
        raise ValueError(f"build evidence coverage mismatch: expected {expected}, got {actual}")
    if (
        len(evidence.source_coverage) != len(coverage(evidence.source_coverage))
        or len(evidence.object_coverage) != len(coverage(evidence.object_coverage))
        or coverage(evidence.source_coverage) != expected
        or coverage(evidence.object_coverage) != expected
    ):
        raise ValueError("build source/object coverage mismatch")
    if project_fingerprint is not None and evidence.project_fingerprint != project_fingerprint:
        raise ValueError("stale project fingerprint in build evidence")
    if manifest_sha256 is not None and evidence.manifest_sha256 != manifest_sha256:
        raise ValueError("stale manifest identity in build evidence")
    if recipe_sha256 is not None and evidence.recipe_sha256 != recipe_sha256:
        raise ValueError("stale recipe identity in build evidence")
    if toolchain_sha256 is not None and evidence.toolchain_sha256 != toolchain_sha256:
        raise ValueError("stale toolchain identity in build evidence")
    if expected_checkpoints is not None:
        expected_checkpoint_map: dict[tuple[int, str], TargetCheckpoint] = {
            checkpoint.key(): checkpoint for checkpoint in expected_checkpoints
        }
        actual_checkpoint_map: dict[tuple[int, str], TargetCheckpoint] = {
            checkpoint.key(): checkpoint for checkpoint in evidence.targets
        }
        if set(actual_checkpoint_map) != set(expected_checkpoint_map):
            raise ValueError("stale checkpoint coverage in build evidence")
        for key, checkpoint in expected_checkpoint_map.items():
            current = actual_checkpoint_map[key]
            for field in (
                "name",
                "signature",
                "calling_convention",
                "output_path",
                "source_sha256",
                "output_sha256",
                "input_sha256",
                "generated_sha256",
                "object_sha256",
            ):
                if getattr(current, field) != getattr(checkpoint, field):
                    raise ValueError(f"stale checkpoint identity for {key}")


def _validate_run_evidence(evidence: BuildEvidence) -> None:
    """Validate the always-recorded run envelope, including failed recipes."""
    if not isinstance(evidence, BuildEvidence) or evidence.schema_version != 1:
        raise ValueError("unsupported build evidence")
    for field in (evidence.project_fingerprint, evidence.manifest_sha256, evidence.recipe_sha256):
        if not isinstance(field, str) or not _DIGEST.fullmatch(field):
            raise ValueError("build evidence contains an invalid identity digest")
    if evidence.evidence_sha256 != _digest(evidence.as_dict(include_hash=False)):
        raise ValueError("stale build evidence hash")
    if not isinstance(evidence.run_id, str) or not evidence.run_id:
        raise ValueError("build evidence requires a run_id")
    if not isinstance(evidence.stdout, str) or not isinstance(evidence.stderr, str):
        raise ValueError("run evidence output must be text")
    if not isinstance(evidence.output_path, str):
        raise ValueError("run evidence output_path must be text")
    if evidence.exit_status is not None and (
        isinstance(evidence.exit_status, bool) or not isinstance(evidence.exit_status, int)
    ):
        raise ValueError("run evidence exit_status must be an integer or null")
    if not isinstance(evidence.timed_out, bool) or not isinstance(evidence.partial, bool):
        raise ValueError("run evidence flags are malformed")
    for digest in (
        evidence.compiler_sha256,
        evidence.toolchain_sha256,
        evidence.artifact_sha256,
        evidence.output_sha256,
        evidence.inspection_output_sha256,
    ):
        if digest and not _DIGEST.fullmatch(digest):
            raise ValueError("malformed build identity or artifact digest")
    for target in evidence.targets:
        if isinstance(target.address, bool) or not isinstance(target.address, int) or target.address < 0:
            raise ValueError("malformed target address in build evidence")
        if not target.name or not target.status:
            raise ValueError("malformed target checkpoint in build evidence")


def validate_run_evidence(evidence: BuildEvidence) -> None:
    """Validate the structured run envelope without requiring successful publication."""
    _validate_run_evidence(evidence)


def load_evidence(path: Path, *, validate_success: bool = False) -> BuildEvidence:
    """Load canonical evidence and validate its structural hash."""
    raw = json.loads(path.read_bytes())
    required = {
        "schema_version",
        "project_fingerprint",
        "manifest_sha256",
        "recipe_sha256",
        "targets",
        "output_path",
        "output_sha256",
        "toolchain_sha256",
        "evidence_sha256",
        "run_id",
        "source_coverage",
        "object_coverage",
        "stdout",
        "stderr",
        "exit_status",
        "timed_out",
        "artifact_sha256",
        "inspection_output_sha256",
        "compiler_sha256",
        "partial",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("malformed build evidence JSON")
    targets = tuple(TargetCheckpoint(**item) for item in raw["targets"])
    evidence = BuildEvidence(
        project_fingerprint=raw["project_fingerprint"],
        manifest_sha256=raw["manifest_sha256"],
        recipe_sha256=raw["recipe_sha256"],
        targets=targets,
        output_path=raw["output_path"],
        output_sha256=raw["output_sha256"],
        toolchain_sha256=raw["toolchain_sha256"],
        schema_version=raw["schema_version"],
        evidence_sha256=raw["evidence_sha256"],
        run_id=raw["run_id"],
        source_coverage=tuple(tuple(item) for item in raw["source_coverage"]),
        object_coverage=tuple(tuple(item) for item in raw["object_coverage"]),
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        exit_status=raw["exit_status"],
        timed_out=raw["timed_out"],
        artifact_sha256=raw["artifact_sha256"],
        inspection_output_sha256=raw["inspection_output_sha256"],
        compiler_sha256=raw["compiler_sha256"],
        partial=raw["partial"],
    )
    if evidence.to_json() != _canonical(raw):
        raise ValueError("non-canonical build evidence JSON")
    validate_run_evidence(evidence)
    if validate_success:
        validate_evidence(evidence, evidence.coverage())
    return evidence


def save_evidence(evidence: BuildEvidence, path: Path) -> BuildEvidence:
    """Validate and write canonical evidence exactly once; never overwrite."""
    checked = evidence.with_hash()
    validate_run_evidence(checked)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(checked.to_json())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return checked


__all__ = [
    "BuildEvidence",
    "TargetCheckpoint",
    "coverage",
    "load_evidence",
    "save_evidence",
    "validate_evidence",
    "validate_run_evidence",
]
