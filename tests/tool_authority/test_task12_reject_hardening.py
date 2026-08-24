from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import offerpilot.ai.write_operations as write_operations_module
from offerpilot.ai.tool_authority import AuthorityFactory, AuthorityPhaseError
from offerpilot.ai.write_operations import (
    WriteOperationCoordinator,
    WriteOperationError,
)
from offerpilot.models import Conversation
from tests.tool_authority.test_reject_privacy import _setup


_HMAC_FINGERPRINT = "hmac-sha256:" + "a" * 64
_ARGUMENTS_DIGEST = "sha256:" + "b" * 64


def _register_proof_sources(
    factory: AuthorityFactory,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    operation = SimpleNamespace(
        id="operation-1",
        status="proposed",
        adapter_kind="typed",
        tool_call_id="call-1",
        tool_name="add_note",
        proposal_fingerprint=_HMAC_FINGERPRINT,
        confirmation_token_fingerprint=_HMAC_FINGERPRINT,
        conversation_id=1,
    )
    pointer = SimpleNamespace(
        conversation_id=1,
        operation_id="operation-1",
        tool_call_id="call-1",
        tool_name="add_note",
        pending_confirmation_claim_id="",
    )
    factory.register_operation(operation)
    factory.register_pending(
        pointer,
        conversation_id=1,
        operation_id="operation-1",
        tool_call_id="call-1",
        tool_name="add_note",
        pending_action_revision=1,
        pending_confirmation_claim_id="",
        arguments_digest=_ARGUMENTS_DIGEST,
        effective_args_digest=_ARGUMENTS_DIGEST,
    )
    return operation, pointer


def test_omitted_token_proof_requires_exact_session_outer_transaction() -> None:
    factory = AuthorityFactory()
    operation, pointer = _register_proof_sources(factory)
    fake_transaction = object()

    with pytest.raises(AuthorityPhaseError, match="transaction"):
        factory.register_transaction(fake_transaction)
        factory.issue_omitted_token_proof(
            operation,
            pending_pointer=pointer,
            transaction=fake_transaction,
        )


@pytest.mark.parametrize("provided_token", (False, True), ids=("omitted", "provided"))
def test_reject_operation_terminalization_is_one_exact_proposed_identity_cas(
    tmp_path,
    provided_token: bool,
) -> None:
    sessions, repository, conversation_id, operation_id, token, fingerprint = _setup(
        tmp_path,
        "{}",
    )
    statements: list[str] = []
    engine = sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update write_operations set status"):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        WriteOperationCoordinator(repository).reject_primary(
            operation_id=operation_id,
            conversation_id=conversation_id,
            tool_call_id="call-privacy",
            tool_name="create_application",
            request_fingerprint=fingerprint,
            visible_result="cancelled",
            confirmation_token=token if provided_token else None,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 1
    where_clause = statements[0].split(" where ", maxsplit=1)[1]
    for exact_guard in (
        "write_operations.id",
        "write_operations.status",
        "write_operations.conversation_id",
        "write_operations.adapter_kind",
        "write_operations.tool_call_id",
        "write_operations.tool_name",
        "write_operations.proposal_fingerprint",
        "write_operations.confirmation_token_fingerprint",
    ):
        assert exact_guard in where_clause


def test_omitted_token_proof_stays_live_until_successful_database_commit(
    tmp_path,
) -> None:
    sessions, repository, conversation_id, operation_id, _token, fingerprint = _setup(
        tmp_path,
        "{}",
    )
    lifecycle: list[str] = []

    class SpyFactory(AuthorityFactory):
        def issue_omitted_token_proof(self, *args, **kwargs):
            lifecycle.append("proof-issued")
            return super().issue_omitted_token_proof(*args, **kwargs)

        def consume(self, value):
            lifecycle.append("proof-consumed")
            return super().consume(value)

        def revoke(self, value):
            lifecycle.append("proof-revoked")
            return super().revoke(value)

        def close(self) -> None:
            lifecycle.append("factory-closed")
            super().close()

    original_commit = Session.commit

    def commit(session: Session) -> None:
        lifecycle.append("database-commit")
        original_commit(session)

    with (
        patch.object(write_operations_module, "AuthorityFactory", SpyFactory),
        patch.object(Session, "commit", commit),
    ):
        WriteOperationCoordinator(repository).reject_primary(
            operation_id=operation_id,
            conversation_id=conversation_id,
            tool_call_id="call-privacy",
            tool_name="create_application",
            request_fingerprint=fingerprint,
            visible_result="cancelled",
            confirmation_token=None,
        )

    assert lifecycle.index("database-commit") < lifecycle.index("proof-consumed")
    assert lifecycle.index("proof-consumed") < lifecycle.index("factory-closed")
    assert "proof-revoked" not in lifecycle


def test_unknown_commit_revokes_omitted_token_proof_instead_of_consuming_it(
    tmp_path,
) -> None:
    sessions, repository, conversation_id, operation_id, _token, fingerprint = _setup(
        tmp_path,
        "{}",
    )
    lifecycle: list[str] = []

    class SpyFactory(AuthorityFactory):
        def consume(self, value):
            lifecycle.append("proof-consumed")
            return super().consume(value)

        def revoke(self, value):
            lifecycle.append("proof-revoked")
            return super().revoke(value)

        def close(self) -> None:
            lifecycle.append("factory-closed")
            super().close()

    original_commit = Session.commit
    commit_attempts = 0

    def fail_first_commit(session: Session) -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise OperationalError("COMMIT", {}, Exception("unknown commit"))
        original_commit(session)

    with (
        patch.object(write_operations_module, "AuthorityFactory", SpyFactory),
        patch.object(Session, "commit", fail_first_commit),
    ):
        WriteOperationCoordinator(repository).reject_primary(
            operation_id=operation_id,
            conversation_id=conversation_id,
            tool_call_id="call-privacy",
            tool_name="create_application",
            request_fingerprint=fingerprint,
            visible_result="cancelled",
            confirmation_token=None,
        )

    assert "proof-revoked" in lifecycle
    assert "proof-consumed" not in lifecycle
    assert lifecycle.index("proof-revoked") < lifecycle.index("factory-closed")


def test_explicit_terminal_preheader_does_not_read_conversation_pending_pointer(
    tmp_path,
) -> None:
    sessions, repository, conversation_id, operation_id, token, fingerprint = _setup(
        tmp_path,
        "{}",
    )
    WriteOperationCoordinator(repository).reject_primary(
        operation_id=operation_id,
        conversation_id=conversation_id,
        tool_call_id="call-privacy",
        tool_name="create_application",
        request_fingerprint=fingerprint,
        visible_result="cancelled",
        confirmation_token=token,
    )
    selects: list[str] = []
    engine = sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select"):
            selects.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        preheader = repository.operation_preheader(
            conversation_id=conversation_id,
            operation_id=operation_id,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert preheader.operation.status == "rejected"
    assert len(selects) == 1
    assert " from write_operations " in selects[0]
    assert all(" from conversations " not in statement for statement in selects)


def test_explicit_terminal_with_deleted_conversation_is_unavailable_without_pointer_read(
    tmp_path,
) -> None:
    sessions, repository, conversation_id, operation_id, token, fingerprint = _setup(
        tmp_path,
        "{}",
    )
    WriteOperationCoordinator(repository).reject_primary(
        operation_id=operation_id,
        conversation_id=conversation_id,
        tool_call_id="call-privacy",
        tool_name="create_application",
        request_fingerprint=fingerprint,
        visible_result="cancelled",
        confirmation_token=token,
    )
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        session.delete(conversation)
        session.commit()

    selects: list[str] = []
    engine = sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select"):
            selects.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(WriteOperationError, match="operation_unavailable"):
            repository.operation_preheader(
                conversation_id=conversation_id,
                operation_id=operation_id,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(selects) == 1
    assert " from write_operations " in selects[0]
    assert all(" from conversations " not in statement for statement in selects)
