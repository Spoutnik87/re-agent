# Architecture

re-agent has an independent reverse/parity surface and a project-scoped
Release 3–5 lifecycle:

```
R3  binary + analysis → owned snapshot → lifecycle backend
                         │
                         └→ activated verified capabilities

R4  project root → deterministic bulk transform → bounded recipe
                         │                         │
                         └── complete evidence ────┘
                                      │
                         immutable build + active pointer

R5  active build → ABI adapter proof → differential adapter proof
                         │                    │
                         └── immutable hash-chained evidence
                                      │
                            authenticated promotion view
```

## Reverse and parity surface

The reverse pipeline remains independent of build publication:

`CLI → config → orchestrator → reverser/checker loop → RE backend → parity`

It supports single-function and class-level reversal, bounded retries, block
reversal, objective structural checks, and configurable parity signals. These
commands do not create or promote a project build.

## Release 3 — owned project and toolchain foundation

### Owned snapshots

`project provision` validates the original binary, inventories the analysis,
copies it into a project-owned snapshot, and writes a `project.id`. The project
fingerprint is derived from the original-binary hash and snapshot inventory
hash. Snapshot paths and metadata are verified whenever a project command
opens the root.

`project export` supplies lifecycle backends for generic analysis snapshots.
The current backend choices are `offline-export` and `ghidra`. Backends produce
validated exports; provisioning remains the no-replace operation that gives a
project its owned identity.

### Verified capabilities

Toolchain profiles are strict, target-neutral descriptions of compiler, linker,
and optional inspection/proof commands. `toolchain activate` publishes a
content-addressed profile and fingerprint, then updates the project's active
link. Capability resolution authenticates the complete pointer → profile →
fingerprint → binary chain before returning commands.

Passing `--profile` to a build or promotion command selects transient
resolution. It fingerprints only the requested capabilities and writes no
activation state. Activated and transient command identities are recorded in
the evidence that consumes them.

## Release 4 — deterministic project build

The legacy direct build mode is removed. Build orchestration is project-only:
`--project-root` is required, and project files—not the caller's CWD—define the
inputs and publication area.

The build phases are:

1. **Transform**: process the complete manifest in deterministic order and
   create per-entry compile checkpoints.
2. **Verify-recipe**: execute a bounded witness for the external recipe without
   publishing a build.
3. **Link/package**: consume complete transform checkpoints, materialize
   recipe manifests, run the bounded external recipe, validate declared output,
   and create build evidence.

The recipe is constrained to project staging, has validated input/output paths,
and cannot escape the project build area. Evidence binds at least the project
fingerprint, manifest hashes, configuration, recipe hash, compiler and linker
fingerprints, complete source/object coverage, output hash, and run identity.

Publication uses an immutable no-replace destination and an authenticated
active pointer. A failed recipe, stale checkpoint, incomplete coverage, or
identity mismatch prevents publication. Existing active output is never
replaced by a partial or failed run.

Compilation and `MANIFEST_BOUND/COMPILE_PASS` evidence establish build gates
only. They are not ABI proofs, behavioral proofs, runtime proofs, or semantic
equivalence claims.

## Release 5 — adapter proofs and promotion

R5 is a generic adapter boundary. Each adapter receives an authenticated
request and returns a bounded result plus captured evidence and attachments.
The promotion service resolves the required proof capabilities from the R3
toolchain chain, stages inputs under the external promotion root, and seals
the results.

### Two proof stages

- **ABI proof** records the adapter's ABI-facing result for the selected
  candidate/build.
- **Differential proof** records the adapter comparison using the required
  original-binary-equivalent input.

Each stage produces content-addressed proof evidence. A sealed proof bundle
hashes its evidence; the append-only promotion journal hash-chains batches.
The immutable evidence store and active promotion publisher authenticate the
bundle, journal, project/build identity, and current promotion view before
reporting `PROMOTED`.

`promote project` is the atomic whole-project entry point: it runs both proof
stages for every manifest entry, commits no journal or pointer until all
bundles are complete, and publishes one authenticated active view. `promote
prove` is useful for recording a single stage; `promote status` derives state
from the current verified project/build and authenticated evidence rather than
trusting history alone.

### Promotion boundaries

Promotion requires:

- an existing verified project root;
- an active verified Release 4 build;
- an external promotion root, outside the project tree; and
- the original-binary-equivalent input for differential and whole-project
  promotion.

The CLI supports `promote prove`, `promote project`, and `promote status`.
There are no reset, demote, force, or partial-promotion operations. Failed or
stale evidence leaves the active promotion view unchanged.

Proofs make only the claims represented by their adapter protocols and
authenticated inputs. Neither compilation nor these proofs claim general ABI
equivalence, behavioral equivalence, or semantic correctness.
