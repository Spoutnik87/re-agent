"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from re_agent.config.loader import load_config


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_text(path: Path) -> str:
    """Read source without universal-newline translation."""
    return path.read_bytes().decode("utf-8")


def _config_identity(path: Path) -> str:
    try:
        return _sha256(path)
    except OSError as exc:
        raise ValueError(f"config identity cannot be read: {path}") from exc


def _value_identity(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _effective_llm_config(llm_cfg: Any) -> dict[str, object]:
    return {
        key: getattr(llm_cfg, key)
        for key in ("provider", "model", "block_model", "base_url", "max_tokens", "temperature", "timeout_s")
        if hasattr(llm_cfg, key)
    }


def _atomic_json(path: Path, value: object) -> None:
    """Write a run record atomically so a killed transform cannot publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _atomic_create_json(path: Path, value: object) -> None:
    """Create an immutable JSON record without replacing an existing record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.link(temporary, path)
    except FileExistsError:
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, destination: Path) -> None:
    _require_regular_contained_file(source, source.parent)
    _reject_path_components(destination.parent)
    if destination.is_symlink() or _is_reparse_point(destination):
        raise ValueError(f"refusing to overwrite linked destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_path_components(destination.parent)
    _require_regular_contained_file(source, source.parent)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    if destination.is_symlink() or _is_reparse_point(destination):
        temporary.unlink(missing_ok=True)
        raise ValueError(f"refusing to overwrite linked destination: {destination}")
    temporary.replace(destination)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"cannot inspect path: {path}") from exc
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_path_components(path: Path) -> None:
    """Reject symlink/reparse components before any path is followed."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if not current.exists() and not current.is_symlink():
            continue
        if current.is_symlink() or _is_reparse_point(current):
            raise ValueError(f"path contains a symlink or reparse point: {current}")


def _require_regular_contained_file(path: Path, root: Path) -> Path:
    """Require a regular, non-linked file resolved beneath ``root``."""
    root_absolute = root.absolute()
    path_absolute = path.absolute()
    _reject_path_components(root_absolute)
    _reject_path_components(path_absolute)
    if not path_absolute.is_relative_to(root_absolute):
        raise ValueError(f"path escapes expected root: {path}")
    if path_absolute.is_symlink() or _is_reparse_point(path_absolute) or not path_absolute.is_file():
        raise ValueError(f"path is not a regular file: {path}")
    try:
        resolved_root = root_absolute.resolve(strict=True)
        resolved_path = path_absolute.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve path: {path}") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"resolved path escapes expected root: {path}")
    return resolved_path


def _project_recipe(project_root: Path, profile_raw: str | None, staging: Path) -> tuple[Any, Any]:
    """Load and strictly validate the profile-owned, shell-free project recipe."""
    from re_agent.build.recipe import BuildRecipe
    from re_agent.toolchain.activation import resolve_capability
    from re_agent.toolchain.profile import load_profile, load_profile_from_dict

    profile_path = Path(profile_raw) if profile_raw else None
    (link_command,) = resolve_capability(project_root=project_root, capability="link", profile_path=profile_path)
    if profile_path is not None:
        profile = load_profile(profile_path)
    else:
        pointer = json.loads((project_root / "toolchain" / "active.link").read_text(encoding="utf-8"))
        profile_file = project_root / "toolchain" / str(pointer["profile_sha256"]) / "profile.json"
        profile = load_profile_from_dict(json.loads(profile_file.read_bytes()))
    raw = profile.extensions.get("build_recipe")
    if not isinstance(raw, dict):
        raise ValueError("toolchain profile extensions.build_recipe is required")
    allowed = {"argv", "output", "staging_root", "cwd", "timeout_seconds", "env"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"build_recipe contains unknown key: {sorted(unknown)[0]}")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("build_recipe.argv must be a non-empty string array")
    if argv[0] == "{linker}":
        argv = [*link_command.argv, *argv[1:]]
    elif Path(argv[0]).resolve() != Path(link_command.argv[0]).resolve():
        raise ValueError("build_recipe executable must be the verified profile linker (use {linker})")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("build_recipe.env must be a string mapping")
    recipe = BuildRecipe(
        argv=tuple(argv),
        output=raw.get("output", "artifact"),
        staging_root=raw.get("staging_root", "."),
        cwd=raw.get("cwd", "."),
        timeout_seconds=raw.get("timeout_seconds", 60.0),
        env=tuple(sorted(env.items())),
    )
    (staging / recipe.staging_root / recipe.cwd).mkdir(parents=True, exist_ok=True)
    recipe.validate_paths(staging)
    return recipe, link_command


def _reject_ambiguous_profile(project_root: Path, profile_raw: str | None) -> None:
    """Reject transient profile selection when the project has an active one."""
    if profile_raw is None:
        return
    active_link = project_root / "toolchain" / "active.link"
    if active_link.is_file() or active_link.is_symlink():
        raise ValueError("--profile cannot be combined with an active toolchain profile")


def _materialize_recipe(recipe: Any, replacements: dict[str, str]) -> Any:
    """Expand only documented, relative recipe placeholders."""
    from re_agent.build.recipe import BuildRecipe

    def expand(value: str) -> str:
        for name, replacement in replacements.items():
            value = value.replace("{" + name + "}", replacement)
        return value

    return BuildRecipe(
        argv=tuple(expand(item) for item in recipe.argv),
        output=expand(recipe.output),
        staging_root=recipe.staging_root,
        cwd=recipe.cwd,
        timeout_seconds=recipe.timeout_seconds,
        env=tuple((key, expand(value)) for key, value in recipe.env),
    )


def _recipe_path(cwd: Path, path: Path) -> str:
    return Path(os.path.relpath(path, cwd)).as_posix()


def _recipe_replacements(staging: Path, recipe: Any, output_path: str) -> dict[str, str]:
    """Compute deterministic materialized paths before a run identity is bound."""
    cwd, _ = recipe.validate_paths(staging)
    source = staging / output_path
    object_path = staging / Path(output_path).with_suffix(".o")
    return {
        "build_manifest": _recipe_path(cwd, cwd / "build-manifest.json"),
        "object_manifest": _recipe_path(cwd, cwd / "object-manifest.json"),
        "source": _recipe_path(cwd, source),
        "object": _recipe_path(cwd, object_path),
        "artifact": _recipe_path(cwd, staging / "artifact"),
        "staging_root": _recipe_path(cwd, staging),
    }


def _source_for_symbol(build_cfg: Any, symbol: Any) -> Path:
    source_dir = Path(build_cfg.input.decompiled_dir)
    _reject_path_components(source_dir)
    candidates = [
        candidate
        for candidate in source_dir.iterdir()
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.name.lower().startswith(f"0x{symbol.address:x}")
        and candidate.suffix == ".cpp"
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one source candidate for 0x{symbol.address:x}")
    _require_regular_contained_file(candidates[0], source_dir)
    return candidates[0]


def _write_recipe_manifests(staging: Path, checkpoints: list[Any], recipe: Any) -> dict[str, str]:
    """Provide generic build/object manifests consumed by recipe placeholders."""
    cwd, _ = recipe.validate_paths(staging)
    entries = [
        {
            "address": item.address,
            "name": item.name,
            "source": _recipe_path(cwd, staging / item.output_path),
            "object": _recipe_path(cwd, staging / Path(item.output_path).with_suffix(".o")),
        }
        for item in checkpoints
    ]
    build_manifest = cwd / "build-manifest.json"
    object_manifest = cwd / "object-manifest.json"
    _atomic_json(build_manifest, {"format_version": 1, "targets": entries})
    _atomic_json(
        object_manifest,
        {"format_version": 1, "objects": [item["object"] for item in entries]},
    )
    return _recipe_replacements(staging, recipe, checkpoints[0].output_path)


def _write_witness_inputs(staging: Path, recipe: Any, compiler: Any) -> dict[str, str]:
    """Create deterministic generic source/object/manifest inputs for recipe verification."""
    cwd, _ = recipe.validate_paths(staging)
    source = cwd / "witness.cpp"
    object_path = cwd / "witness.o"
    source.write_text("int release4_witness() { return 0; }\n", encoding="utf-8")
    from re_agent.toolchain.activation import verify_command

    verify_command(compiler)
    completed = subprocess.run(
        [*compiler.argv, "-o", str(object_path), str(source)],
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
    )
    if completed.returncode != 0 or not object_path.is_file() or object_path.stat().st_size == 0:
        detail = (completed.stdout + completed.stderr).strip()
        raise ValueError(f"verified witness compilation failed: {detail or completed.returncode}")
    _atomic_json(
        cwd / "build-manifest.json",
        {
            "format_version": 1,
            "targets": [
                {
                    "name": "release4_witness",
                    "source": _recipe_path(cwd, source),
                    "object": _recipe_path(cwd, object_path),
                }
            ],
        },
    )
    _atomic_json(
        cwd / "object-manifest.json",
        {"format_version": 1, "objects": [_recipe_path(cwd, object_path)]},
    )
    return {
        "source": _recipe_path(cwd, source),
        "object": _recipe_path(cwd, object_path),
        "build_manifest": _recipe_path(cwd, cwd / "build-manifest.json"),
        "object_manifest": _recipe_path(cwd, cwd / "object-manifest.json"),
        "artifact": _recipe_path(cwd, staging / "artifact"),
        "staging_root": _recipe_path(cwd, staging),
    }


def _target_checkpoint_valid(
    checkpoint: Any,
    symbol: Any,
    staging: Path,
    *,
    input_source: Path,
    manifest: Any,
    verified_contract: Any,
    project_fingerprint: str,
    snapshot_fingerprint: str,
    run_id: str,
    llm_config: dict[str, object],
    compile_command: Any,
) -> bool:
    """Validate the checkpoint and its complete, replayable transform evidence."""
    try:
        source = staging / symbol.output_path
        object_path = staging / Path(symbol.output_path).with_suffix(".o")
        evidence_path = staging / checkpoint.transform_evidence_path
        if not checkpoint.transform_evidence_path or not checkpoint.transform_evidence_sha256:
            return False
        path_parts = checkpoint.transform_evidence_path.split("/")
        if (
            "\\" in checkpoint.transform_evidence_path
            or checkpoint.transform_evidence_path.startswith("/")
            or any(part in {"", ".", ".."} for part in path_parts)
            or not evidence_path.is_relative_to(staging)
            or not evidence_path.is_file()
        ):
            return False
        _require_regular_contained_file(source, staging)
        _require_regular_contained_file(object_path, staging)
        _require_regular_contained_file(evidence_path, staging)
        _require_regular_contained_file(input_source, input_source.parent)
        from re_agent.build.evidence import load_transform_evidence
        from re_agent.build.transform.manifest_bound_transform import build_preserve_abi_prompt
        from re_agent.llm.replay import validate_replay_effective_config

        transform = load_transform_evidence(evidence_path)
        if _sha256(evidence_path) != checkpoint.transform_evidence_sha256:
            return False
        prompt = build_preserve_abi_prompt(symbol, _source_text(input_source), manifest)
        if transform.messages != (("system", prompt.system), ("user", prompt.user)):
            return False
        validate_replay_effective_config(llm_config, transform.llm_config)
        compiler_argv = transform.compiler_argv
        if (
            not compiler_argv
            or tuple(compiler_argv[: len(compile_command.argv)]) != tuple(compile_command.argv)
            or transform.compiler_executable_sha256 != compile_command.executable_sha256
        ):
            return False
        return (
            checkpoint.key() == (symbol.address, symbol.name)
            and checkpoint.status == "compiled"
            and checkpoint.signature == symbol.signature
            and checkpoint.calling_convention == symbol.calling_convention.value
            and checkpoint.output_path == symbol.output_path
            and checkpoint.transform_evidence_path == checkpoint.transform_evidence_path.replace("\\", "/")
            and "MANIFEST_BOUND" in checkpoint.verdicts
            and "COMPILE_PASS" in checkpoint.verdicts
            and transform.project_fingerprint == project_fingerprint
            and transform.snapshot_fingerprint == snapshot_fingerprint
            and transform.manifest_raw_sha256 == verified_contract.raw_sha256
            and transform.manifest_sha256 == verified_contract.canonical_sha256
            and transform.run_id == run_id
            and transform.target_address == symbol.address
            and transform.target_name == symbol.name
            and transform.target_signature == symbol.signature
            and transform.target_calling_convention == symbol.calling_convention.value
            and transform.target_output_path == symbol.output_path
            and transform.input_sha256 == _sha256(input_source)
            and checkpoint.source_sha256 == _sha256(source)
            and checkpoint.generated_sha256 == _sha256(source)
            and checkpoint.output_sha256 == _sha256(source)
            and checkpoint.input_sha256 == _sha256(input_source)
            and checkpoint.object_sha256 == _sha256(object_path)
            and transform.generated_sha256 == _sha256(source)
            and transform.object_sha256 == _sha256(object_path)
        )
    except (AttributeError, OSError, UnicodeDecodeError, ValueError):
        return False


def _write_failure_evidence(
    run_root: Path, run_id: str, identities: dict[str, str], error: str, **result: object
) -> None:
    """Keep a structured, non-publishable record for failed recipe attempts."""
    _atomic_json(
        run_root / "failure-evidence.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "success": False,
            "published": False,
            "identities": identities,
            "error": error,
            "recipe_result": result,
        },
    )


def _cmd_build_project_unlocked(
    args: argparse.Namespace,
    project_context: Any,
    config: Any,
    *,
    run_id: str,
    run_root: Path,
) -> int:
    """Release 4 project orchestration: bulk transform, recipe, evidence, publish."""
    from re_agent.build.bulk import validate_bulk_evidence
    from re_agent.build.evidence import BuildEvidence, TargetCheckpoint, save_evidence
    from re_agent.build.recipe import run_recipe
    from re_agent.build.transform.manifest_bound_transform import run_manifest_bound_transform
    from re_agent.project.publish import publish_build
    from re_agent.toolchain.activation import resolve_capability

    root = project_context.root
    if getattr(args, "allow_partial", False):
        print("Error: --allow-partial is not supported; project publication is all-or-nothing", file=sys.stderr)
        return 2
    phase = getattr(args, "phase", None) or "transform"
    if phase == "verify-recipe":
        args.verify_recipe = True
        phase = "assemble"
    if phase not in {"transform", "assemble", "link", "package"}:
        print("Error: project mode accepts transform, link, package, or verify-recipe", file=sys.stderr)
        return 2
    _reject_ambiguous_profile(root, getattr(args, "profile", None))

    staging = run_root / "staging"
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(exist_ok=True)
        recipe, link_command = _project_recipe(root, getattr(args, "profile", None), staging)
        if getattr(args, "verify_recipe", False):
            with tempfile.TemporaryDirectory(prefix="recipe-witness-", dir=str(root / "build")) as witness:
                witness_root = Path(witness)
                witness_stage = witness_root / "stage"
                witness_stage.mkdir()
                witness_recipe, _ = _project_recipe(root, getattr(args, "profile", None), witness_stage)
                (witness_compiler,) = resolve_capability(
                    project_root=root,
                    capability="compile",
                    profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
                )
                replacements = _write_witness_inputs(witness_stage, witness_recipe, witness_compiler)
                witness_recipe = _materialize_recipe(witness_recipe, replacements)
                witness_result = run_recipe(witness_recipe, witness_stage)
                if not witness_result.successful:
                    raise ValueError(
                        "recipe witness failed: "
                        f"{witness_result.error or witness_result.stderr.strip() or witness_result.returncode}"
                    )
            print(f"Recipe verified: {recipe.recipe_sha256}")
            return 0

        manifest = config.contracts.verified_manifest.manifest
        expected = tuple(sorted((symbol.address, symbol.name) for symbol in manifest.symbols))
        build_cfg = config.build
        first_symbol = sorted(manifest.symbols, key=lambda item: (item.address, item.name))[0]
        recipe_template_sha256 = recipe.recipe_sha256
        recipe = _materialize_recipe(
            recipe,
            _recipe_replacements(staging, recipe, first_symbol.output_path),
        )
        (compile_command,) = resolve_capability(
            project_root=root,
            capability="compile",
            profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
        )
        effective_llm_config = _effective_llm_config(config.llm)
        from re_agent.build.transform.manifest_bound_transform import build_preserve_abi_prompt

        prompt_identity = build_preserve_abi_prompt(first_symbol, "identity-source", manifest).system
        identities = {
            "project_fingerprint": project_context.identity.project_fingerprint,
            "snapshot_manifest_sha256": project_context.identity.snapshot_manifest_sha256,
            "manifest_sha256": manifest.sha256_hash,
            "manifest_raw_sha256": config.contracts.verified_manifest.raw_sha256,
            "toolchain_sha256": link_command.executable_sha256,
            "compiler_sha256": compile_command.executable_sha256,
            "config_sha256": _config_identity(Path(args.config)),
            "recipe_template_sha256": recipe_template_sha256,
            "recipe_sha256": recipe.recipe_sha256,
            "llm_config_sha256": _value_identity(effective_llm_config),
            "prompt_sha256": hashlib.sha256(prompt_identity.encode("utf-8")).hexdigest(),
        }
        build_cfg.input.ghidra_exports = str(project_context.snapshot_root)
        if not Path(build_cfg.output.target_dir).is_absolute():
            build_cfg.output.target_dir = str(root / build_cfg.output.target_dir)
        checkpoint_file = run_root / "checkpoints.json"
        identity_file = run_root / "run.json"
        identity_payload = {"schema_version": 2, "run_id": run_id, **identities}
        if identity_file.exists():
            stored = json.loads(identity_file.read_text(encoding="utf-8"))
            if stored != identity_payload:
                raise ValueError("run identity is stale or was created by another configuration")
        else:
            if checkpoint_file.exists():
                raise ValueError("run checkpoints exist without an immutable run identity")
            _atomic_create_json(identity_file, identity_payload)
        checkpoints: list[TargetCheckpoint] = []
        existing: dict[tuple[int, str], TargetCheckpoint] = {}
        if checkpoint_file.is_file():
            try:
                existing = {
                    item.key(): item
                    for item in (
                        TargetCheckpoint(**raw) for raw in json.loads(checkpoint_file.read_text(encoding="utf-8"))
                    )
                }
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                existing = {}
        if phase == "transform":
            for index, symbol in enumerate(sorted(manifest.symbols, key=lambda item: (item.address, item.name))):
                prior = existing.get((symbol.address, symbol.name))
                input_source = _source_for_symbol(build_cfg, symbol)
                if prior is not None and _target_checkpoint_valid(
                    prior,
                    symbol,
                    staging,
                    input_source=input_source,
                    manifest=manifest,
                    verified_contract=config.contracts.verified_manifest,
                    project_fingerprint=project_context.identity.project_fingerprint,
                    snapshot_fingerprint=project_context.identity.snapshot_manifest_sha256,
                    run_id=f"{run_id}-{index}",
                    llm_config=effective_llm_config,
                    compile_command=compile_command,
                ):
                    checkpoints.append(prior)
                    continue
                stale_unit = (
                    Path(build_cfg.output.target_dir)
                    / ".manifest-bound"
                    / f"{run_id}-{index}"
                    / f"0x{symbol.address:x}"
                )
                if stale_unit.exists() or stale_unit.is_symlink():
                    raise ValueError(f"prior transform publication for 0x{symbol.address:x} is not reusable")
                result = run_manifest_bound_transform(
                    build_cfg,
                    config.llm,
                    config.contracts.verified_manifest,
                    symbol.address,
                    run_id=f"{run_id}-{index}",
                    persist=True,
                    verified_compile_command=compile_command,
                    project_fingerprint=project_context.identity.project_fingerprint,
                    snapshot_fingerprint=project_context.identity.snapshot_manifest_sha256,
                )
                if not result.successful:
                    raise ValueError(f"transform failed for 0x{symbol.address:x}: {getattr(result, 'error', '')}")
                unit = (
                    Path(build_cfg.output.target_dir)
                    / ".manifest-bound"
                    / f"{run_id}-{index}"
                    / f"0x{symbol.address:x}"
                )
                target_root = Path(build_cfg.output.target_dir).absolute()
                _reject_path_components(target_root)
                _reject_path_components(unit)
                if not unit.absolute().is_relative_to(target_root):
                    raise ValueError(f"transform unit escapes target root for 0x{symbol.address:x}")
                source = unit / symbol.output_path
                obj = unit / (Path(symbol.output_path).stem + ".o")
                transform_evidence = unit / "transform-evidence.json"
                try:
                    _require_regular_contained_file(source, unit)
                    _require_regular_contained_file(obj, unit)
                    _require_regular_contained_file(transform_evidence, unit)
                except ValueError as exc:
                    raise ValueError(f"transform evidence missing or unsafe for 0x{symbol.address:x}") from exc
                if not source.is_file() or not obj.is_file() or not transform_evidence.is_file():
                    raise ValueError(f"transform evidence missing for 0x{symbol.address:x}")
                target_source = staging / symbol.output_path
                target_object = staging / Path(symbol.output_path).with_suffix(".o")
                target_evidence = staging / "transform-evidence" / f"0x{symbol.address:x}.json"
                _copy_atomic(source, target_source)
                _copy_atomic(obj, target_object)
                _copy_atomic(transform_evidence, target_evidence)
                from re_agent.build.evidence import load_transform_evidence

                load_transform_evidence(target_evidence)
                checkpoints.append(
                    TargetCheckpoint(
                        address=symbol.address,
                        name=symbol.name,
                        status="compiled",
                        source_sha256=_sha256(target_source),
                        output_sha256=_sha256(target_source),
                        signature=symbol.signature,
                        calling_convention=symbol.calling_convention.value,
                        output_path=symbol.output_path,
                        input_sha256=_sha256(input_source),
                        generated_sha256=_sha256(target_source),
                        object_sha256=_sha256(target_object),
                        verdicts=("MANIFEST_BOUND", "COMPILE_PASS"),
                        transform_evidence_path=target_evidence.relative_to(staging).as_posix(),
                        transform_evidence_sha256=_sha256(target_evidence),
                    )
                )
                _atomic_json(
                    checkpoint_file,
                    [item.as_dict() for item in sorted(checkpoints, key=lambda item: item.key())],
                )
        else:
            if not checkpoint_file.is_file():
                raise ValueError("run has no transform checkpoints; run project transform first")
            raw = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            checkpoints = [TargetCheckpoint(**item) for item in raw]
        checkpoints = sorted(checkpoints, key=lambda item: item.key())
        if tuple(item.key() for item in checkpoints) != expected:
            raise ValueError("current full manifest coverage is required before recipe execution")
        for symbol, checkpoint in zip(
            sorted(manifest.symbols, key=lambda item: (item.address, item.name)), checkpoints, strict=True
        ):
            index = expected.index((symbol.address, symbol.name))
            if not _target_checkpoint_valid(
                checkpoint,
                symbol,
                staging,
                input_source=_source_for_symbol(build_cfg, symbol),
                manifest=manifest,
                verified_contract=config.contracts.verified_manifest,
                project_fingerprint=project_context.identity.project_fingerprint,
                snapshot_fingerprint=project_context.identity.snapshot_manifest_sha256,
                run_id=f"{run_id}-{index}",
                llm_config=effective_llm_config,
                compile_command=compile_command,
            ):
                raise ValueError(f"stale or incomplete checkpoint for 0x{symbol.address:x}")
        _write_recipe_manifests(staging, checkpoints, recipe)
        if phase == "transform":
            print(f"Bulk transform complete: {len(checkpoints)} manifest targets")
            return 0

        try:
            recipe_result = run_recipe(recipe, staging)
        except Exception as exc:
            _write_failure_evidence(
                run_root,
                run_id,
                identities,
                "recipe invocation failed",
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            raise
        if not recipe_result.successful:
            _write_failure_evidence(
                run_root,
                run_id,
                identities,
                "recipe failed",
                returncode=recipe_result.returncode,
                timed_out=recipe_result.timed_out,
                result_error=recipe_result.error,
                stdout=recipe_result.stdout,
                stderr=recipe_result.stderr,
            )
            raise ValueError(
                "build recipe failed: "
                f"{recipe_result.error or recipe_result.stderr.strip() or recipe_result.returncode}"
            )
        for symbol, checkpoint in zip(
            sorted(manifest.symbols, key=lambda item: (item.address, item.name)), checkpoints, strict=True
        ):
            index = expected.index((symbol.address, symbol.name))
            if not _target_checkpoint_valid(
                checkpoint,
                symbol,
                staging,
                input_source=_source_for_symbol(build_cfg, symbol),
                manifest=manifest,
                verified_contract=config.contracts.verified_manifest,
                project_fingerprint=project_context.identity.project_fingerprint,
                snapshot_fingerprint=project_context.identity.snapshot_manifest_sha256,
                run_id=f"{run_id}-{index}",
                llm_config=effective_llm_config,
                compile_command=compile_command,
            ):
                raise ValueError(f"recipe mutated staged input for 0x{symbol.address:x}")
        _, produced = recipe.validate_paths(staging)
        artifact = staging / "artifact"
        if produced != artifact:
            shutil.copyfile(produced, artifact)
        evidence = BuildEvidence(
            project_fingerprint=project_context.identity.project_fingerprint,
            manifest_sha256=manifest.sha256_hash,
            recipe_sha256=recipe.recipe_sha256,
            targets=tuple(checkpoints),
            output_path="artifact",
            output_sha256=_sha256(artifact),
            toolchain_sha256=link_command.executable_sha256,
            schema_version=2,
            run_id=run_id,
            source_coverage=expected,
            object_coverage=expected,
            stdout=recipe_result.stdout,
            stderr=recipe_result.stderr,
            exit_status=recipe_result.returncode,
            timed_out=recipe_result.timed_out,
            artifact_sha256=_sha256(artifact),
            compiler_sha256=compile_command.executable_sha256,
        )
        if (
            evidence.project_fingerprint != identities["project_fingerprint"]
            or evidence.manifest_sha256 != identities["manifest_sha256"]
            or evidence.recipe_sha256 != identities["recipe_sha256"]
            or evidence.toolchain_sha256 != identities["toolchain_sha256"]
            or evidence.compiler_sha256 != identities["compiler_sha256"]
        ):
            raise ValueError("build evidence identity mismatch")
        evidence = save_evidence(evidence, staging / "evidence")
        validate_bulk_evidence(
            evidence,
            expected,
            project_fingerprint=project_context.identity.project_fingerprint,
            manifest_sha256=manifest.sha256_hash,
            recipe_sha256=recipe.recipe_sha256,
        )
        publication = publish_build(staging, root / "build", run_id)
        print(f"Build published: {publication.publication_id}")
        return 0
    except Exception as exc:
        print(f"Project build rejected: {exc}", file=sys.stderr)
        return 2


def _cmd_build_project(args: argparse.Namespace, project_context: Any, config: Any) -> int:
    """Run project orchestration while holding the OS-backed run lock."""
    from re_agent.build.run_lock import RunLock

    run_id = getattr(args, "run_id", None) or f"run-{uuid.uuid4().hex}"
    if not run_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        print("Error: --run-id must be a safe path component", file=sys.stderr)
        return 2
    run_root = project_context.root / "build" / "runs" / run_id
    try:
        with RunLock(run_root, metadata={"run_id": run_id}):
            return _cmd_build_project_unlocked(
                args,
                project_context,
                config,
                run_id=run_id,
                run_root=run_root,
            )
    except Exception as exc:
        print(f"Project build rejected: {exc}", file=sys.stderr)
        return 2


def cmd_build(args: argparse.Namespace) -> int:
    """Build an owned project; legacy direct orchestration is not supported."""
    project_root_raw = getattr(args, "project_root", None)
    if not project_root_raw:
        print("Error: build requires --project-root; invoke an explicit project build", file=sys.stderr)
        return 2

    try:
        from re_agent.project.context import load_verified_project

        project_context = load_verified_project(Path(project_root_raw))
    except (OSError, ValueError) as exc:
        print(f"Project error: {exc}", file=sys.stderr)
        return 2

    try:
        config = load_config(Path(args.config), verified_contract_override=project_context.verified_abi_manifest)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    config.build.input.ghidra_exports = str(project_context.snapshot_root)
    config.contracts.verified_manifest = project_context.verified_abi_manifest
    return _cmd_build_project(args, project_context, config)
