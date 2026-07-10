"""Tests for the deterministic model escalation router (Todo 7).

Pure-data, no LLM/provider calls. Validates that the router chooses
flash/pro/stop deterministically from failure class, classification,
attempt index, and budget, and that decisions serialize to JSON.

Follows Given/When/Then naming.
"""

from __future__ import annotations

import json

import pytest

from re_agent.build.model_router import (
    FailureClass,
    RouterBudget,
    RouterDecision,
    RouterInput,
    route,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "flash-default"
_ESCALATION_MODEL = "pro-escalation"
_BLOCK_MODEL = "block-repair"


def _budget(
    max_calls: int = 10,
    max_prompt_tokens: int | None = 1_000_000,
    max_completion_tokens: int | None = 100_000,
) -> RouterBudget:
    return RouterBudget(
        max_calls=max_calls,
        max_prompt_tokens=max_prompt_tokens,
        max_completion_tokens=max_completion_tokens,
    )


def _input(
    failure_class: FailureClass = FailureClass.UNKNOWN,
    *,
    classification: str = "general",
    attempt_index: int = 0,
    calls_used: int = 0,
    prompt_tokens_used: int = 0,
    completion_tokens_used: int = 0,
    no_output_streak: int = 0,
    budget: RouterBudget | None = None,
) -> RouterInput:
    return RouterInput(
        task_kind="transform",
        classification=classification,
        failure_class=failure_class,
        attempt_index=attempt_index,
        calls_used=calls_used,
        prompt_tokens_used=prompt_tokens_used,
        completion_tokens_used=completion_tokens_used,
        no_output_streak=no_output_streak,
        default_model=_DEFAULT_MODEL,
        escalation_model=_ESCALATION_MODEL,
        block_model=_BLOCK_MODEL,
        budget=budget if budget is not None else _budget(),
    )


# ---------------------------------------------------------------------------
# 1. syntax compile failure -> flash/block repair model under budget
# ---------------------------------------------------------------------------


def test_selects_block_repair_model_when_syntax_compile_failure_under_budget():
    # Given a syntax_compile failure with budget remaining
    inp = _input(failure_class=FailureClass.SYNTAX_COMPILE, attempt_index=1)

    # When routed
    decision = route(inp)

    # Then it selects the block repair model and retries
    assert decision.action == "select_model"
    assert decision.model == _BLOCK_MODEL
    assert decision.should_retry is True
    assert "syntax" in decision.reason.lower() or "compile" in decision.reason.lower()


def test_selects_block_repair_model_for_syntax_compile_with_vtable_classification():
    # Given a syntax_compile failure classified vtable-heavy
    inp = _input(
        failure_class=FailureClass.SYNTAX_COMPILE,
        classification="vtable-heavy",
        attempt_index=1,
    )

    # When routed
    decision = route(inp)

    # Then syntax_compile still routes to the block repair model (syntax first)
    assert decision.action == "select_model"
    assert decision.model == _BLOCK_MODEL


# ---------------------------------------------------------------------------
# 2. vtable-heavy / type failure -> pro/escalation model
# ---------------------------------------------------------------------------


def test_selects_escalation_model_when_type_or_vtable_failure_under_budget():
    # Given a type_or_vtable failure
    inp = _input(
        failure_class=FailureClass.TYPE_OR_VTABLE,
        classification="vtable-heavy",
        attempt_index=1,
    )

    # When routed
    decision = route(inp)

    # Then it escalates to the pro model
    assert decision.action == "select_model"
    assert decision.model == _ESCALATION_MODEL
    assert decision.should_retry is True


def test_selects_escalation_model_for_type_failure_with_general_classification():
    # Given a type failure on a general-class function
    inp = _input(
        failure_class=FailureClass.TYPE_OR_VTABLE,
        classification="general",
        attempt_index=1,
    )

    # When routed
    decision = route(inp)

    # Then it still escalates (failure class drives the model, not classification)
    assert decision.model == _ESCALATION_MODEL


# ---------------------------------------------------------------------------
# 3. parity-red -> pro/escalation model
# ---------------------------------------------------------------------------


def test_selects_escalation_model_when_parity_red_failure_under_budget():
    # Given a parity_red failure
    inp = _input(failure_class=FailureClass.PARITY_RED, attempt_index=1)

    # When routed
    decision = route(inp)

    # Then it escalates to the pro model
    assert decision.action == "select_model"
    assert decision.model == _ESCALATION_MODEL
    assert decision.should_retry is True
    assert "parity" in decision.reason.lower()


# ---------------------------------------------------------------------------
# 4. repeated NO_OUTPUT -> stop/diagnose, no model
# ---------------------------------------------------------------------------


def test_stops_when_no_output_repeated_past_threshold():
    # Given repeated NO_OUTPUT failures past the diagnose threshold
    inp = _input(
        failure_class=FailureClass.NO_OUTPUT,
        attempt_index=2,
        no_output_streak=2,
    )

    # When routed
    decision = route(inp)

    # Then it stops with no model and does not retry
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False
    assert "no_output" in decision.reason.lower() or "diagnose" in decision.reason.lower()


def test_retries_once_on_first_no_output():
    # Given a first NO_OUTPUT failure
    inp = _input(
        failure_class=FailureClass.NO_OUTPUT,
        attempt_index=0,
        no_output_streak=1,
    )

    # When routed
    decision = route(inp)

    # Then it retries with the default model (not yet a diagnose-stopping streak)
    assert decision.action == "select_model"
    assert decision.model == _DEFAULT_MODEL
    assert decision.should_retry is True


# ---------------------------------------------------------------------------
# 5. budget cap exceeded -> stop, no model
# ---------------------------------------------------------------------------


def test_stops_when_call_budget_exceeded():
    # Given calls_used already at the cap
    inp = _input(calls_used=10, budget=_budget(max_calls=10))

    # When routed
    decision = route(inp)

    # Then it stops with no model
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False
    assert "budget" in decision.reason.lower()


def test_stops_when_prompt_token_budget_exceeded():
    # Given prompt tokens already over the cap
    inp = _input(
        prompt_tokens_used=2_000_000,
        budget=_budget(max_prompt_tokens=1_000_000),
    )

    # When routed
    decision = route(inp)

    # Then it stops with no model
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False


def test_stops_when_completion_token_budget_exceeded():
    # Given completion tokens already over the cap
    inp = _input(
        completion_tokens_used=200_000,
        budget=_budget(max_completion_tokens=100_000),
    )

    # When routed
    decision = route(inp)

    # Then it stops with no model
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False


def test_budget_exceeded_takes_priority_over_failure_class():
    # Given a parity_red failure but budget already exhausted
    inp = _input(
        failure_class=FailureClass.PARITY_RED,
        calls_used=10,
        budget=_budget(max_calls=10),
    )

    # When routed
    decision = route(inp)

    # Then budget stop wins
    assert decision.action == "stop"
    assert decision.model is None


# ---------------------------------------------------------------------------
# 6. Router decisions serialize to JSON/dict
# ---------------------------------------------------------------------------


def test_decision_to_json_dict_roundtrips_through_json():
    # Given a routed decision
    decision = route(_input(failure_class=FailureClass.PARITY_RED, attempt_index=1))

    # When serialized to dict then to JSON text and back
    d = decision.to_json_dict()
    text = json.dumps(d, sort_keys=True)
    parsed = json.loads(text)

    # Then the dict contains the expected keys and stable values
    assert parsed["action"] == "select_model"
    assert parsed["model"] == _ESCALATION_MODEL
    assert parsed["should_retry"] is True
    assert isinstance(parsed["reason"], str) and parsed["reason"]
    # All keys present
    assert set(parsed.keys()) == {"action", "model", "reason", "should_retry"}


def test_stop_decision_serializes_with_null_model():
    # Given a budget-exhausted stop decision
    decision = route(_input(calls_used=10, budget=_budget(max_calls=10)))

    # When serialized
    d = decision.to_json_dict()

    # Then model is null in the JSON dict
    assert d["action"] == "stop"
    assert d["model"] is None
    assert d["should_retry"] is False


# ---------------------------------------------------------------------------
# 7. No LLM / provider calls — pure data
# ---------------------------------------------------------------------------


def test_router_is_pure_deterministic_no_side_effects():
    # Given the same input twice
    inp = _input(failure_class=FailureClass.TYPE_OR_VTABLE, attempt_index=1)

    # When routed twice
    d1 = route(inp)
    d2 = route(inp)

    # Then both decisions are equal (deterministic, no state)
    assert d1 == d2


# ---------------------------------------------------------------------------
# Initial / unknown path — deterministic default
# ---------------------------------------------------------------------------


def test_unknown_failure_initial_attempt_selects_default_model():
    # Given an unknown failure class on the first attempt
    inp = _input(failure_class=FailureClass.UNKNOWN, attempt_index=0)

    # When routed
    decision = route(inp)

    # Then it deterministically selects the default model
    assert decision.action == "select_model"
    assert decision.model == _DEFAULT_MODEL
    assert decision.should_retry is True


def test_budget_exceeded_failure_class_stops_regardless_of_tokens():
    # Given an explicit BUDGET_EXCEEDED failure class
    inp = _input(failure_class=FailureClass.BUDGET_EXCEEDED)

    # When routed
    decision = route(inp)

    # Then it stops with no model
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False


# ---------------------------------------------------------------------------
# Adversarial: malformed input
# ---------------------------------------------------------------------------


def test_negative_budget_rejected():
    # Given a budget with a negative max_calls
    # When constructing it
    with pytest.raises(ValueError):
        RouterBudget(max_calls=-1)


def test_negative_usage_rejected():
    # Given a RouterInput with negative calls_used
    # When constructing it
    with pytest.raises(ValueError):
        RouterInput(
            task_kind="transform",
            classification="general",
            failure_class=FailureClass.UNKNOWN,
            attempt_index=0,
            calls_used=-1,
            prompt_tokens_used=0,
            completion_tokens_used=0,
            no_output_streak=0,
            default_model=_DEFAULT_MODEL,
            escalation_model=_ESCALATION_MODEL,
            block_model=_BLOCK_MODEL,
            budget=_budget(),
        )


def test_negative_no_output_streak_rejected():
    # Given a RouterInput with negative no_output_streak
    # When constructing it
    with pytest.raises(ValueError):
        RouterInput(
            task_kind="transform",
            classification="general",
            failure_class=FailureClass.UNKNOWN,
            attempt_index=0,
            calls_used=0,
            prompt_tokens_used=0,
            completion_tokens_used=0,
            no_output_streak=-1,
            default_model=_DEFAULT_MODEL,
            escalation_model=_ESCALATION_MODEL,
            block_model=_BLOCK_MODEL,
            budget=_budget(),
        )


# ---------------------------------------------------------------------------
# 8. RouterDecision.from_json_dict missing-key behavior (current behavior lock)
# ---------------------------------------------------------------------------


def test_from_json_dict_missing_action_defaults_stop_with_valid_stop_data():
    """Given a JSON dict missing the 'action' key,
    When from_json_dict is called with supporting stop keys,
    Then it defaults to action='stop' with model=None and should_retry=False."""
    # Given
    data: dict[str, object] = {
        "model": None,
        "reason": "fallback stop from missing action",
        "should_retry": False,
    }
    # When
    decision = RouterDecision.from_json_dict(data)
    # Then
    assert decision.action == "stop"
    assert decision.model is None
    assert decision.should_retry is False
    assert decision.reason == "fallback stop from missing action"


def test_from_json_dict_missing_model_defaults_none():
    """Given a JSON dict missing the 'model' key,
    When from_json_dict is called,
    Then model defaults to None."""
    # Given
    data: dict[str, object] = {
        "action": "stop",
        "reason": "no model key",
        "should_retry": False,
    }
    # When
    decision = RouterDecision.from_json_dict(data)
    # Then
    assert decision.model is None
    assert decision.action == "stop"


def test_from_json_dict_missing_reason_raises_valueerror():
    """Given a JSON dict missing the 'reason' key,
    When from_json_dict is called,
    Then ValueError is raised because reason defaults to '' which fails post_init."""
    # Given
    data: dict[str, object] = {
        "action": "stop",
        "model": None,
        "should_retry": False,
    }
    # When / Then
    with pytest.raises(ValueError, match="reason must be a non-empty string"):
        RouterDecision.from_json_dict(data)


def test_from_json_dict_missing_should_retry_defaults_false():
    """Given a JSON dict missing the 'should_retry' key,
    When from_json_dict is called,
    Then should_retry defaults to False."""
    # Given
    data: dict[str, object] = {
        "action": "stop",
        "model": None,
        "reason": "missing should_retry",
    }
    # When
    decision = RouterDecision.from_json_dict(data)
    # Then
    assert decision.should_retry is False
    assert decision.action == "stop"


def test_from_json_dict_empty_dict_raises_valueerror():
    """Given an empty JSON dict,
    When from_json_dict is called,
    Then ValueError is raised (empty reason fails validation)."""
    # Given
    data: dict[str, object] = {}
    # When / Then
    with pytest.raises(ValueError, match="reason must be a non-empty string"):
        RouterDecision.from_json_dict(data)
