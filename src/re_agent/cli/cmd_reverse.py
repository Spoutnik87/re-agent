"""re-agent reverse command — single function or class reversal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.reverse.core.models import FunctionTarget
from re_agent.reverse.reports.formatter import format_result


def cmd_reverse(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    rev_cfg = config.reverse

    if args.max_rounds is not None:
        rev_cfg.orchestrator.max_review_rounds = args.max_rounds
    if args.skip_parity:
        rev_cfg.parity.enabled = False
    if args.no_optimize:
        rev_cfg.orchestrator.optimize = False

    if args.dry_run:
        return _dry_run(args, config)

    from re_agent.llm.registry import create_block_provider, create_provider
    from re_agent.reverse.backend.registry import create_backend
    from re_agent.reverse.core.session import Session

    llm = create_provider(config.llm)
    block_llm = create_block_provider(config.llm)
    backend = create_backend(rev_cfg.backend)
    session = Session(rev_cfg.output.session_file)

    if args.address:
        from re_agent.reverse.orchestrator.single import reverse_single

        class_name = args.class_name or ""
        function_name = ""
        if not class_name:
            try:
                dec = backend.decompile(args.address)
                if dec.name and "::" in dec.name:
                    class_name, _, function_name = dec.name.rpartition("::")
                elif dec.name:
                    function_name = dec.name
            except Exception:
                pass

        target = FunctionTarget(
            address=args.address,
            class_name=class_name,
            function_name=function_name,
        )
        result = reverse_single(target, rev_cfg, backend, llm, session, block_llm=block_llm)
        print(format_result(result))
        return 0 if result.success else 1

    if args.class_name:
        from re_agent.reverse.orchestrator.class_runner import reverse_class

        results = reverse_class(
            class_name=args.class_name,
            config=rev_cfg,
            backend=backend,
            llm=llm,
            session=session,
            max_functions=args.max_functions,
            block_llm=block_llm,
        )
        for r in results:
            print(format_result(r))
            print()

        passed = sum(1 for r in results if r.success)
        total = len(results)
        print(f"\nResults: {passed}/{total} passed")
        return 0 if passed == total else 1

    print("Error: specify --address or --class", file=sys.stderr)
    return 1


def _dry_run(args: argparse.Namespace, config: object) -> int:
    print("Dry run mode — no LLM calls will be made.\n")
    if args.address:
        print(f"Would reverse: {args.address}")
        if args.class_name:
            print(f"  Class: {args.class_name}")
        return 0
    if args.class_name:
        print(f"Would reverse functions in class: {args.class_name}")
        max_fn = args.max_functions or 10
        print(f"  Max functions: {max_fn}")
        print(f"  Max rounds per function: {args.max_rounds or 4}")
        return 0
    print("Error: specify --address or --class", file=sys.stderr)
    return 1
