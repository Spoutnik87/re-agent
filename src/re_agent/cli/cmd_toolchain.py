"""Toolchain profile command handlers independent from legacy configuration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from re_agent.project.snapshot import load_json
from re_agent.toolchain.activation import activate_profile, resolve_capability
from re_agent.toolchain.profile import ProfileError, load_profile, profile_schema


def _handle_status(project_root: Path) -> None:
    """Authenticate the active toolchain and detect source-profile drift."""
    active_path = project_root / "toolchain" / "active.link"
    if not active_path.is_file():
        print('no toolchain activation — run "re-agent toolchain activate" first')
        sys.exit(1)

    pointer = load_json(active_path)
    try:
        from re_agent.toolchain.activation import _authenticate_chain

        profile, fingerprint = _authenticate_chain(project_root, pointer)
    except (ProfileError, ValueError) as exc:
        result = {
            "activated": pointer,
            "authenticated": False,
            "error": str(exc),
            "drift": None,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    # Source-profile drift detection
    source_raw = pointer.get("source")
    drift: dict[str, object] | None = None
    if isinstance(source_raw, str):
        source_path = Path(source_raw)
        drift = {"source": str(source_path)}
        if source_path.is_file():
            try:
                current_profile = load_profile(source_path)
                drift["published_sha256"] = profile.sha256
                drift["current_sha256"] = current_profile.sha256
                drift["diverged"] = current_profile.sha256 != profile.sha256
            except (OSError, ProfileError) as exc:
                drift["error"] = f"cannot reload source profile: {exc}"
                drift["diverged"] = True
        else:
            drift["error"] = "source file not found"
            drift["diverged"] = True

    compiler_info: dict[str, object] = {}
    commands: object = fingerprint.get("commands", {})
    if isinstance(commands, dict):
        cc = commands.get("compiler", {})
        if isinstance(cc, dict) and not cc.get("missing"):
            compiler_info = {"sha256": cc.get("sha256", ""), "argv": cc.get("argv", [])}

    result = {
        "activated": pointer,
        "authenticated": True,
        "profile_sha256": profile.sha256,
        "compiler": compiler_info,
        "drift": drift,
    }
    # Compute available capabilities
    available = []
    for cap in ("compile", "link", "inspect_abi", "run_differential"):
        try:
            resolve_capability(project_root=project_root, capability=cap)
            available.append(cap)
        except ProfileError:
            pass
    result["available_capabilities"] = available
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def cmd_toolchain(args: argparse.Namespace) -> int:
    if args.toolchain_command == "schema":
        print(json.dumps(profile_schema(), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.project_root:
        print("ERROR: --project-root is required", file=sys.stderr)
        return 1
    try:
        if args.toolchain_command == "activate":
            if not args.profile:
                raise ProfileError("--profile is required")
            activate_profile(project_root=Path(args.project_root), profile_path=Path(args.profile))
            return 0
        if args.toolchain_command == "status":
            _handle_status(Path(args.project_root))
            return 0
    except (OSError, ProfileError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1
