# re-agent

Autonomous reverse-engineering agent with a project build surface for
reconstructing C++ source trees. It combines a reverser/checker loop, Ghidra
integration, parity checks, and verified project transformation.

## Overview

```
re-agent reverse
    │
    ├── Config and project profile
    ├── Reverser → checker → bounded fix loop
    ├── Objective verifier and parity engine
    └── RE backend

project root
    └── transform → link/package → evidence → atomic publication
```

Reverse and parity remain usable independently of project build publication.

## CLI reference

| Command | Description |
|---------|-------------|
| `re-agent init --abi-manifest PATH` | Generate a config file |
| `re-agent reverse --class NAME` | Reverse functions in a class |
| `re-agent reverse --dry-run` | Show what would be reversed |
| `re-agent parity --filter REGEX` | Run parity checks |
| `re-agent status` | Show reversal progress |
| `re-agent pipeline --skip-build` | Run reverse only |
| `re-agent build --project-root PATH --phase transform` | Transform a complete project |
| `re-agent build --project-root PATH --phase link` | Link verified staged output |
| `re-agent build --project-root PATH --phase package` | Package verified output |
| `re-agent build --project-root PATH --phase verify-recipe` | Verify the recipe without publication |

## Project build

The legacy direct build mode is removed. Build commands require
`--project-root`; direct config/CWD execution is no longer a build interface.
The project root owns the input snapshot, contract, recipe, intermediate state,
toolchain identity, evidence, and publication area.

The supported phases are `transform`, `link`, `package`, and `verify-recipe`.
There is no project analysis phase. Project mode rejects legacy per-function,
partial-work, and partial-publication selectors.

```bash
re-agent build --project-root PATH_TO_PROJECT --phase transform
re-agent build --project-root PATH_TO_PROJECT --phase verify-recipe
re-agent build --project-root PATH_TO_PROJECT --phase link
re-agent build --project-root PATH_TO_PROJECT --phase package
```

Toolchains are resolved from the project's activated profile by default. Pass
`--profile PATH` for a transient, one-shot profile; transient resolution writes
no activation state. Both modes fingerprint the required binaries and record
their identities in evidence.

Transform and publication are persistent and fail closed. Evidence must cover
the complete project and pass identity, recipe, and toolchain checks before
publication. Publication is atomic and all-or-nothing; failed or incomplete
runs never replace the active build.

## Configuration

Configuration priority is CLI flags > `RE_AGENT_*` environment variables >
`re-agent.yaml` > defaults. The `contracts` section is required by operational
commands that load a config. See [docs/configuration.md](docs/configuration.md).

```yaml
contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "abi_manifest.json"
  abi_manifest_sha256: "<64-character SHA-256>"
```

Project build defaults live under `build:`, but are consumed in the context of
an explicit verified project root. They do not create a standalone CWD build.

## Quick start

```bash
# Initialize configuration
re-agent init --abi-manifest PATH_TO_ABI_MANIFEST

# Reverse and inspect independently
re-agent reverse --class NAME --max-functions 10
re-agent parity --limit 50
re-agent status

# Build from an owned project root
re-agent build --project-root PATH_TO_PROJECT --phase transform
re-agent build --project-root PATH_TO_PROJECT --phase package
```

## Reverse pipeline features

- Few-shot retrieval of structurally similar successful reversals.
- Pre-classification for leaf, getter/setter, vtable-heavy, Win32, and complex
  state-machine functions.
- Block-level reversal for large functions with targeted fixes.
- Structural call/control-flow comparison before checker acceptance.
- Stagnation detection and bounded retries.
- Objective verification and configurable parity signals.
- Claude, OpenAI-compatible, and Codex providers with retry/backoff.

## Parity engine

The parity engine uses configurable heuristic signals including missing source,
stub markers, trivial bodies, call-count differences, floating-point
sensitivity, NaN handling, and inline wrappers. It is a conservative review
gate, not full semantic equivalence checking.

## Safety

- No auto-commit or auto-push.
- Reverse and parity commands do not publish project builds.
- Project builds use owned staging, verified evidence, and atomic publication.
- Activated toolchains are authenticated through their hash chain; transient
  profiles are never persisted.

## Requirements and installation

- Python 3.11+
- A configured ghidra-ai-bridge backend for reversal.
- One supported LLM setup: Anthropic, OpenAI-compatible, or Codex CLI.

```bash
pip install re-agent
```

## Development

```bash
git clone https://github.com/Spoutnik87/re-agent.git
cd re-agent
python -m venv .venv
python -m pip install -e ".[dev]"
pytest tests/
ruff check src/
mypy src/re_agent/
```

## License

MIT
