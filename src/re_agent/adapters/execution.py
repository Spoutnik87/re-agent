"""Bounded, shell-free execution of a generic Release 5 adapter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4

from re_agent.adapters.contracts import AdapterAttachment, AdapterRequest, AdapterResult
from re_agent.toolchain.activation import VerifiedCommand, verify_command


def _read(stream: BinaryIO, box: list[bytes], limit: int) -> None:
    kept = bytearray()
    try:
        while chunk := stream.read(65536):
            if len(kept) < limit:
                kept.extend(chunk[: limit - len(kept)])
    except OSError, ValueError:
        pass
    box.append(bytes(kept))


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                check=False,
            )
    else:
        with suppress(OSError, ProcessLookupError):
            kill_process_group = getattr(os, "killpg", None)
            sigkill = getattr(signal, "SIGKILL", None)
            if callable(kill_process_group) and sigkill is not None:
                kill_process_group(process.pid, sigkill)
        with suppress(OSError):
            process.kill()


def _attachment(root: Path, item: AdapterAttachment) -> AdapterAttachment:
    path = (root / item.path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("attachment escapes execution root") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("attachment must be a non-empty regular file")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item.sha256 or path.stat().st_size != item.size_bytes:
        raise ValueError("attachment identity does not match the result")
    return item


@dataclass(frozen=True, slots=True)
class AdapterEvidence:
    """Immutable-once-published paths produced by one adapter invocation."""

    directory: Path
    request_path: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    attachments: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class AdapterExecution:
    result: AdapterResult
    stdout: str
    stderr: str
    evidence: AdapterEvidence


def _new_evidence_dir(parent: Path | None, root: Path) -> Path:
    if parent is None:
        return Path(tempfile.mkdtemp(prefix="re-agent-adapter-"))
    base = parent
    if base.is_symlink() or not base.is_dir():
        raise ValueError("evidence staging must be an existing real directory")
    directory = base / f".adapter-evidence-{uuid4().hex}"
    directory.mkdir()
    return directory


def execute_adapter(
    command: VerifiedCommand,
    request: AdapterRequest,
    root: Path,
    *,
    staging: Path | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 1_048_576,
    env: dict[str, str] | None = None,
) -> tuple[AdapterResult, str, str]:
    """Invoke ``command --request <file> --result <file>`` safely.

    The returned strings are bounded UTF-8 stdout and stderr.  The adapter's
    result is independently validated, including every attachment hash.
    """
    execution = _execute_adapter(
        command,
        request,
        root,
        staging=staging,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        env=env,
    )
    return execution.result, execution.stdout, execution.stderr


def execute_adapter_with_evidence(
    command: VerifiedCommand,
    request: AdapterRequest,
    root: Path,
    *,
    staging: Path | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 1_048_576,
    env: dict[str, str] | None = None,
) -> AdapterExecution:
    """Execute an adapter and retain a complete caller-owned evidence bundle.

    ``staging`` is a caller-owned parent.  A fresh child is created beneath
    it; no file in the parent or child is removed by this function.
    """
    return _execute_adapter(
        command,
        request,
        root,
        staging=staging,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        env=env,
    )


def _execute_adapter(
    command: VerifiedCommand,
    request: AdapterRequest,
    root: Path,
    *,
    staging: Path | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 1_048_576,
    env: dict[str, str] | None = None,
) -> AdapterExecution:
    if (
        not isinstance(command, VerifiedCommand)
        or tuple(command.argv) != request.command.argv
        or command.executable_sha256 != request.command.executable_sha256
    ):
        raise ValueError("verified command does not match request command")
    verify_command(command)
    if (
        not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise ValueError("timeout_seconds and max_output_bytes must be positive")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("execution root must be an existing real directory")
    safe_env: dict[str, str] = {}
    for key, value in (env or {}).items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValueError("env must contain sanitized string key/value pairs")
        safe_env[key] = value
    evidence_dir = _new_evidence_dir(staging, root)
    bound_request = request.with_input_hashes(root)
    request_file = evidence_dir / "request.json"
    result_file = evidence_dir / "result.json"
    stdout_file = evidence_dir / "stdout.bin"
    stderr_file = evidence_dir / "stderr.bin"
    request_file.write_bytes(bound_request.to_json_bytes())
    stdout_box: list[bytes] = []
    stderr_box: list[bytes] = []
    process: subprocess.Popen[bytes] | None = None
    try:
        argv = [*command.argv, "--request", str(request_file), "--result", str(result_file)]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=safe_env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=flags,
        )
        assert process.stdout is not None and process.stderr is not None
        out_thread = threading.Thread(target=_read, args=(process.stdout, stdout_box, max_output_bytes), daemon=True)
        err_thread = threading.Thread(target=_read, args=(process.stderr, stderr_box, max_output_bytes), daemon=True)
        out_thread.start()
        err_thread.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_tree(process)
            raise TimeoutError("adapter execution timed out") from exc
        out_thread.join(2)
        err_thread.join(2)
        stdout = stdout_box[0] if stdout_box else b""
        stderr = stderr_box[0] if stderr_box else b""
        stdout_file.write_bytes(stdout)
        stderr_file.write_bytes(stderr)
        if process.returncode != 0:
            raise RuntimeError(f"adapter exited with status {process.returncode}")
        # The adapter ran with access to the project inputs.  Re-authenticate
        # every declared input after it exits, before accepting its result or
        # copying any attachment into the evidence bundle.
        post_request = bound_request.with_input_hashes(root)
        if post_request.identity != bound_request.identity:
            raise ValueError("declared input changed during adapter execution")
        data = json.loads(result_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("adapter result must be a JSON object")
        result = AdapterResult.from_dict(data, expected_request_sha256=bound_request.identity)
        result_file.write_bytes((json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode())
        staged_attachments: list[Path] = []
        for item in result.attachments:
            _attachment(root, item)
            destination = evidence_dir / "attachments" / PurePosixPath(item.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / PurePosixPath(item.path), destination)
            staged_attachments.append(destination)
        return AdapterExecution(
            result,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            AdapterEvidence(
                evidence_dir,
                request_file,
                result_file,
                stdout_file,
                stderr_file,
                tuple(staged_attachments),
            ),
        )
    except BaseException:
        raise


__all__ = ["AdapterEvidence", "AdapterExecution", "execute_adapter", "execute_adapter_with_evidence"]
