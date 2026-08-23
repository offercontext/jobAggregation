# Agent Loop Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the split LangGraph/new-turn and manual confirmation-resume paths with one typed, explicit Agent Loop while preserving every external Chat, Provider, Tool, Pending, Ledger, Journal, Context Surface, HTTP/SSE, cancellation, and business-side-effect contract.

**Architecture:** `PilotRuntime` constructs one `AgentLoopInvocation` containing either `NewTurnSeed` or `ApprovedWriteSeed`, and calls the sole `AgentDriver.execute()` method. `AgentLoopRunner` bootstraps the selected seed and then enters one explicit `while` loop; rejection, replay, recovery, and deterministic actions remain provider-free Runtime routes. Agent events are a closed transport-neutral union translated by composition into existing typed Runtime events.

**Tech Stack:** Python 3.12, pytest, SQLAlchemy 2, SQLite, existing Context Projector/Provider Gateway/Tool Pipeline/Write Operation Ledger/Journal/Pilot Runtime, uv, Ruff, Mypy, Vitest, Vite.

---

## 0. Fixed execution boundary and file structure

Work only in:

```text
D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260823-agent-loop-unification
```

Branch and fixed source baseline:

```text
refactor/20260823-agent-loop-unification
aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb
```

Reviewed design:

```text
docs/superpowers/specs/2026-08-23-agent-loop-unification-design.md
```

Do not push or merge. Do not modify the root workspace or another worktree. Do not add a feature flag, shadow execution, old-loop fallback, database/API/UI change, new retry, or expanded capability.

### File responsibilities

```text
src/offerpilot/ai/agent_contracts.py
  Chat model protocols, PendingAction, AgentTurnResult, closed AgentLoopEvent DTOs,
  cancellation/error types, and the strict AgentDriver/ApprovedWriteContinuation protocols.

src/offerpilot/ai/agent_loop.py
  NewTurnSeed, ApprovedWriteSeed, AgentLoopInvocation, AgentLoopRunner, explicit while loop,
  frozen-surface model step, baseline ToolCall selection, and Tool Pipeline dispatch.

src/offerpilot/ai/confirmation.py
  Pure confirmation-edit parsing, canonical JSON validation, Pending revision, and descriptions.

src/offerpilot/context_projector/binding.py
  Bound response provenance and fail-closed exposed-tool validation.

src/offerpilot/context_projector/gateway.py
  Sole production BoundProviderResponse construction and session-owned attempt identities.

src/offerpilot/ai/client.py
  Expose the existing Gateway Session through the typed model protocol without changing network behavior.

src/offerpilot/pilot_runtime/composition.py
  One concrete AgentDriver.execute adapter, ProposalJournalGate installation,
  ConfirmationSession-to-ApprovedWriteContinuation adapter, and Agent event projection.

src/offerpilot/pilot_runtime/service.py
  Build NewTurnSeed/ApprovedWriteSeed invocations and call execute exactly once;
  keep rejection/replay/recovery/deterministic routes outside the loop.

src/offerpilot/pilot_runtime/continuation.py
  Keep Ledger and delivery state machines; export only the narrow session operations the approved port needs.

src/offerpilot/api.py
  Compose one Agent Loop dependency; remove run/resume wrappers and dual callable injection.

tests/fixtures/agent_loop/baseline_aaecf5d.json
  Immutable canonical characterization asset with no generator or update switch.

tests/agent_loop/test_baseline_golden.py
  Pin the asset to aaecf5d and prove referenced baseline tests exist.

tests/agent_loop/test_contracts.py
  Seed/invocation/event/privacy/provenance contract tests.

tests/agent_loop/test_runner.py
  New-turn, approved bootstrap, ToolCall matrix, failure, cancellation, and sink tests.

tests/agent_loop/test_deletion_gates.py
  AST/source/dependency/serialization/privacy gates plus rejecting negative fixtures.

tests/pilot_runtime/test_start_turn.py
tests/pilot_runtime/test_stream_preparation.py
tests/pilot_runtime/test_confirmation.py
tests/pilot_runtime/test_event_sink.py
tests/test_chat_api.py
  Sync/stream route cutover, exact Runtime/SSE compatibility, and Driver-zero paths.

docs/reports/2026-08-23-agent-loop-unification-release-verification.md
  Baseline, characterization/red/green evidence, commands, CR, risks, and final state.
```

### Task 1: Freeze the aaecf5d characterization golden

**Files:**
- Create: `tests/fixtures/agent_loop/baseline_aaecf5d.json`
- Create: `tests/agent_loop/__init__.py`
- Create: `tests/agent_loop/test_baseline_golden.py`
- Read: `tests/test_ai_agent.py`
- Read: `tests/pilot_runtime/test_confirmation.py`
- Read: `tests/test_chat_api.py`

- [ ] **Step 1: Prove production still equals the fixed source baseline**

Run:

```powershell
$production = git diff --name-only aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb..HEAD -- src pyproject.toml uv.lock
if ($production) { throw "production differs from aaecf5d: $production" }
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Write the failing immutable-asset test**

Create `tests/agent_loop/test_baseline_golden.py` with a canonical JSON assertion, exact source baseline assertion, an exact scenario list, and an AST scan proving every `required_existing_tests` name is defined. The core assertions are:

```python
GOLDEN = Path(__file__).parents[1] / "fixtures" / "agent_loop" / "baseline_aaecf5d.json"


def test_agent_loop_baseline_is_canonical_and_pinned() -> None:
    raw = GOLDEN.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schema_version"] == 1
    assert value["source_baseline"] == "aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb"
    assert raw == json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    assert value["tool_call_selection"] == {
        "read_read": ["read-1", "read-2"],
        "read_write": ["read-1"],
        "write_read": ["write-1"],
        "write_write": ["write-1"],
    }
```

- [ ] **Step 3: Run the golden test and verify RED**

Run:

```powershell
uv run pytest tests/agent_loop/test_baseline_golden.py -q
```

Expected: fail because `baseline_aaecf5d.json` does not exist.

- [ ] **Step 4: Create the canonical read-only asset**

Store only stable synthetic facts: source SHA, required existing test names, ToolCall selection, final/pending shape, approved-origin event order, Provider/Driver zero routes, and SSE event-name sequences. Serialize with sorted keys, compact separators, and one trailing newline. Do not add an asset generator, overwrite command, or acceptance flag.

- [ ] **Step 5: Run baseline characterization and verify GREEN**

Run:

```powershell
uv run pytest tests/agent_loop/test_baseline_golden.py tests/test_ai_agent.py tests/pilot_runtime/test_confirmation.py tests/test_chat_api.py -q
```

Expected: all collected tests pass with no new skip.

- [ ] **Step 6: Commit the plan and immutable characterization**

```powershell
git add docs/superpowers/plans/2026-08-23-agent-loop-unification.md tests/agent_loop tests/fixtures/agent_loop/baseline_aaecf5d.json
git commit -m "test: AI 固化 Agent Loop 基线行为"
```

### Task 2: Define closed Agent contracts and Gateway provenance

**Files:**
- Create: `src/offerpilot/ai/agent_contracts.py`
- Modify: `src/offerpilot/context_projector/binding.py`
- Modify: `src/offerpilot/context_projector/gateway.py`
- Modify: `src/offerpilot/ai/client.py`
- Create: `tests/agent_loop/test_contracts.py`
- Modify: `tests/test_context_projector.py`

- [ ] **Step 1: Write failing contract tests**

Test `PendingAction`, `AgentTurnResult`, `AgentAssistantDelta`, `AgentToolCall`, `AgentToolResult`, `AgentDriver.execute`, and `ApprovedWriteContinuation`. Prove event DTOs reject mutable/non-JSON payloads and that transient DTOs/ports use `repr=False`, reject pickle, and do not expose credentials/callbacks.

Use this desired Driver surface:

```python
class AgentDriver(Protocol):
    def execute(self, invocation: AgentLoopInvocation) -> AgentTurnResult: ...
```

Use these event shapes:

```python
@dataclass(frozen=True, slots=True)
class AgentAssistantDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    tool_call_id: str
    tool_name: str
    public_label: str
    kind: Literal["read", "write"]
    confirm_mode: Literal["none", "hitl", "approved"]
    summary: str
    args_summary: Mapping[str, JsonValue]
```

- [ ] **Step 2: Write failing provenance tests**

Cover model-call mismatch, fingerprint mismatch, negative/out-of-range ordinal, empty attempt ID, attempt from a different Gateway Session, and exposed-tool failure. Assert response validation occurs before assistant/event/dispatcher counters change.

- [ ] **Step 3: Run tests and verify RED**

```powershell
uv run pytest tests/agent_loop/test_contracts.py tests/test_context_projector.py -q
```

Expected: fail because the contract module and session-owned attempt validation do not exist.

- [ ] **Step 4: Implement minimal closed contracts and attempt ownership**

Make `AgentProviderGatewaySession` the only production creator of bound responses. Create an opaque session attempt handle immediately before each candidate call, register it in the current Session, and require `ModelCallSurfaceBinding.validate_response()` to verify:

```text
same model_call_id
same runtime_surface_fingerprint
0 <= candidate_ordinal < frozen candidate count
non-empty attempt ID owned by the current Gateway Session
every ToolCall name is exposed
```

Do not compare ordinal or attempt ID with a value invented before candidate execution. Test injection must construct a one-candidate Gateway Session.

- [ ] **Step 5: Run provenance tests and verify GREEN**

```powershell
uv run pytest tests/agent_loop/test_contracts.py tests/test_context_projector.py tests/test_ai_client.py -q
```

Expected: all pass.

### Task 3: Define the two seeds and write the explicit-loop RED matrix

**Files:**
- Create: `src/offerpilot/ai/agent_loop.py`
- Create: `tests/agent_loop/test_runner.py`

- [ ] **Step 1: Write failing seed and invocation tests**

Use the exact public shape:

```python
@dataclass(frozen=True, slots=True)
class NewTurnSeed:
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWriteSeed:
    continuation: ApprovedWriteContinuation


@dataclass(frozen=True, slots=True, repr=False)
class AgentLoopInvocation:
    seed: NewTurnSeed | ApprovedWriteSeed
    model: ChatModel
    catalog: ToolCatalog
    tool_context: ToolExecutionContext
    auto_approve: bool
    max_iterations: int
    run_recorder: RunRecorder
    event_sink: AgentEventSink | None
    runtime_signal_sink: RuntimeSignalSink[str] | None
    cancel_check: CancelCheck | None
```

Prove NewTurnSeed detaches the tuple, ApprovedWriteSeed reads Pending only from the port, approved context requires a bound Ledger executor, invalid tool/operation identities fail before Provider/executor, and `max_iterations=0` resolves to 20.

- [ ] **Step 2: Write failing new-turn loop tests**

Cover final response, read+read, read failure+read, write validation failure then Provider, write Pending with executor 0, `auto_approve=True` Pending, all four multi-call matrix rows, max-iteration boundary, cancellation checkpoints, BaseException propagation, projection failure Provider 0, and unexposed tool Dispatcher/event/message/executor 0.

- [ ] **Step 3: Write failing approved-bootstrap tests**

Use a fake port that appends phase names and assert:

```python
assert phases == [
    "pending", "prepare", "emit_call", "claim", "execute", "emit_result",
    "record", "load", "provider",
]
```

Cover claim failure, mutable validation failure, terminal persisted result, executor failure, record failure, load failure, origin ToolMessage once, origin records/failures retained, approved→read→final, approved→chained Pending, and model steps restarting at zero.

- [ ] **Step 4: Write failing event-sink tests**

For first delta, later delta, ToolCall, and ToolResult emit failures, assert the sink exception escapes immediately and Provider fallback/next Provider/next Tool/executor rerun counters stay zero. ToolCall failure occurs before prepare; ToolResult failure preserves the one already-completed executor call.

- [ ] **Step 5: Run the RED matrix**

```powershell
uv run pytest tests/agent_loop/test_runner.py -q
```

Expected: collection or behavior failures because `AgentLoopRunner.run()` is not implemented.

### Task 4: Implement one explicit while loop and focused GREEN

**Files:**
- Modify: `src/offerpilot/ai/agent_loop.py`
- Modify: `tests/agent_loop/test_runner.py`

- [ ] **Step 1: Implement seed bootstrap**

For NewTurnSeed initialize detached working messages and empty added/records/failures. For ApprovedWriteSeed perform cancel/fence checks, resolve a write spec, canonicalize JSON, prepare with `record_proposal=False`, emit the approved origin call, claim inside `execute_prepared`, prefer persisted visible/transport results, record the origin ToolMessage, load continuation messages exactly once, and retain origin records/failures.

- [ ] **Step 2: Implement the sole model loop**

The implementation must have one explicit construct equivalent to:

```python
while True:
    require_active()
    if model_steps >= resolved_max_iterations:
        raise RuntimeError("AI 工具调用超过最大轮次")
    assistant = complete_bound_model(working_messages, model_step=model_steps + 1)
    require_active()
    selected = select_tool_calls(assistant.tool_calls, invocation.catalog)
    model_steps += 1
    assistant_message = Message(role="assistant", content=assistant.content, tool_calls=selected)
    working_messages.append(assistant_message)
    added_messages.append(assistant_message)
    if not selected:
        return AgentTurnResult(added_messages, assistant.content, None, tuple(records), tuple(failures))
    pending = dispatch_selected_batch(selected)
    if pending is not None:
        return AgentTurnResult(added_messages, "", pending, tuple(records), tuple(failures))
```

Keep surface projection once per logical call and Provider fallback inside the existing Gateway Session. Validate the full Bound response before incrementing the model step or creating an assistant message/event.

- [ ] **Step 3: Implement exact Tool dispatch semantics**

Execute all-read batches in Provider order. If any selected response call resolves to write, retain only original `tool_calls[0]`. A NewTurn write returns Pending after `ConfirmationRequired` and never executes; reads append compatibility ToolMessages and continue even when an earlier read fails.

- [ ] **Step 4: Make sink failures fail closed**

`AgentLoopRunner` calls the typed sink directly and never catches ordinary sink exceptions. Do not send an error/completed event after a sink failure. Continue swallowing only Journal snapshot/append failures as the existing fail-open contract requires.

- [ ] **Step 5: Run focused GREEN**

```powershell
uv run pytest tests/agent_loop/test_contracts.py tests/agent_loop/test_runner.py tests/test_context_projector.py -q
uv run ruff check src/offerpilot/ai/agent_contracts.py src/offerpilot/ai/agent_loop.py src/offerpilot/context_projector tests/agent_loop
```

Expected: all pass and Ruff exits 0.

### Task 5: Extract pure confirmation editing and remove the old Agent module

**Files:**
- Create: `src/offerpilot/ai/confirmation.py`
- Delete: `src/offerpilot/ai/agent.py`
- Modify: all production/test imports returned by `rg -l "offerpilot\.ai\.agent" src tests`
- Modify: `tests/test_ai_agent.py` by splitting retained edit-contract cases into `tests/agent_loop/test_confirmation.py` and loop cases into `tests/agent_loop/test_runner.py`

- [ ] **Step 1: Move pure edit tests before implementation**

Move the existing `prepare_pending_action` cases unchanged except imports. Add assertions for canonical object JSON, duplicate keys, non-finite numbers, editable-field descriptors, exact clear sentinels, validator failures, preserved IDs, and revised human descriptions.

- [ ] **Step 2: Run moved tests and verify RED**

```powershell
uv run pytest tests/agent_loop/test_confirmation.py -q
```

Expected: fail because `offerpilot.ai.confirmation` does not exist.

- [ ] **Step 3: Move the pure implementation**

Move only `prepare_pending_action` and its JSON/edit helpers into `ai/confirmation.py`. Move shared DTOs/errors into `agent_contracts.py` and execution helpers into `agent_loop.py`. Update every repository import to the new owning module.

- [ ] **Step 4: Delete old execution symbols**

Remove `LangGraphAgentRunner`, Graph State, checkpoint helpers, `run_turn`, `resume_after_confirm`, `_resume_without_checkpoint`, serialization helpers used only by Graph, confirmation locks, fallback claims, and the old dict event bridge. Do not leave `ai.agent` as a façade.

- [ ] **Step 5: Run migrated Agent tests**

```powershell
uv run pytest tests/agent_loop tests/test_ai_agent.py -q
```

Expected: all retained tests pass and no test imports the deleted module.

### Task 6: Cut start sync/stream to AgentDriver.execute(NewTurnSeed)

**Files:**
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `tests/pilot_runtime/test_start_turn.py`
- Modify: `tests/pilot_runtime/test_stream_preparation.py`
- Modify: `tests/pilot_runtime/test_event_sink.py`

- [ ] **Step 1: Write failing strict Driver tests**

Use a Driver spy with only `execute(invocation)`. Assert sync and stream each call it once, `invocation.seed` is NewTurnSeed, messages are detached, `thread_id` is absent, and a Driver exposing only `run_turn` fails composition before Provider work.

- [ ] **Step 2: Write failing event-adapter tests**

Map each closed Agent event one-to-one to existing `AssistantDeltaEvent`, `ToolCallEvent`, and `ToolResultEvent`. Assert Runtime control exceptions propagate; ordinary sink exceptions become `RuntimeTransportAborted`; BaseException propagates unchanged; no generic dict input is accepted.

- [ ] **Step 3: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_start_turn.py tests/pilot_runtime/test_stream_preparation.py tests/pilot_runtime/test_event_sink.py -q
```

Expected: Driver spy assertions fail because Runtime still discovers `run_turn/run` reflectively.

- [ ] **Step 4: Implement the one concrete Driver**

`composition._AgentDriver.execute()` installs `_ProposalJournalGate` for NewTurnSeed, binds the recorder into `ToolExecutionContext`, builds a typed `AgentLoopInvocation`, and calls `AgentLoopRunner.run()` once. Remove `_invoke` use for Agent parameters and remove aliases `max_iter/max_iterations`, `catalog/tool_catalog`, and `run/run_turn` at this boundary.

- [ ] **Step 5: Switch Runtime start paths**

Replace `AgentInvocation` and `_run_driver` with direct construction of `AgentLoopInvocation(NewTurnSeed(...))` followed by `driver.execute(invocation)`. Keep ExecutionHost invocation exactly once. Sync and stream differ only in host/sink ownership.

- [ ] **Step 6: Run start focused GREEN**

```powershell
uv run pytest tests/pilot_runtime/test_start_turn.py tests/pilot_runtime/test_stream_preparation.py tests/pilot_runtime/test_event_sink.py tests/test_chat_api.py -q
```

Expected: all pass with exact prior Runtime/SSE outcomes.

### Task 7: Cut approve/modify sync/stream to ApprovedWriteSeed

**Files:**
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `tests/pilot_runtime/test_confirmation.py`
- Modify: `tests/pilot_runtime/test_stream_preparation.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write failing Approved Port Runtime tests**

Assert approve and modify sync/stream each call Driver once with ApprovedWriteSeed. Assert claim precedes executor; source loads once after terminal commit and delivery ownership; chained Pending atomically replaces the old Pending; reject/replay/recovery call Driver/Projector/Provider/prepare/executor zero times.

- [ ] **Step 2: Write failing concurrency/control tests**

Cover two-connection claim winner, active delivery owner, crash takeover, late Bundle, timeouts during prepare/claim/executor/record/source/provider, disconnect, and Journal degradation. Counters must prove no second loop, Provider, or executor.

- [ ] **Step 3: Run confirmation tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_stream_preparation.py tests/test_chat_api.py -q
```

Expected: strict ApprovedWriteSeed assertions fail while the old resume dictionary exists.

- [ ] **Step 4: Implement the narrow session adapter**

Expose exactly:

```python
class _ApprovedWriteContinuation:
    @property
    def pending(self) -> PendingAction: ...
    def claim(self, pending: PendingAction, prepared: PreparedToolCall[Any, Any]) -> ExecutionAuthorization | ToolFailure: ...
    def record_result(self, pending: PendingAction, tool_message: Message, record: ToolExecutionRecord[Any, Any]) -> None: ...
    def load_continuation_messages(self) -> tuple[Message, ...]: ...
    def delivery_fence(self) -> bool: ...
```

Delegate to the existing `ConfirmationSession`; do not duplicate Ledger states or repository reads.

- [ ] **Step 5: Switch confirmation invocation**

After Ledger-first preheader, session creation, journal opening, tool-context binding, and deferred confirmation sink setup, construct `AgentLoopInvocation(ApprovedWriteSeed(port), ...)` and call `driver.execute()` once. Delete `messages=[]`, `resume_values`, `_invoke(resume, ...)`, confirmation callback aliases, and `thread_id`.

- [ ] **Step 6: Run confirmation focused GREEN**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py tests/pilot_runtime/test_stream_preparation.py tests/test_chat_api.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py -q
```

Expected: all pass with one claim winner and exact Ledger/delivery state.

### Task 8: Remove dual composition injection and LangGraph dependencies

**Files:**
- Modify: `src/offerpilot/api.py`
- Modify: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `pyproject.toml`
- Modify mechanically with uv: `uv.lock`
- Modify: imports/tests identified by source scan

- [ ] **Step 1: Remove API dual entry points**

Delete `_runtime_resume_after_confirm`, imports of `run_turn/resume_after_confirm`, and `build_pilot_runtime(run_turn_fn=..., resume_after_confirm_fn=...)`. Inject one `agent_driver`/runner dependency only.

- [ ] **Step 2: Prove no LangGraph users remain**

Run:

```powershell
rg -n "langgraph|StateGraph|InMemorySaver|interrupt\(|__interrupt__|_GraphState|LangGraphAgentRunner|_resume_without_checkpoint" src tests
```

Expected before dependency removal: no production match and only deliberate negative-fixture strings in deletion-gate tests.

- [ ] **Step 3: Remove direct dependencies with uv**

```powershell
uv remove langgraph langgraph-checkpoint-sqlite
uv lock
```

Expected: `pyproject.toml` and resolved `uv.lock` contain no unused LangGraph packages; unrelated direct dependencies remain unchanged.

- [ ] **Step 4: Run dependency and import tests**

```powershell
uv sync --locked
uv run pytest tests/agent_loop tests/pilot_runtime tests/test_chat_api.py -q
```

Expected: environment resolves and all focused tests pass.

### Task 9: Add deletion, dependency, privacy, and serialization gates

**Files:**
- Create: `tests/agent_loop/test_deletion_gates.py`
- Modify: `tests/test_cutover_files.py` only if its exact allowlist must include the new modules

- [ ] **Step 1: Write the production AST/source validator**

Parse `src` and assert: no LangGraph imports/symbols; no old Agent entries/resume wrappers/reflection aliases/fallback claims; AgentDriver Protocol has one method named `execute`; Agent Loop imports no FastAPI/Starlette/repository/PilotRuntime/SSE; deterministic adapter is unreachable from loop; BoundProviderResponse production construction exists only in Gateway Session; no feature flag/shadow/dual path; no expanded compatibility-string parsing.

- [ ] **Step 2: Add rejecting negative fixtures**

Pass synthetic source strings containing each forbidden import/call/protocol method/direct constructor and assert the validator raises with that rule's stable identifier. This proves the gate is not an empty scan of current source.

- [ ] **Step 3: Add transient privacy/serialization gates**

Use canary credential/callback/owner values. Assert `repr`, pickle, `dataclasses.asdict`, JSON encoding, Journal capture, Runtime event projection, and log text cannot expose AgentLoopInvocation, ApprovedWriteSeed, port, Gateway attempt identity, model credentials, or operation executor.

- [ ] **Step 4: Run gates and repair only real violations**

```powershell
uv run pytest tests/agent_loop/test_deletion_gates.py tests/test_cutover_files.py -q
rg -n "langgraph|StateGraph|InMemorySaver|interrupt\(|__interrupt__|_GraphState|LangGraphAgentRunner|_resume_without_checkpoint|resume_after_confirm|_FALLBACK_CONFIRMATION_CLAIMS|_CONFIRMATION_LOCKS" src
```

Expected: tests pass and source scan has no match.

### Task 10: Run compatibility/release matrix and independent CR

**Files:**
- Modify only for verified defects: files already named in Tasks 2–9
- Create: `docs/reports/2026-08-23-agent-loop-unification-release-verification.md`

- [ ] **Step 1: Run focused backend matrix**

```powershell
uv run pytest tests/agent_loop tests/pilot_runtime tests/test_chat_api.py tests/test_ai_agent.py tests/test_context_projector.py tests/tool_pipeline tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py tests/test_agent_run_journal.py tests/test_agent_run_budget.py tests/test_journal_active_work_budget_gate.py -q
```

Expected: all collected tests pass; list any repository-approved platform skip.

- [ ] **Step 2: Run complete release gates**

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
Push-Location web
npm test -- --run
npm run build
Pop-Location
uv run oc smoke --static-dir web/dist
```

Expected: every available command exits 0. Report missing Docker, Provider credential, browser harness, or release-orchestrator variables explicitly instead of representing them as passed.

- [ ] **Step 3: Run repository local/controlled verification**

Use the repository's checked-in local verify and controlled real-AI commands without changing Provider settings. Verify workspace/application sync/stream, read+read, write approve/modify/reject, approve→read→final, chained Pending, terminal replay, delivery recovery, and SSE disconnect. Record only counts, fingerprints, stable status, and synthetic domain readback.

- [ ] **Step 4: Run built-in browser verification when the local app is available**

Check the same flows without asserting stochastic model text. Assert request/Provider/Tool count bounds, Pending/Ledger/domain state, SSE terminal state, page state after replay, and absence of duplicate requests. Keep screenshots outside the repository.

- [ ] **Step 5: Dispatch independent code review**

Provide the reviewer the approved design, fixed baseline, final diff, test evidence, and explicit P0/P1/P2 severity. Require review of: exposed-tool fail-closed timing; Gateway provenance; single loop/entry; approved source-load timing; claim-before-executor; sink-abort convergence; ToolCall selection matrix; rejection/replay/recovery Driver 0; Journal/fencing/late-result compatibility; old-path/dependency deletion; transient privacy.

- [ ] **Step 6: Resolve every P0/P1/P2**

For each finding, reproduce it with a failing test, verify RED, apply the smallest fix, and verify GREEN. Re-run the focused and affected release gates. A disputed finding is closed only with exact code/test evidence.

- [ ] **Step 7: Write the release report**

Record fixed baseline, characterization SHA, internal breaking changes, unchanged external contracts, every verification command and result, unavailable external gates, independent CR findings/resolutions, remaining risks, final commit, branch, unpushed/unmerged state, and exact `git status` output.

- [ ] **Step 8: Verify before final commit**

```powershell
git diff --check
git status --short --branch
git diff --stat aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb..HEAD
git diff --stat
```

Expected: no whitespace error and only the reviewed implementation/report files are present.

- [ ] **Step 9: Commit verified implementation and report**

```powershell
git add src tests pyproject.toml uv.lock docs/reports/2026-08-23-agent-loop-unification-release-verification.md
git commit -m "refactor: AI 统一 Agent Loop 执行路径"
```

- [ ] **Step 10: Verify final local state**

```powershell
git status --short --branch
git diff --check
git show --check --stat --oneline HEAD
```

Expected: clean worktree; checks exit 0; branch remains `refactor/20260823-agent-loop-unification`; no push or merge has occurred.

---

## Completion contract

Completion requires all of these observable facts:

- new turns and approved/modified confirmations enter one explicit Agent Loop;
- AgentDriver exposes only typed `execute()`;
- rejection, terminal replay, delivery recovery, and deterministic actions execute Driver/Projector/Provider zero times;
- unknown exposed-surface violations fail before assistant/message/event/dispatcher/executor work;
- Provider provenance validates call/fingerprint, ordinal range, and Session-owned non-empty attempt identity;
- exact multi-ToolCall, HITL, Pending, Ledger, Journal, Context, timeout/cancel/fencing, HTTP/SSE, and side-effect contracts pass;
- Graph/resume/reflection/fallback-claim code and unused LangGraph dependencies are absent;
- transient Invocation/Port/attempt state cannot be serialized, checkpointed, logged, or persisted;
- independent CR has no open P0/P1/P2;
- verification report states unavailable external gates and remaining risks without an exactly-once claim beyond the existing Ledger boundary;
- final worktree is clean, unpushed, and unmerged.
