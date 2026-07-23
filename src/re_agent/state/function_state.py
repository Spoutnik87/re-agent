"""Single authoritative per-function state store (keyed by address)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class FunctionRecord:
    address: str
    reversed: bool = False
    normalized: bool = False
    compiles: bool | None = None
    compiles_strict: bool | None = None
    checker: str | None = None
    structural: str | None = None
    parity: str | None = None
    behavioral: str | None = None
    tokens: int = 0


_FIELD_NAMES = {f.name for f in fields(FunctionRecord)}


class FunctionStateStore:
    def __init__(self, path: str | Path = "function-state.json") -> None:
        self.path = Path(path)
        self._records: dict[str, FunctionRecord] = self._load()
        self._dirty = False

    def _load(self) -> dict[str, FunctionRecord]:
        if not self.path.exists():
            return {}
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return {}
        out: dict[str, FunctionRecord] = {}
        for addr, data in raw.items():
            filtered = {k: v for k, v in data.items() if k in _FIELD_NAMES}
            filtered["address"] = addr
            out[addr] = FunctionRecord(**filtered)
        return out

    def get(self, address: str) -> FunctionRecord | None:
        return self._records.get(address)

    def update(self, address: str, **changes: Any) -> FunctionRecord:
        rec = self._records.get(address) or FunctionRecord(address=address)
        for key, value in changes.items():
            if key not in _FIELD_NAMES or key == "address":
                raise ValueError(f"Unknown field: {key!r}")
            setattr(rec, key, value)
        self._records[address] = rec
        self._dirty = True
        return rec

    def all(self) -> list[FunctionRecord]:
        return list(self._records.values())

    def summary(self) -> dict[str, int]:
        recs = self._records.values()
        return {
            "total": len(self._records),
            "reversed": sum(1 for r in recs if r.reversed),
            "compiles": sum(1 for r in recs if r.compiles is True),
            "behaviorally_equivalent": sum(1 for r in recs if r.behavioral == "equivalent"),
        }

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            addr: {k: v for k, v in asdict(rec).items() if k != "address"} for addr, rec in self._records.items()
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._dirty = False
