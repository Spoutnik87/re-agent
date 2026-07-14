"""Runtime identities for validated contract artifacts.

The objects in this module are deliberately small immutable containers.  They
are the hand-off point between configuration loading and consumers such as
Transform: a consumer can use the validated value and its file identity
without opening the file again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class VerifiedContract(Generic[T]):
    """A validated contract value together with its immutable file identity.

    ``manifest`` is already validated by the producer.  Hashes are hexadecimal
    SHA-256 digests: ``raw_sha256`` identifies the exact bytes on disk and
    ``canonical_sha256`` identifies the validated canonical content.
    """

    manifest: T
    resolved_path: Path
    raw_sha256: str
    canonical_sha256: str

    @property
    def value(self) -> T:
        """Generic spelling for consumers that do not know the value shape."""
        return self.manifest


# Descriptive aliases retain the generic runtime container while making the
# ABI use-site self-documenting.
VerifiedManifest = VerifiedContract
VerifiedAbiManifest = VerifiedContract


__all__ = ["VerifiedContract", "VerifiedManifest", "VerifiedAbiManifest"]
