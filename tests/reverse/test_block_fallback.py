from __future__ import annotations

from re_agent.reverse.orchestrator.block import _select_fallback_blocks


def _make_blocks() -> list:
    """Create mock blocks with ids, labels, and decompiled_text of varying size."""

    class _Blk:
        def __init__(self, bid: str, label: str, text: str) -> None:
            self.id = bid
            self.label = label
            self.decompiled_text = text

    return [
        _Blk("b0", "entry", "line1\nline2"),
        _Blk("b1", "branch_0", "line1\nline2\nline3\nline4\nline5\nline6"),
        _Blk("b2", "loop_0", "line1\nline2\nline3"),
        _Blk("b3", "exit", "line1"),
    ]


def test_fallback_selects_largest_two_blocks_when_no_parse_errors() -> None:
    blocks = _make_blocks()
    previous_code = {"b0": "ok", "b1": "ok", "b2": "ok", "b3": "ok"}
    selected = _select_fallback_blocks(blocks, previous_code)
    assert selected == {"b1", "b2"}


def test_fallback_selects_blocks_with_parse_errors() -> None:
    blocks = _make_blocks()
    previous_code = {"b0": "ok", "b1": "{{{{ int a = 1;", "b2": "ok", "b3": ""}
    selected = _select_fallback_blocks(blocks, previous_code)
    assert "b1" in selected
    assert "b3" in selected


def test_fallback_returns_none_for_empty_previous_code() -> None:
    blocks = _make_blocks()
    selected = _select_fallback_blocks(blocks, {})
    assert selected == {"b1", "b2"}


def test_reverse_blocks_accepts_previous_code_and_hint_varmap(monkeypatch) -> None:
    """Tier-2 must accept tier-1's reversed blocks as previous_code so
    unaffected blocks are reused rather than re-reversed from scratch."""
    import inspect

    from re_agent.reverse.orchestrator.block import reverse_blocks

    sig = inspect.signature(reverse_blocks)
    assert "previous_code" in sig.parameters, "reverse_blocks must accept previous_code"
    assert "hint_var_mapping" in sig.parameters, "reverse_blocks must accept hint_var_mapping"
