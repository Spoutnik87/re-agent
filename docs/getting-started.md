# Getting Started

## Installation

```bash
pip install re-agent
```

## Quick Start

1. Initialize configuration:
```bash
re-agent init
```

2. Edit `re-agent.yaml` with your LLM API key and Ghidra bridge path.

3. Reverse a single function:
```bash
re-agent reverse --address 0x6F86A0 --class CTrain
```

4. Reverse a full class:
```bash
re-agent reverse --class CTrain --max-functions 5
```

5. Run parity checks:
```bash
re-agent parity --limit 50
```

6. Check progress:
```bash
re-agent status
```

## Quick Start: Build Pipeline

After reversing functions, assemble a C++ source tree:

```bash
# Run all three phases sequentially
re-agent build

# Run an isolated phase (must run analyze before transform before assemble)
re-agent build --phase analyze
re-agent build --phase transform
re-agent build --phase assemble

# Transform a specific module (must run analyze first)
re-agent build --phase analyze
re-agent build --phase transform --module module_1

# Target a single module from a specific subunit index
re-agent build --phase transform --module module_1 --subunit 5

# Limit total work (across all modules)
re-agent build --max-subunits 10

# Tag the run for diagnostics traceability
re-agent build --run-id "fix-typedef-pass-1"

# Dry-run transform: no disk writes, stdout JSON only
re-agent build --phase transform --no-persist
```

### Build Phases

| Phase | Command | Output |
|-------|---------|--------|
| **Analyze** | `re-agent build --phase analyze` | `modules.json` (call graph, clusters, sub-units) |
| **Transform** | `re-agent build --phase transform` | `temp_transformed/`, compiled `.cpp` files |
| **Assemble** | `re-agent build --phase assemble` | `output/` (source tree, CMake) |

Omitting `--phase` runs all three in sequence. Each phase depends on the previous:
analyze must complete before transform, transform before assemble.

### Module and Subunit Targeting

```bash
# Restrict transform to a single module
re-agent build --phase transform --module MyModule

# Start at a specific subunit index within a module (requires --module)
re-agent build --phase transform --module MyModule --subunit 5

# Process at most N subunits globally (stops mid-module if hit)
re-agent build --max-subunits 20

# Combine targeting
re-agent build --phase transform --module MyModule --subunit 3 --max-subunits 5
```

- `--module` filters by module name from `modules.json`
- `--subunit` is a 0-based index; skipped subunits are NOT marked as completed
- `--max-subunits` applies globally across ALL modules, not per-module

### Run Identifiers

```bash
re-agent build --run-id "my-experiment-001"
```

The `--run-id` is forwarded to diagnostics and evidence paths for traceability
(WorkPacket JSON files under `optimization.diagnostics_dir`).

## --no-persist Mode

`--no-persist` runs only the transform phase in dry-run mode with strict
safety guarantees for evaluation, staging, or CI.

```bash
re-agent build --phase transform --no-persist
```

### Guarantees

| Concern | Behavior |
|---------|----------|
| Disk writes | **Zero**. No temp dirs, no cache, no state, no report, no compiled `.o` files |
| Output channel | Single JSON document on **stdout** |
| Human messages | All informational messages go to **stderr** |
| Disk side effects | No files created, modified, or deleted; LLM calls still run and may be billed |
| Bundle | Bundled by `--module`, `--subunit`, `--max-subunits`, `--run-id` |

### Restrictions

- `--no-persist` is **only valid with `--phase transform`**
- Using `--no-persist` with `--phase analyze`, `--phase assemble`, or without
  `--phase` exits with code 2
- Compilation is skipped entirely (would create temp `.o` files)
- Caching is disabled (would write to `cache_path`)
- Resume state is neither loaded nor saved

### Exit Codes

| Code | Meaning | Condition |
|------|---------|-----------|
| 0 | No contract failure | Transform completed with no hard failure; matching functions are `SKIPPED_COMPILE` |
| 2 | Contract failed | Budget, provider, hard-reject, or TARGET-contract failure |

Exit code 1 is not produced in no-persist mode.

### Output Format (–no-persist stdout JSON)

The stdout is a single JSON object with this structure:

```json
{
  "run_type": "no-persist",
  "exit_code": 0,
  "summary": {
    "total": 12,
    "passed": 0,
    "failed": 12,
    "incomplete": 0,
    "hard_rejects": 0,
    "budget_exceeded": 0,
    "provider_errors": 0,
    "contract_failed": false
  },
  "usage": {
    "prompt_tokens": 45231,
    "completion_tokens": 12890,
    "total_calls": 8
  },
  "budget": {
    "calls_remaining": 0,
    "tokens_remaining": 91879,
    "compile_retry_calls_remaining": 3,
    "exceeded": false,
    "exceeded_reason": ""
  },
  "results": [
    {
      "function": "0x6F86A0",
      "verdict": "SKIPPED_COMPILE",
      "compiles": false,
      "files_matched": 1,
      "match_strategy": "explicit_identity",
      "identity_state": "explicit",
      "identity_reason": "",
      "compile_error_category": null,
      "files": [{"path": "module_1/0x6F86A0__CTrain_Update.cpp"}]
    }
  ]
}
```

The JSON does NOT include prompts, raw code, unbounded stderr, or secrets.

### Verdicts

| Verdict | Meaning |
|---------|---------|
| `PASS` | Compiled successfully (persist mode) |
| `PASS_RETRY` | Compiled after compile retry (persist mode) |
| `SKIPPED_COMPILE` | Compilation intentionally skipped in no-persist mode |
| `NO_OUTPUT` | LLM failed to produce valid output or TARGET violated |
| `INCOMPLETE_TARGETS` | Recovery exhausted; mandatory TARGETs not met (required mode) |
| `BUDGET_EXCEEDED` | Global budget ran out during processing |
| `PROVIDER_ERROR` | LLM provider error (rate limit, timeout, server error) |
| `FAIL_NO_RETRY` | Compile error, no retries remaining |
| `FAIL_AFTER_RETRY` | Compile error persisted after LLM repair |

### Parsing from Shell

```bash
# Capture stdout JSON, stderr goes to terminal
re-agent build --phase transform --no-persist > results.json

# Or pipe to jq for verification
re-agent build --phase transform --no-persist | python -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['exit_code'] == 0 else d['exit_code'])"
```
