"""Tests for the block-path compile gate."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import CheckerVerdict, DecompileResult, FunctionTarget, ObjectiveVerdict, Verdict


def _backend() -> Any:
    b = MagicMock(spec=REBackend)
    b.capabilities.has_xrefs = False
    b.capabilities.has_structs = False
    big = "void f() {\n" + "\n".join(f"  g{i}();" for i in range(120)) + "\n}\n"
    b.decompile.return_value = DecompileResult(
        address="0x1000", name="f", signature="void f()", decompiled=big, raw_output=big
    )
    return b


def _patch_block_helpers(monkeypatch, block_mod):
    """Set up mocks common to both test cases."""
    monkeypatch.setattr(block_mod, "_stitch", lambda split, parts: "void f(){}")
    monkeypatch.setattr(
        block_mod.CheckerAgent,
        "check",
        lambda self, *a, **k: CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[]),
    )
    monkeypatch.setattr(
        block_mod,
        "verify_candidate",
        lambda *a, **k: ObjectiveVerdict(verdict=Verdict.PASS, summary="ok", findings=[]),
    )
    monkeypatch.setattr(block_mod, "generate_variable_mapping", lambda **k: "")
    monkeypatch.setattr(block_mod, "extract_variable_context", lambda d, s: "")
    monkeypatch.setattr(
        block_mod,
        "split_decompiled_function",
        lambda d, **k: MagicMock(num_blocks=3, blocks=[], signature="void f()"),
    )
    monkeypatch.setattr(block_mod.BlockReverserAgent, "reverse_block", lambda self, *a, **k: "{}")
    monkeypatch.setattr(block_mod.BlockReverserAgent, "reset_conversation", lambda self: None)


def test_reverse_blocks_marks_failure_when_compile_gate_fails(monkeypatch):
    """A stitched function that never compiles must not report success when the
    compile gate is active and require_compile=True."""
    from re_agent.reverse.orchestrator import block as block_mod

    _patch_block_helpers(monkeypatch, block_mod)

    result = block_mod.reverse_blocks(
        target=FunctionTarget(address="0x1000", class_name="C", function_name="f"),
        backend=_backend(),
        llm=MagicMock(),
        compile_fn=lambda code: (False, "error: boom"),
        require_compile=True,
        max_fix_rounds=1,
    )
    assert result.success is False


def test_reverse_blocks_success_when_compile_gate_passes(monkeypatch):
    from re_agent.reverse.orchestrator import block as block_mod

    _patch_block_helpers(monkeypatch, block_mod)

    result = block_mod.reverse_blocks(
        target=FunctionTarget(address="0x1000", class_name="C", function_name="f"),
        backend=_backend(),
        llm=MagicMock(),
        compile_fn=lambda code: (True, ""),
        require_compile=True,
        max_fix_rounds=1,
    )
    assert result.success is True
