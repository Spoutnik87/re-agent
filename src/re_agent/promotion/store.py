"""No-replace evidence store and immutable promotion-view publisher."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from re_agent.promotion.models import ProjectState, ProofBundle, ProofEvidence, canonical_json, sha256


@contextmanager
def _publication_lock(path: Path) -> Iterator[None]:
    """Serialize publication and remove a lock only when this owner still owns it."""
    lock = Path(f"{path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    handle = None
    owned = False
    try:
        try:
            handle = lock.open("x", encoding="ascii")
        except FileExistsError as exc:
            raise ValueError(f"promotion view is already being published: {path}") from exc
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
        owned = True
        yield
    finally:
        if handle is not None:
            handle.close()
        if owned:
            try:
                if lock.read_text(encoding="ascii") == token:
                    lock.unlink()
            except FileNotFoundError:
                pass


class ImmutableEvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, bundle: ProofBundle) -> str:
        checked = bundle.sealed()
        checked.verify()
        destination = self.root / "bundles" / f"{checked.bundle_sha256}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(destination, flags)
        except FileExistsError as exc:
            raise FileExistsError(f"proof bundle already exists: {checked.bundle_sha256}") from exc
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(checked_to_json(checked))
        return checked.bundle_sha256

    def get(self, bundle_hash: str) -> ProofBundle:
        # 1. Validate digest format
        if not re.fullmatch(r"^[0-9a-f]{64}$", bundle_hash):
            raise ValueError(f"invalid bundle hash: {bundle_hash!r}")

        # 2. Construct path and validate containment
        resolved = (self.root / "bundles" / f"{bundle_hash}.json").resolve()
        bundles_root = (self.root / "bundles").resolve()
        if not resolved.is_relative_to(bundles_root):
            raise ValueError("bundle path escapes store")

        # 3. Reject linked components
        from re_agent.build._platform import _reject_linked_components as _reject

        _reject(resolved)

        # 4. Read and parse
        raw_bytes = resolved.read_bytes()
        raw = json.loads(raw_bytes)
        evidence = tuple(_evidence(item) for item in raw["evidence"])
        bundle = ProofBundle(raw["project"], raw["target"], raw["candidate"], evidence, raw["bundle_sha256"])
        bundle.verify()

        # 5. Cross-check all digest identities
        if bundle.bundle_sha256 != bundle_hash:
            raise ValueError(f"bundle digest mismatch: requested {bundle_hash}, found {bundle.bundle_sha256}")

        return bundle


def checked_to_json(bundle: ProofBundle) -> bytes:
    return canonical_json(bundle.as_dict())


class PromotionViewPublisher:
    def __init__(self, root: Path, *, auth_key: bytes | str | None = None) -> None:
        self.root = Path(root)
        self.auth_key = auth_key

    def publish(self, state: ProjectState) -> str:
        payload: dict[str, object] = {
            "project": state.project,
            "candidate": state.candidate,
            "state": state.state.value,
            "batch_hash": state.batch_hash,
            "targets": [
                item.__dict__
                if hasattr(item, "__dict__")
                else {
                    "project": item.project,
                    "target": item.target,
                    "candidate": item.candidate,
                    "state": item.state.value,
                    "bundle_sha256": item.bundle_sha256,
                    "proof_types": list(item.proof_types),
                    "build_identity": item.build_identity,
                }
                for item in state.targets
            ],
        }
        summary_hash = sha256(payload)
        self.root.mkdir(parents=True, exist_ok=True)
        summary = self.root / "summaries" / f"{summary_hash}.json"
        summary.parent.mkdir(parents=True, exist_ok=True)
        pointer = self.root / "active.json"
        with _publication_lock(pointer):
            self._publish_summary(summary, payload, summary_hash)
            pointer_payload = {
                "format_version": 1,
                "summary_id": summary_hash,
                "summary_sha256": summary_hash,
            }
            pointer_bytes = self._authenticated_pointer(pointer_payload)
            fd, temporary_name = tempfile.mkstemp(prefix=".active.", dir=self.root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(pointer_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, pointer)
                if os.name != "nt":
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        return summary_hash

    def load_active(self) -> dict[str, object]:
        """Authenticate the active pointer and verify its immutable summary bytes."""
        pointer = self.root / "active.json"
        try:
            pointer_bytes = pointer.read_bytes()
            raw = json.loads(pointer_bytes)
            if not isinstance(raw, dict):
                raise ValueError("malformed active promotion pointer")
            if canonical_json(raw) != pointer_bytes:
                raise ValueError("non-canonical active promotion pointer")
            authentication = raw.pop("authentication")
            if raw.get("format_version") != 1:
                raise ValueError("unsupported active promotion pointer")
            summary_id = raw.get("summary_id")
            summary_hash = raw.get("summary_sha256")
            if not isinstance(summary_id, str) or summary_id != summary_hash:
                raise ValueError("active promotion pointer identity mismatch")
            expected = json.loads(self._authenticated_pointer(raw))
            if authentication != expected["authentication"]:
                raise ValueError("active promotion pointer authentication failed")
            summary = self.root / "summaries" / f"{summary_id}.json"
            return self._read_summary(summary, summary_id)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid active promotion pointer: {pointer}") from exc

    def _publish_summary(self, path: Path, payload: dict[str, object], digest: str) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            self._read_summary(path, digest)
            return
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        self._read_summary(path, digest)

    def _read_summary(self, path: Path, digest: str) -> dict[str, object]:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes)
        if not isinstance(raw, dict) or canonical_json(raw) != raw_bytes or sha256(raw) != digest:
            raise ValueError("invalid immutable promotion summary")
        return cast(dict[str, object], raw)

    def _authenticated_pointer(self, payload: dict[str, object]) -> bytes:
        body = canonical_json(payload)
        if self.auth_key is None:
            value = hashlib.sha256(body).hexdigest()
            algorithm = "sha256"
        else:
            key = self.auth_key.encode("utf-8") if isinstance(self.auth_key, str) else self.auth_key
            value = hmac.new(key, body, hashlib.sha256).hexdigest()
            algorithm = "hmac-sha256"
        return canonical_json({**payload, "authentication": {"algorithm": algorithm, "value": value}})


def _evidence(raw: dict[str, object]) -> ProofEvidence:
    return ProofEvidence(
        cast(str, raw["evidence_type"]),
        cast(str, raw["subject"]),
        cast(dict[str, Any], raw["payload"]),
        cast(str, raw["evidence_sha256"]),
    )


__all__ = ["ImmutableEvidenceStore", "PromotionViewPublisher"]
