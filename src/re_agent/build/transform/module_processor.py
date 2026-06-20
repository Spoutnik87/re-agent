from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from re_agent.build.state.cache import TransformCache
from re_agent.build.state.resume import load_state, save_state
from re_agent.build.transform.context_builder import build_context
from re_agent.build.transform.subunit_processor import (
    _render_system_prompt,
    process_subunit,
)
from re_agent.llm.registry import create_provider

_FILE_MARKER_RE = re.compile(r"^// FILE: (.+)$", re.MULTILINE)


def _parse_filename_from_output(output_file: str, fallback: str) -> str:
    m = _FILE_MARKER_RE.search(output_file)
    if m:
        return m.group(1).strip()
    return fallback


def process_modules(cfg: Any) -> None:
    modules_path = Path("modules.json")
    if not modules_path.exists():
        raise FileNotFoundError("modules.json not found. Run 'cr-agent analyze' first.")

    with open(modules_path, encoding="utf-8") as f:
        modules_data = json.load(f)

    llm = create_provider(cfg.llm)

    cache = None
    if cfg.optimization.cache_enabled:
        cache = TransformCache(cfg.optimization.cache_path)

    temp_dir = Path("temp_transformed")
    temp_dir.mkdir(parents=True, exist_ok=True)

    completed_modules: list[str] = []
    if cfg.resume.enabled:
        state = load_state()
        completed_modules = state.get("completed_modules", [])

    all_results: list[dict[str, Any]] = []

    for module_name, module_info in modules_data.get("modules", {}).items():
        if module_name in completed_modules:
            continue

        module_dir = temp_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)

        module_functions = module_info.get("functions", [])
        sub_units = module_info.get("sub_units", [])
        if not sub_units:
            sub_units = [module_functions]

        for sub_idx, subunit in enumerate(sub_units):
            save_state(
                {
                    "completed_modules": completed_modules,
                    "current_module": module_name,
                    "current_subunit": sub_idx,
                    "phase": "transform",
                }
            )

            # Compute prompt_hash from system prompt to detect prompt edits
            system_prompt = _render_system_prompt(cfg, module_name)
            prompt_hash = TransformCache.hash_prompt(system_prompt)

            context = build_context(
                subunit,
                module_functions,
                Path(cfg.input.decompiled_dir),
                cfg.optimization.context_window,
                cache,
                prompt_hash=prompt_hash,
                model=cfg.llm.model,
            )

            results = process_subunit(context, module_name, llm, cfg, cache)

            for r in results:
                if r.get("compiles") and r.get("output_file"):
                    filename = _parse_filename_from_output(r["output_file"], r["function"])
                    output_path = module_dir / f"{filename}.cpp"
                    output_path.write_text(r["output_file"], encoding="utf-8")

                if cache is not None:
                    # Look up the real decompiled source for this address
                    addr = r["function"]
                    source_for_addr = ""
                    for func in context.get("functions_to_transform", []):
                        if func["address"] == addr:
                            source_for_addr = func["code"]
                            break
                    cache.set(
                        addr,
                        source_for_addr,
                        r.get("output_file", ""),
                        r.get("compiles", False),
                        0,
                        prompt_hash=prompt_hash,
                        model=cfg.llm.model,
                    )

            all_results.extend(results)

        completed_modules.append(module_name)

    total = len(all_results)
    passed = sum(1 for r in all_results if r.get("compiles"))
    failed = total - passed
    total_tokens = llm.total_prompt_tokens + llm.total_completion_tokens

    report = {
        "results": all_results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "total_tokens": total_tokens,
        },
    }

    with open("cr-agent-report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
