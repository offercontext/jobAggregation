from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from threading import Lock
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from offerpilot.ai.agent_contracts import (
    _ASDICT_GUARD as _TRANSIENT_ASDICT_GUARD,
    AgentAssistantDelta,
    AgentLoopEvent,
    AgentEventSink,
    AgentRuntimeSignalSink,
    AgentToolCall,
    AgentToolResult,
    AgentTurnResult,
    ApprovedWriteContinuation,
    CancelCheck,
    ChatModel,
    ChatRunCancelled,
    PendingAction,
    PendingActionValidationError,
    JsonValue,
    StalePendingActionError,
)
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    AuthorityPhaseError,
    AuthorityUse,
    NewTurnPrepareCallIdentity,
    PendingAuthorityClaim,
    ProviderInvocationIdentity,
    SegmentExecutionAuthority,
)
from offerpilot.ai.tool_authority.contracts import _ReplacementProtected
from offerpilot.ai.tool_runtime.catalog import SegmentToolCatalogLease, ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ProviderToolContract,
    ReadyToExecute,
    ToolExecutionRecord,
    ToolFailure,
    ToolResultMetadata,
    ToolSpec,
    TransientToolRuntimeValue,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    ProviderToolMetadataView,
    ToolAuthorityEntryV1,
    ToolAuthorityMetadataView,
    ToolDiscoveryMetadataView,
)
from offerpilot.ai.tool_runtime.policy_types import OperationKind
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.transport import project_transport_event
from offerpilot.ai.tool_runtime.validation import ArgumentValidationError, parse_arguments
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.agent_runtime.journal import EventInput, RunRecorder
from offerpilot.config import AIProviderProfile
from offerpilot.context_projector.binding import ModelCallSurfaceBinding
from offerpilot.context_projector.budget import ProviderBudget
from offerpilot.context_projector.chunking import chunk_structured_source
from offerpilot.context_projector.contracts import (
    CONTRIBUTOR_ORDER,
    ContributorResult,
    ContributorStatus,
    FrozenMessage,
    FrozenModelSurface,
    FrozenSource,
    ProjectionError,
    canonical_json,
    sha256_hex,
)
from offerpilot.context_projector.gateway import (
    AgentProviderGatewaySession,
    FrozenProviderExecutionChain,
    SingleCandidateAgentTransport,
)
from offerpilot.context_projector.projector import ModelSurfaceProjector, ProjectionRequest
from offerpilot.context_projector.selector import (
    ToolSelectionResult,
    ToolSelectionSignals,
    select_tools,
)
from offerpilot.context_projector.authority_surface import (
    AuthoritySurfaceView,
    intersect_authority_surface,
)


DEFAULT_MAX_ITERATIONS = 20
_NO_OVERRIDE = object()


class _InjectedSurfaceAdapter:
    def __init__(self, inner: ChatModel) -> None:
        self._inner = inner
        profile = AIProviderProfile(
            id="injected-agent-model",
            provider="openai_compatible",
            api_key="injected-runtime-only",
            base_url="https://injected.invalid/v1",
            model="injected-agent-model",
            enabled=True,
        )
        self._chain = FrozenProviderExecutionChain.freeze([profile])
        self._transport = SingleCandidateAgentTransport(self._complete, self._stream)
        self._gateway = AgentProviderGatewaySession(self._chain, self._transport)

    def new_agent_provider_session(self) -> AgentProviderGatewaySession:
        return AgentProviderGatewaySession(self._chain, self._transport)

    @property
    def agent_provider_budgets(self) -> tuple[ProviderBudget, ...]:
        return self._gateway.budgets

    @property
    def agent_provider_manifest_identities(self) -> tuple[str, ...]:
        return self._gateway.manifest_identities

    def bind_agent_provider_surface(
        self,
        *,
        authority: SegmentExecutionAuthority,
        build_identity: object,
        surface: FrozenModelSurface,
        model_call_surface_binding: ModelCallSurfaceBinding,
    ) -> ProviderInvocationIdentity:
        return self._gateway.bind_provider_surface(
            authority=authority,
            build_identity=cast(Any, build_identity),
            surface=surface,
            model_call_surface_binding=model_call_surface_binding,
        )

    def preflight_agent_surface(
        self,
        surface: FrozenModelSurface,
        *,
        invocation_identity: ProviderInvocationIdentity,
        stream: bool,
    ) -> None:
        self._gateway.preflight(
            surface,
            invocation_identity=invocation_identity,
            stream=stream,
        )

    def complete_agent_surface(
        self,
        surface: FrozenModelSurface,
        *,
        invocation_identity: ProviderInvocationIdentity,
        before_attempt: Callable[[], None] | None = None,
    ) -> object:
        return self._gateway.complete(
            surface,
            invocation_identity=invocation_identity,
            before_attempt=before_attempt,
        )

    def stream_agent_surface(
        self,
        surface: FrozenModelSurface,
        on_delta: Any,
        *,
        invocation_identity: ProviderInvocationIdentity,
        before_attempt: Callable[[], None] | None = None,
    ) -> object:
        return self._gateway.stream_deferred(
            surface,
            on_delta,
            invocation_identity=invocation_identity,
            before_attempt=before_attempt,
        )

    def consume_agent_provider_attempt(self, attempt_id: str) -> bool:
        return self._gateway.consume_attempt(attempt_id)

    def _complete(
        self,
        _candidate: object,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None,
    ) -> Assistant:
        if response_format is None:
            return cast(Assistant, self._inner.complete(messages, tools))
        return cast(Assistant, self._inner.complete(messages, tools, response_format))

    def _stream(
        self,
        _candidate: object,
        messages: list[Message],
        tools: list[ProviderToolContract],
        on_delta: Any,
    ) -> Assistant:
        stream = getattr(self._inner, "stream_complete", None)
        if callable(stream):
            return cast(Assistant, stream(messages, tools, on_delta))
        return cast(Assistant, self._inner.complete(messages, tools))


class _PerCallSurfaceModel:
    """Bind one exact gateway session to one model-call surface."""

    def __init__(self, inner: ChatModel, gateway: AgentProviderGatewaySession) -> None:
        self._inner = inner
        self._gateway = gateway

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def agent_provider_budgets(self) -> tuple[ProviderBudget, ...]:
        return tuple(self._gateway.budgets)

    @property
    def agent_provider_manifest_identities(self) -> tuple[str, ...]:
        return tuple(self._gateway.manifest_identities)

    def bind_agent_provider_surface(self, **kwargs: object) -> ProviderInvocationIdentity:
        return self._gateway.bind_provider_surface(**cast(Any, kwargs))

    def preflight_agent_surface(self, surface: FrozenModelSurface, **kwargs: object) -> None:
        self._gateway.preflight(surface, **cast(Any, kwargs))

    def complete_agent_surface(self, surface: FrozenModelSurface, **kwargs: object) -> object:
        return self._gateway.complete(surface, **cast(Any, kwargs))

    def stream_agent_surface(
        self,
        surface: FrozenModelSurface,
        on_delta: Callable[[str], None],
        **kwargs: object,
    ) -> object:
        return self._gateway.stream_deferred(surface, on_delta, **cast(Any, kwargs))

    def consume_agent_provider_attempt(self, attempt_id: str) -> bool:
        return self._gateway.consume_attempt(attempt_id)


def _surface_selection_matches(
    left: ToolSelectionResult,
    right: ToolSelectionResult,
) -> bool:
    if (
        left.selected_names != right.selected_names
        or left.provider_envelope_fingerprint != right.provider_envelope_fingerprint
        or left.full_catalog_fallback != right.full_catalog_fallback
        or left.selected_domains != right.selected_domains
        or left.dependency_closure != right.dependency_closure
        or left.fallback_reason != right.fallback_reason
        or left.diagnostics != right.diagnostics
        or len(left.provider_contracts) != len(right.provider_contracts)
    ):
        return False
    return all(
        left_contract is right_contract
        for left_contract, right_contract in zip(
            left.provider_contracts,
            right.provider_contracts,
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class _SurfaceDependencyPolicySeal:
    """Bundle-derived dependency topology used until Task 10 owns the gate."""

    version: str
    catalog_names: tuple[str, ...]
    dependencies: Mapping[str, frozenset[str]] = field(repr=False)
    canonical_fingerprint: str

    @classmethod
    def from_view(
        cls,
        view: ToolDiscoveryMetadataView,
        *,
        version: str,
    ) -> "_SurfaceDependencyPolicySeal":
        entries = view.ordered_entries
        return cls(
            version=version,
            catalog_names=tuple(entry.provider_name for entry in entries),
            dependencies=MappingProxyType(
                {entry.provider_name: frozenset(entry.dependencies) for entry in entries}
            ),
            canonical_fingerprint=view.discovery_fingerprint,
        )

    def validate_closed(
        self,
        selected_names: tuple[str, ...],
        available_names: tuple[str, ...],
    ) -> None:
        if available_names != self.catalog_names:
            raise ProjectionError("dependency_catalog_mismatch")
        selected = frozenset(selected_names)
        if not selected or any(name not in self.dependencies for name in selected):
            raise ProjectionError("invalid_tool_surface")
        if any(
            not dependencies.issubset(selected)
            for name, dependencies in self.dependencies.items()
            if name in selected
        ):
            raise ProjectionError("tool_dependency_not_closed")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SegmentSurfaceGate(_ReplacementProtected, TransientToolRuntimeValue):
    """Provider-free selector/authority intersection for one Segment.

    The gate is sealed before a continuation model is resolved.  The Agent
    Loop may still project messages and budgets for each model call, but it
    consumes this exact selection rather than re-running selector or
    capability/scope intersection inside the loop.
    """

    authority: SegmentExecutionAuthority = field(repr=False)
    context: ToolExecutionContext = field(repr=False)
    catalog_lease: SegmentToolCatalogLease = field(repr=False)
    dispatch_catalog: ToolCatalog = field(repr=False)
    provider_view: ProviderToolMetadataView = field(repr=False)
    discovery_view: ToolDiscoveryMetadataView = field(repr=False)
    authority_metadata_view: ToolAuthorityMetadataView = field(repr=False)
    authority_surface: AuthoritySurfaceView = field(repr=False)
    selection: ToolSelectionResult = field(repr=False)
    dependency_policy: _SurfaceDependencyPolicySeal = field(repr=False)
    policy: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        self._validate_current_surface()
        self._seal_replacement()

    def _validate_current_surface(self) -> None:
        if type(self.authority) is not SegmentExecutionAuthority:
            raise ProjectionError("segment_authority_required")
        if type(self.context) is not ToolExecutionContext:
            raise ProjectionError("tool_context_required")
        if self.context.authority is not self.authority:
            raise ProjectionError("segment_context_mismatch")
        if type(self.catalog_lease) is not SegmentToolCatalogLease:
            raise ProjectionError("segment_catalog_lease_required")
        if self.catalog_lease.closed:
            raise ProjectionError("segment_catalog_lease_closed")
        if type(self.dispatch_catalog) is not ToolCatalog:
            raise ProjectionError("typed_catalog_required")
        try:
            self.catalog_lease.require_catalog(self.dispatch_catalog)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ProjectionError("segment_dispatch_catalog_mismatch") from exc
        if type(self.provider_view) is not ProviderToolMetadataView:
            raise ProjectionError("provider_metadata_view_required")
        if type(self.discovery_view) is not ToolDiscoveryMetadataView:
            raise ProjectionError("discovery_metadata_view_required")
        if type(self.authority_metadata_view) is not ToolAuthorityMetadataView:
            raise ProjectionError("authority_metadata_view_required")
        if not (
            self.catalog_lease.bundle_instance_token
            is self.provider_view.bundle_instance_token
            is self.discovery_view.bundle_instance_token
            is self.authority_metadata_view.bundle_instance_token
            is self.selection.bundle_instance_token
        ):
            raise ProjectionError("cross_Bundle_metadata_views")
        provider_contracts = self.provider_view.ordered_contracts
        dispatch_contracts = tuple(spec.contract for spec in self.dispatch_catalog.specs)
        if len(dispatch_contracts) != len(provider_contracts) or any(
            actual is not expected
            for actual, expected in zip(dispatch_contracts, provider_contracts)
        ):
            raise ProjectionError("provider_surface_mismatch")
        expected_view = AuthoritySurfaceView.from_authority(self.authority)
        if self.authority_surface != expected_view:
            raise ProjectionError("authority_surface_mismatch")
        profile = getattr(self.policy, "capability_profile", None)
        if profile is None:
            raise ProjectionError("capability_profile_required")
        if (
            getattr(profile, "profile_id", None) != self.authority.capability_profile_id
            or getattr(self.policy, "capability_policy_version", None)
            != self.authority.capability_policy_version
            or getattr(self.policy, "binding_policy_version", None)
            != self.authority.binding_policy_version
            or getattr(self.policy, "capability_profile_fingerprint", None)
            != self.authority.capability_profile_fingerprint
            or getattr(self.policy, "binding_policy_fingerprint", None)
            != self.authority.binding_policy_fingerprint
        ):
            raise ProjectionError("capability_profile_drift")
        if (
            not self.selection.provider_contracts
            or tuple(contract.name for contract in self.selection.provider_contracts)
            != self.selection.selected_names
        ):
            raise ProjectionError("invalid_tool_surface")
        if any(
            not any(contract is available for available in provider_contracts)
            for contract in self.selection.provider_contracts
        ):
            raise ProjectionError("preselected_surface_mismatch")
        self.dependency_policy.validate_closed(
            self.selection.dependency_closure,
            tuple(contract.name for contract in provider_contracts),
        )
        try:
            projected = intersect_authority_surface(
                self.discovery_view,
                self.authority_metadata_view,
                self.selection,
                self.authority_surface,
            )
        except ProjectionError:
            raise
        if not _surface_selection_matches(projected, self.selection):
            raise ProjectionError("preselected_surface_mismatch")


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedContinuationSegment(TransientToolRuntimeValue):
    """Fresh Segment services activated after an approved write is fenced.

    The approval port is deliberately the only producer of this value.  It
    carries the canonical post-terminal source messages and all Segment-bound
    Provider inputs together, so the loop cannot accidentally retain the
    Approval context, model, or surface gate after the origin write, while the
    one Bundle-owned immutable catalog remains the exact Runtime catalog.
    ``messages`` are the complete source snapshot after the origin ToolMessage
    has been durably committed; the runner never appends a second local copy.
    """

    messages: tuple[Message, ...]
    model: ChatModel
    catalog: ToolCatalog
    tool_context: ToolExecutionContext
    surface_gate: SegmentSurfaceGate
    # The policy resolver owns the continuation budget.  Carry it with the
    # fresh Segment so bootstrap cannot silently retain the Approval seed's
    # default budget.
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.messages) is not tuple:
            raise TypeError("ApprovedContinuationSegment messages must be a tuple")
        if any(not isinstance(message, Message) for message in self.messages):
            raise TypeError("ApprovedContinuationSegment messages must contain Message values")
        if self.model is None or not callable(getattr(self.model, "complete", None)):
            raise TypeError("ApprovedContinuationSegment model is invalid")
        if type(self.catalog) is not ToolCatalog:
            raise TypeError("ApprovedContinuationSegment catalog is invalid")
        if type(self.tool_context) is not ToolExecutionContext:
            raise TypeError("ApprovedContinuationSegment tool_context is invalid")
        if type(self.tool_context.authority) is not SegmentExecutionAuthority:
            raise TypeError("ApprovedContinuationSegment requires Segment authority")
        if type(self.surface_gate) is not SegmentSurfaceGate:
            raise TypeError("ApprovedContinuationSegment surface_gate is invalid")
        if type(self.max_iterations) is not int or self.max_iterations <= 0:
            raise TypeError("ApprovedContinuationSegment max_iterations is invalid")
        if self.surface_gate.dispatch_catalog is not self.catalog:
            raise ProjectionError("approved continuation replaced the Bundle catalog")
        if (
            self.surface_gate.authority is not self.tool_context.authority
            or self.surface_gate.context is not self.tool_context
        ):
            raise ProjectionError("approved continuation Segment bundle mismatch")
        self.surface_gate._validate_current_surface()

    @property
    def catalog_lease(self) -> SegmentToolCatalogLease:
        return self.surface_gate.catalog_lease

    def __repr__(self) -> str:
        return "<ApprovedContinuationSegment transient>"


def build_segment_surface_gate(
    messages: tuple[Message, ...] | list[Message],
    *,
    catalog: ToolCatalog,
    catalog_lease: SegmentToolCatalogLease,
    context: ToolExecutionContext,
    authority: SegmentExecutionAuthority,
    provider_view: ProviderToolMetadataView,
    discovery_view: ToolDiscoveryMetadataView,
    authority_metadata_view: ToolAuthorityMetadataView,
    policy: object,
) -> SegmentSurfaceGate:
    """Build the provider-free selection gate for one trusted Segment."""

    if type(catalog) is not ToolCatalog:
        raise ProjectionError("typed_catalog_required")
    if type(catalog_lease) is not SegmentToolCatalogLease:
        raise ProjectionError("segment_catalog_lease_required")
    if catalog_lease.closed:
        raise ProjectionError("segment_catalog_lease_closed")
    try:
        catalog_lease.require_catalog(catalog)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ProjectionError("segment_dispatch_catalog_mismatch") from exc
    if type(context) is not ToolExecutionContext:
        raise ProjectionError("tool_context_required")
    if context.authority is not authority:
        raise ProjectionError("segment_context_mismatch")
    if type(provider_view) is not ProviderToolMetadataView:
        raise ProjectionError("provider_metadata_view_required")
    if type(discovery_view) is not ToolDiscoveryMetadataView:
        raise ProjectionError("discovery_metadata_view_required")
    if type(authority_metadata_view) is not ToolAuthorityMetadataView:
        raise ProjectionError("authority_metadata_view_required")
    if not (
        catalog_lease.bundle_instance_token
        is provider_view.bundle_instance_token
        is discovery_view.bundle_instance_token
        is authority_metadata_view.bundle_instance_token
    ):
        raise ProjectionError("cross_Bundle_metadata_views")
    values = tuple(messages)
    user_indexes = [index for index, message in enumerate(values) if message.role == "user"]
    if not user_indexes:
        raise ProjectionError("current_request_required")
    current = values[user_indexes[-1]]
    page_kinds = tuple(
        dict.fromkeys(message.surface_page_kind for message in values if message.surface_page_kind)
    )
    if len(page_kinds) > 1:
        raise ProjectionError("multiple_page_kinds")
    attachment_kinds = tuple(
        dict.fromkeys(
            kind
            for message in values
            for kind in message.surface_attachment_kinds.split(",")
            if kind
        )
    )
    trusted_domains = tuple(
        dict.fromkeys(
            signal for message in values for signal in message.surface_signal.split(",") if signal
        )
    )
    signals = ToolSelectionSignals(
        current_request=current.content,
        page_kind=page_kinds[0] if page_kinds else "workspace",
        attachment_kinds=attachment_kinds,
        trusted_domains=trusted_domains,
    )
    selection = select_tools(discovery_view, authority_metadata_view, signals)
    authority_surface = AuthoritySurfaceView.from_authority(authority)
    selection = intersect_authority_surface(
        discovery_view,
        authority_metadata_view,
        selection,
        authority_surface,
    )
    dependency_policy = _SurfaceDependencyPolicySeal.from_view(
        discovery_view,
        version=authority_surface.dependency_policy_version,
    )
    factory = context.authority_factory
    factory.register_tool_execution_context(context, authority=authority)
    factory.bind_segment_tool_catalog(
        authority,
        authority_metadata_view=authority_metadata_view,
        catalog_lease=catalog_lease,
    )
    gate = SegmentSurfaceGate(
        authority=authority,
        context=context,
        catalog_lease=catalog_lease,
        dispatch_catalog=catalog,
        provider_view=provider_view,
        discovery_view=discovery_view,
        authority_metadata_view=authority_metadata_view,
        authority_surface=authority_surface,
        selection=selection,
        dependency_policy=dependency_policy,
        policy=policy,
    )
    factory.register_segment_surface_gate(
        gate,
        authority=authority,
        context=context,
        catalog=catalog_lease,
        policy=policy,
        dependency_policy=dependency_policy,
        selection=selection,
        authority_surface=authority_surface,
    )
    return gate


class _LoopServices:
    def __init__(
        self,
        invocation: AgentLoopInvocation,
        *,
        run_recorder: RunRecorder | None = None,
        event_sink: AgentEventSink | None | object = _NO_OVERRIDE,
        records: list[ToolExecutionRecord[Any, Any]] | None = None,
        failures: list[ToolFailure] | None = None,
        delivery_fence: Callable[[], bool] | None | object = _NO_OVERRIDE,
    ) -> None:
        model = invocation.model
        self.model = (
            _InjectedSurfaceAdapter(model)
            if model is not None
            and not callable(getattr(model, "complete_agent_surface", None))
            and not callable(getattr(model, "stream_agent_surface", None))
            else model
        )
        self.catalog = invocation.catalog
        self.context = invocation.tool_context
        self.event_sink = (
            invocation.event_sink
            if event_sink is _NO_OVERRIDE
            else cast(AgentEventSink | None, event_sink)
        )
        self.cancel_check = invocation.cancel_check
        self.run_recorder = invocation.run_recorder if run_recorder is None else run_recorder
        self.surface_gate = invocation.surface_gate
        self.delivery_fence = (
            invocation.seed.continuation.delivery_fence
            if delivery_fence is _NO_OVERRIDE and isinstance(invocation.seed, ApprovedWriteSeed)
            else None
            if delivery_fence is _NO_OVERRIDE
            else cast(Callable[[], bool] | None, delivery_fence)
        )
        self.runtime_signal_sink = invocation.runtime_signal_sink
        self.records = [] if records is None else records
        self.failures = [] if failures is None else failures
        self.runner_invocation = invocation
        self._prepare_identities: dict[int, NewTurnPrepareCallIdentity] = {}
        self._provider_invocations: dict[int, ProviderInvocationIdentity] = {}
        self._pending_claims: dict[int, PendingAuthorityClaim] = {}
        if type(self.context.authority) is SegmentExecutionAuthority:
            factory = self.context.authority_factory
            factory.register_runner_invocation(invocation, authority=self.context.authority)
            factory.register_tool_execution_context(self.context, authority=self.context.authority)
            if isinstance(invocation.seed, NewTurnSeed):
                self._require_surface_gate()

    def _require_surface_gate(self) -> SegmentSurfaceGate:
        if type(self.surface_gate) is not SegmentSurfaceGate:
            raise ProjectionError("segment_surface_gate_required")
        gate = self.surface_gate
        if type(self.context.authority) is not SegmentExecutionAuthority:
            raise ProjectionError("segment_authority_required")
        authority = self.context.authority
        try:
            gate._validate_current_surface()
            self.context.authority_factory.require_segment_surface_gate(
                gate,
                authority=authority,
                context=self.context,
                catalog=gate.catalog_lease,
                policy=gate.policy,
                dependency_policy=gate.dependency_policy,
                selection=gate.selection,
                authority_surface=gate.authority_surface,
            )
        except ProjectionError:
            raise
        except Exception as exc:
            raise ProjectionError("segment_surface_gate_mismatch") from exc
        return gate

    def complete_model(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        *,
        model_step: int,
    ) -> Assistant:
        if self.model is None:
            raise RuntimeError("provider-free confirmation cannot call a model")
        if isinstance(self.runner_invocation.seed, NewTurnSeed):
            self._require_surface_gate()
        model: object = self.model
        complete_surface = getattr(model, "complete_agent_surface", None)
        stream_surface = getattr(model, "stream_agent_surface", None)
        surface_aware = callable(complete_surface) or callable(stream_surface)
        if not surface_aware:
            raise TypeError("Agent Provider surface adapter is required")
        new_session = getattr(model, "new_agent_provider_session", None)
        if not callable(new_session):
            raise ProjectionError("provider_gateway_session_factory_required")
        gateway = new_session()
        if type(gateway) is not AgentProviderGatewaySession:
            raise ProjectionError("provider_gateway_session_required")
        model = _PerCallSurfaceModel(cast(ChatModel, model), gateway)
        model_call_id = str(uuid4())
        complete_surface = getattr(model, "complete_agent_surface", None)
        stream_surface = getattr(model, "stream_agent_surface", None)
        if type(self.context.authority) is not SegmentExecutionAuthority:
            raise TypeError("Provider calls require a Segment authority")
        factory = self.context.authority_factory
        build_identity = factory.create_provider_surface_build_identity(
            self.context.authority,
            runner_invocation=self.runner_invocation,
            tool_context=self.context,
            model_call_id=model_call_id,
        )
        surface = self.project_model_surface(
            messages,
            model_call_id=model_call_id,
            build_identity=build_identity,
            model=model,
        )
        messages = surface.thaw_messages()
        tools = list(surface.tools)
        snapshot_id = self.capture_model_input(
            messages,
            tools,
            model_step=model_step,
            model_call_id=model_call_id,
            surface=surface,
            model=model,
        )
        is_stream = callable(stream_surface)
        buffered_deltas: list[str] = []

        def buffer_delta(delta: str) -> None:
            if delta:
                buffered_deltas.append(delta)

        binding = ModelCallSurfaceBinding.from_surface(surface)
        bind_surface = getattr(model, "bind_agent_provider_surface", None)
        if not callable(bind_surface):
            raise TypeError("Agent Provider surface binder is missing")
        invocation_identity = bind_surface(
            authority=self.context.authority,
            build_identity=build_identity,
            surface=surface,
            model_call_surface_binding=binding,
        )
        preflight = getattr(model, "preflight_agent_surface", None)
        if not callable(preflight):
            raise TypeError("Agent Provider preflight is missing")
        preflight(
            surface,
            invocation_identity=invocation_identity,
            stream=is_stream,
        )
        if snapshot_id is not None:
            provider_kind, model_id, supports_json_schema = _journal_model_metadata(model)
            model_id_fingerprint = self.fingerprint_model_id(model_id)
            self.append_journal_event(
                EventInput(
                    event_type="model.requested",
                    facts={
                        "snapshot_id": snapshot_id,
                        "provider_kind": provider_kind,
                        "model_id_fingerprint": model_id_fingerprint,
                        "supports_tools": True,
                        "supports_json_schema": supports_json_schema,
                        "stream": is_stream,
                        "tools_count": len(tools),
                        "response_format_kind": "text",
                    },
                    model_step=model_step,
                    model_call_id=model_call_id,
                    source_ref_type="context_snapshot",
                    source_ref_id=snapshot_id,
                )
            )
        try:
            self.require_active()
            if is_stream:
                if not callable(stream_surface):
                    raise TypeError("surface streaming model is missing")
                bound = stream_surface(
                    surface,
                    buffer_delta,
                    invocation_identity=invocation_identity,
                    before_attempt=self.require_active,
                )
            else:
                if not callable(complete_surface):
                    raise TypeError("surface completion model is missing")
                bound = complete_surface(
                    surface,
                    invocation_identity=invocation_identity,
                    before_attempt=self.require_active,
                )
            attempt_validator = getattr(model, "consume_agent_provider_attempt", None)
            if not callable(attempt_validator):
                raise TypeError("Agent Provider Gateway attempt validator is missing")
            assistant = binding.validate_response(
                bound,
                attempt_validator=attempt_validator,
            )
            if bound.provider_invocation_identity is not invocation_identity:
                raise ProjectionError("provider_response_invocation_mismatch")
            for call in assistant.tool_calls:
                prepare_identity = factory.create_new_turn_prepare_identity(
                    invocation_identity,
                    attempt_id=bound.provider_attempt_id,
                    candidate_ordinal=bound.candidate_ordinal,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments_digest=_provider_arguments_digest(call.args),
                )
                self._prepare_identities[id(call)] = prepare_identity
                self._provider_invocations[id(call)] = invocation_identity
            self.require_active()
            for delta in buffered_deltas:
                self.emit_assistant_delta(delta)
        except Exception as exc:
            if snapshot_id is not None:
                failure_category, provider_outcome = _journal_model_failure(exc)
                self.append_journal_event(
                    EventInput(
                        event_type="model.failed",
                        facts={
                            "failure_category": failure_category,
                            "provider_outcome": provider_outcome,
                        },
                        model_step=model_step,
                        model_call_id=model_call_id,
                    )
                )
            raise
        if snapshot_id is not None:
            self.append_journal_event(
                EventInput(
                    event_type="model.completed",
                    facts={
                        "assistant_kind": _journal_assistant_kind(assistant),
                        "tool_call_count": len(assistant.tool_calls),
                        "finish_category": "tool_calls" if assistant.tool_calls else "stop",
                    },
                    model_step=model_step,
                    model_call_id=model_call_id,
                )
            )
        if self.runtime_signal_sink is not None and (
            assistant.content.strip() or assistant.tool_calls
        ):
            self.runtime_signal_sink.try_emit("first_complete_agent_response")
        return assistant

    def project_model_surface(
        self,
        messages: list[Message],
        *,
        model_call_id: str,
        build_identity: object,
        model: object | None = None,
    ) -> FrozenModelSurface:
        gate = (
            self._require_surface_gate()
            if isinstance(self.runner_invocation.seed, NewTurnSeed)
            else None
        )
        system = tuple(
            FrozenMessage.freeze(message)
            for message in messages
            if message.surface_contributor == "static_policy"
        )
        if not system:
            system = tuple(
                FrozenMessage.freeze(message) for message in messages if message.role == "system"
            )
        user_indexes = [index for index, message in enumerate(messages) if message.role == "user"]
        if not user_indexes:
            raise RuntimeError("Agent model input requires a current user request")
        request_index = user_indexes[-1]
        current_group = tuple(FrozenMessage.freeze(message) for message in messages[request_index:])
        current = current_group[0]
        history = tuple(
            FrozenMessage.freeze(message, source_message_id=index + 1)
            for index, message in enumerate(messages[:request_index])
            if message.surface_contributor in {"", "conversation_history"}
            and message.role != "system"
        )
        control = tuple(
            FrozenMessage.freeze(message)
            for message in messages
            if message.surface_contributor == "active_control"
        )
        routed = {
            name: tuple(
                FrozenMessage.freeze(message)
                for message in messages[:request_index]
                if message.surface_contributor == name
            )
            for name in ("current_scope", "request_page_context", "request_attachments")
        }
        contributors: list[ContributorResult] = []
        for name in CONTRIBUTOR_ORDER:
            status: ContributorStatus = "not_applicable"
            contributor_messages: tuple[FrozenMessage, ...] = ()
            if name == "static_policy":
                status, contributor_messages = (
                    "ready",
                    system or (FrozenMessage.freeze(Message(role="system", content="")),),
                )
            elif name == "active_control" and control:
                status, contributor_messages = "ready", control
            elif name in routed and routed[name]:
                status, contributor_messages = "ready", routed[name]
            elif name == "conversation_history":
                status = "ready"
            elif name == "current_request":
                status, contributor_messages = "ready", current_group
            elif name in {
                "confirmed_memory",
                "knowledge_context",
                "older_conversation_summary",
            }:
                status = "disabled"
            contributors.append(ContributorResult(name, status, contributor_messages))
        surface_model = self.model if model is None else model
        budgets = getattr(surface_model, "agent_provider_budgets", (ProviderBudget(),))
        trusted_domains = tuple(
            dict.fromkeys(
                signal
                for message in messages
                for signal in message.surface_signal.split(",")
                if signal
            )
        )
        page_kinds = tuple(
            dict.fromkeys(
                message.surface_page_kind for message in messages if message.surface_page_kind
            )
        )
        if len(page_kinds) > 1:
            raise RuntimeError("multiple page kinds in one model call")
        attachment_kinds = tuple(
            dict.fromkeys(
                kind
                for message in messages
                for kind in message.surface_attachment_kinds.split(",")
                if kind
            )
        )
        sources: list[FrozenSource] = []
        for name in ("current_scope", "request_page_context", "request_attachments"):
            routed_messages = routed[name]
            if not routed_messages:
                continue
            content = {"messages": [message.canonical_value() for message in routed_messages]}
            revisions = tuple(
                dict.fromkeys(
                    message.surface_revision
                    for message in messages[:request_index]
                    if message.surface_contributor == name and message.surface_revision
                )
            )
            revision_identity = sha256_hex(
                canonical_json({"source": name, "revisions": revisions or ("request-local",)})
            )
            sources.append(
                FrozenSource.present(
                    kind=name,
                    revision_identity=f"snapshot:{revision_identity}",
                    content=content,
                    chunks=chunk_structured_source(content),
                )
            )
        if gate is None:
            raise ProjectionError("segment_surface_gate_required")
        request = ProjectionRequest(
            model_call_id=model_call_id,
            contributors=tuple(contributors),
            history=history,
            tool_signals=ToolSelectionSignals(
                current_request=current.content,
                page_kind=page_kinds[0] if page_kinds else "workspace",
                attachment_kinds=attachment_kinds,
                trusted_domains=trusted_domains,
            ),
            provider_budgets=tuple(budgets),
            selection=gate.selection,
            sources=tuple(sources),
            provider_surface_build_identity=build_identity,
        )
        return ModelSurfaceProjector().project(request)

    def require_delivery_fence(self) -> None:
        if self.delivery_fence is not None and not self.delivery_fence():
            raise ChatRunCancelled("write operation delivery owner fenced")

    def require_active(self) -> None:
        self.raise_if_cancelled()
        self.require_delivery_fence()

    def capture_model_input(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        *,
        model_step: int,
        model_call_id: str,
        surface: FrozenModelSurface | None,
        model: object | None = None,
    ) -> str | None:
        try:
            capture_surface = getattr(self.run_recorder, "capture_surface_context", None)
            if surface is None or not callable(capture_surface):
                return None
            gate = self._require_surface_gate()
            capture_model = self.model if model is None else model
            identities = tuple(
                getattr(capture_model, "agent_provider_manifest_identities", ("agent-provider",))
            )
            captured = capture_surface(
                _journal_model_input(messages, tools),
                surface.audit,
                identities,
                provider_view=gate.provider_view,
                model_step=model_step,
                model_call_id=model_call_id,
            )
            return captured if type(captured) is str else None
        except Exception:
            return None

    def fingerprint_model_id(self, model_id: str) -> str | None:
        try:
            return self.run_recorder.fingerprint_model_id(model_id)
        except Exception:
            return None

    def append_journal_event(self, event: EventInput) -> None:
        try:
            self.run_recorder.append_event(event)
        except Exception:
            return

    def emit_assistant_delta(self, delta: str) -> None:
        if delta and self.event_sink is not None:
            self.event_sink.emit(AgentAssistantDelta(delta))

    def raise_if_cancelled(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise ChatRunCancelled("chat run cancelled")


@dataclass(frozen=True, slots=True, repr=False)
class NewTurnSeed(TransientToolRuntimeValue):
    messages: tuple[Message, ...]
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.messages) is not tuple:
            raise TypeError("NewTurnSeed messages must be a tuple")
        if not all(isinstance(message, Message) for message in self.messages):
            raise TypeError("NewTurnSeed messages must contain Message values")
        # Message and its nested tool/provider payloads remain mutable at the
        # transport boundary.  Keep the loop's seed detached from the caller
        # before it becomes local working state.
        object.__setattr__(self, "messages", tuple(deepcopy(message) for message in self.messages))

    def __repr__(self) -> str:
        return "<NewTurnSeed transient>"


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWriteSeed(TransientToolRuntimeValue):
    continuation: ApprovedWriteContinuation
    _pending_snapshot: PendingAction = field(init=False, repr=False, compare=False)
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.continuation is None:
            raise TypeError("ApprovedWriteSeed continuation is required")
        required_methods = (
            "claim",
            "record_result",
            "activate_continuation_segment",
            "delivery_fence",
        )
        if any(
            not callable(getattr(self.continuation, name, None)) for name in required_methods
        ) or not hasattr(type(self.continuation), "pending"):
            raise TypeError("ApprovedWriteSeed continuation is incomplete")
        pending = self.continuation.pending
        if not isinstance(pending, PendingAction):
            raise PendingActionValidationError("approved pending must be PendingAction")
        if not all((pending.tool_call_id, pending.tool_name, pending.operation_id)):
            raise PendingActionValidationError("approved pending identity is incomplete")
        # Keep the exact Port-owned value.  The read is intentionally made
        # once here; bootstrap uses this cached value instead of consulting a
        # potentially one-shot or advancing Pending source a second time.
        object.__setattr__(self, "_pending_snapshot", pending)

    @property
    def pending(self) -> PendingAction:
        return self._pending_snapshot

    def __repr__(self) -> str:
        return "<ApprovedWriteSeed transient>"


class _AgentLoopLeaseOwnership:
    """Transfer one origin Segment lease from Runtime to Runner exactly once."""

    __slots__ = (
        "_claimed",
        "_coordinated_release",
        "_lock",
        "_origin_release",
        "_released",
        "_runner_finished",
        "_runtime_release_requested",
    )

    def __init__(self, release: Callable[[], object]) -> None:
        self._lock = Lock()
        self._claimed = False
        self._released = False
        self._runner_finished = False
        self._runtime_release_requested = False
        self._origin_release = release
        self._coordinated_release: Callable[[], object] | None = None

    def bind_release(self, release: Callable[[], object]) -> None:
        if not callable(release):
            raise TypeError("Agent Loop Catalog release must be callable")
        with self._lock:
            if self._claimed or self._released:
                raise RuntimeError("Agent Loop Catalog lease ownership already started")
            if self._coordinated_release is not None:
                raise RuntimeError("Agent Loop Catalog release was already bound")
            self._coordinated_release = release

    def claim(self) -> None:
        with self._lock:
            if self._released:
                raise RuntimeError("Agent Loop Catalog lease was already released")
            if self._claimed:
                raise RuntimeError("Agent Loop Catalog lease was already claimed")
            self._claimed = True

    def release_from_runner(self) -> bool:
        with self._lock:
            if self._released:
                return False
            if not self._claimed:
                raise RuntimeError("Agent Loop Catalog lease was not claimed")
            if self._runner_finished:
                return False
            self._runner_finished = True
            coordinated = self._coordinated_release if self._runtime_release_requested else None
            if self._coordinated_release is None or coordinated is not None:
                self._released = True
            origin = self._origin_release
        try:
            origin()
        finally:
            if coordinated is not None:
                coordinated()
        return True

    def release_backstop(self) -> bool:
        with self._lock:
            if self._released or self._claimed or self._runner_finished:
                return False
            self._released = True
            release = self._coordinated_release or self._origin_release
        release()
        return True

    def release_from_runtime(self) -> bool:
        with self._lock:
            if self._released:
                return False
            coordinated = self._coordinated_release
            if not self._claimed:
                self._released = True
                release = coordinated or self._origin_release
            elif not self._runner_finished:
                if coordinated is not None:
                    self._runtime_release_requested = True
                return False
            elif coordinated is None:
                return False
            else:
                self._released = True
                release = coordinated
        release()
        return True


@dataclass(frozen=True, slots=True, repr=False)
class AgentLoopInvocation(TransientToolRuntimeValue):
    seed: NewTurnSeed | ApprovedWriteSeed
    model: ChatModel | None
    catalog: ToolCatalog
    catalog_lease: SegmentToolCatalogLease = field(repr=False, compare=False)
    tool_context: ToolExecutionContext
    auto_approve: bool
    max_iterations: int
    run_recorder: RunRecorder
    event_sink: AgentEventSink | None
    runtime_signal_sink: AgentRuntimeSignalSink | None
    cancel_check: CancelCheck | None
    surface_gate: SegmentSurfaceGate | None = field(default=None, repr=False, compare=False)
    _catalog_ownership: _AgentLoopLeaseOwnership = field(
        init=False,
        repr=False,
        compare=False,
    )
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.seed, (NewTurnSeed, ApprovedWriteSeed)):
            raise TypeError("AgentLoopInvocation seed is invalid")
        if type(self.tool_context) is not ToolExecutionContext:
            raise TypeError("AgentLoopInvocation tool_context is invalid")
        if type(self.catalog) is not ToolCatalog:
            raise TypeError("AgentLoopInvocation catalog is invalid")
        if type(self.catalog_lease) is not SegmentToolCatalogLease:
            raise TypeError("AgentLoopInvocation catalog_lease is invalid")
        if self.catalog_lease.closed:
            raise TypeError("AgentLoopInvocation catalog_lease is closed")
        object.__setattr__(
            self,
            "_catalog_ownership",
            _AgentLoopLeaseOwnership(self.catalog_lease.close),
        )
        authority_type = type(self.tool_context.authority)
        expected_authority = (
            SegmentExecutionAuthority
            if isinstance(self.seed, NewTurnSeed)
            else ApprovalExecutionAuthority
        )
        if authority_type is not expected_authority:
            raise TypeError(
                "NewTurnSeed requires Segment authority"
                if isinstance(self.seed, NewTurnSeed)
                else "ApprovedWriteSeed requires Approval authority"
            )
        if isinstance(self.seed, NewTurnSeed) and type(self.surface_gate) is not SegmentSurfaceGate:
            raise TypeError("NewTurnSeed requires an exact SegmentSurfaceGate")
        if isinstance(self.seed, NewTurnSeed):
            gate = cast(SegmentSurfaceGate, self.surface_gate)
            if gate.catalog_lease is not self.catalog_lease:
                raise TypeError("NewTurnSeed requires the exact Segment catalog lease")
            if gate.dispatch_catalog is not self.catalog:
                raise TypeError("NewTurnSeed requires the exact Bundle catalog")
            authority = cast(SegmentExecutionAuthority, self.tool_context.authority)
            self.tool_context.authority_factory.require_segment_surface_gate(
                gate,
                authority=authority,
                context=self.tool_context,
                catalog=gate.catalog_lease,
                policy=gate.policy,
                dependency_policy=gate.dependency_policy,
                selection=gate.selection,
                authority_surface=gate.authority_surface,
            )
        if type(self.auto_approve) is not bool:
            raise TypeError("AgentLoopInvocation auto_approve must be bool")
        if type(self.max_iterations) is not int or self.max_iterations < 0:
            raise ValueError("AgentLoopInvocation max_iterations is invalid")
        if isinstance(self.seed, ApprovedWriteSeed) and not callable(
            getattr(self.tool_context, "operation_executor", None)
        ):
            raise TypeError("ApprovedWriteSeed requires a Ledger operation executor")

    def _bind_catalog_release(self, release: Callable[[], object]) -> None:
        self._catalog_ownership.bind_release(release)

    def _claim_catalog_lease(self) -> None:
        self._catalog_ownership.claim()

    def _release_catalog_lease_from_runner(self) -> bool:
        return self._catalog_ownership.release_from_runner()

    def _release_catalog_lease_backstop(self) -> bool:
        return self._catalog_ownership.release_backstop()

    def _release_catalog_lease_from_runtime(self) -> bool:
        return self._catalog_ownership.release_from_runtime()

    def __repr__(self) -> str:
        return "<AgentLoopInvocation transient>"


class AgentLoopRunner:
    def run(
        self,
        invocation: AgentLoopInvocation,
        *,
        run_recorder: RunRecorder | None = None,
        event_sink: AgentEventSink | None | object = _NO_OVERRIDE,
    ) -> AgentTurnResult:
        if type(invocation) is not AgentLoopInvocation:
            raise TypeError("AgentLoopRunner requires exact AgentLoopInvocation")
        invocation._claim_catalog_lease()
        owned_leases: list[SegmentToolCatalogLease] = []
        try:
            if not isinstance(invocation.catalog, ToolCatalog):
                raise TypeError("AgentLoopInvocation catalog is invalid")
            if not isinstance(invocation.tool_context, ToolExecutionContext):
                raise TypeError("AgentLoopInvocation tool_context is invalid")
            if isinstance(invocation.seed, NewTurnSeed) and not all(
                isinstance(message, Message) for message in invocation.seed.messages
            ):
                raise TypeError("NewTurnSeed messages must contain Message values")
            return self._run(
                invocation,
                _LoopServices(
                    invocation,
                    run_recorder=run_recorder,
                    event_sink=event_sink,
                ),
                owned_leases,
            )
        finally:
            for lease in reversed(owned_leases):
                lease.close()
            invocation._release_catalog_lease_from_runner()

    def _run(
        self,
        invocation: AgentLoopInvocation,
        services: _LoopServices,
        owned_leases: list[SegmentToolCatalogLease],
    ) -> AgentTurnResult:
        records = services.records
        failures = services.failures
        if isinstance(invocation.seed, NewTurnSeed):
            working_messages = list(invocation.seed.messages)
            added_messages: list[Message] = []
        else:
            (
                invocation,
                services,
                working_messages,
                added_messages,
            ) = self._bootstrap_approved(
                invocation,
                invocation.seed.continuation,
                services,
                records,
                failures,
                owned_leases,
            )

        model_steps = 0
        max_iterations = invocation.max_iterations or DEFAULT_MAX_ITERATIONS
        if type(invocation.surface_gate) is not SegmentSurfaceGate:
            raise ProjectionError("segment_surface_gate_required")
        provider_tools = list(invocation.surface_gate.selection.provider_contracts)
        while True:
            services.raise_if_cancelled()
            services.require_delivery_fence()
            if model_steps >= max_iterations:
                raise RuntimeError("AI 工具调用超过最大轮次")
            assistant = services.complete_model(
                working_messages,
                provider_tools,
                model_step=model_steps + 1,
            )
            services.raise_if_cancelled()
            services.require_delivery_fence()
            selected = _select_tool_calls(
                assistant.tool_calls,
                invocation.surface_gate.authority_metadata_view,
            )
            model_steps += 1
            assistant_message = Message(
                role="assistant",
                content=assistant.content,
                tool_calls=selected,
                provider_blocks=assistant.provider_blocks,
            )
            working_messages.append(assistant_message)
            added_messages.append(assistant_message)
            if not selected:
                services.require_active()
                return AgentTurnResult(
                    added_messages,
                    assistant.content,
                    None,
                    tuple(records),
                    tuple(failures),
                )
            pending = self._dispatch(
                invocation,
                selected,
                working_messages,
                added_messages,
                services,
                records,
                failures,
            )
            if pending is not None:
                services.require_active()
                return AgentTurnResult(
                    added_messages,
                    "",
                    pending,
                    tuple(records),
                    tuple(failures),
                    pending_authority_claim=services._pending_claims.get(id(pending)),
                )

    def _bootstrap_approved(
        self,
        invocation: AgentLoopInvocation,
        continuation: ApprovedWriteContinuation,
        services: _LoopServices,
        records: list[ToolExecutionRecord[Any, Any]],
        failures: list[ToolFailure],
        owned_leases: list[SegmentToolCatalogLease],
    ) -> tuple[AgentLoopInvocation, _LoopServices, list[Message], list[Message]]:
        services.raise_if_cancelled()
        seed = invocation.seed
        if not isinstance(seed, ApprovedWriteSeed):
            raise TypeError("approved bootstrap requires ApprovedWriteSeed")
        pending = seed.pending
        prepare_identity = (
            invocation.tool_context.authority_factory.create_approved_write_prepare_identity(
                cast(ApprovalExecutionAuthority, invocation.tool_context.authority),
                approval_context=invocation.tool_context,
                request_identity=seed,
            )
        )
        prepared_result = prepare_call(
            invocation.catalog_lease,
            invocation.tool_context,
            ToolCall(pending.tool_call_id, pending.tool_name, pending.args),
            call_identity=prepare_identity,
            pending_identity=pending,
            pending_action_revision=_pending_action_revision(
                pending.tool_call_id,
                pending.tool_name,
                pending.args,
            ),
            record_proposal=False,
        )
        services.raise_if_cancelled()
        if not isinstance(prepared_result, ConfirmationRequired):
            if isinstance(prepared_result, Rejected):
                raise PendingActionValidationError(
                    prepared_result.failure.compatibility_detail or prepared_result.failure.code
                )
            raise PendingActionValidationError("pending tool no longer requires confirmation")
        spec = prepared_result.prepared.spec
        authority_entry = invocation.tool_context.authority_factory.require_prepared_route(
            prepared_result.prepared,
            authority=invocation.tool_context.authority,
            use=AuthorityUse.APPROVED_WRITE_PREPARE,
        )
        if authority_entry.operation_kind is not OperationKind.TRANSACTIONAL_WRITE:
            raise PendingActionValidationError("approved pending tool is not a write tool")
        self._emit_pending_tool_call(
            pending,
            spec,
            authority_entry,
            "approved",
            services.event_sink,
        )

        def claim(prepared: Any) -> Any:
            services.raise_if_cancelled()
            return continuation.claim(pending, prepared)

        services.raise_if_cancelled()
        record = execute_prepared(
            prepared_result.prepared,
            invocation.tool_context,
            call_identity=prepare_identity,
            confirmation_claimer=claim,
        )
        records.append(record)
        if isinstance(record.outcome, ToolFailure):
            if record.outcome.code in {
                "authorization_mismatch",
                "confirmation_claim_failed",
                "confirmation_claim_lost",
            }:
                raise StalePendingActionError("stale pending action: confirmation claim failed")
            failures.append(record.outcome)
        if not record.execution_started and not record.terminal_persisted:
            assert isinstance(record.outcome, ToolFailure)
            raise PendingActionValidationError(
                record.outcome.compatibility_detail or record.outcome.code
            )
        if record.terminal_persisted:
            if record.persisted_visible_result is None:
                raise RuntimeError("persisted operation result is missing")
            result = record.persisted_visible_result
        else:
            result = render_compatibility(spec, record.outcome)
        self._emit_tool_result(
            pending.tool_call_id,
            pending.tool_name,
            result,
            record,
            services.event_sink,
        )
        tool_message = Message(role="tool", content=result, tool_call_id=pending.tool_call_id)
        continuation.record_result(pending, tool_message, record)
        # A rejected/stale claim must retain its domain error even when the
        # delivery owner has already been fenced.  For an accepted claim,
        # record the terminal result first so a timeout can converge its late
        # durable delivery before cancellation stops the continuation.
        services.raise_if_cancelled()
        services.require_delivery_fence()
        invocation.catalog_lease.close()
        segment = continuation.activate_continuation_segment()
        if type(segment) is not ApprovedContinuationSegment:
            raise TypeError("approved continuation port returned an invalid Segment bundle")
        continuation_lease = segment.catalog_lease
        owned_leases.append(continuation_lease)
        if segment.catalog is not invocation.catalog:
            raise ProjectionError("approved continuation replaced the Bundle catalog")
        if continuation_lease is invocation.catalog_lease:
            raise ProjectionError("approved continuation reused the Approval Segment lease")
        if (
            continuation_lease.bundle_instance_token
            is not invocation.catalog_lease.bundle_instance_token
        ):
            raise ProjectionError("approved continuation replaced the Bundle lease provenance")
        if segment.model is invocation.model or segment.tool_context is invocation.tool_context:
            raise ProjectionError("approved continuation reused Approval services")
        fresh_recorder = getattr(segment.tool_context, "run_recorder", None)
        set_recorder = getattr(fresh_recorder, "set_delegate", None)
        if not callable(set_recorder):
            raise ProjectionError("continuation segment recorder proxy is required")
        # The Driver owns the one proposal gate for this Agent Loop run.  A
        # fresh Segment must delegate its context recorder to that exact gate;
        # otherwise chained writes would append directly to the raw journal.
        set_recorder(services.run_recorder)
        active_invocation = replace(
            invocation,
            seed=NewTurnSeed(segment.messages),
            model=segment.model,
            catalog=segment.catalog,
            catalog_lease=continuation_lease,
            tool_context=segment.tool_context,
            surface_gate=segment.surface_gate,
            auto_approve=False,
            max_iterations=segment.max_iterations,
            run_recorder=services.run_recorder,
        )
        active_services = _LoopServices(
            active_invocation,
            run_recorder=services.run_recorder,
            event_sink=services.event_sink,
            records=records,
            failures=failures,
            delivery_fence=services.delivery_fence,
        )
        active_services.raise_if_cancelled()
        active_seed = active_invocation.seed
        if not isinstance(active_seed, NewTurnSeed):
            raise TypeError("approved continuation did not activate a Segment seed")
        return (
            active_invocation,
            active_services,
            list(active_seed.messages),
            [tool_message],
        )

    def _dispatch(
        self,
        invocation: AgentLoopInvocation,
        calls: list[ToolCall],
        working_messages: list[Message],
        added_messages: list[Message],
        services: _LoopServices,
        records: list[ToolExecutionRecord[Any, Any]],
        failures: list[ToolFailure],
    ) -> PendingAction | None:
        for call in calls:
            services.raise_if_cancelled()
            services.require_delivery_fence()
            surface_gate = cast(SegmentSurfaceGate, invocation.surface_gate)
            authority_entry = surface_gate.authority_metadata_view.entries.get(call.name)
            is_write = (
                authority_entry is not None
                and authority_entry.operation_kind is OperationKind.TRANSACTIONAL_WRITE
            )
            pending_draft: PendingAction | None = None
            pending_revision: int | None = None
            if is_write:
                pending_revision = max(
                    1,
                    _pending_action_revision(call.id, call.name, call.args),
                )
                pending_draft = PendingAction(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.args,
                    human=call.name,
                    operation_id=str(uuid4()),
                )
            prepared = prepare_call(
                invocation.catalog_lease,
                invocation.tool_context,
                call,
                call_identity=services._prepare_identities.get(id(call)),
                pending_identity=pending_draft,
                pending_action_revision=pending_revision,
            )
            services.raise_if_cancelled()
            spec = (
                prepared.prepared.spec
                if isinstance(prepared, (ConfirmationRequired, ReadyToExecute))
                else prepared.spec
            )
            self._emit_tool_call(spec, authority_entry, call, services.event_sink)
            if isinstance(prepared, Rejected):
                failures.append(prepared.failure)
                result = (
                    render_compatibility(spec, prepared.failure)
                    if spec is not None
                    else "错误：" + (prepared.failure.compatibility_detail or prepared.failure.code)
                )
                self._append_tool_result(
                    call,
                    result,
                    None,
                    working_messages,
                    added_messages,
                    services.event_sink,
                )
                continue
            if spec is None:
                raise ProjectionError("resolved pipeline result omitted its ToolSpec handle")
            if is_write:
                if isinstance(prepared, ConfirmationRequired):
                    if type(invocation.tool_context.authority) is not SegmentExecutionAuthority:
                        raise TypeError("Typed Pending requires Segment authority")
                    if pending_draft is None or pending_revision is None:
                        raise TypeError("Typed Pending draft identity is missing")
                    pending = pending_draft
                    pending.human = _spec_confirmation_description(spec, call.args, call.name)
                    operation_id = pending.operation_id
                    revision = pending_revision
                    pending.bind_typed_proposal_identity(
                        conversation_id=invocation.tool_context.authority.conversation_id,
                        pending_action_revision=revision,
                        pending_confirmation_claim_id=operation_id,
                        arguments_digest=prepared.prepared.arguments_digest,
                    )
                    factory = invocation.tool_context.authority_factory
                    factory.register_pending(pending)
                    factory.create_typed_pending_identity(
                        authority=invocation.tool_context.authority,
                        runner_invocation=services.runner_invocation,
                        tool_context=invocation.tool_context,
                        prepared=prepared.prepared,
                        pending=pending,
                        operation_id=operation_id,
                        pending_action_revision=revision,
                        tool_call_id=call.id,
                        tool_name=call.name,
                        arguments_digest=prepared.prepared.arguments_digest,
                    )
                    pending_claim = factory.issue_pending_claim(
                        invocation.tool_context.authority,
                        prepared=prepared.prepared,
                        pending=pending,
                        operation_id=operation_id,
                        tool_call_id=call.id,
                        tool_name=call.name,
                        arguments_digest=prepared.prepared.arguments_digest,
                        pending_action_revision=revision,
                        pending_confirmation_claim_id=operation_id,
                    )
                    services._pending_claims[id(pending)] = pending_claim
                    return pending
                result = "错误：确认操作状态不一致"
                self._append_tool_result(
                    call,
                    result,
                    None,
                    working_messages,
                    added_messages,
                    services.event_sink,
                )
                continue
            if isinstance(prepared, ReadyToExecute):
                if authority_entry is None:
                    raise ProjectionError("read route omitted its Authority metadata entry")
                services.raise_if_cancelled()
                services.require_delivery_fence()
                provider_invocation = services._provider_invocations.get(id(call))
                if provider_invocation is None:
                    raise TypeError("read execution Provider provenance is missing")
                read_identity = (
                    invocation.tool_context.authority_factory.create_read_execution_identity(
                        provider_invocation,
                        prepared=prepared.prepared,
                        tool_call_id=call.id,
                        tool_name=call.name,
                        arguments_digest=prepared.prepared.arguments_digest,
                    )
                )
                record = execute_prepared(
                    prepared.prepared,
                    invocation.tool_context,
                    call_identity=read_identity,
                )
                services.raise_if_cancelled()
                services.require_delivery_fence()
                records.append(record)
                if isinstance(record.outcome, ToolFailure):
                    failures.append(record.outcome)
                self._require_read_record_route(
                    record,
                    invocation.tool_context,
                    authority_entry,
                )
                result = render_compatibility(spec, record.outcome)
                self._require_read_record_route(
                    record,
                    invocation.tool_context,
                    authority_entry,
                )
            else:
                record = None
                result = "错误：只读工具不能请求确认"
            self._append_tool_result(
                call,
                result,
                record,
                working_messages,
                added_messages,
                services.event_sink,
                route_context=invocation.tool_context if record is not None else None,
                authority_entry=authority_entry if record is not None else None,
            )
        return None

    def _append_tool_result(
        self,
        call: ToolCall,
        result: str,
        record: ToolExecutionRecord[Any, Any] | None,
        working_messages: list[Message],
        added_messages: list[Message],
        event_sink: AgentEventSink | None,
        *,
        route_context: ToolExecutionContext | None = None,
        authority_entry: ToolAuthorityEntryV1 | None = None,
    ) -> None:
        if record is not None and route_context is not None and authority_entry is not None:
            self._require_read_record_route(record, route_context, authority_entry)
        self._emit_tool_result(
            call.id,
            call.name,
            result,
            record,
            event_sink,
            route_context=route_context,
            authority_entry=authority_entry,
        )
        if record is not None and route_context is not None and authority_entry is not None:
            self._require_read_record_route(record, route_context, authority_entry)
        message = Message(role="tool", content=result, tool_call_id=call.id)
        working_messages.append(message)
        added_messages.append(message)

    def _emit_tool_call(
        self,
        spec: ToolSpec[Any, Any] | None,
        authority_entry: ToolAuthorityEntryV1 | None,
        call: ToolCall,
        event_sink: AgentEventSink | None,
    ) -> None:
        is_write = (
            authority_entry is not None
            and authority_entry.operation_kind is OperationKind.TRANSACTIONAL_WRITE
        )
        self._emit(
            event_sink,
            AgentToolCall(
                tool_call_id=call.id,
                tool_name=call.name,
                public_label=_tool_public_label(spec, call.name),
                kind="write" if is_write else "read",
                confirm_mode="hitl" if is_write else "none",
                summary=_tool_call_summary(
                    spec,
                    call.args,
                    call.name,
                    is_write=is_write,
                ),
                args_summary=cast(dict[str, Any], _args_summary(call.args)),
            ),
        )

    def _emit_pending_tool_call(
        self,
        pending: PendingAction,
        spec: ToolSpec[Any, Any],
        authority_entry: ToolAuthorityEntryV1,
        confirm_mode: str,
        event_sink: AgentEventSink | None,
    ) -> None:
        if authority_entry.operation_kind is not OperationKind.TRANSACTIONAL_WRITE:
            raise PendingActionValidationError("pending Authority entry is not a write tool")
        self._emit(
            event_sink,
            AgentToolCall(
                tool_call_id=pending.tool_call_id,
                tool_name=pending.tool_name,
                public_label=_tool_public_label(spec, pending.tool_name),
                kind="write",
                confirm_mode=cast(Any, confirm_mode),
                summary=_spec_confirmation_description(spec, pending.args, pending.human),
                args_summary=cast(dict[str, Any], _args_summary(pending.args)),
            ),
        )

    def _emit_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
        record: ToolExecutionRecord[Any, Any] | None,
        event_sink: AgentEventSink | None,
        *,
        route_context: ToolExecutionContext | None = None,
        authority_entry: ToolAuthorityEntryV1 | None = None,
    ) -> None:
        payload: dict[str, Any]
        if record is not None and record.terminal_persisted:
            if record.persisted_transport is None:
                raise RuntimeError("persisted operation transport is missing")
            payload = dict(record.persisted_transport)
        elif record is not None:
            try:
                if route_context is not None and authority_entry is not None:
                    payload = self._project_read_transport(
                        record,
                        result,
                        route_context,
                        authority_entry,
                    )
                else:
                    payload = project_transport_event(record.prepared.spec, record)
            except Exception:
                payload = _delivery_error_payload(tool_call_id, tool_name, result)
        else:
            payload = _delivery_error_payload(tool_call_id, tool_name, result)
        payload.setdefault("tool_call_id", tool_call_id)
        if record is not None and record.operation_id:
            payload.setdefault("operation_id", record.operation_id)
        payload.setdefault("summary", _summarize_tool_result(result))
        if record is not None and route_context is not None and authority_entry is not None:
            self._require_read_record_route(record, route_context, authority_entry)
        self._emit(
            event_sink,
            AgentToolResult(
                tool_call_id=tool_call_id,
                operation_id=str(payload.get("operation_id") or ""),
                payload=cast(Mapping[str, JsonValue], payload),
            ),
        )
        if record is not None and route_context is not None and authority_entry is not None:
            self._require_read_record_route(record, route_context, authority_entry)

    @staticmethod
    def _require_read_record_route(
        record: ToolExecutionRecord[Any, Any],
        context: ToolExecutionContext,
        expected_entry: ToolAuthorityEntryV1,
    ) -> None:
        current_entry = context.authority_factory.require_prepared_route(
            record.prepared,
            authority=context.authority,
            use=AuthorityUse.READ_EXECUTE,
        )
        if current_entry is not expected_entry:
            raise AuthorityPhaseError("read result Authority entry identity changed")

    @classmethod
    def _project_read_transport(
        cls,
        record: ToolExecutionRecord[Any, Any],
        visible_result: str,
        context: ToolExecutionContext,
        authority_entry: ToolAuthorityEntryV1,
    ) -> dict[str, Any]:
        cls._require_read_record_route(record, context, authority_entry)
        spec = record.prepared.spec
        projector = spec.result_metadata_projector
        cls._require_read_record_route(record, context, authority_entry)
        metadata = ToolResultMetadata()
        if not isinstance(record.outcome, ToolFailure) and projector is not None:
            metadata = projector(record.outcome.result)
            cls._require_read_record_route(record, context, authority_entry)
        payload: dict[str, Any] = {
            "tool_call_id": record.prepared.tool_call_id,
            "tool_name": spec.name,
            "status": "error" if isinstance(record.outcome, ToolFailure) else "success",
            "summary": visible_result[:500],
            "evidence": [dict(item) for item in metadata.evidence],
            "affected_resources": [dict(item) for item in metadata.affected_resources],
            "changed_entities": [dict(item) for item in metadata.changed_entities],
        }
        cls._require_read_record_route(record, context, authority_entry)
        return payload

    @staticmethod
    def _emit(event_sink: AgentEventSink | None, event: AgentLoopEvent) -> None:
        if event_sink is not None:
            event_sink.emit(event)


def _parse_json_object(raw: str, error_message: str) -> dict[str, Any]:
    try:
        parsed = parse_arguments(raw)
    except ArgumentValidationError as exc:
        raise ValueError(error_message) from exc
    return cast(dict[str, Any], parsed)


def _encode_json_object(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _pending_action_revision(tool_call_id: str, tool_name: str, raw_args: str) -> int:
    try:
        normalized_args = _encode_json_object(
            _parse_json_object(raw_args, "pending arguments must be a valid JSON object")
        )
    except ValueError:
        normalized_args = raw_args
    canonical = json.dumps(
        {
            "args": normalized_args,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big") & ((1 << 63) - 1)


def _spec_confirmation_description(
    spec: ToolSpec[Any, Any] | None,
    args: str,
    fallback: str,
) -> str:
    if spec is None:
        return fallback
    try:
        parsed = parse_arguments(args)
        human = spec.presentation.confirmation_description(spec.decoder(parsed))
    except Exception:
        return fallback
    return str(human or fallback)


def _journal_model_input(
    messages: list[Message],
    tools: list[ProviderToolContract],
) -> dict[str, object]:
    provider_payloads = materialize_provider_payloads(tools)
    return {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "args": call.args}
                    for call in message.tool_calls
                ],
            }
            for message in messages
        ],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": cast(dict[str, Any], payload["function"])["parameters"],
            }
            for tool, payload in zip(tools, provider_payloads, strict=True)
        ],
    }


def _provider_arguments_digest(raw: str) -> str:
    """Freeze response arguments without allowing malformed JSON to skip Pipeline validation."""

    try:
        value: object = parse_arguments(raw)
    except ArgumentValidationError:
        value = {"invalid_raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _journal_model_metadata(model: object) -> tuple[str, str, bool]:
    provider_kind = "openai_compatible"
    model_id = f"{type(model).__module__}.{type(model).__qualname__}"
    supports_json_schema = getattr(model, "supports_json_schema", False) is True
    providers = getattr(model, "_providers", None)
    if isinstance(providers, list) and providers:
        profile = providers[0]
        raw_provider = str(getattr(profile, "provider", "") or "")
        provider_kind = (
            raw_provider
            if raw_provider in {"openai", "openai_compatible", "litellm_proxy", "anthropic"}
            else "openai_compatible"
        )
        model_id = str(getattr(profile, "model", "") or model_id)
    else:
        raw_provider = str(getattr(model, "provider_kind", "") or "")
        if raw_provider in {"openai", "openai_compatible", "litellm_proxy", "anthropic"}:
            provider_kind = raw_provider
        model_id = str(getattr(model, "model", "") or model_id)
    return provider_kind, model_id, supports_json_schema


def _journal_model_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, TimeoutError):
        return "timeout", "timeout"
    if isinstance(error, ConnectionError):
        return "network_error", "network_error"
    return "provider_error", "error"


def _journal_assistant_kind(assistant: Assistant) -> str:
    if assistant.content and assistant.tool_calls:
        return "mixed"
    if assistant.tool_calls:
        return "tool_calls"
    if assistant.content:
        return "text"
    return "empty"


def _select_tool_calls(
    tool_calls: list[Any],
    authority_view: ToolAuthorityMetadataView,
) -> list[Any]:
    if not tool_calls:
        return []
    if all(
        (entry := authority_view.entries.get(str(call.name))) is None
        or entry.operation_kind is OperationKind.READ
        for call in tool_calls
    ):
        return tool_calls
    return tool_calls[:1]


def _tool_public_label(spec: ToolSpec[Any, Any] | None, fallback: str) -> str:
    description = spec.contract.description.strip() if spec is not None else ""
    return description or fallback


def _tool_call_summary(
    spec: ToolSpec[Any, Any] | None,
    args: str,
    fallback: str,
    *,
    is_write: bool,
) -> str:
    if is_write and spec is not None:
        return _spec_confirmation_description(spec, args, fallback)
    return _tool_public_label(spec, fallback)


def _args_summary(args: str) -> Any:
    try:
        parsed = json.loads(args) if args else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _scrub_sensitive(parsed)


def _delivery_error_payload(tool_call_id: str, tool_name: str, result: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "status": "error",
        "summary": _summarize_tool_result(result),
        "evidence": [],
        "affected_resources": [],
        "changed_entities": [],
    }


def _summarize_tool_result(result: str) -> str:
    return " ".join(result.split())[:500]


def _scrub_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        scrubbed = {}
        for key, item in value.items():
            normalized = str(key).lower()
            scrubbed[key] = (
                "***"
                if any(marker in normalized for marker in ("key", "token", "secret", "password"))
                else _scrub_sensitive(item)
            )
        return scrubbed
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    return value
