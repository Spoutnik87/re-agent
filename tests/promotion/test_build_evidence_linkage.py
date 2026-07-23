"""R5/R6 linkage checks for published BuildEvidence and transform provenance."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from re_agent.build.evidence import (
    BuildEvidence,
    TargetCheckpoint,
    TransformEvidence,
    load_evidence,
    load_transform_evidence,
    save_evidence,
    save_transform_evidence,
)
from re_agent.project.publish import BuildPublication
from re_agent.promotion.service import PromotionService

PROJECT = "a" * 64
SNAPSHOT = "b" * 64
MANIFEST_RAW = "c" * 64
MANIFEST = "d" * 64
RECIPE = "e" * 64
TOOLCHAIN = "f" * 64
COMPILER = "1" * 64


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_text(value: str) -> str:
    return digest_bytes(value.encode())


def _fixture(tmp_path: Path, *, schema_version: int = 2):
    project_root = tmp_path / "project"
    build_directory = tmp_path / "build" / "builds" / "candidate-1"
    output_path = "nested/target.cpp"
    source = build_directory / output_path
    object_file = build_directory / "nested/target.o"
    artifact = build_directory / "artifact"
    transform_path = build_directory / "transforms/target.json"
    source.parent.mkdir(parents=True)
    object_file.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"published artifact")
    source.write_bytes(b"generated source")
    object_file.write_bytes(b"published object")

    symbol = SimpleNamespace(
        address=1,
        name="target",
        signature="int target()",
        calling_convention=SimpleNamespace(value="cdecl"),
        output_path=output_path,
    )
    context = SimpleNamespace(
        identity=SimpleNamespace(
            project_fingerprint=PROJECT,
            snapshot_manifest_sha256=SNAPSHOT,
            binary_sha256="2" * 64,
        ),
        verified_abi_manifest=SimpleNamespace(
            raw_sha256=MANIFEST_RAW,
            canonical_sha256=MANIFEST,
            value=SimpleNamespace(symbols=(symbol,)),
        ),
    )
    sorted_target_index = sorted(
        (item.address, item.name) for item in context.verified_abi_manifest.value.symbols
    ).index((symbol.address, symbol.name))
    target_run_id = f"run-1-{sorted_target_index}"
    input_text = "source input"
    raw_response = "generated response"
    transform = TransformEvidence(
        project_fingerprint=PROJECT,
        snapshot_fingerprint=SNAPSHOT,
        manifest_raw_sha256=MANIFEST_RAW,
        manifest_sha256=MANIFEST,
        run_id=target_run_id,
        target_address=1,
        target_name="target",
        target_signature=symbol.signature,
        target_calling_convention="cdecl",
        target_output_path=output_path,
        messages=(("user", input_text), ("assistant", raw_response)),
        llm_config={"provider": "test", "model": "test-model"},
        input_text=input_text,
        input_sha256=digest_text(input_text),
        raw_response=raw_response,
        raw_response_sha256=digest_text(raw_response),
        generated_sha256=digest_bytes(source.read_bytes()),
        object_sha256=digest_bytes(object_file.read_bytes()),
        compiler_argv=("g++", "-c"),
        compiler_executable_sha256=COMPILER,
    )
    transform_path.parent.mkdir(parents=True)
    transform = save_transform_evidence(transform, transform_path)
    checkpoint = TargetCheckpoint(
        address=1,
        name="target",
        status="passed",
        source_sha256=digest_bytes(source.read_bytes()),
        output_sha256=digest_bytes(source.read_bytes()),
        signature=symbol.signature,
        calling_convention="cdecl",
        output_path=output_path,
        input_sha256=transform.input_sha256,
        generated_sha256=transform.generated_sha256,
        object_sha256=transform.object_sha256,
        verdicts=("MANIFEST_BOUND", "COMPILE_PASS"),
        transform_evidence_path="transforms/target.json" if schema_version == 2 else "",
        transform_evidence_sha256=digest_bytes(transform_path.read_bytes()) if schema_version == 2 else "",
    )
    evidence = BuildEvidence(
        project_fingerprint=PROJECT,
        manifest_sha256=MANIFEST,
        recipe_sha256=RECIPE,
        targets=(checkpoint,),
        output_path=output_path,
        output_sha256=digest_bytes(source.read_bytes()),
        toolchain_sha256=TOOLCHAIN,
        schema_version=schema_version,
        run_id="run-1",
        source_coverage=((1, "target"),),
        object_coverage=((1, "target"),),
        exit_status=0,
        artifact_sha256=digest_bytes(artifact.read_bytes()),
        compiler_sha256=COMPILER,
    )
    evidence_path = build_directory / "evidence"
    checked = save_evidence(evidence, evidence_path)
    publication = BuildPublication(
        "candidate-1", build_directory, checked.artifact_sha256, digest_bytes(evidence_path.read_bytes())
    )
    service = PromotionService(project_root, promotion_root=tmp_path / "promotion", build_root=tmp_path / "build")
    return service, context, publication, evidence_path, transform_path, checkpoint


def _patch_publication(monkeypatch, service, context, publication, evidence_path):
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: context)
    monkeypatch.setattr("re_agent.promotion.service.load_active_build", lambda root: publication)
    monkeypatch.setattr(
        "re_agent.promotion.service.load_evidence",
        lambda path, validate_success=False: load_evidence(evidence_path),
    )


def test_v2_contained_transform_evidence_loads(monkeypatch, tmp_path):
    service, context, publication, evidence_path, transform_path, *_ = _fixture(tmp_path)
    _patch_publication(monkeypatch, service, context, publication, evidence_path)
    loaded = service._load_build(context, None)
    assert loaded.evidence.schema_version == 2
    assert load_transform_evidence(transform_path).run_id == "run-1-0"


def test_v1_historical_build_evidence_remains_promotion_compatible(monkeypatch, tmp_path):
    service, context, publication, evidence_path, *_ = _fixture(tmp_path, schema_version=1)
    _patch_publication(monkeypatch, service, context, publication, evidence_path)
    assert service._load_build(context, None).evidence.schema_version == 1


@pytest.mark.parametrize("failure", ["missing", "tampered", "file-hash-mismatched", "unsafe", "symlink"])
def test_v2_evidence_path_failures_happen_before_adapter_invocation(monkeypatch, tmp_path, failure):
    service, context, publication, evidence_path, transform_path, checkpoint = _fixture(tmp_path)
    _patch_publication(monkeypatch, service, context, publication, evidence_path)
    invoked = False

    def fail_if_invoked(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("adapter resolution must not occur")

    monkeypatch.setattr(service, "_commands", fail_if_invoked)
    if failure == "missing":
        transform_path.unlink()
    elif failure == "tampered":
        transform_path.write_bytes(transform_path.read_bytes() + b"tampered")
    elif failure == "file-hash-mismatched":
        transform_path.write_bytes(transform_path.read_bytes().replace(b"generated response", b"changed response"))
    else:
        if failure == "unsafe":
            bad_checkpoint = replace(checkpoint, transform_evidence_path="../escape.json")
        else:
            symlink = transform_path.parent / "symlink.json"
            try:
                os.symlink(transform_path, symlink)
            except OSError, NotImplementedError:
                pytest.skip("symlink creation is unavailable")
            bad_checkpoint = replace(checkpoint, transform_evidence_path="transforms/symlink.json")
        bad_evidence = replace(load_evidence(evidence_path), targets=(bad_checkpoint,)).with_hash()
        evidence_path.write_bytes(bad_evidence.to_json())

    with pytest.raises((OSError, ValueError)):
        service.inspect_abi()
    assert not invoked


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_fingerprint", "9" * 64),
        ("manifest_sha256", "8" * 64),
        ("target_name", "substituted"),
        ("target_address", 99),
        ("input_text", "substituted input"),
        ("generated_sha256", "7" * 64),
        ("object_sha256", "6" * 64),
    ],
)
def test_v2_substituted_transform_identity_fails_before_adapters(monkeypatch, tmp_path, field, value):
    service, context, publication, evidence_path, transform_path, checkpoint = _fixture(tmp_path)
    _patch_publication(monkeypatch, service, context, publication, evidence_path)
    transform = load_transform_evidence(transform_path)
    if field == "input_text":
        changed = replace(transform, input_text=value, input_sha256=digest_text(value)).with_hash()
    else:
        changed = replace(transform, **{field: value}).with_hash()
    transform_path.write_bytes(changed.to_json())
    updated_checkpoint = replace(checkpoint, transform_evidence_sha256=digest_bytes(transform_path.read_bytes()))
    loaded = load_evidence(evidence_path)
    updated = replace(loaded, targets=(updated_checkpoint,)).with_hash()
    evidence_path.write_bytes(updated.to_json())
    monkeypatch.setattr(service, "_commands", lambda *args: (_ for _ in ()).throw(AssertionError("adapter invoked")))

    with pytest.raises(ValueError):
        service.inspect_abi()
