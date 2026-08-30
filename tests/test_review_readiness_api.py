from __future__ import annotations

import json

from fastapi.testclient import TestClient

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
    assert rejection_recovery.json()["confirmation_token"] == token
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


def test_product_action_raw_decoder_rejects_bool_and_duplicate_before_sql(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))

    boolean = client.post(
        "/api/interview-notes/1/readiness-focus-actions",
        content=(
            b'{"proposal_id":true,"focus_id":"f","expected_note_revision":1,'
            b'"expected_candidate_fingerprint":"sha256:' + b"0" * 64
            + b'","idempotency_key":"55555555-5555-4555-8555-555555555555",'
            b'"user_note":""}'
        ),
        headers={"content-type": "application/json"},
    )
    duplicate = client.post(
        "/api/interview-notes/1/readiness-focus-actions",
        content=b'{"proposal_id":1,"proposal_id":1}',
        headers={"content-type": "application/json"},
    )

    assert boolean.status_code == 422
    assert boolean.json()["error_code"] == "product_action_invalid_request"
    assert duplicate.status_code == 422
    assert duplicate.json()["error_code"] == "product_action_invalid_request"
