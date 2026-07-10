from __future__ import annotations

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
