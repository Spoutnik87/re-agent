"""Strict, target-neutral toolchain profile parsing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from re_agent.project.snapshot import canonical_json, sha256_bytes


class ProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolchainProfile:
    backend: str
    target: str
    compiler: CommandSpec
    linker: CommandSpec | None = None
    runtime_harness: CommandSpec | None = None
    binary_inspector: CommandSpec | None = None
    abi_verifier: CommandSpec | None = None
    differential_matcher: CommandSpec | None = None
    extensions: dict[str, Any] = field(default_factory=dict)

    def canonical_bytes(self) -> bytes:
        return canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())


_OPTIONAL = {"linker", "runtime_harness", "binary_inspector", "abi_verifier", "differential_matcher"}
_ALLOWED = {"backend", "target", "compiler", "extensions", *_OPTIONAL}

# Commands that accept the optional ``flags`` key in YAML.
_FLAGS_ALLOWED: frozenset[str] = frozenset({"compiler", "linker"})


def _validate_and_build(raw: dict[str, Any]) -> ToolchainProfile:
    """Shared validation used by ``load_profile`` and ``load_profile_from_dict``."""
    unknown = set(raw) - _ALLOWED
    if unknown:
        raise ProfileError(f'unknown key "{sorted(unknown)[0]}" — valid keys are: {", ".join(sorted(_ALLOWED))}')
    for key in ("backend", "target", "compiler"):
        if key not in raw:
            raise ProfileError(f"profile.{key} is required")
    if not isinstance(raw["backend"], str) or not isinstance(raw["target"], str):
        raise ProfileError("profile.backend and profile.target must be strings")
    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ProfileError("profile.extensions must be an object")
    compiler_spec = _command(raw["compiler"], "compiler", flags=True)
    if not compiler_spec.flags:
        raise ProfileError("profile.compiler.flags is required and must be non-empty")
    kwargs: dict[str, Any] = {
        "backend": raw["backend"],
        "target": raw["target"],
        "compiler": compiler_spec,
        "extensions": extensions,
    }
    for key in _OPTIONAL:
        if key in raw and raw[key] is not None:
            kwargs[key] = _command(raw[key], key, flags=(key in _FLAGS_ALLOWED))
    return ToolchainProfile(**kwargs)


def _command(value: object, key: str, *, flags: bool = False) -> CommandSpec:
    if not isinstance(value, dict) or set(value) - {"command", "args", "flags"}:
        raise ProfileError(f"profile.{key} is invalid")
    command = value.get("command")
    args = value.get("args", [])
    command_flags = value.get("flags", [])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ProfileError(f"profile.{key}.command must be a non-empty string array")
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ProfileError(f"profile.{key}.args must be a string array")
    if not flags and "flags" in value:
        raise ProfileError(f"profile.{key}.flags is not allowed")
    if not isinstance(command_flags, list) or not all(isinstance(x, str) for x in command_flags):
        raise ProfileError(f"profile.{key}.flags must be a string array")
    return CommandSpec(tuple(command), tuple(args), tuple(command_flags))


def load_profile(path: Path) -> ToolchainProfile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot load profile: {path}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")
    return _validate_and_build(raw)


def load_profile_from_dict(raw: dict[str, Any]) -> ToolchainProfile:
    """Parse a validated dict into a ToolchainProfile without YAML deserialization.

    Used by ``resolve_capability`` to avoid writing temporary files when
    loading an already-published profile from its JSON representation.
    """
    return _validate_and_build(raw)


def profile_schema() -> dict[str, object]:
    command_properties: dict[str, object] = {
        "command": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "args": {"type": "array", "items": {"type": "string"}},
    }
    command = {
        "type": "object",
        "additionalProperties": False,
        "properties": command_properties,
        "required": ["command"],
    }
    command_with_flags = {
        **command,
        "properties": {**command_properties, "flags": {"type": "array", "items": {"type": "string"}}},
    }
    compiler = {
        **command_with_flags,
        "required": ["command", "flags"],
    }
    linker = command_with_flags
    optional_commands: dict[str, object] = {}
    for key in _OPTIONAL:
        if key == "linker":
            optional_commands[key] = linker
        else:
            optional_commands[key] = command
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "backend": {"type": "string"},
            "target": {"type": "string"},
            "compiler": compiler,
            **optional_commands,
            "extensions": {"type": "object", "additionalProperties": True},
        },
        "required": ["backend", "target", "compiler"],
    }
