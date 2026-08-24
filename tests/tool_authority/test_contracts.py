from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from offerpilot.ai.tool_authority import (
    ApprovedWriteExecuteCallIdentity,
    ApprovedWritePrepareCallIdentity,
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    ApprovalExecutionAuthority,
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
ARG_DIGEST = "sha256:" + hashlib.sha256(b"{}").hexdigest()


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


def _prepared(
    factory: AuthorityFactory,
    authority: ToolExecutionAuthority,
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "get_application",
    kind: str = "read",
    invocation: ProviderInvocationIdentity | None = None,
) -> PreparedToolCall[Any, Any]:
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {"name": tool_name, "description": "", "parameters": {}},
            },
            name=tool_name,
            description="",
            parameters={},
        ),
        kind=kind,  # type: ignore[arg-type]
        decoder=lambda value: value,
        executor=lambda args, context: args,
        confirmation_policy="required" if kind == "write" else "none",
    )
    if isinstance(authority, ApprovalExecutionAuthority):
        prepare_identity = factory.create_approved_write_prepare_identity(
            authority,
            approval_context=object(),
            request_identity=object(),
        )
    else:
        if invocation is None:
            runner = object()
            context = object()
            surface = object()
            binding = object()
            gateway = object()
            factory.register_runner_invocation(runner, authority=authority)
            factory.register_tool_execution_context(context, authority=authority)
            build = factory.create_provider_surface_build_identity(
                authority,
                runner_invocation=runner,
                tool_context=context,
                model_call_id="model-prepare",
            )
            factory.register_frozen_surface(
                surface,
                surface_fingerprint="sha256:" + "c" * 64,
                authority=authority,
                build_identity=build,
            )
            factory.register_model_call_surface_binding(
                binding,
                surface=surface,
                surface_fingerprint="sha256:" + "c" * 64,
                authority=authority,
                build_identity=build,
            )
            factory.register_gateway_session(
                gateway,
                authority=authority,
                build_identity=build,
                surface=surface,
                surface_fingerprint="sha256:" + "c" * 64,
                model_call_surface_binding=binding,
            )
            invocation = factory.create_provider_invocation_identity(
                build,
                surface=surface,
                surface_fingerprint="sha256:" + "c" * 64,
                model_call_surface_binding=binding,
                gateway_session=gateway,
            )
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
        prepare_identity = factory.create_new_turn_prepare_identity(
            invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_digest=ARG_DIGEST,
        )
    factory.register_tool_spec(
        spec,
        authority=authority,
        prepare_identity=prepare_identity,
    )
    contract_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            dict(spec.contract.payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return factory.prepare_tool_call(
        authority,
        prepare_identity=prepare_identity,
        tool_call_id=tool_call_id,
        spec=spec,
        arguments={},
        typed_args={},
        arguments_digest=ARG_DIGEST,
        contract_fingerprint=contract_fingerprint,
        binding=BindingAudit(status="unbound", target_count=0),
    )


def test_authority_types_are_separate_and_segment_is_tool_execution_authority() -> None:
    with execution_scope() as factory:
        segment = _segment(factory)
        pending_object = object()
        factory.register_pending(
            pending_object,
            conversation_id=11,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=ARG_DIGEST,
        )
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending_object,
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest=ARG_DIGEST,
            capabilities=frozenset({"applications.write"}),
        )

        assert isinstance(segment, ToolExecutionAuthority)
        assert isinstance(approval, ToolExecutionAuthority)
        assert type(segment) is not type(approval)
        assert isinstance(segment, SegmentExecutionAuthority)


def test_contracts_star_import_does_not_expose_private_opaque_minting() -> None:
    namespace: dict[str, object] = {}
    exec("from offerpilot.ai.tool_authority.contracts import *", namespace)
    assert "_new_opaque_handle" not in namespace


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
        pending_object = object()
        factory.register_pending(
            pending_object,
            conversation_id=11,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=ARG_DIGEST,
        )
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending_object,
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest=ARG_DIGEST,
            capabilities=frozenset({"applications.write"}),
        )
        context = object()
        runner = object()
        surface = object()
        binding = object()
        gateway = object()
        factory.register_runner_invocation(runner, authority=segment)
        factory.register_tool_execution_context(context, authority=segment)
        build = factory.create_provider_surface_build_identity(
            segment,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-1",
        )
        factory.register_frozen_surface(
            surface,
            surface_fingerprint="sha256:" + "c" * 64,
            authority=segment,
            build_identity=build,
        )
        factory.register_model_call_surface_binding(
            binding,
            surface=surface,
            surface_fingerprint="sha256:" + "c" * 64,
            authority=segment,
            build_identity=build,
        )
        factory.register_gateway_session(
            gateway,
            authority=segment,
            build_identity=build,
            surface=surface,
            surface_fingerprint="sha256:" + "c" * 64,
            model_call_surface_binding=binding,
        )
        invocation = factory.create_provider_invocation_identity(
            build,
            surface=surface,
            surface_fingerprint="sha256:" + "c" * 64,
            model_call_surface_binding=binding,
            gateway_session=gateway,
        )
        prepared = _prepared(factory, segment, invocation=invocation)
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

        with pytest.raises(TypeError):
            replace(read)

        with pytest.raises(AuthorityPhaseError):
            require_authority_phase(segment, "typed_pending_claim", read)


def test_spec_gate_is_fail_closed_for_approval_and_segment_write() -> None:
    with execution_scope() as factory:
        segment = _segment(factory)
        pending_object = object()
        factory.register_pending(
            pending_object,
            conversation_id=11,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=ARG_DIGEST,
        )
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending_object,
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest=ARG_DIGEST,
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
        prepared = _prepared(factory, authority, kind="write")
        pending_object = object()
        pending = factory.register_pending(
            pending_object,
            conversation_id=11,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="get_application",
            pending_action_revision=1,
            pending_confirmation_claim_id="pending-claim-1",
            arguments_digest=ARG_DIGEST,
        )
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending_object,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="get_application",
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
        factory.register_pending(
            pending_object,
            conversation_id=11,
            operation_id="op-1",
            tool_call_id="call-1",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=ARG_DIGEST,
        )
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending_object,
            pending_action_revision=1,
            tool_call_id="call-1",
            tool_name="update_application_status",
            effective_args_digest=ARG_DIGEST,
            capabilities=frozenset({"applications.write"}),
        )
        prepared = _prepared(
            factory,
            approval,
            tool_name="update_application_status",
            kind="write",
        )
        with Session() as session:
            transaction = session.begin()
            factory.register_transaction(transaction)
            claim = factory.issue_execution_claim(
                approval,
                prepared=prepared,
                pending=pending_object,
                operation_id="op-1",
                tool_call_id="call-1",
                tool_name="update_application_status",
                effective_args_digest=approval.effective_args_digest,
                session=session,
                transaction=transaction,
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
