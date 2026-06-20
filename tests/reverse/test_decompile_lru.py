from __future__ import annotations

from unittest.mock import patch

from re_agent.reverse.backend.ghidra_bridge import GhidraBridgeBackend


def test_decompile_caches_repeated_calls() -> None:
    """Decompiling the same address twice must shell out only once."""
    backend = GhidraBridgeBackend(cli_path="ghidra", timeout_s=5)

    raw_output = "void f() { return; }\nCallers: 1 | Callees: 0"

    with patch.object(backend, "_run", return_value=raw_output) as mock_run:
        result1 = backend.decompile("0x1000")
        result2 = backend.decompile("0x1000")

    assert mock_run.call_count == 1, "backend._run must be called once (cached on 2nd call)"
    assert result1.raw_output == raw_output
    assert result2.raw_output == raw_output


def test_decompile_cache_different_addresses() -> None:
    """Different addresses must not hit the same cache entry."""
    backend = GhidraBridgeBackend(cli_path="ghidra", timeout_s=5)

    with patch.object(backend, "_run", side_effect=["void a() {}", "void b() {}"]) as mock_run:
        backend.decompile("0x1000")
        backend.decompile("0x2000")

    assert mock_run.call_count == 2


def test_decompile_clear_cache() -> None:
    """clear_cache() must invalidate the decompile cache."""
    backend = GhidraBridgeBackend(cli_path="ghidra", timeout_s=5)

    with patch.object(backend, "_run", return_value="void f() {}") as mock_run:
        backend.decompile("0x1000")
        backend.clear_cache()
        backend.decompile("0x1000")

    assert mock_run.call_count == 2
