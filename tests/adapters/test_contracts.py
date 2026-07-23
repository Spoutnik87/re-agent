from __future__ import annotations

from dataclasses import replace

import pytest

from re_agent.adapters import (
    REQUEST_PROTOCOL,
    RESULT_PROTOCOL,
    AdapterAttachment,
    AdapterCommand,
    AdapterRequest,
    AdapterResult,
)

HEX = "a" * 64


def _request() -> AdapterRequest:
    return AdapterRequest(
        capability="generic-proof",
        proof_type="fixture",
        command=AdapterCommand(("fake-adapter", "--mode", "check"), HEX),
        project_identity="project-1",
        snapshot_identity="snapshot-1",
        manifest_identity="manifest-1",
        build_target_identity="target-1",
        paths=(("input", "inputs/item.bin"),),
        hashes=(("input", HEX),),
        payload=(("mode", "strict"),),
    )


def test_request_is_canonical_and_hash_bound() -> None:
    request = _request()
    bound = replace(request, request_sha256=request.identity)
    encoded = bound.to_json_bytes()
    restored = AdapterRequest.from_dict(bound.as_dict())

    assert encoded.endswith(b"\n")
    assert restored == bound
    assert bound.identity == restored.identity


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra="unexpected"),
        lambda data: data.update(protocol="wrong.protocol"),
        lambda data: data.update(request_sha256="b" * 64),
        lambda data: data["command"].update(argv=[]),
    ],
    ids=["unknown-field", "protocol", "hash-mismatch", "malformed-command"],
)
def test_request_rejects_unknown_or_malformed_input(mutation) -> None:
    data = _request().as_dict()
    mutation(data)

    with pytest.raises(ValueError):
        AdapterRequest.from_dict(data)


def test_result_round_trip_and_strict_request_binding() -> None:
    result = AdapterResult(
        _request().identity,
        "pass",
        details=(("status", "verified"),),
    )

    assert AdapterResult.from_dict(result.as_dict(), expected_request_sha256=_request().identity) == result

    mismatch = result.as_dict()
    mismatch["request_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="does not match"):
        AdapterResult.from_dict(mismatch, expected_request_sha256=_request().identity)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra="unexpected"),
        lambda data: data.update(outcome="maybe"),
        lambda data: data.update(attachments=[{"path": "x", "sha256": HEX}]),
        lambda data: data.update(details=["not", "a", "mapping"]),
    ],
    ids=["unknown-field", "unknown-outcome", "malformed-attachment", "malformed-details"],
)
def test_result_rejects_unknown_or_malformed_input(mutation) -> None:
    data = AdapterResult(_request().identity, "fail").as_dict()
    mutation(data)

    with pytest.raises(ValueError):
        AdapterResult.from_dict(data, expected_request_sha256=_request().identity)


def test_paths_and_attachments_are_contained_and_strict() -> None:
    with pytest.raises(ValueError):
        replace(_request(), paths=(("input", "../outside"),))
    with pytest.raises(ValueError):
        AdapterAttachment("../outside", HEX, 1)
    with pytest.raises(ValueError):
        AdapterAttachment("output.bin", HEX, 0)


def test_all_declared_inputs_including_original_binary_bind_request_identity(tmp_path) -> None:
    original = tmp_path / "original.bin"
    manifest = tmp_path / "manifest.json"
    original.write_bytes(b"original-binary-v1")
    manifest.write_text("manifest-v1", encoding="utf-8")
    request = replace(
        _request(),
        paths=(("manifest", "manifest.json"), ("original_binary", "original.bin")),
        hashes=(),
    )

    bound = request.with_input_hashes(tmp_path)

    assert set(dict(bound.hashes)) == {"manifest", "original_binary"}
    assert bound.identity != request.identity
    original.write_bytes(b"original-binary-v2")
    changed = request.with_input_hashes(tmp_path)
    assert changed.identity != bound.identity
    with pytest.raises(ValueError, match="hash mismatch"):
        bound.with_input_hashes(tmp_path)


def test_protocol_constants_are_public_and_stable() -> None:
    assert _request().protocol == REQUEST_PROTOCOL
    assert AdapterResult(_request().identity, "unknown").protocol == RESULT_PROTOCOL
