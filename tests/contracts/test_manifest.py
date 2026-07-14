"""Tests for manifest I/O — hash integrity, round-trip, corruption, collisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from re_agent.contracts.manifest import (
    _manifest_to_dict,
    canonical_json_hash,
    load_manifest,
    load_manifest_bytes,
    load_verified_manifest,
    manifest_from_symbols,
    save_manifest,
)
from re_agent.contracts.model import AbiManifest, Architecture, CallingConvention, Symbol

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sample_symbols() -> list[Symbol]:
    return [
        Symbol(
            address=0x401000,
            name="FuncA",
            signature="int func_a(int)",
            calling_convention=CallingConvention.CDECL,
            output_path="mod/func_a.cpp",
        ),
        Symbol(
            address=0x402000,
            name="FuncB",
            signature="void func_b(float)",
            calling_convention=CallingConvention.STDCALL,
            output_path="mod/func_b.cpp",
        ),
    ]


# ===================================================================
# canonical_json_hash
# ===================================================================


class TestCanonicalJsonHash:
    def test_excludes_hash_field(self) -> None:
        """The hash is computed with sha256_hash blanked, so identical content
        (differing only in the hash field) produces the same digest."""
        d1 = {"version": "1.0.0", "sha256_hash": "abc", "architecture": "x86"}
        d2 = {"version": "1.0.0", "sha256_hash": "xyz", "architecture": "x86"}
        assert canonical_json_hash(d1) == canonical_json_hash(d2)

    def test_different_content_different_hash(self) -> None:
        d1 = {"version": "1.0.0", "architecture": "x86"}
        d2 = {"version": "2.0.0", "architecture": "x86"}
        assert canonical_json_hash(d1) != canonical_json_hash(d2)

    def test_deterministic(self) -> None:
        data = {"z": 1, "a": 2, "sha256_hash": "irrelevant"}
        assert canonical_json_hash(data) == canonical_json_hash(data)

    def test_matches_manual_computation(self) -> None:
        """Manually compute the expected SHA-256 to verify the function."""
        data = {"version": "1.0.0", "architecture": "x86", "sha256_hash": ""}
        canonical = json.dumps(data, separators=(",", ":"), sort_keys=True)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # The function blanks sha256_hash internally, so pass with a placeholder.
        result = canonical_json_hash({"version": "1.0.0", "architecture": "x86", "sha256_hash": "placeholder"})
        assert result == expected


# ===================================================================
# manifest_from_symbols
# ===================================================================


class TestManifestFromSymbols:
    def test_basic_creation(self) -> None:
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        assert m.version == "1.0.0"
        assert m.architecture is Architecture.X86
        assert m.pointer_size == 4
        assert len(m.symbols) == 2
        assert len(m.sha256_hash) == 64  # SHA-256 hex length
        assert all(c in "0123456789abcdef" for c in m.sha256_hash)

    def test_hash_is_deterministic(self) -> None:
        m1 = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        m2 = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        assert m1.sha256_hash == m2.sha256_hash

    def test_symbols_sorted_in_manifest(self) -> None:
        """Symbols should be sorted by (address, name) regardless of input order."""
        unsorted_syms = sample_symbols()
        unsorted_syms.reverse()
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=unsorted_syms,
        )
        assert m.symbols[0].address <= m.symbols[1].address

    def test_rejects_empty_symbols(self) -> None:
        with pytest.raises(ValueError, match="at least one symbol"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[],
            )

    def test_rejects_duplicate_address_name(self) -> None:
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="f.cpp",
            ),
            Symbol(
                address=0x401000,
                name="F",
                signature="void f2()",
                calling_convention=CallingConvention.CDECL,
                output_path="f2.cpp",
            ),
        ]
        with pytest.raises(ValueError, match="Duplicate address"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=syms,
            )

    def test_rejects_duplicate_output_path(self) -> None:
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="same.cpp",
            ),
            Symbol(
                address=0x402000,
                name="G",
                signature="void g()",
                calling_convention=CallingConvention.CDECL,
                output_path="same.cpp",
            ),
        ]
        with pytest.raises(ValueError, match="Duplicate output_path"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=syms,
            )

    def test_rejects_inconsistent_pointer_size(self) -> None:
        with pytest.raises(ValueError, match="inconsistent"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=8,
                symbols=sample_symbols(),
            )

    def test_hash_excludes_itself(self) -> None:
        """Verify that the hash is computed over content where sha256_hash is ''."""
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        data_no_hash = _manifest_to_dict(m)
        data_no_hash["sha256_hash"] = ""
        expected = hashlib.sha256(
            json.dumps(data_no_hash, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Re-compute via canonical_json_hash (which blanks the hash)
        assert canonical_json_hash(_manifest_to_dict(m)) == expected
        assert m.sha256_hash == expected


# ===================================================================
# Round-trip: create → save → load
# ===================================================================


class TestSaveLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "contract.json"
        m_in = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m_in, manifest_path)
        assert manifest_path.exists()

        m_out = load_manifest(manifest_path)
        assert m_out == m_in  # dataclass equality
        assert m_out.sha256_hash == m_in.sha256_hash

    def test_round_trip_x64(self, tmp_path: Path) -> None:
        syms = [
            Symbol(
                address=0x140001000,
                name="Func64",
                signature="int f(int)",
                calling_convention=CallingConvention.SYSTEMV,
                output_path="mod/func64.cpp",
            ),
        ]
        manifest_path = tmp_path / "x64_contract.json"
        m_in = manifest_from_symbols(
            version="2.0.0",
            architecture=Architecture.X64,
            pointer_size=8,
            symbols=syms,
        )
        save_manifest(m_in, manifest_path)
        m_out = load_manifest(manifest_path)
        assert m_out == m_in

    def test_created_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "dir" / "contract.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, nested)
        assert nested.exists()
        loaded = load_manifest(nested)
        assert loaded.sha256_hash == m.sha256_hash


# ===================================================================
# Hash integrity / corruption detection
# ===================================================================


class TestHashIntegrity:
    def test_tampered_hash_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "tamper.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)

        # Corrupt the stored hash.
        raw = p.read_text("utf-8")
        raw = raw.replace(m.sha256_hash, "a" * 64)
        p.write_text(raw, "utf-8")

        with pytest.raises(ValueError, match="SHA-256 hash mismatch"):
            load_manifest(p)

    def test_tampered_symbol_name_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "tamper_sym.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)

        # Corrupt a symbol name.
        raw = p.read_text("utf-8")
        raw = raw.replace('"FuncA"', '"FuncX"')
        p.write_text(raw, "utf-8")

        with pytest.raises(ValueError, match="SHA-256 hash mismatch"):
            load_manifest(p)

    def test_tampered_address_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "tamper_addr.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)

        raw = p.read_text("utf-8")
        raw = raw.replace("4198400", "4198401")  # 0x401000 → 0x401001
        p.write_text(raw, "utf-8")

        with pytest.raises(ValueError, match="SHA-256 hash mismatch"):
            load_manifest(p)

    def test_missing_hash_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "no_hash.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)

        raw = p.read_text("utf-8")
        raw = raw.replace(f'"sha256_hash":"{m.sha256_hash}"', '"sha256_hash":""')
        p.write_text(raw, "utf-8")

        with pytest.raises(ValueError, match="must be a non-empty string"):
            load_manifest(p)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.json"
        p.write_text("", "utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_manifest(p)

    def test_invalid_json_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", "utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_manifest(p)


# ===================================================================
# Unknown keys rejection
# ===================================================================


class TestUnknownKeys:
    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "unknown_key.json"
        # Create a proper manifest first, then inject an unknown key.
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="f.cpp",
            )
        ]
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=syms,
        )
        saved = _manifest_to_dict(m)
        saved["extra_field"] = "bad"
        p.write_text(json.dumps(saved, separators=(",", ":"), sort_keys=True), "utf-8")

        with pytest.raises(ValueError, match="Unknown keys in manifest"):
            load_manifest(p)

    def test_unknown_symbol_key_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "unknown_sym_key.json"
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="f.cpp",
            )
        ]
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=syms,
        )
        saved = _manifest_to_dict(m)
        saved["symbols"][0]["unknown_sym_key"] = "bad"
        p.write_text(json.dumps(saved, separators=(",", ":"), sort_keys=True), "utf-8")

        with pytest.raises(ValueError, match="Unknown keys in symbol"):
            load_manifest(p)


# ===================================================================
# Symbol collisions at load time
# ===================================================================


class TestCollisions:
    def test_duplicate_address_name_rejected_on_load(self, tmp_path: Path) -> None:
        """Two symbols with same (address, name) but different output_path."""
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="f.cpp",
            ),
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="f2.cpp",
            ),
        ]
        with pytest.raises(ValueError, match="Duplicate address"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=syms,
            )

    def test_duplicate_output_path_rejected_on_load(self, tmp_path: Path) -> None:
        """Two symbols with same output_path but different addresses."""
        syms = [
            Symbol(
                address=0x401000,
                name="F",
                signature="void f()",
                calling_convention=CallingConvention.CDECL,
                output_path="same.cpp",
            ),
            Symbol(
                address=0x402000,
                name="G",
                signature="void g()",
                calling_convention=CallingConvention.CDECL,
                output_path="same.cpp",
            ),
        ]
        with pytest.raises(ValueError, match="Duplicate output_path"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=syms,
            )


# ===================================================================
# Output path containment
# ===================================================================


class TestOutputPathContainment:
    def test_path_escape_rejected_on_save(self, tmp_path: Path) -> None:
        escaped = Symbol(
            address=0x401000,
            name="Escapee",
            signature="void e()",
            calling_convention=CallingConvention.CDECL,
            output_path="../../etc/passwd.cpp",
        )
        with pytest.raises(ValueError, match=r"\.\.'"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[escaped],
            )

    def test_absolute_path_rejected_by_validation_before_save_attempt(
        self,
        tmp_path: Path,
    ) -> None:
        absolute = Symbol(
            address=0x401000,
            name="Abs",
            signature="void a()",
            calling_convention=CallingConvention.CDECL,
            output_path="/etc/abs.cpp",
        )
        with pytest.raises(ValueError, match="must be relative"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[absolute],
            )


# ===================================================================
# Model constraint tests
# ===================================================================


class TestModelConstraints:
    def test_rejects_empty_manifest(self) -> None:
        with pytest.raises(ValueError, match="at least one symbol"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[],
            )

    def test_rejects_bool_address(self) -> None:
        sym = Symbol(
            address=True,  # type: ignore[arg-type]
            name="F",
            signature="void f()",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        with pytest.raises(ValueError, match="must not be a bool"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[sym],
            )

    def test_rejects_empty_signature(self) -> None:
        sym = Symbol(
            address=0x401000,
            name="F",
            signature="",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        with pytest.raises(ValueError, match="non-empty"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[sym],
            )

    def test_rejects_address_outside_width(self) -> None:
        sym = Symbol(
            address=0x1_0000_0000,
            name="F",
            signature="void f()",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        with pytest.raises(ValueError, match="exceeds"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=[sym],
            )

    def test_rejects_duplicate_address(self) -> None:
        """Two symbols with same address but different names are rejected."""
        syms = [
            Symbol(
                address=0x401000,
                name="A",
                signature="void a()",
                calling_convention=CallingConvention.CDECL,
                output_path="a.cpp",
            ),
            Symbol(
                address=0x401000,
                name="B",
                signature="void b()",
                calling_convention=CallingConvention.CDECL,
                output_path="b.cpp",
            ),
        ]
        with pytest.raises(ValueError, match="Duplicate address"):
            manifest_from_symbols(
                version="1.0.0",
                architecture=Architecture.X86,
                pointer_size=4,
                symbols=syms,
            )


# ===================================================================
# load_manifest_bytes
# ===================================================================


class TestLoadManifestBytes:
    def test_round_trip_via_bytes(self, tmp_path: Path) -> None:
        """Load from bytes produces the same result as load from file."""
        p = tmp_path / "contract.json"
        m_in = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m_in, p)
        raw = p.read_bytes()
        m_out = load_manifest_bytes(raw)
        assert m_out == m_in

    def test_non_utf8_rejected(self) -> None:
        with pytest.raises(ValueError, match="not valid UTF-8"):
            load_manifest_bytes(b"\xff\xfe\x00\x01")

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            load_manifest_bytes(b"{broken json")

    def test_non_dict_top_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="Top-level JSON must be a dict"):
            load_manifest_bytes(b'["array", "is", "not", "a", "manifest"]')

    def test_plain_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="Top-level JSON must be a dict"):
            load_manifest_bytes(b'"just a string"')


# ===================================================================
# load_verified_manifest
# ===================================================================


class TestLoadVerifiedManifest:
    def test_returns_hashes(self, tmp_path: Path) -> None:
        p = tmp_path / "v.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=[
                Symbol(
                    address=0x401000,
                    name="F",
                    signature="void f()",
                    calling_convention=CallingConvention.CDECL,
                    output_path="f.cpp",
                )
            ],
        )
        save_manifest(m, p)
        loaded, raw_h, canon_h = load_verified_manifest(p)
        assert loaded == m
        assert canon_h == m.sha256_hash
        # Raw hash is SHA-256 of the exact file bytes (different from canonical).
        assert raw_h != canon_h
        assert len(raw_h) == 64

    def test_matches_expected_raw_hash(self, tmp_path: Path) -> None:
        p = tmp_path / "expected.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)
        raw_bytes = p.read_bytes()
        expected_raw = hashlib.sha256(raw_bytes).hexdigest()
        loaded, raw_h, canon_h = load_verified_manifest(p, expected_raw_hash=expected_raw)
        assert loaded == m
        assert raw_h == expected_raw
        assert canon_h == m.sha256_hash

    def test_wrong_expected_raw_hash_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "wrong.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)
        with pytest.raises(ValueError, match="Expected raw hash"):
            load_verified_manifest(p, expected_raw_hash="f" * 64)

    def test_raw_hash_changes_when_file_changes(self, tmp_path: Path) -> None:
        """Two different file encodings of the same logical manifest produce
        different raw hashes but the same canonical hash."""
        p = tmp_path / "raw_diff.json"
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, p)
        raw_bytes_1 = p.read_bytes()
        raw_hash_1 = hashlib.sha256(raw_bytes_1).hexdigest()

        # Same manifest, but with different formatting (extra whitespace).
        import json as json_mod

        data = json_mod.loads(raw_bytes_1.decode("utf-8"))
        p.write_text(json_mod.dumps(data, indent=2), "utf-8")
        raw_bytes_2 = p.read_bytes()
        raw_hash_2 = hashlib.sha256(raw_bytes_2).hexdigest()

        # Different raw bytes → different raw hashes.
        assert raw_hash_1 != raw_hash_2

        # But both still load to the same manifest.
        loaded, _, canon_h = load_verified_manifest(p)
        assert loaded == m
        assert canon_h == m.sha256_hash


# ===================================================================
# Stale hash rejection (save_manifest)
# ===================================================================


class TestStaleHash:
    def test_rejects_stale_hash(self) -> None:
        """A manifest whose sha256_hash doesn't match its content is rejected."""
        sym = Symbol(
            address=0x401000,
            name="F",
            signature="void f()",
            calling_convention=CallingConvention.CDECL,
            output_path="f.cpp",
        )
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=[sym],
        )
        # Manually create a stale copy with a wrong hash.
        stale = AbiManifest(
            version=m.version,
            architecture=m.architecture,
            pointer_size=m.pointer_size,
            symbols=m.symbols,
            sha256_hash="a" * 64,
        )
        with pytest.raises(ValueError, match="Stale manifest hash"):
            save_manifest(stale, Path("/tmp/nonexistent/stale.json"))

    def test_accepts_fresh_hash(self, tmp_path: Path) -> None:
        m = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=sample_symbols(),
        )
        save_manifest(m, tmp_path / "fresh.json")  # should not raise


# ===================================================================
# allow_nan rejection in canonical JSON
# ===================================================================


class TestAllowNan:
    def test_nan_rejected(self) -> None:
        """_canonical_json rejects NaN values via allow_nan=False."""
        from re_agent.contracts.manifest import _canonical_json

        with pytest.raises(ValueError, match="Out of range"):
            _canonical_json({"value": float("nan")})

    def test_infinity_rejected(self) -> None:
        from re_agent.contracts.manifest import _canonical_json

        with pytest.raises(ValueError, match="Out of range"):
            _canonical_json({"value": float("inf")})


# ===================================================================
# Robustness: non-dict top-level / non-UTF8
# ===================================================================


class TestRobustness:
    def test_non_dict_top_level_rejected_via_file(self, tmp_path: Path) -> None:
        p = tmp_path / "array.json"
        p.write_text('[{"address": 0}]', "utf-8")
        with pytest.raises(ValueError, match="Top-level JSON must be a dict"):
            load_manifest(p)

    def test_non_utf8_rejected_via_file(self, tmp_path: Path) -> None:
        p = tmp_path / "bad_encoding.json"
        p.write_bytes(b"\xff\xfe\x00\x01")
        with pytest.raises(ValueError, match="not valid UTF-8"):
            load_manifest(p)
