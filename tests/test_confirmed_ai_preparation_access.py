from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from fastapi.testclient import TestClient

from offerpilot.ai.types import Assistant, ToolCall
from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import Application, WriteOperation, InterviewPreparationProposal
from offerpilot.ai.write_operations import build_terminal_payload
from offerpilot.repositories.application_preparation_access import can_prepare_application
from tests.test_interview_preparation_api import FakeModel, _payload


class ConfirmedApplicationModel(FakeModel):
    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        if self.calls == 0:
            self.calls += 1
            return Assistant(
                tool_calls=[
                    ToolCall(
                        id="confirmed-application",
                        name="create_application",
                        args=json.dumps(
                            {"company_name": "星河测试", "position_name": "后端工程师"}
                        ),
                    )
                ]
            )
        if self.calls == 1:
            self.calls += 1
            return Assistant(content="已按你的确认创建投递。")
        return super().complete(messages, tools)


@pytest.fixture(scope="module")
def confirmed_context(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("confirmed-ai-preparation")
    model = ConfirmedApplicationModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    pending_response = client.post(
        "/api/chat",
        json={
            "message": "请创建星河测试的后端工程师投递",
            "conversation_id": 0,
        },
    )
    assert pending_response.status_code == 200
    pending = pending_response.json()
    assert pending["type"] == "confirmation_required"
    assert client.get("/api/applications").json() == []
    confirmed = client.post(
        "/api/chat/confirm",
        json={
            "conversation_id": pending["conversation_id"],
            "approved": True,
            "confirmation_token": pending["pending_action"]["confirmation_token"],
        },
    )
    assert confirmed.status_code == 200
    application = client.get("/api/applications").json()[0]
    assert application["source"] == "ai"
    resume = client.post(
        "/api/resumes",
        json={
            "title": "样例后端简历",
            "text": "Built reliable API services",
            "content_json": {"raw_text": "Built reliable API services"},
        },
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application["id"],
            "event_type": "interview",
            "scheduled_at": "2031-01-02T09:00:00Z",
            "duration_minutes": 60,
        },
    ).json()
    jd = client.post(
        f"/api/applications/{application['id']}/job-description/versions",
        json={
            "jd_text": "Build reliable API services",
            "source_url": None,
            "expected_current_version_id": None,
            "idempotency_key": "confirmed-ai-jd-0001",
        },
    ).json()
    payload = _payload(resume["id"], event["id"], jd_version_id=jd["id"])
    yield client, model, application, payload, session_factory_for_data_dir(tmp_path)
    client.close()


def test_confirmed_ai_application_can_prepare_and_replay_without_provider(confirmed_context):
    client, model, application, payload, _ = confirmed_context
    calls = model.calls
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    assert model.calls == calls + 1
    replay = client.post(url, json=payload)
    assert replay.status_code == 200, replay.text
    assert model.calls == calls + 1


@pytest.mark.parametrize(
    "case",
    [
        "unknown_source",
        "deleted",
        "no_ledger",
        "digest",
        "missing_approval",
        "duplicate_candidate",
        "wrong_result_id",
        "wrong_created_at",
        "wrong_result_source",
        "boolean_id",
        "missing_authorization",
        "wrong_contract",
    ],
)
def test_ai_preparation_requires_intact_exact_creation_proof(confirmed_context, case, monkeypatch):
    _, model, application, _, factory = confirmed_context
    calls = model.calls
    with factory() as session, session.no_autoflush:
        app = session.get(Application, application["id"])
        operation = session.scalar(
            select(WriteOperation).where(WriteOperation.tool_name == "create_application")
        )
        assert app is not None and operation is not None
        assert can_prepare_application(session, app)
        if case == "unknown_source":
            app.source = "crawler"
        elif case == "deleted":
            app.deleted_at = app.created_at
        elif case == "no_ledger":
            app.id += 1000
        elif case == "digest":
            operation.terminal_payload_sha256 = "sha256:" + "0" * 64
        elif case == "missing_approval":
            # Simulate an incomplete read without disabling immutable Ledger triggers.
            execute = session.execute
            monkeypatch.setattr(
                session,
                "execute",
                lambda stmt: [row for row in execute(stmt) if row.state != "approved"],
            )
        elif case == "duplicate_candidate":
            monkeypatch.setattr(session, "scalars", lambda stmt: [operation, operation])
        elif case == "missing_authorization":
            operation.authorization_scope_fingerprint = None
        elif case == "wrong_contract":
            operation.result_contract = "legacy_string_v1"
        else:
            result = json.loads(operation.result_json)
            if case == "wrong_result_id":
                result["id"] += 1
            elif case == "boolean_id":
                result["id"] = True
            elif case == "wrong_result_source":
                result["source"] = "web"
            elif case == "wrong_created_at":
                result["created_at"] = (app.created_at + timedelta(seconds=1)).isoformat()
            terminal = build_terminal_payload(
                status="committed",
                result_contract=operation.result_contract,
                result=result,
                visible_result=operation.visible_result,
                transport=json.loads(operation.transport_json),
                undo=json.loads(operation.undo_json),
                failure_category=None,
                failure_code=None,
            )
            operation.result_json = terminal.result_json
            operation.terminal_payload_sha256 = terminal.digest
        assert not can_prepare_application(session, app), case
        session.rollback()
    assert model.calls == calls


@pytest.mark.parametrize("v2", [False, True])
def test_provider_return_rechecks_creation_proof(confirmed_context, monkeypatch, v2):
    client, model, application, original_payload, factory = confirmed_context
    payload = dict(original_payload, idempotency_key=f"source-drift-attempt-{v2}")
    if v2:
        payload["readiness_feedback_version_ids"] = []
    complete = model.complete

    def change_source(messages, tools):
        response = complete(messages, tools)
        with factory() as session:
            app = session.get(Application, application["id"])
            app.source = "crawler"
            session.commit()
        return response

    monkeypatch.setattr(model, "complete", change_source)
    calls = model.calls
    try:
        response = client.post(
            f"/api/applications/{application['id']}/interview-preparation-proposals",
            json=payload,
        )
        assert response.status_code == (409 if v2 else 404), response.text
        assert model.calls == calls + 1
        with factory() as session:
            row = session.scalar(
                select(InterviewPreparationProposal).where(
                    InterviewPreparationProposal.idempotency_key == payload["idempotency_key"]
                )
            )
            assert row is None or row.attempt_status != "ready"
    finally:
        with factory() as session:
            app = session.get(Application, application["id"])
            app.source = "ai"
            session.commit()


@pytest.mark.parametrize("v2", [False, True])
def test_ready_replay_rechecks_source_but_history_remains_readable(confirmed_context, v2):
    client, model, application, original_payload, factory = confirmed_context
    payload = dict(original_payload, idempotency_key=f"ready-source-attempt-{v2}")
    if v2:
        payload["readiness_feedback_version_ids"] = []
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    created = client.post(url, json=payload)
    assert created.status_code == 201, created.text
    calls = model.calls
    with factory() as session:
        app = session.get(Application, application["id"])
        app.source = "crawler"
        session.commit()
    try:
        replay = client.post(url, json=payload)
        assert replay.status_code == 404, replay.text
        assert model.calls == calls
        history = client.get(url)
        assert history.status_code == 200, history.text
    finally:
        with factory() as session:
            app = session.get(Application, application["id"])
            app.source = "ai"
            session.commit()
