"""Portable, fail-closed publication of a staged directory."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import errno
import os
import sys
from pathlib import Path


class DirectoryPublicationError(ValueError):
    """Raised when a directory cannot be published atomically."""


class DestinationExistsError(DirectoryPublicationError):
    """Raised when the publication destination already exists."""


class UnsupportedPublicationError(DirectoryPublicationError):
    """Raised when the current platform has no supported no-replace primitive."""


class PublicationFailureError(DirectoryPublicationError):
    """Raised when the platform publication primitive fails."""


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
