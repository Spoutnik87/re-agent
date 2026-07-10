"""Typed, deterministic, read/write JSON schema for a function/subunit Work Packet.

Used by cache-aware transform work. Not wired into runtime yet (Todo 4).

Design:
- Frozen dataclasses; tuples for ordered collections.
- Only stdlib (no Pydantic).
- Recursive JsonValue type alias; no `Any` in public signatures.
- Hashes use canonical JSON (sort_keys=True, separators=(",", ":")) over the
  relevant sub-document, sha256, first 16 hex chars (matches TransformCache).
- Optional cache metrics stay None/null through roundtrip; never faked as 0.
- Tuples serialize as JSON lists but roundtrip restores tuple types.

Leaf value types live in work_packet_types.py.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from re_agent.build.work_packet_types import (
    ArtifactRef,
    CompileVerdict,
    FunctionIdentity,
    JsonValue,
    ModelUsage,
    NeighbourContext,
    ParityVerdict,
    as_mapping,
)

__all__ = [
    "JsonValue",
    "FunctionIdentity",
    "NeighbourContext",
    "StableContext",
    "TaskSuffix",
    "ArtifactRef",
    "ModelUsage",
    "CompileVerdict",
    "ParityVerdict",
    "WorkPacket",
]

_VALID_TASK_KINDS = frozenset({"transform", "compile_repair", "parity_triage", "reverse"})
_SCHEMA_VERSION = 1
_HASH_LEN = 16


def _canonical_json_text(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _hash_obj(obj: object) -> str:
    return _hash_text(_canonical_json_text(obj))


@dataclass(frozen=True, slots=True)
class StableContext:
    function: FunctionIdentity
    decompiled_code: str
    neighbour_context: tuple[NeighbourContext, ...]
    ghidra_context: Mapping[str, JsonValue]
    project_rules: Mapping[str, JsonValue]

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "function": self.function.to_json_dict(),
            "decompiled_code": self.decompiled_code,
            "neighbour_context": [n.to_json_dict() for n in self.neighbour_context],
            "ghidra_context": dict(self.ghidra_context),
            "project_rules": dict(self.project_rules),
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> StableContext:
        neighbours_raw = data.get("neighbour_context", [])
        neighbours: tuple[NeighbourContext, ...] = ()
        if isinstance(neighbours_raw, list):
            neighbours = tuple(NeighbourContext.from_json_dict(as_mapping(n)) for n in neighbours_raw)
        ghidra = data.get("ghidra_context", {})
        rules = data.get("project_rules", {})
        return cls(
            function=FunctionIdentity.from_json_dict(as_mapping(data["function"])),
            decompiled_code=str(data["decompiled_code"]),
            neighbour_context=neighbours,
            ghidra_context=dict(ghidra) if isinstance(ghidra, Mapping) else {},
            project_rules=dict(rules) if isinstance(rules, Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class TaskSuffix:
    task_kind: str
    compiler_stderr: str | None = None
    prior_attempt_summary: str | None = None
    requested_output_format: str | None = None

    def __post_init__(self) -> None:
        if self.task_kind not in _VALID_TASK_KINDS:
            raise ValueError(f"Invalid task_kind {self.task_kind!r}; expected one of {sorted(_VALID_TASK_KINDS)}")

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "task_kind": self.task_kind,
            "compiler_stderr": self.compiler_stderr,
            "prior_attempt_summary": self.prior_attempt_summary,
            "requested_output_format": self.requested_output_format,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> TaskSuffix:
        return cls(
            task_kind=str(data["task_kind"]),
            compiler_stderr=_opt_str(data.get("compiler_stderr")),
            prior_attempt_summary=_opt_str(data.get("prior_attempt_summary")),
            requested_output_format=_opt_str(data.get("requested_output_format")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkPacket:
    schema_version: int = _SCHEMA_VERSION
    run_id: str = ""
    stable_context: StableContext
    task_suffix: TaskSuffix
    artifacts: tuple[ArtifactRef, ...] = ()
    model_usage: ModelUsage | None = None
    compile_verdict: CompileVerdict | None = None
    parity_verdict: ParityVerdict | None = None
    evidence_paths: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stable_context": self.stable_context.to_json_dict(),
            "task_suffix": self.task_suffix.to_json_dict(),
            "artifacts": [a.to_json_dict() for a in self.artifacts],
            "model_usage": self.model_usage.to_json_dict() if self.model_usage else None,
            "compile_verdict": self.compile_verdict.to_json_dict() if self.compile_verdict else None,
            "parity_verdict": self.parity_verdict.to_json_dict() if self.parity_verdict else None,
            "evidence_paths": list(self.evidence_paths),
        }

    def to_json_text(self) -> str:
        return json.dumps(self.to_json_dict(), sort_keys=True, indent=2, ensure_ascii=True)

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> WorkPacket:
        version = data.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise ValueError(f"schema_version mismatch: got {version!r}, expected {_SCHEMA_VERSION}")
        artifacts_raw = data.get("artifacts", [])
        artifacts: tuple[ArtifactRef, ...] = ()
        if isinstance(artifacts_raw, list):
            artifacts = tuple(ArtifactRef.from_json_dict(as_mapping(a)) for a in artifacts_raw)
        evidence_raw = data.get("evidence_paths", [])
        evidence: tuple[str, ...] = ()
        if isinstance(evidence_raw, list):
            evidence = tuple(str(e) for e in evidence_raw)
        mu_raw = data.get("model_usage")
        cv_raw = data.get("compile_verdict")
        pv_raw = data.get("parity_verdict")
        return cls(
            schema_version=_SCHEMA_VERSION,
            run_id=str(data["run_id"]),
            stable_context=StableContext.from_json_dict(as_mapping(data["stable_context"])),
            task_suffix=TaskSuffix.from_json_dict(as_mapping(data["task_suffix"])),
            artifacts=artifacts,
            model_usage=ModelUsage.from_json_dict(mu_raw) if isinstance(mu_raw, Mapping) else None,
            compile_verdict=CompileVerdict.from_json_dict(cv_raw) if isinstance(cv_raw, Mapping) else None,
            parity_verdict=ParityVerdict.from_json_dict(pv_raw) if isinstance(pv_raw, Mapping) else None,
            evidence_paths=evidence,
        )

    @classmethod
    def from_json_text(cls, text: str) -> WorkPacket:
        return cls.from_json_dict(json.loads(text))

    def stable_context_hash(self) -> str:
        return _hash_obj(self.stable_context.to_json_dict())

    def task_suffix_hash(self) -> str:
        return _hash_obj(self.task_suffix.to_json_dict())

    def full_packet_hash(self) -> str:
        return _hash_obj(self.to_json_dict())


def _opt_str(v: object) -> str | None:
    return str(v) if v is not None else None
