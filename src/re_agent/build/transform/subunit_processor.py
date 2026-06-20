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

    Strategy: if there's only one function in the subunit, all files belong
    to it. Otherwise, try to match by function name in the file content or path.
    """
    if total_func_count == 1 and parsed_files:
        return parsed_files
    func_name = func.get("name", "")
    if func_name:
        matched = [
            f
            for f in parsed_files
            if func_name.lower() in f["content"].lower() or func_name.lower() in f["path"].lower()
        ]
        if matched:
            return matched
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


def _render_task_prompt(module_name: str, subunit_context: dict[str, Any]) -> str:
    template_path = _PROMPT_DIR / "transform_task.md"
    template = Template(template_path.read_text(encoding="utf-8"))
    _prompt: str = template.render(
        module_name=module_name,
        neighbours=subunit_context.get("neighbour_context", []),
        functions=subunit_context.get("functions_to_transform", []),
    )
    return _prompt


def _build_retry_prompt(output_file: str, err: str) -> str:
    return (
        f"The following code failed to compile with GCC:\n\n"
        f"```cpp\n{output_file}\n```\n\n"
        f"Compiler error:\n{err}\n\n"
        f"Fix the code and output it with the same // FILE: marker."
    )


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
    user = _render_task_prompt(module_name, subunit_context)
    messages = [Message(role="system", content=system), Message(role="user", content=user)]
    response = llm.send(messages)

    parsed_files = _parse_llm_response(response)
    max_retries = getattr(cfg.validation, "max_compile_retries", 0)
    total_funcs = len(functions_to_transform)

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
        elif max_retries > 0:
            retry_files = _retry_with_context(
                func_files,
                err,
                func,
                system,
                user,
                llm,
                max_retries,
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


def _retry_with_context(
    func_files: list[dict[str, str]],
    err: str,
    func: dict[str, Any],
    system: str,
    user: str,
    llm: LLMProvider,
    max_retries: int,
    cfg: Any,
) -> list[dict[str, str]] | None:
    """Retry fixing compile errors with conversation continuity.

    Sends original system + original task + model's prior output + error.
    Uses multi-turn conversation; returns updated files or None.
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
            Message(role="user", content=user),
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
            return current_files
    return current_files if current_files != func_files else None
