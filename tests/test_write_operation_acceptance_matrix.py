from __future__ import annotations

# mypy: disable-error-code="no-untyped-def,no-untyped-call"

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    REQUIRED_UNDO_TOOL_NAMES,
    ToolExceptionMapping,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.write_operations import (
    LEGACY_WRITE_OPERATION_NAMES,
    TYPED_WRITE_OPERATION_NAMES,
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationCoordinator,
    WriteOperationRepository,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, WriteOperation
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


_UNDO_KINDS = {
    "create_application": "delete_application",
    "update_application_status": "update_application_status",
    "create_application_event": "delete_application_event",
    "add_note": "delete_note",
}


def _harness(tmp_path):
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    context = ToolExecutionContext(
        capabilities=frozenset(ToolCapability),
        current_bindings={},
        applications=ApplicationsRepository(sessions),
        events=ApplicationEventsRepository(sessions),
        notes=NotesRepository(sessions),
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    return sessions, repository, chat, context, WriteOperationCoordinator(repository)


def _propose(chat: ChatRepository, tool_name: str):
    conversation = chat.create_conversation("workspace", "", "general")
    operation_id = str(uuid4())
    tool_call_id = f"acceptance-{tool_name.replace(':', '-')}-{operation_id[:8]}"
    assert chat.persist_pending_action(
        conversation.id,
        PendingAction(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args="{}",
            human=f"execute {tool_name}",
            operation_id=operation_id,
        ),
        [],
    )
    return conversation, operation_id, tool_call_id


def _prepared(tool_name: str, tool_call_id: str, calls: list[str]):
    catalog_spec = MODEL_TOOL_CATALOG.resolve(tool_name)
    assert catalog_spec is not None

    def execute(_args, _context):
        calls.append(tool_name)
        return {"adapter": tool_name, "executions": len(calls)}

    spec = replace(
        catalog_spec,
        executor=execute,
        mutable_validator=None,
        success_renderer=lambda result: f"committed:{result['adapter']}",
        result_metadata=None,
    )
    return PreparedToolCall(
        tool_call_id=tool_call_id,
        spec=spec,
        arguments={},
        typed_args={},
        arguments_digest="sha256:" + "1" * 64,
        contract_fingerprint="sha256:" + "2" * 64,
        binding=BindingAudit("unbound", 0),
    )


def _prepare_identity(_prepared, _operation_id: str):
    return object()


def _execute_typed_parent(tmp_path, tool_name: str):
    sessions, repository, chat, context, coordinator = _harness(tmp_path)
    conversation, operation_id, tool_call_id = _propose(chat, tool_name)
    calls: list[str] = []
    prepared = _prepared(tool_name, tool_call_id, calls)
    request_fingerprint = "hmac-sha256:" + "3" * 64
    undo_builder = None
    if tool_name in REQUIRED_UNDO_TOOL_NAMES:
        def build_undo(_prepared, _record, _seed):
            return {"kind": _UNDO_KINDS[tool_name]}

        undo_builder = build_undo
    execution, _record = coordinator.execute_primary(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=_prepare_identity(prepared, operation_id),
        request_fingerprint=request_fingerprint,
        undo_builder=undo_builder,
    )
    assert isinstance(execution, OperationCommitted)
    return (
        sessions,
        repository,
        chat,
        context,
        coordinator,
        conversation,
        operation_id,
        prepared,
        request_fingerprint,
        calls,
        execution,
    )


@pytest.mark.parametrize("tool_name", TYPED_WRITE_OPERATION_NAMES)
def test_all_typed_ledger_adapters_execute_once_and_replay_without_runtime_calls(
    tmp_path, monkeypatch, tool_name: str
) -> None:
    (
        _sessions,
        _repository,
        _chat,
        context,
        coordinator,
        conversation,
        operation_id,
        prepared,
        request_fingerprint,
        calls,
        _first_execution,
    ) = _execute_typed_parent(tmp_path, tool_name)

    monkeypatch.setattr(
        "offerpilot.ai.write_operations.render_compatibility",
        lambda *_args, **_kwargs: pytest.fail("terminal replay invoked renderer"),
    )
    replay, record = coordinator.execute_primary(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=_prepare_identity(prepared, operation_id),
        request_fingerprint=request_fingerprint,
    )

    assert calls == [tool_name]
    assert isinstance(replay, OperationReplay)
    assert replay.payload.status == "committed"
    assert replay.payload.result_contract == "typed_json_v1"
    assert record is None


@pytest.mark.parametrize("tool_name", LEGACY_WRITE_OPERATION_NAMES)
def test_all_legacy_ledger_adapters_execute_once_and_replay(tmp_path, tool_name: str) -> None:
    _sessions, _repository, chat, _context, coordinator = _harness(tmp_path)
    conversation, operation_id, tool_call_id = _propose(chat, tool_name)
    calls: list[str] = []

    def execute(_session):
        calls.append(tool_name)
        return f"committed:{tool_name}"

    arguments = dict(
        operation_id=operation_id,
        conversation_id=conversation.id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        input_fingerprint="hmac-sha256:" + "4" * 64,
        request_fingerprint="hmac-sha256:" + "5" * 64,
        executor=execute,
    )
    first = coordinator.execute_legacy(**arguments)
    replay = coordinator.execute_legacy(**arguments)

    assert calls == [tool_name]
    assert isinstance(first, OperationCommitted)
    assert isinstance(replay, OperationReplay)
    assert replay.payload.result_contract == "legacy_string_v1"


@pytest.mark.parametrize(
    ("parent_tool", "compensation_kind"),
    (
        ("update_application_status", "undo:update_application_status"),
        ("create_application", "undo:create_application"),
        ("create_application_event", "undo:create_application_event"),
        ("add_note", "undo:add_note"),
    ),
)
def test_all_compensation_adapters_execute_once_and_replay(
    tmp_path, parent_tool: str, compensation_kind: str
) -> None:
    (
        _sessions,
        _repository,
        _chat,
        _context,
        coordinator,
        conversation,
        parent_operation_id,
        _prepared_call,
        _request_fingerprint,
        _parent_calls,
        _parent_execution,
    ) = _execute_typed_parent(tmp_path, parent_tool)
    calls: list[str] = []

    def compensate(_session, undo):
        calls.append(str(undo["kind"]))
        return f"compensated:{compensation_kind}"

    first = coordinator.execute_compensation(
        parent_operation_id=parent_operation_id,
        conversation_id=conversation.id,
        compensation_kind=compensation_kind,
        executor=compensate,
    )
    replay = coordinator.execute_compensation(
        parent_operation_id=parent_operation_id,
        conversation_id=conversation.id,
        compensation_kind=compensation_kind,
        executor=compensate,
    )

    assert len(calls) == 1
    assert isinstance(first, OperationCommitted)
    assert isinstance(replay, OperationReplay)
    assert replay.payload.result_contract == "compensation_json_v1"


def test_expired_takeover_fences_late_owner_and_detects_message_and_manifest_tamper(
    tmp_path,
) -> None:
    (
        sessions,
        repository,
        _chat,
        _context,
        _coordinator,
        _conversation,
        operation_id,
        _prepared_call,
        request_fingerprint,
        _calls,
        first_execution,
    ) = _execute_typed_parent(tmp_path, "delete_note")
    original = repository.get(operation_id)
    assert original is not None
    old_owner = first_execution.ownership
    assert old_owner is not None
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_lease_expires_at = 0
        session.commit()

    takeover = repository.converge_expired_delivery(operation_id)
    assert isinstance(takeover, OperationReplay)
    assert takeover.delivery_generation == 2
    with sessions() as session:
        assert repository.complete_delivery(session, old_owner, outcome="final_response") is False
        session.rollback()

    operation = repository.get(operation_id)
    assert operation is not None
    replay = repository.replay(operation, request_fingerprint)
    assert replay.final_message == "操作已提交，但后续说明生成失败。"

    with sessions() as session, pytest.raises(
        IntegrityError, match="operation delivery message is immutable"
    ):
        message = session.query(ChatMessage).filter_by(operation_id=operation_id).first()
        assert message is not None
        message.content += "tampered"
        session.commit()

    with sessions() as session, pytest.raises(
        IntegrityError, match="write operation delivery is immutable"
    ):
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_manifest_sha256 = "sha256:" + "0" * 64
        session.commit()

    stable = repository.get(operation_id)
    assert stable is not None
    assert repository.replay(stable, request_fingerprint).final_message == replay.final_message


def test_two_connections_choose_one_primary_executor_winner(tmp_path) -> None:
    _sessions, _repository, chat, context, coordinator = _harness(tmp_path)
    conversation, operation_id, tool_call_id = _propose(chat, "delete_note")
    calls: list[str] = []
    call_lock = Lock()
    prepared = _prepared("delete_note", tool_call_id, calls)
    original_executor = prepared.spec.executor

    def synchronized_executor(args, bound_context):
        with call_lock:
            return original_executor(args, bound_context)

    prepared = replace(prepared, spec=replace(prepared.spec, executor=synchronized_executor))
    prepare_identity = _prepare_identity(prepared, operation_id)

    def approve():
        return coordinator.execute_primary(
            operation_id=operation_id,
            conversation_id=conversation.id,
            prepared=prepared,
            context=context,
            prepare_identity=prepare_identity,
            request_fingerprint="hmac-sha256:" + "6" * 64,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (pool.submit(approve), pool.submit(approve))]

    assert calls == ["delete_note"]
    assert sum(isinstance(item, OperationCommitted) for item in outcomes) == 1
    assert sum(isinstance(item, OperationReplay) for item in outcomes) == 1


def test_primary_commit_unknown_reconciles_without_second_executor_call(
    tmp_path, monkeypatch
) -> None:
    _sessions, _repository, chat, context, coordinator = _harness(tmp_path)
    conversation, operation_id, tool_call_id = _propose(chat, "delete_note")
    calls: list[str] = []
    prepared = _prepared("delete_note", tool_call_id, calls)
    prepare_identity = _prepare_identity(prepared, operation_id)
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        terminal = any(
            isinstance(item, WriteOperation) and item.status == "committed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if terminal and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)
    arguments = dict(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=prepare_identity,
        request_fingerprint="hmac-sha256:" + "7" * 64,
    )
    reconciled, _ = coordinator.execute_primary(**arguments)
    replay, _ = coordinator.execute_primary(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert isinstance(replay, OperationReplay)
    assert calls == ["delete_note"]


def test_deterministic_failure_commit_unknown_replays_failed_terminal(
    tmp_path, monkeypatch
) -> None:
    _sessions, _repository, chat, context, coordinator = _harness(tmp_path)
    conversation, operation_id, tool_call_id = _propose(chat, "delete_note")
    calls = 0
    prepared = _prepared("delete_note", tool_call_id, [])

    def fail(_args, _context):
        nonlocal calls
        calls += 1
        raise ValueError("conflict")

    prepared = replace(
        prepared,
        spec=replace(
            prepared.spec,
            executor=fail,
            declared_failure_categories=frozenset({"conflict"}),
            exception_map=(ToolExceptionMapping(ValueError, "conflict", "domain_conflict"),),
        ),
    )
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        failed_terminal = any(
            isinstance(item, WriteOperation) and item.status == "failed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if failed_terminal and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)
    arguments = dict(
        operation_id=operation_id,
        conversation_id=conversation.id,
        prepared=prepared,
        context=context,
        prepare_identity=_prepare_identity(prepared, operation_id),
        request_fingerprint="hmac-sha256:" + "9" * 64,
    )
    reconciled, reconciled_record = coordinator.execute_primary(**arguments)
    replay, replay_record = coordinator.execute_primary(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert reconciled.payload.status == "failed"
    assert reconciled.payload.failure_code == "domain_conflict"
    assert reconciled_record is None
    assert isinstance(replay, OperationReplay)
    assert replay_record is None
    assert calls == 1


def test_commit_unknown_reconciliation_distinguishes_primary_and_compensation_states(
    tmp_path, monkeypatch
) -> None:
    _sessions, repository, chat, _context, coordinator = _harness(tmp_path)
    _conversation, proposed_id, _tool_call_id = _propose(chat, "delete_note")
    fingerprint = "hmac-sha256:" + "8" * 64

    primary_proposed = coordinator._reconcile_commit_unknown(
        proposed_id,
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )
    primary_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )
    compensation_proposal_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_not_committed",
        proposed_code="operation_not_committed",
    )
    compensation_execution_absent = coordinator._reconcile_commit_unknown(
        str(uuid4()),
        fingerprint,
        absent_code="operation_result_unknown",
        proposed_code="operation_not_committed",
    )

    assert isinstance(primary_proposed, OperationUnknown)
    assert primary_proposed.code == "operation_not_committed"
    assert isinstance(primary_absent, OperationUnknown)
    assert primary_absent.code == "operation_result_unknown"
    assert isinstance(compensation_proposal_absent, OperationUnknown)
    assert compensation_proposal_absent.code == "operation_not_committed"
    assert isinstance(compensation_execution_absent, OperationUnknown)
    assert compensation_execution_absent.code == "operation_result_unknown"

    def unreadable_session():
        raise OperationalError("SELECT", {}, RuntimeError("unreadable"))

    monkeypatch.setattr(repository, "session_factory", unreadable_session)
    unreadable = coordinator._reconcile_commit_unknown(
        proposed_id,
        fingerprint,
        absent_code="operation_busy",
        proposed_code="operation_busy",
    )
    assert isinstance(unreadable, OperationUnknown)
    assert unreadable.code == "operation_result_unknown"


def test_compensation_commit_unknown_and_parent_conflict_are_stable(
    tmp_path, monkeypatch
) -> None:
    (
        _sessions,
        _repository,
        _chat,
        _context,
        coordinator,
        conversation,
        parent_operation_id,
        _prepared_call,
        _request_fingerprint,
        _parent_calls,
        _parent_execution,
    ) = _execute_typed_parent(tmp_path, "add_note")
    calls: list[str] = []
    real_commit = Session.commit
    injected = False

    def commit_then_lose_response(session):
        nonlocal injected
        terminal_compensation = any(
            isinstance(item, WriteOperation)
            and item.operation_role == "compensation"
            and item.status == "committed"
            for item in session.identity_map.values()
        )
        real_commit(session)
        if terminal_compensation and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_response)

    def compensate(_session, _undo):
        calls.append("delete_note")
        return "compensated"

    arguments = dict(
        parent_operation_id=parent_operation_id,
        conversation_id=conversation.id,
        compensation_kind="undo:add_note",
        executor=compensate,
    )
    reconciled = coordinator.execute_compensation(**arguments)
    replay = coordinator.execute_compensation(**arguments)

    assert isinstance(reconciled, OperationReplay)
    assert isinstance(replay, OperationReplay)
    assert calls == ["delete_note"]

    conflict_parent = _execute_typed_parent(tmp_path / "conflict", "add_note")
    conflict_coordinator = conflict_parent[4]
    conflict_conversation = conflict_parent[5]
    conflict_operation_id = conflict_parent[6]
    conflict_calls = 0

    def conflict(_session, _undo):
        nonlocal conflict_calls
        conflict_calls += 1
        raise ValueError("parent_conflict")

    conflict_arguments = dict(
        parent_operation_id=conflict_operation_id,
        conversation_id=conflict_conversation.id,
        compensation_kind="undo:add_note",
        executor=conflict,
    )
    failed = conflict_coordinator.execute_compensation(**conflict_arguments)
    failed_replay = conflict_coordinator.execute_compensation(**conflict_arguments)
    assert isinstance(failed, OperationFailed)
    assert failed.payload.failure_code == "parent_conflict"
    assert isinstance(failed_replay, OperationReplay)
    assert conflict_calls == 1


def test_two_expired_takeover_connections_converge_to_one_generation(tmp_path) -> None:
    (
        sessions,
        repository,
        _chat,
        _context,
        _coordinator,
        _conversation,
        operation_id,
        _prepared_call,
        _request_fingerprint,
        _calls,
        _first_execution,
    ) = _execute_typed_parent(tmp_path, "delete_note")
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        operation.delivery_lease_expires_at = 0
        session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result()
            for future in (
                pool.submit(repository.converge_expired_delivery, operation_id),
                pool.submit(repository.converge_expired_delivery, operation_id),
            )
        ]

    assert all(isinstance(item, OperationReplay) for item in outcomes)
    assert {item.delivery_generation for item in outcomes if isinstance(item, OperationReplay)} == {
        2
    }
    with sessions() as session:
        messages = session.query(ChatMessage).filter_by(operation_id=operation_id).all()
    assert [(item.delivery_kind, item.delivery_ordinal) for item in messages] == [
        ("origin_tool_result", 0),
        ("continuation_message", 1),
    ]
