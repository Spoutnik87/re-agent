from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from re_agent.build.validate.compiler import compile_module_check


def test_compile_module_check_compiles_files_together(tmp_path: Path) -> None:
    """compile_module_check must pass all .cpp files to the compiler in one
    invocation so cross-file link errors are caught."""
    file_a = tmp_path / "a.cpp"
    file_a.write_text("void a() {}", encoding="utf-8")
    file_b = tmp_path / "b.cpp"
    file_b.write_text("void b() {}", encoding="utf-8")

    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-c -Wall"

    import re_agent.build.validate.compiler as comp_mod

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured_cmd.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result

    original_run = comp_mod.subprocess.run
    comp_mod.subprocess.run = fake_run
    try:
        ok, err = compile_module_check([file_a, file_b], cfg)
    finally:
        comp_mod.subprocess.run = original_run

    assert ok is True
    assert len(captured_cmd) == 1, "expected a single compiler invocation with all files"
    assert str(file_a) in captured_cmd[0]
    assert str(file_b) in captured_cmd[0]
