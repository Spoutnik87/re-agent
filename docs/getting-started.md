# Getting Started

> **⚠ BREAKING MIGRATION** — The `contracts` section in `re-agent.yaml` is
> now **required by all operational commands** (`reverse`, `parity`, `status`,
> `pipeline`, `build`). See
> [configuration.md#abi-contracts](configuration.md#abi-contracts).

## Installation

Requires Python 3.11+. Install via pip:

```bash
pip install re-agent
```

## Quick Start

1. Initialize configuration with your ABI manifest:
```bash
re-agent init --abi-manifest <PATH_TO_ABI_MANIFEST>
```

2. Edit `re-agent.yaml`: add your LLM API key and Ghidra bridge path. The
   required `contracts` section is populated by `init --abi-manifest`:

```yaml
contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "abi_manifest.json"
  abi_manifest_sha256: "<64-char SHA-256 of your manifest file>"
```

The manifest is a generic JSON file describing your binary's ABI surface.
Create it externally, then compute its SHA-256:

```bash
sha256sum abi_manifest.json
```

3. Reverse a single function:
```bash
re-agent reverse --class NAME
```

4. Reverse a full class:
```bash
re-agent reverse --class NAME --max-functions 5
```

5. Run parity checks:
```bash
re-agent parity --limit 50
```

6. Check progress:
```bash
re-agent status
```

## Project build

The legacy direct build mode is removed. Project builds must name a verified
project root; the root supplies the input snapshot, ABI contract, recipe, and
publication area. The project surface is deliberately small:

```bash
# Transform and stage a complete project build
re-agent build --project-root PATH_TO_PROJECT --phase transform

# Validate the recipe without publishing a build
re-agent build --project-root PATH_TO_PROJECT --phase verify-recipe

# Link or package the verified staged result
re-agent build --project-root PATH_TO_PROJECT --phase link
re-agent build --project-root PATH_TO_PROJECT --phase package
```

`transform` is the default project phase. `link`, `package`, and
`verify-recipe` require `--project-root`; there is no direct-config/CWD build
fallback and no project-build support for legacy per-function or partial-work
selectors. Use `--profile PATH` for a transient toolchain profile,
or omit it to use the project's activated profile.

Project builds are persistent and fail closed. Evidence covers the complete
project and publication is all-or-nothing: incomplete, stale, unverifiable, or
failed evidence prevents publication and never replaces the active build.

See [configuration.md](configuration.md) for the full config reference and
[architecture.md](architecture.md) for the pipeline design.
