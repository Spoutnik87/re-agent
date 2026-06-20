from __future__ import annotations

from unittest.mock import MagicMock

from re_agent.reverse.agents.reverser import ReverserAgent


def test_reverser_passes_min_score_to_few_shot_builder() -> None:
    """ReverserAgent must pass few_shot_min_score to find_similar."""
    llm = MagicMock()
    llm.supports_conversations = False
    backend = MagicMock()
    backend.capabilities.has_xrefs = False
    backend.capabilities.has_structs = False
    backend.decompile.return_value = MagicMock(raw_output="void f() {}", address="0x1000")

    builder = MagicMock()
    builder.find_similar.return_value = []

    from re_agent.config.schema import ProjectProfile

    profile = ProjectProfile()
    profile.project_context = ""

    agent = ReverserAgent(
        llm=llm,
        backend=backend,
        source_root=None,
        project_profile=profile,
        optimize=True,
        enable_phase1=False,
        inject_few_shot=True,
        few_shot_max_examples=2,
        few_shot_min_score=4,
    )
    agent._few_shot_builder = builder
    agent.llm.send.return_value = "```cpp\nvoid f() {}\n```"

    from re_agent.reverse.core.models import FunctionTarget

    target = FunctionTarget(address="0x1000", class_name="C", function_name="f")
    agent.reverse(target)

    builder.find_similar.assert_called_once()
    _, kwargs = builder.find_similar.call_args
    assert kwargs.get("min_score") == 4, f"Expected min_score=4, got {kwargs.get('min_score')}"
