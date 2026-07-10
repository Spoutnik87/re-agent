"""Codex CLI-backed LLM provider using ChatGPT login credentials."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from re_agent.llm.protocol import Message, ProviderUsage

_logger = logging.getLogger(__name__)
_RETRY_COUNT = 2
_RETRY_BASE_DELAY = 2.0
_RETRY_MAX_DELAY = 8.0


class CodexCLIProvider:
    """LLM provider backed by the local ``codex exec`` CLI."""

    def __init__(
        self,
        model: str = "gpt-5.4",
        timeout_s: int = 1800,
        codex_bin: str = "codex",
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._codex_bin = codex_bin
        self._conversations: dict[str, list[Message]] = {}
        # Token accounting (approximate — codex exec doesn't return usage stats,
        # so we track call count and mark tokens as untracked)
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_calls: int = 0
        # codex exec returns no usage, so cache metrics are unknown (None).
        self.total_cache_hit_tokens: int | None = None
        self.total_cache_miss_tokens: int | None = None

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        prompt = self._render_messages(messages)
        model = kwargs.get("model", self._model)

        delay = _RETRY_BASE_DELAY
        for attempt in range(_RETRY_COUNT):
            try:
                result = self._run_codex(prompt, model)
                self.total_calls += 1
                return result
            except Exception:
                if attempt == _RETRY_COUNT - 1:
                    raise
                _logger.warning("Codex exec attempt %d failed, retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
        raise RuntimeError("unreachable")

    def _run_codex(self, prompt: str, model: str) -> str:
        with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=False) as tmp:
            out_path = Path(tmp.name)

        try:
            proc = subprocess.run(
                [
                    self._codex_bin,
                    "exec",
                    "-s",
                    "read-only",
                    "--color",
                    "never",
                    "--skip-git-repo-check",
                    "--output-last-message",
                    str(out_path),
                    "-m",
                    str(model),
                    prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"codex exec failed with exit code {proc.returncode}\n{proc.stdout}")
            return out_path.read_text(encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex exec timed out after {self._timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"codex CLI not found: {self._codex_bin}") from exc
        finally:
            out_path.unlink(missing_ok=True)

    @property
    def supports_conversations(self) -> bool:
        return True

    def get_usage(self) -> ProviderUsage:
        """Return a normalized usage snapshot.

        Cache metrics are ``None`` because ``codex exec`` returns no usage
        stats ??? unknown, not a misleading 0.
        """
        return ProviderUsage(
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            calls=self.total_calls,
        )

    def new_conversation(self, system: str) -> str:
        cid = uuid.uuid4().hex
        self._conversations[cid] = [Message(role="system", content=system)]
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
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

    @staticmethod
    def _render_messages(messages: list[Message]) -> str:
        parts: list[str] = []
        for msg in messages:
            role = msg.role.upper()
            parts.append(f"[{role}]\n{msg.content.strip()}")
        return "\n\n".join(parts).strip()
