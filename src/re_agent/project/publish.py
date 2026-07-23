"""Portable, fail-closed publication of a staged directory."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from re_agent.project.snapshot import canonical_json, sha256_file


class DirectoryPublicationError(ValueError):
    """Raised when a directory cannot be published atomically."""


class DestinationExistsError(DirectoryPublicationError):
    """Raised when the publication destination already exists."""


class UnsupportedPublicationError(DirectoryPublicationError):
    """Raised when the current platform has no supported no-replace primitive."""


class PublicationFailureError(DirectoryPublicationError):
    """Raised when the platform publication primitive fails."""


@dataclass(frozen=True)
class BuildPublication:
    """The verified identity of an immutable build publication."""

    publication_id: str
    directory: Path
    artifact_sha256: str
    evidence_sha256: str
    artifact: str = "artifact"
    evidence: str = "evidence"


def _raise_publication_failure(source: Path, destination: Path, error: OSError) -> None:
    if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
        raise DestinationExistsError(f"publication destination already exists: {destination}") from error
    raise PublicationFailureError(f"failed to publish {source} as {destination}: {error}") from error


def _publish_linux(source: Path, destination: Path) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise UnsupportedPublicationError("Linux renameat2 is unavailable") from error

    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    operation_error = OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    if operation_error.errno in (errno.ENOSYS, errno.EINVAL):
        raise UnsupportedPublicationError("Linux renameat2 with RENAME_NOREPLACE is unavailable") from operation_error
    _raise_publication_failure(source, destination, operation_error)


def _publish_windows(source: Path, destination: Path) -> None:
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD]
    move_file_ex.restype = ctypes.wintypes.BOOL
    if move_file_ex(str(source), str(destination), 0x00000008):  # MOVEFILE_WRITE_THROUGH; no replace flag.
        return
    error_code = getattr(ctypes, "get_last_error")()  # noqa: B009
    if error_code in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
        raise DestinationExistsError(f"publication destination already exists: {destination}")
    error = OSError(error_code, getattr(ctypes, "FormatError")(error_code))  # noqa: B009
    raise PublicationFailureError(f"failed to publish {source} as {destination}: {error}") from error


def _publish_macos(source: Path, destination: Path) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
    except AttributeError as error:
        raise UnsupportedPublicationError("macOS renamex_np is unavailable") from error

    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    if result == 0:
        return
    operation_error = OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    if operation_error.errno in (errno.ENOSYS, errno.EINVAL):
        raise UnsupportedPublicationError("macOS renamex_np with RENAME_EXCL is unavailable") from operation_error
    _raise_publication_failure(source, destination, operation_error)


def publish_directory(source: Path, destination: Path) -> None:
    """Atomically publish *source* at an absent *destination*, never replacing it.

    The source directory is consumed by the underlying rename operation. The
    destination's parent must already exist. Unsupported platforms and missing
    kernel primitives fail closed rather than falling back to ``os.rename``.
    """
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise PublicationFailureError(f"publication source is not a directory: {source}")
    if destination.exists() or destination.is_symlink():
        raise DestinationExistsError(f"publication destination already exists: {destination}")

    if sys.platform.startswith("linux"):
        _publish_linux(source, destination)
    elif sys.platform == "win32":
        _publish_windows(source, destination)
    elif sys.platform == "darwin":
        _publish_macos(source, destination)
    else:
        raise UnsupportedPublicationError(f"unsupported publication platform: {sys.platform}")


def _validate_publication_id(publication_id: str) -> None:
    if not publication_id or publication_id in {".", ".."} or Path(publication_id).name != publication_id:
        raise DirectoryPublicationError("publication_id must be a non-empty directory name")


def _digest(root: Path, relative: str) -> str:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PublicationFailureError(f"publication reference escapes build: {relative}")
    path = root / relative_path
    if path.is_symlink() or not path.is_file():
        raise PublicationFailureError(f"publication reference is not a regular file: {relative}")
    return sha256_file(path)


def _pointer_payload(publication: BuildPublication) -> dict[str, object]:
    return {
        "format_version": 1,
        "publication_id": publication.publication_id,
        "artifact_sha256": publication.artifact_sha256,
        "evidence_sha256": publication.evidence_sha256,
        "artifact": publication.artifact,
        "evidence": publication.evidence,
    }


def _authenticated_pointer(payload: dict[str, object], auth_key: bytes | str | None) -> bytes:
    body = canonical_json(payload)
    if auth_key is None:
        authentication = {"algorithm": "sha256", "value": hashlib.sha256(body).hexdigest()}
    else:
        key = auth_key.encode("utf-8") if isinstance(auth_key, str) else auth_key
        authentication = {"algorithm": "hmac-sha256", "value": hmac.new(key, body, hashlib.sha256).hexdigest()}
    return canonical_json({**payload, "authentication": authentication})


@contextmanager
def _publication_lock(path: Path) -> Iterator[None]:
    """Serialize pointer publication using an O_EXCL lock file on all platforms."""
    lock = Path(f"{path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    token = secrets.token_hex(16)
    owns_lock = False
    try:
        try:
            handle = lock.open("x", encoding="ascii")
        except FileExistsError as exc:
            raise PublicationFailureError(f"active-link is already being published: {path}") from exc
        handle.write(token)
        handle.flush()
        owns_lock = True
        yield
    finally:
        if handle is not None:
            handle.close()
        if owns_lock:
            try:
                if lock.read_text(encoding="ascii") == token:
                    lock.unlink()
            except FileNotFoundError:
                pass


def publish_build(
    source: Path,
    root: Path,
    publication_id: str,
    *,
    artifact: str = "artifact",
    evidence: str = "evidence",
    auth_key: bytes | str | None = None,
    active_link: Path | None = None,
) -> BuildPublication:
    """Publish an immutable ``builds/<publication_id>`` and make it active.

    ``source`` is consumed only after both referenced files have been checked.  A
    failed pointer update leaves the old pointer untouched (the new immutable
    directory remains available for a later retry).
    """
    source, root = Path(source), Path(root)
    _validate_publication_id(publication_id)
    if source.is_symlink() or not source.is_dir():
        raise PublicationFailureError(f"publication source is not a directory: {source}")
    destination = root / "builds" / publication_id
    source_artifact, source_evidence = _digest(source, artifact), _digest(source, evidence)
    root.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    publication = BuildPublication(publication_id, destination, source_artifact, source_evidence, artifact, evidence)
    publish_directory(source, destination)
    if _digest(destination, artifact) != source_artifact or _digest(destination, evidence) != source_evidence:
        raise PublicationFailureError("published build failed hash verification")

    link = Path(active_link) if active_link is not None else root / "active.json"
    pointer = _authenticated_pointer(_pointer_payload(publication), auth_key)
    with _publication_lock(link):
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{link.name}.", suffix=".tmp", dir=link.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(pointer)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, link)
            temporary = None
            if os.name != "nt":
                directory_fd = os.open(link.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return publication


def load_active_build(
    root: Path,
    *,
    auth_key: bytes | str | None = None,
    active_link: Path | None = None,
) -> BuildPublication:
    """Load and authenticate the active pointer, then verify both file hashes."""
    root = Path(root)
    link = Path(active_link) if active_link is not None else root / "active.json"
    try:
        raw = json.loads(link.read_text(encoding="utf-8"))
        authentication = raw.pop("authentication")
        expected = _authenticated_pointer(raw, auth_key)
        verified = json.loads(expected)
        if raw.get("format_version") != 1 or not hmac.compare_digest(
            json.dumps(authentication, sort_keys=True),
            json.dumps(verified["authentication"], sort_keys=True),
        ):
            raise ValueError("active pointer authentication failed")
        publication_id = raw["publication_id"]
        artifact_hash, evidence_hash = raw["artifact_sha256"], raw["evidence_sha256"]
        artifact, evidence = raw.get("artifact", "artifact"), raw.get("evidence", "evidence")
        _validate_publication_id(publication_id)
        if not isinstance(artifact, str) or not isinstance(evidence, str):
            raise ValueError("active pointer contains invalid references")
        if not all(isinstance(value, str) and len(value) == 64 for value in (artifact_hash, evidence_hash)):
            raise ValueError("active pointer contains invalid hashes")
        directory = root / "builds" / publication_id
        publication = BuildPublication(publication_id, directory, artifact_hash, evidence_hash, artifact, evidence)
        if _digest(directory, artifact) != artifact_hash or _digest(directory, evidence) != evidence_hash:
            raise ValueError("active build hash verification failed")
        return publication
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, PublicationFailureError):
            raise
        raise PublicationFailureError(f"invalid active build pointer: {link}") from exc
