"""Transport-independent, closed contracts for the Pilot Runtime.

This module intentionally has no FastAPI/Starlette, ORM, persistence, or SSE
imports.  Values crossing the boundary are small immutable dataclasses and
finite enums.  Runtime-owned opaque state is kept out of repr/serialization.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from threading import Lock
from typing import (
    Literal,
    Protocol,
    TYPE_CHECKING,
    TypeAlias,
    TypeVar,
    cast,
    runtime_checkable,
)
from uuid import UUID

from .errors import RuntimeFailureCode


if TYPE_CHECKING:
    class StrEnum(str, Enum):
        pass
else:
    try:
        from enum import StrEnum
    except ImportError:  # pragma: no cover - Python 3.10 compatibility
        class StrEnum(str, Enum):
            def __str__(self) -> str:
                return self.value


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
ImmutablePayload: TypeAlias = Mapping[str, JsonValue]
StreamVersion: TypeAlias = Literal["pilot-sse-v1"]


def _reject_framework_value(value: object, *, field_name: str) -> None:
    """Reject framework/ORM objects without importing those optional modules."""

    module = getattr(type(value), "__module__", "")
    if module == "fastapi" or module.startswith("fastapi."):
        raise TypeError(f"{field_name} cannot contain FastAPI objects")
    if module == "starlette" or module.startswith("starlette."):
        raise TypeError(f"{field_name} cannot contain Starlette objects")
    if module == "sqlalchemy" or module.startswith("sqlalchemy."):
        raise TypeError(f"{field_name} cannot contain ORM objects")


def _validate_json_value(value: object, *, field_name: str) -> None:
    _reject_framework_value(value, field_name=field_name)
    if value is None or type(value) in {str, int, float, bool}:
        return
    if isinstance(value, Mapping):
        # A mutable mapping would make a frozen DTO mutable by aliasing.
        if not isinstance(value, MappingProxyType):
            raise TypeError(f"{field_name} must use an immutable mapping")
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError(f"{field_name} keys must be strings")
            _validate_json_value(child, field_name=f"{field_name}.{key}")
        return
    if type(value) is tuple:
        for index, child in enumerate(cast(tuple[object, ...], value)):
            _validate_json_value(child, field_name=f"{field_name}[{index}]")
        return
    raise TypeError(f"{field_name} contains an unsupported value")


def _require_text(value: object, *, field_name: str, allow_empty: bool = True) -> str:
    _reject_framework_value(value, field_name=field_name)
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_int(value: object, *, field_name: str) -> int:
    _reject_framework_value(value, field_name=field_name)
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    return value


def _require_bool(value: object, *, field_name: str) -> bool:
    _reject_framework_value(value, field_name=field_name)
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _require_immutable_mapping(value: object, *, field_name: str) -> Mapping[str, JsonValue]:
    _reject_framework_value(value, field_name=field_name)
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an immutable mapping")
    if not isinstance(value, MappingProxyType):
        raise TypeError(f"{field_name} must use an immutable mapping")
    mapping = cast(Mapping[str, JsonValue], value)
    _validate_json_value(mapping, field_name=field_name)
    return _freeze_mapping(mapping)


def _freeze_value(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if type(value) is tuple:
        return tuple(_freeze_value(child) for child in value)
    return value


def _freeze_mapping(value: Mapping[str, JsonValue]) -> MappingProxyType[str, JsonValue]:
    return MappingProxyType({key: _freeze_value(child) for key, child in value.items()})


def _empty_json_object() -> MappingProxyType[str, JsonValue]:
    return MappingProxyType({})


def _require_payload_tuple(
    value: object,
    *,
    field_name: str,
) -> tuple[ImmutablePayload, ...]:
    _reject_framework_value(value, field_name=field_name)
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    items: list[ImmutablePayload] = []
    for index, item in enumerate(cast(tuple[object, ...], value)):
        items.append(_require_immutable_mapping(item, field_name=f"{field_name}[{index}]"))
    return tuple(items)


class PreparedLifecycleState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    ABORTED = "aborted"
    COMPLETED = "completed"


class CompletionReason(StrEnum):
    NORMAL = "normal"
    CANCELLED = "cancelled"
    TRANSPORT_ABORTED = "transport_aborted"


class PreparationKind(StrEnum):
    MODEL = "model"
    DETERMINISTIC_INITIAL = "deterministic_initial"
    DETERMINISTIC_CONFIRMATION = "deterministic_confirmation"
    CONFIRMATION = "confirmation"
    REPLAY = "replay"


class StreamExecutionMode(StrEnum):
    DIRECT = "direct"
    AGENT_HOST = "agent_host"


class SignalEmitResult(StrEnum):
    EMITTED = "emitted"
    DUPLICATE = "duplicate"
    CLOSED = "closed"
    FULL = "full"
    DEGRADED = "degraded"


class InvocationState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CancelReason(StrEnum):
    CLIENT_DISCONNECT = "client_disconnect"
    EXPLICIT_CANCEL = "explicit_cancel"
    DEADLINE = "deadline"
    TRANSPORT_ABORTED = "transport_aborted"


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PreparedLifecycle:
    """A four-state, single-use lifecycle with atomic CAS transitions."""

    _state: PreparedLifecycleState
    _completion_reason: CompletionReason | None
    _lock: Lock

    def __init__(
        self,
        state: PreparedLifecycleState = PreparedLifecycleState.PREPARED,
        completion_reason: CompletionReason | None = None,
    ) -> None:
        if not isinstance(state, PreparedLifecycleState):
            raise TypeError("state must be a PreparedLifecycleState")
        if state is PreparedLifecycleState.COMPLETED:
            if not isinstance(completion_reason, CompletionReason):
                raise ValueError("completed lifecycle requires a completion reason")
        elif completion_reason is not None:
            raise ValueError("only completed lifecycle may have a completion reason")
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_completion_reason", completion_reason)
        object.__setattr__(self, "_lock", Lock())

    @property
    def state(self) -> PreparedLifecycleState:
        with self._lock:
            return self._state

    @property
    def completion_reason(self) -> CompletionReason | None:
        with self._lock:
            return self._completion_reason

    def begin(self) -> bool:
        with self._lock:
            if self._state is not PreparedLifecycleState.PREPARED:
                return False
            object.__setattr__(self, "_state", PreparedLifecycleState.EXECUTING)
            return True

    def abort_if_prepared(self) -> bool:
        with self._lock:
            if self._state is not PreparedLifecycleState.PREPARED:
                return False
            object.__setattr__(self, "_state", PreparedLifecycleState.ABORTED)
            # An abort before execution never receives a completion reason.
            object.__setattr__(self, "_completion_reason", None)
            return True

    def complete(self, reason: CompletionReason) -> bool:
        with self._lock:
            if self._state is not PreparedLifecycleState.EXECUTING:
                return False
            if not isinstance(reason, CompletionReason):
                return False
            # State and reason are one atomic transition under this lock.
            object.__setattr__(self, "_state", PreparedLifecycleState.COMPLETED)
            object.__setattr__(self, "_completion_reason", reason)
            return True


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    kind: str
    ref: str

    def __post_init__(self) -> None:
        _require_text(self.kind, field_name="kind", allow_empty=False)
        _require_text(self.ref, field_name="ref", allow_empty=False)


@dataclass(frozen=True, slots=True)
class PilotActionDescriptor:
    """Validated, reference-only deterministic action descriptor."""

    kind: str
    value: str = ""

    def __post_init__(self) -> None:
        _require_text(self.kind, field_name="kind", allow_empty=False)
        _require_text(self.value, field_name="value")


@dataclass(frozen=True, slots=True)
class EditedArgs(Mapping[str, JsonValue]):
    """Immutable confirmation argument state.

    ``_values is None`` means the field was missing.  An empty immutable mapping
    is distinct from missing, as required by confirmation compatibility rules.
    """

    _values: MappingProxyType[str, JsonValue] | None = field(repr=False)

    @classmethod
    def missing(cls) -> EditedArgs:
        return cls(None)

    @classmethod
    def from_mapping(cls, value: Mapping[str, JsonValue]) -> EditedArgs:
        mapping = _require_immutable_mapping(value, field_name="edited_args")
        return cls(MappingProxyType(dict(mapping)))

    def is_missing(self) -> bool:
        return self._values is None

    def is_empty(self) -> bool:
        return self._values is not None and not self._values

    @property
    def as_mapping(self) -> Mapping[str, JsonValue]:
        return MappingProxyType({}) if self._values is None else self._values

    def __getitem__(self, key: str) -> JsonValue:
        return self.as_mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_mapping)

    def __len__(self) -> int:
        return len(self.as_mapping)


MISSING_EDITED_ARGS = EditedArgs.missing()


@dataclass(frozen=True, slots=True)
class PendingActionPayload:
    """Immutable projection of the complete confirmation-card payload."""

    tool_name: str
    operation_id: str
    human: str
    args: ImmutablePayload = field(repr=False)
    confirmation_token: str = field(repr=False)
    editable_fields: tuple[ImmutablePayload, ...] = ()
    details: ImmutablePayload = field(default_factory=_empty_json_object, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.tool_name, field_name="tool_name", allow_empty=False)
        _require_text(self.operation_id, field_name="operation_id", allow_empty=False)
        _require_text(self.human, field_name="human")
        object.__setattr__(
            self,
            "args",
            _require_immutable_mapping(self.args, field_name="args"),
        )
        _require_text(self.confirmation_token, field_name="confirmation_token", allow_empty=False)
        object.__setattr__(
            self,
            "editable_fields",
            _require_payload_tuple(self.editable_fields, field_name="editable_fields"),
        )
        object.__setattr__(
            self,
            "details",
            _require_immutable_mapping(self.details, field_name="details"),
        )

    def as_mapping(self) -> ImmutablePayload:
        payload: dict[str, JsonValue] = {
            "tool_name": self.tool_name,
            "operation_id": self.operation_id,
            "human": self.human,
            "args": self.args,
            "confirmation_token": self.confirmation_token,
            "editable_fields": self.editable_fields,
        }
        payload.update(self.details)
        return _freeze_mapping(payload)


@dataclass(frozen=True, slots=True)
class StartTurnRequest:
    message: str
    conversation_id: int | None = None
    mode: str = "general"
    context_type: str = "workspace"
    context_ref: str = ""
    page_context: ImmutablePayload | None = None
    attachments: tuple[AttachmentReference, ...] = ()
    pilot_action: PilotActionDescriptor | None = None

    def __post_init__(self) -> None:
        _require_text(self.message, field_name="message", allow_empty=False)
        if self.conversation_id is not None:
            _require_int(self.conversation_id, field_name="conversation_id")
            if self.conversation_id < 0:
                raise ValueError("conversation_id must be non-negative")
        _require_text(self.mode, field_name="mode", allow_empty=False)
        _require_text(self.context_type, field_name="context_type", allow_empty=False)
        _require_text(self.context_ref, field_name="context_ref")
        if self.page_context is not None:
            object.__setattr__(
                self,
                "page_context",
                _require_immutable_mapping(self.page_context, field_name="page_context"),
            )
        _reject_framework_value(self.attachments, field_name="attachments")
        if type(self.attachments) is not tuple:
            raise TypeError("attachments must be a tuple")
        if any(not isinstance(item, AttachmentReference) for item in self.attachments):
            raise TypeError("attachments must contain AttachmentReference values")
        if self.pilot_action is not None and not isinstance(self.pilot_action, PilotActionDescriptor):
            raise TypeError("pilot_action must be a PilotActionDescriptor")


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    conversation_id: int
    approved: bool
    confirmation_token: str = field(default="", repr=False)
    operation_id: str | None = None
    edited_args: EditedArgs = field(default_factory=EditedArgs.missing)
    rejection_feedback: str = ""

    def __post_init__(self) -> None:
        _require_int(self.conversation_id, field_name="conversation_id")
        if self.conversation_id < 1:
            raise ValueError("conversation_id must be positive")
        _require_bool(self.approved, field_name="approved")
        _require_text(self.confirmation_token, field_name="confirmation_token")
        if self.operation_id is not None:
            _require_text(self.operation_id, field_name="operation_id")
        if isinstance(self.edited_args, EditedArgs):
            pass
        elif isinstance(self.edited_args, Mapping):
            # Mutable mappings must never be accepted, and immutable mappings are
            # normalized to the explicit missing/empty/non-empty wrapper.
            object.__setattr__(self, "edited_args", EditedArgs.from_mapping(self.edited_args))
        elif self.edited_args is None:
            raise ValueError("edited_args=null is not valid")
        else:
            raise TypeError("edited_args must be omitted or an immutable mapping")
        _require_text(self.rejection_feedback, field_name="rejection_feedback")


@dataclass(frozen=True, slots=True)
class RuntimeTransportContext:
    mode: Literal["sync", "stream"]
    transport_run_id: UUID | None = None
    stream_version: StreamVersion | None = None

    def __post_init__(self) -> None:
        _reject_framework_value(self.mode, field_name="mode")
        if self.mode not in {"sync", "stream"}:
            raise ValueError("mode must be sync or stream")
        if self.transport_run_id is not None and not isinstance(self.transport_run_id, UUID):
            raise TypeError("transport_run_id must be a UUID")
        if self.mode == "sync" and self.stream_version is not None:
            raise ValueError("sync transport cannot carry a stream version")
        if self.mode == "stream" and self.stream_version != "pilot-sse-v1":
            raise ValueError("stream transport requires pilot-sse-v1")


@dataclass(frozen=True, slots=True)
class ImmediateHttpOutcome:
    status_code: int
    payload: ImmutablePayload

    def __post_init__(self) -> None:
        _require_int(self.status_code, field_name="status_code")
        if self.status_code < 100 or self.status_code > 599:
            raise ValueError("status_code must be a valid HTTP status")
        object.__setattr__(
            self,
            "payload",
            _require_immutable_mapping(self.payload, field_name="payload"),
        )

    @property
    def response_payload(self) -> ImmutablePayload:
        return self.payload


@dataclass(frozen=True, slots=True)
class MessageOutcome:
    message: str
    conversation_id: int | None = None
    write_status: str | None = None
    write_error: str | None = None
    undo: ImmutablePayload | None = None
    operation_id: str | None = None
    replayed: bool = False
    persisted: bool = True

    def __post_init__(self) -> None:
        _require_text(self.message, field_name="message")
        if self.conversation_id is not None:
            _require_int(self.conversation_id, field_name="conversation_id")
        if self.write_status is not None:
            _require_text(self.write_status, field_name="write_status")
        if self.write_error is not None:
            _require_text(self.write_error, field_name="write_error")
        if self.undo is not None:
            object.__setattr__(
                self,
                "undo",
                _require_immutable_mapping(self.undo, field_name="undo"),
            )
        if self.operation_id is not None:
            _require_text(self.operation_id, field_name="operation_id")
        _require_bool(self.replayed, field_name="replayed")
        _require_bool(self.persisted, field_name="persisted")


@dataclass(frozen=True, slots=True)
class ConfirmationRequiredOutcome:
    confirmation_token: str = field(repr=False)
    conversation_id: int | None = None
    operation_id: str | None = None
    message: str = ""
    pending_action: PendingActionPayload | None = None

    def __post_init__(self) -> None:
        _require_text(self.confirmation_token, field_name="confirmation_token", allow_empty=False)
        if self.conversation_id is not None:
            _require_int(self.conversation_id, field_name="conversation_id")
        if self.operation_id is not None:
            _require_text(self.operation_id, field_name="operation_id")
        _require_text(self.message, field_name="message")
        if self.pending_action is not None and not isinstance(
            self.pending_action, PendingActionPayload
        ):
            raise TypeError("pending_action must be a PendingActionPayload")


@dataclass(frozen=True, slots=True)
class RuntimeFailureOutcome:
    code: RuntimeFailureCode
    message: str = ""
    status_code: int = 500
    retryable: bool = False
    degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, RuntimeFailureCode):
            raise TypeError("code must be a RuntimeFailureCode")
        _require_text(self.message, field_name="message")
        _require_int(self.status_code, field_name="status_code")
        _require_bool(self.retryable, field_name="retryable")
        _require_bool(self.degraded, field_name="degraded")


@dataclass(frozen=True, slots=True)
class OperationPendingOutcome:
    operation_id: str
    conversation_id: int | None = None
    message: str = ""
    code: RuntimeFailureCode = RuntimeFailureCode.OPERATION_DELIVERY_PENDING
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.operation_id, field_name="operation_id", allow_empty=False)
        if self.conversation_id is not None:
            _require_int(self.conversation_id, field_name="conversation_id")
        _require_text(self.message, field_name="message")
        if not isinstance(self.code, RuntimeFailureCode):
            raise TypeError("code must be a RuntimeFailureCode")
        if self.retry_after_seconds is not None:
            _require_int(self.retry_after_seconds, field_name="retry_after_seconds")
            if self.retry_after_seconds < 0:
                raise ValueError("retry_after_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class OperationReplayOutcome:
    operation_id: str
    conversation_id: int | None = None
    message: str = ""
    status: str = "committed"
    write_status: str | None = None
    write_error: str | None = None
    undo: ImmutablePayload | None = None
    replayed: bool = True
    persisted: bool = True

    def __post_init__(self) -> None:
        _require_text(self.operation_id, field_name="operation_id", allow_empty=False)
        if self.conversation_id is not None:
            _require_int(self.conversation_id, field_name="conversation_id")
        _require_text(self.message, field_name="message")
        _require_text(self.status, field_name="status", allow_empty=False)
        if self.write_status is not None:
            _require_text(self.write_status, field_name="write_status")
        if self.write_error is not None:
            _require_text(self.write_error, field_name="write_error")
        if self.undo is not None:
            object.__setattr__(
                self,
                "undo",
                _require_immutable_mapping(self.undo, field_name="undo"),
            )
        _require_bool(self.replayed, field_name="replayed")
        _require_bool(self.persisted, field_name="persisted")


RuntimeOutcome: TypeAlias = (
    MessageOutcome
    | ConfirmationRequiredOutcome
    | RuntimeFailureOutcome
    | OperationPendingOutcome
    | OperationReplayOutcome
)


_MISSING_OPAQUE_STATE = object()


@dataclass(frozen=True, slots=True, init=False)
class PreparedStreamExecution:
    invocation_id: str | UUID
    preparation_kind: PreparationKind
    execution_mode: StreamExecutionMode
    opaque_state: object = field(repr=False)
    _lifecycle: PreparedLifecycle = field(
        default_factory=PreparedLifecycle,
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        invocation_id: str | UUID,
        preparation_kind: PreparationKind,
        execution_mode: StreamExecutionMode,
        opaque_state: object = _MISSING_OPAQUE_STATE,
        *,
        lifecycle: PreparedLifecycle | None = None,
        lifecycle_state: PreparedLifecycleState | None = None,
        completion_reason: CompletionReason | None = None,
    ) -> None:
        if opaque_state is _MISSING_OPAQUE_STATE:
            raise TypeError("opaque_state is required")
        if lifecycle is None:
            lifecycle = PreparedLifecycle(
                lifecycle_state or PreparedLifecycleState.PREPARED,
                completion_reason,
            )
        elif lifecycle_state is not None and lifecycle.state is not lifecycle_state:
            raise ValueError("lifecycle state does not match lifecycle_state")
        elif completion_reason is not None and lifecycle.completion_reason is not completion_reason:
            raise ValueError("lifecycle reason does not match completion_reason")
        object.__setattr__(self, "invocation_id", invocation_id)
        object.__setattr__(self, "preparation_kind", preparation_kind)
        object.__setattr__(self, "execution_mode", execution_mode)
        object.__setattr__(self, "opaque_state", opaque_state)
        object.__setattr__(self, "_lifecycle", lifecycle)
        self.__post_init__()

    def __post_init__(self) -> None:
        _reject_framework_value(self.opaque_state, field_name="opaque_state")
        if isinstance(self.opaque_state, Mapping):
            _validate_json_value(self.opaque_state, field_name="opaque_state")
        if not isinstance(self.invocation_id, (str, UUID)):
            raise TypeError("invocation_id must be a string or UUID")
        if isinstance(self.invocation_id, str) and not self.invocation_id:
            raise ValueError("invocation_id must not be empty")
        if not isinstance(self.preparation_kind, PreparationKind):
            raise TypeError("preparation_kind must be a PreparationKind")
        if not isinstance(self.execution_mode, StreamExecutionMode):
            raise TypeError("execution_mode must be a StreamExecutionMode")
        if not isinstance(self._lifecycle, PreparedLifecycle):
            raise TypeError("lifecycle must be a PreparedLifecycle")
        allowed_modes: dict[PreparationKind, frozenset[StreamExecutionMode]] = {
            PreparationKind.MODEL: frozenset({StreamExecutionMode.AGENT_HOST}),
            PreparationKind.DETERMINISTIC_INITIAL: frozenset({StreamExecutionMode.DIRECT}),
            PreparationKind.DETERMINISTIC_CONFIRMATION: frozenset({StreamExecutionMode.DIRECT}),
            PreparationKind.CONFIRMATION: frozenset(
                {StreamExecutionMode.DIRECT, StreamExecutionMode.AGENT_HOST}
            ),
            PreparationKind.REPLAY: frozenset({StreamExecutionMode.DIRECT}),
        }
        if self.execution_mode not in allowed_modes[self.preparation_kind]:
            raise ValueError("preparation kind and execution mode are incompatible")

    @property
    def lifecycle(self) -> PreparedLifecycle:
        return self._lifecycle

    @property
    def lifecycle_state(self) -> PreparedLifecycleState:
        return self._lifecycle.state

    @property
    def completion_reason(self) -> CompletionReason | None:
        return self._lifecycle.completion_reason

    def begin(self) -> bool:
        return self._lifecycle.begin()

    def abort_if_prepared(self) -> bool:
        return self._lifecycle.abort_if_prepared()

    def complete(self, reason: CompletionReason) -> bool:
        return self._lifecycle.complete(reason)


@dataclass(frozen=True, slots=True)
class FirstModelCompletedSignal:
    title_eligible: Literal[True] = True

    def __post_init__(self) -> None:
        if self.title_eligible is not True:
            raise ValueError("title_eligible is always true for this closed signal")


@dataclass(frozen=True, slots=True)
class MetaEvent:
    stream_version: StreamVersion = "pilot-sse-v1"
    supports_delta: bool = False
    supports_tool_events: bool = True
    supports_confirmation: bool = True

    def __post_init__(self) -> None:
        if self.stream_version != "pilot-sse-v1":
            raise ValueError("stream_version must be pilot-sse-v1")
        _require_bool(self.supports_delta, field_name="supports_delta")
        _require_bool(self.supports_tool_events, field_name="supports_tool_events")
        _require_bool(self.supports_confirmation, field_name="supports_confirmation")


@dataclass(frozen=True, slots=True)
class UserMessageSavedEvent:
    role: Literal["user"] = "user"

    def __post_init__(self) -> None:
        if self.role != "user":
            raise ValueError("role must be user")


@dataclass(frozen=True, slots=True)
class StatusEvent:
    phase: str
    label: str

    def __post_init__(self) -> None:
        _require_text(self.phase, field_name="phase", allow_empty=False)
        _require_text(self.label, field_name="label")


@dataclass(frozen=True, slots=True)
class AssistantDeltaEvent:
    delta: str

    def __post_init__(self) -> None:
        _require_text(self.delta, field_name="delta")


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    tool_call_id: str
    tool_name: str
    public_label: str = ""
    kind: Literal["read", "write"] = "read"
    confirm_mode: str = "none"
    summary: str = ""
    args_summary: JsonValue = field(default_factory=_empty_json_object, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, field_name="tool_call_id", allow_empty=False)
        _require_text(self.tool_name, field_name="tool_name", allow_empty=False)
        _require_text(self.public_label, field_name="public_label")
        if self.kind not in {"read", "write"}:
            raise ValueError("kind must be read or write")
        _require_text(self.confirm_mode, field_name="confirm_mode", allow_empty=False)
        _require_text(self.summary, field_name="summary")
        _validate_json_value(self.args_summary, field_name="args_summary")
        if isinstance(self.args_summary, Mapping):
            object.__setattr__(
                self,
                "args_summary",
                _require_immutable_mapping(self.args_summary, field_name="args_summary"),
            )


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    tool_call_id: str
    tool_name: str
    status: str
    summary: str
    evidence: tuple[ImmutablePayload, ...] = ()
    affected_resources: tuple[ImmutablePayload, ...] = ()
    changed_entities: tuple[ImmutablePayload, ...] = ()
    operation_id: str | None = None
    message: str = ""
    visible_result: str = ""
    write_status: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, field_name="tool_call_id", allow_empty=False)
        _require_text(self.tool_name, field_name="tool_name", allow_empty=False)
        _require_text(self.status, field_name="status", allow_empty=False)
        _require_text(self.summary, field_name="summary")
        object.__setattr__(
            self,
            "evidence",
            _require_payload_tuple(self.evidence, field_name="evidence"),
        )
        object.__setattr__(
            self,
            "affected_resources",
            _require_payload_tuple(self.affected_resources, field_name="affected_resources"),
        )
        object.__setattr__(
            self,
            "changed_entities",
            _require_payload_tuple(self.changed_entities, field_name="changed_entities"),
        )
        if self.operation_id is not None:
            _require_text(self.operation_id, field_name="operation_id")
        _require_text(self.message, field_name="message")
        _require_text(self.visible_result, field_name="visible_result")
        if self.write_status is not None:
            _require_text(self.write_status, field_name="write_status")


@dataclass(frozen=True, slots=True)
class ConfirmationRequiredEvent:
    confirmation_token: str = field(repr=False)
    operation_id: str | None = None
    pending_action: PendingActionPayload | None = None

    def __post_init__(self) -> None:
        _require_text(self.confirmation_token, field_name="confirmation_token", allow_empty=False)
        if self.operation_id is not None:
            _require_text(self.operation_id, field_name="operation_id")
        if self.pending_action is not None and not isinstance(
            self.pending_action, PendingActionPayload
        ):
            raise TypeError("pending_action must be a PendingActionPayload")


@dataclass(frozen=True, slots=True)
class AssistantMessageEvent:
    message: str

    def __post_init__(self) -> None:
        _require_text(self.message, field_name="message")


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: RuntimeFailureCode
    message: str
    retryable: bool = False
    degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, RuntimeFailureCode):
            raise TypeError("code must be a RuntimeFailureCode")
        _require_text(self.message, field_name="message")
        _require_bool(self.retryable, field_name="retryable")
        _require_bool(self.degraded, field_name="degraded")


@dataclass(frozen=True, slots=True)
class CompletedEvent:
    response: RuntimeOutcome | None = None
    persisted: bool = True

    def __post_init__(self) -> None:
        if self.response is not None and not isinstance(
            self.response,
            (
                MessageOutcome,
                ConfirmationRequiredOutcome,
                RuntimeFailureOutcome,
                OperationPendingOutcome,
                OperationReplayOutcome,
            ),
        ):
            raise TypeError("response must be a RuntimeOutcome")
        _require_bool(self.persisted, field_name="persisted")


RuntimeEvent: TypeAlias = (
    MetaEvent
    | UserMessageSavedEvent
    | StatusEvent
    | AssistantDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | ConfirmationRequiredEvent
    | AssistantMessageEvent
    | ErrorEvent
    | CompletedEvent
)


@runtime_checkable
class RuntimeEventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...


@runtime_checkable
class RuntimeSignalSink(Protocol):
    def try_emit(self, signal: FirstModelCompletedSignal) -> SignalEmitResult: ...


ResultT = TypeVar("ResultT")
AgentThunk: TypeAlias = Callable[[], ResultT]


@runtime_checkable
class RuntimeInvocationControl(Protocol):
    @property
    def state(self) -> InvocationState: ...

    @property
    def cancel_reason(self) -> CancelReason | None: ...

    def request_cancel(self, reason: CancelReason) -> bool: ...

    def request_timeout(self) -> bool: ...

    def mark_completed(self) -> bool: ...

    def is_active(self) -> bool: ...


@runtime_checkable
class AgentExecutionHost(Protocol[ResultT]):
    def run(
        self,
        thunk: AgentThunk[ResultT],
        invocation_control: RuntimeInvocationControl,
    ) -> ResultT: ...


__all__ = [
    "AgentExecutionHost",
    "AgentThunk",
    "AssistantDeltaEvent",
    "AssistantMessageEvent",
    "AttachmentReference",
    "CancelReason",
    "CompletedEvent",
    "CompletionReason",
    "ConfirmationRequest",
    "ConfirmationRequiredEvent",
    "ConfirmationRequiredOutcome",
    "EditedArgs",
    "ErrorEvent",
    "FirstModelCompletedSignal",
    "ImmediateHttpOutcome",
    "ImmutablePayload",
    "InvocationState",
    "JsonScalar",
    "JsonValue",
    "MISSING_EDITED_ARGS",
    "MessageOutcome",
    "MetaEvent",
    "OperationPendingOutcome",
    "OperationReplayOutcome",
    "PendingActionPayload",
    "PilotActionDescriptor",
    "PreparationKind",
    "PreparedLifecycle",
    "PreparedLifecycleState",
    "PreparedStreamExecution",
    "RuntimeEvent",
    "RuntimeEventSink",
    "RuntimeFailureOutcome",
    "RuntimeFailureCode",
    "RuntimeInvocationControl",
    "RuntimeOutcome",
    "RuntimeSignalSink",
    "RuntimeTransportContext",
    "SignalEmitResult",
    "StartTurnRequest",
    "StatusEvent",
    "StreamVersion",
    "StreamExecutionMode",
    "ToolCallEvent",
    "ToolResultEvent",
    "UserMessageSavedEvent",
]
