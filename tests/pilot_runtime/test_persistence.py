from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_type_hints
from uuid import uuid4

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityPhaseError
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    OperationCommitted,
    WriteOperationRepository,
    ledger_fingerprint,
    operation_request_fingerprint,
)
from offerpilot.db import init_database
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.pilot_runtime.persistence import (
    ChatPersistenceCoordinator,
    DeliveryOutcome,
    PendingActionView,
    PendingClarificationView,
    PersistedMessageView,
    PersistedToolCallView,
    PersistenceStatus,
    _persistable_ai_messages,
)
from tests.tool_authority.test_pending_claim import _harness
from tests.tool_metadata.test_pending_routes import (
    issued_clarification_pending_route,
    issued_legacy_pending_route,
    issued_typed_pending_route,
)
from tests.test_write_operation_acceptance_matrix import (
    _legacy_confirmation_token,
    _legacy_harness,
    _legacy_route_binder,
    _propose,
)


_NON_LEDGER_PENDING_TOOL = "display_pending_notice"


def make_persistence_coordinator(tmp_path: Path) -> tuple[ChatPersistenceCoordinator, int]:
    chat = ChatRepository(init_database(tmp_path / "offerpilot.db"))
    conversation = chat.create_conversation("test")
    return ChatPersistenceCoordinator(chat), conversation.id


def make_delivery_fixtures(
    tmp_path: Path,
) -> tuple[
    ChatPersistenceCoordinator,
    int,
    ChatRepository,
    WriteOperationRepository,
    PendingAction,
    DeliveryOwnership,
]:
    sessions, operations, chat, write_coordinator, components = _legacy_harness(tmp_path)
    application = ApplicationsRepository(sessions).create(ApplicationCreate("delivery", "backend"))
    assert application.id == 1
    conversation, pending = _propose(
        chat,
        "save_application_jd_version",
        "jd_deterministic_action",
    )
    pending._test_route_components = components
    operation = operations.get(pending.operation_id)
    assert operation is not None
    confirmation_token = _legacy_confirmation_token(pending)
    request_fingerprint = operation_request_fingerprint(
        operations.key,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=ledger_fingerprint(
            operations.key,
            "write-operation-confirmation-token-v1",
            confirmation_token.encode("ascii"),
        ),
        proposal_fingerprint=operation.proposal_fingerprint,
    )
    terminal = write_coordinator.execute_legacy(
        operation_id=pending.operation_id,
        conversation_id=conversation.id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        request_fingerprint=request_fingerprint,
        route_binder=_legacy_route_binder(
            sessions=sessions,
            components=components,
            pending=pending,
        ),
    )
    assert isinstance(terminal, OperationCommitted)
    assert terminal.ownership is not None
    return (
        ChatPersistenceCoordinator(chat),
        conversation.id,
        chat,
        operations,
        pending,
        terminal.ownership,
    )


def conversation_generation(chat: ChatRepository, conversation_id: int) -> datetime:
    conversation = chat.get_conversation(conversation_id)
    assert conversation is not None
    return conversation.updated_at


def _message_projection(message: object) -> dict[str, str]:
    return {
        field: str(getattr(message, field))
        for field in ("role", "content", "tool_calls", "tool_call_id", "provider_blocks")
    }


def test_initial_pending_forwards_exact_transient_route_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    coordinator = ChatPersistenceCoordinator(harness.chat)
    captured: dict[str, object] = {}
    original = harness.chat.persist_pending_action

    def persist(
        actual_conversation_id: int,
        actual_pending: PendingAction,
        _messages: object,
        *,
        route_handle: object,
    ) -> bool:
        captured.update(
            conversation_id=actual_conversation_id,
            pending=actual_pending,
            route_handle=route_handle,
        )
        return original(
            actual_conversation_id,
            actual_pending,
            [],
            route_handle=route_handle,
        )

    monkeypatch.setattr(harness.chat, "persist_pending_action", persist)
    try:
        with issued_typed_pending_route(
            harness.pending,
            harness.conversation_id,
            claim=harness.claim,
        ) as (route_handle, _identity):
            result = coordinator.persist_initial_pending(
                harness.conversation_id,
                [],
                harness.pending,
                route_handle=route_handle,
            )

        assert result.persisted
        assert captured == {
            "conversation_id": harness.conversation_id,
            "pending": harness.pending,
            "route_handle": route_handle,
        }
        with pytest.raises((AuthorityPhaseError, TypeError, ValueError)):
            coordinator.persist_initial_pending(
                harness.conversation_id,
                [],
                harness.pending,
                route_handle=object(),
            )
    finally:
        harness.close()


def _raw_agent_messages() -> list[Message]:
    return [
        Message(
            role="assistant",
            content=("将执行 `update_application_status`；update_application_status 已准备。"),
            tool_calls=[
                ToolCall(
                    id="call-malicious-1",
                    name="update_application_status",
                    args=json.dumps(
                        {
                            "id": 7,
                            "nested": {
                                "request_id": "nested-request-secret",
                                "canary": "nested-canary-secret",
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
            provider_blocks={
                "reasoning_content": "保留的推理摘要",
                "request_id": "provider-request-secret",
                "canary": "provider-canary-secret",
                "api_key": "provider-api-key-secret",
            },
        ),
        Message(
            role="tool",
            content="工具结果",
            tool_call_id="call-malicious-1",
            provider_blocks={
                "reasoning_content": "tool 摘要",
                "request_id": "tool-request-secret",
                "canary": "tool-canary-secret",
            },
        ),
    ]


def test_initial_pending_matches_baseline_message_sanitization(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    messages = _raw_agent_messages()
    pending = PendingAction("call-malicious-1", _NON_LEDGER_PENDING_TOOL, '{"id": 7}', "更新状态")
    expected = _persistable_ai_messages(messages)

    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.persist_initial_pending(
            conversation_id,
            messages,
            pending,
            route_handle=route_handle,
        )

    assert result.persisted is True
    stored = coordinator._chat.list_messages(conversation_id)[-len(messages) :]
    assert [_message_projection(item) for item in stored] == expected
    assert "provider-request-secret" not in json.dumps(expected, ensure_ascii=False)
    assert "provider-canary-secret" not in json.dumps(expected, ensure_ascii=False)
    assert "provider-api-key-secret" not in json.dumps(expected, ensure_ascii=False)


def test_message_persistence_returns_detached_message_id(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)

    result = coordinator.persist_initial_user_message(conversation_id, "hello")

    assert result.persisted is True
    assert type(result.message_id) is int
    assert result.message_id > 0
    assert result.message_ids == (result.message_id,)


def test_clarification_replaces_stale_clarification_and_reports_assistant_id(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    stale = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"company":"旧"}', "旧复盘")
    fresh = PendingAction("new-call", _NON_LEDGER_PENDING_TOOL, '{"company":"新"}', "新复盘")
    with issued_clarification_pending_route(stale, conversation_id) as (
        stale_route,
        _identity,
    ):
        assert coordinator.set_pending_clarification(
            conversation_id,
            stale,
            "旧问题",
            route_handle=stale_route,
        ).persisted

    with issued_clarification_pending_route(fresh, conversation_id) as (
        fresh_route,
        _identity,
    ):
        result = coordinator.persist_clarification(
            conversation_id,
            [Message(role="assistant", content="需要补充")],
            fresh,
            "新问题",
            route_handle=fresh_route,
        )

    assert result.persisted is True
    assert type(result.message_id) is int
    assert coordinator._chat.get_pending_clarification(conversation_id) == (fresh, "新问题")
    assert coordinator._chat.get_pending_action(conversation_id) is None


def test_confirmation_continuation_matches_baseline_message_sanitization(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    raw_messages = _raw_agent_messages()
    origin = Message(
        role="tool",
        content=raw_messages[1].content,
        tool_call_id=pending.tool_call_id,
        provider_blocks=raw_messages[1].provider_blocks,
    )
    continuation = raw_messages[:1]
    expected = _persistable_ai_messages([origin, *continuation])

    result = coordinator.persist_confirmation_delivery(
        conversation_id,
        ownership,
        origin,
        continuation,
        route_handle=None,
        expected_generation=conversation_generation(chat, conversation_id),
        expected_pending=pending,
        claim_id=pending.operation_id,
    )

    assert result.persisted is True
    stored = chat.list_messages(conversation_id)[-len(expected) :]
    assert {field: getattr(stored[0], field) for field in ("role", "content", "tool_call_id")} == {
        field: expected[0][field] for field in ("role", "content", "tool_call_id")
    }
    assert _message_projection(stored[1]) == expected[1]
    assert "provider-request-secret" not in json.dumps(expected, ensure_ascii=False)
    assert "provider-canary-secret" not in json.dumps(expected, ensure_ascii=False)
    assert "provider-api-key-secret" not in json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize("as_json_string", [False, True])
def test_mapping_tool_args_preserve_nested_json_and_do_not_alias_input(
    tmp_path: Path,
    as_json_string: bool,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    nested_args = {
        "id": 7,
        "nested": {"labels": ["safe", {"depth": [1, 2, 3]}]},
    }
    tool_call_values = [
        {
            "id": "mapping-call",
            "name": "update_application_status",
            "args": nested_args,
        }
    ]
    tool_calls: object = (
        json.dumps(tool_call_values, ensure_ascii=False) if as_json_string else tool_call_values
    )
    mapping = {
        "role": "assistant",
        "content": "执行 `update_application_status`。",
        "tool_calls": tool_calls,
        "tool_call_id": "mapping-call",
        "provider_blocks": {
            "reasoning_content": "保留",
            "request_id": "丢弃",
            "canary": "丢弃",
        },
    }
    expected = _persistable_ai_messages(
        [
            Message(
                role="assistant",
                content="执行 `update_application_status`。",
                tool_calls=[
                    ToolCall(
                        id="mapping-call",
                        name="update_application_status",
                        args=json.dumps(nested_args, ensure_ascii=False),
                    )
                ],
                tool_call_id="mapping-call",
                provider_blocks={
                    "reasoning_content": "保留",
                    "request_id": "丢弃",
                    "canary": "丢弃",
                },
            )
        ]
    )[0]

    result = coordinator.persist_initial_messages(conversation_id, [mapping])

    assert result.persisted is True
    expected_args = json.loads(expected["tool_calls"])[0]["args"]
    nested_args["nested"]["labels"].append("mutated")
    mapping["provider_blocks"]["reasoning_content"] = "mutated"
    stored = coordinator._chat.list_messages(conversation_id)[-1]
    assert _message_projection(stored) == expected
    assert json.loads(stored.tool_calls)[0]["args"] == expected_args


def test_initial_pending_persists_atomic_tool_chain_and_pending(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": '[{"id":"call-1"}]',
            "tool_call_id": "",
            "provider_blocks": "provider",
        },
        {"role": "tool", "content": "pending", "tool_call_id": "call-1"},
    ]

    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.persist_initial_pending(
            conversation_id,
            messages,
            pending,
            route_handle=route_handle,
        )

    assert result.persisted is True
    assert coordinator._chat.get_pending_action(conversation_id) == pending
    assert [item.role for item in coordinator._chat.list_messages(conversation_id)][-2:] == [
        "assistant",
        "tool",
    ]
    assert coordinator._chat.list_messages(conversation_id)[-1].tool_call_id == "call-1"


def test_confirmation_delivery_atomically_replaces_chained_pending(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    current = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "旧卡")
    replacement = PendingAction("new-call", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "新卡")
    with issued_clarification_pending_route(current, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            current,
            route_handle=route_handle,
        )
    generation = conversation_generation(coordinator._chat, conversation_id)
    ownership = DeliveryOwnership("missing-operation", 1, b"raw", "fingerprint")

    # A bad owner must fail before it can leave a partial origin/continuation.
    with issued_clarification_pending_route(replacement, conversation_id) as (
        route_handle,
        _identity,
    ):
        with pytest.raises(TypeError, match="exact parent route"):
            coordinator.persist_confirmation_delivery(
                conversation_id,
                ownership,
                Message(role="tool", content="result", tool_call_id="old-call"),
                [Message(role="assistant", content="继续")],
                replacement,
                route_handle=route_handle,
                expected_generation=generation,
                expected_pending=current,
            )

    assert coordinator._chat.get_pending_action(conversation_id) == current
    assert coordinator._chat.list_messages(conversation_id) == []


def test_confirmation_delivery_rejects_chained_pending_without_parent_ownership(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    current = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "旧卡")
    replacement = PendingAction("new-call", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "新卡")
    with issued_clarification_pending_route(current, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            current,
            route_handle=route_handle,
        )
    generation = conversation_generation(coordinator._chat, conversation_id)

    with issued_clarification_pending_route(replacement, conversation_id) as (
        route_handle,
        _identity,
    ):
        with pytest.raises(TypeError, match="parent|ownership"):
            coordinator.persist_confirmation_delivery(
                conversation_id,
                None,
                Message(role="tool", content="result", tool_call_id="old-call"),
                [Message(role="assistant", content="继续")],
                replacement,
                route_handle=route_handle,
                expected_generation=generation,
                expected_pending=current,
            )

    assert coordinator._chat.get_pending_action(conversation_id) == current
    assert coordinator._chat.list_messages(conversation_id) == []


def test_confirmation_delivery_rejects_conflicting_pending_and_clarification_inputs(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    current = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "旧卡")
    replacement = PendingAction("new-call", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "新卡")
    clarification = (replacement, "请补充信息")
    origin = Message(role="tool", content="结果", tool_call_id=current.tool_call_id)
    continuation = [Message(role="assistant", content="继续")]

    with issued_clarification_pending_route(replacement, conversation_id) as (
        route_handle,
        _identity,
    ):
        with pytest.raises(ValueError, match="clarification.*pending"):
            coordinator.persist_confirmation_delivery(
                conversation_id,
                None,
                origin,
                continuation,
                route_handle=route_handle,
                pending=current,
                clarification=clarification,
            )

    assert coordinator._chat.list_messages(conversation_id) == []


def test_confirmation_delivery_rejects_both_pending_parameter_names_without_writes(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    current = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "旧卡")
    replacement = PendingAction("new-call", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "新卡")

    with issued_clarification_pending_route(replacement, conversation_id) as (
        route_handle,
        _identity,
    ):
        with pytest.raises(ValueError, match="pending"):
            coordinator.persist_confirmation_delivery(
                conversation_id,
                None,
                Message(role="tool", content="结果", tool_call_id=current.tool_call_id),
                [Message(role="assistant", content="继续")],
                replacement,
                route_handle=route_handle,
                pending=current,
            )

    assert coordinator._chat.list_messages(conversation_id) == []


def test_legacy_confirmation_rejects_unsupported_clarification_without_writes(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    current = PendingAction("old-call", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "旧卡")
    clarification = PendingAction("clarify-call", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "补充")
    with issued_clarification_pending_route(current, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            current,
            route_handle=route_handle,
        )

    with issued_clarification_pending_route(clarification, conversation_id) as (
        route_handle,
        _identity,
    ):
        with pytest.raises(ValueError, match="clarification"):
            coordinator.persist_confirmation_delivery(
                conversation_id,
                None,
                Message(role="tool", content="结果", tool_call_id=current.tool_call_id),
                [Message(role="assistant", content="继续")],
                route_handle=route_handle,
                expected_pending=current,
                clarification=(clarification, "请补充信息"),
            )

    assert coordinator._chat.list_messages(conversation_id) == []
    assert coordinator._chat.get_pending_action(conversation_id) == current


def test_legacy_origin_mapping_keeps_delivery_when_metadata_is_opaque(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")
    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            pending,
            route_handle=route_handle,
        )

    result = coordinator.persist_confirmation_delivery(
        conversation_id,
        None,
        {
            "role": "tool",
            "content": "结果",
            "tool_call_id": "call-1",
            "tool_calls": "legacy-opaque",
            "provider_blocks": "legacy-opaque",
        },
        [Message(role="assistant", content="已完成。")],
        route_handle=None,
        expected_pending=pending,
    )

    assert result.persisted is True
    assert [
        (item.role, item.tool_call_id) for item in coordinator._chat.list_messages(conversation_id)
    ] == [
        ("tool", "call-1"),
        ("assistant", ""),
    ]


def test_confirmation_delivery_persists_origin_and_continuation_with_ledger(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    generation = conversation_generation(chat, conversation_id)

    result = coordinator.persist_confirmation_delivery(
        conversation_id,
        ownership,
        Message(role="tool", content="已拒绝", tool_call_id=pending.tool_call_id),
        [Message(role="assistant", content="已按你的选择取消。")],
        route_handle=None,
        expected_generation=generation,
        expected_pending=pending,
        claim_id=pending.operation_id,
    )

    assert result.status is PersistenceStatus.PERSISTED
    assert result.delivery_outcome is DeliveryOutcome.FINAL_RESPONSE
    assert result.message_count == 2
    messages = chat.list_messages(conversation_id)
    assert [(item.role, item.tool_call_id) for item in messages] == [
        ("tool", pending.tool_call_id),
        ("assistant", ""),
    ]
    assert all(item.operation_id == pending.operation_id for item in messages)
    assert [item.delivery_ordinal for item in messages] == [0, 1]


def test_confirmation_delivery_atomically_chains_a_new_pending_with_ledger(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    replacement = PendingAction(
        "call-2",
        "save_application_jd_version",
        '{"application_id":2,"jd_text":"next"}',
        "保存第二份岗位资料",
        str(uuid4()),
    )
    replacement._test_route_components = pending._test_route_components
    generation = conversation_generation(chat, conversation_id)

    with issued_legacy_pending_route(
        replacement,
        conversation_id,
        source="jd_deterministic_action",
    ) as (route_handle, _identity):
        result = coordinator.persist_confirmation_delivery(
            conversation_id,
            ownership,
            Message(role="tool", content="需要重新确认", tool_call_id=pending.tool_call_id),
            [Message(role="assistant", content="请确认下一步。")],
            replacement,
            route_handle=route_handle,
            expected_generation=generation,
            expected_pending=pending,
            claim_id=pending.operation_id,
        )

    assert result.persisted is True
    assert result.delivery_outcome is DeliveryOutcome.CHAINED_PENDING
    assert chat.get_pending_action(conversation_id) == replacement
    operation = operations.get(pending.operation_id)
    assert operation is not None
    assert operation.delivery_next_operation_id == replacement.operation_id
    assert operation.delivery_outcome == "chained_pending"


def test_confirmation_clarification_is_persisted_atomically_with_delivery(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    clarification = PendingAction(
        "call-2",
        _NON_LEDGER_PENDING_TOOL,
        '{"id": 2}',
        "更新第二条状态",
    )
    generation = conversation_generation(chat, conversation_id)

    with issued_clarification_pending_route(clarification, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.persist_confirmation_continuation(
            conversation_id,
            generation,
            [Message(role="assistant", content="还需要一个信息。")],
            route_handle=route_handle,
            clarification=(clarification, "请补充第二条状态。"),
            delivery_ownership=ownership,
            expected_pending=pending,
            claim_id=pending.operation_id,
            origin_message=Message(
                role="tool", content="字段缺失", tool_call_id=pending.tool_call_id
            ),
        )

    assert result.persisted is True
    assert chat.get_pending_action(conversation_id) is None
    assert chat.get_pending_clarification(conversation_id) == (
        clarification,
        "请补充第二条状态。",
    )


def test_confirmation_delivery_cas_loss_has_no_partial_messages(tmp_path: Path) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    newer = PendingAction(
        "new-call",
        _NON_LEDGER_PENDING_TOOL,
        '{"id": 3}',
        "更新第三条状态",
        pending.operation_id,
    )

    result = coordinator.persist_confirmation_delivery(
        conversation_id,
        ownership,
        Message(role="tool", content="过期结果", tool_call_id=pending.tool_call_id),
        [Message(role="assistant", content="不应写入")],
        route_handle=None,
        expected_generation=conversation_generation(chat, conversation_id),
        expected_pending=newer,
        claim_id=pending.operation_id,
    )

    assert result.status is PersistenceStatus.CAS_LOST
    assert chat.list_messages(conversation_id) == []
    assert chat.get_pending_action(conversation_id) == pending


def test_confirmation_delivery_rollback_leaves_no_partial_messages(tmp_path: Path) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )

    with pytest.raises(Exception):
        coordinator.persist_confirmation_delivery(
            conversation_id,
            ownership,
            Message(role="tool", content="结果", tool_call_id=pending.tool_call_id),
            # A tool continuation with an empty call id violates the existing
            # delivery atom's shape check after the origin has been staged.
            [Message(role="tool", content="坏的 continuation")],
            route_handle=None,
            expected_generation=conversation_generation(chat, conversation_id),
            expected_pending=pending,
            claim_id=pending.operation_id,
        )

    assert chat.list_messages(conversation_id) == []
    assert chat.get_pending_action(conversation_id) == pending


def test_clarification_set_and_clear_are_typed(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")

    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        set_result = coordinator.set_pending_clarification(
            conversation_id,
            pending,
            "缺什么？",
            route_handle=route_handle,
        )
    assert set_result.status is PersistenceStatus.PERSISTED
    assert coordinator._chat.get_pending_clarification(conversation_id) == (pending, "缺什么？")

    clear_result = coordinator.clear_pending_clarification(conversation_id)
    assert clear_result.status is PersistenceStatus.PERSISTED
    assert coordinator._chat.get_pending_clarification(conversation_id) is None


def test_clarification_set_ignores_ledger_operation_id_not_stored_by_chat_atom(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction(
        "call-1",
        _NON_LEDGER_PENDING_TOOL,
        '{"id": 1}',
        "更新状态",
        str(uuid4()),
    )

    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.set_pending_clarification(
            conversation_id,
            pending,
            "缺什么？",
            route_handle=route_handle,
        )

    assert result.status is PersistenceStatus.PERSISTED
    stored = coordinator._chat.get_pending_clarification(conversation_id)
    assert stored is not None
    assert stored[0].operation_id == ""
    assert stored[0].tool_call_id == pending.tool_call_id


def test_archived_initial_pending_is_closed_without_messages(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    coordinator._chat.update_conversation_for_archive(
        conversation_id, {"archived_at": datetime.now(timezone.utc)}
    )
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")

    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.persist_initial_pending(
            conversation_id,
            [],
            pending,
            route_handle=route_handle,
        )

    assert result.status is PersistenceStatus.CLOSED
    assert result.persisted is False
    assert coordinator._chat.list_messages(conversation_id) == []


def test_archived_confirmation_delivery_is_closed_without_calling_atom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    coordinator._chat.update_conversation_for_archive(
        conversation_id, {"archived_at": datetime.now(timezone.utc)}
    )
    owner = DeliveryOwnership("operation-1", 1, b"raw", "fingerprint")
    calls = 0

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("archived delivery must not call the atom")

    monkeypatch.setattr(coordinator._chat, "persist_confirmation_continuation", fail_if_called)
    result = coordinator.persist_confirmation_delivery(
        conversation_id,
        owner,
        Message(role="tool", content="结果", tool_call_id="call-1"),
        [Message(role="assistant", content="不应写入")],
        route_handle=None,
    )

    assert result.status is PersistenceStatus.CLOSED
    assert calls == 0
    assert coordinator._chat.list_messages(conversation_id) == []


def test_pending_cas_loss_is_typed_and_non_mutating(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    first = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "第一张")
    second = PendingAction("call-2", _NON_LEDGER_PENDING_TOOL, '{"id": 2}', "第二张")
    with issued_clarification_pending_route(first, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            first,
            route_handle=route_handle,
        )

    with issued_clarification_pending_route(second, conversation_id) as (
        route_handle,
        _identity,
    ):
        result = coordinator.persist_initial_pending(
            conversation_id,
            [],
            second,
            route_handle=route_handle,
        )

    assert result.status is PersistenceStatus.CAS_LOST
    assert coordinator._chat.get_pending_action(conversation_id) == first
    assert coordinator._chat.list_messages(conversation_id) == []


def test_confirmation_fallback_and_replay_have_typed_delivery_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")
    with issued_clarification_pending_route(pending, conversation_id) as (
        route_handle,
        _identity,
    ):
        assert coordinator._chat.set_pending_action(
            conversation_id,
            pending,
            route_handle=route_handle,
        )
    owner = DeliveryOwnership("operation-1", 1, b"raw", "fingerprint")
    generation = conversation_generation(coordinator._chat, conversation_id)
    captured: list[dict[str, object]] = []

    def fake_continuation(*args: object, **kwargs: object) -> datetime:
        captured.append({"args": args, "kwargs": kwargs})
        return generation

    monkeypatch.setattr(coordinator._chat, "persist_confirmation_continuation", fake_continuation)

    fallback = coordinator.persist_confirmation_fallback(
        conversation_id,
        owner,
        Message(role="tool", content="result", tool_call_id="call-1"),
        "后续说明生成失败。",
        route_handle=None,
        expected_generation=generation,
        expected_pending=pending,
    )
    replay = coordinator.persist_replay_delivery(
        conversation_id,
        owner,
        Message(role="tool", content="result", tool_call_id="call-1"),
        [Message(role="assistant", content="重放结果")],
        route_handle=None,
        expected_generation=generation,
        expected_pending=pending,
    )

    assert fallback.persisted is True
    assert fallback.delivery_outcome is DeliveryOutcome.FALLBACK
    assert replay.persisted is True
    assert replay.delivery_outcome is DeliveryOutcome.FINAL_RESPONSE
    assert len(captured) == 2
    assert captured[0]["kwargs"]["delivery_failure_code"] == "operation_delivery_failed"  # type: ignore[index]


def test_confirmation_fallback_marks_ledger_delivery_failed(tmp_path: Path) -> None:
    coordinator, conversation_id, chat, operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )

    result = coordinator.persist_confirmation_fallback(
        conversation_id,
        ownership,
        Message(role="tool", content="工具已完成", tool_call_id=pending.tool_call_id),
        "后续说明生成失败。",
        route_handle=None,
        expected_generation=conversation_generation(chat, conversation_id),
        expected_pending=pending,
        claim_id=pending.operation_id,
    )

    assert result.persisted is True
    assert result.delivery_outcome is DeliveryOutcome.FALLBACK
    operation = operations.get(pending.operation_id)
    assert operation is not None
    assert operation.delivery_status == "failed"
    assert operation.delivery_failure_code == "operation_delivery_failed"


def test_duplicate_chained_delivery_without_pending_has_no_inferred_outcome(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    replacement = PendingAction(
        "call-2",
        "save_application_jd_version",
        '{"application_id":2,"jd_text":"next"}',
        "保存第二份岗位资料",
        str(uuid4()),
    )
    replacement._test_route_components = pending._test_route_components
    origin = Message(role="tool", content="结果", tool_call_id=pending.tool_call_id)
    continuation = [Message(role="assistant", content="请确认下一步。")]
    with issued_legacy_pending_route(
        replacement,
        conversation_id,
        source="jd_deterministic_action",
    ) as (route_handle, _identity):
        first = coordinator.persist_confirmation_delivery(
            conversation_id,
            ownership,
            origin,
            continuation,
            route_handle=route_handle,
            chained_pending=replacement,
            expected_generation=conversation_generation(chat, conversation_id),
            expected_pending=pending,
            claim_id=pending.operation_id,
        )
    before = [
        (item.id, item.role, item.content, item.operation_id, item.delivery_ordinal)
        for item in chat.list_messages(conversation_id)
    ]

    replay = coordinator.persist_replay_delivery(
        conversation_id,
        ownership,
        origin,
        continuation,
        route_handle=None,
        expected_generation=first.generation,
        claim_id=pending.operation_id,
    )

    assert replay.status is PersistenceStatus.DUPLICATE
    assert replay.delivery_outcome is None
    assert chat.get_pending_action(conversation_id) == replacement
    after = [
        (item.id, item.role, item.content, item.operation_id, item.delivery_ordinal)
        for item in chat.list_messages(conversation_id)
    ]
    assert after == before


def test_replay_delivery_returns_duplicate_after_delivery_without_new_messages(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id, chat, _operations, pending, ownership = make_delivery_fixtures(
        tmp_path
    )
    origin = Message(role="tool", content="结果", tool_call_id=pending.tool_call_id)
    continuation = [Message(role="assistant", content="最终结果")]
    first = coordinator.persist_confirmation_delivery(
        conversation_id,
        ownership,
        origin,
        continuation,
        route_handle=None,
        expected_generation=conversation_generation(chat, conversation_id),
        expected_pending=pending,
        claim_id=pending.operation_id,
    )
    before = chat.list_messages(conversation_id)

    replay = coordinator.persist_replay_delivery(
        conversation_id,
        ownership,
        origin,
        continuation,
        route_handle=None,
        expected_generation=first.generation,
        expected_pending=pending,
        claim_id=pending.operation_id,
    )

    assert first.persisted is True
    assert replay.status is PersistenceStatus.DUPLICATE
    assert replay.delivery_outcome is None
    after = chat.list_messages(conversation_id)
    assert [(item.id, item.role, item.content, item.operation_id) for item in after] == [
        (item.id, item.role, item.content, item.operation_id) for item in before
    ]


def test_duplicate_delivery_is_idempotent_and_does_not_call_atom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    owner = DeliveryOwnership("operation-1", 1, b"raw", "fingerprint")
    calls = 0

    class ExistingMessage:
        operation_id = "operation-1"

    monkeypatch.setattr(
        coordinator._chat,
        "list_messages",
        lambda _conversation_id: [ExistingMessage()],
    )

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("duplicate delivery must not call the atom")

    monkeypatch.setattr(coordinator._chat, "persist_confirmation_continuation", fail_if_called)
    result = coordinator.persist_replay_delivery(
        conversation_id,
        owner,
        Message(role="tool", content="result", tool_call_id="call-1"),
        [Message(role="assistant", content="重放结果")],
        route_handle=None,
    )

    assert result.status is PersistenceStatus.DUPLICATE
    assert result.persisted is False
    assert calls == 0


def test_direct_assistant_and_tool_messages_use_baseline_sanitization(
    tmp_path: Path,
) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    args = {"id": 7, "nested": {"items": [1, {"safe": True}]}}
    tool_calls = json.dumps(
        [{"id": "call-1", "name": "update_application_status", "args": args}],
        ensure_ascii=False,
    )
    provider_blocks = json.dumps(
        {
            "reasoning_content": "保留",
            "request_id": "provider-request-secret",
            "canary": "provider-canary-secret",
        },
        ensure_ascii=False,
    )

    assistant = coordinator.persist_assistant_message(
        conversation_id,
        "将调用 `update_application_status`。",
        tool_calls=tool_calls,
        tool_call_id="",
        provider_blocks=provider_blocks,
    )
    tool = coordinator.persist_message(
        conversation_id,
        "tool",
        "工具结果",
        tool_calls=tool_calls,
        tool_call_id="call-1",
        provider_blocks=provider_blocks,
    )

    assert assistant.persisted is True
    assert tool.persisted is True
    stored = coordinator._chat.list_messages(conversation_id)[-2:]
    expected = _persistable_ai_messages(
        [
            Message(
                role="assistant",
                content="将调用 `update_application_status`。",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="update_application_status",
                        args=json.dumps(args, ensure_ascii=False),
                    )
                ],
                provider_blocks={
                    "reasoning_content": "保留",
                    "request_id": "provider-request-secret",
                    "canary": "provider-canary-secret",
                },
            ),
            Message(
                role="tool",
                content="工具结果",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="update_application_status",
                        args=json.dumps(args, ensure_ascii=False),
                    )
                ],
                tool_call_id="call-1",
                provider_blocks={
                    "reasoning_content": "保留",
                    "request_id": "provider-request-secret",
                    "canary": "provider-canary-secret",
                },
            ),
        ]
    )
    assert [_message_projection(item) for item in stored] == expected
    assert "provider-request-secret" not in json.dumps(stored, default=str)
    assert "provider-canary-secret" not in json.dumps(stored, default=str)


def test_repository_is_private_to_persistence_coordinator(tmp_path: Path) -> None:
    coordinator, _conversation_id = make_persistence_coordinator(tmp_path)

    assert not hasattr(coordinator, "chat")
    assert hasattr(coordinator, "_chat")


def test_public_message_reads_are_frozen_detached_snapshots(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    tool_args = {
        "id": 7,
        "nested": {"items": [1, {"safe": True}]},
    }
    provider_blocks = {
        "reasoning_content": {"summary": "保留", "items": ["a", "b"]},
        "request_id": "provider-request-secret",
        "canary": "provider-canary-secret",
    }
    result = coordinator.persist_message(
        conversation_id,
        "assistant",
        "将调用 `update_application_status`。",
        tool_calls=json.dumps(
            [{"id": "call-1", "name": "update_application_status", "args": tool_args}],
            ensure_ascii=False,
        ),
        provider_blocks=json.dumps(provider_blocks, ensure_ascii=False),
    )

    assert result.persisted is True
    views = coordinator.list_messages(conversation_id)

    assert isinstance(views, tuple)
    assert len(views) == 1
    view = views[0]
    assert isinstance(view, PersistedMessageView)
    assert isinstance(view.tool_calls, tuple)
    assert isinstance(view.tool_calls[0], PersistedToolCallView)
    assert isinstance(view.tool_calls[0].args, MappingProxyType)
    assert view.tool_calls[0].args["nested"]["items"] == (1, {"safe": True})
    assert isinstance(view.provider_blocks, MappingProxyType)
    assert view.provider_blocks["reasoning_content"]["items"] == ("a", "b")
    assert "request_id" not in view.provider_blocks
    assert "canary" not in view.provider_blocks
    assert "update_application_status" not in view.content
    assert view.content == "将调用 更新投递状态。"

    with pytest.raises(FrozenInstanceError):
        view.content = "changed"
    with pytest.raises(TypeError):
        view.provider_blocks["new"] = "value"
    with pytest.raises(TypeError):
        view.tool_calls[0].args["nested"]["new"] = "value"

    backing = coordinator._chat.list_messages(conversation_id)[0]
    backing.content = "mutated backing content"
    backing.provider_blocks = json.dumps(
        {"reasoning_content": {"items": ["mutated"]}}, ensure_ascii=False
    )
    backing.tool_calls = json.dumps(
        [{"id": "call-1", "name": "other", "args": {"changed": True}}],
        ensure_ascii=False,
    )
    assert view.content == "将调用 更新投递状态。"
    assert view.provider_blocks["reasoning_content"]["items"] == ("a", "b")
    assert view.tool_calls[0].name == "update_application_status"


def test_public_pending_reads_are_frozen_snapshots(tmp_path: Path) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 7}', "更新状态")
    with issued_clarification_pending_route(pending, conversation_id) as (
        pending_route,
        _identity,
    ):
        assert (
            coordinator._chat.set_pending_action(
                conversation_id,
                pending,
                route_handle=pending_route,
            )
            is True
        )
    with issued_clarification_pending_route(pending, conversation_id) as (
        clarification_route,
        _identity,
    ):
        assert (
            coordinator._chat.set_pending_clarification(
                conversation_id,
                pending,
                "还缺什么？",
                route_handle=clarification_route,
            )
            is None
        )

    pending_view = coordinator.get_pending_action(conversation_id)
    clarification_view = coordinator.get_pending_clarification(conversation_id)

    assert isinstance(pending_view, PendingActionView)
    assert pending_view is not pending
    assert pending_view.args == '{"id": 7}'
    with pytest.raises(FrozenInstanceError):
        pending_view.tool_name = "changed"

    assert isinstance(clarification_view, PendingClarificationView)
    assert clarification_view.pending.tool_call_id == "call-1"
    assert clarification_view.question == "还缺什么？"
    with pytest.raises(FrozenInstanceError):
        clarification_view.question = "changed"
    with pytest.raises(FrozenInstanceError):
        clarification_view.pending.args = "changed"

    backing_pending = coordinator._chat.get_pending_action(conversation_id)
    assert backing_pending is not None
    backing_pending.tool_name = "mutated backing tool"
    backing_clarification = coordinator._chat.get_pending_clarification(conversation_id)
    assert backing_clarification is not None
    backing_clarification[0].args = "mutated backing args"
    assert pending_view.tool_name == _NON_LEDGER_PENDING_TOOL
    assert clarification_view.pending.args == '{"id": 7}'


def test_public_read_annotations_are_closed_snapshot_types() -> None:
    for method_name in (
        "list_messages",
        "get_pending_action",
        "get_pending_clarification",
    ):
        return_annotation = get_type_hints(getattr(ChatPersistenceCoordinator, method_name))[
            "return"
        ]
        rendered = str(return_annotation)
        assert return_annotation is not Any
        assert "Any" not in rendered
        assert "offerpilot.ai.agent.PendingAction" not in rendered
        assert "sqlalchemy" not in rendered

    assert PersistedMessageView.__slots__
    assert PersistedToolCallView.__slots__
    assert PendingActionView.__slots__
    assert PendingClarificationView.__slots__


@pytest.mark.parametrize("method", ["persist_timeout_assistant", "persist_initial_user_message"])
def test_message_helpers_preserve_identity_fields(tmp_path: Path, method: str) -> None:
    coordinator, conversation_id = make_persistence_coordinator(tmp_path)
    if method == "persist_initial_user_message":
        result = coordinator.persist_initial_user_message(conversation_id, "用户消息")
    else:
        pending = PendingAction("call-1", _NON_LEDGER_PENDING_TOOL, '{"id": 1}', "更新状态")
        with issued_clarification_pending_route(pending, conversation_id) as (
            route_handle,
            _identity,
        ):
            coordinator._chat.set_pending_clarification(
                conversation_id,
                pending,
                "缺什么？",
                route_handle=route_handle,
            )
        result = coordinator.persist_timeout_assistant(conversation_id, "超时，请重试。")
    assert result.persisted is True
    assert coordinator._chat.list_messages(conversation_id)[-1].content
    if method == "persist_timeout_assistant":
        assert coordinator._chat.get_pending_clarification(conversation_id) is None
