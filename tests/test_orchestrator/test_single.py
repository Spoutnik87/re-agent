"""Tests for single function orchestrator."""
from __future__ import annotations

from pathlib import Path

import pytest

from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionTarget


def test_dry_run_smoke() -> None:
    """Smoke test that config + target creation works."""
    config = ReAgentConfig.create_default()
    target = FunctionTarget(
        address="0x6F86A0",
        class_name="CTrain",
        function_name="ProcessControl",
    )
    assert target.address == "0x6F86A0"
    assert config.orchestrator.max_review_rounds == 4


def test_reverse_single_passes_optimize_to_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """reverse_single should pass config.orchestrator.optimize to run_fix_loop."""
    from re_agent.backend.stub import StubBackend
    from re_agent.orchestrator.single import reverse_single

    config = ReAgentConfig.create_default()
    config.output.report_dir = str(tmp_path / "reports")
    config.output.log_dir = str(tmp_path / "logs")

    # Capture the call to run_fix_loop
    called_kwargs = {}

    def fake_run_fix_loop(**kwargs: object) -> object:
        nonlocal called_kwargs
        called_kwargs = kwargs
        from re_agent.core.models import CheckerVerdict, ReversalResult, Verdict
        return ReversalResult(
            target=kwargs["target"],
            code="code",
            checker_verdict=CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[]),
            rounds_used=1,
            success=True,
        )

    monkeypatch.setattr(
        "re_agent.orchestrator.single.run_fix_loop",
        fake_run_fix_loop,
    )

    class FakeLLM:
        supports_conversations = False
    class FakeSession:
        def record_result(self, *args: object, **kwargs: object) -> None: pass

    reverse_single(
        target=FunctionTarget(address="0x123", class_name="CTest", function_name="func"),
        config=config,
        backend=StubBackend(),
        llm=FakeLLM(),
        session=FakeSession(),
    )

    assert "optimize" in called_kwargs
    # Default config should have optimize=True
    assert called_kwargs["optimize"] is True


def test_reverse_single_respects_optimize_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """reverse_single should pass optimize=False when config says so."""
    from re_agent.backend.stub import StubBackend
    from re_agent.orchestrator.single import reverse_single

    config = ReAgentConfig.create_default()
    config.orchestrator.optimize = False
    config.output.report_dir = str(tmp_path / "reports")
    config.output.log_dir = str(tmp_path / "logs")

    called_kwargs = {}

    def fake_run_fix_loop(**kwargs: object) -> object:
        nonlocal called_kwargs
        called_kwargs = kwargs
        from re_agent.core.models import CheckerVerdict, ReversalResult, Verdict
        return ReversalResult(
            target=kwargs["target"],
            code="code",
            checker_verdict=CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[]),
            rounds_used=1,
            success=True,
        )

    monkeypatch.setattr(
        "re_agent.orchestrator.single.run_fix_loop",
        fake_run_fix_loop,
    )

    class FakeLLM:
        supports_conversations = False
    class FakeSession:
        def record_result(self, *args: object, **kwargs: object) -> None: pass

    reverse_single(
        target=FunctionTarget(address="0x123", class_name="CTest", function_name="func"),
        config=config,
        backend=StubBackend(),
        llm=FakeLLM(),
        session=FakeSession(),
    )

    assert called_kwargs["optimize"] is False
