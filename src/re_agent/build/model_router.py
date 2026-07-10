"""Deterministic model escalation router with budget stops (Todo 7).

A pure-data router that layers over the existing PipelineProfile /
classification system and chooses flash / pro / stop based on:

- failure class (syntax compile, type/vtable, parity-red, no-output, budget)
- existing classification (leaf, vtable-heavy, general, ...)
- attempt index and call count
- prompt / completion token budget caps

It does NOT call an LLM and does NOT replace PipelineProfile. It is not yet
wired into the runtime transform pipeline; it is a standalone decision
function that future WorkPacket construction can call.

Design:
- Frozen dataclasses with slots; stdlib-only JSON roundtrip
  (matches work_packet_types.py conventions).
- Validation at the boundary: negative budgets and negative usage are
  rejected at construction time (parse, don't validate).
- Generic names — no project-specific identifiers.
- Deterministic: same input -> same decision, no hidden state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal

__all__ = [
    "FailureClass",
    "RouterBudget",
    "RouterInput",
    "RouterDecision",
    "RouterAction",
    "route",
]

RouterAction = Literal["select_model", "stop"]

# Number of consecutive NO_OUTPUT failures after which the router stops and
# asks for diagnosis instead of burning another call. Tunable, deterministic.
_NO_OUTPUT_STOP_THRESHOLD = 2


class FailureClass(Enum):
    """Classification of the most recent failure for a function/subunit."""

    SYNTAX_COMPILE = "syntax_compile"
    TYPE_OR_VTABLE = "type_or_vtable"
    PARITY_RED = "parity_red"
    NO_OUTPUT = "no_output"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RouterBudget:
    """Hard caps that, once exceeded, force a stop decision.

    A None cap means "unbounded" for that dimension. All non-None caps must
    be non-negative; max_calls must be >= 0 (0 means "no calls allowed").
    """

    max_calls: int
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_calls < 0:
            raise ValueError(f"max_calls must be >= 0, got {self.max_calls}")
        if self.max_prompt_tokens is not None and self.max_prompt_tokens < 0:
            raise ValueError(f"max_prompt_tokens must be >= 0 or None, got {self.max_prompt_tokens}")
        if self.max_completion_tokens is not None and self.max_completion_tokens < 0:
            raise ValueError(f"max_completion_tokens must be >= 0 or None, got {self.max_completion_tokens}")


@dataclass(frozen=True, slots=True)
class RouterInput:
    """Pure-data input to the router.

    ``no_output_streak`` is the count of consecutive NO_OUTPUT failures
    observed so far for this function/subunit. ``attempt_index`` is the
    zero-based index of the attempt that just failed.

    All usage counters must be non-negative; they are validated at the
    boundary (parse, don't validate).
    """

    task_kind: str
    classification: str
    failure_class: FailureClass
    attempt_index: int
    calls_used: int
    prompt_tokens_used: int
    completion_tokens_used: int
    no_output_streak: int
    default_model: str
    escalation_model: str
    block_model: str
    budget: RouterBudget

    def __post_init__(self) -> None:
        if self.attempt_index < 0:
            raise ValueError(f"attempt_index must be >= 0, got {self.attempt_index}")
        if self.calls_used < 0:
            raise ValueError(f"calls_used must be >= 0, got {self.calls_used}")
        if self.prompt_tokens_used < 0:
            raise ValueError(f"prompt_tokens_used must be >= 0, got {self.prompt_tokens_used}")
        if self.completion_tokens_used < 0:
            raise ValueError(f"completion_tokens_used must be >= 0, got {self.completion_tokens_used}")
        if self.no_output_streak < 0:
            raise ValueError(f"no_output_streak must be >= 0, got {self.no_output_streak}")


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """The router's decision for one RouterInput.

    - action == "stop" => model is None, should_retry is False.
    - action == "select_model" => model is a non-empty str, should_retry is True.
    - reason is always a non-empty human-readable string for the WorkPacket report.
    """

    action: RouterAction
    model: str | None
    reason: str
    should_retry: bool

    def __post_init__(self) -> None:
        if self.action == "stop":
            if self.model is not None:
                raise ValueError("stop decision must have model=None")
            if self.should_retry:
                raise ValueError("stop decision must have should_retry=False")
        elif self.action == "select_model":
            if self.model is None or not self.model:
                raise ValueError("select_model decision must have a non-empty model")
            if not self.should_retry:
                raise ValueError("select_model decision must have should_retry=True")
        else:
            raise ValueError(f"unknown action: {self.action!r}")
        if not self.reason:
            raise ValueError("reason must be a non-empty string")

    def to_json_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict for WorkPacket reports."""
        return {
            "action": self.action,
            "model": self.model,
            "reason": self.reason,
            "should_retry": self.should_retry,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, object]) -> RouterDecision:
        return cls(
            action=_opt_str(data.get("action")) or "stop",  # type: ignore[arg-type]
            model=_opt_str(data.get("model")),
            reason=str(data.get("reason", "")),
            should_retry=bool(data.get("should_retry", False)),
        )


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _budget_exceeded(inp: RouterInput) -> str | None:
    """Return a reason string if the budget is exceeded, else None."""
    if inp.calls_used >= inp.budget.max_calls:
        return f"budget exceeded: calls_used={inp.calls_used} >= max_calls={inp.budget.max_calls}"
    b = inp.budget
    if b.max_prompt_tokens is not None and inp.prompt_tokens_used >= b.max_prompt_tokens:
        return (
            f"budget exceeded: prompt_tokens_used={inp.prompt_tokens_used} >= max_prompt_tokens={b.max_prompt_tokens}"
        )
    if b.max_completion_tokens is not None and inp.completion_tokens_used >= b.max_completion_tokens:
        return (
            f"budget exceeded: completion_tokens_used={inp.completion_tokens_used} "
            f">= max_completion_tokens={b.max_completion_tokens}"
        )
    return None


def route(inp: RouterInput) -> RouterDecision:
    """Decide which model to use next, or stop, for the given input.

    Pure and deterministic. Order of precedence (highest first):

    1. BUDGET_EXCEEDED failure class or any budget cap reached -> stop.
    2. Repeated NO_OUTPUT past threshold -> stop (diagnose).
    3. SYNTAX_COMPILE -> block repair model.
    4. TYPE_OR_VTABLE or PARITY_RED -> escalation (pro) model.
    5. NO_OUTPUT (first occurrence) -> default model retry.
    6. UNKNOWN / initial -> default model.
    """
    # 1. Budget cap (explicit class or any counter over the cap).
    if inp.failure_class is FailureClass.BUDGET_EXCEEDED:
        return RouterDecision(
            action="stop",
            model=None,
            reason="budget_exceeded failure class: stop and diagnose",
            should_retry=False,
        )
    budget_reason = _budget_exceeded(inp)
    if budget_reason is not None:
        return RouterDecision(
            action="stop",
            model=None,
            reason=budget_reason,
            should_retry=False,
        )

    # 2. Repeated NO_OUTPUT past the diagnose threshold.
    if inp.failure_class is FailureClass.NO_OUTPUT and inp.no_output_streak >= _NO_OUTPUT_STOP_THRESHOLD:
        return RouterDecision(
            action="stop",
            model=None,
            reason=(
                f"no_output streak={inp.no_output_streak} >= threshold={_NO_OUTPUT_STOP_THRESHOLD}: stop and diagnose"
            ),
            should_retry=False,
        )

    # 3. Syntax / compile failure -> block repair model.
    if inp.failure_class is FailureClass.SYNTAX_COMPILE:
        return RouterDecision(
            action="select_model",
            model=inp.block_model,
            reason="syntax_compile failure: route to block repair model",
            should_retry=True,
        )

    # 4. Type / vtable / parity-red -> escalation (pro) model.
    if inp.failure_class in (FailureClass.TYPE_OR_VTABLE, FailureClass.PARITY_RED):
        return RouterDecision(
            action="select_model",
            model=inp.escalation_model,
            reason=f"{inp.failure_class.value} failure: escalate to pro model",
            should_retry=True,
        )

    # 5. First NO_OUTPUT -> default model retry.
    if inp.failure_class is FailureClass.NO_OUTPUT:
        return RouterDecision(
            action="select_model",
            model=inp.default_model,
            reason="no_output (first occurrence): retry with default model",
            should_retry=True,
        )

    # 6. UNKNOWN / initial attempt -> default model, deterministic.
    return RouterDecision(
        action="select_model",
        model=inp.default_model,
        reason=f"unknown/initial attempt_index={inp.attempt_index}: default model",
        should_retry=True,
    )
