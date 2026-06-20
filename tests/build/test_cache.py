from __future__ import annotations

import json
from pathlib import Path

from re_agent.build.state.cache import TransformCache


def test_has_returns_true_when_source_hash_matches(tmp_path: Path) -> None:
    cache = TransformCache(str(tmp_path / "cache.json"))
    cache.set("0x1000", "int main() {}", "output", True, 100, prompt_hash="p1", model="m1")
    assert cache.has("0x1000", "int main() {}") is True


def test_has_returns_false_when_source_changed(tmp_path: Path) -> None:
    cache = TransformCache(str(tmp_path / "cache.json"))
    cache.set("0x1000", "int main() {}", "output", True, 100, prompt_hash="p1", model="m1")
    assert cache.has("0x1000", "int other() {}") is False


def test_has_returns_false_when_prompt_hash_mismatch(tmp_path: Path) -> None:
    """Stale outputs after a prompt edit must not hit."""
    cache = TransformCache(str(tmp_path / "cache.json"))
    cache.set("0x1000", "src", "output", True, 100, prompt_hash="p1", model="m1")
    assert cache.has("0x1000", "src", prompt_hash="p2", model="m1") is False


def test_has_returns_false_when_model_mismatch(tmp_path: Path) -> None:
    cache = TransformCache(str(tmp_path / "cache.json"))
    cache.set("0x1000", "src", "output", True, 100, prompt_hash="p1", model="m1")
    assert cache.has("0x1000", "src", prompt_hash="p1", model="m2") is False


def test_has_returns_true_when_all_fields_match(tmp_path: Path) -> None:
    cache = TransformCache(str(tmp_path / "cache.json"))
    cache.set("0x1000", "src", "output", True, 100, prompt_hash="p1", model="m1")
    assert cache.has("0x1000", "src", prompt_hash="p1", model="m1") is True


def test_set_persists_to_disk(tmp_path: Path) -> None:
    p = tmp_path / "cache.json"
    cache = TransformCache(str(p))
    cache.set("0x1000", "src", "out", True, 50, prompt_hash="p1", model="m1")
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["0x1000"]["hash"] == TransformCache.hash_source("src")
    assert raw["0x1000"]["prompt_hash"] == "p1"
    assert raw["0x1000"]["model"] == "m1"
