from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from offerpilot.api import create_app
from offerpilot.db import init_database
from offerpilot.models import InterviewNote
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.notes import NoteCreate, NotesRepository


def test_create_and_list_application_events_with_application_fields(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()

    created = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "written_test",
            "subtype": "assessment",
            "tags": ["campus", "online"],
            "round": 2,
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 45,
            "location": "Zoom",
            "notes": "tech",
            "remind_at": "2026-07-10T09:30:00Z",
        },
    )
    listed = client.get("/api/application-events", params={"event_type": "written_test"})

    assert created.status_code == 201
    body = created.json()
    assert body["event_type"] == "written_test"
    assert body["subtype"] == "assessment"
    assert body["tags"] == ["campus", "online"]
    assert created.json()["duration_minutes"] == 45
    assert body["remind_at"] == "2026-07-10T09:30:00Z"
    assert listed.status_code == 200
    assert listed.json()[0]["company_name"] == "ByteDance"
    assert listed.json()[0]["position_name"] == "Backend"


def test_list_application_events_month_stays_within_requested_month(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()
    for scheduled_at in ["2026-07-10T10:00:00Z", "2026-08-01T10:00:00Z"]:
        client.post(
            "/api/application-events",
            json={
                "application_id": app["id"],
                "event_type": "interview",
                "scheduled_at": scheduled_at,
                "duration_minutes": 45,
            },
        )

    response = client.get("/api/application-events", params={"month": "2026-07"})

    assert response.status_code == 200
    assert [item["scheduled_at"] for item in response.json()] == [
        "2026-07-10T10:00:00Z"
    ]


def test_application_events_hide_soft_deleted_applications(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "interview",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 45,
        },
    ).json()

    client.delete(f"/api/applications/{app['id']}")
    response = client.get("/api/application-events")

    assert response.status_code == 200
    assert all(item["id"] != event["id"] for item in response.json())


def test_application_events_for_soft_deleted_applications_are_not_addressable(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()
    other = client.post(
        "/api/applications",
        json={"company_name": "OpenAI", "position_name": "Product"},
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "interview",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 45,
        },
    ).json()

    client.delete(f"/api/applications/{app['id']}")

    assert client.get(f"/api/application-events/{event['id']}").status_code == 404
    assert (
        client.put(
            f"/api/application-events/{event['id']}",
            json={
                "application_id": other["id"],
                "event_type": "interview",
                "scheduled_at": "2026-07-11T10:00:00Z",
                "duration_minutes": 45,
            },
        ).status_code
        == 404
    )
    assert client.delete(f"/api/application-events/{event['id']}").status_code == 404


def test_create_event_rejects_missing_application(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post(
        "/api/application-events",
        json={
            "application_id": 404,
            "event_type": "interview",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 45,
        },
    )

    assert response.status_code == 404
    assert response.json() == {"error": "Application not found"}


def test_application_event_validation_and_delete(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()

    invalid = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "coffee",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 45,
        },
    )
    assert invalid.status_code == 400
    assert invalid.json() == {"error": "Invalid event type"}

    legacy_assessment = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "assessment",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 30,
        },
    )
    assert legacy_assessment.status_code == 400
    assert legacy_assessment.json() == {"error": "Invalid event type"}

    event = client.post(
        "/api/application-events",
        json={
            "application_id": app["id"],
            "event_type": "written_test",
            "subtype": "assessment",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 30,
        },
    ).json()
    deleted = client.delete(f"/api/application-events/{event['id']}")

    assert deleted.status_code == 200
    assert deleted.json() == {"message": "Deleted"}
    assert client.get(f"/api/application-events/{event['id']}").status_code == 404


def test_event_delete_trigger_failure_rolls_back_note_unbind_and_revision(tmp_path):
    session_factory = init_database(tmp_path / "rollback.db")
    application = ApplicationsRepository(session_factory).create(
        ApplicationCreate(company_name="Acme", position_name="Backend")
    )
    events = ApplicationEventsRepository(session_factory)
    event = events.create(
        ApplicationEventCreate(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 30, 10, tzinfo=timezone.utc),
            duration_minutes=45,
        )
    )
    note = NotesRepository(session_factory).create(
        NoteCreate(
            application_id=application.id,
            application_event_id=event.id,
            company="Acme",
        )
    )
    with session_factory() as session:
        session.execute(
            text(
                "CREATE TRIGGER reject_event_delete BEFORE DELETE ON application_events "
                "BEGIN SELECT RAISE(ABORT, 'blocked event delete'); END"
            )
        )
        session.commit()

    with pytest.raises(IntegrityError, match="blocked event delete"):
        events.delete(event.id)

    with session_factory() as session:
        stored = session.get(InterviewNote, note.id)
        assert stored is not None
        assert stored.application_event_id == event.id
        assert stored.content_revision == 1


def test_missing_event_delete_changes_no_bound_note_revision(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application = client.post(
        "/api/applications",
        json={"company_name": "Acme", "position_name": "Backend"},
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application["id"],
            "event_type": "interview",
            "scheduled_at": "2026-08-30T10:00:00Z",
            "duration_minutes": 45,
        },
    ).json()
    note = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"application_event_id": event["id"]},
    ).json()

    assert client.delete("/api/application-events/9223372036854775807").status_code == 404

    stored = next(item for item in client.get("/api/notes").json() if item["id"] == note["id"])
    assert stored["application_event_id"] == event["id"]
    assert stored["content_revision"] == 1


def test_legacy_events_api_is_not_exposed(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/events")

    assert response.status_code == 404
