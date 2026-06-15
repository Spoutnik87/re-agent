"""Master pipeline state: tracks reverse and build phases."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


class PipelineState:
    """Manages the master pipeline state file (pipeline-state.json)."""

    _VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})

    def __init__(self, path: str | Path = "pipeline-state.json") -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                _log.warning("Pipeline state file %s is corrupted; using default state.", self.path)
            except OSError:
                _log.warning("Cannot read pipeline state file %s; using default state.", self.path)
        return {
            "pipeline_version": "1.0",
            "phases": {
                "reverse": {"status": "pending"},
                "build": {"status": "pending"},
            },
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["last_pipeline_run"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get_reverse_status(self) -> str:
        return self._data["phases"].get("reverse", {}).get("status", "pending")  # type: ignore[no-any-return]

    def get_build_status(self) -> str:
        return self._data["phases"].get("build", {}).get("status", "pending")  # type: ignore[no-any-return]

    def update_reverse(self, status: str, **kwargs: Any) -> None:
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}. Must be one of {sorted(self._VALID_STATUSES)}")
        existing = self._data["phases"].get("reverse", {})
        self._data["phases"]["reverse"] = {
            **existing,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self._save()

    def update_build(self, status: str, **kwargs: Any) -> None:
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}. Must be one of {sorted(self._VALID_STATUSES)}")
        existing = self._data["phases"].get("build", {})
        self._data["phases"]["build"] = {
            **existing,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self._save()

    def is_reverse_completed(self) -> bool:
        return self.get_reverse_status() == "completed"

    def is_build_completed(self) -> bool:
        return self.get_build_status() == "completed"

    def summary(self) -> dict[str, Any]:
        return dict(self._data)
