"""ABI contract model: versioned, hashed manifest with validated symbols.

A minimal, generic contract format — not tied to any specific binary.
Each ``AbiManifest`` captures the architecture, pointer width, and a set of
unique exported symbols.  Manifests are self-validating via a SHA-256 hash of
their canonical JSON representation.
"""

from __future__ import annotations

from re_agent.contracts.manifest import (
    canonical_json_hash,
    load_manifest,
    load_manifest_bytes,
    load_verified_manifest,
    manifest_from_symbols,
    save_manifest,
)
from re_agent.contracts.model import AbiManifest, Architecture, CallingConvention, Symbol
from re_agent.contracts.runtime import VerifiedAbiManifest, VerifiedContract, VerifiedManifest

__all__ = [
    "AbiManifest",
    "Architecture",
    "CallingConvention",
    "Symbol",
    "canonical_json_hash",
    "load_manifest",
    "load_manifest_bytes",
    "load_verified_manifest",
    "manifest_from_symbols",
    "save_manifest",
    "VerifiedContract",
    "VerifiedManifest",
    "VerifiedAbiManifest",
]
