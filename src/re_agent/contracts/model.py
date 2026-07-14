"""Data model for ABI contracts: enums and frozen dataclasses."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Architecture(enum.StrEnum):
    """Target CPU architecture."""

    X86 = "x86"
    X64 = "x64"
    ARM = "arm"
    ARM64 = "aarch64"


class CallingConvention(enum.StrEnum):
    """Calling convention for a symbol."""

    CDECL = "cdecl"
    STDCALL = "stdcall"
    FASTCALL = "fastcall"
    THISCALL = "thiscall"
    VECTORCALL = "vectorcall"
    SYSTEMV = "systemv"


@dataclass(frozen=True)
class Symbol:
    """A single exported symbol in an ABI contract.

    Attributes
    ----------
    address:
        Memory address of the symbol (typically a function entry point).
        Must be an ``int`` (not ``bool``), non-negative, and fit within the
        manifest's pointer width.
    name:
        Symbol name as exported by the binary.  Non-empty.
    signature:
        C/C++ function signature (return type, parameter types).  Non-empty.
    calling_convention:
        Calling convention used by this symbol.
    output_path:
        Relative POSIX ``.cpp`` file path for the decompiled output of this
        symbol.  Must use forward slashes only, no backslash/drive/UNC,
        no ``.`` or ``..`` components, and no path traversal.
    """

    address: int
    name: str
    signature: str
    calling_convention: CallingConvention
    output_path: str


@dataclass(frozen=True)
class AbiManifest:
    """Versioned ABI contract manifest with integrity hash.

    The SHA-256 hash is computed over the **canonical JSON representation**
    of all fields **except** ``sha256_hash`` itself.  This guarantees that
    creation and loading always produce an identical hash for identical
    content.

    Attributes
    ----------
    version:
        Semantic version string for the contract format (e.g. ``"1.0.0"``).
    architecture:
        Target CPU architecture.
    pointer_size:
        Pointer width in bytes (4 or 8).  Must be consistent with the
        architecture.
    symbols:
        Exported symbols in this contract.  Every address, (address, name)
        pair, and output_path must be unique within the manifest.  At least
        one symbol is required.
    sha256_hash:
        SHA-256 hex digest of the canonical JSON representation of the
        manifest (computed at creation time and verified on load).
    """

    version: str
    architecture: Architecture
    pointer_size: int
    symbols: tuple[Symbol, ...]
    sha256_hash: str
