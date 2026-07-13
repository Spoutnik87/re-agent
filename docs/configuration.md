# Configuration

re-agent is configured via `re-agent.yaml`, environment variables, and CLI flags.

## Priority Order

CLI flags > Environment variables > YAML config > Defaults

## Environment Variables

| Variable | Maps to |
|----------|---------|
| `RE_AGENT_LLM_PROVIDER` | `llm.provider` |
| `RE_AGENT_LLM_API_KEY` | `llm.api_key` |
| `RE_AGENT_LLM_MODEL` | `llm.model` |
| `RE_AGENT_LLM_BASE_URL` | `llm.base_url` |
| `RE_AGENT_BACKEND_CLI_PATH` | `reverse.backend.cli_path` |
| `RE_AGENT_BACKEND_TIMEOUT` | `reverse.backend.timeout_s` |

## LLM Config

```yaml
llm:
  provider: "claude"        # claude | openai | openai-compat | codex
  model: "claude-sonnet-4-5-20250929"
  api_key: null
  base_url: null
  max_tokens: 4096
  temperature: 0.0
  timeout_s: 1800
```

Notes:

- `claude` uses the Anthropic SDK and typically reads `ANTHROPIC_API_KEY`
- `openai` and `openai-compat` use the OpenAI-compatible chat completions provider and typically read `OPENAI_API_KEY`
- `codex` uses the local `codex` CLI and ChatGPT login credentials instead of an API key

## Project Profile

The `reverse.project_profile` section makes re-agent work across different RE projects:

```yaml
reverse:
  project_profile:
    hook_patterns:
      - 'RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)'
    stub_markers: ["NOTSA_UNREACHABLE"]
    stub_call_prefix: "plugin::Call"
    source_root: "./source/game_sa"
    source_extensions: [".cpp", ".h", ".hpp"]
```

## Parity Config

```yaml
reverse:
  parity:
    enabled: true
    call_count_warn_diff: 3
    inline_wrapper_autoskip: false
```

## Orchestrator Config

```yaml
reverse:
  orchestrator:
    max_review_rounds: 4
    max_functions_per_class: 10
    objective_verifier_enabled: true
    objective_call_count_tolerance: 3
    objective_control_flow_tolerance: 2
```

## Build Config

The `build:` section controls the code reconstruction pipeline (analyze → transform → assemble).

```yaml
build:
  input:
    decompiled_dir: "reports/re-agent/code/"
    ghidra_exports: ".ghidra-exports/"

  output:
    language: "cpp"
    standard: "c++23"
    compiler: "C:\\msys64\\mingw32\\bin\\g++.exe"
    compiler_flags: "-std=c++23 -m32 -c -Wall"
    target_dir: "output/"
    # Keep "." for the full pipeline: Assemble reads intermediates from the CWD.
    work_dir: "."
    # Optional: Analyze writes this declaration header only when configured.
    decls_header: ".ghidra-exports/_decls.h"

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
    diagnostics_dir: ""
    raw_response_capture: false
    max_llm_calls_per_run: 8
    max_llm_tokens_per_run: 150000
    max_compile_retry_calls_per_run: 3

  validation:
    compile_per_function: true
    compile_per_module: true
    compile_final_project: true  # Reserved; not applied by the current pipeline.
    max_compile_retries: 1
    target_contract_mode: "legacy"   # legacy | required

  resume:
    enabled: true
    state_path: "cr-agent-state.json"
```

### Build Sections

| Section | Required | Description |
|---------|----------|-------------|
| `input` |  | Source decompiled files and Ghidra exports (defaults provided) |
| `output` |  | Compiler flags and output paths (defaults provided) |
| `project` | | Project metadata and naming conventions |
| `modules` | | Module clustering constraints (min/max size) |
| `optimization` | | LLM budget, caching, subunit batching |
| `validation` | | Compilation gates and TARGET contract enforcement |
| `resume` | | State persistance for interruptible runs |

### Global Transform Budget (optimization)

The three budget parameters form a shared, per-invocation cap across ALL subunits.
Every LLM call — initial generation, TARGET recovery, compile retry — deducts from
these counters.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_llm_calls_per_run` | 8 | Hard cap on total LLM calls. Must be > 0. |
| `max_llm_tokens_per_run` | 150000 | Token cap checked after each call via delta (prompt + completion). Must be > 0. |
| `max_compile_retry_calls_per_run` | 3 | Max compile retry LLM calls across all functions. 0 disables retries. Must be ≥ 0. |

Token budget is a **stop-between-calls** cap: the delta from `get_usage()` before/after
each LLM call is subtracted. Compile retries are further bounded by GCC category
(only `syntax_error`, `undeclared_identifier`, `type_mismatch`, `goto_error` qualify)
and stagnation detection (identical SHA-256 stderr blocks further retries).

### Validation: target_contract_mode

The `target_contract_mode` controls how `// TARGET: <ordinal> <address>` markers
in LLM output are enforced:

- **`legacy`** (default): only an output with no TARGET markers falls back to
  name/address matching. Partial or invalid TARGET coverage is rejected.
- **`required`** (fail-closed): TARGET markers are mandatory for every function.
  The LLM must produce a valid `// TARGET:` line before each `// FILE:` block.
  Recovery (up to 2 LLM calls, batch size 4) runs only for partial, otherwise-valid
  TARGET coverage. Missing, malformed, or conflicting TARGET output fails immediately.
  If recovery still produces incomplete coverage, the entire
  subunit is rejected with `INCOMPLETE_TARGETS` — no files written, no cache
  populated, compile count is zero.

### Resume Config

```yaml
build:
  resume:
    enabled: true          # Set to false to force a fresh run from module 0
    state_path: "cr-agent-state.json"
```

Resume allows interrupted `re-agent build` runs to pick up where they left off.
The state file tracks `completed_modules`, `current_module`, and `current_subunit`.
Disable for clean runs or when module clustering has changed.

`decls_header` is optional. When configured, Analyze writes the declaration header to
that path; make it available to the transformed sources if their generated includes use it.
For a full Analyze → Transform → Assemble run, keep `output.work_dir` as `"."`:
Assemble currently reads intermediate artifacts from the current working directory.

## Breaking Changes from v1.x

- `recovery_token_budget` has been **removed**. The global budget
  (`max_llm_calls_per_run`, `max_llm_tokens_per_run`) now controls everything
  including TARGET recovery. There is no separate allocation for recovery.
