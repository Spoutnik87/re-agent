"""Shared fixtures for re-agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from re_agent.contracts import Architecture, CallingConvention, Symbol, manifest_from_symbols, save_manifest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_config_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_config.yaml"


@pytest.fixture
def sample_source_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_source.cpp"


@pytest.fixture
def sample_hooks_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_hooks.csv"


# ── ABI manifest helpers ────────────────────────────────────────────────


@pytest.fixture
def abi_manifest_factory(tmp_path: Path):
    """Return a factory that writes a valid AbiManifest and yields (path, raw_sha256).

    Usage in tests::

        manifest_path, raw_sha256 = abi_manifest_factory()

    The returned *raw_sha256* is the SHA-256 of the file bytes on disk
    — pass it as ``contracts.abi_manifest_sha256`` in YAML config strings.
    """

    def _make(symbols: list[Symbol] | None = None) -> tuple[Path, str]:
        import hashlib

        if symbols is None:
            symbols = [
                Symbol(
                    address=0x1000,
                    name="func_a",
                    signature="void func_a()",
                    calling_convention=CallingConvention.CDECL,
                    output_path="mod_a.cpp",
                ),
            ]
        manifest = manifest_from_symbols(
            version="1.0.0",
            architecture=Architecture.X86,
            pointer_size=4,
            symbols=symbols,
        )
        path = tmp_path / "abi_manifest.json"
        save_manifest(manifest, path)
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, raw_hash

    return _make
