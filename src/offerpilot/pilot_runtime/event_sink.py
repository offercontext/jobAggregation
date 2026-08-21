"""Transport-independent runtime event and control-flow boundaries.

The runtime emits typed events and raises closed control exceptions.  This
module deliberately contains no HTTP, SSE, database, or provider concerns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock, RLock, get_ident
from typing import Final, cast

from .contracts import (
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CancelReason,
    CompletedEvent,
    ConfirmationRequiredEvent,
    ErrorEvent,
    FirstModelCompletedSignal,
    InvocationState,
    MetaEvent,
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeInvocationControl,
    RuntimeSignalSink,
    SignalEmitResult,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageSavedEvent,
)
from .errors import RuntimeAgentTimedOut, RuntimeCancelled, RuntimeTransportAborted


_EVENT_TYPES: Final[tuple[type[object], ...]] = (
    MetaEvent,
    UserMessageSavedEvent,
    StatusEvent,
    AssistantDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    ConfirmationRequiredEvent,
    AssistantMessageEvent,
    ErrorEvent,
    CompletedEvent,
)
_CONTROL_ERRORS: Final[tuple[type[Exception], ...]] = (
    RuntimeCancelled,
    RuntimeTransportAborted,
    RuntimeAgentTimedOut,
)


def _plain_json(value: object) -> object:
    """Convert an already-validated immutable JSON value to a plain snapshot."""

    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    return value


def _optional(payload: dict[str, object], key: str, value: object | None) -> None:
    if value is not None and value != "":
        payload[key] = _plain_json(value)


def _pending_action_payload(value: object) -> object:
    as_mapping = getattr(value, "as_mapping", None)
    if not callable(as_mapping):
        raise TypeError("pending_action must be a PendingActionPayload")
    return _plain_json(cast(Mapping[str, object], as_mapping()))


def runtime_event_payload(event: RuntimeEvent) -> dict[str, object]:
    """Return the closed, JSON-safe payload for one typed runtime event.

    This is intentionally an explicit mapping rather than ``asdict``: runtime
    events are a closed union and no arbitrary object or dictionary is accepted
    at this boundary.
    """

    if type(event) not in _EVENT_TYPES:
        raise TypeError("event must be a typed RuntimeEvent")
    if isinstance(event, MetaEvent):
        return {
            "stream_version": event.stream_version,
            "supports_delta": event.supports_delta,
            "supports_tool_events": event.supports_tool_events,
            "supports_confirmation": event.supports_confirmation,
        }
    if isinstance(event, UserMessageSavedEvent):
        return {"role": "user"}
    if isinstance(event, StatusEvent):
        return {"phase": event.phase, "label": event.label}
    if isinstance(event, AssistantDeltaEvent):
        return {"delta": event.delta}
    if isinstance(event, ToolCallEvent):
        return {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "public_label": event.public_label,
            "kind": event.kind,
            "confirm_mode": event.confirm_mode,
            "summary": event.summary,
            "args_summary": _plain_json(event.args_summary),
        }
    if isinstance(event, ToolResultEvent):
        payload: dict[str, object] = {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "status": event.status,
            "summary": event.summary,
            "evidence": _plain_json(event.evidence),
            "affected_resources": _plain_json(event.affected_resources),
            "changed_entities": _plain_json(event.changed_entities),
        }
        if event.message:
            payload["message"] = event.message
        if event.visible_result:
            payload["visible_result"] = event.visible_result
        _optional(payload, "operation_id", event.operation_id)
        _optional(payload, "write_status", event.write_status)
        return payload
    if isinstance(event, ConfirmationRequiredEvent):
        payload = {}
        _optional(payload, "operation_id", event.operation_id)
        if event.pending_action is not None:
            payload["pending_action"] = _pending_action_payload(event.pending_action)
        return payload
    if isinstance(event, AssistantMessageEvent):
        return {"message": event.message}
    if isinstance(event, ErrorEvent):
        return {
            "code": event.code.value,
            "message": event.message,
            "retryable": event.retryable,
            "degraded": event.degraded,
        }
    if isinstance(event, CompletedEvent):
        payload = {"persisted": event.persisted}
        if event.response is not None:
            payload["response"] = _plain_json(runtime_outcome_payload(event.response))
        else:
            payload["response"] = None
        return payload
    # The isinstance check above makes this unreachable while keeping the
    # function closed if the union is extended without an explicit mapping.
    raise TypeError("unsupported RuntimeEvent")


def runtime_outcome_payload(outcome: object) -> dict[str, object]:
    """Project an outcome for use inside a completed event.

    The transport-facing implementation lives in :mod:`offerpilot.chat_transport`;
    this small private-boundary helper avoids importing that module here.
    """

    from .contracts import (
        ConfirmationRequiredOutcome,
        MessageOutcome,
        OperationPendingOutcome,
        OperationReplayOutcome,
        RuntimeFailureOutcome,
    )

    if type(outcome) is MessageOutcome:
        payload: dict[str, object] = {
            "type": "message",
            "message": outcome.message,
        }
        _optional(payload, "conversation_id", outcome.conversation_id)
        _optional(payload, "write_status", outcome.write_status)
        _optional(payload, "write_error", outcome.write_error)
        _optional(payload, "undo", outcome.undo)
        _optional(payload, "operation_id", outcome.operation_id)
        if outcome.replayed:
            payload["replayed"] = True
        return payload
    if type(outcome) is ConfirmationRequiredOutcome:
        payload = {"type": "confirmation_required"}
        _optional(payload, "conversation_id", outcome.conversation_id)
        _optional(payload, "operation_id", outcome.operation_id)
        if outcome.message:
            payload["message"] = outcome.message
        if outcome.pending_action is not None:
            payload["pending_action"] = _pending_action_payload(outcome.pending_action)
        if outcome.replayed:
            payload["replayed"] = True
        return payload
    if type(outcome) is RuntimeFailureOutcome:
        return {
            "error_code": outcome.code.value,
            "error": outcome.message,
            "retryable": outcome.retryable,
            "degraded": outcome.degraded,
        }
    if type(outcome) is OperationPendingOutcome:
        payload = {
            "type": "operation_pending",
            "operation_id": outcome.operation_id,
            "message": outcome.message,
            "error_code": outcome.code.value,
        }
        _optional(payload, "conversation_id", outcome.conversation_id)
        _optional(payload, "retry_after_seconds", outcome.retry_after_seconds)
        return payload
    if type(outcome) is OperationReplayOutcome:
        payload = {
            "type": "message",
            "operation_id": outcome.operation_id,
            "message": outcome.message,
            "replayed": outcome.replayed,
        }
        _optional(payload, "conversation_id", outcome.conversation_id)
        _optional(payload, "write_status", outcome.write_status)
        _optional(payload, "write_error", outcome.write_error)
        _optional(payload, "undo", outcome.undo)
        return payload
    raise TypeError("outcome must be a typed RuntimeOutcome")


class CallableRuntimeEventSink:
    """Adapt a callable to ``RuntimeEventSink`` with terminal failure state."""

    __slots__ = ("_callback", "_failed", "_lock")

    def __init__(self, callback: Callable[[RuntimeEvent], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback
        self._failed = False
        self._lock = Lock()

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def emit(self, event: RuntimeEvent) -> None:
        if type(event) not in _EVENT_TYPES:
            raise TypeError("event must be a typed RuntimeEvent")
        with self._lock:
            if self._failed:
                raise RuntimeTransportAborted()
        try:
            self._callback(event)
        except _CONTROL_ERRORS:
            with self._lock:
                self._failed = True
            raise
        except Exception:
            with self._lock:
                self._failed = True
            raise


def emit_runtime_event(sink: RuntimeEventSink, event: RuntimeEvent) -> None:
    """Emit an event, preserving control exceptions and closing on sink errors."""

    if type(event) not in _EVENT_TYPES:
        raise TypeError("event must be a typed RuntimeEvent")
    try:
        sink.emit(event)
    except _CONTROL_ERRORS:
        raise
    except Exception as exc:
        raise RuntimeTransportAborted() from exc


class InMemoryRuntimeInvocationControl:
    """Atomic, in-memory invocation state used by Runtime and transport hosts."""

    __slots__ = ("_state", "_cancel_reason", "_lock", "_fence_owner")

    def __init__(self) -> None:
        self._state = InvocationState.ACTIVE
        self._cancel_reason: CancelReason | None = None
        self._lock = RLock()
        self._fence_owner: int | None = None

    @property
    def state(self) -> InvocationState:
        with self._lock:
            return self._state

    @property
    def cancel_reason(self) -> CancelReason | None:
        with self._lock:
            return self._cancel_reason

    def request_cancel(self, reason: CancelReason) -> bool:
        if not isinstance(reason, CancelReason):
            raise TypeError("reason must be a CancelReason")
        with self._lock:
            if self._fence_owner == get_ident():
                raise RuntimeError("control mutation is not allowed inside a commit fence")
            if self._state is not InvocationState.ACTIVE:
                return False
            object.__setattr__(self, "_state", InvocationState.CANCELLED)
            object.__setattr__(self, "_cancel_reason", reason)
            return True

    def request_timeout(self) -> bool:
        with self._lock:
            if self._fence_owner == get_ident():
                raise RuntimeError("control mutation is not allowed inside a commit fence")
            if self._state is not InvocationState.ACTIVE:
                return False
            object.__setattr__(self, "_state", InvocationState.TIMED_OUT)
            object.__setattr__(self, "_cancel_reason", CancelReason.DEADLINE)
            return True

    def mark_completed(self) -> bool:
        with self._lock:
            if self._fence_owner == get_ident():
                raise RuntimeError("control mutation is not allowed inside a commit fence")
            if self._state is not InvocationState.ACTIVE:
                return False
            object.__setattr__(self, "_state", InvocationState.COMPLETED)
            return True

    def is_active(self) -> bool:
        with self._lock:
            return self._state is InvocationState.ACTIVE

    def run_if_active(
        self,
        action: Callable[[], object],
        *,
        allow_timeout: bool = False,
    ) -> tuple[bool, object | None]:
        """Linearize one persistence commit against cancellation.

        The control lock remains held while ``action`` executes.  Therefore a
        request_cancel/request_timeout from another thread waits for a commit
        that already won the fence, while a request that won first prevents the
        callback from running.  Commit callbacks must not mutate this control;
        same-thread mutation is rejected explicitly to avoid re-entrant
        deadlocks and ambiguous winner semantics.
        """

        if not callable(action):
            raise TypeError("action must be callable")
        with self._lock:
            if self._fence_owner is not None:
                raise RuntimeError("commit fences may not be re-entered")
            if self._state is not InvocationState.ACTIVE and not (
                allow_timeout and self._state is InvocationState.TIMED_OUT
            ):
                return False, None
            self._fence_owner = get_ident()
            try:
                return True, action()
            finally:
                self._fence_owner = None


def require_runtime_active(control: RuntimeInvocationControl) -> None:
    """Raise the closed control marker for a non-active invocation.

    A completed invocation is a terminal success and is therefore safe for
    idempotent post-processing.  Cancellation and timeout are control flow,
    never product outcomes.
    """

    state = control.state
    if state is InvocationState.ACTIVE or state is InvocationState.COMPLETED:
        return
    if state is InvocationState.TIMED_OUT:
        raise RuntimeAgentTimedOut()
    if state is InvocationState.CANCELLED:
        if control.cancel_reason is CancelReason.TRANSPORT_ABORTED:
            raise RuntimeTransportAborted()
        raise RuntimeCancelled(control.cancel_reason)
    raise RuntimeTransportAborted()


class RuntimeSignalLatch:
    """Capacity-one, non-blocking, fail-open first-model signal latch.

    ``register`` is called at most once by ``finalize``.  It is intentionally
    outside the runtime event/outcome path: a registration failure only marks
    the title signal as degraded and cannot alter the runtime result.
    """

    __slots__ = (
        "_signal",
        "_emitted",
        "_closed",
        "_degraded",
        "_finalized",
        "_register",
        "_sink",
        "_lock",
    )

    def __init__(
        self,
        register: Callable[[FirstModelCompletedSignal], None] | None = None,
        *,
        sink: Callable[[FirstModelCompletedSignal], object]
        | RuntimeSignalSink[FirstModelCompletedSignal]
        | None = None,
        on_signal: Callable[[FirstModelCompletedSignal], object] | None = None,
    ) -> None:
        if register is not None and not callable(register):
            raise TypeError("register must be callable")
        if sink is not None and on_signal is not None:
            raise TypeError("provide only one signal sink")
        self._signal: FirstModelCompletedSignal | None = None
        self._emitted = False
        self._closed = False
        self._degraded = False
        self._finalized = False
        self._register = register
        self._sink = sink if sink is not None else on_signal
        self._lock = Lock()

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self._degraded

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def try_emit(self, signal: FirstModelCompletedSignal) -> SignalEmitResult:
        if type(signal) is not FirstModelCompletedSignal:
            raise TypeError("signal must be a FirstModelCompletedSignal")
        with self._lock:
            if self._closed:
                return SignalEmitResult.CLOSED
            if self._degraded:
                return SignalEmitResult.DEGRADED
            if self._emitted:
                if self._signal is not None and signal is not self._signal:
                    return SignalEmitResult.FULL
                return SignalEmitResult.DUPLICATE
            self._emitted = True
            self._signal = signal
        return SignalEmitResult.EMITTED

    def drain(self) -> FirstModelCompletedSignal | None:
        """Consume the signal through the sole drain owner path.

        ``drain`` and ``finalize`` are mutually exclusive owner operations.
        Once this method consumes a pending signal, a later ``finalize`` is a
        closed/no-signal result and never registers that signal.
        """

        with self._lock:
            if self._closed:
                self._signal = None
                return None
            signal = self._signal
            self._signal = None
            return signal

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._signal = None

    def consumer_exit(self) -> None:
        self.close()

    def finalize(self) -> SignalEmitResult:
        """Consume and register through the sole finalizer owner path.

        Composition code must choose ``finalize`` rather than ``drain`` when
        registration is required.  If ``drain`` already owns consumption,
        this method is idempotently closed and performs no registration.
        """

        with self._lock:
            if self._finalized:
                return SignalEmitResult.CLOSED
            self._finalized = True
            self._closed = True
            signal = self._signal
            self._signal = None
            sink = self._sink
            register = self._register
        if signal is None:
            return SignalEmitResult.CLOSED
        degraded = False
        if sink is not None:
            try:
                result = sink.try_emit(signal) if hasattr(sink, "try_emit") else sink(signal)
                if isinstance(result, SignalEmitResult) and result is SignalEmitResult.DEGRADED:
                    degraded = True
            except Exception:
                degraded = True
        if register is not None:
            try:
                register(signal)
            except Exception:
                degraded = True
        if degraded:
            with self._lock:
                self._degraded = True
            return SignalEmitResult.DEGRADED
        return SignalEmitResult.EMITTED

    # The owner uses ``drain_and_close`` when it wants an explicit, named
    # finalizer; retaining ``finalize`` keeps the operation idempotent.
    def drain_and_close(self) -> SignalEmitResult:
        return self.finalize()

    def mark_degraded(self) -> SignalEmitResult:
        with self._lock:
            self._degraded = True
            self._signal = None
        return SignalEmitResult.DEGRADED


class ClosedAgentSignalSink(RuntimeSignalSink[str]):
    """Adapt the one legacy Agent title string to the typed signal boundary.

    The adapter is deliberately closed: only the historical
    ``first_complete_agent_response`` marker is accepted.  It stores one
    typed signal instance, never the legacy marker or model text, and all
    capacity/closed/duplicate behavior remains owned by ``RuntimeSignalLatch``.
    """

    __slots__ = ("_latch", "_signal")

    _LEGACY_MARKER: Final[str] = "first_complete_agent_response"

    def __init__(self, latch: RuntimeSignalLatch) -> None:
        if type(latch) is not RuntimeSignalLatch:
            raise TypeError("latch must be a RuntimeSignalLatch")
        self._latch = latch
        self._signal = FirstModelCompletedSignal()

    def try_emit(self, signal: str) -> SignalEmitResult:
        if type(signal) is not str or signal != self._LEGACY_MARKER:
            return SignalEmitResult.DEGRADED
        return self._latch.try_emit(self._signal)


InMemoryRuntimeSignalLatch = RuntimeSignalLatch
safe_emit_runtime_event = emit_runtime_event
require_active = require_runtime_active


__all__ = [
    "CallableRuntimeEventSink",
    "ClosedAgentSignalSink",
    "InMemoryRuntimeInvocationControl",
    "InMemoryRuntimeSignalLatch",
    "RuntimeSignalLatch",
    "emit_runtime_event",
    "require_active",
    "require_runtime_active",
    "runtime_event_payload",
    "runtime_outcome_payload",
    "safe_emit_runtime_event",
]
