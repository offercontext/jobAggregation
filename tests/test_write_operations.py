from __future__ import annotations

import os
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    ConfirmationRequired,
    ProviderToolContract,
    REQUIRED_UNDO_TOOL_NAMES,
    TRANSACTIONAL_TYPED_WRITE_NAMES,
    ToolExceptionMapping,
    ToolSpec,
    WriteContract,
)
from offerpilot.ai.tool_runtime.pipeline import prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.ai.write_operations import (
    COMPENSATION_OPERATION_NAMES,
    LEDGER_KEY_FILENAME,
    LEGACY_WRITE_OPERATION_NAMES,
    REQUIRED_UNDO_OPERATION_NAMES,
    TYPED_WRITE_OPERATION_NAMES,
    DeliveryHeartbeat,
    DeliveryOwnership,
    OperationFailed,
    OperationUnknown,
    WriteOperationCoordinator,
    WriteOperationError,
    WriteOperationRepository,
    compensation_operation_id,
    ledger_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from offerpilot.models import Conversation, WriteOperation, WriteOperationTransition


def test_write_operation_manifests_are_exact() -> None:
    assert frozenset(TYPED_WRITE_OPERATION_NAMES) == TRANSACTIONAL_TYPED_WRITE_NAMES
    assert len(TYPED_WRITE_OPERATION_NAMES) == 12
    assert LEGACY_WRITE_OPERATION_NAMES == (
        "save_application_jd_version",
        "create_application_submission_snapshot",
        "record_application_outcome",
    )
    assert COMPENSATION_OPERATION_NAMES == (
        "undo:update_application_status",
        "undo:create_application",
        "undo:create_application_event",
        "undo:add_note",
    )
    assert REQUIRED_UNDO_OPERATION_NAMES == REQUIRED_UNDO_TOOL_NAMES


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("undo:update_application_status", "f5cb2151-0014-5ac3-b392-01cb650c67af"),
        ("undo:create_application", "007fcd71-31a0-5489-a474-9fe0ab59bb90"),
        ("undo:create_application_event", "920dc6a7-e9b8-5484-8749-9a1cf37b1b06"),
        ("undo:add_note", "9d8c2d9c-8a14-5e26-bad1-9fd5fb5ef73c"),
    ),
)
def test_compensation_operation_id_matches_design_golden(kind: str, expected: str) -> None:
    parent = "00000000-0000-4000-8000-000000000001"
    assert compensation_operation_id(parent, kind) == expected


def test_ledger_key_is_independent_and_missing_key_fails_closed(tmp_path) -> None:
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    pending = PendingAction(
        tool_call_id="write-1",
        tool_name="save_application_jd_version",
        args='{"id":1,"status":"offer"}',
        human="update",
        operation_id=str(uuid4()),
    )
    assert chat.persist_pending_action(conversation.id, pending, [])

    key_path = tmp_path / LEDGER_KEY_FILENAME
    key_path.unlink()
    with pytest.raises(WriteOperationError, match="operation_unavailable"):
        load_or_create_ledger_key(tmp_path, sessions)


def test_ledger_key_creation_rechecks_after_winning_lock(tmp_path, monkeypatch) -> None:
    seed_dir = tmp_path / "seed"
    target_dir = tmp_path / "target"
    sessions = init_database(tmp_path / "offerpilot.db")
    seed = load_or_create_ledger_key(seed_dir, sessions)
    seed_payload = (seed_dir / LEDGER_KEY_FILENAME).read_bytes()
    target_key = target_dir / LEDGER_KEY_FILENAME
    target_lock = target_dir / f".{LEDGER_KEY_FILENAME}.lock"
    real_open = os.open

    def racing_open(path, flags, mode=0o777):
        if os.fspath(path) == os.fspath(target_lock):
            target_key.write_bytes(seed_payload)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    loaded = load_or_create_ledger_key(target_dir, sessions)

    assert loaded == seed
    assert target_key.read_bytes() == seed_payload


def test_delivery_heartbeat_renews_until_fenced(monkeypatch) -> None:
    import offerpilot.ai.write_operations as ledger_module

    monkeypatch.setattr(ledger_module, "monotonic", lambda: 10_000, raising=False)
    calls: list[int] = []

    class Repository:
        def heartbeat(self, _ownership):
            calls.append(len(calls) + 1)
            return len(calls) < 3

    class ImmediateEvent:
        def wait(self, _seconds):
            return False

        def set(self):
            return None

        def is_set(self):
            return False

    ownership = DeliveryOwnership("operation", 1, b"secret", "fingerprint")
    heartbeat = DeliveryHeartbeat(Repository(), ownership)  # type: ignore[arg-type]
    heartbeat._stop = ImmediateEvent()  # type: ignore[assignment]
    heartbeat._run()

    assert calls == [1, 2, 3]


def test_delivery_owner_has_only_fingerprint_public_serialization() -> None:
    ownership = DeliveryOwnership("operation", 2, b"raw-secret", "hmac-sha256:public")

    assert ownership.public_identity() == {
        "operation_id": "operation",
        "generation": 2,
        "fingerprint": "hmac-sha256:public",
    }
    assert b"raw-secret" not in repr(ownership).encode()
    with pytest.raises(TypeError):
        asdict(ownership)  # type: ignore[arg-type]


def test_bound_chat_operation_uses_caller_transaction(tmp_path) -> None:
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    pending = PendingAction(
        tool_call_id="bound-write",
        tool_name="save_application_jd_version",
        args='{"id":1,"status":"offer"}',
        human="update",
        operation_id=str(uuid4()),
    )

    with sessions() as session:
        assert chat.bind(session).persist_pending_action(conversation.id, pending, [])
        assert (
            session.get(Conversation, conversation.id).pending_operation_id == pending.operation_id
        )
        session.rollback()

    assert chat.get_conversation(conversation.id).pending_operation_id == ""


def test_transition_trigger_rejects_out_of_order_state(tmp_path) -> None:
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    operation_id = str(uuid4())
    assert chat.persist_pending_action(
        conversation.id,
        PendingAction(
            tool_call_id="transition-write",
                tool_name="save_application_jd_version",
            args='{"id":1,"status":"offer"}',
            human="update",
            operation_id=operation_id,
        ),
        [],
    )

    with sessions() as session, pytest.raises(IntegrityError):
        session.add(
            WriteOperationTransition(
                id=str(uuid4()), operation_id=operation_id, seq=3, state="claimed"
            )
        )
        session.commit()


def test_primary_operation_rejects_empty_tool_call_id(tmp_path) -> None:
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")

    with pytest.raises(IntegrityError):
        chat.persist_pending_action(
            conversation.id,
            PendingAction(
                tool_call_id="",
                tool_name="save_application_jd_version",
                args='{"id":1,"status":"offer"}',
                human="update",
                operation_id=str(uuid4()),
            ),
            [],
        )


def test_mapped_domain_failure_rolls_back_executor_savepoint(tmp_path) -> None:
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    operation_id = str(uuid4())
    pending = PendingAction(
        tool_call_id="write-savepoint",
        tool_name="create_application",
        args="{}",
        human="create",
        operation_id=operation_id,
    )
    with sessions() as setup_session:
        setup_conversation = setup_session.get(Conversation, conversation.id)
        assert setup_conversation is not None
        setup_conversation.pending_operation_id = operation_id
        setup_conversation.pending_tool_call_id = pending.tool_call_id
        setup_conversation.pending_tool_name = pending.tool_name
        repository.create_primary(
            setup_session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            adapter_kind="typed",
            proposal_fingerprint=ledger_fingerprint(key, "write-operation-proposal-v1", {}),
            confirmation_token_fingerprint=ledger_fingerprint(
                key, "write-operation-confirmation-token-v1", b"synthetic-token"
            ),
            authorization_scope_fingerprint="hmac-sha256:" + "b" * 64,
        )
        setup_session.commit()

    applications = ApplicationsRepository(sessions)
    events = ApplicationEventsRepository(sessions)
    notes = NotesRepository(sessions)
    offers = OffersRepository(sessions)
    resumes = ResumesRepository(sessions)
    factory = AuthorityFactory()
    arguments_digest = "sha256:" + __import__("hashlib").sha256(b"{}").hexdigest()
    pending_identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=1,
        effective_args_digest=arguments_digest,
    )
    factory.register_pending(pending_identity)
    authority = factory.create_approval_authority(
        operation_id=operation_id,
        conversation_id=conversation.id,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending_identity,
        pending_action_revision=1,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        effective_args_digest=arguments_digest,
        capabilities=frozenset({ToolCapability.APPLICATIONS_WRITE}),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=applications,
        events=events,
        notes=notes,
        offers=offers,
        resumes=resumes,
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    parameters = {"type": "object", "properties": {}}
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {
                    "name": "create_application",
                "description": "create",
                "parameters": parameters,
            },
        },
        name="create_application",
        description="create",
        parameters=parameters,
    )

    def mutate_then_fail(_args, bound_context):
        bound_context.applications.create(ApplicationCreate("partial", "write"))
        raise ValueError("conflict")

    spec = ToolSpec(
        contract=contract,
        kind="write",
        decoder=lambda values: values,
        executor=mutate_then_fail,
        confirmation_policy="required",
        exception_map=(ToolExceptionMapping(ValueError, "conflict", "domain_conflict"),),
        declared_failure_categories=frozenset({"conflict"}),
        write_contract=WriteContract(),
        binding_contract=BindingContract("none"),
    )
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    prepare_identity = factory.create_approved_write_prepare_identity(
        authority,
        approval_context=context,
        request_identity=object(),
    )
    prepared_result = prepare_call(
        catalog,
        context,
        ToolCall(pending.tool_call_id, pending.tool_name, pending.args),
        call_identity=prepare_identity,
        pending_identity=pending_identity,
        pending_action_revision=1,
        record_proposal=False,
    )
    assert isinstance(prepared_result, ConfirmationRequired)
    prepared = prepared_result.prepared

    execution, record = WriteOperationCoordinator(repository).execute_primary(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=prepare_identity,
        request_fingerprint="hmac-sha256:" + "a" * 64,
    )

    assert isinstance(execution, OperationFailed)
    assert record is not None
    assert applications.list() == []

    chat.delete_conversation(conversation.id)
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        assert operation.conversation_id is None
        operation.delivery_lease_expires_at = 0
        session.commit()
    takeover = repository.converge_expired_delivery(operation_id)
    assert isinstance(takeover, OperationUnknown)
    assert takeover.code == "operation_delivery_unknown"
    assert takeover.retryable is False
    factory.close()
