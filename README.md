# re-agent (fork)

Autonomous reverse-engineering agent — source-aware reverser/checker loop, block-level decomposition, configurable prompts, objective verifier, parity engine, and Ghidra backend.

This is a fork with significant enhancements over the original: block-level decompilation for large functions, configurable prompt templates, token-optimized LLM interactions, retry logic, and an extended test suite.

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
    │   ├── Function Picker (ranks by caller count, filters completed)
    │   ├── Context Gatherer (decompile + xrefs + structs + source retrieval)
    │   │
    │   ├── Block-Level Pipeline (>100 lines)
    │   │   ├── Block Splitter (syntactic decomposition at depth-0 boundaries)
    │   │   ├── Variable Mapping (one pro LLM call → shared across all blocks)
    │   │   ├── Block Reverser (per-block reversal with 3-block context window)
    │   │   ├── Skeleton Generator (signature + locals + block placeholders)
    │   │   └── Recursive Decomposer (LLM-driven for >200-line functions)
    │   │
    │   ├── Agent Loop (reverser → checker → fix, max N rounds)
    │   │   ├── LLM Providers: Claude | OpenAI-compatible APIs | Codex CLI
    │   │   ├── Prompt Templates (customizable .md files with $variables)
    │   │   └── Retry with exponential backoff (3 attempts, 1s→10s)
    │   │
    │   ├── Objective Verifier (call-count + control-flow sanity checks)
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

## Key Enhancements (this fork)

### Block-Level Decompilation
Large functions (>100 lines) are automatically split into independent blocks, reversed in parallel with shared variable naming, then stitched back together. This reduces token usage by ~80% compared to reversing the full function in one LLM call.

- **Syntactic splitter**: Splits at `if`/`else`/`for`/`while`/`switch` boundaries at brace-depth 0
- **LLM recursive decomposer**: For functions >200 lines, asks the LLM to identify logical sub-sections
- **Variable mapping**: One pro-model call generates a naming map shared across all blocks
- **3-block context window**: Each block only sees the last 3 reversed blocks, preventing O(n²) token bloat

### Configurable Prompt Templates
All system prompts use `string.Template` variables instead of hardcoded project references:

| Variable | Scope | Config Field |
|----------|-------|-------------|
| `$project_description` | checker, block_reverser, varmap, decompose | `project_profile.project_description` |
| `$project_context` | reverser | `project_profile.project_context` |
| `$custom_rules` | checker | `project_profile.checker_custom_rules` |

Example in `re-agent.yaml`:
```yaml
project_profile:
  project_description: "PROJECT: MyGame (2024) — Unreal Engine 5, x64 MSVC."
  project_context: "PROJECT CONTEXT — You are decompiling MyGame. Patterns: UE5 actor system, UObject hierarchy..."
  checker_custom_rules: "- Verify FString/FName semantics match engine conventions"
```

### Token-Optimized LLM Interactions
- **Task prompts are data-only**: All methodology lives in system prompts, task prompts contain only parameters
- **No triple-send in fix loop**: Only parsed issues+fix_instructions sent, not the full checker report
- **Conversation reset between fix rounds**: Prevents linear history accumulation (4x bloat in default config)
- **Retry with exponential backoff**: 3 attempts with 1s→2s→4s→10s cap on transient API failures
- **Conversation cleanup**: `delete_conversation()` frees accumulated history after each function

### Success/Failure Improvements
- **Checker tolerance guidance**: Counting warnings relaxed — minor structural differences with equivalent semantics are accepted
- **Large function fallback**: Functions >500 lines that fail block reversal get a standard pipeline retry (optimized, stateless)
- **Objective verifier as quality gate**: Free sanity check runs before the LLM checker to catch obvious misses

## Requirements

- Python 3.10+
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
# 1. Initialize project config
re-agent init

# 2. Edit re-agent.yaml with your project settings
#    Add project_description and project_context for better results

# 3. Reverse a single function
re-agent reverse --address 0x6F86A0

# 4. Reverse all functions in a class
re-agent reverse --class CTrain --max-functions 10

# 5. Run parity checks
re-agent parity --address 0x6F86A0

# 6. Check progress
re-agent status
```

## Configuration

re-agent uses a layered configuration system (highest priority first): CLI flags > environment variables (`RE_AGENT_*`) > `re-agent.yaml` > defaults.

```yaml
llm:
  provider: claude           # claude | openai | openai-compat | codex
  model: claude-sonnet-4-5-20250929
  block_model: null          # optional cheaper model for block reversals
  # api_key: set via RE_AGENT_LLM_API_KEY env var
  timeout_s: 1800

backend:
  type: ghidra-bridge
  cli_path: ~/ghidra-tools/ghidra

orchestrator:
  optimize: true             # reset conversations between fix rounds
  max_review_rounds: 4
  max_functions_per_class: 10
  objective_verifier_enabled: true
  objective_call_count_tolerance: 3
  objective_control_flow_tolerance: 2
  block_reversal_enabled: true
  block_threshold_lines: 100
  block_max_lines: 40

project_profile:
  source_root: ./source/game_sa
  hook_patterns:
    - 'RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)'
  stub_markers: ["NOTSA_UNREACHABLE"]
  stub_call_prefix: "plugin::Call"
  # project_description: "PROJECT: MyGame (2024) — Unreal Engine 5, x64 MSVC."
  # project_context: "PROJECT CONTEXT — You are decompiling MyGame..."
  # checker_custom_rules: "Additional custom verification rules..."
```

See [docs/configuration.md](docs/configuration.md) for all options.

## Architecture

### Pipeline by Function Size

| Lines | Strategy |
|-------|----------|
| < 100 | Standard reverser → checker → fix loop |
| 100-500 | Block-level: flash (fast_mode) first, pro-model escalation on FAIL |
| 500-1000 | Block-level flash first, fallback to optimized standard pipeline on FAIL |
| > 1000 | Block-level flash only (skip LLM checker + variable mapping) |

### Model Selection

| Task | Model |
|------|-------|
| Checking, variable mapping, decomposition | `llm.model` (pro) |
| Block reversals (fast_mode) | `llm.block_model` if set, else `llm.model` |
| Block reversals (hybrid, large blocks) | `llm.model` (pro) |

## CLI Reference

| Command | Description |
|---------|-------------|
| `re-agent init` | Generate `re-agent.yaml` config file |
| `re-agent reverse --address ADDR` | Reverse a single function |
| `re-agent reverse --class CLASS` | Reverse all functions in a class |
| `re-agent reverse --address ADDR --no-optimize` | Disable token optimizations |
| `re-agent reverse --dry-run` | Show what would be reversed |
| `re-agent parity --address ADDR` | Run parity checks on a function |
| `re-agent parity --filter REGEX` | Run parity checks matching pattern |
| `re-agent status` | Show reversal progress |
| `re-agent status --class CLASS` | Show progress for a specific class |

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
- **No destructive ops**: Never deletes files, modifies git, or runs builds
- **Session isolation**: Progress appended, never overwritten

## Customizing Prompts

All prompt templates live in `src/re_agent/agents/prompts/` as `.md` files using `$variable` placeholders. You can edit them directly:

```bash
ls src/re_agent/agents/prompts/
# block_reverser_system.md  block_reverser_task.md  checker_system.md  checker_task.md
# decompose_system.md  decompose_task.md  fix_instructions.md  rename_system.md
# rename_task.md  reverser_system.md  reverser_task.md  skeleton_system.md
# skeleton_task.md  varmap_system.md  varmap_task.md
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
