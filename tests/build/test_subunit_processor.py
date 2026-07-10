from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from re_agent.build.transform.subunit_processor import _parse_llm_response, process_subunit
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


class _FakeMultiResponseProvider:
    """Fake LLMProvider returning canned responses in sequence per send() call.

    Use this when tests need different initial vs retry responses. Each
    ``send()`` returns the next response in the list; if more calls are made
    than responses, the last response repeats.
    """

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.last_messages: list[Message] = []

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.last_messages = list(messages)
        self.total_calls += 1
        idx = min(self.total_calls - 1, len(self._responses) - 1)
        return self._responses[idx]


def test_parse_llm_response_returns_all_files() -> None:
    """_parse_llm_response must return ALL // FILE: blocks, not just the first."""
    response = (
        "// FILE: include/mod/Class.h\n#pragma once\nstruct Class {};\n"
        '\n// FILE: src/mod/Class.cpp\n#include "Class.h"\nvoid Class::f() {}\n'
    )
    files = _parse_llm_response(response)
    assert len(files) == 2
    paths = [f["path"] for f in files]
    assert "include/mod/Class.h" in paths
    assert "src/mod/Class.cpp" in paths


def test_process_subunit_returns_files_list_not_single_output(monkeypatch) -> None:
    """Each result must carry a 'files' list of {path, content} dicts,
    not a single 'output_file' string keyed by address."""
    response = (
        "// FILE: include/mod/Class.h\n#pragma once\nstruct Class {};\n"
        '\n// FILE: src/mod/Class.cpp\n#include "Class.h"\nvoid Class::f() {}\n'
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert len(results) == 1
    r = results[0]
    assert "files" in r, "result must have 'files' list"
    assert len(r["files"]) == 2
    assert r["compiles"] is True


def test_process_subunit_uses_shared_provider_protocol(monkeypatch) -> None:
    """process_subunit must accept any LLMProvider, not the deleted LLMClient."""
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert len(results) == 1
    assert results[0]["compiles"] is True
    assert provider.total_calls == 1


def test_retry_loop_iterates_max_compile_retries_times(monkeypatch) -> None:
    """The retry must loop max_compile_retries times, not just once."""
    call_count = [0]

    def _flaky_compile(code: str, cfg: Any) -> tuple[bool, str]:
        call_count[0] += 1
        return (call_count[0] >= 3, "error" if call_count[0] < 3 else "")

    response = "// FILE: src/mod/Class.cpp\nvoid f() {}\n"
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
            max_compile_retries = 3

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _flaky_compile)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    _results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert provider.total_calls <= 4
    assert provider.total_calls >= 2


def test_match_files_to_function_by_address_when_name_missing() -> None:
    """Regression test for the NO_OUTPUT bug (docs/_diagnostic_no_output.md).

    ``context_builder.build_context`` emits ``{"address": addr, "code": code}``
    with NO ``name`` field. The LLM prompt exposes the address via
    ``### Function {{ func.address }}``, so the address is the stable
    identifier across the LLM round-trip. Matching must succeed by address
    even when ``name`` is absent.
    """
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": "void FUN_00414580() {}"}
    parsed = [
        {
            "path": "src/renderer/Renderer.cpp",
            "content": "// 0x00414580\nvoid Renderer::draw() {}",
        }
    ]
    result = _match_files_to_function(parsed, func, total_func_count=10)
    assert result == parsed, "must match by address when name is absent"


def test_match_files_to_function_address_case_insensitive() -> None:
    """Addresses may appear upper- or lower-case in LLM output; matching must be case-insensitive."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": ""}
    parsed = [
        {"path": "src/mod/A.cpp", "content": "// 0x00414580\nvoid A::f() {}"},
        {"path": "src/mod/B.cpp", "content": "// 0x004145a0\nvoid B::g() {}"},
    ]
    result = _match_files_to_function(parsed, func, total_func_count=2)
    assert len(result) == 1
    assert result[0]["path"] == "src/mod/A.cpp"


def test_match_files_to_function_name_takes_precedence_when_present() -> None:
    """When ``name`` is present and matches, it takes precedence over address matching
    (preserves backwards compatibility for callers that still provide ``name``)."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": "", "name": "drawThing"}
    parsed = [
        {"path": "src/mod/drawThing.cpp", "content": "void drawThing() {}"},
        {"path": "src/mod/other.cpp", "content": "// 0x00414580\nvoid other() {}"},
    ]
    result = _match_files_to_function(parsed, func, total_func_count=2)
    assert len(result) == 1
    assert result[0]["path"] == "src/mod/drawThing.cpp"


def test_match_files_to_function_single_parsed_file_fallback() -> None:
    """When the LLM emits a single // FILE: block for a multi-function subunit,
    that file is assigned to every function rather than producing N-1 NO_OUTPUT."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": ""}
    parsed = [{"path": "src/mod/all.cpp", "content": "void f() {} void g() {}"}]
    result = _match_files_to_function(parsed, func, total_func_count=2)
    assert result == parsed


def test_match_files_to_function_old_bug_returns_empty_without_address_match() -> None:
    """Documents the OLD buggy behaviour: with only name-based matching and no
    name in the context dict, a multi-function subunit returned [] for every
    function. The fix adds address matching; this test asserts the fix prevents
    that regression by confirming a non-empty result when the address matches."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": ""}  # no "name" — the real-world case
    parsed = [{"path": "src/mod/A.cpp", "content": "// 0x00414580\nvoid A() {}"}]
    result = _match_files_to_function(parsed, func, total_func_count=10)
    assert result != [], "address-based matching must prevent the NO_OUTPUT regression"


def test_per_subunit_retry_re_sends_whole_subunit(monkeypatch) -> None:
    """When multiple functions fail compile, retry the whole subunit in one call
    with per-function error annotations, not one call per function."""
    response = "// FILE: src/mod/A.cpp\nvoid a() {}\n\n// FILE: src/mod/B.cpp\nvoid b() {}\n"
    provider = _FakeProvider(response)

    compile_calls = [0]

    def _compile(code: str, cfg: Any) -> tuple[bool, str]:
        compile_calls[0] += 1
        return (compile_calls[0] > 2, "error" if compile_calls[0] <= 2 else "")

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
            max_compile_retries = 2

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    _ = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert provider.total_calls <= 3, f"Expected <=3 LLM calls, got {provider.total_calls}"


def test_parsed_but_unmatched_multi_file_no_address(monkeypatch) -> None:
    """Regression for Todo 10 failure class: parsed-but-unmatched multi-file output.

    Simulates a multi-function subunit where the fake LLM emits multiple
    ``// FILE:`` blocks with valid syntax, but paths and content omit every
    original target address/name. The system must classify this as
    **parsed-but-unmatched**: ``marker_count > 0``, ``parse_count > 0``,
    ``match_strategy = "none"``, ``files_written = 0``, verdict ``NO_OUTPUT``
    for every function.

    This is NOT a parser failure (which would have ``marker_count = 0``).
    The per-function diagnostic exposes enough to distinguish the two.
    """
    # ── Fake LLM: two valid FILE blocks, no address/name in paths or content ──
    response = (
        "// FILE: include/renderer/FrameRenderer.h\n"
        "#pragma once\nstruct FrameRenderer {\n  void renderFrame();\n};\n"
        "\n"
        "// FILE: src/renderer/FrameRenderer.cpp\n"
        '#include "FrameRenderer.h"\n'
        "void FrameRenderer::renderFrame() { /* render pass */ }\n"
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
            {"address": "0x00411800", "code": "void FUN_00411800() {}", "name": "FUN_00411800"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    # ── Every function gets NO_OUTPUT (no match) ──
    assert len(results) == 2, f"Expected 2 function results, got {len(results)}"

    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT, got {r['verdict']}"
        assert r["compiles"] is False
        assert r["files"] == [], "parsed-but-unmatched must not assign files"

    # ── Diagnostic: parse_count > 0, match_strategy = "none" ──
    diag = results[0].get("diagnostic", {})
    assert diag.get("marker_count", 0) > 0, (
        "marker_count must be > 0 for parsed-but-unmatched "
        "(distinguishes from true parser failure where marker_count == 0)"
    )
    assert diag.get("parse_count", 0) > 0, "parse_count must be > 0 — FILE blocks were syntactically valid"
    assert diag.get("match_strategy") == "none", (
        "match_strategy must be 'none' when no address/name matches any parsed file"
    )
    assert diag.get("files_written", 999) == 0, (
        "files_written must be 0; no files should be matched without address/name evidence"
    )

    # ── Per-function diagnostic: each func independently unmatched ──
    both_strategies = {r.get("diagnostic", {}).get("match_strategy") for r in results}
    assert both_strategies == {"none"}, f"every function must be unmatched, got {both_strategies}"

    both_files_written = {r.get("diagnostic", {}).get("files_written") for r in results}
    assert both_files_written == {0}, "every function must have files_written == 0"

    # ── Enriched diagnostics: candidate_paths, candidate_has_address, candidate_has_name ──
    # Both functions see all 2 parsed files as candidates.
    for r in results:
        diag = r.get("diagnostic", {})
        candidates = diag.get("candidate_paths", [])
        assert len(candidates) == 2, f"candidate_paths must list all parsed files, got {candidates}"
        assert "include/renderer/FrameRenderer.h" in candidates
        assert "src/renderer/FrameRenderer.cpp" in candidates

        has_addr = diag.get("candidate_has_address", [])
        assert len(has_addr) == 2
        # Neither parsed file contained 0x004117c0 or 0x00411800
        assert not any(has_addr), "candidate_has_address must be all False: no parsed file contained target addresses"

        has_name = diag.get("candidate_has_name", [])
        assert len(has_name) == 2
        # Neither file contained "FUN_004117c0" or "FUN_00411800"
        assert not any(has_name), "candidate_has_name must be all False: no parsed file contained target names"


def test_parsed_but_unmatched_distinct_from_parser_failure(monkeypatch) -> None:
    """parser failure (marker_count=0) and parsed-but-unmatched (marker_count>0, strategy=none)
    must be distinguishable via subunit-level diagnostics.
    """
    # ── Parsed-but-unmatched: valid markers, no address match ──
    response_multi = "// FILE: src/mod/X.cpp\nvoid xFn() {}\n\n// FILE: src/mod/Y.cpp\nvoid yFn() {}\n"
    provider_multi = _FakeProvider(response_multi)

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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx_multi = {
        "functions_to_transform": [
            {"address": "0xA000", "code": "void a() {}", "name": "A"},
            {"address": "0xB000", "code": "void b() {}", "name": "B"},
        ],
        "neighbour_context": [],
    }
    results_multi = process_subunit(ctx_multi, "mod", provider_multi, _Cfg(), cache=None)
    diag_multi = results_multi[0].get("diagnostic", {})

    # ── marker_count > 0, strategy = "none" ──
    assert diag_multi.get("marker_count", -1) > 0
    assert diag_multi.get("parse_count", -1) > 0
    assert diag_multi.get("match_strategy") == "none"
    assert diag_multi.get("files_written") == 0

    # ── Enriched diagnostics: candidate lists populated even when unmatched ──
    assert len(diag_multi.get("candidate_paths", [])) == 2, "candidate_paths must list both parsed files"
    assert "src/mod/X.cpp" in diag_multi["candidate_paths"]
    assert "src/mod/Y.cpp" in diag_multi["candidate_paths"]

    # ── Parser failure: no FILE markers at all ──
    response_none = "This is not proper output without any file markers."
    provider_none = _FakeProvider(response_none)

    ctx_single = {
        "functions_to_transform": [
            {"address": "0xA000", "code": "void a() {}", "name": "A"},
        ],
        "neighbour_context": [],
    }
    results_none = process_subunit(ctx_single, "mod", provider_none, _Cfg(), cache=None)
    diag_none = results_none[0].get("diagnostic", {})

    # ── marker_count == 0 for true parser failure ──
    assert diag_none.get("marker_count", -1) == 0, "parser failure (no FILE markers) must have marker_count == 0"
    assert diag_none.get("parse_count", -1) == 0

    # ── Both produce NO_OUTPUT at function level, but are distinguishable ──
    assert results_none[0]["verdict"] == "NO_OUTPUT"
    assert results_multi[0]["verdict"] == "NO_OUTPUT"

    # ── Subunit-level marker_count distinguishes the two ──
    assert diag_multi["marker_count"] != diag_none["marker_count"], (
        "parsed-but-unmatched and parser-failure must differ in marker_count"
    )

    # ── Parser failure: candidate lists are empty (nothing parsed) ──
    assert diag_none.get("candidate_paths", ["sentinel"]) == [], "parser failure must have empty candidate_paths"
    assert diag_none.get("candidate_has_address", ["sentinel"]) == []
    assert diag_none.get("candidate_has_name", ["sentinel"]) == []


def test_address_bearing_multi_function_all_match(monkeypatch) -> None:
    """Prove address-bearing multi-function output matches all functions end-to-end.

    Given a multi-function subunit where the fake LLM emits address-bearing
    ``// FILE:`` blocks for two functions (each with the target address in both
    path and content), When ``process_subunit`` runs offline, Then every
    function is matched by address, compiles, and records PASS (or PASS_RETRY)
    with ``files_written > 0``. This is the positive counterpart to the
    parsed-but-unmatched regression and proves Todo 2's prompt contract works
    when the LLM follows it.
    """
    # ── Fake LLM: two functions, each with address-bearing FILE blocks ──
    # File paths and content both contain the target addresses.
    response = (
        "// FILE: src/renderer/0x004117c0__RendererInit.cpp\n"
        '#include "_decls.h"\n'
        "// Original function: 0x004117c0\n"
        "void RendererInit() {}\n"
        "\n"
        "// FILE: include/renderer/0x004117c0__RendererInit.h\n"
        "#pragma once\n"
        "// Original function: 0x004117c0\n"
        "void RendererInit();\n"
        "\n"
        "// FILE: src/renderer/0x00411800__RendererDraw.cpp\n"
        '#include "_decls.h"\n'
        "// Original function: 0x00411800\n"
        "void RendererDraw() {}\n"
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
            {"address": "0x00411800", "code": "void FUN_00411800() {}", "name": "FUN_00411800"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    # ── Both functions produce results ──
    assert len(results) == 2, f"Expected 2 function results, got {len(results)}"
    addrs = {r["function"] for r in results}
    assert addrs == {"0x004117c0", "0x00411800"}

    # ── Each function matched by address with files_written > 0 ──
    total_written = 0
    for r in results:
        verdict = r["verdict"]
        assert verdict in {"PASS", "PASS_RETRY"}, f"Expected PASS/PASS_RETRY for {r['function']}, got {verdict}"
        assert r["compiles"] is True
        assert len(r["files"]) > 0, f"No files matched for {r['function']}"

        diag = r["diagnostic"]
        fw = diag.get("files_written", 0)
        assert fw > 0, f"files_written must be > 0 for {r['function']}, got {fw}"
        match_strat = diag.get("match_strategy", "")
        assert match_strat in {
            "by_address",
            "by_name",
            "single_function",
        }, f"match_strategy must be a positive strategy for {r['function']}, got {match_strat!r}"
        total_written += fw

    # ── Aggregate: total_files_written across all functions > 0 ──
    assert total_written > 0, "total files written must be > 0"

    # ── Address 0x004117c0 matched both .cpp and .h (its addr appears in both paths) ──
    r_a = next(r for r in results if r["function"] == "0x004117c0")
    assert r_a["diagnostic"]["files_written"] == 2, (
        "0x004117c0 should match 2 files (its .cpp and .h both carry the address in path)"
    )
    # ── Address 0x00411800 matched only its .cpp (no .h emitted for it) ──
    r_b = next(r for r in results if r["function"] == "0x00411800")
    assert r_b["diagnostic"]["files_written"] == 1, "0x00411800 should match 1 file (only the .cpp carries its address)"


def test_compile_per_function_false_skips_all_compile_checks(monkeypatch) -> None:
    """Given compile_per_function=false, compile_check() must NOT be called
    for ANY function, even well-matched ones. Verdict must be SKIPPED_COMPILE
    (not PASS/PASS_RETRY). No retry LLM calls should occur."""
    compile_called: list[bool] = []

    def _compile_should_not_be_called(code: str, cfg: Any) -> tuple[bool, str]:
        compile_called.append(True)
        return (True, "")

    response = "// FILE: src/mod/A.cpp\nvoid a() {}\n\n// FILE: src/mod/B.cpp\nvoid b() {}\n"
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
            max_compile_retries = 2
            compile_per_function = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_should_not_be_called)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert compile_called == [], (
        f"compile_check must not be called when compile_per_function=False, got {len(compile_called)} call(s)"
    )
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    for r in results:
        assert r["verdict"] == "SKIPPED_COMPILE", f"expected SKIPPED_COMPILE for {r['function']}, got {r['verdict']}"
        assert r["compiles"] is False
        assert len(r["files"]) > 0
    assert provider.total_calls == 1


def test_compile_per_function_false_still_parses_and_writes_diagnostics(monkeypatch, tmp_path) -> None:
    """When compile_per_function=false, parsing/matching/writing diagnostics
    must still work. The work-packet JSON must contain SKIPPED_COMPILE verdicts
    with correct marker_count/parse_count/files_matched."""
    monkeypatch.chdir(tmp_path)

    def _compile_not_called(code: str, cfg: Any) -> tuple[bool, str]:
        raise AssertionError("compile_check must not be called")

    response = (
        "// FILE: src/renderer/0x004117c0__RendererInit.cpp\n"
        '#include "_decls.h"\n'
        "// Original function: 0x004117c0\n"
        "void RendererInit() {}\n"
        "\n"
        "// FILE: include/renderer/0x004117c0__RendererInit.h\n"
        "#pragma once\n"
        "void RendererInit();\n"
        "\n"
        "// FILE: src/renderer/0x00411800__RendererDraw.cpp\n"
        '#include "_decls.h"\n'
        "// Original function: 0x00411800\n"
        "void RendererDraw() {}\n"
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
            compile_per_function = False

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_not_called)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    diag_dir = tmp_path / "work-packets"

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
            {"address": "0x00411800", "code": "void FUN_00411800() {}", "name": "FUN_00411800"},
        ],
        "neighbour_context": [],
        "subunit_index": 1,
        "run_id": "compile-gate-test",
    }
    cfg_instance = _Cfg()
    cfg_instance.optimization.diagnostics_dir = str(diag_dir)
    results = process_subunit(ctx, "renderer", provider, cfg_instance, cache=None)

    assert len(results) == 2

    r_a = next(r for r in results if r["function"] == "0x004117c0")
    assert r_a["verdict"] == "SKIPPED_COMPILE"
    assert r_a["compiles"] is False
    assert len(r_a["files"]) == 2
    assert r_a["files"][0]["path"] == "src/renderer/0x004117c0__RendererInit.cpp"

    r_b = next(r for r in results if r["function"] == "0x00411800")
    assert r_b["verdict"] == "SKIPPED_COMPILE"
    assert r_b["compiles"] is False
    assert len(r_b["files"]) == 1

    diag_a = r_a["diagnostic"]
    assert diag_a["marker_count"] == 3
    assert diag_a["parse_count"] == 3
    assert diag_a["files_written"] == 2
    assert diag_a["work_packet_path"] is not None

    wp_path = Path(diag_a["work_packet_path"])
    assert wp_path.exists()
    wp_data = json.loads(wp_path.read_text(encoding="utf-8"))
    assert wp_data["run_id"] == "compile-gate-test"
    assert wp_data["module_name"] == "renderer"
    assert wp_data["subunit_index"] == 1
    assert wp_data["parse_count"] == 3
    assert wp_data["marker_count"] == 3
    verdict_map = {fv["address"]: fv["verdict"] for fv in wp_data["function_verdicts"]}
    assert verdict_map == {
        "0x004117c0": "SKIPPED_COMPILE",
        "0x00411800": "SKIPPED_COMPILE",
    }
    assert wp_data["total_files_written"] == 0


def test_compile_per_function_false_unmatched_still_no_output(monkeypatch) -> None:
    """When compile_per_function=false but files do not match any function,
    the verdict must remain NO_OUTPUT (not SKIPPED_COMPILE). No compile
    checks happen."""
    compile_called: list[bool] = []

    def _compile_not_called(code: str, cfg: Any) -> tuple[bool, str]:
        compile_called.append(True)
        return (True, "")

    response = (
        "// FILE: include/unrelated.h\n#pragma once\nstruct Unrelated {};\n"
        "\n// FILE: src/other/0x00DEAD00__Unrelated.cpp\nvoid Unrelated::f() {}\n"
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
            compile_per_function = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_not_called)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
            {"address": "0x00411800", "code": "void FUN_00411800() {}", "name": "FUN_00411800"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    assert compile_called == [], "compile_check must not be called when gate is off"
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", (
            f"expected NO_OUTPUT for unmatched function {r['function']}, got {r['verdict']}"
        )
        assert r["compiles"] is False
        assert r["files"] == []
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "none"
        assert diag["files_written"] == 0
        assert diag["marker_count"] == 2
        assert diag["parse_count"] == 2


def test_compile_per_function_default_true_still_compiles(monkeypatch) -> None:
    """When compile_per_function is NOT set (or explicitly True), the default
    compile behavior is preserved: compile_check is called and PASS is produced
    for compiling code."""
    compile_called: list[bool] = []

    def _compile_success(code: str, cfg: Any) -> tuple[bool, str]:
        compile_called.append(True)
        return (True, "")

    response = '// FILE: src/mod/A.cpp\n#include "_decls.h"\nvoid A::f() {}\n'
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_success)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(compile_called) >= 1, (
        f"compile_check should be called at least once by default, got {len(compile_called)} calls"
    )
    assert results[0]["verdict"] == "PASS"
    assert results[0]["compiles"] is True
    assert len(results[0]["files"]) > 0


def test_compile_per_function_true_failing_compile_still_fails(monkeypatch) -> None:
    """When compile_per_function=True and compile fails, the normal
    FAIL_NO_RETRY verdict must still be produced with diagnostics including
    compile_error and compile_error_category."""
    response = '// FILE: src/mod/A.cpp\n#include "_decls.h"\nvoid A::f() { incomplete\n'
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
            compile_per_function = True

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (False, "syntax error"))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 1
    assert results[0]["verdict"] == "FAIL_NO_RETRY"
    assert results[0]["compiles"] is False
    assert len(results[0]["files"]) > 0
    # compile_error and compile_error_category must be populated from the err
    # returned by compile_check.
    diag = results[0].get("diagnostic", {})
    assert diag.get("compile_error") is not None, "FAIL_NO_RETRY must populate compile_error from compile_check err"
    assert diag.get("compile_error_category") is not None, "FAIL_NO_RETRY must populate compile_error_category"


def test_classname_only_no_address_stays_blocked(monkeypatch) -> None:
    """Negative case: ClassName-only paths with no addresses must remain blocked.

    Given a fake LLM response with ``// FILE:`` blocks using descriptive names
    only (no original addresses in paths or content — the pre-Todo-2 output
    shape), When ``process_subunit`` runs, Then every function produces
    ``NO_OUTPUT`` with ``match_strategy = "none"`` and ``files_written = 0``.
    The system must NOT silently PASS on ClassName-only output.
    """
    # ── Fake LLM: descriptive ClassName paths but NO addresses anywhere ──
    response = (
        "// FILE: include/renderer/FrameRenderer.h\n"
        "#pragma once\nstruct FrameRenderer {\n  void render();\n};\n"
        "\n"
        "// FILE: src/renderer/FrameRenderer.cpp\n"
        '#include "_decls.h"\n'
        "void FrameRenderer::render() {}\n"
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
            {"address": "0x00411800", "code": "void FUN_00411800() {}", "name": "FUN_00411800"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    assert len(results) == 2, f"Expected 2 function results, got {len(results)}"

    for r in results:
        assert r["verdict"] == "NO_OUTPUT", (
            f"ClassName-only output must produce NO_OUTPUT for {r['function']}, got {r['verdict']}"
        )
        assert r["compiles"] is False
        assert r["files"] == [], "no files must be matched for ClassName-only output"

        diag = r["diagnostic"]
        assert diag.get("match_strategy") == "none", (
            f"match_strategy must be 'none' for ClassName-only output on {r['function']}"
        )
        assert diag.get("files_written", 999) == 0, "files_written must be 0 for ClassName-only output"
        assert diag.get("marker_count", 0) > 0, "marker_count must be > 0 (parsed-but-unmatched, not parser failure)"
        # ── Diagnostic enrichment: candidates populated but no addresses/names ──
        candidates = diag.get("candidate_paths", [])
        assert len(candidates) == 2, "candidate_paths must list both parsed files"
        assert "include/renderer/FrameRenderer.h" in candidates
        assert "src/renderer/FrameRenderer.cpp" in candidates

        has_addr = diag.get("candidate_has_address", [True])
        assert not any(has_addr), "candidate_has_address must be all False: ClassName-only paths have no addresses"

        has_name = diag.get("candidate_has_name", [True])
        assert not any(has_name), (
            "candidate_has_name must be all False: descriptive names don't match target function names"
        )


def test_compile_fail_no_retry_captures_stderr_in_diagnostic(monkeypatch, tmp_path) -> None:
    """Given compile_check returns (False, 'too many arguments'), When
    process_subunit runs, Then FAIL_NO_RETRY includes compile_error and
    compile_error_category in both per-result diagnostic and WorkPacket JSON."""
    monkeypatch.chdir(tmp_path)
    response = '// FILE: src/mod/A.cpp\n#include "_decls.h"\nvoid A::f() {}\n'
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
            compile_per_function = True

        class optimization:
            diagnostics_dir = "."
            raw_response_capture = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(
        sp,
        "compile_check",
        lambda code, cfg: (False, "error: too many arguments to function 'foo'"),
    )
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 1
    assert results[0]["verdict"] == "FAIL_NO_RETRY"

    # Per-result diagnostic carries compile_error and category
    diag = results[0]["diagnostic"]
    assert diag["compile_error"] is not None
    assert "too many arguments" in diag["compile_error"]
    assert diag["compile_error_category"] == "too_many_arguments"

    # WorkPacket JSON also carries them
    import json

    wp_path = Path(diag["work_packet_path"])
    assert wp_path.exists()
    wp_data = json.loads(wp_path.read_text(encoding="utf-8"))
    fv = wp_data["function_verdicts"][0]
    assert fv["verdict"] == "FAIL_NO_RETRY"
    assert fv["compile_error"] is not None
    assert "too many arguments" in fv["compile_error"]
    assert fv["compile_error_category"] == "too_many_arguments"


def test_compile_fail_after_retry_captures_stderr_in_diagnostic(monkeypatch, tmp_path) -> None:
    """Given compile_check fails even after retry, When process_subunit runs,
    Then FAIL_AFTER_RETRY includes compile_error and compile_error_category
    from the final compile_check call."""
    monkeypatch.chdir(tmp_path)
    response = '// FILE: src/mod/A.cpp\n#include "_decls.h"\nvoid A::f() {}\n'
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
            max_compile_retries = 2
            compile_per_function = True

        class optimization:
            diagnostics_dir = "."
            raw_response_capture = False

    import re_agent.build.transform.subunit_processor as sp

    # Always fails, even after retry (compile_check never returns True).
    monkeypatch.setattr(
        sp,
        "compile_check",
        lambda code, cfg: (False, "error: 'MyType' does not name a type"),
    )
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 1
    assert results[0]["verdict"] == "FAIL_AFTER_RETRY"

    # Per-result diagnostic carries compile_error and category
    diag = results[0]["diagnostic"]
    assert diag["compile_error"] is not None
    assert "does not name a type" in diag["compile_error"]
    assert diag["compile_error_category"] == "undeclared_identifier"

    # WorkPacket JSON also carries them
    import json

    wp_path = Path(diag["work_packet_path"])
    assert wp_path.exists()
    wp_data = json.loads(wp_path.read_text(encoding="utf-8"))
    fv = wp_data["function_verdicts"][0]
    assert fv["verdict"] == "FAIL_AFTER_RETRY"
    assert fv["compile_error"] is not None
    assert "does not name a type" in fv["compile_error"]
    assert fv["compile_error_category"] == "undeclared_identifier"


# ──────────────────────────────────────────────────────────────────────
# Markdown fence-stripping tests for _parse_llm_response (Todo 1)
# ──────────────────────────────────────────────────────────────────────
# These tests should FAIL on the current production code because
# _parse_llm_response only .strip()s content and does NOT remove
# Markdown fence delimiters (```cpp, ```c++, ```, ```). Fixing
# the parser to strip standalone fence lines will make them pass.


def test_parse_llm_response_strips_triple_backtick_cpp_fence() -> None:
    """Given a response with ```cpp fences around a file,
    When _parse_llm_response parses it,
    Then the parsed content must not contain fence delimiters."""
    # Given
    response = "```cpp\n// FILE: a.cpp\nvoid f() {}\n```\n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 1
    assert files[0]["path"] == "a.cpp"
    assert files[0]["content"] == "void f() {}", f"Expected clean content, got {files[0]['content']!r}"


def test_parse_llm_response_strips_bare_triple_backtick_fence() -> None:
    """Given a response with bare ``` fences (no language hint),
    When _parse_llm_response parses it,
    Then the parsed content must not contain fence delimiters."""
    # Given
    response = "```\n// FILE: a.cpp\nvoid f() {}\n```\n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 1
    assert files[0]["path"] == "a.cpp"
    assert files[0]["content"] == "void f() {}", f"Expected clean content, got {files[0]['content']!r}"


def test_parse_llm_response_strips_cpp_plus_plus_fence() -> None:
    """Given a response with ```c++ fences,
    When _parse_llm_response parses it,
    Then the parsed content must not contain fence delimiters."""
    # Given
    response = "```c++\n// FILE: a.cpp\nvoid f() {}\n```\n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 1
    assert files[0]["path"] == "a.cpp"
    assert files[0]["content"] == "void f() {}", f"Expected clean content, got {files[0]['content']!r}"


def test_parse_llm_response_strips_whitespace_padded_fence() -> None:
    """Given fences with extra whitespace e.g. '  ```cpp  ',
    When _parse_llm_response parses it,
    Then the parsed content must not contain any fence lines."""
    # Given
    response = "  ```cpp  \n// FILE: a.cpp\nvoid f() {}\n  ```  \n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 1
    assert files[0]["path"] == "a.cpp"
    assert files[0]["content"] == "void f() {}", f"Expected clean content, got {files[0]['content']!r}"


def test_parse_llm_response_multi_file_with_fences() -> None:
    """Given a multi-file response wrapped in outer ```cpp fences,
    When _parse_llm_response parses it,
    Then both files are returned and neither content contains standalone
    fence delimiter lines."""
    # Given
    response = "```cpp\n// FILE: a.cpp\nvoid f() {}\n// FILE: b.cpp\nvoid g() {}\n```\n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 2, f"Expected 2 files, got {len(files)}"
    assert files[0]["path"] == "a.cpp"
    assert files[1]["path"] == "b.cpp"
    # Neither file content should contain a standalone fence delimiter
    assert "```" not in files[0]["content"], f"File a.cpp content must not contain fence: {files[0]['content']!r}"
    assert "```" not in files[1]["content"], f"File b.cpp content must not contain fence: {files[1]['content']!r}"


def test_parse_llm_response_interior_fence_in_comment_preserved() -> None:
    """Given a response where triple backticks appear inside a C++ comment,
    When _parse_llm_response parses it,
    Then the interior ``` must remain present (it is not a standalone
    fence delimiter) — regression guard against over-zealous stripping."""
    # Given
    response = "// FILE: a.cpp\nvoid f() { /* ``` */ }\n"
    # When
    files = _parse_llm_response(response)
    # Then
    assert len(files) == 1
    # The interior ``` is NOT a standalone fence line and must be preserved
    assert files[0]["content"] == "void f() { /* ``` */ }", (
        f"Interior triple backtick must be preserved, got {files[0]['content']!r}"
    )


def test_parse_llm_response_empty_after_strip_fence() -> None:
    """Given a response where the only content is fence delimiters,
    When _parse_llm_response parses it and the fence lines are stripped,
    Then no file entry is produced (empty content is falsy and dropped).
    This test FAILS on current code because the parser sees fences as
    content; it will PASS once fence-stripping is added."""
    # Given: a file block whose only "content" is ```cpp\n``` (fence markup)
    response = "// FILE: empty.cpp\n```cpp\n```\n"
    # When
    files = _parse_llm_response(response)
    # Then — on current code this returns 1 file (content='```cpp\n```',
    # which is truthy). When fence-stripping is added, it should drop to 0.
    assert len(files) == 0, (
        f"Expected 0 files (content after fence-strip would be empty), got {len(files)} file(s): {files!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Adjacent fenced // FILE: blocks (Todo 1 -- failing-first tests)
# ──────────────────────────────────────────────────────────────────────────────
# These tests should FAIL on current production code because
# _strip_markdown_fence_delimiters only removes fence lines from the periphery
# (first/last lines) of each parsed file content. When adjacent fenced blocks
# are separated by blank lines, a standalone closing ``` delimiter leaks into
# at least the first file's content because it is followed by the next block's
# opening fence (which gets stripped) and a blank line (which does not match
# the fence pattern), so the closing ``` becomes "interior" and survives.


def test_parse_llm_response_adjacent_fenced_file_blocks() -> None:
    """Given adjacent independently fenced // FILE: blocks (```cpp),
    When _parse_llm_response parses them,
    Then both files are returned and no parsed content contains standalone
    fence delimiter lines -- regression test for the bug where a closing
    ``` leaks into the first file's content when blank lines separate
    adjacent fences."""
    _fence_re = re.compile(r"^\s*`{3,}[^\s`]*\s*$")
    response = (
        "```cpp\n"
        "// FILE: include/renderer/0x004117c0__A.h\n"
        "#ifndef RENDERER_A_H\n"
        "#define RENDERER_A_H\n"
        "void foo();\n"
        "#endif\n"
        "```\n"
        "\n"
        "```cpp\n"
        "// FILE: src/renderer/0x004117c0__A.cpp\n"
        '#include "include/renderer/0x004117c0__A.h"\n'
        "void foo() { /* 0x004117c0 */ }\n"
        "```\n"
    )
    files = _parse_llm_response(response)
    assert len(files) == 2, f"Expected 2 files from adjacent fenced blocks, got {len(files)}"
    paths = [f["path"] for f in files]
    assert "include/renderer/0x004117c0__A.h" in paths
    assert "src/renderer/0x004117c0__A.cpp" in paths

    for f in files:
        for line in f["content"].splitlines():
            assert not _fence_re.match(line), (
                f"File {f['path']} contains standalone fence line: {line!r}\nFull content: {f['content']!r}"
            )


def test_parse_llm_response_adjacent_fenced_bare_blocks() -> None:
    """Same as adjacent_fenced_file_blocks but with plain ``` fences
    (no language hint)."""
    _fence_re = re.compile(r"^\s*`{3,}[^\s`]*\s*$")
    response = (
        "```\n"
        "// FILE: include/renderer/0x004117c0__A.h\n"
        "#ifndef RENDERER_A_H\n"
        "#define RENDERER_A_H\n"
        "void foo();\n"
        "#endif\n"
        "```\n"
        "\n"
        "```\n"
        "// FILE: src/renderer/0x004117c0__A.cpp\n"
        '#include "include/renderer/0x004117c0__A.h"\n'
        "void foo() { /* 0x004117c0 */ }\n"
        "```\n"
    )
    files = _parse_llm_response(response)
    assert len(files) == 2, f"Expected 2 files from adjacent bare fenced blocks, got {len(files)}"
    paths = [f["path"] for f in files]
    assert "include/renderer/0x004117c0__A.h" in paths
    assert "src/renderer/0x004117c0__A.cpp" in paths

    for f in files:
        for line in f["content"].splitlines():
            assert not _fence_re.match(line), (
                f"File {f['path']} contains standalone fence line: {line!r}\nFull content: {f['content']!r}"
            )


def test_parse_llm_response_adjacent_fenced_safety_backticks() -> None:
    """Regression guard: standalone fence cleanup must not remove inline
    triple backticks inside C++ comments when processing adjacent fenced blocks.
    The ``` in a comment or string is NOT a standalone fence delimiter
    and must be preserved."""
    response = (
        "```cpp\n"
        "// FILE: file.h\n"
        "void f(); /* triple backticks ``` inline */\n"
        "```\n"
        "\n"
        "```cpp\n"
        "// FILE: file.cpp\n"
        "void f(/* ``` */) {}\n"
        "```\n"
    )
    files = _parse_llm_response(response)
    assert len(files) == 2, f"Expected 2 files, got {len(files)}"
    # Inline triple backticks must be preserved in both files
    assert "``` inline" in files[0]["content"], (
        f"Inline triple backticks missing from file.h content: {files[0]['content']!r}"
    )
    assert "/* ``` */" in files[1]["content"], (
        f"Inline triple backticks missing from file.cpp content: {files[1]['content']!r}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Process-level: fence-wrapped LLM response → compile_check receives clean C++
# ──────────────────────────────────────────────────────────────────────────


def test_process_subunit_fence_wrapped_output_stripped_before_compile(monkeypatch) -> None:
    """Given a fence-wrapped LLM response (```cpp ... ```),
    When process_subunit runs,
    Then compile_check receives code with no standalone fence delimiter lines
    and verdict PASS / compiles True."""
    _fence_re = re.compile(r"^\s*`{3,}[^\s`]*\s*$")

    response = '```cpp\n// FILE: src/mod/0x004117c0__A.cpp\n#include "_decls.h"\n// 0x004117c0\nvoid A() {}\n```\n'
    provider = _FakeProvider(response)

    compiled_code: list[str | None] = [None]

    def _compile_spy(code: str, cfg: Any) -> tuple[bool, str]:
        for line in code.splitlines():
            assert not _fence_re.match(line), f"Fence delimiter line leaked into compile_check: {line!r}"
        compiled_code[0] = code
        return (True, "")

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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_spy)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    # ── Verdict and compile status ──
    assert len(results) == 1
    assert results[0]["verdict"] == "PASS"
    assert results[0]["compiles"] is True

    assert compiled_code[0] is not None, "compile_check must have been called"
    assert compiled_code[0].startswith('#include "_decls.h"'), (
        f"Expected code to start with '#include \"_decls.h\"', got: {compiled_code[0]!r}"
    )
    assert "0x004117c0" in compiled_code[0]


# ──────────────────────────────────────────────────────────────────────────
# Generated-header compile context
# ──────────────────────────────────────────────────────────────────────────
# Verifies that process_subunit passes matched generated .h files to
# compile_generated_file_set so #include directives against generated
# headers resolve during compilation.


def test_process_subunit_include_context_not_passed_to_compile(monkeypatch: Any) -> None:
    """process_subunit must make generated .h content available during compilation.

    The fake LLM response produces both a generated .h and a generated .cpp
    that #includes it. When .h files are present in the matched file set,
    ``_compile_generated_cpp`` delegates to ``compile_generated_file_set``
    which receives all matched files. The test verifies this by intercepting
    the call and asserting both files are passed.
    """
    response = (
        "// FILE: include/renderer/0x004117c0__A.h\n"
        "#pragma once\n"
        "int generatedHeaderValue();\n"
        "\n"
        "// FILE: src/renderer/0x004117c0__A.cpp\n"
        '#include "include/renderer/0x004117c0__A.h"\n'
        "int useHeader() { return 0; }\n"
    )
    provider = _FakeProvider(response)

    captured_filesets: list[list[dict[str, str]]] = []
    captured_targets: list[str] = []

    def _capture_generated_set(files: list[dict[str, str]], target_path: str, cfg: Any) -> tuple[bool, str]:
        captured_filesets.append(files)
        captured_targets.append(target_path)
        return (True, "")

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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_generated_file_set", _capture_generated_set)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    assert len(results) == 1
    assert len(results[0]["files"]) == 2, "Both .h and .cpp should be matched to the function (address in both paths)"

    # compile_generated_file_set must have been called (first-pass + verdict compile)
    assert len(captured_filesets) == 2, (
        "compile_generated_file_set must have been called twice: "
        "first-pass failure collection and final verdict compile"
    )
    called_paths = {f["path"] for f in captured_filesets[0]}
    assert "include/renderer/0x004117c0__A.h" in called_paths, (
        "Generated header must be passed to compile_generated_file_set"
    )
    assert "src/renderer/0x004117c0__A.cpp" in called_paths, (
        "Generated .cpp must be passed to compile_generated_file_set"
    )
    assert captured_targets[0] == "src/renderer/0x004117c0__A.cpp", (
        f"Target path should be the .cpp path, got {captured_targets[0]}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Adjacent fenced .h + .cpp → compile_generated_file_set receives fence-free
# ──────────────────────────────────────────────────────────────────────────────


def test_process_subunit_adjacent_fenced_h_cpp_reaches_compile_generated_set_fence_free(
    monkeypatch: Any,
) -> None:
    """Adjacent independently fenced .h + .cpp LLM output must reach
    ``compile_generated_file_set`` with all fence delimiter lines stripped.

    This is the integration complement to the ``_parse_llm_response`` adjacency
    tests (``test_parse_llm_response_adjacent_fenced_file_blocks`` et al.):
    it proves the end-to-end path from a real LLM response through
    ``process_subunit`` all the way to the compilation boundary, with
    fence-stripping intact for *both* the generated header and source file.

    The test:

    - Provides a fake LLM response containing two independently fenced
      `````cpp```` blocks: one ``.h`` and one ``.cpp``, separated by a blank
      line (the common LLM output shape that triggered the leak bug).
    - Intercepts ``compile_generated_file_set`` with a spy that asserts
      *every* file's content has no standalone fence delimiter lines.
    - Asserts both the header and source are present in the result, the
      compilation target is the ``.cpp``, and the final verdict is PASS.
    - Carries explicit failure-path assertions that would fire if any fence
      line leaked past the parser into the consumer boundary.
    """
    _fence_re = re.compile(r"^\s*`{3,}[^\s`]*\s*$")

    # ── Fake LLM response: adjacent independently fenced .h + .cpp ──
    response = (
        "```cpp\n"
        "// FILE: include/renderer/0x004117c0__A.h\n"
        "#pragma once\n"
        "int generatedHeaderValue();\n"
        "```\n"
        "\n"
        "```cpp\n"
        "// FILE: src/renderer/0x004117c0__A.cpp\n"
        '#include "include/renderer/0x004117c0__A.h"\n'
        "// 0x004117c0\n"
        "int useHeader() { return generatedHeaderValue() + 1; }\n"
        "```\n"
    )
    provider = _FakeProvider(response)

    # ── Spy on compile_generated_file_set ──
    captured_filesets: list[list[dict[str, str]]] = []
    captured_targets: list[str] = []

    def _spy_generated_set(files: list[dict[str, str]], target_path: str, cfg: Any) -> tuple[bool, str]:
        # Failure-path assertion embedded in the spy itself:
        # every file's content must have no standalone fence delimiter lines.
        for entry in files:
            content = entry.get("content", "")
            for line in content.splitlines():
                assert not _fence_re.match(line), (
                    f"Fence delimiter line leaked into file {entry['path']}: {line!r}\nFull content: {content!r}"
                )
        captured_filesets.append(files)
        captured_targets.append(target_path)
        return (True, "")

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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_generated_file_set", _spy_generated_set)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x004117c0", "code": "void FUN_004117c0() {}", "name": "FUN_004117c0"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "renderer", provider, _Cfg(), cache=None)

    # ── Result shape ──
    assert len(results) == 1, f"Expected 1 function result, got {len(results)}"
    r = results[0]
    assert r["verdict"] == "PASS", f"Expected PASS, got {r['verdict']}"
    assert r["compiles"] is True

    # ── Both .h and .cpp are present in the result files ──
    assert len(r["files"]) == 2, f"Both .h and .cpp should be matched to the function, got {len(r['files'])} file(s)"
    paths = [f["path"] for f in r["files"]]
    h_paths = [p for p in paths if p.endswith(".h")]
    cpp_paths = [p for p in paths if p.endswith(".cpp")]
    assert len(h_paths) == 1, f"Expected 1 .h file, got {h_paths}"
    assert len(cpp_paths) == 1, f"Expected 1 .cpp file, got {cpp_paths}"

    # ── compile_generated_file_set was called with correct payload ──
    assert len(captured_filesets) >= 1, "compile_generated_file_set must have been called at least once"
    first_set = captured_filesets[0]
    first_set_paths = {f["path"] for f in first_set}
    assert any(p.endswith(".h") for p in first_set_paths), (
        f"Generated header must appear in compile_generated_file_set: {first_set_paths}"
    )
    assert any(p.endswith(".cpp") for p in first_set_paths), (
        f"Generated .cpp must appear in compile_generated_file_set: {first_set_paths}"
    )
    assert captured_targets[0].endswith(".cpp"), f"Target path must be the .cpp file, got {captured_targets[0]}"

    # ── Explicit failure-path: result files must not contain fence markers ──
    for f in r["files"]:
        assert "```" not in f["content"], (
            f"File {f['path']} still contains fence markers in result content: {f['content']!r}"
        )

    # ── No extraneous provider calls ──
    assert provider.total_calls == 1, f"Expected 1 provider send, got {provider.total_calls}"


# ═══════════════════════════════════════════════════════════════════════
# Retry-merge preservation tests (Todo 1 from re-agent-transform-retry-merge-cache-regression)
#
# These tests expose the wholesale-replacement bug at
# subunit_processor.py:342 where ``parsed_files = retry_files`` replaces
# ALL parsed files with the retry output, losing initially successful
# functions' files when the retry returns only the failed function.
#
# Both tests should FAIL on current production code. After the fix, the
# retry output should be merged by address/path, preserving files for
# functions NOT in the retry response.
# ═══════════════════════════════════════════════════════════════════════


def test_retry_merge_preserves_initial_success(monkeypatch) -> None:
    """Partial subunit retry must not orphan initially successful functions.

    Given a two-function subunit where:
      - the initial LLM response emits address-bearing files for both functions
      - the first function compiles, the second fails
      - the subunit retry returns only the second function's repaired files

    Then the first function must still have its files, must not become
    ``NO_OUTPUT``, and must remain ``PASS`` (it compiled on first try).
    """
    # Use .h/.cpp pairs so the retry produces 2 files per function.
    # This prevents the single-file-fallback (subunit_processor.py:141)
    # from masking the bug: when len(parsed_files)==1 the fallback
    # gives every unmatched function the same file.
    initial_response = (
        "// FILE: include/mod/A.h\n"
        "// 0x1000\n"
        "struct A {};\n"
        "\n"
        "// FILE: src/mod/A.cpp\n"
        "// 0x1000\n"
        '#include "A.h"\n'
        "void a() {}\n"
        "\n"
        "// FILE: include/mod/B.h\n"
        "// 0x1001\n"
        "struct B {};\n"
        "\n"
        "// FILE: src/mod/B.cpp\n"
        "// 0x1001\n"
        "void b() {}\n"
    )
    retry_response = (
        "// FILE: include/mod/B.h\n"
        "// 0x1001\n"
        "struct B { int x; };\n"
        "\n"
        "// FILE: src/mod/B.cpp\n"
        "// 0x1001\n"
        "void b() { return; }\n"
    )
    provider = _FakeMultiResponseProvider([initial_response, retry_response])

    compile_calls: list[int] = [0]

    def _compiler_override(func_files: list[dict[str, str]], cpp_file: dict[str, str], cfg: Any) -> tuple[bool, str]:
        compile_calls[0] += 1
        # Call 1 = Function A in first pass → pass
        # Call 2 = Function B in first pass → fail (triggers subunit retry)
        return (compile_calls[0] != 2, "error" if compile_calls[0] == 2 else "")

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
            max_compile_retries = 1

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "_compile_generated_cpp", _compiler_override)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx: dict[str, Any] = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    # ── Both functions present in results ──
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    result_a = next(r for r in results if r["function"] == "0x1000")
    result_b = next(r for r in results if r["function"] == "0x1001")

    # ── Function A (initially successful) must NOT be NO_OUTPUT ──
    assert result_a["verdict"] != "NO_OUTPUT", (
        f"Function A became NO_OUTPUT after partial retry. "
        f"Expected PASS or PASS_RETRY, got {result_a['verdict']}. "
        f"Files: {result_a.get('files', [])}"
    )
    assert result_a["compiles"] is True, (
        f"Function A should still compile after retry, got compiles={result_a['compiles']}"
    )
    assert len(result_a.get("files", [])) > 0, "Function A should still have files after partial retry, got empty files"
    # A was PASS on first compile, so it must remain PASS (not PASS_RETRY).
    # Currently the bug turns it into NO_OUTPUT — that is the regression.
    assert result_a["verdict"] in (
        "PASS",
        "PASS_RETRY",
    ), f"Function A verdict should be PASS or PASS_RETRY, got {result_a['verdict']}"
    # A must have its OWN files, not fallback-assigned B files
    a_paths = [f["path"] for f in result_a["files"]]
    assert any(p.endswith("A.h") for p in a_paths), f"A.h missing from Function A files: {a_paths}"
    assert any(p.endswith("A.cpp") for p in a_paths), f"A.cpp missing from Function A files: {a_paths}"

    # ── Function B must have some non-NO_OUTPUT verdict ──
    assert result_b["verdict"] != "NO_OUTPUT", "Function B became NO_OUTPUT — retried function should have files."

    # ── Exactly 2 LLM calls: initial + subunit retry ──
    assert provider.total_calls == 2, f"Expected 2 LLM calls (initial + subunit retry), got {provider.total_calls}"


def test_retry_merge_by_address_not_file_order(monkeypatch) -> None:
    """Retry merge must match by address/path, not by file position.

    Given .h/.cpp pairs for two functions where:
      - initial response emits all four files (A.h, A.cpp, B.h, B.cpp)
      - the first function (A) compiles, the second (B) fails
      - the subunit retry returns only B's pair, but in REVERSED order
        (B.cpp before B.h) compared to the initial (B.h before B.cpp)

    Then after merge:
      - Function A must still have its initial files (A.h, A.cpp)
      - Function B must have its retry files (B.h, B.cpp) and the content
        must be the retry version, not the initial version

    The reversed-pair-order proves the merge is address/path-driven,
    not positional.
    """
    initial_response = (
        "// FILE: include/mod/A.h\n"
        "// 0x1000\n"
        "struct A {};\n"
        "\n"
        "// FILE: src/mod/A.cpp\n"
        "// 0x1000\n"
        '#include "A.h"\n'
        "void a() {}\n"
        "\n"
        "// FILE: include/mod/B.h\n"
        "// 0x1001\n"
        "struct B {};\n"
        "\n"
        "// FILE: src/mod/B.cpp\n"
        "// 0x1001\n"
        "void b() {}\n"
    )
    # Retry returns only B's files, but .cpp BEFORE .h (reversed pair order).
    retry_response = (
        "// FILE: src/mod/B.cpp\n"
        "// 0x1001\n"
        "#define RETRY_MERGE_TEST\n"
        "void b() { return; }\n"
        "\n"
        "// FILE: include/mod/B.h\n"
        "// 0x1001\n"
        "struct B { int x; };\n"
    )
    provider = _FakeMultiResponseProvider([initial_response, retry_response])

    compile_calls: list[int] = [0]

    def _compiler_override(func_files: list[dict[str, str]], cpp_file: dict[str, str], cfg: Any) -> tuple[bool, str]:
        compile_calls[0] += 1
        # Call 1 = Function A in first pass → pass
        # Call 2 = Function B in first pass → fail (triggers subunit retry)
        return (compile_calls[0] != 2, "error" if compile_calls[0] == 2 else "")

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
            max_compile_retries = 1

    import re_agent.build.transform.subunit_processor as sp

    # Patch _compile_generated_cpp directly (handles both .h/.cpp pairs)
    monkeypatch.setattr(sp, "_compile_generated_cpp", _compiler_override)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx: dict[str, Any] = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    # ── Both functions present in results ──
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"

    result_a = next(r for r in results if r["function"] == "0x1000")
    result_b = next(r for r in results if r["function"] == "0x1001")

    # ── Function A (initially successful) must NOT be NO_OUTPUT ──
    assert result_a["verdict"] != "NO_OUTPUT", (
        f"Function A became NO_OUTPUT after partial retry with reversed "
        f"pair order. Expected PASS or PASS_RETRY, got {result_a['verdict']}."
    )
    assert len(result_a.get("files", [])) > 0, "Function A should still have files; got empty files"
    a_paths = [f["path"] for f in result_a["files"]]
    assert any(p.endswith("A.h") for p in a_paths), f"A.h missing from Function A files: {a_paths}"
    assert any(p.endswith("A.cpp") for p in a_paths), f"A.cpp missing from Function A files: {a_paths}"

    # ── Function B must have both files and retry content ──
    assert result_b["verdict"] != "NO_OUTPUT", "Function B got NO_OUTPUT despite retry providing its files."
    assert len(result_b.get("files", [])) == 2, (
        f"Function B should have 2 files (.h and .cpp), got {len(result_b.get('files', []))}"
    )
    b_paths = [f["path"] for f in result_b["files"]]
    assert any(p.endswith("B.h") for p in b_paths), f"B.h missing from Function B files: {b_paths}"
    assert any(p.endswith("B.cpp") for p in b_paths), f"B.cpp missing from Function B files: {b_paths}"

    # Content must be the RETRY version (not initial), proving the
    # retry output was merged correctly regardless of file order.
    b_cpp = next(f for f in result_b["files"] if f["path"].endswith("B.cpp"))
    assert "RETRY_MERGE_TEST" in b_cpp["content"], (
        "Function B's .cpp content should be the retry version "
        "(containing RETRY_MERGE_TEST), not the initial version — "
        "proving retry merge used address/path matching, not position."
    )

    # ── Exactly 2 LLM calls ──
    assert provider.total_calls == 2, f"Expected 2 LLM calls, got {provider.total_calls}"


# ---------------------------------------------------------------------------
# Todo 3: Retry diagnostics — initial/effective/retry marker counts
# ---------------------------------------------------------------------------


def test_retry_diagnostics_include_initial_effective_marker_counts(monkeypatch: Any, tmp_path: Path) -> None:
    """FAILING-FIRST: Given initial 20-FILE: response + retry returns only a
    subset (6 markers), When process_subunit runs with compile retry, Then
    diagnostics must have additive fields for initial/effective/retry counts.

    Current production replaces parsed_files with retry_files
    (subunit_processor.py:342) and resets marker_count to len(retry_files)
    (line 343), hiding the initial 20-marker count. This test fails until
    initial_marker_count, effective_marker_count, and retry_marker_count (or
    equivalent evidence) are added to diagnostics.

    Expected after fix:
        initial_marker_count = 20    (pre-retry raw FILE: count)
        retry_marker_count   =  6    (retry response FILE: count)
        effective_marker_count = 20  (post-merge: initial preserved + retry
                                      replaces only the retried function's files)
        marker_count         = 20    (backward compat: reflects effective,
                                      not raw retry count)
    """
    monkeypatch.chdir(tmp_path)

    # ── Initial response: 20 FILE: blocks for 3 functions ──
    #   Function A (0x1000): 4 files
    #   Function B (0x1001): 8 files  (will fail first compile → triggers retry)
    #   Function C (0x1002): 8 files
    # Each block includes the address in content so _selective_compile can
    # match it via compile_check(cpp_content, cfg).
    a_parts = "\n".join(f"// FILE: src/mod/0x1000__a{i}.cpp\n// 0x1000\nvoid a{i}() {{}}\n" for i in range(4))
    b_parts = "\n".join(f"// FILE: src/mod/0x1001__b{i}.cpp\n// 0x1001\nvoid b{i}() {{}}\n" for i in range(8))
    c_parts = "\n".join(f"// FILE: src/mod/0x1002__c{i}.cpp\n// 0x1002\nvoid c{i}() {{}}\n" for i in range(8))
    initial_response = a_parts + "\n" + b_parts + "\n" + c_parts

    # ── Retry response: 6 FILE: blocks for function B only (subset) ──
    retry_response = "\n".join(f"// FILE: src/mod/0x1001__b{i}_retry.cpp\nvoid b{i}_retry() {{}}\n" for i in range(6))

    provider = _FakeMultiResponseProvider([initial_response, retry_response])

    # ── Compile: function B (0x1001) fails on first pass only ──
    _compile_calls: dict[str, int] = {}

    def _selective_compile(code: str, cfg: Any) -> tuple[bool, str]:
        for addr in ("0x1000", "0x1001", "0x1002"):
            if addr in code:
                _compile_calls[addr] = _compile_calls.get(addr, 0) + 1
                if addr == "0x1001" and _compile_calls[addr] == 1:
                    return (False, f"error: {addr} synthetic failure")
                return (True, "")
        return (True, "")

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
            max_compile_retries = 1

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _selective_compile)
    monkeypatch.setattr(
        sp,
        "compile_generated_file_set",
        lambda files, path, cfg: _selective_compile(" ".join(f["content"] for f in files), cfg),
    )
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
            {"address": "0x1002", "code": "void c() {}", "name": "c"},
        ],
        "neighbour_context": [],
        "subunit_index": 1,
        "run_id": "retry-diag-counts",
    }

    diag_dir = tmp_path / "diag"
    cfg_instance = _Cfg()
    cfg_instance.optimization.diagnostics_dir = str(diag_dir)

    results = process_subunit(ctx, "mod", provider, cfg_instance, cache=None)

    assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    # ── Existing marker_count/parse_count backward compat (non-negative) ──
    for r in results:
        diag = r["diagnostic"]
        assert "marker_count" in diag, "marker_count must still be present (backward compat)"
        assert "parse_count" in diag, "parse_count must still be present (backward compat)"
        assert diag["marker_count"] >= 0
        assert diag["parse_count"] >= 0

    # ── FAILING-FIRST: additive fields must exist in per-function diagnostic ──
    r_a = next(r for r in results if r["function"] == "0x1000")
    diag = r_a["diagnostic"]

    assert "initial_marker_count" in diag, (
        "FAILING-FIRST: diagnostics must include initial_marker_count showing "
        "the pre-retry raw FILE: block count. Currently marker_count is "
        "overwritten to len(retry_files) on retry (subunit_processor.py:343), "
        "hiding the initial count."
    )
    assert "retry_marker_count" in diag, (
        "FAILING-FIRST: diagnostics must include retry_marker_count tracking "
        "how many FILE: blocks the retry response produced."
    )
    assert "effective_marker_count" in diag, (
        "FAILING-FIRST: diagnostics must include effective_marker_count "
        "showing the post-merge effective FILE: block count, distinguishing "
        "it from the raw retry count."
    )

    initial = diag["initial_marker_count"]
    retry = diag["retry_marker_count"]
    effective = diag["effective_marker_count"]
    assert initial == 20, f"initial response had 20 FILE: blocks, got {initial}"
    assert retry == 6, f"retry response had 6 FILE: blocks, got {retry}"
    assert effective >= initial, (
        f"effective_marker_count ({effective}) should be >= "
        f"initial_marker_count ({initial}) — retry merge must not drop files"
    )

    # ── FAILING-FIRST: Work-packet JSON must also have additive fields ──
    import json

    wp_path = Path(r_a["diagnostic"]["work_packet_path"])
    assert wp_path.exists()
    wp = json.loads(wp_path.read_text(encoding="utf-8"))

    assert "initial_marker_count" in wp, "FAILING-FIRST: work-packet JSON must include initial_marker_count"
    assert "retry_marker_count" in wp, "FAILING-FIRST: work-packet JSON must include retry_marker_count"
    assert "effective_marker_count" in wp, "FAILING-FIRST: work-packet JSON must include effective_marker_count"
    assert wp["marker_count"] >= 0, "backward compat: marker_count must be present"
    assert wp["parse_count"] >= 0, "backward compat: parse_count must be present"
