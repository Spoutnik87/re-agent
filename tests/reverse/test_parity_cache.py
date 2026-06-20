"""Tests for ParityCache wiring into fetch_ghidra_data."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from re_agent.reverse.parity.cache import ParityCache
from re_agent.reverse.parity.engine import fetch_ghidra_data


def test_fetch_ghidra_data_uses_cache_on_second_call(tmp_path: Path) -> None:
    """fetch_ghidra_data must use ParityCache to avoid re-calling backend.decompile."""
    cache = ParityCache(tmp_path)
    backend = MagicMock()
    backend.capabilities.has_asm = False
    backend.decompile.return_value = MagicMock(
        decompiled="void f() {}",
        raw_output="void f() {}",
        callers=0,
        callees=0,
    )

    # First call: backend.decompile is called, result cached
    _ = fetch_ghidra_data("0x1000", backend, cache=cache)
    assert backend.decompile.call_count == 1

    # Second call: should hit cache, NOT call backend.decompile again
    _ = fetch_ghidra_data("0x1000", backend, cache=cache)
    assert backend.decompile.call_count == 1, "backend.decompile must not be called on cache hit"


def test_fetch_ghidra_data_without_cache_falls_back_to_backend() -> None:
    """When cache is None, fetch_ghidra_data must call the backend directly."""
    backend = MagicMock()
    backend.capabilities.has_asm = False
    backend.decompile.return_value = MagicMock(
        decompiled="void f() {}",
        raw_output="void f() {}",
        callers=0,
        callees=0,
    )
    _ = fetch_ghidra_data("0x1000", backend, cache=None)
    assert backend.decompile.call_count == 1
