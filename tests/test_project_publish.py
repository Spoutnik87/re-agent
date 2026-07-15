from __future__ import annotations

import errno
from pathlib import Path

import pytest

from re_agent.project import publish as publish_module
from re_agent.project.publish import (
    DestinationExistsError,
    PublicationFailureError,
    UnsupportedPublicationError,
    publish_directory,
)


def test_publish_directory_moves_source_without_replacing(tmp_path: Path) -> None:
    source = tmp_path / "stage"
    source.mkdir()
    (source / "payload").write_text("ok", encoding="utf-8")
    destination = tmp_path / "published"

    publish_directory(source, destination)

    assert not source.exists()
    assert (destination / "payload").read_text(encoding="utf-8") == "ok"


def test_publish_directory_rejects_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "stage"
    source.mkdir()
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "sentinel").write_text("keep", encoding="utf-8")

    with pytest.raises(DestinationExistsError):
        publish_directory(source, destination)

    assert source.exists()
    assert (destination / "sentinel").read_text(encoding="utf-8") == "keep"


def test_publish_directory_fails_closed_on_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "stage"
    source.mkdir()
    destination = tmp_path / "published"
    monkeypatch.setattr(publish_module.sys, "platform", "haiku")

    with pytest.raises(UnsupportedPublicationError):
        publish_directory(source, destination)

    assert source.exists()
    assert not destination.exists()


def test_linux_backend_maps_existing_race_to_destination_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "stage"
    source.mkdir()

    def fail(*args: object) -> int:
        ctypes_errno = errno.EEXIST
        monkeypatch.setattr(publish_module.ctypes, "get_errno", lambda: ctypes_errno)
        return -1

    class FakeLibc:
        renameat2 = staticmethod(fail)

    monkeypatch.setattr(publish_module.ctypes, "CDLL", lambda *args, **kwargs: FakeLibc())

    with pytest.raises(DestinationExistsError):
        publish_module._publish_linux(source, tmp_path / "published")


def test_linux_backend_maps_other_error_to_publication_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "stage"
    source.mkdir()

    def fail(*args: object) -> int:
        monkeypatch.setattr(publish_module.ctypes, "get_errno", lambda: errno.EACCES)
        return -1

    class FakeLibc:
        renameat2 = staticmethod(fail)

    monkeypatch.setattr(publish_module.ctypes, "CDLL", lambda *args, **kwargs: FakeLibc())

    with pytest.raises(PublicationFailureError):
        publish_module._publish_linux(source, tmp_path / "published")
