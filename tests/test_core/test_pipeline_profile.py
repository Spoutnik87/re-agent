from __future__ import annotations

from re_agent.core.models import PipelineProfile, profile_for


def test_leaf_profile_skips_all_extras() -> None:
    p = profile_for("leaf")
    assert p.max_rounds == 1
    assert p.enable_phase1 is False
    assert p.inject_source_context is False
    assert p.inject_few_shot is False
    assert p.use_objective_verifier is False
    assert p.few_shot_max_examples == 0


def test_getter_setter_profile_matches_leaf() -> None:
    p = profile_for("getter-setter")
    assert p.max_rounds == 1
    assert p.enable_phase1 is False
    assert p.inject_source_context is False
    assert p.inject_few_shot is False
    assert p.use_objective_verifier is False
    assert p.few_shot_max_examples == 0


def test_win32_profile() -> None:
    p = profile_for("win32-api")
    assert p.max_rounds == 2
    assert p.enable_phase1 is True
    assert p.inject_source_context is False
    assert p.inject_few_shot is True
    assert p.use_objective_verifier is True
    assert p.few_shot_max_examples == 2


def test_vtable_heavy_profile_has_more_rounds_and_examples() -> None:
    p = profile_for("vtable-heavy")
    assert p.max_rounds == 5
    assert p.enable_phase1 is True
    assert p.inject_source_context is True
    assert p.inject_few_shot is True
    assert p.use_objective_verifier is True
    assert p.few_shot_max_examples == 3


def test_complex_state_machine_profile() -> None:
    p = profile_for("complex-state-machine")
    assert p.max_rounds == 2
    assert p.enable_phase1 is True
    assert p.inject_source_context is True
    assert p.use_objective_verifier is True


def test_general_profile() -> None:
    p = profile_for("general")
    assert p.max_rounds == 4
    assert p.enable_phase1 is True
    assert p.inject_source_context is True
    assert p.inject_few_shot is True
    assert p.use_objective_verifier is True
    assert p.few_shot_max_examples == 2


def test_unknown_classification_falls_back_to_general() -> None:
    p = profile_for("unknown-type")
    assert p.max_rounds == 4
    assert p.inject_few_shot is True
    assert p.enable_phase1 is True
    assert p.use_objective_verifier is True
