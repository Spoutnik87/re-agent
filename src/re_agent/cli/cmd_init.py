"""re-agent init command — generates unified config file with ABI manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.defaults import DEFAULT_CONFIG_YAML
from re_agent.contracts import load_verified_manifest


def cmd_init(args: argparse.Namespace) -> int:
    """Create a re-agent.yaml config file with a validated ABI manifest.

    ``--abi-manifest`` is **required**.  The manifest is validated via
    ``load_verified_manifest``; its raw SHA-256 is injected into the
    generated YAML so the file is immediately reloadable.
    """
    config_path = Path(args.config)

    abi_manifest_raw = getattr(args, "abi_manifest", None)
    if abi_manifest_raw is None:
        print("Error: --abi-manifest <PATH> is required.", file=sys.stderr)
        print("Usage: re-agent init --abi-manifest <PATH_TO_ABI_MANIFEST>", file=sys.stderr)
        return 2

    abi_path = Path(abi_manifest_raw)
    if not abi_path.exists():
        print(f"Error: ABI manifest not found: {abi_path}", file=sys.stderr)
        return 2
    if not abi_path.is_file():
        print(f"Error: ABI manifest is not a regular file: {abi_path}", file=sys.stderr)
        return 2

    # Validate manifest and get its raw SHA-256
    try:
        _, raw_hash, _ = load_verified_manifest(abi_path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Error: Invalid ABI manifest: {exc}", file=sys.stderr)
        return 2

    if config_path.exists():
        print(f"Config already exists: {config_path}", file=sys.stderr)
        return 1

    # Generate YAML with contracts section pointing to the manifest
    resolved_path = abi_path.resolve().as_posix()
    contracts_section = f"""\
contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "{resolved_path}"
  abi_manifest_sha256: "{raw_hash}"
"""
    yaml_content = DEFAULT_CONFIG_YAML.rstrip("\n") + "\n" + contracts_section
    config_path.write_text(yaml_content, encoding="utf-8")
    print(f"Created {config_path}")
    print("ABI manifest validated and embedded. Config is ready to use.")
    return 0
