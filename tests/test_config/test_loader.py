"""Tests for config loading."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from re_agent.config.loader import load_config
from re_agent.config.schema import ReAgentConfig


def test_load_default_config() -> None:
    config = load_config(None)
    assert isinstance(config, ReAgentConfig)
    assert config.llm.provider == "openai-compat"
    assert config.reverse.backend.type == "ghidra-bridge"
    assert config.reverse.orchestrator.max_review_rounds == 4
    assert config.reverse.orchestrator.objective_verifier_enabled is True
    assert config.reverse.orchestrator.optimize is True


def test_load_from_yaml(sample_config_path: Path) -> None:
    config = load_config(sample_config_path)
    assert config.reverse.project_profile.stub_call_prefix == "plugin::Call"
    assert config.llm.model == "claude-sonnet-4-5-20250929"
    assert config.reverse.parity.call_count_warn_diff == 3


def test_build_config_defaults_when_no_build_section(sample_config_path: Path) -> None:
    """When YAML has no 'build:' section, BuildConfig uses defaults."""
    config = load_config(sample_config_path)
    build = config.build
    assert build.output.language == "cpp"
    assert build.output.standard == "c++23"
    assert build.output.compiler == "g++"
    assert build.output.target_dir == "output/"
    assert build.output.work_dir == "."
    assert build.modules.min_cluster_size == 20
    assert build.modules.max_cluster_size == 300
    assert build.optimization.subunit_size == 10
    assert build.optimization.cache_enabled is True
    assert build.validation.compile_per_function is True
    assert build.validation.max_compile_retries == 2
    assert build.resume.enabled is True
    assert build.resume.state_path == "cr-agent-state.json"


def test_build_config_from_yaml() -> None:
    """Build section is loaded from YAML with all nested fields."""
    yaml_text = """
build:
  input:
    decompiled_dir: "output/decompiled/"
    ghidra_exports: ".exports/"
  output:
    language: "c"
    standard: "c11"
    compiler: "clang"
    compiler_flags: "-std=c11 -Wall"
    target_dir: "build/"
    work_dir: "scratch/"
  project:
    name: "MyGame"
    description: "A game project"
    conventions:
      naming:
        classes: "snake_case"
        functions: "snake_case"
        globals: "UPPER_CASE"
      includes: "all_includes_with_guards"
      max_function_lines: 100
  modules:
    expected:
      - "Core"
      - "Graphics"
    min_cluster_size: 10
    max_cluster_size: 500
  optimization:
    subunit_size: 5
    context_window: 2
    cache_enabled: false
    cache_path: "custom-cache.json"
  validation:
    compile_per_function: false
    compile_per_module: false
    max_compile_retries: 0
  resume:
    enabled: false
    state_path: "custom-state.json"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        config = load_config(yaml_path)
        build = config.build

        assert build.input.decompiled_dir == "output/decompiled/"
        assert build.input.ghidra_exports == ".exports/"
        assert build.output.language == "c"
        assert build.output.standard == "c11"
        assert build.output.compiler == "clang"
        assert build.output.target_dir == "build/"
        assert build.output.work_dir == "scratch/"
        assert build.project.name == "MyGame"
        assert build.project.description == "A game project"
        assert build.project.conventions.naming.classes == "snake_case"
        assert build.project.conventions.naming.functions == "snake_case"
        assert build.project.conventions.naming.globals == "UPPER_CASE"
        assert build.project.conventions.includes == "all_includes_with_guards"
        assert build.project.conventions.max_function_lines == 100
        assert build.modules.expected == ["Core", "Graphics"]
        assert build.modules.min_cluster_size == 10
        assert build.modules.max_cluster_size == 500
        assert build.optimization.subunit_size == 5
        assert build.optimization.context_window == 2
        assert build.optimization.cache_enabled is False
        assert build.optimization.cache_path == "custom-cache.json"
        assert build.validation.compile_per_function is False
        assert build.validation.compile_per_module is False
        assert build.validation.max_compile_retries == 0
        assert build.resume.enabled is False
        assert build.resume.state_path == "custom-state.json"
    finally:
        yaml_path.unlink(missing_ok=True)


def test_build_config_partial_yaml() -> None:
    """Partial build section gets defaults for missing fields."""
    yaml_text = """
build:
  output:
    work_dir: "partial_build/"
  project:
    name: "Partial"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        config = load_config(yaml_path)
        build = config.build

        assert build.output.work_dir == "partial_build/"
        assert build.output.language == "cpp"
        assert build.output.standard == "c++23"
        assert build.project.name == "Partial"
        assert build.project.description == ""
        assert build.modules.min_cluster_size == 20
        assert build.optimization.cache_enabled is True
        assert build.validation.max_compile_retries == 2
    finally:
        yaml_path.unlink(missing_ok=True)


def test_cli_overrides() -> None:
    config = load_config(None, cli_overrides={"llm.provider": "openai", "reverse.orchestrator.max_review_rounds": "6"})
    assert config.llm.provider == "openai"
    assert config.reverse.orchestrator.max_review_rounds == 6


def test_cli_override_optimize() -> None:
    """CLI override for optimize should toggle the flag."""
    config = load_config(None, cli_overrides={"reverse.orchestrator.optimize": "false"})
    assert config.reverse.orchestrator.optimize is False
    config2 = load_config(None, cli_overrides={"reverse.orchestrator.optimize": "true"})
    assert config2.reverse.orchestrator.optimize is True


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RE_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RE_AGENT_LLM_MODEL", "gpt-4o")
    config = load_config(None)
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"
