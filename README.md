# re-agent

Generic reverse-engineering and project tooling. Reverse and parity
remain independent capabilities alongside owned projects, controlled builds,
adapter-backed promotion, and replayable transform provenance.

## Project lifecycle

```
owned project provision/export → immutable snapshot
    lifecycle backend → activated, verified toolchain capabilities

controlled project transform → bounded recipe → evidence
    immutable build + active pointer

adapter proof (ABI → differential) → hash-chained evidence → promotion view

immutable TransformEvidence → locked run verify/replay → exact offline replay
```

### Owned project snapshots and toolchains

Project provisioning verifies the original binary and
analysis inventory, copies the analysis into an immutable project-owned
snapshot, and records `project.id` plus a project fingerprint. Lifecycle
backends can provision or export snapshots through the generic offline-export
or Ghidra backends.

```bash
re-agent project provision --binary PATH --analysis PATH --output PROJECT_ROOT --name NAME
re-agent project export --backend offline-export --analysis PATH --binary PATH --output PATH
re-agent project export --backend ghidra --analysis PATH --binary PATH --output PATH
```

Toolchain capabilities are project-scoped. Activate a profile once,
then inspect its authenticated status:

```bash
re-agent toolchain activate --project-root PROJECT_ROOT --profile PROFILE.yaml
re-agent toolchain status --project-root PROJECT_ROOT
```

Build and promotion commands use the activated, hash-verified capability chain
by default. A supplied profile is transient and is never activated or written
into project state.

### Controlled candidate builds

Legacy direct build mode is removed. `build` requires `--project-root` and
performs deterministic bulk transformation against the owned snapshot. Every
manifest entry must have complete compile evidence before an external recipe
may run. Recipes are bounded, path-checked, and executed only in project-owned
staging.

```bash
re-agent build --project-root PROJECT_ROOT --phase transform
re-agent build --project-root PROJECT_ROOT --phase verify-recipe
re-agent build --project-root PROJECT_ROOT --phase link
re-agent build --project-root PROJECT_ROOT --phase package
```

Build evidence binds the project, manifest, configuration, recipe, compiler, and
linker identities. Successful output is published as an immutable build and
the authenticated active pointer is updated without replacing an existing
publication. Failures are all-or-nothing: partial, stale, or incomplete
results are never published. Compilation alone is not an ABI or behavioral
proof.

### Adapter-backed promotion

The generic two-stage adapter proof flow has an ABI stage that establishes the
adapter's ABI-facing result; the differential stage compares the candidate
against an original-binary-equivalent input. Each stage emits content-addressed
evidence. Evidence is stored immutably and linked by an append-only hash chain.

Run individual proofs, inspect promotion state, or atomically promote the
whole project:

```bash
re-agent promote prove --project-root PROJECT_ROOT --proof abi --all
re-agent promote prove --project-root PROJECT_ROOT --proof differential --all \
  --original-binary ORIGINAL_BINARY
re-agent promote status --project-root PROJECT_ROOT --format json
re-agent promote project --project-root PROJECT_ROOT \
  --original-binary ORIGINAL_BINARY
```

Promotion requires a verified project root, an existing active
build, the original-binary-equivalent input for differential/project promotion,
and an external promotion root. Use `--promotion-root PATH` to select it; by
default the CLI uses an isolated sibling of the project root. The promotion
root must be outside the project tree. `--profile PATH` may select a transient
verified toolchain for proofs.

Promotion is monotonic and fail-closed. There are no reset, demote, force, or
partial-promotion operations. Proof or publication failure leaves the active
promotion view unchanged.

These results are not claims of ABI equivalence, behavioral equivalence, or
semantic correctness. Compilation proves only compilation; adapter proofs
prove only the explicitly recorded protocol evidence and its authenticated
inputs.

### Replayable transform provenance

Transform writes immutable, target-path-addressed and content-hashed `TransformEvidence`
for every transformed manifest entry. Each per-target record captures the project/snapshot and
manifest identities, effective LLM configuration and request, exact input and
response, compiler invocation and binary hash, generated source hash, and
object hash. `BuildEvidence` schema v2 links every target checkpoint to its
TransformEvidence path and hash.

Every project run is protected by an OS-backed `.run.lock`. Verification and
replay re-read the project, configuration, profile selection, and run state
under that lock; they reject stale identity, missing, substituted, or changed
run files.

```bash
re-agent run verify --project-root PROJECT_ROOT --run-id RUN_ID
re-agent run replay --project-root PROJECT_ROOT --run-id RUN_ID
```

Replay is offline-only: it uses the recorded provider messages and response,
requires exact effective LLM configuration, and does not call a live provider.
Replay still uses the verified compiler and checks regenerated source/object
hashes against the recorded TransformEvidence. A transient `--profile PATH`
may be used only when the project has no active profile; it cannot override an
active profile. Historical BuildEvidence v1 remains promotion-compatible
historical compilation evidence, but is not replayable because it has no
per-target TransformEvidence linkage.

This workflow does not claim universal compiler determinism or semantic equivalence.
Exact replay verifies the recorded inputs, provider output, compiler identity,
and artifact hashes for this run; it is not a universal compiler or behavioral
proof.

## Reverse and parity commands

```bash
re-agent init --abi-manifest PATH
re-agent reverse --class NAME --max-functions 10
re-agent parity --filter REGEX
re-agent status
re-agent pipeline --skip-build
```

The reverse pipeline provides bounded reverser/checker loops, block reversal,
structural checks, objective verification, and configurable parity signals.

## Configuration and installation

Configuration priority is CLI flags > `RE_AGENT_*` environment variables >
`re-agent.yaml` > defaults. See [docs/configuration.md](docs/configuration.md)
for reverse, project, toolchain, build, promotion, and replay configuration.

```bash
pip install re-agent
```

Requirements include Python 3.11+, a configured RE backend, and a supported
LLM provider. The project core is generic and contains no target-specific
rules.

## Development

```bash
python -m pip install -e ".[dev]"
pytest tests/
ruff check src/
mypy src/re_agent/
```

## License

MIT
