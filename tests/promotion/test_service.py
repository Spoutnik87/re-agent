"""Promotion-service transaction tests using generic fakes."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from re_agent.promotion.journal import PromotionJournal
from re_agent.promotion.models import PromotionState, ProofBundle, ProofEvidence
from re_agent.promotion.service import PromotionService, _Build, _Invocation
from re_agent.promotion.state import derive_project_from_bundles
from re_agent.promotion.store import ImmutableEvidenceStore, PromotionViewPublisher
from re_agent.toolchain.activation import VerifiedCommand


def _proof(target: str, proof_type: str) -> ProofBundle:
    return ProofBundle(
        "demo",
        target,
        "candidate-1",
        (
            ProofEvidence("compile", target, {"passed": True, "build": "candidate-1"}),
            ProofEvidence(proof_type, target, {"passed": True, "build": "candidate-1", "stage": "0"}),
        ),
    ).sealed()


def _complete_proof(target: str) -> ProofBundle:
    return ProofBundle(
        "demo",
        target,
        "candidate-1",
        tuple(
            [ProofEvidence("compile", target, {"passed": True, "build": "candidate-1"})]
            + [
                ProofEvidence(kind, target, {"passed": True, "build": "candidate-1", "stage": str(stage)})
                for kind, stage in (("abi", 0), ("abi", 1), ("differential", 0), ("differential", 1))
            ]
        ),
    ).sealed()


class _FakeService(PromotionService):
    def __init__(self, root: Path, targets: tuple[str, ...], failing: str | None = None) -> None:
        super().__init__(root / "project", promotion_root=root / "promotion")
        self.targets = targets
        self.failing = failing

    def _load_build(self, context, candidate):  # type: ignore[no-untyped-def]
        return _Build(
            SimpleNamespace(
                artifact="artifact", evidence="evidence", artifact_sha256="e" * 64, evidence_sha256="f" * 64
            ),
            SimpleNamespace(),
            self.project_root,
            "candidate-1",
        )

    def _verify_checkpoint(self, build, symbol):  # type: ignore[no-untyped-def]
        return None

    def _stage_inputs(self, staging, context, build, original):  # type: ignore[no-untyped-def]
        return None

    def _invoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        symbol = args[2]
        if symbol.name == self.failing:
            raise RuntimeError("simulated target failure")
        command = args[5]
        request = SimpleNamespace(payload=(("stage", str(args[6])),), to_json_bytes=lambda: b"{}")
        return _Invocation(command, request, SimpleNamespace(outcome="pass", as_dict=lambda: {}), "", "", ())

    def _verify_bundle_toolchain(self, bundle):  # type: ignore[no-untyped-def]
        return None


def _context(targets: tuple[str, ...]):
    symbols = tuple(SimpleNamespace(name=target, address=index) for index, target in enumerate(targets))
    return SimpleNamespace(
        identity=SimpleNamespace(name="demo", project_fingerprint="f" * 64, binary_sha256="b" * 64),
        verified_abi_manifest=SimpleNamespace(value=SimpleNamespace(symbols=symbols)),
    )


def test_all_target_failure_leaves_journal_and_pointer_unchanged(monkeypatch, tmp_path):
    targets = ("one", "two")
    service = _FakeService(tmp_path, targets, failing="two")
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: _context(targets))
    monkeypatch.setattr(
        service, "_resolve", lambda **kwargs: (VerifiedCommand(("abi",), "a"), VerifiedCommand(("check",), "b"))
    )

    with pytest.raises(RuntimeError, match="simulated"):
        service.inspect_abi()
    assert not (tmp_path / "promotion" / "journal.jsonl").exists()
    assert not (tmp_path / "promotion" / "active.json").exists()


def test_sequential_abi_then_differential_accumulates_and_promotes(monkeypatch, tmp_path):
    targets = ("one", "two")
    service = _FakeService(tmp_path, targets)
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: _context(targets))
    monkeypatch.setattr(
        service, "_resolve", lambda **kwargs: (VerifiedCommand(("abi",), "a"), VerifiedCommand(("check",), "b"))
    )
    monkeypatch.setattr(service, "_verified_input", lambda path, expected: "original.bin")

    service.inspect_abi()
    result = service.run_differential(original_binary_equivalent=tmp_path / "original.bin")
    assert len(result) == 2
    journal = (tmp_path / "promotion" / "journal.jsonl").read_text().splitlines()
    assert len(journal) == 2
    assert (tmp_path / "promotion" / "active.json").exists()
    assert all(item.project is not None for item in result)


def test_all_target_compile_proof_is_deduplicated_when_merging():
    service = object.__new__(PromotionService)
    current = _proof("one", "abi")
    incoming = _proof("one", "differential")
    merged = service._merge_bundle(current, incoming)
    assert [item.evidence_type for item in merged.evidence].count("compile") == 1
    assert {item.evidence_type for item in merged.evidence} == {"compile", "abi", "differential"}


def test_project_wide_merge_deduplicates_compile_for_every_target(monkeypatch, tmp_path):
    targets = ("one", "two")
    service = _FakeService(tmp_path, targets)
    context = _context(targets)
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: context)
    merged = tuple(service._merge_bundle(_proof(target, "abi"), _proof(target, "differential")) for target in targets)
    assert {item.target for item in merged} == set(targets)
    assert all(sum(item.evidence_type == "compile" for item in bundle.evidence) == 1 for bundle in merged)


def test_required_executable_identities_are_persisted(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: _context(("one",)))
    monkeypatch.setattr(
        service,
        "_resolve",
        lambda **kwargs: (VerifiedCommand(("inspector",), "a" * 64), VerifiedCommand(("verifier",), "b" * 64)),
    )
    service.inspect_abi(target="one")
    records = service.promotion_root / "journal.jsonl"
    digest = json.loads(records.read_text())["bundles"][0]
    stored = ImmutableEvidenceStore(service.promotion_root).get(digest)
    assert [item.payload["command_sha256"] for item in stored.evidence if item.evidence_type == "abi"] == [
        "a" * 64,
        "b" * 64,
    ]


def test_stage_invokes_both_commands_for_each_proof(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    calls: list[tuple[str, str]] = []

    def invoke(*args, **kwargs):
        command = args[5]
        proof_type = args[4]
        calls.append((proof_type, command.argv[0]))
        return SimpleNamespace(
            command=command,
            request=SimpleNamespace(payload=(("stage", str(args[6])),), to_json_bytes=lambda: b"{}"),
            result=SimpleNamespace(outcome="pass", as_dict=lambda: {}),
            stdout="",
            stderr="",
            attachments=(),
        )

    monkeypatch.setattr(service, "_invoke", invoke)
    context = _context(("one",))
    build = service._load_build(context, None)
    symbol = context.verified_abi_manifest.value.symbols[0]
    commands = (VerifiedCommand(("inspector",), "a" * 64), VerifiedCommand(("verifier",), "b" * 64))
    service._stage_bundle(context, build, symbol, "inspect_abi", "abi", commands, None, tmp_path)
    differential_commands = (VerifiedCommand(("harness",), "c" * 64), VerifiedCommand(("matcher",), "d" * 64))
    service._stage_bundle(
        context, build, symbol, "run_differential", "differential", differential_commands, "original.bin", tmp_path
    )
    assert calls == [
        ("abi", "inspector"),
        ("abi", "verifier"),
        ("differential", "harness"),
        ("differential", "matcher"),
    ]


def test_stage_bundle_persists_both_executable_identities(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    build = service._load_build(context, None)
    symbol = context.verified_abi_manifest.value.symbols[0]
    commands = (VerifiedCommand(("inspector",), "a" * 64), VerifiedCommand(("verifier",), "b" * 64))

    def invoke(*args, **kwargs):
        command = args[5]
        request = SimpleNamespace(payload=(("stage", str(args[6])),), to_json_bytes=lambda: b"{}")
        return _Invocation(command, request, SimpleNamespace(outcome="pass", as_dict=lambda: {}), "", "", ())

    monkeypatch.setattr(service, "_invoke", invoke)
    result = service._stage_bundle(context, build, symbol, "inspect_abi", "abi", commands, None, tmp_path)
    assert [item.payload["command_sha256"] for item in result.evidence if item.evidence_type == "abi"] == [
        "a" * 64,
        "b" * 64,
    ]


def test_original_binary_hash_is_retained_in_differential_request():
    binary_hash = "c" * 64
    request = SimpleNamespace(
        payload=(("stage", "differential"),),
        to_json_bytes=lambda: json.dumps({"hashes": {"original_binary_equivalent": binary_hash}}).encode(),
    )
    invocation = _Invocation(
        VerifiedCommand(("matcher",), "d" * 64),
        request,
        SimpleNamespace(outcome="pass", as_dict=lambda: {}),
        "",
        "",
        (),
    )
    service = object.__new__(PromotionService)
    context = _context(("one",))
    build = SimpleNamespace(identity="candidate-1", publication=SimpleNamespace(artifact_sha256="e" * 64))
    bundle = service._bundle(
        context, build, context.verified_abi_manifest.value.symbols[0], "differential", (invocation,)
    )
    payload = json.loads(bundle.evidence[-1].payload["request"])
    assert payload["hashes"]["original_binary_equivalent"] == binary_hash


def test_stage_two_request_binds_stage_one_result_and_attachments(tmp_path):
    service = object.__new__(PromotionService)
    content = b"stage-one-attachment"
    attachment_hash = hashlib.sha256(content).hexdigest()
    prior = _Invocation(
        VerifiedCommand(("inspector",), "a" * 64),
        SimpleNamespace(identity="request-one"),
        SimpleNamespace(as_dict=lambda: {"request_sha256": "request-one", "outcome": "pass"}),
        "",
        "",
        (
            {
                "path": "result.bin",
                "sha256": attachment_hash,
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        ),
    )
    paths, hashes, payload = service._stage_prior_result(tmp_path, prior)
    assert paths["stage1_result"] == "stage1/result.json"
    assert hashes["stage1_result"] == hashlib.sha256(b'{"outcome":"pass","request_sha256":"request-one"}\n').hexdigest()
    assert paths["stage1_attachment_0"] == "stage1/attachments/0-result.bin"
    assert hashes["stage1_attachment_0"] == attachment_hash
    assert payload["stage1_request_sha256"] == "request-one"
    assert (tmp_path / paths["stage1_attachment_0"]).read_bytes() == content


def test_persisted_stage_two_keeps_outer_binding_metadata(monkeypatch, tmp_path):
    service = object.__new__(PromotionService)
    metadata = {
        "stage1_result_sha256": "r" * 64,
        "stage1_request_sha256": "q" * 64,
        "stage1_attachment_hashes": json.dumps(
            {"stage1_attachment_0": "a" * 64, "stage1_attachment_1": "b" * 64}, sort_keys=True
        ),
    }
    request = SimpleNamespace(
        payload=tuple(sorted({**metadata, "stage": "1"}.items())),
        hashes=(
            ("stage1_attachment_0", "a" * 64),
            ("stage1_attachment_1", "b" * 64),
        ),
        to_json_bytes=lambda: b"{}",
    )
    invocation = _Invocation(
        VerifiedCommand(("verifier",), "b" * 64),
        request,
        SimpleNamespace(outcome="pass", as_dict=lambda: {}),
        "",
        "",
        (),
    )
    context = _context(("one",))
    build = SimpleNamespace(identity="candidate-1", publication=SimpleNamespace(artifact_sha256="e" * 64))
    bundle = service._bundle(context, build, context.verified_abi_manifest.value.symbols[0], "abi", (invocation,))
    persisted = bundle.evidence[-1].payload
    assert persisted["stage1_result_sha256"] == metadata["stage1_result_sha256"]
    assert persisted["stage1_request_sha256"] == metadata["stage1_request_sha256"]
    assert persisted["stage1_attachment_hashes"] == metadata["stage1_attachment_hashes"]


def test_aggregate_stage_one_attachment_hashes_are_revalidated(tmp_path):
    service = object.__new__(PromotionService)
    content = (b"first", b"second")
    attachments = tuple(
        {
            "path": f"result-{index}.bin",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size_bytes": len(value),
            "content_base64": base64.b64encode(value).decode("ascii"),
        }
        for index, value in enumerate(content)
    )
    prior = _Invocation(
        VerifiedCommand(("inspector",), "a" * 64),
        SimpleNamespace(identity="request-one"),
        SimpleNamespace(as_dict=lambda: {"outcome": "pass"}),
        "",
        "",
        attachments,
    )
    paths, hashes, _ = service._stage_prior_result(tmp_path, prior)
    assert set(paths) >= {"stage1_result", "stage1_attachment_0", "stage1_attachment_1"}
    assert hashes["stage1_attachment_0"] == attachments[0]["sha256"]
    assert hashes["stage1_attachment_1"] == attachments[1]["sha256"]


def test_stage_one_attachment_declaration_is_revalidated(tmp_path):
    service = object.__new__(PromotionService)
    content = b"not-the-declared-bytes"
    prior = _Invocation(
        VerifiedCommand(("inspector",), "a" * 64),
        SimpleNamespace(identity="request-one"),
        SimpleNamespace(as_dict=lambda: {"outcome": "pass"}),
        "",
        "",
        (
            {
                "path": "result.bin",
                "sha256": "0" * 64,
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        ),
    )
    with pytest.raises(ValueError, match="attachment content does not match"):
        service._stage_prior_result(tmp_path, prior)


def test_promote_project_collects_abi_and_differential_atomically(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one", "two"))
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: _context(("one", "two")))
    monkeypatch.setattr(service, "_verified_input", lambda path, context: "original.bin")
    command_calls: list[str] = []
    bundle_calls: list[str] = []

    def commands(capability):
        command_calls.append(capability)
        names = {
            "inspect_abi": ("inspector", "verifier"),
            "run_differential": ("harness", "matcher"),
        }[capability]
        return tuple(VerifiedCommand((name,), "a" * 64) for name in names)

    def complete(context, build, symbol, commands, original, staging):
        bundle_calls.append(symbol.name)
        return _complete_proof(symbol.name)

    monkeypatch.setattr(service, "_commands", commands)
    monkeypatch.setattr(service, "_complete_bundle", complete)
    result = service.promote(original_binary_equivalent=tmp_path / "original.bin")
    assert command_calls == ["inspect_abi", "run_differential"]
    assert bundle_calls == ["one", "two"]
    assert len(result) == 2
    assert (tmp_path / "promotion" / "active.json").exists()
    assert len((tmp_path / "promotion" / "journal.jsonl").read_text().splitlines()) == 1


def test_status_revalidates_current_project_build_and_proofs(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    calls: list[tuple[str, object]] = []
    original_load = service._load_build
    monkeypatch.setattr(
        "re_agent.promotion.service.load_verified_project",
        lambda root: calls.append(("project", root)) or context,
    )
    monkeypatch.setattr(
        service,
        "_load_build",
        lambda context, candidate: calls.append(("build", candidate)) or original_load(context, candidate),
    )
    monkeypatch.setattr(
        service,
        "_current_bundles",
        lambda context, candidate: calls.append(("proofs", candidate)) or {},
    )
    state = service.status()
    assert state.state.name == "STALE"
    assert [name for name, _ in calls] == ["project", "build", "proofs"]


def test_current_bundles_revalidates_proof_and_toolchain_identity(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    proof = _complete_proof("one")
    store = ImmutableEvidenceStore(service.promotion_root)
    digest = store.put(proof)
    PromotionJournal(service.promotion_root / "journal.jsonl").append(
        (proof,), project="demo", candidate="candidate-1", expected_targets=("one",)
    )
    seen: list[str] = []
    monkeypatch.setattr("re_agent.promotion.service.revalidate_proof_bundle", lambda bundle: seen.append("proof"))
    monkeypatch.setattr(service, "_verify_bundle_toolchain", lambda bundle: seen.append("toolchain"))
    current = service._current_bundles(context, "candidate-1")
    assert current["one"].bundle_sha256 == digest
    assert seen == ["proof", "toolchain"]


def test_stale_historical_stage_is_ignored_and_fresh_same_stage_replaces_it(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))

    def proof(profile):
        return ProofBundle(
            "demo",
            "one",
            "candidate-1",
            (
                ProofEvidence("compile", "one", {"passed": True, "build": "candidate-1"}),
                ProofEvidence("abi", "one", {"passed": True, "build": "candidate-1", "profile": profile}),
            ),
        ).sealed()

    stale = proof("old-profile")
    fresh = proof("fresh-profile")
    store = ImmutableEvidenceStore(service.promotion_root)
    store.put(stale)
    store.put(fresh)
    journal = PromotionJournal(service.promotion_root / "journal.jsonl")
    journal.append((stale,), project="demo", candidate="candidate-1", expected_targets=("one",))
    journal.append((fresh,), project="demo", candidate="candidate-1", expected_targets=("one",))
    monkeypatch.setattr("re_agent.promotion.service.revalidate_proof_bundle", lambda bundle: None)

    def verify(bundle):
        if bundle.evidence[-1].payload.get("profile") == "old-profile":
            raise ValueError("stale historical profile")

    monkeypatch.setattr(service, "_verify_bundle_toolchain", verify)
    current = service._current_bundles(context, "candidate-1")
    assert current["one"].evidence[-1].payload["profile"] == "fresh-profile"
    assert sum(item.evidence_type == "abi" for item in current["one"].evidence) == 1


def test_failed_active_publication_does_not_derive_promoted_and_preserves_prior_view(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: context)
    monkeypatch.setattr(service, "_verified_input", lambda path, expected: "original.bin")
    monkeypatch.setattr(
        service,
        "_commands",
        lambda capability: (
            VerifiedCommand((capability, "stage0"), "a" * 64),
            VerifiedCommand((capability, "stage1"), "b" * 64),
        ),
    )
    monkeypatch.setattr(service, "_complete_bundle", lambda *args: _complete_proof("one"))
    publisher = PromotionViewPublisher(service.promotion_root, auth_key="release-5")
    prior = derive_project_from_bundles("demo", "candidate-1", ("one",), (_complete_proof("one"),), "prior-batch")
    publisher.publish(prior)
    prior_pointer = (service.promotion_root / "active.json").read_bytes()

    monkeypatch.setattr(
        "re_agent.promotion.service.PromotionViewPublisher.publish",
        lambda *args: (_ for _ in ()).throw(ValueError("publication failed")),
    )
    with pytest.raises(ValueError, match="publication failed"):
        service.promote(original_binary_equivalent=tmp_path / "original.bin")
    assert (service.promotion_root / "active.json").read_bytes() == prior_pointer

    state = service.status()
    assert state.state is not PromotionState.PROMOTED


def test_orphan_evidence_is_ignored_until_committed_in_journal(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    proof = _complete_proof("one")
    store = ImmutableEvidenceStore(service.promotion_root)
    store.put(proof)
    monkeypatch.setattr("re_agent.promotion.service.revalidate_proof_bundle", lambda bundle: None)
    monkeypatch.setattr(service, "_verify_bundle_toolchain", lambda bundle: None)
    assert service._current_bundles(context, "candidate-1") == {}
    PromotionJournal(service.promotion_root / "journal.jsonl").append(
        (proof,), project="demo", candidate="candidate-1", expected_targets=("one",)
    )
    assert service._current_bundles(context, "candidate-1")["one"] == proof


@pytest.mark.parametrize("corruption", ["missing", "corrupt", "hash-invalid"])
def test_journal_referenced_invalid_bundle_fails_closed(monkeypatch, tmp_path, corruption):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    proof = _proof("one", "abi")
    store = ImmutableEvidenceStore(service.promotion_root)
    digest = store.put(proof)
    PromotionJournal(service.promotion_root / "journal.jsonl").append(
        (proof,), project="demo", candidate="candidate-1", expected_targets=("one",)
    )
    path = service.promotion_root / "bundles" / f"{digest}.json"
    if corruption == "missing":
        path.unlink()
    else:
        raw = json.loads(path.read_text())
        if corruption == "corrupt":
            raw["evidence"][1]["payload"]["stage"] = "9"
        else:
            raw["bundle_sha256"] = "0" * 64
        path.write_text(json.dumps(raw))
    monkeypatch.setattr("re_agent.promotion.service.revalidate_proof_bundle", lambda bundle: None)
    monkeypatch.setattr(service, "_verify_bundle_toolchain", lambda bundle: None)

    with pytest.raises((OSError, ValueError)):
        service._current_bundles(context, "candidate-1")


def test_journal_batches_do_not_cross_project_or_candidate(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    context = _context(("one",))
    journal = PromotionJournal(service.promotion_root / "journal.jsonl")
    store = ImmutableEvidenceStore(service.promotion_root)

    def variant(project, candidate):
        return ProofBundle(
            project,
            "one",
            candidate,
            (ProofEvidence("compile", "one", {"passed": True, "build": candidate}),),
        ).sealed()

    accepted = variant("demo", "candidate-1")
    other_candidate = variant("demo", "candidate-2")
    other_project = variant("other", "candidate-1")
    for proof, project, candidate in (
        (accepted, "demo", "candidate-1"),
        (other_candidate, "demo", "candidate-2"),
        (other_project, "other", "candidate-1"),
    ):
        store.put(proof)
        journal.append((proof,), project=project, candidate=candidate, expected_targets=("one",))
    monkeypatch.setattr("re_agent.promotion.service.revalidate_proof_bundle", lambda bundle: None)
    monkeypatch.setattr(service, "_verify_bundle_toolchain", lambda bundle: None)
    current = service._current_bundles(context, "candidate-1")
    assert current == {"one": accepted}


def test_adapter_staging_is_external_and_operation_scoped(monkeypatch, tmp_path):
    service = _FakeService(tmp_path, ("one",))
    monkeypatch.setattr("re_agent.promotion.service.load_verified_project", lambda root: _context(("one",)))
    monkeypatch.setattr(
        service,
        "_resolve",
        lambda **kwargs: (VerifiedCommand(("adapter-a",), "a" * 64), VerifiedCommand(("adapter-b",), "b" * 64)),
    )
    captured: list[Path] = []

    def stage_bundle(context, build, symbol, capability, proof_type, commands, original, staging):
        captured.append(staging)
        return _proof(symbol.name, proof_type)

    monkeypatch.setattr(service, "_stage_bundle", stage_bundle)
    service.inspect_abi()
    assert captured and captured[0].parent == service.promotion_root
    assert service.project_root not in captured[0].parents
    assert not captured[0].exists()


def test_nested_source_and_object_paths_validate(tmp_path):
    service = object.__new__(PromotionService)
    service.project_root = (tmp_path / "project").resolve()
    build_dir = service.project_root / "build"
    source = build_dir / "nested" / "source.cpp"
    object_file = build_dir / "nested" / "source.o"
    source.parent.mkdir(parents=True)
    object_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_source = service.project_root / "snapshot" / "nested" / "source.cpp"
    snapshot_source.parent.mkdir(parents=True)
    snapshot_source.write_text("wrong published source", encoding="utf-8")
    source.write_text("void nested() {}", encoding="utf-8")
    object_file.write_bytes(b"object")

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    context = SimpleNamespace(snapshot_root=service.project_root / "snapshot")
    evidence = SimpleNamespace(
        targets=(
            SimpleNamespace(
                address=1,
                name="nested",
                output_path="nested/source.cpp",
                source_sha256=digest(source),
                object_sha256=digest(object_file),
            ),
        )
    )
    symbol = SimpleNamespace(address=1, name="nested")
    service._verify_checkpoint(context, build_dir, evidence, symbol)
