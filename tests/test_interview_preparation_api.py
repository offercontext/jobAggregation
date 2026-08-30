from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from offerpilot.ai.interview_preparation_proposals import safe_empty_interview_preparation_proposal
from offerpilot.ai.types import Assistant
from offerpilot.api import (
    _decode_interview_preparation_request,
    _interview_preparation_diagnostic_message,
    create_app,
)
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    ApplicationEvent,
    InterviewPreparationProposal,
    InterviewReadinessSignal,
)

from tests.review_readiness_support import seed_review_candidate
from tests.test_review_readiness_projection import _commit_signal


class FakeModel:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Assistant(
            content=json.dumps(safe_empty_interview_preparation_proposal(), ensure_ascii=False)
        )


class BlockingFakeModel(FakeModel):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return Assistant(
            content=json.dumps(safe_empty_interview_preparation_proposal(), ensure_ascii=False)
        )


def _context(client: TestClient) -> tuple[dict, dict, dict]:
    application = client.post(
        "/api/applications", json={"company_name": "Acme", "position_name": "Backend"}
    ).json()
    resume = client.post(
        "/api/resumes",
        json={
            "title": "Backend Resume",
            "text": "Built reliable API services",
            "content_json": {"raw_text": "Built reliable API services"},
        },
    ).json()
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application["id"],
            "event_type": "interview",
            "scheduled_at": "2026-07-24T10:00:00Z",
            "duration_minutes": 45,
        },
    ).json()
    jd = client.post(
        f"/api/applications/{application['id']}/job-description/versions",
        json={
            "jd_text": "Build reliable services with Python.",
            "source_url": None,
            "expected_current_version_id": None,
            "idempotency_key": "interview-jd-key-0001",
        },
    ).json()
    application["jd_version_id"] = jd["id"]
    return application, resume, event


def _payload(resume_id: int, event_id: int, key: str = "attempt-00000001", jd_version_id: int = 1) -> dict:
    return {
        "event_id": event_id,
        "resume_id": resume_id,
        "jd_version_id": jd_version_id,
        "knowledge_selections": [],
        "user_assertions": ["I led a migration."],
        "idempotency_key": key,
    }


def _readiness_context(tmp_path, *, focus_count: int = 1, model=None):  # type: ignore[no-untyped-def]
    selected_model = model or FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=selected_model))
    factory = session_factory_for_data_dir(tmp_path)
    focuses = [
        {
            "id": f"focus-{index}",
            "text": f"准备重点 {index}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for index in range(1, focus_count + 1)
    ]
    seeded = seed_review_candidate(
        factory,
        focus_id="focus-1",
        practice_focuses=focuses,
    )
    version_ids = tuple(
        _commit_signal(
            factory,
            seeded,
            focus_id=focus["id"],
            idempotency_key=str(uuid4()),
            user_note=f"用户备注 {focus['id']}",
        )[1]
        for focus in focuses
    )
    resume = client.post(
        "/api/resumes",
        json={
            "title": "Preparation Resume",
            "text": "Built reliable API services",
            "content_json": {"raw_text": "Built reliable API services"},
        },
    ).json()
    target = client.post(
        "/api/application-events",
        json={
            "application_id": seeded["application_id"],
            "event_type": "interview",
            "subtype": "system_design",
            "round": 3,
            "scheduled_at": "2031-01-02T09:00:00Z",
            "duration_minutes": 60,
        },
    ).json()
    jd = client.post(
        f"/api/applications/{seeded['application_id']}/job-description/versions",
        json={
            "jd_text": "Build reliable services with Python.",
            "source_url": None,
            "expected_current_version_id": None,
            "idempotency_key": f"readiness-jd-{uuid4()}",
        },
    ).json()
    return client, factory, selected_model, seeded, version_ids, target, resume, jd


def test_preparation_diagnostic_log_is_redacted_and_keeps_failure_categories() -> None:
    message = _interview_preparation_diagnostic_message(
        {
            "failure_category": "invalid_item_shape",
            "failure_categories": ["invalid_item_shape", "unexpected_field", "secret"],
            "repair_attempted": True,
            "retry_count": 1,
            "duration_ms": 3210,
            "provider_request_id_hash": "abc123abc123",
            "structure_summaries": [
                {
                    "payload_type": "object",
                    "top_level_keys": ["preparation_directions"],
                    "fields": {
                        "preparation_directions": {
                            "type": "array",
                            "length": 1,
                            "item_key_sets": [["text"]],
                        }
                    },
                }
            ],
            "provider_request_id": "provider-request-secret",
        }
    )

    assert "failure_categories=[\"invalid_item_shape\",\"unexpected_field\"]" in message
    assert "structure_summaries=" in message
    assert '"candidate secret"' not in message
    assert "provider_request_id_hash=abc123abc123" in message
    assert "provider_request_secret" not in message
    assert "provider_request_id=provider-request-secret" not in message

    direct_empty = _interview_preparation_diagnostic_message(
        {
            "failure_category": None,
            "failure_categories": [],
            "repair_attempted": False,
            "retry_count": 0,
            "duration_ms": 12,
            "provider_request_id_hash": "",
        }
    )
    assert "category=none failure_categories=[]" in direct_empty


def test_missing_required_input_returns_422_without_provider_call(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)

    missing_jd = _payload(resume["id"], event["id"])
    missing_jd.pop("jd_version_id")
    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=missing_jd,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "application_jd_version_required"
    assert model.calls == 0


def test_invalid_idempotency_key_returns_422_without_provider_call(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    payload = _payload(resume["id"], event["id"], "too-short")
    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_invalid_request"
    assert model.calls == 0


def test_input_limits_return_422_without_provider_call(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    payload = _payload(resume["id"], event["id"])
    payload["user_assertions"] = [f"assertion-{index}" for index in range(11)]
    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_input_too_large"
    assert model.calls == 0


def test_unknown_request_fields_and_forged_source_fields_are_rejected(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    payload = _payload(resume["id"], event["id"])
    payload["job_url"] = "https://jobs.example.invalid/should-not-fetch"
    payload["current_jd_hash"] = "forged"

    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_invalid_request"
    assert model.calls == 0


def test_forged_knowledge_selection_is_rejected_before_provider_call(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    payload = _payload(resume["id"], event["id"])
    payload["knowledge_selections"] = [{"note_version_id": 999, "evidence_ids": ["ev-forged"]}]

    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_knowledge_selection_invalid"
    assert model.calls == 0


def test_create_returns_201_then_same_key_returns_200_without_provider_resolution(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"

    first = client.post(url, json=_payload(resume["id"], event["id"]))
    assert first.status_code == 201
    assert first.json()["source_states"]["jd"] == "current"
    assert first.json()["proposal_status"] == "safe_empty"

    replay = client.post(url, json=_payload(resume["id"], event["id"]))
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert model.calls == 1


def test_same_key_during_live_lease_returns_pending_without_second_provider_call(tmp_path) -> None:
    model = BlockingFakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    payload = _payload(resume["id"], event["id"], "live-lease-key-01")

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(client.post, url, json=payload)
        assert model.entered.wait(5)
        pending = client.post(url, json=payload)
        model.release.set()
        first = first_future.result(timeout=5)

    assert pending.status_code == 202
    assert pending.json()["attempt_status"] == "generating"
    assert first.status_code == 201
    assert model.calls == 1


def test_source_cas_rejects_jd_change_after_provider_claim(tmp_path) -> None:
    model = BlockingFakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    payload = _payload(resume["id"], event["id"], "source-barrier-key-01", application["jd_version_id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, url, json=payload)
        assert model.entered.wait(5)
        changed = client.post(
            f"/api/applications/{application['id']}/job-description/versions",
            json={
                "jd_text": "Build reliable services with Rust.",
                "source_url": None,
                "expected_current_version_id": application["jd_version_id"],
                "idempotency_key": "interview-barrier-jd-0002",
            },
        )
        assert changed.status_code == 201
        model.release.set()
        result = future.result(timeout=5)

    assert result.status_code == 409
    assert result.json()["error_code"] == "interview_preparation_source_conflict"
    assert model.calls == 1


def test_same_key_provider_unknown_returns_202_without_second_provider_call(tmp_path) -> None:
    model = FakeModel(error=TimeoutError("provider secret"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"

    first = client.post(url, json=_payload(resume["id"], event["id"], "unknown-000000001"))
    second = client.post(url, json=_payload(resume["id"], event["id"], "unknown-000000001"))

    assert first.status_code == 502
    assert first.json()["error_code"] == "interview_preparation_provider_error"
    assert second.status_code == 202
    assert second.json()["attempt_status"] == "provider_unknown"
    assert model.calls == 1
    assert "provider secret" not in second.text


def test_history_marks_deleted_event_and_soft_deleted_application_is_404(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path, chat_model=FakeModel()))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    created = client.post(url, json=_payload(resume["id"], event["id"])).json()
    assert client.delete(f"/api/application-events/{event['id']}").status_code == 200

    history = client.get(url)
    assert history.status_code == 200
    assert history.json()[0]["source_states"]["event"] == "source_changed"

    assert client.delete(f"/api/applications/{application['id']}").status_code == 200
    hidden = client.get(url)
    assert hidden.status_code == 404
    assert hidden.json()["error_code"] == "interview_preparation_application_not_found"
    assert created["proposal_status"] == "safe_empty"


def test_unconfigured_provider_has_stable_safe_error(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    application, resume, event = _context(client)
    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=_payload(resume["id"], event["id"]),
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "AI 服务暂不可用，请稍后重试。",
        "error_code": "interview_preparation_provider_error",
    }


def test_explicit_empty_readiness_selection_creates_v2_and_absent_same_key_conflicts(
    tmp_path,
) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    payload = _payload(
        resume["id"],
        event["id"],
        "explicit-empty-api-0001",
        application["jd_version_id"],
    )
    payload["readiness_feedback_version_ids"] = []

    created = client.post(url, json=payload)

    assert created.status_code == 201
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    assert snapshot["input_contract"] == "interview-preparation-input-v2"
    assert snapshot["readiness_feedback_selection"] == {
        "present": True,
        "ordered_version_ids": [],
    }
    assert snapshot["readiness_feedback"] == []

    replay = client.post(url, json=payload)
    assert replay.status_code == 200
    assert replay.content == created.content

    absent = dict(payload)
    absent.pop("readiness_feedback_version_ids")
    conflict = client.post(url, json=absent)
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "interview_preparation_idempotency_conflict"
    assert model.calls == 1


def test_absent_readiness_selection_remains_v1_without_v2_envelope(tmp_path) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        json=_payload(
            resume["id"],
            event["id"],
            "absent-v1-api-000001",
            application["jd_version_id"],
        ),
    )
    assert response.status_code == 201
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    assert "input_contract" not in snapshot
    assert "readiness_feedback_selection" not in snapshot
    assert "readiness_feedback" not in snapshot


@pytest.mark.parametrize(
    "selection_json",
    (
        "null",
        "1",
        "true",
        '"1"',
        "{}",
        "[true]",
        "[1.0]",
        '["1"]',
        "[0]",
        "[-1]",
        "[1,1]",
        "[1,2,3,4,5,6,7,8,9]",
    ),
)
def test_raw_readiness_selection_shape_is_rejected_before_provider(
    tmp_path, selection_json: str
) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    raw = json.dumps(
        _payload(
            resume["id"],
            event["id"],
            "raw-selection-api-001",
            application["jd_version_id"],
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    raw = raw[:-1] + f',"readiness_feedback_version_ids":{selection_json}' + "}"

    response = client.post(
        f"/api/applications/{application['id']}/interview-preparation-proposals",
        content=raw.encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_invalid_request"
    assert model.calls == 0


@pytest.mark.parametrize(
    "raw_body",
    (
        b"[]",
        b"null",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"nested":{"overflow":1e999}}',
        b"{not-json}",
        b"\xff",
    ),
)
def test_raw_non_object_or_non_finite_request_is_rejected_without_provider(
    tmp_path, raw_body: bytes
) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    response = client.post(
        "/api/applications/1/interview-preparation-proposals",
        content=raw_body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_invalid_request"
    assert model.calls == 0


@pytest.mark.parametrize(
    "raw_body",
    (
        ("[" * 2_000 + "0" + "]" * 2_000).encode("ascii"),
        ("{\"deep\":" * 2_000 + "0" + "}" * 2_000).encode("ascii"),
    ),
    ids=("deep-array", "deep-object"),
)
def test_raw_excessive_json_depth_is_safe_422_before_repository_or_provider(
    tmp_path, raw_body: bytes
) -> None:
    decoded = _decode_interview_preparation_request(raw_body)
    assert getattr(decoded, "status_code", None) == 422

    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    response = client.post(
        "/api/applications/1/interview-preparation-proposals",
        content=raw_body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "面试准备请求字段无效。",
        "error_code": "interview_preparation_invalid_request",
    }
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        assert session.scalar(select(InterviewPreparationProposal.id)) is None
    assert model.calls == 0


@pytest.mark.parametrize(
    "raw_body",
    (
        b'{"\\ud800":"value"}',
        b'{"outer":{"\\udfff":"value"}}',
        b'{"outer":"\\ud800"}',
        b'{"outer":["ok",{"inner":"\\udfff"}]}',
    ),
    ids=(
        "top-level-surrogate-key",
        "nested-surrogate-key",
        "top-level-surrogate-value",
        "nested-surrogate-value",
    ),
)
def test_raw_lone_surrogate_key_or_value_is_safe_422_before_repository_or_provider(
    tmp_path, raw_body: bytes
) -> None:
    decoded = _decode_interview_preparation_request(raw_body)
    assert getattr(decoded, "status_code", None) == 422

    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    response = client.post(
        "/api/applications/1/interview-preparation-proposals",
        content=raw_body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "面试准备请求字段无效。",
        "error_code": "interview_preparation_invalid_request",
    }
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        assert session.scalar(select(InterviewPreparationProposal.id)) is None
    assert model.calls == 0


@pytest.mark.parametrize(
    "raw",
    (
        '{"event_id":999999,"resume_id":999999,"jd_version_id":999999,'
        '"knowledge_selections":[],"user_assertions":[],'
        '"idempotency_key":"duplicate-key-api-0001",'
        '"readiness_feedback_version_ids":[],"readiness_feedback_version_ids":[]}',
        '{"event_id":999999,"resume_id":999999,"jd_version_id":999999,'
        '"knowledge_selections":[],"user_assertions":[],"user_assertions":[],'
        '"idempotency_key":"duplicate-key-api-0001"}',
        '{"event_id":999999,"resume_id":999999,"jd_version_id":999999,'
        '"knowledge_selections":[{"note_version_id":1,"note_version_id":2}],'
        '"user_assertions":[],"idempotency_key":"duplicate-key-api-0001"}',
    ),
)
def test_duplicate_any_json_key_is_rejected_before_provider_and_lookup(
    tmp_path, raw: str
) -> None:
    model = FakeModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    response = client.post(
        "/api/applications/999999/interview-preparation-proposals",
        content=raw.encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_preparation_invalid_request"
    assert model.calls == 0


def test_provider_unknown_recovery_keeps_frozen_v2_presence(tmp_path) -> None:
    model = FakeModel(error=TimeoutError("provider secret"))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application, resume, event = _context(client)
    url = f"/api/applications/{application['id']}/interview-preparation-proposals"
    payload = _payload(
        resume["id"],
        event["id"],
        "unknown-v2-presence-001",
        application["jd_version_id"],
    )
    payload["readiness_feedback_version_ids"] = []

    first = client.post(url, json=payload)
    replay = client.post(url, json=payload)
    absent = dict(payload)
    absent.pop("readiness_feedback_version_ids")
    conflict = client.post(url, json=absent)

    assert first.status_code == 502
    assert first.json()["error_code"] == "interview_preparation_provider_error"
    assert replay.status_code == 202
    assert replay.json()["attempt_status"] == "provider_unknown"
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "interview_preparation_idempotency_conflict"
    assert "provider secret" not in first.text + replay.text + conflict.text
    assert model.calls == 1


@pytest.mark.parametrize("focus_count", (1, 2))
def test_explicit_readiness_selection_preserves_order_for_one_and_two_items(
    tmp_path, focus_count: int
) -> None:
    client, factory, model, seeded, version_ids, target, resume, jd = _readiness_context(
        tmp_path,
        focus_count=focus_count,
    )
    selected = list(reversed(version_ids))
    payload = _payload(
        resume["id"],
        target["id"],
        "ordered-selection-api-1",
        jd["id"],
    )
    payload["readiness_feedback_version_ids"] = selected

    response = client.post(
        f"/api/applications/{seeded['application_id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 201
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    assert snapshot["readiness_feedback_selection"]["ordered_version_ids"] == selected
    assert [item["statement"] for item in snapshot["readiness_feedback"]] == [
        f"准备重点 {index}" for index in range(focus_count, 0, -1)
    ]
    assert model.calls == 1


def test_eight_readiness_versions_are_accepted_in_request_order(tmp_path) -> None:
    client, factory, model, seeded, version_ids, target, resume, jd = _readiness_context(
        tmp_path,
        focus_count=8,
    )
    selected = list(reversed(version_ids))
    payload = _payload(
        resume["id"],
        target["id"],
        "eight-selection-api-01",
        jd["id"],
    )
    payload["readiness_feedback_version_ids"] = selected
    response = client.post(
        f"/api/applications/{seeded['application_id']}/interview-preparation-proposals",
        json=payload,
    )
    assert response.status_code == 201
    with factory() as session:
        row = session.scalar(select(InterviewPreparationProposal))
        assert row is not None
        snapshot = json.loads(row.input_snapshot_json)
    assert snapshot["readiness_feedback_selection"]["ordered_version_ids"] == selected
    assert len(snapshot["readiness_feedback"]) == 8
    assert model.calls == 1


@pytest.mark.parametrize(
    "invalid_kind",
    (
        "missing",
        "retracted",
        "source_not_completed",
        "target_completed",
        "target_cancelled",
        "target_unknown",
        "target_wrong_type",
        "source_target_same",
    ),
)
def test_invalid_or_ineligible_readiness_selection_maps_to_safe_422(
    tmp_path, invalid_kind: str
) -> None:
    client, factory, model, seeded, version_ids, target, resume, jd = _readiness_context(
        tmp_path
    )
    selected = list(version_ids)
    if invalid_kind == "missing":
        selected = [999_999]
    elif invalid_kind == "retracted":
        with factory() as session:
            signal = session.scalar(select(InterviewReadinessSignal))
            assert signal is not None
            signal.current_version_id = None
            session.commit()
    elif invalid_kind == "source_not_completed":
        with factory() as session:
            source_row = session.get(ApplicationEvent, seeded["event_id"])
            assert source_row is not None
            source_row.status = "in_progress"
            session.commit()
    elif invalid_kind.startswith("target_"):
        with factory() as session:
            target_row = session.get(ApplicationEvent, target["id"])
            assert target_row is not None
            if invalid_kind == "target_wrong_type":
                target_row.event_type = "written_test"
            else:
                target_row.status = invalid_kind.removeprefix("target_")
            session.commit()
    else:
        target["id"] = seeded["event_id"]
    payload = _payload(
        resume["id"],
        target["id"],
        f"invalid-{invalid_kind}-api-1",
        jd["id"],
    )
    payload["readiness_feedback_version_ids"] = selected

    response = client.post(
        f"/api/applications/{seeded['application_id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": "面试准备输入无法验证。",
        "error_code": (
            "interview_preparation_event_invalid"
            if invalid_kind == "target_wrong_type"
            else "interview_preparation_readiness_selection_invalid"
        ),
    }
    assert model.calls == 0


def test_cross_application_readiness_selection_maps_to_safe_422(tmp_path) -> None:
    client, _factory, model, _seeded, version_ids, _target, _resume, _jd = (
        _readiness_context(tmp_path)
    )
    other_application, other_resume, other_target = _context(client)
    payload = _payload(
        other_resume["id"],
        other_target["id"],
        "cross-application-api-1",
        other_application["jd_version_id"],
    )
    payload["readiness_feedback_version_ids"] = list(version_ids)

    response = client.post(
        f"/api/applications/{other_application['id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == (
        "interview_preparation_readiness_selection_invalid"
    )
    assert model.calls == 0


def test_v2_provider_late_target_drift_is_discarded_and_maps_to_409(tmp_path) -> None:
    factory = session_factory_for_data_dir(tmp_path)
    target_id: list[int] = []

    class TargetDriftModel(FakeModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.calls += 1
            with factory() as session:
                target = session.get(ApplicationEvent, target_id[0])
                assert target is not None
                target.status = "completed"
                session.commit()
            return Assistant(
                content=json.dumps(
                    safe_empty_interview_preparation_proposal(), ensure_ascii=False
                )
            )

    model = TargetDriftModel()
    client, _factory, _model, seeded, version_ids, target, resume, jd = _readiness_context(
        tmp_path,
        model=model,
    )
    target_id.append(target["id"])
    payload = _payload(
        resume["id"],
        target["id"],
        "late-target-drift-api-1",
        jd["id"],
    )
    payload["readiness_feedback_version_ids"] = list(version_ids)

    response = client.post(
        f"/api/applications/{seeded['application_id']}/interview-preparation-proposals",
        json=payload,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "interview_preparation_source_conflict"
    assert model.calls == 1
