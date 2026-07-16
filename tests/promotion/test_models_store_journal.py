"""Release 5 promotion value, evidence-store, and journal contracts."""

from __future__ import annotations

import json

import pytest

from re_agent.promotion.journal import PromotionJournal
from re_agent.promotion.models import PromotionState, ProofBundle, ProofEvidence
from re_agent.promotion.state import derive_project_from_bundles, derive_project_state, derive_target_state
from re_agent.promotion.store import ImmutableEvidenceStore, PromotionViewPublisher


def bundle(target: str = "target", *, proof: str = "abi", passed: bool = True) -> ProofBundle:
    return ProofBundle(
        "demo",
        target,
        "candidate-1",
        (
            ProofEvidence("compile", target, {"passed": True, "build": "candidate-1"}),
            ProofEvidence(proof, target, {"passed": passed, "build": "candidate-1", "stage": "0"}),
        ),
    ).sealed()


def complete_bundle(target: str = "target") -> ProofBundle:
    return ProofBundle(
        "demo",
        target,
        "candidate-1",
        (
            ProofEvidence("compile", target, {"passed": True, "build": "candidate-1"}),
            ProofEvidence("abi", target, {"passed": True, "build": "candidate-1", "stage": "0"}),
            ProofEvidence("abi", target, {"passed": True, "build": "candidate-1", "stage": "1"}),
            ProofEvidence("differential", target, {"passed": True, "build": "candidate-1", "stage": "0"}),
            ProofEvidence("differential", target, {"passed": True, "build": "candidate-1", "stage": "1"}),
        ),
    ).sealed()


def test_proof_bundle_is_sealed_and_no_replace(tmp_path):
    store = ImmutableEvidenceStore(tmp_path / "promotion")
    proof = bundle()
    digest = store.put(proof)

    assert store.get(digest) == proof
    with pytest.raises(FileExistsError):
        store.put(proof)


def test_tampered_bundle_is_rejected_as_invalid(tmp_path):
    store = ImmutableEvidenceStore(tmp_path / "promotion")
    digest = store.put(bundle())
    path = tmp_path / "promotion" / "bundles" / f"{digest}.json"
    raw = json.loads(path.read_text())
    raw["evidence"][1]["payload"]["passed"] = False
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="invalid proof evidence hash|invalid proof bundle hash"):
        store.get(digest)


def test_hash_chain_corruption_is_rejected(tmp_path):
    journal = PromotionJournal(tmp_path / "journal.jsonl")
    journal.append((bundle(),), project="demo", candidate="candidate-1", expected_targets=("target",))
    path = tmp_path / "journal.jsonl"
    raw = json.loads(path.read_text())
    raw["bundles"] = ["tampered"]
    path.write_text(json.dumps(raw) + "\n")

    with pytest.raises(ValueError, match="malformed promotion batch bundle identity|invalid promotion journal record"):
        journal.records()


@pytest.mark.parametrize("proof", ["compile", "abi", "differential"])
def test_compile_only_abi_only_and_diff_only_never_promote(proof):
    evidence = (ProofEvidence(proof, "target", {"passed": True, "build": "candidate-1"}),)
    state = derive_target_state(ProofBundle("demo", "target", "candidate-1", evidence).sealed())
    assert state.state is not PromotionState.PROMOTED
    assert (
        derive_project_state("demo", "candidate-1", (state,), batch_hash="batch").state is not PromotionState.PROMOTED
    )


def test_lone_adapter_stage_never_counts_as_complete_proof():
    proof = ProofBundle(
        "demo",
        "target",
        "candidate-1",
        (
            ProofEvidence("compile", "target", {"passed": True, "build": "candidate-1"}),
            ProofEvidence("abi", "target", {"passed": True, "build": "candidate-1", "stage": "0"}),
        ),
    ).sealed()
    state = derive_target_state(proof)
    assert state.state is PromotionState.ABI_PASS
    assert state.state is not PromotionState.PROMOTED


def test_differential_only_is_not_project_promoted_without_batch():
    proof = ProofBundle(
        "demo",
        "target",
        "candidate-1",
        (ProofEvidence("differential", "target", {"passed": True, "build": "candidate-1"}),),
    ).sealed()
    state = derive_project_from_bundles("demo", "candidate-1", ("target",), (proof,))
    assert state.state is PromotionState.INVALID
    assert state.state is not PromotionState.PROMOTED


def test_stale_identity_and_missing_target_are_invalid():
    proof = bundle()
    target = derive_target_state(proof)
    stale = type(target)(target.project, target.target, "other-candidate", target.state, target.bundle_sha256)
    assert derive_project_state("demo", "candidate-1", (stale,)).state is PromotionState.INVALID
    assert derive_project_from_bundles("demo", "candidate-1", ("target",), (proof,)).state is PromotionState.ABI_PASS
    assert (
        derive_project_from_bundles("demo", "candidate-1", ("target", "missing"), (proof,)).state
        is PromotionState.INVALID
    )
    assert (
        derive_project_from_bundles("demo", "candidate-1", ("target",), (proof,), "batch").state
        is PromotionState.ABI_PASS
    )
    assert stale.candidate != "candidate-1"


def test_promotion_view_publishes_active_summary(tmp_path):
    state = derive_project_from_bundles("demo", "candidate-1", ("target",), (complete_bundle("target"),), "batch")
    assert state.state is PromotionState.PROMOTED
    publisher = PromotionViewPublisher(tmp_path / "promotion")
    digest = publisher.publish(state)
    active = json.loads((tmp_path / "promotion" / "active.json").read_text())
    assert active["format_version"] == 1
    assert active["summary_id"] == active["summary_sha256"] == digest
    assert active["authentication"]["algorithm"] == "sha256"
    assert publisher.load_active()["state"] == PromotionState.PROMOTED


def test_active_pointer_rejects_tampered_existing_summary(tmp_path):
    publisher = PromotionViewPublisher(tmp_path / "promotion", auth_key="release-5")
    state = derive_project_from_bundles("demo", "candidate-1", ("target",), (complete_bundle("target"),), "batch")
    digest = publisher.publish(state)
    summary = tmp_path / "promotion" / "summaries" / f"{digest}.json"
    raw = json.loads(summary.read_text())
    raw["candidate"] = "tampered"
    summary.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="invalid active promotion pointer"):
        publisher.load_active()


def test_active_pointer_preserves_previous_value_when_publish_fails(monkeypatch, tmp_path):
    publisher = PromotionViewPublisher(tmp_path / "promotion", auth_key="release-5")
    first = derive_project_from_bundles("demo", "candidate-1", ("target",), (complete_bundle("target"),), "batch-1")
    publisher.publish(first)
    pointer = tmp_path / "promotion" / "active.json"
    previous = pointer.read_bytes()
    second = derive_project_from_bundles("demo", "candidate-1", ("target",), (complete_bundle("target"),), "batch-2")
    monkeypatch.setattr(publisher, "_publish_summary", lambda *args: (_ for _ in ()).throw(RuntimeError("disk full")))

    with pytest.raises(RuntimeError, match="disk full"):
        publisher.publish(second)
    assert pointer.read_bytes() == previous


def test_locked_active_pointer_rejects_replacement(tmp_path):
    publisher = PromotionViewPublisher(tmp_path / "promotion")
    state = derive_project_from_bundles("demo", "candidate-1", ("target",), (complete_bundle("target"),), "batch")
    publisher.publish(state)
    lock = tmp_path / "promotion" / "active.json.lock"
    lock.write_text("held")

    with pytest.raises(ValueError, match="already being published"):
        publisher.publish(state)
