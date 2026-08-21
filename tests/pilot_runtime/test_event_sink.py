from __future__ import annotations

from dataclasses import dataclass

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CancelReason,
    CompletedEvent,
    FirstModelCompletedSignal,
    InvocationState,
    MetaEvent,
    RuntimeFailureOutcome,
    SignalEmitResult,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageSavedEvent,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.event_sink import (
    CallableRuntimeEventSink,
    InMemoryRuntimeInvocationControl,
    RuntimeSignalLatch,
    emit_runtime_event,
    require_runtime_active,
    runtime_event_payload,
)


def test_safe_emit_maps_ordinary_sink_exception_and_stops_after_failure() -> None:
    seen: list[object] = []

    def fail(event: object) -> None:
        seen.append(event)
        raise OSError("closed")

    sink = CallableRuntimeEventSink(fail)
    with pytest.raises(RuntimeTransportAborted):
        emit_runtime_event(sink, StatusEvent(phase="model_running", label="正在思考"))
    with pytest.raises(RuntimeTransportAborted):
        emit_runtime_event(sink, CompletedEvent())
    assert len(seen) == 1


@pytest.mark.parametrize("control_error", [RuntimeCancelled(), RuntimeTransportAborted()])
def test_safe_emit_preserves_control_exceptions(control_error: Exception) -> None:
    def fail(_event: object) -> None:
        raise control_error

    with pytest.raises(type(control_error)):
        emit_runtime_event(
            CallableRuntimeEventSink(fail),
            StatusEvent(phase="model_running", label="正在思考"),
        )


@pytest.mark.parametrize("base_error", [KeyboardInterrupt(), SystemExit(3)])
def test_safe_emit_does_not_catch_base_exceptions(base_error: BaseException) -> None:
    def fail(_event: object) -> None:
        raise base_error

    with pytest.raises(type(base_error)):
        emit_runtime_event(
            CallableRuntimeEventSink(fail),
            StatusEvent(phase="model_running", label="正在思考"),
        )


def test_runtime_event_payload_is_closed_and_pure() -> None:
    events = [
        MetaEvent(),
        UserMessageSavedEvent(),
        StatusEvent(phase="thinking", label="思考"),
        AssistantDeltaEvent(delta="a"),
        ToolCallEvent(tool_call_id="call-1", tool_name="lookup"),
        ToolResultEvent(
            tool_call_id="call-1",
            tool_name="lookup",
            status="success",
            summary="done",
        ),
        AssistantMessageEvent(message="done"),
        CompletedEvent(),
    ]
    assert runtime_event_payload(UserMessageSavedEvent()) == {"role": "user"}
    for event in events:
        first = runtime_event_payload(event)
        second = runtime_event_payload(event)
        assert first == second
        assert isinstance(first, dict)

    with pytest.raises(TypeError):
        runtime_event_payload({"role": "user"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        runtime_event_payload(object())  # type: ignore[arg-type]


def test_invocation_control_is_closed_cas_and_maps_control_errors() -> None:
    control = InMemoryRuntimeInvocationControl()
    assert control.state is InvocationState.ACTIVE
    assert control.request_cancel(CancelReason.CLIENT_DISCONNECT) is True
    assert control.request_cancel(CancelReason.EXPLICIT_CANCEL) is False
    assert control.mark_completed() is False
    assert control.state is InvocationState.CANCELLED
    with pytest.raises(RuntimeCancelled):
        require_runtime_active(control)

    timed_out = InMemoryRuntimeInvocationControl()
    assert timed_out.request_timeout() is True
    assert timed_out.state is InvocationState.TIMED_OUT
    with pytest.raises(RuntimeAgentTimedOut):
        require_runtime_active(timed_out)

    aborted = InMemoryRuntimeInvocationControl()
    assert aborted.request_cancel(CancelReason.TRANSPORT_ABORTED) is True
    with pytest.raises(RuntimeTransportAborted):
        require_runtime_active(aborted)

    complete = InMemoryRuntimeInvocationControl()
    assert complete.mark_completed() is True
    assert complete.state is InvocationState.COMPLETED
    require_runtime_active(complete)


def test_runtime_signal_latch_is_capacity_one_nonblocking_and_fail_open() -> None:
    latch = RuntimeSignalLatch()
    signal = FirstModelCompletedSignal()
    assert latch.try_emit(signal) is SignalEmitResult.EMITTED
    assert latch.try_emit(signal) is SignalEmitResult.DUPLICATE
    assert latch.drain() == signal
    assert latch.drain() is None
    assert latch.try_emit(signal) is SignalEmitResult.EMITTED
    latch.close()
    assert latch.try_emit(signal) is SignalEmitResult.CLOSED
    assert latch.finalize() is None
    assert latch.finalize() is None


def test_runtime_signal_latch_reports_full_and_registration_failure_without_leaking() -> None:
    latch = RuntimeSignalLatch()
    assert latch.try_emit(FirstModelCompletedSignal()) is SignalEmitResult.EMITTED
    assert latch.try_emit(FirstModelCompletedSignal()) is SignalEmitResult.FULL

    calls: list[object] = []

    def register(signal: FirstModelCompletedSignal) -> None:
        calls.append(signal)
        raise OSError("registration unavailable")

    failed = RuntimeSignalLatch(register=register)
    assert failed.try_emit(FirstModelCompletedSignal()) is SignalEmitResult.EMITTED
    assert failed.finalize() is None
    assert len(calls) == 1
    assert failed.finalize() is None


def test_control_exceptions_are_not_product_outcomes() -> None:
    for control_error in (RuntimeCancelled(), RuntimeTransportAborted(), RuntimeAgentTimedOut()):
        assert not isinstance(control_error, RuntimeFailureOutcome)


@dataclass(frozen=True)
class _NotAnEvent:
    value: str
