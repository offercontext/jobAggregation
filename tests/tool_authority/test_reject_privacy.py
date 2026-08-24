from __future__ import annotations

from uuid import uuid4

import pytest

from offerpilot.ai.write_operations import (
    OperationFailed,
    WriteOperationCoordinator,
    WriteOperationRepository,
    ledger_fingerprint,
    operation_request_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, Conversation, WriteOperation
from offerpilot.repositories.chat import ChatRepository


def _setup(tmp_path, raw_args: str):
    sessions = init_database(tmp_path / "reject.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    conversation = ChatRepository(sessions, repository).create_conversation("reject")
    operation_id = str(uuid4())
    proposal = ledger_fingerprint(key, "write-operation-proposal-v1", {})
    token = "t" * 64
    token_fingerprint = ledger_fingerprint(
        key, "write-operation-confirmation-token-v1", token.encode("ascii")
    )
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.pending_operation_id = operation_id
        owner.pending_tool_call_id = "call-privacy"
        owner.pending_tool_name = "create_application"
        owner.pending_args = raw_args
        owner.pending_human = "must-not-be-read"
        repository.create_primary(
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id="call-privacy",
            tool_name="create_application",
            adapter_kind="typed",
            proposal_fingerprint=proposal,
            confirmation_token_fingerprint=token_fingerprint,
            authorization_scope_fingerprint=ledger_fingerprint(
                key, "write-operation-authorization-scope-v1", {}
            ),
        )
        session.commit()
    request_fingerprint = operation_request_fingerprint(
        key,
        operation_id=operation_id,
        tool_call_id="call-privacy",
        approved=False,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=token_fingerprint,
        proposal_fingerprint=proposal,
    )
    return sessions, repository, conversation.id, operation_id, token, request_fingerprint


@pytest.mark.parametrize(
    "raw_args",
    (
        pytest.param('{"malformed":', id="malformed"),
        pytest.param("x" * 65_537, id="oversized"),
    ),
)
def test_plain_omitted_token_reject_never_decodes_pending_args(tmp_path, raw_args: str) -> None:
    sessions, repository, conversation_id, operation_id, _token, fingerprint = _setup(
        tmp_path, raw_args
    )
    result = WriteOperationCoordinator(repository).reject_primary(
        operation_id=operation_id,
        conversation_id=conversation_id,
        tool_call_id="call-privacy",
        tool_name="create_application",
        request_fingerprint=fingerprint,
        visible_result="已取消这次操作。",
        confirmation_token=None,
    )

    assert isinstance(result, OperationFailed)
    assert result.ownership is None
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        owner = session.get(Conversation, conversation_id)
        assert operation is not None and operation.status == "rejected"
        assert owner is not None and owner.pending_operation_id == ""
        messages = session.query(ChatMessage).filter_by(operation_id=operation_id).all()
        assert len(messages) == 2


def test_supplied_token_is_checked_by_canonical_fingerprint_only(tmp_path) -> None:
    sessions, repository, conversation_id, operation_id, _token, fingerprint = _setup(
        tmp_path, '{"malformed":'
    )
    result = WriteOperationCoordinator(repository).reject_primary(
        operation_id=operation_id,
        conversation_id=conversation_id,
        tool_call_id="call-privacy",
        tool_name="create_application",
        request_fingerprint=fingerprint,
        visible_result="已取消这次操作。",
        confirmation_token="wrong",
    )
    assert getattr(result, "code", None) == "operation_input_conflict"
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        owner = session.get(Conversation, conversation_id)
        assert operation is not None and operation.status == "proposed"
        assert owner is not None and owner.pending_operation_id == operation_id


def test_deleted_owning_conversation_cannot_be_rebound_for_reject(tmp_path) -> None:
    sessions, repository, conversation_id, operation_id, _token, fingerprint = _setup(
        tmp_path, "{}"
    )
    with sessions() as session:
        owner = session.get(Conversation, conversation_id)
        assert owner is not None
        session.delete(owner)
        session.commit()

    result = WriteOperationCoordinator(repository).reject_primary(
        operation_id=operation_id,
        conversation_id=conversation_id,
        tool_call_id="call-privacy",
        tool_name="create_application",
        request_fingerprint=fingerprint,
        visible_result="已取消这次操作。",
    )
    assert getattr(result, "code", None) == "operation_unavailable"
