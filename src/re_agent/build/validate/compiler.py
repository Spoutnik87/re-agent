"""C++ compilation validation using GCC."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _compiler_env(compiler: str) -> dict[str, str]:
    env = os.environ.copy()
    compiler_dir = str(Path(compiler).parent.resolve())
    env["PATH"] = f"{compiler_dir};{env['PATH']}"
    return env


def compile_check(source: str, cfg: Any) -> tuple[bool, str]:
    """Compile a single C++ source string with GCC. Returns (compiles, error_output)."""
    flags = cfg.output.compiler_flags.split()
    try:
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(source.encode("utf-8"))
        result = subprocess.run(
            [cfg.output.compiler, *flags, tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            env=_compiler_env(cfg.output.compiler),
        )
        if result.returncode == 0:
            return (True, "")
        return (False, result.stderr + result.stdout)
    except FileNotFoundError:
        return (False, f"Compiler not found: {cfg.output.compiler}")
    except subprocess.TimeoutExpired:
        return (False, "Compilation timed out after 30 seconds")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def compile_module_check(source_files: list[Path], cfg: Any) -> tuple[bool, str]:
    """Compile all .cpp files in a module together. Returns (compiles, error_output)."""
    errors: list[str] = []
    for src in source_files:
        if src.suffix != ".cpp":
            continue
        source_text = src.read_text(encoding="utf-8")
        ok, err = compile_check(source_text, cfg)
        if not ok:
            errors.append(f"File: {src}\n{err}")
    if errors:
        return (False, "\n".join(errors))
    return (True, "")
