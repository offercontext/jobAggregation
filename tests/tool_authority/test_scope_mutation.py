"""Contract tests for canonical Conversation scope mutation ports."""

from __future__ import annotations

from pathlib import Path

import pytest

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
