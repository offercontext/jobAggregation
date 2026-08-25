from __future__ import annotations

import os
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.policy_types import ToolCapability, UndoPolicy
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ProviderToolContract,
    REQUIRED_UNDO_TOOL_NAMES,
    TRANSACTIONAL_TYPED_WRITE_NAMES,
    ToolExceptionMapping,
)
from offerpilot.ai.tool_runtime.metadata import (
    EditableFieldMetadataV1,
    ToolPresentationBindingV1,
    UndoBuilderBinding,
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
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationCoordinator,
    WriteOperationError,
    WriteOperationRepository,
    compensation_operation_id,
    ledger_fingerprint,
    load_or_create_ledger_key,
    operation_request_fingerprint,
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
from tests.tool_metadata.factories import synthetic_tool_spec, write_metadata


def _capture_empty_undo_seed(_context: object, _args: object) -> None:
    return None


def _build_test_undo(_seed: object, _record: object) -> dict[str, object]:
    return {"kind": "delete_application"}


def _raise_undo_projection_failure(_seed: object, _record: object) -> None:
    raise RuntimeError("undo failed")


def _commit_during_undo_seed(context: object, _args: object) -> dict[str, object]:
    getattr(context, "bound_session").commit()
    return {}


def _rollback_during_undo_seed(context: object, _args: object) -> dict[str, object]:
    getattr(context, "bound_session").rollback()
    return {}


def _close_during_undo_seed(context: object, _args: object) -> dict[str, object]:
    getattr(context, "bound_session").close()
    return {}


def _render_test_success(result: object) -> str:
    return str(result)


def _raise_success_projection_failure(_result: object) -> str:
    raise RuntimeError("presentation failed")


_UNDO_SEED_ENDERS = {
    "commit": _commit_during_undo_seed,
    "rollback": _rollback_during_undo_seed,
    "close": _close_during_undo_seed,
}


def _test_write_spec(
    contract: ProviderToolContract,
    executor: object,
    *,
    editable_fields: tuple[EditableFieldMetadataV1, ...] = (),
    declared_failure_categories: frozenset[str] = frozenset(),
    exception_map: tuple[ToolExceptionMapping, ...] = (),
    undo_capture: object | None = None,
    undo_builder: object | None = None,
    success_projector: object = _render_test_success,
):
    undo_required = undo_capture is not None or undo_builder is not None
    metadata = replace(
        write_metadata(
            name=contract.name,
            undo_policy=(UndoPolicy.REQUIRED if undo_required else UndoPolicy.NONE),
            resolver_descriptors=(),
        ),
        editable_fields=editable_fields,
    )
    base = synthetic_tool_spec(contract.name, metadata=metadata)
    binding = None
    if undo_required:
        assert undo_capture is not None and undo_builder is not None
        operation = metadata.operation
        assert operation.undo_builder_id is not None
        binding = UndoBuilderBinding(
            descriptor=operation,
            implementation_id=operation.undo_builder_id,
            capture_seed=undo_capture,
            build_undo=undo_builder,
        )
    return replace(
        base,
        contract=contract,
        executor=executor,
        presentation=ToolPresentationBindingV1(
            implementation_id="test_write_presentation_v1",
            confirmation_description=base.presentation.confirmation_description,
            pending_details_projector=base.presentation.pending_details_projector,
            success_summary_projector=success_projector,
        ),
        undo_builder_binding=binding,
        declared_failure_categories=declared_failure_categories,
        exception_map=exception_map,
    )


def _approval_request_fingerprint(key, operation_id: str, pending: PendingAction) -> str:
    return operation_request_fingerprint(
        key,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=ledger_fingerprint(
            key, "write-operation-confirmation-token-v1", b"synthetic-token"
        ),
        proposal_fingerprint=ledger_fingerprint(
            key, "write-operation-proposal-v1", {}
        ),
    )


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
        setup_conversation.pending_args = pending.args
        setup_conversation.pending_human = pending.human
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
            authorization_scope_fingerprint=authorization_scope_fingerprint(
                key,
                conversation_id=conversation.id,
                conversation_scope_revision=0,
                context_type="workspace",
                context_ref=None,
                mode="general",
                capability_profile_id="agent_typed_v1",
                capability_policy_version="capability-policy-v1",
                binding_policy_version="binding-policy-v1",
                capability_profile_fingerprint="sha256:" + "0" * 64,
                binding_policy_fingerprint="sha256:" + "0" * 64,
            ),
        )
        setup_session.commit()

    applications = ApplicationsRepository(sessions)
    events = ApplicationEventsRepository(sessions)
    notes = NotesRepository(sessions)
    offers = OffersRepository(sessions)
    resumes = ResumesRepository(sessions)
    factory = AuthorityFactory()
    arguments_digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
    pending_action_revision = _pending_revision(
        pending.tool_call_id, pending.tool_name, pending.args
    )
    pending_identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=pending_action_revision,
        effective_args_digest=arguments_digest,
    )
    factory.register_pending(pending_identity)
    authority = factory.create_approval_authority(
        operation_id=operation_id,
        conversation_id=conversation.id,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending_identity,
        pending_action_revision=pending_action_revision,
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

    spec = _test_write_spec(
        contract,
        mutate_then_fail,
        exception_map=(ToolExceptionMapping(ValueError, "conflict", "domain_conflict"),),
        declared_failure_categories=frozenset({"conflict"}),
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
        pending_action_revision=pending_action_revision,
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
        request_fingerprint=_approval_request_fingerprint(
            key, operation_id, pending
        ),
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


def _pending_revision(tool_call_id: str, tool_name: str, raw_args: str) -> int:
    normalized = json.dumps(
        json.loads(raw_args),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical = json.dumps(
        {"args": normalized, "tool_call_id": tool_call_id, "tool_name": tool_name},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big") & ((1 << 63) - 1)


def _primary_execution_harness(
    tmp_path,
    executor,
    *,
    proposal_args: str = "{}",
    effective_args: str | None = None,
    edited_args: dict[str, object] | None = None,
    tool_name: str = "create_application",
    undo_capture: object | None = None,
    undo_builder: object | None = None,
    success_projector: object = _render_test_success,
):
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("workspace")
    operation_id = str(uuid4())
    pending = PendingAction(
        tool_call_id="write-once",
        tool_name=tool_name,
        args=proposal_args,
        human="create",
        operation_id=operation_id,
    )
    decided_args = pending.args if effective_args is None else effective_args
    revision = _pending_revision(pending.tool_call_id, pending.tool_name, decided_args)
    with sessions() as setup_session:
        setup_conversation = setup_session.get(Conversation, conversation.id)
        assert setup_conversation is not None
        setup_conversation.pending_operation_id = operation_id
        setup_conversation.pending_tool_call_id = pending.tool_call_id
        setup_conversation.pending_tool_name = pending.tool_name
        setup_conversation.pending_args = pending.args
        setup_conversation.pending_human = pending.human
        repository.create_primary(
            setup_session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            adapter_kind="typed",
            proposal_fingerprint=ledger_fingerprint(
                key,
                "write-operation-proposal-v1",
                json.loads(proposal_args),
            ),
            confirmation_token_fingerprint=ledger_fingerprint(
                key, "write-operation-confirmation-token-v1", b"synthetic-token"
            ),
            authorization_scope_fingerprint=authorization_scope_fingerprint(
                key,
                conversation_id=conversation.id,
                conversation_scope_revision=0,
                context_type="workspace",
                context_ref=None,
                mode="general",
                capability_profile_id="agent_typed_v1",
                capability_policy_version="capability-policy-v1",
                binding_policy_version="binding-policy-v1",
                capability_profile_fingerprint="sha256:" + "0" * 64,
                binding_policy_fingerprint="sha256:" + "0" * 64,
            ),
        )
        setup_session.commit()

    applications = ApplicationsRepository(sessions)
    events = ApplicationEventsRepository(sessions)
    notes = NotesRepository(sessions)
    offers = OffersRepository(sessions)
    resumes = ResumesRepository(sessions)
    factory = AuthorityFactory()
    arguments_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            json.loads(decided_args),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pending_identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=revision,
        effective_args_digest=arguments_digest,
    )
    factory.register_pending(pending_identity)
    authority = factory.create_approval_authority(
        operation_id=operation_id,
        conversation_id=conversation.id,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending_identity,
        pending_action_revision=revision,
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
    editable_fields = tuple(
        EditableFieldMetadataV1(
            field=key,
            value_type="long_text",
            options=None,
            clearable=False,
            clear_value=None,
        )
        for key in (edited_args or {})
    )
    parameters = {
        "type": "object",
        "properties": {
            descriptor.field: {"type": "string"} for descriptor in editable_fields
        },
    }
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {
                "name": pending.tool_name,
                "description": "create",
                "parameters": parameters,
            },
        },
        name=pending.tool_name,
        description="create",
        parameters=parameters,
    )
    spec = _test_write_spec(
        contract,
        executor,
        editable_fields=editable_fields,
        undo_capture=undo_capture,
        undo_builder=undo_builder,
        success_projector=success_projector,
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
        ToolCall(pending.tool_call_id, pending.tool_name, decided_args),
        call_identity=prepare_identity,
        pending_identity=pending_identity,
        pending_action_revision=revision,
        record_proposal=False,
    )
    assert isinstance(prepared_result, ConfirmationRequired)
    return SimpleNamespace(
        sessions=sessions,
        repository=repository,
        conversation=conversation,
        operation_id=operation_id,
        pending=pending,
        factory=factory,
        context=context,
        prepared=prepared_result.prepared,
        prepare_identity=prepare_identity,
        coordinator=WriteOperationCoordinator(repository),
        request_fingerprint=(
            _approval_request_fingerprint(key, operation_id, pending)
            if edited_args is None
            else operation_request_fingerprint(
                key,
                operation_id=operation_id,
                tool_call_id=pending.tool_call_id,
                approved=True,
                edited_args_present=True,
                edited_args=edited_args,
                rejection_feedback_present=False,
                rejection_feedback="",
                confirmation_token_fingerprint=ledger_fingerprint(
                    key,
                    "write-operation-confirmation-token-v1",
                    b"synthetic-token",
                ),
                proposal_fingerprint=ledger_fingerprint(
                    key,
                    "write-operation-proposal-v1",
                    json.loads(proposal_args),
                ),
            )
        ),
        edited_args_present=edited_args is not None,
        edited_args=edited_args,
    )


def test_locked_modify_executes_effective_args_against_original_proposal(tmp_path) -> None:
    seen: list[dict[str, object]] = []

    def executor(args, _context):
        seen.append(dict(args))
        return {"ok": True}

    harness = _primary_execution_harness(
        tmp_path,
        executor,
        effective_args='{"assessment":"changed"}',
        edited_args={"assessment": "changed"},
        tool_name="save_offer_assessment",
    )
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            edited_args_present=harness.edited_args_present,
            edited_args=harness.edited_args,
        )
        assert isinstance(execution, OperationCommitted)
        assert record is not None
        assert seen == [{"assessment": "changed"}]
    finally:
        harness.factory.close()


def test_locked_modify_rejects_patch_prepared_mismatch_before_executor(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = _primary_execution_harness(
        tmp_path,
        executor,
        effective_args='{"assessment":"changed"}',
        edited_args={"assessment": "different"},
        tool_name="save_offer_assessment",
    )
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            edited_args_present=True,
            edited_args=harness.edited_args,
        )

        assert isinstance(execution, OperationUnknown)
        assert execution.code == "operation_input_conflict"
        assert record is None
        assert calls == 0
    finally:
        harness.factory.close()


@pytest.mark.parametrize(
    ("proposal_args", "effective_args", "edited_args"),
    [
        ("{}", "{}", {}),
        (
            '{"assessment":"changed"}',
            '{"assessment":"changed"}',
            {"assessment": "changed"},
        ),
    ],
)
def test_locked_modify_preserves_explicit_patch_presence_when_effective_is_unchanged(
    tmp_path,
    proposal_args: str,
    effective_args: str,
    edited_args: dict[str, object],
) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = _primary_execution_harness(
        tmp_path,
        executor,
        proposal_args=proposal_args,
        effective_args=effective_args,
        edited_args=edited_args,
        tool_name="save_offer_assessment",
    )
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            edited_args_present=True,
            edited_args=edited_args,
        )

        assert isinstance(execution, OperationCommitted)
        assert record is not None
        assert calls == 1
    finally:
        harness.factory.close()


@pytest.mark.parametrize("failure_site", ("presentation", "transport", "undo"))
def test_post_executor_projection_failure_terminalizes_without_rerun(
    tmp_path, monkeypatch, failure_site: str
) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = _primary_execution_harness(
        tmp_path,
        executor,
        undo_capture=(
            _capture_empty_undo_seed if failure_site == "undo" else None
        ),
        undo_builder=(
            _raise_undo_projection_failure if failure_site == "undo" else None
        ),
        success_projector=(
            _raise_success_projection_failure
            if failure_site == "presentation"
            else _render_test_success
        ),
    )
    if failure_site == "transport":
        monkeypatch.setattr(
            "offerpilot.ai.write_operations.project_transport_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport failed")),
        )
    arguments = dict(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
    )
    try:
        first, first_record = harness.coordinator.execute_primary(**arguments)
        replay, replay_record = harness.coordinator.execute_primary(**arguments)
        assert isinstance(first, OperationFailed)
        assert first_record is not None and first_record.execution_started
        assert first.payload.failure_code == "operation_projection_failed"
        assert isinstance(replay, OperationReplay)
        assert replay_record is None
        assert calls == 1
    finally:
        harness.factory.close()


def test_ordinary_executor_exception_terminalizes_without_rerun(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        raise RuntimeError("private executor failure")

    harness = _primary_execution_harness(tmp_path, executor)
    arguments = dict(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
    )
    try:
        first, first_record = harness.coordinator.execute_primary(**arguments)
        replay, replay_record = harness.coordinator.execute_primary(**arguments)
        assert isinstance(first, OperationFailed)
        assert first_record is not None and first_record.execution_started
        assert first.payload.failure_code == "executor_exception"
        assert isinstance(replay, OperationReplay)
        assert replay_record is None
        assert calls == 1
    finally:
        harness.factory.close()


def test_locked_pending_args_change_rejects_before_executor(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = _primary_execution_harness(tmp_path, executor)
    with harness.sessions() as session:
        conversation = session.get(Conversation, harness.conversation.id)
        assert conversation is not None
        conversation.pending_args = '{"changed":true}'
        session.commit()
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
        )
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "operation_identity_conflict"
        assert record is None
        assert calls == 0
        with harness.sessions() as session:
            operation = session.get(WriteOperation, harness.operation_id)
            assert operation is not None and operation.status == "proposed"
    finally:
        harness.factory.close()


@pytest.mark.parametrize("end_transaction", ("commit", "rollback", "close"))
def test_undo_seed_cannot_end_claim_transaction_before_executor(
    tmp_path, end_transaction: str
) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = _primary_execution_harness(
        tmp_path,
        executor,
        undo_capture=_UNDO_SEED_ENDERS[end_transaction],
        undo_builder=_build_test_undo,
    )
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
        )
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "operation_not_committed"
        assert record is None
        assert calls == 0
        with harness.sessions() as session:
            operation = session.get(WriteOperation, harness.operation_id)
            conversation = session.get(Conversation, harness.conversation.id)
            transitions = session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == harness.operation_id)
                .order_by(WriteOperationTransition.seq)
            ).all()
            assert operation is not None and operation.status == "proposed"
            assert conversation is not None
            assert conversation.pending_confirmation_claim_id == ""
            assert conversation.pending_confirmation_claimed_at is None
            assert [(item.seq, item.state) for item in transitions] == [
                (1, "proposed")
            ]
    finally:
        harness.factory.close()
