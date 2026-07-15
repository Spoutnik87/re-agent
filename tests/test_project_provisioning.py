from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from re_agent.contracts import Architecture, CallingConvention, Symbol, manifest_from_symbols, save_manifest
from re_agent.project.context import load_verified_project
from re_agent.project.provision import ProvisionError, provision_project


def _analysis(root: Path, binary: Path) -> Path:
    analysis = root / "analysis"
    analysis.mkdir()
    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X86,
        pointer_size=4,
        symbols=[Symbol(0x1000, "entry", "void entry()", CallingConvention.CDECL, "entry.cpp")],
    )
    save_manifest(manifest, analysis / "abi.json")
    metadata = {
        "schema_version": "1",
        "backend": "offline-export",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "abi_manifest_path": "abi.json",
    }
    (analysis / "analysis-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return analysis


def test_provision_is_owned_verified_and_idempotent(tmp_path: Path) -> None:
    binary = tmp_path / "target.bin"
    binary.write_bytes(b"target")
    analysis = _analysis(tmp_path, binary)
    output = tmp_path / "project"
    first = provision_project(binary=binary, analysis=analysis, output=output, name="any target")
    assert provision_project(binary=binary, analysis=analysis, output=output, name="any target") == first
    analysis.joinpath("abi.json").write_text('{"changed":true}', encoding="utf-8")
    context = load_verified_project(output)
    assert context.identity == first
    assert context.verified_abi_manifest.manifest.symbols[0].name == "entry"


def test_provision_mismatch_does_not_create_output(tmp_path: Path) -> None:
    binary = tmp_path / "target.bin"
    binary.write_bytes(b"target")
    analysis = _analysis(tmp_path, binary)
    metadata_path = analysis / "analysis-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["binary_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ProvisionError, match="fingerprint mismatch"):
        provision_project(binary=binary, analysis=analysis, output=tmp_path / "project", name="x")
    assert not (tmp_path / "project").exists()
