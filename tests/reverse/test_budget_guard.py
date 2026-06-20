from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from re_agent.llm.protocol import Message
from re_agent.reverse.agents.loop import run_fix_loop
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import DecompileResult, FunctionTarget


class _CountingProvider:
    """Provider that reports a rising token count to trigger the budget guard."""

    supports_conversations = False
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, tokens_per_call: int = 100_000) -> None:
        self._per_call = tokens_per_call
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        self.total_prompt_tokens += self._per_call
        return "VERDICT: FAIL\nSUMMARY: bad\nISSUES:\n- x\nFIX_INSTRUCTIONS:\n- fix it\n"

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


def _make_target() -> FunctionTarget:
    return FunctionTarget(address="0x1000", class_name="C", function_name="f")


def _make_backend() -> Any:
    backend = MagicMock(spec=REBackend)
    backend.capabilities.has_xrefs = False
    backend.capabilities.has_structs = False
    backend.decompile.return_value = DecompileResult(
        address="0x1000",
        name="f",
        signature="void f()",
        decompiled="{}",
        raw_output="void f() {}",
    )
    return backend


def test_loop_aborts_when_token_budget_exceeded(monkeypatch) -> None:
    """With a 1-token budget, the loop must abort after the first LLM call."""
    provider = _CountingProvider(tokens_per_call=100_000)
    backend = _make_backend()
    target = _make_target()

    import re_agent.reverse.agents.checker as checker_mod

    fake_checker_verdict = MagicMock(
        verdict=MagicMock(value="FAIL"),
        summary="bad",
        issues=["x"],
        fix_instructions=["fix"],
    )
    monkeypatch.setattr(checker_mod.CheckerAgent, "check", lambda self, *a, **k: fake_checker_verdict)

    result = run_fix_loop(
        target=target,
        backend=backend,
        reverser_llm=provider,
        max_rounds=4,
        optimize=True,
        enable_phase1=False,
        max_tokens_per_function=1,
    )
    assert result.success is False
    assert provider.total_calls <= 2
