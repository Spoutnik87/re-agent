"""Tests for the WorkPacket schema (Todo 4).

Deterministic, in-memory only. No files are created by serialization.
Follows Given/When/Then naming.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from re_agent.build.work_packet import (
    ArtifactRef,
    CompileVerdict,
    FunctionIdentity,
    ModelUsage,
    NeighbourContext,
    ParityVerdict,
    StableContext,
    TaskSuffix,
    WorkPacket,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_stable_context() -> StableContext:
    return StableContext(
        function=FunctionIdentity(
            address="0x00401000",
            name="FUN_00401000",
            module="core",
            subunit_index=2,
        ),
        decompiled_code="void FUN_00401000() { return; }",
        neighbour_context=(
            NeighbourContext(address="0x00400FFF", code="// prev"),
            NeighbourContext(address="0x00401001", code="// next"),
        ),
        ghidra_context={"callers": ["0x00402000"]},
        project_rules={"naming": "camelCase"},
    )


def _sample_task_suffix(compiler_stderr: str | None = None) -> TaskSuffix:
    return TaskSuffix(
        task_kind="transform",
        compiler_stderr=compiler_stderr,
        prior_attempt_summary="round 1 failed",
        requested_output_format="cpp",
    )


def _sample_packet(compiler_stderr: str | None = None) -> WorkPacket:
    return WorkPacket(
        schema_version=1,
        run_id="run-abc",
        stable_context=_sample_stable_context(),
        task_suffix=_sample_task_suffix(compiler_stderr=compiler_stderr),
        artifacts=(ArtifactRef(path="output/a.cpp", kind="source", sha256="deadbeef"),),
        evidence_paths=("evidence/run-abc.txt",),
    )


# ---------------------------------------------------------------------------
# 1. JSON roundtrip preserves equality and tuple restoration
# ---------------------------------------------------------------------------


def test_json_roundtrip_preserves_equality_and_tuples() -> None:
    """Given a complete packet, When serialized and deserialized,
    Then the result equals the original and tuple types are restored."""
    # Given
    original = _sample_packet()
    # When
    data = original.to_json_dict()
    restored = WorkPacket.from_json_dict(data)
    # Then
    assert restored == original
    assert isinstance(restored.stable_context.neighbour_context, tuple)
    assert isinstance(restored.artifacts, tuple)
    assert isinstance(restored.evidence_paths, tuple)


def test_json_text_roundtrip_preserves_packet() -> None:
    """Given a packet, When serialized to JSON text and back,
    Then the result equals the original."""
    original = _sample_packet()
    text = original.to_json_text()
    restored = WorkPacket.from_json_text(text)
    assert restored == original


# ---------------------------------------------------------------------------
# 2. Stable context hash unchanged when only compiler_stderr changes
# ---------------------------------------------------------------------------


def test_stable_context_hash_unchanged_when_compiler_stderr_changes() -> None:
    """Given two packets differing only in compiler_stderr,
    When stable_context_hash is computed,
    Then the stable hashes are equal."""
    p1 = _sample_packet(compiler_stderr=None)
    p2 = _sample_packet(compiler_stderr="error: missing semicolon")
    assert p1.stable_context_hash() == p2.stable_context_hash()


# ---------------------------------------------------------------------------
# 3. Task suffix hash changes when compiler_stderr changes
# ---------------------------------------------------------------------------


def test_task_suffix_hash_changes_when_compiler_stderr_changes() -> None:
    """Given two packets differing only in compiler_stderr,
    When task_suffix_hash is computed,
    Then the task suffix hashes differ."""
    p1 = _sample_packet(compiler_stderr=None)
    p2 = _sample_packet(compiler_stderr="error: missing semicolon")
    assert p1.task_suffix_hash() != p2.task_suffix_hash()


def test_full_packet_hash_changes_when_compiler_stderr_changes() -> None:
    """Given two packets differing only in compiler_stderr,
    When full_packet_hash is computed,
    Then the full hashes differ."""
    p1 = _sample_packet(compiler_stderr=None)
    p2 = _sample_packet(compiler_stderr="error: missing semicolon")
    assert p1.full_packet_hash() != p2.full_packet_hash()


def test_hashes_are_16_chars() -> None:
    """Given a packet, When any hash is computed,
    Then the hash is exactly 16 hex characters."""
    p = _sample_packet()
    assert len(p.stable_context_hash()) == 16
    assert len(p.task_suffix_hash()) == 16
    assert len(p.full_packet_hash()) == 16


# ---------------------------------------------------------------------------
# 4. Missing optional cache metrics serialize as null and roundtrip as None
# ---------------------------------------------------------------------------


def test_missing_model_cache_metrics_serialize_as_null_and_roundtrip_none() -> None:
    """Given a ModelUsage with no cache metrics,
    When serialized to JSON dict,
    Then cache fields are null and roundtrip as None (not 0)."""
    usage = ModelUsage(provider="openai", model="gpt-4", prompt_tokens=100, completion_tokens=50)
    packet = WorkPacket(
        schema_version=1,
        run_id="run-x",
        stable_context=_sample_stable_context(),
        task_suffix=_sample_task_suffix(),
        artifacts=(),
        model_usage=usage,
        evidence_paths=(),
    )
    data = packet.to_json_dict()
    mu = data["model_usage"]
    assert mu["cache_hit_tokens"] is None
    assert mu["cache_miss_tokens"] is None
    restored = WorkPacket.from_json_dict(data)
    assert restored.model_usage is not None
    assert restored.model_usage.cache_hit_tokens is None
    assert restored.model_usage.cache_miss_tokens is None


def test_model_usage_none_roundtrips_as_none() -> None:
    """Given a packet with no model_usage,
    When roundtripped,
    Then model_usage stays None."""
    packet = _sample_packet()
    assert packet.model_usage is None
    restored = WorkPacket.from_json_dict(packet.to_json_dict())
    assert restored.model_usage is None


# ---------------------------------------------------------------------------
# 5. Subunit/multi-function adjacent context preserves hex-addressed function fixtures
# ---------------------------------------------------------------------------


def test_neighbour_context_preserves_addresses_and_ordering() -> None:
    """Given a stable context with two hex-style addresses,
    When roundtripped,
    Then both addresses are preserved in order."""
    ctx = _sample_stable_context()
    addrs = [n.address for n in ctx.neighbour_context]
    assert addrs == ["0x00400FFF", "0x00401001"]
    restored_ctx = WorkPacket.from_json_dict(_sample_packet().to_json_dict()).stable_context
    assert [n.address for n in restored_ctx.neighbour_context] == addrs


# ---------------------------------------------------------------------------
# 6. Artifact/evidence paths serialized but no files created
# ---------------------------------------------------------------------------


def test_serialization_does_not_create_files(tmp_path: Path) -> None:
    """Given a packet with artifact and evidence paths,
    When serialized to JSON text in a tmp dir,
    Then no files are created at those paths."""
    packet = _sample_packet()
    text = packet.to_json_text()
    # Write the JSON text itself to a file in tmp_path (this is the test artifact, not the packet's)
    out = tmp_path / "packet.json"
    out.write_text(text, encoding="utf-8")
    # The packet references "output/a.cpp" and "evidence/run-abc.txt" — those must NOT exist
    assert not (tmp_path / "output" / "a.cpp").exists()
    assert not (tmp_path / "evidence" / "run-abc.txt").exists()
    # And the serialized JSON contains the path strings
    assert "output/a.cpp" in text
    assert "evidence/run-abc.txt" in text


# ---------------------------------------------------------------------------
# 7. Invalid task_kind is rejected
# ---------------------------------------------------------------------------


def test_invalid_task_kind_rejected_on_construction() -> None:
    """Given an invalid task_kind,
    When constructing a TaskSuffix,
    Then ValueError is raised."""
    with pytest.raises(ValueError):
        TaskSuffix(task_kind="not_a_real_kind")


def test_invalid_task_kind_rejected_on_from_json_dict() -> None:
    """Given a JSON dict with an invalid task_kind,
    When from_json_dict is called,
    Then ValueError is raised."""
    packet = _sample_packet()
    data = packet.to_json_dict()
    data["task_suffix"]["task_kind"] = "bogus"
    with pytest.raises(ValueError):
        WorkPacket.from_json_dict(data)


# ---------------------------------------------------------------------------
# 8. Schema version mismatch rejected
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_rejected_on_from_json_dict() -> None:
    """Given a JSON dict with a different schema_version,
    When from_json_dict is called,
    Then ValueError is raised."""
    packet = _sample_packet()
    data = packet.to_json_dict()
    data["schema_version"] = 999
    with pytest.raises(ValueError):
        WorkPacket.from_json_dict(data)


# ---------------------------------------------------------------------------
# Extra: verdicts roundtrip
# ---------------------------------------------------------------------------


def test_compile_and_parity_verdicts_roundtrip() -> None:
    """Given a packet with compile and parity verdicts,
    When roundtripped,
    Then the verdicts are preserved."""
    packet = WorkPacket(
        schema_version=1,
        run_id="run-v",
        stable_context=_sample_stable_context(),
        task_suffix=_sample_task_suffix(),
        artifacts=(),
        compile_verdict=CompileVerdict(compiles=False, verdict="FAIL", stderr="error: x"),
        parity_verdict=ParityVerdict(status="RED", details={"call_count_diff": 5}),
        evidence_paths=(),
    )
    restored = WorkPacket.from_json_dict(packet.to_json_dict())
    assert restored.compile_verdict == packet.compile_verdict
    assert restored.parity_verdict == packet.parity_verdict


def test_json_dict_is_json_serializable() -> None:
    """Given to_json_dict output,
    When json.dumps is called,
    Then it succeeds (no non-serializable types leak)."""
    packet = _sample_packet()
    data = packet.to_json_dict()
    # Must not raise
    json.dumps(data)
