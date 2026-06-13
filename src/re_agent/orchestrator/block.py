"""Block-level reversal orchestrator with two-tier model escalation.

First attempt (fast_mode): all flash — cheap, handles ~80% of functions.
If FAIL: re-run with pro for reasoning (checker, var mapping, large blocks).
"""

from __future__ import annotations

import logging

from re_agent.agents.block_reverser import BlockReverserAgent, clear_block_cache, generate_variable_mapping
from re_agent.agents.block_splitter import (
    SplitResult,
    decompiled_line_count,
    extract_variable_context,
    split_decompiled_function,
)
from re_agent.agents.checker import CheckerAgent
from re_agent.backend.protocol import REBackend
from re_agent.core.models import (
    CheckerVerdict,
    FunctionTarget,
    ObjectiveVerdict,
    ReversalResult,
    Verdict,
)
from re_agent.llm.protocol import LLMProvider
from re_agent.orchestrator.recursive import RecursiveDecomposer, should_use_recursive
from re_agent.orchestrator.stagnation import StagnationTracker
from re_agent.utils.text import strip_ghidra_noise
from re_agent.verification.objective import verify_candidate

logger = logging.getLogger(__name__)


def _empty_result(target: FunctionTarget) -> ReversalResult:
    return ReversalResult(
        target=target,
        code="",
        checker_verdict=None,
        objective_verdict=None,
        parity_status=None,
        parity_findings=[],
        rounds_used=0,
        success=False,
    )


def reverse_blocks(
    target: FunctionTarget,
    backend: REBackend,
    llm: LLMProvider,
    block_llm: LLMProvider | None = None,
    block_threshold_lines: int = 100,
    max_block_lines: int = 40,
    max_fix_rounds: int = 3,
    objective_verifier_enabled: bool = True,
    objective_call_count_tolerance: int = 3,
    objective_control_flow_tolerance: int = 2,
    fast_mode: bool = False,
    skip_checker: bool = False,
    skip_var_mapping: bool = False,
    project_description: str = "",
) -> ReversalResult:
    """Reverse a function using block-level decomposition.

    Args:
        llm: Provider for reasoning (checker, var mapping).
        block_llm: Cheaper provider for block reversals.
        fast_mode: Use flash for everything (tier 1 attempt).
        skip_checker: Skip LLM checker + fix loop. Trust block splitter structure.
            For functions >500 lines where checker exceeds token limits.
        skip_var_mapping: Skip LLM variable mapping. Use decompile names as-is.
            For functions >1000 lines where even var mapping call is too large.
    """
    decompile_result = backend.decompile(target.address)
    decompiled = strip_ghidra_noise(decompile_result.raw_output)
    line_count = decompiled_line_count(decompiled)

    # Clear block cache between functions to avoid cross-function contamination
    clear_block_cache()

    if line_count < block_threshold_lines:
        return _empty_result(target)

    # Recursive path — only for >500 line functions with checker enabled
    # (skip_checker/skip_var_mapping must both be False so this is rare)
    if should_use_recursive(decompiled) and not skip_checker and not skip_var_mapping:
        decomposer = RecursiveDecomposer(llm, block_llm=block_llm, project_description=project_description)
        full_code = decomposer.decompose_and_reverse(
            decompiled=decompiled,
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
        )
        if not full_code:
            return _empty_result(target)

        checker = CheckerAgent(llm, backend, project_description=project_description)

        ov: ObjectiveVerdict | None = None
        if objective_verifier_enabled:
            ov = verify_candidate(
                full_code,
                target,
                backend,
                call_count_tolerance=objective_call_count_tolerance,
                control_flow_tolerance=objective_control_flow_tolerance,
                decompile_result=decompile_result,
            )

        cv = None
        if ov is None or ov.verdict == Verdict.FAIL:
            cv = checker.check(full_code, target)
        else:
            cv = CheckerVerdict(
                verdict=Verdict.PASS,
                summary="Objective verifier passed — structure matches",
                issues=[],
                fix_instructions=[],
            )

        fr = 0
        rec_tracker = StagnationTracker()
        while not StagnationTracker.is_pass(cv, ov) and fr < max_fix_rounds:
            fr += 1
            decomposer._block_agent.reset_conversation()
            full_code = decomposer.decompose_and_reverse(
                decompiled=decompiled,
                class_name=target.class_name,
                function_name=target.function_name,
                address=target.address,
            )
            if not full_code:
                break
            ov = None
            if objective_verifier_enabled:
                ov = verify_candidate(
                    full_code,
                    target,
                    backend,
                    call_count_tolerance=objective_call_count_tolerance,
                    control_flow_tolerance=objective_control_flow_tolerance,
                    decompile_result=decompile_result,
                )
            if ov is None or ov.verdict == Verdict.FAIL:
                cv = checker.check(full_code, target)
            else:
                cv = CheckerVerdict(
                    verdict=Verdict.PASS, summary="Objective verifier passed", issues=[], fix_instructions=[]
                )
            if rec_tracker.update(cv):
                logger.info("%s: recursive fix loop stagnated after %d rounds, stopping", target.address, fr)
                break

        ok = cv.verdict == Verdict.PASS and (ov is None or ov.verdict != Verdict.FAIL)
        return ReversalResult(
            target=target,
            code=full_code,
            checker_verdict=cv,
            objective_verdict=ov,
            parity_status=None,
            parity_findings=[],
            rounds_used=1 + fr,
            success=ok,
        )

    split = split_decompiled_function(decompiled, max_block_lines=max_block_lines)
    if split.num_blocks <= 1:
        return _empty_result(target)

    # Model selection based on fast_mode
    # fast_mode: flash for everything. hybrid: pro for reasoning, flash for small blocks.
    _reasoning = block_llm if (fast_mode and block_llm is not None) else llm
    if fast_mode and block_llm is None:
        logger.warning("fast_mode requested but no block_llm configured; using main LLM for all reasoning")
    _block_fast = block_llm if block_llm is not None else llm
    _block_pro = llm

    # Variable mapping — always use pro model for semantic reasoning.
    # Flash model cannot reliably infer variable names/types from decompile.
    var_mapping = ""
    if not skip_var_mapping:
        var_mapping = generate_variable_mapping(
            decompiled=decompiled,
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
            llm=llm,
            project_description=project_description,
        )

    block_agent_fast = BlockReverserAgent(_block_fast, project_description=project_description)
    _pro_desc = project_description
    if _block_fast is not _block_pro:
        block_agent_pro = BlockReverserAgent(_block_pro, project_description=_pro_desc)
    else:
        block_agent_pro = None
    var_context = extract_variable_context(decompiled, split.signature)

    def _reverse_all_blocks(
        split: SplitResult,
        agent_fast: BlockReverserAgent,
        agent_pro: BlockReverserAgent | None,
        fast: bool,
        var_mapping: str,
        var_context: str,
        decompiled: str,
        reset_conv: bool = False,
        only_block_ids: set[str] | None = None,
        previous_code: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        reversed_blocks: dict[str, str] = {}
        all_code: list[str] = []
        for block in split.blocks:
            if (
                only_block_ids is not None
                and block.id not in only_block_ids
                and previous_code
                and block.id in previous_code
            ):
                bc = previous_code[block.id]
                reversed_blocks[block.id] = bc
                all_code.append(bc)
                continue
            bl = len(block.decompiled_text.splitlines())
            if (not fast) and (bl > max_block_lines) and agent_pro is not None:
                agent: BlockReverserAgent = agent_pro
                ctx = decompiled
            else:
                agent = agent_fast
                ctx = var_context
            if reset_conv:
                agent.reset_conversation()
            bid, bc = (
                block.id,
                agent.reverse_block(
                    block=block,
                    class_name=target.class_name,
                    function_name=target.function_name,
                    address=target.address,
                    full_decompiled=ctx,
                    var_mapping=var_mapping,
                    reversed_blocks=reversed_blocks,
                ),
            )
            reversed_blocks[bid] = bc
            all_code.append(bc)
        return all_code, reversed_blocks

    all_code, reversed_blocks = _reverse_all_blocks(
        split,
        block_agent_fast,
        block_agent_pro,
        fast_mode,
        var_mapping,
        var_context,
        decompiled,
        reset_conv=True,
    )
    full_code = _stitch(split, all_code)
    if not full_code:
        return _empty_result(target)

    # Checker — skip for very large functions (>500 lines, token limit)
    if skip_checker:
        # Objective verifier is required when checker is skipped
        ov = verify_candidate(
            full_code,
            target,
            backend,
            call_count_tolerance=objective_call_count_tolerance,
            control_flow_tolerance=objective_control_flow_tolerance,
            decompile_result=decompile_result,
        )
        ok = ov.verdict != Verdict.FAIL
        return ReversalResult(
            target=target,
            code=full_code,
            checker_verdict=CheckerVerdict(
                verdict=Verdict.PASS if ok else Verdict.FAIL,
                summary="Skipped LLM checker (function too large)" if ok else "Objective verifier failed",
                issues=[] if ok else ov.findings,
                fix_instructions=[],
            ),
            objective_verdict=ov,
            parity_status=None,
            parity_findings=[],
            rounds_used=1,
            success=ok,
        )

    checker = CheckerAgent(_reasoning, backend, project_description=project_description)

    # Objective verifier first (free, no LLM tokens)
    ov: ObjectiveVerdict | None = None  # type: ignore[no-redef]
    if objective_verifier_enabled:
        ov = verify_candidate(
            full_code,
            target,
            backend,
            call_count_tolerance=objective_call_count_tolerance,
            control_flow_tolerance=objective_control_flow_tolerance,
            decompile_result=decompile_result,
        )

    # Only run LLM checker if objective verifier fails or is insufficient
    cv = None
    if ov is None or ov.verdict == Verdict.FAIL:
        cv = checker.check(full_code, target)
    else:
        cv = CheckerVerdict(
            verdict=Verdict.PASS,
            summary="Objective verifier passed — structure matches",
            issues=[],
            fix_instructions=[],
        )

    # Fix loop
    fr = 0
    blk_tracker = StagnationTracker()
    while not StagnationTracker.is_pass(cv, ov) and fr < max_fix_rounds:
        fr += 1
        # Recompute var_mapping if checker flagged naming/type issues
        if cv and _has_naming_issues(cv.issues) and not skip_var_mapping:
            logger.info("%s: checker flagged naming issues — regenerating var_mapping", target.address)
            var_mapping = generate_variable_mapping(
                decompiled=decompiled,
                class_name=target.class_name,
                function_name=target.function_name,
                address=target.address,
                llm=_reasoning,
                project_description=project_description,
            )
        affected = _find_affected_blocks(cv.issues, split) if cv else None
        all_code, reversed_blocks = _reverse_all_blocks(
            split,
            block_agent_fast,
            block_agent_pro,
            fast_mode,
            var_mapping,
            var_context,
            decompiled,
            reset_conv=True,
            only_block_ids=affected,
            previous_code=reversed_blocks,
        )
        full_code = _stitch(split, all_code)
        if not full_code:
            break
        # Objective-first: only run LLM checker if objective fails
        ov = None
        if objective_verifier_enabled:
            ov = verify_candidate(
                full_code,
                target,
                backend,
                call_count_tolerance=objective_call_count_tolerance,
                control_flow_tolerance=objective_control_flow_tolerance,
                decompile_result=decompile_result,
            )
        if ov is None or ov.verdict == Verdict.FAIL:
            cv = checker.check(full_code, target)
        else:
            cv = CheckerVerdict(
                verdict=Verdict.PASS, summary="Objective verifier passed", issues=[], fix_instructions=[]
            )
        if blk_tracker.update(cv):
            logger.info("%s: fix loop stagnated after %d rounds, stopping", target.address, fr)
            break

    ok = cv.verdict == Verdict.PASS and (ov is None or ov.verdict != Verdict.FAIL)
    return ReversalResult(
        target=target,
        code=full_code,
        checker_verdict=cv,
        objective_verdict=ov,
        parity_status=None,
        parity_findings=[],
        rounds_used=1 + fr,
        success=ok,
    )


def _has_naming_issues(issues: list[str]) -> bool:
    """Check if any checker issue is related to variable naming or types."""
    naming_keywords = (
        "variable",
        "name",
        "type",
        "rename",
        "identifier",
        "incorrect type",
        "wrong type",
        "naming",
        "undefined",
        "mapping",
    )
    combined = " ".join(issues).lower()
    return any(kw in combined for kw in naming_keywords)


def _find_affected_blocks(issues: list[str], split: SplitResult) -> set[str] | None:
    """Identify blocks referenced in checker issues.

    Returns a set of block IDs to re-reverse, or ``None`` if no blocks
    could be identified (triggering a full re-reversal).
    """
    if not issues:
        return None
    all_text = " ".join(issues).lower()
    affected: set[str] = set()
    for block in split.blocks:
        if block.id in all_text or block.label.lower() in all_text:
            affected.add(block.id)
    return affected if affected else None


def _stitch(split: SplitResult, reversed_parts: list[str]) -> str:
    if not reversed_parts:
        return ""
    cleaned = [p.strip() for p in reversed_parts if p.strip()]
    if not cleaned:
        return ""
    if len(cleaned) != split.num_blocks:
        logger.warning(
            "_stitch: block count mismatch — split has %d blocks but got %d reversed parts",
            split.num_blocks,
            len(cleaned),
        )
    body = "\n".join(cleaned)
    opens = body.count("{")
    closes = body.count("}")
    if opens != closes:
        logger.warning(
            "_stitch: unbalanced braces — %d opens vs %d closes",
            opens,
            closes,
        )
    if split.signature:
        return f"{split.signature} {{\n{body}\n}}"
    return f"{{\n{body}\n}}"
