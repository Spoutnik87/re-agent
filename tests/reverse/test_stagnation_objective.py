from __future__ import annotations

from re_agent.reverse.core.models import CheckerVerdict, ObjectiveVerdict, Verdict
from re_agent.reverse.orchestrator.stagnation import StagnationTracker


def _pass_cv() -> CheckerVerdict:
    return CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[])


def _fail_ov() -> ObjectiveVerdict:
    return ObjectiveVerdict(verdict=Verdict.FAIL, summary="bad", findings=["call count mismatch"])


def _pass_ov() -> ObjectiveVerdict:
    return ObjectiveVerdict(verdict=Verdict.PASS, summary="ok", findings=[])


def test_objective_fail_stagnation_triggers_after_3_rounds() -> None:
    """Even when checker says PASS, if objective stays FAIL for 3 rounds,
    the loop must stagnate (>=2 rounds since last change)."""
    tracker = StagnationTracker()
    # Round 1: establish state
    assert tracker.update(_pass_cv(), _fail_ov()) is False
    # Round 2: first repeat of same state
    assert tracker.update(_pass_cv(), _fail_ov()) is False
    # Round 3: second repeat — stagnation
    assert tracker.update(_pass_cv(), _fail_ov()) is True


def test_objective_recovery_resets_stagnation() -> None:
    """If objective goes from FAIL to PASS, stagnation counter resets."""
    tracker = StagnationTracker()
    tracker.update(_pass_cv(), _fail_ov())
    # Objective now passes
    assert tracker.update(_pass_cv(), _pass_ov()) is False
    # Need 2 more rounds of same state to stagnate
    assert tracker.update(_pass_cv(), _pass_ov()) is False
    assert tracker.update(_pass_cv(), _pass_ov()) is True


def test_checker_only_stagnation_still_works() -> None:
    """When objective is None (disabled), checker-only stagnation still works."""
    tracker = StagnationTracker()
    cv_fail = CheckerVerdict(verdict=Verdict.FAIL, summary="bad", issues=["x"])
    assert tracker.update(cv_fail, None) is False
    assert tracker.update(cv_fail, None) is False
    assert tracker.update(cv_fail, None) is True
