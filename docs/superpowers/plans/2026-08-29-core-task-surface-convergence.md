# Core Task Surface Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Application、Interview、Materials & Resume 三组桌面任务收敛为封闭的单 owner Surface，同时保持既有 HTTP、SSE、HITL、Ledger、Provider 与 Tool 副作用合同。

**Architecture:** 以纯类型 `CoreTaskRef`、封闭 registry 和 generation-safe controller 作为唯一入口层；Application、Interview 和 Materials 各自通过纯 resolver/projector 生成用户状态，再由 `AppShell` 作为唯一 composition root 挂载一个 active owner。后端只修复 Interview index 的只读投影与分页查询，不增加数据库字段、migration、Agent Tool、Provider 调用或后台队列。

**Tech Stack:** React 18、TypeScript 5.6、Vitest、TanStack Query、Ant Design、FastAPI、SQLAlchemy 2、Pytest、Ruff、Mypy。

---

## File map and ownership

高冲突组合文件只由主集成人串行修改：

- `web/src/layout/AppShell.tsx`：创建 controller、接收 canonical launch、挂载唯一 host。
- `web/src/components/ApplicationDetail.tsx`：投递详情三段布局、任务概览与 record-management 边界。
- `web/src/components/InterviewV01View.tsx`：Event card 分桶、事件选择器与唯一自由练习入口。

可独立实现并在每批后回归的边界：

- `web/src/features/coreTaskSurface/**`：Task contracts、registry、controller、host、机械门禁。
- `web/src/features/applicationTasks/**`：ApplicationTaskResolver 与 Opportunity Fit history adapter。
- `web/src/features/interviewEvents/**`：Interview read normalizer、EventLifecycleV1、Event card projector、ResumeSelectionLease。
- `web/src/features/materialSurfaces/**`：Material Kit 状态、经历/参考资料分类、Resume lineage 与统一文案。
- `src/offerpilot/repositories/interview_index.py`、`src/offerpilot/api.py`：Interview index 的唯一 Event row 和只读字段。
- `tests/fixtures/core_task_surface/**`：固定 baseline 的只读资产，生产代码禁止读取。

所有阶段遵守：先写一个可观察行为的失败测试并确认失败原因，再写最小生产实现；每个 commit 前运行本任务聚焦测试和 `git diff --check`。`git add` 与 `git commit` 分开执行，提交标题使用中文。

### Task 0: Pin the approved baseline and create RED mechanical gates

**Files:**

- Create: `tests/fixtures/core_task_surface/core_task_entrypoints_93fb006.json`
- Create: `tests/fixtures/core_task_surface/core_task_request_counts_93fb006.json`
- Create: `tests/fixtures/core_task_surface/core_task_visible_copy_93fb006.json`
- Create: `tests/fixtures/core_task_surface/interview_index_api_93fb006.json`
- Create: `tests/test_core_task_surface_assets.py`
- Create: `web/src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts`

- [x] **Step 1: Capture the baseline from the fixed commit without modifying it**

Use `git show 93fb006:<path>` and `git grep` against `93fb006` to enumerate every audited entrypoint, visible internal lexeme and request side effect. Store manually reviewed JSON with the exact common envelope:

```json
{
  "schema_version": 1,
  "source_baseline": "93fb0063118761f2c76e71e4209000feee0f755b",
  "items": []
}
```

The entrypoint item shape is `{ "file", "qualified_symbol", "category", "task_id" }`; `category` is exactly `core_task | navigation_only | record_management`, and `task_id` is null outside `core_task`. The request-count item shape is `{ "flow", "http_reads", "http_mutations", "provider_calls", "tool_executor_calls", "sse_subscriptions", "domain_writes" }`. The visible-copy item shape is `{ "file", "lexeme", "replacement" }`. The Interview golden stores `list` and `get` payloads exactly as returned at the baseline.

- [x] **Step 2: Write immutable-asset and RED source gates**

`tests/test_core_task_surface_assets.py` must assert the pinned baseline, exact top-level keys, unique entries, closed categories, and SHA-256 constants written directly into the test. It must also assert that no file below `src/offerpilot` imports or reads these review-only assets.

`coreTaskSurfaceGate.test.ts` must read the same assets and initially fail because `contracts.ts`, `registry.ts`, controller owner declarations, centralized event classifier and centralized material label mapper do not yet exist. The gate must report named violations such as `registry:missing-owner`, `entrypoint:unclassified`, `event:local-classifier`, and `copy:forbidden-lexeme`; it must never rewrite fixtures.

- [x] **Step 3: Run the gates and verify RED for missing convergence code**

Run:

```powershell
uv run pytest tests/test_core_task_surface_assets.py -q
```

Expected: PASS for asset integrity.

Run from `web`:

```powershell
npm test -- --run src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts
```

Expected: FAIL with the first named missing-contract/owner violation, not a JSON parse or path error.

- [x] **Step 4: Commit the reviewed fixtures and RED gates**

```powershell
git add -f tests/fixtures/core_task_surface tests/test_core_task_surface_assets.py web/src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts
git commit -m "test: AI 固化核心任务界面基线门禁"
```

### Task 1: Implement closed CoreTask contracts and registry

**Files:**

- Create: `web/src/features/coreTaskSurface/contracts.ts`
- Create: `web/src/features/coreTaskSurface/contracts.test.ts`
- Create: `web/src/features/coreTaskSurface/registry.ts`
- Create: `web/src/features/coreTaskSurface/registry.test.ts`
- Modify: `web/src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts`

- [x] **Step 1: Write failing contract tests**

Cover all eleven task IDs; exact required/forbidden identities; positive integer validation; hint/source exclusion from canonical key; unknown task; invalid identity; and entry categories. The wished-for API is:

```ts
const parsed = parseCoreTaskRef({
  taskId: 'application.interview_review',
  applicationId: 7,
  eventId: 9,
});
expect(parsed).toEqual({ ok: true, ref: expect.any(Object), key: 'application.interview_review:applicationId=7:eventId=9' });
expect(parseCoreTaskRef({ taskId: 'application.material_kit', applicationId: 7, resumeId: 2 }))
  .toEqual({ ok: false, reason: 'invalid_task_identity' });
```

Registry tests must prove `CORE_TASK_IDS` and registry keys are equal, each task has exactly one owner ID, and unregistered tasks return `task_owner_unavailable` without fallback.

- [x] **Step 2: Run focused tests and verify RED**

Run from `web`:

```powershell
npm test -- --run src/features/coreTaskSurface/contracts.test.ts src/features/coreTaskSurface/registry.test.ts
```

Expected: FAIL because the modules and exported functions do not exist.

- [x] **Step 3: Implement the minimal closed contracts**

Use these exported contracts:

```ts
export type CoreTaskId =
  | 'application.opportunity_fit'
  | 'application.material_kit'
  | 'application.interview_prepare'
  | 'application.interview_review'
  | 'application.general_review'
  | 'application.offer_review'
  | 'application.record_outcome'
  | 'interview.free_practice'
  | 'materials.resume'
  | 'materials.story'
  | 'materials.reference';

export type CoreTaskEntrypointCategory = 'core_task' | 'navigation_only' | 'record_management';
export interface CoreTaskRef { taskId: CoreTaskId; applicationId?: number; eventId?: number; offerId?: number; resumeId?: number; storyId?: number; sourceId?: number }
export interface TaskLaunchRequest { ref: CoreTaskRef; source: TaskLaunchSource; focus?: TaskLaunchFocus; hints?: Readonly<TaskLaunchHints> }
export function parseCoreTaskRef(input: unknown): CoreTaskParseResult;
export function coreTaskCanonicalKey(ref: CoreTaskRef): string;
```

`registry.ts` contains one frozen record keyed by `CoreTaskId`; owner IDs are stable user-interface identifiers and do not import services, repositories or Assistant controllers.

- [x] **Step 4: Run focused tests and the asset gate**

Run from `web`:

```powershell
npm test -- --run src/features/coreTaskSurface/contracts.test.ts src/features/coreTaskSurface/registry.test.ts src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts
```

Expected: contracts and registry tests PASS; the wider gate remains RED only for later owner/event/material requirements.

- [x] **Step 5: Commit contracts and registry**

```powershell
git add web/src/features/coreTaskSurface
git commit -m "feat: AI 建立核心任务封闭注册表"
```

### Task 2: Implement generation-safe CoreTaskSurfaceController

**Files:**

- Create: `web/src/features/coreTaskSurface/controller.ts`
- Create: `web/src/features/coreTaskSurface/controller.test.ts`
- Create: `web/src/features/coreTaskSurface/CoreTaskSurfaceHost.tsx`
- Create: `web/src/features/coreTaskSurface/CoreTaskSurfaceHost.test.tsx`
- Create: `web/src/features/coreTaskSurface/CoreTaskSurfaceHost.module.css`

- [x] **Step 1: Write failing controller and Strict Mode tests**

Test transitions `closed -> opening -> open -> closing -> closed`, duplicate launch, source/focus/hint changes on the same canonical key, generation replacement, stale close/focus no-op, pending guard denial, and a different key replacing the active owner only after guard approval. Spy functions for Provider, Tool executor, Chat mutation, SSE subscription and domain writes must remain at zero for launch/focus/close.

```ts
const first = controller.launch(request('application.material_kit', 7));
const duplicate = controller.launch(request('application.material_kit', 7, { source: 'pilot' }));
expect(duplicate.kind).toBe('focused_existing');
expect(duplicate.generation).toBe(first.generation);
expect(sideEffects).toEqual({ provider: 0, tool: 0, chat: 0, sse: 0, write: 0 });
```

Render `CoreTaskSurfaceHost` under `StrictMode`; two equivalent launch effects must mount one `data-core-task-owner` and preserve a typed input draft.

- [x] **Step 2: Run focused tests and verify RED**

Run from `web`:

```powershell
npm test -- --run src/features/coreTaskSurface/controller.test.ts src/features/coreTaskSurface/CoreTaskSurfaceHost.test.tsx
```

Expected: FAIL because the controller and host are absent.

- [x] **Step 3: Implement a pure reducer-backed controller and accessible host**

Export:

```ts
export interface CoreTaskSurfaceState { phase: 'closed' | 'opening' | 'open' | 'closing'; generation: number; active: ActiveCoreTask | null }
export interface CoreTaskSurfaceController {
  getState(): CoreTaskSurfaceState;
  subscribe(listener: () => void): () => void;
  launch(request: TaskLaunchRequest): CoreTaskLaunchResult;
  markOpen(generation: number): void;
  close(generation: number): void;
  markClosed(generation: number): void;
}
export function createCoreTaskSurfaceController(options: CoreTaskControllerOptions): CoreTaskSurfaceController;
```

The host uses `useSyncExternalStore`, a single heading, one owner container and one focus return target. It neither fetches nor imports `services/chat`; duplicate focus uses the existing DOM owner and does not reset children.

- [x] **Step 4: Verify controller and host GREEN**

Run the two focused files again. Expected: PASS with zero-side-effect and Strict Mode assertions.

- [x] **Step 5: Commit controller and host**

```powershell
git add web/src/features/coreTaskSurface
git commit -m "feat: AI 建立单实例任务界面控制器"
```

### Task 3: Fix Interview index uniqueness and additive read contract

**Files:**

- Modify: `src/offerpilot/repositories/interview_index.py`
- Modify: `src/offerpilot/api.py`
- Modify: `tests/test_interview_index_api.py`

- [x] **Step 1: Add failing API tests for latest Note, pagination and fields**

Add cases for two Notes with distinct `created_at`; equal timestamps resolved by higher Note ID; `list()` and `get()` equality; one Event row before pagination; application-level Note exclusion; deleted Application; scheduled and unscheduled events; status aliases; duration boundaries `1` and `10080`; and invalid stored durations `None/0/-1/10081`.

Expected item keys include:

```python
assert item == {
    **legacy_item,
    "event_status": "todo",
    "duration_minutes": 60,
    "scheduled_at_state": "present",
    "preparation_available": True,
}
```

Unscheduled legacy `scheduled_at` remains the serialized year-one sentinel while `scheduled_at_state == "absent"` and `preparation_available is False`.

- [x] **Step 2: Run backend tests and verify RED**

```powershell
uv run pytest tests/test_interview_index_api.py -q
```

Expected: FAIL on duplicate rows, nondeterministic Note selection and missing read fields.

- [x] **Step 3: Implement one shared unique-Event statement**

Create `_event_index_statement()` using a `row_number()` window partitioned by `InterviewNote.application_event_id`, ordered by `created_at DESC, id DESC`, and restricted to `rn == 1` before applying Event offset/limit. Both `list()` and `get()` consume this statement.

Extend `InterviewIndexItem` with:

```python
event_status: str
duration_minutes: int | None
scheduled_at_state: str
```

Set `scheduled_at_state` from the same `ApplicationEvent` row. `preparation_available` is true only when status is one of `todo/pending/scheduled/in_progress`, schedule is present and `type(duration) is int and 1 <= duration <= 10080`. Do not modify models, migrations, Event CRUD payloads or write values.

- [x] **Step 4: Verify backend GREEN and contract serialization**

```powershell
uv run pytest tests/test_interview_index_api.py -q
uv run mypy src
uv run ruff check src/offerpilot/repositories/interview_index.py src/offerpilot/api.py tests/test_interview_index_api.py
```

Expected: all three commands exit zero.

- [x] **Step 5: Commit the controlled read-contract change**

```powershell
git add src/offerpilot/repositories/interview_index.py src/offerpilot/api.py tests/test_interview_index_api.py
git commit -m "fix: AI 修复面试索引唯一事件投影"
```

### Task 4: Implement Interview read normalization and EventLifecycleV1

**Files:**

- Modify: `web/src/types/interviewIndex.ts`
- Create: `web/src/features/interviewEvents/interviewIndexContract.ts`
- Create: `web/src/features/interviewEvents/interviewIndexContract.test.ts`
- Create: `web/src/features/interviewEvents/eventLifecycle.ts`
- Create: `web/src/features/interviewEvents/eventLifecycle.test.ts`
- Create: `web/src/features/interviewEvents/interviewEventCard.ts`
- Create: `web/src/features/interviewEvents/interviewEventCard.test.ts`

- [x] **Step 1: Write the complete failing truth-table tests**

Cover missing/unknown/non-string state; absent/empty/sentinel/invalid/valid RFC3339 schedule; duration missing/null/bool/fraction/zero/negative/oversize; unknown status; source mismatch; future done; future cancelled; past todo; current and expired in-progress; exact end boundary; stable same-time ID ordering.

```ts
expect(projectInterviewEventCard(normalizeInterviewIndexItem(rawPastTodo), now)).toMatchObject({
  lifecycle: 'scheduled',
  bucket: 'needs_status_update',
  primaryAction: 'update_status',
});
```

- [x] **Step 2: Run focused tests and verify RED**

```powershell
cd web
npm test -- --run src/features/interviewEvents/interviewIndexContract.test.ts src/features/interviewEvents/eventLifecycle.test.ts src/features/interviewEvents/interviewEventCard.test.ts
```

Expected: FAIL because the normalizer/projectors do not exist.

- [x] **Step 3: Implement the closed normalizer and classifier**

Export these stable contracts:

```ts
export type EventLifecycleV1 = 'scheduled' | 'in_progress' | 'completed' | 'cancelled' | 'unknown';
export type InterviewEventBucket = 'upcoming' | 'completed' | 'cancelled' | 'needs_status_update' | 'unavailable';
export type InterviewContractReason = 'schedule_absent' | 'schedule_invalid' | 'contract_field_missing' | 'contract_field_invalid' | 'duration_invalid' | 'status_unknown' | 'source_mismatch';
export function normalizeInterviewIndexItem(input: unknown): NormalizedInterviewIndexItem;
export function classifyEventLifecycle(status: unknown): EventLifecycleV1;
export function projectInterviewEventCard(item: NormalizedInterviewIndexItem, now: number): InterviewEventCardModel;
```

Status always wins over time. Time only sorts active events and decides whether an active event is overdue. The projector never mutates an Event or calls a service.

- [x] **Step 4: Verify lifecycle GREEN and TypeScript**

Run the three focused tests and `npm run build`. Expected: PASS and build exit zero.

- [x] **Step 5: Commit lifecycle contracts**

```powershell
git add web/src/types/interviewIndex.ts web/src/features/interviewEvents
git commit -m "feat: AI 统一面试事件生命周期"
```

### Task 5: Implement deterministic ApplicationTaskResolver

**Files:**

- Create: `web/src/features/applicationTasks/applicationTaskResolver.ts`
- Create: `web/src/features/applicationTasks/applicationTaskResolver.test.ts`
- Modify: `web/src/components/applicationWorkspaceModel.ts`
- Modify: `web/src/components/applicationWorkspaceModel.test.ts`
- Modify: `web/src/lib/nextStepSuggestions.ts`
- Modify: `web/src/lib/nextStepSuggestions.test.ts`

- [x] **Step 1: Write failing resolver matrix tests**

Cover every Application status; loading/error/absent JD; Pending and result unknown; zero/one/multiple Offer; Offer ownership mismatch; event lifecycle matrix; completed event with/without review; application-level review without Event; deleted/stale entities; shuffled inputs; stable tie-breaking; and exactly one primary.

```ts
const result = resolveApplicationTasks(Object.freeze(snapshot), now);
expect(result.tasks.filter((task) => task.primary)).toHaveLength(1);
expect(result.tasks.map((task) => task.ref)).toEqual(expect.arrayContaining([
  { taskId: 'application.general_review', applicationId: 7 },
]));
```

- [x] **Step 2: Run resolver tests and verify RED**

```powershell
cd web
npm test -- --run src/features/applicationTasks/applicationTaskResolver.test.ts src/components/applicationWorkspaceModel.test.ts src/lib/nextStepSuggestions.test.ts
```

Expected: FAIL because the pure resolver is missing and old consumers still infer completion from time.

- [x] **Step 3: Implement immutable snapshot resolution**

Export `FrozenApplicationTaskSnapshot`, `TaskAvailability`, `ApplicationTaskModel`, `ApplicationTaskResolution` and:

```ts
export function resolveApplicationTasks(snapshot: FrozenApplicationTaskSnapshot, now: number): ApplicationTaskResolution;
```

Apply priority: scoped Pending/result unknown; prepareable Event within 24 hours; completed Event without review; unfinished Material Kit; unresolved Offer; stage Fit/Material/Outcome; remaining read-only tasks. Ties use business time, numeric identity, then Task ID. Adapt `getApplicationWorkspaceStage()` and `nextStepSuggestions` to consume `EventLifecycleV1` results rather than local time/status sets.

- [x] **Step 4: Verify resolver GREEN**

Run the focused suite again. Expected: PASS with no service imports in the resolver.

- [x] **Step 5: Commit Application resolution**

```powershell
git add web/src/features/applicationTasks web/src/components/applicationWorkspaceModel.ts web/src/components/applicationWorkspaceModel.test.ts web/src/lib/nextStepSuggestions.ts web/src/lib/nextStepSuggestions.test.ts
git commit -m "feat: AI 建立确定性投递任务解析器"
```

### Task 6: Converge ApplicationDetail and AppShell on one active owner

**Files:**

- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/AppShell.test.ts`
- Modify: `web/src/components/ApplicationDetail.tsx`
- Modify: `web/src/components/ApplicationDetail.module.css`
- Modify: `web/src/components/ApplicationDetail.workspace.test.ts`
- Modify: `web/src/components/ApplicationDetail.deterministicPilot.test.tsx`
- Modify: `web/src/components/ApplicationDetail.nextStepSuggestions.test.tsx`
- Create: `web/src/features/coreTaskSurface/AppShellCoreTaskHost.test.tsx`

- [x] **Step 1: Write failing composition and ownership tests**

Assert Header, preparation card, Haru, Pilot and legacy deep link launch the same canonical key; duplicate launch keeps draft and generation; only one active owner mounts; switching Application runs the existing unsaved/Pending guard; stale callbacks do nothing; application-level and event-level reviews remain distinct; zero/one/many Offers enter Application-level owner with Offer ID only as hint.

Raw-source tests must reject Fit/Material/Interview/Offer/Outcome in `moreActionItems`, local `TERMINAL_EVENT_STATUSES`, nearest-event selection and local Drawer owner state.

- [x] **Step 2: Run focused tests and verify RED**

```powershell
cd web
npm test -- --run src/features/coreTaskSurface/AppShellCoreTaskHost.test.tsx src/components/ApplicationDetail.workspace.test.ts src/components/ApplicationDetail.deterministicPilot.test.tsx src/components/ApplicationDetail.nextStepSuggestions.test.tsx src/layout/AppShell.test.ts
```

Expected: FAIL on duplicate local owners and legacy entry handlers.

- [x] **Step 3: Integrate the controller at the composition root**

Create one controller with `useRef(createCoreTaskSurfaceController(...))` in `AppShellContent`. Pass `onLaunchTask(request)` into `ApplicationDetail`, Interview, Haru/Pilot projections and deep-link adapters. `ApplicationDetail` renders preparation overview cards from `resolveApplicationTasks()` and a single `CoreTaskSurfaceHost`; its “更多操作” contains record editing, schedule management, notes, archive/delete only.

Remove local Fit/Material/Preparation/Review/Outcome owner booleans once each canonical renderer is hosted. Preserve existing query cache, mutation functions and Assistant controller; do not import `services/chat` into new business surfaces.

- [x] **Step 4: Verify composition GREEN and zero duplicate transport**

Run the focused suite plus:

```powershell
npm test -- --run src/features/assistantSurface/HaruChatWindow.test.tsx src/features/assistantSurface/PilotWorkspace.test.tsx
```

Expected: PASS; AppShell source contains no `EventSource`, `streamChat(` or second Assistant controller.

- [x] **Step 5: Commit the single-owner Application composition**

```powershell
git add web/src/layout/AppShell.tsx web/src/layout/AppShell.test.ts web/src/components/ApplicationDetail.tsx web/src/components/ApplicationDetail.module.css web/src/components/ApplicationDetail.workspace.test.ts web/src/components/ApplicationDetail.deterministicPilot.test.tsx web/src/components/ApplicationDetail.nextStepSuggestions.test.tsx web/src/features/coreTaskSurface/AppShellCoreTaskHost.test.tsx
git commit -m "refactor: AI 收敛投递详情单一任务界面"
```

### Task 7: Make OpportunityFitReviewDrawer the only mutation owner

**Files:**

- Create: `web/src/features/applicationTasks/opportunityFitHistory.ts`
- Create: `web/src/features/applicationTasks/opportunityFitHistory.test.ts`
- Modify: `web/src/components/OpportunityFitReviewDrawer.tsx`
- Modify: `web/src/components/OpportunityFitReviewDrawer.test.tsx`
- Modify: `web/src/features/pilot/PilotOpportunityFitV2Card.tsx`
- Modify: `web/src/features/pilot/PilotOpportunityFitV2Card.test.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/AppShell.test.ts`

- [x] **Step 1: Write failing owner, history and safe-copy tests**

Test stable V1/V2 merged ordering; partial failure state; source changed; provider/result unknown; read-only V1; Drawer draft restoration by Application; close/reopen; Application switch generation; Pending confirmation; and Pilot projection with one `onOpenTask` callback only.

Type/source tests must reject Pilot props or calls named `onStartTriage`, `onConfirmTriage`, `onStartDeepReview`, `onViewHistory`, `onStartNew`, retry mutations or a mutable draft callback.

- [x] **Step 2: Run Fit tests and verify RED**

```powershell
cd web
npm test -- --run src/features/applicationTasks/opportunityFitHistory.test.ts src/components/OpportunityFitReviewDrawer.test.tsx src/features/pilot/PilotOpportunityFitV2Card.test.tsx src/layout/AppShell.test.ts
```

Expected: FAIL because Pilot still owns mutation callbacks and history is split.

- [x] **Step 3: Implement the single owner and read-only projection**

`adaptOpportunityFitHistory()` produces:

```ts
export interface OpportunityFitHistoryItem {
  internalKey: string;
  createdAt: string;
  summary: string;
  sourceState: 'current' | 'source_changed' | 'unavailable';
  details: SafeOpportunityFitDetails;
}
```

Only `OpportunityFitReviewDrawer` retains triage, confirm, deep-review and recovery mutations. `PilotOpportunityFitV2Card` accepts a bounded result summary, status and `onOpenTask`; it renders no IDs, V1/V2, stage, snapshot, hash, confirmation token or raw error. One history source failure renders “部分历史暂时不可用”.

- [x] **Step 4: Verify Fit GREEN and unchanged service requests**

Run the focused suite and `src/services/opportunityFitReviews.test.ts`. Expected: PASS; service path/payload goldens unchanged.

- [x] **Step 5: Commit Opportunity Fit cutover**

```powershell
git add web/src/features/applicationTasks web/src/components/OpportunityFitReviewDrawer.tsx web/src/components/OpportunityFitReviewDrawer.test.tsx web/src/features/pilot/PilotOpportunityFitV2Card.tsx web/src/features/pilot/PilotOpportunityFitV2Card.test.tsx web/src/layout/AppShell.tsx web/src/layout/AppShell.test.ts
git commit -m "refactor: AI 收敛岗位判断唯一操作面"
```

### Task 8: Converge Material Kit state, handoff and Resume selection lease

**Files:**

- Create: `web/src/features/materialSurfaces/materialKitSurface.ts`
- Create: `web/src/features/materialSurfaces/materialKitSurface.test.ts`
- Create: `web/src/features/interviewEvents/resumeSelectionLease.ts`
- Create: `web/src/features/interviewEvents/resumeSelectionLease.test.ts`
- Modify: `web/src/components/MaterialKitDrawer.tsx`
- Modify: `web/src/components/MaterialKitDrawer.evidenceBundles.test.tsx`
- Modify: `web/src/features/pilot/materialKitHandoff.ts`
- Modify: `web/src/features/pilot/materialKitHandoff.test.ts`
- Modify: `web/src/components/materialFlowCopy.test.ts`

- [x] **Step 1: Write failing state/action and lease tests**

Cover each state and exactly one primary action: loading, missing JD, missing Resume, not generated, dirty draft, waiting confirmation, result unknown/conflict, ready/unsubmitted and submitted. Duplicate open/focus/close must keep Provider/Chat/SSE/write at zero.

Lease tests cover current explicit selection; trusted saved snapshot; exactly one visible Resume; multiple Resumes with master present; deleted selection; cross Application/Event isolation; close/cancel generation revocation; and “ask once” state.

- [x] **Step 2: Run focused tests and verify RED**

```powershell
cd web
npm test -- --run src/features/materialSurfaces/materialKitSurface.test.ts src/features/interviewEvents/resumeSelectionLease.test.ts src/features/pilot/materialKitHandoff.test.ts src/components/MaterialKitDrawer.evidenceBundles.test.tsx
```

Expected: FAIL because state projection and lease rules do not exist.

- [x] **Step 3: Implement pure projections and preserve mutation handlers**

Export:

```ts
export function projectMaterialKitSurface(input: MaterialKitSurfaceInput): MaterialKitSurfaceModel;
export function resolveResumeSelection(input: ResumeSelectionInput): ResumeSelectionResult;
export function createResumeSelectionLease(generation: number): ResumeSelectionLease;
```

Use the projector to render one primary button while retaining existing `handleGenerate`, `handleSave`, `handleConfirm`, proposal and recovery functions. Handoff carries only application identity plus validated hints; hints never enter the canonical key. Remove user-visible hash/session/key/internal ID detail, but do not alter request URLs, bodies, idempotency or confirmation behavior.

- [x] **Step 4: Verify Material GREEN and service goldens**

Run the focused suite plus `src/services/materialKits.test.ts` and `src/services/materialRevisionProposals.test.ts`. Expected: PASS with unchanged request paths/payloads.

- [x] **Step 5: Commit Material Kit convergence**

```powershell
git add web/src/features/materialSurfaces web/src/features/interviewEvents/resumeSelectionLease.ts web/src/features/interviewEvents/resumeSelectionLease.test.ts web/src/components/MaterialKitDrawer.tsx web/src/components/MaterialKitDrawer.evidenceBundles.test.tsx web/src/features/pilot/materialKitHandoff.ts web/src/features/pilot/materialKitHandoff.test.ts web/src/components/materialFlowCopy.test.ts
git commit -m "refactor: AI 收敛投递材料状态与简历选择"
```

### Task 9: Converge Interview Event cards, chooser and free practice

**Files:**

- Modify: `web/src/components/InterviewV01View.tsx`
- Modify: `web/src/components/InterviewV01View.test.tsx`
- Modify: `web/src/components/InterviewV01View.adaptivePractice.test.tsx`
- Modify: `web/src/features/interviewReadiness/InterviewReadinessCenter.tsx`
- Modify: `web/src/features/interviewReadiness/InterviewReadinessCenter.test.tsx`
- Modify: `web/src/features/interviewReadiness/interviewReadinessModel.ts`
- Modify: `web/src/features/interviewReadiness/interviewReadinessModel.test.ts`
- Modify: `web/src/components/ApplicationDetail.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/AppShell.interviewReview.test.tsx`

- [x] **Step 1: Write failing event-card and chooser tests**

Test future done, future cancelled, past todo, current/expired in-progress, unknown, absent schedule, invalid duration and same-time ID ordering. Assert each card has one primary action and only the allowed secondary application link.

Test app-only Pilot intent with zero/one/many Events: all enter the chooser; one Event is still not auto-selected; exact TaskRef is created only after user click. Opening chooser performs no Provider/Tool/Chat/SSE/write. Test one canonical `interview.free_practice` owner from TopBar, Interview page and question source selection.

- [x] **Step 2: Run Interview tests and verify RED**

```powershell
cd web
npm test -- --run src/components/InterviewV01View.test.tsx src/components/InterviewV01View.adaptivePractice.test.tsx src/features/interviewReadiness/InterviewReadinessCenter.test.tsx src/features/interviewReadiness/interviewReadinessModel.test.ts src/layout/AppShell.interviewReview.test.tsx
```

Expected: FAIL on time-only buckets, embedded generic form, nearest-event selection and legacy mock fallback.

- [x] **Step 3: Render only lifecycle-projected cards and exact handoffs**

Remove `ENDED_EVENT_STATUSES`, `isUpcomingInterview`, local time-terminal inference and `onOpenMockInterview` fallback. Upcoming renders Event cards before any preparation UI; completed cards render review as their sole primary; cancelled/unavailable cards cannot execute. `needs_status_update` navigates to existing Event editing only.

Real-event readiness receives a locked `{ applicationId, eventId }`, reads current JD by Application scope, applies `ResumeSelectionLease`, and never asks Application/Event again. Free practice keeps its current explicit user action and Provider-free opening; its POST occurs only after the user confirms the draft.

- [x] **Step 4: Verify Interview GREEN and event consumer gate**

Run the focused suite and `coreTaskSurfaceGate.test.ts`. Expected: PASS for Interview consumer ownership and no local lifecycle sets in ApplicationDetail, InterviewV01View, InterviewReadinessCenter or AppShell.

- [x] **Step 5: Commit Interview Event Surface**

```powershell
git add web/src/components/InterviewV01View.tsx web/src/components/InterviewV01View.test.tsx web/src/components/InterviewV01View.adaptivePractice.test.tsx web/src/features/interviewReadiness web/src/components/ApplicationDetail.tsx web/src/layout/AppShell.tsx web/src/layout/AppShell.interviewReview.test.tsx
git commit -m "refactor: AI 收敛面试事件与自由练习入口"
```

### Task 10: Split experience materials from external references

**Files:**

- Create: `web/src/features/materialSurfaces/materialClassification.ts`
- Create: `web/src/features/materialSurfaces/materialClassification.test.ts`
- Create: `web/src/components/ExperienceMaterialsView.tsx`
- Create: `web/src/components/ExperienceMaterialsView.test.tsx`
- Modify: `web/src/components/KnowledgeSourcesView.tsx`
- Modify: `web/src/components/KnowledgeSourcesView.test.tsx`
- Modify: `web/src/components/KnowledgeSourcesView.mount.test.tsx`
- Modify: `web/src/components/InterviewStoryLibraryView.tsx`
- Modify: `web/src/components/InterviewStoryLibraryView.test.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/types/knowledge.ts`

- [x] **Step 1: Write failing eligibility and projection tests**

Cover complete confirmed capture; capture origin with missing metadata; captured source with broken Note/Event relation; external markdown/text/bundle without capture metadata; external-looking source with capture metadata; unknown kind/origin; Story; raw Note; Proposal; Pending; loading/error/empty states; and record exclusivity.

```ts
expect(classifyMaterialRecord(orphanCapture)).toEqual({ kind: 'captured_unavailable', reason: 'capture_relation_invalid' });
expect(projectExternalReferences(records)).not.toContainEqual(expect.objectContaining({ id: orphanCapture.id }));
```

- [x] **Step 2: Run focused frontend/backend tests and verify RED**

```powershell
cd web
npm test -- --run src/features/materialSurfaces/materialClassification.test.ts src/components/ExperienceMaterialsView.test.tsx src/components/KnowledgeSourcesView.test.tsx src/components/InterviewStoryLibraryView.test.tsx
```

Expected: FAIL because Knowledge still renders confirmed capture and no eligibility projector exists.

- [x] **Step 3: Implement fail-closed classifiers and route projections**

Export:

```ts
export function classifyMaterialRecord(input: MaterialRecord): MaterialClassification;
export function projectExperienceMaterials(input: MaterialProjectionInput): ExperienceMaterialProjection;
export function projectExternalReferences(input: MaterialProjectionInput): ExternalReferenceProjection;
```

`reviews` renders `ExperienceMaterialsView`, combining confirmed Story and complete confirmed captures. `knowledge` renders only external references and retains its existing source management mutations. Orphan/broken captures remain unavailable in interview-source context and never fall back to references. Preserve AppShell's read-only confirmed-capture loader for preparation; do not duplicate fetch ownership or add Chat/SSE/Provider calls.

- [x] **Step 4: Verify classification GREEN**

Run the frontend/backend focused suites again. Expected: PASS with explicit partial-unavailable versus empty states.

- [x] **Step 5: Commit material classification split**

```powershell
git add web/src/features/materialSurfaces web/src/components/ExperienceMaterialsView.tsx web/src/components/ExperienceMaterialsView.test.tsx web/src/components/KnowledgeSourcesView.tsx web/src/components/KnowledgeSourcesView.test.tsx web/src/components/KnowledgeSourcesView.mount.test.tsx web/src/components/InterviewStoryLibraryView.tsx web/src/components/InterviewStoryLibraryView.test.tsx web/src/layout/AppShell.tsx web/src/types/knowledge.ts
git commit -m "refactor: AI 拆分经历素材与外部参考资料"
```

### Task 11: Implement ResumeLineageV1 and unified labels

**Files:**

- Create: `web/src/features/materialSurfaces/resumeLineage.ts`
- Create: `web/src/features/materialSurfaces/resumeLineage.test.ts`
- Create: `web/src/features/materialSurfaces/materialLabels.ts`
- Create: `web/src/features/materialSurfaces/materialLabels.test.ts`
- Modify: `web/src/components/ResumeLibraryView.tsx`
- Modify: `web/src/components/ResumeLibraryView.test.ts`
- Modify: `web/src/components/ResumeCard.tsx`
- Modify: `web/src/components/ResumeCard.test.tsx`
- Modify: `web/src/components/ResumeEditorDrawer.tsx`
- Modify: `web/src/components/ResumeEditorDrawer.mount.test.tsx`
- Modify: `web/src/components/ResumeVersionCompareDrawer.tsx`
- Modify: `web/src/components/ResumeVersionCompareDrawer.test.tsx`
- Modify: `web/src/components/ResumeFactSupplementWorkspace.tsx`
- Modify: `web/src/components/ResumeFactSupplementWorkspace.test.tsx`

- [x] **Step 1: Write failing lineage and cross-entry label tests**

Cover base, valid job variant, independent, missing parent, self-reference, two-node and multi-node cycles, master-with-parent conflict, deleted/hidden parent and valid visible parent. Assert the same record gets the same label in Library, Editor, Compare, Fact Supplement and Material selection. Rendering the projector must call no update/copy/delete/bind service.

- [x] **Step 2: Run focused Resume tests and verify RED**

```powershell
cd web
npm test -- --run src/features/materialSurfaces/resumeLineage.test.ts src/features/materialSurfaces/materialLabels.test.ts src/components/ResumeCard.test.tsx src/components/ResumeVersionCompareDrawer.test.tsx src/components/ResumeFactSupplementWorkspace.test.tsx
```

Expected: FAIL on local “主简历/父版本/#ID” rules and missing anomaly states.

- [x] **Step 3: Implement pure lineage and label mappers**

```ts
export type ResumeLineageKind = 'base' | 'job_variant' | 'independent' | 'relationship_unknown';
export interface ResumeLineage { kind: ResumeLineageKind; parent?: Resume; label: '基础简历' | '岗位版本' | '其他简历' | '关系待确认'; detail?: string }
export function resolveResumeLineage(resumes: readonly Resume[], targetId: number): ResumeLineage;
```

Valid variants show `基于 <visible title>`; anomalies show only `关系待确认` and disable actions that create new lineage. Existing edit/copy/compare/allowed-delete/set-base semantics remain. Remove component-local user labels derived directly from `is_master`, `parent_resume_id`, `origin_kind` and raw internal IDs.

- [x] **Step 4: Verify Resume GREEN and unchanged API contract**

Run the focused suite plus `src/services/resumes.test.ts`. Expected: PASS; no Application binding or database repair is added.

- [x] **Step 5: Commit Resume lineage and shared language**

```powershell
git add web/src/features/materialSurfaces web/src/components/ResumeLibraryView.tsx web/src/components/ResumeLibraryView.test.ts web/src/components/ResumeCard.tsx web/src/components/ResumeCard.test.tsx web/src/components/ResumeEditorDrawer.tsx web/src/components/ResumeEditorDrawer.mount.test.tsx web/src/components/ResumeVersionCompareDrawer.tsx web/src/components/ResumeVersionCompareDrawer.test.tsx web/src/components/ResumeFactSupplementWorkspace.tsx web/src/components/ResumeFactSupplementWorkspace.test.tsx
git commit -m "refactor: AI 统一简历关系与用户文案"
```

### Task 12: Close legacy paths, visible-copy leaks and source gates

**Files:**

- Modify: `web/src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts`
- Create: `web/src/features/coreTaskSurface/coreTaskSurfaceNegativeFixtures.test.ts`
- Modify: `web/src/components/systemCopyRegression.test.ts`
- Modify: `tests/test_core_task_surface_assets.py`

- [x] **Step 1: Add failing negative fixtures for every mechanical rule**

Use TypeScript AST traversal to reject: unclassified entrypoint; duplicate owner; missing owner; local event status set; direct Fit/Material/Interview mutation outside owner allowlist; Pilot mutation callback; legacy handler/fallback/feature flag/shadow surface; Knowledge/Reviews cross-projection; and component-local Resume/source user mapping.

Scan JSX text, string literals used in `aria-label`, Tooltip, Toast and error boundaries for exact forbidden lexemes/regexes, not broad `ID/key/阶段` substrings. Negative fixtures must prove each violation is detected.

- [x] **Step 2: Run gates and verify RED against remaining legacy code**

```powershell
uv run pytest tests/test_core_task_surface_assets.py -q
cd web
npm test -- --run src/features/coreTaskSurface/coreTaskSurfaceGate.test.ts src/features/coreTaskSurface/coreTaskSurfaceNegativeFixtures.test.ts src/components/systemCopyRegression.test.ts
```

Expected: FAIL with named remaining legacy symbols/copy, if any.

- [x] **Step 3: Delete the legacy paths instead of adding fallback facades**

Remove the audited old handlers, flags, shadow renders, duplicate callbacks, V1/V2 headings, assessment number, snapshot/hash/key/Worker/queue/heartbeat/source ID/fingerprint copy, “证据门控”, “VAD 帧” and raw protocol error text from production UI. Keep safe replacements from the central mapper. Do not delete history data or backend read APIs.

- [x] **Step 4: Verify all mechanical gates GREEN**

Run both gate commands again and:

```powershell
git diff --check
git status --short
```

Expected: gates PASS, diff check exits zero, and every changed/untracked path is inside the reviewed project allowlist.

- [x] **Step 5: Commit the one-way cutover gates**

```powershell
git add web/src/features/coreTaskSurface web/src/components/systemCopyRegression.test.ts tests/test_core_task_surface_assets.py
git commit -m "test: AI 封闭旧任务入口与内部文案"
```

### Task 13: Integrated verification, browser matrix, independent review and report

**Files:**

- Create: `docs/superpowers/reports/2026-08-29-core-task-surface-convergence-verification.md`
- Modify: `docs/superpowers/plans/2026-08-29-core-task-surface-convergence.md`

- [x] **Step 1: Run focused contract suites**

```powershell
uv run pytest tests/test_core_task_surface_assets.py tests/test_interview_index_api.py tests/test_interview_knowledge_capture_api.py tests/test_knowledge_sources_api.py tests/test_resumes_api.py -q
```

From `web`:

```powershell
npm test -- --run src/features/coreTaskSurface src/features/applicationTasks src/features/interviewEvents src/features/materialSurfaces src/components/ApplicationDetail.workspace.test.ts src/components/InterviewV01View.test.tsx src/components/OpportunityFitReviewDrawer.test.tsx src/components/MaterialKitDrawer.evidenceBundles.test.tsx src/components/KnowledgeSourcesView.test.tsx src/components/InterviewStoryLibraryView.test.tsx src/components/ResumeCard.test.tsx src/layout/AppShell.test.ts
```

Record exact counts and exit codes.

- [x] **Step 2: Run the full supported release gate with fresh output**

Run separately from repository root:

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
```

Run separately from `web`:

```powershell
npm test -- --run
npm run build
```

Then from repository root:

```powershell
uv run oc smoke --static-dir web/dist
uv run oc verify --profile local --static-dir web/dist
```

Run the repository's controlled real-AI verification command with existing local configuration. If a required Provider credential, orchestrator, Docker capability or external service is unavailable, record the exact command, exit/error and excluded risk; do not substitute fabricated input.

- [x] **Step 3: Run the real browser acceptance matrix**

Use the built-in Codex browser against an isolated local data directory. Verify 768/1024/1280/1440 widths, light/dark themes, keyboard-only navigation and reduced motion. Inspect requests/console while exercising duplicate opener, tab changes, Fit approve/modify/reject/unknown, Material generate/confirm/conflict recovery, past active Event, Pilot chooser without Event ID, Resume base/variant/anomaly and experience/reference isolation.

For open/focus/close/tab/history actions record Provider/Tool/Chat/SSE/domain-write counts as zero. For executing actions compare Provider/HTTP/SSE/mutation counts to `core_task_request_counts_93fb006.json` and record any intentional Interview read-response difference.

- [x] **Step 4: Request independent CR and close all P0/P1/P2 findings**

Dispatch a fresh reviewer with the approved design, this plan, `BASE_SHA=93fb0063118761f2c76e71e4209000feee0f755b`, current `HEAD_SHA`, exact diff and verification evidence. Fix every P0/P1/P2 under TDD, rerun affected focused tests and request a final re-review. The release report must contain the final reviewer verdict and any accepted lower-priority risk.

- [x] **Step 5: Write and verify the release report**

The report must state: implemented surfaces; internally deleted entries/handlers; `/api/interviews` read-contract fields and unique latest-Note projection; no migration; HTTP/SSE/HITL/Ledger/Provider/Tool equivalence evidence; browser matrix; external gate exclusions; remaining risks; and Review-to-Readiness as a separate future project.

Finish with fresh checks:

```powershell
git diff --check
git status --short --branch
git diff --name-only 93fb006..HEAD
```

- [x] **Step 6: Commit the verified report and checked plan**

```powershell
git add -f docs/superpowers/plans/2026-08-29-core-task-surface-convergence.md docs/superpowers/reports/2026-08-29-core-task-surface-convergence-verification.md
git commit -m "docs: AI 记录核心任务界面收敛验收"
```

Do not push or merge. Hand back only after the worktree is clean, the independent review has no open P0/P1/P2, and every unrun/excluded gate is explicitly listed.
