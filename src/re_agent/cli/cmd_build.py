"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def cmd_build(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    build_cfg = config.build
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
            mc = modules["metadata"]["module_count"]
            oc = modules["metadata"]["orphan_count"]
            print(f"Analyze complete: {mc} modules, {oc} orphans")
            state.update_build("in_progress", phase="analyze", modules_completed=[])

        if "transform" in phases:
            print("=== Phase 2/3: Transform (LLM code refinement) ===")
            process_modules(build_cfg)
            state.update_build("in_progress", phase="transform", modules_completed=[])

        if "assemble" in phases:
            print("=== Phase 3/3: Assemble (project tree) ===")
            build_tree(build_cfg)
            state.update_build("completed")
    except Exception:
        state.update_build("failed")
        raise

    print("Build complete.")
    return 0
