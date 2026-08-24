from __future__ import annotations

import copy
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any

import pytest

from offerpilot.ai.tool_authority import (
    AuthorityFactory,
    AuthorityPhaseError,
    BindingTargetResolution,
    ExecutionClaim,
    TrustedContextScope,
    execution_scope,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolSpec,
)


SHA = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _scope() -> TrustedContextScope:
    return TrustedContextScope("application", 37, "general")


def _segment(factory: AuthorityFactory):
    return factory.create_segment_authority(
        conversation_id=11,
        conversation_scope_revision=0,
        segment_id="segment-hardening",
        trusted_scope=_scope(),
        capabilities=frozenset({"applications.read", "applications.write"}),
        capability_profile_fingerprint=SHA,
        binding_policy_fingerprint=SHA_B,
    )


def _prepared(
    factory: AuthorityFactory,
    authority: Any,
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "get_application",
    kind: str = "read",
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
    )
    prepared = PreparedToolCall(
        tool_call_id=tool_call_id,
        spec=spec,
        arguments={},
        typed_args={},
        arguments_digest=SHA,
        contract_fingerprint=SHA_B,
        binding=BindingAudit(status="unbound", target_count=0),
    )
    seal = factory.issue_prepared_construction_identity(authority)
    factory.bind_new_prepared(prepared, authority, seal)
    return prepared


def _registered_invocation(factory: AuthorityFactory, authority: Any):
    runner = object()
    context = object()
    surface = object()
    binding = object()
    gateway = object()
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=SHA,
        candidate_count=2,
        authority=authority,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=SHA,
        authority=authority,
    )
    factory.register_gateway_session(gateway, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-1",
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=SHA,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return invocation, runner, context, surface, binding, gateway


def test_surface_components_must_be_registered_and_attempt_factory_issued() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        with pytest.raises(AuthorityPhaseError):
            factory.create_provider_surface_build_identity(
                authority,
                runner_invocation=object(),
                tool_context=object(),
                model_call_id="model-1",
            )

        invocation, runner, context, surface, binding, gateway = _registered_invocation(
            factory, authority
        )
        with pytest.raises(AuthorityPhaseError):
            factory.create_provider_invocation_identity(
                factory.create_provider_surface_build_identity(
                    authority,
                    runner_invocation=runner,
                    tool_context=context,
                    model_call_id="model-2",
                ),
                surface=object(),
                surface_fingerprint=SHA,
                model_call_surface_binding=binding,
                gateway_session=gateway,
            )
        with pytest.raises(AuthorityPhaseError):
            factory.create_new_turn_prepare_identity(
                invocation,
                attempt_id="caller-chosen",
                candidate_ordinal=0,
                tool_call_id="call-1",
                tool_name="get_application",
                arguments_digest=SHA,
            )
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
        prepared_identity = factory.create_new_turn_prepare_identity(
            invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id="call-1",
            tool_name="get_application",
            arguments_digest=SHA,
        )
        assert prepared_identity.attempt_id == attempt


def test_live_identity_objects_cannot_be_re_registered_in_another_factory() -> None:
    with execution_scope() as first, execution_scope() as second:
        first_authority = _segment(first)
        second_authority = _segment(second)
        runner = object()
        surface = object()
        first.register_runner_invocation(runner, authority=first_authority)
        first.register_frozen_surface(surface, surface_fingerprint=SHA, authority=first_authority)
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id="call-1",
            tool_name="get_application",
            pending_action_revision=1,
            arguments_digest=SHA,
        )
        first.register_pending(pending)
        prepared = _prepared(first, first_authority)
        with pytest.raises(AuthorityPhaseError):
            second.register_runner_invocation(runner, authority=second_authority)
        with pytest.raises(AuthorityPhaseError):
            second.register_frozen_surface(
                surface,
                surface_fingerprint=SHA,
                authority=second_authority,
            )
        with pytest.raises(AuthorityPhaseError):
            second.register_pending(pending)
        with pytest.raises(AuthorityPhaseError):
            second.register_prepared(prepared, second_authority)


def test_external_none_token_prepared_cannot_be_registered_without_construction_seal() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        raw = PreparedToolCall(
            tool_call_id="call-1",
            spec=SimpleNamespace(name="get_application"),  # type: ignore[arg-type]
            arguments={},
            typed_args={},
            arguments_digest=SHA,
            contract_fingerprint=SHA_B,
            binding=BindingAudit(status="unbound", target_count=0),
        )
        with pytest.raises(AuthorityPhaseError):
            factory.register_prepared(raw, authority)
        prepared = _prepared(factory, authority)
        assert factory.is_active(prepared)
        with pytest.raises(TypeError):
            factory.register_prepared(replace(prepared), authority)


def test_prepared_fields_match_every_identity_and_duplicate_claim_is_rejected() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, *_ = _registered_invocation(factory, authority)
        prepared = _prepared(factory, authority)
        with pytest.raises(AuthorityPhaseError):
            factory.create_read_execution_identity(
                invocation,
                prepared=prepared,
                tool_call_id="different-call",
                tool_name=prepared.spec.name,
                arguments_digest=prepared.arguments_digest,
            )
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="pending-claim-1",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-1",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=prepared.arguments_digest,
        )
        assert claim.arguments_digest == prepared.arguments_digest
        with pytest.raises(AuthorityPhaseError):
            factory.issue_pending_claim(
                authority,
                prepared=prepared,
                pending=pending,
                operation_id="op-1",
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.spec.name,
                arguments_digest=prepared.arguments_digest,
            )


def test_execution_claim_requires_registered_transaction_and_approval_pending_object() -> None:
    with execution_scope() as factory:
        _segment(factory)
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=SHA,
        )
        factory.register_pending(pending)
        with pytest.raises(AuthorityPhaseError):
            factory.create_approval_authority(
                operation_id="op-1",
                conversation_id=11,
                conversation_scope_revision=0,
                trusted_scope=_scope(),
                pending_identity=factory.pending_token(pending),
                pending_action_revision=1,
                tool_call_id="update_application_status",
                tool_name="update_application_status",
                effective_args_digest=SHA,
                capability_profile_fingerprint=SHA,
                binding_policy_fingerprint=SHA_B,
            )
        approval = factory.create_approval_authority(
            operation_id="op-1",
            conversation_id=11,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending,
            pending_action_revision=1,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            effective_args_digest=SHA,
            capability_profile_fingerprint=SHA,
            binding_policy_fingerprint=SHA_B,
        )
        prepared = _prepared(
            factory,
            approval,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            kind="write",
        )
        with pytest.raises(AuthorityPhaseError):
            factory.issue_execution_claim(
                approval,
                prepared=prepared,
                pending=pending,
                operation_id="op-1",
                tool_call_id="update_application_status",
                tool_name="update_application_status",
                effective_args_digest=SHA,
                transaction=object(),
            )
        transaction = object()
        factory.register_transaction(transaction)
        claim = factory.issue_execution_claim(
            approval,
            prepared=prepared,
            pending=pending,
            operation_id="op-1",
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            effective_args_digest=SHA,
            transaction=transaction,
        )
        assert isinstance(claim, ExecutionClaim)


def test_omitted_proof_requires_registered_operation_pending_transaction_and_claim_id() -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-1",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-1",
            tool_name="create_application_event",
            proposal_fingerprint=SHA,
            confirmation_token_fingerprint=SHA_B,
            pending_confirmation_claim_id="pending-claim-1",
        )
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id="call-1",
            tool_name="create_application_event",
            pending_action_revision=1,
            arguments_digest=SHA,
            pending_confirmation_claim_id="pending-claim-1",
        )
        transaction = object()
        with pytest.raises(AuthorityPhaseError):
            factory.issue_omitted_token_proof(
                operation=operation,
                pending_pointer=pending,
                transaction=transaction,
            )
        factory.register_operation(operation)
        factory.register_pending(pending)
        factory.register_transaction(transaction)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=transaction,
        )
        assert proof.pending_confirmation_claim_id == "pending-claim-1"
        assert factory.proof_state(proof) == "issued"
        bad_fields = vars(operation).copy()
        bad_fields["status"] = "terminal"
        bad = SimpleNamespace(**bad_fields)
        with pytest.raises(AuthorityPhaseError):
            factory.issue_omitted_token_proof(
                operation=bad,
                pending_pointer=pending,
                transaction=transaction,
            )


def test_revoke_cleans_all_associated_objects_and_double_consume_is_atomic() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority)
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="pending-claim-1",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-1",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=prepared.arguments_digest,
        )
        factory.mark_in_flight(claim)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: _consume(factory, claim), (1, 2)))
        assert sum(results) == 1
        assert factory.claim_state(claim) is None
        factory.revoke_authority(authority)
        assert factory.active_count == 0
        assert factory.registry_size == 0
        assert not factory.is_active(prepared)


def _consume(factory: AuthorityFactory, claim: Any) -> int:
    try:
        factory.consume(claim)
    except AuthorityPhaseError:
        return 0
    return 1


def test_contracts_close_shape_and_identity_equality() -> None:
    with execution_scope() as factory:
        first = _segment(factory)
        second = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-hardening-2",
            trusted_scope=_scope(),
            capabilities=frozenset({"applications.read", "applications.write"}),
            capability_profile_fingerprint=SHA,
            binding_policy_fingerprint=SHA_B,
        )
        with pytest.raises(TypeError):
            replace(first, authority_instance_token=second.authority_instance_token)
        with pytest.raises(ValueError):
            BindingTargetResolution(
                entity_kind="offer",  # type: ignore[arg-type]
                state="omitted",
                identity=None,
                authority_instance_token=first.authority_instance_token,
            )
        with pytest.raises(ValueError):
            factory.create_segment_authority(
                conversation_id=11,
                conversation_scope_revision=0,
                segment_id="segment-invalid",
                trusted_scope=_scope(),
                capabilities=frozenset({object()}),
                capability_profile_fingerprint="sha256:short",
                binding_policy_fingerprint=SHA_B,
            )
        with pytest.raises(TypeError):
            asdict(_scope())
        with pytest.raises(TypeError):
            copy.deepcopy(_scope())
        with pytest.raises(TypeError):
            pickle.dumps(_scope())
