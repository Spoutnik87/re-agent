from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from re_agent.common import compiler as comp


def test_compile_source_builds_expected_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result

    monkeypatch.setattr(comp.subprocess, "run", fake_run)
    ok, err = comp.compile_source("int main(){}", "g++", "-std=c++23 -c -Wall")
    assert ok is True
    assert err == ""
    assert captured[0][0] == "g++"
    assert "-std=c++23" in captured[0]
    assert "-c" in captured[0]


def test_compile_source_injects_decls_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result

    monkeypatch.setattr(comp.subprocess, "run", fake_run)
    comp.compile_source("int x;", "g++", "-c", decls_header="/tmp/decls.h")
    cmd = captured[0]
    assert "-include" in cmd
    assert "/tmp/decls.h" in cmd
    # -include must precede the source temp file
    assert cmd.index("-include") < cmd.index(str(cmd[-1]))
    # -I flag must point to the resolve()d parent of decls_header
    assert "-I" in cmd
    expected_parent = str(Path("/tmp/decls.h").parent.resolve())
    i_idx = cmd.index("-I")
    assert cmd[i_idx + 1] == expected_parent
    # -I must come after -include decls_header
    assert cmd.index("-I") > cmd.index("-include")


def test_include_args_adds_parent_dir_for_gcc() -> None:
    """_include_args must emit -I <parent> along with -include for GCC."""
    args = comp._include_args("/tmp/some_dir/_decls.h", compiler="g++")
    assert "-include" in args
    assert "/tmp/some_dir/_decls.h" in args
    assert "-I" in args
    # The -I value should be the resolve()d parent of decls_header
    i_idx = args.index("-I")
    expected_parent = str(Path("/tmp/some_dir/_decls.h").parent.resolve())
    assert args[i_idx + 1] == expected_parent


def test_include_args_adds_parent_dir_for_msvc() -> None:
    """_include_args must emit /I <parent> along with /FI for MSVC."""
    args = comp._include_args("C:\\tmp\\some_dir\\_decls.h", compiler="cl.exe")
    assert "/FI" in args
    assert "C:\\tmp\\some_dir\\_decls.h" in args
    assert "/I" in args
    i_idx = args.index("/I")
    expected_parent = str(Path("C:\\tmp\\some_dir\\_decls.h").parent.resolve())
    assert args[i_idx + 1] == expected_parent


def test_include_args_none_returns_empty() -> None:
    """_include_args must return empty list when decls_header is None."""
    assert comp._include_args(None) == []
    assert comp._include_args("") == []


def test_include_args_relative_path_resolves_correctly() -> None:
    """_include_args must resolve relative decls_header to absolute parent."""
    args = comp._include_args("relative_dir/decls.h", compiler="g++")
    assert "-I" in args
    i_idx = args.index("-I")
    expected_parent = str(Path("relative_dir/decls.h").resolve().parent)
    assert args[i_idx + 1] == expected_parent


def test_compile_source_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        result = MagicMock()
        result.returncode = 1
        result.stderr = "error: boom"
        result.stdout = ""
        return result

    monkeypatch.setattr(comp.subprocess, "run", fake_run)
    ok, err = comp.compile_source("bad", "g++", "-c")
    assert ok is False
    assert "boom" in err


def test_compile_source_missing_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError

    monkeypatch.setattr(comp.subprocess, "run", fake_run)
    ok, err = comp.compile_source("x", "nope-g++", "-c")
    assert ok is False
    assert "Compiler not found" in err


def test_compile_sources_skips_non_cpp(tmp_path: Path) -> None:
    header = tmp_path / "a.h"
    header.write_text("// header", encoding="utf-8")
    ok, err = comp.compile_sources([header], "g++", "-c")
    # No .cpp files -> trivially OK without invoking the compiler.
    assert ok is True
    assert err == ""


# ──────────────────────────────────────────────────────────────────────
# Generated-header compile context (Todo 1 — failing-first tests)
# ──────────────────────────────────────────────────────────────────────
# These tests MUST FAIL on unchanged production. They document the
# desired ``compile_generated_file_set`` helper API that writes a set
# of generated files (headers + sources) to a temp directory tree and
# compiles the main .cpp with the headers available.
#
# When the helper is implemented, both tests will pass:
# 1. Compilation succeeds when the .h is provided alongside the .cpp.
# 2. Compilation fails when the .h that the .cpp includes is missing.


def test_compile_generated_header_file_set_documents_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_generated_file_set must exist to compile generated .cpp with its .h.

    FAILS on unchanged production: ImportError confirms the missing API.
    Documents the expected shape:
      compile_generated_file_set(
          files: list[dict[str, str]],   # e.g. [{path, content}, ...]
          target_path: str | Path,       # temp dir to write files into
          cfg: Any                        # compiler config (compiler, flags, ...)
      ) -> tuple[bool, str]

    When implemented:
    - Writes ALL files (headers + sources) to ``target_path`` preserving
      relative directory structure (include/..., src/...).
    - Compiles the main .cpp with ``-I <target_path>`` so #include directives
      resolve to the generated headers.
    - Returns (True, "") on success.
    """
    from re_agent.common.compiler import compile_generated_file_set  # type: ignore[import-untyped]  # noqa: F811

    # Fake compile_source so test does not require real g++ on PATH.
    monkeypatch.setattr(
        comp,
        "compile_source",
        lambda source, compiler, flags, **kwargs: (True, ""),
    )

    files = [
        {
            "path": "include/renderer/0x004117c0__A.h",
            "content": "#pragma once\nint generatedHeaderValue();\n",
        },
        {
            "path": "src/renderer/0x004117c0__A.cpp",
            "content": ('#include "include/renderer/0x004117c0__A.h"\nint useHeader() { return 0; }\n'),
        },
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall"

    ok, err = compile_generated_file_set(files, "/tmp/test_out", cfg)
    assert ok is True, f"Expected success when .h is provided, got err={err!r}"
    assert err == ""


def test_compile_generated_header_file_set_missing_header_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_generated_file_set without a required .h must fail.

    FAILS on unchanged production: ImportError confirms the missing API.
    Documents the expected error path:
      compile_generated_file_set returns (False, "No such file...") when a
      generated .cpp includes a .h that is NOT in the file set.
    """
    from re_agent.common.compiler import compile_generated_file_set  # type: ignore[import-untyped]  # noqa: F811

    # Fake compile_source to simulate header-not-found during actual compilation.
    monkeypatch.setattr(
        comp,
        "compile_source",
        lambda source, compiler, flags, **kwargs: (False, "No such file: include/renderer/0x004117c0__A.h"),
    )

    files = [
        {
            "path": "src/renderer/0x004117c0__A.cpp",
            "content": ('#include "include/renderer/0x004117c0__A.h"\nint useHeader() { return 0; }\n'),
        },
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall"

    ok, err = compile_generated_file_set(files, "/tmp/test_out", cfg)
    assert ok is False, "Expected failure when .h is missing from file set, got ok=True"
    assert "No such file" in err or "include" in err.lower(), f"Error must mention missing header, got: {err!r}"


# ──────────────────────────────────────────────────────────────────────
# Basename generated-header include frontier (Todo 1 — characterisation)
# ──────────────────────────────────────────────────────────────────────
# Tests document that ``compile_generated_file_set`` currently passes only
# the temp-directory root as an include dir, meaning a generated .cpp that
# #includes a generated header by basename (e.g. ``#include "A.h"``) will
# fail when the header lives in a subdirectory like ``include/renderer/A.h``.
#
# The desired future behaviour adds parent directories of generated headers
# to the include dir list so both ``#include "include/renderer/A.h"`` and
# ``#include "A.h"`` resolve.


def test_compile_generated_file_set_basename_include_only_tmp_root_in_includes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Characterisation: basename include cannot resolve with current include dirs.

    Captures the ``include_dirs`` passed to ``compile_source`` and asserts
    exactly ONE entry (the temp root) is present. No subdirectory parent dirs.
    When the helper is updated to add parent dirs, this assertion will fail.
    """
    from unittest.mock import MagicMock

    import re_agent.common.compiler as comp

    captured_kwargs: list[dict] = []

    def _capture_compile(source: str, compiler: str, flags: str, **kwargs: object) -> tuple[bool, str]:
        captured_kwargs.append(kwargs)
        return (False, "simulated compile failure")

    monkeypatch.setattr(comp, "compile_source", _capture_compile)

    files = [
        {
            "path": "include/renderer/0x00418a80__A.h",
            "content": "#pragma once\nint foo();\n",
        },
        {
            "path": "src/renderer/0x00418a80__A.cpp",
            "content": '#include "0x00418a80__A.h"\nint useFoo() { return foo(); }\n',
        },
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall"

    comp.compile_generated_file_set(files, "src/renderer/0x00418a80__A.cpp", cfg)

    assert len(captured_kwargs) >= 1, "compile_source must have been called"
    kwargs = captured_kwargs[0]
    include_dirs = kwargs.get("include_dirs", [])

    # New behaviour: temp root + unique parent dirs for generated files.
    # For the fixture file set (include/renderer/...h, src/renderer/...cpp),
    # we expect at least 2 entries (tmpdir + at least one parent dir).
    assert len(include_dirs) >= 2, (
        f"Expected >= 2 include dirs (tmpdir root + parent dirs), got {len(include_dirs)}: {include_dirs}"
    )


# ──────────────────────────────────────────────────────────────────────
# Todo 1: _decls.h attributes warning demotion in generated-file compile
# ──────────────────────────────────────────────────────────────────────
# These tests document the narrow demotion contract:
#   - compile_generated_file_set with GCC + decls_header must add
#     -Wno-error=attributes to flags (FAILS on unchanged production
#     because current code passes flags through unchanged).
#   - No decls_header → NO flag added (characterisation, passes now).
#   - MSVC compiler → NO flag added (characterisation, passes now).
#   - Single-source compile_source → NO flag added (characterisation).
#   - Flags already containing -Wno-error=attributes must not duplicate.
#
# The failing-first test (test_decls_header_attributes_warning_missing_*)
# is the one that will fail NOW and pass AFTER the narrow demotion is
# implemented in compile_generated_file_set.


def test_decls_header_attributes_warning_missing_failing_first_generated_file_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: generated-file compile with decls_header + GCC must
    add -Wno-error=attributes to flags.

    FAILS on unchanged production because compile_generated_file_set
    passes flags through verbatim. Documents the DESIRED narrow demotion:
    when a concrete decls_header is set and the compiler is GCC/MinGW-like,
    the generated-file compile should include -Wno-error=attributes so that
    the _decls.h -Werror=attributes wall does not mask real body errors.
    """
    captured_kwargs: list[dict[str, object]] = []

    def _capture_compile(source: str, compiler: str, flags: str, **kwargs: object) -> tuple[bool, str]:
        captured_kwargs.append(
            {
                "source": source,
                "compiler": compiler,
                "flags": flags,
                **kwargs,
            }
        )
        return (True, "")

    monkeypatch.setattr(comp, "compile_source", _capture_compile)

    files = [
        {"path": "src/mod/test.cpp", "content": "int test_fn() { return 0; }\n"},
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall -Werror"
    cfg.output.decls_header = "/path/to/_decls.h"

    comp.compile_generated_file_set(files, "src/mod/test.cpp", cfg)

    assert len(captured_kwargs) == 1, "compile_source must have been called"
    actual_flags = captured_kwargs[0]["flags"]
    assert isinstance(actual_flags, str)
    assert "-Wno-error=attributes" in actual_flags, (
        f"Expected -Wno-error=attributes in flags when decls_header is set "
        f"and compiler is GCC-like, got flags={actual_flags!r}. "
        f"Current production does NOT add this flag."
    )


def test_generated_file_set_no_decls_header_no_attributes_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHARACTERISATION: generated-file compile WITHOUT decls_header must
    NOT add -Wno-error=attributes. Passes on current code; must continue
    to pass after the narrow demotion is implemented."""
    captured_kwargs: list[dict[str, object]] = []

    def _capture_compile(source: str, compiler: str, flags: str, **kwargs: object) -> tuple[bool, str]:
        captured_kwargs.append({"flags": flags, **kwargs})
        return (True, "")

    monkeypatch.setattr(comp, "compile_source", _capture_compile)

    files = [
        {"path": "src/mod/test.cpp", "content": "int test_fn() { return 0; }\n"},
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall -Werror"
    comp.compile_generated_file_set(files, "src/mod/test.cpp", cfg)

    assert len(captured_kwargs) == 1, "compile_source must have been called"
    actual_flags = captured_kwargs[0]["flags"]
    assert isinstance(actual_flags, str)
    assert "-Wno-error=attributes" not in actual_flags, (
        f"Must NOT add -Wno-error=attributes when no decls_header, got flags={actual_flags!r}"
    )


def test_generated_file_set_msvc_no_attributes_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHARACTERISATION: generated-file compile with MSVC must NOT add
    -Wno-error=attributes (MSVC does not use -Werror=attributes)."""
    captured_kwargs: list[dict[str, object]] = []

    def _capture_compile(source: str, compiler: str, flags: str, **kwargs: object) -> tuple[bool, str]:
        captured_kwargs.append({"compiler": compiler, "flags": flags, **kwargs})
        return (True, "")

    monkeypatch.setattr(comp, "compile_source", _capture_compile)

    files = [
        {"path": "src/mod/test.cpp", "content": "int test_fn() { return 0; }\n"},
    ]
    cfg = MagicMock()
    cfg.output.compiler = "cl.exe"
    cfg.output.compiler_flags = "/nologo /c /WX"
    cfg.output.decls_header = "C:\\_decls.h"

    comp.compile_generated_file_set(files, "src/mod/test.cpp", cfg)

    assert len(captured_kwargs) == 1, "compile_source must have been called"
    actual_flags = captured_kwargs[0]["flags"]
    assert isinstance(actual_flags, str)
    assert "-Wno-error=attributes" not in actual_flags, (
        f"Must NOT add -Wno-error=attributes for MSVC, got flags={actual_flags!r}"
    )


def test_compile_source_with_decls_header_no_attributes_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHARACTERISATION: single-source compile_source with decls_header must
    NOT gain -Wno-error=attributes. The narrow demotion is scoped ONLY to
    compile_generated_file_set (the generated-file transform compile path),
    never to the generic single-source compile."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result

    monkeypatch.setattr(comp.subprocess, "run", fake_run)

    comp.compile_source(
        "int x;",
        "g++",
        "-std=c++23 -c -Wall -Werror",
        decls_header="/tmp/_decls.h",
    )

    assert len(captured) == 1, "subprocess.run must have been called"
    cmd_str = " ".join(captured[0])
    assert "-Wno-error=attributes" not in cmd_str, (
        f"compile_source must NOT gain -Wno-error=attributes in single-source path, got cmd={captured[0]!r}"
    )


def test_generated_file_set_decls_header_idempotent_attributes_warning_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDEMPOTENCY: when flags already contain -Wno-error=attributes, the
    helper must NOT duplicate it.

    Passes on current code (flags pass through unchanged). Must continue
    to pass after the narrow demotion — the append logic must check for
    prior presence."""
    captured_kwargs: list[dict[str, object]] = []

    def _capture_compile(source: str, compiler: str, flags: str, **kwargs: object) -> tuple[bool, str]:
        captured_kwargs.append({"flags": flags, **kwargs})
        return (True, "")

    monkeypatch.setattr(comp, "compile_source", _capture_compile)

    files = [
        {"path": "src/mod/test.cpp", "content": "int test_fn() { return 0; }\n"},
    ]
    cfg = MagicMock()
    cfg.output.compiler = "g++"
    cfg.output.compiler_flags = "-std=c++23 -c -Wall -Werror -Wno-error=attributes"
    cfg.output.decls_header = "/path/to/_decls.h"

    comp.compile_generated_file_set(files, "src/mod/test.cpp", cfg)

    assert len(captured_kwargs) == 1, "compile_source must have been called"
    actual_flags = captured_kwargs[0]["flags"]
    assert isinstance(actual_flags, str)
    occurrences = actual_flags.count("-Wno-error=attributes")
    assert occurrences == 1, (
        f"Expected exactly 1 occurrence of -Wno-error=attributes, got {occurrences} in flags={actual_flags!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Todo 1: Generated sibling-header include contract (failing-first)
# ─────────────────────────────────────────────────────────────────────────────
# The transform prompt (``transform_system.md``) currently requires every
# generated .cpp to include ``_decls.h`` but does NOT require inclusion of
# the generated sibling .h.  Four evidence shapes in
# ``raw-renderer-subunit-1-...txt`` prove this gap:
#
#   • 0x00418a80 — Entity_418a80  struct used in GetEntityMultiplier
#   • 0x004604c0 — Entity_4604c0  packed struct used in EntityProcessFlags
#   • 0x00418bd0 — CMyClass::UpdateWithParam  method
#   • 0x0045f610 — Owner_45f610  struct used in CheckDistanceToTarget
#
# The failing-first test asserts the prompt contract MUST include a
# sibling-header instruction.  The characterisation test in
# ``test_transform_harness.py`` documents the expected passing shape.


def test_sibling_header_include_instruction_in_prompt_failing_first() -> None:
    """FAILING-FIRST: The transform prompt must instruct the LLM to include
    the generated sibling .h in every .cpp that uses types from it.

    The current prompt (``transform_system.md``) only requires
    ``#include "_decls.h"`` (lines 6–19).  Rule 11 tells the LLM to *produce*
    a .h file but does NOT require including it in the .cpp.  After Todo 2
    adds a sibling-header ``#include`` requirement, this test will pass.

    Expected prompt addition (after Todo 2)::

        If an emitted .cpp file uses types declared in a generated .h file
        for the same function, it MUST ``#include`` that .h after
        ``#include "_decls.h"``.

    Expected .cpp shape for 0x00418a80::

        #include "_decls.h"
        // Original function: 0x00418a80
        #include "0x00418a80__GetEntityMultiplier.h"   # <-- currently MISSING
        #include <cmath>
        long double GetEntityMultiplier(void*, Entity_418a80*) ...
    """
    prompt_path = (
        Path(__file__).resolve().parent.parent.parent / "src" / "re_agent" / "build" / "prompts" / "transform_system.md"
    )
    assert prompt_path.exists(), f"Prompt file not found at {prompt_path}"
    prompt_text = prompt_path.read_text(encoding="utf-8")

    # ── FAILING-FIRST ───────────────────────────────────────────────────
    # The prompt must contain an instruction to INCLUDE generated sibling
    # headers in the .cpp.  Rule 11 tells the LLM to *produce* a .h but
    # does NOT tell it to ``#include`` that .h in the .cpp — that is the
    # contract gap.
    #
    # We check for a phrase that couples "include" (the directive) with
    # "generated/sibling/corresponding .h" (the target).  The current
    # prompt has no such phrase: it only requires ``#include "_decls.h"``
    # and separately says "produce a corresponding .h".
    # The prompt must contain an instruction coupling ``#include`` with a
    # generated sibling header.  These three plain-text phrases capture
    # that requirement; none appear in the current prompt (it only says
    # "produce a corresponding .h", not "include the corresponding .h").
    _sibling_include_phrases = (
        "include the generated",
        "include the sibling",
        "include the corresponding",
    )
    found = any(phrase in prompt_text.lower() for phrase in _sibling_include_phrases)
    assert found, (
        "FAILING-FIRST: The transform prompt must require generated .cpp "
        "files to #include their corresponding generated .h header when "
        "one is emitted for the same function.\n\n"
        f"Prompt checked: {prompt_path}\n\n"
        "Current prompt mentions:\n"
        '  - "MUST include `_decls.h`" (only _decls.h, line 8)\n'
        '  - "produce a corresponding .h" (produce ≠ include, line 44)\n\n'
        "Missing: any instruction that couples `#include` with a generated "
        "sibling header.\n\n"
        "Expected prompt addition (after Todo 2):\n"
        '  "If an emitted .cpp file uses types declared in a generated .h '
        "file for the same function, it MUST #include that .h after "
        '#include \\"_decls.h\\"."\n\n'
        "Expected passing .cpp shape for 0x00418a80:\n"
        '  #include "_decls.h"\n'
        "  // Original function: 0x00418a80\n"
        '  #include "0x00418a80__GetEntityMultiplier.h"   '
        "<-- currently MISSING\n"
        "  #include <cmath>\n"
        "  long double GetEntityMultiplier(void*, Entity_418a80*) ...\n"
    )
