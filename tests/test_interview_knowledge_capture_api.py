from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    InterviewKnowledgeCaptureAttempt,
    KnowledgeEvidence,
    KnowledgeExtractionSnapshot,
)


def _create_note(client: TestClient) -> dict[str, object]:
    application = client.post(
        "/api/applications", json={"company_name": "Acme", "position_name": "Backend"}
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
    return client.post(
        f"/api/applications/{application['id']}/notes",
        json={
            "application_event_id": event["id"],
            "questions": "设计一个缓存",
            "self_reflection": "我解释了淘汰策略",
            "difficulty_points": "说明一致性",
            "mood": "平稳",
        },
    ).json()


def _fragments() -> list[dict[str, object]]:
    return [
        {"fragment_id": "client-1", "path": "/questions", "start": 0, "end": 6, "text": "设计一个缓存"},
        {"fragment_id": "client-2", "path": "/self_reflection", "start": 0, "end": 8, "text": "我解释了淘汰策略"},
    ]


def _preview(client: TestClient, note_id: int, key: str = "attempt-1"):
    return client.post(
        f"/api/notes/{note_id}/knowledge-capture/preview",
        json={"attempt_key": key, "mode": "direct", "selected_fragments": _fragments()},
    )


def test_direct_preview_has_no_knowledge_rows_before_confirm(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    response = _preview(client, int(note["id"]))
    assert response.status_code == 200
    assert response.json()["preview_status"] == "direct_ready"
    assert client.get("/api/knowledge/notes").json() == {"items": []}


def test_confirm_requires_a_verified_preview_attempt(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/preview",
        json={"attempt_key": "attempt-1", "mode": "direct", "selected_fragments": _fragments()},
    )
    assert response.status_code == 200
    # A fresh key cannot be used to bypass the direct/AI preview gate.
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": "attempt-2",
            "note_fingerprint": response.json()["note_fingerprint"],
            "title": "绕过预览",
            "blocks": response.json()["preview"]["blocks"],
        },
    )
    assert response.status_code == 404


def test_confirm_creates_frozen_knowledge_and_is_idempotent(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    content = attempt["preview"]
    payload = {
        "attempt_key": attempt["attempt_key"],
        "note_fingerprint": attempt["note_fingerprint"],
        "title": "缓存面试复盘",
        "blocks": content["blocks"],
    }
    first = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    second = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["version_id"] == first.json()["version_id"]
    listing = client.get("/api/knowledge/notes")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["origin_kind"] == "confirmed_interview_capture"
    metadata = listing.json()["items"][0]["capture_metadata"]
    assert metadata == {
        "source_id": first.json()["source_id"],
        "origin_note_id": note["id"],
        "application_event_id": note["application_event_id"],
        "note_fingerprint": attempt["note_fingerprint"],
        "capture_schema_version": "interview-note-capture-v1",
        "captured_at": metadata["captured_at"],
    }
    assert metadata["captured_at"]


def test_confirmed_attempt_is_read_only_for_same_key_direct_preview(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    payload = {
        "attempt_key": attempt["attempt_key"],
        "note_fingerprint": attempt["note_fingerprint"],
        "title": "缓存面试复盘",
        "blocks": attempt["preview"]["blocks"],
    }
    first = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    assert first.status_code == 201

    replay = _preview(client, int(note["id"]), attempt["attempt_key"])
    assert replay.status_code == 200
    assert replay.json()["preview_status"] == "confirmed"
    assert replay.json()["preview"] == attempt["preview"]

    second = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    assert second.status_code == 200
    assert second.json()["version_id"] == first.json()["version_id"]
    assert len(client.get("/api/knowledge/notes").json()["items"]) == 1


def test_confirmed_attempt_recovers_after_note_edit_with_same_key(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    payload = {
        "attempt_key": attempt["attempt_key"],
        "note_fingerprint": attempt["note_fingerprint"],
        "title": "冻结的面试知识",
        "blocks": attempt["preview"]["blocks"],
    }
    first = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    assert first.status_code == 201
    edited = dict(note)
    edited["questions"] = "后续修改的面试问题"
    assert client.put(f"/api/notes/{note['id']}", json=edited).status_code == 200

    replay = _preview(client, int(note["id"]), attempt["attempt_key"])
    assert replay.status_code == 200
    assert replay.json()["preview_status"] == "confirmed"
    second = client.post(f"/api/notes/{note['id']}/knowledge-capture/confirm", json=payload)
    assert second.status_code == 200
    assert second.json()["version_id"] == first.json()["version_id"]


def test_expired_attempt_cannot_be_confirmed(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        row = session.scalar(
            select(InterviewKnowledgeCaptureAttempt).where(
                InterviewKnowledgeCaptureAttempt.attempt_key == attempt["attempt_key"]
            )
        )
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "过期复盘",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    assert response.status_code == 410
    assert response.json()["error_code"] == "interview_knowledge_attempt_expired"
    assert client.get("/api/knowledge/notes").json() == {"items": []}


def test_evidence_offsets_point_to_each_fragment_in_snapshot(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    fragments = [
        {"fragment_id": "client-1", "path": "/questions", "start": 0, "end": 6, "text": "设计一个缓存"},
        {"fragment_id": "client-2", "path": "/self_reflection", "start": 0, "end": 8, "text": "我解释了淘汰策略"},
    ]
    attempt = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/preview",
        json={"attempt_key": "offsets", "mode": "direct", "selected_fragments": fragments},
    ).json()
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "带定位复盘",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    assert response.status_code == 201
    factory = session_factory_for_data_dir(tmp_path)
    with factory() as session:
        snapshot = session.scalar(select(KnowledgeExtractionSnapshot))
        evidence = list(session.scalars(select(KnowledgeEvidence).order_by(KnowledgeEvidence.ordinal)))
        assert snapshot is not None
        snapshot_text = snapshot.canonical_text
        assert len(evidence) == 2
        for row in evidence:
            assert snapshot_text[row.char_start : row.char_end] == row.canonical_excerpt
            assert row.char_end > row.char_start
            assert row.line_end >= row.line_start


def test_editing_preview_marks_content_origin_as_user_edited(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    edited_blocks = [dict(block) for block in attempt["preview"]["blocks"]]
    edited_blocks[0]["text"] = "用户补充的复盘笔记"
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "用户编辑后的标题",
            "blocks": edited_blocks,
        },
    )
    assert response.status_code == 201
    factory = session_factory_for_data_dir(tmp_path)
    from offerpilot.models import KnowledgeNoteVersion

    with factory() as session:
        version = session.scalar(select(KnowledgeNoteVersion))
        assert version is not None
        assert version.content_origin == "user_edited_preview"


def test_captured_source_is_not_mutable_through_generic_source_api(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    confirm = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "只读面试知识",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    source_id = confirm.json()["source_id"]
    protected = "captured_interview_source_read_only"
    for response in (
        client.patch(f"/api/knowledge/sources/{source_id}", json={"display_title": "改名"}),
        client.post(f"/api/knowledge/sources/{source_id}/archive"),
        client.post(f"/api/knowledge/sources/{source_id}/unarchive"),
        client.delete(f"/api/knowledge/sources/{source_id}"),
    ):
        assert response.status_code == 409
        assert response.json()["error_code"] == protected
    assert all(item["id"] != source_id for item in client.get("/api/knowledge/sources").json())
    details = client.get("/api/knowledge/notes").json()["items"][0]
    assert details["evidence"]
    assert details["evidence"][0]["excerpt"]
    assert details["content"]["blocks"][0]["evidence"][0]["path"] == "/questions"
    assert details["content"]["blocks"][0]["evidence"][0]["frozen_at"]


def test_captured_source_brief_routes_are_read_only_but_generic_source_is_not_blocked(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    confirm = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "Brief 隔离测试",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    source_id = confirm.json()["source_id"]
    for response in (
        client.get(f"/api/knowledge/sources/{source_id}/brief"),
        client.post(f"/api/knowledge/sources/{source_id}/brief/rebuild"),
    ):
        assert response.status_code == 409
        assert response.json()["error_code"] == "captured_interview_source_read_only"

    ordinary = client.post(
        "/api/knowledge/sources",
        files={"file": ("ordinary.md", b"# Ordinary\n", "text/markdown")},
    )
    assert ordinary.status_code in {200, 202}
    ordinary_id = ordinary.json()["source"]["id"]
    for response in (
        client.get(f"/api/knowledge/sources/{ordinary_id}/brief"),
        client.post(f"/api/knowledge/sources/{ordinary_id}/brief/rebuild"),
    ):
        assert response.status_code != 409 or response.json().get("error_code") != "captured_interview_source_read_only"


def test_note_edit_before_confirm_returns_409_without_knowledge_asset(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    updated = dict(note)
    updated["questions"] = "变化后的问题"
    assert client.put(f"/api/notes/{note['id']}", json=updated).status_code == 200
    response = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "缓存面试复盘",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "interview_knowledge_source_changed"
    assert client.get("/api/knowledge/notes").json() == {"items": []}


def test_delete_unconfirmed_attempt_is_idempotent(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    path = f"/api/notes/{note['id']}/knowledge-capture/attempts/{attempt['attempt_key']}"
    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 204
    assert client.get("/api/knowledge/notes").json() == {"items": []}


def test_delete_confirmed_attempt_returns_conflict(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    confirm = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "缂撳瓨闈㈣瘯澶嶇洏",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    assert confirm.status_code == 201
    path = f"/api/notes/{note['id']}/knowledge-capture/attempts/{attempt['attempt_key']}"
    response = client.delete(path)
    assert response.status_code == 409
    assert response.json()["error_code"] == "capture_attempt_confirmed"


def test_confirmed_knowledge_remains_auditable_after_event_deletion(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    note = _create_note(client)
    attempt = _preview(client, int(note["id"])).json()
    confirm = client.post(
        f"/api/notes/{note['id']}/knowledge-capture/confirm",
        json={
            "attempt_key": attempt["attempt_key"],
            "note_fingerprint": attempt["note_fingerprint"],
            "title": "缂撳瓨闈㈣瘯澶嶇洏",
            "blocks": attempt["preview"]["blocks"],
        },
    )
    assert confirm.status_code == 201
    assert client.delete(f"/api/application-events/{note['application_event_id']}").status_code == 200
    items = client.get("/api/knowledge/notes").json()["items"]
    assert len(items) == 1
    assert items[0]["source_status"] == "source_changed"
    assert items[0]["content"]["blocks"]
