from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from offerpilot.ai.agent_contracts import (
    AgentAssistantDelta,
    AgentToolCall,
    AgentToolResult,
    ChatRunCancelled,
    PendingAction,
)
from offerpilot.ai.agent_loop import (
    AgentLoopInvocation,
    AgentLoopRunner,
    ApprovedWriteSeed,
    NewTurnSeed,
)
from offerpilot.ai.tool_runtime.contracts import (
    ExecutionAuthorization,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.context_projector.contracts import ProjectionError

from .helpers import RecordingEventSink, ScriptedModel, ToolDefinition, runtime


def invocation(
    model: object,
    definitions: tuple[ToolDefinition, ...],
    *,
    seed: NewTurnSeed | ApprovedWriteSeed | None = None,
    event_sink: object | None = None,
    run_recorder: object | None = None,
    max_iterations: int = 8,
    auto_approve: bool = False,
    cancel_check: object | None = None,
) -> AgentLoopInvocation:
    catalog, context = runtime(*definitions)
    recorder = run_recorder or NullRunRecorder()
    context = replace(context, run_recorder=recorder)
    if isinstance(seed, ApprovedWriteSeed):
        context = replace(context, operation_executor=execute_operation)
    return AgentLoopInvocation(
        seed=seed or NewTurnSeed((Message(role="user", content="开始"),)),
        model=model,
        catalog=catalog,
        tool_context=context,
        auto_approve=auto_approve,
        max_iterations=max_iterations,
        run_recorder=recorder,
        event_sink=event_sink,
        runtime_signal_sink=None,
        cancel_check=cancel_check,
    )


def execute_operation(
    prepared: PreparedToolCall[Any, Any],
    context: object,
    authorization: ExecutionAuthorization,
) -> ToolExecutionRecord[Any, Any]:
    value = prepared.spec.executor(prepared.typed_args, context)
    return ToolExecutionRecord(
        prepared=prepared,
        outcome=ToolSuccess(value),
        execution_started=True,
        operation_id=authorization.operation_id,
        terminal_persisted=True,
        persisted_visible_result=str(value),
        persisted_transport={"status": "success", "result": str(value)},
    )


class RecordingJournal(NullRunRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[object] = []

    def capture_context(self, *_args: object, **_kwargs: object) -> str:
        return f"snapshot-{len(self.events) + 1}"

    def append_event(self, event: object) -> None:
        self.events.append(event)

    def fingerprint_model_id(self, value: str) -> str:
        return value


class StreamingModel:
    def stream_complete(
        self,
        messages: list[object],
        tools: list[object],
        on_delta: object,
    ) -> Assistant:
        del messages, tools
        assert callable(on_delta)
        on_delta("流式")
        on_delta("回复")
        return Assistant(content="流式回复")

    def complete(self, messages: list[object], tools: list[object]) -> Assistant:
        del messages, tools
        raise AssertionError("stream_complete should be preferred")


def test_new_turn_returns_final_from_one_explicit_model_step() -> None:
    model = ScriptedModel(Assistant(content="完成"))

    result = AgentLoopRunner().run(invocation(model, ()))

    assert result.reply == "完成"
    assert result.pending is None
    assert [(message.role, message.content) for message in result.added] == [
        ("assistant", "完成")
    ]
    assert model.calls == 1


def test_read_batch_runs_all_calls_in_provider_order() -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("read-1", "first", "{}"),
                ToolCall("read-2", "second", "{}"),
            ]
        ),
        Assistant(content="完成"),
    )
    definitions = (
        ToolDefinition("first", executor=lambda _args: executed.append("first") or "一"),
        ToolDefinition("second", executor=lambda _args: executed.append("second") or "二"),
    )

    result = AgentLoopRunner().run(invocation(model, definitions))

    assert executed == ["first", "second"]
    assert [message.tool_call_id for message in result.added if message.role == "tool"] == [
        "read-1",
        "read-2",
    ]
    assert result.reply == "完成"


def test_write_tool_pauses_before_execution() -> None:
    calls: list[str] = []
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("w1", "update_application_status", '{"id":1}')])
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition(
                    "update_application_status",
                    kind="write",
                    executor=lambda raw: calls.append(raw) or "written",
                ),
            ),
        )
    )

    assert result.reply == ""
    assert result.pending is not None
    assert result.pending.human == "update_application_status"
    assert calls == []
    assert result.added[-1].tool_calls[0].name == "update_application_status"


def test_pending_return_rechecks_active_after_confirmation_summary() -> None:
    cancelled = False
    descriptions = 0
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("w1", "write", "{}")])
    )
    base = invocation(
        model,
        (ToolDefinition("write", kind="write"),),
        cancel_check=lambda: cancelled,
    )
    spec = base.catalog.resolve("write")
    assert spec is not None

    def describe(_args: object) -> str:
        nonlocal cancelled, descriptions
        descriptions += 1
        if descriptions == 2:
            cancelled = True
        return "write"

    catalog = ToolCatalog(
        (replace(spec, confirmation_description=describe),),
        expected_names=("write",),
    )

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(replace(base, catalog=catalog))

    assert descriptions == 2
    assert model.calls == 1


@pytest.mark.parametrize(
    ("calls", "expected_ids"),
    [
        (
            [ToolCall("read-1", "read", "{}"), ToolCall("read-2", "read", "{}")],
            ["read-1", "read-2"],
        ),
        (
            [ToolCall("write-1", "write", "{}"), ToolCall("read-1", "read", "{}")],
            ["write-1"],
        ),
        (
            [ToolCall("read-1", "read", "{}"), ToolCall("write-1", "write", "{}")],
            ["read-1"],
        ),
        (
            [ToolCall("write-1", "write", "{}"), ToolCall("write-2", "write", "{}")],
            ["write-1"],
        ),
    ],
)
def test_multi_tool_call_selection_matches_baseline_matrix(
    calls: list[ToolCall], expected_ids: list[str]
) -> None:
    model = ScriptedModel(Assistant(tool_calls=calls), Assistant(content="完成"))
    definitions = (
        ToolDefinition("read"),
        ToolDefinition("write", kind="write"),
    )

    result = AgentLoopRunner().run(invocation(model, definitions))

    assistant = result.added[0]
    assert [call.id for call in assistant.tool_calls] == expected_ids


def test_executes_multiple_read_only_tool_calls_from_one_assistant_turn() -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("r1", "list_applications", "{}"),
                ToolCall("r2", "list_notes", '{"limit":3}'),
            ]
        ),
        Assistant(content="已汇总。"),
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition(
                    "list_applications", executor=lambda raw: executed.append("apps") or raw
                ),
                ToolDefinition("list_notes", executor=lambda raw: executed.append("notes") or raw),
            ),
        )
    )

    assert result.reply == "已汇总。"
    assert result.pending is None
    assert executed == ["apps", "notes"]
    assert [call.name for call in result.added[0].tool_calls] == [
        "list_applications",
        "list_notes",
    ]
    assert [message.tool_call_id for message in result.added if message.role == "tool"] == [
        "r1",
        "r2",
    ]


def test_failed_first_read_does_not_block_second_read_in_same_turn() -> None:
    executed: list[str] = []

    def fail(_raw: str) -> str:
        executed.append("first")
        raise ValueError("private read failure")

    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("first", "first_read", "{}"),
                ToolCall("second", "second_read", "{}"),
            ]
        ),
        Assistant(content="done"),
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition("first_read", executor=fail),
                ToolDefinition(
                    "second_read", executor=lambda raw: executed.append("second") or raw
                ),
            ),
        )
    )

    assert executed == ["first", "second"]
    assert [message.tool_call_id for message in result.added if message.role == "tool"] == [
        "first",
        "second",
    ]
    assert result.reply == "done"
    assert result.pending is None


def test_always_confirm_write_pauses_even_when_auto_approve_is_enabled() -> None:
    calls: list[str] = []
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("d1", "delete_note", '{"id":1}')])
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition(
                    "delete_note",
                    kind="write",
                    executor=lambda raw: calls.append(raw) or "deleted",
                ),
            ),
            auto_approve=True,
        )
    )

    assert result.reply == ""
    assert result.pending is not None
    assert result.pending.tool_name == "delete_note"
    assert calls == []


def test_write_never_auto_approves_and_suspends_without_executor() -> None:
    executed: list[str] = []
    model = ScriptedModel(Assistant(tool_calls=[ToolCall("write-1", "write", "{}")]))

    result = AgentLoopRunner().run(
        invocation(
            model,
            (ToolDefinition("write", kind="write", executor=lambda raw: executed.append(raw) or raw),),
            auto_approve=True,
        )
    )

    assert result.reply == ""
    assert result.pending is not None
    assert result.pending.tool_call_id == "write-1"
    assert result.pending.operation_id
    assert executed == []
    assert all(message.role != "tool" for message in result.added)


def test_invalid_non_object_write_args_emit_safe_summary_and_continue() -> None:
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("write-1", "write", "[]")]),
        Assistant(content="参数无效"),
    )
    sink = RecordingEventSink()

    result = AgentLoopRunner().run(
        invocation(
            model,
            (ToolDefinition("write", kind="write", executor=lambda raw: raw),),
            event_sink=sink,
        )
    )

    assert result.reply == "参数无效"
    assert isinstance(sink.events[0], AgentToolCall)
    assert dict(sink.events[0].args_summary) == {}


def test_event_sink_emits_assistant_delta_from_streaming_model() -> None:
    sink = RecordingEventSink()
    result = AgentLoopRunner().run(
        invocation(StreamingModel(), (), event_sink=sink)
    )

    assert result.reply == "流式回复"
    assert result.pending is None
    assert result.added[-1].content == "流式回复"
    assert [event.delta for event in sink.events if isinstance(event, AgentAssistantDelta)] == [
        "流式",
        "回复",
    ]


def test_cancellation_after_provider_response_drops_buffered_deltas() -> None:
    cancelled = False
    sink = RecordingEventSink()

    class CancellingStreamingModel:
        def stream_complete(
            self,
            messages: list[object],
            tools: list[object],
            on_delta: object,
        ) -> Assistant:
            nonlocal cancelled
            del messages, tools
            assert callable(on_delta)
            on_delta("不得发送")
            cancelled = True
            return Assistant(content="完成")

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            invocation(
                CancellingStreamingModel(),
                (),
                event_sink=sink,
                cancel_check=lambda: cancelled,
            )
        )

    assert sink.events == []


def test_journal_records_read_tool_loop_and_increments_model_step() -> None:
    recorder = RecordingJournal()
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
        Assistant(content="done"),
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (ToolDefinition("list_applications", executor=lambda _raw: "[]"),),
            run_recorder=recorder,
        )
    )

    assert result.reply == "done"
    assert result.pending is None
    event_types = [getattr(event, "event_type", "") for event in recorder.events]
    assert event_types == [
        "model.requested",
        "model.completed",
        "tool.proposed",
        "tool.started",
        "tool.completed",
        "model.requested",
        "model.completed",
    ]
    model_events = [event for event in recorder.events if getattr(event, "event_type", "").startswith("model.")]
    assert [getattr(event, "model_step", None) for event in model_events] == [1, 1, 2, 2]
    assert getattr(model_events[0], "model_call_id", None) == getattr(
        model_events[1], "model_call_id", None
    )
    assert getattr(model_events[2], "model_call_id", None) != getattr(
        model_events[0], "model_call_id", None
    )


class ApprovedPort:
    def __init__(self, pending: PendingAction, phases: list[str]) -> None:
        self._pending = pending
        self.phases = phases
        self.record: ToolExecutionRecord[Any, Any] | None = None

    @property
    def pending(self) -> PendingAction:
        self.phases.append("pending")
        return self._pending

    def claim(
        self,
        pending: PendingAction,
        prepared: PreparedToolCall[Any, Any],
    ) -> ExecutionAuthorization | ToolFailure:
        self.phases.append("claim")
        return ExecutionAuthorization(
            pending_identity=prepared.pending_identity,
            pending_action_revision=prepared.pending_action_revision or 0,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            arguments_digest=prepared.arguments_digest,
            operation_id=pending.operation_id,
        )

    def record_result(
        self,
        pending: PendingAction,
        tool_message: Message,
        record: ToolExecutionRecord[Any, Any],
    ) -> None:
        del pending, tool_message
        self.phases.append("record")
        self.record = record

    def load_continuation_messages(self) -> tuple[Message, ...]:
        self.phases.append("load")
        return (Message(role="user", content="继续"),)

    def delivery_fence(self) -> bool:
        return "claim" in self.phases


def test_approved_write_bootstraps_then_enters_same_model_loop() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction("write-1", "write", "{}", "确认", "operation-1")
    port = ApprovedPort(pending, phases)
    sink = RecordingEventSink()
    model = ScriptedModel(Assistant(content="写入完成"))
    base = invocation(
        model,
        (ToolDefinition("write", kind="write", executor=lambda raw: executed.append(raw) or "已写入"),),
        seed=ApprovedWriteSeed(port),
        event_sink=sink,
    )
    result = AgentLoopRunner().run(base)

    assert executed == ["{}"]
    assert phases == ["pending", "claim", "record", "load"]
    assert result.reply == "写入完成"
    assert [message.role for message in result.added] == ["tool", "assistant"]
    assert len(result.records) == 1
    assert [type(event) for event in sink.events[:2]] == [AgentToolCall, AgentToolResult]


def test_final_return_rechecks_delivery_fence_after_last_cancel_checkpoint() -> None:
    phases: list[str] = []
    pending = PendingAction("write-1", "write", "{}", "确认", "operation-1")

    class RevokedAtReturnPort(ApprovedPort):
        allowed = True

        def delivery_fence(self) -> bool:
            return self.allowed and super().delivery_fence()

    port = RevokedAtReturnPort(pending, phases)
    recorder = RecordingJournal()
    post_completion_checks = 0

    def cancel_check() -> bool:
        nonlocal post_completion_checks
        if any(getattr(event, "event_type", "") == "model.completed" for event in recorder.events):
            post_completion_checks += 1
            if post_completion_checks == 2:
                port.allowed = False
        return False

    base = invocation(
        ScriptedModel(Assistant(content="完成")),
        (ToolDefinition("write", kind="write"),),
        seed=ApprovedWriteSeed(port),
        run_recorder=recorder,
        cancel_check=cancel_check,
    )

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(base)

    assert post_completion_checks == 2
    assert port.allowed is False


def test_approved_claim_failure_stops_before_executor_and_provider() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction("write-1", "write", "{}", "确认", "operation-1")
    port = ApprovedPort(pending, phases)
    def lost_claim(_pending: PendingAction, _prepared: PreparedToolCall[Any, Any]) -> ToolFailure:
        phases.append("claim")
        return ToolFailure("stale_state", "confirmation_claim_lost")

    port.claim = lost_claim  # type: ignore[method-assign]
    model = ScriptedModel(Assistant(content="不应调用"))
    base = invocation(
        model,
        (ToolDefinition("write", kind="write", executor=lambda raw: executed.append(raw) or raw),),
        seed=ApprovedWriteSeed(port),
    )
    context = replace(base.tool_context, operation_executor=lambda *_args: pytest.fail("executor"))

    with pytest.raises(Exception, match="confirmation claim"):
        AgentLoopRunner().run(replace(base, tool_context=context))

    assert executed == []
    assert model.calls == 0


def test_cancellation_before_read_executor_is_fail_closed() -> None:
    cancelled = False
    executed: list[str] = []

    class CancellingModel(ScriptedModel):
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            nonlocal cancelled
            value = super().complete(messages, tools)
            cancelled = True
            return value

    model = CancellingModel(
        Assistant(tool_calls=[ToolCall("r1", "read", "{}")] ),
    )
    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            invocation(
                model,
                (ToolDefinition("read", executor=lambda raw: executed.append(raw) or raw),),
                cancel_check=lambda: cancelled,
            )
        )

    assert executed == []


def test_cancellation_after_read_executor_does_not_repeat_executor() -> None:
    cancelled = False
    executed: list[str] = []

    def execute(raw: str) -> str:
        nonlocal cancelled
        executed.append(raw)
        cancelled = True
        return raw

    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("r1", "read", "{}")] ),
        Assistant(content="never reached"),
    )
    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            invocation(
                model,
                (ToolDefinition("read", executor=execute),),
                cancel_check=lambda: cancelled,
            )
        )

    assert len(executed) == 1
    assert model.calls == 1


def test_delivery_fence_after_approved_executor_aborts_without_repeat() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction("write-1", "write", "{}", "确认", "operation-1")

    class RevokedPort(ApprovedPort):
        allowed = True

        def delivery_fence(self) -> bool:
            return self.allowed and super().delivery_fence()

    port = RevokedPort(pending, phases)
    model = ScriptedModel(Assistant(content="never reached"))
    base = invocation(
        model,
        (ToolDefinition("write", kind="write", executor=lambda raw: executed.append(raw) or raw),),
        seed=ApprovedWriteSeed(port),
    )

    def execute_and_revoke(
        prepared: PreparedToolCall[Any, Any],
        context: object,
        authorization: ExecutionAuthorization,
    ) -> ToolExecutionRecord[Any, Any]:
        port.allowed = False
        return execute_operation(prepared, context, authorization)

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            replace(base, tool_context=replace(base.tool_context, operation_executor=execute_and_revoke))
        )

    assert executed == ["{}"]
    assert model.calls == 0


def test_mixed_known_and_unknown_surface_tool_calls_fail_closed_before_events_or_executor() -> None:
    _, context = runtime()
    events = RecordingEventSink()
    executed: list[str] = []

    class MixedModel:
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            del messages, tools
            return Assistant(
                tool_calls=[
                    ToolCall("known", "list_offers", "{}"),
                    ToolCall("unknown", "not_exposed", "{}"),
                ]
            )

    invocation_value = AgentLoopInvocation(
        seed=NewTurnSeed((Message(role="user", content="offer"),)),
        model=MixedModel(),
        catalog=MODEL_TOOL_CATALOG,
        tool_context=context,
        auto_approve=False,
        max_iterations=2,
        run_recorder=NullRunRecorder(),
        event_sink=events,
        runtime_signal_sink=None,
        cancel_check=None,
    )

    from offerpilot.context_projector.contracts import ProjectionError

    with pytest.raises(ProjectionError, match="unknown_tool"):
        AgentLoopRunner().run(invocation_value)
    assert events.events == []
    assert executed == []


def test_partial_catalog_model_also_fails_closed_at_surface_before_dispatch() -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("known", "known_read", "{}"),
                ToolCall("unknown", "not_exposed", "{}"),
            ]
        )
    )
    events = RecordingEventSink()

    with pytest.raises(ProjectionError, match="unknown_tool"):
        AgentLoopRunner().run(
            invocation(
                model,
                (
                    ToolDefinition(
                        "known_read",
                        executor=lambda raw: executed.append(raw) or raw,
                    ),
                ),
                event_sink=events,
            )
        )

    assert events.events == []
    assert executed == []


def test_streaming_unknown_surface_tool_drops_buffered_delta_before_binding() -> None:
    executed: list[str] = []
    events = RecordingEventSink()

    class StreamingMixedModel:
        def stream_complete(
            self,
            messages: list[object],
            tools: list[object],
            on_delta: object,
        ) -> Assistant:
            del messages, tools
            assert callable(on_delta)
            on_delta("不得向外暴露")
            return Assistant(
                tool_calls=[
                    ToolCall("known", "known_read", "{}"),
                    ToolCall("unknown", "not_exposed", "{}"),
                ]
            )

    with pytest.raises(ProjectionError, match="unknown_tool"):
        AgentLoopRunner().run(
            invocation(
                StreamingMixedModel(),
                (
                    ToolDefinition(
                        "known_read",
                        executor=lambda raw: executed.append(raw) or raw,
                    ),
                ),
                event_sink=events,
            )
        )

    assert events.events == []
    assert executed == []
