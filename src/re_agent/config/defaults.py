"""Default configuration templates for re-agent (unified)."""

from __future__ import annotations

DEFAULT_CONFIG_YAML: str = """\
# re-agent unified configuration
# Runs both reverse-engineering (Phase 1) and code reconstruction (Phase 2).

llm:
  provider: "openai-compat"
  model: "deepseek/deepseek-v4-flash"
  # block_model: null
  base_url: "http://localhost:8787/v1"
  max_tokens: 65536
  temperature: 0.0
  timeout_s: 1800

pipeline:
  state_file: "pipeline-state.json"

reverse:
  backend:
    type: "ghidra-bridge"
    cli_path: "ghidra"
    timeout_s: 45

  project_profile:
    source_root: "source/game_sa"
    source_extensions:
      - ".cpp"
      - ".h"
      - ".hpp"

  parity:
    enabled: true
    call_count_warn_diff: 3
    cache_dir: ".cache/re-agent-parity"

  orchestrator:
    optimize: true
    enable_phase1: true
    max_review_rounds: 4
    max_functions_per_class: 10
    objective_verifier_enabled: true
    block_reversal_enabled: true
    block_threshold_lines: 100
    block_max_lines: 40

  output:
    report_dir: "reports/re-agent"
    log_dir: "reports/re-agent/logs"
    session_file: "re-agent-progress.json"

build:
  input:
    decompiled_dir: "reports/re-agent/code/"
    ghidra_exports: ".ghidra-exports/"

  output:
    language: "cpp"
    standard: "c++23"
    compiler: "C:\\\\msys64\\\\mingw32\\\\bin\\\\g++.exe"
    compiler_flags: "-std=c++23 -m32 -c -Wall -Werror"
    target_dir: "output/"

  project:
    name: ""
    description: ""
    conventions:
      naming:
        classes: PascalCase
        functions: camelCase
        globals: snake_case
      includes: "use_forward_decl_when_possible"
      max_function_lines: 200

  modules:
    expected: []
    min_cluster_size: 20
    max_cluster_size: 300

  optimization:
    subunit_size: 10
    context_window: 3
    cache_enabled: true
    cache_path: ".cr-agent-cache.json"

  validation:
    compile_per_function: true
    compile_per_module: true
    compile_final_project: true
    max_compile_retries: 1

  resume:
    enabled: true
    state_path: "cr-agent-state.json"
"""

EXAMPLE_PROFILE_TEMPLATES: dict[str, dict[str, object]] = {}
