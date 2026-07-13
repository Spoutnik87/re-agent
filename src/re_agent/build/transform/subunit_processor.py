from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Template

from re_agent.build.transform.diagnostics import (
    FunctionVerdict,
    SubunitDiagnostics,
    classify_compile_error,
    default_router_decision,
    truncate_compile_error,
    write_diagnostics,
    write_raw_response,
)
from re_agent.build.validate.compiler import compile_check
from re_agent.build.work_packet_types import ModelUsage
from re_agent.common.compiler import compile_generated_file_set
from re_agent.llm.protocol import LLMProvider, Message, ProviderUsage, get_usage

log = logging.getLogger(__name__)

# Maximum total recovery LLM calls (flat global limit, not per-round).
# With 7 missing: batch of 4, batch of 3, then stop (2 calls total).
_TARGET_RECOVERY_MAX_CALLS = 2
# Maximum missing functions per recovery batch
_TARGET_RECOVERY_BATCH_SIZE = 4

# Compile error categories that may benefit from an LLM retry.
# Categories not in this set (include_error, decls_header_warning,
# too_many_arguments, unknown, empty stderr) are never retried.
_RETRYABLE_COMPILE_CATEGORIES = frozenset(
    {
        "syntax_error",
        "undeclared_identifier",
        "type_mismatch",
        "goto_error",
    }
)


@dataclass(frozen=True)
class SendResult:
    """Structured result from a budgeted LLM send.

    ``status`` is one of:
      - ``"ok"`` — the call succeeded (response is the text).
      - ``"budget_exceeded"`` — budget was exhausted before the call.
      - ``"provider_error"`` — the provider raised an exception.

    ``response`` is the LLM response text when status is ``"ok"``,
    ``None`` otherwise.

    ``usage_delta`` is a ``ProviderUsage`` representing the delta
    (prompt, completion, calls) consumed by this send.  When the call
    was rejected before sending (budget_exceeded), all fields are 0.
    """

    status: str  # "ok" | "budget_exceeded" | "provider_error"
    response: str | None
    usage_delta: ProviderUsage


@dataclass
class TransformBudget:
    """Global per-invocation transform budget shared across ALL subunits.

    Tracks remaining calls, tokens, and compile retries.  Each ``llm.send()``
    — initial generation, TARGET recovery, compile retry — deducts from this
    budget.  Once any counter reaches zero (or a token cap is hit between
    calls), further sends are rejected with ``BUDGET_EXCEEDED``.

    ``tokens_remaining`` is a stop-*between*-calls cap: the delta from
    ``get_usage()`` before/after each call is subtracted.  ``None`` values
    from the provider are treated as 0.

    ``compile_retry_calls_remaining`` is a per-function cap; after a subunit-
    level compile retry, per-function retries are disabled entirely for the
    remainder of the run.
    """

    calls_remaining: int = 8
    tokens_remaining: int = 150000
    compile_retry_calls_remaining: int = 3
    exceeded: bool = False
    exceeded_reason: str = ""
    _subunit_retry_occurred: bool = False
    provider_error_count: int = 0

    def check_before_call(self, kind: str) -> bool:
        """Return False if budget is exhausted (no call should be made)."""
        if self.exceeded:
            return False
        if self.calls_remaining <= 0:
            self.exceeded = True
            self.exceeded_reason = f"Call budget exhausted (kind={kind})"
            return False
        return True

    def record_after_call(
        self,
        kind: str,
        before: Any,
        after: Any,
        subunit_retry: bool = False,
        provider_error: bool = False,
    ) -> None:
        """Record usage after an LLM call.  Deducts calls and tokens."""
        self.calls_remaining -= 1
        if provider_error:
            self.provider_error_count += 1
        delta_prompt = max((after.prompt_tokens or 0) - (before.prompt_tokens or 0), 0)
        delta_completion = max((after.completion_tokens or 0) - (before.completion_tokens or 0), 0)
        delta_tokens = delta_prompt + delta_completion
        self.tokens_remaining -= delta_tokens
        if subunit_retry:
            self._subunit_retry_occurred = True
        if self.tokens_remaining <= 0:
            self.exceeded = True
            self.exceeded_reason = (
                f"Token budget exhausted (delta={delta_tokens}, remaining={self.tokens_remaining}, kind={kind})"
            )

    @property
    def subunit_retry_occurred(self) -> bool:
        return self._subunit_retry_occurred

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls_remaining": self.calls_remaining,
            "tokens_remaining": self.tokens_remaining,
            "compile_retry_calls_remaining": self.compile_retry_calls_remaining,
            "exceeded": self.exceeded,
            "exceeded_reason": self.exceeded_reason,
            "subunit_retry_occurred": self._subunit_retry_occurred,
            "provider_error_count": self.provider_error_count,
        }


_FILE_MARKER_RE = re.compile(r"^// FILE: (.+)$", re.MULTILINE)

# Explicit target identity marker: // TARGET: <ordinal> <address>
# Captures exactly <ordinal> <0xADDRESS> with strict single-line format.
_TARGET_LINE_RE = re.compile(r"^//\s*TARGET:\s*(\d+)\s+(0x[0-9a-fA-F]+)\s*$")

# Multiline version for bulk stripping (kept for transition compat).
_TARGET_MARKER_RE = re.compile(r"^//\s*TARGET:\s*(\d+)\s+(0x[0-9a-fA-F]+)\s*$", re.MULTILINE)

# Broad detection: any line that starts with // TARGET: (including malformed/incomplete).
# Used to distinguish "no TARGET at all" (legacy allowed) from "TARGET present but invalid"
# (must reject with rejected_identity, no legacy fallback).
_TARGET_LIKE_RAW_RE = re.compile(r"//\s*TARGET\s*:", re.MULTILINE)

# Standalone Markdown code fence delimiter:
# optional whitespace + at least three backticks + optional non-whitespace
# info string/tag + optional whitespace, and nothing else on the line.
_FENCE_LINE_RE = re.compile(r"^\s*`{3,}[^\s`]*\s*$", re.MULTILINE)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Original function identity anchor comment pattern:
# // Original function: 0x<address>
_ORIGINAL_FUNCTION_RE = re.compile(r"//\s*Original function:\s*(0x[0-9a-fA-F]+)")


@dataclass(frozen=True, slots=True)
class FileRecord:
    """Parsed file block from an LLM response with optional TARGET identity.

    ``target`` is ``(ordinal, address)`` when a valid ``// TARGET:`` line
    was found *directly adjacent* (same line preceding, no blank line
    between) to the ``// FILE:`` marker. ``None`` otherwise.
    """

    path: str
    content: str
    target: tuple[int, str] | None


@dataclass(frozen=True, slots=True)
class TargetCoverage:
    """Analysis of TARGET identity coverage from a subunit LLM response.

    ``covered_ordinals`` — set of ordinals that have at least one valid
    ``// TARGET:`` with matching address and in-range ordinal.
    ``covered_records`` — list of FileRecords whose target is valid.
    ``missing_ordinals`` — ordinals for which no valid TARGET was found.
    ``is_complete`` — True when all functions have valid TARGETs.
    ``has_conflict`` — True when a hard error prevents recovery
      (out-of-range ordinal, wrong address, duplicate path, etc.).
    ``conflict_reason`` — human-readable explanation when ``has_conflict``.
    """

    covered_ordinals: frozenset[int]
    covered_records: tuple[FileRecord, ...]
    missing_ordinals: frozenset[int]
    is_complete: bool
    has_conflict: bool = False
    conflict_reason: str = ""


def _analyze_target_coverage(
    records: list[FileRecord],
    functions_to_transform: list[dict[str, Any]],
) -> TargetCoverage:
    """Analyze TARGET coverage from FileRecords, distinguishing partial from conflict.

    Returns a ``TargetCoverage`` that classifies the response as:
    - Complete (all functions have valid TARGETs)
    - Partial (some functions covered, some missing, no conflicts)
    - Conflict (hard errors: OOB ordinal, wrong address, path collision)

    A record with ``target=None`` is treated as "no TARGET" — it is
    silently ignored for coverage purposes.  Only when TARGET markers
    are present but contain hard errors is the response a conflict.
    """
    n_funcs = len(functions_to_transform)
    func_addrs = {i: f["address"].lower() for i, f in enumerate(functions_to_transform)}
    all_ordinals = set(range(n_funcs))

    covered_ordinals: set[int] = set()
    covered_records: list[FileRecord] = []
    seen_paths: set[str] = set()

    for record in records:
        target = record.target
        if target is None:
            continue
        ordinal, addr = target
        addr = addr.lower()

        # Check 1: Ordinal in range
        if ordinal < 0 or ordinal >= n_funcs:
            return TargetCoverage(
                covered_ordinals=frozenset(),
                covered_records=(),
                missing_ordinals=frozenset(all_ordinals),
                is_complete=False,
                has_conflict=True,
                conflict_reason=f"ordinal {ordinal} out of range [0, {n_funcs - 1}]",
            )

        # Check 2: Address matches expected
        if addr != func_addrs[ordinal]:
            return TargetCoverage(
                covered_ordinals=frozenset(),
                covered_records=(),
                missing_ordinals=frozenset(all_ordinals),
                is_complete=False,
                has_conflict=True,
                conflict_reason=f"address {addr} at ordinal {ordinal} does not match expected {func_addrs[ordinal]}",
            )

        # Check 3: Path collision
        if record.path in seen_paths:
            return TargetCoverage(
                covered_ordinals=frozenset(),
                covered_records=(),
                missing_ordinals=frozenset(all_ordinals),
                is_complete=False,
                has_conflict=True,
                conflict_reason=f"duplicate path '{record.path}' claimed by multiple TARGETs",
            )

        covered_ordinals.add(ordinal)
        seen_paths.add(record.path)
        covered_records.append(record)

    missing_ordinals = all_ordinals - covered_ordinals
    is_complete = not missing_ordinals

    return TargetCoverage(
        covered_ordinals=frozenset(covered_ordinals),
        covered_records=tuple(covered_records),
        missing_ordinals=frozenset(missing_ordinals),
        is_complete=is_complete,
        has_conflict=False,
    )


def _budgeted_send(
    llm: LLMProvider,
    messages: list[Message],
    budget: TransformBudget,
    kind: str,
    record_kwargs: dict[str, Any] | None = None,
) -> SendResult:
    """Send an LLM call through the global budget.

    Returns a ``SendResult`` with:
      - ``status="ok"`` — call succeeded, ``response`` is the text.
      - ``status="budget_exceeded"`` — budget exhausted before sending.
      - ``status="provider_error"`` — provider raised an exception.

    *kind* is a diagnostic label (``"initial"``, ``"target_recovery"``,
    ``"compile_subunit_retry"``, ``"compile_func_retry"``).
    """
    if not budget.check_before_call(kind):
        log.warning("Budget exceeded before %s call: %s", kind, budget.exceeded_reason)
        return SendResult(
            status="budget_exceeded",
            response=None,
            usage_delta=ProviderUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cache_hit_tokens=None,
                cache_miss_tokens=None,
                calls=0,
            ),
        )
    before = get_usage(llm)
    try:
        response = llm.send(messages)
    except Exception:
        log.warning("LLM call failed for kind=%s", kind)
        after = get_usage(llm)
        budget.record_after_call(kind, before, after, **(record_kwargs or {}), provider_error=True)
        return SendResult(
            status="provider_error",
            response=None,
            usage_delta=ProviderUsage(
                prompt_tokens=(after.prompt_tokens or 0) - (before.prompt_tokens or 0),
                completion_tokens=(after.completion_tokens or 0) - (before.completion_tokens or 0),
                cache_hit_tokens=None,
                cache_miss_tokens=None,
                calls=1,
            ),
        )
    after = get_usage(llm)
    budget.record_after_call(kind, before, after, **(record_kwargs or {}))
    usage_delta = ProviderUsage(
        prompt_tokens=(after.prompt_tokens or 0) - (before.prompt_tokens or 0),
        completion_tokens=(after.completion_tokens or 0) - (before.completion_tokens or 0),
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        calls=1,
    )
    # If this call exhausted the budget (token cap breached), propagate
    # budget_exceeded immediately so the caller does NOT process the
    # response — the verdict becomes BUDGET_EXCEEDED for the entire subunit.
    if budget.exceeded:
        return SendResult(
            status="budget_exceeded",
            response=None,
            usage_delta=usage_delta,
        )
    return SendResult(
        status="ok",
        response=response,
        usage_delta=usage_delta,
    )


def _compile_retry_allowed(
    category: str | None,
    stderr: str,
    prev_stderr_hash: str | None,
    budget: TransformBudget,
    subunit_retry_occurred: bool,
) -> tuple[bool, str]:
    """GCC retry policy: decide whether a compile retry is worth attempting.

    ``prev_stderr_hash`` is an optional SHA-256 hex digest of the *previous*
    compiler stderr (from a prior retry attempt).  When provided and matching,
    stagnation is detected — the same build error recurred despite a retry —
    and the retry is blocked.

    Returns ``(allowed, reason)``.
    """
    if budget.compile_retry_calls_remaining <= 0:
        return False, "compile retry budget exhausted"
    if subunit_retry_occurred:
        return False, "subunit retry already occurred — no per-function retry"
    if category is None or category not in _RETRYABLE_COMPILE_CATEGORIES:
        return False, f"non-retryable category: {category}"
    if not stderr.strip():
        return False, "empty stderr (no actionable error)"
    # Stagnation: identical stderr SHA-256 as last attempt → retry won't help
    if prev_stderr_hash is not None:
        current_hash = hashlib.sha256(stderr.encode()).hexdigest()
        if current_hash == prev_stderr_hash:
            return False, "stagnant stderr (identical to previous attempt)"
    return True, ""


def _no_persist_json_output(
    results: list[dict[str, Any]],
    budget: TransformBudget | None,
    start_usage: Any,
    end_usage: Any,
    exit_code: int,
) -> None:
    """Write structured diagnostics JSON to stdout for ``--no-persist`` runs.

    Only called when ``persist=False``.  The JSON includes summary, budget,
    calls, verdicts, GCC categories, and retry skip reasons — NOT prompts,
    raw code, unbounded stderr, or secrets.

    ``start_usage`` and ``end_usage`` should be ``ProviderUsage`` snapshots
    from ``get_usage()`` before and after all LLM calls.  The delta is
    reported as usage.
    """
    import json

    total = len(results)
    passed = sum(1 for r in results if r.get("compiles"))
    incomplete = sum(1 for r in results if r.get("verdict") == "INCOMPLETE_TARGETS")
    hard_rejects = sum(
        1
        for r in results
        if r.get("verdict") == "NO_OUTPUT" and r.get("diagnostic", {}).get("match_strategy") == "rejected_identity"
    )
    budget_exceeds = sum(1 for r in results if r.get("verdict") == "BUDGET_EXCEEDED")
    provider_errors = sum(1 for r in results if r.get("verdict") == "PROVIDER_ERROR")
    contract_failed = incomplete > 0 or hard_rejects > 0 or budget_exceeds > 0 or provider_errors > 0
    failed = total - passed - incomplete - hard_rejects - budget_exceeds - provider_errors

    report: dict[str, Any] = {
        "run_type": "no-persist",
        "exit_code": exit_code,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "incomplete": incomplete,
            "hard_rejects": hard_rejects,
            "budget_exceeded": budget_exceeds,
            "provider_errors": provider_errors,
            "contract_failed": contract_failed,
        },
        "usage": {
            "prompt_tokens": (end_usage.prompt_tokens or 0) - (start_usage.prompt_tokens or 0),
            "completion_tokens": (end_usage.completion_tokens or 0) - (start_usage.completion_tokens or 0),
            "total_calls": (end_usage.calls or 0) - (start_usage.calls or 0),
        },
        "results": [],
    }
    if budget is not None:
        report["budget"] = budget.to_dict()

    for r in results:
        diag = r.get("diagnostic", {})
        entry: dict[str, Any] = {
            "function": r["function"],
            "verdict": r["verdict"],
            "compiles": r["compiles"],
            "files_matched": len(r.get("files", [])),
            "match_strategy": diag.get("match_strategy"),
            "identity_state": diag.get("identity_state"),
            "identity_reason": diag.get("identity_reason", ""),
            "compile_error_category": diag.get("compile_error_category"),
            "files": [{"path": f["path"]} for f in r.get("files", [])],
        }
        # Retry skip reason (from diagnostic or from _compile_retry_allowed)
        skip_reason = diag.get("retry_skip_reason") or diag.get("identity_reason", "")
        if skip_reason:
            entry["retry_skip_reason"] = skip_reason
        report["results"].append(entry)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


def _render_recovery_prompt(batch: list[tuple[int, dict[str, Any]]]) -> str:
    """Build a compact recovery prompt for a batch of missing functions.

    Each element of *batch* is ``(ordinal, func_dict)`` where
    ``func_dict`` has ``address`` and ``code`` keys.
    """
    lines: list[str] = [
        "Complete the following function(s) that were missing from a previous transform response.",
        "Output exactly one // FILE: block per function below, preceded by",
        "a // TARGET: <ordinal> <address> line directly before // FILE:.",
        'Include #include "_decls.h" in .cpp files.',
        "",
        "Do NOT output files for any other function.",
        "",
    ]
    for ordinal, func in batch:
        addr = func["address"]
        code = func.get("code", "")
        lines.append(f"Ordinal {ordinal} {addr}:")
        lines.append(f"```cpp\n{code}\n```")
        lines.append("")

    return "\n".join(lines)


def _validate_target_groups(
    records: list[FileRecord],
    allowed_ordinals: set[int] | None,
    functions_to_transform: list[dict[str, Any]],
    existing_paths: set[str] | None = None,
    require_cpp: bool = True,
) -> tuple[bool, str]:
    """Consolidated validator for TARGET groups (initial and recovery).

    Validates:
    - Every record has a non-None target
    - Ordinal is in *allowed_ordinals* (when not None)
    - Address matches expected function
    - No path collisions within the batch
    - No path collisions with *existing_paths* (when provided)
    - Each ordinal has at least one ``.cpp`` (when *require_cpp*)

    Returns ``(is_valid, reason)``.
    """
    func_addrs = {i: f["address"].lower() for i, f in enumerate(functions_to_transform)}
    seen_ordinals: set[int] = set()
    seen_paths: set[str] = set(existing_paths) if existing_paths else set()

    if not records:
        return False, "Empty record set"

    for record in records:
        target = record.target
        if target is None:
            return False, f"Record '{record.path}' lacks TARGET"
        ordinal, addr = target
        addr = addr.lower()

        if allowed_ordinals is not None and ordinal not in allowed_ordinals:
            return False, f"Ordinal {ordinal} not in allowed set {sorted(allowed_ordinals)}"

        expected = func_addrs.get(ordinal)
        if expected is None:
            return False, f"Ordinal {ordinal} out of range [0, {len(functions_to_transform) - 1}]"
        if addr != expected:
            return False, f"Address {addr} at ordinal {ordinal} doesn't match expected {expected}"

        if record.path in seen_paths:
            return False, f"Duplicate path '{record.path}' across records"

        seen_ordinals.add(ordinal)
        seen_paths.add(record.path)

    if allowed_ordinals is not None:
        missing = allowed_ordinals - seen_ordinals
        if missing:
            return False, f"Missing ordinals: {sorted(missing)}"

    if require_cpp:
        ordinal_to_paths: dict[int, list[str]] = {}
        for record in records:
            assert record.target is not None
            o = record.target[0]
            ordinal_to_paths.setdefault(o, []).append(record.path)
        for o in allowed_ordinals or seen_ordinals:
            paths = ordinal_to_paths.get(o, [])
            if not any(p.endswith(".cpp") for p in paths):
                return False, f"Ordinal {o} has no .cpp file"

    return True, ""


def _run_target_recovery(
    coverage: TargetCoverage,
    functions_to_transform: list[dict[str, Any]],
    llm: LLMProvider,
    system_prompt: str,
    budget: TransformBudget,
    max_calls: int = _TARGET_RECOVERY_MAX_CALLS,
) -> TargetCoverage:
    """Launch recovery for functions missing TARGET coverage.

    **Flat global limit**: at most *max_calls* LLM calls total (not per round).
    Each call handles a batch of at most ``_TARGET_RECOVERY_BATCH_SIZE`` missing
    ordinals.  Recovery records are only merged when they pass
    ``_validate_target_groups`` against the current set of *covered* paths.

    Uses the shared ``TransformBudget`` (``_budgeted_send``) for call and token
    capping — budget is owned by ``process_modules`` and shared across ALL
    subunits.

    Returns a new ``TargetCoverage`` — never ``has_conflict``, only success
    or partial coverage.
    """
    if coverage.is_complete or coverage.has_conflict:
        return coverage

    covered_records = list(coverage.covered_records)
    covered_ordinals = set(coverage.covered_ordinals)
    covered_paths = {r.path for r in covered_records}
    all_ordinals = set(range(len(functions_to_transform)))

    calls_remaining = max_calls

    while calls_remaining > 0:
        remaining = sorted(all_ordinals - covered_ordinals)
        if not remaining:
            break

        # Take next batch
        batch_ordinals = remaining[:_TARGET_RECOVERY_BATCH_SIZE]
        batch_funcs = [(i, functions_to_transform[i]) for i in batch_ordinals]

        prompt = _render_recovery_prompt(batch_funcs)
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=prompt),
        ]

        calls_remaining -= 1
        send_result = _budgeted_send(llm, messages, budget, "target_recovery")
        if send_result.status == "budget_exceeded":
            # Return conflict-flagged coverage so the caller (process_subunit)
            # can detect the budget exceeded and produce BUDGET_EXCEEDED verdicts.
            return TargetCoverage(
                covered_ordinals=frozenset(covered_ordinals),
                covered_records=tuple(covered_records),
                missing_ordinals=frozenset(all_ordinals - covered_ordinals),
                is_complete=False,
                has_conflict=True,
                conflict_reason="budget_exceeded",
            )
        if send_result.status == "provider_error":
            log.warning("Provider error during target recovery — stopping")
            # Return a conflict-flagged coverage so the caller (process_subunit)
            # can detect the error via budget.provider_error_count delta and
            # produce PROVIDER_ERROR verdicts.
            return TargetCoverage(
                covered_ordinals=frozenset(covered_ordinals),
                covered_records=tuple(covered_records),
                missing_ordinals=frozenset(all_ordinals - covered_ordinals),
                is_complete=False,
                has_conflict=True,
                conflict_reason="provider_error",
            )
        if send_result.status != "ok" or send_result.response is None:
            continue
        recovery_response = send_result.response
        recovery_records, has_invalid = _parse_llm_response_records(recovery_response)
        if has_invalid or not recovery_records:
            log.warning("Target recovery batch %s produced invalid records", batch_ordinals)
            continue
        is_valid, reason = _validate_target_groups(
            recovery_records,
            allowed_ordinals=set(batch_ordinals),
            functions_to_transform=functions_to_transform,
            existing_paths=covered_paths,
            require_cpp=True,
        )
        if not is_valid:
            log.warning("Target recovery batch %s rejected: %s", batch_ordinals, reason)
            continue
        for rr in recovery_records:
            assert rr.target is not None
            ordinal = rr.target[0]
            covered_ordinals.add(ordinal)
            covered_records.append(rr)
            covered_paths.add(rr.path)
    missing_ordinals = all_ordinals - covered_ordinals
    return TargetCoverage(
        covered_ordinals=frozenset(covered_ordinals),
        covered_records=tuple(covered_records),
        missing_ordinals=frozenset(missing_ordinals),
        is_complete=not missing_ordinals,
        has_conflict=False,
    )


def _strip_markdown_fence_delimiters(content: str) -> str:
    """Remove standalone Markdown code fence delimiter lines from content.

    Any line that consists solely of a Markdown code fence delimiter
    (````, ```cpp, ```c++, etc. with optional leading/trailing whitespace)
    is removed --- regardless of position. Lines where backticks appear
    inside code, comments, or string literals are preserved because they
    do not match the standalone delimiter pattern (``_FENCE_LINE_RE``).

    This is more aggressive than a periphery-only strip and is required
    because the ``re.split``-by-``// FILE:`` marker produces per-file
    content blocks where a closing ``` from an adjacent outer-fenced
    block may appear in the *middle* of a file's content (followed by a
    blank line and the next block's opening fence), not just at the end.
    """
    lines = content.splitlines()
    return "\n".join(line for line in lines if not _FENCE_LINE_RE.match(line)).strip()


def _extract_adjacent_target(preceding: str) -> tuple[int, str] | None:
    """Extract ``// TARGET:`` from the line immediately before ``// FILE:``.

    The TARGET line must be the immediate predecessor of the ``// FILE:``
    line (no blank line between — blank lines break adjacency).
    Returns ``(ordinal, address)`` or ``None``.
    Rejects malformed TARGET (wrong format, non-hex address, missing parts).

    **Multiple adjacent TARGET lines are rejected** (no "last wins").
    If more than one line matching ``_TARGET_LINE_RE`` appears in
    *preceding*, returns ``None`` — the block has a contradictory or
    ambiguous identity and must be treated as invalid.
    """
    lines = preceding.splitlines()
    if not lines:
        return None
    # The last line of preceding text is the line immediately before
    # the ``// FILE:`` marker. If it is blank/empty, the TARGET is
    # NOT adjacent (even if a TARGET appears earlier in preceding).
    last_line = lines[-1].strip()
    if not last_line:
        return None
    # Count matching TARGET lines in the entire preceding block.
    # Multiple TARGET lines before a single FILE marker are
    # contradictory — reject (no "last wins").
    target_line_count = sum(1 for line in lines if _TARGET_LINE_RE.match(line.strip()))
    if target_line_count > 1:
        return None
    m = _TARGET_LINE_RE.match(last_line)
    if m:
        ordinal = int(m.group(1))
        address = m.group(2).lower()
        # Validate address is a plausible hex address (already validated by RE)
        return (ordinal, address)
    return None


def _parse_llm_response_records(
    response: str,
) -> tuple[list[FileRecord], bool]:
    """Parse LLM response into ``FileRecord`` entries with validated TARGET.

    Single-pass parse replacing the old ``_extract_targets_from_raw`` +
    ``_parse_llm_response`` two-step.  For each ``// FILE:`` block:

      1. Extract file path.
      2. Extract ``// TARGET:`` from the text **immediately preceding**
         the ``// FILE:`` line (must be adjacent — no blank line between).
      3. Strip Markdown fence delimiter lines from content.
      4. Strip leaked ``// TARGET:`` lines from the end of content
         (these belong to the next file block and leak because the split
         boundary is at ``// FILE:`` only — the preceding block's content
         includes the next block's TARGET).
      5. Reject empty FILE path and empty (after fence-strip) content.
         When an empty FILE block is found, ``has_invalid_file_block`` is set
         to ``True`` — this forces ``rejected_identity`` at the association
         layer (no silent skip).

    TARGET lines that appear *inside* content (not adjacent to a FILE
    marker) are preserved verbatim — they are legitimate source code.

    Returns ``(records, has_invalid_file_block)`` where:
    - ``records`` is ``[FileRecord]`` for valid blocks (in file-block order).
    - ``has_invalid_file_block`` is ``True`` when any ``// FILE:`` block
      had an empty path or empty (after fence-strip) content.
    """
    parts = _FILE_MARKER_RE.split(response)
    records: list[FileRecord] = []
    has_invalid_file_block = False
    for i in range(1, len(parts) - 1, 2):
        filepath = parts[i].strip()
        if not filepath:
            has_invalid_file_block = True
            continue
        preceding = parts[i - 1]
        content = _strip_markdown_fence_delimiters(parts[i + 1]).strip()
        if not content:
            has_invalid_file_block = True
            continue
        # Strip leaked ``// TARGET:`` lines from the end of content (P1 leak fix).
        # The split boundary is at ``// FILE:`` only, so the TARGET line for the
        # next block leaks into the current block's content.  Since a legitimate
        # TARGET inside source code would not be at the very end of the block,
        # stripping trailing TARGET lines is safe and prevents contamination.
        content_lines = content.splitlines()
        while content_lines and _TARGET_LINE_RE.match(content_lines[-1].strip()):
            content_lines.pop()
        content = "\n".join(content_lines).strip()
        if not content:
            has_invalid_file_block = True
            continue
        target = _extract_adjacent_target(preceding)
        records.append(FileRecord(path=filepath, content=content, target=target))
    return records, has_invalid_file_block


def _parse_llm_response(response: str) -> list[dict[str, str]]:
    """Parse all ``// FILE:`` blocks from the LLM response.

    Returns a list of ``{'path': str, 'content': str}`` dicts, one per file.
    Backward-compat wrapper over ``_parse_llm_response_records``.
    TARGET markers are NOT stripped from content (they are always extracted
    from the preceding text); any ``// TARGET:`` appearing inside content
    is legitimate source code and must be preserved.

    Note: the ``has_invalid_file_block`` flag from the underlying parser is
    intentionally discarded here — callers that need protocol-error detection
    should use ``_parse_llm_response_records`` directly.
    """
    records, _ = _parse_llm_response_records(response)
    return [{"path": r.path, "content": r.content} for r in records]


def _extract_targets_from_raw(raw_response: str) -> dict[int, tuple[int, str]]:
    """Extract explicit ``// TARGET:`` markers aligned to ``// FILE:`` blocks.

    Delegates to ``_parse_llm_response_records`` and returns
    ``{file_index: (ordinal, address)}`` for records that have a target.

    This is kept for backward compat; new code should use records directly.
    The ``has_invalid_file_block`` flag is intentionally discarded — callers
    needing protocol-error detection should use ``_parse_llm_response_records``.
    """
    records, _ = _parse_llm_response_records(raw_response)
    return {i: r.target for i, r in enumerate(records) if r.target is not None}


def _merge_retry_files(
    parsed_files: list[dict[str, str]],
    retry_files: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Merge retry output into parsed files, replacing by exact path.

    Each retry file replaces an existing parsed file with the same path.
    Retry files with paths not present in ``parsed_files`` are added as
    new files. Files in ``parsed_files`` whose path does not appear in
    ``retry_files`` are preserved unchanged.

    This prevents the subunit retry from orphaning initially successful
    functions: when the retry only returns files for the failed function,
    the other function's files stay intact.

    The merge is **by path, not by position** — paths are the stable
    identifier across the LLM round-trip. This is the same address/path
    matching contract used by ``_match_files_to_function_with_strategy``.
    """
    merged = {f["path"]: f for f in parsed_files}  # use dict to enforce unique path
    for rf in retry_files:
        merged[rf["path"]] = rf
    return list(merged.values())


def _merge_retry_records(
    initial_records: list[FileRecord],
    retry_records: list[FileRecord],
    *,
    require_target: bool = False,
) -> list[FileRecord]:
    """Merge retry FileRecords into initial list, with optional pre-merge validation.

    **Validation before merge (P0 retry contract)** when ``require_target=True``:
    1. Every retry record MUST have a valid ``target`` (not None).
    2. For known paths (present in both initial and retry), the retry
       target must match the stored identity exactly.
    3. New paths (only in retry) require a valid target.
    4. If ANY retry record fails validation, ALL retry records are
       **rejected** — the initial list is returned unchanged.

    When ``require_target=False`` (legacy retry without explicit TARGET),
    retry records are merged without target validation — identity is
    implicit (path/address-based) and validation is deferred to the
    caller's re-association logic.

    On success:
    - Known paths keep their **initial** target, content is replaced by
      retry content.
    - New paths are added with their retry target.
    - Paths only in initial are preserved unchanged.

    This implements the strict retry contract: invalid retry responses
    (missing or contradictory TARGET) never degrade the initial mapping.
    """
    # ── Pre-merge validation (only when TARGET is required) ──
    if require_target:
        for rr in retry_records:
            if rr.target is None:
                return initial_records
            if rr.path in {r.path for r in initial_records}:
                for init_r in initial_records:
                    if init_r.path == rr.path:
                        if init_r.target is not None and rr.target != init_r.target:
                            return initial_records
                        break

    # ── All valid: merge ──
    by_path: dict[str, FileRecord] = {r.path: r for r in initial_records}
    for rr in retry_records:
        if rr.path in by_path:
            # Known path: keep initial target, replace content
            existing = by_path[rr.path]
            by_path[rr.path] = FileRecord(
                path=rr.path,
                content=rr.content,
                target=existing.target,  # preserve initial target
            )
        else:
            # New path: use retry record as-is (target already validated)
            by_path[rr.path] = rr
    return list(by_path.values())


def _parse_explicit_targets(
    parsed_files: list[dict[str, str]],
    target_map: dict[int, tuple[int, str]] | None = None,
) -> dict[int, tuple[int, str]]:
    """Resolve explicit target identity mapping.

    When *target_map* is provided (pre-extracted from raw LLM response),
    returns it directly. Otherwise scans parsed file content for
    ``// TARGET:`` markers.

    Returns ``{file_index: (ordinal, address)}`` for every file that has
    a matching target identity.
    """
    if target_map is not None:
        return target_map
    result: dict[int, tuple[int, str]] = {}
    for idx, f in enumerate(parsed_files):
        m = _TARGET_MARKER_RE.search(f["content"])
        if m:
            ordinal = int(m.group(1))
            address = m.group(2).lower()
            result[idx] = (ordinal, address)
    return result


def _validate_explicit_targets(
    target_map: dict[int, tuple[int, str]],
    functions_to_transform: list[dict[str, Any]],
    parsed_file_count: int = 0,
) -> tuple[bool, str]:
    """Validate explicit TARGET markers form a complete bijection.

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    Validation checks (in order):
    1. All files have TARGET markers (no mixed output).
    2. Every target ordinal is in [0, N-1] where N = number of functions.
    3. Every target address matches the function at that ordinal.
    4. Every function_to_transform has at least one file.
    """
    n_funcs = len(functions_to_transform)
    if not target_map:
        return False, "No explicit target markers found"

    # Check 1: All files must have TARGET markers
    if parsed_file_count > 0 and len(target_map) < parsed_file_count:
        return False, (f"Some files lack TARGET markers: {len(target_map)} of {parsed_file_count} files have markers")

    func_addrs = {i: f["address"].lower() for i, f in enumerate(functions_to_transform)}
    funcs_with_files: set[int] = set()
    error_reasons: list[str] = []

    for file_idx, (ordinal, addr) in target_map.items():
        # Check 2: Ordinal in range
        if ordinal < 0 or ordinal >= n_funcs:
            error_reasons.append(f"ordinal {ordinal} out of range [0, {n_funcs - 1}] (file index {file_idx})")
            continue
        # Check 3: Address matches
        expected_addr = func_addrs[ordinal]
        if addr != expected_addr:
            error_reasons.append(
                f"address {addr} at ordinal {ordinal} does not match expected {expected_addr} (file index {file_idx})"
            )
            continue
        # Check 3b: Path collision (same path claimed by two TARGETs)
        # The path must be retrieved from parsed_files[file_idx].
        # Since we only have target_map (file_idx → mapping), we need access
        # to the parsed files.  This is checked in _analyze_target_coverage
        # at a higher level; here we trust the caller's pre-validation.
        funcs_with_files.add(ordinal)

    if error_reasons:
        return False, "; ".join(error_reasons)

    # Check 4: Every function has at least one file
    if len(funcs_with_files) != n_funcs:
        missing = sorted(set(range(n_funcs)) - funcs_with_files)
        return False, (f"Not all functions have files via TARGET markers; missing function indices: {missing}")

    return True, ""


def _associate_files_to_functions(
    parsed_files: list[dict[str, str]],
    functions_to_transform: list[dict[str, Any]],
    target_map: dict[int, tuple[int, str]] | None = None,
    has_target_markers: bool = False,
    has_invalid_file_block: bool = False,
    strict_partial_recovery: bool = False,
) -> tuple[
    list[list[dict[str, str]]],
    list[str],
    list[tuple[str, str, int]],
]:
    """Global, immutable association of parsed files to functions.

    Strategy (tried in order):
    0. If ``has_invalid_file_block`` is True (empty FILE path or empty
       content detected during parse), reject all functions immediately
       with ``rejected_identity`` — the LLM output is malformed.
    1. Explicit identity: use pre-extracted *target_map* (from raw LLM
       response) or parse ``// TARGET:`` markers from file content.
       If ALL files have valid markers forming a complete bijection, use
       ``explicit_identity`` for every function.
    2. If some files have TARGET markers but validation fails, reject all
       functions with ``rejected_identity``.
    3. If ``has_target_markers`` is True but ``target_map`` is empty
       (TARGET-like content was seen but none was valid), reject all
       with ``rejected_identity`` — TARGET was present but invalid.
    4. No TARGET markers: fall back to per-function by-name/by-address
       matching (``_match_files_to_function_with_strategy``) without the
       positional ``single_file_fallback``.

    Returns ``(per_func_files, per_func_strategies, identity_info)`` where:
    - ``per_func_files[i]`` = list of file dicts for function i (or []).
    - ``per_func_strategies[i]`` = strategy for function i.
    - ``identity_info[i]`` = ``(state, reason, target_count)``.
    """
    n_funcs = len(functions_to_transform)

    # Step 0: Invalid file block forces rejection (protocol error).
    # An empty FILE path or empty content after fence-strip is a malformed
    # LLM response that must not be silently ignored or fall back to legacy
    # matching.  Checked BEFORE the ``not parsed_files`` early return so that
    # a response consisting entirely of an invalid block (producing zero valid
    # parsed files) still gets ``rejected_identity`` (not ``"none"``).
    if has_invalid_file_block:
        reason = "Invalid file block detected (empty path or content) in LLM response"
        empty_invalid: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
        strategies = ["rejected_identity"] * n_funcs
        info = [("rejected", reason, 0)] * n_funcs
        return empty_invalid, strategies, info

    if not parsed_files or n_funcs == 0:
        empty: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
        strategies = ["none"] * n_funcs
        reason = "No parsed files to associate" if not parsed_files else "No functions to transform"
        info = [("none", reason, 0)] * n_funcs
        return empty, strategies, info

    # Step 1: Resolve explicit TARGET markers (from pre-extracted map or content)
    explicit_map = _parse_explicit_targets(parsed_files, target_map)

    if explicit_map:
        # Step 1.5: Check for path collisions (same path claimed by multiple TARGETs).
        # This must happen before _validate_explicit_targets because a path collision
        # is a hard error (rejected_identity) that cannot be recovered.
        seen_paths: set[str] = set()
        has_path_collision = False
        for file_idx in explicit_map:
            path = parsed_files[file_idx]["path"]
            if path in seen_paths:
                has_path_collision = True
                break
            seen_paths.add(path)
        if has_path_collision:
            reason = "Path collision detected: multiple TARGET markers claim the same file path"
            empty_collision: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
            strategies = ["rejected_identity"] * n_funcs
            info = [("rejected", reason, 0)] * n_funcs
            return empty_collision, strategies, info

        # Step 2: Validate
        is_valid, reason = _validate_explicit_targets(
            explicit_map,
            functions_to_transform,
            parsed_file_count=len(parsed_files),
        )
        if is_valid:
            per_func_files: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
            for file_idx, (ordinal, _addr) in explicit_map.items():
                per_func_files[ordinal].append(parsed_files[file_idx])
            strategies = ["explicit_identity"] * n_funcs
            target_counts = [len(per_func_files[i]) for i in range(n_funcs)]
            info = [("explicit", "", target_counts[i]) for i in range(n_funcs)]
            return per_func_files, strategies, info
        else:
            # Partial vs hard rejection: if the reason is "Not all functions have
            # files via TARGET markers; missing function indices: [...]", and there
            # are NO hard validation errors (all existing targets are in-range and
            # address-correct), then this is a *partial valid* response.  Return
            # valid groups for covered functions only when ``strict_partial_recovery``
            # is True (strict TARGET mode).  Otherwise (default legacy mode), partial
            # coverage is still a full rejection — the LLM must output files for
            # ALL functions when TARGET markers are used.
            # Any other reason (OOB ordinal, wrong address, mixed output) is a
            # hard error → reject all regardless of mode.
            if strict_partial_recovery and reason.startswith("Not all functions have files via TARGET markers"):
                per_func_files_partial: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
                for file_idx, (ordinal, _addr) in explicit_map.items():
                    per_func_files_partial[ordinal].append(parsed_files[file_idx])
                strategies_partial: list[str] = []
                info_partial: list[tuple[str, str, int]] = []
                for ordinal_idx in range(n_funcs):
                    if per_func_files_partial[ordinal_idx]:
                        strategies_partial.append("explicit_identity")
                        info_partial.append(("explicit", "", len(per_func_files_partial[ordinal_idx])))
                    else:
                        strategies_partial.append("none")
                        info_partial.append(("none", f"No TARGET in initial response (ordinal {ordinal_idx})", 0))
                return per_func_files_partial, strategies_partial, info_partial
            # Hard rejection: explicit identity present but with validation errors
            empty_list: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
            strategies = ["rejected_identity"] * n_funcs
            info = [("rejected", reason, 0)] * n_funcs
            return empty_list, strategies, info

    # Step 3: TARGET markers present in raw response but none valid → reject all.
    if has_target_markers:
        reason = (
            "TARGET-like markers present in response but none were valid "
            "(malformed, multiple, contradictory, or empty FILE)"
        )
        empty_targets: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
        strategies = ["rejected_identity"] * n_funcs
        info = [("rejected", reason, 0)] * n_funcs
        return empty_targets, strategies, info

    # Step 4: No TARGET markers → fall back to direct matching (legacy).
    # This is a contract violation — the new protocol requires // TARGET: markers.
    # Enforce bijective association: a file can only be claimed by one
    # function.  If the same file is matched to multiple functions, reject
    # all of them with a "rejected_identity" strategy (contract violation).
    log.warning(
        "Legacy fallback contract violation: no // TARGET: markers in %d-file response for %d functions",
        len(parsed_files),
        n_funcs,
    )
    per_func_files = []
    strategies = []
    info = []
    bijective_map: dict[str, int] = {}  # file path → first function index that matched it
    has_conflict = False
    for i, func in enumerate(functions_to_transform):
        files, strategy = _match_files_to_function_with_strategy(
            parsed_files,
            func,
            n_funcs,
            _allow_single_file_fallback=False,
        )
        for f in files:
            path = f["path"]
            if path in bijective_map and bijective_map[path] != i:
                has_conflict = True
                break
            bijective_map[path] = i
        if has_conflict:
            break
        per_func_files.append(files)
        strategies.append(strategy)
        if files:
            state = "matched" if strategy in ("by_name", "by_address", "single_function") else "none"
            reason = f"Matched {len(files)} file(s) via legacy {strategy} strategy (no // TARGET: markers)"
            info.append((state, reason, len(files)))
        else:
            reason = "No files matched via legacy address/name identity anchors (no // TARGET: markers)"
            info.append(("none", reason, 0))

    if has_conflict:
        # File claimed by multiple functions → reject all (contract violation)
        reject_reason = "File claimed by multiple functions (legacy fallback contract violation)"
        log.warning("Legacy fallback contract violation: %s", reject_reason)
        rejected_list: list[list[dict[str, str]]] = [[] for _ in range(n_funcs)]
        strategies = ["rejected_identity"] * n_funcs
        info = [("rejected", reject_reason, 0)] * n_funcs
        return rejected_list, strategies, info

    return per_func_files, strategies, info


def _strategy_to_identity_state(strategy: str) -> str:
    """Map a match strategy to an identity state label for FunctionVerdict."""
    mapping = {
        "explicit_identity": "explicit",
        "by_name": "matched",
        "by_address": "matched",
        "single_function": "matched",
        "rejected_identity": "rejected",
        "none": "none",
    }
    return mapping.get(strategy, "none")


def _match_files_to_function(
    parsed_files: list[dict[str, str]],
    func: dict[str, Any],
    total_func_count: int,
) -> list[dict[str, str]]:
    """Match parsed LLM output files to a specific function (backward-compat wrapper).

    Returns only the matched files. Use ``_match_files_to_function_with_strategy``
    to also get the strategy name used for diagnostics.
    """
    files, _strategy = _match_files_to_function_with_strategy(parsed_files, func, total_func_count)
    return files


def _address_in_path_or_original_comment(path: str, content: str, address: str) -> bool:
    """Check if *address* appears as an identity anchor in *path* or *content*.

    An address is an identity only if it appears:
      - In the file path (as a path component), OR
      - In a ``// Original function: 0x...`` comment in the content.

    A bare mention of the address anywhere in the content (e.g., as a callee
    reference, in a comment describing another function, or in a string) is
    NOT a valid identity anchor.  This prevents legacy legacy-matching from
    assigning files based on callee references.
    """
    addr_lower = address.lower()
    if addr_lower in path.lower():
        return True
    # Check only // Original function: 0x<addr> comments
    return any(m.group(1).lower() == addr_lower for m in _ORIGINAL_FUNCTION_RE.finditer(content))


def _match_files_to_function_with_strategy(
    parsed_files: list[dict[str, str]],
    func: dict[str, Any],
    total_func_count: int,
    _allow_single_file_fallback: bool = False,
) -> tuple[list[dict[str, str]], str]:
    """Match parsed LLM output files to a specific function and report the strategy.

    Returns ``(matched_files, strategy_name)`` where ``strategy_name`` is one of:
    ``single_function``, ``by_name``, ``by_address``, ``none``.

    Strategy:
    1. If there's only one function in the subunit, all files belong to it.
    2. Otherwise, try to match by function ``name`` in the file content or path
       (preserves historical behaviour when ``name`` is provided).
    3. If ``name`` is absent or does not match anything, fall back to matching
       by ``address``. This is required because ``context_builder.build_context``
       only emits ``{"address": addr, "code": code}`` (no ``name`` field), and
       the transform prompt exposes the address to the LLM via
       ``### Function {{ func.address }}`` so the address is the stable
       identifier across the LLM round-trip (the system prompt instructs the
       LLM to *rename* functions, so the original name would not match anyway).

    **Strict identity**: An address is an identity only in the file path or a
    ``// Original function: 0x...`` comment, never a bare callee reference.
    This is enforced by ``_address_in_path_or_original_comment``.

    ``_allow_single_file_fallback`` is deprecated and retained only for
    backward-compatible test access; it defaults to ``False``.

    No positional fallback is performed — output without a matchable
    address/name identity always returns ``([], "none")``.  All
    explicit-identity matching is handled by
    ``_associate_files_to_functions`` which MUST be called first.
    """
    if total_func_count == 1 and parsed_files:
        return parsed_files, "single_function"

    # 2. Match by name (preserved for backwards compatibility).
    func_name = func.get("name", "")
    if func_name:
        matched = [
            f
            for f in parsed_files
            if func_name.lower() in f["content"].lower() or func_name.lower() in f["path"].lower()
        ]
        if matched:
            return matched, "by_name"

    # 3. Match by address via strict identity anchor.
    #    Address must be in the file path or a // Original function: comment.
    addr = func.get("address", "")
    if addr:
        matched = [f for f in parsed_files if _address_in_path_or_original_comment(f["path"], f["content"], addr)]
        if matched:
            return matched, "by_address"

    # 4. Deprecated positional fallback: only active when explicitly requested
    #    (legacy test compatibility). No production code uses this path.
    if _allow_single_file_fallback and len(parsed_files) == 1:
        return parsed_files, "single_file_fallback"

    return [], "none"


def _render_system_prompt(cfg: Any, module_name: str) -> str:
    template_path = _PROMPT_DIR / "transform_system.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    naming = cfg.project.conventions.naming
    _prompt: str = template.render(
        language=getattr(cfg.output, "language", "C++"),
        standard=getattr(cfg.output, "standard", "C++17"),
        project_description=cfg.project.description,
        naming_classes=naming.classes,
        naming_functions=naming.functions,
        naming_globals=naming.globals,
        includes_rule=getattr(cfg.project.conventions, "includes_rule", ""),
        max_function_lines=getattr(cfg.project.conventions, "max_function_lines", 200),
        module_name=module_name,
    )
    return _prompt


def _render_repair_prompt(cfg: Any, module_name: str) -> str:
    """System prompt for compile-error repair (distinct from beautify).

    Repair and beautify are different objectives: the first pass beautifies,
    retries repair. Mixing both in one prompt is what made the beautify prompt
    underperform on non-compiling input.
    """
    template_path = _PROMPT_DIR / "repair_system.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    decls = getattr(cfg.output, "decls_header", None)
    _prompt: str = template.render(
        language=getattr(cfg.output, "language", "C++"),
        standard=getattr(cfg.output, "standard", "C++17"),
        module_name=module_name,
        decls_header=decls if decls else "",
    )
    return _prompt


def _render_task_prompt(module_name: str, subunit_context: dict[str, Any]) -> str:
    template_path = _PROMPT_DIR / "transform_task.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    _prompt: str = template.render(
        module_name=module_name,
        neighbours=subunit_context.get("neighbour_context", []),
        functions=subunit_context.get("functions_to_transform", []),
    )
    return _prompt


def _build_candidate_analysis(
    parsed_files: list[dict[str, str]],
    func_addr: str,
    func_name: str,
) -> tuple[tuple[str, ...], tuple[bool, ...], tuple[bool, ...]]:
    """Build diagnostic triples for parsed-but-unmatched analysis.

    Returns ``(candidate_paths, candidate_has_address, candidate_has_name)``
    aligned by index — one entry per parsed ``// FILE:`` block.

    ``candidate_has_address[i]`` is ``True`` when the target address appears
    in the file path OR in a ``// Original function: 0x...`` comment in the
    content (the same strict identity anchors used by
    ``_address_in_path_or_original_comment``).  A bare callee reference in
    content does NOT count as identity.
    ``candidate_has_name[i]`` is ``True`` when the target name appears
    (case-insensitive) and ``func_name`` is non-empty; ``False`` otherwise.
    """
    paths: list[str] = []
    has_addr: list[bool] = []
    has_name: list[bool] = []
    name_lower = func_name.lower() if func_name else ""
    for f in parsed_files:
        path = f["path"]
        content = f["content"]
        paths.append(path)
        has_addr.append(_address_in_path_or_original_comment(path, content, func_addr))
        has_name.append(bool(name_lower and (name_lower in content.lower() or name_lower in path.lower())))
    return tuple(paths), tuple(has_addr), tuple(has_name)


def _budget_exceeded_result(func: dict[str, Any]) -> dict[str, Any]:
    """Return a BUDGET_EXCEEDED result for a function.

    ``BUDGET_EXCEEDED`` is an explicit, non-cacheable verdict meaning the
    global transform budget (calls or tokens) was exhausted.  All functions
    in the subunit get this verdict when budget runs out mid-subunit.
    """
    return {
        "function": func["address"],
        "module": "",
        "files": [],
        "compiles": False,
        "verdict": "BUDGET_EXCEEDED",
        "diagnostic": {
            "match_strategy": "none",
            "identity_state": "none",
            "identity_reason": "Global transform budget exhausted",
        },
    }


def _provider_error_result(func: dict[str, Any]) -> dict[str, Any]:
    """Return a PROVIDER_ERROR result for a function.

    ``PROVIDER_ERROR`` is distinct from ``BUDGET_EXCEEDED`` — it means the
    LLM provider raised an exception.  Not cacheable, counted separately
    from budget exhaustion.
    """
    return {
        "function": func["address"],
        "module": "",
        "files": [],
        "compiles": False,
        "verdict": "PROVIDER_ERROR",
        "diagnostic": {
            "match_strategy": "none",
            "identity_state": "none",
            "identity_reason": "Provider error during LLM call",
        },
    }


def _handle_budget_exceeded(
    per_func_files: list[list[dict[str, str]]],
    per_func_strategies: list[str],
    identity_info: list[tuple[str, str, int]],
    functions_to_transform: list[dict[str, Any]],
) -> None:
    """Overwrite all per-func data when budget is exceeded mid-subunit."""
    for i in range(len(functions_to_transform)):
        per_func_files[i] = []
        per_func_strategies[i] = "none"
    for i in range(len(functions_to_transform)):
        identity_info[i] = ("none", "Budget exceeded — run stopped", 0)


def _opt_diagnostics_dir(cfg: Any) -> Path | None:
    """Resolve the diagnostics directory from cfg.optimization, or None.

    Uses defensive getattr so callers with a minimal cfg (no ``optimization``
    attribute) do not crash — they simply get no work packet writes.
    """
    opt = getattr(cfg, "optimization", None)
    if opt is None:
        return None
    raw = getattr(opt, "diagnostics_dir", "")
    if not raw:
        return None
    return Path(raw)


def _opt_raw_response_capture(cfg: Any) -> bool:
    """Whether raw LLM response capture is explicitly enabled."""
    opt = getattr(cfg, "optimization", None)
    if opt is None:
        return False
    return bool(getattr(opt, "raw_response_capture", False))


def _compile_per_function_enabled(cfg: Any) -> bool:
    """Whether per-function compile checks are enabled.

    Returns True when the flag is missing (backward-compatible default).
    """
    return getattr(cfg.validation, "compile_per_function", True)


def _compile_generated_cpp(
    func_files: list[dict[str, str]],
    cpp_file: dict[str, str],
    cfg: Any,
) -> tuple[bool, str]:
    """Compile function files, delegating to compile_generated_file_set for multi-file sets."""
    # When .h files are present alongside .cpp, use the generated-file-set path
    # so headers are available for #include resolution.
    if any(f.get("path", "").endswith(".h") for f in func_files if f != cpp_file):
        return compile_generated_file_set(func_files, cpp_file.get("path", ""), cfg)
    # Single .cpp only: fallback to simple compile_check.
    return compile_check(cpp_file["content"], cfg)


def process_subunit(
    subunit_context: dict[str, Any],
    module_name: str,
    llm: LLMProvider,
    cfg: Any,
    cache: Any,
    persist: bool = True,
    budget: TransformBudget | None = None,
) -> list[dict[str, Any]]:
    """Process a subunit of functions through the LLM transform pipeline.

    ``budget`` is REQUIRED in production — every ``llm.send()`` goes through
    ``_budgeted_send`` which enforces call, token, and compile-retry caps.
    ``process_modules`` always passes a configured budget.  When ``None``
    (tests without a budget), a fresh default is created each call so tests
    do not accidentally share budget state.
    """
    functions_to_transform = subunit_context.get("functions_to_transform", [])
    if not functions_to_transform:
        return []

    # Fresh default for callers (e.g. tests) that don't pass a budget.
    if budget is None:
        budget = TransformBudget()

    system = _render_system_prompt(cfg, module_name)
    repair_system = _render_repair_prompt(cfg, module_name)
    user = _render_task_prompt(module_name, subunit_context)
    messages = [Message(role="system", content=system), Message(role="user", content=user)]
    send_result = _budgeted_send(llm, messages, budget, "initial")

    if send_result.status != "ok":
        # Budget exceeded or provider error — return appropriate verdict for all functions
        log.warning("Initial LLM send failed: status=%s", send_result.status)
        if send_result.status == "provider_error":
            return [_provider_error_result(f) for f in functions_to_transform]
        return [_budget_exceeded_result(f) for f in functions_to_transform]

    response = send_result.response
    assert response is not None

    log.info("LLM response (first 500 chars): %s", response[:500])

    # Raw response capture is config-gated (default disabled). The legacy
    # unconditional write to .omo/evidence/llm-raw-subunit.txt is removed.
    diag_dir = _opt_diagnostics_dir(cfg)
    raw_capture = _opt_raw_response_capture(cfg)
    run_id = getattr(subunit_context, "run_id", "") or subunit_context.get("run_id", "") or ""
    subunit_index = subunit_context.get("subunit_index")
    raw_response_path: str | None = None

    # --no-persist: forbid ALL disk writes regardless of config.
    if not persist:
        diag_dir = None
        raw_capture = False

    if raw_capture and diag_dir is not None:
        raw_response_path = write_raw_response(response, diag_dir, run_id, module_name, subunit_index)

    # Parse LLM response into FileRecords with validated TARGET markers.
    # This single pass replaces the old _extract_targets_from_raw + _parse_llm_response.
    # ``has_invalid_file_block`` tracks whether any ``// FILE:`` block had an empty
    # path or empty content — this forces ``rejected_identity`` at the association
    # layer (protocol error, not silent skip).
    initial_records, has_invalid_file_block = _parse_llm_response_records(response)
    initial_target_map: dict[int, tuple[int, str]] = {
        i: r.target for i, r in enumerate(initial_records) if r.target is not None
    }
    has_raw_targets = bool(initial_target_map)
    has_any_target_marker = bool(_TARGET_LIKE_RAW_RE.search(response))

    parsed_files = [{"path": r.path, "content": r.content} for r in initial_records]
    initial_marker_count = len(parsed_files)
    marker_count = initial_marker_count
    max_retries = getattr(cfg.validation, "max_compile_retries", 0)
    # --no-persist: compile creates temp files (.o, temp dir) — forbid ALL
    # compilation when persist=False, even if config says otherwise.
    # The driver returns SKIPPED_COMPILE verdict (compilation skipped).
    compile_enabled = _compile_per_function_enabled(cfg) and persist

    # Global association: map parsed files to functions (immutable, once).
    # Uses pre-extracted target_map when available from the raw response.
    # ``has_target_markers`` tracks whether TARGET-like content was seen in the
    # raw response (even if none parsed as valid) — this prevents legacy
    # fallback when the LLM attempted TARGET but produced malformed output.
    # ``has_invalid_file_block`` forces immediate rejection when an empty FILE
    # path or empty content was found.
    # Read target contract mode from config
    target_contract_mode = getattr(getattr(cfg, "validation", None), "target_contract_mode", "legacy")
    is_strict = target_contract_mode == "required"

    per_func_files, per_func_strategies, identity_info = _associate_files_to_functions(
        parsed_files,
        functions_to_transform,
        target_map=initial_target_map if (has_raw_targets or has_any_target_marker) else None,
        has_target_markers=has_raw_targets or has_any_target_marker,
        has_invalid_file_block=has_invalid_file_block,
        strict_partial_recovery=is_strict,
    )

    # P0-1: Enforce target_contract_mode == "required".
    #   - No TARGET at all + strict → contract failure (no legacy fallback).
    #   - rejected_identity + strict → contract failure.
    #   - Partial + recovery incomplete + strict → contract failure (blocked below).
    #   - Legacy mode preserves historical name/address fallback.
    if is_strict and not has_any_target_marker and not has_raw_targets:
        # No TARGET markers at all — in required mode this is a hard contract failure.
        reason = "contract_failed: TARGET contract required but no TARGET markers found"
        per_func_files = [[] for _ in range(len(functions_to_transform))]
        per_func_strategies = ["rejected_identity"] * len(functions_to_transform)
        identity_info = [("rejected", reason, 0)] * len(functions_to_transform)
        log.warning("Contract failure in required mode: %s", reason)

    if is_strict and all(s == "rejected_identity" for s in per_func_strategies):
        # All rejected — already a hard failure. Enrich reason.
        reason = "contract_failed: TARGET markers invalid or contradictory in required mode"
        identity_info = [("rejected", reason, 0)] * len(functions_to_transform)

    # ── Target coverage recovery: partial valid TARGET → launch recovery ──
    # Detect: some functions have "explicit_identity", others have "none"
    # (this means valid TARGETs exist but not all functions are covered).
    # Hard rejections (rejected_identity) are NOT partial — they have conflicts.
    # Recovery is only attempted in strict mode.
    has_explicit = any(s == "explicit_identity" for s in per_func_strategies)
    has_none = any(s == "none" for s in per_func_strategies)

    if is_strict and has_explicit and has_none:
        log.info("Partial TARGET coverage detected — launching recovery for missing ordinals")
        initial_coverage = _analyze_target_coverage(initial_records, functions_to_transform)

        if not initial_coverage.has_conflict and not initial_coverage.is_complete:
            final_coverage = _run_target_recovery(
                initial_coverage,
                functions_to_transform,
                llm,
                system,
                budget,
                max_calls=_TARGET_RECOVERY_MAX_CALLS,
            )

            # Provider error during recovery → entire subunit is PROVIDER_ERROR
            if final_coverage.has_conflict and final_coverage.conflict_reason == "provider_error":
                log.warning("Provider error during target recovery — PROVIDER_ERROR for all functions")
                return [_provider_error_result(f) for f in functions_to_transform]

            # Budget exceeded during recovery → entire subunit is BUDGET_EXCEEDED
            if final_coverage.has_conflict and final_coverage.conflict_reason == "budget_exceeded":
                log.warning("Budget exceeded during target recovery — BUDGET_EXCEEDED for all functions")
                return [_budget_exceeded_result(f) for f in functions_to_transform]

            # P0-3: Merge ONLY with initial_coverage.covered_records (valid TARGETs),
            # never with unidentified FILE records.  Recovery records are validated
            # against existing covered paths before merge.
            merged_records = list(initial_coverage.covered_records) + [
                r for r in final_coverage.covered_records if r not in initial_coverage.covered_records
            ]
            parsed_files = [{"path": r.path, "content": r.content} for r in merged_records]
            marker_count = len(parsed_files)

            # Rebuild target_map from merged records
            recovery_target_map: dict[int, tuple[int, str]] = {
                i: r.target for i, r in enumerate(merged_records) if r.target is not None
            }
            per_func_files, per_func_strategies, identity_info = _associate_files_to_functions(
                parsed_files,
                functions_to_transform,
                target_map=recovery_target_map if recovery_target_map else None,
                has_target_markers=True,
                has_invalid_file_block=False,
                strict_partial_recovery=True,
            )

            if not final_coverage.is_complete:
                # P0-1: In strict mode, incomplete coverage blocks the ENTIRE
                # subunit — zero compile, zero cache, zero write, zero success.
                # All functions get INCOMPLETE_TARGETS regardless of whether
                # some were covered by initial or recovery.
                log.warning(
                    "Target recovery incomplete after %d calls: missing ordinals %s",
                    _TARGET_RECOVERY_MAX_CALLS,
                    sorted(final_coverage.missing_ordinals),
                )
                # Overwrite ALL per_func_files to empty — no function gets files
                # when the contract is incomplete.
                per_func_files = [[] for _ in range(len(functions_to_transform))]
                per_func_strategies = ["none"] * len(functions_to_transform)
                info_list = list(identity_info)
                for i in range(len(functions_to_transform)):
                    info_list[i] = ("none", "Contract failed: incomplete TARGET coverage after recovery", 0)
                identity_info = info_list
            else:
                log.info(
                    "Target recovery successful: %d/%d functions covered",
                    sum(1 for f in per_func_files if f),
                    len(functions_to_transform),
                )

    # First pass: compile-check all functions, collect failures
    failed_funcs: list[dict[str, Any]] = []
    for i, func in enumerate(functions_to_transform):
        func_files = per_func_files[i]
        if not func_files:
            continue
        if not compile_enabled:
            continue
        cpp_file = next((f for f in func_files if f["path"].endswith(".cpp")), func_files[0])
        compiles, err = _compile_generated_cpp(func_files, cpp_file, cfg)
        if not compiles:
            failed_funcs.append({"func": func, "files": func_files, "err": err})

    # P0-3: Classify compile failures by GCC category.  Decide retry strategy:
    #   - 0 retryable categories => no LLM send at all.
    #   - 1 retryable          => skip subunit retry, let per-function retry handle it
    #                             (at most 1 call via _retry_with_conversation).
    #   - >=2 retryables       => one subunit retry (consumes global compile-retry cap,
    #                             disables all per-function retries).
    # Categories include/decls/too-many/unknown/empty never trigger an LLM call.
    from re_agent.build.transform.diagnostics import classify_compile_error

    retryable_categories = [
        classify_compile_error(f["err"])
        for f in failed_funcs
        if f["err"].strip() and classify_compile_error(f["err"]) in _RETRYABLE_COMPILE_CATEGORIES
    ]
    n_retryable = len(retryable_categories)

    retry_marker_count = 0
    # P0-3 decision tree:
    #   0 retryable → no LLM call (skip subunit retry entirely)
    #   1 retryable → skip subunit retry, let per-function handle
    #   >=2 retryable → one subunit retry (only if compile retry budget remains)
    if failed_funcs and max_retries > 0 and n_retryable >= 2 and budget.compile_retry_calls_remaining > 0:
        error_annotations = "\n\n".join(
            f"Function {f['func']['address']} failed to compile:\n{f['err']}" for f in failed_funcs
        )
        # Include ordinal mapping in retry prompt so the LLM can produce
        # valid // TARGET: markers for the retry response.
        ordinal_lines = "\n".join(f"  Ordinal {i}: {f['address']}" for i, f in enumerate(functions_to_transform))
        retry_prompt = (
            "The following functions failed to compile. Fix ALL of them and "
            "re-output with // FILE: markers. "
            "Use // TARGET: <ordinal> <address> before each // FILE: block "
            "to declare the target function.\n\n"
            f"Function ordinal mapping:\n{ordinal_lines}\n\n" + error_annotations
        )
        retry_messages = [
            Message(role="system", content=repair_system),
            Message(role="user", content=user),
            Message(role="assistant", content=response),
            Message(role="user", content=retry_prompt),
        ]
        budget.compile_retry_calls_remaining -= 1
        retry_send = _budgeted_send(llm, retry_messages, budget, "compile_subunit_retry", {"subunit_retry": True})
        retry_records: list[FileRecord] = []
        retry_has_invalid = False
        retry_response: str | None = None
        if retry_send.status == "budget_exceeded":
            log.warning("Budget exceeded during compile_subunit_retry — BUDGET_EXCEEDED for all functions")
            return [_budget_exceeded_result(f) for f in functions_to_transform]
        elif retry_send.status == "provider_error":
            log.warning("Provider error during compile_subunit_retry — PROVIDER_ERROR for all functions")
            return [_provider_error_result(f) for f in functions_to_transform]
        elif retry_send.status == "ok":
            retry_response = retry_send.response
            assert retry_response is not None
            retry_records, retry_has_invalid = _parse_llm_response_records(retry_response)
        retry_marker_count = len(retry_records)

        # If the retry itself has an invalid file block, reject the retry
        # entirely (keep initial records and association) — the retry response
        # is malformed and must not degrade the initial mapping.
        if retry_records and not retry_has_invalid and retry_response is not None:
            # Merge with target preservation: known paths keep initial target;
            # new paths require valid TARGET (validated in re-association).
            # Strict TARGET validation is required when initial was explicit.
            merged_records = _merge_retry_records(
                initial_records,
                retry_records,
                require_target=has_raw_targets,
            )
            parsed_files = [{"path": r.path, "content": r.content} for r in merged_records]
            marker_count = len(parsed_files)
            max_retries -= 1

            # Rebuild target_map from merged records: known targets preserved,
            # new records carry their retry target (validated by parse).
            merged_target_map: dict[int, tuple[int, str]] = {
                i: r.target for i, r in enumerate(merged_records) if r.target is not None
            }
            # If the initial response used explicit TARGET, we must NEVER fall
            # back to legacy matching on cleaned content (where TARGET markers
            # were parsed out of the body).  An empty merged_target_map means
            # the retry violated the TARGET contract — force explicit identity
            # which will reject all functions.
            retry_has_markers = bool(_TARGET_LIKE_RAW_RE.search(retry_response))
            force_explicit = has_raw_targets or bool(merged_target_map) or retry_has_markers
            per_func_files, per_func_strategies, identity_info = _associate_files_to_functions(
                parsed_files,
                functions_to_transform,
                target_map=merged_target_map if force_explicit else None,
                has_target_markers=force_explicit,
                has_invalid_file_block=False,  # retry was validated above
            )
    effective_marker_count = marker_count

    # Each still-failing function gets its OWN retry budget. (Previously a single
    # shared counter starved every function after the first one or two.)
    per_func_retries = max_retries

    # Per-function result building (with per-function retry for still-failing)
    results: list[dict[str, Any]] = []
    function_verdicts: list[FunctionVerdict] = []
    total_files_written = 0
    for i, func in enumerate(functions_to_transform):
        addr = func["address"]
        func_name = func.get("name", "")
        func_files = per_func_files[i]
        strategy = per_func_strategies[i]
        identity_state, identity_reason, target_count = identity_info[i]

        candidate_paths, candidate_has_address, candidate_has_name = _build_candidate_analysis(
            parsed_files, addr, func_name
        )

        if not func_files:
            # Distinguish INCOMPLETE_TARGETS (partial coverage after recovery
            # failed or contract failed in strict mode) from NO_OUTPUT (no files
            # at all, no TARGET attempted).
            is_incomplete = (
                strategy == "none" and "target recovery" in identity_reason.lower()
            ) or "contract failed" in identity_reason.lower()
            verdict = "INCOMPLETE_TARGETS" if is_incomplete else "NO_OUTPUT"
            function_verdicts.append(
                FunctionVerdict(
                    address=addr,
                    verdict=verdict,
                    compiles=False,
                    files_matched=0,
                    match_strategy=strategy,
                    candidate_paths=candidate_paths,
                    candidate_has_address=candidate_has_address,
                    candidate_has_name=candidate_has_name,
                    identity_state=identity_state,
                    identity_reason=identity_reason,
                    target_file_count=target_count,
                )
            )
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": [],
                    "compiles": False,
                    "verdict": verdict,
                }
            )
            continue

        cpp_file = next((f for f in func_files if f["path"].endswith(".cpp")), func_files[0])

        if not compile_enabled:
            function_verdicts.append(
                FunctionVerdict(
                    address=addr,
                    verdict="SKIPPED_COMPILE",
                    compiles=False,
                    files_matched=len(func_files),
                    match_strategy=strategy,
                    candidate_paths=candidate_paths,
                    candidate_has_address=candidate_has_address,
                    candidate_has_name=candidate_has_name,
                    identity_state=identity_state,
                    identity_reason=identity_reason,
                    target_file_count=target_count,
                )
            )
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": func_files,
                    "compiles": False,
                    "verdict": "SKIPPED_COMPILE",
                }
            )
            continue

        compiles, err = _compile_generated_cpp(func_files, cpp_file, cfg)
        if compiles:
            total_files_written += len(func_files)
            function_verdicts.append(
                FunctionVerdict(
                    address=addr,
                    verdict="PASS",
                    compiles=True,
                    files_matched=len(func_files),
                    match_strategy=strategy,
                    candidate_paths=candidate_paths,
                    candidate_has_address=candidate_has_address,
                    candidate_has_name=candidate_has_name,
                    identity_state=identity_state,
                    identity_reason=identity_reason,
                    target_file_count=target_count,
                )
            )
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": func_files,
                    "compiles": True,
                    "verdict": "PASS",
                }
            )
        elif per_func_retries > 0:
            retry_files, retry_skip_reason = _retry_with_conversation(
                func_files,
                err,
                func,
                repair_system,
                user,
                llm,
                per_func_retries,
                cfg,
                budget=budget,
                ordinal=i,
            )
            if retry_files is not None:
                # Retry was actually attempted (LLM call was made)
                if retry_files:
                    func_files = retry_files
                    cpp_file = next((f for f in func_files if f["path"].endswith(".cpp")), func_files[0])
                retry_compiles, retry_err = _compile_generated_cpp(func_files, cpp_file, cfg)
                if retry_compiles:
                    total_files_written += len(func_files)
                    function_verdicts.append(
                        FunctionVerdict(
                            address=addr,
                            verdict="PASS_RETRY",
                            compiles=True,
                            files_matched=len(func_files),
                            match_strategy=strategy,
                            candidate_paths=candidate_paths,
                            candidate_has_address=candidate_has_address,
                            candidate_has_name=candidate_has_name,
                            identity_state=identity_state,
                            identity_reason=identity_reason,
                            target_file_count=target_count,
                        )
                    )
                    results.append(
                        {
                            "function": addr,
                            "module": module_name,
                            "files": func_files,
                            "compiles": True,
                            "verdict": "PASS_RETRY",
                        }
                    )
                else:
                    # Retry attempted but still fails → FAIL_AFTER_RETRY
                    function_verdicts.append(
                        FunctionVerdict(
                            address=addr,
                            verdict="FAIL_AFTER_RETRY",
                            compiles=False,
                            files_matched=len(func_files),
                            match_strategy=strategy,
                            candidate_paths=candidate_paths,
                            candidate_has_address=candidate_has_address,
                            candidate_has_name=candidate_has_name,
                            compile_error=truncate_compile_error(retry_err),
                            compile_error_category=classify_compile_error(retry_err),
                            identity_state=identity_state,
                            identity_reason=identity_reason,
                            target_file_count=target_count,
                        )
                    )
                    results.append(
                        {
                            "function": addr,
                            "module": module_name,
                            "files": func_files,
                            "compiles": False,
                            "verdict": "FAIL_AFTER_RETRY",
                        }
                    )
            else:
                if retry_skip_reason == "PROVIDER_ERROR":
                    # Provider error during retry → PROVIDER_ERROR (not FAIL_NO_RETRY).
                    # provider_error_count was already incremented by _budgeted_send.
                    function_verdicts.append(
                        FunctionVerdict(
                            address=addr,
                            verdict="PROVIDER_ERROR",
                            compiles=False,
                            files_matched=len(func_files),
                            match_strategy=strategy,
                            candidate_paths=candidate_paths,
                            candidate_has_address=candidate_has_address,
                            candidate_has_name=candidate_has_name,
                            identity_state=identity_state,
                            identity_reason=identity_reason,
                            target_file_count=target_count,
                            retry_skip_reason="Provider error during retry",
                        )
                    )
                    results.append(
                        {
                            "function": addr,
                            "module": module_name,
                            "files": func_files,
                            "compiles": False,
                            "verdict": "PROVIDER_ERROR",
                        }
                    )
                    continue  # skip FAIL_NO_RETRY fallthrough

                if retry_skip_reason == "BUDGET_EXCEEDED":
                    # Budget exceeded during retry → BUDGET_EXCEEDED (not FAIL_NO_RETRY)
                    function_verdicts.append(
                        FunctionVerdict(
                            address=addr,
                            verdict="BUDGET_EXCEEDED",
                            compiles=False,
                            files_matched=0,
                            match_strategy=strategy,
                            candidate_paths=candidate_paths,
                            candidate_has_address=candidate_has_address,
                            candidate_has_name=candidate_has_name,
                            identity_state=identity_state,
                            identity_reason=identity_reason,
                            target_file_count=target_count,
                            retry_skip_reason="Budget exceeded during per-function retry",
                        )
                    )
                    results.append(
                        {
                            "function": addr,
                            "module": module_name,
                            "files": [],
                            "compiles": False,
                            "verdict": "BUDGET_EXCEEDED",
                        }
                    )
                    continue  # skip FAIL_NO_RETRY fallthrough

                # No retry LLM call made: fall through to FAIL_NO_RETRY
                function_verdicts.append(
                    FunctionVerdict(
                        address=addr,
                        verdict="FAIL_NO_RETRY",
                        compiles=False,
                        files_matched=len(func_files),
                        match_strategy=strategy,
                        candidate_paths=candidate_paths,
                        candidate_has_address=candidate_has_address,
                        candidate_has_name=candidate_has_name,
                        compile_error=truncate_compile_error(err),
                        compile_error_category=classify_compile_error(err),
                        identity_state=identity_state,
                        identity_reason=identity_reason,
                        target_file_count=target_count,
                        retry_skip_reason=retry_skip_reason,
                    )
                )
                results.append(
                    {
                        "function": addr,
                        "module": module_name,
                        "files": func_files,
                        "compiles": False,
                        "verdict": "FAIL_NO_RETRY",
                    }
                )
        else:
            no_retry_reason = "max_compile_retries=0 in config — retry disabled"
            function_verdicts.append(
                FunctionVerdict(
                    address=addr,
                    verdict="FAIL_NO_RETRY",
                    compiles=False,
                    files_matched=len(func_files),
                    match_strategy=strategy,
                    candidate_paths=candidate_paths,
                    candidate_has_address=candidate_has_address,
                    candidate_has_name=candidate_has_name,
                    compile_error=truncate_compile_error(err),
                    compile_error_category=classify_compile_error(err),
                    identity_state=identity_state,
                    identity_reason=identity_reason,
                    target_file_count=target_count,
                    retry_skip_reason=no_retry_reason,
                )
            )
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": func_files,
                    "compiles": False,
                    "verdict": "FAIL_NO_RETRY",
                }
            )

    subunit_strategy = _subunit_match_strategy(per_func_strategies)
    usage_snapshot = get_usage(llm)
    model_usage = ModelUsage(
        provider=getattr(llm, "provider_name", type(llm).__name__),
        model=getattr(llm, "model", getattr(llm, "_model", "unknown")),
        prompt_tokens=usage_snapshot.prompt_tokens,
        completion_tokens=usage_snapshot.completion_tokens,
        cache_hit_tokens=usage_snapshot.cache_hit_tokens,
        cache_miss_tokens=usage_snapshot.cache_miss_tokens,
        calls=usage_snapshot.calls,
    )
    router_decision = default_router_decision()
    diagnostics = SubunitDiagnostics(
        run_id=run_id,
        module_name=module_name,
        subunit_index=subunit_index if isinstance(subunit_index, int) else None,
        raw_response_length=len(response),
        marker_count=marker_count,
        parse_count=marker_count,
        initial_marker_count=initial_marker_count,
        retry_marker_count=retry_marker_count,
        effective_marker_count=effective_marker_count,
        match_strategy=subunit_strategy,
        total_files_written=total_files_written,
        function_verdicts=tuple(function_verdicts),
        model_usage=model_usage,
        router_decision=router_decision,
        raw_response_path=raw_response_path,
        work_packet_path=None,
    )
    # --no-persist: forbid diagnostics/work-packet JSON writes.
    work_packet_path = write_diagnostics(diagnostics, diag_dir) if persist else None

    diag_summary: dict[str, Any] = {
        "raw_response_length": diagnostics.raw_response_length,
        "marker_count": diagnostics.marker_count,
        "parse_count": diagnostics.parse_count,
        "initial_marker_count": diagnostics.initial_marker_count,
        "retry_marker_count": diagnostics.retry_marker_count,
        "effective_marker_count": diagnostics.effective_marker_count,
        "match_strategy": subunit_strategy,
        "work_packet_path": work_packet_path,
        "raw_response_path": raw_response_path,
        "model_usage": model_usage.to_json_dict() if model_usage else None,
        "router_decision": dict(router_decision),
    }
    for i, r in enumerate(results):
        per_func_diag = dict(diag_summary)
        per_func_diag["match_strategy"] = per_func_strategies[i]
        # ``files_written`` per-result = all files matched to this function,
        # regardless of compile outcome. This differs from WorkPacket-level
        # ``total_files_written`` which is scoped to PASS/PASS_RETRY verdicts
        # only (files eligible for output write). Both names kept for
        # backward compatibility — ``files_written`` describes matched
        # candidates, ``total_files_written`` describes output-eligible count.
        per_func_diag["files_written"] = function_verdicts[i].files_matched
        per_func_diag["candidate_paths"] = list(function_verdicts[i].candidate_paths)
        per_func_diag["candidate_has_address"] = list(function_verdicts[i].candidate_has_address)
        per_func_diag["candidate_has_name"] = list(function_verdicts[i].candidate_has_name)
        per_func_diag["compile_error"] = function_verdicts[i].compile_error
        per_func_diag["compile_error_category"] = function_verdicts[i].compile_error_category
        per_func_diag["identity_state"] = function_verdicts[i].identity_state
        per_func_diag["identity_reason"] = function_verdicts[i].identity_reason
        per_func_diag["target_file_count"] = function_verdicts[i].target_file_count
        per_func_diag["retry_skip_reason"] = function_verdicts[i].retry_skip_reason
        r["diagnostic"] = per_func_diag

    return results


def _subunit_match_strategy(per_func_strategies: list[str]) -> str:
    """Reduce per-function match strategies to a single subunit-level label.

    Prefers the first non-``none`` strategy (the strategy that actually matched
    files for at least one function). Falls back to ``none`` when every
    function failed to match.
    """
    for s in per_func_strategies:
        if s != "none":
            return s
    return "none"


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _retry_with_conversation(
    func_files: list[dict[str, str]],
    err: str,
    func: dict[str, Any],
    system: str,
    original_user: str,
    llm: LLMProvider,
    max_retries: int,
    cfg: Any,
    budget: TransformBudget,
    ordinal: int = 0,
) -> tuple[list[dict[str, str]] | None, str]:
    """Retry fixing compile errors using multi-turn conversation.

    ``budget`` is REQUIRED — the compile-retry cap (``compile_retry_calls_remaining``)
    is checked via ``_compile_retry_allowed`` before the first send, and the
    call/token caps are enforced via ``_budgeted_send``.

    Sends: original system + original task + model's prior output + error.
    Makes at most 1 retry per function (max_retries loop but compile-retry
    budget is consumed once at entry; subsequent loop iterations only consume
    call/token budget).

    The retry prompt includes the function ordinal and address and asks the
    LLM to preserve ``// TARGET:`` markers so the output can be re-associated.

    **P0 retry contract**: each retry response is validated before use.
    Every ``// FILE:`` block must carry a valid ``// TARGET:`` with the
    expected ordinal and address.  If any record lacks a valid target or
    has a contradictory one, the retry response is rejected (previous files
    are preserved without degradation).

    Returns:
        ``(files_or_None, skip_reason)`` where:
        - ``files_or_None``:
            - ``None`` — no retry was attempted (budget, category, or stagnation
              prevented it).  Caller produces FAIL_NO_RETRY.
            - ``list[dict]`` — the (possibly unchanged) file set after retry.
              Caller must recompile to determine PASS_RETRY vs FAIL_AFTER_RETRY.
        - ``skip_reason``: empty string when retry was attempted; non-empty
          explanation when retry was skipped (for diagnostic recording).
    """
    # GCC retry policy: only retry if the compile error category is actionable.
    func_addr = func.get("address", "?")
    category = classify_compile_error(err)

    current_files = func_files
    cpp_file = next((f for f in current_files if f["path"].endswith(".cpp")), current_files[0])

    retry_allowed, skip_reason = _compile_retry_allowed(
        category,
        err,
        None,
        budget,
        budget.subunit_retry_occurred,
    )

    ordinal_str = f" (ordinal {ordinal})"
    expected_target = (ordinal, func_addr.lower())

    if not retry_allowed:
        log.info("Retry skipped for %s: %s", func_addr, skip_reason)
        return None, skip_reason  # signal to caller: no retry LLM call was made

    # Consume one compile-retry unit for this function (max 1 per function).
    budget.compile_retry_calls_remaining -= 1

    _llm_call_made = False
    prev_stderr_hash: str | None = None

    for _ in range(max_retries):
        if not budget.check_before_call("compile_func_retry"):
            log.warning("Budget exhausted before func retry for %s", func_addr)
            break
        # Stagnation check: identical SHA-256 stderr → retry won't help
        if prev_stderr_hash is not None and err.strip():
            current_hash = hashlib.sha256(err.encode()).hexdigest()
            if current_hash == prev_stderr_hash:
                log.warning("Stagnant stderr for %s — stopping retry loop", func_addr)
                break
        _llm_call_made = True
        prior_output = "\n\n".join(f"// FILE: {f['path']}\n{f['content']}" for f in current_files)
        retry_prompt = (
            f"The following code for function {func_addr}{ordinal_str} "
            f"failed to compile with GCC:\n\n"
            f"```cpp\n{cpp_file['content']}\n```\n\n"
            f"Compiler error:\n{err}\n\n"
            f"Fix the code and output it with the same // FILE: markers. "
            f"Include a ``// TARGET: <ordinal> {func_addr}`` line before each "
            f"``// FILE:`` block so the output can be matched to the correct function."
        )
        retry_messages = [
            Message(role="system", content=system),
            Message(role="user", content=original_user),
            Message(role="assistant", content=prior_output),
            Message(role="user", content=retry_prompt),
        ]
        send_result = _budgeted_send(llm, retry_messages, budget, "compile_func_retry")

        if send_result.status != "ok":
            if send_result.status == "budget_exceeded":
                log.warning("Budget exceeded during func retry for %s", func_addr)
                return None, "BUDGET_EXCEEDED"
            # provider_error — signal to caller for PROVIDER_ERROR verdict
            log.warning("Provider error during func retry for %s", func_addr)
            return None, "PROVIDER_ERROR"
        retry_response = send_result.response
        assert retry_response is not None
        retry_records, retry_has_invalid = _parse_llm_response_records(retry_response)
        # Validate every retry record.
        # An invalid file block (empty path or content) is a protocol error;
        # the entire retry is rejected (current_files preserved unchanged).
        all_valid = not retry_has_invalid
        if all_valid and retry_records:
            for rr in retry_records:
                if rr.target is None:
                    all_valid = False
                    break
                if rr.target != expected_target:
                    all_valid = False
                    break
        if all_valid:
            new_files = [{"path": r.path, "content": r.content} for r in retry_records]
            current_files = new_files
            cpp_file = next((f for f in current_files if f["path"].endswith(".cpp")), current_files[0])
        new_compiles, err = _compile_generated_cpp(current_files, cpp_file, cfg)
        if new_compiles:
            break
        # Track stderr SHA-256 for stagnation detection in next iteration
        prev_stderr_hash = hashlib.sha256(err.encode()).hexdigest() if err.strip() else None
    if not _llm_call_made:
        return None, "no LLM call made (budget exhausted before first send)"  # signal to caller: no retry
    return current_files, ""
