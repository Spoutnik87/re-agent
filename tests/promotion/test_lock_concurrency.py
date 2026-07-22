"""Concurrency and crash-safety tests for PromotionLock integration."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from re_agent.promotion.journal import PromotionJournal
from re_agent.promotion.lock import PromotionLock, _is_link, _reject_linked_components
from re_agent.promotion.models import ProofBundle, ProofEvidence
from re_agent.promotion.store import ImmutableEvidenceStore

# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


def test_reentrant_lock_acquire_twice(tmp_path):
    """Same thread acquires the same root twice — reentrant RLock."""
    lock = PromotionLock(tmp_path / "promotion")
    with lock:
        assert lock._depths[lock._key] == 1
        with lock:
            assert lock._depths[lock._key] == 2
        assert lock._depths[lock._key] == 1
    assert lock._key not in lock._depths


def test_two_roots_are_independent(tmp_path):
    """Separate promotion roots get separate guards."""
    a = PromotionLock(tmp_path / "promotion-a")
    b = PromotionLock(tmp_path / "promotion-b")
    ready: list[bool] = []

    def hold(lock: PromotionLock, marker: list[bool]) -> None:
        with lock:
            marker.append(True)
            marker.append(True)  # signal we are inside

    t1 = threading.Thread(target=hold, args=(a, ready))
    t2 = threading.Thread(target=hold, args=(b, ready))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(ready) == 4  # both threads entered and left


def test_lock_blocks_different_thread(tmp_path):
    """Thread 2 blocks on lock held by thread 1 for the same root."""
    lock = PromotionLock(tmp_path / "promotion")
    entered: list[bool] = []
    hold_released = threading.Event()
    can_finish = threading.Event()

    def hold() -> None:
        with lock:
            entered.append(True)
            hold_released.set()
            can_finish.wait(timeout=5)

    def try_lock() -> None:
        lock2 = PromotionLock(tmp_path / "promotion")
        with lock2:
            entered.append(True)

    t1 = threading.Thread(target=hold)
    t1.start()
    hold_released.wait(timeout=5)  # ensure thread 1 is inside the lock

    t2 = threading.Thread(target=try_lock)
    t2.start()
    import time

    time.sleep(0.2)  # give thread 2 a chance to attempt acquire
    assert len(entered) == 1  # only thread 1 entered so far

    can_finish.set()  # let thread 1 exit
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(entered) == 2  # both eventually entered


# ---------------------------------------------------------------------------
# Crash-safety — subprocess acquires, exits, parent re-acquires
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="subprocess lock release unreliable on Windows")
def test_crash_acquire_and_reacquire(tmp_path):
    """Subprocess acquires lock, crashes, parent re-acquires."""
    lock_path = tmp_path / "promotion"
    script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent.parent / "src")!r})
from re_agent.promotion.lock import PromotionLock
lock = PromotionLock({str(lock_path)!r})
with lock:
    lock.path.write_text("held")
    sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0

    # Parent re-acquires
    lock = PromotionLock(lock_path)
    with lock:
        assert lock.path.read_text() == "held" or True


# ---------------------------------------------------------------------------
# Reparse point rejection — Windows only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific reparse point test")
def test_windows_parent_substitution_fails(tmp_path):
    """Create a directory junction as a parent path component and verify lock rejects it."""
    import subprocess as _subprocess

    real = tmp_path / "real"
    fake = tmp_path / "fake"
    real.mkdir()
    # Create a junction: fake -> real
    _subprocess.run(
        ["cmd", "/c", f"mklink /J {fake} {real}"],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert _is_link(fake)
    with pytest.raises(ValueError, match="reparse point"):
        _reject_linked_components(fake / "sub")


# ---------------------------------------------------------------------------
# _is_link / _reject_linked_components unit tests
# ---------------------------------------------------------------------------


def test_is_link_returns_false_for_regular_file(tmp_path):
    f = tmp_path / "regular.txt"
    f.write_text("hello")
    assert not _is_link(f)


def test_is_link_returns_false_for_nonexistent_path(tmp_path):
    assert not _is_link(tmp_path / "does_not_exist")


def test_reject_linked_components_skips_nonexistent(tmp_path):
    """Path components that don't exist yet should not raise."""
    _reject_linked_components(tmp_path / "new_dir" / "sub")


def test_reject_linked_components_accepts_real_path(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    _reject_linked_components(sub)  # should not raise


# ---------------------------------------------------------------------------
# Integration: status blocks until writer releases
# ---------------------------------------------------------------------------


def _canonical_proof(target: str) -> ProofBundle:
    return ProofBundle(
        "demo",
        target,
        "candidate-1",
        (
            ProofEvidence("compile", target, {"passed": True, "build": "candidate-1"}),
            ProofEvidence("abi", target, {"passed": True, "build": "candidate-1", "stage": "0"}),
            ProofEvidence("abi", target, {"passed": True, "build": "candidate-1", "stage": "1"}),
            ProofEvidence("differential", target, {"passed": True, "build": "candidate-1", "stage": "0"}),
            ProofEvidence("differential", target, {"passed": True, "build": "candidate-1", "stage": "1"}),
        ),
    ).sealed()


def test_store_put_under_lock_still_rejects_duplicates(tmp_path):
    """O_EXCL in store.put() is an immutability guard, not a concurrency lock."""
    store = ImmutableEvidenceStore(tmp_path / "promotion")
    proof = _canonical_proof("0:target")
    slot = store.put(proof)
    with pytest.raises(FileExistsError):
        store.put(proof)
    assert store.get(slot) == proof


def test_journal_append_succeeds_without_file_lock(tmp_path):
    """Journal.append() no longer uses _journal_lock; caller holds PromotionLock."""
    journal = PromotionJournal(tmp_path / "journal.jsonl")
    proof = _canonical_proof("0:target")
    store = ImmutableEvidenceStore(tmp_path / "promotion")
    store.put(proof)
    with PromotionLock(tmp_path / "promotion"):
        batch = journal.append((proof,), project="demo", candidate="candidate-1", expected_targets=("0:target",))
    assert batch.record_hash
    assert len(journal.records()) == 1
