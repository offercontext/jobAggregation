from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import event

from offerpilot.ai.write_operations import (
    WriteOperationError,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import Conversation
from offerpilot.repositories.chat import ChatRepository
from tests.tool_authority.test_pending_claim import create_primary_with_typed_route


def _ledger(tmp_path):
    sessions = init_database(tmp_path / "preheader.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("preheader")
    operation_id = str(uuid4())
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.pending_operation_id = operation_id
        owner.pending_tool_call_id = "call-1"
        owner.pending_tool_name = "create_application"
        owner.pending_args = '{"malformed":'
        owner.pending_human = "private human text"
        create_primary_with_typed_route(
            repository,
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id="call-1",
            tool_name="create_application",
            raw_args='{"malformed":',
            pending_action_revision=1,
            arguments_digest="sha256:" + "0" * 64,
            proposal_fingerprint=ledger_fingerprint(key, "write-operation-proposal-v1", {}),
            confirmation_token_fingerprint=ledger_fingerprint(
                key, "write-operation-confirmation-token-v1", b"token"
            ),
            authorization_scope_fingerprint=ledger_fingerprint(
                key, "write-operation-authorization-scope-v1", {}
            ),
        )
        session.commit()
    return sessions, repository, conversation.id, operation_id


@pytest.mark.parametrize("explicit", (True, False))
def test_preheader_has_one_ordered_bootstrap_and_never_selects_pending_body(
    tmp_path, explicit: bool
) -> None:
    sessions, repository, conversation_id, operation_id = _ledger(tmp_path)
    statements: list[str] = []
    engine = sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        header = repository.operation_preheader(
            conversation_id=conversation_id,
            operation_id=operation_id if explicit else None,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert header.operation.id == operation_id
    assert header.pending_pointer.operation_id == operation_id
    assert statements
    assert ("write_operations" in statements[0]) is explicit
    projection = next(item for item in statements if " from conversations " in item)
    assert "pending_args" not in projection
    assert "pending_human" not in projection


def test_preheader_never_scans_ledger_when_omitted_pointer_is_absent(tmp_path) -> None:
    _sessions, repository, conversation_id, _operation_id = _ledger(tmp_path)
    with repository.session_factory() as session:
        owner = session.get(Conversation, conversation_id)
        assert owner is not None
        owner.pending_operation_id = ""
        owner.pending_tool_call_id = ""
        owner.pending_tool_name = ""
        session.commit()

    with pytest.raises(WriteOperationError, match="stale_pending_action"):
        repository.operation_preheader(conversation_id=conversation_id, operation_id=None)


def test_explicit_operation_with_deleted_owner_is_unavailable(tmp_path) -> None:
    sessions, repository, conversation_id, operation_id = _ledger(tmp_path)
    with sessions() as session:
        owner = session.get(Conversation, conversation_id)
        assert owner is not None
        session.delete(owner)
        session.commit()

    with pytest.raises(WriteOperationError, match="operation_unavailable"):
        repository.operation_preheader(
            conversation_id=conversation_id,
            operation_id=operation_id,
        )
