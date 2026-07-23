"""Release 5 canonical contracts for command adapters.

The objects in this module are deliberately boring: they are immutable,
strictly validated, and have one canonical JSON representation.  An adapter
therefore receives a portable description of work rather than Python
implementation details.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REQUEST_PROTOCOL = "re-agent.adapter.request.v1"
RESULT_PROTOCOL = "re-agent.adapter.result.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string without NUL")
    return value


def _hash(value: str, name: str) -> str:
    value = _text(value, name)
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _path(value: str, name: str) -> str:
    value = _text(value, name)
    parsed = PurePosixPath(value)
    if "\\" in value or parsed.is_absolute() or ":" in value or ".." in parsed.parts:
        raise ValueError(f"{name} must be a safe relative POSIX path")
    return value


def _canonical(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class AdapterCommand:
    """An authenticated argv and executable identity."""

    argv: tuple[str, ...]
    executable_sha256: str

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(x, str) or not x or "\x00" in x for x in self.argv):
            raise ValueError("argv must be a non-empty tuple of strings")
        _hash(self.executable_sha256, "executable_sha256")

    def as_dict(self) -> dict[str, object]:
        return {"argv": list(self.argv), "executable_sha256": self.executable_sha256}


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """Canonical, hash-bound input to an adapter."""

    capability: str
    proof_type: str
    command: AdapterCommand
    project_identity: str
    snapshot_identity: str
    manifest_identity: str
    build_target_identity: str
    paths: tuple[tuple[str, str], ...] = ()
    hashes: tuple[tuple[str, str], ...] = ()
    payload: tuple[tuple[str, str], ...] = ()
    request_sha256: str = ""
    protocol: str = REQUEST_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != REQUEST_PROTOCOL:
            raise ValueError("unsupported request protocol")
        for name in (
            "capability",
            "proof_type",
            "project_identity",
            "snapshot_identity",
            "manifest_identity",
            "build_target_identity",
        ):
            _text(getattr(self, name), name)
        for key, value in self.paths:
            _path(key, "path key")
            _path(value, "path value")
        for key, value in self.hashes:
            _text(key, "hash key")
            _hash(value, f"hashes[{key}]")
        for key, value in self.payload:
            _text(key, "payload key")
            _text(value, f"payload[{key}]")
        if self.request_sha256:
            _hash(self.request_sha256, "request_sha256")
            if self.request_sha256 != self._digest():
                raise ValueError("request_sha256 does not match canonical request")

    def _body(self) -> dict[str, object]:
        return {
            "build_target_identity": self.build_target_identity,
            "capability": self.capability,
            "command": self.command.as_dict(),
            "hashes": {k: v for k, v in sorted(self.hashes)},
            "manifest_identity": self.manifest_identity,
            "paths": {k: v for k, v in sorted(self.paths)},
            "payload": {k: v for k, v in sorted(self.payload)},
            "project_identity": self.project_identity,
            "proof_type": self.proof_type,
            "protocol": self.protocol,
            "snapshot_identity": self.snapshot_identity,
            "request_sha256": "",
        }

    def _digest(self) -> str:
        return hashlib.sha256(_canonical(self._body())).hexdigest()

    @property
    def identity(self) -> str:
        return self.request_sha256 or self._digest()

    def as_dict(self) -> dict[str, object]:
        data = self._body()
        data["request_sha256"] = self.identity
        return data

    def to_json_bytes(self) -> bytes:
        return _canonical(self.as_dict())

    def with_input_hashes(self, root: Path) -> AdapterRequest:
        """Return a request whose declared file inputs are hash-bound.

        Existing hashes are authenticated rather than silently replaced.  A
        declared path that is not a regular, non-symlink file is rejected.
        """
        bound = dict(self.hashes)
        for name, relative in self.paths:
            path = (root / PurePosixPath(relative)).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"declared input escapes root: {relative}") from exc
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"declared input is not a non-empty regular file: {relative}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if name in bound and bound[name] != digest:
                raise ValueError(f"declared input hash mismatch: {name}")
            bound[name] = digest
        return AdapterRequest(
            self.capability,
            self.proof_type,
            self.command,
            self.project_identity,
            self.snapshot_identity,
            self.manifest_identity,
            self.build_target_identity,
            self.paths,
            tuple(sorted(bound.items())),
            self.payload,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AdapterRequest:
        required = {
            "protocol",
            "capability",
            "proof_type",
            "command",
            "project_identity",
            "snapshot_identity",
            "manifest_identity",
            "build_target_identity",
            "paths",
            "hashes",
            "payload",
            "request_sha256",
        }
        if set(data) != required:
            raise ValueError("request contains unknown or missing fields")
        command = data["command"]
        if not isinstance(command, Mapping) or set(command) != {"argv", "executable_sha256"}:
            raise ValueError("invalid request command")
        argv = command["argv"]
        if not isinstance(argv, list):
            raise ValueError("command argv must be an array")

        def pairs(value: object, name: str) -> tuple[tuple[str, str], ...]:
            if not isinstance(value, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"{name} must be a string mapping")
            return tuple(sorted(value.items()))

        return cls(
            capability=data["capability"],
            proof_type=data["proof_type"],
            command=AdapterCommand(tuple(argv), command["executable_sha256"]),
            project_identity=data["project_identity"],
            snapshot_identity=data["snapshot_identity"],
            manifest_identity=data["manifest_identity"],
            build_target_identity=data["build_target_identity"],
            paths=pairs(data["paths"], "paths"),
            hashes=pairs(data["hashes"], "hashes"),
            payload=pairs(data["payload"], "payload"),
            request_sha256=data["request_sha256"],
            protocol=data["protocol"],
        )


@dataclass(frozen=True, slots=True)
class AdapterAttachment:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _path(self.path, "attachment path")
        _hash(self.sha256, "attachment sha256")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes <= 0:
            raise ValueError("attachment size_bytes must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class AdapterResult:
    request_sha256: str
    outcome: str
    attachments: tuple[AdapterAttachment, ...] = ()
    details: tuple[tuple[str, str], ...] = ()
    protocol: str = RESULT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != RESULT_PROTOCOL:
            raise ValueError("unsupported result protocol")
        _hash(self.request_sha256, "request_sha256")
        if self.outcome not in {"pass", "fail", "unknown"}:
            raise ValueError("outcome must be pass, fail, or unknown")
        for key, value in self.details:
            _text(key, "detail key")
            _text(value, f"details[{key}]")

    def as_dict(self) -> dict[str, object]:
        return {
            "attachments": [a.as_dict() for a in self.attachments],
            "details": {k: v for k, v in sorted(self.details)},
            "outcome": self.outcome,
            "protocol": self.protocol,
            "request_sha256": self.request_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, expected_request_sha256: str) -> AdapterResult:
        if set(data) != {"protocol", "request_sha256", "outcome", "attachments", "details"}:
            raise ValueError("result contains unknown or missing fields")
        if data["request_sha256"] != expected_request_sha256:
            raise ValueError("result request hash does not match request")
        raw = data["attachments"]
        if not isinstance(raw, list):
            raise ValueError("attachments must be an array")
        attachments = tuple(
            AdapterAttachment(x["path"], x["sha256"], x["size_bytes"])
            for x in raw
            if isinstance(x, Mapping) and set(x) == {"path", "sha256", "size_bytes"}
        )
        if len(attachments) != len(raw):
            raise ValueError("invalid attachment")
        details = data["details"]
        if not isinstance(details, Mapping):
            raise ValueError("details must be a string mapping")
        return cls(
            data["request_sha256"], data["outcome"], attachments, tuple(sorted(details.items())), data["protocol"]
        )


__all__ = [
    "REQUEST_PROTOCOL",
    "RESULT_PROTOCOL",
    "AdapterAttachment",
    "AdapterCommand",
    "AdapterRequest",
    "AdapterResult",
]
