from __future__ import annotations

import gc
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from offerpilot.chat_transport import PreparedStreamGuard, SseAgentExecutionHost
from offerpilot.ai.agent import PendingAction
from offerpilot.agent_runtime.journal import NullRunRecorder, RunRecorderFactory
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.db import init_database
from offerpilot.pilot_runtime.persistence import ChatPersistenceCoordinator
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.pilot_runtime.errors import RuntimeCancelled, RuntimeTransportAborted
from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CompletedEvent,
    CompletionReason,
    ConfirmationRequiredEvent,
    ErrorEvent,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    MetaEvent,
    PreparationKind,
    PreparedStreamExecution,
    PilotActionDescriptor,
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
        self.create_calls = 0

    def create(self, request: object) -> Conversation:
        del request
        self.create_calls += 1
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
    def __init__(self, value: object | None = None) -> None:
        self.value = value

    def assemble(self, source: object, conversation: object, request: object) -> list[str]:
        del source, conversation, request
        if self.value is not None:
            return self.value  # type: ignore[return-value]
        return ["frozen-context"]


class Driver:
    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None
        self.result: object | None = None

    def run_turn(self, model: object, messages: object, **kwargs: object) -> object:
        del model, messages, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
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
    catalog: object | None = None,
    assembled: object | None = None,
) -> tuple[PilotRuntime, Persistence, Driver, Host, Journal]:
    persistence = Persistence()
    driver = Driver()
    host = Host()
    journal = Journal()

    def resolve(request: object, conversation: object) -> object:
        del request, conversation
        return None if model is None else ResolvedModel(model=model, catalog=catalog)

    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(conversation),
            persistence=persistence,
            model_resolver=resolve,
            source_loader=source or Source(),
            context_assembler=Assembler(assembled),
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
    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]
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

    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
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
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


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


def test_deterministic_pilot_action_is_rejected_before_conversation_side_effects() -> None:
    phases = Phases()
    conversations = Conversations()
    persistence = Persistence()
    driver = Driver()
    journal = Journal()
    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=conversations,
            persistence=persistence,
            model_resolver=lambda request, conversation: ResolvedModel(model="model"),
            source_loader=Source(),
            context_assembler=Assembler(),
            agent_driver=driver,
            journal=journal,
            phase_sink=phases,
        )
    )
    control = InMemoryRuntimeInvocationControl()
    result = instance.prepare_stream(
        StartTurnRequest(
            message="run deterministic",
            pilot_action=PilotActionDescriptor(kind="create_application"),
        ),
        transport=transport(),
        invocation_control=control,
    )

    assert isinstance(result, ImmediateHttpOutcome)
    assert result.status_code == 400
    assert conversations.create_calls == 0
    assert persistence.user_count == 0
    assert driver.calls == 0
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

    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
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
    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=Sink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert host.calls == 0


def test_model_abort_keeps_user_and_abandons_open_run_without_new_facts() -> None:
    phases = Phases()
    instance, persistence, _driver, _host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)

    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]
    assert guard.abort_if_prepared() is True
    assert prepared.lifecycle_state.value == "aborted"
    assert persistence.user_count == 1
    assert persistence.assistant_count == 0
    assert persistence.pending is None
    assert journal.recorder.abandoned == 1
    assert guard.abort_if_prepared() is False
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


def test_model_prepared_stream_adapts_sse_host_queue_once() -> None:
    phases = Phases()
    instance, _persistence, driver, _unused_host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(StartTurnRequest(message="hi"), transport=transport(), invocation_control=control)
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
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


def test_real_stream_run_recorder_keeps_transport_uuid_and_terminal_events(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path
    sessions = init_database(data_dir / "offerpilot.db")
    chat = ChatRepository(sessions)
    conversation = chat.create_conversation("real stream journal")
    persistence = ChatPersistenceCoordinator(chat)
    repository = AgentRunRepository(sessions)
    key = JournalKeyDomain("00000000-0000-0000-0000-000000000003", b"s" * 32)

    class CapturingFactory(RunRecorderFactory):
        recorder: object | None = None

        def start_run(self, command: object) -> object:
            self.recorder = super().start_run(command)  # type: ignore[arg-type]
            return self.recorder

    journal = CapturingFactory(repository, key=key, enabled=True)

    class Gateway:
        def create(self, request: object) -> object:
            del request
            return conversation

        def load(self, conversation_id: int) -> object:
            assert conversation_id == conversation.id
            return conversation

    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Gateway(),
            persistence=persistence,
            model_resolver=lambda request, current: ResolvedModel(model="model"),
            source_loader=Source(),
            context_assembler=Assembler(),
            agent_driver=Driver(),
            journal=journal,
        )
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    result = instance.execute_prepared_stream(
        prepared,
        event_sink=None,
        signal_sink=None,
        execution_host=Host(),
        cancel_check=lambda: False,
    )

    assert isinstance(result, MessageOutcome)
    assert journal.recorder is not None
    run_id = getattr(journal.recorder, "run_id")
    assert isinstance(run_id, str)
    events = repository.list_events(run_id)
    assert events
    assert {event.event_type for event in events} >= {
        "route.selected",
        "context.captured",
        "assistant.persisted",
        "segment.finished",
    }


def test_null_journal_recorder_does_not_mark_prepared_run_open() -> None:
    phases = Phases()
    instance, _persistence, _driver, _host, _journal = runtime(phases)

    class NullJournal:
        def start_run(self, builder: object) -> NullRunRecorder:
            del builder
            return NullRunRecorder(["journal_disabled"])

    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=Persistence(),
            model_resolver=lambda request, conversation: ResolvedModel(model="model"),
            source_loader=Source(),
            context_assembler=Assembler(),
            agent_driver=Driver(),
            journal=NullJournal(),
        )
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert getattr(prepared.opaque_state, "journal_started") is False
    assert getattr(prepared.opaque_state, "cell").run_open is False


def test_execute_prepared_stream_requires_guard_begin_and_does_not_self_start() -> None:
    phases = Phases()
    instance, _persistence, driver, host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=Sink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert prepared.lifecycle_state.value == "prepared"
    assert seen == []
    assert driver.calls == 0
    assert host.calls == 0


def test_sink_abort_after_recorder_finish_does_not_abandon_finished_run() -> None:
    phases = Phases()
    instance, _persistence, _driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True

    class FailingSink:
        def emit(self, event: object) -> None:
            if isinstance(event, CompletedEvent):
                raise RuntimeTransportAborted()

    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=FailingSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert journal.recorder.finished
    assert journal.recorder.abandoned == 0
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert prepared.lifecycle_state.value == "completed"
    assert prepared.completion_reason is CompletionReason.TRANSPORT_ABORTED
    second_seen: list[object] = []

    class SecondSink:
        def emit(self, event: object) -> None:
            second_seen.append(event)

    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=SecondSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert second_seen == []


def test_terminal_abort_releases_provider_token_and_canary_exactly_once() -> None:
    class Provider:
        pass

    provider = Provider()
    provider_ref = weakref.ref(provider)

    class Resolver:
        def __init__(self, model: object) -> None:
            self.model = model

        def resolve(self, request: object, conversation: object) -> object:
            del request, conversation
            return ResolvedModel(model=self.model)

    resolver = Resolver(provider)
    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=Persistence(),
            model_resolver=resolver,
            source_loader=Source(),
            context_assembler=Assembler(),
            agent_driver=Driver(),
            journal=Journal(),
        )
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]
    resolver.model = None
    del provider
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True

    class FailingSink:
        def emit(self, event: object) -> None:
            if isinstance(event, CompletedEvent):
                raise RuntimeTransportAborted()

    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=FailingSink(),
            signal_sink=None,
            execution_host=Host(),
            cancel_check=lambda: False,
        )
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    gc.collect()
    assert provider_ref() is None
    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=None,
            signal_sink=None,
            execution_host=Host(),
            cancel_check=lambda: False,
        )
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (RuntimeCancelled(), CompletionReason.CANCELLED),
        (RuntimeTransportAborted(), CompletionReason.TRANSPORT_ABORTED),
        (KeyboardInterrupt(), CompletionReason.TRANSPORT_ABORTED),
    ],
)
def test_preterminal_cancel_abort_and_baseexception_release_model_token_once(
    raised: BaseException,
    reason: CompletionReason,
) -> None:
    phases = Phases()
    instance, _persistence, _driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True

    class FailingSink:
        def emit(self, event: object) -> None:
            del event
            raise raised

    with pytest.raises(type(raised)):
        instance.execute_prepared_stream(
            prepared,
            event_sink=FailingSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert prepared.completion_reason is reason
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert journal.recorder.abandoned == 1
    assert guard.abort_if_prepared() is False
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


def test_stream_meta_supports_delta_reflects_resolved_stream_model() -> None:
    class StreamingModel:
        def stream_complete(self, messages: object, tools: object, on_delta: object) -> object:
            del messages, tools, on_delta
            return None

    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(
        phases,
        model=StreamingModel(),
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    assert seen[0] == MetaEvent(supports_delta=True)


def test_stream_provider_failure_ends_with_error_without_completed_event() -> None:
    phases = Phases()
    instance, _persistence, driver, host, _journal = runtime(phases)
    driver.error = RuntimeError("provider down")
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
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
    assert getattr(result, "code", None).value == "ai_provider_error"
    assert [type(event) for event in seen] == [
        MetaEvent,
        UserMessageSavedEvent,
        StatusEvent,
        ErrorEvent,
    ]
    assert not any(isinstance(event, CompletedEvent) for event in seen)


def test_stream_pending_emits_waiting_status_before_confirmation() -> None:
    class Catalog:
        def resolve(self, name: str) -> object:
            return SimpleNamespace(name=name, kind="write")

        def write_names(self) -> set[str]:
            return {"update_application_status"}

        def provider_contracts(self) -> list[object]:
            return [SimpleNamespace(name="update_application_status")]

    phases = Phases()
    instance, _persistence, driver, host, _journal = runtime(
        phases,
        catalog=Catalog(),
    )
    driver.result = SimpleNamespace(
        added=[],
        reply="",
        pending=PendingAction(
            "call-1",
            "update_application_status",
            "{}",
            "更新状态",
            "op-1",
        ),
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
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
    assert result.__class__.__name__ == "ConfirmationRequiredOutcome"
    assert [type(event) for event in seen] == [
        MetaEvent,
        UserMessageSavedEvent,
        StatusEvent,
        StatusEvent,
        ConfirmationRequiredEvent,
        CompletedEvent,
    ]
    assert isinstance(seen[3], StatusEvent)
    assert seen[3].phase == "waiting_confirmation"


def test_stream_host_iterator_is_closed_when_outer_execution_aborts() -> None:
    phases = Phases()
    instance, _persistence, _driver, _host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True

    class Iterator:
        def __init__(self) -> None:
            self.close_calls = 0

        def __iter__(self) -> "Iterator":
            return self

        def __next__(self) -> object:
            raise RuntimeTransportAborted()

        def close(self) -> None:
            self.close_calls += 1

    class Host:
        def __init__(self) -> None:
            self.iterator = Iterator()

        def iter_events(self, thunk: object, control: object) -> object:
            del thunk, control
            return self.iterator

        def run(self, thunk: object, control: object) -> object:
            return self.iter_events(thunk, control)

    host = Host()
    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=None,
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert host.iterator.close_calls == 1


def test_prepare_stream_rejects_unknown_detached_context_value_fail_closed() -> None:
    unknown = object()
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(
        phases,
        assembled=[unknown],
    )
    control = InMemoryRuntimeInvocationControl()

    result = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )

    assert isinstance(result, ImmediateHttpOutcome)
    assert result.status_code == 503
    assert result.payload["error_code"] == "operation_failed"
    assert persistence.user_count == 1
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.abandoned == 1


def test_prepared_state_does_not_retain_resolved_provider_object() -> None:
    class Provider:
        def __repr__(self) -> str:
            return "PROVIDER_CREDENTIAL_CANARY"

    phases = Phases()
    instance, _persistence, _driver, _host, _journal = runtime(
        phases,
        model=Provider(),
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    state = prepared.opaque_state
    assert getattr(state, "resolved", None) is None
    assert "PROVIDER_CREDENTIAL_CANARY" not in repr(prepared)
