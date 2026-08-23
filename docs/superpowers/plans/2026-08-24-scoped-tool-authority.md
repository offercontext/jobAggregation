# Scoped Capability & Binding Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce an explicit, versioned Capability Profile and frozen Binding Contract for all 25 model-visible Typed Tools, including SQL-level Application scoping and confirmation-time scope revalidation, while preserving Provider schemas, HTTP/SSE, HITL, Ledger, Undo, Journal, and business transaction semantics.

**Architecture:** A trusted Conversation snapshot creates either a sealed `SegmentExecutionAuthority` or `ApprovalExecutionAuthority`. The authority-bound `ToolExecutionContext` is the only execution context accepted by the Typed Pipeline. Provider visibility is the intersection of the frozen 25-tool catalog and the Segment Authority, while execution independently rechecks capability, binding, scope constraint, and—when writing—a one-shot Ledger-issued `ExecutionClaim`. Conversation scope revision and a private HMAC fingerprint bind every new Typed proposal to its trusted scope. Reject, terminal replay, delivery recovery, and the three Legacy deterministic tools remain provider-free and outside Typed Authority execution.

**Tech Stack:** Python 3.12, pytest, SQLAlchemy 2, SQLite triggers and guarded SQL, existing Agent Loop/Context Projector/Provider Gateway/Write Operation Ledger/Journal/Pilot Runtime, uv, Ruff, Mypy, Vitest, Vite, local static smoke.

---

## 0. Fixed boundary, baseline, and invariants

Work only in:

```text
D:\Users\yuqi.chen\offerpilot\.worktrees\feat-20260824-scoped-tool-authority
```

Branch and baselines:

```text
branch: feat/20260824-scoped-tool-authority
repository baseline: 1574d0e891391c817c325f598b4f22f8a7838330
production characterization baseline: 2427fa6 (Agent Loop merge)
approved design: docs/superpowers/specs/2026-08-24-scoped-tool-authority-design.md
```

`1574d0e` differs from `2427fa6` only by the approved design merge. Do not push or merge. Do not touch the root workspace or another worktree. Do not add an audit-only mode, feature flag, shadow execution, Typed-to-Legacy fallback, Provider double-send, automatic golden updater, or compatibility bypass.

Fixed external contracts:

```text
25 Provider Tool envelopes, order, descriptions, and JSON Schemas remain byte/canonical equivalent.
HTTP/SSE request and response shapes remain unchanged.
All write tools continue to require HITL; auto_approve does not bypass confirmation.
Journal event types/schema remain unchanged.
Ledger, Undo, delivery fencing, terminal replay, and business side effects preserve baseline semantics.
RuntimeTransportAborted remains the only failure-only transport compatibility exception.
```

Fixed internal breaking changes:

```text
Delete direct capabilities/current_bindings construction from ToolExecutionContext.
Delete ExecutionAuthorization and replace it with sealed one-shot ExecutionClaim.
Delete ResolvedModel.tool_context and the broad model_tool_context API callback.
Delete Typed read-time Pending/Operation lazy backfill.
Delete typed_catalog_drift -> injected surface fallback.
All new Typed Pending proposals require PendingAuthorityClaim.
All Application-owned final reads and writes require an exact authority-bound scope constraint.
```

### File responsibilities

```text
src/offerpilot/ai/tool_authority/contracts.py
  Leaf enums, immutable transient DTOs, opaque tokens, call identities, authority types,
  Binding contracts/resolutions, constraints, Pending/Execution/Reject proofs.

src/offerpilot/ai/tool_authority/policy.py
  agent_typed_v1 profile, six version constants, binding aggregation/decision,
  manifest validation, canonical public fingerprints.

src/offerpilot/ai/tool_authority/composition.py
  Registry-backed factories for Segment/Approval authorities, contexts, constraints,
  Prepared/Pending/Execution claims, and lifecycle revocation.

src/offerpilot/ai/tool_authority/fingerprint.py
  Canonical Conversation scope envelope and write-operation-authorization-scope-v1 HMAC.

src/offerpilot/ai/tool_authority/visibility.py
  One active-Application visibility query with raw sqlite and SQLAlchemy Session adapters.

src/offerpilot/context_projector/authority_surface.py
  Capability/scope intersection over the complete 25-tool catalog.

src/offerpilot/ai/tool_runtime/contracts.py
src/offerpilot/ai/tool_runtime/context.py
src/offerpilot/ai/tool_runtime/catalog.py
src/offerpilot/ai/tool_runtime/pipeline.py
  Authority-bound ToolSpec preparation, read UoW, sealed write execution, and safe outcomes.

src/offerpilot/ai/tool_specs/{applications,application_events,notes,offers,resumes,jd_analyses}.py
src/offerpilot/ai/tool_specs/catalog.py
  Frozen 25-tool capability/binding/resolver metadata; unchanged Provider contracts.

src/offerpilot/repositories/{applications,application_events,notes,offers,jd}.py
  Caller-session scoped collection/point/mutation SQL with exact constraint guards.

src/offerpilot/repositories/chat.py
  Atomic scope create/patch, read-only Pending identity preheader, Typed Pending claim port,
  Legacy Pending port, and no read-time writes.

src/offerpilot/models.py
src/offerpilot/db.py
  scope_revision, authorization_scope_fingerprint, migration 0028, checks and triggers.

src/offerpilot/ai/write_operations.py
src/offerpilot/pilot_runtime/continuation.py
  Ledger preheader, locked approval recheck/claim, reject proof, strict chained replay.

src/offerpilot/context_projector/{selector,projector,loader}.py
src/offerpilot/ai/agent_loop.py
src/offerpilot/ai/agent_contracts.py
  DependencyPolicyV1, authority surface, seed ownership, read/write dispatch boundaries.

src/offerpilot/pilot_runtime/{composition,service,persistence}.py
src/offerpilot/api.py
  Source-before-authority runtime assembly, split policy/model resolvers, four sync/stream paths.

tests/fixtures/tool_authority/*.json
  Immutable canonical characterization and policy assets; no writer or update switch.

docs/reports/2026-08-24-scoped-tool-authority-release-verification.md
  Characterization/red/green evidence, compatibility, migration, CR, risks, and final state.
```

### Task 1: Freeze the Agent Loop production characterization

**Files:**
- Create: `tests/tool_authority/__init__.py`
- Create: `tests/tool_authority/golden.py`
- Create: `tests/tool_authority/test_baseline_golden.py`
- Create: `tests/fixtures/tool_authority/baseline_2427fa6.json`
- Create: `tests/fixtures/tool_authority/authority_manifest_v1.json`
- Create: `tests/fixtures/tool_authority/policy_fingerprints_v1.json`
- Read: `tests/fixtures/tool_pipeline/provider_manifest_30c944f.json`
- Read: `tests/fixtures/tool_pipeline/tool_outcomes_30c944f.json`
- Read: `tests/fixtures/tool_pipeline/journal_sequences_30c944f.json`
- Read: `tests/fixtures/agent_loop/baseline_aaecf5d.json`
- Read: `tests/fixtures/pilot_runtime/baseline_golden.json`

- [ ] **Step 1: Prove the merged design did not change production**

Run:

```powershell
$changed = git diff --name-only 2427fa6..1574d0e -- src pyproject.toml uv.lock
if ($changed) { throw "production differs from Agent Loop merge: $changed" }
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Write the failing read-only golden test**

Create `tests/tool_authority/golden.py` with only `load_golden()` and `canonical_json()`. Create `test_baseline_golden.py` with these pinned facts:

```python
REPOSITORY_BASELINE = "1574d0e891391c817c325f598b4f22f8a7838330"
PRODUCTION_BASELINE = "2427fa6"
LEGACY_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)


def test_scoped_authority_baseline_is_canonical_and_private() -> None:
    raw = GOLDEN.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schema_version"] == 1
    assert value["repository_baseline"] == REPOSITORY_BASELINE
    assert value["production_baseline"] == PRODUCTION_BASELINE
    assert value["typed_tool_names"] == list(MODEL_TOOL_NAMES)
    assert value["legacy_deterministic_names"] == list(LEGACY_NAMES)
    assert raw == canonical_json(value) + "\n"
```

The test must also pin: all 25 Provider envelopes and schema fingerprints; the exact files `provider_manifest_30c944f.json`, `tool_outcomes_30c944f.json`, `journal_sequences_30c944f.json`, `baseline_aaecf5d.json`, and `baseline_golden.json`; exact pre-executor sync/SSE status/code/message bodies; replay 409/503 mappings; call-count baselines for new turn/approve/modify/reject/final replay/chained replay/delivery recovery; and the current dependency closure manifest.

Create one canonical `authority_manifest_v1.json` as the sole frozen source for all 25 tools. Every entry contains exact `name`, ordinal, `kind`, `confirmation_policy`, required capability, Binding Contract, and ordered resolver metadata (`resolver_id`, `entity_kind`, `arg_path`, `presence`, `identity_type`). Create a separate `policy_fingerprints_v1.json` that pins the independently reviewed expected `sha256:` digests for the capability profile and the complete Binding policy input; it is not generated or updated by tests. Tasks 3 and 4 must compute fingerprints and validate the production Catalog against these files; do not create a second matrix with overlapping fields.

- [ ] **Step 3: Run the test and verify RED**

```powershell
uv run pytest tests/tool_authority/test_baseline_golden.py -q
```

Expected: fail because `baseline_2427fa6.json` is absent.

- [ ] **Step 4: Manually create the canonical synthetic asset**

Use only stable synthetic facts. Do not include SQLite bytes, user text, prompts, entity IDs, paths, timestamps, secrets, exception strings, or tracebacks. Do not add a script, environment flag, CLI option, fixture rewrite helper, or acceptance switch.

- [ ] **Step 5: Prove the characterization and old goldens are GREEN**

```powershell
uv run pytest tests/tool_authority/test_baseline_golden.py tests/tool_pipeline/test_golden_assets.py tests/agent_loop/test_baseline_golden.py tests/pilot_runtime/test_baseline_golden.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit the characterization**

```powershell
git add tests/tool_authority tests/fixtures/tool_authority
git commit -m "test: AI 固化作用域授权基线行为"
```

### Task 2: Define opaque Authority contracts and object-identity registries

**Files:**
- Create: `src/offerpilot/ai/tool_authority/__init__.py`
- Create: `src/offerpilot/ai/tool_authority/contracts.py`
- Create: `src/offerpilot/ai/tool_authority/composition.py`
- Create: `tests/tool_authority/test_contracts.py`
- Create: `tests/tool_authority/test_serialization.py`
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`

- [ ] **Step 1: Write failing contract tests**

Cover exact type separation and phase/call identity rejection for:

```text
SegmentExecutionAuthority
ApprovalExecutionAuthority
ProviderSurfaceBuildIdentity
ProviderInvocationIdentity
NewTurnPrepareCallIdentity
ReadExecutionCallIdentity
TypedPendingCallIdentity
ApprovedWritePrepareCallIdentity
ApprovedWriteExecuteCallIdentity
PendingAuthorityClaim
ExecutionClaim
TrustedLedgerOmittedTokenProof
ApplicationScopeConstraint
BindingTargetResolution
```

Prove that same-field reconstructed dataclasses, `copy`, `deepcopy`, `pickle`, `asdict`, `replace`, `to_json`, and checkpoint serialization fail before Catalog/Repository/Provider/executor access.

- [ ] **Step 2: Run contract tests and verify RED**

```powershell
uv run pytest tests/tool_authority/test_contracts.py tests/tool_authority/test_serialization.py -q
```

Expected: import failure because `offerpilot.ai.tool_authority` does not exist.

- [ ] **Step 3: Implement leaf contracts without repository/runtime imports**

Use closed shapes equivalent to:

```python
@dataclass(frozen=True, slots=True, repr=False)
class BindingTargetResolution(TransientToolRuntimeValue):
    entity_kind: Literal["application", "resume"]
    state: Literal["resolved", "omitted", "detached", "unavailable"]
    identity: int | None
    authority_instance_token: AuthorityInstanceToken


@dataclass(frozen=True, slots=True, repr=False)
class ApplicationScopeConstraint(TransientToolRuntimeValue):
    entity_kind: Literal["application"]
    mode: Literal["unrestricted", "restricted"]
    allowed_identities: frozenset[int]
    authority_instance_token: AuthorityInstanceToken


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionClaim(TransientToolRuntimeValue):
    operation_id: str
    conversation_id: int
    pending_identity: PendingInstanceToken
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    effective_args_digest: str
    approval_authority_instance_token: AuthorityInstanceToken
    prepared_instance_token: PreparedInstanceToken
    execution_claim_instance_token: ExecutionClaimInstanceToken
```

Only exact Python `int` values in `1..9223372036854775807` are valid identities; reject `bool`, float, string, zero, negative, and overflow. Keep this leaf module free of SQLAlchemy, repositories, Pilot Runtime, and Context Projector imports.

- [ ] **Step 4: Implement registry-backed factories and lifecycles**

Registries must bind exact authority/prepared/pending/transaction objects and enforce:

```text
Pending claim: issued -> in_flight -> consumed | revoked
Execution claim: issued -> in_flight -> consumed | revoked
Omitted-token proof: issued -> in_flight -> consumed | revoked
```

Every exception, rollback, cancellation, and normal exit revokes or consumes the object in `finally`. Each execution-scope registry holds strong references while the scope is active, then removes tokens, claims, authorities, and Prepared objects on revoke/consume/exit. Do not use weak references that can invalidate a live authority early, and do not retain completed process-wide history. Never log raw tokens.

- [ ] **Step 5: Run tests and verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_contracts.py tests/tool_authority/test_serialization.py -q
uv run mypy src/offerpilot/ai/tool_authority src/offerpilot/ai/tool_runtime/contracts.py
```

Expected: all pass.

- [ ] **Step 6: Commit the contracts**

```powershell
git add src/offerpilot/ai/tool_authority src/offerpilot/ai/tool_runtime/contracts.py tests/tool_authority
git commit -m "feat: AI 建立密封作用域授权契约"
```

### Task 3: Freeze Capability Profile, Binding policy, and six versioned policy identifiers

**Files:**
- Create: `src/offerpilot/ai/tool_authority/policy.py`
- Create: `src/offerpilot/ai/tool_authority/fingerprint.py`
- Create: `tests/tool_authority/test_policy.py`
- Create: `tests/tool_authority/test_fingerprint.py`
- Create: `tests/fixtures/tool_authority/capability_profile_agent_typed_v1.json`
- Read: `tests/fixtures/tool_authority/authority_manifest_v1.json`
- Read: `tests/fixtures/tool_authority/policy_fingerprints_v1.json`

- [ ] **Step 1: Write RED tests for the exact profile and versions**

Pin these exact values:

```python
PROFILE_ID = "agent_typed_v1"
CAPABILITY_POLICY_VERSION = "capability-policy-v1"
BINDING_POLICY_VERSION = "binding-policy-v1"
BINDING_AGGREGATION_VERSION = "binding-aggregation-v1"
APPLICATION_COLLECTION_SCOPE_VERSION = "application-collection-scope-v1"
SCOPE_DENIAL_VERSION = "scope-denial-v1"
DEPENDENCY_POLICY_VERSION = "dependency-policy-v1"

EXPECTED_CAPABILITIES = (
    "applications.read", "applications.write",
    "application_events.read", "application_events.write",
    "notes.read", "notes.write",
    "offers.read", "offers.write",
    "resumes.read", "resumes.write",
    "jd_analyses.read",
)
```

Test empty/read-only/write-only/missing-one/unknown profiles, enum additions, version drift, canonical SHA fingerprints, HMAC domain separation, constant-time comparison, and `BaseException` propagation.

The Binding fingerprint canonical input is exactly:

```text
schema = binding-policy-v1
aggregation_rule_version = binding-aggregation-v1
collection_scope_rule_version = application-collection-scope-v1
public_denial_rule_version = scope-denial-v1
tools = the complete ordered authority_manifest_v1 entries
```

The runtime-computed digest must equal the independently pinned `sha256:` value in `policy_fingerprints_v1.json`. Tests independently mutate each of the three rule versions, kind, confirmation policy, capability, contract, and every resolver metadata field while holding the expected digest fixed; every mutation must fail startup validation.

- [ ] **Step 2: Write RED Binding truth-table tests**

Cover all `none`, `enforce_if_bound`, `scoped_collection`, `optional_target`, and `non_application_only` combinations, including resolved/omitted/detached/unavailable, same/different identity, optional explicit unavailable, and workspace/global/mode unbound behavior.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_policy.py tests/tool_authority/test_fingerprint.py -q
```

Expected: fail because policy/fingerprint functions are absent.

- [ ] **Step 4: Implement pure policy and canonical fingerprints**

The scope HMAC input must include exactly: `conversation_id`, canonical `context_type`, canonical `context_ref`, canonical `mode`, monotonic `scope_revision`, capability profile ID, `capability_policy_version`, `binding_policy_version`, `capability_profile_fingerprint`, and the public `binding_policy_fingerprint` computed from `authority_manifest_v1.json`, under:

```text
write-operation-authorization-scope-v1\0
```

Do not include `dependency_policy_version`; dependency changes must not stale an existing Pending. Also exclude Provider page context, entity body, args, credentials, Journal data, timestamps, and ORM values. Test every field independently, canonical string/integer Application reference equivalence, case-sensitive mode variants, domain separation, and fixed-length constant-time comparison.

- [ ] **Step 5: Verify GREEN and immutable assets**

```powershell
uv run pytest tests/tool_authority/test_policy.py tests/tool_authority/test_fingerprint.py tests/tool_authority/test_baseline_golden.py -q
```

Expected: all pass; changing a version or manifest item without changing its pinned fixture fails.

- [ ] **Step 6: Commit the policy**

```powershell
git add src/offerpilot/ai/tool_authority/policy.py src/offerpilot/ai/tool_authority/fingerprint.py tests/tool_authority tests/fixtures/tool_authority
git commit -m "feat: AI 冻结能力与绑定授权策略"
```

### Task 4: Encode the exact 25-Tool authority matrix in ToolSpec and Catalog

**Files:**
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`
- Modify: `src/offerpilot/ai/tool_runtime/catalog.py`
- Modify: `src/offerpilot/ai/tool_specs/catalog.py`
- Modify: `src/offerpilot/ai/tool_specs/applications.py`
- Modify: `src/offerpilot/ai/tool_specs/application_events.py`
- Modify: `src/offerpilot/ai/tool_specs/notes.py`
- Modify: `src/offerpilot/ai/tool_specs/offers.py`
- Modify: `src/offerpilot/ai/tool_specs/resumes.py`
- Modify: `src/offerpilot/ai/tool_specs/jd_analyses.py`
- Create: `tests/tool_authority/test_matrix.py`
- Read: `tests/fixtures/tool_authority/authority_manifest_v1.json`
- Modify: `tests/tool_pipeline/test_catalog.py`

- [ ] **Step 1: Write a failing exact-matrix golden test**

The 25 entries, in `MODEL_TOOL_NAMES` order, must be:

```text
list_applications read none applications.read scoped_collection(application)
get_application read none applications.read enforce_if_bound(application) application_identity_arg:id:required
create_application write required applications.write non_application_only
update_application_status write required applications.write enforce_if_bound(application) application_identity_arg:id:required
list_application_events read none application_events.read scoped_collection(application) application_identity_arg:application_id:optional
get_application_event read none application_events.read enforce_if_bound(application) application_event_parent:id:required
create_application_event write required application_events.write enforce_if_bound(application) application_identity_arg:application_id:required
update_application_event write required application_events.write enforce_if_bound(application) application_event_parent:id:required,application_identity_arg:application_id:required
delete_application_event write required application_events.write enforce_if_bound(application) application_event_parent:id:required
list_notes read none notes.read scoped_collection(application) application_identity_arg:application_id:optional
add_note write required notes.write optional_target(application) application_identity_arg:application_id:optional
update_note write required notes.write enforce_if_bound(application) note_application_parent:id:required,application_identity_arg:application_id:optional
delete_note write required notes.write enforce_if_bound(application) note_application_parent:id:required
list_offers read none offers.read scoped_collection(application)
get_offer read none offers.read enforce_if_bound(application) offer_application_parent:id:required
compare_offers read none offers.read non_application_only
update_offer write required offers.write enforce_if_bound(application) offer_application_parent:id:required
save_offer_assessment write required offers.write enforce_if_bound(application) offer_application_parent:id:required
list_resumes read none resumes.read none
get_resume read none resumes.read enforce_if_bound(resume) resume_identity_arg:id:required
resume_update_career_intent write required resumes.write enforce_if_bound(resume) resume_identity_arg:id:required
resume_rewrite_highlight write required resumes.write enforce_if_bound(resume) resume_identity_arg:id:required
list_resume_matches read none resumes.read enforce_if_bound(resume) resume_identity_arg:resume_id:required
list_jd_analyses read none jd_analyses.read scoped_collection(application) application_identity_arg:application_id:optional
get_jd_analysis read none jd_analyses.read enforce_if_bound(application) jd_analysis_application_parent:id:required
```

The columns are `name kind confirmation_policy capability binding resolvers`. Every resolver has `identity_type=positive_int64`. `update_application_event` resolver order is exact. The three Legacy names are absent. Catalog startup compares all fields with the single `authority_manifest_v1.json`; policy fingerprinting reads that same canonical bytestring.

- [ ] **Step 2: Run matrix tests and verify RED**

```powershell
uv run pytest tests/tool_authority/test_matrix.py tests/tool_pipeline/test_catalog.py -q
```

Expected: fail because `ToolSpec` has no typed Binding Contract/resolver metadata.

- [ ] **Step 3: Add closed metadata types to ToolSpec**

Replace raw resolver callables with a stable descriptor:

```python
@dataclass(frozen=True, slots=True)
class BindingResolverSpec(Generic[ArgsT]):
    resolver_id: BindingResolverId
    entity_kind: Literal["application", "resume"]
    arg_path: str
    presence: Literal["required", "optional"]
    identity_type: Literal["positive_int64"]
    resolve: Callable[[ArgsT, ToolExecutionContext], BindingTargetResolution] = field(
        repr=False, compare=False
    )
```

Catalog initialization rejects mixed kinds, illegal resolver count, wrong presence, unknown resolver IDs, dynamic/reflective metadata, profile/manifest drift, and Provider contract changes.

- [ ] **Step 4: Update all six domain ToolSpec modules**

Keep every Provider payload untouched. Parent resolvers read only primitive ID/parent ID, never entity body. Explicit optional args that cannot resolve return `unavailable`, not `omitted`.

- [ ] **Step 5: Verify matrix and Provider compatibility GREEN**

```powershell
uv run pytest tests/tool_authority/test_matrix.py tests/tool_pipeline/test_catalog.py tests/tool_pipeline/test_golden_assets.py -q
```

Expected: all pass; the existing Provider manifest remains unchanged.

- [ ] **Step 6: Commit the ToolSpec matrix**

```powershell
git add src/offerpilot/ai/tool_runtime src/offerpilot/ai/tool_specs tests/tool_authority tests/tool_pipeline/test_catalog.py tests/fixtures/tool_authority
git commit -m "feat: AI 固化二十五项工具授权矩阵"
```

### Task 5: Add migration 0028 and atomic Conversation scope mutation

**Files:**
- Modify: `src/offerpilot/models.py`
- Modify: `src/offerpilot/db.py`
- Modify: `src/offerpilot/repositories/chat.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/tool_authority/test_migration_0028.py`
- Create: `tests/tool_authority/test_scope_mutation.py`
- Modify: `tests/test_chat_repository.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Confirm 0028 is free**

```powershell
rg -n '0028_' src tests
```

Expected: no migration definition or recorded schema version. If a real migration is present at execution time, use the next continuous free number everywhere; never rewrite an existing migration.

- [ ] **Step 2: Write RED migration and trigger tests**

Cover fresh database, upgrade from 0027, repeat init, backup/restore, and direct SQL. Assert:

```text
conversations.scope_revision INTEGER NOT NULL DEFAULT 0
write_operations.authorization_scope_fingerprint nullable
legacy mode NULL/'' -> general; other unknown mode remains unchanged
new Conversation scope revision exactly 0
raw context_type/context_ref/mode change requires exactly OLD + 1
unchanged scope requires unchanged revision
new Typed primary proposal requires the prefix `hmac-sha256:` followed by exactly 64 lowercase hexadecimal characters
fingerprint and immutable authority identity columns cannot change
old typed primary proposed/null can only remain proposed or become rejected
conversation_id cannot rebind non-null IDs or null -> non-null
```

Direct-SQL tests must name every immutable Write Operation authority column: `operation_role`, `adapter_kind`, `tool_name`, `tool_call_id`, `fingerprint_key_id`, `proposal_fingerprint`, `confirmation_token_fingerprint`, and `parent_operation_id`. Separately cover valid/invalid `authorization_scope_fingerprint`, nullable Legacy/terminal/compensation rows, proposed→committed/failed/rejected status transitions, FK/direct `conversation_id=NULL`, non-null rebind rejection, null→non-null rejection, `scope_revision` overflow at `9223372036854775807`, INSERT revision≠0, mode INSERT/update triggers, and raw scope-change/no-change trigger semantics.

- [ ] **Step 3: Write RED scope mutation tests**

Test the complete request fields-set table for missing/null/empty/value `context_type`, `context_ref`, and `mode`, plus Application int/string equivalence, invalid coercion, unknown context type, A→B→A revision increments, mixed title/pin/archive+scope atomicity, and concurrent CAS loser with no partial mutation. Pin mode lexical rejection for overlength, surrogate, control character, and leading/trailing whitespace. Do not casefold: `"GENERAL"` and other lexically valid case variants are accepted as distinct unknown modes, remain distinct in persistence/Authority/HMAC, and increment revision when changed from `"general"`. Existing Start Turn scope fields are ignored completely: they perform no scope mutation/visibility lookup and Authority uses the persisted Conversation. New Conversation with invalid or invisible Application scope leaves Conversation/User Message/Pending/Provider/Tool counts at 0.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_migration_0028.py tests/tool_authority/test_scope_mutation.py tests/test_chat_repository.py -q
```

Expected: fail because both columns, triggers, and atomic ports are absent.

- [ ] **Step 5: Implement additive schema and atomic ports**

Expose only:

```python
def create_conversation_with_scope(
    self, title: str, mutation: ConversationScopeMutationSnapshot, *, title_source: str
) -> Conversation: ...

def patch_conversation_with_scope(
    self,
    conversation_id: int,
    values: Mapping[str, object],
    mutation: ConversationScopeMutationSnapshot | None,
    *,
    expected_scope_revision: int,
) -> Conversation | None: ...
```

Generic `create_conversation()` and `update_conversation()` must reject scope keys. API presence handling must happen before coercion and call one atomic repository method.

Persisted legacy workspace/global/mode scopes with a non-empty historical `context_ref` canonicalize by safely ignoring that ref. Persisted unknown/custom context types and invalid legacy mode values fail closed in Source/Authority construction without preventing title/pin/archive updates, reject, or terminal replay.

- [ ] **Step 6: Delete Typed read-time lazy writes**

Remove Operation creation from `get_conversation()`, `list_conversations()`, `get_pending_action()`, and every list/get path. Keep a separate explicit Legacy-only persistence route. Add an AST/spy assertion that read methods execute no INSERT/UPDATE.

- [ ] **Step 7: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_migration_0028.py tests/tool_authority/test_scope_mutation.py tests/test_chat_repository.py tests/test_chat_api.py -q
```

Expected: all pass with public Conversation JSON unchanged and no `scope_revision` field.

- [ ] **Step 8: Commit schema and scope mutation**

```powershell
git add src/offerpilot/models.py src/offerpilot/db.py src/offerpilot/repositories/chat.py src/offerpilot/api.py tests/tool_authority tests/test_chat_repository.py tests/test_chat_api.py
git commit -m "feat: AI 增加会话作用域版本与原子变更"
```

### Task 6: Unify active-Application visibility with Context Source

**Files:**
- Create: `src/offerpilot/ai/tool_authority/visibility.py`
- Modify: `src/offerpilot/context_projector/loader.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/tool_authority/test_visibility.py`
- Modify: `tests/test_context_projector.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED adapter parity tests**

For raw sqlite and SQLAlchemy Session adapters, assert identical primitive results for active/deleted/missing Application. Both adapters must call one canonical SQL-definition builder, decoder, and cardinality check; no duplicated query strings or divergent result parsing. Reject duplicate/ambiguous results and ordinary SQL/codec errors as internal failures; propagate `BaseException`.

- [ ] **Step 2: Write RED source snapshot tests**

Prove canonical Conversation scope, Application visibility, and body are read from one snapshot. Ensure `_load_chat_source_messages()` filters `applications.deleted_at IS NULL`. Test delete between snapshots cannot yield a valid Authority with stale body. Persisted unknown/custom scope and invalid legacy mode fail closed; historical workspace/global/mode non-empty refs are ignored without becoming bindings.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_visibility.py tests/test_context_projector.py tests/test_chat_api.py -q
```

Expected: targeted tests fail because visibility predicates/adapters are not shared.

- [ ] **Step 4: Implement the single query port**

The only public result is a bounded primitive visibility decision. Do not return ORM, entity body, exception text, or target IDs in diagnostics. Both adapters must execute the same semantic predicate:

```sql
SELECT id
FROM applications
WHERE id = :application_id AND deleted_at IS NULL
LIMIT 2
```

- [ ] **Step 5: Integrate Source Loader and verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_visibility.py tests/test_context_projector.py tests/test_chat_api.py -q
```

Expected: all pass; Source failure remains the existing safe 503 mapping.

- [ ] **Step 6: Commit visibility**

```powershell
git add src/offerpilot/ai/tool_authority/visibility.py src/offerpilot/context_projector/loader.py src/offerpilot/api.py tests/tool_authority/test_visibility.py tests/test_context_projector.py tests/test_chat_api.py
git commit -m "feat: AI 统一会话与应用可见性快照"
```

### Task 7: Implement authority-bound scoped collection and point-read repositories

**Files:**
- Modify: `src/offerpilot/repositories/applications.py`
- Modify: `src/offerpilot/repositories/application_events.py`
- Modify: `src/offerpilot/repositories/notes.py`
- Modify: `src/offerpilot/repositories/offers.py`
- Modify: `src/offerpilot/repositories/jd.py`
- Modify: `src/offerpilot/repositories/session_binding.py`
- Modify: `src/offerpilot/ai/tool_specs/applications.py`
- Modify: `src/offerpilot/ai/tool_specs/application_events.py`
- Modify: `src/offerpilot/ai/tool_specs/notes.py`
- Modify: `src/offerpilot/ai/tool_specs/offers.py`
- Modify: `src/offerpilot/ai/tool_specs/jd_analyses.py`
- Create: `tests/tool_authority/test_scoped_reads.py`
- Modify: `tests/test_applications_repository.py`
- Create: `tests/test_application_events_repository.py`
- Create: `tests/test_jd_analyses_repository.py`
- Modify: `tests/tool_pipeline/test_applications.py`
- Modify: `tests/tool_pipeline/test_application_events.py`
- Modify: `tests/tool_pipeline/test_notes.py`
- Modify: `tests/tool_pipeline/test_offers.py`
- Modify: `tests/tool_pipeline/test_jd_analyses.py`

- [ ] **Step 1: Write RED guard and caller-session tests**

Assert every scoped method requires the exact `ApplicationScopeConstraint` object registered to the bound repository/context. Same values with a copied/fabricated token, another Authority, another Context, wrong kind, restricted empty/multiple set, or an unrestricted constraint with any identity fail before SQL. The only legal unrestricted constraint has an exactly empty identity set. Assert `JDAnalysesRepository.bind(session)` exists and no repository checks out another Session.

- [ ] **Step 2: Write RED collection tests**

Cover exact methods:

```text
list_applications_scoped
list_application_events_scoped
list_notes_scoped
list_offers_scoped
list_jd_analyses_scoped
```

For restricted mode, one SQL statement must contain an active-parent sentinel/CTE and distinguish parent-visible empty `[]` from missing/deleted parent denial. Filters still work. Detached Note/Offer/JD rows are invisible. Unrestricted mode preserves baseline detached/empty behavior.

- [ ] **Step 3: Write RED point-read tests**

Cover `get_application_scoped`, `get_application_event_scoped`, `get_note_scoped`, `get_offer_scoped`, and `get_jd_analysis_scoped`. Restricted SQL simultaneously constrains target, allowed singleton parent, and active parent. Cross-Application/detached/missing rows yield the same safe denial without reading body.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_scoped_reads.py tests/test_applications_repository.py tests/test_application_events_repository.py tests/test_jd_analyses_repository.py -q
```

Expected: fail because scoped ports and JD session binding are absent.

- [ ] **Step 5: Implement guarded SQL without fallback**

Use caller-owned Sessions and SQL predicates, not Python post-filtering. A guard mismatch raises before `Session.execute`. Never catch a scoped denial and call the legacy unscoped method. Change the five domain read executors to call only the scoped collection/point ports with the exact Context-bound constraint.

- [ ] **Step 6: Verify GREEN and query counts**

```powershell
uv run pytest tests/tool_authority/test_scoped_reads.py tests/test_applications_repository.py tests/test_application_events_repository.py tests/test_jd_analyses_repository.py -q
```

Expected: all pass; each final collection/point query count is exactly 1.

- [ ] **Step 7: Commit scoped reads**

```powershell
git add src/offerpilot/repositories src/offerpilot/ai/tool_specs tests/tool_authority/test_scoped_reads.py tests/test_applications_repository.py tests/test_application_events_repository.py tests/test_jd_analyses_repository.py tests/tool_pipeline
git commit -m "feat: AI 强制应用作用域只读查询"
```

### Task 8: Implement the nine scoped Application-owned mutation ports

**Files:**
- Modify: `src/offerpilot/repositories/applications.py`
- Modify: `src/offerpilot/repositories/application_events.py`
- Modify: `src/offerpilot/repositories/notes.py`
- Modify: `src/offerpilot/repositories/offers.py`
- Modify: `src/offerpilot/ai/tool_specs/applications.py`
- Modify: `src/offerpilot/ai/tool_specs/application_events.py`
- Modify: `src/offerpilot/ai/tool_specs/notes.py`
- Modify: `src/offerpilot/ai/tool_specs/offers.py`
- Create: `tests/tool_authority/test_scoped_writes.py`
- Modify: `tests/tool_pipeline/test_applications.py`
- Modify: `tests/tool_pipeline/test_application_events.py`
- Modify: `tests/tool_pipeline/test_notes.py`
- Modify: `tests/tool_pipeline/test_offers.py`

- [ ] **Step 1: Write RED tests for the exact nine ports**

```text
update_application_status_scoped
create_application_event_scoped
update_application_event_scoped
delete_application_event_scoped
create_note_scoped
update_note_scoped
delete_note_scoped
update_offer_scoped
save_offer_assessment_scoped
```

Test exact token/constraint guard, rowcount/RETURNING uniqueness, active parent, existing mutable/revision predicates, cross-Application write, reparent/delete after prepare, and no unscoped ORM fallback.

- [ ] **Step 2: Write standalone add_note RED tests**

In Application scope, omitted `application_id` still inserts `NULL`, but the current Application must be active in the same locked Session. An explicit `application_id` must match. Do not inject the current ID into args/digest/Pending/ToolMessage.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_scoped_writes.py tests/tool_pipeline/test_applications.py tests/tool_pipeline/test_application_events.py tests/tool_pipeline/test_notes.py tests/tool_pipeline/test_offers.py -q
```

Expected: fail because current executors use unscoped mutations.

- [ ] **Step 4: Implement guarded mutations**

Restricted update/delete uses one guarded `UPDATE/DELETE ... RETURNING` with target, singleton parent, active parent, and mutable predicates. Restricted create uses `INSERT ... SELECT` from the active allowed Application. Undo snapshots and render data come from scoped SELECT/RETURNING in the same locked Session. Change the four domain write-spec modules so all nine Application-owned executors call only these scoped ports; there is no catch-and-fallback to the old repository methods.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_scoped_writes.py tests/tool_pipeline/test_applications.py tests/tool_pipeline/test_application_events.py tests/tool_pipeline/test_notes.py tests/tool_pipeline/test_offers.py -q
```

Expected: all pass; unauthorized paths have side-effect count 0.

- [ ] **Step 6: Commit scoped writes**

```powershell
git add src/offerpilot/repositories src/offerpilot/ai/tool_specs tests/tool_authority/test_scoped_writes.py tests/tool_pipeline
git commit -m "feat: AI 强制应用作用域写入约束"
```

### Task 9: Make ToolExecutionContext authority-bound and enforce prepare/read Pipeline phases

**Files:**
- Modify: `src/offerpilot/ai/tool_runtime/context.py`
- Modify: `src/offerpilot/ai/tool_runtime/pipeline.py`
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`
- Modify: `tests/tool_pipeline/domain_harness.py`
- Modify: `tests/tool_pipeline/test_context.py`
- Modify: `tests/tool_pipeline/test_pipeline.py`
- Create: `tests/tool_authority/test_read_uow.py`

- [ ] **Step 1: Write RED phase-order tests**

Pin:

```text
authority call-identity prelookup
catalog lookup
authority/spec postlookup
parse
schema
decode
capability
pre-resolver scope policy
binding resolve
binding policy
preflight
prepared/confirmation/failure
```

Unknown tool has Catalog lookup only. Missing capability has resolver/Repository/preflight/executor 0. `non_application_only` rejects before resolver. Ordinary resolver exception maps safely; `BaseException` propagates.

- [ ] **Step 2: Write RED public-equivalence tests**

For a real cross-Application ID, detached row, and missing ID, assert exactly identical `permission_denied/scope_access_denied`, compatibility ToolMessage, HTTP/SSE shape, Journal sequence, and downstream calls. `not_found` is allowed only after current scope authorizes the target.

- [ ] **Step 3: Write RED read-UoW race tests**

Require one caller-owned Session:

```text
binding recheck snapshot
rollback
tool.started
fresh final scoped SQL as first target statement
tool.completed/tool.failed
```

Use barriers for reparent/delete after rollback and before final SQL. The final query denies. A writer overlapping the final SQL follows that statement snapshot. Race-after-started yields executor/query 1, `scope_access_denied`, Journal `tool.failed(tool_error)`, and normal read-batch continuation.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/tool_pipeline/test_context.py tests/tool_pipeline/test_pipeline.py tests/tool_authority/test_read_uow.py -q
```

Expected: fail because context exposes raw capabilities/bindings and read UoW does not exist.

- [ ] **Step 5: Implement the authority-bound context**

`ToolExecutionContext` must contain the exact Authority object, repositories, recorder, and execution port. Remove public `capabilities` and `current_bindings`. `bind(session)` binds all six repositories and the exact registered constraint.

- [ ] **Step 6: Implement prepare and read execution**

Store only safe `BindingAudit`, exact Authority/Prepared instance tokens, and canonical digests in `PreparedToolCall`; never target identity/ORM/resolution. Preserve baseline compatibility messages and read/read ToolCall ordering.

- [ ] **Step 7: Verify GREEN**

```powershell
uv run pytest tests/tool_pipeline/test_context.py tests/tool_pipeline/test_pipeline.py tests/tool_authority/test_read_uow.py tests/tool_pipeline/test_journal.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit Pipeline read enforcement**

```powershell
git add src/offerpilot/ai/tool_runtime tests/tool_pipeline tests/tool_authority/test_read_uow.py
git commit -m "feat: AI 强制工具准备与只读授权阶段"
```

### Task 10: Replace ExecutionAuthorization with a sealed one-shot ExecutionClaim

**Files:**
- Modify: `src/offerpilot/ai/tool_runtime/contracts.py`
- Modify: `src/offerpilot/ai/tool_runtime/pipeline.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `tests/tool_pipeline/test_confirmation_claim_schema.py`
- Modify: `tests/test_write_operations.py`
- Create: `tests/tool_authority/test_execution_claim.py`

- [ ] **Step 1: Write RED claim-forgery tests**

Reject same-field self-created/copy claims, another Approval Authority, another Prepared object, another transaction, consumed/revoked claim, changed/replaced `typed_args`, and missing operation executor. All executor counts are 0.

- [ ] **Step 2: Write RED exactly-once local execution tests**

The Ledger claim port is the only issuer. A legal claim is consumed once. Executor ordinary exception still counts 1. Journal/renderer/transport/delivery/continuation failure never reuses the claim or reruns executor. Cancellation revokes in `finally` and propagates.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_execution_claim.py tests/tool_pipeline/test_confirmation_claim_schema.py tests/test_write_operations.py -q
```

Expected: fail because `ExecutionAuthorization` remains forgeable and direct write fallback exists.

- [ ] **Step 4: Implement the locked claim issuer and final match**

Immediately before synchronous dispatch, constant-time compare canonical typed args digest across Approval Authority, locked effective args, Prepared, and Claim; validate exact registry object identity; do not `await` between final check and dispatch.

- [ ] **Step 5: Delete old write paths**

Remove `ExecutionAuthorization`, any direct write execution without `operation_executor`, and any test/runtime helper that signs a claim from fields alone.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_execution_claim.py tests/tool_pipeline/test_confirmation_claim_schema.py tests/test_write_operations.py -q
rg -n "ExecutionAuthorization|authorization_match" src tests
```

Expected: tests pass; source search has no production `ExecutionAuthorization` or old field-only match path.

- [ ] **Step 7: Commit sealed write execution**

```powershell
git add src/offerpilot/ai/tool_runtime src/offerpilot/ai/write_operations.py src/offerpilot/pilot_runtime/continuation.py tests/tool_authority/test_execution_claim.py tests/tool_pipeline/test_confirmation_claim_schema.py tests/test_write_operations.py
git commit -m "feat: AI 使用一次性执行声明保护写入"
```

### Task 11: Bind every new Typed Pending proposal to trusted scope HMAC

**Files:**
- Modify: `src/offerpilot/repositories/chat.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/pilot_runtime/persistence.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Create: `tests/tool_authority/test_pending_claim.py`
- Modify: `tests/pilot_runtime/test_persistence.py`
- Modify: `tests/test_chat_repository.py`

- [ ] **Step 1: Write RED Typed Pending claim tests**

Cover New Turn first Pending, duplicate, replacement, approve/modify chained Pending, and delivery transaction. Explicitly spy every production entry: `set_pending_action`, `persist_pending_action`, `replace_pending_confirmation`, `persist_confirmation_continuation`, and the delivery transaction’s chained Pending replacement. Each Typed route must delegate to `persist_typed_pending(..., PendingAuthorityClaim)` and no route may infer Authority from Conversation/Pending fields. Missing/forged/cross-Conversation/cross-Segment/wrong operation/tool/args/Prepared/Pending/consumed/revoked claims must fail before writes.

- [ ] **Step 2: Write RED null-fingerprint upgrade tests**

Old Typed proposed/null is never backfilled. Approve/modify returns stale-unbound before Source/Catalog/decode; reject succeeds if Conversation identity exists. `conversation_id IS NULL` returns operation-unavailable for all confirmation decisions. Terminal/Legacy/compensation null rows retain replay behavior.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_pending_claim.py tests/pilot_runtime/test_persistence.py tests/test_chat_repository.py -q
```

Expected: fail because generic `_create_operation_for_pending` can infer authority and read paths can backfill.

- [ ] **Step 4: Implement the exclusive Typed proposal port**

Production Typed creation must call:

```python
persist_typed_pending(
    session,
    conversation_id,
    pending,
    pending_authority_claim,
)
```

Within one `BEGIN IMMEDIATE`: reload Conversation, canonicalize scope/revision, verify active parent, verify exact claim identities/digest, compute HMAC using the startup-loaded Ledger key, then atomically write Pending and Typed primary Operation. Rollback revokes the claim.

- [ ] **Step 5: Isolate Legacy persistence**

Only the exact three names may call `persist_legacy_pending()`. Compensation creates no Pending. Delete the generic Typed `_create_operation_for_pending()` route and any lazy backfill.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_pending_claim.py tests/pilot_runtime/test_persistence.py tests/test_chat_repository.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit scope-bound proposals**

```powershell
git add src/offerpilot/repositories/chat.py src/offerpilot/ai/write_operations.py src/offerpilot/pilot_runtime/persistence.py src/offerpilot/pilot_runtime/continuation.py tests/tool_authority/test_pending_claim.py tests/pilot_runtime/test_persistence.py tests/test_chat_repository.py
git commit -m "feat: AI 将待确认写入绑定可信作用域"
```

### Task 12: Add LedgerOperationPreheader and parameter-free reject proof

**Files:**
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Create: `tests/tool_authority/test_ledger_preheader.py`
- Create: `tests/tool_authority/test_reject_privacy.py`
- Modify: `tests/agent_loop/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`

- [ ] **Step 1: Write RED operation-ID bootstrap truth-table tests**

Explicit ID loads Operation first. Omitted ID may read only Conversation `id + pending_operation_id + pending_tool_call_id + pending_tool_name + pending_confirmation_claim_id`, then load that Operation. It must not select args/human text, scan Ledger, guess latest operation, or write.

- [ ] **Step 2: Write RED reject tests**

Provided token compares only canonical fingerprint. Legal omitted-token plain reject uses sealed `TrustedLedgerOmittedTokenProof`. Malformed/65,537+ byte Pending args, target deletion, Scope change, and policy change do not parse args or access Source/Catalog/Schema/Binding/Repository/preflight/executor/Provider. Feedback without token remains existing 422. Owning Conversation deletion is operation-unavailable.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_ledger_preheader.py tests/tool_authority/test_reject_privacy.py tests/agent_loop/test_confirmation.py tests/pilot_runtime/test_confirmation.py -q
```

Expected: targeted tests fail because reject still shares the token/session decode path.

- [ ] **Step 4: Implement preheader and proof lifecycle**

Validate Operation original object identity and exact pointer/CAS tuple. Reject CAS must match proposed status, Conversation ID, operation/tool-call/tool-name pointers, and expected unclaimed identity, then terminalize and clear only the exact pointer.

- [ ] **Step 5: Delete reject decode paths**

Remove reject calls to `_new_session()`, `_token()`, `_confirmation_token()`, `json.loads(pending.args)`, Typed Catalog, and target repositories.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_ledger_preheader.py tests/tool_authority/test_reject_privacy.py tests/agent_loop/test_confirmation.py tests/pilot_runtime/test_confirmation.py -q
```

Expected: all pass; reject Provider/Tool/executor/args-decoder counts are 0.

- [ ] **Step 7: Commit Ledger-first reject**

```powershell
git add src/offerpilot/ai/write_operations.py src/offerpilot/pilot_runtime/continuation.py src/offerpilot/pilot_runtime/service.py tests/tool_authority tests/agent_loop/test_confirmation.py tests/pilot_runtime/test_confirmation.py
git commit -m "feat: AI 实现账本优先无参数拒绝"
```

### Task 13: Revalidate approve/modify inside the locked Ledger transaction

**Files:**
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/ai/tool_runtime/pipeline.py`
- Modify: `src/offerpilot/ai/tool_authority/composition.py`
- Create: `tests/tool_authority/test_approval_transaction.py`
- Create: `tests/tool_authority/test_approval_authority_resolver.py`
- Modify: `tests/test_write_operations.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`

- [ ] **Step 1: Write RED convergence-table tests**

Pin each approved-design row:

```text
outside prepare ToolFailure -> proposed/Pending unchanged, origin ToolMessage 0, Provider 0
outside resolver exception -> safe retryable, unchanged
locked scope/HMAC/identity/parent/Binding stale -> rollback, stale_pending_action 409
locked mapped terminal domain failure -> claim then terminal failed, executor 0, no tool.started
locked infrastructure/internal -> rollback, unchanged
claim/CAS loser -> terminal replay or proposed conflict, executor 0
executor return/exception -> exactly one call and authoritative terminal
```

Also pin the namespace/projection table: New Turn `missing_capability` and `scope_access_denied` remain compatibility Tool failures; approve/modify `authorization_scope_changed`, `authorization_scope_unbound`, `authorization_scope_unavailable`, and locked Binding denial all project to existing `RuntimeFailureCode.STALE_PENDING_ACTION` 409 without leaking the internal code; policy/manifest/opaque-token internal failure projects to existing `RuntimeFailureCode.OPERATION_FAILED` safe retryable 503. An Approval prepare result of `confirmation_required` is internal policy intent only: it creates no second Pending, duplicate `approval.requested`, interrupt, ToolMessage, or Provider continuation.

- [ ] **Step 2: Write RED concurrency tests**

Use two connections for Scope update vs approve, A→B→A ABA, target reparent/delete, parent deletion including standalone `add_note`, approve/approve, approve/reject, modify changing to another Application, and old-args late request. Binding and scope denial occur before claim; Pending remains rejectable.

- [ ] **Step 3: Write RED minimal ApprovalAuthorityResolver tests**

The resolver may read only trusted Operation/Pending identity columns, Conversation scope/revision, and active-Application visibility needed to create `ApprovalExecutionAuthority`. Its Source/history/attachment/Projector/Provider/model/renderer/full Typed Pipeline counters must be 0. Old null fingerprint and terminal/unavailable preheader results short-circuit before this resolver.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_approval_transaction.py tests/tool_authority/test_approval_authority_resolver.py tests/test_write_operations.py tests/pilot_runtime/test_confirmation.py -q
```

Expected: fail because locked scope HMAC/binding checks and sealed claim issuance are incomplete.

- [ ] **Step 5: Implement the minimal resolver and transaction order exactly**

```text
BEGIN IMMEDIATE
reload Operation/Conversation/Pending
identity and effective args digest
canonical scope/revision and HMAC
active parent visibility
session-bound Binding recheck
mutable/revision/stale checks
Pending claim/CAS and one-shot ExecutionClaim
approval.decided(approved | edited)
tool.started
guarded executor once
result/undo/transport projection
Ledger terminal + domain mutation commit
```

No file/keyring/Provider/network I/O occurs inside the transaction. `approval.decided` is emitted only after claim/CAS succeeds and before `tool.started`; claim/CAS losers and pre-executor failures emit neither event.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_approval_transaction.py tests/tool_authority/test_approval_authority_resolver.py tests/test_write_operations.py tests/pilot_runtime/test_confirmation.py -q
```

Expected: all pass; authorization denial never terminalizes or clears Pending.

- [ ] **Step 7: Commit locked approval**

```powershell
git add src/offerpilot/ai/write_operations.py src/offerpilot/pilot_runtime/continuation.py src/offerpilot/ai/tool_runtime/pipeline.py src/offerpilot/ai/tool_authority/composition.py tests/tool_authority/test_approval_transaction.py tests/tool_authority/test_approval_authority_resolver.py tests/test_write_operations.py tests/pilot_runtime/test_confirmation.py
git commit -m "feat: AI 在账本事务内复核批准授权"
```

### Task 14: Harden terminal/chained replay without creating Authority

**Files:**
- Modify: `src/offerpilot/ai/write_operations.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Create: `src/offerpilot/ai/pending_replay.py`
- Create: `tests/tool_authority/test_pending_replay_decoder.py`
- Create: `tests/tool_authority/test_replay_topology.py`
- Modify: `tests/agent_loop/test_confirmation.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED strict-decoder tests**

For `PendingReplayArgsDecoderV1`, cover exactly 65,536/65,537 bytes, depth 32/33, aggregate member/element 2,048/2,049, aggregate key/string UTF-8 65,536/65,537, duplicate key, non-finite, number range/canonical mismatch, surrogate, trailing content, array/scalar root, recursion and ordinary decode exception. Failures map to existing `RuntimeFailureCode.OPERATION_INTEGRITY_ERROR` 409.

- [ ] **Step 2: Write RED topology tests**

Final terminal replay reads Pending 0. Chained replay first loads `delivery_next_operation_id -> child primary proposed Operation -> exact pending_operation_id`. Typed branch verifies child HMAC then decodes once. Legacy `save_application_jd_version` uses its existing codec. Unknown/mixed adapter, missing child/pointer/relation, topology SQL error map to existing `RuntimeFailureCode.OPERATION_DELIVERY_UNKNOWN` 503. Terminal payload SHA mismatch, Typed decoder failure, child proposal HMAC mismatch, and deterministic confirmation projection mismatch map to existing `RuntimeFailureCode.OPERATION_INTEGRITY_ERROR` 409. Provider/Authority/Tool/executor remain 0.

- [ ] **Step 3: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_pending_replay_decoder.py tests/tool_authority/test_replay_topology.py tests/agent_loop/test_confirmation.py tests/test_chat_api.py -q
```

Expected: targeted tests fail because bounded Typed replay decoder and exact adapter split are absent.

- [ ] **Step 4: Implement deterministic replay branches**

New proposals and replay use the same strict canonical JSON. Never parse before topology/identity checks. Never derive Authority from replay. Keep the exact three Legacy names isolated.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/tool_authority/test_pending_replay_decoder.py tests/tool_authority/test_replay_topology.py tests/agent_loop/test_confirmation.py tests/test_chat_api.py -q
```

Expected: all pass with sync/SSE status, code, message, and retryable flag identical.

- [ ] **Step 6: Commit replay hardening**

```powershell
git add src/offerpilot/ai/pending_replay.py src/offerpilot/ai/write_operations.py src/offerpilot/pilot_runtime/continuation.py tests/tool_authority tests/agent_loop/test_confirmation.py tests/test_chat_api.py
git commit -m "feat: AI 强化待确认链式重放完整性"
```

### Task 15: Intersect Provider Surface with Segment Authority and freeze dependencies

**Files:**
- Create: `src/offerpilot/context_projector/authority_surface.py`
- Modify: `src/offerpilot/context_projector/selector.py`
- Modify: `src/offerpilot/context_projector/projector.py`
- Modify: `src/offerpilot/context_projector/contracts.py`
- Modify: `src/offerpilot/context_projector/binding.py`
- Modify: `src/offerpilot/context_projector/gateway.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Create: `tests/tool_authority/test_authority_surface.py`
- Create: `tests/tool_authority/test_dependency_policy.py`
- Create: `tests/fixtures/tool_authority/dependency_policy_v1.json`
- Modify: `tests/test_context_projector.py`

- [ ] **Step 1: Write RED DependencyPolicyV1 tests**

Pin version, 25-name coverage, dependency closure canonical manifest, unknown tool/dependency, missing node, cycle, and version mismatch. `validate_closed(selected_names, catalog_names)` is read-only and shared by Selector and Authority intersection.

- [ ] **Step 2: Write RED surface intersection tests**

Capability/scope intersection occurs before history budget. It removes complete envelopes only, never changes Provider schema/order/payload. Application scope removes `create_application` and `compare_offers`. Empty/policy/selector/dependency failure yields Provider 0. Fallback candidates reuse the exact Frozen Surface/Authority.

- [ ] **Step 3: Write RED build/invoke identity tests**

`provider_surface_build` cannot invoke. Provider network calls require a `ProviderInvocationIdentity` bound to exact Surface/Binding object identity, fingerprint, model-call ID, Gateway Session, candidate ordinal, and session-issued non-empty attempt ID. Explicitly reject negative ordinal, ordinal equal to/outside the frozen candidate count, empty attempt ID, and an attempt issued by another Gateway Session. Do not compare against ordinal/attempt values that do not exist before candidate execution. Copies/alternate surfaces call Provider 0.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/tool_authority/test_authority_surface.py tests/tool_authority/test_dependency_policy.py tests/test_context_projector.py -q
```

Expected: fail because authority intersection/dependency port do not exist and injected fallback remains.

- [ ] **Step 5: Implement intersection and delete alternate surface**

Delete `_project_injected_surface`, `injected-surface-v1`, and `typed_catalog_drift` fallback. Tests entering production Agent Loop must inject the complete 25-tool Catalog; narrower fakes stay at Pipeline unit boundary.

- [ ] **Step 6: Verify GREEN and unchanged Provider contract**

```powershell
uv run pytest tests/tool_authority/test_authority_surface.py tests/tool_authority/test_dependency_policy.py tests/test_context_projector.py tests/tool_pipeline/test_golden_assets.py -q
rg -n "typed_catalog_drift|_project_injected_surface|injected-surface-v1" src
```

Expected: tests pass; search returns no production fallback.

- [ ] **Step 7: Commit authority surface**

```powershell
git add src/offerpilot/context_projector src/offerpilot/ai/agent_loop.py tests/tool_authority tests/test_context_projector.py tests/fixtures/tool_authority
git commit -m "feat: AI 按执行授权收敛模型工具面"
```

### Task 16: Split Runtime policy/model composition and cut over new-turn sync/stream

**Files:**
- Modify: `src/offerpilot/pilot_runtime/contracts.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/ai/agent_contracts.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/pilot_runtime/test_contracts.py`
- Modify: `tests/pilot_runtime/test_start_turn.py`
- Modify: `tests/pilot_runtime/test_stream_preparation.py`
- Modify: `tests/agent_loop/test_contracts.py`
- Modify: `tests/agent_loop/test_runner.py`

- [ ] **Step 1: Write RED composition tests**

Delete `ResolvedModel.tool_context`. Introduce a Provider-free policy/catalog resolver and continuation model resolver. Prove the policy path cannot call Provider and the API no longer uses `capabilities=frozenset(ToolCapability)` or a broad `model_tool_context` callback.

- [ ] **Step 2: Write RED new-turn phase tests**

Sync and stream exact order:

```text
validate request
canonical load/create Conversation
Context Source snapshot
Segment Authority + authority-bound Context
Catalog/Profile/Selector/Authority Surface/Projector
continuation model resolver
persist user
AgentDriver.execute once
persist/project existing result
```

Invalid/deleted scope, policy, surface, or adapter preflight yields Provider 0 and no `model.requested`.

- [ ] **Step 3: Write RED Agent Loop seed tests**

One New Turn Seed owns one Segment Authority across all model calls, read-tool loops, and Provider candidate fallback. It cannot be replaced inside the Segment. Approval Authority cannot enter Provider/read/Pending paths.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_contracts.py tests/pilot_runtime/test_start_turn.py tests/pilot_runtime/test_stream_preparation.py tests/agent_loop/test_contracts.py tests/agent_loop/test_runner.py -q
```

Expected: fail because model/context resolution occurs before Source and `ResolvedModel.tool_context` remains.

- [ ] **Step 5: Implement split composition and both new-turn paths**

Authority-bound `ToolExecutionContext` is created only from the trusted Source snapshot and Profile. `AgentLoopInvocation` has one explicit Context/Authority carrier. Preserve ToolCall batch rules and external sync/SSE sequence.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/pilot_runtime/test_contracts.py tests/pilot_runtime/test_start_turn.py tests/pilot_runtime/test_stream_preparation.py tests/agent_loop/test_contracts.py tests/agent_loop/test_runner.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit new-turn cutover**

```powershell
git add src/offerpilot/pilot_runtime src/offerpilot/ai/agent_contracts.py src/offerpilot/ai/agent_loop.py src/offerpilot/api.py tests/pilot_runtime tests/agent_loop
git commit -m "refactor: AI 切换新运行作用域授权组装"
```

### Task 17: Cut over approve/modify continuation sync/stream and preserve provider-free routes

**Files:**
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_stream_preparation.py`
- Modify: `tests/agent_loop/test_confirmation.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write RED approval/continuation order tests**

Both sync and stream must execute:

```text
LedgerOperationPreheader
proposed-only Approval Authority
origin prepare/locked checks/claim/executor/terminal
delivery ownership
canonical Conversation reload
one new Source snapshot
new Segment Authority + model resolver
Agent Loop continuation
```

The old approval Authority and approve-before Conversation object cannot reach Provider. A legal Scope change after origin terminal is accepted by the new continuation Segment.

- [ ] **Step 2: Write RED Provider/Driver-zero route tests**

Reject, final terminal replay, ordinary delivery recovery, deterministic action, and Legacy deterministic confirmation create no Authority, Source, Projector, Provider, Typed Catalog, Schema/decode, Binding, preflight, or executor. Chained replay performs only its approved operation-owned Pending exception.

- [ ] **Step 3: Write RED sync/SSE compatibility tests**

Pin existing statuses, event sequences, error bodies, timeout/cancellation, sink failure, provider failure after terminal, chained Pending replacement, and one-executor guarantees. `RuntimeTransportAborted` cannot trigger another Provider/Tool/executor.

- [ ] **Step 4: Run and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_stream_preparation.py tests/agent_loop/test_confirmation.py tests/test_chat_api.py -q
```

Expected: targeted phase tests fail because approval resolves model too early and captures old context.

- [ ] **Step 5: Cut over all four confirmation paths together**

Update `_continue_ledger_confirmation`, `_prepare_ledger_confirmation_stream`, `_execute_prepared_ledger_confirmation`, and `_confirmation_source_loader`. Delete `_bind_confirmation_context` and any model-view `tool_context` authority source.

- [ ] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_stream_preparation.py tests/agent_loop/test_confirmation.py tests/test_chat_api.py -q
```

Expected: all pass with provider-free counters and old HTTP/SSE goldens unchanged.

- [ ] **Step 7: Commit confirmation cutover**

```powershell
git add src/offerpilot/pilot_runtime src/offerpilot/ai/agent_loop.py tests/pilot_runtime tests/agent_loop/test_confirmation.py tests/test_chat_api.py
git commit -m "refactor: AI 切换确认续跑授权事务"
```

### Task 18: Add deletion, AST, privacy, and serialization gates

**Files:**
- Create: `tests/tool_authority/test_source_gates.py`
- Create: `tests/tool_authority/test_privacy.py`
- Modify: `tests/agent_loop/test_deletion_gates.py`
- Modify: `tests/test_context_projector_source_gates.py`
- Modify: `tests/test_pilot_runtime_extraction_gate.py`

- [ ] **Step 1: Add AST/source gates for forbidden paths**

Mechanically reject production occurrences of:

```text
frozenset(ToolCapability)
request-derived capability/current binding
ToolExecutionContext construction outside composition root/factory
ResolvedModel.tool_context or broad model_tool_context
Binding resolver/Repository call before capability assertion
non_application_only resolver/Repository call before scope-policy denial
Provider adapter invocation without exact ProviderInvocationIdentity
Approval Authority passed to Provider/Selector/read execution/Typed Pending claim
Segment Authority used for approved write execution without confirmation claim
Conversation scope fields written outside create_conversation_with_scope/patch_conversation_with_scope/set_context_scope CAS
ExecutionAuthorization
direct Typed write without ExecutionClaim
Typed Pending creation without PendingAuthorityClaim
Typed Pending authority inferred from request/provider/page context or Pending args
read/list lazy Operation backfill
Reject token/authority reconstructed from pending_args
Typed-to-Legacy fallback
Legacy names in Provider Surface
typed_catalog_drift/injected surface fallback
scoped Repository fallback to unscoped list/get/update/delete
Application-bound point query or nine write ports calling unscoped Repository methods
Python post-filter for scoped data
Binding resolver implicit Session checkout/network/file/keyring/Provider/write
Authority Surface using a different DependencyPolicyV1 instance or copied dependency metadata
auto_approve authority bypass
```

- [ ] **Step 2: Add privacy canaries**

Assert raw capability sets, Application/Resume/Event/Offer/Note/JD IDs, context_ref/mode text, args, Provider answer, confirmation token, operation lease/owner, repository object, exception text, traceback, scope HMAC, and proof tokens never enter logs, Journal, Trace, Prompt, HTTP, SSE, Manifest, checkpoint, or generic payloads.

- [ ] **Step 3: Add serialization registry cleanup tests**

Every Authority/CallIdentity/Resolution/Constraint/Prepared/PendingClaim/ExecutionClaim/OmittedProof rejects copy/deepcopy/pickle/asdict/replace/to_json/checkpoint. Normal, exception, rollback, and cancellation paths leave bounded empty registries.

- [ ] **Step 4: Run gates and fix every failure**

```powershell
uv run pytest tests/tool_authority/test_source_gates.py tests/tool_authority/test_privacy.py tests/agent_loop/test_deletion_gates.py tests/test_context_projector_source_gates.py tests/test_pilot_runtime_extraction_gate.py -q
```

Expected: all pass.

- [ ] **Step 5: Run formatting/static checks on changed Python**

```powershell
uv run ruff check src/offerpilot/ai/tool_authority src/offerpilot/ai/tool_runtime src/offerpilot/ai/tool_specs src/offerpilot/context_projector src/offerpilot/pilot_runtime src/offerpilot/repositories tests/tool_authority
uv run mypy src
git diff --check
```

Expected: all pass.

- [ ] **Step 6: Commit mechanical gates**

```powershell
git add tests/tool_authority tests/agent_loop/test_deletion_gates.py tests/test_context_projector_source_gates.py tests/test_pilot_runtime_extraction_gate.py
git commit -m "test: AI 增加授权删除隐私与序列化门禁"
```

### Task 19: Run compatibility, release, migration, and browser matrices

**Files:**
- Create: `docs/reports/2026-08-24-scoped-tool-authority-release-verification.md`
- Modify only if evidence exposes a defect: production/tests already listed above

- [ ] **Step 1: Run focused authority matrix**

```powershell
uv run pytest tests/tool_authority tests/tool_pipeline tests/agent_loop tests/pilot_runtime -q
```

Expected: all pass with no new skip.

- [ ] **Step 2: Run Ledger, Chat, migration, and compatibility suites**

```powershell
uv run pytest tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py tests/test_chat_repository.py tests/test_chat_api.py tests/test_database.py tests/test_context_projector.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full backend gates**

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
```

Expected: all pass.

Then run the Windows manifest/union/node-ID/skip/aggregate gates with an isolated result directory:

```powershell
$gateRoot = Join-Path ([System.IO.Path]::GetTempPath()) "offerpilot-scoped-authority-gates"
New-Item -ItemType Directory -Force -Path $gateRoot | Out-Null
$backendResults = Join-Path $gateRoot "backend"
New-Item -ItemType Directory -Force -Path $backendResults | Out-Null
$fullManifest = @(& uv run pytest --collect-only -q --disable-warnings tests)
if ($LASTEXITCODE -ne 0) { throw "full pytest manifest collection failed" }
$fullManifest | Set-Content -LiteralPath (Join-Path $backendResults 'full-manifest.txt') -Encoding utf8
foreach ($group in @('agent','domain','knowledge','proposals','misc')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Group $group -ResultDir $backendResults
}
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -ResultDir $backendResults -Aggregate
$frontendResults = Join-Path $gateRoot "frontend"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -Collect -ResultDir $frontendResults
foreach ($group in @('components-core','components-chat','components-interview','components-offer','components-support','features','layout','lib','services','theme')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -Group $group -ResultDir $frontendResults
}
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -ResultDir $frontendResults -Aggregate
```

Expected: both aggregates pass with exact manifest union, unique node IDs, only the repository allowlisted skips, and no stale group result.

- [ ] **Step 4: Run frontend and static release gates**

```powershell
Push-Location web
npm test -- --run
npm run build
Pop-Location
uv run oc smoke --static-dir web/dist
```

Expected: all pass. If Docker-dependent or controlled real-AI verification cannot run, record the exact command, reason, and risk; do not claim it passed.

- [ ] **Step 5: Run controlled local verification and browser acceptance**

Run the exact verification commands:

```powershell
uv run oc verify --profile local --static-dir web/dist
uv run oc verify --profile real-ai --static-dir web/dist
```

With the built app, walk workspace/global/application Chat in the built-in browser. Verify same-scope reads, cross-scope denial, application collections, standalone `add_note`, Resume explicit unbound behavior, HITL approve/modify/reject, terminal replay, and SSE parity. Record synthetic evidence only. If the controlled real-AI environment is unavailable, record the command, failure reason, and remaining risk rather than claiming it passed.

- [ ] **Step 6: Verify migration rollback boundary**

Prove the new binary works on 0028, migrated old Typed proposed/null can reject but cannot approve, Legacy and terminal replay still work, and the old binary cannot create a new Typed proposal after migration. Record that rollback requires stopping the app and restoring a pre-0028 database snapshot.

- [ ] **Step 7: Write the release report**

Include:

```text
fixed baselines and commits
characterization -> red -> green evidence per task
25 Typed / 3 Legacy exact boundary
internal breaking changes
external compatibility result
0028 migration and rollback boundary
all verification commands and results
known boundaries: Resume unbound; standalone add_note targetless but scope-valid
remaining risks and any unavailable gates
no push/no merge status
```

- [ ] **Step 8: Commit the verified implementation report**

```powershell
git add docs/reports/2026-08-24-scoped-tool-authority-release-verification.md
git commit -m "docs: AI 记录作用域工具授权验收结果"
```

### Task 20: Independent code review and final clean-state audit

**Files:**
- Modify as required by review findings
- Update: `docs/reports/2026-08-24-scoped-tool-authority-release-verification.md`

- [ ] **Step 1: Start an independent review against fixed baseline**

Review `1574d0e..HEAD` for the approved design, authorization bypass, SQL scoping, transaction ordering, replay/reject privacy, HTTP/SSE compatibility, migration safety, and test credibility. Require explicit P0/P1/P2/P3 classification.

- [ ] **Step 2: Resolve every P0/P1/P2**

For each finding: add a failing regression test, demonstrate RED, implement the smallest fix, demonstrate GREEN, then rerun the affected matrix. Do not accept an open P0/P1/P2.

- [ ] **Step 3: Re-run final evidence after the last code change**

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
Push-Location web
npm test -- --run
npm run build
Pop-Location
uv run oc smoke --static-dir web/dist
git diff --check
git show --check --stat HEAD
```

Expected: all available gates pass on the final tree.

Recreate and rerun the final Windows manifest/union/node-ID/skip/aggregate evidence after the last review fix:

```powershell
$finalGateRoot = Join-Path ([System.IO.Path]::GetTempPath()) "offerpilot-scoped-authority-final-gates"
$finalBackend = Join-Path $finalGateRoot "backend"
New-Item -ItemType Directory -Force -Path $finalBackend | Out-Null
$finalManifest = @(& uv run pytest --collect-only -q --disable-warnings tests)
if ($LASTEXITCODE -ne 0) { throw "final pytest manifest collection failed" }
$finalManifest | Set-Content -LiteralPath (Join-Path $finalBackend 'full-manifest.txt') -Encoding utf8
foreach ($group in @('agent','domain','knowledge','proposals','misc')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Group $group -ResultDir $finalBackend
}
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -ResultDir $finalBackend -Aggregate
$finalFrontend = Join-Path $finalGateRoot "frontend"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -Collect -ResultDir $finalFrontend
foreach ($group in @('components-core','components-chat','components-interview','components-offer','components-support','features','layout','lib','services','theme')) {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -Group $group -ResultDir $finalFrontend
}
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-vitest-groups.ps1 -ResultDir $finalFrontend -Aggregate
```

Expected: both final aggregates pass on the exact final tree.

- [ ] **Step 4: Record CR conclusion and commit any review fixes**

Use a Chinese Conventional Commit title, for example:

```powershell
git add src tests docs/reports/2026-08-24-scoped-tool-authority-release-verification.md
git commit -m "fix: AI 收口作用域授权独立复审问题"
```

Skip this commit only when review requires no file change.

- [ ] **Step 5: Audit final repository state**

```powershell
git status --short --branch
git log --oneline --decorate 1574d0e..HEAD
git diff --name-status 1574d0e..HEAD
git branch -vv
```

Expected: implementation worktree clean; branch is local/unpushed; no merge into `main`; root workspace and other worktrees untouched.

## Plan self-review gates

Before starting Task 1 implementation, verify this plan itself:

- [ ] Every approved-design section maps to at least one task/test: authority types, 25-tool matrix, scope mutation, visibility, scoped SQL, read UoW, Pending HMAC, locked approval, reject, replay, surface, runtime, migration, privacy, cutover, rollback.
- [ ] Every production file named in the approved design appears in a task or is deliberately unchanged.
- [ ] The exact 25 Typed and 3 Legacy names are frozen; Tool Metadata Convergence is absent.
- [ ] No step introduces API/UI/Provider-schema changes, new retry, RBAC, persistent Resume binding, Legacy migration, or exactly-once claims beyond the approved boundary.
- [ ] No golden writer/update mechanism is planned.
- [ ] Every behavior-changing task through Task 17 has an explicit RED command, GREEN command, and separate `git add`/`git commit` sequence; Tasks 18–20 are gate, release, and independent-review verification tasks.
- [ ] A manual incomplete-marker scan finds no deferred implementation detail or update mechanism.
