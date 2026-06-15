"""Post-reversal renaming pass — cleans up Ghidra variable/type names."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from re_agent.llm.protocol import LLMProvider, Message
from re_agent.reverse.core.models import FunctionTarget
from re_agent.reverse.utils.templates import render_template

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
CODE_BLOCK_RE = re.compile(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", re.S)


class RenameAgent:
    """Post-processes reversed code to replace Ghidra names with clean identifiers."""

    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm
        self._system_prompt = render_template(PROMPTS_DIR / "rename_system.md")

    def rename(self, code: str, class_name: str, function_name: str, address: str) -> str:
        """Rename variables/types in a reversed function.

        Returns the renamed code, or the original if renaming fails.
        """
        task_prompt = render_template(
            PROMPTS_DIR / "rename_task.md",
            code=code,
            class_name=class_name,
            function_name=function_name,
            address=address,
        )

        messages = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=task_prompt),
        ]

        try:
            response = self.llm.send(messages)
            renamed = self._extract_code(response)
            if renamed and len(renamed) > len(code) * 0.3:
                return renamed
            logger.warning(
                "Renamed code too short (%.0f%% of original); keeping original code",
                len(renamed) / len(code) * 100 if code else 0,
            )
        except Exception:
            logger.warning("Rename pass failed; keeping original code", exc_info=True)

        return code

    @staticmethod
    def _extract_code(response: str) -> str:
        m = CODE_BLOCK_RE.search(response)
        if m:
            return m.group(1).strip()
        logger.warning("No code block found in rename response, returning raw text")
        return response.strip()


def run_rename_pass(
    code: str,
    target: FunctionTarget,
    llm: LLMProvider,
) -> str:
    """Run the renaming pass on reversed code.

    Uses the pro model (llm) since renaming requires semantic understanding.
    Returns the renamed code, or the original on failure.
    """
    if not code:
        return code

    agent = RenameAgent(llm)
    return agent.rename(
        code=code,
        class_name=target.class_name,
        function_name=target.function_name,
        address=target.address,
    )
