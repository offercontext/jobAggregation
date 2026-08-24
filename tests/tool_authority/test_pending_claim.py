from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

import pytest
from sqlalchemy import select

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.types import Message
from offerpilot.ai.tool_authority import (
    AuthorityFactory,
    AuthorityPhaseError,
    PendingAuthorityClaim,
    TrustedContextScope,
)
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    BindingContract,
    ProviderToolContract,
    ToolSpec,
)
from offerpilot.ai.write_operations import (
    WriteOperationRepository,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, Conversation, WriteOperation
from offerpilot.repositories.chat import ChatRepository, ConversationScopeMutationSnapshot


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _registered_invocation(factory: AuthorityFactory, authority: object) -> tuple[object, object]:
    runner = object()
    context = object()
    surface = object()
    binding = object()
    gateway = object()
    factory.register_runner_invocation(runner, authority=authority)  # type: ignore[arg-type]
    factory.register_tool_execution_context(context, authority=authority)  # type: ignore[arg-type]
    build = factory.create_provider_surface_build_identity(
        authority,  # type: ignore[arg-type]
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-pending",
    )
    surface_fingerprint = _digest({"surface": "pending"})
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=surface_fingerprint,
        candidate_count=1,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return invocation, context


@dataclass
class PendingHarness:
    sessions: Any
    operations: WriteOperationRepository
    chat: ChatRepository
    conversation_id: int
    factory: AuthorityFactory
    pending: PendingAction
    claim: PendingAuthorityClaim

    def close(self) -> None:
        self.factory.close()


def _harness(tmp_path: Any, *, segment_id: str = "segment-pending") -> PendingHarness:
    sessions = init_database(tmp_path / f"{segment_id}.db")
    key = load_or_create_ledger_key(tmp_path / segment_id, sessions)
    operations = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, operations)
    conversation = chat.create_conversation("workspace")
    arguments = {"id": 7, "status": "offer"}
    raw_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    arguments_digest = _digest(arguments)
    pending = PendingAction(
        tool_call_id=f"call-{segment_id}",
        tool_name="update_application_status",
        args=raw_arguments,
        human="更新状态",
        operation_id=str(uuid4()),
    )
    pending.bind_typed_proposal_identity(
        conversation_id=conversation.id,
        pending_action_revision=1,
        pending_confirmation_claim_id=pending.operation_id,
        arguments_digest=arguments_digest,
    )
    factory = AuthorityFactory()
    authority = factory.create_segment_authority(
        conversation_id=conversation.id,
        conversation_scope_revision=0,
        segment_id=segment_id,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset({"applications.write"}),
        capability_profile_fingerprint=_digest({"profile": 1}),
        binding_policy_fingerprint=_digest({"binding": 1}),
    )
    invocation, _ = _registered_invocation(factory, authority)
    attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
    prepare_identity = factory.create_new_turn_prepare_identity(
        invocation,
        attempt_id=attempt,
        candidate_ordinal=0,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        arguments_digest=arguments_digest,
    )
    parameters: dict[str, object] = {"type": "object", "properties": {}}
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {
                "name": pending.tool_name,
                "description": "",
                "parameters": parameters,
            },
        },
        name=pending.tool_name,
        description="",
        parameters=parameters,
    )
    spec = ToolSpec(
        contract=contract,
        kind="write",
        decoder=lambda value: value,
        executor=lambda value, _context: value,
        confirmation_policy="required",
        binding_contract=BindingContract("none"),
    )
    factory.register_tool_spec(
        spec,
        authority=authority,
        prepare_identity=prepare_identity,
    )
    prepared = factory.prepare_tool_call(
        authority,
        prepare_identity=prepare_identity,
        tool_call_id=pending.tool_call_id,
        spec=spec,
        arguments=arguments,
        typed_args=arguments,
        arguments_digest=arguments_digest,
        contract_fingerprint=_digest(dict(contract.payload)),
        binding=BindingAudit(status="unbound", target_count=0),
    )
    factory.register_pending(
        pending,
        conversation_id=conversation.id,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=1,
        pending_confirmation_claim_id=pending.operation_id,
        arguments_digest=arguments_digest,
    )
    claim = factory.issue_pending_claim(
        authority,
        prepared=prepared,
        pending=pending,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        arguments_digest=arguments_digest,
        pending_action_revision=1,
        pending_confirmation_claim_id=pending.operation_id,
    )
    return PendingHarness(
        sessions=sessions,
        operations=operations,
        chat=chat,
        conversation_id=conversation.id,
        factory=factory,
        pending=pending,
        claim=claim,
    )


def test_initial_typed_pending_delegates_and_commits_scope_hmac_atomically(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    calls: list[tuple[object, int, PendingAction, PendingAuthorityClaim]] = []
    original = harness.chat.persist_typed_pending

    def spy(
        session: object,
        conversation_id: int,
        pending: PendingAction,
        claim: PendingAuthorityClaim,
    ) -> None:
        calls.append((session, conversation_id, pending, claim))
        original(session, conversation_id, pending, claim)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.chat, "persist_typed_pending", spy)
    try:
        assert harness.chat.persist_pending_action(
            harness.conversation_id,
            harness.pending,
            [{"role": "assistant", "tool_calls": '[{"id":"call"}]'}],
            pending_authority_claim=harness.claim,
        )
        assert len(calls) == 1
        assert calls[0][1:] == (
            harness.conversation_id,
            harness.pending,
            harness.claim,
        )
        assert harness.factory.claim_state(harness.claim) is None
        with harness.sessions() as session:
            conversation = session.get(Conversation, harness.conversation_id)
            operation = session.get(WriteOperation, harness.pending.operation_id)
            messages = list(
                session.scalars(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == harness.conversation_id
                    )
                )
            )
        assert conversation is not None
        assert conversation.pending_operation_id == harness.pending.operation_id
        assert operation is not None
        expected_scope = authorization_scope_fingerprint(
            harness.operations.key,
            conversation_id=harness.conversation_id,
            conversation_scope_revision=0,
            context_type="workspace",
            context_ref=None,
            mode="general",
            capability_profile_id=harness.claim.capability_profile_id,
            capability_policy_version=harness.claim.capability_policy_version,
            binding_policy_version=harness.claim.binding_policy_version,
            capability_profile_fingerprint=harness.claim.capability_profile_fingerprint,
            binding_policy_fingerprint=harness.claim.binding_policy_fingerprint,
        )
        assert operation.authorization_scope_fingerprint == expected_scope
        assert operation.adapter_kind == "typed"
        assert [message.role for message in messages] == ["assistant"]
    finally:
        harness.close()


@pytest.mark.parametrize("route", ("set", "replace", "continuation"))
def test_every_typed_pending_route_delegates_the_exact_claim(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    harness = _harness(tmp_path, segment_id=f"segment-{route}")
    calls: list[tuple[int, PendingAction, PendingAuthorityClaim]] = []
    original = harness.chat.persist_typed_pending

    def spy(
        session: object,
        conversation_id: int,
        pending: PendingAction,
        claim: PendingAuthorityClaim,
    ) -> None:
        calls.append((conversation_id, pending, claim))
        original(session, conversation_id, pending, claim)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.chat, "persist_typed_pending", spy)
    try:
        if route == "set":
            persisted = harness.chat.set_pending_action(
                harness.conversation_id,
                harness.pending,
                pending_authority_claim=harness.claim,
            )
            assert persisted is True
        elif route == "replace":
            expected = PendingAction(
                "old-call", "save_application_jd_version", "{}", "old", str(uuid4())
            )
            with harness.sessions() as session:
                conversation = session.get(Conversation, harness.conversation_id)
                assert conversation is not None
                conversation.pending_tool_call_id = expected.tool_call_id
                conversation.pending_operation_id = expected.operation_id
                conversation.pending_tool_name = expected.tool_name
                conversation.pending_args = expected.args
                conversation.pending_human = expected.human
                session.commit()
            persisted = harness.chat.replace_pending_confirmation(
                harness.conversation_id,
                expected,
                harness.pending,
                tool_message=Message(role="tool", content="replace", tool_call_id="old-call"),
                undo=None,
                pending_authority_claim=harness.claim,
            )
            assert persisted is not None
        else:
            expected = PendingAction(
                "old-call", "save_application_jd_version", "{}", "old", str(uuid4())
            )
            with harness.sessions() as session:
                conversation = session.get(Conversation, harness.conversation_id)
                assert conversation is not None
                conversation.pending_tool_call_id = expected.tool_call_id
                conversation.pending_operation_id = expected.operation_id
                conversation.pending_tool_name = expected.tool_name
                conversation.pending_args = expected.args
                conversation.pending_human = expected.human
                session.commit()
                generation = conversation.updated_at
            assert generation is not None
            persisted = harness.chat.persist_confirmation_continuation(
                harness.conversation_id,
                generation,
                [{"role": "assistant", "content": "continue"}],
                pending=harness.pending,
                expected_pending=expected,
                pending_authority_claim=harness.claim,
            )
            assert persisted is not None
        assert calls == [(harness.conversation_id, harness.pending, harness.claim)]
        assert harness.factory.claim_state(harness.claim) is None
        operation = harness.operations.get(harness.pending.operation_id)
        assert operation is not None and operation.authorization_scope_fingerprint is not None
    finally:
        harness.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda harness: setattr(harness.pending, "args", '{"id":8,"status":"offer"}'),
        lambda harness: setattr(harness.pending, "operation_id", str(uuid4())),
        lambda harness: setattr(harness.pending, "tool_call_id", "wrong-call"),
        lambda harness: setattr(harness.pending, "tool_name", "add_note"),
    ],
    ids=("args", "operation", "tool-call", "tool-name"),
)
def test_typed_pending_identity_mismatch_revokes_before_any_durable_write(
    tmp_path: Any, mutate: Callable[[PendingHarness], None]
) -> None:
    harness = _harness(tmp_path)
    original_operation_id = harness.claim.operation_id
    mutate(harness)
    try:
        with pytest.raises(AuthorityPhaseError):
            harness.chat.persist_pending_action(
                harness.conversation_id,
                harness.pending,
                [{"role": "assistant", "content": "must rollback"}],
                pending_authority_claim=harness.claim,
            )
        assert harness.factory.claim_state(harness.claim) is None
        with harness.sessions() as session:
            conversation = session.get(Conversation, harness.conversation_id)
            operation = session.get(WriteOperation, original_operation_id)
            message_count = session.scalar(
                select(ChatMessage.id).where(
                    ChatMessage.conversation_id == harness.conversation_id
                )
            )
        assert conversation is not None and conversation.pending_tool_name == ""
        assert operation is None
        assert message_count is None
    finally:
        harness.close()


def test_cross_conversation_claim_is_revoked_before_any_write(tmp_path: Any) -> None:
    harness = _harness(tmp_path)
    other = harness.chat.create_conversation("other")
    try:
        with pytest.raises(AuthorityPhaseError):
            harness.chat.persist_pending_action(
                other.id,
                harness.pending,
                [],
                pending_authority_claim=harness.claim,
            )
        assert harness.factory.claim_state(harness.claim) is None
        assert harness.chat.get_pending_action(other.id) is None
        assert harness.operations.get(harness.pending.operation_id) is None
    finally:
        harness.close()


def test_scope_revision_change_revokes_claim_and_rolls_back(tmp_path: Any) -> None:
    harness = _harness(tmp_path)
    changed = harness.chat.patch_conversation_with_scope(
        harness.conversation_id,
        {},
        ConversationScopeMutationSnapshot("global", "", "general"),
        expected_scope_revision=0,
    )
    assert changed is not None and changed.scope_revision == 1
    try:
        with pytest.raises(AuthorityPhaseError):
            harness.chat.persist_pending_action(
                harness.conversation_id,
                harness.pending,
                [],
                pending_authority_claim=harness.claim,
            )
        assert harness.factory.claim_state(harness.claim) is None
        assert harness.chat.get_pending_action(harness.conversation_id) is None
        assert harness.operations.get(harness.pending.operation_id) is None
    finally:
        harness.close()


def test_cas_loser_revokes_claim_and_does_not_leave_operation(tmp_path: Any) -> None:
    harness = _harness(tmp_path)
    harness.chat.update_conversation_for_archive(
        harness.conversation_id,
        {"archived_at": datetime.now(timezone.utc)},
    )
    try:
        assert (
            harness.chat.persist_pending_action(
                harness.conversation_id,
                harness.pending,
                [],
                pending_authority_claim=harness.claim,
            )
            is False
        )
        assert harness.factory.claim_state(harness.claim) is None
        assert harness.operations.get(harness.pending.operation_id) is None
    finally:
        harness.close()


def test_typed_pending_without_claim_fails_closed_but_exact_legacy_remains_unbound(
    tmp_path: Any,
) -> None:
    sessions = init_database(tmp_path / "legacy.db")
    key = load_or_create_ledger_key(tmp_path / "legacy", sessions)
    operations = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, operations)
    conversation = chat.create_conversation("workspace")
    typed = PendingAction(
        "typed-call",
        "update_application_status",
        '{"id":1,"status":"offer"}',
        "typed",
        str(uuid4()),
    )
    with pytest.raises(AuthorityPhaseError):
        chat.persist_pending_action(conversation.id, typed, [])
    assert chat.get_pending_action(conversation.id) is None
    assert operations.get(typed.operation_id) is None

    legacy = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        '{"application_id":1,"jd_text":"x"}',
        "legacy",
        str(uuid4()),
    )
    assert chat.persist_pending_action(conversation.id, legacy, [])
    operation = operations.get(legacy.operation_id)
    assert operation is not None
    assert operation.adapter_kind == "legacy_deterministic"
    assert operation.authorization_scope_fingerprint is None
