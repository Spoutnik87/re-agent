from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from re_agent.reverse.agents.source_context import SourceContextBuilder


def test_class_header_is_cached_across_calls(tmp_path: Path) -> None:
    """The rglob scan for the class header must run once per class, not per function."""
    header = tmp_path / "CTest.h"
    header.write_text("class CTest {\npublic:\n    void foo();\n};\n", encoding="utf-8")

    profile = MagicMock()
    profile.source_root = str(tmp_path)
    indexer = MagicMock()
    indexer.token_index = []
    indexer.find.return_value = None

    builder = SourceContextBuilder(source_root=tmp_path, profile=profile, indexer=indexer)

    call_count = [0]
    original = builder._find_class_header

    def counting_find(class_name: str) -> str:
        call_count[0] += 1
        return original(class_name)

    builder._find_class_header = counting_find  # type: ignore[assignment]

    from re_agent.reverse.core.models import FunctionTarget

    target1 = FunctionTarget(address="0x1000", class_name="CTest", function_name="foo")
    target2 = FunctionTarget(address="0x1001", class_name="CTest", function_name="bar")

    builder.build(target1)
    builder.build(target2)

    assert call_count[0] <= 1, f"Expected <=1 header scan, got {call_count[0]}"


def test_cache_invalidates_on_different_class(tmp_path: Path) -> None:
    header_a = tmp_path / "ClassA.h"
    header_a.write_text("class ClassA {};\n", encoding="utf-8")
    header_b = tmp_path / "ClassB.h"
    header_b.write_text("class ClassB {};\n", encoding="utf-8")

    profile = MagicMock()
    indexer = MagicMock()
    indexer.token_index = []
    indexer.find.return_value = None

    builder = SourceContextBuilder(source_root=tmp_path, profile=profile, indexer=indexer)

    call_count = [0]
    original = builder._find_class_header

    def counting_find(class_name: str) -> str:
        call_count[0] += 1
        return original(class_name)

    builder._find_class_header = counting_find  # type: ignore[assignment]

    from re_agent.reverse.core.models import FunctionTarget

    builder.build(FunctionTarget(address="0x1000", class_name="ClassA", function_name="f"))
    builder.build(FunctionTarget(address="0x1001", class_name="ClassB", function_name="g"))

    assert call_count[0] == 2, f"Expected 2 header scans for two classes, got {call_count[0]}"

    builder.build(FunctionTarget(address="0x1002", class_name="ClassA", function_name="h"))
    assert call_count[0] == 2, "Second call for same class should use cache"
