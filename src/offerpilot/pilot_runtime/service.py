"""Transport-independent synchronous Pilot Runtime orchestration.

Task 6 deliberately contains only the initial model turn.  The service owns the
causal order around the existing Agent driver, while the transport owns the
``AgentExecutionHost`` (and therefore the worker and deadline).  All external
objects are injected through small structural seams so this module does not
need to know about FastAPI, ORM rows, or LangGraph state.
"""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeAlias, cast
from uuid import uuid4

from offerpilot.ai.agent import PendingAction
from offerpilot.ai.tool_runtime.contracts import ToolFailure, ToolSuccess
from offerpilot.ai.tool_runtime.journal import journal_shape_digest
from offerpilot.ai.types import Message, ToolCall
from offerpilot.agent_runtime.events import (
    ContextManifestInput,
    normalize_context_identity,
    prepare_event,
)
from offerpilot.agent_runtime.journal import (
    EventInput,
    StartRunBuilder,
    SuspendedDisposition,
    TerminalDisposition,
)
from offerpilot.repositories.agent_runs import StartRunCommand

from .contracts import (
    AgentExecutionHost,
    ConfirmationRequiredOutcome,
    ImmutablePayload,
    MessageOutcome,
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeFailureOutcome,
    RuntimeInvocationControl,
    RuntimeOutcome,
    RuntimeSignalSink,
    RuntimeTransportContext,
    SignalEmitResult,
    StartTurnRequest,
    WriteStatus,
    freeze_json_mapping,
)
from .errors import (
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeFailureCode,
    RuntimeTransportAborted,
)
from .event_sink import emit_runtime_event, require_runtime_active


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


class RuntimePersistence(Protocol):
    def get_pending_action(self, conversation_id: int) -> object | None: ...

    def persist_initial_user_message(self, conversation_id: int, content: str) -> object: ...

    def persist_initial_messages(self, conversation_id: int, messages: Sequence[object]) -> object: ...

    def persist_initial_pending(
        self,
        conversation_id: int,
        messages: Sequence[object],
        pending: PendingAction,
    ) -> object: ...


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


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Composition seams used by :class:`PilotRuntime`.

    The aliases are intentional.  During the extraction the composition root
    may call the conversation seam ``conversations`` or ``conversation_store``;
    both names describe the same narrow create/load capability and neither
    leaks a repository or ORM type into this module.
    """

    conversations: object | None = None
    persistence: object | None = None
    model_resolver: object | None = None
    source_loader: object | None = None
    context_assembler: object | None = None
    agent_driver: object | None = None
    route_selector: object | None = None
    journal: object | None = None
    catalog: object | None = None
    missing_target_question: object | None = None
    conversation_store: object | None = None
    conversation_gateway: object | None = None
    pending_guard: object | None = None
    validator: object | None = None
    phase_sink: object | None = None
    application_visible: object | None = None


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
        # The current Agent driver still has a legacy callback-shaped event
        # seam.  Sync Start Turn has no transport event delivery, so legacy
        # dictionaries are intentionally ignored; typed RuntimeEvents use the
        # same safe boundary as ``emit``.
        if isinstance(event, Mapping):
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

    args: list[object] = []
    kwargs: dict[str, object] = {}
    fallback_index = 0
    has_var_keyword = False
    for parameter in signature.parameters.values():
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
            raise TypeError(f"injected callable requires unsupported parameter {parameter.name}")
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
    return function(*args, **kwargs)


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
    if value is None:
        return RouteKind.DETERMINISTIC if request.pilot_action is not None else RouteKind.MODEL
    if isinstance(value, RouteKind):
        return value
    raw = _attribute(value, "kind", _attribute(value, "route", value))
    text = str(getattr(raw, "value", raw)).lower()
    return RouteKind.DETERMINISTIC if text in {"deterministic", "confirmation"} or "deterministic" in text else RouteKind.MODEL


def _result_persisted(result: object) -> bool:
    if result is None:
        return True
    if type(result) is bool:
        return result
    value = _attribute(result, "persisted")
    if type(value) is bool:
        return value
    status = _attribute(result, "status")
    if status is not None and str(getattr(status, "value", status)) in {
        "closed",
        "not_found",
        "cas_lost",
        "duplicate",
    }:
        return False
    return True


def _timeout_result_persisted(result: object) -> bool:
    """Require explicit success before exposing the timeout assistant reply."""

    if result is None:
        return False
    if type(result) is bool:
        return result
    persisted = _attribute(result, "persisted")
    if type(persisted) is bool:
        return persisted
    status = _attribute(result, "status")
    return str(getattr(status, "value", status or "")) == "persisted"


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
                provider_blocks=message.provider_blocks,
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
        return PendingAction(call.id, call.name, call.args, call.name)
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

    __slots__ = ("_dependencies",)

    def __init__(
        self,
        dependencies: RuntimeDependenciesLike | None = None,
        **kwargs: object,
    ) -> None:
        if dependencies is None:
            values = dict(kwargs)
            self._dependencies = RuntimeDependencies(**_dependency_values(values))
            return
        if kwargs:
            if isinstance(dependencies, RuntimeDependencies):
                values = {field: getattr(dependencies, field) for field in RuntimeDependencies.__dataclass_fields__}
            else:
                if isinstance(dependencies, Mapping):
                    values = dict(dependencies)
                else:
                    values = {
                        field: getattr(dependencies, field)
                        for field in RuntimeDependencies.__dataclass_fields__
                        if hasattr(dependencies, field)
                    }
            values.update(kwargs)
            self._dependencies = RuntimeDependencies(**_dependency_values(values))
        elif isinstance(dependencies, RuntimeDependencies):
            self._dependencies = dependencies
        else:
            if isinstance(dependencies, Mapping):
                values = dict(dependencies)
            else:
                values = {
                    field: getattr(dependencies, field)
                    for field in RuntimeDependencies.__dataclass_fields__
                    if hasattr(dependencies, field)
                }
            self._dependencies = RuntimeDependencies(**_dependency_values(values))

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
        self._phase("validate")
        self._validate(request)
        self._check_cancel(cancel, invocation_control)

        self._phase("conversation")
        conversation = self._load_conversation(request)
        if conversation is None:
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        conversation_id = _conversation_id(conversation)
        if conversation_id is None:
            return self._failure(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        if _is_archived(conversation):
            return self._failure(RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409)

        route = self._select_route(request, conversation)
        self._phase(f"route:{route.value}")
        if route is not RouteKind.MODEL:
            # Deterministic and confirmation orchestration is intentionally
            # private to later extraction tasks.  Return before user/Run/
            # Source/Agent side effects.
            return self._failure(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)

        self._phase("pending_guard")
        pending_guard = self._pending_guard(conversation_id, conversation, request)
        if pending_guard is not None and pending_guard is not False:
            return self._failure(
                RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED,
                "当前写入仍待确认，请先处理确认卡。",
                409,
            )

        self._phase("model_resolve")
        resolved = self._resolve_model(request, conversation)
        if isinstance(resolved, RuntimeFailureOutcome):
            return resolved
        if resolved is None:
            return self._failure(
                RuntimeFailureCode.AI_PROVIDER_ERROR,
                "AI 设置尚未完成，请检查模型配置。",
                503,
                retryable=False,
            )

        persistence = self._require_dependency("persistence")
        self._phase("user_persist")
        self._check_cancel(cancel, invocation_control)
        user_result = self._persist_user(persistence, conversation_id, request.message)
        if not _result_persisted(user_result):
            code = (
                RuntimeFailureCode.CONVERSATION_ARCHIVED
                if _failure_status(user_result) == "closed"
                else RuntimeFailureCode.APPLICATION_NOT_FOUND
                if _failure_status(user_result) == "not_found"
                else RuntimeFailureCode.OPERATION_FAILED
            )
            status = 409 if code is RuntimeFailureCode.CONVERSATION_ARCHIVED else 404 if code is RuntimeFailureCode.APPLICATION_NOT_FOUND else 503
            return self._failure(code, "对话当前不可写入。", status)

        input_message_id = _attribute(user_result, "message_id")
        if type(input_message_id) is not int or input_message_id <= 0:
            persisted_ids = self._snapshot_message_ids(persistence, conversation_id)
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

        def abandon_once() -> None:
            nonlocal abandoned
            if abandoned:
                return
            abandoned = True
            self._abandon(recorder, journal_started)

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
                self._dependencies.catalog,
                persistence,
                invocation_control,
            )
            self._check_cancel(cancel, invocation_control)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            abandon_once()
            raise
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
                timeout_result = self._persist_timeout(persistence, conversation_id)
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
                ensure_active=lambda: self._check_cancel(cancel, invocation_control),
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
                )
            except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
                abandon_once()
                raise
        else:
            if isinstance(outcome, RuntimeFailureOutcome):
                finish_or_raise("failed", "unknown")
            else:
                finish_or_raise("completed", None)
        return outcome

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

    def _validate(self, request: StartTurnRequest) -> None:
        validator = _callable(self._dependencies.validator, ("validate", "validate_request"))
        if validator is not None:
            _invoke(validator, {"request": request}, (request,))

    def _phase(self, name: str) -> None:
        sink = _callable(self._dependencies.phase_sink, ("append", "record", "phase"))
        if sink is not None:
            sink(name)

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

    def _select_route(self, request: StartTurnRequest, conversation: object) -> RouteKind:
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
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception:
            return None
        except BaseException:
            raise
        if isinstance(value, RuntimeFailureOutcome):
            return value
        return _resolved_model(value)

    def _persist_user(self, persistence: object, conversation_id: int, message: str) -> object:
        function = _callable(
            persistence,
            ("persist_initial_user_message", "persist_user_message", "persist_user", "append_user"),
        )
        if function is None:
            raise TypeError("persistence does not provide user message persistence")
        return _invoke(
            function,
            {"conversation_id": conversation_id, "content": message, "message": message},
            (conversation_id, message),
        )

    def _start_journal(
        self,
        conversation: object,
        conversation_id: int,
        input_message_id: object,
        request: StartTurnRequest,
        transport: RuntimeTransportContext,
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
                    "origin_kind": "user_message",
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
                    "request_kind": "initial",
                    "transport_mode": transport.mode,
                    "execution_path": "model_turn",
                    "transport_run_id": transport.transport_run_id,
                },
                budget_check=budget_check,
            )
            budget_check()
            return StartRunCommand(
                run_id=run_id,
                conversation_id=conversation_id,
                input_message_id=input_message_id if type(input_message_id) is int else None,
                origin_kind="user_message",
                initial_context_type=normalized.context_type,
                initial_context_entity_id=(
                    str(normalized.entity_id) if normalized.entity_id is not None else None
                ),
                initial_context_ref_fingerprint=normalized.ref_fingerprint,
                fingerprint_key_id=str(getattr(key, "key_id")),
                initial_transport_mode=transport.mode,
                initial_route_kind="model",
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
        return (recorder if recorder is not None else _NoopRecorder()), True

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
        except Exception:
            return ()
        if not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)):
            return ()
        return tuple(
            str(name)
            for contract in contracts
            if (name := _attribute(contract, "name")) is not None
        )

    @staticmethod
    def _snapshot_message_ids(persistence: object, conversation_id: int) -> tuple[int, ...]:
        function = getattr(persistence, "list_messages", None)
        if not callable(function):
            return ()
        try:
            values = function(conversation_id)
        except Exception:
            return ()
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return ()
        return tuple(
            int(value)
            for item in values
            if type(value := _attribute(item, "id")) is int and value > 0
        )

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
            except Exception:
                values = ()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                role_by_id = {
                    int(item.id): str(item.role)
                    for item in values
                    if type(_attribute(item, "id")) is int
                }
        for message_id in message_ids:
            if type(message_id) is not int or message_id <= 0:
                continue
            self._journal_call(
                recorder,
                "append_event",
                EventInput(
                    event_type="assistant.persisted",
                    facts={
                        "message_id": message_id,
                        "message_kind": role_by_id.get(message_id, "assistant"),
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
            catalog=resolved.catalog if resolved.catalog is not None else self._dependencies.catalog,
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

    def _persist_timeout(self, persistence: object, conversation_id: int) -> object:
        function = _callable(persistence, ("persist_timeout_assistant",))
        if function is not None:
            return _invoke(function, {"conversation_id": conversation_id, "content": CHAT_TIMEOUT_MESSAGE}, (conversation_id, CHAT_TIMEOUT_MESSAGE))
        function = _callable(persistence, ("persist_assistant_message", "persist_initial_assistant_message"))
        if function is None:
            return None
        result = _invoke(function, {"conversation_id": conversation_id, "content": CHAT_TIMEOUT_MESSAGE}, (conversation_id, CHAT_TIMEOUT_MESSAGE))
        if not _timeout_result_persisted(result):
            return result
        clear = _callable(persistence, ("clear_pending_clarification",))
        if clear is not None:
            clear_result = _invoke(clear, {"conversation_id": conversation_id}, (conversation_id,))
            if not _timeout_result_persisted(clear_result):
                return clear_result
        return result

    def _persist_result(
        self,
        persistence: object,
        conversation_id: int,
        request: StartTurnRequest,
        result: NormalizedAgentTurn,
        conversation: object,
        *,
        ensure_active: Callable[[], None],
    ) -> _PersistedTurn:
        del request
        pending = result.pending
        effective_messages, forced_reply = _with_write_error_followup(
            result.added,
            result.records,
            result.failures,
        )
        reply = forced_reply or _user_facing_assistant_content(result.reply)
        write_status, write_error = _write_outcome(
            result.records,
            _has_write_attempt(result.added, result.records, self._dependencies.catalog),
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
                    ensure_active=ensure_active,
                )
            function = _callable(persistence, ("persist_initial_pending", "persist_pending"))
            if function is None:
                raise TypeError("persistence does not provide atomic pending persistence")
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = _invoke(
                function,
                {"conversation_id": conversation_id, "messages": messages, "pending": pending},
                (conversation_id, messages, pending),
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
        persisted = _invoke(function, {"conversation_id": conversation_id, "messages": messages}, (conversation_id, messages))
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
                self._dependencies.catalog,
            )
            if forced_pending is not None:
                setter = _callable(persistence, ("set_pending_clarification",))
                if setter is not None:
                    ensure_active()
                    set_result = _invoke(
                        setter,
                        {
                            "conversation_id": conversation_id,
                            "pending": forced_pending,
                            "question": forced_reply,
                        },
                        (conversation_id, forced_pending, forced_reply),
                    )
                    if not _result_persisted(set_result):
                        return _PersistedTurn(
                            self._persistence_failure(set_result, "对话澄清暂时无法保存。"),
                            message_ids,
                        )
        elif clarification is not None and _looks_like_followup_question(reply):
            setter = _callable(persistence, ("set_pending_clarification",))
            if setter is not None:
                ensure_active()
                set_result = _invoke(
                    setter,
                    {
                        "conversation_id": conversation_id,
                        "pending": clarification[0],
                        "question": reply,
                    },
                    (conversation_id, clarification[0], reply),
                )
                if not _result_persisted(set_result):
                    return _PersistedTurn(
                        self._persistence_failure(set_result, "对话澄清暂时无法保存。"),
                        message_ids,
                    )
        else:
            clear = _callable(persistence, ("clear_pending_clarification",))
            if clear is not None:
                ensure_active()
                clear_result = _invoke(
                    clear,
                    {"conversation_id": conversation_id},
                    (conversation_id,),
                )
                if not _result_persisted(clear_result):
                    return _PersistedTurn(
                        self._persistence_failure(clear_result, "对话澄清暂时无法清理。"),
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
            return None
        value = _invoke(getter, {"conversation_id": conversation_id}, (conversation_id,))
        if not isinstance(value, tuple) or len(value) != 2:
            return None
        pending = _pending(value[0])
        question = value[1]
        return (pending, question) if pending is not None and isinstance(question, str) else None

    def _persistence_failure(self, result: object, archived_message: str) -> RuntimeFailureOutcome:
        status = _failure_status(result)
        if status in {"", "closed"}:
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
        ensure_active: Callable[[], None],
    ) -> _PersistedTurn:
        atomic = _callable(persistence, ("persist_clarification", "persist_pending_clarification"))
        if atomic is not None:
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = _invoke(
                atomic,
                {
                    "conversation_id": conversation_id,
                    "messages": messages,
                    "pending": pending,
                    "question": question,
                },
                (conversation_id, messages, pending, question),
            )
            message_ids = self._result_message_ids(
                persistence,
                conversation_id,
                persisted,
                before_ids,
            )
        else:
            initial = _callable(persistence, ("persist_initial_messages", "persist_messages"))
            if initial is None:
                raise TypeError("persistence does not provide clarification persistence")
            before_ids = self._snapshot_message_ids(persistence, conversation_id)
            ensure_active()
            persisted = _invoke(initial, {"conversation_id": conversation_id, "messages": messages}, (conversation_id, messages))
            message_ids = self._result_message_ids(persistence, conversation_id, persisted, before_ids)
            if not _result_persisted(persisted):
                return _PersistedTurn(self._persistence_failure(persisted, "对话已归档，无法保存回复。"), message_ids)
            clear = _callable(persistence, ("clear_pending_action",))
            if clear is not None:
                ensure_active()
                clear_result = _invoke(clear, {"conversation_id": conversation_id}, (conversation_id,))
                if not _result_persisted(clear_result):
                    return _PersistedTurn(self._persistence_failure(clear_result, "对话已归档，无法保存回复。"), message_ids)
            setter = _callable(persistence, ("set_pending_clarification",))
            if setter is None:
                raise TypeError("persistence does not provide clarification persistence")
            try:
                ensure_active()
                set_result = _invoke(
                    setter,
                    {"conversation_id": conversation_id, "pending": pending, "question": question},
                    (conversation_id, pending, question),
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
            assistant = _callable(persistence, ("persist_assistant_message", "persist_initial_assistant_message"))
            if assistant is not None:
                ensure_active()
                persisted = _invoke(assistant, {"conversation_id": conversation_id, "content": question}, (conversation_id, question))
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
    ) -> None:
        if not started or pending is None:
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
    return {key: value for key, value in result.items() if key in valid}


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
]
