"""Phase-neutral C++ compilation helpers.

Both the reverse phase (compile-gate) and the build phase (transform
validation) compile generated C++ with the same underlying logic. The
build-phase wrappers in ``re_agent.build.validate.compiler`` delegate here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_msvc_env_cache: dict[str, dict[str, str]] = {}


def _is_msvc(compiler: str) -> bool:
    """Return True when the compiler is Microsoft's cl.exe."""
    name = Path(compiler).name.lower()
    return name in ("cl.exe", "cl")


def _compiler_env(compiler: str) -> dict[str, str]:
    """Return an environment with the compiler's own directory on PATH.

    For MinGW, adds the compiler directory.  For MSVC, locates and runs
    ``vcvars32.bat`` to populate INCLUDE, LIB, and PATH.
    """
    if _is_msvc(compiler):
        return _msvc_env(compiler)
    env = os.environ.copy()
    compiler_dir = str(Path(compiler).parent.resolve())
    env["PATH"] = f"{compiler_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _msvc_env(compiler: str) -> dict[str, str]:
    """Locate vcvars32.bat from the MSVC toolchain and extract its environment."""
    if compiler in _msvc_env_cache:
        return _msvc_env_cache[compiler]
    env = os.environ.copy()
    cl_path = Path(compiler).resolve()
    tools_dir = cl_path.parent
    while tools_dir.name.lower() != "msvc":
        tools_dir = tools_dir.parent
    vcvars = tools_dir.parent.parent / "Auxiliary" / "Build" / "vcvars32.bat"
    if not vcvars.exists():
        return env
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".bat", delete=False, encoding="ascii") as f:
            f.write(f'@echo off\r\ncall "{vcvars}" >nul\r\nset\r\n')
            bat = f.name
        result = subprocess.run(
            ["cmd", "/c", bat],
            capture_output=True,
            text=True,
            timeout=15,
        )
        os.unlink(bat)
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                if key.upper() in ("PATH", "INCLUDE", "LIB", "LIBPATH"):
                    env[key] = value
    except Exception:
        pass
    _msvc_env_cache[compiler] = env
    return env


def _include_args(decls_header: str | os.PathLike[str] | None, *, compiler: str = "") -> list[str]:
    if not decls_header:
        return []
    decls_path = Path(decls_header)
    parent_dir = str(decls_path.parent.resolve())
    if _is_msvc(compiler):
        return ["/FI", str(decls_header), "/I", parent_dir]
    return ["-include", str(decls_header), "-I", parent_dir]


def compile_source(
    source: str,
    compiler: str,
    flags: str,
    *,
    decls_header: str | os.PathLike[str] | None = None,
    include_dirs: list[str | os.PathLike[str]] | None = None,
    timeout: int = 30,
) -> tuple[bool, str]:
    """Compile a single C++ source string. Returns ``(compiles, error_output)``.

    ``decls_header``, when given, is force-included (``-include``) ahead of the
    source so a function referencing externally-defined symbols can still be
    compiled in isolation.

    ``include_dirs``, when given, adds ``-I <dir>`` (GCC) or ``/I <dir>``
    (MSVC) flags for each directory, placed after ``-include`` / ``/FI``.
    """
    flag_list = flags.split()
    include_args = _include_args(decls_header, compiler=compiler)
    extra_includes: list[str] = []
    if include_dirs:
        flag = "/I" if _is_msvc(compiler) else "-I"
        for d in include_dirs:
            extra_includes.extend([flag, str(d)])
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(source.encode("utf-8"))
        result = subprocess.run(
            [compiler, *flag_list, *include_args, *extra_includes, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_compiler_env(compiler),
        )
        if result.returncode == 0:
            return (True, "")
        return (False, result.stderr + result.stdout)
    except FileNotFoundError:
        return (False, f"Compiler not found: {compiler}")
    except subprocess.TimeoutExpired:
        return (False, f"Compilation timed out after {timeout} seconds")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def compile_sources(
    source_files: list[Path],
    compiler: str,
    flags: str,
    *,
    decls_header: str | os.PathLike[str] | None = None,
    include_dirs: list[str | os.PathLike[str]] | None = None,
    timeout: int = 60,
) -> tuple[bool, str]:
    """Compile multiple .cpp files together to catch cross-file errors."""
    cpp_files = [str(f) for f in source_files if Path(f).suffix == ".cpp"]
    if not cpp_files:
        return (True, "")
    flag_list = flags.split()
    include_args = _include_args(decls_header, compiler=compiler)
    extra_includes: list[str] = []
    if include_dirs:
        flag = "/I" if _is_msvc(compiler) else "-I"
        for d in include_dirs:
            extra_includes.extend([flag, str(d)])
    try:
        result = subprocess.run(
            [compiler, *flag_list, *include_args, *extra_includes, *cpp_files],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_compiler_env(compiler),
        )
        if result.returncode == 0:
            return (True, "")
        return (False, result.stderr + result.stdout)
    except FileNotFoundError:
        return (False, f"Compiler not found: {compiler}")
    except subprocess.TimeoutExpired:
        return (False, f"Module compilation timed out after {timeout} seconds")


def _generated_file_flags_with_attributes_demotion(
    base_flags: str,
    compiler: str,
    decls_header: str | os.PathLike[str] | None,
) -> str:
    """Append -Wno-error=attributes for GCC/MinGW generated-file compilation.

    When a concrete ``decls_header`` is present and the compiler is not MSVC,
    append ``-Wno-error=attributes`` to the flags *unless* the flag is already
    present.  This prevents ``-Werror=attributes`` in ``_decls.h`` from masking
    real body errors during generated-file compilation in the transform pipeline.

    MSVC, no-decls, and single-source (``compile_source`` / ``compile_sockets``)
    paths do NOT receive the flag — this helper is called only from
    ``compile_generated_file_set``.
    """
    if not decls_header:
        return base_flags
    if _is_msvc(compiler):
        return base_flags
    if "-Wno-error=attributes" in base_flags.split():
        return base_flags
    return f"{base_flags} -Wno-error=attributes"


def compile_generated_file_set(
    files: list[dict[str, str]],
    target_path: str,
    cfg: Any,
) -> tuple[bool, str]:
    """Compile a generated .cpp with its generated headers available.

    Creates a temporary directory, writes every generated file under its
    ``path`` (preserving directory structure), then compiles only the target
    ``.cpp`` with the temp root added as an include directory so that
    ``#include`` directives against generated headers resolve.

    Args:
        files: List of dicts with ``"path"`` (relative path under temp root)
            and ``"content"`` (file text) keys.
        target_path: Path identifying which file to compile. When none of
            the file entries has an exact matching path, the function falls
            back to compiling the single ``.cpp`` file in the set.
        cfg: Config object with ``output.compiler`` and
            ``output.compiler_flags`` (and optionally ``output.decls_header``).

    Returns:
        ``(True, "")`` on successful compilation; ``(False, error_message)``
        if the target file cannot be found, path traversal is detected, or
        the compiler reports errors.
    """
    compiler = cfg.output.compiler
    flags = cfg.output.compiler_flags

    # Safely extract decls_header — cfg may be a MagicMock in tests.
    # NOTE: isinstance(x, os.PathLike) is NOT used because MagicMock auto-creates
    # __fspath__ on access, making isinstance(mock, os.PathLike) return True on
    # Python 3.12+. We check for concrete types (str | Path) instead.
    decls_header_raw = getattr(cfg.output, "decls_header", None)
    decls_header: str | os.PathLike[str] | None = decls_header_raw if isinstance(decls_header_raw, str | Path) else None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_resolved = Path(tmpdir).resolve()

        # Write every generated file under the temp root.
        for entry in files:
            file_path = entry.get("path", "")
            content = entry.get("content", "")

            # Reject absolute paths (could escape the temp directory).
            if os.path.isabs(file_path):
                return (
                    False,
                    f"Absolute path not allowed in generated file set: {file_path}",
                )

            # Reject path traversal (".." or symlinks escaping tmpdir).
            dest = tmpdir_resolved / file_path
            try:
                dest.resolve().relative_to(tmpdir_resolved)
            except ValueError:
                return (
                    False,
                    f"Path traversal not allowed in generated file set: {file_path}",
                )

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        # Find the target file to compile.
        target_entry: dict[str, str] | None = None
        for entry in files:
            if entry.get("path") == target_path:
                target_entry = entry
                break

        if target_entry is None:
            # Fall back: compile the single .cpp file in the set.
            cpp_entries = [f for f in files if f.get("path", "").endswith(".cpp")]
            if len(cpp_entries) == 0:
                return (False, "No .cpp file found in generated file set")
            if len(cpp_entries) > 1:
                return (
                    False,
                    f"target_path={target_path!r} does not match any file in the "
                    f"generated file set and multiple .cpp files exist",
                )
            target_entry = cpp_entries[0]

        # Collect unique parent directories of generated files so that
        # #include directives using either a subdirectory path (e.g.
        # #include "include/renderer/x.h") OR just the basename (e.g.
        # #include "x.h") resolve against the generated header tree.
        # The temp root is kept first so absolute-from-root includes
        # continue to work (backward compatible).
        header_parents: set[str] = set()
        for entry in files:
            fpath = entry.get("path", "")
            # Only process files that could be #included (headers) or
            # that contribute to the include context.
            parent = os.path.dirname(fpath)
            if parent:
                header_parents.add(str(tmpdir_resolved / parent))
        include_dirs: list[str | os.PathLike[str]] = [tmpdir]
        for p in sorted(header_parents):
            include_dirs.append(p)

        return compile_source(
            target_entry["content"],
            compiler,
            _generated_file_flags_with_attributes_demotion(flags, compiler, decls_header),
            decls_header=decls_header,
            include_dirs=include_dirs,
        )
