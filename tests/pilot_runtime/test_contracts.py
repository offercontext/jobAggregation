from __future__ import annotations

import pickle
import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import StrEnum
from threading import Barrier, Thread
from types import MappingProxyType
from typing import Literal, TypeVar, get_args, get_origin, get_type_hints
from uuid import uuid4
from pathlib import Path

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CancelReason,
    CompletedEvent,
    CompletionReason,
    ConfirmationRequest,
    ConfirmationRequiredEvent,
    ConfirmationRequiredOutcome,
    EditedArgs,
    ErrorEvent,
    freeze_json_mapping,
    FirstModelCompletedSignal,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    MetaEvent,
    OperationPendingOutcome,
    OperationReplayOutcome,
    PendingActionPayload,
    PreparationKind,
    PreparedLifecycle,
    PreparedLifecycleState,
    PreparedStreamExecution,
    RuntimeFailureOutcome,
    RuntimeFailureCode,
    AgentExecutionHost,
    RuntimeSignalSink,
    RuntimeTransportContext,
    SignalEmitResult,
    StartTurnRequest,
    StatusEvent,
    StreamExecutionMode,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageSavedEvent,
)
from offerpilot.pilot_runtime.errors import RuntimeCancelled, RuntimeTransportAborted


def test_prepared_lifecycle_accepts_only_reviewed_transitions() -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.begin() is True
    assert lifecycle.complete(CompletionReason.NORMAL) is True
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.NORMAL
    assert lifecycle.complete(CompletionReason.CANCELLED) is False
    assert lifecycle.abort_if_prepared() is False


def test_before_start_abort_has_no_completion_reason() -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.abort_if_prepared() is True
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert lifecycle.completion_reason is None
    assert lifecycle.begin() is False


@pytest.mark.parametrize(
    "reason",
    [CompletionReason.NORMAL, CompletionReason.CANCELLED, CompletionReason.TRANSPORT_ABORTED],
)
def test_completion_cleanup_winner_is_unique(reason: CompletionReason) -> None:
    lifecycle = PreparedLifecycle()
    assert lifecycle.begin() is True
    assert lifecycle.complete(reason) is True
    assert lifecycle.complete(reason) is False


def test_lifecycle_completion_reason_and_state_are_validated_without_side_effects() -> None:
    with pytest.raises(ValueError):
        PreparedStreamExecution(
            invocation_id=uuid4(),
            preparation_kind=PreparationKind.MODEL,
            execution_mode=StreamExecutionMode.DIRECT,
            opaque_state=object(),
            lifecycle_state=PreparedLifecycleState.COMPLETED,
            completion_reason=None,
        )
    with pytest.raises(ValueError):
        PreparedStreamExecution(
            invocation_id=uuid4(),
            preparation_kind=PreparationKind.MODEL,
            execution_mode=StreamExecutionMode.DIRECT,
            opaque_state=object(),
            lifecycle_state=PreparedLifecycleState.PREPARED,
            completion_reason=CompletionReason.NORMAL,
        )


def test_lifecycle_cas_race_has_one_winner_and_no_illegal_state() -> None:
    lifecycle = PreparedLifecycle()
    barrier = Barrier(2)
    results: list[tuple[str, bool]] = []

    def begin() -> None:
        barrier.wait()
        results.append(("begin", lifecycle.begin()))

    def abort() -> None:
        barrier.wait()
        results.append(("abort", lifecycle.abort_if_prepared()))

    first = Thread(target=begin)
    second = Thread(target=abort)
    first.start()
    second.start()
    first.join()
    second.join()
    assert sum(result for _, result in results) == 1
    if lifecycle.state is PreparedLifecycleState.EXECUTING:
        assert lifecycle.complete(CompletionReason.NORMAL) is True
    else:
        assert lifecycle.state is PreparedLifecycleState.ABORTED
        assert lifecycle.completion_reason is None


def test_lifecycle_identity_hash_and_set_membership_survive_transitions() -> None:
    lifecycle = PreparedLifecycle()
    original_hash = hash(lifecycle)
    members = {lifecycle}
    assert lifecycle in members
    assert PreparedLifecycle() not in members
    assert lifecycle.begin() is True
    assert lifecycle.complete(CompletionReason.NORMAL) is True
    assert hash(lifecycle) == original_hash
    assert lifecycle in members


def test_prepared_stream_execution_uses_identity_semantics() -> None:
    first = PreparedStreamExecution(
        invocation_id="run-1",
        preparation_kind=PreparationKind.MODEL,
        execution_mode=StreamExecutionMode.AGENT_HOST,
        opaque_state=MappingProxyType({"secret": "one"}),
    )
    second = PreparedStreamExecution(
        invocation_id="run-1",
        preparation_kind=PreparationKind.MODEL,
        execution_mode=StreamExecutionMode.AGENT_HOST,
        opaque_state=MappingProxyType({"secret": "one"}),
    )
    members = {first}
    assert first != second
    assert first in members
    assert first.begin() is True
    assert first.complete(CompletionReason.NORMAL) is True
    assert first in members


def test_all_contracts_are_closed_frozen_slot_dataclasses() -> None:
    values = [
        StartTurnRequest(message="hello"),
        ConfirmationRequest(conversation_id=1, approved=True, confirmation_token="token"),
        RuntimeTransportContext(mode="sync"),
        ImmediateHttpOutcome(status_code=200, payload=MappingProxyType({"ok": True})),
        MessageOutcome(message="done"),
        ConfirmationRequiredOutcome(conversation_id=1, confirmation_token="token"),
        RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR, message="暂时不可用"),
        OperationPendingOutcome(operation_id="op-1"),
        OperationReplayOutcome(operation_id="op-1"),
        PreparedLifecycle(),
        MetaEvent(stream_version="pilot-sse-v1"),
        UserMessageSavedEvent(),
        StatusEvent(phase="model_running", label="正在思考"),
        AssistantDeltaEvent(delta="hi"),
        ToolCallEvent(tool_call_id="call-1", tool_name="lookup"),
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            status="success",
            summary="done",
        ),
        ConfirmationRequiredEvent(confirmation_token="token"),
        AssistantMessageEvent(message="done"),
        ErrorEvent(code=RuntimeFailureCode.AI_PROVIDER_ERROR, message="暂时不可用"),
        CompletedEvent(),
        FirstModelCompletedSignal(),
    ]
    for value in values:
        assert is_dataclass(value)
        assert getattr(type(value), "__slots__", None)
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, object())


def test_user_event_and_signal_are_closed_types() -> None:
    event = UserMessageSavedEvent()
    assert event.role == "user"
    with pytest.raises(ValueError):
        UserMessageSavedEvent(role="assistant")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StatusEvent(payload={"phase": "model_running"})  # type: ignore[call-arg]
    assert FirstModelCompletedSignal().title_eligible is True
    assert get_type_hints(FirstModelCompletedSignal)["title_eligible"] == Literal[True]


def test_request_and_event_reject_framework_objects_and_mutable_mappings() -> None:
    from fastapi import Request
    from starlette.background import BackgroundTasks

    with pytest.raises(TypeError):
        StartTurnRequest(message="hello", page_context={"view": "pilot"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StartTurnRequest(message="hello", page_context=Request)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RuntimeTransportContext(mode=BackgroundTasks())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ImmediateHttpOutcome(status_code=200, payload={"ok": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolResultEvent(tool_call_id="call-1", status="completed", payload={"x": 1})  # type: ignore[arg-type]


def test_freeze_json_mapping_snapshots_mutable_route_values() -> None:
    backing_list = ["before"]
    backing = {"nested": {"items": backing_list}, "tuple": ("before",)}
    frozen = freeze_json_mapping(backing)
    backing_list.append("after")
    backing["nested"]["items"] = ["changed"]
    backing["tuple"] = ("changed",)
    assert frozen["nested"]["items"] == ("before",)
    assert frozen["tuple"] == ("before",)


def test_json_fields_snapshot_backing_mapping_aliases() -> None:
    args_backing = {"value": "before"}
    args = (MappingProxyType(args_backing),)
    event = ToolCallEvent(
        tool_call_id="call-1",
        tool_name="lookup",
        args_summary=args,
    )
    args_backing["value"] = "after"
    assert event.args_summary == (MappingProxyType({"value": "before"}),)

    opaque_backing = {"value": "before"}
    prepared = PreparedStreamExecution(
        invocation_id="run-1",
        preparation_kind=PreparationKind.MODEL,
        execution_mode=StreamExecutionMode.AGENT_HOST,
        opaque_state=MappingProxyType(opaque_backing),
    )
    opaque_backing["value"] = "after"
    assert prepared.opaque_state == MappingProxyType({"value": "before"})


def test_confirmation_edited_args_distinguish_missing_empty_and_nonempty() -> None:
    missing = ConfirmationRequest(conversation_id=1, approved=True, confirmation_token="token")
    empty = ConfirmationRequest(
        conversation_id=1,
        approved=True,
        confirmation_token="token",
        edited_args=MappingProxyType({}),
    )
    nonempty = ConfirmationRequest(
        conversation_id=1,
        approved=True,
        confirmation_token="token",
        edited_args=MappingProxyType({"title": "new"}),
    )
    assert missing.edited_args is not empty.edited_args
    assert missing.edited_args.is_missing() is True
    assert empty.edited_args.is_empty() is True
    assert nonempty.edited_args.is_empty() is False
    with pytest.raises(TypeError):
        ConfirmationRequest(
            conversation_id=1,
            approved=True,
            confirmation_token="token",
            edited_args={},
        )
    with pytest.raises(ValueError):
        ConfirmationRequest(
            conversation_id=1,
            approved=True,
            confirmation_token="token",
            edited_args=None,
        )


def test_direct_edited_args_reject_mutable_aliases() -> None:
    mutable = {"title": "old"}
    with pytest.raises(TypeError):
        EditedArgs(mutable)  # type: ignore[arg-type]

    source = {"title": "old"}
    edited = EditedArgs(MappingProxyType(source))
    source["title"] = "mutated"
    assert edited["title"] == "old"


def test_signal_sink_protocol_has_closed_nonblocking_result() -> None:
    assert issubclass(SignalEmitResult, StrEnum)
    assert {member.value for member in SignalEmitResult} == {
        "emitted",
        "duplicate",
        "closed",
        "full",
        "degraded",
    }
    assert get_origin(RuntimeSignalSink) is None or get_args(RuntimeSignalSink)
    assert {member.value for member in InvocationState} >= {
        "active",
        "completed",
        "timed_out",
        "cancelled",
    }
    assert {member.value for member in CancelReason} >= {
        "client_disconnect",
        "explicit_cancel",
        "deadline",
    }


def _json_object(value: dict[str, object]) -> MappingProxyType:
    return MappingProxyType(value)


def test_baseline_nested_tool_payloads_are_closed_and_lossless() -> None:
    nested_args = _json_object(
        {
            "filters": _json_object({"status": "open", "ids": (1, 2)}),
            "locations": ("remote", "hybrid"),
        }
    )
    tool_call = ToolCallEvent(
        tool_call_id="call-1",
        tool_name="search_applications",
        args_summary=nested_args,
    )
    assert tool_call.args_summary == nested_args
    evidence = (_json_object({"id": "application-1", "kind": "application"}),)
    result = ToolResultEvent(
        tool_call_id="call-1",
        tool_name="search_applications",
        status="success",
        summary="找到 1 条投递记录",
        evidence=evidence,
        affected_resources=evidence,
        changed_entities=(),
    )
    assert result.evidence == evidence
    assert result.affected_resources == evidence
    with pytest.raises(TypeError):
        ToolCallEvent(
            tool_call_id="call-1",
            tool_name="search_applications",
            args_summary={"filters": {"ids": [1, 2]}},  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ToolCallEvent(
            tool_call_id="call-1",
            tool_name="search_applications",
            args_summary=_json_object({"filters": ["open"]}),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="search_applications",
            summary="bad",
            evidence=([{"id": "application-1"}],),  # type: ignore[arg-type]
        )


def test_confirmation_payload_preserves_complete_pending_action_shape() -> None:
    pending = PendingActionPayload(
        tool_name="create_application",
        operation_id="op-1",
        human="创建投递记录",
        args=_json_object(
            {
                "company_name": "OfferPilot",
                "status": "applied",
                "nested": _json_object({"ids": (1, 2)}),
            }
        ),
        confirmation_token="a" * 64,
        editable_fields=(_json_object({"field": "status", "label": "状态"}),),
        details=_json_object(
            {
                "target": _json_object(
                    {"id": "application-draft-1", "kind": "application"}
                ),
                "proposed_changes": (
                    _json_object({"field": "status", "before": "", "after": "applied"}),
                ),
                "evidence": (),
            }
        ),
    )
    outcome = ConfirmationRequiredOutcome(
        conversation_id=1,
        confirmation_token=pending.confirmation_token,
        pending_action=pending,
    )
    event = ConfirmationRequiredEvent(
        confirmation_token=pending.confirmation_token,
        pending_action=pending,
    )
    assert outcome.pending_action == pending
    assert event.pending_action.details["target"] == pending.details["target"]
    with pytest.raises(TypeError):
        PendingActionPayload(
            tool_name="create_application",
            operation_id="op-1",
            human="创建投递记录",
            args={"company_name": "OfferPilot"},  # type: ignore[arg-type]
            confirmation_token="a" * 64,
        )


def test_pending_action_details_cannot_shadow_canonical_fields() -> None:
    with pytest.raises(ValueError):
        PendingActionPayload(
            tool_name="create_application",
            operation_id="op-1",
            human="创建投递记录",
            args=_json_object({"company_name": "OfferPilot"}),
            confirmation_token="a" * 64,
            details=_json_object({"tool_name": "spoofed"}),
        )


def test_confirmation_required_outcome_marks_chained_replay() -> None:
    replay = ConfirmationRequiredOutcome(
        confirmation_token="a" * 64,
        operation_id="op-1",
        replayed=True,
    )
    assert replay.replayed is True
    with pytest.raises(TypeError):
        ConfirmationRequiredOutcome(
            confirmation_token="a" * 64,
            replayed="true",  # type: ignore[arg-type]
        )


def test_message_replay_pending_and_failure_outcomes_cover_baseline_body_fields() -> None:
    undo = _json_object(
        {"kind": "delete_application", "application_id": 1, "parent_operation_id": "op-1"}
    )
    message = MessageOutcome(
        message="已保存",
        conversation_id=1,
        write_status="success",
        write_error="目标记录不存在",
        undo=undo,
        operation_id="op-1",
        replayed=False,
    )
    replay = OperationReplayOutcome(
        operation_id="op-1",
        conversation_id=1,
        message="操作已完成。",
        status="committed",
        write_status="success",
        write_error="operation_failed",
        undo=undo,
        replayed=True,
    )
    pending = OperationPendingOutcome(
        operation_id="op-2",
        conversation_id=1,
        message="确认操作仍在后台执行",
        code=RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
    )
    failure = RuntimeFailureOutcome(
        code=RuntimeFailureCode.AI_PROVIDER_ERROR,
        message="AI 连接失败",
        retryable=True,
    )
    assert message.undo == undo
    assert message.write_error == "目标记录不存在"
    assert replay.write_error == "operation_failed"
    assert replay.replayed is True
    assert pending.code is RuntimeFailureCode.OPERATION_DELIVERY_PENDING
    assert failure.code is RuntimeFailureCode.AI_PROVIDER_ERROR


@pytest.mark.parametrize(
    ("kind", "mode", "valid"),
    [
        (PreparationKind.MODEL, StreamExecutionMode.AGENT_HOST, True),
        (PreparationKind.MODEL, StreamExecutionMode.DIRECT, False),
        (PreparationKind.DETERMINISTIC_INITIAL, StreamExecutionMode.DIRECT, True),
        (PreparationKind.DETERMINISTIC_INITIAL, StreamExecutionMode.AGENT_HOST, False),
        (PreparationKind.DETERMINISTIC_CONFIRMATION, StreamExecutionMode.DIRECT, True),
        (PreparationKind.DETERMINISTIC_CONFIRMATION, StreamExecutionMode.AGENT_HOST, False),
        # Ordinary reject uses direct delivery and must not call a provider.
        (PreparationKind.CONFIRMATION, StreamExecutionMode.DIRECT, True),
        # Approve/modify continuation is hosted by the agent runtime.
        (PreparationKind.CONFIRMATION, StreamExecutionMode.AGENT_HOST, True),
        (PreparationKind.REPLAY, StreamExecutionMode.DIRECT, True),
        (PreparationKind.REPLAY, StreamExecutionMode.AGENT_HOST, False),
    ],
)
def test_prepared_execution_kind_and_mode_matrix(
    kind: PreparationKind,
    mode: StreamExecutionMode,
    valid: bool,
) -> None:
    if valid:
        prepared = PreparedStreamExecution(
            invocation_id=uuid4(),
            preparation_kind=kind,
            execution_mode=mode,
            opaque_state=object(),
        )
        assert prepared.lifecycle_state is PreparedLifecycleState.PREPARED
    else:
        with pytest.raises(ValueError):
            PreparedStreamExecution(
                invocation_id=uuid4(),
                preparation_kind=kind,
                execution_mode=mode,
                opaque_state=object(),
            )


def test_failure_codes_are_closed_and_errors_are_not_serializable() -> None:
    assert RuntimeFailureOutcome(code=RuntimeFailureCode.SOURCE_LOAD_FAILED).code is RuntimeFailureCode.SOURCE_LOAD_FAILED
    with pytest.raises((TypeError, ValueError)):
        RuntimeFailureOutcome(code="made_up_failure")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ErrorEvent(code="made_up_failure", message="bad")  # type: ignore[arg-type]
    for error in (RuntimeCancelled("secret reason"), RuntimeTransportAborted("secret reason")):
        assert "secret reason" not in repr(error)
        assert "secret reason" not in str(error)
        with pytest.raises(TypeError):
            pickle.dumps(error)


def test_tool_and_write_statuses_use_baseline_finite_vocabularies() -> None:
    for confirm_mode in ("none", "hitl", "approved", "rejected"):
        ToolCallEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            confirm_mode=confirm_mode,
        )
    with pytest.raises(ValueError):
        ToolCallEvent(tool_call_id="call-1", tool_name="lookup", confirm_mode="ask")

    for status in ("success", "error", "cancelled"):
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            status=status,
            summary="done",
        )
    with pytest.raises(ValueError):
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            status="completed",
            summary="done",
        )

    for write_status in ("none", "success", "failed", "cancelled"):
        MessageOutcome(message="done", write_status=write_status)
        OperationReplayOutcome(operation_id="op-1", write_status=write_status)
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            status="success",
            summary="done",
            write_status=write_status,
        )
    with pytest.raises(ValueError):
        MessageOutcome(message="done", write_status="pending")

    for status in ("committed", "rejected", "failed"):
        OperationReplayOutcome(operation_id="op-1", status=status)
    with pytest.raises(ValueError):
        OperationReplayOutcome(operation_id="op-1", status="success")


def test_sensitive_and_opaque_values_are_not_exposed_by_repr() -> None:
    class SecretOpaque:
        def __repr__(self) -> str:
            return "opaque-secret"

    prepared = PreparedStreamExecution(
        invocation_id=uuid4(),
        preparation_kind=PreparationKind.MODEL,
        execution_mode=StreamExecutionMode.AGENT_HOST,
        opaque_state=SecretOpaque(),
    )
    pending = PendingActionPayload(
        tool_name="update_application_status",
        operation_id="op-1",
        human="更新投递状态",
        args=_json_object({"password": "secret-password"}),
        confirmation_token="secret-token",
    )
    confirmation = ConfirmationRequest(
        conversation_id=1,
        approved=True,
        confirmation_token="secret-token",
    )
    required = ConfirmationRequiredOutcome(
        confirmation_token="secret-token",
        pending_action=pending,
    )
    required_event = ConfirmationRequiredEvent(
        confirmation_token="secret-token",
        pending_action=pending,
    )
    assert "opaque-secret" not in repr(prepared)
    assert "secret-password" not in repr(pending)
    assert "secret-token" not in repr(pending)
    assert "secret-token" not in repr(confirmation)
    assert "secret-token" not in repr(required)
    assert "secret-token" not in repr(required_event)


def test_stream_version_and_transport_mode_are_closed_and_consistent() -> None:
    assert RuntimeTransportContext(mode="sync").stream_version is None
    run_id = uuid4()
    assert RuntimeTransportContext(
        mode="stream", transport_run_id=run_id, stream_version="pilot-sse-v1"
    ).stream_version == "pilot-sse-v1"
    with pytest.raises(TypeError):
        RuntimeTransportContext(mode=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeTransportContext(mode="sync", transport_run_id=run_id)
    with pytest.raises(ValueError):
        RuntimeTransportContext(mode="stream", stream_version="pilot-sse-v1")
    with pytest.raises(TypeError):
        RuntimeTransportContext(
            mode="stream", transport_run_id="run-1", stream_version="pilot-sse-v1"
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeTransportContext(mode="sync", stream_version="pilot-sse-v1")
    with pytest.raises(ValueError):
        RuntimeTransportContext(mode="stream", stream_version="pilot-sse-v2")
    with pytest.raises(ValueError):
        MetaEvent(stream_version="pilot-sse-v2")


def test_invalid_lifecycle_objects_are_rejected_before_property_access() -> None:
    with pytest.raises(TypeError):
        PreparedStreamExecution(
            invocation_id="run-1",
            preparation_kind=PreparationKind.MODEL,
            execution_mode=StreamExecutionMode.AGENT_HOST,
            opaque_state=object(),
            lifecycle=object(),  # type: ignore[arg-type]
            lifecycle_state=PreparedLifecycleState.PREPARED,
        )


def test_failure_http_status_and_sensitive_repr_fields_are_closed() -> None:
    RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR, status_code=100)
    RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR, status_code=599)
    with pytest.raises(ValueError):
        RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR, status_code=99)
    with pytest.raises(ValueError):
        RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR, status_code=600)

    secret = _json_object({"secret": "do-not-print"})
    message = MessageOutcome(message="done", undo=secret)
    result = ToolResultEvent(
        tool_call_id="call-1",
        tool_name="lookup",
        status="success",
        summary="done",
        evidence=(secret,),
        affected_resources=(secret,),
        changed_entities=(secret,),
    )
    assert "do-not-print" not in repr(message)
    assert "do-not-print" not in repr(result)


def test_prepared_runtime_state_explicitly_rejects_serialization() -> None:
    prepared = PreparedStreamExecution(
        invocation_id="run-1",
        preparation_kind=PreparationKind.MODEL,
        execution_mode=StreamExecutionMode.AGENT_HOST,
        opaque_state=object(),
    )
    for value in (PreparedLifecycle(), prepared):
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            value.__getstate__()  # type: ignore[attr-defined]


def test_agent_host_contract_is_generic_and_does_not_use_object_result() -> None:
    return_type = get_type_hints(AgentExecutionHost.run)["return"]
    assert isinstance(return_type, TypeVar)
    assert return_type.__name__ == "ResultT"


def test_chat_route_failure_codes_match_the_closed_baseline_set() -> None:
    api_path = Path(__file__).parents[2] / "src" / "offerpilot" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    route_names = {"send_chat", "send_chat_stream", "confirm_chat", "confirm_chat_stream"}
    route_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in route_names
    ]
    literals: set[str] = set()
    for node in route_nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id == "error_response":
                    for keyword in child.keywords:
                        if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                            if isinstance(keyword.value.value, str):
                                literals.add(keyword.value.value)
            if isinstance(child, ast.Dict):
                for key, value in zip(child.keys, child.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "code"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        literals.add(value.value)
    expected = {
        "ai_provider_error",
        "application_archive_idempotency_conflict",
        "application_archive_invalid_request",
        "application_archive_source_conflict",
        "application_jd_idempotency_conflict",
        "application_jd_invalid_request",
        "application_jd_not_found",
        "application_jd_stale_current_version",
        "application_not_found",
        "application_outcome_idempotency_conflict",
        "application_outcome_invalid_request",
        "application_outcome_source_conflict",
        "chat_agent_timeout",
        "confirmation_in_progress",
        "conversation_archived",
        "invalid_confirmation",
        "operation_busy",
        "operation_delivery_failed",
        "operation_delivery_pending",
        "operation_delivery_unknown",
        "operation_failed",
        "operation_identity_conflict",
        "operation_input_conflict",
        "operation_integrity_error",
        "operation_not_committed",
        "operation_not_transactional",
        "operation_projection_failed",
        "operation_result_too_large",
        "operation_result_unknown",
        "operation_unavailable",
        "pending_confirmation_required",
        "resume_not_found",
        "source_load_failed",
        "stale_pending_action",
    }
    enum_values = {item.value for item in RuntimeFailureCode}
    assert literals
    assert literals <= enum_values
    # These are the non-literal sources reached by the four routes: the
    # operation ledger and the two deterministic legacy repositories.  Keep
    # this evidence explicit so a route-only AST scan cannot silently omit a
    # dynamic exception or mapping value.
    source_expectations = {
        api_path: {
            "ai_provider_error",
            "application_archive_idempotency_conflict",
            "application_archive_invalid_request",
            "application_archive_source_conflict",
            "application_jd_idempotency_conflict",
            "application_jd_invalid_request",
            "application_jd_not_found",
            "application_jd_stale_current_version",
            "application_not_found",
            "application_outcome_idempotency_conflict",
            "application_outcome_invalid_request",
            "application_outcome_source_conflict",
            "chat_agent_timeout",
            "confirmation_in_progress",
            "conversation_archived",
            "invalid_confirmation",
            "operation_delivery_failed",
            "operation_delivery_pending",
            "operation_failed",
            "operation_identity_conflict",
            "operation_input_conflict",
            "operation_integrity_error",
            "operation_result_unknown",
            "operation_unavailable",
            "pending_confirmation_required",
            "resume_not_found",
            "source_load_failed",
            "stale_pending_action",
        },
        api_path.parents[0] / "ai" / "write_operations.py": {
            "operation_busy",
            "operation_delivery_failed",
            "operation_delivery_pending",
            "operation_delivery_unknown",
            "operation_identity_conflict",
            "operation_input_conflict",
            "operation_integrity_error",
            "operation_not_committed",
            "operation_not_transactional",
            "operation_projection_failed",
            "operation_result_too_large",
            "operation_result_unknown",
            "operation_unavailable",
        },
        api_path.parents[0] / "repositories" / "application_jd_versions.py": {
            "application_jd_idempotency_conflict",
            "application_jd_invalid_request",
            "application_jd_not_found",
            "application_jd_stale_current_version",
        },
        api_path.parents[0] / "repositories" / "application_outcomes.py": {
            "application_archive_idempotency_conflict",
            "application_archive_invalid_request",
            "application_archive_source_conflict",
            "application_not_found",
            "application_outcome_idempotency_conflict",
            "application_outcome_invalid_request",
            "application_outcome_source_conflict",
            "resume_not_found",
        },
    }
    for source_path, source_codes in source_expectations.items():
        source_text = source_path.read_text(encoding="utf-8")
        assert source_codes <= {
            code for code in expected if code in source_text
        }, source_path
    assert enum_values == expected
