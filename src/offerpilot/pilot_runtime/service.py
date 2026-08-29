"""Transport-independent synchronous Pilot Runtime orchestration.

The service owns the causal order around the existing Agent driver, including
the response-header preparation boundary.  The transport owns the
``AgentExecutionHost`` (and therefore the worker and deadline).  All external
objects are injected through small structural seams so this module does not
need to know about FastAPI, ORM rows, or Agent Loop internals.
"""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from threading import RLock
from types import SimpleNamespace
from typing import Any, Protocol, TypeAlias, cast
from threading import Lock
from uuid import uuid4

from offerpilot.ai.agent_contracts import (
    AgentLoopControlError,
    AgentDriver,
    AgentTurnResult,
    ChatModel,
    ChatRunCancelled,
    PendingAction,
    PendingActionValidationError,
    StalePendingActionError,
)
from offerpilot.ai.agent_loop import (
    AgentLoopInvocation,
    ApprovedContinuationSegment,
    ApprovedWriteSeed,
    NewTurnSeed,
    PendingPresentationSnapshot,
    SegmentSurfaceGate,
)
from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.tool_runtime.catalog import SegmentToolCatalogLease, SegmentToolSpecHandle
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    ToolExecutionRecord,
    ToolFailure,
    ToolSuccess,
    TransientToolRuntimeValue,
)
from offerpilot.ai.tool_runtime.journal import journal_shape_digest
from offerpilot.ai.tool_runtime.metadata import (
    ProviderToolMetadataView,
    ToolAuthorityMetadataView,
    ToolDiscoveryMetadataView,
    ToolMetadataBundleV1,
    ToolOperationMetadataView,
    ToolOperationMetadataPort,
    WriteOperationMetadataV1,
)
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.pending_replay import PendingReplayArgsDecoderV1, PendingReplayIntegrityError
from offerpilot.ai.write_operations import (
    ClarificationPendingRouteHandle,
    LedgerOperationPreheader,
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    PendingPersistenceRouteHandle,
    PendingPersistenceRoutePort,
    PendingRouteIdentityV1,
    TypedPendingRouteHandle,
    WriteOperationError,
    pending_action_identity,
)
from offerpilot.agent_runtime.events import (
    ContextManifestInput,
    normalize_context_identity,
    prepare_event,
)
from offerpilot.agent_runtime.journal import (
    EventInput,
    NullRunRecorder,
    ResumedDisposition,
    StartRunBuilder,
    SuspendedDisposition,
    TerminalDisposition,
)
from offerpilot.repositories.agent_runs import StartRunCommand, StartSegmentCommand

from .contracts import (
    AgentExecutionHost,
    AssistantDeltaEvent,
    AssistantMessageEvent,
    ConfirmationRequiredOutcome,
    ConfirmationRequest,
    ConfirmationRequiredEvent,
    CompletedEvent,
    CompletionReason,
    ErrorEvent,
    ImmutablePayload,
    ImmediateHttpOutcome,
    MessageOutcome,
    MetaEvent,
    OperationReplayOutcome,
    OperationPendingOutcome,
    PreparationKind,
    PreparedLifecycleState,
    PreparedStreamExecution,
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeFailureOutcome,
    InvocationState,
    RuntimeInvocationControl,
    RuntimeOutcome,
    RuntimeSignalSink,
    RuntimeTransportContext,
    SignalEmitResult,
    StartTurnRequest,
    StatusEvent,
    StreamExecutionMode,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageSavedEvent,
    WriteStatus,
    freeze_json_mapping,
)
from .errors import (
    ModelUnconfiguredError,
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeFailureCode,
    RuntimeTransportAborted,
)
from .deterministic import (
    DeterministicExecution,
    DeterministicPilotAdapter,
    _DeterministicConfirmationPreflight,
)
from .event_sink import emit_runtime_event, require_runtime_active
from .continuation import (
    ConfirmationApprovedWritePort,
    ConfirmationCoordinator,
    ConfirmationReplayError,
    ConfirmationSession,
    DeliveryBundle,
)
from .persistence import (
    PendingActionView,
    PendingClarificationView,
    PersistedMessageView,
    PersistenceResult,
    PersistenceStatus,
)


CHAT_TIMEOUT_MESSAGE = "这次处理时间过长，已停止。你可以重试或换一种问法。"
DEFAULT_MAX_ITERATIONS = 20
_CANCELLED_TOOL_RESULT = json.dumps(
    {"status": "cancelled", "message": "用户取消了该操作，未执行。"},
    ensure_ascii=False,
)


class RouteKind(str, Enum):
    MODEL = "model"
    DETERMINISTIC = "deterministic"


class _ContinuationActivationRequest(TransientToolRuntimeValue):
    """Request-free marker for the post-terminal Segment boundary.

    Approval credentials and client-edited fields must never cross the
    one-shot activation port.  The fresh Segment only needs the canonical
    Conversation identity; its scope, Source and policy are reloaded from
    persistence after delivery ownership is established.
    """

    __slots__ = ("_conversation_id",)

    def __init__(self, conversation_id: int) -> None:
        if type(conversation_id) is not int or conversation_id <= 0:
            raise TypeError("continuation activation conversation_id must be positive int")
        object.__setattr__(self, "_conversation_id", conversation_id)

    @property
    def conversation_id(self) -> int:
        return cast(int, object.__getattribute__(self, "_conversation_id"))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("continuation activation marker is immutable")

    def __repr__(self) -> str:
        return "<_ContinuationActivationRequest transient>"


_RuntimeRequest: TypeAlias = StartTurnRequest | _ContinuationActivationRequest


class ConversationGateway(Protocol):
    def create(self, request: StartTurnRequest) -> object: ...

    def load(self, conversation_id: int) -> object | None: ...


class RouteSelector(Protocol):
    def select(self, request: StartTurnRequest, conversation: object) -> object: ...


class PolicyCatalogResolver(Protocol):
    """Resolve the closed Catalog/Policy before any Provider is constructed."""

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        segment: object | None = None,
    ) -> object: ...


class SegmentContextResolver(Protocol):
    """Construct one authority-bound Context from one trusted Source snapshot."""

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        recorder: object,
    ) -> object: ...


class SegmentSurfaceGateResolver(Protocol):
    """Build provider-free selector/authority visibility for one Segment."""

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        assembled: object,
        policy: object,
        segment: object,
    ) -> object: ...


class ContinuationModelResolver(Protocol):
    """Construct a Provider only after Source, Authority and Policy are ready."""

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        policy: object,
    ) -> object: ...


class SourceLoader(Protocol):
    def load(self, conversation: object, request: StartTurnRequest) -> object: ...


class ContextAssembler(Protocol):
    def assemble(
        self,
        source: object,
        conversation: object,
        request: StartTurnRequest,
    ) -> object: ...


class RuntimePersistence(Protocol):
    """Complete Task 6 read/write facade.

    Every method is deliberately listed instead of being discovered by
    duck-typing at the call site.  The Runtime preflights this surface before
    the first user write and treats a missing/readback operation as a closed
    persistence failure.
    """

    def get_pending_action(self, conversation_id: int) -> PendingActionView | None: ...

    def get_pending_clarification(
        self, conversation_id: int
    ) -> PendingClarificationView | None: ...

    def list_messages(self, conversation_id: int) -> tuple[PersistedMessageView, ...]: ...

    def persist_initial_user_message(
        self, conversation_id: int, content: str
    ) -> PersistenceResult: ...

    def persist_initial_assistant_message(
        self,
        conversation_id: int,
        content: str,
        *,
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> PersistenceResult: ...

    def persist_assistant_message(
        self,
        conversation_id: int,
        content: str,
        *,
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> PersistenceResult: ...

    def persist_initial_messages(
        self, conversation_id: int, messages: Sequence[Message]
    ) -> PersistenceResult: ...

    def persist_initial_pending(
        self,
        conversation_id: int,
        messages: Sequence[Message],
        pending: PendingAction,
        *,
        route_handle: PendingPersistenceRouteHandle,
    ) -> PersistenceResult: ...

    def persist_clarification(
        self,
        conversation_id: int,
        messages: Sequence[Message],
        pending: PendingAction,
        question: str,
        *,
        route_handle: ClarificationPendingRouteHandle,
    ) -> PersistenceResult: ...

    def set_pending_clarification(
        self,
        conversation_id: int,
        pending: PendingAction,
        question: str,
        *,
        route_handle: ClarificationPendingRouteHandle,
    ) -> PersistenceResult: ...

    def clear_pending_action(self, conversation_id: int) -> PersistenceResult: ...

    def clear_pending_clarification(self, conversation_id: int) -> PersistenceResult: ...

    def persist_timeout_assistant(
        self, conversation_id: int, content: str
    ) -> PersistenceResult: ...


class JournalFactory(Protocol):
    def start_run(self, command: StartRunCommand | StartRunBuilder) -> object: ...


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Frozen invocation view returned by a model resolver.

    The resolver owns provider/configuration details.  Each runtime Segment
    supplies its authority-bound Catalog and Context separately.
    """

    model: ChatModel | None
    config: object | None = None
    auto_approve: bool = False
    max_iter: int = DEFAULT_MAX_ITERATIONS
    provider_error_message: Callable[[Exception], str] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class NormalizedAgentTurn:
    """Sealed adapter projection of the existing ``AgentTurnResult`` shape."""

    added: tuple[object, ...]
    reply: str
    pending: PendingAction | None
    records: tuple[object, ...] = ()
    failures: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedPolicyCatalog:
    """Provider-free policy/catalog result for one canonical Source snapshot."""

    catalog: object
    policy: object
    dependency_policy: object | None = None
    provider_metadata_view: ProviderToolMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    discovery_metadata_view: ToolDiscoveryMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    authority_metadata_view: ToolAuthorityMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class SegmentExecution:
    """One Segment authority and its exact Context identity.

    The close callback revokes the execution-scoped factory.  Runtime keeps
    this object alive across all model calls and closes it exactly once after
    sync/stream completion or abort.
    """

    authority: object
    context: ToolExecutionContext
    catalog: object | None
    close: Callable[[], object]
    surface_gate: SegmentSurfaceGate | object | None = field(
        default=None, repr=False, compare=False
    )
    policy: ResolvedPolicyCatalog | None = field(default=None, repr=False, compare=False)
    catalog_lease: SegmentToolCatalogLease | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _PersistedTurn:
    outcome: RuntimeOutcome
    message_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedConversation:
    """Detached conversation identity retained by a prepared stream."""

    conversation_id: int
    context_type: str
    context_ref: str
    mode: str


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedToolCall:
    id: str
    name: str
    args: str


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedMessage:
    """Immutable message snapshot used to cross the response-header boundary."""

    role: str
    content: str
    tool_calls: tuple[_PreparedToolCall, ...] = ()
    tool_call_id: str = ""
    provider_blocks: ImmutablePayload = field(default_factory=lambda: freeze_json_mapping({}))
    surface_contributor: str = ""
    surface_signal: str = ""
    surface_revision: str = ""
    surface_page_kind: str = ""
    surface_attachment_kinds: str = ""


class _PreparedExecutionCell:
    """Small mutable cell for one-shot execution and cached direct outcomes."""

    __slots__ = (
        "lock",
        "running",
        "outcome",
        "run_open",
        "aborted",
        "completed",
        "execution_owner",
    )

    def __init__(self, *, run_open: bool) -> None:
        self.lock = Lock()
        self.running = False
        self.outcome: RuntimeOutcome | None = None
        self.run_open = run_open
        self.aborted = False
        self.completed = False
        self.execution_owner: object | None = None


class _PreparedConfirmationCell:
    """Execution-only holder for a deferred approval session.

    The response-header preparation state must not retain a live Conversation
    or Approval authority.  The transport execution phase fills this cell
    after it has reloaded the canonical Conversation and called
    ``approve_modify``.
    """

    __slots__ = ("session", "conversation")

    def __init__(self) -> None:
        self.session: ConfirmationSession | None = None
        self.conversation: object | None = None


class _PreparedModelLease:
    """Independent once-only release gate for a prepared provider token."""

    __slots__ = ("_catalog_lease", "_invocation", "_lock", "_released", "_release")

    def __init__(self, release: Callable[[], object]) -> None:
        self._lock = Lock()
        self._released = False
        self._release = release
        self._catalog_lease: SegmentToolCatalogLease | None = None
        self._invocation: AgentLoopInvocation | None = None

    def bind_catalog_lease(self, catalog_lease: SegmentToolCatalogLease) -> None:
        if type(catalog_lease) is not SegmentToolCatalogLease:
            raise TypeError("prepared model requires exact Segment Catalog lease")
        with self._lock:
            if self._released:
                raise RuntimeError("prepared model lease was already released")
            if self._catalog_lease is not None:
                raise RuntimeError("prepared model Catalog lease was already bound")
            self._catalog_lease = catalog_lease

    def bind_invocation(self, invocation: AgentLoopInvocation) -> None:
        if type(invocation) is not AgentLoopInvocation:
            raise TypeError("prepared model requires exact Agent Loop invocation")
        with self._lock:
            if self._released:
                raise RuntimeError("prepared model lease was already released")
            if self._invocation is not None:
                raise RuntimeError("prepared model invocation was already bound")
        invocation._bind_catalog_release(self._release_owned)
        with self._lock:
            if self._released:
                raise RuntimeError("prepared model lease was already released")
            self._invocation = invocation

    def release_once(self) -> bool:
        with self._lock:
            invocation = self._invocation
        if invocation is not None:
            return invocation._release_catalog_lease_from_runtime()
        return self._release_owned()

    def _release_owned(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
            catalog_lease = self._catalog_lease
            self._catalog_lease = None
            self._invocation = None
        try:
            if catalog_lease is not None:
                catalog_lease.close()
        finally:
            self._release()
        return True


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedStreamState:
    """Runtime-owned opaque state for one response-header preparation."""

    owner_token: object = field(repr=False, compare=False)
    preparation_kind: PreparationKind
    execution_mode: StreamExecutionMode
    control: RuntimeInvocationControl = field(repr=False, compare=False)
    request: StartTurnRequest | ConfirmationRequest = field(repr=False, compare=False)
    conversation: _PreparedConversation | None = field(repr=False, compare=False)
    conversation_id: int | None = field(default=None, compare=False)
    model_token: object | None = field(default=None, repr=False, compare=False)
    segment_token: object | None = field(default=None, repr=False, compare=False)
    segment_lease: _PreparedModelLease | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    surface_gate: SegmentSurfaceGate | object | None = field(
        default=None, repr=False, compare=False
    )
    assembled: tuple[object, ...] = field(default=(), repr=False, compare=False)
    recorder: object = field(default_factory=lambda: _NoopRecorder(), repr=False, compare=False)
    journal_started: bool = field(default=False, compare=False)
    transport: RuntimeTransportContext | None = field(default=None, compare=False)
    cell: _PreparedExecutionCell = field(
        default_factory=lambda: _PreparedExecutionCell(run_open=False),
        repr=False,
        compare=False,
    )
    events: tuple[RuntimeEvent, ...] = field(default=(), repr=False, compare=False)
    outcome: RuntimeOutcome | None = field(default=None, repr=False, compare=False)
    confirmation_session: object | None = field(default=None, repr=False, compare=False)
    confirmation_pending: PendingAction | None = field(default=None, repr=False, compare=False)
    confirmation_cell: _PreparedConfirmationCell = field(
        default_factory=_PreparedConfirmationCell,
        repr=False,
        compare=False,
    )
    on_abort: Callable[[], object] | None = field(default=None, repr=False, compare=False)
    on_complete: Callable[[CompletionReason], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


class _PersistenceReadbackError(RuntimeError):
    """A required detached persistence snapshot was unavailable or invalid."""


_REQUIRED_PERSISTENCE_METHODS = (
    "get_pending_action",
    "get_pending_clarification",
    "list_messages",
    "persist_initial_user_message",
    "persist_initial_assistant_message",
    "persist_assistant_message",
    "persist_initial_messages",
    "persist_initial_pending",
    "persist_clarification",
    "set_pending_clarification",
    "clear_pending_action",
    "clear_pending_clarification",
    "persist_timeout_assistant",
)


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Composition seams used by :class:`PilotRuntime`.

    The aliases are intentional.  During the extraction the composition root
    may call the conversation seam ``conversations`` or ``conversation_store``;
    both names describe the same narrow create/load capability and neither
    leaks a repository or ORM type into this module.
    """

    conversations: ConversationGateway | None = None
    persistence: RuntimePersistence | None = None
    policy_catalog_resolver: PolicyCatalogResolver | None = None
    segment_context_resolver: SegmentContextResolver | None = None
    surface_gate_resolver: SegmentSurfaceGateResolver | None = None
    continuation_model_resolver: ContinuationModelResolver | None = None
    source_loader: SourceLoader | None = None
    context_assembler: ContextAssembler | None = None
    agent_driver: AgentDriver | None = None
    route_selector: RouteSelector | None = None
    journal: JournalFactory | None = None
    catalog: object | None = None
    metadata_bundle: ToolMetadataBundleV1 | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    metadata_components: TransientToolRuntimeValue | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    provider_metadata_view: ProviderToolMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    discovery_metadata_view: ToolDiscoveryMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    authority_metadata_view: ToolAuthorityMetadataView | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    missing_target_question: Callable[..., str | None] | None = None
    conversation_store: ConversationGateway | None = None
    conversation_gateway: ConversationGateway | None = None
    pending_guard: Callable[..., object] | RuntimePersistence | None = None
    validator: Callable[..., object] | None = None
    phase_sink: Callable[[str], None] | None = None
    application_visible: Callable[[int], bool] | None = None
    deterministic: DeterministicPilotAdapter | None = None
    confirmation_coordinator: ConfirmationCoordinator | None = field(
        default=None, repr=False, compare=False
    )
    continuation: ConfirmationCoordinator | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        metadata_values = (
            self.metadata_bundle,
            self.provider_metadata_view,
            self.discovery_metadata_view,
            self.authority_metadata_view,
            self.metadata_components,
        )
        if all(value is None for value in metadata_values):
            return
        if any(value is None for value in metadata_values):
            raise TypeError("Runtime Tool Metadata dependencies must be injected atomically")
        bundle = self.metadata_bundle
        provider = self.provider_metadata_view
        discovery = self.discovery_metadata_view
        authority = self.authority_metadata_view
        components = self.metadata_components
        if (
            type(bundle) is not ToolMetadataBundleV1
            or type(provider) is not ProviderToolMetadataView
            or type(discovery) is not ToolDiscoveryMetadataView
            or type(authority) is not ToolAuthorityMetadataView
            or not isinstance(components, TransientToolRuntimeValue)
        ):
            raise ValueError("Runtime Tool Metadata dependencies have mixed Bundle provenance")
        exact_bundle = bundle
        exact_provider = provider
        exact_discovery = discovery
        exact_authority = authority
        if (
            self.catalog is not exact_bundle._typed_catalog
            or getattr(components, "bundle", None) is not exact_bundle
            or exact_provider is not exact_bundle.provider_view()
            or exact_discovery is not exact_bundle.discovery_view()
            or exact_authority is not exact_bundle.authority_view()
        ):
            raise ValueError("Runtime Tool Metadata dependencies have mixed Bundle provenance")
        deterministic = self.deterministic
        component_operation_port = getattr(components, "operation_port", None)
        component_pending_port = getattr(components, "pending_persistence_route_port", None)
        if (
            type(component_operation_port) is not ToolOperationMetadataPort
            or component_operation_port.bundle_instance_token
            is not exact_bundle.bundle_instance_token
            or type(component_pending_port) is not PendingPersistenceRoutePort
            or component_pending_port.bundle_instance_token
            is not exact_bundle.bundle_instance_token
        ):
            raise ValueError("Runtime Tool Metadata dependencies have mixed Bundle provenance")
        if (
            type(deterministic) is DeterministicPilotAdapter
            and deterministic.dependencies.operation_port is not component_operation_port
        ):
            raise ValueError("Runtime Tool Metadata dependencies have mixed Bundle provenance")
        for coordinator in (self.confirmation_coordinator, self.continuation):
            if type(coordinator) is not ConfirmationCoordinator:
                continue
            coordinator_dependencies = coordinator.dependencies
            if (
                coordinator_dependencies.operation_port is not component_operation_port
                or coordinator_dependencies.pending_persistence_route_port
                is not component_pending_port
            ):
                raise ValueError("Runtime Tool Metadata dependencies have mixed Bundle provenance")


RuntimeDependenciesLike: TypeAlias = RuntimeDependencies | Mapping[str, object]
PilotRuntimeDependencies = RuntimeDependencies
StartTurnDependencies = RuntimeDependencies
PilotRuntimeDeps = RuntimeDependencies


class _NoopRecorder:
    run_id = None
    segment_id = None
    diagnostics: list[str] = []

    def abandon(self) -> None:
        return None

    def finish(self, *_args: object, **_kwargs: object) -> None:
        return None

    def suspend(self, *_args: object, **_kwargs: object) -> None:
        return None


class _RuntimeRecorderProxy:
    """Stable recorder identity for a Context built before journal start."""

    __slots__ = ("_delegate",)

    def __init__(self, delegate: object | None = None) -> None:
        self._delegate = delegate or _NoopRecorder()

    def set_delegate(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _NoopEventSink:
    def emit(self, _event: RuntimeEvent) -> None:
        return None


class _SafeEventSink:
    __slots__ = ("_sink",)

    def __init__(self, sink: RuntimeEventSink) -> None:
        self._sink = sink

    def emit(self, event: RuntimeEvent) -> None:
        emit_runtime_event(self._sink, event)


class _SafeSignalSink:
    __slots__ = ("_sink",)

    def __init__(self, sink: RuntimeSignalSink[str]) -> None:
        self._sink = sink

    def try_emit(self, signal: str) -> SignalEmitResult:
        try:
            return self._sink.try_emit(signal)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return SignalEmitResult.DEGRADED


class _ConfirmationEventSink:
    """Fence the approved origin ToolResult until delivery succeeds."""

    __slots__ = ("_sink", "_origin_tool_call_id", "_deferred")

    def __init__(
        self,
        sink: RuntimeEventSink,
        origin_tool_call_id: str,
        deferred: list[RuntimeEvent],
    ) -> None:
        self._sink = sink
        self._origin_tool_call_id = origin_tool_call_id
        self._deferred = deferred

    def emit(self, event: RuntimeEvent) -> None:
        if isinstance(event, ToolResultEvent) and event.tool_call_id == self._origin_tool_call_id:
            if event not in self._deferred:
                self._deferred.append(event)
            return
        emit_runtime_event(self._sink, event)


class _NegotiatedConfirmationEventSink:
    """Emit confirmation stream metadata from the activated Segment model.

    A live approval cannot resolve its continuation model until the origin
    write is terminal and owns delivery.  Buffer the small pre-activation
    event prefix, then publish ``MetaEvent`` first with the fresh model's
    actual streaming capability.  This keeps transport negotiation truthful
    without resolving Source/model before the approved executor runs.
    """

    __slots__ = (
        "_buffered",
        "_deferred",
        "_negotiated",
        "_origin_tool_call_id",
        "_sink",
        "_lock",
    )

    def __init__(
        self,
        sink: RuntimeEventSink,
        origin_tool_call_id: str,
        deferred: list[RuntimeEvent],
    ) -> None:
        self._sink = sink
        self._origin_tool_call_id = origin_tool_call_id
        self._deferred = deferred
        self._buffered: list[RuntimeEvent] = []
        self._negotiated = False
        self._lock = RLock()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            if (
                isinstance(event, ToolResultEvent)
                and event.tool_call_id == self._origin_tool_call_id
            ):
                if event not in self._deferred:
                    self._deferred.append(event)
                return
            if not self._negotiated:
                self._buffered.append(event)
                return
            emit_runtime_event(self._sink, event)

    def negotiate(self, model: object | None = None) -> None:
        with self._lock:
            if self._negotiated:
                return
            supports_delta = callable(getattr(model, "stream_complete", None)) or any(
                isinstance(event, AssistantDeltaEvent) for event in self._buffered
            )
            buffered = tuple(self._buffered)
            self._buffered.clear()
            self._negotiated = True
            emit_runtime_event(
                self._sink,
                MetaEvent(supports_delta=supports_delta),
            )
            emit_runtime_event(
                self._sink,
                StatusEvent(phase="tool_running", label="正在执行确认操作"),
            )
            for event in buffered:
                emit_runtime_event(self._sink, event)


def _callable(target: object | None, names: tuple[str, ...]) -> Callable[..., object] | None:
    if target is None:
        return None
    if callable(target):
        return cast(Callable[..., object], target)
    for name in names:
        try:
            candidate = getattr(target, name)
        except AttributeError:
            continue
        if callable(candidate):
            return cast(Callable[..., object], candidate)
    return None


def _attribute(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except AttributeError:
        return default


def _invoke(
    function: Callable[..., object],
    values: Mapping[str, object],
    positional_fallback: tuple[object, ...] = (),
    *,
    var_keyword_values: Mapping[str, object] | None = None,
) -> object:
    """Call a narrow injected seam without guessing from caught ``TypeError``.

    Signature inspection happens before the call, so a ``TypeError`` raised by
    the function body remains the function's own error and is never retried
    with a different argument shape.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*positional_fallback)

    parameters = tuple(signature.parameters.values())

    def composed_call() -> tuple[tuple[object, ...], dict[str, object]]:
        args: list[object] = []
        kwargs: dict[str, object] = {}
        fallback_index = 0
        has_var_keyword = False
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                args.extend(positional_fallback[fallback_index:])
                fallback_index = len(positional_fallback)
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_var_keyword = True
                continue
            if parameter.name in values:
                value = values[parameter.name]
            elif fallback_index < len(positional_fallback):
                value = positional_fallback[fallback_index]
                fallback_index += 1
            elif parameter.default is inspect.Parameter.empty:
                continue
            else:
                continue
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = value
            else:
                args.append(value)
        if has_var_keyword:
            keyword_values = dict(values)
            keyword_values.update(var_keyword_values or {})
            for name, value in keyword_values.items():
                if name not in kwargs and name not in signature.parameters:
                    kwargs[name] = value
        return tuple(args), kwargs

    def named_call() -> tuple[tuple[object, ...], dict[str, object]]:
        args: list[object] = []
        kwargs: dict[str, object] = {}
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                if parameter.name in values:
                    args.append(values[parameter.name])
                continue
            if (
                parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
                and parameter.name in values
            ):
                kwargs[parameter.name] = values[parameter.name]
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            keyword_values = dict(values)
            keyword_values.update(var_keyword_values or {})
            for name, value in keyword_values.items():
                if name not in signature.parameters:
                    kwargs[name] = value
        return tuple(args), kwargs

    candidates: tuple[tuple[tuple[object, ...], dict[str, object]], ...] = (
        composed_call(),
        named_call(),
        (tuple(positional_fallback), {}),
    )
    for args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        # The injected body is entered exactly once.  In particular, a body
        # TypeError is never mistaken for a binding failure and retried.
        return function(*args, **kwargs)
    raise TypeError("injected callable does not accept a supported argument shape")


def _conversation_id(conversation: object) -> int | None:
    value = _attribute(conversation, "id", _attribute(conversation, "conversation_id"))
    if type(value) is int:
        return value
    return None


def _title_from_message(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:80] or "新对话"


def _is_archived(conversation: object) -> bool:
    archived_at = _attribute(conversation, "archived_at")
    if archived_at is not None:
        return True
    return _attribute(conversation, "archived", False) is True


def _route_kind(value: object, request: StartTurnRequest) -> RouteKind:
    # An explicit client action is always handled by the trusted bridge.  The
    # bridge performs the closed-name/schema check; routing it to a model on a
    # selector mistake would turn an invalid client control into a fallback.
    if request.pilot_action is not None:
        return RouteKind.DETERMINISTIC
    if value is None:
        return RouteKind.MODEL
    if isinstance(value, RouteKind):
        return value
    raw = _attribute(value, "kind", _attribute(value, "route", value))
    text = getattr(raw, "value", raw)
    if type(text) is not str:
        raise ValueError("unsupported runtime route")
    if text == RouteKind.MODEL.value:
        return RouteKind.MODEL
    if text == RouteKind.DETERMINISTIC.value:
        return RouteKind.DETERMINISTIC
    raise ValueError("unsupported runtime route")


def _result_persisted(result: object) -> bool:
    if type(result) is bool:
        return result
    status = _attribute(result, "status")
    return status is PersistenceStatus.PERSISTED


def _timeout_result_persisted(result: object) -> bool:
    """Require explicit success before exposing the timeout assistant reply."""

    return _result_persisted(result)


def _failure_status(result: object) -> str:
    status = _attribute(result, "status")
    return str(getattr(status, "value", status or ""))


def _message(value: object) -> Message:
    if isinstance(value, Message):
        return value
    if isinstance(value, Mapping):
        role = str(value.get("role") or "assistant")
        content = str(value.get("content") or "")
        raw_calls = value.get("tool_calls") or []
        tool_calls = (
            [_tool_call(item) for item in raw_calls]
            if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes))
            else []
        )
        return Message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=str(value.get("tool_call_id") or ""),
            provider_blocks=dict(value.get("provider_blocks") or {})
            if isinstance(value.get("provider_blocks"), Mapping)
            else {},
            surface_contributor=str(value.get("surface_contributor") or ""),
            surface_signal=str(value.get("surface_signal") or ""),
            surface_revision=str(value.get("surface_revision") or ""),
            surface_page_kind=str(value.get("surface_page_kind") or ""),
            surface_attachment_kinds=str(value.get("surface_attachment_kinds") or ""),
        )
    raw_tool_calls = _attribute(value, "tool_calls", ())
    tool_calls = (
        [_tool_call(item) for item in cast(Sequence[object], raw_tool_calls)]
        if isinstance(raw_tool_calls, Sequence) and not isinstance(raw_tool_calls, (str, bytes))
        else []
    )
    raw_provider_blocks = _attribute(value, "provider_blocks", {})
    provider_blocks = (
        dict(cast(Mapping[str, object], raw_provider_blocks))
        if isinstance(raw_provider_blocks, Mapping)
        else {}
    )
    return Message(
        role=str(_attribute(value, "role", "assistant")),
        content=str(_attribute(value, "content", "") or ""),
        tool_calls=tool_calls,
        tool_call_id=str(_attribute(value, "tool_call_id", "") or ""),
        provider_blocks=provider_blocks,
        surface_contributor=str(_attribute(value, "surface_contributor", "") or ""),
        surface_signal=str(_attribute(value, "surface_signal", "") or ""),
        surface_revision=str(_attribute(value, "surface_revision", "") or ""),
        surface_page_kind=str(_attribute(value, "surface_page_kind", "") or ""),
        surface_attachment_kinds=str(_attribute(value, "surface_attachment_kinds", "") or ""),
    )


def _freeze_stream_value(value: object) -> object:
    """Snapshot source/context values without retaining mutable containers."""

    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("stream preparation requires finite numbers")
        return value
    if isinstance(value, Message):
        text_fields = (
            value.role,
            value.content,
            value.tool_call_id,
            value.surface_contributor,
            value.surface_signal,
            value.surface_revision,
            value.surface_page_kind,
            value.surface_attachment_kinds,
        )
        if any(type(item) is not str for item in text_fields):
            raise TypeError("stream message contains an unsupported field")
        if not isinstance(value.provider_blocks, Mapping):
            raise TypeError("stream message provider blocks must be a mapping")
        if not isinstance(value.tool_calls, (list, tuple)):
            raise TypeError("stream message tool calls must be a sequence")
        prepared_tool_calls: list[_PreparedToolCall] = []
        for call in value.tool_calls:
            if not isinstance(call, ToolCall):
                raise TypeError("stream message tool calls must be typed values")
            if any(type(item) is not str for item in (call.id, call.name, call.args)):
                raise TypeError("stream message tool call contains an unsupported field")
            prepared_tool_calls.append(_PreparedToolCall(call.id, call.name, call.args))
        blocks = value.provider_blocks
        frozen_blocks = freeze_json_mapping(cast(Mapping[str, object], blocks))
        return _PreparedMessage(
            role=value.role,
            content=value.content,
            tool_calls=tuple(prepared_tool_calls),
            tool_call_id=value.tool_call_id,
            provider_blocks=frozen_blocks,
            surface_contributor=value.surface_contributor,
            surface_signal=value.surface_signal,
            surface_revision=value.surface_revision,
            surface_page_kind=value.surface_page_kind,
            surface_attachment_kinds=value.surface_attachment_kinds,
        )
    if isinstance(value, Mapping):
        return freeze_json_mapping(cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_stream_value(child) for child in value)
    raise TypeError("stream preparation contains a mutable or unsupported value")


def _materialize_stream_value(value: object) -> object:
    """Thaw only the message shape expected by the existing Agent driver."""

    if value is None or type(value) in {str, int, bool, float}:
        return value
    if isinstance(value, _PreparedMessage):
        return Message(
            role=value.role,
            content=value.content,
            tool_calls=[ToolCall(call.id, call.name, call.args) for call in value.tool_calls],
            tool_call_id=value.tool_call_id,
            provider_blocks=dict(value.provider_blocks),
            surface_contributor=value.surface_contributor,
            surface_signal=value.surface_signal,
            surface_revision=value.surface_revision,
            surface_page_kind=value.surface_page_kind,
            surface_attachment_kinds=value.surface_attachment_kinds,
        )
    if isinstance(value, tuple):
        return tuple(_materialize_stream_value(child) for child in value)
    if isinstance(value, Mapping):
        return {str(key): _materialize_stream_value(child) for key, child in value.items()}
    raise TypeError("prepared stream contains an unsupported detached value")


def _prepared_conversation(value: object, conversation_id: int) -> _PreparedConversation:
    return _PreparedConversation(
        conversation_id=conversation_id,
        context_type=str(_attribute(value, "context_type", "workspace") or "workspace"),
        context_ref=str(_attribute(value, "context_ref", "") or ""),
        mode=str(_attribute(value, "mode", "general") or "general"),
    )


def _tool_call(value: object) -> ToolCall:
    if isinstance(value, ToolCall):
        return value
    if isinstance(value, Mapping):
        return ToolCall(
            str(value.get("id") or ""),
            str(value.get("name") or ""),
            str(value.get("args") or ""),
        )
    return ToolCall(
        str(_attribute(value, "id", "") or ""),
        str(_attribute(value, "name", "") or ""),
        str(_attribute(value, "args", "") or ""),
    )


def _canonical_tool_args(value: object) -> str | None:
    """Return canonical persisted proposal bytes for history identity checks."""

    if type(value) is not str:
        return None
    try:
        decoded = PendingReplayArgsDecoderV1().decode(value)
    except PendingReplayIntegrityError:
        return None
    try:
        return json.dumps(
            decoded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None


def _pending(value: object) -> PendingAction | None:
    if value is None:
        return None
    if isinstance(value, PendingAction):
        return value
    return PendingAction(
        tool_call_id=str(_attribute(value, "tool_call_id", _attribute(value, "call_id", "")) or ""),
        tool_name=str(_attribute(value, "tool_name", "") or ""),
        args=str(_attribute(value, "args", "") or ""),
        human=str(_attribute(value, "human", "") or ""),
        operation_id=str(_attribute(value, "operation_id", "") or ""),
    )


def _confirmation_token(pending: PendingAction) -> str:
    """Return the exact token used by the legacy Chat confirmation helper."""

    try:
        parsed_args = json.loads(pending.args)
        canonical_args = json.dumps(
            parsed_args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        canonical_args = pending.args
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, canonical_args],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(identity.encode("utf-8")).hexdigest()


_USER_FACING_TOOL_NAMES = {
    "update_application_status": "更新投递状态",
    "create_application_event": "添加投递日程",
    "update_application_event": "更新投递日程",
    "delete_application_event": "删除投递日程",
    "add_application": "新建投递记录",
    "create_application": "新建投递记录",
    "add_note": "添加复盘记录",
    "update_note": "更新复盘记录",
    "delete_note": "删除复盘记录",
}


def _user_facing_assistant_content(content: str) -> str:
    """Apply the baseline's internal-tool-name redaction to assistant text."""

    if not content:
        return content
    sanitized = content
    for internal_name, label in _USER_FACING_TOOL_NAMES.items():
        sanitized = sanitized.replace(f"`{internal_name}`", label)
        sanitized = sanitized.replace(internal_name, label)
    return sanitized


def _record_outcome(record: object) -> object | None:
    return _attribute(record, "outcome")


def _record_is_write(
    record: object,
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
) -> bool:
    if type(record) is not ToolExecutionRecord:
        return False
    return _published_typed_write(
        operation_view,
        provider_view,
        record.prepared.spec.name,
    )


def _failure_detail(failure: object) -> str:
    detail = _attribute(failure, "compatibility_detail", "")
    if isinstance(detail, str) and detail:
        return detail
    code = _attribute(failure, "code", "operation_failed")
    return str(code)


def _with_write_error_followup(
    added: Sequence[object],
    records: Sequence[object],
    failures: Sequence[object],
) -> tuple[list[Message], str]:
    followup = _write_error_followup(records, failures)
    updated = [_message(item) for item in added]
    if not followup:
        return updated, ""
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if message.role == "assistant" and not message.tool_calls:
            updated[index] = Message(
                role="assistant",
                content=followup,
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
                provider_blocks=message.provider_blocks,
                surface_contributor=message.surface_contributor,
                surface_signal=message.surface_signal,
                surface_revision=message.surface_revision,
                surface_page_kind=message.surface_page_kind,
                surface_attachment_kinds=message.surface_attachment_kinds,
            )
            return updated, followup
    updated.append(Message(role="assistant", content=followup))
    return updated, followup


def _write_error_followup(records: Sequence[object], failures: Sequence[object]) -> str:
    recorded_failures = tuple(
        outcome
        for outcome in (_record_outcome(record) for record in records)
        if isinstance(outcome, ToolFailure)
    )
    for failure in reversed((*recorded_failures, *failures)):
        code = str(_attribute(failure, "code", ""))
        if code == "unclear_note_date":
            return "这次复盘的具体面试日期还不明确。请告诉我具体日期，或回复“日期待定”确认先按待定保存。"
        if code == "company_required":
            return "这次复盘还缺少公司信息。请告诉我公司名称，或先说明不关联具体公司。"
        if code == "new_position_confirmation_required":
            return "我找到同公司已有不同岗位记录。请确认是否为这个新岗位单独新建一条投递记录？确认后我再继续整理。"
    return ""


def _write_outcome(
    records: Sequence[object],
    attempted: bool,
    failures: Sequence[object] = (),
    *,
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
) -> tuple[str, str]:
    if not attempted:
        return "none", ""
    write_records = tuple(
        record for record in records if _record_is_write(record, operation_view, provider_view)
    )
    for record in reversed(write_records):
        outcome = _record_outcome(record)
        if isinstance(outcome, ToolFailure):
            return "failed", _failure_detail(outcome)
        # Keep the projection safe for strict fakes that use a detached
        # failure-like value instead of importing the transient ToolFailure.
        if outcome is not None and _attribute(outcome, "code") is not None:
            return "failed", _failure_detail(outcome)
    if failures:
        return "failed", _failure_detail(failures[-1])
    for record in reversed(write_records):
        outcome = _record_outcome(record)
        result = _attribute(outcome, "result")
        if isinstance(outcome, ToolSuccess) and isinstance(result, dict):
            if result.get("deleted") is False:
                return "failed", "目标记录不存在"
            return "success", ""
        if isinstance(result, dict):
            if result.get("deleted") is False:
                return "failed", "目标记录不存在"
            return "success", ""
    return "failed", "写入未完成"


def _apply_exact_success_presentation(
    reply: str,
    record: object,
) -> str:
    if type(record) is not ToolExecutionRecord:
        return reply
    exact_record = record
    if (
        not isinstance(exact_record.outcome, ToolSuccess)
        or exact_record.terminal_persisted is not True
    ):
        return reply
    summary = exact_record.persisted_visible_result
    if type(summary) is not str:
        return reply
    try:
        json.loads(summary)
    except json.JSONDecodeError:
        pass
    else:
        return reply
    if not summary or summary in reply:
        return reply
    return f"{summary}\n\n{reply}".strip()


def _published_typed_write(
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
    tool_name: str,
) -> bool:
    if (
        type(operation_view) is not ToolOperationMetadataView
        or type(provider_view) is not ProviderToolMetadataView
        or type(tool_name) is not str
        or not tool_name
        or operation_view.bundle_instance_token is not provider_view.bundle_instance_token
    ):
        return False
    entry = operation_view.entries.get(tool_name)
    if entry is None or type(entry.operation) is not WriteOperationMetadataV1:
        return False
    return any(contract.name == tool_name for contract in provider_view.ordered_contracts)


def _published_legacy_write(
    operation_port: ToolOperationMetadataPort | None,
    tool_name: str,
) -> bool:
    if type(operation_port) is not ToolOperationMetadataPort:
        return False
    matches = tuple(
        entry
        for entry in operation_port.legacy_primary_entries
        if entry.operation_name == tool_name
    )
    return bool(
        len(matches) == 1
        and matches[0].adapter_kind == "legacy_deterministic"
        and matches[0].operation_role == "primary"
    )


def _valid_pending_action(
    pending: PendingAction,
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
    *,
    operation_port: ToolOperationMetadataPort | None = None,
    require_operation_id: bool = True,
    trusted_legacy: bool = False,
) -> bool:
    """Validate the closed pending boundary before persistence or suspension."""

    fields: tuple[str, ...] = (pending.tool_call_id, pending.tool_name)
    if require_operation_id:
        fields = (*fields, pending.operation_id)
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        return False
    if not isinstance(pending.args, str):
        return False
    try:
        parsed = json.loads(pending.args)
        if not isinstance(parsed, Mapping):
            return False
        freeze_json_mapping(cast(Mapping[str, object], parsed))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if trusted_legacy:
        if not _published_legacy_write(operation_port, pending.tool_name):
            return False
    elif not _published_typed_write(operation_view, provider_view, pending.tool_name):
        return False
    return bool(_confirmation_token(pending))


def _has_write_attempt(
    added: Sequence[object],
    records: Sequence[object],
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
) -> bool:
    for item in added:
        message = _message(item)
        if message.role == "assistant" and any(
            _published_typed_write(operation_view, provider_view, call.name)
            for call in message.tool_calls
        ):
            return True
    return any(_record_is_write(record, operation_view, provider_view) for record in records)


def _pending_action_from_added_write_call(
    added: Sequence[object],
    operation_view: ToolOperationMetadataView | None,
    provider_view: ProviderToolMetadataView | None,
) -> PendingAction | None:
    for item in reversed(added):
        message = _message(item)
        if message.role != "assistant" or not message.tool_calls:
            continue
        call = message.tool_calls[0]
        if not _published_typed_write(operation_view, provider_view, call.name):
            continue
        return PendingAction(call.id, call.name, call.args, call.name, call.id)
    return None


def _looks_like_followup_question(reply: str) -> bool:
    trimmed = reply.strip()
    return bool(trimmed) and (
        "?" in trimmed or "？" in trimmed or "请告诉我" in trimmed or "请补充" in trimmed
    )


def _normalize_agent_result(value: object) -> NormalizedAgentTurn:
    if not isinstance(value, AgentTurnResult):
        # A mapping, namespace, or arbitrary object is never a turn result.
        # The Agent Driver boundary is intentionally nominal and fail-closed.
        raise TypeError("Agent result is not a sealed turn result")
    return NormalizedAgentTurn(
        tuple(value.added),
        value.reply,
        value.pending,
        tuple(value.records),
        tuple(value.failures),
    )


def _resolved_model(value: object) -> ResolvedModel | None:
    if isinstance(value, ResolvedModel):
        if value.model is None:
            return None
        return value
    if value is None or isinstance(value, RuntimeFailureOutcome):
        return None
    if isinstance(value, tuple) and value:
        model = value[0]
        if model is None or model is False:
            return None
        config = value[1] if len(value) > 1 else None
        return _resolved_model_parts(model, config)
    model = _attribute(value, "model", value)
    if model is None or model is False:
        return None
    config = _attribute(value, "config")
    return _resolved_model_parts(value if model is value else model, config, source=value)


def _explicitly_unconfigured_model(value: object) -> bool:
    """Recognize only a closed ``model=None`` resolver result.

    A resolver exception or an invalid non-null shape is a provider/runtime
    failure, not configuration absence.  This helper therefore inspects only
    explicit model fields and never parses exception text.
    """

    if value is None:
        return True
    if isinstance(value, ResolvedModel):
        return value.model is None
    if isinstance(value, tuple) and value:
        return value[0] is None
    if isinstance(value, Mapping) and "model" in value:
        return value["model"] is None
    try:
        model = getattr(value, "model")
    except AttributeError:
        return False
    return model is None


def _resolved_model_parts(
    model: object, config: object | None, *, source: object | None = None
) -> ResolvedModel:
    origin = source if source is not None else config
    auto_approve = _attribute(
        config, "chat_auto_approve_writes", _attribute(config, "auto_approve", False)
    )
    max_iter = _attribute(
        config, "max_iter", _attribute(config, "max_iterations", DEFAULT_MAX_ITERATIONS)
    )
    return ResolvedModel(
        model=cast(ChatModel, model),
        config=config,
        auto_approve=auto_approve is True,
        max_iter=max_iter if type(max_iter) is int and max_iter > 0 else DEFAULT_MAX_ITERATIONS,
        provider_error_message=(
            cast(Callable[[Exception], str], _attribute(origin, "provider_error_message"))
            if callable(_attribute(origin, "provider_error_message"))
            else None
        ),
    )


def _safe_pending_payload(pending: PendingAction) -> tuple[ImmutablePayload, str]:
    try:
        parsed = json.loads(pending.args or "{}")
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, Mapping):
        parsed = {}
    args = freeze_json_mapping(cast(Mapping[str, object], parsed))
    return args, _confirmation_token(pending)


class PilotRuntime:
    """The synchronous model-only Start Turn state machine."""

    __slots__ = (
        "_dependencies",
        "_owner_token",
        "_prepared_models",
        "_prepared_segments",
    )

    def __init__(
        self,
        dependencies: RuntimeDependenciesLike | None = None,
        **kwargs: object,
    ) -> None:
        self._owner_token = object()
        self._prepared_models: dict[object, ResolvedModel] = {}
        self._prepared_segments: dict[object, SegmentExecution] = {}
        if dependencies is None:
            values = dict(kwargs)
            self._dependencies = RuntimeDependencies(**cast(Any, _dependency_values(values)))
            return
        if kwargs:
            if isinstance(dependencies, RuntimeDependencies):
                values = {
                    field: getattr(dependencies, field)
                    for field in RuntimeDependencies.__dataclass_fields__
                }
            else:
                if isinstance(dependencies, Mapping):
                    values = dict(dependencies)
                else:
                    values = _dependency_object_values(dependencies)
            values.update(kwargs)
            self._dependencies = RuntimeDependencies(**cast(Any, _dependency_values(values)))
        elif isinstance(dependencies, RuntimeDependencies):
            self._dependencies = dependencies
        else:
            if isinstance(dependencies, Mapping):
                values = dict(dependencies)
            else:
                values = {**_dependency_object_values(dependencies)}
            self._dependencies = RuntimeDependencies(**cast(Any, _dependency_values(values)))

    @property
    def metadata_bundle(self) -> ToolMetadataBundleV1:
        bundle = self._dependencies.metadata_bundle
        if type(bundle) is not ToolMetadataBundleV1:
            raise RuntimeError("Pilot Runtime has no production Tool Metadata Bundle")
        _ = bundle.bundle_instance_token
        return bundle

    @property
    def metadata_components(self) -> TransientToolRuntimeValue:
        components = self._dependencies.metadata_components
        if not isinstance(components, TransientToolRuntimeValue):
            raise RuntimeError("Pilot Runtime has no production Tool Metadata components")
        if getattr(components, "bundle", None) is not self.metadata_bundle:
            raise RuntimeError("Pilot Runtime Tool Metadata provenance mismatch")
        return components

    def _operation_metadata_view(self) -> ToolOperationMetadataView | None:
        bundle = self._dependencies.metadata_bundle
        if type(bundle) is not ToolMetadataBundleV1:
            return None
        view = bundle.operation_view()
        return view if type(view) is ToolOperationMetadataView else None

    def _provider_metadata_view(self) -> ProviderToolMetadataView | None:
        provider = self._dependencies.provider_metadata_view
        bundle = self._dependencies.metadata_bundle
        if (
            type(provider) is not ProviderToolMetadataView
            or type(bundle) is not ToolMetadataBundleV1
            or provider is not bundle.provider_view()
        ):
            return None
        return provider

    def _operation_metadata_port(self) -> ToolOperationMetadataPort | None:
        components = self._dependencies.metadata_components
        bundle = self._dependencies.metadata_bundle
        operation_port = getattr(components, "operation_port", None)
        if (
            type(operation_port) is not ToolOperationMetadataPort
            or type(bundle) is not ToolMetadataBundleV1
            or operation_port.bundle_instance_token is not bundle.bundle_instance_token
        ):
            return None
        return operation_port

    def _open_approval_catalog_lease(
        self,
        tool_context: ToolExecutionContext,
        catalog: object,
        catalog_lease: SegmentToolCatalogLease | None = None,
    ) -> SegmentToolCatalogLease:
        if catalog is not self._dependencies.catalog:
            raise RuntimeError("approval Catalog drifted from the Runtime Bundle")
        authority_view = self._dependencies.authority_metadata_view
        if type(authority_view) is not ToolAuthorityMetadataView:
            raise RuntimeError("approval authority metadata view is unavailable")
        lease = catalog_lease or self.metadata_bundle.open_segment_lease()
        if type(lease) is not SegmentToolCatalogLease or lease.closed:
            raise RuntimeError("approval Catalog lease is unavailable")
        try:
            tool_context.authority_factory.bind_segment_tool_catalog(
                tool_context.authority,
                authority_metadata_view=authority_view,
                catalog_lease=lease,
            )
        except BaseException:
            lease.close()
            raise
        return lease

    def start_turn(
        self,
        request: StartTurnRequest,
        *,
        transport: RuntimeTransportContext | None = None,
        event_sink: RuntimeEventSink | None = None,
        signal_sink: RuntimeSignalSink[str] | None = None,
        execution_host: AgentExecutionHost[object] | None = None,
        invocation_control: RuntimeInvocationControl | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> RuntimeOutcome:
        """Execute one synchronous model turn and return a typed outcome."""

        if not isinstance(request, StartTurnRequest):
            raise TypeError("request must be a StartTurnRequest")
        resolved_transport = transport or RuntimeTransportContext(mode="sync")
        if resolved_transport.mode != "sync":
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
            )
        if execution_host is None or invocation_control is None:
            raise TypeError("execution_host and invocation_control are required")
        cancel = cancel_check or (lambda: False)
        segment_lease: _PreparedModelLease | None = None

        def close_segment_once() -> None:
            if segment_lease is not None:
                segment_lease.release_once()

        def complete_early(outcome: RuntimeOutcome) -> RuntimeOutcome:
            close_segment_once()
            self._mark_completed(invocation_control)
            return outcome

        self._phase("validate")
        self._validate(request)
        self._check_cancel(cancel, invocation_control)

        deterministic_adapter = self._dependencies.deterministic
        if request.conversation_id in (None, 0) and deterministic_adapter is not None:
            preflight = _callable(deterministic_adapter, ("validate_new_request",))
            if preflight is not None:
                try:
                    _invoke(preflight, {"request": request}, (request,))
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except ValueError as exc:
                    text = str(exc)
                    code = (
                        RuntimeFailureCode.OPERATION_UNAVAILABLE
                        if "context" in text
                        else RuntimeFailureCode.INVALID_CONFIRMATION
                    )
                    return complete_early(self._failure(code, text, 422))
                except LookupError:
                    return complete_early(
                        self._failure(
                            RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404
                        )
                    )
                except Exception:
                    return complete_early(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话结果暂时无法保存。",
                            503,
                            retryable=True,
                        )
                    )

        self._phase("conversation")
        conversation = self._load_conversation(request)
        if conversation is None:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
                )
            )
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
                )
            )
        if _is_archived(conversation):
            return complete_early(
                self._failure(
                    RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409
                )
            )

        route_validation = self._validate_route_action(request)
        if route_validation is not None:
            return complete_early(route_validation)
        route = self._select_route(request, conversation)
        if isinstance(route, RuntimeFailureOutcome):
            return complete_early(route)
        self._phase(f"route:{route.value}")
        self._phase("pending_guard")
        try:
            pending_guard = self._pending_guard(conversation_id, conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话当前不可读取。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            raise
        if route is RouteKind.MODEL and pending_guard is not None and pending_guard is not False:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED,
                    "当前写入仍待确认，请先处理确认卡。",
                    409,
                )
            )

        if route is not RouteKind.MODEL:
            adapter = self._dependencies.deterministic
            if adapter is None:
                # Keep the pre-Task-8 closed boundary when a composition root
                # has not installed the trusted deterministic bridge.
                return complete_early(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
                    )
                )
            validate_action = _callable(adapter, ("validate_action",))
            if validate_action is not None:
                try:
                    _invoke(validate_action, {"request": request}, (request,))
                except ValueError as exc:
                    return complete_early(
                        self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422)
                    )
            self._phase("deterministic")
            try:
                return complete_early(
                    self._start_deterministic_turn(
                        adapter,
                        request,
                        conversation,
                        resolved_transport,
                        invocation_control,
                    )
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except ValueError as exc:
                text = str(exc)
                if "context is required" in text or "context is invalid" in text:
                    deterministic_outcome = self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE, text, 422
                    )
                else:
                    deterministic_outcome = self._failure(
                        RuntimeFailureCode.INVALID_CONFIRMATION, text, 422
                    )
                return complete_early(deterministic_outcome)
            except LookupError:
                return complete_early(
                    self._failure(
                        RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404
                    )
                )
            except Exception:
                return complete_early(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    )
                )
            raise AssertionError("unreachable deterministic dispatch")

        self._phase("source_load")
        try:
            source = self._load_source(conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            raise

        recorder_proxy = _RuntimeRecorderProxy()
        self._phase("segment_resolve")
        try:
            segment_value = self._resolve_segment_context(
                request,
                conversation,
                source,
                recorder_proxy,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            raise
        if isinstance(segment_value, RuntimeFailureOutcome):
            return complete_early(segment_value)
        if segment_value is None:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        segment = segment_value
        segment_lease = _PreparedModelLease(segment.close)

        def phase_with_segment(name: str) -> None:
            try:
                self._phase(name)
            except BaseException:
                close_segment_once()
                raise

        phase_with_segment("context_assemble")
        try:
            assembled = self._assemble_context(source, conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise

        phase_with_segment("policy_resolve")
        try:
            policy = self._resolve_policy_catalog(request, conversation, source, segment)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "工具权限暂时不可用，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise
        if isinstance(policy, RuntimeFailureOutcome):
            return complete_early(policy)
        segment = SegmentExecution(
            authority=segment.authority,
            context=segment.context,
            catalog=policy.catalog,
            close=segment.close,
            surface_gate=segment.surface_gate,
            policy=policy,
        )

        phase_with_segment("surface_resolve")
        try:
            surface_gate = self._resolve_surface_gate(
                request,
                conversation,
                source,
                assembled,
                policy,
                segment,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "工具权限暂时不可用，请稍后重试。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise
        if isinstance(surface_gate, RuntimeFailureOutcome):
            return complete_early(surface_gate)
        segment_lease.bind_catalog_lease(surface_gate.catalog_lease)
        segment = SegmentExecution(
            authority=segment.authority,
            context=segment.context,
            catalog=segment.catalog,
            close=segment.close,
            surface_gate=surface_gate,
            policy=policy,
            catalog_lease=surface_gate.catalog_lease,
        )

        phase_with_segment("model_resolve")
        try:
            resolved = self._resolve_continuation_model(request, conversation, policy)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.AI_PROVIDER_ERROR,
                    "AI 连接失败。请检查 AI 设置或稍后重试。",
                    502,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise
        if isinstance(resolved, RuntimeFailureOutcome):
            return complete_early(resolved)
        if resolved is None:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.MODEL_UNCONFIGURED,
                    "AI 设置尚未完成，请检查模型配置。",
                    503,
                    retryable=False,
                )
            )

        try:
            persistence = self._require_dependency("persistence")
            persistence_failure = self._validate_persistence_surface(persistence)
        except BaseException:
            close_segment_once()
            raise
        if persistence_failure is not None:
            return complete_early(persistence_failure)
        phase_with_segment("user_persist")
        try:
            self._check_cancel(cancel, invocation_control)
        except BaseException:
            close_segment_once()
            raise
        try:
            user_result = self._persist_user(
                persistence,
                conversation_id,
                request.message,
                control=invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            return complete_early(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话当前不可写入。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise
        try:
            user_persisted = _result_persisted(user_result)
            user_failure_status = _failure_status(user_result) if not user_persisted else ""
        except BaseException:
            close_segment_once()
            raise
        if not user_persisted:
            code = (
                RuntimeFailureCode.CONVERSATION_ARCHIVED
                if user_failure_status == "closed"
                else RuntimeFailureCode.APPLICATION_NOT_FOUND
                if user_failure_status == "not_found"
                else RuntimeFailureCode.OPERATION_FAILED
            )
            status = (
                409
                if code is RuntimeFailureCode.CONVERSATION_ARCHIVED
                else 404
                if code is RuntimeFailureCode.APPLICATION_NOT_FOUND
                else 503
            )
            return complete_early(
                self._failure(
                    code,
                    "对话当前不可写入。",
                    status,
                    retryable=code is RuntimeFailureCode.OPERATION_FAILED,
                )
            )

        try:
            input_message_id = _attribute(user_result, "message_id")
        except BaseException:
            close_segment_once()
            raise
        if type(input_message_id) is not int or input_message_id <= 0:
            try:
                persisted_ids = self._snapshot_message_ids(persistence, conversation_id)
            except _PersistenceReadbackError:
                return complete_early(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    )
                )
            except BaseException:
                close_segment_once()
                raise
            input_message_id = persisted_ids[-1] if persisted_ids else None

        phase_with_segment("run_start")
        try:
            self._check_cancel(cancel, invocation_control)
        except BaseException:
            close_segment_once()
            raise
        try:
            recorder, journal_started = self._start_journal(
                conversation,
                conversation_id,
                input_message_id,
                request,
                resolved_transport,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            close_segment_once()
            raise
        except Exception:
            close_segment_once()
            return complete_early(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话结果暂时无法保存。",
                    503,
                    retryable=True,
                )
            )
        except BaseException:
            close_segment_once()
            raise

        abandoned = False
        completion_marked = False

        def abandon_once() -> None:
            nonlocal abandoned
            if abandoned:
                close_segment_once()
                return
            abandoned = True
            self._abandon(recorder, journal_started)
            close_segment_once()

        def complete_once() -> None:
            nonlocal completion_marked
            if completion_marked:
                close_segment_once()
                return
            self._mark_completed(invocation_control)
            completion_marked = True
            close_segment_once()

        def finish_or_raise(
            status: str,
            failure_code: str | None,
            *,
            allow_timeout: bool = False,
        ) -> None:
            try:
                self._finish(
                    recorder,
                    journal_started,
                    status,
                    failure_code,
                    invocation_control,
                    allow_timeout=allow_timeout,
                )
                if not allow_timeout:
                    complete_once()
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
            except BaseException:
                abandon_once()
                raise
            finally:
                # Timeout/failure delivery may legally run after the control
                # leaves ACTIVE, but the Segment lease still ends here.
                close_segment_once()

        try:
            # These are the first two baseline Journal facts after run creation.
            self._record_journal_route(
                recorder,
                journal_started,
                route_kind="model",
                route_reason_code="model_default",
                control=invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except BaseException:
            abandon_once()
            raise

        try:
            self._capture_initial_journal_context(
                recorder,
                journal_started,
                conversation,
                conversation_id,
                input_message_id,
                segment.catalog,
                persistence,
                invocation_control,
            )
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except _PersistenceReadbackError:
            finish_or_raise("failed", "unknown")
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED,
                "对话结果暂时无法保存。",
                503,
                retryable=True,
            )
        except BaseException:
            abandon_once()
            raise

        try:
            driver = cast(AgentDriver, self._require_dependency("agent_driver"))
        except BaseException:
            abandon_once()
            raise
        safe_event_sink: RuntimeEventSink = (
            _SafeEventSink(event_sink) if event_sink is not None else _NoopEventSink()
        )
        safe_signal_sink: RuntimeSignalSink[str] | None = (
            _SafeSignalSink(signal_sink) if signal_sink is not None else None
        )

        def checked_cancel() -> bool:
            self._check_cancel(cancel, invocation_control)
            return False

        try:
            invocation = self._agent_invocation(
                resolved,
                segment,
                assembled,
                conversation,
                request,
                persistence,
                conversation_id,
                invocation_control,
                recorder,
                safe_event_sink,
                safe_signal_sink,
                checked_cancel,
            )
            segment_lease.bind_invocation(invocation)
        except BaseException:
            abandon_once()
            raise

        try:
            self._phase("agent_host")

            def thunk() -> object:
                return self._run_driver(driver, invocation)

            raw_result = execution_host.run(thunk, invocation_control)
            require_runtime_active(invocation_control)
        except RuntimeAgentTimedOut:
            try:
                self._allow_timeout_persistence(invocation_control)
                timeout_result = self._persist_timeout(
                    persistence,
                    conversation_id,
                    control=invocation_control,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
            except Exception:
                timeout_result = None
            except BaseException:
                abandon_once()
                raise
            if _timeout_result_persisted(timeout_result):
                try:
                    timeout_message_id = _attribute(timeout_result, "message_id")
                    self._record_journal_persisted(
                        recorder,
                        journal_started,
                        persistence,
                        conversation_id,
                        (timeout_message_id,) if type(timeout_message_id) is int else (),
                        invocation_control,
                        allow_timeout=True,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    abandon_once()
                    raise
                except Exception:
                    pass
                except BaseException:
                    abandon_once()
                    raise
                finish_or_raise("timed_out", "timeout", allow_timeout=True)
                return MessageOutcome(
                    message=CHAT_TIMEOUT_MESSAGE,
                    conversation_id=conversation_id,
                )
            finish_or_raise("failed", "unknown", allow_timeout=True)
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED,
                "对话结果暂时无法保存。",
                503,
                retryable=True,
            )
        except (RuntimeCancelled, RuntimeTransportAborted):
            abandon_once()
            raise
        except AgentLoopControlError as exc:
            abandon_once()
            if isinstance(exc, ChatRunCancelled):
                raise RuntimeCancelled() from exc
            raise
        except Exception as exc:
            finish_or_raise("failed", "provider_error")
            return self._provider_failure(resolved, exc, conversation_id=conversation_id)
        except BaseException:
            abandon_once()
            raise

        try:
            phase_with_segment("result_normalize")
            self._check_cancel(cancel, invocation_control)
            normalized = _normalize_agent_result(raw_result)
            self._check_cancel(cancel, invocation_control)
            self._settle_deferred_proposals(invocation.run_recorder, normalized.pending)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception as exc:
            finish_or_raise("failed", "provider_error")
            return self._provider_failure(resolved, exc, conversation_id=conversation_id)
        except BaseException:
            abandon_once()
            raise

        try:
            phase_with_segment("message_persist")
            self._check_cancel(cancel, invocation_control)
            persisted_turn = (
                self._pending_persistence_result(invocation, cast(AgentTurnResult, raw_result))
                if normalized.pending is not None
                else self._persist_result(
                    persistence,
                    conversation_id,
                    request,
                    normalized,
                    conversation,
                    catalog=segment.catalog,
                    route_handle=None,
                    presentation=None,
                    ensure_active=lambda: self._check_cancel(cancel, invocation_control),
                    control=invocation_control,
                )
            )
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception:
            finish_or_raise("failed", "unknown")
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True
            )
        except BaseException:
            abandon_once()
            raise

        try:
            outcome = persisted_turn.outcome
        except BaseException:
            abandon_once()
            raise
        try:
            self._record_journal_persisted(
                recorder,
                journal_started,
                persistence,
                conversation_id,
                persisted_turn.message_ids,
                invocation_control,
            )
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except BaseException:
            abandon_once()
            raise

        if isinstance(outcome, ConfirmationRequiredOutcome):
            try:
                self._suspend(
                    recorder,
                    journal_started,
                    normalized.pending,
                    invocation_control,
                    catalog=segment.catalog,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
            try:
                complete_once()
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
            except BaseException:
                abandon_once()
                raise
        else:
            if isinstance(outcome, RuntimeFailureOutcome):
                finish_or_raise("failed", "unknown")
            else:
                finish_or_raise("completed", None)
        return outcome

    def continue_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        transport: RuntimeTransportContext | None = None,
        invocation_control: RuntimeInvocationControl | None = None,
        event_sink: RuntimeEventSink | None = None,
        signal_sink: RuntimeSignalSink[str] | None = None,
        execution_host: AgentExecutionHost[object] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> RuntimeOutcome:
        """Dispatch a confirmation through the Ledger continuation boundary.

        The legacy deterministic adapter remains available when no Ledger
        coordinator is installed.  A coordinator-owned confirmation keeps
        replay/rejection provider-free and resumes approved work through the
        unchanged Agent Driver callback contract.
        """

        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")
        control = invocation_control
        if control is None:
            raise TypeError("invocation_control is required")
        resolved_transport = transport or RuntimeTransportContext(mode="sync")
        if resolved_transport.mode != "sync":
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
            )
        self._phase("validate")
        try:
            self._validate(request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except (PendingActionValidationError, ValueError):
            self._mark_completed_if_active(control)
            return self._confirmation_failure(WriteOperationError("invalid_confirmation"))
        except Exception as exc:
            self._mark_completed_if_active(control)
            return self._confirmation_failure(exc)
        effective_cancel_check = cancel_check or (lambda: False)
        self._check_cancel(effective_cancel_check, control)

        continuation = self._confirmation_coordinator()
        route_preheader: LedgerOperationPreheader | None = None
        legacy_deterministic = False
        legacy_preflight: _DeterministicConfirmationPreflight | None = None
        deterministic = self._dependencies.deterministic
        if continuation is not None and request.approved:
            try:
                route_preheader = continuation.operation_preheader(request)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                self._mark_completed_if_active(control)
                return self._confirmation_failure(exc)
            try:
                legacy_deterministic = self._is_deterministic_confirmation(
                    request,
                    preheader=route_preheader,
                )
            except WriteOperationError as exc:
                self._mark_completed_if_active(control)
                return DeterministicPilotAdapter._write_error(exc)
            if legacy_deterministic:
                if type(deterministic) is not DeterministicPilotAdapter:
                    self._mark_completed_if_active(control)
                    return self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        400,
                    )
                deterministic_operations = _attribute(
                    _attribute(deterministic, "dependencies"), "write_operations"
                )
                coordinator_operations = _attribute(
                    _attribute(continuation, "dependencies"), "write_operations"
                )
                if deterministic_operations is not coordinator_operations:
                    self._mark_completed_if_active(control)
                    return self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "写入账本暂不可用。",
                        503,
                        retryable=True,
                    )
                try:
                    replay = continuation.replay_outcome(
                        request,
                        preheader=route_preheader,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except Exception as exc:
                    self._mark_completed_if_active(control)
                    return self._confirmation_failure(exc)
                if replay is not None:
                    self._mark_completed_if_active(control)
                    return replay
                try:
                    candidate = deterministic.preflight_confirmation(
                        request,
                        preheader=route_preheader,
                        transport=resolved_transport,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except Exception as exc:
                    self._mark_completed_if_active(control)
                    return self._confirmation_failure(exc)
                if type(candidate) is not _DeterministicConfirmationPreflight:
                    self._mark_completed_if_active(control)
                    return self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        400,
                    )
                legacy_preflight = candidate
                if candidate.preheader is not route_preheader or not candidate.matched:
                    self._mark_completed_if_active(control)
                    return self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "写入账本暂不可用。",
                        503,
                        retryable=True,
                    )
                if candidate.execution is not None:
                    self._mark_completed_if_active(control)
                    return candidate.execution.outcome
        if continuation is not None and not legacy_deterministic:
            return self._continue_ledger_confirmation(
                continuation,
                request,
                control=control,
                event_sink=event_sink,
                signal_sink=signal_sink,
                execution_host=execution_host,
                cancel_check=effective_cancel_check,
                transport=resolved_transport,
                replay_preheader=route_preheader,
            )
        conversation = self._load_confirmation_conversation(request.conversation_id)
        if conversation is None:
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
            )
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
            )
        if _is_archived(conversation):
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409
            )
        adapter = self._dependencies.deterministic
        if adapter is None:
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
            )
        validate_action = _callable(adapter, ("validate_action",))
        if validate_action is not None:
            try:
                _invoke(validate_action, {"request": request}, (request,))
            except ValueError as exc:
                self._mark_completed_if_active(control)
                return self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422)
        if legacy_preflight is not None:
            terminal_replay = legacy_preflight.execution is not None
        else:
            terminal_probe = _callable(adapter, ("is_terminal_replay",))
            terminal_replay = (
                bool(_invoke(terminal_probe, {"request": request}, (request,)))
                if terminal_probe is not None
                else False
            )
        if (
            not terminal_replay
            and request.approved
            and not self._confirmation_messages_exist(conversation_id)
        ):
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
            )
        original_pending = None if terminal_replay else adapter.pending_action(conversation)
        original, journal_holder, on_attempt, on_bound, on_result = (
            self._deterministic_confirmation_callbacks(
                adapter,
                conversation,
                resolved_transport,
                control,
                original=original_pending,
                edited=not request.edited_args.is_missing(),
            )
        )
        try:
            execution = _invoke(
                adapter.confirm,
                {
                    "request": request,
                    "conversation": conversation,
                    "transport": resolved_transport,
                    "on_confirmation_attempt": on_attempt,
                    "on_confirmation_bound": on_bound,
                    "on_tool_result": on_result,
                    "preflight": legacy_preflight,
                },
                (request, conversation),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        except Exception:
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._finish(
                    recorder, journal_holder.get("started") is True, "failed", "unknown", control
                )
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True
            )
        except BaseException:
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        outcome = (
            execution.outcome
            if isinstance(execution, DeterministicExecution)
            else cast(RuntimeOutcome, execution)
        )
        try:
            self._finish_deterministic_confirmation_journal(
                journal_holder,
                original,
                conversation,
                outcome,
                control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        except BaseException:
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        self._mark_completed_if_active(control)
        return outcome

    def _confirmation_coordinator(self) -> ConfirmationCoordinator | None:
        return self._dependencies.confirmation_coordinator or self._dependencies.continuation

    def _is_deterministic_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        preheader: LedgerOperationPreheader | None = None,
    ) -> bool:
        """Select the closed deterministic adapter from the persisted operation kind."""

        if not isinstance(request, ConfirmationRequest) or not request.approved:
            return False
        if type(preheader) is not LedgerOperationPreheader:
            return False
        operation = preheader.operation
        if operation.adapter_kind != "legacy_deterministic":
            return False
        deterministic = self._dependencies.deterministic
        operation_port = self._operation_metadata_port()
        if type(deterministic) is DeterministicPilotAdapter:
            route_is_published = deterministic.accepts_legacy_route(operation.tool_name)
        elif type(operation_port) is ToolOperationMetadataPort:
            route_is_published = _published_legacy_write(operation_port, operation.tool_name)
        else:
            # The persisted adapter kind still selects the closed Legacy route.
            # The caller will fail it with 400 when no exact adapter is installed.
            route_is_published = True
        if not route_is_published:
            raise WriteOperationError("operation_identity_conflict")
        return True

    def _open_ledger_journal(
        self,
        session: ConfirmationSession,
        conversation: object | None,
        transport: RuntimeTransportContext,
        control: RuntimeInvocationControl,
        *,
        execution_path: str = "agent_resume",
        tool_names: tuple[str, ...] | None = None,
    ) -> tuple[object, bool]:
        """Resume the waiting run and install typed confirmation journal hooks."""

        try:
            recorder, started = self._resume_journal_confirmation(
                session.state.identity.conversation_id,
                session.state.pending,
                transport,
                execution_path=execution_path,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            recorder, started = _NoopRecorder(), False
        base_attempt = session.on_confirmation_attempt
        base_result = session.on_confirmation_result
        attempt_id = str(uuid4())
        late_journal_closed = False

        original_fingerprint = self._journal_call(
            recorder,
            "fingerprint_pending_identity",
            {
                "tool_call_id": session.state.pending.tool_call_id,
                "tool_name": session.state.pending.tool_name,
                "args": session.state.pending.args,
            },
            control=control,
        )
        decided_fingerprint = self._journal_call(
            recorder,
            "fingerprint_pending_identity",
            {
                "tool_call_id": session.state.effective_pending.tool_call_id,
                "tool_name": session.state.effective_pending.tool_name,
                "args": session.state.effective_pending.args,
            },
            control=control,
        )
        approval_event = (
            EventInput(
                event_type="approval.decided",
                facts={
                    "confirmation_attempt_id": attempt_id,
                    "decision": (
                        "rejected"
                        if not session.state.approved
                        else "edited"
                        if not session.state.edited_args.is_missing()
                        else "approved"
                    ),
                    "tool_call_id": session.state.pending.tool_call_id,
                    "original_input_fingerprint": original_fingerprint,
                    "decided_input_fingerprint": decided_fingerprint,
                },
                source_ref_type="tool_call",
                source_ref_id=session.state.pending.tool_call_id,
            )
            if isinstance(original_fingerprint, str) and isinstance(decided_fingerprint, str)
            else None
        )
        approval_draft = (
            self._journal_call(
                recorder,
                "prepare_event_draft",
                approval_event,
                control=control,
            )
            if approval_event is not None
            else None
        )
        resume_recorded = False
        bound_recording_failed = False

        def record_decision(bound_session: object | None) -> None:
            nonlocal bound_recording_failed, resume_recorded
            with session.state.lock:
                if session.state.approval_decided_recorded:
                    return
                session.state.approval_decided_recorded = True
            if not started or approval_event is None:
                return
            if bound_session is not None:
                if approval_draft is None:
                    # A bounded callback must never fall back to an owned
                    # Journal transaction while the Ledger holds BEGIN
                    # IMMEDIATE.  Missing draft means the recorder is already
                    # degraded; latch the local hook and let the Ledger
                    # transaction continue without a second connection.
                    resume_recorded = True
                    return
                record_bound = getattr(recorder, "record_approval_and_resume_bound", None)
                if not callable(record_bound):
                    raise WriteOperationError("operation_not_committed", retryable=True)
                recorded = record_bound(
                    bound_session,
                    approval_draft,
                    ResumedDisposition(
                        confirmation_attempt_id=attempt_id,
                        tool_call_id=session.state.pending.tool_call_id,
                    ),
                )
                if type(recorded) is not bool:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                # SafeRunRecorder returns False after a bounded ordinary
                # Journal failure.  The Ledger transaction must remain
                # usable (the recorder is degraded and recovery owns the
                # projection); only a missing/malformed bound port is a
                # programming-contract failure.
                bound_recording_failed = not recorded
                resume_recorded = recorded
                return
            self._journal_call(
                recorder,
                "append_event",
                approval_event,
                control=control,
            )
            # Without a caller-owned Ledger session this callback only
            # records the decision.  Approved execution resumes after the
            # terminal callback; the bound path above performs both pieces
            # atomically before the executor.

        def resume_decision(bound_session: object | None = None) -> None:
            nonlocal resume_recorded
            if resume_recorded or not session.state.approval_decided_recorded:
                return
            command = ResumedDisposition(
                confirmation_attempt_id=attempt_id,
                tool_call_id=session.state.pending.tool_call_id,
            )
            if bound_session is not None:
                resume_bound = getattr(recorder, "resume_bound", None)
                if not callable(resume_bound):
                    raise WriteOperationError("operation_not_committed", retryable=True)
                resumed = resume_bound(
                    bound_session,
                    command,
                )
                if type(resumed) is not bool:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                resume_recorded = True
                return
            if bound_recording_failed:
                recover = getattr(recorder, "recover_approval_and_resume", None)
                if callable(recover) and approval_draft is not None:
                    recovered = recover(approval_draft, command)
                    if type(recovered) is not bool:
                        raise WriteOperationError("operation_not_committed", retryable=True)
                # The product transaction is already terminal. Recovery is
                # Journal-only and remains fail-open even if it cannot persist.
                resume_recorded = True
                return
            self._journal_call(
                recorder,
                "resume",
                command,
                control=control,
            )
            resume_recorded = True

        session.state.approval_decided_callback = record_decision
        session.state.approval_resume_callback = resume_decision

        def attempt(action: PendingAction, prepared: object | None) -> object:
            # The Ledger atom invokes ``record_decision`` only after its
            # Pending claim/CAS wins.  Scope/Binding/pre-executor failures and
            # losers therefore leave no synthetic approval decision event.
            return base_attempt(action, cast(Any, prepared))

        def result(
            action: PendingAction,
            approved: bool,
            tool_message: Message,
            execution_record: object | None,
        ) -> object:
            nonlocal late_journal_closed
            value = base_result(
                action,
                approved,
                tool_message,
                cast(Any, execution_record),
            )
            with session.state.lock:
                late_terminal = (
                    session.state.timed_out
                    and session.state.origin_tool_message is not None
                    and (
                        session.state.active is False
                        or session.state.transactional_delivery_persisted
                    )
                )
                succeeded = session.state.succeeded
            if late_terminal and started and not late_journal_closed:
                late_failure_code = None
                if not succeeded:
                    late_outcome = _attribute(execution_record, "outcome")
                    late_failure_code = str(
                        _attribute(late_outcome, "code", "operation_failed") or "operation_failed"
                    )
                self._finish(
                    recorder,
                    True,
                    "completed" if succeeded else "failed",
                    late_failure_code,
                    control,
                    allow_timeout=True,
                )
                late_journal_closed = True
            # The production ``execute_prepared`` atom projects terminal
            # success/failure inside its Ledger transaction.  Re-projecting
            # it from this callback would create duplicate tool.completed or
            # tool.failed journal rows. If the bounded Journal failed before
            # tool.started, degraded recovery deliberately omits both sides
            # rather than create an orphan completion. Rejection has no tool
            # terminal at all, and is represented only by approval.decided.
            terminal_persisted = _attribute(execution_record, "terminal_persisted") is True
            journal_started_recorded = (
                _attribute(execution_record, "journal_started_recorded") is True
            )
            if not approved or (
                terminal_persisted
                and (
                    journal_started_recorded
                    or (
                        _attribute(recorder, "recording_status") == "degraded"
                        and not journal_started_recorded
                    )
                )
            ):
                return value
            succeeded = isinstance(_attribute(execution_record, "outcome"), ToolSuccess)
            self._record_journal_tool_result(
                recorder,
                started,
                action,
                tool_message.content,
                succeeded,
                control,
                allow_timeout=session.state.timed_out,
            )
            return value

        session.on_confirmation_attempt = cast(Any, attempt)
        session.on_confirmation_result = cast(Any, result)
        return recorder, started

    def _close_ledger_journal(
        self,
        recorder: object,
        started: bool,
        outcome: RuntimeOutcome | None,
        control: RuntimeInvocationControl,
        *,
        pending: PendingAction | None = None,
        catalog: object | None = None,
        allow_timeout: bool = False,
        delivery_succeeded: bool = False,
    ) -> None:
        if not started:
            return
        if isinstance(outcome, RuntimeFailureOutcome) and outcome.code in {
            RuntimeFailureCode.STALE_PENDING_ACTION,
            RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            RuntimeFailureCode.OPERATION_INPUT_CONFLICT,
            RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
        }:
            self._abandon(recorder, True)
        elif (
            pending is not None
            and delivery_succeeded
            and not isinstance(outcome, RuntimeFailureOutcome)
        ):
            # A chained Pending is a real Ledger proposal.  Passing the live
            # catalog keeps the journal suspension on the typed write-tool
            # boundary; ``catalog=None`` would silently skip
            # ``run.waiting_confirmation`` and leave the resumed segment
            # running forever.
            self._suspend(recorder, True, pending, control, catalog=catalog)
        elif isinstance(outcome, RuntimeFailureOutcome):
            self._finish(
                recorder,
                True,
                "timed_out" if allow_timeout else "failed",
                outcome.code.value,
                control,
                allow_timeout=allow_timeout,
            )
        else:
            self._finish(
                recorder,
                True,
                "timed_out" if allow_timeout else "completed",
                None,
                control,
                allow_timeout=allow_timeout,
            )

    @staticmethod
    def _confirmation_delivery_succeeded(
        session: ConfirmationSession,
        outcome: RuntimeOutcome | None,
    ) -> bool:
        """Return true only for an authoritative final-delivery result.

        ``state.delivered`` is set by the coordinator only after the
        persistence atom returns ``persisted``/``duplicate``.  Looking at the
        state rather than a route-local fallback result keeps sync and SSE
        from releasing the origin ToolResult or suspending a chained Pending
        on a CAS/closed/unknown delivery response.
        """

        if isinstance(outcome, RuntimeFailureOutcome):
            return False
        state = session.state
        return bool(
            state.delivered
            and _failure_status(state.delivery_result)
            in {PersistenceStatus.PERSISTED.value, PersistenceStatus.DUPLICATE.value}
        )

    @staticmethod
    def _release_confirmation_origin_events(
        event_sink: RuntimeEventSink | None,
        deferred_origin_events: list[RuntimeEvent],
        *,
        delivery_succeeded: bool,
    ) -> None:
        """Release deferred origin events only after durable delivery."""

        if not delivery_succeeded or event_sink is None:
            deferred_origin_events.clear()
            return
        while deferred_origin_events:
            emit_runtime_event(event_sink, deferred_origin_events.pop(0))

    def _finalize_confirmation_result(
        self,
        recorder: object,
        started: bool,
        session: ConfirmationSession,
        outcome: RuntimeOutcome | None,
        control: RuntimeInvocationControl,
        *,
        pending: PendingAction | None = None,
        catalog: object | None = None,
        allow_timeout: bool = False,
        event_sink: RuntimeEventSink | None = None,
        deferred_origin_events: list[RuntimeEvent] | None = None,
    ) -> bool:
        """Close Journal and release origin events from one shared atom.

        Both confirmation transports call this after normal, timeout, and
        ordinary-exception convergence.  The only success signal is the
        coordinator's authoritative ``final_delivery`` result.
        """

        delivery_succeeded = self._confirmation_delivery_succeeded(session, outcome)
        self._record_ledger_delivery_journal(
            recorder,
            started,
            session,
            control,
            allow_timeout=allow_timeout,
        )
        with session.state.lock:
            defer_late_terminal = (
                allow_timeout
                and session.state.confirmation_attempted
                and session.state.active
                and not session.state.delivered
                and not session.state.transactional_delivery_persisted
                and session.state.origin_tool_message is None
            )
        if not defer_late_terminal:
            self._close_ledger_journal(
                recorder,
                started,
                outcome,
                control,
                pending=pending,
                catalog=catalog,
                allow_timeout=allow_timeout,
                delivery_succeeded=delivery_succeeded,
            )
        if deferred_origin_events is not None:
            self._release_confirmation_origin_events(
                event_sink,
                deferred_origin_events,
                delivery_succeeded=delivery_succeeded,
            )
        return delivery_succeeded

    def _record_ledger_delivery_journal(
        self,
        recorder: object,
        started: bool,
        session: ConfirmationSession,
        control: RuntimeInvocationControl,
        *,
        allow_timeout: bool = False,
    ) -> None:
        """Project the atomic Ledger delivery after it has actually committed."""

        if not started:
            return
        result = session.state.delivery_result
        if result is None or not result.message_ids:
            return
        persistence = self._dependencies.persistence
        if persistence is None:
            return
        self._record_journal_persisted(
            recorder,
            started,
            persistence,
            session.state.identity.conversation_id,
            result.message_ids,
            control,
            allow_timeout=allow_timeout,
        )

    def _continue_ledger_rejection(
        self,
        coordinator: ConfirmationCoordinator,
        request: ConfirmationRequest,
        *,
        control: RuntimeInvocationControl,
        transport: RuntimeTransportContext | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> RuntimeOutcome:
        """Complete rejection from Pending/Ledger atoms only."""

        try:
            session = coordinator.reject(request)
        except ConfirmationReplayError:
            replay = coordinator.replay_outcome(request)
            self._mark_completed_if_active(control)
            return replay or self._failure(
                RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                "operation result is unavailable",
                503,
                retryable=True,
            )
        recorder: object = _NoopRecorder()
        journal_started = False
        try:
            if transport is not None:
                recorder, journal_started = self._open_ledger_journal(
                    session,
                    SimpleNamespace(
                        id=request.conversation_id,
                        context_type="workspace",
                        context_ref="",
                        mode="general",
                    ),
                    transport,
                    control,
                    execution_path="agent_resume",
                )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        except BaseException:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        try:
            claimed = session.on_confirmation_attempt(session.pending, None)
        except BaseException:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        if isinstance(session.state.terminal_execution, OperationReplay):
            # Another worker may have terminalized the operation between the
            # Ledger-first probe and this reject CAS.  Do not fabricate a
            # second delivery; converge through the repository replay atom.
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            try:
                replay = coordinator.replay_outcome(request)
            except Exception as exc:
                self._mark_completed_if_active(control)
                return self._confirmation_failure(exc)
            self._mark_completed_if_active(control)
            return replay or self._failure(
                RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                "operation result is unavailable",
                503,
                retryable=True,
            )
        if isinstance(claimed, ToolFailure):
            self._abandon(recorder, journal_started)
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.STALE_PENDING_ACTION, claimed.code, 409)
        if cancel_check is not None:
            try:
                self._check_cancel(cancel_check, control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                raise
        visible = str(
            _attribute(
                _attribute(session.state.terminal_execution, "payload"),
                "visible_result",
                "已取消这次操作。",
            )
            or "已取消这次操作。"
        )
        if session.state.delivered:
            outcome = MessageOutcome(
                message=visible,
                conversation_id=request.conversation_id,
                write_status="cancelled",
                undo=self._previous_write_undo(None, request.conversation_id),
                operation_id=session.state.identity.operation_id,
                persisted=True,
                legacy_projection=True,
            )
            self._close_ledger_journal(recorder, journal_started, outcome, control)
            self._mark_completed_if_active(control)
            return outcome
        origin = Message(
            role="tool",
            content=_CANCELLED_TOOL_RESULT,
            tool_call_id=session.pending.tool_call_id,
        )
        try:
            session.on_confirmation_result(session.pending, False, origin, None)
            delivered = coordinator.final_delivery(
                session,
                DeliveryBundle((Message(role="assistant", content=visible),)),
            )
            self._record_ledger_delivery_journal(recorder, journal_started, session, control)
        except BaseException:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        delivered_status = _failure_status(delivered)
        if delivered is None or delivered_status in {"cas_lost", "closed", "not_found"}:
            self._abandon(recorder, journal_started)
            self._mark_completed_if_active(control)
            if delivered_status == "cas_lost":
                return self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            return self._failure(
                RuntimeFailureCode.OPERATION_DELIVERY_PENDING
                if delivered is None
                else RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                "确认结果正在处理中，请刷新对话查看结果。"
                if delivered is None
                else "对话结果暂时无法保存。",
                409 if delivered is None else 503,
                retryable=True,
            )
        outcome = MessageOutcome(
            message=visible,
            conversation_id=request.conversation_id,
            write_status="cancelled",
            undo=self._previous_write_undo(None, request.conversation_id),
            operation_id=request.operation_id or session.state.identity.operation_id,
            persisted=True,
            legacy_projection=True,
        )
        self._close_ledger_journal(recorder, journal_started, outcome, control)
        self._mark_completed_if_active(control)
        return outcome

    def _build_confirmation_segment(
        self,
        session: ConfirmationSession,
        activation_request: _ContinuationActivationRequest,
        recorder: object,
        journal_started: bool,
        control: RuntimeInvocationControl,
        cancel_check: Callable[[], bool],
    ) -> ApprovedContinuationSegment:
        """Build the sole post-terminal Segment for an approved continuation.

        Every value below comes from a canonical Conversation reload after the
        origin terminal acquired delivery ownership.  No object captured
        before approval, approval Authority, or old source loader is reused.
        """

        activation_id = activation_request.conversation_id

        def require_activation_identity() -> None:
            if activation_request.conversation_id != activation_id:
                raise WriteOperationError("operation_integrity_error")

        if not session.delivery_fence():
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        terminal = session.state.terminal_execution
        execution_record = session.state.execution_record
        if not isinstance(terminal, (OperationCommitted, OperationFailed)):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        if terminal.operation_id != session.state.identity.operation_id:
            raise WriteOperationError("operation_integrity_error")
        if (
            execution_record is None
            or _attribute(execution_record, "terminal_persisted", False) is not True
        ):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        persisted_visible_result = _attribute(execution_record, "persisted_visible_result")
        terminal_visible_result = _attribute(_attribute(terminal, "payload"), "visible_result")
        if (
            type(persisted_visible_result) is not str
            or type(terminal_visible_result) is not str
            or persisted_visible_result != terminal_visible_result
        ):
            raise WriteOperationError("operation_integrity_error")
        conversation = self._load_confirmation_conversation(activation_request.conversation_id)
        require_activation_identity()
        if (
            conversation is None
            or _conversation_id(conversation) != activation_request.conversation_id
        ):
            raise WriteOperationError("operation_unavailable")
        if _is_archived(conversation):
            raise WriteOperationError("operation_unavailable")
        generation = _attribute(conversation, "updated_at")
        if isinstance(generation, datetime):
            with session.state.lock:
                session.state.continuation_generation = generation
        self._check_cancel(cancel_check, control)

        source = self._load_source(
            cast(Any, conversation),
            activation_request,
            pending_tool_call_id=session.state.pending.tool_call_id,
        )
        require_activation_identity()
        recorder_proxy = _RuntimeRecorderProxy(recorder)
        segment_value = self._resolve_segment_context(
            activation_request, conversation, source, recorder_proxy
        )
        require_activation_identity()
        if isinstance(segment_value, RuntimeFailureOutcome) or segment_value is None:
            raise WriteOperationError("operation_unavailable")
        segment = segment_value
        try:
            assembled = self._assemble_context(source, conversation, activation_request)
            require_activation_identity()
            values = _attribute(assembled, "messages", _attribute(assembled, "history", assembled))
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise WriteOperationError("operation_unavailable")
            messages = tuple(_message(item) for item in values)
            origin = session.state.origin_tool_message
            if origin is None:
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            pending = session.state.pending
            if origin.tool_call_id != pending.tool_call_id:
                raise WriteOperationError("operation_integrity_error")
            expected_args = _canonical_tool_args(pending.args)
            proposal_matches: list[tuple[int, ToolCall, str | None]] = []
            for index, raw_message in enumerate(values):
                if str(_attribute(raw_message, "role", "") or "") != "assistant":
                    continue
                raw_calls = _attribute(raw_message, "tool_calls", ())
                if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
                    continue
                for raw_call in raw_calls:
                    call = _tool_call(raw_call)
                    if call.id == pending.tool_call_id:
                        proposal_matches.append(
                            (
                                index,
                                call,
                                _canonical_tool_args(_attribute(raw_call, "args", call.args)),
                            )
                        )
            if (
                expected_args is None
                or len(proposal_matches) != 1
                or proposal_matches[0][1].name != pending.tool_name
                or proposal_matches[0][2] != expected_args
            ):
                raise WriteOperationError("operation_integrity_error")
            origin_matches = tuple(
                item
                for item in messages
                if item.role == "tool" and item.tool_call_id == origin.tool_call_id
            )
            origin_indices = tuple(
                index
                for index, item in enumerate(messages)
                if item.role == "tool" and item.tool_call_id == origin.tool_call_id
            )
            if len(origin_matches) == 0:
                # The normal delivery atom persists origin+continuation only
                # after the origin terminal.  When this canonical reload races
                # that atom, make the runtime-owned bundle complete from the
                # authoritative terminal message exactly once.  The Agent
                # Loop/Runner never repairs history itself.
                messages = (*messages, origin)
            elif len(origin_matches) != 1 or origin_matches[0] != origin:
                raise WriteOperationError("operation_integrity_error")
            elif proposal_matches[0][0] >= origin_indices[0]:
                raise WriteOperationError("operation_integrity_error")
            policy = self._resolve_policy_catalog(activation_request, conversation, source, segment)
            require_activation_identity()
            if isinstance(policy, RuntimeFailureOutcome):
                raise WriteOperationError(str(policy.code.value))
            segment = SegmentExecution(
                authority=segment.authority,
                context=segment.context,
                catalog=policy.catalog,
                close=segment.close,
                surface_gate=segment.surface_gate,
                policy=policy,
            )
            surface_gate = self._resolve_surface_gate(
                activation_request, conversation, source, messages, policy, segment
            )
            require_activation_identity()
            if isinstance(surface_gate, RuntimeFailureOutcome):
                raise WriteOperationError(str(surface_gate.code.value))
            segment = SegmentExecution(
                authority=segment.authority,
                context=segment.context,
                catalog=segment.catalog,
                close=segment.close,
                surface_gate=surface_gate,
                policy=policy,
                catalog_lease=surface_gate.catalog_lease,
            )
            resolved = self._resolve_continuation_model(activation_request, conversation, policy)
            require_activation_identity()
            if isinstance(resolved, RuntimeFailureOutcome):
                raise WriteOperationError(str(resolved.code.value))
            if resolved is None or resolved.model is None:
                raise WriteOperationError("model_unconfigured")
            model = resolved.model
            persistence = self._require_dependency("persistence")
            try:
                self._capture_confirmation_journal_context(
                    recorder,
                    journal_started,
                    conversation,
                    activation_request.conversation_id,
                    persistence,
                    control,
                    tool_names=(session.pending.tool_name,),
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception:
                # Journal is diagnostic and fail-open.  The Segment remains
                # authoritative for Provider execution and delivery.
                pass
            bundle = ApprovedContinuationSegment(
                messages=messages,
                model=model,
                catalog=cast(Any, segment.catalog),
                tool_context=segment.context,
                surface_gate=cast(Any, segment.surface_gate),
                max_iterations=resolved.max_iter,
            )
            # Publish the close callback only after the complete immutable
            # bundle has been constructed.  If any constructor/validation
            # above fails, the outer candidate cleanup owns the sole close.
            with session.state.lock:
                session.state.continuation_segment_close = lambda: (
                    self._safe_close_segment_candidate(segment)
                )
            return bundle
        except BaseException:
            self._safe_close_segment_candidate(segment)
            raise

    def _continue_ledger_confirmation(
        self,
        coordinator: ConfirmationCoordinator,
        request: ConfirmationRequest,
        *,
        control: RuntimeInvocationControl,
        event_sink: RuntimeEventSink | None,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost[object] | None,
        cancel_check: Callable[[], bool],
        transport: RuntimeTransportContext,
        replay_preheader: LedgerOperationPreheader | None,
    ) -> RuntimeOutcome:
        """Run a Ledger-backed confirmation without opening the old route state.

        Terminal replay and rejection are deliberately completed before any
        Conversation/model dependency is touched.  Only an approved live
        Pending enters the Agent Driver's typed ``execute`` entry; all result
        persistence remains a coordinator atom.
        """

        # Rejection is the direct worker path.  It must not load a Conversation
        # or resolve a model before the Pending/Ledger CAS has converged.
        if not request.approved:
            try:
                return self._continue_ledger_rejection(
                    coordinator,
                    request,
                    control=control,
                    transport=transport,
                    cancel_check=cancel_check,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                self._mark_completed_if_active(control)
                return self._confirmation_failure(exc)
            except BaseException:
                raise

        try:
            replay = coordinator.replay_outcome(request, preheader=replay_preheader)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            self._mark_completed_if_active(control)
            return self._confirmation_failure(exc)
        if replay is not None:
            self._mark_completed_if_active(control)
            return replay

        # Validate the live Pending/Ledger identity before touching a
        # Conversation or resolving a model.  The session constructor repeats
        # this read at the claim boundary; this detached probe only closes the
        # preheader race and is never used as execution authority.
        preflight_pending: PendingAction | None = None
        try:
            preflight_pending = coordinator.preflight_live(
                request,
            )
        except ConfirmationReplayError:
            try:
                replayed = coordinator.replay_outcome(request)
            except Exception as exc:
                self._mark_completed_if_active(control)
                return self._confirmation_failure(exc)
            self._mark_completed_if_active(control)
            return replayed or self._failure(
                RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                "operation result is unavailable",
                503,
                retryable=True,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            self._mark_completed_if_active(control)
            return self._confirmation_failure(exc)

        catalog = self._dependencies.catalog
        approval_catalog_lease = self.metadata_bundle.open_segment_lease()
        approval_spec_handle = approval_catalog_lease.resolve(preflight_pending.tool_name)
        if type(approval_spec_handle) is not SegmentToolSpecHandle:
            approval_catalog_lease.close()
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "unsupported runtime route",
                400,
            )
        try:
            session_or_replay = coordinator.approve_modify(
                request,
                pending=preflight_pending,
                conversation=None,
                catalog_lease=approval_catalog_lease,
                spec_handle=approval_spec_handle,
            )
            if isinstance(session_or_replay, OperationReplay):
                approval_catalog_lease.close()
                self._mark_completed_if_active(control)
                try:
                    replayed = coordinator.replay_outcome(request)
                except Exception as exc:
                    return self._confirmation_failure(exc)
                return replayed or self._failure(
                    RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                    "operation result is unavailable",
                    503,
                    retryable=True,
                )
            session = session_or_replay
        except ConfirmationReplayError:
            approval_catalog_lease.close()
            self._mark_completed_if_active(control)
            try:
                replayed = coordinator.replay_outcome(request)
            except Exception as exc:
                return self._confirmation_failure(exc)
            return replayed or self._failure(
                RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                "operation result is unavailable",
                503,
                retryable=True,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            approval_catalog_lease.close()
            raise
        except Exception as exc:
            approval_catalog_lease.close()
            self._mark_completed_if_active(control)
            return self._confirmation_failure(exc)
        except BaseException:
            approval_catalog_lease.close()
            raise

        driver = self._dependencies.agent_driver
        if driver is None:
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._mark_completed_if_active(control)
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
            )
        recorder: object = _NoopRecorder()
        journal_started = False
        try:
            recorder, journal_started = self._open_ledger_journal(
                session,
                None,
                transport,
                control,
                tool_names=self._journal_tool_names(self._provider_metadata_view()),
            )
            activation_request = _ContinuationActivationRequest(
                session.state.identity.conversation_id
            )
            session.continuation_segment_builder = lambda: self._build_confirmation_segment(
                session,
                activation_request,
                recorder,
                journal_started,
                control,
                cancel_check,
            )
            # Approval owns the origin execution and records its terminal
            # result directly.  Proposal gating is installed only after the
            # fresh Segment is activated; passing a proxy here would make the
            # Driver wrap proxy->gate->proxy recursively.
            tool_context = session.approval_execution_context(recorder)
            approval_catalog_lease = self._open_approval_catalog_lease(
                tool_context,
                catalog,
                approval_catalog_lease,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            raise
        except Exception as exc:
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            self._mark_completed_if_active(control)
            return self._confirmation_failure(exc)
        except BaseException:
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            raise
        deferred_origin_events: list[RuntimeEvent] = []
        origin_tool_call_id = session.pending.tool_call_id

        confirmation_event_sink: RuntimeEventSink | None = (
            _ConfirmationEventSink(event_sink, origin_tool_call_id, deferred_origin_events)
            if event_sink is not None
            else None
        )
        invocation_construction_failed = False
        invocation: AgentLoopInvocation | None = None
        try:
            try:
                invocation = AgentLoopInvocation(
                    seed=ApprovedWriteSeed(ConfirmationApprovedWritePort(session)),
                    model=None,
                    catalog=cast(Any, catalog),
                    catalog_lease=approval_catalog_lease,
                    tool_context=cast(Any, tool_context),
                    auto_approve=False,
                    max_iterations=DEFAULT_MAX_ITERATIONS,
                    run_recorder=cast(Any, recorder),
                    event_sink=cast(Any, confirmation_event_sink),
                    runtime_signal_sink=signal_sink,
                    cancel_check=self._confirmation_cancel_check(control, cancel_check),
                )
                self._bind_invocation_pending_persistence(
                    invocation,
                    lambda turn, route_handle, presentation: (
                        self._persist_chained_pending_before_release(
                            coordinator,
                            session,
                            request,
                            control,
                            turn,
                            route_handle,
                            presentation,
                        )
                    ),
                )
            except Exception:
                invocation_construction_failed = True
                raise
            self._check_cancel(cancel_check, control)
            raw_result = (
                execution_host.run(lambda: self._run_driver(driver, invocation), control)
                if execution_host is not None
                else self._run_driver(driver, invocation)
            )
            self._check_cancel(cancel_check, control)
            normalized = _normalize_agent_result(raw_result)
            outcome: RuntimeOutcome = (
                cast(
                    RuntimeOutcome,
                    invocation._pending_persistence_result(cast(AgentTurnResult, raw_result)),
                )
                if normalized.pending is not None
                else self._finish_ledger_confirmation(
                    coordinator,
                    session,
                    normalized,
                    request,
                    control,
                )
            )
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                control,
                pending=normalized.pending,
                catalog=catalog,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            self._mark_completed_if_active(control)
            self._stop_confirmation_heartbeat(coordinator, session)
            return outcome
        except RuntimeAgentTimedOut:
            try:
                fallback = coordinator.timeout_convergence(session)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                fallback = None
                timeout_error: BaseException | None = exc
            else:
                timeout_error = None
            state = session.state
            self._stop_confirmation_heartbeat(coordinator, session)
            if timeout_error is not None:
                outcome = self._confirmation_failure(timeout_error)
            elif state.cas_lost:
                outcome = self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            elif state.delivered:
                # The deadline can race the final delivery commit.  Once the
                # owner has atomically marked delivery complete, report a
                # durable terminal result instead of incorrectly returning
                # ``confirmation_in_progress``.
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state),
                    conversation_id=request.conversation_id,
                    write_status="success" if state.succeeded else "failed",
                    undo=self._confirmation_undo(state),
                    operation_id=state.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            elif _failure_status(fallback) in {
                PersistenceStatus.PERSISTED.value,
                PersistenceStatus.DUPLICATE.value,
            }:
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state),
                    conversation_id=request.conversation_id,
                    write_status="success" if state.succeeded else "failed",
                    undo=self._confirmation_undo(state),
                    operation_id=state.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            elif state.confirmation_attempted:
                outcome = self._failure(
                    RuntimeFailureCode.CONFIRMATION_IN_PROGRESS,
                    "确认操作仍在后台执行，请刷新对话查看结果，不要重复提交。",
                    409,
                    retryable=False,
                )
            else:
                outcome = self._failure(
                    RuntimeFailureCode.CHAT_AGENT_TIMEOUT,
                    CHAT_TIMEOUT_MESSAGE,
                    504,
                    retryable=True,
                )
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                control,
                allow_timeout=True,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            self._mark_completed_if_active(control)
            return outcome
        except RuntimeCancelled:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        except RuntimeTransportAborted:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        except ConfirmationReplayError:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            try:
                replay = coordinator.replay_outcome(request)
            except Exception as exc:
                self._mark_completed_if_active(control)
                return self._confirmation_failure(exc)
            if replay is not None:
                self._mark_completed_if_active(control)
                return replay
            raise
        except Exception as exc:
            if invocation_construction_failed:
                coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                raise
            if _attribute(exc, "code") == RuntimeFailureCode.OPERATION_INTEGRITY_ERROR.value:
                coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                outcome = self._confirmation_failure(exc)
                self._mark_completed_if_active(control)
                return outcome
            state = session.state
            try:
                failure_delivery: object = coordinator.fallback(session)
            except Exception as delivery_error:
                failure_delivery = None
                delivery_error_value: BaseException | None = delivery_error
            else:
                delivery_error_value = None
            if delivery_error_value is not None:
                outcome = self._confirmation_failure(delivery_error_value)
            elif state.cas_lost:
                outcome = self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            elif _failure_status(failure_delivery) in {
                PersistenceStatus.PERSISTED.value,
                PersistenceStatus.DUPLICATE.value,
            }:
                coordinator._hydrate_terminal_undo(state)
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state),
                    conversation_id=request.conversation_id,
                    write_status="success" if state.succeeded else "failed",
                    undo=self._confirmation_undo(state),
                    operation_id=state.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            else:
                outcome = self._provider_confirmation_failure(exc)
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                control,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            self._mark_completed_if_active(control)
            return outcome
        except BaseException:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        finally:
            # ``stop_heartbeat`` is idempotent and is the final safety net for
            # provider failures, sink aborts, and uncancellable late results.
            self._stop_confirmation_heartbeat(coordinator, session)
            if invocation is None:
                approval_catalog_lease.close()
            else:
                invocation._release_catalog_lease_from_runtime()

    def _persist_chained_pending_before_release(
        self,
        coordinator: ConfirmationCoordinator,
        session: object,
        request: ConfirmationRequest,
        control: RuntimeInvocationControl,
        turn: AgentTurnResult,
        route_handle: PendingPersistenceRouteHandle,
        presentation: PendingPresentationSnapshot,
    ) -> RuntimeOutcome:
        normalized = _normalize_agent_result(turn)
        pending = normalized.pending
        if pending is None:
            raise TypeError("chained Pending persistence requires a Pending result")
        return self._finish_ledger_confirmation(
            coordinator,
            session,
            normalized,
            request,
            control,
            route_handle=route_handle,
            presentation=presentation,
            complete_control=False,
        )

    def _finish_ledger_confirmation(
        self,
        coordinator: ConfirmationCoordinator,
        session: object,
        normalized: NormalizedAgentTurn,
        request: ConfirmationRequest,
        control: RuntimeInvocationControl,
        *,
        route_handle: PendingPersistenceRouteHandle | None = None,
        presentation: PendingPresentationSnapshot | None = None,
        complete_control: bool = True,
    ) -> RuntimeOutcome:
        def complete_control_once() -> None:
            if complete_control:
                self._mark_completed_if_active(control)

        typed_session = cast(Any, session)
        state = typed_session.state
        origin = state.origin_tool_message
        if origin is None:
            # A driver that does not use the unchanged callback boundary is
            # not allowed to make an unowned write appear durable.
            coordinator.cancel_cleanup(typed_session)
            complete_control_once()
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED, "写入结果暂时无法保存。", 503, retryable=True
            )
        added = [_message(item) for item in normalized.added]
        continuation: list[Message] = []
        origin_removed = False
        for message in added:
            if (
                not origin_removed
                and message.role == "tool"
                and message.tool_call_id == origin.tool_call_id
            ):
                origin_removed = True
                continue
            continuation.append(message)
        if not continuation and normalized.reply:
            continuation.append(Message(role="assistant", content=normalized.reply))
        pending = normalized.pending
        delivery = coordinator.final_delivery(
            typed_session,
            DeliveryBundle(
                tuple(continuation),
                pending=pending,
                route_handle=route_handle,
            ),
        )
        delivery_status = _failure_status(delivery)
        if delivery is None or delivery_status in {"cas_lost", "closed", "not_found"}:
            complete_control_once()
            if delivery_status == "cas_lost":
                return self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            return self._failure(
                RuntimeFailureCode.OPERATION_DELIVERY_PENDING
                if delivery is None
                else RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                "确认结果正在处理中，请刷新对话查看结果。"
                if delivery is None
                else "对话结果暂时无法保存。",
                409 if delivery is None else 503,
                retryable=True,
            )
        complete_control_once()
        if pending is not None:
            if route_handle is None or type(presentation) is not PendingPresentationSnapshot:
                raise WriteOperationError("operation_unavailable")
            args, token = _safe_pending_payload(pending)
            from .contracts import PendingActionPayload

            editable = tuple(
                freeze_json_mapping(dict(value)) for value in presentation.editable_fields
            )
            details = freeze_json_mapping(dict(presentation.details))

            return ConfirmationRequiredOutcome(
                confirmation_token=token,
                conversation_id=request.conversation_id,
                operation_id=pending.operation_id or state.identity.operation_id,
                message=_user_facing_assistant_content(normalized.reply),
                pending_action=PendingActionPayload(
                    tool_name=pending.tool_name,
                    operation_id=pending.operation_id or state.identity.operation_id,
                    human=pending.human,
                    args=args,
                    confirmation_token=token,
                    editable_fields=editable,
                    details=details,
                ),
            )
        payload = _attribute(_attribute(state.terminal_execution, "payload"), "failure_code")
        record_outcome = _attribute(_attribute(state, "execution_record"), "outcome")
        compatibility_detail = _attribute(record_outcome, "compatibility_detail", "")
        write_error = (
            str(compatibility_detail)
            if isinstance(compatibility_detail, str) and compatibility_detail
            else str(payload)
            if payload
            else None
        )
        write_status: WriteStatus = "success" if state.succeeded else "failed"
        if not state.approved:
            write_status = "cancelled"
        visible_reply = _user_facing_assistant_content(
            normalized.reply or continuation[-1].content if continuation else ""
        )
        visible_reply = _apply_exact_success_presentation(
            visible_reply,
            state.execution_record,
        )
        return MessageOutcome(
            message=visible_reply,
            conversation_id=request.conversation_id,
            write_status=write_status,
            write_error=write_error,
            undo=self._confirmation_undo(state),
            operation_id=state.identity.operation_id,
            replayed=state.replayed,
            persisted=True,
            legacy_projection=True,
        )

    @staticmethod
    def _confirmation_undo(state: object) -> ImmutablePayload | None:
        raw = (
            _attribute(state, "undo_update")
            if _attribute(state, "approved", True)
            else _attribute(state, "undo")
        )
        if not isinstance(raw, Mapping) or not raw:
            return None
        value = dict(raw)
        identity = _attribute(state, "identity")
        operation_id = _attribute(identity, "operation_id", "") or _attribute(
            state, "undo_operation_id", ""
        )
        if isinstance(operation_id, str) and operation_id:
            value["parent_operation_id"] = operation_id
        return freeze_json_mapping(value)

    @staticmethod
    def _stop_confirmation_heartbeat(
        coordinator: ConfirmationCoordinator,
        session: ConfirmationSession,
    ) -> None:
        state = session.state
        with state.lock:
            retain_for_late_result = bool(
                state.timed_out
                and state.active
                and state.confirmation_attempted
                and not state.delivered
                and not state.cas_lost
            )
        if not retain_for_late_result:
            coordinator.stop_heartbeat(session)

    def _previous_write_undo(
        self,
        conversation: object | None,
        conversation_id: int | None = None,
    ) -> ImmutablePayload | None:
        raw = _attribute(conversation, "last_write_undo")
        if raw is None and conversation_id is not None:
            getter = _callable(self._dependencies.persistence, ("get_last_write_undo",))
            if getter is not None:
                raw = _invoke(
                    getter,
                    {"conversation_id": conversation_id, "id": conversation_id},
                    (conversation_id,),
                )
        if not isinstance(raw, Mapping) or not raw:
            return None
        value = dict(raw)
        operation_id = _attribute(conversation, "last_write_operation_id", "")
        if isinstance(operation_id, str) and operation_id:
            value["parent_operation_id"] = operation_id
        return freeze_json_mapping(value)

    def _metadata_operation_ports(
        self,
    ) -> tuple[ToolOperationMetadataPort, PendingPersistenceRoutePort]:
        components = self._dependencies.metadata_components
        operation_port = getattr(components, "operation_port", None)
        pending_port = getattr(components, "pending_persistence_route_port", None)
        bundle = self._dependencies.metadata_bundle
        if type(operation_port) is not ToolOperationMetadataPort:
            raise RuntimeError("Runtime Tool Operation Metadata Port is unavailable")
        if type(pending_port) is not PendingPersistenceRoutePort:
            raise RuntimeError("Runtime Pending Persistence Route Port is unavailable")
        if (
            type(bundle) is not ToolMetadataBundleV1
            or operation_port.bundle_instance_token is not bundle.bundle_instance_token
            or pending_port.bundle_instance_token is not bundle.bundle_instance_token
            or pending_port._operation_port is not operation_port
        ):
            raise RuntimeError("Runtime Tool Metadata Port provenance mismatch")
        return operation_port, pending_port

    def _bind_invocation_pending_persistence(
        self,
        invocation: AgentLoopInvocation,
        consumer: Callable[..., object],
    ) -> None:
        if self._dependencies.metadata_components is None:
            # Narrow non-production Runtime tests may never produce Pending.
            # The private handoff remains unissued, so any attempted Pending
            # persistence still fails closed before a repository side effect.
            invocation._bind_pending_persistence(consumer)
            return
        operation_port, pending_port = self._metadata_operation_ports()
        invocation._bind_pending_persistence(consumer, operation_port, pending_port)

    def _clarification_pending_route(
        self,
        pending: PendingAction,
        conversation_id: int,
    ) -> tuple[PendingPersistenceRoutePort, ClarificationPendingRouteHandle]:
        digest, revision = pending_action_identity(
            pending.tool_call_id,
            pending.tool_name,
            pending.args,
        )
        identity = PendingRouteIdentityV1(
            conversation_id=conversation_id,
            operation_id="",
            tool_call_id=pending.tool_call_id,
            tool_name="",
            pending_action_revision=revision,
            pending_confirmation_claim_id="",
            arguments_digest=digest,
        )
        _, pending_port = self._metadata_operation_ports()
        return pending_port, pending_port.bind_clarification_pending(identity)

    @staticmethod
    def _project_pending_presentation(
        pending: PendingAction,
        lease: SegmentToolCatalogLease,
        spec_handle: SegmentToolSpecHandle,
        context: ToolExecutionContext,
    ) -> tuple[tuple[ImmutablePayload, ...], ImmutablePayload]:
        spec = lease.require_spec(spec_handle)
        if spec.name != pending.tool_name:
            raise ValueError("Pending presentation Spec identity mismatch")
        editable = tuple(
            freeze_json_mapping(cast(Mapping[str, object], value.to_compat_descriptor()))
            for value in spec.metadata.editable_fields
        )
        try:
            decoded = spec.decoder(json.loads(pending.args))
            raw_details = spec.presentation.pending_details_projector(decoded, context)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_details = {}
        details = (
            freeze_json_mapping(raw_details)
            if isinstance(raw_details, Mapping)
            else freeze_json_mapping({})
        )
        return editable, details

    @staticmethod
    def _confirmation_failure(error: BaseException) -> RuntimeFailureOutcome:
        code = str(_attribute(error, "code", "operation_failed") or "operation_failed")
        status_code = 503
        try:
            failure_code = RuntimeFailureCode(code)
        except ValueError:
            if code in {"stale_pending_action", "confirmation_claim_lost"}:
                failure_code = RuntimeFailureCode.STALE_PENDING_ACTION
                status_code = 409
            elif code == "operation_identity_conflict":
                failure_code = RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT
                status_code = 409
            else:
                failure_code = RuntimeFailureCode.OPERATION_FAILED
        if failure_code in {
            RuntimeFailureCode.STALE_PENDING_ACTION,
            RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            RuntimeFailureCode.OPERATION_INPUT_CONFLICT,
            RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
            RuntimeFailureCode.CONFIRMATION_IN_PROGRESS,
            RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
        }:
            status_code = 409
        elif failure_code is RuntimeFailureCode.INVALID_CONFIRMATION:
            status_code = 422
        return PilotRuntime._failure(
            failure_code,
            "对话结果暂时无法保存。",
            status_code,
            retryable=True,
        )

    @staticmethod
    def _provider_failure(
        resolved: ResolvedModel | None,
        error: Exception,
        *,
        conversation_id: int | None = None,
    ) -> RuntimeFailureOutcome:
        formatter = _attribute(resolved, "provider_error_message")
        message = None
        if callable(formatter):
            try:
                candidate = formatter(error)
            except Exception:
                candidate = None
            if isinstance(candidate, str) and candidate:
                message = candidate
        return PilotRuntime._failure(
            RuntimeFailureCode.AI_PROVIDER_ERROR,
            message or "AI 连接失败。请检查 AI 设置或稍后重试。",
            502,
            retryable=True,
            conversation_id=conversation_id,
        )

    @staticmethod
    def _provider_confirmation_failure(
        error: BaseException,
        resolved: ResolvedModel | None = None,
    ) -> RuntimeFailureOutcome:
        """Map an uncategorized Agent/provider exception to the baseline 502."""

        if isinstance(error, PendingActionValidationError):
            return PilotRuntime._failure(
                RuntimeFailureCode.INVALID_CONFIRMATION,
                f"确认参数无效：{error}",
                422,
                retryable=True,
            )
        if isinstance(error, StalePendingActionError):
            return PilotRuntime._failure(
                RuntimeFailureCode.STALE_PENDING_ACTION,
                "待确认操作已过期或正在处理中，请刷新对话后重试。",
                409,
                retryable=True,
            )
        if _attribute(error, "code") not in (None, ""):
            return PilotRuntime._confirmation_failure(error)
        return PilotRuntime._provider_failure(
            resolved,
            error if isinstance(error, Exception) else RuntimeError(str(error)),
        )

    confirmation = continue_confirmation
    execute_confirmation = continue_confirmation

    # ---- stream preparation/execution --------------------------------------

    def prepare_stream(
        self,
        request: StartTurnRequest | ConfirmationRequest,
        *,
        transport: RuntimeTransportContext,
        invocation_control: RuntimeInvocationControl,
    ) -> ImmediateHttpOutcome | PreparedStreamExecution:
        """Perform the response-header preparation boundary for SSE.

        The stream model path intentionally keeps Source before Run.  No Agent
        host is touched until the returned prepared handle is begun by the
        transport guard.
        """

        if not isinstance(request, (StartTurnRequest, ConfirmationRequest)):
            raise TypeError("request must be a StartTurnRequest or ConfirmationRequest")
        if not isinstance(transport, RuntimeTransportContext):
            raise TypeError("transport must be a RuntimeTransportContext")
        if not isinstance(invocation_control, RuntimeInvocationControl):
            # RuntimeInvocationControl is runtime-checkable, so this catches
            # accidental test/route objects before any write side effect.
            raise TypeError("invocation_control must implement RuntimeInvocationControl")
        if transport.mode != "stream":
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "unsupported runtime route",
                    400,
                ),
                invocation_control,
            )

        self._phase("validate")
        try:
            self._validate(cast(StartTurnRequest, request))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except (PendingActionValidationError, ValueError):
            return self._stream_immediate(
                self._confirmation_failure(WriteOperationError("invalid_confirmation")),
                invocation_control,
            )
        except Exception as exc:
            return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
        self._check_cancel(lambda: False, invocation_control)

        confirmation_coordinator = self._confirmation_coordinator()
        stream_replay: RuntimeOutcome | None = None
        preflight_pending: PendingAction | None = None
        route_preheader: LedgerOperationPreheader | None = None
        legacy_deterministic = False
        legacy_preflight: _DeterministicConfirmationPreflight | None = None
        deterministic = self._dependencies.deterministic
        if (
            isinstance(request, ConfirmationRequest)
            and request.approved
            and confirmation_coordinator is not None
        ):
            try:
                route_preheader = confirmation_coordinator.operation_preheader(request)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
            try:
                legacy_deterministic = self._is_deterministic_confirmation(
                    request,
                    preheader=route_preheader,
                )
            except WriteOperationError as exc:
                return self._stream_immediate(
                    DeterministicPilotAdapter._write_error(exc),
                    invocation_control,
                )
            if legacy_deterministic:
                if type(deterministic) is not DeterministicPilotAdapter:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_UNAVAILABLE,
                            "unsupported runtime route",
                            400,
                        ),
                        invocation_control,
                    )
                deterministic_operations = _attribute(
                    _attribute(deterministic, "dependencies"), "write_operations"
                )
                coordinator_operations = _attribute(
                    _attribute(confirmation_coordinator, "dependencies"),
                    "write_operations",
                )
                if deterministic_operations is not coordinator_operations:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_UNAVAILABLE,
                            "写入账本暂不可用。",
                            503,
                            retryable=True,
                        ),
                        invocation_control,
                    )
                try:
                    stream_replay = confirmation_coordinator.replay_outcome(
                        request,
                        preheader=route_preheader,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except Exception as exc:
                    return self._stream_immediate(
                        self._confirmation_failure(exc),
                        invocation_control,
                        direct=True,
                    )
                if stream_replay is not None:
                    return self._prepare_deterministic_stream(
                        DeterministicExecution(
                            stream_replay,
                            preparation_kind=PreparationKind.REPLAY,
                        ),
                        request=request,
                        conversation=None,
                        conversation_id=request.conversation_id,
                        transport=transport,
                        invocation_control=invocation_control,
                    )
                try:
                    candidate = deterministic.preflight_confirmation(
                        request,
                        preheader=route_preheader,
                        transport=transport,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except Exception as exc:
                    return self._stream_immediate(
                        self._confirmation_failure(exc), invocation_control
                    )
                if type(candidate) is not _DeterministicConfirmationPreflight:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_UNAVAILABLE,
                            "unsupported runtime route",
                            400,
                        ),
                        invocation_control,
                    )
                legacy_preflight = candidate
                if candidate.preheader is not route_preheader or not candidate.matched:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_UNAVAILABLE,
                            "写入账本暂不可用。",
                            503,
                            retryable=True,
                        ),
                        invocation_control,
                    )
                if candidate.execution is not None:
                    return self._prepare_deterministic_stream(
                        candidate.execution,
                        request=request,
                        conversation=None,
                        conversation_id=request.conversation_id,
                        transport=transport,
                        invocation_control=invocation_control,
                    )
        if (
            isinstance(request, ConfirmationRequest)
            and confirmation_coordinator is not None
            and not legacy_deterministic
        ):
            if not request.approved:
                # The reject coordinator owns terminal replay as part of the
                # same bounded preheader; do not perform a second bootstrap.
                return self._prepare_ledger_confirmation_stream(
                    confirmation_coordinator,
                    request,
                    None,
                    request.conversation_id,
                    transport,
                    invocation_control,
                    replay=None,
                    terminal_checked=True,
                )
            try:
                stream_replay = confirmation_coordinator.replay_outcome(
                    request,
                    preheader=route_preheader,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
            if stream_replay is not None:
                # Ledger replay is transport-independent and must not load a
                # Conversation or source before the response header.
                return self._prepare_deterministic_stream(
                    DeterministicExecution(
                        stream_replay,
                        preparation_kind=PreparationKind.REPLAY,
                    ),
                    request=request,
                    conversation=None,
                    conversation_id=request.conversation_id,
                    transport=transport,
                    invocation_control=invocation_control,
                )
            try:
                preflight_pending = confirmation_coordinator.preflight_live(
                    request,
                )
            except ConfirmationReplayError:
                try:
                    replayed = confirmation_coordinator.replay_outcome(request)
                except Exception as exc:
                    return self._stream_immediate(
                        self._confirmation_failure(exc), invocation_control
                    )
                if replayed is not None:
                    return self._prepare_deterministic_stream(
                        DeterministicExecution(
                            replayed,
                            preparation_kind=PreparationKind.REPLAY,
                        ),
                        request=request,
                        conversation=None,
                        conversation_id=request.conversation_id,
                        transport=transport,
                        invocation_control=invocation_control,
                    )
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                        "operation result is unavailable",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
            return self._prepare_ledger_confirmation_stream(
                confirmation_coordinator,
                request,
                None,
                request.conversation_id,
                transport,
                invocation_control,
                replay=None,
                terminal_checked=True,
                preflight_pending=preflight_pending,
                defer_approval=True,
            )

        if (
            isinstance(request, StartTurnRequest)
            and request.conversation_id in (None, 0)
            and self._dependencies.deterministic is not None
        ):
            preflight = _callable(self._dependencies.deterministic, ("validate_new_request",))
            if preflight is not None:
                try:
                    _invoke(preflight, {"request": request}, (request,))
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    raise
                except ValueError as exc:
                    text = str(exc)
                    code = (
                        RuntimeFailureCode.OPERATION_UNAVAILABLE
                        if "context" in text
                        else RuntimeFailureCode.INVALID_CONFIRMATION
                    )
                    return self._stream_immediate(
                        self._failure(code, text, 422), invocation_control
                    )
                except LookupError:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404
                        ),
                        invocation_control,
                    )
                except Exception:
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话结果暂时无法保存。",
                            503,
                            retryable=True,
                        ),
                        invocation_control,
                    )

        # An uninstalled bridge keeps the old closed boundary: invalid
        # deterministic requests fail before Conversation creation.  Once the
        # trusted bridge is installed, the branch is prepared below so all
        # pre-header deterministic effects happen exactly once.
        if (
            (isinstance(request, ConfirmationRequest) or request.pilot_action is not None)
            and self._dependencies.deterministic is None
            and self._confirmation_coordinator() is None
        ):
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "unsupported runtime route",
                    400,
                ),
                invocation_control,
            )

        self._phase("conversation")
        try:
            conversation = (
                self._load_confirmation_conversation(request.conversation_id)
                if isinstance(request, ConfirmationRequest)
                else self._load_conversation(request)
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.APPLICATION_NOT_FOUND,
                    "conversation not found",
                    404,
                ),
                invocation_control,
            )
        conversation_id = _conversation_id(conversation) if conversation is not None else None
        if conversation is None or conversation_id is None:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.APPLICATION_NOT_FOUND,
                    "conversation not found",
                    404,
                ),
                invocation_control,
            )
        if _is_archived(conversation):
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.CONVERSATION_ARCHIVED,
                    "conversation is archived",
                    409,
                ),
                invocation_control,
            )

        continuation = self._confirmation_coordinator()
        if (
            isinstance(request, ConfirmationRequest)
            and continuation is not None
            and not legacy_deterministic
        ):
            return self._prepare_ledger_confirmation_stream(
                continuation,
                request,
                conversation,
                conversation_id,
                transport,
                invocation_control,
                replay=stream_replay,
                terminal_checked=True,
                preflight_pending=preflight_pending,
            )

        adapter = self._dependencies.deterministic
        if isinstance(request, ConfirmationRequest):
            if adapter is None:
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
                    ),
                    invocation_control,
                )
            validate_action = _callable(adapter, ("validate_action",))
            if validate_action is not None:
                try:
                    _invoke(validate_action, {"request": request}, (request,))
                except ValueError as exc:
                    return self._stream_immediate(
                        self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422),
                        invocation_control,
                    )
            terminal_replay = bool(
                legacy_preflight is not None and legacy_preflight.execution is not None
            )
            if (
                not terminal_replay
                and request.approved
                and not self._confirmation_messages_exist(conversation_id)
            ):
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
                    ),
                    invocation_control,
                )
            original_pending = None if terminal_replay else adapter.pending_action(conversation)
            original, journal_holder, on_attempt, on_bound, on_result = (
                self._deterministic_confirmation_callbacks(
                    adapter,
                    conversation,
                    transport,
                    invocation_control,
                    original=original_pending,
                    edited=not request.edited_args.is_missing(),
                )
            )
            try:
                execution = _invoke(
                    adapter.confirm,
                    {
                        "request": request,
                        "conversation": conversation,
                        "transport": transport,
                        "on_confirmation_attempt": on_attempt,
                        "on_confirmation_bound": on_bound,
                        "on_tool_result": on_result,
                        "preflight": legacy_preflight,
                    },
                    (request, conversation),
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._abandon(recorder, journal_holder.get("started") is True)
                raise
            except Exception:
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._finish(
                        recorder,
                        journal_holder.get("started") is True,
                        "failed",
                        "unknown",
                        invocation_control,
                    )
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            except BaseException:
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._abandon(recorder, journal_holder.get("started") is True)
                raise
            if not isinstance(execution, DeterministicExecution):
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            try:
                self._finish_deterministic_confirmation_journal(
                    journal_holder,
                    original,
                    conversation,
                    execution.outcome,
                    invocation_control,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._abandon(recorder, journal_holder.get("started") is True)
                raise
            except BaseException:
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._abandon(recorder, journal_holder.get("started") is True)
                raise
            return self._prepare_deterministic_stream(
                execution,
                request=request,
                conversation=conversation,
                conversation_id=conversation_id,
                transport=transport,
                invocation_control=invocation_control,
            )

        route_validation = self._validate_route_action(request)
        if route_validation is not None:
            return self._stream_immediate(route_validation, invocation_control)
        try:
            route = self._select_route(request, conversation)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "unsupported runtime route",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        if isinstance(route, RuntimeFailureOutcome):
            return self._stream_immediate(route, invocation_control)
        self._phase(f"route:{route.value}")
        if isinstance(request, StartTurnRequest):
            self._phase("pending_guard")
            try:
                pending_guard = self._pending_guard(conversation_id, conversation, request)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception:
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话当前不可读取。",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            except BaseException:
                raise
            if (
                route is RouteKind.MODEL
                and pending_guard is not None
                and pending_guard is not False
            ):
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED,
                        "当前写入仍待确认，请先处理确认卡。",
                        409,
                    ),
                    invocation_control,
                )
        if route is not RouteKind.MODEL:
            if adapter is None:
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
                    ),
                    invocation_control,
                )
            validate_action = _callable(adapter, ("validate_action",))
            if validate_action is not None:
                try:
                    _invoke(validate_action, {"request": request}, (request,))
                except ValueError as exc:
                    return self._stream_immediate(
                        self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422),
                        invocation_control,
                    )
            try:
                recorder, journal_started, replay = self._prepare_deterministic_journal(
                    adapter,
                    request,
                    conversation,
                    transport,
                    invocation_control,
                )
                execution = adapter.prepare_stream(
                    request,
                    conversation,
                    transport=transport,
                    on_user_message_persisted=lambda message_id: self._journal_call(
                        recorder,
                        "attach_input_message",
                        message_id,
                        control=invocation_control,
                    ),
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                if "journal_started" in locals():
                    self._abandon(recorder, journal_started)
                raise
            except ValueError as exc:
                text = str(exc)
                code = (
                    RuntimeFailureCode.OPERATION_UNAVAILABLE
                    if "context" in text
                    else RuntimeFailureCode.INVALID_CONFIRMATION
                )
                status_code = 422
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(
                    self._failure(code, text, status_code), invocation_control
                )
            except LookupError:
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404
                    ),
                    invocation_control,
                )
            except Exception:
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            except BaseException:
                if "journal_started" in locals():
                    self._abandon(recorder, journal_started)
                raise
            if replay or execution.pending_replay:
                try:
                    self._finish_journal_replay(recorder, invocation_control)
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    self._abandon(recorder, journal_started)
                    raise
                except BaseException:
                    self._abandon(recorder, journal_started)
                    raise
            elif isinstance(execution.outcome, ConfirmationRequiredOutcome):
                try:
                    self._suspend(
                        recorder,
                        journal_started,
                        adapter.pending_action(conversation),
                        invocation_control,
                        catalog=None,
                        trusted_legacy=True,
                    )
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    self._abandon(recorder, journal_started)
                    raise
                except BaseException:
                    self._abandon(recorder, journal_started)
                    raise
            elif isinstance(execution.outcome, RuntimeFailureOutcome):
                try:
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    self._abandon(recorder, journal_started)
                    raise
                except BaseException:
                    self._abandon(recorder, journal_started)
                    raise
            else:
                try:
                    self._finish(recorder, journal_started, "completed", None, invocation_control)
                except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                    self._abandon(recorder, journal_started)
                    raise
                except BaseException:
                    self._abandon(recorder, journal_started)
                    raise
            return self._prepare_deterministic_stream(
                execution,
                request=request,
                conversation=conversation,
                conversation_id=conversation_id,
                transport=transport,
                invocation_control=invocation_control,
                recorder=recorder,
                journal_started=journal_started,
            )

        self._phase("source_load")
        try:
            source = self._load_source(conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            raise

        recorder_proxy = _RuntimeRecorderProxy()
        self._phase("segment_resolve")
        try:
            segment_value = self._resolve_segment_context(
                request, conversation, source, recorder_proxy
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            raise
        if isinstance(segment_value, RuntimeFailureOutcome):
            return self._stream_immediate(segment_value, invocation_control)
        if segment_value is None:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        segment = segment_value
        segment_token = object()
        self._prepared_segments[segment_token] = segment
        segment_lease = _PreparedModelLease(segment.close)

        def release_segment_now() -> None:
            try:
                segment_lease.release_once()
            finally:
                self._prepared_segments.pop(segment_token, None)

        def phase_with_segment(name: str) -> None:
            try:
                self._phase(name)
            except BaseException:
                release_segment_now()
                raise

        phase_with_segment("context_assemble")
        try:
            assembled = self._assemble_context(source, conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_segment_now()
            raise
        except Exception:
            release_segment_now()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.SOURCE_LOAD_FAILED,
                    "上下文暂时无法加载，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_segment_now()
            raise

        phase_with_segment("policy_resolve")
        try:
            policy = self._resolve_policy_catalog(request, conversation, source, segment)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_segment_now()
            raise
        except Exception:
            release_segment_now()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "工具权限暂时不可用，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_segment_now()
            raise
        if isinstance(policy, RuntimeFailureOutcome):
            release_segment_now()
            return self._stream_immediate(policy, invocation_control)
        segment = SegmentExecution(
            authority=segment.authority,
            context=segment.context,
            catalog=policy.catalog,
            close=segment.close,
            surface_gate=segment.surface_gate,
            policy=policy,
        )

        phase_with_segment("surface_resolve")
        try:
            surface_gate = self._resolve_surface_gate(
                request,
                conversation,
                source,
                assembled,
                policy,
                segment,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_segment_now()
            raise
        except Exception:
            release_segment_now()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "工具权限暂时不可用，请稍后重试。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_segment_now()
            raise
        if isinstance(surface_gate, RuntimeFailureOutcome):
            release_segment_now()
            return self._stream_immediate(surface_gate, invocation_control)
        segment_lease.bind_catalog_lease(surface_gate.catalog_lease)
        segment = SegmentExecution(
            authority=segment.authority,
            context=segment.context,
            catalog=segment.catalog,
            close=segment.close,
            surface_gate=surface_gate,
            policy=policy,
            catalog_lease=surface_gate.catalog_lease,
        )
        self._prepared_segments[segment_token] = segment

        phase_with_segment("model_resolve")
        try:
            resolved = self._resolve_continuation_model(request, conversation, policy)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_segment_now()
            raise
        except Exception:
            release_segment_now()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.AI_PROVIDER_ERROR,
                    "AI 连接失败。请检查 AI 设置或稍后重试。",
                    502,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_segment_now()
            raise
        if isinstance(resolved, RuntimeFailureOutcome):
            release_segment_now()
            return self._stream_immediate(resolved, invocation_control)
        if resolved is None:
            release_segment_now()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.MODEL_UNCONFIGURED,
                    "AI 设置尚未完成，请检查模型配置。",
                    503,
                ),
                invocation_control,
            )

        model_token = object()
        self._prepared_models[model_token] = resolved
        model_lease = _PreparedModelLease(lambda: self._prepared_models.pop(model_token, None))

        def release_segment() -> None:
            release_segment_now()

        def release_model() -> None:
            model_lease.release_once()

        def release_prepared() -> None:
            try:
                release_model()
            finally:
                release_segment()

        def phase_with_prepared(name: str) -> None:
            try:
                self._phase(name)
            except BaseException:
                release_prepared()
                raise

        def stream_failure(outcome: RuntimeFailureOutcome) -> ImmediateHttpOutcome:
            release_prepared()
            return self._stream_immediate(outcome, invocation_control)

        try:
            persistence = self._require_dependency("persistence")
            persistence_failure = self._validate_persistence_surface(persistence)
        except BaseException:
            release_prepared()
            raise
        if persistence_failure is not None:
            return stream_failure(persistence_failure)

        phase_with_prepared("user_persist")
        try:
            self._check_cancel(lambda: False, invocation_control)
        except BaseException:
            release_prepared()
            raise
        try:
            user_result = self._persist_user(
                persistence,
                conversation_id,
                request.message,
                control=invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_prepared()
            raise
        except Exception:
            return stream_failure(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话当前不可写入。",
                    503,
                    retryable=True,
                ),
            )
        except BaseException:
            release_prepared()
            raise
        try:
            user_persisted = _result_persisted(user_result)
            status = _failure_status(user_result) if not user_persisted else ""
        except BaseException:
            release_prepared()
            raise
        if not user_persisted:
            code = (
                RuntimeFailureCode.CONVERSATION_ARCHIVED
                if status == "closed"
                else RuntimeFailureCode.APPLICATION_NOT_FOUND
                if status == "not_found"
                else RuntimeFailureCode.OPERATION_FAILED
            )
            status_code = (
                409
                if code is RuntimeFailureCode.CONVERSATION_ARCHIVED
                else 404
                if code is RuntimeFailureCode.APPLICATION_NOT_FOUND
                else 503
            )
            return stream_failure(
                self._failure(
                    code,
                    "对话当前不可写入。",
                    status_code,
                    retryable=code is RuntimeFailureCode.OPERATION_FAILED,
                ),
            )

        try:
            input_message_id = _attribute(user_result, "message_id")
        except BaseException:
            release_prepared()
            raise
        if type(input_message_id) is not int or input_message_id <= 0:
            try:
                persisted_ids = self._snapshot_message_ids(persistence, conversation_id)
            except _PersistenceReadbackError:
                return stream_failure(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                )
            except BaseException:
                release_prepared()
                raise
            input_message_id = persisted_ids[-1] if persisted_ids else None

        phase_with_prepared("transport_identity")
        try:
            self._check_cancel(lambda: False, invocation_control)
        except BaseException:
            release_prepared()
            raise
        phase_with_prepared("run_start")
        try:
            recorder, journal_started = self._start_journal(
                conversation,
                conversation_id,
                input_message_id,
                request,
                transport,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            release_prepared()
            raise
        except Exception:
            release_prepared()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话结果暂时无法保存。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_prepared()
            raise

        try:
            self._record_journal_route(
                recorder,
                journal_started,
                route_kind="model",
                route_reason_code="model_default",
                control=invocation_control,
            )
            self._phase("context_capture")
            self._capture_initial_journal_context(
                recorder,
                journal_started,
                conversation,
                conversation_id,
                input_message_id,
                segment.catalog,
                persistence,
                invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            self._abandon(recorder, journal_started)
            release_prepared()
            raise
        except Exception:
            self._abandon(recorder, journal_started)
            release_prepared()
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话结果暂时无法保存。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            self._abandon(recorder, journal_started)
            release_prepared()
            raise

        cell = _PreparedExecutionCell(run_open=journal_started)

        def on_abort() -> None:
            release_prepared()
            with cell.lock:
                if cell.aborted or cell.completed:
                    return
                cell.aborted = True
                should_abandon = cell.run_open
                cell.run_open = False
            if should_abandon:
                self._abandon(recorder, journal_started)
            if invocation_control.state is InvocationState.ACTIVE:
                invocation_control.mark_completed()

        def on_complete(_reason: CompletionReason) -> None:
            release_prepared()
            with cell.lock:
                if cell.aborted or cell.completed:
                    return
                cell.run_open = False
                cell.completed = True

        try:
            frozen_assembled = _freeze_stream_value(assembled)
        except (TypeError, ValueError):
            release_prepared()
            self._abandon(recorder, journal_started)
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话结果暂时无法保存。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_prepared()
            self._abandon(recorder, journal_started)
            raise
        assembled_values = (
            cast(tuple[object, ...], frozen_assembled)
            if isinstance(frozen_assembled, tuple)
            else (frozen_assembled,)
        )
        try:
            detached_conversation = _prepared_conversation(conversation, conversation_id)
            state = _PreparedStreamState(
                owner_token=self._owner_token,
                preparation_kind=PreparationKind.MODEL,
                execution_mode=StreamExecutionMode.AGENT_HOST,
                control=invocation_control,
                request=request,
                conversation=detached_conversation,
                conversation_id=conversation_id,
                model_token=model_token,
                segment_token=segment_token,
                segment_lease=segment_lease,
                surface_gate=surface_gate,
                assembled=assembled_values,
                recorder=recorder,
                journal_started=journal_started,
                transport=transport,
                cell=cell,
                on_abort=on_abort,
                on_complete=on_complete,
            )
        except (TypeError, ValueError):
            release_prepared()
            self._abandon(recorder, journal_started)
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话结果暂时无法保存。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        except BaseException:
            release_prepared()
            self._abandon(recorder, journal_started)
            raise
        transport_run_id = transport.transport_run_id
        if transport_run_id is None:  # pragma: no cover - RuntimeTransportContext validates this
            release_prepared()
            self._abandon(recorder, journal_started)
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
                ),
                invocation_control,
            )
        try:
            prepared = PreparedStreamExecution(
                invocation_id=transport_run_id,
                preparation_kind=PreparationKind.MODEL,
                execution_mode=StreamExecutionMode.AGENT_HOST,
                opaque_state=state,
            )
        except BaseException:
            release_prepared()
            self._abandon(recorder, journal_started)
            raise
        try:
            self._phase("prepared")
        except BaseException:
            release_prepared()
            self._abandon(recorder, journal_started)
            raise
        return prepared

    def execute_prepared_stream(
        self,
        prepared: PreparedStreamExecution,
        *,
        event_sink: RuntimeEventSink | None,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost[object],
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome:
        """Execute business work; the transport Guard owns lifecycle completion."""

        state = (
            prepared.opaque_state
            if isinstance(prepared, PreparedStreamExecution)
            and isinstance(prepared.opaque_state, _PreparedStreamState)
            else None
        )
        should_abort = False
        if state is not None and state.owner_token is self._owner_token:
            with state.cell.lock:
                should_abort = (
                    state.cell.execution_owner is not None
                    and not state.cell.running
                    and not state.cell.aborted
                    and not state.cell.completed
                )

        def cleanup_on_error() -> None:
            if not should_abort or state is None or state.on_abort is None:
                return
            with state.cell.lock:
                state.cell.running = False
            try:
                state.on_abort()
            except BaseException:
                pass

        try:
            return self._execute_prepared_stream_body(
                prepared,
                event_sink=event_sink,
                signal_sink=signal_sink,
                execution_host=execution_host,
                cancel_check=cancel_check,
            )
        except RuntimeCancelled:
            cleanup_on_error()
            raise
        except (RuntimeTransportAborted, RuntimeAgentTimedOut):
            cleanup_on_error()
            raise
        except BaseException:
            cleanup_on_error()
            raise

    def _execute_prepared_stream_body(
        self,
        prepared: PreparedStreamExecution,
        *,
        event_sink: RuntimeEventSink | None,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost[object],
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome:
        """Execute one prepared handle; direct handles never enter the Agent host."""

        if not isinstance(prepared, PreparedStreamExecution):
            raise TypeError("prepared must be a PreparedStreamExecution")
        state = prepared.opaque_state
        if not isinstance(state, _PreparedStreamState):
            raise TypeError("prepared stream state is not owned by PilotRuntime")
        if state.owner_token is not self._owner_token:
            raise TypeError("prepared stream belongs to a different PilotRuntime")
        if not callable(cancel_check):
            raise TypeError("cancel_check must be callable")

        if prepared.lifecycle_state is not PreparedLifecycleState.EXECUTING:
            raise RuntimeTransportAborted()
        with state.cell.lock:
            if state.cell.execution_owner is None:
                raise RuntimeTransportAborted()
            if state.cell.running or state.cell.aborted or state.cell.completed:
                raise RuntimeTransportAborted()
            state.cell.running = True

        safe_event_sink: RuntimeEventSink = (
            _SafeEventSink(event_sink) if event_sink is not None else _NoopEventSink()
        )
        safe_signal_sink: RuntimeSignalSink[str] | None = (
            _SafeSignalSink(signal_sink) if signal_sink is not None else None
        )

        def close_terminal_owner() -> None:
            """Close Runtime ownership before projecting terminal events."""

            with state.cell.lock:
                state.cell.run_open = False
                state.cell.completed = True

        def finish(outcome: RuntimeOutcome, reason: CompletionReason) -> RuntimeOutcome:
            with state.cell.lock:
                state.cell.outcome = outcome
                state.cell.running = False
                state.cell.run_open = False
                state.cell.completed = True
            if state.on_complete is not None:
                try:
                    state.on_complete(reason)
                except BaseException:
                    pass
            return outcome

        def abort(reason: CompletionReason) -> None:
            with state.cell.lock:
                state.cell.running = False
            if state.on_abort is not None:
                try:
                    state.on_abort()
                except BaseException:
                    pass

        if isinstance(state.request, ConfirmationRequest) and (
            state.confirmation_session is not None or state.confirmation_pending is not None
        ):
            try:
                confirmation_outcome = self._execute_prepared_ledger_confirmation(
                    state,
                    safe_event_sink,
                    safe_signal_sink,
                    execution_host,
                    cancel_check,
                )
                if isinstance(confirmation_outcome, ConfirmationRequiredOutcome):
                    emit_runtime_event(
                        safe_event_sink,
                        StatusEvent(phase="waiting_confirmation", label="需要确认"),
                    )
                    if confirmation_outcome.pending_action is not None:
                        emit_runtime_event(
                            safe_event_sink,
                            ConfirmationRequiredEvent(
                                confirmation_token=confirmation_outcome.confirmation_token,
                                operation_id=confirmation_outcome.operation_id,
                                pending_action=confirmation_outcome.pending_action,
                            ),
                        )
                if isinstance(confirmation_outcome, RuntimeFailureOutcome):
                    emit_runtime_event(
                        safe_event_sink,
                        ErrorEvent(
                            confirmation_outcome.code,
                            confirmation_outcome.message,
                            confirmation_outcome.retryable,
                            confirmation_outcome.degraded,
                        ),
                    )
                else:
                    emit_runtime_event(
                        safe_event_sink, CompletedEvent(response=confirmation_outcome)
                    )
                return finish(confirmation_outcome, CompletionReason.NORMAL)
            except RuntimeCancelled:
                abort(CompletionReason.CANCELLED)
                raise
            except (RuntimeTransportAborted, RuntimeAgentTimedOut):
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise
            except BaseException:
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise

        if state.execution_mode is StreamExecutionMode.DIRECT:
            try:
                self._check_cancel(cancel_check, state.control)
                # Direct preparation has already committed its terminal facts;
                # close Runtime ownership before projecting any external event.
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                for event in state.events:
                    if type(event) is CompletedEvent:
                        continue
                    emit_runtime_event(safe_event_sink, event)
                outcome: RuntimeOutcome
                if state.outcome is None:
                    outcome = self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    )
                else:
                    outcome = state.outcome
                emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
                return finish(outcome, CompletionReason.NORMAL)
            except RuntimeCancelled:
                abort(CompletionReason.CANCELLED)
                raise
            except (RuntimeTransportAborted, RuntimeAgentTimedOut):
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise
            except BaseException:
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise

        resolved_model = (
            self._prepared_models.get(state.model_token) if state.model_token is not None else None
        )
        segment = (
            self._prepared_segments.get(state.segment_token)
            if state.segment_token is not None
            else None
        )
        if (
            resolved_model is None
            or segment is None
            or state.conversation is None
            or state.conversation_id is None
        ):
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise RuntimeTransportAborted()

        try:
            emit_runtime_event(
                safe_event_sink,
                MetaEvent(
                    supports_delta=callable(getattr(resolved_model.model, "stream_complete", None))
                ),
            )
            emit_runtime_event(safe_event_sink, UserMessageSavedEvent())
            emit_runtime_event(
                safe_event_sink,
                StatusEvent(phase="model_running", label="正在思考"),
            )
            self._check_cancel(cancel_check, state.control)

            def checked_cancel() -> bool:
                self._check_cancel(cancel_check, state.control)
                return False

            driver = cast(AgentDriver, self._require_dependency("agent_driver"))
            invocation_holder: dict[str, AgentLoopInvocation] = {}

            def build_invocation(agent_events: RuntimeEventSink) -> AgentLoopInvocation:
                invocation = self._agent_invocation(
                    resolved_model,
                    segment,
                    tuple(_materialize_stream_value(item) for item in state.assembled),
                    state.conversation,
                    cast(StartTurnRequest, state.request),
                    self._require_dependency("persistence"),
                    cast(int, state.conversation_id),
                    state.control,
                    state.recorder,
                    agent_events,
                    safe_signal_sink,
                    checked_cancel,
                )
                if state.segment_lease is None:
                    raise RuntimeTransportAborted()
                state.segment_lease.bind_invocation(invocation)
                invocation_holder["value"] = invocation
                return invocation

            # SseAgentExecutionHost owns an unbounded queue and passes its
            # typed sink to a one-argument thunk.  Sync/fake hosts use the
            # ordinary zero-argument Runtime thunk.  Keep this adaptation at
            # the host boundary; business preparation remains Runtime-owned.
            if callable(getattr(execution_host, "iter_events", None)):
                self._phase("agent_host")

                def stream_thunk(agent_events: RuntimeEventSink) -> object:
                    invocation = build_invocation(_SafeEventSink(agent_events))
                    return self._run_driver(driver, invocation)

                streamed = cast(Any, execution_host).run(stream_thunk, state.control)
                if hasattr(streamed, "__next__"):
                    try:
                        for agent_event in cast(Iterable[object], streamed):
                            emit_runtime_event(safe_event_sink, cast(RuntimeEvent, agent_event))
                        result_reader = getattr(streamed, "result", None)
                        if callable(result_reader):
                            raw_result = result_reader()
                        elif result_reader is not None:
                            raw_result = result_reader
                        else:
                            raise RuntimeTransportAborted()
                    finally:
                        close = getattr(streamed, "close", None)
                        if callable(close):
                            try:
                                close()
                            except BaseException:
                                pass
                else:
                    raw_result = streamed
            else:
                self._phase("agent_host")
                invocation = build_invocation(safe_event_sink)

                def thunk() -> object:
                    return self._run_driver(driver, invocation)

                raw_result = execution_host.run(thunk, state.control)
            self._check_cancel(cancel_check, state.control)
            normalized = _normalize_agent_result(raw_result)
            completed_invocation = invocation_holder.get("value")
            if completed_invocation is not None:
                self._settle_deferred_proposals(
                    completed_invocation.run_recorder, normalized.pending
                )
        except RuntimeAgentTimedOut:
            persistence = self._require_dependency("persistence")
            timeout_result: object | None = None
            try:
                self._allow_timeout_persistence(state.control)
                timeout_result = self._persist_timeout(
                    persistence,
                    state.conversation_id,
                    control=state.control,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abort(CompletionReason.CANCELLED)
                raise
            except Exception:
                timeout_result = None
            if _timeout_result_persisted(timeout_result):
                self._record_journal_persisted(
                    state.recorder,
                    state.journal_started,
                    persistence,
                    state.conversation_id,
                    tuple(
                        value
                        for value in (_attribute(timeout_result, "message_id"),)
                        if type(value) is int
                    ),
                    state.control,
                    allow_timeout=True,
                )
                self._finish(
                    state.recorder,
                    state.journal_started,
                    "timed_out",
                    "timeout",
                    state.control,
                    allow_timeout=True,
                )
                close_terminal_owner()
                outcome = MessageOutcome(
                    message=CHAT_TIMEOUT_MESSAGE,
                    conversation_id=state.conversation_id,
                )
                emit_runtime_event(safe_event_sink, AssistantMessageEvent(message=outcome.message))
                emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
                return finish(outcome, CompletionReason.CANCELLED)
            self._finish(
                state.recorder,
                state.journal_started,
                "failed",
                "unknown",
                state.control,
                allow_timeout=True,
            )
            close_terminal_owner()
            outcome = self._failure(
                RuntimeFailureCode.OPERATION_FAILED,
                "对话结果暂时无法保存。",
                503,
                retryable=True,
            )
            emit_runtime_event(
                safe_event_sink,
                ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded),
            )
            emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
            return finish(outcome, CompletionReason.CANCELLED)
        except RuntimeCancelled:
            abort(CompletionReason.CANCELLED)
            raise
        except RuntimeTransportAborted:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise
        except Exception as exc:
            outcome = self._provider_failure(
                resolved_model,
                exc,
                conversation_id=state.conversation_id,
            )
            self._finish(
                state.recorder,
                state.journal_started,
                "failed",
                "provider_error",
                state.control,
            )
            close_terminal_owner()
            self._mark_completed_if_active(state.control)
            emit_runtime_event(
                safe_event_sink,
                ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded),
            )
            return finish(outcome, CompletionReason.NORMAL)
        except BaseException:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise

        persistence = self._require_dependency("persistence")
        try:
            self._phase("result_normalize")
            self._check_cancel(cancel_check, state.control)
            # ``normalized`` is already the sealed result; the phase is kept
            # for parity with sync diagnostics and golden ordering.
            self._phase("message_persist")
            persisted_turn = (
                self._pending_persistence_result(
                    invocation_holder["value"],
                    cast(AgentTurnResult, raw_result),
                )
                if normalized.pending is not None
                else self._persist_result(
                    persistence,
                    state.conversation_id,
                    cast(StartTurnRequest, state.request),
                    normalized,
                    state.conversation,
                    catalog=segment.catalog,
                    route_handle=None,
                    presentation=None,
                    ensure_active=lambda: self._check_cancel(cancel_check, state.control),
                    control=state.control,
                )
            )
            self._record_journal_persisted(
                state.recorder,
                state.journal_started,
                persistence,
                state.conversation_id,
                persisted_turn.message_ids,
                state.control,
            )
            outcome = persisted_turn.outcome
            if isinstance(outcome, ConfirmationRequiredOutcome):
                self._suspend(
                    state.recorder,
                    state.journal_started,
                    normalized.pending,
                    state.control,
                    catalog=segment.catalog,
                )
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                if outcome.pending_action is not None:
                    emit_runtime_event(
                        safe_event_sink,
                        StatusEvent(phase="waiting_confirmation", label="需要确认"),
                    )
                    emit_runtime_event(
                        safe_event_sink,
                        ConfirmationRequiredEvent(
                            confirmation_token=outcome.confirmation_token,
                            operation_id=outcome.operation_id,
                            pending_action=outcome.pending_action,
                        ),
                    )
            elif isinstance(outcome, RuntimeFailureOutcome):
                self._finish(
                    state.recorder, state.journal_started, "failed", "unknown", state.control
                )
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                emit_runtime_event(
                    safe_event_sink,
                    ErrorEvent(
                        outcome.code,
                        outcome.message,
                        outcome.retryable,
                        outcome.degraded,
                        pending_action=outcome.pending_action,
                    ),
                )
            else:
                self._finish(
                    state.recorder, state.journal_started, "completed", None, state.control
                )
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                if isinstance(outcome, (MessageOutcome, OperationReplayOutcome)):
                    emit_runtime_event(
                        safe_event_sink, AssistantMessageEvent(message=outcome.message)
                    )
            emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
            return finish(outcome, CompletionReason.NORMAL)
        except RuntimeCancelled:
            abort(CompletionReason.CANCELLED)
            raise
        except RuntimeTransportAborted:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise
        except (RuntimeAgentTimedOut,):
            abort(CompletionReason.CANCELLED)
            raise
        except Exception:
            outcome = self._failure(
                RuntimeFailureCode.OPERATION_FAILED,
                "对话结果暂时无法保存。",
                503,
                retryable=True,
            )
            try:
                self._finish(
                    state.recorder, state.journal_started, "failed", "unknown", state.control
                )
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise
            emit_runtime_event(
                safe_event_sink,
                ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded),
            )
            emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
            return finish(outcome, CompletionReason.NORMAL)
        except BaseException:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise

    def _stream_immediate(
        self,
        outcome: RuntimeFailureOutcome,
        control: RuntimeInvocationControl,
        *,
        direct: bool = False,
    ) -> ImmediateHttpOutcome:
        self._mark_completed_if_active(control)
        payload: dict[str, object] = {
            "error": outcome.message,
            "error_code": outcome.code.value,
            "_runtime_stream_retryable": outcome.retryable,
            "_runtime_stream_degraded": outcome.degraded,
            "_runtime_stream_direct": direct or outcome.status_code == 422,
        }
        if outcome.pending_action is not None:
            payload["pending_action"] = outcome.pending_action.as_mapping()
        return ImmediateHttpOutcome(
            status_code=outcome.status_code,
            payload=freeze_json_mapping(payload),
        )

    @staticmethod
    def _ledger_direct_events(
        outcome: RuntimeOutcome,
        *,
        pending: PendingAction | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        rejected: bool = False,
    ) -> tuple[RuntimeEvent, ...]:
        """Build the complete typed event prefix for provider-free branches."""

        operation_id = _attribute(outcome, "operation_id")
        replay = outcome if isinstance(outcome, OperationReplayOutcome) else None
        call_id = (
            replay.tool_call_id
            if replay is not None
            else pending.tool_call_id
            if pending is not None
            else tool_call_id
        )
        name = (
            replay.tool_name
            if replay is not None
            else pending.tool_name
            if pending is not None
            else tool_name
        )
        if not call_id or not name:
            raise WriteOperationError("operation_integrity_error")
        write_status = _attribute(outcome, "write_status")
        result_status = (
            # The baseline SSE contract represents a rejected write as a
            # failed tool result even though the HTTP write status is
            # ``cancelled``.  Keep that distinction on the typed event
            # boundary; clients use ``confirm_mode=rejected`` to explain the
            # cancellation and ``status=error`` to match the old route.
            "error"
            if rejected
            else "cancelled"
            if write_status == "cancelled"
            else "success"
            if write_status == "success"
            else "error"
        )
        confirmation_mode = "rejected" if rejected else "approved"
        message = str(_attribute(outcome, "message", "") or "")
        summary = (
            replay.summary
            if replay is not None
            else ("用户已拒绝，操作未执行。" if rejected else message)
        )
        visible_result = replay.visible_result if replay is not None else message
        evidence = replay.evidence if replay is not None else ()
        affected_resources = replay.affected_resources if replay is not None else ()
        changed_entities = replay.changed_entities if replay is not None else ()
        return (
            MetaEvent(supports_delta=False, supports_tool_events=True),
            StatusEvent(
                phase="thinking" if rejected else "completed",
                label="正在根据你的反馈继续" if rejected else "已完成",
            ),
            ToolCallEvent(
                tool_call_id=call_id,
                tool_name=name,
                public_label="已取消的写入操作" if rejected else name,
                kind="write",
                confirm_mode=cast(Any, confirmation_mode),
                summary=summary,
            ),
            ToolResultEvent(
                tool_call_id=call_id,
                tool_name=name,
                status=cast(Any, result_status),
                summary=summary,
                evidence=evidence,
                affected_resources=affected_resources,
                changed_entities=changed_entities,
                message=visible_result,
                visible_result=visible_result,
                operation_id=str(operation_id) if operation_id else None,
                write_status=cast(Any, write_status) if write_status else None,
            ),
            AssistantMessageEvent(message=message),
        )

    def _prepare_deterministic_stream(
        self,
        execution: DeterministicExecution,
        *,
        request: StartTurnRequest | ConfirmationRequest,
        conversation: object,
        conversation_id: int,
        transport: RuntimeTransportContext,
        invocation_control: RuntimeInvocationControl,
        recorder: object | None = None,
        journal_started: bool = False,
    ) -> ImmediateHttpOutcome | PreparedStreamExecution:
        """Freeze a provider-free result before SSE headers are sent."""

        outcome = execution.outcome
        events = execution.events
        if isinstance(request, ConfirmationRequest):
            # Confirmation/replay never represents a new user message.  Keep
            # the direct branch as complete as the Agent branch by projecting
            # the typed meta/status/tool/result/assistant sequence.
            events = tuple(
                event for event in events if not isinstance(event, UserMessageSavedEvent)
            )
            if not events and isinstance(outcome, ConfirmationRequiredOutcome):
                if outcome.pending_action is not None:
                    events = (
                        MetaEvent(supports_delta=False, supports_tool_events=True),
                        StatusEvent(phase="waiting_confirmation", label="需要确认"),
                        ConfirmationRequiredEvent(
                            confirmation_token=outcome.confirmation_token,
                            operation_id=outcome.operation_id,
                            pending_action=outcome.pending_action,
                        ),
                    )
            if not events and isinstance(outcome, (MessageOutcome, OperationReplayOutcome)):
                events = self._ledger_direct_events(
                    outcome,
                    rejected=outcome.write_status == "cancelled",
                )
        if isinstance(outcome, RuntimeFailureOutcome):
            return self._stream_immediate(outcome, invocation_control, direct=True)
        if isinstance(outcome, OperationPendingOutcome):
            status = 409 if outcome.code is RuntimeFailureCode.OPERATION_DELIVERY_PENDING else 503
            self._mark_completed_if_active(invocation_control)
            return ImmediateHttpOutcome(
                status_code=status,
                payload=freeze_json_mapping(
                    {
                        "error": outcome.message,
                        "error_code": outcome.code.value,
                        "operation_id": outcome.operation_id,
                        "_runtime_stream_direct": True,
                    }
                ),
            )
        transport_run_id = transport.transport_run_id
        if transport_run_id is None:  # pragma: no cover - transport validates this
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400
                ),
                invocation_control,
            )
        cell = _PreparedExecutionCell(run_open=False)

        def on_abort() -> None:
            with cell.lock:
                if cell.aborted or cell.completed:
                    return
                cell.aborted = True
                cell.run_open = False
            if invocation_control.state is InvocationState.ACTIVE:
                invocation_control.mark_completed()

        state = _PreparedStreamState(
            owner_token=self._owner_token,
            preparation_kind=execution.preparation_kind,
            execution_mode=StreamExecutionMode.DIRECT,
            control=invocation_control,
            request=request,
            conversation=_prepared_conversation(conversation, conversation_id),
            conversation_id=conversation_id,
            transport=transport,
            recorder=recorder or _NoopRecorder(),
            journal_started=journal_started,
            cell=cell,
            events=events,
            outcome=outcome,
            on_abort=on_abort,
        )
        try:
            prepared = PreparedStreamExecution(
                invocation_id=transport_run_id,
                preparation_kind=execution.preparation_kind,
                execution_mode=StreamExecutionMode.DIRECT,
                opaque_state=state,
            )
        except BaseException:
            raise
        self._phase("prepared")
        return prepared

    def _prepare_ledger_confirmation_stream(
        self,
        coordinator: ConfirmationCoordinator,
        request: ConfirmationRequest,
        conversation: object | None,
        conversation_id: int,
        transport: RuntimeTransportContext,
        invocation_control: RuntimeInvocationControl,
        *,
        replay: RuntimeOutcome | None = None,
        terminal_checked: bool = False,
        preflight_pending: PendingAction | None = None,
        defer_approval: bool = False,
    ) -> ImmediateHttpOutcome | PreparedStreamExecution:
        """Freeze Ledger replay/rejection, or lease approved Agent work."""

        if not terminal_checked:
            try:
                replay = coordinator.replay_outcome(request)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
        if replay is not None:
            return self._prepare_deterministic_stream(
                DeterministicExecution(
                    replay,
                    preparation_kind=PreparationKind.REPLAY,
                ),
                request=request,
                conversation=conversation,
                conversation_id=conversation_id,
                transport=transport,
                invocation_control=invocation_control,
            )

        if not request.approved:
            session: ConfirmationSession | None = None
            recorder: object = _NoopRecorder()
            journal_started = False
            try:
                try:
                    session = coordinator.reject(request, conversation=conversation)
                except ConfirmationReplayError:
                    replayed = coordinator.replay_outcome(request)
                    if replayed is not None:
                        return self._prepare_deterministic_stream(
                            DeterministicExecution(
                                replayed,
                                preparation_kind=PreparationKind.REPLAY,
                            ),
                            request=request,
                            conversation=conversation,
                            conversation_id=conversation_id,
                            transport=transport,
                            invocation_control=invocation_control,
                        )
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                            "operation result is unavailable",
                            503,
                            retryable=True,
                        ),
                        invocation_control,
                    )
                recorder, journal_started = self._open_ledger_journal(
                    session,
                    conversation
                    if conversation is not None
                    else SimpleNamespace(
                        id=conversation_id,
                        context_type="workspace",
                        context_ref="",
                        mode="general",
                    ),
                    transport,
                    invocation_control,
                    execution_path="agent_resume",
                )
                claimed = session.on_confirmation_attempt(session.pending, None)
                if isinstance(session.state.terminal_execution, OperationReplay):
                    coordinator.cancel_cleanup(session)
                    self._abandon(recorder, journal_started)
                    try:
                        replayed = coordinator.replay_outcome(request)
                    except Exception as exc:
                        return self._stream_immediate(
                            self._confirmation_failure(exc), invocation_control
                        )
                    if replayed is not None:
                        return self._prepare_deterministic_stream(
                            DeterministicExecution(
                                replayed,
                                preparation_kind=PreparationKind.REPLAY,
                            ),
                            request=request,
                            conversation=conversation,
                            conversation_id=conversation_id,
                            transport=transport,
                            invocation_control=invocation_control,
                        )
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                            "operation result is unavailable",
                            503,
                            retryable=True,
                        ),
                        invocation_control,
                    )
                if isinstance(claimed, ToolFailure):
                    self._abandon(recorder, journal_started)
                    return self._stream_immediate(
                        self._failure(RuntimeFailureCode.STALE_PENDING_ACTION, claimed.code, 409),
                        invocation_control,
                    )
                self._check_cancel(lambda: False, invocation_control)
                visible = str(
                    _attribute(
                        _attribute(session.state.terminal_execution, "payload"),
                        "visible_result",
                        "已取消这次操作。",
                    )
                    or "已取消这次操作。"
                )
                if session.state.delivered:
                    outcome = MessageOutcome(
                        message=visible,
                        conversation_id=request.conversation_id,
                        write_status="cancelled",
                        undo=self._previous_write_undo(conversation, conversation_id),
                        operation_id=session.state.identity.operation_id,
                        persisted=True,
                        legacy_projection=True,
                    )
                    self._close_ledger_journal(
                        recorder,
                        journal_started,
                        outcome,
                        invocation_control,
                    )
                    return self._prepare_deterministic_stream(
                        DeterministicExecution(
                            outcome,
                            events=self._ledger_direct_events(
                                outcome,
                                pending=session.pending,
                                rejected=True,
                            ),
                            preparation_kind=PreparationKind.CONFIRMATION,
                        ),
                        request=request,
                        conversation=conversation,
                        conversation_id=conversation_id,
                        transport=transport,
                        invocation_control=invocation_control,
                    )
                origin = Message(
                    role="tool",
                    content=_CANCELLED_TOOL_RESULT,
                    tool_call_id=session.pending.tool_call_id,
                )
                session.on_confirmation_result(session.pending, False, origin, None)
                delivered = coordinator.final_delivery(
                    session,
                    DeliveryBundle((Message(role="assistant", content=visible),)),
                )
                self._record_ledger_delivery_journal(
                    recorder, journal_started, session, invocation_control
                )
                delivered_status = _failure_status(delivered)
                if delivered is None or delivered_status in {"cas_lost", "closed", "not_found"}:
                    self._abandon(recorder, journal_started)
                    if delivered_status == "cas_lost":
                        return self._stream_immediate(
                            self._failure(
                                RuntimeFailureCode.STALE_PENDING_ACTION,
                                "待确认操作已被更新，请刷新对话后重试。",
                                409,
                                retryable=True,
                            ),
                            invocation_control,
                        )
                    return self._stream_immediate(
                        self._failure(
                            RuntimeFailureCode.OPERATION_DELIVERY_PENDING
                            if delivered is None
                            else RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                            "确认结果正在处理中，请刷新对话查看结果。"
                            if delivered is None
                            else "对话结果暂时无法保存。",
                            409 if delivered is None else 503,
                            retryable=True,
                        ),
                        invocation_control,
                    )
                outcome = MessageOutcome(
                    message=visible,
                    conversation_id=conversation_id,
                    write_status="cancelled",
                    undo=self._previous_write_undo(conversation, conversation_id),
                    operation_id=session.state.identity.operation_id,
                    legacy_projection=True,
                )
                self._close_ledger_journal(
                    recorder,
                    journal_started,
                    outcome,
                    invocation_control,
                )
                return self._prepare_deterministic_stream(
                    DeterministicExecution(
                        outcome,
                        events=self._ledger_direct_events(
                            outcome,
                            pending=session.pending,
                            rejected=True,
                        ),
                        preparation_kind=PreparationKind.CONFIRMATION,
                    ),
                    request=request,
                    conversation=conversation,
                    conversation_id=conversation_id,
                    transport=transport,
                    invocation_control=invocation_control,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                if session is not None:
                    coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                raise
            except Exception as exc:
                if session is not None:
                    coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                return self._stream_immediate(self._confirmation_failure(exc), invocation_control)
            except BaseException:
                if session is not None:
                    coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                raise

        if defer_approval:
            if not request.approved or conversation is not None:
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            cell = _PreparedExecutionCell(run_open=False)
            confirmation_cell = _PreparedConfirmationCell()

            def deferred_on_abort() -> None:
                session = confirmation_cell.session
                if session is not None:
                    coordinator.cancel_cleanup(session)
                with cell.lock:
                    cell.aborted = True
                    cell.run_open = False

            def deferred_on_complete(_reason: CompletionReason) -> None:
                with cell.lock:
                    cell.run_open = False

            state = _PreparedStreamState(
                owner_token=self._owner_token,
                preparation_kind=PreparationKind.CONFIRMATION,
                execution_mode=StreamExecutionMode.AGENT_HOST,
                control=invocation_control,
                request=request,
                conversation=None,
                conversation_id=conversation_id,
                assembled=(),
                cell=cell,
                transport=transport,
                confirmation_pending=preflight_pending,
                confirmation_cell=confirmation_cell,
                on_abort=deferred_on_abort,
                on_complete=deferred_on_complete,
            )
            transport_run_id = transport.transport_run_id
            if transport_run_id is None:
                deferred_on_abort()
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        400,
                    ),
                    invocation_control,
                )
            return PreparedStreamExecution(
                invocation_id=transport_run_id,
                preparation_kind=PreparationKind.CONFIRMATION,
                execution_mode=StreamExecutionMode.AGENT_HOST,
                opaque_state=state,
            )

        # Typed approved continuation is always prepared with the deferred
        # state above.  There is intentionally no second approval/session
        # path that can load Conversation or Source during header preparation.
        raise RuntimeError("typed approval must use deferred continuation preparation")

    def _execute_prepared_ledger_confirmation(
        self,
        state: _PreparedStreamState,
        event_sink: RuntimeEventSink,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost[object],
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome:
        coordinator = cast(ConfirmationCoordinator, self._confirmation_coordinator())

        def finish_pre_agent(outcome: RuntimeOutcome) -> RuntimeOutcome:
            """Close a deferred approval that never entered the Agent host.

            Response-header preparation intentionally emits no events because
            the continuation model is still unknown.  If the live claim then
            loses a race or fails before invocation construction, this body
            path still owns the complete SSE prefix and Runtime control.  Use
            the provider-free metadata shape, project replay text before the
            outer ``CompletedEvent``, and terminalize control exactly as the
            normal confirmation finalizer does.
            """

            emit_runtime_event(event_sink, MetaEvent(supports_delta=False))
            if isinstance(outcome, OperationReplayOutcome):
                emit_runtime_event(
                    event_sink,
                    AssistantMessageEvent(message=outcome.message),
                )
            self._mark_completed_if_active(state.control)
            return outcome

        session = cast(Any, state.confirmation_session or state.confirmation_cell.session)
        approval_catalog_lease: SegmentToolCatalogLease | None = None
        if session is None:
            request = cast(ConfirmationRequest, state.request)
            pending = state.confirmation_pending
            if pending is None:
                return finish_pre_agent(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        400,
                    )
                )
            approval_catalog_lease = self.metadata_bundle.open_segment_lease()
            approval_spec_handle = approval_catalog_lease.resolve(pending.tool_name)
            if type(approval_spec_handle) is not SegmentToolSpecHandle:
                approval_catalog_lease.close()
                return finish_pre_agent(
                    self._failure(
                        RuntimeFailureCode.OPERATION_UNAVAILABLE,
                        "unsupported runtime route",
                        400,
                    )
                )
            try:
                session_or_replay = coordinator.approve_modify(
                    request,
                    pending=pending,
                    conversation=None,
                    catalog_lease=approval_catalog_lease,
                    spec_handle=approval_spec_handle,
                )
                if isinstance(session_or_replay, OperationReplay):
                    approval_catalog_lease.close()
                    replay = coordinator.replay_outcome(request)
                    if replay is None:
                        return finish_pre_agent(
                            self._failure(
                                RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                                "operation result is unavailable",
                                503,
                                retryable=True,
                            )
                        )
                    return finish_pre_agent(replay)
                session = session_or_replay
                state.confirmation_cell.session = session
                object.__setattr__(state, "confirmation_session", session)
            except ConfirmationReplayError:
                approval_catalog_lease.close()
                replay = coordinator.replay_outcome(request)
                if replay is not None:
                    return finish_pre_agent(replay)
                return finish_pre_agent(
                    self._failure(
                        RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                        "operation result is unavailable",
                        503,
                        retryable=True,
                    )
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                approval_catalog_lease.close()
                raise
            except Exception as exc:
                approval_catalog_lease.close()
                return finish_pre_agent(self._confirmation_failure(exc))
            except BaseException:
                approval_catalog_lease.close()
                raise
        else:
            approval_catalog_lease = getattr(session.state, "approval_catalog_lease", None)
        assert session is not None
        if type(approval_catalog_lease) is not SegmentToolCatalogLease:
            coordinator.cancel_cleanup(session)
            return finish_pre_agent(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "unsupported runtime route",
                    400,
                )
            )
        driver = self._dependencies.agent_driver
        if driver is None:
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            return finish_pre_agent(
                self._failure(
                    RuntimeFailureCode.OPERATION_UNAVAILABLE,
                    "unsupported runtime route",
                    400,
                )
            )
        catalog = self._dependencies.catalog
        recorder: object = _NoopRecorder()
        journal_started = False
        try:
            self._check_cancel(cancel_check, state.control)
            recorder, journal_started = self._open_ledger_journal(
                session,
                state.conversation,
                state.transport or RuntimeTransportContext(mode="stream"),
                state.control,
                tool_names=self._journal_tool_names(self._provider_metadata_view()),
            )
            activation_request = _ContinuationActivationRequest(cast(int, state.conversation_id))
            # See the synchronous path: the approval origin uses the raw
            # journal recorder.  The post-terminal Segment owns the gated
            # proxy used for any chained Pending.
            tool_context = session.approval_execution_context(recorder)
            approval_catalog_lease = self._open_approval_catalog_lease(
                tool_context,
                catalog,
                approval_catalog_lease,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            coordinator = cast(ConfirmationCoordinator, self._confirmation_coordinator())
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            raise
        except Exception as exc:
            coordinator = cast(ConfirmationCoordinator, self._confirmation_coordinator())
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            bind_outcome = self._confirmation_failure(exc)
            return finish_pre_agent(bind_outcome)
        except BaseException:
            coordinator = cast(ConfirmationCoordinator, self._confirmation_coordinator())
            coordinator.cancel_cleanup(session)
            approval_catalog_lease.close()
            self._abandon(recorder, journal_started)
            raise
        auto_approve = False
        max_iter = DEFAULT_MAX_ITERATIONS
        request = cast(ConfirmationRequest, state.request)
        deferred_origin_events: list[RuntimeEvent] = []
        origin_tool_call_id = session.pending.tool_call_id
        invocation_construction_failed = False
        invocation_holder: dict[str, AgentLoopInvocation] = {}

        def invoke_driver(agent_events: RuntimeEventSink) -> object:
            nonlocal invocation_construction_failed
            confirmation_events = _NegotiatedConfirmationEventSink(
                agent_events,
                origin_tool_call_id,
                deferred_origin_events,
            )

            def build_continuation_segment() -> ApprovedContinuationSegment:
                segment = self._build_confirmation_segment(
                    session,
                    activation_request,
                    recorder,
                    journal_started,
                    state.control,
                    cancel_check,
                )
                confirmation_events.negotiate(segment.model)
                return segment

            session.continuation_segment_builder = build_continuation_segment
            try:
                try:
                    invocation = AgentLoopInvocation(
                        seed=ApprovedWriteSeed(ConfirmationApprovedWritePort(session)),
                        model=None,
                        catalog=cast(Any, catalog),
                        catalog_lease=approval_catalog_lease,
                        tool_context=cast(Any, tool_context),
                        auto_approve=auto_approve,
                        max_iterations=max_iter,
                        run_recorder=cast(Any, recorder),
                        event_sink=cast(
                            Any,
                            confirmation_events,
                        ),
                        runtime_signal_sink=signal_sink,
                        cancel_check=self._confirmation_cancel_check(state.control, cancel_check),
                    )
                    self._bind_invocation_pending_persistence(
                        invocation,
                        lambda turn, route_handle, presentation: (
                            self._persist_chained_pending_before_release(
                                coordinator,
                                session,
                                request,
                                state.control,
                                turn,
                                route_handle,
                                presentation,
                            )
                        ),
                    )
                    invocation_holder["value"] = invocation
                except Exception:
                    invocation_construction_failed = True
                    raise
                return self._run_driver(driver, invocation)
            finally:
                # Direct/failing test Drivers may stop before activating a
                # post-terminal Segment.  Preserve the closed stream prefix
                # while deriving ``supports_delta`` from any observed delta.
                confirmation_events.negotiate()

        coordinator = cast(ConfirmationCoordinator, self._confirmation_coordinator())
        try:
            if callable(getattr(execution_host, "iter_events", None)):
                streamed = cast(Any, execution_host).run(invoke_driver, state.control)
                if hasattr(streamed, "__next__"):
                    try:
                        for agent_event in cast(Iterable[object], streamed):
                            typed_agent_event = cast(RuntimeEvent, agent_event)
                            if (
                                isinstance(typed_agent_event, ToolResultEvent)
                                and typed_agent_event.tool_call_id == origin_tool_call_id
                            ):
                                # ``SseAgentExecutionHost`` yields the same
                                # typed event that the injected sink already
                                # observed.  Keep one origin result in the
                                # post-delivery release queue while still
                                # accepting hosts that only yield events.
                                if typed_agent_event not in deferred_origin_events:
                                    deferred_origin_events.append(typed_agent_event)
                            else:
                                emit_runtime_event(event_sink, typed_agent_event)
                        result_reader = getattr(streamed, "result", None)
                        raw_result = result_reader() if callable(result_reader) else result_reader
                    finally:
                        close = getattr(streamed, "close", None)
                        if callable(close):
                            close()
                else:
                    raw_result = streamed
            else:
                raw_result = execution_host.run(lambda: invoke_driver(event_sink), state.control)
            normalized = _normalize_agent_result(raw_result)
            invocation = invocation_holder.get("value")
            outcome: RuntimeOutcome = (
                cast(
                    RuntimeOutcome,
                    invocation._pending_persistence_result(cast(AgentTurnResult, raw_result)),
                )
                if normalized.pending is not None and invocation is not None
                else self._finish_ledger_confirmation(
                    coordinator,
                    session,
                    normalized,
                    request,
                    state.control,
                )
            )
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                state.control,
                pending=normalized.pending,
                catalog=catalog,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            self._mark_completed_if_active(state.control)
            if isinstance(outcome, (MessageOutcome, OperationReplayOutcome)):
                emit_runtime_event(event_sink, AssistantMessageEvent(message=outcome.message))
            return outcome
        except RuntimeAgentTimedOut:
            try:
                fallback = coordinator.timeout_convergence(session)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception as exc:
                fallback = None
                timeout_error: BaseException | None = exc
            else:
                timeout_error = None
            state_value = session.state
            self._stop_confirmation_heartbeat(coordinator, session)
            if timeout_error is not None:
                outcome = self._confirmation_failure(timeout_error)
            elif state_value.cas_lost:
                outcome = self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            elif state_value.delivered:
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state_value),
                    conversation_id=request.conversation_id,
                    write_status="success" if state_value.succeeded else "failed",
                    undo=self._confirmation_undo(state_value),
                    operation_id=state_value.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            elif _failure_status(fallback) in {
                PersistenceStatus.PERSISTED.value,
                PersistenceStatus.DUPLICATE.value,
            }:
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state_value),
                    conversation_id=request.conversation_id,
                    write_status="success" if state_value.succeeded else "failed",
                    undo=self._confirmation_undo(state_value),
                    operation_id=state_value.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            elif state_value.confirmation_attempted:
                outcome = self._failure(
                    RuntimeFailureCode.CONFIRMATION_IN_PROGRESS,
                    "确认操作仍在后台执行，请刷新对话查看结果，不要重复提交。",
                    409,
                    retryable=False,
                )
            else:
                outcome = self._failure(
                    RuntimeFailureCode.CHAT_AGENT_TIMEOUT,
                    CHAT_TIMEOUT_MESSAGE,
                    504,
                    retryable=True,
                )
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                state.control,
                allow_timeout=True,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            if isinstance(outcome, MessageOutcome):
                emit_runtime_event(event_sink, AssistantMessageEvent(message=outcome.message))
            return outcome
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        except ConfirmationReplayError:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            try:
                replay = coordinator.replay_outcome(request)
            except Exception as exc:
                outcome = self._confirmation_failure(exc)
                return outcome
            if replay is not None:
                if isinstance(replay, OperationReplayOutcome):
                    emit_runtime_event(event_sink, AssistantMessageEvent(message=replay.message))
                return replay
            raise
        except Exception as exc:
            if invocation_construction_failed:
                coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                raise
            if _attribute(exc, "code") == RuntimeFailureCode.OPERATION_INTEGRITY_ERROR.value:
                coordinator.cancel_cleanup(session)
                self._abandon(recorder, journal_started)
                outcome = self._confirmation_failure(exc)
                return outcome
            state_value = session.state
            try:
                failure_delivery: object = coordinator.fallback(session)
            except Exception as delivery_error:
                failure_delivery = None
                delivery_error_value: BaseException | None = delivery_error
            else:
                delivery_error_value = None
            if delivery_error_value is not None:
                outcome = self._confirmation_failure(delivery_error_value)
            elif state_value.cas_lost:
                outcome = self._failure(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新对话后重试。",
                    409,
                    retryable=True,
                )
            elif _failure_status(failure_delivery) in {
                PersistenceStatus.PERSISTED.value,
                PersistenceStatus.DUPLICATE.value,
            }:
                coordinator._hydrate_terminal_undo(state_value)
                outcome = MessageOutcome(
                    message=coordinator._fallback_message(state_value),
                    conversation_id=request.conversation_id,
                    write_status="success" if state_value.succeeded else "failed",
                    undo=self._confirmation_undo(state_value),
                    operation_id=state_value.identity.operation_id,
                    persisted=True,
                    legacy_projection=True,
                )
            else:
                outcome = self._provider_confirmation_failure(exc)
            self._finalize_confirmation_result(
                recorder,
                journal_started,
                session,
                outcome,
                state.control,
                event_sink=event_sink,
                deferred_origin_events=deferred_origin_events,
            )
            if isinstance(outcome, MessageOutcome):
                emit_runtime_event(event_sink, AssistantMessageEvent(message=outcome.message))
            return outcome
        except BaseException:
            coordinator.cancel_cleanup(session)
            self._abandon(recorder, journal_started)
            raise
        finally:
            self._stop_confirmation_heartbeat(coordinator, session)
            invocation = invocation_holder.get("value")
            if invocation is None:
                approval_catalog_lease.close()
            else:
                invocation._release_catalog_lease_from_runtime()

    @staticmethod
    def _mark_completed_if_active(control: RuntimeInvocationControl) -> None:
        if control.state is InvocationState.ACTIVE:
            if not control.mark_completed():
                require_runtime_active(control)

    # ---- state-machine stages -------------------------------------------------

    def _require_dependency(self, name: str) -> object:
        if name == "conversation_gateway":
            value = (
                self._dependencies.conversations
                if self._dependencies.conversations is not None
                else self._dependencies.conversation_gateway
                if self._dependencies.conversation_gateway is not None
                else self._dependencies.conversation_store
            )
        else:
            value = getattr(self._dependencies, name)
        if value is None:
            raise TypeError(f"PilotRuntime dependency {name} is required")
        return value

    def _validate(self, request: StartTurnRequest | ConfirmationRequest) -> None:
        validator = _callable(self._dependencies.validator, ("validate", "validate_request"))
        if validator is not None:
            _invoke(validator, {"request": request}, (request,))

    def _phase(self, name: str) -> None:
        sink = _callable(self._dependencies.phase_sink, ("append", "record", "phase"))
        if sink is not None:
            try:
                sink(name)
            except BaseException:
                # Phase diagnostics are sync-only and fail-open; they must not
                # hide the recorder's authoritative disposition or exception.
                return

    def _load_conversation(self, request: StartTurnRequest) -> object | None:
        gateway = self._require_dependency("conversation_gateway")
        if request.conversation_id in (None, 0):
            function = _callable(gateway, ("create", "create_conversation", "create_or_load"))
            if function is None:
                raise TypeError("conversation gateway does not provide create")
            return _invoke(
                function,
                {
                    "request": request,
                    "payload": request,
                    "message": request.message,
                    "title": _title_from_message(request.message),
                    "mode": request.mode,
                    "context_type": request.context_type,
                    "context_ref": request.context_ref,
                },
                (request,),
            )
        function = _callable(gateway, ("load", "get", "get_conversation", "create_or_load"))
        if function is None:
            raise TypeError("conversation gateway does not provide load")
        return _invoke(
            function,
            {"conversation_id": request.conversation_id, "id": request.conversation_id},
            (request.conversation_id,),
        )

    def _load_confirmation_conversation(self, conversation_id: int) -> object | None:
        """Load-only conversation path for confirmation preheader checks."""

        gateway = self._require_dependency("conversation_gateway")
        function = _callable(gateway, ("load", "get", "get_conversation", "create_or_load"))
        if function is None:
            raise TypeError("conversation gateway does not provide load")
        return _invoke(
            function,
            {"conversation_id": conversation_id, "id": conversation_id},
            (conversation_id,),
        )

    def _confirmation_messages_exist(self, conversation_id: int) -> bool:
        """Mirror the legacy approved-confirmation message existence guard."""

        persistence = self._dependencies.persistence
        getter = _callable(persistence, ("list_messages",))
        if getter is None:
            # Narrow fakes and persistence implementations that expose only
            # the confirmation atoms remain valid; the loaded Conversation is
            # the available existence proof in that case.
            return True
        try:
            value = _invoke(
                getter,
                {"conversation_id": conversation_id},
                (conversation_id,),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return False
        return bool(value)

    def _select_route(
        self,
        request: StartTurnRequest,
        conversation: object,
    ) -> RouteKind | RuntimeFailureOutcome:
        try:
            adapter = self._dependencies.deterministic
            if adapter is not None and adapter.matches(request, conversation):
                return RouteKind.DETERMINISTIC
            selector = self._dependencies.route_selector
            if selector is None:
                return _route_kind(None, request)
            function = _callable(selector, ("select", "select_route", "route"))
            if function is None:
                return _route_kind(None, request)
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "conversation_id": _conversation_id(conversation),
                },
                (request, conversation),
            )
            return _route_kind(value, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "unsupported runtime route",
                503,
                retryable=True,
            )

    def _validate_route_action(self, request: StartTurnRequest) -> RuntimeFailureOutcome | None:
        if request.pilot_action is None:
            return None
        adapter = self._dependencies.deterministic
        if adapter is None:
            return None
        function = _callable(adapter, ("validate_action",))
        if function is None:
            return None
        try:
            _invoke(function, {"request": request}, (request,))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except ValueError as exc:
            return self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422)
        except Exception:
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "unsupported runtime route",
                503,
                retryable=True,
            )
        return None

    def _pending_guard(
        self, conversation_id: int, conversation: object, request: StartTurnRequest
    ) -> object | None:
        target = self._dependencies.pending_guard or self._dependencies.persistence
        function = _callable(
            target, ("get_pending_action", "pending_guard", "get_live_pending", "check")
        )
        if function is None:
            return None
        return _invoke(
            function,
            {"conversation_id": conversation_id, "conversation": conversation, "request": request},
            (conversation_id,),
        )

    def _resolve_policy_catalog(
        self,
        request: _RuntimeRequest,
        conversation: object,
        source: object,
        segment: SegmentExecution | None = None,
    ) -> ResolvedPolicyCatalog | RuntimeFailureOutcome:
        resolver = self._require_dependency("policy_catalog_resolver")
        function = _callable(resolver, ("resolve", "resolve_policy", "resolve_catalog"))
        if function is None:
            raise TypeError("policy/catalog resolver does not provide resolve")
        try:
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "source": source,
                    "segment": segment,
                },
                (request, conversation, source, segment),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        except BaseException:
            raise
        if isinstance(value, RuntimeFailureOutcome):
            return value
        if (
            type(value) is not ResolvedPolicyCatalog
            or type(segment) is not SegmentExecution
            or type(segment.authority) is not SegmentExecutionAuthority
            or type(segment.context) is not ToolExecutionContext
            or segment.context.authority is not segment.authority
            or segment.catalog is not None
            or segment.policy is not None
            or segment.surface_gate is not None
            or value.catalog is None
            or value.policy is None
            or value.dependency_policy is None
        ):
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        profile = _attribute(value.policy, "capability_profile")
        capabilities = _attribute(profile, "capabilities")
        authority = segment.authority
        if (
            type(capabilities) is not tuple
            or authority.capability_profile_id != _attribute(profile, "profile_id")
            or authority.capabilities != frozenset(capabilities)
            or authority.capability_policy_version
            != _attribute(value.policy, "capability_policy_version")
            or authority.binding_policy_version
            != _attribute(value.policy, "binding_policy_version")
            or authority.capability_profile_fingerprint
            != _attribute(value.policy, "capability_profile_fingerprint")
            or authority.binding_policy_fingerprint
            != _attribute(value.policy, "binding_policy_fingerprint")
        ):
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        return value

    def _resolve_segment_context(
        self,
        request: _RuntimeRequest,
        conversation: object,
        source: object,
        recorder: object,
    ) -> SegmentExecution | RuntimeFailureOutcome:
        resolver = self._require_dependency("segment_context_resolver")
        function = _callable(resolver, ("resolve", "resolve_segment", "resolve_context"))
        if function is None:
            raise TypeError("segment context resolver does not provide resolve")
        try:
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "source": source,
                    "recorder": recorder,
                },
                (request, conversation, source, recorder),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._failure(
                RuntimeFailureCode.SOURCE_LOAD_FAILED,
                "上下文暂时无法加载，请稍后重试。",
                503,
                retryable=True,
            )
        except BaseException:
            raise
        if isinstance(value, RuntimeFailureOutcome):
            return value
        valid = False
        if type(value) is SegmentExecution:
            try:
                valid = (
                    type(value.authority) is SegmentExecutionAuthority
                    and type(value.context) is ToolExecutionContext
                    and value.context.authority is value.authority
                    and value.catalog is None
                    and value.policy is None
                    and value.surface_gate is None
                    and callable(value.close)
                )
            except BaseException:
                self._safe_close_segment_candidate(value)
                raise
        if not valid:
            self._safe_close_segment_candidate(value)
            return self._failure(
                RuntimeFailureCode.SOURCE_LOAD_FAILED,
                "上下文暂时无法加载，请稍后重试。",
                503,
                retryable=True,
            )
        return cast(SegmentExecution, value)

    @staticmethod
    def _safe_close_segment_candidate(value: object) -> None:
        try:
            catalog_lease = getattr(value, "catalog_lease", None)
        except BaseException:
            catalog_lease = None
        if type(catalog_lease) is SegmentToolCatalogLease:
            try:
                catalog_lease.close()
            except BaseException:
                pass
        try:
            close = getattr(value, "close", None)
        except BaseException:
            return
        if not callable(close):
            return
        try:
            close()
        except BaseException:
            return

    def _resolve_surface_gate(
        self,
        request: _RuntimeRequest,
        conversation: object,
        source: object,
        assembled: object,
        policy: ResolvedPolicyCatalog,
        segment: SegmentExecution,
    ) -> SegmentSurfaceGate | RuntimeFailureOutcome:
        resolver = self._require_dependency("surface_gate_resolver")
        function = _callable(resolver, ("resolve", "resolve_surface_gate", "resolve_gate"))
        if function is None:
            raise TypeError("segment surface gate resolver does not provide resolve")
        try:
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "source": source,
                    "assembled": assembled,
                    "policy": policy,
                    "segment": segment,
                },
                (request, conversation, source, assembled, policy, segment),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        except BaseException:
            raise
        if isinstance(value, RuntimeFailureOutcome):
            return value
        if (
            type(value) is not SegmentSurfaceGate
            or value.authority is not segment.authority
            or value.context is not segment.context
            or value.dispatch_catalog is not segment.catalog
            or type(value.catalog_lease) is not SegmentToolCatalogLease
            or value.catalog_lease.closed
            or segment.policy is not policy
        ):
            if type(value) is SegmentSurfaceGate:
                value.catalog_lease.close()
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        try:
            segment.context.authority_factory.require_segment_surface_gate(
                value,
                authority=segment.authority,
                context=segment.context,
                catalog=value.catalog_lease,
                policy=value.policy,
                dependency_policy=value.dependency_policy,
                selection=value.selection,
                authority_surface=value.authority_surface,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            value.catalog_lease.close()
            raise
        except Exception:
            value.catalog_lease.close()
            return self._failure(
                RuntimeFailureCode.OPERATION_UNAVAILABLE,
                "工具权限暂时不可用，请稍后重试。",
                503,
                retryable=True,
            )
        except BaseException:
            value.catalog_lease.close()
            raise
        return value

    def _resolve_continuation_model(
        self,
        request: _RuntimeRequest,
        conversation: object,
        policy: ResolvedPolicyCatalog,
    ) -> ResolvedModel | RuntimeFailureOutcome | None:
        resolver = self._require_dependency("continuation_model_resolver")
        function = _callable(
            resolver,
            ("resolve", "resolve_model", "resolve_continuation_model"),
        )
        if function is None:
            raise TypeError("continuation model resolver does not provide resolve")
        try:
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "policy": policy,
                    "catalog": policy.catalog,
                },
                (request, conversation, policy),
            )
        except ModelUnconfiguredError:
            return self._failure(
                RuntimeFailureCode.MODEL_UNCONFIGURED,
                "AI is not configured: run `oc config` to set your API key",
                503,
                retryable=False,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
            )
        except BaseException:
            raise
        if isinstance(value, RuntimeFailureOutcome):
            return value
        if _explicitly_unconfigured_model(value):
            return None
        resolved = _resolved_model(value)
        if resolved is None:
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
            )
        return resolved

    @staticmethod
    def _commit_fence(
        control: RuntimeInvocationControl,
        action: Callable[[], object],
        *,
        allow_timeout: bool = False,
    ) -> object:
        fence = getattr(control, "run_if_active", None)
        if not callable(fence):
            raise TypeError("invocation control does not provide run_if_active")
        result = fence(action, allow_timeout=allow_timeout)
        if not isinstance(result, tuple) or len(result) != 2 or type(result[0]) is not bool:
            raise TypeError("run_if_active must return (bool, value)")
        if not result[0]:
            if allow_timeout:
                PilotRuntime._allow_timeout_persistence(control)
            else:
                require_runtime_active(control)
            raise RuntimeCancelled()
        return result[1]

    @staticmethod
    def _mark_completed(control: RuntimeInvocationControl) -> None:
        """Close the invocation only after Runtime-owned terminal work."""

        if control.mark_completed():
            return
        state = control.state
        if state is InvocationState.COMPLETED:
            raise RuntimeTransportAborted()
        require_runtime_active(control)
        raise RuntimeTransportAborted()

    @staticmethod
    def _validate_persistence_surface(persistence: object) -> RuntimeFailureOutcome | None:
        missing: list[str] = []
        for name in _REQUIRED_PERSISTENCE_METHODS:
            try:
                candidate = getattr(persistence, name, None)
            except Exception:
                candidate = None
            if not callable(candidate):
                missing.append(name)
        if missing:
            return PilotRuntime._failure(
                RuntimeFailureCode.OPERATION_FAILED,
                "对话结果暂时无法保存。",
                503,
                retryable=True,
            )
        return None

    def _persist_user(
        self,
        persistence: object,
        conversation_id: int,
        message: str,
        *,
        control: RuntimeInvocationControl,
    ) -> object:
        function = _callable(
            persistence,
            ("persist_initial_user_message", "persist_user_message", "persist_user", "append_user"),
        )
        if function is None:
            raise TypeError("persistence does not provide user message persistence")
        return self._commit_fence(
            control,
            lambda: _invoke(
                function,
                {"conversation_id": conversation_id, "content": message, "message": message},
                (conversation_id, message),
            ),
        )

    def _start_journal(
        self,
        conversation: object,
        conversation_id: int,
        input_message_id: object,
        request: StartTurnRequest,
        transport: RuntimeTransportContext,
        *,
        origin_kind: str = "user_message",
        route_kind: str = "model",
        request_kind: str = "initial",
        execution_path: str | None = None,
    ) -> tuple[object, bool]:
        factory = self._dependencies.journal
        if factory is None:
            return _NoopRecorder(), False
        function = getattr(factory, "start_run", None)
        if not callable(function):
            return _NoopRecorder(), False

        context_type = _attribute(conversation, "context_type", request.context_type)
        context_ref = _attribute(conversation, "context_ref", request.context_ref)
        application_visible = _callable(
            self._dependencies.application_visible,
            ("__call__", "is_visible", "application_visible"),
        )
        if application_visible is None:

            def application_visible(_application_id: int) -> bool:
                return False

        def build_start_command(key: object, budget_check: Callable[[], None]) -> StartRunCommand:
            budget_check()
            normalized = normalize_context_identity(
                context_type,
                context_ref,
                application_visible=cast(Callable[[int], bool], application_visible),
                key=key,  # type: ignore[arg-type]
                budget_check=budget_check,
            )
            run_id = str(uuid4())
            segment_id = str(uuid4())
            run_started = prepare_event(
                event_type="run.started",
                execution_segment_id=segment_id,
                facts={
                    "agent_run_id": run_id,
                    "origin_kind": origin_kind,
                    "conversation_id": conversation_id,
                    "context_type": normalized.context_type,
                    "transport_mode": transport.mode,
                },
                budget_check=budget_check,
            )
            segment_started = prepare_event(
                event_type="segment.started",
                execution_segment_id=segment_id,
                facts={
                    "request_kind": request_kind,
                    "transport_mode": transport.mode,
                    "execution_path": execution_path
                    or ("model_turn" if route_kind == "model" else "deterministic_action"),
                    "transport_run_id": (
                        str(transport.transport_run_id)
                        if transport.transport_run_id is not None
                        else None
                    ),
                },
                budget_check=budget_check,
            )
            budget_check()
            return StartRunCommand(
                run_id=run_id,
                conversation_id=conversation_id,
                input_message_id=input_message_id if type(input_message_id) is int else None,
                origin_kind=origin_kind,
                initial_context_type=normalized.context_type,
                initial_context_entity_id=(
                    str(normalized.entity_id) if normalized.entity_id is not None else None
                ),
                initial_context_ref_fingerprint=normalized.ref_fingerprint,
                fingerprint_key_id=str(getattr(key, "key_id")),
                initial_transport_mode=transport.mode,
                initial_route_kind=route_kind,
                run_started=run_started,
                segment_started=segment_started,
            )

        try:
            # RunRecorderFactory.start_run accepts exactly one StartRunCommand
            # or StartRunBuilder.  Passing the builder preserves its budget and
            # key-domain validation; no fallback signature is attempted.
            recorder = function(build_start_command)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return _NoopRecorder(), False
        except BaseException:
            raise
        if recorder is None:
            return _NoopRecorder(), False
        if isinstance(recorder, NullRunRecorder):
            return recorder, False
        return recorder, True

    def _resume_journal_replay(
        self,
        conversation_id: int,
        pending: PendingAction,
        transport: RuntimeTransportContext,
    ) -> tuple[object, bool]:
        factory = self._dependencies.journal
        if factory is None:
            return _NoopRecorder(), False
        function = getattr(factory, "resume_waiting_run", None)
        if not callable(function):
            return _NoopRecorder(), False

        def build_segment(
            run_id: str,
            _key: object,
            budget_check: Callable[[], None],
        ) -> StartSegmentCommand:
            segment_id = str(uuid4())
            segment_started = prepare_event(
                event_type="segment.started",
                execution_segment_id=segment_id,
                facts={
                    "request_kind": "pending_replay",
                    "transport_mode": transport.mode,
                    "execution_path": "deterministic_action",
                    "transport_run_id": (
                        str(transport.transport_run_id)
                        if transport.transport_run_id is not None
                        else None
                    ),
                },
                budget_check=budget_check,
            )
            return StartSegmentCommand(run_id=run_id, segment_started=segment_started)

        try:
            recorder = function(conversation_id, pending.tool_call_id, build_segment)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return _NoopRecorder(), False
        if recorder is None or isinstance(recorder, NullRunRecorder):
            return _NoopRecorder() if recorder is None else recorder, False
        return recorder, True

    def _finish_journal_replay(self, recorder: object, control: RuntimeInvocationControl) -> None:
        self._journal_call(
            recorder,
            "append_event",
            EventInput(
                event_type="segment.finished",
                facts={"outcome": "noop", "terminal_run_status": None},
            ),
            control=control,
        )

    def _resume_journal_confirmation(
        self,
        conversation_id: int,
        pending: PendingAction,
        transport: RuntimeTransportContext,
        *,
        execution_path: str = "deterministic_confirmation",
    ) -> tuple[object, bool]:
        factory = self._dependencies.journal
        if factory is None:
            return _NoopRecorder(), False
        function = getattr(factory, "resume_waiting_run", None)
        if not callable(function):
            return _NoopRecorder(), False

        def build_segment(
            run_id: str,
            _key: object,
            budget_check: Callable[[], None],
        ) -> StartSegmentCommand:
            segment_id = str(uuid4())
            segment_started = prepare_event(
                event_type="segment.started",
                execution_segment_id=segment_id,
                facts={
                    "request_kind": "confirmation",
                    "transport_mode": transport.mode,
                    "execution_path": execution_path,
                    "transport_run_id": (
                        str(transport.transport_run_id)
                        if transport.transport_run_id is not None
                        else None
                    ),
                },
                budget_check=budget_check,
            )
            return StartSegmentCommand(run_id=run_id, segment_started=segment_started)

        try:
            recorder = function(conversation_id, pending.tool_call_id, build_segment)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return _NoopRecorder(), False
        if recorder is None or isinstance(recorder, NullRunRecorder):
            return _NoopRecorder() if recorder is None else recorder, False
        return recorder, True

    def _deterministic_confirmation_callbacks(
        self,
        adapter: DeterministicPilotAdapter,
        conversation: object,
        transport: RuntimeTransportContext,
        control: RuntimeInvocationControl,
        *,
        original: PendingAction | None,
        edited: bool,
    ) -> tuple[
        PendingAction | None,
        dict[str, object],
        Callable[[PendingAction, bool], object],
        Callable[[object], object],
        Callable[[PendingAction, str, bool], object],
    ]:
        holder: dict[str, object] = {}
        attempt_id = str(uuid4())

        if original is not None:
            recorder, started = self._resume_journal_confirmation(
                _conversation_id(conversation) or 0,
                original,
                transport,
            )
            holder["recorder"] = recorder
            holder["started"] = started
            if started:
                self._capture_confirmation_journal_context(
                    recorder,
                    started,
                    conversation,
                    _conversation_id(conversation) or 0,
                    self._require_dependency("persistence"),
                    control,
                    tool_names=(original.tool_name,),
                )

        def attempt(effective: PendingAction, approved: bool) -> None:
            holder["attempted"] = True
            holder["approved"] = approved
            holder["effective"] = effective
            if original is None:
                return
            recorder = holder.get("recorder")
            started = holder.get("started") is True
            if recorder is None:
                return
            if not approved:
                self._record_journal_approval(
                    recorder,
                    started,
                    attempt_id,
                    original,
                    effective,
                    False,
                    edited=edited,
                    control=control,
                )
                holder["approval_recorded"] = started
                return
            if not started:
                return
            original_fingerprint = self._journal_call(
                recorder,
                "fingerprint_pending_identity",
                {
                    "tool_call_id": original.tool_call_id,
                    "tool_name": original.tool_name,
                    "args": original.args,
                },
                control=control,
            )
            decided_fingerprint = self._journal_call(
                recorder,
                "fingerprint_pending_identity",
                {
                    "tool_call_id": effective.tool_call_id,
                    "tool_name": effective.tool_name,
                    "args": effective.args,
                },
                control=control,
            )
            if not isinstance(original_fingerprint, str) or not isinstance(
                decided_fingerprint, str
            ):
                holder["bound_recording_failed"] = True
                return
            approval_event = EventInput(
                event_type="approval.decided",
                facts={
                    "confirmation_attempt_id": attempt_id,
                    "decision": "edited" if edited else "approved",
                    "tool_call_id": original.tool_call_id,
                    "original_input_fingerprint": original_fingerprint,
                    "decided_input_fingerprint": decided_fingerprint,
                },
                source_ref_type="tool_call",
                source_ref_id=original.tool_call_id,
            )
            tool_started_event = EventInput(
                event_type="tool.started",
                facts={
                    "tool_call_id": effective.tool_call_id,
                    "tool_name": effective.tool_name,
                    "result_contract": "legacy_string_v1",
                },
                source_ref_type="tool_call",
                source_ref_id=effective.tool_call_id,
            )
            holder["approval_draft"] = self._journal_call(
                recorder,
                "prepare_event_draft",
                approval_event,
                control=control,
            )
            holder["tool_started_draft"] = self._journal_call(
                recorder,
                "prepare_event_draft",
                tool_started_event,
                control=control,
            )

        def bound(session: object) -> None:
            holder["bound_attempted"] = True
            recorder = holder.get("recorder")
            if recorder is None or holder.get("started") is not True:
                return
            approval_draft = holder.get("approval_draft")
            tool_started_draft = holder.get("tool_started_draft")
            if approval_draft is None or tool_started_draft is None:
                holder["bound_recording_failed"] = True
                return
            recorded = self._journal_call(
                recorder,
                "record_approval_and_resume_bound",
                session,
                approval_draft,
                ResumedDisposition(
                    confirmation_attempt_id=attempt_id,
                    tool_call_id=(original.tool_call_id if original is not None else ""),
                ),
                control=control,
            )
            if recorded is not True:
                holder["bound_recording_failed"] = True
                return
            holder["approval_recorded"] = True
            started_recorded = self._journal_call(
                recorder,
                "append_prepared_event_bound",
                session,
                tool_started_draft,
                control=control,
            )
            holder["tool_started_recorded"] = started_recorded is True
            if started_recorded is not True:
                holder["bound_recording_failed"] = True

        def result(effective: PendingAction, value: str, succeeded: bool) -> None:
            recorder = holder.get("recorder")
            started = holder.get("started") is True
            if (
                recorder is not None
                and holder.get("bound_recording_failed") is True
                and holder.get("approval_recorded") is not True
            ):
                approval_draft = holder.get("approval_draft")
                if approval_draft is not None:
                    recovered = self._journal_call(
                        recorder,
                        "recover_approval_and_resume",
                        approval_draft,
                        ResumedDisposition(
                            confirmation_attempt_id=attempt_id,
                            tool_call_id=(original.tool_call_id if original is not None else ""),
                        ),
                        control=control,
                    )
                    holder["approval_recorded"] = recovered is True
            if recorder is not None and holder.get("tool_started_recorded") is True:
                self._record_journal_tool_result(
                    recorder,
                    started,
                    effective,
                    value,
                    succeeded,
                    control,
                )

        return original, holder, attempt, bound, result

    def _finish_deterministic_confirmation_journal(
        self,
        holder: Mapping[str, object],
        original: PendingAction | None,
        conversation: object,
        outcome: RuntimeOutcome,
        control: RuntimeInvocationControl,
    ) -> None:
        recorder = holder.get("recorder")
        if recorder is None or holder.get("started") is not True:
            return
        if holder.get("attempted") is not True or (
            holder.get("approved") is True and holder.get("bound_attempted") is not True
        ):
            self._finish_journal_replay(recorder, control)
            return
        persistence = self._require_dependency("persistence")
        getter = _callable(persistence, ("get_pending_action",))
        current = None
        if getter is not None:
            try:
                current = _pending(
                    _invoke(
                        getter,
                        {"conversation_id": _conversation_id(conversation)},
                        (_conversation_id(conversation),),
                    )
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception:
                # Journal readback is diagnostic/fail-open.  If the pending
                # snapshot cannot be read, close this resumed segment rather
                # than changing the already-committed product outcome.
                current = None
        if (
            isinstance(outcome, RuntimeFailureOutcome)
            and outcome.code is RuntimeFailureCode.STALE_PENDING_ACTION
        ):
            self._abandon(recorder, True)
        elif (
            original is not None
            and current is not None
            and current.tool_call_id != original.tool_call_id
        ):
            self._suspend(recorder, True, current, control, catalog=None, trusted_legacy=True)
        elif isinstance(outcome, RuntimeFailureOutcome):
            self._finish(recorder, True, "failed", "unknown", control)
        else:
            self._finish(recorder, True, "completed", None, control)

    def _capture_confirmation_journal_context(
        self,
        recorder: object,
        started: bool,
        conversation: object,
        conversation_id: int,
        persistence: object,
        control: RuntimeInvocationControl,
        *,
        tool_names: tuple[str, ...],
    ) -> None:
        if not started:
            return
        message_ids = self._snapshot_message_ids(persistence, conversation_id)
        logical_input = {
            "conversation_id": conversation_id,
            "context_type": str(
                _attribute(conversation, "context_type", "workspace") or "workspace"
            ),
            "context_ref": str(_attribute(conversation, "context_ref", "") or ""),
            "message_count": len(message_ids),
            "tool_names": list(tool_names),
        }
        manifest = ContextManifestInput(
            conversation_message_ids=message_ids,
            tool_names=tool_names,
            attachment_refs=(),
            domain_source_refs=(),
        )
        self._journal_call(
            recorder,
            "capture_context",
            logical_input,
            manifest,
            snapshot_kind="confirmation_resume",
            control=control,
        )

    def _record_journal_approval(
        self,
        recorder: object,
        started: bool,
        attempt_id: str,
        original: PendingAction,
        effective: PendingAction,
        approved: bool,
        *,
        edited: bool,
        control: RuntimeInvocationControl,
    ) -> None:
        if not started:
            return
        original_fingerprint = self._journal_call(
            recorder,
            "fingerprint_pending_identity",
            {
                "tool_call_id": original.tool_call_id,
                "tool_name": original.tool_name,
                "args": original.args,
            },
            control=control,
        )
        decided_fingerprint = self._journal_call(
            recorder,
            "fingerprint_pending_identity",
            {
                "tool_call_id": effective.tool_call_id,
                "tool_name": effective.tool_name,
                "args": effective.args,
            },
            control=control,
        )
        if not isinstance(original_fingerprint, str) or not isinstance(decided_fingerprint, str):
            return
        self._journal_call(
            recorder,
            "append_event",
            EventInput(
                event_type="approval.decided",
                facts={
                    "confirmation_attempt_id": attempt_id,
                    "decision": "rejected" if not approved else "edited" if edited else "approved",
                    "tool_call_id": original.tool_call_id,
                    "original_input_fingerprint": original_fingerprint,
                    "decided_input_fingerprint": decided_fingerprint,
                },
                source_ref_type="tool_call",
                source_ref_id=original.tool_call_id,
            ),
            control=control,
        )
        self._journal_call(
            recorder,
            "resume",
            ResumedDisposition(
                confirmation_attempt_id=attempt_id,
                tool_call_id=original.tool_call_id,
            ),
            control=control,
        )

    def _record_journal_tool_start(
        self,
        recorder: object,
        started: bool,
        pending: PendingAction,
        control: RuntimeInvocationControl,
    ) -> None:
        if not started:
            return
        self._journal_call(
            recorder,
            "append_event",
            EventInput(
                event_type="tool.started",
                facts={
                    "tool_call_id": pending.tool_call_id,
                    "tool_name": pending.tool_name,
                    "result_contract": "legacy_string_v1",
                },
                source_ref_type="tool_call",
                source_ref_id=pending.tool_call_id,
            ),
            control=control,
        )

    def _record_journal_tool_result(
        self,
        recorder: object,
        started: bool,
        pending: PendingAction,
        result: str,
        succeeded: bool,
        control: RuntimeInvocationControl,
        *,
        allow_timeout: bool = False,
    ) -> None:
        if not started:
            return
        self._journal_call(
            recorder,
            "append_event",
            EventInput(
                event_type="tool.completed" if succeeded else "tool.failed",
                facts={
                    "tool_call_id": pending.tool_call_id,
                    "tool_name": pending.tool_name,
                    **(
                        {
                            "outcome": "completed",
                            "result_shape_digest": journal_shape_digest(result),
                        }
                        if succeeded
                        else {"failure_category": "tool_error"}
                    ),
                },
                source_ref_type="tool_call",
                source_ref_id=pending.tool_call_id,
            ),
            control=control,
            allow_timeout=allow_timeout,
        )

    def _start_deterministic_turn(
        self,
        adapter: DeterministicPilotAdapter,
        request: StartTurnRequest,
        conversation: object,
        transport: RuntimeTransportContext,
        control: RuntimeInvocationControl,
    ) -> RuntimeOutcome:
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            raise LookupError("conversation not found")
        pending_before = adapter.pending_action(conversation)
        replay = pending_before is not None and adapter.accepts_legacy_route(
            pending_before.tool_name
        )
        if replay:
            assert pending_before is not None
            recorder, started = self._resume_journal_replay(
                conversation_id, pending_before, transport
            )
        else:
            recorder, started = self._start_journal(
                conversation,
                conversation_id,
                None,
                request,
                transport,
                origin_kind="pilot_action",
                route_kind="deterministic",
                request_kind="initial",
                execution_path="deterministic_action",
            )
        self._record_journal_route(
            recorder,
            started,
            route_kind="deterministic",
            route_reason_code="pending_action_replay" if replay else "deterministic_action_match",
            control=control,
        )
        try:
            self._capture_initial_journal_context(
                recorder,
                started,
                conversation,
                conversation_id,
                None,
                None,
                self._require_dependency("persistence"),
                control,
            )
            execution = adapter.start_turn(
                request,
                conversation,
                transport=transport,
                on_user_message_persisted=lambda message_id: self._journal_call(
                    recorder,
                    "attach_input_message",
                    message_id,
                    control=control,
                ),
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            self._abandon(recorder, started)
            raise
        except LookupError:
            self._finish(recorder, started, "failed", "unknown", control)
            return self._failure(
                RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404
            )
        except ValueError as exc:
            self._finish(recorder, started, "failed", "unknown", control)
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, str(exc), 422)
        except Exception:
            self._finish(recorder, started, "failed", "unknown", control)
            return self._failure(
                RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True
            )
        except BaseException:
            self._abandon(recorder, started)
            raise
        if replay or execution.pending_replay:
            try:
                self._finish_journal_replay(recorder, control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                self._abandon(recorder, started)
                raise
            except BaseException:
                self._abandon(recorder, started)
                raise
        elif isinstance(execution.outcome, ConfirmationRequiredOutcome):
            try:
                pending = adapter.pending_action(conversation)
                self._suspend(
                    recorder, started, pending, control, catalog=None, trusted_legacy=True
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                self._abandon(recorder, started)
                raise
            except BaseException:
                self._abandon(recorder, started)
                raise
        elif isinstance(execution.outcome, RuntimeFailureOutcome):
            try:
                self._finish(recorder, started, "failed", "unknown", control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                self._abandon(recorder, started)
                raise
            except BaseException:
                self._abandon(recorder, started)
                raise
        else:
            try:
                self._finish(recorder, started, "completed", None, control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                self._abandon(recorder, started)
                raise
            except BaseException:
                self._abandon(recorder, started)
                raise
        return execution.outcome

    def _prepare_deterministic_journal(
        self,
        adapter: DeterministicPilotAdapter,
        request: StartTurnRequest,
        conversation: object,
        transport: RuntimeTransportContext,
        control: RuntimeInvocationControl,
    ) -> tuple[object, bool, bool]:
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            raise LookupError("conversation not found")
        pending = adapter.pending_action(conversation)
        replay = pending is not None and adapter.accepts_legacy_route(pending.tool_name)
        if replay:
            assert pending is not None
            recorder, started = self._resume_journal_replay(conversation_id, pending, transport)
        else:
            recorder, started = self._start_journal(
                conversation,
                conversation_id,
                None,
                request,
                transport,
                origin_kind="pilot_action",
                route_kind="deterministic",
                request_kind="initial",
                execution_path="deterministic_action",
            )
        self._record_journal_route(
            recorder,
            started,
            route_kind="deterministic",
            route_reason_code="pending_action_replay" if replay else "deterministic_action_match",
            control=control,
        )
        try:
            self._capture_initial_journal_context(
                recorder,
                started,
                conversation,
                conversation_id,
                None,
                None,
                self._require_dependency("persistence"),
                control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        return recorder, started, replay

    @staticmethod
    def _journal_call(
        recorder: object,
        method_name: str,
        *args: object,
        control: RuntimeInvocationControl | None = None,
        allow_timeout: bool = False,
        **kwargs: object,
    ) -> object | None:
        if control is not None:
            if allow_timeout:
                PilotRuntime._allow_timeout_persistence(control)
            else:
                require_runtime_active(control)
        function = getattr(recorder, method_name, None)
        if not callable(function):
            return None
        try:
            return cast(object, function(*args, **kwargs))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            # Journal is explicitly fail-open; a degraded recorder must not
            # change the product outcome.
            return None
        except BaseException:
            # Preserve process/control-flow signals from a recorder.  Ordinary
            # Journal failures are diagnostic-only, but BaseException values
            # must retain their identity and escape the Runtime boundary.
            raise

    def _record_journal_route(
        self,
        recorder: object,
        started: bool,
        *,
        route_kind: str,
        route_reason_code: str,
        control: RuntimeInvocationControl,
    ) -> None:
        if not started:
            return
        self._journal_call(
            recorder,
            "append_event",
            EventInput(
                event_type="route.selected",
                facts={
                    "route_kind": route_kind,
                    "route_reason_code": route_reason_code,
                },
            ),
            control=control,
        )

    @staticmethod
    def _journal_tool_names(
        provider_view: ProviderToolMetadataView | None,
    ) -> tuple[str, ...]:
        if type(provider_view) is not ProviderToolMetadataView:
            return ()
        try:
            return tuple(contract.name for contract in provider_view.ordered_contracts)
        except BaseException:
            return ()

    @staticmethod
    def _snapshot_message_ids(persistence: object, conversation_id: int) -> tuple[int, ...]:
        function = getattr(persistence, "list_messages", None)
        if not callable(function):
            raise _PersistenceReadbackError("persistence list_messages capability is missing")
        try:
            values = function(conversation_id)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            raise _PersistenceReadbackError("persistence message readback failed") from exc
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise _PersistenceReadbackError("persistence message snapshot is invalid")
        try:
            ids: list[int] = []
            for item in values:
                value = _attribute(item, "id")
                if type(value) is not int or value <= 0:
                    raise _PersistenceReadbackError("persistence message snapshot is invalid")
                ids.append(value)
            return tuple(ids)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except _PersistenceReadbackError:
            raise
        except Exception as exc:
            raise _PersistenceReadbackError("persistence message snapshot is invalid") from exc

    def _capture_initial_journal_context(
        self,
        recorder: object,
        started: bool,
        conversation: object,
        conversation_id: int,
        input_message_id: object,
        catalog: object | None,
        persistence: object,
        control: RuntimeInvocationControl,
    ) -> None:
        if not started:
            return
        message_ids = self._snapshot_message_ids(persistence, conversation_id)
        if (
            type(input_message_id) is int
            and input_message_id > 0
            and input_message_id not in message_ids
        ):
            message_ids = (*message_ids, input_message_id)
        logical_input = {
            "conversation_id": conversation_id,
            "context_type": str(
                _attribute(conversation, "context_type", "workspace") or "workspace"
            ),
            "context_ref": str(_attribute(conversation, "context_ref", "") or ""),
            "mode": str(_attribute(conversation, "mode", "general") or "general"),
            "message_count": len(message_ids),
            "tool_names": list(self._journal_tool_names(self._provider_metadata_view())),
        }
        manifest = ContextManifestInput(
            conversation_message_ids=message_ids,
            tool_names=self._journal_tool_names(self._provider_metadata_view()),
            attachment_refs=(),
            domain_source_refs=(),
        )
        self._journal_call(
            recorder,
            "capture_context",
            logical_input,
            manifest,
            snapshot_kind="initial",
            control=control,
        )

    def _record_journal_persisted(
        self,
        recorder: object,
        started: bool,
        persistence: object,
        conversation_id: int,
        message_ids: Sequence[int],
        control: RuntimeInvocationControl,
        *,
        allow_timeout: bool = False,
    ) -> None:
        if not started:
            return
        role_by_id: dict[int, str] = {}
        function = getattr(persistence, "list_messages", None)
        if callable(function):
            try:
                values = function(conversation_id)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    for item in values:
                        item_id = _attribute(item, "id")
                        if type(item_id) is int:
                            role_by_id[item_id] = str(_attribute(item, "role"))
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except BaseException:
                # Journal projection is diagnostic only.  A malformed frozen
                # or Mapping snapshot must never change the product outcome.
                return
        for message_id in message_ids:
            if type(message_id) is not int or message_id <= 0:
                continue
            message_kind = role_by_id.get(message_id)
            if message_kind is not None and message_kind not in {"assistant", "tool"}:
                continue
            self._journal_call(
                recorder,
                "append_event",
                EventInput(
                    event_type="assistant.persisted",
                    facts={
                        "message_id": message_id,
                        "message_kind": message_kind or "assistant",
                    },
                    source_ref_type="message",
                    source_ref_id=message_id,
                ),
                control=control,
                allow_timeout=allow_timeout,
            )

    @staticmethod
    def _settle_deferred_proposals(recorder: object, pending: PendingAction | None) -> None:
        """Order tool proposal facts after the assistant tool-call persists."""

        if pending is not None:
            settle = getattr(recorder, "discard_proposals", None)
        else:
            settle = getattr(recorder, "release_proposals", None)
        if callable(settle):
            settle()

    def _load_source(
        self,
        conversation: object,
        request: StartTurnRequest | _ContinuationActivationRequest,
        *,
        pending_tool_call_id: str | None = None,
    ) -> object:
        loader = self._require_dependency("source_loader")
        function = _callable(loader, ("load", "load_sources", "load_chat_source_messages"))
        if function is None:
            raise TypeError("source loader does not provide load")
        if isinstance(request, StartTurnRequest):
            attachments = request.attachments
            page_context = request.page_context
        else:
            attachments = ()
            page_context = None
        trusted_pending_tool_call_id = (
            str(_attribute(conversation, "pending_tool_call_id", "") or "")
            if pending_tool_call_id is None
            else pending_tool_call_id
        )
        if type(trusted_pending_tool_call_id) is not str:
            raise TypeError("pending tool call identity must be text")
        return _invoke(
            function,
            {
                "conversation": conversation,
                "request": request,
                "attachments": attachments,
                "page_context": page_context,
                "pending_tool_call_id": trusted_pending_tool_call_id,
                "conversation_id": _conversation_id(conversation),
            },
            (conversation, request),
        )

    def _assemble_context(
        self,
        source: object,
        conversation: object,
        request: StartTurnRequest | _ContinuationActivationRequest,
    ) -> object:
        assembler = self._dependencies.context_assembler
        if assembler is None:
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                return tuple(source)
            messages = _attribute(source, "messages", _attribute(source, "history"))
            return messages if messages is not None else source
        function = _callable(assembler, ("assemble", "assemble_context", "build_messages"))
        if function is None:
            raise TypeError("context assembler does not provide assemble")
        if isinstance(request, StartTurnRequest):
            attachments = request.attachments
            page_context = request.page_context
        else:
            attachments = ()
            page_context = None
        values = {
            "source": source,
            "sources": source,
            "conversation": conversation,
            "request": request,
            "page_context": page_context,
            "attachments": attachments,
        }
        return _invoke(function, values, (source, conversation, request))

    def _agent_invocation(
        self,
        resolved: ResolvedModel,
        segment: SegmentExecution,
        assembled: object,
        conversation: object,
        request: StartTurnRequest,
        persistence: object,
        conversation_id: int,
        control: RuntimeInvocationControl,
        recorder: object,
        event_sink: RuntimeEventSink,
        signal_sink: RuntimeSignalSink[str] | None,
        cancel_check: Callable[[], bool],
    ) -> AgentLoopInvocation:
        if isinstance(assembled, Sequence) and not isinstance(assembled, (str, bytes)):
            messages = tuple(_message(item) for item in assembled)
        else:
            raw = _attribute(assembled, "messages", _attribute(assembled, "history"))
            messages = (
                tuple(_message(item) for item in raw)
                if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
                else (_message(assembled),)
            )
        invocation = AgentLoopInvocation(
            seed=NewTurnSeed(messages),
            model=resolved.model,
            catalog=cast(Any, segment.catalog),
            catalog_lease=cast(Any, segment.catalog_lease),
            tool_context=cast(Any, segment.context),
            auto_approve=resolved.auto_approve,
            max_iterations=resolved.max_iter,
            surface_gate=cast(Any, segment.surface_gate),
            # The Driver creates the one proposal gate around this raw
            # recorder and delegates the Segment Context's stable proxy to
            # that gate.  Keeping this raw avoids proxy->gate recursion.
            run_recorder=cast(Any, recorder),
            event_sink=cast(Any, event_sink),
            runtime_signal_sink=signal_sink,
            cancel_check=cancel_check,
        )
        self._bind_invocation_pending_persistence(
            invocation,
            lambda turn, route_handle, presentation: self._persist_result(
                persistence,
                conversation_id,
                request,
                _normalize_agent_result(turn),
                conversation,
                catalog=invocation.catalog,
                route_handle=route_handle,
                presentation=presentation,
                ensure_active=lambda: self._check_cancel(cancel_check, control),
                control=control,
            ),
        )
        return invocation

    def _run_driver(self, driver: AgentDriver, invocation: AgentLoopInvocation) -> AgentTurnResult:
        result = driver.execute(invocation)
        if not isinstance(result, AgentTurnResult):
            raise TypeError("Agent Driver must return AgentTurnResult")
        return result

    @staticmethod
    def _pending_persistence_result(
        invocation: AgentLoopInvocation,
        turn: AgentTurnResult,
    ) -> _PersistedTurn:
        value = invocation._pending_persistence_result(turn)
        if type(value) is not _PersistedTurn:
            raise TypeError("Pending persistence consumer returned an invalid result")
        return value

    def _persist_timeout(
        self,
        persistence: object,
        conversation_id: int,
        *,
        control: RuntimeInvocationControl,
    ) -> object:
        function = _callable(persistence, ("persist_timeout_assistant",))
        if function is not None:
            commit_function = function
            result = self._commit_fence(
                control,
                lambda: _invoke(
                    commit_function,
                    {"conversation_id": conversation_id, "content": CHAT_TIMEOUT_MESSAGE},
                    (conversation_id, CHAT_TIMEOUT_MESSAGE),
                ),
                allow_timeout=True,
            )
            if not _timeout_result_persisted(result):
                return result
            if not self._verify_pending_cleared(
                persistence,
                conversation_id,
                clarification=True,
            ):
                return None
            return result
        function = _callable(
            persistence, ("persist_assistant_message", "persist_initial_assistant_message")
        )
        if function is None:
            return None
        result = self._commit_fence(
            control,
            lambda: _invoke(
                function,
                {"conversation_id": conversation_id, "content": CHAT_TIMEOUT_MESSAGE},
                (conversation_id, CHAT_TIMEOUT_MESSAGE),
            ),
            allow_timeout=True,
        )
        if not _timeout_result_persisted(result):
            return result
        clear = _callable(persistence, ("clear_pending_clarification",))
        if clear is not None:
            clear_result = self._commit_fence(
                control,
                lambda: _invoke(clear, {"conversation_id": conversation_id}, (conversation_id,)),
                allow_timeout=True,
            )
            if not _timeout_result_persisted(clear_result):
                return clear_result
            if not self._verify_pending_cleared(
                persistence,
                conversation_id,
                clarification=True,
            ):
                return None
        return result

    def _persist_result(
        self,
        persistence: object,
        conversation_id: int,
        request: StartTurnRequest,
        result: NormalizedAgentTurn,
        conversation: object,
        *,
        catalog: object | None,
        route_handle: PendingPersistenceRouteHandle | None,
        presentation: PendingPresentationSnapshot | None,
        ensure_active: Callable[[], None],
        control: RuntimeInvocationControl,
    ) -> _PersistedTurn:
        del request
        operation_view = self._operation_metadata_view()
        provider_view = self._provider_metadata_view()
        pending = result.pending
        if pending is not None and (
            type(route_handle) is not TypedPendingRouteHandle
            or type(presentation) is not PendingPresentationSnapshot
        ):
            return _PersistedTurn(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "待确认操作暂时无法保存。",
                    503,
                    retryable=True,
                )
            )
        effective_messages, forced_reply = _with_write_error_followup(
            result.added,
            result.records,
            result.failures,
        )
        reply = forced_reply or _user_facing_assistant_content(result.reply)
        write_status, write_error = _write_outcome(
            result.records,
            _has_write_attempt(
                result.added,
                result.records,
                operation_view,
                provider_view,
            ),
            result.failures,
            operation_view=operation_view,
            provider_view=provider_view,
        )
        messages = [
            Message(
                role=message.role,
                content=(
                    _user_facing_assistant_content(message.content)
                    if message.role == "assistant"
                    else message.content
                ),
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
                provider_blocks=message.provider_blocks,
                surface_contributor=message.surface_contributor,
                surface_signal=message.surface_signal,
                surface_revision=message.surface_revision,
                surface_page_kind=message.surface_page_kind,
                surface_attachment_kinds=message.surface_attachment_kinds,
            )
            for message in effective_messages
        ]
        if pending is not None:
            typed_route_handle = cast(TypedPendingRouteHandle, route_handle)
            typed_presentation = cast(PendingPresentationSnapshot, presentation)
            question = self._missing_question(pending, conversation_id)
            if question:
                clarification_port, clarification_handle = self._clarification_pending_route(
                    pending,
                    conversation_id,
                )
                try:
                    return self._persist_clarification(
                        persistence,
                        conversation_id,
                        messages,
                        pending,
                        question,
                        route_handle=clarification_handle,
                        catalog=catalog,
                        ensure_active=ensure_active,
                        control=control,
                    )
                finally:
                    clarification_port.revoke_pending(clarification_handle)
            function = _callable(persistence, ("persist_initial_pending",))
            if function is None:
                raise TypeError("persistence does not provide atomic pending persistence")
            persist_initial_pending = function
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = self._commit_fence(
                control,
                lambda: persist_initial_pending(
                    conversation_id,
                    messages,
                    pending,
                    route_handle=typed_route_handle,
                ),
            )
            if not _result_persisted(persisted):
                return _PersistedTurn(
                    self._persistence_failure(
                        persisted,
                        "对话已归档，无法保存待确认操作。",
                    )
                )
            message_ids = self._result_message_ids(
                persistence,
                conversation_id,
                persisted,
                before_ids,
            )
            if not self._verify_pending_snapshot(persistence, conversation_id, pending):
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "待确认操作暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            args, token = _safe_pending_payload(pending)
            editable = tuple(
                freeze_json_mapping(dict(value)) for value in typed_presentation.editable_fields
            )
            details = freeze_json_mapping(dict(typed_presentation.details))
            from .contracts import PendingActionPayload

            payload = PendingActionPayload(
                tool_name=pending.tool_name,
                operation_id=pending.operation_id or pending.tool_call_id,
                human=pending.human,
                args=args,
                confirmation_token=token,
                editable_fields=editable,
                details=details,
            )
            return _PersistedTurn(
                ConfirmationRequiredOutcome(
                    confirmation_token=token,
                    conversation_id=conversation_id,
                    operation_id=pending.operation_id or None,
                    pending_action=payload,
                ),
                message_ids,
            )

        if not messages and reply:
            messages = [Message(role="assistant", content=reply)]
        function = _callable(
            persistence, ("persist_initial_messages", "persist_messages", "persist_ai_messages")
        )
        if function is None:
            raise TypeError("persistence does not provide message persistence")
        before_ids = self._snapshot_message_ids(persistence, conversation_id)
        ensure_active()
        persisted = self._commit_fence(
            control,
            lambda: _invoke(
                function,
                {"conversation_id": conversation_id, "messages": messages},
                (conversation_id, messages),
            ),
        )
        if not _result_persisted(persisted):
            return _PersistedTurn(
                self._persistence_failure(persisted, "对话已归档，无法保存回复。")
            )
        message_ids = self._result_message_ids(
            persistence,
            conversation_id,
            persisted,
            before_ids,
        )
        ensure_active()

        clarification = self._existing_clarification(persistence, conversation_id)
        if forced_reply:
            forced_pending = _pending_action_from_added_write_call(
                result.added,
                operation_view,
                provider_view,
            )
            if forced_pending is not None:
                if not _valid_pending_action(
                    forced_pending,
                    operation_view,
                    provider_view,
                ):
                    return _PersistedTurn(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话澄清暂时无法保存。",
                            503,
                            retryable=True,
                        ),
                        message_ids,
                    )
                setter = _callable(persistence, ("set_pending_clarification",))
                if setter is None:
                    return _PersistedTurn(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话澄清暂时无法保存。",
                            503,
                            retryable=True,
                        ),
                        message_ids,
                    )
                set_result = self._set_clarification_with_route(
                    setter,
                    conversation_id=conversation_id,
                    pending=forced_pending,
                    question=forced_reply,
                    ensure_active=ensure_active,
                    control=control,
                )
                if not _result_persisted(set_result):
                    return _PersistedTurn(
                        self._persistence_failure(set_result, "对话澄清暂时无法保存。"),
                        message_ids,
                    )
                if not self._verify_pending_snapshot(
                    persistence,
                    conversation_id,
                    forced_pending,
                    clarification=True,
                    expected_question=forced_reply,
                ):
                    return _PersistedTurn(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话澄清暂时无法保存。",
                            503,
                            retryable=True,
                        ),
                        message_ids,
                    )
        elif clarification is not None and _looks_like_followup_question(reply):
            if not _valid_pending_action(
                clarification[0],
                operation_view,
                provider_view,
                require_operation_id=False,
            ):
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            setter = _callable(persistence, ("set_pending_clarification",))
            if setter is None:
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            set_result = self._set_clarification_with_route(
                setter,
                conversation_id=conversation_id,
                pending=clarification[0],
                question=reply,
                ensure_active=ensure_active,
                control=control,
            )
            if not _result_persisted(set_result):
                return _PersistedTurn(
                    self._persistence_failure(set_result, "对话澄清暂时无法保存。"),
                    message_ids,
                )
            if not self._verify_pending_snapshot(
                persistence,
                conversation_id,
                clarification[0],
                clarification=True,
                expected_question=reply,
            ):
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
        else:
            clear = _callable(persistence, ("clear_pending_clarification",))
            if clarification is not None and clear is None:
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法清理。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            if clear is not None:
                ensure_active()
                clear_result = self._commit_fence(
                    control,
                    lambda: _invoke(
                        clear,
                        {"conversation_id": conversation_id},
                        (conversation_id,),
                    ),
                )
                if not _result_persisted(clear_result):
                    return _PersistedTurn(
                        self._persistence_failure(clear_result, "对话澄清暂时无法清理。"),
                        message_ids,
                    )
                if not self._verify_pending_cleared(
                    persistence,
                    conversation_id,
                    clarification=True,
                ):
                    return _PersistedTurn(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话澄清暂时无法清理。",
                            503,
                            retryable=True,
                        ),
                        message_ids,
                    )
        return _PersistedTurn(
            MessageOutcome(
                message=reply,
                conversation_id=conversation_id,
                write_status=cast(Any, write_status),
                write_error=write_error or None,
            ),
            message_ids,
        )

    def _set_clarification_with_route(
        self,
        setter: Callable[..., object],
        *,
        conversation_id: int,
        pending: PendingAction,
        question: str,
        ensure_active: Callable[[], None],
        control: RuntimeInvocationControl,
    ) -> object:
        route_port, route_handle = self._clarification_pending_route(
            pending,
            conversation_id,
        )
        try:
            ensure_active()
            return self._commit_fence(
                control,
                lambda: _invoke(
                    setter,
                    {
                        "conversation_id": conversation_id,
                        "pending": pending,
                        "question": question,
                        "route_handle": route_handle,
                    },
                    (conversation_id, pending, question),
                ),
            )
        finally:
            route_port.revoke_pending(route_handle)

    @staticmethod
    def _verify_pending_snapshot(
        persistence: object,
        conversation_id: int,
        expected: PendingAction,
        *,
        clarification: bool = False,
        expected_question: str | None = None,
    ) -> bool:
        names = ("get_pending_clarification",) if clarification else ("get_pending_action",)
        getter = _callable(persistence, names)
        if getter is None:
            return False
        try:
            value = _invoke(getter, {"conversation_id": conversation_id}, (conversation_id,))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return False
        except BaseException:
            raise
        question: object | None = None
        if clarification:
            if isinstance(value, tuple) and value:
                if len(value) == 2:
                    value, question = value
                else:
                    value = value[0]
            else:
                question = _attribute(value, "question")
                value = _attribute(value, "pending")
        actual = _pending(value)
        if actual is None:
            return False
        if expected_question is not None and question != expected_question:
            return False
        return (
            actual.tool_call_id == expected.tool_call_id
            and actual.tool_name == expected.tool_name
            and actual.args == expected.args
            and actual.human == expected.human
            and (
                (not clarification and actual.operation_id == expected.operation_id)
                or (clarification and actual.operation_id in {"", expected.operation_id})
            )
        )

    @staticmethod
    def _verify_pending_cleared(
        persistence: object,
        conversation_id: int,
        *,
        clarification: bool,
    ) -> bool:
        names = ("get_pending_clarification",) if clarification else ("get_pending_action",)
        getter = _callable(persistence, names)
        if getter is None:
            return False
        try:
            value = _invoke(getter, {"conversation_id": conversation_id}, (conversation_id,))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            raise _PersistenceReadbackError("persistence clear readback failed") from exc
        return value is None

    @staticmethod
    def _result_message_ids(
        persistence: object,
        conversation_id: int,
        result: object,
        before_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        raw_ids = _attribute(result, "message_ids", ())
        if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes)):
            ids = tuple(value for value in raw_ids if type(value) is int and value > 0)
            if ids:
                return ids
        message_id = _attribute(result, "message_id")
        if type(message_id) is int and message_id > 0:
            return (message_id,)
        after_ids = PilotRuntime._snapshot_message_ids(persistence, conversation_id)
        return tuple(value for value in after_ids if value not in before_ids)

    @staticmethod
    def _existing_clarification(
        persistence: object,
        conversation_id: int,
    ) -> tuple[PendingAction, str] | None:
        getter = _callable(persistence, ("get_pending_clarification",))
        if getter is None:
            raise _PersistenceReadbackError(
                "persistence clarification readback capability is missing"
            )
        try:
            value = _invoke(getter, {"conversation_id": conversation_id}, (conversation_id,))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            raise _PersistenceReadbackError("persistence clarification readback failed") from exc
        if isinstance(value, tuple) and len(value) == 2:
            pending_value, question = value
        else:
            pending_value = _attribute(value, "pending")
            question = _attribute(value, "question")
        pending = _pending(pending_value)
        if value is not None and (pending is None or not isinstance(question, str)):
            raise _PersistenceReadbackError("persistence clarification snapshot is invalid")
        return (pending, question) if pending is not None and isinstance(question, str) else None

    def _persistence_failure(self, result: object, archived_message: str) -> RuntimeFailureOutcome:
        status = _failure_status(result)
        if status == "closed":
            return self._failure(RuntimeFailureCode.CONVERSATION_ARCHIVED, archived_message, 409)
        if status == "not_found":
            return self._failure(
                RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404
            )
        return self._failure(
            RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True
        )

    def _missing_question(self, pending: PendingAction, conversation_id: int) -> str | None:
        function = _callable(
            self._dependencies.missing_target_question,
            ("missing_target_question", "question", "resolve"),
        )
        if function is None:
            value = _attribute(pending, "missing_question")
            return value if isinstance(value, str) and value else None
        value = _invoke(
            function,
            {"pending": pending, "conversation_id": conversation_id},
            (pending, conversation_id),
        )
        return value if isinstance(value, str) and value else None

    def _persist_clarification(
        self,
        persistence: object,
        conversation_id: int,
        messages: Sequence[Message],
        pending: PendingAction,
        question: str,
        *,
        route_handle: ClarificationPendingRouteHandle,
        catalog: object | None,
        ensure_active: Callable[[], None],
        control: RuntimeInvocationControl,
    ) -> _PersistedTurn:
        if not _valid_pending_action(
            pending,
            self._operation_metadata_view(),
            self._provider_metadata_view(),
        ):
            return _PersistedTurn(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话澄清暂时无法保存。",
                    503,
                    retryable=True,
                )
            )
        atomic = _callable(persistence, ("persist_clarification", "persist_pending_clarification"))
        if atomic is not None:
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = self._commit_fence(
                control,
                lambda: _invoke(
                    atomic,
                    {
                        "conversation_id": conversation_id,
                        "messages": messages,
                        "pending": pending,
                        "question": question,
                        "route_handle": route_handle,
                    },
                    (conversation_id, messages, pending, question),
                ),
            )
            if not _result_persisted(persisted):
                return _PersistedTurn(
                    self._persistence_failure(persisted, "对话澄清暂时无法保存。")
                )
            message_ids = self._result_message_ids(
                persistence,
                conversation_id,
                persisted,
                before_ids,
            )
            if not self._verify_pending_snapshot(
                persistence,
                conversation_id,
                pending,
                clarification=True,
                expected_question=question,
            ):
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
        else:
            initial = _callable(persistence, ("persist_initial_messages", "persist_messages"))
            if initial is None:
                raise TypeError("persistence does not provide clarification persistence")
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = self._commit_fence(
                control,
                lambda: _invoke(
                    initial,
                    {"conversation_id": conversation_id, "messages": messages},
                    (conversation_id, messages),
                ),
            )
            message_ids = self._result_message_ids(
                persistence, conversation_id, persisted, before_ids
            )
            if not _result_persisted(persisted):
                return _PersistedTurn(
                    self._persistence_failure(persisted, "对话已归档，无法保存回复。"), message_ids
                )
            clear = _callable(persistence, ("clear_pending_action",))
            if clear is not None:
                ensure_active()
                clear_result = self._commit_fence(
                    control,
                    lambda: _invoke(
                        clear, {"conversation_id": conversation_id}, (conversation_id,)
                    ),
                )
                if not _result_persisted(clear_result):
                    return _PersistedTurn(
                        self._persistence_failure(clear_result, "对话已归档，无法保存回复。"),
                        message_ids,
                    )
                if not self._verify_pending_cleared(
                    persistence,
                    conversation_id,
                    clarification=False,
                ):
                    return _PersistedTurn(
                        self._failure(
                            RuntimeFailureCode.OPERATION_FAILED,
                            "对话结果暂时无法保存。",
                            503,
                            retryable=True,
                        ),
                        message_ids,
                    )
            setter = _callable(persistence, ("set_pending_clarification",))
            if setter is None:
                raise TypeError("persistence does not provide clarification persistence")
            try:
                ensure_active()
                set_result = self._commit_fence(
                    control,
                    lambda: _invoke(
                        setter,
                        {
                            "conversation_id": conversation_id,
                            "pending": pending,
                            "question": question,
                            "route_handle": route_handle,
                        },
                        (conversation_id, pending, question),
                    ),
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception:
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            if set_result is None:
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            if not _result_persisted(set_result):
                return _PersistedTurn(
                    self._persistence_failure(set_result, "对话澄清暂时无法保存。"),
                    message_ids,
                )
            if not self._verify_pending_snapshot(
                persistence,
                conversation_id,
                pending,
                clarification=True,
                expected_question=question,
            ):
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话澄清暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            assistant = _callable(
                persistence, ("persist_assistant_message", "persist_initial_assistant_message")
            )
            if assistant is None:
                return _PersistedTurn(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    message_ids,
                )
            ensure_active()
            persisted = self._commit_fence(
                control,
                lambda: _invoke(
                    assistant,
                    {"conversation_id": conversation_id, "content": question},
                    (conversation_id, question),
                ),
            )
            assistant_ids = self._result_message_ids(
                persistence, conversation_id, persisted, before_ids
            )
            message_ids = tuple(dict.fromkeys((*message_ids, *assistant_ids)))
        if not _result_persisted(persisted):
            return _PersistedTurn(
                self._persistence_failure(persisted, "对话已归档，无法保存回复。"), message_ids
            )
        return _PersistedTurn(
            MessageOutcome(message=question, conversation_id=conversation_id), message_ids
        )

    # ---- cleanup and control -------------------------------------------------

    @staticmethod
    def _check_cancel(cancel_check: Callable[[], bool], control: RuntimeInvocationControl) -> None:
        try:
            requested = cancel_check()
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            raise RuntimeTransportAborted() from exc
        except BaseException:
            raise
        if requested:
            require_runtime_active(control)
            raise RuntimeCancelled()
        require_runtime_active(control)

    @staticmethod
    def _confirmation_cancel_check(
        control: RuntimeInvocationControl,
        external_check: Callable[[], bool],
    ) -> Callable[[], bool]:
        """Preserve the route cancel seam while fencing late confirmation work."""

        def check() -> bool:
            if external_check():
                require_runtime_active(control)
                raise RuntimeCancelled()
            require_runtime_active(control)
            return False

        return check

    @staticmethod
    def _allow_timeout_persistence(control: RuntimeInvocationControl) -> None:
        state = getattr(control, "state", None)
        state_value = str(getattr(state, "value", state or ""))
        if state_value == "timed_out":
            return
        if state_value in {"active", "completed"}:
            return
        require_runtime_active(control)

    @staticmethod
    def _failure(
        code: RuntimeFailureCode,
        message: str,
        status_code: int,
        *,
        retryable: bool = False,
        degraded: bool = False,
        conversation_id: int | None = None,
    ) -> RuntimeFailureOutcome:
        return RuntimeFailureOutcome(
            code=code,
            message=message,
            status_code=status_code,
            retryable=retryable,
            degraded=degraded,
            conversation_id=conversation_id,
        )

    def _finish(
        self,
        recorder: object,
        started: bool,
        status: str,
        failure_code: str | None,
        control: RuntimeInvocationControl,
        *,
        allow_timeout: bool = False,
    ) -> None:
        if not started:
            return
        if allow_timeout:
            self._allow_timeout_persistence(control)
        else:
            require_runtime_active(control)
        self._phase("run_finish")
        if allow_timeout:
            self._allow_timeout_persistence(control)
        else:
            require_runtime_active(control)
        function = getattr(recorder, "finish", None)
        if not callable(function):
            return
        try:
            function(TerminalDisposition(status=cast(Any, status), failure_code=failure_code))
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return
        except BaseException:
            return

    def _suspend(
        self,
        recorder: object,
        started: bool,
        pending: PendingAction | None,
        control: RuntimeInvocationControl,
        *,
        catalog: object | None,
        trusted_legacy: bool = False,
    ) -> None:
        if not started or pending is None:
            return
        if not _valid_pending_action(
            pending,
            self._operation_metadata_view(),
            self._provider_metadata_view(),
            operation_port=self._operation_metadata_port(),
            trusted_legacy=trusted_legacy,
        ):
            return
        require_runtime_active(control)
        self._phase("run_suspend")
        require_runtime_active(control)
        function = getattr(recorder, "suspend", None)
        if not callable(function):
            return
        try:
            identity = {
                "tool_call_id": pending.tool_call_id,
                "tool_name": pending.tool_name,
                "args": pending.args,
            }
            fingerprint = None
            if callable(getattr(recorder, "fingerprint_pending_identity", None)):
                value = self._journal_call(
                    recorder,
                    "fingerprint_pending_identity",
                    identity,
                    control=control,
                )
                fingerprint = value if isinstance(value, str) else None
            require_runtime_active(control)
            function(
                SuspendedDisposition(
                    tool_call_id=pending.tool_call_id,
                    tool_name=pending.tool_name,
                    tool_kind="write",
                    args_shape_digest=journal_shape_digest(pending.args),
                    pending_identity_fingerprint=fingerprint,
                    pending_identity=identity,
                )
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return
        except BaseException:
            return

    def _abandon(self, recorder: object, started: bool) -> None:
        if not started:
            return
        function = getattr(recorder, "abandon", None)
        if function is None:
            return
        try:
            function()
        except BaseException:
            return


def _dependency_object_values(dependencies: object) -> dict[str, object]:
    raw = getattr(dependencies, "__dict__", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    return {
        field: getattr(dependencies, field)
        for field in RuntimeDependencies.__dataclass_fields__
        if hasattr(dependencies, field)
    }


def _dependency_values(values: Mapping[str, object]) -> dict[str, object]:
    aliases = {
        "conversation_gateway": "conversations",
        "conversation_repository": "conversations",
        "conversation_store": "conversations",
        "conversation": "conversations",
        "route": "route_selector",
        "source": "source_loader",
        "assembler": "context_assembler",
        "context_builder": "context_assembler",
        "agent": "agent_driver",
        "driver": "agent_driver",
        "journal_factory": "journal",
        "run_recorder_factory": "journal",
        "pending_question": "missing_target_question",
        "persistence_coordinator": "persistence",
        "pending_checker": "pending_guard",
        "request_validator": "validator",
        "phase_recorder": "phase_sink",
        "confirmation": "confirmation_coordinator",
        "confirmation_runtime": "confirmation_coordinator",
        "confirmation_continuation": "continuation",
        "continuation_coordinator": "continuation",
    }
    result: dict[str, object] = {}
    for key, value in values.items():
        result[aliases.get(key, key)] = value
    valid = set(RuntimeDependencies.__dataclass_fields__)
    unknown = sorted(key for key in result if key not in valid)
    if unknown:
        raise TypeError("unknown runtime dependency: " + ", ".join(unknown))
    return result


__all__ = [
    "AgentDriver",
    "ContextAssembler",
    "ConversationGateway",
    "JournalFactory",
    "NormalizedAgentTurn",
    "PilotRuntime",
    "PilotRuntimeDependencies",
    "PilotRuntimeDeps",
    "ResolvedModel",
    "RouteKind",
    "RouteSelector",
    "RuntimeDependencies",
    "RuntimePersistence",
    "SourceLoader",
    "StartTurnDependencies",
]
