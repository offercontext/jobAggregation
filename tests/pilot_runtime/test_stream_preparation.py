from __future__ import annotations

import asyncio
import gc
import inspect
import tempfile
import weakref
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from offerpilot.chat_transport import PreparedStreamGuard, SseAgentExecutionHost
from offerpilot.ai.agent_contracts import AgentTurnResult, PendingAction
from offerpilot.ai.agent_loop import (
    ApprovedContinuationSegment,
    SegmentSurfaceGate,
    build_segment_surface_gate,
)
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    OperationCommitted,
    OperationReplay,
    TerminalPayload,
    WriteOperationError,
)
from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.agent_runtime.journal import NullRunRecorder, RunRecorderFactory
from offerpilot.agent_runtime.keyring import JournalKeyDomain
from offerpilot.db import init_database
from offerpilot.pilot_runtime.persistence import ChatPersistenceCoordinator
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from offerpilot.pilot_runtime.errors import RuntimeCancelled, RuntimeTransportAborted
from offerpilot.pilot_runtime.contracts import (
    AssistantMessageEvent,
    CancelReason,
    CompletedEvent,
    CompletionReason,
    ConfirmationRequest,
    ConfirmationRequiredEvent,
    ErrorEvent,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    MetaEvent,
    OperationReplayOutcome,
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
from offerpilot.pilot_runtime.continuation import (
    ConfirmationApprovedWritePort,
    ConfirmationSession,
)
from offerpilot.pilot_runtime.service import (
    PilotRuntime,
    ResolvedPolicyCatalog,
    ResolvedModel,
    RuntimeDependencies,
    SegmentExecution,
    _PreparedExecutionCell,
    _PreparedStreamState,
    _freeze_stream_value,
    _materialize_stream_value,
)
from offerpilot.ai.types import Message, ToolCall


_AUTHORITY_SESSIONS = init_database(
    Path(tempfile.mkdtemp(prefix="offerpilot-stream-authority-")) / "authority.db"
)
_AUTHORITY_POLICY = validate_startup_policy(MODEL_TOOL_CATALOG.authority_manifest)


def _metadata_bundle() -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    return ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],  # type: ignore[arg-type]
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )


_METADATA_BUNDLE = _metadata_bundle()


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

    def persist_initial_pending(
        self, conversation_id: int, messages: object, pending: object
    ) -> PersistenceResult:
        self.pending = pending
        return self.persist_initial_messages(conversation_id, messages)

    def persist_initial_assistant_message(
        self, conversation_id: int, content: str, **kwargs: object
    ) -> PersistenceResult:
        del conversation_id, content, kwargs
        self.assistant_count += 1
        return PersistenceResult(PersistenceStatus.PERSISTED, message_id=self._persist("assistant"))

    def persist_assistant_message(
        self, conversation_id: int, content: str, **kwargs: object
    ) -> PersistenceResult:
        return self.persist_initial_assistant_message(conversation_id, content, **kwargs)

    def persist_clarification(
        self, conversation_id: int, messages: object, pending: object, question: str
    ) -> PersistenceResult:
        del question
        self.pending = pending
        return self.persist_initial_messages(conversation_id, messages)

    def set_pending_clarification(
        self, conversation_id: int, pending: object, question: str
    ) -> PersistenceResult:
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

    def execute(self, invocation: object) -> object:
        del invocation
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return AgentTurnResult([], "hello", None)


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


class ConfirmationProbe:
    """Ledger-only probe for the stream response-header boundary.

    The probe intentionally does not implement the future continuation
    activation port.  It records the legacy ``approve_modify`` arguments so
    the RED test can pin that approval/session/source material must not cross
    the response-header boundary.
    """

    def __init__(self) -> None:
        operation_id = "operation-stream-task17"
        self.operation = SimpleNamespace(
            id=operation_id,
            conversation_id=7,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="write-1",
            tool_name="update_application_status",
        )
        self.pending = PendingAction(
            "write-1",
            "update_application_status",
            '{"id":1,"status":"offer"}',
            "确认更新状态",
            operation_id,
        )
        self.approve_calls: list[dict[str, object]] = []
        self.cleanup_calls = 0
        self.approval_context = SimpleNamespace(secret="approval-context-canary")
        self.session = SimpleNamespace(
            pending=self.pending,
            state=SimpleNamespace(approval_context=self.approval_context),
        )

    def operation_preheader(self, request: object, **kwargs: object) -> object:
        del request, kwargs
        return SimpleNamespace(operation=self.operation)

    def replay_outcome(self, request: object, **kwargs: object) -> None:
        del request, kwargs
        return None

    def preflight_live(self, request: object, **kwargs: object) -> PendingAction:
        del request, kwargs
        return self.pending

    def approve_modify(self, request: object, **kwargs: object) -> object:
        del request
        self.approve_calls.append(dict(kwargs))
        return self.session

    def cancel_cleanup(self, session: object) -> None:
        assert session is self.session
        self.cleanup_calls += 1


def _policy_resolver(catalog: object) -> object:
    def resolve(request: object, conversation: object, source: object) -> object:
        del request, conversation, source
        return ResolvedPolicyCatalog(
            catalog=catalog,
            policy=_AUTHORITY_POLICY,
            dependency_policy=_METADATA_BUNDLE.discovery_view().policy,
            provider_metadata_view=_METADATA_BUNDLE.provider_view(),
            discovery_metadata_view=_METADATA_BUNDLE.discovery_view(),
            authority_metadata_view=_METADATA_BUNDLE.authority_view(),
        )

    return resolve


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
        segment_id=f"stream-test-{conversation_id}-{id(recorder)}",
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


def _segment_resolver(
    request: object,
    conversation: object,
    source: object,
    recorder: object,
) -> SegmentExecution:
    del request, source
    return _real_segment(conversation, recorder, MODEL_TOOL_CATALOG)[0]


def _surface_resolver(
    request: object,
    conversation: object,
    source: object,
    assembled: object,
    policy: object,
    segment: object,
) -> object:
    del request, conversation, source
    messages = tuple(
        value if isinstance(value, Message) else Message(role="user", content=str(value))
        for value in assembled
    )
    return build_segment_surface_gate(
        messages,
        catalog=getattr(policy, "catalog"),
        context=getattr(segment, "context"),
        authority=getattr(segment, "authority"),
        provider_view=getattr(policy, "provider_metadata_view"),
        discovery_view=getattr(policy, "discovery_metadata_view"),
        authority_metadata_view=getattr(policy, "authority_metadata_view"),
        policy=getattr(policy, "policy"),
    )


def runtime(
    phases: Phases,
    *,
    persistence: Persistence | None = None,
    source: Source | None = None,
    route: str = "model",
    conversation: Conversation | None = None,
    model: object = "model",
    catalog: object | None = None,
    assembled: object | None = None,
    surface_resolver: object | None = None,
    policy_resolver: object | None = None,
    segment_resolver: object | None = None,
    close_counter: list[int] | None = None,
) -> tuple[PilotRuntime, Persistence, Driver, Host, Journal]:
    resolved_persistence = persistence or Persistence()
    driver = Driver()
    host = Host()
    journal = Journal()

    # Segment visibility is issued only against the reviewed typed catalog;
    # pending/readback tests use typed tool names for their assertions.
    resolved_catalog = MODEL_TOOL_CATALOG

    def resolve_segment(
        request: object,
        conversation: object,
        source_value: object,
        recorder: object,
    ) -> SegmentExecution:
        del request, source_value
        return _real_segment(
            conversation,
            recorder,
            resolved_catalog,
            close_counter,
        )[0]

    def resolve(request: object, conversation: object) -> object:
        del request, conversation
        return None if model is None else ResolvedModel(model=model)

    instance = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(conversation),
            persistence=resolved_persistence,
            policy_catalog_resolver=policy_resolver or _policy_resolver(resolved_catalog),
            segment_context_resolver=segment_resolver or resolve_segment,
            surface_gate_resolver=surface_resolver or _surface_resolver,
            continuation_model_resolver=resolve,
            source_loader=source or Source(),
            context_assembler=Assembler(assembled),
            agent_driver=driver,
            journal=journal,
            route_selector=lambda request, conversation: route,
            phase_sink=phases,
        )
    )
    return instance, resolved_persistence, driver, host, journal


def transport() -> RuntimeTransportContext:
    return RuntimeTransportContext(
        mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"
    )


def test_stream_model_preparation_order_and_source_failure_boundary() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(
        phases, source=Source(error=RuntimeError("no"))
    )
    control = InMemoryRuntimeInvocationControl()

    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"), transport=transport(), invocation_control=control
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.status_code == 503
    assert prepared.payload["error_code"] == "source_load_failed"
    assert phases.items == [
        "validate",
        "conversation",
        "route:model",
        "pending_guard",
        "source_load",
    ]
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []
    assert control.state is InvocationState.COMPLETED


def test_stream_segment_failure_stops_before_policy_catalog_and_side_effects() -> None:
    phases = Phases()
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
            dependency_policy=_METADATA_BUNDLE.discovery_view().policy,
            provider_metadata_view=_METADATA_BUNDLE.provider_view(),
            discovery_metadata_view=_METADATA_BUNDLE.discovery_view(),
            authority_metadata_view=_METADATA_BUNDLE.authority_view(),
        )

    instance, persistence, driver, host, journal = runtime(
        phases,
        segment_resolver=failing_segment,
        policy_resolver=policy_spy,
    )
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.payload["error_code"] == "source_load_failed"
    assert policy_calls == []
    assert "context_assemble" not in phases.items
    assert "policy_resolve" not in phases.items
    assert "surface_resolve" not in phases.items
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []
    assert journal.recorder.abandoned == 0


def test_stream_live_policy_drift_closes_segment_before_provider_or_user() -> None:
    phases = Phases()
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
            dependency_policy=_METADATA_BUNDLE.discovery_view().policy,
            provider_metadata_view=_METADATA_BUNDLE.provider_view(),
            discovery_metadata_view=_METADATA_BUNDLE.discovery_view(),
            authority_metadata_view=_METADATA_BUNDLE.authority_view(),
        )

    instance, persistence, driver, host, journal = runtime(
        phases,
        policy_resolver=drift_policy,
        close_counter=close_count,
    )
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.payload["error_code"] == "operation_unavailable"
    assert close_count == [1]
    assert "surface_resolve" not in phases.items
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []
    assert journal.recorder.abandoned == 0
    assert instance._prepared_models == {}  # type: ignore[attr-defined]
    assert instance._prepared_segments == {}  # type: ignore[attr-defined]


def test_stream_policy_spy_sees_exact_unbound_segment_after_segment_phase() -> None:
    phases = Phases()
    seen: list[tuple[list[str], object]] = []

    def policy_spy(
        request: object,
        conversation: object,
        source: object,
        segment: object,
    ) -> object:
        del request, conversation, source
        snapshot = list(phases.items)
        phases.append("policy_manifest_read")
        seen.append((snapshot, segment))
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
            dependency_policy=_METADATA_BUNDLE.discovery_view().policy,
            provider_metadata_view=_METADATA_BUNDLE.provider_view(),
            discovery_metadata_view=_METADATA_BUNDLE.discovery_view(),
            authority_metadata_view=_METADATA_BUNDLE.authority_view(),
        )

    def malformed_gate(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    instance, persistence, driver, host, journal = runtime(
        phases,
        policy_resolver=policy_spy,
        surface_resolver=malformed_gate,
    )
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.payload["error_code"] == "operation_unavailable"
    assert len(seen) == 1
    snapshot, _segment = seen[0]
    assert snapshot.index("segment_resolve") < snapshot.index("policy_resolve")
    assert phases.items.index("policy_manifest_read") > phases.items.index("segment_resolve")
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []


def test_stream_malformed_surface_gate_closes_before_model_or_user() -> None:
    phases = Phases()
    close_count: list[int] = []

    def malformed_gate(*_args: object, **_kwargs: object) -> object:
        return object()

    instance, persistence, driver, host, journal = runtime(
        phases,
        surface_resolver=malformed_gate,
        close_counter=close_count,
    )
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.payload["error_code"] == "operation_unavailable"
    assert close_count == [1]
    assert "model_resolve" not in phases.items
    assert persistence.user_count == 0
    assert driver.calls == 0
    assert host.calls == 0
    assert journal.recorder.finished == []


@pytest.mark.parametrize(
    ("field_name", "error"),
    [
        ("status", ValueError("status getter")),
        ("status", KeyboardInterrupt()),
        ("message_id", ValueError("message id getter")),
        ("message_id", KeyboardInterrupt()),
    ],
)
def test_stream_user_result_getter_releases_segment_on_any_exception(
    field_name: str,
    error: BaseException,
) -> None:
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

    class PersistenceWithExplodingResult(Persistence):
        def persist_initial_user_message(self, conversation_id: int, content: str) -> object:
            del conversation_id, content
            return ExplodingResult()

    phases = Phases()
    persistence = PersistenceWithExplodingResult()
    instance, _unused, _driver, _host, _journal = runtime(
        phases,
        persistence=persistence,
        close_counter=close_count,
    )

    with pytest.raises(type(error)):
        instance.prepare_stream(
            StartTurnRequest(message="hi"),
            transport=transport(),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
    assert close_count == [1]
    assert instance._prepared_models == {}  # type: ignore[attr-defined]
    assert instance._prepared_segments == {}  # type: ignore[attr-defined]


@pytest.mark.parametrize("error", [ValueError("persistence surface"), KeyboardInterrupt()])
def test_stream_persistence_surface_getter_releases_segment_on_any_exception(
    error: BaseException,
) -> None:
    close_count: list[int] = []

    class PersistenceWithExplodingSurface(Persistence):
        def __getattribute__(self, name: str) -> object:
            if name == "persist_initial_assistant_message":
                raise error
            return super().__getattribute__(name)

    persistence = PersistenceWithExplodingSurface()
    phases = Phases()
    instance, _unused, _driver, _host, _journal = runtime(
        phases,
        persistence=persistence,
        close_counter=close_count,
    )

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            instance.prepare_stream(
                StartTurnRequest(message="hi"),
                transport=transport(),
                invocation_control=InMemoryRuntimeInvocationControl(),
            )
    else:
        result = instance.prepare_stream(
            StartTurnRequest(message="hi"),
            transport=transport(),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(result, ImmediateHttpOutcome)
        assert result.payload["error_code"] == "operation_failed"
    assert close_count == [1]
    assert instance._prepared_models == {}  # type: ignore[attr-defined]
    assert instance._prepared_segments == {}  # type: ignore[attr-defined]


@pytest.mark.parametrize("error", [RuntimeError("freeze"), KeyboardInterrupt()])
def test_stream_assembled_freeze_releases_segment_on_any_exception(
    error: BaseException,
) -> None:
    class ExplodingList(list[object]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            if "context_capture" in phases.items:
                raise error
            return super().__iter__()

    close_count: list[int] = []
    phases = Phases()
    instance, _persistence, _driver, _host, journal = runtime(
        phases,
        assembled=ExplodingList([Message(role="user", content="hi")]),
        close_counter=close_count,
    )

    with pytest.raises(type(error)):
        instance.prepare_stream(
            StartTurnRequest(message="hi"),
            transport=transport(),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
    assert close_count == [1]
    assert instance._prepared_models == {}  # type: ignore[attr-defined]
    assert instance._prepared_segments == {}  # type: ignore[attr-defined]
    assert journal.recorder.abandoned == 1


@pytest.mark.parametrize("error", [RuntimeError("conversation getter"), KeyboardInterrupt()])
def test_stream_prepared_conversation_getter_releases_segment_on_any_exception(
    error: BaseException,
) -> None:
    class ExplodingConversation:
        id = 7
        context_type = "workspace"
        context_ref = ""
        archived_at = None

        def __init__(self) -> None:
            self._mode_reads = 0

        @property
        def mode(self) -> str:
            self._mode_reads += 1
            if self._mode_reads >= 3 and "context_capture" in phases.items:
                raise error
            return "general"

    close_count: list[int] = []
    phases = Phases()
    instance, _persistence, _driver, _host, journal = runtime(
        phases,
        conversation=ExplodingConversation(),  # type: ignore[arg-type]
        close_counter=close_count,
    )

    with pytest.raises(type(error)):
        instance.prepare_stream(
            StartTurnRequest(message="hi"),
            transport=transport(),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
    assert close_count == [1]
    assert instance._prepared_models == {}  # type: ignore[attr-defined]
    assert instance._prepared_segments == {}  # type: ignore[attr-defined]
    assert journal.recorder.abandoned == 1


def test_stream_model_prepare_and_agent_host_execution_emits_baseline_prefix() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"), transport=transport(), invocation_control=control
    )

    assert isinstance(prepared, PreparedStreamExecution)
    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]
    assert prepared.preparation_kind is PreparationKind.MODEL
    assert prepared.execution_mode is StreamExecutionMode.AGENT_HOST
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
        "transport_identity",
        "run_start",
        "context_capture",
        "prepared",
    ]
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
    assert isinstance(result, MessageOutcome)
    assert result.message == "hello"
    assert persistence.user_count == 1
    assert persistence.assistant_count == 1
    assert driver.calls == 1
    assert host.calls == 1
    assert [type(event) for event in seen[:3]] == [MetaEvent, UserMessageSavedEvent, StatusEvent]
    assert isinstance(seen[-1], CompletedEvent)
    assert guard.complete(CompletionReason.NORMAL) is True
    assert control.state is InvocationState.COMPLETED
    assert journal.recorder.finished
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert len(instance._prepared_segments) == 0  # type: ignore[attr-defined]
    assert "agent_host" in phases.items


def test_prepare_rejects_deterministic_route_before_user_or_run() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases, route="deterministic")
    control = InMemoryRuntimeInvocationControl()

    result = instance.prepare_stream(
        StartTurnRequest(message="hi"), transport=transport(), invocation_control=control
    )

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
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    assert guard.execute_once() == outcome
    assert host.calls == 0
    assert driver.calls == 0
    assert seen == [
        MetaEvent(),
        AssistantMessageEvent(message="already committed"),
        CompletedEvent(response=outcome),
    ]
    assert guard.complete(CompletionReason.NORMAL) is True
    assert guard.execute_once() is None
    assert host.calls == 0


def test_direct_terminal_sink_failure_does_not_abandon_precomputed_facts() -> None:
    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    cell = _PreparedExecutionCell(run_open=True)
    abandoned: list[bool] = []

    def on_abort() -> None:
        with cell.lock:
            if not cell.completed:
                abandoned.append(True)
            cell.aborted = True
            cell.run_open = False

    state = _PreparedStreamState(
        owner_token=instance._owner_token,  # type: ignore[attr-defined]
        preparation_kind=PreparationKind.DETERMINISTIC_INITIAL,
        execution_mode=StreamExecutionMode.DIRECT,
        control=control,
        request=StartTurnRequest(message="hi"),
        conversation=None,
        cell=cell,
        events=(MetaEvent(),),
        outcome=MessageOutcome(message="already committed", conversation_id=7),
        on_abort=on_abort,
    )
    prepared = PreparedStreamExecution(
        invocation_id=transport().transport_run_id,
        preparation_kind=PreparationKind.DETERMINISTIC_INITIAL,
        execution_mode=StreamExecutionMode.DIRECT,
        opaque_state=state,
    )

    class FailingSink:
        def emit(self, event: object) -> None:
            if isinstance(event, CompletedEvent):
                raise RuntimeTransportAborted()

    guard = PreparedStreamGuard(
        prepared=prepared,
        execute=lambda: instance.execute_prepared_stream(
            prepared,
            event_sink=FailingSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        ),
    )
    assert guard.begin_execution() is True
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert abandoned == []
    assert cell.completed is True
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True


def test_direct_first_event_abort_closes_invocation_before_projection() -> None:
    phases = Phases()
    instance, _persistence, driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    cell = _PreparedExecutionCell(run_open=True)
    model_token = object()
    instance._prepared_models[model_token] = ResolvedModel(model="prepared-model")  # type: ignore[attr-defined]
    release_calls = 0

    def release_model() -> None:
        nonlocal release_calls
        if model_token in instance._prepared_models:  # type: ignore[attr-defined]
            instance._prepared_models.pop(model_token)  # type: ignore[attr-defined]
            release_calls += 1

    def on_abort() -> None:
        release_model()
        with cell.lock:
            if cell.aborted or cell.completed:
                return
            cell.aborted = True
            cell.run_open = False
        if control.state is InvocationState.ACTIVE:
            control.mark_completed()
        journal.recorder.abandon()

    def on_complete(_reason: CompletionReason) -> None:
        release_model()

    journal.recorder.finished.append("terminal-fact")
    state = _PreparedStreamState(
        owner_token=instance._owner_token,  # type: ignore[attr-defined]
        preparation_kind=PreparationKind.DETERMINISTIC_INITIAL,
        execution_mode=StreamExecutionMode.DIRECT,
        control=control,
        request=StartTurnRequest(message="hi"),
        conversation=None,
        model_token=model_token,
        recorder=journal.recorder,
        journal_started=True,
        cell=cell,
        events=(MetaEvent(),),
        outcome=MessageOutcome(message="already committed", conversation_id=7),
        on_abort=on_abort,
        on_complete=on_complete,
    )
    prepared = PreparedStreamExecution(
        invocation_id=transport().transport_run_id,
        preparation_kind=PreparationKind.DETERMINISTIC_INITIAL,
        execution_mode=StreamExecutionMode.DIRECT,
        opaque_state=state,
    )
    seen: list[object] = []

    class FirstEventFailingSink:
        def emit(self, event: object) -> None:
            seen.append(event)
            raise RuntimeTransportAborted()

    guard = PreparedStreamGuard(
        prepared=prepared,
        execute=lambda: instance.execute_prepared_stream(
            prepared,
            event_sink=FirstEventFailingSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        ),
    )
    assert guard.begin_execution() is True
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert control.state is InvocationState.COMPLETED
    assert control.request_cancel(CancelReason.EXPLICIT_CANCEL) is False
    assert seen == [MetaEvent()]
    assert driver.calls == 0
    assert host.calls == 0
    assert cell.completed is True
    assert journal.recorder.abandoned == 0
    assert journal.recorder.finished == ["terminal-fact"]
    assert release_calls == 1
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True
    assert release_calls == 1


def test_model_abort_keeps_user_and_abandons_open_run_without_new_facts() -> None:
    phases = Phases()
    instance, persistence, _driver, _host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"), transport=transport(), invocation_control=control
    )
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
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"), transport=transport(), invocation_control=control
    )
    assert isinstance(prepared, PreparedStreamExecution)
    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    seen: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            seen.append(event)

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=SseAgentExecutionHost(timeout_seconds=1.0),
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
    assert isinstance(result, MessageOutcome)
    assert driver.calls == 1
    assert [type(item) for item in seen[:3]] == [MetaEvent, UserMessageSavedEvent, StatusEvent]
    assert isinstance(seen[-1], CompletedEvent)
    assert guard.complete(CompletionReason.NORMAL) is True


def test_real_stream_run_recorder_keeps_transport_uuid_and_terminal_events(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    data_dir = tmp_path
    sessions = init_database(data_dir / "offerpilot.db")
    request.addfinalizer(lambda: sessions.kw["bind"].dispose())
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

    # Keep this repository integration assertion deterministic under full-suite
    # load; the real-time budget is covered by dedicated budget tests.
    journal = CapturingFactory(repository, key=key, enabled=True, clock=lambda: 0.0)

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
            policy_catalog_resolver=_policy_resolver(MODEL_TOOL_CATALOG),
            segment_context_resolver=_segment_resolver,
            surface_gate_resolver=_surface_resolver,
            continuation_model_resolver=lambda request, current, policy: ResolvedModel(
                model="model"
            ),
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
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=None,
        signal_sink=None,
        execution_host=Host(),
        cancel_check=lambda: False,
    )
    result = guard.execute_once()

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
    assert guard.complete(CompletionReason.NORMAL) is True


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
            policy_catalog_resolver=_policy_resolver(MODEL_TOOL_CATALOG),
            segment_context_resolver=_segment_resolver,
            surface_gate_resolver=_surface_resolver,
            continuation_model_resolver=lambda request, conversation, policy: ResolvedModel(
                model="model"
            ),
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=FailingSink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert journal.recorder.finished
    assert journal.recorder.abandoned == 0
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert prepared.lifecycle_state.value == "executing"
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True
    assert prepared.lifecycle_state.value == "completed"
    assert prepared.completion_reason is CompletionReason.TRANSPORT_ABORTED
    second_seen: list[object] = []

    class SecondSink:
        def emit(self, event: object) -> None:
            second_seen.append(event)

    assert guard.execute_once() is None
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
            policy_catalog_resolver=_policy_resolver(MODEL_TOOL_CATALOG),
            segment_context_resolver=_segment_resolver,
            surface_gate_resolver=_surface_resolver,
            continuation_model_resolver=resolver,
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=FailingSink(),
        signal_sink=None,
        execution_host=Host(),
        cancel_check=lambda: False,
    )
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    gc.collect()
    assert provider_ref() is None
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True
    assert guard.execute_once() is None
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=FailingSink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    with pytest.raises(type(raised)):
        guard.execute_once()
    assert guard.complete(reason) is True
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    guard.execute_once()
    assert seen[0] == MetaEvent(supports_delta=True)
    assert guard.complete(CompletionReason.NORMAL) is True


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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
    assert getattr(result, "code", None).value == "ai_provider_error"
    assert [type(event) for event in seen] == [
        MetaEvent,
        UserMessageSavedEvent,
        StatusEvent,
        ErrorEvent,
    ]
    assert not any(isinstance(event, CompletedEvent) for event in seen)
    assert guard.complete(CompletionReason.NORMAL) is True


def test_provider_error_sink_abort_releases_before_guard_completion() -> None:
    phases = Phases()
    instance, _persistence, driver, host, journal = runtime(phases)
    driver.error = RuntimeError("provider down")
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    seen: list[object] = []

    class FailingSink:
        def emit(self, event: object) -> None:
            seen.append(event)
            if isinstance(event, ErrorEvent):
                raise RuntimeTransportAborted()

    guard = PreparedStreamGuard(
        prepared=prepared,
        execute=lambda: instance.execute_prepared_stream(
            prepared,
            event_sink=FailingSink(),
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        ),
    )
    assert guard.begin_execution() is True
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]
    assert journal.recorder.finished
    assert prepared.opaque_state.cell.running is False  # type: ignore[union-attr]
    assert prepared.lifecycle_state.value == "executing"
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


def test_runtime_does_not_complete_guard_lifecycle_before_transport_owner() -> None:
    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    cleanup: list[CompletionReason | None] = []
    guard = PreparedStreamGuard(
        prepared=prepared,
        on_cleanup=lambda reason=None: cleanup.append(reason),
    )
    assert guard.begin_execution() is True
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=None,
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
    assert isinstance(result, MessageOutcome)
    assert prepared.lifecycle_state is not None
    assert prepared.lifecycle_state.value == "executing"
    assert cleanup == []
    assert guard.complete(CompletionReason.NORMAL) is True
    assert cleanup == [CompletionReason.NORMAL]


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        (None, CompletionReason.NORMAL),
        (RuntimeCancelled, CompletionReason.CANCELLED),
        (RuntimeTransportAborted, CompletionReason.TRANSPORT_ABORTED),
    ],
)
def test_guard_runtime_completion_owner_cleans_up_once(
    error_type: type[BaseException] | None,
    reason: CompletionReason,
) -> None:
    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    cleanup: list[CompletionReason | None] = []

    class Sink:
        def emit(self, _event: object) -> None:
            if error_type is not None:
                raise error_type()

    guard = PreparedStreamGuard(
        prepared=prepared,
        on_cleanup=lambda completion_reason=None: cleanup.append(completion_reason),
    )
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    assert guard.begin_execution() is True
    if error_type is None:
        guard.execute_once()
    else:
        with pytest.raises(error_type):
            guard.execute_once()
    assert prepared.lifecycle_state.value == "executing"
    assert guard.complete(reason) is True
    assert cleanup == [reason]
    assert prepared.completion_reason is reason
    assert len(instance._prepared_models) == 0  # type: ignore[attr-defined]


def test_bare_execute_after_lifecycle_begin_is_rejected_without_side_effects() -> None:
    phases = Phases()
    instance, persistence, driver, host, journal = runtime(phases)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        StartTurnRequest(message="hi"),
        transport=transport(),
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.begin() is True
    with pytest.raises(RuntimeTransportAborted):
        instance.execute_prepared_stream(
            prepared,
            event_sink=None,
            signal_sink=None,
            execution_host=host,
            cancel_check=lambda: False,
        )
    assert persistence.assistant_count == 0
    assert driver.calls == 0
    assert journal.recorder.finished == []
    assert len(instance._prepared_models) == 1  # type: ignore[attr-defined]


def test_guard_does_not_execute_after_completion_winner() -> None:
    from offerpilot.pilot_runtime.contracts import PreparedLifecycle

    lifecycle = PreparedLifecycle()
    calls: list[str] = []
    guard = PreparedStreamGuard(
        lifecycle=lifecycle,
        execute=lambda: calls.append("execute"),
    )
    assert guard.begin_execution() is True
    assert guard.complete(CompletionReason.NORMAL) is True
    assert guard.execute_once() is None
    assert calls == []


def test_background_finalizer_does_not_complete_active_body_owner() -> None:
    async def scenario() -> tuple[object, object]:
        entered = asyncio.Event()
        release = asyncio.Event()
        lifecycle = PreparedLifecycle()
        guard = PreparedStreamGuard(lifecycle=lifecycle)

        async def content():
            entered.set()
            await release.wait()
            yield b"body"

        response = GuardedStreamingResponse(content(), guard)

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: dict[str, object]) -> None:
            return None

        task = asyncio.create_task(
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
        await entered.wait()
        await response._background_finalizer()
        during = lifecycle.state
        release.set()
        await task
        return during, lifecycle.state

    from offerpilot.chat_transport import GuardedStreamingResponse
    from offerpilot.pilot_runtime.contracts import PreparedLifecycle, PreparedLifecycleState

    during, after = asyncio.run(scenario())
    assert during is PreparedLifecycleState.EXECUTING
    assert after is PreparedLifecycleState.COMPLETED


def test_prepared_message_surface_roundtrip_preserves_projector_fingerprint() -> None:
    original = Message(
        role="user",
        content="context",
        tool_calls=[ToolCall(id="call-1", name="list_applications", args="{}")],
        tool_call_id="tool-1",
        provider_blocks={"provider": {"kind": "surface"}},
        surface_contributor="request_page_context",
        surface_signal="applications",
        surface_revision="revision-1",
        surface_page_kind="application_detail",
        surface_attachment_kinds="resume",
    )
    frozen = _freeze_stream_value(original)
    restored = _materialize_stream_value(frozen)
    assert isinstance(restored, Message)
    assert restored == original


def test_materialize_stream_value_rejects_unknown_detached_values() -> None:
    with pytest.raises(TypeError):
        _materialize_stream_value(object())


def test_stream_pending_emits_waiting_status_before_confirmation() -> None:
    phases = Phases()
    instance, _persistence, driver, host, _journal = runtime(
        phases,
        catalog=MODEL_TOOL_CATALOG,
    )
    driver.result = AgentTurnResult(
        [],
        "",
        PendingAction(
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
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
    assert guard.complete(CompletionReason.NORMAL) is True


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
    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=None,
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    with pytest.raises(RuntimeTransportAborted):
        guard.execute_once()
    assert host.iterator.close_calls == 1
    assert guard.complete(CompletionReason.TRANSPORT_ABORTED) is True


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


def test_stream_approval_port_is_a_no_arg_fresh_segment_activation_boundary() -> None:
    method = getattr(ConfirmationApprovedWritePort, "activate_continuation_segment", None)

    assert callable(method)
    signature = inspect.signature(method)
    assert tuple(signature.parameters) == ("self",)
    assert ApprovedContinuationSegment.__name__ in str(signature.return_annotation)


def test_stream_approval_port_rejects_non_segment_activation_results() -> None:
    class InvalidActivationSession:
        def activate_continuation_segment(self) -> object:
            return object()

    port = ConfirmationApprovedWritePort(InvalidActivationSession())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="ApprovedContinuationSegment"):
        port.activate_continuation_segment()


def test_stream_activation_requires_terminal_and_delivery_ownership() -> None:
    payload = TerminalPayload(
        status="committed",
        result_contract="typed_json_v1",
        result_json="{}",
        visible_result="saved",
        transport_json="{}",
        undo_json=None,
        failure_category=None,
        failure_code=None,
        digest="sha256:" + "0" * 64,
    )
    terminal = OperationCommitted(
        "operation-stream-task17",
        payload,
        DeliveryOwnership("operation-stream-task17", 1, b"owner", "owner-fingerprint"),
    )
    segment = _real_segment(Conversation(), object(), MODEL_TOOL_CATALOG)[0]
    segment = replace(segment, catalog=MODEL_TOOL_CATALOG)
    messages = (
        Message(role="user", content="continue"),
        Message(role="tool", content="saved", tool_call_id="write-1"),
        Message(role="assistant", content="已保存"),
    )
    surface_gate = build_segment_surface_gate(
        messages,
        catalog=MODEL_TOOL_CATALOG,
        context=segment.context,
        authority=segment.authority,
        provider_view=_METADATA_BUNDLE.provider_view(),
        discovery_view=_METADATA_BUNDLE.discovery_view(),
        authority_metadata_view=_METADATA_BUNDLE.authority_view(),
        policy=_AUTHORITY_POLICY,
    )
    bundle = ApprovedContinuationSegment(
        messages=messages,
        model=SimpleNamespace(complete=lambda *_args, **_kwargs: object()),
        catalog=MODEL_TOOL_CATALOG,
        tool_context=segment.context,
        surface_gate=surface_gate,
    )
    state = SimpleNamespace(
        lock=RLock(),
        continuation_segment_activated=False,
        cancelled=False,
        timed_out=False,
        active=True,
        terminal_execution=terminal,
        delivery_ownership=None,
        approval_context=None,
        continuation_segment_builder=lambda: bundle,
    )
    session = ConfirmationSession(
        state=state,
        on_confirmation_attempt=lambda _pending, _prepared: None,
        on_confirmation_result=lambda *_args: None,
        execute_operation=lambda *_args: object(),
        delivery_fence=lambda: state.delivery_ownership is terminal.ownership,
        continuation_segment_builder=lambda: bundle,
    )

    try:
        with pytest.raises(WriteOperationError, match="operation_delivery_unknown"):
            session.activate_continuation_segment()

        state.delivery_ownership = terminal.ownership
        activated = session.activate_continuation_segment()
        assert type(activated) is ApprovedContinuationSegment
        assert activated.messages == messages
        tool_messages = [message for message in activated.messages if message.role == "tool"]
        assert tool_messages == [messages[1]]
        assert len(tool_messages) == 1
        assert state.continuation_segment_activated is True

        with pytest.raises(WriteOperationError, match="operation_delivery_unknown"):
            session.activate_continuation_segment()
    finally:
        segment.close()


def test_stream_approval_prepare_defers_activation_and_does_not_capture_approval_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation must carry only an opaque continuation, never live approval state.

    The approved origin is not executable at the response-header boundary.  A
    later execute phase must obtain the origin terminal/delivery fence first,
    then ask the one-shot port for a fresh Segment bundle.  In particular,
    ``approve_modify`` must not receive the approve-time Conversation or a
    closure over the raw Runtime Source loader.
    """

    phases = Phases()
    source = Source()
    instance, _persistence, _driver, _host, _journal = runtime(phases, source=source)
    probe = ConfirmationProbe()
    object.__setattr__(instance._dependencies, "confirmation_coordinator", probe)
    conversation_reads: list[object] = []

    def load_conversation(_instance: PilotRuntime, request: object) -> Conversation:
        conversation_reads.append(request)
        return Conversation()

    monkeypatch.setattr(PilotRuntime, "_load_confirmation_conversation", load_conversation)
    request = ConfirmationRequest(
        conversation_id=7,
        operation_id=probe.operation.id,
        approved=True,
        confirmation_token="stream-token",
    )

    prepared = instance.prepare_stream(
        request,
        transport=transport(),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, PreparedStreamExecution)
    assert probe.approve_calls == []
    assert conversation_reads == []
    assert source.calls == 0
    state = prepared.opaque_state
    assert getattr(state, "conversation", None) is None
    assert getattr(state, "confirmation_session", None) is None
    assert getattr(state, "confirmation_model", None) is None


def test_stream_deferred_approval_claim_failure_emits_meta_and_closes_control() -> None:
    class BusyConfirmationProbe(ConfirmationProbe):
        def approve_modify(self, request: object, **kwargs: object) -> object:
            del request, kwargs
            raise WriteOperationError("operation_busy", retryable=True)

    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    probe = BusyConfirmationProbe()
    object.__setattr__(instance._dependencies, "confirmation_coordinator", probe)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            operation_id=probe.operation.id,
            approved=True,
            confirmation_token="stream-token",
        ),
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    outcome = guard.execute_once()

    assert getattr(outcome, "code", None).value == "operation_busy"
    assert [type(event) for event in seen] == [MetaEvent, ErrorEvent]
    assert control.state is InvocationState.COMPLETED
    assert host.calls == 0
    assert guard.complete(CompletionReason.NORMAL) is True


def test_stream_deferred_approval_pre_agent_failure_cleans_live_session() -> None:
    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    probe = ConfirmationProbe()
    object.__setattr__(instance._dependencies, "confirmation_coordinator", probe)
    object.__setattr__(instance._dependencies, "agent_driver", None)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            operation_id=probe.operation.id,
            approved=True,
            confirmation_token="stream-token",
        ),
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    outcome = guard.execute_once()

    assert getattr(outcome, "code", None).value == "operation_unavailable"
    assert [type(event) for event in seen] == [MetaEvent, ErrorEvent]
    assert probe.cleanup_calls == 1
    assert control.state is InvocationState.COMPLETED
    assert host.calls == 0
    assert guard.complete(CompletionReason.NORMAL) is True


def test_stream_deferred_approval_claim_race_replays_full_event_sequence() -> None:
    class ReplayConfirmationProbe(ConfirmationProbe):
        def __init__(self) -> None:
            super().__init__()
            self.replay_checks = 0
            self.replay = OperationReplay(
                self.operation.id,
                TerminalPayload(
                    status="committed",
                    result_contract="typed_json_v1",
                    result_json="{}",
                    visible_result="saved",
                    transport_json="{}",
                    undo_json=None,
                    failure_category=None,
                    failure_code=None,
                    digest="sha256:" + "0" * 64,
                ),
                "delivered",
                1,
                None,
                final_message="already committed",
            )

        def replay_outcome(self, request: object, **kwargs: object) -> object | None:
            del request, kwargs
            self.replay_checks += 1
            if self.replay_checks == 1:
                return None
            return OperationReplayOutcome(
                operation_id=self.operation.id,
                conversation_id=7,
                message="already committed",
                status="committed",
                write_status="success",
            )

        def approve_modify(self, request: object, **kwargs: object) -> OperationReplay:
            del request, kwargs
            return self.replay

    phases = Phases()
    instance, _persistence, _driver, host, _journal = runtime(phases)
    probe = ReplayConfirmationProbe()
    object.__setattr__(instance._dependencies, "confirmation_coordinator", probe)
    control = InMemoryRuntimeInvocationControl()
    prepared = instance.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            operation_id=probe.operation.id,
            approved=True,
            confirmation_token="stream-token",
        ),
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

    guard._execute = lambda: instance.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    outcome = guard.execute_once()

    assert isinstance(outcome, OperationReplayOutcome)
    assert [type(event) for event in seen] == [
        MetaEvent,
        AssistantMessageEvent,
        CompletedEvent,
    ]
    assert isinstance(seen[1], AssistantMessageEvent)
    assert seen[1].message == "already committed"
    assert control.state is InvocationState.COMPLETED
    assert host.calls == 0
    assert guard.complete(CompletionReason.NORMAL) is True
