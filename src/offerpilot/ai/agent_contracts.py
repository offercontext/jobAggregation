from __future__ import annotations

from offerpilot.ai.control import AgentLoopControlError, ChatRunCancelled

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Protocol, TYPE_CHECKING, TypeAlias, cast

from offerpilot.ai.tool_runtime.contracts import (
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    TransientToolRuntimeValue,
)
from offerpilot.ai.types import Message

if TYPE_CHECKING:
    from offerpilot.ai.agent_loop import AgentLoopInvocation


# Keep the established import surface while the dependency-free control
# exceptions live outside the tool-runtime package to avoid import cycles.
__all__ = ["AgentLoopControlError", "ChatRunCancelled"]


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
AssistantDeltaSink: TypeAlias = Callable[[str], None]
CancelCheck: TypeAlias = Callable[[], bool]


class AgentRuntimeSignalSink(Protocol):
    """Minimal signal seam shared by Runtime and Context Projector sinks."""

    def try_emit(self, signal: str) -> object: ...


def _require_text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _freeze_json(value: object, field_name: str) -> JsonValue:
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonValue, value)
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{field_name} must be finite")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError(f"{field_name} keys must be strings")
            copied[key] = _freeze_json(child, f"{field_name}.{key}")
        return MappingProxyType(copied)
    if type(value) in {tuple, list}:
        return tuple(
            _freeze_json(child, f"{field_name}[{index}]")
            for index, child in enumerate(cast(tuple[object, ...] | list[object], value))
        )
    raise TypeError(f"{field_name} contains a non-JSON value")


def _freeze_payload(value: Mapping[str, object], field_name: str) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return frozen


class _TransientAsdictGuard:
    """Sentinel which makes ``dataclasses.asdict`` fail closed.

    ``asdict`` bypasses ``__reduce_ex__``/``__getstate__`` and recursively
    copies dataclass fields.  Keeping a private, non-init sentinel in the
    transient DTOs closes that otherwise easy-to-miss serialization path.
    """

    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise TypeError("transient tool runtime value cannot be serialized")


_ASDICT_GUARD = _TransientAsdictGuard()


class StalePendingActionError(ValueError):
    """Raised when confirmation authority no longer owns the Pending action."""


class PendingActionValidationError(ValueError):
    """Raised when an approved Pending action fails before execution starts."""


class ChatModel(Protocol):
    def complete(
        self,
        messages: list[Message],
        tools: list[Any],
        response_format: dict[str, Any] | None = None,
    ) -> Any: ...


class StreamingChatModel(Protocol):
    def stream_complete(
        self,
        messages: list[Message],
        tools: list[Any],
        on_delta: AssistantDeltaSink,
    ) -> Any: ...


@dataclass(repr=False)
class PendingAction(TransientToolRuntimeValue):
    tool_call_id: str
    tool_name: str
    args: str
    human: str
    operation_id: str = ""
    _serialization_guard: _TransientAsdictGuard = field(
        default=_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        # Detached replay and validation probes may carry an intentionally
        # empty field; the Seed/Runtime ownership boundary validates identity
        # completeness before execution or persistence.  This DTO boundary
        # only rejects non-text private/runtime values.
        _require_text(self.tool_call_id, "tool_call_id", allow_empty=True)
        _require_text(self.tool_name, "tool_name", allow_empty=True)
        _require_text(self.args, "args", allow_empty=True)
        _require_text(self.human, "human", allow_empty=True)
        _require_text(self.operation_id, "operation_id", allow_empty=True)

    def __repr__(self) -> str:
        return "<PendingAction transient>"


@dataclass(frozen=True, repr=False)
class AgentTurnResult(TransientToolRuntimeValue):
    added: list[Message]
    reply: str
    pending: PendingAction | None
    records: tuple[ToolExecutionRecord[Any, Any], ...] = ()
    failures: tuple[ToolFailure, ...] = ()
    _serialization_guard: _TransientAsdictGuard = field(
        default=_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.added) is not list:
            raise TypeError("AgentTurnResult added must be a list")
        if not all(isinstance(message, Message) for message in self.added):
            raise TypeError("AgentTurnResult added must contain Message values")
        _require_text(self.reply, "reply", allow_empty=True)
        if self.pending is not None and not isinstance(self.pending, PendingAction):
            raise TypeError("AgentTurnResult pending must be PendingAction or None")
        if type(self.records) is not tuple or type(self.failures) is not tuple:
            raise TypeError("AgentTurnResult records/failures must be tuples")

    def __repr__(self) -> str:
        return "<AgentTurnResult transient>"

    def __iter__(self) -> Iterator[Any]:
        yield self.added
        yield self.reply
        yield self.pending


@dataclass(frozen=True, slots=True, repr=False)
class AgentAssistantDelta(TransientToolRuntimeValue):
    delta: str = field(repr=False)
    _serialization_guard: _TransientAsdictGuard = field(
        default=_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _require_text(self.delta, "delta")

    def __repr__(self) -> str:
        return "<AgentAssistantDelta transient>"


@dataclass(frozen=True, slots=True, repr=False)
class AgentToolCall(TransientToolRuntimeValue):
    tool_call_id: str
    tool_name: str
    public_label: str
    kind: Literal["read", "write"]
    confirm_mode: Literal["none", "hitl", "approved"]
    summary: str
    args_summary: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")
        _require_text(self.public_label, "public_label")
        _require_text(self.summary, "summary", allow_empty=True)
        if self.kind not in {"read", "write"}:
            raise ValueError("kind must be read or write")
        if self.confirm_mode not in {"none", "hitl", "approved"}:
            raise ValueError("confirm_mode is invalid")
        object.__setattr__(
            self,
            "args_summary",
            _freeze_payload(cast(Mapping[str, object], self.args_summary), "args_summary"),
        )

    def __repr__(self) -> str:
        return "<AgentToolCall transient>"


@dataclass(frozen=True, slots=True, repr=False)
class AgentToolResult(TransientToolRuntimeValue):
    tool_call_id: str
    operation_id: str
    payload: Mapping[str, JsonValue] = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.operation_id, "operation_id", allow_empty=True)
        object.__setattr__(
            self,
            "payload",
            _freeze_payload(cast(Mapping[str, object], self.payload), "payload"),
        )

    def __repr__(self) -> str:
        return "<AgentToolResult transient>"


AgentLoopEvent: TypeAlias = AgentAssistantDelta | AgentToolCall | AgentToolResult


class AgentEventSink(Protocol):
    def emit(self, event: AgentLoopEvent) -> None: ...


class ApprovedWriteContinuation(Protocol):
    @property
    def pending(self) -> PendingAction: ...

    def claim(
        self,
        pending: PendingAction,
        prepared: PreparedToolCall[Any, Any],
    ) -> ToolFailure | None: ...

    def record_result(
        self,
        pending: PendingAction,
        tool_message: Message,
        record: ToolExecutionRecord[Any, Any],
    ) -> None: ...

    def load_continuation_messages(self) -> tuple[Message, ...]: ...

    def delivery_fence(self) -> bool: ...


class AgentDriver(Protocol):
    def execute(self, invocation: AgentLoopInvocation) -> AgentTurnResult: ...
