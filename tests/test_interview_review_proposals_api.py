from __future__ import annotations

import json

from fastapi.testclient import TestClient

from offerpilot.ai.types import Assistant
from offerpilot.api import create_app
from offerpilot.diagnostics import read_recent_log_entries


def _payload() -> dict[str, object]:
    return {
        "summary": {
            "text": "The reflection highlights a tradeoff to revisit.",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/self_reflection",
                    "excerpt": "I struggled to explain the tradeoff.",
                }
            ],
        },
        "observations": [],
        "clarifications": [],
        "practice_focuses": [],
        "next_questions": [],
    }


class FakeModel:
    def __init__(self, *, error: Exception | None = None, response: object | None = None) -> None:
        self.calls = 0
        self.error = error
        self.response = response or _payload()

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Assistant(content=json.dumps(self.response, ensure_ascii=False))


def _create_bound_note(client: TestClient) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    application = client.post(
        "/api/applications",
        json={"company_name": "Acme", "position_name": "Backend"},
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application["id"],
            "event_type": "interview",
            "scheduled_at": "2026-07-20T10:00:00Z",
            "duration_minutes": 45,
        },
    ).json()
    note = client.post(
        f"/api/applications/{application['id']}/notes",
        json={
            "application_event_id": event["id"],
            "questions": "How would you design a cache?",
            "self_reflection": "I struggled to explain the tradeoff.",
            "difficulty_points": "Explaining tradeoffs",
            "mood": "nervous",
        },
    ).json()
    return application, event, note


def test_create_is_idempotent_and_returns_source_status(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    _, _, note = _create_bound_note(client)

    first = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-1"},
    )
    second = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-1"},
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["source_status"] == "current"
    assert first.json()["proposal_schema_version"] == 2
    assert first.json()["source_note_revision"] == note["content_revision"]
    assert first.json()["proposal"]["summary"]["evidence_refs"][0]["excerpt"] == (
        "I struggled to explain the tradeoff."
    )
    assert model.calls == 1


def test_idempotent_hit_returns_existing_proposal_before_provider_resolution(tmp_path) -> None:
    first_client = TestClient(create_app(data_dir=tmp_path, chat_model=FakeModel()))
    _, _, note = _create_bound_note(first_client)
    created = first_client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "provider-can-disappear"},
    )
    assert created.status_code == 201

    client_without_provider = TestClient(create_app(data_dir=tmp_path))
    replay = client_without_provider.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "provider-can-disappear"},
    )

    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]


def test_history_and_detail_remain_readable_after_event_delete(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path, chat_model=FakeModel()))
    _, event, note = _create_bound_note(client)
    created = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-1"},
    ).json()
    assert client.delete(f"/api/application-events/{event['id']}").status_code == 200

    history = client.get(f"/api/notes/{note['id']}/interview-review-proposals")
    detail = client.get(
        f"/api/notes/{note['id']}/interview-review-proposals/{created['id']}"
    )
    generation = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-2"},
    )

    assert history.status_code == 200
    assert history.json()[0]["source_status"] == "source_changed"
    assert detail.status_code == 200
    assert detail.json()["source_status"] == "source_changed"
    assert generation.status_code == 422
    assert generation.json()["error_code"] == "interview_review_event_required"


def test_provider_and_unverifiable_failures_are_safe_and_do_not_persist(tmp_path) -> None:
    provider = FakeModel(error=TimeoutError("secret note and API key"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=provider))
    _, _, note = _create_bound_note(client)
    response = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-provider"},
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "AI 服务暂不可用，请稍后重试。",
        "error_code": "interview_review_provider_error",
    }
    assert client.get(f"/api/notes/{note['id']}/interview-review-proposals").json() == []
    assert "secret note" not in response.text

    invalid = FakeModel(response={"summary": {"text": "not enough"}})
    invalid_client = TestClient(create_app(data_dir=tmp_path / "invalid", chat_model=invalid))
    _, _, invalid_note = _create_bound_note(invalid_client)
    invalid_response = invalid_client.post(
        f"/api/notes/{invalid_note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-invalid"},
    )
    assert invalid_response.status_code == 201
    assert invalid_response.json()["proposal"]["summary"]["evidence_refs"] == []
    assert len(invalid_client.get(
        f"/api/notes/{invalid_note['id']}/interview-review-proposals"
    ).json()) == 1


def test_contract_failure_persists_safe_empty_and_logs_only_safe_diagnostic(tmp_path) -> None:
    invalid = FakeModel(
        response={
            "summary": {"text": "candidate secret", "evidence_refs": []},
            "unexpected": "raw model output",
        }
    )
    client = TestClient(create_app(data_dir=tmp_path, chat_model=invalid))
    _, _, note = _create_bound_note(client)

    response = client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-safe-empty"},
    )

    assert response.status_code == 201
    assert response.json()["proposal"]["summary"]["evidence_refs"] == []
    assert response.json()["proposal"]["observations"] == []
    assert response.json()["source_status"] == "current"
    assert client.post(
        f"/api/notes/{note['id']}/interview-review-proposals",
        json={"idempotency_key": "attempt-safe-empty"},
    ).status_code == 200
    logs = read_recent_log_entries(tmp_path)
    message = logs[-1]["message"]
    assert "category=unexpected_field" in message
    assert "repair_attempted=true" in message
    assert "retry_count=1" in message
    assert "candidate secret" not in message
    assert "raw model output" not in message


def test_soft_deleted_note_application_returns_safe_not_found(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path, chat_model=FakeModel()))
    application, _, note = _create_bound_note(client)
    assert client.delete(f"/api/applications/{application['id']}").status_code == 200

    response = client.get(f"/api/notes/{note['id']}/interview-review-proposals")
    assert response.status_code == 404
    assert response.json()["error_code"] == "interview_review_not_found"
