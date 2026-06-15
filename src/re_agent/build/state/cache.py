"""Cache that maps source hash -> transformation result to avoid re-processing."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


class TransformCache:
    """Cache that maps source hash -> transformation result to avoid re-processing."""

    def __init__(self, cache_path: str = ".cr-agent-cache.json") -> None:
        self._cache_path = cache_path
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                self._data: dict[str, dict[str, Any]] = json.load(f)
        else:
            self._data: dict[str, dict[str, Any]] = {}

    @staticmethod
    def hash_source(source: str) -> str:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]

    def get(self, address: str) -> dict[str, Any] | None:
        return self._data.get(address)

    def set(
        self,
        address: str,
        source: str,
        output_file: str,
        compiles: bool,
        tokens_used: int,
    ) -> None:
        self._data[address] = {
            "hash": self.hash_source(source),
            "output_file": output_file,
            "compiles": compiles,
            "tokens_used": tokens_used,
        }
        self._persist()

    def has(self, address: str, source: str) -> bool:
        entry = self._data.get(address)
        if entry is None:
            return False
        return entry["hash"] == self.hash_source(source)

    def size(self) -> int:
        return len(self._data)

    def _persist(self) -> None:
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
