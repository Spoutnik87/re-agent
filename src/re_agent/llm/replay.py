"""Offline recording and exact replay provider wrappers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from re_agent.llm.protocol import LLMProvider, Message, ProviderUsage

_EFFECTIVE_CONFIG_KEYS = {
    "provider",
    "model",
    "block_model",
    "base_url",
    "max_tokens",
    "temperature",
    "timeout_s",
}


def _json_safe(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_safe(item) for key, item in value.items())
    return False


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted(((str(key), _freeze(item)) for key, item in value.items()), key=lambda item: item[0]))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _normalize_effective_config(
    config: Mapping[str, object] | tuple[tuple[str, object], ...],
) -> tuple[tuple[str, object], ...]:
    values = dict(config)
    if set(values) - _EFFECTIVE_CONFIG_KEYS or "provider" not in values or "model" not in values:
        raise ValueError("effective provider config must contain only allowed provider/model settings")
    if not all(isinstance(key, str) and _json_safe(value) for key, value in values.items()):
        raise ValueError("effective provider config must be finite JSON-safe data")
    if not isinstance(values["provider"], str) or not values["provider"]:
        raise ValueError("effective provider config provider is invalid")
    if not isinstance(values["model"], str) or not values["model"]:
        raise ValueError("effective provider config model is invalid")
    for key in ("max_tokens", "timeout_s"):
        candidate = values.get(key)
        if key in values and (isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0):
            raise ValueError(f"effective provider config {key} is invalid")
    if "temperature" in values and (
        isinstance(values["temperature"], bool)
        or not isinstance(values["temperature"], (int, float))
        or not math.isfinite(values["temperature"])
    ):
        raise ValueError("effective provider config temperature is invalid")
    if "base_url" in values and values["base_url"] is not None:
        if not isinstance(values["base_url"], str):
            raise ValueError("effective provider config base_url is invalid")
        parsed = urlsplit(values["base_url"])
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("base_url with query, fragment, or credentials is not replay-safe")
    return tuple(sorted(((key, _freeze(value)) for key, value in values.items()), key=lambda item: item[0]))


def _normalize_request_kwargs(
    config: Mapping[str, object] | tuple[tuple[str, object], ...],
) -> tuple[tuple[str, object], ...]:
    values = dict(config)
    if not all(isinstance(key, str) and key and "\x00" not in key for key in values):
        raise ValueError("provider request kwargs have invalid keys")
    if not all(_json_safe(value) for value in values.values()):
        raise ValueError("provider request kwargs must be finite JSON-safe data")
    return tuple(sorted(((key, _freeze(value)) for key, value in values.items()), key=lambda item: item[0]))


def validate_replay_request(
    current_messages: list[Message] | tuple[tuple[str, str], ...],
    current_request_kwargs: Mapping[str, object] | tuple[tuple[str, object], ...],
    recorded_messages: tuple[tuple[str, str], ...],
    recorded_request_kwargs: tuple[tuple[str, object], ...],
) -> None:
    """Reject any request whose ordered messages or kwargs differ exactly."""
    actual_messages = (
        tuple(_message_tuple(message) for message in current_messages)
        if current_messages and isinstance(current_messages[0], Message)
        else tuple(current_messages)
    )
    if actual_messages != recorded_messages:
        raise ValueError("replay request messages do not exactly match recorded messages")
    if _normalize_request_kwargs(current_request_kwargs) != _normalize_request_kwargs(recorded_request_kwargs):
        raise ValueError("replay request configuration does not exactly match recorded configuration")


def validate_replay_effective_config(
    current_config: Mapping[str, object] | tuple[tuple[str, object], ...],
    recorded_config: Mapping[str, object] | tuple[tuple[str, object], ...],
) -> None:
    """Reject effective provider configuration drift before offline replay."""
    if _normalize_effective_config(current_config) != _normalize_effective_config(recorded_config):
        raise ValueError("replay effective provider configuration does not match recorded configuration")


def _message_tuple(message: Any) -> tuple[str, str]:
    role, content = message.role, message.content
    if not isinstance(role, str) or not isinstance(content, str):
        raise ValueError("replay messages must contain string roles and content")
    return role, content


def _messages(messages: list[Message]) -> tuple[tuple[str, str], ...]:
    return tuple((message.role, message.content) for message in messages)


def _config(kwargs: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return _normalize_request_kwargs(kwargs)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    messages: tuple[tuple[str, str], ...]
    request_kwargs: tuple[tuple[str, Any], ...]
    response: str
    effective_config: tuple[tuple[str, object], ...] = ()

    @property
    def config(self) -> tuple[tuple[str, Any], ...]:
        """Compatibility alias for the exact send kwargs."""
        return self.request_kwargs


class RecordingProvider:
    """Record exact provider requests while delegating to an injected provider."""

    def __init__(self, provider: LLMProvider, effective_config: Mapping[str, object] | None = None) -> None:
        self.provider = provider
        self.calls: list[RecordedCall] = []
        self.effective_config = _normalize_effective_config(effective_config or {})

    @property
    def recorded_calls(self) -> tuple[RecordedCall, ...]:
        return tuple(self.calls)

    @property
    def supports_conversations(self) -> bool:
        return self.provider.supports_conversations

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        response = self.provider.send(messages, **kwargs)
        self.calls.append(RecordedCall(_messages(messages), _config(kwargs), response, self.effective_config))
        return response

    def new_conversation(self, system: str) -> str:
        return self.provider.new_conversation(system)

    def resume(self, conversation_id: str, message: str) -> str:
        return self.provider.resume(conversation_id, message)

    def delete_conversation(self, conversation_id: str) -> None:
        self.provider.delete_conversation(conversation_id)

    def get_usage(self) -> ProviderUsage:
        from re_agent.llm.protocol import get_usage

        return get_usage(self.provider)

    @property
    def total_prompt_tokens(self) -> int:
        return getattr(self.provider, "total_prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return getattr(self.provider, "total_completion_tokens", 0)

    @property
    def total_cache_hit_tokens(self) -> int | None:
        return getattr(self.provider, "total_cache_hit_tokens", None)

    @property
    def total_cache_miss_tokens(self) -> int | None:
        return getattr(self.provider, "total_cache_miss_tokens", None)


class ReplayProvider:
    """A no-network provider accepting one exact recorded request."""

    def __init__(
        self,
        messages: Any,
        config: tuple[tuple[str, Any], ...] | None = None,
        response: str | None = None,
        effective_config: tuple[tuple[str, object], ...] = (),
    ) -> None:
        if config is None and response is None and hasattr(messages, "messages"):
            evidence = messages
            messages, config, response = evidence.messages, evidence.request_kwargs, evidence.raw_response
            effective_config = evidence.llm_config
        if config is None or response is None:
            raise TypeError("replay requires recorded messages, configuration, and response")
        self._messages = tuple(messages)
        self._config = _normalize_request_kwargs(config)
        self._response = response
        self._effective_config = _normalize_effective_config(dict(effective_config))
        self._used = False

    @classmethod
    def from_call(cls, call: RecordedCall) -> ReplayProvider:
        return cls(call.messages, call.request_kwargs, call.response, call.effective_config)

    @classmethod
    def from_evidence(cls, evidence: Any) -> ReplayProvider:
        return cls(evidence.messages, evidence.request_kwargs, evidence.raw_response, evidence.llm_config)

    @property
    def used(self) -> bool:
        return self._used

    @property
    def supports_conversations(self) -> bool:
        return False

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        if self._used:
            raise ValueError("replay provider accepts exactly one request")
        validate_replay_request(messages, kwargs, self._messages, self._config)
        self._used = True
        return self._response

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(0, 0, None, None, 0)

    def validate_effective_config(self, current_config: Mapping[str, object]) -> None:
        validate_replay_effective_config(current_config, self._effective_config)

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cache_hit_tokens = None
    total_cache_miss_tokens = None

    def new_conversation(self, system: str) -> str:
        raise RuntimeError("replay provider does not support conversations")

    def resume(self, conversation_id: str, message: str) -> str:
        raise RuntimeError("replay provider does not support conversations")

    def delete_conversation(self, conversation_id: str) -> None:
        raise RuntimeError("replay provider does not support conversations")


__all__ = [
    "RecordedCall",
    "RecordingProvider",
    "ReplayProvider",
    "validate_replay_effective_config",
    "validate_replay_request",
]
