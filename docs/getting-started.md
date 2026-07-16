# Getting Started

re-agent's release workflow is project-scoped. The old direct build mode is
not supported; all build and promotion commands require an owned project root.

## Install

```bash
pip install re-agent
```

Python 3.11+ is required. Configure an RE backend and an LLM provider for
reverse operations.

## 1. Create a Release 3 project

Start with a binary and a validated analysis export. Provisioning verifies
their identity and creates an owned snapshot:

```bash
re-agent project provision \
  --binary ORIGINAL_BINARY \
  --analysis ANALYSIS_EXPORT \
  --output PROJECT_ROOT \
  --name PROJECT_NAME
```

Alternatively export through a lifecycle backend:

```bash
re-agent project export --backend offline-export \
  --analysis ANALYSIS_EXPORT --binary ORIGINAL_BINARY --output EXPORT_PATH
re-agent project export --backend ghidra \
  --analysis ANALYSIS_INPUT --binary ORIGINAL_BINARY --output EXPORT_PATH
```

The project records a verified binary hash, snapshot inventory hash, and
project fingerprint. Do not edit the snapshot after provisioning.

## 2. Activate a verified toolchain

```bash
re-agent toolchain activate --project-root PROJECT_ROOT --profile PROFILE.yaml
re-agent toolchain status --project-root PROJECT_ROOT
```

Builds use the activated profile and authenticate its complete hash chain. For
a one-shot run, pass `--profile PROFILE.yaml` to `build` or `promote`; this is
transient and does not alter activation state.

## 3. Run Release 4 build

```bash
re-agent build --project-root PROJECT_ROOT --phase transform
re-agent build --project-root PROJECT_ROOT --phase verify-recipe
re-agent build --project-root PROJECT_ROOT --phase link
re-agent build --project-root PROJECT_ROOT --phase package
```

`transform` is the default phase. It processes the complete project in a
deterministic bulk run and records bounded compile evidence. The recipe is an
external, bounded, path-checked operation; it runs only after complete
transform coverage. Successful output is published immutably and the active
build pointer is updated atomically. Partial output is never published.

Compilation alone is not an ABI proof or a behavioral proof.

## 4. Run Release 5 proofs and promotion

The two generic proof stages can be recorded independently:

```bash
re-agent promote prove --project-root PROJECT_ROOT --proof abi --all
re-agent promote prove --project-root PROJECT_ROOT --proof differential --all \
  --original-binary ORIGINAL_BINARY
```

For an atomic whole-project operation, use:

```bash
re-agent promote project --project-root PROJECT_ROOT \
  --original-binary ORIGINAL_BINARY
```

Inspect the derived state and authenticated active promotion view:

```bash
re-agent promote status --project-root PROJECT_ROOT --format json
```

Promotion requires `--project-root`, an active verified Release 4 build, and an
original-binary-equivalent input for differential/project promotion. Evidence
is written to an external immutable promotion root. Set it explicitly with
`--promotion-root PATH`, or let the CLI use its isolated sibling default. It
must be outside the project root.

There are no reset, demote, force, or partial-promotion commands. Failure does
not replace the active promotion view. Proof results make only the claims
represented by their recorded adapter protocol and authenticated inputs; they
do not claim general ABI equivalence, behavioral equivalence, or semantic
correctness.

## Reverse and parity remain independent

```bash
re-agent init --abi-manifest PATH
re-agent reverse --class NAME --max-functions 5
re-agent parity --limit 50
re-agent status
```

See [configuration.md](configuration.md) for configuration and
[architecture.md](architecture.md) for the Release 3–5 design.
