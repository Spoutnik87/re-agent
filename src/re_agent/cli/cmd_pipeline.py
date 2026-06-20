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

        try:
            rev_code = cmd_reverse(args)
        except Exception:
            state.update_reverse("failed")
            state.flush()
            print("Reverse phase failed with exception. Pipeline stopped.", file=sys.stderr)
            raise
        if rev_code != 0:
            state.update_reverse("failed")
            state.flush()
            print("Reverse phase failed. Pipeline stopped.", file=sys.stderr)
            return rev_code

        state.update_reverse("completed")
        state.flush()
        print("Reverse phase complete.")

    if not build_ok and not args.skip_build:
        if args.skip_reverse and not state.is_reverse_completed():
            print("Warning: Skipping reverse phase but it was not completed.", file=sys.stderr)

        print("=== Pipeline: Phase 2 — Build ===")
        from re_agent.cli.cmd_build import cmd_build

        try:
            build_code = cmd_build(args)
        except Exception:
            state.update_build("failed")
            state.flush()
            print("Build phase failed with exception. Pipeline stopped.", file=sys.stderr)
            raise
        if build_code != 0:
            state.update_build("failed")
            state.flush()
            print("Build phase failed. Pipeline stopped.", file=sys.stderr)
            return build_code

        # cmd_build already wrote pipeline state to disk; reload to stay in sync
        state = PipelineState(config.pipeline.state_file)
        print("Build phase complete.")

    print("Pipeline completed successfully.")
    return 0
