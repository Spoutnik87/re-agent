"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def cmd_build(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    build_cfg = config.build
    llm_cfg = config.llm
    pipeline_cfg = config.pipeline

    state = PipelineState(pipeline_cfg.state_file)

    phase = getattr(args, "phase", None)
    phases = [phase] if phase else ["analyze", "transform", "assemble"]

    from re_agent.build.analyze.clusterer import cluster
    from re_agent.build.analyze.graph_builder import build_graph
    from re_agent.build.analyze.indexer import index_modules
    from re_agent.build.assemble.tree_builder import build_tree
    from re_agent.build.transform.module_processor import process_modules

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
            )
            state.update_build("in_progress", phase="transform", modules_completed=[])

            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            if total > 0 and passed > 0:
                print(f"Transform complete: {passed}/{total} functions compiled successfully")
            elif total > 0 and passed == 0:
                print(f"Transform complete: 0/{total} functions compiled — see report for details")
            else:
                print("Transform complete: no functions processed")

        if "assemble" in phases:
            print("=== Phase 3/3: Assemble (project tree) ===")
            build_tree(build_cfg)
            state.update_build("completed")
    except Exception:
        state.update_build("failed")
        state.flush()
        raise

    state.flush()
    print("Build complete.")
    return 0
