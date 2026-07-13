"""Unified configuration schema for re-agent (reverse + build + pipeline)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    provider: str = "openai-compat"
    model: str = "deepseek/deepseek-v4-flash"
    block_model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 65536
    temperature: float = 0.0
    timeout_s: int = 1800


@dataclass
class BackendConfig:
    type: str = "ghidra-bridge"
    cli_path: str = "ghidra"
    export_dir: str | None = None
    timeout_s: int = 45


@dataclass
class ProjectProfile:
    hook_patterns: list[str] = field(
        default_factory=lambda: [
            r"RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)",
            r"RH_ScopedVirtualInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)",
        ]
    )
    stub_patterns: list[str] = field(default_factory=lambda: [r"plugin::Call"])
    stub_markers: list[str] = field(default_factory=lambda: ["NOTSA_UNREACHABLE"])
    stub_call_prefix: str = "plugin::Call"
    class_macro: str = "RH_ScopedClass"
    source_root: str = "source/game_sa"
    source_extensions: list[str] = field(default_factory=lambda: [".cpp", ".h", ".hpp"])
    hooks_csv: str | None = None
    project_description: str = ""
    project_context: str = ""
    checker_custom_rules: str = ""


@dataclass
class ParityConfig:
    enabled: bool = True
    call_count_warn_diff: int = 3
    inline_wrapper_autoskip: bool = False
    semantic_rules_file: str | None = None
    manual_checks_file: str | None = None
    cache_dir: str = ".cache/re-agent-parity"


@dataclass
class OrchestratorConfig:
    optimize: bool = True
    enable_phase1: bool = True
    max_review_rounds: int = 4
    max_functions_per_class: int = 10
    max_tokens_per_function: int = 200_000
    max_tokens_per_class: int = 2_000_000
    objective_verifier_enabled: bool = True
    objective_call_count_tolerance: int = 3
    objective_control_flow_tolerance: int = 2
    block_reversal_enabled: bool = True
    block_threshold_lines: int = 100
    block_max_lines: int = 40
    few_shot_min_score: int = 4


@dataclass
class ReverseOutputConfig:
    report_dir: str = "reports/re-agent"
    log_dir: str = "reports/re-agent/logs"
    session_file: str = "re-agent-progress.json"
    format: str = "json"


@dataclass
class CompileConfig:
    """Reverse-phase compile gate.

    When ``enabled``, each candidate is normalized and compiled inside the fix
    loop so compilation becomes a first-class PASS criterion and compiler
    errors are fed back to the reverser while Ghidra/asm context is still in
    hand. Disabled by default (requires a working compiler).

    ``flags`` is a permissive *gating* profile (syntax-only, no ``-Werror``);
    strict warning-level polish belongs to the build phase, not this gate.
    ``decls_header`` is force-included so a function referencing externally
    defined symbols can still compile in isolation.
    """

    enabled: bool = False
    compiler: str = "g++"
    compiler_flags: str = "-std=c++23 -fsyntax-only -w"
    decls_header: str | None = None
    require_compile: bool = True
    timeout_s: int = 30


@dataclass
class ReverseConfig:
    """Reverse-engineering (Phase 1) configuration."""

    backend: BackendConfig = field(default_factory=BackendConfig)
    project_profile: ProjectProfile = field(default_factory=ProjectProfile)
    parity: ParityConfig = field(default_factory=ParityConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    output: ReverseOutputConfig = field(default_factory=ReverseOutputConfig)
    compile: CompileConfig = field(default_factory=CompileConfig)


@dataclass
class BuildInputConfig:
    """Input directories for the build phase."""

    decompiled_dir: str = "reports/re-agent/code/"
    ghidra_exports: str = ".ghidra-exports/"


@dataclass
class BuildOutputConfig:
    """Output configuration for the build phase."""

    language: str = "cpp"
    standard: str = "c++23"
    compiler: str = "g++"
    # Gating compile profile: warnings are surfaced (-Wall) but do NOT fail the
    # build. Apply -Werror only as a separate, non-gating final lint.
    compiler_flags: str = "-std=c++23 -c -Wall"
    decls_header: str | None = None
    target_dir: str = "output/"
    work_dir: str = "."


@dataclass
class ProjectNaming:
    """Naming conventions for generated project code."""

    classes: str = "PascalCase"
    functions: str = "camelCase"
    globals: str = "snake_case"


@dataclass
class ProjectConventions:
    """Conventions for include style and function size limits."""

    naming: ProjectNaming = field(default_factory=ProjectNaming)
    includes: str = "use_forward_decl_when_possible"
    max_function_lines: int = 200


@dataclass
class BuildProjectConfig:
    """Project metadata for the build output."""

    name: str = ""
    description: str = ""
    conventions: ProjectConventions = field(default_factory=ProjectConventions)


@dataclass
class ModulesConfig:
    """Module clustering configuration."""

    expected: list[str] = field(default_factory=list)
    min_cluster_size: int = 20
    max_cluster_size: int = 300


@dataclass
class BuildOptimizationConfig:
    """LLM optimization and caching configuration.

    ``diagnostics_dir`` is the directory where per-subunit WorkPacket
    diagnostic JSON files are written. Empty string means "do not write
    work packet files" (backward-compatible default for callers that have
    not opted in). The path must NEVER point under ``reports/re-agent/code/``
    (the precious decompiled corpus) — it is run/evidence/report scoped.

    ``raw_response_capture`` gates writing the raw LLM response text to
    a file under ``diagnostics_dir``. Disabled by default so unconfigured
    runs never silently dump raw LLM output to a hidden path.
    """

    subunit_size: int = 10
    context_window: int = 3
    cache_enabled: bool = True
    cache_path: str = ".cr-agent-cache.json"
    diagnostics_dir: str = ""
    raw_response_capture: bool = False
    max_llm_calls_per_run: int = 8
    max_llm_tokens_per_run: int = 150000
    max_compile_retry_calls_per_run: int = 3

    def __post_init__(self) -> None:
        if self.max_llm_calls_per_run <= 0:
            raise ValueError("max_llm_calls_per_run must be > 0")
        if self.max_llm_tokens_per_run <= 0:
            raise ValueError("max_llm_tokens_per_run must be > 0")
        if self.max_compile_retry_calls_per_run < 0:
            raise ValueError("max_compile_retry_calls_per_run must be >= 0")


@dataclass
class ValidationConfig:
    """Build validation (compilation checks) configuration."""

    compile_per_function: bool = True
    compile_per_module: bool = True
    compile_final_project: bool = True
    max_compile_retries: int = 2
    target_contract_mode: str = "legacy"  # "legacy" or "required"


@dataclass
class BuildResumeConfig:
    """Build resumption configuration."""

    enabled: bool = True
    state_path: str = "cr-agent-state.json"


@dataclass
class BuildConfig:
    """Build (Phase 2) configuration."""

    input: BuildInputConfig = field(default_factory=BuildInputConfig)
    output: BuildOutputConfig = field(default_factory=BuildOutputConfig)
    project: BuildProjectConfig = field(default_factory=BuildProjectConfig)
    modules: ModulesConfig = field(default_factory=ModulesConfig)
    optimization: BuildOptimizationConfig = field(default_factory=BuildOptimizationConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    resume: BuildResumeConfig = field(default_factory=BuildResumeConfig)


@dataclass
class PipelineConfig:
    """Pipeline orchestration configuration."""

    state_file: str = "pipeline-state.json"


@dataclass
class ReAgentConfig:
    """Top-level unified configuration for re-agent (reverse + build + pipeline)."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    reverse: ReverseConfig = field(default_factory=ReverseConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)

    @classmethod
    def create_default(cls) -> ReAgentConfig:
        return cls()
