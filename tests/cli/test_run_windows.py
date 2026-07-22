"""Windows-specific CLI coverage for RunLock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from re_agent.build import RunLock

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-specific RunLock policy")


def test_windows_run_lock_acquires_and_releases_from_cli(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    lock = RunLock(run_directory)
    lock.acquire()
    assert lock.locked
    assert (run_directory / ".run.lock").exists()
    lock.release()
    assert not lock.locked
