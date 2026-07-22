"""Canonical, immutable values used by the generic promotion layer."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^\d+:[^:]+$")  # canonical: "address:name"


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_target(address: int, name: str) -> str:
    """Return the canonical target identity string ``"{address}:{name}"``."""
    if not isinstance(address, int) or address < 0 or address > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"invalid target address: {address!r}")
    if not name or not isinstance(name, str):
        raise ValueError(f"invalid target name: {name!r}")
    result = f"{address}:{name}"
    if not _TARGET.fullmatch(result):
        raise ValueError("target identity must contain address separator")
    return result


def parse_target(target: str) -> tuple[int, str]:
    """Parse canonical target identity into ``(address, name)``.

    Raises ``ValueError`` if the target is a legacy name-only string.
    """
    parts = target.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"legacy name-only target is not supported: {target!r}")
    try:
        address = int(parts[0])
    except ValueError as err:
        raise ValueError(f"legacy name-only target is not supported: {target!r}") from err
    if not parts[1]:
        raise ValueError(f"invalid target name (empty) in: {target!r}")
    return address, parts[1]


def classify_target(target: str, expected_address: int | None = None, expected_name: str | None = None) -> None:
    """Validate target format; raise ``ValueError`` on legacy or mismatch."""
    address, name = parse_target(target)
    if expected_address is not None and address != expected_address:
        raise ValueError(f"target address mismatch: expected {expected_address}, got {address}")
    if expected_name is not None and name != expected_name:
        raise ValueError(f"target name mismatch: expected {expected_name!r}, got {name!r}")


class PromotionState(StrEnum):
    COMPILE_PASS = "COMPILE_PASS"
    ABI_PASS = "ABI_PASS"
    DIFFERENTIAL_PASS = "DIFFERENTIAL_PASS"
    PROMOTED = "PROMOTED"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ProofEvidence:
    """One content-addressed proof, deliberately independent of build state."""

    evidence_type: str
    subject: str
    payload: Mapping[str, Any]
    evidence_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def as_dict(self, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "evidence_type": self.evidence_type,
            "subject": self.subject,
            "payload": dict(self.payload),
        }
        if include_hash:
            value["evidence_sha256"] = self.evidence_sha256
        return value

    def sealed(self) -> ProofEvidence:
        return ProofEvidence(self.evidence_type, self.subject, dict(self.payload), sha256(self.as_dict(False)))

    def verify(self) -> None:
        if not self.evidence_type or not self.subject or not isinstance(self.payload, Mapping):
            raise ValueError("malformed proof evidence")
        if not _HASH.fullmatch(self.evidence_sha256) or self.evidence_sha256 != sha256(self.as_dict(False)):
            raise ValueError("invalid proof evidence hash")


@dataclass(frozen=True, slots=True)
class ProofBundle:
    """Complete proof set for one generic target/candidate pair."""

    project: str
    target: str
    candidate: str
    evidence: tuple[ProofEvidence, ...]
    bundle_sha256: str = ""

    def as_dict(self, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "project": self.project,
            "target": self.target,
            "candidate": self.candidate,
            "evidence": [item.as_dict() for item in self.evidence],
        }
        if include_hash:
            value["bundle_sha256"] = self.bundle_sha256
        return value

    def sealed(self) -> ProofBundle:
        proofs = tuple(item.sealed() for item in self.evidence)
        return ProofBundle(
            self.project,
            self.target,
            self.candidate,
            proofs,
            sha256(
                {
                    "project": self.project,
                    "target": self.target,
                    "candidate": self.candidate,
                    "evidence": [item.as_dict() for item in proofs],
                }
            ),
        )

    def verify(self) -> None:
        if not self.project or not self.target or not self.candidate or not self.evidence:
            raise ValueError("incomplete proof bundle")
        parse_target(self.target)  # reject legacy name-only targets
        for item in self.evidence:
            item.verify()
        if len({item.evidence_sha256 for item in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate proof evidence")
        if self.bundle_sha256 != sha256(
            {
                "project": self.project,
                "target": self.target,
                "candidate": self.candidate,
                "evidence": [item.as_dict() for item in self.evidence],
            }
        ):
            raise ValueError("invalid proof bundle hash")


@dataclass(frozen=True, slots=True)
class TargetState:
    project: str
    target: str
    candidate: str
    state: PromotionState
    bundle_sha256: str = ""
    proof_types: tuple[str, ...] = ()
    build_identity: str = ""

    @property
    def target_address(self) -> int:
        return parse_target(self.target)[0]

    @property
    def target_name(self) -> str:
        return parse_target(self.target)[1]


@dataclass(frozen=True, slots=True)
class ProjectState:
    project: str
    candidate: str
    state: PromotionState
    targets: tuple[TargetState, ...]
    batch_hash: str = ""
