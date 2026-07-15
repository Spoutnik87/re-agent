"""Safe JSON snapshot inventory and canonical serialization."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from re_agent.project.model import AnalysisMetadata, SnapshotFile


class SnapshotError(ValueError):
    """Raised when an analysis snapshot cannot be trusted."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SnapshotError(f"JSON object required: {path}")
    return value


def _is_reparse_point(path: Path) -> bool:
    """Return ``True`` if *path* is a Windows reparse point (junction, mount point, etc.).

    Returns ``False`` on non-Windows platforms where reparse points do not exist.
    """
    if os.name != "nt":
        return False
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _safe_relative(root: Path, item: Path) -> PurePosixPath:
    relative = item.relative_to(root)
    if item.is_symlink() or item.is_dir() and item.is_symlink():
        raise SnapshotError(f"links are not allowed: {item}")
    if _is_reparse_point(item):
        raise SnapshotError(f"reparse points are not allowed: {item}")
    return PurePosixPath(relative.as_posix())


def _validate_abi_path(root: Path, raw_path: str, entries: tuple[SnapshotFile, ...]) -> PurePosixPath:
    """Validate and resolve an ABI manifest path with containment and inventory checks.

    Raises:
        SnapshotError: If *raw_path* contains drive/backslash/UNC syntax,
            resolves outside *root*, or is absent from the inventory entries.
    """
    # Reject Windows drive letters (e.g. ``C:\foo``, ``c:/bar``).
    if re.search(r"^[A-Za-z]:", raw_path):
        raise SnapshotError(f"abi_manifest_path contains drive letter: {raw_path!r}")
    # Reject backslashes (Windows path separator — not valid in POSIX inventory).
    if "\\" in raw_path:
        raise SnapshotError(f"abi_manifest_path contains backslash: {raw_path!r}")
    # Reject UNC paths (both //server/share and \\server/share forms).
    if raw_path.startswith("//") or raw_path.startswith("\\\\"):
        raise SnapshotError(f"abi_manifest_path is a UNC path: {raw_path!r}")
    abi = PurePosixPath(raw_path)
    if abi.is_absolute():
        raise SnapshotError("abi_manifest_path is absolute")
    if str(abi) == ".":
        raise SnapshotError("abi_manifest_path is the root directory itself")
    if ".." in abi.parts:
        raise SnapshotError("abi_manifest_path contains parent traversal")
    # Resolve and enforce containment under root.
    try:
        resolved = (root / abi).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError(f"abi_manifest_path cannot be resolved: {raw_path!r}") from exc
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise SnapshotError(f"abi_manifest_path resolves outside snapshot root: {raw_path!r}") from exc
    # Must be an entry in the inventory (defence-in-depth: file already checked above).
    if abi not in {e.path for e in entries}:
        raise SnapshotError(f"abi_manifest_path is not in snapshot inventory: {raw_path!r}")
    return abi


def inventory_snapshot(root: Path) -> tuple[AnalysisMetadata, tuple[SnapshotFile, ...]]:
    if not root.is_dir() or root.is_symlink():
        raise SnapshotError("analysis must be a real directory")
    # On Windows, also reject junctions and other reparse points at root.
    if _is_reparse_point(root):
        raise SnapshotError(f"analysis root is a reparse point: {root}")
    entries: list[SnapshotFile] = []
    folded: set[str] = set()
    for item in sorted(root.rglob("*")):
        if item.is_dir():
            if item.is_symlink() or _is_reparse_point(item):
                raise SnapshotError(f"links are not allowed: {item}")
            continue
        if not item.is_file():
            raise SnapshotError(f"snapshot contains a non-regular file: {item}")
        rel = _safe_relative(root, item)
        if rel == PurePosixPath("snapshot.sha256"):
            continue
        if item.suffix != ".json":
            raise SnapshotError(f"snapshot contains non-JSON file: {rel}")
        key = str(rel).casefold()
        if key in folded:
            raise SnapshotError(f"case-fold path collision: {rel}")
        folded.add(key)
        load_json(item)
        entries.append(SnapshotFile(rel, sha256_file(item), item.stat().st_size))
    metadata_path = root / "analysis-metadata.json"
    if not metadata_path.is_file():
        raise SnapshotError("analysis-metadata.json is required")
    raw = load_json(metadata_path)
    required = {"schema_version", "backend", "binary_sha256", "abi_manifest_path"}
    if not required.issubset(raw) or not all(isinstance(raw[key], str) and raw[key] for key in required):
        raise SnapshotError("analysis-metadata.json has invalid fields")
    if not re.fullmatch(r"[0-9a-f]{64}", raw["binary_sha256"]):
        raise SnapshotError("analysis-metadata.json has invalid binary_sha256")
    abi = _validate_abi_path(root, raw["abi_manifest_path"], tuple(entries))
    return AnalysisMetadata(raw["schema_version"], raw["backend"], raw["binary_sha256"], abi), tuple(entries)


def manifest_document(files: tuple[SnapshotFile, ...]) -> dict[str, object]:
    return {"format_version": 1, "files": [{"path": str(f.path), "sha256": f.sha256, "size": f.size} for f in files]}
