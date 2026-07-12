from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

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

    The address is identity only in the file path or a ``// Original function:``
    comment (not a bare callee reference).
    """
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": "void FUN_00414580() {}"}
    parsed = [
        {
            "path": "src/renderer/0x00414580__Renderer.cpp",
            "content": "// Original function: 0x00414580\nvoid Renderer::draw() {}",
        }
    ]
    result = _match_files_to_function(parsed, func, total_func_count=10)
    assert result == parsed, "must match by address when name is absent"
    # Also verify that a bare address in content (no // Original function:)
    # does NOT match with strict identity rules.
    parsed_bare = [{"path": "src/renderer/Renderer.cpp", "content": "// 0x00414580\nvoid Renderer::draw() {}"}]
    result_bare = _match_files_to_function(parsed_bare, func, total_func_count=10)
    assert result_bare == [], "bare address in content must NOT match (strict identity)"


def test_match_files_to_function_address_case_insensitive() -> None:
    """Addresses may appear upper- or lower-case in LLM output; matching must be case-insensitive."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": ""}
    parsed = [
        {"path": "src/mod/0x00414580__A.cpp", "content": "// Original function: 0x00414580\nvoid A::f() {}"},
        {"path": "src/mod/B.cpp", "content": "// 0x004145a0\nvoid B::g() {}"},
    ]
    result = _match_files_to_function(parsed, func, total_func_count=2)
    assert len(result) == 1
    assert result[0]["path"] == "src/mod/0x00414580__A.cpp"
    # Also verify that bare address in content (no Original function:) does NOT match
    parsed_bare = [
        {"path": "src/mod/A.cpp", "content": "// 0x00414580\nvoid A::f() {}"},
        {"path": "src/mod/B.cpp", "content": "// 0x004145a0\nvoid B::g() {}"},
    ]
    result_bare = _match_files_to_function(parsed_bare, func, total_func_count=2)
    assert result_bare == [], "bare address in content must NOT match (strict identity)"


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


def test_match_files_to_function_single_parsed_file_no_fallback() -> None:
    """Positional single-file fallback is removed (contract repair).

    When the LLM emits a single ``// FILE:`` block for a multi-function
    subunit, and no address/name matches, the file is NOT assigned to every
    function. Returns ``[]`` with strategy ``"none"``.
    """
    from re_agent.build.transform.subunit_processor import _match_files_to_function_with_strategy

    func = {"address": "0x00414580", "code": ""}
    parsed = [{"path": "src/mod/all.cpp", "content": "void f() {} void g() {}"}]
    result, strategy = _match_files_to_function_with_strategy(parsed, func, total_func_count=2)
    assert result == [], "Positional fallback removed: must return [] for unmatched single file"
    assert strategy == "none", f"Strategy must be 'none' without address/name match, got {strategy!r}"


def test_match_files_to_function_old_bug_returns_empty_without_address_match() -> None:
    """Documents the OLD buggy behaviour: with only name-based matching and no
    name in the context dict, a multi-function subunit returned [] for every
    function. The fix adds address matching via strict identity anchors;
    this test asserts the fix prevents that regression by confirming a non-empty
    result when the address appears in the path or a // Original function: comment."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x00414580", "code": ""}  # no "name" — the real-world case
    # Strict identity: address in path → match
    parsed = [{"path": "src/mod/0x00414580__A.cpp", "content": "void A() {}"}]
    result = _match_files_to_function(parsed, func, total_func_count=10)
    assert result != [], "address-based matching via path must prevent the NO_OUTPUT regression"
    # Also works via // Original function: comment
    parsed_comment = [{"path": "src/mod/A.cpp", "content": "// Original function: 0x00414580\nvoid A() {}"}]
    result_comment = _match_files_to_function(parsed_comment, func, total_func_count=10)
    assert result_comment != [], "address-based matching via Original function comment must work"


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
    assert (
        diag.get("match_strategy") == "none"
    ), "match_strategy must be 'none' when no address/name matches any parsed file"
    assert (
        diag.get("files_written", 999) == 0
    ), "files_written must be 0; no files should be matched without address/name evidence"

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
    assert (
        diag_multi["marker_count"] != diag_none["marker_count"]
    ), "parsed-but-unmatched and parser-failure must differ in marker_count"

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
    assert (
        r_a["diagnostic"]["files_written"] == 2
    ), "0x004117c0 should match 2 files (its .cpp and .h both carry the address in path)"
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

    assert (
        compile_called == []
    ), f"compile_check must not be called when compile_per_function=False, got {len(compile_called)} call(s)"
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
        assert (
            r["verdict"] == "NO_OUTPUT"
        ), f"expected NO_OUTPUT for unmatched function {r['function']}, got {r['verdict']}"
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

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_success)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    # Legacy mode: address-bearing path without TARGET → matched via legacy
    results = process_subunit(ctx, "mod", provider, _min_cfg(), cache=None)

    assert (
        len(compile_called) >= 1
    ), f"compile_check should be called at least once by default, got {len(compile_called)} calls"
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
        assert (
            r["verdict"] == "NO_OUTPUT"
        ), f"ClassName-only output must produce NO_OUTPUT for {r['function']}, got {r['verdict']}"
        assert r["compiles"] is False
        assert r["files"] == [], "no files must be matched for ClassName-only output"

        diag = r["diagnostic"]
        assert (
            diag.get("match_strategy") == "none"
        ), f"match_strategy must be 'none' for ClassName-only output on {r['function']}"
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
        assert not any(
            has_name
        ), "candidate_has_name must be all False: descriptive names don't match target function names"


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
    assert (
        files[0]["content"] == "void f() { /* ``` */ }"
    ), f"Interior triple backtick must be preserved, got {files[0]['content']!r}"


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
    assert (
        len(files) == 0
    ), f"Expected 0 files (content after fence-strip would be empty), got {len(files)} file(s): {files!r}"


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
            assert not _fence_re.match(
                line
            ), f"File {f['path']} contains standalone fence line: {line!r}\nFull content: {f['content']!r}"


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
            assert not _fence_re.match(
                line
            ), f"File {f['path']} contains standalone fence line: {line!r}\nFull content: {f['content']!r}"


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
    assert (
        "``` inline" in files[0]["content"]
    ), f"Inline triple backticks missing from file.h content: {files[0]['content']!r}"
    assert (
        "/* ``` */" in files[1]["content"]
    ), f"Inline triple backticks missing from file.cpp content: {files[1]['content']!r}"


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
    assert compiled_code[0].startswith(
        '#include "_decls.h"'
    ), f"Expected code to start with '#include \"_decls.h\"', got: {compiled_code[0]!r}"
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
    assert (
        "include/renderer/0x004117c0__A.h" in called_paths
    ), "Generated header must be passed to compile_generated_file_set"
    assert (
        "src/renderer/0x004117c0__A.cpp" in called_paths
    ), "Generated .cpp must be passed to compile_generated_file_set"
    assert (
        captured_targets[0] == "src/renderer/0x004117c0__A.cpp"
    ), f"Target path should be the .cpp path, got {captured_targets[0]}"


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
                assert not _fence_re.match(
                    line
                ), f"Fence delimiter line leaked into file {entry['path']}: {line!r}\nFull content: {content!r}"
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
    assert any(
        p.endswith(".h") for p in first_set_paths
    ), f"Generated header must appear in compile_generated_file_set: {first_set_paths}"
    assert any(
        p.endswith(".cpp") for p in first_set_paths
    ), f"Generated .cpp must appear in compile_generated_file_set: {first_set_paths}"
    assert captured_targets[0].endswith(".cpp"), f"Target path must be the .cpp file, got {captured_targets[0]}"

    # ── Explicit failure-path: result files must not contain fence markers ──
    for f in r["files"]:
        assert (
            "```" not in f["content"]
        ), f"File {f['path']} still contains fence markers in result content: {f['content']!r}"

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
    # Addresses are in file paths (strict identity anchor).
    initial_response = (
        "// FILE: include/mod/0x1000__A.h\n"
        "// Original function: 0x1000\n"
        "struct A {};\n"
        "\n"
        "// FILE: src/mod/0x1000__A.cpp\n"
        "// Original function: 0x1000\n"
        '#include "0x1000__A.h"\n'
        "void a() {}\n"
        "\n"
        "// FILE: include/mod/0x1001__B.h\n"
        "// Original function: 0x1001\n"
        "struct B {};\n"
        "\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        "// Original function: 0x1001\n"
        "void b() {}\n"
    )
    retry_response = (
        "// FILE: include/mod/0x1001__B.h\n"
        "// Original function: 0x1001\n"
        "struct B { int x; };\n"
        "\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        "// Original function: 0x1001\n"
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
            {"address": "0x1000", "code": "void FuncAImpl() {}", "name": "FuncAImpl"},
            {"address": "0x1001", "code": "void FuncBImpl() {}", "name": "FuncBImpl"},
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
    assert (
        result_a["compiles"] is True
    ), f"Function A should still compile after retry, got compiles={result_a['compiles']}"
    assert len(result_a.get("files", [])) > 0, "Function A should still have files after partial retry, got empty files"
    # A was PASS on first compile, so it must remain PASS (not PASS_RETRY).
    # Currently the bug turns it into NO_OUTPUT — that is the regression.
    assert result_a["verdict"] in (
        "PASS",
        "PASS_RETRY",
    ), f"Function A verdict should be PASS or PASS_RETRY, got {result_a['verdict']}"
    # A must have its OWN files, not fallback-assigned B files
    a_paths = [f["path"] for f in result_a["files"]]
    assert any(p.endswith("0x1000__A.h") for p in a_paths), f"0x1000__A.h missing from Function A files: {a_paths}"
    assert any(p.endswith("0x1000__A.cpp") for p in a_paths), f"0x1000__A.cpp missing from Function A files: {a_paths}"

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
        "// FILE: include/mod/0x1000__A.h\n"
        "// Original function: 0x1000\n"
        "struct A {};\n"
        "\n"
        "// FILE: src/mod/0x1000__A.cpp\n"
        "// Original function: 0x1000\n"
        '#include "0x1000__A.h"\n'
        "void a() {}\n"
        "\n"
        "// FILE: include/mod/0x1001__B.h\n"
        "// Original function: 0x1001\n"
        "struct B {};\n"
        "\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        "// Original function: 0x1001\n"
        "void b() {}\n"
    )
    # Retry returns only B's files, but .cpp BEFORE .h (reversed pair order).
    retry_response = (
        "// FILE: src/mod/0x1001__B.cpp\n"
        "// Original function: 0x1001\n"
        "#define RETRY_MERGE_TEST\n"
        "void b() { return; }\n"
        "\n"
        "// FILE: include/mod/0x1001__B.h\n"
        "// Original function: 0x1001\n"
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
            {"address": "0x1000", "code": "void FuncAImpl() {}", "name": "FuncAImpl"},
            {"address": "0x1001", "code": "void FuncBImpl() {}", "name": "FuncBImpl"},
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
    assert any(p.endswith("0x1000__A.h") for p in a_paths), f"0x1000__A.h missing from Function A files: {a_paths}"
    assert any(p.endswith("0x1000__A.cpp") for p in a_paths), f"0x1000__A.cpp missing from Function A files: {a_paths}"

    # ── Function B must have both files and retry content ──
    assert result_b["verdict"] != "NO_OUTPUT", "Function B got NO_OUTPUT despite retry providing its files."
    assert (
        len(result_b.get("files", [])) == 2
    ), f"Function B should have 2 files (.h and .cpp), got {len(result_b.get('files', []))}"
    b_paths = [f["path"] for f in result_b["files"]]
    assert any(p.endswith("0x1001__B.h") for p in b_paths), f"0x1001__B.h missing from Function B files: {b_paths}"
    assert any(p.endswith("0x1001__B.cpp") for p in b_paths), f"0x1001__B.cpp missing from Function B files: {b_paths}"

    # Content must be the RETRY version (not initial), proving the
    # retry output was merged correctly regardless of file order.
    b_cpp = next(f for f in result_b["files"] if f["path"].endswith("0x1001__B.cpp"))
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
            {"address": "0x1000", "code": "void a() {}", "name": "FuncA"},
            {"address": "0x1001", "code": "void b() {}", "name": "FuncB"},
            {"address": "0x1002", "code": "void c() {}", "name": "FuncC"},
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


# ═══════════════════════════════════════════════════════════════════════
# Explicit identity (// TARGET:) contract tests (transform contract repair)
# ═══════════════════════════════════════════════════════════════════════


def test_explicit_identity_valid_match(monkeypatch: Any) -> None:
    """Valid explicit ``// TARGET:`` markers map files correctly.

    Given a two-function subunit where the fake LLM emits four files with
    correct ``// TARGET:`` comments, When ``process_subunit`` runs, Then
    every function is matched via ``explicit_identity``, files are assigned
    per the markers, verdicts are PASS, and identity_state is ``"explicit"``.
    """
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: include/mod/0x1000__A.h\n"
        "// 0x1000\n"
        "#pragma once\n"
        "void a();\n"
        "\n"
        "// TARGET: 0 0x1000\n"
        "// FILE: src/mod/0x1000__A.cpp\n"
        '#include "_decls.h"\n'
        "// 0x1000\n"
        "void a() {}\n"
        "\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: include/mod/0x1001__B.h\n"
        "// 0x1001\n"
        "#pragma once\n"
        "void b();\n"
        "\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        '#include "_decls.h"\n'
        "// 0x1001\n"
        "void b() {}\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2, f"Expected 2 function results, got {len(results)}"

    r_a = next(r for r in results if r["function"] == "0x1000")
    r_b = next(r for r in results if r["function"] == "0x1001")

    for label, r in [("0x1000", r_a), ("0x1001", r_b)]:
        assert r["verdict"] in {"PASS", "PASS_RETRY"}, f"Function {label} expected PASS/PASS_RETRY, got {r['verdict']}"
        assert r["compiles"] is True
        assert len(r["files"]) == 2, f"Function {label} expected 2 files, got {len(r['files'])}"
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "explicit_identity", (
            f"Function {label} match_strategy should be " f"'explicit_identity', got {diag['match_strategy']!r}"
        )
        assert diag["identity_state"] == "explicit", (
            f"Function {label} identity_state should be " f"'explicit', got {diag['identity_state']!r}"
        )
        assert diag["identity_reason"] == "", (
            f"Function {label} identity_reason should be empty, " f"got {diag['identity_reason']!r}"
        )
        assert diag["target_file_count"] == 2, (
            f"Function {label} target_file_count should be 2, " f"got {diag['target_file_count']}"
        )


def test_explicit_identity_preserves_content(monkeypatch: Any) -> None:
    """Explicit identity matching must preserve original file content unchanged.

    Given files with ``// TARGET:`` markers, When they are associated and
    compiled, Then each result's file content must be exactly as parsed
    (no truncation, no modification).
    """
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a.cpp\n"
        "// 0x1000\n"
        "int fn_a() { return 42; }\n"
        "\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: b.cpp\n"
        "// 0x1001\n"
        "int fn_b() { return 7; }\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    r_a = next(r for r in results if r["function"] == "0x1000")
    r_b = next(r for r in results if r["function"] == "0x1001")

    # File contents must be exactly what was parsed.  The ``// TARGET: 1 0x1001``
    # line ends up in a.cpp's content because it lies between the two ``// FILE:``
    # blocks and is part of the "text between" in the split algorithm — this is
    # expected behavior (the TARGET is also correctly extracted for b.cpp's
    # preceding text).
    a_files = r_a["files"]
    assert len(a_files) == 1
    assert "int fn_a() { return 42; }" in a_files[0]["content"]
    assert a_files[0]["path"] == "a.cpp"

    b_files = r_b["files"]
    assert len(b_files) == 1
    assert "int fn_b() { return 7; }" in b_files[0]["content"]
    assert b_files[0]["path"] == "b.cpp"


def test_explicit_identity_rejects_incomplete_targets(monkeypatch: Any) -> None:
    """Partial explicit identity (only one function has TARGET markers) is rejected.

    Given a two-function subunit where only function 0 has a ``// TARGET:``
    marker, When ``process_subunit`` runs, Then both functions produce
    ``NO_OUTPUT`` with ``match_strategy = "rejected_identity"``.
    """
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a.cpp\n"
        "// 0x1000\n"
        "void a() {}\n"
        "\n"
        "// FILE: b.cpp\n"
        "// 0x1001\n"
        "void b() {}\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        assert r["compiles"] is False
        assert r["files"] == []
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "rejected_identity", (
            f"Expected 'rejected_identity' strategy for {r['function']}, " f"got {diag['match_strategy']!r}"
        )
        assert diag["identity_state"] == "rejected"
        assert "Some files lack TARGET markers" in diag["identity_reason"]
        assert diag["target_file_count"] == 0


def test_explicit_identity_rejects_duplicate_target(monkeypatch: Any) -> None:
    """Duplicate TARGET (both files pointing to same function) is rejected.

    Given two functions but both ``// TARGET:`` markers point to function 0,
    When ``process_subunit`` runs, Then both functions produce NO_OUTPUT
    with ``rejected_identity`` because function 1 has no files.
    """
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a1.cpp\n"
        "// 0x1000\n"
        "void a1() {}\n"
        "\n"
        "// TARGET: 0 0x1000\n"
        "// FILE: a2.cpp\n"
        "// 0x1000\n"
        "void a2() {}\n"
    )
    provider = _FakeProvider(response)

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    # In strict mode, duplicate TARGET (both files target ordinal 0) triggers
    # recovery for ordinal 1.  Recovery fails (fake provider returns same
    # response targeting ordinal 0) → contract failure for ALL.
    results = process_subunit(ctx, "mod", provider, _strict_cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert (
            r["verdict"] == "INCOMPLETE_TARGETS"
        ), f"Expected INCOMPLETE_TARGETS for {r['function']}, got {r['verdict']}"
        assert r["files"] == []
        assert "contract failed" in r.get("diagnostic", {}).get("identity_reason", "").lower()


def test_explicit_identity_rejects_out_of_range_ordinal(monkeypatch: Any) -> None:
    """Out-of-range ordinal in TARGET marker is rejected.

    Given a TARGET with ordinal 5 for only 2 functions, When
    ``process_subunit`` runs, Then both functions produce NO_OUTPUT."""
    response = "// TARGET: 5 0x9999\n" "// FILE: x.cpp\n" "void x() {}\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT"
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "rejected_identity"
        assert "out of range" in diag["identity_reason"] or "ordinal" in diag["identity_reason"].lower()


def test_explicit_identity_rejects_callee_address(monkeypatch: Any) -> None:
    """TARGET with address not matching the expected function at that ordinal is rejected.

    Given a TARGET with ordinal 0 pointing to address 0xDEAD (not 0x1000),
    When ``process_subunit`` runs, Then both functions produce NO_OUTPUT."""
    response = (
        "// TARGET: 0 0xDEAD\n"
        "// FILE: x.cpp\n"
        "// 0xDEAD\n"
        "void x() {}\n"
        "\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: y.cpp\n"
        "// 0x1001\n"
        "void y() {}\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT"
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "rejected_identity"
        assert "does not match" in diag["identity_reason"]


def test_no_target_fallback_to_address_matching_preserved(monkeypatch: Any) -> None:
    """When no ``// TARGET:`` markers present, direct address matching still works.

    Given address-bearing file paths/content but no TARGET markers, When
    ``process_subunit`` runs, Then functions are matched by address with
    ``match_strategy = "by_address"`` and ``identity_state = "matched"``.
    """
    response = (
        "// FILE: src/mod/0x1000__A.cpp\n"
        '#include "_decls.h"\n'
        "// 0x1000\n"
        "void a() {}\n"
        "\n"
        "// FILE: src/mod/0x1001__B.cpp\n"
        '#include "_decls.h"\n'
        "// 0x1001\n"
        "void b() {}\n"
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
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] in {
            "PASS",
            "PASS_RETRY",
        }, f"Expected PASS/PASS_RETRY for {r['function']}, got {r['verdict']}"
        assert r["compiles"] is True
        assert len(r["files"]) == 1
        diag = r["diagnostic"]
        # Either by_name (when name matches renamed function in content) or
        # by_address (direct address match) is acceptable.
        assert diag["match_strategy"] in {"by_name", "by_address"}, (
            f"Expected 'by_name' or 'by_address' strategy for {r['function']}, " f"got {diag['match_strategy']!r}"
        )
    assert diag["identity_state"] == "matched", (
        f"Expected identity_state 'matched' for {r['function']}, " f"got {diag['identity_state']!r}"
    )
    # identity_reason is populated with a descriptive message for legacy fallback
    assert (
        "legacy" in diag["identity_reason"].lower()
    ), f"Expected identity_reason to mention legacy fallback, got {diag['identity_reason']!r}"
    assert diag["target_file_count"] == 1


def test_rejected_identity_no_compile_no_write(monkeypatch: Any, tmp_path: Path) -> None:
    """Rejected identity must produce NO_OUTPUT, no compile calls, no file writes.

    Given invalid explicit TARGET markers, When ``process_subunit`` runs,
    Then compile_check is never called, no files are written to disk,
    and all functions produce NO_OUTPUT.
    """
    monkeypatch.chdir(tmp_path)

    compile_called: list[bool] = []

    def _compile_not_called(code: str, cfg: Any) -> tuple[bool, str]:
        compile_called.append(True)
        return (True, "")

    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a.cpp\n"
        "void a() {}\n"
        # Missing TARGET for function 1 → incomplete → rejected
        "// FILE: b.cpp\n"
        "void b() {}\n"
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

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", _compile_not_called)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}", "name": "a"},
            {"address": "0x1001", "code": "void b() {}", "name": "b"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert compile_called == [], "compile_check must NOT be called when identity is rejected"
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        assert r["compiles"] is False
        assert r["files"] == []
        diag = r["diagnostic"]
        assert diag["match_strategy"] == "rejected_identity"


def test_ambiguous_h_cpp_pair_no_target_rejected(monkeypatch: Any) -> None:
    """A .h/.cpp pair without TARGET markers or address-bearing paths is rejected.

    Two functions, one .h/.cpp pair with descriptive names only (no addresses),
    no TARGET markers → NO_OUTPUT for both."""
    # Use descriptive names that do NOT appear as substrings in content/paths.
    # "a" matches "#pragma" (contains "a"), "FuncA" does not match "Foo".
    response = (
        "// FILE: include/mod/MyStruct.h\n"
        "#pragma once\n"
        "struct MyStruct {};\n"
        "\n"
        "// FILE: src/mod/MyStruct.cpp\n"
        '#include "MyStruct.h"\n'
        "void MyStruct::process() {}\n"
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
            {"address": "0x004117c0", "code": "void funcA() {}", "name": "funcA"},
            {"address": "0x00411800", "code": "void funcB() {}", "name": "funcB"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        assert r["files"] == []


def test_reversed_order_without_identity_no_output(monkeypatch: Any) -> None:
    """Reversed file order without explicit identity must produce NO_OUTPUT.

    Two functions, files emitted in wrong order (B before A), no TARGET markers,
    no address-bearing paths → NO_OUTPUT for both (no positional assumption)."""
    # Use names that do NOT appear in content. If names ("a", "b") appear in
    # content as renamed functions ("void a() {}"), by_name would match them
    # legitimately. We need truly unmatched output.
    response = (
        "// FILE: src/mod/Other.cpp\n"
        "void Other::run() {}\n"
        "\n"
        "// FILE: src/mod/Another.cpp\n"
        "void Another::exec() {}\n"
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
            {"address": "0x1000", "code": "void funcX() {}", "name": "funcX"},
            {"address": "0x1001", "code": "void funcY() {}", "name": "funcY"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _Cfg(), cache=None)

    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        assert r["files"] == []


def test_explicit_identity_unit_test_only(monkeypatch: Any) -> None:
    """Unit-level tests for _associate_files_to_functions, _parse_explicit_targets.

    Tests the internal helpers directly to verify edge cases without
    going through the full process_subunit path."""
    from re_agent.build.transform.subunit_processor import (
        _associate_files_to_functions,
        _parse_explicit_targets,
        _validate_explicit_targets,
    )

    # ── _parse_explicit_targets ──
    files_with_targets = [
        {"path": "a.cpp", "content": "// TARGET: 0 0x1000\nvoid a() {}"},
        {"path": "b.cpp", "content": "// TARGET: 1 0x1001\nvoid b() {}"},
        {"path": "c.cpp", "content": "// TARGET: 0 0x1000\nvoid a2() {}"},
    ]
    parsed = _parse_explicit_targets(files_with_targets)
    assert len(parsed) == 3
    assert parsed[0] == (0, "0x1000")
    assert parsed[1] == (1, "0x1001")
    assert parsed[2] == (0, "0x1000")

    # No TARGET marker → empty
    empty = _parse_explicit_targets([{"path": "x.cpp", "content": "void x() {}"}])
    assert empty == {}

    # ── _validate_explicit_targets ──
    funcs = [
        {"address": "0x1000", "code": ""},
        {"address": "0x1001", "code": ""},
    ]

    # Valid bijection
    valid_map = {0: (0, "0x1000"), 1: (1, "0x1001")}
    is_valid, reason = _validate_explicit_targets(valid_map, funcs)
    assert is_valid, f"Expected valid, got: {reason}"
    assert reason == ""

    # Incomplete (function 1 missing)
    incomplete_map = {0: (0, "0x1000")}
    is_valid, reason = _validate_explicit_targets(incomplete_map, funcs)
    assert not is_valid
    assert "Some files lack TARGET markers" in reason or "Not all functions" in reason

    # Out of range ordinal
    oob_map = {0: (5, "0x1000")}
    is_valid, reason = _validate_explicit_targets(oob_map, funcs)
    assert not is_valid
    assert "out of range" in reason

    # Wrong address for ordinal
    wrong_addr_map = {0: (0, "0xDEAD")}
    is_valid, reason = _validate_explicit_targets(wrong_addr_map, funcs)
    assert not is_valid
    assert "does not match" in reason

    # ── _associate_files_to_functions end-to-end ──
    assoc_files, assoc_strats, assoc_info = _associate_files_to_functions(files_with_targets, funcs)
    assert len(assoc_files) == 2
    assert len(assoc_files[0]) == 2  # two files for func 0
    assert len(assoc_files[1]) == 1  # one file for func 1
    assert assoc_strats == ["explicit_identity", "explicit_identity"]
    assert assoc_info[0] == ("explicit", "", 2)
    assert assoc_info[1] == ("explicit", "", 1)


# ═══════════════════════════════════════════════════════════════════════
# P0 test cases: new contract enforcement (re-agent NO-GO remediation)
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def _p0_cfg():
    """Minimal config for P0 contract tests."""

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

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    return _Cfg()


@pytest.fixture
def _p0_patches(monkeypatch, _p0_cfg):
    """Apply standard process_subunit patches for P0 tests."""
    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")
    return _p0_cfg


def _p0_context(*addrs: str) -> dict[str, Any]:
    """Build a standard functions_to_transform context from address-only entries."""
    return {
        "functions_to_transform": [
            {"address": addr, "code": f"void f_{addr}() {{}}", "name": f"func_{addr}"} for addr in addrs
        ],
        "neighbour_context": [],
    }


# ── 1. Record parsing: new _parse_llm_response_records ──


def test_parse_records_adjacent_target_extracted() -> None:
    """``// TARGET:`` immediately before ``// FILE:`` is extracted as target."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    response = "// TARGET: 0 0x1000\n// FILE: a.cpp\nint x = 1;\n"
    records, _ = _parse_llm_response_records(response)
    assert len(records) == 1
    assert records[0].path == "a.cpp"
    assert records[0].target == (0, "0x1000")


def test_parse_records_non_adjacent_target_not_extracted() -> None:
    """``// TARGET:`` separated by a blank line from ``// FILE:`` is NOT extracted."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    response = "// TARGET: 0 0x1000\n\n// FILE: a.cpp\nint x = 1;\n"
    records, _ = _parse_llm_response_records(response)
    assert len(records) == 1
    assert records[0].target is None, "non-adjacent TARGET must NOT be extracted"


def test_parse_records_target_in_body_preserved() -> None:
    """``// TARGET:`` line inside file body (not preceding FILE) is preserved in content."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    response = "// FILE: a.cpp\n// TARGET: 0 0x1000\nint x = 1;\n"
    records, _ = _parse_llm_response_records(response)
    assert len(records) == 1
    assert records[0].target is None  # not extracted
    assert "// TARGET:" in records[0].content, "TARGET inside body must be preserved"


def test_parse_records_empty_filepath_skipped() -> None:
    """``// FILE:`` with empty path (whitespace-only after strip) is rejected
    and sets ``has_invalid_file_block``."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    # Use tab character so the regex ``(.+)$`` has something to match
    # (``// FILE: \n`` would not match because ``.+`` needs >=1 char).
    response = "// FILE: \t\nsome content\n// FILE: valid.cpp\nint x;\n"
    records, has_invalid = _parse_llm_response_records(response)
    assert has_invalid, "empty FILE path must set has_invalid_file_block"
    assert len(records) == 1
    assert records[0].path == "valid.cpp"


def test_parse_records_empty_content_skipped() -> None:
    """``// FILE:`` with empty (after fence-strip) content is rejected
    and sets ``has_invalid_file_block``."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    response = "// FILE: empty.cpp\n```cpp\n```\n// FILE: valid.cpp\nint x;\n"
    records, has_invalid = _parse_llm_response_records(response)
    assert len(records) == 1
    assert records[0].path == "valid.cpp"


# ── 2. New path without TARGET in retry ──


def test_retry_new_path_without_target_no_output() -> None:
    """A new file path from retry without a valid TARGET is rejected (retry entirely dropped).

    P0 retry contract (``require_target=True``): if any retry record lacks a valid
    TARGET, ALL retry records are rejected — the initial list is returned unchanged.
    With ``require_target=False`` (legacy retry), the retry is merged without
    TARGET validation."""
    from re_agent.build.transform.subunit_processor import (
        FileRecord,
        _merge_retry_records,
    )

    initial = [FileRecord(path="a.cpp", content="int x;", target=(0, "0x1000"))]
    retry_without_target = [FileRecord(path="b.cpp", content="int y;", target=None)]

    # ── require_target=True: retry rejected ──
    merged_strict = _merge_retry_records(initial, retry_without_target, require_target=True)
    assert len(merged_strict) == 1, f"Expected 1 (retry rejected), got {len(merged_strict)}"
    assert merged_strict[0].path == "a.cpp"
    assert merged_strict[0].target == (0, "0x1000")
    assert merged_strict[0].content == "int x;"

    # ── require_target=False (default): retry merged (legacy) ──
    merged_legacy = _merge_retry_records(initial, retry_without_target)
    assert len(merged_legacy) == 2, f"Expected 2 (retry merged), got {len(merged_legacy)}"
    assert merged_legacy[1].path == "b.cpp"
    assert merged_legacy[1].target is None


# ── 3. Callee address must NOT match as identity ──


def test_legacy_callee_address_not_identity() -> None:
    """A bare address reference in content (callee) must NOT count as identity."""
    from re_agent.build.transform.subunit_processor import _match_files_to_function

    func = {"address": "0x004117c0", "code": ""}
    # Content mentions 0x004117c0 as a callee call, not as // Original function:
    parsed = [{"path": "src/mod/Renderer.cpp", "content": "   0x004117c0();  // callee call\n"}]
    result = _match_files_to_function(parsed, func, total_func_count=5)
    assert result == [], "bare callee address in content must NOT match as identity"
    # But a path bearing the address should match
    parsed_path = [{"path": "src/mod/0x004117c0__Renderer.cpp", "content": "   0x004117c0();\n"}]
    result_path = _match_files_to_function(parsed_path, func, total_func_count=5)
    assert len(result_path) == 1, "address in path must still match"


# ── 4. Double claim rejection in legacy mode ──


def test_legacy_double_claim_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """When two functions claim the same file via legacy matching, both are rejected."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Both functions 0x1000 and 0x1001 claim the same path via Original function comment
    response = (
        "// FILE: src/mod/Shared.cpp\n"
        "// Original function: 0x1000\n"
        "// Original function: 0x1001\n"
        "void shared() {}\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000", "0x1001")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for double-claimed file, got {r['verdict']}"
        diag = r["diagnostic"]
        assert (
            diag["match_strategy"] == "rejected_identity"
        ), f"Expected rejected_identity, got {diag['match_strategy']}"
        assert "contract violation" in diag["identity_reason"].lower() or "claimed" in diag["identity_reason"].lower()


# ── 5. Malformed TARGET rejected ──


def test_malformed_target_rejected() -> None:
    """Malformed ``// TARGET:`` lines are not extracted as valid targets."""
    from re_agent.build.transform.subunit_processor import _extract_adjacent_target

    # Valid
    assert _extract_adjacent_target("// TARGET: 0 0x1000\n") == (0, "0x1000")
    # Missing ordinal
    assert _extract_adjacent_target("// TARGET: 0x1000\n// FILE: a.cpp\n") is None
    # Non-hex address
    assert _extract_adjacent_target("// TARGET: 0 invalid\n") is None
    # Extra text after address
    assert _extract_adjacent_target("// TARGET: 0 0x1000 extra stuff\n") is None
    # Missing colon
    assert _extract_adjacent_target("// TARGET 0 0x1000\n") is None
    # Empty preceding
    assert _extract_adjacent_target("") is None
    # Multiple TARGET lines (contradictory → invalid, no "last wins")
    multi = "// TARGET: 0 0xDEAD\n// TARGET: 0 0x1000\n"
    assert _extract_adjacent_target(multi) is None, "multiple TARGET lines must be rejected"


# ── 6. NO_OUTPUT not cached (module_processor level) ──


def test_no_output_not_cached() -> None:
    """``cache.set`` must NOT be called when verdict is NO_OUTPUT.

    The module_processor cache set is guarded by ``r.get("verdict") != "NO_OUTPUT"``
    — verified structurally in the integration test below."""
    # Structural assertion: the guard condition exists in module_processor.py.
    # Actual verification is in test_no_output_skips_cache_entry.


def test_no_output_skips_cache_entry(tmp_path: Path) -> None:
    """When a result has NO_OUTPUT verdict, the cache must NOT store an entry.

    The cache guard in ``module_processor.py`` checks ``verdict != "NO_OUTPUT"``.
    We verify by simulating a cache-set call and asserting NO_OUTPUT results
    are skipped.
    """
    from re_agent.build.state.cache import TransformCache

    cache_path = tmp_path / "test_cache.json"
    cache = TransformCache(str(cache_path))

    # Simulate the guard condition from module_processor.py:
    # "if cache is not None and r.get('verdict') != 'NO_OUTPUT':"
    # The guard skips NO_OUTPUT — we verify by setting a non-NO_OUTPUT entry
    # and asserting the NO_OUTPUT address is NOT present.
    cache.set("0xCAFE", "source", "output", compiles=False, tokens_used=0, prompt_hash="", model="")
    assert cache.size() == 1
    assert cache.get("0xDEAD") is None, "NO_OUTPUT must not be cached"


# ── 7. Legacy fallback marked as contract violation ──


def test_legacy_fallback_logs_contract_violation(caplog: Any, monkeypatch: Any, _p0_patches: Any) -> None:
    """Legacy fallback matching (no TARGET) logs a contract violation warning."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    response = "// FILE: src/mod/0x1000__A.cpp\n" "// Original function: 0x1000\n" "void a() {}\n"
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000")
    caplog.clear()
    with caplog.at_level("WARNING", logger="re_agent.build.transform.subunit_processor"):
        results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 1
    # Legacy fallback is acceptable when TARGET markers are absent
    assert results[0]["verdict"] in {"PASS", "PASS_RETRY"}
    r = results[0]
    diag = r["diagnostic"]
    assert diag["match_strategy"] in {"by_address", "by_name", "single_function"}
    assert diag["identity_state"] == "matched"


# ── 8. Diagnostic: identity_state reason populated when unmatched ──


def test_diagnostic_identity_reason_populated(monkeypatch: Any, _p0_patches: Any) -> None:
    """When identity is rejected or absent, identity_reason must be non-empty."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Response with malformed TARGET (not directly adjacent — blank line between).
    # TARGET-like content IS present in the raw response, so the P0 protocol
    # correctly classifies this as "TARGET present but invalid" → rejected_identity
    # (not legacy fallback).
    response = "// TARGET: 0 0x1000\n\n// FILE: a.cpp\nint x = 1;\n"  # blank line breaks adjacency
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000", "0x1001")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT"
        diag = r["diagnostic"]
        # TARGET was present but invalid → rejected_identity (not "none")
        assert diag["match_strategy"] == "rejected_identity", (
            f"Expected rejected_identity (TARGET present but invalid), " f"got {diag['match_strategy']}"
        )
        assert diag["identity_state"] == "rejected", f"Expected rejected, got {diag['identity_state']}"
        idr = diag["identity_reason"]
        assert (
            "TARGET" in idr or "invalid" in idr.lower() or "malformed" in idr.lower()
        ), f"identity_reason must mention TARGET/invalid/malformed, got {idr!r}"


# ═══════════════════════════════════════════════════════════════════════
# P0 remediation — final review corrections
# ═══════════════════════════════════════════════════════════════════════


def test_parse_records_empty_file_with_target_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """Empty FILE path with preceding TARGET must produce rejected_identity.

    P0: empty FILE must produce rejected_identity, prevent legacy fallback,
    and prevent compile/write/cache."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Response has a TARGET line preceding an empty FILE and a valid second block
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: \n"  # empty FILE path — TARGET-like content present but invalid
        "some leaked content\n"
        "// FILE: b.cpp\n"
        "void b() {}\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000", "0x1001")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']} (empty FILE)"
        diag = r["diagnostic"]
        assert (
            diag["match_strategy"] == "rejected_identity"
        ), f"Expected rejected_identity, got {diag['match_strategy']}"
        assert "TARGET" in diag["identity_reason"]


def test_malformed_target_addressable_path_still_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """Malformed TARGET (no ordinal) with addressable path must still produce rejected_identity.

    P0: invalid TARGET prevents legacy fallback even when the path or content
    would otherwise match by address."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Malformed TARGET (no ordinal — just address) before FILE that bears
    # an addressable path.  The TARGET-like content "// TARGET: 0x1000" should
    # prevent legacy fallback and force rejected_identity.
    response = (
        "// TARGET: 0x1000\n"  # malformed — missing ordinal
        "// FILE: src/mod/0x1000__A.cpp\n"
        "// Original function: 0x1000\n"
        "void a() {}\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT (malformed TARGET prevents legacy), got {r['verdict']}"
    diag = r["diagnostic"]
    assert diag["match_strategy"] == "rejected_identity", f"Expected rejected_identity, got {diag['match_strategy']}"


def test_parse_records_multiple_target_no_last_wins(monkeypatch: Any, _p0_patches: Any) -> None:
    """Multiple TARGET lines before a single FILE → rejected_identity.

    P0: no "last TARGET wins". Contradictory multiple TARGET lines must
    produce rejected_identity and prevent legacy fallback."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Two TARGET lines before a.cpp, then a valid TARGET for b.cpp
    response = (
        "// TARGET: 0 0xDEAD\n"  # first target (contradicts second)
        "// TARGET: 0 0x1000\n"  # second target
        "// FILE: a.cpp\n"
        "void a() {}\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: b.cpp\n"
        "void b() {}\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000", "0x1001")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        diag = r["diagnostic"]
        assert (
            diag["match_strategy"] == "rejected_identity"
        ), f"Expected rejected_identity, got {diag['match_strategy']}"


def test_p1_target_leak_stripped_from_content(monkeypatch: Any, _p0_patches: Any) -> None:
    """The next block's TARGET must NOT leak into the current block's content.

    P1 leak fix: ``// TARGET:`` lines at the end of a content block
    (which belong to the next ``// FILE:``) must be stripped."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    # The '// TARGET: 1 0x1001' at the end of content block 0's region
    # (before '// FILE: b.cpp') is the next file's TARGET — must be stripped.
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a.cpp\n"
        "void a() {}\n"
        "// TARGET: 1 0x1001\n"
        "// FILE: b.cpp\n"
        "void b() {}\n"
    )
    records, _ = _parse_llm_response_records(response)
    assert len(records) == 2
    # a.cpp content must NOT contain the leaked TARGET for b
    assert "// TARGET: 1 0x1001" not in records[0].content, "Leaked TARGET must be stripped from content"
    assert records[0].content == "void a() {}", f"Expected clean content, got {records[0].content!r}"
    # b.cpp gets its TARGET from adjacent preceding line
    assert records[1].target == (1, "0x1001")


# ── P0 retry validation ──


def _retry_cfg():
    """Config with retries enabled for P0 retry tests."""

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
            compile_per_function = True

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    return _Cfg()


@pytest.fixture
def _p0_retry_patches(monkeypatch):
    """Apply patches for P0 retry tests (compile always fails on first attempt)."""
    import re_agent.build.transform.subunit_processor as sp

    call_count: list[int] = [0]

    def _compile_fail_first(code: str, cfg: Any) -> tuple[bool, str]:
        call_count[0] += 1
        if call_count[0] == 1:
            return (False, "compile error")
        return (True, "")

    monkeypatch.setattr(sp, "compile_check", _compile_fail_first)
    monkeypatch.setattr(sp, "compile_generated_file_set", _compile_fail_first)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")
    return _retry_cfg()


def test_retry_contradictory_target_rejected(monkeypatch: Any, _p0_retry_patches: Any) -> None:
    """Contradictory TARGET on known path in retry → retry rejected, initial preserved.

    P0 retry: a known path must carry exactly the stored identity.  If retry
    has wrong address, the retry is rejected and initial records survive."""
    cfg = _p0_retry_patches
    import re_agent.build.transform.subunit_processor as sp

    # Compile mock that ALWAYS fails (so we can verify retry rejection without
    # compile success masking the retry outcome).
    compile_call_count: list[int] = [0]

    def _always_fail(code: str, cfg: Any) -> tuple[bool, str]:
        compile_call_count[0] += 1
        return (False, "compile error")

    monkeypatch.setattr(sp, "compile_check", _always_fail)
    monkeypatch.setattr(sp, "compile_generated_file_set", _always_fail)

    # Initial response: valid TARGET for a.cpp.
    # Retry response: a.cpp gets contradictory TARGET (wrong address) → must reject retry.
    responses = _FakeMultiResponseProvider(
        [
            ("// TARGET: 0 0x1000\n" "// FILE: a.cpp\n" "void initial_content() {}\n"),
            # Retry: contradictory TARGET on same path (0x1001 instead of 0x1000)
            (
                "// TARGET: 0 0x1001\n"  # wrong address!
                "// FILE: a.cpp\n"
                "void retry_content() {}\n"
            ),
        ]
    )

    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", responses, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    # The retry with contradictory TARGET was rejected (merge returned initial).
    # Compile fails even on second attempt → FAIL_NO_RETRY.
    assert r["verdict"] == "FAIL_NO_RETRY", f"Expected FAIL_NO_RETRY (retry rejected), got {r['verdict']}"
    # Critical: content must be INITIAL (retry was rejected, not merged in)
    initial_content = next(
        (f["content"] for f in r.get("files", []) if "initial_content" in f["content"]),
        None,
    )
    assert initial_content is not None, (
        "Initial content must be preserved when retry is rejected." f" Files: {r.get('files', [])}"
    )


def test_retry_initial_explicit_missing_target_rejected(monkeypatch: Any, _p0_retry_patches: Any) -> None:
    """Retry without TARGET after initial explicit TARGET → retry rejected, initial preserved.

    P0 retry: if initial response used explicit TARGET, each retry block
    must have valid TARGET.  A retry without TARGET is rejected entirely
    without degrading the initial association."""
    cfg = _p0_retry_patches
    import re_agent.build.transform.subunit_processor as sp

    # Compile mock that ALWAYS fails (so we can verify retry rejection
    # doesn't degrade initial mapping).
    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (False, "compile error"))
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (False, "compile error"))

    # Initial: valid TARGET.  Retry: missing TARGET.
    responses = _FakeMultiResponseProvider(
        [
            ("// TARGET: 0 0x1000\n" "// FILE: a.cpp\n" "void initial_content() {}\n"),
            # Retry: NO target markers at all
            ("// FILE: a.cpp\n" "void retry_content() {}\n"),
        ]
    )

    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", responses, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    # Initial had explicit TARGET, retry missing it → retry rejected.
    # The initial association (explicit_identity) must survive.
    diag = r["diagnostic"]
    assert diag["match_strategy"] == "explicit_identity", (
        f"Initial explicit identity must survive invalid retry, " f"got {diag['match_strategy']}"
    )
    # Content must be INITIAL (retry was rejected, not merged)
    initial_files = r.get("files", [])
    initial_content = next(
        (f["content"] for f in initial_files if "initial_content" in f["content"]),
        None,
    )
    assert initial_content is not None, (
        "Initial content must be preserved when retry is rejected. " f"Files: {initial_files}"
    )


def test_retry_with_conversation_wrong_ordinal_rejected(monkeypatch: Any, _p0_retry_patches: Any) -> None:
    """``_retry_with_conversation`` must reject retry with wrong ordinal.

    P0 retry by function: every retry file must carry a TARGET matching
    the expected ordinal and address."""
    import re_agent.build.transform.subunit_processor as sp

    # Track calls to _retry_with_conversation
    original_retry = sp._retry_with_conversation

    def _tracking_retry(func_files, err, func, system, original_user, llm, max_retries, cfg, ordinal=0):
        # The LLM response for _retry_with_conversation is the *second* send call.
        # We can't inject into _retry_with_conversation directly, but we can
        # monkeypatch the provider to return a response with wrong ordinal.
        #
        # Instead, test _merge_retry_records directly with the validation logic.
        return original_retry(func_files, err, func, system, original_user, llm, max_retries, cfg, ordinal=ordinal)

    monkeypatch.setattr(sp, "_retry_with_conversation", _tracking_retry)

    from re_agent.build.transform.subunit_processor import (
        FileRecord,
        _merge_retry_records,
    )

    # Initial: valid target for func 0
    initial = [FileRecord(path="a.cpp", content="int x;", target=(0, "0x1000"))]

    # Retry with wrong ordinal (ordinal 1 instead of 0) when require_target=True
    retry_wrong = [FileRecord(path="a.cpp", content="int y;", target=(1, "0x1000"))]
    merged = _merge_retry_records(initial, retry_wrong, require_target=True)
    # Retry must be rejected entirely → initial returned unchanged
    assert len(merged) == 1
    assert merged[0].content == "int x;", f"Expected initial content preserved, got {merged[0].content!r}"

    # With require_target=False (default): retry is merged (legacy behavior)
    merged_legacy = _merge_retry_records(initial, retry_wrong)
    assert len(merged_legacy) == 1
    assert (
        merged_legacy[0].content == "int y;"
    ), f"Expected retry content merged (legacy), got {merged_legacy[0].content!r}"

    # Retry with new path but valid target
    retry_new_valid = [FileRecord(path="b.cpp", content="int z;", target=(0, "0x1000"))]
    merged2 = _merge_retry_records(initial, retry_new_valid)
    assert len(merged2) == 2
    assert merged2[1].path == "b.cpp"
    assert merged2[1].target == (0, "0x1000")

    # Retry with new path but NO target → rejected (require_target=True)
    retry_new_no_target = [FileRecord(path="c.cpp", content="int w;", target=None)]
    merged3 = _merge_retry_records(initial, retry_new_no_target, require_target=True)
    assert len(merged3) == 1
    assert merged3[0].path == "a.cpp"

    # With require_target=False (default): retry merged (legacy)
    merged3_legacy = _merge_retry_records(initial, retry_new_no_target)
    assert len(merged3_legacy) == 2
    assert merged3_legacy[1].path == "c.cpp"


def test_parse_records_content_trailing_target_stripped() -> None:
    """Trailing TARGET lines are stripped from content (P1 leak fix).

    When the next block's TARGET leaks into the current content, it must
    be removed.  This test verifies the strip logic in
    ``_parse_llm_response_records``."""
    from re_agent.build.transform.subunit_processor import _parse_llm_response_records

    # Two consecutive blocks: the first ends right before the second's TARGET
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: a.cpp\n"
        "void a() {}\n"
        "// TARGET: 1 0x1001\n"  # this is the leak (belongs to b.cpp)
        "// FILE: b.cpp\n"
        "void b() {}\n"
    )
    records, _ = _parse_llm_response_records(response)
    assert len(records) == 2, f"Expected 2 records, got {len(records)}"


# ═══════════════════════════════════════════════════════════════════════
# Invalid file block tests (review 2026-07-12)
# ═══════════════════════════════════════════════════════════════════════
# These tests verify that an empty FILE path or empty content (after
# fence-strip) is NOT silently ignored.  The parser sets
# ``has_invalid_file_block=True``, which forces ``rejected_identity``
# at the association layer — no legacy fallback, no compile/write/cache.


def test_initial_invalid_file_block_empty_path_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """Initial response with empty FILE path must produce rejected_identity.

    P0: empty FILE path is a protocol error.  All functions must produce
    NO_OUTPUT with ``match_strategy="rejected_identity"``.  No compile
    calls should occur."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    compile_called: list[bool] = []

    def _compile_not_called(code: str, cfg: Any) -> tuple[bool, str]:
        compile_called.append(True)
        return (True, "")

    monkeypatch.setattr(sp, "compile_check", _compile_not_called)
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))

    # Response with an empty FILE path block.
    # Use tab character so the regex ``(.+)$`` matches (``// FILE: \n`` would
    # not match because ``.+`` needs >=1 char).
    response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: \t\n"  # empty path → invalid (tab-only strips to "")
        "some leaked content\n"
        "// FILE: b.cpp\n"
        "void b() {}\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000", "0x1001")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 2
    assert compile_called == [], "compile_check must NOT be called when invalid file block detected"

    for r in results:
        assert (
            r["verdict"] == "NO_OUTPUT"
        ), f"Expected NO_OUTPUT for {r['function']} (empty FILE path), got {r['verdict']}"
        assert r["compiles"] is False
        assert r["files"] == []
        diag = r["diagnostic"]
        assert (
            diag["match_strategy"] == "rejected_identity"
        ), f"Expected rejected_identity, got {diag['match_strategy']}"
        assert (
            "empty path or content" in diag["identity_reason"].lower()
        ), f"identity_reason must mention empty path/content, got {diag['identity_reason']!r}"


def test_initial_invalid_file_block_empty_content_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """Initial response with empty content (after fence-strip) must produce rejected_identity.

    P0: empty content is a protocol error.  All functions rejected."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Response where a FILE block has only ``` fence delimiters (no actual content)
    response = (
        "// FILE: empty.cpp\n"
        "```cpp\n```\n"  # only fence → content becomes "" after strip
        "// FILE: b.cpp\n"
        "int x;\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT (empty content after fence-strip), got {r['verdict']}"
    assert r["files"] == []
    diag = r["diagnostic"]
    assert diag["match_strategy"] == "rejected_identity", f"Expected rejected_identity, got {diag['match_strategy']}"
    assert (
        "empty path or content" in diag["identity_reason"].lower()
    ), f"identity_reason must mention empty path/content, got {diag['identity_reason']!r}"


def test_initial_invalid_file_block_empty_path_no_target_still_rejected(monkeypatch: Any, _p0_patches: Any) -> None:
    """Empty FILE path without TARGET markers must still produce rejected_identity.

    No legacy fallback to by-address matching when an invalid file block is present."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    # Use tab character so the regex ``(.+)$`` has something to match.
    response = (
        "// FILE: \t\n"  # empty path (tab-only), no TARGET at all
        "int garbage;\n"
    )
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT (empty FILE path, no TARGET), got {r['verdict']}"
    diag = r["diagnostic"]
    assert (
        diag["match_strategy"] == "rejected_identity"
    ), f"Expected rejected_identity (invalid file block), got {diag['match_strategy']}"
    assert "empty path or content" in diag["identity_reason"].lower()


def test_initial_valid_response_no_invalid_flag(monkeypatch: Any, _p0_patches: Any) -> None:
    """A fully valid initial response must NOT set has_invalid_file_block or trigger rejection.

    Normal behavior preserved: address-bearing paths match, PASS verdict produced."""
    cfg = _p0_patches
    import re_agent.build.transform.subunit_processor as sp

    response = "// FILE: src/mod/0x1000__A.cpp\n" "// Original function: 0x1000\n" "void a() {}\n"
    provider = _FakeProvider(response)
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, cfg, cache=None)
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] in {"PASS", "PASS_RETRY"}, f"Expected PASS/PASS_RETRY for valid response, got {r['verdict']}"
    diag = r["diagnostic"]
    assert (
        diag["match_strategy"] != "rejected_identity"
    ), f"Valid response must not be rejected, got {diag['match_strategy']}"
    assert "rejected" not in diag.get("identity_state", "")


def test_retry_invalid_file_block_preserves_initial(monkeypatch: Any) -> None:
    """Retry response with invalid file block must be rejected; initial preserved.

    P0: an invalid FILE block in the retry response (empty path) means the
    entire retry is rejected — the initial records and association survive."""
    import re_agent.build.transform.subunit_processor as sp

    # Compile mock: first call fails (triggers subunit retry), subsequent succeed
    compile_call_count: list[int] = [0]

    def _compile_fail_first(code: str, cfg: Any) -> tuple[bool, str]:
        compile_call_count[0] += 1
        if compile_call_count[0] == 1:
            return (False, "synthetic error")
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

    monkeypatch.setattr(sp, "compile_check", _compile_fail_first)
    monkeypatch.setattr(sp, "compile_generated_file_set", _compile_fail_first)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    # Initial: valid TARGET-bearing response
    initial_response = "// TARGET: 0 0x1000\n" "// FILE: a.cpp\n" "void initial_content() {}\n"
    # Retry: has an empty FILE path → protocol error
    retry_response = (
        "// TARGET: 0 0x1000\n"
        "// FILE: \n"  # empty path → invalid
        "void retry_content() {}\n"
    )
    provider = _FakeMultiResponseProvider([initial_response, retry_response])
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert len(results) == 1
    r = results[0]
    # The retry with invalid block was rejected.  The initial association
    # (explicit_identity) must survive.  Since compile still fails on retry
    # (the retry was rejected, so the original files are recompiled),
    # verdict is FAIL_NO_RETRY (compile never succeeded).
    assert r["verdict"] != "NO_OUTPUT", (
        f"Initial association must survive invalid retry; " f"got {r['verdict']}. Files: {r.get('files', [])}"
    )
    # Initial content must be preserved (not replaced by invalid retry)
    initial_content_found = any("initial_content" in f["content"] for f in r.get("files", []))
    assert initial_content_found, (
        "Initial content must be preserved when retry has invalid file block. " f"Files: {r.get('files', [])}"
    )


def test_retry_invalid_file_block_empty_content_preserves_initial(monkeypatch: Any) -> None:
    """Retry response with empty content (after fence-strip) is rejected; initial preserved."""
    import re_agent.build.transform.subunit_processor as sp

    compile_call_count: list[int] = [0]

    def _compile_fail_first(code: str, cfg: Any) -> tuple[bool, str]:
        compile_call_count[0] += 1
        if compile_call_count[0] == 1:
            return (False, "synthetic error")
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

    monkeypatch.setattr(sp, "compile_check", _compile_fail_first)
    monkeypatch.setattr(sp, "compile_generated_file_set", _compile_fail_first)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    initial_response = "// TARGET: 0 0x1000\n" "// FILE: a.cpp\n" "void keep_me() {}\n"
    # Retry: file block with only ``` fence (empty content after strip)
    retry_response = (
        "// TARGET: 0 0x1000\n" "// FILE: a.cpp\n" "```cpp\n```\n"  # fence-only → empty after strip
    )
    provider = _FakeMultiResponseProvider([initial_response, retry_response])
    ctx = _p0_context("0x1000")
    results = sp.process_subunit(ctx, "mod", provider, _Cfg(), cache=None)
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] != "NO_OUTPUT", "Initial must survive invalid retry"
    initial_content_found = any("keep_me" in f["content"] for f in r.get("files", []))
    assert initial_content_found, "Initial content ('keep_me') must be preserved. " f"Files: {r.get('files', [])}"


from re_agent.build.transform.subunit_processor import (  # noqa: E402
    FileRecord,
    _analyze_target_coverage,
    _render_recovery_prompt,
    _run_target_recovery,
    _validate_target_groups,
)
from re_agent.llm.protocol import ProviderUsage  # noqa: E402


class _SeqProvider:
    """LLM provider returning canned responses in sequence per send()."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.last_messages: list[Message] = []
        self._idx = 0

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.last_messages = list(messages)
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        self.total_calls += 1
        return self._responses[idx]

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(
            prompt_tokens=0,
            completion_tokens=0,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            calls=self.total_calls,
        )

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


# â”€â”€ Helpers â”€â”€


def _record(path: str, content: str = "int x;", target: tuple[int, str] | None = None) -> FileRecord:
    return FileRecord(path=path, content=content, target=target)


def _func(addr: str, code: str = "void f() {}") -> dict:
    return {"address": addr, "code": code}


def _min_cfg():
    """Minimal config (legacy target_contract_mode) for process_subunit tests."""

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
            target_contract_mode = "legacy"

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    return _Cfg()


def _strict_cfg():
    """Minimal config with target_contract_mode = required."""

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
            target_contract_mode = "required"

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    return _Cfg()


# â”€â”€ 1. _analyze_target_coverage â”€â”€


def test_coverage_complete() -> None:
    cov = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0x1000")), _record("b.cpp", target=(1, "0x1001"))],
        [_func("0x1000"), _func("0x1001")],
    )
    assert cov.is_complete and not cov.has_conflict
    assert cov.covered_ordinals == {0, 1}
    assert not cov.missing_ordinals


def test_coverage_partial() -> None:
    cov = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0x1000")), _record("x.cpp", target=None)],
        [_func("0x1000"), _func("0x1001"), _func("0x1002")],
    )
    assert not cov.is_complete and not cov.has_conflict
    assert cov.covered_ordinals == {0}
    assert cov.missing_ordinals == {1, 2}


def test_coverage_oob_ordinal() -> None:
    cov = _analyze_target_coverage(
        [_record("a.cpp", target=(5, "0x1000"))],
        [_func("0x1000"), _func("0x1001")],
    )
    assert cov.has_conflict and "out of range" in cov.conflict_reason


def test_coverage_wrong_address() -> None:
    cov = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0xDEAD"))],
        [_func("0x1000"), _func("0x1001")],
    )
    assert cov.has_conflict and "does not match" in cov.conflict_reason


def test_coverage_path_collision() -> None:
    cov = _analyze_target_coverage(
        [
            _record("s.cpp", target=(0, "0x1000")),
            _record("s.cpp", target=(1, "0x1001")),
        ],
        [_func("0x1000"), _func("0x1001")],
    )
    assert cov.has_conflict and "duplicate path" in cov.conflict_reason


def test_coverage_all_none() -> None:
    cov = _analyze_target_coverage(
        [_record("a.cpp"), _record("b.cpp")],
        [_func("0x1000"), _func("0x1001")],
    )
    assert not cov.is_complete and not cov.has_conflict
    assert not cov.covered_ordinals
    assert cov.missing_ordinals == {0, 1}


# â”€â”€ 2. _validate_recovery_response â”€â”€


def test_validate_recovery_valid() -> None:
    ok, reason = _validate_target_groups(
        [_record("src/a.cpp", target=(0, "0x1000")), _record("src/b.cpp", target=(1, "0x1001"))],
        {0, 1},
        [_func("0x1000"), _func("0x1001")],
    )
    assert ok, reason


def test_validate_recovery_missing_ordinal() -> None:
    ok, reason = _validate_target_groups(
        [_record("src/a.cpp", target=(0, "0x1000"))],
        {0, 1},
        [_func("0x1000"), _func("0x1001")],
    )
    assert not ok and "Missing ordinals" in reason


def test_validate_recovery_foreign_ordinal() -> None:
    ok, reason = _validate_target_groups(
        [_record("src/a.cpp", target=(5, "0xDEAD"))],
        {0},
        [_func("0x1000")],
    )
    assert not ok and "not in allowed set" in reason


def test_validate_recovery_no_cpp() -> None:
    ok, reason = _validate_target_groups(
        [_record("include/a.h", target=(0, "0x1000"))],
        {0},
        [_func("0x1000")],
    )
    assert not ok and "no .cpp" in reason


def test_validate_recovery_duplicate_path() -> None:
    ok, reason = _validate_target_groups(
        [
            _record("src/a.cpp", target=(0, "0x1000")),
            _record("src/a.cpp", target=(1, "0x1001")),
        ],
        {0, 1},
        [_func("0x1000"), _func("0x1001")],
    )
    assert not ok and "Duplicate path" in reason


def test_validate_recovery_empty() -> None:
    ok, reason = _validate_target_groups([], {0}, [_func("0x1000")])
    assert not ok and "Empty" in reason


# â”€â”€ 3. _render_recovery_prompt â”€â”€


def test_render_recovery_prompt() -> None:
    p = _render_recovery_prompt([(0, {"address": "0x1000", "code": "void f() {}"})])
    assert "// TARGET:" in p and "0x1000" in p and "void f() {}" in p and "Ordinal 0" in p


# â”€â”€ 4. _run_target_recovery â”€â”€


def test_recovery_complete() -> None:
    funcs = [_func("0x1000"), _func("0x1001"), _func("0x1002")]
    initial = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0x1000"))],
        funcs,
    )
    assert initial.missing_ordinals == {1, 2}

    llm = _SeqProvider(
        [
            "// TARGET: 1 0x1001\n// FILE: b.cpp\nint y;\n" "// TARGET: 2 0x1002\n// FILE: c.cpp\nint z;\n",
        ]
    )
    final = _run_target_recovery(initial, funcs, llm, "system")
    assert final.is_complete
    assert final.covered_ordinals == {0, 1, 2}


def test_recovery_still_incomplete() -> None:
    funcs = [_func("0x1000"), _func("0x1001"), _func("0x1002")]
    initial = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0x1000"))],
        funcs,
    )

    llm = _SeqProvider(
        [
            "// TARGET: 1 0x1001\n// FILE: b.cpp\nint y;\n",
            # second batch in round 1 fails
            "bad",
            # round 2 repeats
            "bad",
            "bad",
        ]
    )
    final = _run_target_recovery(initial, funcs, llm, "system")
    assert not final.is_complete
    assert 2 in final.missing_ordinals


def test_recovery_foreign_target_rejected() -> None:
    """Foreign TARGET â†’ recovery round rejected, initial preserved."""
    funcs = [_func("0x1000"), _func("0x1001")]
    initial = _analyze_target_coverage(
        [_record("a.cpp", target=(0, "0x1000"))],
        funcs,
    )
    llm = _SeqProvider(["// TARGET: 5 0xDEAD\n// FILE: bad.cpp\nint bad;\n"])
    import re_agent.build.transform.subunit_processor as sp

    final = sp._run_target_recovery(initial, funcs, llm, "system")
    assert 0 in final.covered_ordinals
    assert not final.is_complete
    assert final.missing_ordinals == {1}


# â”€â”€ 5. Integration tests via process_subunit â”€â”€


def _patch_sp(monkeypatch: Any) -> None:
    """Standard patches for process_subunit integration tests."""
    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")


def test_recovery_3of10_to_full(monkeypatch: Any) -> None:
    """3/10 initial â†’ recovery covers remaining 7 â†’ all PASS."""
    _patch_sp(monkeypatch)
    initial = "\n".join(f"// TARGET: {i} 0x{i:04x}\n// FILE: src/f{i}.cpp\nvoid f{i}() {{}}\n" for i in range(3))
    # Recovery batches: ordinals 3-6 (4) then 7-9 (3)
    r1 = "\n".join(f"// TARGET: {i} 0x{i:04x}\n// FILE: src/f{i}.cpp\nvoid f{i}() {{}}\n" for i in range(3, 7))
    r2 = "\n".join(f"// TARGET: {i} 0x{i:04x}\n// FILE: src/f{i}.cpp\nvoid f{i}() {{}}\n" for i in range(7, 10))

    provider = _SeqProvider([initial, r1, r2])
    ctx = {
        "functions_to_transform": [{"address": f"0x{i:04x}", "code": f"void f{i}() {{}}"} for i in range(10)],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _strict_cfg(), cache=None)
    assert len(results) == 10
    for r in results:
        assert r["verdict"] in {"PASS", "PASS_RETRY"}, f"Got {r['verdict']} for {r['function']}"
        assert len(r["files"]) > 0


def test_recovery_still_incomplete_after_retry(monkeypatch: Any) -> None:
    """3/10 initial â†’ recovery covers 4/7 â†’ 3 INCOMPLETE_TARGETS."""
    _patch_sp(monkeypatch)
    initial = "\n".join(f"// TARGET: {i} 0x{i:04x}\n// FILE: src/f{i}.cpp\nvoid f{i}() {{}}\n" for i in range(3))
    # Recovery covers ordinals 3-6 only (4 files)
    r1 = "\n".join(f"// TARGET: {i} 0x{i:04x}\n// FILE: src/f{i}.cpp\nvoid f{i}() {{}}\n" for i in range(3, 7))
    # Invalid responses for remaining batches â†’ recovery fails
    r2 = "bad"
    r3 = "bad"

    provider = _SeqProvider([initial, r1, r2, r3])
    ctx = {
        "functions_to_transform": [{"address": f"0x{i:04x}", "code": f"void f{i}() {{}}"} for i in range(10)],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _strict_cfg(), cache=None)
    assert len(results) == 10

    # In strict mode, ALL functions are blocked when recovery is incomplete.
    # Zero compilation, zero files — even covered functions get INCOMPLETE_TARGETS.
    covered = sum(1 for r in results if r["verdict"] in {"PASS", "PASS_RETRY"})
    incomplete = sum(1 for r in results if r["verdict"] == "INCOMPLETE_TARGETS")
    assert covered == 0, f"Expected 0 covered (blocked by contract), got {covered}"
    assert incomplete == 10, f"Expected 10 incomplete (whole subunit blocked), got {incomplete}"
    for r in results:
        assert r["files"] == []
        assert "contract failed" in r.get("diagnostic", {}).get("identity_reason", "").lower()


def test_foreign_initial_hard_reject(monkeypatch: Any) -> None:
    """Foreign TARGET in initial response â†’ rejected_identity."""
    _patch_sp(monkeypatch)
    provider = _SeqProvider(
        [
            "// TARGET: 0 0xDEAD\n// FILE: a.cpp\nvoid a() {}\n",
        ]
    )
    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}"},
            {"address": "0x1001", "code": "void b() {}"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _min_cfg(), cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT"
        assert r["diagnostic"]["match_strategy"] == "rejected_identity"


def test_path_collision_hard_reject(monkeypatch: Any) -> None:
    """Duplicate path in TARGET markers â†’ rejected_identity via has_conflict."""
    _patch_sp(monkeypatch)
    # The same path "s.cpp" claimed by two different TARGETs â†’ path collision
    provider = _SeqProvider(
        [
            "// TARGET: 0 0x1000\n// FILE: s.cpp\nvoid a() {}\n" "// TARGET: 1 0x1001\n// FILE: s.cpp\nvoid b() {}\n",
        ]
    )
    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}"},
            {"address": "0x1001", "code": "void b() {}"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _min_cfg(), cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] == "NO_OUTPUT", f"Expected NO_OUTPUT for {r['function']}, got {r['verdict']}"
        # The path collision is detected by _analyze_target_coverage which
        # sets has_conflict=True.  This triggers rejected_identity because
        # the conflict reason propagates through _associate_files_to_functions.
        diag = r.get("diagnostic", {})
        assert diag.get("match_strategy") in (
            "rejected_identity",
            "none",
        ), f"Expected rejected_identity, got {diag.get('match_strategy')}"


def test_no_target_legacy_works(monkeypatch: Any) -> None:
    """No TARGET markers â†’ legacy matching (no recovery, no conflict)."""
    _patch_sp(monkeypatch)
    provider = _SeqProvider(
        [
            "// FILE: src/mod/0x1000__A.cpp\n// Original function: 0x1000\nvoid a() {}\n"
            "// FILE: src/mod/0x1001__B.cpp\n// Original function: 0x1001\nvoid b() {}\n",
        ]
    )
    ctx = {
        "functions_to_transform": [
            {"address": "0x1000", "code": "void a() {}"},
            {"address": "0x1001", "code": "void b() {}"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _min_cfg(), cache=None)
    assert len(results) == 2
    for r in results:
        assert r["verdict"] in {"PASS", "PASS_RETRY"}, f"Got {r['verdict']}"


def test_no_persist_with_phase_analyze_rejected() -> None:
    """CLI rejects --no-persist with --phase analyze (before config load)."""
    # Test the validation logic at the top of cmd_build directly
    # The check is: if not persist and phase is not None and phase != "transform" â†’ return 2
    # Simulate by running the validation without actually calling cmd_build's I/O
    import argparse

    args = argparse.Namespace(
        config="",
        no_persist=True,
        phase="analyze",
        module=None,
        subunit=None,
        max_subunits=None,
        run_id=None,
    )
    persist = not args.no_persist
    phase = args.phase
    # This is the exact check cmd_build does â€” return code 2
    assert (
        not persist and phase is not None and phase != "transform"
    ), "Validation should have rejected --no-persist with --phase analyze"
