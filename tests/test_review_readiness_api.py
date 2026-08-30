from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.review_readiness.candidates import project_readiness_candidates

from tests.review_readiness_support import seed_review_candidate


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
