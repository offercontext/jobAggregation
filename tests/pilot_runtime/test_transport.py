from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import ClientDisconnect

from offerpilot.sse import SseRun

from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CompletionReason,
    CompletedEvent,
    MetaEvent,
    MessageOutcome,
    PreparedLifecycle,
    PreparedLifecycleState,
    RuntimeFailureOutcome,
    RuntimeFailureCode,
    RuntimeEventSink,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.chat_transport import (
    build_guarded_streaming_response,
    encode_sse_event,
    GuardedStreamingResponse,
    PreparedStreamGuard,
    SseAgentExecutionHost,
    event_sse_payload,
    outcome_http_payload,
    outcome_http_response,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl


def test_outcome_http_and_event_sse_renderers_are_pure_and_safe() -> None:
    outcome = MessageOutcome(message="done", conversation_id=4)
    assert outcome_http_payload(outcome) == {
        "type": "message",
        "message": "done",
        "conversation_id": 4,
    }
    assert outcome_http_response(outcome).status_code == 200
    failure = RuntimeFailureOutcome(code=RuntimeFailureCode.AI_PROVIDER_ERROR)
    assert outcome_http_payload(failure)["error_code"] == "ai_provider_error"
    assert event_sse_payload(AssistantMessageEvent(message="hi"))["message"] == "hi"
    assert event_sse_payload(CompletedEvent(response=outcome))["response"]["message"] == "done"


def test_guard_shared_lifecycle_winner_runs_cleanup_once() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"begin": 0, "abort": 0, "complete": 0, "cleanup": 0, "execute": 0}

    def begin() -> bool:
        calls["begin"] += 1
        return lifecycle.begin()

    def abort() -> bool:
        calls["abort"] += 1
        won = lifecycle.abort_if_prepared()
        if won:
            calls["cleanup"] += 1
        return won

    def complete(reason: CompletionReason) -> bool:
        calls["complete"] += 1
        return lifecycle.complete(reason)

    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        begin=begin,
        abort_if_prepared=abort,
        complete=complete,
        on_cleanup=lambda _reason=None: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )
    assert guard.begin_execution() is True
    calls["execute"] += 1
    assert guard.complete(CompletionReason.NORMAL) is True
    assert guard.complete(CompletionReason.NORMAL) is False
    assert guard.abort_if_prepared() is False
    assert calls["cleanup"] == 1
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert calls["execute"] in {0, 1}


def test_guarded_response_disconnects_before_body_and_aborts() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"execute": 0, "cleanup": 0}
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_abort=lifecycle.abort_if_prepared,
        on_cleanup=lambda _reason=None: calls.__setitem__("cleanup", calls["cleanup"] + 1),
        on_execute=lambda: calls.__setitem__("execute", calls["execute"] + 1),
    )
    response = GuardedStreamingResponse(
        content=[b"hello"],
        guard=guard,
        background=None,
    )
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    import asyncio

    asyncio.run(
        response(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "asgi": {"spec_version": "2.0"},
            },
            receive,
            send,
        )
    )
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert calls["execute"] == 0
    assert calls["cleanup"] == 1


def test_guarded_response_construction_failure_aborts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = PreparedLifecycle()
    calls = {"abort": 0, "cleanup": 0}

    def abort() -> bool:
        calls["abort"] += 1
        return lifecycle.abort_if_prepared()

    guard = PreparedStreamGuard(
        abort_if_prepared=abort,
        on_cleanup=lambda _reason: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )

    from starlette.responses import StreamingResponse

    original = StreamingResponse.__init__

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("response construction failed")

    monkeypatch.setattr(StreamingResponse, "__init__", fail)
    with pytest.raises(OSError):
        build_guarded_streaming_response([], guard=guard)
    monkeypatch.setattr(StreamingResponse, "__init__", original)
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert calls == {"abort": 1, "cleanup": 1}


def test_guarded_response_normal_and_duplicate_finalizers_cleanup_once() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"cleanup": 0, "background": 0}
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_cleanup=lambda _reason: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )

    def background() -> None:
        calls["background"] += 1

    response = GuardedStreamingResponse([b"hello"], guard, background=background)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(
        response(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
    )
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.NORMAL
    assert calls == {"cleanup": 1, "background": 1}
    asyncio.run(response._background_finalizer())
    assert calls == {"cleanup": 1, "background": 1}


def test_guarded_response_first_iteration_disconnect_maps_cancelled() -> None:
    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_execute=lambda: (_ for _ in ()).throw(asyncio.CancelledError()),
        on_cleanup=lambda _reason: None,
    )
    response = GuardedStreamingResponse([b"never"], guard)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.CANCELLED


def test_guarded_response_renderer_failure_maps_transport_aborted() -> None:
    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(lifecycle=lifecycle)

    async def body():
        raise OSError("renderer failed")
        yield b"unreachable"

    response = GuardedStreamingResponse(body(), guard)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(RuntimeTransportAborted):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.TRANSPORT_ABORTED


def test_guarded_response_first_send_oserror_becomes_client_disconnect_and_aborts() -> None:
    from starlette.requests import ClientDisconnect

    lifecycle = PreparedLifecycle()
    calls = {"execute": 0}
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_execute=lambda: calls.__setitem__("execute", calls["execute"] + 1),
    )
    response = GuardedStreamingResponse([b"never"], guard)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert calls["execute"] == 0


def test_guarded_response_midstream_send_oserror_becomes_client_disconnect_cancelled() -> None:
    from starlette.requests import ClientDisconnect

    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(lifecycle=lifecycle)
    response = GuardedStreamingResponse([b"hello"], guard)
    sends = {"count": 0}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sends["count"] += 1
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert sends["count"] == 2
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.CANCELLED


def test_guarded_response_closes_replacement_source_after_send_disconnect() -> None:
    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(lifecycle=lifecycle)
    calls = {"close": 0}

    class Source:
        def __iter__(self):
            yield b"first"
            yield b"second"

        def close(self) -> None:
            calls["close"] += 1

    source = Source()
    response = GuardedStreamingResponse(
        [],
        guard,
        execute=lambda: source,
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(
            response(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "headers": [],
                    "asgi": {"spec_version": "2.4"},
                },
                receive,
                send,
            )
        )

    assert calls["close"] == 1


def test_guarded_response_acloses_async_replacement_source_after_send_disconnect() -> None:
    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(lifecycle=lifecycle)
    calls = {"aclose": 0}

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            if calls["aclose"]:
                raise StopAsyncIteration
            return b"first"

        async def aclose(self) -> None:
            calls["aclose"] += 1

    source = Source()
    response = GuardedStreamingResponse(
        [],
        guard,
        execute=lambda: source,
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(
            response(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "headers": [],
                    "asgi": {"spec_version": "2.4"},
                },
                receive,
                send,
            )
        )

    assert calls["aclose"] == 1


@pytest.mark.parametrize("_attempt", range(3))
@pytest.mark.parametrize("send_error_type", [ClientDisconnect, GeneratorExit])
def test_guarded_response_closes_real_sse_host_when_send_exits(
    _attempt: int,
    send_error_type: type[BaseException],
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    shutdown_calls: list[bool] = []

    class SpyExecutor(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutdown_calls.append(cancel_futures)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](
        timeout_seconds=2.0,
        poll_seconds=0.01,
        executor_factory=lambda **kwargs: SpyExecutor(**kwargs),
    )

    def thunk(sink: RuntimeEventSink) -> str:
        sink.emit(MetaEvent())
        started.set()
        try:
            release.wait(2.0)
            return "done"
        finally:
            finished.set()

    stream = host.run(thunk, control)

    class NestedHostIterator:
        close_calls = 0

        def __init__(self) -> None:
            self._generator = self._events()

        def _events(self):
            for _event in stream:
                yield b"first"

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            return next(self._generator)

        def close(self) -> None:
            type(self).close_calls += 1
            self._generator.close()
            stream.close()

    response = GuardedStreamingResponse(
        [],
        PreparedStreamGuard(lifecycle=PreparedLifecycle()),
        execute=NestedHostIterator,
    )

    async def run_request() -> None:
        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                raise send_error_type()

        await asyncio.wait_for(
            response(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "headers": [],
                    "asgi": {"spec_version": "2.4"},
                },
                receive,
                send,
            ),
            timeout=1.0,
        )

    shutdown_before_cleanup: list[bool] = []
    started_before_cleanup = False
    finished_before_cleanup = False
    try:
        with pytest.raises(send_error_type):
            asyncio.run(run_request())
    finally:
        started_before_cleanup = started.is_set()
        release.set()
        finished_before_cleanup = finished.wait(0.2)
        shutdown_before_cleanup = list(shutdown_calls)
        if not stream._closed:
            stream.close()

    assert NestedHostIterator.close_calls == 1
    assert started_before_cleanup
    assert finished_before_cleanup
    assert shutdown_before_cleanup == [True], (
        shutdown_before_cleanup,
        started_before_cleanup,
    )


@pytest.mark.parametrize("_attempt", range(3))
@pytest.mark.parametrize("termination", ["disconnect", "manual"])
def test_guarded_response_closes_real_sse_host_on_pre24_disconnect_cancellation(
    _attempt: int,
    termination: str,
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    shutdown_calls: list[bool] = []

    class SpyExecutor(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutdown_calls.append(cancel_futures)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    control = InMemoryRuntimeInvocationControl()
    host = SseAgentExecutionHost[str](
        timeout_seconds=2.0,
        poll_seconds=0.01,
        executor_factory=lambda **kwargs: SpyExecutor(**kwargs),
    )

    def thunk(sink: RuntimeEventSink) -> str:
        sink.emit(MetaEvent())
        started.set()
        try:
            release.wait(2.0)
            return "done"
        finally:
            finished.set()

    stream = host.run(thunk, control)

    class NestedHostIterator:
        close_calls = 0

        def __init__(self) -> None:
            self._generator = self._events()

        def _events(self):
            for _event in stream:
                yield b"first"

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            return next(self._generator)

        def close(self) -> None:
            type(self).close_calls += 1
            self._generator.close()
            stream.close()

    response = GuardedStreamingResponse(
        [],
        PreparedStreamGuard(lifecycle=PreparedLifecycle()),
        execute=NestedHostIterator,
    )

    async def run_request() -> None:
        body_sent = asyncio.Event()
        response_task: asyncio.Task[None] | None = None
        receive_calls = 0

        async def receive() -> dict[str, object]:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            if termination == "manual":
                return {"type": "http.request", "body": b"", "more_body": False}
            await body_sent.wait()
            assert response_task is not None
            response_task.cancel()
            asyncio.get_running_loop().call_soon(response_task.cancel)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                body_sent.set()
                if termination == "manual":
                    assert response_task is not None
                    response_task.cancel()
                    asyncio.get_running_loop().call_soon(response_task.cancel)
                await asyncio.Event().wait()

        response_task = asyncio.create_task(
            response(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "headers": [],
                    "asgi": {
                        "spec_version": "2.0" if termination == "disconnect" else "2.4"
                    },
                },
                receive,
                send,
            )
        )
        await response_task

    shutdown_before_cleanup: list[bool] = []
    started_before_cleanup = False
    finished_before_cleanup = False
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(run_request(), timeout=1.0))
    finally:
        started_before_cleanup = started.is_set()
        release.set()
        finished_before_cleanup = finished.wait(0.2)
        shutdown_before_cleanup = list(shutdown_calls)
        if not stream._closed:
            stream.close()

    assert NestedHostIterator.close_calls == 1
    assert started_before_cleanup
    assert finished_before_cleanup
    assert shutdown_before_cleanup == [True], (
        shutdown_before_cleanup,
        started_before_cleanup,
    )


@pytest.mark.parametrize(
    "background_error, expected_reason",
    [
        (RuntimeCancelled(), CompletionReason.CANCELLED),
        (RuntimeTransportAborted(), CompletionReason.TRANSPORT_ABORTED),
        (RuntimeAgentTimedOut(), CompletionReason.CANCELLED),
        (ClientDisconnect(), CompletionReason.CANCELLED),
        (asyncio.CancelledError(), CompletionReason.CANCELLED),
    ],
)
def test_background_control_exception_is_preserved_and_classified(
    background_error: BaseException,
    expected_reason: CompletionReason,
) -> None:
    lifecycle = PreparedLifecycle()

    def background() -> None:
        raise background_error

    response = GuardedStreamingResponse([b"hello"], PreparedStreamGuard(lifecycle=lifecycle), background=background)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(type(background_error)):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is expected_reason


def test_cleanup_control_exception_is_not_reclassified_as_transport_failure() -> None:
    lifecycle = PreparedLifecycle()

    def cleanup(_reason: CompletionReason | None) -> None:
        raise RuntimeCancelled()

    response = GuardedStreamingResponse(
        [b"hello"],
        PreparedStreamGuard(lifecycle=lifecycle, on_cleanup=cleanup),
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(RuntimeCancelled):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )


def test_background_ordinary_exception_maps_transport_aborted() -> None:
    lifecycle = PreparedLifecycle()

    def background() -> None:
        raise OSError("background failed")

    response = GuardedStreamingResponse(
        [b"hello"],
        PreparedStreamGuard(lifecycle=lifecycle),
        background=background,
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(RuntimeTransportAborted):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.completion_reason is CompletionReason.TRANSPORT_ABORTED


def test_sse_envelope_cannot_override_reserved_typed_fields() -> None:
    event = AssistantMessageEvent(message="hello")
    for key, value in (("seq", 99), ("event", "wrong"), ("data", {"message": "wrong"})):
        with pytest.raises(ValueError):
            encode_sse_event(event, seq=1, envelope={key: value})


def test_sse_run_complete_envelope_merges_with_canonical_typed_event() -> None:
    run = SseRun(
        run_id="run-1",
        conversation_id=7,
        context_type="application",
        context_ref="app-1",
        mode="agent",
    )
    envelope = run.envelope("assistant_message", {"message": "hello"})

    encoded = encode_sse_event(
        AssistantMessageEvent(message="hello"),
        seq=1,
        run_id=run.run_id,
        envelope=envelope,
    )
    payload = json.loads(encoded.split("data: ", 1)[1].splitlines()[0])

    assert payload["run_id"] == "run-1"
    assert payload["conversation_id"] == 7
    assert payload["context_type"] == "application"
    assert payload["context_ref"] == "app-1"
    assert payload["mode"] == "agent"
    assert payload["seq"] == 1
    assert payload["event"] == "assistant_message"
    assert payload["data"] == {"message": "hello"}


def test_unneeded_transport_aliases_are_not_exported() -> None:
    import offerpilot.chat_transport as transport

    for name in (
        "runtime_event_sse_payload",
        "render_outcome_http",
        "outcome_to_http",
        "render_outcome_response",
        "outcome_to_http_payload",
        "render_event_sse",
        "event_to_sse_payload",
        "make_guarded_streaming_response",
        "prepared_streaming_response",
    ):
        assert not hasattr(transport, name)


@pytest.mark.parametrize("base_error", [KeyboardInterrupt(), SystemExit(7)])
def test_guarded_response_base_exception_cleans_and_rethrows(base_error: BaseException) -> None:
    lifecycle = PreparedLifecycle()
    calls = {"cleanup": 0}

    async def body():
        raise base_error
        yield b"unreachable"

    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_cleanup=lambda _reason: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )
    response = GuardedStreamingResponse(body(), guard)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(type(base_error)):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.COMPLETED
    assert lifecycle.completion_reason is CompletionReason.TRANSPORT_ABORTED
    assert calls["cleanup"] == 1


def test_guarded_response_receive_barrier_base_exception_aborts_and_rethrows() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"cleanup": 0}
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        on_cleanup=lambda _reason: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )
    response = GuardedStreamingResponse([], guard)

    async def receive() -> dict[str, object]:
        raise KeyboardInterrupt()

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.0"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.ABORTED
    assert calls["cleanup"] == 1


def test_guarded_response_receive_disconnect_runs_original_background_once() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"background": 0, "send": 0}

    def background() -> None:
        calls["background"] += 1

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        calls["send"] += 1

    response = GuardedStreamingResponse(
        [b"never"],
        PreparedStreamGuard(lifecycle=lifecycle),
        background=background,
    )
    asyncio.run(
        response(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.0"}},
            receive,
            send,
        )
    )

    assert calls == {"background": 1, "send": 0}
    assert lifecycle.state is PreparedLifecycleState.ABORTED


def test_guarded_response_missing_spec_version_uses_receive_barrier() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"send": 0}

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        calls["send"] += 1

    response = GuardedStreamingResponse([b"never"], PreparedStreamGuard(lifecycle=lifecycle))
    asyncio.run(
        response(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {}},
            receive,
            send,
        )
    )

    assert calls["send"] == 0
    assert lifecycle.state is PreparedLifecycleState.ABORTED


def test_guarded_response_missing_spec_version_waits_for_slow_first_receive() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"receive": 0, "body": 0}

    async def receive() -> dict[str, object]:
        calls["receive"] += 1
        await asyncio.sleep(0.01)
        if calls["receive"] == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            calls["body"] += 1

    response = GuardedStreamingResponse([b"hello"], PreparedStreamGuard(lifecycle=lifecycle))
    asyncio.run(
        response(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {}},
            receive,
            send,
        )
    )

    assert calls == {"receive": 2, "body": 1}
    assert lifecycle.state is PreparedLifecycleState.COMPLETED


def test_guarded_response_consumer_failure_maps_transport_aborted() -> None:
    lifecycle = PreparedLifecycle()
    guard = PreparedStreamGuard(lifecycle=lifecycle)
    response = GuardedStreamingResponse([], guard)

    async def receive() -> dict[str, object]:
        raise OSError("consumer failed")

    async def send(_message: dict[str, object]) -> None:
        return None

    with pytest.raises(RuntimeTransportAborted):
        asyncio.run(
            response(
                {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.0"}},
                receive,
                send,
            )
        )
    assert lifecycle.state is PreparedLifecycleState.ABORTED


def test_guard_cleanup_type_error_is_called_once_without_signature_retry() -> None:
    lifecycle = PreparedLifecycle()
    calls = {"cleanup": 0}

    def cleanup(_reason: CompletionReason | None) -> None:
        calls["cleanup"] += 1
        raise TypeError("callback body failure")

    guard = PreparedStreamGuard(lifecycle=lifecycle, on_cleanup=cleanup)
    assert guard.begin_execution() is True
    with pytest.raises(TypeError, match="callback body failure"):
        guard.complete(CompletionReason.NORMAL)
    assert calls["cleanup"] == 1
