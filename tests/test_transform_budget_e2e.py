"""End-to-end regression tests for the transform budget system.

Calls the real production path (process_modules / cmd_build) with fully faked
LLM providers and compilers to verify:

1. Call cap (calls=4) with retryable compile errors → at most 4 calls, ≤1 retry
2. compile=0 cap → no subunit retry, single call
3. Global retry cap consumed by first subunit blocks the second subunit
4. Token budget exhaustion (2×30k) → BUDGET_EXCEEDED/exit2, no cache/write
5. Provider error during recovery/retry → distinct PROVIDER_ERROR, non-zero exit
6. No-persist stdout is single parseable JSON with budget/calls/verdicts

These tests should FAIL on current code if the documented budget bugs exist.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

from re_agent.llm.protocol import Message, ProviderUsage

# ================================================================
# — Fake LLM providers
# ================================================================


class _FakeTokenTrackingProvider:
    """Base class: satisfies LLMProvider protocol with usage tracking."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens: int | None = 0
    total_cache_miss_tokens: int | None = 0

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


class _FakeProvider(_FakeTokenTrackingProvider):
    """Returns a fixed response; each send = 80 tokens (50+30)."""

    def __init__(self, response: str = "ok") -> None:
        self._response = response

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        self.total_prompt_tokens += 50
        self.total_completion_tokens += 30
        return self._response


class _SeqProvider(_FakeTokenTrackingProvider):
    """Returns canned responses in sequence; each send = 80 tokens."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._idx = 0

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        self.total_calls += 1
        self.total_prompt_tokens += 50
        self.total_completion_tokens += 30
        return self._responses[idx]


class _RaisingProvider(_FakeTokenTrackingProvider):
    """Always raises RuntimeError on send()."""

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        raise RuntimeError("Simulated provider failure")


class _TokenExhaustProvider(_FakeTokenTrackingProvider):
    """Reports high token usage (30k per call) to exhaust token budget."""

    def __init__(self, token_cost: int = 30000, response: str = "ok") -> None:
        self._token_cost = token_cost
        self._response = response

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        self.total_prompt_tokens += self._token_cost
        self.total_completion_tokens += 0
        return self._response


# ================================================================
# — Fake compile helpers
# ================================================================


def _retryable_compile(*args: Any, **kwargs: Any) -> tuple[bool, str]:
    return (False, "error: expected ';' before '}' token")


def _ok_compile(*args: Any, **kwargs: Any) -> tuple[bool, str]:
    return (True, "")


# ================================================================
# — Config helpers
# ================================================================


def _build_cfg(
    tmp_path_str: str,
    *,
    calls: int = 4,
    tokens: int = 50000,
    compile_retries: int = 1,
    max_retries: int = 2,
) -> Any:
    """Return a nested-namespace config matching what process_modules expects.

    All paths (work_dir, decompiled_dir) are under *tmp_path_str*.
    """

    class _Opt:
        context_window = 0
        cache_enabled = True
        cache_path = str(Path(tmp_path_str) / ".cache.json")
        diagnostics_dir = str(Path(tmp_path_str) / "diag")
        raw_response_capture = False
        max_llm_calls_per_run = calls
        max_llm_tokens_per_run = tokens
        max_compile_retry_calls_per_run = compile_retries

    class _Out:
        language = "C++"
        standard = "c++23"
        compiler = "g++"
        compiler_flags = "-std=c++23 -c -Wall"
        decls_header = None
        target_dir = ""
        work_dir = tmp_path_str

    class _In:
        decompiled_dir = str(Path(tmp_path_str) / "code")
        ghidra_exports = ""

    class _Naming:
        classes = "PascalCase"
        functions = "camelCase"
        globals = "snake_case"

    class _Conv:
        naming = _Naming()
        includes_rule = ""
        max_function_lines = 200

    class _Proj:
        name = ""
        description = ""
        conventions = _Conv()

    class _Val:
        max_compile_retries = max_retries
        compile_per_function = True
        target_contract_mode = "required"
        compile_per_module = False

    class _Resume:
        enabled = False
        state_path = ""

    class _Cfg:
        optimization = _Opt()
        output = _Out()
        input = _In()
        project = _Proj()
        validation = _Val()
        resume = _Resume()

    return _Cfg()


class _LLMCfg:
    """Minimal llm_cfg for process_modules."""

    provider = "fake"
    model = "test-model"


# ================================================================
# — Fixtures
# ================================================================


def _setup_fs(
    tmp_path: Path,
    n_funcs: int = 5,
    sub_unit_map: list[list[str]] | None = None,
) -> Path:
    """Create modules.json + decompiled .cpp files under *tmp_path*.

    *sub_unit_map* controls subunit splitting (default: one subunit with all).
    """
    code_dir = tmp_path / "code"
    code_dir.mkdir(parents=True, exist_ok=True)

    addrs = [f"0x{i:04x}" for i in range(n_funcs)]
    for i, addr in enumerate(addrs):
        cpp = code_dir / f"{addr}__f{i}.cpp"
        cpp.write_text(f"void f{i}() {{}}", encoding="utf-8")

    sub_units = sub_unit_map if sub_unit_map is not None else [addrs]

    modules = {
        "modules": {
            "mod": {
                "functions": addrs,
                "sub_units": sub_units,
            }
        },
        "metadata": {"module_count": 1, "orphan_count": 0},
    }
    (tmp_path / "modules.json").write_text(json.dumps(modules), encoding="utf-8")
    return tmp_path


def _multi_module_fs(
    tmp_path: Path,
    module_subs: dict[str, list[list[str]]],
) -> Path:
    """Create modules.json with multiple modules.

    *module_subs*: ``{name: [sub_unit1, sub_unit2, ...]}`` where each
    sub_unit is a list of address strings.
    """
    code_dir = tmp_path / "code"
    code_dir.mkdir(parents=True, exist_ok=True)

    modules: dict[str, Any] = {}
    seen: set[str] = set()

    for mod_name, subs in module_subs.items():
        flat: list[str] = []
        for su in subs:
            flat.extend(su)
        for addr in flat:
            if addr not in seen:
                seen.add(addr)
                idx = int(addr, 16)
                cpp = code_dir / f"{addr}__f{idx}.cpp"
                cpp.write_text(f"void f{idx}() {{}}", encoding="utf-8")
        modules[mod_name] = {
            "functions": flat,
            "sub_units": subs,
        }

    data = {
        "modules": modules,
        "metadata": {"module_count": len(modules), "orphan_count": 0},
    }
    (tmp_path / "modules.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


# ================================================================
# — Shared monkey-patch helpers
# ================================================================


def _patch_compile(monkeypatch: Any, fail: bool = True) -> None:
    """Patch compile_check + compile_generated_file_set in subunit_processor."""
    import re_agent.build.transform.subunit_processor as sp

    if fail:
        monkeypatch.setattr(sp, "compile_check", _retryable_compile)
        monkeypatch.setattr(
            sp,
            "compile_generated_file_set",
            lambda files, path, cfg: (False, "error: expected ';' before '}' token"),
        )
    else:
        monkeypatch.setattr(sp, "compile_check", _ok_compile)
        monkeypatch.setattr(
            sp,
            "compile_generated_file_set",
            lambda files, path, cfg: (True, ""),
        )


def _patch_create_provider(monkeypatch: Any, provider: Any) -> None:
    """Patch create_provider in module_processor to return *provider*."""
    import re_agent.build.transform.module_processor as mp

    monkeypatch.setattr(mp, "create_provider", lambda llm_cfg: provider)


# ================================================================
# — Test 1: 5 functions, retryable compile errors, caps
# ================================================================


class TestBudgetE2E:
    """E2E budget regression tests via process_modules."""

    # ------------------------------------------------------------------
    # Test 1: calls=4, tokens=50000, compile=1  — 5 functions, all retryable
    # ------------------------------------------------------------------
    def test_1_calls_capped_and_retry_honored(self, tmp_path: Path, monkeypatch: Any) -> None:
        """5 functions, all retryable compile errors.

        Budget: calls=4, tokens=50000, compile_retry=1.
        Expected: ≤4 calls, ≤1 compile retry, ideally 2 (initial + subunit).
        """
        _setup_fs(tmp_path, n_funcs=5)
        _patch_compile(monkeypatch, fail=True)
        cfg = _build_cfg(str(tmp_path), calls=4, tokens=50000, compile_retries=1)
        # persist=True so compile is enabled and retry can be triggered
        cfg.optimization.cache_enabled = False

        # Provider: 5 valid TARGETs in a single response
        lines: list[str] = []
        for i in range(5):
            addr = f"0x{i:04x}"
            lines.append(f"// TARGET: {i} {addr}")
            lines.append(f"// FILE: src/mod/f{i}.cpp\nvoid f{i}() {{}}")
        provider = _SeqProvider(["\n".join(lines)])
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        summary = process_modules(cfg, _LLMCfg(), persist=True)

        # 1a) at most 4 LLM calls (budget cap)
        assert provider.total_calls <= 4, f"Expected ≤4 calls, got {provider.total_calls}"
        # 1b) ideally 2: initial + subunit retry (≥2 retryable → subunit retry)
        #   With compile_retries=1 (budget), the subunit retry is allowed.
        assert provider.total_calls == 2, (
            f"Expected exactly 2 calls (initial + subunit retry), got {provider.total_calls}"
        )

        # 1c) summary shows 5 functions processed
        assert summary["total"] == 5, f"Expected 5 functions, got {summary['total']}"

        # 1d) budget not exceeded (we stayed within caps)
        assert summary.get("budget_exceeded", 0) == 0, "budget should not be exceeded"

    # ------------------------------------------------------------------
    # Test 2: same scenario with compile=0 → no subunit retry
    # ------------------------------------------------------------------
    def test_2_compile_zero_no_retry(self, tmp_path: Path, monkeypatch: Any) -> None:
        """max_compile_retries=0 in config → no subunit retry, single initial call."""
        _setup_fs(tmp_path, n_funcs=5)
        _patch_compile(monkeypatch, fail=True)
        # max_retries=0 disables the subunit retry (subunit_processor line 1645:
        # "if failed_funcs and max_retries > 0 and n_retryable >= 2")
        cfg = _build_cfg(str(tmp_path), calls=4, tokens=50000, compile_retries=1, max_retries=0)
        cfg.optimization.cache_enabled = False

        lines: list[str] = []
        for i in range(5):
            addr = f"0x{i:04x}"
            lines.append(f"// TARGET: {i} {addr}")
            lines.append(f"// FILE: src/mod/f{i}.cpp\nvoid f{i}() {{}}")
        provider = _SeqProvider(["\n".join(lines)])
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        summary = process_modules(cfg, _LLMCfg(), persist=True)

        # With max_retries=0, subunit retry condition fails → only initial call
        assert provider.total_calls == 1, f"Expected exactly 1 call (no retry), got {provider.total_calls}"
        # Still 5 functions
        assert summary["total"] == 5

    # ------------------------------------------------------------------
    # Test 3: two modules, global retry cap consumed by first blocks second
    # ------------------------------------------------------------------
    def test_3_retry_consumed_by_first_blocks_second(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Two modules, each with 1 function.

        compile_retry_calls_remaining=1 (global).
        Module A: 1 function, retryable error → per-function retry consumes the cap.
        Module B: 1 function, retryable error → per-function retry *blocked*.

        Expected: Module A gets a retry (2 calls: initial + per-function),
                  Module B gets NO retry (1 call, FAIL_NO_RETRY).
        """
        # Two separate modules, each with one function in one subunit
        _multi_module_fs(
            tmp_path,
            {
                "mod_a": [["0x0000"]],
                "mod_b": [["0x0001"]],
            },
        )
        _patch_compile(monkeypatch, fail=True)
        cfg = _build_cfg(
            str(tmp_path),
            calls=10,
            tokens=50000,
            compile_retries=1,  # ← one per-function retry total, shared
            max_retries=1,
        )
        cfg.optimization.cache_enabled = False

        # Each module's initial call returns a valid TARGET
        provider = _SeqProvider(
            [
                "// TARGET: 0 0x0000\n// FILE: src/mod/f0.cpp\nvoid f0() {}\n",
                "// TARGET: 0 0x0001\n// FILE: src/mod/f1.cpp\nvoid f1() {}\n",
            ]
        )
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        summary = process_modules(cfg, _LLMCfg(), persist=True)

        # Total calls:
        #   mod_a initial (1)
        #   mod_a per-function retry (1) — compile_retry_calls_remaining: 1→0
        #   mod_b initial (1)
        #   mod_b per-function retry — BLOCKED (compile_retry_calls_remaining=0)
        # Total = 3
        assert provider.total_calls == 3, (
            f"Expected 3 total calls, got {provider.total_calls}: mod_a initial + retry (2) + mod_b initial (1)"
        )

        # mod_a's result should include the function
        assert summary["total"] == 2, f"Expected 2 functions, got {summary['total']}"

        # The budget summary should show compile_retry_calls_remaining = 0
        budget = summary.get("budget", {})
        if budget:
            assert budget.get("compile_retry_calls_remaining", -1) == 0, "compile retry budget should be fully consumed"

    # ------------------------------------------------------------------
    # Test 4: token budget exhaustion → BUDGET_EXCEEDED, no cache/write
    # ------------------------------------------------------------------
    def test_4_token_exhaustion_budget_exceeded_exit2(self, tmp_path: Path, monkeypatch: Any) -> None:
        """2 calls of 30k tokens each → budget flagged after second.

        Budget: calls=5, tokens=50000 (each call costs 30k).
        After call 2: tokens_remaining=-10000 → exceeded=True.
        Remaining LLM calls should be blocked → BUDGET_EXCEEDED.
        No files should be written, no cache entries.

        Exit code 2 should propagate (budget_exceeded > 0).
        """
        # 3 subunits → 3 initial calls, but budget exhausted after call 2
        _setup_fs(
            tmp_path,
            n_funcs=6,
            sub_unit_map=[["0x0000", "0x0001"], ["0x0002", "0x0003"], ["0x0004", "0x0005"]],
        )
        _patch_compile(monkeypatch, fail=False)  # compile OK, no retries
        cfg = _build_cfg(
            str(tmp_path),
            calls=5,
            tokens=50000,
            compile_retries=0,
            max_retries=0,
        )
        # Enable cache so we can verify nothing is cached
        cfg.optimization.cache_enabled = True
        # Disable persist checks inside process_subunit (compile OK, no retry)
        # so we can see budget exhaustion from token usage alone.

        provider = _TokenExhaustProvider(
            token_cost=30000,
            response=(
                "// TARGET: 0 0x0000\n// FILE: src/mod/f0.cpp\nvoid f0() {}\n"
                "// TARGET: 1 0x0001\n// FILE: src/mod/f1.cpp\nvoid f1() {}\n"
            ),
        )
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        # Capture no-persist stdout to check exit_code
        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            summary = process_modules(cfg, _LLMCfg(), persist=False)
        finally:
            sys.stdout = old_stdout

        stdout_text = captured.getvalue()

        # 4a) Exactly 2 LLM calls (budget exhausts at call 2, third blocked)
        assert provider.total_calls == 2, (
            f"Expected exactly 2 calls (budget exhausts at call 2), got {provider.total_calls}"
        )

        # 4b) budget_exceeded in summary > 0
        assert summary["budget_exceeded"] > 0, f"Expected budget_exceeded > 0, got {summary['budget_exceeded']}"

        # 4c) Some results should be BUDGET_EXCEEDED
        budget_exceeded_count = summary["budget_exceeded"]
        assert budget_exceeded_count > 0, "At least one function should be BUDGET_EXCEEDED"

        # 4d) The no-persist JSON should have exit_code=2 (unconditional)
        assert stdout_text.strip(), "No-persist stdout must be non-empty when budget exceeded"
        parsed = json.loads(stdout_text)
        assert parsed.get("exit_code") == 2, f"Expected exit_code=2 when budget exceeded, got {parsed.get('exit_code')}"
        assert parsed["summary"]["budget_exceeded"] > 0

        # 4e) No cache entry written (cache is at tmp_path / ".cache.json")
        # With persist=False, cache should NOT be created at all.
        cache_path = Path(cfg.optimization.cache_path)
        assert not cache_path.exists(), "Cache file should NOT exist with --no-persist (budget exceeded)"

        # 4f) No temp_transformed files should be written (persist=False)
        temp_dir = Path(tmp_path) / "temp_transformed"
        assert not temp_dir.exists() or not list(temp_dir.rglob("*")), (
            "No temp_transformed files should exist (persist=False)"
        )

    # ------------------------------------------------------------------
    # Test 5: provider error during recovery/retry → PROVIDER_ERROR, non-zero
    # ------------------------------------------------------------------
    def test_5_provider_error_distinct_and_not_cached(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Provider error during initial send → PROVIDER_ERROR verdict.

        PROVIDER_ERROR must be distinct from BUDGET_EXCEEDED, not cached,
        and produce a non-zero exit code (contract_failed).
        """
        _setup_fs(tmp_path, n_funcs=3)
        _patch_compile(monkeypatch, fail=False)
        cfg = _build_cfg(
            str(tmp_path),
            calls=5,
            tokens=50000,
            compile_retries=0,
            max_retries=0,
        )
        cfg.optimization.cache_enabled = True

        provider = _RaisingProvider()
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            summary = process_modules(cfg, _LLMCfg(), persist=False)
        finally:
            sys.stdout = old_stdout

        stdout_text = captured.getvalue()

        # 5a) All provider errors
        assert summary["provider_errors"] == 3, f"Expected 3 provider_errors, got {summary['provider_errors']}"
        assert summary["total"] == 3
        assert summary["passed"] == 0

        # 5b) contract_failed is True (provider_errors contributed)
        assert summary["contract_failed"] is True, "provider_errors should set contract_failed"

        # 5c) The no-persist JSON should have exit_code=2 (contract_failed)
        assert stdout_text.strip(), "No-persist stdout must be non-empty when provider errors"
        parsed = json.loads(stdout_text)
        assert parsed.get("exit_code") == 2, f"Expected exit_code=2 for provider errors, got {parsed.get('exit_code')}"

        # 5d) No cache entries written for provider_error verdicts
        cache_path = Path(cfg.optimization.cache_path)
        assert not cache_path.exists(), "Cache file should NOT exist (provider errors are not cacheable)"

    # ------------------------------------------------------------------
    # Test 5b: provider error during compile retry → distinct, not cached
    # ------------------------------------------------------------------
    def test_5b_provider_error_during_retry(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Initial call succeeds, but retry call fails with provider error.

        Verdicts should be PROVIDER_ERROR (not FAIL_AFTER_RETRY).
        """
        _setup_fs(tmp_path, n_funcs=2)
        _patch_compile(monkeypatch, fail=True)
        cfg = _build_cfg(
            str(tmp_path),
            calls=10,
            tokens=50000,
            compile_retries=2,
            max_retries=2,
        )
        cfg.optimization.cache_enabled = True

        # Use a custom provider that raises on every call after the first one.
        class _RetryRaisingProvider(_FakeTokenTrackingProvider):
            """First call succeeds; all subsequent send() calls raise."""

            _first_response = (
                "// TARGET: 0 0x0000\n// FILE: src/mod/f0.cpp\nvoid f0() {}\n"
                "// TARGET: 1 0x0001\n// FILE: src/mod/f1.cpp\nvoid f1() {}\n"
            )
            _call_count = 0

            def send(self, messages: list[Message], **kwargs: Any) -> str:
                self.total_calls += 1
                self.total_prompt_tokens += 50
                self.total_completion_tokens += 30
                self._call_count += 1
                if self._call_count == 1:
                    return self._first_response
                raise RuntimeError("Simulated provider error during retry")

        provider = _RetryRaisingProvider()
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        # Use persist=True so compile is enabled and triggers the retry
        summary = process_modules(cfg, _LLMCfg(), persist=True)

        # The initial call succeeded (2 TARGETs), then compile failed for both
        # → subunit retry triggered (n_retryable >= 2) → but retry raises
        # → the entire subunit returns PROVIDER_ERROR for all functions
        assert provider.total_calls >= 2, f"Expected at least 2 calls (initial + retry), got {provider.total_calls}"
        assert summary["provider_errors"] > 0, f"Expected provider_errors > 0, got {summary['provider_errors']}"
        assert summary["contract_failed"] is True

    # ------------------------------------------------------------------
    # Test 6: no-persist stdout is parseable JSON with budget/calls/verdicts
    # ------------------------------------------------------------------
    def test_6_no_persist_json_format(self, tmp_path: Path, monkeypatch: Any) -> None:
        """``process_modules(..., persist=False)`` writes a single parseable
        JSON object to stdout containing budget, calls, and verdicts,
        with NO human-readable text outside the JSON.
        """
        _setup_fs(tmp_path, n_funcs=3)
        _patch_compile(monkeypatch, fail=False)
        cfg = _build_cfg(
            str(tmp_path),
            calls=8,
            tokens=50000,
            compile_retries=2,
        )

        provider = _FakeProvider(
            "// TARGET: 0 0x0000\n// FILE: src/mod/f0.cpp\nvoid f0() {}\n"
            "// TARGET: 1 0x0001\n// FILE: src/mod/f1.cpp\nvoid f1() {}\n"
            "// TARGET: 2 0x0002\n// FILE: src/mod/f2.cpp\nvoid f2() {}\n"
        )
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        # Capture stdout
        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            process_modules(cfg, _LLMCfg(), persist=False)
        finally:
            sys.stdout = old_stdout

        stdout_text = captured.getvalue()

        # 6a) stdout is non-empty
        assert stdout_text.strip(), "No-persist stdout should not be empty"

        # 6b) stdout is a single parseable JSON object (no trailing text)
        parsed = json.loads(stdout_text)
        assert isinstance(parsed, dict), "No-persist output must be a JSON object"

        # 6c) Contains run_type, exit_code, summary, usage, results
        assert parsed.get("run_type") == "no-persist"
        assert "exit_code" in parsed
        assert "summary" in parsed
        assert "usage" in parsed
        assert "results" in parsed

        # 6d) Summary has budget/calls/verdicts
        s = parsed["summary"]
        assert "total" in s
        assert "passed" in s
        assert "failed" in s
        assert "budget_exceeded" in s
        assert "provider_errors" in s

        # 6e) Budget present
        assert "budget" in parsed
        b = parsed["budget"]
        assert "calls_remaining" in b
        assert "tokens_remaining" in b
        assert "compile_retry_calls_remaining" in b
        assert "exceeded" in b

        # 6f) Usage has call/token counts
        u = parsed["usage"]
        assert "total_calls" in u
        assert "prompt_tokens" in u
        assert "completion_tokens" in u

        # 6g) Results has per-function entries
        assert len(parsed["results"]) == 3
        for r in parsed["results"]:
            assert "function" in r
            assert "verdict" in r
            assert "compiles" in r
            assert "files_matched" in r
            assert "match_strategy" in r

        # 6h) No human-readable text outside JSON — the JSON should be the
        # only thing on stdout (no print() calls for summaries etc.)
        # Re-parse to ensure no trailing garbage
        # The JSON ends with a single newline (sys.stdout.write(json + "\n"))
        assert stdout_text.rstrip("\n") == json.dumps(parsed, indent=2), (
            "stdout must contain ONLY the JSON line, no extra text"
        )

    # ------------------------------------------------------------------
    # Test 4b: persist=True + budget exceeded → cache exists but empty
    # ------------------------------------------------------------------
    def test_4b_persist_true_cache_empty_on_budget_exceeded(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When persist=True and budget is exceeded, cache file may exist but
        must contain NO entries for BUDGET_EXCEEDED verdicts."""
        _setup_fs(
            tmp_path,
            n_funcs=6,
            sub_unit_map=[["0x0000", "0x0001"], ["0x0002", "0x0003"], ["0x0004", "0x0005"]],
        )
        _patch_compile(monkeypatch, fail=False)
        cfg = _build_cfg(
            str(tmp_path),
            calls=5,
            tokens=50000,
            compile_retries=0,
            max_retries=0,
        )
        cfg.optimization.cache_enabled = True

        provider = _TokenExhaustProvider(
            token_cost=30000,
            response=(
                "// TARGET: 0 0x0000\n// FILE: src/mod/f0.cpp\nvoid f0() {}\n"
                "// TARGET: 1 0x0001\n// FILE: src/mod/f1.cpp\nvoid f1() {}\n"
            ),
        )
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        # persist=True — cache file will exist, but BUDGET_EXCEEDED entries
        # must NOT be cached.
        summary = process_modules(cfg, _LLMCfg(), persist=True)

        # Budget was exceeded
        assert summary["budget_exceeded"] > 0

        # Cache file was created (persist=True)
        cache_path = Path(cfg.optimization.cache_path)
        assert cache_path.exists(), "Cache file should exist when persist=True"

        # Read cache and verify it has NO entries for BUDGET_EXCEEDED addresses
        import json as _json

        with open(cache_path, encoding="utf-8") as f:
            cache_data = _json.load(f)

        # All cached entries should be for addresses that were NOT budget_exceeded
        # (i.e., only 0x0000 and 0x0001 from the first subunit)
        budget_exceeded_addrs = {"0x0002", "0x0003", "0x0004", "0x0005"}
        for addr in budget_exceeded_addrs:
            assert str(addr) not in cache_data, f"Cache should NOT contain entry for budget-exceeded address {addr}"

    # ------------------------------------------------------------------
    # Test 5c: persist=True + provider error → cache exists but empty
    # ------------------------------------------------------------------
    def test_5c_persist_true_cache_empty_on_provider_error(self, tmp_path: Path, monkeypatch: Any) -> None:
        """persist=True, provider error → cache file exists but has NO entries
        for PROVIDER_ERROR verdicts."""
        _setup_fs(tmp_path, n_funcs=3)
        _patch_compile(monkeypatch, fail=False)
        cfg = _build_cfg(
            str(tmp_path),
            calls=5,
            tokens=50000,
            compile_retries=0,
            max_retries=0,
        )
        cfg.optimization.cache_enabled = True

        provider = _RaisingProvider()
        _patch_create_provider(monkeypatch, provider)

        from re_agent.build.transform.module_processor import process_modules

        summary = process_modules(cfg, _LLMCfg(), persist=True)

        assert summary["provider_errors"] == 3

        cache_path = Path(cfg.optimization.cache_path)
        # Cache file should NOT exist — no cache.set() was called because
        # all verdicts are PROVIDER_ERROR (excluded from caching).
        assert not cache_path.exists(), (
            "Cache file should NOT exist when all functions are PROVIDER_ERROR (no cache entries were written)"
        )
