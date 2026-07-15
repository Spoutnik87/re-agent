# re-agent (fork) — v2.0.0

Autonomous reverse-engineering agent with a **build pipeline** for code reconstruction: source-aware reverser/checker loop, block-level decompilation, few-shot learning, objective verifier, Ghidra backend, and a **three-phase build (analyze → transform → assemble)** for reconstructing C++ source trees from flat `.cpp` decompilations.

This is a fork with significant enhancements: block-level decompilation for large functions, few-shot example retrieval (5700+ indexed successes), pre-classification for strategy selection, configurable prompt templates, token-optimized LLM interactions (+ retry logic), a `re-agent build` pipeline, and an extended test suite.

## Overview

re-agent automates a reverse-engineering workflow by combining a reverser/checker loop with Ghidra decompilation through [ghidra-ai-bridge](https://github.com/dryxio/ghidra-ai-bridge). The pipeline also retrieves nearby project source context during generation and runs a conservative structural verifier before accepting checker passes.

```
re-agent reverse --class CTrain
    │
    ├── Config (re-agent.yaml + env + CLI)
    │   ├── project_profile (stub_markers, hook_patterns, source_layout)
    │   └── project_description / project_context / checker_custom_rules
    │
    ├── Orchestrator (single / class runner / block)
    │   ├── Pre-classification (leaf / getter-setter / vtable-heavy / Win32 / complex)
    │   │   └── Skips expensive block reversal for trivial functions
    │   │
    │   ├── Block-Level Pipeline (>100 lines, 2-tier escalation)
    │   │   ├── Block Splitter (if/else chains grouped into single blocks)
    │   │   ├── Variable Mapping (always pro model — flash=translation only)
    │   │   ├── Block Reverser (per-block + targeted issue injection)
    │   │   └── Stitch Validation (brace balance + block count checks)
    │   │
    │   ├── Agent Loop (reverser → checker → fix, stagnation detection)
    │   │   ├── Few-Shot Builder (5700+ indexed successes → 2 similar examples)
    │   │   ├── Structural Diff (non-LLM call-order comparison for checker)
    │   │   ├── LLM Providers: Claude | OpenAI-compatible APIs | Codex CLI
    │   │   ├── Prompt Templates (customizable, Phase 1/2 optional)
    │   │   ├── Token Tracking (total_prompt_tokens + completion_tokens)
    │   │   └── Retry with exponential backoff (3 attempts, 1s→10s)
    │   │
    │   ├── Objective Verifier (cached Ghidra decompile, call/CF counts)
    │   │
    │   ├── Parity Engine (GREEN/YELLOW/RED verification gate)
    │   │   ├── Source Indexer (C++ body parser)
    │   │   ├── 11 Heuristic Signals (all configurable/toggleable)
    │   │   └── Semantic Rules + Manual Approvals
    │   │
    │   └── Session State (JSON progress file)
    │
    └── RE Backend: ghidra-ai-bridge
        └── Capability flags → graceful degradation
```

## Build Pipeline (v2.0.0)

The `re-agent build` command runs a three-phase pipeline to produce a C++ source tree from flat decompiled `.cpp` files:

```
re-agent build
    │
    └── Pipeline (3-phase)
        │
        ├── Phase 1: Analyze
        │   ├── Call graph construction (undirected, networkx)
        │   ├── Louvain community detection (networkx)
        │   ├── TF-IDF similarity sub-unit grouping (scikit-learn)
        │   └── Output: modules.json
        │
        ├── Phase 2: Transform
        │   ├── LLM code refinement per sub-unit
        │   ├── Target identity protocol (// TARGET: markers)
        │   ├── Compile-gated acceptance (GCC per-function)
        │   ├── Global budget (calls, tokens, compile-retries)
        │   └── Output: temp_transformed/ files
        │
        └── Phase 3: Assemble
            ├── Copy transformed files to output/
            ├── Conflict reporting (no automated resolution)
            ├── Common header generation
            ├── CMakeLists.txt generation
            └── Output: output/ source tree
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `re-agent init --abi-manifest PATH` | Generate `re-agent.yaml` config file |
| `re-agent reverse --address ADDR` | Reverse a single function |
| `re-agent reverse --class CLASS` | Reverse all functions in a class |
| `re-agent reverse --address ADDR --no-optimize` | Disable token optimizations |
| `re-agent reverse --dry-run` | Show what would be reversed |
| `re-agent parity --address ADDR` | Run parity checks on a function |
| `re-agent parity --filter REGEX` | Run parity checks matching pattern |
| `re-agent status` | Show reversal progress |
| `re-agent status --class CLASS` | Show progress for a specific class |
| `re-agent pipeline` | Run full pipeline (reverse then build) |
| `re-agent pipeline --skip-reverse` | Run build only |
| `re-agent pipeline --skip-build` | Run reverse only |
| `re-agent build` | Run full build pipeline (analyze → transform → assemble) |
| `re-agent build --phase analyze` | Run only the analyze phase |
| `re-agent build --phase transform` | Run only the transform phase |
| `re-agent build --phase transform --address 0xADDRESS` | Transform a single function bound to its ABI manifest entry (preserve_abi mode) |
| `re-agent build --phase assemble` | Run only the assemble phase |
| `re-agent build --phase transform --module MyModule` | Transform a specific module |
| `re-agent build --phase transform --module MyModule --subunit 5` | Start at subunit index 5 |
| `re-agent build --max-subunits 10` | Process at most 10 subunits |
| `re-agent build --run-id "my-run"` | Tag diagnostics with a run identifier |
| `re-agent build --phase transform --no-persist` | Dry-run transform, stdout JSON only |

### Build Pipeline Details

**`--phase`** selects which phase(s) to run. Without `--phase`, all three phases execute sequentially. Phases can be run independently — `analyze` must complete before `transform`, and `transform` before `assemble`.

**`--module`** restricts the transform phase to a single module (by name from `modules.json`).

**`--subunit`** sets the starting subunit index within the targeted module (used only with `--module` and `--phase transform`; no hard constraint enforce).

**`--max-subunits`** caps the total number of subunits processed globally across all modules.

**`--run-id`** tags diagnostics and evidence paths for traceability.

**`--no-persist`** — dry-run mode for the `--phase transform` only. Output is a single JSON document on stdout. No files, cache, state, or temp dirs are written, and compilation is skipped. Functions successfully associated with their targets report `SKIPPED_COMPILE` with `compiles: false`. LLM calls still run and consume the global budget (`max_llm_calls_per_run`, `max_llm_tokens_per_run`), so the run is billable. Only valid with `--phase transform`. Messages go to stderr. Exit codes: **0** (no contract failure) or **2** (contract failure, budget exceeded, provider errors, incomplete targets, or hard rejects). Exit 1 is not produced in no-persist mode.

### Target Contract Mode

The `target_contract_mode` setting in `validation:` controls how `// TARGET:` markers in LLM output are enforced:

- **`legacy`** (default): TARGET markers are optional only when **no TARGET markers at all** are present; then the system falls back to name/address matching. Any partial or invalid TARGET coverage rejects identity rather than falling back.
- **`required`** (fail-closed): TARGET markers are mandatory. If **no TARGET markers at all** are present, the subunit fails immediately with `contract_failed`. If any `// TARGET:` line is present but malformed/invalid, the subunit also fails immediately. Only **partial valid TARGETs** (some functions covered, some missing, no conflicts) trigger recovery (up to 2 LLM calls, batch size 4). If recovery still produces incomplete coverage, the entire subunit is rejected with `INCOMPLETE_TARGETS` — no files written, no cache populated, compile count is zero.

### Global Transform Budget

Shared across ALL subunits within a single `re-agent build` invocation:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_llm_calls_per_run` | 8 | Hard cap on total LLM calls (initial gen, TARGET recovery, compile retries) |
| `max_llm_tokens_per_run` | 150000 | Token cap checked after each LLM call via delta |
| `max_compile_retry_calls_per_run` | 3 | Max compile retry calls across all functions |

Token budget is enforced as a **stop-between-calls** cap: the delta from `get_usage()` before/after each call is subtracted. GCC compile retries are additionally bounded by retryable category (only `syntax_error`, `undeclared_identifier`, `type_mismatch`, `goto_error` qualify) and stagnation detection (identical SHA-256 stderr blocks further retries).

### Preserve-ABI Transform

`re-agent build --phase transform --address 0xADDRESS` operates in preserve_abi mode. It binds one transformed artifact to a specific ABI manifest entry: its address and exact output path are validated, while the declared signature and calling convention are supplied to the LLM prompt. A successful compilation produces the composite verdict **MANIFEST_BOUND/COMPILE_PASS**.

**This verdict is not an ABI proof** — it confirms only that (1) the function address was matched to a manifest entry, and (2) the generated code compiles. It is not a behavioral proof: the compiled output is never executed, compared against the original binary's disassembly, or checked for semantic equivalence. No runtime, ASM comparison, or symbol-level verification is performed.

**Current refusals in preserve_abi mode:** the following invocations are rejected with exit code 2 and a diagnostic message before any LLM call or disk operation:

| Invocation | Refused | Reason |
|------------|---------|--------|
| `re-agent build` (no `--phase`) | Yes | Bulk all-phase run cannot satisfy per-address manifest binding |
| `re-agent build --phase analyze` | No | Analysis remains available; it does not transform or publish ABI-bound code |
| `re-agent build --phase assemble` | Yes | Expects a full module tree, incompatible with single-function binding |
| `re-agent build --phase transform` (no `--address`) | Yes | Bulk transform processes multiple subunits, not a single entry |
| `re-agent build --phase transform --address 0xADDRESS` | No | Single-function manifest-bound transform — the only accepted form |

### Breaking Changes from v1.x

- `recovery_token_budget` has been **removed**. The global budget (`max_llm_calls_per_run`, `max_llm_tokens_per_run`) now controls everything including TARGET recovery.

## Key Enhancements (this fork)

### Phase 1/Phase 2 Toggle (`enable_phase1`)

By default the reverser uses a two-phase prompt: Phase 1 (control flow tree + variable map + call inventory) followed by Phase 2 (code generation). This costs ~40% output tokens. Setting `enable_phase1: false` in `re-agent.yaml` switches to a direct code-generation prompt — the LLM produces clean C++20 without the explicit analysis preamble.

```yaml
orchestrator:
  enable_phase1: true   # false = skip analysis preamble, direct code gen
```

### Few-Shot Example Retrieval

5700+ successfully decompiled functions are indexed by structural features (line count, vtable density, global reference count). Before each reversal, 2 similar examples are retrieved and injected into the reverser prompt as reference. This provides the LLM with in-project naming conventions, vtable dispatch patterns, and code style.

```python
# Index is built lazily on first use from reports/re-agent/code/
# Similarity: line bucket (4 tiers) + vtable bucket (no/light/heavy) + globals + calls
# Lazy-loaded singleton, shared across all reverser instances
```

### Pre-Classification + Strategy Selection

Functions are pre-classified by their decompile characteristics before strategy selection:

| Class | Characteristics | Strategy |
|-------|----------------|----------|
| `leaf` | 0 calls, 0 vtables | Skip block reversal, 2 rounds |
| `getter-setter` | ≤2 calls, no vtables, <20 lines | Skip block reversal, 2 rounds |
| `vtable-heavy` | ≥5 vtable dispatches | Pro model, 5 rounds |
| `win32-api` | GetProcAddress/LoadLibrary/RegisterClass | Skip LLM checker |
| `complex-state-machine` | ≥200 lines, ≥10 calls | Block-level decomposition |
| `general` | Default | Standard pipeline |

### Block Splitter: if/else Chain Grouping

Instead of splitting `if {} else if {} else {}` into 3 separate blocks, the splitter now groups entire conditional chains into a single logical block. This eliminates orphaned `else` fragments that the LLM couldn't meaningfully reverse.

### Stagnation Detection

Fix loops now track issue count across rounds. If 2 consecutive rounds produce the same verdict with the same-or-worse issue count, the loop terminates early — avoiding wasted LLM calls on irrecoverable functions.

### Structural Diff (Non-LLM)

Before the LLM checker runs, a non-LLM structural comparison extracts call order and control flow counts from both decompile and reversed code. The summary is injected into the checker prompt as additional context:

```
Structural pre-analysis: call_count: decompile=12 reversed=10 | missing_calls: malloc, free | control_flow: decompile=8 reversed=8
```

This lets the LLM checker focus on semantic issues rather than recounting calls.

### Targeted Fix (Surgical Block Corrections)

In the block-level fix loop, checker issues are matched to specific blocks. When re-reversing, only affected blocks are sent to the LLM along with the checker's specific critique for that block. Unaffected blocks are reused from the previous round.

### Token Tracking

LLM providers now track cumulative token usage:
```python
provider.total_prompt_tokens     # Cumulative input tokens
provider.total_completion_tokens # Cumulative output tokens
provider.total_calls             # Total API calls made
```

### EFLAGS / Noise Stripping

Ghidra decompile output is cleaned before being sent to the LLM:
- `byte in_CF;` / `byte in_ZF;` … EFLAGS register declarations removed
- `undefined4 unaff_EDI;` … unaffiliated register declarations removed
- WARNING comments stripped

This reduces decompile size by ~25% and prevents the LLM from incorrectly declaring CPU flags as function-local variables.

### Flash=Translation, Pro=Reasoning

Variable mapping (semantic naming of Ghidra auto-generated variables) always uses the pro model. The flash model is used only for translation of individual blocks — never for reasoning tasks. This prevents common flash-model failures where variable types and names are misidentified.

### Stateless Checker

The checker no longer maintains conversation state between rounds. Each verification call is a fresh `[system → user]` message pair — avoiding accumulated decompile text from previous rounds that provided zero additional value.

### Gzip-Friendly Stitch Validation

`_stitch()` now validates assembled blocks: brace balance checking and block count mismatch detection produce warnings when the block reverser produces syntactically broken output.

## Requirements

- [Python 3.11+](https://www.python.org/downloads/)
- [ghidra-ai-bridge](https://github.com/Dryxio/ghidra-ai-bridge) — re-agent uses this as its backend to decompile functions, fetch xrefs, read structs/enums, and query Ghidra. Install it and point it at your Ghidra project before running `re-agent reverse`.
- One supported LLM setup:
  - `ANTHROPIC_API_KEY` for Claude
  - `OPENAI_API_KEY` for OpenAI-compatible APIs
  - a local `codex` CLI login for the Codex provider

## Installation

```bash
pip install re-agent
```

## Quick Start

```bash
# 1. Initialize project config with an ABI manifest
re-agent init --abi-manifest <PATH_TO_ABI_MANIFEST>

# 2. Edit re-agent.yaml with your LLM API key, Ghidra bridge path.
#    ⚠ The 'contracts' section is now required by ALL operational commands
#      (reverse, parity, status, pipeline, build). Without it every command
#      that loads a config file fails with a clear error.
#    See docs/configuration.md#abi-contracts for details.

# 3. Reverse a single function
re-agent reverse --address 0x6F86A0

# 4. Reverse all functions in a class
re-agent reverse --class CTrain --max-functions 10

# 5. Run the build pipeline
re-agent build

# 6. Run parity checks
re-agent parity --address 0x6F86A0

# 7. Check progress
re-agent status

# 7. Reconstruct C++ source from reversed code
re-agent build
re-agent build --phase transform --no-persist     # dry-run transform only
re-agent build --phase analyze                    # analyze only
```

## Configuration

### Layered System

re-agent uses a layered configuration system (highest priority first): CLI flags > environment variables (`RE_AGENT_*`) > `re-agent.yaml` > defaults.

### Breaking Migration: ABI Contracts

**This version introduces a breaking migration.** The `contracts` section is now **required by all operational commands** (`reverse`, `parity`, `status`, `pipeline`, `build`). Any existing `re-agent.yaml` without it is rejected with a clear error. There is no legacy fallback. See [docs/configuration.md#abi-contracts](docs/configuration.md#abi-contracts).

```yaml
contracts:
  transformation_policy: "preserve_abi"       # mandatory, only valid value
  abi_manifest_path: "abi_manifest.json"       # relative to re-agent.yaml
  abi_manifest_sha256: "abc123..."            # raw SHA-256 of manifest file
```

- `abi_manifest_path` is resolved **relative to the YAML config file's directory**.
- `abi_manifest_sha256` is the raw SHA-256 hex digest of the manifest file bytes — computed with `sha256sum`, not the manifest's internal canonical hash.
- The ABI manifest is a **generic, versioned contract format**. See [docs/configuration.md#abi-contracts](docs/configuration.md#abi-contracts) for the full schema.

### Full YAML Reference

```yaml
llm:
  provider: claude           # claude | openai | openai-compat | codex
  model: claude-sonnet-4-5-20250929
  block_model: null          # optional cheaper model for block reversals
  # api_key: set via RE_AGENT_LLM_API_KEY env var
  timeout_s: 1800

contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "abi_manifest.json"
  abi_manifest_sha256: "abc123..."

pipeline:
  state_file: "pipeline-state.json"

reverse:
  backend:
    type: ghidra-bridge
    cli_path: ~/ghidra-tools/ghidra

  project_profile:
    source_root: ./reports/re-agent/code  # for few-shot example retrieval
    hook_patterns:
      - 'RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)'
    stub_markers: ["NOTSA_UNREACHABLE"]
    stub_call_prefix: "plugin::Call"
    # project_description: "PROJECT: MyGame (2024) — Unreal Engine 5, x64 MSVC."
    # project_context: "PROJECT CONTEXT — You are decompiling MyGame..."
    # checker_custom_rules: "Additional custom verification rules..."

  parity:
    enabled: true
    call_count_warn_diff: 3

  orchestrator:
    optimize: true             # reset conversations between fix rounds
    enable_phase1: true        # false = skip Phase 1 analysis, direct code gen (-30% output tokens)
    max_review_rounds: 4
    max_functions_per_class: 10
    objective_verifier_enabled: true
    objective_call_count_tolerance: 3
    objective_control_flow_tolerance: 2
    block_reversal_enabled: true
    block_threshold_lines: 100
    block_max_lines: 40

build:
  input:
    decompiled_dir: "reports/re-agent/code/"
    ghidra_exports: ".ghidra-exports/"

  output:
    language: "cpp"
    standard: "c++23"
    compiler: "g++"
    compiler_flags: "-std=c++23 -m32 -c -Wall"
    target_dir: "output/"
    work_dir: "."
    # decls_header: ".ghidra-exports/_decls.h"   # optional declaration header

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
    max_llm_calls_per_run: 8
    max_llm_tokens_per_run: 150000
    max_compile_retry_calls_per_run: 3

  validation:
    compile_per_function: true
    compile_per_module: true
    compile_final_project: true
    max_compile_retries: 2
    target_contract_mode: "legacy"   # legacy | required

  resume:
    enabled: true
    state_path: "cr-agent-state.json"
```

See [docs/configuration.md](docs/configuration.md) for all options.

## Architecture

### Reverse Pipeline by Function Size

| Lines | Classification | Strategy |
|-------|---------------|----------|
| < 100 (leaf) | 0 calls, 0 vtables | Skip block reversal, 2 rounds, flash model |
| < 100 | Standard | Reverser → checker → fix loop |
| 100-500 | General | Block-level: flash (fast_mode) first, pro-model escalation on FAIL. Few-shot + structural diff active |
| 500-1000 | Complex | Block-level flash first, fallback to optimized standard pipeline on FAIL. Per-block targeted fix |
| > 1000 | Huge | Block-level flash only (skip LLM checker + variable mapping). Trust objective verifier |

### Model Selection

| Task | Model | Notes |
|------|-------|-------|
| Variable mapping | `llm.model` (pro) | **Always pro** — flash cannot reliably infer names from decompile |
| Checking, decomposition | `llm.model` (pro) | Semantic reasoning required |
| Block reversals (fast, small) | `llm.block_model` (flash) | Translation only — mapping already provided by pro |
| Block reversals (hybrid, large blocks) | `llm.model` (pro) | Complex logic requires pro reasoning |

## LLM Providers

- **Claude** (Anthropic SDK) — set `ANTHROPIC_API_KEY`
- **OpenAI / OpenAI-compatible** — set `OPENAI_API_KEY`, optionally set `base_url`
- **Codex CLI** — uses local `codex exec` with ChatGPT login credentials; no API key required

All providers include automatic retry with exponential backoff (3 attempts, 1s→10s cap).

## Parity Engine

The parity engine runs 11 configurable heuristic signals to verify reversed code matches the original binary:

| Signal | Level | Description |
|--------|-------|-------------|
| Missing source | RED | No source body found for hooked function |
| Stub markers | RED | Source contains stub markers (e.g., NOTSA_UNREACHABLE) |
| Trivial stub | RED | Plugin-call heavy with tiny body and no control flow |
| Large ASM tiny source | RED | ASM >= 80 instructions but source <= 12 lines |
| Plugin-call heavy | YELLOW | Plugin calls dominate the function body |
| Short body | YELLOW | Body has fewer than 6 lines |
| Low call count | YELLOW | Decompile shows many callees but source has few |
| FP sensitivity | YELLOW | ASM has floating-point ops but source doesn't |
| Call count mismatch | YELLOW | Source call count differs significantly from ASM |
| NaN logic | YELLOW | Decompile has NaN handling but source doesn't |
| Inline wrapper | INFO | Function is a thin inline wrapper |

## Objective Verifier

The reversal loop also runs a conservative structural verifier after the LLM checker passes. It only blocks acceptance on strong mismatches such as:

- call-count gaps between candidate code and decompile/ASM
- control-flow gaps where the candidate is clearly missing branches or loops

This is intentionally narrower than full equivalence checking, but it catches obvious false positives before they are recorded as successful reversals.

## Safety

- **No auto-commit**: re-agent writes code but never commits or pushes
- **Bounded retries**: Hard cap on fix loop iterations (default: 4)
- **API retry**: 3 retries with exponential backoff on transient failures
- **Conversation cleanup**: History freed after each function reversal
- **Deterministic logs**: Every LLM call logged with timestamps
- **Reverse / no-persist are safe**: The `reverse` phase and `build --no-persist` never delete files, modify git, or compile. However, `build --phase assemble` deletes and recreates `target_dir`; `build --phase transform` (persist mode) compiles per-function and writes temp files. LLM calls in no-persist are billed normally.
- **Session isolation**: Progress appended, never overwritten

## Customizing Prompts

### Reverse Prompts

All prompt templates for the reverse pipeline live in `src/re_agent/reverse/agents/prompts/` as `.md` files using `$variable` placeholders:

```bash
ls src/re_agent/reverse/agents/prompts/
# block_reverser_system.md  block_reverser_task.md  checker_system.md  checker_task.md
# decompose_system.md  decompose_task.md  fix_instructions.md
# reverser_system.md  reverser_task.md  varmap_system.md  varmap_task.md
```

### Build Prompts

Build prompt templates live in `src/re_agent/build/prompts/`:

```bash
ls src/re_agent/build/prompts/
# transform_system.md  transform_task.md  repair_system.md
```

Variables available in all system prompts:
- `$project_description` — from `project_profile.project_description`
- `$project_context` — from `project_profile.project_context` (reverser only)
- `$custom_rules` — from `project_profile.checker_custom_rules` (checker only)

## Development

```bash
git clone https://github.com/Spoutnik87/re-agent.git
cd auto-re-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest tests/
ruff check src/
mypy src/re_agent/
```

## License

MIT
