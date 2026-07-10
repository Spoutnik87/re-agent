"""Tests for the decls-header generator."""

import json
from pathlib import Path

from re_agent.build.analyze.decls_generator import (
    _normalize_signature,
    generate_decls_header,
    load_index,
    sanitize_name,
    strip_redundant_externs,
    write_decls_header,
)
from re_agent.reverse.core.models import EnumDef, EnumValue, StructDef


def test_function_prototypes_are_emitted_with_semicolons():
    header = generate_decls_header(
        signatures=["void CFoo::Bar(CFoo *this, int x)", "int baz(void)"],
        structs=[],
        enums=[],
    )
    assert "#pragma once" in header
    assert "void CFoo::Bar(CFoo *this, int x);" in header
    assert "int baz(void);" in header


def test_duplicate_signatures_collapse():
    header = generate_decls_header(signatures=["int f(void)", "int f(void)"], structs=[], enums=[])
    assert header.count("int f(void);") == 1


def test_structs_emitted_with_size_preserving_padding():
    header = generate_decls_header(
        signatures=[],
        structs=[StructDef(name="CFoo", size=16, fields=[])],
        enums=[],
    )
    assert "struct CFoo { unsigned char _data[16]; };" in header


def test_zero_size_struct_is_forward_declared_only():
    header = generate_decls_header(signatures=[], structs=[StructDef(name="Opaque", size=0, fields=[])], enums=[])
    assert "struct Opaque;" in header
    assert "_data" not in header


def test_enums_emitted_with_values():
    header = generate_decls_header(
        signatures=[],
        structs=[],
        enums=[EnumDef(name="eState", values=[EnumValue("OFF", 0), EnumValue("ON", 1)])],
    )
    assert "enum eState { OFF = 0, ON = 1 };" in header


def test_load_index_returns_address_to_name_map(tmp_path: Path):
    idx = tmp_path / "_index.json"
    idx.write_text(
        json.dumps(
            {
                "00b3657b": {"name": "FUN_00b3657b", "address": "00b3657b", "num_callers": 2, "num_callees": 1},
                "00b365c0": {"name": "_ValidateExecute", "address": "00b365c0", "num_callers": 2, "num_callees": 1},
            }
        ),
        encoding="utf-8",
    )
    names = load_index(idx)
    assert names["00b3657b"] == "FUN_00b3657b"
    assert names["00b365c0"] == "_ValidateExecute"


def test_sanitize_name_drops_fid_conflict_prefix():
    assert sanitize_name("FID_conflict:_ValidateRead") == "_ValidateRead"
    assert sanitize_name("FUN_00b3657b") == "FUN_00b3657b"


def test_write_decls_header_pulls_signatures_from_per_function_files(tmp_path: Path):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "_index.json").write_text(
        json.dumps(
            {
                "00000001": {"name": "f", "address": "00000001", "num_callers": 1, "num_callees": 0},
            }
        ),
        encoding="utf-8",
    )
    (exports / "00000001.json").write_text(json.dumps({"signature": "int f(void)"}), encoding="utf-8")
    out = tmp_path / "_decls.h"

    class _Cfg:
        class build:
            class input:
                ghidra_exports = str(exports)

            class output:
                decls_header = str(out)

    write_decls_header(_Cfg())
    assert "int f(void);" in out.read_text(encoding="utf-8")


def test_normalize_signature_replaces_ghidra_types():
    assert _normalize_signature("undefined FUN_00401000(void)") == "uint8_t FUN_00401000(void)"
    assert _normalize_signature("undefined4 f(int x)") == "uint32_t f(int x)"
    assert _normalize_signature("int baz(void)") == "int baz(void)"
    assert _normalize_signature("longlong g()") == "int64_t g()"


def test_normalize_signature_sanitizes_fid_conflict():
    assert (
        _normalize_signature("uint8_t FID_conflict:_ValidateRead(void)") == "uint8_t FID_conflict__ValidateRead(void)"
    )


def test_normalize_signature_sanitizes_at_signs():
    assert _normalize_signature("uint8_t Catch@00b092c3(void)") == "uint8_t Catch_00b092c3(void)"


def test_normalize_signature_strips_backticks():
    assert (
        _normalize_signature("void __stdcall `vector_constructor_iterator'(void * param_1)")
        == "void __stdcall vector_constructor_iterator(void * param_1)"
    )


def test_normalize_signature_replaces_uint_standalone():
    assert _normalize_signature("int __cdecl f(uint param_1)") == "int __cdecl f(unsigned int param_1)"
    assert _normalize_signature("uint32_t x(uint y)") == "uint32_t x(unsigned int y)"


def test_strip_redundant_externs_removes_declared_names(tmp_path: Path):
    header = tmp_path / "decls.h"
    header.write_text("uint8_t FUN_00429550(void);\nint _ValidateExecute(int *p);\n")

    source = (
        "#include <stdint.h>\n"
        "extern void FUN_00429550();\n"
        "extern int _ValidateExecute(int *p);\n"
        "extern float * some_other(void);\n"
        "void test() { FUN_00429550(); }\n"
    )
    result = strip_redundant_externs(source, header)
    assert "extern void FUN_00429550();" not in result
    assert "extern int _ValidateExecute" not in result
    assert "extern float * some_other" in result
    assert "void test()" in result
