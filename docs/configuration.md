# Configuration

re-agent combines ordinary reverse/parity configuration with project-scoped
snapshot, build, promotion, and replay state. CLI flags override environment
variables, which override `re-agent.yaml`, which overrides defaults.

## ABI contracts

The `contracts` section is required. The legacy non-project path validates the
manifest file and both hashes during config loading. Project mode instead
loads the already verified manifest from `--project-root` and requires the
same `preserve_abi` policy in YAML.

```yaml
contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "abi_manifest.json"
  abi_manifest_sha256: "<64 hexadecimal characters>"
```

The manifest format is generic and versioned. Its canonical hash and the raw
file hash are independent integrity checks. Neither a manifest match nor a
successful compilation alone proves ABI equivalence or behavior.

## Reverse and parity configuration

```yaml
llm:
  provider: "claude"       # claude | openai | openai-compat | codex
  model: "MODEL_NAME"
  api_key: null
  base_url: null
  timeout_s: 1800

reverse:
  backend:
    type: ghidra-bridge
    cli_path: PATH_TO_BACKEND
  project_profile:
    source_root: PATH_TO_SOURCE
    source_extensions: [".cpp", ".h", ".hpp"]
  parity:
    enabled: true
    call_count_warn_diff: 3
  orchestrator:
    optimize: true
    max_review_rounds: 4
```

Reverse and parity commands remain independent of build and promotion:

```bash
re-agent reverse --class NAME
re-agent parity --filter REGEX
```

## Owned project configuration

Project roots contain an owned snapshot, `project.id`, and the verified
contract used by project operations. Create and inspect them with:

```bash
re-agent project provision --binary ORIGINAL_BINARY \
  --analysis ANALYSIS_EXPORT --output PROJECT_ROOT --name PROJECT_NAME
re-agent project export --backend offline-export \
  --analysis ANALYSIS_EXPORT --binary ORIGINAL_BINARY --output EXPORT_PATH
re-agent project export --backend ghidra \
  --analysis ANALYSIS_INPUT --binary ORIGINAL_BINARY --output EXPORT_PATH
```

The project fingerprint binds the original-binary hash to the snapshot
inventory hash. Provisioning is no-replace: a different identity cannot reuse
an existing destination.

### Activated and transient toolchains

Toolchain profiles are strict, target-neutral YAML documents:

```yaml
backend: "backend-name"
target: "target-name"
compiler:
  command: ["compiler"]
  flags: ["-c"]
linker:
  command: ["linker"]
extensions: {}
```

Activate and inspect the project's immutable, verified capability chain:

```bash
re-agent toolchain activate --project-root PROJECT_ROOT --profile PROFILE.yaml
re-agent toolchain status --project-root PROJECT_ROOT
```

Build and promotion use the activated profile by default. Passing
`--profile PROFILE.yaml` to those commands performs transient resolution for
the requested capabilities and writes no activation state. Every resolved
binary is fingerprinted and its identity is recorded in downstream evidence.

## Controlled candidate build configuration

There is no legacy direct build mode. `build` requires `--project-root`; YAML
does not create a standalone CWD build. The `build:` section configures the
project operation:

```yaml
build:
  input:
    decompiled_dir: "snapshot-input"
    ghidra_exports: "snapshot-exports"
  output:
    language: "cpp"
    standard: "c++23"
    compiler: "compiler"
    compiler_flags: "-std=c++23 -c -Wall"
    target_dir: "build/output"
    work_dir: "build/work"
  optimization:
    max_llm_calls_per_run: 8
    max_llm_tokens_per_run: 150000
    max_compile_retry_calls_per_run: 3
  validation:
    compile_per_function: true
    compile_per_module: true
    compile_final_project: true
  resume:
    enabled: true
    state_path: "build/state.json"
```

Run the current project surface with:

```bash
re-agent build --project-root PROJECT_ROOT --phase transform
re-agent build --project-root PROJECT_ROOT --phase verify-recipe
re-agent build --project-root PROJECT_ROOT --phase link
re-agent build --project-root PROJECT_ROOT --phase package
```

The project transform processes the complete manifest deterministically, validates compile
checkpoints, and then runs only bounded, path-safe external recipes. Evidence
binds snapshot, manifest, config, recipe, compiler, and linker identities.
Build publication is immutable and updates an authenticated active pointer;
failed or partial runs cannot publish.

## Adapter-backed promotion configuration and commands

Promotion has no YAML override. It is explicitly scoped by CLI:

```bash
re-agent promote prove --project-root PROJECT_ROOT --proof abi --all
re-agent promote prove --project-root PROJECT_ROOT --proof differential --all \
  --original-binary ORIGINAL_BINARY --promotion-root PROMOTION_ROOT
re-agent promote status --project-root PROJECT_ROOT \
  --promotion-root PROMOTION_ROOT --format json
re-agent promote project --project-root PROJECT_ROOT \
  --original-binary ORIGINAL_BINARY --promotion-root PROMOTION_ROOT
```

The promotion root is external to the project and stores immutable proof
bundles plus an append-only hash-chained journal and authenticated active
promotion view. If omitted, the CLI selects an isolated sibling. It must never
be the project root or a descendant of it.

`prove --proof abi` and `prove --proof differential` are the two generic
adapter-proof stages. `promote project` runs both stages for the complete
project and publishes only after every bundle and chain check succeeds.

Promotion requires the original-binary-equivalent input for differential and
whole-project promotion, an active verified build, and a verified
project root. There are no reset, demote, force, or partial-promotion options.
Compilation and proof records do not claim general ABI equivalence, behavioral
equivalence, or semantic correctness.

## Replayable transform provenance

Transform writes one immutable, canonical, target-path-addressed and
content-hashed `TransformEvidence` record per
manifest entry. The record contains the project and snapshot identities,
manifest identities, effective LLM configuration, exact request/messages and
response, compiler command and executable hash, generated source hash, and
object hash. `BuildEvidence` schema v2 links each checkpoint to its
TransformEvidence path and digest.

Each project run is protected by an OS-backed `build/runs/RUN_ID/.run.lock`.
`run verify` and `run replay` hold that lock for the complete operation and
re-read project identity, config, profile selection, run identity, checkpoints,
and evidence before proceeding.

```bash
re-agent run verify --project-root PROJECT_ROOT --run-id RUN_ID
re-agent run replay --project-root PROJECT_ROOT --run-id RUN_ID
```

Replay is exact and offline-only. It uses the recorded provider transcript and
response through the replay provider, requires exact effective LLM
configuration, and never contacts a live provider. It still invokes the
verified compiler and compares regenerated source/object hashes with the
recorded TransformEvidence.

Profile selection has one rule: omit `--profile` to use the activated project
profile; pass a transient `--profile PATH` only when no active profile exists.
Transient selection cannot override activation and writes no activation state.

BuildEvidence v1 remains promotion-compatible historical compilation evidence,
but is not replayable because it lacks per-target TransformEvidence linkage. This workflow does not
claim universal compiler determinism or semantic proof; replay validates only
the recorded inputs, provider output, toolchain identity, and artifact hashes.
