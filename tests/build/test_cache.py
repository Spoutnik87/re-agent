from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from re_agent.build.state.cache import TransformCache
from re_agent.build.transform.subunit_processor import process_subunit
from re_agent.llm.protocol import Message


class _FakeProvider:
    """Minimal LLMProvider-compatible double returning a canned transform."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_messages: list[Message] = []

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.last_messages = list(messages)
        self.total_calls += 1
        return self._response

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


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


def test_cache_old_flat_format_readable(tmp_path: Path) -> None:
    """Backward-compat: old-format cache entries with only output_file
    must still be readable via TransformCache.get(). Regression guard:
    adding structured output_files must not break existing readers."""
    cache_path = tmp_path / "cache.json"
    old_data = {
        "0x1000": {
            "hash": "abc",
            "output_file": "void f() {}",
            "compiles": True,
            "tokens_used": 100,
            "prompt_hash": "p1",
            "model": "m1",
        }
    }
    cache_path.write_text(json.dumps(old_data), encoding="utf-8")
    cache = TransformCache(str(cache_path))
    entry = cache.get("0x1000")
    assert entry is not None
    assert entry["output_file"] == "void f() {}"
    assert entry["compiles"] is True


def test_cache_entry_boundaries_preserved_on_write(monkeypatch: Any, tmp_path: Path) -> None:
    """FAILING-FIRST: cache output_file must preserve // FILE: boundaries
    when written through the module_processor caching path.

    Currently module_processor joins only f['content'] (already stripped of
    // FILE: headers by _parse_llm_response), so the stored output_file
    cannot be split into per-file blocks and cross-address contamination
    cannot be detected. This test asserts the expected boundary markers
    and will fail until module_processor includes them.
    """
    # -- Fake provider returning three FILE blocks for two addresses --------
    response = (
        "// FILE: src/mod/0x1000__A.cpp\n"
        "// 0x1000\nvoid A::f() {}\n"
        "\n"
        "// FILE: include/mod/0x1000__A.h\n"
        "#pragma once\nstruct A { void f(); };\n"
        "\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        "// 0x1001\nvoid B::g() {}\n"
    )
    provider = _FakeProvider(response)

    class _Cfg:
        class output:
            language = "C++"
            standard = "c++23"

        class project:
            description = ""

            class conventions:
                class naming:
                    classes = "PascalCase"
                    functions = "camelCase"
                    globals = "snake_case"

                includes_rule = ""
                max_function_lines = 200

        class validation:
            max_compile_retries = 0
            compile_per_function = False  # avoid needing a real compiler

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx: dict[str, Any] = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void FUN_0x1000() {}", "name": "FUN_0x1000"},
            {"address": "0x1001", "code": "void FUN_0x1001() {}", "name": "FUN_0x1001"},
        ],
        "neighbour_context": [],
    }

    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    # -- Replicate the cache-write loop from module_processor (lines 144-167) --
    cache = TransformCache(str(tmp_path / "cache.json"))
    for r in results:
        addr = r["function"]
        source_for_addr = ""
        for func in ctx["functions_to_transform"]:
            if func["address"] == addr:
                source_for_addr = func["code"]
                break
        combined_output = "\n".join(f["content"] for f in r.get("files", []))
        cache.set(addr, source_for_addr, combined_output, r.get("compiles", False), 0, output_files=r.get("files", []))

    # -- Assert: cache entry for 0x1000 has // FILE: markers ---------------
    entry_a = cache.get("0x1000")
    assert entry_a is not None, "Cache entry for 0x1000 must exist"

    from re_agent.build.transform.subunit_processor import _FILE_MARKER_RE

    parts_a = _FILE_MARKER_RE.split(entry_a["output_file"])
    assert len(parts_a) > 1, (
        f"Cache output_file for 0x1000 must contain // FILE: markers "
        f"so per-file reconstruction is possible. "
        f"Got ({len(parts_a)} parts): {entry_a['output_file'][:200]!r}"
    )

    # -- Assert: 0x1000 entry does not contain 0x1001 content ---------------
    assert "0x1001" not in entry_a["output_file"], "Cache entry for 0x1000 must not contain 0x1001 address markers"

    # -- Assert: cache entry for 0x1001 also has boundaries -----------------
    entry_b = cache.get("0x1001")
    assert entry_b is not None, "Cache entry for 0x1001 must exist"

    parts_b = _FILE_MARKER_RE.split(entry_b["output_file"])
    assert len(parts_b) > 1, (
        f"Cache output_file for 0x1001 must contain // FILE: markers. "
        f"Got ({len(parts_b)} parts): {entry_b['output_file'][:200]!r}"
    )
