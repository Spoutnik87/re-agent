"""Release 5 promotion CLI guardrails."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from re_agent.cli.cmd_promote import cmd_promote
from re_agent.cli.main import build_parser
from re_agent.promotion.models import ProjectState, PromotionState
from re_agent.promotion.store import PromotionViewPublisher


@pytest.mark.parametrize(
    "argv",
    [
        ["promote", "prove", "--proof", "abi", "--all"],
        ["promote", "prove", "--proof", "differential", "--all"],
        ["promote", "status"],
    ],
)
def test_promote_parser_requires_project_root(argv):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_promote_parser_rejects_target_combinations_and_unknown_controls():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["promote", "prove", "--project-root", "p", "--proof", "abi", "--all", "--address", "0x1"])
    for option in ("--demote", "--reset", "--force", "--partial"):
        with pytest.raises(SystemExit):
            parser.parse_args(["promote", "prove", "--project-root", "p", "--proof", "abi", "--all", option])


def test_cmd_promote_rejects_missing_root_before_side_effects(capsys):
    args = argparse.Namespace(project_root=None, promote_command="prove")
    assert cmd_promote(args) == 1
    assert "--project-root is required" in capsys.readouterr().err


def test_cli_differential_requires_original_binary(tmp_path, capsys):
    args = build_parser().parse_args(
        [
            "promote",
            "prove",
            "--project-root",
            str(tmp_path),
            "--promotion-root",
            str(tmp_path.parent / "promotion"),
            "--proof",
            "differential",
            "--all",
        ]
    )
    assert cmd_promote(args) == 1
    assert "--original-binary is required" in capsys.readouterr().err


def test_cli_rejects_original_binary_for_abi(tmp_path, capsys):
    args = build_parser().parse_args(
        [
            "promote",
            "prove",
            "--project-root",
            str(tmp_path),
            "--promotion-root",
            str(tmp_path.parent / "promotion"),
            "--proof",
            "abi",
            "--all",
            "--original-binary",
            "x",
        ]
    )
    assert cmd_promote(args) == 1
    assert "only valid" in capsys.readouterr().err


def test_project_requires_original_binary_and_accepts_external_root(tmp_path):
    args = build_parser().parse_args(
        [
            "promote",
            "project",
            "--project-root",
            str(tmp_path),
            "--promotion-root",
            str(tmp_path.parent / "promotion"),
            "--original-binary",
            str(tmp_path / "original.bin"),
        ]
    )
    assert args.promotion_root == str(tmp_path.parent / "promotion")
    assert args.original_binary == str(tmp_path / "original.bin")


def test_project_cli_publication_failure_returns_nonzero_and_preserves_active(monkeypatch, tmp_path):
    promotion_root = tmp_path.parent / "promotion"
    prior = ProjectState("demo", "candidate-1", PromotionState.PROMOTED, (), "prior-batch")
    PromotionViewPublisher(promotion_root, auth_key="release-5").publish(prior)
    pointer = promotion_root / "active.json"
    previous = pointer.read_bytes()

    class FailedService:
        def __init__(self, *args, **kwargs):
            pass

        def promote(self, **kwargs):
            return [SimpleNamespace(project=None)]

    monkeypatch.setattr("re_agent.cli.cmd_promote.PromotionService", FailedService)
    args = build_parser().parse_args(
        [
            "promote",
            "project",
            "--project-root",
            str(tmp_path),
            "--promotion-root",
            str(promotion_root),
            "--original-binary",
            str(tmp_path / "original.bin"),
        ]
    )

    assert cmd_promote(args) == 1
    assert pointer.read_bytes() == previous
