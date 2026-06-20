from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from re_agent.build.state.resume import load_state, save_state


def test_save_and_load_preserves_current_subunit(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    with patch("re_agent.build.state.resume.STATE_PATH", state_path):
        save_state(
            {
                "completed_modules": [],
                "current_module": "mod1",
                "current_subunit": 5,
                "phase": "transform",
            }
        )
        _state = load_state()
    assert _state["current_module"] == "mod1"
    assert _state["current_subunit"] == 5


def test_load_state_returns_empty_when_no_file(tmp_path: Path) -> None:
    state_path = tmp_path / "nonexistent.json"
    with patch("re_agent.build.state.resume.STATE_PATH", state_path):
        _state = load_state()
    assert _state == {}


def test_save_state_uses_utf8_encoding(tmp_path: Path) -> None:
    """save_state must write UTF-8 (not platform default cp1252 on Windows)."""
    state_path = tmp_path / "state.json"
    with patch("re_agent.build.state.resume.STATE_PATH", state_path):
        save_state({"phase": "transform", "note": "caf\u00e9"})
    content = state_path.read_text(encoding="utf-8")
    assert "caf\u00e9" in content
