from __future__ import annotations

import pickle
import json
from dataclasses import asdict, fields

import pytest

from offerpilot.ai.agent_contracts import (
    AgentAssistantDelta,
    AgentDriver,
    AgentToolCall,
    AgentToolResult,
    AgentTurnResult,
    ChatRunCancelled,
    PendingActionValidationError,
    PendingAction,
)
from offerpilot.ai.agent_loop import _InjectedSurfaceAdapter
from offerpilot.ai.agent_loop import (
    AgentLoopInvocation,
    AgentLoopRunner,
    ApprovedWriteSeed,
    NewTurnSeed,
)
from offerpilot.ai.types import Assistant
from offerpilot.ai.types import Message, ToolCall
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.context_projector.binding import BoundProviderResponse
from offerpilot.pilot_runtime.composition import _AgentDriver
from offerpilot.pilot_runtime.errors import RuntimeCancelled

from tests.agent_loop.helpers import ScriptedModel, runtime


def test_agent_driver_has_only_execute_method() -> None:
    methods = {
        name
        for name, value in vars(AgentDriver).items()
        if callable(value) and not name.startswith("_")
    }
    assert methods == {"execute"}


def test_pending_and_turn_result_keep_exact_compatibility_shape() -> None:
    pending = PendingAction("call-1", "write", "{}", "确认", "operation-1")
    message = Message(role="assistant", content="完成")
    result = AgentTurnResult([message], "完成", pending)

    assert tuple(result) == ([message], "完成", pending)
    assert pending.operation_id == "operation-1"


def test_agent_event_union_is_closed_and_typed() -> None:
    delta = AgentAssistantDelta("片段")
    call = AgentToolCall(
        tool_call_id="call-1",
        tool_name="lookup",
        public_label="查询",
        kind="read",
        confirm_mode="none",
        summary="查询",
        args_summary={},
    )
    result = AgentToolResult(
        tool_call_id="call-1",
        operation_id="",
        payload={"status": "success"},
    )

    assert delta.delta == "片段"
    assert call.kind == "read"
    assert result.payload["status"] == "success"
    assert {item.name for item in fields(call)} == {
        "tool_call_id",
        "tool_name",
        "public_label",
        "kind",
        "confirm_mode",
        "summary",
        "args_summary",
    }


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AgentToolCall("c", "t", "p", "invalid", "none", "s", {}),
        lambda: AgentToolCall("c", "t", "p", "read", "invalid", "s", {}),
        lambda: AgentToolResult("c", "", {"bad": object()}),
    ],
)
def test_agent_events_reject_open_or_non_json_values(factory) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


@pytest.mark.parametrize(
    "value",
    [
        AgentToolCall("c", "t", "p", "read", "none", "s", {"secret": "canary"}),
        AgentToolResult("c", "operation", {"status": "success"}),
    ],
)
def test_agent_event_values_are_not_pickle_checkpoints(value: object) -> None:
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(value)


@pytest.mark.parametrize(
    "value",
    [
        PendingAction("c", "t", '{"secret":"private"}', "private"),
        AgentAssistantDelta("private delta"),
        AgentToolCall("c", "t", "private", "read", "none", "private", {}),
        AgentToolResult("c", "operation", {"secret": "private"}),
        AgentTurnResult([Message(role="assistant", content="private")], "private", None),
    ],
)
def test_transient_dtos_fail_closed_for_generic_serializers(value: object) -> None:
    rendered = repr(value)
    assert "private" not in rendered
    with pytest.raises(TypeError):
        asdict(value)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(value)


def test_pending_action_rejects_non_string_private_values() -> None:
    with pytest.raises(TypeError, match="args must be a string"):
        PendingAction("c", "t", object(), "private")  # type: ignore[arg-type]


def test_seed_boundaries_reject_untyped_messages_and_incomplete_ports() -> None:
    with pytest.raises(TypeError, match="messages must contain Message"):
        NewTurnSeed((object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="continuation is incomplete"):
        ApprovedWriteSeed(object())  # type: ignore[arg-type]


def test_new_turn_seed_detaches_the_complete_message_graph() -> None:
    original = Message(
        role="user",
        content="before",
        tool_calls=[ToolCall("call-before", "tool-before", '{"before": true}')],
        tool_call_id="message-before",
        provider_blocks={"nested": {"values": ["before"]}},
        surface_contributor="conversation_history",
        surface_signal="before-signal",
        surface_revision="before-revision",
        surface_page_kind="before-page",
        surface_attachment_kinds="before-attachment",
    )

    seed = NewTurnSeed((original,))

    original.role = "assistant"
    original.content = "after"
    original.tool_calls[0].id = "call-after"
    original.tool_calls[0].name = "tool-after"
    original.tool_calls[0].args = '{"after": true}'
    original.tool_call_id = "message-after"
    original.provider_blocks["nested"]["values"].append("after")
    original.surface_contributor = "after-contributor"
    original.surface_signal = "after-signal"
    original.surface_revision = "after-revision"
    original.surface_page_kind = "after-page"
    original.surface_attachment_kinds = "after-attachment"

    snapshot = seed.messages[0]
    assert snapshot is not original
    assert snapshot.role == "user"
    assert snapshot.content == "before"
    assert snapshot.tool_calls[0].id == "call-before"
    assert snapshot.tool_calls[0].name == "tool-before"
    assert snapshot.tool_calls[0].args == '{"before": true}'
    assert snapshot.tool_call_id == "message-before"
    assert snapshot.provider_blocks == {"nested": {"values": ["before"]}}
    assert snapshot.surface_contributor == "conversation_history"
    assert snapshot.surface_signal == "before-signal"
    assert snapshot.surface_revision == "before-revision"
    assert snapshot.surface_page_kind == "before-page"
    assert snapshot.surface_attachment_kinds == "before-attachment"


def test_approved_write_seed_caches_and_validates_port_pending_once() -> None:
    class Port:
        def __init__(self) -> None:
            self.reads = 0
            self._pending = PendingAction("call-1", "write", "{}", "确认", "op-1")

        @property
        def pending(self) -> PendingAction:
            self.reads += 1
            return self._pending

        def claim(self, *_args: object) -> object:
            return object()

        def record_result(self, *_args: object) -> None:
            return None

        def load_continuation_messages(self) -> tuple[Message, ...]:
            return ()

        def delivery_fence(self) -> bool:
            return True

    port = Port()
    seed = ApprovedWriteSeed(port)  # type: ignore[arg-type]

    assert port.reads == 1
    assert seed.pending is port._pending
    assert port.reads == 1


@pytest.mark.parametrize(
    "pending",
    [
        PendingAction("", "write", "{}", "确认", "op-1"),
        PendingAction("call-1", "", "{}", "确认", "op-1"),
        PendingAction("call-1", "write", "{}", "确认", ""),
    ],
)
def test_approved_write_seed_rejects_incomplete_pending_identity(
    pending: PendingAction,
) -> None:
    class Port:
        @property
        def pending(self) -> PendingAction:
            return pending

        def claim(self, *_args: object) -> object:
            return object()

        def record_result(self, *_args: object) -> None:
            return None

        def load_continuation_messages(self) -> tuple[Message, ...]:
            return ()

        def delivery_fence(self) -> bool:
            return True

    with pytest.raises(PendingActionValidationError, match="identity"):
        ApprovedWriteSeed(Port())  # type: ignore[arg-type]


def test_injected_two_argument_model_remains_supported_behind_gateway() -> None:
    class Model:
        def complete(self, messages: object, tools: object) -> Assistant:
            del messages, tools
            return Assistant(content="ok")

    result = _InjectedSurfaceAdapter(Model())._complete(None, [], [], None)  # type: ignore[arg-type]

    assert result.content == "ok"


def test_composition_driver_maps_typed_chat_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, context = runtime()
    invocation = AgentLoopInvocation(
        seed=NewTurnSeed((Message(role="user", content="hello"),)),
        model=ScriptedModel(Assistant(content="unused")),
        catalog=catalog,
        tool_context=context,
        auto_approve=False,
        max_iterations=1,
        run_recorder=NullRunRecorder(),
        event_sink=None,
        runtime_signal_sink=None,
        cancel_check=None,
    )

    def cancel(_runner: AgentLoopRunner, _invocation: AgentLoopInvocation) -> AgentTurnResult:
        raise ChatRunCancelled("cancelled")

    monkeypatch.setattr(AgentLoopRunner, "run", cancel)

    with pytest.raises(RuntimeCancelled) as raised:
        _AgentDriver().execute(invocation)
    assert isinstance(raised.value.__cause__, ChatRunCancelled)


def test_bound_provider_attempt_identity_is_transient_and_private() -> None:
    attempt_canary = "provider-attempt-private-canary"
    response = BoundProviderResponse(
        "model-call",
        0,
        attempt_canary,
        "f" * 64,
        Assistant(content="ok"),
    )

    assert attempt_canary not in repr(response)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(response)
    with pytest.raises(TypeError, match="cannot be serialized"):
        asdict(response)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(response)
