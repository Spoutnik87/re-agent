"""LLM provider factory registry."""
from __future__ import annotations

from re_agent.config.schema import LLMConfig
from re_agent.llm.protocol import LLMProvider


def create_provider(config: LLMConfig) -> LLMProvider:
    """Instantiate an LLM provider from a configuration object.

    Args:
        config: The LLM configuration specifying provider type, model,
            API key, and other parameters.

    Returns:
        An object satisfying the :class:`LLMProvider` protocol.

    Raises:
        ValueError: If ``config.provider`` is not a recognised provider name.
    """
    return _create_provider_for_model(config, config.model)


def create_block_provider(config: LLMConfig) -> LLMProvider | None:
    """Create a provider for block-level reversals.

    Returns a provider using ``config.block_model`` when set,
    or ``None`` when the main model should be used for everything.
    """
    if config.block_model is None:
        return None
    return _create_provider_for_model(config, config.block_model)


def _create_provider_for_model(config: LLMConfig, model: str) -> LLMProvider:
    if config.provider == "claude":
        from re_agent.llm.claude import ClaudeProvider

        return ClaudeProvider(
            api_key=config.api_key,
            model=model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

    if config.provider in ("openai", "openai-compat"):
        from re_agent.llm.openai_compat import OpenAIProvider

        return OpenAIProvider(
            api_key=config.api_key,
            model=model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            base_url=config.base_url,
        )

    if config.provider == "codex":
        from re_agent.llm.codex_cli import CodexCLIProvider

        return CodexCLIProvider(
            model=model or "gpt-5.4",
            timeout_s=config.timeout_s,
        )

    raise ValueError(
        f"Unknown LLM provider: {config.provider!r}. "
        f"Supported providers: 'claude', 'openai', 'openai-compat', 'codex'."
    )
