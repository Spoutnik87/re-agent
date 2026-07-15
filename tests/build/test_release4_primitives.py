from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from re_agent.build.bulk import checkpoint_coverage, missing_targets, validate_bulk_evidence
from re_agent.build.evidence import (
    BuildEvidence,
    TargetCheckpoint,
    coverage,
    load_evidence,
    save_evidence,
    validate_evidence,
)
from re_agent.build.recipe import BuildRecipe, run_recipe
from re_agent.project.publish import (
    DestinationExistsError,
    PublicationFailureError,
    UnsupportedPublicationError,
    load_active_build,
    publish_build,
)

HEX = "a" * 64
TARGETS = ((0x401000, "alpha"), (0x402000, "beta"))


def _evidence() -> BuildEvidence:
    checkpoints = tuple(
        TargetCheckpoint(
            address=address,
            name=name,
            status="compiled",
            source_sha256=HEX,
            output_sha256=HEX,
            signature=f"int {name}()",
            calling_convention="cdecl",
            output_path=f"unit/{name}.cpp",
            input_sha256=HEX,
            generated_sha256=HEX,
            object_sha256=HEX,
            verdicts=("MANIFEST_BOUND", "COMPILE_PASS"),
        )
        for address, name in TARGETS
    )
    return BuildEvidence(
        project_fingerprint=HEX,
        manifest_sha256=HEX,
        recipe_sha256=HEX,
        targets=checkpoints,
        output_path="build/game.exe",
        output_sha256=HEX,
        toolchain_sha256=HEX,
        run_id="release4-success",
        source_coverage=TARGETS,
        object_coverage=TARGETS,
        compiler_sha256=HEX,
        artifact_sha256=HEX,
        inspection_output_sha256=HEX,
        exit_status=0,
    )


def test_canonical_evidence_success_and_bulk_coverage(tmp_path: Path) -> None:
    evidence = save_evidence(_evidence(), tmp_path / "evidence.json")

    assert load_evidence(tmp_path / "evidence.json").to_json() == evidence.to_json()
    assert (tmp_path / "evidence.json").read_bytes().endswith(b"\n")
    assert coverage(reversed(TARGETS)) == TARGETS
    assert checkpoint_coverage(reversed(evidence.targets)) == TARGETS
    assert missing_targets(TARGETS, [(TARGETS[0])]) == (TARGETS[1],)
    validate_bulk_evidence(evidence, TARGETS, project_fingerprint=HEX)


@pytest.mark.parametrize(
    "change",
    [
        lambda e: replace(e, targets=(e.targets[0],)),
        lambda e: replace(e, targets=(replace(e.targets[0], verdicts=("MANIFEST_BOUND",)), e.targets[1])),
        lambda e: replace(e, targets=(replace(e.targets[0], object_sha256="not-a-digest"), e.targets[1])),
    ],
    ids=["incomplete-coverage", "incomplete-verdict", "malformed-target-digest"],
)
def test_target_evidence_rejects_incomplete_or_malformed_records(change) -> None:
    candidate = change(_evidence()).with_hash()
    with pytest.raises(ValueError):
        validate_evidence(candidate, TARGETS)


def test_stale_evidence_is_rejected() -> None:
    evidence = _evidence().with_hash()
    with pytest.raises(ValueError, match="stale project fingerprint"):
        validate_evidence(evidence, TARGETS, project_fingerprint="b" * 64)


def test_malformed_evidence_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed build evidence JSON"):
        load_evidence(path)


@pytest.mark.parametrize("field", ["output", "staging_root", "cwd"])
def test_recipe_rejects_unsafe_paths(field: str) -> None:
    with pytest.raises(ValueError):
        values = {"output": "out.bin", "staging_root": ".", "cwd": "."}
        values[field] = "../escape"
        BuildRecipe(argv=(sys.executable, "-c", "pass"), **values)


def test_recipe_runs_without_shell_and_accepts_nested_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "stage"
    (staging / "work").mkdir(parents=True)
    observed: dict[str, object] = {}
    real_popen = subprocess.Popen

    def checked_popen(*args, **kwargs):
        observed["argv"] = args[0]
        observed.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked_popen)
    recipe = BuildRecipe(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('nested').mkdir(); Path('nested/out.bin').write_bytes(b'ok')",
        ),
        cwd="work",
        output="work/nested/out.bin",
    )
    result = run_recipe(recipe, staging)

    assert result.successful
    assert observed["argv"] == list(recipe.argv)
    assert observed["shell"] is False
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE
    assert result.output_sha256 == hashlib.sha256(b"ok").hexdigest()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("raise SystemExit(3)", "nonzero"),
        ("pass", "missing"),
        ("from pathlib import Path; Path('out.bin').touch()", "empty"),
    ],
)
def test_recipe_failures_are_not_successful(tmp_path: Path, code: str, expected: str) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    recipe = BuildRecipe(argv=(sys.executable, "-c", code), output="out.bin")
    result = run_recipe(recipe, staging)

    assert not result.successful
    assert result.error or result.returncode != 0


def _publish(source: Path, root: Path, publication_id: str):
    try:
        return publish_build(source, root, publication_id, auth_key="release4-key")
    except UnsupportedPublicationError as exc:
        pytest.skip(f"publisher reports unsupported primitive: {exc}")


def _staged_build(root: Path, name: str, *, evidence: bytes = b"evidence") -> Path:
    source = root / name
    source.mkdir()
    (source / "artifact").write_bytes(b"artifact-" + name.encode())
    (source / "evidence").write_bytes(evidence)
    return source


def test_publication_is_immutable_and_active_pointer_verifies(tmp_path: Path) -> None:
    root = tmp_path / "published"
    source = _staged_build(tmp_path, "staged")
    publication = _publish(source, root, "build-1")

    active = load_active_build(root, auth_key="release4-key")
    assert active == publication
    assert publication.directory == root / "builds" / "build-1"
    with pytest.raises(DestinationExistsError):
        publish_build(_staged_build(tmp_path, "duplicate"), root, "build-1", auth_key="release4-key")


def test_failed_or_invalid_new_publication_leaves_prior_pointer_valid(tmp_path: Path) -> None:
    root = tmp_path / "published"
    _publish(_staged_build(tmp_path, "first"), root, "build-1")
    before = load_active_build(root, auth_key="release4-key")

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "artifact").write_bytes(b"artifact")
    with pytest.raises(PublicationFailureError):
        publish_build(invalid, root, "build-invalid", auth_key="release4-key")

    assert load_active_build(root, auth_key="release4-key") == before
