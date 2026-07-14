"""Tests for config loading."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from re_agent.config.loader import load_config
from re_agent.config.schema import ReAgentConfig
from re_agent.contracts import (
    Architecture,
    CallingConvention,
    Symbol,
    manifest_from_symbols,
    save_manifest,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _write_valid_manifest(tmp_path: Path) -> tuple[Path, str]:
    """Create a valid AbiManifest, return (path, raw_sha256_hex).

    The returned *raw_sha256* is the SHA-256 of the file bytes on disk
    — this is what ``abi_manifest_sha256`` stores in the YAML config.
    """
    import hashlib

    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X86,
        pointer_size=4,
        symbols=[
            Symbol(
                address=0x1000,
                name="func_a",
                signature="void func_a()",
                calling_convention=CallingConvention.CDECL,
                output_path="mod_a.cpp",
            ),
        ],
    )
    path = tmp_path / "abi_manifest.json"
    save_manifest(manifest, path)
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, raw_hash


def _contracts_yaml(manifest_path: str, sha256: str) -> str:
    """YAML snippet for a valid contracts section."""
    posix_path = manifest_path.replace("\\", "/")
    return (
        "contracts:\n"
        '  transformation_policy: "preserve_abi"\n'
        f'  abi_manifest_path: "{posix_path}"\n'
        f'  abi_manifest_sha256: "{sha256}"\n'
    )


def _cfg_yaml(yaml_body: str, manifest_path: str, sha256: str) -> str:
    """Wrap *yaml_body* with a contracts section."""
    return yaml_body + "\n" + _contracts_yaml(manifest_path, sha256)


# ── Basic config loading (with valid contracts via fixture) ─────────────


def test_load_from_yaml(sample_config_path: Path) -> None:
    """Full sample config (including contracts) loads successfully."""
    config = load_config(sample_config_path)
    assert config.reverse.project_profile.stub_call_prefix == "plugin::Call"
    assert config.llm.model == "claude-sonnet-4-5-20250929"
    assert config.reverse.parity.call_count_warn_diff == 3
    assert config.contracts.transformation_policy == "preserve_abi"
    assert config.contracts.abi_manifest_path == "test_abi_manifest.json"


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


def test_load_default_config(sample_config_path: Path) -> None:
    """Loading from the sample fixture returns a fully populated config."""
    config = load_config(sample_config_path)
    assert isinstance(config, ReAgentConfig)
    assert config.llm.provider == "claude"
    assert config.reverse.backend.type == "ghidra-bridge"
    assert config.reverse.orchestrator.max_review_rounds == 4
    assert config.reverse.orchestrator.objective_verifier_enabled is True
    assert config.reverse.orchestrator.optimize is True
    assert config.contracts.transformation_policy == "preserve_abi"


# ── Build config loading tests (with valid contracts) ───────────────────


def test_build_config_from_yaml(tmp_path: Path) -> None:
    """Build section is loaded from YAML with all nested fields."""
    manifest_path, manifest_sha = _write_valid_manifest(tmp_path)
    yaml_text = f"""build:
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
{_contracts_yaml(str(manifest_path), manifest_sha)}
"""
    yaml_path = tmp_path / "test_build.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

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
    assert config.contracts.transformation_policy == "preserve_abi"


def test_build_config_partial_yaml(tmp_path: Path) -> None:
    """Partial build section gets defaults for missing fields."""
    manifest_path, manifest_sha = _write_valid_manifest(tmp_path)
    yaml_text = f"""build:
  output:
    work_dir: "partial_build/"
  project:
    name: "Partial"
{_contracts_yaml(str(manifest_path), manifest_sha)}
"""
    yaml_path = tmp_path / "test_partial.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

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
    assert config.contracts.transformation_policy == "preserve_abi"


# ── CLI overrides (via a valid fixture config) ──────────────────────────


def test_cli_overrides(sample_config_path: Path) -> None:
    config = load_config(
        sample_config_path,
        cli_overrides={"llm.provider": "openai", "reverse.orchestrator.max_review_rounds": "6"},
    )
    assert config.llm.provider == "openai"
    assert config.reverse.orchestrator.max_review_rounds == 6


def test_cli_override_optimize(sample_config_path: Path) -> None:
    """CLI override for optimize should toggle the flag."""
    config = load_config(sample_config_path, cli_overrides={"reverse.orchestrator.optimize": "false"})
    assert config.reverse.orchestrator.optimize is False
    config2 = load_config(sample_config_path, cli_overrides={"reverse.orchestrator.optimize": "true"})
    assert config2.reverse.orchestrator.optimize is True


def test_env_override(monkeypatch: pytest.MonkeyPatch, sample_config_path: Path) -> None:
    monkeypatch.setenv("RE_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RE_AGENT_LLM_MODEL", "gpt-4o")
    config = load_config(sample_config_path)
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"


# ── Contracts: fail-fast validation ─────────────────────────────────────


def test_old_yaml_without_contracts_is_rejected() -> None:
    """YAML without a 'contracts:' section triggers a breaking-migration error."""
    yaml_text = """llm:
  provider: "openai"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="contracts.transformation_policy is required"):
            load_config(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)


def test_invalid_policy_value_rejected() -> None:
    """A policy other than 'preserve_abi' raises ValueError."""
    yaml_text = """contracts:
  transformation_policy: "legacy"
  abi_manifest_path: "dummy.json"
  abi_manifest_sha256: "abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="not supported"):
            load_config(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)


def test_missing_manifest_path_rejected() -> None:
    """When policy is set but manifest path is empty, raises ValueError."""
    yaml_text = """contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: ""
  abi_manifest_sha256: "abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="abi_manifest_path must be a non-empty path"):
            load_config(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)


def test_manifest_file_not_found(tmp_path: Path) -> None:
    """A non-existent manifest path raises FileNotFoundError."""
    manifest_path, manifest_sha = _write_valid_manifest(tmp_path)
    manifest_path.unlink()  # delete before referencing
    yaml_text = _contracts_yaml(str(manifest_path), manifest_sha)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        with pytest.raises(FileNotFoundError, match="ABI manifest not found"):
            load_config(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)


def test_manifest_not_a_regular_file(tmp_path: Path) -> None:
    """A directory-like path raises ValueError."""
    manifest_path, manifest_sha = _write_valid_manifest(tmp_path)
    manifest_path.unlink()
    manifest_path.mkdir()  # replace with directory
    yaml_text = _contracts_yaml(str(manifest_path), manifest_sha)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        yaml_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="not a regular file"):
            load_config(yaml_path)
    finally:
        yaml_path.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(manifest_path, ignore_errors=True)


def test_invalid_manifest_json_rejected(tmp_path: Path) -> None:
    """A manifest containing invalid JSON raises ValueError."""
    manifest_path = tmp_path / "bad.json"
    manifest_path.write_text("this is not json", encoding="utf-8")
    sha = "ab" * 32  # dummy hex, won't be checked because JSON parse fails first
    yaml_text = _contracts_yaml(str(manifest_path), sha)
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="ABI manifest validation failed"):
        load_config(yaml_path)


def test_wrong_sha256_rejected(tmp_path: Path) -> None:
    """A manifest whose configured SHA-256 does not match raises ValueError."""
    manifest_path, _ = _write_valid_manifest(tmp_path)
    # Use the wrong hash (all zeros)
    wrong_sha = "00" * 32
    yaml_text = _contracts_yaml(str(manifest_path), wrong_sha)
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="ABI manifest validation failed"):
        load_config(yaml_path)


def test_invalid_sha256_format_rejected(tmp_path: Path) -> None:
    """A non-hex SHA-256 string (wrong chars) raises ValueError."""
    manifest_path, _ = _write_valid_manifest(tmp_path)
    bad_sha = "x" * 64
    yaml_text = _contracts_yaml(str(manifest_path), bad_sha)
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="Not valid hexadecimal"):
        load_config(yaml_path)


def test_wrong_sha256_length_rejected(tmp_path: Path) -> None:
    """A SHA-256 with wrong length (not 64 chars) raises ValueError."""
    manifest_path, _ = _write_valid_manifest(tmp_path)
    short_sha = "abcdef"
    yaml_text = _contracts_yaml(str(manifest_path), short_sha)
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="exactly 64 hex characters"):
        load_config(yaml_path)


def test_valid_contracts_config_ok(tmp_path: Path) -> None:
    """A properly configured contracts section with valid manifest passes."""
    manifest_path, manifest_sha = _write_valid_manifest(tmp_path)
    yaml_text = _contracts_yaml(str(manifest_path), manifest_sha)
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    config = load_config(yaml_path)
    assert config.contracts.transformation_policy == "preserve_abi"
    assert Path(config.contracts.abi_manifest_path).resolve() == manifest_path.resolve()
    assert config.contracts.abi_manifest_sha256 == manifest_sha
    verified = config.contracts.verified_manifest
    assert verified is not None
    assert verified.resolved_path == manifest_path.resolve()
    assert verified.raw_sha256 == manifest_sha
    assert verified.canonical_sha256 == verified.manifest.sha256_hash


def test_relative_manifest_path_resolved_against_yaml_dir(tmp_path: Path) -> None:
    """A relative abi_manifest_path is resolved relative to the YAML config dir."""
    # Create the manifest inside a subdirectory of tmp
    manifest_dir = tmp_path / "abi"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path, manifest_sha = _write_valid_manifest(manifest_dir)

    # YAML lives in a sibling config directory with a relative path
    yaml_dir = tmp_path / "config"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    yaml_text = f"""contracts:
  transformation_policy: preserve_abi
  abi_manifest_path: "../abi/abi_manifest.json"
  abi_manifest_sha256: "{manifest_sha}"
"""
    yaml_path = yaml_dir / "re-agent.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    config = load_config(yaml_path)
    assert config.contracts.transformation_policy == "preserve_abi"
