"""Transport-independent synchronous Pilot Runtime orchestration.

The service owns the causal order around the existing Agent driver, including
the response-header preparation boundary.  The transport owns the
``AgentExecutionHost`` (and therefore the worker and deadline).  All external
objects are injected through small structural seams so this module does not
need to know about FastAPI, ORM rows, or LangGraph state.
"""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any, Protocol, TypeAlias, cast
from threading import Lock
from uuid import uuid4

from offerpilot.ai.agent import PendingAction
from offerpilot.ai.tool_runtime.contracts import ToolFailure, ToolSuccess
from offerpilot.ai.tool_runtime.legacy import LEGACY_DETERMINISTIC_NAMES
from offerpilot.ai.tool_runtime.journal import journal_shape_digest
from offerpilot.ai.types import Message, ToolCall
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
from .deterministic import DeterministicExecution, DeterministicPilotAdapter
from .event_sink import emit_runtime_event, require_runtime_active
from .persistence import (
    PendingActionView,
    PendingClarificationView,
    PersistedMessageView,
    PersistenceResult,
    PersistenceStatus,
)


CHAT_TIMEOUT_MESSAGE = "这次处理时间过长，已停止。你可以重试或换一种问法。"
DEFAULT_MAX_ITERATIONS = 20


class RouteKind(str, Enum):
    MODEL = "model"
    DETERMINISTIC = "deterministic"


class ConversationGateway(Protocol):
    def create(self, request: StartTurnRequest) -> object: ...

    def load(self, conversation_id: int) -> object | None: ...


class RouteSelector(Protocol):
    def select(self, request: StartTurnRequest, conversation: object) -> object: ...


class ModelResolver(Protocol):
    def resolve(self, request: StartTurnRequest, conversation: object) -> object: ...


class SourceLoader(Protocol):
    def load(self, conversation: object, request: StartTurnRequest) -> object: ...


class ContextAssembler(Protocol):
    def assemble(
        self,
        source: object,
        conversation: object,
        request: StartTurnRequest,
    ) -> object: ...


class AgentDriver(Protocol):
    def run_turn(self, *args: object, **kwargs: object) -> object: ...


class ToolCatalog(Protocol):
    def resolve(self, name: str) -> object | None: ...

    def write_names(self) -> Iterable[str]: ...

    def provider_contracts(self) -> Sequence[object]: ...


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
    ) -> PersistenceResult: ...

    def persist_clarification(
        self,
        conversation_id: int,
        messages: Sequence[Message],
        pending: PendingAction,
        question: str,
    ) -> PersistenceResult: ...

    def set_pending_clarification(
        self,
        conversation_id: int,
        pending: PendingAction,
        question: str,
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

    ``catalog`` and ``tool_context`` intentionally remain opaque.  The Agent
    adapter passes them through without inspecting Graph state or provider
    internals.
    """

    model: object
    catalog: object | None = None
    config: object | None = None
    tool_context: object | None = None
    auto_approve: bool = False
    max_iter: int = DEFAULT_MAX_ITERATIONS
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    """The only context the Runtime exposes to an injected Agent driver."""

    model: object
    catalog: object | None
    messages: tuple[object, ...]
    config: object | None
    conversation: object
    request: StartTurnRequest
    tool_context: object | None
    auto_approve: bool
    max_iter: int
    thread_id: str
    run_recorder: object
    event_sink: RuntimeEventSink
    signal_sink: RuntimeSignalSink[str] | None
    cancel_check: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class NormalizedAgentTurn:
    """Sealed adapter projection of the existing ``AgentTurnResult`` shape."""

    added: tuple[object, ...]
    reply: str
    pending: PendingAction | None
    records: tuple[object, ...] = ()
    failures: tuple[object, ...] = ()


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


class _PreparedModelLease:
    """Independent once-only release gate for a prepared provider token."""

    __slots__ = ("_lock", "_released", "_release")

    def __init__(self, release: Callable[[], object]) -> None:
        self._lock = Lock()
        self._released = False
        self._release = release

    def release_once(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
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
    model_resolver: ModelResolver | None = None
    source_loader: SourceLoader | None = None
    context_assembler: ContextAssembler | None = None
    agent_driver: AgentDriver | None = None
    route_selector: RouteSelector | None = None
    journal: JournalFactory | None = None
    catalog: ToolCatalog | None = None
    missing_target_question: Callable[..., str | None] | None = None
    conversation_store: ConversationGateway | None = None
    conversation_gateway: ConversationGateway | None = None
    pending_guard: Callable[..., object] | RuntimePersistence | None = None
    validator: Callable[..., object] | None = None
    phase_sink: Callable[[str], None] | None = None
    application_visible: Callable[[int], bool] | None = None
    deterministic: DeterministicPilotAdapter | None = None


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


class _NoopEventSink:
    def emit(self, _event: RuntimeEvent) -> None:
        return None

    def __call__(self, _event: object) -> None:
        return None


class _SafeEventSink:
    __slots__ = ("_sink",)

    def __init__(self, sink: RuntimeEventSink) -> None:
        self._sink = sink

    def emit(self, event: RuntimeEvent) -> None:
        emit_runtime_event(self._sink, event)

    def __call__(self, event: object) -> None:
        if isinstance(event, Mapping):
            typed = _legacy_runtime_event(event)
            if typed is not None:
                self.emit(typed)
            return None
        self.emit(cast(RuntimeEvent, event))


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


def _legacy_runtime_event(value: Mapping[object, object]) -> RuntimeEvent | None:
    """Project the existing Agent callback dictionaries into typed events."""

    name = value.get("event")
    data = value.get("data")
    if not isinstance(name, str) or not isinstance(data, Mapping):
        return None
    if name == "assistant_delta":
        return AssistantDeltaEvent(delta=str(data.get("delta") or ""))
    if name == "assistant_message":
        return AssistantMessageEvent(message=str(data.get("message") or ""))
    if name == "status":
        return StatusEvent(
            phase=str(data.get("phase") or "model_running"),
            label=str(data.get("label") or ""),
        )
    if name == "tool_call":
        try:
            args_summary = freeze_json_mapping(
                cast(Mapping[str, object], data.get("args_summary") or {})
            )
        except (TypeError, ValueError):
            args_summary = freeze_json_mapping({})
        kind = str(data.get("kind") or "read")
        if kind not in {"read", "write"}:
            kind = "read"
        confirm_mode = str(data.get("confirm_mode") or "none")
        if confirm_mode not in {"none", "hitl", "approved", "rejected"}:
            confirm_mode = "none"
        return ToolCallEvent(
            tool_call_id=str(data.get("tool_call_id") or "unknown"),
            tool_name=str(data.get("tool_name") or "unknown"),
            public_label=str(data.get("public_label") or ""),
            kind=cast(Any, kind),
            confirm_mode=cast(Any, confirm_mode),
            summary=str(data.get("summary") or ""),
            args_summary=args_summary,
        )
    if name == "tool_result":
        status = str(data.get("status") or "error")
        if status not in {"success", "error", "cancelled"}:
            status = "error"
        write_status = data.get("write_status")
        if write_status not in {None, "none", "success", "failed", "cancelled"}:
            write_status = None
        return ToolResultEvent(
            tool_call_id=str(data.get("tool_call_id") or "unknown"),
            tool_name=str(data.get("tool_name") or "unknown"),
            status=cast(Any, status),
            summary=str(data.get("summary") or ""),
            evidence=_legacy_payload_tuple(data.get("evidence")),
            affected_resources=_legacy_payload_tuple(data.get("affected_resources")),
            changed_entities=_legacy_payload_tuple(data.get("changed_entities")),
            message=str(data.get("message") or ""),
            visible_result=str(data.get("visible_result") or ""),
            operation_id=(
                str(data["operation_id"])
                if data.get("operation_id") not in (None, "")
                else None
            ),
            write_status=cast(Any, write_status),
        )
    if name == "confirmation_required":
        token = str(data.get("confirmation_token") or data.get("token") or "")
        if not token:
            return None
        return ConfirmationRequiredEvent(
            confirmation_token=token,
            operation_id=(
                str(data["operation_id"])
                if data.get("operation_id") not in (None, "")
                else None
            ),
        )
    if name == "error":
        try:
            code = RuntimeFailureCode(str(data.get("code") or "operation_failed"))
        except ValueError:
            code = RuntimeFailureCode.OPERATION_FAILED
        return ErrorEvent(
            code=code,
            message=str(data.get("message") or ""),
            retryable=data.get("retryable") is True,
            degraded=data.get("degraded") is True,
        )
    return None


def _legacy_payload_tuple(value: object) -> tuple[ImmutablePayload, ...]:
    """Snapshot legacy tool-result payload arrays into immutable mappings."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    items: list[ImmutablePayload] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            items.append(freeze_json_mapping(cast(Mapping[str, object], item)))
        except (TypeError, ValueError):
            continue
    return tuple(items)


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
        if has_var_keyword and var_keyword_values:
            for name, value in var_keyword_values.items():
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
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            } and parameter.name in values:
                kwargs[parameter.name] = values[parameter.name]
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            for name, value in (var_keyword_values or {}).items():
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
        tool_calls = [_tool_call(item) for item in raw_calls] if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes)) else []
        return Message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=str(value.get("tool_call_id") or ""),
            provider_blocks=dict(value.get("provider_blocks") or {}) if isinstance(value.get("provider_blocks"), Mapping) else {},
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


def _record_is_write(record: object) -> bool:
    prepared = _attribute(record, "prepared")
    spec = _attribute(prepared, "spec")
    kind = _attribute(spec, "kind")
    return str(getattr(kind, "value", kind or "")) == "write"


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
) -> tuple[str, str]:
    if not attempted:
        return "none", ""
    write_records = tuple(record for record in records if _record_is_write(record))
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


def _catalog_write_names(catalog: object | None) -> set[str]:
    function = _callable(catalog, ("write_names",))
    if function is None:
        return set()
    try:
        values = function()
    except Exception:
        return set()
    if isinstance(values, (str, bytes)):
        return set()
    try:
        return {str(value) for value in cast(Iterable[object], values)}
    except TypeError:
        return set()


def _catalog_exposes_write(catalog: object | None, tool_name: str) -> bool:
    if catalog is None or not tool_name:
        return False
    resolver = _callable(catalog, ("resolve",))
    contracts = _callable(catalog, ("provider_contracts",))
    if resolver is None or contracts is None:
        return False
    try:
        spec = _invoke(resolver, {"name": tool_name}, (tool_name,))
        exposed = contracts()
    except Exception:
        return False
    if spec is None:
        return False
    kind = _attribute(spec, "kind")
    if str(getattr(kind, "value", kind or "")) != "write":
        return False
    if not isinstance(exposed, Sequence) or isinstance(exposed, (str, bytes)):
        return False
    return any(_attribute(contract, "name") == tool_name for contract in exposed)


def _valid_pending_action(
    pending: PendingAction,
    catalog: object | None,
    *,
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
        if pending.tool_name not in LEGACY_DETERMINISTIC_NAMES:
            return False
    elif not _catalog_exposes_write(catalog, pending.tool_name):
        return False
    return bool(_confirmation_token(pending))


def _has_write_attempt(added: Sequence[object], records: Sequence[object], catalog: object | None) -> bool:
    write_names = _catalog_write_names(catalog)
    for item in added:
        message = _message(item)
        if message.role == "assistant" and any(
            call.name in write_names for call in message.tool_calls
        ):
            return True
    return any(_record_is_write(record) for record in records)


def _pending_action_from_added_write_call(
    added: Sequence[object],
    catalog: object | None,
) -> PendingAction | None:
    write_names = _catalog_write_names(catalog)
    for item in reversed(added):
        message = _message(item)
        if message.role != "assistant" or not message.tool_calls:
            continue
        call = message.tool_calls[0]
        if write_names and call.name not in write_names:
            continue
        return PendingAction(call.id, call.name, call.args, call.name, call.id)
    return None


def _looks_like_followup_question(reply: str) -> bool:
    trimmed = reply.strip()
    return bool(trimmed) and (
        "?" in trimmed or "？" in trimmed or "请告诉我" in trimmed or "请补充" in trimmed
    )


def _normalize_agent_result(value: object) -> NormalizedAgentTurn:
    if isinstance(value, NormalizedAgentTurn):
        return value
    if isinstance(value, Mapping):
        # Only the sealed AgentTurn-like fields are accepted.  In particular,
        # a LangGraph state mapping (``messages``, ``__interrupt__`` etc.) is
        # never interpreted here.
        allowed = {"added", "reply", "pending", "records", "failures"}
        if set(value) - allowed or "added" not in value or "reply" not in value:
            raise TypeError("Agent result is not a sealed turn result")
        added_value = value["added"]
        if not isinstance(added_value, Sequence) or isinstance(added_value, (str, bytes)):
            raise TypeError("Agent result added must be a sequence")
        return NormalizedAgentTurn(
            tuple(added_value),
            str(value["reply"] or ""),
            _pending(value.get("pending")),
            tuple(cast(Sequence[object], value.get("records") or ())),
            tuple(cast(Sequence[object], value.get("failures") or ())),
        )
    added_value = _attribute(value, "added")
    reply_value = _attribute(value, "reply")
    if not isinstance(added_value, Sequence) or isinstance(added_value, (str, bytes)) or not isinstance(reply_value, str):
        raise TypeError("Agent result is not a sealed turn result")
    return NormalizedAgentTurn(
        tuple(added_value),
        reply_value,
        _pending(_attribute(value, "pending")),
        tuple(cast(Sequence[object], _attribute(value, "records", ()) or ())),
        tuple(cast(Sequence[object], _attribute(value, "failures", ()) or ())),
    )


def _resolved_model(value: object) -> ResolvedModel | None:
    if isinstance(value, ResolvedModel):
        if value.model is None or value.model is False:
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


def _resolved_model_parts(model: object, config: object | None, *, source: object | None = None) -> ResolvedModel:
    origin = source if source is not None else config
    catalog = _attribute(origin, "catalog", _attribute(origin, "tool_catalog"))
    tool_context = _attribute(origin, "tool_context")
    auto_approve = _attribute(config, "chat_auto_approve_writes", _attribute(config, "auto_approve", False))
    max_iter = _attribute(config, "max_iter", _attribute(config, "max_iterations", DEFAULT_MAX_ITERATIONS))
    thread_id = _attribute(config, "thread_id")
    return ResolvedModel(
        model=model,
        catalog=catalog,
        config=config,
        tool_context=tool_context,
        auto_approve=auto_approve is True,
        max_iter=max_iter if type(max_iter) is int and max_iter > 0 else DEFAULT_MAX_ITERATIONS,
        thread_id=thread_id if isinstance(thread_id, str) else None,
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


def _write_status(result: NormalizedAgentTurn) -> WriteStatus:
    """Project only the public write status from opaque tool records."""

    attempted = False
    failed = False
    for record in result.records:
        prepared = _attribute(record, "prepared")
        spec = _attribute(prepared, "spec")
        kind = _attribute(spec, "kind")
        if str(getattr(kind, "value", kind or "")) != "write":
            continue
        attempted = True
        outcome = _attribute(record, "outcome")
        if outcome is not None and _attribute(outcome, "code") is not None:
            failed = True
    if not attempted:
        return "none"
    return "failed" if failed or result.failures else "success"


def _is_agent_cancelled(exc: BaseException) -> bool:
    return type(exc).__name__ == "ChatRunCancelled"


class PilotRuntime:
    """The synchronous model-only Start Turn state machine."""

    __slots__ = ("_dependencies", "_owner_token", "_prepared_models")

    def __init__(
        self,
        dependencies: RuntimeDependenciesLike | None = None,
        **kwargs: object,
    ) -> None:
        self._owner_token = object()
        self._prepared_models: dict[object, ResolvedModel] = {}
        if dependencies is None:
            values = dict(kwargs)
            self._dependencies = RuntimeDependencies(**cast(Any, _dependency_values(values)))
            return
        if kwargs:
            if isinstance(dependencies, RuntimeDependencies):
                values = {field: getattr(dependencies, field) for field in RuntimeDependencies.__dataclass_fields__}
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
                values = {
                    **_dependency_object_values(dependencies)
                }
            self._dependencies = RuntimeDependencies(**cast(Any, _dependency_values(values)))

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
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)
        if execution_host is None or invocation_control is None:
            raise TypeError("execution_host and invocation_control are required")
        cancel = cancel_check or (lambda: False)

        def complete_early(outcome: RuntimeOutcome) -> RuntimeOutcome:
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
                        self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404)
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
                self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
            )
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            return complete_early(
                self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
            )
        if _is_archived(conversation):
            return complete_early(
                self._failure(RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409)
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
        if pending_guard is not None and pending_guard is not False:
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
                    self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)
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
                    deterministic_outcome = self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, text, 422)
                else:
                    deterministic_outcome = self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, text, 422)
                return complete_early(deterministic_outcome)
            except LookupError:
                return complete_early(
                    self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404)
                )
            except Exception:
                return complete_early(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True)
                )
            raise AssertionError("unreachable deterministic dispatch")

        self._phase("model_resolve")
        resolved = self._resolve_model(request, conversation)
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

        persistence = self._require_dependency("persistence")
        persistence_failure = self._validate_persistence_surface(persistence)
        if persistence_failure is not None:
            return complete_early(persistence_failure)
        self._phase("user_persist")
        self._check_cancel(cancel, invocation_control)
        try:
            user_result = self._persist_user(
                persistence,
                conversation_id,
                request.message,
                control=invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
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
            raise
        if not _result_persisted(user_result):
            code = (
                RuntimeFailureCode.CONVERSATION_ARCHIVED
                if _failure_status(user_result) == "closed"
                else RuntimeFailureCode.APPLICATION_NOT_FOUND
                if _failure_status(user_result) == "not_found"
                else RuntimeFailureCode.OPERATION_FAILED
            )
            status = 409 if code is RuntimeFailureCode.CONVERSATION_ARCHIVED else 404 if code is RuntimeFailureCode.APPLICATION_NOT_FOUND else 503
            return complete_early(
                self._failure(
                    code,
                    "对话当前不可写入。",
                    status,
                    retryable=code is RuntimeFailureCode.OPERATION_FAILED,
                )
            )

        input_message_id = _attribute(user_result, "message_id")
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
            input_message_id = persisted_ids[-1] if persisted_ids else None

        self._phase("run_start")
        self._check_cancel(cancel, invocation_control)
        recorder, journal_started = self._start_journal(
            conversation,
            conversation_id,
            input_message_id,
            request,
            resolved_transport,
        )

        abandoned = False
        completion_marked = False

        def abandon_once() -> None:
            nonlocal abandoned
            if abandoned:
                return
            abandoned = True
            self._abandon(recorder, journal_started)

        def complete_once() -> None:
            nonlocal completion_marked
            if completion_marked:
                return
            self._mark_completed(invocation_control)
            completion_marked = True

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

        try:
            self._phase("source_load")
            source = self._load_source(conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception:
            finish_or_raise("failed", RuntimeFailureCode.SOURCE_LOAD_FAILED.value)
            return self._failure(
                RuntimeFailureCode.SOURCE_LOAD_FAILED,
                "上下文暂时无法加载，请稍后重试。",
                503,
                retryable=True,
            )
        except BaseException:
            abandon_once()
            raise

        try:
            self._phase("context_assemble")
            assembled = self._assemble_context(source, conversation, request)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception:
            finish_or_raise("failed", RuntimeFailureCode.SOURCE_LOAD_FAILED.value)
            return self._failure(
                RuntimeFailureCode.SOURCE_LOAD_FAILED,
                "上下文暂时无法加载，请稍后重试。",
                503,
                retryable=True,
            )
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
                resolved.catalog,
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

        driver = self._require_dependency("agent_driver")
        safe_event_sink: RuntimeEventSink = _SafeEventSink(event_sink) if event_sink is not None else _NoopEventSink()
        safe_signal_sink: RuntimeSignalSink[str] | None = _SafeSignalSink(signal_sink) if signal_sink is not None else None
        def checked_cancel() -> bool:
            self._check_cancel(cancel, invocation_control)
            return False

        invocation = self._agent_invocation(
            resolved,
            assembled,
            conversation,
            request,
            recorder,
            safe_event_sink,
            safe_signal_sink,
            checked_cancel,
        )

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
        except Exception as exc:
            if _is_agent_cancelled(exc):
                abandon_once()
                raise RuntimeCancelled() from exc
            finish_or_raise("failed", "provider_error")
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
            )
        except BaseException:
            abandon_once()
            raise

        try:
            self._phase("result_normalize")
            self._check_cancel(cancel, invocation_control)
            normalized = _normalize_agent_result(raw_result)
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception:
            finish_or_raise("failed", "provider_error")
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
            )
        except BaseException:
            abandon_once()
            raise

        try:
            self._phase("message_persist")
            self._check_cancel(cancel, invocation_control)
            persisted_turn = self._persist_result(
                persistence,
                conversation_id,
                request,
                normalized,
                conversation,
                catalog=resolved.catalog,
                ensure_active=lambda: self._check_cancel(cancel, invocation_control),
                control=invocation_control,
            )
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
        except Exception:
            finish_or_raise("failed", "unknown")
            return self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True)
        except BaseException:
            abandon_once()
            raise

        outcome = persisted_turn.outcome
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
                    catalog=resolved.catalog,
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
            try:
                complete_once()
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
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
    ) -> RuntimeOutcome:
        """Dispatch a confirmation to the trusted deterministic bridge.

        General Agent confirmation remains owned by the later continuation
        extraction.  Keeping this narrow method here prevents a deterministic
        confirmation from accidentally resolving a model/provider dependency.
        """

        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")
        control = invocation_control
        if control is None:
            raise TypeError("invocation_control is required")
        resolved_transport = transport or RuntimeTransportContext(mode="sync")
        if resolved_transport.mode != "sync":
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)
        self._phase("validate")
        self._validate(request)
        self._check_cancel(lambda: False, control)
        conversation = self._load_confirmation_conversation(request)
        if conversation is None:
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        if _is_archived(conversation):
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409)
        adapter = self._dependencies.deterministic
        if adapter is None:
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)
        validate_action = _callable(adapter, ("validate_action",))
        if validate_action is not None:
            try:
                _invoke(validate_action, {"request": request}, (request,))
            except ValueError as exc:
                self._mark_completed_if_active(control)
                return self._failure(RuntimeFailureCode.INVALID_CONFIRMATION, str(exc), 422)
        terminal_probe = _callable(adapter, ("is_terminal_replay",))
        terminal_replay = (
            bool(_invoke(terminal_probe, {"request": request}, (request,)))
            if terminal_probe is not None
            else False
        )
        if not terminal_replay and request.approved and not self._confirmation_messages_exist(conversation_id):
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        original_pending = None if terminal_replay else adapter.pending_action(conversation)
        original, journal_holder, on_attempt, on_result = self._deterministic_confirmation_callbacks(
            adapter,
            conversation,
            resolved_transport,
            control,
            original=original_pending,
            edited=not request.edited_args.is_missing(),
        )
        try:
            execution = adapter.confirm(
                request,
                conversation,
                transport=resolved_transport,
                on_confirmation_attempt=on_attempt,
                on_tool_result=on_result,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        except Exception:
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._finish(recorder, journal_holder.get("started") is True, "failed", "unknown", control)
            self._mark_completed_if_active(control)
            return self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True)
        except BaseException:
            recorder = journal_holder.get("recorder")
            if recorder is not None:
                self._abandon(recorder, journal_holder.get("started") is True)
            raise
        outcome = execution.outcome if isinstance(execution, DeterministicExecution) else cast(RuntimeOutcome, execution)
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
        self._validate(cast(StartTurnRequest, request))
        self._check_cancel(lambda: False, invocation_control)

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
                    return self._stream_immediate(self._failure(code, text, 422), invocation_control)
                except LookupError:
                    return self._stream_immediate(
                        self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404),
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
        if (isinstance(request, ConfirmationRequest) or request.pilot_action is not None) and self._dependencies.deterministic is None:
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
                self._load_confirmation_conversation(request)
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

        adapter = self._dependencies.deterministic
        if isinstance(request, ConfirmationRequest):
            if adapter is None:
                return self._stream_immediate(
                    self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400),
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
            terminal_probe = _callable(adapter, ("is_terminal_replay",))
            terminal_replay = (
                bool(_invoke(terminal_probe, {"request": request}, (request,)))
                if terminal_probe is not None
                else False
            )
            if not terminal_replay and request.approved and not self._confirmation_messages_exist(conversation_id):
                return self._stream_immediate(
                    self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404),
                    invocation_control,
                )
            original_pending = None if terminal_replay else adapter.pending_action(conversation)
            original, journal_holder, on_attempt, on_result = self._deterministic_confirmation_callbacks(
                adapter,
                conversation,
                transport,
                invocation_control,
                original=original_pending,
                edited=not request.edited_args.is_missing(),
            )
            try:
                execution = adapter.confirm(
                    request,
                    conversation,
                    transport=transport,
                    on_confirmation_attempt=on_attempt,
                    on_tool_result=on_result,
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
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
                    invocation_control,
                )
            except BaseException:
                recorder = journal_holder.get("recorder")
                if recorder is not None:
                    self._abandon(recorder, journal_holder.get("started") is True)
                raise
            if not isinstance(execution, DeterministicExecution):
                return self._stream_immediate(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
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
            if pending_guard is not None and pending_guard is not False:
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
                    self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400),
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
                code = RuntimeFailureCode.OPERATION_UNAVAILABLE if "context" in text else RuntimeFailureCode.INVALID_CONFIRMATION
                status_code = 422
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(self._failure(code, text, status_code), invocation_control)
            except LookupError:
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(
                    self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404),
                    invocation_control,
                )
            except Exception:
                if "journal_started" in locals():
                    self._finish(recorder, journal_started, "failed", "unknown", invocation_control)
                return self._stream_immediate(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
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

        self._phase("model_resolve")
        resolved = self._resolve_model(request, conversation)
        if isinstance(resolved, RuntimeFailureOutcome):
            return self._stream_immediate(resolved, invocation_control)
        if resolved is None:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.MODEL_UNCONFIGURED,
                    "AI 设置尚未完成，请检查模型配置。",
                    503,
                ),
                invocation_control,
            )

        persistence = self._require_dependency("persistence")
        persistence_failure = self._validate_persistence_surface(persistence)
        if persistence_failure is not None:
            return self._stream_immediate(persistence_failure, invocation_control)

        self._phase("user_persist")
        self._check_cancel(lambda: False, invocation_control)
        try:
            user_result = self._persist_user(
                persistence,
                conversation_id,
                request.message,
                control=invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return self._stream_immediate(
                self._failure(
                    RuntimeFailureCode.OPERATION_FAILED,
                    "对话当前不可写入。",
                    503,
                    retryable=True,
                ),
                invocation_control,
            )
        if not _result_persisted(user_result):
            status = _failure_status(user_result)
            code = (
                RuntimeFailureCode.CONVERSATION_ARCHIVED
                if status == "closed"
                else RuntimeFailureCode.APPLICATION_NOT_FOUND
                if status == "not_found"
                else RuntimeFailureCode.OPERATION_FAILED
            )
            status_code = 409 if code is RuntimeFailureCode.CONVERSATION_ARCHIVED else 404 if code is RuntimeFailureCode.APPLICATION_NOT_FOUND else 503
            return self._stream_immediate(
                self._failure(
                    code,
                    "对话当前不可写入。",
                    status_code,
                    retryable=code is RuntimeFailureCode.OPERATION_FAILED,
                ),
                invocation_control,
            )

        input_message_id = _attribute(user_result, "message_id")
        if type(input_message_id) is not int or input_message_id <= 0:
            try:
                persisted_ids = self._snapshot_message_ids(persistence, conversation_id)
            except _PersistenceReadbackError:
                return self._stream_immediate(
                    self._failure(
                        RuntimeFailureCode.OPERATION_FAILED,
                        "对话结果暂时无法保存。",
                        503,
                        retryable=True,
                    ),
                    invocation_control,
                )
            input_message_id = persisted_ids[-1] if persisted_ids else None

        try:
            self._phase("source_load")
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

        try:
            self._phase("context_assemble")
            assembled = self._assemble_context(source, conversation, request)
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

        self._phase("transport_identity")
        self._check_cancel(lambda: False, invocation_control)
        self._phase("run_start")
        recorder, journal_started = self._start_journal(
            conversation,
            conversation_id,
            input_message_id,
            request,
            transport,
        )

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
                resolved.catalog,
                persistence,
                invocation_control,
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            self._abandon(recorder, journal_started)
            raise
        except Exception:
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

        cell = _PreparedExecutionCell(run_open=journal_started)
        model_token = object()
        self._prepared_models[model_token] = resolved
        model_lease = _PreparedModelLease(
            lambda: self._prepared_models.pop(model_token, None)
        )

        def release_model() -> None:
            model_lease.release_once()

        def on_abort() -> None:
            release_model()
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
            release_model()
            with cell.lock:
                if cell.aborted or cell.completed:
                    return
                cell.run_open = False
                cell.completed = True

        try:
            frozen_assembled = _freeze_stream_value(assembled)
        except (TypeError, ValueError):
            release_model()
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
        assembled_values = (
            cast(tuple[object, ...], frozen_assembled)
            if isinstance(frozen_assembled, tuple)
            else (frozen_assembled,)
        )
        state = _PreparedStreamState(
            owner_token=self._owner_token,
            preparation_kind=PreparationKind.MODEL,
            execution_mode=StreamExecutionMode.AGENT_HOST,
            control=invocation_control,
            request=request,
            conversation=_prepared_conversation(conversation, conversation_id),
            conversation_id=conversation_id,
            model_token=model_token,
            assembled=assembled_values,
            recorder=recorder,
            journal_started=journal_started,
            transport=transport,
            cell=cell,
            on_abort=on_abort,
            on_complete=on_complete,
        )
        transport_run_id = transport.transport_run_id
        if transport_run_id is None:  # pragma: no cover - RuntimeTransportContext validates this
            release_model()
            self._abandon(recorder, journal_started)
            return self._stream_immediate(
                self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400),
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
            release_model()
            self._abandon(recorder, journal_started)
            raise
        self._phase("prepared")
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
            self._prepared_models.get(state.model_token)
            if state.model_token is not None
            else None
        )
        if resolved_model is None or state.conversation is None or state.conversation_id is None:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise RuntimeTransportAborted()

        try:
            emit_runtime_event(
                safe_event_sink,
                MetaEvent(
                    supports_delta=callable(
                        getattr(resolved_model.model, "stream_complete", None)
                    )
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

            driver = self._require_dependency("agent_driver")

            def build_invocation(agent_events: RuntimeEventSink) -> AgentInvocation:
                return self._agent_invocation(
                    resolved_model,
                    tuple(_materialize_stream_value(item) for item in state.assembled),
                    state.conversation,
                    cast(StartTurnRequest, state.request),
                    state.recorder,
                    agent_events,
                    safe_signal_sink,
                    checked_cancel,
                )

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
            emit_runtime_event(safe_event_sink, ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded))
            emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
            return finish(outcome, CompletionReason.CANCELLED)
        except RuntimeCancelled:
            abort(CompletionReason.CANCELLED)
            raise
        except RuntimeTransportAborted:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise
        except Exception as exc:
            del exc
            outcome = self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
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
            emit_runtime_event(safe_event_sink, ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded))
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
            persisted_turn = self._persist_result(
                persistence,
                state.conversation_id,
                cast(StartTurnRequest, state.request),
                normalized,
                state.conversation,
                catalog=resolved_model.catalog,
                ensure_active=lambda: self._check_cancel(cancel_check, state.control),
                control=state.control,
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
                    catalog=resolved_model.catalog,
                )
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                if normalized.pending is not None:
                    args, token = _safe_pending_payload(normalized.pending)
                    from .contracts import PendingActionPayload

                    payload = PendingActionPayload(
                        tool_name=normalized.pending.tool_name,
                        operation_id=normalized.pending.operation_id or normalized.pending.tool_call_id,
                        human=normalized.pending.human,
                        args=args,
                        confirmation_token=token,
                    )
                    emit_runtime_event(
                        safe_event_sink,
                        StatusEvent(phase="waiting_confirmation", label="需要确认"),
                    )
                    emit_runtime_event(
                        safe_event_sink,
                        ConfirmationRequiredEvent(
                            confirmation_token=token,
                            operation_id=normalized.pending.operation_id or None,
                            pending_action=payload,
                        ),
                    )
            elif isinstance(outcome, RuntimeFailureOutcome):
                self._finish(state.recorder, state.journal_started, "failed", "unknown", state.control)
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
                self._finish(state.recorder, state.journal_started, "completed", None, state.control)
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
                if isinstance(outcome, (MessageOutcome, OperationReplayOutcome)):
                    emit_runtime_event(safe_event_sink, AssistantMessageEvent(message=outcome.message))
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
                self._finish(state.recorder, state.journal_started, "failed", "unknown", state.control)
                close_terminal_owner()
                self._mark_completed_if_active(state.control)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abort(CompletionReason.TRANSPORT_ABORTED)
                raise
            emit_runtime_event(safe_event_sink, ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded))
            emit_runtime_event(safe_event_sink, CompletedEvent(response=outcome))
            return finish(outcome, CompletionReason.NORMAL)
        except BaseException:
            abort(CompletionReason.TRANSPORT_ABORTED)
            raise

    def _stream_immediate(
        self,
        outcome: RuntimeFailureOutcome,
        control: RuntimeInvocationControl,
    ) -> ImmediateHttpOutcome:
        self._mark_completed_if_active(control)
        payload: dict[str, object] = {
            "error": outcome.message,
            "error_code": outcome.code.value,
        }
        if outcome.pending_action is not None:
            payload["pending_action"] = outcome.pending_action.as_mapping()
        return ImmediateHttpOutcome(
            status_code=outcome.status_code,
            payload=freeze_json_mapping(payload),
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
        if isinstance(outcome, RuntimeFailureOutcome):
            return self._stream_immediate(outcome, invocation_control)
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
                    }
                ),
            )
        transport_run_id = transport.transport_run_id
        if transport_run_id is None:  # pragma: no cover - transport validates this
            return self._stream_immediate(
                self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400),
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
            events=execution.events,
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
        return _invoke(function, {"conversation_id": request.conversation_id, "id": request.conversation_id}, (request.conversation_id,))

    def _load_confirmation_conversation(self, request: ConfirmationRequest) -> object | None:
        """Load-only conversation path for confirmation preheader checks."""

        gateway = self._require_dependency("conversation_gateway")
        function = _callable(gateway, ("load", "get", "get_conversation", "create_or_load"))
        if function is None:
            raise TypeError("conversation gateway does not provide load")
        return _invoke(
            function,
            {"conversation_id": request.conversation_id, "id": request.conversation_id},
            (request.conversation_id,),
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

    def _pending_guard(self, conversation_id: int, conversation: object, request: StartTurnRequest) -> object | None:
        target = self._dependencies.pending_guard or self._dependencies.persistence
        function = _callable(target, ("get_pending_action", "pending_guard", "get_live_pending", "check"))
        if function is None:
            return None
        return _invoke(
            function,
            {"conversation_id": conversation_id, "conversation": conversation, "request": request},
            (conversation_id,),
        )

    def _resolve_model(
        self,
        request: StartTurnRequest,
        conversation: object,
    ) -> ResolvedModel | RuntimeFailureOutcome | None:
        resolver = self._require_dependency("model_resolver")
        function = _callable(resolver, ("resolve", "resolve_model", "get_model"))
        if function is None:
            raise TypeError("model resolver does not provide resolve")
        try:
            value = _invoke(
                function,
                {
                    "request": request,
                    "conversation": conversation,
                    "conversation_id": _conversation_id(conversation),
                },
                (request, conversation),
            )
        except ModelUnconfiguredError:
            return self._failure(
                RuntimeFailureCode.MODEL_UNCONFIGURED,
                "AI 设置尚未完成，请检查模型配置。",
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
        if value is False or (
            isinstance(value, tuple) and bool(value) and value[0] is False
        ):
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 连接失败。请检查 AI 设置或稍后重试。",
                502,
                retryable=True,
            )
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
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or type(result[0]) is not bool
        ):
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
                    "execution_path": execution_path or ("model_turn" if route_kind == "model" else "deterministic_action"),
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
                    "execution_path": "deterministic_confirmation",
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
        Callable[[PendingAction, str, bool], object],
    ]:
        holder: dict[str, object] = {}
        attempt_id = str(uuid4())

        def attempt(effective: PendingAction, approved: bool) -> None:
            if original is None:
                return
            recorder, started = self._resume_journal_confirmation(
                _conversation_id(conversation) or 0,
                original,
                transport,
            )
            holder["recorder"] = recorder
            holder["started"] = started
            self._capture_confirmation_journal_context(
                recorder,
                started,
                conversation,
                _conversation_id(conversation) or 0,
                self._require_dependency("persistence"),
                control,
                tool_names=(original.tool_name,),
            )
            self._record_journal_approval(
                recorder,
                started,
                attempt_id,
                original,
                effective,
                approved,
                edited=edited,
                control=control,
            )
            if approved:
                self._record_journal_tool_start(recorder, started, effective, control)

        def result(effective: PendingAction, value: str, succeeded: bool) -> None:
            recorder = holder.get("recorder")
            started = holder.get("started") is True
            if recorder is not None:
                self._record_journal_tool_result(
                    recorder,
                    started,
                    effective,
                    value,
                    succeeded,
                    control,
                )

        return original, holder, attempt, result

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
        if isinstance(outcome, RuntimeFailureOutcome) and outcome.code is RuntimeFailureCode.STALE_PENDING_ACTION:
            self._abandon(recorder, True)
        elif original is not None and current is not None and current.tool_call_id != original.tool_call_id:
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
            "context_type": str(_attribute(conversation, "context_type", "workspace") or "workspace"),
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
            {"tool_call_id": original.tool_call_id, "tool_name": original.tool_name, "args": original.args},
            control=control,
        )
        decided_fingerprint = self._journal_call(
            recorder,
            "fingerprint_pending_identity",
            {"tool_call_id": effective.tool_call_id, "tool_name": effective.tool_name, "args": effective.args},
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
        replay = pending_before is not None and pending_before.tool_name in LEGACY_DETERMINISTIC_NAMES
        if replay:
            assert pending_before is not None
            recorder, started = self._resume_journal_replay(conversation_id, pending_before, transport)
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
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "application not found", 404)
        except ValueError as exc:
            self._finish(recorder, started, "failed", "unknown", control)
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, str(exc), 422)
        except Exception:
            self._finish(recorder, started, "failed", "unknown", control)
            return self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True)
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
                self._suspend(recorder, started, pending, control, catalog=None, trusted_legacy=True)
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
        replay = pending is not None and pending.tool_name in LEGACY_DETERMINISTIC_NAMES
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
            return None

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
    def _journal_tool_names(catalog: object | None) -> tuple[str, ...]:
        if catalog is None:
            return ()
        provider_contracts = _callable(catalog, ("provider_contracts",))
        if provider_contracts is None:
            return ()
        try:
            contracts = provider_contracts()
        except BaseException:
            return ()
        if not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)):
            return ()
        try:
            return tuple(
                str(name)
                for contract in contracts
                if (name := _attribute(contract, "name")) is not None
            )
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
        if type(input_message_id) is int and input_message_id > 0 and input_message_id not in message_ids:
            message_ids = (*message_ids, input_message_id)
        logical_input = {
            "conversation_id": conversation_id,
            "context_type": str(_attribute(conversation, "context_type", "workspace") or "workspace"),
            "context_ref": str(_attribute(conversation, "context_ref", "") or ""),
            "mode": str(_attribute(conversation, "mode", "general") or "general"),
            "message_count": len(message_ids),
            "tool_names": list(self._journal_tool_names(catalog)),
        }
        manifest = ContextManifestInput(
            conversation_message_ids=message_ids,
            tool_names=self._journal_tool_names(catalog),
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

    def _load_source(self, conversation: object, request: StartTurnRequest) -> object:
        loader = self._require_dependency("source_loader")
        function = _callable(loader, ("load", "load_sources", "load_chat_source_messages"))
        if function is None:
            raise TypeError("source loader does not provide load")
        return _invoke(
            function,
            {
                "conversation": conversation,
                "request": request,
                "attachments": request.attachments,
                "page_context": request.page_context,
                "conversation_id": _conversation_id(conversation),
            },
            (conversation, request),
        )

    def _assemble_context(self, source: object, conversation: object, request: StartTurnRequest) -> object:
        assembler = self._dependencies.context_assembler
        if assembler is None:
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                return tuple(source)
            messages = _attribute(source, "messages", _attribute(source, "history"))
            return messages if messages is not None else source
        function = _callable(assembler, ("assemble", "assemble_context", "build_messages"))
        if function is None:
            raise TypeError("context assembler does not provide assemble")
        values = {
            "source": source,
            "sources": source,
            "conversation": conversation,
            "request": request,
            "page_context": request.page_context,
            "attachments": request.attachments,
        }
        return _invoke(function, values, (source, conversation, request))

    def _agent_invocation(
        self,
        resolved: ResolvedModel,
        assembled: object,
        conversation: object,
        request: StartTurnRequest,
        recorder: object,
        event_sink: RuntimeEventSink,
        signal_sink: RuntimeSignalSink[str] | None,
        cancel_check: Callable[[], bool],
    ) -> AgentInvocation:
        if isinstance(assembled, Sequence) and not isinstance(assembled, (str, bytes)):
            messages = tuple(assembled)
        else:
            raw = _attribute(assembled, "messages", _attribute(assembled, "history"))
            messages = tuple(raw) if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else (assembled,)
        return AgentInvocation(
            model=resolved.model,
            catalog=resolved.catalog,
            messages=messages,
            config=resolved.config,
            conversation=conversation,
            request=request,
            tool_context=resolved.tool_context,
            auto_approve=resolved.auto_approve,
            max_iter=resolved.max_iter,
            thread_id=resolved.thread_id or f"conversation:{_conversation_id(conversation)}",
            run_recorder=recorder,
            event_sink=event_sink,
            signal_sink=signal_sink,
            cancel_check=cancel_check,
        )

    def _run_driver(self, driver: object, invocation: AgentInvocation) -> object:
        function = _callable(driver, ("run_turn", "run"))
        if function is None:
            raise TypeError("agent driver does not provide run_turn")
        values: dict[str, object] = {
            "invocation": invocation,
            "agent_invocation": invocation,
            "model": invocation.model,
            "catalog": invocation.catalog,
            "tool_catalog": invocation.catalog,
            "messages": list(invocation.messages),
            "history": list(invocation.messages),
            "context": invocation,
            "auto_approve": invocation.auto_approve,
            "max_iter": invocation.max_iter,
            "max_iterations": invocation.max_iter,
            "thread_id": invocation.thread_id,
            "run_recorder": invocation.run_recorder,
            "runtime_signal_sink": invocation.signal_sink,
            "signal_sink": invocation.signal_sink,
            "tool_context": invocation.tool_context,
            "event_sink": invocation.event_sink,
            "cancel_check": invocation.cancel_check,
        }
        optional = {
            "auto_approve": invocation.auto_approve,
            "max_iter": invocation.max_iter,
            "thread_id": invocation.thread_id,
            "run_recorder": invocation.run_recorder,
            "runtime_signal_sink": invocation.signal_sink,
            "tool_context": invocation.tool_context,
            "event_sink": invocation.event_sink,
            "cancel_check": invocation.cancel_check,
        }
        return _invoke(function, values, (invocation,), var_keyword_values=optional)

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
        function = _callable(persistence, ("persist_assistant_message", "persist_initial_assistant_message"))
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
        ensure_active: Callable[[], None],
        control: RuntimeInvocationControl,
    ) -> _PersistedTurn:
        del request
        pending = result.pending
        if pending is not None and not _valid_pending_action(pending, catalog):
            return _PersistedTurn(
                self._failure(RuntimeFailureCode.OPERATION_FAILED, "待确认操作暂时无法保存。", 503, retryable=True)
            )
        effective_messages, forced_reply = _with_write_error_followup(
            result.added,
            result.records,
            result.failures,
        )
        reply = forced_reply or _user_facing_assistant_content(result.reply)
        write_status, write_error = _write_outcome(
            result.records,
            _has_write_attempt(result.added, result.records, catalog),
            result.failures,
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
            question = self._missing_question(pending, conversation_id)
            if question:
                return self._persist_clarification(
                    persistence,
                    conversation_id,
                    messages,
                    pending,
                    question,
                    catalog=catalog,
                    ensure_active=ensure_active,
                    control=control,
                )
            function = _callable(persistence, ("persist_initial_pending", "persist_pending"))
            if function is None:
                raise TypeError("persistence does not provide atomic pending persistence")
            commit_function = function
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = self._commit_fence(
                control,
                lambda: _invoke(
                    commit_function,
                    {"conversation_id": conversation_id, "messages": messages, "pending": pending},
                    (conversation_id, messages, pending),
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
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "待确认操作暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
            args, token = _safe_pending_payload(pending)
            from .contracts import PendingActionPayload  # local import keeps module exports compact

            payload = PendingActionPayload(
                tool_name=pending.tool_name,
                operation_id=pending.operation_id or pending.tool_call_id,
                human=pending.human,
                args=args,
                confirmation_token=token,
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
        function = _callable(persistence, ("persist_initial_messages", "persist_messages", "persist_ai_messages"))
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
                catalog,
            )
            if forced_pending is not None:
                if not _valid_pending_action(forced_pending, catalog):
                    return _PersistedTurn(
                        self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                        message_ids,
                    )
                setter = _callable(persistence, ("set_pending_clarification",))
                if setter is None:
                    return _PersistedTurn(
                        self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                        message_ids,
                    )
                commit_setter = setter
                ensure_active()
                set_result = self._commit_fence(
                    control,
                    lambda: _invoke(
                        commit_setter,
                        {
                            "conversation_id": conversation_id,
                            "pending": forced_pending,
                            "question": forced_reply,
                        },
                        (conversation_id, forced_pending, forced_reply),
                    ),
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
                        self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                        message_ids,
                    )
        elif clarification is not None and _looks_like_followup_question(reply):
            if not _valid_pending_action(
                clarification[0],
                catalog,
                require_operation_id=False,
            ):
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
            setter = _callable(persistence, ("set_pending_clarification",))
            if setter is None:
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
            ensure_active()
            set_result = self._commit_fence(
                control,
                lambda: _invoke(
                    setter,
                    {
                        "conversation_id": conversation_id,
                        "pending": clarification[0],
                        "question": reply,
                    },
                    (conversation_id, clarification[0], reply),
                ),
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
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
        else:
            clear = _callable(persistence, ("clear_pending_clarification",))
            if clarification is not None and clear is None:
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法清理。", 503, retryable=True),
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
                        self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法清理。", 503, retryable=True),
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
            raise _PersistenceReadbackError("persistence clarification readback capability is missing")
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
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        return self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True)

    def _missing_question(self, pending: PendingAction, conversation_id: int) -> str | None:
        function = _callable(self._dependencies.missing_target_question, ("missing_target_question", "question", "resolve"))
        if function is None:
            value = _attribute(pending, "missing_question")
            return value if isinstance(value, str) and value else None
        value = _invoke(function, {"pending": pending, "conversation_id": conversation_id}, (pending, conversation_id))
        return value if isinstance(value, str) and value else None

    def _persist_clarification(
        self,
        persistence: object,
        conversation_id: int,
        messages: Sequence[Message],
        pending: PendingAction,
        question: str,
        *,
        catalog: object | None,
        ensure_active: Callable[[], None],
        control: RuntimeInvocationControl,
    ) -> _PersistedTurn:
        if not _valid_pending_action(pending, catalog):
            return _PersistedTurn(
                self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True)
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
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
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
            message_ids = self._result_message_ids(persistence, conversation_id, persisted, before_ids)
            if not _result_persisted(persisted):
                return _PersistedTurn(self._persistence_failure(persisted, "对话已归档，无法保存回复。"), message_ids)
            clear = _callable(persistence, ("clear_pending_action",))
            if clear is not None:
                ensure_active()
                clear_result = self._commit_fence(
                    control,
                    lambda: _invoke(clear, {"conversation_id": conversation_id}, (conversation_id,)),
                )
                if not _result_persisted(clear_result):
                    return _PersistedTurn(self._persistence_failure(clear_result, "对话已归档，无法保存回复。"), message_ids)
                if not self._verify_pending_cleared(
                    persistence,
                    conversation_id,
                    clarification=False,
                ):
                    return _PersistedTurn(
                        self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
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
                        {"conversation_id": conversation_id, "pending": pending, "question": question},
                        (conversation_id, pending, question),
                    ),
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                raise
            except Exception:
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
            if set_result is None:
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
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
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话澄清暂时无法保存。", 503, retryable=True),
                    message_ids,
                )
            assistant = _callable(persistence, ("persist_assistant_message", "persist_initial_assistant_message"))
            if assistant is None:
                return _PersistedTurn(
                    self._failure(RuntimeFailureCode.OPERATION_FAILED, "对话结果暂时无法保存。", 503, retryable=True),
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
            assistant_ids = self._result_message_ids(persistence, conversation_id, persisted, before_ids)
            message_ids = tuple(dict.fromkeys((*message_ids, *assistant_ids)))
        if not _result_persisted(persisted):
            return _PersistedTurn(self._persistence_failure(persisted, "对话已归档，无法保存回复。"), message_ids)
        return _PersistedTurn(MessageOutcome(message=question, conversation_id=conversation_id), message_ids)

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
    ) -> RuntimeFailureOutcome:
        return RuntimeFailureOutcome(
            code=code,
            message=message,
            status_code=status_code,
            retryable=retryable,
            degraded=degraded,
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
        if not _valid_pending_action(pending, catalog, trusted_legacy=trusted_legacy):
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
        "model": "model_resolver",
        "model_resolver_once": "model_resolver",
        "model_config_resolver": "model_resolver",
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
    "AgentInvocation",
    "ContextAssembler",
    "ConversationGateway",
    "JournalFactory",
    "ModelResolver",
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
    "ToolCatalog",
]
