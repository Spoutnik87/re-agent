# Configuration

> **⚠ BREAKING MIGRATION** — The `contracts` section is now **required by all
> operational commands** (`reverse`, `parity`, `status`, `pipeline`, `build`).
> Any `re-agent.yaml` without it is rejected with a clear error.
> There is no legacy fallback. See [Contracts](#abi-contracts) below.

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
| `RE_AGENT_CONTRACTS_TRANSFORMATION_POLICY` | `contracts.transformation_policy` |
| `RE_AGENT_CONTRACTS_ABI_MANIFEST_PATH` | `contracts.abi_manifest_path` |
| `RE_AGENT_CONTRACTS_ABI_MANIFEST_SHA256` | `contracts.abi_manifest_sha256` |

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

## ABI Contracts (Breaking Migration)

The `contracts` section pins the binary's ABI surface via an external manifest.
It is validated **fail-fast during config loading**, which means every command
that reads `re-agent.yaml` (`reverse`, `parity`, `status`, `pipeline`, `build`)
enforces it. Only `re-agent init` can be run without a pre-existing config.

The manifest is **validated and pinned** at config load time (`load_config()`),
but its symbols are **not yet consumed by the Transform phase**. This is the
first step — the manifest must exist and pass integrity checks, but ABI contract
enforcement during code generation is a future capability.

```yaml
contracts:
  # ── Mandatory. Only "preserve_abi" is valid. ─────────────────
  transformation_policy: "preserve_abi"

  # ── Path to the ABI manifest (relative to re-agent.yaml). ────
  abi_manifest_path: "abi_manifest.json"

  # ── Raw SHA-256 of the manifest file bytes (64 hex chars). ──
  abi_manifest_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

### Path Resolution

`abi_manifest_path` is resolved **relative to the directory containing the YAML
config file**. For example, if `re-agent.yaml` lives in `/home/user/project/`,
the value `"abi_manifest.json"` resolves to `/home/user/project/abi_manifest.json`.
Absolute paths are also accepted.

### Two-Layer Hash Model

re-agent uses **two independent SHA-256 hashes** at different layers:

| Layer | Field | Scope | Computation |
|-------|-------|-------|-------------|
| **Config** | `abi_manifest_sha256` (YAML) | Raw file bytes | `sha256sum abi_manifest.json` — trust anchor, user pins the exact file |
| **Manifest** | `sha256_hash` (inside JSON) | Canonical JSON content | SHA-256 of sorted-key JSON with `sha256_hash` blanked — self-integrity |

The config-layer hash is validated **fail-fast** during `load_config()`:
- Missing → `ValueError`
- Wrong length (not 64 hex chars) → `ValueError`
- Mismatch with actual file bytes → `ValueError`

The manifest-layer hash is validated when the manifest is loaded by
`load_manifest()`. Both must pass before an operational command finishes
loading its configuration.

### Preserve-ABI Transform: `--address` flag

`re-agent build --phase transform --address 0xADDRESS`
operates in preserve_abi mode (`contracts.transformation_policy: preserve_abi`).
It binds the transformed artifact to a single ABI manifest entry: the LLM output
is validated for its target address and exact output path; the declared signature
and calling convention are supplied to the prompt. A successful compilation produces the composite
verdict **MANIFEST_BOUND/COMPILE_PASS**.

> **⚠ This composite verdict is not an ABI proof.** It confirms manifest
> conformance and compilation only — no runtime, disassembly, or
> semantic-equivalence check is performed. It is not a behavioral proof.

**Current refusals in preserve_abi mode:**

| Invocation | Refused | Reason |
|------------|---------|--------|
| `re-agent build` (no `--phase`) | Yes | Bulk all-phase run cannot satisfy per-address manifest binding |
| `re-agent build --phase analyze` | No | Analysis remains available; it does not transform or publish ABI-bound code |
| `re-agent build --phase assemble` | Yes | Expects a full module tree, incompatible with single-function binding |
| `re-agent build --phase transform` (no `--address`) | Yes | Bulk transform processes multiple subunits, not a single entry |
| `re-agent build --phase transform --address 0xADDRESS` | No | Single-function manifest-bound transform — the only accepted form |

Refusals produce exit code 2 with a diagnostic message before any LLM call or
disk operation.

### Generating the Config Hash

```bash
# Linux / macOS
sha256sum abi_manifest.json

# Windows (PowerShell)
Get-FileHash abi_manifest.json -Algorithm SHA256

# Windows (cmd)
certutil -hashfile abi_manifest.json SHA256
```

Copy the hex digest output (64 characters) into `abi_manifest_sha256`.

## ABI Manifest Format

The ABI manifest is a **generic, versioned contract format** — not tied to any
specific binary. The core contains no target-specific rules.

### Schema

```json
{
  "version": "1.0.0",
  "architecture": "x86",
  "pointer_size": 4,
  "symbols": [
    {
      "address": 7316864,
      "name": "sub_6F9C80",
      "signature": "void __thiscall sub_6F9C80(void *this)",
      "calling_convention": "thiscall",
      "output_path": "module_a/sub_6F9C80.cpp"
    }
  ],
  "sha256_hash": "abc123..."
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Semantic version (MAJOR.MINOR.PATCH). Mandatory. |
| `architecture` | string | Target CPU: `"x86"`, `"x64"`, `"arm"`, `"aarch64"`. Mandatory. |
| `pointer_size` | integer | Pointer width in bytes (4 for x86/arm, 8 for x64/aarch64). Mandatory. |
| `symbols` | array | Exported symbols. At least one required. |
| `sha256_hash` | string | Canonical JSON SHA-256 (computed excluding itself). Mandatory. |

### Symbol Fields

| Field | Type | Description |
|-------|------|-------------|
| `address` | integer | Function entry point. Non-negative, must fit pointer width. |
| `name` | string | Symbol name as exported by the binary. Non-empty. |
| `signature` | string | C/C++ signature. Non-empty. |
| `calling_convention` | string | One of: `"cdecl"`, `"stdcall"`, `"fastcall"`, `"thiscall"`, `"vectorcall"`, `"systemv"`. |
| `output_path` | string | Relative POSIX `.cpp` path (forward slashes, no `..`, no drive letters). |

The manifest is fully validated on load:
- Unknown keys → `ValueError`
- Non-unique (address, name, output_path) → `ValueError`
- Path traversal in `output_path` → `ValueError`
- SHA-256 mismatch → `ValueError` (tamper detection)

### Calling Conventions

| Convention | Typical Platform |
|------------|-----------------|
| `cdecl` | x86 C (caller cleans stack) |
| `stdcall` | Win32 API |
| `fastcall` | x86 (first two args in ecx/edx) |
| `thiscall` | MSVC C++ member functions |
| `vectorcall` | MSVC vector extension |
| `systemv` | x64 System V ABI |

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
