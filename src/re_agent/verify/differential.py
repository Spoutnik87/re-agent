from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from re_agent.verify.runner import FunctionRunner, RunInputs, RunResult


@dataclass
class DiffVerdict:
    equivalent: bool
    reason: str = ""


def compare_runs(original: RunResult, candidate: RunResult) -> DiffVerdict:
    if original.return_value != candidate.return_value:
        return DiffVerdict(False, f"return value differs: {original.return_value} != {candidate.return_value}")
    if original.writes.keys() != candidate.writes.keys():
        only = set(original.writes) ^ set(candidate.writes)
        return DiffVerdict(False, f"different memory addresses written: {sorted(only)}")
    for addr, val in original.writes.items():
        if candidate.writes[addr] != val:
            return DiffVerdict(False, f"memory at {addr} differs")
    return DiffVerdict(True)


@dataclass
class DiffReport:
    equivalent: bool
    checked: int
    first_divergence: RunInputs | None = None
    reason: str = ""


def run_differential(
    original: FunctionRunner,
    candidate: FunctionRunner,
    vectors: list[RunInputs],
) -> DiffReport:
    checked = 0
    for vec in vectors:
        checked += 1
        verdict = compare_runs(original.run(vec), candidate.run(vec))
        if not verdict.equivalent:
            return DiffReport(False, checked, first_divergence=vec, reason=verdict.reason)
    return DiffReport(True, checked)


def persist_differential_verdict(
    address: str,
    report: DiffReport,
    *,
    store_path: str | Path = "function-state.json",
) -> None:
    """Persist the behavioral-equivalence verdict into the function-state store."""
    from re_agent.state.function_state import FunctionStateStore

    store = FunctionStateStore(store_path)
    store.update(address, behavioral="equivalent" if report.equivalent else "divergent")
    store.flush()
