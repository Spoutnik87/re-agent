"""re-agent status command — show pipeline or phase progress."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.reverse.core.session import Session
from re_agent.reverse.reports.tracker import ProgressTracker
from re_agent.state.pipeline_state import PipelineState


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))

    if args.phase == "reverse":
        session = Session(config.reverse.output.session_file)
        tracker = ProgressTracker(session)
        if args.format == "json":
            data = tracker.get_function_table(args.class_name) if args.class_name else session.get_summary()
            print(json.dumps(data, indent=2))
            return 0
        if args.format == "markdown":
            rows = tracker.get_function_table(args.class_name)
            if not rows:
                print("No functions recorded yet.")
                return 0
            print("| Address | Class | Function | Status | Rounds | Time |")
            print("|---------|-------|----------|--------|--------|------|")
            for r in rows:
                a, c, f, s, rd, ts = (
                    r["address"],
                    r["class"],
                    r["function"],
                    r["status"],
                    r["rounds"],
                    r["timestamp"],
                )
                print(f"| {a} | {c} | {f} | {s} | {rd} | {ts} |")
            return 0
        if args.class_name:
            print(tracker.print_class_summary(args.class_name))
            print()
            rows = tracker.get_function_table(args.class_name)
            for r in rows:
                print(f"  {r['address']}  {r['function']:40s}  {r['status']:4s}  ({r['rounds']} rounds)")
        else:
            print(tracker.print_summary())
        return 0

    if args.phase == "build":
        from re_agent.build.state.resume import load_state

        state = load_state()
        if not state:
            print("No build state found.")
            return 0
        print(f"Build phase: {state.get('phase', 'unknown')}")
        print(f"Completed modules: {state.get('completed_modules', [])}")
        print(f"Current module: {state.get('current_module', 'none')}")
        print(f"Current sub-unit: {state.get('current_subunit', 0)}")
        return 0

    state = PipelineState(config.pipeline.state_file)
    summary = state.summary()
    if args.format == "json":
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Pipeline version: {summary.get('pipeline_version', '?')}")
    phases = summary.get("phases", {})
    rev = phases.get("reverse", {})
    build = phases.get("build", {})
    print(f"  Reverse: {rev.get('status', 'unknown')}")
    print(f"  Build:   {build.get('status', 'unknown')}")
    if summary.get("last_pipeline_run"):
        print(f"  Last run: {summary['last_pipeline_run']}")
    return 0
