"""Tests for normalized provider usage snapshot (ProviderUsage + get_usage).

Covers the cache-aware accounting normalization: providers that surface real
cache metrics (OpenAI-compatible with DeepSeek-style prompt_cache_*_tokens)
report them as ints in the normalized snapshot, while providers that do not
track cache (Claude, Codex) report None — never a misleading 0.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from re_agent.llm.claude import ClaudeProvider
from re_agent.llm.codex_cli import CodexCLIProvider
from re_agent.llm.protocol import Message, ProviderUsage, get_usage

# ---------------------------------------------------------------------------
# ProviderUsage dataclass
# ---------------------------------------------------------------------------


def test_provider_usage_is_frozen_dataclass() -> None:
    """ProviderUsage must be a frozen dataclass so snapshots are immutable."""
    snap = ProviderUsage(
        prompt_tokens=10,
        completion_tokens=2,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        calls=1,
    )
    assert snap.prompt_tokens == 10
    assert snap.completion_tokens == 2
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None
    assert snap.calls == 1
    try:
        snap.prompt_tokens = 99  # type: ignore[misc]
        raise AssertionError("ProviderUsage should be frozen")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# get_usage free function — fallback path for protocol-only providers
# ---------------------------------------------------------------------------


class _LegacyProvider:
    """Provider that only exposes legacy total_* int counters (no get_usage)."""

    def __init__(self) -> None:
        self.total_prompt_tokens: int = 42
        self.total_completion_tokens: int = 7
        self.total_calls: int = 3
        # Legacy int cache counters — should NOT be trusted as real metrics.
        self.total_cache_hit_tokens: int = 0
        self.total_cache_miss_tokens: int = 0


def test_get_usage_fallback_returns_none_for_cache_when_no_real_metrics() -> None:
    """A provider without a get_usage method must surface cache as None
    (unknown), not the legacy 0 — so reports never fake unknown as zero."""
    snap = get_usage(_LegacyProvider())
    assert snap.prompt_tokens == 42
    assert snap.completion_tokens == 7
    assert snap.calls == 3
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None


def test_get_usage_fallback_handles_missing_counters() -> None:
    """A provider exposing nothing must not crash the snapshot."""

    class Empty:
        pass

    snap = get_usage(Empty())
    assert snap.prompt_tokens is None
    assert snap.completion_tokens is None
    assert snap.calls is None
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None


# ---------------------------------------------------------------------------
# Claude — cache metrics must be None (provider does not capture them)
# ---------------------------------------------------------------------------


def _make_claude_response(content: str, input_tokens: int, output_tokens: int) -> Any:
    resp = MagicMock()
    resp.content = [MagicMock(text=content)]
    resp.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return resp


def test_claude_get_usage_cache_is_none_when_not_captured() -> None:
    """Claude provider must report cache_hit/miss as None, not 0, since it
    does not surface real cache metrics in the current implementation."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    provider._client = MagicMock()
    provider._client.messages.create.return_value = _make_claude_response("ok", 100, 20)
    with patch("re_agent.llm.claude.time.sleep"):
        provider.send([Message(role="user", content="hi")])

    snap = get_usage(provider)
    assert snap.prompt_tokens == 100
    assert snap.completion_tokens == 20
    assert snap.calls == 1
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None


def test_claude_legacy_cache_counters_are_none_not_zero() -> None:
    """The legacy cache counter attributes on Claude must default to None
    (unknown), not a misleading 0."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    assert provider.total_cache_hit_tokens is None
    assert provider.total_cache_miss_tokens is None


def test_claude_send_does_not_crash_without_usage_cache_fields() -> None:
    """A Claude response whose usage lacks any cache fields must not raise
    and must leave cache counters as None."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    provider._client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text="ok")]
    # usage with only input/output tokens, no cache fields
    resp.usage = MagicMock(spec=["input_tokens", "output_tokens"])
    resp.usage.input_tokens = 5
    resp.usage.output_tokens = 1
    provider._client.messages.create.return_value = resp
    with patch("re_agent.llm.claude.time.sleep"):
        provider.send([Message(role="user", content="hi")])
    assert provider.total_cache_hit_tokens is None
    assert provider.total_cache_miss_tokens is None


# ---------------------------------------------------------------------------
# Codex — cache metrics must be None (CLI returns no usage stats)
# ---------------------------------------------------------------------------


def test_codex_get_usage_cache_is_none() -> None:
    """Codex CLI provider must report cache as None (untracked), not 0."""
    provider = CodexCLIProvider(model="m")
    snap = get_usage(provider)
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None
    # prompt/completion are untracked too (CLI returns no usage), but calls=0.
    assert snap.calls == 0


def test_codex_legacy_cache_counters_are_none_not_zero() -> None:
    """Codex legacy cache counter attributes must default to None."""
    provider = CodexCLIProvider(model="m")
    assert provider.total_cache_hit_tokens is None
    assert provider.total_cache_miss_tokens is None


# ---------------------------------------------------------------------------
# Adversarial: malformed usage / missing cache fields must not fake 0
# ---------------------------------------------------------------------------


def test_get_usage_does_not_fake_zero_for_unknown_cache() -> None:
    """Regression guard: the normalized snapshot must never represent an
    unknown cache metric as 0. Unknown is None."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    snap = get_usage(provider)
    assert snap.cache_hit_tokens != 0  # noqa: E711 — explicit guard
    assert snap.cache_hit_tokens is None
