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


def _parse_llm_response(response: str) -> dict[str, str]:
    parts = _FILE_MARKER_RE.split(response)
    result: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        filepath = parts[i].strip()
        content = parts[i + 1].strip()
        if filepath and content:
            result[filepath] = content
    return result


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

    parsed = _parse_llm_response(response)
    max_retries = getattr(cfg.validation, "max_compile_retries", 0)

    results: list[dict[str, Any]] = []
    for func in functions_to_transform:
        addr = func["address"]
        output_file = parsed.get(addr, "")

        if not output_file:
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "output_file": "",
                    "compiles": False,
                    "verdict": "NO_OUTPUT",
                }
            )
            continue

        compiles, err = compile_check(output_file, cfg)
        if compiles:
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "output_file": output_file,
                    "compiles": True,
                    "verdict": "PASS",
                }
            )
        elif max_retries > 0:
            retry_prompt = _build_retry_prompt(output_file, err)
            retry_messages = [Message(role="system", content=system), Message(role="user", content=retry_prompt)]
            retry_response = llm.send(retry_messages)
            retry_parsed = _parse_llm_response(retry_response)
            retry_output = retry_parsed.get(addr, output_file)

            retry_compiles, _ = compile_check(retry_output, cfg)
            if retry_compiles:
                results.append(
                    {
                        "function": addr,
                        "module": module_name,
                        "output_file": retry_output,
                        "compiles": True,
                        "verdict": "PASS_RETRY",
                    }
                )
            else:
                results.append(
                    {
                        "function": addr,
                        "module": module_name,
                        "output_file": retry_output,
                        "compiles": False,
                        "verdict": "FAIL_NO_RETRY",
                    }
                )
        else:
            results.append(
                {
                    "function": addr,
                    "module": module_name,
                    "output_file": output_file,
                    "compiles": False,
                    "verdict": "FAIL_NO_RETRY",
                }
            )

    return results
