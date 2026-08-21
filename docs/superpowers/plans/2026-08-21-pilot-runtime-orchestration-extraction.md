# Pilot Runtime Orchestration Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all Chat, streaming Chat, confirmation, and streaming confirmation orchestration from `api.py` into one transport-independent `PilotRuntime` while preserving every HTTP/SSE, Provider, Tool, Pending, Ledger, Journal, Context Surface, timeout, cancellation, and business-side-effect contract.

**Architecture:** `PilotRuntime` owns the causal application state machine and delegates the unchanged LangGraph loop through a narrow Transport-owned `AgentExecutionHost`. FastAPI-specific `chat_transport.py` owns HTTP/SSE rendering, worker/Future/Queue/cancel resources, `PreparedStreamGuard`, and title-signal finalization. The four routes switch to the new Runtime in one production cutover; no feature flag, shadow execution, old-path fallback, Agent Loop rewrite, Schema migration, or new capability is allowed.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pytest, SQLAlchemy 2, SQLite rollback journal, LangGraph, existing Tool Pipeline and Write Operation Ledger, Ruff, Mypy, PowerShell release gates.

---

## 0. Fixed execution boundary

Work only in:

```text
D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260821-pilot-runtime-orchestration
```

Branch:

```text
refactor/20260821-pilot-runtime-orchestration
```

Fixed source baseline:

```text
b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2
```

Reviewed design:

```text
docs/superpowers/specs/2026-08-21-pilot-runtime-orchestration-extraction-design.md
```

The commit containing this plan becomes the immutable implementation baseline. Do not edit the design or
plan during execution. Any contract change stops implementation and returns to design review.

### File responsibilities

```text
src/offerpilot/pilot_runtime/__init__.py
  Stable internal exports only.

src/offerpilot/pilot_runtime/contracts.py
  Request DTOs, RuntimeOutcome, RuntimeEvent, Prepared lifecycle, invocation control, protocols.

src/offerpilot/pilot_runtime/errors.py
  RuntimeCancelled, RuntimeTransportAborted, and closed product failure categories.

src/offerpilot/pilot_runtime/event_sink.py
  Safe event/cancel wrappers; no FastAPI or persistence.

src/offerpilot/pilot_runtime/persistence.py
  ChatMessage/Pending/clarification orchestration over existing ChatRepository methods.

src/offerpilot/pilot_runtime/deterministic.py
  Explicit bridge for current server-trusted deterministic Pilot actions and confirmations.

src/offerpilot/pilot_runtime/continuation.py
  Ledger-first confirmation, claim, heartbeat, replay, delivery, fallback, and fencing orchestration.

src/offerpilot/pilot_runtime/service.py
  Single state machine for start_turn, prepare_stream, execute_prepared_stream, and confirmation.

src/offerpilot/pilot_runtime/composition.py
  Construct PilotRuntime from existing repositories, model resolver, catalog, recorder factory, and clocks.

src/offerpilot/chat_transport.py
  FastAPI HTTP/SSE mapping, AgentExecutionHost, unbounded Queue, PreparedStreamGuard, guarded response.

src/offerpilot/api.py
  Request normalization, auth entry, Runtime invocation, and final FastAPI response only.
```

### Exact positive allowlist

```text
src/offerpilot/pilot_runtime/__init__.py
src/offerpilot/pilot_runtime/contracts.py
src/offerpilot/pilot_runtime/errors.py
src/offerpilot/pilot_runtime/event_sink.py
src/offerpilot/pilot_runtime/persistence.py
src/offerpilot/pilot_runtime/deterministic.py
src/offerpilot/pilot_runtime/continuation.py
src/offerpilot/pilot_runtime/service.py
src/offerpilot/pilot_runtime/composition.py
src/offerpilot/chat_transport.py
src/offerpilot/api.py
tests/pilot_runtime/__init__.py
tests/pilot_runtime/test_baseline_golden.py
tests/pilot_runtime/test_contracts.py
tests/pilot_runtime/test_event_sink.py
tests/pilot_runtime/test_transport.py
tests/pilot_runtime/test_execution_host.py
tests/pilot_runtime/test_persistence.py
tests/pilot_runtime/test_start_turn.py
tests/pilot_runtime/test_stream_preparation.py
tests/pilot_runtime/test_deterministic.py
tests/pilot_runtime/test_confirmation.py
tests/fixtures/pilot_runtime/baseline_golden.json
tests/test_pilot_runtime_extraction_gate.py
tests/test_chat_api.py
docs/reports/2026-08-21-pilot-runtime-orchestration-extraction-release-verification.md
```

Do not modify models, migrations, repositories, `ai/agent.py`, Tool Pipeline, Write Operation Ledger,
Context Projector, Journal implementation, Provider clients, frontend, README, configuration, or public
schemas. If an existing lower-layer interface is insufficient, stop and return to design review instead of
expanding the allowlist silently.

### Task 0: Persist the reviewed baseline and immutable scope gate

**Files:**
- Read: `docs/superpowers/specs/2026-08-21-pilot-runtime-orchestration-extraction-design.md`
- Read: `docs/superpowers/plans/2026-08-21-pilot-runtime-orchestration-extraction.md`
- Create outside repository: `%TEMP%\offerpilot-pilot-runtime-gate\baseline.txt`
- Create outside repository: `%TEMP%\offerpilot-pilot-runtime-gate\allowlist.txt`
- Create outside repository: `%TEMP%\offerpilot-pilot-runtime-gate.locator.json`

- [ ] **Step 1: Verify the implementation starts from the plan commit**

```powershell
$ErrorActionPreference = 'Stop'
$repoRoot = (Get-Location).Path
if ((git status --short).Count -ne 0) { throw 'implementation must start clean' }
$baseline = (git log -1 --format=%H -- docs/superpowers/plans/2026-08-21-pilot-runtime-orchestration-extraction.md).Trim()
if (-not $baseline) { throw 'reviewed plan commit is missing' }
if ((git rev-parse HEAD).Trim() -ne $baseline) { throw 'HEAD must equal the plan commit' }
if ((git cat-file -t $baseline).Trim() -ne 'commit') { throw 'baseline is not a commit' }
```

Expected: exit code 0, no output.

- [ ] **Step 2: Persist baseline, exact allowlist, and hash outside the worktree**

```powershell
$gateRoot = Join-Path $env:TEMP 'offerpilot-pilot-runtime-gate'
$locatorPath = Join-Path $env:TEMP 'offerpilot-pilot-runtime-gate.locator.json'
New-Item -ItemType Directory -Force -Path $gateRoot | Out-Null
$allowlist = @(
  'src/offerpilot/pilot_runtime/__init__.py',
  'src/offerpilot/pilot_runtime/contracts.py',
  'src/offerpilot/pilot_runtime/errors.py',
  'src/offerpilot/pilot_runtime/event_sink.py',
  'src/offerpilot/pilot_runtime/persistence.py',
  'src/offerpilot/pilot_runtime/deterministic.py',
  'src/offerpilot/pilot_runtime/continuation.py',
  'src/offerpilot/pilot_runtime/service.py',
  'src/offerpilot/pilot_runtime/composition.py',
  'src/offerpilot/chat_transport.py',
  'src/offerpilot/api.py',
  'tests/pilot_runtime/__init__.py',
  'tests/pilot_runtime/test_baseline_golden.py',
  'tests/pilot_runtime/test_contracts.py',
  'tests/pilot_runtime/test_event_sink.py',
  'tests/pilot_runtime/test_transport.py',
  'tests/pilot_runtime/test_execution_host.py',
  'tests/pilot_runtime/test_persistence.py',
  'tests/pilot_runtime/test_start_turn.py',
  'tests/pilot_runtime/test_stream_preparation.py',
  'tests/pilot_runtime/test_deterministic.py',
  'tests/pilot_runtime/test_confirmation.py',
  'tests/fixtures/pilot_runtime/baseline_golden.json',
  'tests/test_pilot_runtime_extraction_gate.py',
  'tests/test_chat_api.py',
  'docs/reports/2026-08-21-pilot-runtime-orchestration-extraction-release-verification.md'
)
$baselinePath = Join-Path $gateRoot 'baseline.txt'
$allowlistPath = Join-Path $gateRoot 'allowlist.txt'
$baseline | Set-Content -LiteralPath $baselinePath -Encoding ascii -NoNewline
$allowlist | Set-Content -LiteralPath $allowlistPath -Encoding utf8
$allowlistHash = [Convert]::ToHexString(
  [Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes($allowlistPath))
).ToLowerInvariant()
[ordered]@{
  repository_root = $repoRoot
  baseline_path = $baselinePath
  baseline_sha = $baseline
  source_baseline_sha = 'b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2'
  allowlist_path = $allowlistPath
  allowlist_sha256 = $allowlistHash
} | ConvertTo-Json | Set-Content -LiteralPath $locatorPath -Encoding utf8
```

Expected: three gate files exist outside the repository.

- [ ] **Step 3: Run the reusable scope assertion**

```powershell
$locator = Get-Content -Raw (Join-Path $env:TEMP 'offerpilot-pilot-runtime-gate.locator.json') | ConvertFrom-Json
if ((Get-Location).Path -ne [string]$locator.repository_root) { throw 'wrong worktree' }
$baseline = (Get-Content -Raw -LiteralPath $locator.baseline_path).Trim()
if ($baseline -ne [string]$locator.baseline_sha) { throw 'baseline file changed' }
$hash = [Convert]::ToHexString(
  [Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes([string]$locator.allowlist_path))
).ToLowerInvariant()
if ($hash -ne [string]$locator.allowlist_sha256) { throw 'allowlist changed' }
$allowed = @{}
Get-Content -LiteralPath $locator.allowlist_path | ForEach-Object {
  $allowed[$_.Trim().Replace('\','/')] = $true
}
$paths = @(
  git diff --name-only "$baseline..HEAD"
  git diff --cached --name-only
  git diff --name-only
  git ls-files --others --exclude-standard
) | Where-Object { $_ } | ForEach-Object { $_.Trim().Replace('\','/') } | Sort-Object -Unique
$outside = @($paths | Where-Object { -not $allowed.ContainsKey($_) })
if ($outside.Count) { throw "outside allowlist: $($outside -join ', ')" }
```

Expected: exit code 0. Re-run after every task; never recompute the baseline.

---

### Task 1: Freeze baseline route and SSE characterization

**Files:**
- Create: `tests/pilot_runtime/__init__.py`
- Create: `tests/pilot_runtime/test_baseline_golden.py`
- Create: `tests/fixtures/pilot_runtime/baseline_golden.json`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write the failing read-only golden test**

Create `tests/pilot_runtime/test_baseline_golden.py`:

```python
from __future__ import annotations

import json
from pathlib import Path


GOLDEN = Path(__file__).parents[1] / "fixtures" / "pilot_runtime" / "baseline_golden.json"


def test_pilot_runtime_baseline_golden_is_canonical_and_pinned() -> None:
    raw = GOLDEN.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schema_version"] == 1
    assert value["source_baseline"] == "b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2"
    assert raw == json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    assert value["sse_sequences"]["initial_model"] == [
        "meta", "user_message_saved", "status", "assistant_message", "completed"
    ]
    assert value["sse_sequences"]["hitl_entry"] == [
        "meta", "user_message_saved", "status", "tool_call", "status",
        "confirmation_required", "completed"
    ]
    assert value["sse_sequences"]["hitl_confirm"] == [
        "meta", "status", "tool_call", "tool_result", "assistant_message", "completed"
    ]
    assert value["preheader_http"]["source_load_failed"] == {
        "error_code": "source_load_failed", "status": 503
    }
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_baseline_golden.py -q
```

Expected: FAIL because `baseline_golden.json` does not exist.

- [ ] **Step 3: Create the immutable canonical asset**

Create the JSON with this exact semantic content and serialize it using UTF-8, sorted keys, compact
separators, one trailing newline:

```json
{
  "required_existing_tests": [
    "test_chat_stream_emits_pilot_sse_v1_sequence",
    "test_deterministic_pilot_jd_stream_uses_fixed_events_without_model",
    "test_journal_hitl_pending_approve_executes_once_and_finishes_healthy",
    "test_journal_hitl_pending_reject_records_ledger_and_no_tool_execution",
    "test_journal_hitl_chained_pending_keeps_ledger_and_run_causal",
    "test_chat_confirm_timeout_after_write_returns_completed_fallback",
    "test_chat_confirm_stream_cancel_persists_tool_result",
    "test_chat_fails_closed_before_model_for_invalid_application_scope"
  ],
  "schema_version": 1,
  "source_baseline": "b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2",
  "sse_sequences": {
    "hitl_chain_confirm": ["meta", "status", "tool_call", "tool_call", "tool_result", "status", "confirmation_required", "completed"],
    "hitl_confirm": ["meta", "status", "tool_call", "tool_result", "assistant_message", "completed"],
    "hitl_entry": ["meta", "user_message_saved", "status", "tool_call", "status", "confirmation_required", "completed"],
    "initial_model": ["meta", "user_message_saved", "status", "assistant_message", "completed"]
  },
  "preheader_http": {
    "source_load_failed": {"error_code": "source_load_failed", "status": 503},
    "stale_pending_action": {"error_code": "stale_pending_action", "status": 409}
  }
}
```

Do not add a generator, update flag, approval flag, or overwrite command to production tests.

- [ ] **Step 4: Add exact baseline assertions to existing route tests**

In `tests/test_chat_api.py`, keep the existing full response and database assertions and add a helper that
loads the read-only asset. Replace duplicated event-name literals only where the loaded list is byte-for-byte
the existing expectation. Do not weaken payload, Provider-call, Ledger, Journal, or side-effect assertions.

```python
def _pilot_runtime_baseline_golden() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "pilot_runtime" / "baseline_golden.json"
    return json.loads(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Run characterization tests and verify GREEN**

```powershell
uv run pytest tests/pilot_runtime/test_baseline_golden.py tests/test_chat_api.py -q
```

Expected: all collected tests pass; no skips are introduced.

- [ ] **Step 6: Commit the immutable characterization**

```powershell
git add tests/pilot_runtime/__init__.py tests/pilot_runtime/test_baseline_golden.py tests/fixtures/pilot_runtime/baseline_golden.json tests/test_chat_api.py
git commit -m "test: AI 固化 Pilot Runtime 基线行为"
```

---

### Task 2: Define closed Runtime contracts and lifecycle CAS

**Files:**
- Create: `src/offerpilot/pilot_runtime/__init__.py`
- Create: `src/offerpilot/pilot_runtime/contracts.py`
- Create: `src/offerpilot/pilot_runtime/errors.py`
- Create: `tests/pilot_runtime/test_contracts.py`

- [ ] **Step 1: Write failing contract and lifecycle tests**

Create tests that instantiate every request/outcome/event type and prove the exact four-state machine:

```python
def test_prepared_lifecycle_accepts_only_reviewed_transitions() -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.begin() is True
    assert lifecycle.complete(CompletionReason.NORMAL) is True
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.NORMAL
    assert lifecycle.complete(CompletionReason.CANCELLED) is False
    assert lifecycle.abort_if_prepared() is False


def test_before_start_abort_has_no_completion_reason() -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.abort_if_prepared() is True
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert lifecycle.completion_reason is None
    assert lifecycle.begin() is False


@pytest.mark.parametrize(
    "reason",
    [CompletionReason.NORMAL, CompletionReason.CANCELLED, CompletionReason.TRANSPORT_ABORTED],
)
def test_completion_cleanup_winner_is_unique(reason: CompletionReason) -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.begin() is True
    assert lifecycle.complete(reason) is True
    assert lifecycle.complete(reason) is False
```

Also assert DTOs reject FastAPI objects, arbitrary event payloads, mutable mappings, and invalid
`completion_reason`/state combinations. Add a closed `FirstModelCompletedSignal` and prove that title
eligibility can only be represented by that signal, never by a free-form callback payload.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_contracts.py -q
```

Expected: import failure because `offerpilot.pilot_runtime` does not exist.

- [ ] **Step 3: Implement the exact closed types**

Implement frozen/slot dataclasses and string enums:

```python
class PreparedLifecycleState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    ABORTED = "aborted"
    COMPLETED = "completed"


class CompletionReason(StrEnum):
    NORMAL = "normal"
    CANCELLED = "cancelled"
    TRANSPORT_ABORTED = "transport_aborted"


class PreparationKind(StrEnum):
    MODEL = "model"
    DETERMINISTIC_INITIAL = "deterministic_initial"
    DETERMINISTIC_CONFIRMATION = "deterministic_confirmation"
    CONFIRMATION = "confirmation"
    REPLAY = "replay"


class StreamExecutionMode(StrEnum):
    DIRECT = "direct"
    AGENT_HOST = "agent_host"
```

`PreparedLifecycle` must use one `threading.Lock`; `begin`, `abort_if_prepared`, and `complete` return
`True` only for the single CAS winner. `complete` is valid only from `EXECUTING` and writes state plus
reason under the same lock. Store no exception or free-form diagnostic.

Define frozen request DTOs, `RuntimeTransportContext`, `ImmediateHttpOutcome`, the five RuntimeOutcome
variants, typed RuntimeEvent dataclasses including `UserMessageSavedEvent(role: Literal["user"] = "user")`,
and protocols for `RuntimeEventSink`, `RuntimeSignalSink`, `AgentExecutionHost`, and
`RuntimeInvocationControl`. `RuntimeSignalSink.try_emit()` is non-blocking and returns only the closed
`emitted | duplicate | closed | full | degraded` result enum.

- [ ] **Step 4: Run unit tests and type checks**

```powershell
uv run pytest tests/pilot_runtime/test_contracts.py -q
uv run ruff check src/offerpilot/pilot_runtime tests/pilot_runtime/test_contracts.py
uv run mypy src/offerpilot/pilot_runtime
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit contracts**

```powershell
git add src/offerpilot/pilot_runtime/__init__.py src/offerpilot/pilot_runtime/contracts.py src/offerpilot/pilot_runtime/errors.py tests/pilot_runtime/test_contracts.py
git commit -m "feat: AI 定义 Pilot Runtime 封闭契约"
```

---

### Task 3: Implement control-flow-safe event delivery and guarded SSE lifecycle

**Files:**
- Create: `src/offerpilot/pilot_runtime/event_sink.py`
- Create: `src/offerpilot/chat_transport.py`
- Create: `tests/pilot_runtime/test_event_sink.py`
- Create: `tests/pilot_runtime/test_transport.py`

- [ ] **Step 1: Write failing event/control tests**

```python
def test_safe_emit_maps_sink_exception_to_transport_abort() -> None:
    sink = CallableRuntimeEventSink(lambda _event: (_ for _ in ()).throw(OSError("closed")))
    with pytest.raises(RuntimeTransportAborted):
        emit_runtime_event(sink, StatusEvent(phase="model_running", label="正在思考"))


def test_user_message_saved_payload_is_closed() -> None:
    assert runtime_event_payload(UserMessageSavedEvent()) == {"role": "user"}


def test_cancel_reason_maps_without_product_error() -> None:
    control = InMemoryRuntimeInvocationControl()
    assert control.request_cancel(CancelReason.CLIENT_DISCONNECT) is True
    with pytest.raises(RuntimeCancelled):
        require_runtime_active(control)
```

Assert `KeyboardInterrupt` and `SystemExit` propagate unchanged and no control exception can be rendered as
`RuntimeFailureOutcome`.

Add capacity-one signal-latch tests for first emit, duplicate, full, closed, consumer exit, and an injected
ordinary sink exception. In every failure case the Agent Outcome and Runtime Event stream are unchanged;
only title generation is omitted. `BaseException` still propagates after owner cleanup.

- [ ] **Step 2: Write failing Guard and response tests**

Use a real Starlette ASGI call harness. Cover response construction failure, response constructed but body
iterator never entered, first-iteration disconnect, normal completion, duplicate body/background finalizer,
and begin/abort barrier races. The central assertion is:

```python
assert runtime.begin_calls + runtime.abort_calls == 1
assert runtime.cleanup_calls == 1
assert runtime.execute_calls in {0, 1}
```

For the never-entered body case, invoke `GuardedStreamingResponse.__call__` with a receive callable that
immediately returns `{"type": "http.disconnect"}` and assert lifecycle becomes `ABORTED`.

- [ ] **Step 3: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_event_sink.py tests/pilot_runtime/test_transport.py -q
```

Expected: import failures for `event_sink` and `chat_transport`.

- [ ] **Step 4: Implement safe event wrappers and transport renderers**

`emit_runtime_event` must catch `RuntimeCancelled`/`RuntimeTransportAborted` and re-raise, convert other
ordinary sink exceptions to `RuntimeTransportAborted`, and never catch `BaseException`. Implement pure
Outcome-to-HTTP and Event-to-SSE payload functions; only `chat_transport.py` may import FastAPI/Starlette.

Implement the capacity-one `RuntimeSignalSink` as a non-blocking, fail-open latch. It stores only the closed
signal enum, never model text or a Provider response, and cannot mutate Runtime Outcome, Journal, or Event
delivery.

Implement `PreparedStreamGuard` so all begin/abort/complete calls delegate to the shared Runtime lifecycle.
Implement `GuardedStreamingResponse.__call__` with an outer `try/finally` that calls
`guard.abort_if_prepared()` even when the body iterator was never entered. Register an idempotent response
background finalizer; do not use `__del__` or weakref timing.

- [ ] **Step 5: Run focused tests**

```powershell
uv run pytest tests/pilot_runtime/test_event_sink.py tests/pilot_runtime/test_transport.py -q
uv run ruff check src/offerpilot/pilot_runtime/event_sink.py src/offerpilot/chat_transport.py tests/pilot_runtime/test_event_sink.py tests/pilot_runtime/test_transport.py
uv run mypy src/offerpilot/pilot_runtime src/offerpilot/chat_transport.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit transport primitives**

```powershell
git add src/offerpilot/pilot_runtime/event_sink.py src/offerpilot/chat_transport.py tests/pilot_runtime/test_event_sink.py tests/pilot_runtime/test_transport.py
git commit -m "feat: AI 建立 Pilot Runtime 流式传输边界"
```

---

### Task 4: Extract the baseline AgentExecutionHost without changing timeout semantics

**Files:**
- Modify: `src/offerpilot/chat_transport.py`
- Create: `tests/pilot_runtime/test_execution_host.py`
- Read unchanged: `src/offerpilot/api.py:10415-10455`

- [ ] **Step 1: Write failing sync host tests**

Use a scripted clock/event and a blocking thunk. Assert:

```python
def test_sync_host_times_only_agent_thunk() -> None:
    host = SyncAgentExecutionHost(timeout_seconds=0.05)
    assert host.run(lambda: "ok", InMemoryRuntimeInvocationControl()) == "ok"


def test_sync_host_timeout_does_not_wait_for_uncancellable_worker() -> None:
    release = Event()
    host = SyncAgentExecutionHost(timeout_seconds=0.01)
    started = perf_counter()
    with pytest.raises(RuntimeAgentTimedOut):
        host.run(lambda: release.wait(5), InMemoryRuntimeInvocationControl())
    assert perf_counter() - started < 0.2
    release.set()
```

Assert a late result cannot change invocation state or invoke a second thunk.

- [ ] **Step 2: Write failing SSE host tests**

Assert the host constructs exactly `Queue()` with no `maxsize`, forwards events in order, retains the 0.1
second poll behavior, sets cancel in `finally`, uses one worker, and cannot block on a deliberately slow
consumer. Verify `queue.maxsize == 0`.

- [ ] **Step 3: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_execution_host.py -q
```

Expected: host classes are missing.

- [ ] **Step 4: Move the exact baseline worker mechanics**

Move the mechanics of `_run_chat_agent_with_timeout` and `_run_chat_agent_with_sse_events` into
`SyncAgentExecutionHost` and `SseAgentExecutionHost`. Keep:

```python
CHAT_AGENT_TIMEOUT_SECONDS = 120.0
event_queue: Queue[RuntimeEvent | object] = Queue()
poll_seconds = 0.1
ThreadPoolExecutor(max_workers=1)
executor.shutdown(wait=False, cancel_futures=...)
```

The host owns worker/Future/Queue/cancel/deadline only. It receives one Agent thunk and returns its typed
result or raises closed timeout/control exceptions. It must not import repositories, Journal, Pending,
Ledger, tool result renderers, or application models.

- [ ] **Step 5: Run focused and existing streaming tests**

```powershell
uv run pytest tests/pilot_runtime/test_execution_host.py tests/test_chat_api.py -q
```

Expected: all pass; the production routes still use their old helpers at this intermediate commit.

- [ ] **Step 6: Commit host extraction**

```powershell
git add src/offerpilot/chat_transport.py tests/pilot_runtime/test_execution_host.py
git commit -m "refactor: AI 提取 Agent 执行宿主"
```

---

### Task 5: Build the Chat persistence coordinator over existing repository atoms

**Files:**
- Create: `src/offerpilot/pilot_runtime/persistence.py`
- Create: `tests/pilot_runtime/test_persistence.py`
- Read unchanged: `src/offerpilot/repositories/chat.py:169-738`
- Read unchanged: `src/offerpilot/api.py:10611-10924`

- [ ] **Step 1: Write failing persistence tests**

With a temporary SQLite database, cover:

```python
def test_initial_pending_persists_atomic_tool_chain_and_pending(tmp_path: Path) -> None:
    coordinator = make_persistence_coordinator(tmp_path)
    outcome = coordinator.persist_initial_pending(conversation_id, messages, pending)
    assert outcome.persisted is True
    assert coordinator.chat.get_pending_action(conversation_id).tool_call_id == pending.tool_call_id
    assert persisted_roles(coordinator.chat, conversation_id)[-2:] == ["assistant", "tool"]


def test_confirmation_delivery_atomically_replaces_chained_pending(tmp_path: Path) -> None:
    result = coordinator.persist_confirmation_delivery(
        conversation_id, ownership, origin_tool_message, continuation, chained_pending
    )
    assert result.delivery_outcome == "chained_pending"
    assert coordinator.chat.get_pending_action(conversation_id) == chained_pending
```

Also test archived conversation, CAS loss, clarification set/clear, duplicate delivery, and rollback with no
partial messages.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_persistence.py -q
```

Expected: `ChatPersistenceCoordinator` is missing.

- [ ] **Step 3: Implement a narrow coordinator**

Call the current `ChatRepository` atomic methods; do not create a new generic transaction or duplicate SQL.
Expose typed methods for initial messages, pending, clarification, timeout message, confirmation continuation,
fallback, and replay delivery. Return sealed persistence results rather than dictionaries. Preserve message
roles, tool_call_id, operation_id/delivery_kind/delivery_ordinal, and current CAS behavior.

- [ ] **Step 4: Run persistence and repository tests**

```powershell
uv run pytest tests/pilot_runtime/test_persistence.py tests/test_chat_repository.py tests/test_chat_api.py -q
```

Expected: all collected tests pass.

- [ ] **Step 5: Commit persistence coordinator**

```powershell
git add src/offerpilot/pilot_runtime/persistence.py tests/pilot_runtime/test_persistence.py
git commit -m "refactor: AI 收束 Chat 持久化编排"
```

---

### Task 6: Implement the sync model Start Turn state machine

**Files:**
- Create: `src/offerpilot/pilot_runtime/service.py`
- Create: `tests/pilot_runtime/test_start_turn.py`
- Read unchanged: `src/offerpilot/api.py:5479-5746`
- Read unchanged: `src/offerpilot/ai/agent.py:233-844`

- [ ] **Step 1: Write failing state-order tests with strict fakes**

Create fakes that append phase names to a list and reject unexpected calls. Lock this exact sync sequence:

```python
assert phases == [
    "validate",
    "conversation",
    "route:model",
    "pending_guard",
    "model_resolve",
    "user_persist",
    "run_start",
    "source_load",
    "context_assemble",
    "agent_host",
    "result_normalize",
    "message_persist",
    "run_finish",
]
```

Add failure tests for conversation missing, live Pending, model unconfigured, source failure, Agent timeout,
Provider failure, pending, missing target clarification, archived conversation, and Journal degraded. Assert
exact Provider/Tool/persistence counts.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_start_turn.py -q
```

Expected: `PilotRuntime` is missing.

- [ ] **Step 3: Implement sync model flow only**

Implement `PilotRuntime.start_turn` using injected protocols. It must:

1. create/load Conversation;
2. perform trusted route selection and live Pending guard;
3. resolve current model/config once;
4. persist user message;
5. start Journal Run/Segment before Source load;
6. load/assemble the current frozen source/messages through existing helpers injected by composition;
7. call the unchanged Agent Driver only through `SyncAgentExecutionHost`;
8. use `ChatPersistenceCoordinator` for pending/clarification/messages;
9. finish/suspend Journal through existing recorder methods;
10. return typed Outcome.

Do not implement deterministic or confirmation branches in this task; return a private test-only unsupported
route failure before side effects for those branches. The production API is not switched yet.

- [ ] **Step 4: Run sync Runtime tests and unchanged API suite**

```powershell
uv run pytest tests/pilot_runtime/test_start_turn.py tests/test_chat_api.py -q
uv run ruff check src/offerpilot/pilot_runtime/service.py tests/pilot_runtime/test_start_turn.py
uv run mypy src/offerpilot/pilot_runtime
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit sync Runtime**

```powershell
git add src/offerpilot/pilot_runtime/service.py tests/pilot_runtime/test_start_turn.py
git commit -m "feat: AI 建立同步 Pilot Runtime 状态机"
```

---

### Task 7: Implement stream preparation, direct execution, and abort convergence

**Files:**
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Modify: `src/offerpilot/chat_transport.py`
- Create: `tests/pilot_runtime/test_stream_preparation.py`

- [ ] **Step 1: Write failing preheader order tests**

Lock stream model order separately from sync:

```python
assert phases == [
    "validate",
    "conversation",
    "route:model",
    "pending_guard",
    "model_resolve",
    "user_persist",
    "source_load",
    "context_assemble",
    "transport_identity",
    "run_start",
    "context_capture",
    "prepared",
]
```

Assert source failure returns `ImmediateHttpOutcome(status_code=503)` with worker/Queue/Provider/Tool = 0.
Assert invalid deterministic/confirmation prechecks return immediate HTTP and never build a response.

- [ ] **Step 2: Write failing abort-kind tests**

For each preparation kind, seed the facts present after prepare, abort through the Guard, and assert:

```text
model: user message remains; no assistant/Pending/Ledger/domain write
deterministic_initial: existing Chat/Pending/result remains; action count unchanged
deterministic_confirmation: Ledger/domain terminal remains; no undo/re-execution/delivery
replay: Provider/Projector/executor remain zero; terminal fact unchanged
```

Assert direct execution does not construct an Agent worker or Queue.

- [ ] **Step 3: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_stream_preparation.py -q
```

Expected: `prepare_stream`/`execute_prepared_stream` are missing.

- [ ] **Step 4: Implement preparation and execution methods**

Implement `prepare_stream` with the reviewed order and `PreparedStreamExecution` with shared lifecycle.
Implement `execute_prepared_stream`:

```text
DIRECT → emit precomputed typed events/outcome; Agent host count = 0
AGENT_HOST → emit meta/user_message_saved/status as baseline, call host once, persist via Runtime
```

Create no active ORM/Session inside Prepared state. If Source is loaded before preparation returns, store only
immutable typed/JSON DTOs. Implement preparation-kind abort callbacks with no reversal of committed facts.

- [ ] **Step 5: Run stream and Guard tests**

```powershell
uv run pytest tests/pilot_runtime/test_stream_preparation.py tests/pilot_runtime/test_transport.py tests/pilot_runtime/test_execution_host.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit stream preparation**

```powershell
git add src/offerpilot/pilot_runtime/service.py src/offerpilot/chat_transport.py tests/pilot_runtime/test_stream_preparation.py
git commit -m "feat: AI 实现 Pilot Runtime 流式准备阶段"
```

---

### Task 8: Move deterministic Pilot actions behind the explicit bridge

**Files:**
- Create: `src/offerpilot/pilot_runtime/deterministic.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Create: `tests/pilot_runtime/test_deterministic.py`
- Read unchanged during extraction: `src/offerpilot/api.py:1889-2480`

- [ ] **Step 1: Write failing deterministic boundary tests**

Cover initial sync/stream, clarification, Pending replay, approve, edit, reject, stale CAS, and retry. Assert:

```python
assert provider.calls == []
assert model_catalog_resolutions == []
assert projector.calls == []
assert deterministic_executor.calls == 1
```

For stream initial success assert exact sequence starts
`meta, user_message_saved, status`; for direct deterministic confirmation assert no Agent worker/Queue.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_deterministic.py -q
```

Expected: `DeterministicPilotAdapter` is missing.

- [ ] **Step 3: Move exact existing behavior into the bridge**

Move the bodies of `_deterministic_pilot_chat`, `_deterministic_pilot_confirmation`, and their Journal/
response preparation helpers from `api.py` into a dependency-injected class. Preserve existing trusted route
selection, closed tool names, token/CAS/edit rules, idempotency keys, writes, replay, and messages. The bridge
returns typed Runtime outcomes/events and never receives `MODEL_TOOL_CATALOG` or Provider interfaces.

- [ ] **Step 4: Run deterministic and existing API regressions**

```powershell
uv run pytest tests/pilot_runtime/test_deterministic.py tests/test_deterministic_pilot_actions.py tests/test_chat_api.py -q
```

Expected: all pass before route cutover because the old API path remains active.

- [ ] **Step 5: Commit deterministic bridge**

```powershell
git add src/offerpilot/pilot_runtime/deterministic.py src/offerpilot/pilot_runtime/service.py tests/pilot_runtime/test_deterministic.py
git commit -m "refactor: AI 隔离确定性 Pilot 编排"
```

---

### Task 9: Move confirmation and Ledger delivery coordination into Runtime

**Files:**
- Create: `src/offerpilot/pilot_runtime/continuation.py`
- Modify: `src/offerpilot/pilot_runtime/service.py`
- Create: `tests/pilot_runtime/test_confirmation.py`
- Read unchanged during extraction: `src/offerpilot/api.py:6109-7390`
- Read unchanged: `src/offerpilot/ai/write_operations.py:130-1080`

- [ ] **Step 1: Write failing Ledger-first and reject tests**

Assert terminal replay is checked before Pending and runs Provider/Projector/executor zero times. Assert reject
uses only token/Pending/Ledger identity and rejection CAS; Schema/decode/capability/binding/preflight/target
Repository/Provider/executor counts are all zero.

- [ ] **Step 2: Write failing approve/modify/delivery tests**

Cover mutable recheck under lock, claim before executor, one executor call, commit unknown reconciliation,
required undo, continuation source reload, read-tool continuation, chained Pending, origin tool-result event
deferral, delivery heartbeat, active owner, takeover, late Bundle, fallback, and CAS loss.

Use two-connection barriers for claim/delivery races and assert one winner.

- [ ] **Step 3: Write failing timeout/control tests**

Cover timeout before claim, during executor, after terminal, during continuation, client disconnect, sink abort,
and uncancellable late Provider return. Assert timeout/control never becomes Provider product failure and late
non-write messages/Pending are not persisted.

- [ ] **Step 4: Run tests and verify RED**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py -q
```

Expected: `ConfirmationCoordinator` is missing.

- [ ] **Step 5: Move exact confirmation coordination**

Move the Route-local callbacks and state currently responsible for request fingerprint, result recorder,
confirmation attempt, WriteOperationCoordinator execution, heartbeat, fallback, replay, delivery fence, and
continuation persistence into `ConfirmationCoordinator`. Use sealed dataclasses instead of the current
`dict[str, Any] confirmed_outcome`. Keep `resume_after_confirm` unchanged behind Agent Driver.

The coordinator must expose one Runtime-facing method per reviewed branch: terminal replay, proposed approve/
modify, proposed reject, timeout convergence, cancellation cleanup, and final delivery.

- [ ] **Step 6: Run focused Ledger/HITL matrix**

```powershell
uv run pytest tests/pilot_runtime/test_confirmation.py tests/test_write_operations.py tests/test_write_operation_acceptance_matrix.py tests/test_chat_api.py -q
```

Expected: all collected tests pass. These are the exact Ledger files present at the fixed source baseline;
do not replace them with a guessed compatibility filename.

- [ ] **Step 7: Commit confirmation coordinator**

```powershell
git add src/offerpilot/pilot_runtime/continuation.py src/offerpilot/pilot_runtime/service.py tests/pilot_runtime/test_confirmation.py
git commit -m "refactor: AI 收束确认与 Ledger 编排"
```

---

### Task 10: Compose PilotRuntime and atomically cut over all four routes

**Files:**
- Create: `src/offerpilot/pilot_runtime/composition.py`
- Modify: `src/offerpilot/pilot_runtime/__init__.py`
- Modify: `src/offerpilot/api.py`
- Modify: `src/offerpilot/chat_transport.py`
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Write failing composition and route-spy tests**

Monkeypatch one PilotRuntime spy and assert each endpoint calls exactly one method:

```text
POST /api/chat                 → start_turn
POST /api/chat/stream          → prepare_stream, then execute_prepared_stream only for Prepared
POST /api/chat/confirm         → continue_confirmation
POST /api/chat/confirm/stream  → prepare_stream, then execute_prepared_stream only for Prepared
```

Assert Immediate HTTP creates no StreamingResponse/worker/Queue. Assert route renderer failure does not call
Runtime twice. Cover title qualification and finalization for sync success, sync later failure, Pending,
SSE success, immediate disconnect, cancellation, duplicate/late signal, registration failure, and first-model
failure. The Transport owner must drain/close once; only a first complete valid model response can register
one title task, while replay/reject/deterministic/pre-model failure register none.

- [ ] **Step 2: Run route-spy tests and verify RED**

```powershell
uv run pytest tests/test_chat_api.py -q -k "pilot_runtime_route or stream_preheader"
```

Expected: new spy assertions fail because routes still call old orchestration.

- [ ] **Step 3: Implement composition root**

`build_pilot_runtime(...)` must receive existing create_app dependencies and construct one Runtime. It may
wrap existing private pure helpers as injected callables during this cutover, but all side-effect orchestration
must live in Runtime modules. No dynamic config/Provider reread beyond current Segment rules.

- [ ] **Step 4: Switch all four routes in one patch**

Keep Route responsibilities to:

```text
normalize payload/auth
build typed request and Transport context
call PilotRuntime
render Immediate HTTP, RuntimeOutcome, or guarded SSE
finalize title signal exactly once
```

The sync response-assembly `finally` and SSE Transport-owner finalizer are the only signal consumers. Model
worker/Runtime code never receives FastAPI `BackgroundTasks`; registration failure is terminal for the title
state and never changes or retries the Agent result.

Delete Route-local model/Journal/Pending/Ledger/delivery/timeout callback closures and the old
`_run_chat_agent_with_timeout` / `_run_chat_agent_with_sse_events` helpers after all call sites move to
`AgentExecutionHost`. Delete moved deterministic and confirmation helpers; do not leave aliases, façades,
feature flags, or fallback imports.

- [ ] **Step 5: Run the complete four-route compatibility matrix**

```powershell
uv run pytest tests/pilot_runtime tests/test_chat_api.py tests/test_ai_agent.py tests/test_context_projector.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py -q
```

Expected: all pass with exact baseline HTTP/SSE/Provider/Tool/Pending/Ledger/Journal/Surface behavior.

- [ ] **Step 6: Commit the atomic production cutover**

```powershell
git add src/offerpilot/pilot_runtime src/offerpilot/chat_transport.py src/offerpilot/api.py tests/pilot_runtime tests/test_chat_api.py
git commit -m "refactor: AI 切换统一 Pilot Runtime 编排"
```

---

### Task 11: Add mechanical deletion, ownership, serialization, and privacy gates

**Files:**
- Create: `tests/test_pilot_runtime_extraction_gate.py`

- [ ] **Step 1: Write failing AST/source gates**

The gate must parse production AST and fail unless:

```text
four Route functions do not reference run_turn/resume_after_confirm/ContextSourceLoader/RunRecorder/
WriteOperationCoordinator/Pending claim/DeliveryHeartbeat/Chat persistence helpers

pilot_runtime does not import FastAPI/Starlette response/background tasks

chat_transport is the only new owner of ThreadPoolExecutor/Future/Queue/Event/format_sse/seq/Guard

Queue construction has no maxsize argument

AgentExecutionHost imports no repository/Journal/Pending/Ledger modules

every PreparedStreamExecution is wrapped by PreparedStreamGuard

RuntimeEvent union includes UserMessageSavedEvent with only role=user

no old helper aliases, feature flags, shadow execution, fallback, startswith("错误：") expansion,
or model-to-Legacy routing exists
```

Add negative source snippets to prove each validator fails on a forbidden pattern.

- [ ] **Step 2: Write failing serialization/privacy gates**

Walk all new dataclasses and assert Prepared state, lifecycle, invocation control, execution host, sink,
exception, credential, repository, ORM, Session, binding audit, and outcome records cannot enter Graph State,
ChatMessage, Pending, Ledger, Journal, JSON logs, or generic `asdict` serialization. Include canary strings and
assert none appear in Journal/Trace/SSE/error logs.

- [ ] **Step 3: Run gates and verify RED**

```powershell
uv run pytest tests/test_pilot_runtime_extraction_gate.py -q
```

Expected: at least one old Route/helper ownership assertion fails before final deletions are complete.

- [ ] **Step 4: Remove every remaining old path and satisfy gates**

Delete, do not allowlist, any remaining Route-side orchestration or duplicate helper. Tighten imports and
exports until every negative fixture and production scan passes. Do not change behavior to satisfy the gate.

- [ ] **Step 5: Run static gates and focused suites**

```powershell
uv run pytest tests/test_pilot_runtime_extraction_gate.py tests/pilot_runtime tests/test_chat_api.py -q
uv run ruff check src/offerpilot/pilot_runtime src/offerpilot/chat_transport.py src/offerpilot/api.py tests/pilot_runtime tests/test_pilot_runtime_extraction_gate.py tests/test_chat_api.py
uv run mypy src/offerpilot/pilot_runtime src/offerpilot/chat_transport.py src/offerpilot/api.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit mechanical gates and cleanup**

```powershell
git add src/offerpilot/api.py src/offerpilot/pilot_runtime src/offerpilot/chat_transport.py tests/test_pilot_runtime_extraction_gate.py
git commit -m "test: AI 固化 Pilot Runtime 提取门禁"
```

---

### Task 12: Full verification, independent CR, and release report

**Files:**
- Create: `docs/reports/2026-08-21-pilot-runtime-orchestration-extraction-release-verification.md`
- Modify only for verified defects: files already present in the exact allowlist

- [ ] **Step 1: Re-run immutable scope gate**

Run Task 0 Step 3 exactly. Expected: no path outside the fixed allowlist.

- [ ] **Step 2: Run focused backend matrix**

```powershell
uv run pytest tests/pilot_runtime tests/test_pilot_runtime_extraction_gate.py tests/test_chat_api.py tests/test_ai_agent.py tests/test_context_projector.py tests/test_agent_run_budget.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py tests/test_journal_active_work_budget_gate.py -q
```

Expected: all pass; only repository-approved platform skips may remain and must be listed.

- [ ] **Step 3: Run full release gates**

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

Expected: every command exits 0. If the external Application JD scope gate lacks its release-orchestrator
baseline/allowlist, report the exact missing variable and do not claim it passed.

- [ ] **Step 4: Run local verify and controlled real-AI verify**

Use the repository's current local verify commands/configuration without changing Provider settings. Verify:

```text
workspace sync and stream Chat
application-scoped Chat
read + read multi-call
write confirmation approve, modify, reject
approve → read tool → final
approve → chained Pending
terminal replay and delivery recovery
Journal enabled, disabled, and degraded
```

Record Provider calls, Tool executor calls, Pending/Ledger state, Journal Trace anomalies, Context Surface
fingerprint, HTTP/SSE result, and final domain readback. Do not save prompts, answers, secrets, JD/resume text,
or user data in the report.

- [ ] **Step 5: Complete browser verification**

Use the built-in Codex browser against the local app. Verify the same user-visible flows, including immediate
preheader HTTP error and SSE disconnect. Save only synthetic screenshots under the existing external
verification directory, not in the repository.

- [ ] **Step 6: Request independent code review**

Review the full implementation diff against the approved design with explicit P0/P1/P2 severity. Required
focus:

```text
HTTP-before-SSE-header boundary
sync/stream Source-vs-Run order
Guard zero-iteration cleanup and CAS
unbounded Queue and exact timeout owner
control exceptions vs Provider failures
reject Provider/Projector/executor zero
terminal replay Provider/executor zero
delivery heartbeat/fencing/late result
Journal fail-open and Surface fingerprint
old path deletion and no implicit fallback
```

Fix every P0/P1/P2 or document a user-approved exception before release.

- [ ] **Step 7: Write the release report**

Record:

- fixed baseline and final commit;
- exact changed-file allowlist result;
- internal breaking extraction and unchanged external contracts;
- every command, pass/fail/skip count, and external gate limitation;
- real-AI and browser evidence;
- independent CR findings and resolution;
- remaining risks: uncancellable native/provider call, existing external JD gate, and no cross-system
  exactly-once claim.

- [ ] **Step 8: Final clean-worktree verification**

```powershell
git diff --check
git status --short --branch
git log --oneline --decorate -12
```

Expected: `git diff --check` exits 0; only the release report is pending before the final commit.

- [ ] **Step 9: Commit the release report**

```powershell
git add docs/reports/2026-08-21-pilot-runtime-orchestration-extraction-release-verification.md
git commit -m "docs: AI 验证 Pilot Runtime 编排提取"
```

- [ ] **Step 10: Verify final state without push or merge**

```powershell
git status --short --branch
git diff --check
```

Expected: clean worktree. Do not push or merge without explicit user authorization.

---

## Implementation completion contract

Do not claim completion until all of the following are true:

- all four Chat routes call one `PilotRuntime` and contain no reliability orchestration;
- sync/stream/deterministic/confirmation/replay orders match `b05d915` golden behavior;
- HTTP-before-SSE-header, `user_message_saved`, seq, event order, and termination are exact;
- Provider, Tool executor, Pending, Ledger, Journal, Context Surface, and domain side effects are equivalent;
- Runtime control exceptions never become Provider/user product failures;
- Prepared lifecycle has only four states and three completion reasons with one cleanup winner;
- deterministic Legacy actions remain model-invisible and provider-free;
- no old path, feature flag, fallback, shadow execution, or duplicate write exists;
- focused/full backend, Ruff, Mypy, frontend, build, smoke, local verify, real-AI, browser, and independent CR
  gates have evidence;
- worktree is clean, unpushed, and unmerged.
