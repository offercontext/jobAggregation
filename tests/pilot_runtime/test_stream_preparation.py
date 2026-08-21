from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

from offerpilot.chat_transport import PreparedStreamGuard, SseAgentExecutionHost
from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CompletedEvent,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    MetaEvent,
    PreparationKind,
    PreparedStreamExecution,
    RuntimeTransportContext,
    StartTurnRequest,
    StatusEvent,
    StreamExecutionMode,
    UserMessageSavedEvent,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.persistence import PersistenceResult, PersistenceStatus
from offerpilot.pilot_runtime.service import (
    PilotRuntime,
    ResolvedModel,
    RuntimeDependencies,
    _PreparedExecutionCell,
    _PreparedStreamState,
)


class Phases:
    def __init__(self) -> None:
        self.items: list[str] = []

    def append(self, value: str) -> None:
        self.items.append(value)


@dataclass
class Conversation:
    id: int = 7
    context_type: str = "workspace"
    context_ref: str = ""
    mode: str = "general"
    archived_at: object | None = None


class Conversations:
    def __init__(self, value: Conversation | None = None) -> None:
        self.value = value or Conversation()

    def create(self, request: object) -> Conversation:
        del request
        return self.value

    def load(self, conversation_id: int) -> Conversation | None:
        del conversation_id
        return self.value


class Persistence:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.next_id = 1
        self.user_count = 0
        self.assistant_count = 0
        self.pending: object | None = None
        self.tool_count = 0

    def _persist(self, role: str) -> int:
        message_id = self.next_id
        self.next_id += 1
        self.messages.append(SimpleNamespace(id=message_id, role=role))
        return message_id

    def get_pending_action(self, conversation_id: int) -> object | None:
        del conversation_id
        return self.pending

    def get_pending_clarification(self, conversation_id: int) -> None:
        del conversation_id
        return None

    def list_messages(self, conversation_id: int) -> tuple[object, ...]:
        del conversation_id
        return tuple(self.messages)

    def persist_initial_user_message(self, conversation_id: int, content: str) -> PersistenceResult:
        del conversation_id, content
        self.user_count += 1
        return PersistenceResult(
            PersistenceStatus.PERSISTED,
            message_count=1,
            message_id=self._persist("user"),
        )

    def persist_initial_messages(self, conversation_id: int, messages: object) -> PersistenceResult:
        del conversation_id
        values = tuple(messages) if isinstance(messages, (tuple, list)) else ()
        ids = tuple(self._persist(getattr(item, "role", "assistant")) for item in values)
        self.assistant_count += len(ids)
        return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=ids)

    def persist_initial_pending(self, conversation_id: int, messages: object, pending: object) -> PersistenceResult:
        self.pending = pending
        return self.persist_initial_messages(conversation_id, messages)

    def persist_initial_assistant_message(self, conversation_id: int, content: str, **kwargs: object) -> PersistenceResult:
        del conversation_id, content, kwargs
        self.assistant_count += 1
        return PersistenceResult(PersistenceStatus.PERSISTED, message_id=self._persist("assistant"))

    def persist_assistant_message(self, conversation_id: int, content: str, **kwargs: object) -> PersistenceResult:
        return self.persist_initial_assistant_message(conversation_id, content, **kwargs)

    def persist_clarification(self, conversation_id: int, messages: object, pending: object, question: str) -> PersistenceResult:
        del question
        self.pending = pending
        return self.persist_initial_messages(conversation_id, messages)

    def set_pending_clarification(self, conversation_id: int, pending: object, question: str) -> PersistenceResult:
        del conversation_id, question
        self.pending = pending
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def clear_pending_action(self, conversation_id: int) -> PersistenceResult:
        del conversation_id
        self.pending = None
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def clear_pending_clarification(self, conversation_id: int) -> PersistenceResult:
        del conversation_id
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def persist_timeout_assistant(self, conversation_id: int, content: str) -> PersistenceResult:
        return self.persist_initial_assistant_message(conversation_id, content)


class Source:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def load(self, conversation: object, request: object) -> list[str]:
        del conversation, request
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ["frozen-source"]


class Assembler:
    def assemble(self, source: object, conversation: object, request: object) -> list[str]:
        del source, conversation, request
        return ["frozen-context"]


class Driver:
    def __init__(self) -> None:
        self.calls = 0

    def run_turn(self, model: object, messages: object, **kwargs: object) -> object:
        del model, messages, kwargs
        self.calls += 1
        return SimpleNamespace(added=[], reply="hello", pending=None)


class Host:
    def __init__(self) -> None:
        self.calls = 0
        self.queue_count = 0

    def run(self, thunk: object, control: object) -> object:
        del control
        self.calls += 1
        assert callable(thunk)
        return thunk()


class Recorder:
    def __init__(self) -> None:
        self.abandoned = 0
        self.finished: list[object] = []

    def append_event(self, event: object) -> None:
        del event

    def capture_context(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def finish(self, value: object) -> None:
        self.finished.append(value)

    def abandon(self) -> None:
        self.abandoned += 1


class Journal:
    def __init__(self) -> None:
        self.recorder = Recorder()

    def start_run(self, builder: object) -> Recorder:
        del builder
        return self.recorder


def runtime(
    phases: Phases,
    *,
    source: Source | None = None,
    route: str = "model",
    conversation: Conversation | None = None,
    model: object = "model",
) -> tuple[PilotRuntime, Persistence, Driver, Host, Journal]:
    persistence = Persistence()
    driver = Driver()
    host = Host()
    journal = Journal()

    def resolve(request: object, conversation: object) -> object:
        del request, conversation
        return None if model is None else ResolvedModel(model=model)

    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(conversation),
            persistence=persistence,
            model_resolver=resolve,
            source_loader=source or Source(),
            context_assembler=Assembler(),
            agent_driver=driver,
            journal=journal,
            route_selector=lambda request, conversation: route,
            phase_sink=phases,
        )
    )
    return instance, persistence, driver, host, journal


def transport() -> RuntimeTransportContext:
    return RuntimeTransportContext(mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1")


def test_stream_model_preparation_order_and_source_failure_boundary() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases, source=Source(error=RuntimeError("no")))
    control = InMemoryRuntimeInvocationControl()

    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.status_code == 503
    assert prepared.payload["error_code"] == "source_load_failed"
    assert phases.items == [
        "validate", "conversation", "route:model", "pending_guard", "model_resolve",
        "user_persist", "source_load",
    ]
    assert persistence.user_count == 1
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []
    assert control.state is InvocationState.COMPLETED


def test_stream_model_prepare_and_agent_host_execution_emits_baseline_prefix() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)

    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.preparation_kind is PreparationKind.MODEL
    assert prepared.execution_mode is StreamExecutionMode.AGENT_HOST
    assert phases.items == [
        "validate", "conversation", "route:model", "pending_guard", "model_resolve",
        "user_persist", "source_load", "context_assemble", "transport_identity",
        "run_start", "context_capture", "prepared",
    ]
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    result = instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    assert isinstance(result, MessageOutcome)
    assert result.message == "hello"
    assert persistence.user_count == 1
    assert persistence.assistant_count == 1
    assert driver.calls == 1
    assert host.calls == 1
    assert [type(event) for event in seen[:3]] == [MetaEvent, UserMessageSavedEvent, StatusEvent]
    assert isinstance(seen[-1], CompletedEvent)
    assert control.state is InvocationState.COMPLETED
    assert journal.recorder.finished


def test_prepare_rejects_deterministic_route_before_user_or_run() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases, route="deterministic")
    control = InMemoryRuntimeInvocationControl()

    result = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)

    assert isinstance(result, ImmediateHttpOutcome)
    assert result.status_code == 400
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []
    assert control.state is InvocationState.COMPLETED


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        (PreparationKind.DETERMINISTIC_INITIAL, StreamExecutionMode.DIRECT),
        (PreparationKind.DETERMINISTIC_CONFIRMATION, StreamExecutionMode.DIRECT),
        (PreparationKind.CONFIRMATION, StreamExecutionMode.DIRECT),
        (PreparationKind.REPLAY, StreamExecutionMode.DIRECT),
    ],
)
def test_direct_prepared_execution_has_no_agent_host_and_is_single_use(
    kind: PreparationKind,
    mode: StreamExecutionMode,
) -> None:
    phases = Phases()
    instance, _persistence, driver, host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    outcome = MessageOutcome(message="already committed", conversation_id=7)
    stream_transport = transport()
    state = _PreparedStreamState(
        owner_token=instance._owner_token,  # type: ignore[attr-defined]
        preparation_kind=kind,
        execution_mode=mode,
        control=control,
        request=StartTurnRequest(message="hi"),
        conversation=None,
        cell=_PreparedExecutionCell(run_open=False),
        events=(MetaEvent(), AssistantMessageEvent(message="already committed")),
        outcome=outcome,
    )
    prepared = PreparedStreamExecution(
        invocation_id=stream_transport.transport_run_id,
        preparation_kind=kind,
        execution_mode=mode,
        opaque_state=state,
    )
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    assert instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    ) == outcome
    assert host.calls == 0
    assert driver.calls == 0
    assert seen == [MetaEvent(), AssistantMessageEvent(message="already committed"), CompletedEvent(response=outcome)]
    assert instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    ) == outcome
    assert host.calls == 0


def test_model_abort_keeps_user_and_abandons_open_run_without_new_facts() -> None:
    phases = Phases()
    instance, persistence, _driver, _host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)

    assert guard.abort_if_prepared() is True
    assert prepared.lifecycle_state.value == "aborted"
    assert persistence.user_count == 1
    assert persistence.assistant_count == 0
    assert persistence.pending is None
    assert journal.recorder.abandoned == 1
    assert guard.abort_if_prepared() is False


def test_model_prepared_stream_adapts_sse_host_queue_once() -> None:
    phases = Phases()
    instance, _persistence, driver, _unused_host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)
    assert isinstance(prepared, PreparedStreamExecution)
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    result = instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=SseAgentExecutionHost(timeout_seconds=1.0),
        cancel_check=lambda: False,
    )
    assert isinstance(result, MessageOutcome)
    assert driver.calls == 1
    assert [type(item) for item in seen[:3]] == [MetaEvent, UserMessageSavedEvent, StatusEvent]
    assert isinstance(seen[-1], CompletedEvent)
