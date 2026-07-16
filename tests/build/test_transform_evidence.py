"""Focused tests for the public transform/build evidence contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from re_agent.build.evidence import (
    BuildEvidence,
    TargetCheckpoint,
    TransformEvidence,
    load_evidence,
    load_transform_evidence,
    save_evidence,
    save_transform_evidence,
    validate_evidence,
    validate_transform_evidence,
)

HEX = "a" * 64
TARGET = (0x401000, "alpha")


def _transform() -> TransformEvidence:
    return TransformEvidence(
        project_fingerprint=HEX,
        snapshot_fingerprint=HEX,
        manifest_raw_sha256=HEX,
        manifest_sha256=HEX,
        run_id="phase1-r6",
        target_address=TARGET[0],
        target_name=TARGET[1],
        target_signature="int alpha()",
        target_calling_convention="cdecl",
        target_output_path="unit/alpha.cpp",
        messages=(("system", "Transform the target."), ("user", "int alpha();")),
        llm_config={"provider": "replay", "model": "test-model", "temperature": 0},
        input_text="int alpha();",
        input_sha256="",
        raw_response="int alpha() { return 1; }",
        raw_response_sha256="",
        generated_sha256=HEX,
        object_sha256=HEX,
        compiler_argv=("g++", "-std=c++23", "-c", "alpha.cpp"),
        compiler_executable_sha256=HEX,
        request_kwargs=(("temperature", 0), ("max_tokens", 128)),
    )


def _build(schema_version: int = 2, *, path: str = "evidence/alpha.json") -> BuildEvidence:
    target = TargetCheckpoint(
        address=TARGET[0],
        name=TARGET[1],
        status="compiled",
        source_sha256=HEX,
        output_sha256=HEX,
        signature="int alpha()",
        calling_convention="cdecl",
        output_path="unit/alpha.cpp",
        input_sha256=HEX,
        generated_sha256=HEX,
        object_sha256=HEX,
        verdicts=("MANIFEST_BOUND", "COMPILE_PASS"),
        transform_evidence_path=path if schema_version == 2 else "",
        transform_evidence_sha256=HEX if schema_version == 2 else "",
    )
    return BuildEvidence(
        project_fingerprint=HEX,
        manifest_sha256=HEX,
        recipe_sha256=HEX,
        targets=(target,),
        output_path="build/game.exe",
        output_sha256=HEX,
        toolchain_sha256=HEX,
        schema_version=schema_version,
        run_id="phase1-r6",
        source_coverage=(TARGET,),
        object_coverage=(TARGET,),
        compiler_sha256=HEX,
        artifact_sha256=HEX,
        inspection_output_sha256=HEX,
        exit_status=0,
    )


def test_transform_evidence_is_canonical_and_round_trips(tmp_path: Path) -> None:
    saved = save_transform_evidence(_transform(), tmp_path / "transform.json")
    loaded = load_transform_evidence(tmp_path / "transform.json")

    assert loaded == saved
    assert loaded.to_json() == (tmp_path / "transform.json").read_bytes()


def test_unsigned_64_bit_target_addresses_are_accepted() -> None:
    address = 0xFFFFFFFFFFFFFFFF
    transform = replace(_transform(), target_address=address).with_hash()
    validate_transform_evidence(transform)

    target = replace(_build().targets[0], address=address)
    build = replace(
        _build(),
        targets=(target,),
        source_coverage=((address, TARGET[1]),),
        object_coverage=((address, TARGET[1]),),
    )
    validate_evidence(build.with_hash(), ((address, TARGET[1]),))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.replace(b'"diagnostics":""', b'"diagnostics":"","diagnostics":"duplicate"'),
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b'"diagnostics":""', b'"diagnostics":NaN'),
    ],
    ids=["duplicate-key", "noncanonical-bytes", "nan"],
)
def test_transform_loader_rejects_noncanonical_json(tmp_path: Path, mutate) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(mutate(save_transform_evidence(_transform(), tmp_path / "valid.json").to_json()))

    with pytest.raises(ValueError):
        load_transform_evidence(path)


@pytest.mark.parametrize(
    "change",
    [
        lambda evidence: replace(evidence, target_output_path="../escape.cpp"),
        lambda evidence: replace(evidence, messages=(("tool", "bad role"),)),
        lambda evidence: replace(evidence, compiler_argv=("",)),
        lambda evidence: replace(evidence, llm_config={"api_key": "secret"}),
    ],
    ids=["target-path", "message", "compiler", "config"],
)
def test_transform_validation_rejects_invalid_target_message_config_or_compiler(change) -> None:
    if change.__name__ == "<lambda>":
        try:
            candidate = change(_transform()).with_hash()
        except ValueError:
            return
    else:
        candidate = change(_transform()).with_hash()

    with pytest.raises(ValueError):
        validate_transform_evidence(candidate)


@pytest.mark.parametrize(
    "base_url",
    ["https://user:pass@example.test/v1", "https://example.test/v1?token=x", "https://example.test/v1#fragment"],
)
def test_transform_effective_config_rejects_base_url_credentials_query_or_fragment(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        replace(_transform(), llm_config={"provider": "fake", "model": "unit", "base_url": base_url})


def test_v2_checkpoint_requires_safe_unique_transform_references() -> None:
    evidence = _build()
    validate_evidence(evidence.with_hash(), (TARGET,))

    unsafe = replace(evidence.targets[0], transform_evidence_path="../escape.json")
    with pytest.raises(ValueError, match="unsafe"):
        validate_evidence(replace(evidence, targets=(unsafe,)).with_hash(), (TARGET,))

    duplicate = replace(evidence, targets=(evidence.targets[0], replace(evidence.targets[0], address=0x402000)))
    with pytest.raises(ValueError, match="duplicate transform evidence paths"):
        validate_evidence(
            replace(
                duplicate,
                source_coverage=(TARGET, (0x402000, "beta")),
                object_coverage=(TARGET, (0x402000, "beta")),
            ).with_hash(),
            (TARGET, (0x402000, "beta")),
        )

    bad_hash = replace(evidence.targets[0], transform_evidence_sha256="not-a-digest")
    with pytest.raises(ValueError):
        validate_evidence(replace(evidence, targets=(bad_hash,)).with_hash(), (TARGET,))


def test_v1_checkpoint_loads_for_history_but_has_no_replay_reference(tmp_path: Path) -> None:
    path = tmp_path / "build.json"
    saved = save_evidence(_build(schema_version=1), path)
    loaded = load_evidence(path, validate_success=True)

    assert saved.schema_version == 1
    assert loaded.targets[0].transform_evidence_path == ""
    assert loaded.targets[0].transform_evidence_sha256 == ""
    assert loaded.to_json() == path.read_bytes()
