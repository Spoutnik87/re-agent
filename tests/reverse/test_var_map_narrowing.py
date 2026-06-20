from __future__ import annotations

from re_agent.reverse.core.models import CheckerVerdict, Verdict
from re_agent.reverse.orchestrator.block import _should_regenerate_varmap


def test_generic_type_mention_does_not_trigger_regen() -> None:
    """A vague 'type' mention must not trigger a full var-map pro call."""
    cv = CheckerVerdict(
        verdict=Verdict.FAIL,
        summary="bad",
        issues=["The return type of the function seems wrong"],
        fix_instructions=["Fix the return type"],
    )
    assert _should_regenerate_varmap(cv) is False


def test_explicit_rename_request_triggers_regen() -> None:
    """An issue that explicitly names a variable to rename triggers regen."""
    cv = CheckerVerdict(
        verdict=Verdict.FAIL,
        summary="bad",
        issues=["Variable 'param_1' should be renamed to 'playerData'"],
        fix_instructions=["Rename param_1 to playerData"],
        naming_issues_explicit=True,
        affected_variables=["param_1"],
    )
    assert _should_regenerate_varmap(cv) is True


def test_undefined_variable_mention_triggers_regen() -> None:
    """An undefined variable reference (uVar2, local_c, etc.) triggers regen."""
    cv = CheckerVerdict(
        verdict=Verdict.FAIL,
        summary="bad",
        issues=["uVar3 is used but not declared in the reversed code"],
        fix_instructions=["Declare uVar3 with the correct type"],
        naming_issues_explicit=True,
        affected_variables=["uVar3"],
    )
    assert _should_regenerate_varmap(cv) is True


def test_control_flow_issue_does_not_trigger_regen() -> None:
    cv = CheckerVerdict(
        verdict=Verdict.FAIL,
        summary="bad",
        issues=["Missing else branch for the if statement at line 15"],
        fix_instructions=["Add the missing else branch"],
    )
    assert _should_regenerate_varmap(cv) is False


def test_empty_issues_does_not_trigger_regen() -> None:
    cv = CheckerVerdict(verdict=Verdict.PASS, summary="ok", issues=[], fix_instructions=[])
    assert _should_regenerate_varmap(cv) is False
