"""Block splitter — decomposes large decompiled functions into self-contained logical blocks.

Each block has balanced braces — they can be concatenated to form the complete
function body.  Blocks are split at top-level (depth-0) control-flow boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTROL_FLOW_KW_RE = re.compile(r"^\s*(if|else\s+if|else|for|while|do|switch|case|default)\b")
RETURN_RE = re.compile(r"^\s*return\b")
LABEL_RE = re.compile(r"^\s*(LAB_[0-9a-fA-F]+)\s*:")
GOTO_RE = re.compile(r"^\s*goto\s+(LAB_[0-9a-fA-F]+)\s*;")

MAX_BLOCK_LINES = 40


def _skip_strings_and_comments(text: str) -> list[tuple[int, str]]:
    """Return (index, char) pairs, replacing string/comment chars with spaces.

    Characters inside C string literals, // comments, and /* */ comments
    are replaced with spaces so brace counting is not thrown off.
    """
    result: list[tuple[int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            result.append((i, " "))
            i += 1
            while i < n:
                if text[i] == "\\":
                    result.append((i, " "))
                    i += 1
                    if i < n:
                        result.append((i, " "))
                        i += 1
                elif text[i] == '"':
                    result.append((i, " "))
                    i += 1
                    break
                else:
                    result.append((i, " "))
                    i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                result.append((i, " "))
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            result.append((i, " "))
            i += 2
            result.append((i - 1, " "))
            while i < n - 1:
                if text[i] == "*" and text[i + 1] == "/":
                    result.append((i, " "))
                    result.append((i + 1, " "))
                    i += 2
                    break
                result.append((i, " "))
                i += 1
            else:
                i = n
        elif ch == "'":
            result.append((i, " "))
            i += 1
            while i < n:
                if text[i] == "\\":
                    result.append((i, " "))
                    i += 1
                    if i < n:
                        result.append((i, " "))
                        i += 1
                elif text[i] == "'":
                    result.append((i, " "))
                    i += 1
                    break
                else:
                    result.append((i, " "))
                    i += 1
        else:
            result.append((i, ch))
            i += 1
    return result


@dataclass
class Block:
    """A self-contained block of decompiled code with balanced braces."""

    id: str
    label: str  # e.g. "entry", "if_0", "else_0", "loop_0", "exit"
    decompiled_text: str  # raw decompiled code for this block
    comment: str = ""


@dataclass
class SplitResult:
    signature: str
    blocks: list[Block]
    total_lines: int
    num_blocks: int


def split_decompiled_function(decompiled: str, max_block_lines: int = MAX_BLOCK_LINES) -> SplitResult:
    """Split decompiled code into self-contained blocks at depth-0 branch points.

    Each block has balanced braces — concatenating all blocks produces the
    complete function body.

    Splitting happens at:
    - ``if`` at depth 0 (starts a new block; ``else``/``else if`` are absorbed into it)
    - ``for``, ``while``, ``do`` at depth 0
    - ``switch`` at depth 0 (``case``/``default`` absorbed)
    - ``return`` at depth 0 (marks the exit block)
    - ``LAB_xxx:`` goto labels at depth 0
    """
    lines = decompiled.splitlines()

    # Find function body: from first { to matching }
    body_start = -1
    depth = 0
    for i, line in enumerate(lines):
        clean = _skip_strings_and_comments(line)
        for _idx, ch in clean:
            if ch == "{":
                if depth == 0:
                    body_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
        if body_start >= 0:
            break

    if body_start < 0:
        return SplitResult(
            signature=decompiled.strip(),
            blocks=[Block(id="b0", label="body", decompiled_text=decompiled.strip())],
            total_lines=len(lines),
            num_blocks=1,
        )

    signature = "\n".join(lines[:body_start]).strip()

    # Clean signature: strip Ghidra boilerplate comments (// ... and /* ... */)
    sig_lines = signature.splitlines()
    cleaned_sig: list[str] = []
    for sl in sig_lines:
        s = sl.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        if not s:
            continue
        cleaned_sig.append(s)
    signature = "\n".join(cleaned_sig).strip()
    # Take only the last meaningful line as the function signature
    if signature:
        sig_parts = signature.splitlines()
        signature = sig_parts[-1].strip()

    # Extract body lines excluding the outer braces
    # The body_start line contains the opening {
    body_lines: list[str] = []
    found_open = False
    depth = 0
    for line in lines[body_start:]:
        clean = _skip_strings_and_comments(line)
        for _idx, ch in clean:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth == 0:
            # Include the closing brace line and stop
            body_lines.append(line)
            break
        elif found_open:
            body_lines.append(line)

    # Remove the outer braces from first and last lines
    if body_lines:
        first = body_lines[0]
        # Remove leading { from first line
        stripped_first = first.replace("{", "", 1).strip()
        if stripped_first:
            body_lines[0] = stripped_first
        else:
            body_lines.pop(0)

        if body_lines:
            last = body_lines[-1]
            # Remove trailing } from last line
            stripped_last = last.replace("}", "", 1).strip()
            if stripped_last:
                body_lines[-1] = stripped_last
            else:
                body_lines.pop()

    if not body_lines:
        return SplitResult(
            signature=signature,
            blocks=[],
            total_lines=len(lines),
            num_blocks=0,
        )

    # Split body lines into blocks at depth-0 control flow boundaries
    blocks: list[Block] = []
    current_lines: list[str] = []
    current_label = "entry"
    block_idx = 0
    branch_idx = 0
    loop_idx = 0
    switch_idx = 0
    depth = 0

    def flush() -> None:
        nonlocal block_idx
        if not current_lines:
            return
        text = "\n".join(current_lines).strip()
        if text:
            blocks.append(
                Block(
                    id=f"b{block_idx}",
                    label=current_label,
                    decompiled_text=text,
                )
            )
            block_idx += 1
        current_lines.clear()

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            if current_lines:
                current_lines.append(line)
            continue

        # Track brace depth changes for this line (skip strings/comments)
        clean = _skip_strings_and_comments(line)
        opens = sum(1 for _, c in clean if c == "{")
        closes = sum(1 for _, c in clean if c == "}")
        new_depth = depth + opens - closes

        cf_match = CONTROL_FLOW_KW_RE.match(line)
        ret_match = RETURN_RE.match(line)
        lbl_match = LABEL_RE.match(line)

        kw_lower = cf_match.group(1).lower() if cf_match else ""

        # else/else-if are absorbed into their preceding if block — never split
        is_absorbed = depth == 0 and kw_lower in ("else", "else if", "case", "default")

        is_split_point = depth == 0 and (cf_match or ret_match or lbl_match) and not is_absorbed and new_depth != depth
        # Also split on simple return at depth 0 (no braces)
        if depth == 0 and ret_match and opens == 0 and closes == 0:
            is_split_point = True

        if is_split_point:
            flush()
            if cf_match:
                kw = cf_match.group(1)
                if kw in ("if",):
                    current_label = f"branch_{branch_idx}"
                    branch_idx += 1
                elif kw in ("for", "while", "do"):
                    current_label = f"loop_{loop_idx}"
                    loop_idx += 1
                elif kw in ("switch",):
                    current_label = f"switch_{switch_idx}"
                    switch_idx += 1
            elif lbl_match:
                current_label = f"label_{lbl_match.group(1)}"
            elif ret_match:
                current_label = "exit"

        current_lines.append(line)
        depth = new_depth

        # If block grows too large, split at a safe midpoint (balanced braces)
        if len(current_lines) >= max_block_lines:
            split_at = _safe_midpoint_split(current_lines)
            if split_at > 10:
                block_lines = current_lines[:split_at]
                text = "\n".join(block_lines).strip()
                if text:
                    blocks.append(
                        Block(
                            id=f"b{block_idx}",
                            label=current_label,
                            decompiled_text=text,
                        )
                    )
                    block_idx += 1
                current_lines = current_lines[split_at:]
                current_label = f"part_{branch_idx}"
                branch_idx += 1

    flush()

    # Link blocks and generate comments
    for i, b in enumerate(blocks):
        parts = []
        if i > 0:
            parts.append("after " + blocks[i - 1].label)
        if i + 1 < len(blocks):
            parts.append("before " + blocks[i + 1].label)
        b.comment = "; ".join(parts) if parts else "standalone"

    return SplitResult(
        signature=signature,
        blocks=blocks,
        total_lines=len(lines),
        num_blocks=len(blocks),
    )


def _safe_midpoint_split(lines: list[str]) -> int:
    """Split a large block at the approximate midpoint where braces are balanced.

    Finds the first point after the midpoint where cumulative brace depth
    returns to 0, ensuring both halves are self-contained.
    """
    if len(lines) < 20:
        return -1

    midpoint = len(lines) // 2
    depth = 0
    for i, line in enumerate(lines):
        depth += line.count("{") - line.count("}")
        stripped = line.rstrip()
        code_part = stripped.split("//")[0].rstrip()
        if i >= midpoint and depth == 0 and (code_part.endswith(";") or code_part.endswith("}")):
            return i + 1
    return -1


def decompiled_line_count(decompiled: str) -> int:
    return sum(1 for line in decompiled.splitlines() if line.strip())


def build_skeleton(decompiled: str, split: SplitResult) -> str:
    """Build a skeleton (signature + locals + block placeholders) from split result.

    Parses the decompiled code to extract local variable declarations,
    uses the split result's signature, and creates block placeholders
    matching the splitter's block IDs.
    """
    lines = decompiled.splitlines()

    # Extract local variable declarations (lines in the body before first real statement)
    # These are lines that look like type + name + semicolon between the opening brace
    # and the first block's decompiled text.
    locals_list: list[str] = []
    body_start = -1
    depth = 0
    for i, line in enumerate(lines):
        for ch in line:
            if ch == "{":
                if depth == 0:
                    body_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
        if body_start >= 0:
            break

    if body_start >= 0 and split.blocks:
        first_block_text = split.blocks[0].decompiled_text
        first_block_lines = first_block_text.splitlines()
        if first_block_lines:
            first_line = first_block_lines[0].strip()
            for j in range(body_start + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped == first_line or stripped.startswith(first_line.split("{")[0].strip()):
                    break
                if stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                    locals_list.append(lines[j])

    locals_str = "\n".join(locals_list) if locals_list else "    // (no local variables detected)"

    # Build block placeholders
    block_placeholders: list[str] = []
    for b in split.blocks:
        desc = b.label.replace("_", " ").title()
        block_placeholders.append(f"    // BLOCK: {b.id} — {desc} ({b.comment})")
        block_placeholders.append(f"    {{ /* TODO: {b.id} */ }}")
        block_placeholders.append("")

    placeholder_str = "\n".join(block_placeholders)

    signature = split.signature if split.signature else "void unknown_function()"

    return f"""{signature} {{
{locals_str}

{placeholder_str}}}"""


def should_use_block_reversal(decompiled: str, min_lines: int = 100) -> bool:
    return decompiled_line_count(decompiled) >= min_lines


VARIABLE_DECL_RE = re.compile(
    r"^\s*(undefined[0-9]|int|uint|float|double|longdouble|bool|char|short|byte|ushort|"
    r"ulonglong|longlong|int32_t|uint32_t|float10|code\s*\*?)\s+\w+"
)


def extract_variable_context(decompiled: str, signature: str) -> str:
    """Extract just the variable declarations from decompiled code.

    Returns a compact context string (signature + local variable declarations)
    to provide type/variable context to block-level reversers without sending
    the full decompiled function (~80% token savings per block call).
    """
    lines = decompiled.splitlines()

    body_start = -1
    depth = 0
    for i, line in enumerate(lines):
        for ch in line:
            if ch == "{":
                if depth == 0:
                    body_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
        if body_start >= 0:
            break

    if body_start < 0:
        return signature

    decls: list[str] = []
    for j in range(body_start + 1, min(body_start + 50, len(lines))):
        stripped = lines[j].strip()
        if not stripped:
            continue
        if stripped.startswith("//") or stripped.startswith("/*"):
            continue
        if CONTROL_FLOW_KW_RE.match(stripped) or RETURN_RE.match(stripped):
            break
        if VARIABLE_DECL_RE.match(stripped) or (";" in stripped and "(" not in stripped):
            decls.append(lines[j])
        else:
            break

    if decls:
        return f"{signature}\n\n// Local variables:\n" + "\n".join(decls)
    return signature
