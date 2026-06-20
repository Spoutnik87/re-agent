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
