from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from jinja2 import Template

from re_agent.build.validate.compiler import compile_check
from re_agent.llm.protocol import LLMProvider, Message

log = logging.getLogger(__name__)

_FILE_MARKER_RE = re.compile(r"^// FILE: (.+)$", re.MULTILINE)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _parse_llm_response(response: str) -> list[dict[str, str]]:
    """Parse all // FILE: blocks from the LLM response.

    Returns a list of {'path': str, 'content': str} dicts, one per file.
    """
    parts = _FILE_MARKER_RE.split(response)
    files: list[dict[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        filepath = parts[i].strip()
        content = parts[i + 1].strip()
        if filepath and content:
            files.append({"path": filepath, "content": content})
    return files


def _match_files_to_function(
    parsed_files: list[dict[str, str]],
    func: dict[str, Any],
    total_func_count: int,
) -> list[dict[str, str]]:
    """Match parsed LLM output files to a specific function.

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

    See docs/_diagnostic_no_output.md for the root-cause analysis
    that motivated the address-based fallback.
    """
    if total_func_count == 1 and parsed_files:
        return parsed_files

    # 2. Match by name (preserved for backwards compatibility).
    func_name = func.get("name", "")
    if func_name:
        matched = [
            f
            for f in parsed_files
            if func_name.lower() in f["content"].lower() or func_name.lower() in f["path"].lower()
        ]
        if matched:
            return matched

    # 3. Match by address (the stable identifier present in both the context
    #    dict and the LLM prompt/output). Case-insensitive: addresses may appear
    #    upper- or lower-case in the LLM output.
    addr = func.get("address", "")
    if addr:
        addr_lower = addr.lower()
        matched = [f for f in parsed_files if addr_lower in f["content"].lower() or addr_lower in f["path"].lower()]
        if matched:
            return matched

    # 4. Last-resort fallback: a single parsed file belongs to every function.
    #    Without this, a subunit of N functions where the LLM emitted only one
    #    // FILE: block would yield N-1 NO_OUTPUT verdicts.
    if len(parsed_files) == 1:
        return parsed_files

    return []


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

    parsed_files = _parse_llm_response(response)
    max_retries = getattr(cfg.validation, "max_compile_retries", 0)
    total_funcs = len(functions_to_transform)

    # First pass: compile-check all functions, collect failures
    failed_funcs: list[dict[str, Any]] = []
    for func in functions_to_transform:
        func_files = _match_files_to_function(parsed_files, func, total_funcs)
        if not func_files:
            continue
        cpp_file = next((f for f in func_files if f["path"].endswith(".cpp")), func_files[0])
        compiles, err = compile_check(cpp_file["content"], cfg)
        if not compiles:
            failed_funcs.append({"func": func, "files": func_files, "err": err})

    # Subunit-level retry: re-send whole subunit with all errors in one call
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
        if retry_files:
            parsed_files = retry_files
            max_retries -= 1

    # Each still-failing function gets its OWN retry budget. (Previously a single
    # shared counter starved every function after the first one or two.)
    per_func_retries = max_retries

    # Per-function result building (with per-function retry for still-failing)
    results: list[dict[str, Any]] = []
    for func in functions_to_transform:
        addr = func["address"]
        func_files = _match_files_to_function(parsed_files, func, total_funcs)

        if not func_files:
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
        compiles, err = compile_check(cpp_file["content"], cfg)
        if compiles:
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
            retry_compiles, _ = compile_check(cpp_file["content"], cfg)
            if retry_compiles:
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
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "files": func_files,
                    "compiles": False,
                    "verdict": "FAIL_NO_RETRY",
                }
            )

    return results


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
        new_compiles, err = compile_check(cpp_file["content"], cfg)
        if new_compiles:
            break
    return current_files
