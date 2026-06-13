# re-agent (fork)

Autonomous reverse-engineering agent — source-aware reverser/checker loop, block-level decomposition, configurable prompts, few-shot learning, objective verifier, and Ghidra backend.

This is a fork with significant enhancements: block-level decompilation for large functions, few-shot example retrieval (5700+ indexed successes), pre-classification for strategy selection, configurable prompt templates, token-optimized LLM interactions (+ retry logic), and an extended test suite.

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
  enable_phase1: true        # false = skip Phase 1 analysis, direct code gen (-30% output tokens)
  max_review_rounds: 4
  max_functions_per_class: 10
  objective_verifier_enabled: true
  objective_call_count_tolerance: 3
  objective_control_flow_tolerance: 2
  block_reversal_enabled: true
  block_threshold_lines: 100
  block_max_lines: 40

project_profile:
  source_root: ./reports/re-agent/code  # for few-shot example retrieval
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
