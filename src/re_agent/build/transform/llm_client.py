from __future__ import annotations

import time
from typing import Any

import openai
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class LLMClient:
    """Thin wrapper around OpenAI-compatible chat completions API."""

    def __init__(self, cfg: Any) -> None:
        self._client = openai.OpenAI(
            base_url=cfg.llm.url,
            api_key="not-needed",
        )
        self._model = cfg.llm.model
        self._max_tokens = cfg.llm.max_tokens_response
        self._temperature = cfg.llm.temperature

        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_calls = 0

    def send(self, system: str, user: str) -> str:
        retryable = (
            RateLimitError,
            APIConnectionError,
            InternalServerError,
            APITimeoutError,
        )
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                )
            except retryable as exc:
                last_exc = exc
                wait = min(2**attempt, 10)
                time.sleep(wait)
                continue

            self.total_calls += 1
            if resp.usage:
                self.total_prompt_tokens += resp.usage.prompt_tokens
                self.total_completion_tokens += resp.usage.completion_tokens
            return resp.choices[0].message.content or ""

        raise RuntimeError("LLM call failed after 3 retries") from last_exc

    @property
    def stats(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_calls": self.total_calls,
        }
