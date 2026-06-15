"""re-agent pipeline command — orchestrates reverse then build."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def cmd_pipeline(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    state = PipelineState(config.pipeline.state_file)

    reverse_ok = args.skip_reverse or state.is_reverse_completed()
    build_ok = args.skip_build or state.is_build_completed()

    if reverse_ok and build_ok:
        print("Pipeline already completed. Nothing to do.")
        if not args.skip_reverse and not args.skip_build:
            return 0

    if not reverse_ok and not args.skip_reverse:
        print("=== Pipeline: Phase 1 — Reverse engineering ===")
        from re_agent.cli.cmd_reverse import cmd_reverse

        rev_code = cmd_reverse(args)
        if rev_code != 0:
            state.update_reverse("failed")
            print("Reverse phase failed. Pipeline stopped.", file=sys.stderr)
            return rev_code

        state.update_reverse("completed", functions_decompiled=0)
        print("Reverse phase complete.")

    if not build_ok and not args.skip_build:
        if args.skip_reverse and not state.is_reverse_completed():
            print("Warning: Skipping reverse phase but it was not completed.", file=sys.stderr)

        print("=== Pipeline: Phase 2 — Build ===")
        from re_agent.cli.cmd_build import cmd_build

        build_code = cmd_build(args)
        if build_code != 0:
            state.update_build("failed")
            print("Build phase failed. Pipeline stopped.", file=sys.stderr)
            return build_code

        state.update_build("completed")
        print("Build phase complete.")

    print("Pipeline completed successfully.")
    return 0
