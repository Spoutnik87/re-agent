"""Leaf value types for the WorkPacket schema (Todo 4).

Frozen dataclasses with stdlib-only JSON roundtrip. See work_packet.py for the
aggregate WorkPacket, StableContext, and TaskSuffix.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "JsonValue",
    "FunctionIdentity",
    "NeighbourContext",
    "ArtifactRef",
    "ModelUsage",
    "CompileVerdict",
    "ParityVerdict",
]

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class FunctionIdentity:
    address: str
    name: str | None = None
    module: str | None = None
    subunit_index: int | None = None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "address": self.address,
            "name": self.name,
            "module": self.module,
            "subunit_index": self.subunit_index,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> FunctionIdentity:
        return cls(
            address=str(data["address"]),
            name=_opt_str(data.get("name")),
            module=_opt_str(data.get("module")),
            subunit_index=_opt_int(data.get("subunit_index")),
        )


@dataclass(frozen=True, slots=True)
class NeighbourContext:
    address: str
    code: str

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"address": self.address, "code": self.code}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> NeighbourContext:
        return cls(address=str(data["address"]), code=str(data["code"]))


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    path: str
    kind: str
    sha256: str | None = None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> ArtifactRef:
        return cls(
            path=str(data["path"]),
            kind=str(data["kind"]),
            sha256=_opt_str(data.get("sha256")),
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    calls: int | None = None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "calls": self.calls,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> ModelUsage:
        return cls(
            provider=str(data["provider"]),
            model=str(data["model"]),
            prompt_tokens=_opt_int(data.get("prompt_tokens")),
            completion_tokens=_opt_int(data.get("completion_tokens")),
            cache_hit_tokens=_opt_int(data.get("cache_hit_tokens")),
            cache_miss_tokens=_opt_int(data.get("cache_miss_tokens")),
            calls=_opt_int(data.get("calls")),
        )


@dataclass(frozen=True, slots=True)
class CompileVerdict:
    compiles: bool
    verdict: str
    stderr: str | None = None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"compiles": self.compiles, "verdict": self.verdict, "stderr": self.stderr}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> CompileVerdict:
        return cls(
            compiles=bool(data["compiles"]),
            verdict=str(data["verdict"]),
            stderr=_opt_str(data.get("stderr")),
        )


@dataclass(frozen=True, slots=True)
class ParityVerdict:
    status: str
    details: Mapping[str, JsonValue]

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"status": self.status, "details": dict(self.details)}

    @classmethod
    def from_json_dict(cls, data: Mapping[str, JsonValue]) -> ParityVerdict:
        details = data.get("details", {})
        return cls(
            status=str(data["status"]),
            details=dict(details) if isinstance(details, Mapping) else {},
        )


def _opt_str(v: object) -> str | None:
    return str(v) if v is not None else None


def _opt_int(v: object) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError(f"Expected int, got bool: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        return int(v)
    raise TypeError(f"Expected int or int-coercible, got {type(v).__name__}: {v!r}")


def as_mapping(v: object) -> dict[str, JsonValue]:
    if isinstance(v, dict):
        return v
    raise TypeError(f"Expected a JSON object/dict, got {type(v).__name__}: {v!r}")
