"""Unified configuration loader for re-agent."""

from __future__ import annotations

import logging
import os
import typing
from pathlib import Path
from typing import Any, TypeVar

from re_agent.config.schema import (
    BackendConfig,
    BuildConfig,
    BuildInputConfig,
    BuildOptimizationConfig,
    BuildOutputConfig,
    BuildProjectConfig,
    BuildResumeConfig,
    LLMConfig,
    ModulesConfig,
    OrchestratorConfig,
    ParityConfig,
    PipelineConfig,
    ProjectConventions,
    ProjectNaming,
    ProjectProfile,
    ReAgentConfig,
    ReverseConfig,
    ReverseOutputConfig,
    ValidationConfig,
)

_log = logging.getLogger(__name__)
_T = TypeVar("_T")


def _load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as err:
        raise ImportError("PyYAML is required. Install with: pip install pyyaml") from err
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}, got {type(data).__name__}")
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    env_mappings: list[tuple[str, list[str], type]] = [
        ("RE_AGENT_LLM_PROVIDER", ["llm", "provider"], str),
        ("RE_AGENT_LLM_API_KEY", ["llm", "api_key"], str),
        ("RE_AGENT_LLM_MODEL", ["llm", "model"], str),
        ("RE_AGENT_LLM_BLOCK_MODEL", ["llm", "block_model"], str),
        ("RE_AGENT_LLM_BASE_URL", ["llm", "base_url"], str),
        ("RE_AGENT_BACKEND_CLI_PATH", ["reverse", "backend", "cli_path"], str),
        ("RE_AGENT_BACKEND_TIMEOUT", ["reverse", "backend", "timeout_s"], int),
        # Legacy flat keys (backward compat)
        ("RE_AGENT_BACKEND_CLI_PATH", ["backend", "cli_path"], str),
        ("RE_AGENT_BACKEND_TIMEOUT", ["backend", "timeout_s"], int),
    ]
    for env_var, key_path, cast_type in env_mappings:
        value = os.environ.get(env_var)
        if value is None:
            continue
        d = raw
        for part in key_path[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        try:
            d[key_path[-1]] = cast_type(value)
        except (ValueError, TypeError) as exc:
            _log.warning("Invalid value for %s: %r — %s, ignoring", env_var, value, exc)
    return raw


def _apply_cli_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for dotted_key, value in overrides.items():
        parts = dotted_key.split(".")
        d = raw
        for part in parts[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value
    return raw


def _coerce_field(value: Any, field_type: Any) -> Any:
    if value is None:
        return value
    origin = typing.get_origin(field_type)
    if origin is not None:
        if origin is list:
            args = typing.get_args(field_type)
            if isinstance(value, list) and args:
                return [_coerce_field(v, args[0]) for v in value]
            return value
        return value
    if field_type is int and not isinstance(value, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if field_type is float and not isinstance(value, int | float):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if field_type is bool and not isinstance(value, bool):
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    return value


def _build_with_coercion(cls: type[_T], data: dict[str, Any]) -> _T:
    hints = typing.get_type_hints(cls)
    filtered: dict[str, Any] = {}
    for k, v in data.items():
        if k in hints:
            filtered[k] = _coerce_field(v, hints[k])
        else:
            _log.warning(
                "Unknown config key '%s' in %s (known: %s) — ignored",
                k,
                cls.__name__,
                ", ".join(sorted(hints)),
            )
    return cls(**filtered)


def _build_reverse_config(data: dict[str, Any]) -> ReverseConfig:
    backend = _build_with_coercion(BackendConfig, data.get("backend", {}))
    pp = _build_with_coercion(ProjectProfile, data.get("project_profile", {}))
    parity = _build_with_coercion(ParityConfig, data.get("parity", {}))
    orch = _build_with_coercion(OrchestratorConfig, data.get("orchestrator", {}))
    out = _build_with_coercion(ReverseOutputConfig, data.get("output", {}))
    return ReverseConfig(backend=backend, project_profile=pp, parity=parity, orchestrator=orch, output=out)


def _build_build_config(data: dict[str, Any]) -> BuildConfig:
    inp = _build_with_coercion(BuildInputConfig, data.get("input", {}))
    out = _build_with_coercion(BuildOutputConfig, data.get("output", {}))

    proj_raw = data.get("project", {})
    if not isinstance(proj_raw, dict):
        proj_raw = {}
    conv_raw = proj_raw.get("conventions", {})
    if not isinstance(conv_raw, dict):
        conv_raw = {}
    naming_raw = conv_raw.get("naming", {})
    if not isinstance(naming_raw, dict):
        naming_raw = {}

    naming = _build_with_coercion(ProjectNaming, naming_raw)
    conventions_raw = dict(conv_raw)
    conventions_raw["naming"] = naming
    conventions = _build_with_coercion(ProjectConventions, conventions_raw)
    proj_dict = dict(proj_raw)
    proj_dict["conventions"] = conventions
    project = _build_with_coercion(BuildProjectConfig, proj_dict)

    modules = _build_with_coercion(ModulesConfig, data.get("modules", {}))
    opt = _build_with_coercion(BuildOptimizationConfig, data.get("optimization", {}))
    val = _build_with_coercion(ValidationConfig, data.get("validation", {}))
    resume = _build_with_coercion(BuildResumeConfig, data.get("resume", {}))
    return BuildConfig(
        input=inp, output=out, project=project, modules=modules, optimization=opt, validation=val, resume=resume
    )


def _build_config(raw: dict[str, Any]) -> ReAgentConfig:
    llm = _build_with_coercion(LLMConfig, raw.get("llm", {}))
    reverse = _build_reverse_config(raw.get("reverse", {}))
    build = _build_build_config(raw.get("build", {}))
    pipeline = _build_with_coercion(PipelineConfig, raw.get("pipeline", {}))
    return ReAgentConfig(llm=llm, reverse=reverse, build=build, pipeline=pipeline)


def load_config(
    yaml_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ReAgentConfig:
    raw: dict[str, Any] = {}
    if yaml_path is not None:
        if yaml_path.exists():
            raw = _load_yaml_file(yaml_path)
        else:
            raise FileNotFoundError(f"Config file not found: {yaml_path}")
    else:
        default_path = Path("re-agent.yaml")
        if default_path.exists():
            raw = _load_yaml_file(default_path)

    raw = _apply_env_overrides(raw)
    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)
    return _build_config(raw)
