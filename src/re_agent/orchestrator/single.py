"""Single function reversal pipeline."""
from __future__ import annotations

import logging
from pathlib import Path

from re_agent.agents.block_splitter import decompiled_line_count
from re_agent.agents.loop import run_fix_loop
from re_agent.backend.protocol import REBackend
from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionTarget, HookEntry, ReversalResult
from re_agent.core.session import Session
from re_agent.llm.protocol import LLMProvider
from re_agent.orchestrator.block import reverse_blocks
from re_agent.parity.engine import fetch_ghidra_data, score_single
from re_agent.parity.source_indexer import SourceIndexer

logger = logging.getLogger(__name__)


def reverse_single(
    target: FunctionTarget,
    config: ReAgentConfig,
    backend: REBackend,
    llm: LLMProvider,
    session: Session | None = None,
    output_dir: Path | None = None,
    indexer: SourceIndexer | None = None,
    block_llm: LLMProvider | None = None,
) -> ReversalResult:
    """Reverse a single function: agent loop -> optional parity check -> record.

    For functions above the block threshold, uses block-level decomposition
    to split the function into smaller self-contained blocks, reverse each
    independently, and stitch them together.

    Args:
        output_dir: If provided, write the generated code to a file in this
            directory.  The file is named ``<address>_<class>_<func>.cpp``.
        indexer: Pre-built source indexer.  When running multiple functions
            in the same class, callers should build the indexer once and pass
            it here to avoid re-scanning the entire source tree each time.
        block_llm: Optional cheaper/faster LLM provider for block-level
            reversals.  When ``None``, uses the main ``llm`` for everything.
    """
    log_dir = Path(config.output.log_dir) if config.output.log_dir else None

    # Try block-level reversal first for large functions
    # Two-tier: fast_mode (all flash, cheap) then hybrid (pro for reasoning) on failure
    if config.orchestrator.block_reversal_enabled:
        try:
            decompile = backend.decompile(target.address)
            line_count = decompiled_line_count(decompile.raw_output)
            if line_count >= config.orchestrator.block_threshold_lines:
                block_kwargs = dict(
                    target=target, backend=backend, llm=llm, block_llm=block_llm,
                    block_threshold_lines=config.orchestrator.block_threshold_lines,
                    max_block_lines=config.orchestrator.block_max_lines,
                    max_fix_rounds=config.orchestrator.max_review_rounds,
                    objective_verifier_enabled=config.orchestrator.objective_verifier_enabled,
                    objective_call_count_tolerance=config.orchestrator.objective_call_count_tolerance,
                    objective_control_flow_tolerance=config.orchestrator.objective_control_flow_tolerance,
                    project_description=config.project_profile.project_description,
                )

                # Strategy by size:
                #   100-500  lines: two-tier (flash first, pro on FAIL) + checker
                #   500-1000 lines: flash block first, fall back to standard pipeline on FAIL
                #   1000+    lines: flash block with skip_checker+skip_var_mapping only
                if line_count > 1000:
                    logger.info("%s: %d lines — very large: flash block only",
                                target.address, line_count)
                    result = reverse_blocks(
                        **block_kwargs,  # type: ignore
                        fast_mode=True,
                        skip_checker=True, skip_var_mapping=True,
                    )
                elif line_count > 500:
                    logger.info("%s: %d lines — large: flash block, fallback to standard",
                                target.address, line_count)
                    result = reverse_blocks(
                        **block_kwargs,  # type: ignore
                        fast_mode=True,
                        skip_checker=True, skip_var_mapping=True,
                    )
                    if not result.success:
                        logger.info("%s: flash block FAIL — falling back to standard",
                                target.address)
                        result = run_fix_loop(
                            target=target, backend=backend, reverser_llm=llm, checker_llm=llm,
                            max_rounds=config.orchestrator.max_review_rounds,
                            log_dir=log_dir, optimize=True,
                            objective_verifier_enabled=config.orchestrator.objective_verifier_enabled,
                            objective_call_count_tolerance=config.orchestrator.objective_call_count_tolerance,
                            objective_control_flow_tolerance=config.orchestrator.objective_control_flow_tolerance,
                        )
                else:
                    # Tier 1: all-flash (fast_mode) — cheap, handles ~80%
                    result = reverse_blocks(**block_kwargs, fast_mode=True)  # type: ignore
                    if not result.success:
                        # Tier 2: hybrid pro/flash — handles remaining ~20%
                        logger.info("%s: fast_mode FAIL — escalating to hybrid", target.address)
                        result = reverse_blocks(**block_kwargs, fast_mode=False)  # type: ignore

                if result.success:
                    logger.info("%s: PASS (rounds=%d)", target.address, result.rounds_used)
                _write_code(result, target, config, output_dir)
                _run_parity(result, target, config, backend, indexer)
                if session:
                    session.record_result(result)
                return result
        except Exception:
            logger.warning(
                "%s: block reversal failed, falling back to standard pipeline",
                target.address, exc_info=True,
            )

    result = run_fix_loop(
        target=target,
        backend=backend,
        reverser_llm=llm,
        checker_llm=llm,
        max_rounds=config.orchestrator.max_review_rounds,
        log_dir=log_dir,
        source_root=Path(config.project_profile.source_root),
        project_profile=config.project_profile,
        indexer=indexer,
        session=session,
        report_dir=Path(config.output.report_dir),
        objective_verifier_enabled=config.orchestrator.objective_verifier_enabled,
        objective_call_count_tolerance=config.orchestrator.objective_call_count_tolerance,
        objective_control_flow_tolerance=config.orchestrator.objective_control_flow_tolerance,
        optimize=config.orchestrator.optimize,
    )

    _write_code(result, target, config, output_dir)
    _run_parity(result, target, config, backend, indexer)
    if session:
        session.record_result(result)

    return result


def _write_code(
    result: ReversalResult,
    target: FunctionTarget,
    config: ReAgentConfig,
    output_dir: Path | None,
) -> None:
    """Write generated code to a file (PASS only)."""
    if not result.code or not result.success:
        return
    code_dir = output_dir or (Path(config.output.report_dir) / "code")
    try:
        code_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{target.address}_{target.class_name}_{target.function_name}.cpp"
        safe_name = safe_name.replace("::", "_").replace("/", "_")
        code_path = code_dir / safe_name
        code_path.write_text(result.code, encoding="utf-8")
        logger.info("Code written to %s", code_path)
    except OSError as exc:
        logger.warning("Failed to write code file: %s", exc)


def _run_parity(
    result: ReversalResult,
    target: FunctionTarget,
    config: ReAgentConfig,
    backend: REBackend,
    indexer: SourceIndexer | None,
) -> None:
    """Run parity check if enabled and code was produced."""
    if not config.parity.enabled or not result.code:
        return
    try:
        if indexer is None:
            source_root = Path(config.project_profile.source_root)
            indexer = SourceIndexer(source_root, config.project_profile)
        source = indexer.find(target.class_name, target.function_name)

        ghidra_data = None
        if backend.capabilities.has_decompile:
            try:
                ghidra_data = fetch_ghidra_data(target.address, backend)
            except Exception:
                logger.debug("Ghidra data fetch failed for %s, running source-only", target.address, exc_info=True)

        status, findings = score_single(
            entry=_target_to_hook(target),
            source=source,
            ghidra=ghidra_data,
            config=config.parity,
        )
        result.parity_status = status
        result.parity_findings = findings
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Parity check failed for %s: %s", target.address, exc)


def _target_to_hook(target: FunctionTarget) -> HookEntry:
    return HookEntry(
        class_path=target.class_name,
        fn_name=target.function_name,
        address=target.address,
        reversed=True,
        locked=False,
        is_virtual=False,
    )
