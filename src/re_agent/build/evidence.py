"""Immutable, canonical evidence for bounded build orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
)
_EFFECTIVE_CONFIG_KEYS = {
    "provider",
    "model",
    "block_model",
    "base_url",
    "max_tokens",
    "temperature",
    "timeout_s",
}


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted(((str(k), _freeze(v)) for k, v in value.items()), key=lambda item: item[0]))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _json_safe(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_safe(item) for key, item in value.items())
    return False


def _safe_relative_posix(path: str) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", path) or path.startswith("//"):
        return False
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def validate_run_id(run_id: str) -> str:
    """Validate and return a safe run ID, or raise ``ValueError``.

    Rules
    -----
    * Only ASCII alphanumeric, hyphen, underscore, dot characters.
    * Must not be empty.
    * Must not start or end with a dot or hyphen.
    * Must not be a Windows reserved device name
      (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``-``COM9``, ``LPT1``-``LPT9``).
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not all(c.isascii() and (c.isalnum() or c in "._-") for c in run_id):
        raise ValueError("run_id must contain only ASCII alphanumeric, hyphen, underscore, or dot characters")
    if run_id.startswith(".") or run_id.endswith("."):
        raise ValueError("run_id must not start or end with a dot")
    if run_id.startswith("-") or run_id.endswith("-"):
        raise ValueError("run_id must not start or end with a hyphen")
    name = run_id.upper()
    bare = name.split(".")[0]
    if bare in _WINDOWS_RESERVED:
        raise ValueError(f"run_id must not be a Windows reserved device name: {run_id!r}")
    return run_id


def _normalize_effective_config(config: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    if not isinstance(config, Mapping):
        raise ValueError("effective provider config must be an object")
    unknown = set(config) - _EFFECTIVE_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown effective provider config keys: {sorted(unknown)}")
    if any(str(key).lower() in {"api_key", "token", "secret", "password"} for key in config):
        raise ValueError("secret provider configuration is not evidence-safe")
    if "provider" not in config or "model" not in config:
        raise ValueError("effective provider config requires provider and model")
    if not all(isinstance(key, str) for key in config) or not all(_json_safe(value) for value in config.values()):
        raise ValueError("effective provider config must contain only finite JSON-safe values")
    for key, value in config.items():
        if key in {"provider", "model"} and (not isinstance(value, str) or not value):
            raise ValueError(f"effective provider config value is invalid: {key}")
        if key in {"block_model", "base_url"} and value is not None and not isinstance(value, str):
            raise ValueError(f"effective provider config value is invalid: {key}")
        if key == "base_url" and value is not None:
            parsed = urlsplit(cast(str, value))
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("base_url with query, fragment, or credentials is not evidence-safe")
        if key in {"max_tokens", "timeout_s"} and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f"effective provider config value is invalid: {key}")
        if key == "temperature" and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        ):
            raise ValueError("effective provider config temperature is invalid")
    return tuple(sorted(((key, _freeze(value)) for key, value in config.items()), key=lambda item: item[0]))


def _normalize_request_kwargs(
    config: Mapping[str, object] | Iterable[tuple[str, object]],
) -> tuple[tuple[str, object], ...]:
    values: dict[str, object] = (
        {str(key): value for key, value in config.items()} if isinstance(config, Mapping) else dict(config)
    )
    if not all(isinstance(key, str) and key and "\x00" not in key for key in values):
        raise ValueError("provider request kwargs have invalid keys")
    if not all(_json_safe(value) for value in values.values()):
        raise ValueError("provider request kwargs must be finite JSON-safe values")
    return tuple(sorted(((key, _freeze(value)) for key, value in values.items()), key=lambda item: item[0]))


def _thaw(value: object) -> object:
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _message_pair(item: Any) -> tuple[str, str]:
    candidate = cast(Any, item)
    if hasattr(candidate, "role"):
        role, content = candidate.role, candidate.content
    else:
        role, content = candidate[0], candidate[1]
    if not isinstance(role, str) or not isinstance(content, str):
        raise ValueError("transform messages must contain strings")
    return role, content


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
    transform_evidence_path: str = ""
    transform_evidence_sha256: str = ""

    def key(self) -> tuple[int, str]:
        return (self.address, self.name)

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
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
        if self.transform_evidence_path or self.transform_evidence_sha256:
            data["transform_evidence_path"] = self.transform_evidence_path
            data["transform_evidence_sha256"] = self.transform_evidence_sha256
        return data


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
    if not isinstance(evidence, BuildEvidence) or evidence.schema_version not in (1, 2):
        raise ValueError("unsupported build evidence")
    for field in (evidence.project_fingerprint, evidence.manifest_sha256, evidence.recipe_sha256):
        if not isinstance(field, str) or not _DIGEST.fullmatch(field):
            raise ValueError("build evidence contains an invalid identity digest")
    if evidence.evidence_sha256 != _digest(evidence.as_dict(include_hash=False)):
        raise ValueError("stale build evidence hash")
    try:
        validate_run_id(evidence.run_id)
    except ValueError:
        raise ValueError("build evidence requires a run_id") from None
    for digest in (evidence.compiler_sha256, evidence.artifact_sha256, evidence.inspection_output_sha256):
        if digest and not _DIGEST.fullmatch(digest):
            raise ValueError("malformed build identity or artifact digest")
    if not _DIGEST.fullmatch(evidence.compiler_sha256) or not _DIGEST.fullmatch(evidence.toolchain_sha256):
        raise ValueError("compiler and toolchain identities are required")
    for target in evidence.targets:
        if (
            isinstance(target.address, bool)
            or not isinstance(target.address, int)
            or target.address < 0
            or target.address > 0xFFFFFFFFFFFFFFFF
        ):
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
        if not _safe_relative_posix(target.output_path):
            raise ValueError("unsafe target output path")
        if "MANIFEST_BOUND" not in target.verdicts or "COMPILE_PASS" not in target.verdicts:
            raise ValueError("target evidence lacks required success verdicts")
        for digest in (target.source_sha256, target.output_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("malformed target digest in build evidence")
        for digest in (target.input_sha256, target.generated_sha256, target.object_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("required target artifact hash is missing or malformed")
        if evidence.schema_version >= 2 and (
            not target.transform_evidence_path or not _DIGEST.fullmatch(target.transform_evidence_sha256)
        ):
            raise ValueError("target transform evidence path and hash are required")
        if evidence.schema_version >= 2 and not _safe_relative_posix(target.transform_evidence_path):
            raise ValueError("target transform evidence path is unsafe")
    if evidence.schema_version >= 2:
        paths = [target.transform_evidence_path for target in evidence.targets]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate transform evidence paths")
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
                "transform_evidence_path",
                "transform_evidence_sha256",
            ):
                if getattr(current, field) != getattr(checkpoint, field):
                    raise ValueError(f"stale checkpoint identity for {key}")


def _validate_run_evidence(evidence: BuildEvidence) -> None:
    """Validate the always-recorded run envelope, including failed recipes."""
    if not isinstance(evidence, BuildEvidence) or evidence.schema_version not in (1, 2):
        raise ValueError("unsupported build evidence")
    for field in (evidence.project_fingerprint, evidence.manifest_sha256, evidence.recipe_sha256):
        if not isinstance(field, str) or not _DIGEST.fullmatch(field):
            raise ValueError("build evidence contains an invalid identity digest")
    if evidence.evidence_sha256 != _digest(evidence.as_dict(include_hash=False)):
        raise ValueError("stale build evidence hash")
    try:
        validate_run_id(evidence.run_id)
    except ValueError:
        raise ValueError("build evidence requires a run_id") from None
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
        if (
            isinstance(target.address, bool)
            or not isinstance(target.address, int)
            or target.address < 0
            or target.address > 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("malformed target address in build evidence")
        if not target.name or not target.status:
            raise ValueError("malformed target checkpoint in build evidence")


def validate_run_evidence(evidence: BuildEvidence) -> None:
    """Validate the structured run envelope without requiring successful publication."""
    _validate_run_evidence(evidence)


def load_evidence(path: Path, *, validate_success: bool = False) -> BuildEvidence:
    """Load canonical evidence and validate its structural hash."""
    original = path.read_bytes()
    raw = json.loads(original, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_json_constant)
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
    if not isinstance(raw, dict) or not required.issubset(raw):
        raise ValueError("malformed build evidence JSON")
    if raw.get("schema_version") == 1:
        # R5 evidence remains loadable for historical inspection, but is never
        # upgraded into replayable R6 evidence.
        for item in raw["targets"]:
            if not isinstance(item, dict):
                raise ValueError("malformed build evidence JSON")
            item.pop("transform_evidence_path", None)
            item.pop("transform_evidence_sha256", None)
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
    if evidence.to_json() != original:
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


@dataclass(frozen=True, slots=True)
class TransformEvidence:
    """Canonical, immutable provenance for one successfully transformed target."""

    project_fingerprint: str
    snapshot_fingerprint: str
    manifest_raw_sha256: str
    manifest_sha256: str
    run_id: str
    target_address: int
    target_name: str
    target_signature: str
    target_calling_convention: str
    target_output_path: str
    messages: tuple[tuple[str, str], ...] | tuple[object, ...]
    llm_config: Mapping[str, object] | tuple[tuple[str, object], ...]
    input_text: str
    input_sha256: str
    raw_response: str
    raw_response_sha256: str
    generated_sha256: str
    object_sha256: str
    compiler_argv: tuple[str, ...]
    compiler_executable_sha256: str
    diagnostics: str = ""
    exit_status: int = 0
    request_kwargs: tuple[tuple[str, object], ...] = ()
    schema_version: int = 1
    evidence_sha256: str = ""

    def __post_init__(self) -> None:
        normalized_messages: tuple[tuple[str, str], ...] = tuple(_message_pair(item) for item in self.messages)
        object.__setattr__(self, "messages", normalized_messages)
        config = dict(self.llm_config) if isinstance(self.llm_config, Mapping) else dict(self.llm_config)
        object.__setattr__(self, "llm_config", _normalize_effective_config(config))
        object.__setattr__(self, "compiler_argv", tuple(self.compiler_argv))
        object.__setattr__(self, "request_kwargs", _normalize_request_kwargs(self.request_kwargs))
        if self.input_sha256 == "":
            object.__setattr__(self, "input_sha256", _hash_text(self.input_text))
        if self.raw_response_sha256 == "":
            object.__setattr__(self, "raw_response_sha256", _hash_text(self.raw_response))

    @property
    def response_sha256(self) -> str:
        return self.raw_response_sha256

    @property
    def snapshot_sha256(self) -> str:
        return self.snapshot_fingerprint

    def as_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "compiler_argv": list(self.compiler_argv),
            "compiler_executable_sha256": self.compiler_executable_sha256,
            "diagnostics": self.diagnostics,
            "exit_status": self.exit_status,
            "generated_sha256": self.generated_sha256,
            "input_sha256": self.input_sha256,
            "input_text": self.input_text,
            "llm_config": {key: _thaw(value) for key, value in cast(tuple[tuple[str, object], ...], self.llm_config)},
            "manifest_raw_sha256": self.manifest_raw_sha256,
            "manifest_sha256": self.manifest_sha256,
            "messages": [
                {"role": role, "content": content} for role, content in cast(tuple[tuple[str, str], ...], self.messages)
            ],
            "object_sha256": self.object_sha256,
            "project_fingerprint": self.project_fingerprint,
            "raw_response": self.raw_response,
            "raw_response_sha256": self.raw_response_sha256,
            "request_kwargs": {key: _thaw(value) for key, value in self.request_kwargs},
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "target_address": self.target_address,
            "target_calling_convention": self.target_calling_convention,
            "target_name": self.target_name,
            "target_output_path": self.target_output_path,
            "target_signature": self.target_signature,
        }
        if include_hash:
            data["evidence_sha256"] = self.evidence_sha256
        return data

    def with_hash(self) -> TransformEvidence:
        return replace(self, evidence_sha256=_digest(self.as_dict(include_hash=False)))

    def to_json(self) -> bytes:
        return _canonical(self.as_dict())


def validate_transform_evidence(evidence: TransformEvidence) -> None:
    if not isinstance(evidence, TransformEvidence) or evidence.schema_version != 1:
        raise ValueError("unsupported transform evidence")
    if evidence.evidence_sha256 != _digest(evidence.as_dict(include_hash=False)):
        raise ValueError("stale transform evidence hash")
    for digest in (
        evidence.project_fingerprint,
        evidence.snapshot_fingerprint,
        evidence.manifest_raw_sha256,
        evidence.manifest_sha256,
        evidence.input_sha256,
        evidence.raw_response_sha256,
        evidence.generated_sha256,
        evidence.object_sha256,
        evidence.compiler_executable_sha256,
    ):
        if not _DIGEST.fullmatch(digest):
            raise ValueError("transform evidence contains an invalid digest")
    try:
        validate_run_id(evidence.run_id)
    except ValueError:
        raise ValueError("transform evidence run_id is unsafe") from None
    if not evidence.messages or not evidence.compiler_argv:
        raise ValueError("transform evidence is incomplete")
    if (
        isinstance(evidence.target_address, bool)
        or not isinstance(evidence.target_address, int)
        or evidence.target_address < 0
        or evidence.target_address > 0xFFFFFFFFFFFFFFFF
    ):
        raise ValueError("transform evidence target address is invalid")
    for value in (
        evidence.target_name,
        evidence.target_signature,
        evidence.target_calling_convention,
        evidence.target_output_path,
    ):
        if not isinstance(value, str) or not value:
            raise ValueError("transform evidence target identity is incomplete")
    if not _safe_relative_posix(evidence.target_output_path):
        raise ValueError("transform evidence output path is unsafe")
    for role, content in cast(tuple[tuple[str, str], ...], evidence.messages):
        if role not in {"system", "user", "assistant"} or not content:
            raise ValueError("transform evidence messages are malformed")
    if not all(isinstance(item, str) and item and "\x00" not in item for item in evidence.compiler_argv):
        raise ValueError("transform compiler argv is malformed")
    for kw_key, kw_value in evidence.request_kwargs:
        if not isinstance(kw_key, str) or not kw_key or not _json_safe(kw_value):
            raise ValueError("transform request kwargs must be finite JSON-safe values")
    if not isinstance(evidence.input_text, str) or not evidence.input_text:
        raise ValueError("transform input text is empty")
    if not isinstance(evidence.raw_response, str) or not evidence.raw_response:
        raise ValueError("transform raw response is empty")
    if evidence.input_sha256 != _hash_text(evidence.input_text):
        raise ValueError("stale transform input hash")
    if evidence.raw_response_sha256 != _hash_text(evidence.raw_response):
        raise ValueError("stale transform response hash")
    if evidence.exit_status != 0:
        raise ValueError("transform evidence does not record a successful transform")


def load_transform_evidence(path: Path) -> TransformEvidence:
    original = path.read_bytes()
    raw = json.loads(original, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_json_constant)
    if not isinstance(raw, dict):
        raise ValueError("malformed transform evidence JSON")
    required = {
        "compiler_argv",
        "compiler_executable_sha256",
        "diagnostics",
        "evidence_sha256",
        "exit_status",
        "generated_sha256",
        "input_sha256",
        "input_text",
        "llm_config",
        "manifest_raw_sha256",
        "manifest_sha256",
        "messages",
        "object_sha256",
        "project_fingerprint",
        "raw_response",
        "raw_response_sha256",
        "request_kwargs",
        "run_id",
        "schema_version",
        "snapshot_fingerprint",
        "target_address",
        "target_calling_convention",
        "target_name",
        "target_output_path",
        "target_signature",
    }
    if set(raw) != required:
        raise ValueError("malformed transform evidence JSON")
    try:
        evidence = TransformEvidence(
            **{
                **raw,
                "messages": tuple((m["role"], m["content"]) for m in raw["messages"]),
                "llm_config": tuple(raw["llm_config"].items()),
                "compiler_argv": tuple(raw["compiler_argv"]),
                "request_kwargs": tuple(raw["request_kwargs"].items()),
            }
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed transform evidence JSON") from exc
    if evidence.to_json() != original:
        raise ValueError("non-canonical transform evidence JSON")
    validate_transform_evidence(evidence)
    return evidence


def save_transform_evidence(evidence: TransformEvidence, path: Path) -> TransformEvidence:
    checked = evidence.with_hash()
    validate_transform_evidence(checked)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(checked.to_json())
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return checked


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


__all__ = [
    "BuildEvidence",
    "TargetCheckpoint",
    "coverage",
    "load_evidence",
    "save_evidence",
    "validate_evidence",
    "validate_run_evidence",
    "validate_run_id",
    "TransformEvidence",
    "load_transform_evidence",
    "save_transform_evidence",
    "validate_transform_evidence",
]
