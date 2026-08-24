"""Production composition for the transport-independent Pilot Runtime.

The API module owns concrete repositories and provider configuration.  This
module is the single composition boundary that turns those concrete objects
into the narrow seams consumed by :class:`PilotRuntime`.  Route handlers only
deal in the public request/transport contracts; none of the adapters below
are request-local state.
"""

from __future__ import annotations

import json
import inspect
from dataclasses import replace
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, cast

from sqlalchemy import event as sqlalchemy_event, select

from offerpilot.ai.agent_contracts import (
    AgentAssistantDelta,
    AgentLoopEvent,
    AgentToolCall,
    AgentToolResult,
    AgentTurnResult,
    ChatModel,
    ChatRunCancelled,
    PendingAction,
)
from offerpilot.ai.agent_loop import (
    AgentLoopInvocation,
    AgentLoopRunner,
    NewTurnSeed,
)
from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.tool_specs.legacy import build_legacy_deterministic_catalog
from offerpilot.ai.write_operations import WriteOperationCoordinator, WriteOperationRepository
from offerpilot.agent_runtime.journal import NullRunRecorder, RunRecorderFactory
from offerpilot.ai.types import Message
from offerpilot.config import Config, load_config
from offerpilot.context_projector.loader import ContextSourceLoader
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
)
from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    ConfirmationRequest,
    JsonValue,
    RuntimeEvent,
    RuntimeEventSink,
    StartTurnRequest,
    ToolCallEvent,
    ToolResultEvent,
    freeze_json_mapping,
)
from offerpilot.pilot_runtime.deterministic import (
    DeterministicDependencies,
    DeterministicPilotAdapter,
)
from offerpilot.pilot_runtime.errors import (
    ModelUnconfiguredError,
    RuntimeAgentTimedOut,
    RuntimeCancelled,
    RuntimeTransportAborted,
)
from offerpilot.pilot_runtime.persistence import (
    ChatPersistenceCoordinator,
    DeliveryOutcome,
    PersistenceResult,
    PersistenceStatus,
)
from offerpilot.pilot_runtime.service import (
    ContextAssembler,
    PilotRuntime,
    ResolvedModel,
    RuntimeDependencies,
    SourceLoader,
)


_ACTIVE_TIMEOUT_DELIVERY: ContextVar[tuple[object, object] | None] = ContextVar(
    "offerpilot_active_timeout_delivery",
    default=None,
)


def _attribute(value: object | None, name: str, default: object = None) -> object:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except AttributeError:
        return default


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    return value


def _provider_error_message(error: Exception, config: Config) -> str:
    """Keep provider diagnostics useful while masking configured secrets."""

    detail = str(error).strip() or "模型供应商连接失败"
    profiles = config.provider_profiles()
    for profile in profiles:
        if profile.api_key:
            detail = detail.replace(profile.api_key, "***")
    return f"AI 连接失败：{detail}。请检查 AI 设置或稍后重试。"


def _invoke(function: Callable[..., object], values: Mapping[str, object], positional: tuple[object, ...]) -> object:
    """Bind one injected composition seam without retrying its body."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*positional)
    parameters = tuple(signature.parameters.values())
    args: list[object] = []
    kwargs: dict[str, object] = {}
    fallback_index = 0
    has_var_keyword = False
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            args.extend(positional[fallback_index:])
            fallback_index = len(positional)
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if parameter.name in values:
            selected = values[parameter.name]
        elif fallback_index < len(positional):
            selected = positional[fallback_index]
            fallback_index += 1
        elif parameter.default is inspect.Parameter.empty:
            continue
        else:
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = selected
        else:
            args.append(selected)
    if has_var_keyword:
        for name, value in values.items():
            if name not in signature.parameters and name not in kwargs:
                kwargs[name] = value
    signature.bind(*args, **kwargs)
    return function(*args, **kwargs)


class _ConversationGateway:
    __slots__ = ("_chat", "_title_from_message")

    def __init__(
        self,
        chat: object,
        title_from_message: Callable[[str], str] | None = None,
    ) -> None:
        self._chat = chat
        self._title_from_message = title_from_message or _title_from_message

    def create(self, request: StartTurnRequest) -> object:
        from offerpilot.repositories.chat import ConversationScopeMutationSnapshot

        create = getattr(self._chat, "create_conversation_with_scope")
        mutation = ConversationScopeMutationSnapshot(
            context_type=request.context_type,
            context_ref=request.context_ref,
            mode=request.mode,
        )
        return create(
            self._title_from_message(request.message),
            mutation,
            title_source="fallback",
        )

    def load(self, conversation_id: int) -> object | None:
        getter = getattr(self._chat, "get_conversation")
        return cast(object | None, getter(conversation_id))


class _ModelResolver:
    __slots__ = (
        "_injected",
        "_data_dir",
        "_catalog",
        "_tool_context",
    )

    def __init__(
        self,
        injected: ChatModel | None,
        data_dir: Path,
        catalog: object,
        tool_context: Callable[[object, object], object],
    ) -> None:
        self._injected = injected
        self._data_dir = data_dir
        self._catalog = catalog
        self._tool_context = tool_context

    def resolve(self, request: StartTurnRequest, conversation: object) -> ResolvedModel:
        del request
        config: Config
        if self._injected is None:
            config = load_config(self._data_dir)
            try:
                model: object = ConfiguredAIClient(
                    config,
                    on_provider_event=lambda level, message: _append_log(
                        self._data_dir, level, message
                    ),
                )
            except ValueError as exc:
                raise ModelUnconfiguredError(str(exc)) from exc
        else:
            model = self._injected
            config = load_config(self._data_dir)
        def provider_error(error: Exception, current_config: Config = config) -> str:
            return _provider_error_message(error, current_config)

        return ResolvedModel(
            model=cast(ChatModel, model),
            catalog=self._catalog,
            config=config,
            tool_context=self._tool_context(conversation, NullRunRecorder()),
            auto_approve=config.chat_auto_approve_writes is True,
            provider_error_message=provider_error,
        )


class _SourceAdapter(SourceLoader):
    __slots__ = ("_loader",)

    def __init__(self, loader: Callable[..., object]) -> None:
        self._loader = loader

    def load(
        self,
        conversation: object,
        request: StartTurnRequest | ConfirmationRequest,
        *,
        attachments: Sequence[object] = (),
        page_context: object | None = None,
        pending_tool_call_id: str = "",
    ) -> object:
        del page_context
        return _invoke(
            self._loader,
            {
                "conversation": conversation,
                "request": request,
                "attachments": tuple(attachments),
                "pending_tool_call_id": pending_tool_call_id
                or str(_attribute(conversation, "pending_tool_call_id", "") or ""),
                "conversation_id": _attribute(conversation, "id"),
            },
            (conversation, request),
        )


class _ContextAdapter(ContextAssembler):
    __slots__ = (
        "_persistence",
        "_system_message",
        "_clarification_message",
        "_page_messages",
    )

    def __init__(
        self,
        persistence: ChatPersistenceCoordinator,
        *,
        system_message: Callable[[], object],
        clarification_message: Callable[
            [tuple[PendingAction, str] | None, str], object | None
        ],
        page_messages: Callable[[Mapping[str, object] | None], Sequence[object]],
    ) -> None:
        self._persistence = persistence
        self._system_message = system_message
        self._clarification_message = clarification_message
        self._page_messages = page_messages

    @staticmethod
    def _pending_view(value: object | None) -> PendingAction | None:
        if value is None:
            return None
        if isinstance(value, PendingAction):
            return value
        return PendingAction(
            tool_call_id=str(_attribute(value, "tool_call_id", "") or ""),
            tool_name=str(_attribute(value, "tool_name", "") or ""),
            args=str(_attribute(value, "args", "") or ""),
            human=str(_attribute(value, "human", "") or ""),
            operation_id=str(_attribute(value, "operation_id", "") or ""),
        )

    def assemble(
        self,
        source: object,
        conversation: object,
        request: StartTurnRequest | ConfirmationRequest,
    ) -> tuple[object, ...]:
        history: tuple[object, ...] = tuple(
            cast(Sequence[object], _attribute(source, "history", ()) or ())
        )
        context_message = _attribute(source, "context_message")
        attachment_messages: tuple[object, ...] = tuple(
            cast(Sequence[object], _attribute(source, "attachment_messages", ()) or ())
        )
        values: list[object] = [self._system_message()]
        if isinstance(request, StartTurnRequest):
            conversation_id = int(
                cast(
                    int,
                    _attribute(conversation, "id", request.conversation_id or 0) or 0,
                )
            )
            clarification_view = self._persistence.get_pending_clarification(conversation_id)
            clarification = None
            if clarification_view is not None:
                pending = self._pending_view(_attribute(clarification_view, "pending"))
                if pending is not None:
                    clarification = (pending, str(_attribute(clarification_view, "question", "")))
            message = self._clarification_message(clarification, request.message)
            if message is not None:
                values.append(message)
            page_context = (
                cast(dict[str, object], _plain_json(request.page_context))
                if request.page_context is not None
                else None
            )
        if context_message is not None:
            values.append(context_message)
        if isinstance(request, StartTurnRequest):
            values.extend(self._page_messages(page_context))
            values.extend(attachment_messages)
        values.extend(history)
        return tuple(values)


class _ProposalJournalGate:
    """Delay the agent's proposal projection until Pending is durable.

    The journal's suspension atom owns ``tool.proposed`` for HITL turns.  The
    model tool pipeline also projects that fact while it is preparing a call;
    holding the latter prevents the proposal from preceding the persisted
    assistant tool-call message and lets the suspension atom remain the sole
    writer for confirmation turns.
    """

    __slots__ = ("_delegate", "_proposals")

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self._proposals: list[object] = []

    def append_event(self, event: object) -> object:
        if getattr(event, "event_type", None) == "tool.proposed":
            self._proposals.append(event)
            return None
        append = getattr(self._delegate, "append_event")
        return cast(object, append(event))

    def release_proposals(self) -> None:
        proposals = tuple(self._proposals)
        self._proposals.clear()
        if not proposals:
            return
        append = getattr(self._delegate, "append_event")
        for event in proposals:
            append(event)

    def discard_proposals(self) -> None:
        self._proposals.clear()

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _AgentEventAdapter:
    __slots__ = ("_sink",)

    def __init__(self, sink: RuntimeEventSink) -> None:
        self._sink = sink

    def emit(self, event: AgentLoopEvent) -> None:
        projected: RuntimeEvent
        if isinstance(event, AgentAssistantDelta):
            projected = AssistantDeltaEvent(delta=event.delta)
        elif isinstance(event, AgentToolCall):
            projected = ToolCallEvent(
                tool_call_id=event.tool_call_id,
                tool_name=event.tool_name,
                public_label=event.public_label,
                kind=event.kind,
                confirm_mode=event.confirm_mode,
                summary=event.summary,
                args_summary=freeze_json_mapping(cast(Mapping[str, object], event.args_summary)),
            )
        elif isinstance(event, AgentToolResult):
            payload = event.payload
            status = str(payload.get("status") or "error")
            if status not in {"success", "error", "cancelled"}:
                status = "error"
            write_status = payload.get("write_status")
            if write_status not in {None, "none", "success", "failed", "cancelled"}:
                write_status = None
            projected = ToolResultEvent(
                tool_call_id=event.tool_call_id,
                tool_name=str(payload.get("tool_name") or "unknown"),
                status=cast(Any, status),
                summary=str(payload.get("summary") or ""),
                evidence=_agent_payload_tuple(payload.get("evidence")),
                affected_resources=_agent_payload_tuple(payload.get("affected_resources")),
                changed_entities=_agent_payload_tuple(payload.get("changed_entities")),
                message=str(payload.get("message") or ""),
                visible_result=str(payload.get("visible_result") or ""),
                operation_id=event.operation_id or None,
                write_status=cast(Any, write_status),
            )
        else:
            raise TypeError("unknown Agent Loop event")
        try:
            self._sink.emit(projected)
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except Exception as exc:
            raise RuntimeTransportAborted() from exc
        except BaseException:
            raise


def _agent_payload_tuple(value: object) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    projected: list[Mapping[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            projected.append(freeze_json_mapping(cast(Mapping[str, object], item)))
        except (TypeError, ValueError):
            continue
    return tuple(projected)


class _AgentDriver:
    __slots__ = ("_runner",)

    def __init__(self) -> None:
        self._runner = AgentLoopRunner()

    def execute(self, invocation: AgentLoopInvocation) -> AgentTurnResult:
        recorder = (
            _ProposalJournalGate(invocation.run_recorder)
            if isinstance(invocation.seed, NewTurnSeed)
            else invocation.run_recorder
        )
        context = invocation.tool_context
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("Agent Loop requires ToolExecutionContext")
        bound_context = replace(context, run_recorder=cast(Any, recorder))
        runtime_sink = cast(RuntimeEventSink | None, invocation.event_sink)
        agent_sink = _AgentEventAdapter(runtime_sink) if runtime_sink is not None else None
        try:
            return self._runner.run(
                replace(
                    invocation,
                    tool_context=bound_context,
                    run_recorder=cast(Any, recorder),
                    event_sink=agent_sink,
                )
            )
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            raise
        except ChatRunCancelled as exc:
            raise RuntimeCancelled() from exc


class _AtomicTimeoutDelivery:
    """Deliver a timed-out write inside its authoritative Ledger commit.

    A slow handler can finish after the HTTP deadline while its write
    transaction is still open.  The normal Runtime callback delivers the
    fallback after that transaction commits, which briefly exposes the
    updated domain row alongside the old Pending card.  This composition seam
    observes only registered confirmation states and, when the deadline has
    already fired, projects the fenced fallback in the same SQLAlchemy
    transaction before the primary commit becomes visible.
    """

    __slots__ = (
        "_chat",
        "_repository",
        "_states",
        "_owners",
        "_tokens",
        "_lock",
        "_prepare_owner",
    )

    def __init__(self, chat: object, repository: WriteOperationRepository) -> None:
        self._chat = chat
        self._repository = repository
        self._states: dict[object, object] = {}
        self._owners: dict[object, object] = {}
        self._tokens: dict[object, Token[tuple[object, object] | None]] = {}
        self._lock = RLock()
        self._prepare_owner = repository.prepare_owner

        def capture_owner(operation_id: str, generation: int = 1) -> object:
            owner = self._prepare_owner(operation_id, generation)
            active = _ACTIVE_TIMEOUT_DELIVERY.get()
            if active is not None and active[0] is self:
                handle = active[1]
                with self._lock:
                    if handle in self._states:
                        self._owners[handle] = owner
            return owner

        setattr(repository, "prepare_owner", capture_owner)
        sqlalchemy_event.listen(
            repository.session_factory, "before_commit", self._before_commit
        )

    def register(self, state: object) -> object:
        handle = object()
        token = _ACTIVE_TIMEOUT_DELIVERY.set((self, handle))
        with self._lock:
            self._states[handle] = state
            self._tokens[handle] = token
        return handle

    def unregister(self, state: object, handle: object | None = None) -> None:
        active = _ACTIVE_TIMEOUT_DELIVERY.get()
        if handle is None and active is not None and active[0] is self:
            handle = active[1]
        if handle is None:
            return
        removed = False
        token: Token[tuple[object, object] | None] | None = None
        with self._lock:
            if self._states.get(handle) is state:
                self._states.pop(handle, None)
                self._owners.pop(handle, None)
                token = self._tokens.pop(handle, None)
                removed = True
        if removed and active is not None and active[0] is self and active[1] is handle:
            if token is None:
                _ACTIVE_TIMEOUT_DELIVERY.set(None)
                return
            try:
                _ACTIVE_TIMEOUT_DELIVERY.reset(token)
            except ValueError:
                _ACTIVE_TIMEOUT_DELIVERY.set(None)
                return
            restored = _ACTIVE_TIMEOUT_DELIVERY.get()
            if restored is not None and isinstance(restored[0], _AtomicTimeoutDelivery):
                with restored[0]._lock:
                    restored_registered = restored[1] in restored[0]._states
                if not restored_registered:
                    _ACTIVE_TIMEOUT_DELIVERY.set(None)

    def _before_commit(self, session: object) -> None:
        with self._lock:
            candidates = tuple(self._states.items())
        for handle, state in candidates:
            with self._lock:
                if self._states.get(handle) is not state:
                    continue
                owner = self._owners.get(handle)
            if owner is None:
                continue
            identity = _attribute(state, "identity")
            operation_id = str(_attribute(identity, "operation_id", "") or "")
            if not operation_id:
                continue
            lock = _attribute(state, "lock")
            if lock is None or not hasattr(lock, "__enter__"):
                continue
            with cast(Any, lock):
                timed_out = bool(_attribute(state, "timed_out", False))
                active = bool(_attribute(state, "active", False))
                attempted = bool(_attribute(state, "confirmation_attempted", False))
                origin = _attribute(state, "origin_tool_message")
                already_persisted = bool(
                    _attribute(state, "transactional_delivery_persisted", False)
                )
                pending = _attribute(state, "pending")
                claim_id = _attribute(state, "claim_id")
            if (
                not timed_out
                or not active
                or not attempted
                or origin is not None
                or already_persisted
                or pending is None
                or claim_id is None
            ):
                continue
            getter = getattr(session, "get", None)
            if not callable(getter):
                continue
            operation = getter(_write_operation_model(), operation_id)
            if operation is None or str(_attribute(operation, "status", "")) not in {
                "committed",
                "failed",
            }:
                continue
            try:
                raw_undo = _attribute(operation, "undo_json")
                undo: dict[str, object] | None = None
                if raw_undo:
                    decoded = json.loads(str(raw_undo))
                    if isinstance(decoded, Mapping):
                        undo = dict(decoded)
                succeeded = str(_attribute(operation, "status", "")) == "committed"
                message = (
                    "写入已完成，但暂时无法生成后续说明。你可以刷新数据查看结果。"
                    if succeeded
                    else "写入未完成，错误结果已记录。请检查输入后重试。"
                )
                bound_chat = getattr(self._chat, "bind")(session)
                resolved = getattr(bound_chat, "resolve_pending_confirmation")(
                    cast(
                        int,
                        _attribute(_attribute(state, "identity"), "conversation_id", 0)
                        or 0,
                    ),
                    cast(Any, pending),
                    Message(
                        role="tool",
                        content=str(_attribute(operation, "visible_result", "") or ""),
                        tool_call_id=str(_attribute(pending, "tool_call_id", "") or ""),
                    ),
                    undo,
                    claim_id=str(claim_id),
                    terminal_assistant_content=message,
                    delivery_ownership=owner,
                )
            except Exception:
                continue
            if resolved is not None:
                message_model = cast(Any, _chat_message_model())
                message_ids = tuple(
                    int(message_id)
                    for message_id in cast(Any, session).scalars(
                        select(message_model.id)
                        .where(message_model.operation_id == operation_id)
                        .order_by(message_model.delivery_ordinal.asc(), message_model.id.asc())
                    )
                )
                with cast(Any, lock):
                    state_any = cast(Any, state)
                    state_any.delivery_result = PersistenceResult(
                        PersistenceStatus.PERSISTED,
                        generation=resolved if isinstance(resolved, datetime) else None,
                        delivery_outcome=DeliveryOutcome.FINAL_RESPONSE,
                        message_count=len(message_ids),
                        message_ids=message_ids,
                        operation_id=operation_id,
                    )
                    state_any.delivered = True
                    state_any.delivery_in_progress = False
                    state_any.transactional_delivery_persisted = True


def _write_operation_model() -> object:
    from offerpilot.models import WriteOperation

    return WriteOperation


def _chat_message_model() -> object:
    from offerpilot.models import ChatMessage

    return ChatMessage


def _title_from_message(message: str) -> str:
    for line in message.splitlines():
        title = " ".join(line.split())
        if title:
            break
    else:
        return "新对话"
    for marker in ("。", "！", "？", "!", "?", "；", ";"):
        index = title.find(marker)
        if index >= 7:
            title = title[: index + 1]
            break
    return title[:36] or "新对话"


def _append_log(data_dir: Path, level: str, message: str) -> None:
    # Kept as a late import so composition remains independent from api.py.
    from offerpilot.diagnostics import append_log_entry

    append_log_entry(data_dir, level, message)


def build_pilot_runtime(
    *,
    data_dir: Path,
    chat: object,
    applications: object,
    application_jd_versions: object,
    application_outcomes: object,
    events: object,
    notes: object,
    offers: object,
    resumes: object,
    jd_analyses: object,
    context_source_loader: ContextSourceLoader[Any, Any],
    run_recorder_factory: RunRecorderFactory | object | None,
    chat_model: ChatModel | None,
    write_operations: WriteOperationRepository | None,
    write_coordinator: WriteOperationCoordinator | None,
    source_loader: Callable[..., object],
    system_message: Callable[[], object],
    clarification_message: Callable[
        [tuple[PendingAction, str] | None, str], object | None
    ],
    page_context_messages: Callable[[Mapping[str, object] | None], Sequence[object]],
    model_tool_context: Callable[[object, object], object],
    missing_target_question: Callable[..., str | None] | None = None,
    pending_action_details: Callable[[PendingAction], Mapping[str, object]] | None = None,
    undo_seed_for_pending: Callable[[PendingAction, object], Mapping[str, object]] | None = None,
    build_write_undo: Callable[
        [PendingAction, object | None, dict[str, object]], Mapping[str, object] | None
    ]
    | None = None,
    title_from_message: Callable[[str], str] | None = None,
    catalog: object = MODEL_TOOL_CATALOG,
    application_visible: Callable[[int], bool] | None = None,
    clock: Callable[[], object] | None = None,
) -> PilotRuntime:
    """Build one frozen production Runtime graph from app-owned dependencies."""

    del context_source_loader
    persistence = ChatPersistenceCoordinator(cast(Any, chat))
    gateway = _ConversationGateway(chat, title_from_message)
    source = _SourceAdapter(source_loader)
    assembler = _ContextAdapter(
        persistence,
        system_message=system_message,
        clarification_message=clarification_message,
        page_messages=page_context_messages,
    )
    driver = _AgentDriver()
    resolver = _ModelResolver(chat_model, data_dir, catalog, model_tool_context)
    deterministic = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, persistence),
            applications=applications,
            application_jd_versions=application_jd_versions,
            application_outcomes=application_outcomes,
            write_operations=write_operations,
            write_coordinator=write_coordinator,
            chat=chat,
            legacy_catalog_factory=cast(Any, build_legacy_deterministic_catalog),
        )
    )
    transactional_delivery = (
        _AtomicTimeoutDelivery(cast(Any, chat), write_operations)
        if write_operations is not None
        else None
    )
    confirmation = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=cast(Any, persistence),
            write_operations=cast(Any, write_operations),
            write_coordinator=cast(Any, write_coordinator),
            conversations=gateway,
            catalog=catalog,
            source_loader=cast(Any, source),
            context_assembler=cast(Any, assembler),
            journal=cast(Any, run_recorder_factory),
            applications=applications,
            transactional_delivery=transactional_delivery,
            undo_seed_builder=(
                (
                    lambda _prepared, context, state: undo_seed_for_pending(
                        cast(
                            PendingAction,
                            _attribute(state, "effective_pending", _attribute(state, "pending")),
                        ),
                        _attribute(context, "applications", applications),
                    )
                )
                if undo_seed_for_pending is not None
                else None
            ),
            undo_builder=(
                (
                    lambda _prepared, record, seed, state: build_write_undo(
                        cast(
                            PendingAction,
                            _attribute(state, "effective_pending", _attribute(state, "pending")),
                        ),
                        record,
                        dict(seed) if isinstance(seed, Mapping) else {},
                    )
                )
                if build_write_undo is not None
                else None
            ),
            clock=cast(Any, clock) if clock is not None else lambda: datetime.now(timezone.utc),
        )
    )
    visible = application_visible or (
        lambda application_id: getattr(applications, "get")(application_id) is not None
    )
    dependencies = RuntimeDependencies(
        conversations=gateway,
        persistence=persistence,
        model_resolver=resolver,
        source_loader=source,
        context_assembler=assembler,
        agent_driver=cast(Any, driver),
        journal=cast(Any, run_recorder_factory),
        catalog=cast(Any, catalog),
        missing_target_question=missing_target_question,
        pending_action_details=pending_action_details,
        application_visible=visible,
        deterministic=deterministic,
        confirmation_coordinator=confirmation,
    )
    return PilotRuntime(dependencies)


__all__ = ["build_pilot_runtime"]
