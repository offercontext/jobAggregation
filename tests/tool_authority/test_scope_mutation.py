"""Contract tests for canonical Conversation scope mutation ports."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from offerpilot.api import create_app
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.db import init_database
from offerpilot.models import Application
from offerpilot.repositories.chat import (
    ChatRepository,
    ConversationScopeError,
    ConversationScopeMutationSnapshot,
    ConversationScopeVisibilityFailure,
)
from offerpilot.ai.tool_authority.visibility import (
    AuthorityApplicationVisibilityError,
    AuthorityApplicationVisibilityQuery,
)


@pytest.fixture
def repo(tmp_path: Path) -> ChatRepository:
    sessions = init_database(tmp_path / "offerpilot.db")
    return ChatRepository(sessions)


def test_patch_visibility_internal_error_is_retryable_and_not_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    application = client.post(
        "/api/applications",
        json={"company_name": "Scope", "position_name": "Role"},
    ).json()
    repository = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repository.create_conversation_with_scope(
        "scoped",
        ConversationScopeMutationSnapshot(
            context_type="application",
            context_ref=application["id"],
            mode="general",
        ),
    )

    def fail_visibility(*_args: object, **_kwargs: object) -> object:
        raise AuthorityApplicationVisibilityError("secret database detail")

    monkeypatch.setattr(
        AuthorityApplicationVisibilityQuery,
        "execute_on_session",
        fail_visibility,
    )
    with pytest.raises(ConversationScopeVisibilityFailure):
        repository.patch_conversation_with_scope(
            conversation.id,
            {"title": "must roll back"},
            ConversationScopeMutationSnapshot(
                context_type="application",
                context_ref=application["id"],
                mode="focused",
            ),
            expected_scope_revision=0,
        )

    response = client.patch(
        f"/api/chat/conversations/{conversation.id}",
        json={"mode": "focused", "title": "must roll back"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": "上下文暂时无法加载，请稍后重试。",
        "error_code": "source_load_failed",
    }
    assert "secret" not in response.text
    stored = repository.get_conversation(conversation.id)
    assert stored is not None
    assert (stored.title, stored.mode, stored.scope_revision) == ("scoped", "general", 0)


def test_generic_conversation_writes_reject_scope_keys(repo: ChatRepository) -> None:
    with pytest.raises(ValueError, match="scope"):
        repo.create_conversation("bad", context_type="application")
    conversation = repo.create_conversation("ok")
    with pytest.raises(ValueError, match="scope"):
        repo.update_conversation(conversation.id, {"context_type": "global"})
    with pytest.raises(ValueError, match="scope"):
        repo.update_conversation_for_archive(
            conversation.id,
            {"context_type": "application", "context_ref": "999999", "scope_revision": 1},
        )
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert (stored.context_type, stored.context_ref, stored.scope_revision) == (
        "workspace",
        "",
        0,
    )


@pytest.mark.parametrize("context_type", ["workspace", "global", "mode"])
def test_non_application_ref_is_validated_then_discarded(
    repo: ChatRepository,
    context_type: str,
) -> None:
    conversation = repo.create_conversation_with_scope(
        "discard ref",
        ConversationScopeMutationSnapshot(
            context_type=context_type,
            context_ref="legacy-ref",
            mode="general",
        ),
    )
    assert conversation.context_ref == ""


@pytest.mark.parametrize(
    "context_ref",
    [1, True, "bad\x00ref", "x" * 257, "\ud800"],
)
def test_non_application_ref_rejects_invalid_shape_before_write(
    repo: ChatRepository,
    context_ref: object,
) -> None:
    with pytest.raises(ConversationScopeError, match="context_ref"):
        ConversationScopeMutationSnapshot(
            context_type="workspace",
            context_ref=context_ref,
            mode="general",
        )
    assert repo.list_conversations(include_archived=True) == []


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
    assert response.json() == {
        "error": "上下文暂时无法加载，请稍后重试。",
        "error_code": "source_load_failed",
    }
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


@pytest.mark.parametrize("deleted", [False, True])
def test_patch_unavailable_application_uses_safe_not_found_and_rolls_back(
    tmp_path: Path,
    deleted: bool,
) -> None:
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("before")
    with repo._session_factory() as session:
        application = Application(company_name="Hidden", position_name="Role")
        if deleted:
            application.deleted_at = datetime.now(timezone.utc)
        session.add(application)
        session.commit()
        application_id = application.id
    if not deleted:
        application_id += 100_000
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.patch(
        f"/api/chat/conversations/{conversation.id}",
        json={
            "title": "must not commit",
            "context_type": "application",
            "context_ref": application_id,
        },
    )

    assert response.status_code == 404
    assert response.json() == {"error": "conversation not found"}
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert (stored.title, stored.context_type, stored.context_ref, stored.scope_revision) == (
        "before",
        "workspace",
        "",
        0,
    )


def test_patch_title_retains_baseline_string_coercion(tmp_path: Path) -> None:
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("before")
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.patch(
        f"/api/chat/conversations/{conversation.id}",
        json={"title": 123},
    )

    assert response.status_code == 200
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert stored.title == "123"


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
