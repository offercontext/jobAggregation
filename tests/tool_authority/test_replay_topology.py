from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.write_operations import (
    WriteOperationError,
    WriteOperationRepository,
    build_terminal_payload,
    ledger_fingerprint,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import ChatMessage, Conversation, WriteOperation
from offerpilot.repositories.chat import ChatRepository


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
    outcome: str = "chained_pending",
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
    child_pending = PendingAction(
        "child-call", child_name, child_args, "confirm child", child_id
    )
    child_token = __import__(
        "offerpilot.pilot_runtime.continuation", fromlist=["_confirmation_token"]
    )._confirmation_token(child_pending)
    now = datetime.now(timezone.utc)
    owner = repository.prepare_owner(origin_id)
    payload = build_terminal_payload(
        status="committed",
        result_contract=(
            "typed_json_v1" if origin_adapter == "typed" else "legacy_string_v1"
        ),
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
                key, "write-operation-confirmation-token-v1", b"origin-token"
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
        origin.operation_request_fingerprint = ledger_fingerprint(
            key, "test-origin-request-v1", {}
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
        origin.delivery_lease_expires_at = 4_102_444_800
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
        assert repository.complete_delivery(
            session,
            owner,
            outcome=outcome,  # type: ignore[arg-type]
            next_operation_id=child.id if outcome == "chained_pending" else None,
        )
        session.commit()
    return sessions, repository, origin_id, child_id, conversation.id


def _replay(repository: WriteOperationRepository, operation_id: str):
    operation = repository.get(operation_id)
    assert operation is not None
    return repository.replay(operation, operation.operation_request_fingerprint or "")


def test_typed_chained_replay_returns_one_verified_operation_owned_pending(tmp_path) -> None:
    _sessions, repository, origin_id, child_id, _conversation_id = _seed_completed_origin(
        tmp_path
    )
    replay = _replay(repository, origin_id)
    assert replay.chained_pending is not None
    assert replay.chained_pending.adapter_kind == "typed"
    assert replay.chained_pending.operation_id == child_id
    assert replay.chained_pending.decoded_args == {"id": 1, "content": "next"}


def test_manifest_failure_wins_before_malformed_pending_decode(
    tmp_path, monkeypatch
) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(
        tmp_path
    )
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_args = "{"
        session.commit()
    monkeypatch.setattr(
        "offerpilot.ai.write_operations._valid_delivery_messages",
        lambda *_args: False,
    )
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_delivery_unknown"


def test_typed_replay_decoder_failure_is_integrity(tmp_path) -> None:
    sessions, repository, origin_id, _child_id, conversation_id = _seed_completed_origin(
        tmp_path
    )
    with sessions() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.pending_args = '{"id":1,"id":2}'
        session.commit()
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_integrity_error"


def test_terminal_child_is_not_a_valid_chained_pending(tmp_path) -> None:
    sessions, repository, origin_id, child_id, _conversation_id = _seed_completed_origin(
        tmp_path
    )
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
    _sessions, repository, origin_id, _child_id, _conversation_id = _seed_completed_origin(
        tmp_path, outcome="final_response"
    )
    replay = _replay(repository, origin_id)
    assert replay.chained_pending is None


@pytest.mark.parametrize(
    "child_name",
    ("create_application_submission_snapshot", "record_application_outcome"),
)
def test_only_jd_save_legacy_child_is_replay_reachable(tmp_path, child_name: str) -> None:
    _sessions, repository, origin_id, _child_id, _conversation_id = _seed_completed_origin(
        tmp_path,
        origin_adapter="legacy_deterministic",
        origin_name=child_name,
        child_adapter="legacy_deterministic",
        child_name=child_name,
        child_args='{"application_id":1}',
    )
    with pytest.raises(WriteOperationError) as caught:
        _replay(repository, origin_id)
    assert caught.value.code == "operation_delivery_unknown"


def test_new_typed_proposal_uses_strict_replay_codec() -> None:
    from offerpilot.repositories import chat as chat_module

    with pytest.raises(Exception, match="canonical JSON"):
        chat_module._canonical_pending_arguments('{"id":1,"id":2}')
