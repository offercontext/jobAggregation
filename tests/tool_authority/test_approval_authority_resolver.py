from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from offerpilot.ai.tool_authority import AuthorityFactory, ApprovalExecutionAuthority
from offerpilot.ai.write_operations import (
    WriteOperationError,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import Conversation
from offerpilot.pilot_runtime.continuation import ApprovalAuthorityResolver
from offerpilot.repositories.chat import ChatRepository
from tests.tool_authority.test_pending_claim import create_primary_with_typed_route


def _revision(tool_call_id: str, tool_name: str, raw_args: str) -> int:
    normalized = json.dumps(json.loads(raw_args), separators=(",", ":"))
    payload = json.dumps(
        {"args": normalized, "tool_call_id": tool_call_id, "tool_name": tool_name},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1)


def _setup(tmp_path):
    sessions = init_database(tmp_path / "resolver.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    conversation = ChatRepository(sessions, repository).create_conversation("resolver")
    operation_id = str(uuid4())
    args = "{}"
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.pending_operation_id = operation_id
        owner.pending_tool_call_id = "call-resolver"
        owner.pending_tool_name = "create_application"
        owner.pending_args = args
        operation = create_primary_with_typed_route(
            repository,
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id="call-resolver",
            tool_name="create_application",
            raw_args=args,
            proposal_fingerprint=ledger_fingerprint(key, "write-operation-proposal-v1", {}),
            confirmation_token_fingerprint=ledger_fingerprint(
                key, "write-operation-confirmation-token-v1", b"token"
            ),
            authorization_scope_fingerprint=ledger_fingerprint(
                key, "write-operation-authorization-scope-v1", {}
            ),
        )
        session.commit()
    operation = repository.get(operation_id)
    assert operation is not None
    digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    revision = _revision("call-resolver", "create_application", args)
    identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id="call-resolver",
        tool_name="create_application",
        pending_action_revision=revision,
        arguments_digest=digest,
        effective_args_digest=digest,
    )
    return sessions, repository, operation, identity, revision, digest


def test_resolver_reads_only_bounded_ledger_scope_and_visibility(tmp_path) -> None:
    sessions, repository, operation, identity, revision, digest = _setup(tmp_path)
    statements: list[str] = []
    engine = sessions.kw["bind"]

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.lower().split()))

    factory = AuthorityFactory()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        authority = ApprovalAuthorityResolver(repository, factory).resolve(
            operation=operation,
            pending=identity,
            conversation_id=identity.conversation_id,
            pending_action_revision=revision,
            effective_args_digest=digest,
        )
        assert isinstance(authority, ApprovalExecutionAuthority)
        assert all("pending_args" not in statement for statement in statements)
        assert all("pending_human" not in statement for statement in statements)
        assert all("chat_messages" not in statement for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        factory.close()


@pytest.mark.parametrize(
    ("status", "fingerprint", "code"),
    (
        ("committed", "hmac-sha256:" + "0" * 64, "operation_identity_conflict"),
        ("proposed", None, "authorization_scope_unbound"),
    ),
)
def test_resolver_short_circuits_terminal_and_unbound_before_conversation(
    status: str, fingerprint: str | None, code: str
) -> None:
    ledger_row = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        conversation_id=1,
        status=status,
        adapter_kind="typed",
        tool_call_id="call-resolver",
        tool_name="create_application",
        proposal_fingerprint="hmac-sha256:" + "1" * 64,
        confirmation_token_fingerprint="hmac-sha256:" + "2" * 64,
        authorization_scope_fingerprint=fingerprint,
    )
    statements: list[object] = []

    class Result:
        def one_or_none(self):
            return ledger_row

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            statements.append(statement)
            return Result()

    repository = SimpleNamespace(session_factory=lambda: Session())
    operation = SimpleNamespace(id=ledger_row.id)
    identity = SimpleNamespace()
    factory = AuthorityFactory()
    try:
        with pytest.raises(WriteOperationError, match=code):
            ApprovalAuthorityResolver(repository, factory).resolve(
                operation=operation,
                pending=identity,
                conversation_id=1,
                pending_action_revision=0,
                effective_args_digest="sha256:" + "3" * 64,
            )
        assert len(statements) == 1
    finally:
        factory.close()
