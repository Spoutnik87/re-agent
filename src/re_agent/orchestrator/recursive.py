"""Recursive decomposition for very large functions (>200 lines).

When a function or block exceeds the comfortable reversal size, asks the LLM
to identify logical sub-sections, then reverses each sub-section independently
and stitches them together.
"""

from __future__ import annotations

import re
from pathlib import Path

from re_agent.agents.block_reverser import BlockReverserAgent
from re_agent.agents.block_splitter import Block, decompiled_line_count
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.utils.templates import render_template

PROMPTS_DIR = Path(__file__).parent.parent / "agents" / "prompts"

DECOMPOSE_PLAN_RE = re.compile(
    r"-\s*(\w+)\s*\[lines?\s*(\d+)\s*-\s*(\d+)\]\s*:\s*(.+)",
    re.I,
)

RECURSIVE_THRESHOLD = 200  # Lines above which recursive decomposition is used
BLOCK_RECURSIVE_THRESHOLD = 60  # Lines above which a single block is recursively split


class RecursiveDecomposer:
    """Decomposes very large functions/blocks into sub-sections using the LLM,
    reverses each sub-section independently, then stitches them back together.
    """

    def __init__(self, llm: LLMProvider, block_llm: LLMProvider | None = None, project_description: str = "") -> None:
        self.llm = llm
        self._project_description = project_description
        _block_llm = block_llm if block_llm is not None else llm
        self._system_prompt = render_template(
            PROMPTS_DIR / "decompose_system.md",
            project_description=project_description,
        )
        self._block_agent = BlockReverserAgent(_block_llm, project_description=project_description)

    def should_split_block(self, block: Block) -> bool:
        return decompiled_line_count(block.decompiled_text) >= BLOCK_RECURSIVE_THRESHOLD

    def decompose_and_reverse(
        self,
        decompiled: str,
        class_name: str,
        function_name: str,
        address: str,
    ) -> str:
        """Decompose a large function into sub-sections, reverse each, and stitch.

        Returns the complete reversed code.
        """
        line_count = decompiled_line_count(decompiled)

        # Step 1: Get decomposition plan from LLM
        plan = self._get_decomposition_plan(decompiled, line_count)
        if not plan:
            # Fallback: reverse entire function as a single block
            block = Block(
                id="b0",
                label="entry",
                decompiled_text=decompiled,
                comment="Full function (decomposition failed)",
            )
            return self._block_agent.reverse_block(
                block=block,
                class_name=class_name,
                function_name=function_name,
                address=address,
                full_decompiled="",
                reversed_blocks={},
            )

        lines = decompiled.splitlines()
        sub_sections: list[tuple[str, str, str]] = []  # (section_id, description, decompiled_text)

        for section_id, start, end, desc in plan:
            start_idx = max(0, start - 1)
            end_idx = min(len(lines), end)
            section_text = "\n".join(lines[start_idx:end_idx])
            sub_sections.append((section_id, desc, section_text))

        # Step 2: Reverse each sub-section
        reversed_parts: list[str] = []
        reversed_context: dict[str, str] = {}

        for section_id, desc, section_text in sub_sections:
            # Create a synthetic block for this sub-section
            block = Block(
                id=section_id,
                label=section_id,
                decompiled_text=section_text,
                comment=f"Sub-section: {desc}",
            )

            # Use the block reverser to reverse this sub-section
            block_code = self._block_agent.reverse_block(
                block=block,
                class_name=class_name,
                function_name=function_name,
                address=address,
                full_decompiled="",
                reversed_blocks=reversed_context,
            )
            reversed_parts.append(block_code)
            reversed_context[section_id] = block_code

        return "\n".join(reversed_parts)

    def _get_decomposition_plan(self, decompiled: str, total_lines: int) -> list[tuple[str, int, int, str]]:
        """Ask the LLM to produce a decomposition plan.

        Returns list of (section_id, start_line, end_line, description).
        """
        task_prompt = render_template(
            PROMPTS_DIR / "decompose_task.md",
            decompiled=decompiled,
            total_lines=str(total_lines),
        )

        messages = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=task_prompt),
        ]
        response = self.llm.send(messages)

        plan: list[tuple[str, int, int, str]] = []
        for match in DECOMPOSE_PLAN_RE.finditer(response):
            section_id = match.group(1)
            try:
                start = int(match.group(2))
                end = int(match.group(3))
            except ValueError:
                continue
            desc = match.group(4).strip()
            plan.append((section_id, start, end, desc))

        return plan


def should_use_recursive(decompiled: str) -> bool:
    return decompiled_line_count(decompiled) >= RECURSIVE_THRESHOLD
