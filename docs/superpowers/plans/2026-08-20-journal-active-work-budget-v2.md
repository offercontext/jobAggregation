# Journal Active Work Budget V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Journal Segment wall-clock deadline with a 150 ms cumulative active-work budget, retain one independent 50 ms final convergence attempt, and preserve every existing business, Provider, HTTP/SSE, HITL, Ledger, privacy, and fail-open contract.

**Architecture:** A new dependency-free `agent_runtime/budget.py` owns monotonic sampling, cumulative charging, per-operation leases, and the split `SafeClockAdapter`. `SafeRunRecorder` and `RunRecorderFactory` serialize Journal-owned work through a bounded operation lock and explicit ingress/disposition state machines. `AgentRunRepository` applies dynamic SQLite guards only to Journal-owned Sessions; the existing caller-owned `append_event_bound()` SAVEPOINT remains the sole narrow exception.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite rollback journal, pytest, Ruff, Mypy, FastAPI Chat/SSE integration, PowerShell release gates.

---

## 0. Fixed execution boundary

Work only in:

```text
D:\Users\yuqi.chen\offerpilot\.worktrees\fix-20260820-journal-active-work-budget
```

Branch:

```text
fix/20260820-journal-active-work-budget
```

Fixed source baseline:

```text
4a354f9d58e2eb8b0800059b4532cc6e78235c80
```

The reviewed design is [2026-08-20-journal-active-work-budget-v2-design.md](../specs/2026-08-20-journal-active-work-budget-v2-design.md). The commit containing this plan becomes the immutable implementation baseline. Do not modify the design or plan during execution; any contract change stops implementation and requires a new review.

### Exact positive allowlist

```text
src/offerpilot/agent_runtime/budget.py
src/offerpilot/agent_runtime/events.py
src/offerpilot/agent_runtime/journal.py
src/offerpilot/context_projector/manifest.py
src/offerpilot/repositories/agent_runs.py
tests/test_agent_run_budget.py
tests/test_agent_run_journal.py
tests/test_agent_runs_repository.py
tests/test_context_projector.py
tests/test_chat_api.py
tests/test_journal_active_work_budget_gate.py
docs/reports/2026-08-20-journal-active-work-budget-v2-release-verification.md
```

Do not modify database models/migrations, API schemas, SSE contracts, Provider code, Tool Pipeline, Ledger, Agent loop, frontend code, `README.md`, or configuration. The implementation must not create `pilot_runtime` or scoped-capability modules.

### Task 0: Persist the reviewed baseline and allowlist

**Files:**
- Read: `docs/superpowers/specs/2026-08-20-journal-active-work-budget-v2-design.md`
- Read: `docs/superpowers/plans/2026-08-20-journal-active-work-budget-v2.md`
- Create outside repository: `%TEMP%\offerpilot-journal-budget-v2-gate\baseline.txt`
- Create outside repository: `%TEMP%\offerpilot-journal-budget-v2-gate\allowlist.txt`
- Create outside repository: `%TEMP%\offerpilot-journal-budget-v2-gate.locator.json`

- [ ] **Step 1: Verify the clean worktree and capture the plan commit**

```powershell
$ErrorActionPreference = 'Stop'
$repoRoot = (Get-Location).Path
if ((git status --short).Count -ne 0) { throw 'implementation must start clean' }
$baseline = (git log -1 --format=%H -- docs/superpowers/plans/2026-08-20-journal-active-work-budget-v2.md).Trim()
if (-not $baseline) { throw 'reviewed plan commit is missing' }
if ((git rev-parse HEAD).Trim() -ne $baseline) { throw 'HEAD must equal the reviewed plan commit' }
if ((git cat-file -t $baseline).Trim() -ne 'commit') { throw 'baseline does not resolve to a commit' }
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Persist one immutable allowlist source**

```powershell
$gateRoot = Join-Path $env:TEMP 'offerpilot-journal-budget-v2-gate'
$locatorPath = Join-Path $env:TEMP 'offerpilot-journal-budget-v2-gate.locator.json'
New-Item -ItemType Directory -Force -Path $gateRoot | Out-Null
$allowlist = @(
  'src/offerpilot/agent_runtime/budget.py',
  'src/offerpilot/agent_runtime/events.py',
  'src/offerpilot/agent_runtime/journal.py',
  'src/offerpilot/context_projector/manifest.py',
  'src/offerpilot/repositories/agent_runs.py',
  'tests/test_agent_run_budget.py',
  'tests/test_agent_run_journal.py',
  'tests/test_agent_runs_repository.py',
  'tests/test_context_projector.py',
  'tests/test_chat_api.py',
  'tests/test_journal_active_work_budget_gate.py',
  'docs/reports/2026-08-20-journal-active-work-budget-v2-release-verification.md'
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
  allowlist_path = $allowlistPath
  allowlist_sha256 = $allowlistHash
} | ConvertTo-Json | Set-Content -LiteralPath $locatorPath -Encoding utf8
```

Expected: all three gate files exist outside the worktree.

- [ ] **Step 3: Run the reusable scope assertion**

```powershell
$locator = Get-Content -Raw (Join-Path $env:TEMP 'offerpilot-journal-budget-v2-gate.locator.json') | ConvertFrom-Json
if ((Get-Location).Path -ne [string]$locator.repository_root) { throw 'wrong worktree' }
$baseline = (Get-Content -Raw -LiteralPath $locator.baseline_path).Trim()
if ($baseline -ne [string]$locator.baseline_sha) { throw 'baseline file changed' }
$actualAllowlistHash = [Convert]::ToHexString(
  [Security.Cryptography.SHA256]::HashData([IO.File]::ReadAllBytes([string]$locator.allowlist_path))
).ToLowerInvariant()
if ($actualAllowlistHash -ne [string]$locator.allowlist_sha256) { throw 'allowlist changed' }
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

Expected: exit code 0. Re-run this assertion after every task; never recompute the baseline.

---

### Task 1: Build the active-work budget primitives

**Files:**
- Create: `src/offerpilot/agent_runtime/budget.py`
- Create: `tests/test_agent_run_budget.py`

- [ ] **Step 1: Write failing monotonic and adapter tests**

Create `tests/test_agent_run_budget.py` with concrete clock scripts and these assertions:

```python
class ScriptedClock:
    def __init__(self, values: list[float | BaseException]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


def test_negative_monotonic_origin_is_valid_when_values_increase() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([-10.0, -9.5, -9.0]))
    assert [budget.safe_monotonic_read().valid for _ in range(3)] == [True, True, True]
    assert budget.used_seconds == 0.0


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_invalid_sample_has_no_budget_or_diagnostic_side_effect(value: object) -> None:
    budget = ActiveWorkBudget(0.150, lambda: value)  # type: ignore[return-value]
    sample = budget.safe_monotonic_read()
    assert sample.valid is False
    assert budget.used_seconds == 0.0
    assert budget.clock_invalid_latched is False


def test_safe_adapter_splits_no_throw_sample_from_throwing_require_value() -> None:
    budget = ActiveWorkBudget(0.150, ScriptedClock([KeyboardInterrupt(), SystemExit()]))
    adapter = budget.safe_clock_adapter()
    assert adapter.sample().valid is False
    with pytest.raises(JournalDeadlineExceeded):
        adapter.require_value()
```

Also cover a decreasing sample, `Exception`, arbitrary `BaseException`, and an invalid final sample after a valid entry.

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
uv run pytest tests/test_agent_run_budget.py -q
```

Expected: collection fails because `offerpilot.agent_runtime.budget` does not exist.

- [ ] **Step 3: Implement the dependency-free budget module**

Create these exact public/internal contracts:

```python
JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS = 0.150
JOURNAL_OPERATION_HARD_CAP_SECONDS = 0.050
JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS = 0.005
JOURNAL_DISPOSITION_BUDGET_SECONDS = 0.050
JOURNAL_SQLITE_PROGRESS_STEPS = 100
JOURNAL_DEFAULT_BUSY_TIMEOUT_MS = 50


class JournalBudgetExhausted(RuntimeError):
    pass


class JournalDeadlineExceeded(RuntimeError):
    def __init__(self, reason: Literal["deadline", "clock_invalid"] = "deadline") -> None:
        super().__init__("journal deadline exhausted")
        self.reason = reason


@dataclass(frozen=True)
class MonotonicSample:
    value: float
    valid: bool


@dataclass(frozen=True, repr=False)
class SafeClockAdapter:
    _reader: Callable[[], MonotonicSample]

    def sample(self) -> MonotonicSample:
        return self._reader()

    def require_value(self) -> float:
        sample = self.sample()
        if not sample.valid:
            raise JournalDeadlineExceeded("clock_invalid")
        return sample.value
```

`ActiveWorkBudget.safe_monotonic_read()` must catch both `Exception` and `BaseException`, reject bool/non-finite/decreasing samples, accept the first finite negative value, and never mutate usage or diagnostics on invalid input. `begin_operation()` computes hard/work deadlines from the entry sample and the current remaining balance. `finish_operation()` charges actual non-negative elapsed time in a no-throw critical section; an invalid final sample saturates the budget and sets only the transient `clock_invalid_latched` flag for the owning Recorder to consume.

```python
@dataclass(repr=False)
class ActiveWorkBudget:
    total_seconds: float
    clock: Callable[[], float]
    used_seconds: float = 0.0
    clock_invalid_latched: bool = False
    _last_valid: float | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def safe_monotonic_read(self) -> MonotonicSample:
        try:
            raw = self.clock()
            if type(raw) not in {int, float} or not math.isfinite(raw):
                return MonotonicSample(0.0, False)
            value = float(raw)
        except BaseException:
            return MonotonicSample(0.0, False)
        with self._lock:
            if self._last_valid is not None and value < self._last_valid:
                return MonotonicSample(value, False)
            self._last_valid = value
        return MonotonicSample(value, True)

    def begin_operation(
        self, entry: MonotonicSample, hard_cap_seconds: float
    ) -> OperationLease:
        if not entry.valid:
            raise JournalDeadlineExceeded("clock_invalid")
        with self._lock:
            allowance = min(hard_cap_seconds, self.total_seconds - self.used_seconds)
        if allowance <= JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS:
            raise JournalBudgetExhausted
        hard_deadline = entry.value + allowance
        return OperationLease(
            budget=self,
            entry_started_at=entry.value,
            work_deadline=hard_deadline - JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS,
            hard_deadline=hard_deadline,
        )

    def finish_operation(self, entry: MonotonicSample) -> bool:
        final = self.safe_monotonic_read()
        if not final.valid:
            self.latch_clock_invalid()
            return True
        elapsed = max(0.0, final.value - entry.value)
        with self._lock:
            self.used_seconds = min(self.total_seconds, self.used_seconds + elapsed)
            return self.used_seconds >= self.total_seconds

    def latch_clock_invalid(self) -> None:
        with self._lock:
            self.used_seconds = self.total_seconds
            self.clock_invalid_latched = True

    def safe_clock_adapter(self) -> SafeClockAdapter:
        return SafeClockAdapter(self.safe_monotonic_read)


@dataclass(frozen=True, repr=False)
class OperationLease:
    budget: ActiveWorkBudget
    entry_started_at: float
    work_deadline: float
    hard_deadline: float

    @property
    def safe_clock(self) -> SafeClockAdapter:
        return self.budget.safe_clock_adapter()

    def checkpoint(self) -> None:
        sample = self.budget.safe_monotonic_read()
        if not sample.valid:
            self.budget.latch_clock_invalid()
            raise JournalDeadlineExceeded("clock_invalid")
        if sample.value >= self.work_deadline:
            raise JournalBudgetExhausted
```

`OperationLease.checkpoint()` must use the same budget reader and raise only `JournalBudgetExhausted` or `JournalDeadlineExceeded`; it must never call the raw clock directly. `AgentRunRepository` re-exports `JournalDeadlineExceeded` for current import compatibility. The sealed `reason` is transient: Recorder maps `clock_invalid` to `journal_clock_invalid` and other deadline exhaustion to `journal_budget_exhausted`; it never enters logs or persisted payloads.

- [ ] **Step 4: Verify GREEN and type/lint the module**

```powershell
uv run pytest tests/test_agent_run_budget.py -q
uv run ruff check src/offerpilot/agent_runtime/budget.py tests/test_agent_run_budget.py
uv run mypy src/offerpilot/agent_runtime/budget.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the budget primitives**

```powershell
git add src/offerpilot/agent_runtime/budget.py tests/test_agent_run_budget.py
git commit -m "feat: AI add journal active work budget"
```

---

### Task 2: Make canonical, HMAC, and V2 Manifest work interruptible

**Files:**
- Modify: `src/offerpilot/agent_runtime/events.py`
- Modify: `src/offerpilot/context_projector/manifest.py`
- Modify: `tests/test_agent_run_journal.py`
- Modify: `tests/test_context_projector.py`

- [ ] **Step 1: Write failing checkpoint tests**

Add a deterministic guard that fails on an exact call count:

```python
class FailingGuard:
    def __init__(self, fail_at: int) -> None:
        self.calls = 0
        self.fail_at = fail_at

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise JournalBudgetExhausted


def test_surface_manifest_checks_budget_inside_provider_and_chunk_loops() -> None:
    guard = FailingGuard(12)
    with pytest.raises(JournalBudgetExhausted):
        prepare_surface_manifest_v2(
            maximal_runtime_audit(),
            key_id=KEY.key_id,
            secret=KEY.secret,
            provider_identities=tuple(f"provider-{index}" for index in range(8)),
            budget_check=guard,
        )
    assert guard.calls == 12
```

Extend the existing canonicalization test so UTF-8 encoding, ordered digest, HMAC chunk updates, Manifest assembly, and final digest each cross at least one checkpoint.

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
uv run pytest tests/test_agent_run_journal.py::test_canonicalization_invokes_budget_guard_during_collection_traversal tests/test_context_projector.py::test_surface_manifest_checks_budget_inside_provider_and_chunk_loops -q
```

Expected: the V2 test fails because `prepare_surface_manifest_v2()` does not accept `budget_check`.

- [ ] **Step 3: Thread one operation checkpoint through pure transforms**

Add the optional callback without changing serialized output:

```python
def _check_budget(budget_check: Callable[[], None] | None) -> None:
    if budget_check is not None:
        budget_check()


def _build_manifest_payload(
    audit: RuntimeSurfaceAudit,
    *,
    key_id: str,
    secret: bytes,
    providers: list[str],
    signals: tuple[str, ...],
    budget_check: Callable[[], None] | None,
) -> dict[str, object]:
    sources: list[dict[str, object]] = []
    for source in audit.source_records:
        _check_budget(budget_check)
        chunks: list[dict[str, object]] = []
        for chunk in source.chunks:
            _check_budget(budget_check)
            chunks.append(
                {
                    "path_hmac": _identity(secret, b"offerpilot-surface-chunk-v2", chunk.path),
                    "ordinal": chunk.ordinal,
                    "total": chunk.total,
                    "truncated": chunk.truncated,
                    "original_bytes": chunk.original_bytes,
                    "original_codepoints": chunk.original_codepoints,
                }
            )
        sources.append(
            {
                "source_hmac": _identity(
                    secret,
                    b"offerpilot-surface-source-v2",
                    f"{source.kind}:{source.revision_identity}",
                ),
                "content_revision_fingerprint": source.content_revision_fingerprint,
                "chunks": chunks,
            }
        )
    if not sources:
        for index, fingerprint in enumerate(audit.source_fingerprints):
            _check_budget(budget_check)
            sources.append(
                {
                    "source_hmac": _identity(
                        secret,
                        b"offerpilot-surface-source-v2",
                        f"{index}:{fingerprint}",
                    ),
                    "content_revision_fingerprint": fingerprint,
                    "chunks": [],
                }
            )
    contributors: list[dict[str, str]] = []
    for name, status in audit.contributor_statuses:
        _check_budget(budget_check)
        contributors.append({"name": name, "status": status})
    history_groups: list[str] = []
    for item in audit.selected_history_group_ids:
        _check_budget(budget_check)
        history_groups.append(
            _identity(secret, b"offerpilot-surface-history-v2", item)
        )
    tools: list[str] = []
    for tool_name in audit.selected_tool_names:
        _check_budget(budget_check)
        tools.append(tool_name)
    effective_signals: list[str] = []
    for signal in signals or audit.signals:
        _check_budget(budget_check)
        effective_signals.append(signal)
    return {
        "manifest_schema_version": 2,
        "budget_policy_version": audit.budget_policy_version,
        "providers": providers,
        "contributors": contributors,
        "history_groups": history_groups,
        "tools": tools,
        "sources": sources,
        "signals": effective_signals,
        "counts": {
            "estimated_input_units": audit.estimated_input_units,
            "canonical_message_bytes": audit.canonical_message_bytes,
            "canonical_tool_bytes": audit.canonical_tool_bytes,
        },
        "truncated": audit.truncated,
        "fingerprint_key_id": key_id,
    }


def prepare_surface_manifest_v2(
    audit: RuntimeSurfaceAudit,
    *,
    key_id: str,
    secret: bytes,
    provider_identities: tuple[str, ...],
    signals: tuple[str, ...] = (),
    budget_check: Callable[[], None] | None = None,
) -> PreparedSurfaceManifestV2:
    _check_budget(budget_check)
    providers: list[str] = []
    for identity in provider_identities:
        _check_budget(budget_check)
        providers.append(_identity(secret, b"offerpilot-surface-provider-v2", identity))
    _check_budget(budget_check)
    manifest = _build_manifest_payload(
        audit,
        key_id=key_id,
        secret=secret,
        providers=providers,
        signals=signals,
        budget_check=budget_check,
    )
    _check_budget(budget_check)
    rendered = _canonical(manifest)
    _check_budget(budget_check)
    validate_surface_manifest_v2(rendered)
    encoded = rendered.encode("utf-8")
    _check_budget(budget_check)
    return PreparedSurfaceManifestV2(
        rendered,
        hashlib.sha256(encoded).hexdigest(),
        key_id,
    )
```

Call it before and after every bounded provider/contributor/history/tool/source/chunk loop, before each HMAC update, before/after canonical encoding, and before/after the final SHA-256. Preserve the exact canonical JSON and fingerprints. In `events.py`, retain existing traversal checkpoints and add missing checks around final UTF-8 encoding, byte caps, and SHA/HMAC finalization. Do not change schema versions or digest domains.

- [ ] **Step 4: Verify byte-for-byte compatibility and GREEN**

```powershell
uv run pytest tests/test_agent_run_journal.py tests/test_context_projector.py -q
uv run ruff check src/offerpilot/agent_runtime/events.py src/offerpilot/context_projector/manifest.py tests/test_agent_run_journal.py tests/test_context_projector.py
uv run mypy src/offerpilot/agent_runtime/events.py src/offerpilot/context_projector/manifest.py
```

Expected: tests pass; existing V1/V2 canonical fixtures and fingerprints remain unchanged.

- [ ] **Step 5: Commit the interruptible transforms**

```powershell
git add src/offerpilot/agent_runtime/events.py src/offerpilot/context_projector/manifest.py tests/test_agent_run_journal.py tests/test_context_projector.py
git commit -m "refactor: AI bound journal canonical work"
```

---

### Task 3: Add Journal-owned SQLite deadline guards

**Files:**
- Modify: `src/offerpilot/repositories/agent_runs.py`
- Modify: `tests/test_agent_runs_repository.py`

- [ ] **Step 1: Write failing dynamic deadline and progress-handler tests**

Add tests for:

```python
class ManualClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_busy_timeout_uses_remaining_operation_budget(tmp_path: Path) -> None:
    clock = ManualClock(10.000)
    adapter = ActiveWorkBudget(0.150, clock).safe_clock_adapter()
    repository = _repository(tmp_path)
    statements: list[str] = []
    engine = repository.session_factory.kw["bind"]
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(
            statement
        ),
    )
    repository.append_event(RUN_ID, _route_draft(), deadline=10.012, safe_clock=adapter)
    configured = [item for item in statements if item.startswith("PRAGMA busy_timeout =")]
    assert configured
    assert int(configured[0].rsplit(" ", 1)[-1]) <= 12


def test_progress_handler_invalid_clock_returns_nonzero_without_raising(tmp_path: Path) -> None:
    adapter = ActiveWorkBudget(0.150, lambda: float("nan")).safe_clock_adapter()
    handler = _progress_handler(adapter, deadline=1.0)
    assert handler() != 0


def test_progress_handler_interrupts_recursive_cte_and_connection_is_reusable(tmp_path: Path) -> None:
    conversation_id, _ = _seed_conversation(tmp_path)
    base = _repository(tmp_path)
    base.create_run_and_initial_segment(_start_command(conversation_id))

    class SlowScanRepository(AgentRunRepository):
        @staticmethod
        def _required_run(session: Session, run_id: str) -> AgentRun:
            session.execute(
                text(
                    "WITH RECURSIVE counter(x) AS ("
                    "SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < 100000000"
                    ") SELECT sum(x) FROM counter"
                )
            ).scalar_one()
            return AgentRunRepository._required_run(session, run_id)

    slow = SlowScanRepository(base.session_factory)
    budget = ActiveWorkBudget(0.150, time.monotonic)
    with pytest.raises(JournalDeadlineExceeded):
        slow.append_event(
            RUN_ID,
            _route_draft(),
            deadline=time.monotonic() + 0.005,
            safe_clock=budget.safe_clock_adapter(),
        )

    inserted = base.append_event(RUN_ID, _route_draft())
    assert inserted.event_type == "route.selected"
```

Use the existing seeding helpers and imports for `AgentRun`, `Session`, and SQLAlchemy `text`. Also assert restored `PRAGMA busy_timeout = 50` and a cleared handler through a succeeding subsequent append.

- [ ] **Step 2: Run repository deadline tests and verify RED**

```powershell
uv run pytest tests/test_agent_runs_repository.py -k "busy_timeout or progress_handler or deadline" -q
```

Expected: tests fail because Repository methods still accept raw `clock` callables and do not install a progress handler.

- [ ] **Step 3: Replace raw clock parameters on Journal-owned paths**

Use this signature on `create_run_and_initial_segment`, `attach_input_message`, `start_segment`, `append_event`, `capture_context`, `converge_disposition`, `mark_degraded`, `find_waiting_run`, and all replay helpers:

```python
def append_event(
    self,
    run_id: str,
    draft: EventDraft,
    *,
    deadline: float | None = None,
    safe_clock: SafeClockAdapter | None = None,
) -> AgentEvent:
    self._validate_event_draft(draft)
    is_noop_finish = False
    if draft.event_type == "segment.finished":
        facts = json.loads(draft.payload_json)["facts"]
        is_noop_finish = facts == {"outcome": "noop", "terminal_run_status": None}
    if draft.event_type in _DISPOSITION_EVENT_TYPES and not is_noop_finish:
        raise JournalConflictError("disposition event requires its atomic repository method")
    try:
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            existing = self._existing_event(session, run_id, draft)
            if existing is not None:
                return self._detach(session, existing)
            run = self._required_run(session, run_id)
            if run.status in _TERMINAL_STATUSES and not is_noop_finish:
                raise JournalConflictError("terminal run cannot accept a new event")
            if is_noop_finish:
                segment_start = session.scalar(
                    select(AgentEvent).where(
                        AgentEvent.run_id == run_id,
                        AgentEvent.event_type == "segment.started",
                        AgentEvent.execution_segment_id == draft.execution_segment_id,
                    )
                )
                if segment_start is None:
                    raise JournalConflictError("noop finish requires a resumable segment")
                started_facts = json.loads(segment_start.payload_json)["facts"]
                if started_facts["request_kind"] not in {"confirmation", "pending_replay"}:
                    raise JournalConflictError("noop finish requires a resumable segment")
            event = self._insert_event(session, run_id, draft, self._utc_now())
            return self._detach(session, event)
    except IntegrityError:
        replayed = self._replay_event(
            run_id,
            draft,
            deadline=deadline,
            safe_clock=safe_clock,
        )
        if replayed is not None:
            return replayed
        raise JournalConflictError("event conflicts with persisted journal state") from None
```

When `deadline is not None`, reject a missing adapter. When `deadline is None`, preserve internal maintenance/test behavior with the default 50 ms SQLite timeout and no operation progress handler. Do not change `append_event_bound(session, run_id, draft)`.

- [ ] **Step 4: Implement an owned-session guard and cleanup**

Implement a private context manager that:

```text
checkout Journal-owned Session
→ require_value()
→ set busy_timeout to min(50, floor(remaining * 1000))
→ install raw sqlite progress handler every 100 VM steps
→ execute transaction/replay
→ rollback incomplete transaction
→ clear progress handler
→ restore busy_timeout=50
→ invalidate/close when restoration cannot be proven
```

The progress callback itself is a small pure closure:

```python
def _progress_handler(safe_clock: SafeClockAdapter, deadline: float) -> Callable[[], int]:
    def check() -> int:
        sample = safe_clock.sample()
        return int(not sample.valid or sample.value >= deadline)

    return check
```

The progress callback must only call `safe_clock.sample()` and return nonzero for invalid/expired samples. It must never raise. Cleanup exceptions must not return a contaminated connection to the Pool. Preserve original `BaseException` priority after cleanup.

After an SQLite interrupt, sample the adapter once outside the progress callback: invalid maps to `JournalDeadlineExceeded("clock_invalid")`; a valid sample at/after the deadline maps to `JournalDeadlineExceeded("deadline")`; unrelated SQLite failures preserve their existing safe classification.

- [ ] **Step 5: Add lock, cleanup, native-call, and ABA tests**

Use two SQLite connections for the busy-lock case. Add monkeypatched cleanup failures for rollback, handler clear, PRAGMA restore, and invalidate. Use a recursive CTE for strict VM interruption. Use a blocking SQLite UDF only to prove that, after it returns, the operation is classified exhausted and the connection is restored; do not assert 50 ms preemption.

```python
assert elapsed < 0.250  # lock/VM tests only, with platform tolerance
assert next_connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 50
assert subsequent_normal_append.event_type == "route.selected"
```

- [ ] **Step 6: Verify GREEN and commit**

```powershell
uv run pytest tests/test_agent_runs_repository.py -q
uv run ruff check src/offerpilot/repositories/agent_runs.py tests/test_agent_runs_repository.py
uv run mypy src/offerpilot/repositories/agent_runs.py
git add src/offerpilot/repositories/agent_runs.py tests/test_agent_runs_repository.py
git commit -m "feat: AI enforce journal sqlite operation deadlines"
```

---

### Task 4: Switch Factory creation and ordinary Recorder calls to active operations

**Files:**
- Modify: `src/offerpilot/agent_runtime/journal.py`
- Modify: `tests/test_agent_run_journal.py`

- [ ] **Step 1: Write failing cumulative-budget tests**

Add explicit red tests:

```python
def test_two_seconds_between_calls_do_not_consume_active_budget() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    recorder.append_event(_route_event())
    clock.advance(2.0)
    recorder.append_event(_route_event())
    assert recorder.recording_status == "healthy"
    assert repository.append_calls == 2


def test_five_operations_share_one_hundred_fifty_ms_budget() -> None:
    clock = ManualClock()
    repository = RecordingJournalRepository(on_append=lambda: clock.advance(0.030))
    recorder = _recorder(repository, clock=clock)
    for index in range(5):
        recorder.append_event(_route_event_with_step(index))
    assert repository.append_calls == 5
    assert recorder.recording_status == "degraded"
    assert recorder.diagnostics == ["journal_budget_exhausted"]
```

Also assert that a sixth call performs no preparation or Repository call.

Extend the existing `test_safe_recorder_does_not_swallow_base_exception` so the Repository raises `KeyboardInterrupt`, the final clock read raises `SystemExit`, and the observed exception remains the original `KeyboardInterrupt`. Add one case where cleanup raises an ordinary exception and assert the operation lock is released, elapsed work is charged, and the Recorder degrades without exception text.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
uv run pytest tests/test_agent_run_journal.py -k "two_seconds_between_calls or share_one_hundred_fifty" -q
```

Expected: the first test fails because the existing Segment absolute deadline expires during the two-second gap.

- [ ] **Step 3: Create one shared operation wrapper in `SafeRunRecorder`**

Replace `_segment_started_at`, `_segment_deadline`, and `_disposition_attempted` with:

```python
self._active_budget = active_budget
self._operation_lock = threading.Lock()
self._state_lock = threading.RLock()
self._state_changed = threading.Condition(self._state_lock)
self._resume_state = "not_attempted"
self._disposition_state = "not_attempted"
self._disposition_waits_for_resume = False
```

Implement one wrapper that records the entry sample before lock acquisition, bounds the wait, rechecks `recording_status == "healthy"` and `_disposition_state == "not_attempted"` after acquiring the lock, runs prepare/Repository/replay with one lease, cleans up, charges in nested `finally`, and releases the lock last. Ordinary `Exception` maps to the existing safe diagnostic; `BaseException` is re-raised after cleanup and charging.

If the budget reports `clock_invalid_latched` or a Repository exception has `reason == "clock_invalid"`, the wrapper must use `journal_clock_invalid`. It must not collapse that case into `journal_budget_exhausted`.

- [ ] **Step 4: Move every ordinary operation under the wrapper**

Cover `start_segment`, `attach_input_message`, both context capture methods, `append_event`, both fingerprint methods, and `_sync_degraded`. Pass `lease.work_deadline` and `lease.safe_clock` to every Journal-owned Repository call. Bind canonical/HMAC/Manifest `budget_check` to `lease.checkpoint`.

Preserve each current public return value and diagnostic code. Do not add retries or a second Repository call.

- [ ] **Step 5: Switch `RunRecorderFactory` without resetting the budget**

Create `ActiveWorkBudget` before a deferred start/resume builder. Run lookup, builder, HMAC, command construction, create/start Segment, replay, and cleanup in one Operation. After success, pass the same already-charged object to `SafeRunRecorder`:

```text
budget B = ActiveWorkBudget(segment_budget_seconds, factory clock)
entry = B.safe_monotonic_read()
lease = B.begin_operation(entry, 50 ms)
builder guard = lease.checkpoint
Repository deadline = lease.work_deadline
Repository safe clock = lease.safe_clock
finally = B.finish_operation(entry)
SafeRunRecorder.active_budget = the same object B
```

For confirmation resume, `find_waiting_run()` and `start_segment()` share one lease/deadline. Do not grant either call a new 50 ms window.

- [ ] **Step 6: Verify Factory/ordinary GREEN and commit**

```powershell
uv run pytest tests/test_agent_run_budget.py tests/test_agent_run_journal.py -q
uv run ruff check src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py
uv run mypy src/offerpilot/agent_runtime/journal.py
git add src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py
git commit -m "refactor: AI switch journal recorder to active work"
```

---

### Task 5: Preserve the caller-owned `tool.started` transaction boundary

**Files:**
- Modify: `src/offerpilot/agent_runtime/journal.py`
- Modify: `tests/test_agent_run_journal.py`
- Modify: `tests/test_agent_runs_repository.py`

- [ ] **Step 1: Write failing caller-owned transaction tests**

Add six cases: success, Journal conflict, budget exhausted before entry, native overshoot after insert, ordinary exception, and `BaseException`.

```python
def test_bound_native_overshoot_returns_true_and_preserves_outer_commit(tmp_path: Path) -> None:
    clock = ManualClock()
    recorder, session, draft = seeded_bound_recorder(tmp_path, clock)
    repository.before_bound_return = lambda: clock.advance(0.060)
    with session.begin():
        assert recorder.append_prepared_event_bound(session, draft) is True
        write_domain_marker(session)
    assert load_event(draft.dedupe_key) is not None
    assert load_domain_marker() is not None
    assert recorder.recording_status == "degraded"
```

For the conflict/ordinary exception case, assert only the nested SAVEPOINT rolls back while a domain marker in the outer transaction commits. For `KeyboardInterrupt`, assert the caller can roll back the entire outer transaction and neither write persists.

- [ ] **Step 2: Run the bound tests and verify RED**

```powershell
uv run pytest tests/test_agent_run_journal.py tests/test_agent_runs_repository.py -k "bound or prepared_event" -q
```

Expected: at least the active-time and overshoot assertions fail because the current bound path has no operation accounting.

- [ ] **Step 3: Implement the two-operation bound protocol**

`prepare_event_draft()` is a CPU-only normal active Operation and must return `None` before opening a business Session when exhausted. `append_prepared_event_bound()` is a second active Operation that uses the Recorder operation lock and current budget but passes no deadline/adapter to the caller Session.

Inside the caller-owned path, permit only:

```python
with session.begin_nested():
    self.repository.append_event_bound(session, self.run_id, draft)
```

Do not install handlers, change PRAGMA, call commit/rollback/close/invalidate, open a Journal Session, or invoke `_sync_degraded()` while the outer transaction is open. If the insert succeeded but returned after the hard cap, return `True`, then charge and latch degraded; never rerun the insert.

- [ ] **Step 4: Verify atomicity GREEN and commit**

```powershell
uv run pytest tests/test_agent_run_journal.py tests/test_agent_runs_repository.py -k "bound or prepared_event" -q
uv run ruff check src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py
git add src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py
git commit -m "fix: AI preserve bound journal transaction ownership"
```

---

### Task 6: Implement resume and final disposition concurrency state machines

**Files:**
- Modify: `src/offerpilot/agent_runtime/journal.py`
- Modify: `tests/test_agent_run_journal.py`

- [ ] **Step 1: Write the three-thread nonterminal fencing regression**

Use `threading.Event` barriers to force:

```text
A owns operation lock
B passes fast precheck and waits
C claims final disposition
A releases
B acquires before C
```

Assert B fails the post-lock authoritative recheck and performs zero prepare/Repository work; C performs exactly one convergence.

- [ ] **Step 2: Write resume-first/finalizer-first parameterized RED tests**

```python
@pytest.mark.parametrize("final_kind", ["finish", "suspend", "abandon"])
@pytest.mark.parametrize("claim_order", ["resume_first", "finalizer_first"])
def test_resume_and_finalizer_linearize_without_late_run_resumed(
    final_kind: str, claim_order: str
) -> None:
    recorder, repository, barriers = concurrent_recorder()
    run_claim_order(recorder, barriers, claim_order, final_kind)
    event_types = repository.persisted_event_types
    assert not final_event_precedes_run_resumed(event_types)
    expected_calls = 2 if claim_order == "resume_first" else 1
    assert repository.converge_calls == expected_calls
```

Add spurious `Condition.notify_all()` calls and assert the waiter stays in its predicate loop with the original absolute deadline.

- [ ] **Step 3: Write the invalid-clock duplicate-finalizer RED test**

```python
class ToggleClock:
    def __init__(self, initial: float) -> None:
        self.value = initial
        self.invalid = False

    def __call__(self) -> float:
        return float("nan") if self.invalid else self.value


def recorder_debug_state(recorder: SafeRunRecorder) -> tuple[object, ...]:
    return (
        recorder.recording_status,
        tuple(recorder.diagnostics),
        recorder._active_budget.used_seconds,
        recorder._disposition_state,
        recorder._resume_state,
    )


def test_completed_finalizer_then_invalid_clock_is_absolute_noop() -> None:
    clock = ToggleClock(initial=0.0)
    repository = RecordingJournalRepository()
    recorder = _recorder(repository, clock=clock)
    recorder.finish(TerminalDisposition(status="completed"))
    before = recorder_debug_state(recorder)
    clock.invalid = True
    recorder.finish(TerminalDisposition(status="completed"))
    assert recorder_debug_state(recorder) == before
    assert recorder._disposition_state == "completed"
    assert repository.converge_calls == 1
```

The debug helper may read transient test-visible properties; do not add serialization or a production debug endpoint.

- [ ] **Step 4: Implement resume ingress linearization**

Under one state lock/Condition:

```text
resume claims first      → _resume_state=claimed; later finalizer waits
finalizer claims first   → resume absolute no-op
resume post-lock check   → allow only not_attempted disposition or claimed+wait flag
resume finally           → completed|failed; notify_all
```

Waiting finalizers do not hold the operation lock. Condition wait, operation lock wait, preparation, Repository work, and cleanup share the same 50 ms finalizer deadline.

- [ ] **Step 5: Implement one-shot final convergence**

At entry, read a side-effect-free sample. For invalid input, under the state lock return absolute no-op when disposition is already non-initial; otherwise transition `not_attempted → failed` and only the winner latches `journal_clock_invalid`. For valid input, atomically claim before waiting for the operation lock. Set `claimed → completed|failed` and clear the wait flag in the outermost `finally` before propagating any `BaseException`.

`suspend`, `finish`, and `abandon` share this state machine and one independent 50 ms lease. `resume()` never consumes it.

- [ ] **Step 6: Verify concurrency GREEN and commit**

```powershell
uv run pytest tests/test_agent_run_journal.py -k "thread or concurrent or resume or disposition or finalizer or absolute_noop" -q
uv run pytest tests/test_agent_run_journal.py -q
uv run ruff check src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py
uv run mypy src/offerpilot/agent_runtime/journal.py
git add src/offerpilot/agent_runtime/journal.py tests/test_agent_run_journal.py
git commit -m "fix: AI serialize journal resume and disposition"
```

---

### Task 7: Prove Repository call ownership mechanically

**Files:**
- Create: `tests/test_journal_active_work_budget_gate.py`
- Modify: `tests/test_agent_run_journal.py`
- Modify: `tests/test_agent_runs_repository.py`

- [ ] **Step 1: Write a failing AST gate for Journal-owned calls**

Parse `src/offerpilot/agent_runtime/journal.py` and assert every production call to these methods contains both `deadline=` and `safe_clock=`:

```python
OWNED_METHODS = {
    "create_run_and_initial_segment",
    "attach_input_message",
    "start_segment",
    "append_event",
    "capture_context",
    "converge_disposition",
    "mark_degraded",
    "find_waiting_run",
}
```

Assert the only call to `append_event_bound` occurs inside `SafeRunRecorder.append_prepared_event_bound` and has neither keyword. Reject references there to `_configure_deadline`, progress handlers, PRAGMA, Session factories, commit, rollback, close, or invalidate.

- [ ] **Step 2: Add stale-path deletion assertions**

The same gate must fail if `journal.py` contains `_segment_deadline`, `_segment_started_at`, `_disposition_attempted`, direct `self.clock()` outside budget construction, or Repository calls using `clock=`. It must also prove `budget.py` objects are absent from Graph/Chat/Pending/Ledger/model serialization modules.

- [ ] **Step 3: Run the AST tests and verify RED, then remove stale paths**

```powershell
uv run pytest tests/test_journal_active_work_budget_gate.py -q
```

Expected before cleanup: FAIL on remaining legacy wall-deadline symbols or raw Repository clock keywords. Delete those paths; do not add an allowlisted fallback or feature flag.

- [ ] **Step 4: Verify GREEN and commit**

```powershell
uv run pytest tests/test_journal_active_work_budget_gate.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py -q
uv run ruff check tests/test_journal_active_work_budget_gate.py src/offerpilot/agent_runtime src/offerpilot/repositories/agent_runs.py
git add tests/test_journal_active_work_budget_gate.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py src/offerpilot/agent_runtime/journal.py src/offerpilot/repositories/agent_runs.py
git commit -m "test: AI gate journal active budget ownership"
```

---

### Task 8: Verify Chat, SSE, HITL, Ledger, and Trace equivalence

**Files:**
- Modify: `tests/test_chat_api.py`

- [ ] **Step 1: Add a real two-second Provider wait regression**

Create a scripted model whose first Agent completion sleeps two seconds and then returns a deterministic response. Run both sync and SSE routes with the real stable Journal factory.

```python
assert response.status_code == 200
assert provider.calls == 1
assert runs[0].recording_status == "healthy"
assert "model_call_incomplete" not in reconstruct_trace(repository, runs[0].id).anomalies
assert [event.event_type for event in events] == [
    "run.started",
    "segment.started",
    "route.selected",
    "context.captured",
    "context.captured",
    "model.requested",
    "model.completed",
    "assistant.persisted",
    "run.completed",
    "segment.finished",
]
```

Keep this list copied from the existing healthy lifecycle test; never regenerate it from the new implementation.

- [ ] **Step 2: Add read-tool and confirmation continuation regressions**

Cover:

```text
2 s Provider → read tool → Provider → final
write Pending → approve → execution → final
write Pending → reject
approve → chained Pending → approve/reject
terminal replay and delivery recovery/fallback
```

For each case assert HTTP/SSE fields and ordering, Provider/tool call counts, ChatMessage/Pending/Ledger/domain rows, Journal event order, and Trace anomalies. Provider/tool/business waits must not consume Journal active budget.

- [ ] **Step 3: Add failure-mode equivalence**

Parameterize Journal disabled, key unavailable, locked SQLite, active-budget exhausted, invalid clock, and caller-owned Journal conflict. Reuse the existing business-result snapshots and assert only Journal health/diagnostics differ.

- [ ] **Step 4: Run integration tests and verify GREEN**

```powershell
uv run pytest tests/test_chat_api.py -k "journal" -q
uv run pytest tests/tool_pipeline/test_journal.py tests/tool_pipeline/test_pipeline.py -q
uv run pytest tests/test_agent_run_trace.py -q
```

Expected: all selected tests pass; the two-second Provider cases retain a healthy complete Trace.

- [ ] **Step 5: Commit integration coverage**

```powershell
git add tests/test_chat_api.py
git commit -m "test: AI verify journal active budget integration"
```

---

### Task 9: Run focused quality gates and independent review

**Files:**
- Modify only when fixing an observed failure: files already listed in the allowlist

- [ ] **Step 1: Run the complete backend focus set**

```powershell
uv run pytest tests/test_agent_run_budget.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py tests/test_agent_run_trace.py tests/test_context_projector.py tests/tool_pipeline/test_journal.py tests/tool_pipeline/test_pipeline.py tests/test_chat_api.py -q
uv run ruff check src/offerpilot/agent_runtime src/offerpilot/context_projector/manifest.py src/offerpilot/repositories/agent_runs.py tests/test_agent_run_budget.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py tests/test_context_projector.py tests/test_chat_api.py tests/test_journal_active_work_budget_gate.py
uv run mypy src/offerpilot/agent_runtime src/offerpilot/context_projector/manifest.py src/offerpilot/repositories/agent_runs.py
git diff --check
```

Expected: all tests pass; Ruff/Mypy/diff checks exit 0.

- [ ] **Step 2: Run an independent CR**

Review against the approved design, with special attention to:

```text
active time excludes Provider/tool/business/call gaps
every active path charges in finally
invalid clock cannot pollute completed disposition
post-lock nonterminal fencing
resume/finalizer ordering
caller-owned Session ownership
SQLite guard cleanup and ABA
no raw exception/content diagnostics
no old wall-deadline fallback
```

Expected: no open P0/P1/P2. Fix each finding with a failing regression first, re-run the smallest affected test, then re-run Step 1.

- [ ] **Step 3: Commit review fixes when needed**

```powershell
git add src/offerpilot/agent_runtime/budget.py src/offerpilot/agent_runtime/events.py src/offerpilot/agent_runtime/journal.py src/offerpilot/context_projector/manifest.py src/offerpilot/repositories/agent_runs.py tests/test_agent_run_budget.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py tests/test_context_projector.py tests/test_chat_api.py tests/test_journal_active_work_budget_gate.py
git commit -m "fix: AI close journal budget review"
```

Skip this commit only when CR finds no issues and the worktree is already clean.

---

### Task 10: Complete release verification and handoff

**Files:**
- Create: `docs/reports/2026-08-20-journal-active-work-budget-v2-release-verification.md`

- [ ] **Step 1: Run the full local gate**

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy src
Push-Location web
npm test -- --run
npm run build
Pop-Location
uv run oc smoke --static-dir web/dist
```

Expected: backend, frontend, type/lint, build, and static smoke pass. Record exact counts and any explicitly allowed platform skips.

- [ ] **Step 2: Run runtime verification**

Run local smoke/local verify, then one controlled real-AI Chat path where Provider latency exceeds two seconds. Verify sync or SSE final delivery, one HITL approve/reject loop, business state readback, healthy Journal Trace, and no `model_call_incomplete` caused solely by Provider wall time.

Expected: external behavior is unchanged and the healthy Journal captures the complete lifecycle.

- [ ] **Step 3: Re-run the immutable scope gate**

Use Task 0 Step 3 verbatim, then run:

```powershell
git diff --check
git status --short --branch
```

Expected: every changed path is allowlisted; no unexpected untracked files.

- [ ] **Step 4: Write the release report**

The report must include:

```text
fixed baseline and final commit
active-work and per-operation guarantees
non-interruptible native-call limitation
caller-owned Session exception
no Schema/API/SSE/business behavior changes
focused/full test counts
Ruff/Mypy/frontend/build/smoke results
real-AI result
independent CR conclusion
remaining non-goals and risks
```

- [ ] **Step 5: Commit the release report**

```powershell
git add docs/reports/2026-08-20-journal-active-work-budget-v2-release-verification.md
git commit -m "docs: AI verify journal active work budget"
```

- [ ] **Step 6: Final clean-tree verification**

```powershell
git diff --check
if ((git status --porcelain).Count -ne 0) { throw 'release worktree is not clean' }
git log --oneline --decorate -12
```

Expected: clean worktree. Do not push or merge unless the user separately authorizes it.

---

## TDD and commit discipline

For every behavior change:

```text
RED     → add one focused regression and observe the expected failure
GREEN   → implement the minimum production change
REFACTOR→ remove duplication only while the focused tests remain green
COMMIT  → stage exact files, then commit with `<type>: AI <English subject>`
```

Never add production code before its regression fails. Never update a test merely to accept implementation drift. Golden Journal sequences, Provider/tool call counts, HTTP/SSE payloads, Pending/Ledger/domain side effects, and privacy canaries remain authoritative.

## Completion statement

The project is complete only when the implementation can accurately state:

> The Journal 150 ms limit accumulates only Journal-owned synchronous active work. Provider, tool, business Repository, confirmation, and between-call waiting do not consume it. Every Journal operation has one bounded deadline; final disposition has one independent convergence opportunity; SQLite and CPU work are bounded where mechanically interruptible; non-interruptible native work is charged after return; Journal failure remains fail-open and does not alter business behavior.

This statement does not promise a complete Journal under failure, cross-request exactly-once, Event Sourcing, SSE replay, or any Phase 2+ orchestration refactor.
