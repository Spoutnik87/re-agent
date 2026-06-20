from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from re_agent.llm.openai_compat import OpenAIProvider
from re_agent.llm.protocol import Message


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
