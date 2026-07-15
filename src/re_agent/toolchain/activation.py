"""Immutable toolchain activation and capability verification.

Content-addressed, tamper-evident activation:

*   **Unique staging**: Every publish writes to a UUID-named staging
    directory first, then atomically replaces the content-hash directory.
    If the hash directory already exists the publish is skipped (content
    is already immutable).
*   **Hash-chained pointer**: ``active.link`` records both
    ``profile_sha256`` and ``fingerprint_sha256``.  Every
    ``resolve_capability`` authenticates the chain
    ``pointer → profile.json → fingerprint.json → binaries``.
*   **Transient resolution** (``profile_path`` given) fingerprints only
    the commands required by the requested capability and writes **no**
    temporary files to disk.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from re_agent.project.snapshot import canonical_json, load_json, sha256_bytes, sha256_file
from re_agent.toolchain.profile import (
    CommandSpec,
    ProfileError,
    ToolchainProfile,
    load_profile,
    load_profile_from_dict,
)

_log = logging.getLogger(__name__)


# ── capability → required command names ───────────────────────────────────
_CAPABILITY_COMMANDS: dict[str, tuple[str, ...]] = {
    "compile": ("compiler",),
    "link": ("linker",),
    "inspect_abi": ("binary_inspector", "abi_verifier"),
    "run_differential": ("runtime_harness", "differential_matcher"),
}

_ALL_COMMAND_NAMES = (
    "compiler",
    "linker",
    "runtime_harness",
    "binary_inspector",
    "abi_verifier",
    "differential_matcher",
)


# ── public types ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerifiedCommand:
    argv: tuple[str, ...]
    executable_sha256: str


# ── verify_command ────────────────────────────────────────────────────────


def verify_command(command: VerifiedCommand) -> None:
    """Re-check that the executable at *argv[0]* still matches its recorded hash.

    Raises:
        ProfileError: If the binary has changed, is missing, or is not a
            regular executable file.
    """
    executable = Path(command.argv[0])
    if not executable.is_file():
        raise ProfileError(f"toolchain binary not found: {executable}")
    actual = sha256_file(executable)
    if actual != command.executable_sha256:
        raise ProfileError(
            f"toolchain binary {executable} has changed (expected {command.executable_sha256}, got {actual})"
        )


# ── internal helpers ──────────────────────────────────────────────────────


def _resolve_command(spec: CommandSpec, profile_dir: Path) -> Path:
    """Resolve a ``CommandSpec.command[0]`` to an absolute, executable path."""
    raw = Path(spec.command[0])
    if raw.is_absolute():
        candidate = raw
    elif raw.parent != Path("."):
        candidate = profile_dir / raw
    else:
        resolved = shutil.which(raw.name)
        candidate = Path(resolved) if resolved else Path("")
    if not candidate or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ProfileError(f"toolchain command is unavailable: {spec.command[0]}")
    return candidate.resolve()


def _fingerprint(
    profile: ToolchainProfile,
    profile_dir: Path,
    *,
    optional: bool,
    required_only: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Resolve and checksum the requested (or all) toolchain commands.

    When *required_only* is set, only those named commands are resolved
    (used by transient resolution in ``resolve_capability``).

    Warnings are logged for unavailable optional commands that the profile
    defines but cannot be resolved on this host.
    """
    names = required_only if required_only is not None else _ALL_COMMAND_NAMES
    commands: dict[str, dict[str, object]] = {}
    for name in names:
        spec = getattr(profile, name, None)
        if spec is None:
            continue
        try:
            executable = _resolve_command(spec, profile_dir)
        except ProfileError:
            if name == "compiler" or not optional:
                raise
            _log.warning("toolchain command %r is unavailable — skipping fingerprint", name)
            commands[name] = {"missing": True}
            continue
        commands[name] = {
            "argv": [str(executable), *spec.command[1:], *spec.args, *spec.flags],
            "sha256": sha256_file(executable),
        }
    return {"profile_sha256": profile.sha256, "commands": commands}


# ── atomic publish ────────────────────────────────────────────────────────


def _publish_immutable(
    directory: Path,
    profile: ToolchainProfile,
    fingerprint: dict[str, object],
    project_root: Path,
) -> None:
    """Publish profile+fingerprint under *directory* with no-clobber.

    ``os.rename`` on a non-empty directory **never** replaces an existing
    target, making it the atomic no-clobber gate for concurrent publishers:
    exactly one wins, the rest verify the winner's content and return.

    Once published the directory is immutable: any subsequent tamper is
    detected but never silently corrected.
    """
    # Fast path — skip staging if already published.
    if directory.is_dir():
        _verify_live(directory, profile, fingerprint)
        return

    stage = project_root / "toolchain" / f".staging_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        (stage / "profile.json").write_bytes(profile.canonical_bytes())
        (stage / "fingerprint.json").write_bytes(canonical_json(fingerprint))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    # Atomic rename — the real no-clobber gate.  os.rename on a non-empty
    # directory never replaces an existing target (FileExistsError /
    # ENOTEMPTY), so concurrent publishers are safe.
    try:
        os.rename(str(stage), str(directory))
    except OSError:
        if directory.is_dir():
            # Another process published first — verify their content.
            shutil.rmtree(stage, ignore_errors=True)
            _verify_live(directory, profile, fingerprint)
            return
        # Real OS error (e.g. filesystem full, permission denied).
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _verify_live(
    directory: Path,
    profile: ToolchainProfile,
    fingerprint: dict[str, object],
) -> None:
    """Verify that an existing hash directory matches *profile* and *fingerprint*.

    Raises:
        ProfileError: On any mismatch — the directory is tampered and cannot
            be used or overwritten.
    """
    try:
        existing_profile = (directory / "profile.json").read_bytes()
        existing_fp = (directory / "fingerprint.json").read_bytes()
    except OSError as exc:
        raise ProfileError(
            f"toolchain profile directory {directory.name} is corrupt (cannot read published files: {exc})"
        ) from exc
    if sha256_bytes(existing_profile) != profile.sha256:
        raise ProfileError(f"toolchain profile directory {directory.name} is tampered (profile.json hash mismatch)")
    expected_fp_hash = sha256_bytes(canonical_json(fingerprint))
    if sha256_bytes(existing_fp) != expected_fp_hash:
        raise ProfileError(f"toolchain profile directory {directory.name} is tampered (fingerprint.json hash mismatch)")
    # Both match — content is intact.  This is a no-op publish skip.


# ── activation ────────────────────────────────────────────────────────────


def activate_profile(*, project_root: Path, profile_path: Path) -> dict[str, object]:
    """Publish a toolchain profile under its content hash and point ``active.link`` at it.

    Publishing is atomic:

    1.  The profile YAML is parsed and validated.
    2.  All optional toolchain commands are resolved and fingerprinted.
    3.  If the content-hash directory already exists, publishing is skipped
        (the content is already immutable on disk).
    4.  Otherwise, profile + fingerprint are written to a UUID-named staging
        directory and atomically renamed to the final hash directory.
    5.  ``active.link`` is written via a temporary swap file.

    Returns:
        A pointer dict ``{source, activated_at, profile_sha256,
        fingerprint_sha256}`` — the same dict that is stored in
        ``active.link``.
    """
    profile = load_profile(profile_path)
    fingerprint = _fingerprint(profile, profile_path.parent, optional=True)
    directory = project_root / "toolchain" / profile.sha256

    # Content-addressed, immutable: verify integrity if already published,
    # otherwise publish via atomic staging.  Never overwrite — even if
    # tampered, the user must manually remove the directory.
    _publish_immutable(directory, profile, fingerprint, project_root)

    # Build pointer from on-disk fingerprint to get the authoritative hash.
    fp_bytes = (directory / "fingerprint.json").read_bytes()
    fingerprint_sha256 = sha256_bytes(fp_bytes)

    pointer: dict[str, object] = {
        "source": str(profile_path.resolve()),
        "activated_at": datetime.now(UTC).isoformat(),
        "profile_sha256": profile.sha256,
        "fingerprint_sha256": fingerprint_sha256,
    }

    active = project_root / "toolchain" / "active.link"
    # Write to a UUID-named temp so concurrent processes never collide.
    temp = active.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_bytes(canonical_json(pointer))
    temp.replace(active)
    return pointer


# ── capability resolution ─────────────────────────────────────────────────


def _authenticate_chain(
    project_root: Path,
    pointer: dict[str, object],
) -> tuple[ToolchainProfile, dict[str, object]]:
    """Authenticate the ``active.link → profile.json → fingerprint.json`` chain.

    Hash values in *pointer* are validated syntactically (64-char hex) **before**
    any filesystem path is constructed from them, preventing path-injection or
    misdirection attacks.

    Raises:
        ProfileError: On any hash mismatch or missing artifact.
    """
    profile_hash = pointer.get("profile_sha256")
    if not isinstance(profile_hash, str) or len(profile_hash) != 64:
        raise ProfileError('no toolchain fingerprint — run "re-agent toolchain activate" first')
    # Validate hex BEFORE using as a directory name.
    try:
        int(profile_hash, 16)
    except ValueError as exc:
        raise ProfileError(f"invalid profile_sha256 — not a hex string: {profile_hash[:16]}…") from exc

    expected_fp_sha = pointer.get("fingerprint_sha256")
    if not isinstance(expected_fp_sha, str) or len(expected_fp_sha) != 64:
        raise ProfileError("invalid fingerprint_sha256 in active.link")
    try:
        int(expected_fp_sha, 16)
    except ValueError as exc:
        raise ProfileError(f"invalid fingerprint_sha256 — not a hex string: {expected_fp_sha[:16]}…") from exc

    directory = project_root / "toolchain" / profile_hash
    if not directory.is_dir():
        raise ProfileError(f"toolchain profile directory not found: {profile_hash}")

    # 1. profile.json → verify pointer.profile_sha256
    profile_path = directory / "profile.json"
    if not profile_path.is_file():
        raise ProfileError(f"profile.json not found for hash {profile_hash}")
    profile_bytes = profile_path.read_bytes()
    if sha256_bytes(profile_bytes) != profile_hash:
        raise ProfileError(f"profile.json hash mismatch: {profile_hash}")

    # 2. fingerprint.json → verify pointer.fingerprint_sha256
    fp_path = directory / "fingerprint.json"
    if not fp_path.is_file():
        raise ProfileError(f"fingerprint.json not found for hash {profile_hash}")
    fp_bytes = fp_path.read_bytes()
    if sha256_bytes(fp_bytes) != expected_fp_sha:
        raise ProfileError("fingerprint.json hash mismatch")

    # 3. Cross-check: fingerprint references the right profile
    fingerprint = json.loads(fp_bytes)
    if not isinstance(fingerprint, dict):
        raise ProfileError("fingerprint.json is not an object")
    if fingerprint.get("profile_sha256") != profile_hash:
        raise ProfileError("fingerprint.json references wrong profile")

    profile = load_profile_from_dict(json.loads(profile_bytes))
    return profile, fingerprint


def resolve_capability(
    *, project_root: Path, capability: str, profile_path: Path | None = None
) -> tuple[VerifiedCommand, ...]:
    """Resolve a named capability to a tuple of verified commands.

    Two resolution modes:

    *   **Transient** (*profile_path* is given) — the profile is loaded
        and only the commands required by *capability* are fingerprinted.
        No files are written to disk.
    *   **Active** (*profile_path* is ``None``) — the published
        ``active.link`` is loaded and the full
        ``pointer → profile.json → fingerprint.json → binary``
        hash chain is authenticated before returning the requested
        capability's commands.

    Args:
        project_root: Root of the re-agent project (contains
            ``toolchain/``).
        capability: One of ``"compile"``, ``"link"``,
            ``"inspect_abi"``, ``"run_differential"``.
        profile_path: Optional path to a profile YAML for transient
            (one-shot) resolution.

    Returns:
        A tuple of ``VerifiedCommand`` instances for the capability.

    Raises:
        ProfileError: If the capability is unknown, required commands
            are unavailable, or hash authentication fails.
    """
    required = _CAPABILITY_COMMANDS.get(capability)
    if required is None:
        raise ProfileError(f"unknown capability: {capability}")

    if profile_path is not None:
        # ── Transient resolution: no temp files, only required commands ──
        profile = load_profile(profile_path)
        fingerprint = _fingerprint(profile, profile_path.parent, optional=False, required_only=required)
    else:
        # ── Active resolution: authenticate full hash chain ──
        pointer_path = project_root / "toolchain" / "active.link"
        if not pointer_path.is_file():
            raise ProfileError('no toolchain activation — run "re-agent toolchain activate" first')
        pointer = load_json(pointer_path)
        profile, fingerprint = _authenticate_chain(project_root, pointer)

    commands: object = fingerprint.get("commands")
    if not isinstance(commands, dict):
        raise ProfileError("corrupt toolchain fingerprint: missing commands")

    result: list[VerifiedCommand] = []
    for name in required:
        entry = commands.get(name)
        if (
            not isinstance(entry, dict)
            or entry.get("missing")
            or not isinstance(entry.get("argv"), (list, tuple))
            or not isinstance(entry.get("sha256"), str)
        ):
            raise ProfileError(f"toolchain capability unavailable: {capability} (command {name})")
        raw_argv = list(entry["argv"])
        executable = Path(raw_argv[0])
        if not executable.is_file():
            raise ProfileError(f"toolchain binary not found: {executable}")
        actual = sha256_file(executable)
        if actual != entry["sha256"]:
            raise ProfileError(f"toolchain binary {executable} has changed (expected {entry['sha256']}, got {actual})")
        result.append(VerifiedCommand(tuple(str(x) for x in raw_argv), entry["sha256"]))

    return tuple(result)


def resolve_version(
    *,
    project_root: Path,
    profile_path: Path | None = None,
    command_name: str = "compiler",
) -> dict[str, object]:
    """Return a deterministic version fingerprint for a toolchain command.

    The version is the SHA-256 of the command's executable binary file.
    No subprocess is ever launched — no guessed ``--version`` flag, no
    stdout parsing, no invented default.

    Two resolution modes (matching ``resolve_capability``):

    *   **Transient** (*profile_path* given): load the profile YAML, resolve
        the command binary, hash it.
    *   **Active** (*profile_path* ``None``): authenticate the full chain,
        then hash the command binary recorded in the published fingerprint.

    Args:
        project_root: Root of the re-agent project.
        profile_path: Optional path for transient (one-shot) resolution.
        command_name: Which command to fingerprint (default ``"compiler"``).

    Returns:
        A dict with keys ``command_name``, ``argv``, ``sha256``, ``profile_sha256``
        (empty string if unavailable).

    Raises:
        ProfileError: If the command is not defined in the active/transient
            profile, the binary cannot be found, or chain authentication fails.
    """
    if profile_path is not None:
        profile = load_profile(profile_path)
        spec: CommandSpec | None = getattr(profile, command_name, None)
        if spec is None:
            raise ProfileError(f"command {command_name!r} is not defined in profile")
        executable = _resolve_command(spec, profile_path.parent)
        digest = sha256_file(executable)
        return {
            "command_name": command_name,
            "argv": str(executable),
            "sha256": digest,
            "profile_sha256": profile.sha256,
        }
    # Active resolution
    pointer_path = project_root / "toolchain" / "active.link"
    if not pointer_path.is_file():
        raise ProfileError('no toolchain activation — run "re-agent toolchain activate" first')
    pointer = load_json(pointer_path)
    profile, fingerprint = _authenticate_chain(project_root, pointer)
    commands: object = fingerprint.get("commands")
    if not isinstance(commands, dict):
        raise ProfileError("corrupt toolchain fingerprint: missing commands")
    entry = commands.get(command_name)
    if not isinstance(entry, dict) or entry.get("missing") or not isinstance(entry.get("sha256"), str):
        raise ProfileError(f"command {command_name!r} is not available in active toolchain")
    return {
        "command_name": command_name,
        "argv": str(entry.get("argv", [None])[0]),
        "sha256": entry["sha256"],
        "profile_sha256": fingerprint.get("profile_sha256", ""),
    }
