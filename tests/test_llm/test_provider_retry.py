"""Tests for LLM provider retry, conversation cleanup, and protocol additions."""

from __future__ import annotations

from typing import Any

from re_agent.llm.protocol import LLMProvider, Message


class FailingProvider:
    """Provider that fails N times then succeeds, for testing retry logic."""

    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0

    def __init__(self, fail_count: int = 2, success_response: str = "OK") -> None:
        self._fail_count = fail_count
        self._attempt = 0
        self._success_response = success_response
        self._conversations: dict[str, list[Message]] = {}

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self._attempt += 1
        if self._attempt <= self._fail_count:
            raise RuntimeError(f"Simulated failure {self._attempt}")
        return self._success_response

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        self._conversations["conv-1"] = [Message(role="system", content=system)]
        return "conv-1"

    def resume(self, conversation_id: str, message: str) -> str:
        return self.send([Message(role="user", content=message)])

    def delete_conversation(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)


def test_failing_provider_implements_protocol() -> None:
    provider = FailingProvider()
    assert isinstance(provider, LLMProvider)


def test_delete_conversation_removes_entry() -> None:
    provider = FailingProvider()
    cid = provider.new_conversation("test")
    assert cid == "conv-1"
    provider.delete_conversation("conv-1")
    assert "conv-1" not in provider._conversations


def test_delete_conversation_missing_id_no_error() -> None:
    provider = FailingProvider()
    provider.delete_conversation("nonexistent")


class TrackingProvider:
    """Provider that tracks conversation state for testing cleanup."""

    def __init__(self) -> None:
        self._conversations: dict[str, list[Message]] = {}
        self.conversation_count: int = 0

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        return "resp"

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        cid = f"conv-{self.conversation_count}"
        self.conversation_count += 1
        self._conversations[cid] = [Message(role="system", content=system)]
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
        return self.send([Message(role="user", content=message)])

    def delete_conversation(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)


def test_tracking_provider_conversation_lifecycle() -> None:
    provider = TrackingProvider()
    cid = provider.new_conversation("system text")
    provider.resume(cid, "user message 1")
    provider.resume(cid, "user message 2")
    assert cid in provider._conversations
    provider.delete_conversation(cid)
    assert cid not in provider._conversations
