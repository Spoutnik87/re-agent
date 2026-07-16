"""Explicit, project-scoped Release 5 promotion commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from re_agent.project import load_verified_project
from re_agent.promotion.models import ProjectState, PromotionState
from re_agent.promotion.service import PromotionResult, PromotionService


def _address_target(project_root: Path, address: str) -> str:
    try:
        value = int(address, 0)
    except ValueError as exc:
        raise ValueError(f"invalid address: {address}") from exc
    context = load_verified_project(project_root)
    matches = [item.name for item in context.verified_abi_manifest.value.symbols if item.address == value]
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous promotion address: {address}")
    return matches[0]


def _promotion_root(project_root: Path, requested: str | None) -> Path:
    """Use an isolated sibling by default; never put promotion data in the project."""
    root = project_root.resolve()
    promotion = Path(requested).resolve() if requested else root.parent / f".{root.name}-promotion"
    if promotion == root or root in promotion.parents:
        raise ValueError("--promotion-root must be outside --project-root")
    return promotion


def _service(args: argparse.Namespace, project_root: Path) -> PromotionService:
    return PromotionService(
        project_root,
        promotion_root=_promotion_root(project_root, getattr(args, "promotion_root", None)),
        profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
    )


def _result_dict(result: PromotionResult) -> dict[str, object]:
    return {
        "project": result.bundle.project,
        "target": result.bundle.target,
        "candidate": result.bundle.candidate,
        "bundle_sha256": result.bundle_sha256,
        "state": result.project.state.value if result.project is not None else None,
        "batch_hash": result.batch.record_hash if result.batch is not None else None,
    }


def _state_dict(state: ProjectState) -> dict[str, object]:
    return {
        "project": state.project,
        "candidate": state.candidate,
        "state": state.state.value,
        "batch_hash": state.batch_hash,
        "targets": [
            {
                "target": item.target,
                "candidate": item.candidate,
                "state": item.state.value,
                "bundle_sha256": item.bundle_sha256,
            }
            for item in state.targets
        ],
    }


def _require_active_promoted(service: PromotionService, results: list[PromotionResult], candidate: str | None) -> None:
    """Do not report project promotion until the service confirms publication."""
    if not results:
        raise RuntimeError("project promotion produced no proof results")
    if any(result.project is None or result.project.state is not PromotionState.PROMOTED for result in results):
        raise RuntimeError("project promotion did not publish an active PROMOTED view")
    published_candidate = candidate or results[0].bundle.candidate
    state = service.status(candidate=published_candidate)
    if state.state is not PromotionState.PROMOTED or any(
        target.state is not PromotionState.DIFFERENTIAL_PASS for target in state.targets
    ):
        raise RuntimeError("project promotion did not publish an authenticated active PROMOTED view")


def _print(value: object, output_format: str = "text") -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, list):
        for item in value:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, dict):
        print("\n".join(f"{key}: {item}" for key, item in value.items()))
    else:
        print(value)


def cmd_promote(args: argparse.Namespace) -> int:
    if not args.project_root:
        print("ERROR: --project-root is required", file=sys.stderr)
        return 1
    root = Path(args.project_root)
    try:
        if args.promote_command == "prove":
            if args.original_binary and args.proof != "differential":
                raise ValueError("--original-binary is only valid with --proof differential")
            if args.proof == "differential" and not args.original_binary:
                raise ValueError("--original-binary is required for differential proof")
            target = _address_target(root, args.address) if args.address else None
            service = _service(args, root)
            if args.proof == "abi":
                raw = service.inspect_abi(target=target, candidate=args.build_id)
            else:
                raw = service.run_differential(
                    original_binary_equivalent=Path(args.original_binary), target=target, candidate=args.build_id
                )
            results = [raw] if isinstance(raw, PromotionResult) else list(raw)
            _print([_result_dict(item) for item in results])
            return 0
        if args.promote_command == "status":
            state = _service(args, root).status(candidate=args.build_id)
            if args.address:
                target = _address_target(root, args.address)
                state = ProjectState(
                    state.project,
                    state.candidate,
                    state.state,
                    tuple(item for item in state.targets if item.target == target),
                    state.batch_hash,
                )
            _print(_state_dict(state), args.format)
            return 0
        if args.promote_command == "project":
            if not args.original_binary:
                raise ValueError("--original-binary is required for project promotion")
            service = _service(args, root)
            raw = service.promote(original_binary_equivalent=Path(args.original_binary), candidate=args.build_id)
            results = [raw] if isinstance(raw, PromotionResult) else list(raw)
            _require_active_promoted(service, results, args.build_id)
            _print([_result_dict(item) for item in results])
            return 0
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: promotion failed: {exc}", file=sys.stderr)
        return 1
    return 1


__all__ = ["cmd_promote"]
