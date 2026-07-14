## 1. Foundation and migration safety

- [ ] 1.1 Inventory target-specific defaults in `config`, build generators, CLI templates, and tests; move required values into explicit fixtures or external profiles.
- [ ] 1.2 Define generic Pydantic models for project identity, snapshot references, toolchain capabilities, run evidence, and adapter commands with strict `extensions` escape hatches.
- [ ] 1.3 Add fixture projects for offline exports and a minimal C/C++ target; ensure they contain no BGE-specific assumptions.
- [ ] 1.4 Add schema, migration, and rejection tests for unknown core keys, absent required capability configuration, and user-supplied target names.

## 2. Release 1 — Manifest-bound single-target Transform

- [x] 2.1 Add `--address` to the existing `re-agent build` CLI and reject unsupported combinations with `--module`, `--subunit`, bulk Transform, Assemble, and all-phase build under `preserve_abi`.
- [x] 2.2 Thread the already verified `AbiManifest` and its resolved raw-file identity from config loading to the Transform execution path without reopening an unverified manifest.
- [x] 2.3 Resolve exactly one source candidate and exactly one manifest symbol for `build --phase transform --address`; fail before an LLM call on missing or ambiguous input.
- [x] 2.4 Extract the minimum mono-target execution primitive from `subunit_processor.py` without refactoring the existing bulk path.
- [x] 2.5 Add preserve-ABI prompt templates that inject only address, name, signature, calling convention, and exact relative `output_path`; forbid extra targets, files, headers, helpers, stubs, renames, and path changes.
- [x] 2.6 Parse and validate the mono-target response: one matching TARGET, one non-empty `.cpp`, exact manifest output path, no traversal/absolute path, and no auxiliary artifact.
- [x] 2.7 Write validated output to run-scoped staging, compile it with the declared compile capability, and atomically commit only `MANIFEST_BOUND` plus `COMPILE_PASS` artifacts and provenance.
- [x] 2.8 Add unit and CLI integration tests for all rejected target/address/path/output combinations, retry identity preservation, `--no-persist`, compile failure, and successful single-target output.
- [x] 2.9 Update CLI/config/architecture documentation to state precisely that Release 1 binds a manifest and compiles; it does not prove ABI or behavior.

## 3. Release 2 — Replayable, isolated Transform runs

- [ ] 3.1 Create run-scoped directory and metadata conventions, with OS-backed exclusion lock and separate informational lock metadata.
- [ ] 3.2 Record immutable run inputs, resolved config, manifest identities, source hashes, prompt templates, provider/model parameters, toolchain fingerprint, and output hashes.
- [ ] 3.3 Capture every LLM exchange as an ordered record containing request hash, system/user prompts, raw response, response hash, provider/model, parameters, usage, and timestamps.
- [ ] 3.4 Implement an offline replay provider that fails closed on absent or hash-mismatched records and never falls back to a live provider.
- [ ] 3.5 Implement explicit resume validation and checkpointing; invalidate stale runs when their bound source, manifest, prompt, config, or toolchain identity differs.
- [ ] 3.6 Add replay and resume tests proving no network access, byte-identical artifacts for captured responses, and rejection after each relevant input mutation.

## 4. Release 3 — Generic project snapshots and toolchain capabilities

- [ ] 4.1 Add project provisioning that hashes the input binary, writes an owned `ProjectManifest`, and copies a verified analysis snapshot rather than retaining a mutable external reference.
- [ ] 4.2 Extend the backend protocol with health check, provisioning, analysis/export, and fingerprint operations; implement Ghidra and offline-export adapters behind that protocol.
- [ ] 4.3 Define toolchain capability contracts for compile, link, inspect-ABI, and run-differential, with executable/argument/version fingerprints verified only by operations that use them.
- [ ] 4.4 Remove implicit architecture, compiler, C++ standard, output directory, and Ghidra-path defaults from core behavior; require an explicit profile or fail clearly.
- [ ] 4.5 Add integration tests for provisioning, snapshot mutation isolation, profile validation, capability-specific failures, and two contrasting fixture targets.

## 5. Release 4 — Controlled bulk and verified external build

- [ ] 5.1 Build a deterministic bulk scheduler over the proven mono-target primitive, with coverage derived from the manifest and per-target resumable checkpoints.
- [ ] 5.2 Require every included target to have current `MANIFEST_BOUND` and `COMPILE_PASS` evidence before an aggregate build may proceed.
- [ ] 5.3 Replace CWD-dependent assemble paths with run-scoped staging and preserve full manifest-relative output paths.
- [ ] 5.4 Define one bounded external `build_recipe.command` interface (argv, cwd, sanitized environment, timeout, declared output) for link/package work.
- [ ] 5.5 Implement immutable versioned publication and atomic pointer update; preserve a previous published artifact on every build/link failure.
- [ ] 5.6 Record BuildEvidence for source/object coverage, recipe identity, stdout/stderr, exit status, artifact hash, and declared inspection output.
- [ ] 5.7 Add end-to-end fixture tests for complete coverage, failed compile, failed recipe, partial/stale evidence, nested output paths, and atomic publication rollback.

## 6. Release 5 — Adapter-backed evidence and promotion

- [ ] 6.1 Define versioned adapter command contracts for ABI inspection and differential execution, including structured result schema and all input/output fingerprints.
- [ ] 6.2 Store ABI and differential evidence append-only and derive `COMPILE_PASS`, `ABI_PASS`, `DIFFERENTIAL_PASS`, `PROMOTED`, `STALE`, and `INVALID` from valid linked evidence rather than mutable state files.
- [ ] 6.3 Fail closed when an adapter command, proof, harness observable, hash, or required capability is absent or mismatched.
- [ ] 6.4 Implement project-level promotion only as an all-or-nothing derived view over complete target evidence; do not implement forced demotion or heuristic bypasses.
- [ ] 6.5 Add fake adapter tests for valid evidence, crashes, timeouts, unknown observations, stale hashes, incomplete coverage, and rejected promotion.
- [ ] 6.6 Document the adapter boundary with a BGE adapter example kept outside the re-agent core, plus a minimal generic fixture adapter.

## 7. Final quality gates

- [ ] 7.1 Run Ruff, mypy, full pytest, OpenSpec validation, and dependency lock verification after every release.
- [ ] 7.2 Add a release qualification test that provisions a clean fixture project, snapshots it, transforms/replays it, builds it through its external recipe, and verifies its evidence bundle.
- [ ] 7.3 Verify no core Python source imports or embeds target-project behavior; allow target terms only in tests, fixtures, profiles, recipes, and adapters.
- [ ] 7.4 Produce a release report listing supported capabilities, unsupported operations, hashes, evidence completeness, and explicit non-claims about ABI or behavioral equivalence.
