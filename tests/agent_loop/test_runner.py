from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, NoReturn

import pytest

import offerpilot.ai.agent_loop as agent_loop_module
import offerpilot.ai.tool_runtime.pipeline as pipeline_module
import offerpilot.context_projector.selector as selector_module
from offerpilot.ai.agent_contracts import (
    AgentAssistantDelta,
    AgentToolCall,
    AgentToolResult,
    ChatRunCancelled,
    PendingAction,
)
from offerpilot.ai.agent_loop import (
    ApprovedContinuationSegment,
    AgentLoopInvocation,
    AgentLoopRunner,
    ApprovedWriteSeed,
    NewTurnSeed,
    build_segment_surface_gate,
    _pending_action_revision,
    _provider_arguments_digest,
)
from offerpilot.ai.tool_authority import AuthorityFactory, AuthorityPhaseError, TrustedContextScope
from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import ToolPresentationBindingV1
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.contracts import (
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    ToolResultMetadata,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.catalog import (
    SegmentToolCatalogLease,
    ToolCatalog,
    compile_tool_metadata_manifest,
)
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.config import AIProviderProfile
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.context_projector.gateway import (
    AgentProviderGatewaySession,
    FrozenProviderExecutionChain,
    SingleCandidateAgentTransport,
)
from offerpilot.context_projector.projector import ModelSurfaceProjector
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components

from .helpers import RecordingEventSink, ScriptedModel, ToolDefinition, runtime


class _PresentationProbe:
    def __init__(self) -> None:
        self.cancelled = False
        self.descriptions = 0

    def reset(self) -> None:
        self.cancelled = False
        self.descriptions = 0


_PRESENTATION_PROBE = _PresentationProbe()
_READ_ROUTE_DRIFT_SPEC: ToolSpec[Any, Any] | None = None
_READ_ROUTE_DRIFT_CALLS: list[str] = []


def _probe_confirmation_description(_args: object) -> str:
    _PRESENTATION_PROBE.descriptions += 1
    if _PRESENTATION_PROBE.descriptions == 2:
        _PRESENTATION_PROBE.cancelled = True
    return "write"


def _probe_pending_details(_args: object) -> dict[str, object]:
    return {}


def _probe_success_summary(result: object) -> str:
    return str(result)


def _probe_cancel_check() -> bool:
    return _PRESENTATION_PROBE.cancelled


def _drifted_read_renderer(_result: object) -> str:
    _READ_ROUTE_DRIFT_CALLS.append("success_renderer")
    return "drifted"


def _mutating_result_metadata_projector(_result: object) -> ToolResultMetadata:
    if _READ_ROUTE_DRIFT_SPEC is None:
        raise AssertionError("read route drift spec is not configured")
    object.__setattr__(_READ_ROUTE_DRIFT_SPEC, "success_renderer", _drifted_read_renderer)
    return ToolResultMetadata(changed_entities=({"kind": "mutated"},))


def _replacement_result_metadata_projector(_result: object) -> ToolResultMetadata:
    _READ_ROUTE_DRIFT_CALLS.append("result_metadata_projector")
    return ToolResultMetadata(changed_entities=({"kind": "replacement"},))


def _with_mutating_read_projector(catalog: ToolCatalog) -> ToolCatalog:
    global _READ_ROUTE_DRIFT_SPEC
    specs = tuple(
        replace(item, result_metadata_projector=_mutating_result_metadata_projector)
        if item.name == "list_applications"
        else item
        for item in catalog.specs
    )
    _READ_ROUTE_DRIFT_SPEC = next(item for item in specs if item.name == "list_applications")
    return ToolCatalog(specs, expected_names=tuple(item.name for item in specs))


def _capture_read_route_spec(catalog: ToolCatalog) -> ToolCatalog:
    global _READ_ROUTE_DRIFT_SPEC
    _READ_ROUTE_DRIFT_SPEC = next(
        item for item in catalog.specs if item.name == "list_applications"
    )
    return catalog


def _with_probe_presentation(catalog: ToolCatalog) -> ToolCatalog:
    specs = tuple(
        replace(
            item,
            presentation=ToolPresentationBindingV1(
                implementation_id="agent_loop_test_probe_presentation_v1",
                confirmation_description=_probe_confirmation_description,
                pending_details_projector=_probe_pending_details,
                success_summary_projector=_probe_success_summary,
            ),
        )
        if item.name == "update_application_status"
        else item
        for item in catalog.specs
    )
    return ToolCatalog(specs, expected_names=tuple(item.name for item in specs))


class _DelegatingRecorder:
    """Test equivalent of the Runtime's stable Segment recorder proxy."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def set_delegate(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


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
    catalog_transform: Callable[[ToolCatalog], ToolCatalog] | None = None,
) -> AgentLoopInvocation:
    catalog, context = runtime(*definitions)
    segment_origin = context
    if catalog_transform is not None:
        catalog = catalog_transform(catalog)
    recorder = run_recorder or NullRunRecorder()
    resolved_seed = seed or NewTurnSeed((Message(role="user", content="开始"),))
    bundle = _task9_metadata_bundle(catalog)
    catalog_lease = bundle.open_segment_lease()
    if isinstance(resolved_seed, ApprovedWriteSeed):
        observe_runtime = getattr(resolved_seed.continuation, "observe_runtime_bundle", None)
        if callable(observe_runtime):
            observe_runtime(bundle, catalog_lease)
        policy = validate_startup_policy(catalog.authority_manifest)
        pending = resolved_seed.pending
        revision = _pending_action_revision(
            pending.tool_call_id,
            pending.tool_name,
            pending.args,
        )
        digest = _provider_arguments_digest(pending.args)
        pending.bind_typed_proposal_identity(
            conversation_id=1,
            pending_action_revision=revision,
            pending_confirmation_claim_id="agent-loop-test-claim",
            arguments_digest=digest,
        )
        approval_factory = AuthorityFactory()
        approval_factory.register_pending(pending)
        authority = approval_factory.create_approval_authority(
            operation_id=pending.operation_id,
            conversation_id=1,
            conversation_scope_revision=0,
            trusted_scope=TrustedContextScope("workspace", None, "general"),
            pending_identity=pending,
            pending_action_revision=revision,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            effective_args_digest=digest,
            capabilities=frozenset(ToolCapability),
            capability_profile_id=policy.capability_profile.profile_id,
            capability_policy_version=policy.capability_policy_version,
            binding_policy_version=policy.binding_policy_version,
            capability_profile_fingerprint=policy.capability_profile_fingerprint,
            binding_policy_fingerprint=policy.binding_policy_fingerprint,
        )
        approval_factory.bind_segment_tool_catalog(
            authority,
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=catalog_lease,
        )
        context = ToolExecutionContext(
            authority=authority,
            applications=context.applications,
            events=context.events,
            jd_analyses=context.jd_analyses,
            notes=context.notes,
            offers=context.offers,
            resumes=context.resumes,
            run_recorder=recorder,
            operation_executor=execute_operation,
        )
        segment_catalog = catalog
        segment_context = segment_origin.with_runtime_dependencies(
            run_recorder=_DelegatingRecorder(recorder),
            operation_executor=None,
        )
        segment_messages = (
            Message(role="user", content="开始"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(pending.tool_call_id, pending.tool_name, pending.args)],
            ),
            Message(role="tool", content="已写入", tool_call_id=pending.tool_call_id),
            Message(role="user", content="继续"),
        )

        def activate_continuation() -> ApprovedContinuationSegment:
            continuation_lease = bundle.open_segment_lease()
            try:
                segment_gate, _segment_bundle = _task9_surface_gate(
                    segment_catalog,
                    segment_context,
                    segment_messages,
                    bundle=bundle,
                    lease=continuation_lease,
                )
                return ApprovedContinuationSegment(
                    messages=segment_messages,
                    model=_ContinuationModel(model),
                    catalog=segment_catalog,
                    tool_context=segment_context,
                    surface_gate=segment_gate,
                )
            except BaseException:
                continuation_lease.close()
                raise

        configure_segment = getattr(resolved_seed.continuation, "set_continuation_factory", None)
        if callable(configure_segment):
            configure_segment(activate_continuation)
    else:
        context = context.with_runtime_dependencies(
            run_recorder=recorder,
            operation_executor=None,
        )
    surface_gate = (
        _task9_surface_gate(
            catalog,
            context,
            tuple(resolved_seed.messages),
            bundle=bundle,
            lease=catalog_lease,
        )[0]
        if isinstance(resolved_seed, NewTurnSeed)
        else None
    )
    try:
        return AgentLoopInvocation(
            seed=resolved_seed,
            model=model,
            catalog=catalog,
            catalog_lease=catalog_lease,  # type: ignore[call-arg]
            tool_context=context,
            auto_approve=auto_approve,
            max_iterations=max_iterations,
            run_recorder=recorder,
            event_sink=event_sink,
            runtime_signal_sink=None,
            cancel_check=cancel_check,
            surface_gate=surface_gate,
        )
    except BaseException:
        catalog_lease.close()
        raise


def execute_operation(
    prepared: PreparedToolCall[Any, Any],
    context: object,
    _prepare_identity: object,
) -> ToolExecutionRecord[Any, Any]:
    value = prepared.spec.executor(prepared.typed_args, context)
    operation_id = getattr(getattr(context, "authority", None), "operation_id", "operation-1")
    return ToolExecutionRecord(
        prepared=prepared,
        outcome=ToolSuccess(value),
        execution_started=True,
        operation_id=operation_id,
        terminal_persisted=True,
        persisted_visible_result=str(value),
        persisted_transport={"status": "success", "result": str(value)},
    )


class RecordingJournal(NullRunRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[object] = []

    def capture_surface_context(self, *_args: object, **_kwargs: object) -> str:
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


class _ContinuationModel:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    def complete(self, messages: list[object], tools: list[object]) -> Assistant:
        complete = getattr(self._inner, "complete")
        return complete(messages, tools)


def test_new_turn_returns_final_from_one_explicit_model_step() -> None:
    model = ScriptedModel(Assistant(content="完成"))

    result = AgentLoopRunner().run(invocation(model, ()))

    assert result.reply == "完成"
    assert result.pending is None
    assert [(message.role, message.content) for message in result.added] == [("assistant", "完成")]
    assert model.calls == 1


def test_read_batch_runs_all_calls_in_provider_order() -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("read-1", "list_applications", "{}"),
                ToolCall("read-2", "list_notes", "{}"),
            ]
        ),
        Assistant(content="完成"),
    )
    definitions = (
        ToolDefinition(
            "list_applications", executor=lambda _args: executed.append("first") or "一"
        ),
        ToolDefinition("list_notes", executor=lambda _args: executed.append("second") or "二"),
    )

    base = invocation(model, definitions)
    authority = base.tool_context.authority
    assert base.surface_gate is not None
    assert base.surface_gate.authority is authority
    result = AgentLoopRunner().run(base)

    assert executed == ["first", "second"]
    assert [message.tool_call_id for message in result.added if message.role == "tool"] == [
        "read-1",
        "read-2",
    ]
    assert result.reply == "完成"
    assert model.calls == 2
    assert base.tool_context.authority is authority
    assert base.surface_gate.authority is authority


def test_write_tool_pauses_before_execution() -> None:
    calls: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[ToolCall("w1", "update_application_status", '{"id":1,"status":"applied"}')]
        )
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
    _PRESENTATION_PROBE.reset()
    model = ScriptedModel(
        Assistant(
            tool_calls=[ToolCall("w1", "update_application_status", '{"id":1,"status":"applied"}')]
        )
    )

    base = invocation(
        model,
        (ToolDefinition("update_application_status", kind="write"),),
        cancel_check=_probe_cancel_check,
        catalog_transform=_with_probe_presentation,
    )

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(base)

    assert _PRESENTATION_PROBE.descriptions == 2
    assert model.calls == 1
    _PRESENTATION_PROBE.reset()


@pytest.mark.parametrize(
    ("calls", "expected_ids"),
    [
        (
            [
                ToolCall("read-1", "list_applications", "{}"),
                ToolCall("read-2", "list_applications", "{}"),
            ],
            ["read-1", "read-2"],
        ),
        (
            [
                ToolCall("write-1", "update_application_status", '{"id":1,"status":"applied"}'),
                ToolCall("read-1", "list_applications", "{}"),
            ],
            ["write-1"],
        ),
        (
            [
                ToolCall("read-1", "list_applications", "{}"),
                ToolCall("write-1", "update_application_status", '{"id":1,"status":"applied"}'),
            ],
            ["read-1"],
        ),
        (
            [
                ToolCall("write-1", "update_application_status", '{"id":1,"status":"applied"}'),
                ToolCall("write-2", "update_application_status", '{"id":2,"status":"applied"}'),
            ],
            ["write-1"],
        ),
    ],
)
def test_multi_tool_call_selection_matches_baseline_matrix(
    calls: list[ToolCall], expected_ids: list[str]
) -> None:
    model = ScriptedModel(Assistant(tool_calls=calls), Assistant(content="完成"))
    definitions = (
        ToolDefinition("list_applications"),
        ToolDefinition("update_application_status", kind="write"),
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
                ToolCall("first", "list_applications", "{}"),
                ToolCall("second", "list_notes", "{}"),
            ]
        ),
        Assistant(content="done"),
    )
    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition("list_applications", executor=fail),
                ToolDefinition("list_notes", executor=lambda raw: executed.append("second") or raw),
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
    model = ScriptedModel(Assistant(tool_calls=[ToolCall("d1", "delete_note", '{"id":1}')]))
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
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("write-1", "update_application_status", '{"id":1,"status":"applied"}')
            ]
        )
    )

    result = AgentLoopRunner().run(
        invocation(
            model,
            (
                ToolDefinition(
                    "update_application_status",
                    kind="write",
                    executor=lambda raw: executed.append(raw) or raw,
                ),
            ),
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
        Assistant(tool_calls=[ToolCall("write-1", "update_application_status", "[]")]),
        Assistant(content="参数无效"),
    )
    sink = RecordingEventSink()

    result = AgentLoopRunner().run(
        invocation(
            model,
            (ToolDefinition("update_application_status", kind="write", executor=lambda raw: raw),),
            event_sink=sink,
        )
    )

    assert result.reply == "参数无效"
    assert isinstance(sink.events[0], AgentToolCall)
    assert dict(sink.events[0].args_summary) == {}


def test_event_sink_emits_assistant_delta_from_streaming_model() -> None:
    sink = RecordingEventSink()
    result = AgentLoopRunner().run(invocation(StreamingModel(), (), event_sink=sink))

    assert result.reply == "流式回复"
    assert result.pending is None
    assert result.added[-1].content == "流式回复"
    assert [event.delta for event in sink.events if isinstance(event, AgentAssistantDelta)] == [
        "流式",
        "回复",
    ]


def test_runner_event_sink_override_receives_read_tool_events() -> None:
    invocation_sink = RecordingEventSink()
    runner_sink = RecordingEventSink()
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
        Assistant(content="done"),
    )

    result = AgentLoopRunner().run(
        invocation(
            model,
            (ToolDefinition("list_applications", executor=lambda _raw: "[]"),),
            event_sink=invocation_sink,
        ),
        event_sink=runner_sink,
    )

    assert result.reply == "done"
    assert any(isinstance(event, AgentToolCall) for event in runner_sink.events)
    assert any(isinstance(event, AgentToolResult) for event in runner_sink.events)
    assert invocation_sink.events == []


def test_read_transport_projector_drift_fails_before_result_event() -> None:
    sink = RecordingEventSink()
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
        Assistant(content="must not continue"),
    )
    _READ_ROUTE_DRIFT_CALLS.clear()
    try:
        with pytest.raises(AuthorityPhaseError):
            AgentLoopRunner().run(
                invocation(
                    model,
                    (ToolDefinition("list_applications", executor=lambda _raw: "[]"),),
                    event_sink=sink,
                    catalog_transform=_with_mutating_read_projector,
                )
            )

        assert not any(isinstance(event, AgentToolResult) for event in sink.events)
        assert _READ_ROUTE_DRIFT_CALLS == []
        assert model.calls == 1
    finally:
        if _READ_ROUTE_DRIFT_SPEC is not None:
            object.__setattr__(_READ_ROUTE_DRIFT_SPEC, "success_renderer", str)
        _READ_ROUTE_DRIFT_CALLS.clear()


def test_read_result_event_mutation_fails_before_tool_message_or_next_model_call() -> None:
    class MutatingEventSink(RecordingEventSink):
        def emit(self, event: object) -> None:
            super().emit(event)
            if isinstance(event, AgentToolResult):
                if _READ_ROUTE_DRIFT_SPEC is None:
                    raise AssertionError("read route drift spec is not configured")
                object.__setattr__(
                    _READ_ROUTE_DRIFT_SPEC,
                    "result_metadata_projector",
                    _replacement_result_metadata_projector,
                )

    sink = MutatingEventSink()
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
        Assistant(content="must not continue"),
    )
    _READ_ROUTE_DRIFT_CALLS.clear()
    try:
        with pytest.raises(AuthorityPhaseError):
            AgentLoopRunner().run(
                invocation(
                    model,
                    (ToolDefinition("list_applications", executor=lambda _raw: "[]"),),
                    event_sink=sink,
                    catalog_transform=_capture_read_route_spec,
                )
            )

        assert sum(isinstance(event, AgentToolResult) for event in sink.events) == 1
        assert _READ_ROUTE_DRIFT_CALLS == []
        assert model.calls == 1
    finally:
        if _READ_ROUTE_DRIFT_SPEC is not None:
            object.__setattr__(_READ_ROUTE_DRIFT_SPEC, "result_metadata_projector", None)
        _READ_ROUTE_DRIFT_CALLS.clear()


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
    model_events = [
        event for event in recorder.events if getattr(event, "event_type", "").startswith("model.")
    ]
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
        self._segment: ApprovedContinuationSegment | None = None
        self._continuation_factory: Callable[[], ApprovedContinuationSegment] | None = None
        self._bundle: ToolMetadataBundleV1 | None = None
        self._approval_lease: SegmentToolCatalogLease | None = None

    @property
    def pending(self) -> PendingAction:
        self.phases.append("pending")
        return self._pending

    def claim(
        self,
        pending: PendingAction,
        prepared: PreparedToolCall[Any, Any],
    ) -> ToolFailure | None:
        self.phases.append("claim")
        return None

    def record_result(
        self,
        pending: PendingAction,
        tool_message: Message,
        record: ToolExecutionRecord[Any, Any],
    ) -> None:
        del pending, tool_message
        self.phases.append("record")
        self.record = record

    def set_continuation_segment(self, segment: ApprovedContinuationSegment) -> None:
        self._segment = segment

    def set_continuation_factory(
        self,
        factory: Callable[[], ApprovedContinuationSegment],
    ) -> None:
        self._continuation_factory = factory

    def observe_runtime_bundle(
        self,
        bundle: ToolMetadataBundleV1,
        approval_lease: SegmentToolCatalogLease,
    ) -> None:
        self._bundle = bundle
        self._approval_lease = approval_lease

    def activate_continuation_segment(self) -> ApprovedContinuationSegment:
        self.phases.append("activate")
        if self._segment is None and self._continuation_factory is not None:
            self._segment = self._continuation_factory()
        if self._segment is None:
            raise AssertionError("continuation segment was not configured")
        return self._segment

    def delivery_fence(self) -> bool:
        return "claim" in self.phases


def test_approval_authority_switches_to_fresh_segment_model_loop() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )
    port = ApprovedPort(pending, phases)
    sink = RecordingEventSink()
    model = ScriptedModel(Assistant(content="写入完成"))
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or "已写入",
            ),
        ),
        seed=ApprovedWriteSeed(port),
        event_sink=sink,
    )
    result = AgentLoopRunner().run(base)

    assert executed == ['{"id":1,"status":"applied"}']
    assert phases == ["pending", "claim", "record", "activate"]
    assert [type(event) for event in sink.events[:2]] == [AgentToolCall, AgentToolResult]
    assert result.reply == "写入完成"
    assert model.calls == 1
    assert [
        message.tool_call_id
        for message in model.inputs[0]
        if getattr(message, "role", None) == "tool"
    ] == ["write-1"]


def test_approved_continuation_rejects_a_replacement_catalog() -> None:
    phases: list[str] = []
    pending = PendingAction(
        "write-1",
        "update_application_status",
        '{"id":1,"status":"applied"}',
        "确认",
        "operation-1",
    )
    port = ApprovedPort(pending, phases)
    continuation_model = ScriptedModel(Assistant(content="不应调用"))
    base = invocation(
        continuation_model,
        (ToolDefinition("update_application_status", kind="write"),),
        seed=ApprovedWriteSeed(port),
    )
    continuation_factory = port._continuation_factory
    assert continuation_factory is not None
    replacement = ToolCatalog(
        tuple(base.catalog.specs),
        expected_names=tuple(spec.name for spec in base.catalog.specs),
    )

    def replacement_segment() -> ApprovedContinuationSegment:
        segment = continuation_factory()
        try:
            return replace(segment, catalog=replacement)
        except BaseException:
            segment.surface_gate.catalog_lease.close()  # type: ignore[attr-defined]
            raise

    port.set_continuation_factory(replacement_segment)

    with pytest.raises(ProjectionError, match="[Bb]undle catalog"):
        AgentLoopRunner().run(base)

    assert continuation_model.calls == 0


def test_approved_activation_failure_does_not_repeat_origin_executor() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )

    class FailingPort(ApprovedPort):
        def activate_continuation_segment(self) -> ApprovedContinuationSegment:
            self.phases.append("activate")
            raise RuntimeError("segment activation failed")

    port = FailingPort(pending, phases)
    model = ScriptedModel(Assistant(content="不应调用"))
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or "已写入",
            ),
        ),
        seed=ApprovedWriteSeed(port),
    )

    with pytest.raises(RuntimeError, match="segment activation failed"):
        AgentLoopRunner().run(base)

    assert executed == ['{"id":1,"status":"applied"}']
    assert phases == ["pending", "claim", "record", "activate"]
    assert model.calls == 0


def test_active_segment_rechecks_origin_delivery_fence_after_provider_returns() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )

    class RevocablePort(ApprovedPort):
        allowed = True

        def delivery_fence(self) -> bool:
            return self.allowed and super().delivery_fence()

    port = RevocablePort(pending, phases)

    class FencedAfterProviderModel(ScriptedModel):
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            port.allowed = False
            return super().complete(messages, tools)

    model = FencedAfterProviderModel(Assistant(content="不应提交"))
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or "已写入",
            ),
        ),
        seed=ApprovedWriteSeed(port),
    )

    with pytest.raises(ChatRunCancelled, match="delivery owner fenced"):
        AgentLoopRunner().run(base)

    assert executed == ['{"id":1,"status":"applied"}']
    assert model.calls == 1


def test_provider_failure_after_approval_does_not_repeat_origin_executor() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )
    port = ApprovedPort(pending, phases)

    class FailingProviderModel(ScriptedModel):
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            self.calls += 1
            raise RuntimeError("provider failed")

    model = FailingProviderModel()
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or "已写入",
            ),
        ),
        seed=ApprovedWriteSeed(port),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        AgentLoopRunner().run(base)

    assert executed == ['{"id":1,"status":"applied"}']
    assert model.calls == 1


def test_active_segment_provider_input_is_detached_from_source_bundle() -> None:
    phases: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )
    port = ApprovedPort(pending, phases)

    class MutatingSourceModel(ScriptedModel):
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            assert port._segment is not None
            port._segment.messages[0].content = "mutated source"  # type: ignore[misc]
            return super().complete(messages, tools)

    model = MutatingSourceModel(Assistant(content="完成"))
    base = invocation(
        model,
        (ToolDefinition("update_application_status", kind="write"),),
        seed=ApprovedWriteSeed(port),
    )

    result = AgentLoopRunner().run(base)

    assert result.reply == "完成"
    detached_source = next(message for message in model.inputs[0] if message.content == "开始")
    assert detached_source is not port._segment.messages[0]
    assert detached_source.content == "开始"
    assert port._segment.messages[0].content == "mutated source"


def test_live_approval_delivery_fence_closes_before_fresh_provider() -> None:
    phases: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )

    class RevokedAtReturnPort(ApprovedPort):
        allowed = True

        def delivery_fence(self) -> bool:
            return self.allowed and super().delivery_fence()

    port = RevokedAtReturnPort(pending, phases)
    model = ScriptedModel(Assistant(content="不应调用"))
    base = invocation(
        model,
        (ToolDefinition("update_application_status", kind="write"),),
        seed=ApprovedWriteSeed(port),
    )

    result = AgentLoopRunner().run(base)

    assert result.reply == "不应调用"
    assert model.calls == 1
    assert port.allowed is True


def test_approval_authority_cannot_be_reused_as_a_new_turn_segment() -> None:
    phases: list[str] = []
    pending = PendingAction(
        "write-1",
        "update_application_status",
        '{"id":1,"status":"applied"}',
        "确认",
        "operation-1",
    )
    base = invocation(
        ScriptedModel(Assistant(content="不应调用")),
        (ToolDefinition("update_application_status", kind="write"),),
        seed=ApprovedWriteSeed(ApprovedPort(pending, phases)),
    )

    with pytest.raises(TypeError, match="NewTurnSeed requires Segment authority"):
        replace(base, seed=NewTurnSeed((Message(role="user", content="continue"),)))


def test_approved_claim_failure_stops_before_executor_and_provider() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )
    port = ApprovedPort(pending, phases)

    def lost_claim(_pending: PendingAction, _prepared: PreparedToolCall[Any, Any]) -> ToolFailure:
        phases.append("claim")
        return ToolFailure("stale_state", "confirmation_claim_lost")

    port.claim = lost_claim  # type: ignore[method-assign]
    model = ScriptedModel(Assistant(content="不应调用"))
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or raw,
            ),
        ),
        seed=ApprovedWriteSeed(port),
    )
    context = base.tool_context.with_runtime_dependencies(
        run_recorder=base.run_recorder,
        operation_executor=lambda *_args: pytest.fail("executor"),
    )

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
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
    )
    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            invocation(
                model,
                (
                    ToolDefinition(
                        "list_applications", executor=lambda raw: executed.append(raw) or raw
                    ),
                ),
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
        Assistant(tool_calls=[ToolCall("r1", "list_applications", "{}")]),
        Assistant(content="never reached"),
    )
    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            invocation(
                model,
                (ToolDefinition("list_applications", executor=execute),),
                cancel_check=lambda: cancelled,
            )
        )

    assert len(executed) == 1
    assert model.calls == 1


def test_delivery_fence_after_approved_executor_aborts_without_repeat() -> None:
    phases: list[str] = []
    executed: list[str] = []
    pending = PendingAction(
        "write-1", "update_application_status", '{"id":1,"status":"applied"}', "确认", "operation-1"
    )

    class RevokedPort(ApprovedPort):
        allowed = True

        def delivery_fence(self) -> bool:
            return self.allowed and super().delivery_fence()

    port = RevokedPort(pending, phases)
    model = ScriptedModel(Assistant(content="never reached"))
    base = invocation(
        model,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or raw,
            ),
        ),
        seed=ApprovedWriteSeed(port),
    )

    def execute_and_revoke(
        prepared: PreparedToolCall[Any, Any],
        context: object,
        prepare_identity: object,
    ) -> ToolExecutionRecord[Any, Any]:
        port.allowed = False
        return execute_operation(prepared, context, prepare_identity)

    with pytest.raises(ChatRunCancelled):
        AgentLoopRunner().run(
            replace(
                base,
                tool_context=base.tool_context.with_runtime_dependencies(
                    run_recorder=base.run_recorder,
                    operation_executor=execute_and_revoke,
                ),
            )
        )

    assert executed == ['{"id":1,"status":"applied"}']
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

    seed = NewTurnSeed((Message(role="user", content="offer"),))
    bundle = _task9_metadata_bundle(MODEL_TOOL_CATALOG)
    lease = bundle.open_segment_lease()
    invocation_value = AgentLoopInvocation(
        seed=seed,
        model=MixedModel(),
        catalog=MODEL_TOOL_CATALOG,
        catalog_lease=lease,  # type: ignore[call-arg]
        tool_context=context,
        auto_approve=False,
        max_iterations=2,
        run_recorder=NullRunRecorder(),
        event_sink=events,
        runtime_signal_sink=None,
        cancel_check=None,
        surface_gate=_task9_surface_gate(
            MODEL_TOOL_CATALOG,
            context,
            seed.messages,
            bundle=bundle,
            lease=lease,
        )[0],
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
                ToolCall("known", "list_offers", "{}"),
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
                        "list_offers",
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
                    ToolCall("known", "list_offers", "{}"),
                    ToolCall("unknown", "not_exposed", "{}"),
                ]
            )

    with pytest.raises(ProjectionError, match="unknown_tool"):
        AgentLoopRunner().run(
            invocation(
                StreamingMixedModel(),
                (
                    ToolDefinition(
                        "list_offers",
                        executor=lambda raw: executed.append(raw) or raw,
                    ),
                ),
                event_sink=events,
            )
        )

    assert events.events == []
    assert executed == []


def _task9_metadata_bundle(catalog: ToolCatalog) -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(catalog.specs)
    projection = manifest.to_dict()
    return ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=projection["legacy_boundary"],  # type: ignore[arg-type]
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )


def _task9_surface_gate(
    catalog: ToolCatalog,
    context: ToolExecutionContext,
    messages: tuple[Message, ...],
    *,
    bundle: ToolMetadataBundleV1,
    lease: SegmentToolCatalogLease,
) -> tuple[object, ToolMetadataBundleV1]:
    gate = build_segment_surface_gate(
        messages,
        catalog=catalog,
        catalog_lease=lease,  # type: ignore[call-arg]
        context=context,
        authority=context.authority,
        provider_view=bundle.provider_view(),
        discovery_view=bundle.discovery_view(),
        authority_metadata_view=bundle.authority_view(),
        policy=validate_startup_policy(catalog.authority_manifest),
    )
    return gate, bundle


def _task10_new_turn_invocation(
    model: object,
    definitions: tuple[ToolDefinition, ...] = (),
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[AgentLoopInvocation, ToolMetadataBundleV1, SegmentToolCatalogLease]:
    """Build the wished-for Task 10 invocation without a compatibility path."""

    catalog, context = runtime(*definitions)
    bundle = _task9_metadata_bundle(catalog)
    lease = bundle.open_segment_lease()
    messages = (Message(role="user", content="offer", surface_page_kind="offers"),)
    gate = build_segment_surface_gate(
        messages,
        catalog=catalog,
        catalog_lease=lease,  # type: ignore[call-arg]
        context=context,
        authority=context.authority,
        provider_view=bundle.provider_view(),
        discovery_view=bundle.discovery_view(),
        authority_metadata_view=bundle.authority_view(),
        policy=validate_startup_policy(catalog.authority_manifest),
    )
    return (
        AgentLoopInvocation(
            seed=NewTurnSeed(messages),
            model=model,  # type: ignore[arg-type]
            catalog=catalog,
            catalog_lease=lease,  # type: ignore[call-arg]
            tool_context=context,
            auto_approve=False,
            max_iterations=4,
            run_recorder=NullRunRecorder(),
            event_sink=None,
            runtime_signal_sink=None,
            cancel_check=cancel_check,
            surface_gate=gate,
        ),
        bundle,
        lease,
    )


def _task10_approved_invocation(
    model: object,
    port: ApprovedPort,
    definitions: tuple[ToolDefinition, ...],
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[
    AgentLoopInvocation,
    ToolMetadataBundleV1,
    SegmentToolCatalogLease,
]:
    """Give Approval a lease; continuation opens lazily during activation."""

    base = invocation(
        model,
        definitions,
        seed=ApprovedWriteSeed(port),
        cancel_check=cancel_check,
    )
    approval_lease = base.catalog_lease  # type: ignore[attr-defined]
    assert type(approval_lease) is SegmentToolCatalogLease
    bundle = port._bundle
    assert type(bundle) is ToolMetadataBundleV1
    return base, bundle, approval_lease


def test_one_segment_lease_resolves_each_provider_call_once_and_threads_exact_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(
            tool_calls=[
                ToolCall("offer-1", "list_offers", "{}"),
                ToolCall("offer-2", "list_offers", "{}"),
            ]
        ),
        Assistant(content="完成"),
    )
    invocation_value, bundle, lease = _task10_new_turn_invocation(
        model,
        (
            ToolDefinition(
                "list_offers",
                executor=lambda raw: executed.append(raw) or raw,
            ),
        ),
    )
    resolved: list[object] = []
    required: list[object] = []
    original_resolve = SegmentToolCatalogLease.resolve
    original_require = SegmentToolCatalogLease.require_spec

    def resolve_once(self: SegmentToolCatalogLease, name: str) -> object:
        handle = original_resolve(self, name)
        if self is lease:
            resolved.append(handle)
        return handle

    def require_exact(self: SegmentToolCatalogLease, handle: object) -> object:
        if self is lease:
            required.append(handle)
        return original_require(self, handle)

    monkeypatch.setattr(SegmentToolCatalogLease, "resolve", resolve_once)
    monkeypatch.setattr(SegmentToolCatalogLease, "require_spec", require_exact)

    result = AgentLoopRunner().run(invocation_value)

    assert result.reply == "完成"
    assert executed == ["{}", "{}"]
    assert lease.generation == 1
    assert lease.bundle_instance_token is bundle.bundle_instance_token
    assert [getattr(handle, "tool_name", None) for handle in resolved] == [
        "list_offers",
        "list_offers",
    ]
    assert resolved[0] is not resolved[1]
    assert required
    assert all(any(handle is issued for issued in resolved) for handle in required)
    assert all(sum(handle is issued for handle in required) >= 2 for issued in resolved)
    assert lease.closed is True


class _Task10Abort(BaseException):
    pass


@pytest.mark.parametrize("outcome", ("success", "exception", "base_exception", "cancel", "pending"))
def test_new_turn_segment_lease_closes_in_every_runner_exit(outcome: str) -> None:
    cancelled = False

    class ExitModel:
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            nonlocal cancelled
            del messages, tools
            if outcome == "exception":
                raise RuntimeError("provider failed")
            if outcome == "base_exception":
                raise _Task10Abort("provider aborted")
            if outcome == "cancel":
                cancelled = True
            if outcome == "pending":
                return Assistant(
                    tool_calls=[
                        ToolCall(
                            "write-1",
                            "update_application_status",
                            '{"id":1,"status":"applied"}',
                        )
                    ]
                )
            return Assistant(content="完成")

    invocation_value, bundle, lease = _task10_new_turn_invocation(
        ExitModel(),
        (ToolDefinition("update_application_status", kind="write"),)
        if outcome == "pending"
        else (),
        cancel_check=lambda: cancelled,
    )

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="provider failed"):
            AgentLoopRunner().run(invocation_value)
    elif outcome == "base_exception":
        with pytest.raises(_Task10Abort, match="provider aborted"):
            AgentLoopRunner().run(invocation_value)
    elif outcome == "cancel":
        with pytest.raises(ChatRunCancelled):
            AgentLoopRunner().run(invocation_value)
    elif outcome == "pending":
        assert AgentLoopRunner().run(invocation_value).pending is not None
    else:
        assert AgentLoopRunner().run(invocation_value).reply == "完成"

    assert lease.bundle_instance_token is bundle.bundle_instance_token
    assert lease.closed is True


def test_runner_lease_handoff_blocks_runtime_backstop_until_runner_exit() -> None:
    invocation_value, _bundle, lease = _task10_new_turn_invocation(
        ScriptedModel(Assistant(content="完成")),
        (),
    )
    releases = 0

    def release_once() -> None:
        nonlocal releases
        releases += 1
        lease.close()

    invocation_value._bind_catalog_release(release_once)
    invocation_value._claim_catalog_lease()

    assert invocation_value._release_catalog_lease_from_runtime() is False
    assert releases == 0
    assert lease.closed is False

    assert invocation_value._release_catalog_lease_from_runner() is True
    assert invocation_value._release_catalog_lease_from_runtime() is False
    assert releases == 1
    assert lease.closed is True


@pytest.mark.parametrize("outcome", ("success", "exception", "base_exception", "cancel"))
def test_approval_and_continuation_use_distinct_same_bundle_leases_and_close(
    outcome: str,
) -> None:
    phases: list[str] = []
    executed: list[str] = []
    cancelled = False
    pending = PendingAction(
        "write-1",
        "update_application_status",
        '{"id":1,"status":"applied"}',
        "确认",
        "operation-1",
    )
    port = ApprovedPort(pending, phases)

    class ContinuationExitModel:
        def complete(self, messages: list[object], tools: list[object]) -> Assistant:
            nonlocal cancelled
            del messages, tools
            if outcome == "exception":
                raise RuntimeError("continuation failed")
            if outcome == "base_exception":
                raise _Task10Abort("continuation aborted")
            if outcome == "cancel":
                cancelled = True
            assert approval_lease.closed is True
            return Assistant(content="完成")

    invocation_value, bundle, approval_lease = _task10_approved_invocation(
        ContinuationExitModel(),
        port,
        (
            ToolDefinition(
                "update_application_status",
                kind="write",
                executor=lambda raw: executed.append(raw) or "已写入",
            ),
        ),
        cancel_check=lambda: cancelled,
    )

    if outcome == "exception":
        with pytest.raises(RuntimeError, match="continuation failed"):
            AgentLoopRunner().run(invocation_value)
    elif outcome == "base_exception":
        with pytest.raises(_Task10Abort, match="continuation aborted"):
            AgentLoopRunner().run(invocation_value)
    elif outcome == "cancel":
        with pytest.raises(ChatRunCancelled):
            AgentLoopRunner().run(invocation_value)
    else:
        assert AgentLoopRunner().run(invocation_value).reply == "完成"

    assert port._segment is not None
    continuation_lease = port._segment.surface_gate.catalog_lease  # type: ignore[attr-defined]
    assert type(continuation_lease) is SegmentToolCatalogLease
    assert executed == ['{"id":1,"status":"applied"}']
    assert approval_lease is not continuation_lease
    assert approval_lease.segment_catalog_token is not continuation_lease.segment_catalog_token
    assert approval_lease.bundle_instance_token is continuation_lease.bundle_instance_token
    assert approval_lease.bundle_instance_token is bundle.bundle_instance_token
    assert (approval_lease.generation, continuation_lease.generation) == (1, 2)
    assert approval_lease.closed is True
    assert continuation_lease.closed is True
    later_next_turn_lease = bundle.open_segment_lease()
    try:
        assert later_next_turn_lease.generation == 3
        assert later_next_turn_lease.segment_catalog_token not in {
            approval_lease.segment_catalog_token,
            continuation_lease.segment_catalog_token,
        }
    finally:
        later_next_turn_lease.close()


def test_visible_provider_tool_still_requires_pipeline_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []
    model = ScriptedModel(
        Assistant(tool_calls=[ToolCall("offer-1", "list_offers", "{}")]),
        Assistant(content="完成"),
    )
    invocation_value, _bundle, lease = _task10_new_turn_invocation(
        model,
        (
            ToolDefinition(
                "list_offers",
                executor=lambda raw: executed.append(raw) or raw,
            ),
        ),
    )
    assert invocation_value.surface_gate is not None
    assert "list_offers" in invocation_value.surface_gate.selection.selected_names
    authorization_checks: list[str] = []

    def deny_visible_tool(entry: object, _context: object) -> ToolFailure:
        authorization_checks.append(str(getattr(entry, "provider_name", "")))
        return ToolFailure("permission_denied", "missing_capability", "permission denied")

    monkeypatch.setattr(pipeline_module, "_require_entry_capabilities", deny_visible_tool)

    result = AgentLoopRunner().run(invocation_value)

    assert result.reply == "完成"
    assert authorization_checks == ["list_offers"]
    assert executed == []
    assert [failure.code for failure in result.failures] == ["missing_capability"]
    assert lease.closed is True


def test_segment_surface_gate_consumes_same_bundle_views_and_selector_result_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, context = runtime()
    bundle = _task9_metadata_bundle(catalog)
    lease = bundle.open_segment_lease()
    provider_view = bundle.provider_view()
    discovery_view = bundle.discovery_view()
    authority_view = bundle.authority_view()
    signals = selector_module.ToolSelectionSignals(
        page_kind="offers",
        current_request="比较 offer",
    )
    selection_type = getattr(selector_module, "ToolSelectionResult", None)
    assert selection_type is not None, "Task 9 must publish the final ToolSelectionResult"
    expected = selector_module.select_tools(discovery_view, authority_view, signals)
    assert type(expected) is selection_type
    selector_calls: list[tuple[object, object, object]] = []

    def select_once(
        actual_discovery: object,
        actual_authority: object,
        actual_signals: object,
    ) -> object:
        selector_calls.append((actual_discovery, actual_authority, actual_signals))
        return expected

    def forbidden_catalog_rebuild(_self: object) -> NoReturn:
        raise AssertionError("Agent Loop must consume ToolSelectionResult provider contracts")

    monkeypatch.setattr(agent_loop_module, "select_tools", select_once)
    monkeypatch.setattr(ToolCatalog, "provider_contracts", forbidden_catalog_rebuild)
    messages = (
        Message(
            role="user",
            content="比较 offer",
            surface_page_kind="offers",
        ),
    )
    gate = build_segment_surface_gate(
        messages,
        catalog=catalog,
        catalog_lease=lease,  # type: ignore[call-arg]
        context=context,
        authority=context.authority,
        provider_view=provider_view,
        discovery_view=discovery_view,
        authority_metadata_view=authority_view,
        policy=validate_startup_policy(catalog.authority_manifest),
    )

    assert selector_calls == [(discovery_view, authority_view, signals)]
    assert gate.provider_view is provider_view
    assert gate.discovery_view is discovery_view
    assert gate.authority_metadata_view is authority_view
    assert gate.selection is expected
    assert gate.selection.provider_contracts is expected.provider_contracts
    assert gate.selection.provider_envelope_fingerprint == expected.provider_envelope_fingerprint
    assert provider_view.bundle_instance_token is discovery_view.bundle_instance_token
    assert discovery_view.bundle_instance_token is authority_view.bundle_instance_token


def test_unexposed_tool_fails_before_dispatcher_with_bundle_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, context = runtime(
        ToolDefinition("delete_note", kind="write", executor=lambda raw: raw),
    )
    messages = (Message(role="user", content="比较 offer", surface_page_kind="offers"),)
    bundle = _task9_metadata_bundle(catalog)
    lease = bundle.open_segment_lease()
    gate, _bundle = _task9_surface_gate(
        catalog,
        context,
        messages,
        bundle=bundle,
        lease=lease,
    )
    dispatcher_calls: list[str] = []

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> NoReturn:
        dispatcher_calls.append("prepare_call")
        raise AssertionError("an unexposed tool reached Dispatcher")

    def forbidden_catalog_rebuild(_self: object) -> NoReturn:
        raise AssertionError("Agent Loop rebuilt Provider contracts from Catalog")

    monkeypatch.setattr(agent_loop_module, "prepare_call", forbidden_dispatch)
    monkeypatch.setattr(ToolCatalog, "provider_contracts", forbidden_catalog_rebuild)
    invocation_value = AgentLoopInvocation(
        seed=NewTurnSeed(messages),
        model=ScriptedModel(
            Assistant(tool_calls=[ToolCall("hidden", "delete_note", '{"id":1}')]),
        ),
        catalog=catalog,
        catalog_lease=lease,  # type: ignore[call-arg]
        tool_context=context,
        auto_approve=False,
        max_iterations=2,
        run_recorder=NullRunRecorder(),
        event_sink=None,
        runtime_signal_sink=None,
        cancel_check=None,
        surface_gate=gate,
    )

    with pytest.raises(ProjectionError, match="unknown_tool|not_exposed|surface"):
        AgentLoopRunner().run(invocation_value)
    assert dispatcher_calls == []


def test_same_model_call_fallback_reuses_one_frozen_provider_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, context = runtime()
    messages = (Message(role="user", content="比较 offer", surface_page_kind="offers"),)
    bundle = _task9_metadata_bundle(catalog)
    lease = bundle.open_segment_lease()
    gate, bundle = _task9_surface_gate(
        catalog,
        context,
        messages,
        bundle=bundle,
        lease=lease,
    )
    expected_contracts = gate.selection.provider_contracts
    attempted_tools: list[tuple[object, ...]] = []
    attempted_candidates: list[str] = []
    attempted_model_call_ids: list[str] = []
    projected_surfaces: list[object] = []
    chain = FrozenProviderExecutionChain.freeze(
        [
            AIProviderProfile(id="primary", api_key="a", base_url="https://a.test/v1"),
            AIProviderProfile(id="fallback", api_key="b", base_url="https://b.test/v1"),
        ]
    )

    def complete(*_args: object) -> Assistant:
        raise AssertionError("Agent Loop uses the deferred stream boundary")

    def stream(
        candidate: object,
        _messages: object,
        tools: object,
        _emit: object,
    ) -> Assistant:
        assert isinstance(tools, list)
        assert projected_surfaces
        attempted_candidates.append(str(getattr(candidate, "provider_id")))
        attempted_tools.append(tuple(tools))
        attempted_model_call_ids.append(str(getattr(projected_surfaces[-1], "model_call_id")))
        if len(attempted_candidates) == 1:
            raise ConnectionError("primary unavailable")
        return Assistant(content="fallback success")

    transport = SingleCandidateAgentTransport(complete, stream)

    class FallbackSurfaceModel:
        def new_agent_provider_session(self) -> AgentProviderGatewaySession:
            return AgentProviderGatewaySession(chain, transport)

        def complete_agent_surface(self, *_args: object, **_kwargs: object) -> NoReturn:
            raise AssertionError("the per-call gateway wrapper must own completion")

        def stream_agent_surface(self, *_args: object, **_kwargs: object) -> NoReturn:
            raise AssertionError("the per-call gateway wrapper must own streaming")

    project_calls: list[object] = []
    original_project = ModelSurfaceProjector.project

    def project_once(self: object, request: object) -> object:
        project_calls.append(request)
        surface = original_project(self, request)  # type: ignore[arg-type]
        projected_surfaces.append(surface)
        return surface

    monkeypatch.setattr(ModelSurfaceProjector, "project", project_once)
    invocation_value = AgentLoopInvocation(
        seed=NewTurnSeed(messages),
        model=FallbackSurfaceModel(),  # type: ignore[arg-type]
        catalog=catalog,
        catalog_lease=lease,  # type: ignore[call-arg]
        tool_context=context,
        auto_approve=False,
        max_iterations=2,
        run_recorder=NullRunRecorder(),
        event_sink=None,
        runtime_signal_sink=None,
        cancel_check=None,
        surface_gate=gate,
    )
    result = AgentLoopRunner().run(invocation_value)

    assert result.reply == "fallback success"
    assert attempted_candidates == ["primary", "fallback"]
    assert len(project_calls) == 1
    assert len(projected_surfaces) == 1
    assert attempted_model_call_ids == [
        projected_surfaces[0].model_call_id,  # type: ignore[attr-defined]
        projected_surfaces[0].model_call_id,  # type: ignore[attr-defined]
    ]
    assert project_calls[0].model_call_id == projected_surfaces[0].model_call_id  # type: ignore[attr-defined]
    assert len(attempted_tools) == 2
    assert all(
        len(tools) == len(expected_contracts)
        and all(actual is expected for actual, expected in zip(tools, expected_contracts))
        for tools in attempted_tools
    )
    assert lease.closed is True
