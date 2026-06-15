"""Single function reversal pipeline."""

from __future__ import annotations

import logging
from pathlib import Path

from re_agent.config.schema import ReverseConfig
from re_agent.llm.protocol import LLMProvider
from re_agent.reverse.agents.block_splitter import decompiled_line_count
from re_agent.reverse.agents.few_shot_builder import pre_classify
from re_agent.reverse.agents.loop import run_fix_loop
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import FunctionTarget, HookEntry, PipelineProfile, ReversalResult, profile_for
from re_agent.reverse.core.session import Session
from re_agent.reverse.orchestrator.block import reverse_blocks
from re_agent.reverse.parity.engine import fetch_ghidra_data, score_single
from re_agent.reverse.parity.source_indexer import SourceIndexer

logger = logging.getLogger(__name__)


def _compute_max_fix_rounds(line_count: int, max_rounds: int) -> int:
    """Scale fix rounds based on function size to avoid excessive LLM calls.

    Large functions require more tokens per round; reducing rounds
    prevents timeout on the fix loop.
    """
    if line_count > 400:
        return min(max_rounds, 1)
    if line_count > 200:
        return min(max_rounds, 2)
    return max_rounds


def reverse_single(
    target: FunctionTarget,
    config: ReverseConfig,
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

    pipeline_profile: PipelineProfile | None = None

    # Try block-level reversal first for large functions
    # Two-tier: fast_mode (all flash, cheap) then hybrid (pro for reasoning) on failure
    if config.orchestrator.block_reversal_enabled:
        block_result: ReversalResult | None = None
        try:
            decompile = backend.decompile(target.address)
            line_count = decompiled_line_count(decompile.raw_output)
            classification = pre_classify(decompile.raw_output)
            pipeline_profile = profile_for(classification)
            logger.info(
                "%s: %d lines, classified as %s",
                target.address,
                line_count,
                classification,
            )
            # Skip expensive block decomposition for trivial functions
            if pipeline_profile.max_rounds == 1 and line_count < 100:
                logger.info("%s: trivial %s — skipping block reversal", target.address, classification)
                block_result = None
            elif line_count >= config.orchestrator.block_threshold_lines:
                effective_max_rounds = _compute_max_fix_rounds(line_count, config.orchestrator.max_review_rounds)
                logger.info(
                    "%s: %d lines — effective max fix rounds: %d (from %d)",
                    target.address,
                    line_count,
                    effective_max_rounds,
                    config.orchestrator.max_review_rounds,
                )
                block_kwargs = dict(
                    target=target,
                    backend=backend,
                    llm=llm,
                    block_llm=block_llm,
                    block_threshold_lines=config.orchestrator.block_threshold_lines,
                    max_block_lines=config.orchestrator.block_max_lines,
                    max_fix_rounds=effective_max_rounds,
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
                    logger.info("%s: %d lines — very large: flash block only", target.address, line_count)
                    block_result = reverse_blocks(
                        **block_kwargs,  # type: ignore[arg-type]
                        fast_mode=True,
                        skip_checker=True,
                        skip_var_mapping=True,
                    )
                elif line_count > 500:
                    logger.info("%s: %d lines — large: flash block, fallback to standard", target.address, line_count)
                    block_result = reverse_blocks(
                        **block_kwargs,  # type: ignore[arg-type]
                        fast_mode=True,
                        skip_checker=True,
                        skip_var_mapping=True,
                    )
                    if not block_result.success:
                        logger.info("%s: flash block FAIL — falling back to standard", target.address)
                        # Note: profile.max_rounds takes precedence over size-based scaling here.
                        block_result = run_fix_loop(
                            target=target,
                            backend=backend,
                            reverser_llm=llm,
                            checker_llm=llm,
                            max_rounds=config.orchestrator.max_review_rounds,
                            log_dir=log_dir,
                            optimize=config.orchestrator.optimize,
                            enable_phase1=config.orchestrator.enable_phase1,
                            objective_verifier_enabled=config.orchestrator.objective_verifier_enabled,
                            objective_call_count_tolerance=config.orchestrator.objective_call_count_tolerance,
                            objective_control_flow_tolerance=config.orchestrator.objective_control_flow_tolerance,
                            profile=pipeline_profile,
                        )
                else:
                    # Tier 1: all-flash (fast_mode) — cheap, handles ~80%
                    block_result = reverse_blocks(**block_kwargs, fast_mode=True)  # type: ignore[arg-type]
                    if not block_result.success:
                        # Tier 2: hybrid pro/flash — handles remaining ~20%
                        logger.info("%s: fast_mode FAIL — escalating to hybrid", target.address)
                        block_result = reverse_blocks(**block_kwargs, fast_mode=False)  # type: ignore[arg-type]

                if block_result is not None and block_result.success:
                    logger.info("%s: PASS (rounds=%d)", target.address, block_result.rounds_used)
        except Exception:
            logger.warning(
                "%s: block reversal crashed, falling back to standard pipeline",
                target.address,
                exc_info=True,
            )
            if "line_count" in locals() and line_count >= config.orchestrator.block_threshold_lines:
                logger.info(
                    "%s: blocking fallback — function too large (%d lines) for standard pipeline",
                    target.address,
                    line_count,
                )
                block_result = ReversalResult(
                    target=target,
                    code="",
                    checker_verdict=None,
                    objective_verdict=None,
                    parity_status=None,
                    parity_findings=[],
                    rounds_used=0,
                    success=False,
                )
            else:
                block_result = None

        if block_result is not None:
            _write_code(block_result, target, config, output_dir)
            _run_parity(block_result, target, config, backend, indexer)
            if session:
                session.record_result(block_result)
            return block_result

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
        enable_phase1=config.orchestrator.enable_phase1,
        profile=pipeline_profile,
    )

    _write_code(result, target, config, output_dir)
    _run_parity(result, target, config, backend, indexer)
    if session:
        session.record_result(result)

    return result


def _write_code(
    result: ReversalResult,
    target: FunctionTarget,
    config: ReverseConfig,
    output_dir: Path | None,
) -> None:
    """Write generated code to a file (PASS only)."""
    if not result.code:
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
    config: ReverseConfig,
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
    except Exception as exc:
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
