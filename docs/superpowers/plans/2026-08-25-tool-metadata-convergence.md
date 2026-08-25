# Tool Metadata Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace every duplicated Python runtime classification of the 25 Typed tools with one immutable, validated `ToolMetadataBundleV1`, while preserving the exact 25/3/4 Typed/Legacy/Compensation boundary and all Provider, HITL, Ledger, HTTP/SSE, Journal, database, and business-side-effect contracts.

**Architecture:** The six domain modules declare complete `ToolSpec` values. A generic compiler validates and freezes those 25 specs together with discovery policy, three static Legacy adapters, and four Compensation handlers into one application-scoped Bundle. Narrow Provider, Discovery, Authority, Operation, Legacy, and Compensation views are injected from the same Bundle. Per-Segment leases and opaque route/proof handles preserve provenance without recompiling metadata or persisting runtime objects.

**Tech Stack:** Python 3.10+, pytest 8, SQLAlchemy 2, SQLite, FastAPI, jsonschema 4.26.0, the existing Agent Loop/Pilot Runtime/Context Projector/Tool Pipeline/Tool Authority/Write Operation Ledger/Journal, uv, Ruff, Mypy, Vitest, Vite, local verification, controlled real-AI verification, and the in-app browser.

**Scope revisions:** Task 4 was first formally stopped before product edits after the original baseline-only `ToolSpec(` scan missed `dataclasses.replace()` consumers, attribute readers, and the Task 3 synthetic factory. During the reviewed Task 4 full GREEN matrix it was stopped again when two pre-existing Authority tests proved to construct a production Spec as a non-closed one-tool Catalog, and the production Authority capability validator began tripping the existing no-automatic-grant AST gate after `ToolCapability` became the closed enum. The reviewed Task 4 scope below explicitly includes all seven supplemental consumers, migrates the two test Catalogs to explicit closed test metadata, and validates capability values directly without materializing the whole enum. These scope-only revisions do not change the approved design, fixed identities, 25/3/4 boundary, protocol seals, or Golden assets.

---

## 0. Fixed workspace, baseline, and execution rules

Work only in:

```text
D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260825-tool-metadata-convergence
```

Fixed identities:

```text
branch: refactor/20260825-tool-metadata-convergence
fixed production baseline: 0c10e05e256eb757d5f89a8b009dcea193f2fc78
fixed implementation start: bf879fd8f10575e3996bacf8ff9c6ccc8ab7cc64
approved design: docs/superpowers/specs/2026-08-25-tool-metadata-convergence-design.md
implementation plan: docs/superpowers/plans/2026-08-25-tool-metadata-convergence.md
```

Do not modify the root workspace, push, merge, rebase, or create another implementation branch. Preserve unrelated user changes. Use `apply_patch` for source edits. Run `git add` and `git commit` as separate commands. Commit messages must use `<type>: AI <中文主题>`.

Before product edits, create these immutable gate files:

```text
%TEMP%\offerpilot-tool-metadata-convergence-gate\baseline.txt
%TEMP%\offerpilot-tool-metadata-convergence-gate\implementation-start.txt
%TEMP%\offerpilot-tool-metadata-convergence-gate\allowlist.txt
%TEMP%\offerpilot-tool-metadata-convergence-gate\task-01.txt ... task-13.txt
%TEMP%\offerpilot-tool-metadata-convergence-gate.locator.json
```

Rules:

- `baseline.txt` contains only the full fixed production baseline above.
- `implementation-start.txt` contains only the full fixed implementation-start identity above. Gate regeneration after a reviewed scope stop must preserve it and must not substitute the then-current `HEAD`.
- `allowlist.txt` contains exactly the union of all per-Task paths. Task 13 explicitly contains the already reviewed design, this implementation plan, the final verification report, and the inherited Tasks 1-12 path union, so the global equality is mechanical rather than exceptional.
- each `task-NN.txt` contains the exact repository-relative file paths allowed for that Task; no directory, glob, or prefix entry is valid.
- locator JSON records absolute worktree, branch, the three core file paths, and the per-Task gate directory.
- tests and implementation scripts may read these files but may never rewrite, accept, or regenerate them.
- every final scope comparison uses the fixed baseline, never the implementation-start commit.

Before writing product files, materialize every per-Task path list and its union. Literal `Files` entries are copied exactly. The computed entries are the three baseline-only scan sets named in Tasks 2, 4, and 12 plus Task 13's exact inherited union of Tasks 1-12; resolve the three scan sets from the fixed commit before any source edit:

```powershell
$fixed = '0c10e05e256eb757d5f89a8b009dcea193f2fc78'
$capabilityImportSet = git grep -l 'ToolCapability' $fixed -- src tests | ForEach-Object { $_ -replace '^[^:]+:', '' }
$toolSpecConstructorSet = git grep -l 'ToolSpec(' $fixed -- src tests | ForEach-Object { $_ -replace '^[^:]+:', '' }
$classificationConsumerSet = git grep -l -E 'MODEL_TOOL_NAMES|MODEL_TOOL_CATALOG|DEPENDENCY_POLICY_V1|TRANSACTIONAL_TYPED_WRITE_NAMES|REQUIRED_UNDO_TOOL_NAMES|TYPED_WRITE_OPERATION_NAMES|LEGACY_WRITE_OPERATION_NAMES|COMPENSATION_OPERATION_NAMES|REQUIRED_UNDO_OPERATION_NAMES|WRITE_OPERATION_NAMES|_pending_adapter_kind|_chained_adapter_kind|_with_write_contract|_with_runtime_metadata|editable_fields_for_tool|_undo_seed_for_pending|_build_write_undo|_CREATED_RECORD_FINGERPRINT_FIELDS|legacy_catalog_factory|_legacy_catalog|_legacy_adapter' $fixed -- src tests | ForEach-Object { $_ -replace '^[^:]+:', '' }
```

The baseline-only `$toolSpecConstructorSet` is intentionally supplemented in Task 4 by these seven explicitly reviewed paths, because a `ToolSpec(` text scan cannot discover `dataclasses.replace()`, attribute reads, source-gate fallout from the closed enum cutover, or files introduced after the fixed baseline:

```text
tests/agent_loop/helpers.py
tests/agent_loop/test_runner.py
src/offerpilot/ai/tool_authority/contracts.py
tests/tool_authority/test_approval_transaction.py
tests/tool_authority/test_read_uow.py
tests/tool_authority/test_replay_topology.py
tests/tool_metadata/factories.py
```

Do not replace this explicit supplement with a broader runtime grep or dynamically append scan results to a gate. Future scope discoveries still require the stop/revise/re-review/regenerate procedure below.

Normalize separators to `/`, sort ordinally, remove duplicates, review the explicit results, then write immutable `task-NN.txt` files and their union. No task may append a path later. If implementation discovers a required path outside the frozen union, stop, document the reason, revise/re-review the plan, and regenerate all gate files before resuming; never silently widen the allowlist.

Every commit uses its task file, never a directory-level `git add`:

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-NN.txt" | Where-Object { $_ })
git add -- $taskPaths
$staged = @(git diff --cached --name-only)
$outsideTask = @($staged | Where-Object { $taskPaths -notcontains $_ })
if ($outsideTask.Count -ne 0) { throw "staged path outside task allowlist: $($outsideTask -join ', ')" }
```

For the ignored final report only, replace `git add --` with `git add -f --`. The task file must already contain the report path.

### External contracts that must remain unchanged

```text
Provider surface: exact 25 complete envelopes, same order/name/description/schema.
Legacy boundary: exact 3 deterministic tools, never model-visible.
Compensation boundary: exact 4 operation kinds, never model-visible.
HTTP/SSE: no removed, renamed, reordered, or newly required fields/events.
HITL: approve/modify/reject, Pending identity, claim/CAS, chained Pending unchanged.
Ledger: operation-scoped terminal at-most-once, replay, delivery fencing, commit-unknown unchanged.
Journal: existing event types, schemas, result_contract, and 25+3 tool whitelist unchanged.
Database: no model, table, column, index, CHECK, trigger, or migration changes.
Provider/tool calls: no hidden retry, fallback, double execution, or shadow path.
```

The implementation is internally destructive. The final branch must contain no feature flag, dual registry, compatibility forwarding property, shadow execution, automatic Golden updater, Typed-to-Legacy fallback, runtime rollback switch, or name-based runtime classification outside the exact historical compatibility allowlist.

### Fixed 25/3/4 truth

Typed order:

```text
list_applications
get_application
create_application
update_application_status
list_application_events
get_application_event
create_application_event
update_application_event
delete_application_event
list_notes
add_note
update_note
delete_note
list_offers
get_offer
compare_offers
update_offer
save_offer_assessment
list_resumes
get_resume
resume_update_career_intent
resume_rewrite_highlight
list_resume_matches
list_jd_analyses
get_jd_analysis
```

Required Undo bindings:

```text
create_application        -> delete_application        -> undo:create_application
update_application_status -> update_application_status -> undo:update_application_status
create_application_event  -> delete_application_event  -> undo:create_application_event
add_note                  -> delete_note               -> undo:add_note
```

Legacy initial-route bindings:

```text
jd_clarification           -> save_application_jd_version
jd_deterministic_action    -> save_application_jd_version
submission_snapshot_action -> create_application_submission_snapshot
outcome_recording_action   -> record_application_outcome
confirmation_resume        -> proof-only; never an initial issuer
```

Approved whole-boundary seals:

```text
Provider: sha256:db60a499a2c76fa46e769214cbdce1488bb86b2f67941d6ae961f25ed997e98b
Legacy:   sha256:7d1d6b7e6cf3953b1a17e7a81c9d5655b5366de25b263ea6bd89a646c2f2a580
```

### Narrow Bundle views

Implement these frozen, non-serializable views. Every view carries an opaque `bundle_instance_token` compared by exact registry identity and never retains an ORM object, Session, Repository, raw arguments, secret, or full Bundle reference.

```text
ProviderToolMetadataView
  ordered_contracts: tuple[ProviderToolContract, ...]
  provider_boundary_fingerprint: str

ToolDiscoveryMetadataView
  ordered_entries: tuple[ToolDiscoveryEntryV1, ...]
  policy: ToolDiscoveryPolicyV1
  discovery_fingerprint: str

ToolAuthorityMetadataView
  entries: read-only mapping[str, ToolAuthorityEntryV1]
  authority_manifest_fingerprint: str

ToolOperationMetadataView
  entries: read-only mapping[str, ToolOperationEntryV1]
  operation_fingerprint: str

LegacyDeterministicBoundaryV1
  ordered_adapter_bindings: tuple[LegacyAdapterBindingV1, ...]
  initial_route_bindings: tuple[LegacyInitialRouteBindingV1, ...]
  legacy_boundary_fingerprint: str

CompensationMetadataView
  ordered_handler_bindings: tuple[CompensationHandlerBindingV1, ...]
  compensation_fingerprint: str
```

`ToolMetadataBundleV1` is the only constructor and exposes `provider_view()`, `discovery_view()`, `authority_view()`, `operation_view()`, `legacy_boundary()`, and `compensation_view()`. Each call returns a view bound to the same token; callers cannot construct a valid token or view directly. Canonical view projections include ordered static fields and fingerprints but exclude the token, callables, and object identity. All mappings are copied and wrapped read-only at composition. `AuthoritySurfaceView` is derived only from `ToolAuthorityMetadataView + SegmentExecutionAuthority`; the old `DependencyPolicyV1` projects into `ToolDiscoveryPolicyV1` during compilation and is not retained as a second runtime source.

`OperationKind.TRANSACTIONAL_WRITE` is internal only. Existing Authority Manifest, Pending/Ledger, Journal `tool_kind`, transport, and model-visible projections continue to emit the baseline-compatible string `write`; `OperationKind.READ` continues to emit `read`.

## Task 1: Freeze independent read-only Golden assets

**Files:**

- Create: `tests/tool_metadata/__init__.py`
- Create: `tests/tool_metadata/golden.py`
- Create: `tests/tool_metadata/test_golden_assets.py`
- Create: `tests/fixtures/tool_metadata/golden_index_v1.json`
- Create: `tests/fixtures/tool_metadata/tool_metadata_manifest_v1.json`
- Create: `tests/fixtures/tool_metadata/tool_selection_matrix_0c10e05.json`
- Create: `tests/fixtures/tool_metadata/tool_operation_matrix_0c10e05.json`
- Create: `tests/fixtures/tool_metadata/resolver_implementation_bindings_0c10e05.json`
- Read: `tests/fixtures/tool_pipeline/provider_manifest_30c944f.json`
- Read: `tests/fixtures/tool_authority/authority_manifest_v1.json`
- Read: `tests/fixtures/tool_authority/dependency_policy_v1.json`

- [ ] **Step 1: Write the read-only loader and failing asset tests**

`golden.py` exposes only `load_asset(name)`, `canonical_bytes(value)`, and `sha256(raw)`. Canonical JSON uses UTF-8, sorted keys, compact separators, `ensure_ascii=False`, `allow_nan=False`, and a trailing newline. Tests reject writers (`write_text`, `write_bytes`, writable `open`, update flags, environment-controlled acceptance, and fixture overwrite) under `tests/tool_metadata`.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_golden_assets.py -q
```

Expected: failure because the five assets do not yet exist.

- [ ] **Step 3: Capture from an explicit detached baseline checkout**

```powershell
$capture = Join-Path $env:TEMP 'offerpilot-tool-metadata-baseline-capture'
git worktree add --detach $capture 0c10e05e256eb757d5f89a8b009dcea193f2fc78
```

Inspect only the baseline checkout. Add reviewed canonical assets to the implementation worktree with `apply_patch`; never add a generator. After review, remove only that exact detached worktree with `git worktree remove $capture`.

The assets contain only synthetic static contract data. They must not contain entity IDs, user text, Prompt/answer content, local paths, timestamps, SQLite bytes, secrets, HMAC values, exception strings, callable repr, or object addresses.

- [ ] **Step 4: Verify exact counts and existing independent Goldens**

```powershell
uv run pytest tests/tool_metadata/test_golden_assets.py tests/tool_pipeline/test_golden_assets.py tests/tool_authority/test_baseline_golden.py tests/tool_authority/test_dependency_policy.py -q
```

Expected: exact 25 Typed, 3 Legacy, 4 Compensation, 4 required Undo, all discovery cases, and all resolver ordinals pass; production imports no test fixture.

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-01.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 1 staged scope violation' }
git commit -m "test: AI 固化工具元数据基线资产"
```

## Task 2: Add leaf policy types and canonical immutable values

**Files:**

- Create: `src/offerpilot/ai/tool_runtime/policy_types.py`
- Create: `src/offerpilot/ai/tool_runtime/metadata.py`
- Modify: `src/offerpilot/ai/tool_runtime/context.py`
- Modify: `src/offerpilot/ai/tool_runtime/__init__.py`
- Modify: `src/offerpilot/ai/tool_authority/contracts.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/ai/tool_specs/applications.py`
- Modify: `src/offerpilot/ai/tool_specs/application_events.py`
- Modify: `src/offerpilot/ai/tool_specs/notes.py`
- Modify: `src/offerpilot/ai/tool_specs/offers.py`
- Modify: `src/offerpilot/ai/tool_specs/resumes.py`
- Modify: `src/offerpilot/ai/tool_specs/jd_analyses.py`
- Modify: the exact frozen Task 2 `$capabilityImportSet` resolved from the fixed baseline in §0
- Create: `tests/tool_metadata/test_contracts.py`
- Create: `tests/tool_metadata/test_canonical.py`

- [ ] **Step 1: Write RED tests**

Test the exact closed enums, deep-copy/deep-freeze behavior, canonical UTF-8 JSON, bool-vs-int rejection, non-finite number rejection, string-key enforcement, bounded control-text rejection, and lack of Unicode normalization. Add an Authority test proving `_require_capabilities()` accepts the relocated exact enum and rejects lookalike enum instances.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_contracts.py tests/tool_metadata/test_canonical.py tests/tool_authority/test_contracts.py -q
```

- [ ] **Step 3: Implement and migrate every capability import atomically**

Move the existing eleven `ToolCapability` values to `policy_types.py`. Do not re-export them from `context.py`. Update `tool_runtime.__init__`, Authority module/type identity validation, all six domain specs, production callers, and test builders in the same commit.

Add closed `ToolDomain`, `ProviderVisibility`, `LegacyBoundaryVisibility`, `OperationKind`, `UndoPolicy`, `UndoPayloadKind`, and `CompensationKind` enums. Add `freeze_json`, `materialize_json`, `canonical_json_bytes`, and `canonical_sha256` to `metadata.py`.

- [ ] **Step 4: Verify GREEN and import boundaries**

```powershell
uv run pytest tests/tool_metadata/test_contracts.py tests/tool_metadata/test_canonical.py tests/tool_pipeline/test_context.py tests/tool_authority/test_contracts.py tests/tool_authority/test_matrix.py -q
uv run ruff check src/offerpilot/ai/tool_runtime src/offerpilot/ai/tool_authority tests/tool_metadata
uv run mypy src/offerpilot/ai/tool_runtime src/offerpilot/ai/tool_authority
```

Expected: `policy_types.py` imports no Repository, ORM, Authority Composition, Pilot Runtime, or test module; no import from `tool_runtime.context.ToolCapability` remains.

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-02.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 2 staged scope violation' }
git commit -m "feat: AI 建立工具元数据基础契约"
```

## Task 3: Define metadata DTOs and runtime-binding validation without cutting ToolSpec

**Files:**

- Modify: `src/offerpilot/ai/tool_runtime/metadata.py`
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`
- Create: `tests/tool_metadata/factories.py`
- Create: `tests/tool_metadata/test_metadata_validation.py`
- Create: `tests/tool_metadata/test_runtime_bindings.py`

- [ ] **Step 1: Add deterministic test factories**

`tests/tool_metadata/factories.py` must define these concrete helpers and no fixture writer:

```text
synthetic_provider_contract(name="synthetic_tool")
read_metadata(name="synthetic_tool", domains=(applications,), dependencies=(), resolver_descriptors=())
write_metadata(name="synthetic_write", undo_policy=none, dependencies=(), resolver_descriptors=())
resolver_descriptor(resolver_id="application_identity_arg", arg_path="id")
resolver_binding(descriptor)
runtime_bindings()
presentation_binding()
synthetic_tool_spec(name="synthetic_tool", metadata=None)
synthetic_manifest()
synthetic_legacy_boundary()
synthetic_compensation_view()
compose_synthetic_bundle()
forbid_call(*args, **kwargs)
```

Every helper returns a new value; no module-global mutable object is permitted.

- [ ] **Step 2: Write RED invariant tests**

Cover read/write union exclusivity; duplicate domain/capability/dependency; Provider editable-field/schema mismatch; resolver count/ordinal/entity/presence/identity type; exact descriptor object identity; stable implementation ID; lambda/partial rejection; required Undo descriptor/callable matching; missing presentation; callable replacement after sealing; and generic serialization rejection.

Use the baseline-compatible resolver example:

```text
resolver_id = application_identity_arg
arg_path = id
entity_kind = application
presence = required
identity_type = positive_int64
```

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_metadata_validation.py tests/tool_metadata/test_runtime_bindings.py -q
```

- [ ] **Step 4: Implement standalone final DTOs and validators**

Implement frozen `BindingResolverDescriptorV1`, `ResolverImplementationBinding`, `ToolBindingMetadataV1`, `EditableFieldMetadataV1`, `ReadOperationMetadataV1`, `WriteOperationMetadataV1`, `ToolSurfaceMetadataV1`, `UndoBuilderBinding`, `ToolPresentationBindingV1`, and runtime binding groups.

This task must not change the existing production `ToolSpec` constructor or delete its old fields. Add pure `validate_tool_spec_components(...)` and callable-identity sealing so synthetic data can be proven before the atomic production cutover. The final breaking `ToolSpec` shape is performed only in Task 4; no compatibility forwarding property is introduced.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_metadata_validation.py tests/tool_metadata/test_runtime_bindings.py tests/tool_metadata/test_contracts.py tests/tool_metadata/test_canonical.py -q
uv run ruff check src/offerpilot/ai/tool_runtime tests/tool_metadata
uv run mypy src/offerpilot/ai/tool_runtime
```

- [ ] **Step 6: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-03.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 3 staged scope violation' }
git commit -m "feat: AI 完善工具元数据与运行时绑定"
```

## Task 4: Perform the atomic ToolSpec/25-tool/compiler cutover

**Files:**

- Create: `src/offerpilot/ai/tool_runtime/protocol_seals.py`
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`
- Modify: `src/offerpilot/ai/tool_runtime/catalog.py`
- Modify: `src/offerpilot/ai/tool_runtime/context.py`
- Modify: `src/offerpilot/ai/tool_runtime/pipeline.py`
- Modify: `src/offerpilot/ai/tool_runtime/journal.py`
- Modify: `src/offerpilot/ai/tool_runtime/transport.py`
- Modify: `src/offerpilot/ai/tool_specs/applications.py`
- Modify: `src/offerpilot/ai/tool_specs/application_events.py`
- Modify: `src/offerpilot/ai/tool_specs/notes.py`
- Modify: `src/offerpilot/ai/tool_specs/offers.py`
- Modify: `src/offerpilot/ai/tool_specs/resumes.py`
- Modify: `src/offerpilot/ai/tool_specs/jd_analyses.py`
- Modify: `src/offerpilot/ai/tool_specs/catalog.py`
- Modify: `src/offerpilot/ai/tool_specs/__init__.py`
- Modify: `src/offerpilot/ai/tool_authority/contracts.py`
- Modify: `src/offerpilot/ai/tool_authority/composition.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/ai/confirmation.py`
- Modify: `src/offerpilot/context_projector/authority_surface.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/tool_metadata/test_manifest.py`
- Create: `tests/tool_metadata/test_compiler.py`
- Create: `tests/tool_metadata/test_protocol_seals.py`
- Create: `tests/tool_metadata/test_presentation_bindings.py`
- Modify: the exact frozen Task 4 `$toolSpecConstructorSet` resolved from the fixed baseline in §0
- Modify: `tests/agent_loop/helpers.py`
- Modify: `tests/agent_loop/test_runner.py`
- Modify: `tests/tool_authority/test_approval_transaction.py`
- Modify: `tests/tool_authority/test_read_uow.py`
- Modify: `tests/tool_authority/test_replay_topology.py`
- Modify: `tests/tool_metadata/factories.py`
- Modify: `tests/tool_pipeline/test_catalog.py`
- Modify: `tests/tool_pipeline/test_pipeline.py`
- Modify: `tests/tool_pipeline/test_transport.py`
- Modify: `tests/tool_authority/test_matrix.py`
- Modify: `tests/tool_authority/test_baseline_golden.py`
- Modify: `tests/tool_authority/test_dependency_policy.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_confirmation_cutover.py`

- [ ] **Step 1: Write RED compiler, seal, compatibility, and 25-tool tests**

Tests must cover exact Manifest keys/order/nullability, duplicate names, dependency cycles, self/unknown/Legacy dependencies, read/write union validation, runtime callable identity, Provider payload mutation, Legacy kind/visibility mutation, operation-kind compatibility projection, exact four required-Undo builder bindings, complete confirmation/presentation bindings, and unchanged transport result projection.

Presentation tests must prove that replacement bindings are constructed as complete new `ToolPresentationBindingV1` values with fresh callable-identity seals and that the replacement succeeds before a post-seal mutation fails closed. Test callbacks bound into accepted presentation metadata must be module-level named functions: no lambda, local closure, or `partial`. A lambda/local/partial may appear only as a negative constructor-rejection probe inside an assertion that it fails. Stateful cancellation/counting behavior uses a dedicated test probe/state object observed by a module-level callback; it must not place the local test function into a binding.

`test_protocol_seals.py` must independently read the committed baseline Provider and Legacy assets, canonicalize the complete ordered boundaries, recompute both approved digests, and then prove that name, order, full payload, kind, or visibility changes fail. A test that only compares two hard-coded constants is insufficient.

`test_compiler.py` must additionally spy on the production `build_model_tool_catalog()` integration: it passes the complete ordered 25 Provider payloads to `verify_provider_boundary()` before returning, and an injected seal failure prevents a Catalog from being returned or published. Direct verifier tests alone are insufficient.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_manifest.py tests/tool_metadata/test_compiler.py tests/tool_metadata/test_protocol_seals.py tests/tool_metadata/test_presentation_bindings.py tests/agent_loop tests/tool_pipeline tests/tool_authority tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py tests/test_chat_api.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py -q
```

- [ ] **Step 3: Atomically change ToolSpec and migrate all direct consumers**

Change `ToolSpec` to the approved final shape: Provider contract, metadata, resolver bindings, optional Undo builder binding, decoder/executor/preflight/mutable validator/failure mapping/renderers/projectors, and presentation binding. Remove old top-level classification fields and do not add forwarding properties.

Migrate all six domain modules and every direct consumer listed above in this same task. Encode complete metadata at declaration sites. Define each tool-specific Undo seed/build named callable beside its domain ToolSpec and bind each of the exact four directly; domain specs must not import Pilot Runtime. Bind the existing compatibility renderers, confirmation descriptors, result metadata projector, and Presentation projectors directly in every ToolSpec. The existing generic transport projector remains a compatibility boundary that consumes the exact ToolSpec `success_renderer` and `result_metadata_projector`; it is not a new ToolSpec field. Update `api.py`, `pilot_runtime/service.py`, `composition.py`, `continuation.py`, `confirmation.py`, `agent_loop.py`, and `tool_runtime/transport.py` to consume those exact bindings. Remove the two injected Undo callbacks from `build_pilot_runtime()`/`ConfirmationDependencies`; continuation resolves the exact ToolSpec binding from the existing Catalog until Segment handles arrive in Task 10. Remove `_with_write_contract`, `_with_runtime_metadata`, editable-field name lookup, confirmation-description name lookup, `_undo_seed_for_pending`, and `_build_write_undo` in this same atomic task.

Existing `MODEL_TOOL_NAMES`/`MODEL_TOOL_CATALOG` imports that have not yet moved to Bundle views may remain only until the final Runtime composition cutover in Task 11; they must be derived from the one newly compiled Catalog and must not augment or classify a tool. No placeholder Undo or presentation binding is permitted.

Compile exactly 25 ordered specs. Copy/freeze complete Provider payloads, precompile copied schemas, and verify resolver descriptor object identity. `build_model_tool_catalog()` must pass the 25 complete ordered payloads to `verify_provider_boundary()` before publishing the production Typed Catalog; generic 1..N test catalogs do not use the production seal. Task 4 exposes and independently tests Legacy boundary verification, but the actual ordered Legacy Adapter Catalog is verified only inside the all-or-nothing Task 9 production Composition; Task 4 must not fabricate a Legacy name collection or publish a partial Bundle. The internal `transactional_write` enum must project to existing external `write` everywhere outside V1 metadata.

- [ ] **Step 4: Migrate old tests instead of retaining compatibility exports**

Run `rg -n 'MODEL_TOOL_NAMES|MODEL_TOOL_CATALOG|ToolSpec\(|\.(kind|required_capabilities|binding_contract|binding_resolvers|confirmation_policy|editable_fields|write_contract|confirmation_description|result_metadata)\b' tests`. Review every match in the Task 4 scope and update every old `ToolSpec` constructor, `dataclasses.replace()` call, and direct attribute reader to the final shape. Noise from unrelated `.kind` fields is reviewed, not suppressed by narrowing the old-field list. Do not retain a legacy constructor branch or inspect `ToolSpec.__dataclass_fields__` in `tests/tool_metadata/factories.py`; after this task it constructs only the final `ToolSpec` shape.

`tests/agent_loop/helpers.py` and `tests/agent_loop/test_runner.py` must use complete freshly sealed Presentation bindings with module-level named callbacks and test probes as specified in Step 1. Their repeated inclusion is intentional: Task 4 handles only final ToolSpec/Presentation replacement, Task 9 handles Bundle/Selector composition, Task 10 handles Segment route handles, and Task 12 handles mechanical deletion gates. `tests/tool_authority/test_replay_topology.py` migrates only its Typed Presentation field access in Task 4; its Legacy proof/replay route migration remains in Task 11. Do not retain `MODEL_TOOL_NAMES` merely for tests, and do not auto-update a Golden.

`tests/tool_authority/test_approval_transaction.py` and `tests/tool_authority/test_read_uow.py` must replace copied production metadata with complete test-local metadata whose `dependencies=()` before constructing their intentional one-tool Catalogs; generic Catalog dependency closure remains fail-closed and production dependencies remain unchanged. In `src/offerpilot/ai/tool_authority/contracts.py`, validate each supplied capability through the closed `ToolCapability` enum directly; do not iterate/materialize the entire enum, create an automatic grant set, or add another capability-name collection.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata tests/agent_loop tests/tool_pipeline tests/tool_authority tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py tests/test_chat_api.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py -q
uv run ruff check src/offerpilot/ai src/offerpilot/api.py src/offerpilot/context_projector/authority_surface.py src/offerpilot/pilot_runtime/service.py src/offerpilot/pilot_runtime/composition.py src/offerpilot/pilot_runtime/continuation.py tests/tool_metadata tests/agent_loop tests/tool_pipeline tests/tool_authority tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py tests/test_chat_api.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py
uv run mypy src/offerpilot/ai src/offerpilot/api.py src/offerpilot/context_projector/authority_surface.py src/offerpilot/pilot_runtime/service.py src/offerpilot/pilot_runtime/composition.py src/offerpilot/pilot_runtime/continuation.py
```

Expected: production Catalog imports and all direct `ToolSpec` consumers collect and pass with no compatibility property; Provider/Authority/Journal projections remain byte/canonical equivalent.

- [ ] **Step 6: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-04.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 4 staged scope violation' }
git commit -m "refactor: AI 原子迁移二十五个工具契约"
```

## Task 5: Implement Bundle, views, and Segment lease primitives with synthetic tests

**Files:**

- Modify: `src/offerpilot/ai/tool_runtime/metadata.py`
- Modify: `src/offerpilot/ai/tool_runtime/catalog.py`
- Create: `tests/tool_metadata/test_bundle.py`
- Create: `tests/tool_metadata/test_segment_lease.py`

- [ ] **Step 1: Write RED Bundle/view/provenance tests**

Using only `tests.tool_metadata.factories`, prove exact view fields, immutability, Bundle-token equality, cross-Bundle rejection, closed-lease rejection, lease generation monotonicity, no deepcopy/recompile after composition, no pickle/asdict/copy/deepcopy, no callable/object address in repr, and no full Catalog reference in narrow views.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_bundle.py tests/tool_metadata/test_segment_lease.py -q
```

- [ ] **Step 3: Implement only generic primitives**

Implement `ToolMetadataBundleV1`, the six narrow views listed in §0, `SegmentToolCatalogLease`, typed route handles, Bundle fingerprinting, and provenance checks. Bundle construction is all-or-nothing and immutable.

Do not change `build_pilot_runtime()` or compose a production Bundle in this task. Production composition is deferred until real Operation, Compensation, Legacy, and proof registries exist; placeholders and mutable attach APIs are forbidden.

- [ ] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_bundle.py tests/tool_metadata/test_segment_lease.py tests/tool_metadata/test_manifest.py tests/tool_metadata/test_compiler.py -q
uv run ruff check src/offerpilot/ai/tool_runtime tests/tool_metadata
uv run mypy src/offerpilot/ai/tool_runtime
```

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-05.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 5 staged scope violation' }
git commit -m "feat: AI 建立工具元数据 Bundle 与 Segment 租约"
```

## Task 6: Implement Operation Port, required Undo, Compensation, and SQL compatibility gates

**Files:**

- Create: `src/offerpilot/pilot_runtime/primary_undo.py`
- Create: `src/offerpilot/pilot_runtime/compensation.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/ai/tool_runtime/metadata.py`
- Create: `tests/tool_metadata/test_operation_port.py`
- Create: `tests/tool_metadata/test_compensation_registry.py`
- Create: `tests/tool_metadata/test_primary_undo.py`
- Create: `tests/tool_metadata/test_published_operation_checks.py`
- Modify: `tests/test_schema_compatibility.py`
- Modify: `tests/test_write_operations.py`
- Modify: `tests/test_write_operation_acceptance_matrix.py`

- [ ] **Step 1: Write RED Operation/Undo/Compensation tests**

Prove exact 25 Typed primary, 3 Legacy primary, 4 Compensation, and 4 previously bound required Undo entries; exact route-handle provenance; session-bound compensation handlers; no tool-name set lookup; required Undo built before primary commit; and all failure/rollback/commit-unknown paths unchanged. `primary_undo.py` provides only shared runtime contract/checkpoint helpers and consumes the exact domain binding; it contains no tool-name branch and domain specs do not import it.

- [ ] **Step 2: Add the published SQLite CHECK equivalence test**

`test_published_operation_checks.py` must:

- assert both published constraint names and normalized SQL fragments remain unchanged;
- build temporary baseline and current SQLite schemas and run the same allow/reject operation-name matrix;
- assert the exact `ToolOperationMetadataPort` projection equals the CHECK allow set; Task 9 repeats this assertion against the completed production Bundle;
- assert Coordinator/Repository production code never parses CHECK SQL for routing;
- use `length(CAST(value AS BLOB))` behavior where the existing schema does so;
- never edit `models.py` or add a migration.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_operation_port.py tests/tool_metadata/test_compensation_registry.py tests/tool_metadata/test_primary_undo.py tests/tool_metadata/test_published_operation_checks.py tests/test_schema_compatibility.py -q
```

- [ ] **Step 4: Implement exact bindings without switching persistence callers**

Implement `ToolOperationMetadataPort`, opaque Typed/Legacy/Compensation route handles, shared Undo execution/checkpoint helpers, and exact four Compensation handler specs. Validate the four required-Undo bindings created in Task 4 against the exact handler registry. Handlers receive the caller-owned Session and never open another Session. Remove name switches inside the new modules.

This task exposes final Ports and tests them, but does not yet require every Pending/Ledger caller to persist a handle. The production persistence switch occurs after Legacy initial and confirmation handles exist in Task 11; no temporary handle or fallback is allowed.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_operation_port.py tests/tool_metadata/test_compensation_registry.py tests/tool_metadata/test_primary_undo.py tests/tool_metadata/test_published_operation_checks.py tests/test_schema_compatibility.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py -q
uv run ruff check src/offerpilot/pilot_runtime/primary_undo.py src/offerpilot/pilot_runtime/compensation.py src/offerpilot/ai/write_operations.py tests/tool_metadata
uv run mypy src/offerpilot/pilot_runtime/primary_undo.py src/offerpilot/pilot_runtime/compensation.py src/offerpilot/ai/write_operations.py
```

- [ ] **Step 6: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-06.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 6 staged scope violation' }
git commit -m "refactor: AI 收口写入与补偿元数据"
```

## Task 7: Make Legacy adapters static and implement request-scoped initial issuers

**Files:**

- Modify: `src/offerpilot/ai/tool_runtime/legacy.py`
- Modify: `src/offerpilot/ai/tool_specs/legacy.py`
- Modify: `src/offerpilot/pilot_runtime/contracts.py`
- Create: `tests/tool_metadata/test_legacy_initial_route.py`
- Modify: `tests/tool_pipeline/test_legacy.py`
- Modify: `tests/pilot_runtime/test_deterministic.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED static-adapter and issuer tests**

Cover exact three adapters, no captured Repository/Service/Session, approved Legacy seal, four reusable source-bound issuers, request-local owner/child leases, fresh token identity for consecutive same-source requests, two issuers mapping to the same Adapter remaining distinct, cross-source/container/Port rejection, issue/resolve/close linearization, ABA resistance, cancellation and every `BaseException` cleanup path.

Also assert `repr` contains no sensitive identity, copy/deepcopy/pickle/asdict/generic serialization all fail, owner close cascades revocation to every child token/handle, and none of these transient objects can enter Pending, Ledger, Journal, ChatMessage, checkpoint, HTTP, or SSE DTOs.

Add an AST ownership test proving the unpublished component factory has no production caller in this intermediate commit. Tests may import it directly; `build_pilot_runtime()`, API, deterministic services, and repositories may not. Characterize the existing `build_legacy_deterministic_catalog`, deterministic entry, and server-loaded confirmation signatures and behavior; they must remain unchanged and green until Task 9/11 replaces them. The task may add final static components but may not mutate the currently imported production factory or resolver.

- [ ] **Step 2: Add the four source-bound component matrix**

Exercise these source capabilities separately through the unpublished component factory:

```text
jd_clarification
jd_deterministic_action
submission_snapshot_action
outcome_recording_action
```

For each source, assert it receives only its source-bound issuer factory, opens one exact `RuntimeRequestOwnerLease`, issues a fresh child token, and closes/revokes in `finally` on success, Pending creation simulation, ordinary exception, cancellation, and other `BaseException`. Assert `confirmation_resume` cannot obtain any initial issuer or child-lease factory. This task does not publish the factory from application Composition or modify a production request entry; the real-entry matrix lands atomically with the final Bundle in Task 9.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_legacy_initial_route.py tests/tool_pipeline/test_legacy.py tests/pilot_runtime/test_deterministic.py tests/test_chat_api.py -q
```

- [ ] **Step 4: Implement trusted initial routing**

`legacy.py` owns `RuntimeRequestOwnerLease`, `LegacyInitialRequestLease`, their registry, four reusable source-bound issuer capabilities, and the exact initial-route Port. Implement one all-or-nothing component factory under new final symbols, but do not invoke or publish it from application Composition yet and do not alter the existing production factory/resolver. Only the exact issuer can derive a source-bound child lease and issue `ServerDeterministicInvocationToken`. `LegacyInitialRoutePort.resolve_initial(token)` accepts no source/name/Pending argument and returns one exact route handle. Request close and token consume are linearized under the registry lock. Task 9 atomically replaces the initial-route production path and deletes its old factory; no request can select between them.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_legacy_initial_route.py tests/tool_pipeline/test_legacy.py tests/pilot_runtime/test_deterministic.py tests/test_chat_api.py -q
uv run ruff check src/offerpilot/ai/tool_runtime/legacy.py src/offerpilot/ai/tool_specs/legacy.py src/offerpilot/pilot_runtime/contracts.py tests/tool_metadata tests/tool_pipeline/test_legacy.py
uv run mypy src/offerpilot/ai/tool_runtime/legacy.py src/offerpilot/ai/tool_specs/legacy.py src/offerpilot/pilot_runtime/contracts.py
```

- [ ] **Step 6: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-07.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 7 staged scope violation' }
git commit -m "refactor: AI 收口 Legacy 初始路由能力"
```

## Task 8: Implement Legacy confirmation preparation and one-shot proof routing

**Files:**

- Create: `src/offerpilot/ai/tool_runtime/legacy_proof.py`
- Create: `src/offerpilot/pilot_runtime/legacy_route.py`
- Modify: `src/offerpilot/ai/tool_runtime/legacy.py`
- Create: `tests/tool_metadata/test_legacy_confirmation_proof.py`
- Create: `tests/tool_metadata/test_legacy_registry_composition.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_confirmation_cutover.py`

- [ ] **Step 1: Write RED proof and Composition tests**

Cover preparation without execute capability; caller-owned read transaction; explicit-null edited args returning existing 422; locked mutable recheck; claim-before-proof; issuer/verifier/Session/Catalog/Bundle/operation/source/scope/fingerprint/key-ID mismatch; copied proof/evidence; concurrent single consume; rollback/revoke; terminal replay without Pending; reject/replay with preparation/proof/Catalog/executor all zero.

`test_legacy_registry_composition.py` must prove the unpublished component factory creates and identity-binds exactly one `LegacyPreparationRegistry`, one `LegacyRouteProofRegistry`, issuer-only registration port, proof consumer port, `LegacyPendingIdentityVerifierPort`, and Legacy Catalog component. Construction failure returns/publishes none of them. The application Composition Root does not invoke this factory until Task 9, where all identities are bound to the final Bundle atomically.

Add the same intermediate AST ownership assertion for the preparation/proof factory: no production caller exists before Task 9. Characterize and keep the currently imported `resolve_server_loaded(pending)` confirmation path byte/behavior compatible in this intermediate commit; this task adds proof components without changing that production method. Task 11 performs the one-time proof-route replacement and deletes the old method.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_legacy_confirmation_proof.py tests/tool_metadata/test_legacy_registry_composition.py tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py -q
```

- [ ] **Step 3: Implement preparation and proof registries**

Preparation verifies trusted Pending/Ledger identity in one caller-owned read transaction, closes it, and then performs pure validation. Public objects are identity tombstones and reject repr content, copy/deepcopy/pickle/asdict/generic serialization.

The Preparation Registry owns effective arguments through the complete state machine `prepared -> consumed -> executor_returned -> cleared`. Claim/proof failure revokes and clears immediately. After a successful proof consume, only the Preparation Registry retains the arguments until the unique executor call returns; Proof Registry, Catalog, proof, and route handle never retain them. Commit, rollback, ordinary exception, cancellation, and every other `BaseException` clear in `finally`. Add spies proving arguments exist only during the executor window and are absent afterward.

After `BEGIN IMMEDIATE`, mutable recheck, and confirmation claim CAS, `LegacyRouteProofIssuer.issue_after_claim()` revalidates locked primitives and existing Ledger HMAC fingerprints, atomically consumes the prepared call, and issues one proof. Catalog accepts only `resolve_server_loaded(proof)`. Catalog never receives Ledger key, Repository, ORM, raw/effective args, or ordinary DTO.

- [ ] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_legacy_confirmation_proof.py tests/tool_metadata/test_legacy_registry_composition.py tests/tool_authority/test_privacy.py tests/tool_authority/test_serialization.py tests/tool_pipeline/test_legacy.py tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py -q
```

Expected approve/modify order: prepare -> locked mutable recheck -> claim -> proof -> Catalog -> executor. Reject and terminal replay call all six zero times.

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-08.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 8 staged scope violation' }
git commit -m "refactor: AI 收口 Legacy 确认恢复证明"
```

## Task 9: Compose the complete production Bundle and switch Context Projector views

**Files:**

- Modify: `src/offerpilot/ai/tool_runtime/legacy.py`
- Modify: `src/offerpilot/ai/tool_specs/legacy.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/pilot_runtime/deterministic.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `src/offerpilot/context_projector/selector.py`
- Modify: `src/offerpilot/context_projector/authority_surface.py`
- Modify: `src/offerpilot/context_projector/manifest.py`
- Modify: `src/offerpilot/context_projector/projector.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/tool_metadata/test_production_bundle.py`
- Create: `tests/tool_metadata/test_selector_views.py`
- Modify: `tests/tool_metadata/test_published_operation_checks.py`
- Modify: `tests/tool_metadata/test_legacy_initial_route.py`
- Modify: `tests/tool_metadata/test_legacy_registry_composition.py`
- Modify: `tests/test_context_projector.py`
- Modify: `tests/test_context_projector_source_gates.py`
- Modify: `tests/pilot_runtime/test_deterministic.py`
- Modify: `tests/agent_loop/test_runner.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED complete-Bundle and Selector tests**

Prove one application Composition factory atomically creates and publishes the complete Bundle containing the compiled 25 Typed specs, static 3 Legacy adapters, 4 Compensation handlers, complete Operation Port, and all six narrow views together with the exact initial Registry/Port, preparation/proof Registries, issuer/consumer ports, verifier port, and Legacy Catalog. Prove initialization failure publishes no Runtime or subcomponent. Spy on the production Legacy seal integration and prove it consumes the projection from the actual three ordered Adapter objects; an injected seal failure publishes no Runtime, Bundle, Catalog, Registry, or Port. Every issuer, proof, Catalog, route handle, Compensation handler, and view must carry the same final Bundle/Catalog provenance; no pre-Bundle identity may escape.

Update the component-factory AST ownership tests: exactly the approved final Composition factory may call them. API, `deterministic.py`, service, continuation, Repository, and every other Runtime builder must receive injected capabilities and may not construct/publish a second instance.

Repeat the SQLite compatibility assertion against this completed production Bundle: its Operation view must equal the published CHECK allow set. Add the four real deterministic entry sources here and prove each receives only its matching source-bound issuer factory, creates a fresh request owner lease, and closes/revokes on success, Pending creation, ordinary exception, cancellation, and every other `BaseException`. `confirmation_resume` receives no initial issuer/factory.

The final Selector signature and return contract are:

```text
select_tools(discovery_view, authority_view, trusted_signals) -> ToolSelectionResult

ToolSelectionResult
  provider_contracts: tuple[ProviderToolContract, ...]
  provider_envelope_fingerprint: str
  selected_names: tuple[str, ...]
  selected_domains: tuple[ToolDomain, ...]
  dependency_closure: tuple[str, ...]
  full_catalog_fallback: bool
  fallback_reason: no_trusted_signal | declared_ambiguous_input | null
  diagnostics: tuple[closed safe diagnostic enum/count records, ...]
```

Tests require both views to carry the same Bundle token. Cross-Bundle views fail closed. No signal or declared ambiguity selects full 25; invalid/unknown inputs fail closed. Dependency closure preserves full Catalog order and never admits Legacy. Selector computes `provider_envelope_fingerprint` exactly once from the ordered complete envelopes using the approved canonical algorithm. `SegmentSurfaceGate`, `ModelSurfaceProjector`, and `_surface_selection_matches()` consume this result directly; no caller rebuilds contracts from names or recomputes the fingerprint.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_production_bundle.py tests/tool_metadata/test_selector_views.py tests/tool_metadata/test_published_operation_checks.py tests/tool_metadata/test_legacy_initial_route.py tests/tool_metadata/test_legacy_registry_composition.py tests/test_context_projector.py tests/test_context_projector_source_gates.py tests/pilot_runtime/test_deterministic.py tests/agent_loop/test_runner.py tests/test_chat_api.py -q
```

- [ ] **Step 3: Compose once and inject the Bundle views**

`build_pilot_runtime()` invokes the initial-route, proof, Compensation, and Typed component factories inside one non-publishing assembly scope, binds their opaque registries to the final Bundle/Catalog tokens, validates the complete graph, and only then publishes one Runtime. Provider builder consumes only `ProviderToolMetadataView`; Projector and Agent Loop receive Discovery and Authority views from that same Bundle. Migrate `SegmentSurfaceGate` and every Agent Loop Selector call to `ToolSelectionResult` in this task; no old-signature façade remains. Remove Selector-local domain/dependency/name maps and `DEPENDENCY_POLICY_V1` runtime imports. Update Manifest validation to compare against the injected Provider view rather than `MODEL_TOOL_NAMES`.

Before publication, Composition must pass the exact ordered names, `LegacyBoundaryVisibility.FORBIDDEN`, and `legacy_deterministic` adapter kind projected from the actual three Adapter objects to `verify_legacy_boundary()`. It must not verify a fixture, a separately maintained name tuple, or a Typed Catalog projection.

Fallback within one `model_call_id` reuses the same frozen Provider surface. An unexposed tool remains fail-closed before Dispatcher. Tool visibility does not replace Pipeline authorization.

- [ ] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/tool_metadata/test_production_bundle.py tests/tool_metadata/test_selector_views.py tests/tool_metadata/test_published_operation_checks.py tests/tool_metadata/test_legacy_initial_route.py tests/tool_metadata/test_legacy_registry_composition.py tests/test_context_projector.py tests/test_context_projector_source_gates.py tests/pilot_runtime/test_deterministic.py tests/agent_loop/test_runner.py tests/test_chat_api.py tests/tool_pipeline/test_catalog.py tests/tool_pipeline/test_golden_assets.py -q
```

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-09.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 9 staged scope violation' }
git commit -m "refactor: AI 统一工具发现与 Provider 视图"
```

## Task 10: Switch Authority and Pipeline to Segment handles

**Files:**

- Modify: `src/offerpilot/ai/tool_authority/contracts.py`
- Modify: `src/offerpilot/ai/tool_authority/composition.py`
- Modify: `src/offerpilot/ai/tool_runtime/pipeline.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Create: `tests/tool_metadata/test_authority_views.py`
- Create: `tests/tool_metadata/test_pipeline_handles.py`
- Modify: `tests/tool_authority/test_hardening.py`
- Modify: `tests/tool_authority/test_serialization.py`
- Modify: `tests/tool_pipeline/test_pipeline.py`
- Modify: `tests/agent_loop/test_runner.py`

- [ ] **Step 1: Write RED provenance and one-call tests**

Prove one Segment opens one `SegmentToolCatalogLease`; Provider ToolCall resolves once to a typed route handle; Authority and Pipeline require exact Bundle/Segment provenance; copied/closed/cross-Bundle handles fail before resolver/preflight/executor; executor remains at most once per `execute_prepared()`.

- [ ] **Step 2: Lock the complete Pipeline parameter mapping**

Preserve every existing parameter and side effect while adding the exact handle:

```text
prepare_call:
  catalog lease, execution context, ToolCall, call identity, Pending identity,
  Pending revision, stage sink, proposal recorder

execute_prepared:
  PreparedToolCall, execution context, call identity, confirmation claimer,
  execution claim, locked effective-args digest, stage sink
```

The route handle replaces name lookup only; it must not remove proposal Journal events, Pending revision, confirmation claim/CAS, locked digest, mutable recheck, or stage-sink behavior.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_authority_views.py tests/tool_metadata/test_pipeline_handles.py tests/tool_authority/test_hardening.py tests/tool_pipeline/test_pipeline.py -q
```

- [ ] **Step 4: Implement and verify GREEN**

Authority consumes only `ToolAuthorityMetadataView`. Pipeline accepts exact Segment route handles and preserves capability-before-binding short-circuit, binding audit-only behavior, prepare/execute separation, claim-before-executor, ordinary `Exception` mapping, `BaseException` propagation, and Journal fail-open projection.

```powershell
uv run pytest tests/tool_metadata/test_authority_views.py tests/tool_metadata/test_pipeline_handles.py tests/tool_authority/test_hardening.py tests/tool_authority/test_serialization.py tests/tool_pipeline/test_pipeline.py tests/tool_pipeline/test_journal.py tests/agent_loop/test_runner.py -q
```

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-10.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 10 staged scope violation' }
git commit -m "refactor: AI 统一工具授权与执行句柄"
```

## Task 11: Switch Pending, Ledger, Undo, and chained topology

**Files:**

- Modify: `src/offerpilot/ai/tool_runtime/legacy.py`
- Modify: `src/offerpilot/ai/confirmation.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/repositories/chat.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/pilot_runtime/persistence.py`
- Modify: `src/offerpilot/pilot_runtime/deterministic.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `src/offerpilot/chat_transport.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/tool_metadata/test_presentation_bindings.py`
- Create: `tests/tool_metadata/test_pending_routes.py`
- Create: `tests/tool_metadata/test_runtime_cutover.py`
- Modify: `tests/tool_authority/test_legacy_replay_preconversation.py`
- Modify: `tests/tool_authority/test_pending_claim.py`
- Modify: `tests/tool_authority/test_pending_claim_reissue.py`
- Modify: `tests/tool_authority/test_replay_topology.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_confirmation_cutover.py`
- Modify: `tests/tool_pipeline/test_legacy.py`
- Modify: `tests/test_chat_repository.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED presentation, route, and topology tests**

Golden-test success and every declared failure renderer, confirmation descriptions, sync/SSE transport payloads, result shape, and no exception/raw-args/result persistence. Test exact Typed, Legacy-initial, Legacy-proof, and Compensation route handles for initial, replacement, chained, continuation, replay, and compensation paths. Add the production approve/modify order `prepare -> locked mutable recheck -> claim -> proof -> Catalog -> executor`; reject and terminal replay must call all six zero times.

Add the trusted chained matrix:

```text
typed -> typed: allowed by existing rule
typed -> legacy: rejected
same exact Legacy adapter -> same exact Legacy adapter: allowed by existing rule
one Legacy adapter -> another Legacy adapter: rejected
Legacy -> typed: rejected
Compensation -> any child: rejected
```

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_presentation_bindings.py tests/tool_metadata/test_pending_routes.py tests/tool_metadata/test_runtime_cutover.py tests/tool_authority/test_legacy_replay_preconversation.py tests/tool_authority/test_pending_claim.py tests/tool_authority/test_pending_claim_reissue.py tests/tool_authority/test_replay_topology.py tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_confirmation_cutover.py tests/tool_pipeline/test_legacy.py tests/test_chat_repository.py tests/test_chat_api.py -q
```

- [ ] **Step 3: Perform the single persistence/runtime route cutover**

Every proposal, Pending insertion/replacement, chained Pending, confirmation continuation, primary Operation, and Compensation Operation must consume an exact sealed route handle issued by the current Bundle/Segment/Legacy proof registry. Persist only the approved primitive identity fields; never persist the handle or Bundle token.

Replace and delete the transitional `resolve_server_loaded(pending)` production method in this task. Migrate `tests/tool_pipeline/test_legacy.py` from direct `ServerPending` calls to proof-only resolution in the same atomic change. Confirmation resume accepts only the one-shot `LegacyRouteProof`; no old raw-Pending overload, compatibility façade, or runtime fallback remains.

Consume the exact ToolSpec presentation and Undo bindings established in Task 4 through the new route handles; no name-based presentation/Undo helper may return. Replace `_chained_adapter_kind()` with sealed `ChainedPendingTopologyPolicyV1`. Preserve atomic parent delivery, child proposal, Pending replacement, required Undo before commit, terminal replay, response-loss recovery, and delivery fencing.

Task 11's repeated modification of `tests/tool_authority/test_replay_topology.py` is limited to the Legacy proof/replay route cutover described here. The old Typed `ToolSpec.confirmation_description` attribute read was already removed in Task 4 and must not be reintroduced.

Terminal replay, delivery recovery, and fallback initialize neither Provider, Projector, Bundle resolution, resolver, preflight, proof registry, nor executor. Reject bypasses Schema, binding, preflight, proof, and executor.

- [ ] **Step 4: Verify integrated GREEN**

```powershell
uv run pytest tests/tool_metadata/test_presentation_bindings.py tests/tool_metadata/test_pending_routes.py tests/tool_metadata/test_runtime_cutover.py tests/agent_loop tests/pilot_runtime tests/tool_pipeline/test_legacy.py tests/tool_authority/test_replay_topology.py tests/tool_authority/test_pending_replay_decoder.py tests/test_chat_repository.py tests/test_chat_api.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py -q
```

Expected: Provider/tool call counts, HTTP/SSE order, HITL, Journal events, Ledger state, Undo, and business writes remain externally equivalent.

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-11.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 11 staged scope violation' }
git commit -m "refactor: AI 完成工具元数据生产切换"
```

## Task 12: Delete global Catalog/classification paths and add mechanical gates

**Files:**

- Modify: `src/offerpilot/ai/tool_specs/catalog.py`
- Modify: `src/offerpilot/ai/tool_specs/__init__.py`
- Modify: `src/offerpilot/context_projector/selector.py`
- Modify: `src/offerpilot/context_projector/manifest.py`
- Modify: `src/offerpilot/context_projector/projector.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/agent_loop/test_deletion_gates.py`
- Modify: `tests/tool_pipeline/test_source_gates.py`
- Modify: `tests/tool_authority/test_source_gates.py`
- Create: `tests/tool_metadata/test_deletion_gates.py`
- Modify: `tests/test_mock_legacy_removed.py`
- Modify: `tests/test_pilot_runtime_extraction_gate.py`
- Modify: the exact frozen Task 12 `$classificationConsumerSet` resolved from the fixed baseline in §0

- [ ] **Step 1: Add RED source/AST gates**

Reject these symbols or equivalent runtime roles outside approved historical assets:

```text
MODEL_TOOL_NAMES
MODEL_TOOL_CATALOG
LEGACY_DETERMINISTIC_NAMES
TRANSACTIONAL_TYPED_WRITE_NAMES
REQUIRED_UNDO_TOOL_NAMES
TYPED_WRITE_OPERATION_NAMES
LEGACY_WRITE_OPERATION_NAMES
COMPENSATION_OPERATION_NAMES
REQUIRED_UNDO_OPERATION_NAMES
WRITE_OPERATION_NAMES
DEPENDENCY_POLICY_V1
_pending_adapter_kind
_chained_adapter_kind
_with_write_contract
_with_runtime_metadata
editable_fields_for_tool
_undo_seed_for_pending
_build_write_undo
_CREATED_RECORD_FINGERPRINT_FIELDS
legacy_catalog_factory
build_legacy_deterministic_catalog
_legacy_catalog
_legacy_adapter
```

Also reject name-based sets/maps/switches, string-prefix routing, reflective classification, non-Composition Bundle construction, Provider dict registries, Dispatcher Legacy imports, Typed-to-Legacy fallback, feature flags, shadow/double execution, Golden writers, Legacy proof imports of Ledger/keyring/Repository/ORM/Pilot Runtime, initial route calls that pass source/name/Pending rather than an exact token, and any initial/proof component-factory call outside the single approved final Composition factory.

For Legacy specifically, permit only the exact static three-Adapter declaration and proof-bound final Catalog method. AST must reject the old `build_legacy_deterministic_catalog` symbol, any Legacy factory parameter/capture of Repository or Service, any `resolve_server_loaded` parameter typed/named as ordinary Pending, and every call that passes Pending/tool name instead of `LegacyRouteProof`.

Exact allowlists are limited to published `models.py` CHECK text, Journal `_TOOL_NAMES`, Provider declarations, three Legacy Adapter declarations, four Compensation handler declarations, protocol seals, terminal persisted-payload renderers, and read-only test fixtures.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/tool_metadata/test_deletion_gates.py tests/agent_loop/test_deletion_gates.py tests/tool_pipeline/test_source_gates.py tests/tool_authority/test_source_gates.py tests/test_mock_legacy_removed.py tests/test_pilot_runtime_extraction_gate.py -q
```

- [ ] **Step 3: Delete every old path and migrate all remaining imports**

Run:

```powershell
rg -n "MODEL_TOOL_NAMES|MODEL_TOOL_CATALOG|DEPENDENCY_POLICY_V1|TRANSACTIONAL_TYPED_WRITE_NAMES|REQUIRED_UNDO_TOOL_NAMES|TYPED_WRITE_OPERATION_NAMES|LEGACY_WRITE_OPERATION_NAMES|COMPENSATION_OPERATION_NAMES|WRITE_OPERATION_NAMES|LEGACY_DETERMINISTIC_NAMES|build_legacy_deterministic_catalog|resolve_server_loaded" src tests
```

Delete production definitions and update all callers to injected Bundle views/Ports. Do not weaken gates or retain a re-export. Keep published CHECK strings and Journal whitelist byte-identical. Verify `jsonschema==4.26.0` remains exact in `pyproject.toml` and `uv.lock`.

- [ ] **Step 4: Verify GREEN, privacy, serialization, and SQL assets**

```powershell
uv run pytest tests/tool_metadata/test_deletion_gates.py tests/tool_metadata/test_published_operation_checks.py tests/agent_loop/test_deletion_gates.py tests/tool_pipeline/test_source_gates.py tests/tool_pipeline/test_checkpoint.py tests/tool_pipeline/test_journal.py tests/tool_pipeline/test_transport.py tests/tool_authority/test_source_gates.py tests/tool_authority/test_privacy.py tests/tool_authority/test_serialization.py tests/test_agent_run_journal.py tests/test_mock_legacy_removed.py tests/test_pilot_runtime_extraction_gate.py -q
```

Expected: no transient Bundle/view/lease/proof/handle appears in ChatMessage, Pending, Ledger payload, Journal, checkpoint, Prompt, HTTP/SSE, log, repr, pickle, or generic serialization.

- [ ] **Step 5: Commit**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-12.txt" | Where-Object { $_ })
git add -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 12 staged scope violation' }
git commit -m "test: AI 固化工具元数据旧路径删除门禁"
```

## Task 13: Run release verification, independent CR, and write the report

**Files:**

- Verify unchanged scope anchor: `docs/superpowers/specs/2026-08-25-tool-metadata-convergence-design.md`
- Verify unchanged scope anchor: `docs/superpowers/plans/2026-08-25-tool-metadata-convergence.md`
- Create: `docs/reports/2026-08-25-tool-metadata-convergence-release-verification.md`
- Modify only when a gate finds a real defect: the exact materialized union of files already listed by Tasks 1-12

`task-13.txt` contains the two scope anchors, the report, and that inherited Tasks 1-12 union. Consequently, the union of `task-01.txt ... task-13.txt` must equal `allowlist.txt` byte-for-byte after ordinal sorting and duplicate removal.

- [ ] **Step 1: Run focused backend suites in bounded groups**

```powershell
uv run pytest tests/tool_metadata tests/tool_pipeline -q
uv run pytest tests/tool_authority -q
uv run pytest tests/test_context_projector.py tests/test_context_projector_source_gates.py -q
uv run pytest tests/agent_loop tests/pilot_runtime -q
uv run pytest tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py tests/test_chat_repository.py tests/test_chat_api.py -q
```

Record exact counts/durations. Reject unexpected skip, duplicate node ID, missing group, or extra group.

- [ ] **Step 2: Run full backend/static gates**

Use the repository's Windows group manifest/aggregate procedure and compare its union with `uv run pytest --collect-only -q`. Then run:

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
git diff --check
```

The external Application-JD baseline/allowlist gate may be excluded only when its release-orchestrator inputs are genuinely unavailable. Record the exact command and reason; never fabricate inputs.

- [ ] **Step 3: Run frontend and local verification**

```powershell
Push-Location web
npm test -- --run
npm run build
Pop-Location
uv run oc smoke --static-dir web/dist
uv run oc verify --local
```

- [ ] **Step 4: Run controlled real-AI and browser compatibility loops**

Verify workspace and application contexts over sync and SSE:

```text
read + read
write + read
read + write
write + write
approve
modify
reject
chained Pending
terminal replay / delivery recovery
all four Legacy initial sources
one Legacy confirmation resume for each of the three adapters
```

Record Provider attempts, executor counts, Pending/Ledger state, final business readback, Journal/Trace health, console errors, duplicate API/SSE calls, and screenshots. Never log credentials or lock model prose.

- [ ] **Step 5: Request independent CR and close every P0/P1/P2**

Use `superpowers:requesting-code-review` or an equivalent fresh subagent. Review the fixed-baseline diff with emphasis on exact 25/3/4 isolation, Provider seals, Bundle/View provenance, ToolSpec consumer migration, Operation/Pending handles, Legacy owner lease and proof authorization, caller-owned Session boundaries, SQL CHECK equivalence, replay, privacy, deletion gates, and absence of dual paths. Fix every finding with a RED/GREEN test and repeat until no P0/P1/P2 remains.

- [ ] **Step 6: Verify immutable scope and write the report**

```powershell
$gateRoot = Join-Path $env:TEMP 'offerpilot-tool-metadata-convergence-gate'
$locatorPath = Join-Path $env:TEMP 'offerpilot-tool-metadata-convergence-gate.locator.json'
$locator = Get-Content -LiteralPath $locatorPath -Raw | ConvertFrom-Json
$baselinePath = Join-Path $gateRoot 'baseline.txt'
$startPath = Join-Path $gateRoot 'implementation-start.txt'
$allowlistPath = Join-Path $gateRoot 'allowlist.txt'
$baseline = (Get-Content -LiteralPath $baselinePath -Raw).Trim()
$start = (Get-Content -LiteralPath $startPath -Raw).Trim()
$branch = (git branch --show-current).Trim()
$worktree = (Resolve-Path '.').Path
if ($baseline -ne '0c10e05e256eb757d5f89a8b009dcea193f2fc78') { throw 'fixed baseline mismatch' }
if ($start -ne 'bf879fd8f10575e3996bacf8ff9c6ccc8ab7cc64') { throw 'fixed implementation-start mismatch' }
if ($locator.worktree -ne $worktree -or $locator.branch -ne $branch) { throw 'locator identity mismatch' }
if ($locator.baseline_file -ne $baselinePath -or $locator.implementation_start_file -ne $startPath -or $locator.allowlist_file -ne $allowlistPath) { throw 'locator gate path mismatch' }
$allowlist = @(Get-Content -LiteralPath $allowlistPath | Where-Object { $_ } | Sort-Object -Unique)
$taskGatePaths = @(1..13 | ForEach-Object { Join-Path $gateRoot ("task-{0:D2}.txt" -f $_) })
$missingTaskGates = @($taskGatePaths | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missingTaskGates.Count -ne 0) { throw "task gate file is missing: $($missingTaskGates -join ', ')" }
$taskUnion = @($taskGatePaths | ForEach-Object {
    Get-Content -LiteralPath $_ -ErrorAction Stop | Where-Object { $_ }
} | Sort-Object -Unique)
if (@(Compare-Object $allowlist $taskUnion).Count -ne 0) { throw 'task union does not equal allowlist' }
$committed = @(git diff --name-only "$baseline..HEAD" | Where-Object { $_ })
$working = @(git diff --name-only | Where-Object { $_ })
$staged = @(git diff --cached --name-only | Where-Object { $_ })
$untracked = @(git ls-files --others --exclude-standard)
$reportPath = 'docs/reports/2026-08-25-tool-metadata-convergence-release-verification.md'
$reportCandidate = @()
if ((Test-Path -LiteralPath $reportPath) -and -not (git ls-files --error-unmatch $reportPath 2>$null)) { $reportCandidate = @($reportPath) }
$observed = @($committed + $working + $staged + $untracked + $reportCandidate | Sort-Object -Unique)
$outside = @($observed | Where-Object { $allowlist -notcontains $_ })
if ($outside.Count -ne 0) { throw "changed path outside immutable allowlist: $($outside -join ', ')" }
$dirtyOutsideReport = @(($working + $staged + $untracked) | Sort-Object -Unique | Where-Object { $_ -ne $reportPath })
if ($dirtyOutsideReport.Count -ne 0) { throw "unexpected dirty paths before report commit: $($dirtyOutsideReport -join ', ')" }
if (-not (Test-Path -LiteralPath $reportPath)) { throw 'release report is missing' }
git status --short --branch
git diff --check
```

The report records fixed baseline/final commit, implementation-start commit, internal destructive cutover, exact 25/3/4 boundary, unchanged external contracts, every verification result, independent CR, external exclusions, remaining risks, and explicit non-goals. It must not claim cross-request or external-system exactly-once.

- [ ] **Step 7: Commit evidence**

```powershell
$taskPaths = @(Get-Content -LiteralPath "$env:TEMP\offerpilot-tool-metadata-convergence-gate\task-13.txt" | Where-Object { $_ })
$gateRoot = Join-Path $env:TEMP 'offerpilot-tool-metadata-convergence-gate'
$allowlist = @(Get-Content -LiteralPath (Join-Path $gateRoot 'allowlist.txt') | Where-Object { $_ } | Sort-Object -Unique)
$taskGatePaths = @(1..13 | ForEach-Object { Join-Path $gateRoot ("task-{0:D2}.txt" -f $_) })
$missingTaskGates = @($taskGatePaths | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missingTaskGates.Count -ne 0) { throw "task gate file is missing: $($missingTaskGates -join ', ')" }
$taskUnion = @($taskGatePaths | ForEach-Object {
    Get-Content -LiteralPath $_ -ErrorAction Stop | Where-Object { $_ }
} | Sort-Object -Unique)
if (@(Compare-Object $allowlist $taskUnion).Count -ne 0) { throw 'task union does not equal allowlist' }
git add -f -- $taskPaths
if (@(git diff --cached --name-only | Where-Object { $taskPaths -notcontains $_ }).Count -ne 0) { throw 'Task 13 staged scope violation' }
git commit -m "docs: AI 验证工具元数据收敛发布门禁"
$dirty = @(git status --porcelain --untracked-files=all)
if ($dirty.Count -ne 0) { throw "worktree is not clean: $($dirty -join '; ')" }
git status --short --branch
```

Expected: clean worktree; do not push or merge.

## Execution order and safe parallelism

Required order:

```text
Task 1 -> Task 2 -> Task 3 -> Task 4 -> Task 5 -> Task 6
       -> Task 7 -> Task 8 -> Task 9 -> Task 10 -> Task 11
       -> Task 12 -> Task 13
```

After Task 3, tests for the six domain declarations in Task 4 may be prepared in parallel, but one owner performs the atomic ToolSpec/Catalog/consumer cutover and combined commit. Task 6 test preparation may overlap Task 5 only after Bundle contracts are frozen. Tasks 7 and 8 are security-sensitive and integrate serially. Selector tests for Task 9 and Authority tests for Task 10 may be prepared in parallel, but production composition must land before either GREEN claim. Tasks 11-13 are serial.

Intermediate commits may add unused tested leaf contracts and Ports, but production requests must never choose between old and new paths. The only complete production composition occurs after Typed, Operation, Compensation, Legacy initial, and Legacy proof components all exist. No placeholder component, mutable Bundle attachment, feature flag, shadow execution, automatic fallback, or long-lived façade is permitted.

## Completion statement

Declare completion only when all of these are true:

```text
Typed Catalog = exact 25
Legacy Boundary = exact 3
Compensation Registry = exact 4
Provider/Discovery/Authority/Operation Goldens unchanged
Provider and Legacy seals independently recomputed
all production consumers use one Bundle and exact Segment/route/proof identities
four Legacy initial sources use fresh source-bound request tokens
confirmation resume uses claim-after-proof and no initial issuer
published SQLite CHECK semantics unchanged
terminal replay and reject remain Provider/Projector/executor free
no old name-based runtime classification remains outside exact historical allowlists
HTTP/SSE/HITL/Ledger/Journal/database and business side effects remain compatible
all release gates and independent CR pass
worktree is clean; branch is not pushed or merged
```
