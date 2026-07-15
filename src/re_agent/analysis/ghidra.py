"""Ghidra CLI lifecycle adapter — provisions and analyses via subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path

from re_agent.analysis.lifecycle import BackendFingerprint, BackendHealth
from re_agent.project.snapshot import (
    SnapshotError,
    inventory_snapshot,
)


class GhidraLifecycleError(ValueError):
    """Raised when a Ghidra lifecycle operation fails."""


class GhidraLifecycleBackend:
    """Backend that shells out to a Ghidra CLI for lifecycle operations.

    Uses explicit ``argv`` lists with ``shell=False`` and a configurable
    timeout.  Pre-existing output directories are rejected to prevent
    accidental overwrites.  Every exported snapshot is validated through
    :func:`inventory_snapshot`.

    Args:
        ghidra_cli: Path or command name of the Ghidra CLI tool.
        timeout_s: Maximum wall-clock seconds per CLI invocation.
    """

    def __init__(self, ghidra_cli: str = "ghidra", timeout_s: int = 120) -> None:
        self._cli = ghidra_cli
        self._timeout = timeout_s

    # ── private helpers ─────────────────────────────────────────────────

    def _run(self, *args: str) -> str:
        """Execute the Ghidra CLI with explicit argv, no shell, and a timeout.

        Returns:
            Combined stdout+stderr as a string.

        Raises:
            GhidraLifecycleError: If the CLI is not found or returns
                a non-zero exit code.
        """
        argv = [self._cli, *args]
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self._timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            raise GhidraLifecycleError(f"Ghidra CLI not found: {self._cli}") from None
        except OSError as exc:
            raise GhidraLifecycleError(f"Ghidra CLI invocation failed: {exc}") from exc

        if proc.returncode != 0:
            msg = f"Ghidra CLI exited with code {proc.returncode}"
            if proc.stdout:
                msg += f": {proc.stdout.strip()}"
            raise GhidraLifecycleError(msg)

        return proc.stdout or ""

    def _validate_snapshot(self, path: Path) -> None:
        """Run inventory_snapshot and raise GhidraLifecycleError on failure.

        This is called after every export to ensure the output directory
        is a valid, trusted snapshot.
        """
        try:
            inventory_snapshot(path)
        except SnapshotError as exc:
            raise GhidraLifecycleError(f"exported snapshot validation failed: {exc}") from exc

    # ── public protocol ─────────────────────────────────────────────────

    def health_check(self) -> BackendHealth:
        """Check whether the Ghidra CLI is reachable and responsive.

        Executes ``<ghidra_cli> --version`` and returns ``ok=True`` with
        the version string on success.
        """
        try:
            output = self._run("--version")
        except GhidraLifecycleError:
            return BackendHealth(ok=False, version="")
        version = output.strip().splitlines()[0].strip() if output.strip() else "unknown"
        return BackendHealth(ok=True, version=version)

    def fingerprint(self, *, binary_path: str | None = None) -> BackendFingerprint:
        """Return the SHA-256 of a binary via the Ghidra CLI.

        Args:
            binary_path: Path to the target binary.  If ``None``, the CLI's
                own executable is fingerprinted as a fallback.

        Raises:
            GhidraLifecycleError: If the CLI invocation fails.
        """
        if binary_path is not None:
            target = Path(binary_path)
            if not target.is_file():
                raise GhidraLifecycleError(f"binary not found: {binary_path}")
            argv = ["fingerprint", str(target.resolve())]
        else:
            argv = ["fingerprint", "--self"]
        output = self._run(*argv)
        # Expect a 64-char hex SHA-256 on the first line.
        digest = output.strip().splitlines()[0].strip() if output.strip() else ""
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise GhidraLifecycleError(f"invalid fingerprint output: {output.strip()!r}")
        return BackendFingerprint(sha256=digest)

    def provision_workspace(self, binary: Path, workspace: Path) -> None:
        """Provision a Ghidra project workspace for *binary*.

        The CLI sub-command is ``provision <binary> <workspace>``.

        Args:
            binary: Path to the target binary.
            workspace: Destination directory.  Must not exist.

        Raises:
            GhidraLifecycleError: If the CLI fails or *workspace* exists.
        """
        if workspace.exists():
            raise GhidraLifecycleError(f"workspace already exists: {workspace}")
        resolved_binary = binary.resolve()
        if not resolved_binary.is_file():
            raise GhidraLifecycleError(f"binary not found: {binary}")

        self._run("provision", str(resolved_binary), str(workspace.resolve()))

        if not workspace.is_dir():
            raise GhidraLifecycleError("Ghidra CLI did not create workspace")

    def analyze_export(self, workspace: Path, output: Path) -> Path:
        """Run Ghidra analysis and export the snapshot.

        The CLI sub-command is ``analyze-export <workspace> <output>``.

        Args:
            workspace: An existing provisioned Ghidra project directory.
            output: Destination for the exported snapshot.  Must not exist.

        Returns:
            The resolved *output* path after successful validation.

        Raises:
            GhidraLifecycleError: If *output* exists, the CLI fails, or
                the exported snapshot fails inventory validation.
        """
        if output.exists():
            raise GhidraLifecycleError(f"output already exists: {output}")
        resolved_ws = workspace.resolve()
        resolved_out = output.resolve()

        if not resolved_ws.is_dir():
            raise GhidraLifecycleError(f"workspace does not exist: {workspace}")

        self._run("analyze-export", str(resolved_ws), str(resolved_out))

        if not resolved_out.is_dir():
            raise GhidraLifecycleError("Ghidra CLI did not create output snapshot")

        self._validate_snapshot(resolved_out)
        return resolved_out

    def run_direct_export(self, argv: list[str], output: Path) -> Path:
        """Execute a user-provided Ghidra export command verbatim.

        Runs *argv* with ``shell=False`` and the configured timeout without
        inventing any subcommands or defaults — exactly what the caller
        passes is what gets executed.  After the command completes the
        output directory is validated via :func:`inventory_snapshot`.

        Args:
            argv: The exact argument list to execute (e.g.
                ``["/usr/bin/ghidra", "analyze-export", "/ws", "/out"]``).
            output: Path to the snapshot directory the command is expected
                to populate.  Must not exist before the run.

        Returns:
            The resolved *output* path after successful validation.

        Raises:
            GhidraLifecycleError: If *output* already exists, the command
                fails, or the resulting snapshot is invalid.
        """
        if output.exists():
            raise GhidraLifecycleError(f"output already exists: {output}")
        resolved_out = output.resolve()

        if not argv:
            raise GhidraLifecycleError("argv must not be empty")

        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self._timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            raise GhidraLifecycleError(f"command not found: {argv[0]}") from None
        except OSError as exc:
            raise GhidraLifecycleError(f"command invocation failed: {exc}") from exc

        if proc.returncode != 0:
            msg = f"export command exited with code {proc.returncode}"
            if proc.stdout:
                msg += f": {proc.stdout.strip()}"
            raise GhidraLifecycleError(msg)

        if not resolved_out.is_dir():
            raise GhidraLifecycleError("export command did not create output snapshot")

        self._validate_snapshot(resolved_out)
        return resolved_out
