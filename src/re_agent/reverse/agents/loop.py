"""Fix loop — reverser -> checker -> fix, bounded by max rounds."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from re_agent.config.schema import ProjectProfile
from re_agent.llm.protocol import LLMProvider
from re_agent.reverse.agents.checker import CheckerAgent
from re_agent.reverse.agents.reverser import ReverserAgent
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import (
    CheckerVerdict,
    FunctionTarget,
    ObjectiveVerdict,
    PipelineProfile,
    ReversalResult,
)
from re_agent.reverse.core.session import Session
from re_agent.reverse.parity.source_indexer import SourceIndexer
from re_agent.reverse.verification.objective import compute_structural_summary, verify_candidate

logger = logging.getLogger(__name__)


def run_fix_loop(
    target: FunctionTarget,
    backend: REBackend,
    reverser_llm: LLMProvider,
    checker_llm: LLMProvider | None = None,
    max_rounds: int = 4,
    log_dir: Path | None = None,
    source_root: Path | None = None,
    project_profile: ProjectProfile | None = None,
    indexer: SourceIndexer | None = None,
    session: Session | None = None,
    report_dir: Path | None = None,
    objective_verifier_enabled: bool = True,
    objective_call_count_tolerance: int = 3,
    objective_control_flow_tolerance: int = 2,
    optimize: bool = False,
    enable_phase1: bool = True,
    max_tokens_per_function: int = 0,
    profile: PipelineProfile | None = None,
) -> ReversalResult:
    """Run the reverser->checker->fix loop up to max_rounds.

    Args:
        target: Function to reverse
        backend: RE backend for Ghidra data
        reverser_llm: LLM provider for the reverser agent
        checker_llm: LLM provider for the checker agent (defaults to reverser_llm)
        max_rounds: Maximum fix iterations
        log_dir: Directory to write prompt/response logs

    Returns:
        ReversalResult with the final code and verdict
    """
    if checker_llm is None:
        checker_llm = reverser_llm

    # Profile provides a floor; CLI/explicit max_rounds can raise above it
    effective_max_rounds = max(profile.max_rounds, max_rounds) if profile is not None else max_rounds
    effective_phase1 = profile.enable_phase1 if profile is not None else enable_phase1
    effective_obj_verify = profile.use_objective_verifier if profile is not None else objective_verifier_enabled
    inject_src_ctx = profile.inject_source_context if profile is not None else True
    inject_few_shot_flag = profile.inject_few_shot if profile is not None else True
    few_shot_max = profile.few_shot_max_examples if profile is not None else 2

    reverser = ReverserAgent(
        reverser_llm,
        backend,
        source_root=source_root,
        project_profile=project_profile,
        indexer=indexer,
        session=session,
        report_dir=report_dir,
        optimize=optimize,
        enable_phase1=effective_phase1,
        inject_source_context=inject_src_ctx,
        inject_few_shot=inject_few_shot_flag,
        few_shot_max_examples=few_shot_max,
    )

    project_desc = project_profile.project_description if project_profile else ""
    checker_rules = project_profile.checker_custom_rules if project_profile else ""
    checker = CheckerAgent(checker_llm, backend, project_description=project_desc, checker_custom_rules=checker_rules)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

    code = ""
    last_code = ""
    last_verdict: CheckerVerdict | None = None
    last_objective_verdict: ObjectiveVerdict | None = None
    cached_decompile = None
    # Lazy import to break circular import: loop → orchestrator.stagnation → orchestrator.__init__ → loop
    from re_agent.reverse.orchestrator.stagnation import StagnationTracker

    tracker = StagnationTracker()

    for round_num in range(1, effective_max_rounds + 1):
        timestamp = time.strftime("%Y%m%d-%H%M%S")

        if max_tokens_per_function > 0:
            used = getattr(reverser_llm, "total_prompt_tokens", 0) + getattr(reverser_llm, "total_completion_tokens", 0)
            if used > max_tokens_per_function:
                logger.warning(
                    "%s: token budget %d exceeded (%d used), aborting fix loop",
                    target.address,
                    max_tokens_per_function,
                    used,
                )
                cleanup_loop(reverser, checker)
                return ReversalResult(
                    target=target,
                    code=code,
                    checker_verdict=last_verdict,
                    objective_verdict=last_objective_verdict,
                    parity_status=None,
                    parity_findings=[],
                    rounds_used=round_num - 1,
                    success=False,
                )

        # Reverse (or fix)
        if round_num == 1:
            code, tag = reverser.reverse(target)
        else:
            if last_verdict is None:
                raise RuntimeError("Fix called without a prior checker verdict")
            code, tag = reverser.fix(
                checker_report=last_verdict.summary,
                issues=last_verdict.issues,
                fix_instructions=last_verdict.fix_instructions,
                target=target,
                decompiled=cached_decompile.raw_output if cached_decompile else "",
                prior_code=last_code,
                objective_findings=last_objective_verdict.findings if last_objective_verdict else None,
            )

        # Short-circuit: identical code means no progress; skip checker and stop
        if round_num > 1 and code == last_code:
            logger.info(
                "%s: code unchanged in round %d, stopping fix loop early",
                target.address,
                round_num,
            )
            cleanup_loop(reverser, checker)
            return ReversalResult(
                target=target,
                code=code,
                checker_verdict=last_verdict,
                objective_verdict=last_objective_verdict,
                parity_status=None,
                parity_findings=[],
                rounds_used=round_num,
                success=False,
            )
        last_code = code

        if log_dir:
            log_entry = {
                "round": round_num,
                "timestamp": timestamp,
                "phase": "reverse" if round_num == 1 else "fix",
                "target": f"{target.class_name}::{target.function_name}",
                "address": target.address,
                "prompt": reverser.last_prompt,
                "response": reverser.last_response,
                "code_length": len(code),
                "phase1_analysis": reverser._phase1_analysis,
            }
            log_path = log_dir / f"round{round_num}-{timestamp}-reverser.json"
            log_path.write_text(json.dumps(log_entry, indent=2), encoding="utf-8")

        # Check
        if optimize and round_num == 1:
            cached_decompile = reverser.last_decompile_result
        structural = ""
        if cached_decompile:
            structural = compute_structural_summary(cached_decompile.raw_output, code)
        verdict = checker.check(
            code,
            target,
            decompile_result=cached_decompile if optimize else None,
            structural_summary=structural,
        )
        last_verdict = verdict

        objective_verdict: ObjectiveVerdict | None = None
        if effective_obj_verify:
            objective_verdict = verify_candidate(
                code,
                target,
                backend,
                call_count_tolerance=objective_call_count_tolerance,
                control_flow_tolerance=objective_control_flow_tolerance,
                decompile_result=cached_decompile,
            )
        last_objective_verdict = objective_verdict

        if log_dir:
            check_log = {
                "round": round_num,
                "timestamp": timestamp,
                "phase": "check",
                "prompt": checker.last_prompt,
                "response": checker.last_response,
                "verdict": verdict.verdict.value,
                "summary": verdict.summary,
                "issues": verdict.issues,
                "fix_instructions": verdict.fix_instructions,
                "objective_verdict": objective_verdict.verdict.value if objective_verdict else None,
                "objective_summary": objective_verdict.summary if objective_verdict else "",
                "objective_findings": objective_verdict.findings if objective_verdict else [],
            }
            check_path = log_dir / f"round{round_num}-{timestamp}-checker.json"
            check_path.write_text(json.dumps(check_log, indent=2), encoding="utf-8")

        if StagnationTracker.is_pass(verdict, objective_verdict):
            cleanup_loop(reverser, checker)
            return ReversalResult(
                target=target,
                code=code,
                checker_verdict=verdict,
                objective_verdict=objective_verdict,
                parity_status=None,
                parity_findings=[],
                rounds_used=round_num,
                success=True,
            )
        if tracker.update(verdict):
            logger.info("%s: fix loop stagnated after %d rounds, stopping", target.address, round_num)
            break

    # Exhausted all rounds
    cleanup_loop(reverser, checker)
    return ReversalResult(
        target=target,
        code=code,
        checker_verdict=last_verdict,
        objective_verdict=last_objective_verdict,
        parity_status=None,
        parity_findings=[],
        rounds_used=effective_max_rounds,
        success=False,
    )


def cleanup_loop(reverser: ReverserAgent, checker: CheckerAgent) -> None:
    """Clean up conversations held by the fix loop to prevent memory leaks."""
    if reverser._conversation_id and reverser.llm.supports_conversations:
        reverser.llm.delete_conversation(reverser._conversation_id)
        reverser._conversation_id = None
