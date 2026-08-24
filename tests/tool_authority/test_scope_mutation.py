"""Contract tests for canonical Conversation scope mutation ports."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from offerpilot.api import create_app
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.db import init_database
from offerpilot.models import Application
from offerpilot.repositories.chat import (
    ChatRepository,
    ConversationScopeMutationSnapshot,
)


@pytest.fixture
def repo(tmp_path: Path) -> ChatRepository:
    sessions = init_database(tmp_path / "offerpilot.db")
    return ChatRepository(sessions)


def test_generic_conversation_writes_reject_scope_keys(repo: ChatRepository) -> None:
    with pytest.raises(ValueError, match="scope"):
        repo.create_conversation("bad", context_type="application")
    conversation = repo.create_conversation("ok")
    with pytest.raises(ValueError, match="scope"):
        repo.update_conversation(conversation.id, {"context_type": "global"})


def test_create_scope_canonicalizes_application_int_and_starts_revision_zero(
    repo: ChatRepository,
) -> None:
    with repo._session_factory() as session:
        application = Application(company_name="Acme", position_name="Engineer")
        session.add(application)
        session.commit()
        application_id = application.id
    conversation = repo.create_conversation_with_scope(
        "application",
        ConversationScopeMutationSnapshot(
            context_type="application", context_ref=application_id, mode="general"
        ),
        title_source="fallback",
    )
    assert conversation.context_type == "application"
    assert conversation.context_ref == str(application_id)
    assert conversation.mode == "general"
    assert conversation.scope_revision == 0


def test_patch_scope_uses_cas_and_mixes_non_scope_atomically(repo: ChatRepository) -> None:
    conversation = repo.create_conversation("before")
    current = repo.get_conversation(conversation.id)
    assert current is not None
    changed = repo.patch_conversation_with_scope(
        conversation.id,
        {"title": "after", "title_source": "manual"},
        ConversationScopeMutationSnapshot(
            context_type="global", context_ref="", mode="GENERAL"
        ),
        expected_scope_revision=current.scope_revision,
    )
    assert changed is not None
    assert (changed.title, changed.context_type, changed.mode, changed.scope_revision) == (
        "after",
        "global",
        "GENERAL",
        1,
    )
    assert repo.patch_conversation_with_scope(
        conversation.id,
        {"title": "loser"},
        ConversationScopeMutationSnapshot(
            context_type="workspace", context_ref="", mode="general"
        ),
        expected_scope_revision=0,
    ) is None


@pytest.mark.parametrize(
    "mode",
    [" " + "x", "x ", "\x00", "x\n", "x" * 65],
)
def test_new_scope_rejects_invalid_mode(mode: str, repo: ChatRepository) -> None:
    with pytest.raises(ValueError, match="mode"):
        repo.create_conversation_with_scope(
            "bad",
            ConversationScopeMutationSnapshot(
                context_type="workspace", context_ref="", mode=mode
            ),
            title_source="fallback",
        )


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_new_invisible_application_scope_has_no_conversation_or_runtime_side_effects(
    tmp_path: Path, endpoint: str
) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    response = client.post(
        endpoint,
        json={
            "message": "不可见投递",
            "conversation_id": 0,
            "context_type": "application",
            "context_ref": "999999",
        },
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "scope_unavailable"
    assert ChatRepository(init_database(tmp_path / "data.db")).list_conversations(
        include_archived=True
    ) == []


@pytest.mark.parametrize("context_ref", ["01", "+1", " 1", True, 1.0])
def test_new_application_scope_rejects_noncanonical_id_before_runtime(
    tmp_path: Path, context_ref: object
) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    response = client.post(
        "/api/chat",
        json={
            "message": "非法投递",
            "conversation_id": 0,
            "context_type": "application",
            "context_ref": context_ref,
        },
    )
    assert response.status_code == 422
    assert ChatRepository(init_database(tmp_path / "data.db")).list_conversations(
        include_archived=True
    ) == []


def test_existing_start_scope_fields_are_shape_only_and_public_scope_revision_is_hidden(
    tmp_path: Path,
) -> None:
    sessions = init_database(tmp_path / "data.db")
    repo = ChatRepository(sessions)
    conversation = repo.create_conversation("existing")
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post(
        "/api/chat",
        json={
            "message": "继续",
            "conversation_id": conversation.id,
            "context_type": "application",
            "context_ref": "not-an-id",
            "mode": "  historical mode  ",
        },
    )
    assert response.status_code == 503
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert (stored.context_type, stored.context_ref, stored.mode, stored.scope_revision) == (
        "workspace",
        "",
        "general",
        0,
    )
    listed = client.get("/api/chat/conversations").json()
    assert "scope_revision" not in listed[0]


def test_patch_context_ref_without_type_is_rejected_without_revision_change(tmp_path: Path) -> None:
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("patch")
    client = TestClient(create_app(data_dir=tmp_path))
    response = client.patch(
        f"/api/chat/conversations/{conversation.id}",
        json={"context_ref": "1", "title": "should not commit"},
    )
    assert response.status_code == 422
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert stored.title == "patch"
    assert stored.scope_revision == 0


def test_pending_read_does_not_lazy_create_operation(tmp_path: Path) -> None:
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("read")
    assert repo.set_pending_action(
        conversation.id,
        PendingAction("call", "update_application_status", '{"id":1}', "update"),
    )
    assert repo.get_pending_action(conversation.id) is not None
    assert repo.get_conversation(conversation.id) is not None
    assert repo.list_conversations(include_archived=True)
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert stored.pending_operation_id == ""
