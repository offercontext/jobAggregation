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
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, cast
from uuid import uuid4

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
    SegmentSurfaceGate,
    AgentLoopInvocation,
    AgentLoopRunner,
    build_segment_surface_gate,
)
from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_authority.policy import (
    validate_startup_policy,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog as RuntimeToolCatalog
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.tool_specs.legacy import build_legacy_deterministic_catalog
from offerpilot.context_projector.selector import DEPENDENCY_POLICY_V1
from offerpilot.ai.write_operations import (
    WriteOperationCoordinator,
    WriteOperationError,
    WriteOperationRepository,
)
from offerpilot.agent_runtime.journal import NullRunRecorder, RunRecorderFactory
from offerpilot.ai.types import Message
from offerpilot.config import Config, load_config
from offerpilot.context_projector.loader import ContextSourceLoader
from offerpilot.pilot_runtime.continuation import (
    ApprovalAuthorityResolver,
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
    ResolvedPolicyCatalog,
    SourceLoader,
    SegmentExecution,
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


def _invoke(
    function: Callable[..., object], values: Mapping[str, object], positional: tuple[object, ...]
) -> object:
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


class _PolicyCatalogResolver:
    """Pure startup/policy boundary; it never constructs a Provider."""

    __slots__ = ("_catalog",)

    def __init__(self, catalog: object) -> None:
        self._catalog = catalog

    @staticmethod
    def _fresh_segment_catalog(catalog: object) -> RuntimeToolCatalog:
        """Rebuild a detached catalog for each execution Segment.

        The Approval invocation owns the composition-root catalog.  A
        continuation Segment must receive a distinct catalog identity so an
        Approval-bound invocation cannot be reused as the post-terminal
        Segment.  Rebuilding from the startup-validated specs and manifest
        also keeps the exact provider schema/order and authority fingerprint,
        while preventing mutable catalog/spec state from crossing the phase
        boundary.
        """

        if type(catalog) is not RuntimeToolCatalog:
            raise ValueError("segment catalog must be a frozen ToolCatalog")
        specs = tuple(deepcopy(catalog.specs))
        expected_names = tuple(spec.name for spec in specs)
        manifest = deepcopy(catalog.authority_manifest)
        return RuntimeToolCatalog(
            specs,
            expected_names=expected_names,
            authority_manifest=manifest,
        )

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        segment: object | None = None,
    ) -> ResolvedPolicyCatalog:
        del request, conversation, source
        if (
            type(segment) is not SegmentExecution
            or type(segment.authority) is not SegmentExecutionAuthority
            or type(segment.context) is not ToolExecutionContext
            or segment.catalog is not None
        ):
            raise ValueError("policy resolver requires an unbound Segment authority")
        segment_catalog = self._fresh_segment_catalog(self._catalog)
        manifest = segment_catalog.authority_manifest
        if not isinstance(manifest, Mapping):
            raise ValueError("typed catalog manifest is unavailable")
        snapshot = validate_startup_policy(manifest)
        profile = snapshot.capability_profile
        authority = segment.authority
        if (
            authority.capability_profile_id != profile.profile_id
            or authority.capabilities != frozenset(profile.capabilities)
            or authority.capability_policy_version != snapshot.capability_policy_version
            or authority.binding_policy_version != snapshot.binding_policy_version
            or authority.capability_profile_fingerprint != snapshot.capability_profile_fingerprint
            or authority.binding_policy_fingerprint != snapshot.binding_policy_fingerprint
        ):
            raise ValueError("live policy drifted from Segment authority")
        return ResolvedPolicyCatalog(
            catalog=segment_catalog,
            policy=snapshot,
            dependency_policy=DEPENDENCY_POLICY_V1,
        )


class _ContinuationModelResolver:
    """The only production resolver allowed to construct ``ConfiguredAIClient``."""

    __slots__ = ("_injected", "_data_dir")

    def __init__(self, injected: ChatModel | None, data_dir: Path) -> None:
        self._injected = injected
        self._data_dir = data_dir

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        policy: object | None = None,
    ) -> ResolvedModel:
        del request, conversation
        config: Config = load_config(self._data_dir)
        if self._injected is None:
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

        def provider_error(error: Exception, current_config: Config = config) -> str:
            return _provider_error_message(error, current_config)

        return ResolvedModel(
            model=cast(ChatModel, model),
            config=config,
            auto_approve=config.chat_auto_approve_writes is True,
            provider_error_message=provider_error,
        )


class _SegmentContextResolver:
    """Bind one Source scope/revision to one execution-scoped authority/context."""

    __slots__ = (
        "_applications",
        "_events",
        "_notes",
        "_offers",
        "_resumes",
        "_jd_analyses",
        "_policy_snapshot",
    )

    def __init__(
        self,
        *,
        applications: object,
        events: object,
        notes: object,
        offers: object,
        resumes: object,
        jd_analyses: object,
        policy_snapshot: object,
    ) -> None:
        self._applications = applications
        self._events = events
        self._notes = notes
        self._offers = offers
        self._resumes = resumes
        self._jd_analyses = jd_analyses
        self._policy_snapshot = policy_snapshot

    @staticmethod
    def _scope(conversation: object, source: object) -> tuple[int, str, int | None, str, int]:
        values = {
            name: _attribute(source, name, None)
            for name in ("conversation_id", "context_type", "context_ref", "mode", "scope_revision")
        }
        conversation_id = values["conversation_id"]
        context_type = values["context_type"]
        context_ref_value = values["context_ref"]
        mode = values["mode"]
        revision = values["scope_revision"]
        if type(conversation_id) is not int or conversation_id <= 0:
            raise ValueError("invalid canonical conversation identity")
        if type(context_type) is not str or context_type not in {
            "workspace",
            "global",
            "application",
            "mode",
        }:
            raise ValueError("invalid canonical context type")
        if type(mode) is not str or not mode:
            raise ValueError("invalid canonical context mode")
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid canonical scope revision")
        conversation_identity = _attribute(conversation, "id")
        if conversation_identity != conversation_id:
            raise ValueError("source conversation identity changed")
        conversation_scope = (
            _attribute(conversation, "context_type"),
            _attribute(conversation, "context_ref"),
            _attribute(conversation, "mode"),
            _attribute(conversation, "scope_revision"),
        )
        if any(value is None for value in conversation_scope):
            raise ValueError("conversation scope is not canonical")
        source_ref = "" if context_ref_value is None else str(context_ref_value)
        conversation_ref = "" if conversation_scope[1] is None else str(conversation_scope[1])
        if (context_type, source_ref, mode, revision) != (
            conversation_scope[0],
            conversation_ref,
            conversation_scope[2],
            conversation_scope[3],
        ):
            raise ValueError("source scope changed before authority resolution")
        if context_type == "application":
            try:
                context_ref = int(str(context_ref_value or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid application scope identity") from exc
            if context_ref <= 0:
                raise ValueError("invalid application scope identity")
        else:
            context_ref = None
        return conversation_id, context_type, context_ref, mode, revision

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        recorder: object,
    ) -> SegmentExecution:
        del request
        snapshot = self._policy_snapshot
        profile = _attribute(snapshot, "capability_profile")
        capabilities = _attribute(profile, "capabilities", ())
        if not isinstance(capabilities, tuple):
            raise ValueError("capability profile is not immutable")
        conversation_id, context_type, context_ref, mode, revision = self._scope(
            conversation, source
        )
        factory = AuthorityFactory()
        try:
            trusted_scope = TrustedContextScope(cast(Any, context_type), context_ref, mode)
            authority = factory.create_segment_authority(
                conversation_id=conversation_id,
                conversation_scope_revision=revision,
                segment_id=uuid4().hex,
                trusted_scope=trusted_scope,
                capability_profile_id=str(_attribute(profile, "profile_id", "agent_typed_v1")),
                capabilities=frozenset(capabilities),
                capability_policy_version=str(
                    _attribute(snapshot, "capability_policy_version", "capability-policy-v1")
                ),
                binding_policy_version=str(
                    _attribute(snapshot, "binding_policy_version", "binding-policy-v1")
                ),
                capability_profile_fingerprint=str(
                    _attribute(snapshot, "capability_profile_fingerprint", "")
                ),
                binding_policy_fingerprint=str(
                    _attribute(snapshot, "binding_policy_fingerprint", "")
                ),
            )
            context = ToolExecutionContext(
                authority=authority,
                applications=cast(Any, self._applications),
                events=cast(Any, self._events),
                notes=cast(Any, self._notes),
                offers=cast(Any, self._offers),
                resumes=cast(Any, self._resumes),
                jd_analyses=cast(Any, self._jd_analyses),
                run_recorder=cast(Any, recorder),
            )
            return SegmentExecution(
                authority=authority,
                context=context,
                catalog=None,
                close=factory.close,
            )
        except BaseException:
            factory.close()
            raise


class _SegmentSurfaceGateResolver:
    """Freeze Catalog/Profile/Selector/Authority visibility before Provider."""

    def resolve(
        self,
        request: StartTurnRequest,
        conversation: object,
        source: object,
        assembled: object,
        policy: object,
        segment: object,
    ) -> SegmentSurfaceGate:
        del request, conversation, source
        if not isinstance(segment, SegmentExecution):
            raise TypeError("Segment surface gate requires SegmentExecution")
        if segment.catalog is not _attribute(policy, "catalog"):
            raise ValueError("Segment catalog drifted from policy catalog")
        if type(segment.context) is not ToolExecutionContext:
            raise TypeError("Segment surface gate requires exact ToolExecutionContext")
        if not isinstance(assembled, Sequence) or isinstance(assembled, (str, bytes)):
            raise TypeError("Segment surface gate requires assembled messages")
        messages = tuple(
            value
            if isinstance(value, Message)
            else Message(
                role=str(_attribute(value, "role", "assistant") or "assistant"),
                content=str(_attribute(value, "content", value) or ""),
                surface_contributor=str(_attribute(value, "surface_contributor", "") or ""),
                surface_signal=str(_attribute(value, "surface_signal", "") or ""),
                surface_revision=str(_attribute(value, "surface_revision", "") or ""),
                surface_page_kind=str(_attribute(value, "surface_page_kind", "") or ""),
                surface_attachment_kinds=str(
                    _attribute(value, "surface_attachment_kinds", "") or ""
                ),
            )
            for value in assembled
        )
        return build_segment_surface_gate(
            messages,
            catalog=cast(Any, segment.catalog),
            context=segment.context,
            authority=cast(Any, segment.authority),
            dependency_policy=cast(Any, _attribute(policy, "dependency_policy")),
            policy=cast(Any, _attribute(policy, "policy")),
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
        clarification_message: Callable[[tuple[PendingAction, str] | None, str], object | None],
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
        if isinstance(request, StartTurnRequest):
            # The current request is a mandatory, request-local surface
            # contributor.  It must be the final user message so the
            # provider-free Segment gate and the later projector cannot
            # accidentally select a historical user turn.
            values.append(
                Message(
                    role="user",
                    content=request.message,
                    surface_contributor="current_request",
                )
            )
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
        # Every Agent Loop seed gets one proposal gate.  The origin approved
        # write uses ``record_proposal=False``; only a chained Pending can
        # queue ``tool.proposed`` and it is released by Runtime after the
        # authoritative delivery atom succeeds.  The gate wraps the raw
        # invocation recorder exactly once; Context carries a stable proxy
        # whose delegate is switched to this gate for the active run.
        recorder = _ProposalJournalGate(invocation.run_recorder)
        context = invocation.tool_context
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("Agent Loop requires ToolExecutionContext")
        # The Context is the exact authority-bound carrier created by the
        # Source/Segment composition phase.  Only its recorder proxy may swap
        # the runtime journal delegate; never clone the Context here.
        set_recorder = getattr(context.run_recorder, "set_delegate", None)
        if callable(set_recorder):
            set_recorder(recorder)
        runtime_sink = cast(RuntimeEventSink | None, invocation.event_sink)
        agent_sink = _AgentEventAdapter(runtime_sink) if runtime_sink is not None else None
        try:
            result = self._runner.run(
                invocation,
                run_recorder=cast(Any, recorder),
                event_sink=agent_sink,
            )
            if not isinstance(result, AgentTurnResult):
                raise TypeError("Agent Loop must return AgentTurnResult")
            if result.pending is None:
                # A non-suspending turn owns the queued proposals itself.
                recorder.release_proposals()
            else:
                # A suspending turn is persisted by the delivery atom; its
                # journal suspension is the sole durable proposal writer.
                recorder.discard_proposals()
            return result
        except (RuntimeCancelled, RuntimeTransportAborted, RuntimeAgentTimedOut):
            recorder.discard_proposals()
            raise
        except ChatRunCancelled as exc:
            recorder.discard_proposals()
            raise RuntimeCancelled() from exc
        except BaseException:
            # Provider/Tool/transport failures must never leave deferred
            # proposal facts available for a later turn.
            recorder.discard_proposals()
            raise


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
        sqlalchemy_event.listen(repository.session_factory, "before_commit", self._before_commit)

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
                        _attribute(_attribute(state, "identity"), "conversation_id", 0) or 0,
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
    clarification_message: Callable[[tuple[PendingAction, str] | None, str], object | None],
    page_context_messages: Callable[[Mapping[str, object] | None], Sequence[object]],
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
    policy_resolver = _PolicyCatalogResolver(catalog)
    continuation_model_resolver = _ContinuationModelResolver(chat_model, data_dir)
    surface_gate_resolver = _SegmentSurfaceGateResolver()
    policy_snapshot = validate_startup_policy(
        cast(Mapping[str, object], getattr(catalog, "authority_manifest"))
    )
    segment_resolver = _SegmentContextResolver(
        applications=applications,
        events=events,
        notes=notes,
        offers=offers,
        resumes=resumes,
        jd_analyses=jd_analyses,
        policy_snapshot=policy_snapshot,
    )
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

    def resolve_approval_context(
        *,
        operation: object,
        pending: object,
        conversation_id: int,
        pending_action_revision: int,
        effective_args_digest: str,
    ) -> ToolExecutionContext:
        if write_operations is None:
            raise WriteOperationError("operation_unavailable")
        factory = AuthorityFactory()
        try:
            authority = ApprovalAuthorityResolver(
                write_operations,
                factory,
                capabilities=frozenset(policy_snapshot.capability_profile.capabilities),
                capability_profile_id=policy_snapshot.capability_profile.profile_id,
                capability_policy_version=policy_snapshot.capability_policy_version,
                binding_policy_version=policy_snapshot.binding_policy_version,
                capability_profile_fingerprint=policy_snapshot.capability_profile_fingerprint,
                binding_policy_fingerprint=policy_snapshot.binding_policy_fingerprint,
            ).resolve(
                operation=operation,
                pending=pending,
                conversation_id=conversation_id,
                pending_action_revision=pending_action_revision,
                effective_args_digest=effective_args_digest,
            )
            return ToolExecutionContext(
                authority=authority,
                applications=cast(Any, applications),
                events=cast(Any, events),
                notes=cast(Any, notes),
                offers=cast(Any, offers),
                resumes=cast(Any, resumes),
                jd_analyses=cast(Any, jd_analyses),
                run_recorder=NullRunRecorder(),
            )
        except BaseException:
            factory.close()
            raise

    confirmation = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=cast(Any, persistence),
            write_operations=cast(Any, write_operations),
            write_coordinator=cast(Any, write_coordinator),
            catalog=catalog,
            approval_context_resolver=resolve_approval_context,
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
        policy_catalog_resolver=policy_resolver,
        segment_context_resolver=segment_resolver,
        surface_gate_resolver=surface_gate_resolver,
        continuation_model_resolver=continuation_model_resolver,
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
