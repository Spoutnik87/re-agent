"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def cmd_build(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    build_cfg = config.build
    llm_cfg = config.llm
    pipeline_cfg = config.pipeline

    persist = not getattr(args, "no_persist", False)

    # --no-persist is only valid with an explicit --phase transform.
    # With --phase analyze, --phase assemble, or no --phase (which runs
    # all phases including analyze/assemble), --no-persist would silently
    # skip writes — this is a user error.
    phase = getattr(args, "phase", None)
    if not persist and phase != "transform":
        phase_label = phase if phase else "(all phases)"
        print(
            f"Error: --no-persist is only valid with --phase transform " f"(got --phase {phase_label})",
            file=sys.stderr,
        )
        return 2

    state = PipelineState(pipeline_cfg.state_file) if persist else None

    phases = [phase] if phase else ["analyze", "transform", "assemble"]

    from re_agent.build.analyze.clusterer import cluster
    from re_agent.build.analyze.graph_builder import build_graph
    from re_agent.build.analyze.indexer import index_modules
    from re_agent.build.assemble.tree_builder import build_tree
    from re_agent.build.transform.module_processor import process_modules

    has_incomplete = False
    contract_failed = False

    try:
        if "analyze" in phases:
            print("=== Phase 1/3: Analyze (call graph + clustering) ===")
            graph = build_graph(build_cfg)
            modules = cluster(graph, build_cfg)
            index_modules(modules, build_cfg)

            from re_agent.build.analyze.decls_generator import write_decls_header

            decls_path = write_decls_header(config)
            if decls_path is not None:
                print(f"Wrote declarations header: {decls_path}")
            mc = modules["metadata"]["module_count"]
            oc = modules["metadata"]["orphan_count"]
            print(f"Analyze complete: {mc} modules, {oc} orphans")
            if persist and state is not None:
                state.update_build("in_progress", phase="analyze", modules_completed=[])

        if "transform" in phases:
            print("=== Phase 2/3: Transform (LLM code refinement) ===")
            summary = process_modules(
                build_cfg,
                llm_cfg,
                module=getattr(args, "module", None),
                subunit=getattr(args, "subunit", None),
                max_subunits=getattr(args, "max_subunits", None),
                run_id=getattr(args, "run_id", "") or "",
                persist=persist,
            )
            if persist and state is not None:
                state.update_build("in_progress", phase="transform", modules_completed=[])

            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            incomplete = summary.get("incomplete", 0)
            contract_failed = summary.get("contract_failed", False)

            if total > 0 and passed > 0:
                parts = [f"{passed}/{total} functions compiled"]
                if incomplete:
                    parts.append(f"{incomplete} incomplete targets")
                    has_incomplete = True
                print(f"Transform complete: {', '.join(parts)}")
            elif total > 0 and passed == 0:
                if contract_failed:
                    msg = (
                        f"CONTRACT FAILED — {incomplete}/{total} functions have INCOMPLETE_TARGETS "
                        "(TARGET contract required but recovery exhausted)"
                    )
                    print(f"Transform rejected: {msg}")
                    has_incomplete = True
                elif incomplete:
                    msg = f"{incomplete}/{total} functions have INCOMPLETE_TARGETS — recovery exhausted"
                    print(f"Transform complete: {msg}")
                    has_incomplete = True
                else:
                    print(f"Transform complete: 0/{total} functions compiled — see report for details")
            else:
                print("Transform complete: no functions processed")

        # P0-4: Any contract failure or hard reject → exit 2 before assemble.
        # Never print "Build complete" in this case.
        if "assemble" in phases:
            if contract_failed or has_incomplete:
                print("Skipping assemble: contract failed (TARGET violations).")
            else:
                print("=== Phase 3/3: Assemble (project tree) ===")
                build_tree(build_cfg)
                if persist and state is not None:
                    state.update_build("completed")
    except Exception:
        if persist and state is not None:
            state.update_build("failed")
            state.flush()
        raise

    if persist and state is not None:
        state.flush()

    if contract_failed or has_incomplete:
        code = 2 if contract_failed else 1
        label = "CONTRACT FAILED" if contract_failed else "INCOMPLETE"
        print(f"Build {label}: TARGET requirements not met.")
        return code
    print("Build complete.")
    return 0
