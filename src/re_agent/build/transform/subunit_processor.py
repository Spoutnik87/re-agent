from __future__ import annotations

import logging
import re
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
from re_agent.llm.protocol import LLMProvider, Message, get_usage

log = logging.getLogger(__name__)

_FILE_MARKER_RE = re.compile(r"^// FILE: (.+)$", re.MULTILINE)

# Standalone Markdown code fence delimiter:
# optional whitespace + at least three backticks + optional non-whitespace
# info string/tag + optional whitespace, and nothing else on the line.
_FENCE_LINE_RE = re.compile(r"^\s*`{3,}[^\s`]*\s*$", re.MULTILINE)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


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


def _parse_llm_response(response: str) -> list[dict[str, str]]:
    """Parse all // FILE: blocks from the LLM response.

    Returns a list of {'path': str, 'content': str} dicts, one per file.
    Periphery Markdown fence delimiters are stripped from each block.
    """
    parts = _FILE_MARKER_RE.split(response)
    files: list[dict[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        filepath = parts[i].strip()
        content = _strip_markdown_fence_delimiters(parts[i + 1])
        if filepath and content:
            files.append({"path": filepath, "content": content})
    return files


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


def _match_files_to_function_with_strategy(
    parsed_files: list[dict[str, str]],
    func: dict[str, Any],
    total_func_count: int,
) -> tuple[list[dict[str, str]], str]:
    """Match parsed LLM output files to a specific function and report the strategy.

    Returns ``(matched_files, strategy_name)`` where ``strategy_name`` is one of:
    ``single_function``, ``by_name``, ``by_address``, ``single_file_fallback``,
    ``none``.

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
    4. Last-resort fallback: if exactly one file was parsed, assign it to every
       function so we never silently produce NO_OUTPUT when the LLM did emit
       output.

    See docs/_diagnostic_no_output.md (project-specific) for the root-cause analysis
    that motivated the address-based fallback.
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

    # 3. Match by address (the stable identifier present in both the context
    #    dict and the LLM prompt/output). Case-insensitive: addresses may appear
    #    upper- or lower-case in the LLM output.
    addr = func.get("address", "")
    if addr:
        addr_lower = addr.lower()
        matched = [f for f in parsed_files if addr_lower in f["content"].lower() or addr_lower in f["path"].lower()]
        if matched:
            return matched, "by_address"

    # 4. Last-resort fallback: a single parsed file belongs to every function.
    #    Without this, a subunit of N functions where the LLM emitted only one
    #    // FILE: block would yield N-1 NO_OUTPUT verdicts.
    if len(parsed_files) == 1:
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
    (case-insensitive) in the file path or content of parsed file ``i``.
    ``candidate_has_name[i]`` is ``True`` when the target name appears
    (case-insensitive) and ``func_name`` is non-empty; ``False`` otherwise.
    """
    paths: list[str] = []
    has_addr: list[bool] = []
    has_name: list[bool] = []
    addr_lower = func_addr.lower()
    name_lower = func_name.lower() if func_name else ""
    for f in parsed_files:
        path = f["path"]
        content_lower = f["content"].lower()
        path_lower = path.lower()
        paths.append(path)
        has_addr.append(addr_lower in content_lower or addr_lower in path_lower)
        has_name.append(bool(name_lower and (name_lower in content_lower or name_lower in path_lower)))
    return tuple(paths), tuple(has_addr), tuple(has_name)


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
) -> list[dict[str, Any]]:
    functions_to_transform = subunit_context.get("functions_to_transform", [])
    if not functions_to_transform:
        return []

    system = _render_system_prompt(cfg, module_name)
    repair_system = _render_repair_prompt(cfg, module_name)
    user = _render_task_prompt(module_name, subunit_context)
    messages = [Message(role="system", content=system), Message(role="user", content=user)]
    response = llm.send(messages)

    log.info("LLM response (first 500 chars): %s", response[:500])

    # Raw response capture is config-gated (default disabled). The legacy
    # unconditional write to .omo/evidence/llm-raw-subunit.txt is removed.
    diag_dir = _opt_diagnostics_dir(cfg)
    raw_capture = _opt_raw_response_capture(cfg)
    run_id = getattr(subunit_context, "run_id", "") or subunit_context.get("run_id", "") or ""
    subunit_index = subunit_context.get("subunit_index")
    raw_response_path: str | None = None
    if raw_capture and diag_dir is not None:
        raw_response_path = write_raw_response(response, diag_dir, run_id, module_name, subunit_index)

    parsed_files = _parse_llm_response(response)
    initial_marker_count = len(parsed_files)
    marker_count = initial_marker_count
    max_retries = getattr(cfg.validation, "max_compile_retries", 0)
    total_funcs = len(functions_to_transform)
    compile_enabled = _compile_per_function_enabled(cfg)

    # First pass: compile-check all functions, collect failures
    failed_funcs: list[dict[str, Any]] = []
    for func in functions_to_transform:
        func_files, _strategy = _match_files_to_function_with_strategy(parsed_files, func, total_funcs)
        if not func_files:
            continue
        if not compile_enabled:
            continue
        cpp_file = next((f for f in func_files if f["path"].endswith(".cpp")), func_files[0])
        compiles, err = _compile_generated_cpp(func_files, cpp_file, cfg)
        if not compiles:
            failed_funcs.append({"func": func, "files": func_files, "err": err})

    # Subunit-level retry: re-send whole subunit with all errors in one call
    retry_marker_count = 0
    if failed_funcs and max_retries > 0:
        error_annotations = "\n\n".join(
            f"Function {f['func']['address']} failed to compile:\n{f['err']}" for f in failed_funcs
        )
        retry_prompt = (
            "The following functions failed to compile. Fix ALL of them and "
            "re-output with // FILE: markers.\n\n" + error_annotations
        )
        retry_messages = [
            Message(role="system", content=repair_system),
            Message(role="user", content=user),
            Message(role="assistant", content=response),
            Message(role="user", content=retry_prompt),
        ]
        retry_response = llm.send(retry_messages)
        retry_files = _parse_llm_response(retry_response)
        retry_marker_count = len(retry_files)
        if retry_files:
            parsed_files = _merge_retry_files(parsed_files, retry_files)
            marker_count = len(parsed_files)
            max_retries -= 1
    effective_marker_count = marker_count

    # Each still-failing function gets its OWN retry budget. (Previously a single
    # shared counter starved every function after the first one or two.)
    per_func_retries = max_retries

    # Per-function result building (with per-function retry for still-failing)
    results: list[dict[str, Any]] = []
    function_verdicts: list[FunctionVerdict] = []
    per_func_strategies: list[str] = []
    total_files_written = 0
    for func in functions_to_transform:
        addr = func["address"]
        func_name = func.get("name", "")
        func_files, strategy = _match_files_to_function_with_strategy(parsed_files, func, total_funcs)
        per_func_strategies.append(strategy)

        candidate_paths, candidate_has_address, candidate_has_name = _build_candidate_analysis(
            parsed_files, addr, func_name
        )

        if not func_files:
            function_verdicts.append(
                FunctionVerdict(
                    address=addr,
                    verdict="NO_OUTPUT",
                    compiles=False,
                    files_matched=0,
                    match_strategy=strategy,
                    candidate_paths=candidate_paths,
                    candidate_has_address=candidate_has_address,
                    candidate_has_name=candidate_has_name,
                )
            )
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": [],
                    "compiles": False,
                    "verdict": "NO_OUTPUT",
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
            retry_files = _retry_with_conversation(
                func_files,
                err,
                func,
                repair_system,
                user,
                llm,
                per_func_retries,
                cfg,
            )
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
    work_packet_path = write_diagnostics(diagnostics, diag_dir)

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
) -> list[dict[str, str]]:
    """Retry fixing compile errors using multi-turn conversation.

    Sends: original system + original task + model's prior output + error.
    Iterates up to max_retries times. Returns the last file set (fixed or not).
    """
    current_files = func_files
    cpp_file = next((f for f in current_files if f["path"].endswith(".cpp")), current_files[0])

    for _ in range(max_retries):
        prior_output = "\n\n".join(f"// FILE: {f['path']}\n{f['content']}" for f in current_files)
        retry_prompt = (
            f"The following code failed to compile with GCC:\n\n"
            f"```cpp\n{cpp_file['content']}\n```\n\n"
            f"Compiler error:\n{err}\n\n"
            f"Fix the code and output it with the same // FILE: markers."
        )
        retry_messages = [
            Message(role="system", content=system),
            Message(role="user", content=original_user),
            Message(role="assistant", content=prior_output),
            Message(role="user", content=retry_prompt),
        ]
        retry_response = llm.send(retry_messages)
        new_files = _parse_llm_response(retry_response)
        if new_files:
            current_files = new_files
            cpp_file = next((f for f in current_files if f["path"].endswith(".cpp")), current_files[0])
        new_compiles, err = _compile_generated_cpp(current_files, cpp_file, cfg)
        if new_compiles:
            break
    return current_files
