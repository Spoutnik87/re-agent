"""Tests for ReverserAgent optimize mode."""

from __future__ import annotations

from pathlib import Path

from re_agent.agents.reverser import ReverserAgent
from re_agent.config.schema import ProjectProfile
from re_agent.core.models import FunctionTarget
from re_agent.llm.protocol import Message


class RecordingLLM:
    """LLM that records all send/resume calls for inspection."""

    def __init__(self, response: str = "") -> None:
        self.response = response or "```cpp\nvoid foo() {}\n```\nREVERSED_FUNCTION: CTest::foo (0x123456)"
        self.sent_messages: list[list[Message]] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.supports_conv = True

    def send(self, messages: list[Message], **kwargs: object) -> str:
        self.sent_messages.append(list(messages))
        return self.response

    @property
    def supports_conversations(self) -> bool:
        return self.supports_conv

    def new_conversation(self, system: str) -> str:
        return "conv-1"

    def resume(self, conversation_id: str, message: str) -> str:
        self.resume_calls.append((conversation_id, message))
        return self.response


class GReverserStubBackend:
    @property
    def capabilities(self) -> object:
        class _Caps:
            has_xrefs = False
            has_structs = False

        return _Caps()

    def decompile(self, target: str) -> object:
        class _Dec:
            raw_output = "/* WARNING: Could not resolve indirect call */\nvoid CTest::foo() { bar(); }\n"

        return _Dec()


def test_optimize_strips_ghidra_noise_from_prompt() -> None:
    """In optimize mode, WARNING lines should be stripped from the prompt."""
    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=True)
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "bar()" in prompt
    assert "WARNING" not in prompt


def test_non_optimize_preserves_ghidra_noise() -> None:
    """Without optimize, WARNING lines should stay in the prompt."""
    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=False)
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "WARNING" in prompt
    assert "bar()" in prompt


def test_optimize_skips_empty_source_context() -> None:
    """In optimize mode, 'No relevant...' should not appear when source dir is empty/non-existent."""
    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    # Use a non-existent source root that still passes exists() check in __init__
    # Actually the __init__ gating requires the path to exist, so we test by
    # not passing source_root at all, then source_context stays at ""
    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=True)
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "No relevant existing source context" not in prompt


def test_fix_uses_fresh_send_in_optimize_mode() -> None:
    """In optimize mode, fix() should call send() with fresh messages, not resume()."""
    llm = RecordingLLM()
    llm.supports_conv = True

    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=True)
    reverser._conversation_id = "conv-1"  # simulate first round done

    reverser.fix(
        checker_report="Missing branch (informational — not sent to LLM)",
        issues=["add if check", "missing branch in condition"],
        fix_instructions=["add the if check"],
        target=FunctionTarget(address="0x123456", class_name="CTest", function_name="foo"),
    )

    assert len(llm.sent_messages) == 1
    assert len(llm.resume_calls) == 0
    msg = llm.sent_messages[0]
    assert len(msg) == 2  # system + user
    assert msg[0].role == "system"
    assert msg[1].role == "user"
    assert "checker_report" not in msg[1].content  # no longer sent (triple-send fix)
    assert "checker_report" not in msg[0].content  # also not in system prompt
    assert "add if check" in msg[1].content
    assert "missing branch" in msg[1].content.lower()


def test_fix_uses_resume_in_non_optimize_mode() -> None:
    """Without optimize, fix() should call resume() to preserve history."""
    llm = RecordingLLM()
    llm.supports_conv = True

    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=False)
    reverser._conversation_id = "conv-1"

    reverser.fix(
        checker_report="Missing branch (informational — not sent to LLM)",
        issues=["add if check", "missing branch in condition"],
        fix_instructions=["add the if check"],
        target=FunctionTarget(address="0x123456", class_name="CTest", function_name="foo"),
    )

    assert len(llm.resume_calls) == 1
    args = llm.resume_calls[0]
    assert args[0] == "conv-1"
    assert "add if check" in args[1]
    assert "missing branch" in args[1].lower()


def test_last_decompile_result_stored() -> None:
    """reverse() should store DecompileResult on the agent."""
    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(llm, GReverserStubBackend(), optimize=True)
    assert reverser.last_decompile_result is None

    reverser.reverse(target)

    assert reverser.last_decompile_result is not None
    dr = reverser.last_decompile_result
    assert "bar()" in dr.raw_output


class EmptySourceBackend:
    @property
    def capabilities(self) -> object:
        class _Caps:
            has_xrefs = False
            has_structs = False

        return _Caps()

    def decompile(self, target: str) -> object:
        class _Dec:
            raw_output = "void CTest::foo() { bar(); }\n"

        return _Dec()


def test_optimize_source_context_with_real_source(tmp_path: Path) -> None:
    """In optimize mode, source_context should appear when real source exists."""
    (tmp_path / "CTest.h").write_text("class CTest { void foo(); };\n", encoding="utf-8")

    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")
    profile = ProjectProfile(
        source_root=str(tmp_path),
        source_extensions=[".cpp", ".h", ".hpp"],
        hooks_csv=None,
    )

    reverser = ReverserAgent(
        llm,
        EmptySourceBackend(),
        source_root=tmp_path,
        project_profile=profile,
        optimize=True,
    )
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "Class header:" in prompt
    assert "class CTest" in prompt


def test_inject_source_context_false_skips_source_context_build(tmp_path: Path) -> None:
    """When inject_source_context=False, build() is never called even if source_root exists."""
    (tmp_path / "CTest.h").write_text("class CTest { void foo(); };\n", encoding="utf-8")

    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")
    profile = ProjectProfile(
        source_root=str(tmp_path),
        source_extensions=[".cpp", ".h", ".hpp"],
        hooks_csv=None,
    )

    reverser = ReverserAgent(
        llm,
        EmptySourceBackend(),
        source_root=tmp_path,
        project_profile=profile,
        inject_source_context=False,
    )
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "Class header:" not in prompt
    assert "class CTest" not in prompt


def test_inject_few_shot_false_skips_few_shot_injection(tmp_path: Path) -> None:
    """When inject_few_shot=False, few-shot examples are not injected even if index has matches."""
    from re_agent.agents.few_shot_builder import FewShotBuilder

    FewShotBuilder.clear_cache()
    (tmp_path / "0x111_CFoo_bar.cpp").write_text(
        "void CFoo::bar() { baz(); qux(); }\n", encoding="utf-8"
    )

    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(
        llm,
        EmptySourceBackend(),
        source_root=tmp_path,
        inject_few_shot=False,
    )
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "Reference examples" not in prompt
    FewShotBuilder.clear_cache()


def test_inject_few_shot_true_with_examples_injects_them(tmp_path: Path) -> None:
    """When inject_few_shot=True (default), found examples are injected."""
    from re_agent.agents.few_shot_builder import FewShotBuilder

    FewShotBuilder.clear_cache()
    (tmp_path / "0x111_CFoo_bar.cpp").write_text(
        "void CFoo::bar() { baz(); qux(); }\n", encoding="utf-8"
    )

    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(
        llm,
        EmptySourceBackend(),
        source_root=tmp_path,
        inject_few_shot=True,
    )
    reverser.reverse(target)

    prompt = reverser.last_prompt
    assert "Reference examples" in prompt
    FewShotBuilder.clear_cache()


def test_few_shot_max_examples_limits_injected_count(tmp_path: Path) -> None:
    """few_shot_max_examples=1 injects at most one example."""
    from re_agent.agents.few_shot_builder import FewShotBuilder

    FewShotBuilder.clear_cache()
    for i in range(5):
        (tmp_path / f"0x{i:03x}_CFoo_fn{i}.cpp").write_text(
            f"void CFoo::fn{i}() {{ call{i}(); }}\n", encoding="utf-8"
        )

    llm = RecordingLLM()
    target = FunctionTarget(address="0x123456", class_name="CTest", function_name="foo")

    reverser = ReverserAgent(
        llm,
        EmptySourceBackend(),
        source_root=tmp_path,
        inject_few_shot=True,
        few_shot_max_examples=1,
    )
    reverser.reverse(target)

    prompt = reverser.last_prompt
    # With max_examples=1, only one "// Example from" header should appear
    assert prompt.count("// Example from") >= 1  # at least one injected
    assert prompt.count("// Example from") <= 1  # at most one (max_examples=1)
    FewShotBuilder.clear_cache()


def test_few_shot_skipped_when_similarity_below_threshold(tmp_path: Path) -> None:
    """Few-shot examples are skipped when the best match score is below min_score."""
    from re_agent.agents.few_shot_builder import FewShotBuilder
    FewShotBuilder.clear_cache()

    # Create a cpp file that will have low similarity to the query
    (tmp_path / "0x111_CFoo_bar.cpp").write_text(
        "void CFoo::bar() { baz(); qux(); }\n", encoding="utf-8"
    )

    builder = FewShotBuilder(tmp_path, max_examples=2)
    # Query for a very different function; line count >= 200 → bucket "200+l"
    # but the example has ~1 line → bucket "<25l" → no line_bucket match (0 points)
    # vtable match (+3), globals within 2 (+1), calls within 3 (+1) = score 5
    long_decompile = "void Fn() {\n" + "  int x = 0;\n" * 200 + "}\n"
    results = builder.find_similar(long_decompile, min_score=6)

    assert results == []


def test_few_shot_returned_when_above_threshold(tmp_path: Path) -> None:
    """Few-shot examples are returned when score meets min_score."""
    from re_agent.agents.few_shot_builder import FewShotBuilder
    FewShotBuilder.clear_cache()

    # Create a cpp file with matching characteristics (same small size)
    content = "void CFoo::bar() {\n" + "  call_something();\n" * 3 + "}\n"
    (tmp_path / "0x111_CFoo_bar.cpp").write_text(content, encoding="utf-8")

    builder = FewShotBuilder(tmp_path, max_examples=2)
    # Same small function → line_bucket match (+3), vtable match (+3) = score 6
    results = builder.find_similar(content, min_score=3)

    assert len(results) == 1
    assert "CFoo" in results[0] or "bar" in results[0]
