"""Generate a forward-declarations header from Ghidra export data."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from re_agent.reverse.core.models import EnumDef, StructDef


def _emit_signatures(signatures: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sig in signatures:
        s = sig.strip().rstrip(";")
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(f"{s};")
    return out


def _emit_struct(s: StructDef) -> str:
    if s.size and s.size > 0:
        return f"struct {s.name} {{ unsigned char _data[{s.size}]; }};"
    return f"struct {s.name};"


def _emit_enum(e: EnumDef) -> str:
    body = ", ".join(f"{v.name} = {v.value}" for v in e.values)
    return f"enum {e.name} {{ {body} }};"


def generate_decls_header(
    signatures: Iterable[str],
    structs: Iterable[StructDef],
    enums: Iterable[EnumDef],
    *,
    globals_map: dict[str, str] | None = None,
) -> str:
    lines: list[str] = [
        "#pragma once",
        "#include <cstdint>",
        "#include <cstdio>",
        "#include <ctime>",
        "#include <windows.h>",
        "",
    ]
    for s in structs:
        lines.append(_emit_struct(s))
    for e in enums:
        lines.append(_emit_enum(e))
    if structs or enums:
        lines.append("")
    if globals_map:
        lines += _emit_extern_globals(globals_map)
    lines += _emit_signatures(signatures)
    return "\n".join(lines) + "\n"


def load_index(index_path: Path) -> dict[str, str]:
    data: dict[str, Any] = json.loads(Path(index_path).read_text(encoding="utf-8"))
    return {addr: entry.get("name", "") for addr, entry in data.items()}


def sanitize_name(name: str) -> str:
    if name.startswith("FID_conflict:"):
        return name.split(":", 1)[1]
    return name


def _normalize_signature(sig: str) -> str:
    """Replace Ghidra pseudo-types with valid C++ equivalents.

    Ghidra signatures use ``undefined``, ``undefined2``, ``undefined4``, etc.
    which are not valid C++ types.  Map them to standard-width integers.
    Also sanitize ``FID_conflict:`` prefixes and MSVC ``@`` decorators.
    """
    sig = sig.strip()
    sig = " " + sig + " "  # pad for reliable token replacement
    for ghidra_type, cpp_type in _GHIDRA_TYPE_MAP:
        sig = sig.replace(ghidra_type, cpp_type)
    sig = sig.replace("FID_conflict:", "FID_conflict_")
    sig = sig.replace("@", "_")
    sig = sig.replace("`", "")
    sig = sig.replace("'", "")
    return sig.strip()


_GHIDRA_TYPE_MAP: list[tuple[str, str]] = [
    (" undefined8 ", " uint64_t "),
    (" undefined4 ", " uint32_t "),
    (" undefined2 ", " uint16_t "),
    (" undefined1 ", " uint8_t "),
    (" undefined ", " uint8_t "),
    ("(undefined ", "(uint8_t "),
    (" longlong ", " int64_t "),
    (" ulonglong ", " uint64_t "),
    (" ulong ", " unsigned long "),
    ("(ulong ", "(unsigned long "),
    (" uchar ", " unsigned char "),
    ("(uchar ", "(unsigned char "),
    (" noreturn ", " [[noreturn]] "),
    ("(noreturn ", "([[noreturn]] "),
    (" byte ", " uint8_t "),
    (" float10 ", " long double "),
    (" code * ", " void * "),
    (" code* ", " void* "),
    ("_func_int *", "int *"),
    ("_func_void *", "void *"),
    ("_func_void_ptr_void_ptr *", "void *"),
    (" uint ", " unsigned int "),
    ("(uint ", "(unsigned int "),
]


def _is_compilable(sig: str) -> bool:
    """Return False for signatures that reference unresolvable Ghidra/MSVC types."""
    return all(token not in sig for token in _UNRESOLVABLE_TYPES)


_UNRESOLVABLE_TYPES = (
    "EHExceptionRecord",
    "EHRegistrationNode",
    "_s_HandlerType",
    "_s_CatchableType",
    "_s_FuncInfo",
    "_s_TryBlockMapEntry",
    "FrameInfo",
    "INTRNCVT_STATUS",
    "PEXCEPTION_RECORD",
    "_LDBL12",
    "_CRT_DOUBLE",
    "LPLC_STRINGS",
    "LCTYPE",
    "_func_4879",
    "_PtFuncCompare",
)


def _read_per_function(exports_dir: Path, address: str) -> dict[str, Any]:
    f = exports_dir / f"{address}.json"
    if not f.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def write_decls_header(cfg: Any) -> Path | None:
    out = getattr(cfg.build.output, "decls_header", None)
    if not out:
        return None
    exports_dir = Path(cfg.build.input.ghidra_exports)
    index_path = exports_dir / "_index.json"
    if not index_path.exists():
        return None

    names = load_index(index_path)
    signatures: list[str] = []
    structs: list[StructDef] = []
    enums: list[EnumDef] = []
    for address in names:
        data = _read_per_function(exports_dir, address)
        sig = data.get("signature")
        if sig:
            norm = _normalize_signature(sig)
            if _is_compilable(norm):
                signatures.append(norm)

    decompiled_dir = getattr(cfg.build.input, "decompiled_dir", None)
    globals_map: dict[str, str] | None = None
    if decompiled_dir:
        globals_map = scan_global_variables(decompiled_dir)

    header = generate_decls_header(signatures, structs, enums, globals_map=globals_map)
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header, encoding="utf-8")
    return out_path


def _extract_declared_names(decls_header: Path) -> set[str]:
    """Extract function names from a forward-declarations header."""
    names: set[str] = set()
    text = decls_header.read_text(encoding="utf-8")
    for m in re.finditer(r"\b(\w+)\s*\(", text):
        names.add(m.group(1))
    return names


def strip_redundant_externs(source: str, decls_path: str | Path) -> str:
    """Remove ``extern`` declarations already provided by the decls header.

    When ``_decls.h`` is force-included during compilation, any ``extern
    <type> <name>(...);`` line in the source becomes redundant and risks
    conflicting with the header's Ghidra-inferred type.
    """
    dpath = Path(decls_path)
    if not dpath.exists():
        return source
    declared = _extract_declared_names(dpath)
    if not declared:
        return source

    # Strip extern function declarations AND bare forward declarations
    # that are already provided by the decls header
    pattern = re.compile(r"^\s*(?:extern\s)?\w[\w\s*&<>:]+\s+(\w+)\s*\([^)]*\)\s*;\s*$", re.MULTILINE)
    lines = source.splitlines()
    result: list[str] = []
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) in declared:
            continue
        result.append(line)
    return "\n".join(result) + "\n"


def scan_global_variables(decompiled_dir: str | Path, *, limit: int = 0) -> dict[str, str]:
    """Scan decompiled .cpp files for undeclared global-variable usage.

    Many decompiled functions reference globals (``DAT_*``, ``PTR_*``,
    ``g_*``, ``pThis``, ``pEntity``, etc.) without an accompanying
    ``extern`` declaration.  This scanner gathers every identifier that
    *looks like* a global (by naming convention) and is used in the code,
    but is neither a standard type, keyword, Win32 symbol, nor a declared
    function.

    Returns a dict of ``{name: best_guessed_type}``.
    """
    global_name_re = re.compile(
        r"\b(DAT_[0-9a-fA-F]{6,8}|PTR_[0-9a-fA-F]{6,8}|"
        r"g_\w+|pThis|pEntity|pContext|pEngine|pManager|pDevice|pRender|"
        r"pD3D|unaff_\w+|in_\w{2,4})\b"
    )
    known_words = frozenset(
        {
            "int",
            "char",
            "float",
            "double",
            "void",
            "bool",
            "short",
            "long",
            "unsigned",
            "signed",
            "const",
            "volatile",
            "static",
            "extern",
            "struct",
            "class",
            "enum",
            "union",
            "typedef",
            "sizeof",
            "if",
            "else",
            "for",
            "while",
            "do",
            "switch",
            "case",
            "break",
            "return",
            "goto",
            "continue",
            "default",
            "auto",
            "register",
            "int8_t",
            "uint8_t",
            "int16_t",
            "uint16_t",
            "int32_t",
            "uint32_t",
            "int64_t",
            "uint64_t",
            "size_t",
            "ptrdiff_t",
            "nullptr",
            "NULL",
            "true",
            "false",
            "HINSTANCE",
            "HWND",
            "HMODULE",
            "HANDLE",
            "DWORD",
            "BOOL",
            "UINT",
            "LPCSTR",
            "LPCWSTR",
            "LPSTR",
            "LPWSTR",
            "WPARAM",
            "LPARAM",
            "LRESULT",
            "HRESULT",
            "PVOID",
            "LPVOID",
            "cdecl",
            "stdcall",
            "fastcall",
            "thiscall",
            "noreturn",
            "WNDCLASSA",
            "RECT",
            "POINT",
            "SIZE",
            "MSG",
            "_exception",
            "_EXCEPTION_POINTERS",
            "EXCEPTION_POINTERS",
            "GetCurrentProcessId",
            "FUN_",
            "operator",
        }
    )
    pointers = frozenset({"pThis", "pEntity", "pContext", "pEngine", "pManager", "pDevice", "pRender", "pD3D"})
    globals_count: dict[str, int] = {}
    cpp_dir = Path(decompiled_dir)
    files = sorted(cpp_dir.glob("*.cpp"))
    if limit > 0:
        files = files[:limit]

    # First pass: count occurrences
    for cpp in files:
        try:
            content = cpp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in global_name_re.finditer(content):
            name = m.group(0)
            if name in known_words:
                continue
            if any(name.startswith(p) for p in ("FUN_", "FID_", "LAB_")):
                continue
            globals_count[name] = globals_count.get(name, 0) + 1

    # Infer types by scanning usage context
    result: dict[str, str] = {}
    for name in sorted(globals_count):
        if globals_count[name] < 2:
            continue
        if name in pointers or name.startswith("p") or name.startswith(("DAT_", "PTR_")):
            result[name] = "unsigned char*"
        elif name.startswith("unaff_") or name.startswith("in_"):
            result[name] = "int"
        else:
            result[name] = "int"
    return result


def _emit_extern_globals(globals_map: dict[str, str]) -> list[str]:
    """Render extern global-variable declarations as header lines."""
    if not globals_map:
        return []
    lines = ["// Extern global variables (auto-collected from decompiled sources)"]
    for name in sorted(globals_map):
        ctype = globals_map[name]
        lines.append(f"extern {ctype} {name};")
    lines.append("")
    return lines
