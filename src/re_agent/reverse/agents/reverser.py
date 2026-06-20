"""Reverser agent — gathers context and asks LLM to produce reversed C++ code."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from re_agent.config.schema import ProjectProfile
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.reverse.agents.few_shot_builder import FewShotBuilder
from re_agent.reverse.agents.source_context import SourceContextBuilder
from re_agent.reverse.backend.protocol import REBackend
from re_agent.reverse.core.models import DecompileResult, FunctionTarget
from re_agent.reverse.core.session import Session
from re_agent.reverse.parity.source_indexer import SourceIndexer
from re_agent.reverse.utils.templates import render_template
from re_agent.reverse.utils.text import strip_ghidra_noise

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
CODE_BLOCK_RE = re.compile(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", re.S)
REVERSED_TAG_RE = re.compile(r"REVERSED_FUNCTION:\s*(.+)")
PHASE_2_MARKER_RE = re.compile(r"##\s*Phase\s*2\s*:\s*Code", re.I)
PHASE_1_SECTION_RE = re.compile(r"##\s*Phase\s*1\s*:.*?\n(.*?)(?=##\s*Phase\s*2|\Z)", re.S | re.I)


class ReverserAgent:
    """Gathers decompile context and asks the LLM to reverse a function."""

    def __init__(
        self,
        llm: LLMProvider,
        backend: REBackend,
        source_root: Path | None = None,
        project_profile: ProjectProfile | None = None,
        indexer: SourceIndexer | None = None,
        session: Session | None = None,
        report_dir: Path | None = None,
        optimize: bool = False,
        enable_phase1: bool = True,
        inject_source_context: bool = True,
        inject_few_shot: bool = True,
        few_shot_max_examples: int = 2,
    ) -> None:
        self.llm = llm
        self.backend = backend
        self.optimize = optimize
        self.enable_phase1 = enable_phase1
        self._inject_source_context = inject_source_context
        self._inject_few_shot = inject_few_shot
        self._few_shot_max_examples = few_shot_max_examples
        project_context = project_profile.project_context if project_profile else ""
        self._system_prompt = render_template(
            PROMPTS_DIR / "reverser_system.md",
            project_context=project_context,
        )
        if not enable_phase1:
            self._system_prompt += (
                "\n\nDO NOT produce a Phase 1 analysis. "
                "Output ONLY the reversed C++ code in a ```cpp block, "
                "with inline comments for key decisions. "
                "End with REVERSED_FUNCTION: tag."
            )
        self._source_context_builder: SourceContextBuilder | None = None
        if source_root is not None and project_profile is not None and source_root.exists():
            self._source_context_builder = SourceContextBuilder(
                source_root=source_root,
                profile=project_profile,
                indexer=indexer,
                session=session,
                report_dir=report_dir,
            )
        self._conversation_id: str | None = None
        self.last_prompt: str = ""
        self.last_response: str = ""
        self.last_decompile_result: DecompileResult | None = None
        self._phase1_analysis: str = ""
        self._few_shot_builder: FewShotBuilder | None = None
        if source_root is not None and source_root.exists():
            self._few_shot_builder = FewShotBuilder.singleton(source_root)

    def reverse(self, target: FunctionTarget) -> tuple[str, str]:
        """Reverse a function. Returns (code, reversed_function_tag)."""
        decompile_result = self.backend.decompile(target.address)
        self.last_decompile_result = decompile_result
        decompiled = decompile_result.raw_output
        if self.optimize:
            decompiled = strip_ghidra_noise(decompiled)

        caps = self.backend.capabilities

        xrefs_text = ""
        if caps.has_xrefs:
            try:
                xrefs = self.backend.xrefs_from(target.address)
                xrefs_text = "\n".join(f"- {x.name} ({x.address}) [{x.ref_type}]" for x in xrefs) or "None found"
            except Exception:
                xrefs_text = "Unavailable"

        structs_text = ""
        if caps.has_structs and target.class_name:
            try:
                struct = self.backend.get_struct(target.class_name)
                if struct:
                    structs_text = f"{struct.name} (size: {struct.size})\n"
                    structs_text += "\n".join(
                        f"  +0x{f.offset:X} {f.type_str} {f.name} (size: {f.size})" for f in struct.fields
                    )
            except Exception:
                structs_text = "Unavailable"

        source_context = ""
        if self._inject_source_context and self._source_context_builder is not None:
            source_context = self._source_context_builder.build(target)
            if self.optimize and source_context == "No relevant existing source context found.":
                source_context = ""

        task_prompt = render_template(
            PROMPTS_DIR / "reverser_task.md",
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
            decompiled=decompiled,
        )

        if self._conversation_id is None and self.llm.supports_conversations:
            self._conversation_id = self.llm.new_conversation(self._system_prompt)

        if xrefs_text and xrefs_text not in ("None found", "Unavailable", "None"):
            task_prompt += f"\n\n**Cross-references (calls from this function):**\n{xrefs_text}"
        if structs_text and structs_text not in ("Unavailable", "None"):
            task_prompt += f"\n\n**Struct/type context:**\n{structs_text}"
        if source_context and source_context not in ("None", "No relevant existing source context found."):
            task_prompt += f"\n\n**Existing source context:**\n{source_context}"

        if self._inject_few_shot and self._few_shot_builder is not None and self._few_shot_max_examples != 0:
            examples = self._few_shot_builder.find_similar(decompiled, max_examples=self._few_shot_max_examples)
            if examples:
                task_prompt += "\n\n**Reference examples (similar functions successfully decompiled):**\n"
                task_prompt += "\n".join(examples)

        self.last_prompt = task_prompt

        if self._conversation_id:
            response = self.llm.resume(self._conversation_id, task_prompt)
        else:
            messages = [
                Message(role="system", content=self._system_prompt),
                Message(role="user", content=task_prompt),
            ]
            response = self.llm.send(messages)

        self.last_response = response
        self._phase1_analysis = self._extract_phase1(response)
        code = self._extract_code(response)
        tag = self._extract_tag(response)
        return code, tag

    def fix(
        self,
        checker_report: str,
        issues: list[str],
        fix_instructions: list[str],
        target: FunctionTarget,
        decompiled: str = "",
        prior_code: str = "",
        objective_findings: list[str] | None = None,
    ) -> tuple[str, str]:
        """Ask the reverser to fix code based on checker feedback."""
        all_issues = list(issues)
        all_fix_instructions = list(fix_instructions)
        if objective_findings:
            all_issues.extend(f"objective verifier: {finding}" for finding in objective_findings)
            all_fix_instructions.extend("Resolve objective mismatch: " + finding for finding in objective_findings)
        fix_prompt = render_template(
            PROMPTS_DIR / "fix_instructions.md",
            issues="\n".join(f"- {i}" for i in all_issues),
            fix_instructions="\n".join(f"- {i}" for i in all_fix_instructions),
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
            decompiled=decompiled,
            prior_code=prior_code,
            phase1_summary=self._phase1_analysis,
        )

        self.last_prompt = fix_prompt

        if self.optimize:
            messages = [
                Message(role="system", content=self._system_prompt),
                Message(role="user", content=fix_prompt),
            ]
            response = self.llm.send(messages)
        elif self._conversation_id:
            response = self.llm.resume(self._conversation_id, fix_prompt)
        else:
            messages = [Message(role="user", content=fix_prompt)]
            response = self.llm.send(messages)

        self.last_response = response
        self._phase1_analysis = self._extract_phase1(response) if self.enable_phase1 else ""
        code = self._extract_code(response)
        tag = self._extract_tag(response)
        return code, tag

    def _extract_code(self, response: str) -> str:
        if self.enable_phase1:
            phase2_match = PHASE_2_MARKER_RE.search(response)
            if phase2_match:
                after_phase2 = response[phase2_match.end() :]
                m = CODE_BLOCK_RE.search(after_phase2)
                if m:
                    return m.group(1).strip()
        m = CODE_BLOCK_RE.search(response)
        if m:
            return m.group(1).strip()
        logger.warning("No code block found in LLM response, returning raw text")
        return response.strip()

    @staticmethod
    def _extract_phase1(response: str) -> str:
        m = PHASE_1_SECTION_RE.search(response)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_tag(response: str) -> str:
        m = REVERSED_TAG_RE.search(response)
        return m.group(1).strip() if m else ""
