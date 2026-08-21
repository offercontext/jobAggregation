from __future__ import annotations

from dataclasses import dataclass
from threading import Barrier, Event, Lock, Thread
from time import sleep

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CancelReason,
    CompletedEvent,
    CompletionReason,
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
    runtime_outcome_payload,
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


def test_payload_projection_omits_baseline_absent_optional_fields_and_internal_status() -> None:
    event = ToolResultEvent(
        tool_call_id="call-1",
        tool_name="lookup",
        status="success",
        summary="done",
    )
    assert runtime_event_payload(event) == {
        "tool_call_id": "call-1",
        "tool_name": "lookup",
        "status": "success",
        "summary": "done",
        "evidence": [],
        "affected_resources": [],
        "changed_entities": [],
    }

    from offerpilot.pilot_runtime.contracts import OperationReplayOutcome
    from offerpilot.chat_transport import event_sse_payload, outcome_http_payload

    replay = OperationReplayOutcome(
        operation_id="op-1",
        message="已完成",
        status="committed",
        write_status="success",
    )
    assert outcome_http_payload(replay) == {
        "type": "message",
        "operation_id": "op-1",
        "message": "已完成",
        "write_status": "success",
        "replayed": True,
    }
    assert "status" not in event_sse_payload(CompletedEvent(response=replay))["response"]


def test_event_and_sse_projection_reject_runtime_event_subclasses() -> None:
    class ChildStatusEvent(StatusEvent):
        pass

    child = ChildStatusEvent(phase="thinking", label="思考")
    from offerpilot.chat_transport import event_sse_payload

    with pytest.raises(TypeError):
        runtime_event_payload(child)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        event_sse_payload(child)  # type: ignore[arg-type]


def test_outcome_renderers_are_closed_exact_types() -> None:
    from offerpilot.chat_transport import outcome_http_payload, outcome_http_status
    from offerpilot.pilot_runtime.contracts import MessageOutcome

    class ChildMessageOutcome(MessageOutcome):
        pass

    outcome = MessageOutcome(message="done")
    assert outcome_http_status(outcome) == 200
    assert outcome_http_payload(outcome)["message"] == "done"
    assert runtime_outcome_payload(outcome)["message"] == "done"

    for unknown in (object(), ChildMessageOutcome(message="child")):
        with pytest.raises(TypeError):
            outcome_http_status(unknown)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            outcome_http_payload(unknown)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            runtime_outcome_payload(unknown)


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
    assert latch.try_emit(signal) is SignalEmitResult.DUPLICATE
    latch.close()
    assert latch.try_emit(signal) is SignalEmitResult.CLOSED
    assert latch.finalize() is SignalEmitResult.CLOSED
    assert latch.finalize() is SignalEmitResult.CLOSED


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
    assert failed.finalize() is SignalEmitResult.DEGRADED
    assert len(calls) == 1
    assert failed.finalize() is SignalEmitResult.CLOSED


def test_runtime_signal_latch_has_permanent_one_shot_and_close_discards_signal() -> None:
    signal = FirstModelCompletedSignal()
    latch = RuntimeSignalLatch()
    assert latch.try_emit(signal) is SignalEmitResult.EMITTED
    assert latch.drain() == signal
    assert latch.try_emit(signal) is SignalEmitResult.DUPLICATE

    registered: list[FirstModelCompletedSignal] = []
    closed = RuntimeSignalLatch(register=registered.append)
    assert closed.try_emit(signal) is SignalEmitResult.EMITTED
    closed.close()
    assert closed.drain() is None
    closed.finalize()
    assert registered == []
    assert closed.try_emit(signal) is SignalEmitResult.CLOSED

    exited = RuntimeSignalLatch(register=registered.append)
    assert exited.try_emit(signal) is SignalEmitResult.EMITTED
    exited.consumer_exit()
    assert exited.drain() is None
    exited.finalize()
    assert registered == []


def test_runtime_signal_latch_try_emit_does_not_run_sink_callback() -> None:
    called = Event()

    def sink(_signal: FirstModelCompletedSignal) -> None:
        called.set()
        raise OSError("sink should run only during owner finalization")

    latch = RuntimeSignalLatch(sink=sink)
    assert latch.try_emit(FirstModelCompletedSignal()) is SignalEmitResult.EMITTED
    assert not called.is_set()


def test_runtime_signal_latch_dispatches_sink_once_during_finalize() -> None:
    seen: list[FirstModelCompletedSignal] = []

    class TypedSink:
        def try_emit(self, signal: FirstModelCompletedSignal) -> SignalEmitResult:
            seen.append(signal)
            return SignalEmitResult.EMITTED

    latch = RuntimeSignalLatch(sink=TypedSink())
    signal = FirstModelCompletedSignal()

    assert latch.try_emit(signal) is SignalEmitResult.EMITTED
    latch.finalize()
    latch.finalize()

    assert seen == [signal]


def test_runtime_signal_latch_rejects_non_signal_and_finalize_reports_no_signal() -> None:
    registered: list[FirstModelCompletedSignal] = []
    latch = RuntimeSignalLatch(register=registered.append)

    with pytest.raises(TypeError):
        latch.try_emit("title")  # type: ignore[arg-type]
    signal = FirstModelCompletedSignal()
    assert latch.try_emit(signal) is SignalEmitResult.EMITTED
    assert latch.drain() == signal
    assert latch.finalize() is SignalEmitResult.CLOSED
    assert latch.finalize() is SignalEmitResult.CLOSED
    assert registered == []


class _DelayedLifecycleRuntime:
    def __init__(self) -> None:
        from offerpilot.pilot_runtime.contracts import PreparedLifecycle

        self.lifecycle = PreparedLifecycle()
        self.begin_calls = 0
        self.abort_calls = 0
        self.cleanup_calls = 0
        self._lock = Lock()
        self.entered = Event()
        self.release = Event()

    @property
    def lifecycle_state(self) -> object:
        return self.lifecycle.state

    def begin(self) -> bool:
        with self._lock:
            self.begin_calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return self.lifecycle.begin()

    def abort(self) -> bool:
        with self._lock:
            self.abort_calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return self.lifecycle.abort_if_prepared()

    def complete(self, _reason: object) -> bool:
        return self.lifecycle.complete(_reason)  # type: ignore[arg-type]

    def cleanup(self, _reason: object | None) -> None:
        with self._lock:
            self.cleanup_calls += 1


def test_guard_transition_callback_is_claimed_once_under_40_thread_race() -> None:
    from offerpilot.chat_transport import PreparedStreamGuard

    runtime = _DelayedLifecycleRuntime()
    guard = PreparedStreamGuard(runtime=runtime)
    barrier = Barrier(40)
    results: list[bool] = []
    results_lock = Lock()

    def worker(index: int) -> None:
        barrier.wait(timeout=5)
        result = guard.begin_execution() if index % 2 == 0 else guard.abort_if_prepared()
        with results_lock:
            results.append(result)

    threads = [Thread(target=worker, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    assert runtime.entered.wait(timeout=5)
    sleep(0.01)
    runtime.release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 40
    assert runtime.begin_calls + runtime.abort_calls == 1
    if runtime.lifecycle.state.name == "EXECUTING":
        assert guard.complete(CompletionReason.NORMAL) is True
    assert runtime.cleanup_calls == 1
    assert sum(results) == 1


def test_control_exceptions_are_not_product_outcomes() -> None:
    for control_error in (RuntimeCancelled(), RuntimeTransportAborted(), RuntimeAgentTimedOut()):
        assert not isinstance(control_error, RuntimeFailureOutcome)


@dataclass(frozen=True)
class _NotAnEvent:
    value: str
