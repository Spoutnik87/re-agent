"""CLI entry point for re-agent."""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re-agent",
        description="Autonomous reverse engineering and code reconstruction agent",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 2.0.0")
    parser.add_argument("--config", default="re-agent.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    init_p = sub.add_parser("init", help="Initialize re-agent.yaml config file")
    init_p.add_argument("--abi-manifest", required=True, help="Path to a valid ABI manifest JSON file (required)")
    init_p.add_argument("--profile", default=None, help=argparse.SUPPRESS)

    project_p = sub.add_parser("project", help="Provision and export generic project snapshots")
    project_sub = project_p.add_subparsers(dest="project_command", required=True)
    provision_p = project_sub.add_parser("provision", help="Create an owned project snapshot")
    provision_p.add_argument("--binary")
    provision_p.add_argument("--analysis")
    provision_p.add_argument("--output")
    provision_p.add_argument("--name")
    export_p = project_sub.add_parser("export", help="Export a validated analysis snapshot")
    export_p.add_argument("--backend", choices=["offline-export", "ghidra"], required=True)
    export_p.add_argument("--analysis")
    export_p.add_argument("--binary")
    export_p.add_argument("--output", required=True)
    export_p.add_argument("--command", nargs=argparse.REMAINDER)
    export_p.add_argument("--timeout", type=int, default=300)

    toolchain_p = sub.add_parser("toolchain", help="Manage generic toolchain profiles")
    toolchain_sub = toolchain_p.add_subparsers(dest="toolchain_command", required=True)
    toolchain_sub.add_parser("schema", help="Print the strict profile JSON schema")
    activate_p = toolchain_sub.add_parser("activate", help="Activate an immutable profile")
    activate_p.add_argument("--project-root")
    activate_p.add_argument("--profile")
    status_p = toolchain_sub.add_parser("status", help="Show the active profile")
    status_p.add_argument("--project-root")

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
        "--phase",
        choices=["transform", "link", "package", "verify-recipe"],
        default=None,
        help="Run a single build phase (project mode also accepts link/package/verify-recipe)",
    )
    build_p.add_argument("--run-id", default=None, help="Run identifier for diagnostics/evidence paths")
    build_p.add_argument("--project-root", required=True, help="Owned generic project root")
    build_p.add_argument("--profile", default=None, help="Transient generic toolchain profile (project mode only)")
    build_p.add_argument(
        "--verify-recipe", action="store_true", help="Validate the project build recipe without running it"
    )
    build_p.add_argument(
        "--allow-partial", action="store_true", help="Deprecated; project builds never publish partial results"
    )

    run_p = sub.add_parser("run", help="Verify or replay a completed project run")
    run_sub = run_p.add_subparsers(dest="run_command", required=True)
    for operation, help_text in (
        ("verify", "Verify recorded run identity, checkpoints, and transform evidence"),
        ("replay", "Replay recorded transforms offline without a live provider"),
    ):
        operation_p = run_sub.add_parser(operation, help=help_text)
        operation_p.add_argument("--project-root", required=True, help="Owned generic project root")
        operation_p.add_argument("--run-id", required=True, help="Existing project run identifier")
        operation_p.add_argument("--profile", default=None, help="Transient generic toolchain profile")
    # pipeline
    pipe_p = sub.add_parser("pipeline", help="Run full pipeline: reverse then build")
    pipe_p.add_argument("--address", help="Single function address (delegated to reverse)")
    pipe_p.add_argument("--class", dest="class_name", help="Class name (delegated to reverse)")
    pipe_p.add_argument("--max-functions", type=int, default=None, help="Max functions (delegated to reverse)")
    pipe_p.add_argument("--skip-reverse", action="store_true", help="Skip reverse phase, run build only")
    pipe_p.add_argument("--skip-build", action="store_true", help="Skip build phase, run reverse only")
    pipe_p.add_argument("--project-root", help="Owned generic project root for the build phase")
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

    promote_p = sub.add_parser("promote", help="Project-scoped Release 5 promotion")
    promote_sub = promote_p.add_subparsers(dest="promote_command", required=True)
    prove_p = promote_sub.add_parser("prove", help="Run a promotion proof")
    prove_p.add_argument("--project-root", required=True)
    prove_p.add_argument("--proof", choices=["abi", "differential"], required=True)
    target_group = prove_p.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--address")
    target_group.add_argument("--all", action="store_true")
    prove_p.add_argument("--build-id")
    prove_p.add_argument("--profile")
    prove_p.add_argument("--original-binary")
    prove_p.add_argument("--promotion-root", help="External immutable promotion store")

    promote_status = promote_sub.add_parser("status", help="Show promotion state")
    promote_status.add_argument("--project-root", required=True)
    promote_status.add_argument("--address")
    promote_status.add_argument("--build-id")
    promote_status.add_argument("--format", choices=["text", "json"], default="text")
    promote_status.add_argument("--promotion-root", help="External immutable promotion store")

    promote_project = promote_sub.add_parser("project", help="Atomically prove and promote the whole project")
    promote_project.add_argument("--project-root", required=True)
    promote_project.add_argument("--build-id")
    promote_project.add_argument("--profile")
    promote_project.add_argument("--original-binary", required=True)
    promote_project.add_argument("--promotion-root", help="External immutable promotion store")

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

    if args.command == "project":
        from re_agent.cli.cmd_project import cmd_project

        return cmd_project(args)

    if args.command == "toolchain":
        from re_agent.cli.cmd_toolchain import cmd_toolchain

        return cmd_toolchain(args)

    if args.command == "reverse":
        from re_agent.cli.cmd_reverse import cmd_reverse

        return cmd_reverse(args)

    if args.command == "build":
        from re_agent.cli.cmd_build import cmd_build

        return cmd_build(args)

    if args.command == "run":
        from re_agent.cli.cmd_run import cmd_run

        try:
            return cmd_run(args)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Run rejected: {exc}", file=sys.stderr)
            return 2

    if args.command == "pipeline":
        from re_agent.cli.cmd_pipeline import cmd_pipeline

        return cmd_pipeline(args)

    if args.command == "parity":
        from re_agent.cli.cmd_parity import cmd_parity

        return cmd_parity(args)

    if args.command == "status":
        from re_agent.cli.cmd_status import cmd_status

        return cmd_status(args)

    if args.command == "promote":
        from re_agent.cli.cmd_promote import cmd_promote

        return cmd_promote(args)

    parser.print_help()
    return 1
