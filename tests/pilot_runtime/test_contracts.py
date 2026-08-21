from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from enum import StrEnum
from threading import Barrier, Thread
from types import MappingProxyType
from typing import Literal, get_args, get_origin, get_type_hints
from uuid import uuid4

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
    ErrorEvent,
    FirstModelCompletedSignal,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    MetaEvent,
    OperationPendingOutcome,
    OperationReplayOutcome,
    PreparationKind,
    PreparedLifecycle,
    PreparedLifecycleState,
    PreparedStreamExecution,
    RuntimeFailureOutcome,
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
            prepared_state=object(),
            lifecycle_state=PreparedLifecycleState.COMPLETED,
            completion_reason=None,
        )
    with pytest.raises(ValueError):
        PreparedStreamExecution(
            invocation_id=uuid4(),
            preparation_kind=PreparationKind.MODEL,
            execution_mode=StreamExecutionMode.DIRECT,
            prepared_state=object(),
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


def test_all_contracts_are_closed_frozen_slot_dataclasses() -> None:
    values = [
        StartTurnRequest(message="hello"),
        ConfirmationRequest(conversation_id=1, approved=True, confirmation_token="token"),
        RuntimeTransportContext(mode="sync"),
        ImmediateHttpOutcome(status_code=200, payload=MappingProxyType({"ok": True})),
        MessageOutcome(message="done"),
        ConfirmationRequiredOutcome(conversation_id=1, confirmation_token="token"),
        RuntimeFailureOutcome(code="provider_error", message="暂时不可用"),
        OperationPendingOutcome(operation_id="op-1"),
        OperationReplayOutcome(operation_id="op-1"),
        PreparedLifecycle(),
        MetaEvent(stream_version="pilot-sse-v1"),
        UserMessageSavedEvent(),
        StatusEvent(phase="model_running", label="正在思考"),
        AssistantDeltaEvent(delta="hi"),
        ToolCallEvent(tool_call_id="call-1", tool_name="lookup"),
        ToolResultEvent(tool_call_id="call-1", status="completed"),
        ConfirmationRequiredEvent(confirmation_token="token"),
        AssistantMessageEvent(message="done"),
        ErrorEvent(code="provider_error", message="暂时不可用"),
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
