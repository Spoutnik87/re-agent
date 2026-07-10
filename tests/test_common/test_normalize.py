from __future__ import annotations

from re_agent.common.normalize import normalize_code


def test_undefined_sized_types_become_fixed_width() -> None:
    code = "undefined4 a; undefined2 b; undefined1 c; undefined8 d;"
    out = normalize_code(code)
    assert "uint32_t a;" in out
    assert "uint16_t b;" in out
    assert "uint8_t c;" in out
    assert "uint64_t d;" in out
    assert "undefined" not in out


def test_bare_undefined_becomes_void() -> None:
    out = normalize_code("undefined foo(void) { return; }")
    assert out.startswith("void foo(void)") or "void foo(void)" in out
    assert "undefined" not in out


def test_uint_family_expanded() -> None:
    out = normalize_code("uint x; ushort y; uchar z; ulong w;")
    assert "unsigned int x;" in out
    assert "unsigned short y;" in out
    assert "unsigned char z;" in out
    assert "unsigned long w;" in out


def test_uint_does_not_mangle_fixed_width_names() -> None:
    # \buint\b must not touch uint32_t / uintptr_t
    out = normalize_code("uint32_t a; uintptr_t b;")
    assert "uint32_t a;" in out
    assert "uintptr_t b;" in out


def test_cstdint_added_when_fixed_width_present() -> None:
    out = normalize_code("undefined4 x;")
    assert out.splitlines()[0] == "#include <cstdint>"


def test_cstdint_not_duplicated() -> None:
    out = normalize_code("#include <cstdint>\nundefined4 x;")
    assert out.count("#include <cstdint>") == 1


def test_cstdint_not_added_without_fixed_width() -> None:
    out = normalize_code("int main() { return 0; }")
    assert "cstdint" not in out


def test_reversed_function_marker_removed() -> None:
    code = "// REVERSED_FUNCTION: 0x401000\nint f() { return 1; }"
    out = normalize_code(code)
    assert "REVERSED_FUNCTION" not in out
    assert "int f() { return 1; }" in out


def test_msvc_pragma_commented_out() -> None:
    out = normalize_code("#pragma warning(disable: 4101)\nint x;")
    assert "// #pragma warning(disable: 4101)" in out


def test_backticks_and_smart_quotes_stripped() -> None:
    out = normalize_code("const char* s = “hi”; // `note`")
    assert "`" not in out
    assert "“" not in out and "”" not in out
    assert '"hi"' in out


def test_normalize_is_idempotent() -> None:
    code = "// REVERSED_FUNCTION: 0x1\n#pragma warning(disable: 1)\nundefined4 g(uint a, ushort b) { return a + b; }\n"
    once = normalize_code(code)
    twice = normalize_code(once)
    assert once == twice


def test_empty_input_is_safe() -> None:
    assert normalize_code("") == ""
