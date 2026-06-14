"""Conservative structural verification that does not rely on an LLM."""

from __future__ import annotations

import re

from re_agent.backend.protocol import REBackend
from re_agent.core.models import DecompileResult, FunctionTarget, ObjectiveVerdict, Verdict
from re_agent.utils.text import count_calls, count_control_flow, strip_comments

CALL_SEQ_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_:]*)\s*\(")
CPP_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "decltype",
        "static_cast",
        "reinterpret_cast",
        "const_cast",
        "dynamic_cast",
        "catch",
        "new",
        "delete",
    }
)


def _extract_call_order(body_no_comments: str) -> list[str]:
    """Extract ordered list of function call names from source."""
    calls: list[str] = []
    for m in CALL_SEQ_RE.finditer(body_no_comments):
        name = m.group(1)
        if name in CPP_KEYWORDS:
            continue
        calls.append(name)
    return calls


def compute_structural_summary(decompiled_text: str, reversed_code: str) -> str:
    """Compute a non-LLM structural comparison summary.

    Returns a concise text suitable for injection into the checker prompt
    to help the LLM focus on semantic issues rather than recounting calls.
    """
    decomp_body = strip_comments(_extract_body(decompiled_text))
    rev_body = strip_comments(_extract_body(reversed_code))

    decomp_calls = _extract_call_order(decomp_body)
    rev_calls = _extract_call_order(rev_body)

    decomp_cf = count_control_flow(decomp_body)
    rev_cf = count_control_flow(rev_body)

    parts: list[str] = [
        f"call_count: decompile={len(decomp_calls)} reversed={len(rev_calls)}",
    ]
    if decomp_calls != rev_calls:
        missing = [c for c in decomp_calls if c not in rev_calls]
        extra = [c for c in rev_calls if c not in decomp_calls]
        if missing:
            parts.append(f"missing_calls: {', '.join(missing[:8])}")
        if extra:
            parts.append(f"extra_calls: {', '.join(extra[:8])}")
        if not missing and not extra:
            call_match = sum(1 for a, b in zip(decomp_calls, rev_calls, strict=False) if a == b)
            parts.append(f"call_order_match: {call_match}/{len(decomp_calls)}")
    else:
        parts.append("call_order: IDENTICAL")
    parts.append(f"control_flow: decompile={decomp_cf} reversed={rev_cf}")

    return " | ".join(parts)


def verify_candidate(
    code: str,
    target: FunctionTarget,
    backend: REBackend,
    call_count_tolerance: int = 3,
    control_flow_tolerance: int = 2,
    decompile_result: DecompileResult | None = None,
) -> ObjectiveVerdict:
    """Return FAIL only on strong structural mismatches, else PASS/UNKNOWN.

    Args:
        decompile_result: Pre-fetched decompile from a prior backend call.
            When provided, avoids a redundant Ghidra decompile invocation.
    """
    if not code.strip():
        return ObjectiveVerdict(
            verdict=Verdict.FAIL,
            summary="No candidate code produced",
            findings=["Candidate code is empty"],
        )

    source_body = strip_comments(_extract_body(code))
    source_call_count, _, _ = count_calls(source_body)
    source_flow_count = count_control_flow(source_body)

    findings: list[str] = []
    checks_run = 0

    if decompile_result is None:
        try:
            decompile = backend.decompile(target.address)
        except Exception as exc:
            return ObjectiveVerdict(
                verdict=Verdict.UNKNOWN,
                summary="Objective verifier could not read decompile output",
                findings=[str(exc)],
            )
    else:
        decompile = decompile_result

    decompile_body = strip_comments(_extract_body(decompile.raw_output))
    decompile_flow_count = count_control_flow(decompile_body)
    if decompile.callees is not None:
        checks_run += 1
        call_diff = abs(decompile.callees - source_call_count)
        if call_diff >= call_count_tolerance and source_call_count < decompile.callees:
            findings.append(
                f"Call count mismatch: decompile reports {decompile.callees} callees, "
                f"candidate has {source_call_count} calls"
            )

    if decompile_flow_count >= 2:
        checks_run += 1
        flow_diff = decompile_flow_count - source_flow_count
        if flow_diff >= control_flow_tolerance and source_flow_count < decompile_flow_count:
            findings.append(
                f"Control-flow mismatch: decompile has {decompile_flow_count} branches/loops, "
                f"candidate has {source_flow_count}"
            )

    if backend.capabilities.has_asm:
        try:
            asm = backend.get_asm(target.address)
        except Exception:
            asm = None
        if asm is not None:
            checks_run += 1
            call_diff = abs(asm.call_count - source_call_count)
            if call_diff >= call_count_tolerance and source_call_count < asm.call_count:
                findings.append(
                    f"ASM call mismatch: disassembly has {asm.call_count} calls, candidate has {source_call_count}"
                )

    if findings:
        return ObjectiveVerdict(
            verdict=Verdict.FAIL,
            summary="Objective verifier found structural mismatches",
            findings=findings,
        )
    if checks_run == 0:
        return ObjectiveVerdict(
            verdict=Verdict.UNKNOWN,
            summary="Objective verifier had insufficient structural data",
            findings=[],
        )
    return ObjectiveVerdict(
        verdict=Verdict.PASS,
        summary="No structural mismatches found",
        findings=[],
    )


def _extract_body(text: str) -> str:
    open_brace = text.find("{")
    close_brace = text.rfind("}")
    if open_brace == -1 or close_brace == -1 or close_brace <= open_brace:
        return text
    return text[open_brace : close_brace + 1]
