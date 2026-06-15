"""re-agent init command — generates unified config file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.defaults import DEFAULT_CONFIG_YAML


def cmd_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config)

    if args.profile is not None:
        print("Warning: --profile is deprecated and ignored; profile templates have been removed.", file=sys.stderr)
        print("Configure project_profile fields directly in the generated YAML.", file=sys.stderr)

    if config_path.exists():
        print(f"Config already exists: {config_path}")
        print("Delete it first if you want to regenerate.")
        return 1

    config_path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    print(f"Created {config_path}")
    print("Edit it to configure LLM, backend, project, and pipeline settings.")
    return 0
