from __future__ import annotations

import hashlib
import inspect
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session
from sqlalchemy.orm.session import SessionTransaction

from offerpilot.agent_runtime.events import (
    EventDraft,
    PreparedSnapshot,
    canonical_json,
    prepare_event,
)
from offerpilot.agent_runtime.budget import (
    JOURNAL_DEFAULT_BUSY_TIMEOUT_MS,
    JournalDeadlineExceeded as BudgetJournalDeadlineExceeded,
    MonotonicSample,
    SafeClockAdapter,
)
from offerpilot.db import init_database, journal_session_factory_for_data_dir
from offerpilot.models import AgentContextSnapshot, AgentEvent, ChatMessage, Conversation
from offerpilot.repositories.agent_runs import (
    AgentRunRepository,
    CaptureContextCommand,
    DispositionCommand,
    JournalConflictError,
    JournalDeadlineExceeded,
    StartRunCommand,
    StartSegmentCommand,
    _SQLiteGuard,
    _progress_handler,
)


KEY_ID = "11111111-1111-4111-8111-111111111111"
OTHER_KEY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_ID = "22222222-2222-4222-8222-222222222222"
SEGMENT_ID = "33333333-3333-4333-8333-333333333333"
SNAPSHOT_ID = "44444444-4444-4444-8444-444444444444"
MODEL_CALL_ID = "55555555-5555-4555-8555-555555555555"
MANIFEST_JSON = canonical_json(
    {
        "manifest_schema_version": 1,
        "conversation": {
            "message_count": 0,
            "first_message_id": None,
            "last_message_id": None,
            "ordered_ids_digest": "sha256:" + "0" * 64,
            "included_recent_message_ids": [],
        },
        "tools": {
            "count": 0,
            "ordered_names_digest": "sha256:" + "0" * 64,
            "included_names": [],
        },
        "attachments": {
            "count": 0,
            "ordered_refs_digest": "sha256:" + "0" * 64,
            "included_refs": [],
        },
        "domain_sources": {
            "count": 0,
            "ordered_refs_digest": "sha256:" + "0" * 64,
            "included_refs": [],
        },
    }
)


class SyntheticJournalFailure(RuntimeError):
    pass


def _seed_conversation(tmp_path: Path) -> tuple[int, int]:
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        conversation = Conversation(title="journal repository")
        session.add(conversation)
        session.flush()
        message = ChatMessage(conversation_id=conversation.id, role="user", content="private")
        session.add(message)
        session.commit()
        return conversation.id, message.id


def _run_started(conversation_id: int) -> EventDraft:
    return prepare_event(
        event_type="run.started",
        execution_segment_id=SEGMENT_ID,
        facts={
            "agent_run_id": RUN_ID,
            "origin_kind": "user_message",
            "conversation_id": conversation_id,
            "context_type": "workspace",
            "transport_mode": "sync",
        },
    )


def _segment_started(
    segment_id: str = SEGMENT_ID,
    *,
    request_kind: str = "initial",
) -> EventDraft:
    execution_path = {
        "initial": "model_turn",
        "confirmation": "agent_resume",
        "pending_replay": "deterministic_action",
    }[request_kind]
    return prepare_event(
        event_type="segment.started",
        execution_segment_id=segment_id,
        facts={
            "request_kind": request_kind,
            "transport_mode": "sync",
            "execution_path": execution_path,
            "transport_run_id": None,
        },
    )


def _start_command(conversation_id: int, message_id: int | None = None) -> StartRunCommand:
    return StartRunCommand(
        run_id=RUN_ID,
        conversation_id=conversation_id,
        input_message_id=message_id,
        origin_kind="user_message",
        initial_context_type="workspace",
        initial_context_entity_id=None,
        initial_context_ref_fingerprint=None,
        fingerprint_key_id=KEY_ID,
        initial_transport_mode="sync",
        initial_route_kind="model",
        run_started=_run_started(conversation_id),
        segment_started=_segment_started(),
    )


def _repository(tmp_path: Path, **kwargs: object) -> AgentRunRepository:
    return AgentRunRepository(journal_session_factory_for_data_dir(tmp_path), **kwargs)


def _create_run(tmp_path: Path) -> tuple[AgentRunRepository, int, int]:
    conversation_id, message_id = _seed_conversation(tmp_path)
    repository = _repository(tmp_path)
    repository.create_run_and_initial_segment(_start_command(conversation_id))
    return repository, conversation_id, message_id


def _assistant_event(message_id: int, *, duration_ms: int = 10) -> EventDraft:
    return prepare_event(
        event_type="assistant.persisted",
        execution_segment_id=SEGMENT_ID,
        facts={"message_id": message_id, "message_kind": "assistant"},
        telemetry={"duration_ms": duration_ms},
        source_ref_type="message",
        source_ref_id=message_id,
    )


def _snapshot_command(
    *,
    snapshot_id: str = SNAPSHOT_ID,
    segment_id: str = SEGMENT_ID,
    model_call_id: str = MODEL_CALL_ID,
) -> CaptureContextCommand:
    prepared = PreparedSnapshot(
        manifest_schema_version=1,
        manifest_json=MANIFEST_JSON,
        manifest_digest=hashlib.sha256(MANIFEST_JSON.encode("utf-8")).hexdigest(),
        logical_input_fingerprint="b" * 64,
        fingerprint_key_id=KEY_ID,
    )
    return CaptureContextCommand(
        snapshot_id=snapshot_id,
        execution_segment_id=segment_id,
        snapshot_key=f"model-input:{segment_id}:{model_call_id}",
        snapshot_kind="model_input",
        model_step=1,
        model_call_id=model_call_id,
        prepared=prepared,
        estimated_token_count=12,
        token_estimator_name="chars",
        token_estimator_version="1",
    )


def _waiting_events(
    tool_call_id: str,
    segment_id: str = SEGMENT_ID,
) -> tuple[EventDraft, ...]:
    proposed = prepare_event(
        event_type="tool.proposed",
        execution_segment_id=segment_id,
        facts={
            "tool_call_id": tool_call_id,
            "tool_name": "create_application",
            "tool_kind": "write",
            "args_shape_digest": "sha256:" + "c" * 64,
            "proposal_outcome": "confirmation_required",
        },
        source_ref_type="tool_call",
        source_ref_id=tool_call_id,
    )
    requested = prepare_event(
        event_type="approval.requested",
        execution_segment_id=segment_id,
        facts={
            "tool_call_id": tool_call_id,
            "confirmation_mode": "required",
            "pending_identity_fingerprint": "d" * 64,
        },
        source_ref_type="tool_call",
        source_ref_id=tool_call_id,
        fingerprint_key_id=KEY_ID,
    )
    waiting = prepare_event(
        event_type="run.waiting_confirmation",
        execution_segment_id=segment_id,
        facts={"tool_call_id": tool_call_id},
        source_ref_type="tool_call",
        source_ref_id=tool_call_id,
    )
    finished = prepare_event(
        event_type="segment.finished",
        execution_segment_id=segment_id,
        facts={"outcome": "suspended", "terminal_run_status": None},
    )
    return proposed, requested, waiting, finished


def _terminal_events(
    status: str, segment_id: str = SEGMENT_ID
) -> tuple[EventDraft, EventDraft]:
    terminal = prepare_event(
        event_type=f"run.{status}",
        execution_segment_id=segment_id,
        facts={"agent_run_id": RUN_ID, "status": status, "failure_code": None},
    )
    finished = prepare_event(
        event_type="segment.finished",
        execution_segment_id=segment_id,
        facts={"outcome": status, "terminal_run_status": status},
    )
    return terminal, finished


def _resumed_event(
    tool_call_id: str,
    segment_id: str,
    confirmation_attempt_id: str,
) -> EventDraft:
    return prepare_event(
        event_type="run.resumed",
        execution_segment_id=segment_id,
        facts={
            "confirmation_attempt_id": confirmation_attempt_id,
            "tool_call_id": tool_call_id,
        },
    )


def test_create_run_atomically_creates_initial_events(tmp_path: Path) -> None:
    conversation_id, message_id = _seed_conversation(tmp_path)
    repository = _repository(tmp_path)

    created = repository.create_run_and_initial_segment(
        _start_command(conversation_id, message_id)
    )

    assert created.run.last_seq == 2
    assert created.run.input_message_id == message_id
    assert [(event.seq, event.event_type) for event in created.events] == [
        (1, "run.started"),
        (2, "segment.started"),
    ]


def test_create_run_is_idempotent_and_conflicts_on_changed_facts(tmp_path: Path) -> None:
    conversation_id, _ = _seed_conversation(tmp_path)
    repository = _repository(tmp_path)
    command = _start_command(conversation_id)

    first = repository.create_run_and_initial_segment(command)
    second = repository.create_run_and_initial_segment(command)

    assert second.run.id == first.run.id
    assert [event.id for event in second.events] == [event.id for event in first.events]
    changed = StartRunCommand(**{**command.__dict__, "initial_route_kind": "deterministic"})
    with pytest.raises(JournalConflictError):
        repository.create_run_and_initial_segment(changed)


def test_create_run_rejects_mismatched_initial_event_facts(tmp_path: Path) -> None:
    conversation_id, _ = _seed_conversation(tmp_path)
    repository = _repository(tmp_path)
    command = _start_command(conversation_id)
    mismatched = StartRunCommand(
        **{
            **command.__dict__,
            "run_started": prepare_event(
                event_type="run.started",
                execution_segment_id=SEGMENT_ID,
                facts={
                    "agent_run_id": RUN_ID,
                    "origin_kind": "user_message",
                    "conversation_id": conversation_id + 1,
                    "context_type": "workspace",
                    "transport_mode": "sync",
                },
            ),
        }
    )

    with pytest.raises(JournalConflictError, match="facts differ"):
        repository.create_run_and_initial_segment(mismatched)

    assert repository.get_run(RUN_ID) is None


def test_create_run_input_message_must_belong_to_conversation(tmp_path: Path) -> None:
    conversation_id, _ = _seed_conversation(tmp_path)
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        another = Conversation(title="other run conversation")
        session.add(another)
        session.flush()
        foreign = ChatMessage(conversation_id=another.id, role="user", content="private")
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id

    repository = _repository(tmp_path)
    with pytest.raises(JournalConflictError, match="input message"):
        repository.create_run_and_initial_segment(_start_command(conversation_id, foreign_id))

    assert repository.get_run(RUN_ID) is None


def test_attach_input_message_is_set_once_and_must_belong_to_conversation(tmp_path: Path) -> None:
    repository, conversation_id, message_id = _create_run(tmp_path)

    attached = repository.attach_input_message(RUN_ID, message_id)
    assert attached.input_message_id == message_id
    assert repository.attach_input_message(RUN_ID, message_id).input_message_id == message_id

    main_factory = init_database(tmp_path / "data.db")
    with main_factory() as session:
        another = Conversation(title="other")
        session.add(another)
        session.flush()
        foreign = ChatMessage(conversation_id=another.id, role="user", content="private")
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id
    with pytest.raises(JournalConflictError):
        repository.attach_input_message(RUN_ID, foreign_id)
    assert repository.find_waiting_run(conversation_id, "missing") is None


def test_terminal_run_rejects_late_input_message_attachment(tmp_path: Path) -> None:
    repository, _, message_id = _create_run(tmp_path)
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(target_status="completed", events=_terminal_events("completed")),
    )

    with pytest.raises(JournalConflictError, match="terminal"):
        repository.attach_input_message(RUN_ID, message_id)

    run = repository.get_run(RUN_ID)
    assert run is not None and run.input_message_id is None


def test_capture_context_is_atomic_and_idempotent(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    command = _snapshot_command()

    first = repository.capture_context(RUN_ID, command)
    second = repository.capture_context(RUN_ID, command)

    assert second.snapshot.id == first.snapshot.id
    assert second.event.id == first.event.id
    assert repository.get_run(RUN_ID).last_seq == 3  # type: ignore[union-attr]


def test_capture_context_rolls_back_snapshot_when_event_insert_fails(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)

    def fail(_event: AgentEvent) -> None:
        raise SyntheticJournalFailure

    failing = _repository(tmp_path, before_event_insert=fail)
    with pytest.raises(SyntheticJournalFailure):
        failing.capture_context(RUN_ID, _snapshot_command())

    assert repository.list_snapshots(RUN_ID) == []
    assert repository.get_run(RUN_ID).last_seq == 2  # type: ignore[union-attr]


def test_capture_context_rejects_non_manifest_fields(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    command = _snapshot_command()
    private_manifest = canonical_json(
        {"manifest_schema_version": 1, "content": "must-not-enter-journal"}
    )
    invalid = CaptureContextCommand(
        **{
            **command.__dict__,
            "prepared": PreparedSnapshot(
                manifest_schema_version=1,
                manifest_json=private_manifest,
                manifest_digest=hashlib.sha256(private_manifest.encode("utf-8")).hexdigest(),
                logical_input_fingerprint="b" * 64,
                fingerprint_key_id=KEY_ID,
            ),
        }
    )

    with pytest.raises(JournalConflictError, match="manifest"):
        repository.capture_context(RUN_ID, invalid)

    assert repository.list_snapshots(RUN_ID) == []


def test_same_dedupe_and_same_facts_ignores_telemetry_but_changed_facts_conflict(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_run(tmp_path)
    first = repository.append_event(RUN_ID, _assistant_event(91, duration_ms=10))
    second = repository.append_event(RUN_ID, _assistant_event(91, duration_ms=90))

    assert second.id == first.id
    assert repository.count_events(RUN_ID, first.dedupe_key) == 1
    with pytest.raises(JournalConflictError):
        repository.append_event(
            RUN_ID,
            prepare_event(
                event_type="assistant.persisted",
                execution_segment_id=SEGMENT_ID,
                facts={"message_id": 91, "message_kind": "tool"},
                source_ref_type="message",
                source_ref_id=91,
            ),
        )


def test_event_fingerprint_key_domain_must_match_run(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    mismatched = prepare_event(
        event_type="approval.requested",
        execution_segment_id=SEGMENT_ID,
        facts={
            "tool_call_id": "call-mismatched-key",
            "confirmation_mode": "required",
            "pending_identity_fingerprint": "e" * 64,
        },
        source_ref_type="tool_call",
        source_ref_id="call-mismatched-key",
        fingerprint_key_id=OTHER_KEY_ID,
    )

    with pytest.raises(JournalConflictError, match="key domain"):
        repository.append_event(RUN_ID, mismatched)

    assert repository.get_run(RUN_ID).last_seq == 2  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "draft",
    [
        _terminal_events("completed")[0],
        _terminal_events("completed")[1],
    ],
)
def test_append_event_rejects_disposition_events(tmp_path: Path, draft: EventDraft) -> None:
    repository, _, _ = _create_run(tmp_path)

    with pytest.raises(JournalConflictError, match="disposition"):
        repository.append_event(RUN_ID, draft)

    run = repository.get_run(RUN_ID)
    assert run is not None and run.status == "running" and run.last_seq == 2


def test_concurrent_identical_event_append_returns_one_persisted_event(tmp_path: Path) -> None:
    _create_run(tmp_path)
    draft = _assistant_event(92)
    barrier = threading.Barrier(2)

    class RacingRepository(AgentRunRepository):
        def __init__(self) -> None:
            super().__init__(journal_session_factory_for_data_dir(tmp_path))
            self._waited = False

        def _existing_event(
            self,
            session: Session,
            run_id: str,
            candidate: EventDraft,
        ) -> AgentEvent | None:
            existing = super()._existing_event(session, run_id, candidate)
            if existing is None and candidate.dedupe_key == draft.dedupe_key and not self._waited:
                self._waited = True
                barrier.wait(timeout=2)
            return existing

    repositories = [RacingRepository(), RacingRepository()]
    results: list[AgentEvent] = []
    errors: list[BaseException] = []

    def append(repository: AgentRunRepository) -> None:
        try:
            results.append(repository.append_event(RUN_ID, draft))
        except BaseException as exc:  # test thread must report every failure
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(repository,)) for repository in repositories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0].id == results[1].id
    assert repositories[0].count_events(RUN_ID, draft.dedupe_key) == 1


def test_concurrent_seq_allocation_has_no_gaps_or_duplicates(tmp_path: Path) -> None:
    _create_run(tmp_path)
    repositories = [_repository(tmp_path), _repository(tmp_path)]
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def append_batch(index: int) -> None:
        try:
            barrier.wait()
            for offset in range(5):
                repositories[index].append_event(
                    RUN_ID, _assistant_event(100 + index * 10 + offset)
                )
        except BaseException as exc:  # test thread must report every failure
            errors.append(exc)

    threads = [threading.Thread(target=append_batch, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [event.seq for event in repositories[0].list_events(RUN_ID)] == list(range(1, 13))


def test_cas_fallback_stops_after_two_failed_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, _ = _create_run(tmp_path)
    fallback = _repository(tmp_path, supports_returning=lambda _session: False)
    attempts: list[int] = []

    def lose_cas(
        _session: object,
        _run_id: str,
        _expected: int,
        attempt: int,
        _now: datetime,
    ) -> int | None:
        attempts.append(attempt)
        return None

    monkeypatch.setattr(fallback, "_cas_increment", lose_cas)
    with pytest.raises(JournalConflictError, match="sequence allocation"):
        fallback.append_event(RUN_ID, _assistant_event(201))

    assert attempts == [1, 2]
    assert repository.get_run(RUN_ID).last_seq == 2  # type: ignore[union-attr]
    assert repository.count_events(RUN_ID, "assistant.persisted:201") == 0


def test_suspended_and_terminal_dispositions_are_atomic_and_terminal_is_immutable(
    tmp_path: Path,
) -> None:
    repository, conversation_id, _ = _create_run(tmp_path)
    tool_call_id = "call-1"
    waiting_events = _waiting_events(tool_call_id)

    created = repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=waiting_events,
            waiting_tool_call_id=tool_call_id,
        ),
    )

    assert len(created) == 4
    waiting = repository.find_waiting_run(conversation_id, tool_call_id)
    assert waiting is not None and waiting.status == "waiting_confirmation"

    confirmation_segment = "99999999-9999-4999-8999-999999999999"
    repository.start_segment(
        StartSegmentCommand(
            run_id=RUN_ID,
            segment_started=_segment_started(
                confirmation_segment,
                request_kind="confirmation",
            ),
        )
    )
    confirmation_attempt_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
    resumed = repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="running",
            events=(
                _resumed_event(tool_call_id, confirmation_segment, confirmation_attempt_id),
            ),
        ),
    )
    assert [event.event_type for event in resumed] == ["run.resumed"]
    assert repository.find_waiting_run(conversation_id, tool_call_id) is None
    terminal = repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="failed",
            events=_terminal_events("failed", confirmation_segment),
        ),
    )
    assert [event.event_type for event in terminal] == ["run.failed", "segment.finished"]
    run = repository.get_run(RUN_ID)
    assert run is not None and run.status == "failed" and run.waiting_tool_call_id is None
    assert run.finished_at is not None
    with pytest.raises(JournalConflictError):
        repository.converge_disposition(
            RUN_ID,
            DispositionCommand(target_status="cancelled", events=_terminal_events("cancelled")),
        )


def test_waiting_disposition_converges_when_some_events_already_exist(tmp_path: Path) -> None:
    repository, conversation_id, _ = _create_run(tmp_path)
    tool_call_id = "call-partial"
    events = _waiting_events(tool_call_id)
    proposed = repository.append_event(RUN_ID, events[0])
    requested = repository.append_event(RUN_ID, events[1])

    converged = repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=events,
            waiting_tool_call_id=tool_call_id,
        ),
    )

    assert [event.id for event in converged[:2]] == [proposed.id, requested.id]
    assert [event.seq for event in converged] == [3, 4, 5, 6]
    assert repository.find_waiting_run(conversation_id, tool_call_id) is not None


def test_waiting_disposition_requires_complete_minimum_event_set(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    events = _waiting_events("call-incomplete")

    with pytest.raises(JournalConflictError, match="missing required events"):
        repository.converge_disposition(
            RUN_ID,
            DispositionCommand(
                target_status="waiting_confirmation",
                events=events[2:],
                waiting_tool_call_id="call-incomplete",
            ),
        )

    run = repository.get_run(RUN_ID)
    assert run is not None and run.status == "running" and run.last_seq == 2


def test_waiting_disposition_rejects_existing_nonprefix_event(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    events = _waiting_events("call-out-of-order")
    repository.append_event(RUN_ID, events[1])

    with pytest.raises(JournalConflictError, match="prefix"):
        repository.converge_disposition(
            RUN_ID,
            DispositionCommand(
                target_status="waiting_confirmation",
                events=events,
                waiting_tool_call_id="call-out-of-order",
            ),
        )

    run = repository.get_run(RUN_ID)
    assert run is not None and run.status == "running" and run.last_seq == 3


def test_waiting_disposition_rejects_existing_events_in_reverse_order(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    events = _waiting_events("call-reversed")
    repository.append_event(RUN_ID, events[1])
    repository.append_event(RUN_ID, events[0])

    with pytest.raises(JournalConflictError, match="order differs"):
        repository.converge_disposition(
            RUN_ID,
            DispositionCommand(
                target_status="waiting_confirmation",
                events=events,
                waiting_tool_call_id="call-reversed",
            ),
        )

    run = repository.get_run(RUN_ID)
    assert run is not None and run.status == "running" and run.last_seq == 4


def test_disposition_replay_queries_existing_events_before_terminal_transition_check(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_run(tmp_path)
    command = DispositionCommand(target_status="completed", events=_terminal_events("completed"))
    first = repository.converge_disposition(RUN_ID, command)
    second = repository.converge_disposition(RUN_ID, command)
    assert [event.id for event in second] == [event.id for event in first]


def test_waiting_tool_call_partial_unique_index_is_enforced(tmp_path: Path) -> None:
    repository, conversation_id, _ = _create_run(tmp_path)
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=_waiting_events("same-call"),
            waiting_tool_call_id="same-call",
        ),
    )
    second_run = str(uuid4())
    second_segment = str(uuid4())
    second = StartRunCommand(
        **{
            **_start_command(conversation_id).__dict__,
            "run_id": second_run,
            "run_started": prepare_event(
                event_type="run.started",
                execution_segment_id=second_segment,
                facts={
                    "agent_run_id": second_run,
                    "origin_kind": "user_message",
                    "conversation_id": conversation_id,
                    "context_type": "workspace",
                    "transport_mode": "sync",
                },
            ),
            "segment_started": _segment_started(second_segment),
        }
    )
    repository.create_run_and_initial_segment(second)
    with pytest.raises(JournalConflictError):
        repository.converge_disposition(
            second_run,
            DispositionCommand(
                target_status="waiting_confirmation",
                events=_waiting_events("same-call", second_segment),
                waiting_tool_call_id="same-call",
            ),
        )


def test_successful_event_snapshot_and_state_writes_advance_updated_at(tmp_path: Path) -> None:
    ticks = iter(
        [
            datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 17, 1, 3, tzinfo=timezone.utc),
        ]
    )
    conversation_id, _ = _seed_conversation(tmp_path)
    repository = _repository(tmp_path, now_factory=lambda: next(ticks))
    created = repository.create_run_and_initial_segment(_start_command(conversation_id))
    first = created.run.updated_at
    repository.append_event(RUN_ID, _assistant_event(501))
    second = repository.get_run(RUN_ID).updated_at  # type: ignore[union-attr]
    repository.capture_context(RUN_ID, _snapshot_command())
    third = repository.get_run(RUN_ID).updated_at  # type: ignore[union-attr]
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(target_status="completed", events=_terminal_events("completed")),
    )
    fourth = repository.get_run(RUN_ID).updated_at  # type: ignore[union-attr]
    assert first < second < third < fourth


def test_sqlite_write_lock_fails_within_budget(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    blocker = sqlite3.connect(tmp_path / "data.db", timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    acquired = threading.Event()
    acquired.set()
    assert acquired.wait(timeout=0.01)
    started = time.monotonic()
    try:
        with pytest.raises(OperationalError):
            repository.append_event(
                RUN_ID,
                _assistant_event(601),
                deadline=time.monotonic() + 0.050,
                safe_clock=SafeClockAdapter(
                    lambda: MonotonicSample(time.monotonic(), True)
                ),
            )
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 0.25


def test_journal_pool_checkout_fails_within_budget(tmp_path: Path) -> None:
    _create_run(tmp_path)
    factory = journal_session_factory_for_data_dir(tmp_path)
    repository = AgentRunRepository(factory)
    engine = factory.kw["bind"]
    held = engine.connect()
    acquired = threading.Event()
    acquired.set()
    assert acquired.wait(timeout=0.01)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            repository.get_run(RUN_ID)
    finally:
        held.close()
    assert time.monotonic() - started < 0.25


def test_expired_call_deadline_prevents_repository_write(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)

    with pytest.raises(JournalDeadlineExceeded):
        repository.append_event(
            RUN_ID,
            _assistant_event(602),
            deadline=1.0,
            safe_clock=_safe_clock(1.0),
        )

    assert repository.count_events(RUN_ID, _assistant_event(602).dedupe_key) == 0


def test_context_conflict_does_not_modify_existing_snapshot(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    command = _snapshot_command()
    repository.capture_context(RUN_ID, command)
    changed = CaptureContextCommand(
        **{
            **command.__dict__,
            "prepared": PreparedSnapshot(
                **{**command.prepared.__dict__, "logical_input_fingerprint": "f" * 64}
            ),
        }
    )
    with pytest.raises(JournalConflictError):
        repository.capture_context(RUN_ID, changed)
    with repository.session_factory() as session:
        snapshots = list(session.scalars(select(AgentContextSnapshot)))
    assert len(snapshots) == 1
    assert snapshots[0].logical_input_fingerprint == "b" * 64


def test_model_call_can_have_only_one_context_snapshot(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    repository.capture_context(RUN_ID, _snapshot_command())
    second_segment = "77777777-7777-4777-8777-777777777777"
    tool_call_id = "call-second-snapshot"
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=_waiting_events(tool_call_id),
            waiting_tool_call_id=tool_call_id,
        ),
    )
    repository.start_segment(
        StartSegmentCommand(
            run_id=RUN_ID,
            segment_started=_segment_started(second_segment, request_kind="confirmation"),
        )
    )
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="running",
            events=(
                _resumed_event(
                    tool_call_id,
                    second_segment,
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac",
                ),
            ),
        ),
    )

    with pytest.raises(JournalConflictError, match="model call"):
        repository.capture_context(
            RUN_ID,
            _snapshot_command(
                snapshot_id="88888888-8888-4888-8888-888888888888",
                segment_id=second_segment,
            ),
        )

    assert len(repository.list_snapshots(RUN_ID)) == 1


def test_start_segment_is_idempotent_and_requires_live_run(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=_waiting_events("call-start-segment"),
            waiting_tool_call_id="call-start-segment",
        ),
    )
    segment_id = "77777777-7777-4777-8777-777777777777"
    command = StartSegmentCommand(
        run_id=RUN_ID,
        segment_started=_segment_started(segment_id, request_kind="confirmation"),
    )
    first = repository.start_segment(command)
    second = repository.start_segment(command)
    assert second.id == first.id
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="running",
            events=(
                _resumed_event(
                    "call-start-segment",
                    segment_id,
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad",
                ),
            ),
        ),
    )
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="completed",
            events=_terminal_events("completed", segment_id),
        ),
    )
    with pytest.raises(JournalConflictError):
        repository.start_segment(
            StartSegmentCommand(
                run_id=RUN_ID,
                segment_started=_segment_started(
                    "88888888-8888-4888-8888-888888888888",
                    request_kind="confirmation",
                ),
            )
        )


def test_start_segment_rejects_initial_kind_outside_atomic_run_creation(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)

    with pytest.raises(JournalConflictError, match="request kind"):
        repository.start_segment(
            StartSegmentCommand(
                run_id=RUN_ID,
                segment_started=_segment_started(
                    "99999999-9999-4999-8999-999999999998",
                ),
            )
        )


def test_pending_replay_segment_can_finish_noop_without_changing_run_state(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_run(tmp_path)
    tool_call_id = "call-pending-replay"
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=_waiting_events(tool_call_id),
            waiting_tool_call_id=tool_call_id,
        ),
    )
    segment_id = "99999999-9999-4999-8999-999999999997"
    repository.start_segment(
        StartSegmentCommand(
            run_id=RUN_ID,
            segment_started=_segment_started(segment_id, request_kind="pending_replay"),
        )
    )
    finished = repository.append_event(
        RUN_ID,
        prepare_event(
            event_type="segment.finished",
            execution_segment_id=segment_id,
            facts={"outcome": "noop", "terminal_run_status": None},
        ),
    )

    run = repository.get_run(RUN_ID)
    assert finished.event_type == "segment.finished"
    assert run is not None and run.status == "waiting_confirmation"


def test_confirmation_loser_can_finish_segment_after_winner_terminates_run(
    tmp_path: Path,
) -> None:
    repository, _, _ = _create_run(tmp_path)
    tool_call_id = "call-concurrent-confirmation"
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="waiting_confirmation",
            events=_waiting_events(tool_call_id),
            waiting_tool_call_id=tool_call_id,
        ),
    )
    winner_segment = "99999999-9999-4999-8999-999999999996"
    loser_segment = "99999999-9999-4999-8999-999999999995"
    for segment_id in (winner_segment, loser_segment):
        repository.start_segment(
            StartSegmentCommand(
                run_id=RUN_ID,
                segment_started=_segment_started(segment_id, request_kind="confirmation"),
            )
        )
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="running",
            events=(
                _resumed_event(
                    tool_call_id,
                    winner_segment,
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac",
                ),
            ),
        ),
    )
    repository.converge_disposition(
        RUN_ID,
        DispositionCommand(
            target_status="completed",
            events=_terminal_events("completed", winner_segment),
        ),
    )

    finished = repository.append_event(
        RUN_ID,
        prepare_event(
            event_type="segment.finished",
            execution_segment_id=loser_segment,
            facts={"outcome": "noop", "terminal_run_status": None},
        ),
    )

    run = repository.get_run(RUN_ID)
    assert finished.event_type == "segment.finished"
    assert run is not None and run.status == "completed"
    with pytest.raises(JournalConflictError, match="different stable facts"):
        repository.append_event(
            RUN_ID,
            prepare_event(
                event_type="segment.finished",
                execution_segment_id=winner_segment,
                facts={"outcome": "noop", "terminal_run_status": None},
            ),
        )


def _safe_clock(value: float, *, valid: bool = True) -> SafeClockAdapter:
    return SafeClockAdapter(lambda: MonotonicSample(value, valid))


def test_journal_repository_deadline_api_uses_safe_clock_adapter() -> None:
    assert JournalDeadlineExceeded is BudgetJournalDeadlineExceeded
    for name in (
        "create_run_and_initial_segment",
        "attach_input_message",
        "start_segment",
        "append_event",
        "capture_context",
        "converge_disposition",
        "mark_degraded",
        "find_waiting_run",
    ):
        signature = inspect.signature(getattr(AgentRunRepository, name))
        assert "clock" not in signature.parameters
        assert signature.parameters["deadline"].annotation == "float | None"
        assert signature.parameters["safe_clock"].annotation == "SafeClockAdapter | None"


def test_deadline_without_safe_clock_is_rejected_before_checkout(tmp_path: Path) -> None:
    repository, _, _ = _create_run(tmp_path)
    with pytest.raises(ValueError, match="safe_clock"):
        repository.append_event(RUN_ID, _assistant_event(603), deadline=1.0)


def test_dynamic_busy_timeout_uses_remaining_deadline_and_restores_default(tmp_path: Path) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    with repository._session_guard(deadline=0.012, safe_clock=_safe_clock(0.0)) as session:
        timeout = session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        assert 0 <= timeout <= 12
    with repository._session_guard(deadline=None, safe_clock=None) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


def test_public_write_commit_keeps_deadline_guard_under_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    database_path = tmp_path / "data.db"
    reader = sqlite3.connect(database_path, timeout=5.0)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM agent_runs").fetchall()
    commit_busy_timeouts: list[int] = []
    commit_elapsed: list[float] = []
    original_commit = SessionTransaction.commit
    original_restore = AgentRunRepository._restore_sqlite_guard
    restore_called = False

    def observe_restore(guard):
        nonlocal restore_called
        result = original_restore(guard)
        restore_called = True
        return result

    def observe_commit(transaction: SessionTransaction, *args, **kwargs):
        assert restore_called is False
        connection = transaction.session.connection()
        commit_busy_timeouts.append(
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        )
        commit_started = time.perf_counter()
        try:
            return original_commit(transaction, *args, **kwargs)
        finally:
            commit_elapsed.append(time.perf_counter() - commit_started)

    monkeypatch.setattr(
        AgentRunRepository,
        "_restore_sqlite_guard",
        staticmethod(observe_restore),
    )
    monkeypatch.setattr(SessionTransaction, "commit", observe_commit)
    started = time.perf_counter()
    try:
        with pytest.raises((JournalDeadlineExceeded, OperationalError)) as raised:
            repository.append_event(
                RUN_ID,
                _assistant_event(604),
                deadline=0.012,
                safe_clock=_safe_clock(0.0),
            )
    finally:
        reader.rollback()
        reader.close()
    elapsed = time.perf_counter() - started

    assert restore_called is True
    assert commit_busy_timeouts and commit_busy_timeouts[0] <= 12
    assert commit_elapsed and commit_elapsed[0] < 0.045
    assert elapsed < 0.250
    if isinstance(raised.value, JournalDeadlineExceeded):
        assert str(raised.value) == "deadline"
    else:
        assert "locked" in str(raised.value).lower()


def test_progress_handler_is_total_for_invalid_clock() -> None:
    callback = _progress_handler(_safe_clock(0.0, valid=False), 1.0)
    assert callback() != 0


def test_recursive_cte_interrupts_by_deadline_and_connection_is_reusable(tmp_path: Path) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    now = time.monotonic()
    started = time.monotonic()
    with pytest.raises(JournalDeadlineExceeded):
        with repository._session_guard(
            deadline=now + 0.010,
            safe_clock=SafeClockAdapter(
                lambda: MonotonicSample(time.monotonic(), True)
            ),
        ) as session:
            session.connection().exec_driver_sql(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n "
                "LIMIT 100000000) SELECT sum(x) FROM n"
            ).scalar_one()
    assert time.monotonic() - started < 0.250
    with repository._session_guard(deadline=None, safe_clock=None) as session:
        assert session.connection().exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


def test_native_blocking_udf_overshoot_is_classified_after_return_and_restored(
    tmp_path: Path,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    clock_value = 0.0
    udf_called = False

    def slow_udf() -> int:
        nonlocal clock_value, udf_called
        udf_called = True
        time.sleep(0.060)
        clock_value = 1.0
        return 1

    safe_clock = SafeClockAdapter(lambda: MonotonicSample(clock_value, True))
    with pytest.raises(JournalDeadlineExceeded):
        with repository._session_guard(
            deadline=0.500,
            safe_clock=safe_clock,
        ) as session:
            raw = session.connection().connection.driver_connection
            raw.create_function("slow_udf", 0, slow_udf)
            session.connection().exec_driver_sql("SELECT slow_udf()").scalar_one()
    assert udf_called is True
    assert clock_value >= 0.500
    with repository._session_guard(deadline=None, safe_clock=None) as session:
        assert session.connection().exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


def test_commit_and_rollback_cleanup_failure_invalidates_owned_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    engine = repository.session_factory.kw["bind"]
    owned_dbapi = None

    def fail_commit(_transaction: SessionTransaction) -> None:
        raise RuntimeError("commit primary")

    def fail_rollback(_session: Session) -> None:
        raise RuntimeError("rollback cleanup failed")

    monkeypatch.setattr(SessionTransaction, "commit", fail_commit)
    monkeypatch.setattr(Session, "rollback", fail_rollback)
    with pytest.raises(RuntimeError, match="commit primary"):
        with repository._journal_transaction(deadline=None, safe_clock=None) as session:
            owned_dbapi = session.connection().connection.driver_connection

    assert owned_dbapi is not None
    with engine.connect() as connection:
        next_dbapi = connection.connection.driver_connection
        assert next_dbapi is not owned_dbapi
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


class _RawConnectionProxy:
    def __init__(
        self,
        raw_connection,
        *,
        fail_handler_clear: bool = False,
        fail_handler_install: bool = False,
        fail_pragma_restore: bool = False,
    ) -> None:
        self.raw_connection = raw_connection
        self.fail_handler_clear = fail_handler_clear
        self.fail_handler_install = fail_handler_install
        self.fail_pragma_restore = fail_pragma_restore
        self.handler_clear_calls = 0
        self.handler_install_calls = 0
        self.pragma_restore_calls = 0

    def set_progress_handler(self, handler, steps):
        if handler is not None:
            self.handler_install_calls += 1
            result = self.raw_connection.set_progress_handler(handler, steps)
            if self.fail_handler_install:
                raise RuntimeError("partial handler install failed")
            return result
        if handler is None and steps == 0:
            self.handler_clear_calls += 1
            if self.fail_handler_clear:
                raise RuntimeError("handler clear failed")
        return self.raw_connection.set_progress_handler(handler, steps)

    def execute(self, statement, *parameters):
        if "PRAGMA busy_timeout = 50" in str(statement):
            self.pragma_restore_calls += 1
            if self.fail_pragma_restore:
                raise RuntimeError("pragma restore failed")
        return self.raw_connection.execute(statement, *parameters)

    def __getattr__(self, name):
        return getattr(self.raw_connection, name)


def _patch_raw_connection_proxy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_handler_clear: bool = False,
    fail_handler_install: bool = False,
    fail_pragma_restore: bool = False,
) -> list[_RawConnectionProxy]:
    proxies: list[_RawConnectionProxy] = []

    def wrap(connection):
        raw_connection = getattr(connection, "driver_connection", connection)
        proxy = _RawConnectionProxy(
            raw_connection,
            fail_handler_clear=fail_handler_clear,
            fail_handler_install=fail_handler_install,
            fail_pragma_restore=fail_pragma_restore,
        )
        proxies.append(proxy)
        return proxy

    monkeypatch.setattr(AgentRunRepository, "_raw_connection", staticmethod(wrap))
    return proxies


def _assert_next_borrower_is_distinct_and_healthy(
    repository: AgentRunRepository,
    owned_dbapi,
) -> None:
    assert owned_dbapi is not None
    engine = repository.session_factory.kw["bind"]
    with engine.connect() as connection:
        next_dbapi = connection.connection.driver_connection
        assert next_dbapi is not owned_dbapi
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


def test_rollback_cleanup_failure_invalidates_before_next_borrower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    owned_dbapi = None
    original_rollback = Session.rollback
    calls = 0

    def fail_once(session: Session) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("rollback cleanup failed")
        original_rollback(session)

    monkeypatch.setattr(Session, "rollback", fail_once)
    with pytest.raises(RuntimeError, match="primary"):
        with repository._journal_transaction(deadline=None, safe_clock=None) as session:
            owned_dbapi = session.connection().connection.driver_connection
            raise RuntimeError("primary")

    _assert_next_borrower_is_distinct_and_healthy(repository, owned_dbapi)


def test_handler_clear_failure_invalidates_before_next_borrower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    proxies = _patch_raw_connection_proxy(monkeypatch, fail_handler_clear=True)
    owned_dbapi = None
    with pytest.raises(RuntimeError, match="handler clear failed"):
        with repository._journal_transaction(
            deadline=1.0,
            safe_clock=_safe_clock(0.0),
        ) as session:
            owned_dbapi = session.connection().connection.driver_connection

    assert proxies and proxies[0].handler_clear_calls >= 1
    _assert_next_borrower_is_distinct_and_healthy(repository, owned_dbapi)


def test_partial_progress_handler_install_is_cleared_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    proxies = _patch_raw_connection_proxy(monkeypatch, fail_handler_install=True)
    with pytest.raises(RuntimeError, match="partial handler install failed"):
        with repository._journal_transaction(
            deadline=1.0,
            safe_clock=_safe_clock(0.0),
        ):
            pass

    assert proxies and proxies[0].handler_install_calls == 1
    assert proxies[0].handler_clear_calls >= 1
    with repository._session_guard(deadline=None, safe_clock=None) as session:
        assert session.connection().exec_driver_sql("SELECT 1").scalar_one() == 1
        assert (
            session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            == JOURNAL_DEFAULT_BUSY_TIMEOUT_MS
        )


def test_pragma_restore_failure_invalidates_before_next_borrower(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    proxies = _patch_raw_connection_proxy(monkeypatch, fail_pragma_restore=True)
    owned_dbapi = None
    with pytest.raises(RuntimeError, match="pragma restore failed"):
        with repository._journal_transaction(deadline=None, safe_clock=None) as session:
            owned_dbapi = session.connection().connection.driver_connection

    assert proxies and proxies[0].pragma_restore_calls >= 1
    _assert_next_borrower_is_distinct_and_healthy(repository, owned_dbapi)


@pytest.mark.parametrize("close_fails", [False, True])
def test_invalidation_closes_detached_raw_handle_after_record_invalidation(
    close_fails: bool,
) -> None:
    class RawConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if close_fails:
                raise RuntimeError("raw close failed")

    class ConnectionRecord:
        def __init__(self) -> None:
            self.invalidate_calls = 0
            self.dbapi_connection = object()

        def invalidate(self, _error: BaseException) -> None:
            self.invalidate_calls += 1

    class Connection:
        def __init__(self) -> None:
            self.detach_calls = 0

        def invalidate(self) -> None:
            raise RuntimeError("connection invalidate failed")

        def detach(self) -> None:
            self.detach_calls += 1

    raw_connection = RawConnection()
    connection_record = ConnectionRecord()
    guard = _SQLiteGuard(
        Connection(),
        raw_connection,
        False,
        connection_record,
    )

    error = AgentRunRepository._invalidate_sqlite_guard(guard)

    assert connection_record.invalidate_calls == 1
    assert raw_connection.close_calls == 1
    if close_fails:
        assert isinstance(error, RuntimeError)
        assert str(error) == "raw close failed"
    else:
        assert isinstance(error, RuntimeError)
        assert str(error) == "connection invalidate failed"


def test_invalidation_failure_does_not_return_aba_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_run(tmp_path)
    repository = _repository(tmp_path)
    monkeypatch.setattr(
        AgentRunRepository,
        "_restore_sqlite_guard",
        staticmethod(lambda _guard: (True, RuntimeError("restore cleanup failed"))),
    )
    monkeypatch.setattr(
        Connection,
        "invalidate",
        lambda _connection: (_ for _ in ()).throw(
            RuntimeError("invalidate cleanup failed")
        ),
    )
    owned_dbapi = None
    with pytest.raises(RuntimeError, match="restore cleanup failed"):
        with repository._journal_transaction(deadline=None, safe_clock=None) as session:
            owned_dbapi = session.connection().connection.driver_connection

    _assert_next_borrower_is_distinct_and_healthy(repository, owned_dbapi)
