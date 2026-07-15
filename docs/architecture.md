# Architecture

re-agent has two related surfaces: reverse engineering and project build.

```
CLI → Config → Orchestrator → Agent Loop → LLM Providers
                   │                │
                   ▼                ▼
             Function Picker   RE Backend
                   │
                   ▼
              Parity Engine

Project root → transform → link/package
                    │
                    ▼
              evidence → atomic publication
```

## Reverse pipeline

The reverse pipeline is independent of project build publication:

CLI → Config → Orchestrator → Agent Loop → LLM Providers + RE Backend

### Layers

- **CLI**: `init`, `reverse`, `parity`, `status`, and pipeline entry points.
- **Config**: YAML, environment, and CLI overlays, project profiles, and ABI
  contracts.
- **Orchestrator**: single-function or class-level reversal.
- **Agents**: reverser and checker with a bounded fix loop.
- **LLM**: protocol-based Claude, Codex, and OpenAI-compatible providers.
- **Backend**: RE-tool abstraction with capability flags.
- **Parity**: configurable heuristic verification and scoring.
- **Reports**: JSON/Markdown output and session tracking.

## Project build architecture

The legacy direct build mode is removed. Project builds require an explicit
`--project-root`; all inputs, intermediate state, toolchain identity, evidence,
and publication metadata are owned by that root rather than the process CWD.

The project build surface is intentionally limited:

1. **Transform** creates a complete staged result and its evidence.
2. **Link/package** consume verified staged output.
3. **Verify-recipe** executes a recipe witness without publishing a build.

There is no project analysis phase. Project mode rejects legacy per-function,
partial-work, and partial-publication selectors.

### Toolchain profiles

Without `--profile`, the project authenticates its activated
`toolchain/active.link → profile → fingerprint → binaries` chain. With
`--profile PATH`, it resolves a transient profile for that invocation only;
the transient path writes no activation state. Required compiler and linker
binaries are fingerprinted in either mode, and their identities become part
of the build evidence.

### Evidence and publication

Each run stages output under the project build area and records identity for
the project snapshot, contract, configuration, recipe, and toolchain. Evidence
is validated as a complete set before publication. Missing, stale, mismatched,
or failed evidence rejects the run. Publication is atomic and all-or-nothing:
a failed run cannot replace the active build, and partial results are never
published.

## Prompt architecture

Prompts live in two distinct directories:

### Reverse prompts

Located at `src/re_agent/reverse/agents/prompts/`:

| File | Purpose |
|------|---------|
| `reverser_system.md` | Main reverser system prompt |
| `reverser_task.md` | Per-function reversal task |
| `checker_system.md` | Verification checker system prompt |
| `checker_task.md` | Verification task |
| `block_reverser_system.md` | Block-level reversal system prompt |
| `block_reverser_task.md` | Block decomposition task |
| `decompose_system.md` | Function decomposition system prompt |
| `decompose_task.md` | Split guidance |
| `varmap_system.md` | Variable mapping system prompt |
| `varmap_task.md` | Variable mapping task |
| `fix_instructions.md` | Compile-error fix instructions |

### Project build prompts

Located at `src/re_agent/build/prompts/`:

| File | Purpose |
|------|---------|
| `transform_system.md` | Project transformation system prompt |
| `transform_task.md` | Project context and transformation task |
| `repair_system.md` | Compile-error repair mode |

## Contracts layer

The contracts layer pins the ABI surface through an external manifest. The
manifest is validated and pinned at config load time before an operational
command proceeds.

### Key properties

- **Generic**: the manifest format supports multiple architectures and is not
  tied to a particular binary.
- **Versioned**: manifests carry a semantic version.
- **Self-hashing**: the internal hash covers canonical JSON with itself blanked.
- **Fail-fast**: missing policy, bad hashes, unknown keys, and unsafe paths fail
  during loading.

The `contracts` section is mandatory for `reverse`, `parity`, `status`, and
other commands that load `re-agent.yaml`; `init` is the only command that can
start without an existing config.

See [configuration.md](configuration.md) for the schema and project build
configuration.
