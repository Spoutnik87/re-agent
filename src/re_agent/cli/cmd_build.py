"""re-agent build command — code reconstruction from flat .cpp files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.state.pipeline_state import PipelineState


def _dry_report(
    exit_code: int,
    summary: dict[str, object],
    results: list[dict[str, object]] | None = None,
    *,
    usage: dict[str, int] | None = None,
    budget: dict[str, object] | None = None,
) -> None:
    complete_summary: dict[str, object] = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "incomplete": 0,
        "hard_rejects": 0,
        "budget_exceeded": 0,
        "provider_errors": 0,
        "contract_failed": exit_code != 0,
    }
    for key in complete_summary:
        if key in summary:
            complete_summary[key] = summary[key]
    print(
        json.dumps(
            {
                "run_type": "no-persist",
                "exit_code": exit_code,
                "summary": complete_summary,
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_calls": 0},
                "budget": budget
                or {
                    "calls_remaining": 0,
                    "tokens_remaining": 0,
                    "compile_retry_calls_remaining": 0,
                    "exceeded": False,
                    "exceeded_reason": "",
                },
                "results": results or [],
            },
            separators=(",", ":"),
        )
    )


def cmd_build(args: argparse.Namespace) -> int:
    dry_run = bool(getattr(args, "no_persist", False))
    project_root_raw = getattr(args, "project_root", None)
    profile_raw = getattr(args, "profile", None)

    def cli_error(message: str) -> int:
        if dry_run:
            _dry_report(2, {"error": message})
        else:
            print(message, file=sys.stderr)
        return 2

    if profile_raw and not project_root_raw:
        return cli_error("Error: --profile requires --project-root")
    project_context = None
    if project_root_raw:
        try:
            from re_agent.project.context import load_verified_project

            project_context = load_verified_project(Path(project_root_raw))
        except (OSError, ValueError) as exc:
            return cli_error(f"Project error: {exc}")
    try:
        if project_context is None:
            config = load_config(Path(args.config))
        else:
            config = load_config(Path(args.config), verified_contract_override=project_context.verified_abi_manifest)
    except (ValueError, FileNotFoundError) as exc:
        if dry_run:
            _dry_report(2, {"error": str(exc)})
            return 2
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    build_cfg = config.build
    llm_cfg = config.llm
    pipeline_cfg = config.pipeline
    if project_context is not None:
        build_cfg.input.ghidra_exports = str(project_context.snapshot_root)
        config.contracts.verified_manifest = project_context.verified_abi_manifest

    persist = not getattr(args, "no_persist", False)
    phase = getattr(args, "phase", None)
    address = getattr(args, "address", None)
    module = getattr(args, "module", None)
    subunit = getattr(args, "subunit", None)

    # ABI-preserving builds must be explicit and bounded.  Keep these checks
    # at the CLI boundary so an invalid invocation cannot start a mutating
    # phase before it is rejected.
    contracts_cfg = getattr(config, "contracts", None)
    preserve_abi = getattr(contracts_cfg, "transformation_policy", None) == "preserve_abi"
    verified_compile_command = None
    if address is not None and (module is not None or subunit is not None):
        return cli_error("Error: --address cannot be combined with --module or --subunit")
    if address is not None and getattr(args, "max_subunits", None) is not None:
        return cli_error("Error: --address cannot be combined with --max-subunits")
    if address is not None and not preserve_abi:
        return cli_error("Error: --address is only valid with contracts transformation_policy=preserve_abi")
    if preserve_abi:
        if phase == "analyze" and address is not None:
            return cli_error("Error: --phase analyze cannot be combined with --address")
        if phase != "transform" and address is not None:
            return cli_error("Error: --address is only valid with --phase transform")
        if phase == "analyze" and any(x is not None for x in (module, subunit, getattr(args, "max_subunits", None))):
            return cli_error("Error: preserve_abi analyze does not accept transform selectors")
        if phase == "transform" and address is None:
            return cli_error("Error: --phase transform with preserve_abi requires exactly one --address")
        if phase in (None, "assemble"):
            phase_label = "all phases" if phase is None else "--phase assemble"
            return cli_error(f"Error: preserve_abi does not allow {phase_label}; use --phase analyze")

    # --no-persist is only valid with an explicit --phase transform.
    # With --phase analyze, --phase assemble, or no --phase (which runs
    # all phases including analyze/assemble), --no-persist would silently
    # skip writes — this is a user error.
    if not persist and phase != "transform":
        phase_label = phase if phase else "(all phases)"
        return cli_error(f"Error: --no-persist is only valid with --phase transform (got --phase {phase_label})")

    if project_context is not None and persist and phase == "transform":
        try:
            from re_agent.toolchain.activation import resolve_capability

            (verified_compile_command,) = resolve_capability(
                project_root=project_context.root,
                capability="compile",
                profile_path=Path(profile_raw) if profile_raw else None,
            )
        except ValueError as exc:
            return cli_error(f"Toolchain error: {exc}")

    state = PipelineState(pipeline_cfg.state_file) if persist else None

    def preserve_failure(message: str) -> int:
        if persist and state is not None:
            state.update_build("failed")
            state.flush()
            print(message, file=_out)
        else:
            _dry_report(2, {"error": message})
        return 2

    phases = [phase] if phase else ["analyze", "transform", "assemble"]

    from re_agent.build.analyze.clusterer import cluster
    from re_agent.build.analyze.graph_builder import build_graph
    from re_agent.build.analyze.indexer import index_modules
    from re_agent.build.assemble.tree_builder import build_tree
    from re_agent.build.transform.module_processor import process_modules

    has_incomplete = False
    contract_failed = False

    # --no-persist: human messages go to stderr; stdout is reserved for JSON.
    _out = sys.stderr if not persist else sys.stdout

    try:
        if "analyze" in phases:
            if persist:
                print("=== Phase 1/3: Analyze (call graph + clustering) ===")
            graph = build_graph(build_cfg)
            modules = cluster(graph, build_cfg)
            index_modules(modules, build_cfg)

            from re_agent.build.analyze.decls_generator import write_decls_header

            decls_path = write_decls_header(config)
            if decls_path is not None:
                print(f"Wrote declarations header: {decls_path}", file=_out)
            mc = modules["metadata"]["module_count"]
            oc = modules["metadata"]["orphan_count"]
            if persist:
                print(f"Analyze complete: {mc} modules, {oc} orphans")
            if persist and state is not None:
                state.update_build("in_progress", phase="analyze", modules_completed=[])

        if "transform" in phases:
            if persist:
                print("=== Phase 2/3: Transform (LLM code refinement) ===")
            if preserve_abi:
                from re_agent.build.transform.manifest_bound_transform import (
                    ManifestBoundTransformError,
                    ManifestBoundVerdict,
                    run_manifest_bound_transform,
                )

                try:
                    if contracts_cfg is None or address is None:
                        raise ManifestBoundTransformError(
                            "preserve_abi transform requires a verified manifest and address"
                        )
                    result = run_manifest_bound_transform(
                        build_cfg,
                        llm_cfg,
                        contracts_cfg.verified_manifest,
                        address,
                        run_id=getattr(args, "run_id", "") or "",
                        persist=persist,
                        verified_compile_command=verified_compile_command,
                    )
                except ManifestBoundTransformError as exc:
                    if not persist:
                        _dry_report(2, {"total": 1, "failed": 1, "hard_rejects": 1})
                    else:
                        return preserve_failure(f"Transform rejected: {exc}")
                    return 2
                except Exception as exc:
                    # Provider/compiler integration failures are non-committing
                    # failures; do not let the CLI continue into assemble.
                    if not persist:
                        _dry_report(2, {"total": 1, "failed": 1, "provider_errors": 1})
                    else:
                        return preserve_failure(f"Transform failed: {exc}")
                    return 2
                if persist and not result.successful:
                    return preserve_failure("Transform rejected: incomplete or unknown manifest-bound verdict")
                if not persist and result.verdict in (
                    ManifestBoundVerdict.PROVIDER_ERROR,
                    ManifestBoundVerdict.BUDGET_EXCEEDED,
                ):
                    result_entry = {
                        "function": f"0x{result.address:X}",
                        "verdict": result.verdict.value,
                        "compiles": False,
                        "files_matched": 0,
                        "match_strategy": "explicit_identity",
                        "identity_state": "explicit",
                        "identity_reason": result.error,
                        "compile_error_category": None,
                        "files": [],
                    }
                    _dry_report(
                        2,
                        {
                            "total": 1,
                            "failed": 1,
                            "provider_errors": result.provider_errors,
                            "budget_exceeded": int(result.verdict is ManifestBoundVerdict.BUDGET_EXCEEDED),
                            "contract_failed": True,
                        },
                        [result_entry],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 2
                if not persist and result.verdict is ManifestBoundVerdict.VALIDATION_ERROR:
                    _dry_report(
                        2,
                        {"total": 1, "failed": 1, "hard_rejects": 1, "contract_failed": True},
                        [
                            {
                                "function": f"0x{result.address:X}",
                                "verdict": result.verdict.value,
                                "compiles": False,
                                "files_matched": 0,
                                "match_strategy": "rejected_identity",
                                "identity_state": "rejected",
                                "identity_reason": result.error,
                                "compile_error_category": None,
                                "files": [],
                            }
                        ],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 2
                if result.verdict == "COMPILE_FAIL":
                    if not persist:
                        _dry_report(
                            2,
                            {"error": "compilation failed"},
                            [
                                {
                                    "function": f"0x{result.address:X}",
                                    "verdict": result.verdict.value,
                                    "compiles": False,
                                    "files_matched": 0,
                                    "match_strategy": None,
                                    "identity_state": None,
                                    "identity_reason": "",
                                    "compile_error_category": "compile",
                                    "files": [],
                                }
                            ],
                        )
                    else:
                        return preserve_failure(f"Transform rejected: compilation failed\n{result.compiler_log}")
                    return 2
                if not persist:
                    if result.verdict is not ManifestBoundVerdict.SKIPPED_COMPILE or result.compiles:
                        _dry_report(2, {"error": "invalid dry-run verdict"})
                        return 2
                    _dry_report(
                        0,
                        {"total": 1, "failed": 1},
                        [
                            {
                                "function": f"0x{result.address:X}",
                                "verdict": result.verdict.value,
                                "compiles": False,
                                "files_matched": 1,
                                "match_strategy": "explicit_identity",
                                "identity_state": "explicit",
                                "identity_reason": "",
                                "compile_error_category": None,
                                "files": [{"path": result.path}],
                            }
                        ],
                        usage=getattr(result, "usage", None),
                        budget=getattr(result, "budget", None),
                    )
                    return 0
                print(
                    f"Transform complete: MANIFEST_BOUND + COMPILE_PASS for 0x{result.address:x}",
                    file=_out,
                )
                summary = {"total": 1, "passed": 1, "incomplete": 0, "budget_exceeded": 0}
            else:
                summary = process_modules(
                    build_cfg,
                    llm_cfg,
                    module=getattr(args, "module", None),
                    subunit=getattr(args, "subunit", None),
                    max_subunits=getattr(args, "max_subunits", None),
                    run_id=getattr(args, "run_id", "") or "",
                    persist=persist,
                )
            if persist and state is not None:
                state.update_build("in_progress", phase="transform", modules_completed=[])

            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            incomplete = summary.get("incomplete", 0)
            budget_exceeded = summary.get("budget_exceeded", 0)
            contract_failed = bool(summary.get("contract_failed", False))

            if total > 0 and passed > 0:
                parts = [f"{passed}/{total} functions compiled"]
                if incomplete:
                    parts.append(f"{incomplete} incomplete targets")
                    has_incomplete = True
                if budget_exceeded:
                    parts.append(f"{budget_exceeded} budget exceeded")
                    has_incomplete = True
                print(f"Transform complete: {', '.join(parts)}", file=_out)
            elif total > 0 and passed == 0:
                if contract_failed:
                    msg = (
                        f"CONTRACT FAILED — {incomplete}/{total} functions have INCOMPLETE_TARGETS "
                        "(TARGET contract required but recovery exhausted)"
                    )
                    print(f"Transform rejected: {msg}", file=_out)
                    has_incomplete = True
                elif budget_exceeded:
                    msg = f"{budget_exceeded}/{total} functions BUDGET_EXCEEDED — transform capped"
                    print(f"Transform rejected: {msg}", file=_out)
                    has_incomplete = True
                elif incomplete:
                    msg = f"{incomplete}/{total} functions have INCOMPLETE_TARGETS — recovery exhausted"
                    print(f"Transform complete: {msg}", file=_out)
                    has_incomplete = True
                else:
                    print(f"Transform complete: 0/{total} functions compiled — see report for details", file=_out)
            else:
                print("Transform complete: no functions processed", file=_out)

        if "assemble" in phases:
            if contract_failed or has_incomplete:
                print("Skipping assemble: contract failed (TARGET violations).", file=_out)
            else:
                if persist:
                    print("=== Phase 3/3: Assemble (project tree) ===")
                build_tree(build_cfg)
                if persist and state is not None:
                    state.update_build("completed")
    except Exception:
        if persist and state is not None:
            state.update_build("failed")
            state.flush()
        raise

    if persist and state is not None:
        state.flush()

    if contract_failed or has_incomplete:
        code = 2 if contract_failed else 1
        label = "CONTRACT FAILED" if contract_failed else "INCOMPLETE"
        print(f"Build {label}: TARGET requirements not met.", file=_out)
        return code
    if persist:
        print("Build complete.")
    return 0
