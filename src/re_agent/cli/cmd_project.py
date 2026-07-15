"""Project provisioning and explicit analysis export commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.analysis.ghidra import GhidraLifecycleBackend, GhidraLifecycleError
from re_agent.analysis.offline_export import OfflineExportBackend, OfflineExportError
from re_agent.project.provision import ProvisionError, provision_project


def cmd_project(args: argparse.Namespace) -> int:
    if args.project_command == "provision":
        missing = [f"--{name}" for name in ("binary", "analysis", "output", "name") if not getattr(args, name, None)]
        if missing:
            print(f"ERROR: missing required arguments: {', '.join(missing)}", file=sys.stderr)
            return 1
        try:
            provision_project(
                binary=Path(args.binary), analysis=Path(args.analysis), output=Path(args.output), name=args.name
            )
        except ProvisionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print('INFO: no profile set — use "re-agent toolchain activate" before building')
        return 0
    if args.project_command == "export":
        output = Path(args.output)
        try:
            if args.backend == "offline-export":
                if not args.analysis:
                    raise ValueError("--analysis is required for offline-export")
                backend = OfflineExportBackend(Path(args.analysis))
                backend.provision_workspace(
                    binary=Path(args.binary) if args.binary else Path.cwd(),
                    workspace=output,
                )
            else:
                if not args.command:
                    raise ValueError("--command is required for ghidra backend")
                ghidra_backend = GhidraLifecycleBackend(
                    ghidra_cli=args.command[0],
                    timeout_s=args.timeout,
                )
                ghidra_backend.run_direct_export(argv=list(args.command), output=output)
        except (ValueError, OfflineExportError, GhidraLifecycleError) as exc:
            print(f"ERROR: export failed: {exc}", file=sys.stderr)
            return 1
        return 0
    return 1
