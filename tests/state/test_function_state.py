"""Tests for the unified per-function state store."""

from __future__ import annotations

from pathlib import Path

from re_agent.state.function_state import FunctionStateStore


def test_update_and_persist_roundtrip(tmp_path: Path):
    path = tmp_path / "functions.json"
    store = FunctionStateStore(path)
    store.update("0x401000", reversed=True, compiles=True, checker="PASS", tokens=1234)
    store.flush()

    reloaded = FunctionStateStore(path)
    rec = reloaded.get("0x401000")
    assert rec is not None
    assert rec.reversed is True
    assert rec.compiles is True
    assert rec.checker == "PASS"
    assert rec.tokens == 1234


def test_update_merges_partial_fields(tmp_path: Path):
    store = FunctionStateStore(tmp_path / "f.json")
    store.update("0x1", reversed=True)
    store.update("0x1", compiles=False)
    rec = store.get("0x1")
    assert rec.reversed is True
    assert rec.compiles is False


def test_summary_counts_by_objective(tmp_path):
    store = FunctionStateStore(tmp_path / "f.json")
    store.update("0x1", reversed=True, compiles=True, behavioral="equivalent")
    store.update("0x2", reversed=True, compiles=True, behavioral="untested")
    store.update("0x3", reversed=True, compiles=False)
    s = store.summary()
    assert s["total"] == 3
    assert s["reversed"] == 3
    assert s["compiles"] == 2
    assert s["behaviorally_equivalent"] == 1
