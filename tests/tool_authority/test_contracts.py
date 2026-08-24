from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from offerpilot.ai.tool_authority import (
    ApprovedWriteExecuteCallIdentity,
    ApprovedWritePrepareCallIdentity,
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    BindingTargetResolution,
    ExecutionClaim,
    NewTurnPrepareCallIdentity,
    PendingAuthorityClaim,
    ProviderInvocationIdentity,
    ProviderSurfaceBuildIdentity,
    ReadExecutionCallIdentity,
    SegmentExecutionAuthority,
    ToolExecutionAuthority,
    TrustedContextScope,
    TrustedLedgerOmittedTokenProof,
    TypedPendingCallIdentity,
    require_authority_phase,
    require_authority_spec,
    execution_scope,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolSpec,
)


MAX_INT64 = 2**63 - 1


def _scope() -> TrustedContextScope:
    return TrustedContextScope(context_type="application", context_ref=37, mode="general")


def _segment(factory: AuthorityFactory) -> SegmentExecutionAuthority:
    return factory.create_segment_authority(
        conversation_id=11,
        conversation_scope_revision=0,
        segment_id="segment-1",
        trusted_scope=_scope(),
        capabilities=frozenset({"applications.read"}),
    )


def _prepared(authority: SegmentExecutionAuthority) -> PreparedToolCall[Any, Any]:
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {"name": "get_application", "description": "", "parameters": {}},
            },
            name="get_application",
            description="",
            parameters={},
        ),
        kind="read",
        decoder=lambda value: value,
        executor=lambda args, context: args,
    )
    return PreparedToolCall(
        tool_call_id="call-1",
        spec=spec,
        arguments={},
        typed_args={},
        arguments_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        binding=BindingAudit(status="unbound", target_count=0),
        authority_instance_token=authority.authority_instance_token,
    )


def test_authority_types_are_separate_and_segment_is_tool_execution_authority() -> None:
    with execution_scope() as factory:
        segment = _segment(factory)
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=factory.register_pending(object()),
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest="sha256:" + "a" * 64,
            capabilities=frozenset({"applications.write"}),
        )

        assert isinstance(segment, ToolExecutionAuthority)
        assert isinstance(approval, ToolExecutionAuthority)
        assert type(segment) is not type(approval)
        assert isinstance(segment, SegmentExecutionAuthority)


def test_positive_int64_is_strict_for_contract_identities() -> None:
    with execution_scope() as factory:
        for value in (True, False, 1.0, "1", 0, -1, MAX_INT64 + 1):
            with pytest.raises((TypeError, ValueError)):
                factory.create_segment_authority(
                    conversation_id=value,  # type: ignore[arg-type]
                    conversation_scope_revision=0,
                    segment_id="segment-1",
                    trusted_scope=_scope(),
                    capabilities=frozenset(),
                )

        for value in (True, 1.0, "37", 0, -1, MAX_INT64 + 1):
            with pytest.raises((TypeError, ValueError)):
                BindingTargetResolution(
                    entity_kind="application",
                    state="resolved",
                    identity=value,  # type: ignore[arg-type]
                    authority_instance_token=object(),  # type: ignore[arg-type]
                )


def test_phase_matrix_rejects_wrong_authority_and_wrong_identity_before_lookup() -> None:
    with execution_scope() as factory:
        segment = _segment(factory)
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=factory.register_pending(object()),
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest="sha256:" + "a" * 64,
            capabilities=frozenset({"applications.write"}),
        )
        context = object()
        runner = object()
        surface = object()
        binding = object()
        gateway = object()
        build = factory.create_provider_surface_build_identity(
            segment,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-1",
        )
        invocation = factory.create_provider_invocation_identity(
            build,
            surface=surface,
            surface_fingerprint="sha256:" + "c" * 64,
            model_call_surface_binding=binding,
            gateway_session=gateway,
        )
        pending = factory.register_pending(object())
        prepared = _prepared(segment)
        factory.register_prepared(prepared, segment)
        read = factory.create_read_execution_identity(
            invocation,
            prepared=prepared,
            tool_call_id="call-1",
            tool_name="get_application",
            arguments_digest=prepared.arguments_digest,
        )

        assert require_authority_phase(segment, "provider_surface_build", build) is None
        assert require_authority_phase(segment, "provider_invoke", invocation) is None
        assert require_authority_phase(segment, "read_execute", read) is None

        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(approval, "provider_invoke", invocation)
        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(segment, "approved_write_execute", read)
        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(segment, "provider_invoke", build)

        same_fields = replace(read)
        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(segment, "read_execute", same_fields)

        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(segment, "typed_pending_claim", read)
        del pending


def test_spec_gate_is_fail_closed_for_approval_and_segment_write() -> None:
    with execution_scope() as factory:
        segment = _segment(factory)
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=factory.register_pending(object()),
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest="sha256:" + "a" * 64,
            capabilities=frozenset({"applications.write"}),
        )
        read_spec = type("Spec", (), {"kind": "read", "confirmation_policy": "none"})()
        write_spec = type("Spec", (), {"kind": "write", "confirmation_policy": "required"})()

        with pytest.raises(AuthorityPhaseError):
            require_authority_spec(approval, "approved_write_prepare", read_spec)
        assert require_authority_spec(approval, "approved_write_prepare", write_spec) is None
        with pytest.raises(AuthorityPhaseError):
            require_authority_spec(segment, "read_execute", write_spec)


def test_constraint_and_resolution_invariants_are_closed() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        token = authority.authority_instance_token
        assert ApplicationScopeConstraint(
            entity_kind="application",
            mode="unrestricted",
            allowed_identities=frozenset(),
            authority_instance_token=token,
        ).allowed_identities == frozenset()
        with pytest.raises(ValueError):
            ApplicationScopeConstraint(
                entity_kind="application",
                mode="unrestricted",
                allowed_identities=frozenset({37}),
                authority_instance_token=token,
            )
        with pytest.raises(ValueError):
            ApplicationScopeConstraint(
                entity_kind="application",
                mode="restricted",
                allowed_identities=frozenset(),
                authority_instance_token=token,
            )
        with pytest.raises(ValueError):
            BindingTargetResolution(
                entity_kind="application",
                state="omitted",
                identity=37,
                authority_instance_token=token,
            )


def test_claims_are_one_shot_and_scope_exit_revokes_active_values() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(authority)
        factory.register_prepared(prepared, authority)
        pending_object = object()
        pending = factory.register_pending(pending_object)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending_object,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="create_application_event",
            arguments_digest=prepared.arguments_digest,
        )
        assert isinstance(claim, PendingAuthorityClaim)
        assert factory.claim_state(claim) == "issued"
        factory.mark_in_flight(claim)
        assert factory.claim_state(claim) == "in_flight"
        factory.consume(claim)
        assert factory.claim_state(claim) is None
        with pytest.raises(AuthorityPhaseError):
            factory.mark_in_flight(claim)
        assert pending is not None

    assert factory.active_count == 0


def test_approval_execution_claim_binds_prepared_and_authority_identity() -> None:
    with execution_scope() as factory:
        pending_object = object()
        pending = factory.register_pending(pending_object)
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending,
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest="sha256:" + "a" * 64,
            capabilities=frozenset({"applications.write"}),
        )
        prepared = _prepared(approval)
        factory.register_prepared(prepared, approval)
        claim = factory.issue_execution_claim(
            approval,
            prepared=prepared,
            pending=pending_object,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest=approval.effective_args_digest,
        )
        assert isinstance(claim, ExecutionClaim)
        assert claim.prepared_instance_token is factory.prepared_token(prepared)
        factory.mark_in_flight(claim)
        factory.consume(claim)
        with pytest.raises(AuthorityPhaseError):
            factory.consume(claim)


def test_call_identity_variants_are_closed_types() -> None:
    assert issubclass(ProviderSurfaceBuildIdentity, object)
    assert issubclass(ProviderInvocationIdentity, object)
    assert issubclass(NewTurnPrepareCallIdentity, object)
    assert issubclass(ReadExecutionCallIdentity, object)
    assert issubclass(TypedPendingCallIdentity, object)
    assert issubclass(ApprovedWritePrepareCallIdentity, object)
    assert issubclass(ApprovedWriteExecuteCallIdentity, object)
    assert issubclass(TrustedLedgerOmittedTokenProof, object)
    assert issubclass(PendingAuthorityClaim, object)
