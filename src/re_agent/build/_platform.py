"""Shared platform-level path utilities for build admission and evidence identity.

Functions
---------
_is_link
    Detect symlinks (POSIX) and reparse points (Windows).
_reject_linked_components
    Walk path components, rejecting any link.
_directory_identity
    Return ``(dev, ino)`` or ``None``.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path


def _is_link(path: Path) -> bool:
    """Return ``True`` if *path* is a symlink (POSIX) or reparse point (Windows)."""
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            st = path.lstat()
            attributes = getattr(st, "st_file_attributes", 0)
            reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if attributes & reparse_flag:
                return True
        return False
    except FileNotFoundError, OSError:
        return False


def _reject_linked_components(path: Path) -> None:
    """Walk all components of *path* and raise ``ValueError`` if any is a link."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if not current.exists() and not current.is_symlink():
            continue
        if _is_link(current):
            raise ValueError(f"path contains a symlink or reparse point: {current}")


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` for *path*, or ``None`` on error."""
    try:
        st = path.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


__all__ = [
    "_is_link",
    "_reject_linked_components",
    "_directory_identity",
]
