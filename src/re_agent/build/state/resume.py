"""Build-phase resume state management."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_PATH = Path("cr-agent-state.json")


def save_state(data: dict[str, Any], state_path: Path | None = None) -> None:
    """Save current pipeline state to JSON file (with timestamp)."""
    path = state_path or STATE_PATH
    data["timestamp"] = datetime.now().isoformat()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state(state_path: Path | None = None) -> dict[str, Any]:
    """Load saved pipeline state. Returns empty dict if no state exists."""
    path = state_path or STATE_PATH
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    return {}


def show_status(state_path: Path | None = None) -> None:
    """Display current pipeline progress from state file."""
    state = load_state(state_path)
    if not state:
        print("No state file found. No pipeline in progress.")
        return
    print(f"Phase: {state.get('phase', 'unknown')}")
    print(f"Completed modules: {state.get('completed_modules', [])}")
    print(f"Current module: {state.get('current_module', 'none')}")
    print(f"Current sub-unit: {state.get('current_subunit', 0)}")
    print(f"Last update: {state.get('timestamp', 'unknown')}")
