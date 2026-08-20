from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.agent_runtime.budget import (
    JOURNAL_DEFAULT_BUSY_TIMEOUT_MS,
    JOURNAL_SQLITE_PROGRESS_STEPS,
    JournalDeadlineExceeded,
    SafeClockAdapter,
)
from offerpilot.agent_runtime.events import (
    EventDraft,
    JournalEventValidationError,
    PreparedSnapshot,
    prepare_event,
    validate_context_manifest_json,
    validate_event_draft,
)
from offerpilot.models import AgentContextSnapshot, AgentEvent, AgentRun, ChatMessage

RunStatus = Literal[
    "running",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]
TerminalRunStatus = Literal["completed", "failed", "cancelled", "timed_out"]

_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
_DISPOSITION_EVENT_TYPES = {
    "context.captured",
    "run.started",
    "run.waiting_confirmation",
    "run.resumed",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "run.timed_out",
    "segment.started",
    "segment.finished",
}
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_ModelT = TypeVar("_ModelT")


@dataclass(frozen=True)
class StartRunCommand:
    run_id: str
    conversation_id: int
    input_message_id: int | None
    origin_kind: str
    initial_context_type: str
    initial_context_entity_id: str | None
    initial_context_ref_fingerprint: str | None
    fingerprint_key_id: str
    initial_transport_mode: str
    initial_route_kind: str
    run_started: EventDraft
    segment_started: EventDraft


@dataclass(frozen=True)
class StartSegmentCommand:
    run_id: str
    segment_started: EventDraft


@dataclass(frozen=True)
class CaptureContextCommand:
    snapshot_id: str
    execution_segment_id: str
    snapshot_key: str
    snapshot_kind: str
    model_step: int | None
    model_call_id: str | None
    prepared: PreparedSnapshot
    estimated_token_count: int | None = None
    token_estimator_name: str | None = None
    token_estimator_version: str | None = None


@dataclass(frozen=True)
class DispositionCommand:
    target_status: RunStatus
    events: tuple[EventDraft, ...]
    waiting_tool_call_id: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class StartedRun:
    run: AgentRun
    events: tuple[AgentEvent, AgentEvent]


@dataclass(frozen=True)
class CapturedContext:
    snapshot: AgentContextSnapshot
    event: AgentEvent


@dataclass(frozen=True)
class RunJournalRows:
    run: AgentRun | None
    events: tuple[AgentEvent, ...]
    snapshots: tuple[AgentContextSnapshot, ...]


class JournalConflictError(RuntimeError):
    pass


@dataclass
class _SQLiteGuard:
    connection: Any
    raw_connection: Any
    progress_installed: bool
    connection_record: Any | None = None


def _progress_handler(
    safe_clock: SafeClockAdapter,
    deadline: float,
) -> Callable[[], int]:
    """Return a total SQLite callback that never propagates clock failures."""

    def progress() -> int:
        try:
            sample = safe_clock.sample()
            if sample.valid is not True:
                return 1
            return int(sample.value >= deadline)
        except BaseException:
            return 1

    return progress


class AgentRunRepository:
    """Own short, independent Journal transactions and immutable event ordering."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        supports_returning: Callable[[Session], bool] | None = None,
        before_event_insert: Callable[[AgentEvent], None] | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.session_factory = session_factory
        self._supports_returning = supports_returning or self._dialect_supports_returning
        self._before_event_insert = before_event_insert
        self._now_factory = now_factory

    def create_run_and_initial_segment(
        self,
        command: StartRunCommand,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> StartedRun:
        self._validate_deadline_args(deadline, safe_clock)
        self._validate_initial_command(command)
        try:
            with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
                self._assert_input_message_belongs(
                    session,
                    command.conversation_id,
                    command.input_message_id,
                )
                existing = session.get(AgentRun, command.run_id)
                if existing is not None:
                    self._assert_run_matches(existing, command)
                    first = self._existing_event(session, command.run_id, command.run_started)
                    second = self._existing_event(session, command.run_id, command.segment_started)
                    if first is None or second is None:
                        raise JournalConflictError("existing run is missing its initial events")
                    return StartedRun(
                        self._detach(session, existing),
                        (self._detach(session, first), self._detach(session, second)),
                    )

                now = self._utc_now()
                run = AgentRun(
                    id=command.run_id,
                    conversation_id=command.conversation_id,
                    input_message_id=command.input_message_id,
                    origin_kind=command.origin_kind,
                    initial_context_type=command.initial_context_type,
                    initial_context_entity_id=command.initial_context_entity_id,
                    initial_context_ref_fingerprint=command.initial_context_ref_fingerprint,
                    fingerprint_key_id=command.fingerprint_key_id,
                    initial_transport_mode=command.initial_transport_mode,
                    initial_route_kind=command.initial_route_kind,
                    status="running",
                    last_seq=0,
                    recording_status="healthy",
                    recording_error_count=0,
                    started_at=now,
                    updated_at=now,
                )
                session.add(run)
                session.flush()
                first = self._insert_event(session, run.id, command.run_started, now)
                second = self._insert_event(session, run.id, command.segment_started, now)
                session.refresh(run)
                return StartedRun(
                    self._detach(session, run),
                    (self._detach(session, first), self._detach(session, second)),
                )
        except IntegrityError:
            replayed = self._replay_created_run(
                command, deadline=deadline, safe_clock=safe_clock
            )
            if replayed is not None:
                return replayed
            raise JournalConflictError(
                "run creation conflicts with persisted journal state"
            ) from None

    def attach_input_message(
        self,
        run_id: str,
        message_id: int,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> AgentRun:
        self._validate_deadline_args(deadline, safe_clock)
        try:
            with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
                run = self._required_run(session, run_id)
                if run.input_message_id == message_id:
                    return self._detach(session, run)
                if run.input_message_id is not None:
                    raise JournalConflictError("run already has a different input message")
                if run.status in _TERMINAL_STATUSES:
                    raise JournalConflictError("terminal run cannot attach an input message")
                message = session.get(ChatMessage, message_id)
                if message is None or message.conversation_id != run.conversation_id:
                    raise JournalConflictError(
                        "input message does not belong to the run conversation"
                    )
                now = self._utc_now()
                result = session.execute(
                    update(AgentRun)
                    .where(AgentRun.id == run_id, AgentRun.input_message_id.is_(None))
                    .values(input_message_id=message_id, updated_at=now)
                )
                if getattr(result, "rowcount", 0) != 1:
                    session.expire(run)
                    if run.input_message_id != message_id:
                        raise JournalConflictError("run already has a different input message")
                else:
                    session.refresh(run)
                return self._detach(session, run)
        except IntegrityError:
            replayed = self._replay_input_attachment(
                run_id,
                message_id,
                deadline=deadline,
                safe_clock=safe_clock,
            )
            if replayed is not None:
                return replayed
            raise JournalConflictError("input message attachment conflicts") from None

    def start_segment(
        self,
        command: StartSegmentCommand,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> AgentEvent:
        self._validate_deadline_args(deadline, safe_clock)
        self._validate_event_draft(command.segment_started)
        if command.segment_started.event_type != "segment.started":
            raise JournalConflictError("segment command requires segment.started")
        try:
            with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
                existing = self._existing_event(session, command.run_id, command.segment_started)
                if existing is not None:
                    return self._detach(session, existing)
                run = self._required_run(session, command.run_id)
                if run.status in _TERMINAL_STATUSES:
                    raise JournalConflictError("terminal run cannot start a segment")
                segment_facts = json.loads(command.segment_started.payload_json)["facts"]
                if segment_facts["request_kind"] not in {"confirmation", "pending_replay"}:
                    raise JournalConflictError("segment request kind requires atomic run creation")
                if run.status != "waiting_confirmation":
                    raise JournalConflictError("new segment requires a waiting run")
                event = self._insert_event(
                    session, command.run_id, command.segment_started, self._utc_now()
                )
                return self._detach(session, event)
        except IntegrityError:
            replayed = self._replay_event(
                command.run_id,
                command.segment_started,
                deadline=deadline,
                safe_clock=safe_clock,
            )
            if replayed is not None:
                return replayed
            raise JournalConflictError(
                "segment start conflicts with persisted journal state"
            ) from None

    def append_event(
        self,
        run_id: str,
        draft: EventDraft,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> AgentEvent:
        self._validate_deadline_args(deadline, safe_clock)
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
                    if started_facts["request_kind"] not in {
                        "confirmation",
                        "pending_replay",
                    }:
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

    def append_event_bound(self, session: Session, run_id: str, draft: EventDraft) -> AgentEvent:
        """Append through a caller-owned transaction without committing it."""

        if draft.event_type in _DISPOSITION_EVENT_TYPES:
            raise JournalConflictError("disposition event requires its atomic repository method")
        existing = self._existing_event(session, run_id, draft)
        if existing is not None:
            return existing
        run = self._required_run(session, run_id)
        if run.status in _TERMINAL_STATUSES:
            raise JournalConflictError("terminal run cannot accept a new event")
        return self._insert_event(session, run_id, draft, self._utc_now())

    def capture_context(
        self,
        run_id: str,
        command: CaptureContextCommand,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> CapturedContext:
        self._validate_deadline_args(deadline, safe_clock)
        self._validate_snapshot_command(command)
        try:
            event_draft = prepare_event(
                event_type="context.captured",
                execution_segment_id=command.execution_segment_id,
                model_step=command.model_step,
                model_call_id=command.model_call_id,
                facts={
                    "snapshot_id": command.snapshot_id,
                    "snapshot_key": command.snapshot_key,
                    "manifest_digest": command.prepared.manifest_digest,
                    "logical_input_fingerprint": command.prepared.logical_input_fingerprint,
                },
                fingerprint_key_id=command.prepared.fingerprint_key_id,
                source_ref_type="context_snapshot",
                source_ref_id=command.snapshot_id,
            )
        except JournalEventValidationError:
            raise JournalConflictError("context event identity is invalid") from None
        try:
            with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
                existing = session.scalar(
                    select(AgentContextSnapshot).where(
                        AgentContextSnapshot.run_id == run_id,
                        AgentContextSnapshot.snapshot_key == command.snapshot_key,
                    )
                )
                if existing is not None:
                    self._assert_snapshot_matches(existing, command)
                    event = self._existing_event(session, run_id, event_draft)
                    if event is None:
                        raise JournalConflictError("snapshot is missing its context event")
                    return CapturedContext(
                        self._detach(session, existing), self._detach(session, event)
                    )
                run = self._required_run(session, run_id)
                if run.status in _TERMINAL_STATUSES:
                    raise JournalConflictError("terminal run cannot capture context")
                if run.fingerprint_key_id != command.prepared.fingerprint_key_id:
                    raise JournalConflictError("snapshot key domain differs from run key domain")
                if command.model_call_id is not None:
                    existing_model_call = session.scalar(
                        select(AgentContextSnapshot).where(
                            AgentContextSnapshot.run_id == run_id,
                            AgentContextSnapshot.model_call_id == command.model_call_id,
                        )
                    )
                    if existing_model_call is not None:
                        raise JournalConflictError("model call already has a context snapshot")
                snapshot = AgentContextSnapshot(
                    id=command.snapshot_id,
                    run_id=run_id,
                    execution_segment_id=command.execution_segment_id,
                    snapshot_key=command.snapshot_key,
                    manifest_schema_version=command.prepared.manifest_schema_version,
                    snapshot_kind=command.snapshot_kind,
                    model_step=command.model_step,
                    model_call_id=command.model_call_id,
                    manifest_json=command.prepared.manifest_json,
                    manifest_digest=command.prepared.manifest_digest,
                    canonicalizer_version="1",
                    logical_input_fingerprint=command.prepared.logical_input_fingerprint,
                    fingerprint_key_id=command.prepared.fingerprint_key_id,
                    estimated_token_count=command.estimated_token_count,
                    token_estimator_name=command.token_estimator_name,
                    token_estimator_version=command.token_estimator_version,
                )
                session.add(snapshot)
                session.flush()
                event = self._insert_event(session, run_id, event_draft, self._utc_now())
                return CapturedContext(
                    self._detach(session, snapshot), self._detach(session, event)
                )
        except IntegrityError:
            replayed = self._replay_captured_context(
                run_id,
                command,
                event_draft,
                deadline=deadline,
                safe_clock=safe_clock,
            )
            if replayed is not None:
                return replayed
            raise JournalConflictError(
                "context capture conflicts with persisted journal state"
            ) from None

    def converge_disposition(
        self,
        run_id: str,
        command: DispositionCommand,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> tuple[AgentEvent, ...]:
        self._validate_deadline_args(deadline, safe_clock)
        self._validate_disposition_shape(run_id, command)
        try:
            with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
                existing: list[AgentEvent | None] = [
                    self._existing_event(session, run_id, draft) for draft in command.events
                ]
                existing_positions = [
                    index for index, event in enumerate(existing) if event is not None
                ]
                if existing_positions != list(range(len(existing_positions))):
                    raise JournalConflictError("existing disposition events are not a prefix")
                self._assert_existing_disposition_order(existing)
                run = self._required_run(session, run_id)
                if all(item is not None for item in existing):
                    self._assert_disposition_projection(run, command)
                    return tuple(self._detach(session, cast(AgentEvent, item)) for item in existing)

                self._assert_status_transition(run, command.target_status)
                self._assert_disposition_matches_run(run, command)
                now = self._utc_now()
                run.status = command.target_status
                run.updated_at = now
                if command.target_status == "waiting_confirmation":
                    run.waiting_tool_call_id = command.waiting_tool_call_id
                    run.failure_code = None
                    run.finished_at = None
                elif command.target_status == "running":
                    run.waiting_tool_call_id = None
                    run.failure_code = None
                    run.finished_at = None
                else:
                    run.waiting_tool_call_id = None
                    run.failure_code = command.failure_code
                    run.finished_at = now
                created: list[AgentEvent] = []
                for item, draft in zip(existing, command.events, strict=True):
                    created.append(item or self._insert_event(session, run_id, draft, now))
                session.flush()
                return tuple(self._detach(session, event) for event in created)
        except IntegrityError:
            replayed = self._replay_disposition(
                run_id,
                command,
                deadline=deadline,
                safe_clock=safe_clock,
            )
            if replayed is not None:
                return replayed
            raise JournalConflictError(
                "disposition conflicts with persisted journal state"
            ) from None

    def mark_degraded(
        self,
        run_id: str,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> AgentRun:
        """Latch persisted recording health without changing the business lifecycle."""

        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            run = self._required_run(session, run_id)
            if run.recording_status == "degraded":
                return self._detach(session, run)
            now = self._utc_now()
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.recording_status == "healthy",
                )
                .values(
                    recording_status="degraded",
                    recording_error_count=AgentRun.recording_error_count + 1,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                session.expire(run)
                if run.recording_status != "degraded":
                    raise JournalConflictError("recording health transition conflicts")
            else:
                session.refresh(run)
            return self._detach(session, run)

    def find_waiting_run(
        self,
        conversation_id: int,
        tool_call_id: str,
        *,
        deadline: float | None = None,
        safe_clock: SafeClockAdapter | None = None,
    ) -> AgentRun | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            run = session.scalar(
                select(AgentRun).where(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.waiting_tool_call_id == tool_call_id,
                    AgentRun.status == "waiting_confirmation",
                )
            )
            return None if run is None else self._detach(session, run)

    def get_run(self, run_id: str) -> AgentRun | None:
        with self.session_factory() as session:
            run = session.get(AgentRun, run_id)
            return None if run is None else self._detach(session, run)

    def list_events(self, run_id: str) -> list[AgentEvent]:
        with self.session_factory() as session:
            events = list(
                session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.seq.asc())
                )
            )
            return [self._detach(session, event) for event in events]

    def list_snapshots(self, run_id: str) -> list[AgentContextSnapshot]:
        with self.session_factory() as session:
            snapshots = list(
                session.scalars(
                    select(AgentContextSnapshot)
                    .where(AgentContextSnapshot.run_id == run_id)
                    .order_by(AgentContextSnapshot.created_at.asc())
                )
            )
            return [self._detach(session, snapshot) for snapshot in snapshots]

    def read_run_journal(self, run_id: str) -> RunJournalRows:
        """Read one causally consistent diagnostic view from a single Session."""

        with self.session_factory() as session, session.begin():
            run = session.get(AgentRun, run_id)
            events = list(
                session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.seq.asc())
                )
            )
            snapshots = list(
                session.scalars(
                    select(AgentContextSnapshot)
                    .where(AgentContextSnapshot.run_id == run_id)
                    .order_by(
                        AgentContextSnapshot.execution_segment_id.asc(),
                        AgentContextSnapshot.model_step.asc(),
                        AgentContextSnapshot.snapshot_key.asc(),
                    )
                )
            )
            return RunJournalRows(
                None if run is None else self._detach(session, run),
                tuple(self._detach(session, event) for event in events),
                tuple(self._detach(session, snapshot) for snapshot in snapshots),
            )

    def count_events(self, run_id: str, dedupe_key: str) -> int:
        with self.session_factory() as session:
            return len(
                list(
                    session.scalars(
                        select(AgentEvent.id).where(
                            AgentEvent.run_id == run_id,
                            AgentEvent.dedupe_key == dedupe_key,
                        )
                    )
                )
            )

    @staticmethod
    def _validate_deadline_args(
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> None:
        if deadline is not None and safe_clock is None:
            raise ValueError("safe_clock is required when deadline is provided")

    @staticmethod
    def _raw_connection(connection: Any) -> Any:
        driver_connection = getattr(connection, "driver_connection", None)
        if driver_connection is not None:
            return driver_connection
        return connection

    @staticmethod
    def _progress_handler(
        safe_clock: SafeClockAdapter,
        deadline: float,
    ) -> Callable[[], int]:
        return _progress_handler(safe_clock, deadline)

    @staticmethod
    def _configure_deadline(
        session: Session,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
        _guard: _SQLiteGuard | None = None,
    ) -> _SQLiteGuard:
        AgentRunRepository._validate_deadline_args(deadline, safe_clock)
        if deadline is None:
            guard = _guard
            if guard is None:
                connection = session.connection()
                raw_connection = AgentRunRepository._raw_connection(connection.connection)
                guard = _SQLiteGuard(
                    connection,
                    raw_connection,
                    False,
                    getattr(connection.connection, "_connection_record", None),
                )
            else:
                connection = guard.connection
            connection.exec_driver_sql(
                f"PRAGMA busy_timeout = {JOURNAL_DEFAULT_BUSY_TIMEOUT_MS}"
            )
            return guard

        assert safe_clock is not None
        now = safe_clock.require_value()
        remaining = deadline - now
        if remaining <= 0:
            raise JournalDeadlineExceeded("deadline")
        remaining_ms = math.floor(remaining * 1000)
        # Pool checkout is Journal work too.  Re-read after checkout so a slow
        # connect cannot hand SQLite a stale 50 ms window.
        guard = _guard
        if guard is None:
            connection = session.connection()
            raw_connection = AgentRunRepository._raw_connection(connection.connection)
            guard = _SQLiteGuard(
                connection,
                raw_connection,
                False,
                getattr(connection.connection, "_connection_record", None),
            )
        connection = guard.connection
        raw_connection = guard.raw_connection
        now = safe_clock.require_value()
        remaining = deadline - now
        if remaining <= 0:
            raise JournalDeadlineExceeded("deadline")
        remaining_ms = math.floor(remaining * 1000)
        busy_timeout_ms = min(JOURNAL_DEFAULT_BUSY_TIMEOUT_MS, max(0, remaining_ms))
        connection.exec_driver_sql(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        guard.progress_installed = True
        raw_connection.set_progress_handler(
            AgentRunRepository._progress_handler(safe_clock, deadline),
            JOURNAL_SQLITE_PROGRESS_STEPS,
        )
        return guard

    @staticmethod
    def _check_deadline(
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> None:
        if deadline is None:
            return
        AgentRunRepository._validate_deadline_args(deadline, safe_clock)
        assert safe_clock is not None
        if safe_clock.require_value() >= deadline:
            raise JournalDeadlineExceeded("deadline")

    @staticmethod
    def _classify_sqlite_exception(
        error: BaseException,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> BaseException:
        if not isinstance(error, OperationalError) or deadline is None or safe_clock is None:
            return error
        try:
            interrupted = "interrupted" in str(error).lower()
        except BaseException:
            return error
        if not interrupted:
            return error
        sample = safe_clock.sample()
        if sample.valid is not True:
            return JournalDeadlineExceeded("clock_invalid")
        if sample.value >= deadline:
            return JournalDeadlineExceeded("deadline")
        return error

    @staticmethod
    def _restore_sqlite_guard(guard: _SQLiteGuard) -> tuple[bool, BaseException | None]:
        failed = False
        cleanup_error: BaseException | None = None
        if guard.progress_installed:
            try:
                guard.raw_connection.set_progress_handler(None, 0)
            except BaseException as error:
                failed = True
                cleanup_error = error
        try:
            cursor = guard.raw_connection.execute(
                f"PRAGMA busy_timeout = {JOURNAL_DEFAULT_BUSY_TIMEOUT_MS}"
            )
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
        except BaseException as error:
            failed = True
            if cleanup_error is None:
                cleanup_error = error
        return failed, cleanup_error

    @staticmethod
    def _invalidate_sqlite_guard(guard: _SQLiteGuard) -> BaseException | None:
        try:
            guard.connection.invalidate()
            return None
        except BaseException as error:
            try:
                # ``invalidate`` itself may fail after a driver error.  Detach
                # before closing so the Pool cannot hand the closed DBAPI handle
                # to the next borrower (the ABA case).
                guard.connection.detach()
            except BaseException:
                pass
            try:
                if guard.connection_record is not None:
                    guard.connection_record.invalidate(error)
            except BaseException:
                try:
                    if guard.connection_record is not None:
                        guard.connection_record.dbapi_connection = None
                except BaseException:
                    pass
            try:
                guard.raw_connection.close()
            except BaseException as close_error:
                return close_error
            return error

    @contextmanager
    def _session_guard(
        self,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> Generator[Session, None, None]:
        """Own one Journal Session and restore every SQLite connection setting."""

        self._validate_deadline_args(deadline, safe_clock)
        session: Session | None = None
        guard: _SQLiteGuard | None = None
        transaction: Any | None = None
        committed = False
        guard_restored = False
        guard_cleanup_failed = False
        rollback_failed = False
        cleanup_failed = False
        primary: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            session = self.session_factory()
            transaction = session.begin()
            try:
                if deadline is not None:
                    safe_clock_required = safe_clock
                    assert safe_clock_required is not None
                    self._check_deadline(deadline, safe_clock_required)
                connection = session.connection()
                guard = _SQLiteGuard(
                    connection,
                    self._raw_connection(connection.connection),
                    False,
                    getattr(connection.connection, "_connection_record", None),
                )
                guard = self._configure_deadline(
                    session,
                    deadline,
                    safe_clock,
                    _guard=guard,
                )
                yield session
                self._check_deadline(deadline, safe_clock)
                transaction.commit()
                committed = True
                self._check_deadline(deadline, safe_clock)
                if guard is not None:
                    cleanup_failed, restore_error = self._restore_sqlite_guard(guard)
                    guard_restored = not cleanup_failed
                    guard_cleanup_failed = cleanup_failed
                    if cleanup_failed and restore_error is None:
                        restore_error = RuntimeError("journal SQLite cleanup failed")
                    if restore_error is not None:
                        raise restore_error
            except BaseException as error:
                primary = self._classify_sqlite_exception(error, deadline, safe_clock)
        except BaseException as error:
            if primary is None:
                primary = self._classify_sqlite_exception(error, deadline, safe_clock)
        finally:
            if transaction is not None and not committed:
                try:
                    assert session is not None
                    session.rollback()
                except BaseException as error:
                    rollback_failed = True
                    if cleanup_error is None:
                        cleanup_error = error

            if guard is not None:
                if not guard_restored:
                    try:
                        cleanup_failed, restore_error = self._restore_sqlite_guard(guard)
                    except BaseException as error:
                        cleanup_failed, restore_error = True, error
                    if restore_error is not None and cleanup_error is None:
                        cleanup_error = restore_error
                if cleanup_failed or guard_cleanup_failed or rollback_failed:
                    try:
                        invalidate_error = self._invalidate_sqlite_guard(guard)
                    except BaseException as error:
                        invalidate_error = error
                    if invalidate_error is not None and cleanup_error is None:
                        cleanup_error = invalidate_error

            if session is not None:
                try:
                    session.close()
                except BaseException as error:
                    cleanup_failed = True
                    if cleanup_error is None:
                        cleanup_error = error
                    if guard is not None:
                        try:
                            invalidate_error = self._invalidate_sqlite_guard(guard)
                        except BaseException as invalidate_exception:
                            invalidate_error = invalidate_exception
                        if invalidate_error is not None and cleanup_error is None:
                            cleanup_error = invalidate_error

            if primary is not None:
                raise primary.with_traceback(primary.__traceback__)
            if cleanup_error is not None:
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)

    @contextmanager
    def _journal_transaction(
        self,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> Generator[Session, None, None]:
        """Compatibility name for the owned-session guard contract."""

        with self._session_guard(deadline=deadline, safe_clock=safe_clock) as session:
            yield session

    @staticmethod
    def _dialect_supports_returning(session: Session) -> bool:
        bind = session.get_bind()
        return bool(getattr(bind.dialect, "update_returning", False))

    def _insert_event(
        self, session: Session, run_id: str, draft: EventDraft, now: datetime
    ) -> AgentEvent:
        if draft.fingerprint_key_id is not None:
            run_key_id = session.scalar(
                select(AgentRun.fingerprint_key_id).where(AgentRun.id == run_id)
            )
            if run_key_id != draft.fingerprint_key_id:
                raise JournalConflictError("event key domain differs from run key domain")
        seq = self._allocate_seq(session, run_id, now)
        event = AgentEvent(
            id=str(uuid4()),
            run_id=run_id,
            seq=seq,
            dedupe_key=draft.dedupe_key,
            event_type=draft.event_type,
            schema_version=draft.schema_version,
            execution_segment_id=draft.execution_segment_id,
            model_step=draft.model_step,
            model_call_id=draft.model_call_id,
            source_ref_type=draft.source_ref_type,
            source_ref_id=draft.source_ref_id,
            fingerprint_key_id=draft.fingerprint_key_id,
            payload_json=draft.payload_json,
            payload_digest=draft.payload_digest,
            fact_digest=draft.fact_digest,
            created_at=now,
        )
        if self._before_event_insert is not None:
            self._before_event_insert(event)
        session.add(event)
        session.flush()
        return event

    def _allocate_seq(self, session: Session, run_id: str, now: datetime) -> int:
        if self._supports_returning(session):
            allocated = session.scalar(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(last_seq=AgentRun.last_seq + 1, updated_at=now)
                .returning(AgentRun.last_seq)
            )
            if allocated is None:
                raise JournalConflictError("agent run does not exist")
            return int(allocated)
        for attempt in range(1, 3):
            expected = session.scalar(select(AgentRun.last_seq).where(AgentRun.id == run_id))
            if expected is None:
                raise JournalConflictError("agent run does not exist")
            allocated = self._cas_increment(session, run_id, int(expected), attempt, now)
            if allocated is not None:
                return allocated
        raise JournalConflictError("sequence allocation conflict")

    def _cas_increment(
        self, session: Session, run_id: str, expected: int, _attempt: int, now: datetime
    ) -> int | None:
        result = session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.last_seq == expected)
            .values(last_seq=expected + 1, updated_at=now)
        )
        return expected + 1 if getattr(result, "rowcount", 0) == 1 else None

    def _existing_event(
        self, session: Session, run_id: str, draft: EventDraft
    ) -> AgentEvent | None:
        event = session.scalar(
            select(AgentEvent).where(
                AgentEvent.run_id == run_id,
                AgentEvent.dedupe_key == draft.dedupe_key,
            )
        )
        if event is None:
            return None
        stable = (
            event.event_type,
            event.schema_version,
            event.execution_segment_id,
            event.model_step,
            event.model_call_id,
            event.source_ref_type,
            event.source_ref_id,
            event.fingerprint_key_id,
            event.fact_digest,
        )
        candidate = (
            draft.event_type,
            draft.schema_version,
            draft.execution_segment_id,
            draft.model_step,
            draft.model_call_id,
            draft.source_ref_type,
            draft.source_ref_id,
            draft.fingerprint_key_id,
            draft.fact_digest,
        )
        if stable != candidate:
            raise JournalConflictError("event dedupe identity has different stable facts")
        return event

    def _replay_created_run(
        self,
        command: StartRunCommand,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> StartedRun | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            run = session.get(AgentRun, command.run_id)
            if run is None:
                return None
            self._assert_run_matches(run, command)
            first = self._existing_event(session, command.run_id, command.run_started)
            second = self._existing_event(session, command.run_id, command.segment_started)
            if first is None or second is None:
                return None
            return StartedRun(
                self._detach(session, run),
                (self._detach(session, first), self._detach(session, second)),
            )

    def _replay_input_attachment(
        self,
        run_id: str,
        message_id: int,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> AgentRun | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            run = session.get(AgentRun, run_id)
            if run is None or run.input_message_id != message_id:
                return None
            return self._detach(session, run)

    def _replay_event(
        self,
        run_id: str,
        draft: EventDraft,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> AgentEvent | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            event = self._existing_event(session, run_id, draft)
            return None if event is None else self._detach(session, event)

    def _replay_captured_context(
        self,
        run_id: str,
        command: CaptureContextCommand,
        event_draft: EventDraft,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> CapturedContext | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            snapshot = session.scalar(
                select(AgentContextSnapshot).where(
                    AgentContextSnapshot.run_id == run_id,
                    AgentContextSnapshot.snapshot_key == command.snapshot_key,
                )
            )
            if snapshot is None:
                return None
            self._assert_snapshot_matches(snapshot, command)
            event = self._existing_event(session, run_id, event_draft)
            if event is None:
                return None
            return CapturedContext(
                self._detach(session, snapshot),
                self._detach(session, event),
            )

    def _replay_disposition(
        self,
        run_id: str,
        command: DispositionCommand,
        *,
        deadline: float | None,
        safe_clock: SafeClockAdapter | None,
    ) -> tuple[AgentEvent, ...] | None:
        self._validate_deadline_args(deadline, safe_clock)
        with self._journal_transaction(deadline=deadline, safe_clock=safe_clock) as session:
            events = [self._existing_event(session, run_id, draft) for draft in command.events]
            if any(event is None for event in events):
                return None
            self._assert_existing_disposition_order(events)
            run = session.get(AgentRun, run_id)
            if run is None:
                return None
            self._assert_disposition_projection(run, command)
            return tuple(self._detach(session, cast(AgentEvent, event)) for event in events)

    @staticmethod
    def _required_run(session: Session, run_id: str) -> AgentRun:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise JournalConflictError("agent run does not exist")
        return run

    def _validate_initial_command(self, command: StartRunCommand) -> None:
        self._validate_event_draft(command.run_started)
        self._validate_event_draft(command.segment_started)
        if command.initial_route_kind not in {"model", "deterministic", "unknown"}:
            raise JournalConflictError("unsupported initial route kind")
        if command.initial_context_type in {"workspace", "global"}:
            if (
                command.initial_context_entity_id is not None
                or command.initial_context_ref_fingerprint is not None
            ):
                raise JournalConflictError("context identity is not normalized")
        elif command.initial_context_type == "application":
            entity_id = command.initial_context_entity_id
            if entity_id is not None and (
                not entity_id.isascii() or not entity_id.isdigit() or entity_id.startswith("0")
            ):
                raise JournalConflictError("application context identity is invalid")
            if command.initial_context_ref_fingerprint is not None:
                raise JournalConflictError("application context cannot retain raw reference")
        elif command.initial_context_type in {"mode", "unknown"}:
            if command.initial_context_entity_id is not None or (
                command.initial_context_ref_fingerprint is None
                or _HEX_DIGEST.fullmatch(command.initial_context_ref_fingerprint) is None
            ):
                raise JournalConflictError("private context identity is not fingerprinted")
        else:
            raise JournalConflictError("unsupported initial context type")
        if command.run_started.event_type != "run.started":
            raise JournalConflictError("initial command requires run.started")
        if command.segment_started.event_type != "segment.started":
            raise JournalConflictError("initial command requires segment.started")
        run_facts = json.loads(command.run_started.payload_json)["facts"]
        if run_facts != {
            "agent_run_id": command.run_id,
            "context_type": command.initial_context_type,
            "conversation_id": command.conversation_id,
            "origin_kind": command.origin_kind,
            "transport_mode": command.initial_transport_mode,
        }:
            raise JournalConflictError("run.started facts differ from command")
        if command.run_started.execution_segment_id != command.segment_started.execution_segment_id:
            raise JournalConflictError("initial events use different segments")
        segment_facts = json.loads(command.segment_started.payload_json)["facts"]
        if segment_facts["request_kind"] != "initial":
            raise JournalConflictError("initial segment has a different request kind")
        if segment_facts["transport_mode"] != command.initial_transport_mode:
            raise JournalConflictError("initial segment transport differs from command")

    @staticmethod
    def _validate_event_draft(draft: EventDraft) -> None:
        try:
            validate_event_draft(draft)
        except JournalEventValidationError:
            raise JournalConflictError("event draft is not canonical") from None

    @staticmethod
    def _assert_input_message_belongs(
        session: Session,
        conversation_id: int,
        message_id: int | None,
    ) -> None:
        if message_id is None:
            return
        message = session.get(ChatMessage, message_id)
        if message is None or message.conversation_id != conversation_id:
            raise JournalConflictError("input message does not belong to the run conversation")

    @staticmethod
    def _validate_snapshot_command(command: CaptureContextCommand) -> None:
        prepared = command.prepared
        try:
            manifest = validate_context_manifest_json(prepared.manifest_json)
        except JournalEventValidationError:
            raise JournalConflictError("context manifest is not canonical") from None
        if prepared.manifest_schema_version not in {1, 2} or manifest.get(
            "manifest_schema_version"
        ) != prepared.manifest_schema_version:
            raise JournalConflictError("unsupported context manifest schema")
        expected_digest = hashlib.sha256(prepared.manifest_json.encode("utf-8")).hexdigest()
        if prepared.manifest_digest != expected_digest:
            raise JournalConflictError("context manifest digest differs")
        if _HEX_DIGEST.fullmatch(prepared.logical_input_fingerprint) is None:
            raise JournalConflictError("logical input fingerprint is invalid")
        if command.snapshot_kind == "model_input":
            if command.model_call_id is None or command.model_step is None:
                raise JournalConflictError("model input snapshot requires model identity")
            expected_key = f"model-input:{command.execution_segment_id}:{command.model_call_id}"
        elif command.snapshot_kind == "initial":
            if command.model_call_id is not None or command.model_step is not None:
                raise JournalConflictError("initial snapshot cannot use model identity")
            expected_key = f"initial:{command.execution_segment_id}"
        elif command.snapshot_kind == "confirmation_resume":
            if command.model_call_id is not None or command.model_step is not None:
                raise JournalConflictError("confirmation snapshot cannot use model identity")
            expected_key = f"confirmation-resume:{command.execution_segment_id}"
        else:
            raise JournalConflictError("unsupported context snapshot kind")
        if command.snapshot_key != expected_key:
            raise JournalConflictError("context snapshot key differs from identity")
        if command.estimated_token_count is not None and (
            type(command.estimated_token_count) is not int or command.estimated_token_count < 0
        ):
            raise JournalConflictError("estimated token count is invalid")
        if (command.token_estimator_name is None) != (command.token_estimator_version is None):
            raise JournalConflictError("token estimator identity must be paired")

    @staticmethod
    def _assert_run_matches(run: AgentRun, command: StartRunCommand) -> None:
        actual = (
            run.conversation_id,
            run.input_message_id,
            run.origin_kind,
            run.initial_context_type,
            run.initial_context_entity_id,
            run.initial_context_ref_fingerprint,
            run.fingerprint_key_id,
            run.initial_transport_mode,
            run.initial_route_kind,
        )
        expected = (
            command.conversation_id,
            command.input_message_id,
            command.origin_kind,
            command.initial_context_type,
            command.initial_context_entity_id,
            command.initial_context_ref_fingerprint,
            command.fingerprint_key_id,
            command.initial_transport_mode,
            command.initial_route_kind,
        )
        if actual != expected:
            raise JournalConflictError("run identity has different stable facts")

    @staticmethod
    def _assert_snapshot_matches(
        snapshot: AgentContextSnapshot, command: CaptureContextCommand
    ) -> None:
        actual = (
            snapshot.id,
            snapshot.execution_segment_id,
            snapshot.snapshot_kind,
            snapshot.model_step,
            snapshot.model_call_id,
            snapshot.manifest_schema_version,
            snapshot.manifest_json,
            snapshot.manifest_digest,
            snapshot.canonicalizer_version,
            snapshot.logical_input_fingerprint,
            snapshot.fingerprint_key_id,
            snapshot.estimated_token_count,
            snapshot.token_estimator_name,
            snapshot.token_estimator_version,
        )
        expected = (
            command.snapshot_id,
            command.execution_segment_id,
            command.snapshot_kind,
            command.model_step,
            command.model_call_id,
            command.prepared.manifest_schema_version,
            command.prepared.manifest_json,
            command.prepared.manifest_digest,
            "1",
            command.prepared.logical_input_fingerprint,
            command.prepared.fingerprint_key_id,
            command.estimated_token_count,
            command.token_estimator_name,
            command.token_estimator_version,
        )
        if actual != expected:
            raise JournalConflictError("snapshot key has different stable facts")

    @staticmethod
    def _validate_disposition_shape(run_id: str, command: DispositionCommand) -> None:
        types = [draft.event_type for draft in command.events]
        for draft in command.events:
            AgentRunRepository._validate_event_draft(draft)
        if len(types) != len(set(draft.dedupe_key for draft in command.events)):
            raise JournalConflictError("disposition contains duplicate event identities")
        if len({draft.execution_segment_id for draft in command.events}) != 1:
            raise JournalConflictError("disposition events must use one segment")
        facts_by_type = {
            draft.event_type: json.loads(draft.payload_json)["facts"] for draft in command.events
        }
        if command.target_status == "waiting_confirmation":
            if not command.waiting_tool_call_id:
                raise JournalConflictError("waiting disposition requires tool call identity")
            required = {
                "tool.proposed",
                "approval.requested",
                "run.waiting_confirmation",
                "segment.finished",
            }
            expected_order = [
                "tool.proposed",
                "approval.requested",
                "run.waiting_confirmation",
                "segment.finished",
            ]
            if types != expected_order:
                raise JournalConflictError("waiting disposition is missing required events")
            for event_type in required - {"segment.finished"}:
                if facts_by_type[event_type]["tool_call_id"] != command.waiting_tool_call_id:
                    raise JournalConflictError("waiting disposition tool identity differs")
            if facts_by_type["segment.finished"] != {
                "outcome": "suspended",
                "terminal_run_status": None,
            }:
                raise JournalConflictError("waiting disposition segment outcome differs")
        elif command.target_status == "running":
            if command.waiting_tool_call_id is not None or types != ["run.resumed"]:
                raise JournalConflictError("resume disposition is invalid")
        else:
            if command.waiting_tool_call_id is not None:
                raise JournalConflictError("terminal disposition cannot retain waiting identity")
            terminal_type = f"run.{command.target_status}"
            if types != [terminal_type, "segment.finished"]:
                raise JournalConflictError("terminal disposition is missing required events")
            if facts_by_type[terminal_type] != {
                "agent_run_id": run_id,
                "failure_code": command.failure_code,
                "status": command.target_status,
            }:
                raise JournalConflictError("terminal disposition run facts differ")
            if facts_by_type["segment.finished"] != {
                "outcome": command.target_status,
                "terminal_run_status": command.target_status,
            }:
                raise JournalConflictError("terminal disposition segment outcome differs")

    @staticmethod
    def _assert_disposition_matches_run(run: AgentRun, command: DispositionCommand) -> None:
        if command.target_status != "running":
            return
        resumed = next(draft for draft in command.events if draft.event_type == "run.resumed")
        facts = json.loads(resumed.payload_json)["facts"]
        if facts["tool_call_id"] != run.waiting_tool_call_id:
            raise JournalConflictError("resume disposition tool identity differs")

    @staticmethod
    def _assert_existing_disposition_order(events: list[AgentEvent | None]) -> None:
        sequences = [event.seq for event in events if event is not None]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise JournalConflictError("existing disposition event order differs")

    @staticmethod
    def _assert_status_transition(run: AgentRun, target: RunStatus) -> None:
        if run.status in _TERMINAL_STATUSES:
            raise JournalConflictError("terminal run is immutable")
        allowed: dict[str, set[str]] = {
            "running": {"waiting_confirmation", *_TERMINAL_STATUSES},
            "waiting_confirmation": {"running", "failed", "cancelled", "timed_out"},
        }
        if run.recording_status == "degraded" and run.status == "waiting_confirmation":
            allowed["waiting_confirmation"].add("completed")
        if target not in allowed.get(run.status, set()):
            raise JournalConflictError("invalid run status transition")

    @staticmethod
    def _assert_disposition_projection(run: AgentRun, command: DispositionCommand) -> None:
        if run.status != command.target_status:
            raise JournalConflictError("events exist but run projection differs")
        expected_waiting = (
            command.waiting_tool_call_id
            if command.target_status == "waiting_confirmation"
            else None
        )
        if run.waiting_tool_call_id != expected_waiting:
            raise JournalConflictError("events exist but waiting projection differs")
        expected_failure = (
            command.failure_code if command.target_status in _TERMINAL_STATUSES else None
        )
        if run.failure_code != expected_failure:
            raise JournalConflictError("events exist but failure projection differs")
        if (run.finished_at is not None) != (command.target_status in _TERMINAL_STATUSES):
            raise JournalConflictError("events exist but finish projection differs")

    def _utc_now(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("journal clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _detach(session: Session, value: _ModelT) -> _ModelT:
        session.flush()
        session.expunge(value)
        return value
