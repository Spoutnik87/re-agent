"""Tests for the agent fix loop."""

from __future__ import annotations

from re_agent.agents.loop import run_fix_loop
from re_agent.backend.stub import StubBackend
from re_agent.core.models import AsmResult, DecompileResult, FunctionTarget, Verdict
from re_agent.llm.protocol import Message


class MockLLM:
    """Mock LLM that returns canned responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._idx = 0
        self.send_calls: list[list[Message]] = []
        self.resume_calls: list[tuple[str, str]] = []

    def send(self, messages: list[Message], **kwargs: object) -> str:
        self.send_calls.append(list(messages))
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        return self._responses[idx]

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        return "mock-conv"

    def resume(self, conversation_id: str, message: str) -> str:
        self.resume_calls.append((conversation_id, message))
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        return self._responses[idx]

    def delete_conversation(self, conversation_id: str) -> None:
        pass


class NonConvMockLLM(MockLLM):
    """Mock LLM that explicitly does not support conversations."""

    @property
    def supports_conversations(self) -> bool:
        return False

    def new_conversation(self, system: str) -> str:
        return ""

    def resume(self, conversation_id: str, message: str) -> str:
        return ""


def test_loop_pass_first_round(tmp_path: object) -> None:
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")
    backend = StubBackend()

    reverser_resp = (
        "```cpp\nvoid CTrain::ProcessControl() { }\n```\nREVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)"
    )
    checker_resp = "VERDICT: PASS\nSUMMARY: All good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none"

    rev_llm = NonConvMockLLM([reverser_resp])
    chk_llm = NonConvMockLLM([checker_resp])
    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=3)

    assert result.success
    assert result.rounds_used == 1
    assert result.checker_verdict is not None
    assert result.checker_verdict.verdict == Verdict.PASS
    assert result.objective_verdict is not None
    assert result.objective_verdict.verdict == Verdict.PASS


def test_loop_fail_then_pass(tmp_path: object) -> None:
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")
    backend = StubBackend()

    reverser_responses = [
        "```cpp\nvoid CTrain::ProcessControl() { /* wrong */ }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
        "```cpp\nvoid CTrain::ProcessControl() { /* fixed */ }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
    ]
    checker_responses = [
        "VERDICT: FAIL\nSUMMARY: Missing branch\nISSUES:\n- missing if check\nFIX_INSTRUCTIONS:\n- add the if check",
        "VERDICT: PASS\nSUMMARY: All good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
    ]

    rev_llm = NonConvMockLLM(reverser_responses)
    chk_llm = NonConvMockLLM(checker_responses)
    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=3)

    assert result.success
    assert result.rounds_used == 2


def test_loop_exhausts_rounds() -> None:
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")
    backend = StubBackend()

    reverser_resp = "```cpp\nvoid CTrain::ProcessControl() { }\n```"
    checker_resp = "VERDICT: FAIL\nSUMMARY: Still wrong\nISSUES:\n- issue\nFIX_INSTRUCTIONS:\n- fix it"

    rev_llm = NonConvMockLLM([reverser_resp] * 5)
    chk_llm = NonConvMockLLM([checker_resp] * 5)
    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=2)

    assert not result.success
    assert result.rounds_used == 2


class StructuralBackend(StubBackend):
    def decompile(self, target: str) -> DecompileResult:
        raw = """\
void CTrain::ProcessControl() {
    if (m_nState) {
        FuncA();
        FuncB();
        FuncC();
    }
}
// Callers: 1 | Callees: 3
"""
        return DecompileResult(
            address=target,
            name="CTrain::ProcessControl",
            signature="void CTrain::ProcessControl()",
            decompiled=raw,
            raw_output=raw,
            callers=1,
            callees=3,
        )

    def get_asm(self, target: str) -> AsmResult | None:
        instructions = "\n".join(
            [
                "00400000 CALL FuncA",
                "00400004 CALL FuncB",
                "00400008 CALL FuncC",
            ]
        )
        return AsmResult(
            address=target,
            instructions=instructions,
            instruction_count=3,
            call_count=3,
            has_fp_sensitive=False,
        )


def test_loop_objective_verifier_blocks_false_pass() -> None:
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")
    backend = StructuralBackend()

    reverser_responses = [
        "```cpp\nvoid CTrain::ProcessControl() { }\n```\nREVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
        "```cpp\nvoid CTrain::ProcessControl() { if (m_nState) { FuncA(); FuncB(); FuncC(); } }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
    ]
    checker_responses = [
        "VERDICT: PASS\nSUMMARY: Looks good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
        "VERDICT: PASS\nSUMMARY: Looks good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
    ]

    rev_llm = NonConvMockLLM(reverser_responses)
    chk_llm = NonConvMockLLM(checker_responses)
    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=2)

    assert result.success
    assert result.rounds_used == 2
    assert result.objective_verdict is not None
    assert result.objective_verdict.verdict == Verdict.PASS


# ---------------------------------------------------------------------------
# Optimize mode tests
# ---------------------------------------------------------------------------


class DecompileTrackingBackend(StubBackend):
    """A stub backend that tracks how many times decompile() is called."""

    def __init__(self) -> None:
        super().__init__()
        self.decompile_call_count = 0

    def decompile(self, target: str) -> DecompileResult:
        self.decompile_call_count += 1
        return DecompileResult(
            address=target,
            name="CStub::StubFunction",
            signature="void __fastcall CStub::StubFunction(CStub *this)",
            decompiled="void CStub::StubFunction() { do_work(); }",
            raw_output="void CStub::StubFunction() { do_work(); }",
            callers=2,
            callees=1,
        )

    def get_asm(self, target: str) -> AsmResult | None:
        return AsmResult(
            address=target,
            instructions="00400000 CALL do_work",
            instruction_count=1,
            call_count=1,
            has_fp_sensitive=False,
        )


class DecompileTrackingStructuralBackend(StubBackend):
    """Tracks decompile() calls while providing rich structural data for objective verifier."""

    def __init__(self) -> None:
        super().__init__()
        self.decompile_call_count = 0

    def decompile(self, target: str) -> DecompileResult:
        self.decompile_call_count += 1
        raw = """\
void CTrain::ProcessControl() {
    if (m_nState) {
        FuncA();
        FuncB();
        FuncC();
    }
}
// Callers: 1 | Callees: 3
"""
        return DecompileResult(
            address=target,
            name="CTrain::ProcessControl",
            signature="void CTrain::ProcessControl()",
            decompiled=raw,
            raw_output=raw,
            callers=1,
            callees=3,
        )

    def get_asm(self, target: str) -> AsmResult | None:
        instructions = "\n".join(
            [
                "00400000 CALL FuncA",
                "00400004 CALL FuncB",
                "00400008 CALL FuncC",
            ]
        )
        return AsmResult(
            address=target,
            instructions=instructions,
            instruction_count=3,
            call_count=3,
            has_fp_sensitive=False,
        )


def test_optimize_caches_decompile_and_avoids_double_call() -> None:
    """In optimize mode, backend.decompile() is only called once by reverser (checker uses cache).

    Note: objective_verifier also calls decompile() independently, so we disable it.
    """
    backend = DecompileTrackingBackend()
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")

    reverser_resp = (
        "```cpp\nvoid CTrain::ProcessControl() { do_work(); }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)"
    )
    checker_resp = "VERDICT: PASS\nSUMMARY: All good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none"

    rev_llm = NonConvMockLLM([reverser_resp])
    chk_llm = NonConvMockLLM([checker_resp])
    result = run_fix_loop(
        target,
        backend,
        rev_llm,
        chk_llm,
        max_rounds=3,
        optimize=True,
        objective_verifier_enabled=False,
    )

    assert result.success
    assert backend.decompile_call_count == 1  # Only reverser calls it, checker uses cache


def test_non_optimize_calls_decompile_twice() -> None:
    """Without optimize, reverser + checker each call decompile (objective_verifier disabled)."""
    backend = DecompileTrackingBackend()
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")

    reverser_resp = (
        "```cpp\nvoid CTrain::ProcessControl() { do_work(); }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)"
    )
    checker_resp = "VERDICT: PASS\nSUMMARY: All good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none"

    rev_llm = NonConvMockLLM([reverser_resp])
    chk_llm = NonConvMockLLM([checker_resp])
    result = run_fix_loop(
        target,
        backend,
        rev_llm,
        chk_llm,
        max_rounds=3,
        optimize=False,
        objective_verifier_enabled=False,
    )

    assert result.success
    assert backend.decompile_call_count == 2  # Both reverser and checker call it


def test_optimize_fix_rounds_use_fresh_send() -> None:
    """In optimize mode, fix rounds should use send() not resume()."""
    backend = DecompileTrackingBackend()
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")

    reverser_responses = [
        "```cpp\nvoid CTrain::ProcessControl() { /*v1*/ }\n```\nREVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
        "```cpp\nvoid CTrain::ProcessControl() { /*v2*/ }\n```\nREVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
    ]
    checker_responses = [
        "VERDICT: FAIL\nSUMMARY: Bad\nISSUES:\n- fixit\nFIX_INSTRUCTIONS:\n- fixit",
        "VERDICT: PASS\nSUMMARY: Good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
    ]

    rev_llm = MockLLM(reverser_responses)
    chk_llm = NonConvMockLLM(checker_responses)
    result = run_fix_loop(
        target,
        backend,
        rev_llm,
        chk_llm,
        max_rounds=3,
        optimize=True,
        objective_verifier_enabled=False,
    )

    assert result.success
    assert result.rounds_used == 2
    # Verify the fix round sent fresh messages (not resume) and included issues
    assert len(rev_llm.send_calls) >= 1
    all_send_content = " ".join(msg.content for call in rev_llm.send_calls for msg in call)
    assert "fixit" in all_send_content


def test_optimize_loop_with_objective_verifier() -> None:
    """Optimize mode should work correctly with objective verifier enabled.

    Uses DecompileTrackingStructuralBackend so decompile call count can be verified.
    """
    backend = DecompileTrackingStructuralBackend()
    target = FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl")

    reverser_responses = [
        "```cpp\nvoid CTrain::ProcessControl() { }\n```\nREVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
        "```cpp\nvoid CTrain::ProcessControl() { if (m_nState) { FuncA(); FuncB(); FuncC(); } }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)",
    ]
    checker_responses = [
        "VERDICT: PASS\nSUMMARY: Looks good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
        "VERDICT: PASS\nSUMMARY: Looks good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none",
    ]

    rev_llm = MockLLM(reverser_responses)
    chk_llm = NonConvMockLLM(checker_responses)
    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=2, optimize=True)

    assert result.success
    assert result.rounds_used == 2
    assert result.objective_verdict is not None
    assert result.objective_verdict.verdict == Verdict.PASS


def test_loop_respects_profile_max_rounds() -> None:
    """run_fix_loop with a leaf profile should stop after 1 round even if checker fails."""
    from re_agent.core.models import PipelineProfile

    target = FunctionTarget(address="0x100", class_name="C", function_name="f")
    backend = StubBackend()

    reverser_resp = "```cpp\nvoid C::f() {}\n```\nREVERSED_FUNCTION: C::f (0x100)"
    # Checker always fails
    fail_resp = "VERDICT: FAIL\nSUMMARY: Issues found\nISSUES:\n- missing logic\nFIX_INSTRUCTIONS:\n- add logic"

    rev_llm = NonConvMockLLM([reverser_resp] * 5)
    chk_llm = NonConvMockLLM([fail_resp] * 5)

    leaf_profile = PipelineProfile(
        max_rounds=1,
        enable_phase1=False,
        inject_source_context=False,
        inject_few_shot=False,
        use_objective_verifier=False,
        few_shot_max_examples=0,
    )

    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=4, profile=leaf_profile)

    assert result.rounds_used == 1
    assert not result.success
    # Only 1 reverser call + 1 checker call
    assert len(rev_llm.send_calls) == 1
    assert len(chk_llm.send_calls) == 1


def test_loop_breaks_when_code_unchanged_between_rounds() -> None:
    """If the reverser produces identical code two rounds in a row, break without checker call."""
    target = FunctionTarget(address="0x200", class_name="C", function_name="g")
    backend = StubBackend()

    # Reverser always returns same code
    same_code = "```cpp\nvoid C::g() { call1(); }\n```\nREVERSED_FUNCTION: C::g (0x200)"
    fail_resp = "VERDICT: FAIL\nSUMMARY: Still wrong\nISSUES:\n- bad\nFIX_INSTRUCTIONS:\n- fix it"

    rev_llm = NonConvMockLLM([same_code] * 4)
    chk_llm = NonConvMockLLM([fail_resp] * 4)

    result = run_fix_loop(target, backend, rev_llm, chk_llm, max_rounds=4)

    # Round 1: reverse + check. Round 2: reverse produces same code → break.
    # Checker should NOT be called in round 2.
    assert not result.success
    assert result.rounds_used == 2
    # checker called exactly once (round 1 only)
    assert len(chk_llm.send_calls) == 1


def test_loop_profile_disables_objective_verifier() -> None:
    """run_fix_loop with use_objective_verifier=False should not run structural checks."""
    from re_agent.core.models import PipelineProfile

    target = FunctionTarget(address="0x100", class_name="C", function_name="f")
    backend = StubBackend()

    reverser_resp = "```cpp\nvoid C::f() {}\n```\nREVERSED_FUNCTION: C::f (0x100)"
    checker_resp = "VERDICT: PASS\nSUMMARY: OK\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none"

    rev_llm = NonConvMockLLM([reverser_resp])
    chk_llm = NonConvMockLLM([checker_resp])

    no_verify_profile = PipelineProfile(
        max_rounds=1,
        enable_phase1=False,
        inject_source_context=False,
        inject_few_shot=False,
        use_objective_verifier=False,
        few_shot_max_examples=0,
    )

    result = run_fix_loop(
        target, backend, rev_llm, chk_llm,
        max_rounds=4,
        objective_verifier_enabled=True,  # Would normally run, profile overrides
        profile=no_verify_profile,
    )

    assert result.success
    assert result.objective_verdict is None
