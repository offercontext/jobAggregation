from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from offerpilot.api import create_app
from offerpilot.ai.write_operations import LedgerKeyDomain, build_terminal_payload
from offerpilot.models import InterviewNote, InterviewStoryProposalAttempt
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
)
from offerpilot.product_actions import compensation as compensation_module
from offerpilot.product_actions.compensation import (
    ProductActionCompensationError,
    product_action_compensation_request_fingerprint,
)
from offerpilot.product_actions.coordinator import (
    ProductActionCoordinatorError,
    ProductActionStoryWriteConflict,
)
from offerpilot.product_actions.repository import ProductActionPublicationV1
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    LedgerKeyProfileStoreV1,
)
from offerpilot.product_actions.repository import ProductActionProposalRepository
from offerpilot.repositories.interview_stories import (
    InterviewStoriesRepository,
    InterviewStoryProductActionHandler,
    StoryValidationError,
    _manual_request_fingerprint,
    _story_lifecycle_idempotency_key,
    _story_lifecycle_request_fingerprint,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text


def _note(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/notes",
        json={
            "company": "星云数据",
            "position": "后端工程师",
            "questions": "你如何排查线上延迟？",
            "self_reflection": "我应该更早同步风险。",
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _provider_story(snapshot: Any) -> dict[str, Any]:
    note = next(item for item in snapshot.sources if item["source_kind"] == "interview_note")
    assertion = next(item for item in snapshot.sources if item["source_kind"] == "user_assertion")

    def ref(item: Mapping[str, str]) -> dict[str, str]:
        return {
            "source_kind": item["source_kind"],
            "source_stable_id": item["source_stable_id"],
            "source_version_or_snapshot": item["source_version_or_snapshot"],
            "source_path": item["path"],
            "excerpt": item["excerpt"],
        }

    return {
        "title": {"text": "Incident recovery", "evidence_refs": [ref(note)]},
        "blocks": [
            {
                "kind": "situation",
                "text": "Latency investigation",
                "fact_mode": "evidence_backed",
                "evidence_refs": [ref(note)],
            },
            {
                "kind": "reflection",
                "text": "Communicate earlier",
                "fact_mode": "user_view",
                "evidence_refs": [ref(assertion)],
            },
        ],
        "capability_labels": [
            {"text": "incident response", "evidence_refs": [ref(note)]}
        ],
        "applicable_questions": [
            {"text": "Describe incident response", "evidence_refs": [ref(note)]}
        ],
        "fact_gap_codes": ["missing_result"],
    }


def _proposal_request(note_id: int, *, key: str = "story-product-action-attempt-0001") -> dict[str, Any]:
    return {
        "target_story_id": None,
        "expected_current_version_id": None,
        "expected_story_revision": None,
        "selections": [
            {"source_kind": "interview_note", "source_id": note_id, "path": "/questions"}
        ],
        "assertions": ["I owned this incident response."],
        "idempotency_key": key,
    }


def _confirmation_from_attempt(
    attempt: dict[str, Any],
    *,
    token: str | None = None,
) -> dict[str, Any]:
    proposal = attempt["proposal"]
    return {
        "confirmation_token": token or attempt["product_action"]["confirmation_token"],
        "content": {
            "title": proposal["content"]["title"]["text"],
            "blocks": [
                {key: block[key] for key in ("kind", "text", "fact_mode")}
                for block in proposal["content"]["blocks"]
            ],
            "capability_labels": [
                item["text"] for item in proposal["content"]["capability_labels"]
            ],
            "applicable_questions": [
                item["text"] for item in proposal["content"]["applicable_questions"]
            ],
            "fact_gap_codes": proposal["content"]["fact_gap_codes"],
        },
        "evidence_links": [
            {
                key: value
                for key, value in link.items()
                if key
                in {
                    "target_kind",
                    "target_id",
                    "source_kind",
                    "source_stable_id",
                    "source_version_or_snapshot",
                    "source_path",
                    "excerpt",
                    "text_location",
                }
            }
            for link in proposal["evidence_links"]
        ],
        "expected_current_version_id": None,
        "expected_story_revision": None,
    }


def _legacy_ready_attempt(
    client: TestClient,
    *,
    note_id: int,
    key: str,
) -> tuple[InterviewStoriesRepository, dict[str, Any]]:
    production = client.app.state.interview_stories_repository
    legacy = InterviewStoriesRepository(production._session_factory)
    claim = legacy.claim_proposal(
        target_story_id=None,
        expected_current_version_id=None,
        expected_story_revision=None,
        selections=[
            {
                "source_kind": "interview_note",
                "source_id": note_id,
                "path": "/questions",
            }
        ],
        assertions=["I owned this incident response."],
        idempotency_key=key,
        entrypoint="ui",
    )
    assert legacy.complete_proposal(
        attempt_id=claim.attempt_id,
        generation_revision=claim.generation_revision,
        provider_call_token=claim.provider_call_token,
        proposal=_provider_story(claim.source_snapshot),
    )
    attempt = legacy.get_attempt(claim.attempt_id)
    assert attempt is not None and attempt["product_action_generation"] == 0
    return legacy, attempt


@pytest.fixture
def story_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "offerpilot.api.generate_interview_story_proposal",
        lambda _model, snapshot, **_kwargs: _provider_story(snapshot),
    )
    with TestClient(create_app(data_dir=tmp_path, chat_model=object())) as client:
        yield client, tmp_path


def _confirmed_appended_story(
    client: TestClient,
    *,
    key_prefix: str,
) -> dict[str, int | str]:
    note = _note(client)
    first = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"{key_prefix}-base-0001"),
    ).json()
    first_confirmation = _confirmation_from_attempt(first)
    first_confirmation["content"]["title"] = "Original title"
    created = client.post(
        f"/api/interview-story-proposals/{first['id']}/confirm",
        json=first_confirmation,
    )
    assert created.status_code == 201, created.json()
    story_id = created.json()["story_id"]
    previous_version_id = created.json()["version_id"]
    second_request = _proposal_request(
        note["id"],
        key=f"{key_prefix}-append-0001",
    )
    second_request.update(
        {
            "target_story_id": story_id,
            "expected_current_version_id": previous_version_id,
            "expected_story_revision": 1,
        }
    )
    second = client.post(
        "/api/interview-story-proposals",
        json=second_request,
    ).json()
    second_confirmation = _confirmation_from_attempt(second)
    second_confirmation["content"]["title"] = "Replacement title"
    second_confirmation["expected_current_version_id"] = previous_version_id
    second_confirmation["expected_story_revision"] = 1
    appended = client.post(
        f"/api/interview-story-proposals/{second['id']}/confirm",
        json=second_confirmation,
    )
    assert appended.status_code == 201, appended.json()
    return {
        "story_id": story_id,
        "previous_version_id": previous_version_id,
        "created_version_id": appended.json()["version_id"],
        "base_parent_operation_id": first["product_action"]["operation_id"],
        "parent_operation_id": second["product_action"]["operation_id"],
        "attempt_id": second["id"],
    }


def _manual_history_then_product_action_append(
    client: TestClient,
    *,
    key_prefix: str,
    lifecycle_before_manual: bool = False,
) -> dict[str, Any]:
    note = _note(client)
    seed = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"{key_prefix}-materialize-0001"),
    ).json()
    confirmation = _confirmation_from_attempt(seed)
    repository = client.app.state.interview_stories_repository
    selections = [
        {
            "source_kind": "interview_note",
            "source_id": note["id"],
            "path": "/questions",
        }
    ]
    assertions = ["I owned this incident response."]
    created = repository.create_manual_story(
        content={**confirmation["content"], "title": "Manual original title"},
        evidence_links=confirmation["evidence_links"],
        selections=selections,
        assertions=assertions,
        expected_current_version_id=None,
        idempotency_key=f"{key_prefix}-manual-create-0001",
    )
    if lifecycle_before_manual:
        archived = client.post(
            f"/api/interview-stories/{created['id']}/archive",
            json={"expected_story_revision": created["story_revision"]},
        )
        assert archived.status_code == 200, archived.json()
        restored = client.post(
            f"/api/interview-stories/{created['id']}/restore",
            json={"expected_story_revision": archived.json()["story_revision"]},
        )
        assert restored.status_code == 200, restored.json()
        created = restored.json()
    manual_version = repository.create_manual_version(
        story_id=created["id"],
        content={**confirmation["content"], "title": "Manual current title"},
        evidence_links=confirmation["evidence_links"],
        selections=selections,
        assertions=assertions,
        expected_current_version_id=created["current_version_id"],
        expected_story_revision=created["story_revision"],
        idempotency_key=f"{key_prefix}-manual-version-0001",
    )
    append_request = _proposal_request(
        note["id"],
        key=f"{key_prefix}-proposal-append-0001",
    )
    append_request.update(
        {
            "target_story_id": created["id"],
            "expected_current_version_id": manual_version["current_version_id"],
            "expected_story_revision": manual_version["story_revision"],
        }
    )
    append_attempt = client.post(
        "/api/interview-story-proposals",
        json=append_request,
    ).json()
    append_confirmation = _confirmation_from_attempt(append_attempt)
    append_confirmation.update(
        {
            "expected_current_version_id": manual_version["current_version_id"],
            "expected_story_revision": manual_version["story_revision"],
        }
    )
    append_confirmation["content"]["title"] = "Proposal replacement title"
    appended = client.post(
        f"/api/interview-story-proposals/{append_attempt['id']}/confirm",
        json=append_confirmation,
    )
    assert appended.status_code == 201, appended.json()
    return {
        "story_id": created["id"],
        "manual_create_version_id": created["current_version_id"],
        "previous_version_id": manual_version["current_version_id"],
        "created_version_id": appended.json()["version_id"],
        "parent_operation_id": append_attempt["product_action"]["operation_id"],
        "manual_content": {
            **confirmation["content"],
            "title": "Manual current title",
        },
        "manual_evidence_links": confirmation["evidence_links"],
        "manual_selections": selections,
        "manual_assertions": assertions,
        "lifecycle_before_manual": lifecycle_before_manual,
    }


def test_ready_story_publication_is_one_four_object_product_action_bundle(story_client) -> None:
    client, data_dir = story_client
    note = _note(client)

    response = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"]),
    )

    assert response.status_code == 201, response.json()
    payload = response.json()
    assert payload["attempt_status"] == "ready"
    assert payload["product_action_generation"] == 1
    assert set(payload["product_action"]) == {
        "operation_id",
        "action_call_id",
        "confirmation_token",
        "action_name",
    }
    assert payload["product_action"]["action_name"] == "confirm_interview_story"
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt = connection.execute(
            "SELECT product_action_operation_id, product_action_generation "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (payload["id"],),
        ).fetchone()
        parent = connection.execute(
            "SELECT status, adapter_kind, tool_name FROM write_operations WHERE id=?",
            (payload["product_action"]["operation_id"],),
        ).fetchone()
        route = connection.execute(
            "SELECT action_name, request_origin FROM product_action_proposals WHERE operation_id=?",
            (payload["product_action"]["operation_id"],),
        ).fetchone()
        transitions = connection.execute(
            "SELECT seq, state FROM write_operation_transitions WHERE operation_id=? ORDER BY seq",
            (payload["product_action"]["operation_id"],),
        ).fetchall()
    assert attempt == (payload["product_action"]["operation_id"], 1)
    assert parent == ("proposed", "product_action", "confirm_interview_story")
    assert route == ("confirm_interview_story", "current")
    assert transitions == [(1, "proposed")]


def test_created_story_product_action_undo_archives_and_replays(
    story_client,
    monkeypatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-created-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    )
    assert confirmed.status_code == 201, confirmed.json()
    story_id = confirmed.json()["story_id"]
    parent_operation_id = attempt["product_action"]["operation_id"]
    executions = 0
    repository = client.app.state.interview_stories_repository
    original_executor = repository.undo_product_action_in_session

    def counted_executor(*args: Any, **kwargs: Any) -> Any:
        nonlocal executions
        executions += 1
        return original_executor(*args, **kwargs)

    monkeypatch.setattr(
        repository,
        "undo_product_action_in_session",
        counted_executor,
    )

    undone = client.post(
        f"/api/interview-stories/{story_id}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )
    assert undone.status_code == 200, undone.json()
    assert undone.json() == {
        "schema_version": 1,
        "operation_id": undone.json()["operation_id"],
        "compensation_kind": "undo:confirm_interview_story",
        "status": "committed",
        "result": {
            "kind": "interview_story_undo_applied_v1",
            "story_id": story_id,
            "current_version_id": confirmed.json()["version_id"],
            "story_revision": 2,
            "status": "archived",
        },
        "replayed": False,
    }
    def forbidden_executor(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal executions
        executions += 1
        raise AssertionError("terminal replay must not execute Story Undo")

    monkeypatch.setattr(
        repository,
        "undo_product_action_in_session",
        forbidden_executor,
    )
    replay = client.post(
        f"/api/interview-stories/{story_id}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )
    assert replay.status_code == 200
    assert replay.json()["result"] == undone.json()["result"]
    assert replay.json()["replayed"] is True
    assert executions == 1
    proof = client.app.state.interview_story_undo_issuer.issue(
        story_id=story_id,
        parent_operation_id=parent_operation_id,
    )
    with pytest.raises(TypeError):
        copy.copy(proof)
    direct_replay = client.app.state.product_action_compensation_coordinator.execute(
        proof
    )
    assert direct_replay.replayed is True
    with pytest.raises(ProductActionCompensationError) as reused:
        client.app.state.product_action_compensation_coordinator.execute(proof)
    assert reused.value.code == "product_action_compensation_proof_invalid"
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT status,current_version_id,story_revision FROM interview_stories WHERE id=?",
            (story_id,),
        ).fetchone()
        versions = connection.execute(
            "SELECT COUNT(*) FROM interview_story_versions WHERE story_id=?",
            (story_id,),
        ).fetchone()[0]
    assert story == ("archived", confirmed.json()["version_id"], 2)
    assert versions == 1


def test_appended_story_product_action_undo_restores_pointer_title_and_preserves_history(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    first = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-base-0001"),
    ).json()
    first_confirmation = _confirmation_from_attempt(first)
    first_confirmation["content"]["title"] = "Original title"
    created = client.post(
        f"/api/interview-story-proposals/{first['id']}/confirm",
        json=first_confirmation,
    )
    assert created.status_code == 201
    story_id = created.json()["story_id"]
    original_version_id = created.json()["version_id"]

    second_request = _proposal_request(
        note["id"],
        key="story-product-action-undo-append-0001",
    )
    second_request.update(
        {
            "target_story_id": story_id,
            "expected_current_version_id": original_version_id,
            "expected_story_revision": 1,
        }
    )
    second = client.post("/api/interview-story-proposals", json=second_request).json()
    second_confirmation = _confirmation_from_attempt(second)
    second_confirmation["content"]["title"] = "Replacement title"
    second_confirmation["expected_current_version_id"] = original_version_id
    second_confirmation["expected_story_revision"] = 1
    appended = client.post(
        f"/api/interview-story-proposals/{second['id']}/confirm",
        json=second_confirmation,
    )
    assert appended.status_code == 201, appended.json()

    undone = client.post(
        f"/api/interview-stories/{story_id}/product-action-undo",
        json={"parent_operation_id": second["product_action"]["operation_id"]},
    )
    assert undone.status_code == 200, undone.json()
    assert undone.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "story_id": story_id,
        "current_version_id": original_version_id,
        "story_revision": 3,
        "status": "active",
    }
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT title,status,current_version_id,story_revision FROM interview_stories WHERE id=?",
            (story_id,),
        ).fetchone()
        version_ids = connection.execute(
            "SELECT id FROM interview_story_versions WHERE story_id=? ORDER BY version_number",
            (story_id,),
        ).fetchall()
        compensation = connection.execute(
            "SELECT result_json,visible_result,transport_json,undo_json "
            "FROM write_operations WHERE parent_operation_id=? "
            "AND tool_name='undo:confirm_interview_story'",
            (second["product_action"]["operation_id"],),
        ).fetchone()
    assert story == ("Original title", "active", original_version_id, 3)
    assert version_ids == [(original_version_id,), (appended.json()["version_id"],)]
    assert compensation is not None and compensation[3] is None
    assert "Original title" not in "".join(item or "" for item in compensation[:3])


def test_appended_story_undo_supports_real_manual_history_and_terminal_replay(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-manual-history",
    )
    request = {"parent_operation_id": seeded["parent_operation_id"]}

    undone = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )
    replay = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )

    assert undone.status_code == 200, undone.json()
    assert undone.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "story_id": seeded["story_id"],
        "current_version_id": seeded["previous_version_id"],
        "story_revision": 4,
        "status": "active",
    }
    assert replay.status_code == 200, replay.json()
    assert replay.json()["replayed"] is True
    assert replay.json()["result"] == undone.json()["result"]
    with sqlite3.connect(data_dir / "data.db") as connection:
        history = connection.execute(
            "SELECT id,origin_kind FROM interview_story_versions "
            "WHERE story_id=? ORDER BY version_number",
            (seeded["story_id"],),
        ).fetchall()
    assert history == [
        (seeded["manual_create_version_id"], "manual"),
        (seeded["previous_version_id"], "manual"),
        (seeded["created_version_id"], "proposal"),
    ]


def test_story_undo_proves_lifecycle_then_manual_raw_cas_and_replays(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-lifecycle-manual-chain",
        lifecycle_before_manual=True,
    )
    request = {"parent_operation_id": seeded["parent_operation_id"]}

    undone = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )
    replay = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )

    assert undone.status_code == 200, undone.json()
    assert undone.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "story_id": seeded["story_id"],
        "current_version_id": seeded["previous_version_id"],
        "story_revision": 6,
        "status": "active",
    }
    assert replay.status_code == 200, replay.json()
    assert replay.json()["replayed"] is True
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT title,current_version_id,story_revision,status,archived_at "
            "FROM interview_stories WHERE id=?",
            (seeded["story_id"],),
        ).fetchone()
        manual_payload = json.loads(
            connection.execute(
                "SELECT input_snapshot_json FROM interview_story_proposal_attempts "
                "WHERE confirmed_story_version_id=?",
                (seeded["previous_version_id"],),
            ).fetchone()[0]
        )
        create_payload = json.loads(
            connection.execute(
                "SELECT input_snapshot_json FROM interview_story_proposal_attempts "
                "WHERE confirmed_story_version_id=?",
                (seeded["manual_create_version_id"],),
            ).fetchone()[0]
        )
        lifecycle_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM interview_story_proposal_attempts "
                "WHERE target_story_id=? AND entrypoint='internal' ORDER BY id",
                (seeded["story_id"],),
            ).fetchall()
        ]
    assert story == (
        "Manual current title",
        seeded["previous_version_id"],
        6,
        "active",
        None,
    )
    assert manual_payload == {
        "expected_current_version_id": seeded["manual_create_version_id"],
        "expected_story_revision": 3,
        "operation": "manual_save",
        "request_fingerprint": manual_payload["request_fingerprint"],
        "target_story_id": seeded["story_id"],
    }
    assert create_payload == {
        "expected_current_version_id": None,
        "expected_story_revision": None,
        "operation": "manual_save",
        "request_fingerprint": create_payload["request_fingerprint"],
        "target_story_id": None,
    }
    assert len(lifecycle_ids) == 2
    for lifecycle_id in lifecycle_ids:
        hidden = client.get(f"/api/interview-story-proposals/{lifecycle_id}")
        assert hidden.status_code == 404
    hidden_confirm = client.post(
        f"/api/interview-story-proposals/{lifecycle_ids[0]}/confirm",
        json={
            "confirmation_token": "0" * 64,
            "content": seeded["manual_content"],
            "evidence_links": seeded["manual_evidence_links"],
            "expected_current_version_id": seeded["manual_create_version_id"],
            "expected_story_revision": 1,
        },
    )
    hidden_next = client.post(
        f"/api/interview-story-proposals/{lifecycle_ids[0]}/product-actions",
        json={
            "expected_generation_revision": 1,
            "expected_product_action_generation": 1,
        },
    )
    assert hidden_confirm.status_code == 404
    assert hidden_next.status_code == 404


def test_story_undo_restores_manual_version_saved_with_public_client_link_shape(
    story_client,
) -> None:
    client, _data_dir = story_client
    note = _note(client)
    content = {
        "title": "Manual original title",
        "blocks": [
            {
                "kind": "situation",
                "text": "Latency investigation",
                "fact_mode": "evidence_backed",
            }
        ],
        "capability_labels": ["incident response"],
        "applicable_questions": ["Describe incident response"],
        "fact_gap_codes": ["missing_result"],
    }
    source = {
        "source_kind": "interview_note",
        "source_id": note["id"],
        "source_path": "/questions",
        "excerpt": "你如何排查线上延迟？",
    }
    evidence_links = [
        {"target_kind": "title", "target_id": "title", **source},
        {
            "target_kind": "block",
            "target_id": "situation_001",
            **source,
        },
        {
            "target_kind": "capability_label",
            "target_id": "capability_001",
            **source,
        },
        {
            "target_kind": "applicable_question",
            "target_id": "question_001",
            **source,
        },
    ]
    create_payload = {
        "content": content,
        "evidence_links": evidence_links,
        "selections": [
            {
                "source_kind": "interview_note",
                "source_id": note["id"],
                "path": "/questions",
            }
        ],
        "assertions": [],
        "expected_current_version_id": None,
        "idempotency_key": "story-undo-public-manual-create-0001",
    }
    created = client.post("/api/interview-stories", json=create_payload)
    assert created.status_code == 201, created.json()
    archived = client.post(
        f"/api/interview-stories/{created.json()['id']}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200, archived.json()
    restored = client.post(
        f"/api/interview-stories/{created.json()['id']}/restore",
        json={"expected_story_revision": 2},
    )
    assert restored.status_code == 200, restored.json()
    version_payload = {
        **create_payload,
        "content": {**content, "title": "Manual current title"},
        "expected_current_version_id": restored.json()["current_version_id"],
        "expected_story_revision": restored.json()["story_revision"],
        "idempotency_key": "story-undo-public-manual-version-0001",
    }
    manual = client.post(
        f"/api/interview-stories/{created.json()['id']}/versions",
        json=version_payload,
    )
    assert manual.status_code == 201, manual.json()

    append_request = _proposal_request(
        note["id"],
        key="story-undo-public-manual-append-0001",
    )
    append_request.update(
        {
            "target_story_id": created.json()["id"],
            "expected_current_version_id": manual.json()["current_version_id"],
            "expected_story_revision": manual.json()["story_revision"],
        }
    )
    attempt = client.post("/api/interview-story-proposals", json=append_request).json()
    confirmation = _confirmation_from_attempt(attempt)
    confirmation.update(
        {
            "expected_current_version_id": manual.json()["current_version_id"],
            "expected_story_revision": manual.json()["story_revision"],
        }
    )
    confirmation["content"]["title"] = "Proposal replacement title"
    appended = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=confirmation,
    )
    assert appended.status_code == 201, appended.json()

    undone = client.post(
        f"/api/interview-stories/{created.json()['id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )

    assert undone.status_code == 200, undone.json()
    assert undone.json()["status"] == "committed"
    assert undone.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "story_id": created.json()["id"],
        "current_version_id": manual.json()["current_version_id"],
        "story_revision": 6,
        "status": "active",
    }
    story = client.get(f"/api/interview-stories/{created.json()['id']}")
    assert story.status_code == 200, story.json()
    assert story.json()["title"] == "Manual current title"


@pytest.mark.parametrize(
    "corruption",
    ("content", "evidence", "assertion", "request_fingerprint"),
)
def test_appended_story_undo_fails_closed_on_manual_previous_lineage_corruption(
    story_client,
    corruption: str,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix=f"story-undo-manual-corrupt-{corruption}",
    )
    if corruption in {"content", "evidence", "assertion"}:
        _tamper_previous_story_version(
            data_dir,
            int(seeded["previous_version_id"]),
            corruption,
        )
    else:
        with sqlite3.connect(data_dir / "data.db") as connection:
            attempt_id = connection.execute(
                "SELECT id FROM interview_story_proposal_attempts "
                "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NULL",
                (seeded["previous_version_id"],),
            ).fetchone()[0]
            payload = json.loads(
                connection.execute(
                    "SELECT input_snapshot_json FROM interview_story_proposal_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            payload["request_fingerprint"] = "0" * 64
            connection.execute(
                "UPDATE interview_story_proposal_attempts SET input_snapshot_json=? WHERE id=?",
                (canonical_json(payload), attempt_id),
            )
            connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_manual_previous_lineage_rejects_coherent_but_unprovable_revision_rewrite(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-manual-coherent-cas",
    )
    rewritten_fingerprint = _manual_request_fingerprint(
        target_story_id=seeded["story_id"],
        content=seeded["manual_content"],
        evidence_links=seeded["manual_evidence_links"],
        selections=seeded["manual_selections"],
        assertions=seeded["manual_assertions"],
        expected_current_version_id=seeded["manual_create_version_id"],
        expected_story_revision=2,
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt_id, raw = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NULL",
            (seeded["previous_version_id"],),
        ).fetchone()
        payload = json.loads(raw)
        payload["expected_story_revision"] = 2
        payload["request_fingerprint"] = rewritten_fingerprint
        rewritten_input = canonical_json(payload)
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=?,"
            "confirmation_payload_hash=? WHERE id=?",
            (rewritten_input, sha256_text(rewritten_input), attempt_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_manual_previous_lineage_revalidates_each_prior_attempt_in_sequence(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-manual-prior-attempt-sequence",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt_id, raw = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NULL",
            (seeded["manual_create_version_id"],),
        ).fetchone()
        payload = json.loads(raw)
        payload["request_fingerprint"] = "0" * 64
        rewritten = canonical_json(payload)
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=?,"
            "confirmation_payload_hash=? WHERE id=?",
            (rewritten, sha256_text(rewritten), attempt_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_story_undo_accepts_provable_legacy_manual_create_attempt_shape(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-legacy-manual-create",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt_id, raw = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NULL",
            (seeded["manual_create_version_id"],),
        ).fetchone()
        current = json.loads(raw)
        legacy = canonical_json(
            {
                "operation": "manual_save",
                "request_fingerprint": current["request_fingerprint"],
            }
        )
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=?,"
            "confirmation_payload_hash=? WHERE id=?",
            (legacy, sha256_text(legacy), attempt_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "committed"
    assert response.json()["result"]["current_version_id"] == seeded[
        "previous_version_id"
    ]


def test_story_undo_accepts_provable_legacy_manual_update_attempt_shape(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _manual_history_then_product_action_append(
        client,
        key_prefix="story-undo-legacy-manual-update",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt_id, raw = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NULL",
            (seeded["previous_version_id"],),
        ).fetchone()
        current = json.loads(raw)
        legacy = canonical_json(
            {
                "operation": "manual_save",
                "request_fingerprint": current["request_fingerprint"],
            }
        )
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=?,"
            "confirmation_payload_hash=? WHERE id=?",
            (legacy, sha256_text(legacy), attempt_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "committed"
    assert response.json()["result"]["current_version_id"] == seeded[
        "previous_version_id"
    ]
    story = client.get(f"/api/interview-stories/{seeded['story_id']}")
    assert story.status_code == 200, story.json()
    assert story.json()["title"] == "Manual current title"


def test_story_undo_rejects_forged_pointer_rollback_without_later_lineage(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-forged-pointer-rollback",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_stories SET title='Original title',current_version_id=?,"
            "story_revision=3,status='active',archived_at=NULL WHERE id=?",
            (seeded["previous_version_id"], seeded["story_id"]),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_story_undo_rejects_revision_increase_without_a_later_version(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-forged-revision",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_stories SET story_revision=3 WHERE id=?",
            (seeded["story_id"],),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_appended_story_undo_fails_closed_on_proposal_previous_request_lineage_corruption(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-proposal-request-lineage",
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        attempt_id, raw = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE confirmed_story_version_id=? AND product_action_operation_id IS NOT NULL",
            (seeded["previous_version_id"],),
        ).fetchone()
        payload = json.loads(raw)
        payload["request_fingerprint"] = "0" * 64
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=? WHERE id=?",
            (canonical_json(payload), attempt_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone() == (0,)


def test_story_compensation_coordinator_uses_frozen_six_field_request_envelope(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-frozen-envelope-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    captured: list[dict[str, Any]] = []
    original_fingerprint = compensation_module.ledger_fingerprint

    def capture(
        key: LedgerKeyDomain,
        domain: str,
        payload: dict[str, Any],
    ) -> str:
        if domain == "product-action-compensation-request-v1":
            captured.append(dict(payload))
        return original_fingerprint(key, domain, payload)

    monkeypatch.setattr(compensation_module, "ledger_fingerprint", capture)
    undone = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert undone.status_code == 200, undone.json()
    assert captured and all(
        set(payload)
        == {
            "request_kind",
            "operation_id",
            "parent_operation_id",
            "parent_action_name",
            "parent_terminal_payload_sha256",
            "compensation_kind",
        }
        for payload in captured
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        persisted = connection.execute(
            "SELECT id,operation_request_fingerprint,fingerprint_key_id,"
            "parent_terminal_payload_sha256 FROM write_operations "
            "WHERE operation_role='compensation' AND parent_operation_id=?",
            (attempt["product_action"]["operation_id"],),
        ).fetchone()
    assert persisted is not None
    key = client.app.state.product_action_compensation_coordinator._key_profiles.resolve(
        persisted[2]
    )
    assert persisted[1] == product_action_compensation_request_fingerprint(
        key,
        operation_id=persisted[0],
        parent_operation_id=attempt["product_action"]["operation_id"],
        parent_action_name="confirm_interview_story",
        parent_terminal_payload_sha256=persisted[3],
        compensation_kind="undo:confirm_interview_story",
    )
    replay = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json()["replayed"] is True


def test_story_product_action_undo_rejects_wrong_owner_and_proves_archive_stale(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-stale-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    parent_operation_id = attempt["product_action"]["operation_id"]
    wrong = client.post(
        f"/api/interview-stories/{confirmed['story_id'] + 1}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )
    assert wrong.status_code == 404
    assert wrong.json()["error_code"] == "product_action_compensation_not_found"

    archived = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200
    stale = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )
    assert stale.status_code == 200
    assert stale.json()["status"] == "failed"
    assert stale.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "code": "interview_story_undo_stale",
    }
    replay = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json()["replayed"] is True
    assert replay.json()["result"] == stale.json()["result"]
    with sqlite3.connect(data_dir / "data.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM write_operations "
            "WHERE operation_role='compensation' AND parent_operation_id=?",
            (parent_operation_id,),
        ).fetchone()
    assert count == (1,)


def test_lifecycle_audit_namespace_cannot_be_preoccupied_by_proposal_clients(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-lifecycle-owner-base-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    )
    assert confirmed.status_code == 201, confirmed.json()
    story_id = confirmed.json()["story_id"]

    def legacy_collision_key(revision: int, desired_status: str) -> str:
        material = canonical_json(
            {
                "operation": "story_lifecycle_v1",
                "target_story_id": story_id,
                "expected_story_revision": revision,
                "desired_status": desired_status,
            }
        )
        return "story_lifecycle_" + sha256_text(material)

    archive_collision = legacy_collision_key(1, "archived")
    preoccupy_archive = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=archive_collision),
    )
    assert preoccupy_archive.status_code == 422, preoccupy_archive.json()
    archived = client.post(
        f"/api/interview-stories/{story_id}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200, archived.json()

    restore_collision = legacy_collision_key(2, "active")
    preoccupy_restore = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=restore_collision),
    )
    assert preoccupy_restore.status_code == 422, preoccupy_restore.json()
    restored = client.post(
        f"/api/interview-stories/{story_id}/restore",
        json={"expected_story_revision": 2},
    )
    assert restored.status_code == 200, restored.json()

    with sqlite3.connect(data_dir / "data.db") as connection:
        lifecycle_rows = connection.execute(
            "SELECT id,idempotency_key FROM interview_story_proposal_attempts "
            "WHERE target_story_id=? AND entrypoint='internal' ORDER BY id",
            (story_id,),
        ).fetchall()
    assert len(lifecycle_rows) == 2
    assert {row[1] for row in lifecycle_rows}.isdisjoint(
        {archive_collision, restore_collision}
    )
    for lifecycle_id, _key in lifecycle_rows:
        hidden = client.get(f"/api/interview-story-proposals/{lifecycle_id}")
        assert hidden.status_code == 404


def test_manual_writer_reserves_lifecycle_namespace_without_breaking_legacy_replay(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    seed = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-manual-namespace-seed-0001"),
    ).json()
    confirmation = _confirmation_from_attempt(seed)
    repository = client.app.state.interview_stories_repository
    selections = [
        {
            "source_kind": "interview_note",
            "source_id": note["id"],
            "path": "/questions",
        }
    ]
    create_args = {
        "content": {**confirmation["content"], "title": "Legacy replay title"},
        "evidence_links": confirmation["evidence_links"],
        "selections": selections,
        "assertions": ["I owned this incident response."],
        "expected_current_version_id": None,
    }
    created = repository.create_manual_story(
        **create_args,
        idempotency_key="story-manual-namespace-create-0001",
    )
    legacy_key = "story_lifecycle_" + "e" * 64
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET idempotency_key=?,"
            "confirmation_token_hash=? WHERE confirmed_story_version_id=?",
            (
                legacy_key,
                sha256_text(legacy_key),
                created["current_version_id"],
            ),
        )
        connection.commit()

    replay = repository.create_manual_story(
        **create_args,
        idempotency_key=legacy_key,
    )
    assert replay["id"] == created["id"]
    with pytest.raises(StoryValidationError):
        repository.create_manual_story(
            **create_args,
            idempotency_key="story_lifecycle_" + "f" * 64,
        )


def test_story_undo_validates_pre_isolation_lifecycle_audit_identity(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-legacy-lifecycle-base-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    story_id = confirmed["story_id"]
    archived = client.post(
        f"/api/interview-stories/{story_id}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200, archived.json()
    material = canonical_json(
        {
            "operation": "story_lifecycle_v1",
            "target_story_id": story_id,
            "expected_story_revision": 1,
            "desired_status": "archived",
        }
    )
    legacy_key = "story_lifecycle_" + sha256_text(material)
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET idempotency_key=?,"
            "confirmation_token_hash=? WHERE target_story_id=? AND entrypoint='internal'",
            (legacy_key, sha256_text(legacy_key), story_id),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{story_id}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "failed"
    assert response.json()["result"]["code"] == "interview_story_undo_stale"


@pytest.mark.parametrize("corruption", ("coherent_revision", "confirmed_at"))
def test_story_undo_fails_closed_on_lifecycle_event_corruption(
    story_client,
    corruption: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-lifecycle-corrupt-{corruption}-0001",
        ),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    story_id = confirmed["story_id"]
    parent_operation_id = attempt["product_action"]["operation_id"]
    archived = client.post(
        f"/api/interview-stories/{story_id}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200, archived.json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        lifecycle_id, payload_json = connection.execute(
            "SELECT id,input_snapshot_json FROM interview_story_proposal_attempts "
            "WHERE target_story_id=? AND entrypoint='internal'",
            (story_id,),
        ).fetchone()
        if corruption == "coherent_revision":
            payload = json.loads(payload_json)
            payload["expected_story_revision"] = 2
            payload["before"]["story_revision"] = 2
            payload["after"]["story_revision"] = 3
            fingerprint_fields = dict(payload)
            fingerprint_fields.pop("request_fingerprint")
            fingerprint = _story_lifecycle_request_fingerprint(
                fingerprint_fields
            )
            payload["request_fingerprint"] = fingerprint
            rewritten = canonical_json(payload)
            key = _story_lifecycle_idempotency_key(
                story_id=story_id,
                expected_story_revision=2,
                desired_status="archived",
            )
            connection.execute(
                "UPDATE interview_story_proposal_attempts SET "
                "idempotency_key=?,input_snapshot_json=?,source_fingerprint=?,"
                "confirmation_token_hash=?,confirmation_payload_hash=? WHERE id=?",
                (
                    key,
                    rewritten,
                    fingerprint,
                    sha256_text(key),
                    sha256_text(rewritten),
                    lifecycle_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE interview_story_proposal_attempts "
                "SET confirmed_at='1999-01-01 00:00:00.000000' WHERE id=?",
                (lifecycle_id,),
            )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{story_id}/product-action-undo",
        json={"parent_operation_id": parent_operation_id},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations "
            "WHERE operation_role='compensation' AND parent_operation_id=?",
            (parent_operation_id,),
        ).fetchone() == (0,)


def test_only_owner_scoped_product_action_undo_routes_are_exposed(story_client) -> None:
    client, _data_dir = story_client
    routes = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
    }
    assert (
        "POST",
        "/api/applications/{application_id}/readiness-signals/{signal_id}/undo",
    ) in routes
    assert (
        "POST",
        "/api/interview-stories/{story_id}/product-action-undo",
    ) in routes
    assert not any(
        method == "POST"
        and "product-action" in path
        and "undo" in path
        and path
        not in {
            "/api/applications/{application_id}/readiness-signals/{signal_id}/undo",
            "/api/interview-stories/{story_id}/product-action-undo",
        }
        for method, path in routes
    )
    assert not any(path.startswith("/api/stories/") for _method, path in routes)


def test_story_product_action_undo_rejects_raw_shape_before_owner_query(
    story_client,
    monkeypatch,
) -> None:
    client, _data_dir = story_client
    issuer = client.app.state.interview_story_undo_issuer
    monkeypatch.setattr(
        issuer,
        "issue",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("issuer queried")),
    )
    for raw_body in (
        b"[]",
        b"{}",
        b'{"parent_operation_id":true}',
        b'{"parent_operation_id":"00000000-0000-4000-8000-000000000001","x":1}',
        b'{"parent_operation_id":"00000000-0000-4000-8000-000000000001",'
        b'"parent_operation_id":"00000000-0000-4000-8000-000000000001"}',
    ):
        response = client.post(
            "/api/interview-stories/1/product-action-undo",
            content=raw_body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json() == {"error_code": "product_action_invalid_request"}


def test_story_product_action_undo_has_one_executor_under_twenty_call_race(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-race-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    story_id = confirmed["story_id"]
    parent_operation_id = attempt["product_action"]["operation_id"]

    def undo(_index: int) -> tuple[int, dict[str, Any]]:
        response = client.post(
            f"/api/interview-stories/{story_id}/product-action-undo",
            json={"parent_operation_id": parent_operation_id},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(undo, range(20)))
    assert all(status == 200 for status, _payload in outcomes)
    operation_ids = {payload["operation_id"] for _status, payload in outcomes}
    assert len(operation_ids) == 1
    assert sum(not payload["replayed"] for _status, payload in outcomes) == 1
    assert len({json.dumps(payload["result"], sort_keys=True) for _, payload in outcomes}) == 1
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT status,story_revision FROM interview_stories WHERE id=?",
            (story_id,),
        ).fetchone()
        transitions = connection.execute(
            "SELECT seq,state FROM write_operation_transitions WHERE operation_id=? "
            "ORDER BY seq",
            (next(iter(operation_ids)),),
        ).fetchall()
    assert story == ("archived", 2)
    assert transitions == [
        (1, "proposed"),
        (2, "approved"),
        (3, "claimed"),
        (4, "committed"),
    ]


def test_story_product_action_undo_execution_commit_unknown_reconciles_without_retrying_executor(
    story_client,
    monkeypatch,
) -> None:
    client, _data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-unknown-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    repository = client.app.state.interview_stories_repository
    original_executor = repository.undo_product_action_in_session
    executions = 0

    def counted_executor(*args: Any, **kwargs: Any) -> Any:
        nonlocal executions
        executions += 1
        return original_executor(*args, **kwargs)

    monkeypatch.setattr(
        repository,
        "undo_product_action_in_session",
        counted_executor,
    )
    original_commit = Session.commit
    commits = 0

    def lose_execution_response(self: Session) -> None:
        nonlocal commits
        commits += 1
        original_commit(self)
        if commits == 2:
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", lose_execution_response)
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "committed"
    assert response.json()["replayed"] is True
    assert executions == 1


def test_story_product_action_undo_proof_rejects_owner_aba(story_client) -> None:
    client, _data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-product-action-undo-aba-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    story_id = confirmed["story_id"]
    proof = client.app.state.interview_story_undo_issuer.issue(
        story_id=story_id,
        parent_operation_id=attempt["product_action"]["operation_id"],
    )
    archived = client.post(
        f"/api/interview-stories/{story_id}/archive",
        json={"expected_story_revision": 1},
    )
    assert archived.status_code == 200
    restored = client.post(
        f"/api/interview-stories/{story_id}/restore",
        json={"expected_story_revision": 2},
    )
    assert restored.status_code == 200
    with pytest.raises(ProductActionIntegrityError) as captured:
        client.app.state.product_action_compensation_coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_owner_snapshot"


def test_story_product_action_undo_fails_closed_on_source_attempt_lineage_corruption(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key="story-product-action-undo-lineage-0001",
        ),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET confirmed_story_id=? WHERE id=?",
            (confirmed["story_id"] + 1, attempt["id"]),
        )
        connection.commit()
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error_code": "operation_result_unknown",
        "retryable": True,
    }


def test_story_undo_direct_capability_toctou_retires_unclaimed_proof(
    story_client,
) -> None:
    client, _data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-direct-retire-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    issuer = client.app.state.interview_story_undo_issuer
    coordinator = client.app.state.product_action_compensation_coordinator
    proof = issuer.issue(
        story_id=confirmed["story_id"],
        parent_operation_id=attempt["product_action"]["operation_id"],
    )
    coordinator._capability_check = lambda _capability: False
    with pytest.raises(ProductActionCompensationError) as denied:
        coordinator.execute(proof)
    assert denied.value.code == "product_action_compensation_permission_denied"
    for operation in (
        lambda: issuer._proof_registry.peek(proof),
        lambda: issuer._proof_registry.claim(proof),
        lambda: coordinator.execute(proof),
    ):
        with pytest.raises(ProductActionCompensationError) as retired:
            operation()
        assert retired.value.code == "product_action_compensation_proof_invalid"


def test_story_undo_route_capability_toctou_retires_issued_proof(
    story_client,
    monkeypatch,
) -> None:
    client, _data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-route-retire-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    issuer = client.app.state.interview_story_undo_issuer
    coordinator = client.app.state.product_action_compensation_coordinator
    original_issue = issuer.issue
    captured: list[Any] = []

    def issue_then_revoke_capability(**kwargs: Any) -> Any:
        proof = original_issue(**kwargs)
        captured.append(proof)
        coordinator._capability_check = lambda _capability: False
        return proof

    monkeypatch.setattr(issuer, "issue", issue_then_revoke_capability)
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert response.status_code == 403
    assert len(captured) == 1
    with pytest.raises(ProductActionCompensationError) as retired:
        issuer._proof_registry.peek(captured[0])
    assert retired.value.code == "product_action_compensation_proof_invalid"


def test_story_undo_preclaim_owner_failure_retires_issued_proof(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-owner-retire-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    issuer = client.app.state.interview_story_undo_issuer
    coordinator = client.app.state.product_action_compensation_coordinator
    proof = issuer.issue(
        story_id=confirmed["story_id"],
        parent_operation_id=attempt["product_action"]["operation_id"],
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET confirmed_story_id=? WHERE id=?",
            (confirmed["story_id"] + 1, attempt["id"]),
        )
        connection.commit()
    with pytest.raises(ProductActionIntegrityError):
        coordinator.execute(proof)
    for operation in (
        lambda: issuer._proof_registry.peek(proof),
        lambda: issuer._proof_registry.claim(proof),
        lambda: coordinator.execute(proof),
    ):
        with pytest.raises(ProductActionCompensationError) as retired:
            operation()
        assert retired.value.code == "product_action_compensation_proof_invalid"


def _tamper_previous_story_version(
    data_dir: Any,
    previous_version_id: int,
    tamper: str,
) -> None:
    with sqlite3.connect(data_dir / "data.db") as connection:
        if tamper == "content":
            raw = connection.execute(
                "SELECT content_json FROM interview_story_versions WHERE id=?",
                (previous_version_id,),
            ).fetchone()[0]
            content = json.loads(raw)
            content["blocks"][0]["text"] += " tampered"
            rewritten = canonical_json(content)
            connection.execute(
                "UPDATE interview_story_versions SET content_json=?,content_hash=? WHERE id=?",
                (rewritten, sha256_text(rewritten), previous_version_id),
            )
        elif tamper == "assertion":
            row = connection.execute(
                "SELECT id,statement_text FROM interview_story_user_assertions "
                "WHERE story_version_id=? ORDER BY id LIMIT 1",
                (previous_version_id,),
            ).fetchone()
            assert row is not None
            statement = row[1] + " tampered"
            connection.execute(
                "UPDATE interview_story_user_assertions SET statement_text=?,statement_hash=? "
                "WHERE id=?",
                (statement, sha256_text(statement), row[0]),
            )
        elif tamper == "timestamp":
            connection.execute(
                "UPDATE interview_story_versions SET confirmed_at=? WHERE id=?",
                ("2030-01-01 00:00:00.000000", previous_version_id),
            )
        else:
            row = connection.execute(
                "SELECT id,target_kind,target_id,source_kind,source_stable_id,"
                "source_version_or_snapshot,source_path,text_location,excerpt,source_fingerprint "
                "FROM interview_story_version_evidence_links WHERE story_version_id=? "
                "ORDER BY id LIMIT 1",
                (previous_version_id,),
            ).fetchone()
            assert row is not None
            identity = {
                "target_kind": row[1],
                "target_id": row[2],
                "source_kind": row[3],
                "source_stable_id": row[4],
                "source_version_or_snapshot": row[5],
                "source_path": row[6],
                "text_location": row[7],
                "excerpt": row[8] + " tampered",
                "source_fingerprint": row[9],
            }
            connection.execute(
                "UPDATE interview_story_version_evidence_links SET excerpt=?,link_hash=? "
                "WHERE id=?",
                (
                    identity["excerpt"],
                    sha256_text(canonical_json(identity)),
                    row[0],
                ),
            )
        connection.commit()


@pytest.mark.parametrize(
    ("phase", "tamper"),
    (
        ("issue", "content"),
        ("execution", "assertion"),
        ("replay", "evidence"),
        ("replay", "timestamp"),
    ),
)
def test_appended_story_undo_binds_full_previous_immutable_aggregate(
    story_client,
    phase: str,
    tamper: str,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix=f"story-undo-previous-{phase}",
    )
    issuer = client.app.state.interview_story_undo_issuer
    coordinator = client.app.state.product_action_compensation_coordinator
    proof = None
    if phase == "execution":
        proof = issuer.issue(
            story_id=seeded["story_id"],
            parent_operation_id=seeded["parent_operation_id"],
        )
    if phase == "replay":
        first = client.post(
            f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
            json={"parent_operation_id": seeded["parent_operation_id"]},
        )
        assert first.status_code == 200 and first.json()["status"] == "committed"
    _tamper_previous_story_version(
        data_dir,
        int(seeded["previous_version_id"]),
        tamper,
    )
    if phase == "execution":
        with pytest.raises(ProductActionIntegrityError):
            coordinator.execute(proof)
    else:
        response = client.post(
            f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
            json={"parent_operation_id": seeded["parent_operation_id"]},
        )
        assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT status,current_version_id,story_revision FROM interview_stories WHERE id=?",
            (seeded["story_id"],),
        ).fetchone()
        compensations = connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["parent_operation_id"],),
        ).fetchone()[0]
    if phase == "replay":
        assert story == ("active", seeded["previous_version_id"], 3)
        assert compensations == 1
    else:
        assert story == ("active", seeded["created_version_id"], 2)
        assert compensations == 0


@pytest.mark.parametrize(
    ("section", "field"),
    (
        ("result", "schema_version"),
        ("undo", "story_id"),
        ("undo", "created_version_id"),
        ("undo", "expected_current_version_id"),
        ("undo", "expected_story_revision"),
    ),
)
def test_story_undo_rejects_bool_numeric_parent_codec_before_compensation_write(
    story_client,
    section: str,
    field: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-undo-bool-{section}-{field}-0001",
        ),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    operation_id = attempt["product_action"]["operation_id"]
    with sqlite3.connect(data_dir / "data.db") as connection:
        row = connection.execute(
            "SELECT result_contract,result_json,visible_result,transport_json,undo_json,"
            "failure_category,failure_code FROM write_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        assert row is not None
        result = json.loads(row[1])
        undo = json.loads(row[4])
        (result if section == "result" else undo)[field] = True
        payload = build_terminal_payload(
            status="committed",
            result_contract=row[0],
            result=result,
            visible_result=row[2],
            transport=json.loads(row[3]),
            undo=undo,
            failure_category=row[5],
            failure_code=row[6],
        )
        connection.execute("DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable")
        connection.execute(
            "UPDATE write_operations SET result_json=?,undo_json=?,terminal_payload_sha256=? "
            "WHERE id=?",
            (payload.result_json, payload.undo_json, payload.digest, operation_id),
        )
        connection.commit()
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": operation_id},
    )
    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    "tamper",
    ("version_content", "evidence", "assertion", "confirmation_hash"),
)
def test_story_undo_recomputes_exact_confirmed_effective_payload_hash(
    story_client,
    tamper: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-undo-effective-{tamper}-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        if tamper == "version_content":
            raw = connection.execute(
                "SELECT content_json FROM interview_story_versions WHERE id=?",
                (confirmed["version_id"],),
            ).fetchone()[0]
            content = json.loads(raw)
            content["blocks"][0]["text"] += " coherent rewrite"
            rewritten = canonical_json(content)
            connection.execute(
                "UPDATE interview_story_versions SET content_json=?,content_hash=? WHERE id=?",
                (rewritten, sha256_text(rewritten), confirmed["version_id"]),
            )
        elif tamper == "confirmation_hash":
            connection.execute(
                "UPDATE interview_story_proposal_attempts SET confirmation_payload_hash=? "
                "WHERE id=?",
                ("sha256:" + "a" * 64, attempt["id"]),
            )
        connection.commit()
    if tamper in {"evidence", "assertion"}:
        _tamper_previous_story_version(
            data_dir,
            confirmed["version_id"],
            tamper,
        )
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone()[0]
    assert count == 0


def test_story_undo_rejects_title_current_version_relation_corruption(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-title-relation-0001"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_stories SET title=? WHERE id=?",
            ("coherent-looking wrong title", confirmed["story_id"]),
        )
        connection.commit()
    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )
    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone()[0]
    assert count == 0


@pytest.mark.parametrize("phase", ("issue", "execution"))
def test_story_undo_rejects_active_story_with_archived_timestamp_without_writes(
    story_client,
    phase: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-undo-active-archived-at-{phase}"),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    issuer = client.app.state.interview_story_undo_issuer
    proof = None
    if phase == "execution":
        proof = issuer.issue(
            story_id=confirmed["story_id"],
            parent_operation_id=attempt["product_action"]["operation_id"],
        )
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_stories SET archived_at=updated_at WHERE id=?",
            (confirmed["story_id"],),
        )
        connection.commit()
    if phase == "execution":
        with pytest.raises(ProductActionIntegrityError):
            client.app.state.product_action_compensation_coordinator.execute(proof)
    else:
        response = client.post(
            f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
            json={"parent_operation_id": attempt["product_action"]["operation_id"]},
        )
        assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone() == (0,)


@pytest.mark.parametrize("archived_at", (None, "1999-01-01 00:00:00.000000"))
def test_story_undo_rejects_corrupt_archived_terminal_lifecycle_without_writes(
    story_client,
    archived_at: str | None,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-undo-archived-corrupt-{archived_at is None}-0001",
        ),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        connection.execute(
            "UPDATE interview_stories SET status='archived',story_revision=2,archived_at=? "
            "WHERE id=?",
            (archived_at, confirmed["story_id"]),
        )
        connection.commit()

    response = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json={"parent_operation_id": attempt["product_action"]["operation_id"]},
    )

    assert response.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone() == (0,)


@pytest.mark.parametrize("archived_at", (None, "1999-01-01 00:00:00.000000"))
def test_story_undo_terminal_replay_revalidates_archived_timestamp(
    story_client,
    archived_at: str | None,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-undo-replay-archived-{archived_at is None}-0001",
        ),
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    ).json()
    request = {"parent_operation_id": attempt["product_action"]["operation_id"]}
    first = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json=request,
    )
    assert first.status_code == 200, first.json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM write_operation_transitions WHERE operation_id=?",
            (first.json()["operation_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE interview_stories SET archived_at=? WHERE id=?",
            (archived_at, confirmed["story_id"]),
        )
        connection.commit()

    replay = client.post(
        f"/api/interview-stories/{confirmed['story_id']}/product-action-undo",
        json=request,
    )

    assert replay.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operation_transitions WHERE operation_id=?",
            (first.json()["operation_id"],),
        ).fetchone() == before


def test_story_undo_maps_a_later_version_edit_to_stale_without_mutating_history(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-later-version",
    )
    note = _note(client)
    third_request = _proposal_request(
        note["id"],
        key="story-undo-later-version-third-0001",
    )
    third_request.update(
        {
            "target_story_id": seeded["story_id"],
            "expected_current_version_id": seeded["created_version_id"],
            "expected_story_revision": 2,
        }
    )
    third = client.post(
        "/api/interview-story-proposals",
        json=third_request,
    ).json()
    third_confirmation = _confirmation_from_attempt(third)
    third_confirmation["expected_current_version_id"] = seeded["created_version_id"]
    third_confirmation["expected_story_revision"] = 2
    latest = client.post(
        f"/api/interview-story-proposals/{third['id']}/confirm",
        json=third_confirmation,
    )
    assert latest.status_code == 201, latest.json()
    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "failed"
    assert response.json()["result"] == {
        "kind": "interview_story_undo_applied_v1",
        "code": "interview_story_undo_stale",
    }
    with sqlite3.connect(data_dir / "data.db") as connection:
        story = connection.execute(
            "SELECT current_version_id,story_revision,status FROM interview_stories WHERE id=?",
            (seeded["story_id"],),
        ).fetchone()
    assert story == (latest.json()["version_id"], 3, "active")


def test_story_undo_accepts_exact_later_manual_version_as_stale(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-later-manual",
    )
    note = _note(client)
    materialized = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-undo-later-manual-source-0001"),
    ).json()
    confirmation = _confirmation_from_attempt(materialized)
    later = client.app.state.interview_stories_repository.create_manual_version(
        story_id=seeded["story_id"],
        content={**confirmation["content"], "title": "Later manual title"},
        evidence_links=confirmation["evidence_links"],
        selections=[
            {
                "source_kind": "interview_note",
                "source_id": note["id"],
                "path": "/questions",
            }
        ],
        assertions=["I owned this incident response."],
        expected_current_version_id=seeded["created_version_id"],
        expected_story_revision=2,
        idempotency_key="story-undo-later-manual-save-0001",
    )

    response = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "failed"
    assert response.json()["result"]["code"] == "interview_story_undo_stale"
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT current_version_id,story_revision,title,status FROM interview_stories "
            "WHERE id=?",
            (seeded["story_id"],),
        ).fetchone() == (
            later["current_version_id"],
            3,
            "Later manual title",
            "active",
        )


def test_story_undo_accepts_exact_later_restore_compensation_as_stale(
    story_client,
) -> None:
    client, _data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-later-compensation",
    )
    restore = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["parent_operation_id"]},
    )
    assert restore.status_code == 200, restore.json()
    assert restore.json()["status"] == "committed"

    stale = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json={"parent_operation_id": seeded["base_parent_operation_id"]},
    )

    assert stale.status_code == 200, stale.json()
    assert stale.json()["status"] == "failed"
    assert stale.json()["result"]["code"] == "interview_story_undo_stale"


def test_story_undo_failed_terminal_replay_revalidates_later_lineage(
    story_client,
) -> None:
    client, data_dir = story_client
    seeded = _confirmed_appended_story(
        client,
        key_prefix="story-undo-failed-replay-lineage",
    )
    request = {"parent_operation_id": seeded["base_parent_operation_id"]}
    failed = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )
    assert failed.status_code == 200, failed.json()
    assert failed.json()["status"] == "failed"
    with sqlite3.connect(data_dir / "data.db") as connection:
        raw = connection.execute(
            "SELECT input_snapshot_json FROM interview_story_proposal_attempts WHERE id=?",
            (seeded["attempt_id"],),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["request_fingerprint"] = "0" * 64
        connection.execute(
            "UPDATE interview_story_proposal_attempts SET input_snapshot_json=? WHERE id=?",
            (canonical_json(payload), seeded["attempt_id"]),
        )
        connection.commit()

    replay = client.post(
        f"/api/interview-stories/{seeded['story_id']}/product-action-undo",
        json=request,
    )

    assert replay.status_code == 503
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations WHERE operation_role='compensation' "
            "AND parent_operation_id=?",
            (seeded["base_parent_operation_id"],),
        ).fetchone() == (1,)


def test_story_confirm_uses_server_token_and_freezes_direct_and_replay_http_projection(
    story_client,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-direct-product-action-0001"),
    ).json()
    request = _confirmation_from_attempt(attempt)

    direct = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )

    assert direct.status_code == 201
    assert direct.json()["created"] is True
    assert replay.status_code == 200
    assert replay.json() == {**direct.json(), "created": False}
    forged = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json={**request, "confirmation_token": "0" * 64},
    )
    assert forged.status_code == 409
    assert forged.json()["error_code"] == "story_idempotency_conflict"


def test_rejected_story_generation_requires_explicit_n_plus_one_and_rotates_token(
    story_client,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-reject-product-action-0001"),
    ).json()
    old = attempt["product_action"]
    rejected = client.post(
        f"/api/product-actions/{old['operation_id']}/decisions",
        json={"confirmation_token": old["confirmation_token"], "decision": "reject"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    rejected_owner = client.get(
        f"/api/interview-story-proposals/{attempt['id']}"
    )
    assert rejected_owner.status_code == 200
    assert rejected_owner.json()["product_action"]["status"] == "rejected"
    assert "confirmation_token" not in rejected_owner.json()["product_action"]

    next_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )

    assert next_response.status_code == 201
    next_action = next_response.json()
    assert next_action["contract"] == "story_product_action_proposal_response_v1"
    assert next_action["product_action_generation"] == 2
    assert next_action["proposal_created"] is True
    assert next_action["operation_id"] != old["operation_id"]
    assert next_action["confirmation_token"] != old["confirmation_token"]
    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert replay.status_code == 200
    assert replay.json() == {**next_action, "proposal_created": False}
    old_token = client.post(
        f"/api/product-actions/{next_action['operation_id']}/decisions",
        json={"confirmation_token": old["confirmation_token"], "decision": "approve"},
    )
    assert old_token.status_code == 409
    current_attempt = client.get(
        f"/api/interview-story-proposals/{attempt['id']}"
    ).json()
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(current_attempt),
    )
    assert confirmed.status_code == 201
    terminal_replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert terminal_replay.status_code == 200
    assert terminal_replay.json()["proposal_created"] is False
    assert terminal_replay.json()["status"] == "committed"
    assert "confirmation_token" not in terminal_replay.json()
    refused_after_confirm = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 2,
        },
    )
    assert refused_after_confirm.status_code == 409


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_n_plus_one_exact_integers_are_rejected_before_attempt_lookup(
    story_client,
    value: object,
) -> None:
    client, data_dir = story_client
    response = client.post(
        "/api/interview-story-proposals/999/product-actions",
        json={
            "expected_generation_revision": value,
            "expected_product_action_generation": 1,
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_story_invalid_request"
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (0,)


def test_story_raw_contract_rejects_duplicate_keys_before_lookup(story_client) -> None:
    client, data_dir = story_client
    raw = (
        b'{"expected_generation_revision":1,"expected_generation_revision":1,'
        b'"expected_product_action_generation":1}'
    )
    response = client.post(
        "/api/interview-story-proposals/999/product-actions",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "interview_story_invalid_request"
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (0,)


def test_invalid_raw_story_requests_make_zero_domain_calls(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = story_client
    repository = client.app.state.interview_stories_repository
    confirmation_calls = 0
    next_calls = 0

    def confirmation_spy(**_kwargs: Any) -> Any:
        nonlocal confirmation_calls
        confirmation_calls += 1
        raise AssertionError("invalid confirmation reached the domain")

    def next_spy(**_kwargs: Any) -> Any:
        nonlocal next_calls
        next_calls += 1
        raise AssertionError("invalid N+1 request reached the domain")

    monkeypatch.setattr(repository, "prepare_confirmation_decision", confirmation_spy)
    monkeypatch.setattr(repository, "create_next_product_action", next_spy)
    invalid_confirmation = client.post(
        "/api/interview-story-proposals/999/confirm",
        content=(
            b'{"confirmation_token":"story-invalid-raw-token-0001",'
            b'"content":{},"evidence_links":[],'
            b'"expected_current_version_id":true,'
            b'"expected_story_revision":null}'
        ),
        headers={"content-type": "application/json"},
    )
    duplicate_next = client.post(
        "/api/interview-story-proposals/999/product-actions",
        content=(
            b'{"expected_generation_revision":1,'
            b'"expected_product_action_generation":1,'
            b'"expected_product_action_generation":1}'
        ),
        headers={"content-type": "application/json"},
    )
    assert invalid_confirmation.status_code == 422
    assert duplicate_next.status_code == 422
    assert confirmation_calls == 0
    assert next_calls == 0


def test_bound_story_executor_rolls_back_with_caller_transaction(tmp_path: Any) -> None:
    # The session-bound cutover deliberately has no self-committing confirm_attempt API.
    from offerpilot.db import init_database
    from offerpilot.product_actions.contracts import ProductActionProofRegistryV1
    from offerpilot.repositories.interview_stories import InterviewStoriesRepository

    factory = init_database(tmp_path / "bound-story.db")
    repository = InterviewStoriesRepository(factory)
    assert not hasattr(repository, "confirm_attempt")
    assert hasattr(repository, "confirm_attempt_bound")
    assert isinstance(ProductActionProofRegistryV1(), ProductActionProofRegistryV1)
    factory.kw["bind"].dispose()


def test_historical_ready_bridge_uses_legacy_token_only_as_request_identity(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    _legacy, attempt = _legacy_ready_attempt(
        client,
        note_id=note["id"],
        key="story-historical-ready-seed-0001",
    )
    legacy_token = "story-historical-ready-token-0001"
    request = _confirmation_from_attempt(attempt, token=legacy_token)
    coordinator = client.app.state.product_action_coordinator
    original_decide = coordinator.decide
    observed_tokens: list[str] = []

    def observe_decide(*, operation_id: str, request: dict[str, Any]):
        observed_tokens.append(request["confirmation_token"])
        return original_decide(operation_id=operation_id, request=request)

    monkeypatch.setattr(coordinator, "decide", observe_decide)
    direct = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )

    assert direct.status_code == 201, direct.json()
    assert direct.json()["created"] is True
    assert replay.status_code == 200
    assert replay.json() == {**direct.json(), "created": False}
    assert observed_tokens and observed_tokens[0] != legacy_token
    assert len(observed_tokens[0]) == 64
    with sqlite3.connect(data_dir / "data.db") as connection:
        route = connection.execute(
            "SELECT request_origin, historical_request_token_fingerprint "
            "FROM product_action_proposals"
        ).fetchone()
        pointer = connection.execute(
            "SELECT product_action_generation, product_action_operation_id "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (attempt["id"],),
        ).fetchone()
    assert route[0] == "historical_story_bridge"
    assert route[1].startswith("hmac-sha256:")
    assert pointer[0] == 1 and pointer[1]
    different_token = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json={**request, "confirmation_token": "story-historical-ready-token-0002"},
    )
    assert different_token.status_code == 409
    assert different_token.json()["error_code"] == "story_idempotency_conflict"
    changed_payload = dict(request)
    changed_payload["content"] = {
        **request["content"],
        "title": "Different historical payload",
    }
    different_payload = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=changed_payload,
    )
    assert different_payload.status_code == 409
    assert different_payload.json()["error_code"] == "story_idempotency_conflict"


def test_historical_ready_failed_bridge_replays_frozen_conflict_and_hashes_payload(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = story_client
    note = _note(client)
    _legacy, attempt = _legacy_ready_attempt(
        client,
        note_id=note["id"],
        key="story-historical-failed-seed-0001",
    )
    repository = client.app.state.interview_stories_repository

    def declared(
        _session: Any,
        _trusted: Any,
        authorization: ProductActionExecutionAuthorization,
        *,
        authorization_binding: tuple[object, ...],
    ) -> Any:
        with repository._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            expected_binding=authorization_binding,
        ):
            raise ProductActionStoryWriteConflict()

    monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    request = _confirmation_from_attempt(
        attempt,
        token="story-historical-failed-token-0001",
    )
    direct = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    same = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    changed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json={
            **request,
            "content": {**request["content"], "title": "changed failed payload"},
        },
    )

    assert direct.status_code == 409
    assert direct.json()["error_code"] == "product_action_story_write_conflict"
    assert same.status_code == 409
    assert same.json()["error_code"] == "product_action_story_write_conflict"
    assert changed.status_code == 409
    assert changed.json()["error_code"] == "story_idempotency_conflict"


@pytest.mark.parametrize("malformed_terminal", [False, True])
def test_historical_rejected_legacy_retry_is_stable_and_codec_verified(
    story_client,
    malformed_terminal: bool,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    _legacy, attempt = _legacy_ready_attempt(
        client,
        note_id=note["id"],
        key=f"story-historical-rejected-{malformed_terminal}-seed-0001",
    )
    repository = client.app.state.interview_stories_repository
    legacy_token = f"story-historical-rejected-{malformed_terminal}-token-0001"
    request = _confirmation_from_attempt(attempt, token=legacy_token)
    with repository._session_factory() as session:
        row = session.get(InterviewStoryProposalAttempt, attempt["id"])
        assert row is not None
        route_raw = repository._story_route_payload(
            row,
            proposal_hash=row.proposal_hash,
            product_action_generation=1,
        )
    publication = repository._publish_historical_story_bridge(
        attempt_id=attempt["id"],
        route_payload_raw=route_raw,
        legacy_confirmation_token=legacy_token,
    )
    assert publication.confirmation_token is not None
    rejected = client.post(
        f"/api/product-actions/{publication.operation_id}/decisions",
        json={
            "confirmation_token": publication.confirmation_token,
            "decision": "reject",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    if malformed_terminal:
        with sqlite3.connect(data_dir / "data.db") as connection:
            persisted = connection.execute(
                "SELECT result_contract,result_json,visible_result,transport_json,"
                "failure_category,failure_code FROM write_operations WHERE id=?",
                (publication.operation_id,),
            ).fetchone()
            assert persisted is not None
            result = json.loads(persisted[1])
            transport = json.loads(persisted[3])
            result["unexpected_field"] = "must-not-leak"
            transport["unexpected_field"] = "must-not-leak"
            payload = build_terminal_payload(
                status="rejected",
                result_contract=persisted[0],
                result=result,
                visible_result=persisted[2],
                transport=transport,
                undo=None,
                failure_category=persisted[4],
                failure_code=persisted[5],
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"
            )
            connection.execute(
                "UPDATE write_operations SET result_json=?,transport_json=?,"
                "terminal_payload_sha256=? WHERE id=?",
                (
                    payload.result_json,
                    payload.transport_json,
                    payload.digest,
                    publication.operation_id,
                ),
            )
            connection.commit()

    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    if malformed_terminal:
        assert replay.status_code == 503
        assert replay.json() == {
            "error_code": "operation_result_unknown",
            "retryable": True,
        }
        assert "must-not-leak" not in replay.text
    else:
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "story_idempotency_conflict"
        assert "unexpected_field" not in replay.text


def test_historical_confirmed_attempt_is_read_only_replay_without_product_action(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    legacy, attempt = _legacy_ready_attempt(
        client,
        note_id=note["id"],
        key="story-historical-confirmed-seed-0001",
    )
    token = "story-historical-confirmed-token-0001"
    request = _confirmation_from_attempt(attempt, token=token)
    story = legacy.create_manual_story(
        content=request["content"],
        evidence_links=request["evidence_links"],
        selections=[
            {
                "source_kind": "interview_note",
                "source_id": note["id"],
                "path": "/questions",
            }
        ],
        assertions=["I owned this incident response."],
        expected_current_version_id=None,
        idempotency_key="story-historical-confirmed-story-0001",
    )
    with legacy._session_factory() as session:
        row = session.get(InterviewStoryProposalAttempt, attempt["id"])
        assert row is not None
        row.attempt_status = "confirmed"
        row.confirmation_token_hash = sha256_text(token)
        row.confirmation_payload_hash = sha256_text(
            canonical_json(
                {
                    "content": request["content"],
                    "evidence_links": request["evidence_links"],
                }
            )
        )
        row.confirmed_story_id = story["id"]
        row.confirmed_story_version_id = story["current_version_id"]
        row.confirmed_at = datetime.now(timezone.utc)
        session.commit()
    with sqlite3.connect(data_dir / "data.db") as connection:
        count_before = connection.execute(
            "SELECT COUNT(*) FROM write_operations"
        ).fetchone()[0]
    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json() == {
        "story_id": story["id"],
        "version_id": story["current_version_id"],
        "created": False,
    }
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM write_operations"
        ).fetchone()[0] == count_before


def test_story_attempt_payload_never_exposes_terminal_confirmation_token(story_client) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-token-absence-terminal-0001"),
    ).json()
    request = _confirmation_from_attempt(attempt)
    assert client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    ).status_code == 201
    terminal = client.get(f"/api/interview-story-proposals/{attempt['id']}")
    assert terminal.status_code == 200
    assert terminal.json()["attempt_status"] == "confirmed"
    assert "product_action" not in terminal.json()
    assert terminal.json()["product_action_generation"] == 1


def test_story_source_change_recovers_rejection_only_control(story_client) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-source-change-recovery-0001"),
    ).json()
    original = attempt["product_action"]
    repository = client.app.state.interview_stories_repository
    with repository._session_factory() as session:
        changed = session.get(InterviewNote, note["id"])
        assert changed is not None
        changed.questions = "The selected Story source changed."
        session.commit()

    recovered = client.get(f"/api/interview-story-proposals/{attempt['id']}")

    assert recovered.status_code == 200, recovered.json()
    control = recovered.json()["product_action"]
    assert control["operation_id"] == original["operation_id"]
    assert control["confirmation_token"] != original["confirmation_token"]
    assert control["rejection_only"] is True
    assert control["allowed_decisions"] == ["reject"]
    approve = client.post(
        f"/api/product-actions/{original['operation_id']}/decisions",
        json={"confirmation_token": control["confirmation_token"], "decision": "approve"},
    )
    assert approve.status_code == 409
    rejected = client.post(
        f"/api/product-actions/{original['operation_id']}/decisions",
        json={"confirmation_token": control["confirmation_token"], "decision": "reject"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_story_declared_write_conflict_terminalizes_failed_from_savepoint(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-declared-write-conflict-0001"),
    ).json()
    repository = client.app.state.interview_stories_repository

    def declared(
        _session: Any,
        _trusted: Any,
        authorization: ProductActionExecutionAuthorization,
        *,
        authorization_binding: tuple[object, ...],
    ) -> Any:
        with repository._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            expected_binding=authorization_binding,
        ):
            raise ProductActionStoryWriteConflict()

    monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    result = client.app.state.product_action_coordinator.decide(
        operation_id=attempt["product_action"]["operation_id"],
        request={
            "confirmation_token": attempt["product_action"]["confirmation_token"],
            "decision": "approve",
        },
    )
    assert result.status == "failed"
    assert result.result["code"] == "product_action_story_write_conflict"
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM interview_stories").fetchone() == (0,)
        operation = connection.execute(
            "SELECT status, failure_code FROM write_operations WHERE id=?",
            (attempt["product_action"]["operation_id"],),
        ).fetchone()
    assert operation == ("failed", "product_action_story_write_conflict")
    state = client.app.state.product_action_coordinator.get_state(
        operation_id=attempt["product_action"]["operation_id"]
    )
    assert state.status == "failed"
    assert state.result["code"] == "product_action_story_write_conflict"
    failed_owner = client.get(f"/api/interview-story-proposals/{attempt['id']}")
    assert failed_owner.status_code == 200
    assert failed_owner.json()["product_action"]["status"] == "failed"
    assert "confirmation_token" not in failed_owner.json()["product_action"]
    refused = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert refused.status_code == 409
    raw_legacy = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(attempt),
    )
    assert raw_legacy.status_code == 409
    assert raw_legacy.json()["error_code"] == "product_action_story_write_conflict"


def test_story_unrecognized_executor_failure_rolls_back_to_proposed(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-unrecognized-write-failure-0001"),
    ).json()
    repository = client.app.state.interview_stories_repository

    def unexpected(
        _session: Any,
        _trusted: Any,
        authorization: ProductActionExecutionAuthorization,
        *,
        authorization_binding: tuple[object, ...],
    ) -> Any:
        with repository._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            expected_binding=authorization_binding,
        ):
            raise RuntimeError("unexpected Story executor failure")

    monkeypatch.setattr(repository, "confirm_attempt_bound", unexpected)
    with pytest.raises(RuntimeError, match="unexpected Story executor failure"):
        client.app.state.product_action_coordinator.decide(
            operation_id=attempt["product_action"]["operation_id"],
            request={
                "confirmation_token": attempt["product_action"]["confirmation_token"],
                "decision": "approve",
            },
        )
    with sqlite3.connect(data_dir / "data.db") as connection:
        operation = connection.execute(
            "SELECT status, operation_request_fingerprint FROM write_operations WHERE id=?",
            (attempt["product_action"]["operation_id"],),
        ).fetchone()
        transitions = connection.execute(
            "SELECT seq, state FROM write_operation_transitions WHERE operation_id=? ORDER BY seq",
            (attempt["product_action"]["operation_id"],),
        ).fetchall()
    assert operation == ("proposed", None)
    assert transitions == [(1, "proposed")]


def test_bound_story_executor_consumes_auth_and_leaves_commit_or_rollback_to_caller(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-bound-executor-real-0001"),
    ).json()
    repository = client.app.state.interview_stories_repository
    operation_id = attempt["product_action"]["operation_id"]
    bundle = client.app.state.product_action_proposal_repository.load_bundle(operation_id)
    route_payload = json.loads(bundle.route.route_payload_json)
    handler = InterviewStoryProductActionHandler(repository)
    trusted = handler.external_preflight(
        route_payload,
        SimpleNamespace(decision="approve", edited_payload=None),
    ).trusted_source

    def authorization(binding: tuple[object, ...]) -> ProductActionExecutionAuthorization:
        return repository._proof_registry._issue(
            ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            binding=binding,
        )

    rollback_binding = (operation_id, "rollback", "request", "payload", "scope")
    with repository._session_factory() as session:
        session.begin()
        result = repository.confirm_attempt_bound(
            session,
            trusted,
            authorization(rollback_binding),
            authorization_binding=rollback_binding,
        )
        assert result.outcome == "created"
        assert session.in_transaction()
        session.rollback()
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM interview_stories").fetchone() == (0,)
        assert connection.execute(
            "SELECT attempt_status FROM interview_story_proposal_attempts WHERE id=?",
            (attempt["id"],),
        ).fetchone() == ("ready",)

    commit_binding = (operation_id, "commit", "request", "payload", "scope")
    with repository._session_factory() as session:
        session.begin()
        committed = repository.confirm_attempt_bound(
            session,
            trusted,
            authorization(commit_binding),
            authorization_binding=commit_binding,
        )
        assert session.in_transaction()
        session.commit()
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM interview_stories").fetchone() == (1,)
        assert connection.execute(
            "SELECT attempt_status,confirmed_story_id,confirmed_story_version_id "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (attempt["id"],),
        ).fetchone() == ("confirmed", committed.story_id, committed.version_id)


def test_twenty_way_n_plus_one_has_one_creator_and_one_frozen_identity(story_client) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-n-plus-one-twenty-way-0001"),
    ).json()
    current = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{current['operation_id']}/decisions",
        json={"confirmation_token": current["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    repository = client.app.state.interview_stories_repository

    def advance(_index: int):
        return repository.create_next_product_action(
            attempt_id=attempt["id"],
            expected_generation_revision=attempt["generation_revision"],
            expected_product_action_generation=1,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(advance, range(20)))
    assert sum(result.proposal_created for result in results) == 1
    assert len({result.operation_id for result in results}) == 1
    assert len({result.action_call_id for result in results}) == 1
    assert len({result.confirmation_token for result in results}) == 1
    assert all(result.product_action_generation == 2 for result in results)


def test_late_n_plus_one_request_replays_pointer_identity_after_active_key_rotation(
    story_client,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-next-key-rotation-0001"),
    ).json()
    current = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{current['operation_id']}/decisions",
        json={"confirmation_token": current["confirmation_token"], "decision": "reject"},
    ).status_code == 200

    production = client.app.state.product_action_proposal_repository
    old_key = production._key_profiles.active()
    new_key = LedgerKeyDomain(
        "33333333-3333-4333-8333-333333333333",
        b"3" * 32,
    )
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)
    profiles = LedgerKeyProfileStoreV1(
        (old_key, new_key),
        active_key_id=old_key.key_id,
    )
    issuer = InterviewStoryActionIssuer(catalog, registry, profiles)
    proposals = ProductActionProposalRepository(
        production.session_factory,
        catalog=catalog,
        proof_registry=registry,
        key_profiles=profiles,
    )
    restarted_repository = InterviewStoriesRepository(
        production.session_factory,
        action_issuer=issuer,
        proposal_repository=proposals,
        proof_registry=registry,
    )
    first = restarted_repository.create_next_product_action(
        attempt_id=attempt["id"],
        expected_generation_revision=attempt["generation_revision"],
        expected_product_action_generation=1,
    )
    profiles.activate(new_key.key_id)

    late = restarted_repository.create_next_product_action(
        attempt_id=attempt["id"],
        expected_generation_revision=attempt["generation_revision"],
        expected_product_action_generation=1,
    )

    assert first.proposal_created is True
    assert late.proposal_created is False
    assert late.operation_id == first.operation_id
    assert late.action_call_id == first.action_call_id
    assert late.confirmation_token == first.confirmation_token


def test_rejected_n2_to_n3_replays_proposed_terminal_and_late_n2_history(
    story_client,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-next-later-pointer-0001"),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    second_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    second = second_response.json()
    assert second_response.status_code == 201
    assert client.post(
        f"/api/product-actions/{second['operation_id']}/decisions",
        json={"confirmation_token": second["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    third_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 2,
        },
    )
    third = third_response.json()
    assert third_response.status_code == 201

    proposed_replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 2,
        },
    )
    assert proposed_replay.status_code == 200
    assert proposed_replay.json()["operation_id"] == third["operation_id"]
    assert proposed_replay.json()["confirmation_token"] == third["confirmation_token"]
    assert proposed_replay.json()["proposal_created"] is False
    current = client.get(f"/api/interview-story-proposals/{attempt['id']}").json()
    assert client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(
            current,
            token=third["confirmation_token"],
        ),
    ).status_code == 201
    terminal_replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 2,
        },
    )
    late_n2 = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )

    assert terminal_replay.status_code == 200
    assert terminal_replay.json()["status"] == "committed"
    assert terminal_replay.json()["proposal_created"] is False
    assert "confirmation_token" not in terminal_replay.json()
    assert late_n2.status_code == 200
    assert late_n2.json()["operation_id"] == second["operation_id"]
    assert late_n2.json()["status"] == "rejected"
    assert late_n2.json()["proposal_created"] is False
    assert "confirmation_token" not in late_n2.json()


@pytest.mark.parametrize("current_pointer", ["exact_generation_2", "older_generation_1"])
def test_n3_publication_requires_exact_rejected_n2_pointer_and_route(
    story_client,
    current_pointer: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-n3-current-route-{current_pointer}-0001",
        ),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    second = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    ).json()
    assert client.post(
        f"/api/product-actions/{second['operation_id']}/decisions",
        json={"confirmation_token": second["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    if current_pointer == "older_generation_1":
        with sqlite3.connect(data_dir / "data.db") as connection:
            connection.execute(
                "UPDATE interview_story_proposal_attempts "
                "SET product_action_operation_id=? WHERE id=?",
                (first["operation_id"], attempt["id"]),
            )
            connection.commit()

    response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 2,
        },
    )
    if current_pointer == "exact_generation_2":
        assert response.status_code == 201
        assert response.json()["product_action_generation"] == 3
        assert response.json()["proposal_created"] is True
    else:
        assert response.status_code == 503
        assert response.json() == {
            "error_code": "operation_result_unknown",
            "retryable": True,
        }
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_action_proposals WHERE source_id=?",
            (attempt["id"],),
        ).fetchone() == ((3,) if current_pointer == "exact_generation_2" else (2,))


@pytest.mark.parametrize("corruption", ["pointer", "parent_route", "unreadable"])
def test_n_plus_one_fails_closed_on_pointer_or_bundle_integrity(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-next-{corruption}-0001"),
    ).json()
    current = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{current['operation_id']}/decisions",
        json={"confirmation_token": current["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    original = client.app.state.product_action_proposal_repository.load_bundle_in_session

    def corrupted(session: Session, uow: Any, operation_id: str):
        if operation_id == current["operation_id"]:
            code = {
                "pointer": "story_next_generation_pointer",
                "parent_route": "partial_product_action_bundle",
                "unreadable": "product_action_bundle_unreadable",
            }[corruption]
            raise ProductActionIntegrityError(code)
        return original(session, uow, operation_id)

    monkeypatch.setattr(
        client.app.state.product_action_proposal_repository,
        "load_bundle_in_session",
        corrupted,
    )

    response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "operation_result_unknown",
        "retryable": True,
    }
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_action_proposals WHERE source_id=?",
            (attempt["id"],),
        ).fetchone()[0] <= 1


@pytest.mark.parametrize("corruption", ["pointer", "parent", "route"])
def test_published_n_plus_one_fails_closed_on_persisted_corruption(
    story_client,
    corruption: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-next-post-{corruption}-0001"),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    second_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert second_response.status_code == 201
    second = second_response.json()
    with sqlite3.connect(data_dir / "data.db") as connection:
        if corruption == "pointer":
            connection.execute(
                "UPDATE interview_story_proposal_attempts "
                "SET product_action_operation_id=? WHERE id=?",
                (first["operation_id"], attempt["id"]),
            )
        elif corruption == "parent":
            connection.execute(
                "DROP TRIGGER trg_write_operation_scope_identity_immutable"
            )
            connection.execute(
                "UPDATE write_operations SET proposal_fingerprint=? WHERE id=?",
                ("hmac-sha256:" + "0" * 64, second["operation_id"]),
            )
        else:
            connection.execute(
                "DROP TRIGGER trg_product_action_route_identity_immutable"
            )
            connection.execute(
                "UPDATE product_action_proposals SET route_payload_fingerprint=? "
                "WHERE operation_id=?",
                ("hmac-sha256:" + "0" * 64, second["operation_id"]),
            )
        connection.commit()

    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )

    assert replay.status_code == 503
    assert replay.json() == {
        "error_code": "operation_result_unknown",
        "retryable": True,
    }


def test_failed_n_plus_one_replays_golden_terminal_and_legacy_conflict(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-next-failed-golden-0001"),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    next_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert next_response.status_code == 201
    second = next_response.json()
    repository = client.app.state.interview_stories_repository

    def declared(
        _session: Any,
        _trusted: Any,
        authorization: ProductActionExecutionAuthorization,
        *,
        authorization_binding: tuple[object, ...],
    ) -> Any:
        with repository._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            expected_binding=authorization_binding,
        ):
            raise ProductActionStoryWriteConflict()

    monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    failed = client.post(
        f"/api/product-actions/{second['operation_id']}/decisions",
        json={
            "confirmation_token": second["confirmation_token"],
            "decision": "approve",
        },
    )
    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    legacy = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=_confirmation_from_attempt(
            client.get(f"/api/interview-story-proposals/{attempt['id']}").json(),
            token=second["confirmation_token"],
        ),
    )

    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"
    assert replay.json()["proposal_created"] is False
    assert replay.json()["terminal_result"]["code"] == (
        "product_action_story_write_conflict"
    )
    assert "confirmation_token" not in replay.json()
    assert legacy.status_code == 409
    assert legacy.json()["error_code"] == "product_action_story_write_conflict"


@pytest.mark.parametrize("terminal", ["committed", "rejected", "failed"])
def test_n_plus_one_replay_rejects_coherent_action_local_terminal_canary(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-terminal-canary-{terminal}-0001",
        ),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    second = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    ).json()
    repository = client.app.state.interview_stories_repository
    if terminal == "failed":

        def declared(
            _session: Any,
            _trusted: Any,
            authorization: ProductActionExecutionAuthorization,
            *,
            authorization_binding: tuple[object, ...],
        ) -> Any:
            with repository._proof_registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="confirm_interview_story",
                expected_binding=authorization_binding,
            ):
                raise ProductActionStoryWriteConflict()

        monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    decision = "reject" if terminal == "rejected" else "approve"
    finalized = client.post(
        f"/api/product-actions/{second['operation_id']}/decisions",
        json={
            "confirmation_token": second["confirmation_token"],
            "decision": decision,
        },
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == terminal

    with sqlite3.connect(data_dir / "data.db") as connection:
        row = connection.execute(
            "SELECT result_contract,result_json,visible_result,transport_json,undo_json,"
            "failure_category,failure_code FROM write_operations WHERE id=?",
            (second["operation_id"],),
        ).fetchone()
        assert row is not None
        result = json.loads(row[1])
        transport = json.loads(row[3])
        result["canary"] = "must-not-leak"
        transport["canary"] = "must-not-leak"
        payload = build_terminal_payload(
            status=terminal,
            result_contract=row[0],
            result=result,
            visible_result=row[2],
            transport=transport,
            undo=json.loads(row[4]) if row[4] is not None else None,
            failure_category=row[5],
            failure_code=row[6],
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"
        )
        connection.execute(
            "UPDATE write_operations SET result_json=?,transport_json=?,"
            "terminal_payload_sha256=? WHERE id=?",
            (
                payload.result_json,
                payload.transport_json,
                payload.digest,
                second["operation_id"],
            ),
        )
        connection.commit()

    replay = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert replay.status_code == 503
    assert replay.json() == {
        "error_code": "operation_result_unknown",
        "retryable": True,
    }
    assert "must-not-leak" not in replay.text


def test_story_n_and_n_plus_one_product_action_rows_are_private_and_utf8_bounded(
    story_client,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-next-privacy-budget-0001"),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    next_response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert next_response.status_code == 201
    second = next_response.json()
    current = client.get(f"/api/interview-story-proposals/{attempt['id']}").json()
    secret = "仅存领域聚合🧪" * 12
    confirmation = _confirmation_from_attempt(
        current,
        token=second["confirmation_token"],
    )
    confirmation["content"] = {
        **confirmation["content"],
        "title": secret,
    }
    committed = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=confirmation,
    )
    assert committed.status_code == 201, committed.json()

    with sqlite3.connect(data_dir / "data.db") as connection:
        product_action_rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "write_operations",
                "write_operation_transitions",
                "product_action_proposals",
            )
        }
        terminal = connection.execute(
            "SELECT result_json,visible_result,transport_json,undo_json "
            "FROM write_operations WHERE id=?",
            (second["operation_id"],),
        ).fetchone()
    assert terminal is not None
    serialized = json.dumps(product_action_rows, ensure_ascii=False, default=str)
    assert secret not in serialized
    byte_lengths = tuple(len((value or "").encode("utf-8")) for value in terminal)
    assert byte_lengths[0] <= 4_096
    assert byte_lengths[1] <= 1_024
    assert byte_lengths[2] <= 4_096
    assert byte_lengths[3] <= 32_768
    assert sum(byte_lengths) <= 49_152


@pytest.mark.parametrize("previous_title_length", [200, 201])
def test_actual_story_restore_undo_title_cap_and_cap_plus_one_rollback(
    story_client,
    previous_title_length: int,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    first_attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(
            note["id"],
            key=f"story-undo-cap-seed-{previous_title_length}-0001",
        ),
    ).json()
    first_confirmation = _confirmation_from_attempt(first_attempt)
    first_confirmation["content"] = {
        **first_confirmation["content"],
        "title": "界" * 200,
    }
    first_saved = client.post(
        f"/api/interview-story-proposals/{first_attempt['id']}/confirm",
        json=first_confirmation,
    )
    assert first_saved.status_code == 201
    story_id = first_saved.json()["story_id"]
    version_id = first_saved.json()["version_id"]
    update_request = {
        **_proposal_request(
            note["id"],
            key=f"story-undo-cap-update-{previous_title_length}-0001",
        ),
        "target_story_id": story_id,
        "expected_current_version_id": version_id,
        "expected_story_revision": 1,
    }
    update_attempt = client.post(
        "/api/interview-story-proposals",
        json=update_request,
    ).json()
    if previous_title_length == 201:
        with sqlite3.connect(data_dir / "data.db") as connection:
            connection.execute(
                "UPDATE interview_stories SET title=? WHERE id=?",
                ("界" * 201, story_id),
            )
            connection.commit()
    confirmation = _confirmation_from_attempt(update_attempt)
    confirmation["expected_current_version_id"] = version_id
    confirmation["expected_story_revision"] = 1
    saved = client.post(
        f"/api/interview-story-proposals/{update_attempt['id']}/confirm",
        json=confirmation,
    )

    operation_id = update_attempt["product_action"]["operation_id"]
    with sqlite3.connect(data_dir / "data.db") as connection:
        operation = connection.execute(
            "SELECT status,undo_json FROM write_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        story = connection.execute(
            "SELECT title,current_version_id,story_revision FROM interview_stories WHERE id=?",
            (story_id,),
        ).fetchone()
        versions = connection.execute(
            "SELECT COUNT(*) FROM interview_story_versions WHERE story_id=?",
            (story_id,),
        ).fetchone()[0]
        transitions = connection.execute(
            "SELECT seq,state FROM write_operation_transitions "
            "WHERE operation_id=? ORDER BY seq",
            (operation_id,),
        ).fetchall()
    if previous_title_length == 200:
        assert saved.status_code == 201
        assert operation[0] == "committed"
        assert json.loads(operation[1])["previous_title"] == "界" * 200
        assert story[2] == 2 and versions == 2
        assert transitions[-1] == (4, "committed")
    else:
        assert saved.status_code == 503
        assert saved.json()["error_code"] == "operation_result_unknown"
        assert operation == ("proposed", None)
        assert story == ("界" * 201, version_id, 1)
        assert versions == 1
        assert transitions == [(1, "proposed")]


@pytest.mark.parametrize("commit_then_raise", [False, True])
def test_ready_four_object_publication_reconciles_commit_unknown_without_provider_recall(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    commit_then_raise: bool,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    repository = client.app.state.interview_stories_repository
    claim = repository.claim_proposal(
        **_proposal_request(note["id"], key=f"story-ready-unknown-{commit_then_raise}-0001"),
        entrypoint="ui",
    )
    frozen = _provider_story(claim.source_snapshot)
    original_commit = Session.commit
    attempts = 0

    def lose_first_commit(session: Session) -> None:
        nonlocal attempts
        attempts += 1
        if commit_then_raise:
            original_commit(session)
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        original_commit(session)

    monkeypatch.setattr(Session, "commit", lose_first_commit)
    assert repository.complete_proposal(
        attempt_id=claim.attempt_id,
        generation_revision=claim.generation_revision,
        provider_call_token=claim.provider_call_token,
        proposal=frozen,
    )
    assert attempts == (1 if commit_then_raise else 2)
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT attempt_status, product_action_generation "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (claim.attempt_id,),
        ).fetchone() == ("ready", 1)
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM product_action_proposals").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM write_operation_transitions").fetchone() == (1,)


@pytest.mark.parametrize("terminal", ["approve", "reject", "declared_failed"])
def test_first_publication_reconciliation_overlaps_terminal_decision(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    client, _ = story_client
    note = _note(client)
    repository = client.app.state.interview_stories_repository
    claim = repository.claim_proposal(
        **_proposal_request(
            note["id"],
            key=f"story-first-overlap-{terminal}-0001",
        ),
        entrypoint="ui",
    )
    if terminal == "declared_failed":
        def declared(
            _session: Any,
            _trusted: Any,
            authorization: ProductActionExecutionAuthorization,
            *,
            authorization_binding: tuple[object, ...],
        ) -> Any:
            with repository._proof_registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="confirm_interview_story",
                expected_binding=authorization_binding,
            ):
                raise ProductActionStoryWriteConflict()

        monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    original_commit = Session.commit
    original_require = repository._require_ready_publication_attempt
    reconciliation_entered = threading.Event()
    release_reconciliation = threading.Event()
    captured: dict[str, Any] = {}
    owner_thread_id: int | None = None
    response_lost = False

    def lose_owner_response(session: Session) -> None:
        nonlocal response_lost
        original_commit(session)
        if threading.get_ident() == owner_thread_id and not response_lost:
            response_lost = True
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit response lost"),
            )

    def overlap_reconciliation(session: Session, **kwargs: Any) -> None:
        if threading.get_ident() == owner_thread_id and response_lost:
            captured["operation_id"] = kwargs["operation_id"]
            captured["bundle"] = kwargs["bundle"]
            reconciliation_entered.set()
            assert release_reconciliation.wait(10)
        original_require(session, **kwargs)

    monkeypatch.setattr(Session, "commit", lose_owner_response)
    monkeypatch.setattr(
        repository,
        "_require_ready_publication_attempt",
        overlap_reconciliation,
    )

    def publish() -> bool:
        nonlocal owner_thread_id
        owner_thread_id = threading.get_ident()
        return repository.complete_proposal(
            attempt_id=claim.attempt_id,
            generation_revision=claim.generation_revision,
            provider_call_token=claim.provider_call_token,
            proposal=_provider_story(claim.source_snapshot),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(publish)
        assert reconciliation_entered.wait(10)
        operation_id = captured["operation_id"]
        bundle = captured["bundle"]
        token = repository._action_issuer.recover_confirmation_token(bundle)
        decision = "approve" if terminal != "reject" else "reject"
        decided = pool.submit(
            client.app.state.product_action_coordinator.decide,
            operation_id=operation_id,
            request={"confirmation_token": token, "decision": decision},
        )
        assert not decided.done()
        release_reconciliation.set()
        assert owner.result(timeout=10) is True
        result = decided.result(timeout=10)
    expected = "failed" if terminal == "declared_failed" else (
        "rejected" if terminal == "reject" else "committed"
    )
    assert result.status == expected
    assert client.app.state.product_action_proposal_repository.load_bundle(
        operation_id
    ).operation.status == expected


@pytest.mark.parametrize("commit_then_raise", [False, True])
@pytest.mark.parametrize("declared_failure", [False, True])
def test_first_publication_commit_unknown_then_concurrent_decisions_stay_atomic(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    commit_then_raise: bool,
    declared_failure: bool,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    repository = client.app.state.interview_stories_repository
    claim = repository.claim_proposal(
        **_proposal_request(
            note["id"],
            key=f"story-first-race-{commit_then_raise}-{declared_failure}-0001",
        ),
        entrypoint="ui",
    )
    original_commit = Session.commit
    commits = 0

    def lose_publication_response(session: Session) -> None:
        nonlocal commits
        commits += 1
        if commit_then_raise:
            original_commit(session)
        if commits == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        original_commit(session)

    with monkeypatch.context() as response_loss:
        response_loss.setattr(Session, "commit", lose_publication_response)
        assert repository.complete_proposal(
            attempt_id=claim.attempt_id,
            generation_revision=claim.generation_revision,
            provider_call_token=claim.provider_call_token,
            proposal=_provider_story(claim.source_snapshot),
        )
    attempt = repository.get_attempt(claim.attempt_id)
    assert attempt is not None
    operation_id = attempt["product_action_operation_id"]
    bundle = client.app.state.product_action_proposal_repository.load_bundle(operation_id)
    token = repository._action_issuer.recover_confirmation_token(bundle)

    if declared_failure:
        def declared(
            _session: Any,
            _trusted: Any,
            authorization: ProductActionExecutionAuthorization,
            *,
            authorization_binding: tuple[object, ...],
        ) -> Any:
            with repository._proof_registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="confirm_interview_story",
                expected_binding=authorization_binding,
            ):
                raise ProductActionStoryWriteConflict()

        monkeypatch.setattr(repository, "confirm_attempt_bound", declared)

    coordinator = client.app.state.product_action_coordinator

    def decide(index: int) -> tuple[str, str]:
        decision = "approve" if index % 2 == 0 else "reject"
        try:
            result = coordinator.decide(
                operation_id=operation_id,
                request={"confirmation_token": token, "decision": decision},
            )
        except ProductActionCoordinatorError as exc:
            return "error", exc.code
        return "result", result.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(decide, range(8)))

    terminal = client.app.state.product_action_proposal_repository.load_bundle(operation_id)
    assert terminal.classification == "exact_terminal"
    assert terminal.operation.status in {"committed", "rejected", "failed"}
    assert ("result", terminal.operation.status) in outcomes
    with sqlite3.connect(data_dir / "data.db") as connection:
        transitions = connection.execute(
            "SELECT seq,state FROM write_operation_transitions "
            "WHERE operation_id=? ORDER BY seq",
            (operation_id,),
        ).fetchall()
        story_count = connection.execute(
            "SELECT COUNT(*) FROM interview_stories"
        ).fetchone()[0]
        attempt_state = connection.execute(
            "SELECT attempt_status FROM interview_story_proposal_attempts WHERE id=?",
            (claim.attempt_id,),
        ).fetchone()[0]
    if terminal.operation.status == "rejected":
        assert transitions == [(1, "proposed"), (2, "rejected")]
        assert story_count == 0 and attempt_state == "ready"
    else:
        assert transitions == [
            (1, "proposed"),
            (2, "approved"),
            (3, "claimed"),
            (4, terminal.operation.status),
        ]
        if terminal.operation.status == "committed":
            assert story_count == 1 and attempt_state == "confirmed"
        else:
            assert declared_failure
            assert story_count == 0 and attempt_state == "ready"


@pytest.mark.parametrize(
    ("integrity_code", "expected"),
    [
        ("partial_product_action_bundle", "integrity"),
        ("product_action_bundle_unreadable", "unknown"),
    ],
)
def test_ready_publication_commit_unknown_fails_closed_on_partial_or_unreadable(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    integrity_code: str,
    expected: str,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    repository = client.app.state.interview_stories_repository
    claim = repository.claim_proposal(
        **_proposal_request(note["id"], key=f"story-ready-{integrity_code}-0001"),
        entrypoint="ui",
    )

    original_commit = Session.commit
    commits = 0

    def lose_first_commit(session: Session) -> None:
        nonlocal commits
        commits += 1
        if commits == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        original_commit(session)

    def reconcile(_session: Session, _uow: Any, _prepared: Any) -> ProductActionPublicationV1:
        raise ProductActionIntegrityError(integrity_code)

    with monkeypatch.context() as commit_unknown:
        commit_unknown.setattr(Session, "commit", lose_first_commit)
        commit_unknown.setattr(
            client.app.state.product_action_proposal_repository,
            "reconcile_publication_in_session",
            reconcile,
        )
        error_type = (
            ProductActionIntegrityError
            if expected == "integrity"
            else ProductActionCoordinatorError
        )
        with pytest.raises(error_type) as caught:
            repository.complete_proposal(
                attempt_id=claim.attempt_id,
                generation_revision=claim.generation_revision,
                provider_call_token=claim.provider_call_token,
                proposal=_provider_story(claim.source_snapshot),
            )
    assert caught.value.code == (
        integrity_code if expected == "integrity" else "operation_result_unknown"
    )
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT attempt_status,provider_call_token,provider_lease_until,failure_category "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (claim.attempt_id,),
        ).fetchone() == (
            "provider_unknown",
            "",
            None,
            "product_action_publication_unknown",
        )

    provider_calls = 0

    def forbidden_provider(_model: Any, snapshot: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        return _provider_story(snapshot)

    monkeypatch.setattr(
        "offerpilot.api.generate_interview_story_proposal",
        forbidden_provider,
    )
    with TestClient(create_app(data_dir=data_dir, chat_model=object())) as restarted:
        late = restarted.post(
            "/api/interview-story-proposals",
            json=_proposal_request(
                note["id"],
                key=f"story-ready-{integrity_code}-0001",
            ),
        )
    assert late.status_code == 202
    assert late.json()["attempt_status"] == "provider_unknown"
    assert provider_calls == 0


@pytest.mark.parametrize("commit_then_raise", [False, True])
def test_n_plus_one_reconciles_commit_unknown_to_same_frozen_proposal(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    commit_then_raise: bool,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-next-unknown-{commit_then_raise}-0001"),
    ).json()
    action = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{action['operation_id']}/decisions",
        json={"confirmation_token": action["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    original_commit = Session.commit
    commits = 0

    def lose_first_commit(session: Session) -> None:
        nonlocal commits
        commits += 1
        if commit_then_raise:
            original_commit(session)
        if commits == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        original_commit(session)

    monkeypatch.setattr(Session, "commit", lose_first_commit)
    result = client.app.state.interview_stories_repository.create_next_product_action(
        attempt_id=attempt["id"],
        expected_generation_revision=attempt["generation_revision"],
        expected_product_action_generation=1,
    )
    assert result.proposal_created is False
    assert result.product_action_generation == 2
    assert commits == (1 if commit_then_raise else 2)
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT product_action_generation, product_action_operation_id "
            "FROM interview_story_proposal_attempts WHERE id=?",
            (attempt["id"],),
        ).fetchone() == (2, result.operation_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM product_action_proposals WHERE source_id=?",
            (attempt["id"],),
        ).fetchone() == (2,)


@pytest.mark.parametrize("terminal", ["approve", "reject", "declared_failed"])
def test_n_plus_one_response_loss_overlaps_terminal_decision(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    client, _ = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key=f"story-next-overlap-{terminal}-0001"),
    ).json()
    first = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{first['operation_id']}/decisions",
        json={"confirmation_token": first["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    repository = client.app.state.interview_stories_repository
    if terminal == "declared_failed":
        def declared(
            _session: Any,
            _trusted: Any,
            authorization: ProductActionExecutionAuthorization,
            *,
            authorization_binding: tuple[object, ...],
        ) -> Any:
            with repository._proof_registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="confirm_interview_story",
                expected_binding=authorization_binding,
            ):
                raise ProductActionStoryWriteConflict()

        monkeypatch.setattr(repository, "confirm_attempt_bound", declared)
    product_actions = client.app.state.product_action_proposal_repository
    original_commit = Session.commit
    original_validate = repository._validate_replayed_story_generation_in_session
    reconciliation_entered = threading.Event()
    release_reconciliation = threading.Event()
    captured: dict[str, Any] = {}
    owner_thread_id: int | None = None
    response_lost = False

    def lose_owner_response(session: Session) -> None:
        nonlocal response_lost
        original_commit(session)
        if threading.get_ident() == owner_thread_id and not response_lost:
            response_lost = True
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("N+1 commit response lost"),
            )

    def overlap_validate(
        session: Session,
        uow: Any,
        bundle: Any,
        **kwargs: Any,
    ) -> None:
        if threading.get_ident() == owner_thread_id and response_lost:
            captured["operation_id"] = bundle.operation.id
            captured["bundle"] = bundle
            reconciliation_entered.set()
            assert release_reconciliation.wait(10)
        original_validate(session, uow, bundle, **kwargs)

    monkeypatch.setattr(Session, "commit", lose_owner_response)
    monkeypatch.setattr(
        repository,
        "_validate_replayed_story_generation_in_session",
        overlap_validate,
    )

    def advance():
        nonlocal owner_thread_id
        owner_thread_id = threading.get_ident()
        return repository.create_next_product_action(
            attempt_id=attempt["id"],
            expected_generation_revision=attempt["generation_revision"],
            expected_product_action_generation=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(advance)
        assert reconciliation_entered.wait(10)
        operation_id = captured["operation_id"]
        bundle = captured["bundle"]
        token = repository._action_issuer.recover_confirmation_token(bundle)
        decision = "approve" if terminal != "reject" else "reject"
        decided = pool.submit(
            client.app.state.product_action_coordinator.decide,
            operation_id=operation_id,
            request={"confirmation_token": token, "decision": decision},
        )
        assert not decided.done()
        release_reconciliation.set()
        proposal = owner.result(timeout=10)
        result = decided.result(timeout=10)
    expected = "failed" if terminal == "declared_failed" else (
        "rejected" if terminal == "reject" else "committed"
    )
    assert proposal.proposal_created is False
    assert proposal.operation_id == operation_id
    assert proposal.status == "proposed"
    assert result.status == expected
    assert product_actions.load_bundle(operation_id).operation.status == expected


def test_historical_bridge_commit_unknown_forces_replay_http_projection(
    story_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, data_dir = story_client
    note = _note(client)
    _legacy, attempt = _legacy_ready_attempt(
        client,
        note_id=note["id"],
        key="story-historical-unknown-seed-0001",
    )
    request = _confirmation_from_attempt(
        attempt,
        token="story-historical-unknown-token-0001",
    )
    original_commit = Session.commit
    commits = 0

    def fail_before_first_commit(session: Session) -> None:
        nonlocal commits
        commits += 1
        if commits == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_before_first_commit)
    response = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/confirm",
        json=request,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["created"] is False
    assert commits == 3
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM interview_stories").fetchone() == (1,)


def test_raw_story_compatibility_rejects_exact_integer_confusion_and_duplicates(
    story_client,
) -> None:
    client, data_dir = story_client
    base = {
        "confirmation_token": "story-raw-compatibility-token-0001",
        "content": {},
        "evidence_links": [],
        "expected_current_version_id": None,
        "expected_story_revision": None,
    }
    for field in ("expected_current_version_id", "expected_story_revision"):
        for value in (True, 1.0, "1"):
            response = client.post(
                "/api/interview-story-proposals/999/confirm",
                json={**base, field: value},
            )
            assert response.status_code == 422
            assert response.json()["error_code"] == "interview_story_invalid_request"
    duplicate = client.post(
        "/api/interview-story-proposals/999/confirm",
        content=(
            b'{"confirmation_token":"story-raw-compatibility-token-0001",'
            b'"confirmation_token":"story-raw-compatibility-token-0001",'
            b'"content":{},"evidence_links":[],'
            b'"expected_current_version_id":null,"expected_story_revision":null}'
        ),
        headers={"content-type": "application/json"},
    )
    assert duplicate.status_code == 422
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (0,)


@pytest.mark.parametrize(
    "extra",
    [
        {"confirmation_token": "x" * 64},
        {"content": {}},
        {"evidence_links": []},
        {"generation": 1},
    ],
)
def test_n_plus_one_rejects_every_non_contract_field_before_lookup(
    story_client,
    extra: dict[str, Any],
) -> None:
    client, data_dir = story_client
    response = client.post(
        "/api/interview-story-proposals/999/product-actions",
        json={
            "expected_generation_revision": 1,
            "expected_product_action_generation": 1,
            **extra,
        },
    )
    assert response.status_code == 422
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_operations").fetchone() == (0,)


def test_n_plus_one_refuses_stale_and_invalidated_attempts(story_client) -> None:
    client, data_dir = story_client
    note = _note(client)
    attempt = client.post(
        "/api/interview-story-proposals",
        json=_proposal_request(note["id"], key="story-next-stale-invalidated-0001"),
    ).json()
    action = attempt["product_action"]
    assert client.post(
        f"/api/product-actions/{action['operation_id']}/decisions",
        json={"confirmation_token": action["confirmation_token"], "decision": "reject"},
    ).status_code == 200
    stale = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"] + 1,
            "expected_product_action_generation": 1,
        },
    )
    assert stale.status_code == 409
    repository = client.app.state.interview_stories_repository
    with repository._session_factory() as session:
        row = session.get(InterviewStoryProposalAttempt, attempt["id"])
        assert row is not None
        row.attempt_status = "invalidated"
        row.failure_category = "source_changed"
        session.commit()
    invalidated = client.post(
        f"/api/interview-story-proposals/{attempt['id']}/product-actions",
        json={
            "expected_generation_revision": attempt["generation_revision"],
            "expected_product_action_generation": 1,
        },
    )
    assert invalidated.status_code == 409
    with sqlite3.connect(data_dir / "data.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_action_proposals WHERE source_id=?",
            (attempt["id"],),
        ).fetchone() == (1,)


def test_story_attempt_model_carries_product_action_pointer_shape() -> None:
    assert "product_action_operation_id" in InterviewStoryProposalAttempt.__table__.columns
    assert "product_action_generation" in InterviewStoryProposalAttempt.__table__.columns
