from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReviewProposal,
)
from offerpilot.review_readiness.candidates import project_readiness_candidates
from offerpilot.review_readiness.projection import (
    compute_practice_target_fingerprint_v1,
    project_practice_focus,
)
from offerpilot.repositories.adaptive_interview_practice import (
    AdaptivePracticeRepository,
    AdaptivePracticeUnavailable,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text

from tests.review_readiness_support import seed_review_candidate


class ForbiddenModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("adaptive practice must not call a provider")


def _create_signal(client, session_factory, seeded):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        candidate = project_readiness_candidates(
            int(seeded["note_id"]), int(seeded["proposal_id"]), session
        ).candidates[0]
    proposed = client.post(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions",
        content=json.dumps(
            {
                "proposal_id": seeded["proposal_id"],
                "focus_id": seeded["focus_id"],
                "expected_note_revision": seeded["note_revision"],
                "expected_candidate_fingerprint": candidate.candidate_fingerprint,
                "idempotency_key": str(uuid4()),
                "user_note": "练习时先说明约束。",
            },
            ensure_ascii=False,
        ).encode(),
        headers={"content-type": "application/json"},
    )
    assert proposed.status_code == 201
    decided = client.post(
        f"/api/product-actions/{proposed.json()['operation_id']}/decisions",
        content=json.dumps(
            {
                "confirmation_token": proposed.json()["confirmation_token"],
                "decision": "approve",
            }
        ).encode(),
        headers={"content-type": "application/json"},
    )
    assert decided.status_code == 200
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        return signal.current_version_id


def _add_target(session_factory, application_id: int, *, round_: int = 3) -> int:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        target = ApplicationEvent(
            application_id=application_id,
            event_type="interview",
            subtype="system_design",
            round=round_,
            scheduled_at=datetime(2031, 2, round_, 9, tzinfo=timezone.utc),
            duration_minutes=60,
            status="scheduled",
        )
        target.tags = ["onsite", f"round-{round_}"]
        session.add(target)
        session.commit()
        return target.id


def _v2_setup(tmp_path):  # type: ignore[no-untyped-def]
    model = ForbiddenModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(
        session_factory,
        practice_focuses=[
            {
                "id": "focus-1",
                "text": "下次先澄清约束条件，再说明缓存一致性的取舍。",
                "evidence_refs": [
                    {
                        "source": "interview_note",
                        "path": "/difficulty_points",
                        "excerpt": "cache consistency tradeoffs",
                    },
                    {
                        "source": "interview_note",
                        "path": "/self_reflection",
                        "excerpt": "clarify constraints",
                    },
                ],
            }
        ],
    )
    version_id = _create_signal(client, session_factory, seeded)
    target_id = _add_target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        projection = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )
        assert projection.state == "ready"
        assert projection.source is not None and projection.target is not None
        source_fingerprint = projection.source.practice_source_fingerprint
        target_fingerprint = projection.target.practice_target_fingerprint
    body = {
        "readiness_signal_version_id": version_id,
        "target_application_event_id": target_id,
        "expected_source_fingerprint": source_fingerprint,
        "expected_target_fingerprint": target_fingerprint,
        "idempotency_key": str(uuid4()),
    }
    return client, session_factory, model, seeded, body


def _seed_legacy_plan(session_factory, seeded):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        proposal = session.get(InterviewReviewProposal, int(seeded["proposal_id"]))
        note = session.get(InterviewNote, int(seeded["note_id"]))
        assert proposal is not None and note is not None
        source_fingerprint = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal.id,
                    "proposal_hash": proposal.proposal_hash,
                    "focus_id": seeded["focus_id"],
                    "source_path": "/difficulty_points",
                    "source_excerpt": "cache consistency tradeoffs",
                    "source_value_hash": sha256_text(note.difficulty_points),
                    "note_id": note.id,
                    "event_id": seeded["event_id"],
                }
            )
        )
        key = "historical-practice-start"
        input_fingerprint = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal.id,
                    "focus_id": seeded["focus_id"],
                    "expected_source_fingerprint": source_fingerprint,
                }
            )
        )
        plan = AdaptivePracticePlan(
            application_id=int(seeded["application_id"]),
            application_event_id=int(seeded["event_id"]),
            interview_note_id=int(seeded["note_id"]),
            interview_review_proposal_id=int(seeded["proposal_id"]),
            focus_id=str(seeded["focus_id"]),
            start_idempotency_key=key,
            start_input_fingerprint=input_fingerprint,
            source_fingerprint=source_fingerprint,
            source_path="/difficulty_points",
            source_excerpt="cache consistency tradeoffs",
            source_hash=sha256_text(note.difficulty_points),
            drill_kind="difficulty_breakdown",
            title="拆解卡住的关键一步",
            observation="下次先澄清约束条件。",
            reason="冻结的 legacy 理由",
            prompt="冻结的 legacy 练习提示",
        )
        session.add(plan)
        session.commit()
        return plan.id, {
            "proposal_id": proposal.id,
            "focus_id": seeded["focus_id"],
            "expected_source_fingerprint": source_fingerprint,
            "idempotency_key": key,
        }


def test_v2_start_replay_read_completion_and_history_are_provider_free(tmp_path) -> None:
    client, session_factory, model, seeded, body = _v2_setup(tmp_path)

    assert client.get("/api/interview-practice/recommendations").json() == []
    started = client.post("/api/interview-practice/plans", json=body)
    assert started.status_code == 201
    plan = started.json()
    assert plan["origin_contract"] == "confirmed_readiness_signal_v1"
    assert plan["application_event_id"] == seeded["event_id"]
    assert plan["target_application_event_id"] == body["target_application_event_id"]
    assert plan["application_event_id"] != plan["target_application_event_id"]
    assert plan["readiness_signal_version_id"] == body["readiness_signal_version_id"]
    assert plan["source_fingerprint"] == body["expected_source_fingerprint"]
    assert plan["target_fingerprint"] == body["expected_target_fingerprint"]
    changed_input = client.post(
        "/api/interview-practice/plans",
        json={**body, "expected_target_fingerprint": "sha256:" + "f" * 64},
    )
    assert changed_input.status_code == 409
    assert changed_input.json()["error_code"] == "adaptive_practice_idempotency_conflict"

    with session_factory() as session:
        target = session.get(ApplicationEvent, int(body["target_application_event_id"]))
        assert target is not None
        session.delete(target)
        session.commit()
    replay = client.post("/api/interview-practice/plans", json=body)
    assert replay.status_code == 200
    assert replay.json()["id"] == plan["id"]
    assert replay.json()["source_fingerprint"] == plan["source_fingerprint"]
    assert replay.json()["target_fingerprint"] == plan["target_fingerprint"]
    assert replay.json()["target_application_event_id"] is None

    detail = client.get(f"/api/interview-practice/plans/{plan['id']}")
    assert detail.status_code == 200
    assert detail.json()["practice_state"] == "target_missing"
    assert client.get("/api/interview-practice/plans").json()[0]["id"] == plan["id"]

    canonical_completion_key = str(uuid4())
    for invalid_key in (
        "adaptive-practice-complete-not-a-uuid",
        f" {canonical_completion_key}",
        canonical_completion_key.upper(),
    ):
        invalid_v2_completion = client.post(
            f"/api/interview-practice/plans/{plan['id']}/complete",
            json={
                "expected_revision": 1,
                "response_text": "不会被写入。",
                "reflection_text": "",
                "self_assessment": "clearer",
                "idempotency_key": invalid_key,
            },
        )
        assert invalid_v2_completion.status_code == 422, invalid_key
        assert invalid_v2_completion.json()["error_code"] == (
            "adaptive_practice_invalid_payload"
        )
    assert client.get(f"/api/interview-practice/plans/{plan['id']}").json()["revision"] == 1

    completed_body = {
        "expected_revision": 1,
        "response_text": "先说明影响范围，再描述定位过程和恢复结果。",
        "reflection_text": "下一次先给结论。",
        "self_assessment": "clearer",
        "idempotency_key": str(uuid4()),
    }
    completed = client.post(
        f"/api/interview-practice/plans/{plan['id']}/complete",
        json=completed_body,
    )
    completed_replay = client.post(
        f"/api/interview-practice/plans/{plan['id']}/complete",
        json=completed_body,
    )
    assert completed.status_code == 200
    assert completed_replay.status_code == 200
    assert completed_replay.json() == completed.json()
    assert completed.json()["status"] == "completed"
    assert completed.json()["practice_state"] == "target_missing"
    assert model.calls == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"null",
        b"[]",
        b'"not-an-object"',
        b'{"readiness_signal_version_id":1,"readiness_signal_version_id":1,'
        b'"target_application_event_id":2,"expected_source_fingerprint":"sha256:'
        + b"a" * 64
        + b'","expected_target_fingerprint":"sha256:'
        + b"b" * 64
        + b'","idempotency_key":"00000000-0000-4000-8000-000000000001"}',
        b'{"readiness_signal_version_id":NaN,"target_application_event_id":2,'
        b'"expected_source_fingerprint":"sha256:'
        + b"a" * 64
        + b'","expected_target_fingerprint":"sha256:'
        + b"b" * 64
        + b'","idempotency_key":"00000000-0000-4000-8000-000000000001"}',
    ],
)
def test_v2_raw_decoder_rejects_nonobject_duplicate_and_nonfinite_before_repository(
    tmp_path,
    monkeypatch,
    raw: bytes,
) -> None:
    calls = 0

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("repository must not run for malformed JSON")

    monkeypatch.setattr(AdaptivePracticeRepository, "start_v2", forbidden)
    monkeypatch.setattr(AdaptivePracticeRepository, "replay_legacy_start", forbidden)
    model = ForbiddenModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    response = client.post(
        "/api/interview-practice/plans",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "adaptive_practice_invalid_payload"
    assert calls == 0
    assert model.calls == 0


def test_v2_exact_shape_and_scalar_types_are_rejected_before_repository(
    tmp_path,
    monkeypatch,
) -> None:
    calls = 0

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise AssertionError("repository must not run for invalid exact body")

    monkeypatch.setattr(AdaptivePracticeRepository, "start_v2", forbidden)
    monkeypatch.setattr(AdaptivePracticeRepository, "replay_legacy_start", forbidden)
    client = TestClient(create_app(data_dir=tmp_path, chat_model=ForbiddenModel()))
    valid = {
        "readiness_signal_version_id": 1,
        "target_application_event_id": 2,
        "expected_source_fingerprint": "sha256:" + "a" * 64,
        "expected_target_fingerprint": "sha256:" + "b" * 64,
        "idempotency_key": "00000000-0000-4000-8000-000000000001",
    }
    invalid_bodies: list[dict[str, object]] = []
    for field in ("readiness_signal_version_id", "target_application_event_id"):
        for bad in (True, 1.0, "1", 0, -1):
            invalid_bodies.append({**valid, field: bad})
    invalid_bodies.extend(
        [
            {**valid, "extra": "forbidden"},
            {key: value for key, value in valid.items() if key != "expected_target_fingerprint"},
            {**valid, "expected_source_fingerprint": "sha256:" + "A" * 64},
            {**valid, "expected_target_fingerprint": "sha256:short"},
            {**valid, "idempotency_key": "not-a-uuid"},
            {**valid, "idempotency_key": "00000000-0000-4000-8000-000000000001 "},
        ]
    )
    for body in invalid_bodies:
        response = client.post("/api/interview-practice/plans", json=body)
        assert response.status_code == 422, body
        assert response.json()["error_code"] == "adaptive_practice_invalid_payload"
    assert calls == 0


def test_legacy_new_create_is_410_but_historical_replay_read_and_complete_survive(
    tmp_path,
) -> None:
    model = ForbiddenModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    plan_id, replay_body = _seed_legacy_plan(session_factory, seeded)

    replay = client.post("/api/interview-practice/plans", json=replay_body)
    assert replay.status_code == 200
    assert replay.json()["id"] == plan_id
    assert replay.json()["origin_contract"] == "legacy_review_focus_v1"
    assert client.get(f"/api/interview-practice/plans/{plan_id}").status_code == 200

    retired = client.post(
        "/api/interview-practice/plans",
        json={**replay_body, "focus_id": "new-focus", "idempotency_key": "new-legacy-key"},
    )
    assert retired.status_code == 410
    assert retired.json()["error_code"] == "adaptive_practice_v1_retired"
    assert client.get("/api/interview-practice/recommendations").json() == []

    completed = client.post(
        f"/api/interview-practice/plans/{plan_id}/complete",
        json={
            "expected_revision": 1,
            "response_text": "先澄清容量约束，再说明取舍。",
            "reflection_text": "结构更清晰。",
            "self_assessment": "clearer",
            "idempotency_key": "historical-complete-key",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert model.calls == 0


def test_v2_conflicts_are_stable_and_same_signal_can_bind_different_targets(tmp_path) -> None:
    client, session_factory, model, seeded, body = _v2_setup(tmp_path)

    stale_source = client.post(
        "/api/interview-practice/plans",
        json={
            **body,
            "expected_source_fingerprint": "sha256:" + "a" * 64,
            "idempotency_key": str(uuid4()),
        },
    )
    assert stale_source.status_code == 409
    assert stale_source.json()["error_code"] == "adaptive_practice_source_conflict"
    stale_target = client.post(
        "/api/interview-practice/plans",
        json={
            **body,
            "expected_target_fingerprint": "sha256:" + "b" * 64,
            "idempotency_key": str(uuid4()),
        },
    )
    assert stale_target.status_code == 409
    assert stale_target.json()["error_code"] == "adaptive_practice_target_conflict"
    missing = client.post(
        "/api/interview-practice/plans",
        json={**body, "readiness_signal_version_id": 2**62, "idempotency_key": str(uuid4())},
    )
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "adaptive_practice_not_found"

    with session_factory() as session:
        source_event = session.get(ApplicationEvent, int(seeded["event_id"]))
        assert source_event is not None
        source_as_target_fingerprint = compute_practice_target_fingerprint_v1(source_event)
    source_as_target = client.post(
        "/api/interview-practice/plans",
        json={
            **body,
            "target_application_event_id": seeded["event_id"],
            "expected_target_fingerprint": source_as_target_fingerprint,
            "idempotency_key": str(uuid4()),
        },
    )
    assert source_as_target.status_code == 409
    assert source_as_target.json()["error_code"] == "adaptive_practice_target_conflict"

    first = client.post("/api/interview-practice/plans", json=body)
    assert first.status_code == 201
    duplicate_pair = client.post(
        "/api/interview-practice/plans",
        json={**body, "idempotency_key": str(uuid4())},
    )
    assert duplicate_pair.status_code == 409
    assert duplicate_pair.json()["error_code"] == "adaptive_practice_pair_conflict"
    completed_first = client.post(
        f"/api/interview-practice/plans/{first.json()['id']}/complete",
        json={
            "expected_revision": 1,
            "response_text": "先澄清约束，再说明取舍。",
            "reflection_text": "结构更清晰。",
            "self_assessment": "clearer",
            "idempotency_key": str(uuid4()),
        },
    )
    assert completed_first.status_code == 200
    assert completed_first.json()["practice_state"] == "completed"

    second_target = _add_target(session_factory, int(seeded["application_id"]), round_=4)
    with session_factory() as session:
        projection = project_practice_focus(
            session,
            signal_version_id=int(body["readiness_signal_version_id"]),
            target_event_id=second_target,
        )
        assert projection.target is not None
        second_target_fingerprint = projection.target.practice_target_fingerprint
    second = client.post(
        "/api/interview-practice/plans",
        json={
            **body,
            "target_application_event_id": second_target,
            "expected_target_fingerprint": second_target_fingerprint,
            "idempotency_key": str(uuid4()),
        },
    )
    assert second.status_code == 201
    assert second.json()["target_application_event_id"] == second_target
    assert model.calls == 0


def test_v2_cross_application_target_is_indistinguishable_from_missing(tmp_path) -> None:
    client, session_factory, model, seeded, body = _v2_setup(tmp_path)
    with session_factory() as session:
        other_application = Application(
            company_name="Other",
            position_name="Backend",
            source="manual",
        )
        session.add(other_application)
        session.commit()
        other_application_id = other_application.id
    cross_target_id = _add_target(session_factory, other_application_id)
    common = {
        **body,
        "expected_target_fingerprint": "sha256:" + "e" * 64,
    }
    cross_scope = client.post(
        "/api/interview-practice/plans",
        json={
            **common,
            "target_application_event_id": cross_target_id,
            "idempotency_key": str(uuid4()),
        },
    )
    nonexistent = client.post(
        "/api/interview-practice/plans",
        json={
            **common,
            "target_application_event_id": 2**62,
            "idempotency_key": str(uuid4()),
        },
    )
    assert cross_scope.status_code == nonexistent.status_code == 404
    assert cross_scope.content == nonexistent.content
    assert cross_scope.json()["error_code"] == "adaptive_practice_not_found"

    source_as_target = client.post(
        "/api/interview-practice/plans",
        json={
            **common,
            "target_application_event_id": seeded["event_id"],
            "idempotency_key": str(uuid4()),
        },
    )
    lifecycle_target_id = _add_target(
        session_factory,
        int(seeded["application_id"]),
        round_=4,
    )
    with session_factory() as session:
        lifecycle_target = session.get(ApplicationEvent, lifecycle_target_id)
        assert lifecycle_target is not None
        lifecycle_target.status = "completed"
        session.commit()
        lifecycle_fingerprint = compute_practice_target_fingerprint_v1(lifecycle_target)
    lifecycle_ineligible = client.post(
        "/api/interview-practice/plans",
        json={
            **common,
            "target_application_event_id": lifecycle_target_id,
            "expected_target_fingerprint": lifecycle_fingerprint,
            "idempotency_key": str(uuid4()),
        },
    )
    assert source_as_target.status_code == lifecycle_ineligible.status_code == 409
    assert source_as_target.json()["error_code"] == "adaptive_practice_target_conflict"
    assert lifecycle_ineligible.json()["error_code"] == "adaptive_practice_target_conflict"
    with session_factory() as session:
        assert session.scalar(select(AdaptivePracticePlan)) is None
    assert model.calls == 0


@pytest.mark.parametrize("field", ["response_text", "reflection_text"])
def test_v2_completion_rejects_escaped_lone_surrogate_without_writes(
    tmp_path,
    field: str,
) -> None:
    client, session_factory, model, _seeded, body = _v2_setup(tmp_path)
    started = client.post("/api/interview-practice/plans", json=body)
    assert started.status_code == 201
    raw_fields = {
        "expected_revision": "1",
        "response_text": '"valid response"',
        "reflection_text": '"valid reflection"',
        "self_assessment": '"clearer"',
        "idempotency_key": f'"{uuid4()}"',
    }
    raw_fields[field] = '"\\ud800"'
    raw = ("{" + ",".join(f'"{key}":{value}' for key, value in raw_fields.items()) + "}").encode()
    rejected = client.post(
        f"/api/interview-practice/plans/{started.json()['id']}/complete",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "adaptive_practice_invalid_payload"
    with session_factory() as session:
        plan = session.get(AdaptivePracticePlan, started.json()["id"])
        assert plan is not None
        assert plan.status == "in_progress"
        assert plan.revision == 1
        assert plan.response_text == plan.reflection_text == ""
        assert plan.self_assessment == ""
        assert plan.completion_idempotency_key is None
        assert plan.completion_fingerprint == ""
    assert model.calls == 0


def test_v2_unavailable_is_a_retryable_503_without_internal_detail(tmp_path, monkeypatch) -> None:
    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AdaptivePracticeUnavailable("sensitive internal database detail")

    monkeypatch.setattr(AdaptivePracticeRepository, "start_v2", unavailable)
    client = TestClient(create_app(data_dir=tmp_path, chat_model=ForbiddenModel()))
    response = client.post(
        "/api/interview-practice/plans",
        json={
            "readiness_signal_version_id": 1,
            "target_application_event_id": 2,
            "expected_source_fingerprint": "sha256:" + "a" * 64,
            "expected_target_fingerprint": "sha256:" + "b" * 64,
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
        },
    )
    assert response.status_code == 503
    assert response.json() == {
        "error_code": "adaptive_practice_unavailable",
        "error": "练习来源暂时不可用，请稍后重试。",
    }


@pytest.mark.parametrize(
    ("repository_method", "http_method", "path", "payload"),
    [
        ("list_plans", "GET", "/api/interview-practice/plans", None),
        ("get", "GET", "/api/interview-practice/plans/1", None),
        (
            "complete",
            "POST",
            "/api/interview-practice/plans/1/complete",
            {
                "expected_revision": 1,
                "response_text": "回答",
                "reflection_text": "",
                "self_assessment": "clearer",
                "idempotency_key": "00000000-0000-4000-8000-000000000001",
            },
        ),
    ],
)
def test_adaptive_practice_read_and_complete_storage_failures_are_stable_503(
    tmp_path,
    monkeypatch,
    repository_method: str,
    http_method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AdaptivePracticeUnavailable("sensitive database detail")

    monkeypatch.setattr(AdaptivePracticeRepository, repository_method, unavailable)
    client = TestClient(create_app(data_dir=tmp_path, chat_model=ForbiddenModel()))
    request_kwargs = {"json": payload} if payload is not None else {}
    response = client.request(http_method, path, **request_kwargs)
    assert response.status_code == 503
    assert response.json() == {
        "error_code": "adaptive_practice_unavailable",
        "error": "练习来源暂时不可用，请稍后重试。",
    }
