from __future__ import annotations

from pathlib import Path

from re_agent.build.transform.context_builder import build_context


def test_build_context_uses_provided_source_map(tmp_path: Path) -> None:
    """When a source_map is provided, build_context must NOT glob the dir."""
    src = tmp_path / "0x1000_foo.cpp"
    src.write_text("void foo() {}", encoding="utf-8")

    source_map = {"0x1000": "void foo() {}"}

    ctx = build_context(
        subunit=["0x1000"],
        module_functions=["0x1000"],
        decompiled_dir=tmp_path,
        context_window=3,
        cache=None,
        source_map=source_map,
    )
    assert len(ctx["functions_to_transform"]) == 1
    assert ctx["functions_to_transform"][0]["code"] == "void foo() {}"
