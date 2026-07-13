"""Manifest I/O: load, save, hash, and construct ``AbiManifest`` objects.

All functions are **fail-fast**: any validation error raises immediately
with a descriptive message.

Hash semantics (two layers)
----------------------------
* **Raw bytes hash** (``abi_manifest_sha256`` in external config):
  SHA-256 of the exact file bytes on disk.  Used by external trust stores
  to verify the manifest file has not been replaced.

* **Canonical content hash** (``sha256_hash`` inside the manifest JSON):
  SHA-256 of the canonical JSON representation with the ``sha256_hash``
  field blanked.  This guarantees that a round-trip create‑→save‑→load
  always produces a matching hash, and that any content tampering is
  detected.

The two hashes are **independent** — they operate at different trust layers
and are never compared to each other.

The canonical hash procedure is:

1. Build a dict of the manifest (either from Python objects or from JSON).
2. **Copy** the dict and blank the ``sha256_hash`` key (set to ``""``).
3. Serialise the copy with ``sort_keys=True, separators=(',', ':')``.
4. Hash with SHA-256.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from re_agent.contracts.model import AbiManifest, Architecture, Symbol
from re_agent.contracts.validation import (
    _KNOWN_MANIFEST_KEYS,
    _KNOWN_SYMBOL_KEYS,
    build_symbol_from_dict,
    validate_architecture,
    validate_no_unknown_keys,
    validate_pointer_size,
    validate_symbol_fields,
    validate_symbols_nonempty,
    validate_symbols_unique,
    validate_version,
)

# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _canonical_json(data: dict[str, Any]) -> str:
    """Serialise *data* as canonical JSON (sorted keys, compact separators).

    ``allow_nan=False`` ensures that ``NaN``, ``Infinity``, and ``-Infinity``
    float values are rejected — they are not valid JSON.
    """
    return json.dumps(data, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _dict_without_hash(data: dict[str, Any]) -> dict[str, Any]:
    """Return a **copy** of *data* with ``sha256_hash`` blanked."""
    c = dict(data)
    c["sha256_hash"] = ""
    return c


def canonical_json_hash(data: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of *data* with the hash field blanked.

    The hash is computed on a **copy** of the dict where ``sha256_hash`` is
    set to ``""``, ensuring that creation and loading produce identical
    digests for identical content.

    Parameters
    ----------
    data:
        Full manifest dict (may or may not contain ``sha256_hash``).
    """
    copy = _dict_without_hash(data)
    serialised = _canonical_json(copy)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Create, save, load
# ---------------------------------------------------------------------------


def manifest_from_symbols(
    *,
    version: str,
    architecture: Architecture,
    pointer_size: int,
    symbols: list[Symbol],
) -> AbiManifest:
    """Construct an ``AbiManifest`` from validated components, computing the hash.

    Parameters
    ----------
    version:
        Semantic version string (MAJOR.MINOR.PATCH).
    architecture:
        Target CPU architecture.
    pointer_size:
        Pointer width in bytes (4 or 8).  Must be consistent with
        *architecture*.
    symbols:
        Exported symbols (at least one required).  All (address, name) and
        output_path values must be unique.

    Raises
    ------
    ValueError
        If any validation rule is violated.
    """
    validate_version(version)
    validate_pointer_size(architecture, pointer_size)
    validate_symbols_nonempty(symbols)
    for s in symbols:
        validate_symbol_fields(s, pointer_size)
    validate_symbols_unique(symbols)

    sorted_syms = tuple(sorted(symbols, key=lambda s: (s.address, s.name)))

    # Build the dict without hash → compute hash → final manifest.
    manifest_no_hash = AbiManifest(
        version=version,
        architecture=architecture,
        pointer_size=pointer_size,
        symbols=sorted_syms,
        sha256_hash="",
    )
    computed = _compute_hash(manifest_no_hash)

    return AbiManifest(
        version=version,
        architecture=architecture,
        pointer_size=pointer_size,
        symbols=sorted_syms,
        sha256_hash=computed,
    )


def save_manifest(manifest: AbiManifest, path: Path) -> None:
    """Serialise *manifest* to a JSON file at *path*.

    The write is **atomic** (write to a temp file, then rename) and
    **exclusive** (uses ``O_EXCL`` on the temp file to prevent races).

    The manifest is **re-validated** before writing so that a corrupt or
    tampered object cannot be written to disk.  Additionally the stored
    hash is verified to match the computed hash — a stale manifest (one
    whose hash was not refreshed after field changes) is rejected.
    """
    # Re-validate everything before writing.
    validate_version(manifest.version)
    validate_pointer_size(manifest.architecture, manifest.pointer_size)
    syms = list(manifest.symbols)
    validate_symbols_nonempty(syms)
    for s in syms:
        validate_symbol_fields(s, manifest.pointer_size)
    validate_symbols_unique(syms)

    # Reject stale hash: the manifest's sha256_hash must match the
    # computed hash of its current content.
    computed = _compute_hash(manifest)
    if manifest.sha256_hash != computed:
        raise ValueError(
            f"Stale manifest hash: stored={manifest.sha256_hash!r}, "
            f"computed={computed!r}.  Re-create the manifest with "
            f"manifest_from_symbols() to refresh."
        )

    # Validate output_path containment: none may escape the output directory.
    output_dir = path.parent.resolve()
    for sym in manifest.symbols:
        resolved = (output_dir / sym.output_path).resolve()
        if not str(resolved).startswith(str(output_dir) + os.sep) and resolved != output_dir:
            raise ValueError(f"output_path {sym.output_path!r} escapes parent directory {output_dir}")

    data = _manifest_to_dict(manifest)
    serialised = _canonical_json(data)

    # Atomic exclusive write: write to .tmp, then rename.
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(serialised)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, str(path))
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise


def load_manifest(path: Path) -> AbiManifest:
    """Load, validate, and return an ``AbiManifest`` from a JSON file.

    The SHA-256 hash stored in the file is verified against the computed
    hash of the loaded content.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the JSON content fails any validation rule, including SHA-256
        hash mismatch, unknown keys, or non-dict top-level.
    json.JSONDecodeError
        If the file is not valid JSON.
    """
    raw_bytes = path.read_bytes()
    return _load_from_bytes(raw_bytes)


def load_manifest_bytes(raw_bytes: bytes) -> AbiManifest:
    """Load, validate, and return an ``AbiManifest`` from raw JSON bytes.

    This is the in-memory equivalent of ``load_manifest`` — useful when the
    manifest content is already in memory (e.g. downloaded, generated, or
    embedded) and no filesystem access is needed.

    Raises
    ------
    ValueError
        If the bytes cannot be decoded as UTF-8, or if the JSON content
        fails any validation rule including SHA-256 hash mismatch.
    json.JSONDecodeError
        If the content is not valid JSON.
    """
    return _load_from_bytes(raw_bytes)


def load_verified_manifest(
    path: Path,
    *,
    expected_raw_hash: str | None = None,
) -> tuple[AbiManifest, str, str]:
    """Load a manifest and return it together with its two verified hashes.

    Two independent hashes are computed and returned:

    * **raw hash** — SHA-256 of the exact file bytes on disk.  This is what
      an external config field like ``abi_manifest_sha256`` should store.
      It is validated against *expected_raw_hash* when provided.
    * **canonical hash** — the internal ``sha256_hash`` from the manifest
      itself (canonical JSON with the hash field excluded).  This is
      always verified against the stored value inside the manifest.

    The two hashes are **never compared to each other** — they operate
    at different trust layers.

    Parameters
    ----------
    path:
        Path to the JSON manifest file.
    expected_raw_hash:
        Optional external hash to verify against the raw bytes.  When
        provided, the raw SHA-256 of the file bytes must match exactly.

    Returns
    -------
    tuple[AbiManifest, str, str]
        ``(validated_manifest, raw_sha256, canonical_sha256)``.

    Raises
    ------
    ValueError
        If *expected_raw_hash* is provided and does not match, or if any
        other validation rule is violated.
    """
    raw_bytes = path.read_bytes()
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()

    # External verification: raw bytes hash (for config/trust store).
    if expected_raw_hash is not None and raw_hash != expected_raw_hash:
        raise ValueError(f"Expected raw hash {expected_raw_hash!r} does not match computed raw hash {raw_hash!r}")

    # Internal verification: canonical JSON hash (self-consistency).
    manifest = _load_from_bytes(raw_bytes)

    return manifest, raw_hash, manifest.sha256_hash


# ---------------------------------------------------------------------------
# Shared loading internals
# ---------------------------------------------------------------------------


def _load_from_bytes(raw_bytes: bytes) -> AbiManifest:
    """Parse, validate, and return an ``AbiManifest`` from raw JSON bytes."""
    # Reject non-UTF-8 bytes early.
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Manifest content is not valid UTF-8: {exc}") from None

    parsed = json.loads(text)

    # Reject non-dict top-level JSON (e.g. array, string, number).
    if not isinstance(parsed, dict):
        raise ValueError(f"Top-level JSON must be a dict (object), got {type(parsed).__name__}")

    data: dict[str, Any] = parsed

    # ---- Structural checks first (before hash) — these don't change
    #      the integrity check; they validate schema compliance. ----
    validate_no_unknown_keys(data, _KNOWN_MANIFEST_KEYS, "manifest")

    raw_symbols = data.get("symbols", [])
    if not isinstance(raw_symbols, list):
        raise ValueError("symbols must be a JSON array")

    for item in raw_symbols:
        if not isinstance(item, dict):
            raise ValueError(f"Each symbol must be a JSON object, got {type(item).__name__}")
        # Check unknown keys at the symbol level *before* hash, so
        # structural schema violations are reported even if the hash
        # is also wrong.
        validate_no_unknown_keys(item, _KNOWN_SYMBOL_KEYS, "symbol")

    # ---- Hash verification (tamper detection). ----
    stored_hash = data.get("sha256_hash", "")
    if not isinstance(stored_hash, str) or not stored_hash:
        raise ValueError("sha256_hash must be a non-empty string")
    computed = canonical_json_hash(data)
    if stored_hash != computed:
        raise ValueError(f"SHA-256 hash mismatch: stored={stored_hash!r}, computed={computed!r}")

    # ---- Content validation. ----
    version = data.get("version", "")
    validate_version(version)

    architecture = validate_architecture(data.get("architecture", ""))
    pointer_size = validate_pointer_size(architecture, data.get("pointer_size", -1))

    symbols: list[Symbol] = []
    for item in raw_symbols:
        symbols.append(build_symbol_from_dict(item, pointer_size))

    validate_symbols_nonempty(symbols)
    validate_symbols_unique(symbols)

    sorted_syms = tuple(sorted(symbols, key=lambda s: (s.address, s.name)))
    return AbiManifest(
        version=version,
        architecture=architecture,
        pointer_size=pointer_size,
        symbols=sorted_syms,
        sha256_hash=computed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _symbol_to_dict(s: Symbol) -> dict[str, Any]:
    return {
        "address": s.address,
        "name": s.name,
        "signature": s.signature,
        "calling_convention": s.calling_convention.value,
        "output_path": s.output_path,
    }


def _manifest_to_dict(m: AbiManifest) -> dict[str, Any]:
    return {
        "version": m.version,
        "architecture": m.architecture.value,
        "pointer_size": m.pointer_size,
        "symbols": [_symbol_to_dict(s) for s in m.symbols],
        "sha256_hash": m.sha256_hash,
    }


def _compute_hash(m: AbiManifest) -> str:
    """Compute the SHA-256 hex digest of *m* (hash field excluded)."""
    data = _manifest_to_dict(m)
    return canonical_json_hash(data)
