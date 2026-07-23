"""OS-backed lifetime lock for a build run directory.

The lock file is deliberately retained after release.  Its contents are only
diagnostic; ownership is represented by the kernel lock on its file
descriptor, so a crashed process cannot leave an unbreakable stale lock.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from re_agent.build._platform import _is_link

if os.name == "nt":  # pragma: no cover - platform dependent import
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - defensive, unsupported platform
        msvcrt = None  # type: ignore[assignment]
else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - defensive, unsupported platform
        fcntl = None  # type: ignore[assignment]


class RunLockError(RuntimeError):
    """Raised when a run lock cannot be acquired or released."""


class RunLock:
    """Coordinate one active process per run directory.

    ``lock_name`` is not used as a source of ownership truth.  The OS-level
    advisory/mandatory lock on the retained file is the source of truth.

    The caller **must** create ``run_directory`` before calling ``acquire()``.
    ``acquire()`` validates the directory is a real directory (not a symlink
    or reparse point) and retains a file descriptor to it for later
    ``revalidate()`` checks.
    """

    def __init__(
        self,
        run_directory: str | os.PathLike[str],
        *,
        lock_name: str = ".run.lock",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.run_directory = Path(run_directory)
        if not lock_name or Path(lock_name).name != lock_name:
            raise ValueError("lock_name must be a non-empty file name")
        self.lock_path = self.run_directory / lock_name
        self._metadata = dict(metadata or {})
        self._file: Any = None
        self._run_dir_fd: int | None = None

    @property
    def locked(self) -> bool:
        """Whether this instance currently owns the OS lock."""

        return self._file is not None

    def acquire(self) -> RunLock:
        """Acquire the lock, raising ``RunLockError`` if it is unavailable.

        The caller is responsible for creating ``run_directory`` beforehand.
        This method validates the directory is a real, non-linked directory
        and retains a file descriptor to defend against TOCTOU substitution.
        """
        if self.locked:
            raise RunLockError("run lock is already acquired by this instance")
        if os.name == "nt":
            if msvcrt is None:
                raise RunLockError("OS-backed run locks are unsupported on this platform")
        elif fcntl is None:
            raise RunLockError("OS-backed run locks are unsupported on this platform")

        if not self.run_directory.is_dir():
            raise RunLockError(f"run directory does not exist: {self.run_directory}")
        if _is_link(self.run_directory):
            raise RunLockError(f"run directory is a link: {self.run_directory}")

        file: Any | None = None
        try:
            if os.name == "nt":
                # Windows cannot retain an fd on a directory; reparse check
                # via lstat is sufficient at acquire time.
                self._run_dir_fd = None
                file = open(self.lock_path, "a+b")  # noqa: SIM115 - fd retained beyond scope
            else:
                # POSIX: open with O_NOFOLLOW + O_DIRECTORY to retain the
                # directory identity; open lock file relative to it.
                self._run_dir_fd = os.open(
                    self.run_directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                lock_fd = os.open(
                    ".run.lock",
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._run_dir_fd,
                )
                file = os.fdopen(lock_fd, "a+b")

            if file.seek(0, os.SEEK_END) == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            self._lock_file(file)

            # Verify lock file identity matches stat expectation.
            lock_fd = file.fileno()
            fd_stat = os.fstat(lock_fd)
            path_stat = self.lock_path.stat()
            if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                file.close()
                raise RunLockError("lock file descriptor identity mismatch")

            self._file = file
            self._write_metadata(file)
            return self
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if file is not None:
                with suppress(OSError):
                    self._unlock_file(file)
                file.close()
            raise RunLockError(f"unable to acquire run lock {self.lock_path}") from exc

    def revalidate(self) -> RunLock:
        """Verify retained descriptors still refer to the same directory and
        lock file.  Raises ``RunLockError`` on mismatch.

        Returns ``self`` for chaining.
        """
        if self._run_dir_fd is not None:
            try:
                fd_stat = os.fstat(self._run_dir_fd)
                current = self.run_directory.stat()
                if (fd_stat.st_dev, fd_stat.st_ino) != (current.st_dev, current.st_ino):
                    raise RunLockError("run directory identity changed since lock acquisition")
            except OSError as exc:
                raise RunLockError(f"cannot revalidate run directory: {exc}") from exc
        if self._file is not None:
            try:
                fd_stat = os.fstat(self._file.fileno())
                current = self.lock_path.stat()
                if (fd_stat.st_dev, fd_stat.st_ino) != (current.st_dev, current.st_ino):
                    raise RunLockError("lock file identity changed since lock acquisition")
            except OSError as exc:
                raise RunLockError(f"cannot revalidate lock file: {exc}") from exc
        return self

    def release(self) -> None:
        """Release the OS lock and close its descriptor."""
        file = self._file
        if file is None:
            return
        self._file = None
        if self._run_dir_fd is not None:
            with suppress(OSError):
                os.close(self._run_dir_fd)
        self._run_dir_fd = None
        try:
            self._unlock_file(file)
        finally:
            file.close()

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()

    @staticmethod
    def _lock_file(file: Any) -> None:
        if os.name == "nt":
            assert msvcrt is not None
            locking = getattr(msvcrt, "locking", None)
            nonblocking_lock = getattr(msvcrt, "LK_NBLCK", None)
            if not callable(locking) or nonblocking_lock is None:
                raise RunLockError("Windows locking support is unavailable")
            locking(file.fileno(), nonblocking_lock, 1)
        else:
            assert fcntl is not None
            flock = getattr(fcntl, "flock", None)
            exclusive = getattr(fcntl, "LOCK_EX", None)
            nonblocking = getattr(fcntl, "LOCK_NB", None)
            if not callable(flock) or exclusive is None or nonblocking is None:
                raise RunLockError("POSIX locking support is unavailable")
            flock(file.fileno(), exclusive | nonblocking)

    @staticmethod
    def _unlock_file(file: Any) -> None:
        if os.name == "nt":
            assert msvcrt is not None
            file.seek(0)
            locking = getattr(msvcrt, "locking", None)
            unlock = getattr(msvcrt, "LK_UNLCK", None)
            if not callable(locking) or unlock is None:
                raise RunLockError("Windows locking support is unavailable")
            locking(file.fileno(), unlock, 1)
        else:
            assert fcntl is not None
            flock = getattr(fcntl, "flock", None)
            unlock = getattr(fcntl, "LOCK_UN", None)
            if not callable(flock) or unlock is None:
                raise RunLockError("POSIX locking support is unavailable")
            flock(file.fileno(), unlock)

    def _write_metadata(self, file: Any) -> None:
        diagnostic = {
            **self._metadata,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }
        payload = (json.dumps(diagnostic, sort_keys=True) + "\n").encode("utf-8")
        file.seek(0)
        file.truncate()
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())


__all__ = ["RunLock", "RunLockError"]
