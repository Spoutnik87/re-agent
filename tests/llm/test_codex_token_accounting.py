"""Tests for Codex CLI token accounting attributes."""

from __future__ import annotations

from re_agent.llm.codex_cli import CodexCLIProvider
from re_agent.llm.protocol import get_usage


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


def test_codex_cache_counters_default_to_none_not_zero() -> None:
    """Codex CLI returns no usage stats, so cache metrics are unknown ??? must
    be None, not a misleading 0."""
    provider = CodexCLIProvider(model="m")
    assert provider.total_cache_hit_tokens is None
    assert provider.total_cache_miss_tokens is None


def test_codex_get_usage_cache_is_none() -> None:
    """Normalized snapshot for Codex must surface cache as None (unknown)."""
    provider = CodexCLIProvider(model="m")
    snap = get_usage(provider)
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None
    assert snap.calls == 0
