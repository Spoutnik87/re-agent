"""Project-scoped verification and offline transform replay."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from re_agent.build.evidence import validate_run_id
from re_agent.config.loader import load_config
from re_agent.project.context import load_verified_project


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _load_json(path: Path) -> object:
    return json.loads(
        path.read_bytes(),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def _require_existing_run_directory(project_root: Path, run_id: str) -> Path:
    from re_agent.cli.cmd_build import _reject_path_components

    root = project_root.absolute()
    runs_root = root / "build" / "runs"
    candidate = runs_root / run_id
    _reject_path_components(root)
    _reject_path_components(runs_root)
    if not runs_root.is_dir() or runs_root.is_symlink():
        raise ValueError("project run root is missing or linked")
    _reject_path_components(candidate)
    _reject_path_components(candidate / ".run.lock")
    if not candidate.is_dir() or candidate.is_symlink():
        raise ValueError("run directory is missing or linked")
    try:
        resolved_root = runs_root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("cannot resolve project run directory") from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError("run directory escapes the project run root")
    return candidate


def _require_replay_parent(project_root: Path) -> Path:
    from re_agent.cli.cmd_build import _reject_path_components

    project_root = project_root.absolute()
    build_root = project_root / "build"
    replay_parent = build_root / "replays"
    _reject_path_components(project_root)
    _reject_path_components(build_root)
    _reject_path_components(replay_parent)
    if not build_root.is_dir() or build_root.is_symlink():
        raise ValueError("project build root is missing or linked")
    if replay_parent.exists() and (not replay_parent.is_dir() or replay_parent.is_symlink()):
        raise ValueError("replay parent is not a regular directory")
    try:
        resolved_build = build_root.resolve(strict=True)
        if replay_parent.exists() and not replay_parent.resolve(strict=True).is_relative_to(resolved_build):
            raise ValueError("replay parent escapes project build root")
    except OSError as exc:
        raise ValueError("cannot resolve replay parent") from exc
    replay_parent.mkdir(parents=False, exist_ok=True)
    _reject_path_components(replay_parent)
    if not replay_parent.is_dir() or replay_parent.is_symlink():
        raise ValueError("replay parent is missing or linked")
    if not replay_parent.resolve(strict=True).is_relative_to(resolved_build):
        raise ValueError("replay parent escapes project build root")
    return replay_parent


def _require_replay_root(replay_root: Path, replay_parent: Path) -> None:
    from re_agent.cli.cmd_build import _reject_path_components

    _reject_path_components(replay_parent)
    _reject_path_components(replay_root)
    if not replay_root.is_dir() or replay_root.is_symlink():
        raise ValueError("replay root is missing or linked")
    try:
        if not replay_root.resolve(strict=True).is_relative_to(replay_parent.resolve(strict=True)):
            raise ValueError("replay root escapes replay parent")
    except OSError as exc:
        raise ValueError("cannot resolve replay root") from exc


def _current_identities(
    args: argparse.Namespace,
    context: Any,
    config: Any,
    profile_raw: str | None,
) -> tuple[dict[str, str], Any]:
    from re_agent.build.transform.manifest_bound_transform import build_preserve_abi_prompt
    from re_agent.cli.cmd_build import (
        _effective_llm_config,
        _materialize_recipe,
        _project_recipe,
        _recipe_replacements,
        _value_identity,
    )
    from re_agent.toolchain.activation import resolve_capability

    manifest = config.contracts.verified_manifest.manifest
    first_symbol = sorted(manifest.symbols, key=lambda item: (item.address, item.name))[0]
    with tempfile.TemporaryDirectory(prefix="run-verify-") as temporary:
        temporary_root = Path(temporary)
        temporary_staging = temporary_root / "staging"
        temporary_staging.mkdir()
        recipe, link_command = _project_recipe(context.root, profile_raw, temporary_staging)
        recipe_template_sha256 = recipe.recipe_sha256
        recipe = _materialize_recipe(
            recipe,
            _recipe_replacements(temporary_staging, recipe, first_symbol.output_path),
        )
    profile_path = Path(profile_raw) if profile_raw else None
    (compile_command,) = resolve_capability(
        project_root=context.root,
        capability="compile",
        profile_path=profile_path,
    )
    llm_config = _effective_llm_config(config.llm)
    prompt = build_preserve_abi_prompt(first_symbol, "identity-source", manifest)
    identities = {
        "project_fingerprint": context.identity.project_fingerprint,
        "snapshot_manifest_sha256": context.identity.snapshot_manifest_sha256,
        "manifest_sha256": manifest.sha256_hash,
        "manifest_raw_sha256": config.contracts.verified_manifest.raw_sha256,
        "toolchain_sha256": link_command.executable_sha256,
        "compiler_sha256": compile_command.executable_sha256,
        "config_sha256": hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),
        "recipe_template_sha256": recipe_template_sha256,
        "recipe_sha256": recipe.recipe_sha256,
        "llm_config_sha256": _value_identity(llm_config),
        "prompt_sha256": hashlib.sha256(prompt.system.encode("utf-8")).hexdigest(),
    }
    return identities, compile_command


def _require_run_files(run_root: Path) -> tuple[dict[str, object], list[Any], Path]:
    from re_agent.cli.cmd_build import _reject_path_components, _require_regular_contained_file

    _reject_path_components(run_root)
    if not run_root.is_dir() or run_root.is_symlink():
        raise ValueError("run directory is missing or linked")
    identity_path = run_root / "run.json"
    checkpoint_path = run_root / "checkpoints.json"
    _require_regular_contained_file(identity_path, run_root)
    _require_regular_contained_file(checkpoint_path, run_root)
    identity = _load_json(identity_path)
    raw_checkpoints = _load_json(checkpoint_path)
    if not isinstance(identity, dict) or not isinstance(raw_checkpoints, list):
        raise ValueError("malformed run state")
    from re_agent.build.evidence import TargetCheckpoint

    try:
        checkpoints = [TargetCheckpoint(**item) for item in raw_checkpoints]
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed checkpoints") from exc
    staging = run_root / "staging"
    _reject_path_components(staging)
    if not staging.is_dir() or staging.is_symlink():
        raise ValueError("run staging directory is missing or linked")
    return identity, checkpoints, staging


def _validate_run(
    args: argparse.Namespace,
    context: Any,
    config: Any,
    run_id: str,
    profile_raw: str | None,
) -> tuple[dict[str, object], list[Any], Any, Any, Path]:
    from re_agent.cli.cmd_build import _target_checkpoint_valid

    run_root = context.root / "build" / "runs" / run_id
    identity, checkpoints, staging = _require_run_files(run_root)
    identities, compile_command = _current_identities(args, context, config, profile_raw)
    expected_identity = {"schema_version": 2, "run_id": run_id, **identities}
    if identity != expected_identity:
        raise ValueError("run identity is stale or does not match the current project")
    manifest = config.contracts.verified_manifest.manifest
    expected = tuple(sorted((symbol.address, symbol.name) for symbol in manifest.symbols))
    if tuple(item.key() for item in sorted(checkpoints, key=lambda item: item.key())) != expected:
        raise ValueError("run checkpoints do not cover the complete current manifest")
    build_cfg = copy.deepcopy(config.build)
    build_cfg.input.ghidra_exports = str(context.snapshot_root)
    effective_llm_config = {
        key: getattr(config.llm, key)
        for key in ("provider", "model", "block_model", "base_url", "max_tokens", "temperature", "timeout_s")
        if hasattr(config.llm, key)
    }
    ordered = sorted(manifest.symbols, key=lambda item: (item.address, item.name))
    for index, (symbol, checkpoint) in enumerate(
        zip(ordered, sorted(checkpoints, key=lambda item: item.key()), strict=True)
    ):
        input_source = _source_for_symbol(build_cfg, symbol)
        if not _target_checkpoint_valid(
            checkpoint,
            symbol,
            staging,
            input_source=input_source,
            manifest=manifest,
            verified_contract=config.contracts.verified_manifest,
            project_fingerprint=context.identity.project_fingerprint,
            snapshot_fingerprint=context.identity.snapshot_manifest_sha256,
            run_id=f"{run_id}-{index}",
            llm_config=effective_llm_config,
            compile_command=compile_command,
        ):
            raise ValueError(f"invalid transform evidence for 0x{symbol.address:x}")
    return identity, checkpoints, compile_command, build_cfg, run_root


def _source_for_symbol(build_cfg: Any, symbol: Any) -> Path:
    from re_agent.cli.cmd_build import _source_for_symbol as find_source

    return find_source(build_cfg, symbol)


def _replay_locked(
    args: argparse.Namespace,
    context: Any,
    config: Any,
    run_id: str,
    profile_raw: str | None,
) -> int:
    from re_agent.build.evidence import load_transform_evidence
    from re_agent.build.transform.manifest_bound_transform import run_manifest_bound_transform
    from re_agent.cli.cmd_build import _require_regular_contained_file, _sha256
    from re_agent.llm.replay import ReplayProvider

    run_root = context.root / "build" / "runs" / run_id
    _, checkpoints, compile_command, build_cfg, _ = _validate_run(
        args,
        context,
        config,
        run_id,
        profile_raw,
    )
    manifest = config.contracts.verified_manifest.manifest
    ordered = sorted(manifest.symbols, key=lambda item: (item.address, item.name))
    evidence = []
    for checkpoint in sorted(checkpoints, key=lambda item: item.key()):
        evidence.append(load_transform_evidence(run_root / "staging" / checkpoint.transform_evidence_path))

    replay_parent = _require_replay_parent(context.root)
    replay_root = replay_parent / f"{run_id}-{uuid.uuid4().hex}"
    from re_agent.cli.cmd_build import _reject_path_components

    _reject_path_components(replay_root)
    if replay_root.exists():
        raise ValueError("replay root already exists")
    replay_root.mkdir(parents=True, exist_ok=False)
    _require_replay_root(replay_root, replay_parent)
    try:
        _require_replay_root(replay_root, replay_parent)
        replay_build = copy.deepcopy(build_cfg)
        replay_build.output.target_dir = str(replay_root / "output")
        replay_build.output.work_dir = str(replay_root / "work")
        for index, (symbol, transform) in enumerate(zip(ordered, evidence, strict=True)):
            _require_replay_root(replay_root, replay_parent)
            replay_provider = ReplayProvider.from_evidence(transform)
            replay_provider.validate_effective_config(
                {
                    key: getattr(config.llm, key)
                    for key in (
                        "provider",
                        "model",
                        "block_model",
                        "base_url",
                        "max_tokens",
                        "temperature",
                        "timeout_s",
                    )
                    if hasattr(config.llm, key)
                }
            )
            result = run_manifest_bound_transform(
                replay_build,
                config.llm,
                config.contracts.verified_manifest,
                symbol.address,
                run_id=f"replay-{run_id}-{index}",
                persist=True,
                provider=replay_provider,
                verified_compile_command=compile_command,
                project_fingerprint=context.identity.project_fingerprint,
                snapshot_fingerprint=context.identity.snapshot_manifest_sha256,
            )
            if not result.successful:
                raise ValueError(f"replay failed for 0x{symbol.address:x}: {result.error}")
            unit = replay_root / "output" / ".manifest-bound" / f"replay-{run_id}-{index}" / f"0x{symbol.address:x}"
            source = unit / symbol.output_path
            object_path = unit / (Path(symbol.output_path).stem + ".o")
            _require_regular_contained_file(source, replay_root / "output")
            _require_regular_contained_file(object_path, replay_root / "output")
            if _sha256(source) != transform.generated_sha256 or _sha256(object_path) != transform.object_sha256:
                raise ValueError(f"replayed artifacts differ for 0x{symbol.address:x}")
    finally:
        try:
            _require_replay_root(replay_root, replay_parent)
        except ValueError:
            # Never recursively delete an ownership/containment violation.
            pass
        else:
            shutil.rmtree(replay_root, ignore_errors=True)
    print(f"Replay verified: {len(evidence)} manifest targets")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a project-only verify or offline replay operation."""
    if args.run_command not in {"verify", "replay"}:
        raise ValueError("unsupported run operation")
    run_id = args.run_id
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise ValueError(f"--run-id must be a safe path component: {exc}") from None
    project_root = Path(args.project_root)
    profile_raw = getattr(args, "profile", None)
    # These checks intentionally happen before RunLock: RunLock creates its
    # lock file and must never create a missing or substituted run directory.
    context = load_verified_project(project_root)
    from re_agent.cli.cmd_build import _reject_ambiguous_profile

    _reject_ambiguous_profile(context.root, profile_raw)
    run_root = _require_existing_run_directory(context.root, run_id)
    config = load_config(Path(args.config), verified_contract_override=context.verified_abi_manifest)
    config.contracts.verified_manifest = context.verified_abi_manifest

    from re_agent.build.run_lock import RunLock

    with RunLock(run_root, metadata={"run_id": run_id, "operation": args.run_command}):
        # Re-read all ownership inputs under the command-lifetime lock, then
        # re-check the path before reading mutable run state.
        locked_context = load_verified_project(project_root)
        if locked_context.identity != context.identity:
            raise ValueError("project identity changed while acquiring run lock")
        _reject_ambiguous_profile(locked_context.root, profile_raw)
        _require_existing_run_directory(locked_context.root, run_id)
        locked_config = load_config(Path(args.config), verified_contract_override=locked_context.verified_abi_manifest)
        locked_config.contracts.verified_manifest = locked_context.verified_abi_manifest
        if args.run_command == "verify":
            _validate_run(args, locked_context, locked_config, run_id, profile_raw)
            print(f"Run verified: {run_id}")
            return 0
        return _replay_locked(args, locked_context, locked_config, run_id, profile_raw)


__all__ = ["cmd_run"]
