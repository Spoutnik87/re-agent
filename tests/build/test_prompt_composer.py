"""Tests for the prompt composer (Todo 6).

The composer takes a WorkPacket (Todo 4) and emits cache-stable prompt parts:
a stable prefix (project/system rules, function identity, decompiled code,
neighbour + ghidra context, stable artifact/evidence identifiers) and a task
suffix (task_kind, compiler stderr, prior attempt summary, requested output
format, compile/parity issue details).

Deterministic, in-memory only. No LLM calls, no file IO. Given/When/Then naming.
"""

from __future__ import annotations

import hashlib

import pytest

from re_agent.build.prompt_composer import (
    compose_prompt_parts,
)
from re_agent.build.work_packet import (
    ArtifactRef,
    FunctionIdentity,
    NeighbourContext,
    StableContext,
    TaskSuffix,
    WorkPacket,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_context(decompiled_code: str = "void FUN_00401000() { return; }") -> StableContext:
    return StableContext(
        function=FunctionIdentity(
            address="0x00401000",
            name="FUN_00401000",
            module="core",
            subunit_index=2,
        ),
        decompiled_code=decompiled_code,
        neighbour_context=(
            NeighbourContext(address="0x00400FFF", code="// prev"),
            NeighbourContext(address="0x00401001", code="// next"),
        ),
        ghidra_context={"callers": ["0x00402000"]},
        project_rules={"naming": "camelCase"},
    )


def _task_suffix(
    task_kind: str = "transform",
    compiler_stderr: str | None = None,
    prior_attempt_summary: str | None = "round 1 failed",
    requested_output_format: str | None = "cpp",
) -> TaskSuffix:
    return TaskSuffix(
        task_kind=task_kind,
        compiler_stderr=compiler_stderr,
        prior_attempt_summary=prior_attempt_summary,
        requested_output_format=requested_output_format,
    )


def _packet(
    compiler_stderr: str | None = None,
    decompiled_code: str = "void FUN_00401000() { return; }",
    task_kind: str = "transform",
    artifacts: tuple[ArtifactRef, ...] = (ArtifactRef(path="output/a.cpp", kind="source", sha256="deadbeef"),),
) -> WorkPacket:
    return WorkPacket(
        schema_version=1,
        run_id="run-abc",
        stable_context=_stable_context(decompiled_code=decompiled_code),
        task_suffix=_task_suffix(
            task_kind=task_kind,
            compiler_stderr=compiler_stderr,
        ),
        artifacts=artifacts,
        evidence_paths=("evidence/run-abc.txt",),
    )


# ---------------------------------------------------------------------------
# 1. Stable prefix is byte-identical when only compiler_stderr changes
# ---------------------------------------------------------------------------


def test_stable_prefix_byte_identical_when_only_compiler_stderr_changes() -> None:
    """Given two packets differing only in compiler_stderr,
    When compose_prompt_parts is called on each,
    Then the stable_prefix strings are byte-identical and the task_suffix strings differ."""
    # Given
    p1 = _packet(compiler_stderr=None)
    p2 = _packet(compiler_stderr="error: missing semicolon")
    # When
    parts1 = compose_prompt_parts(p1)
    parts2 = compose_prompt_parts(p2)
    # Then
    assert parts1.stable_prefix == parts2.stable_prefix
    assert parts1.task_suffix != parts2.task_suffix


# ---------------------------------------------------------------------------
# 2. Changing only compiler_stderr leaves stable_context_hash unchanged
#    but changes task suffix hash and full prompt hash
# ---------------------------------------------------------------------------


def test_compiler_stderr_change_preserves_stable_hash_but_changes_suffix_and_full_hashes() -> None:
    """Given two packets differing only in compiler_stderr,
    When WorkPacket hashes and prompt-part hashes are computed,
    Then stable_context_hash is unchanged, task_suffix_hash changes,
    and the full_prompt hash changes."""
    # Given
    p1 = _packet(compiler_stderr=None)
    p2 = _packet(compiler_stderr="error: missing semicolon")
    # When
    parts1 = compose_prompt_parts(p1)
    parts2 = compose_prompt_parts(p2)
    # Then
    assert p1.stable_context_hash() == p2.stable_context_hash()
    assert p1.task_suffix_hash() != p2.task_suffix_hash()
    # full_prompt hash derived from the composed parts
    assert parts1.full_prompt_hash() != parts2.full_prompt_hash()


# ---------------------------------------------------------------------------
# 3. Changing decompiled code changes stable_prefix and stable_context_hash
# ---------------------------------------------------------------------------


def test_decompiled_code_change_changes_stable_prefix_and_stable_hash() -> None:
    """Given two packets differing only in decompiled_code,
    When compose_prompt_parts is called and stable_context_hash is computed,
    Then both the stable_prefix strings and the stable_context_hash differ."""
    # Given
    p1 = _packet(decompiled_code="void FUN_00401000() { return; }")
    p2 = _packet(decompiled_code="void FUN_00401000() { /* changed */ return; }")
    # When
    parts1 = compose_prompt_parts(p1)
    parts2 = compose_prompt_parts(p2)
    # Then
    assert parts1.stable_prefix != parts2.stable_prefix
    assert p1.stable_context_hash() != p2.stable_context_hash()


# ---------------------------------------------------------------------------
# 4. Each task kind surfaces task_kind and requested output expectations in suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_kind", ["transform", "compile_repair", "parity_triage", "reverse"])
def test_each_task_kind_included_in_suffix_with_output_expectations(task_kind: str) -> None:
    """Given a packet for each supported task_kind with a requested_output_format,
    When compose_prompt_parts is called,
    Then the task_suffix contains the task_kind and the requested output format."""
    # Given
    packet = _packet(task_kind=task_kind)
    # When
    parts = compose_prompt_parts(packet)
    # Then
    assert task_kind in parts.task_suffix, f"task_kind {task_kind!r} must appear in suffix"
    # requested_output_format default is "cpp"
    assert "cpp" in parts.task_suffix, "requested output format must appear in suffix"
    # task_kind must NOT leak into the stable prefix (it is task-specific)
    assert task_kind not in parts.stable_prefix


# ---------------------------------------------------------------------------
# 5. PromptParts.full_prompt concatenates prefix + suffix deterministically
# ---------------------------------------------------------------------------


def test_full_prompt_is_stable_prefix_then_task_suffix_concatenation() -> None:
    """Given composed PromptParts,
    When full_prompt is called,
    Then it equals stable_prefix + task_suffix (deterministic ordering)."""
    # Given
    packet = _packet()
    parts = compose_prompt_parts(packet)
    # When
    full = parts.full_prompt()
    # Then
    assert full == parts.stable_prefix + parts.task_suffix
    # And stable prefix precedes task suffix
    assert full.index(parts.stable_prefix) < full.index(parts.task_suffix)


# ---------------------------------------------------------------------------
# 6. Stable prefix includes function identity, decompiled code, neighbour + ghidra context
# ---------------------------------------------------------------------------


def test_stable_prefix_includes_function_identity_and_contexts() -> None:
    """Given a packet with function identity, decompiled code, neighbour and ghidra context,
    When compose_prompt_parts is called,
    Then the stable_prefix contains the address, name, module, subunit_index,
    decompiled code, neighbour addresses, and ghidra callers."""
    # Given
    packet = _packet()
    # When
    parts = compose_prompt_parts(packet)
    # Then
    sp = parts.stable_prefix
    assert "0x00401000" in sp, "function address must be in stable prefix"
    assert "FUN_00401000" in sp, "function name must be in stable prefix"
    assert "core" in sp, "module must be in stable prefix"
    assert "subunit_index: 2" in sp, "subunit_index must be in stable prefix"
    assert "void FUN_00401000() { return; }" in sp, "decompiled code must be in stable prefix"
    assert "0x00400FFF" in sp, "neighbour address must be in stable prefix"
    assert "0x00402000" in sp, "ghidra caller must be in stable prefix"


# ---------------------------------------------------------------------------
# 7. Stable prefix includes stable artifact/evidence identifiers
# ---------------------------------------------------------------------------


def test_stable_prefix_includes_stable_artifact_and_evidence_identifiers() -> None:
    """Given a packet with artifacts and evidence_paths,
    When compose_prompt_parts is called,
    Then the stable_prefix contains the artifact path, kind, sha256 and evidence path
    (these are stable identifiers, not task-specific)."""
    # Given
    packet = _packet()
    # When
    parts = compose_prompt_parts(packet)
    # Then
    sp = parts.stable_prefix
    assert "output/a.cpp" in sp, "artifact path must be in stable prefix"
    assert "source" in sp, "artifact kind must be in stable prefix"
    assert "deadbeef" in sp, "artifact sha256 must be in stable prefix"
    assert "evidence/run-abc.txt" in sp, "evidence path must be in stable prefix"


# ---------------------------------------------------------------------------
# 8. Task suffix includes compiler stderr, prior attempt summary, output format
# ---------------------------------------------------------------------------


def test_task_suffix_includes_compiler_stderr_prior_summary_and_output_format() -> None:
    """Given a packet with compiler_stderr, prior_attempt_summary, requested_output_format,
    When compose_prompt_parts is called,
    Then the task_suffix contains all three task-specific fields."""
    # Given
    packet = _packet(compiler_stderr="error: missing semicolon at line 5")
    # When
    parts = compose_prompt_parts(packet)
    # Then
    ts = parts.task_suffix
    assert "error: missing semicolon at line 5" in ts, "compiler_stderr must be in task suffix"
    assert "round 1 failed" in ts, "prior_attempt_summary must be in task suffix"
    assert "cpp" in ts, "requested_output_format must be in task suffix"


# ---------------------------------------------------------------------------
# 9. PromptParts is a frozen value type
# ---------------------------------------------------------------------------


def test_prompt_parts_is_frozen() -> None:
    """Given a PromptParts instance,
    When an attribute assignment is attempted,
    Then it raises FrozenInstanceError (dataclass frozen=True)."""
    # Given
    packet = _packet()
    parts = compose_prompt_parts(packet)
    # When / Then
    with pytest.raises(AttributeError):
        parts.stable_prefix = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 10. full_prompt_hash is sha256-derived and 16 hex chars (matches WorkPacket convention)
# ---------------------------------------------------------------------------


def test_full_prompt_hash_is_16_hex_chars_and_matches_sha256_prefix() -> None:
    """Given composed PromptParts,
    When full_prompt_hash is called,
    Then it returns 16 hex characters matching the first 16 of sha256(full_prompt)."""
    # Given
    packet = _packet()
    parts = compose_prompt_parts(packet)
    # When
    h = parts.full_prompt_hash()
    expected = hashlib.sha256(parts.full_prompt().encode("utf-8")).hexdigest()[:16]
    # Then
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)
    assert h == expected


# ---------------------------------------------------------------------------
# 11. Composer does not duplicate schema hash logic — reuses WorkPacket hashes
# ---------------------------------------------------------------------------


def test_composer_stable_hash_matches_work_packet_stable_context_hash() -> None:
    """Given a packet,
    When compose_prompt_parts is called and stable_hash is requested,
    Then it equals the WorkPacket.stable_context_hash (no duplicated hashing)."""
    # Given
    packet = _packet()
    parts = compose_prompt_parts(packet)
    # When / Then
    assert parts.stable_hash == packet.stable_context_hash()
    assert parts.suffix_hash == packet.task_suffix_hash()
