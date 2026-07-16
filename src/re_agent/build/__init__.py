"""Build primitives for manifest-bound reconstruction."""

from re_agent.build.bulk import checkpoint_coverage, missing_targets, validate_bulk_evidence
from re_agent.build.evidence import (
    BuildEvidence,
    TargetCheckpoint,
    TransformEvidence,
    coverage,
    load_evidence,
    load_transform_evidence,
    save_evidence,
    save_transform_evidence,
    validate_evidence,
    validate_run_evidence,
    validate_transform_evidence,
)
from re_agent.build.recipe import BuildRecipe, BuildRunResult, run_recipe
from re_agent.build.run_lock import RunLock, RunLockError

__all__ = [
    "BuildEvidence",
    "BuildRecipe",
    "BuildRunResult",
    "TargetCheckpoint",
    "checkpoint_coverage",
    "coverage",
    "load_evidence",
    "missing_targets",
    "run_recipe",
    "RunLock",
    "RunLockError",
    "save_evidence",
    "validate_bulk_evidence",
    "validate_evidence",
    "validate_run_evidence",
    "TransformEvidence",
    "load_transform_evidence",
    "save_transform_evidence",
    "validate_transform_evidence",
]

__version__ = "2.0.0"
