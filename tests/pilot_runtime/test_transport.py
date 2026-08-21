from __future__ import annotations

import asyncio

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CompletionReason,
    CompletedEvent,
    MessageOutcome,
    PreparedLifecycle,
    PreparedLifecycleState,
    RuntimeFailureOutcome,
    RuntimeFailureCode,
)
from offerpilot.pilot_runtime.errors import RuntimeTransportAborted
from offerpilot.chat_transport import (
    build_guarded_streaming_response,
    GuardedStreamingResponse,
    PreparedStreamGuard,
    event_sse_payload,
    outcome_http_payload,
    outcome_http_response,
)


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
