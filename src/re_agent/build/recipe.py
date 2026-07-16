"""Strict external build recipe and bounded subprocess execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


def _inside(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_relative(value: str) -> None:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts or ":" in value:
        raise ValueError("path must be a safe relative POSIX path")


def _safe_regular_output(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and descendants without waiting indefinitely."""
    if os.name == "nt":
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5.0,
                check=False,
            )
    else:
        with suppress(OSError, ProcessLookupError):
            kill_process_group = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(kill_process_group) and sigkill is not None:
                kill_process_group(process.pid, sigkill)
        if process.poll() is None:
            with suppress(OSError):
                process.kill()


def _reader(stream: BinaryIO, result: list[bytes], limit: int) -> None:
    kept = bytearray()
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            if len(kept) < limit:
                kept.extend(chunk[: limit - len(kept)])
    except (OSError, ValueError):
        pass
    result.append(bytes(kept))


@dataclass(frozen=True, slots=True)
class BuildRecipe:
    """Serializable command contract; it never contains a shell command line."""

    argv: tuple[str, ...]
    output: str
    staging_root: str = "."
    cwd: str = "."
    timeout_seconds: float = 60.0
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.argv):
            raise ValueError("argv must be a non-empty tuple of strings")
        _safe_relative(self.output)
        _safe_relative(self.staging_root)
        _safe_relative(self.cwd)
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        keys = set()
        for key, value in self.env:
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or key in keys
                or "=" in key
                or "\x00" in key
                or "\x00" in value
            ):
                raise ValueError("env must contain unique sanitized key/value pairs")
            keys.add(key)

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env": [[k, v] for k, v in self.env],
            "output": self.output,
            "staging_root": self.staging_root,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def identity(self) -> str:
        serialized = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        return hashlib.sha256(serialized.encode()).hexdigest()

    @property
    def recipe_sha256(self) -> str:
        """Digest spelling used by evidence records."""
        return self.identity

    @property
    def declared_output(self) -> str:
        """Compatibility spelling for the declared relative output."""
        return self.output

    def validate_paths(self, staging: Path) -> tuple[Path, Path]:
        if staging.is_symlink() or not staging.is_dir():
            raise ValueError("staging root must be an existing real directory")
        root = (staging / PurePosixPath(self.staging_root)).resolve()
        if not root.is_dir() or root.is_symlink():
            raise ValueError("recipe staging_root must be an existing real directory")
        cwd = (root / PurePosixPath(self.cwd)).resolve()
        output = (root / PurePosixPath(self.output)).resolve()
        if not _inside(root, cwd) or not _inside(root, output):
            raise ValueError("recipe path escapes staging root")
        return cwd, output


@dataclass(frozen=True, slots=True)
class BuildRunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    recipe_sha256: str
    output_sha256: str
    error: str = ""

    @property
    def successful(self) -> bool:
        return self.returncode == 0 and not self.timed_out and bool(self.output_sha256) and not self.error


def run_recipe(recipe: BuildRecipe, staging: Path, *, max_output_bytes: int = 1_048_576) -> BuildRunResult:
    """Run a recipe with no shell, explicit environment, bounded captures, and output hashing."""
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    cwd, output = recipe.validate_paths(staging)
    env = {key: value for key, value in recipe.env}
    error = ""

    process: subprocess.Popen[bytes] | None = None
    try:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            list(recipe.argv),
            cwd=cwd,
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_result: list[bytes] = []
        stderr_result: list[bytes] = []
        stdout_thread = threading.Thread(
            target=_reader, args=(process.stdout, stdout_result, max_output_bytes), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_reader, args=(process.stderr, stderr_result, max_output_bytes), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=recipe.timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
            returncode = None
            timed_out = True
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            process.stdout.close()
            process.stderr.close()
            stdout_thread.join(timeout=0.5)
            stderr_thread.join(timeout=0.5)
        stdout = (stdout_result[0] if stdout_result else b"").decode("utf-8", "replace")
        stderr = (stderr_result[0] if stderr_result else b"").decode("utf-8", "replace")
        if timed_out:
            error = "timeout"
    except OSError as exc:
        timed_out, returncode = False, None
        stdout, stderr, error = "", "", str(exc)
    output_hash = _safe_regular_output(output)
    if returncode == 0 and not output_hash and not error:
        error = "declared output is missing, symlinked, empty, or not a regular file"
    return BuildRunResult(returncode, stdout, stderr, timed_out, recipe.identity, output_hash, error)


__all__ = ["BuildRecipe", "BuildRunResult", "run_recipe"]
