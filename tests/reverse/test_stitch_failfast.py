from __future__ import annotations

from re_agent.reverse.orchestrator.block import _stitch, _stitch_is_valid


class _Split:
    """Minimal SplitResult stand-in."""

    def __init__(self, num_blocks: int, signature: str = "") -> None:
        self.num_blocks = num_blocks
        self.signature = signature


def test_stitch_valid_balanced() -> None:
    split = _Split(2, "void f()")
    parts = ["{ int a = 1; }", "{ int b = 2; }"]
    code = _stitch(split, parts)
    assert code
    assert _stitch_is_valid(split, parts) is True


def test_stitch_imbalanced_braces_returns_empty() -> None:
    """Brace imbalance > 2 must hard-fail (return empty string)."""
    split = _Split(1, "void f()")
    parts = ["{ { { { int a = 1;"]
    code = _stitch(split, parts)
    assert code == ""


def test_stitch_count_mismatch_gt_1_returns_empty() -> None:
    """Block count mismatch > 1 must hard-fail."""
    split = _Split(5, "void f()")
    parts = ["a", "b", "c"]
    code = _stitch(split, parts)
    assert code == ""


def test_stitch_count_mismatch_eq_1_warns_but_returns_code() -> None:
    """Block count mismatch of exactly 1 is a warning, not a hard fail."""
    split = _Split(3, "void f()")
    parts = ["{ a; }", "{ b; }"]
    code = _stitch(split, parts)
    assert code != ""
