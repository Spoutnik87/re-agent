"""Release 5 generic immutable promotion foundation."""

from re_agent.promotion.derive import (
    derive_project_from_bundles,
    derive_project_state,
    derive_target_state,
    revalidate_proof_bundle,
    revalidate_proof_evidence,
)
from re_agent.promotion.journal import PromotionBatch, PromotionJournal
from re_agent.promotion.models import ProjectState, PromotionState, ProofBundle, ProofEvidence, TargetState
from re_agent.promotion.service import PromotionResult, PromotionService
from re_agent.promotion.store import ImmutableEvidenceStore, PromotionViewPublisher

__all__ = [
    "ImmutableEvidenceStore",
    "ProjectState",
    "ProofBundle",
    "ProofEvidence",
    "PromotionBatch",
    "PromotionJournal",
    "PromotionState",
    "PromotionViewPublisher",
    "TargetState",
    "PromotionResult",
    "PromotionService",
    "derive_project_from_bundles",
    "derive_project_state",
    "derive_target_state",
    "revalidate_proof_bundle",
    "revalidate_proof_evidence",
]
