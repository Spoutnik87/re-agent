"""CLI entry point for re-agent."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re-agent",
        description="Autonomous reverse engineering and code reconstruction agent",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.2.0")
    parser.add_argument("--config", default="re-agent.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    init_p = sub.add_parser("init", help="Initialize re-agent.yaml config file")
    init_p.add_argument("--profile", default=None, help="Use a built-in project profile template")

    # reverse
    rev_p = sub.add_parser("reverse", help="Reverse engineer functions (Phase 1)")
    rev_p.add_argument("--address", help="Single function address to reverse")
    rev_p.add_argument("--class", dest="class_name", help="Class name for class-level reversal")
    rev_p.add_argument("--max-functions", type=int, default=None, help="Max functions per class")
    rev_p.add_argument("--max-rounds", type=int, default=None, help="Max review rounds per function")
    rev_p.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    rev_p.add_argument("--skip-parity", action="store_true", help="Skip parity check after PASS")
    rev_p.add_argument("--no-optimize", action="store_true", help="Disable token optimization")

    # build
    build_p = sub.add_parser("build", help="Reconstruct project from flat .cpp files (Phase 2)")
    build_p.add_argument(
        "--phase", choices=["analyze", "transform", "assemble"], default=None, help="Run a single build phase"
    )

    # pipeline
    pipe_p = sub.add_parser("pipeline", help="Run full pipeline: reverse then build")
    pipe_p.add_argument("--address", help="Single function address (delegated to reverse)")
    pipe_p.add_argument("--class", dest="class_name", help="Class name (delegated to reverse)")
    pipe_p.add_argument("--max-functions", type=int, default=None, help="Max functions (delegated to reverse)")
    pipe_p.add_argument("--skip-reverse", action="store_true", help="Skip reverse phase, run build only")
    pipe_p.add_argument("--skip-build", action="store_true", help="Skip build phase, run reverse only")
    pipe_p.add_argument("--skip-parity", action="store_true", help="Skip parity check")

    # parity
    par_p = sub.add_parser("parity", help="Run parity checks on hooked functions")
    par_p.add_argument("--address", action="append", help="Specific address (repeatable)")
    par_p.add_argument("--filter", help="Regex filter on symbol/class")
    par_p.add_argument("--limit", type=int, help="Max functions to check")
    par_p.add_argument("--skip-ghidra", action="store_true", help="Source-only checks")
    par_p.add_argument("--strict-exit", action="store_true", help="Exit 1 on RED")
    par_p.add_argument("--output", help="Output JSON report path")

    # status
    stat_p = sub.add_parser("status", help="Show pipeline or phase progress")
    stat_p.add_argument("--phase", choices=["reverse", "build"], default=None, help="Show per-phase detail")
    stat_p.add_argument("--class", dest="class_name", help="Filter by class (reverse phase only)")
    stat_p.add_argument("--format", choices=["text", "json", "markdown"], default="text")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "init":
        from re_agent.cli.cmd_init import cmd_init

        return cmd_init(args)

    if args.command == "reverse":
        from re_agent.cli.cmd_reverse import cmd_reverse

        return cmd_reverse(args)

    if args.command == "build":
        from re_agent.cli.cmd_build import cmd_build

        return cmd_build(args)

    if args.command == "pipeline":
        from re_agent.cli.cmd_pipeline import cmd_pipeline

        return cmd_pipeline(args)

    if args.command == "parity":
        from re_agent.cli.cmd_parity import cmd_parity

        return cmd_parity(args)

    if args.command == "status":
        from re_agent.cli.cmd_status import cmd_status

        return cmd_status(args)

    parser.print_help()
    return 1
