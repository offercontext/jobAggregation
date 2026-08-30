from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Event, Thread
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    InterviewReviewProposal,
)
from offerpilot.review_readiness.candidates import project_readiness_candidates
from offerpilot.review_readiness.projection import (
    compute_practice_target_fingerprint_v1,
    load_canonical_readiness_signal,
)
from offerpilot.review_readiness import repository as readiness_repository_module
from offerpilot.repositories.json_contract import canonical_json, sha256_text

from tests.review_readiness_support import seed_review_candidate


def _create_readiness_signal(client, session_factory, seeded):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
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
                "user_note": "下次先给结论。",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
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
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert decided.status_code == 200
    with session_factory() as session:
        signal = session.scalar(
            select(InterviewReadinessSignal).where(
                InterviewReadinessSignal.source_proposal_id == seeded["proposal_id"]
            )
        )
        assert signal is not None and signal.current_version_id is not None
        return signal.id, signal.current_version_id


def _create_target(session_factory, application_id: int, *, status: str = "todo") -> int:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        target = ApplicationEvent(
            application_id=application_id,
            event_type="interview",
            subtype="system_design",
            round=3,
            scheduled_at=datetime(2031, 2, 3, 9, tzinfo=timezone.utc),
            duration_minutes=60,
            status=status,
        )
        target.tags = ["onsite"]
        session.add(target)
        session.commit()
        return target.id


def _practice_plan(
    seeded,
    *,
    origin_contract: str,
    status: str,
    version_id: int | None = None,
    target_id: int | None = None,
    source_fingerprint: str = "sha256:" + "a" * 64,
    target_fingerprint: str | None = None,
    source_path: str = "/difficulty_points",
    source_excerpt: str = "safe excerpt",
    source_hash: str = "sha256:" + "c" * 64,
):  # type: ignore[no-untyped-def]
    start_idempotency_key = str(uuid4())
    start_input_fingerprint = "sha256:" + "b" * 64
    if origin_contract == "confirmed_readiness_signal_v1":
        assert version_id is not None and target_id is not None
        assert target_fingerprint is not None
        start_input_fingerprint = "sha256:" + sha256_text(
            canonical_json(
                {
                    "idempotency_key": start_idempotency_key,
                    "readiness_signal_version_id": version_id,
                    "expected_source_fingerprint": source_fingerprint,
                    "target_application_event_id": target_id,
                    "expected_target_fingerprint": target_fingerprint,
                }
            )
        )
    return AdaptivePracticePlan(
        application_id=int(seeded["application_id"]),
        application_event_id=int(seeded["event_id"]),
        interview_note_id=int(seeded["note_id"]),
        interview_review_proposal_id=int(seeded["proposal_id"]),
        focus_id=str(seeded["focus_id"]),
        start_idempotency_key=start_idempotency_key,
        start_input_fingerprint=start_input_fingerprint,
        source_fingerprint=source_fingerprint,
        source_path=source_path,
        source_excerpt=source_excerpt,
        source_hash=source_hash,
        drill_kind="explain",
        title="Practice",
        observation="Observation",
        reason="Reason",
        prompt="Prompt",
        status=status,
        revision=1,
        origin_contract=origin_contract,
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        target_fingerprint=target_fingerprint,
    )


def test_readiness_signal_product_action_api_and_safe_generic_get(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    client = TestClient(app)
    proposal_body = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "55555555-5555-4555-8555-555555555555",
        "user_note": "",
    }

    proposed = client.post(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions",
        content=json.dumps(proposal_body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert proposed.status_code == 201
    operation_id = proposed.json()["operation_id"]
    token = proposed.json()["confirmation_token"]

    generic = client.get(f"/api/product-actions/{operation_id}")
    assert generic.status_code == 200
    assert generic.json()["status"] == "proposed"
    assert "confirmation_token" not in generic.json()

    owner_recovery = client.get(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions/{operation_id}"
    )
    assert owner_recovery.status_code == 200
    assert owner_recovery.json()["confirmation_token"] == token
    assert owner_recovery.json()["allowed_decisions"] == [
        "approve",
        "modify",
        "reject",
    ]
    rejection_recovery = client.get(
        f"/api/applications/{seeded['application_id']}/product-actions/"
        f"{operation_id}/rejection-control"
    )
    assert rejection_recovery.status_code == 200
    assert rejection_recovery.json()["confirmation_token"] != token
    assert rejection_recovery.json()["allowed_decisions"] == ["reject"]
    assert rejection_recovery.json()["live_source_state"] == "not_observed"

    decision = client.post(
        f"/api/product-actions/{operation_id}/decisions",
        content=json.dumps(
            {"confirmation_token": token, "decision": "approve"}
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "committed"
    assert client.get(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions/{operation_id}"
    ).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        b'{"proposal_id":true,"focus_id":"f","expected_note_revision":1,'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":1.0,"focus_id":"f","expected_note_revision":1,'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":"1","focus_id":"f","expected_note_revision":1,'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":1,"focus_id":"f","expected_note_revision":true,'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":1,"focus_id":"f","expected_note_revision":1.0,'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":1,"focus_id":"f","expected_note_revision":"1",'
        b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
        + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
        b'"user_note":""}',
        b'{"proposal_id":1,"proposal_id":1}',
    ],
)
def test_product_action_raw_decoder_rejects_nonexact_int_and_duplicate_before_sql(
    tmp_path,
    body,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        response = TestClient(app).post(
            "/api/interview-notes/1/readiness-focus-actions",
            content=body,
            headers={"content-type": "application/json"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert response.status_code == 422
    assert response.json()["error_code"] == "product_action_invalid_request"
    assert statements == []


def test_product_action_decision_duplicate_key_is_rejected_before_sql(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        response = TestClient(app).post(
            "/api/product-actions/11111111-1111-4111-8111-111111111111/decisions",
            content=(
                b'{"confirmation_token":"'
                + b"0" * 64
                + b'","decision":"approve","decision":"reject"}'
            ),
            headers={"content-type": "application/json"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert response.status_code == 422
    assert response.json()["error_code"] == "product_action_invalid_request"
    assert statements == []


def test_product_action_proposal_missing_and_cross_scope_share_safe_404(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    first = seed_review_candidate(session_factory)
    second = seed_review_candidate(session_factory, focus_id="focus-cross-scope")
    with session_factory() as session:
        candidate = project_readiness_candidates(
            second["note_id"], second["proposal_id"], session
        ).candidates[0]
    body = {
        "proposal_id": second["proposal_id"],
        "focus_id": second["focus_id"],
        "expected_note_revision": second["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "89898989-8989-4989-8989-898989898989",
        "user_note": "",
    }
    client = TestClient(app)

    missing = client.post(
        "/api/interview-notes/999999/readiness-focus-actions",
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    cross_scope = client.post(
        f"/api/interview-notes/{first['note_id']}/readiness-focus-actions",
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert missing.status_code == cross_scope.status_code == 404
    assert missing.json() == cross_scope.json() == {
        "error_code": "review_readiness_not_found",
        "retryable": False,
    }


def test_product_action_recovery_missing_identity_is_safe_404(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    operation_id = "91919191-9191-4191-8191-919191919191"

    owner = client.get(
        f"/api/interview-notes/1/readiness-focus-actions/{operation_id}"
    )
    rejection = client.get(
        f"/api/applications/1/product-actions/{operation_id}/rejection-control"
    )

    assert owner.status_code == rejection.status_code == 404
    assert owner.json() == rejection.json() == {
        "error_code": "review_readiness_not_found",
        "retryable": False,
    }


def test_product_action_owner_and_rejection_recovery_cross_scope_are_safe_404(
    tmp_path,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    owned = seed_review_candidate(session_factory, focus_id="focus-owned")
    other = seed_review_candidate(session_factory, focus_id="focus-other")
    with session_factory() as session:
        candidate = project_readiness_candidates(
            owned["note_id"], owned["proposal_id"], session
        ).candidates[0]
    client = TestClient(app)
    proposed = client.post(
        f"/api/interview-notes/{owned['note_id']}/readiness-focus-actions",
        content=json.dumps(
            {
                "proposal_id": owned["proposal_id"],
                "focus_id": owned["focus_id"],
                "expected_note_revision": owned["note_revision"],
                "expected_candidate_fingerprint": candidate.candidate_fingerprint,
                "idempotency_key": "92929292-9292-4292-8292-929292929292",
                "user_note": "",
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert proposed.status_code == 201
    operation_id = proposed.json()["operation_id"]

    owner = client.get(
        f"/api/interview-notes/{other['note_id']}/readiness-focus-actions/"
        f"{operation_id}"
    )
    rejection = client.get(
        f"/api/applications/{other['application_id']}/product-actions/"
        f"{operation_id}/rejection-control"
    )

    assert owner.status_code == rejection.status_code == 404
    assert owner.json() == rejection.json() == {
        "error_code": "review_readiness_not_found",
        "retryable": False,
    }


def test_review_readiness_read_routes_have_one_exact_manifest_entry(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    manifest = [
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if "readiness-feedback" in route.path
        or "readiness-signals" in route.path
        or route.path == "/api/interview-practice/focus/{signal_version_id}"
    ]
    assert sorted(manifest) == sorted(
        [
            (
                "/api/interview-notes/{note_id}/readiness-feedback-candidates",
                "GET",
            ),
            (
                "/api/applications/{application_id}/events/{event_id}/readiness-feedback",
                "GET",
            ),
            (
                "/api/applications/{application_id}/readiness-signals/{signal_id}",
                "GET",
            ),
            ("/api/interview-practice/focus/{signal_version_id}", "GET"),
        ]
    )


def test_candidate_read_api_returns_bounded_exact_v2_projection(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)

    response = TestClient(app).get(
        f"/api/interview-notes/{seeded['note_id']}/readiness-feedback-candidates",
        params={"proposal_id": seeded["proposal_id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"schema_version", "state", "note_id", "proposal_id", "candidates"}
    assert body["schema_version"] == 1
    assert body["state"] == "ready"
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert set(candidate) == {
        "application_id",
        "event_id",
        "note_id",
        "proposal_id",
        "proposal_schema_version",
        "focus_id",
        "statement",
        "source_note_revision",
        "source_note_fingerprint",
        "source_proposal_hash",
        "candidate_fingerprint",
        "evidence",
    }
    assert candidate["proposal_schema_version"] == 2
    assert candidate["statement"]
    assert len(candidate["evidence"]) == 1
    assert set(candidate["evidence"][0]) == {
        "ordinal",
        "source_path",
        "excerpt",
        "excerpt_sha256",
        "source_field_sha256",
    }


def test_candidate_read_api_missing_and_cross_scope_are_same_safe_404(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    first = seed_review_candidate(session_factory, focus_id="first-focus")
    second = seed_review_candidate(session_factory, focus_id="second-focus")
    client = TestClient(app)

    missing = client.get(
        "/api/interview-notes/999999/readiness-feedback-candidates",
        params={"proposal_id": second["proposal_id"]},
    )
    cross_scope = client.get(
        f"/api/interview-notes/{first['note_id']}/readiness-feedback-candidates",
        params={"proposal_id": second["proposal_id"]},
    )

    assert missing.status_code == cross_scope.status_code == 404
    assert missing.json() == cross_scope.json() == {
        "error_code": "review_readiness_not_found",
        "retryable": False,
    }


def test_signal_detail_and_exact_focus_expose_safe_canonical_projection(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))

    detail = client.get(
        f"/api/applications/{seeded['application_id']}/readiness-signals/{signal_id}"
    )
    focus = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )

    assert detail.status_code == focus.status_code == 200
    detail_body = detail.json()
    assert set(detail_body) == {
        "schema_version",
        "signal_id",
        "version_id",
        "application_id",
        "source_event_id",
        "state",
        "focus_id",
        "title",
        "source_label",
        "statement",
        "user_note",
        "practice_source_fingerprint",
        "evidence",
    }
    assert detail_body["state"] == "current"
    assert detail_body["title"] == detail_body["statement"]
    assert detail_body["source_label"] == "第 2 轮面试复盘"
    assert detail_body["practice_source_fingerprint"].startswith("sha256:")
    assert set(focus.json()) == {
        "schema_version",
        "signalId",
        "versionId",
        "targetEventId",
        "practiceSourceFingerprint",
        "practiceTargetFingerprint",
        "state",
        "practiceState",
        "selected",
        "title",
        "sourceLabel",
    }
    assert focus.json()["state"] == "available"
    assert focus.json()["practiceState"] == "not_started"
    assert focus.json()["selected"] is False
    assert focus.json()["targetEventId"] == target_id
    assert focus.json()["practiceTargetFingerprint"].startswith("sha256:")
    serialized = json.dumps(
        {"detail": detail_body, "focus": focus.json()}, ensure_ascii=False
    ).lower()
    for forbidden in (
        "confirmation_token",
        "operation_id",
        "proposal_json",
        "input_snapshot_json",
        "write_operation_id",
    ):
        assert forbidden not in serialized


def test_event_advisory_maps_exact_pair_states_and_ignores_self_assessment(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))

    initial = client.get(
        f"/api/applications/{seeded['application_id']}/events/{target_id}/readiness-feedback"
    )
    assert initial.status_code == 200
    assert initial.json()["items"] == [
        {
            "signalId": signal_id,
            "versionId": version_id,
            "practiceSourceFingerprint": initial.json()["items"][0][
                "practiceSourceFingerprint"
            ],
            "practiceTargetFingerprint": initial.json()["items"][0][
                "practiceTargetFingerprint"
            ],
            "state": "available",
            "practiceState": "not_started",
            "selected": False,
            "title": initial.json()["items"][0]["title"],
            "sourceLabel": "第 2 轮面试复盘",
        }
    ]

    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=version_id)
        target = session.get(ApplicationEvent, target_id)
        assert source.aggregate is not None and target is not None
        primary = source.aggregate.evidence[0]
        target_fingerprint = compute_practice_target_fingerprint_v1(target)
        start_idempotency_key = str(uuid4())
        plan = AdaptivePracticePlan(
            application_id=int(seeded["application_id"]),
            application_event_id=int(seeded["event_id"]),
            interview_note_id=int(seeded["note_id"]),
            interview_review_proposal_id=int(seeded["proposal_id"]),
            focus_id=str(seeded["focus_id"]),
            start_idempotency_key=start_idempotency_key,
            start_input_fingerprint="sha256:" + sha256_text(
                canonical_json(
                    {
                        "idempotency_key": start_idempotency_key,
                        "readiness_signal_version_id": version_id,
                        "expected_source_fingerprint": (
                            source.aggregate.practice_source_fingerprint
                        ),
                        "target_application_event_id": target_id,
                        "expected_target_fingerprint": target_fingerprint,
                    }
                )
            ),
            source_fingerprint=source.aggregate.practice_source_fingerprint,
            source_path=primary.source_path,
            source_excerpt=primary.excerpt,
            source_hash=primary.source_field_sha256,
            drill_kind="explain",
            title="Practice",
            observation="Observation",
            reason="Reason",
            prompt="Prompt",
            status="completed",
            revision=2,
            response_text="Response",
            reflection_text="Reflection",
            self_assessment="unrelated-private-rating",
            completion_idempotency_key=str(uuid4()),
            completion_fingerprint="sha256:" + "c" * 64,
            completed_at=datetime.now(timezone.utc),
            origin_contract="confirmed_readiness_signal_v1",
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            target_fingerprint=target_fingerprint,
        )
        session.add(plan)
        session.commit()

    completed = client.get(
        f"/api/applications/{seeded['application_id']}/events/{target_id}/readiness-feedback"
    )
    assert completed.status_code == 200
    item = completed.json()["items"][0]
    assert item["state"] == "practiced"
    assert item["practiceState"] == "completed"
    assert item["selected"] is True
    assert "self_assessment" not in json.dumps(completed.json())
    assert "unrelated-private-rating" not in json.dumps(completed.json())
    with session_factory() as session:
        stored = session.scalar(
            select(AdaptivePracticePlan).where(
                AdaptivePracticePlan.readiness_signal_version_id == version_id,
                AdaptivePracticePlan.target_application_event_id == target_id,
            )
        )
        assert stored is not None
        stored.self_assessment = "a-different-private-rating"
        session.commit()
    assert client.get(
        f"/api/applications/{seeded['application_id']}/events/{target_id}/readiness-feedback"
    ).json() == completed.json()


def test_event_advisory_distinguishes_legacy_only_and_exact_in_progress(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    url = (
        f"/api/applications/{seeded['application_id']}/events/"
        f"{target_id}/readiness-feedback"
    )

    with session_factory() as session:
        session.add(
            _practice_plan(
                seeded,
                origin_contract="legacy_review_focus_v1",
                status="completed",
            )
        )
        session.commit()
    legacy = client.get(url)
    assert legacy.status_code == 200
    assert legacy.json()["items"][0]["state"] == "available"
    assert legacy.json()["items"][0]["practiceState"] == "legacy_only"
    assert legacy.json()["items"][0]["selected"] is False

    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=version_id)
        target = session.get(ApplicationEvent, target_id)
        assert source.aggregate is not None and target is not None
        primary = source.aggregate.evidence[0]
        session.add(
            _practice_plan(
                seeded,
                origin_contract="confirmed_readiness_signal_v1",
                status="in_progress",
                version_id=version_id,
                target_id=target_id,
                source_fingerprint=source.aggregate.practice_source_fingerprint,
                target_fingerprint=compute_practice_target_fingerprint_v1(target),
                source_path=primary.source_path,
                source_excerpt=primary.excerpt,
                source_hash=primary.source_field_sha256,
            )
        )
        session.commit()
    exact = client.get(url)
    assert exact.status_code == 200
    assert exact.json()["items"][0]["state"] == "available"
    assert exact.json()["items"][0]["practiceState"] == "in_progress"
    assert exact.json()["items"][0]["selected"] is True


def test_exact_focus_keeps_both_fingerprints_when_source_becomes_stale(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        proposal = session.get(InterviewReviewProposal, int(seeded["proposal_id"]))
        assert proposal is not None
        proposal.proposal_hash = "0" * 64
        session.commit()

    response = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "stale_source"
    assert response.json()["practiceState"] == "not_started"
    assert response.json()["selected"] is False
    assert response.json()["practiceSourceFingerprint"].startswith("sha256:")
    assert response.json()["practiceTargetFingerprint"].startswith("sha256:")


def test_read_owner_and_source_queries_precede_any_target_query(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    client = TestClient(app)
    statements: list[str] = []
    engine = app.state.db_engine

    def observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        advisory = client.get(
            f"/api/applications/999999/events/{target_id}/readiness-feedback"
        )
        advisory_statements = tuple(statements)
        statements.clear()
        focus = client.get(
            "/api/interview-practice/focus/999999",
            params={"target_event_id": target_id},
        )
        focus_statements = tuple(statements)
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert advisory.status_code == focus.status_code == 404
    assert any("from applications" in statement for statement in advisory_statements)
    assert not any("application_events" in statement for statement in advisory_statements)
    assert any(
        "interview_readiness_signal_versions" in statement
        for statement in focus_statements
    )
    assert not any("application_events" in statement for statement in focus_statements)


def test_event_advisory_uses_one_real_sqlite_read_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    with session_factory.kw["bind"].connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"
    entered = Event()
    release = Event()
    transaction_states: list[bool] = []
    responses: list[object] = []
    original = readiness_repository_module.project_practice_focus

    def pause_after_target_snapshot(session, **kwargs):  # type: ignore[no-untyped-def]
        driver_connection = session.connection().connection.driver_connection
        transaction_states.append(bool(driver_connection.in_transaction))
        entered.set()
        assert release.wait(10)
        return original(session, **kwargs)

    monkeypatch.setattr(
        readiness_repository_module,
        "project_practice_focus",
        pause_after_target_snapshot,
    )
    url = (
        f"/api/applications/{seeded['application_id']}/events/"
        f"{target_id}/readiness-feedback"
    )
    reader = Thread(target=lambda: responses.append(client.get(url)), daemon=True)
    reader.start()
    assert entered.wait(10)
    with session_factory() as writer:
        note = writer.get(InterviewNote, int(seeded["note_id"]))
        assert note is not None
        note.content_revision += 1
        writer.commit()
    release.set()
    reader.join(10)
    assert not reader.is_alive()
    assert transaction_states == [True]
    assert len(responses) == 1
    frozen_response = responses[0]
    assert getattr(frozen_response, "status_code") == 200
    assert frozen_response.json()["items"][0]["state"] == "available"  # type: ignore[union-attr]

    current = client.get(url)
    assert current.status_code == 200
    assert current.json()["items"][0]["state"] == "stale_source"


@pytest.mark.parametrize(
    "malformation",
    [
        "application_id",
        "application_event_id",
        "interview_note_id",
        "interview_review_proposal_id",
        "focus_id",
        "fake_completed",
    ],
)
def test_exact_v2_plan_requires_full_signal_owner_and_closed_shape(
    tmp_path,
    malformation: str,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=version_id)
        target = session.get(ApplicationEvent, target_id)
        assert source.aggregate is not None and target is not None
        primary = source.aggregate.evidence[0]
        status = "completed" if malformation == "fake_completed" else "in_progress"
        plan = _practice_plan(
            seeded,
            origin_contract="confirmed_readiness_signal_v1",
            status=status,
            version_id=version_id,
            target_id=target_id,
            source_fingerprint=source.aggregate.practice_source_fingerprint,
            target_fingerprint=compute_practice_target_fingerprint_v1(target),
            source_path=primary.source_path,
            source_excerpt=primary.excerpt,
            source_hash=primary.source_field_sha256,
        )
        if malformation == "application_id":
            other = Application(company_name="Other", position_name="Other", source="manual")
            session.add(other)
            session.flush()
            plan.application_id = other.id
        elif malformation == "application_event_id":
            plan.application_event_id = target_id
        elif malformation == "interview_note_id":
            plan.interview_note_id = int(seeded["note_id"]) + 999_000
        elif malformation == "interview_review_proposal_id":
            plan.interview_review_proposal_id = int(seeded["proposal_id"]) + 999_000
        elif malformation == "focus_id":
            plan.focus_id = "another-focus"
        session.add(plan)
        session.commit()

    focus_response = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )
    advisory_response = client.get(
        f"/api/applications/{seeded['application_id']}/events/"
        f"{target_id}/readiness-feedback"
    )
    assert focus_response.status_code == advisory_response.status_code == 503
    assert focus_response.json() == advisory_response.json() == {
        "error_code": "review_readiness_unavailable",
        "retryable": True,
    }


def test_both_v2_locator_columns_swapped_are_unavailable_over_http(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with Session(connection) as session:
            source = load_canonical_readiness_signal(
                session,
                signal_version_id=version_id,
            )
            target = session.get(ApplicationEvent, target_id)
            assert source.aggregate is not None and target is not None
            primary = source.aggregate.evidence[0]
            session.add(
                _practice_plan(
                    seeded,
                    origin_contract="confirmed_readiness_signal_v1",
                    status="in_progress",
                    version_id=target_id,
                    target_id=version_id,
                    source_fingerprint=source.aggregate.practice_source_fingerprint,
                    target_fingerprint=compute_practice_target_fingerprint_v1(target),
                    source_path=primary.source_path,
                    source_excerpt=primary.excerpt,
                    source_hash=primary.source_field_sha256,
                )
            )
            session.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()

    focus = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )
    advisory = client.get(
        f"/api/applications/{seeded['application_id']}/events/"
        f"{target_id}/readiness-feedback"
    )
    assert focus.status_code == advisory.status_code == 503
    assert focus.json() == advisory.json() == {
        "error_code": "review_readiness_unavailable",
        "retryable": True,
    }


def test_deleted_historical_target_does_not_poison_a_new_exact_pair_over_http(
    tmp_path,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    _signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    deleted_target_id = _create_target(
        session_factory,
        int(seeded["application_id"]),
    )
    current_target_id = _create_target(
        session_factory,
        int(seeded["application_id"]),
    )
    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=version_id)
        deleted_target = session.get(ApplicationEvent, deleted_target_id)
        assert source.aggregate is not None and deleted_target is not None
        primary = source.aggregate.evidence[0]
        plan = _practice_plan(
            seeded,
            origin_contract="confirmed_readiness_signal_v1",
            status="completed",
            version_id=version_id,
            target_id=deleted_target_id,
            source_fingerprint=source.aggregate.practice_source_fingerprint,
            target_fingerprint=compute_practice_target_fingerprint_v1(deleted_target),
            source_path=primary.source_path,
            source_excerpt=primary.excerpt,
            source_hash=primary.source_field_sha256,
        )
        plan.revision = 2
        plan.response_text = "Response"
        plan.reflection_text = "Reflection"
        plan.completion_idempotency_key = str(uuid4())
        plan.completion_fingerprint = "sha256:" + "e" * 64
        plan.completed_at = datetime.now(timezone.utc)
        session.add(plan)
        session.commit()
        plan_id = plan.id
        frozen_target_fingerprint = plan.target_fingerprint
    with session_factory() as session:
        deleted_target = session.get(ApplicationEvent, deleted_target_id)
        assert deleted_target is not None
        session.delete(deleted_target)
        session.commit()

    focus = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": current_target_id},
    )
    advisory = client.get(
        f"/api/applications/{seeded['application_id']}/events/"
        f"{current_target_id}/readiness-feedback"
    )
    assert focus.status_code == advisory.status_code == 200
    assert focus.json()["state"] == advisory.json()["items"][0]["state"] == "available"
    assert focus.json()["practiceState"] == "not_started"
    assert advisory.json()["items"][0]["practiceState"] == "not_started"
    with session_factory() as session:
        stored = session.get(AdaptivePracticePlan, plan_id)
        assert stored is not None
        assert stored.target_application_event_id is None
        assert stored.target_fingerprint == frozen_target_fingerprint
        assert stored.status == "completed"


@pytest.mark.parametrize(
    "corruption_sql",
    [
        "subtype = x'00'",
        "tags = 'not-json'",
        "round = 'invalid'",
        "scheduled_at = 'invalid'",
        "duration_minutes = 'invalid'",
    ],
)
def test_zero_signals_still_validates_the_full_same_scope_target(
    tmp_path,
    corruption_sql: str,
) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        other = Application(company_name="Other", position_name="Other", source="manual")
        session.add(other)
        session.commit()
        other_application_id = other.id
    with session_factory() as session:
        session.execute(
            text(
                f"UPDATE application_events SET {corruption_sql} "
                "WHERE id = :event_id"
            ),
            {"event_id": target_id},
        )
        session.commit()

    client = TestClient(app)
    same_scope = client.get(
        f"/api/applications/{seeded['application_id']}/events/"
        f"{target_id}/readiness-feedback"
    )
    cross_scope = client.get(
        f"/api/applications/{other_application_id}/events/"
        f"{target_id}/readiness-feedback"
    )
    assert same_scope.status_code == 503
    assert same_scope.json() == {
        "error_code": "review_readiness_unavailable",
        "retryable": True,
    }
    assert cross_scope.status_code == 404
    assert cross_scope.json() == {
        "error_code": "review_readiness_not_found",
        "retryable": False,
    }


def test_read_apis_fail_closed_for_missing_cross_scope_and_unreadable(tmp_path) -> None:
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    client = TestClient(app)
    signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    target_id = _create_target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        other = Application(company_name="Other", position_name="Other", source="manual")
        session.add(other)
        session.commit()
        other_application_id = other.id
    cross_target = _create_target(session_factory, other_application_id)
    with session_factory() as session:
        session.execute(
            text("UPDATE application_events SET status = x'00' WHERE id = :event_id"),
            {"event_id": cross_target},
        )
        session.commit()

    responses = (
        client.get("/api/applications/999999/readiness-signals/999999"),
        client.get(
            f"/api/applications/{other_application_id}/readiness-signals/{signal_id}"
        ),
        client.get(
            f"/api/applications/{seeded['application_id']}/events/999999/readiness-feedback"
        ),
        client.get(
            f"/api/applications/{other_application_id}/events/{target_id}/readiness-feedback"
        ),
        client.get(
            f"/api/interview-practice/focus/{version_id}",
            params={"target_event_id": cross_target},
        ),
        client.get(
            f"/api/applications/{seeded['application_id']}/events/"
            f"{cross_target}/readiness-feedback"
        ),
        client.get(
            "/api/interview-practice/focus/999999",
            params={"target_event_id": cross_target},
        ),
    )
    assert all(response.status_code == 404 for response in responses)
    assert {json.dumps(response.json(), sort_keys=True) for response in responses} == {
        json.dumps(
            {"error_code": "review_readiness_not_found", "retryable": False},
            sort_keys=True,
        )
    }

    with session_factory() as session:
        session.execute(
            text("UPDATE application_events SET status = x'00' WHERE id = :event_id"),
            {"event_id": target_id},
        )
        session.commit()
    corrupt_target_advisory = client.get(
        f"/api/applications/{seeded['application_id']}/events/{target_id}/readiness-feedback"
    )
    corrupt_target_focus = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )
    assert corrupt_target_advisory.status_code == corrupt_target_focus.status_code == 503
    with session_factory() as session:
        session.execute(
            text("UPDATE application_events SET status = 'todo' WHERE id = :event_id"),
            {"event_id": target_id},
        )
        session.commit()

    with session_factory() as session:
        evidence_id = session.scalar(
            select(InterviewReadinessSignalEvidence.id).where(
                InterviewReadinessSignalEvidence.signal_version_id == version_id
            )
        )
        assert evidence_id is not None
        session.execute(
            text(
                "UPDATE interview_readiness_signal_evidence "
                "SET excerpt_sha256 = :bad WHERE id = :evidence_id"
            ),
            {"bad": "sha256:" + "0" * 64, "evidence_id": evidence_id},
        )
        session.commit()

    detail = client.get(
        f"/api/applications/{seeded['application_id']}/readiness-signals/{signal_id}"
    )
    advisory = client.get(
        f"/api/applications/{seeded['application_id']}/events/{target_id}/readiness-feedback"
    )
    focus = client.get(
        f"/api/interview-practice/focus/{version_id}",
        params={"target_event_id": target_id},
    )
    assert detail.status_code == advisory.status_code == focus.status_code == 503
    assert detail.json() == advisory.json() == focus.json() == {
        "error_code": "review_readiness_unavailable",
        "retryable": True,
    }


def test_baseline_interview_readiness_bytes_ignore_every_signal_source_state(tmp_path) -> None:
    """Signal state is orthogonal to the established Application/Event readiness bytes."""

    focuses = [
        {
            "id": focus_id,
            "text": f"Focus {focus_id}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for focus_id in ("focus-1", "focus-2")
    ]
    app = create_app(data_dir=tmp_path)
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(
        session_factory,
        focus_id="focus-1",
        practice_focuses=focuses,
    )
    client = TestClient(app)
    readiness_url = f"/api/interviews/{seeded['event_id']}"
    zero_signal_bytes = client.get(readiness_url).content

    signal_id, version_id = _create_readiness_signal(client, session_factory, seeded)
    with session_factory() as session:
        assert load_canonical_readiness_signal(
            session, signal_id=signal_id
        ).state == "current"
        second_candidate = next(
            item
            for item in project_readiness_candidates(
                int(seeded["note_id"]), int(seeded["proposal_id"]), session
            ).candidates
            if item.focus_id == "focus-2"
        )
    proposed = client.post(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions",
        content=json.dumps(
            {
                "proposal_id": seeded["proposal_id"],
                "focus_id": "focus-2",
                "expected_note_revision": seeded["note_revision"],
                "expected_candidate_fingerprint": second_candidate.candidate_fingerprint,
                "idempotency_key": str(uuid4()),
                "user_note": "",
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert proposed.status_code == 201
    rejected = client.post(
        f"/api/product-actions/{proposed.json()['operation_id']}/decisions",
        content=json.dumps(
            {
                "confirmation_token": proposed.json()["confirmation_token"],
                "decision": "reject",
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert rejected.status_code == 200
    assert client.get(readiness_url).content == zero_signal_bytes

    with session_factory() as session:
        proposal = session.get(InterviewReviewProposal, int(seeded["proposal_id"]))
        assert proposal is not None
        proposal.proposal_hash = "0" * 64
        session.commit()
    with session_factory() as session:
        assert load_canonical_readiness_signal(
            session, signal_id=signal_id
        ).state == "changed"
    assert client.get(readiness_url).content == zero_signal_bytes

    with session_factory() as session:
        parent = session.get(InterviewReadinessSignalVersion, version_id)
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert parent is not None and signal is not None
        evidence = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(InterviewReadinessSignalEvidence.signal_version_id == parent.id)
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        retraction = InterviewReadinessSignalVersion(
            signal_id=signal.id,
            version_number=2,
            parent_version_id=parent.id,
            disposition="retracted",
            schema_version=parent.schema_version,
            statement_text=parent.statement_text,
            user_note=parent.user_note,
            source_note_revision=parent.source_note_revision,
            source_note_fingerprint=parent.source_note_fingerprint,
            source_proposal_hash=parent.source_proposal_hash,
            candidate_fingerprint=parent.candidate_fingerprint,
            domain_idempotency_key=str(uuid4()),
            write_operation_id=proposed.json()["operation_id"],
        )
        session.add(retraction)
        session.flush()
        session.add_all(
            InterviewReadinessSignalEvidence(
                signal_version_id=retraction.id,
                ordinal=item.ordinal,
                source_path=item.source_path,
                excerpt=item.excerpt,
                excerpt_sha256=item.excerpt_sha256,
                source_field_sha256=item.source_field_sha256,
            )
            for item in evidence
        )
        signal.current_version_id = retraction.id
        signal.revision = 2
        session.commit()
        retraction_id = retraction.id
    with session_factory() as session:
        assert load_canonical_readiness_signal(
            session, signal_id=signal_id
        ).state == "retracted"
    assert client.get(readiness_url).content == zero_signal_bytes

    with session_factory() as session:
        evidence_id = session.scalar(
            select(InterviewReadinessSignalEvidence.id).where(
                InterviewReadinessSignalEvidence.signal_version_id == retraction_id
            )
        )
        assert evidence_id is not None
        session.execute(
            text(
                "UPDATE interview_readiness_signal_evidence "
                "SET excerpt_sha256 = :bad WHERE id = :evidence_id"
            ),
            {"bad": "sha256:" + "0" * 64, "evidence_id": evidence_id},
        )
        session.commit()
    with session_factory() as session:
        assert load_canonical_readiness_signal(
            session, signal_id=signal_id
        ).state == "unavailable"
    assert client.get(readiness_url).content == zero_signal_bytes
