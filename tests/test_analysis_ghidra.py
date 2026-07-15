"""Tests for GhidraLifecycleBackend."""

# ruff: noqa: SIM117

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from re_agent.analysis.ghidra import GhidraLifecycleBackend, GhidraLifecycleError

# ── helpers ───────────────────────────────────────────────────────────────


def _make_mock_run(*, returncode: int = 0, stdout: str = "", side_effect=None):
    """Return a mock for subprocess.run that simulates CLI output."""
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
        return mock
    mock.return_value = MagicMock(
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )
    return mock


def _valid_source_snapshot(path: Path) -> None:
    """Create a minimal valid analysis snapshot at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": "1.0.0",
        "backend": "ghidra",
        "binary_sha256": "b" * 64,
        "abi_manifest_path": "abi_manifest.json",
    }
    (path / "analysis-metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    abi = {
        "format_version": 1,
        "version": "1.0.0",
        "architecture": "x86",
        "pointer_size": 4,
        "symbols": [],
    }
    (path / "abi_manifest.json").write_text(json.dumps(abi, sort_keys=True) + "\n", encoding="utf-8")


# ── health_check ──────────────────────────────────────────────────────────


class TestHealthCheck:
    def test_ok_when_cli_responds(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with patch("subprocess.run", _make_mock_run(stdout="Ghidra 11.2\nCopyright ...\n")):
            health = backend.health_check()
        assert health.ok
        assert health.version == "Ghidra 11.2"

    def test_ok_uses_first_line(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with patch("subprocess.run", _make_mock_run(stdout="11.2.0\n")):
            health = backend.health_check()
        assert health.ok
        assert health.version == "11.2.0"

    def test_not_ok_when_cli_not_found(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="nonexistent-ghidra")
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            health = backend.health_check()
        assert not health.ok
        assert health.version == ""

    def test_not_ok_when_cli_fails(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with patch("subprocess.run", _make_mock_run(returncode=1, stdout="error")):
            health = backend.health_check()
        assert not health.ok
        assert health.version == ""

    def test_not_ok_on_os_error(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with patch("subprocess.run", side_effect=PermissionError("denied")):
            health = backend.health_check()
        assert not health.ok
        assert health.version == ""


# ── fingerprint ───────────────────────────────────────────────────────────


class TestFingerprint:
    def test_parses_sha256_from_output(self, tmp_path: Path) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        expected = "ab" + "cd" * 31  # 64 hex chars
        binary = tmp_path / "test.bin"
        binary.write_bytes(b"test")
        with patch("subprocess.run", _make_mock_run(stdout=f"{expected}\n")):
            fp = backend.fingerprint(binary_path=str(binary))
        assert fp.sha256 == expected

    def test_uses_self_fallback(self) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with patch("subprocess.run", _make_mock_run(stdout="c" * 64 + "\n")) as mock:
            fp = backend.fingerprint()
        assert fp.sha256 == "c" * 64
        # Verify --self was passed
        args = mock.call_args[0][0]
        assert "--self" in args

    def test_passes_binary_path(self, tmp_path: Path) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        binary = tmp_path / "test.bin"
        binary.write_bytes(b"test")
        with patch("subprocess.run", _make_mock_run(stdout="d" * 64 + "\n")) as mock:
            backend.fingerprint(binary_path=str(binary))
        args = mock.call_args[0][0]
        assert "fingerprint" in args
        assert str(binary) in args

    def test_raises_if_binary_not_found(self, tmp_path: Path) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        with pytest.raises(GhidraLifecycleError, match="binary not found"):
            backend.fingerprint(binary_path=str(tmp_path / "missing.bin"))

    def test_raises_on_non_hex_output(self, tmp_path: Path) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        binary = tmp_path / "test.bin"
        binary.write_bytes(b"test")
        with patch("subprocess.run", _make_mock_run(stdout="not-a-sha\n")):
            with pytest.raises(GhidraLifecycleError, match="invalid fingerprint"):
                backend.fingerprint(binary_path=str(binary))

    def test_raises_on_wrong_length_output(self, tmp_path: Path) -> None:
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")
        binary = tmp_path / "test.bin"
        binary.write_bytes(b"test")
        with patch("subprocess.run", _make_mock_run(stdout="abc123\n")):
            with pytest.raises(GhidraLifecycleError, match="invalid fingerprint"):
                backend.fingerprint(binary_path=str(binary))


# ── provision_workspace ───────────────────────────────────────────────────


class TestProvisionWorkspace:
    def test_creates_workspace(self, tmp_path: Path) -> None:
        binary = tmp_path / "test.bin"
        binary.write_text("binary content")
        workspace = tmp_path / "workspace"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        def _side_effect(argv, **_kw):
            workspace.mkdir(parents=True)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)) as mock:
            backend.provision_workspace(binary=binary, workspace=workspace)

        # Verify the CLI was called with correct args
        args = mock.call_args[0][0]
        assert args[0] == "fake-ghidra"
        assert args[1] == "provision"
        assert str(binary.resolve()) in args
        assert str(workspace.resolve()) in args

    def test_rejects_existing_workspace(self, tmp_path: Path) -> None:
        binary = tmp_path / "test.bin"
        binary.write_text("content")
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with pytest.raises(GhidraLifecycleError, match="workspace already exists"):
            backend.provision_workspace(binary=binary, workspace=workspace)

    def test_rejects_missing_binary(self, tmp_path: Path) -> None:
        binary = tmp_path / "missing.bin"
        workspace = tmp_path / "workspace"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with pytest.raises(GhidraLifecycleError, match="binary not found"):
            backend.provision_workspace(binary=binary, workspace=workspace)

    def test_raises_if_cli_fails(self, tmp_path: Path) -> None:
        binary = tmp_path / "test.bin"
        binary.write_text("content")
        workspace = tmp_path / "workspace"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with patch("subprocess.run", _make_mock_run(returncode=1, stdout="error: invalid")):
            with pytest.raises(GhidraLifecycleError, match="exited with code 1"):
                backend.provision_workspace(binary=binary, workspace=workspace)


# ── analyze_export ────────────────────────────────────────────────────────


class TestAnalyzeExport:
    def test_runs_and_validates(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        def _side_effect(argv, **_kw):
            # Simulate CLI creating a valid snapshot
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="exported\n")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)):
            result = backend.analyze_export(workspace=workspace, output=output)

        assert result == output.resolve()

    def test_rejects_existing_output(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        output = tmp_path / "output"
        output.mkdir(parents=True)
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with pytest.raises(GhidraLifecycleError, match="output already exists"):
            backend.analyze_export(workspace=workspace, output=output)

    def test_rejects_missing_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "missing-ws"
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with pytest.raises(GhidraLifecycleError, match="workspace does not exist"):
            backend.analyze_export(workspace=workspace, output=output)

    def test_raises_if_cli_does_not_create_output(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with patch("subprocess.run", _make_mock_run(stdout="")):
            with pytest.raises(GhidraLifecycleError, match="did not create output"):
                backend.analyze_export(workspace=workspace, output=output)

    def test_validates_exported_snapshot(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        def _side_effect(argv, **_kw):
            # Create an INVALID output (missing required files)
            output.mkdir(parents=True)
            (output / "analysis-metadata.json").write_text('{"bad": true}', encoding="utf-8")
            return MagicMock(returncode=0, stdout="exported\n")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)):
            with pytest.raises(GhidraLifecycleError, match="snapshot validation failed"):
                backend.analyze_export(workspace=workspace, output=output)

    def test_passes_correct_argv(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="/usr/bin/ghidra")

        def _side_effect(argv, **_kw):
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)) as mock:
            backend.analyze_export(workspace=workspace, output=output)
        args = mock.call_args[0][0]
        assert args == ["/usr/bin/ghidra", "analyze-export", str(workspace.resolve()), str(output.resolve())]

    def test_uses_shell_false_and_timeout(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra", timeout_s=300)

        def _side_effect(argv, **_kw):
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)) as mock:
            backend.analyze_export(workspace=workspace, output=output)
        kwargs = mock.call_args[1]
        assert kwargs.get("shell") is False
        assert kwargs.get("timeout") == 300

    def test_raises_on_cli_failure(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="fake-ghidra")

        with patch("subprocess.run", _make_mock_run(returncode=2, stdout="analysis failed")):
            with pytest.raises(GhidraLifecycleError, match="exited with code 2"):
                backend.analyze_export(workspace=workspace, output=output)


# ── run_direct_export ───────────────────────────────────────────────────


class TestRunDirectExport:
    """Tests for GhidraLifecycleBackend.run_direct_export — exact-user-argv path."""

    def test_runs_user_argv_verbatim(self, tmp_path: Path) -> None:
        """The argv passed to subprocess.run is exactly what the caller gives,
        with no prepended subcommand invented by the backend."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy", timeout_s=120)
        user_argv = ["/custom/path/ghidra", "my-custom-export", tmp_path.as_posix(), output.name]

        def _side_effect(argv, **_kw):
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="done")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)) as mock:
            backend.run_direct_export(argv=user_argv, output=output)

        captured_argv = mock.call_args[0][0]
        assert captured_argv == user_argv, f"Expected {user_argv}, got {captured_argv}"

    def test_shell_false_and_timeout(self, tmp_path: Path) -> None:
        """Confirm shell=False and timeout are enforced."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy", timeout_s=99)

        def _side_effect(argv, **_kw):
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)) as mock:
            backend.run_direct_export(argv=["ghidra", "x"], output=output)

        kwargs = mock.call_args[1]
        assert kwargs.get("shell") is False
        assert kwargs.get("timeout") == 99

    def test_rejects_existing_output(self, tmp_path: Path) -> None:
        """Existing output directory is rejected before any execution."""
        output = tmp_path / "output"
        output.mkdir(parents=True)
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")
        with pytest.raises(GhidraLifecycleError, match="output already exists"):
            backend.run_direct_export(argv=["ghidra"], output=output)

    def test_rejects_empty_argv(self, tmp_path: Path) -> None:
        """Empty argv list is rejected."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")
        with pytest.raises(GhidraLifecycleError, match="argv must not be empty"):
            backend.run_direct_export(argv=[], output=output)

    def test_validates_output_snapshot(self, tmp_path: Path) -> None:
        """After command succeeds, the output is validated via inventory_snapshot."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")

        def _side_effect(argv, **_kw):
            # Create invalid snapshot (no metadata)
            output.mkdir(parents=True)
            (output / "some_file.json").write_text("{}", encoding="utf-8")
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)):
            with pytest.raises(GhidraLifecycleError, match="snapshot validation failed"):
                backend.run_direct_export(argv=["ghidra", "x"], output=output)

    def test_raises_if_cli_does_not_create_output(self, tmp_path: Path) -> None:
        """When the command succeeds but doesn't create the output dir, error."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")

        with patch("subprocess.run", _make_mock_run(stdout="")):
            with pytest.raises(GhidraLifecycleError, match="did not create output"):
                backend.run_direct_export(argv=["ghidra", "x"], output=output)

    def test_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        """Non-zero exit code produces a descriptive error."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")
        with patch("subprocess.run", _make_mock_run(returncode=3, stdout="boom")):
            with pytest.raises(GhidraLifecycleError, match="exited with code 3"):
                backend.run_direct_export(argv=["ghidra", "x"], output=output)

    def test_returns_resolved_output_path(self, tmp_path: Path) -> None:
        """On success, returns the resolved output Path."""
        output = tmp_path / "output"
        backend = GhidraLifecycleBackend(ghidra_cli="dummy")

        def _side_effect(argv, **_kw):
            _valid_source_snapshot(output)
            return MagicMock(returncode=0, stdout="")

        with patch("subprocess.run", _make_mock_run(side_effect=_side_effect)):
            result = backend.run_direct_export(argv=["ghidra", "x"], output=output)

        assert result == output.resolve()
