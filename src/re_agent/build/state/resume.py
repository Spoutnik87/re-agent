import json
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_PATH = Path("cr-agent-state.json")


def save_state(data: dict[str, Any]) -> None:
    """Save current pipeline state to JSON file (with timestamp)."""
    data["timestamp"] = datetime.now().isoformat()
    STATE_PATH.write_text(json.dumps(data, indent=2))


def load_state() -> dict[str, Any]:
    """Load saved pipeline state. Returns empty dict if no state exists."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    return {}


def show_status() -> None:
    """Display current pipeline progress from state file."""
    state = load_state()
    if not state:
        print("No state file found. No pipeline in progress.")
        return
    print(f"Phase: {state.get('phase', 'unknown')}")
    print(f"Completed modules: {state.get('completed_modules', [])}")
    print(f"Current module: {state.get('current_module', 'none')}")
    print(f"Current sub-unit: {state.get('current_subunit', 0)}")
    print(f"Last update: {state.get('timestamp', 'unknown')}")
