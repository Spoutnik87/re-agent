from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from re_agent.llm.openai_compat import OpenAIProvider
from re_agent.llm.protocol import Message, get_usage


def _make_response(
    content: str, prompt_tokens: int, completion_tokens: int, cache_hit: int = 0, cache_miss: int = 0
) -> Any:
    """Build a fake OpenAI response object with DeepSeek cache fields."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    # DeepSeek-specific fields (absent on plain OpenAI)
    usage.prompt_cache_hit_tokens = cache_hit
    usage.prompt_cache_miss_tokens = cache_miss
    resp.usage = usage
    return resp


def _make_response_without_cache_fields(content: str, prompt_tokens: int, completion_tokens: int) -> Any:
    """Build a fake OpenAI response whose usage has NO cache attributes at all
    (plain OpenAI / vLLM without DeepSeek cache reporting)."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    usage = MagicMock(spec=["prompt_tokens", "completion_tokens"])
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    resp.usage = usage
    return resp


def test_send_accumulates_cache_hit_and_miss_tokens(monkeypatch) -> None:
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    provider._client = MagicMock()
    provider._client.chat.completions.create.side_effect = [
        _make_response("a", prompt_tokens=100, completion_tokens=10, cache_hit=60, cache_miss=40),
        _make_response("b", prompt_tokens=200, completion_tokens=20, cache_hit=150, cache_miss=50),
    ]
    provider.send([Message(role="user", content="hi")])
    provider.send([Message(role="user", content="again")])
    assert provider.total_prompt_tokens == 300
    assert provider.total_completion_tokens == 30
    assert provider.total_cache_hit_tokens == 210
    assert provider.total_cache_miss_tokens == 90


def test_send_handles_absent_cache_fields_gracefully(monkeypatch) -> None:
    """Plain OpenAI responses don't have prompt_cache_*_tokens — must not crash."""
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    provider._client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "ok"
    usage = MagicMock(spec=[])  # no attributes
    usage.prompt_tokens = 50
    usage.completion_tokens = 5
    resp.usage = usage
    provider._client.chat.completions.create.return_value = resp
    provider.send([Message(role="user", content="hi")])
    assert provider.total_cache_hit_tokens == 0
    assert provider.total_cache_miss_tokens == 0


# ---------------------------------------------------------------------------
# Normalized ProviderUsage snapshot (cache-aware accounting)
# ---------------------------------------------------------------------------


def test_get_usage_preserves_cache_hit_and_miss_when_present() -> None:
    """Requirement 1: when response usage has prompt_cache_hit_tokens and
    prompt_cache_miss_tokens, the normalized snapshot must surface the real
    accumulated ints (not None, not 0)."""
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    provider._client = MagicMock()
    provider._client.chat.completions.create.side_effect = [
        _make_response("a", prompt_tokens=100, completion_tokens=10, cache_hit=60, cache_miss=40),
        _make_response("b", prompt_tokens=200, completion_tokens=20, cache_hit=150, cache_miss=50),
    ]
    provider.send([Message(role="user", content="hi")])
    provider.send([Message(role="user", content="again")])

    snap = get_usage(provider)
    assert snap.prompt_tokens == 300
    assert snap.completion_tokens == 30
    assert snap.calls == 2
    assert snap.cache_hit_tokens == 210
    assert snap.cache_miss_tokens == 90


def test_get_usage_cache_is_none_when_cache_fields_never_observed() -> None:
    """Requirement 2: when OpenAI-compatible usage never carries cache fields,
    the normalized snapshot must report cache as None (unknown), not a
    misleading 0. Legacy total_cache_*_tokens stay 0 and must not crash."""
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _make_response_without_cache_fields(
        "ok", prompt_tokens=50, completion_tokens=5
    )
    provider.send([Message(role="user", content="hi")])

    # Legacy counters stay 0 (backwards compatible) and don't crash.
    assert provider.total_cache_hit_tokens == 0
    assert provider.total_cache_miss_tokens == 0

    # Normalized snapshot represents unknown cache as None.
    snap = get_usage(provider)
    assert snap.prompt_tokens == 50
    assert snap.completion_tokens == 5
    assert snap.calls == 1
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None


def test_get_usage_cache_becomes_int_once_observed_even_if_later_absent() -> None:
    """Once a provider has observed real cache fields, the snapshot reports
    the accumulated int (which may be 0); it does not revert to None."""
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    provider._client = MagicMock()
    provider._client.chat.completions.create.side_effect = [
        _make_response("a", prompt_tokens=10, completion_tokens=1, cache_hit=5, cache_miss=5),
        _make_response_without_cache_fields("b", prompt_tokens=20, completion_tokens=2),
    ]
    provider.send([Message(role="user", content="hi")])
    provider.send([Message(role="user", content="again")])

    snap = get_usage(provider)
    assert snap.cache_hit_tokens == 5
    assert snap.cache_miss_tokens == 5


def test_get_usage_on_fresh_openai_provider_cache_is_none() -> None:
    """A brand-new OpenAI provider that has never sent must report cache as
    None (unknown), not 0."""
    provider = OpenAIProvider(api_key="k", model="m", max_tokens=100)
    snap = get_usage(provider)
    assert snap.cache_hit_tokens is None
    assert snap.cache_miss_tokens is None
    assert snap.calls == 0
    assert snap.prompt_tokens == 0
    assert snap.completion_tokens == 0
