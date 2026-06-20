"""Tests for Codex CLI token accounting attributes."""

from __future__ import annotations

from re_agent.llm.codex_cli import CodexCLIProvider


def test_codex_provider_has_token_counters() -> None:
    """CodexCLIProvider must expose total_prompt_tokens / total_completion_tokens
    (even if approximate) so it's not invisible in reports."""
    provider = CodexCLIProvider(model="m")
    assert hasattr(provider, "total_prompt_tokens")
    assert hasattr(provider, "total_completion_tokens")
    assert hasattr(provider, "total_calls")
    assert provider.total_prompt_tokens == 0
    assert provider.total_completion_tokens == 0
    assert provider.total_calls == 0
