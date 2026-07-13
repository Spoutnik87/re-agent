"""Tests for validation functions — every oracle blocker is covered."""

from __future__ import annotations

import pytest

from re_agent.contracts.model import Architecture, CallingConvention, Symbol
from re_agent.contracts.validation import (
    build_symbol_from_dict,
    validate_address,
    validate_architecture,
    validate_calling_convention,
    validate_no_unknown_keys,
    validate_pointer_size,
    validate_relative_cpp_path,
    validate_signature,
    validate_symbol_name,
    validate_symbols_nonempty,
    validate_symbols_unique,
    validate_version,
)

# ===================================================================
# validate_version
# ===================================================================


class TestValidateVersion:
    def test_valid(self) -> None:
        assert validate_version("1.0.0") == "1.0.0"
        assert validate_version("0.1.0") == "0.1.0"
        assert validate_version("2.0.0-alpha") == "2.0.0-alpha"
        assert validate_version("1.2.3+build.42") == "1.2.3+build.42"
        assert validate_version("3.4.5-rc.1") == "3.4.5-rc.1"

    def test_invalid(self) -> None:
        for bad in ("", "1.0", "1", "abc", "1.0.0.0", "v1.0.0", None, 42):
            with pytest.raises(ValueError, match="Invalid version"):
                validate_version(bad)  # type: ignore[arg-type]


# ===================================================================
# validate_architecture
# ===================================================================


class TestValidateArchitecture:
    def test_valid(self) -> None:
        assert validate_architecture("x86") is Architecture.X86
        assert validate_architecture("x64") is Architecture.X64
        assert validate_architecture("arm") is Architecture.ARM
        assert validate_architecture("aarch64") is Architecture.ARM64

    def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="Unknown architecture"):
            validate_architecture("mips")
        with pytest.raises(ValueError, match="Unknown architecture"):
            validate_architecture("")


# ===================================================================
# validate_pointer_size
# ===================================================================


class TestValidatePointerSize:
    def test_x86_4(self) -> None:
        assert validate_pointer_size(Architecture.X86, 4) == 4

    def test_x64_8(self) -> None:
        assert validate_pointer_size(Architecture.X64, 8) == 8

    def test_arm_4(self) -> None:
        assert validate_pointer_size(Architecture.ARM, 4) == 4

    def test_arm64_8(self) -> None:
        assert validate_pointer_size(Architecture.ARM64, 8) == 8

    @pytest.mark.parametrize(
        "arch, bad_size",
        [
            (Architecture.X86, 8),
            (Architecture.X64, 4),
            (Architecture.ARM, 8),
            (Architecture.ARM64, 4),
        ],
    )
    def test_inconsistent_rejected(self, arch: Architecture, bad_size: int) -> None:
        with pytest.raises(ValueError, match="inconsistent"):
            validate_pointer_size(arch, bad_size)

    def test_non_int_rejected(self) -> None:
        with pytest.raises(ValueError, match="pointer_size must be an int"):
            validate_pointer_size(Architecture.X86, "4")  # type: ignore[arg-type]

    def test_bool_rejected(self) -> None:
        with pytest.raises(ValueError, match="pointer_size must be an int"):
            validate_pointer_size(Architecture.X86, True)  # type: ignore[arg-type]


# ===================================================================
# validate_relative_cpp_path
# ===================================================================


class TestValidateRelativeCppPath:
    def test_valid_simple(self) -> None:
        assert validate_relative_cpp_path("foo.cpp") == "foo.cpp"

    def test_valid_subdir(self) -> None:
        assert validate_relative_cpp_path("sub/dir/func.cpp") == "sub/dir/func.cpp"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            validate_relative_cpp_path("")

    def test_no_cpp_extension(self) -> None:
        with pytest.raises(ValueError, match="must end with .cpp"):
            validate_relative_cpp_path("foo.c")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValueError, match="backslash"):
            validate_relative_cpp_path("sub\\func.cpp")

    def test_dos_drive_rejected(self) -> None:
        with pytest.raises(ValueError, match="DOS drive"):
            validate_relative_cpp_path("C:func.cpp")

    def test_absolute_unix_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be relative"):
            validate_relative_cpp_path("/absolute/path.cpp")

    def test_absolute_windows_style_rejected(self) -> None:
        with pytest.raises(ValueError, match="backslash|absolute"):
            validate_relative_cpp_path("C:\\absolute\\path.cpp")

    def test_dot_component_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.' components"):
            validate_relative_cpp_path("./func.cpp")

    def test_dotdot_component_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.\.' components"):
            validate_relative_cpp_path("../func.cpp")

    def test_dotdot_deep_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.\.' components"):
            validate_relative_cpp_path("sub/../../func.cpp")

    def test_dot_in_middle_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.' components"):
            validate_relative_cpp_path("sub/./func.cpp")

    def test_null_byte_rejected(self) -> None:
        with pytest.raises(ValueError, match="null bytes"):
            validate_relative_cpp_path("sub\0func.cpp")

    def test_unc_rejected(self) -> None:
        with pytest.raises(ValueError, match="UNC"):
            validate_relative_cpp_path("//server/share/func.cpp")

    def test_empty_component_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty components"):
            validate_relative_cpp_path("foo//bar.cpp")


# ===================================================================
# validate_address
# ===================================================================


class TestValidateAddress:
    def test_valid_32bit(self) -> None:
        assert validate_address(0, 4) == 0
        assert validate_address(0xFFFFFFFF, 4) == 0xFFFFFFFF
        assert validate_address(0x401000, 4) == 0x401000

    def test_valid_64bit(self) -> None:
        assert validate_address(0, 8) == 0
        assert validate_address(0x7FFFFFFFFFFFFFFF, 8) == 0x7FFFFFFFFFFFFFFF

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            validate_address(-1, 4)

    def test_exceeds_32bit(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            validate_address(0x1_0000_0000, 4)

    def test_exceeds_64bit(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            validate_address(1 << 64, 8)

    def test_bool_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be a bool"):
            validate_address(True, 4)  # type: ignore[arg-type]

    def test_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            validate_address("0x401000", 4)  # type: ignore[arg-type]

    def test_float_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            validate_address(4198400.0, 4)  # type: ignore[arg-type]


# ===================================================================
# validate_symbol_name
# ===================================================================


class TestValidateSymbolName:
    def test_valid(self) -> None:
        assert validate_symbol_name("CreateFile") == "CreateFile"
        assert validate_symbol_name("_ZN6Entity8DoStuffEv") == "_ZN6Entity8DoStuffEv"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_symbol_name("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_symbol_name("   ")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_symbol_name(None)  # type: ignore[arg-type]


# ===================================================================
# validate_signature
# ===================================================================


class TestValidateSignature:
    def test_valid(self) -> None:
        assert validate_signature("void foo(int)") == "void foo(int)"
        assert (
            validate_signature("HANDLE WINAPI CreateFileA(LPCSTR, DWORD, ...)")
            == "HANDLE WINAPI CreateFileA(LPCSTR, DWORD, ...)"
        )

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_signature("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_signature("   ")


# ===================================================================
# validate_calling_convention
# ===================================================================


class TestValidateCallingConvention:
    def test_valid(self) -> None:
        assert validate_calling_convention("cdecl") is CallingConvention.CDECL
        assert validate_calling_convention("stdcall") is CallingConvention.STDCALL
        assert validate_calling_convention("fastcall") is CallingConvention.FASTCALL
        assert validate_calling_convention("thiscall") is CallingConvention.THISCALL
        assert validate_calling_convention("vectorcall") is CallingConvention.VECTORCALL
        assert validate_calling_convention("systemv") is CallingConvention.SYSTEMV

    def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="Unknown calling convention"):
            validate_calling_convention("pascal")


# ===================================================================
# validate_symbols_nonempty
# ===================================================================


class TestValidateSymbolsNonempty:
    def test_nonempty(self) -> None:
        s = Symbol(
            address=0x401000,
            name="F",
            signature="void f()",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        validate_symbols_nonempty([s])  # should not raise

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one symbol"):
            validate_symbols_nonempty([])


# ===================================================================
# validate_symbols_unique
# ===================================================================


class TestValidateSymbolsUnique:
    def make(self, **kw: object) -> Symbol:
        defaults: dict = {
            "address": 0x401000,
            "name": "F",
            "signature": "void f()",
            "calling_convention": CallingConvention.CDECL,
            "output_path": "f.cpp",
        }
        defaults.update(kw)
        return Symbol(**defaults)  # type: ignore[arg-type]

    def test_unique_pass(self) -> None:
        s1 = self.make(address=0x401000, name="A", output_path="a.cpp")
        s2 = self.make(address=0x402000, name="B", output_path="b.cpp")
        validate_symbols_unique([s1, s2])  # should not raise

    def test_duplicate_address_rejected(self) -> None:
        """Two symbols with the same address are rejected even if names differ."""
        s1 = self.make(address=0x401000, name="A", output_path="a.cpp")
        s2 = self.make(address=0x401000, name="B", output_path="b.cpp")
        with pytest.raises(ValueError, match="Duplicate address"):
            validate_symbols_unique([s1, s2])

    def test_duplicate_address_name_rejected(self) -> None:
        s1 = self.make(address=0x401000, name="A", output_path="a.cpp")
        s2 = self.make(address=0x401000, name="A", output_path="b.cpp")
        with pytest.raises(ValueError, match="Duplicate address"):
            validate_symbols_unique([s1, s2])

    def test_duplicate_output_path_rejected(self) -> None:
        s1 = self.make(address=0x401000, name="A", output_path="same.cpp")
        s2 = self.make(address=0x402000, name="B", output_path="same.cpp")
        with pytest.raises(ValueError, match="Duplicate output_path"):
            validate_symbols_unique([s1, s2])

    def test_same_address_different_name_rejected(self) -> None:
        """Address alone identifies a target, irrespective of aliases."""
        s1 = self.make(address=0x401000, name="A", output_path="a.cpp")
        s2 = self.make(address=0x401000, name="B", output_path="b.cpp")
        with pytest.raises(ValueError, match="Duplicate address"):
            validate_symbols_unique([s1, s2])


# ===================================================================
# validate_no_unknown_keys
# ===================================================================


class TestValidateNoUnknownKeys:
    KNOWN = frozenset({"a", "b"})

    def test_no_extra(self) -> None:
        validate_no_unknown_keys({"a": 1, "b": 2}, self.KNOWN, "test")  # ok

    def test_extra_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown keys in test"):
            validate_no_unknown_keys({"a": 1, "c": 3}, self.KNOWN, "test")


# ===================================================================
# build_symbol_from_dict
# ===================================================================


class TestBuildSymbolFromDict:
    def test_valid(self) -> None:
        s = build_symbol_from_dict(
            {
                "address": 0x401000,
                "name": "FuncA",
                "signature": "int func_a(int)",
                "calling_convention": "cdecl",
                "output_path": "mod/func_a.cpp",
            },
            pointer_size=4,
        )
        assert isinstance(s, Symbol)
        assert s.address == 0x401000
        assert s.name == "FuncA"
        assert s.calling_convention is CallingConvention.CDECL
        assert s.output_path == "mod/func_a.cpp"

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown keys in symbol"):
            build_symbol_from_dict(
                {
                    "address": 0x401000,
                    "name": "F",
                    "signature": "void f()",
                    "calling_convention": "cdecl",
                    "output_path": "f.cpp",
                    "extra_key": "bad",
                },
                pointer_size=4,
            )

    def test_bool_address_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be a bool"):
            build_symbol_from_dict(
                {
                    "address": True,
                    "name": "F",
                    "signature": "void f()",
                    "calling_convention": "cdecl",
                    "output_path": "f.cpp",
                },
                pointer_size=4,
            )

    def test_empty_signature_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            build_symbol_from_dict(
                {
                    "address": 0x401000,
                    "name": "F",
                    "signature": "",
                    "calling_convention": "cdecl",
                    "output_path": "f.cpp",
                },
                pointer_size=4,
            )

    def test_address_exceeds_width(self) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            build_symbol_from_dict(
                {
                    "address": 0x1_0000_0000,
                    "name": "F",
                    "signature": "void f()",
                    "calling_convention": "cdecl",
                    "output_path": "f.cpp",
                },
                pointer_size=4,
            )
