"""LLM provider protocol and message types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Message:
    """A single message in an LLM conversation.

    Attributes:
        role: One of ``"system"``, ``"user"``, or ``"assistant"``.
        content: The text content of the message.
    """

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Normalized, reportable usage snapshot for an LLM provider.

    Cache fields are ``None`` when the provider does not surface real cache
    metrics, so unknown is never faked as a misleading ``0``. OpenAI-compatible
    endpoints that report DeepSeek-style ``prompt_cache_hit_tokens`` /
    ``prompt_cache_miss_tokens`` populate them with real ints.

    Attributes:
        prompt_tokens: Cumulative input/prompt tokens, or ``None`` if untracked.
        completion_tokens: Cumulative output/completion tokens, or ``None``.
        cache_hit_tokens: Cumulative cache-hit tokens, or ``None`` if unknown.
        cache_miss_tokens: Cumulative cache-miss tokens, or ``None`` if unknown.
        calls: Total number of successful provider calls, or ``None``.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    calls: int | None


def get_usage(provider: object) -> ProviderUsage:
    """Build a normalized :class:`ProviderUsage` snapshot from a provider.

    Prefers a provider-specific ``get_usage()`` method (so providers that
    distinguish "unknown" from "zero" cache metrics can report accurately).
    Falls back to reading legacy ``total_*`` int attributes, representing
    cache metrics as ``None`` (unknown) since the legacy counters cannot
    distinguish "0 reported" from "not tracked".
    """
    method = getattr(provider, "get_usage", None)
    if callable(method):
        return method()  # type: ignore[no-any-return]
    return ProviderUsage(
        prompt_tokens=getattr(provider, "total_prompt_tokens", None),
        completion_tokens=getattr(provider, "total_completion_tokens", None),
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        calls=getattr(provider, "total_calls", None),
    )


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that all LLM provider implementations must satisfy."""

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        """Send a list of messages to the LLM and return the response text.

        Args:
            messages: Ordered conversation messages.  If the first message has
                ``role="system"`` it is treated as the system prompt.
            **kwargs: Provider-specific overrides (e.g. ``temperature``).

        Returns:
            The assistant's response text.
        """
        ...

    @property
    def supports_conversations(self) -> bool:
        """Whether this provider supports multi-turn conversation state."""
        ...

    def new_conversation(self, system: str) -> str:
        """Start a new conversation with the given system prompt.

        Args:
            system: The system-level instruction for the conversation.

        Returns:
            A conversation ID that can be passed to :meth:`resume`.
        """
        ...

    def resume(self, conversation_id: str, message: str) -> str:
        """Resume an existing conversation with a new user message.

        Args:
            conversation_id: ID returned by :meth:`new_conversation`.
            message: The new user message.

        Returns:
            The assistant's response text.
        """
        ...

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation, freeing its history.

        Args:
            conversation_id: ID returned by :meth:`new_conversation`.
        """
        ...

    # DeepSeek context-cache metrics (optional). Providers that surface real
    # cache counters (OpenAI-compatible with prompt_cache_*_tokens) report
    # ints; providers that don't track cache (Claude, Codex) use None so
    # unknown is not faked as 0. Used by the orchestrator's cost report and
    # normalized via get_usage() into ProviderUsage.
    total_cache_hit_tokens: int | None
    total_cache_miss_tokens: int | None
    total_prompt_tokens: int
    total_completion_tokens: int
