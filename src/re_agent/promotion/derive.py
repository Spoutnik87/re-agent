"""Fail-closed Release 5 derivation rules."""

from __future__ import annotations

from collections.abc import Iterable

from re_agent.promotion.models import ProjectState, PromotionState, ProofBundle, TargetState

_REQUIRED = frozenset(("compile", "abi", "differential"))


def _passed_proofs(bundle: ProofBundle) -> tuple[frozenset[str], str] | None:
    bundle.verify()
    builds: set[str] = set()
    passed: set[str] = set()
    for evidence in bundle.evidence:
        revalidate_proof_evidence(evidence, subject=bundle.target, build_identity=bundle.candidate)
        if evidence.payload.get("passed") is not True:
            continue
        build = evidence.payload.get("build")
        if not isinstance(build, str):
            return None
        builds.add(build)
        passed.add(evidence.evidence_type)
    if len(builds) != 1 or builds != {bundle.candidate}:
        return None
    return frozenset(passed), bundle.candidate


def derive_target_state(bundle: ProofBundle) -> TargetState:
    """Derive one target without allowing proof types or builds to be mixed."""
    try:
        result = _passed_proofs(bundle)
    except TypeError, ValueError:
        result = None
    if result is None:
        return TargetState(
            bundle.project, bundle.target, bundle.candidate, PromotionState.INVALID, bundle.bundle_sha256
        )
    proof_types, build = result
    if proof_types >= _REQUIRED:
        state = PromotionState.DIFFERENTIAL_PASS
    elif {"compile", "abi"} <= proof_types:
        state = PromotionState.ABI_PASS
    elif proof_types == {"compile"}:
        state = PromotionState.COMPILE_PASS
    else:
        state = PromotionState.INVALID
    return TargetState(
        bundle.project, bundle.target, bundle.candidate, state, bundle.bundle_sha256, tuple(sorted(proof_types)), build
    )


def derive_project_state(
    project: str, candidate: str, targets: Iterable[TargetState], batch_hash: str = ""
) -> ProjectState:
    """Promote only a complete, same-build compile/ABI/differential set."""
    values = tuple(sorted(targets, key=lambda item: item.target))
    if (
        not values
        or any(item.project != project or item.candidate != candidate for item in values)
        or any(item.state is PromotionState.INVALID for item in values)
    ):
        state = PromotionState.INVALID
    elif all(set(item.proof_types) >= _REQUIRED and item.build_identity == candidate for item in values):
        state = PromotionState.PROMOTED if batch_hash else PromotionState.DIFFERENTIAL_PASS
    elif all(set(item.proof_types) >= {"compile", "abi"} and item.build_identity == candidate for item in values):
        state = PromotionState.ABI_PASS
    elif all(set(item.proof_types) == {"compile"} and item.build_identity == candidate for item in values):
        state = PromotionState.COMPILE_PASS
    else:
        state = PromotionState.STALE
    return ProjectState(project, candidate, state, values, batch_hash)


def derive_project_from_bundles(
    project: str, candidate: str, expected_targets: Iterable[str], bundles: Iterable[ProofBundle], batch_hash: str = ""
) -> ProjectState:
    by_target = {bundle.target: bundle for bundle in bundles}
    states = tuple(
        derive_target_state(by_target[target])
        if target in by_target
        else TargetState(project, target, candidate, PromotionState.INVALID)
        for target in sorted(set(expected_targets))
    )
    return derive_project_state(project, candidate, states, batch_hash)


def revalidate_proof_evidence(evidence: object, *, subject: str, build_identity: str) -> None:
    """Revalidate proof identity before deriving or publishing a later view."""
    from re_agent.promotion.models import ProofEvidence

    if not isinstance(evidence, ProofEvidence):
        raise ValueError("invalid proof evidence type")
    evidence.verify()
    if evidence.subject != subject or evidence.payload.get("build") != build_identity:
        raise ValueError("proof evidence identity is stale")


def revalidate_proof_bundle(bundle: ProofBundle) -> None:
    """Revalidate a bundle and all of its target/build-bound proof identities."""
    bundle.verify()
    for evidence in bundle.evidence:
        revalidate_proof_evidence(evidence, subject=bundle.target, build_identity=bundle.candidate)


__all__ = [
    "derive_project_from_bundles",
    "derive_project_state",
    "derive_target_state",
    "revalidate_proof_bundle",
    "revalidate_proof_evidence",
]
