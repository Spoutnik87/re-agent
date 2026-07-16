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

    @property
    def locked(self) -> bool:
        """Whether this instance currently owns the OS lock."""

        return self._file is not None

    def acquire(self) -> RunLock:
        """Acquire the lock, raising ``RunLockError`` if it is unavailable."""
        if self.locked:
            raise RunLockError("run lock is already acquired by this instance")
        if os.name == "nt":
            if msvcrt is None:
                raise RunLockError("OS-backed run locks are unsupported on this platform")
        elif fcntl is None:
            raise RunLockError("OS-backed run locks are unsupported on this platform")

        file: Any | None = None
        try:
            self.run_directory.mkdir(parents=True, exist_ok=True)
            file = self.lock_path.open("a+b")
            if file.seek(0, os.SEEK_END) == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            self._lock_file(file)
            self._file = file
            self._write_metadata(file)
            return self
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if file is not None:
                with suppress(OSError):
                    self._unlock_file(file)
                file.close()
            raise RunLockError(f"unable to acquire run lock {self.lock_path}") from exc

    def release(self) -> None:
        """Release the OS lock and close its descriptor."""
        file = self._file
        if file is None:
            return
        self._file = None
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
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            assert fcntl is not None
            fcntl.flock(  # type: ignore[attr-defined]
                file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )

    @staticmethod
    def _unlock_file(file: Any) -> None:
        if os.name == "nt":
            assert msvcrt is not None
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            assert fcntl is not None
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]

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
