from __future__ import annotations

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
from offerpilot.chat_transport import (
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
