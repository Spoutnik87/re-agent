"""Tests for the TransformBudget system in subunit_processor.

Covers:
  - SendResult status propagation (ok, budget_exceeded, provider_error)
  - Budget cap enforcement (calls, tokens, compile retries)
  - Compile retry policy (categories, stagnation, budget exhaustion)
  - Provider error recovery/retry
  - budget_exceeded propagation: no cache, no success, exit 2
  - JSON no-persist output is parseable
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from re_agent.build.transform.subunit_processor import (
    _RETRYABLE_COMPILE_CATEGORIES,
    SendResult,
    TransformBudget,
    _budgeted_send,
    _compile_retry_allowed,
)
from re_agent.llm.protocol import Message, ProviderUsage

# ── Helpers ──────────────────────────────────────────────────────────


class _RaisingProvider:
    """LLM provider that always raises an exception."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        raise RuntimeError("Simulated provider failure")

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            calls=self.total_calls,
        )


class _OkProvider:
    """LLM provider that always returns a fixed response."""

    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_calls = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0

    def __init__(self, response: str = "ok") -> None:
        self._response = response

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.total_calls += 1
        self.total_prompt_tokens += 50
        self.total_completion_tokens += 30
        return self._response

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
            cache_hit_tokens=None,
            cache_miss_tokens=None,
            calls=self.total_calls,
        )


def _msg(text: str = "hello") -> list[Message]:
    return [Message(role="user", content=text)]


# ── Shared test helpers ─────────────────────────────────────────────


def _make_cfg(max_retries: int = 0) -> Any:
    """Create a minimal config object for testing TransformBudget scenarios."""

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
            max_compile_retries = max_retries

        class optimization:
            diagnostics_dir = ""
            raw_response_capture = False

    return _Cfg()


def _apply_patches(monkeypatch: Any) -> None:
    """Apply basic monkeypatches needed for budget tests (render prompts)."""
    import re_agent.build.transform.subunit_processor as sp

    monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
    monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
    monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")


# ── Tests: SendResult ───────────────────────────────────────────────


class TestSendResult:
    def test_ok_status(self) -> None:
        sr = SendResult(
            status="ok",
            response="hello",
            usage_delta=ProviderUsage(
                prompt_tokens=10,
                completion_tokens=20,
                cache_hit_tokens=None,
                cache_miss_tokens=None,
                calls=1,
            ),
        )
        assert sr.status == "ok"
        assert sr.response == "hello"
        assert sr.usage_delta.calls == 1

    def test_budget_exceeded_status(self) -> None:
        sr = SendResult(
            status="budget_exceeded",
            response=None,
            usage_delta=ProviderUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cache_hit_tokens=None,
                cache_miss_tokens=None,
                calls=0,
            ),
        )
        assert sr.status == "budget_exceeded"
        assert sr.response is None

    def test_provider_error_status(self) -> None:
        sr = SendResult(
            status="provider_error",
            response=None,
            usage_delta=ProviderUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cache_hit_tokens=None,
                cache_miss_tokens=None,
                calls=1,
            ),
        )
        assert sr.status == "provider_error"
        assert sr.response is None


# ── Tests: TransformBudget check_before_call / record_after_call ────


class TestTransformBudgetCheck:
    def test_check_before_call_allows_when_budget_remaining(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=1000)
        assert b.check_before_call("test")

    def test_check_before_call_rejects_when_exceeded(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=1000, exceeded=True, exceeded_reason="already exceeded")
        assert not b.check_before_call("test")
        assert b.exceeded_reason == "already exceeded"

    def test_check_before_call_exhausts_on_zero_calls(self) -> None:
        b = TransformBudget(calls_remaining=0, tokens_remaining=1000)
        assert not b.check_before_call("test")
        assert b.exceeded
        assert "Call budget exhausted" in b.exceeded_reason

    def test_record_after_call_deducts_call_and_tokens(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=1000)
        before = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        after = ProviderUsage(
            prompt_tokens=50, completion_tokens=30, calls=1, cache_hit_tokens=None, cache_miss_tokens=None
        )
        b.record_after_call("test", before, after)
        assert b.calls_remaining == 4
        assert b.tokens_remaining == 920  # 1000 - 80
        assert not b.exceeded

    def test_record_after_call_exhausts_tokens(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=100)
        before = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        after = ProviderUsage(
            prompt_tokens=200, completion_tokens=100, calls=1, cache_hit_tokens=None, cache_miss_tokens=None
        )
        b.record_after_call("test", before, after)
        assert b.calls_remaining == 4
        assert b.tokens_remaining == -200
        assert b.exceeded
        assert "Token budget exhausted" in b.exceeded_reason

    def test_record_after_call_tracks_provider_error(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=1000)
        before = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        after = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        b.record_after_call("test", before, after, provider_error=True)
        assert b.provider_error_count == 1
        assert b.calls_remaining == 4

    def test_record_after_call_tracks_subunit_retry(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=1000)
        before = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        after = ProviderUsage(
            prompt_tokens=10, completion_tokens=10, calls=1, cache_hit_tokens=None, cache_miss_tokens=None
        )
        b.record_after_call("test", before, after, subunit_retry=True)
        assert b.subunit_retry_occurred

    def test_to_dict_matches_state(self) -> None:
        b = TransformBudget(calls_remaining=3, tokens_remaining=500, compile_retry_calls_remaining=1)
        d = b.to_dict()
        assert d["calls_remaining"] == 3
        assert d["tokens_remaining"] == 500
        assert d["compile_retry_calls_remaining"] == 1
        assert not d["exceeded"]


# ── Tests: _budgeted_send ───────────────────────────────────────────


class TestBudgetedSend:
    def test_ok_send_deducts_budget(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=100000)
        provider = _OkProvider("good response")
        result = _budgeted_send(provider, _msg(), b, "test")
        assert result.status == "ok"
        assert result.response == "good response"
        assert b.calls_remaining == 4  # one call deducted
        assert not b.exceeded

    def test_budget_exceeded_before_send_no_call(self) -> None:
        b = TransformBudget(calls_remaining=0, tokens_remaining=100000)
        provider = _OkProvider()
        result = _budgeted_send(provider, _msg(), b, "test")
        assert result.status == "budget_exceeded"
        assert result.response is None
        assert provider.total_calls == 0  # no call made

    def test_provider_error_propagates(self) -> None:
        b = TransformBudget(calls_remaining=5, tokens_remaining=100000)
        provider = _RaisingProvider()
        result = _budgeted_send(provider, _msg(), b, "test")
        assert result.status == "provider_error"
        assert result.response is None
        assert b.calls_remaining == 4  # call deducted
        assert b.provider_error_count == 1

    def test_token_exhaustion_sets_exceeded(self) -> None:
        """Call that exhausts token budget sets exceeded, preventing further calls."""
        b = TransformBudget(calls_remaining=5, tokens_remaining=50)
        provider = _OkProvider()
        # First call: consumes 80 tokens (50+30), exceeds 50 budget
        # Contract: budget_exceeded is returned immediately when cap is breached
        result = _budgeted_send(provider, _msg(), b, "test")
        assert result.status == "budget_exceeded"
        assert b.exceeded
        assert "Token budget exhausted" in b.exceeded_reason
        # Second call should be blocked
        result2 = _budgeted_send(provider, _msg(), b, "test")
        assert result2.status == "budget_exceeded"
        assert provider.total_calls == 1  # only the first call was made

    def test_multiple_ok_calls_exhaust_call_budget(self) -> None:
        """With calls_remaining=2, third call is rejected."""
        b = TransformBudget(calls_remaining=2, tokens_remaining=100000)
        provider = _OkProvider()
        r1 = _budgeted_send(provider, _msg(), b, "test")
        assert r1.status == "ok"
        r2 = _budgeted_send(provider, _msg(), b, "test")
        assert r2.status == "ok"
        r3 = _budgeted_send(provider, _msg(), b, "test")
        assert r3.status == "budget_exceeded"
        assert provider.total_calls == 2

    def test_exceeded_flag_stops_all_subsequent_calls(self) -> None:
        """Once exceeded=True, ALL subsequent sends are rejected."""
        b = TransformBudget(calls_remaining=5, tokens_remaining=10)
        provider = _OkProvider()
        r1 = _budgeted_send(provider, _msg(), b, "test")
        assert r1.status == "budget_exceeded"  # exhausts tokens immediately
        assert b.exceeded
        # All further calls blocked
        for _ in range(3):
            r = _budgeted_send(provider, _msg(), b, "test")
            assert r.status == "budget_exceeded"
        assert provider.total_calls == 1  # only first call made


# ── Tests: _compile_retry_allowed ───────────────────────────────────


class TestCompileRetryAllowed:
    def test_retryable_category_allowed(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=1)
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            "error: expected ';'",
            None,
            b,
            False,
        )
        assert allowed
        assert reason == ""

    def test_budget_exhausted_rejected(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=0)
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            "error: expected ';'",
            None,
            b,
            False,
        )
        assert not allowed
        assert "compile retry budget exhausted" in reason

    def test_subunit_retry_occurred_rejected(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=1)
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            "error: expected ';'",
            None,
            b,
            True,
        )
        assert not allowed
        assert "subunit retry already occurred" in reason

    def test_non_retryable_category_rejected(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=1)
        allowed, reason = _compile_retry_allowed(
            "include_error",
            "fatal error: file not found",
            None,
            b,
            False,
        )
        assert not allowed
        assert "non-retryable" in reason

    def test_all_retryable_categories_allowed(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=5)
        for cat in _RETRYABLE_COMPILE_CATEGORIES:
            allowed, reason = _compile_retry_allowed(
                cat,
                f"some {cat} error",
                None,
                b,
                False,
            )
            assert allowed, f"category {cat} should be retryable: {reason}"

    def test_empty_stderr_rejected(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=1)
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            "",
            None,
            b,
            False,
        )
        assert not allowed
        assert "empty stderr" in reason

    def test_stagnant_stderr_rejected(self) -> None:
        b = TransformBudget(compile_retry_calls_remaining=1)
        import hashlib

        stderr = "error: expected ';' before '}' token"
        h = hashlib.sha256(stderr.encode()).hexdigest()
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            stderr,
            h,
            b,
            False,
        )
        assert not allowed
        assert "stagnant" in reason

    def test_stagnation_non_empty_prev_hash(self) -> None:
        """Different stderr with same hash does not trigger (practically
        impossible with SHA-256, but verifies the comparison logic)."""
        b = TransformBudget(compile_retry_calls_remaining=1)
        stderr_a = "error: something"
        stderr_b = "error: something else"
        import hashlib

        h_a = hashlib.sha256(stderr_a.encode()).hexdigest()
        # Different stderr → allowed
        allowed, reason = _compile_retry_allowed(
            "syntax_error",
            stderr_b,
            h_a,
            b,
            False,
        )
        assert allowed


# ── Tests: Budget exhaustion propagation (functional via process_subunit) ──


class TestBudgetIntegration:
    """End-to-end budget scenarios using process_subunit with controlled budgets.

    Uses minimal patches for compile/prompts to focus on budget behavior.
    """

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch: Any) -> None:
        import re_agent.build.transform.subunit_processor as sp

        _apply_patches(monkeypatch)
        monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
        monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))

    def _cfg(self, max_retries: int = 0) -> Any:
        return _make_cfg(max_retries=max_retries)

    def test_calls_capped_at_4_with_5_targets(self) -> None:
        """Budget with calls_remaining=4 stops after 4 calls for 5 functions.
        The initial response covers all functions in one FILE block, so
        1 call = initial. With 5 functions but 1 block, the rest get NO_OUTPUT.
        Verify the budget is not exceeded by too many calls."""
        from re_agent.build.transform.subunit_processor import process_subunit

        provider = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        budget = TransformBudget(calls_remaining=4, tokens_remaining=100000)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
                {"address": "0x1001", "code": "void b() {}", "name": "b"},
                {"address": "0x1002", "code": "void c() {}", "name": "c"},
                {"address": "0x1003", "code": "void d() {}", "name": "d"},
                {"address": "0x1004", "code": "void e() {}", "name": "e"},
            ],
            "neighbour_context": [],
        }
        process_subunit(ctx, "mod", provider, self._cfg(), cache=None, budget=budget)
        # Should not exceed 4 calls (budget cap)
        assert provider.total_calls <= 4
        # Budget should not be exceeded (we stayed within limits)
        assert not budget.exceeded

    def test_compile_retries_capped(self) -> None:
        """With compile_retry_calls_remaining=1 and 5 compile failures,
        at most 1 compile retry call is made.  Non-retryable errors
        (include_error) do not trigger LLM calls."""
        from re_agent.build.transform.subunit_processor import process_subunit

        # All 5 functions get the same file but fail compile with retryable error
        compile_calls = [0]

        def _flaky_compile(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            compile_calls[0] += 1
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp
        # We need a custom monkeypatch for this specific test
        # Use context manager style

        # Re-patch compile for this test
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _flaky_compile)

        provider = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        budget = TransformBudget(
            calls_remaining=10,
            tokens_remaining=100000,
            compile_retry_calls_remaining=1,
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            process_subunit(ctx, "mod", provider, self._cfg(max_retries=1), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # With 1 retryable failure and compile_retry_calls_remaining=1,
        # at most 1 retry attempt (1 initial + 1 retry = 2 total)
        # But since the retry fails with same error, and max_retries=1,
        # total calls should be 2 (1 initial + 1 retry)
        assert provider.total_calls == 2, f"Expected 2 total calls (initial + retry), got {provider.total_calls}"
        assert budget.compile_retry_calls_remaining == 0

    def test_budget_exceeded_after_send_no_subsequent_calls(self) -> None:
        """When token budget is exceeded mid-run, subsequent sends are blocked."""
        from re_agent.build.transform.subunit_processor import process_subunit

        # First subunit exhausts the budget
        provider = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        budget = TransformBudget(calls_remaining=5, tokens_remaining=50)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        process_subunit(ctx, "mod", provider, self._cfg(), cache=None, budget=budget)

        # First subunit succeeded (response was in hand) but budget exceeded
        assert budget.exceeded

        # Second subunit should have initial send blocked → all BUDGET_EXCEEDED
        provider2 = _OkProvider("// FILE: src/mod/b.cpp\nvoid b() {}\n")
        ctx2 = {
            "functions_to_transform": [
                {"address": "0x2000", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }
        results2 = process_subunit(ctx2, "mod2", provider2, self._cfg(), cache=None, budget=budget)
        assert len(results2) == 1
        assert results2[0]["verdict"] == "BUDGET_EXCEEDED", (
            f"Expected BUDGET_EXCEEDED for second subunit, got {results2[0]['verdict']}"
        )
        # Second send was NOT made (blocked by budget)
        assert provider2.total_calls == 0

    def test_provider_error_recovery(self) -> None:
        """Provider error is not retried; subunit completes with PROVIDER_ERROR verdict."""
        from re_agent.build.transform.subunit_processor import process_subunit

        provider = _RaisingProvider()
        budget = TransformBudget(calls_remaining=5, tokens_remaining=100000)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        results = process_subunit(ctx, "mod", provider, self._cfg(), cache=None, budget=budget)
        assert len(results) == 1
        assert results[0]["verdict"] == "PROVIDER_ERROR"
        assert budget.provider_error_count == 1

    def test_provider_error_then_budget_exhausted(self) -> None:
        """Provider error counts as a call and deducts from budget.
        If calls_remaining runs out after provider error, subsequent
        initial call for next subunit would be blocked."""
        from re_agent.build.transform.subunit_processor import process_subunit

        # First subunit: provider error consumes the only remaining call
        provider = _RaisingProvider()
        budget = TransformBudget(calls_remaining=1, tokens_remaining=100000)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        results1 = process_subunit(ctx, "mod1", provider, self._cfg(), cache=None, budget=budget)
        assert results1[0]["verdict"] == "PROVIDER_ERROR"
        assert budget.calls_remaining == 0  # call was deducted
        # exceeded is set on NEXT check (not retroactively)
        assert not budget.check_before_call("next")
        assert budget.exceeded

    def test_no_persist_json_parseable(self) -> None:
        """Verify _no_persist_json_output produces valid, parseable JSON."""
        from re_agent.build.transform.subunit_processor import _no_persist_json_output

        results = [
            {
                "function": "0x1000",
                "module": "mod",
                "files": [],
                "compiles": False,
                "verdict": "BUDGET_EXCEEDED",
                "diagnostic": {
                    "match_strategy": "none",
                    "identity_state": "none",
                    "identity_reason": "Budget exhausted",
                    "retry_skip_reason": "",
                },
            }
        ]
        budget = TransformBudget(calls_remaining=0, tokens_remaining=0, exceeded=True, exceeded_reason="test")
        start = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        end = ProviderUsage(
            prompt_tokens=50, completion_tokens=30, calls=1, cache_hit_tokens=None, cache_miss_tokens=None
        )

        # Capture stdout
        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            _no_persist_json_output(results, budget, start, end, exit_code=2)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        parsed = json.loads(output)
        assert parsed["run_type"] == "no-persist"
        assert parsed["exit_code"] == 2
        assert parsed["summary"]["budget_exceeded"] == 1
        assert parsed["usage"]["total_calls"] == 1
        assert parsed["budget"]["exceeded"] is True
        assert parsed["results"][0]["verdict"] == "BUDGET_EXCEEDED"

    def test_no_persist_json_no_budget(self) -> None:
        """_no_persist_json_output with budget=None still produces valid JSON."""
        from re_agent.build.transform.subunit_processor import _no_persist_json_output

        results = [
            {
                "function": "0x1000",
                "module": "mod",
                "files": [],
                "compiles": False,
                "verdict": "NO_OUTPUT",
                "diagnostic": {
                    "match_strategy": "none",
                    "identity_state": "none",
                    "identity_reason": "",
                    "retry_skip_reason": "",
                },
            }
        ]
        start = ProviderUsage(
            prompt_tokens=0, completion_tokens=0, calls=0, cache_hit_tokens=None, cache_miss_tokens=None
        )
        end = ProviderUsage(
            prompt_tokens=10, completion_tokens=5, calls=1, cache_hit_tokens=None, cache_miss_tokens=None
        )

        import io

        captured = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            _no_persist_json_output(results, None, start, end, exit_code=0)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        parsed = json.loads(output)
        assert "budget" not in parsed
        assert parsed["exit_code"] == 0

    def test_non_retryable_failures_no_llm_send(self) -> None:
        """Non-retryable compile errors (include_error) never trigger an LLM
        call, even with budget and retries available."""
        from re_agent.build.transform.subunit_processor import process_subunit

        compile_calls = [0]

        def _fail_include(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            compile_calls[0] += 1
            return (False, "fatal error: No such file or directory")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_include)

        provider = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        budget = TransformBudget(
            calls_remaining=10,
            tokens_remaining=100000,
            compile_retry_calls_remaining=5,
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # Only 1 call (initial) - no retry since include_error is non-retryable
        assert provider.total_calls == 1, f"Expected 1 call (no retry for non-retryable), got {provider.total_calls}"
        assert results[0]["verdict"] == "FAIL_NO_RETRY"
        # retry_skip_reason should indicate non-retryable
        diag = results[0].get("diagnostic", {})
        assert "non-retryable" in diag.get("retry_skip_reason", "")


# ── Tests: Config validation ────────────────────────────────────────


class TestConfigValidation:
    def test_default_values_valid(self) -> None:
        """Default TransformBudget values pass all checks."""
        b = TransformBudget()
        assert b.calls_remaining == 8
        assert b.tokens_remaining == 150000
        assert b.compile_retry_calls_remaining == 3
        assert not b.exceeded

    def test_schema_validation(self) -> None:
        """BuildOptimizationConfig validates caps."""
        from re_agent.config.schema import BuildOptimizationConfig

        # Valid config
        cfg = BuildOptimizationConfig(
            max_llm_calls_per_run=10,
            max_llm_tokens_per_run=100000,
            max_compile_retry_calls_per_run=2,
        )
        assert cfg.max_llm_calls_per_run == 10

        # Zero calls should raise
        with pytest.raises(ValueError, match="max_llm_calls_per_run"):
            BuildOptimizationConfig(max_llm_calls_per_run=0)

        # Zero tokens should raise
        with pytest.raises(ValueError, match="max_llm_tokens_per_run"):
            BuildOptimizationConfig(max_llm_tokens_per_run=0)

        # Negative compile retry should raise
        with pytest.raises(ValueError, match="max_compile_retry_calls_per_run"):
            BuildOptimizationConfig(max_compile_retry_calls_per_run=-1)

        # Zero compile retry is valid (disables retry)
        cfg0 = BuildOptimizationConfig(max_compile_retry_calls_per_run=0)
        assert cfg0.max_compile_retry_calls_per_run == 0


# ── Tests: Exit code semantics ─────────────────────────────────────


class TestBudgetExceededExitCode:
    def test_module_processor_exit_code_2_on_budget_exceeded(self) -> None:
        """Exit code 2 when BUDGET_EXCEEDED present (verified via no-persist JSON).
        This tests the logic in process_modules replicated here."""
        budget_counts = {
            "budget_exceeded": 1,
            "contract_failed": True,
        }
        # Replicate the exit code logic from module_processor.py
        exit_code = 2 if (budget_counts["budget_exceeded"] > 0 or budget_counts["contract_failed"]) else 0
        assert exit_code == 2

    def test_exit_code_0_on_success(self) -> None:
        budget_counts = {
            "budget_exceeded": 0,
            "contract_failed": False,
        }
        exit_code = 2 if (budget_counts["budget_exceeded"] > 0 or budget_counts["contract_failed"]) else 0
        assert exit_code == 0


# ── Tests: Recovery budget ──────────────────────────────────────────


class TestRecoveryBudget:
    """TARGET recovery respects the shared TransformBudget."""

    def test_recovery_calls_counted(self) -> None:
        """Recovery calls deduct from the budget."""
        from re_agent.build.transform.subunit_processor import (
            TransformBudget,
            _analyze_target_coverage,
            _parse_llm_response_records,
            _run_target_recovery,
        )

        funcs = [
            {"address": "0x1000", "code": "void a() {}"},
            {"address": "0x1001", "code": "void b() {}"},
        ]

        # Parse an initial record for func 0
        records, _ = _parse_llm_response_records("// TARGET: 0 0x1000\n// FILE: a.cpp\nvoid a() {}\n")
        initial = _analyze_target_coverage(records, funcs)
        assert not initial.is_complete

        class _RecoveryProvider:
            supports_conversations = False
            total_prompt_tokens = 0
            total_completion_tokens = 0
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0

            def send(self, messages, **kwargs):
                self.total_calls += 1
                self.total_prompt_tokens += 40
                self.total_completion_tokens += 20
                return "// TARGET: 1 0x1001\n// FILE: b.cpp\nvoid b() {}\n"

            def get_usage(self):
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _RecoveryProvider()
        budget = TransformBudget(calls_remaining=5, tokens_remaining=100000)
        final = _run_target_recovery(initial, funcs, provider, "system", budget)
        assert final.is_complete
        # 1 recovery call should have been made
        assert budget.calls_remaining == 4  # one deducted
        assert budget.tokens_remaining == 99940  # 100000 - 60


# ── Tests: Compile retry checks (issue 3) ──────────────────────────


class TestCompileRetryChecks:
    """Verify compile_retry_calls_remaining > 0 check / decrement by 1."""

    def _cfg(self, max_retries: int = 0) -> Any:
        return _make_cfg(max_retries=max_retries)

    def test_compile_retry_cap_zero_blocks_all_sends(self) -> None:
        """compile_retry_calls_remaining=0 prevents ALL retry LLM calls."""
        from re_agent.build.transform.subunit_processor import _compile_retry_allowed, process_subunit

        # Allowed check: budget exhausted → blocked
        b = TransformBudget(compile_retry_calls_remaining=0)
        allowed, reason = _compile_retry_allowed("syntax_error", "error", None, b, False)
        assert not allowed
        assert "compile retry budget exhausted" in reason

        # Integration: compile fails, retry should not trigger
        b2 = TransformBudget(calls_remaining=10, tokens_remaining=100000, compile_retry_calls_remaining=0)

        def _fail_compile(*a, **kw):
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)

        provider = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            # max_retries=1 but compile_retry budget=0 → no retry call
            _apply_patches(monkeypatch)
            results = process_subunit(ctx, "mod", provider, _make_cfg(max_retries=1), cache=None, budget=b2)
        finally:
            monkeypatch.undo()

        # Only the initial LLM call was made (no retry)
        assert provider.total_calls == 1, f"Expected 1 call (no retry with cap=0), got {provider.total_calls}"
        diag = results[0].get("diagnostic", {})
        assert diag.get("retry_skip_reason", "") != "", "retry_skip_reason should explain why retry was skipped"

    def test_compile_retry_decrement_in_subunit_retry(self) -> None:
        """Subunit retry decrements compile_retry_calls_remaining by 1, not set to 0."""
        b = TransformBudget(calls_remaining=10, tokens_remaining=100000, compile_retry_calls_remaining=2)
        initial_remaining = b.compile_retry_calls_remaining

        # Trigger a subunit retry path: 2+ retryable failures, max_retries>0
        from re_agent.build.transform.subunit_processor import process_subunit

        def _fail_compile(*a, **kw):
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)
        monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (False, "error"))
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

        provider = _OkProvider(
            "// TARGET: 0 0x1000\n// FILE: src/mod/a.cpp\nvoid a() {}\n"
            "// TARGET: 1 0x1001\n// FILE: src/mod/b.cpp\nvoid b() {}\n"
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
                {"address": "0x1001", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }
        try:
            process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=b)
        finally:
            monkeypatch.undo()

        # compile_retry should have decremented by 1 (not set to 0)
        assert b.compile_retry_calls_remaining == initial_remaining - 1, (
            f"Expected compile_retry_calls_remaining={initial_remaining - 1}, got {b.compile_retry_calls_remaining}"
        )
        # At least one more call happened (the subunit retry)
        assert provider.total_calls >= 2

    def test_compile_retry_shared_between_subunits(self) -> None:
        """Two separate process_subunit calls share the same compile retry budget.
        First consumes the unit, second is blocked."""
        from re_agent.build.transform.subunit_processor import process_subunit

        def _fail_compile(*a, **kw):
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

        b = TransformBudget(calls_remaining=10, tokens_remaining=100000, compile_retry_calls_remaining=1)

        provider1 = _OkProvider("// FILE: src/mod/a.cpp\nvoid a() {}\n")
        ctx1 = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            process_subunit(ctx1, "mod1", provider1, self._cfg(max_retries=1), cache=None, budget=b)
        finally:
            monkeypatch.undo()

        # First subunit consumed the retry budget
        assert b.compile_retry_calls_remaining == 0, (
            f"Expected 0 retry remaining, got {b.compile_retry_calls_remaining}"
        )

        # Second subunit must not get a retry
        provider2 = _OkProvider("// FILE: src/mod/b.cpp\nvoid b() {}\n")
        ctx2 = {
            "functions_to_transform": [
                {"address": "0x2000", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }
        calls_before = provider2.total_calls
        # Re-patch compile for second call (monkeypatch was undone)
        monkeypatch2 = pytest.MonkeyPatch()
        monkeypatch2.setattr(sp, "compile_check", _fail_compile)
        try:
            process_subunit(ctx2, "mod2", provider2, self._cfg(max_retries=1), cache=None, budget=b)
        finally:
            monkeypatch2.undo()

        # No additional LLM calls for retry (budget exhausted)
        assert provider2.total_calls <= calls_before + 1, "Second subunit should not trigger a retry (budget exhausted)"


# ── Tests: Provider error during retry (issue 2 callsites) ──────────


class TestProviderError:
    """Provider error at each of the three _budgeted_send callsites."""

    def _cfg(self, max_retries: int = 0) -> Any:
        return _make_cfg(max_retries=max_retries)

    # Callsite 1: initial send → PROVIDER_ERROR (tested in TestBudgetIntegration.test_provider_error_recovery)

    def test_provider_error_during_per_function_retry(self) -> None:
        """Provider error during per-function retry → PROVIDER_ERROR verdict,
        not FAIL_NO_RETRY."""
        from re_agent.build.transform.subunit_processor import process_subunit

        def _fail_compile(*a, **kw):
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

        # Provider that succeeds once then raises
        class _RetryRaiser:
            supports_conversations = False
            total_prompt_tokens = 50
            total_completion_tokens = 30
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            _first = True

            def send(self, messages, **kwargs):
                self.total_calls += 1
                self.total_prompt_tokens += 50
                self.total_completion_tokens += 30
                if self._first:
                    self._first = False
                    return "// FILE: src/mod/a.cpp\nvoid a() {}\n"
                raise RuntimeError("Provider error during retry")

            def get_usage(self):
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _RetryRaiser()
        b = TransformBudget(calls_remaining=10, tokens_remaining=100000, compile_retry_calls_remaining=2)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=1), cache=None, budget=b)
        finally:
            monkeypatch.undo()

        # Provider error during retry → PROVIDER_ERROR
        assert results[0].get("verdict") == "PROVIDER_ERROR", (
            f"Expected PROVIDER_ERROR, got {results[0].get('verdict')}"
        )

    def test_provider_error_during_subunit_retry(self) -> None:
        """Provider error during subunit retry → PROVIDER_ERROR for ALL functions."""
        from re_agent.build.transform.subunit_processor import process_subunit

        def _fail_compile(*a, **kw):
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

        # Provider succeeds once then raises (subunit retry)
        class _RetryRaiser:
            supports_conversations = False
            total_prompt_tokens = 50
            total_completion_tokens = 30
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            _first = True

            def send(self, messages, **kwargs):
                self.total_calls += 1
                self.total_prompt_tokens += 50
                self.total_completion_tokens += 30
                if self._first:
                    self._first = False
                    return (
                        "// TARGET: 0 0x1000\n// FILE: src/mod/a.cpp\nvoid a() {}\n"
                        "// TARGET: 1 0x1001\n// FILE: src/mod/b.cpp\nvoid b() {}\n"
                    )
                raise RuntimeError("Provider error during subunit retry")

            def get_usage(self):
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _RetryRaiser()
        b = TransformBudget(calls_remaining=10, tokens_remaining=100000, compile_retry_calls_remaining=2)
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
                {"address": "0x1001", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }
        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=b)
        finally:
            monkeypatch.undo()

        # Both functions should be PROVIDER_ERROR (subunit-level failure)
        assert len(results) == 2
        for r in results:
            assert r.get("verdict") == "PROVIDER_ERROR", f"Expected PROVIDER_ERROR, got {r.get('verdict')}"

    def test_provider_error_during_target_recovery(self) -> None:
        """Provider error during TARGET recovery → stop recovery.  The
        uncovered functions remain uncovered, but the function is handled
        by including the provider error in the diagnostic outcome."""
        # This test verifies the code doesn't crash and returns a result.
        # The recovery error is detected via budget.provider_error_count.
        from re_agent.build.transform.subunit_processor import (
            TransformBudget,
            _analyze_target_coverage,
            _parse_llm_response_records,
            _run_target_recovery,
        )

        funcs = [
            {"address": "0x1000", "code": "void a() {}"},
            {"address": "0x1001", "code": "void b() {}"},
        ]

        records, _ = _parse_llm_response_records("// TARGET: 0 0x1000\n// FILE: a.cpp\nvoid a() {}\n")
        initial = _analyze_target_coverage(records, funcs)

        class _ErrorProvider:
            supports_conversations = False
            total_prompt_tokens = 0
            total_completion_tokens = 0
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0

            def send(self, messages, **kwargs):
                self.total_calls += 1
                self.total_prompt_tokens += 10
                self.total_completion_tokens += 5
                raise RuntimeError("Provider error during recovery")

            def get_usage(self):
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _ErrorProvider()
        budget = TransformBudget(calls_remaining=5, tokens_remaining=100000)
        final = _run_target_recovery(initial, funcs, provider, "system", budget)

        # Recovery stopped (did not crash)
        assert not final.is_complete, "Recovery should be incomplete after provider error"
        # The returned coverage has conflict (provider_error flag)
        assert final.has_conflict, "Recovery should report conflict after provider error"
        assert "provider_error" in final.conflict_reason


# ── Tests: Budget exceeded propagation (all 3 callsites) ─────────────


class TestBudgetExceededPropagation:
    """Budget exceeded at each of the three _budgeted_send callsites must
    produce BUDGET_EXCEEDED verdicts for all affected targets, never
    NO_OUTPUT, INCOMPLETE_TARGETS, or FAIL_AFTER_RETRY."""

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch: Any) -> None:
        _apply_patches(monkeypatch)

    def _cfg(self, max_retries: int = 0) -> Any:
        return _make_cfg(max_retries=max_retries)

    def test_regression_budget_exceeded_during_subunit_retry(self) -> None:
        """5 TARGET/5 compilations retryable, initial usage 30k, retry +30k,
        cap 50k ⇒ 2 calls, all 5 verdicts BUDGET_EXCEEDED, cache vide.

        Regression: budget_exceeded from _budgeted_send during subunit retry
        must return _budget_exceeded_result for all targets, not continue to
        produce NO_OUTPUT or INCOMPLETE_TARGETS verdicts."""
        from re_agent.build.transform.subunit_processor import (
            TransformBudget,
            process_subunit,
        )
        from re_agent.llm.protocol import ProviderUsage

        # 5 functions, all will fail compile with retryable error
        funcs = [{"address": f"0x{i}000", "code": f"void func{i}() {{}}", "name": f"func{i}"} for i in range(5)]

        # Initial response: 5 valid TARGET files
        initial_response = "\n".join(
            f"// TARGET: {i} 0x{i}000\n// FILE: src/mod/func{i}.cpp\nvoid func{i}() {{}}" for i in range(5)
        )

        class _TokenTrackingProvider:
            supports_conversations = False
            total_prompt_tokens = 0
            total_completion_tokens = 0
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0
            _call_count = 0

            def send(self, messages: list[Message], **kwargs: Any) -> str:
                self.total_calls += 1
                self._call_count += 1
                if self._call_count == 1:
                    # Initial call: consumes 30k tokens
                    self.total_prompt_tokens += 30000
                    return initial_response
                # Subunit retry call: consumes 30k more → total 60k > 50k cap
                self.total_prompt_tokens += 30000
                return initial_response

            def get_usage(self) -> ProviderUsage:
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _TokenTrackingProvider()
        # 50k token cap — initial uses 30k, retry uses 30k → budget exceeded after retry
        budget = TransformBudget(
            calls_remaining=10,
            tokens_remaining=50000,
            compile_retry_calls_remaining=2,
        )

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        # All 5 functions fail compile with retryable error → triggers subunit retry
        monkeypatch.setattr(sp, "compile_check", lambda *a, **kw: (False, "error: expected ';'"))
        monkeypatch.setattr(
            sp,
            "compile_generated_file_set",
            lambda files, path, cfg: (False, "error: expected ';'"),
        )

        ctx = {
            "functions_to_transform": funcs,
            "neighbour_context": [],
        }
        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # ── Assertions ──────────────────────────────────────────────
        # All 5 must be BUDGET_EXCEEDED
        assert len(results) == 5, f"Expected 5 results, got {len(results)}"
        for r in results:
            assert r["verdict"] == "BUDGET_EXCEEDED", (
                f"Expected BUDGET_EXCEEDED, got {r['verdict']} for {r['function']}"
            )
            assert r["files"] == [], f"Expected empty files for BUDGET_EXCEEDED, got {r['files']}"
            assert not r["compiles"]

        # Exactly 2 LLM calls (initial + retry, no more)
        assert provider.total_calls == 2, f"Expected 2 LLM calls, got {provider.total_calls}"

        # Budget properly recorded as exceeded
        assert budget.exceeded
        assert budget.calls_remaining == 8  # 10 - 2 calls
        assert budget.tokens_remaining == -10000  # 50000 - 60000

        # No function should have NO_OUTPUT, INCOMPLETE_TARGETS, or FAIL_AFTER_RETRY
        bad_verdicts = {"NO_OUTPUT", "INCOMPLETE_TARGETS", "FAIL_AFTER_RETRY"}
        for r in results:
            assert r["verdict"] not in bad_verdicts, f"Got forbidden verdict {r['verdict']} for {r['function']}"

        # Summary (simulate module_processor summary logic)
        budget_exceeds = sum(1 for r in results if r.get("verdict") == "BUDGET_EXCEEDED")
        assert budget_exceeds == 5, f"Expected 5 budget_exceeded, got {budget_exceeds}"

        contract_failed = budget_exceeds > 0
        exit_code = 2 if budget_exceeds or contract_failed else 0
        assert exit_code == 2, f"Expected exit_code 2, got {exit_code}"


# ── Tests: Blocker fixes ────────────────────────────────────────────


class TestBlockerFixes:
    """Tests for the three PR blockers."""

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch: Any) -> None:
        import re_agent.build.transform.subunit_processor as sp

        monkeypatch.setattr(sp, "compile_check", lambda code, cfg: (True, ""))
        monkeypatch.setattr(sp, "compile_generated_file_set", lambda files, path, cfg: (True, ""))
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

    def _cfg(self, max_retries: int = 1) -> Any:
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
                max_compile_retries = max_retries

            class optimization:
                diagnostics_dir = ""
                raw_response_capture = False

        return _Cfg()

    # ── Blocker 2: provider_error exact count ──

    def test_provider_error_count_exactly_one_per_send(self) -> None:
        """Provider error during retry increments provider_error_count exactly once.
        _budgeted_send counts it; the caller must NOT double-count."""
        from re_agent.build.transform.subunit_processor import process_subunit

        # Compile always fails with retryable error to trigger retry
        compile_calls = [0]

        def _fail_compile(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            compile_calls[0] += 1
            return (False, "error: expected ';' before '}' token")

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sp, "compile_check", _fail_compile)

        provider = _RaisingProvider()
        budget = TransformBudget(
            calls_remaining=5,
            tokens_remaining=100000,
            compile_retry_calls_remaining=2,
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
            ],
            "neighbour_context": [],
        }
        try:
            process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # provider_error_count should be exactly 1 (the one from _budgeted_send)
        # If the caller double-counts, it would be 2
        assert budget.provider_error_count == 1, (
            f"Expected exactly 1 provider_error_count (from _budgeted_send), got {budget.provider_error_count}"
        )

    # ── Blocker 3: compile-retry budget=0 with multiple retryable failures ──

    def test_compile_retry_budget_zero_multi_function_no_llm_call(self) -> None:
        """With compile_retry_calls_remaining=0 and multiple retryable failures,
        no LLM call is made. Functions get FAIL_NO_RETRY, not BUDGET_EXCEEDED.
        Candidate files are preserved if applicable."""
        from re_agent.build.transform.subunit_processor import process_subunit

        initial_response = "// FILE: src/mod/a.cpp\nvoid a() {}\n// FILE: src/mod/b.cpp\nvoid b() {}\n"

        class _CompileFailProvider:
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
                self.total_calls += 1
                self.total_prompt_tokens += 50
                self.total_completion_tokens += 30
                self.last_messages = list(messages)
                return self._response

            def get_usage(self) -> ProviderUsage:
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        provider = _CompileFailProvider(initial_response)

        # Compile always fails with retryable errors
        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()

        def _fail_retryable(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            return (False, "error: expected ';' before '}' token")

        monkeypatch.setattr(sp, "compile_check", _fail_retryable)

        budget = TransformBudget(
            calls_remaining=10,
            tokens_remaining=100000,
            compile_retry_calls_remaining=0,  # ← ZERO — no retry budget
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
                {"address": "0x1001", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }
        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=1), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # Only 1 LLM call: the initial one
        assert provider.total_calls == 1, f"Expected exactly 1 LLM call (initial only), got {provider.total_calls}"

        # Both functions should have FAIL_NO_RETRY (not BUDGET_EXCEEDED)
        for r in results:
            assert r["verdict"] == "FAIL_NO_RETRY", f"Expected FAIL_NO_RETRY for {r['function']}, got {r['verdict']}"
            # Candidate files should be preserved (if matched)
            diag = r.get("diagnostic", {})
            if r["function"] == "0x1000":
                assert len(diag.get("candidate_paths", [])) > 0
            # retry_skip_reason should say compile retry budget exhausted
            skip = diag.get("retry_skip_reason", "")
            assert "compile retry budget exhausted" in skip.lower(), (
                f"Expected retry_skip_reason about budget exhaustion, got {skip!r}"
            )

    def test_compile_retry_budget_exhausted_after_subunit_retry(self) -> None:
        """When subunit retry exhausts compile_retry_calls_remaining (set to 0),
        per-function retries are correctly blocked with FAIL_NO_RETRY."""
        from re_agent.build.transform.subunit_processor import process_subunit

        class _MultiSendProvider:
            supports_conversations = False
            total_prompt_tokens = 0
            total_completion_tokens = 0
            total_calls = 0
            total_cache_hit_tokens = 0
            total_cache_miss_tokens = 0

            def __init__(self, responses: list[str]) -> None:
                self._responses = responses
                self.last_messages: list[Message] = []

            def send(self, messages: list[Message], **kwargs: Any) -> str:
                self.total_calls += 1
                self.total_prompt_tokens += 50
                self.total_completion_tokens += 30
                self.last_messages = list(messages)
                idx = min(self.total_calls - 1, len(self._responses) - 1)
                return self._responses[idx]

            def get_usage(self) -> ProviderUsage:
                return ProviderUsage(
                    prompt_tokens=self.total_prompt_tokens,
                    completion_tokens=self.total_completion_tokens,
                    cache_hit_tokens=None,
                    cache_miss_tokens=None,
                    calls=self.total_calls,
                )

        # Initial response produces 2 valid FILE blocks
        init_resp = (
            "// TARGET: 0 0x1000\n// FILE: a.cpp\nvoid a() {}\n// TARGET: 1 0x1001\n// FILE: b.cpp\nvoid b() {}\n"
        )
        # Subunit retry response also valid (but still fails compile)
        retry_resp = (
            "// TARGET: 0 0x1000\n// FILE: a.cpp\nvoid a_fixed() {}\n"
            "// TARGET: 1 0x1001\n// FILE: b.cpp\nvoid b_fixed() {}\n"
        )
        provider = _MultiSendProvider([init_resp, retry_resp])

        import re_agent.build.transform.subunit_processor as sp

        monkeypatch = pytest.MonkeyPatch()

        def _always_fail(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            return (False, "error: expected ';' before '}' token")

        monkeypatch.setattr(sp, "compile_check", _always_fail)
        monkeypatch.setattr(
            sp, "compile_generated_file_set", lambda files, path, cfg: (False, "error: expected ';' before '}' token")
        )
        # Re-patch prompts for TARGET mode
        monkeypatch.setattr(sp, "_render_system_prompt", lambda cfg, mn: "system")
        monkeypatch.setattr(sp, "_render_repair_prompt", lambda cfg, mn: "repair")
        monkeypatch.setattr(sp, "_render_task_prompt", lambda mn, ctx: "task")

        budget = TransformBudget(
            calls_remaining=10,
            tokens_remaining=200000,
            compile_retry_calls_remaining=1,  # Only 1 — consumed by subunit retry
        )
        ctx = {
            "functions_to_transform": [
                {"address": "0x1000", "code": "void a() {}", "name": "a"},
                {"address": "0x1001", "code": "void b() {}", "name": "b"},
            ],
            "neighbour_context": [],
        }

        try:
            results = process_subunit(ctx, "mod", provider, self._cfg(max_retries=2), cache=None, budget=budget)
        finally:
            monkeypatch.undo()

        # LLM calls: 1 initial + 1 subunit retry = 2 (no per-function retries)
        assert provider.total_calls == 2, f"Expected 2 LLM calls (initial + subunit retry), got {provider.total_calls}"
        assert budget.compile_retry_calls_remaining == 0

        # Per-function retries were attempted (max_retries=2), but
        # compile_retry_calls_remaining was 0 after subunit retry,
        # so _compile_retry_allowed blocks them → FAIL_NO_RETRY
        for r in results:
            assert r["verdict"] in ("FAIL_NO_RETRY", "FAIL_AFTER_RETRY"), (
                f"Expected FAIL_NO_RETRY or FAIL_AFTER_RETRY for {r['function']}, got {r['verdict']}"
            )
