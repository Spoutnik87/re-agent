"""C++ compilation validation for the build phase.

Thin ``cfg``-based wrappers around the phase-neutral helpers in
``re_agent.common.compiler``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from re_agent.common.compiler import compile_source, compile_sources


def _decls_header(cfg: Any) -> str | None:
    return getattr(cfg.output, "decls_header", None) or None


def compile_check(source: str, cfg: Any) -> tuple[bool, str]:
    """Compile a single C++ source string with the configured compiler."""
    return compile_source(
        source,
        cfg.output.compiler,
        cfg.output.compiler_flags,
        decls_header=_decls_header(cfg),
    )


def compile_module_check(source_files: list[Path], cfg: Any) -> tuple[bool, str]:
    """Compile all .cpp files in a module together in a single invocation.

    Catches cross-file link/type errors that per-file compilation misses.
    """
    return compile_sources(
        source_files,
        cfg.output.compiler,
        cfg.output.compiler_flags,
        decls_header=_decls_header(cfg),
    )
