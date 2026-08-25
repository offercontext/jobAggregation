from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

import offerpilot.ai.write_operations as write_operations
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.write_operations import (
    OperationReplay,
    TYPED_WRITE_OPERATION_NAMES,
    WriteOperationError,
    WriteOperationRepository,
    _typed_pending_confirmation_human,
    build_terminal_payload,
    ledger_fingerprint,
    load_or_create_ledger_key,
    operation_request_fingerprint,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, Conversation, WriteOperation
from offerpilot.pilot_runtime import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
)
from offerpilot.pilot_runtime.contracts import (
    ConfirmationRequest,
    ConfirmationRequiredOutcome,
    RuntimeFailureOutcome,
)
from offerpilot.pilot_runtime.errors import RuntimeFailureCode
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies
from offerpilot.repositories.chat import ChatRepository


_LEGACY_PENDING_HUMAN = {
    "save_application_jd_version": "请确认将这份岗位资料保存到当前投递。",
    "create_application_submission_snapshot": (
        "请确认冻结这次实际投递使用的简历、岗位资料和材料。"
    ),
    "record_application_outcome": "请确认记录这次投递进展、原始反馈和下一步行动。",
}


def _typed_pending_human(tool_name: str, args: dict[str, object]) -> str:
    """Use the frozen production metadata only as an independent test oracle."""

    spec = MODEL_TOOL_CATALOG.resolve(tool_name)
    assert spec is not None
    return str(spec.presentation.confirmation_description(spec.decoder(args)))


_ORIGIN_CONFIRMATION_TOKEN = "origin-replay-token"


def _scope_fingerprint() -> str:
    return "hmac-sha256:" + "1" * 64


def _seed_completed_origin(
    tmp_path,
    *,
    origin_adapter: str = "typed",
    origin_name: str = "update_note",
    child_adapter: str = "typed",
    child_name: str = "update_note",
    child_args: str = '{"id":1,"content":"next"}',
    child_human: str | None = None,
    outcome: str = "chained_pending",
    complete_delivery: bool = True,
    delivery_should_fail: bool = False,
):
    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("replay")
    origin_id = str(uuid4())
    child_id = str(uuid4())
    origin_args = {"id": 1, "content": "origin"}
    child_value = json.loads(child_args)
    expected_child_human = child_human
    if expected_child_human is None:
        expected_child_human = (
            _typed_pending_human(child_name, child_value)
            if child_adapter == "typed"
            else _LEGACY_PENDING_HUMAN[child_name]
        )
    child_pending = PendingAction(
        "child-call", child_name, child_args, expected_child_human, child_id
    )
    child_token = __import__(
        "offerpilot.pilot_runtime.continuation", fromlist=["_confirmation_token"]
    )._confirmation_token(child_pending)
    now = datetime.now(timezone.utc)
    owner = repository.prepare_owner(origin_id)
    payload = build_terminal_payload(
        status="committed",
        result_contract=("typed_json_v1" if origin_adapter == "typed" else "legacy_string_v1"),
        result={},
        visible_result="saved",
        transport={"tool_call_id": "origin-call", "tool_name": origin_name},
        undo=None,
        failure_category=None,
        failure_code=None,
    )
    with sessions() as session:
        repository.create_primary(
            session,
            operation_id=origin_id,
            conversation_id=conversation.id,
            tool_call_id="origin-call",
            tool_name=origin_name,
            adapter_kind=origin_adapter,  # type: ignore[arg-type]
            proposal_fingerprint=ledger_fingerprint(
                key, "write-operation-proposal-v1", origin_args
            ),
            confirmation_token_fingerprint=ledger_fingerprint(
                key,
                "write-operation-confirmation-token-v1",
                _ORIGIN_CONFIRMATION_TOKEN.encode("ascii"),
            ),
            authorization_scope_fingerprint=(
                _scope_fingerprint() if origin_adapter == "typed" else None
            ),
        )
        child = repository.create_primary(
            session,
            operation_id=child_id,
            conversation_id=conversation.id,
            tool_call_id="child-call",
            tool_name=child_name,
            adapter_kind=child_adapter,  # type: ignore[arg-type]
            proposal_fingerprint=ledger_fingerprint(
                key, "write-operation-proposal-v1", child_value
            ),
            confirmation_token_fingerprint=ledger_fingerprint(
                key,
                "write-operation-confirmation-token-v1",
                child_token.encode("ascii"),
            ),
            authorization_scope_fingerprint=(
                _scope_fingerprint() if child_adapter == "typed" else None
            ),
        )
        pending = child_pending
        conversation_row = session.get(Conversation, conversation.id)
        assert conversation_row is not None
        if outcome == "chained_pending":
            conversation_row.pending_operation_id = child.id
            conversation_row.pending_tool_call_id = child.tool_call_id or ""
            conversation_row.pending_tool_name = child.tool_name
            conversation_row.pending_args = child_args
            conversation_row.pending_human = pending.human
        origin = session.get(WriteOperation, origin_id)
        assert origin is not None
        origin.status = "committed"
        origin.operation_request_fingerprint = operation_request_fingerprint(
            key,
            operation_id=origin.id,
            tool_call_id=origin.tool_call_id or "",
            approved=True,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=(origin.confirmation_token_fingerprint or ""),
            proposal_fingerprint=origin.proposal_fingerprint or "",
        )
        origin.input_fingerprint = ledger_fingerprint(key, "test-origin-input-v1", {})
        origin.result_contract = payload.result_contract
        origin.result_json = payload.result_json
        origin.visible_result = payload.visible_result
        origin.transport_json = payload.transport_json
        origin.undo_json = payload.undo_json
        origin.terminal_payload_sha256 = payload.digest
        origin.approved_at = now
        origin.claimed_at = now
        origin.committed_at = now
        origin.delivery_generation = owner.generation
        origin.delivery_owner_token_fingerprint = owner.fingerprint
        origin.delivery_lease_expires_at = 4_102_444_800 if complete_delivery else 0
        if complete_delivery:
            session.add_all(
                [
                    ChatMessage(
                        conversation_id=conversation.id,
                        role="tool",
                        content="saved",
                        tool_call_id="origin-call",
                        operation_id=origin.id,
                        delivery_kind="origin_tool_result",
                        delivery_ordinal=0,
                    ),
                    ChatMessage(
                        conversation_id=conversation.id,
                        role="assistant",
                        content="done",
                        operation_id=origin.id,
                        delivery_kind="continuation_message",
                        delivery_ordinal=1,
                    ),
                ]
            )
            session.flush()
            if delivery_should_fail:
                with pytest.raises(WriteOperationError, match="operation_delivery_unknown"):
                    repository.complete_delivery(
                        session,
                        owner,
                        outcome=outcome,  # type: ignore[arg-type]
                        next_operation_id=(child.id if outcome == "chained_pending" else None),
                    )
                assert origin.delivery_status == "pending"
                assert origin.delivery_next_operation_id is None
            else:
                assert repository.complete_delivery(
                    session,
                    owner,
                    outcome=outcome,  # type: ignore[arg-type]
                    next_operation_id=(child.id if outcome == "chained_pending" else None),
                )
        else:
            conversation_row.pending_operation_id = origin.id
            conversation_row.pending_tool_call_id = origin.tool_call_id or ""
            conversation_row.pending_tool_name = origin.tool_name
            conversation_row.pending_args = '{"private":"must-not-be-selected"}'
            conversation_row.pending_human = "private pending"
        session.commit()
    return sessions, repository, origin_id, child_id, conversation.id


def _replay(repository: WriteOperationRepository, operation_id: str):
    operation = repository.get(operation_id)
    assert operation is not None
    return repository.replay(operation, operation.operation_request_fingerprint or "")


def test_typed_chained_replay_returns_one_verified_operation_owned_pending(tmp_path) -> None:
    _sessions, repository, origin_id, child_id, _conversation_id = _seed_completed_origin(tmp_path)
    replay = _replay(repository, origin_id)
    assert replay.chained_pending is not None
    assert replay.chained_pending.adapter_kind == "typed"
    assert replay.chained_pending.operation_id == child_id
    assert replay.chained_pending.decoded_args == {"id": 1, "content": "next"}


def test_mixed_adapter_child_is_rejected_before_delivery_commit(tmp_path) -> None:
    sessions, _repository, origin_id, child_id, conversation_id = _seed_completed_origin(
        tmp_path,
        origin_adapter="typed",
        origin_name="update_note",
        child_adapter="legacy_deterministic",
        child_name="save_application_jd_version",
        child_args='{"application_id":1,"jd_text":"next"}',
        delivery_should_fail=True,
    )

    with sessions() as session:
        origin = session.get(WriteOperation, origin_id)
        conversation = session.get(Conversation, conversation_id)
        assert origin is not None
        assert conversation is not None
        assert origin.delivery_status == "pending"
        assert origin.delivery_outcome is None
        assert origin.delivery_next_operation_id is None
        assert conversation.pending_operation_id == child_id


@pytest.mark.parametrize(
    ("tool_name", "args"),
    (
        ("create_application", {"company_name": "A", "position_name": "B"}),
        ("update_application_status", {"id": 1, "status": "offer"}),
        (
            "create_application_event",
            {
                "event_type": "interview",
                "scheduled_at": "2026-08-24T02:00:00Z",
                "duration_minutes": 30,
            },
        ),
        ("update_application_event", {"id": 2}),
        ("delete_application_event", {"id": 3}),
        ("add_note", {"company": "A", "position": "B", "round": "一面"}),
        ("update_note", {"id": 4}),
        ("delete_note", {"id": 5}),
        ("update_offer", {"id": 6}),
        ("save_offer_assessment", {"id": 7, "assessment": "ok"}),
        ("resume_update_career_intent", {"id": 8, "career_intent": {}}),
        (
            "resume_rewrite_highlight",
            {
                "id": 9,
                "section": "work_experience",
                "item_index": 0,
                "highlight_index": 0,
                "text": "rewritten",
            },
        ),
    ),
)
def test_closed_replay_renderer_matches_frozen_typed_confirmation_projection(
    tool_name: str,
    args: dict[str, object],
) -> None:
    assert set(TYPED_WRITE_OPERATION_NAMES) == {
        "create_application",
        "update_application_status",
        "create_application_event",
        "update_application_event",
        "delete_application_event",
        "add_note",
        "update_note",
        "delete_note",
        "update_offer",
        "save_offer_assessment",
        "resume_update_career_intent",
        "resume_rewrite_highlight",
    }
    assert _typed_pending_confirmation_human(tool_name, args) == _typed_pending_human(
        tool_name, args
    )


@pytest.mark.parametrize("tampered_human", (False, True))
def test_typed_chained_replay_integrity_and_runtime_side_effect_boundary(
    tmp_path,
    tampered_human: bool,
) -> None:
    sessions, repository, origin_id, child_id, conversation_id = _seed_completed_origin(tmp_path)
    if tampered_human:
        with sessions() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            conversation.pending_human = "FORGED RUNTIME CONFIRMATION TEXT"
            session.commit()
    counters = {
        "provider": 0,
        "authority": 0,
        "tool": 0,
        "executor": 0,
        "conversation": 0,
    }

    def forbidden(name: str):
        def call(*_args: object, **_kwargs: object) -> object:
            counters[name] += 1
            raise AssertionError(f"{name} must remain zero during replay")

        return call

    class ForbiddenConversationGateway:
        def load(self, *_args: object, **_kwargs: object) -> object:
            return forbidden("conversation")()

    class ForbiddenCatalog:
        def resolve(self, *_args: object, **_kwargs: object) -> object:
            return forbidden("tool")()

    class ForbiddenWriteCoordinator:
        def execute_primary(self, *_args: object, **_kwargs: object) -> object:
            return forbidden("executor")()

        def reject_primary(self, *_args: object, **_kwargs: object) -> object:
            return forbidden("executor")()

    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            write_operations=repository,
            write_coordinator=ForbiddenWriteCoordinator(),  # type: ignore[arg-type]
            catalog=ForbiddenCatalog(),
            approval_context_resolver=forbidden("authority"),
        )
    )
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=ForbiddenConversationGateway(),
            confirmation_coordinator=coordinator,
            catalog=ForbiddenCatalog(),  # type: ignore[arg-type]
            continuation_model_resolver=forbidden("provider"),  # type: ignore[arg-type]
            agent_driver=forbidden("provider"),  # type: ignore[arg-type]
        )
    )

    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=conversation_id,
            approved=True,
            operation_id=origin_id,
            confirmation_token=_ORIGIN_CONFIRMATION_TOKEN,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    if tampered_human:
        assert isinstance(outcome, RuntimeFailureOutcome)
        assert outcome.code is RuntimeFailureCode.OPERATION_INTEGRITY_ERROR
        assert outcome.status_code == 409
        assert outcome.retryable is True
    else:
        assert isinstance(outcome, ConfirmationRequiredOutcome)
        assert outcome.operation_id == origin_id
        assert outcome.pending_action.operation_id == child_id
    assert counters == {
        "provider": 0,
        "authority": 0,
        "tool": 0,
        "executor": 0,
        "conversation": 0,
    }


def test_manifest_failure_wins_before_malformed_pending_decode(tmp_path, monkeypatch) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(tmp_path)
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_args = "{"
        session.commit()
    chained_manifest = write_operations._chained_manifest

    def tampered_manifest(operation: WriteOperation | None):
        manifest = chained_manifest(operation)
        assert manifest is not None
        return {**manifest, "tool_name": "tampered-after-delivery"}

    monkeypatch.setattr(write_operations, "_chained_manifest", tampered_manifest)
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_delivery_unknown"


def test_typed_replay_decoder_failure_is_integrity(tmp_path) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(tmp_path)
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_args = '{"id":1,"id":2}'
        session.commit()
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_integrity_error"


def test_typed_chained_replay_rejects_tampered_confirmation_projection(
    tmp_path,
) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(tmp_path)
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_human = "FORGED CONFIRMATION TEXT"
        session.commit()

    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)

    assert caught.value.code == "operation_integrity_error"


def test_typed_chained_replay_rejects_valid_json_with_wrong_proposal_hmac(
    tmp_path,
) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(tmp_path)
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_args = '{"id":2,"content":"next"}'
        conversation.pending_human = _typed_pending_human(
            "update_note", {"id": 2, "content": "next"}
        )
        session.commit()

    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)

    assert caught.value.code == "operation_integrity_error"


def test_legacy_chained_replay_rejects_tampered_confirmation_projection(
    tmp_path,
) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(
        tmp_path,
        origin_adapter="legacy_deterministic",
        origin_name="save_application_jd_version",
        child_adapter="legacy_deterministic",
        child_name="save_application_jd_version",
        child_args='{"application_id":1,"jd_text":"next"}',
    )
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_human = "FORGED LEGACY CONFIRMATION TEXT"
        session.commit()

    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)

    assert caught.value.code == "operation_integrity_error"


def test_terminal_child_is_not_a_valid_chained_pending(tmp_path) -> None:
    sessions, repository, origin_id, child_id, _conversation_id = _seed_completed_origin(tmp_path)
    with sessions() as session:
        child = session.get(WriteOperation, child_id)
        assert child is not None
        payload = build_terminal_payload(
            status="rejected",
            result_contract="rejection_json_v1",
            result={"status": "cancelled"},
            visible_result="cancelled",
            transport={},
            undo=None,
            failure_category=None,
            failure_code=None,
        )
        child.status = "rejected"
        child.operation_request_fingerprint = ledger_fingerprint(
            repository.key, "test-child-request-v1", {}
        )
        child.result_contract = payload.result_contract
        child.result_json = payload.result_json
        child.visible_result = payload.visible_result
        child.transport_json = payload.transport_json
        child.terminal_payload_sha256 = payload.digest
        child.rejected_at = datetime.now(timezone.utc)
        owner = repository.prepare_owner(child.id)
        child.delivery_generation = owner.generation
        child.delivery_owner_token_fingerprint = owner.fingerprint
        child.delivery_lease_expires_at = 4_102_444_800
        session.commit()
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_delivery_unknown"


def test_final_terminal_replay_has_no_pending_projection(tmp_path) -> None:
    sessions, repository, origin_id, child_id, conversation_id = _seed_completed_origin(
        tmp_path, outcome="final_response"
    )
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        child = session.get(WriteOperation, child_id)
        assert conversation is not None
        assert child is not None
        conversation.pending_operation_id = child.id
        conversation.pending_tool_call_id = child.tool_call_id or ""
        conversation.pending_tool_name = child.tool_name
        conversation.pending_args = '{"private":"must-not-be-selected"}'
        conversation.pending_human = "private pending"
        session.commit()
        bind = session.get_bind()
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", capture_statement)
    try:
        replay = _replay(repository, origin_id)
    finally:
        event.remove(bind, "before_cursor_execute", capture_statement)

    assert replay.chained_pending is None
    selected_projections = tuple(
        statement.lower().split("from", maxsplit=1)[0]
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    )
    assert not any("conversations.pending_" in value for value in selected_projections)


def test_expired_delivery_recovery_never_selects_pending_columns(tmp_path) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(
        tmp_path,
        outcome="final_response",
        complete_delivery=False,
    )
    with sessions() as session:
        bind = session.get_bind()
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", capture_statement)
    try:
        replay = repository.converge_expired_delivery(origin_id)
    finally:
        event.remove(bind, "before_cursor_execute", capture_statement)

    assert isinstance(replay, OperationReplay)
    selected_projections = tuple(
        statement.lower().split("from", maxsplit=1)[0]
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    )
    assert not any("conversations.pending_" in value for value in selected_projections)
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.pending_operation_id == ""


@pytest.mark.parametrize(
    "child_name",
    ("create_application_submission_snapshot", "record_application_outcome"),
)
def test_only_jd_save_legacy_child_is_delivery_reachable(tmp_path, child_name: str) -> None:
    sessions, _repository, origin_id, _child_id, _conversation_id = _seed_completed_origin(
        tmp_path,
        origin_adapter="legacy_deterministic",
        origin_name=child_name,
        child_adapter="legacy_deterministic",
        child_name=child_name,
        child_args='{"application_id":1}',
        delivery_should_fail=True,
    )
    with sessions() as session:
        origin = session.get(WriteOperation, origin_id)
        assert origin is not None
        assert origin.delivery_status == "pending"
        assert origin.delivery_next_operation_id is None


def test_new_typed_proposal_uses_strict_replay_codec() -> None:
    from offerpilot.repositories import chat as chat_module

    with pytest.raises(Exception, match="canonical JSON"):
        chat_module._canonical_pending_arguments('{"id":1,"id":2}')
