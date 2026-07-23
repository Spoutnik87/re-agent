"""Crash-releasing, process-wide promotion transaction lock."""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast

if sys.platform == "win32":
    import ctypes
    import msvcrt as _msvcrt_module  # noqa: F401 — accessed via globals() in _windows_open_non_reparse
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]


def _windows_open_non_reparse(path: str) -> int | None:
    """Open *path* with reparse-point detection, returning fd or None."""
    if sys.platform != "win32":
        return None

    # All symbols below are defined in the ``if sys.platform == "win32"`` block above.
    _k32: Any = globals()["_kernel32"]
    _msvcrt_mod: Any = globals()["_msvcrt_module"]
    _wf: Any = globals()["wintypes"]
    _info_cls: Any = globals()["_BY_HANDLE_FILE_INFORMATION"]

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_ALWAYS = 4
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400

    ctypes_mod = cast(Any, globals()["ctypes"])
    INVALID_HANDLE_VALUE = _wf.HANDLE(-1).value

    handle = _k32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_ALWAYS,
        FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes_mod.get_last_error()
        raise OSError(err, ctypes_mod.FormatError(err).strip(), path)

    info = _info_cls()
    if not _k32.GetFileInformationByHandle(handle, ctypes_mod.byref(info)):
        _k32.CloseHandle(handle)
        raise ctypes_mod.WinError()

    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        _k32.CloseHandle(handle)
        raise ValueError(f"lock file is a reparse point: {path}")

    return cast(int, _msvcrt_mod.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_NOINHERIT", 0)))


class PromotionLock:
    """One OS-backed exclusive lock per promotion root.

    The lock file is only a stable inode/name.  Ownership is provided by the
    kernel, so a crashed process releases the lock without stale-file cleanup.
    The in-process reentrant guard permits service transactions to call the
    lower-level store/journal/view APIs while retaining one fixed lock order.
    """

    _guards: ClassVar[dict[str, threading.RLock]] = {}
    _depths: ClassVar[dict[str, int]] = {}
    _handles: ClassVar[dict[str, BinaryIO]] = {}
    _root_fds: ClassVar[dict[str, int]] = {}
    _identities: ClassVar[dict[str, tuple[int, int]]] = {}
    _guards_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, root: Path) -> None:
        lexical_root = Path(os.path.abspath(root))
        _reject_linked_components(lexical_root)
        self.root = lexical_root.resolve()
        self.path = self.root / ".promotion.lock"
        self._key = os.path.normcase(str(self.root))
        self._verified_root_identity = _directory_identity(self.root)
        self._verified_parent_identity = _directory_identity(self.root.parent)
        with self._guards_lock:
            self._guard = self._guards.setdefault(self._key, threading.RLock())

    def __enter__(self) -> PromotionLock:
        self._guard.acquire()
        if self._depths.get(self._key, 0):
            retained = self._root_fds.get(self._key)
            if retained is not None:
                _validate_retained_root(
                    self.root, retained, self._verified_root_identity, self._verified_parent_identity
                )
            self._depths[self._key] += 1
            return self
        handle: BinaryIO | None = None
        root_fd: int | None = None
        try:
            use_no_follow = sys.platform != "win32"
            root_fd = _open_verified_root(self.root, self._verified_root_identity, self._verified_parent_identity)
            if root_fd is not None:
                _validate_retained_root(
                    self.root, root_fd, self._verified_root_identity, self._verified_parent_identity
                )
            if use_no_follow:
                no_follow = getattr(os, "O_NOFOLLOW", None)
                directory = getattr(os, "O_DIRECTORY", None)
                if no_follow is None or directory is None:
                    raise ValueError("promotion lock requires POSIX no-follow directory support")
                descriptor = os.open(
                    ".promotion.lock",
                    os.O_RDWR | os.O_CREAT | no_follow,
                    0o600,
                    dir_fd=root_fd,
                )
                handle = os.fdopen(descriptor, "r+b")
            else:
                fd = _windows_open_non_reparse(str(self.path))
                if fd is not None:
                    descriptor = fd
                else:
                    # Non-Windows fallback that doesn't have no-follow:
                    # fail closed — promotion requires verified root.
                    raise RuntimeError("promotion lock requires POSIX no-follow or Windows reparse-safe opening")
                handle = os.fdopen(descriptor, "r+b")
            if root_fd is not None:
                _validate_retained_root(
                    self.root, root_fd, self._verified_root_identity, self._verified_parent_identity
                )
            identity = _validate_lock_file(self.path, handle, self._identities.get(self._key), root_fd)
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt_module = cast(Any, msvcrt)
                msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_LOCK, 1)
            else:
                import fcntl

                fcntl_module = cast(Any, fcntl)
                fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_EX)
            identity = _validate_lock_file(self.path, handle, self._identities.get(self._key), root_fd)
            self._identities.setdefault(self._key, identity)
            self._handles[self._key] = handle
            if root_fd is not None:
                self._root_fds[self._key] = root_fd
                root_fd = None
            self._depths[self._key] = 1
            return self
        except Exception:
            try:
                if handle is not None:
                    handle.close()
                if root_fd is not None:
                    os.close(root_fd)
            finally:
                self._guard.release()
            raise

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        depth = self._depths[self._key]
        if depth > 1:
            self._depths[self._key] = depth - 1
            self._guard.release()
            return
        try:
            handle = self._handles.pop(self._key)
            root_fd = self._root_fds.pop(self._key, None)
            self._depths.pop(self._key)
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt_module = cast(Any, msvcrt)
                    msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl_module = cast(Any, fcntl)
                    fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
            finally:
                handle.close()
                if root_fd is not None:
                    os.close(root_fd)
        finally:
            self._guard.release()


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_linked_components(path: Path) -> None:
    current = Path(os.path.abspath(path))
    candidate = Path(current.anchor)
    for part in current.parts[1:]:
        candidate /= part
        if candidate.exists() and _is_link(candidate):
            raise ValueError("promotion lock path contains a symlink or reparse point")


def _validate_lock_file(
    path: Path, handle: BinaryIO, expected: tuple[int, int] | None, root_fd: int | None
) -> tuple[int, int]:
    _reject_linked_components(path)
    descriptor = os.fstat(handle.fileno())
    if root_fd is not None:
        named = os.stat(".promotion.lock", dir_fd=root_fd, follow_symlinks=False)
    else:
        named = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(descriptor.st_mode) or not stat.S_ISREG(named.st_mode):
        raise ValueError("promotion lock is not a regular file")
    identity = (descriptor.st_dev, descriptor.st_ino)
    if identity != (named.st_dev, named.st_ino):
        raise ValueError("promotion lock file was replaced")
    if expected is not None and identity != expected:
        raise ValueError("promotion lock file identity changed")
    return identity


def _open_verified_root(
    root: Path, expected_root: tuple[int, int] | None, expected_parent: tuple[int, int] | None
) -> int | None:
    _reject_linked_components(root)
    root.mkdir(parents=True, exist_ok=True)
    _reject_linked_components(root)
    if expected_parent is not None and _directory_identity(root.parent) != expected_parent:
        raise ValueError("promotion lock root parent was replaced")
    current_root = _directory_identity(root)
    if expected_root is not None and current_root != expected_root:
        raise ValueError("promotion lock root was replaced")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        return None
    root_fd = os.open(root, os.O_RDONLY | directory | no_follow)
    descriptor = os.fstat(root_fd)
    named = root.stat()
    if not stat.S_ISDIR(descriptor.st_mode) or (descriptor.st_dev, descriptor.st_ino) != (
        named.st_dev,
        named.st_ino,
    ):
        os.close(root_fd)
        raise ValueError("promotion lock root was replaced")
    return root_fd


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("promotion lock root component is not a directory")
    return info.st_dev, info.st_ino


def _validate_retained_root(
    root: Path,
    root_fd: int | None,
    expected_root: tuple[int, int] | None,
    expected_parent: tuple[int, int] | None,
) -> None:
    if root_fd is None:
        raise ValueError("promotion lock has no retained root directory handle")
    _reject_linked_components(root)
    if expected_parent is not None and _directory_identity(root.parent) != expected_parent:
        raise ValueError("promotion lock root parent was replaced")
    current_root = _directory_identity(root)
    if expected_root is not None and current_root != expected_root:
        raise ValueError("promotion lock root was replaced")
    descriptor = os.fstat(root_fd)
    named = root.stat()
    if not stat.S_ISDIR(descriptor.st_mode) or (descriptor.st_dev, descriptor.st_ino) != (
        named.st_dev,
        named.st_ino,
    ):
        raise ValueError("promotion lock root was replaced")


__all__ = ["PromotionLock"]
