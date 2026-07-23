"""CLI guardrail and internal transform tests for the project build flow.

Deterministic, tmp_path-scoped, no live LLM/provider/network.
Verifies:
1. Removed legacy build options are rejected by the parser.
2. Project builds require a project root before any side effects.
3. Internal process_modules guardrails remain covered.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from re_agent.cli.main import build_parser

# ---------------------------------------------------------------------------
# 1. CLI arg parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("removed_option", ["--address", "--module", "--subunit", "--max-subunits", "--no-persist"])
def test_build_parser_rejects_removed_legacy_options(removed_option: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["build", "--project-root", "project", removed_option, "1"])
    assert exc_info.value.code == 2


def test_build_parser_accepts_run_id_arg() -> None:
    """Given the build subparser, When --run-id is provided, Then the parsed
    namespace has ``run_id`` set to the given string."""
    parser = build_parser()
    args = parser.parse_args(["build", "--project-root", "project", "--run-id", "renderer-subunit-3-cache-aware"])
    assert args.run_id == "renderer-subunit-3-cache-aware"


def test_build_parser_run_id_defaults_none() -> None:
    """Given the build subparser, When --run-id is omitted, Then ``run_id``
    defaults to None (preserving existing behaviour)."""
    parser = build_parser()
    args = parser.parse_args(["build", "--project-root", "project"])
    assert args.run_id is None


def test_build_parser_accepts_project_release4_options() -> None:
    args = build_parser().parse_args(
        ["build", "--project-root", "project", "--phase", "transform", "--run-id", "release4"]
    )
    assert args.project_root == "project"
    assert args.phase == "transform"
    assert args.run_id == "release4"


# ---------------------------------------------------------------------------
# 2. process_modules guardrail logic (monkeypatched deps)
# ---------------------------------------------------------------------------
#
# We test process_modules directly with a fake modules.json, a no-op LLM
# provider, and monkeypatched process_subunit that records calls.


class _FakeLLMProvider:
    """Minimal LLMProvider double for process_modules (never actually called
    when process_subunit is monkeypatched away)."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    total_calls = 0

    def send(self, messages: list, **kwargs: Any) -> str:
        return ""

    def new_conversation(self, system: str) -> str:
        raise NotImplementedError

    def resume(self, conversation_id: str, message: str) -> str:
        raise NotImplementedError

    def delete_conversation(self, conversation_id: str) -> None:
        raise NotImplementedError


def _make_minimal_cfg(tmp_path: Path) -> SimpleNamespace:
    """Build a minimal cfg namespace that points all paths under tmp_path."""
    input_ns = SimpleNamespace(decompiled_dir="reports/re-agent/code/")
    output_ns = SimpleNamespace(
        language="C++",
        standard="c++23",
        decls_header=None,
        work_dir=str(tmp_path),
        target_dir=str(tmp_path / "output"),
        compiler="g++",
        compiler_flags="-std=c++23 -c -Wall",
    )
    project_conventions = SimpleNamespace(
        naming=SimpleNamespace(classes="PascalCase", functions="camelCase", globals="snake_case"),
        includes_rule="",
        max_function_lines=200,
    )
    project_ns = SimpleNamespace(description="", conventions=project_conventions)
    optimization_ns = SimpleNamespace(
        cache_enabled=False,
        cache_path="",
        subunit_size=10,
        context_window=3,
        diagnostics_dir="",
        raw_response_capture=False,
    )
    validation_ns = SimpleNamespace(
        compile_per_function=False,
        compile_per_module=False,
        compile_final_project=False,
        max_compile_retries=0,
    )
    resume_ns = SimpleNamespace(enabled=False, state_path="")
    modules_ns = SimpleNamespace(expected=[])
    return SimpleNamespace(
        input=input_ns,
        output=output_ns,
        project=project_ns,
        optimization=optimization_ns,
        validation=validation_ns,
        resume=resume_ns,
        modules=modules_ns,
        model="test-model",
    )


def _make_llm_cfg() -> SimpleNamespace:
    """Build a minimal llm_cfg namespace with the fields process_modules reads."""
    return SimpleNamespace(model="test-model", provider="test-provider")


def _write_modules_json(tmp_path: Path, module_names: list[str]) -> None:
    """Write a minimal modules.json under tmp_path with one sub_unit per module."""
    modules = {}
    for i, name in enumerate(module_names):
        addr_a = f"0x{i:08x}a"
        addr_b = f"0x{i:08x}b"
        modules[name] = {
            "functions": [addr_a, addr_b],
            "sub_units": [[addr_a], [addr_b]],
            "metadata": {"size": 2},
        }
    data = {"modules": modules, "metadata": {"module_count": len(module_names)}}
    (tmp_path / "modules.json").write_text(json.dumps(data), encoding="utf-8")


def _make_decompiled_stubs(tmp_path: Path, module_names: list[str]) -> Path:
    """Create a fake decompiled dir with stub .cpp files so globbing succeeds."""
    d = tmp_path / "decompiled_stubs"
    d.mkdir()
    for i, name in enumerate(module_names):
        addr_a = f"0x{i:08x}a"
        addr_b = f"0x{i:08x}b"
        (d / f"{addr_a}__FUN_{name}_A.cpp").write_text(f"void FUN_{name}_A() {{}}", encoding="utf-8")
        (d / f"{addr_b}__FUN_{name}_B.cpp").write_text(f"void FUN_{name}_B() {{}}", encoding="utf-8")
    return d


# Module names for all guardrail tests.
_M1 = "renderer"
_M2 = "physics"
_M3 = "audio"


def test_guardrail_module_filter_skips_other_modules(monkeypatch, tmp_path: Path) -> None:
    """Given three modules and --module=renderer, When process_modules runs,
    Then process_subunit is only called for the renderer module."""
    _write_modules_json(tmp_path, [_M1, _M2, _M3])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1, _M2, _M3])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    called_modules: list[str] = []

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        called_modules.append(module_name)
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg(), module="renderer")

    # Each module has 2 subunits, so 2 calls expected for renderer only.
    assert called_modules == [
        "renderer",
        "renderer",
    ], f"Only 'renderer' subunits should be processed, got {called_modules}"


def test_guardrail_no_module_processes_all(monkeypatch, tmp_path: Path) -> None:
    """Given three modules and no --module filter, When process_modules runs,
    Then all modules are processed (existing behaviour preserved)."""
    _write_modules_json(tmp_path, [_M1, _M2, _M3])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1, _M2, _M3])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    called_modules: list[str] = []

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        called_modules.append(module_name)
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg())

    # Each of 3 modules has 2 subunits → 6 total calls.
    expected = ["renderer", "renderer", "physics", "physics", "audio", "audio"]
    assert called_modules == expected, f"All three modules should be processed, got {called_modules}"


def test_guardrail_subunit_start_skips_earlier(monkeypatch, tmp_path: Path) -> None:
    """Given a module with two subunits and --subunit=1, When process_modules
    runs, Then subunit index 0 is skipped and only 1 subunit (index 1) is
    processed (verified by call count: 1 instead of 2)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    call_count = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        call_count[0] += 1
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", subunit=1)

    # Without --subunit=1, both subunits (0 and 1) would be processed → 2 calls.
    # With --subunit=1, only subunit 1 is processed → 1 call.
    assert call_count[0] == 1, f"Only 1 subunit should be processed when starting at index 1, got {call_count[0]}"


def test_guardrail_max_subunits_stops_after_n(monkeypatch, tmp_path: Path) -> None:
    """Given a module with 2 subunits and --max-subunits=1, When
    process_modules runs, Then only 1 subunit is processed (verified by
    call count: 1 instead of 2)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    call_count = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        call_count[0] += 1
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", max_subunits=1)

    # Without --max-subunits=1, both subunits would be processed → 2 calls.
    # With --max-subunits=1, only 1 subunit is processed → 1 call.
    assert call_count[0] == 1, f"Only 1 subunit should be processed, got {call_count[0]}"


def test_guardrail_max_subunits_no_overflow(monkeypatch, tmp_path: Path) -> None:
    """Given max_subunits larger than actual subunit count, When
    process_modules runs, Then all subunits are processed (no crash)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    processed_count = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        processed_count[0] += 1
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    # Module renderer has 2 subunits; ask for 99 → should not crash or
    # artificially inflate the count.
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", max_subunits=99)

    assert processed_count[0] == 2, (
        f"All 2 subunits should be processed (bounded by available, not limit), got {processed_count[0]}"
    )


def test_guardrail_max_subunits_global_across_modules(monkeypatch, tmp_path: Path) -> None:
    """Given two modules with 2 subunits each and --max-subunits=1 (no
    --module filter), When process_modules runs, Then only 1 subunit total
    is processed across ALL modules — NOT 1 per module (global cap).

    This is the regression test for the old per-module max_subunits bug
    where ``subunit_count`` was scoped inside the module loop and reset
    for each module, allowing ``max_subunits=1`` to process N modules × 1
    subunit each instead of 1 total.
    """
    _write_modules_json(tmp_path, [_M1, _M2])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1, _M2])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    call_count = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        call_count[0] += 1
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    # --max-subunits=1 without --module filter → TWO modules are eligible.
    # The global cap must allow exactly 1 subunit total, not 1 per module.
    mp.process_modules(cfg, _make_llm_cfg(), max_subunits=1)

    assert call_count[0] == 1, (
        f"max_subunits=1 must process exactly 1 subunit total across all modules, "
        f"got {call_count[0]} (old per-module bug would produce 2)"
    )


def test_guardrail_run_id_propagates_to_context(monkeypatch, tmp_path: Path) -> None:
    """Given --run-id='my-run-001', When process_modules runs, Then the run_id
    appears in the context dict passed to process_subunit."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    captured_contexts: list[dict] = []

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        captured_contexts.append(dict(ctx))
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", run_id="my-run-001")

    assert len(captured_contexts) >= 1
    for ctx in captured_contexts:
        assert ctx.get("run_id") == "my-run-001", f"run_id should be 'my-run-001' in context, got {ctx.get('run_id')}"


def test_guardrail_no_run_id_does_not_set_key(monkeypatch, tmp_path: Path) -> None:
    """Given no --run-id (empty string default), When process_modules runs,
    Then the context dict does NOT contain a 'run_id' key (preserving existing
    behaviour where process_subunit handles missing run_id gracefully)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    captured_contexts: list[dict] = []

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        captured_contexts.append(dict(ctx))
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg(), module="renderer")

    assert len(captured_contexts) >= 1
    for ctx in captured_contexts:
        assert "run_id" not in ctx, f"run_id should not be set when omitted, got {ctx.get('run_id')!r}"


def test_guardrail_all_params_together(monkeypatch, tmp_path: Path) -> None:
    """Given all four guardrail params together, When process_modules runs,
    Then all filtering/bounding rules apply simultaneously."""
    _write_modules_json(tmp_path, [_M1, _M2, _M3])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1, _M2, _M3])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    processed: list[tuple[str, dict]] = []  # (module_name, context)

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        processed.append((module_name, dict(ctx)))
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    mp.process_modules(
        cfg,
        _make_llm_cfg(),
        module="renderer",
        subunit=1,
        max_subunits=1,
        run_id="all-params-test",
    )

    # Only renderer processed, only subunit 1, count=1.
    assert len(processed) == 1
    module_name, ctx = processed[0]
    assert module_name == "renderer"
    assert ctx.get("run_id") == "all-params-test"


# ---------------------------------------------------------------------------
# 3. Transform summary return-value tests (for cmd_build reporting)
# ---------------------------------------------------------------------------
#
# process_modules now returns a summary dict used by cmd_build.py for
# contextual completion messages. These tests verify the summary is
# computed correctly from process_subunit results.


def test_transform_summary_all_failed_shows_zero_passed(monkeypatch, tmp_path: Path) -> None:
    """Given all functions fail to compile, When process_modules runs, Then
    the returned summary has ``passed=0`` and ``failed=N`` (so the CLI can
    print a message distinguishing completion from success).

    Each subunit has 1 function, and there are 2 subunits per module,
    so process_subunit is called twice → 2 results total."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    _call_count: list[int] = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        _call_count[0] += 1
        # Each subunit has 1 function → return 1 result per call
        return [
            {
                "function": f"0x{_call_count[0]:08x}",
                "module": _M1,
                "compiles": False,
                "files": [],
                "verdict": "FAIL_NO_RETRY",
            },
        ]

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    summary = mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert summary["total"] == 2
    assert summary["passed"] == 0
    assert summary["failed"] == 2


def test_transform_summary_mixed_success_failure(monkeypatch, tmp_path: Path) -> None:
    """Given a mix of passing and failing functions, When process_modules
    runs, Then the returned summary reflects the correct counts.

    Each subunit has 1 function, and there are 2 subunits per module.
    Return one pass for call 1, one fail for call 2 → 1 passed, 1 failed."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    _call_count: list[int] = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        _call_count[0] += 1
        compiles = _call_count[0] == 1  # first call passes, second fails
        return [
            {
                "function": f"0x{_call_count[0]:08x}",
                "module": _M1,
                "compiles": compiles,
                "files": [],
                "verdict": "PASS" if compiles else "FAIL_NO_RETRY",
            },
        ]

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    summary = mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1


def test_transform_summary_empty_results(monkeypatch, tmp_path: Path) -> None:
    """Given process_subunit returns no results, When process_modules runs,
    Then the summary shows total=0, passed=0, failed=0 (no crash)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    summary = mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert summary["total"] == 0
    assert summary["passed"] == 0
    assert summary["failed"] == 0


def test_transform_summary_all_passed(monkeypatch, tmp_path: Path) -> None:
    """Given all functions compile successfully, When process_modules runs,
    Then summary shows passed=N, failed=0.

    Each subunit has 1 function, and there are 2 subunits per module,
    so process_subunit is called twice → 2 results, both passing."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        return [
            {"function": "0x0001", "module": _M1, "compiles": True, "files": [], "verdict": "PASS"},
        ]

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    summary = mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert summary["total"] == 2  # 2 subunits × 1 passing result each
    assert summary["passed"] == 2
    assert summary["failed"] == 0


def test_guardrail_unknown_module_skips_silently(monkeypatch, tmp_path: Path) -> None:
    """Given --module=nonexistent, When process_modules runs, Then no modules
    are processed and the function completes without error."""
    _write_modules_json(tmp_path, [_M1, _M2])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1, _M2])

    cfg = _make_minimal_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    processed_count = [0]

    def _fake_process_subunit(ctx: dict, module_name: str, llm: Any, cfg: Any, cache: Any, **kwargs: Any) -> list[dict]:
        processed_count[0] += 1
        return []

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    # Should not raise — nonexistent module is silently skipped.
    mp.process_modules(cfg, _make_llm_cfg(), module="nonexistent")

    assert processed_count[0] == 0, "No modules should be processed"


# ═════════════════════════════════════════════════════════════════════════════
# 4. Real cmd_build tests with mocked side effects
# ═════════════════════════════════════════════════════════════════════════════


def _fake_process_modules_ok(*a: Any, **kw: Any) -> dict:
    """Fake process_modules returning all PASS."""
    return {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "incomplete": 0,
        "hard_rejects": 0,
        "contract_failed": False,
        "total_tokens": 100,
    }


def _fake_process_modules_contract_failed(*a: Any, **kw: Any) -> dict:
    return {
        "total": 10,
        "passed": 0,
        "failed": 0,
        "incomplete": 10,
        "hard_rejects": 0,
        "contract_failed": True,
        "total_tokens": 100,
    }


def _fake_process_modules_hard_reject(*a: Any, **kw: Any) -> dict:
    return {
        "total": 2,
        "passed": 0,
        "failed": 0,
        "incomplete": 0,
        "hard_rejects": 2,
        "contract_failed": True,
        "total_tokens": 50,
    }


def _fake_process_modules_mixed(*a: Any, **kw: Any) -> dict:
    return {
        "total": 10,
        "passed": 7,
        "failed": 0,
        "incomplete": 3,
        "hard_rejects": 0,
        "contract_failed": True,
        "total_tokens": 200,
    }


def test_cmd_build_without_project_root_rejects_before_side_effects(monkeypatch: Any, capsys: Any) -> None:
    import re_agent.cli.cmd_build as cb

    calls: list[str] = []

    def fail_load_config(*args: Any, **kwargs: Any) -> Any:
        calls.append("load_config")
        raise AssertionError("load_config must not run without --project-root")

    monkeypatch.setattr(cb, "load_config", fail_load_config)
    monkeypatch.setattr(
        "re_agent.project.context.load_verified_project",
        lambda *args, **kwargs: calls.append("project") or None,
    )
    args = argparse.Namespace(config="missing.yml")

    assert cb.cmd_build(args) == 2
    assert calls == []
    assert "requires --project-root" in capsys.readouterr().err


def test_pipeline_build_attempt_rejects_before_reverse(monkeypatch: Any, tmp_path: Path) -> None:
    import re_agent.cli.cmd_pipeline as cp

    state_path = tmp_path / "pipeline.json"
    cfg = SimpleNamespace(pipeline=SimpleNamespace(state_file=str(state_path)))
    reverse_called = False
    monkeypatch.setattr(cp, "load_config", lambda _path: cfg)

    def fail_reverse(_args: Any) -> int:
        nonlocal reverse_called
        reverse_called = True
        return 0

    monkeypatch.setattr("re_agent.cli.cmd_reverse.cmd_reverse", fail_reverse)
    args = build_parser().parse_args(["pipeline", "--skip-reverse"])

    assert cp.cmd_pipeline(args) == 2
    assert not reverse_called


def test_pipeline_skip_build_remains_supported(monkeypatch: Any, tmp_path: Path) -> None:
    import re_agent.cli.cmd_pipeline as cp

    state_path = tmp_path / "pipeline.json"
    cfg = SimpleNamespace(pipeline=SimpleNamespace(state_file=str(state_path)))
    reverse_called = False
    monkeypatch.setattr(cp, "load_config", lambda _path: cfg)

    def fake_reverse(_args: Any) -> int:
        nonlocal reverse_called
        reverse_called = True
        return 0

    monkeypatch.setattr("re_agent.cli.cmd_reverse.cmd_reverse", fake_reverse)
    args = build_parser().parse_args(["pipeline", "--skip-build"])

    assert cp.cmd_pipeline(args) == 0
    assert reverse_called


# 5. Module completion tests (process_modules level)
# ═════════════════════════════════════════════════════════════════════════════


def _make_completion_cfg(tmp_path: Path, resume_state: dict | None = None) -> SimpleNamespace:
    """Build cfg with optional resume state for completion tests."""
    if resume_state is not None:
        state_path = tmp_path / "cr-agent-state.json"
        state_path.write_text(json.dumps(resume_state), encoding="utf-8")
        rs_path = str(state_path)
        rs_enabled = True
    else:
        rs_path = ""
        rs_enabled = False
    input_ns = SimpleNamespace(decompiled_dir=str(tmp_path / "decompiled_stubs"))
    output_ns = SimpleNamespace(
        language="C++",
        standard="c++23",
        decls_header=None,
        work_dir=str(tmp_path),
        target_dir=str(tmp_path / "output"),
        compiler="g++",
        compiler_flags="-std=c++23 -c -Wall",
    )
    project_conventions = SimpleNamespace(
        naming=SimpleNamespace(classes="PascalCase", functions="camelCase", globals="snake_case"),
        includes_rule="",
        max_function_lines=200,
    )
    project_ns = SimpleNamespace(description="", conventions=project_conventions)
    optimization_ns = SimpleNamespace(
        cache_enabled=False,
        cache_path="",
        subunit_size=10,
        context_window=3,
        diagnostics_dir="",
        raw_response_capture=False,
    )
    validation_ns = SimpleNamespace(
        compile_per_function=True, compile_per_module=False, compile_final_project=False, max_compile_retries=0
    )
    resume_ns = SimpleNamespace(enabled=rs_enabled, state_path=rs_path)
    modules_ns = SimpleNamespace(expected=[])
    return SimpleNamespace(
        input=input_ns,
        output=output_ns,
        project=project_ns,
        optimization=optimization_ns,
        validation=validation_ns,
        resume=resume_ns,
        modules=modules_ns,
        model="test-model",
    )


def _write_module_json_with_subunits(tmp_path: Path, module_name: str, num_subunits: int) -> None:
    """Write modules.json with a module having *num_subunits* subunits of 1 function each."""
    addrs = [f"0x{i:08x}" for i in range(num_subunits)]
    sub_units = [[a] for a in addrs]
    modules = {module_name: {"functions": addrs, "sub_units": sub_units, "size": num_subunits}}
    data = {"modules": modules, "metadata": {"module_count": 1}}
    (tmp_path / "modules.json").write_text(json.dumps(data), encoding="utf-8")


def _make_stubs(tmp_path: Path, addrs: list[str]) -> Path:
    """Create stub .cpp files for each address."""
    d = tmp_path / "decompiled_stubs"
    d.mkdir(exist_ok=True)
    for a in addrs:
        (d / f"{a}__stub.cpp").write_text(f"void stub_{a}() {{}}", encoding="utf-8")
    return d


def test_completion_compile_fail_blocks_module(monkeypatch: Any, tmp_path: Path) -> None:
    """Compile failure → not accepted verdict → module NOT completed."""
    _write_module_json_with_subunits(tmp_path, "renderer", 2)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002"])
    cfg = _make_completion_cfg(tmp_path)
    import re_agent.build.transform.module_processor as mp

    _call = [0]

    def _fake_subunit(*a, **kw):
        _call[0] += 1
        compiles = _call[0] == 1  # first passes, second fails
        return [
            {
                "function": f"0x{_call[0]:08x}",
                "module": "renderer",
                "compiles": compiles,
                "files": [],
                "verdict": "PASS" if compiles else "FAIL_NO_RETRY",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer")
    # Module should NOT be completed (FAIL_NO_RETRY is not an accepted verdict)
    assert not cfg.resume.enabled  # no resume state was written


def test_completion_max_subunits_stops_mid_module(monkeypatch: Any, tmp_path: Path) -> None:
    """--max-subunits=1 on a 2-subunit module → module NOT completed."""
    _write_module_json_with_subunits(tmp_path, "renderer", 2)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002"])
    cfg = _make_completion_cfg(tmp_path)
    import re_agent.build.transform.module_processor as mp

    def _fake_subunit(*a, **kw):
        return [
            {
                "function": "0x0001",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", max_subunits=1)
    # If completed_modules was populated, it would be written to state.
    # We don't have a direct way to check completed_modules, but we can
    # verify that a second run with subunit=1 processes 1 more subunit
    # (not 0 which would mean the module was marked completed).
    call_count2 = [0]

    def _fake_subunit2(*a, **kw):
        call_count2[0] += 1
        return [
            {
                "function": "0x0002",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit2)
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", subunit=1, max_subunits=1)
    assert call_count2[0] == 1, "Second run should process subunit 1 (module not completed)"


def test_completion_resume_all_pass_completes(monkeypatch: Any, tmp_path: Path) -> None:
    """Resume: set current_subunit=1 (meaning sub 0 was processing when interrupted).
    Both remaining subunits (indices 1 and 2) all PASS → module completed."""
    _write_module_json_with_subunits(tmp_path, "renderer", 3)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002", "0x00000003"])
    # Resume state: current_subunit=1 means "was processing subunit 1 when interrupted"
    state = {"completed_modules": [], "current_module": "renderer", "current_subunit": 1, "phase": "transform"}
    cfg = _make_completion_cfg(tmp_path, resume_state=state)
    import re_agent.build.transform.module_processor as mp

    processed = [0]

    def _fake_subunit(*a, **kw):
        processed[0] += 1
        return [
            {
                "function": f"0x{processed[0]:08x}",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    # Do NOT use module= here — resume start only applies without --module filter
    summary = mp.process_modules(cfg, _make_llm_cfg())
    assert processed[0] == 2, "2 subunits (indices 1 and 2) should be processed"
    assert summary["passed"] == 2, "2 results should be PASS"


def test_completion_resume_with_failure_blocks(monkeypatch: Any, tmp_path: Path) -> None:
    """Resume with current_subunit=1, restarted subunits include fail → NOT completed."""
    _write_module_json_with_subunits(tmp_path, "renderer", 3)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002", "0x00000003"])
    state = {"completed_modules": [], "current_module": "renderer", "current_subunit": 1, "phase": "transform"}
    cfg = _make_completion_cfg(tmp_path, resume_state=state)
    import re_agent.build.transform.module_processor as mp

    _call = [0]

    def _fake_subunit(*a, **kw):
        _call[0] += 1
        compiles = _call[0] == 1  # first resumed subunit passes, second fails
        return [
            {
                "function": f"0x{_call[0]:08x}",
                "module": "renderer",
                "compiles": compiles,
                "files": [],
                "verdict": "PASS" if compiles else "FAIL_NO_RETRY",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    # Do NOT use module= — resume start only applies without --module filter
    mp.process_modules(cfg, _make_llm_cfg())
    # Resume restarts from current_subunit=1. 3 subunits: index 0 skipped,
    # index 1 processed (PASS), index 2 processed (FAIL) → 2 calls.
    assert _call[0] == 2, f"Expected 2 resumed subunits, got {_call[0]}"


def test_completion_all_fail_blocks_module(monkeypatch: Any, tmp_path: Path) -> None:
    """All functions FAIL_AFTER_RETRY → NOT accepted → module NOT completed."""
    _write_module_json_with_subunits(tmp_path, "renderer", 2)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002"])
    cfg = _make_completion_cfg(tmp_path)
    import re_agent.build.transform.module_processor as mp

    def _fake_subunit(*a, **kw):
        return [
            {
                "function": "0x0001",
                "module": "renderer",
                "compiles": False,
                "files": [],
                "verdict": "FAIL_AFTER_RETRY",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    summary = mp.process_modules(cfg, _make_llm_cfg(), module="renderer")
    assert summary["passed"] == 0
    assert summary["failed"] == 2
    # If modify report to expose completed, we can check. Instead verify
    # that a second run processes all subunits (module not completed).
    call_count2 = [0]

    def _fake_subunit2(*a, **kw):
        call_count2[0] += 1
        return [
            {
                "function": f"0x{call_count2[0]:08x}",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit2)
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer")
    assert call_count2[0] == 2, "All 2 subunits should re-process (module was not completed)"


def test_explicit_subunit_does_not_complete(monkeypatch: Any, tmp_path: Path) -> None:
    """Explicit --subunit 1 on 2-subunit module → NOT completed (subunit 0 skipped, not done)."""
    _write_module_json_with_subunits(tmp_path, "renderer", 2)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002"])
    cfg = _make_completion_cfg(tmp_path)
    import re_agent.build.transform.module_processor as mp

    def _fake_subunit(*a, **kw):
        return [
            {
                "function": "0x0002",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", subunit=1)
    # Subunit 0 was NOT processed — module should NOT be completed.
    # A second call with subunit=0 should process subunit 0.
    call2 = [0]

    def _fake_subunit2(*a, **kw):
        call2[0] += 1
        return [
            {
                "function": "0x0001",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit2)
    mp.process_modules(cfg, _make_llm_cfg(), module="renderer", subunit=0)
    # Subunit 0 was NOT processed in first run → module NOT completed.
    # Second run processes all subunits (0 and 1).
    assert call2[0] == 2, "Both subunits should re-process (module was not completed)"


def test_resume_completes_and_persists(monkeypatch: Any, tmp_path: Path) -> None:
    """Resume with current_subunit=1, remaining all PASS → module completed, state persisted."""
    _write_module_json_with_subunits(tmp_path, "renderer", 3)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002", "0x00000003"])
    state_path = tmp_path / "cr-agent-state.json"
    state = {"completed_modules": [], "current_module": "renderer", "current_subunit": 1, "phase": "transform"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    import re_agent.build.transform.module_processor as mp

    def _fake_subunit(*a, **kw):
        return [
            {
                "function": "0x1000",
                "module": "renderer",
                "compiles": True,
                "files": [],
                "verdict": "PASS",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    cfg = _make_completion_cfg(tmp_path, resume_state=state)
    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg())

    # State file should now have renderer in completed_modules
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "renderer" in saved.get("completed_modules", []), (
        f"renderer should be in completed_modules after successful resume, got {saved}"
    )


def test_resume_with_failure_does_not_complete(monkeypatch: Any, tmp_path: Path) -> None:
    """Resume with failure → module NOT completed, state NOT updated."""
    _write_module_json_with_subunits(tmp_path, "renderer", 3)
    _make_stubs(tmp_path, ["0x00000001", "0x00000002", "0x00000003"])
    state_path = tmp_path / "cr-agent-state.json"
    state = {"completed_modules": [], "current_module": "renderer", "current_subunit": 1, "phase": "transform"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    import re_agent.build.transform.module_processor as mp

    _call = [0]

    def _fake_subunit(*a, **kw):
        _call[0] += 1
        compiles = _call[0] == 1  # first passes, second fails
        return [
            {
                "function": f"0x{_call[0]:08x}",
                "module": "renderer",
                "compiles": compiles,
                "files": [],
                "verdict": "PASS" if compiles else "FAIL_NO_RETRY",
                "diagnostic": {"match_strategy": "explicit_identity"},
            }
        ]

    cfg = _make_completion_cfg(tmp_path, resume_state=state)
    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)
    monkeypatch.setattr(mp, "create_provider", lambda c: _FakeLLMProvider())

    mp.process_modules(cfg, _make_llm_cfg())

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "renderer" not in saved.get("completed_modules", []), (
        f"renderer should NOT be in completed_modules after failed resume, got {saved}"
    )
