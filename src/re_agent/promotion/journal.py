"""Hash-chained, append-only promotion journal.

.. deprecated::
   The per-file ``_journal_lock`` is kept as a fallback but no longer used
   by ``append()``.  Callers are expected to hold a ``PromotionLock``
   (from ``re_agent.promotion.lock``) for the duration of the transaction.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from re_agent.promotion.models import ProofBundle, canonical_json, sha256

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@contextmanager
def _journal_lock(path: Path) -> Iterator[None]:
    lock = Path(f"{path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    handle = None
    owned = False
    try:
        try:
            handle = lock.open("x", encoding="ascii")
        except FileExistsError as exc:
            raise ValueError(f"promotion journal is already locked: {path}") from exc
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


@dataclass(frozen=True, slots=True)
class PromotionBatch:
    project: str
    candidate: str
    bundles: tuple[str, ...]
    previous_hash: str
    record_hash: str = ""

    def as_dict(self, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "project": self.project,
            "candidate": self.candidate,
            "bundles": list(self.bundles),
            "previous_hash": self.previous_hash,
        }
        if include_hash:
            value["record_hash"] = self.record_hash
        return value

    def sealed(self) -> PromotionBatch:
        return PromotionBatch(
            self.project, self.candidate, tuple(self.bundles), self.previous_hash, sha256(self.as_dict(False))
        )

    def verify(self, expected_previous: str | None = None) -> None:
        if not self.project or not self.candidate or not self.bundles or len(set(self.bundles)) != len(self.bundles):
            raise ValueError("malformed promotion batch")
        if any(not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in self.bundles):
            raise ValueError("malformed promotion batch bundle identity")
        if self.previous_hash and not _DIGEST.fullmatch(self.previous_hash):
            raise ValueError("malformed promotion journal chain")
        if not self.previous_hash and expected_previous not in (None, ""):
            raise ValueError("broken promotion journal chain")
        if expected_previous is not None and self.previous_hash != expected_previous:
            raise ValueError("broken promotion journal chain")
        if not _DIGEST.fullmatch(self.record_hash) or self.record_hash != sha256(self.as_dict(False)):
            raise ValueError("invalid promotion journal record")


class PromotionJournal:
    """One record per successful complete batch; failed attempts write nothing."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(
        self, bundles: tuple[ProofBundle, ...], *, project: str, candidate: str, expected_targets: tuple[str, ...]
    ) -> PromotionBatch:
        if (
            not expected_targets
            or len(set(expected_targets)) != len(expected_targets)
            or tuple(sorted(bundle.target for bundle in bundles)) != tuple(sorted(expected_targets))
        ):
            raise ValueError("batch is not complete")
        for bundle in bundles:
            bundle.verify()
            if bundle.project != project or bundle.candidate != candidate:
                raise ValueError("batch identity mismatch")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.records()
        previous = existing[-1].record_hash if existing else ""
        record = PromotionBatch(
            project, candidate, tuple(bundle.bundle_sha256 for bundle in bundles), previous
        ).sealed()
        with self.path.open("ab") as stream:
            stream.write(canonical_json(record.as_dict()))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return record

    def records(self) -> tuple[PromotionBatch, ...]:
        if not self.path.exists():
            return ()
        if self.path.stat().st_size == 0 or not self.path.read_bytes().endswith(b"\n"):
            raise ValueError("truncated promotion journal")
        result: list[PromotionBatch] = []
        previous = ""
        for line in self.path.read_bytes().splitlines():
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict) or set(raw) != {
                    "project",
                    "candidate",
                    "bundles",
                    "previous_hash",
                    "record_hash",
                }:
                    raise ValueError("malformed promotion journal record")
                record = PromotionBatch(
                    raw["project"], raw["candidate"], tuple(raw["bundles"]), raw["previous_hash"], raw["record_hash"]
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("invalid promotion journal record") from exc
            record.verify(previous)
            previous = record.record_hash
            result.append(record)
        return tuple(result)


__all__ = ["PromotionBatch", "PromotionJournal"]
