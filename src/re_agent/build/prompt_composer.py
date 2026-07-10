"""Cache-stable prompt composer for the WorkPacket schema (Todo 6).

Takes a WorkPacket (Todo 4) and emits two cache-stable prompt parts:

- ``stable_prefix`` — project/system rules, function identity (address, name,
  module, subunit_index), decompiled code, neighbour context, ghidra context,
  and stable artifact/evidence identifiers. This is the prefix a provider-side
  prompt cache (e.g. DeepSeek disk cache, Anthropic prompt caching) can hit
  across rounds and across task kinds for the same function.

- ``task_suffix`` — task_kind, compiler stderr, prior attempt summary,
  requested output format, and compile/parity issue details. This is the
  per-task variable tail that changes between rounds.

Design:
- Frozen dataclasses; stdlib only.
- Does NOT duplicate WorkPacket schema hashing logic — ``stable_hash`` and
  ``suffix_hash`` delegate to ``WorkPacket.stable_context_hash`` and
  ``WorkPacket.task_suffix_hash``.
- Not wired into runtime transform/parity yet (Todo 6 deliverable).
- Preserves current prompt content/intent; only makes ordering/separation
  explicit. No Jinja2 here — the composer is a deterministic string builder
  over the typed packet so the cache prefix is byte-stable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from re_agent.build.work_packet import WorkPacket
from re_agent.build.work_packet_types import ArtifactRef, JsonValue, NeighbourContext

__all__ = ["PromptParts", "compose_prompt_parts"]

_HASH_LEN = 16


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _format_function_identity(
    address: str,
    name: str | None,
    module: str | None,
    subunit_index: int | None,
) -> str:
    parts: list[str] = ["## Function Identity", f"address: {address}"]
    if name is not None:
        parts.append(f"name: {name}")
    if module is not None:
        parts.append(f"module: {module}")
    if subunit_index is not None:
        parts.append(f"subunit_index: {subunit_index}")
    return "\n".join(parts)


def _format_neighbour_context(neighbours: tuple[NeighbourContext, ...]) -> str:
    if not neighbours:
        return "## Neighbour Context\n(none)"
    lines = ["## Neighbour Context"]
    for n in neighbours:
        lines.append(f"### {n.address}")
        lines.append(n.code)
    return "\n".join(lines)


def _format_mapping(title: str, mapping: Mapping[str, JsonValue]) -> str:
    if not mapping:
        return f"## {title}\n(none)"
    lines = [f"## {title}"]
    for key in sorted(mapping):
        lines.append(f"{key}: {mapping[key]!r}")
    return "\n".join(lines)


def _format_artifacts(artifacts: tuple[ArtifactRef, ...]) -> str:
    if not artifacts:
        return "## Artifacts\n(none)"
    lines = ["## Artifacts"]
    for a in artifacts:
        sha = a.sha256 if a.sha256 is not None else "(no sha256)"
        lines.append(f"- path={a.path} kind={a.kind} sha256={sha}")
    return "\n".join(lines)


def _format_evidence_paths(paths: tuple[str, ...]) -> str:
    if not paths:
        return "## Evidence Paths\n(none)"
    lines = ["## Evidence Paths"]
    for p in paths:
        lines.append(f"- {p}")
    return "\n".join(lines)


def _format_optional_field(label: str, value: str | None) -> str:
    if value is None:
        return f"{label}: (none)"
    return f"{label}:\n{value}"


@dataclass(frozen=True, slots=True)
class PromptParts:
    """Cache-stable prompt parts: a stable prefix and a task-specific suffix.

    The stable prefix is byte-identical across packets that share the same
    ``StableContext`` (function identity, decompiled code, neighbour/ghidra
    context, project rules, stable artifact/evidence identifiers). The task
    suffix varies per round/task.

    ``stable_hash`` and ``suffix_hash`` are precomputed at compose time by
    delegating to ``WorkPacket.stable_context_hash`` / ``task_suffix_hash`` —
    the composer does NOT duplicate the schema's canonical-JSON hashing logic.
    """

    stable_prefix: str
    task_suffix: str
    stable_hash: str
    suffix_hash: str

    def full_prompt(self) -> str:
        """Concatenate stable prefix then task suffix (deterministic ordering).

        The stable prefix always precedes the task suffix so a provider-side
        prompt cache can hit on the prefix across rounds.
        """
        return self.stable_prefix + self.task_suffix

    def full_prompt_hash(self) -> str:
        """sha256-derived 16-hex-char hash of the full prompt (matches the
        WorkPacket hash length convention)."""
        return _hash_text(self.full_prompt())


def compose_prompt_parts(packet: WorkPacket) -> PromptParts:
    """Compose cache-stable prompt parts from a WorkPacket.

    The stable prefix is built only from ``packet.stable_context`` plus the
    stable artifact/evidence identifiers (paths, kinds, sha256). The task
    suffix is built only from ``packet.task_suffix`` plus compile/parity
    verdict issue details when present.

    No LLM calls, no file IO, no Jinja2 rendering — deterministic string
    assembly over the typed packet.
    """
    stable_prefix = _build_stable_prefix(packet)
    task_suffix = _build_task_suffix(packet)
    # Delegate hashing to the WorkPacket schema — do not duplicate canonical-JSON logic.
    return PromptParts(
        stable_prefix=stable_prefix,
        task_suffix=task_suffix,
        stable_hash=packet.stable_context_hash(),
        suffix_hash=packet.task_suffix_hash(),
    )


def _build_stable_prefix(packet: WorkPacket) -> str:
    sc = packet.stable_context
    sections: list[str] = []

    # 1. Project / system rules (stable across the whole project)
    sections.append(_format_mapping("Project Rules", sc.project_rules))

    # 2. Function identity (address, name, module, subunit_index)
    sections.append(
        _format_function_identity(
            address=sc.function.address,
            name=sc.function.name,
            module=sc.function.module,
            subunit_index=sc.function.subunit_index,
        )
    )

    # 3. Decompiled code (stable across rounds for the same function)
    sections.append("## Decompiled Code\n" + sc.decompiled_code)

    # 4. Neighbour context (stable for the same function layout)
    sections.append(_format_neighbour_context(sc.neighbour_context))

    # 5. Ghidra context (callers, callees, structs — stable for the binary)
    sections.append(_format_mapping("Ghidra Context", sc.ghidra_context))

    # 6. Stable artifact identifiers (paths, kinds, sha256 — not task-specific)
    sections.append(_format_artifacts(packet.artifacts))

    # 7. Stable evidence identifiers (paths — not task-specific)
    sections.append(_format_evidence_paths(packet.evidence_paths))

    return "\n\n".join(sections) + "\n"


def _build_task_suffix(packet: WorkPacket) -> str:
    ts = packet.task_suffix
    sections: list[str] = []

    # 1. Task kind (the discriminator — always present, validated by TaskSuffix)
    sections.append(f"## Task Kind\n{ts.task_kind}")

    # 2. Requested output format (task-specific expectation)
    sections.append(_format_optional_field("Requested Output Format", ts.requested_output_format))

    # 3. Compiler stderr (task-specific, changes between compile_repair rounds)
    sections.append(_format_optional_field("Compiler Stderr", ts.compiler_stderr))

    # 4. Prior attempt summary (task-specific, changes between rounds)
    sections.append(_format_optional_field("Prior Attempt Summary", ts.prior_attempt_summary))

    # 5. Compile verdict issue details (task-specific, when present)
    if packet.compile_verdict is not None:
        cv = packet.compile_verdict
        cv_lines = [
            "## Compile Verdict",
            f"compiles: {cv.compiles}",
            f"verdict: {cv.verdict}",
        ]
        if cv.stderr is not None:
            cv_lines.append(f"stderr:\n{cv.stderr}")
        sections.append("\n".join(cv_lines))

    # 6. Parity verdict issue details (task-specific, when present)
    if packet.parity_verdict is not None:
        pv = packet.parity_verdict
        sections.append(f"## Parity Verdict\nstatus: {pv.status}\ndetails: {dict(pv.details)!r}")

    return "\n\n".join(sections) + "\n"
