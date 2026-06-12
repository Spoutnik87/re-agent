"""Block-level reverser — reverses individual blocks within a larger function."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from re_agent.agents.block_splitter import Block
from re_agent.backend.protocol import REBackend
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.utils.templates import render_template

PROMPTS_DIR = Path(__file__).parent / "prompts"
BLOCK_CODE_RE = re.compile(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", re.S)

_response_cache: dict[str, str] = {}


def clear_block_cache() -> None:
    """Clear the block response cache. Call between function reversals."""
    _response_cache.clear()


def generate_variable_mapping(
    decompiled: str,
    class_name: str,
    function_name: str,
    address: str,
    llm: LLMProvider,
    project_description: str = "",
) -> str:
    """Pre-compute a variable name/type mapping for the entire function.

    One pro LLM call produces a mapping that is injected into every block
    reversal, ensuring consistent naming without redundant inference.
    """
    task_prompt = render_template(
        PROMPTS_DIR / "varmap_task.md",
        decompiled=decompiled,
        class_name=class_name,
        function_name=function_name,
        address=address,
    )
    system_prompt = render_template(
        PROMPTS_DIR / "varmap_system.md",
        project_description=project_description,
    )
    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=task_prompt),
    ]
    response = llm.send(messages)
    return response.strip()


class BlockReverserAgent:
    """Reverses individual blocks of a decomposed function."""

    def __init__(self, llm: LLMProvider, project_description: str = "") -> None:
        self.llm = llm
        self._project_description = project_description
        self._system_prompt = render_template(
            PROMPTS_DIR / "block_reverser_system.md",
            project_description=project_description,
        )
        self._conversation_id: str | None = None
        self.last_prompt: str = ""
        self.last_response: str = ""

    def reset_conversation(self) -> None:
        """Reset the conversation state. Call between fix rounds to avoid token bloat."""
        self._conversation_id = None

    def reverse_block(
        self,
        block: Block,
        class_name: str,
        function_name: str,
        address: str,
        full_decompiled: str = "",
        var_mapping: str = "",
        reversed_blocks: dict[str, str] | None = None,
    ) -> str:
        """Reverse a single block of a decomposed function.

        Uses a content-hash cache: identical block+context combinations
        return the cached response, saving tokens during fix loops.
        """
        reversed_text = ""
        if reversed_blocks:
            # Only include the last 3 blocks to avoid O(n²) token growth
            recent = list(reversed_blocks.items())[-3:]
            parts = [f"// BLOCK {bid}:\n{code}" for bid, code in recent]
            reversed_text = "\n\n".join(parts)

        task_prompt = render_template(
            PROMPTS_DIR / "block_reverser_task.md",
            class_name=class_name,
            function_name=function_name,
            address=address,
            block_id=block.id,
            block_label=block.label,
            block_decompiled=block.decompiled_text,
            block_comment=block.comment,
            full_decompiled=full_decompiled or "(same as block — this is the first/only block)",
            var_mapping=var_mapping or "(infer from decompile — no pre-computed mapping)",
            reversed_blocks=reversed_text or "(none yet)",
        )

        # Cache key: hash of block content + context (excludes previously reversed blocks
        # since they change between fix rounds but shouldn't affect the block output)
        cache_key = hashlib.md5(
            f"{self._system_prompt}|{block.id}|{class_name}|{function_name}|{block.decompiled_text}|{full_decompiled}|{var_mapping}".encode()
        ).hexdigest()
        if cache_key in _response_cache:
            return self._extract_block_code(_response_cache[cache_key], block.id)

        self.last_prompt = task_prompt

        # Conversations for <20 blocks: LLM sees previous block context for consistency.
        # Stateless for 20+ blocks: avoids quadratic token growth.
        use_conversation = True  # Always use conversations — manageable for <500 line functions

        if use_conversation:
            if self._conversation_id is None:
                if self.llm.supports_conversations:
                    self._conversation_id = self.llm.new_conversation(self._system_prompt)
                else:
                    # Fallback: provider does not support conversations
                    messages = [
                        Message(role="system", content=self._system_prompt),
                        Message(role="user", content=task_prompt),
                    ]
                    response = self.llm.send(messages)
                    self.last_response = response
                    _response_cache[cache_key] = response
                    return self._extract_block_code(response, block.id)
            response = self.llm.resume(self._conversation_id, task_prompt)
        else:
            messages = [
                Message(role="system", content=self._system_prompt),
                Message(role="user", content=task_prompt),
            ]
            response = self.llm.send(messages)

        self.last_response = response
        _response_cache[cache_key] = response
        return self._extract_block_code(response, block.id)

    @staticmethod
    def _extract_block_code(response: str, block_id: str) -> str:
        m = BLOCK_CODE_RE.search(response)
        code = m.group(1).strip() if m else response.strip().strip("`").strip()

        lines = code.splitlines()
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("// BLOCK:") or stripped.startswith("// BLOCK "):
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()


class SkeletonGenerator:
    """Generates a function skeleton (signature + locals + block placeholders)
    from decompiled code."""

    def __init__(self, llm: LLMProvider, backend: REBackend, project_description: str = "") -> None:
        self.llm = llm
        self.backend = backend
        self._system_prompt = render_template(PROMPTS_DIR / "skeleton_system.md")
        self.last_prompt: str = ""
        self.last_response: str = ""

    def generate(
        self,
        decompiled: str,
        class_name: str,
        function_name: str,
        address: str,
    ) -> str:
        """Generate a skeleton from decompiled code.

        Returns:
            Skeleton C++ code with ``{ /* TODO */ }`` block placeholders.
        """
        structs_text = ""
        caps = self.backend.capabilities
        if caps.has_structs and class_name:
            try:
                struct = self.backend.get_struct(class_name)
                if struct:
                    structs_text = f"{struct.name} (size: {struct.size})\n"
                    structs_text += "\n".join(
                        f"  +0x{f.offset:X} {f.type_str} {f.name} (size: {f.size})"
                        for f in struct.fields
                    )
            except Exception:
                structs_text = "Unavailable"

        task_prompt = render_template(
            PROMPTS_DIR / "skeleton_task.md",
            class_name=class_name,
            function_name=function_name,
            address=address,
            decompiled=decompiled,
            structs=structs_text or "None",
        )

        self.last_prompt = task_prompt
        messages = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=task_prompt),
        ]
        response = self.llm.send(messages)
        self.last_response = response

        return self._extract_skeleton(response)

    @staticmethod
    def _extract_skeleton(response: str) -> str:
        m = BLOCK_CODE_RE.search(response)
        if m:
            return m.group(1).strip()
        return response.strip()
