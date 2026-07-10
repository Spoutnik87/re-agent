"""Tests for the Phase-1 compile gate in run_fix_loop and is_pass."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from re_agent.reverse.agents.loop import run_fix_loop
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import (
    CheckerVerdict,
    DecompileResult,
    FunctionTarget,
    ObjectiveVerdict,
    Verdict,
)
from re_agent.reverse.orchestrator.stagnation import StagnationTracker


class _Provider:
    supports_conversations = False
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self) -> None:
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def send(self, messages: list[Any], **kwargs: Any) -> str:
        self.total_calls += 1
        return ""

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


def _make_backend() -> Any:
    backend = MagicMock(spec=REBackend)
    backend.capabilities.has_xrefs = False
    backend.capabilities.has_structs = False
    backend.decompile.return_value = DecompileResult(
        address="0x1000", name="f", signature="void f()", decompiled="{}", raw_output="void f() {}"
    )
    return backend


def _pass_checker() -> CheckerVerdict:
    return CheckerVerdict(verdict=Verdict.PASS, summary="ok")


def test_is_pass_legacy_two_args_unchanged() -> None:
    cv = _pass_checker()
    assert StagnationTracker.is_pass(cv) is True
    assert StagnationTracker.is_pass(cv, None) is True


def test_is_pass_compile_gate_blocks_on_false() -> None:
    cv = _pass_checker()
    assert StagnationTracker.is_pass(cv, None, compiles=False) is False
    assert StagnationTracker.is_pass(cv, None, compiles=True) is True


def test_is_pass_compile_none_disables_gate() -> None:
    cv = _pass_checker()
    # None means "no gate" -> falls back to checker/objective only.
    assert StagnationTracker.is_pass(cv, None, compiles=None) is True


def test_is_pass_objective_fail_still_blocks_even_if_compiles() -> None:
    cv = _pass_checker()
    ov = ObjectiveVerdict(verdict=Verdict.FAIL, summary="bad")
    assert StagnationTracker.is_pass(cv, ov, compiles=True) is False


def test_compile_gate_feeds_error_and_passes_after_fix(monkeypatch) -> None:
    """Checker PASS but compile fail must NOT pass round 1; the compile error is
    fed into the fix call, and once it compiles the loop succeeds."""
    from re_agent.reverse.agents import loop as loop_mod

    monkeypatch.setattr(
        loop_mod.CheckerAgent,
        "check",
        lambda self, *a, **k: CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[]),
    )
    monkeypatch.setattr(loop_mod.ReverserAgent, "reverse", lambda self, t: ("v0", "tag"))

    fix_calls: list[dict[str, Any]] = []

    def fake_fix(self: Any, **kwargs: Any) -> tuple[str, str]:
        fix_calls.append(kwargs)
        return ("v1", "tag")

    monkeypatch.setattr(loop_mod.ReverserAgent, "fix", fake_fix)

    compile_seq = iter([(False, "error: 'X' was not declared"), (True, "")])

    def compile_fn(code: str) -> tuple[bool, str]:
        return next(compile_seq)

    result = run_fix_loop(
        target=FunctionTarget(address="0x1000", class_name="C", function_name="f"),
        backend=_make_backend(),
        reverser_llm=_Provider(),
        max_rounds=4,
        optimize=False,
        enable_phase1=False,
        objective_verifier_enabled=False,
        compile_fn=compile_fn,
        require_compile=True,
    )

    assert result.success is True
    assert len(fix_calls) == 1, "fix should be called exactly once (round 2)"
    fed_issues = fix_calls[0]["issues"]
    assert any("compile" in issue.lower() for issue in fed_issues)


def test_compile_gate_require_false_does_not_block(monkeypatch) -> None:
    """When require_compile is False, a failing compile must not block PASS."""
    from re_agent.reverse.agents import loop as loop_mod

    monkeypatch.setattr(
        loop_mod.CheckerAgent,
        "check",
        lambda self, *a, **k: CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[]),
    )
    monkeypatch.setattr(loop_mod.ReverserAgent, "reverse", lambda self, t: ("v0", "tag"))

    result = run_fix_loop(
        target=FunctionTarget(address="0x1000", class_name="C", function_name="f"),
        backend=_make_backend(),
        reverser_llm=_Provider(),
        max_rounds=4,
        optimize=False,
        enable_phase1=False,
        objective_verifier_enabled=False,
        compile_fn=lambda code: (False, "boom"),
        require_compile=False,
    )
    assert result.success is True
