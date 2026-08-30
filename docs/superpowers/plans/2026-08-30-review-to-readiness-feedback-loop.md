# Review-to-Readiness Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 exact Interview Review V2 中用户确认的准备重点，经模型不可见的 Product Action HITL、Write Operation Ledger 与 required Undo，安全接入 exact target Event 的 Adaptive Practice V2 和 Interview Preparation V2，同时把 Story Proposal confirm 切换到同一独立 Product Action 安全核心。

**Architecture:** 新增与 Agent Tool/Legacy/Agent Compensation 完全分离的 `product_actions` 域（2 primary + 2 compensation），复用 Ledger 的 canonical HMAC、terminal payload 和 transition primitives，但拥有独立 Catalog、Issuer、Repository、Coordinator、owner-scoped proof 与 commit-unknown 状态机。`review_readiness` 域持有不可变 Signal Version/Evidence、候选与只读投影；Practice 和 Preparation 只消费 current、active、source-current 的 exact Version，Context Projector 的 `confirmed_memory` 继续生产不可达。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2、SQLite、Pytest、Pydantic 2、React 18、TypeScript 5.6、TanStack Query、Ant Design、Vitest。

---

## File map and ownership

数据库和高冲突 composition 文件由主集成人串行修改：

- `src/offerpilot/models.py`：0029 所需 ORM shape、WriteOperation manifest/delivery/undo checks。
- `src/offerpilot/db.py`：0029 rebuild、indexes、triggers、migration marker 与既有 trigger 重装。
- `src/offerpilot/api.py`：Product Action composition、owner-scoped routes、旧 Story confirm adapter、错误 codec。
- `web/src/layout/AppShell.tsx`：canonical owner handoff、唯一 review/story/preparation/practice owner。

新增边界文件：

- `src/offerpilot/product_actions/{contracts,catalog,issuer,repository,coordinator,compensation}.py`：模型不可见 Product Action 2/2。
- `src/offerpilot/review_readiness/{contracts,repository,candidates,projection,preparation_selection,contributor}.py`：Signal 聚合、候选、advisory、Preparation V2 selection 和 future-only type asset。
- `web/src/features/reviewReadiness/{contracts,service,ReviewReadinessNextStep,ReadinessFeedbackAdvisory,ProductActionConfirmation}.tsx|ts`：四个现有 Core Task owner 内的 UI 适配器。

现有领域 owner 文件：

- Note/Review：`repositories/notes.py`、`repositories/interview_review_proposals.py`、`ai/interview_review_proposals.py`。
- Story：`repositories/interview_stories.py`、`ai/interview_stories.py`、`components/InterviewStoryDrawer.tsx`。
- Practice：`repositories/adaptive_interview_practice.py`、`components/AdaptiveInterviewPracticeWorkspace.tsx`。
- Preparation：`repositories/interview_preparation_proposals.py`、`ai/interview_preparation_proposals.py`、`components/InterviewPreparationProposalDrawer.tsx`。
- Context gate：`context_projector/**` 和现有 Phase 4 manifest tests；生产 registry 不接入 future contributor。

所有任务执行同一 TDD 循环：先添加一个可观察行为的失败测试并确认失败原因；再写最小实现；聚焦测试 GREEN 后才整理代码；提交前运行 Ruff/Mypy 或相应前端测试以及 `git diff --check`。`git add` 与 `git commit` 分开执行，提交标题为中文且符合 `<type>: AI <subject>`。

### Task 0: Pin approved baseline, allowlists and RED gates

**Files:**

- Modify: `docs/superpowers/specs/2026-08-30-review-to-readiness-feedback-loop-design.md`
- Create: `tests/fixtures/review_readiness/review_to_readiness_baseline_c5a020c.json`
- Create: `tests/fixtures/review_readiness/event_lifecycle_v1.json`
- Create: `tests/fixtures/review_readiness/interview_preparation_v1_c5a020c.json`
- Create: `tests/test_review_to_readiness_baseline.py`
- Create: `tests/test_review_to_readiness_source_gates.py`
- Create: `web/src/features/reviewReadiness/reviewReadinessGate.test.ts`

- [x] **Step 1: Freeze the exact approved baseline and classifications**

Write a manually reviewed asset with this closed envelope:

```json
{
  "schema_version": 1,
  "source_baseline": "c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff",
  "provider_tools": 25,
  "legacy_deterministic": 3,
  "agent_compensations": 4,
  "product_actions": ["confirm_interview_story", "save_review_readiness_signal"],
  "product_action_compensations": ["undo:confirm_interview_story", "undo:save_review_readiness_signal"],
  "production_contributors_disabled": ["confirmed_memory", "knowledge_context", "older_conversation_summary"]
}
```

Record SHA-256 constants in the Python test and assert no production module reads this review-only fixture.
Freeze the cross-language EventLifecycle fixture with the exact status mapping from the design; the later backend and existing frontend classifiers must both consume the fixture only in tests and produce identical results.
Before any `schemas.py` or `api.py` change, capture from `c5a020c` one deterministic Interview Preparation V1 input snapshot, canonical JSON bytes, request fingerprint and input fingerprint in `interview_preparation_v1_c5a020c.json`; hard-code its fixture SHA in the baseline test. Task 9 must compare against this pinned asset and must not regenerate expected bytes from the then-current implementation.

- [x] **Step 2: Add RED mechanical gates**

`test_review_to_readiness_source_gates.py` must initially fail on missing `0029_review_to_readiness_feedback`, missing Product Action 2/2 modules, old `confirm_attempt()` production call, Proposal-driven practice recommendation, and missing Note revision helper. The frontend gate must initially fail on client-generated Story confirmation tokens and absent canonical-owner review readiness components. Every failure uses a named rule such as `ledger:missing-product-action`, `story:self-committing-confirm`, `practice:unconfirmed-proposal-source`, or `ui:client-authorization-token`.

- [x] **Step 3: Verify asset PASS and source gates RED**

```powershell
uv run pytest tests/test_review_to_readiness_baseline.py -q
uv run pytest tests/test_review_to_readiness_source_gates.py -q
cd web
npm test -- --run src/features/reviewReadiness/reviewReadinessGate.test.ts
cd ..
```

Expected: the immutable asset test passes; both production gates fail only for named missing/cutover requirements.

- [x] **Step 4: Commit approved status, plan, fixtures and RED gates**

> Task 0 只固化基线与机械门禁；后端与前端 source gate 仍保持预期 RED，待后续任务逐项转绿。

```powershell
git add docs/superpowers/specs/2026-08-30-review-to-readiness-feedback-loop-design.md tests/fixtures/review_readiness tests/test_review_to_readiness_baseline.py tests/test_review_to_readiness_source_gates.py web/src/features/reviewReadiness/reviewReadinessGate.test.ts
git add -f docs/superpowers/plans/2026-08-30-review-to-readiness-feedback-loop.md
git commit -m "test: AI 固化复盘准备闭环基线"
```

### Task 1: Implement 0029 models, rebuilds and database invariants

**Files:**

- Modify: `src/offerpilot/models.py`
- Modify: `src/offerpilot/db.py`
- Create: `tests/test_review_to_readiness_migration_0029.py`
- Modify: `tests/test_interview_review_migrations.py`
- Modify: `tests/test_interview_stories_migrations.py`
- Modify: `tests/test_adaptive_interview_practice_migrations.py`
- Modify: `tests/tool_authority/test_migration_0028.py`
- Modify: `tests/test_conditional_delete_repositories.py`

- [ ] **Step 1: Write migration RED tests against empty and real 0028 databases**

Cover exact columns/defaults, Note revision=1, Proposal V1/NULL history, Story Attempt generation=0, legacy Practice origin, old unique removal, column-for-column WriteOperation and transition preservation, repeat startup, injected rollback, `integrity_check`, `foreign_key_check`, old 25/3/4 accepted rows and unknown manifest rejection. Add explicit RED cases for:

- Product Action route action/source/origin mapping, semantic/historical fingerprint iff rules, 16 KiB bytes, integer-not-bool coercion and active/terminal truth table;
- route-without-parent rejection; raw parent-only SQL as the declared SQLite boundary; parent terminal clearing the route in the same statement; route identity immutability and no-delete;
- mutually exclusive Product primary versus Product compensation manifest rows and every cross-pair rejection;
- Adaptive legacy/V2 origin truth table, target fingerprint required/format/immutable, both partial uniques and source-only/target-only/both locator `SET NULL` history;
- Signal source ID rebind and NULL→non-NULL rejection;
- every historical `write_operation_transition` column and ordered row remaining byte-for-byte unchanged.

The Signal self-FK test must execute:

```python
conn.execute("INSERT INTO interview_readiness_signal_versions (...) VALUES (...)")
with pytest.raises(sqlite3.IntegrityError):
    conn.execute("INSERT INTO interview_readiness_signal_versions (... parent_version_id from another signal ...)")
conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))
assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
```

It must separately prove direct deletion of a Version while its Signal survives is rejected.

- [ ] **Step 2: Run migration tests and verify RED**

```powershell
uv run pytest tests/test_review_to_readiness_migration_0029.py tests/test_interview_review_migrations.py tests/test_interview_stories_migrations.py tests/test_adaptive_interview_practice_migrations.py -q
```

Expected: fail because 0029 tables/columns/checks do not exist and the legacy Practice unique remains.

- [ ] **Step 3: Add the exact ORM shapes**

Add `InterviewNote.content_revision/updated_at`, Proposal V2 fields, Story Product Action pointer/generation, Practice V2 fields, and these new classes:

```python
class ProductActionProposal(Base): ...
class InterviewReadinessSignal(Base): ...
class InterviewReadinessSignalVersion(Base): ...
class InterviewReadinessSignalEvidence(Base): ...
```

Use `(parent_version_id, signal_id) -> (id, signal_id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`; add Signal to `APPLICATION_FOREIGN_KEY_MODELS`. Replace the ordinary Practice proposal/focus unique with origin-specific partial uniques. Extend `WriteOperation` with two mutually exclusive branches:

```text
Product primary:
  operation_role=primary
  adapter_kind=product_action
  tool_name in the exact 2 Product Action names
  result_contract=product_action_json_v1 when committed/failed
  required undo when committed
  terminal delivery=not_applicable

Product compensation:
  operation_role=compensation
  adapter_kind=compensation
  tool_name in the exact 2 Product Action compensation names
  result_contract=compensation_json_v1 when committed/failed
  rejected is forbidden
  terminal delivery=not_applicable
```

Do not register either compensation name in the Agent Compensation Registry.

- [ ] **Step 4: Implement `_ensure_review_to_readiness_feedback_schema()`**

The migration must rebuild `write_operations` and `adaptive_practice_plans` in one controlled transaction, copy every historical column explicitly, avoid recomputing bytes/digests/timestamps, swap tables, recreate existing 0026/0028 indexes/triggers plus 0029 guards, then write the migration marker last. Install route→parent, parent-terminal→route-clear, immutable/no-delete, source monotonic-null, Version immutable/delete, Product compensation mapping and exact HMAC/UUID/JSON byte checks.

- [ ] **Step 5: Verify migration GREEN**

```powershell
uv run pytest tests/test_review_to_readiness_migration_0029.py tests/test_interview_review_migrations.py tests/test_interview_stories_migrations.py tests/test_adaptive_interview_practice_migrations.py tests/tool_authority/test_migration_0028.py tests/test_conditional_delete_repositories.py -q
uv run ruff check src/offerpilot/models.py src/offerpilot/db.py tests/test_review_to_readiness_migration_0029.py
uv run mypy src
git diff --check
```

- [ ] **Step 6: Commit 0029**

```powershell
git add src/offerpilot/models.py src/offerpilot/db.py tests/test_review_to_readiness_migration_0029.py tests/test_interview_review_migrations.py tests/test_interview_stories_migrations.py tests/test_adaptive_interview_practice_migrations.py tests/tool_authority/test_migration_0028.py tests/test_conditional_delete_repositories.py
git commit -m "feat: AI 建立复盘准备闭环数据模型"
```

### Task 2: Add Note revision and Interview Review Proposal V2

**Files:**

- Modify: `src/offerpilot/repositories/notes.py`
- Modify: `src/offerpilot/repositories/application_events.py`
- Modify: `src/offerpilot/repositories/interview_review_proposals.py`
- Modify: `src/offerpilot/ai/interview_review_proposals.py`
- Modify: `src/offerpilot/pilot_runtime/compensation.py`
- Modify: `src/offerpilot/schemas.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/test_notes_api.py`
- Modify: `tests/test_events_api.py`
- Modify: `tests/test_conditional_delete_repositories.py`
- Modify: `tests/tool_authority/test_scoped_writes.py`
- Modify: `tests/tool_metadata/test_compensation_registry.py`
- Modify: `tests/test_interview_review_proposals_repository.py`
- Modify: `tests/test_interview_review_proposals_api.py`
- Modify: `tests/test_review_to_readiness_source_gates.py`

- [ ] **Step 1: Write revision and V2 RED tests**

Cover create revision 1; every content/application/event update increments once and updates timestamp; REST, scoped Agent and bulk paths use the same rule; reads/generation/capture do not increment. Event hard delete is also a binding write: REST delete, scoped Agent delete, conditional delete and Agent compensation must explicitly unbind every surviving Note through the same revision helper in the same transaction before deleting the Event. Successful delete increments once; missing/cross-scope/predicate mismatch/compensation conflict/rollback and already-unbound Note increment zero. Add an Event `BEFORE DELETE RAISE(ABORT)` rollback case and an AST ownership gate against direct production `ApplicationEvent` deletes outside the approved owner primitive. Add Provider-before/after races including ABA content restored to identical bytes and Event deletion: generation must reject because revision changed or the source Event is missing. Historical V1/NULL remains readable but ineligible; every new ready Proposal is V2 with exact source revision.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_notes_api.py tests/test_events_api.py tests/test_conditional_delete_repositories.py tests/tool_authority/test_scoped_writes.py tests/tool_metadata/test_compensation_registry.py tests/test_interview_review_proposals_repository.py tests/test_interview_review_proposals_api.py -q
uv run pytest tests/test_review_to_readiness_source_gates.py -k "not production_cutover_gate" -q
```

- [ ] **Step 3: Centralize the atomic Note update**

All production update paths must call one helper equivalent to:

```python
def _revisioned_note_values(values: Mapping[str, object]) -> dict[str, object]:
    return {
        **values,
        "content_revision": InterviewNote.content_revision + 1,
        "updated_at": func.current_timestamp(),
    }
```

Do not increment on read-only or downstream writes. Add `content_revision` and `updated_at` to additive API/TypeScript output later without requiring clients to submit them.

Event deletion must not rely on the FK's implicit `ON DELETE SET NULL`, which bypasses revision. Add one Session-bound owner primitive that first performs an `UPDATE interview_notes` restricted by the exact final Event/scope/conditional predicate, sets `application_event_id=NULL` through `_revisioned_note_values(...)`, and then deletes that exact Event under the same writer transaction. REST, scoped Agent, conditional delete and Agent compensation all delegate to it. Because the Note locator is already NULL, the subsequent FK action cannot double-increment. Event DELETE failure rolls the unbind/revision back; direct production Event deletes are mechanically forbidden outside this owner. Do not add a migration trigger.

- [ ] **Step 4: Freeze and recheck Proposal V2 identity**

Before Provider call freeze Note revision and canonical source fingerprint; after Provider returns, the ready write transaction must reload exact Note/Event and compare resource, revision and fingerprint. Persist `proposal_schema_version=2` and `source_note_revision=<frozen revision>` without changing Provider-visible snapshot bytes or proposal JSON shape.

- [ ] **Step 5: Verify GREEN and commit**

```powershell
uv run pytest tests/test_notes_api.py tests/test_events_api.py tests/test_conditional_delete_repositories.py tests/tool_authority/test_scoped_writes.py tests/tool_metadata/test_compensation_registry.py tests/test_interview_review_proposals_repository.py tests/test_interview_review_proposals_api.py -q
uv run pytest tests/test_review_to_readiness_source_gates.py -k "not production_cutover_gate" -q
uv run ruff check src/offerpilot/repositories/notes.py src/offerpilot/repositories/application_events.py src/offerpilot/repositories/interview_review_proposals.py src/offerpilot/ai/interview_review_proposals.py src/offerpilot/pilot_runtime/compensation.py src/offerpilot/schemas.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/repositories/notes.py src/offerpilot/repositories/application_events.py src/offerpilot/repositories/interview_review_proposals.py src/offerpilot/ai/interview_review_proposals.py src/offerpilot/pilot_runtime/compensation.py src/offerpilot/schemas.py src/offerpilot/api.py tests/test_notes_api.py tests/test_events_api.py tests/test_conditional_delete_repositories.py tests/tool_authority/test_scoped_writes.py tests/tool_metadata/test_compensation_registry.py tests/test_interview_review_proposals_repository.py tests/test_interview_review_proposals_api.py tests/test_review_to_readiness_source_gates.py
git commit -m "feat: AI 版本化面试复盘来源"
```

### Task 3: Build the sealed Product Action 2/2 core

**Files:**

- Create: `src/offerpilot/product_actions/__init__.py`
- Create: `src/offerpilot/product_actions/contracts.py`
- Create: `src/offerpilot/product_actions/catalog.py`
- Create: `src/offerpilot/product_actions/issuer.py`
- Create: `src/offerpilot/product_actions/repository.py`
- Create: `src/offerpilot/event_lifecycle.py`
- Modify: `src/offerpilot/ai/write_operations.py`
- Create: `tests/product_actions/test_catalog.py`
- Create: `tests/product_actions/test_identity.py`
- Create: `tests/product_actions/test_repository.py`
- Create: `tests/product_actions/test_isolation.py`
- Create: `tests/test_event_lifecycle_v1.py`
- Modify: `tests/tool_metadata/test_production_bundle.py`
- Modify: `tests/tool_metadata/test_published_operation_checks.py`

- [ ] **Step 1: Write Catalog/isolation/identity RED tests**

Assert exact runtime classification `25 / 3 / 4 + 2 / 2`, Provider schema bytes/order unchanged, Product Action names returned by Provider are `unknown_tool` with executor 0, and Product modules never import ToolCatalog/LegacyCatalog/Agent compensation/Provider. Add cross-process goldens for five HMAC envelopes, tagged null, Chinese `user_note`, generation changes and deterministic UUIDs.
Add exact boundary tests for 16,384/16,385-byte route JSON, duplicate JSON keys, NaN/Infinity, boolean-as-integer, malformed UUID/HMAC and action/source cross-pairs. The HTTP-safe decoder must operate on raw request bytes with a duplicate-key-aware object hook before Pydantic/default normalization; FastAPI `dict = Body(...)` is not sufficient for these routes.
For bundle recovery, cover publication `all_absent | exact_proposed | exact_terminal | unreadable`, plus every parent/route/seq1 single-sided, missing, extra, duplicate, wrong-state or wrong-order corruption. Fresh reconciliation must never repair a partial bundle.
Prove token identity mechanics directly: the issuer derives the server token before `BEGIN`, the proposed insert stores the matching fingerprint, and the post-commit response returns the exact same raw token. A restart must use the Operation's stored key profile even after active-key rotation; a missing historical key fails closed and never substitutes a new token. Add proof-negative tests for ordinary DTOs, copy/serialization, cross-container, cross-owner, cross-source, cross-action, cross-proof-union, repeated consumption and ABA reuse. `SignalOwnerRecoveryProof` has no generation field; `StoryOwnerRecoveryProof` binds both expected generations; action-discriminated `RejectionOnlyRecoveryProof` fixes `live_source_state=not_observed`.
Load `tests/fixtures/review_readiness/event_lifecycle_v1.json` in a backend RED test and require one `classify_event_lifecycle_v1(status: object)` implementation for every alias and unknown fallback before Candidate work can begin.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/product_actions/test_catalog.py tests/product_actions/test_identity.py tests/product_actions/test_repository.py tests/product_actions/test_isolation.py tests/test_event_lifecycle_v1.py tests/tool_metadata/test_production_bundle.py tests/tool_metadata/test_published_operation_checks.py -q
```

- [ ] **Step 3: Implement closed contracts and Catalogs**

Export exact names and sealed proof types:

```python
PRODUCT_ACTION_NAMES = ("confirm_interview_story", "save_review_readiness_signal")
PRODUCT_ACTION_COMPENSATION_NAMES = (
    "undo:confirm_interview_story",
    "undo:save_review_readiness_signal",
)

class ProductActionCatalogV1: ...
class ProductActionCompensationCatalogV1: ...
class ProductActionRouteProof: ...
class HistoricalStoryRouteProof: ...
class SignalOwnerRecoveryProof: ...
class StoryOwnerRecoveryProof: ...
class RejectionOnlyRecoveryProof: ...
class ProductActionExecutionAuthorization: ...
```

Proofs bind issuer/container/registry incarnation, expose no serialization/copy protocol, are single-consume and revoked on every exit path.
Implement `classify_event_lifecycle_v1(status: object)` from the pinned fixture as the sole backend classifier. Candidate, advisory, Practice and Preparation must import this module; no task may add a temporary local lifecycle classifier.

- [ ] **Step 4: Implement issuer identity and token recovery**

Use the approved UUID namespaces and derivation order. Validate route payload before any HMAC. Read the Ledger key profile before `BEGIN`, derive the raw server token once, persist only its Ledger fingerprint, and return that same token after commit. Restart recovery must use the operation's stored key ID; missing key fails closed without rotation.

- [ ] **Step 5: Implement ProductActionProposalRepository**

Provide only bundle publication/load/reconciliation methods. Every load reads parent, route and `ORDER BY seq` transitions and validates exact prefix:

```python
EXPECTED_PREFIX = {
    "proposed": ((1, "proposed"),),
    "rejected": ((1, "proposed"), (2, "rejected")),
    "committed": ((1, "proposed"), (2, "approved"), (3, "claimed"), (4, "committed")),
    "failed": ((1, "proposed"), (2, "approved"), (3, "claimed"), (4, "failed")),
}
```

The publication UoW inserts parent+route+seq1 inside `BEGIN IMMEDIATE`, reverse-reads the exact pair before commit, and never exposes a bare Product Action parent insert. Partial/mismatched state is an integrity failure, never repairable.

- [ ] **Step 6: Exclude product rows from Chat delivery ownership**

Guard every delivery lease, heartbeat, takeover and fallback query in `ai/write_operations.py` with `adapter_kind != 'product_action'`; keep Agent/Legacy/Agent-compensation behavior byte-stable.

- [ ] **Step 7: Verify GREEN and commit**

```powershell
uv run pytest tests/product_actions/test_catalog.py tests/product_actions/test_identity.py tests/product_actions/test_repository.py tests/product_actions/test_isolation.py tests/test_event_lifecycle_v1.py tests/tool_metadata -q
uv run ruff check src/offerpilot/product_actions src/offerpilot/event_lifecycle.py src/offerpilot/ai/write_operations.py tests/product_actions tests/test_event_lifecycle_v1.py
uv run mypy src
git diff --check
git add src/offerpilot/product_actions src/offerpilot/event_lifecycle.py src/offerpilot/ai/write_operations.py tests/product_actions tests/test_event_lifecycle_v1.py tests/tool_metadata/test_production_bundle.py tests/tool_metadata/test_published_operation_checks.py
git commit -m "feat: AI 建立独立产品操作安全核心"
```

### Task 4: Implement Product Action HITL, terminal codecs and Readiness Signal write

**Files:**

- Create: `src/offerpilot/product_actions/coordinator.py`
- Create: `src/offerpilot/review_readiness/__init__.py`
- Create: `src/offerpilot/review_readiness/contracts.py`
- Create: `src/offerpilot/review_readiness/repository.py`
- Create: `src/offerpilot/review_readiness/candidates.py`
- Modify: `src/offerpilot/schemas.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/product_actions/test_coordinator.py`
- Create: `tests/test_review_readiness_repository.py`
- Create: `tests/test_review_readiness_candidates.py`
- Create: `tests/test_review_readiness_api.py`

- [ ] **Step 1: Write candidate, concurrency and HITL RED tests**

Cover every candidate closed state, structure cap and evidence path; same key/same input concurrent replay; same key/different input conflict; same semantic focus with different keys produces one active winner and stable non-leaking 409; reject performs zero candidate/source/capability/binding/preflight/executor/Provider calls; approve/modify execute once; terminal replay executes zero; cancellation/BaseException propagates after cleanup.

Add the complete Signal publication/decision matrix. Publication distinguishes all-absent, exact proposed, exact terminal and unreadable; any parent/route/seq1 partial is an integrity error. Decision starts from an already-persisted proposal, so absent/partial must never reuse publication's rebuild rule; exact proposed with the original decision payload may retry, different decision/effective payload conflicts, and terminal replay validates request fingerprint + terminal digest + the complete ordered prefix with executor=0. `rejected` prefix is valid only for primary operations; compensation has only proposed/committed/failed. Exercise the semantic loser key after winner active/rejected/declared-failed/committed, and golden-test every action-local result/visible/transport/undo/aggregate byte boundary plus rejected/failed codecs.
For Signal proposal authorization, assert the capability check short-circuits before any Application/Note/Proposal/Candidate query. Owner recovery must reject ordinary DTOs, every cross-proof/container/owner/source/action combination, duplicate consumption and ABA proofs; generic GET, terminal responses and cross-owner paths never expose a token. Rejection-only recovery is action-discriminated and can only observe `live_source_state=not_observed`. For both Signal and Story, test source existing/changed/missing branches and require source-currentness repository calls=0, capability=0, binding=0 and preflight=0; recovery uses only the validated Operation/route HMAC and returns exactly `allowed_decisions=('reject',)`.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/product_actions/test_coordinator.py tests/test_review_readiness_repository.py tests/test_review_readiness_candidates.py tests/test_review_readiness_api.py -q
```

- [ ] **Step 3: Implement candidate canonicalization and source-bound proposal issuance**

`project_readiness_candidates(note_id, proposal_id, session)` accepts only current V2 Proposal/exact revision/completed interview Event and 1–5 verbatim evidence refs. Compute the approved `candidate_fingerprint` envelope and return at most eight safe candidates without any write/Provider/Tool/Ledger side effect.

The route accepts only:

```json
{"proposal_id":1,"focus_id":"focus-1","expected_note_revision":2,"expected_candidate_fingerprint":"sha256:...","idempotency_key":"uuid","user_note":""}
```

Server reconstructs all IDs, statement and evidence; client-supplied text/evidence/action names are rejected.

- [ ] **Step 4: Implement `ProductActionCoordinator`**

Keep proposal publication and decision commit-unknown as distinct state machines. Approve/modify follows prepare → capability/binding/preflight → `BEGIN IMMEDIATE` → mutable recheck → seq2 approved → seq3 claimed → one Session-bound executor → domain+terminal+seq4 commit. Reject decodes only control envelope and commits rejected+seq2; ordinary unmapped exceptions rollback to proposed; the one declared Story conflict may terminalize failed after SAVEPOINT rollback.

- [ ] **Step 5: Implement Signal aggregate transaction and codecs**

Create Signal + active Version + ordered Evidence, set current pointer only after aggregate completeness, and terminalize Ledger in the same transaction. Statement is exact focus text; only `user_note` is modifiable. Produce the approved `product_action_json_v1` safe result/transport/required undo payload and enforce action-local byte caps before commit.

- [ ] **Step 6: Add safe Product Action APIs**

Add proposal, generic safe GET, owner recovery, rejection-only recovery and decision routes exactly as designed. Generic GET never returns a proposed token; full owner or rejection-only recovery consumes a sealed proof once; source missing/changed still allows reject but never approve/modify.

- [ ] **Step 7: Verify GREEN and commit**

```powershell
uv run pytest tests/product_actions/test_coordinator.py tests/test_review_readiness_repository.py tests/test_review_readiness_candidates.py tests/test_review_readiness_api.py -q
uv run ruff check src/offerpilot/product_actions/coordinator.py src/offerpilot/review_readiness src/offerpilot/schemas.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/product_actions/coordinator.py src/offerpilot/review_readiness src/offerpilot/schemas.py src/offerpilot/api.py tests/product_actions/test_coordinator.py tests/test_review_readiness_repository.py tests/test_review_readiness_candidates.py tests/test_review_readiness_api.py
git commit -m "feat: AI 保存可审计复盘准备重点"
```

### Task 5: Cut Story confirmation over to Product Action and generation N+1

**Files:**

- Modify: `src/offerpilot/repositories/interview_stories.py`
- Modify: `src/offerpilot/api.py`
- Modify: `src/offerpilot/schemas.py`
- Modify: `tests/test_interview_stories_repository.py`
- Modify: `tests/test_interview_stories_api.py`
- Create: `tests/test_interview_story_product_actions.py`

- [ ] **Step 1: Write Story publication/decision RED matrix**

Cover the four-object ready bundle (Attempt+parent+route+seq1), publication response loss with concurrent approve/reject/declared failure, historical confirmed replay, historical-ready bridge, source-changed rejection-only recovery, bound executor transaction rollback, direct commit 201/created true and every fresh reconciliation/replay 200/created false. Historical-ready same legacy token+same payload converges; different token or payload conflicts; the legacy token is only HMAC-bound request identity and never executor authorization.

- [ ] **Step 2: Write N→N+1 RED matrix**

Only ready + rejected current N may create N+1. The exact body is `{expected_generation_revision, expected_product_action_generation}` and rejects extra fields, old token, content/evidence and a generic `generation` alias. Cover direct 201, fresh proposed 200 with same token, terminal replay without token, 20-way single winner, old token rejection, failed/stale/invalidated/confirmed refusal, later-pointer historical replay and every partial/unreadable commit-unknown class.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/test_interview_stories_repository.py tests/test_interview_stories_api.py tests/test_interview_story_product_actions.py -q
```

- [ ] **Step 4: Publish ready Story proposals atomically**

Keep Provider outside writer transaction. Freeze validated result and identities before entering a short UoW that publishes ready Attempt, Product Action parent, route and seq1 together. Fresh reconciliation must distinguish exact bundle, pre-ready/all-absent with retained frozen result, partial/mismatch and unreadable; restart without frozen result preserves provider-unknown and performs no Provider call.

- [ ] **Step 5: Replace the self-committing executor**

Replace production `confirm_attempt()` with:

```python
def confirm_attempt_bound(
    session: Session,
    trusted_request: TrustedStoryDecision,
    authorization: ProductActionExecutionAuthorization,
) -> StoryWriteResult:
    """Flush Story/Version/Evidence/Assertion and Attempt state; never commit or close."""
```

The legacy `/confirm` endpoint is only a fixed Coordinator adapter. Byte-equivalent content is approve; edited content/evidence is modify; target CAS fields must equal the frozen route. Historical confirmed stays read-only; historical ready first creates an exact bridge with old token HMAC as request identity but executes only using the new server token/proof.
After the historical bridge tests are GREEN, delete the self-committing `confirm_attempt()` production method and all callers; no compatibility fallback may retain its transaction ownership.

- [ ] **Step 6: Implement explicit next-generation endpoint**

Add `POST /api/interview-story-proposals/{attempt_id}/product-actions` with exact expected generation fields. Use the dedicated `story_product_action_proposal_response_v1`; never mix it with the Story write result codec, implicitly execute, reuse a terminal token or advance after failed/stale target.

- [ ] **Step 7: Verify GREEN and commit**

```powershell
uv run pytest tests/test_interview_stories_repository.py tests/test_interview_stories_api.py tests/test_interview_story_product_actions.py -q
uv run ruff check src/offerpilot/repositories/interview_stories.py src/offerpilot/api.py src/offerpilot/schemas.py tests/test_interview_story_product_actions.py
uv run mypy src
git diff --check
git add src/offerpilot/repositories/interview_stories.py src/offerpilot/api.py src/offerpilot/schemas.py tests/test_interview_stories_repository.py tests/test_interview_stories_api.py tests/test_interview_story_product_actions.py
git commit -m "refactor: AI 切换经历素材产品确认链路"
```

### Task 6: Implement owner-scoped Product Action compensation and required Undo

**Files:**

- Create: `src/offerpilot/product_actions/compensation.py`
- Modify: `src/offerpilot/review_readiness/repository.py`
- Modify: `src/offerpilot/repositories/interview_stories.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/product_actions/test_compensation.py`
- Modify: `tests/test_chat_api.py`
- Modify: `tests/test_interview_story_product_actions.py`
- Modify: `tests/test_review_readiness_api.py`

- [ ] **Step 1: Write owner-proof and compensation RED tests**

Cover capability-before-query, exact application/story owner, parent action/result/undo/digest binding, Story source-attempt lineage, owner switch, ordinary/copy/cross-container/cross-owner/cross-action/ABA/reused proof rejection, deterministic compensation UUID, 20-way one executor winner, proposal/execution commit-unknown, response-loss owner route re-signing a new request-local proof before deterministic terminal replay, terminal replay zero executor and `/api/chat/undo-last-write` remaining incapable of Product Action undo.
Compensation publication must test `absent | proposed | terminal | unreadable`: only compensation parent and seq1 both absent may be reconstructed. Execution begins after parent+seq1 are durable, so either one absent or any partial state is integrity failure and must never rebuild. Exact proposed is only `[(1, proposed)]`; terminal validates full digest, request/input fingerprints and exact seq1/2/3/4. Unreadable returns unknown. Two-connection all-absent races have exactly one proposal/executor winner. Pin cross-process canonical goldens for compensation operation, request and input fingerprints. SQLite, serialization, projector and every unmapped Exception roll back the whole execution transaction and leave the Operation proposed; only a mapped domain-stale outcome may close as failed. The same execution never calls its executor twice, and Provider calls plus Agent Compensation fallback calls remain zero.

- [ ] **Step 2: Write Signal and Story domain undo RED tests**

Signal Undo appends a retracted Version with `version_number=parent+1`, `parent_version_id=<active version id>`, `disposition=retracted`, byte-copied statement/user note/source revision/three fingerprints, and every Evidence field byte-copied in ordinal order. It must generate `domain_idempotency_key=uuid5(READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE, compensation_operation_id + ':signal-retraction')` and set `write_operation_id=<compensation operation id>` rather than copying either UNIQUE identity from the active Version. Version, Evidence, Signal pointer/revision and compensation terminal commit in one transaction; Undo works after source deletion and never deletes history. New Story Undo archives and increments revision; appended Story Undo restores previous pointer/title and leaves the new Version immutable. Any later edit makes undo stale. Golden-test both `compensation_json_v1` results and the 4/1/4/12 KiB result/visible/transport/aggregate caps. Assert no正文 enters those projections and `previous_title` appears only in Story `undo_json`, never visible/transport/log/Journal/error.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/product_actions/test_compensation.py tests/test_chat_api.py tests/test_interview_story_product_actions.py tests/test_review_readiness_api.py -q
```

- [ ] **Step 4: Implement independent compensation coordinator**

Do not register these names in Agent Compensation Registry. Build owner-scoped issuers and sealed proof union, deterministic operation identity, proposal+seq1 UoW, exact prefix/digest reload, seq2/3 + one executor + domain mutation + seq4 terminal UoW, and separate proposal/execution commit-unknown rules. Product compensation keeps `adapter_kind=compensation`, can never be rejected, uses no `ProductActionProposal`, has terminal delivery `not_applicable`, and writes no Conversation/last-write/Journal row.

- [ ] **Step 5: Add only the approved owner-scoped routes**

```text
POST /api/applications/{application_id}/readiness-signals/{signal_id}/undo
POST /api/interview-stories/{story_id}/product-action-undo
```

Both accept only `{"parent_operation_id":"uuid"}`. Add route manifest tests that forbid an operation-id-only generic undo endpoint and `/api/stories/...` alias.

- [ ] **Step 6: Verify GREEN and commit**

```powershell
uv run pytest tests/product_actions/test_compensation.py tests/test_chat_api.py tests/test_interview_story_product_actions.py tests/test_review_readiness_api.py tests/tool_metadata/test_compensation_registry.py -q
uv run ruff check src/offerpilot/product_actions/compensation.py src/offerpilot/review_readiness/repository.py src/offerpilot/repositories/interview_stories.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/product_actions/compensation.py src/offerpilot/review_readiness/repository.py src/offerpilot/repositories/interview_stories.py src/offerpilot/api.py tests/product_actions/test_compensation.py tests/test_chat_api.py tests/test_interview_story_product_actions.py tests/test_review_readiness_api.py
git commit -m "feat: AI 增加产品操作受限撤销"
```

### Task 7: Add Signal source state, detail and readiness advisory APIs

**Files:**

- Create: `src/offerpilot/review_readiness/projection.py`
- Modify: `src/offerpilot/event_lifecycle.py`
- Modify: `src/offerpilot/review_readiness/candidates.py`
- Modify: `src/offerpilot/review_readiness/repository.py`
- Modify: `src/offerpilot/api.py`
- Create: `tests/test_review_readiness_projection.py`
- Modify: `tests/test_event_lifecycle_v1.py`
- Modify: `tests/test_interview_index_api.py`
- Modify: `web/src/features/interviewEvents/eventLifecycle.test.ts`

- [ ] **Step 1: Write projection RED tests**

Cover source current/changed/missing/unavailable/retracted, deleted and soft-deleted Application, exact same-Application target rules, every EventLifecycle status alias, source==target, no time inference, practiced only for exact completed pair, read exception returning unavailable rather than empty, safe cross-scope 404 and fingerprint ordering with one/five Evidence rows. Query Evidence in ordinal order even if the DB return is shuffled; missing ordinal 0, any ordinal gap, a sixth row or any changed Evidence hash corrupts the aggregate and makes advisory/detail unavailable.
`tests/test_event_lifecycle_v1.py` must load `tests/fixtures/review_readiness/event_lifecycle_v1.json`; the existing frontend `eventLifecycle.test.ts` must load the same fixture and prove byte-for-byte agreement of every alias and unknown fallback.
Add a strict orthogonality assertion: zero, current, stale, retracted or unreadable Signals never change baseline `InterviewReadinessResult.ready`; it remains a function only of Application/Event/JD/Resume.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_event_lifecycle_v1.py tests/test_review_readiness_projection.py tests/test_interview_index_api.py -q
```

- [ ] **Step 3: Implement one canonical aggregate loader and fingerprints**

Compute `practice_source_fingerprint_v1` from Signal/Version plus all Evidence ordered by ordinal; compute `practice_target_fingerprint_v1` from the exact authoritative Event. Expose only bounded statement/user note/evidence/detail labels and safe hashes; never return Note/Proposal snapshots, Operation or token.
Reuse the Task 3 `classify_event_lifecycle_v1(status: object)` unchanged. Add mechanical import/AST assertions that Candidate, advisory, Practice and Preparation all import it and contain no local status classifier or date-based lifecycle inference.

- [ ] **Step 4: Add the four read APIs**

Implement candidates, event advisory, Signal detail and exact practice focus paths from the design. Same loader and lifecycle truth table must serve advisory, practice and preparation validation. Read errors become explicit unavailable/503; missing/cross-scope are indistinguishable 404.

- [ ] **Step 5: Verify GREEN and commit**

```powershell
uv run pytest tests/test_event_lifecycle_v1.py tests/test_review_readiness_projection.py tests/test_interview_index_api.py tests/test_review_readiness_api.py -q
uv run ruff check src/offerpilot/event_lifecycle.py src/offerpilot/review_readiness/projection.py src/offerpilot/review_readiness/repository.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/event_lifecycle.py src/offerpilot/review_readiness/candidates.py src/offerpilot/review_readiness/projection.py src/offerpilot/review_readiness/repository.py src/offerpilot/api.py tests/fixtures/review_readiness/event_lifecycle_v1.json tests/test_event_lifecycle_v1.py tests/test_review_readiness_projection.py tests/test_interview_index_api.py tests/test_review_readiness_api.py web/src/features/interviewEvents/eventLifecycle.test.ts
git commit -m "feat: AI 投影复盘准备状态"
```

### Task 8: Replace new Adaptive Practice creation with V2 exact pair binding

**Files:**

- Modify: `src/offerpilot/repositories/adaptive_interview_practice.py`
- Modify: `src/offerpilot/schemas.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/test_adaptive_interview_practice_repository.py`
- Modify: `tests/test_adaptive_interview_practice_api.py`

- [ ] **Step 1: Write V2 RED matrix**

Cover exact Signal Version + target Event, same Application/interview/scheduled-or-in-progress/completed source/source!=target; both canonical fingerprint goldens; ordinal 0 snapshot selection; all Evidence affecting source fingerprint; same key/same stored input live query 0 after changed/missing/retracted/completed; same key/different body conflict; same pair one winner; same Signal/different targets; source/target/both FK delete history; V1 replay/complete and V1 new create 410. A shuffled Evidence query is re-ordered by ordinal; missing ordinal 0, any ordinal gap, a sixth row or any changed hash fails closed with Plan writes=0.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_adaptive_interview_practice_repository.py tests/test_adaptive_interview_practice_api.py -q
```

- [ ] **Step 3: Implement idempotency-first V2 start**

Decode only:

```json
{"readiness_signal_version_id":91,"target_application_event_id":103,"expected_source_fingerprint":"sha256:...","expected_target_fingerprint":"sha256:...","idempotency_key":"uuid"}
```

After `BEGIN IMMEDIATE`, query the idempotency key before any live source/target Repository call. Exact existing input replays the frozen Plan; different input conflicts; absent key invokes the shared canonical loaders and inserts `confirmed_readiness_signal_v1` with both fingerprints and ordinal-0 legacy snapshot.

Persist the identities without overloading the legacy column:

```text
origin_contract=confirmed_readiness_signal_v1
application_event_id=signal.source_event_id
target_application_event_id=request.target_application_event_id
readiness_signal_version_id=request.readiness_signal_version_id
```

Copy the legacy source Note/Proposal/focus/snapshot fields from the confirmed Version for immutable display. A source≠target test must prove the two Event columns cannot be swapped; start remains Provider=0.

- [ ] **Step 4: Remove unconfirmed Proposal recommendation creation**

`list_recommendations` may expose compatibility history but cannot create or recommend a new legacy plan. List/get branch by `origin_contract` and retain in-progress/completed V2 rows after locators become null. Completion changes only Plan state and never Signal/Memory/Knowledge.
Delete the legacy create implementation after its 410/replay/complete tests are GREEN; preserve only historical read/replay/complete branches.

- [ ] **Step 5: Verify GREEN and commit**

```powershell
uv run pytest tests/test_adaptive_interview_practice_repository.py tests/test_adaptive_interview_practice_api.py tests/test_review_readiness_projection.py -q
uv run ruff check src/offerpilot/repositories/adaptive_interview_practice.py src/offerpilot/schemas.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/repositories/adaptive_interview_practice.py src/offerpilot/schemas.py src/offerpilot/api.py tests/test_adaptive_interview_practice_repository.py tests/test_adaptive_interview_practice_api.py
git commit -m "feat: AI 绑定复盘信号与目标面试练习"
```

### Task 9: Add Interview Preparation Input V2 explicit readiness selection

**Files:**

- Create: `src/offerpilot/review_readiness/preparation_selection.py`
- Modify: `src/offerpilot/repositories/interview_preparation_proposals.py`
- Modify: `src/offerpilot/ai/interview_preparation_proposals.py`
- Modify: `src/offerpilot/schemas.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/test_interview_preparation_repository.py`
- Modify: `tests/test_interview_preparation_ai.py`
- Modify: `tests/test_interview_preparation_api.py`

- [ ] **Step 1: Write V1-byte-equivalence and raw-presence RED tests**

Compare the absent-field V1 snapshot bytes/request fingerprint/input fingerprint against the pinned `interview_preparation_v1_c5a020c.json`; explicit `[]` must create V2; absent versus empty with same key conflicts; null/non-array/bool/int confusion/duplicates/9 items return 422. Unknown/replay must restore the frozen presence bit, not current UI defaults. Explicit `[]` performs zero application-wide Signal aggregate queries and freezes an empty `readiness_feedback` provider input.

- [ ] **Step 2: Write selection/lease/budget RED tests**

Cover 0/1/8 items, order, cross-app, stale/retracted/missing, target completed/cancelled/unknown/wrong type, source==target, same read snapshot as Event/JD/Resume, Provider fallback frozen input, lifecycle drift during Provider and late-result discard. Build exact final-wrapper fixtures for 65,536 bytes and 65,537 bytes using escaped quotes, backslashes, Chinese and emoji. Add raw-body tests for duplicate `readiness_feedback_version_ids` including absent/empty ambiguity, duplicate unrelated keys, non-object top level and NaN/Infinity.

- [ ] **Step 3: Verify RED**

```powershell
uv run pytest tests/test_interview_preparation_repository.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_api.py -q
```

- [ ] **Step 4: Preserve raw presence and V1 builder**

Decode raw request bytes with a duplicate-key-aware object hook before Pydantic/default handling, then freeze `readiness_feedback_version_ids_present` from own-key presence. When false, call the physically isolated untouched V1 snapshot builder with no import/call into the V2 builder and no additional envelope key. When true, validate an ordered tuple and include `readiness_feedback_selection={present:true,ordered_version_ids:[...]}` in the V2 request/input fingerprint.

- [ ] **Step 5: Implement Session-bound selection loader and V2 input**

Load exact selected Versions with Event/JD/Resume in the same read UoW; validate current/active/source-current/completed source/same application/exact scheduled-or-in-progress interview target/source!=target. After rollback, canonicalize the bounded untrusted `readiness_feedback` envelope, persist selected IDs/presence/fingerprint in Attempt input, and use the same frozen input for Provider fallback and final lease/CAS checks.

- [ ] **Step 6: Extend output evidence safely**

Allow `confirmed_readiness_feedback` refs only to paths in this attempt's frozen provider input. User note is labeled context, not supporting external evidence. Any mandatory-chain or aggregate-budget failure occurs before Provider call and never truncates fields.

- [ ] **Step 7: Verify GREEN and commit**

```powershell
uv run pytest tests/test_interview_preparation_repository.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_api.py -q
uv run ruff check src/offerpilot/review_readiness/preparation_selection.py src/offerpilot/repositories/interview_preparation_proposals.py src/offerpilot/ai/interview_preparation_proposals.py src/offerpilot/schemas.py src/offerpilot/api.py
uv run mypy src
git diff --check
git add src/offerpilot/review_readiness/preparation_selection.py src/offerpilot/repositories/interview_preparation_proposals.py src/offerpilot/ai/interview_preparation_proposals.py src/offerpilot/schemas.py src/offerpilot/api.py tests/test_interview_preparation_repository.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_api.py
git commit -m "feat: AI 接入显式复盘准备输入"
```

### Task 10: Add future-only contributor assets and production-unreachable gates

**Files:**

- Create: `src/offerpilot/review_readiness/contributor.py`
- Modify: `tests/test_context_projector.py`
- Create: `tests/test_review_readiness_context_gate.py`
- Modify: `tests/test_pilot_runtime_extraction_gate.py`

- [ ] **Step 1: Write production-unreachable RED tests**

Assert all production Agent paths keep `confirmed_memory`, `knowledge_context`, and `older_conversation_summary` disabled with Signal query 0; future port is absent from composition/selector/runner call graph; a normal `ContributorResult(status='ready')` cannot enable it; SurfaceManifestV2 and Journal Manifest bytes do not change; ordinary Chat/Application Chat/Haru never import the selection loader.

- [ ] **Step 2: Verify RED for missing future type asset only**

```powershell
uv run pytest tests/test_context_projector.py tests/test_review_readiness_context_gate.py tests/test_pilot_runtime_extraction_gate.py -q
```

- [ ] **Step 3: Add a sealed validator-only type asset**

`ConfirmedReadinessContributorPort` validates only synthetic ordered version IDs, application/target/resume identities and selection fingerprint. It exposes no production proof constructor and is not registered or imported by composition, Projector or Runner. `PreparationReadinessSelectionLoader` remains reachable only from Interview Preparation owner.

- [ ] **Step 4: Verify GREEN and commit**

```powershell
uv run pytest tests/test_context_projector.py tests/test_review_readiness_context_gate.py tests/test_pilot_runtime_extraction_gate.py -q
uv run ruff check src/offerpilot/review_readiness/contributor.py tests/test_review_readiness_context_gate.py
uv run mypy src
git diff --check
git add src/offerpilot/review_readiness/contributor.py tests/test_context_projector.py tests/test_review_readiness_context_gate.py tests/test_pilot_runtime_extraction_gate.py
git commit -m "test: AI 封闭复盘信号上下文边界"
```

### Task 11: Integrate the four canonical Core Task owners

**Files:**

- Create: `web/src/features/reviewReadiness/contracts.ts`
- Create: `web/src/features/reviewReadiness/service.ts`
- Create: `web/src/features/reviewReadiness/ProductActionConfirmation.tsx`
- Create: `web/src/features/reviewReadiness/ReviewReadinessNextStep.tsx`
- Create: `web/src/features/reviewReadiness/ReadinessFeedbackAdvisory.tsx`
- Create: `web/src/features/reviewReadiness/reviewReadiness.module.css`
- Create: `web/src/features/reviewReadiness/*.test.tsx`
- Create: `web/src/features/reviewReadiness/service.test.ts`
- Modify: `web/src/types/note.ts`
- Modify: `web/src/types/interviewReviewProposal.ts`
- Modify: `web/src/types/interviewStory.ts`
- Modify: `web/src/types/adaptiveInterviewPractice.ts`
- Modify: `web/src/types/interviewPreparationProposal.ts`
- Modify: `web/src/services/interviewStories.ts`
- Modify: `web/src/services/adaptiveInterviewPractice.ts`
- Modify: `web/src/services/interviewPreparationProposals.ts`
- Modify: `web/src/components/InterviewReviewProposalDrawer.tsx`
- Modify: `web/src/components/InterviewStoryDrawer.tsx`
- Modify: `web/src/components/AdaptiveInterviewPracticeWorkspace.tsx`
- Modify: `web/src/components/InterviewPreparationProposalDrawer.tsx`
- Modify: `web/src/components/InterviewV01View.tsx`
- Modify: `web/src/components/QuestionBankView.tsx`
- Modify: `web/src/layout/AppShell.tsx`

- [ ] **Step 1: Write services and state-machine RED tests**

Freeze every URL/body/response union, token presence rule and no-extra-field behavior. Test Signal duplicate click convergence; approve/modify/reject/unknown/replay/undo; Story server-token use, explicit N+1 after reject and no token on terminal replay; Practice exact Signal+target draft isolation; Preparation omitted versus explicit-empty field.

- [ ] **Step 2: Write canonical-owner UI RED tests**

Review shows one primary “保存为下次准备重点” and secondary Story opener only for ready safe V2 focus. Product Action confirmation stays inside review/story owner, never Chat Pending or a second modal owner. Preparation defaults to zero selected and caps at eight. Practice requires an explicit target Event and never guesses nearest/only Event. Haru/Pilot only opens exact Core Task and performs zero Signal query/token/draft mutations.

- [ ] **Step 3: Verify RED**

```powershell
cd web
npm test -- --run src/features/reviewReadiness src/services/interviewStories.test.ts src/components/InterviewReviewProposalDrawer.interaction.test.tsx src/components/InterviewStoryDrawer.interaction.test.tsx src/components/AdaptiveInterviewPracticeWorkspace.test.tsx src/components/QuestionBankView.test.tsx src/components/InterviewV01View.adaptivePractice.test.tsx src/components/InterviewPreparationProposalDrawer.interaction.test.tsx src/features/coreTaskSurface
cd ..
```

- [ ] **Step 4: Implement typed services and confirmation reducer**

Keep operation ID, action call ID, server token, original decision payload and result-unknown state in owner-generation scoped drafts. Delete the client-generated Story authorization token fallback. Generic safe GET never upgrades to token; only source-bound owner recovery does. Terminal replay clears token but preserves safe result and undo eligibility.
Keep the existing `CoreTaskId` union and top-level navigation unchanged; review feedback/question bank/quick practice are modes inside the existing owner, not new tasks or routes.

- [ ] **Step 5: Render accessible owner components**

Use `aria-busy`, `role=alert`, bounded `aria-live`, non-color-only status, 44px primary controls, heading focus/return focus and reduced-motion CSS. Verify 768/1024/1280/1440 desktop widths and light/dark themes. Replacement/draft guards must preserve unknown operations and prevent duplicate API/Provider/SSE calls.

- [ ] **Step 6: Verify GREEN and commit**

```powershell
cd web
npm test -- --run src/features/reviewReadiness src/services/interviewStories.test.ts src/components/InterviewReviewProposalDrawer.interaction.test.tsx src/components/InterviewStoryDrawer.interaction.test.tsx src/components/AdaptiveInterviewPracticeWorkspace.test.tsx src/components/QuestionBankView.test.tsx src/components/InterviewV01View.adaptivePractice.test.tsx src/components/InterviewPreparationProposalDrawer.interaction.test.tsx src/features/coreTaskSurface
npm run build
cd ..
git diff --check
git add web/src/features/reviewReadiness web/src/types web/src/services/interviewStories.ts web/src/services/adaptiveInterviewPractice.ts web/src/services/interviewPreparationProposals.ts web/src/components/InterviewReviewProposalDrawer.tsx web/src/components/InterviewStoryDrawer.tsx web/src/components/AdaptiveInterviewPracticeWorkspace.tsx web/src/components/InterviewPreparationProposalDrawer.tsx web/src/components/InterviewV01View.tsx web/src/components/QuestionBankView.tsx web/src/layout/AppShell.tsx
git commit -m "feat: AI 接通复盘到下次面试准备"
```

### Task 12: Delete old paths and close AST, manifest and privacy gates

**Files:**

- Modify: `tests/test_review_to_readiness_source_gates.py`
- Modify: `web/src/features/reviewReadiness/reviewReadinessGate.test.ts`
- Create: `web/src/features/reviewReadiness/reviewReadinessNegativeFixtures.test.ts`
- Modify: `tests/test_pilot_runtime_extraction_gate.py`
- Modify: `src/offerpilot/smoke.py`

- [ ] **Step 1: Add negative fixtures for every forbidden path**

Detect Product Action imports in Provider/Tool selector/dispatcher/Legacy/Agent Compensation; Chat/Pending/Message/AgentRun/Journal writes; parent-only Product Action inserts; unconfirmed Proposal Practice creation; old self-committing Story confirm; client-generated Story token; generic operation-id undo; `/api/stories` alias; Signal query from Chat/Haru; registered/ready `confirmed_memory`; direct review UI domain CRUD; target inference; Preparation V1 importing/calling the V2 builder; new `CoreTaskId`/top-level navigation; and sensitive canary in Snapshot, Event, Journal, Manifest, HTTP error, terminal result/visible/transport/undo, sessionStorage and release report. Permit `previous_title` only in Story `undo_json`.

- [ ] **Step 2: Run gates and verify any remaining RED violations**

```powershell
uv run pytest tests/test_review_to_readiness_baseline.py tests/test_review_to_readiness_source_gates.py tests/test_pilot_runtime_extraction_gate.py -q
cd web
npm test -- --run src/features/reviewReadiness/reviewReadinessGate.test.ts src/features/reviewReadiness/reviewReadinessNegativeFixtures.test.ts
cd ..
```

- [ ] **Step 3: Delete forbidden production paths**

Verify the owning Tasks already removed the production `confirm_attempt()` method/callers, new V1 Practice creation and client token generation. Task 12 does not patch production cleanup opportunistically: if a gate remains RED, return to the corresponding owning Task, add a focused failing test, fix and commit every actual production file there, then rerun this gate. Keep historical V1 read/replay/complete and historical confirmed Story replay, but add no fallback facade, feature flag, shadow write, second registry or alias.

- [ ] **Step 4: Update smoke to the server-token/Product Action flow**

Smoke must obtain the Story token from ready response, exercise one decision, and keep Product Action Provider calls at zero. Do not fabricate a client token or count Product Actions inside the 25-tool surface.

- [ ] **Step 5: Verify all gates GREEN and commit**

```powershell
uv run pytest tests/test_review_to_readiness_baseline.py tests/test_review_to_readiness_source_gates.py tests/test_pilot_runtime_extraction_gate.py -q
cd web
npm test -- --run src/features/reviewReadiness/reviewReadinessGate.test.ts src/features/reviewReadiness/reviewReadinessNegativeFixtures.test.ts
cd ..
git diff --check
git status --short
git add tests/test_review_to_readiness_source_gates.py web/src/features/reviewReadiness/reviewReadinessGate.test.ts web/src/features/reviewReadiness/reviewReadinessNegativeFixtures.test.ts tests/test_pilot_runtime_extraction_gate.py src/offerpilot/smoke.py
git commit -m "test: AI 封闭旧复盘准备执行路径"
```

### Task 13: Full verification, browser acceptance, independent CR and release report

**Files:**

- Modify: `docs/superpowers/plans/2026-08-30-review-to-readiness-feedback-loop.md`
- Create: `docs/superpowers/reports/2026-08-30-review-to-readiness-feedback-loop-verification.md`

- [ ] **Step 1: Run focused contract suites**

```powershell
uv run pytest tests/test_review_to_readiness_migration_0029.py tests/product_actions tests/test_review_readiness_repository.py tests/test_review_readiness_candidates.py tests/test_review_readiness_projection.py tests/test_review_readiness_api.py tests/test_interview_story_product_actions.py tests/test_adaptive_interview_practice_repository.py tests/test_adaptive_interview_practice_api.py tests/test_interview_preparation_repository.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_api.py tests/test_review_readiness_context_gate.py -q
```

```powershell
cd web
npm test -- --run src/features/reviewReadiness src/components/InterviewReviewProposalDrawer.interaction.test.tsx src/components/InterviewStoryDrawer.interaction.test.tsx src/components/InterviewV01View.adaptivePractice.test.tsx src/components/InterviewPreparationProposalDrawer.interaction.test.tsx src/features/coreTaskSurface
cd ..
```

Record exact counts and exit codes.

- [ ] **Step 2: Run the full supported release gate with fresh output**

Run separately:

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
cd web
npm test -- --run
npm run build
cd ..
uv run oc smoke --static-dir web/dist
uv run oc verify --profile local --static-dir web/dist
```

Run the existing controlled real-AI gate only if configured and explicitly supported by local credentials. Record Docker, external release orchestrator or Application-JD gate as excluded when unavailable; do not synthesize inputs.

- [ ] **Step 3: Run real browser acceptance with the built-in Codex browser**

On isolated local data, verify Note revision and Proposal V2; Signal approve/modify/reject/unknown/replay/undo; Story approve/modify/reject/N+1/unknown/undo; exact target Practice start/complete; Preparation V2 zero/one/eight selection and source drift; source missing; keyboard/focus; light/dark; reduced motion; 768/1024/1280/1440. Inspect network and logs for duplicate HTTP/SSE/Provider/executor calls and prove Product Action Provider=0.

- [ ] **Step 4: Request independent spec and code-quality review**

Give fresh GPT-5.6 Sol xhigh reviewers the approved design, this plan, `BASE_SHA=c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff`, current HEAD, exact diff and verification evidence. Close every P0/P1/P2 under TDD and rerun affected focused and full gates; request re-review until no P0/P1/P2 remains.

- [ ] **Step 5: Write the release report**

The report records: 0029 and destructive internal rebuild; exact `25/3/4 + 2/2`; Story confirm internal cutover and historical compatibility; V1/V2 coexistence; Signal fact level and no Memory/Knowledge/Summary writes; commit-unknown and Undo matrices; privacy scan; browser evidence; external exclusions; remaining risks; final reviewer verdict; and no claim of global exactly-once.

- [ ] **Step 6: Run final repository hygiene checks**

```powershell
git diff --check
git status --short --branch
git diff --name-only c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff..HEAD
git ls-files --others --exclude-standard
```

Every changed/untracked file must belong to the approved project and the worktree must be clean after the final commit.

- [ ] **Step 7: Commit the verified report and checked plan**

```powershell
git add docs/superpowers/plans/2026-08-30-review-to-readiness-feedback-loop.md
git add -f docs/superpowers/reports/2026-08-30-review-to-readiness-feedback-loop-verification.md
git commit -m "docs: AI 记录复盘准备闭环验收"
```

Do not push or merge. Hand back only with fresh verification evidence, independent CR with no open P0/P1/P2, explicit exclusions/risks, final commit SHA and a clean isolated worktree.
