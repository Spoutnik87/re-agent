"""Pure bulk-build substrate: coverage and evidence gates, no provider calls."""

from __future__ import annotations

from collections.abc import Iterable

from re_agent.build.evidence import BuildEvidence, TargetCheckpoint, coverage, validate_evidence


def missing_targets(
    expected: Iterable[tuple[int, str]], observed: Iterable[tuple[int, str]]
) -> tuple[tuple[int, str], ...]:
    """Return sorted manifest targets absent from observed coverage."""
    return tuple(sorted(set(coverage(expected)) - set(coverage(observed))))


def checkpoint_coverage(checkpoints: Iterable[TargetCheckpoint]) -> tuple[tuple[int, str], ...]:
    """Return canonical target coverage for checkpoint collections."""
    return coverage(checkpoints)


def validate_bulk_evidence(
    evidence: BuildEvidence,
    expected_targets: Iterable[tuple[int, str]],
    *,
    expected_checkpoints: Iterable[TargetCheckpoint] | None = None,
    **identities: str,
) -> None:
    """Reusable fail-closed gate for orchestration before publishing a build."""
    validate_evidence(evidence, expected_targets, expected_checkpoints=expected_checkpoints, **identities)


__all__ = [
    "BuildEvidence",
    "TargetCheckpoint",
    "checkpoint_coverage",
    "missing_targets",
    "validate_bulk_evidence",
]
