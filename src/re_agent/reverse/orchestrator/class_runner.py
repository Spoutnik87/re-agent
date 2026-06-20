"""Class-level auto-advance orchestrator."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from re_agent.config.schema import ReverseConfig
from re_agent.llm.protocol import LLMProvider
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.function_picker import pick_next
from re_agent.reverse.core.models import ReversalResult
from re_agent.reverse.core.session import Session
from re_agent.reverse.orchestrator.single import reverse_single
from re_agent.reverse.parity.source_indexer import SourceIndexer

logger = logging.getLogger(__name__)


def reverse_class(
    class_name: str,
    config: ReverseConfig,
    backend: REBackend,
    llm: LLMProvider,
    session: Session | None = None,
    max_functions: int | None = None,
    block_llm: LLMProvider | None = None,
) -> list[ReversalResult]:
    """Reverse functions in a class one by one until done or limit reached.

    Args:
        class_name: Target class name
        config: Full configuration
        backend: RE backend
        llm: LLM provider (for reasoning-heavy tasks)
        session: Session for progress tracking
        max_functions: Override for max functions (defaults to config value)
        block_llm: Optional cheaper provider for block-level reversals

    Returns:
        List of ReversalResult for each attempted function
    """
    if session is None:
        session = Session(config.output.session_file)

    limit = max_functions if max_functions is not None else config.orchestrator.max_functions_per_class
    results: list[ReversalResult] = []

    # Build the source indexer once for the entire class run.
    indexer: SourceIndexer | None = None
    if config.parity.enabled:
        source_root = Path(config.project_profile.source_root)
        if source_root.exists():
            indexer = SourceIndexer(source_root, config.project_profile)
        else:
            logger.warning("Source root %s not found, skipping index", source_root)

    for fn_idx in range(1, limit + 1):
        target = pick_next(class_name, backend, session)
        if target is None:
            print(f"No more candidates in {class_name}.", file=sys.stderr)
            break

        print(
            f"[{fn_idx}/{limit}] Reversing {target.class_name}::{target.function_name} ({target.address})...",
            file=sys.stderr,
        )

        result = reverse_single(target, config, backend, llm, session, indexer=indexer, block_llm=block_llm)
        results.append(result)

        status = "PASS" if result.success else "FAIL"
        print(
            f"  -> {status} (rounds: {result.rounds_used})",
            file=sys.stderr,
        )

    return results
