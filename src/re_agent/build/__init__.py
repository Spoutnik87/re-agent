"""Build primitives for manifest-bound reconstruction."""

from re_agent.build.bulk import checkpoint_coverage, missing_targets, validate_bulk_evidence
from re_agent.build.evidence import (
    BuildEvidence,
    TargetCheckpoint,
    coverage,
    load_evidence,
    save_evidence,
    validate_evidence,
    validate_run_evidence,
)
from re_agent.build.recipe import BuildRecipe, BuildRunResult, run_recipe

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
    "save_evidence",
    "validate_bulk_evidence",
    "validate_evidence",
    "validate_run_evidence",
]

__version__ = "2.0.0"
