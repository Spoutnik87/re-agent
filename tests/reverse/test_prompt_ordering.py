from __future__ import annotations

from pathlib import Path

from re_agent.reverse.utils.templates import render_template

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "src" / "re_agent" / "reverse" / "agents" / "prompts"


def test_reverser_task_decompile_precedes_target() -> None:
    """Decompile (stable) must appear before class/fn/addr (variable) so the
    DeepSeek disk cache can hit on the stable prefix across rounds."""
    rendered = render_template(
        PROMPTS_DIR / "reverser_task.md",
        class_name="Cls",
        function_name="fn",
        address="0x1000",
        decompiled="DECOMPILE_BODY",
    )
    decompile_pos = rendered.index("DECOMPILE_BODY")
    target_pos = rendered.index("0x1000")
    assert decompile_pos < target_pos, "decompile must precede the target address (stable prefix first)"


def test_checker_task_decompile_precedes_reversed_code() -> None:
    """Decompile (stable across rounds) must precede reversed_code (variable)."""
    rendered = render_template(
        PROMPTS_DIR / "checker_task.md",
        class_name="Cls",
        function_name="fn",
        address="0x1000",
        reversed_code="REVERSED_BODY",
        decompiled="DECOMPILE_BODY",
    )
    decompile_pos = rendered.index("DECOMPILE_BODY")
    reversed_pos = rendered.index("REVERSED_BODY")
    assert decompile_pos < reversed_pos, "decompile must precede reversed code (stable prefix first)"


def test_fix_instructions_has_decompile_and_prior_code_placeholders() -> None:
    """The fix prompt must include decompile + prior_code in the stable prefix
    so the model can see what it's fixing (W3 fix) and DeepSeek can cache it."""
    text = (PROMPTS_DIR / "fix_instructions.md").read_text(encoding="utf-8")
    assert "${decompiled}" in text, "fix_instructions.md must reference decompiled"
    assert "${prior_code}" in text, "fix_instructions.md must reference prior_code"
