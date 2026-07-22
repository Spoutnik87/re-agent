"""Unified configuration loader for re-agent."""

from __future__ import annotations

import hashlib
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
    CompileConfig,
    ContractsConfig,
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
from re_agent.contracts import AbiManifest, VerifiedContract, load_verified_manifest

_log = logging.getLogger(__name__)
_T = TypeVar("_T")


_KNOWN_CONTRACTS_KEYS: frozenset[str] = frozenset({"transformation_policy", "abi_manifest_path", "abi_manifest_sha256"})


def _load_dotenv(yaml_path: Path | None = None) -> None:
    """Load ``.env`` file if present, searching config dir, CWD, and parents."""
    candidates: list[Path] = []
    if yaml_path:
        d = yaml_path.parent
        for _ in range(5):
            candidates.append(d / ".env")
            if d.parent == d or str(d.parent) == d.anchor:
                break
            d = d.parent
    candidates.append(Path(".env"))
    env_file = None
    for c in candidates:
        if c.exists():
            env_file = c
            break
    if env_file is None:
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except ImportError:
        pass


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
        # Contracts overrides
        ("RE_AGENT_CONTRACTS_TRANSFORMATION_POLICY", ["contracts", "transformation_policy"], str),
        ("RE_AGENT_CONTRACTS_ABI_MANIFEST_PATH", ["contracts", "abi_manifest_path"], str),
        ("RE_AGENT_CONTRACTS_ABI_MANIFEST_SHA256", ["contracts", "abi_manifest_sha256"], str),
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
    compile_cfg = _build_with_coercion(CompileConfig, data.get("compile", {}))
    return ReverseConfig(
        backend=backend, project_profile=pp, parity=parity, orchestrator=orch, output=out, compile=compile_cfg
    )


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


def _validate_contracts(contracts: ContractsConfig, yaml_dir: Path | None) -> VerifiedContract[AbiManifest]:
    """Validate contracts configuration fail-fast using ``load_verified_manifest``.

    This is a *breaking migration*: any YAML config without a properly
    configured ``contracts`` section is rejected with a clear error.
    There is no legacy fallback.

    .. note::

        This function is **only called in the legacy (no-override) path**.
        Project-mode callers that supply ``verified_contract_override`` skip
        this entirely — the override already carries a pre-validated
        ``VerifiedContract[AbiManifest]`` whose external path/hash
        requirements are satisfied by the project context.

    Raises
    ------
    ValueError
        If policy is missing, invalid, path/hash empty, file not found,
        JSON invalid, or hash mismatch.
    FileNotFoundError
        If the resolved manifest path does not exist.
    """
    # ── Policy presence (breaking) ──────────────────────────────────────
    if contracts.transformation_policy is None:
        raise ValueError(
            "contracts.transformation_policy is required. "
            "Set it to 'preserve_abi' to enable ABI preservation. "
            "This is a breaking migration: old configs without this section "
            "are rejected."
        )

    # ── Policy value ────────────────────────────────────────────────────
    if contracts.transformation_policy != "preserve_abi":
        raise ValueError(
            f"contracts.transformation_policy={contracts.transformation_policy!r} "
            "is not supported. Only 'preserve_abi' is valid."
        )

    # ── External path/hash presence (fail-closed for legacy) ────────────
    if not contracts.abi_manifest_path.strip():
        raise ValueError(
            "contracts.abi_manifest_path must be a non-empty path "
            "when contracts.transformation_policy is set (legacy mode)."
        )
    if not contracts.abi_manifest_sha256.strip():
        raise ValueError(
            "contracts.abi_manifest_sha256 must be a non-empty SHA-256 digest "
            "when contracts.transformation_policy is set (legacy mode)."
        )

    # ── Resolve manifest path (relative to YAML config dir) ─────────────
    manifest_path = Path(contracts.abi_manifest_path)
    if not manifest_path.is_absolute() and yaml_dir is not None:
        manifest_path = yaml_dir / manifest_path

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"ABI manifest not found: {manifest_path} "
            f"(resolved from contracts.abi_manifest_path="
            f"{contracts.abi_manifest_path!r})"
        )
    if not manifest_path.is_file():
        raise ValueError(f"ABI manifest path is not a regular file: {manifest_path}")

    # ── Validate SHA-256 ────────────────────────────────────────────────
    sha256_hex = contracts.abi_manifest_sha256.strip()
    if len(sha256_hex) != 64:
        raise ValueError(
            "contracts.abi_manifest_sha256 must be exactly 64 hex characters "
            f"(SHA-256 digest), got {len(sha256_hex)} characters: "
            f"{sha256_hex!r}"
        )
    _validate_hex(sha256_hex)

    # ── Delegate to contracts module (loads JSON, validates raw hash) ──
    try:
        manifest, raw_hash, canonical_hash = load_verified_manifest(manifest_path, expected_raw_hash=sha256_hex)
    except ValueError as exc:
        raise ValueError(f"ABI manifest validation failed: {exc}") from exc

    return VerifiedContract(
        manifest=manifest,
        resolved_path=manifest_path.resolve(),
        raw_sha256=raw_hash,
        canonical_sha256=canonical_hash,
    )


def _validate_hex(hex_str: str) -> None:
    """Fail-fast if *hex_str* is not exactly 64 hex characters."""
    if len(hex_str) != 64:
        raise ValueError(f"Expected 64 hex characters, got {len(hex_str)}: {hex_str!r}")
    try:
        int(hex_str, 16)
    except ValueError as err:
        raise ValueError(f"Not valid hexadecimal: {hex_str!r}") from err


def _build_config(
    raw: dict[str, Any],
    yaml_path: Path | None = None,
    *,
    verified_contract_override: VerifiedContract[AbiManifest] | None = None,
) -> ReAgentConfig:
    llm = _build_with_coercion(LLMConfig, raw.get("llm", {}))
    reverse = _build_reverse_config(raw.get("reverse", {}))
    build = _build_build_config(raw.get("build", {}))
    contracts_raw = raw.get("contracts", {})
    if isinstance(contracts_raw, dict):
        _validate_known_keys(contracts_raw, _KNOWN_CONTRACTS_KEYS, "contracts")
    contracts = _build_with_coercion(ContractsConfig, contracts_raw)
    pipeline = _build_with_coercion(PipelineConfig, raw.get("pipeline", {}))
    config = ReAgentConfig(llm=llm, reverse=reverse, build=build, contracts=contracts, pipeline=pipeline)

    # ── Contract validation: two paths ──────────────────────────────────
    #
    # 1. **Legacy path** (override is None): full fail-closed validation
    #    via _validate_contracts — requires policy, path, SHA-256, and a
    #    real manifest file on disk.  Missing/mismatched fields raise.
    #
    # 2. **Project-mode path** (override is provided): the caller already
    #    holds a VerifiedContract[AbiManifest] from the project context.
    #    External ABI path/hash requirements are bypassed — the override
    #    carries its own integrity proof.  Only the policy value is checked
    #    for consistency.
    #
    yaml_dir = yaml_path.parent if yaml_path is not None else None
    if verified_contract_override is None:
        # ── Legacy: fail-closed ─────────────────────────────────────────
        config.contracts.verified_manifest = _validate_contracts(config.contracts, yaml_dir)
    else:
        # ── Project mode: override bypasses external ABI path/hash ──────
        if config.contracts.transformation_policy != "preserve_abi":
            raise ValueError(
                "Project mode (--project-root) requires "
                "contracts.transformation_policy=preserve_abi in the YAML config. "
                "Add the following to your re-agent.yaml:\n"
                "  contracts:\n"
                "    transformation_policy: preserve_abi\n"
                "    abi_manifest_path: ...\n"
                "    abi_manifest_sha256: ..."
            )
        config.contracts.verified_manifest = verified_contract_override

    return config


def _validate_known_keys(data: dict[str, Any], known: frozenset[str], section: str) -> None:
    """Raise ValueError on unknown keys in *data* for *section*."""
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown key(s) in {section} section: {', '.join(sorted(unknown))}")


def load_config(
    yaml_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    *,
    verified_contract_override: VerifiedContract[AbiManifest] | None = None,
) -> ReAgentConfig:
    """Load, validate, and return a ``ReAgentConfig``.

    This is a **breaking migration**: ``load_config(None)`` no longer returns
    defaults — it looks for ``re-agent.yaml`` in the current directory and
    fails with ``FileNotFoundError`` if none is found.  Use ``re-agent init
    --abi-manifest <PATH>`` to create a valid config file.

    Parameters
    ----------
    yaml_path:
        Explicit path to a YAML config file.  When ``None``, the loader
        tries ``re-agent.yaml`` in the current directory.
    cli_overrides:
        Optional dotted-key → value overrides (e.g. ``llm.provider``).

    Raises
    ------
    FileNotFoundError
        If *yaml_path* is ``None`` and no ``re-agent.yaml`` exists in CWD,
        or if the specified path does not exist.
    ValueError
        If the config fails validation (missing/invalid contracts, bad
        manifest SHA-256, malicious manifest content, etc.).
    """
    _load_dotenv(yaml_path)
    raw: dict[str, Any] = {}
    if yaml_path is not None:
        if yaml_path.exists():
            raw = _load_yaml_file(yaml_path)
        else:
            raise FileNotFoundError(f"Config file not found: {yaml_path}")
    else:
        default_path = Path("re-agent.yaml")
        if default_path.exists():
            yaml_path = default_path  # ← enables contract validation
            raw = _load_yaml_file(default_path)
        else:
            raise FileNotFoundError(
                "No config file specified and 're-agent.yaml' not found in "
                "the current directory.  Create one with:\n"
                "  re-agent init --abi-manifest <PATH_TO_ABI_MANIFEST>"
            )

    raw = _apply_env_overrides(raw)
    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)
    return _build_config(raw, yaml_path, verified_contract_override=verified_contract_override)


def load_config_bytes(data: bytes, path: Path, **kwargs: Any) -> tuple[ReAgentConfig, str]:
    """Load ``ReAgentConfig`` from raw byte content and return ``(config, sha256)``.

    Parameters
    ----------
    data:
        Raw bytes of the YAML config file.
    path:
        Path to the original config file (used as base dir for relative paths).
    **kwargs:
        Forwarded to ``_build_config``; supports ``cli_overrides`` and
        ``verified_contract_override``.

    Returns
    -------
    tuple[ReAgentConfig, str]
        The parsed config and its SHA-256 hex digest.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    import yaml as _yaml

    raw = _yaml.safe_load(data)
    if raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        raise ValueError(f"Expected YAML mapping, got {type(raw).__name__}")
    raw = _apply_env_overrides(raw)
    cli_overrides = kwargs.get("cli_overrides")
    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)
    config = _build_config(
        raw,
        path,
        verified_contract_override=kwargs.get("verified_contract_override"),
    )
    return config, sha256
