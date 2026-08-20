from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Condition, Lock, RLock
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar
from uuid import uuid4

from sqlalchemy.exc import OperationalError

from offerpilot.agent_runtime.budget import (
    JOURNAL_OPERATION_HARD_CAP_SECONDS,
    JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS,
    ActiveWorkBudget,
    JournalBudgetExhausted,
    JournalDeadlineExceeded,
    OperationLease,
)
from offerpilot.agent_runtime.events import (
    ContextManifestInput,
    EventDraft,
    JournalEventValidationError,
    PreparedSnapshot,
    model_id_fingerprint,
    pending_identity_fingerprint,
    prepare_context_snapshot,
    prepare_event,
)
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.repositories.agent_runs import (
    AgentRunRepository,
    CaptureContextCommand,
    DispositionCommand,
    RunStatus,
    StartRunCommand,
    StartSegmentCommand,
)

if TYPE_CHECKING:
    from offerpilot.context_projector.contracts import RuntimeSurfaceAudit

RecordingStatus = Literal["healthy", "degraded"]
TerminalStatus = Literal["completed", "failed", "cancelled", "timed_out"]
EventPreparer = Callable[["EventInput", float], EventDraft]
ContextPreparer = Callable[[object, ContextManifestInput, float], PreparedSnapshot]
StartRunBuilder = Callable[
    [JournalKeyDomain, Callable[[], None]],
    StartRunCommand,
]
StartSegmentBuilder = Callable[
    [str, JournalKeyDomain, Callable[[], None]],
    StartSegmentCommand,
]
_PENDING_IDENTITY_UNSET = object()
_T = TypeVar("_T")


@dataclass(frozen=True)
class EventInput:
    event_type: str
    facts: Mapping[str, object]
    telemetry: Mapping[str, object] = field(default_factory=dict)
    model_step: int | None = None
    model_call_id: str | None = None
    source_ref_type: str | None = None
    source_ref_id: object = None


@dataclass(frozen=True)
class SuspendedDisposition:
    tool_call_id: str
    tool_name: str
    tool_kind: Literal["read", "write"]
    args_shape_digest: str
    pending_identity_fingerprint: str | None = None
    pending_identity: object = field(default=_PENDING_IDENTITY_UNSET, repr=False)


@dataclass(frozen=True)
class TerminalDisposition:
    status: TerminalStatus
    failure_code: str | None = None


@dataclass(frozen=True)
class ResumedDisposition:
    confirmation_attempt_id: str
    tool_call_id: str


class RunRecorder(Protocol):
    @property
    def run_id(self) -> str | None: ...

    @property
    def segment_id(self) -> str | None: ...

    @property
    def diagnostics(self) -> list[str]: ...

    def start_segment(self, command: StartSegmentCommand) -> None: ...

    def attach_input_message(self, message_id: int) -> None: ...

    def capture_context(
        self,
        logical_input: object,
        manifest: ContextManifestInput,
        *,
        snapshot_kind: str,
        model_step: int | None = None,
        model_call_id: str | None = None,
        estimated_token_count: int | None = None,
        token_estimator_name: str | None = None,
        token_estimator_version: str | None = None,
    ) -> str | None: ...

    def capture_surface_context(
        self,
        logical_input: object,
        audit: RuntimeSurfaceAudit,
        provider_identities: tuple[str, ...],
        *,
        model_step: int,
        model_call_id: str,
    ) -> str | None: ...

    def append_event(self, event: EventInput) -> None: ...

    def resume(self, command: ResumedDisposition) -> None: ...

    def suspend(self, command: SuspendedDisposition) -> None: ...

    def abandon(self) -> None: ...

    def finish(self, command: TerminalDisposition) -> None: ...

    def fingerprint_model_id(self, value: str) -> str | None: ...

    def fingerprint_pending_identity(self, value: object) -> str | None: ...


class NullRunRecorder:
    run_id = None
    segment_id = None
    recording_status: RecordingStatus = "healthy"

    def __init__(self, diagnostics: list[str] | None = None) -> None:
        self.diagnostics = list(diagnostics or ())

    def start_segment(self, _command: StartSegmentCommand) -> None:
        return None

    def attach_input_message(self, _message_id: int) -> None:
        return None

    def capture_context(
        self,
        _logical_input: object,
        _manifest: ContextManifestInput,
        *,
        snapshot_kind: str,
        model_step: int | None = None,
        model_call_id: str | None = None,
        estimated_token_count: int | None = None,
        token_estimator_name: str | None = None,
        token_estimator_version: str | None = None,
    ) -> None:
        del (
            snapshot_kind,
            model_step,
            model_call_id,
            estimated_token_count,
            token_estimator_name,
            token_estimator_version,
        )
        return None

    def append_event(self, _event: EventInput) -> None:
        return None

    def capture_surface_context(
        self,
        _logical_input: object,
        _audit: RuntimeSurfaceAudit,
        _provider_identities: tuple[str, ...],
        *,
        model_step: int,
        model_call_id: str,
    ) -> None:
        del model_step, model_call_id
        return None

    def resume(self, _command: ResumedDisposition) -> None:
        return None

    def suspend(self, _command: SuspendedDisposition) -> None:
        return None

    def abandon(self) -> None:
        return None

    def finish(self, _command: TerminalDisposition) -> None:
        return None

    def fingerprint_model_id(self, _value: str) -> None:
        return None

    def fingerprint_pending_identity(self, _value: object) -> None:
        return None


class SafeRunRecorder:
    """Fail-open Journal facade with a cumulative active-work budget."""

    def __init__(
        self,
        repository: AgentRunRepository,
        key: JournalKeyDomain,
        run_id: str,
        segment_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        segment_budget_seconds: float = 0.150,
        disposition_budget_seconds: float = 0.050,
        segment_started_at: float | None = None,
        active_budget: ActiveWorkBudget | None = None,
        event_preparer: EventPreparer | None = None,
        context_preparer: ContextPreparer | None = None,
        uuid_factory: Callable[[], str] = lambda: str(uuid4()),
        diagnostic_sink: Callable[[str], None] | None = None,
    ) -> None:
        del segment_started_at
        self.repository = repository
        self.key = key
        self.run_id = run_id
        self.segment_id = segment_id
        self.segment_budget_seconds = segment_budget_seconds
        self.disposition_budget_seconds = disposition_budget_seconds
        self.active_budget = active_budget or ActiveWorkBudget(segment_budget_seconds, clock)
        self._event_preparer = event_preparer or self._prepare_event
        self._context_preparer = context_preparer or self._prepare_context
        self._uuid_factory = uuid_factory
        self._diagnostic_sink = diagnostic_sink
        self.recording_status: RecordingStatus = "healthy"
        self.diagnostics: list[str] = []
        self._degraded_persisted = False
        self._operation_lock = Lock()
        self._state_lock = RLock()
        self._state_condition = Condition(self._state_lock)
        self._resume_state: Literal["not_attempted", "claimed", "completed", "failed"] = (
            "not_attempted"
        )
        self._disposition_state: Literal[
            "not_attempted", "claimed", "completed", "failed"
        ] = "not_attempted"
        self._waits_for_resume = False
        self._wait_flag = False
        self._current_lease: OperationLease | None = None

    def start_segment(self, command: StartSegmentCommand) -> None:
        def operation(lease: OperationLease) -> None:
            if command.run_id != self.run_id or (
                command.segment_started.execution_segment_id != self.segment_id
            ):
                if self._degrade("journal_segment_identity_changed"):
                    self._sync_degraded(lease)
                return
            self.repository.start_segment(
                command,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )

        self._ordinary(operation, "journal_segment_write_failed", None)

    def attach_input_message(self, message_id: int) -> None:
        self._ordinary(
            lambda lease: self.repository.attach_input_message(
                self.run_id,
                message_id,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            ),
            "journal_message_link_failed",
            None,
        )

    def capture_context(
        self,
        logical_input: object,
        manifest: ContextManifestInput,
        *,
        snapshot_kind: str,
        model_step: int | None = None,
        model_call_id: str | None = None,
        estimated_token_count: int | None = None,
        token_estimator_name: str | None = None,
        token_estimator_version: str | None = None,
    ) -> str | None:
        def operation(lease: OperationLease) -> str:
            prepared = self._context_preparer(
                logical_input,
                manifest,
                lease.work_deadline,
            )
            lease.checkpoint()
            snapshot_id = self._uuid_factory()
            if snapshot_kind == "model_input":
                if model_call_id is None:
                    raise JournalEventValidationError("model call identity is required")
                snapshot_key = f"model-input:{self.segment_id}:{model_call_id}"
            elif snapshot_kind == "initial":
                snapshot_key = f"initial:{self.segment_id}"
            elif snapshot_kind == "confirmation_resume":
                snapshot_key = f"confirmation-resume:{self.segment_id}"
            else:
                raise JournalEventValidationError("unsupported snapshot kind")
            command = CaptureContextCommand(
                snapshot_id=snapshot_id,
                execution_segment_id=self.segment_id,
                snapshot_key=snapshot_key,
                snapshot_kind=snapshot_kind,
                model_step=model_step,
                model_call_id=model_call_id,
                prepared=prepared,
                estimated_token_count=estimated_token_count,
                token_estimator_name=token_estimator_name,
                token_estimator_version=token_estimator_version,
            )
            self.repository.capture_context(
                self.run_id,
                command,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            return snapshot_id

        return self._ordinary(operation, "journal_context_write_failed", None)

    def append_event(self, event: EventInput) -> None:
        def operation(lease: OperationLease) -> None:
            draft = self._event_preparer(event, lease.work_deadline)
            lease.checkpoint()
            self.repository.append_event(
                self.run_id,
                draft,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )

        self._ordinary(operation, "journal_event_write_failed", None)

    def capture_surface_context(
        self,
        logical_input: object,
        audit: RuntimeSurfaceAudit,
        provider_identities: tuple[str, ...],
        *,
        model_step: int,
        model_call_id: str,
    ) -> str | None:
        """Project transient audit to V2; every failure remains journal-only."""

        def operation(lease: OperationLease) -> str:
            from offerpilot.context_projector.manifest import prepare_surface_manifest_v2

            identity = prepare_context_snapshot(
                logical_input,
                ContextManifestInput((), (), (), ()),
                key=self.key,
                budget_check=lease.checkpoint,
            )
            manifest = prepare_surface_manifest_v2(
                audit,
                key_id=self.key.key_id,
                secret=self.key.secret,
                provider_identities=provider_identities,
                budget_check=lease.checkpoint,
            )
            prepared = PreparedSnapshot(
                manifest_schema_version=2,
                manifest_json=manifest.manifest_json,
                manifest_digest=manifest.manifest_digest,
                logical_input_fingerprint=identity.logical_input_fingerprint,
                fingerprint_key_id=self.key.key_id,
            )
            snapshot_id = self._uuid_factory()
            command = CaptureContextCommand(
                snapshot_id=snapshot_id,
                execution_segment_id=self.segment_id,
                snapshot_key=f"model-input:{self.segment_id}:{model_call_id}",
                snapshot_kind="model_input",
                model_step=model_step,
                model_call_id=model_call_id,
                prepared=prepared,
                estimated_token_count=audit.estimated_input_units,
                token_estimator_name="surface_conservative",
                token_estimator_version="utf8-upper-bound-v1",
            )
            self.repository.capture_context(
                self.run_id,
                command,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            return snapshot_id

        return self._ordinary(operation, "journal_context_write_failed", None)

    def prepare_event_draft(self, event: EventInput) -> EventDraft | None:
        return self._ordinary(
            lambda lease: self._event_preparer(event, lease.work_deadline),
            "journal_tool_projection_failed",
            None,
            allow_sync=False,
        )

    def append_prepared_event_bound(self, session: Any, draft: EventDraft) -> bool:
        def operation(lease: OperationLease) -> bool:
            lease.checkpoint()
            with session.begin_nested():
                self.repository.append_event_bound(session, self.run_id, draft)
            return True

        return self._ordinary(
            operation,
            "journal_tool_projection_failed",
            False,
            allow_sync=False,
        )

    def resume(self, command: ResumedDisposition) -> None:
        with self._state_lock:
            if (
                self._resume_state != "not_attempted"
                or self._disposition_state != "not_attempted"
            ):
                return
            self._resume_state = "claimed"
            self._state_condition.notify_all()

        succeeded = False

        def operation(lease: OperationLease) -> bool:
            event = self._event_preparer(
                EventInput(
                    event_type="run.resumed",
                    facts={
                        "confirmation_attempt_id": command.confirmation_attempt_id,
                        "tool_call_id": command.tool_call_id,
                    },
                    source_ref_type="tool_call",
                    source_ref_id=command.tool_call_id,
                ),
                lease.work_deadline,
            )
            self.repository.converge_disposition(
                self.run_id,
                DispositionCommand(
                    target_status="running",
                    events=(event,),
                    waiting_tool_call_id=None,
                    failure_code=None,
                ),
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            return True

        try:
            succeeded = bool(
                self._ordinary(
                    operation,
                    "journal_resume_failed",
                    False,
                    state_check=self._resume_operation_allowed_locked,
                )
            )
        finally:
            with self._state_lock:
                self._resume_state = "completed" if succeeded else "failed"
                self._wait_flag = False
                self._state_condition.notify_all()

    def suspend(self, command: SuspendedDisposition) -> None:
        def inputs(lease: OperationLease) -> tuple[EventDraft, ...]:
            pending_fingerprint = command.pending_identity_fingerprint
            if pending_fingerprint is None:
                if command.pending_identity is _PENDING_IDENTITY_UNSET:
                    raise JournalEventValidationError("pending identity is required")
                pending_fingerprint = pending_identity_fingerprint(
                    self.key,
                    command.pending_identity,
                    budget_check=lease.checkpoint,
                )
            values = (
                EventInput(
                    event_type="tool.proposed",
                    facts={
                        "tool_call_id": command.tool_call_id,
                        "tool_name": command.tool_name,
                        "tool_kind": command.tool_kind,
                        "args_shape_digest": command.args_shape_digest,
                        "proposal_outcome": "confirmation_required",
                    },
                    source_ref_type="tool_call",
                    source_ref_id=command.tool_call_id,
                ),
                EventInput(
                    event_type="approval.requested",
                    facts={
                        "tool_call_id": command.tool_call_id,
                        "confirmation_mode": "required",
                        "pending_identity_fingerprint": pending_fingerprint,
                    },
                    source_ref_type="tool_call",
                    source_ref_id=command.tool_call_id,
                ),
                EventInput(
                    event_type="run.waiting_confirmation",
                    facts={"tool_call_id": command.tool_call_id},
                    source_ref_type="tool_call",
                    source_ref_id=command.tool_call_id,
                ),
                EventInput(
                    event_type="segment.finished",
                    facts={"outcome": "suspended", "terminal_run_status": None},
                ),
            )
            return tuple(self._event_preparer(value, lease.work_deadline) for value in values)

        self._converge(
            inputs,
            target_status="waiting_confirmation",
            waiting_tool_call_id=command.tool_call_id,
            failure_code=None,
        )

    def finish(self, command: TerminalDisposition) -> None:
        def inputs(lease: OperationLease) -> tuple[EventDraft, ...]:
            values = (
                EventInput(
                    event_type=f"run.{command.status}",
                    facts={
                        "agent_run_id": self.run_id,
                        "status": command.status,
                        "failure_code": command.failure_code,
                    },
                ),
                EventInput(
                    event_type="segment.finished",
                    facts={
                        "outcome": command.status,
                        "terminal_run_status": command.status,
                    },
                ),
            )
            return tuple(self._event_preparer(value, lease.work_deadline) for value in values)

        self._converge(
            inputs,
            target_status=command.status,
            waiting_tool_call_id=None,
            failure_code=command.failure_code,
        )

    def abandon(self) -> None:
        def operation(lease: OperationLease) -> bool:
            event = self._event_preparer(
                EventInput(
                    event_type="segment.finished",
                    facts={"outcome": "noop", "terminal_run_status": None},
                ),
                lease.work_deadline,
            )
            self.repository.append_event(
                self.run_id,
                event,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            if self.recording_status == "degraded":
                self._sync_degraded(lease)
            return True

        self._run_final(operation, "journal_disposition_failed")

    def mark_degraded(self, diagnostic: str = "journal_recording_degraded") -> None:
        self._degrade(diagnostic)

    def fingerprint_model_id(self, value: str) -> str | None:
        return self._ordinary(
            lambda lease: model_id_fingerprint(
                self.key,
                value,
                budget_check=lease.checkpoint,
            ),
            "journal_fingerprint_failed",
            None,
        )

    def fingerprint_pending_identity(self, value: object) -> str | None:
        return self._ordinary(
            lambda lease: pending_identity_fingerprint(
                self.key,
                value,
                budget_check=lease.checkpoint,
            ),
            "journal_fingerprint_failed",
            None,
        )

    def _prepare_event(self, value: EventInput, deadline: float) -> EventDraft:
        self._checkpoint(deadline)
        facts = dict(value.facts)
        contains_hmac = any(field.endswith("_fingerprint") for field in facts)
        draft = prepare_event(
            event_type=value.event_type,
            execution_segment_id=self.segment_id,
            facts=facts,
            telemetry=dict(value.telemetry),
            model_step=value.model_step,
            model_call_id=value.model_call_id,
            source_ref_type=value.source_ref_type,
            source_ref_id=value.source_ref_id,
            fingerprint_key_id=self.key.key_id if contains_hmac else None,
            budget_check=self._checkpoint_callback,
        )
        self._checkpoint(deadline)
        return draft

    def _prepare_context(
        self,
        logical_input: object,
        manifest: ContextManifestInput,
        deadline: float,
    ) -> PreparedSnapshot:
        self._checkpoint(deadline)
        prepared = prepare_context_snapshot(
            logical_input,
            manifest,
            key=self.key,
            budget_check=self._checkpoint_callback,
        )
        self._checkpoint(deadline)
        return prepared

    def _ordinary(
        self,
        operation: Callable[[OperationLease], _T],
        failure_diagnostic: str,
        default: _T,
        *,
        allow_sync: bool = True,
        state_check: Callable[[], bool] | None = None,
    ) -> _T:
        entry = self.active_budget.safe_monotonic_read()
        lease: OperationLease | None = None
        acquired = False
        result = default
        primary_base: BaseException | None = None
        cleanup_base: BaseException | None = None
        work_started = False
        used_before: float | None = None
        clock_invalid_before = self.active_budget.clock_invalid_latched

        try:
            try:
                lease = self.active_budget.begin_operation(
                    entry,
                    JOURNAL_OPERATION_HARD_CAP_SECONDS,
                )
                with self._state_lock:
                    pre_allowed = self._ordinary_state_allowed_locked(state_check)
                if pre_allowed:
                    acquired = self._acquire_operation(lease)
                    if not acquired:
                        self._degrade("journal_budget_exhausted")
                    else:
                        refreshed = self.active_budget.begin_operation(
                            entry,
                            JOURNAL_OPERATION_HARD_CAP_SECONDS,
                        )
                        lease = self._tighten_lease(lease, refreshed)
                        lease.checkpoint()
                        with self._state_lock:
                            allowed = self._ordinary_state_allowed_locked(state_check)
                        if allowed:
                            used_before = self.active_budget.used_seconds
                            clock_invalid_before = self.active_budget.clock_invalid_latched
                            work_started = True
                            self._current_lease = lease
                            try:
                                result = operation(lease)
                            except Exception as error:
                                self._record_failure(
                                    error,
                                    failure_diagnostic,
                                    lease,
                                    allow_sync=allow_sync,
                                )
                            except BaseException as error:
                                primary_base = error
            except Exception as error:
                self._record_failure(
                    error,
                    failure_diagnostic,
                    lease,
                    allow_sync=allow_sync and work_started,
                )
            except BaseException as error:
                primary_base = error
        finally:
            try:
                try:
                    if acquired:
                        self._cleanup_operation(lease)
                except Exception:
                    first_transition = self._degrade("journal_cleanup_failed")
                    if (
                        first_transition
                        and allow_sync
                        and lease is not None
                        and lease is self._current_lease
                    ):
                        try:
                            self._sync_degraded(lease)
                        except BaseException as error:
                            cleanup_base = error
                except BaseException as error:
                    cleanup_base = error
            finally:
                self._current_lease = None
                try:
                    exhausted = self.active_budget.finish_operation(entry)
                except BaseException:
                    self.active_budget.latch_clock_invalid()
                    exhausted = True
                if self.active_budget.clock_invalid_latched and not clock_invalid_before:
                    self._degrade("journal_clock_invalid")
                elif (
                    self.recording_status != "degraded"
                    and (
                        exhausted
                        or (
                            used_before is not None
                            and self.active_budget.used_seconds - used_before
                            >= JOURNAL_OPERATION_HARD_CAP_SECONDS
                        )
                    )
                ):
                    self._degrade("journal_budget_exhausted")
                if acquired:
                    self._operation_lock.release()

        if primary_base is not None:
            raise primary_base.with_traceback(primary_base.__traceback__)
        if cleanup_base is not None:
            raise cleanup_base.with_traceback(cleanup_base.__traceback__)
        return result

    def _ordinary_state_allowed_locked(
        self,
        state_check: Callable[[], bool] | None,
    ) -> bool:
        if self.recording_status != "healthy":
            return False
        if state_check is not None:
            return state_check()
        return self._disposition_state == "not_attempted"

    def _resume_operation_allowed_locked(self) -> bool:
        return self._resume_state == "claimed" and (
            self._disposition_state == "not_attempted"
            or (
                self._disposition_state == "claimed"
                and self._waits_for_resume
            )
        )

    def _acquire_operation(self, lease: OperationLease) -> bool:
        timeout = max(0.0, lease.hard_deadline - lease.entry_started_at)
        try:
            return self._operation_lock.acquire(timeout=timeout)
        except OverflowError:
            return self._operation_lock.acquire(blocking=False)

    def _acquire_final_operation(self, lease: OperationLease) -> bool:
        sample = lease.safe_clock.sample()
        if sample.valid is not True:
            lease.budget.latch_clock_invalid()
            return False
        acquire_lease = OperationLease(
            budget=lease.budget,
            entry_started_at=sample.value,
            work_deadline=lease.work_deadline,
            hard_deadline=lease.hard_deadline,
        )
        return self._acquire_operation(acquire_lease)

    def _tighten_lease(
        self,
        original: OperationLease,
        refreshed: OperationLease,
    ) -> OperationLease:
        hard_deadline = min(original.hard_deadline, refreshed.hard_deadline)
        reserve = original.hard_deadline - original.work_deadline
        return OperationLease(
            budget=self.active_budget,
            entry_started_at=original.entry_started_at,
            work_deadline=hard_deadline - reserve,
            hard_deadline=hard_deadline,
        )

    def _record_failure(
        self,
        error: Exception,
        failure_diagnostic: str,
        lease: OperationLease | None,
        *,
        allow_sync: bool,
    ) -> None:
        diagnostic = self._diagnostic_for(error, failure_diagnostic, lease)
        first_transition = self._degrade(diagnostic)
        if first_transition and allow_sync and lease is not None and diagnostic not in {
            "journal_budget_exhausted",
            "journal_clock_invalid",
        }:
            self._sync_degraded(lease)

    def _diagnostic_for(
        self,
        error: Exception,
        failure_diagnostic: str,
        lease: OperationLease | None,
    ) -> str:
        if self.active_budget.clock_invalid_latched and not isinstance(
            error, JournalEventValidationError
        ):
            return "journal_clock_invalid"
        if isinstance(error, JournalDeadlineExceeded):
            return (
                "journal_clock_invalid"
                if error.reason == "clock_invalid"
                else "journal_budget_exhausted"
            )
        if isinstance(error, JournalBudgetExhausted):
            return "journal_budget_exhausted"
        if isinstance(error, JournalEventValidationError):
            if failure_diagnostic.startswith("journal_context"):
                return "journal_context_invalid"
            if failure_diagnostic.startswith("journal_event"):
                return "journal_event_invalid"
            if failure_diagnostic.startswith("journal_resume"):
                return "journal_resume_invalid"
            return failure_diagnostic
        if isinstance(error, OperationalError):
            if _is_sqlite_lock_error(error):
                return "journal_budget_exhausted"
            exhausted = self._lease_exhaustion_diagnostic(lease)
            return exhausted or failure_diagnostic
        exhausted = self._lease_exhaustion_diagnostic(lease)
        return exhausted or failure_diagnostic

    def _lease_exhaustion_diagnostic(self, lease: OperationLease | None) -> str | None:
        if self.active_budget.clock_invalid_latched:
            return "journal_clock_invalid"
        if lease is None:
            return "journal_clock_invalid" if self.active_budget.clock_invalid_latched else None
        try:
            lease.checkpoint()
        except JournalDeadlineExceeded as error:
            return "journal_clock_invalid" if error.reason == "clock_invalid" else "journal_budget_exhausted"
        except JournalBudgetExhausted:
            return "journal_budget_exhausted"
        return None

    def _cleanup_operation(self, _lease: OperationLease | None) -> None:
        return None

    def _checkpoint(self, _deadline: float) -> None:
        if self._current_lease is not None:
            self._current_lease.checkpoint()
            return
        sample = self.active_budget.safe_monotonic_read()
        if sample.valid is not True:
            self.active_budget.latch_clock_invalid()
            raise JournalDeadlineExceeded("clock_invalid")

    def _checkpoint_callback(self) -> None:
        if self._current_lease is None:
            raise JournalBudgetExhausted
        self._current_lease.checkpoint()

    def _converge(
        self,
        prepare: Callable[[OperationLease], tuple[EventDraft, ...]],
        *,
        target_status: RunStatus,
        waiting_tool_call_id: str | None,
        failure_code: str | None,
    ) -> None:
        def operation(lease: OperationLease) -> bool:
            events = prepare(lease)
            lease.checkpoint()
            self.repository.converge_disposition(
                self.run_id,
                DispositionCommand(
                    target_status=target_status,
                    events=events,
                    waiting_tool_call_id=waiting_tool_call_id,
                    failure_code=failure_code,
                ),
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            if self.recording_status == "degraded":
                self._sync_degraded(lease)
            if target_status == "waiting_confirmation":
                with self._state_lock:
                    self._wait_flag = True
            return True

        self._run_final(operation, "journal_disposition_failed")

    def _run_final(
        self,
        operation: Callable[[OperationLease], bool],
        failure_diagnostic: str,
    ) -> None:
        budget = ActiveWorkBudget(self.disposition_budget_seconds, self.active_budget.clock)
        entry = budget.safe_monotonic_read()
        invalid_entry = False
        with self._state_lock:
            if self._disposition_state != "not_attempted":
                return
            if entry.valid is not True:
                self._disposition_state = "failed"
                invalid_entry = True
            else:
                self._disposition_state = "claimed"
                self._waits_for_resume = self._resume_state == "claimed"
                self._wait_flag = self._waits_for_resume
            self._state_condition.notify_all()

        if invalid_entry:
            budget.latch_clock_invalid()
            self._degrade("journal_clock_invalid")
            return

        hard_deadline = entry.value + self.disposition_budget_seconds
        lease: OperationLease | None = None
        acquired = False
        succeeded = False
        primary_base: BaseException | None = None
        cleanup_base: BaseException | None = None

        try:
            try:
                lease = OperationLease(
                    budget=budget,
                    entry_started_at=entry.value,
                    work_deadline=hard_deadline - JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS,
                    hard_deadline=hard_deadline,
                )
                self._wait_for_resume(lease)
                lease.checkpoint()
                acquired = self._acquire_final_operation(lease)
                if not acquired:
                    deadline_error = self._final_deadline_error(lease)
                    raise deadline_error or JournalDeadlineExceeded("deadline")
                else:
                    lease.checkpoint()
                    with self._state_lock:
                        allowed = self._disposition_state == "claimed"
                    if allowed:
                        self._current_lease = lease
                        try:
                            succeeded = bool(operation(lease))
                        except Exception as error:
                            self._record_final_failure(error, failure_diagnostic, lease)
                        except BaseException as error:
                            primary_base = error
            except Exception as error:
                self._record_final_failure(error, failure_diagnostic, lease)
            except BaseException as error:
                primary_base = error
        finally:
            try:
                try:
                    if acquired:
                        self._cleanup_operation(lease)
                except Exception:
                    succeeded = False
                    if (
                        self._degrade("journal_cleanup_failed")
                        and lease is not None
                        and lease is self._current_lease
                    ):
                        try:
                            self._sync_degraded(lease)
                        except BaseException as error:
                            cleanup_base = error
                except BaseException as error:
                    cleanup_base = error
                    succeeded = False
            finally:
                self._current_lease = None
                try:
                    exhausted = budget.finish_operation(entry)
                except BaseException:
                    budget.latch_clock_invalid()
                    exhausted = True
                if budget.clock_invalid_latched:
                    self._degrade("journal_clock_invalid")
                elif exhausted:
                    self._degrade("journal_disposition_budget_exhausted")
                if acquired:
                    self._operation_lock.release()
                with self._state_lock:
                    self._waits_for_resume = False
                    self._wait_flag = False
                    self._disposition_state = "completed" if succeeded else "failed"
                    self._state_condition.notify_all()

        if primary_base is not None:
            raise primary_base.with_traceback(primary_base.__traceback__)
        if cleanup_base is not None:
            raise cleanup_base.with_traceback(cleanup_base.__traceback__)

    def _wait_for_resume(self, lease: OperationLease) -> None:
        while True:
            with self._state_lock:
                if not (self._waits_for_resume and self._resume_state == "claimed"):
                    return
                sample = lease.safe_clock.sample()
                if sample.valid is not True:
                    lease.budget.latch_clock_invalid()
                    raise JournalDeadlineExceeded("clock_invalid")
                remaining = lease.hard_deadline - sample.value
                if remaining <= 0:
                    raise JournalDeadlineExceeded("deadline")
                if not self._state_condition.wait(timeout=remaining):
                    if not (self._waits_for_resume and self._resume_state == "claimed"):
                        return
                    sample = lease.safe_clock.sample()
                    if sample.valid is not True:
                        lease.budget.latch_clock_invalid()
                        raise JournalDeadlineExceeded("clock_invalid")
                    raise JournalDeadlineExceeded("deadline")

    def _final_deadline_error(self, lease: OperationLease) -> JournalDeadlineExceeded | None:
        sample = lease.safe_clock.sample()
        if sample.valid is not True:
            lease.budget.latch_clock_invalid()
            return JournalDeadlineExceeded("clock_invalid")
        if sample.value >= lease.hard_deadline:
            return JournalDeadlineExceeded("deadline")
        return None

    def _record_final_failure(
        self,
        error: Exception,
        failure_diagnostic: str,
        lease: OperationLease | None,
    ) -> None:
        diagnostic = self._diagnostic_for_final(error, failure_diagnostic, lease)
        first_transition = self._degrade(diagnostic)
        if first_transition and lease is not None and diagnostic not in {
            "journal_disposition_budget_exhausted",
            "journal_clock_invalid",
        }:
            self._sync_degraded(lease)

    def _diagnostic_for_final(
        self,
        error: Exception,
        failure_diagnostic: str,
        lease: OperationLease | None,
    ) -> str:
        if (
            lease is not None
            and lease.budget.clock_invalid_latched
            and not isinstance(error, JournalEventValidationError)
        ):
            return "journal_clock_invalid"
        if isinstance(error, JournalDeadlineExceeded):
            return (
                "journal_clock_invalid"
                if error.reason == "clock_invalid"
                else "journal_disposition_budget_exhausted"
            )
        if isinstance(error, JournalBudgetExhausted):
            return "journal_disposition_budget_exhausted"
        if isinstance(error, JournalEventValidationError):
            return "journal_disposition_invalid"
        if isinstance(error, OperationalError):
            if _is_sqlite_lock_error(error):
                return "journal_disposition_budget_exhausted"
            if lease is not None:
                try:
                    lease.checkpoint()
                except JournalDeadlineExceeded as deadline_error:
                    return (
                        "journal_clock_invalid"
                        if deadline_error.reason == "clock_invalid"
                        else "journal_disposition_budget_exhausted"
                    )
                except JournalBudgetExhausted:
                    return "journal_disposition_budget_exhausted"
            return failure_diagnostic
        return failure_diagnostic

    def _sync_degraded(self, lease: OperationLease) -> None:
        with self._state_lock:
            if self._degraded_persisted:
                return
        try:
            lease.checkpoint()
            self.repository.mark_degraded(
                self.run_id,
                deadline=lease.work_deadline,
                safe_clock=lease.safe_clock,
            )
            with self._state_lock:
                self._degraded_persisted = True
        except JournalDeadlineExceeded as error:
            if error.reason == "clock_invalid":
                self._degrade("journal_clock_invalid")
        except JournalBudgetExhausted:
            return
        except OperationalError as error:
            if _is_sqlite_lock_error(error):
                return
            self._diagnose("journal_mark_degraded_failed")
        except Exception:
            self._diagnose("journal_mark_degraded_failed")

    def _degrade(self, diagnostic: str) -> bool:
        with self._state_lock:
            first_transition = self.recording_status != "degraded"
            self.recording_status = "degraded"
            should_emit = diagnostic not in self.diagnostics
            if should_emit:
                self.diagnostics.append(diagnostic)
            self._state_condition.notify_all()
        if should_emit:
            self._emit_diagnostic(diagnostic)
        return first_transition

    def _diagnose(self, code: str) -> None:
        with self._state_lock:
            if code in self.diagnostics:
                return
            self.diagnostics.append(code)
        self._emit_diagnostic(code)

    def _emit_diagnostic(self, code: str) -> None:
        if self._diagnostic_sink is not None:
            try:
                self._diagnostic_sink(code)
            except BaseException:
                pass


class RunRecorderFactory:
    def __init__(
        self,
        repository: AgentRunRepository,
        *,
        key: JournalKeyDomain | None,
        enabled: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        segment_budget_seconds: float = 0.150,
        disposition_budget_seconds: float = 0.050,
        diagnostic_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.key = key
        self.enabled = _journal_enabled_from_env() if enabled is None else enabled
        self._clock = clock
        self.segment_budget_seconds = segment_budget_seconds
        self.disposition_budget_seconds = disposition_budget_seconds
        self._diagnostic_sink = diagnostic_sink
        self.diagnostics: list[str] = []

    def start_run(self, command: StartRunCommand | StartRunBuilder) -> RunRecorder:
        if not self.enabled:
            return NullRunRecorder()
        if self.key is None:
            return self._null("journal_secret_unavailable")

        budget = ActiveWorkBudget(self.segment_budget_seconds, self._clock)
        entry = budget.safe_monotonic_read()
        lease: OperationLease | None = None
        result_command: StartRunCommand | None = None
        diagnostic: str | None = None
        primary_base: BaseException | None = None
        used_before = budget.used_seconds
        try:
            try:
                lease = budget.begin_operation(entry, JOURNAL_OPERATION_HARD_CAP_SECONDS)
                lease.checkpoint()
                if callable(command):
                    command = command(self.key, lease.checkpoint)
                lease.checkpoint()
                if command.fingerprint_key_id != self.key.key_id:
                    diagnostic = "fingerprint_key_domain_changed"
                else:
                    self.repository.create_run_and_initial_segment(
                        command,
                        deadline=lease.work_deadline,
                        safe_clock=lease.safe_clock,
                    )
                    result_command = command
            except JournalDeadlineExceeded as error:
                diagnostic = (
                    "journal_clock_invalid"
                    if error.reason == "clock_invalid"
                    else "journal_budget_exhausted"
                )
            except JournalBudgetExhausted:
                diagnostic = "journal_budget_exhausted"
            except OperationalError as error:
                exhausted_diagnostic = _factory_lease_exhaustion(lease)
                diagnostic = exhausted_diagnostic or (
                    "journal_budget_exhausted"
                    if _is_sqlite_lock_error(error)
                    else "journal_run_create_failed"
                )
            except Exception:
                diagnostic = (
                    _factory_lease_exhaustion(lease)
                    or "journal_run_create_failed"
                )
            except BaseException as error:
                primary_base = error
        finally:
            try:
                exhausted = budget.finish_operation(entry)
            except BaseException:
                budget.latch_clock_invalid()
                exhausted = True
            if budget.clock_invalid_latched:
                diagnostic = "journal_clock_invalid"
            elif (
                diagnostic is None
                and (
                    exhausted
                    or budget.used_seconds - used_before
                    >= JOURNAL_OPERATION_HARD_CAP_SECONDS
                )
            ):
                diagnostic = "journal_budget_exhausted"

        if primary_base is not None:
            raise primary_base.with_traceback(primary_base.__traceback__)
        if result_command is None:
            return self._null(diagnostic or "journal_run_create_failed")
        recorder = self._safe(
            result_command.run_id,
            result_command.segment_started.execution_segment_id,
            budget,
        )
        if diagnostic in {"journal_budget_exhausted", "journal_clock_invalid"} or budget.used_seconds >= budget.total_seconds:
            recorder.mark_degraded(
                "journal_clock_invalid"
                if budget.clock_invalid_latched or diagnostic == "journal_clock_invalid"
                else "journal_budget_exhausted"
            )
        return recorder

    def resume_waiting_run(
        self,
        conversation_id: int,
        waiting_tool_call_id: str,
        command: StartSegmentCommand | StartSegmentBuilder,
    ) -> RunRecorder:
        if not self.enabled:
            return NullRunRecorder()
        if self.key is None:
            return self._null("journal_secret_unavailable")

        budget = ActiveWorkBudget(self.segment_budget_seconds, self._clock)
        entry = budget.safe_monotonic_read()
        lease: OperationLease | None = None
        run: Any = None
        result_command: StartSegmentCommand | None = None
        diagnostic: str | None = None
        primary_base: BaseException | None = None
        used_before = budget.used_seconds
        try:
            try:
                lease = budget.begin_operation(entry, JOURNAL_OPERATION_HARD_CAP_SECONDS)
                lease.checkpoint()
                run = self.repository.find_waiting_run(
                    conversation_id,
                    waiting_tool_call_id,
                    deadline=lease.work_deadline,
                    safe_clock=lease.safe_clock,
                )
                lease.checkpoint()
                if run is None:
                    diagnostic = "journal_run_missing"
                elif run.fingerprint_key_id != self.key.key_id:
                    diagnostic = "fingerprint_key_domain_changed"
                else:
                    if callable(command):
                        command = command(run.id, self.key, lease.checkpoint)
                    lease.checkpoint()
                    if command.run_id != run.id:
                        diagnostic = "journal_run_identity_changed"
                    else:
                        self.repository.start_segment(
                            command,
                            deadline=lease.work_deadline,
                            safe_clock=lease.safe_clock,
                        )
                        result_command = command
            except JournalDeadlineExceeded as error:
                diagnostic = (
                    "journal_clock_invalid"
                    if error.reason == "clock_invalid"
                    else "journal_budget_exhausted"
                )
            except JournalBudgetExhausted:
                diagnostic = "journal_budget_exhausted"
            except OperationalError as error:
                exhausted_diagnostic = _factory_lease_exhaustion(lease)
                diagnostic = exhausted_diagnostic or (
                    "journal_budget_exhausted"
                    if _is_sqlite_lock_error(error)
                    else (
                        "journal_segment_create_failed"
                        if run is not None
                        else "journal_run_lookup_failed"
                    )
                )
            except Exception:
                exhausted_diagnostic = _factory_lease_exhaustion(lease)
                diagnostic = exhausted_diagnostic or (
                    "journal_segment_create_failed"
                    if run is not None
                    else "journal_run_lookup_failed"
                )
            except BaseException as error:
                primary_base = error
        finally:
            try:
                exhausted = budget.finish_operation(entry)
            except BaseException:
                budget.latch_clock_invalid()
                exhausted = True
            if budget.clock_invalid_latched:
                diagnostic = "journal_clock_invalid"
            elif (
                diagnostic is None
                and (
                    exhausted
                    or budget.used_seconds - used_before
                    >= JOURNAL_OPERATION_HARD_CAP_SECONDS
                )
            ):
                diagnostic = "journal_budget_exhausted"

        if primary_base is not None:
            raise primary_base.with_traceback(primary_base.__traceback__)
        if result_command is None or run is None:
            return self._null(diagnostic or "journal_segment_create_failed")
        recorder = self._safe(
            run.id,
            result_command.segment_started.execution_segment_id,
            budget,
        )
        if diagnostic in {"journal_budget_exhausted", "journal_clock_invalid"} or budget.used_seconds >= budget.total_seconds:
            recorder.mark_degraded(
                "journal_clock_invalid"
                if budget.clock_invalid_latched or diagnostic == "journal_clock_invalid"
                else "journal_budget_exhausted"
            )
        return recorder

    def _safe(
        self,
        run_id: str,
        segment_id: str,
        active_budget: ActiveWorkBudget,
    ) -> SafeRunRecorder:
        assert self.key is not None
        return SafeRunRecorder(
            self.repository,
            self.key,
            run_id,
            segment_id,
            clock=active_budget.clock,
            segment_budget_seconds=self.segment_budget_seconds,
            disposition_budget_seconds=self.disposition_budget_seconds,
            active_budget=active_budget,
            diagnostic_sink=self._diagnostic_sink,
        )

    def _null(self, diagnostic: str) -> NullRunRecorder:
        self._diagnose(diagnostic)
        return NullRunRecorder([diagnostic])

    def _diagnose(self, code: str) -> None:
        if code not in self.diagnostics:
            self.diagnostics.append(code)
        if self._diagnostic_sink is not None:
            try:
                self._diagnostic_sink(code)
            except BaseException:
                pass


class NullRunRecorderFactory:
    def __init__(self, diagnostic: str | None = None) -> None:
        self.diagnostics = [] if diagnostic is None else [diagnostic]

    def start_run(self, _command: StartRunCommand | StartRunBuilder) -> RunRecorder:
        return NullRunRecorder(self.diagnostics)

    def resume_waiting_run(
        self,
        _conversation_id: int,
        _waiting_tool_call_id: str,
        _command: StartSegmentCommand | StartSegmentBuilder,
    ) -> RunRecorder:
        return NullRunRecorder(self.diagnostics)


def _is_sqlite_lock_error(error: OperationalError) -> bool:
    sqlite_error_code = getattr(error.orig, "sqlite_errorcode", None)
    return type(sqlite_error_code) is int and sqlite_error_code & 0xFF in {5, 6}


def _factory_lease_exhaustion(lease: OperationLease | None) -> str | None:
    if lease is None:
        return None
    if lease.budget.clock_invalid_latched:
        return "journal_clock_invalid"
    try:
        lease.checkpoint()
    except JournalDeadlineExceeded as error:
        return "journal_clock_invalid" if error.reason == "clock_invalid" else "journal_budget_exhausted"
    except JournalBudgetExhausted:
        return "journal_budget_exhausted"
    return None


def _journal_enabled_from_env() -> bool:
    value = os.getenv("OFFERPILOT_AGENT_JOURNAL_ENABLED", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


__all__ = [
    "EventInput",
    "NullRunRecorderFactory",
    "NullRunRecorder",
    "RunRecorder",
    "RunRecorderFactory",
    "SafeRunRecorder",
    "ResumedDisposition",
    "StartRunBuilder",
    "StartSegmentBuilder",
    "SuspendedDisposition",
    "TerminalDisposition",
]
