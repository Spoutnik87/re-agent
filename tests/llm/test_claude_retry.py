"""Tests for Claude provider retry scope — only transient errors should retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import anthropic
import pytest

from re_agent.llm.claude import ClaudeProvider
from re_agent.llm.protocol import Message


def test_retry_on_rate_limit_error() -> None:
    """RateLimitError must be retried (transient)."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    provider._client = MagicMock()
    provider._client.messages.create.side_effect = [
        anthropic.RateLimitError("rate limited", response=MagicMock(), body=None),
        MagicMock(content=[MagicMock(text="ok")], usage=MagicMock(input_tokens=1, output_tokens=1)),
    ]
    with patch("re_agent.llm.claude.time.sleep"):
        result = provider.send([Message(role="user", content="hi")])
    assert result == "ok"
    assert provider._client.messages.create.call_count == 2


def test_no_retry_on_bad_request_error() -> None:
    """BadRequestError (4xx) must NOT be retried — it's a permanent failure."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    provider._client = MagicMock()
    provider._client.messages.create.side_effect = anthropic.BadRequestError(
        "bad model name", response=MagicMock(), body=None
    )
    with (
        patch("re_agent.llm.claude.time.sleep"),
        pytest.raises(anthropic.BadRequestError),
    ):
        provider.send([Message(role="user", content="hi")])
    assert provider._client.messages.create.call_count == 1


def test_no_retry_on_api_error_4xx() -> None:
    """APIStatusError with 4xx status must NOT be retried."""
    provider = ClaudeProvider(api_key="fake", model="m", max_tokens=10)
    provider._client = MagicMock()
    resp = MagicMock()
    resp.status_code = 422
    provider._client.messages.create.side_effect = anthropic.APIStatusError("unprocessable", response=resp, body=None)
    with (
        patch("re_agent.llm.claude.time.sleep"),
        pytest.raises(anthropic.APIStatusError),
    ):
        provider.send([Message(role="user", content="hi")])
    assert provider._client.messages.create.call_count == 1
