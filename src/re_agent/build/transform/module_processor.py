from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from re_agent.build.analyze.decls_generator import strip_redundant_externs
from re_agent.build.state.cache import TransformCache
from re_agent.build.state.resume import load_state, save_state
from re_agent.build.transform.context_builder import build_context
from re_agent.build.transform.subunit_processor import (
    _render_system_prompt,
    process_subunit,
)
from re_agent.llm.registry import create_provider


def process_modules(cfg: Any, llm_cfg: Any) -> None:
    modules_path = Path(cfg.output.work_dir) / "modules.json"
    if not modules_path.exists():
        raise FileNotFoundError("modules.json not found. Run 'cr-agent analyze' first.")

    with open(modules_path, encoding="utf-8") as f:
        modules_data = json.load(f)

    llm = create_provider(llm_cfg)

    cache = None
    if cfg.optimization.cache_enabled:
        cache = TransformCache(cfg.optimization.cache_path)

    temp_dir = Path(cfg.output.work_dir) / "temp_transformed"
    temp_dir.mkdir(parents=True, exist_ok=True)

    completed_modules: list[str] = []
    resume_module: str | None = None
    resume_subunit: int = 0
    state_path: Path | None = None
    if cfg.resume.enabled:
        state_path = Path(cfg.resume.state_path) if cfg.resume.state_path else None
        state = load_state(state_path)
        completed_modules = state.get("completed_modules", [])
        resume_module = state.get("current_module")
        resume_subunit = state.get("current_subunit", 0)

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

        # Build source map once per module (avoids O(N^2) glob scans)
        source_map: dict[str, str] = {}
        decompiled_dir = Path(cfg.input.decompiled_dir)
        decls_path = getattr(cfg.output, "decls_header", None)
        for addr in module_functions:
            candidates = list(decompiled_dir.glob(f"{addr}*.cpp"))
            if candidates:
                src = candidates[0].read_text(encoding="utf-8", errors="replace")
                if decls_path:
                    src = strip_redundant_externs(src, decls_path)
                source_map[addr] = src

        start_subunit = resume_subunit if (resume_module == module_name) else 0
        resume_module = None  # only apply to the first matching module
        for sub_idx, subunit in enumerate(sub_units):
            if sub_idx < start_subunit:
                continue
            save_state(
                {
                    "completed_modules": completed_modules,
                    "current_module": module_name,
                    "current_subunit": sub_idx,
                    "phase": "transform",
                },
                state_path,
            )

            # Compute prompt_hash from system prompt to detect prompt edits
            system_prompt = _render_system_prompt(cfg, module_name)
            prompt_hash = TransformCache.hash_prompt(system_prompt)

            context = build_context(
                subunit,
                module_functions,
                decompiled_dir,
                cfg.optimization.context_window,
                cache,
                prompt_hash=prompt_hash,
                model=llm_cfg.model,
                source_map=source_map,
            )

            results = process_subunit(context, module_name, llm, cfg, cache)

            for r in results:
                if r.get("compiles") and r.get("files"):
                    for f in r["files"]:
                        filename = Path(f["path"]).name
                        output_path = module_dir / filename
                        output_path.write_text(f["content"], encoding="utf-8")

                if cache is not None:
                    addr = r["function"]
                    source_for_addr = ""
                    for func in context.get("functions_to_transform", []):
                        if func["address"] == addr:
                            source_for_addr = func["code"]
                            break
                    combined_output = "\n".join(f["content"] for f in r.get("files", []))
                    cache.set(
                        addr,
                        source_for_addr,
                        combined_output,
                        r.get("compiles", False),
                        0,
                        prompt_hash=prompt_hash,
                        model=llm_cfg.model,
                    )

            all_results.extend(results)

        # Per-module compile check: catch cross-file link errors
        if getattr(cfg.validation, "compile_per_module", False):
            module_cpp_files = list(module_dir.glob("*.cpp"))
            if module_cpp_files:
                from re_agent.build.validate.compiler import compile_module_check

                mod_ok, mod_err = compile_module_check(module_cpp_files, cfg)
                if not mod_ok:
                    import logging

                    _log = logging.getLogger(__name__)
                    _log.warning("Module %s link errors:\n%s", module_name, mod_err)

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

    with open(Path(cfg.output.work_dir) / "cr-agent-report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
