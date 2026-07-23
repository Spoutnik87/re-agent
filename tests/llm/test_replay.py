"""Focused tests for exact offline LLM recording and replay."""

from __future__ import annotations

import pytest

from re_agent.llm.protocol import LLMProvider, Message, ProviderUsage, get_usage
from re_agent.llm.replay import RecordingProvider, ReplayProvider


class FakeProvider:
    supports_conversations = True
    total_prompt_tokens = 11
    total_completion_tokens = 7
    total_cache_hit_tokens: int | None = 3
    total_cache_miss_tokens: int | None = 2

    def __init__(self) -> None:
        self.calls = 0

    def send(self, messages: list[Message], **kwargs: object) -> str:
        self.calls += 1
        return f"response-{self.calls}"

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(11, 7, 3, 2, self.calls)

    def new_conversation(self, system: str) -> str:
        return system

    def resume(self, conversation_id: str, message: str) -> str:
        return message

    def delete_conversation(self, conversation_id: str) -> None:
        return None


def test_record_and_replay_match_messages_config_usage_and_protocol() -> None:
    live = FakeProvider()
    recorder = RecordingProvider(live, {"provider": "fake", "model": "unit", "base_url": "https://example.test/v1"})
    messages = [Message("system", "You are exact."), Message("user", "hello")]

    assert isinstance(recorder, LLMProvider)
    assert recorder.send(messages, temperature=0, max_tokens=32) == "response-1"
    call = recorder.recorded_calls[0]
    replay = ReplayProvider.from_call(call)

    assert isinstance(replay, LLMProvider)
    assert replay.send(messages, max_tokens=32, temperature=0) == "response-1"
    assert replay.get_usage() == ProviderUsage(0, 0, None, None, 0)
    assert get_usage(recorder) == ProviderUsage(11, 7, 3, 2, 1)
    assert recorder.total_prompt_tokens == 11
    assert call.config == (("max_tokens", 32), ("temperature", 0))
    replay.validate_effective_config({"provider": "fake", "model": "unit", "base_url": "https://example.test/v1"})


def test_replay_requires_exact_effective_provider_and_model_config() -> None:
    recorder = RecordingProvider(FakeProvider(), {"provider": "fake", "model": "unit"})
    recorder.send([Message("user", "hello")], temperature=0)
    replay = ReplayProvider.from_call(recorder.recorded_calls[0])

    with pytest.raises(ValueError, match="effective provider"):
        replay.validate_effective_config({"provider": "fake", "model": "other"})
    with pytest.raises(ValueError, match="effective provider"):
        replay.validate_effective_config({"provider": "other", "model": "unit"})
    with pytest.raises(ValueError, match="effective provider"):
        replay.validate_effective_config({"model": "unit"})
    with pytest.raises(ValueError, match="effective provider"):
        replay.validate_effective_config({"provider": "fake"})
    replay.validate_effective_config({"provider": "fake", "model": "unit"})


@pytest.mark.parametrize(
    "base_url",
    ["https://user:pass@example.test/v1", "https://example.test/v1?token=x", "https://example.test/v1#fragment"],
)
def test_replay_rejects_unsafe_base_url_config(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        RecordingProvider(FakeProvider(), {"provider": "fake", "model": "unit", "base_url": base_url})


@pytest.mark.parametrize(
    "messages,kwargs",
    [
        ([Message("system", "You are different."), Message("user", "hello")], {"temperature": 0}),
        ([Message("system", "You are exact."), Message("user", "goodbye")], {"temperature": 0}),
        ([Message("system", "You are exact."), Message("user", "hello")], {"temperature": 1}),
    ],
    ids=["messages-system", "messages-content", "config"],
)
def test_replay_rejects_any_message_or_config_difference(messages, kwargs) -> None:
    recorder = RecordingProvider(FakeProvider(), {"provider": "fake", "model": "unit"})
    recorder.send([Message("system", "You are exact."), Message("user", "hello")], temperature=0)
    replay = ReplayProvider.from_call(recorder.recorded_calls[0])

    with pytest.raises(ValueError, match="exactly match"):
        replay.send(messages, **kwargs)


def test_replay_never_calls_a_live_provider() -> None:
    live = FakeProvider()
    recorder = RecordingProvider(live, {"provider": "fake", "model": "unit"})
    messages = [Message("user", "offline")]
    recorder.send(messages, temperature=0)
    replay = ReplayProvider.from_call(recorder.recorded_calls[0])

    def fail_if_called(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("live provider was called")

    live.send = fail_if_called  # type: ignore[method-assign]

    assert replay.send(messages, temperature=0) == "response-1"
    assert live.calls == 1
    assert replay.used

    with pytest.raises(ValueError, match="exactly one"):
        replay.send(messages, temperature=0)
    assert live.calls == 1
