"""Subunit-level WorkPacket diagnostics for the transform pipeline (Todo 8).

Records per-subunit diagnostic data — parse counts, match strategy, files
written, per-function compile verdicts, normalized model/cache usage, and a
router decision snapshot — and writes it as JSON to a run/evidence/report
scoped directory. Never writes under ``reports/re-agent/code/``.

Design:
- Frozen dataclasses with slots; stdlib-only JSON roundtrip.
- ``ModelUsage`` is reused from ``work_packet_types`` so cache metrics stay
  ``None`` (not faked zero) when the provider does not surface them.
- The router decision is recorded as a pure-data snapshot (dict); this module
  does NOT call the live router. Todo 8 records an explicit default/placeholder
  decision so the diagnostic is self-describing even before routing is active.
- No LLM calls, no file reads. The only IO is the optional JSON write.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from re_agent.build.work_packet_types import JsonValue, ModelUsage

__all__ = [
    "COMPILE_ERROR_MAX_LENGTH",
    "FunctionVerdict",
    "SubunitDiagnostics",
    "classify_compile_error",
    "default_router_decision",
    "model_usage_to_json",
    "truncate_compile_error",
    "write_diagnostics",
]

_VALID_MATCH_STRATEGIES = frozenset(
    {
        "single_function",
        "by_name",
        "by_address",
        "single_file_fallback",
        "none",
        "explicit_identity",
        "rejected_identity",
    }
)

# Maximum length for captured compiler stderr. Stderr longer than this is
# deterministically truncated to preserve first KEEP_HEAD chars and last
# KEEP_TAIL chars, with a bounded truncation marker between them.
COMPILE_ERROR_MAX_LENGTH = 4096
_KEEP_HEAD = 2048
_KEEP_TAIL = 1024
_TRUNC_MARKER = "\n... [truncated %d chars] ...\n"


def truncate_compile_error(stderr: str) -> str:
    """Deterministically truncate compiler stderr when it exceeds the maximum length.

    Preserves the first ``_KEEP_HEAD`` characters and the last ``_KEEP_TAIL``
    characters, with a ``... [truncated N chars] ...`` marker between them.
    This keeps the beginning (error type, file:line) and the end (caret line,
    final error) of typical GCC stderr output.
    """
    if len(stderr) <= COMPILE_ERROR_MAX_LENGTH:
        return stderr
    truncated_len = len(stderr) - COMPILE_ERROR_MAX_LENGTH
    # Account for the truncation marker length (slightly imprecise for rare
    # multi-digit N, but within a few chars of the budget and well below the
    # old length).
    marker = _TRUNC_MARKER % truncated_len
    head = stderr[:_KEEP_HEAD]
    tail = stderr[-_KEEP_TAIL:]
    return head + marker + tail


def classify_compile_error(stderr: str) -> str:
    """Classify a compiler stderr string into a coarse category.

    Returns one of:
    - ``include_error`` — fatal include/file-not-found error (blocking
      resolution of #include or -include)
    - ``too_many_arguments`` — argument count mismatch (too many/few args)
    - ``undeclared_identifier`` — unknown type, variable, or function name
    - ``type_mismatch`` — type conversion/assignment incompatibility
    - ``syntax_error`` — parse/lex error (expected token, stray char, etc.)
    - ``goto_error`` — ``jump to label`` / ``crosses initialization``
    - ``decls_header_warning`` — ``_decls.h`` dllimport artifact warning
    - ``unknown`` — no recognised pattern

    Classification is based on keyword matching against patterns found in
    common GCC / MinGW diagnostics. It is intentionally coarse and generic
    (not project-specific).
    """
    lower = stderr.lower()

    # Check include_error early: a fatal include/file-not-found error blocks
    # all subsequent C++ analysis, so it should not be masked by downstream
    # patterns that happen to co-occur.  The pattern combines the GCC/Clang
    # "fatal error:" prefix with "No such file or directory" to avoid
    # over-matching unrelated fatal errors (e.g. write failures).
    if _matches_any(
        lower,
        ("no such file or directory",),
    ):
        return "include_error"

    # Check syntax_error first because "expected" is very common and could
    # appear alongside other patterns.
    if _matches_any(
        lower,
        (
            "error: expected",
            "error: stray",
            "error: missing terminating",
            "error: parse error",
            "syntax error",
        ),
    ):
        return "syntax_error"

    if _matches_any(
        lower,
        (
            "too many arguments",
            "too few arguments",
        ),
    ):
        return "too_many_arguments"

    if _matches_any(
        lower,
        (
            "was not declared",
            "undeclared",
            "unknown type name",
            "has not been declared",
            "did you mean",
            "not declared",
            "not a type",
            "does not name a type",
        ),
    ):
        return "undeclared_identifier"

    if _matches_any(
        lower,
        (
            "cannot convert",
            "cannot initialize",
            "invalid conversion",
            "incompatible",
            "no matching",
            "no known conversion",
            "cannot bind",
            "cannot be used",
            "invalid type",
        ),
    ):
        return "type_mismatch"

    # goto_error: jump to label / crosses initialization. This is a real
    # C++ control-flow error where a goto jumps past a variable declaration
    # with an initializer. It must be checked before decls_header_warning
    # because the latter is an artifact of the compile context (not a real
    # body error). The same stderr often contains BOTH patterns, and
    # goto_error should win as the real C++ error.
    if _matches_any(
        lower,
        (
            "jump to label",
            "crosses initialization",
        ),
    ):
        return "goto_error"

    # _decls.h dllimport warning-as-error: the force-included _decls.h
    # triggers -Werror=attributes for symbols redeclared without dllimport
    # in the freestanding compile check.  This warning is an artifact of
    # the compile context, not a real C++ error in the decompiled function.
    if "_decls.h" in lower and "redeclared without dllimport" in lower:
        return "decls_header_warning"

    return "unknown"


def _matches_any(lower_text: str, patterns: tuple[str, ...]) -> bool:
    return any(p in lower_text for p in patterns)


@dataclass(frozen=True, slots=True)
class FunctionVerdict:
    """Per-function outcome within a subunit.

    ``candidate_paths``, ``candidate_has_address``, and ``candidate_has_name``
    are diagnostic triples aligned by index: for every parsed ``// FILE:`` block
    they record whether its path/content contained the target function's address
    or name. This makes parsed-but-unmatched diagnostics actionable without
    requiring raw-response capture. All three are empty when no files were
    parsed.

    ``compile_error`` holds the bounded compiler stderr text (truncated
    deterministically via ``truncate_compile_error`` when overlong) for
    compile-failed verdicts (FAIL_NO_RETRY, FAIL_AFTER_RETRY). It is
    ``None`` for PASS, PASS_RETRY, and NO_OUTPUT verdicts where no
    compile error was produced.

    ``compile_error_category`` is a coarse classification of the compile
    error via ``classify_compile_error``. It is ``None`` when
    ``compile_error`` is ``None``.

    ``identity_state`` describes the association method:
    - ``"explicit"`` — matched via ``// TARGET:`` explicit identity markers.
    - ``"matched"`` — matched via direct address/name matching.
    - ``"rejected"`` — explicit identity was present but invalid.
    - ``"none"`` — no files matched to this function.

    ``identity_reason`` provides a human-readable explanation when the
    identity was rejected or no match was found. It is empty on success.

    ``target_file_count`` is the number of files explicitly associated
    with this function's target grouping (via ``// TARGET:`` or equivalent).
    For non-explicit strategies it equals ``files_matched``.
    """

    address: str
    verdict: str
    compiles: bool
    files_matched: int
    match_strategy: str = "none"
    candidate_paths: tuple[str, ...] = ()
    candidate_has_address: tuple[bool, ...] = ()
    candidate_has_name: tuple[bool, ...] = ()
    compile_error: str | None = None
    compile_error_category: str | None = None
    identity_state: str = ""
    identity_reason: str = ""
    target_file_count: int = 0

    def __post_init__(self) -> None:
        if (self.compile_error is None) != (self.compile_error_category is None):
            raise ValueError("compile_error and compile_error_category must both be None or both non-None")

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "address": self.address,
            "verdict": self.verdict,
            "compiles": self.compiles,
            "files_matched": self.files_matched,
            "match_strategy": self.match_strategy,
            "candidate_paths": list(self.candidate_paths),
            "candidate_has_address": list(self.candidate_has_address),
            "candidate_has_name": list(self.candidate_has_name),
            "compile_error": self.compile_error,
            "compile_error_category": self.compile_error_category,
            "identity_state": self.identity_state,
            "identity_reason": self.identity_reason,
            "target_file_count": self.target_file_count,
        }


@dataclass(frozen=True, slots=True)
class SubunitDiagnostics:
    """Subunit-level diagnostic snapshot written as the WorkPacket report.

    ``marker_count`` and ``parse_count`` are the same value (the number of
    ``// FILE:`` blocks parsed from the LLM response); both names are kept
    so consumers can read either without ambiguity.

    ``total_files_written`` is the count of files that will be written to the
    output tree for this subunit (sum of ``files_matched`` over functions
    whose verdict is PASS / PASS_RETRY). ``process_subunit`` itself does not
    write source files — that is ``module_processor``'s responsibility — so
    this is the count of files *eligible* to be written.

    ``raw_response_path`` is ``None`` unless raw response capture is enabled.
    ``work_packet_path`` is ``None`` when ``diagnostics_dir`` is empty.

    Additive retry-count fields (all present even when no retry occurred):
    ``initial_marker_count`` — the pre-retry raw ``// FILE:`` block count.
    ``retry_marker_count`` — the retry response ``// FILE:`` block count (0 if no retry).
    ``effective_marker_count`` — the post-merge effective count (same as
    ``marker_count`` in the no-retry case; >= initial when retry preserved files).

    Old ``marker_count`` / ``parse_count`` consumers still find them; they reflect
    the **effective** (post-merge) count for backward compatibility.
    """

    run_id: str
    module_name: str
    subunit_index: int | None
    raw_response_length: int
    marker_count: int
    parse_count: int
    initial_marker_count: int
    retry_marker_count: int
    effective_marker_count: int
    match_strategy: str
    total_files_written: int
    function_verdicts: tuple[FunctionVerdict, ...]
    model_usage: ModelUsage | None
    router_decision: Mapping[str, JsonValue]
    raw_response_path: str | None
    work_packet_path: str | None

    def __post_init__(self) -> None:
        if self.match_strategy not in _VALID_MATCH_STRATEGIES:
            raise ValueError(f"match_strategy {self.match_strategy!r} not in {sorted(_VALID_MATCH_STRATEGIES)}")
        if self.marker_count < 0 or self.parse_count < 0:
            raise ValueError("marker_count/parse_count must be non-negative")
        if self.initial_marker_count < 0:
            raise ValueError("initial_marker_count must be non-negative")
        if self.retry_marker_count < 0:
            raise ValueError("retry_marker_count must be non-negative")
        if self.effective_marker_count < 0:
            raise ValueError("effective_marker_count must be non-negative")
        if self.total_files_written < 0:
            raise ValueError("total_files_written must be non-negative")
        if self.marker_count != self.parse_count:
            raise ValueError("marker_count must equal parse_count")

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "module_name": self.module_name,
            "subunit_index": self.subunit_index,
            "raw_response_length": self.raw_response_length,
            "marker_count": self.marker_count,
            "parse_count": self.parse_count,
            "initial_marker_count": self.initial_marker_count,
            "retry_marker_count": self.retry_marker_count,
            "effective_marker_count": self.effective_marker_count,
            "match_strategy": self.match_strategy,
            "total_files_written": self.total_files_written,
            "function_verdicts": [fv.to_json_dict() for fv in self.function_verdicts],
            "model_usage": model_usage_to_json(self.model_usage),
            "router_decision": dict(self.router_decision),
            "raw_response_path": self.raw_response_path,
            "work_packet_path": self.work_packet_path,
        }


def default_router_decision() -> dict[str, JsonValue]:
    """Explicit placeholder router decision used before live routing is active.

    Todo 8 records this so the diagnostic is self-describing: the router is
    not yet wired into the transform runtime, so every subunit gets a
    ``select_model`` decision with the default model and an explicit reason.
    This is NOT a live routing call.
    """
    return {
        "action": "select_model",
        "model": None,
        "reason": "router not yet active: default placeholder decision (todo 8)",
        "should_retry": True,
    }


def model_usage_to_json(usage: ModelUsage | None) -> dict[str, JsonValue] | None:
    if usage is None:
        return None
    return usage.to_json_dict()


def write_diagnostics(
    diagnostics: SubunitDiagnostics,
    diagnostics_dir: Path | None,
) -> str | None:
    """Write the diagnostics JSON to ``diagnostics_dir``.

    Returns the string path of the written file, or ``None`` when
    ``diagnostics_dir`` is ``None`` or empty (backward-compatible opt-out).

    The filename is deterministic per subunit so repeated runs with the same
    ``run_id`` / ``module_name`` / ``subunit_index`` overwrite the same file
    (no stale-state accumulation). The directory is created if missing.
    """
    if diagnostics_dir is None or str(diagnostics_dir) == "":
        return None
    diag_dir = Path(diagnostics_dir)
    diag_dir.mkdir(parents=True, exist_ok=True)
    idx = diagnostics.subunit_index
    idx_part = f"{idx:03d}" if idx is not None else "na"
    safe_module = diagnostics.module_name.replace("/", "_").replace("\\", "_")
    fname = f"workpacket-{diagnostics.run_id}-{safe_module}-{idx_part}.json"
    out_path = diag_dir / fname
    # Write the JSON with the work_packet_path filled in to the actual write
    # location so the on-disk file is self-referential.
    payload = diagnostics.to_json_dict()
    payload["work_packet_path"] = str(out_path)
    out_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return str(out_path)


def write_raw_response(
    raw_text: str,
    diagnostics_dir: Path | None,
    run_id: str,
    module_name: str,
    subunit_index: int | None,
) -> str | None:
    """Write the raw LLM response text under ``diagnostics_dir``.

    Returns the path, or ``None`` when ``diagnostics_dir`` is empty/None.
    Called only when raw response capture is explicitly enabled.
    """
    if diagnostics_dir is None or str(diagnostics_dir) == "":
        return None
    diag_dir = Path(diagnostics_dir)
    diag_dir.mkdir(parents=True, exist_ok=True)
    idx_part = f"{subunit_index:03d}" if subunit_index is not None else "na"
    safe_module = module_name.replace("/", "_").replace("\\", "_")
    fname = f"raw-{run_id}-{safe_module}-{idx_part}.txt"
    out_path = diag_dir / fname
    out_path.write_text(raw_text, encoding="utf-8")
    return str(out_path)
