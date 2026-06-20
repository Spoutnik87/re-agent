"""OpenAI-compatible LLM provider implementation."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import openai

from re_agent.llm.protocol import Message

_logger = logging.getLogger(__name__)
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 10.0


class OpenAIProvider:
    """LLM provider backed by any OpenAI-compatible API.

    Works with the official OpenAI API as well as any third-party endpoint
    that implements the same ``/v1/chat/completions`` interface (vLLM, Ollama,
    LM Studio, Together, etc.).

    Implements :class:`LLMProvider`.

    Args:
        api_key: API key.  If ``None``, the SDK falls back to the
            ``OPENAI_API_KEY`` environment variable.
        model: Model identifier (e.g. ``"gpt-4o"``).
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature (``0.0`` = deterministic).
        base_url: Optional base URL for an OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        base_url: str | None = None,
    ) -> None:
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._conversations: dict[str, list[Message]] = {}
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_calls: int = 0
        self.total_cache_hit_tokens: int = 0
        self.total_cache_miss_tokens: int = 0

    # -- LLMProvider interface ------------------------------------------------

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        """Send messages via the chat completions API and return the response."""
        api_messages: list[dict[str, str]] = [{"role": m.role, "content": m.content} for m in messages]

        response = self._call_with_retry(
            self._client.chat.completions.create,
            dict(
                model=kwargs.get("model", self._model),
                messages=api_messages,
                max_tokens=kwargs.get("max_tokens", self._max_tokens),
                temperature=kwargs.get("temperature", self._temperature),
            ),
        )

        choice = response.choices[0]
        if hasattr(response, "usage") and response.usage:
            self.total_prompt_tokens += response.usage.prompt_tokens or 0
            self.total_completion_tokens += response.usage.completion_tokens or 0
            hit = getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0
            miss = getattr(response.usage, "prompt_cache_miss_tokens", 0) or 0
            self.total_cache_hit_tokens += hit
            self.total_cache_miss_tokens += miss
        self.total_calls += 1
        return choice.message.content or ""

    @staticmethod
    def _call_with_retry(fn: Any, kwargs: dict[str, Any]) -> Any:
        delay = _RETRY_BASE_DELAY
        for attempt in range(_RETRY_COUNT):
            try:
                return fn(**kwargs)
            except (
                openai.RateLimitError,
                openai.APIConnectionError,
                openai.InternalServerError,
                openai.APITimeoutError,
            ):
                if attempt == _RETRY_COUNT - 1:
                    raise
                _logger.warning("OpenAI API call attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
        raise RuntimeError("unreachable")

    @property
    def supports_conversations(self) -> bool:
        """OpenAI-compatible providers support multi-turn (client-side history)."""
        return True

    def new_conversation(self, system: str) -> str:
        """Create a new conversation with a system prompt, returning its ID."""
        cid = uuid.uuid4().hex
        self._conversations[cid] = [Message(role="system", content=system)]
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
        """Append a user message to the conversation and return the response."""
        history = self._conversations.get(conversation_id)
        if history is None:
            raise KeyError(f"Unknown conversation ID: {conversation_id}")

        history.append(Message(role="user", content=message))
        response_text = self.send(list(history))
        history.append(Message(role="assistant", content=response_text))
        return response_text

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation, freeing its history."""
        self._conversations.pop(conversation_id, None)
