"""Tests for CheckerAgent with cached DecompileResult."""
from __future__ import annotations

from re_agent.agents.checker import CheckerAgent
from re_agent.core.models import DecompileResult, FunctionTarget
from re_agent.llm.protocol import Message


class CheckerRecordingLLM:
    def __init__(self) -> None:
        self.response = (
            "VERDICT: PASS\nSUMMARY: All good\nISSUES:\n- none\nFIX_INSTRUCTIONS:\n- none"
        )
        self.sent_messages: list[list[Message]] = []
        self.supports_conv = False

    def send(self, messages: list[Message], **kwargs: object) -> str:
        self.sent_messages.append(list(messages))
        return self.response

    @property
    def supports_conversations(self) -> bool:
        return self.supports_conv

    def new_conversation(self, system: str) -> str:
        return ""

    def resume(self, conversation_id: str, message: str) -> str:
        return ""


class DecompileCountingBackend:
    def __init__(self, decompiled_text: str) -> None:
        self.decompiled_text = decompiled_text
        self.decompile_call_count = 0

    @property
    def capabilities(self) -> object:
        class _Caps:
            pass
        return _Caps()

    def decompile(self, target: str) -> DecompileResult:
        self.decompile_call_count += 1
        return DecompileResult(
            address=target,
            name="CStub::func",
            signature="",
            decompiled=self.decompiled_text,
            raw_output=self.decompiled_text,
        )


def test_checker_skips_backend_call_with_cached_decompile() -> None:
    """When decompile_result is passed, checker should NOT call backend.decompile()."""
    backend = DecompileCountingBackend("void CStub::func() { ret = 42; }")
    llm = CheckerRecordingLLM()
    checker = CheckerAgent(llm, backend)

    cached = DecompileResult(
        address="0x123",
        name="CStub::func",
        signature="",
        decompiled="void CStub::func() { ret = 42; }",
        raw_output="void CStub::func() { ret = 42; }",
    )
    target = FunctionTarget(address="0x123", class_name="CStub", function_name="func")

    result = checker.check("void CStub::func() { ret = 42; }", target, decompile_result=cached)
    assert backend.decompile_call_count == 0
    assert result.verdict.value == "PASS"


def test_checker_calls_backend_when_no_cached_decompile() -> None:
    """Without cached decompile result, checker should call backend.decompile()."""
    backend = DecompileCountingBackend("void CStub::func() { ret = 42; }")
    llm = CheckerRecordingLLM()
    checker = CheckerAgent(llm, backend)

    target = FunctionTarget(address="0x123", class_name="CStub", function_name="func")
    checker.check("void CStub::func() { ret = 42; }", target)
    assert backend.decompile_call_count == 1


def test_checker_with_cached_decompile_uses_correct_text() -> None:
    """Cached decompile text should appear in the checker prompt."""
    backend = DecompileCountingBackend("SHOULD NOT APPEAR")
    llm = CheckerRecordingLLM()
    checker = CheckerAgent(llm, backend)

    cached = DecompileResult(
        address="0x123",
        name="CTest::method",
        signature="",
        decompiled="void CTest::method() { specific_logic(); }",
        raw_output="void CTest::method() { specific_logic(); }",
    )
    target = FunctionTarget(address="0x123", class_name="CTest", function_name="method")

    checker.check(
        "void CTest::method() { specific_logic(); }",
        target,
        decompile_result=cached,
    )

    prompt = checker.last_prompt
    assert "specific_logic" in prompt
    assert "SHOULD NOT APPEAR" not in prompt
    assert backend.decompile_call_count == 0


def test_checker_none_cached_decompile_behaves_like_original() -> None:
    """When decompile_result=None, behavior should match original (calls backend)."""
    backend = DecompileCountingBackend("void CTest::method() { original_call(); }")
    llm = CheckerRecordingLLM()
    checker = CheckerAgent(llm, backend)

    target = FunctionTarget(address="0x123", class_name="CTest", function_name="method")
    checker.check("void CTest::method() { original_call(); }", target, decompile_result=None)

    assert backend.decompile_call_count == 1
    assert "original_call" in checker.last_prompt
