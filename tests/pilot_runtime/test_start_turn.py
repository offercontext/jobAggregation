from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CancelReason,
    ConfirmationRequiredOutcome,
    InvocationState,
    MessageOutcome,
    RuntimeFailureOutcome,
    RuntimeTransportContext,
    StartTurnRequest,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeFailureCode,
    ModelUnconfiguredError,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.chat_transport import SyncAgentExecutionHost
from offerpilot.pilot_runtime.service import (
    PilotRuntime,
    ResolvedPolicyCatalog,
    ResolvedModel,
    SegmentExecution,
    RuntimeDependencies,
    _result_persisted,
)
from offerpilot.pilot_runtime.service import _normalize_agent_result
from offerpilot.pilot_runtime.composition import _ContextAdapter
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.ai.agent_loop import _LoopServices
from offerpilot.ai.agent_contracts import AgentTurnResult, PendingAction
from offerpilot.ai.agent_loop import NewTurnSeed, SegmentSurfaceGate, build_segment_surface_gate
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.context_projector.selector import DEPENDENCY_POLICY_V1
from offerpilot.ai.tool_runtime.contracts import ToolFailure
from offerpilot.ai.types import Message, ToolCall
from offerpilot.api import _confirmation_token as baseline_confirmation_token
from offerpilot.pilot_runtime.service import _confirmation_token
from offerpilot.agent_runtime.journal import SuspendedDisposition, TerminalDisposition
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.agent_runtime.journal import RunRecorderFactory
from offerpilot.db import init_database
from offerpilot.pilot_runtime.persistence import (
    ChatPersistenceCoordinator,
    PersistenceResult,
    PersistenceStatus,
)
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.agent_runs import StartRunCommand
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


_AUTHORITY_SESSIONS = init_database(
    Path(tempfile.mkdtemp(prefix="offerpilot-pilot-authority-")) / "authority.db"
)
_AUTHORITY_POLICY = validate_startup_policy(MODEL_TOOL_CATALOG.authority_manifest)


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


class _Catalog:
    def __init__(self, *, write_names: tuple[str, ...] = ("write", "create_application")) -> None:
        self._write_names = frozenset(write_names)

    def resolve(self, name: str) -> object | None:
        if name in self._write_names:
            return SimpleNamespace(name=name, kind="write")
        if name == "read_tool":
            return SimpleNamespace(name=name, kind="read")
        return None

    def write_names(self) -> frozenset[str]:
        return self._write_names

    def provider_contracts(self) -> tuple[object, ...]:
        return tuple(SimpleNamespace(name=name) for name in sorted(self._write_names))


def _real_segment(
    conversation: object,
    recorder: object,
    catalog: object,
    close_counter: list[int] | None = None,
) -> tuple[SegmentExecution, SegmentSurfaceGate | None]:
    conversation_id = getattr(conversation, "id")
    context_type = str(getattr(conversation, "context_type", "workspace"))
    context_ref = getattr(conversation, "context_ref", None)
    if context_type != "application":
        context_ref = None
    mode = str(getattr(conversation, "mode", "general"))
    revision = int(getattr(conversation, "scope_revision", 0))
    factory = AuthorityFactory()
    authority = factory.create_segment_authority(
        conversation_id=conversation_id,
        conversation_scope_revision=revision,
        segment_id=f"pilot-test-{conversation_id}-{id(recorder)}",
        trusted_scope=TrustedContextScope(context_type, context_ref, mode),
        capability_profile_id=_AUTHORITY_POLICY.capability_profile.profile_id,
        capabilities=frozenset(_AUTHORITY_POLICY.capability_profile.capabilities),
        capability_policy_version=_AUTHORITY_POLICY.capability_policy_version,
        binding_policy_version=_AUTHORITY_POLICY.binding_policy_version,
        capability_profile_fingerprint=_AUTHORITY_POLICY.capability_profile_fingerprint,
        binding_policy_fingerprint=_AUTHORITY_POLICY.binding_policy_fingerprint,
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(_AUTHORITY_SESSIONS),
        events=ApplicationEventsRepository(_AUTHORITY_SESSIONS),
        notes=NotesRepository(_AUTHORITY_SESSIONS),
        offers=OffersRepository(_AUTHORITY_SESSIONS),
        resumes=ResumesRepository(_AUTHORITY_SESSIONS),
        jd_analyses=JDAnalysesRepository(_AUTHORITY_SESSIONS),
        run_recorder=recorder,  # type: ignore[arg-type]
    )

    def close() -> None:
        if close_counter is not None:
            close_counter.append(1)
        factory.close()

    return (
        SegmentExecution(
            authority=authority,
            context=context,
            catalog=None,
            close=close,
            surface_gate=None,
        ),
        None,
    )


class _Persistence:
    def __init__(self, phases: _Phases, *, pending: object | None = None) -> None:
        self.phases = phases
        self.pending = pending
        self.clarification: object | None = None
        self._next_message_id = 11
        self._messages: list[object] = []
        self.user_count = 0
        self.message_count = 0
        self.pending_count = 0
        self.clarification_count = 0

    def get_pending_action(self, conversation_id: int) -> object | None:
        del conversation_id
        return self.pending

    def get_pending_clarification(self, conversation_id: int) -> object | None:
        del conversation_id
        return self.clarification

    def list_messages(self, conversation_id: int) -> tuple[object, ...]:
        del conversation_id
        return tuple(self._messages)

    def _append_message(self, role: str) -> int:
        message_id = self._next_message_id
        self._next_message_id += 1
        self._messages.append(SimpleNamespace(id=message_id, role=role))
        return message_id

    def persist_initial_user_message(self, conversation_id: int, content: str) -> object:
        del conversation_id, content
        self.user_count += 1
        message_id = self._append_message("user")
        return PersistenceResult(
            PersistenceStatus.PERSISTED, message_count=1, message_id=message_id
        )

    def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
        del conversation_id
        self.message_count += 1
        values = tuple(messages) if isinstance(messages, (tuple, list)) else ()
        message_ids = tuple(
            self._append_message(str(getattr(message, "role", "assistant"))) for message in values
        )
        if not message_ids:
            message_ids = (self._append_message("assistant"),)
        return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=message_ids)

    def persist_initial_pending(
        self, conversation_id: int, messages: object, pending: object
    ) -> object:
        del conversation_id
        self.pending = pending
        self.pending_count += 1
        values = tuple(messages) if isinstance(messages, (tuple, list)) else ()
        message_ids = tuple(
            self._append_message(str(getattr(message, "role", "assistant"))) for message in values
        )
        return PersistenceResult(
            PersistenceStatus.PERSISTED,
            message_ids=message_ids,
        )

    def persist_initial_assistant_message(
        self, conversation_id: int, content: str, **kwargs: object
    ) -> object:
        del conversation_id, content, kwargs
        return PersistenceResult(
            PersistenceStatus.PERSISTED,
            message_id=self._append_message("assistant"),
        )

    def persist_assistant_message(
        self, conversation_id: int, content: str, **kwargs: object
    ) -> object:
        return self.persist_initial_assistant_message(conversation_id, content, **kwargs)

    def persist_clarification(
        self,
        conversation_id: int,
        messages: object,
        pending: object,
        question: str,
    ) -> object:
        del conversation_id
        self.clarification_count += 1
        values = tuple(messages) if isinstance(messages, (tuple, list)) else ()
        message_ids = tuple(
            self._append_message(str(getattr(message, "role", "assistant"))) for message in values
        )
        self.pending = None
        self.clarification = SimpleNamespace(pending=pending, question=question)
        message_ids += (self._append_message("assistant"),)
        return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=message_ids)

    def set_pending_clarification(
        self, conversation_id: int, pending: object, question: str
    ) -> object:
        del conversation_id
        self.clarification = SimpleNamespace(pending=pending, question=question)
        self.clarification_count += 1
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def clear_pending_action(self, conversation_id: int) -> object:
        del conversation_id
        self.pending = None
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def clear_pending_clarification(self, conversation_id: int) -> object:
        del conversation_id
        self.clarification = None
        return PersistenceResult(PersistenceStatus.PERSISTED)

    def persist_timeout_assistant(self, conversation_id: int, content: str) -> object:
        del conversation_id, content
        self.clarification = None
        return PersistenceResult(
            PersistenceStatus.PERSISTED,
            message_id=self._append_message("assistant"),
        )


class _Recorder:
    def __init__(self, phases: _Phases) -> None:
        self.phases = phases
        self.dispositions: list[tuple[object, object | None]] = []
        self.abandoned = 0
        self.events: list[object] = []

    def finish(self, command: TerminalDisposition) -> None:
        self.dispositions.append((command.status, command.failure_code))

    def suspend(self, command: SuspendedDisposition) -> None:
        del command

    def abandon(self) -> None:
        self.abandoned += 1

    def append_event(self, event: object) -> None:
        self.events.append(event)


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
    def __init__(
        self, phases: _Phases, result: object | None = None, error: BaseException | None = None
    ) -> None:
        self.phases = phases
        self.result = result or AgentTurnResult([], "hello", None)
        self.error = error
        self.provider_calls = 0
        self.invocations: list[object] = []

    def execute(self, invocation: object) -> object:
        self.invocations.append(invocation)
        assert isinstance(getattr(invocation, "seed", None), NewTurnSeed)
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


def test_agent_driver_result_boundary_rejects_structural_namespace() -> None:
    class NamespaceDriver:
        def execute(self, invocation: object) -> object:
            del invocation
            return SimpleNamespace(added=[], reply="done", pending=None)

    runtime = PilotRuntime.__new__(PilotRuntime)
    with pytest.raises(TypeError, match="Agent.*AgentTurnResult"):
        runtime._run_driver(NamespaceDriver(), object())

    with pytest.raises(TypeError, match="sealed turn result"):
        _normalize_agent_result({"added": [], "reply": "done", "pending": None})


def test_production_context_assembler_appends_current_request_as_last_user() -> None:
    class Persistence:
        def get_pending_clarification(self, conversation_id: int) -> None:
            del conversation_id
            return None

    adapter = _ContextAdapter(
        Persistence(),  # type: ignore[arg-type]
        system_message=lambda: Message(role="system", content="policy"),
        clarification_message=lambda _pending, _message: None,
        page_messages=lambda _page: (),
    )
    assembled = adapter.assemble(
        SimpleNamespace(history=(Message(role="user", content="old"),)),
        _Conversation(),
        StartTurnRequest(message="new request"),
    )
    assert isinstance(assembled[-1], Message)
    assert assembled[-1].role == "user"
    assert assembled[-1].content == "new request"
    assert assembled[-1].surface_contributor == "current_request"


def test_surface_aware_model_without_exact_session_factory_fails_closed() -> None:
    class SurfaceModel:
        def complete_agent_surface(self, *_args: object, **_kwargs: object) -> object:
            return object()

    invocation = SimpleNamespace(
        model=SurfaceModel(),
        catalog=SimpleNamespace(),
        tool_context=SimpleNamespace(authority=object()),
        seed=SimpleNamespace(),
        event_sink=None,
        cancel_check=None,
        run_recorder=_Recorder(_Phases()),
        runtime_signal_sink=None,
        surface_gate=None,
    )
    services = _LoopServices(invocation)
    with pytest.raises(ProjectionError, match="factory_required"):
        services.complete_model([], [], model_step=1)


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
    catalog: object | None = None,
    dependency_catalog: object | None = None,
    model_resolver: object | None = None,
    surface_resolver: object | None = None,
    policy_resolver: object | None = None,
    segment_resolver: object | None = None,
    close_counter: list[int] | None = None,
) -> tuple[PilotRuntime, _Persistence, _Journal]:
    resolved_persistence = persistence or _Persistence(phases)
    resolved_journal = journal or _Journal(phases)
    # Segment visibility is issued only against the reviewed typed catalog;
    # pending/readback tests use unknown names when they need a rejection.
    resolved_catalog = MODEL_TOOL_CATALOG

    def resolve_model(request: object, conversation: object) -> object:
        del request, conversation
        if model is None:
            return None
        return ResolvedModel(model=model)

    def resolve_policy(request: object, conversation: object, source_value: object) -> object:
        del request, conversation, source_value
        return ResolvedPolicyCatalog(
            catalog=resolved_catalog,
            policy=_AUTHORITY_POLICY,
            dependency_policy=DEPENDENCY_POLICY_V1,
        )

    def resolve_segment(
        request: object,
        conversation: object,
        source_value: object,
        recorder: object,
    ) -> object:
        del request, source_value
        segment, _gate = _real_segment(
            conversation,
            recorder,
            resolved_catalog,
            close_counter,
        )
        return segment

    def resolve_surface(
        request: object,
        conversation: object,
        source_value: object,
        assembled: object,
        policy: object,
        segment: object,
    ) -> object:
        del request, conversation, source_value
        messages = tuple(
            value if isinstance(value, Message) else Message(role="user", content=str(value))
            for value in assembled
        )
        return build_segment_surface_gate(
            messages,
            catalog=getattr(policy, "catalog"),
            context=getattr(segment, "context"),
            authority=getattr(segment, "authority"),
            dependency_policy=getattr(policy, "dependency_policy"),
            policy=getattr(policy, "policy"),
        )

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_ConversationStore(phases, conversation),
            route_selector=lambda request, conversation: route,
            policy_catalog_resolver=policy_resolver or resolve_policy,
            segment_context_resolver=segment_resolver or resolve_segment,
            surface_gate_resolver=surface_resolver or resolve_surface,
            continuation_model_resolver=model_resolver or resolve_model,
            persistence=resolved_persistence,
            journal=resolved_journal,
            source_loader=source or _Source(phases),
            context_assembler=_Assembler(phases),
            agent_driver=driver or _Driver(phases),
            phase_sink=phases,
            missing_target_question=missing_target_question,
            catalog=dependency_catalog,
        )
    )
    return runtime, resolved_persistence, resolved_journal


def _start(
    runtime: PilotRuntime,
    host: _Host,
    *,
    message: str = "hi",
    control: InMemoryRuntimeInvocationControl | None = None,
) -> object:
    return runtime.start_turn(
        StartTurnRequest(message=message),
        transport=RuntimeTransportContext(mode="sync"),
        event_sink=None,
        signal_sink=None,
        execution_host=host,
        invocation_control=control or InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )


def test_start_turn_sync_sequence_is_frozen() -> None:
    phases = _Phases()
    driver = _Driver(phases)
    runtime, persistence, _journal = _runtime(phases, driver=driver)
    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert result.message == "hello"
    assert len(driver.invocations) == 1
    assert phases.items == [
        "validate",
        "conversation",
        "route:model",
        "pending_guard",
        "source_load",
        "segment_resolve",
        "context_assemble",
        "policy_resolve",
        "surface_resolve",
        "model_resolve",
        "user_persist",
        "run_start",
        "agent_host",
        "result_normalize",
        "message_persist",
        "run_finish",
    ]


def test_sync_segment_failure_stops_before_policy_catalog_and_side_effects() -> None:
    phases = _Phases()
    policy_calls: list[object] = []

    def failing_segment(
        request: object,
        conversation: object,
        source: object,
        recorder: object,
    ) -> object:
        del request, conversation, source, recorder
        raise RuntimeError("segment unavailable")

    def policy_spy(*args: object, **kwargs: object) -> object:
        policy_calls.append((args, kwargs))
        return ResolvedPolicyCatalog(
            catalog=MODEL_TOOL_CATALOG,
            policy=_AUTHORITY_POLICY,
            dependency_policy=DEPENDENCY_POLICY_V1,
        )

    runtime, persistence, journal = _runtime(
        phases,
        segment_resolver=failing_segment,
        policy_resolver=policy_spy,
    )
    driver = runtime._dependencies.agent_driver
    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.SOURCE_LOAD_FAILED
    assert policy_calls == []
    assert "context_assemble" not in phases.items
    assert "policy_resolve" not in phases.items
    assert "surface_resolve" not in phases.items
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0
    assert getattr(driver, "provider_calls", 0) == 0
    assert journal.recorder.dispositions == []


def test_sync_live_policy_drift_closes_segment_before_provider_or_user() -> None:
    phases = _Phases()
    close_count: list[int] = []
    drifted = replace(
        _AUTHORITY_POLICY,
        binding_policy_fingerprint="sha256:" + "1" * 64,
    )

    def drift_policy(
        request: object,
        conversation: object,
        source: object,
        segment: object,
    ) -> object:
        del request, conversation, source
        assert type(segment) is SegmentExecution
        assert type(segment.authority) is SegmentExecutionAuthority
        assert type(segment.context) is ToolExecutionContext
        assert segment.context.authority is segment.authority
        assert segment.catalog is None
        assert segment.policy is None
        assert segment.surface_gate is None
        return ResolvedPolicyCatalog(
            catalog=MODEL_TOOL_CATALOG,
            policy=drifted,
            dependency_policy=DEPENDENCY_POLICY_V1,
        )

    runtime, persistence, journal = _runtime(
        phases,
        policy_resolver=drift_policy,
        close_counter=close_count,
    )
    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert close_count == [1]
    assert "surface_resolve" not in phases.items
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0
    assert journal.recorder.dispositions == []


def test_sync_policy_spy_sees_exact_unbound_segment_after_segment_phase() -> None:
    phases = _Phases()
    seen: list[tuple[list[str], object]] = []

    def policy_spy(
        request: object,
        conversation: object,
        source: object,
        segment: object,
    ) -> object:
        del request, conversation, source
        seen.append((list(phases.items), segment))
        assert type(segment) is SegmentExecution
        assert type(segment.authority) is SegmentExecutionAuthority
        assert type(segment.context) is ToolExecutionContext
        assert segment.context.authority is segment.authority
        assert segment.catalog is None
        assert segment.policy is None
        assert segment.surface_gate is None
        return ResolvedPolicyCatalog(
            catalog=MODEL_TOOL_CATALOG,
            policy=_AUTHORITY_POLICY,
            dependency_policy=DEPENDENCY_POLICY_V1,
        )

    runtime, _persistence, _journal = _runtime(phases, policy_resolver=policy_spy)
    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert len(seen) == 1
    snapshot, _segment = seen[0]
    assert snapshot.index("segment_resolve") < snapshot.index("policy_resolve")


def test_sync_malformed_surface_gate_closes_before_model_or_user() -> None:
    phases = _Phases()
    close_count: list[int] = []

    def malformed_gate(*_args: object, **_kwargs: object) -> object:
        return object()

    runtime, persistence, journal = _runtime(
        phases,
        surface_resolver=malformed_gate,
        close_counter=close_count,
    )
    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert close_count == [1]
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0


@pytest.mark.parametrize(
    ("field_name", "error"),
    [
        ("status", ValueError("status getter")),
        ("status", KeyboardInterrupt()),
        ("message_id", ValueError("message id getter")),
        ("message_id", KeyboardInterrupt()),
    ],
)
def test_sync_user_result_getter_closes_segment_on_any_exception(
    field_name: str,
    error: BaseException,
) -> None:
    phases = _Phases()
    close_count: list[int] = []

    class ExplodingResult:
        @property
        def status(self) -> object:
            if field_name == "status":
                raise error
            return PersistenceStatus.PERSISTED

        @property
        def message_id(self) -> object:
            if field_name == "message_id":
                raise error
            return 1

    class PersistenceWithExplodingResult(_Persistence):
        def persist_initial_user_message(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            return ExplodingResult()

    persistence = PersistenceWithExplodingResult(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        close_counter=close_count,
    )

    with pytest.raises(type(error)):
        _start(runtime, _Host(phases))
    assert close_count == [1]


@pytest.mark.parametrize("error", [ValueError("persistence surface"), KeyboardInterrupt()])
def test_sync_persistence_surface_getter_closes_segment_on_any_exception(
    error: BaseException,
) -> None:
    phases = _Phases()
    close_count: list[int] = []

    class PersistenceWithExplodingSurface(_Persistence):
        def __getattribute__(self, name: str) -> object:
            if name == "persist_initial_assistant_message":
                raise error
            return super().__getattribute__(name)

    persistence = PersistenceWithExplodingSurface(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        close_counter=close_count,
    )

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _start(runtime, _Host(phases))
    else:
        result = _start(runtime, _Host(phases))
        assert isinstance(result, RuntimeFailureOutcome)
        assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert close_count == [1]
    assert journal.recorder.events == []


def test_sync_host_leaves_control_active_until_runtime_terminal_commit() -> None:
    phases = _Phases()
    runtime, persistence, _journal = _runtime(phases)
    control = InMemoryRuntimeInvocationControl()

    result = runtime.start_turn(
        StartTurnRequest(message="hi"),
        transport=RuntimeTransportContext(mode="sync"),
        execution_host=SyncAgentExecutionHost(timeout_seconds=1.0),
        invocation_control=control,
        cancel_check=lambda: False,
    )

    assert isinstance(result, MessageOutcome)
    assert persistence.user_count == 1
    assert persistence.message_count == 1
    assert control.state is InvocationState.COMPLETED


@pytest.mark.parametrize(
    "value",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(persisted=True),
        SimpleNamespace(status="persisted"),
        SimpleNamespace(status="unknown"),
    ],
)
def test_persistence_result_projection_is_fail_closed(value: object) -> None:
    assert _result_persisted(value) is False
    assert _result_persisted(True) is True
    assert _result_persisted(False) is False
    assert _result_persisted(PersistenceResult(PersistenceStatus.PERSISTED)) is True


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
    control = InMemoryRuntimeInvocationControl()
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
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
    )

    result = _start(runtime, _Host(phases), control=control)

    assert isinstance(result, ConfirmationRequiredOutcome)
    assert control.state is InvocationState.COMPLETED
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
        getattr(event, "event_type", None) == "route.selected" for event in journal.recorder.events
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

    # The integration assertion exercises the real repository path; freeze the
    # journal clock so suite load cannot turn this identity check into a
    # fail-open budget diagnostic.
    journal = CapturingFactory(
        repository,
        key=key,
        enabled=True,
        clock=lambda: 0.0,
    )

    class Gateway:
        def create(self, request: object) -> object:
            del request
            return conversation

        def load(self, conversation_id: int) -> object:
            assert conversation_id == conversation.id
            return conversation

    def resolve_surface(
        request: object,
        current_conversation: object,
        source: object,
        assembled: object,
        policy: object,
        segment: object,
    ) -> object:
        del request, current_conversation, source
        messages = tuple(
            value if isinstance(value, Message) else Message(role="user", content=str(value))
            for value in assembled
        )
        return build_segment_surface_gate(
            messages,
            catalog=getattr(policy, "catalog"),
            context=getattr(segment, "context"),
            authority=getattr(segment, "authority"),
            dependency_policy=getattr(policy, "dependency_policy"),
            policy=getattr(policy, "policy"),
        )

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Gateway(),
            persistence=coordinator,
            policy_catalog_resolver=lambda request, conversation, source: ResolvedPolicyCatalog(
                catalog=MODEL_TOOL_CATALOG,
                policy=_AUTHORITY_POLICY,
                dependency_policy=DEPENDENCY_POLICY_V1,
            ),
            segment_context_resolver=lambda request, conversation, source, recorder: _real_segment(
                conversation, recorder, MODEL_TOOL_CATALOG
            )[0],
            surface_gate_resolver=resolve_surface,
            continuation_model_resolver=lambda request, conversation, policy: ResolvedModel(
                model="model"
            ),
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


def test_journal_persisted_events_skip_user_messages() -> None:
    phases = _Phases()

    class RolePersistence(_Persistence):
        def list_messages(self, conversation_id: int) -> tuple[object, ...]:
            del conversation_id
            return (
                SimpleNamespace(id=11, role="user"),
                SimpleNamespace(id=12, role="assistant"),
                SimpleNamespace(id=13, role="tool"),
            )

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id, messages
            self.message_count += 1
            return PersistenceResult(
                PersistenceStatus.PERSISTED,
                message_ids=(11, 12, 13),
            )

    persistence = RolePersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    persisted_ids = {
        getattr(event, "source_ref_id", None)
        for event in journal.recorder.events
        if getattr(event, "event_type", None) == "assistant.persisted"
    }
    assert persisted_ids == {12, 13}


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
    pending = SimpleNamespace(
        tool_call_id="call-1", tool_name="write", args="{}", human="write", operation_id="op-1"
    )
    runtime, persistence, _ = _runtime(phases, persistence=_Persistence(phases, pending=pending))

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED
    assert persistence.user_count == 0
    assert "agent_host" not in phases.items


def test_source_failure_stops_before_user_and_journal() -> None:
    phases = _Phases()
    control = InMemoryRuntimeInvocationControl()
    runtime, persistence, journal = _runtime(
        phases,
        source=_Source(phases, error=RuntimeError("source failed")),
    )

    result = _start(runtime, _Host(phases), control=control)

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.SOURCE_LOAD_FAILED
    assert persistence.user_count == 0
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == []
    assert control.state is InvocationState.COMPLETED


@pytest.mark.parametrize("failure_mode", ["exception", "none", "failed"])
def test_initial_user_persist_failure_is_safe_before_journal_or_agent(
    failure_mode: str,
) -> None:
    phases = _Phases()

    class FailingUserPersistence(_Persistence):
        def persist_initial_user_message(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            if failure_mode == "exception":
                raise OSError("user persist secret")
            if failure_mode == "none":
                return None
            return PersistenceResult(PersistenceStatus.CAS_LOST)

    persistence = FailingUserPersistence(phases)
    driver = _Driver(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=driver,
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert result.status_code == 503
    assert result.retryable is True
    assert "user persist secret" not in result.message
    assert persistence.message_count == 0
    assert driver.provider_calls == 0
    assert journal.recorder.dispositions == []
    assert "run_start" not in phases.items
    assert "source_load" in phases.items
    assert "context_assemble" in phases.items
    assert "policy_resolve" in phases.items
    assert "segment_resolve" in phases.items
    assert "model_resolve" in phases.items
    assert "run_start" not in phases.items
    assert "agent_host" not in phases.items


def test_phase_sink_failure_is_fail_open_before_authoritative_finish() -> None:
    class FailingFinishPhase(_Phases):
        def once(self, name: str) -> None:
            super().once(name)
            if name == "run_finish":
                raise OSError("diagnostic sink unavailable")

        append = once

    phases = FailingFinishPhase()
    runtime, _persistence, journal = _runtime(
        phases,
        source=_Source(phases, error=RuntimeError("source failed")),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.SOURCE_LOAD_FAILED
    assert journal.recorder.dispositions == []


def test_phase_sink_failure_does_not_block_authoritative_abandon() -> None:
    control = InMemoryRuntimeInvocationControl()

    class FailingCancelPhase(_Phases):
        def once(self, name: str) -> None:
            super().once(name)
            if name == "run_finish":
                control.request_cancel(CancelReason.EXPLICIT_CANCEL)
                raise OSError("diagnostic sink unavailable")

        append = once

    phases = FailingCancelPhase()
    runtime, _persistence, journal = _runtime(phases)

    with pytest.raises(RuntimeCancelled):
        runtime.start_turn(
            StartTurnRequest(message="hi"),
            transport=RuntimeTransportContext(mode="sync"),
            execution_host=_Host(phases),
            invocation_control=control,
            cancel_check=lambda: False,
        )

    assert journal.recorder.dispositions == []
    assert journal.recorder.abandoned == 1


def test_timeout_writes_fixed_assistant_message_and_does_not_provider_map() -> None:
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
            self.phases.once("message_persist")
            return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence = TimeoutPersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)
    result = _start(runtime, TimeoutHost(), control=control)  # type: ignore[arg-type]

    assert isinstance(result, MessageOutcome)
    assert result.message
    assert persistence.message_count == 1
    assert journal.recorder.dispositions == [("timed_out", "timeout")]
    assert control.state is InvocationState.TIMED_OUT


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
            return PersistenceResult(PersistenceStatus.CAS_LOST)

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
            return PersistenceResult(PersistenceStatus.PERSISTED, message_id=13)

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
    assert result.code is RuntimeFailureCode.MODEL_UNCONFIGURED
    assert persistence.user_count == 0


def test_model_unconfigured_is_only_mapped_from_closed_signal() -> None:
    phases = _Phases()

    def resolve_model(request: object, conversation: object) -> object:
        del request, conversation
        raise ModelUnconfiguredError()

    runtime, persistence, _journal = _runtime(phases, model_resolver=resolve_model)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.MODEL_UNCONFIGURED
    assert persistence.user_count == 0


def test_model_resolver_exception_is_provider_failure_not_unconfigured() -> None:
    phases = _Phases()

    def resolve_model(request: object, conversation: object) -> object:
        del request, conversation
        raise ValueError("AI is not configured")

    runtime, persistence, _journal = _runtime(phases, model_resolver=resolve_model)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.AI_PROVIDER_ERROR
    assert result.status_code == 502
    assert result.retryable is True
    assert persistence.user_count == 0


def test_resolved_model_with_none_model_is_unconfigured_before_user_persist() -> None:
    phases = _Phases()
    persistence = _Persistence(phases)
    driver = _Driver(phases)

    def resolve_model(request: object, conversation: object) -> object:
        del request, conversation
        return ResolvedModel(model=None)

    runtime, _, _journal = _runtime(
        phases,
        persistence=persistence,
        driver=driver,
        model_resolver=resolve_model,
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.MODEL_UNCONFIGURED
    assert persistence.user_count == 0
    assert driver.provider_calls == 0


def test_missing_persistence_readback_capability_fails_before_user_persist() -> None:
    phases = _Phases()
    persistence = _Persistence(phases)
    persistence.get_pending_clarification = None  # type: ignore[method-assign]
    runtime, _, _journal = _runtime(phases, persistence=persistence)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.user_count == 0


def test_forced_clarification_requires_exact_pending_readback() -> None:
    phases = _Phases()

    class MismatchClarification(_Persistence):
        def get_pending_clarification(self, conversation_id: int) -> object | None:
            del conversation_id
            return SimpleNamespace(
                pending=SimpleNamespace(
                    tool_call_id="other-call",
                    tool_name="update_application_status",
                    args="{}",
                    human="update_application_status",
                    operation_id="",
                ),
                question="other question",
            )

    persistence = MismatchClarification(phases)
    result_value = AgentTurnResult(
        added=[
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall("call-1", "update_application_status", "{}")],
            )
        ],
        reply="",
        pending=None,
        records=(
            SimpleNamespace(
                prepared=SimpleNamespace(spec=SimpleNamespace(kind="write")),
                outcome=SimpleNamespace(code="company_required"),
            ),
        ),
        failures=(ToolFailure("validation_error", "company_required", "company_required"),),
    )
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=result_value),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert journal.recorder.dispositions == [("failed", "unknown")]


def test_journal_persisted_projection_accepts_mapping_snapshots() -> None:
    phases = _Phases()

    class MappingPersistence(_Persistence):
        def list_messages(self, conversation_id: int) -> tuple[object, ...]:
            del conversation_id
            return (
                {"id": 11, "role": "user"},
                {"id": 12, "role": "assistant"},
            )

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id, messages
            self.message_count += 1
            return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=(12,))

    persistence = MappingPersistence(phases)
    runtime, _, journal = _runtime(phases, persistence=persistence)

    result = _start(runtime, _Host(phases))

    assert isinstance(result, MessageOutcome)
    assert any(
        getattr(event, "source_ref_id", None) == 12
        for event in journal.recorder.events
        if getattr(event, "event_type", None) == "assistant.persisted"
    )


def test_commit_fence_cancel_wins_without_running_callback() -> None:
    control = InMemoryRuntimeInvocationControl()
    writes: list[str] = []

    assert control.request_cancel(CancelReason.EXPLICIT_CANCEL)
    committed, value = control.run_if_active(lambda: (writes.append("persisted"), "value")[1])

    assert committed is False
    assert value is None
    assert writes == []


def test_commit_fence_persist_wins_then_cancel_waits_for_commit() -> None:
    control = InMemoryRuntimeInvocationControl()
    entered = Event()
    release = Event()
    writes: list[str] = []
    result: list[tuple[bool, object | None]] = []

    def persist() -> str:
        entered.set()
        assert release.wait(timeout=5)
        writes.append("persisted")
        return "value"

    worker = Thread(target=lambda: result.append(control.run_if_active(persist)))
    worker.start()
    assert entered.wait(timeout=5)
    cancel_result: list[bool] = []
    canceller = Thread(
        target=lambda: cancel_result.append(control.request_cancel(CancelReason.EXPLICIT_CANCEL))
    )
    canceller.start()
    release.set()
    worker.join(timeout=5)
    canceller.join(timeout=5)

    assert not worker.is_alive()
    assert not canceller.is_alive()
    assert result == [(True, "value")]
    assert writes == ["persisted"]
    assert cancel_result == [True]


def test_provider_failure_is_safe_and_finishes_provider_error() -> None:
    phases = _Phases()
    runtime, persistence, journal = _runtime(
        phases, driver=_Driver(phases, error=ValueError("secret"))
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.AI_PROVIDER_ERROR
    assert "secret" not in result.message
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == [("failed", "provider_error")]


def test_same_named_ordinary_exception_is_not_runtime_cancellation() -> None:
    class ChatRunCancelled(Exception):
        pass

    phases = _Phases()
    runtime, persistence, journal = _runtime(
        phases,
        driver=_Driver(phases, error=ChatRunCancelled("ordinary provider failure")),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.AI_PROVIDER_ERROR
    assert persistence.message_count == 0
    assert journal.recorder.dispositions == [("failed", "provider_error")]


def test_pending_result_is_atomically_persisted_and_suspended() -> None:
    phases = _Phases()
    pending = PendingAction(
        "call-1", "update_application_status", '{"id": 1}', "update_application_status", "op-1"
    )
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, ConfirmationRequiredOutcome)
    assert persistence.pending_count == 1
    assert journal.recorder.dispositions == []
    assert phases.items[-1] == "run_suspend"


@pytest.mark.parametrize(
    "pending",
    [
        PendingAction("", "write", "{}", "write", "op-1"),
        PendingAction("call-1", "", "{}", "write", "op-1"),
        PendingAction("call-1", "write", "{}", "write", ""),
        PendingAction("call-1", "unknown", "{}", "write", "op-1"),
        PendingAction("call-1", "read_tool", "{}", "read", "op-1"),
        PendingAction("call-1", "write", "[]", "write", "op-1"),
        PendingAction("call-1", "write", '{"x": NaN}', "write", "op-1"),
    ],
)
def test_invalid_pending_is_rejected_before_any_pending_persistence(pending: object) -> None:
    phases = _Phases()
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.pending_count == 0
    assert persistence.clarification_count == 0
    assert journal.recorder.dispositions == [("failed", "unknown")]


def test_pending_uses_resolved_catalog_not_global_dependency_catalog() -> None:
    phases = _Phases()
    pending = PendingAction("call-1", "write", "{}", "write", "op-1")
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        catalog=MODEL_TOOL_CATALOG,
        dependency_catalog=_Catalog(write_names=("write",)),
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.pending_count == 0
    assert journal.recorder.dispositions == [("failed", "unknown")]


def test_pending_readback_identity_mismatch_is_safe_after_persistence() -> None:
    phases = _Phases()
    pending = PendingAction(
        "call-1", "update_application_status", "{}", "update_application_status", "op-1"
    )

    class MismatchPersistence(_Persistence):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.guard_reads = 0

        def get_pending_action(self, conversation_id: int) -> object | None:
            del conversation_id
            self.guard_reads += 1
            if self.guard_reads == 1:
                return None
            return SimpleNamespace(
                tool_call_id="other-call",
                tool_name="update_application_status",
                args="{}",
                human="update_application_status",
                operation_id="other-op",
            )

    persistence = MismatchPersistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.pending_count == 1
    assert journal.recorder.dispositions == [("failed", "unknown")]


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
    action = PendingAction(
        "call-1", "update_application_status", '{"id":1}', "update_application_status", "op-1"
    )

    class FailingPersistence(_Persistence):
        def persist_initial_pending(
            self,
            conversation_id: int,
            messages: object,
            pending_value: object,
        ) -> object:
            del conversation_id, messages, pending_value
            return PersistenceResult(PersistenceStatus(status))

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id, messages
            return PersistenceResult(PersistenceStatus(status))

    persistence = FailingPersistence(phases)
    result_value = (
        AgentTurnResult([], "", action) if pending else AgentTurnResult([], "final", None)
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
    pending = PendingAction(
        "call-1", "update_application_status", "{}", "update_application_status", "op-1"
    )
    persistence = _Persistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
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
    pending = PendingAction(
        "call-1", "update_application_status", "{}", "update_application_status", "op-1"
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
            return PersistenceResult(PersistenceStatus.CAS_LOST)

        def persist_assistant_message(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            self.assistant_calls += 1
            return PersistenceResult(PersistenceStatus.PERSISTED, message_id=13)

    persistence = FallbackPersistence(phases)
    runtime, _, journal = _runtime(
        phases,
        persistence=persistence,
        driver=_Driver(phases, result=AgentTurnResult([], "", pending)),
        missing_target_question=lambda pending, conversation_id: "请先选择投递目标。",
    )

    result = _start(runtime, _Host(phases))

    assert isinstance(result, RuntimeFailureOutcome)
    assert result.code is RuntimeFailureCode.OPERATION_FAILED
    assert persistence.setter_calls == 0
    assert persistence.assistant_calls == 0
    assert journal.recorder.dispositions == []


def test_final_projection_redacts_internal_tool_names_and_uses_safe_write_error() -> None:
    phases = _Phases()

    class CapturingPersistence(_Persistence):
        def __init__(self, phases: _Phases) -> None:
            super().__init__(phases)
            self.messages: object | None = None

        def persist_initial_messages(self, conversation_id: int, messages: object) -> object:
            del conversation_id
            self.messages = messages
            return PersistenceResult(PersistenceStatus.PERSISTED, message_id=12)

    persistence = CapturingPersistence(phases)
    write_record = SimpleNamespace(
        prepared=SimpleNamespace(spec=SimpleNamespace(kind="write")),
        outcome=SimpleNamespace(code="company_required", compatibility_detail="company_required"),
    )
    result_value = AgentTurnResult(
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
        def execute(self, invocation: object) -> object:
            sink = getattr(invocation, "event_sink", None)
            assert sink is not None
            sink.emit(AssistantMessageEvent(message="hi"))
            return AgentTurnResult([], "hello", None)

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


@pytest.mark.parametrize(
    "control_error", [RuntimeCancelled(), RuntimeTransportAborted(), KeyboardInterrupt()]
)
def test_control_and_base_exceptions_are_rethrown_after_journal_cleanup(
    control_error: BaseException,
) -> None:
    phases = _Phases()
    runtime, _persistence, journal = _runtime(phases)

    with pytest.raises(type(control_error)) as raised:
        _start(runtime, _Host(phases, control_error))

    assert raised.value is control_error
    assert journal.recorder.abandoned == 1


def test_unknown_runtime_dependency_key_is_rejected() -> None:
    with pytest.raises(TypeError, match="unknown runtime dependency"):
        PilotRuntime({"unknown_dependency": object()})
    with pytest.raises(TypeError, match="unknown runtime dependency"):
        PilotRuntime(SimpleNamespace(unknown_dependency=object()))
