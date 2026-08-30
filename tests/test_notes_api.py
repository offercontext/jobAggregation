from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from offerpilot.api import create_app
from offerpilot.db import init_database
from offerpilot.models import InterviewNote
from offerpilot.repositories.notes import NoteCreate, NoteUpdate, NotesRepository


def test_create_note_for_application_backfills_company_and_position(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    app = client.post(
        "/api/applications",
        json={"company_name": "ByteDance", "position_name": "Backend"},
    ).json()

    response = client.post(
        f"/api/applications/{app['id']}/notes",
        json={"round": "一面", "date": "2026-07-11", "questions": "Go?"},
    )

    assert response.status_code == 201
    assert response.json()["application_id"] == app["id"]
    assert response.json()["company"] == "ByteDance"
    assert response.json()["position"] == "Backend"


def test_note_rest_output_exposes_server_owned_revision_and_timestamp(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    created = client.post("/api/notes", json={"company": "Acme"})
    first_update = client.put(
        f"/api/notes/{created.json()['id']}",
        json={"company": "Acme", "questions": "first"},
    )
    second_update = client.put(
        f"/api/notes/{created.json()['id']}",
        json={"company": "Acme", "questions": "second"},
    )

    assert created.status_code == 201
    assert created.json()["content_revision"] == 1
    assert created.json()["updated_at"]
    assert first_update.json()["content_revision"] == 2
    assert second_update.json()["content_revision"] == 3
    assert client.get("/api/notes").json()[0]["content_revision"] == 3


def test_bound_repository_updates_increment_revision_atomically_without_committing(tmp_path):
    session_factory = init_database(tmp_path / "bound.db")
    repository = NotesRepository(session_factory)
    note = repository.create(NoteCreate(company="Acme"))
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with session_factory() as session:
        session.execute(
            text("UPDATE interview_notes SET updated_at=:old WHERE id=:note_id"),
            {"old": old_timestamp, "note_id": note.id},
        )
        session.commit()
        bound = repository.bind(session)

        first = bound.update(note.id, NoteUpdate(company="Acme", questions="first"))
        first_revision = first.content_revision if first is not None else None
        second = bound.update(note.id, NoteUpdate(company="Acme", questions="second"))

        assert first is not None
        assert second is not None
        assert first_revision == 2
        assert second.content_revision == 3
        assert second.updated_at > old_timestamp.replace(tzinfo=None)
        assert session.get_transaction() is not None
        session.rollback()

    with session_factory() as session:
        stored = session.get(InterviewNote, note.id)
        assert stored is not None
        assert stored.content_revision == 1
        assert stored.questions == ""


def test_create_standalone_note_requires_company(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post("/api/notes", json={"position": "Backend"})

    assert response.status_code == 400
    assert response.json() == {"error": "company is required"}


def test_list_update_delete_notes(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    note = client.post(
        "/api/notes",
        json={"company": "ByteDance", "position": "Backend", "round": "一面"},
    ).json()

    listed = client.get("/api/notes")
    updated = client.put(
        f"/api/notes/{note['id']}",
        json={"company": "Tencent", "position": "Frontend", "round": "二面"},
    )
    deleted = client.delete(f"/api/notes/{note['id']}")

    assert listed.status_code == 200
    assert listed.json()[0]["company"] == "ByteDance"
    assert updated.status_code == 200
    assert updated.json()["company"] == "Tencent"
    assert updated.json()["application_id"] is None
    assert updated.json()["application_event_id"] is None
    assert deleted.status_code == 200
    assert deleted.json() == {"message": "Deleted"}


def _create_application_and_event(client, *, company="ByteDance", event_type="interview"):
    application = client.post(
        "/api/applications",
        json={"company_name": company, "position_name": "Backend"},
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application["id"],
            "event_type": event_type,
            "scheduled_at": "2026-07-20T10:00:00Z",
            "duration_minutes": 45,
        },
    )
    assert event.status_code == 201
    return application, event.json()


def _create_bound_note(client, application, event):
    response = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"application_event_id": event["id"], "questions": "How do you test?"},
    )
    assert response.status_code == 201
    return response.json()


def test_bound_note_update_preserves_ownership_and_event(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    note = _create_bound_note(client, application, event)

    response = client.put(
        f"/api/notes/{note['id']}",
        json={"questions": "How do you debug?"},
    )

    assert response.status_code == 200
    assert response.json()["application_id"] == application["id"]
    assert response.json()["application_event_id"] == event["id"]
    assert response.json()["questions"] == "How do you debug?"


def test_bound_note_can_explicitly_unbind_event_without_unbinding_application(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    note = _create_bound_note(client, application, event)

    response = client.put(
        f"/api/notes/{note['id']}",
        json={"application_event_id": None, "questions": "Updated"},
    )

    assert response.status_code == 200
    assert response.json()["application_id"] == application["id"]
    assert response.json()["application_event_id"] is None


def test_bound_note_rejects_application_reassignment_or_unbinding(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    other = client.post(
        "/api/applications",
        json={"company_name": "Tencent", "position_name": "Frontend"},
    ).json()
    note = _create_bound_note(client, application, event)

    null_response = client.put(
        f"/api/notes/{note['id']}", json={"application_id": None}
    )
    other_response = client.put(
        f"/api/notes/{note['id']}", json={"application_id": other["id"]}
    )

    assert null_response.status_code == 422
    assert other_response.status_code == 422
    current = next(
        item for item in client.get("/api/notes").json() if item["id"] == note["id"]
    )
    assert current["content_revision"] == 1


def test_note_binding_requires_same_application_interview_event(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, _ = _create_application_and_event(client)
    other, other_event = _create_application_and_event(client, company="Tencent")
    _, written_test = _create_application_and_event(client, event_type="written_test")

    cross_app = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"application_event_id": other_event["id"]},
    )
    non_interview = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"application_event_id": written_test["id"]},
    )

    assert other["id"] != application["id"]
    assert cross_app.status_code == 422
    assert non_interview.status_code == 422


def test_one_main_note_per_interview_event(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    first = _create_bound_note(client, application, event)
    second = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"application_event_id": event["id"]},
    )

    assert first["application_event_id"] == event["id"]
    assert second.status_code == 409


def test_deleting_event_unbinds_note_but_deleting_application_hides_bound_notes(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    note = _create_bound_note(client, application, event)

    assert client.delete(f"/api/application-events/{event['id']}").status_code == 200
    after_event_delete = client.get("/api/notes").json()
    assert after_event_delete[0]["id"] == note["id"]
    assert after_event_delete[0]["application_event_id"] is None
    assert after_event_delete[0]["content_revision"] == 2

    application_two, event_two = _create_application_and_event(client, company="Tencent")
    note_two = _create_bound_note(client, application_two, event_two)
    assert client.delete(f"/api/applications/{application_two['id']}").status_code == 200

    assert all(item["id"] != note_two["id"] for item in client.get("/api/notes").json())
    assert client.get(f"/api/applications/{application_two['id']}/notes").status_code == 404
    assert client.put(f"/api/notes/{note_two['id']}", json={"questions": "x"}).status_code == 404
    assert client.delete(f"/api/notes/{note_two['id']}").status_code == 404


def test_deleting_event_does_not_revision_an_already_unbound_note(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    application, event = _create_application_and_event(client)
    note = client.post(
        f"/api/applications/{application['id']}/notes",
        json={"questions": "Not bound to the event"},
    ).json()

    assert client.delete(f"/api/application-events/{event['id']}").status_code == 200

    stored = next(item for item in client.get("/api/notes").json() if item["id"] == note["id"])
    assert stored["application_event_id"] is None
    assert stored["content_revision"] == 1

