"""Tests for the behavioral differential-testing harness."""

from __future__ import annotations

from re_agent.verify.runner import RunInputs, RunResult


def test_run_result_equality_by_value_and_writes():
    a = RunResult(return_value=7, writes={"0x10": b"\x01"})
    b = RunResult(return_value=7, writes={"0x10": b"\x01"})
    assert a == b


def test_run_inputs_are_hashable_for_dedup():
    i = RunInputs(args=(1, 2), memory={"0x10": b"\x00"})
    assert i.key() == RunInputs(args=(1, 2), memory={"0x10": b"\x00"}).key()


def test_identical_results_are_equivalent():
    from re_agent.verify.differential import compare_runs

    v = compare_runs(RunResult(1, {"0x4": b"\xaa"}), RunResult(1, {"0x4": b"\xaa"}))
    assert v.equivalent is True
    assert v.reason == ""


def test_return_value_mismatch_diverges():
    from re_agent.verify.differential import compare_runs

    v = compare_runs(RunResult(1), RunResult(2))
    assert v.equivalent is False
    assert "return value" in v.reason


def test_memory_write_mismatch_diverges():
    from re_agent.verify.differential import compare_runs

    v = compare_runs(RunResult(0, {"0x4": b"\x01"}), RunResult(0, {"0x4": b"\x02"}))
    assert v.equivalent is False
    assert "0x4" in v.reason


class _Echo:
    def run(self, inputs: RunInputs) -> RunResult:
        return RunResult(return_value=sum(inputs.args))


class _Off:
    def run(self, inputs: RunInputs) -> RunResult:
        return RunResult(return_value=sum(inputs.args) + 1)


def test_run_differential_all_equivalent():
    from re_agent.verify.differential import run_differential

    vectors = [RunInputs(args=(1, 2)), RunInputs(args=(3, 4))]
    report = run_differential(_Echo(), _Echo(), vectors)
    assert report.equivalent is True
    assert report.checked == 2
    assert report.first_divergence is None


def test_run_differential_reports_first_divergence():
    from re_agent.verify.differential import run_differential

    vectors = [RunInputs(args=(0, 0)), RunInputs(args=(5, 5))]
    report = run_differential(_Echo(), _Off(), vectors)
    assert report.equivalent is False
    assert report.first_divergence == vectors[0]


def test_persist_differential_verdict_equivalent(tmp_path):
    from re_agent.state.function_state import FunctionStateStore
    from re_agent.verify.differential import DiffReport, persist_differential_verdict

    path = tmp_path / "functions.json"
    report = DiffReport(equivalent=True, checked=4)
    persist_differential_verdict("0x1000", report, store_path=path)

    store = FunctionStateStore(path)
    rec = store.get("0x1000")
    assert rec is not None
    assert rec.behavioral == "equivalent"


def test_persist_differential_verdict_divergent(tmp_path):
    from re_agent.state.function_state import FunctionStateStore
    from re_agent.verify.differential import DiffReport, persist_differential_verdict

    path = tmp_path / "functions.json"
    report = DiffReport(equivalent=False, checked=3, reason="return value differs")
    persist_differential_verdict("0x2000", report, store_path=path)

    store = FunctionStateStore(path)
    rec = store.get("0x2000")
    assert rec is not None
    assert rec.behavioral == "divergent"
