"""FastAPI/Starlette transport adapters for Pilot Runtime.

Runtime code deals only in typed outcomes/events.  This module is the sole
place where those values become HTTP or SSE responses and where a prepared
stream's response lifecycle is owned.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from math import isfinite
from queue import Empty, Queue
from threading import Event, Lock
from time import perf_counter
from typing import Any, Final, Generic, Iterator, NoReturn, TypeAlias, TypeVar, cast

from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from offerpilot.pilot_runtime.contracts import (
    AgentThunk,
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CancelReason,
    CompletionReason,
    CompletedEvent,
    ConfirmationRequiredEvent,
    ConfirmationRequiredOutcome,
    ErrorEvent,
    ImmediateHttpOutcome,
    MessageOutcome,
    MetaEvent,
    OperationPendingOutcome,
    OperationReplayOutcome,
    PreparedLifecycle,
    PreparedLifecycleState,
    PreparedStreamExecution,
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeFailureOutcome,
    RuntimeInvocationControl,
    RuntimeOutcome,
    InvocationState,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageSavedEvent,
)
from offerpilot.pilot_runtime.errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.event_sink import runtime_event_payload, runtime_outcome_payload


Content: TypeAlias = Iterable[bytes | str] | AsyncIterable[bytes | str]
CleanupCallback: TypeAlias = Callable[[CompletionReason | None], object]
CHAT_AGENT_TIMEOUT_SECONDS: Final[float] = 120.0
SSE_POLL_SECONDS: Final[float] = 0.1
_ResultT = TypeVar("_ResultT")
_EVENT_NAMES: Final[dict[type[object], str]] = {
    MetaEvent: "meta",
    UserMessageSavedEvent: "user_message_saved",
    StatusEvent: "status",
    AssistantDeltaEvent: "assistant_delta",
    ToolCallEvent: "tool_call",
    ToolResultEvent: "tool_result",
    ConfirmationRequiredEvent: "confirmation_required",
    AssistantMessageEvent: "assistant_message",
    ErrorEvent: "error",
    CompletedEvent: "completed",
}


def _host_timeout_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("timeout_seconds must be a number")
    timeout = float(value)
    if timeout < 0 or timeout != timeout or timeout == float("inf"):
        raise ValueError("timeout_seconds must be a finite non-negative number")
    return timeout


def _raise_control_terminal(control: RuntimeInvocationControl) -> NoReturn:
    state = control.state
    if state is InvocationState.TIMED_OUT:
        raise RuntimeAgentTimedOut()
    if state is InvocationState.CANCELLED:
        if control.cancel_reason is CancelReason.TRANSPORT_ABORTED:
            raise RuntimeTransportAborted()
        raise RuntimeCancelled(control.cancel_reason)
    raise RuntimeTransportAborted()


def _record_control_exception(
    control: RuntimeInvocationControl,
    exc: BaseException,
) -> None:
    """Keep control state useful when a thunk raises a control marker itself."""

    if isinstance(exc, RuntimeAgentTimedOut):
        control.request_timeout()
    elif isinstance(exc, RuntimeCancelled):
        control.request_cancel(control.cancel_reason or CancelReason.EXPLICIT_CANCEL)
    elif isinstance(exc, RuntimeTransportAborted):
        control.request_cancel(CancelReason.TRANSPORT_ABORTED)


def _require_host_active(control: RuntimeInvocationControl) -> None:
    if not control.is_active():
        _raise_control_terminal(control)


class SyncAgentExecutionHost(Generic[_ResultT]):
    """Run one Runtime thunk with the bounded, non-joining Agent deadline."""

    __slots__ = ("timeout_seconds", "_started", "_lock")

    def __init__(self, timeout_seconds: float = CHAT_AGENT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = _host_timeout_seconds(timeout_seconds)
        self._started = False
        self._lock = Lock()

    def run(
        self,
        thunk: AgentThunk[_ResultT],
        invocation_control: RuntimeInvocationControl,
    ) -> _ResultT:
        if not callable(thunk):
            raise TypeError("thunk must be callable")
        with self._lock:
            if self._started:
                raise RuntimeTransportAborted()
            self._started = True
        _require_host_active(invocation_control)

        executor = ThreadPoolExecutor(max_workers=1)
        future: Future[_ResultT] = executor.submit(thunk)
        cancel_futures = False
        try:
            try:
                result = future.result(timeout=self.timeout_seconds)
            except FutureTimeoutError as exc:
                cancel_futures = True
                future.cancel()
                if invocation_control.request_timeout():
                    raise RuntimeAgentTimedOut() from exc
                _raise_control_terminal(invocation_control)
            except BaseException as exc:
                cancel_futures = True
                _record_control_exception(invocation_control, exc)
                raise

            if invocation_control.is_active():
                return result
            cancel_futures = True
            _raise_control_terminal(invocation_control)
        finally:
            if cancel_futures:
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=False)


class _QueueRuntimeEventSink:
    """Typed, unbounded, non-blocking event sink owned by one SSE host."""

    __slots__ = ("_queue", "_cancel_event", "_control")

    def __init__(
        self,
        event_queue: Queue[RuntimeEvent | object],
        cancel_event: Event,
        control: RuntimeInvocationControl,
    ) -> None:
        self._queue = event_queue
        self._cancel_event = cancel_event
        self._control = control

    def emit(self, event: RuntimeEvent) -> None:
        if type(event) not in _EVENT_NAMES:
            raise TypeError("event must be a typed RuntimeEvent")
        if self._cancel_event.is_set() or not self._control.is_active():
            return
        self._queue.put_nowait(event)

    def __call__(self, event: RuntimeEvent) -> None:
        self.emit(event)


def _invoke_sse_thunk(
    thunk: Callable[..., _ResultT],
    sink: RuntimeEventSink,
    cancel_check: Callable[[], bool],
) -> _ResultT:
    """Call a typed Runtime thunk without catching errors from its body.

    Runtime implementations may use a zero-argument closure, a typed event
    sink, or the legacy two-callback shape while the transport migration is in
    progress.  Required positional parameters select the one supported shape;
    optional parameters are left at their defaults.  Signature inspection
    keeps a thunk's own ``TypeError`` intact and avoids guessing.
    """

    try:
        signature = inspect.signature(thunk)
    except (TypeError, ValueError):
        raise TypeError("SSE thunk signature is unsupported")

    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    has_varkw = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    required_keyword_only = any(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
    if has_varargs or has_varkw or required_keyword_only:
        raise TypeError("SSE thunk signature is unsupported")

    required_positional = [
        parameter
        for parameter in positional
        if parameter.default is inspect.Parameter.empty
    ]
    if len(required_positional) == 0:
        return thunk()
    if len(required_positional) == 1:
        return thunk(sink)
    if len(required_positional) == 2:
        return thunk(sink, cancel_check)
    raise TypeError("SSE thunk signature is unsupported")


class _SseInvocationIterator(Generic[_ResultT], Iterator[RuntimeEvent]):
    """One started SSE invocation; StopIteration.value is the typed result."""

    __slots__ = (
        "event_queue",
        "poll_seconds",
        "cancel_event",
        "_control",
        "_executor",
        "_future",
        "_sentinel",
        "_deadline",
        "_closed",
        "_result",
        "_result_set",
        "_shutdown_called",
    )

    def __init__(
        self,
        thunk: Callable[..., _ResultT],
        control: RuntimeInvocationControl,
        *,
        timeout_seconds: float,
        poll_seconds: float,
        executor_factory: Callable[..., ThreadPoolExecutor],
    ) -> None:
        self.event_queue: Queue[RuntimeEvent | object] = Queue()
        self.poll_seconds = poll_seconds
        self.cancel_event = Event()
        self._control = control
        self._sentinel = object()
        self._closed = False
        self._result: _ResultT | None = None
        self._result_set = False
        self._shutdown_called = False
        self._executor = executor_factory(max_workers=1)
        sink = _QueueRuntimeEventSink(self.event_queue, self.cancel_event, control)
        self._future: Future[_ResultT] = self._executor.submit(
            _invoke_sse_thunk,
            thunk,
            sink,
            self.cancel_event.is_set,
        )
        self._deadline = perf_counter() + timeout_seconds

        def on_done(_future: Future[_ResultT]) -> None:
            self.event_queue.put_nowait(self._sentinel)

        self._future.add_done_callback(on_done)

    def _deadline_expired(self) -> bool:
        return perf_counter() >= self._deadline

    def _finish(self) -> None:
        try:
            result = self._future.result()
        except BaseException as exc:
            _record_control_exception(self._control, exc)
            raise
        if not self._control.is_active():
            _raise_control_terminal(self._control)
        self._result = result
        self._result_set = True

    def _timeout(self, cause: BaseException | None = None) -> None:
        self._future.cancel()
        if self._control.request_timeout():
            if cause is None:
                raise RuntimeAgentTimedOut()
            raise RuntimeAgentTimedOut() from cause
        _raise_control_terminal(self._control)

    def _shutdown(self, *, cancel_futures: bool) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self._executor.shutdown(wait=False, cancel_futures=cancel_futures)

    def __next__(self) -> RuntimeEvent:
        if self._closed:
            raise StopIteration(self._result if self._result_set else None)
        completed = False
        cancel_futures = True
        try:
            while True:
                if not self._control.is_active():
                    self._future.cancel()
                    _raise_control_terminal(self._control)
                try:
                    item = self.event_queue.get(timeout=self.poll_seconds)
                except Empty as exc:
                    if not self._control.is_active():
                        self._future.cancel()
                        _raise_control_terminal(self._control)
                    if self._deadline_expired():
                        self._timeout(exc)
                    continue
                if item is self._sentinel:
                    # The sentinel proves the sole worker is done.  Lock the
                    # normal-completion cleanup policy before observing its
                    # result, including when that result is an exception.
                    cancel_futures = False
                    self._finish()
                    self._closed = True
                    self.cancel_event.set()
                    self._shutdown(cancel_futures=False)
                    completed = True
                    break
                if type(item) not in _EVENT_NAMES:
                    raise TypeError("event must be a typed RuntimeEvent")
                if not self._control.is_active():
                    self._future.cancel()
                    _raise_control_terminal(self._control)
                return cast(RuntimeEvent, item)
        except BaseException:
            self._closed = True
            self.cancel_event.set()
            self._shutdown(cancel_futures=cancel_futures)
            raise
        if completed:
            raise StopIteration(self._result)
        raise RuntimeTransportAborted()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_event.set()
        self._control.request_cancel(CancelReason.EXPLICIT_CANCEL)
        self._future.cancel()
        self._shutdown(cancel_futures=True)

    @property
    def result(self) -> _ResultT:
        if not self._result_set:
            raise RuntimeTransportAborted()
        return cast(_ResultT, self._result)


class SseAgentExecutionHost(Generic[_ResultT]):
    """Run one typed Runtime thunk while forwarding typed SSE events."""

    __slots__ = (
        "timeout_seconds",
        "poll_seconds",
        "_executor_factory",
        "_started",
        "_lock",
    )

    def __init__(
        self,
        timeout_seconds: float = CHAT_AGENT_TIMEOUT_SECONDS,
        *,
        poll_seconds: float = SSE_POLL_SECONDS,
        executor_factory: Callable[..., ThreadPoolExecutor] | None = None,
    ) -> None:
        self.timeout_seconds = _host_timeout_seconds(timeout_seconds)
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)):
            raise TypeError("poll_seconds must be a number")
        poll = float(poll_seconds)
        if not isfinite(poll) or poll <= 0:
            raise ValueError("poll_seconds must be a finite positive number")
        self.poll_seconds = poll
        self._executor_factory = ThreadPoolExecutor if executor_factory is None else executor_factory
        self._started = False
        self._lock = Lock()

    def run(
        self,
        thunk: Callable[..., _ResultT],
        invocation_control: RuntimeInvocationControl,
    ) -> _SseInvocationIterator[_ResultT]:
        if not callable(thunk):
            raise TypeError("thunk must be callable")
        with self._lock:
            if self._started:
                raise RuntimeTransportAborted()
            self._started = True
        _require_host_active(invocation_control)
        return _SseInvocationIterator(
            thunk,
            invocation_control,
            timeout_seconds=self.timeout_seconds,
            poll_seconds=self.poll_seconds,
            executor_factory=self._executor_factory,
        )

    def iter_events(
        self,
        thunk: Callable[..., _ResultT],
        invocation_control: RuntimeInvocationControl,
    ) -> _SseInvocationIterator[_ResultT]:
        return self.run(thunk, invocation_control)

    stream = iter_events


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value


def _http_message_payload(outcome: MessageOutcome) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "message",
        "message": outcome.message,
    }
    if outcome.conversation_id is not None:
        payload["conversation_id"] = outcome.conversation_id
    if outcome.write_status is not None:
        payload["write_status"] = outcome.write_status
    if outcome.write_error:
        payload["write_error"] = outcome.write_error
    if outcome.undo is not None:
        payload["undo"] = _plain(outcome.undo)
    if outcome.operation_id is not None:
        payload["operation_id"] = outcome.operation_id
    if outcome.replayed:
        payload["replayed"] = True
    return payload


def outcome_http_payload(outcome: RuntimeOutcome | ImmediateHttpOutcome) -> dict[str, object]:
    """Project a typed outcome to the existing safe HTTP JSON body."""

    if type(outcome) is ImmediateHttpOutcome:
        return {str(key): _plain(value) for key, value in outcome.payload.items()}
    if type(outcome) is MessageOutcome:
        return _http_message_payload(outcome)
    if type(outcome) is ConfirmationRequiredOutcome:
        return runtime_outcome_payload(outcome)
    if type(outcome) is RuntimeFailureOutcome:
        payload: dict[str, object] = {"error": outcome.message}
        payload["error_code"] = outcome.code.value
        return payload
    if type(outcome) is OperationPendingOutcome:
        payload = {
            "error": outcome.message,
            "error_code": outcome.code.value,
            "operation_id": outcome.operation_id,
        }
        if outcome.conversation_id is not None:
            payload["conversation_id"] = outcome.conversation_id
        if outcome.retry_after_seconds is not None:
            payload["retry_after_seconds"] = outcome.retry_after_seconds
        return payload
    if type(outcome) is OperationReplayOutcome:
        return runtime_outcome_payload(outcome)
    raise TypeError("outcome must be a typed RuntimeOutcome or ImmediateHttpOutcome")


def outcome_http_status(outcome: RuntimeOutcome | ImmediateHttpOutcome) -> int:
    """Return the closed status mapping for a Runtime outcome."""

    if type(outcome) is ImmediateHttpOutcome:
        return outcome.status_code
    if type(outcome) is RuntimeFailureOutcome:
        return outcome.status_code
    if type(outcome) is OperationPendingOutcome:
        return 409 if outcome.code.value == "operation_delivery_pending" else 503
    if type(outcome) in {MessageOutcome, ConfirmationRequiredOutcome, OperationReplayOutcome}:
        return 200
    raise TypeError("outcome must be a typed RuntimeOutcome or ImmediateHttpOutcome")


def outcome_http_response(outcome: RuntimeOutcome | ImmediateHttpOutcome) -> JSONResponse:
    """Construct one JSON response from an already validated outcome."""

    return JSONResponse(outcome_http_payload(outcome), status_code=outcome_http_status(outcome))


def event_sse_name(event: RuntimeEvent) -> str:
    if type(event) not in _EVENT_NAMES:
        raise TypeError("event must be a typed RuntimeEvent")
    return _EVENT_NAMES[type(event)]


def event_sse_payload(event: RuntimeEvent) -> dict[str, object]:
    """Return only the JSON-safe SSE data for a typed event."""

    return runtime_event_payload(event)


def encode_sse_event(
    event: RuntimeEvent,
    *,
    seq: int,
    run_id: str = "",
    envelope: Mapping[str, object] | None = None,
) -> str:
    """Encode one typed event with an optional complete legacy envelope.

    The typed event owns ``seq``, ``event``, and ``data``.  A complete
    ``SseRun.envelope()`` may carry those reserved fields only when they are
    exactly the canonical values; all other envelope fields are preserved as
    extra metadata.
    """

    if type(event) not in _EVENT_NAMES:
        raise TypeError("event must be a typed RuntimeEvent")
    if type(seq) is not int or seq < 1:
        raise ValueError("seq must be a positive integer")
    canonical = {
        "seq": seq,
        "event": event_sse_name(event),
        "data": event_sse_payload(event),
    }
    data: dict[str, object] = dict(envelope or {})
    for key, expected in canonical.items():
        if key in data and (type(data[key]) is not type(expected) or data[key] != expected):
            raise ValueError(f"SSE envelope cannot override typed field: {key}")
    data.update(canonical)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    event_name = event_sse_name(event)
    event_id = f"{run_id}:{seq}" if run_id else str(seq)
    return f"event: {event_name}\nid: {event_id}\ndata: {body}\n\n"


def _adapt_cleanup_callback(callback: Callable[..., object] | None) -> CleanupCallback | None:
    """Adapt a zero- or one-argument cleanup callable once at construction."""

    if callback is None:
        return None
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return cast(CleanupCallback, callback)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    accepts_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    required_positional = [
        parameter
        for parameter in positional
        if parameter.default is inspect.Parameter.empty
    ]
    required_keyword_only = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
    ]
    if required_keyword_only or len(required_positional) > 1:
        raise TypeError("cleanup callback must accept at most one required reason")
    if accepts_varargs or positional:
        return cast(CleanupCallback, callback)
    if not required_keyword_only:
        def no_argument_adapter(_reason: CompletionReason | None) -> object:
            return callback()

        return no_argument_adapter
    raise TypeError("cleanup callback must accept a reason or no arguments")


class PreparedStreamGuard:
    """Single-owner CAS guard for prepared stream execution and cleanup."""

    __slots__ = (
        "_prepared",
        "_lifecycle",
        "_runtime",
        "_begin",
        "_abort",
        "_complete",
        "_cleanup",
        "_execute",
        "_lock",
        "_transition_inflight",
        "_begun",
        "_executed",
        "_aborted",
        "_completed",
        "_cleanup_done",
        "_response_started",
    )

    def __init__(
        self,
        first: object | None = None,
        second: object | None = None,
        *,
        prepared: PreparedStreamExecution | None = None,
        lifecycle: PreparedLifecycle | None = None,
        runtime: object | None = None,
        begin: Callable[[], bool] | None = None,
        abort_if_prepared: Callable[[], bool] | None = None,
        complete: Callable[[CompletionReason], bool] | None = None,
        on_begin: Callable[[], bool] | None = None,
        on_abort: Callable[[], bool] | None = None,
        on_complete: Callable[[CompletionReason], bool] | None = None,
        on_cleanup: Callable[..., object] | None = None,
        cleanup: Callable[..., object] | None = None,
        on_execute: Callable[[], object] | None = None,
        execute: Callable[[], object] | None = None,
    ) -> None:
        if first is not None:
            if isinstance(first, PreparedStreamExecution):
                if prepared is not None:
                    raise TypeError("prepared provided twice")
                prepared = first
                if second is not None:
                    if runtime is not None:
                        raise TypeError("runtime provided twice")
                    runtime = second
            elif isinstance(first, PreparedLifecycle):
                if lifecycle is not None:
                    raise TypeError("lifecycle provided twice")
                lifecycle = first
            elif runtime is None:
                runtime = first
            else:
                raise TypeError("runtime provided twice")
        if second is not None:
            if isinstance(second, PreparedStreamExecution):
                if prepared is not None:
                    raise TypeError("prepared provided twice")
                prepared = second
            elif isinstance(second, PreparedLifecycle):
                if lifecycle is not None:
                    raise TypeError("lifecycle provided twice")
                lifecycle = second
            elif runtime is None and not isinstance(first, PreparedStreamExecution):
                runtime = second
            elif not isinstance(first, PreparedStreamExecution):
                raise TypeError("runtime provided twice")
        if lifecycle is not None and prepared is not None:
            raise TypeError("provide prepared or lifecycle, not both")
        self._prepared = prepared
        self._lifecycle = lifecycle
        self._runtime = runtime
        self._begin = begin if begin is not None else on_begin
        self._abort = abort_if_prepared if abort_if_prepared is not None else on_abort
        self._complete = complete if complete is not None else on_complete
        cleanup_callback = on_cleanup if on_cleanup is not None else cleanup
        if cleanup_callback is None and runtime is not None:
            for name in ("cleanup", "cleanup_prepared_stream", "finish_cleanup"):
                candidate = getattr(runtime, name, None)
                if callable(candidate):
                    cleanup_callback = cast(Callable[..., object], candidate)
                    break
        self._cleanup = _adapt_cleanup_callback(cleanup_callback)
        self._execute = on_execute if on_execute is not None else execute
        self._lock = Lock()
        self._transition_inflight = False
        self._begun = False
        self._executed = False
        self._aborted = False
        self._completed = False
        self._cleanup_done = False
        self._response_started = False

    @property
    def lifecycle_state(self) -> PreparedLifecycleState:
        with self._lock:
            aborted = self._aborted
            completed = self._completed
            begun = self._begun
        if aborted:
            return PreparedLifecycleState.ABORTED
        if completed:
            return PreparedLifecycleState.COMPLETED
        if self._prepared is not None:
            return self._prepared.lifecycle_state
        if self._lifecycle is not None:
            return self._lifecycle.state
        if self._runtime is not None:
            state = getattr(self._runtime, "lifecycle_state", None)
            if isinstance(state, PreparedLifecycleState):
                return state
        return PreparedLifecycleState.EXECUTING if begun else PreparedLifecycleState.PREPARED

    @property
    def response_started(self) -> bool:
        return self._response_started

    def mark_response_started(self) -> None:
        self._response_started = True

    def _call_runtime(self, names: tuple[str, ...], *args: object) -> bool:
        if self._runtime is None:
            return False
        for name in names:
            callback = getattr(self._runtime, name, None)
            if callable(callback):
                result = callback(*args)
                if type(result) is not bool:
                    raise TypeError(f"{name} must return bool")
                return result
        return False

    def _transition_begin(self) -> bool:
        if self._begin is not None:
            return self._begin()
        if self._prepared is not None:
            return self._prepared.begin()
        if self._lifecycle is not None:
            return self._lifecycle.begin()
        return self._call_runtime(("begin_execution", "begin_prepared_stream", "begin"))

    def _transition_abort(self) -> bool:
        if self._abort is not None:
            return self._abort()
        if self._prepared is not None:
            return self._prepared.abort_if_prepared()
        if self._lifecycle is not None:
            return self._lifecycle.abort_if_prepared()
        return self._call_runtime(("abort_if_prepared", "abort_before_start", "abort"))

    def _transition_complete(self, reason: CompletionReason) -> bool:
        if self._complete is not None:
            return self._complete(reason)
        if self._prepared is not None:
            return self._prepared.complete(reason)
        if self._lifecycle is not None:
            return self._lifecycle.complete(reason)
        return self._call_runtime(("complete_execution", "complete_prepared_stream", "complete"), reason)

    def _run_cleanup(self, reason: CompletionReason | None) -> None:
        with self._lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True
            cleanup = self._cleanup
        if cleanup is None:
            return
        result = cleanup(reason)
        if inspect.isawaitable(result):
            # Guard transitions are synchronous; async cleanup is scheduled by
            # the response finalizer instead of being silently awaited here.
            raise RuntimeError("PreparedStreamGuard cleanup must be synchronous")

    def _claim_transition(self, transition: str) -> bool:
        with self._lock:
            if self._transition_inflight:
                return False
            if transition == "begin":
                if self._begun or self._aborted or self._completed:
                    return False
            elif transition == "abort":
                if self._begun or self._aborted or self._completed:
                    return False
            elif transition == "complete":
                if not self._begun or self._aborted or self._completed:
                    return False
            else:  # pragma: no cover - private callers use the closed set
                raise ValueError("unknown guard transition")
            self._transition_inflight = True
            return True

    def _finish_transition(self, transition: str, won: bool) -> None:
        if type(won) is not bool:
            raise TypeError("lifecycle transition must return bool")
        with self._lock:
            self._transition_inflight = False
            if not won:
                return
            if transition == "begin":
                self._begun = True
            elif transition == "abort":
                self._aborted = True
            else:
                self._completed = True

    def begin_execution(self) -> bool:
        if not self._claim_transition("begin"):
            return False
        try:
            won = self._transition_begin()
            self._finish_transition("begin", won)
        except BaseException:
            with self._lock:
                self._transition_inflight = False
            raise
        return won

    def begin(self) -> bool:
        return self.begin_execution()

    def execute_once(self) -> object | None:
        with self._lock:
            if not self._begun or self._executed:
                return None
            self._executed = True
            execute = self._execute
        if execute is None:
            return None
        return execute()

    def abort_if_prepared(self) -> bool:
        if not self._claim_transition("abort"):
            return False
        try:
            won = self._transition_abort()
            self._finish_transition("abort", won)
        except BaseException:
            with self._lock:
                self._transition_inflight = False
            raise
        if won:
            self._run_cleanup(None)
        return won

    def abort(self) -> bool:
        return self.abort_if_prepared()

    def complete(self, reason: CompletionReason) -> bool:
        if not isinstance(reason, CompletionReason):
            raise TypeError("reason must be a CompletionReason")
        if not self._claim_transition("complete"):
            return False
        try:
            won = self._transition_complete(reason)
            self._finish_transition("complete", won)
        except BaseException:
            with self._lock:
                self._transition_inflight = False
            raise
        if won:
            self._run_cleanup(reason)
        return won

    def complete_execution(self, reason: CompletionReason) -> bool:
        return self.complete(reason)

    def complete_normal(self) -> bool:
        return self.complete(CompletionReason.NORMAL)

    def complete_cancelled(self) -> bool:
        return self.complete(CompletionReason.CANCELLED)

    def complete_transport_aborted(self) -> bool:
        return self.complete(CompletionReason.TRANSPORT_ABORTED)

    def finalize(self, reason: CompletionReason = CompletionReason.TRANSPORT_ABORTED) -> bool:
        if self.lifecycle_state is PreparedLifecycleState.PREPARED:
            return self.abort_if_prepared()
        if self.lifecycle_state is PreparedLifecycleState.EXECUTING:
            return self.complete(reason)
        return False


class GuardedStreamingResponse(StreamingResponse):
    """StreamingResponse with an outer, idempotent prepared-stream finalizer."""

    def __init__(
        self,
        content: Content,
        guard: PreparedStreamGuard,
        *,
        execute: Callable[[], object] | None = None,
        background: BackgroundTask | Callable[[], object] | None = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = "text/event-stream; charset=utf-8",
    ) -> None:
        self.guard = guard
        if execute is not None:
            guard._execute = execute
        self._body_entered = False
        self._body_exhausted = False
        self._finalizer_registered = False
        self._original_background = background
        self._background_finalizer_lock = Lock()
        self._background_finalized = False
        wrapped_content = self._wrap_content(content)
        wrapped_background = BackgroundTask(self._background_finalizer)
        super().__init__(
            wrapped_content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=wrapped_background,
        )
        self._finalizer_registered = True

    async def _background_finalizer(self) -> None:
        with self._background_finalizer_lock:
            if self._background_finalized:
                return
            self._background_finalized = True
        background_error: BaseException | None = None
        failure_reason = CompletionReason.TRANSPORT_ABORTED
        try:
            original = self._original_background
            if original is not None:
                result = original() if callable(original) else original
                if inspect.isawaitable(result):
                    await cast(Awaitable[object], result)
        except (RuntimeCancelled, RuntimeAgentTimedOut, asyncio.CancelledError, ClientDisconnect) as exc:
            background_error = exc
            failure_reason = CompletionReason.CANCELLED
            raise
        except RuntimeTransportAborted as exc:
            background_error = exc
            raise
        except BaseException as exc:
            background_error = exc
            raise
        finally:
            if background_error is not None:
                self._finalize_owner_preserving(failure_reason)
            else:
                reason = CompletionReason.NORMAL if self._body_exhausted else failure_reason
                try:
                    self._finalize_owner(reason)
                except (
                    RuntimeCancelled,
                    RuntimeTransportAborted,
                    RuntimeAgentTimedOut,
                    ClientDisconnect,
                    asyncio.CancelledError,
                ):
                    raise
                except Exception as exc:
                    raise RuntimeTransportAborted() from exc

    def _finalize_owner(self, reason: CompletionReason) -> None:
        state = self.guard.lifecycle_state
        if state is PreparedLifecycleState.PREPARED:
            self.guard.abort_if_prepared()
        elif state is PreparedLifecycleState.EXECUTING:
            self.guard.complete(reason)

    def _finalize_owner_preserving(self, reason: CompletionReason) -> None:
        try:
            self._finalize_owner(reason)
        except BaseException:
            # The caller's control/BaseException remains authoritative.  The
            # guard has still been transitioned before cleanup was attempted.
            return

    async def _body(self, content: Content) -> AsyncIterable[bytes | str]:
        if not self.guard.begin_execution():
            return
        self._body_entered = True
        try:
            replacement = self.guard.execute_once()
            source: Any = replacement if replacement is not None else content
            if hasattr(source, "__aiter__"):
                async for chunk in cast(AsyncIterable[bytes | str], source):
                    yield chunk
            else:
                async for chunk in iterate_in_threadpool(cast(Iterable[bytes | str], source)):
                    yield chunk
            self._body_exhausted = True
        except (RuntimeCancelled, RuntimeAgentTimedOut, asyncio.CancelledError, ClientDisconnect):
            self._complete_preserving(CompletionReason.CANCELLED)
            raise
        except RuntimeTransportAborted:
            self._complete_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise
        except Exception as exc:
            self._complete_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise RuntimeTransportAborted() from exc
        except BaseException:
            # Cleanup is owned by the CAS winner; never suppress the original
            # BaseException (including KeyboardInterrupt/SystemExit).
            self._complete_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise

    def _complete_preserving(self, reason: CompletionReason) -> None:
        try:
            self.guard.complete(reason)
        except BaseException:
            return

    def _wrap_content(self, content: Content) -> AsyncIterable[bytes | str]:
        async def body() -> AsyncIterable[bytes | str]:
            async for chunk in self._body(content):
                yield chunk

        return body()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def tracked_send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                self.guard.mark_response_started()
            try:
                await send(message)
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut, ClientDisconnect):
                raise
            except OSError:
                # Starlette owns the ASGI send-disconnect translation.  Keep
                # OSError intact so its response adapter raises ClientDisconnect.
                raise
            except Exception as exc:
                raise RuntimeTransportAborted() from exc

        guarded_receive = receive
        try:
            # Starlette's pre-2.4 implementation races body iteration against
            # the disconnect listener.  A receive-first barrier makes an
            # already disconnected response deterministic and, for a normal
            # request body, replays the first message unchanged.
            spec_version = tuple(
                map(int, scope.get("asgi", {}).get("spec_version", "2.0").split("."))
            )
            if spec_version < (2, 4):
                first_message = await receive()
                if first_message.get("type") == "http.disconnect":
                    self._finalize_owner(CompletionReason.TRANSPORT_ABORTED)
                    return
                replayed = False

                async def replay_receive() -> dict[str, Any]:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return cast(dict[str, Any], first_message)
                    return cast(dict[str, Any], await receive())

                guarded_receive = replay_receive
            await super().__call__(scope, guarded_receive, tracked_send)
        except (asyncio.CancelledError, ClientDisconnect):
            self._finalize_owner_preserving(CompletionReason.CANCELLED)
            raise
        except RuntimeCancelled:
            self._finalize_owner_preserving(CompletionReason.CANCELLED)
            raise
        except RuntimeTransportAborted:
            self._finalize_owner_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise
        except RuntimeAgentTimedOut:
            self._finalize_owner_preserving(CompletionReason.CANCELLED)
            raise
        except Exception as exc:
            self._finalize_owner_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise RuntimeTransportAborted() from exc
        except BaseException:
            self._finalize_owner_preserving(CompletionReason.TRANSPORT_ABORTED)
            raise
        finally:
            # This is authoritative for a response whose body iterator was
            # never entered.  BackgroundTask calls the same operation again.
            self._finalize_owner_preserving(
                CompletionReason.NORMAL
                if self._body_exhausted
                else CompletionReason.CANCELLED
                if self._body_entered
                else CompletionReason.TRANSPORT_ABORTED
            )


def build_guarded_streaming_response(
    content: Content,
    *,
    guard: PreparedStreamGuard,
    execute: Callable[[], object] | None = None,
    background: BackgroundTask | Callable[[], object] | None = None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    media_type: str | None = "text/event-stream; charset=utf-8",
) -> GuardedStreamingResponse:
    """Construct a guarded response and abort before-start on construction errors."""

    try:
        return GuardedStreamingResponse(
            content,
            guard=guard,
            execute=execute,
            background=background,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )
    except BaseException:
        try:
            guard.abort_if_prepared()
        except BaseException:
            # The construction error remains authoritative; cleanup is
            # best-effort and must not replace it.
            pass
        raise


__all__ = [
    "GuardedStreamingResponse",
    "PreparedStreamGuard",
    "build_guarded_streaming_response",
    "encode_sse_event",
    "event_sse_name",
    "event_sse_payload",
    "outcome_http_payload",
    "outcome_http_response",
    "outcome_http_status",
]
