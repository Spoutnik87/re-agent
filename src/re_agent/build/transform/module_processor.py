from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from re_agent.build.analyze.decls_generator import strip_redundant_externs
from re_agent.build.state.cache import TransformCache
from re_agent.build.state.resume import load_state, save_state
from re_agent.build.transform.context_builder import build_context
from re_agent.build.transform.subunit_processor import (
    TransformBudget,
    _no_persist_json_output,
    _render_system_prompt,
    process_subunit,
)
from re_agent.llm.protocol import get_usage
from re_agent.llm.registry import create_provider


def process_modules(
    cfg: Any,
    llm_cfg: Any,
    module: str | None = None,
    subunit: int | None = None,
    max_subunits: int | None = None,
    run_id: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    """Run the transform phase over all (or selected) modules.

    Args:
        cfg: Build configuration namespace.
        llm_cfg: LLM configuration namespace.
        module: If set, restrict transform to only this module name.
        subunit: Start at this subunit index (applies to ``--module`` target).
        max_subunits: Process at most this many subunits, then stop.
        run_id: Run identifier for diagnostics/evidence paths.
        persist: If True (default), write generated files, report, state, and
            cache to disk. If False, skip all disk writes (dry-run mode —
            results remain in memory / stdout only).
    """
    modules_path = Path(cfg.output.work_dir) / "modules.json"
    if not modules_path.exists():
        raise FileNotFoundError("modules.json not found. Run 'cr-agent analyze' first.")

    with open(modules_path, encoding="utf-8") as f:
        modules_data = json.load(f)

    llm = create_provider(llm_cfg)

    # Snapshot usage at the start of the run for accurate no-persist delta.
    start_usage = get_usage(llm)

    # Global per-invocation transform budget (shared across ALL subunits).
    # Use getattr for backward compat with test configs that lack these fields.
    budget = TransformBudget(
        calls_remaining=getattr(cfg.optimization, "max_llm_calls_per_run", 8),
        tokens_remaining=getattr(cfg.optimization, "max_llm_tokens_per_run", 150000),
        compile_retry_calls_remaining=getattr(cfg.optimization, "max_compile_retry_calls_per_run", 3),
    )

    cache = None
    # --no-persist: never create a cache — it would be written to disk and
    # also used to skip functions in build_context (which we must not do).
    if persist and cfg.optimization.cache_enabled:
        cache = TransformCache(cfg.optimization.cache_path)

    temp_dir: Path | None = None
    if persist:
        temp_dir = Path(cfg.output.work_dir) / "temp_transformed"
        temp_dir.mkdir(parents=True, exist_ok=True)

    completed_modules: list[str] = []
    resume_module: str | None = None
    resume_subunit: int = 0
    state_path: Path | None = None
    # --no-persist: never load resume state — we must NOT skip modules based
    # on a previous run's state, and we must NOT write state at all.
    if persist and cfg.resume.enabled:
        state_path = Path(cfg.resume.state_path) if cfg.resume.state_path else None
        state = load_state(state_path)
        completed_modules = state.get("completed_modules", [])
        resume_module = state.get("current_module")
        resume_subunit = state.get("current_subunit", 0)

    all_results: list[dict[str, Any]] = []

    # Global subunit count across all modules for --max-subunits bound.
    # Must live outside the module loop so the cap applies across the
    # entire invocation, not per-module.
    subunit_count = 0

    for module_name, module_info in modules_data.get("modules", {}).items():
        # --module filter: skip non-matching modules
        if module is not None and module_name != module:
            continue

        if module_name in completed_modules:
            continue

        if persist:
            assert temp_dir is not None
            module_dir = temp_dir / module_name
            module_dir.mkdir(parents=True, exist_ok=True)
        else:
            module_dir = None

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

        # P0-1: Determine start subunit and whether it came from resume.
        #   - Explicit ``--subunit`` (via ``--module``) is a manual override.
        #     Previous skipped subunits are NOT "already completed".
        #   - Resume ``current_subunit`` from state means previous subunits
        #     WERE completed (or were being processed before interrupt).
        #     They count toward "all subunits processed".
        #   - Without either, start from 0.
        is_resume = False
        if module is not None and module_name == module:
            # Explicit --module + --subunit: no resume counting
            start_subunit = subunit if subunit is not None else 0
            is_resume = False
        elif resume_module == module_name:
            # Resume from state: previous subunits count as completed
            start_subunit = resume_subunit
            is_resume = True
        else:
            start_subunit = 0
            is_resume = False
        resume_module = None  # only apply to the first matching module

        # P0-1: Track per-module results and subunit count independently so that
        # --max-subunits can stop mid-module without marking it as completed.
        module_results: list[dict[str, Any]] = []
        module_subunits_processed = 0
        for sub_idx, sub_unit in enumerate(sub_units):
            # --max-subunits bound: stop once processed enough
            if max_subunits is not None and subunit_count >= max_subunits:
                break
            if sub_idx < start_subunit:
                continue
            subunit_count += 1
            module_subunits_processed += 1
            if persist:
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
                sub_unit,
                module_functions,
                decompiled_dir,
                cfg.optimization.context_window,
                cache,
                prompt_hash=prompt_hash,
                model=llm_cfg.model,
                source_map=source_map,
            )

            # Propagate run_id to diagnostics via subunit context
            if run_id:
                context["run_id"] = run_id
            sub_results = process_subunit(context, module_name, llm, cfg, cache, persist=persist, budget=budget)

            for r in sub_results:
                if r.get("compiles") and r.get("files"):
                    for f in r["files"]:
                        if persist and module_dir is not None:
                            filename = Path(f["path"]).name
                            output_path = module_dir / filename
                            output_path.write_text(f["content"], encoding="utf-8")

                # Cache the result if caching is enabled and the result is
                # NOT a non-retryable verdict (NO_OUTPUT, INCOMPLETE_TARGETS,
                # BUDGET_EXCEEDED, PROVIDER_ERROR are unreliable or unrecoverable
                # and caching them would prevent retry).
                _no_cache_verdicts = frozenset({"NO_OUTPUT", "INCOMPLETE_TARGETS", "BUDGET_EXCEEDED", "PROVIDER_ERROR"})
                if cache is not None and persist and r.get("verdict") not in _no_cache_verdicts:
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

            module_results.extend(sub_results)

        all_results.extend(module_results)

        # P0-1: Only mark completed if ALL subunits of this module were processed
        # AND every result has an accepted verdict.
        # Accepted verdicts: PASS, PASS_RETRY, SKIPPED_COMPILE.
        # Any other verdict (NO_OUTPUT, INCOMPLETE_TARGETS, FAIL_NO_RETRY,
        # FAIL_AFTER_RETRY, etc.) blocks completion.
        # ``--max-subunits`` can stop mid-module; that module must NOT be completed.
        # ``--subunit`` explicit skip must NOT count skipped subunits as completed.
        # Only resume state counts previous subunits as already processed.
        accepted_verdicts = frozenset({"PASS", "PASS_RETRY", "SKIPPED_COMPILE"})
        # Only for resume: previous subunits count as "already processed".
        # For explicit --subunit, skipped subunits are NOT completed.
        already_processed = start_subunit if is_resume else 0
        total_processed = module_subunits_processed + already_processed
        all_subunits_processed = total_processed >= len(sub_units)
        module_has_failure = any(r.get("verdict") not in accepted_verdicts for r in module_results)
        if all_subunits_processed and not module_has_failure:
            completed_modules.append(module_name)
            # P0-2: Persist state immediately after completing a module so the
            # JSON file reflects the updated completed_modules list.
            if persist:
                save_state(
                    {
                        "completed_modules": completed_modules,
                        "current_module": None,
                        "current_subunit": 0,
                        "phase": "transform",
                    },
                    state_path,
                )

        # Per-module compile check: catch cross-file link errors
        if (
            all_subunits_processed
            and not module_has_failure
            and persist
            and getattr(cfg.validation, "compile_per_module", False)
            and module_dir is not None
        ):
            module_cpp_files = list(module_dir.glob("*.cpp"))
            if module_cpp_files:
                from re_agent.build.validate.compiler import compile_module_check

                mod_ok, mod_err = compile_module_check(module_cpp_files, cfg)
                if not mod_ok:
                    import logging

                    _log = logging.getLogger(__name__)
                    _log.warning("Module %s link errors:\n%s", module_name, mod_err)

    total = len(all_results)
    passed = sum(1 for r in all_results if r.get("compiles"))
    incomplete = sum(1 for r in all_results if r.get("verdict") == "INCOMPLETE_TARGETS")
    hard_rejects = sum(
        1
        for r in all_results
        if r.get("verdict") == "NO_OUTPUT" and r.get("diagnostic", {}).get("match_strategy") == "rejected_identity"
    )
    budget_exceeds = sum(1 for r in all_results if r.get("verdict") == "BUDGET_EXCEEDED")
    provider_errors = sum(1 for r in all_results if r.get("verdict") == "PROVIDER_ERROR")
    contract_failed = incomplete > 0 or hard_rejects > 0 or budget_exceeds > 0 or provider_errors > 0
    exit_code = 2 if budget_exceeds or contract_failed else (1 if incomplete > 0 or hard_rejects > 0 else 0)
    failed = total - passed - incomplete - hard_rejects - budget_exceeds - provider_errors
    total_tokens = llm.total_prompt_tokens + llm.total_completion_tokens

    summary_result: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "incomplete": incomplete,
        "hard_rejects": hard_rejects,
        "budget_exceeded": budget_exceeds,
        "provider_errors": provider_errors,
        "contract_failed": contract_failed,
        "total_tokens": total_tokens,
    }
    if budget:
        summary_result["budget"] = budget.to_dict()
    budget_obj = budget

    if not persist:
        # --no-persist: write ONLY the JSON to stdout, no human banner/footer
        # Use real start/end usage snapshots for accurate delta reporting.
        end_usage = get_usage(llm)
        _no_persist_json_output(
            all_results,
            budget_obj,
            start_usage,
            end_usage,
            exit_code=exit_code,
        )
        # Return summary without results for backward compat
        return summary_result

    # persist=True: write report file and return summary
    report = {
        "results": all_results,
        "summary": summary_result,
    }

    with open(Path(cfg.output.work_dir) / "cr-agent-report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return summary_result
