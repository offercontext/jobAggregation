from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CancelReason,
    ConfirmationRequiredOutcome,
    MessageOutcome,
    RuntimeFailureOutcome,
    RuntimeTransportContext,
    StartTurnRequest,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeFailureCode,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies
from offerpilot.ai.agent import PendingAction
from offerpilot.ai.tool_runtime.contracts import ToolFailure
from offerpilot.api import _confirmation_token as baseline_confirmation_token
from offerpilot.pilot_runtime.service import _confirmation_token
from offerpilot.agent_runtime.journal import SuspendedDisposition, TerminalDisposition
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.agent_runtime.journal import RunRecorderFactory
from offerpilot.db import init_database
from offerpilot.pilot_runtime.persistence import ChatPersistenceCoordinator
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.agent_runs import StartRunCommand


class _Phases:
    def __init__(self) -> None:
        self.items: list[str] = []

    def once(self, name: str) -> None:
        self.items.append(name)

    append = once


@dataclass
class _Conversation:
    id: int = 7
    archived_at: object | None = None
    context_type: str = "workspace"
    context_ref: str = ""
    mode: str = "general"


class _ConversationStore:
    def __init__(self, phases: _Phases, conversation: _Conversation | None = None) -> None:
        self.phases = phases
        self.conversation = conversation or _Conversation()

    def create(self, request: object) -> _Conversation:
        del request
        return self.conversation

    def load(self, conversation_id: int) -> _Conversation | None:
        del conversation_id
        return self.conversation


class _Persistence:
    def __init__(self, phases: _Phases, *, pending: object | None = None) -> None:
        self.phases = phases
        self.pending = pending
        self.user_count = 0
        self.message_count = 0
        self.pending_count = 0
        self.clarification_count = 0

    def get_pending_action(self, conversation_id: int) -> object | None:
        del conversation_id
        return self.pending

    def persist_initial_user_message(self, conversation_id: int, content: str) -> object:
        del conversation_id, content
        self.user_count += 1
        return SimpleNamespace(persisted=True, message_count=1, message_id=11)

    def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
        del conversation_id, messages
        self.message_count += 1
        return SimpleNamespace(persisted=True, message_id=12)

    def persist_initial_pending(self, conversation_id: int, messages: object, pending: object) -> object:
        del conversation_id, messages, pending
        self.pending_count += 1
        return SimpleNamespace(persisted=True)

    def persist_clarification(
        self,
        conversation_id: int,
        messages: object,
        pending: object,
        question: str,
    ) -> object:
        del conversation_id, messages, pending, question
        self.clarification_count += 1
        return SimpleNamespace(persisted=True)


class _Recorder:
    def __init__(self, phases: _Phases) -> None:
        self.phases = phases
        self.dispositions: list[tuple[object, object | None]] = []
        self.abandoned = 0

    def finish(self, command: TerminalDisposition) -> None:
        self.dispositions.append((command.status, command.failure_code))

    def suspend(self, command: SuspendedDisposition) -> None:
        del command

    def abandon(self) -> None:
        self.abandoned += 1


class _Journal:
    def __init__(self, phases: _Phases) -> None:
        self.phases = phases
        self.recorder = _Recorder(phases)

    def start_run(self, command: object) -> _Recorder:
        assert callable(command)
        return self.recorder


class _Source:
    def __init__(self, phases: _Phases, *, error: BaseException | None = None) -> None:
        self.phases = phases
        self.error = error

    def load(self, conversation: object, request: object) -> list[str]:
        del conversation, request
        if self.error is not None:
            raise self.error
        return ["frozen-source"]


class _Assembler:
    def __init__(self, phases: _Phases) -> None:
        self.phases = phases

    def assemble(self, source: object, conversation: object, request: object) -> list[str]:
        del source, conversation, request
        return ["system", "history"]


class _Driver:
    def __init__(self, phases: _Phases, result: object | None = None, error: BaseException | None = None) -> None:
        self.phases = phases
        self.result = result or SimpleNamespace(added=[], reply="hello", pending=None)
        self.error = error
        self.provider_calls = 0

    def run_turn(self, model: object, messages: object, **kwargs: object) -> object:
        del model, messages, kwargs
        self.provider_calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _Host:
    def __init__(self, phases: _Phases, error: BaseException | None = None) -> None:
        self.phases = phases
        self.error = error

    def run(self, thunk: object, control: object) -> object:
        del control
        if self.error is not None:
            raise self.error
        assert callable(thunk)
        return thunk()


def _runtime(
    phases: _Phases,
    *,
    conversation: _Conversation | None = None,
    persistence: _Persistence | None = None,
    source: _Source | None = None,
    driver: _Driver | None = None,
    host: _Host | None = None,
    journal: _Journal | None = None,
    model: object = "model",
    route: object = "model",
    missing_target_question: object | None = None,
) -> tuple[PilotRuntime, _Persistence, _Journal]:
    resolved_persistence = persistence or _Persistence(phases)
    resolved_journal = journal or _Journal(phases)
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_ConversationStore(phases, conversation),
            route_selector=lambda request, conversation: route,
            model_resolver=lambda request, conversation: model,
            persistence=resolved_persistence,
            journal=resolved_journal,
            source_loader=source or _Source(phases),
            context_assembler=_Assembler(phases),
            agent_driver=driver or _Driver(phases),
            phase_sink=phases,
            missing_target_question=missing_target_question,
        )
    )
    return runtime, resolved_persistence, resolved_journal


def _start(runtime: PilotRuntime, host: _Host, *, message: str = "hi") -> object:
    return runtime.start_turn(
        StartTurnRequest(message=message),
        transport=RuntimeTransportContext(mode="sync"),
        event_sink=None,
        signal_sink=None,
        execution_host=host,
        invocation_control=InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )


def test_start_turn_sync_sequence_is_frozen() -> None:
    phases = _Phases()
    runtime, persistence, _journal = _runtime(phases)
    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert result.message == "hello"
    assert phases.items == [
        "validate",
        "conversation",
        "route:model",
        "pending_guard",
        "model_resolve",
        "user_persist",
        "run_start",
        "source_load",
        "context_assemble",
        "agent_host",
        "result_normalize",
        "message_persist",
        "run_finish",
    ]
    assert persistence.user_count == 1
    assert persistence.message_count == 1


def test_confirmation_token_matches_closed_baseline_helper() -> None:
    pending = PendingAction(
        "call-17",
        "create_application",
        '{"z":1,"a":"text"}',
        "新建投递",
        "operation-17",
    )

    assert _confirmation_token(pending) == baseline_confirmation_token(pending)
    assert _confirmation_token(pending) == (
        "e7b9b3f0b6fb3ce539ce2912b6f41d1c93ba747a8c9d331d013d70b8220642d2"
    )


def test_pending_outcome_uses_confirmation_token_from_persisted_pending() -> None:
    phases = _Phases()
    pending = PendingAction(
        "call-17",
        "create_application",
        '{"z":1,"a":"text"}',
        "新建投递",
        "operation-17",
    )
    persistence = _Persistence(phases)
    runtime, _, _journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=SimpleNamespace(added=[], reply="", pending=pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, ConfirmationRequiredOutcome)
    assert result.confirmation_token == baseline_confirmation_token(pending)
    assert result.pending_action is not None
    assert result.pending_action.confirmation_token == result.confirmation_token


def test_journal_factory_receives_exact_start_run_builder_and_baseline_events() -> None:
    phases = _Phases()

    class StrictRecorder(_Recorder):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.events: list[object] = []
            self.contexts: list[object] = []

        def append_event(self, event: object) -> None:
            self.events.append(event)

        def capture_context(self, *args: object, **kwargs: object) -> None:
            self.contexts.append((args, kwargs))

    class StrictJournal:
        def __init__(self) -> None:
            self.recorder = StrictRecorder(phases)
            self.command: StartRunCommand | None = None

        def start_run(self, builder: object) -> StrictRecorder:
            assert callable(builder)
            key = JournalKeyDomain("00000000-0000-0000-0000-000000000001", b"k" * 32)
            command = builder(key, lambda: None)
            assert isinstance(command, StartRunCommand)
            self.command = command
            return self.recorder

    journal = StrictJournal()
    runtime, _persistence, _ = _runtime(phases, journal=journal)  # type: ignore[arg-type]

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert journal.command is not None
    assert journal.command.input_message_id == 11
    assert any(
        getattr(event, "event_type", None) == "route.selected"
        for event in journal.recorder.events
    )
    assert journal.recorder.contexts
    assert any(
        getattr(event, "event_type", None) == "assistant.persisted"
        for event in journal.recorder.events
    )


def test_real_run_recorder_factory_accepts_runtime_builder_and_records_terminal_events(
    tmp_path: Path,
) -> None:
    phases = _Phases()
    data_dir = tmp_path
    sessions = init_database(data_dir / "offerpilot.db")
    chat = ChatRepository(sessions)
    conversation = chat.create_conversation("real journal")
    coordinator = ChatPersistenceCoordinator(chat)
    repository = AgentRunRepository(sessions)
    key = JournalKeyDomain("00000000-0000-0000-0000-000000000002", b"j" * 32)

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

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Gateway(),
            persistence=coordinator,
            model_resolver=lambda request, conversation: "model",
            source_loader=_Source(phases),
            context_assembler=_Assembler(phases),
            agent_driver=_Driver(phases),
            journal=journal,
            phase_sink=phases,
        )
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert journal.recorder is not None
    run_id = getattr(journal.recorder, "run_id")
    assert isinstance(run_id, str)
    events = repository.list_events(run_id)
    event_types = [event.event_type for event in events]
    assert "route.selected" in event_types
    assert "context.captured" in event_types
    assert "assistant.persisted" in event_types
    assert event_types[-1] == "segment.finished"


def test_missing_conversation_has_no_model_or_persistence_side_effect() -> None:
    phases = _Phases()
    persistence = _Persistence(phases)
    runtime, _, _ = _runtime(phases, conversation=None, persistence=persistence)
    runtime._dependencies.conversations.conversation = None  # type: ignore[attr-defined]

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.APPLICATION_NOT_FOUND
    assert persistence.user_count == 0


def test_live_pending_stops_before_model_resolution() -> None:
    phases = _Phases()
    pending = SimpleNamespace(tool_call_id="call-1", tool_name="write", args="{}", human="write", operation_id="op-1")
    runtime, persistence, _ = _runtime(phases, persistence=_Persistence(phases, pending=pending))

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED
    assert persistence.user_count == 0
    assert "agent_host" not in phases.items


def test_source_failure_persists_user_and_finishes_failed_journal() -> None:
    phases = _Phases()
    runtime, persistence, journal = _runtime(
        phases,
        source=_Source(phases, error=RuntimeError("source failed")),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.SOURCE_LOAD_FAILED
    assert persistence.user_count == 1
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == [("failed", "source_load_failed")]


def test_timeout_writes_fixed_assistant_message_and_does_not_provider_map() -> None:
    phases = _Phases()

    class TimeoutPersistence(_Persistence):
        def persist_timeout_assistant(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            self.message_count += 1
            self.phases.once("message_persist")
            return SimpleNamespace(persisted=True)

    persistence = TimeoutPersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)
    result = _start(runtime, _Host(phases, RuntimeAgentTimedOut()))

    assert isinstance(result, MessageOutcome)
    assert result.message
    assert persistence.message_count == 1
    assert journal.recorder.dispositions == [("timed_out", "timeout")]


@pytest.mark.parametrize("failure_mode", ["exception", "none", "failed"])
def test_timeout_persistence_failure_is_safe_and_not_reported_as_message(
    failure_mode: str,
) -> None:
    phases = _Phases()

    class FailingTimeoutPersistence(_Persistence):
        def persist_timeout_assistant(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            if failure_mode == "exception":
                raise OSError("timeout message unavailable")
            if failure_mode == "none":
                return None
            return SimpleNamespace(persisted=False, status="failed")

    persistence = FailingTimeoutPersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)

    result = _start(runtime, _Host(phases, RuntimeAgentTimedOut()))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert result.status_code == 503
    assert result.retryable is True
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == [("failed", "unknown")]


def test_host_timeout_with_timed_out_control_still_records_timeout_delivery() -> None:
    phases = _Phases()
    control = InMemoryRuntimeInvocationControl()

    class TimeoutHost:
        def run(self, thunk: object, invocation_control: object) -> object:
            del thunk
            assert invocation_control is control
            assert control.request_timeout()
            raise RuntimeAgentTimedOut()

    class TimeoutPersistence(_Persistence):
        def persist_timeout_assistant(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            self.message_count += 1
            return SimpleNamespace(persisted=True, message_id=13)

    persistence = TimeoutPersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)

    result = runtime.start_turn(
        StartTurnRequest(message="hi"),
        transport=RuntimeTransportContext(mode="sync"),
        execution_host=TimeoutHost(),  # type: ignore[arg-type]
        invocation_control=control,
        cancel_check=lambda: False,
    )

    assert isinstance(result, MessageOutcome)
    assert journal.recorder.dispositions == [("timed_out", "timeout")]


def test_transport_abort_is_rethrown_and_journal_is_abandoned() -> None:
    phases = _Phases()
    runtime, _, journal = _runtime(phases)

    with pytest.raises(RuntimeTransportAborted):
        _start(runtime, _Host(phases, RuntimeTransportAborted()))

    assert journal.recorder.dispositions == []
    assert journal.recorder.abandoned == 1


def test_model_unconfigured_returns_before_user_persist() -> None:
    phases = _Phases()
    runtime, persistence, _journal = _runtime(phases, model=None)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.AI_PROVIDER_ERROR
    assert persistence.user_count == 0


def test_provider_failure_is_safe_and_finishes_provider_error() -> None:
    phases = _Phases()
    runtime, persistence, journal = _runtime(phases, driver=_Driver(phases, error=ValueError("secret")))

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.AI_PROVIDER_ERROR
    assert "secret" not in result.message
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == [("failed", "provider_error")]


def test_pending_result_is_atomically_persisted_and_suspended() -> None:
    phases = _Phases()
    pending = SimpleNamespace(
        tool_call_id="call-1",
        tool_name="write",
        args='{"id": 1}',
        human="write",
        operation_id="op-1",
    )
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=SimpleNamespace(added=[], reply="", pending=pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, ConfirmationRequiredOutcome)
    assert persistence.pending_count == 1
    assert journal.recorder.dispositions == []
    assert phases.items[-1] == "run_suspend"


@pytest.mark.parametrize(
    ("pending", "status", "expected_code"),
    [
        (True, "cas_lost", RuntimeFailureCode.OPERATION_FAILED),
        (False, "closed", RuntimeFailureCode.CONVERSATION_ARCHIVED),
    ],
)
def test_persistence_failure_finishes_failed_not_completed(
    pending: bool,
    status: str,
    expected_code: RuntimeFailureCode,
) -> None:
    phases = _Phases()
    action = PendingAction("call-1", "write", '{"id":1}', "write", "op-1")

    class FailingPersistence(_Persistence):
        def persist_initial_pending(
            self,
            conversation_id: int,
            messages: object,
            pending_value: object,
        ) -> object:
            del conversation_id, messages, pending_value
            return SimpleNamespace(persisted=False, status=status)

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id, messages
            return SimpleNamespace(persisted=False, status=status)

    persistence = FailingPersistence(phases)
    result_value = (
        SimpleNamespace(added=[], reply="", pending=action)
        if pending
        else SimpleNamespace(added=[], reply="final", pending=None)
    )
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=result_value),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is expected_code
    assert journal.recorder.dispositions == [("failed", "unknown")]
    assert "run_finish" in phases.items


def test_missing_target_uses_clarification_without_pending_outcome() -> None:
    phases = _Phases()
    pending = SimpleNamespace(
        tool_call_id="call-1",
        tool_name="write",
        args="{}",
        human="write",
        operation_id="op-1",
    )
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=SimpleNamespace(added=[], reply="", pending=pending)),
        missing_target_question=lambda pending, conversation_id: "请先选择投递目标。",
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert result.message == "请先选择投递目标。"
    assert persistence.clarification_count == 1
    assert journal.recorder.dispositions == [("completed", None)]


@pytest.mark.parametrize("failure_mode", ["exception", "none", "failed"])
def test_non_atomic_clarification_set_failure_stops_before_assistant_and_completion(
    failure_mode: str,
) -> None:
    phases = _Phases()
    pending = SimpleNamespace(
        tool_call_id="call-1",
        tool_name="write",
        args="{}",
        human="write",
        operation_id="op-1",
    )

    class FallbackPersistence(_Persistence):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.persist_clarification = None  # type: ignore[method-assign]
            self.setter_calls = 0
            self.assistant_calls = 0

        def set_pending_clarification(
            self,
            conversation_id: int,
            pending_value: object,
            question: str,
        ) -> object:
            del conversation_id, pending_value, question
            self.setter_calls += 1
            if failure_mode == "exception":
                raise OSError("clarification CAS unavailable")
            if failure_mode == "none":
                return None
            return SimpleNamespace(persisted=False, status="cas_lost")

        def persist_assistant_message(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            self.assistant_calls += 1
            return SimpleNamespace(persisted=True, message_id=13)

    persistence = FallbackPersistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=SimpleNamespace(added=[], reply="", pending=pending)),
        missing_target_question=lambda pending, conversation_id: "请先选择投递目标。",
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.setter_calls == 1
    assert persistence.assistant_calls == 0
    assert journal.recorder.dispositions == [("failed", "unknown")]


def test_final_projection_redacts_internal_tool_names_and_uses_safe_write_error() -> None:
    phases = _Phases()

    class CapturingPersistence(_Persistence):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.messages: object | None = None

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id
            self.messages = messages
            return SimpleNamespace(persisted=True, message_id=12)

    persistence = CapturingPersistence(phases)
    write_record = SimpleNamespace(
        prepared=SimpleNamespace(spec=SimpleNamespace(kind="write")),
        outcome=SimpleNamespace(code="company_required", compatibility_detail="company_required"),
    )
    result_value = SimpleNamespace(
        added=[],
        reply="请继续调用 update_application_status。",
        pending=None,
        records=(write_record,),
        failures=(ToolFailure("validation_error", "company_required", "company_required"),),
    )
    runtime, _, _journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=result_value),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert result.message == "这次复盘还缺少公司信息。请告诉我公司名称，或先说明不关联具体公司。"
    assert result.write_status == "failed"
    assert result.write_error == "company_required"
    assert persistence.messages is not None
    assert "update_application_status" not in str(persistence.messages)


def test_archived_conversation_stops_before_pending_and_model() -> None:
    phases = _Phases()
    archived = _Conversation(archived_at=object())
    runtime, persistence, _journal = _runtime(phases, conversation=archived)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.CONVERSATION_ARCHIVED
    assert persistence.user_count == 0
    assert "pending_guard" not in phases.items


def test_deterministic_route_is_private_unsupported_before_model_side_effects() -> None:
    phases = _Phases()
    runtime, persistence, journal = _runtime(phases, route="deterministic")

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert persistence.user_count == 0
    assert journal.recorder.dispositions == []


def test_journal_failure_is_fail_open_for_successful_model_turn() -> None:
    phases = _Phases()

    class DegradedRecorder(_Recorder):
        def finish(self, status: object, failure_code: object | None = None) -> None:
            del status, failure_code
            raise OSError("journal unavailable")

    class DegradedJournal(_Journal):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.recorder = DegradedRecorder(phases)

    runtime, persistence, _journal = _runtime(phases, journal=DegradedJournal(phases))
    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert persistence.message_count == 1


def test_late_control_result_is_not_persisted() -> None:
    phases = _Phases()
    runtime, persistence, journal = _runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    assert control.request_cancel(CancelReason.EXPLICIT_CANCEL)

    with pytest.raises(RuntimeCancelled):
        runtime.start_turn(
            StartTurnRequest(message="hi"),
            transport=RuntimeTransportContext(mode="sync"),
            event_sink=None,
            signal_sink=None,
            execution_host=_Host(phases),
            invocation_control=control,
            cancel_check=lambda: False,
        )

    assert persistence.message_count == 0
    assert journal.recorder.abandoned == 0


@pytest.mark.parametrize("barrier_phase", ["result_normalize", "message_persist"])
def test_cancel_barrier_before_result_persist_has_no_late_writes(barrier_phase: str) -> None:
    control = InMemoryRuntimeInvocationControl()

    class BarrierPhases(_Phases):
        def once(self, name: str) -> None:
            super().once(name)
            if name == barrier_phase:
                assert control.request_cancel(CancelReason.EXPLICIT_CANCEL)

        append = once

    phases = BarrierPhases()
    runtime, persistence, journal = _runtime(phases)

    with pytest.raises(RuntimeCancelled):
        runtime.start_turn(
            StartTurnRequest(message="hi"),
            transport=RuntimeTransportContext(mode="sync"),
            execution_host=_Host(phases),
            invocation_control=control,
            cancel_check=lambda: False,
        )

    assert persistence.message_count == 0
    assert persistence.pending_count == 0
    assert persistence.clarification_count == 0
    assert journal.recorder.abandoned == 1


def test_cancel_between_terminal_phase_and_journal_write_abandons_once() -> None:
    control = InMemoryRuntimeInvocationControl()

    class TerminalBarrier(_Phases):
        def once(self, name: str) -> None:
            super().once(name)
            if name == "run_finish":
                assert control.request_cancel(CancelReason.EXPLICIT_CANCEL)

        append = once

    phases = TerminalBarrier()
    runtime, persistence, journal = _runtime(phases)

    with pytest.raises(RuntimeCancelled):
        runtime.start_turn(
            StartTurnRequest(message="hi"),
            transport=RuntimeTransportContext(mode="sync"),
            execution_host=_Host(phases),
            invocation_control=control,
            cancel_check=lambda: False,
        )

    assert persistence.message_count == 1
    assert journal.recorder.dispositions == []
    assert journal.recorder.abandoned == 1


def test_event_sink_transport_failure_is_not_provider_failure() -> None:
    phases = _Phases()

    class EmittingDriver(_Driver):
        def run_turn(self, model: object, messages: object, **kwargs: object) -> object:
            del model, messages
            sink = kwargs.get("event_sink")
            assert callable(sink)
            cast_sink = sink
            cast_sink(AssistantMessageEvent(message="hi"))
            return SimpleNamespace(added=[], reply="hello", pending=None)

    class FailingSink:
        def emit(self, event: object) -> None:
            del event
            raise OSError("closed")

    runtime, persistence, _journal = _runtime(phases, driver=EmittingDriver(phases))
    with pytest.raises(RuntimeTransportAborted):
        runtime.start_turn(
            StartTurnRequest(message="hi"),
            transport=RuntimeTransportContext(mode="sync"),
            event_sink=FailingSink(),  # type: ignore[arg-type]
            signal_sink=None,
            execution_host=_Host(phases),
            invocation_control=InMemoryRuntimeInvocationControl(),
            cancel_check=lambda: False,
        )
    assert persistence.message_count == 0


@pytest.mark.parametrize("control_error", [RuntimeCancelled(), RuntimeTransportAborted(), KeyboardInterrupt()])
def test_control_and_base_exceptions_are_rethrown_after_journal_cleanup(control_error: BaseException) -> None:
    phases = _Phases()
    runtime, _persistence, journal = _runtime(phases)

    with pytest.raises(type(control_error)) as raised:
        _start(runtime, _Host(phases, control_error))

    assert raised.value is control_error
    assert journal.recorder.abandoned == 1
