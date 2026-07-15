"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_identity(path: Path) -> str:
    try:
        return _sha256(path)
    except OSError as exc:
        raise ValueError(f"config identity cannot be read: {path}") from exc


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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


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


def _target_checkpoint_valid(checkpoint: Any, symbol: Any, staging: Path) -> bool:
    """Validate checkpoint identity and rehash both staged artifacts on resume."""
    try:
        source = staging / symbol.output_path
        object_path = staging / Path(symbol.output_path).with_suffix(".o")
        return (
            checkpoint.key() == (symbol.address, symbol.name)
            and checkpoint.status == "compiled"
            and checkpoint.signature == symbol.signature
            and checkpoint.calling_convention == symbol.calling_convention.value
            and checkpoint.output_path == symbol.output_path
            and "MANIFEST_BOUND" in checkpoint.verdicts
            and "COMPILE_PASS" in checkpoint.verdicts
            and source.is_file()
            and object_path.is_file()
            and checkpoint.source_sha256 == _sha256(source)
            and checkpoint.generated_sha256 == _sha256(source)
            and checkpoint.output_sha256 == _sha256(source)
            and checkpoint.input_sha256 == _sha256(source)
            and checkpoint.object_sha256 == _sha256(object_path)
        )
    except (AttributeError, OSError, ValueError):
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


def _cmd_build_project(args: argparse.Namespace, project_context: Any, config: Any) -> int:
    """Release 4 project orchestration: bulk transform, recipe, evidence, publish."""
    from re_agent.build.bulk import validate_bulk_evidence
    from re_agent.build.evidence import BuildEvidence, TargetCheckpoint, save_evidence
    from re_agent.build.recipe import run_recipe
    from re_agent.build.transform.manifest_bound_transform import run_manifest_bound_transform
    from re_agent.project.publish import publish_build
    from re_agent.toolchain.activation import resolve_capability

    root = project_context.root
    if any(getattr(args, name, None) is not None for name in ("address", "module", "subunit", "max_subunits")):
        print("Error: project mode rejects legacy transform selectors", file=sys.stderr)
        return 2
    if getattr(args, "allow_partial", False):
        print("Error: --allow-partial is not supported; project publication is all-or-nothing", file=sys.stderr)
        return 2
    if getattr(args, "no_persist", False):
        print("Error: project mode requires persistence", file=sys.stderr)
        return 2
    phase = getattr(args, "phase", None) or "transform"
    if phase == "analyze":
        print("Error: project mode has no legacy analyze phase", file=sys.stderr)
        return 2
    if phase == "verify-recipe":
        args.verify_recipe = True
        phase = "assemble"
    if phase not in {"transform", "assemble", "link", "package"}:
        print("Error: project mode accepts transform, link, package, or verify-recipe", file=sys.stderr)
        return 2

    run_id = getattr(args, "run_id", None) or f"run-{uuid.uuid4().hex}"
    if not run_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        print("Error: --run-id must be a safe path component", file=sys.stderr)
        return 2
    run_root = root / "build" / "runs" / run_id
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
                    profile_path=Path(args.profile) if args.profile else None,
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
            project_root=root, capability="compile", profile_path=Path(args.profile) if args.profile else None
        )
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
        }
        build_cfg.input.ghidra_exports = str(project_context.snapshot_root)
        if not Path(build_cfg.output.target_dir).is_absolute():
            build_cfg.output.target_dir = str(root / build_cfg.output.target_dir)
        checkpoint_file = run_root / "checkpoints.json"
        identity_file = run_root / "run.json"
        identity_payload = {"schema_version": 1, "run_id": run_id, **identities}
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
                if prior is not None and _target_checkpoint_valid(prior, symbol, staging):
                    checkpoints.append(prior)
                    continue
                stale_unit = (
                    Path(build_cfg.output.target_dir)
                    / ".manifest-bound"
                    / f"{run_id}-{index}"
                    / f"0x{symbol.address:x}"
                )
                if stale_unit.exists() or stale_unit.is_symlink():
                    shutil.rmtree(stale_unit, ignore_errors=True)
                result = run_manifest_bound_transform(
                    build_cfg,
                    config.llm,
                    config.contracts.verified_manifest,
                    symbol.address,
                    run_id=f"{run_id}-{index}",
                    persist=True,
                    verified_compile_command=compile_command,
                )
                if not result.successful:
                    raise ValueError(f"transform failed for 0x{symbol.address:x}: {getattr(result, 'error', '')}")
                unit = (
                    Path(build_cfg.output.target_dir)
                    / ".manifest-bound"
                    / f"{run_id}-{index}"
                    / f"0x{symbol.address:x}"
                )
                source = unit / symbol.output_path
                obj = unit / (Path(symbol.output_path).stem + ".o")
                if not source.is_file() or not obj.is_file():
                    raise ValueError(f"transform evidence missing for 0x{symbol.address:x}")
                target_source = staging / symbol.output_path
                target_object = staging / Path(symbol.output_path).with_suffix(".o")
                _copy_atomic(source, target_source)
                _copy_atomic(obj, target_object)
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
                        input_sha256=_sha256(target_source),
                        generated_sha256=_sha256(target_source),
                        object_sha256=_sha256(target_object),
                        verdicts=("MANIFEST_BOUND", "COMPILE_PASS"),
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
            if not _target_checkpoint_valid(checkpoint, symbol, staging):
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
            if not _target_checkpoint_valid(checkpoint, symbol, staging):
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


def _dry_report(
    exit_code: int,
    summary: dict[str, object],
    results: list[dict[str, object]] | None = None,
    *,
    usage: dict[str, int] | None = None,
    budget: dict[str, object] | None = None,
) -> None:
    complete_summary: dict[str, object] = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "incomplete": 0,
        "hard_rejects": 0,
        "budget_exceeded": 0,
        "provider_errors": 0,
        "contract_failed": exit_code != 0,
    }
    for key in complete_summary:
        if key in summary:
            complete_summary[key] = summary[key]
    print(
        json.dumps(
            {
                "run_type": "no-persist",
                "exit_code": exit_code,
                "summary": complete_summary,
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_calls": 0},
                "budget": budget
                or {
                    "calls_remaining": 0,
                    "tokens_remaining": 0,
                    "compile_retry_calls_remaining": 0,
                    "exceeded": False,
                    "exceeded_reason": "",
                },
                "results": results or [],
            },
            separators=(",", ":"),
        )
    )


def cmd_build(args: argparse.Namespace) -> int:
    dry_run = bool(getattr(args, "no_persist", False))
    project_root_raw = getattr(args, "project_root", None)
    profile_raw = getattr(args, "profile", None)

    def cli_error(message: str) -> int:
        if dry_run:
            _dry_report(2, {"error": message})
        else:
            print(message, file=sys.stderr)
        return 2

    if getattr(args, "allow_partial", False):
        return cli_error("Error: --allow-partial is not supported")
    if profile_raw and not project_root_raw:
        return cli_error("Error: --profile requires --project-root")
    if not project_root_raw and (
        getattr(args, "verify_recipe", False) or getattr(args, "phase", None) in {"link", "package", "verify-recipe"}
    ):
        return cli_error("Error: link/package/verify-recipe require --project-root")
    project_context = None
    if project_root_raw:
        try:
            from re_agent.project.context import load_verified_project

            project_context = load_verified_project(Path(project_root_raw))
        except (OSError, ValueError) as exc:
            return cli_error(f"Project error: {exc}")
    try:
        if project_context is None:
            config = load_config(Path(args.config))
        else:
            config = load_config(Path(args.config), verified_contract_override=project_context.verified_abi_manifest)
    except (ValueError, FileNotFoundError) as exc:
        if dry_run:
            _dry_report(2, {"error": str(exc)})
            return 2
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    build_cfg = config.build
    llm_cfg = config.llm
    pipeline_cfg = config.pipeline
    if project_context is not None:
        build_cfg.input.ghidra_exports = str(project_context.snapshot_root)
        config.contracts.verified_manifest = project_context.verified_abi_manifest

        # Release 4 is deliberately a separate project-mode surface.  Do not
        # let legacy module/phase orchestration accidentally mix with it.
        return _cmd_build_project(args, project_context, config)

    persist = not getattr(args, "no_persist", False)
    phase = getattr(args, "phase", None)
    address = getattr(args, "address", None)
    module = getattr(args, "module", None)
    subunit = getattr(args, "subunit", None)

    # ABI-preserving builds must be explicit and bounded.  Keep these checks
    # at the CLI boundary so an invalid invocation cannot start a mutating
    # phase before it is rejected.
    contracts_cfg = getattr(config, "contracts", None)
    preserve_abi = getattr(contracts_cfg, "transformation_policy", None) == "preserve_abi"
    verified_compile_command = None
    if address is not None and (module is not None or subunit is not None):
        return cli_error("Error: --address cannot be combined with --module or --subunit")
    if address is not None and getattr(args, "max_subunits", None) is not None:
        return cli_error("Error: --address cannot be combined with --max-subunits")
    if address is not None and not preserve_abi:
        return cli_error("Error: --address is only valid with contracts transformation_policy=preserve_abi")
    if preserve_abi:
        if phase == "analyze" and address is not None:
            return cli_error("Error: --phase analyze cannot be combined with --address")
        if phase != "transform" and address is not None:
            return cli_error("Error: --address is only valid with --phase transform")
        if phase == "analyze" and any(x is not None for x in (module, subunit, getattr(args, "max_subunits", None))):
            return cli_error("Error: preserve_abi analyze does not accept transform selectors")
        if phase == "transform" and address is None:
            return cli_error("Error: --phase transform with preserve_abi requires exactly one --address")
        if phase in (None, "assemble"):
            phase_label = "all phases" if phase is None else "--phase assemble"
            return cli_error(f"Error: preserve_abi does not allow {phase_label}; use --phase analyze")

    # --no-persist is only valid with an explicit --phase transform.
    # With --phase analyze, --phase assemble, or no --phase (which runs
    # all phases including analyze/assemble), --no-persist would silently
    # skip writes — this is a user error.
    if not persist and phase != "transform":
        phase_label = phase if phase else "(all phases)"
        return cli_error(f"Error: --no-persist is only valid with --phase transform (got --phase {phase_label})")

    if project_context is not None and persist and phase == "transform":
        try:
            from re_agent.toolchain.activation import resolve_capability

            (verified_compile_command,) = resolve_capability(
                project_root=project_context.root,
                capability="compile",
                profile_path=Path(profile_raw) if profile_raw else None,
            )
        except ValueError as exc:
            return cli_error(f"Toolchain error: {exc}")

    state = PipelineState(pipeline_cfg.state_file) if persist else None

    def preserve_failure(message: str) -> int:
        if persist and state is not None:
            state.update_build("failed")
            state.flush()
            print(message, file=_out)
        else:
            _dry_report(2, {"error": message})
        return 2

    phases = [phase] if phase else ["analyze", "transform", "assemble"]

    from re_agent.build.analyze.clusterer import cluster
    from re_agent.build.analyze.graph_builder import build_graph
    from re_agent.build.analyze.indexer import index_modules
    from re_agent.build.assemble.tree_builder import build_tree
    from re_agent.build.transform.module_processor import process_modules

    has_incomplete = False
    contract_failed = False

    # --no-persist: human messages go to stderr; stdout is reserved for JSON.
    _out = sys.stderr if not persist else sys.stdout

    try:
        if "analyze" in phases:
            if persist:
                print("=== Phase 1/3: Analyze (call graph + clustering) ===")
            graph = build_graph(build_cfg)
            modules = cluster(graph, build_cfg)
            index_modules(modules, build_cfg)

            from re_agent.build.analyze.decls_generator import write_decls_header

            decls_path = write_decls_header(config)
            if decls_path is not None:
                print(f"Wrote declarations header: {decls_path}", file=_out)
            mc = modules["metadata"]["module_count"]
            oc = modules["metadata"]["orphan_count"]
            if persist:
                print(f"Analyze complete: {mc} modules, {oc} orphans")
            if persist and state is not None:
                state.update_build("in_progress", phase="analyze", modules_completed=[])

        if "transform" in phases:
            if persist:
                print("=== Phase 2/3: Transform (LLM code refinement) ===")
            if preserve_abi:
                from re_agent.build.transform.manifest_bound_transform import (
                    ManifestBoundTransformError,
                    ManifestBoundVerdict,
                    run_manifest_bound_transform,
                )

                try:
                    if contracts_cfg is None or address is None:
                        raise ManifestBoundTransformError(
                            "preserve_abi transform requires a verified manifest and address"
                        )
                    result = run_manifest_bound_transform(
                        build_cfg,
                        llm_cfg,
                        contracts_cfg.verified_manifest,
                        address,
                        run_id=getattr(args, "run_id", "") or "",
                        persist=persist,
                        verified_compile_command=verified_compile_command,
                    )
                except ManifestBoundTransformError as exc:
                    if not persist:
                        _dry_report(2, {"total": 1, "failed": 1, "hard_rejects": 1})
                    else:
                        return preserve_failure(f"Transform rejected: {exc}")
                    return 2
                except Exception as exc:
                    # Provider/compiler integration failures are non-committing
                    # failures; do not let the CLI continue into assemble.
                    if not persist:
                        _dry_report(2, {"total": 1, "failed": 1, "provider_errors": 1})
                    else:
                        return preserve_failure(f"Transform failed: {exc}")
                    return 2
                if persist and not result.successful:
                    return preserve_failure("Transform rejected: incomplete or unknown manifest-bound verdict")
                if not persist and result.verdict in (
                    ManifestBoundVerdict.PROVIDER_ERROR,
                    ManifestBoundVerdict.BUDGET_EXCEEDED,
                ):
                    result_entry = {
                        "function": f"0x{result.address:X}",
                        "verdict": result.verdict.value,
                        "compiles": False,
                        "files_matched": 0,
                        "match_strategy": "explicit_identity",
                        "identity_state": "explicit",
                        "identity_reason": result.error,
                        "compile_error_category": None,
                        "files": [],
                    }
                    _dry_report(
                        2,
                        {
                            "total": 1,
                            "failed": 1,
                            "provider_errors": result.provider_errors,
                            "budget_exceeded": int(result.verdict is ManifestBoundVerdict.BUDGET_EXCEEDED),
                            "contract_failed": True,
                        },
                        [result_entry],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 2
                if not persist and result.verdict is ManifestBoundVerdict.VALIDATION_ERROR:
                    _dry_report(
                        2,
                        {"total": 1, "failed": 1, "hard_rejects": 1, "contract_failed": True},
                        [
                            {
                                "function": f"0x{result.address:X}",
                                "verdict": result.verdict.value,
                                "compiles": False,
                                "files_matched": 0,
                                "match_strategy": "rejected_identity",
                                "identity_state": "rejected",
                                "identity_reason": result.error,
                                "compile_error_category": None,
                                "files": [],
                            }
                        ],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 2
                if result.verdict == "COMPILE_FAIL":
                    if not persist:
                        _dry_report(
                            2,
                            {"error": "compilation failed"},
                            [
                                {
                                    "function": f"0x{result.address:X}",
                                    "verdict": result.verdict.value,
                                    "compiles": False,
                                    "files_matched": 0,
                                    "match_strategy": None,
                                    "identity_state": None,
                                    "identity_reason": "",
                                    "compile_error_category": "compile",
                                    "files": [],
                                }
                            ],
                        )
                    else:
                        return preserve_failure(f"Transform rejected: compilation failed\n{result.compiler_log}")
                    return 2
                if not persist:
                    if result.verdict is not ManifestBoundVerdict.SKIPPED_COMPILE or result.compiles:
                        _dry_report(2, {"error": "invalid dry-run verdict"})
                        return 2
                    _dry_report(
                        0,
                        {"total": 1, "failed": 1},
                        [
                            {
                                "function": f"0x{result.address:X}",
                                "verdict": result.verdict.value,
                                "compiles": False,
                                "files_matched": 1,
                                "match_strategy": "explicit_identity",
                                "identity_state": "explicit",
                                "identity_reason": "",
                                "compile_error_category": None,
                                "files": [{"path": result.path}],
                            }
                        ],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 0
                print(
                    f"Transform complete: MANIFEST_BOUND + COMPILE_PASS for 0x{result.address:x}",
                    file=_out,
                )
                summary = {"total": 1, "passed": 1, "incomplete": 0, "budget_exceeded": 0}
            else:
                summary = process_modules(
                    build_cfg,
                    llm_cfg,
                    module=getattr(args, "module", None),
                    subunit=getattr(args, "subunit", None),
                    max_subunits=getattr(args, "max_subunits", None),
                    run_id=getattr(args, "run_id", "") or "",
                    persist=persist,
                )
            if persist and state is not None:
                state.update_build("in_progress", phase="transform", modules_completed=[])

            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            incomplete = summary.get("incomplete", 0)
            budget_exceeded = summary.get("budget_exceeded", 0)
            contract_failed = bool(summary.get("contract_failed", False))

            if total > 0 and passed > 0:
                parts = [f"{passed}/{total} functions compiled"]
                if incomplete:
                    parts.append(f"{incomplete} incomplete targets")
                    has_incomplete = True
                if budget_exceeded:
                    parts.append(f"{budget_exceeded} budget exceeded")
                    has_incomplete = True
                print(f"Transform complete: {', '.join(parts)}", file=_out)
            elif total > 0 and passed == 0:
                if contract_failed:
                    msg = (
                        f"CONTRACT FAILED — {incomplete}/{total} functions have INCOMPLETE_TARGETS "
                        "(TARGET contract required but recovery exhausted)"
                    )
                    print(f"Transform rejected: {msg}", file=_out)
                    has_incomplete = True
                elif budget_exceeded:
                    msg = f"{budget_exceeded}/{total} functions BUDGET_EXCEEDED — transform capped"
                    print(f"Transform rejected: {msg}", file=_out)
                    has_incomplete = True
                elif incomplete:
                    msg = f"{incomplete}/{total} functions have INCOMPLETE_TARGETS — recovery exhausted"
                    print(f"Transform complete: {msg}", file=_out)
                    has_incomplete = True
                else:
                    print(f"Transform complete: 0/{total} functions compiled — see report for details", file=_out)
            else:
                print("Transform complete: no functions processed", file=_out)

        if "assemble" in phases:
            if contract_failed or has_incomplete:
                print("Skipping assemble: contract failed (TARGET violations).", file=_out)
            else:
                if persist:
                    print("=== Phase 3/3: Assemble (project tree) ===")
                build_tree(build_cfg)
                if persist and state is not None:
                    state.update_build("completed")
    except Exception:
        if persist and state is not None:
            state.update_build("failed")
            state.flush()
        raise

    if persist and state is not None:
        state.flush()

    if contract_failed or has_incomplete:
        code = 2 if contract_failed else 1
        label = "CONTRACT FAILED" if contract_failed else "INCOMPLETE"
        print(f"Build {label}: TARGET requirements not met.", file=_out)
        return code
    if persist:
        print("Build complete.")
    return 0
