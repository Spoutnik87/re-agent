"""Focused public CLI tests for R6 project verification and replay."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from re_agent.build.recipe import BuildRecipe
from re_agent.build.run_lock import RunLock, RunLockError
from re_agent.cli.cmd_run import cmd_run
from re_agent.cli.main import main
from re_agent.contracts.manifest import manifest_from_symbols
from re_agent.contracts.model import Architecture, CallingConvention, Symbol
from re_agent.contracts.runtime import VerifiedContract
from re_agent.llm.protocol import ProviderUsage
from re_agent.toolchain.activation import VerifiedCommand, activate_profile


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


class FixtureProvider:
    supports_conversations = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cache_hit_tokens = None
    total_cache_miss_tokens = None

    def __init__(self) -> None:
        self.calls = 0

    def send(self, messages, **kwargs: object) -> str:
        self.calls += 1
        match = re.search(r"address: (0x[0-9a-f]+)", messages[-1].content)
        assert match is not None
        address = match.group(1)
        path = "root.cpp" if address == "0x1000" else "deep/nested.cpp"
        name = "root" if address == "0x1000" else "nested"
        return f"// TARGET: {address}\n// FILE: {path}\nint {name}() {{ return {int(address, 16)}; }}"

    def get_usage(self) -> ProviderUsage:
        return ProviderUsage(0, 0, None, None, self.calls)

    def new_conversation(self, system: str) -> str:
        raise AssertionError("conversations are not used")

    def resume(self, conversation_id: str, message: str) -> str:
        raise AssertionError("conversations are not used")

    def delete_conversation(self, conversation_id: str) -> None:
        raise AssertionError("conversations are not used")


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    config_path = root / "config.yml"
    config_path.write_text("fixture: run-r6\n", encoding="utf-8")
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
    verified = VerifiedContract(manifest, root / "manifest.json", _digest(b"raw"), manifest.sha256_hash)
    config = SimpleNamespace(
        build=SimpleNamespace(
            input=SimpleNamespace(ghidra_exports="", decompiled_dir=str(snapshot)),
            output=SimpleNamespace(target_dir=str(root / "compiled"), work_dir=str(root / "work")),
        ),
        llm=SimpleNamespace(provider="fixture", model="fixture-model"),
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
    for symbol in manifest.symbols:
        (snapshot / f"0x{symbol.address:x}__{symbol.name}.cpp").write_bytes(
            f"int {symbol.name}() {{ return {symbol.address}; }}\n".encode()
        )

    compiler_code = (
        "import sys; from pathlib import Path; "
        "Path(sys.argv[2]).write_bytes(Path(sys.argv[3]).read_bytes() + b'\\ncompiled')"
    )
    compiler_executable = _readable_python_executable()
    compiler = VerifiedCommand(
        (str(compiler_executable), "-c", compiler_code), _digest(compiler_executable.read_bytes())
    )
    recipe = BuildRecipe(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('dist').mkdir(); Path('dist/app.bin').write_bytes(b'artifact')",
        ),
        output="dist/app.bin",
    )
    provider = FixtureProvider()
    monkeypatch.setattr("re_agent.cli.cmd_build.load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr("re_agent.project.context.load_verified_project", lambda _root: context)
    monkeypatch.setattr("re_agent.cli.cmd_run.load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr("re_agent.cli.cmd_run.load_verified_project", lambda _root: context)
    monkeypatch.setattr("re_agent.cli.cmd_build._project_recipe", lambda *_args: (recipe, compiler))
    monkeypatch.setattr("re_agent.toolchain.activation.resolve_capability", lambda **kwargs: (compiler,))
    monkeypatch.setattr("re_agent.llm.registry.create_provider", lambda _config: provider)
    return SimpleNamespace(
        root=root,
        config_path=config_path,
        provider=provider,
        recipe=recipe,
        compiler_path=compiler_executable,
    )


def _build(project, run_id: str = "run-1", profile: Path | None = None) -> None:
    common = ["--config", str(project.config_path), "build", "--project-root", str(project.root)]
    if profile is not None:
        common.extend(["--profile", str(profile)])
    assert main([*common, "--phase", "transform", "--run-id", run_id]) == 0


def _run(project, operation: str, run_id: str = "run-1", profile: Path | None = None) -> int:
    argv = [
        "--config",
        str(project.config_path),
        "run",
        operation,
        "--project-root",
        str(project.root),
        "--run-id",
        run_id,
    ]
    if profile is not None:
        argv.extend(["--profile", str(profile)])
    return main(argv)


def _write_profile(project, name: str, *, flags: str = "-c") -> Path:
    profile = project.root / f"{name}.yml"
    profile.write_text(
        "backend: offline-export\n"
        "target: arbitrary\n"
        "compiler:\n"
        f"  command: [{json.dumps(str(project.compiler_path))}]\n"
        f"  flags: [{json.dumps(flags)}]\n",
        encoding="utf-8",
    )
    return profile


def test_verify_and_offline_replay_produce_matching_artifacts(project) -> None:
    _build(project)
    assert _run(project, "verify") == 0
    assert _run(project, "replay") == 0


def test_verify_holds_run_lock_during_validation(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    run_root = project.root / "build" / "runs" / "run-1"
    observed: list[bool] = []
    original_validate = __import__("re_agent.cli.cmd_run", fromlist=["_validate_run"])._validate_run

    def observe_validate(*args, **kwargs):
        observed.append((run_root / ".run.lock").is_file())
        return original_validate(*args, **kwargs)

    monkeypatch.setattr("re_agent.cli.cmd_run._validate_run", observe_validate)
    assert _run(project, "verify") == 0
    assert observed == [True]


def test_build_rejects_active_and_transient_profiles(project) -> None:
    profile = _write_profile(project, "transient-profile")
    activate_profile(project_root=project.root, profile_path=profile)
    common = ["--config", str(project.config_path), "build", "--project-root", str(project.root)]
    assert main([*common, "--profile", str(profile), "--phase", "transform", "--run-id", "ambiguous"]) == 2


def test_run_recheck_rejects_active_profile_created_during_lock(project, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _write_profile(project, "transient-profile")
    _build(project, profile=profile)
    from re_agent.build.run_lock import RunLock as RunLockType

    original_acquire = RunLockType.acquire

    def acquire_then_activate(lock):
        result = original_acquire(lock)
        activate_profile(project_root=project.root, profile_path=profile)
        return result

    monkeypatch.setattr(RunLockType, "acquire", acquire_then_activate)
    assert _run(project, "verify", profile=profile) == 2


@pytest.mark.parametrize("layout", ["missing", "run-symlink", "runs-symlink"])
def test_replay_prelock_path_rejection_does_not_create_lock_file(project, layout: str) -> None:
    runs_root = project.root / "build" / "runs"
    run_root = runs_root / "run-1"
    outside = project.root.parent / f"outside-{layout}"
    lock_path = run_root / ".run.lock"
    if layout == "missing":
        pass
    else:
        outside.mkdir()
        try:
            if layout == "run-symlink":
                runs_root.mkdir(parents=True)
                run_root.symlink_to(outside, target_is_directory=True)
                lock_path = outside / ".run.lock"
            else:
                runs_root.parent.mkdir(parents=True, exist_ok=True)
                runs_root.symlink_to(outside, target_is_directory=True)
                lock_path = outside / "run-1" / ".run.lock"
        except OSError:
            pytest.skip("directory symlinks unavailable")

    assert _run(project, "replay") == 2
    assert not lock_path.exists()


def test_replay_path_substitution_after_lock_fails_closed(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    run_root = project.root / "build" / "runs" / "run-1"
    staging = run_root / "staging"
    outside = project.root.parent / "replay-substitution"
    outside.mkdir()
    from re_agent.build.run_lock import RunLock as RunLockType

    original_acquire = RunLockType.acquire

    def acquire_then_substitute(lock):
        result = original_acquire(lock)
        shutil.rmtree(staging)
        try:
            staging.symlink_to(outside, target_is_directory=True)
        except OSError:
            result.release()
            pytest.skip("directory symlinks unavailable")
        return result

    monkeypatch.setattr(RunLockType, "acquire", acquire_then_substitute)
    assert _run(project, "replay") == 2
    assert not (project.root / "build" / "replays").exists()


def test_build_linked_build_parent_fails_without_escaped_writes(project) -> None:
    outside = project.root.parent / "build-parent-outside"
    outside.mkdir()
    build_root = project.root / "build"
    try:
        build_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    common = ["--config", str(project.config_path), "build", "--project-root", str(project.root)]
    assert main([*common, "--phase", "transform", "--run-id", "linked-build"]) == 2
    assert not any(outside.rglob("*"))


def test_replay_linked_parent_fails_without_escaped_writes(project) -> None:
    _build(project)
    replay_parent = project.root / "build" / "replays"
    outside = project.root.parent / "replays-parent-outside"
    outside.mkdir()
    try:
        replay_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    assert _run(project, "replay") == 2
    assert not any(outside.rglob("*"))
    assert replay_parent.is_symlink()


def test_replay_substituted_parent_under_lock_fails_without_cleanup(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    replay_parent = project.root / "build" / "replays"
    replay_parent.mkdir()
    outside = project.root.parent / "replays-substituted-outside"
    outside.mkdir()
    from re_agent.build.run_lock import RunLock as RunLockType

    original_acquire = RunLockType.acquire

    def acquire_then_substitute_parent(lock):
        result = original_acquire(lock)
        shutil.rmtree(replay_parent)
        try:
            replay_parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            result.release()
            pytest.skip("directory symlinks unavailable")
        return result

    monkeypatch.setattr(RunLockType, "acquire", acquire_then_substitute_parent)
    assert _run(project, "replay") == 2
    assert not any(outside.rglob("*"))


def test_replay_linked_root_fails_without_escaped_writes_or_cleanup(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    replay_parent = project.root / "build" / "replays"
    replay_parent.mkdir()
    outside = project.root.parent / "replay-root-outside"
    outside.mkdir()
    replay_root = replay_parent / "run-1-fixed"
    try:
        replay_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setattr("re_agent.cli.cmd_run.uuid.uuid4", lambda: SimpleNamespace(hex="fixed"))

    assert _run(project, "replay") == 2
    assert not any(outside.rglob("*"))
    assert replay_root.is_symlink()


def test_matching_transient_profile_verifies_and_replays(project) -> None:
    profile = _write_profile(project, "matching-profile")
    _build(project, profile=profile)
    assert _run(project, "verify", profile=profile) == 0
    assert _run(project, "replay", profile=profile) == 0


def test_wrong_transient_profile_rejects_existing_run(project, monkeypatch: pytest.MonkeyPatch) -> None:
    matching = _write_profile(project, "matching-profile")
    wrong = _write_profile(project, "wrong-profile", flags="-c -DWRONG")
    _build(project, profile=matching)

    def resolve_with_wrong_profile(*, project_root, capability, profile_path=None):
        if profile_path == wrong:
            return (VerifiedCommand((str(project.compiler_path), "-c"), "b" * 64),)
        return (VerifiedCommand((str(project.compiler_path), "-c"), _digest(project.compiler_path.read_bytes())),)

    monkeypatch.setattr("re_agent.toolchain.activation.resolve_capability", resolve_with_wrong_profile)
    assert _run(project, "verify", profile=wrong) == 2
    assert _run(project, "replay", profile=wrong) == 2


def test_active_and_transient_profile_selection_is_rejected_as_ambiguous(project) -> None:
    profile = _write_profile(project, "matching-profile")
    _build(project, profile=profile)
    activate_profile(project_root=project.root, profile_path=profile)
    assert _run(project, "verify", profile=profile) == 2
    assert _run(project, "replay", profile=profile) == 2


def test_replay_never_creates_a_live_provider(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    monkeypatch.setattr(
        "re_agent.llm.registry.create_provider",
        lambda _config: (_ for _ in ()).throw(AssertionError("live provider creation was called")),
    )
    assert _run(project, "replay") == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "malformed",
        "stale",
        "input",
        "target",
        "messages",
        "config",
        "response",
        "compiler",
        "generated",
        "object",
    ],
)
def test_replay_mutations_reject_before_provider_or_compiler(project, monkeypatch, mutation: str) -> None:
    _build(project)
    run_root = project.root / "build" / "runs" / "run-1"
    checkpoint_path = run_root / "checkpoints.json"
    evidence_path = run_root / "staging" / "transform-evidence" / "0x1000.json"
    if mutation == "missing":
        evidence_path.unlink()
    elif mutation == "malformed":
        evidence_path.write_bytes(b"not-json")
    elif mutation == "stale":
        checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoints[0]["transform_evidence_sha256"] = "b" * 64
        checkpoint_path.write_text(json.dumps(checkpoints), encoding="utf-8")
    elif mutation == "input":
        (project.root / "snapshot" / "0x1000__root.cpp").write_bytes(b"changed\n")
    elif mutation == "target":
        checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoints[0]["name"] = "changed"
        checkpoint_path.write_text(json.dumps(checkpoints), encoding="utf-8")
    else:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if mutation == "messages":
            evidence["messages"][1]["content"] += " changed"
        elif mutation == "config":
            evidence["llm_config"]["model"] = "changed"
        elif mutation == "response":
            evidence["raw_response"] += " changed"
        elif mutation == "compiler":
            evidence["compiler_argv"].append("-DCHANGED")
        elif mutation == "generated":
            evidence["generated_sha256"] = "b" * 64
        else:
            evidence["object_sha256"] = "b" * 64
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    provider_calls = project.provider.calls
    monkeypatch.setattr(
        "re_agent.llm.registry.create_provider",
        lambda _config: (_ for _ in ()).throw(AssertionError("provider called before validation")),
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("compiler called"))
    )
    assert _run(project, "replay") == 2
    assert project.provider.calls == provider_calls


@pytest.mark.parametrize(
    "identity",
    [
        "project_fingerprint",
        "snapshot_manifest_sha256",
        "manifest_sha256",
        "toolchain_sha256",
        "compiler_sha256",
        "config_sha256",
        "recipe_sha256",
        "llm_config_sha256",
        "prompt_sha256",
    ],
)
def test_replay_identity_mutation_rejects_before_provider(project, monkeypatch, identity: str) -> None:
    _build(project)
    run_json = project.root / "build" / "runs" / "run-1" / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))
    payload[identity] = "b" * 64
    run_json.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "re_agent.llm.registry.create_provider",
        lambda _config: (_ for _ in ()).throw(AssertionError("provider called before identity validation")),
    )
    assert _run(project, "replay") == 2


def test_replay_staging_failure_preserves_original_run(project, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(project)
    run_root = project.root / "build" / "runs" / "run-1"
    before = {
        path.relative_to(run_root): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file() and path.name != ".run.lock"
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="failed"),
    )
    assert _run(project, "replay") == 2
    after = {
        path.relative_to(run_root): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file() and path.name != ".run.lock"
    }
    assert after == before


def test_concurrent_run_lock_blocks_replay(project) -> None:
    _build(project)
    lock = RunLock(project.root / "build" / "runs" / "run-1", metadata={"run_id": "run-1"}).acquire()
    try:
        with pytest.raises(RunLockError):
            RunLock(project.root / "build" / "runs" / "run-1").acquire()
        assert _run(project, "replay") == 2
    finally:
        lock.release()


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "replay", "--run-id", "run-1"],
        ["run", "replay", "--project-root", "root"],
        ["run", "replay", "--project-root", "root", "--run-id", "run-1", "--phase", "x"],
    ],
)
def test_run_parser_rejects_missing_root_run_id_or_unsupported_option(argv) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_cmd_run_rejects_unsupported_operation() -> None:
    with pytest.raises(ValueError, match="unsupported run operation"):
        cmd_run(Namespace(run_command="unsupported", run_id="run-1", project_root="."))
