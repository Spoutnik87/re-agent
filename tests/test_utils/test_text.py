"""Tests for text utilities including strip_ghidra_noise."""

from __future__ import annotations

from re_agent.reverse.utils.text import strip_comments, strip_ghidra_noise


def test_strip_ghidra_noise_removes_warning_lines() -> None:
    raw = """\
/* WARNING: Could not resolve jump table */
/* WARNING: Control flow may be inaccurate */
void CTrain::ProcessControl() {
    FuncA();
}
"""
    result = strip_ghidra_noise(raw)
    assert "WARNING" not in result
    assert "void CTrain::ProcessControl" in result
    assert "FuncA" in result


def test_strip_ghidra_noise_removes_line_comment_warnings() -> None:
    raw = """\
// WARNING: Restarted dead code elimination
// WARNING: Bad clean-up
int real_code() { return 42; }
"""
    result = strip_ghidra_noise(raw)
    assert "WARNING" not in result
    assert "int real_code" in result
    assert "42" in result


def test_strip_ghidra_noise_preserves_real_code() -> None:
    raw = """\
/* WARNING: Stack frame may be incomplete */
void __fastcall CEntity::Render(CEntity *this) {
    if (this->m_pModel) {
        this->m_pModel->Draw();
    }
    return;
}
"""
    result = strip_ghidra_noise(raw)
    assert "void __fastcall CEntity::Render" in result
    assert "this->m_pModel->Draw()" in result
    assert "return;" in result
    assert "WARNING" not in result


def test_strip_ghidra_noise_handles_empty_input() -> None:
    assert strip_ghidra_noise("") == ""


def test_strip_ghidra_noise_preserves_blank_lines() -> None:
    raw = "line1\n\nline2\n"
    result = strip_ghidra_noise(raw)
    assert result == "line1\n\nline2"


def test_strip_ghidra_noise_no_warnings_no_change() -> None:
    raw = "void foo() { bar(); }"
    assert strip_ghidra_noise(raw) == raw


def test_strip_comments_removes_comments() -> None:
    """Ensure original strip_comments still works."""
    text = "int x = 1; // comment\n/* block */ int y = 2;"
    result = strip_comments(text)
    assert "int x = 1;" in result
    assert "int y = 2;" in result
    assert "comment" not in result
    assert "block" not in result
