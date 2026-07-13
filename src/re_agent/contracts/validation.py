"""Validation logic for ABI contracts.

All validation functions are isolated from I/O so they can be unit-tested
without fixture files.  Every function is **fail-fast**: the first violation
raises immediately with a descriptive message.
"""

from __future__ import annotations

import re
from typing import Any

from re_agent.contracts.model import Architecture, CallingConvention, Symbol

# ---------------------------------------------------------------------------
# Architecture / pointer-size helpers
# ---------------------------------------------------------------------------

_VALID_ARCHITECTURES: frozenset[str] = frozenset(m.value for m in Architecture)
_VALID_CONVENTIONS: frozenset[str] = frozenset(m.value for m in CallingConvention)

# A relaxed semver pattern: major.minor.patch with optional pre-release suffix.
# NOTE: the pre-release separator must be ``-`` or ``+``, **not** ``.``,
# so that ``1.0.0.0`` is rejected (it is not valid semver).
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.]+)?$")

# Architecture → mandatory pointer_size mapping.
_ARCH_PTR_SIZE: dict[Architecture, int] = {
    Architecture.X86: 4,
    Architecture.X64: 8,
    Architecture.ARM: 4,
    Architecture.ARM64: 8,
}


def validate_version(version: str) -> str:
    """Return *version* if it is a valid semver string, else raise.

    Accepts ``MAJOR.MINOR.PATCH`` with an optional pre-release suffix.
    """
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ValueError(f"Invalid version string: {version!r}")
    return version


def validate_architecture(arch: str) -> Architecture:
    """Return the ``Architecture`` enum member for *arch*, else raise."""
    try:
        return Architecture(arch)
    except ValueError:
        valid = ", ".join(sorted(_VALID_ARCHITECTURES))
        raise ValueError(f"Unknown architecture {arch!r}. Valid: {valid}") from None


def validate_pointer_size(arch: Architecture, size: int) -> int:
    """Return *size* if it is the correct pointer width for *arch*, else raise."""
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(f"pointer_size must be an int, got {type(size).__name__}")
    expected = _ARCH_PTR_SIZE.get(arch)
    if expected is None:
        raise ValueError(f"No pointer-size mapping for architecture {arch.value!r}")
    if size != expected:
        raise ValueError(f"pointer_size {size} is inconsistent with architecture {arch.value!r} (expected {expected})")
    return size


# ---------------------------------------------------------------------------
# Symbol-level validation
# ---------------------------------------------------------------------------


def validate_calling_convention(cc: str) -> CallingConvention:
    """Return the ``CallingConvention`` enum member for *cc*, else raise."""
    try:
        return CallingConvention(cc)
    except ValueError:
        valid = ", ".join(sorted(_VALID_CONVENTIONS))
        raise ValueError(f"Unknown calling convention {cc!r}. Valid: {valid}") from None


def validate_relative_cpp_path(path: str) -> str:
    """Validate *path* is a relative POSIX ``.cpp`` path without traversal.

    Rules
    -----
    * Must not be empty.
    * Must not contain a null byte.
    * Must end with ``.cpp``.
    * Must use forward slashes only — backslash **rejected**.
    * Must not be a DOS drive path (``C:`` / ``D:`` / etc.) or UNC (``\\\\``).
    * Must be relative (must not start with ``/``).
    * Must not contain ``.`` or ``..`` path components.
    """
    if not path:
        raise ValueError("output_path must not be empty")
    if "\x00" in path:
        raise ValueError(f"output_path contains null bytes: {path!r}")
    if not path.endswith(".cpp"):
        raise ValueError(f"output_path must end with .cpp: {path!r}")

    # ---- POSIX-only: reject backslash ----
    if "\\" in path:
        raise ValueError(f"output_path must use forward slashes, got backslash: {path!r}")

    # ---- Reject DOS drive letter (e.g. "C:foo.cpp") ----
    if re.match(r"^[a-zA-Z]:", path):
        raise ValueError(f"output_path must not use a DOS drive prefix: {path!r}")

    # ---- Reject UNC (starts with //) ----
    if path.startswith("//"):
        raise ValueError(f"output_path must not be a UNC path: {path!r}")

    # ---- Reject absolute (starts with /) ----
    if path.startswith("/"):
        raise ValueError(f"output_path must be relative, got absolute: {path!r}")

    # ---- Reject . and .. components ----
    parts = path.split("/")
    for part in parts:
        if part in (".", ".."):
            raise ValueError(f"output_path must not contain {part!r} components: {path!r}")

    # ---- Reject empty components (e.g. "foo//bar.cpp") ----
    if "" in parts:
        raise ValueError(f"output_path must not contain empty components: {path!r}")

    # ---- Single component is OK as long as it ends with .cpp ----
    return path


def validate_address(addr: int, pointer_size: int) -> int:
    """Return *addr* if it is a valid address for *pointer_size*, else raise.

    Checks:
    * Must be ``int``, not ``bool``.
    * Must be non-negative.
    * Must fit within ``2 ** (pointer_size * 8)``.
    """
    if isinstance(addr, bool):
        raise ValueError(f"Address must not be a bool, got {addr!r}")
    if not isinstance(addr, int):
        raise ValueError(f"Address must be an int, got {type(addr).__name__}")
    if addr < 0:
        raise ValueError(f"Address must be non-negative, got {addr!r}")
    max_addr = (1 << (pointer_size * 8)) - 1
    if addr > max_addr:
        raise ValueError(f"Address 0x{addr:016x} exceeds pointer width {pointer_size} (max 0x{max_addr:016x})")
    return addr


def validate_symbol_name(name: str) -> str:
    """Return *name* if it is a non-empty symbol name, else raise."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Symbol name must be a non-empty string, got {name!r}")
    return name


def validate_signature(sig: str) -> str:
    """Return *sig* if it is a non-empty signature string, else raise."""
    if not isinstance(sig, str) or not sig.strip():
        raise ValueError(f"Signature must be a non-empty string, got {sig!r}")
    return sig


# ---------------------------------------------------------------------------
# Batch / manifest-level validation
# ---------------------------------------------------------------------------


def validate_symbol_fields(s: Symbol, pointer_size: int) -> None:
    """Validate every field of an already-constructed ``Symbol``.

    This is the entry point used by ``manifest_from_symbols`` and
    ``save_manifest`` where the caller already has a ``Symbol`` object
    (as opposed to ``build_symbol_from_dict`` which starts from a JSON
    dict).
    """
    validate_address(s.address, pointer_size)
    validate_symbol_name(s.name)
    validate_signature(s.signature)
    # calling_convention is already an enum — just check type
    if not isinstance(s.calling_convention, CallingConvention):
        raise ValueError(
            f"calling_convention must be a CallingConvention enum, got {type(s.calling_convention).__name__}"
        )
    validate_relative_cpp_path(s.output_path)


def validate_symbols_nonempty(symbols: list[Symbol]) -> None:
    """Raise if the symbol list is empty."""
    if not symbols:
        raise ValueError("Manifest must contain at least one symbol")


def validate_symbols_unique(symbols: list[Symbol]) -> None:
    """Raise if any two symbols share the same address, name, or output_path.

    Every address must be unique across all symbols.  Additionally,
    each (address, name) pair and each output_path must be unique.
    """
    seen_address: set[int] = set()
    seen_addr_name: set[tuple[int, str]] = set()
    seen_output_path: set[str] = set()

    for s in symbols:
        if s.address in seen_address:
            raise ValueError(f"Duplicate address: 0x{s.address:08x}")
        seen_address.add(s.address)

        key = (s.address, s.name)
        if key in seen_addr_name:
            raise ValueError(f"Duplicate symbol: (address=0x{key[0]:08x}, name={key[1]!r})")
        seen_addr_name.add(key)

        if s.output_path in seen_output_path:
            raise ValueError(f"Duplicate output_path: {s.output_path!r}")
        seen_output_path.add(s.output_path)


# ---------------------------------------------------------------------------
# Build a Symbol from a JSON-decoded dict (full validation)
# ---------------------------------------------------------------------------

_KNOWN_MANIFEST_KEYS: frozenset[str] = frozenset({"version", "architecture", "pointer_size", "symbols", "sha256_hash"})
_KNOWN_SYMBOL_KEYS: frozenset[str] = frozenset({"address", "name", "signature", "calling_convention", "output_path"})


def validate_no_unknown_keys(data: dict[str, Any], known: frozenset[str], context: str) -> None:
    """Raise if *data* contains any key not in *known*."""
    extra = set(data) - known
    if extra:
        raise ValueError(f"Unknown keys in {context}: {sorted(extra)}")


def build_symbol_from_dict(data: dict[str, Any], pointer_size: int) -> Symbol:
    """Construct a ``Symbol`` from a raw JSON-decoded dict, validating all fields."""
    validate_no_unknown_keys(data, _KNOWN_SYMBOL_KEYS, "symbol")
    addr = validate_address(data.get("address", -1), pointer_size)
    name = validate_symbol_name(data.get("name", ""))
    sig = validate_signature(data.get("signature", ""))
    cc = validate_calling_convention(data.get("calling_convention", ""))
    output_path = validate_relative_cpp_path(data.get("output_path", ""))
    return Symbol(
        address=addr,
        name=name,
        signature=sig,
        calling_convention=cc,
        output_path=output_path,
    )
