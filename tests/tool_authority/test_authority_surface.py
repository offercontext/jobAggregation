from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import pytest

from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.ai.tool_authority.composition import execution_scope
from offerpilot.ai.tool_authority.contracts import TrustedContextScope
from offerpilot.ai.tool_authority.policy import (
    AGENT_TYPED_V1_PROFILE,
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
)
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.context_projector.authority_surface import (
    AuthoritySurfaceView,
    intersect_authority_surface,
)
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.context_projector.binding import ModelCallSurfaceBinding
from offerpilot.context_projector.contracts import (
    FrozenMessage,
    FrozenModelSurface,
    RuntimeSurfaceAudit,
)
from offerpilot.context_projector.gateway import (
    AgentProviderGatewaySession,
    FrozenProviderExecutionChain,
    SingleCandidateAgentTransport,
)
from offerpilot.config import AIProviderProfile, Config
from offerpilot.ai.types import Assistant
from offerpilot.context_projector.selector import (
    ToolSelectionResult,
    ToolSelectionSignals,
    select_tools,
)
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components


_TEST_TOOL_CATALOG = build_model_tool_catalog()
_TEST_TOOL_NAMES = tuple(spec.name for spec in _TEST_TOOL_CATALOG.specs)


def _view(
    *, application: bool = False, capabilities: frozenset[object] | None = None
) -> AuthoritySurfaceView:
    return AuthoritySurfaceView(
        capability_profile_id="agent_typed_v1",
        capability_policy_version=CAPABILITY_POLICY_VERSION,
        dependency_policy_version=DEPENDENCY_POLICY_VERSION,
        capabilities=(
            frozenset(AGENT_TYPED_V1_PROFILE.capabilities) if capabilities is None else capabilities
        ),
        context_type="application" if application else "workspace",
    )


def _bundle() -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    return ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )


def _all_selection(bundle: ToolMetadataBundleV1) -> ToolSelectionResult:
    return select_tools(
        bundle.discovery_view(),
        bundle.authority_view(),
        ToolSelectionSignals(page_kind="workspace"),
    )


def test_application_surface_removes_only_complete_forbidden_envelopes() -> None:
    bundle = _bundle()
    selection = _all_selection(bundle)
    result = intersect_authority_surface(
        bundle.discovery_view(),
        bundle.authority_view(),
        selection,
        _view(application=True),
    )
    assert result.selected_names == tuple(
        name for name in _TEST_TOOL_NAMES if name not in {"create_application", "compare_offers"}
    )
    assert result.provider_contracts == tuple(
        contract
        for contract in _TEST_TOOL_CATALOG.provider_contracts()
        if contract.name in result.selected_names
    )


def test_capability_intersection_preserves_catalog_order_and_payload() -> None:
    bundle = _bundle()
    applications_read = next(
        capability
        for capability in AGENT_TYPED_V1_PROFILE.capabilities
        if str(capability) == "applications.read"
    )
    result = intersect_authority_surface(
        bundle.discovery_view(),
        bundle.authority_view(),
        _all_selection(bundle),
        _view(capabilities=frozenset({applications_read})),
    )
    assert result.selected_names == ("list_applications", "get_application")
    assert result.provider_contracts == _TEST_TOOL_CATALOG.provider_contracts()[:2]


def test_empty_policy_and_dependency_fail_closed() -> None:
    bundle = _bundle()
    with pytest.raises(ProjectionError, match="empty_authority_surface"):
        intersect_authority_surface(
            bundle.discovery_view(),
            bundle.authority_view(),
            _all_selection(bundle),
            _view(capabilities=frozenset()),
        )
    with pytest.raises(ProjectionError, match="unsupported_capability_policy_version"):
        intersect_authority_surface(
            bundle.discovery_view(),
            bundle.authority_view(),
            _all_selection(bundle),
            replace(_view(), capability_policy_version="capability-policy-v2"),
        )


def test_surface_view_discards_scope_identity_but_freezes_segment_policy() -> None:
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=1,
            conversation_scope_revision=2,
            segment_id="segment",
            trusted_scope=TrustedContextScope(
                context_type="application", context_ref=37, mode="general"
            ),
            capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
        )
        view = AuthoritySurfaceView.from_authority(authority)
    assert view.context_type == "application"
    assert not hasattr(view, "context_ref")
    assert not hasattr(view, "conversation_id")


def _surface(
    build: object,
    *,
    candidate_count: int = 1,
    model_call_id: str = "model-call",
) -> FrozenModelSurface:
    return FrozenModelSurface(
        model_call_id=model_call_id,
        messages=(FrozenMessage(role="user", content="hello"),),
        tools=_TEST_TOOL_CATALOG.provider_contracts(),
        runtime_surface_fingerprint="sha256:" + "a" * 64,
        provider_candidate_count=candidate_count,
        audit=RuntimeSurfaceAudit(
            budget_policy_version="budget-policy-v1",
            contributor_statuses=(),
            selected_history_group_ids=(),
            selected_tool_names=_TEST_TOOL_NAMES,
            source_fingerprints=(),
            estimated_input_units=1,
            canonical_message_bytes=1,
            canonical_tool_bytes=1,
            truncated=False,
        ),
        provider_surface_build_identity=build,
    )


def test_gateway_requires_exact_invocation_surface_binding_and_session_before_network() -> None:
    calls: list[object] = []
    chain = FrozenProviderExecutionChain.freeze(
        [AIProviderProfile(id="one", api_key="secret", base_url="https://one.test/v1")]
    )

    def complete(candidate: object, *_args: object) -> Assistant:
        calls.append(candidate)
        return Assistant(content="ok")

    gateway = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(complete, lambda *_args: Assistant(content="ok")),
    )
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=1,
            conversation_scope_revision=0,
            segment_id="segment",
            trusted_scope=TrustedContextScope(
                context_type="workspace", context_ref=None, mode="general"
            ),
            capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
        )
        runner = object()
        context = SimpleNamespace(authority=authority, authority_factory=factory)
        factory.register_runner_invocation(runner, authority=authority)
        factory.register_tool_execution_context(context, authority=authority)
        build = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-call",
        )
        surface = _surface(build)
        binding = ModelCallSurfaceBinding.from_surface(surface)
        invalid_bindings = (
            replace(binding, model_call_id="other-model-call"),
            replace(binding, runtime_surface_fingerprint="sha256:" + "b" * 64),
            replace(binding, exposed_tool_names=frozenset()),
            replace(binding, provider_candidate_count=2),
        )
        for invalid_binding in invalid_bindings:
            with pytest.raises(ProjectionError, match="provider_surface_binding_mismatch"):
                gateway.bind_provider_surface(
                    authority=authority,
                    build_identity=build,
                    surface=surface,
                    model_call_surface_binding=invalid_binding,
                )
        assert calls == []
        invocation = gateway.bind_provider_surface(
            authority=authority,
            build_identity=build,
            surface=surface,
            model_call_surface_binding=binding,
        )
        response = gateway.complete(surface, invocation_identity=invocation)
        assert response.provider_invocation_identity is invocation
        assert response.model_call_surface_binding is binding
        assert calls and response.candidate_ordinal == 0

        copied = replace(surface)
        with pytest.raises(ProjectionError):
            gateway.complete(copied, invocation_identity=invocation)
        assert len(calls) == 1

        alternate_gateway = AgentProviderGatewaySession(
            chain,
            SingleCandidateAgentTransport(complete, lambda *_args: Assistant(content="ok")),
        )
        alternate_build = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="alternate-model-call",
        )
        alternate_surface = _surface(alternate_build, model_call_id="alternate-model-call")
        alternate_binding = ModelCallSurfaceBinding.from_surface(alternate_surface)
        alternate_invocation = alternate_gateway.bind_provider_surface(
            authority=authority,
            build_identity=alternate_build,
            surface=alternate_surface,
            model_call_surface_binding=alternate_binding,
        )
        with pytest.raises(ProjectionError):
            gateway.complete(surface, invocation_identity=alternate_invocation)
        assert len(calls) == 1


def test_gateway_missing_or_alternate_invocation_calls_provider_zero() -> None:
    calls: list[object] = []
    chain = FrozenProviderExecutionChain.freeze(
        [AIProviderProfile(id="one", api_key="secret", base_url="https://one.test/v1")]
    )
    gateway = AgentProviderGatewaySession(
        chain,
        SingleCandidateAgentTransport(
            lambda candidate, *_args: calls.append(candidate) or Assistant(content="ok"),
            lambda *_args: Assistant(content="ok"),
        ),
    )
    with pytest.raises(TypeError):
        gateway.complete(_surface(object()))
    assert calls == []


def test_single_candidate_adapter_requires_exact_registered_attempt_before_callback() -> None:
    calls: list[object] = []
    chain = FrozenProviderExecutionChain.freeze(
        [AIProviderProfile(id="one", api_key="secret", base_url="https://one.test/v1")]
    )
    transport = SingleCandidateAgentTransport(
        lambda candidate, *_args: calls.append(candidate) or Assistant(content="ok"),
        lambda *_args: Assistant(content="ok"),
    )
    gateway = AgentProviderGatewaySession(chain, transport)
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=1,
            conversation_scope_revision=0,
            segment_id="segment",
            trusted_scope=TrustedContextScope(
                context_type="workspace", context_ref=None, mode="general"
            ),
            capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
        )
        runner = object()
        context = SimpleNamespace(authority=authority, authority_factory=factory)
        factory.register_runner_invocation(runner, authority=authority)
        factory.register_tool_execution_context(context, authority=authority)
        build = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-call",
        )
        surface = _surface(build)
        binding = ModelCallSurfaceBinding.from_surface(surface)
        invocation = gateway.bind_provider_surface(
            authority=authority,
            build_identity=build,
            surface=surface,
            model_call_surface_binding=binding,
        )
        attempt_id = gateway._begin_authorized_attempt(invocation, 0)

        with pytest.raises(TypeError):
            transport.complete_one(chain.candidates[0], surface)
        with pytest.raises(ProjectionError):
            transport.complete_one(
                chain.candidates[0],
                surface,
                invocation_identity=invocation,
                provider_attempt_id="",
                candidate_ordinal=0,
                gateway_session=gateway,
            )
        with pytest.raises(ProjectionError):
            transport.complete_one(
                chain.candidates[0],
                surface,
                invocation_identity=invocation,
                provider_attempt_id=attempt_id,
                candidate_ordinal=1,
                gateway_session=gateway,
            )
        assert calls == []

        response = transport.complete_one(
            chain.candidates[0],
            surface,
            invocation_identity=invocation,
            provider_attempt_id=attempt_id,
            candidate_ordinal=0,
            gateway_session=gateway,
        )
        assert response.content == "ok"
        assert len(calls) == 1
        with pytest.raises(ProjectionError):
            transport.complete_one(
                chain.candidates[0],
                surface,
                invocation_identity=invocation,
                provider_attempt_id=attempt_id,
                candidate_ordinal=0,
                gateway_session=gateway,
            )
        assert len(calls) == 1


def test_configured_client_requires_identity_before_provider_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("offerpilot.ai.client.completion", lambda **payload: calls.append(payload))
    client = ConfiguredAIClient(Config(api_key="secret"))
    with pytest.raises(TypeError):
        client.complete_agent_surface(_surface(object()))
    assert calls == []
