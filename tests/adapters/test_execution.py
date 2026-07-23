from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from re_agent.adapters import AdapterCommand, AdapterRequest, execute_adapter, execute_adapter_with_evidence
from re_agent.toolchain.activation import ProfileError, VerifiedCommand


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request(command: AdapterCommand) -> AdapterRequest:
    return AdapterRequest(
        capability="generic-proof",
        proof_type="fixture",
        command=command,
        project_identity="project-1",
        snapshot_identity="snapshot-1",
        manifest_identity="manifest-1",
        build_target_identity="target-1",
    )


def _command(code: str) -> VerifiedCommand:
    candidates = [Path(sys.prefix) / "python.exe", Path(sys.base_prefix) / "python.exe", Path(sys.executable)]
    resolved = shutil.which("python")
    if resolved:
        candidates.append(Path(resolved))
    executable = next(candidate.resolve() for candidate in candidates if candidate.is_file() and _readable(candidate))
    return VerifiedCommand((str(executable), "-c", code), _sha256(executable))


def _readable(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def _adapter_code(*, result: str = "pass", output: str = "") -> str:
    return (
        "import json,sys; from pathlib import Path; "
        "request=Path(sys.argv[sys.argv.index('--request')+1]); "
        "result=Path(sys.argv[sys.argv.index('--result')+1]); "
        f"{output}"
        "result.write_text(json.dumps({"
        "'protocol':'re-agent.adapter.result.v1',"
        "'request_sha256':json.loads(request.read_text())['request_sha256'],"
        f"'outcome':{result!r},'attachments':[], 'details':{{}}}}))"
    )


def test_execute_adapter_uses_argv_only_and_validates_attachment(tmp_path: Path) -> None:
    payload = b"portable attachment\n"
    attachment = tmp_path / "result.bin"
    attachment.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    code = (
        "import json,sys; from pathlib import Path; "
        "request=Path(sys.argv[sys.argv.index('--request')+1]); "
        "result=Path(sys.argv[sys.argv.index('--result')+1]); "
        "Path('result.bin').write_bytes(b'portable attachment\\n'); "
        "result.write_text(json.dumps({'protocol':'re-agent.adapter.result.v1',"
        "'request_sha256':json.loads(request.read_text())['request_sha256'],"
        "'outcome':'pass','attachments':[{'path':'result.bin','sha256':'"
        + digest
        + "','size_bytes':20}], 'details':{}})); print('adapter-stdout'); print('adapter-stderr',file=sys.stderr)"
    )
    command = _command(code)
    request = _request(AdapterCommand(command.argv, command.executable_sha256))

    result, stdout, stderr = execute_adapter(command, request, tmp_path)

    assert result.outcome == "pass"
    assert stdout.replace("\r\n", "\n") == "adapter-stdout\n"
    assert stderr.replace("\r\n", "\n") == "adapter-stderr\n"
    assert not list(tmp_path.glob(".adapter-*.json"))


def test_isolated_staging_preserves_canonical_evidence_and_keeps_project_root_untouched(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    execution_root = tmp_path / "execution"
    staging = tmp_path / "caller-staging"
    project_root.mkdir()
    execution_root.mkdir()
    staging.mkdir()
    (staging / "caller-owned.txt").write_text("keep", encoding="utf-8")
    original = execution_root / "original.bin"
    original.write_bytes(b"original-binary")
    attachment = b"validated attachment\n"
    attachment_hash = hashlib.sha256(attachment).hexdigest()
    code = (
        "import json,sys; from pathlib import Path; "
        "request=Path(sys.argv[sys.argv.index('--request')+1]); "
        "result=Path(sys.argv[sys.argv.index('--result')+1]); "
        "Path('cwd-marker.txt').write_text(str(Path.cwd()), encoding='utf-8'); "
        "Path('attachment.bin').write_bytes(b'validated attachment\\n'); "
        "payload=json.loads(request.read_text()); "
        "result.write_text(json.dumps({'protocol':'re-agent.adapter.result.v1',"
        "'request_sha256':payload['request_sha256'],'outcome':'pass',"
        "'attachments':[{'path':'attachment.bin','sha256':'"
        + attachment_hash
        + "','size_bytes':21}], 'details':{'proof':'isolated'}})); "
        "print('canonical-stdout'); print('canonical-stderr', file=sys.stderr)"
    )
    command = _command(code)
    request = _request(AdapterCommand(command.argv, command.executable_sha256))
    request = request.__class__(
        request.capability,
        request.proof_type,
        request.command,
        request.project_identity,
        request.snapshot_identity,
        request.manifest_identity,
        request.build_target_identity,
        (("original_binary", "original.bin"),),
        request.hashes,
        request.payload,
    )

    execution = execute_adapter_with_evidence(command, request, execution_root, staging=staging)
    evidence = execution.evidence

    assert evidence.directory.parent == staging
    assert evidence.directory != staging
    assert evidence.request_path.read_bytes() == request.with_input_hashes(execution_root).to_json_bytes()
    assert (
        evidence.result_path.read_bytes()
        == (json.dumps(execution.result.as_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    assert evidence.stdout_path.read_bytes().replace(b"\r\n", b"\n") == b"canonical-stdout\n"
    assert evidence.stderr_path.read_bytes().replace(b"\r\n", b"\n") == b"canonical-stderr\n"
    assert len(evidence.attachments) == 1
    assert evidence.attachments[0].read_bytes() == attachment
    assert (execution_root / "cwd-marker.txt").read_text(encoding="utf-8") == str(execution_root)
    assert not (project_root / "cwd-marker.txt").exists()
    assert (staging / "caller-owned.txt").read_text(encoding="utf-8") == "keep"

    evidence.result_path.unlink()
    with pytest.raises(FileNotFoundError):
        evidence.result_path.read_bytes()


@pytest.mark.parametrize("mutated_input", ["original_binary", "manifest"])
def test_declared_input_mutation_after_launch_rejects_result_and_evidence(tmp_path: Path, mutated_input: str) -> None:
    execution_root = tmp_path / "execution"
    staging = tmp_path / "caller-staging"
    execution_root.mkdir()
    staging.mkdir()
    (execution_root / "original.bin").write_bytes(b"original-binary")
    (execution_root / "manifest.json").write_text("manifest-v1", encoding="utf-8")
    code = (
        "import json,sys; from pathlib import Path; "
        "request=Path(sys.argv[sys.argv.index('--request')+1]); "
        "result=Path(sys.argv[sys.argv.index('--result')+1]); "
        "payload=json.loads(request.read_text()); "
        f"Path(payload['paths'][{mutated_input!r}]).write_bytes(b'tampered-after-launch'); "
        "result.write_text(json.dumps({'protocol':'re-agent.adapter.result.v1',"
        "'request_sha256':payload['request_sha256'],'outcome':'pass',"
        "'attachments':[], 'details':{}}))"
    )
    command = _command(code)
    request = replace(
        _request(AdapterCommand(command.argv, command.executable_sha256)),
        paths=(("manifest", "manifest.json"), ("original_binary", "original.bin")),
    )

    with pytest.raises(ValueError, match="declared input"):
        execute_adapter_with_evidence(command, request, execution_root, staging=staging)

    assert (execution_root / ("original.bin" if mutated_input == "original_binary" else "manifest.json")).read_bytes()
    evidence_dirs = list(staging.glob(".adapter-evidence-*"))
    assert len(evidence_dirs) == 1
    assert not (evidence_dirs[0] / "attachments").exists()


@pytest.mark.parametrize("mode", ["missing", "empty"])
def test_missing_or_empty_result_is_rejected(tmp_path: Path, mode: str) -> None:
    code = "import sys; from pathlib import Path; result=Path(sys.argv[sys.argv.index('--result')+1]); " + (
        "result.write_text('')" if mode == "empty" else "pass"
    )
    command = _command(code)
    request = _request(AdapterCommand(command.argv, command.executable_sha256))

    with pytest.raises((FileNotFoundError, json.JSONDecodeError)):
        execute_adapter(command, request, tmp_path)
    assert not list(tmp_path.glob(".adapter-*.json"))


def test_timeout_kills_process_and_cleans_protocol_files(tmp_path: Path) -> None:
    code = "import time; time.sleep(30)"
    command = _command(code)
    request = _request(AdapterCommand(command.argv, command.executable_sha256))

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        execute_adapter(command, request, tmp_path, timeout_seconds=0.1)

    assert time.monotonic() - started < 10
    assert not list(tmp_path.glob(".adapter-*.json"))


def test_executable_and_command_identity_failures_are_rejected(tmp_path: Path) -> None:
    command = _command(_adapter_code())
    request = _request(AdapterCommand(command.argv, command.executable_sha256))

    with pytest.raises(ValueError, match="does not match"):
        execute_adapter(
            VerifiedCommand(command.argv[:-1] + ("different",), command.executable_sha256), request, tmp_path
        )
    wrong_request = _request(AdapterCommand(command.argv, "b" * 64))
    with pytest.raises(ProfileError):
        execute_adapter(VerifiedCommand(command.argv, "b" * 64), wrong_request, tmp_path)


def test_unknown_outcome_and_request_mismatch_are_rejected(tmp_path: Path) -> None:
    command = _command(_adapter_code(result="maybe"))
    request = _request(AdapterCommand(command.argv, command.executable_sha256))

    with pytest.raises(ValueError, match="outcome"):
        execute_adapter(command, request, tmp_path)

    mismatch_code = _adapter_code(result="pass").replace(
        "json.loads(request.read_text())['request_sha256']", "'b' * 64"
    )
    mismatch_command = _command(mismatch_code)
    mismatch_request = _request(AdapterCommand(mismatch_command.argv, mismatch_command.executable_sha256))
    with pytest.raises(ValueError, match="does not match"):
        execute_adapter(mismatch_command, mismatch_request, tmp_path)
