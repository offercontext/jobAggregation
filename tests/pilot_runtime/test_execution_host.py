from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from offerpilot.chat_transport import (
    CHAT_AGENT_TIMEOUT_SECONDS,
    SSE_POLL_SECONDS,
    SseAgentExecutionHost,
    SyncAgentExecutionHost,
)
from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    CancelReason,
    InvocationState,
    RuntimeEventSink,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl


def _collect(stream: Iterator[object]) -> tuple[list[object], object | None]:
    events: list[object] = []
    iterator = iter(stream)
    while True:
        try:
            events.append(next(iterator))
        except StopIteration as stop:
            return events, stop.value


def test_sync_host_times_only_the_agent_thunk() -> None:
    before = time.perf_counter()
    time.sleep(0.02)
    control = InMemoryRuntimeInvocationControl()
    result = SyncAgentExecutionHost[str](timeout_seconds=0.05).run(lambda: "ok", control)
    time.sleep(0.02)
    elapsed = time.perf_counter() - before

    assert result == "ok"
    assert elapsed >= 0.04
    assert control.state is InvocationState.COMPLETED


def test_sync_host_timeout_does_not_wait_for_uncancellable_worker() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def blocking() -> str:
        nonlocal calls
        calls += 1
        started.set()
        try:
            release.wait(1)
            return "late"
        finally:
            finished.set()

    control = InMemoryRuntimeInvocationControl()
    host = SyncAgentExecutionHost[str](timeout_seconds=0.01)
    started_at = time.perf_counter()
    with pytest.raises(RuntimeAgentTimedOut):
        host.run(blocking, control)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.2
    assert started.wait(0.2)
    assert control.state is InvocationState.TIMED_OUT
    assert calls == 1
    release.set()
    assert finished.wait(0.2)
    assert control.state is InvocationState.TIMED_OUT
    with pytest.raises(RuntimeTransportAborted):
        host.run(lambda: "second", control)
    assert calls == 1


@pytest.mark.parametrize("error", [ValueError("ordinary"), RuntimeCancelled(), RuntimeTransportAborted()])
def test_sync_host_preserves_worker_exception_categories(error: BaseException) -> None:
    control = InMemoryRuntimeInvocationControl()

    def raise_error() -> str:
        raise error

    with pytest.raises(type(error)) as raised:
        SyncAgentExecutionHost[str](timeout_seconds=0.2).run(raise_error, control)
    assert raised.value is error


def test_sync_host_preserves_base_exception() -> None:
    control = InMemoryRuntimeInvocationControl()

    def raise_base() -> str:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        SyncAgentExecutionHost[str](timeout_seconds=0.2).run(raise_base, control)


def test_sse_host_is_typed_ordered_and_uses_unbounded_queue() -> None:
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.2)
    events = [AssistantDeltaEvent(delta="one"), AssistantDeltaEvent(delta="two")]

    def thunk(sink: RuntimeEventSink) -> str:
        sink.emit(events[0])
        sink.emit(events[1])
        return "done"

    stream = host.run(thunk, control)
    assert stream.event_queue.maxsize == 0
    assert stream.poll_seconds == SSE_POLL_SECONDS == 0.1
    delivered, result = _collect(stream)

    assert delivered == events
    assert result == "done"
    assert control.state is InvocationState.COMPLETED
    assert host.timeout_seconds == 0.2
    assert CHAT_AGENT_TIMEOUT_SECONDS == 120.0


def test_sse_host_worker_is_not_blocked_by_slow_consumer() -> None:
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.2)
    produced = threading.Event()
    events = [AssistantDeltaEvent(delta=str(index)) for index in range(5)]

    def thunk(sink: RuntimeEventSink) -> str:
        for event in events:
            sink.emit(event)
        produced.set()
        return "done"

    stream = host.run(thunk, control)
    assert produced.wait(0.1)
    time.sleep(0.12)
    delivered, result = _collect(stream)

    assert delivered == events
    assert result == "done"
    assert control.state is InvocationState.COMPLETED


def test_sse_host_timeout_drops_late_events_and_result() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    late = AssistantDeltaEvent(delta="late")
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.01)
    calls = 0

    def thunk(sink: RuntimeEventSink) -> str:
        nonlocal calls
        calls += 1
        started.set()
        try:
            release.wait(1)
            sink.emit(late)
            return "late"
        finally:
            finished.set()

    stream = host.run(thunk, control)
    assert started.wait(0.2)
    with pytest.raises(RuntimeAgentTimedOut):
        next(stream)
    assert control.state is InvocationState.TIMED_OUT
    release.set()
    assert finished.wait(0.2)
    assert calls == 1


def test_sse_host_worker_exception_is_rethrown_and_cancel_event_is_set() -> None:
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.2)
    error = RuntimeTransportAborted()

    def thunk(_sink: RuntimeEventSink, cancelled: object) -> str:
        assert callable(cancelled)
        raise error

    stream = host.run(thunk, control)
    with pytest.raises(RuntimeTransportAborted) as raised:
        next(stream)
    assert raised.value is error
    assert stream.cancel_event.is_set()


def test_sse_worker_exception_keeps_baseline_shutdown_flag_false() -> None:
    shutdown_flags: list[bool] = []

    class SpyExecutor(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutdown_flags.append(cancel_futures)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](
        timeout_seconds=0.2,
        executor_factory=lambda **kwargs: SpyExecutor(**kwargs),
    )

    def thunk(_sink: RuntimeEventSink) -> str:
        raise ValueError("ordinary worker failure")

    stream = host.run(thunk, control)
    with pytest.raises(ValueError, match="ordinary worker failure"):
        next(stream)

    assert shutdown_flags == [False]


def test_sse_host_client_cancel_is_control_flow_and_does_not_start_again() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.2)

    def thunk(_sink: RuntimeEventSink) -> str:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        return "late"

    stream = host.run(thunk, control)
    assert started.wait(0.2)
    assert control.request_cancel(CancelReason.CLIENT_DISCONNECT) is True
    with pytest.raises(RuntimeCancelled):
        next(stream)
    release.set()
    assert calls == 1
    with pytest.raises(RuntimeTransportAborted):
        host.run(lambda: "second", control)


def test_sse_host_rejects_untyped_events_without_parsing_bytes() -> None:
    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](timeout_seconds=0.2)

    def thunk(sink: RuntimeEventSink) -> str:
        sink.emit(b"event: assistant_delta")  # type: ignore[arg-type]
        return "never"

    stream = host.run(thunk, control)
    with pytest.raises(TypeError, match="typed RuntimeEvent"):
        next(stream)
