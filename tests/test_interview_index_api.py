from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    ApplicationEvent,
    InterviewNote,
    InterviewReviewProposal,
    KnowledgeCapturedSourceMetadata,
    KnowledgeSource,
)
from offerpilot.repositories.application_events import ApplicationEventCreate, ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.interview_index import _item
from offerpilot.repositories.notes import NoteCreate, NotesRepository


def _ready(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    applications = ApplicationsRepository(session_factory_for_data_dir(tmp_path))
    application = applications.create(ApplicationCreate(company_name="Acme", position_name="Backend"))
    event = ApplicationEventsRepository(session_factory_for_data_dir(tmp_path)).create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 7, 28, 9, tzinfo=timezone.utc),
            duration_minutes=60,
        )
    )
    note = NotesRepository(session_factory_for_data_dir(tmp_path)).create(
        NoteCreate(
            application_id=application.id,
            application_event_id=event.id,
            company="Acme",
            position="Backend",
            questions="Tell me about APIs",
        )
    )
    return client, applications, application, event, note


def test_interview_index_lists_visible_events_and_bound_notes(tmp_path) -> None:
    client, _applications, application, event, note = _ready(tmp_path)

    response = client.get("/api/interviews")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert body["items"] == [
        {
            "application_id": application.id,
            "event_id": event.id,
            "company_name": "Acme",
            "position_name": "Backend",
            "scheduled_at": "2026-07-28T09:00:00+00:00",
            "note_id": note.id,
            "note_source_status": "current",
            "has_review_proposal": False,
            "review_summary": None,
            "has_confirmed_knowledge": False,
            "event_status": "todo",
            "duration_minutes": 60,
            "scheduled_at_state": "present",
            "preparation_available": True,
        }
    ]


def test_interview_index_excludes_soft_deleted_application_but_deep_link_is_404(tmp_path) -> None:
    client, applications, application, event, _note = _ready(tmp_path)
    applications.delete(application.id)

    assert client.get("/api/interviews").status_code == 200
    assert client.get("/api/interviews").json()["items"] == []
    detail = client.get(f"/api/interviews/{event.id}")
    assert detail.status_code == 404
    assert detail.json()["error_code"] == "interview_not_found"


def test_interview_index_rejects_invalid_pagination(tmp_path) -> None:
    client, *_ = _ready(tmp_path)
    assert client.get("/api/interviews?limit=0").status_code == 422
    assert client.get("/api/interviews?limit=201").status_code == 422


def test_interview_index_puts_unscheduled_events_last_and_uses_recent_tie_breakers(tmp_path) -> None:
    client, _applications, application, scheduled, _note = _ready(tmp_path)
    events = ApplicationEventsRepository(session_factory_for_data_dir(tmp_path))
    unscheduled = events.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=None,
            duration_minutes=None,
        )
    )
    later = events.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 7, 29, 9, tzinfo=timezone.utc),
            duration_minutes=60,
        )
    )

    items = client.get("/api/interviews").json()["items"]
    assert [item["event_id"] for item in items] == [scheduled.id, later.id, unscheduled.id]


def test_interview_index_exposes_preparation_entry(tmp_path) -> None:
    client, *_ = _ready(tmp_path)

    item = client.get("/api/interviews").json()["items"][0]

    assert item["preparation_available"] is True


def test_interview_index_uses_latest_bound_note_before_pagination_and_list_get_agree(tmp_path) -> None:
    client, _applications, application, event, first_note = _ready(tmp_path)
    with session_factory_for_data_dir(tmp_path)() as session:
        session.execute(text("DROP INDEX IF EXISTS uq_interview_notes_event_main"))
        second_note = InterviewNote(
            application_id=application.id,
            application_event_id=event.id,
            company="Acme",
            position="Backend",
            questions="new question",
            created_at=datetime(2026, 7, 29, 10, tzinfo=timezone.utc),
        )
        session.add(second_note)
        session.flush()
        first_note_row = session.get(InterviewNote, first_note.id)
        assert first_note_row is not None
        first_note_row.created_at = datetime(2026, 7, 28, 10, tzinfo=timezone.utc)
        session.commit()

    listed = client.get("/api/interviews").json()["items"]
    detail = client.get(f"/api/interviews/{event.id}").json()
    assert len(listed) == 1
    assert listed[0] == detail
    assert detail["note_id"] == second_note.id


def test_interview_index_breaks_equal_note_timestamp_by_note_id(tmp_path) -> None:
    client, _applications, application, event, _first_note = _ready(tmp_path)
    timestamp = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    with session_factory_for_data_dir(tmp_path)() as session:
        session.execute(text("DROP INDEX IF EXISTS uq_interview_notes_event_main"))
        first = InterviewNote(
            application_id=application.id,
            application_event_id=event.id,
            company="Acme",
            position="Backend",
            questions="first",
            created_at=timestamp,
        )
        second = InterviewNote(
            application_id=application.id,
            application_event_id=event.id,
            company="Acme",
            position="Backend",
            questions="second",
            created_at=timestamp,
        )
        session.add_all([first, second])
        session.flush()
        session.commit()
        expected_id = max(first.id, second.id)

    assert client.get(f"/api/interviews/{event.id}").json()["note_id"] == expected_id


def test_interview_index_paginates_unique_events_and_excludes_application_level_notes(tmp_path) -> None:
    client, _applications, application, first_event, _note = _ready(tmp_path)
    events = ApplicationEventsRepository(session_factory_for_data_dir(tmp_path))
    second_event = events.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 7, 29, 9, tzinfo=timezone.utc),
            duration_minutes=60,
        )
    )
    NotesRepository(session_factory_for_data_dir(tmp_path)).create(
        NoteCreate(
            application_id=application.id,
            application_event_id=None,
            company="Acme",
            position="Backend",
            questions="general review",
        )
    )

    first_page = client.get("/api/interviews?limit=1").json()
    second_page = client.get(f"/api/interviews?limit=1&cursor={first_page['next_cursor']}").json()
    assert [item["event_id"] for item in first_page["items"] + second_page["items"]] == [
        first_event.id,
        second_event.id,
    ]
    second_item = next(item for item in second_page["items"] if item["event_id"] == second_event.id)
    assert second_item["note_id"] is None


def test_interview_index_additive_fields_use_raw_event_values_and_preparation_truth_table(tmp_path) -> None:
    client, _applications, _application, event, _note = _ready(tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    active_statuses = ("todo", "pending", "scheduled", "in_progress")
    for status in active_statuses:
        with session_factory() as session:
            row = session.get(ApplicationEvent, event.id)
            assert row is not None
            row.status = status
            row.duration_minutes = 1
            row.scheduled_at = datetime(2026, 7, 28, 9, tzinfo=timezone.utc)
            session.commit()
        item = client.get(f"/api/interviews/{event.id}").json()
        assert item["event_status"] == status
        assert item["duration_minutes"] == 1
        assert item["scheduled_at_state"] == "present"
        assert item["preparation_available"] is True

    for status in ("done", "completed", "cancelled", "unknown"):
        with session_factory() as session:
            row = session.get(ApplicationEvent, event.id)
            assert row is not None
            row.status = status
            session.commit()
        assert client.get(f"/api/interviews/{event.id}").json()["preparation_available"] is False

    for duration in (0, -1, 10081):
        with session_factory() as session:
            row = session.get(ApplicationEvent, event.id)
            assert row is not None
            row.status = "todo"
            row.duration_minutes = duration
            session.commit()
        item = client.get(f"/api/interviews/{event.id}").json()
        assert item["duration_minutes"] == duration
        assert item["preparation_available"] is False

    for duration in (1, 10080):
        with session_factory() as session:
            row = session.get(ApplicationEvent, event.id)
            assert row is not None
            row.duration_minutes = duration
            session.commit()
        assert client.get(f"/api/interviews/{event.id}").json()["preparation_available"] is True

    with session_factory() as session:
        row = session.get(ApplicationEvent, event.id)
        assert row is not None
        row.scheduled_at = None
        session.commit()
    item = client.get(f"/api/interviews/{event.id}").json()
    assert item["scheduled_at_state"] == "absent"
    assert item["scheduled_at"] == "0001-01-01T00:00:00+00:00"
    assert item["preparation_available"] is False


@pytest.mark.parametrize("duration", [None, True])
def test_interview_index_rejects_non_integer_duration_without_repairing_value(duration) -> None:
    event = ApplicationEvent(
        application_id=1,
        event_type="interview",
        scheduled_at=datetime(2026, 7, 28, 9, tzinfo=timezone.utc),
        duration_minutes=duration,
        status="todo",
    )

    item = _item((event, "Acme", "Backend", None, None, False))

    assert item.duration_minutes is duration
    assert item.preparation_available is False


def test_interview_index_marks_changed_review_source(tmp_path) -> None:
    client, _applications, _application, event, note = _ready(tmp_path)
    with session_factory_for_data_dir(tmp_path)() as session:
        session.add(
            InterviewReviewProposal(
                note_id=note.id,
                application_event_id=event.id,
                idempotency_key="review-index-source-change",
                input_snapshot_json="{}",
                source_fingerprint="old-fingerprint",
                proposal_json="{}",
                proposal_hash="proposal-hash",
            )
        )
        session.commit()

    item = client.get("/api/interviews").json()["items"][0]
    assert item["note_source_status"] == "source_changed"


def test_interview_index_does_not_assign_ambiguous_capture_to_any_event(tmp_path) -> None:
    client, _applications, _application, first_event, note = _ready(tmp_path)
    second_event = ApplicationEventsRepository(session_factory_for_data_dir(tmp_path)).create(
        ApplicationEventCreate(
            application_id=note.application_id,
            event_type="interview",
            scheduled_at=datetime(2026, 7, 29, 9, tzinfo=timezone.utc),
            duration_minutes=60,
        )
    )
    with session_factory_for_data_dir(tmp_path)() as session:
        source = KnowledgeSource(
            source_hash="ambiguous-index-source",
            source_kind="captured_interview_note",
            title_hint="Ambiguous capture",
            main_filename="interview-note.txt",
            main_media_type="text/plain",
            main_relative_path="captured://interview-note/ambiguous",
            total_bytes=1,
        )
        session.add(source)
        session.flush()
        session.add(
            KnowledgeCapturedSourceMetadata(
                source_id=source.id,
                origin_note_id=note.id,
                application_event_id=None,
                note_fingerprint="ambiguous-note-fingerprint",
                selected_fragments_json="[]",
                capture_schema_version="interview-note-capture-v1",
            )
        )
        for event_id in (first_event.id, second_event.id):
            session.add(
                InterviewReviewProposal(
                    note_id=note.id,
                    application_event_id=event_id,
                    idempotency_key=f"ambiguous-index-review-{event_id}",
                    input_snapshot_json="{}",
                    source_fingerprint=f"ambiguous-index-fingerprint-{event_id}",
                    proposal_json="{}",
                    proposal_hash=f"ambiguous-index-hash-{event_id}",
                )
            )
        session.commit()

    items = {item["event_id"]: item for item in client.get("/api/interviews").json()["items"]}
    assert items[first_event.id]["has_confirmed_knowledge"] is False
    assert items[second_event.id]["has_confirmed_knowledge"] is False


def test_interview_index_keeps_review_history_after_note_unbind(tmp_path) -> None:
    client, _applications, _application, event, note = _ready(tmp_path)
    with session_factory_for_data_dir(tmp_path)() as session:
        session.add(
            InterviewReviewProposal(
                note_id=note.id,
                application_event_id=event.id,
                idempotency_key="review-index-unbound",
                input_snapshot_json=json.dumps({"event": {"id": event.id}}),
                source_fingerprint="old-fingerprint",
                proposal_json=json.dumps({"summary": {"text": "历史复盘摘要"}}),
                proposal_hash="proposal-hash",
            )
        )
        session.commit()

    assert client.put(f"/api/notes/{note.id}", json={"application_event_id": None}).status_code == 200

    item = client.get("/api/interviews").json()["items"][0]
    assert item["note_id"] is None
    assert item["has_review_proposal"] is True
    assert item["review_summary"] == "历史复盘摘要"
    assert item["note_source_status"] == "source_changed"


def test_interview_index_keeps_review_and_knowledge_history_after_note_delete(tmp_path) -> None:
    client, _applications, _application, event, note = _ready(tmp_path)
    with session_factory_for_data_dir(tmp_path)() as session:
        session.add(
            InterviewReviewProposal(
                note_id=note.id,
                application_event_id=event.id,
                idempotency_key="review-index-deleted",
                input_snapshot_json=json.dumps({"event": {"id": event.id}}),
                source_fingerprint="old-fingerprint",
                proposal_json=json.dumps({"summary": {"text": "删除后的复盘摘要"}}),
                proposal_hash="proposal-hash",
            )
        )
        source = KnowledgeSource(
            source_hash="captured-source-index",
            source_kind="captured_interview_note",
            title_hint="Captured interview",
            main_filename="interview-note.txt",
            main_media_type="text/plain",
            main_relative_path="captured://interview-note/1",
            total_bytes=10,
        )
        session.add(source)
        session.flush()
        session.add(
            KnowledgeCapturedSourceMetadata(
                source_id=source.id,
                origin_note_id=note.id,
                application_event_id=event.id,
                note_fingerprint="note-fingerprint",
                selected_fragments_json="[]",
                capture_schema_version="interview-note-capture-v1",
            )
        )
        session.commit()

    assert client.delete(f"/api/notes/{note.id}").status_code == 200

    item = client.get("/api/interviews").json()["items"][0]
    assert item["note_id"] is None
    assert item["has_review_proposal"] is True
    assert item["has_confirmed_knowledge"] is True
    assert item["review_summary"] == "删除后的复盘摘要"
    assert item["note_source_status"] == "source_changed"
