"""Focused public-behaviour tests for :class:`re_agent.build.RunLock`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import re_agent.build.run_lock as run_lock_module
from re_agent.build import RunLock, RunLockError

_SUPPORTED = (os.name == "nt" and run_lock_module.msvcrt is not None) or (
    os.name != "nt" and run_lock_module.fcntl is not None
)
pytestmark = pytest.mark.skipif(not _SUPPORTED, reason="RunLock OS backend is unsupported on this platform")


def _start_holder(lock_path: Path, metadata: dict[str, Any] | None = None) -> subprocess.Popen[str]:
    script = """
import json
import sys
from re_agent.build import RunLock

lock = RunLock(sys.argv[1], metadata=json.loads(sys.argv[2]))
lock.acquire()
print("ready", flush=True)
try:
    sys.stdin.readline()
finally:
    lock.release()
"""
    env = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, env.get("PYTHONPATH"))))
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), json.dumps(metadata or {})],
        cwd=source_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready", _process_error(process)
    return process


def _process_error(process: subprocess.Popen[str]) -> str:
    if process.stderr is None:
        return "lock holder failed"
    return process.stderr.read()


def _stop_holder(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        if process.stdin is not None:
            process.stdin.write("release\n")
            process.stdin.flush()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_acquire_release_and_context_lifecycle(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    lock = RunLock(run_directory, metadata={"run_id": "test-run"})

    assert not lock.locked
    assert lock.acquire() is lock
    assert lock.locked
    assert (run_directory / ".run.lock").is_file()
    lock.release()
    lock.release()
    assert not lock.locked

    with RunLock(run_directory) as context_lock:
        assert context_lock.locked
    assert not context_lock.locked


def test_concurrent_same_run_is_rejected_and_release_makes_lock_available(tmp_path: Path) -> None:
    lock_path = tmp_path / "run"
    holder = _start_holder(lock_path)
    try:
        contender = RunLock(lock_path)
        with pytest.raises(RunLockError):
            contender.acquire()
        assert not contender.locked
    finally:
        _stop_holder(holder)

    available = RunLock(lock_path)
    available.acquire()
    assert available.locked
    available.release()


def test_terminated_holder_releases_retained_lock_for_new_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "run"
    holder = _start_holder(lock_path)
    try:
        assert (lock_path / ".run.lock").is_file()

        holder.kill()
        holder.wait(timeout=5)

        reacquired = _start_holder(lock_path)
        try:
            assert reacquired.poll() is None
        finally:
            _stop_holder(reacquired)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_diagnostic_metadata_never_grants_force_break(tmp_path: Path) -> None:
    lock_path = tmp_path / "run"
    metadata = {"force": True, "owner": "diagnostic-only"}
    holder = _start_holder(lock_path, metadata)
    try:
        with pytest.raises(RunLockError):
            RunLock(lock_path, metadata={"force": True}).acquire()
    finally:
        _stop_holder(holder)

    diagnostic = json.loads((lock_path / ".run.lock").read_text(encoding="utf-8"))
    assert diagnostic["force"] is True
    assert diagnostic["owner"] == "diagnostic-only"
