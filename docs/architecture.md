# Architecture

re-agent is structured as a layered pipeline with three distinct phases for code
reconstruction (Phase 2: Build) alongside the reverse-engineering pipeline (Phase 1).

```
CLI → Config → Orchestrator → Agent Loop → LLM Providers
                  |                |
                  v                v
          Function Picker     RE Backend (Ghidra)
                  |
                  v
           Parity Engine

                  │
                  ▼
          Build Pipeline
    analyze → transform → assemble
```

## Phase 1 — Reverse

CLI → Config → Orchestrator → Agent Loop → LLM Providers + RE Backend (Ghidra)

### Layers

- **CLI**: argparse entry points (init, reverse, build, parity, status)
- **Config**: YAML + env + CLI overlay, project profiles, **ABI contracts** (breaking migration)
- **Orchestrator**: Single function or class-level auto-advance
- **Agents**: Reverser + Checker with fix loop
- **LLM**: Protocol-based providers (Claude, Codex, OpenAI-compat)
- **Backend**: RE tool abstraction with capability flags
- **Parity**: 11-signal verification engine with scoring
- **Reports**: JSON/markdown output, session tracking

---

## Build Pipeline Architecture (Phase 2)

The build pipeline produces a C++ source tree from flat decompiled `.cpp` files.
It runs in three sequential phases:

```
  .cpp files
       │
       ▼
┌─────────────────────────────┐
│  Phase 1: Analyze           │
│                             │
│  build_graph()  ── call     │
│                   graph     │
│       │                     │
│       ▼                     │
│  cluster()  ── networkx     │
│               Louvain       │
│       │                     │
│       ▼                     │
│  index_modules() ── TF-IDF  │
│   (scikit-learn)  subunit   │
│                    grouping │
│       │                     │
│       ▼                     │
│  write_decls_header()  (opt)│
└───────────┬─────────────────┘
            │ modules.json
            ▼
┌─────────────────────────────┐
│  Phase 2: Transform         │
│                             │
│  process_modules()          │
│       │                     │
│       ▼                     │
│  For each module:           │
│    build_context()          │
│    process_subunit()        │
│      ├── LLM generation     │
│      ├── TARGET recovery    │
│      ├── Compile gate       │
│      └── Retry logic        │
│       │                     │
│       ▼                     │
│  TransformBudget (global)   │
│  ─ calls_remaining          │
│  ─ tokens_remaining         │
│  ─ compile_retry_remaining  │
└───────────┬─────────────────┘
            │ temp_transformed/
            ▼
┌─────────────────────────────┐
│  Phase 3: Assemble          │
│                             │
│  build_tree()               │
│    ├── Copy .cpp → src/     │
│    ├── Copy .h → include/   │
│    ├── generate_common_hdr  │
│    ├── resolve_conflicts    │
│    └── generate_cmake       │
└─────────────────────────────┘
            │
            ▼
      output/
       ├── src/module_1/*.cpp
       ├── include/module_1/*.h
       ├── CMakeLists.txt
       └── cr-agent-report.json
```

### Phase 1: Analyze

Scans all decompiled `.cpp` files in `input.decompiled_dir` and produces a
`modules.json` with function-to-module mapping and sub-unit groupings.

| Step | Tool | Output |
|------|------|--------|
| `build_graph()` | Regex-based call detection (FUN_ and `0x` patterns) | Undirected call graph `dict[addr, set[addr]]` |
| `cluster()` | **networkx** graph + **Louvain** community detection (`louvain_communities`) | Module clusters with min/max size enforcement, orphan detection |
| `index_modules()` | **scikit-learn** `TfidfVectorizer` + `cosine_similarity` | Sub-unit grouping by TF-IDF similarity within each module |
| `write_decls_header()` | Function declaration extraction | Optional declaration header when `output.decls_header` is configured |

Modules that exceed `max_cluster_size` are re-clustered once with Louvain
at the subgraph level. Functions in clusters below `min_cluster_size` are
demoted to orphans.

### Phase 2: Transform

Sends each sub-unit to the LLM for code refinement. The global `TransformBudget`
is shared across ALL subunits within a single invocation.

**Components:**

| Component | Source | Role |
|-----------|--------|------|
| `TransformBudget` | `src/re_agent/build/transform/subunit_processor.py` | Per-invocation call/token/retry budget |
| `build_context()` | `src/re_agent/build/transform/context_builder.py` | Assembles neighbour context + functions to transform |
| `process_subunit()` | `src/re_agent/build/transform/subunit_processor.py` | Single subunit: LLM gen → TARGET recovery → compile gate |
| `TransformCache` | `src/re_agent/build/state/cache.py` | Source-hash → result cache to avoid re-processing |
| `module_processor.py` | `src/re_agent/build/transform/` | Module-level orchestration, resume, persist vs no-persist |

**TransformBudget** tracks three counters initialized from `build.optimization`:

```
calls_remaining: 8               # max_llm_calls_per_run
tokens_remaining: 150000         # max_llm_tokens_per_run
compile_retry_calls_remaining: 3 # max_compile_retry_calls_per_run
```

When the call or token counter reaches zero (or a token delta exhausts the budget between calls),
further LLM sends are rejected with `BUDGET_EXCEEDED`. A zero compile-retry counter
only disables compile retries. Provider errors increment
`provider_error_count` but do NOT zero the budget — the run continues until the
call cap is hit.

**Target Contract Mode** (`validation.target_contract_mode`):

- `legacy`: fallback is permitted only when no TARGET markers appear; partial or invalid markers reject identity.
- `required`: TARGET mandatory for every function. Only partial, valid coverage enters recovery; missing or invalid markers fail immediately. Failed recovery →
  `INCOMPLETE_TARGETS` → subunit rejected (no files written, no cache populated).
  This is a **fail-closed** protocol.

### Phase 3: Assemble

Copies transformed files into a C++ source tree and generates build artifacts.

| Step | Description |
|------|-------------|
| `build_tree()` | Creates `src/<module>/` and `include/<module>/`, copies `.cpp` / `.h` files |
| `generate_common_header()` | Extracts function declarations from headers into `include/common.h` |
| `resolve_conflicts()` | Reports cross-module symbol conflicts; it does not resolve them |
| `generate_cmake()` | Writes `CMakeLists.txt` with static library targets per module |

The current orchestration does not make module compile warnings or all `FAIL_*` results
block Assemble; inspect reports before treating the tree as buildable. `compile_final_project`
is configured but not currently applied.

### Cache and State

| Artifact | Path | Written when |
|----------|------|-------------|
| `modules.json` | `{work_dir}/` | After analyze phase |
| `cr-agent-cache.json` | `{optimization.cache_path}` | After each successful transform (persist mode) |
| `cr-agent-state.json` | `{resume.state_path}` | After each subunit (persist mode), updated by module |
| `cr-agent-report.json` | `{work_dir}/` | After transform (persist mode) |
| `pipeline-state.json` | `{pipeline.state_file}` | Pipeline progress (persist mode) |

**`--no-persist` guarantee**: With `--no-persist`, NONE of the above artifacts
are written to disk. No cache is created, no state loaded or saved, no report
written, no temp directories created, and no compilation executed. LLM calls still
run and consume budget. Output is a single JSON document on stdout.

### Outputs

| Output | Path | Content |
|--------|------|---------|
| Source files | `{target_dir}/src/<module>/*.cpp` | Transformed C++ source |
| Headers | `{target_dir}/include/<module>/*.h` | Module headers |
| Common header | `{target_dir}/include/common.h` | Aggregated declarations |
| CMakeLists.txt | `{target_dir}/CMakeLists.txt` | CMake build definition |
| Build report | `{target_dir}/cr-agent-report.json` | Per-function verdicts + summary |

---

## Prompt Architecture

Prompts live in two distinct directories with no overlap:

### Reverse Prompts (Phase 1)

Located at `src/re_agent/reverse/agents/prompts/`:

| File | Purpose |
|------|---------|
| `reverser_system.md` | System prompt for the main reverser agent |
| `reverser_task.md` | Task prompt with per-function reversal instructions |
| `checker_system.md` | System prompt for the parity checker agent |
| `checker_task.md` | Task prompt for verification checks |
| `block_reverser_system.md` | System prompt for block-level reversal |
| `block_reverser_task.md` | Task prompt for block decomposition |
| `decompose_system.md` | System prompt for function decomposition |
| `decompose_task.md` | Task prompt for split guidance |
| `varmap_system.md` | System prompt for variable map extraction |
| `varmap_task.md` | Task prompt for variable mapping |
| `fix_instructions.md` | Step-by-step fix instructions for compile errors |

### Build Prompts (Phase 2)

Located at `src/re_agent/build/prompts/`:

| File | Purpose |
|------|---------|
| `transform_system.md` | System prompt for code reconstruction agent |
| `transform_task.md` | Task prompt with module context + functions to transform |
| `repair_system.md` | System prompt for compile-error repair mode |

---

## Contracts Layer

The contracts layer pins the binary's ABI surface via an external manifest.
The manifest is **validated and pinned at config load time**, but its symbols
are **not yet consumed by the Transform phase**. This is the foundational step:
the manifest must exist, be valid, and pass integrity checks before any
operational command proceeds.

```
re-agent.yaml                    abi_manifest.json
┌────────────────────┐          ┌─────────────────────┐
│ contracts:         │  path→   │ version: "1.0.0"    │
│   transformation_  │  resolve │ architecture: "x86" │
│     policy:        │  ───────→│ pointer_size: 4     │
│     "preserve_abi" │  rel YAML│ symbols: [...]      │
│   abi_manifest_    │  dir     │ sha256_hash: "..."  │
│     path: "..."    │          └─────────────────────┘
│   abi_manifest_    │               │
│     sha256: "..."  │  raw hash     │ canonical hash
│                   │  verifies │    │ verifies
└────────┬──────────┘  ────────┘    ┘
         │           load_config()   load_manifest()
         │           fail-fast       fail-fast
         v
   All operational commands
   (reverse / parity / status / pipeline / build)
```

### Key Properties

- **Generic**: the `AbiManifest` model supports x86, x64, arm, aarch64 — not
  tied to any specific binary. The core contains **no target-specific rules**.
- **Versioned**: manifests carry a semver `version` string.
- **Self-hashing**: the internal `sha256_hash` is computed over canonical JSON
  with itself blanked, enabling tamper detection.
- **Fail-fast**: missing policy, bad hash, unknown keys, path traversal — all
  raise immediately during config or manifest loading.

### Breaking Migration

The `contracts` section is **mandatory** when loading `re-agent.yaml`.
This means every operational command — `reverse`, `parity`, `status`,
`pipeline`, `build` — enforces it. Only `re-agent init` can be run without
a pre-existing config.

`contracts.transformation_policy` must be `"preserve_abi"`. Any config
without it is rejected with a clear error. There is no legacy fallback.

Use `re-agent init --abi-manifest <PATH>` to generate a config with the
contracts section pre-populated.

### Preserve-ABI Mode semantics

When `contracts.transformation_policy` is `"preserve_abi"`,
`re-agent build --phase transform --address 0xADDRESS` interface activates
single-function manifest binding. The run performs these steps:

1. Resolves `0xADDRESS` against the ABI manifest's `symbols` array.
2. Supplies the manifest entry's declared signature and calling convention to the prompt, and validates the exact output path.
3. Runs the LLM transform on the single function.
4. Compiles the output.

A successful compilation produces the composite verdict **MANIFEST_BOUND/COMPILE_PASS**:

- **MANIFEST_BOUND**: the function address was matched to a manifest entry and
  the response uses its exact declared output path. Signature and calling
  convention are prompt constraints, not independently verified evidence.
- **COMPILE_PASS**: the generated code compiles successfully under the
  configured compiler flags.

This verdict is **neither an ABI proof nor a behavioral proof**. It does not
execute the compiled code, compare it against the original binary's disassembly,
or verify semantic equivalence. It is a build-quality gate, not a correctness
verification.

**Current refusals:** the following invocations are rejected with exit code 2
and a diagnostic message before any LLM call or disk operation:

| Invocation | Refused | Reason |
|------------|---------|--------|
| `re-agent build` (no `--phase`) | Yes | Bulk all-phase run cannot satisfy per-address manifest binding |
| `re-agent build --phase analyze` | No | Analysis remains available; it does not transform or publish ABI-bound code |
| `re-agent build --phase assemble` | Yes | Expects a full module tree, incompatible with single-function binding |
| `re-agent build --phase transform` (no `--address`) | Yes | Bulk transform processes multiple subunits, not a single entry |
| `re-agent build --phase transform --address 0xADDRESS` | No | Single-function manifest-bound transform — the only accepted form |

See [docs/configuration.md#abi-contracts](docs/configuration.md#abi-contracts) for the full
configuration reference.
