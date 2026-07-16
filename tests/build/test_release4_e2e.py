"""End-to-end coverage for the generic Release 4 project-mode build CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from re_agent.build.evidence import TransformEvidence, load_evidence, load_transform_evidence, save_transform_evidence
from re_agent.build.recipe import BuildRecipe
from re_agent.build.transform.manifest_bound_transform import build_preserve_abi_prompt
from re_agent.cli.main import main
from re_agent.contracts.manifest import manifest_from_symbols
from re_agent.contracts.model import Architecture, CallingConvention, Symbol
from re_agent.contracts.runtime import VerifiedContract
from re_agent.project.publish import DestinationExistsError, load_active_build, publish_build


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _readable_python_executable() -> Path:
    for candidate in (Path(sys.prefix) / "python.exe", Path(sys.base_prefix) / "python.exe", Path(sys.executable)):
        try:
            candidate.read_bytes()
        except OSError:
            continue
        return candidate
    raise RuntimeError("test Python executable is not readable")


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "generic-project"
    root.mkdir()
    (root / "config.yml").write_text("fixture: release4\n", encoding="utf-8")
    snapshot = root / "snapshot"
    snapshot.mkdir()
    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X64,
        pointer_size=8,
        symbols=[
            Symbol(0x2000, "nested", "int nested()", CallingConvention.CDECL, "deep/nested.cpp"),
            Symbol(0x1000, "root", "int root()", CallingConvention.CDECL, "root.cpp"),
        ],
    )
    compiled = tmp_path / "compiled"
    verified = VerifiedContract(manifest, root / "manifest.json", _digest(b"raw"), manifest.sha256_hash)
    config = SimpleNamespace(
        build=SimpleNamespace(
            input=SimpleNamespace(ghidra_exports="", decompiled_dir=str(snapshot)),
            output=SimpleNamespace(target_dir=str(compiled)),
        ),
        llm=SimpleNamespace(provider="fake", model="fixture"),
        pipeline=SimpleNamespace(),
        contracts=SimpleNamespace(verified_manifest=verified),
    )
    context = SimpleNamespace(
        root=root,
        snapshot_root=snapshot,
        verified_abi_manifest=verified,
        identity=SimpleNamespace(
            project_fingerprint=_digest(b"project"),
            snapshot_manifest_sha256=_digest(b"snapshot"),
        ),
    )
    compiler_code = (
        "import sys; from pathlib import Path; "
        "Path(sys.argv[2]).write_bytes(Path(sys.argv[3]).read_bytes() + b'\\ncompiled-by-verified-compiler')"
    )
    compiler_executable = _readable_python_executable()
    command = SimpleNamespace(
        argv=(str(compiler_executable), "-c", compiler_code),
        executable_sha256=_digest(compiler_executable.read_bytes()),
    )
    recipe = BuildRecipe(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist/app.bin').parent.mkdir(); "
            "Path('dist/app.bin').write_bytes(b'artifact')",
        ),
        output="dist/app.bin",
    )
    transform_calls: list[int] = []
    recipe_calls: list[object] = []
    for symbol in manifest.symbols:
        (snapshot / f"0x{symbol.address:x}__{symbol.name}.cpp").write_bytes(
            f"int {symbol.name}() {{ return {symbol.address}; }}\n".encode()
        )

    def fake_transform(
        build_cfg,
        _llm,
        _manifest,
        address,
        *,
        run_id,
        persist,
        verified_compile_command,
        project_fingerprint,
        snapshot_fingerprint,
    ):
        transform_calls.append(address)
        symbol = next(item for item in manifest.symbols if item.address == address)
        unit = Path(build_cfg.output.target_dir) / ".manifest-bound" / run_id / f"0x{address:x}"
        source = unit / symbol.output_path
        source.parent.mkdir(parents=True, exist_ok=True)
        generated_bytes = f"int {symbol.name}() {{ return {address}; }}\n".encode()
        source.write_bytes(generated_bytes)
        object_path = unit / f"{Path(symbol.output_path).stem}.o"
        object_bytes = b"object"
        object_path.write_bytes(object_bytes)
        input_path = Path(build_cfg.input.decompiled_dir) / f"0x{address:x}__{symbol.name}.cpp"
        input_bytes = input_path.read_bytes()
        input_text = input_bytes.decode("utf-8")
        prompt = build_preserve_abi_prompt(symbol, input_text, manifest)
        raw_response = f"// TARGET: 0x{address:x}\n// FILE: {symbol.output_path}\n{generated_bytes.decode()}"
        save_transform_evidence(
            TransformEvidence(
                project_fingerprint=project_fingerprint,
                snapshot_fingerprint=snapshot_fingerprint,
                manifest_raw_sha256=verified.raw_sha256,
                manifest_sha256=verified.canonical_sha256,
                run_id=run_id,
                target_address=address,
                target_name=symbol.name,
                target_signature=symbol.signature,
                target_calling_convention=symbol.calling_convention.value,
                target_output_path=symbol.output_path,
                messages=(("system", prompt.system), ("user", prompt.user)),
                llm_config={"provider": "fake", "model": "fixture"},
                input_text=input_text,
                input_sha256=_digest(input_bytes),
                raw_response=raw_response,
                raw_response_sha256="",
                generated_sha256=_digest(generated_bytes),
                object_sha256=_digest(object_bytes),
                compiler_argv=tuple(verified_compile_command.argv),
                compiler_executable_sha256=verified_compile_command.executable_sha256,
            ),
            unit / "transform-evidence.json",
        )
        return SimpleNamespace(successful=True)

    monkeypatch.setattr("re_agent.cli.cmd_build.load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr("re_agent.project.context.load_verified_project", lambda _root: context)
    monkeypatch.setattr("re_agent.cli.cmd_build._project_recipe", lambda *_args: (recipe, command))
    monkeypatch.setattr("re_agent.toolchain.activation.resolve_capability", lambda **kwargs: (command,))
    monkeypatch.setattr(
        "re_agent.build.transform.manifest_bound_transform.run_manifest_bound_transform", fake_transform
    )
    real_run_recipe = __import__("re_agent.build.recipe", fromlist=["run_recipe"]).run_recipe

    def tracked_recipe(*args, **kwargs):
        recipe_calls.append(args)
        return real_run_recipe(*args, **kwargs)

    monkeypatch.setattr("re_agent.build.recipe.run_recipe", tracked_recipe)
    return SimpleNamespace(
        root=root,
        manifest=manifest,
        context=context,
        config=config,
        recipe=recipe,
        command=command,
        transform_calls=transform_calls,
        recipe_calls=recipe_calls,
        compiled=compiled,
    )


def _run(project, *extra: str) -> int:
    return main(["--config", str(project.root / "config.yml"), "build", "--project-root", str(project.root), *extra])


def test_project_transform_schedules_full_manifest_and_preserves_nested_paths(project) -> None:
    assert _run(project, "--phase", "transform", "--run-id", "ordered") == 0
    assert project.transform_calls == [0x1000, 0x2000]
    staging = project.root / "build" / "runs" / "ordered" / "staging"
    assert (staging / "root.cpp").is_file()
    assert (staging / "root.o").is_file()
    assert (staging / "deep/nested.cpp").is_file()
    assert (staging / "deep/nested.o").is_file()


def test_failed_target_blocks_recipe_invocation(project, monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*args, **kwargs):
        project.transform_calls.append(args[3])
        return SimpleNamespace(successful=False, error="target failed")

    monkeypatch.setattr("re_agent.build.transform.manifest_bound_transform.run_manifest_bound_transform", failed)
    assert _run(project, "--phase", "transform", "--run-id", "failed-target") == 2
    assert project.recipe_calls == []


def test_stale_or_incomplete_checkpoint_blocks_recipe(project) -> None:
    assert _run(project, "--phase", "link", "--run-id", "missing-checkpoint") == 2
    assert project.recipe_calls == []


def test_checkpoints_without_matching_immutable_run_identity_are_not_reused(project) -> None:
    assert _run(project, "--phase", "transform", "--run-id", "interrupted") == 0
    run_root = project.root / "build" / "runs" / "interrupted"
    identity = run_root / "run.json"
    assert identity.is_file()
    assert (run_root / "checkpoints.json").is_file()
    identity.unlink()

    assert _run(project, "--phase", "link", "--run-id", "interrupted") == 2
    assert not (project.root / "build" / "builds" / "interrupted").exists()


def test_checkpoints_cannot_be_relabeled_under_a_different_run_identity(project) -> None:
    assert _run(project, "--phase", "transform", "--run-id", "original") == 0
    original = project.root / "build" / "runs" / "original"
    relabeled = project.root / "build" / "runs" / "relabeled"
    relabeled.mkdir(parents=True)
    shutil.copyfile(original / "checkpoints.json", relabeled / "checkpoints.json")

    assert _run(project, "--phase", "link", "--run-id", "relabeled") == 2
    assert not (project.root / "build" / "builds" / "relabeled").exists()

    payload = json.loads((original / "run.json").read_text(encoding="utf-8"))
    payload["project_fingerprint"] = _digest(b"different-project")
    (original / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    assert _run(project, "--phase", "link", "--run-id", "original") == 2


@pytest.mark.parametrize("corruption", ["stale-source", "missing-object"])
def test_current_evidence_corruption_blocks_recipe(project, corruption: str) -> None:
    assert _run(project, "--phase", "transform", "--run-id", "guarded") == 0
    staging = project.root / "build" / "runs" / "guarded" / "staging"
    if corruption == "stale-source":
        (staging / "root.cpp").write_text("int root() { return -1; }\n", encoding="utf-8")
    else:
        (staging / "deep/nested.o").unlink()

    assert _run(project, "--phase", "link", "--run-id", "guarded") == 2
    assert project.recipe_calls == []


@pytest.mark.parametrize("mutation", ["source", "object"])
def test_recipe_mutating_staged_input_after_gate_blocks_publication(project, monkeypatch, mutation: str) -> None:
    code = (
        "import sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_bytes(b'external source mutation'); "
        "Path(sys.argv[2]).write_bytes(b'external object mutation'); "
        "Path('dist').mkdir(); Path('dist/app.bin').write_bytes(b'artifact')"
    )
    recipe = replace(
        project.recipe,
        argv=(sys.executable, "-c", code, "{source}", "{object}"),
    )
    monkeypatch.setattr(
        "re_agent.cli.cmd_build._project_recipe",
        lambda *_args: (recipe, SimpleNamespace(executable_sha256=_digest(b"tool"))),
    )

    run_id = f"mutated-{mutation}"
    assert _run(project, "--phase", "transform", "--run-id", run_id) == 0
    staging = project.root / "build" / "runs" / run_id / "staging"

    assert _run(project, "--phase", "link", "--run-id", run_id) == 2
    assert not (project.root / "build" / "builds" / run_id).exists()
    assert project.recipe_calls
    assert staging.is_dir()
    mutated = staging / ("root.cpp" if mutation == "source" else "root.o")
    assert mutated.read_bytes().startswith(b"external")


def test_successful_recipe_publishes_authenticated_immutable_build(project) -> None:
    assert _run(project, "--phase", "transform", "--run-id", "release") == 0
    assert _run(project, "--phase", "link", "--run-id", "release") == 0
    active = load_active_build(project.root / "build")
    evidence = load_evidence(active.directory / "evidence")
    assert active.directory == project.root / "build" / "builds" / "release"
    assert evidence.run_id == "release"
    run_identity = json.loads((project.root / "build" / "runs" / "release" / "run.json").read_text(encoding="utf-8"))
    assert run_identity["recipe_sha256"] == project.recipe.recipe_sha256
    assert evidence.recipe_sha256 == run_identity["recipe_sha256"]
    assert evidence.partial is False
    assert evidence.schema_version == 2
    for checkpoint in evidence.targets:
        transform_path = active.directory / checkpoint.transform_evidence_path
        source_path = active.directory / checkpoint.output_path
        object_path = active.directory / Path(checkpoint.output_path).with_suffix(".o")
        assert transform_path.is_file()
        assert source_path.is_file()
        assert object_path.is_file()
        assert _digest(transform_path.read_bytes()) == checkpoint.transform_evidence_sha256
        transform = load_transform_evidence(transform_path)
        assert transform.generated_sha256 == _digest(source_path.read_bytes())
        assert transform.object_sha256 == _digest(object_path.read_bytes())
    assert active.artifact_sha256 == _digest(b"artifact")
    pointer = json.loads((project.root / "build" / "active.json").read_text(encoding="utf-8"))
    assert pointer["authentication"]["algorithm"] == "sha256"
    duplicate = project.root / "duplicate"
    duplicate.mkdir()
    (duplicate / "artifact").write_bytes(b"duplicate-artifact")
    (duplicate / "evidence").write_bytes(b"duplicate-evidence")
    with pytest.raises(DestinationExistsError):
        publish_build(duplicate, project.root / "build", "release")


def test_failed_recipe_preserves_previous_active_publication(project, monkeypatch: pytest.MonkeyPatch) -> None:
    old = project.root / "old"
    old.mkdir()
    (old / "artifact").write_bytes(b"old-artifact")
    (old / "evidence").write_bytes(b"old-evidence")
    publish_build(old, project.root / "build", "old")
    before = load_active_build(project.root / "build")
    failing = BuildRecipe(argv=(sys.executable, "-c", "raise SystemExit(9)"), output="dist/app.bin")
    monkeypatch.setattr(
        "re_agent.cli.cmd_build._project_recipe",
        lambda *_args: (failing, SimpleNamespace(executable_sha256=_digest(b"tool"))),
    )
    assert _run(project, "--phase", "transform", "--run-id", "failed-recipe") == 0
    assert _run(project, "--phase", "link", "--run-id", "failed-recipe") == 2
    assert load_active_build(project.root / "build") == before


def test_allow_partial_is_rejected_in_project_mode(project) -> None:
    assert _run(project, "--allow-partial") == 2


def test_build_parser_requires_project_root(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(tmp_path / "config.yml"), "build", "--phase", "link"])
    assert exc_info.value.code == 2


def test_verify_recipe_runs_disposable_witness(project) -> None:
    assert _run(project, "--phase", "verify-recipe", "--run-id", "witness") == 0
    assert len(project.recipe_calls) == 1
    witness_stage = project.recipe_calls[0][1]
    assert witness_stage.name == "stage"
    assert "recipe-witness-" in str(witness_stage.parent)
    assert not witness_stage.parent.exists()


def test_verify_recipe_supplies_usable_disposable_witness_inputs(project, monkeypatch: pytest.MonkeyPatch) -> None:
    code = (
        "import json,sys; from pathlib import Path; "
        "assert all(Path(p).is_file() and Path(p).stat().st_size for p in sys.argv[1:5]); "
        "assert b'compiled-by-verified-compiler' in Path(sys.argv[2]).read_bytes(); "
        "assert json.loads(Path(sys.argv[3]).read_text())['targets']; "
        "assert json.loads(Path(sys.argv[4]).read_text())['objects']; "
        "Path('dist').mkdir(); Path('dist/app.bin').write_bytes(b'witness')"
    )
    recipe = replace(
        project.recipe,
        argv=(sys.executable, "-c", code, "{source}", "{object}", "{build_manifest}", "{object_manifest}"),
    )
    monkeypatch.setattr(
        "re_agent.cli.cmd_build._project_recipe",
        lambda *_args: (recipe, SimpleNamespace(executable_sha256=_digest(b"tool"))),
    )

    assert _run(project, "--phase", "verify-recipe", "--run-id", "witness-inputs") == 0


def test_nested_recipe_cwd_receives_usable_manifest_and_artifact_paths(project, monkeypatch) -> None:
    code = (
        "import json,sys; from pathlib import Path; "
        "assert all(Path(p).is_file() and Path(p).stat().st_size for p in sys.argv[1:5]); "
        "assert json.loads(Path(sys.argv[3]).read_text())['targets']; "
        "assert json.loads(Path(sys.argv[4]).read_text())['objects']; "
        "Path(sys.argv[5]).write_bytes(b'nested-artifact')"
    )
    recipe = replace(
        project.recipe,
        cwd="nested/work",
        output="artifact",
        argv=(
            sys.executable,
            "-c",
            code,
            "../../root.cpp",
            "../../root.o",
            "build-manifest.json",
            "object-manifest.json",
            "../../artifact",
        ),
    )

    def nested_recipe(_root, _profile, staging):
        (staging / "nested/work").mkdir(parents=True, exist_ok=True)
        return recipe, project.command

    monkeypatch.setattr("re_agent.cli.cmd_build._project_recipe", nested_recipe)
    assert _run(project, "--phase", "transform", "--run-id", "nested-cwd") == 0
    assert _run(project, "--phase", "link", "--run-id", "nested-cwd") == 0
    assert (project.root / "build" / "builds" / "nested-cwd" / "artifact").read_bytes() == b"nested-artifact"
