"""Fail-closed Release 5 promotion orchestration."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from re_agent.adapters import (
    AdapterAttachment,
    AdapterCommand,
    AdapterExecution,
    AdapterRequest,
    AdapterResult,
    execute_adapter_with_evidence,
)
from re_agent.build import (
    BuildEvidence,
    load_evidence,
    load_transform_evidence,
    validate_evidence,
    validate_transform_evidence,
)
from re_agent.contracts import Symbol
from re_agent.project import BuildPublication, VerifiedProjectContext, load_active_build, load_verified_project
from re_agent.project.snapshot import sha256_file
from re_agent.promotion.derive import (
    derive_project_state,
    derive_target_state,
    revalidate_proof_bundle,
)
from re_agent.promotion.journal import PromotionBatch, PromotionJournal
from re_agent.promotion.lock import PromotionLock
from re_agent.promotion.models import (
    ProjectState,
    PromotionState,
    ProofBundle,
    ProofEvidence,
    TargetState,
    canonical_target,
    parse_target,
)
from re_agent.promotion.store import ImmutableEvidenceStore, PromotionViewPublisher
from re_agent.toolchain.activation import VerifiedCommand, resolve_capability, verify_command


@dataclass(frozen=True, slots=True)
class PromotionResult:
    bundle: ProofBundle
    bundle_sha256: str
    batch: PromotionBatch | None
    project: ProjectState | None


@dataclass(frozen=True, slots=True)
class _Build:
    publication: BuildPublication
    evidence: BuildEvidence
    directory: Path
    identity: str


@dataclass(frozen=True, slots=True)
class _Invocation:
    command: VerifiedCommand
    request: AdapterRequest
    result: AdapterResult
    stdout: str
    stderr: str
    attachments: tuple[dict[str, object], ...]


class _StaleEvidenceError(ValueError):
    """Historical evidence no longer matches the current project/toolchain."""


class _CorruptEvidenceError(ValueError):
    """Journal-referenced evidence is malformed, unsafe, or tampered with."""


class PromotionService:
    """Promote verified Release 4 builds using only authenticated adapters.

    ``promotion_root`` is deliberately mandatory and must be an isolated
    caller-owned directory outside the project tree.  It is used for both
    immutable evidence and per-operation adapter staging.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        promotion_root: Path,
        build_root: Path | None = None,
        profile_path: Path | None = None,
        timeout_seconds: float = 60.0,
        execute: Callable[..., AdapterExecution] = execute_adapter_with_evidence,
        resolve: Callable[..., tuple[VerifiedCommand, ...]] = resolve_capability,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.promotion_root = Path(promotion_root).resolve()
        if self.promotion_root == self.project_root or self.project_root in self.promotion_root.parents:
            raise ValueError("promotion_root must be outside project_root")
        if self.promotion_root.is_symlink():
            raise ValueError("promotion_root must not be a symlink")
        self.promotion_root.mkdir(parents=True, exist_ok=True)
        self.build_root = Path(build_root or self.project_root / "build").resolve()
        self.profile_path = profile_path
        self.timeout_seconds = timeout_seconds
        self._execute = execute
        self._resolve = resolve

    def inspect_abi(
        self, *, target: str | None = None, candidate: str | None = None
    ) -> PromotionResult | tuple[PromotionResult, ...]:
        with PromotionLock(self.promotion_root):
            return self._run_stage("inspect_abi", "abi", target=target, candidate=candidate)

    def run_differential(
        self,
        *,
        original_binary_equivalent: Path,
        target: str | None = None,
        candidate: str | None = None,
    ) -> PromotionResult | tuple[PromotionResult, ...]:
        with PromotionLock(self.promotion_root):
            return self._run_stage(
                "run_differential",
                "differential",
                target=target,
                candidate=candidate,
                original=Path(original_binary_equivalent),
            )

    def promote(
        self,
        *,
        original_binary_equivalent: Path,
        target: str | None = None,
        candidate: str | None = None,
    ) -> tuple[PromotionResult, ...]:
        """Run ABI and differential proofs before mutating any journal/pointer.

        This is the atomic all-target entry point.  A single target also gets
        both proofs in one operation; partial proof bundles are never published
        by this method.
        """
        context = load_verified_project(self.project_root)
        build = self._load_build(context, candidate)
        symbols = self._select_symbols(context, None)
        original_rel = self._verified_input(Path(original_binary_equivalent), context)
        commands = self._commands("inspect_abi") + self._commands("run_differential")
        with (
            PromotionLock(self.promotion_root),
            tempfile.TemporaryDirectory(prefix="promotion-", dir=self.promotion_root) as raw,
        ):
            staging = Path(raw)
            self._stage_inputs(staging, context, build, original_rel)
            bundles = tuple(
                self._complete_bundle(context, build, symbol, commands, original_rel, staging) for symbol in symbols
            )
            return self._commit(context, build, bundles, None)

    def status(self, *, candidate: str | None = None) -> ProjectState:
        """Derive status from the current verified project/build, not history alone."""
        with PromotionLock(self.promotion_root):
            return self._status_unlocked(candidate)

    def _status_unlocked(self, candidate: str | None = None) -> ProjectState:
        """Status derivation without lock (caller must hold PromotionLock)."""
        context = load_verified_project(self.project_root)
        try:
            build = self._load_build(context, candidate)
        except (OSError, TypeError, ValueError) as exc:
            targets = tuple(
                TargetState(context.identity.name, symbol.name, candidate or "", PromotionState.INVALID)
                for symbol in context.verified_abi_manifest.value.symbols
            )
            if not targets:
                raise ValueError("project manifest has no targets") from exc
            return ProjectState(context.identity.name, candidate or "", PromotionState.INVALID, targets)
        bundles = self._current_bundles(context, build.identity)
        states: list[TargetState] = []
        for symbol in context.verified_abi_manifest.value.symbols:
            bundle = bundles.get(symbol.name)
            if bundle is None:
                states.append(TargetState(context.identity.name, symbol.name, build.identity, PromotionState.STALE))
            elif self._bundle_has_complete_stages(bundle):
                states.append(derive_target_state(bundle))
            else:
                states.append(
                    TargetState(
                        context.identity.name,
                        symbol.name,
                        build.identity,
                        PromotionState.STALE,
                        bundle.bundle_sha256,
                    )
                )
        complete = len(states) == len(tuple(context.verified_abi_manifest.value.symbols)) and all(
            self._bundle_has_complete_stages(bundles[symbol.name])
            for symbol in context.verified_abi_manifest.value.symbols
            if symbol.name in bundles
        )
        batch = self._batch_for_current(context, build.identity, bundles)
        if complete and batch is not None and self._active_view_matches(context, build.identity, batch, bundles):
            batch_hash = batch.record_hash
        else:
            batch_hash = ""
        return derive_project_state(context.identity.name, build.identity, states, batch_hash=batch_hash)

    def _run_stage(
        self,
        capability: str,
        proof_type: str,
        *,
        target: str | None,
        candidate: str | None,
        original: Path | None = None,
    ) -> PromotionResult | tuple[PromotionResult, ...]:
        context = load_verified_project(self.project_root)
        build = self._load_build(context, candidate)
        symbols = self._select_symbols(context, target)
        original_rel = self._verified_input(original, context) if original is not None else None
        commands = self._commands(capability)
        with tempfile.TemporaryDirectory(prefix="promotion-", dir=self.promotion_root) as raw:
            staging = Path(raw)
            self._stage_inputs(staging, context, build, original_rel)
            bundles = tuple(
                self._stage_bundle(context, build, symbol, capability, proof_type, commands, original_rel, staging)
                for symbol in symbols
            )
            committed = self._commit(context, build, bundles, target)
        return committed[0] if len(committed) == 1 and target is not None else committed

    def _commands(self, capability: str) -> tuple[VerifiedCommand, VerifiedCommand]:
        commands = self._resolve(project_root=self.project_root, capability=capability, profile_path=self.profile_path)
        if len(commands) != 2:
            raise ValueError(f"{capability} must resolve exactly two verified commands")
        return commands[0], commands[1]

    def _select_symbols(self, context: VerifiedProjectContext, target: str | None) -> tuple[Symbol, ...]:
        symbols = tuple(context.verified_abi_manifest.value.symbols)
        if target is not None:
            parsed_addr, parsed_name = parse_target(target)
            selected = tuple(s for s in symbols if s.address == parsed_addr and s.name == parsed_name)
        else:
            selected = symbols
        if not selected or (target is not None and len(selected) != 1):
            raise ValueError("unknown or ambiguous promotion target")
        return selected

    def _load_build(self, context: VerifiedProjectContext, candidate: str | None) -> _Build:
        publication = load_active_build(self.build_root) if candidate is None else self._selected_build(candidate)
        evidence_path = self._contained_build_file(publication.directory, publication.evidence)
        artifact_path = self._contained_build_file(publication.directory, publication.artifact)
        evidence = load_evidence(evidence_path, validate_success=False)
        expected = tuple((symbol.address, symbol.name) for symbol in context.verified_abi_manifest.value.symbols)
        validate_evidence(
            evidence,
            expected,
            project_fingerprint=context.identity.project_fingerprint,
            manifest_sha256=context.verified_abi_manifest.canonical_sha256,
        )
        if sha256_file(artifact_path) != publication.artifact_sha256:
            raise ValueError("published artifact hash mismatch")
        for symbol in context.verified_abi_manifest.value.symbols:
            self._verify_checkpoint(context, publication.directory, evidence, symbol)
        if evidence.schema_version >= 2:
            self._verify_transform_evidence(context, publication.directory, evidence)
        return _Build(publication, evidence, publication.directory, publication.publication_id)

    def _selected_build(self, candidate: str) -> BuildPublication:
        if not candidate or Path(candidate).name != candidate or candidate in {".", ".."}:
            raise ValueError("invalid candidate build identity")
        directory = self.build_root / "builds" / candidate
        if not (directory / "artifact").is_file() or not (directory / "evidence").is_file():
            raise ValueError("selected candidate build is absent")
        artifact = self._contained_build_file(directory, "artifact")
        evidence = self._contained_build_file(directory, "evidence")
        return BuildPublication(candidate, directory, sha256_file(artifact), sha256_file(evidence))

    def _verify_checkpoint(
        self, context: VerifiedProjectContext, build_directory: Path, evidence: BuildEvidence, symbol: Symbol
    ) -> None:
        checkpoint = next(
            (item for item in evidence.targets if item.address == symbol.address and item.name == symbol.name), None
        )
        if checkpoint is None:
            raise ValueError(f"build evidence does not cover target {symbol.name}")
        source_file = self._contained_build_file(build_directory, checkpoint.output_path)
        object_file = self._contained_build_file(
            build_directory, PurePosixPath(checkpoint.output_path).with_suffix(".o").as_posix()
        )
        source_hash = checkpoint.source_sha256
        generated_hash = getattr(checkpoint, "generated_sha256", source_hash)
        output_hash = getattr(checkpoint, "output_sha256", source_hash)
        if (
            source_file.is_symlink()
            or not source_file.is_file()
            or sha256_file(source_file) != source_hash
            or getattr(evidence, "schema_version", 1) >= 2
            and not (source_hash == generated_hash == output_hash)
        ):
            raise ValueError(f"source hash is absent or stale for {symbol.name}")
        if (
            object_file.is_symlink()
            or not object_file.is_file()
            or sha256_file(object_file) != checkpoint.object_sha256
        ):
            raise ValueError(f"object hash is absent or stale for {symbol.name}")

    def _verify_transform_evidence(
        self, context: VerifiedProjectContext, build_directory: Path, evidence: BuildEvidence
    ) -> None:
        scheduled = {
            (symbol.address, symbol.name): (symbol, index)
            for index, symbol in enumerate(
                sorted(
                    context.verified_abi_manifest.value.symbols,
                    key=lambda item: (item.address, item.name),
                )
            )
        }
        for checkpoint in evidence.targets:
            scheduled_target = scheduled.get(checkpoint.key())
            if scheduled_target is None:
                raise ValueError("transform evidence target is not in the verified manifest")
            symbol, index = scheduled_target
            transform_path = self._contained_build_file(build_directory, checkpoint.transform_evidence_path)
            if sha256_file(transform_path) != checkpoint.transform_evidence_sha256:
                raise ValueError(f"transform evidence hash mismatch for {checkpoint.name}")
            transform = load_transform_evidence(transform_path)
            validate_transform_evidence(transform)
            if (
                transform.project_fingerprint != context.identity.project_fingerprint
                or transform.snapshot_fingerprint != context.identity.snapshot_manifest_sha256
                or transform.manifest_raw_sha256 != context.verified_abi_manifest.raw_sha256
                or transform.manifest_sha256 != context.verified_abi_manifest.canonical_sha256
                or transform.run_id != f"{evidence.run_id}-{index}"
            ):
                raise ValueError(f"transform evidence project identity mismatch for {checkpoint.name}")
            if (
                transform.target_address != symbol.address
                or transform.target_name != symbol.name
                or transform.target_signature != symbol.signature
                or transform.target_calling_convention != symbol.calling_convention.value
                or transform.target_output_path != checkpoint.output_path
            ):
                raise ValueError(f"transform evidence target identity mismatch for {checkpoint.name}")
            if (
                transform.input_sha256 != checkpoint.input_sha256
                or transform.generated_sha256 != checkpoint.generated_sha256
                or transform.generated_sha256 != checkpoint.output_sha256
                or transform.object_sha256 != checkpoint.object_sha256
            ):
                raise ValueError(f"transform evidence artifact identity mismatch for {checkpoint.name}")

    @staticmethod
    def _contained_build_file(root: Path, relative: str) -> Path:
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("unsafe transform evidence path")
        root_resolved = root.resolve()
        current = root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError("transform evidence path contains a link")
        resolved = current.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("transform evidence path escapes build publication") from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError("transform evidence file is absent or unsafe")
        return resolved

    def _verified_input(self, path: Path | None, context: VerifiedProjectContext) -> str:
        if path is None:
            raise ValueError("original-binary-equivalent is required")
        resolved = path.resolve()
        if resolved.is_symlink() or not resolved.is_file() or sha256_file(resolved) != context.identity.binary_sha256:
            raise ValueError("original-binary-equivalent does not match project binary identity")
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise ValueError("original-binary-equivalent must be inside project root") from exc

    def _stage_inputs(
        self, staging: Path, context: VerifiedProjectContext, build: _Build, original: str | None
    ) -> None:
        files = [
            (build.directory / build.publication.artifact, Path("build/artifact")),
            (context.abi_manifest_path, Path("manifest.json")),
        ]
        for symbol in context.verified_abi_manifest.value.symbols:
            checkpoint = next(item for item in build.evidence.targets if item.name == symbol.name)
            files.extend(
                (
                    (build.directory / checkpoint.output_path, Path("sources") / checkpoint.output_path),
                    (
                        build.directory / Path(checkpoint.output_path).with_suffix(".o"),
                        Path("objects") / Path(checkpoint.output_path).with_suffix(".o"),
                    ),
                )
            )
        if original is not None:
            files.append((self.project_root / original, Path("original") / Path(original).name))
        for source, relative in files:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    def _stage_bundle(
        self,
        context: VerifiedProjectContext,
        build: _Build,
        symbol: Symbol,
        capability: str,
        proof_type: str,
        commands: tuple[VerifiedCommand, ...],
        original: str | None,
        staging: Path,
    ) -> ProofBundle:
        invocations: list[_Invocation] = []
        for index, command in enumerate(commands):
            prior = invocations[-1] if invocations else None
            invocation = self._invoke(
                context, build, symbol, capability, proof_type, command, index, original, staging, prior=prior
            )
            if invocation.result.outcome != "pass":
                raise RuntimeError(f"{proof_type} proof requires both adapter stages to pass")
            invocations.append(invocation)
        return self._bundle(context, build, symbol, proof_type, tuple(invocations))

    def _complete_bundle(
        self,
        context: VerifiedProjectContext,
        build: _Build,
        symbol: Symbol,
        commands: tuple[VerifiedCommand, ...],
        original: str,
        staging: Path,
    ) -> ProofBundle:
        abi = self._stage_bundle(context, build, symbol, "inspect_abi", "abi", commands[:2], None, staging)
        differential = self._stage_bundle(
            context, build, symbol, "run_differential", "differential", commands[2:], original, staging
        )
        return self._merge_bundle(abi, differential)

    def _invoke(
        self,
        context: VerifiedProjectContext,
        build: _Build,
        symbol: Symbol,
        capability: str,
        proof_type: str,
        command: VerifiedCommand,
        index: int,
        original: str | None,
        staging: Path,
        *,
        prior: _Invocation | None = None,
    ) -> _Invocation:
        paths = {
            "artifact": "build/artifact",
            "manifest": "manifest.json",
            "source": f"sources/{symbol.output_path}",
            "object": f"objects/{Path(symbol.output_path).with_suffix('.o').as_posix()}",
        }
        if original is not None:
            paths["original_binary_equivalent"] = f"original/{Path(original).name}"
        payload = {"address": str(symbol.address), "name": symbol.name, "stage": str(index)}
        prior_hashes: dict[str, str] = {}
        if prior is not None:
            prior_paths, prior_hashes, prior_payload = self._stage_prior_result(staging, prior)
            paths.update(prior_paths)
            payload.update(prior_payload)
        checkpoint = next(item for item in build.evidence.targets if item.name == symbol.name)
        request = AdapterRequest(
            capability,
            proof_type,
            AdapterCommand(command.argv, command.executable_sha256),
            context.identity.project_fingerprint,
            context.identity.snapshot_manifest_sha256,
            context.verified_abi_manifest.raw_sha256,
            build.identity,
            tuple(sorted(paths.items())),
            tuple(
                sorted(
                    {
                        "artifact": build.publication.artifact_sha256,
                        "build_evidence": build.publication.evidence_sha256,
                        "source": checkpoint.source_sha256,
                        "object": checkpoint.object_sha256,
                        "original_binary_equivalent": context.identity.binary_sha256
                        if original is not None
                        else hashlib.sha256(b"").hexdigest(),
                        **(prior_hashes if prior is not None else {}),
                    }.items()
                )
            ),
            tuple(sorted(payload.items())),
        )
        bound_request = request.with_input_hashes(staging)
        execution = self._execute(
            command, bound_request, staging, staging=staging, timeout_seconds=self.timeout_seconds
        )
        result, stdout, stderr = execution.result, execution.stdout, execution.stderr
        if result.request_sha256 != bound_request.identity:
            raise ValueError("adapter result is not bound to its request")
        attachments = tuple(
            self._attachment_file(path, item)
            for path, item in zip(execution.evidence.attachments, result.attachments, strict=True)
        )
        return _Invocation(command, bound_request, result, stdout, stderr, attachments)

    def _stage_prior_result(
        self, staging: Path, prior: _Invocation
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        result_path = Path("stage1/result.json")
        result_bytes = self._canonical_json(prior.result.as_dict())
        destination = staging / result_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result_bytes)
        paths = {"stage1_result": result_path.as_posix()}
        hashes = {"stage1_result": hashlib.sha256(result_bytes).hexdigest()}
        payload = {
            "stage1_result_sha256": hashes["stage1_result"],
            "stage1_request_sha256": prior.request.identity,
            "stage1_attachment_hashes": self._canonical_json({}).decode(),
        }
        for index, attachment in enumerate(prior.attachments):
            relative = Path("stage1/attachments") / f"{index}-{Path(str(attachment['path'])).name}"
            attachment_path = staging / relative
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(str(attachment["content_base64"]))
            attachment_path.write_bytes(content)
            key = f"stage1_attachment_{index}"
            digest = str(attachment["sha256"])
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("stage 1 attachment content does not match its declaration")
            paths[key] = relative.as_posix()
            hashes[key] = digest
            payload[f"{key}_sha256"] = digest
        payload["stage1_attachment_hashes"] = self._canonical_json(
            {key: value for key, value in hashes.items() if key.startswith("stage1_attachment_")}
        ).decode()
        return paths, hashes, payload

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def _attachment(self, root: Path, item: AdapterAttachment) -> dict[str, object]:
        relative = Path(item.path)
        path = (root / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or root.resolve() not in path.parents:
            raise ValueError("adapter attachment escapes promotion staging")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item.size_bytes
            or sha256_file(path) != item.sha256
        ):
            raise ValueError("adapter attachment changed before proof sealing")
        return {
            "path": item.path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    def _attachment_file(self, path: Path, item: AdapterAttachment) -> dict[str, object]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.size_bytes:
            raise ValueError("adapter attachment evidence is stale")
        if sha256_file(path) != item.sha256:
            raise ValueError("adapter attachment evidence hash mismatch")
        return {
            "path": item.path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    def _bundle(
        self,
        context: VerifiedProjectContext,
        build: _Build,
        symbol: Symbol,
        proof_type: str,
        invocations: tuple[_Invocation, ...],
    ) -> ProofBundle:
        canonical = canonical_target(symbol.address, symbol.name)
        evidence_schema_version = int(getattr(getattr(build, "evidence", None), "schema_version", 1))
        evidence: list[ProofEvidence] = [
            ProofEvidence(
                "compile",
                canonical,
                {
                    "passed": True,
                    "build": build.identity,
                    "artifact_sha256": build.publication.artifact_sha256,
                    "build_evidence_schema_version": str(evidence_schema_version),
                    "replayable": evidence_schema_version >= 2,
                },
            )
        ]
        for invocation in invocations:
            invocation_payload = dict(invocation.request.payload)
            stage = invocation_payload.get("stage")
            if stage is None:
                raise ValueError("adapter invocation is missing an explicit stage")
            stage = str(stage)
            request_hashes = getattr(invocation.request, "hashes", ())
            stage_attachment_hashes = {
                key: value for key, value in request_hashes if key.startswith("stage1_attachment_")
            }
            evidence.append(
                ProofEvidence(
                    proof_type,
                    canonical,
                    {
                        "passed": True,
                        "build": build.identity,
                        "stage": stage,
                        "stage1_result_sha256": invocation_payload.get("stage1_result_sha256", ""),
                        "stage1_request_sha256": invocation_payload.get("stage1_request_sha256", ""),
                        "stage1_attachment_hashes": invocation_payload.get(
                            "stage1_attachment_hashes", self._canonical_json(stage_attachment_hashes).decode()
                        ),
                        "command_argv": json.dumps(list(invocation.command.argv)),
                        "command_sha256": invocation.command.executable_sha256,
                        "request": invocation.request.to_json_bytes().decode("utf-8"),
                        "result": json.dumps(invocation.result.as_dict(), sort_keys=True),
                        "stdout": invocation.stdout,
                        "stderr": invocation.stderr,
                        "attachments": json.dumps(invocation.attachments, sort_keys=True),
                    },
                )
            )
        return ProofBundle(context.identity.name, canonical, build.identity, tuple(evidence)).sealed()

    def _commit(
        self, context: VerifiedProjectContext, build: _Build, bundles: tuple[ProofBundle, ...], target: str | None
    ) -> tuple[PromotionResult, ...]:
        store = ImmutableEvidenceStore(self.promotion_root)
        existing = self._current_bundles(context, build.identity)
        merged = tuple(
            bundle if bundle.target not in existing else self._merge_bundle(existing[bundle.target], bundle)
            for bundle in bundles
        )
        for bundle in merged:
            store.put(bundle)
        journal = PromotionJournal(self.promotion_root / "journal.jsonl")
        batch = journal.append(
            merged,
            project=context.identity.name,
            candidate=build.identity,
            expected_targets=tuple(bundle.target for bundle in merged),
        )
        project = self._publish_if_complete(context, journal, store, build.identity)
        return tuple(PromotionResult(bundle, bundle.bundle_sha256, batch, project) for bundle in merged)

    def _merge_bundle(self, current: ProofBundle, incoming: ProofBundle) -> ProofBundle:
        """Merge a new proof without duplicating the target's compile proof."""
        if (current.project, current.target, current.candidate) != (
            incoming.project,
            incoming.target,
            incoming.candidate,
        ):
            raise ValueError("proof bundle identity mismatch")
        current.verify()
        incoming.verify()
        current_compile = tuple(item for item in current.evidence if item.evidence_type == "compile")
        incoming_compile = tuple(item for item in incoming.evidence if item.evidence_type == "compile")
        if len(current_compile) != 1 or len(incoming_compile) != 1:
            raise ValueError("proof bundle must contain exactly one compile proof")
        if current_compile[0].as_dict() != incoming_compile[0].as_dict():
            raise ValueError("conflicting compile proof for target/build")
        incoming_types = {item.evidence_type for item in incoming.evidence if item.evidence_type != "compile"}
        retained = tuple(
            item
            for item in current.evidence
            if item.evidence_type == "compile" or item.evidence_type not in incoming_types
        )
        evidence = tuple((*retained, *(item for item in incoming.evidence if item.evidence_type != "compile")))
        merged = ProofBundle(current.project, current.target, current.candidate, evidence).sealed()
        merged.verify()
        return merged

    @staticmethod
    def _bundle_has_complete_stages(bundle: ProofBundle) -> bool:
        compile_proofs = tuple(item for item in bundle.evidence if item.evidence_type == "compile")
        if len(compile_proofs) != 1 or compile_proofs[0].payload.get("passed") is not True:
            return False
        for proof_type in ("abi", "differential"):
            try:
                proof_evidence = tuple(item for item in bundle.evidence if item.evidence_type == proof_type)
                stages = {int(item.payload["stage"]) for item in proof_evidence}
            except (KeyError, TypeError, ValueError):
                return False
            if stages != {0, 1} or any(item.payload.get("passed") is not True for item in proof_evidence):
                return False
        return True

    def _current_bundles(self, context: VerifiedProjectContext, candidate: str) -> dict[str, ProofBundle]:
        store = ImmutableEvidenceStore(self.promotion_root)
        journal = PromotionJournal(self.promotion_root / "journal.jsonl")
        result: dict[str, ProofBundle] = {}
        for record in journal.records():
            if record.project != context.identity.name or record.candidate != candidate:
                continue
            for digest in record.bundles:
                try:
                    bundle = store.get(digest)
                    revalidate_proof_bundle(bundle)
                    self._verify_bundle_toolchain(bundle)
                except _StaleEvidenceError:
                    # Historical proof material may be stale after a toolchain
                    # or profile change.  It is not current evidence, but it
                    # must not prevent a fresh proof from being collected.
                    continue
                except ValueError as exc:
                    if str(exc) == "stale historical profile":
                        continue
                    raise _CorruptEvidenceError("journal-referenced proof evidence is corrupt") from exc
                except (OSError, TypeError, binascii.Error) as exc:
                    raise _CorruptEvidenceError("journal-referenced proof evidence is corrupt") from exc
                if bundle.project != context.identity.name or bundle.candidate != candidate:
                    raise _CorruptEvidenceError("journal bundle identity mismatch")
                result[bundle.target] = bundle
        return result

    def _verify_bundle_toolchain(self, bundle: ProofBundle) -> None:
        context = load_verified_project(self.project_root)
        if bundle.project != context.identity.name:
            raise _StaleEvidenceError("proof bundle project is stale")
        try:
            build = self._load_build(context, bundle.candidate)
        except ValueError as exc:
            if "absent" in str(exc):
                raise _StaleEvidenceError("historical proof build is absent") from exc
            raise _CorruptEvidenceError("journal-referenced proof build is corrupt") from exc
        checkpoint = next(
            (item for item in build.evidence.targets if item.name == parse_target(bundle.target)[1]), None
        )
        if checkpoint is None:
            raise _StaleEvidenceError("proof bundle target is absent from current build")
        commands: dict[str, tuple[VerifiedCommand, VerifiedCommand]] = {}
        by_type: dict[str, dict[int, ProofEvidence]] = {"abi": {}, "differential": {}}
        for evidence in bundle.evidence:
            if evidence.evidence_type not in by_type:
                continue
            try:
                payload = dict(evidence.payload)
                stage_value = payload["stage"]
                if not isinstance(stage_value, str):
                    raise _CorruptEvidenceError("persisted adapter stage is malformed")
                stage = int(stage_value)
                if stage not in (0, 1) or stage in by_type[evidence.evidence_type]:
                    raise _CorruptEvidenceError("duplicate or invalid adapter stage")
                by_type[evidence.evidence_type][stage] = evidence
                request = AdapterRequest.from_dict(json.loads(payload["request"]))
                result_raw = json.loads(payload["result"])
                result = AdapterResult.from_dict(result_raw, expected_request_sha256=request.identity)
                if result.outcome != "pass" or payload.get("passed") is not True:
                    raise _CorruptEvidenceError("persisted adapter stage did not pass")
                persisted_attachments = json.loads(payload["attachments"])
                if not isinstance(persisted_attachments, list):
                    raise _CorruptEvidenceError("persisted attachment evidence is malformed")
                result_attachments = [attachment.as_dict() for attachment in result.attachments]
                if [
                    {key: item.get(key) for key in ("path", "sha256", "size_bytes")}
                    for item in persisted_attachments
                    if isinstance(item, dict)
                ] != result_attachments:
                    raise _CorruptEvidenceError("persisted attachment result is mismatched")
                for item in persisted_attachments:
                    if not isinstance(item, dict) or "content_base64" not in item:
                        raise _CorruptEvidenceError("persisted attachment content is missing")
                    content = base64.b64decode(str(item["content_base64"]), validate=True)
                    if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                        raise _CorruptEvidenceError("persisted attachment content hash is stale")
                if request.project_identity != context.identity.project_fingerprint:
                    raise _StaleEvidenceError("proof request project identity is stale")
                if request.snapshot_identity != context.identity.snapshot_manifest_sha256:
                    raise _StaleEvidenceError("proof request snapshot identity is stale")
                if request.manifest_identity != context.verified_abi_manifest.raw_sha256:
                    raise _StaleEvidenceError("proof request manifest identity is stale")
                if request.build_target_identity != bundle.candidate:
                    raise _StaleEvidenceError("proof request build identity is stale")
                request_payload = dict(request.payload)
                if request_payload.get("stage") != stage_value:
                    raise _CorruptEvidenceError("request and outer stage identities differ")
                request_stage1_result = request_payload.get("stage1_result_sha256", "")
                request_stage1_request = request_payload.get("stage1_request_sha256", "")
                request_stage1_attachments = {
                    key: value for key, value in request.hashes if key.startswith("stage1_attachment_")
                }
                if payload.get("stage1_result_sha256", "") != request_stage1_result:
                    raise _CorruptEvidenceError("outer stage 1 result binding differs from request")
                if payload.get("stage1_request_sha256", "") != request_stage1_request:
                    raise _CorruptEvidenceError("outer stage 1 request binding differs from request")
                outer_attachment_text = payload.get("stage1_attachment_hashes", "{}")
                outer_attachment_hashes = json.loads(outer_attachment_text)
                if (
                    outer_attachment_text != self._canonical_json(outer_attachment_hashes).decode()
                    or outer_attachment_hashes != request_stage1_attachments
                ):
                    raise _CorruptEvidenceError("outer stage 1 attachment binding differs from request")
                if (
                    request_payload.get("address") != str(checkpoint.address)
                    or request_payload.get("name") != parse_target(bundle.target)[1]
                ):
                    raise _StaleEvidenceError("proof request target identity is stale")
                capability = "inspect_abi" if evidence.evidence_type == "abi" else "run_differential"
                try:
                    expected_commands = commands.setdefault(capability, self._commands(capability))
                except (OSError, TypeError, ValueError) as exc:
                    raise _StaleEvidenceError("current adapter capability is unavailable") from exc
                expected = expected_commands[stage]
                if (
                    request.command.argv != expected.argv
                    or request.command.executable_sha256 != expected.executable_sha256
                ):
                    raise _StaleEvidenceError("proof request command identity is stale")
                persisted_argv = tuple(json.loads(payload["command_argv"]))
                if persisted_argv != expected.argv or payload["command_sha256"] != expected.executable_sha256:
                    raise _StaleEvidenceError("persisted command identity is stale")
                try:
                    verify_command(expected)
                except (OSError, TypeError, ValueError) as exc:
                    raise _StaleEvidenceError("current adapter executable is unavailable") from exc
                expected_paths = {
                    "artifact": "build/artifact",
                    "manifest": "manifest.json",
                    "source": f"sources/{checkpoint.output_path}",
                    "object": f"objects/{Path(checkpoint.output_path).with_suffix('.o').as_posix()}",
                }
                if evidence.evidence_type == "differential":
                    original_path = dict(request.paths).get("original_binary_equivalent")
                    if original_path is None:
                        raise _CorruptEvidenceError("differential proof lacks original binary input")
                    expected_paths["original_binary_equivalent"] = original_path
                if stage == 1:
                    expected_paths["stage1_result"] = "stage1/result.json"
                    prior_attachments = json.loads(by_type[evidence.evidence_type][0].payload["attachments"])
                    for index, _ in enumerate(prior_attachments):
                        expected_paths[f"stage1_attachment_{index}"] = dict(request.paths)[f"stage1_attachment_{index}"]
                if dict(request.paths) != expected_paths:
                    raise _StaleEvidenceError("proof request paths are stale")
                expected_hashes = {
                    "artifact": build.publication.artifact_sha256,
                    "build_evidence": build.publication.evidence_sha256,
                    "manifest": context.verified_abi_manifest.raw_sha256,
                    "source": checkpoint.source_sha256,
                    "object": checkpoint.object_sha256,
                    "original_binary_equivalent": context.identity.binary_sha256
                    if evidence.evidence_type == "differential"
                    else hashlib.sha256(b"").hexdigest(),
                }
                if stage == 1:
                    prior = by_type[evidence.evidence_type].get(0)
                    if prior is None:
                        raise _CorruptEvidenceError("stage 2 has no stage 1 proof")
                    prior_result = json.loads(prior.payload["result"])
                    prior_result_hash = hashlib.sha256(self._canonical_json(prior_result)).hexdigest()
                    if payload.get("stage1_result_sha256") != prior_result_hash:
                        raise _CorruptEvidenceError("stage 2 is not bound to stage 1 result")
                    expected_hashes["stage1_result"] = prior_result_hash
                    if (
                        payload.get("stage1_request_sha256")
                        != AdapterRequest.from_dict(json.loads(prior.payload["request"])).identity
                    ):
                        raise _CorruptEvidenceError("stage 2 is not bound to stage 1 request")
                    prior_attachment_hashes = {
                        f"stage1_attachment_{index}": str(attachment["sha256"])
                        for index, attachment in enumerate(json.loads(prior.payload["attachments"]))
                    }
                    if outer_attachment_hashes != prior_attachment_hashes:
                        raise _CorruptEvidenceError("stage 2 attachment binding is stale")
                    expected_hashes.update(prior_attachment_hashes)
                elif (
                    any((payload.get(key) or "") for key in ("stage1_result_sha256", "stage1_request_sha256"))
                    or outer_attachment_hashes
                ):
                    raise _CorruptEvidenceError("stage 1 contains unexpected stage 1 binding metadata")
                if dict(request.hashes) != expected_hashes:
                    raise _StaleEvidenceError("proof request input hashes are stale")
            except (_StaleEvidenceError, _CorruptEvidenceError):
                raise
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
                raise _CorruptEvidenceError("proof bundle has malformed adapter evidence") from exc

    def _publish_if_complete(
        self,
        context: VerifiedProjectContext,
        journal: PromotionJournal,
        store: ImmutableEvidenceStore,
        candidate: str,
    ) -> ProjectState | None:
        latest = self._current_bundles(context, candidate)
        expected = tuple(canonical_target(s.address, s.name) for s in context.verified_abi_manifest.value.symbols)
        if set(latest) != set(expected):
            return None
        if any(not self._bundle_has_complete_stages(bundle) for bundle in latest.values()):
            return None
        batch = self._batch_for_current(context, candidate, latest)
        if batch is None:
            return None
        state = derive_project_state(
            context.identity.name,
            candidate,
            tuple(
                derive_target_state(latest[canonical_target(s.address, s.name)])
                for s in context.verified_abi_manifest.value.symbols
            ),
            batch_hash=batch.record_hash,
        )
        if state.state is PromotionState.PROMOTED:
            PromotionViewPublisher(self.promotion_root).publish(state)
            return state
        return None

    def _batch_for_current(
        self, context: VerifiedProjectContext, candidate: str, bundles: dict[str, ProofBundle]
    ) -> PromotionBatch | None:
        expected_targets = tuple(
            sorted(canonical_target(s.address, s.name) for s in context.verified_abi_manifest.value.symbols)
        )
        expected_bundles = tuple(sorted(bundle.bundle_sha256 for bundle in bundles.values()))
        for record in reversed(PromotionJournal(self.promotion_root / "journal.jsonl").records()):
            if record.project != context.identity.name or record.candidate != candidate:
                continue
            if tuple(sorted(record.bundles)) != expected_bundles:
                continue
            if tuple(sorted(bundles)) != expected_targets:
                continue
            return record
        return None

    def _active_view_matches(
        self,
        context: VerifiedProjectContext,
        candidate: str,
        batch: PromotionBatch,
        bundles: dict[str, ProofBundle],
    ) -> bool:
        try:
            view = PromotionViewPublisher(self.promotion_root).load_active()
            if (
                view.get("project") != context.identity.name
                or view.get("candidate") != candidate
                or view.get("state") != PromotionState.PROMOTED.value
                or view.get("batch_hash") != batch.record_hash
            ):
                return False
            targets = view.get("targets")
            if not isinstance(targets, list):
                return False
            active_bundles = {
                item.get("target"): item.get("bundle_sha256") for item in targets if isinstance(item, dict)
            }
            expected = {
                canonical_target(s.address, s.name): bundle.bundle_sha256
                for s, bundle in zip(context.verified_abi_manifest.value.symbols, bundles.values(), strict=True)
            }
            return active_bundles == expected
        except (OSError, TypeError, ValueError, KeyError):
            return False

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise ValueError("promotion path must be inside project root") from exc


__all__ = ["PromotionResult", "PromotionService"]
