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


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


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


@dataclass(frozen=True, slots=True)
class ProjectState:
    project: str
    candidate: str
    state: PromotionState
    targets: tuple[TargetState, ...]
    batch_hash: str = ""
