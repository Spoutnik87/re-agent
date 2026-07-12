"""Tests for --no-persist mode (persist=False) in build transform.

Verifies:
1. CLI parses ``--no-persist`` correctly.
2. ``persist=True`` (default) writes generated files, report, cache, state.
3. ``persist=False`` skips ALL disk writes, cache sets, and save_state calls.
4. Results and diagnostics are still computed in memory and returned.
5. ``persist=False`` with ``compile_per_function=True`` still skips compilation
   and produces SKIPPED_COMPILE verdicts — zero I/O, zero compile calls.
6. ``persist=False`` ignores pre-existing resume state: functions are processed
   regardless and the resume file is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from re_agent.build.state.cache import TransformCache
from re_agent.cli.main import build_parser

# ---------------------------------------------------------------------------
# 1. CLI arg parsing
# ---------------------------------------------------------------------------


def test_build_parser_accepts_no_persist() -> None:
    """Given the build subparser, When --no-persist is provided, Then the
    parsed namespace has ``no_persist`` set to True."""
    parser = build_parser()
    args = parser.parse_args(["build", "--no-persist"])
    assert args.no_persist is True


def test_build_parser_no_persist_defaults_false() -> None:
    """Given the build subparser, When --no-persist is omitted, Then
    ``no_persist`` defaults to the argparse default (falsy / None)."""
    parser = build_parser()
    args = parser.parse_args(["build"])
    # argparse store_true stores False when absent
    assert args.no_persist is False


def test_build_parser_no_persist_with_other_args() -> None:
    """Given --no-persist combined with other transform args, When parsed,
    Then all values are present."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "build",
            "--phase",
            "transform",
            "--module",
            "physics",
            "--max-subunits",
            "3",
            "--no-persist",
        ]
    )
    assert args.phase == "transform"
    assert args.module == "physics"
    assert args.max_subunits == 3
    assert args.no_persist is True


# ---------------------------------------------------------------------------
# Helpers (reuse patterns from test_cli_guardrails.py)
# ---------------------------------------------------------------------------

_M1 = "renderer"
_M2 = "physics"


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


def _make_cfg(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    """Build a cfg namespace that points all paths under tmp_path.

    Keyword overrides are applied at the top level (e.g. extra fields
    not covered by the standard structure).

    By default sets resume and optimization so write paths are exercised.
    """
    input_ns = SimpleNamespace(
        decompiled_dir=str(tmp_path / "decompiled_stubs"),
    )
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
        cache_enabled=True,
        cache_path=str(tmp_path / ".cr-agent-cache.json"),
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
    resume_ns = SimpleNamespace(
        enabled=True,
        state_path=str(tmp_path / "cr-agent-state.json"),
    )
    modules_ns = SimpleNamespace(expected=[])
    cfg = SimpleNamespace(
        input=input_ns,
        output=output_ns,
        project=project_ns,
        optimization=optimization_ns,
        validation=validation_ns,
        resume=resume_ns,
        modules=modules_ns,
        model="test-model",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_llm_cfg() -> SimpleNamespace:
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


def _passing_result(function: str, path: str, content: str) -> dict[str, Any]:
    """Build a passing result dict as emitted by process_subunit."""
    return {
        "function": function,
        "module": _M1,
        "compiles": True,
        "files": [{"path": path, "content": content}],
        "verdict": "PASS",
    }


# ---------------------------------------------------------------------------
# 2. persist=True (default) — writes happen
# ---------------------------------------------------------------------------


def test_persist_true_creates_temp_dir_and_writes_files(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=True (default), When process_modules runs with passing
    results, Then temp_transformed dir, module subdir, and generated .cpp
    files are created on disk."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    def _fake_process_subunit(ctx: dict, *a: Any, **kw: Any) -> list[dict]:
        return [
            _passing_result("0x00000000a", "0x00000000a__test.cpp", "void test() {}"),
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_process_subunit)

    # persist=True is the default
    mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    # temp_transformed dir exists
    temp_dir = tmp_path / "temp_transformed"
    assert temp_dir.is_dir(), "temp_transformed dir must exist when persist=True"

    # module subdir exists
    module_dir = temp_dir / _M1
    assert module_dir.is_dir(), "module subdir must exist when persist=True"

    # generated file was written
    generated = list(module_dir.glob("*.cpp"))
    assert len(generated) == 1, f"Expected 1 generated .cpp file, got {len(generated)}"
    assert generated[0].read_text(encoding="utf-8") == "void test() {}"


def test_persist_true_writes_report_json(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=True, When process_modules runs, Then cr-agent-report.json
    is written to the work directory."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(
        mp, "process_subunit", lambda *a, **kw: [_passing_result("0x00000000a", "f.cpp", "void f() {}")]
    )

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    report_path = tmp_path / "cr-agent-report.json"
    assert report_path.is_file(), "cr-agent-report.json must exist when persist=True"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["total"] >= 1
    assert report["summary"]["passed"] >= 1


def test_persist_true_calls_save_state(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=True and resume enabled, When process_modules runs, Then
    save_state is called at least once."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(mp, "process_subunit", lambda *a, **kw: [])

    save_state_mock = MagicMock(wraps=mp.save_state)
    monkeypatch.setattr(mp, "save_state", save_state_mock)

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert save_state_mock.call_count > 0, "save_state must be called when persist=True"


def test_persist_true_writes_cache(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=True and cache enabled, When process_modules runs with
    passing results, Then cache.set is called and cache is persisted to disk."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(
        mp,
        "process_subunit",
        lambda *a, **kw: [_passing_result("0x00000000a", "f.cpp", "void f() {}")],
    )

    # Track TransformCache.set at the class level (keeps hash_prompt intact)
    original_set = TransformCache.set
    set_called = [False]

    def _tracking_set(self: TransformCache, *args: Any, **kwargs: Any) -> None:
        set_called[0] = True
        original_set(self, *args, **kwargs)

    monkeypatch.setattr(TransformCache, "set", _tracking_set)

    cache_path = tmp_path / ".cr-agent-cache.json"

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    assert set_called[0], "cache.set must be called when persist=True"

    # Cache file exists on disk
    assert cache_path.is_file(), "cache file must exist on disk when persist=True"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "0x00000000a" in cached, "cache must contain the processed address"


# ---------------------------------------------------------------------------
# 3. persist=False — no writes at all
# ---------------------------------------------------------------------------


def test_persist_false_no_temp_dir_created(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs, Then temp_transformed
    directory is NOT created."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(mp, "process_subunit", lambda *a, **kw: [])

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    temp_dir = tmp_path / "temp_transformed"
    assert not temp_dir.exists(), "temp_transformed dir must NOT be created when persist=False"


def test_persist_false_no_report_written(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs, Then cr-agent-report.json
    is NOT written to disk."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(mp, "process_subunit", lambda *a, **kw: [])

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    report_path = tmp_path / "cr-agent-report.json"
    assert not report_path.exists(), "cr-agent-report.json must NOT be written when persist=False"


def test_persist_false_no_save_state_called(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs, Then save_state is
    NEVER called."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(mp, "process_subunit", lambda *a, **kw: [])

    save_state_mock = MagicMock()
    monkeypatch.setattr(mp, "save_state", save_state_mock)

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    save_state_mock.assert_not_called(), "save_state must NOT be called when persist=False"


def test_persist_false_no_cache_set_called(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs with passing results,
    Then cache.set is NEVER called."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(
        mp,
        "process_subunit",
        lambda *a, **kw: [_passing_result("0x00000000a", "f.cpp", "void f() {}")],
    )

    # Track TransformCache.set at the class level (keeps hash_prompt intact)
    original_set = TransformCache.set
    set_called = [False]

    def _tracking_set(self: TransformCache, *args: Any, **kwargs: Any) -> None:
        set_called[0] = True
        original_set(self, *args, **kwargs)

    monkeypatch.setattr(TransformCache, "set", _tracking_set)

    cache_path = tmp_path / ".cr-agent-cache.json"

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    assert not set_called[0], "cache.set must NOT be called when persist=False"

    # Cache file itself must NOT exist (no writes at all)
    assert not cache_path.exists(), "cache file must NOT be created when persist=False"


def test_persist_false_no_generated_files_written(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs with passing results
    that include file content, Then no generated .cpp files are written
    anywhere under tmp_path (beyond pre-existing stubs)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(
        mp,
        "process_subunit",
        lambda *a, **kw: [
            _passing_result("0x00000000a", "0x00000000a__gen.cpp", "void generated() {}"),
        ],
    )

    # Snapshot files before run
    before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}

    mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}

    # No new files should appear (report, cache, generated files all skipped)
    new_files = after - before
    assert len(new_files) == 0, f"Expected no new files when persist=False, got: {new_files}"


# ---------------------------------------------------------------------------
# 4. persist=False — results still returned in memory
# ---------------------------------------------------------------------------


def test_persist_false_still_returns_summary(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False, When process_modules runs, Then the returned
    summary dict still contains correct total/passed/failed counts (results
    are computed in memory even without disk writes)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    call_no = [0]

    def _fake_subunit(*a: Any, **kw: Any) -> list[dict]:
        call_no[0] += 1
        compiles = call_no[0] == 1  # first passes, second fails
        return [
            {
                "function": f"0x{call_no[0]:08x}",
                "module": _M1,
                "compiles": compiles,
                "files": [],
                "verdict": "PASS" if compiles else "FAIL_NO_RETRY",
            },
        ]

    monkeypatch.setattr(mp, "process_subunit", _fake_subunit)

    summary = mp.process_modules(cfg, _make_llm_cfg(), module=_M1, persist=False)

    # 2 subunits → 2 results expected
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    # Token tracking still works
    assert "total_tokens" in summary


# ---------------------------------------------------------------------------
# 5. persist=True preserved by default (no regression)
# ---------------------------------------------------------------------------


def test_persist_default_is_true(monkeypatch: Any, tmp_path: Path) -> None:
    """Given process_modules called without persist= keyword, Then persist
    defaults to True (existing behaviour preserved)."""
    _write_modules_json(tmp_path, [_M1])
    decompiled_dir = _make_decompiled_stubs(tmp_path, [_M1])
    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(decompiled_dir)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())
    monkeypatch.setattr(mp, "process_subunit", lambda *a, **kw: [])

    # Call WITHOUT persist= keyword → should default to True
    mp.process_modules(cfg, _make_llm_cfg(), module=_M1)

    # temp_transformed dir must exist (persist=True behavior)
    assert (tmp_path / "temp_transformed").is_dir(), "Default persist=True must create temp_transformed dir"


# ---------------------------------------------------------------------------
# 6. PipelineState not constructed when persist=False (cmd_build level)
# ---------------------------------------------------------------------------


def test_cmd_build_no_persist_skips_state_construction(monkeypatch: Any, tmp_path: Path) -> None:
    """Given --no-persist, When cmd_build runs, Then PipelineState is NOT
    constructed (== state is None so update_build/flush are never called)."""
    config_path = tmp_path / "re-agent.yaml"
    tp = tmp_path.as_posix()
    config_path.write_text(
        f"""
pipeline:
  state_file: "{tp}/pipeline-state.json"
build:
  input:
    decompiled_dir: "{tp}/decompiled_stubs"
  output:
    work_dir: "{tp}"
"""
    )

    # Write modules.json so the transform phase can at least start
    _write_modules_json(tmp_path, [_M1])
    _make_decompiled_stubs(tmp_path, [_M1])

    import re_agent.cli.cmd_build as cb
    from re_agent.state.pipeline_state import PipelineState

    # Mock the analyzer phases to skip (we don't have real data)
    monkeypatch.setattr("re_agent.build.analyze.graph_builder.build_graph", lambda cfg: {})
    monkeypatch.setattr(
        "re_agent.build.analyze.clusterer.cluster",
        lambda g, cfg: {"metadata": {"module_count": 0, "orphan_count": 0}, "modules": {}},
    )
    monkeypatch.setattr("re_agent.build.analyze.indexer.index_modules", lambda m, cfg: None)

    # Mock process_modules to avoid LLM calls
    monkeypatch.setattr(
        "re_agent.build.transform.module_processor.process_modules",
        lambda *a, **kw: {"total": 0, "passed": 0, "failed": 0, "total_tokens": 0},
    )
    # Mock build_tree to avoid errors
    monkeypatch.setattr("re_agent.build.assemble.tree_builder.build_tree", lambda cfg: None)

    # Track PipelineState construction
    orig_init = PipelineState.__init__
    init_called_with_persist = [False]

    def _tracking_init(self, path: str | Path) -> None:
        # When persist=False, PipelineState should NOT be constructed
        init_called_with_persist[0] = True
        orig_init(self, path)

    monkeypatch.setattr(PipelineState, "__init__", _tracking_init)

    # Run with --no-persist and --phase to limit scope (we have mocks for all phases).
    # Note: --config is a global argparse flag and must come BEFORE the subcommand.
    args = build_parser().parse_args(["--config", str(config_path), "build", "--no-persist", "--phase", "transform"])
    cb.cmd_build(args)

    # PipelineState.__init__ should have been called because persist=False causes
    # state = None (not constructed)
    assert not init_called_with_persist[0], "PipelineState must NOT be constructed when --no-persist is given"


# ---------------------------------------------------------------------------
# 7. Integration test: persist=False with process_subunit NOT mocked,
#    diagnostics/raw-capture/compile ENABLED, LLM stub → zero IO.
# ---------------------------------------------------------------------------
# This is the core NO-GO guard test. It does NOT mock process_subunit.
# Instead it:
#   - Provides a real config with diagnostics_dir, raw_response_capture, and
#     compile_per_function all enabled.
#   - Uses a _FakeProvider (LLM stub) that returns a canned multi-file response.
#   - Calls process_modules with persist=False.
#   - Asserts zero files/dirs/state/cache/report/objects are created on disk.
#   - Asserts results are returned in memory with SKIPPED_COMPILE verdicts.


def test_persist_false_integration_no_io(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False with all IO gates WIDE open (diagnostics enabled,
    raw capture enabled, compile enabled, cache enabled, resume enabled),
    When process_modules runs through process_subunit (NOT mocked), Then
    ZERO files, directories, state, cache, report, or objects are created
    on disk — and results are returned in memory with SKIPPED_COMPILE."""

    from re_agent.llm.protocol import Message

    # ── Module setup ──
    module_name = "physics"
    _write_modules_json(tmp_path, [module_name])
    _make_decompiled_stubs(tmp_path, [module_name])

    # ── Config with ALL IO gates WIDE OPEN ──
    cfg = _make_cfg(
        tmp_path,
        # Override optimization to enable diagnostics + raw capture
        optimization=SimpleNamespace(
            cache_enabled=True,
            cache_path=str(tmp_path / ".cr-agent-cache.json"),
            subunit_size=10,
            context_window=3,
            diagnostics_dir=str(tmp_path / "work-packets"),
            raw_response_capture=True,
        ),
        # Override resume to be enabled with explicit state path
        resume=SimpleNamespace(
            enabled=True,
            state_path=str(tmp_path / "cr-agent-state.json"),
        ),
    )
    # The global model attribute may interfere; ensure it's set
    cfg.model = "test-model"
    cfg.input.decompiled_dir = str(tmp_path / "decompiled_stubs")

    # ── LLM stub that returns a proper multi-file response ──
    class _StubLLM:
        """Minimal LLMProvider stub returning a canned response with
        address-bearing file blocks (will be matched by address)."""

        supports_conversations = False
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_hit_tokens = 0
        total_cache_miss_tokens = 0
        total_calls = 0

        def send(self, messages: list[Message], **kwargs: Any) -> str:
            self.total_calls += 1
            # Return address-bearing files for both functions in the subunit.
            # Using explicit // Original function: comments so matching works.
            return (
                "// FILE: src/physics/0x00000000a__MassCalc.cpp\n"
                "// Original function: 0x00000000a\n"
                '#include "_decls.h"\n'
                "void MassCalc() {}\n"
                "\n"
                "// FILE: src/physics/0x00000000b__ForceApply.cpp\n"
                "// Original function: 0x00000000b\n"
                '#include "_decls.h"\n'
                "void ForceApply() {}\n"
            )

        def new_conversation(self, system: str) -> str:
            raise NotImplementedError

        def resume(self, conversation_id: str, message: str) -> str:
            raise NotImplementedError

        def delete_conversation(self, conversation_id: str) -> None:
            raise NotImplementedError

    import re_agent.build.transform.module_processor as mp

    # Mock the LLM provider factory but NOT process_subunit
    monkeypatch.setattr(
        mp,
        "create_provider",
        lambda llm_cfg: _StubLLM(),
    )

    # Snapshot all files and dirs BEFORE the run
    before_files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    before_dirs = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}

    # ── Run with persist=False ──
    llm_cfg = _make_llm_cfg()
    summary = mp.process_modules(
        cfg,
        llm_cfg,
        module=module_name,
        persist=False,
    )

    # ── Snapshot AFTER ──
    after_files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    after_dirs = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}

    new_files = after_files - before_files
    new_dirs = after_dirs - before_dirs

    # ── Assert: ZERO new files ──
    assert len(new_files) == 0, f"persist=False must create ZERO new files, got: {new_files}"
    # ── Assert: ZERO new directories ──
    assert len(new_dirs) == 0, f"persist=False must create ZERO new directories, got: {new_dirs}"

    # ── Assert: no temp_transformed dir anywhere ──
    assert not (tmp_path / "temp_transformed").exists(), "temp_transformed dir must NOT exist when persist=False"

    # ── Assert: no report ──
    assert not (tmp_path / "cr-agent-report.json").exists(), "cr-agent-report.json must NOT exist when persist=False"

    # ── Assert: no cache file ──
    assert not (tmp_path / ".cr-agent-cache.json").exists(), "Cache file must NOT exist when persist=False"

    # ── Assert: no state file ──
    state_path = tmp_path / "cr-agent-state.json"
    assert not state_path.exists(), "State file must NOT exist when persist=False"

    # ── Assert: no diagnostics/work-packet dir or files ──
    diag_dir = tmp_path / "work-packets"
    assert not diag_dir.exists(), "Diagnostics dir must NOT be created when persist=False"

    # ── Assert: summary returned in memory with correct counts ──
    # With compile skipped, all matched functions have compiles=False,
    # so passed=0 and failed=total.
    assert summary["total"] == 2, f"Expected 2 total results, got {summary['total']}"
    assert summary["passed"] == 0, f"Expected 0 passed (compile skipped), got {summary['passed']}"
    assert summary["failed"] == summary["total"], (
        f"failed must equal total when compile is skipped, " f"got failed={summary['failed']} total={summary['total']}"
    )
    assert "total_tokens" in summary, "token tracking must still work"


# ---------------------------------------------------------------------------
# 8. persist=False + compile_per_function=True → still NO compile calls,
#    SKIPPED_COMPILE verdicts, zero I/O.
# ---------------------------------------------------------------------------
# This test is the "full-open-gate" variant of §7: it explicitly sets
# validation.compile_per_function=True and patches _compile_generated_cpp
# to fail *if* called.  Because persist=False unconditionally disables
# compilation (subunit_processor line 818: ``compile_enabled = … and persist``),
# the patched helper MUST NOT be reached — and we assert zero IO on top.


def test_persist_false_compile_enabled_still_skips_compile(monkeypatch: Any, tmp_path: Path) -> None:
    """Given persist=False with compile_per_function=True, When
    process_modules runs, Then _compile_generated_cpp is NEVER called,
    verdicts are SKIPPED_COMPILE, and zero new files/dirs appear on disk."""
    module_name = "physics"
    _write_modules_json(tmp_path, [module_name])
    _make_decompiled_stubs(tmp_path, [module_name])

    # ── Config: compile_per_function=True (explicit) + all IO gates open ──
    cfg = _make_cfg(
        tmp_path,
        validation=SimpleNamespace(
            compile_per_function=True,
            compile_per_module=False,
            compile_final_project=False,
            max_compile_retries=1,
        ),
        optimization=SimpleNamespace(
            cache_enabled=True,
            cache_path=str(tmp_path / ".cr-agent-cache.json"),
            subunit_size=10,
            context_window=3,
            diagnostics_dir=str(tmp_path / "work-packets"),
            raw_response_capture=True,
        ),
        resume=SimpleNamespace(
            enabled=True,
            state_path=str(tmp_path / "cr-agent-state.json"),
        ),
    )
    cfg.model = "test-model"
    cfg.input.decompiled_dir = str(tmp_path / "decompiled_stubs")

    import re_agent.build.transform.module_processor as mp
    import re_agent.build.transform.subunit_processor as sp

    # ── Patch _compile_generated_cpp to explode if called ──
    compile_called = False

    def _exploding_compile(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        nonlocal compile_called
        compile_called = True
        raise AssertionError(
            "_compile_generated_cpp was called despite persist=False — " "compile should be unconditionally skipped"
        )

    monkeypatch.setattr(sp, "_compile_generated_cpp", _exploding_compile)

    # ── LLM stub from §7 ──
    class _StubLLM:
        supports_conversations = False
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_hit_tokens = 0
        total_cache_miss_tokens = 0
        total_calls = 0

        def send(self, messages: list, **kwargs: Any) -> str:
            self.total_calls += 1
            return (
                "// FILE: src/physics/0x00000000a__MassCalc.cpp\n"
                "// Original function: 0x00000000a\n"
                '#include "_decls.h"\n'
                "void MassCalc() {}\n"
                "\n"
                "// FILE: src/physics/0x00000000b__ForceApply.cpp\n"
                "// Original function: 0x00000000b\n"
                '#include "_decls.h"\n'
                "void ForceApply() {}\n"
            )

        def new_conversation(self, system: str) -> str:
            raise NotImplementedError

        def resume(self, conversation_id: str, message: str) -> str:
            raise NotImplementedError

        def delete_conversation(self, conversation_id: str) -> None:
            raise NotImplementedError

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _StubLLM())

    # ── Wrap process_subunit to capture individual results ──
    _captured_results: list[dict[str, Any]] = []
    _original_subunit = mp.process_subunit

    def _capturing_subunit(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        results = _original_subunit(*args, **kwargs)
        _captured_results.extend(results)
        return results

    monkeypatch.setattr(mp, "process_subunit", _capturing_subunit)

    # ── Snapshot before ──
    before_files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    before_dirs = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}

    # ── Run ──
    summary = mp.process_modules(cfg, _make_llm_cfg(), module=module_name, persist=False)

    # ── Assert: compile helper NEVER called ──
    assert not compile_called, "_compile_generated_cpp must NOT be called when persist=False"

    # ── Assert: zero new files or dirs (identical to §7) ──
    after_files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    after_dirs = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}

    new_files = after_files - before_files
    new_dirs = after_dirs - before_dirs

    assert len(new_files) == 0, f"persist=False must create ZERO new files, got: {new_files}"
    assert len(new_dirs) == 0, f"persist=False must create ZERO new dirs, got: {new_dirs}"

    # ── Assert: specific expected paths absent ──
    assert not (tmp_path / "temp_transformed").exists()
    assert not (tmp_path / "cr-agent-report.json").exists()
    assert not (tmp_path / ".cr-agent-cache.json").exists()
    assert not (tmp_path / "cr-agent-state.json").exists()
    assert not (tmp_path / "work-packets").exists()

    # ── Assert: each individual result has SKIPPED_COMPILE verdict ──
    assert len(_captured_results) == 2, f"Expected 2 captured results, got {len(_captured_results)}"
    for i, r in enumerate(_captured_results):
        assert (
            r["verdict"] == "SKIPPED_COMPILE"
        ), f"Result {i}: expected verdict='SKIPPED_COMPILE', got {r['verdict']!r}"
        assert r["compiles"] is False, f"Result {i}: expected compiles=False, got {r['compiles']}"

    # ── Assert: summary matches individual results ──
    assert summary["total"] == len(
        _captured_results
    ), f"summary total={summary['total']} differs from captured {len(_captured_results)}"
    assert summary["passed"] == 0, f"Expected 0 passed (compile skipped), got {summary['passed']}"
    assert summary["failed"] == summary["total"], (
        f"Expected failed == total when compile is skipped, " f"got failed={summary['failed']} total={summary['total']}"
    )
    assert "total_tokens" in summary


# ---------------------------------------------------------------------------
# 9. persist=False ignores pre-existing resume state — functions still
#    processed, resume file untouched.
# ---------------------------------------------------------------------------
# When persist=True + resume enabled, process_modules loads the resume state
# and skips any module listed in ``completed_modules``.
#
# When persist=False, the resume state is never loaded (module_processor.py:66:
# ``if persist and cfg.resume.enabled:``), so every module is processed
# regardless.  The resume file on disk must NOT be modified in any way
# (no timestamp update, no write).


def test_persist_false_resume_state_ignored(monkeypatch: Any, tmp_path: Path) -> None:
    """Given a pre-existing resume file that lists ``renderer`` as
    completed, When process_modules runs with persist=False and
    module='renderer', Then the module is STILL processed (resume not
    loaded) and the resume file content is exactly unchanged."""
    module_name = "renderer"
    _write_modules_json(tmp_path, [module_name])
    _make_decompiled_stubs(tmp_path, [module_name])

    # ── Pre-create resume state that would normally skip renderer ──
    resume_path = tmp_path / "cr-agent-state.json"
    resume_content = {
        "completed_modules": [module_name],
        "current_module": module_name,
        "current_subunit": 0,
        "phase": "transform",
    }
    resume_path.write_text(json.dumps(resume_content, indent=2), encoding="utf-8")
    # Snapshot file content and timestamp.
    before_content = resume_path.read_bytes()
    before_mtime = resume_path.stat().st_mtime_ns

    cfg = _make_cfg(tmp_path)
    cfg.input.decompiled_dir = str(tmp_path / "decompiled_stubs")
    # Ensure resume is enabled (the cfg helper already sets this)
    cfg.resume.enabled = True
    cfg.resume.state_path = str(resume_path)

    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: _FakeLLMProvider())

    call_log: list[str] = []

    call_idx = [0]  # tracks which subunit we are in

    def _logging_subunit(ctx: dict, *a: Any, **kw: Any) -> list[dict]:
        call_log.append("process_subunit called")
        call_idx[0] += 1
        addr = f"0x{call_idx[0]:08x}"
        return [
            {
                "function": addr,
                "module": module_name,
                "compiles": False,
                "verdict": "SKIPPED_COMPILE",
                "files": [],
            },
        ]

    monkeypatch.setattr(mp, "process_subunit", _logging_subunit)

    # ── Run with persist=False ──
    summary = mp.process_modules(cfg, _make_llm_cfg(), module=module_name, persist=False)

    # ── Assert: module WAS processed despite resume state ──
    assert len(call_log) > 0, (
        "process_subunit must be called even when resume state exists " "— persist=False must ignore completed_modules"
    )
    assert summary["total"] == 2, f"Expected 2 functions processed, got {summary['total']}"

    # ── Assert: resume file content exactly unchanged (byte-level) ──
    after_content = resume_path.read_bytes()
    assert after_content == before_content, (
        "Resume file content must NOT change when persist=False — "
        f"expected:\n{before_content}\ngot:\n{after_content}"
    )
    after_mtime = resume_path.stat().st_mtime_ns
    assert after_mtime == before_mtime, "Resume file mtime must NOT change when persist=False"
