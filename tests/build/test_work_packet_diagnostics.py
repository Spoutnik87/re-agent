"""Failing-first tests for Todo 8: WorkPacket diagnostics wired into transform.

These tests assert the NEW contract that process_subunit must:
- NOT unconditionally write the raw LLM response to
  ``.omo/evidence/llm-raw-subunit.txt`` (or anywhere) unless explicitly
  configured.
- Attach a ``diagnostic`` dict to every result carrying:
  raw_response_length, parse_count (marker_count), match_strategy,
  files_written, work_packet_path, raw_response_path (only when enabled),
  model_usage snapshot, and router_decision snapshot.
- Preserve existing successful parsing/writing behavior (result shape,
  files list, compiles/verdict fields).

No live LLM/provider calls. Deterministic. tmp_path-scoped.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from re_agent.build.transform.diagnostics import (
    FunctionVerdict,
    classify_compile_error,
    truncate_compile_error,
)
from re_agent.build.transform.subunit_processor import process_subunit
from re_agent.llm.protocol import Message, ProviderUsage


class _UsageProvider:
    """Fake LLMProvider returning a canned response and a real get_usage()."""

    supports_conversations = False
    total_prompt_tokens = 120
    total_completion_tokens = 80
    total_calls = 0
    total_cache_hit_tokens = 40
    total_cache_miss_tokens = 60

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_messages: list[Message] = []

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.last_messages = list(messages)
        self.total_calls += 1
        return self._response

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            cache_hit_tokens=self.total_cache_hit_tokens,
            cache_miss_tokens=self.total_cache_miss_tokens,
            calls=self.total_calls,
        )

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


class _LegacyProvider:
    """Fake provider WITHOUT get_usage() — exercises the legacy fallback path."""

    supports_conversations = False
    total_prompt_tokens = 10
    total_completion_tokens = 5
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, response: str) -> None:
        self._response = response

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        return self._response

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


def _make_cfg(
    diagnostics_dir: str = "",
    raw_response_capture: bool = False,
    max_compile_retries: int = 0,
) -> Any:
    naming = SimpleNamespace(classes="PascalCase", functions="camelCase", globals="snake_case")
    conventions = SimpleNamespace(naming=naming, includes_rule="", max_function_lines=200)
    project = SimpleNamespace(description="", conventions=conventions)
    output = SimpleNamespace(
        language="C++",
        standard="c++23",
        compiler="g++",
        compiler_flags="-std=c++23 -c -Wall -Werror",
    )
    validation = SimpleNamespace(max_compile_retries=max_compile_retries)
    optimization = SimpleNamespace(
        cache_enabled=False,
        cache_path="",
        subunit_size=10,
        context_window=3,
        diagnostics_dir=diagnostics_dir,
        raw_response_capture=raw_response_capture,
    )
    return SimpleNamespace(
        output=output,
        project=project,
        validation=validation,
        optimization=optimization,
    )


def _patch_sp(monkeypatch, compile_result: tuple[bool, str] = (True, "")) -> None:
    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "compile_check", lambda code, cfg: compile_result)
    monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, target, cfg: compile_result)
    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")


# ---------------------------------------------------------------------------
# 1. No unconditional raw LLM response write when not configured
# ---------------------------------------------------------------------------


def test_no_unconditional_raw_response_write_when_disabled(monkeypatch, tmp_path: Path) -> None:
    """Given raw_response_capture=False (default), When process_subunit runs,
    Then no .omo/evidence/llm-raw-subunit.txt is created anywhere under CWD."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    process_subunit(ctx, "mod", provider, _make_cfg(), cache=None)
    # The legacy hidden write must NOT happen.
    legacy = tmp_path / ".omo" / "evidence" / "llm-raw-subunit.txt"
    assert not legacy.exists(), f"unconditional raw response write detected at {legacy} — must be config-gated"
    # And no raw response file anywhere under tmp_path.
    raw_files = list(tmp_path.rglob("llm-raw-subunit.txt"))
    assert raw_files == [], f"unexpected raw response files: {raw_files}"


def test_raw_response_written_only_when_enabled(monkeypatch, tmp_path: Path) -> None:
    """Given raw_response_capture=True and diagnostics_dir set, When process_subunit runs,
    Then the raw response is written under diagnostics_dir (not the legacy hidden path)."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir), raw_response_capture=True),
        cache=None,
    )
    # Legacy hidden path still must NOT exist.
    assert not (tmp_path / ".omo" / "evidence" / "llm-raw-subunit.txt").exists()
    # Raw response written under diag_dir.
    r = results[0]
    assert r["diagnostic"]["raw_response_path"] is not None
    raw_path = Path(r["diagnostic"]["raw_response_path"])
    assert raw_path.exists(), f"raw response file should exist at {raw_path}"
    assert raw_path.read_text(encoding="utf-8") == response
    # And it must be inside diag_dir (evidence/report scoped).
    assert diag_dir in raw_path.parents or raw_path == diag_dir / "raw"


# ---------------------------------------------------------------------------
# 2. NO_OUTPUT diagnostic includes required fields
# ---------------------------------------------------------------------------


def test_no_output_diagnostic_includes_required_fields(monkeypatch, tmp_path: Path) -> None:
    """Given a fake LLM emitting NO // FILE: markers, When process_subunit runs,
    Then the NO_OUTPUT result's diagnostic includes raw_response_length,
    parse_count=0, match_strategy, files_written=0, work_packet_path,
    raw_response_path=None (not enabled), and no files written to output."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    no_markers = "I am sorry, I cannot produce that output. Here is some prose instead."
    provider = _UsageProvider(no_markers)
    ctx = {
        "functions_to_transform": [{"address": "0x00414580", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "NO_OUTPUT"
    assert r["files"] == []
    diag = r["diagnostic"]
    assert diag["raw_response_length"] == len(no_markers)
    assert diag["parse_count"] == 0
    assert diag["marker_count"] == 0
    assert diag["match_strategy"] == "none"
    assert diag["files_written"] == 0
    assert diag["raw_response_path"] is None
    assert diag["work_packet_path"] is not None
    wp_path = Path(diag["work_packet_path"])
    assert wp_path.exists(), f"work packet JSON should exist at {wp_path}"
    assert diag_dir in wp_path.parents
    # No output files written anywhere under tmp_path (except the work packet JSON under diag_dir).
    written_sources = list(tmp_path.rglob("*.cpp")) + list(tmp_path.rglob("*.h"))
    assert written_sources == [], f"no source files should be written by process_subunit: {written_sources}"
    # model_usage snapshot present with cache metrics.
    mu = diag["model_usage"]
    assert mu is not None
    assert mu["prompt_tokens"] == 120
    assert mu["completion_tokens"] == 80
    assert mu["cache_hit_tokens"] == 40
    assert mu["cache_miss_tokens"] == 60
    # router_decision snapshot present (placeholder/default is explicit).
    rd = diag["router_decision"]
    assert rd is not None
    assert "action" in rd
    assert "reason" in rd


def test_no_output_work_packet_json_contains_required_fields(monkeypatch, tmp_path: Path) -> None:
    """Given NO_OUTPUT, When the work packet JSON is written, Then it contains
    the subunit-level diagnostic fields (raw_response_length, parse_count,
    match_strategy, function_verdicts, model_usage, router_decision)."""
    import json

    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    provider = _UsageProvider("no markers at all")
    ctx = {
        "functions_to_transform": [{"address": "0x00414580", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    wp_path = Path(results[0]["diagnostic"]["work_packet_path"])
    data = json.loads(wp_path.read_text(encoding="utf-8"))
    assert data["raw_response_length"] == len("no markers at all")
    assert data["parse_count"] == 0
    assert data["marker_count"] == 0
    assert data["match_strategy"] == "none"
    assert data["total_files_written"] == 0
    assert len(data["function_verdicts"]) == 1
    fv = data["function_verdicts"][0]
    assert fv["address"] == "0x00414580"
    assert fv["verdict"] == "NO_OUTPUT"
    assert fv["compiles"] is False
    assert fv["files_matched"] == 0
    assert data["model_usage"]["prompt_tokens"] == 120
    assert data["router_decision"]["action"] is not None


# ---------------------------------------------------------------------------
# 3. Success path records files_written and token/cache usage
# ---------------------------------------------------------------------------


def test_success_path_records_files_written_and_usage(monkeypatch, tmp_path: Path) -> None:
    """Given a fake LLM emitting valid // FILE: markers, When process_subunit runs,
    Then the PASS result's diagnostic records files_written>0 and the provider's
    token/cache usage from get_usage()."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    response = (
        "// FILE: include/mod/Class.h\n#pragma once\nstruct Class {};\n"
        '\n// FILE: src/mod/Class.cpp\n#include "Class.h"\nvoid Class::f() {}\n'
    )
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "Class"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    assert len(results) == 1
    r = results[0]
    assert r["verdict"] == "PASS"
    assert r["compiles"] is True
    assert len(r["files"]) == 2
    diag = r["diagnostic"]
    assert diag["raw_response_length"] == len(response)
    assert diag["parse_count"] == 2
    assert diag["marker_count"] == 2
    # single function in subunit → all files assigned to it
    assert diag["match_strategy"] == "single_function"
    assert diag["files_written"] == 2
    assert diag["work_packet_path"] is not None
    mu = diag["model_usage"]
    assert mu["prompt_tokens"] == 120
    assert mu["completion_tokens"] == 80
    assert mu["cache_hit_tokens"] == 40
    assert mu["cache_miss_tokens"] == 60
    assert mu["calls"] == 1


def test_success_path_records_usage_from_legacy_provider(monkeypatch, tmp_path: Path) -> None:
    """Given a provider WITHOUT get_usage(), When process_subunit runs,
    Then the diagnostic records token usage from total_* attributes and
    cache metrics as None (unknown, not faked zero)."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
    provider = _LegacyProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    diag = results[0]["diagnostic"]
    mu = diag["model_usage"]
    assert mu is not None
    assert mu["prompt_tokens"] == 10
    assert mu["completion_tokens"] == 5
    # Legacy fallback: cache metrics must be None, not 0.
    assert mu["cache_hit_tokens"] is None
    assert mu["cache_miss_tokens"] is None


def test_match_strategy_by_address_when_name_missing(monkeypatch, tmp_path: Path) -> None:
    """Given a multi-function subunit where name is absent and address matches,
    When process_subunit runs, Then match_strategy is 'by_address'."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "diag"
    # The strict identity rule requires the address to be in the file path
    # or in a ``// Original function:`` comment — bare address references
    # in content (callee) are NOT identity anchors.
    response = (
        "// FILE: src/mod/0x00414580__A.cpp\n"
        "// Original function: 0x00414580\n"
        "void A() {}\n"
        "\n"
        "// FILE: src/mod/0x004145a0__B.cpp\n"
        "// Original function: 0x004145a0\n"
        "void B() {}\n"
    )
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [
            {"address": "0x00414580", "code": "void A() {}"},
            {"address": "0x004145a0", "code": "void B() {}"},
        ],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    strategies = {r["diagnostic"]["match_strategy"] for r in results}
    assert "by_address" in strategies


# ---------------------------------------------------------------------------
# 4. Existing successful parsing/writing behavior unchanged
# ---------------------------------------------------------------------------


def test_existing_result_shape_preserved(monkeypatch, tmp_path: Path) -> None:
    """Given a successful transform, When process_subunit runs,
    Then the result dict still has the legacy keys (function, module, files,
    compiles, verdict) with the same semantics — diagnostic is additive."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    response = (
        "// FILE: include/mod/Class.h\n#pragma once\nstruct Class {};\n"
        '\n// FILE: src/mod/Class.cpp\n#include "Class.h"\nvoid Class::f() {}\n'
    )
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "Class"}],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _make_cfg(), cache=None)
    assert len(results) == 1
    r = results[0]
    # Legacy keys preserved.
    assert r["function"] == "0x1000"
    assert r["module"] == "mod"
    assert r["compiles"] is True
    assert r["verdict"] == "PASS"
    assert isinstance(r["files"], list)
    assert len(r["files"]) == 2
    assert {f["path"] for f in r["files"]} == {"include/mod/Class.h", "src/mod/Class.cpp"}
    # Diagnostic is additive (new key).
    assert "diagnostic" in r


def test_no_diagnostics_dir_means_no_work_packet_file(monkeypatch, tmp_path: Path) -> None:
    """Given diagnostics_dir='' (default), When process_subunit runs,
    Then no work packet JSON file is written (backward compat for callers
    that don't opt in), but the diagnostic dict is still attached."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(ctx, "mod", provider, _make_cfg(), cache=None)
    diag = results[0]["diagnostic"]
    # work_packet_path is None because diagnostics_dir was not set.
    assert diag["work_packet_path"] is None
    # No JSON files written anywhere under tmp_path.
    json_files = list(tmp_path.rglob("*.json"))
    assert json_files == [], f"no work packet JSON expected: {json_files}"


def test_work_packet_path_never_under_reports_re_agent_code(monkeypatch, tmp_path: Path) -> None:
    """Given a diagnostics_dir, When the work packet JSON is written,
    Then its path is never under reports/re-agent/code/ (precious corpus)."""
    monkeypatch.chdir(tmp_path)
    _patch_sp(monkeypatch)
    diag_dir = tmp_path / "evidence" / "work-packets"
    response = "// FILE: src/mod/f.cpp\nvoid f() {}\n"
    provider = _UsageProvider(response)
    ctx = {
        "functions_to_transform": [{"address": "0x1000", "code": "void f() {}", "name": "f"}],
        "neighbour_context": [],
    }
    results = process_subunit(
        ctx,
        "mod",
        provider,
        _make_cfg(diagnostics_dir=str(diag_dir)),
        cache=None,
    )
    wp_path = results[0]["diagnostic"]["work_packet_path"]
    assert wp_path is not None
    assert "reports" not in wp_path.replace("\\", "/").split("/")
    assert "re-agent" not in wp_path.replace("\\", "/").split("/") or "evidence" in wp_path.replace("\\", "/")


# ---------------------------------------------------------------------------
# 5. Compile error and category fields in FunctionVerdict
# ---------------------------------------------------------------------------


def test_function_verdict_with_compile_error_serializes_stderr_and_category() -> None:
    """Given FAIL_NO_RETRY or FAIL_AFTER_RETRY, When FunctionVerdict has
    compile_error and compile_error_category set, Then to_json_dict includes
    both fields with the expected values."""
    fv = FunctionVerdict(
        address="0x00414580",
        verdict="FAIL_NO_RETRY",
        compiles=False,
        files_matched=1,
        match_strategy="by_address",
        compile_error="error: too many arguments to function 'foo'",
        compile_error_category="too_many_arguments",
    )
    j = fv.to_json_dict()
    assert j["address"] == "0x00414580"
    assert j["verdict"] == "FAIL_NO_RETRY"
    assert j["compile_error"] == "error: too many arguments to function 'foo'"
    assert j["compile_error_category"] == "too_many_arguments"


def test_function_verdict_without_compile_error_defaults_to_none() -> None:
    """Given PASS or NO_OUTPUT, When FunctionVerdict uses defaults,
    Then compile_error and compile_error_category are None in JSON."""
    fv = FunctionVerdict(
        address="0x00414580",
        verdict="PASS",
        compiles=True,
        files_matched=1,
    )
    j = fv.to_json_dict()
    assert j["compile_error"] is None
    assert j["compile_error_category"] is None


def test_function_verdict_no_output_defaults_compile_error_none() -> None:
    """Given NO_OUTPUT verdict, When FunctionVerdict is constructed,
    Then compile_error and compile_error_category default to None."""
    fv = FunctionVerdict(
        address="0x00414580",
        verdict="NO_OUTPUT",
        compiles=False,
        files_matched=0,
        match_strategy="none",
    )
    j = fv.to_json_dict()
    assert j["compile_error"] is None
    assert j["compile_error_category"] is None


def test_function_verdict_compile_error_must_match_category_none() -> None:
    """Given mismatched None/non-None for compile_error and
    compile_error_category, When FunctionVerdict is constructed,
    Then ValueError is raised."""
    import pytest

    with pytest.raises(ValueError, match="both be None or both"):
        FunctionVerdict(
            address="0x1000",
            verdict="FAIL_NO_RETRY",
            compiles=False,
            files_matched=1,
            compile_error="some error",
            compile_error_category=None,
        )
    with pytest.raises(ValueError, match="both be None or both"):
        FunctionVerdict(
            address="0x1000",
            verdict="FAIL_NO_RETRY",
            compiles=False,
            files_matched=1,
            compile_error=None,
            compile_error_category="too_many_arguments",
        )


# ---------------------------------------------------------------------------
# 6. Compile error truncation
# ---------------------------------------------------------------------------


def test_truncate_short_compile_error_passthrough() -> None:
    """Given a short compiler stderr (within limit), When truncating,
    Then the original text is returned unchanged."""
    text = "error: too many arguments to function 'foo'"
    result = truncate_compile_error(text)
    assert result == text


def test_truncate_long_compile_error_deterministic() -> None:
    """Given an overlong compiler stderr, When truncating, Then the result
    is deterministic: same input always yields same truncated output,
    preserves the first KEEP_HEAD chars and last KEEP_TAIL chars, and
    contains the truncation marker."""
    head_text = "A" * 3000
    middle_text = "B" * 5000
    tail_text = "C" * 2000
    long_stderr = head_text + middle_text + tail_text

    result1 = truncate_compile_error(long_stderr)
    result2 = truncate_compile_error(long_stderr)

    # Deterministic
    assert result1 == result2

    # Length reduced
    from re_agent.build.transform.diagnostics import _KEEP_HEAD, _KEEP_TAIL

    assert len(result1) < len(long_stderr)
    # Preserves first KEEP_HEAD chars
    assert result1.startswith(head_text[:_KEEP_HEAD])
    # Preserves last KEEP_TAIL chars (the truncation marker + tail)
    assert result1.endswith(tail_text[-_KEEP_TAIL:])
    # Contains truncation marker
    assert "truncated" in result1


def test_truncate_empty_stderr_passthrough() -> None:
    """Given an empty string as stderr, When truncating, Then it is returned
    unchanged (empty string is within limit)."""
    assert truncate_compile_error("") == ""


def test_truncate_preserves_gcc_error_location_in_head() -> None:
    """Given a realistic GCC-style stderr with error prefix at the start,
    When truncating, Then the error prefix (file:line:error:) is preserved
    in the first KEEP_HEAD characters."""
    gcc_stderr = (
        "src/mod/f.cpp:42:10: error: too many arguments to function 'bar'\n"
        "   42 |     bar(x, y, z);\n"
        "      |     ~~~^\n"
        + ("note: some long template context\n" * 100)
        + "src/mod/f.cpp:50:5: note: declared here\n"
        "   50 | void bar(int a);\n"
        "      |      ^~~"
    )
    result = truncate_compile_error(gcc_stderr)
    assert "error: too many arguments" in result
    assert "declared here" in result
    assert "note:" in result


# ---------------------------------------------------------------------------
# 7. Compile error classification
# ---------------------------------------------------------------------------


class TestClassifyCompileError:
    """Classification pattern matching."""

    def test_too_many_arguments(self) -> None:
        assert classify_compile_error("error: too many arguments to function 'foo'") == "too_many_arguments"
        assert classify_compile_error("error: too few arguments to function 'bar'") == "too_many_arguments"

    def test_undeclared_identifier(self) -> None:
        for msg in [
            "'baz' was not declared in this scope",
            "error: 'MyType' does not name a type",
            "error: unknown type name 'uint32_t'",
            "error: 'Something' has not been declared",
        ]:
            assert classify_compile_error(msg) == "undeclared_identifier", msg

    def test_type_mismatch(self) -> None:
        for msg in [
            "error: cannot convert 'int*' to 'float*'",
            "error: cannot initialize a variable of type 'int'",
            "error: invalid conversion from 'char' to 'int'",
            "error: no matching function for call to 'foo'",
            "error: no known conversion for argument 1",
            "error: cannot bind 'int' lvalue to 'int&&'",
        ]:
            assert classify_compile_error(msg) == "type_mismatch", msg

    def test_syntax_error(self) -> None:
        for msg in [
            "error: expected ';' before '}' token",
            "error: expected primary-expression before 'int'",
            "error: stray '\\123' in program",
            "error: missing terminating ' character",
            "syntax error",
        ]:
            assert classify_compile_error(msg) == "syntax_error", msg

    def test_syntax_error_takes_precedence_over_other_patterns(self) -> None:
        """'expected' is checked first and takes precedence even if the
        text also contains 'undeclared' or other patterns."""
        assert classify_compile_error("error: expected ';' before 'undeclared_identifier'") == "syntax_error"

    def test_unknown_pattern(self) -> None:
        for msg in [
            "gcc: fatal error: cannot open output file",
            "internal compiler error",
            "some random message",
            "",
        ]:
            assert classify_compile_error(msg) == "unknown", msg

    def test_include_error(self) -> None:
        """_decls.h: No such file or directory (include path issue)"""
        assert (
            classify_compile_error(
                "fatal error: _decls.h: No such file or directory\n"
                '    1 | #include "_decls.h"\n'
                "      |          ^~~~~~~~~~\n"
                "compilation terminated.\n"
            )
            == "include_error"
        )
        assert (
            classify_compile_error("src/mod/f.cpp:1:10: fatal error: 'someheader.h': No such file or directory")
            == "include_error"
        )
        # "cannot open output file" does NOT match — different category.
        assert classify_compile_error("gcc: fatal error: cannot open output file") == "unknown"
        assert classify_compile_error("fatal error: no such file or directory") == "include_error"

    def test_empty_string_unknown(self) -> None:
        assert classify_compile_error("") == "unknown"

    # ──────────────────────────────────────────────────────────────────────
    # _decls.h dllimport warning frontier
    # ──────────────────────────────────────────────────────────────────────
    # The rerun1 frontier revealed that `_decls.h:19040` dllimport
    # warning-as-error was classifying as `unknown`. classify_compile_error
    # now recognises the pattern and returns `decls_header_warning`.
    # This test locks that behavior so the category is preserved even after
    # future additions to the classifier.

    def test_decls_header_warning_failing_first_compile_frontier(self) -> None:
        """_decls.h:19040 dllimport warning-as-error → 'decls_header_warning'.

        CHARACTERISATION test: currently passing since classify_compile_error
        recognises the pattern. Proves the _decls.h-specific 'redeclared without
        dllimport attribute' / 'warnings being treated as errors' wording
        correctly maps to 'decls_header_warning'.

        Real rerun1 stderr shape (normalised path):
          In file included from <command-line>:
          _decls.h:19040:17: error: 'DWORD GetCurrentProcessId()'
            redeclared without dllimport attribute:
            previous dllimport ignored [-Werror=attributes]
          cc1plus.exe: all warnings being treated as errors
        """
        stderr = (
            "In file included from <command-line>:\n"
            "D:\\dev\\.ghidra-exports\\_decls.h:19040:17: error: "
            "'DWORD GetCurrentProcessId()' redeclared without dllimport "
            "attribute: previous dllimport ignored [-Werror=attributes]\n"
            "19040 | DWORD __stdcall GetCurrentProcessId(void);\n"
            "      |                 ^~~~~~~~~~~~~~~~~~~\n"
            "cc1plus.exe: all warnings being treated as errors\n"
        )
        # Desired: 'decls_header_warning'. Current: 'unknown'.
        # Asserting the desired value makes this a failing-first test.
        assert classify_compile_error(stderr) == "decls_header_warning"

    # ──────────────────────────────────────────────────────────────────────
    # sqrtl undeclared characterisation (Todo 1 — confirms current category)
    # ──────────────────────────────────────────────────────────────────────
    # The rerun1 frontier also includes functions where the only real C++
    # error is 'sqrtl' was not declared. This test locks the current correct
    # classification as 'undeclared_identifier' and guards against
    # accidentally absorbing it into a broader _decls.h catcher.

    def test_compile_frontier_both_decls_and_real_error_real_wins(self) -> None:
        """When stderr contains BOTH a _decls.h warning and a sqrtl undeclared
        error, classify_compile_error returns 'undeclared_identifier' — the
        real error, not 'decls_header_warning'. Proves that after demotion,
        real body errors surface through classification even when decls warning
        is present in the same stderr."""
        stderr = (
            "In file included from <command-line>:\n"
            "_decls.h:19040:17: error: 'DWORD GetCurrentProcessId()' "
            "redeclared without dllimport attribute: "
            "previous dllimport ignored [-Werror=attributes]\n"
            "19040 | DWORD __stdcall GetCurrentProcessId(void);\n"
            "      |                 ^~~~~~~~~~~~~~~~~~~\n"
            "cc1plus.exe: all warnings being treated as errors\n"
            "tmp.cpp:9:12: error: 'sqrtl' was not declared in this scope; "
            "did you mean 'strtol'?\n"
            "    9 |     return sqrtl(dy * dy + dx * dx + dz * dz);\n"
            "      |            ^~~~~\n"
            "      |            strtol\n"
        )
        assert classify_compile_error(stderr) == "undeclared_identifier", (
            "Real error must take priority over decls_header_warning when both appear in stderr"
        )

    def test_compile_frontier_sqrtl_undeclared_classifies_as_undeclared_identifier(self) -> None:
        """'sqrtl' was not declared in this scope → 'undeclared_identifier'.

        CHARACTERISATION test: current classify_compile_error correctly
        returns 'undeclared_identifier' via the 'was not declared' pattern.
        Must CONTINUE to do so even after adding _decls.h warning handling.

        Rerun1 stderr excerpt (the sqrtl fragment only):
          ...: In function 'long double calculateDistance(...)':
          ...:9:12: error: 'sqrtl' was not declared in this scope;
            did you mean 'strtol'?
        """
        stderr = (
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpbuda7o3x.cpp: "
            "In function 'long double calculateDistance(const float*, "
            "const float*)':\n"
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpbuda7o3x.cpp:9:12: "
            "error: 'sqrtl' was not declared in this scope; "
            "did you mean 'strtol'?\n"
            "    9 |     return sqrtl(dy * dy + dx * dx + dz * dz);\n"
            "      |            ^~~~~\n"
            "      |            strtol\n"
        )
        assert classify_compile_error(stderr) == "undeclared_identifier"

    # ──────────────────────────────────────────────────────────────────────
    # goto_error — GCC jump to label / crosses initialization (Todo 4)
    # ──────────────────────────────────────────────────────────────────────
    # The renderer subunit 1 pilot showed UpdateSystemState (0x005164b0)
    # was misclassified as decls_header_warning because the _decls.h
    # dllimport check was last. goto_error goes before it so real
    # goto-crosses-init errors take priority over the ctx artifact.

    def test_goto_error_mixed_with_decls_warning(self) -> None:
        """When stderr contains BOTH a _decls.h dllimport warning and
        goto jump-to-label/crosses-initialization errors, classify as
        'goto_error' — the real C++ error takes priority over the
        decls_header_warning artifact."""
        stderr = (
            "In file included from <command-line>:\n"
            "D:\\project\\.ghidra-exports\\_decls.h:19040:17: "
            "warning: 'DWORD GetCurrentProcessId()' redeclared without dllimport "
            "attribute: previous dllimport ignored [-Wattributes]\n"
            "19040 | DWORD __stdcall GetCurrentProcessId(void);\n"
            "      |                 ^~~~~~~~~~~~~~~~~~~\n"
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpk962m2ok.cpp: In function "
            "'int UpdateSystemState()':\n"
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpk962m2ok.cpp:89:1: "
            "error: jump to label 'label_166a4'\n"
            "   89 | label_166a4:\n"
            "      | ^~~~~~~~~~~\n"
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpk962m2ok.cpp:47:22: "
            "note:   from here\n"
            "   47 |                 goto label_166a4;\n"
            "      |                      ^~~~~~~~~~~\n"
            "C:\\Users\\vladk\\AppData\\Local\\Temp\\tmpk962m2ok.cpp:81:21: "
            "note:   crosses initialization of 'int32_t status'\n"
            "   81 |             int32_t status = FUN_0049f0d0();\n"
            "      |                     ^~~~~~\n"
        )
        assert classify_compile_error(stderr) == "goto_error"

    def test_goto_error_pure_goto_crosses_init(self) -> None:
        """When stderr contains ONLY goto-crosses-initialization errors
        (no _decls.h warning), classify as 'goto_error'."""
        stderr = (
            "C:\\Temp\\tmp.cpp: In function 'int update()':\n"
            "C:\\Temp\\tmp.cpp:89:1: error: jump to label 'label_foo'\n"
            "   89 | label_foo:\n"
            "      | ^~~~~~~~~\n"
            "C:\\Temp\\tmp.cpp:47:22: note:   from here\n"
            "   47 |                 goto label_foo;\n"
            "      |                      ^~~~~~~~~\n"
            "C:\\Temp\\tmp.cpp:81:21: note:   crosses initialization of "
            "'int32_t status'\n"
            "   81 |             int32_t status = FUN_0049f0d0();\n"
            "      |                     ^~~~~~\n"
        )
        assert classify_compile_error(stderr) == "goto_error"

    def test_goto_error_jump_to_label_only(self) -> None:
        """When stderr contains 'error: jump to label' without
        the 'crosses initialization' note, classify as 'goto_error'."""
        stderr = "error: jump to label 'label_skip'\n   10 | label_skip:\n      | ^~~~~~~~~~\n"
        assert classify_compile_error(stderr) == "goto_error"

    def test_goto_error_crosses_init_only(self) -> None:
        """When stderr contains 'crosses initialization' without
        'jump to label', classify as 'goto_error'."""
        stderr = (
            "note:   crosses initialization of 'int32_t value'\n"
            "   42 |             int32_t value = get_value();\n"
            "      |                     ^~~~~\n"
        )
        assert classify_compile_error(stderr) == "goto_error"

    def test_decls_header_warning_still_classifies_when_no_real_error(self) -> None:
        """When stderr contains ONLY the _decls.h dllimport warning
        (no goto/crosses-init or other real body errors), it must still
        classify as 'decls_header_warning' — NOT as 'goto_error'."""
        stderr = (
            "In file included from <command-line>:\n"
            "D:\\project\\.ghidra-exports\\_decls.h:19040:17: "
            "error: 'DWORD GetCurrentProcessId()' redeclared without dllimport "
            "attribute: previous dllimport ignored [-Werror=attributes]\n"
            "19040 | DWORD __stdcall GetCurrentProcessId(void);\n"
            "      |                 ^~~~~~~~~~~~~~~~~~~\n"
            "cc1plus.exe: all warnings being treated as errors\n"
        )
        assert classify_compile_error(stderr) == "decls_header_warning"


# ---------------------------------------------------------------------------
# 8. End-to-end: FunctionVerdict JSON roundtrip with compile error
# ---------------------------------------------------------------------------


def test_function_verdict_json_roundtrip_with_compile_error() -> None:
    """Given a FunctionVerdict with compile_error, When serialized to JSON
    via json.dumps, Then deserialization preserves all fields including
    compile_error and compile_error_category."""
    import json

    fv = FunctionVerdict(
        address="0x00414580",
        verdict="FAIL_NO_RETRY",
        compiles=False,
        files_matched=1,
        match_strategy="by_address",
        compile_error="error: too many arguments to function 'bar'",
        compile_error_category="too_many_arguments",
    )
    j = fv.to_json_dict()
    raw = json.dumps(j, sort_keys=True, ensure_ascii=True)
    loaded = json.loads(raw)
    assert loaded["compile_error"] == "error: too many arguments to function 'bar'"
    assert loaded["compile_error_category"] == "too_many_arguments"


# ---------------------------------------------------------------------------
# 9. No secrets or raw full prompts included
# ---------------------------------------------------------------------------


def test_compile_error_does_not_contain_full_prompt() -> None:
    """Given a long compiler stderr that includes prompt-like text as a
    substring, When truncated, the result must not contain the raw full
    prompt (it is bounded by truncation)."""
    # Simulate a situation where a build error accidentally echoes back a
    # long prompt prefix from the LLM.
    prompt_text = (
        "System: You are a reverse engineering assistant.\n"
        "You must translate the following C code from Ghidra into "
        "readable C++23 code.\n"
        "Follow these rules:\n"
        "1. Use meaningful names\n"
        "2. Use cstdint types\n" + "3. Keep the original logic\n" * 200
        # Make the prompt long enough to trigger truncation
    )
    # Stderr that contains the prompt as a substring
    full_stderr = (
        "In file included from <command-line>:\n"
        "error: 'uint32_t' does not name a type\n"
        + f"note: in expansion of macro from prompt:\n{prompt_text}\n"
        + "error: expected ';' before '}' token\n"
        "fatal: too many errors\n"
    )
    result = truncate_compile_error(full_stderr)
    # The result should be bounded (below COMPILE_ERROR_MAX_LENGTH)
    from re_agent.build.transform.diagnostics import COMPILE_ERROR_MAX_LENGTH

    assert len(result) <= COMPILE_ERROR_MAX_LENGTH + 200  # small slack for marker
    # The result should still contain the key GCC error info
    assert "error:" in result
    assert "does not name a type" in result or "expected" in result
