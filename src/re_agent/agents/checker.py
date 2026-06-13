"""Checker agent — verifies reversed code against Ghidra decompilation."""

from __future__ import annotations

import re
from pathlib import Path

from re_agent.backend.protocol import REBackend
from re_agent.core.models import CheckerVerdict, DecompileResult, FunctionTarget, Verdict
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.utils.templates import render_template

PROMPTS_DIR = Path(__file__).parent / "prompts"
VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.I)
SUMMARY_RE = re.compile(r"SUMMARY:\s*(.+)")
ISSUES_RE = re.compile(r"ISSUES:\s*\n((?:\s*-\s*.+\n?)+)", re.I)
FIX_RE = re.compile(r"FIX_INSTRUCTIONS:\s*\n((?:\s*-\s*.+\n?)+)", re.I)


class CheckerAgent:
    """Verifies reversed code against Ghidra decompilation."""

    def __init__(
        self, llm: LLMProvider, backend: REBackend, project_description: str = "", checker_custom_rules: str = ""
    ) -> None:
        self.llm = llm
        self.backend = backend
        self._project_description = project_description
        self._checker_custom_rules = checker_custom_rules
        self.last_prompt: str = ""
        self.last_response: str = ""

    def check(
        self,
        code: str,
        target: FunctionTarget,
        decompile_result: DecompileResult | None = None,
    ) -> CheckerVerdict:
        """Check reversed code against decompilation. Returns CheckerVerdict.

        Always uses stateless messages — no conversation persistence.
        The checker compares the same decompile against different reversed
        code each round, so accumulated history is pure token waste.

        Args:
            code: The reversed C++ code to verify.
            target: Function identification.
            decompile_result: Pre-fetched decompile result. When provided,
                skips the redundant backend call (optimized path).
        """
        if decompile_result is not None:
            decompiled = decompile_result.raw_output
        else:
            decompile_result = self.backend.decompile(target.address)
            decompiled = decompile_result.raw_output

        system_prompt = render_template(
            PROMPTS_DIR / "checker_system.md",
            project_description=self._project_description,
            custom_rules=self._checker_custom_rules,
        )
        task_prompt = render_template(
            PROMPTS_DIR / "checker_task.md",
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
            reversed_code=code,
            decompiled=decompiled,
        )

        self.last_prompt = task_prompt
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=task_prompt),
        ]
        response = self.llm.send(messages)

        self.last_response = response
        return self._parse_verdict(response)

    @staticmethod
    def _parse_verdict(response: str) -> CheckerVerdict:
        verdict_match = VERDICT_RE.search(response)
        if verdict_match:
            verdict_str = verdict_match.group(1).upper()
            verdict = Verdict.PASS if verdict_str == "PASS" else Verdict.FAIL
        else:
            verdict = Verdict.UNKNOWN

        summary_match = SUMMARY_RE.search(response)
        summary = summary_match.group(1).strip() if summary_match else ""

        issues: list[str] = []
        issues_match = ISSUES_RE.search(response)
        if issues_match:
            for line in issues_match.group(1).strip().splitlines():
                item = line.strip().lstrip("- ").strip()
                if item and item.lower() != "none":
                    issues.append(item)

        fix_instructions: list[str] = []
        fix_match = FIX_RE.search(response)
        if fix_match:
            for line in fix_match.group(1).strip().splitlines():
                item = line.strip().lstrip("- ").strip()
                if item and item.lower() != "none":
                    fix_instructions.append(item)

        return CheckerVerdict(
            verdict=verdict,
            summary=summary,
            issues=issues,
            fix_instructions=fix_instructions,
        )
