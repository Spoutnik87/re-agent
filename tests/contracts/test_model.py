"""Tests for the ABI contract data model — enums, frozen constraints, edge cases."""

from __future__ import annotations

import pytest

from re_agent.contracts.model import AbiManifest, Architecture, CallingConvention, Symbol


class TestArchitecture:
    def test_values(self) -> None:
        assert Architecture.X86.value == "x86"
        assert Architecture.X64.value == "x64"
        assert Architecture.ARM.value == "arm"
        assert Architecture.ARM64.value == "aarch64"

    def test_membership(self) -> None:
        vals = {m.value for m in Architecture}
        assert "x86" in vals
        assert "mips" not in vals
        assert "sparc" not in vals


class TestCallingConvention:
    def test_values(self) -> None:
        assert CallingConvention.CDECL.value == "cdecl"
        assert CallingConvention.STDCALL.value == "stdcall"
        assert CallingConvention.FASTCALL.value == "fastcall"
        assert CallingConvention.THISCALL.value == "thiscall"
        assert CallingConvention.VECTORCALL.value == "vectorcall"
        assert CallingConvention.SYSTEMV.value == "systemv"

    def test_membership(self) -> None:
        vals = {m.value for m in CallingConvention}
        assert "stdcall" in vals
        assert "pascal" not in vals


class TestSymbol:
    def make(self, **kw: object) -> Symbol:
        defaults: dict = {
            "address": 0x401000,
            "name": "FuncA",
            "signature": "int func_a(int)",
            "calling_convention": CallingConvention.CDECL,
            "output_path": "mod/func_a.cpp",
        }
        defaults.update(kw)
        return Symbol(**defaults)  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        s = self.make()
        with pytest.raises(AttributeError):
            s.name = "Overwrite"  # type: ignore[misc]

    def test_equality(self) -> None:
        s1 = self.make()
        s2 = self.make()
        assert s1 == s2
        assert hash(s1) == hash(s2)

    def test_inequality(self) -> None:
        s1 = self.make(name="Foo", address=0x401000, output_path="foo.cpp")
        s2 = self.make(name="Bar", address=0x402000, output_path="bar.cpp")
        assert s1 != s2

    def test_signature_empty_rejected_by_validation_only(self) -> None:
        """The model itself allows empty signature; validation rejects it."""
        s = self.make(signature="")
        assert s.signature == ""

    def test_output_path_any_string_allowed_by_model(self) -> None:
        """The model does not enforce path rules; validation does."""
        s = self.make(output_path="evil/../../../etc.cpp")
        assert s.output_path == "evil/../../../etc.cpp"


class TestAbiManifest:
    def test_fields(self) -> None:
        sym = Symbol(
            address=0x401000,
            name="FuncA",
            signature="int func_a(int)",
            calling_convention=CallingConvention.CDECL,
            output_path="mod/func_a.cpp",
        )
        manifest = AbiManifest(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=(sym,),
            sha256_hash="abc123def456",
        )
        assert manifest.version == "1.0.0"
        assert manifest.architecture is Architecture.X86
        assert manifest.pointer_size == 4
        assert len(manifest.symbols) == 1
        assert manifest.sha256_hash == "abc123def456"

    def test_frozen(self) -> None:
        sym = Symbol(
            address=0x401000,
            name="F",
            signature="void f()",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        m = AbiManifest(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=(sym,),
            sha256_hash="def456",
        )
        with pytest.raises(AttributeError):
            m.version = "2.0.0"  # type: ignore[misc]

    def test_symbols_tuple_order_preserved(self) -> None:
        """The model itself does not sort — sorting is done by manifest functions."""
        s1 = Symbol(
            address=0x402000,
            name="B",
            signature="void b()",
            calling_convention=CallingConvention.CDECL,
            output_path="b.cpp",
        )
        s2 = Symbol(
            address=0x401000,
            name="A",
            signature="void a()",
            calling_convention=CallingConvention.CDECL,
            output_path="a.cpp",
        )
        m = AbiManifest(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=(s1, s2),
            sha256_hash="",
        )
        assert list(m.symbols) == [s1, s2]
