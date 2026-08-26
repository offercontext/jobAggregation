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
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, NoReturn, cast
from uuid import uuid4

from sqlalchemy import event as sqlalchemy_event, select, update

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
from offerpilot.ai.tool_runtime.catalog import (
    ToolCatalog as RuntimeToolCatalog,
    compile_tool_metadata_manifest,
)
from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.legacy import (
    LegacyInitialRouteComponents,
    LegacyRouteSourceV1,
    LegacyStaticAdapterCatalogV1,
    build_unpublished_legacy_initial_route_components,
)
from offerpilot.ai.tool_runtime.metadata import (
    ProviderToolMetadataView,
    ToolAuthorityMetadataView,
    ToolDiscoveryMetadataView,
    ToolMetadataBundleV1,
    ToolOperationMetadataPort,
    canonical_json_bytes,
    freeze_json,
)
from offerpilot.ai.tool_runtime.protocol_seals import verify_legacy_boundary
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.tool_specs.legacy import (
    build_legacy_deterministic_catalog,
    build_static_legacy_adapter_catalog,
)
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
from offerpilot.pilot_runtime.compensation import (
    CompensationHandlerRegistry,
    prepare_compensation_handler_components,
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
from offerpilot.pilot_runtime.legacy_route import (
    LegacyConfirmationRouteComponents,
    LegacyCompositeRouteVerifier,
    LegacyPendingIdentityVerifierPort,
    build_legacy_composite_route_verifier,
    build_legacy_pending_identity_verifier_port,
    build_unpublished_legacy_confirmation_components,
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

    __slots__ = (
        "_catalog",
        "_provider_view",
        "_discovery_view",
        "_authority_view",
    )

    def __init__(
        self,
        catalog: object,
        *,
        provider_view: ProviderToolMetadataView,
        discovery_view: ToolDiscoveryMetadataView,
        authority_view: ToolAuthorityMetadataView,
    ) -> None:
        if type(catalog) is not RuntimeToolCatalog:
            raise TypeError("policy resolver requires the exact Typed Catalog")
        token = provider_view.bundle_instance_token
        if (
            discovery_view.bundle_instance_token is not token
            or authority_view.bundle_instance_token is not token
        ):
            raise ValueError("policy resolver metadata views have mixed Bundle provenance")
        self._catalog = catalog
        self._provider_view = provider_view
        self._discovery_view = discovery_view
        self._authority_view = authority_view

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
        segment_catalog = self._catalog
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
            dependency_policy=self._discovery_view.policy,
            provider_metadata_view=self._provider_view,
            discovery_metadata_view=self._discovery_view,
            authority_metadata_view=self._authority_view,
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

    __slots__ = ("_provider_view", "_discovery_view", "_authority_view")

    def __init__(
        self,
        *,
        provider_view: ProviderToolMetadataView,
        discovery_view: ToolDiscoveryMetadataView,
        authority_view: ToolAuthorityMetadataView,
    ) -> None:
        token = provider_view.bundle_instance_token
        if (
            discovery_view.bundle_instance_token is not token
            or authority_view.bundle_instance_token is not token
        ):
            raise ValueError("surface resolver metadata views have mixed Bundle provenance")
        self._provider_view = provider_view
        self._discovery_view = discovery_view
        self._authority_view = authority_view

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
        if (
            _attribute(policy, "provider_metadata_view") is not self._provider_view
            or _attribute(policy, "discovery_metadata_view") is not self._discovery_view
            or _attribute(policy, "authority_metadata_view") is not self._authority_view
        ):
            raise ValueError("Segment policy metadata views drifted from the Runtime Bundle")
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
            provider_view=self._provider_view,
            discovery_view=self._discovery_view,
            authority_metadata_view=self._authority_view,
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


_PRODUCTION_METADATA_COMPONENT_SEAL = object()


def _legacy_manifest_from_adapters(
    catalog: LegacyStaticAdapterCatalogV1,
) -> dict[str, object]:
    catalog.require_integrity()
    adapters = catalog.ordered_adapters
    return {
        "boundary_version": "legacy-deterministic-boundary-v1",
        "provider_visibility": "forbidden",
        "adapter_kind": "legacy_deterministic",
        "ordered_names": [adapter.name for adapter in adapters],
        "chained_policies": [adapter.chained_policy for adapter in adapters],
        "initial_route_bindings": [
            {
                "route_source": source.value,
                "adapter_ordinal": adapter.ordinal,
            }
            for adapter in adapters
            for source in adapter.initial_route_sources
        ],
    }


def _legacy_manifest_from_initial_registry(
    components: LegacyInitialRouteComponents,
) -> dict[str, object]:
    catalog = components.catalog
    adapters = catalog.ordered_adapters
    owner = components.owner_lease_factory.open()
    routes: list[dict[str, object]] = []
    try:
        for adapter in adapters:
            for source in adapter.initial_route_sources:
                issuer = components.initial_issuer_for(source)
                lease = issuer.open_request_lease(owner)
                token = issuer.issue(lease)
                handle = components.initial_route_port.resolve_initial(token)
                binding = components.initial_route_port.require_route(handle)
                routes.append(
                    {
                        "route_source": source.value,
                        "adapter_ordinal": binding.ordinal,
                    }
                )
    finally:
        owner.close()
    projection = _legacy_manifest_from_adapters(catalog)
    projection["initial_route_bindings"] = routes
    return projection


def _same_canonical_projection(
    left: Mapping[str, object],
    right: object,
) -> bool:
    if not isinstance(right, Mapping):
        return False
    return canonical_json_bytes(freeze_json(left)) == canonical_json_bytes(
        freeze_json(cast(Mapping[str, object], right))
    )


class ProductionToolMetadataComponents(TransientToolRuntimeValue):
    """One sealed application-level Tool Metadata graph."""

    __slots__ = (
        "_bundle",
        "_operation_port",
        "_compensation_registry",
        "_initial_routes",
        "_confirmation_routes",
        "_legacy_route_verifier",
        "_integrity_seal",
    )
    _bundle: ToolMetadataBundleV1
    _operation_port: ToolOperationMetadataPort
    _compensation_registry: CompensationHandlerRegistry
    _initial_routes: LegacyInitialRouteComponents
    _confirmation_routes: LegacyConfirmationRouteComponents
    _legacy_route_verifier: LegacyCompositeRouteVerifier
    _integrity_seal: tuple[object, ...]

    def __new__(
        cls,
        seal: object | None = None,
        **_kwargs: object,
    ) -> "ProductionToolMetadataComponents":
        if seal is not _PRODUCTION_METADATA_COMPONENT_SEAL:
            raise TypeError("production Tool Metadata components are Composition-created")
        return object.__new__(cls)

    def __init__(
        self,
        seal: object | None = None,
        *,
        bundle: ToolMetadataBundleV1,
        operation_port: ToolOperationMetadataPort,
        compensation_registry: CompensationHandlerRegistry,
        initial_routes: LegacyInitialRouteComponents,
        confirmation_routes: LegacyConfirmationRouteComponents,
        legacy_route_verifier: LegacyCompositeRouteVerifier,
    ) -> None:
        if seal is not _PRODUCTION_METADATA_COMPONENT_SEAL:
            raise TypeError("production Tool Metadata components are Composition-created")
        values = (
            bundle,
            operation_port,
            compensation_registry,
            initial_routes,
            confirmation_routes,
            legacy_route_verifier,
        )
        for name, value in zip(self.__slots__[:-1], values):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_integrity_seal", values)
        self._ensure_integrity()

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("production Tool Metadata components are sealed")

    def _ensure_integrity(self) -> None:
        try:
            current = (
                self._bundle,
                self._operation_port,
                self._compensation_registry,
                self._initial_routes,
                self._confirmation_routes,
                self._legacy_route_verifier,
            )
            if (
                type(self._integrity_seal) is not tuple
                or len(self._integrity_seal) != len(current)
                or any(
                    expected is not actual
                    for expected, actual in zip(self._integrity_seal, current)
                )
            ):
                raise ValueError("production Tool Metadata component identity drift")
            token = self._bundle.bundle_instance_token
            if (
                self._operation_port.bundle_instance_token is not token
                or self._compensation_registry.bundle_instance_token is not token
                or self._initial_routes.initial_route_port.bundle_instance_token is not token
                or self._confirmation_routes.bundle_instance_token is not token
                or self._confirmation_routes.catalog.bundle_instance_token is not token
                or self._legacy_route_verifier.bundle_instance_token is not token
                or self._operation_port.legacy_route_registry_token
                is not self._legacy_route_verifier.registry_token
            ):
                raise ValueError("production Tool Metadata Bundle provenance drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("production Tool Metadata component integrity drift") from exc

    @property
    def bundle(self) -> ToolMetadataBundleV1:
        self._ensure_integrity()
        return self._bundle

    @property
    def operation_port(self) -> ToolOperationMetadataPort:
        self._ensure_integrity()
        return self._operation_port

    @property
    def compensation_registry(self) -> CompensationHandlerRegistry:
        self._ensure_integrity()
        return self._compensation_registry

    @property
    def initial_routes(self) -> LegacyInitialRouteComponents:
        self._ensure_integrity()
        return self._initial_routes

    @property
    def confirmation_routes(self) -> LegacyConfirmationRouteComponents:
        self._ensure_integrity()
        return self._confirmation_routes


def build_production_tool_metadata_components(
    *,
    pending_identity_verifier_port: LegacyPendingIdentityVerifierPort,
) -> ProductionToolMetadataComponents:
    """Assemble and publish the closed 25/3/4 Tool Metadata graph atomically."""

    if type(pending_identity_verifier_port) is not LegacyPendingIdentityVerifierPort:
        raise TypeError("production metadata requires the exact Legacy verifier Port")
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    manifest_projection = manifest.to_dict()
    expected_legacy = manifest_projection["legacy_boundary"]
    legacy_catalog = build_static_legacy_adapter_catalog()
    actual_legacy = _legacy_manifest_from_adapters(legacy_catalog)
    verify_legacy_boundary(
        tuple(adapter.name for adapter in legacy_catalog.ordered_adapters),
        "forbidden",
        "legacy_deterministic",
    )
    if not _same_canonical_projection(actual_legacy, expected_legacy):
        raise ValueError("actual Legacy Adapter policy does not match the exact Manifest")
    compensation_components = prepare_compensation_handler_components()
    bundle = ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=actual_legacy,
        compensation=compensation_components.metadata_projection(),
    )
    runtime_container_token = object()
    initial_routes = build_unpublished_legacy_initial_route_components(
        catalog=legacy_catalog,
        legacy_boundary=bundle.legacy_boundary(),
        runtime_container_token=runtime_container_token,
    )
    actual_initial = _legacy_manifest_from_initial_registry(initial_routes)
    if not _same_canonical_projection(actual_initial, expected_legacy):
        raise ValueError("actual Legacy initial-route policy does not match the exact Manifest")
    confirmation_routes = build_unpublished_legacy_confirmation_components(
        catalog=legacy_catalog,
        legacy_boundary=bundle.legacy_boundary(),
        runtime_container_token=runtime_container_token,
        pending_identity_verifier_port=pending_identity_verifier_port,
    )
    legacy_route_verifier = build_legacy_composite_route_verifier(
        initial_route_port=initial_routes.initial_route_port,
        proof_consumer_port=confirmation_routes.proof_consumer_port,
        legacy_boundary=bundle.legacy_boundary(),
    )
    compensation_registry = compensation_components.bind(bundle.compensation_view())
    operation_port = ToolOperationMetadataPort(
        operation_view=bundle.operation_view(),
        legacy_boundary=bundle.legacy_boundary(),
        compensation_view=bundle.compensation_view(),
        compensation_registry=compensation_registry,
        legacy_route_issuer_port=legacy_route_verifier,
    )
    return ProductionToolMetadataComponents(
        _PRODUCTION_METADATA_COMPONENT_SEAL,
        bundle=bundle,
        operation_port=operation_port,
        compensation_registry=compensation_registry,
        initial_routes=initial_routes,
        confirmation_routes=confirmation_routes,
        legacy_route_verifier=legacy_route_verifier,
    )


class _SqlAlchemyLegacyPendingIdentityBackend:
    """Caller-transaction adapter for the sealed Legacy proof verifier."""

    __slots__ = ()

    @staticmethod
    def _snapshot(
        session: object,
        lookup_identity: object,
        confirmation_input: object,
    ) -> dict[str, object]:
        from offerpilot.models import Conversation, WriteOperation

        conversation_id = getattr(lookup_identity, "conversation_id", None)
        requested_operation_id = getattr(confirmation_input, "operation_id", None)
        if type(conversation_id) is not int or conversation_id <= 0:
            raise WriteOperationError("operation_identity_conflict")
        getter = getattr(session, "get", None)
        if not callable(getter):
            raise WriteOperationError("operation_unavailable")
        conversation = getter(Conversation, conversation_id)
        if conversation is None or getattr(conversation, "archived_at", None) is not None:
            raise WriteOperationError("stale_pending_action")
        pending_operation_id = str(getattr(conversation, "pending_operation_id", "") or "")
        operation_id = requested_operation_id or pending_operation_id
        if type(operation_id) is not str or not operation_id:
            raise WriteOperationError("stale_pending_action")
        operation = getter(WriteOperation, operation_id)
        if operation is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        raw_args = str(getattr(conversation, "pending_args", "") or "")
        try:
            normalized_args = json.loads(raw_args)
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_integrity_error") from exc
        if not isinstance(normalized_args, Mapping):
            raise WriteOperationError("operation_integrity_error")
        pending_tool_call_id = str(getattr(conversation, "pending_tool_call_id", "") or "")
        pending_tool_name = str(getattr(conversation, "pending_tool_name", "") or "")
        if (
            pending_operation_id != operation_id
            or getattr(operation, "operation_role", None) != "primary"
            or getattr(operation, "adapter_kind", None) != "legacy_deterministic"
            or getattr(operation, "status", None) != "proposed"
            or getattr(operation, "conversation_id", None) != conversation_id
            or getattr(operation, "tool_call_id", None) != pending_tool_call_id
            or getattr(operation, "tool_name", None) != pending_tool_name
        ):
            raise WriteOperationError("operation_identity_conflict")
        return {
            "adapter_kind": "legacy_deterministic",
            "operation_role": "primary",
            "route_source": "confirmation_resume",
            "conversation_id": conversation_id,
            "conversation_scope_revision": getattr(conversation, "scope_revision", None),
            "pending_operation_id": pending_operation_id,
            "operation_id": operation_id,
            "tool_call_id": pending_tool_call_id,
            "tool_name": pending_tool_name,
            "fingerprint_key_id": getattr(operation, "fingerprint_key_id", None),
            "raw_args": raw_args,
            "normalized_args": dict(normalized_args),
            "proposal_fingerprint": getattr(operation, "proposal_fingerprint", None),
            "confirmation_token_fingerprint": getattr(
                operation,
                "confirmation_token_fingerprint",
                None,
            ),
            "authorization_scope_fingerprint": getattr(
                operation,
                "authorization_scope_fingerprint",
                None,
            ),
            "input_fingerprint": getattr(operation, "input_fingerprint", None),
            "operation_request_fingerprint": getattr(
                operation,
                "operation_request_fingerprint",
                None,
            ),
            "pending_confirmation_claim_id": str(
                getattr(conversation, "pending_confirmation_claim_id", "") or ""
            ),
            "pending_confirmation_claimed_at": getattr(
                conversation,
                "pending_confirmation_claimed_at",
                None,
            ),
        }

    def read_snapshot(
        self,
        session: object,
        lookup_identity: object,
        confirmation_input: object,
    ) -> dict[str, object]:
        return self._snapshot(session, lookup_identity, confirmation_input)

    def locked_recheck(
        self,
        session: object,
        lookup_identity: object,
        confirmation_input: object,
    ) -> dict[str, object]:
        return self._snapshot(session, lookup_identity, confirmation_input)

    def claim_cas(
        self,
        session: object,
        lookup_identity: object,
        confirmation_input: object,
    ) -> dict[str, object]:
        from offerpilot.models import Conversation

        snapshot = self._snapshot(session, lookup_identity, confirmation_input)
        claimed_at = datetime.now(timezone.utc)
        executor = getattr(session, "execute", None)
        if not callable(executor):
            raise WriteOperationError("operation_unavailable")
        result = executor(
            update(Conversation)
            .where(Conversation.id == snapshot["conversation_id"])
            .where(Conversation.archived_at.is_(None))
            .where(Conversation.scope_revision == snapshot["conversation_scope_revision"])
            .where(Conversation.pending_operation_id == snapshot["operation_id"])
            .where(Conversation.pending_tool_call_id == snapshot["tool_call_id"])
            .where(Conversation.pending_tool_name == snapshot["tool_name"])
            .where(Conversation.pending_args == snapshot["raw_args"])
            .where(Conversation.pending_confirmation_claim_id == "")
            .where(Conversation.pending_confirmation_claimed_at.is_(None))
            .values(
                pending_confirmation_claim_id=snapshot["operation_id"],
                pending_confirmation_claimed_at=claimed_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise WriteOperationError("confirmation_claim_lost")
        claim_row = executor(
            select(
                Conversation.pending_confirmation_claim_id,
                Conversation.pending_confirmation_claimed_at,
            ).where(Conversation.id == snapshot["conversation_id"])
        ).one_or_none()
        if claim_row is None:
            raise WriteOperationError("operation_integrity_error")
        claim_id, persisted_claimed_at = claim_row
        if claim_id != snapshot["operation_id"] or persisted_claimed_at is None:
            raise WriteOperationError("operation_integrity_error")
        claimed_snapshot = dict(snapshot)
        claimed_snapshot["pending_confirmation_claim_id"] = claim_id
        claimed_snapshot["pending_confirmation_claimed_at"] = persisted_claimed_at
        return claimed_snapshot


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
    title_from_message: Callable[[str], str] | None = None,
    catalog: object = MODEL_TOOL_CATALOG,
    application_visible: Callable[[int], bool] | None = None,
    clock: Callable[[], object] | None = None,
) -> PilotRuntime:
    """Build one frozen production Runtime graph from app-owned dependencies."""

    if catalog is not MODEL_TOOL_CATALOG:
        raise ValueError("Pilot Runtime requires the exact Bundle-owned Typed Catalog")
    if (
        type(write_operations) is not WriteOperationRepository
        or type(write_coordinator) is not WriteOperationCoordinator
        or write_coordinator.repository is not write_operations
    ):
        raise WriteOperationError("operation_unavailable")
    pending_identity_verifier = build_legacy_pending_identity_verifier_port(
        backend=_SqlAlchemyLegacyPendingIdentityBackend(),
        ledger_key=write_operations.key,
    )
    metadata_components = build_production_tool_metadata_components(
        pending_identity_verifier_port=pending_identity_verifier,
    )
    metadata_bundle = metadata_components.bundle
    initial_routes = metadata_components.initial_routes
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
    policy_resolver = _PolicyCatalogResolver(
        catalog,
        provider_view=metadata_bundle.provider_view(),
        discovery_view=metadata_bundle.discovery_view(),
        authority_view=metadata_bundle.authority_view(),
    )
    continuation_model_resolver = _ContinuationModelResolver(chat_model, data_dir)
    surface_gate_resolver = _SegmentSurfaceGateResolver(
        provider_view=metadata_bundle.provider_view(),
        discovery_view=metadata_bundle.discovery_view(),
        authority_view=metadata_bundle.authority_view(),
    )
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
            legacy_request_owner_lease_factory=initial_routes.owner_lease_factory,
            legacy_initial_route_port=initial_routes.initial_route_port,
            legacy_jd_clarification_issuer=initial_routes.initial_issuer_for(
                LegacyRouteSourceV1("jd_clarification")
            ),
            legacy_jd_deterministic_action_issuer=initial_routes.initial_issuer_for(
                LegacyRouteSourceV1("jd_deterministic_action")
            ),
            legacy_submission_snapshot_issuer=initial_routes.initial_issuer_for(
                LegacyRouteSourceV1("submission_snapshot_action")
            ),
            legacy_outcome_recording_issuer=initial_routes.initial_issuer_for(
                LegacyRouteSourceV1("outcome_recording_action")
            ),
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
        metadata_bundle=metadata_bundle,
        metadata_components=metadata_components,
        provider_metadata_view=metadata_bundle.provider_view(),
        discovery_metadata_view=metadata_bundle.discovery_view(),
        authority_metadata_view=metadata_bundle.authority_view(),
        missing_target_question=missing_target_question,
        pending_action_details=pending_action_details,
        application_visible=visible,
        deterministic=deterministic,
        confirmation_coordinator=confirmation,
    )
    return PilotRuntime(dependencies)


__all__ = [
    "ProductionToolMetadataComponents",
    "build_pilot_runtime",
    "build_production_tool_metadata_components",
]
