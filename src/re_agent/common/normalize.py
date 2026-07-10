"""Deterministic (zero-token) normalization of decompiled C++.

Ports the mechanical clean-ups that were previously done by external
PowerShell scripts into the tool itself, so they run automatically on every
reverse-phase output and before every compile check.

Every transform here is **safe and idempotent**: running ``normalize_code``
twice yields the same result as running it once, and no transform changes the
logic of the code — only Ghidra type artefacts, markers, and invalid
characters. Anything that cannot be done safely with a deterministic rule
(e.g. ``void*`` pointer-arithmetic casts) is deliberately left to the LLM
repair pass rather than risked here.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Ghidra base-type -> fixed-width / standard C++ type replacements.
# Order matters: the sized ``undefined<N>`` types must be handled before the
# bare ``undefined`` rule (word boundaries already prevent overlap, but being
# explicit keeps the intent clear).
# ---------------------------------------------------------------------------

_TYPE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bundefined8\b"), "uint64_t"),
    (re.compile(r"\bundefined4\b"), "uint32_t"),
    (re.compile(r"\bundefined2\b"), "uint16_t"),
    (re.compile(r"\bundefined1\b"), "uint8_t"),
    (re.compile(r"\bundefined\b"), "void"),
    (re.compile(r"\buint\b"), "unsigned int"),
    (re.compile(r"\bushort\b"), "unsigned short"),
    (re.compile(r"\buchar\b"), "unsigned char"),
    (re.compile(r"\bulong\b"), "unsigned long"),
)

# Fixed-width type names that require <cstdint>.
_FIXED_WIDTH_RE = re.compile(r"\b(?:u?int(?:8|16|32|64)_t)\b")

# A line that is purely a re-agent reversal marker.
_MARKER_RE = re.compile(r"REVERSED_FUNCTION")

# MSVC pragma that GCC rejects; comment it out instead of deleting (keeps
# provenance and stays idempotent because already-commented lines are skipped).
_PRAGMA_WARNING_RE = re.compile(r"^(\s*)(#pragma\s+warning\b.*)$")

# Unicode artefacts that frequently leak from LLM output and break compilation.
_UNICODE_FIXUPS: tuple[tuple[str, str], ...] = (
    ("﻿", ""),  # BOM
    ("​", ""),  # zero-width space
    (" ", " "),  # non-breaking space
    ("“", '"'),  # left double quote
    ("”", '"'),  # right double quote
    ("‘", "'"),  # left single quote
    ("’", "'"),  # right single quote
    ("–", "-"),  # en dash
    ("—", "-"),  # em dash
)


def _strip_markers(code: str) -> str:
    """Drop any line containing a ``REVERSED_FUNCTION`` marker."""
    return "\n".join(line for line in code.splitlines() if not _MARKER_RE.search(line))


def _replace_types(code: str) -> str:
    for pattern, repl in _TYPE_REPLACEMENTS:
        code = pattern.sub(repl, code)
    return code


def _comment_msvc_pragmas(code: str) -> str:
    out: list[str] = []
    for line in code.splitlines():
        m = _PRAGMA_WARNING_RE.match(line)
        if m:
            out.append(f"{m.group(1)}// {m.group(2)}")
        else:
            out.append(line)
    return "\n".join(out)


def _fix_unicode(code: str) -> str:
    code = code.replace("`", "")
    for bad, good in _UNICODE_FIXUPS:
        code = code.replace(bad, good)
    return code


def _ensure_cstdint(code: str) -> str:
    if not _FIXED_WIDTH_RE.search(code):
        return code
    if re.search(r"#\s*include\s*<cstdint>", code) or re.search(r"#\s*include\s*<stdint\.h>", code):
        return code
    return "#include <cstdint>\n" + code


def normalize_code(code: str) -> str:
    """Apply all deterministic clean-ups to a decompiled C++ string.

    Idempotent: ``normalize_code(normalize_code(x)) == normalize_code(x)``.
    """
    if not code:
        return code
    code = _fix_unicode(code)
    code = _strip_markers(code)
    code = _comment_msvc_pragmas(code)
    code = _replace_types(code)
    code = _ensure_cstdint(code)
    return code
