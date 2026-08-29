from __future__ import annotations

from collections.abc import Iterator
import copy
import hashlib
import json
import math
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session, SessionTransaction

import offerpilot.ai.tool_authority.composition as authority_composition
from offerpilot.ai.tool_authority import (
    AuthorityFactory,
    AuthorityPhaseError,
    AuthorityUse,
    ApprovalExecutionAuthority,
    BindingTargetResolution,
    ExecutionClaim,
    TrustedContextScope,
    TrustedLedgerOmittedTokenProof,
    execution_scope,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolExceptionMapping,
    ToolSpec,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    EditableFieldMetadataV1,
    ToolMetadataBundleV1,
    ToolPresentationBindingV1,
)
from offerpilot.ai.tool_runtime.catalog import (
    SegmentToolCatalogLease,
    SegmentToolSpecHandle,
    ToolCatalog,
)
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    read_metadata,
    resolver_binding,
    resolver_descriptor,
    synthetic_tool_spec,
    write_metadata,
)


SHA = "sha256:" + hashlib.sha256(b"{}").hexdigest()
SHA_B = "sha256:" + "b" * 64
HMAC = "hmac-sha256:" + "c" * 64
HMAC_B = "hmac-sha256:" + "d" * 64
_LEASES: list[object] = []
_ROUTES: dict[int, tuple[object, object, dict[str, object]]] = {}


@pytest.fixture(autouse=True)
def _close_segment_leases() -> Iterator[None]:
    yield
    _ROUTES.clear()
    while _LEASES:
        getattr(_LEASES.pop(), "close")()


def _forged_confirmation_description(_args: object) -> str:
    return "forged confirmation"


def _forged_pending_details(_args: object) -> dict[str, bool]:
    return {"forged": True}


def _forged_success_summary(_result: object) -> str:
    return "forged success"


@pytest.fixture
def active_transaction() -> Iterator[SessionTransaction]:
    with Session() as session, session.begin() as transaction:
        yield transaction


def _args_digest(arguments: object) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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


def _bind_specs(
    factory: AuthorityFactory,
    authority: Any,
    *specs: ToolSpec[Any, Any],
) -> tuple[SegmentToolCatalogLease, tuple[SegmentToolSpecHandle, ...]]:
    catalog = ToolCatalog(specs, expected_names=tuple(spec.name for spec in specs))
    source = compose_synthetic_bundle()
    manifest = {**source["manifest"], "typed_tools": tuple(spec.name for spec in specs)}
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    _LEASES.append(lease)
    factory.bind_segment_tool_catalog(
        authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    handles = tuple(lease.resolve(spec.name) for spec in specs)
    assert all(handle is not None for handle in handles)
    return lease, tuple(cast(SegmentToolSpecHandle, handle) for handle in handles)


def _prepared(
    factory: AuthorityFactory,
    authority: Any,
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "get_application",
    kind: str = "read",
) -> PreparedToolCall[Any, Any]:
    if kind == "write" and tool_name == "get_application":
        tool_name = "update_application"

    route = _ROUTES.get(id(authority))
    if route is None:
        specs: list[object] = []
        for candidate_name, candidate_kind in (
            ("get_application", "read"),
            ("update_application", "write"),
            ("update_application_status", "write"),
        ):
            parameters: dict[str, object] = {"type": "object", "properties": {}}
            contract = ProviderToolContract(
                payload={
                    "type": "function",
                    "function": {
                        "name": candidate_name,
                        "description": "",
                        "parameters": parameters,
                    },
                },
                name=candidate_name,
                description="",
                parameters=parameters,
            )
            metadata = (
                write_metadata(candidate_name)
                if candidate_kind == "write"
                else read_metadata(candidate_name)
            )
            if candidate_kind == "write":
                metadata = replace(metadata, editable_fields=())
            specs.append(
                replace(
                    synthetic_tool_spec(candidate_name, metadata=metadata),
                    contract=contract,
                    decoder=lambda value: value,
                    executor=lambda args, context: args,
                )
            )
        catalog = ToolCatalog(tuple(specs), expected_names=tuple(spec.name for spec in specs))
        source = compose_synthetic_bundle()
        manifest = {**source["manifest"], "typed_tools": tuple(spec.name for spec in specs)}
        bundle = ToolMetadataBundleV1(
            typed_catalog=catalog,
            manifest=manifest,
            legacy_boundary=source["legacy_boundary"],
            compensation=source["compensation"],
        )
        lease = bundle.open_segment_lease()
        _LEASES.append(lease)
        factory.bind_segment_tool_catalog(
            authority,
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=lease,
        )
        route = (bundle, lease, {spec.name: spec for spec in specs})
        _ROUTES[id(authority)] = route
    _, lease, specs_by_name = route
    spec = specs_by_name[tool_name]
    if isinstance(authority, ApprovalExecutionAuthority):
        prepare_identity = factory.create_approved_write_prepare_identity(
            authority,
            approval_context=object(),
            request_identity=object(),
        )
    else:
        invocation, *_ = _registered_invocation(factory, authority)
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
        prepare_identity = factory.create_new_turn_prepare_identity(
            invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_digest=SHA,
        )
    spec_handle = lease.resolve(tool_name)
    assert spec_handle is not None
    factory.register_tool_spec(
        spec_handle,
        catalog_lease=lease,
        authority=authority,
        prepare_identity=prepare_identity,
    )
    contract_fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                materialize_provider_payloads((spec.contract,))[0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    return factory.prepare_tool_call(
        authority,
        catalog_lease=lease,
        spec_handle=spec_handle,
        prepare_identity=prepare_identity,
        tool_call_id=tool_call_id,
        arguments={},
        typed_args={},
        arguments_digest=SHA,
        contract_fingerprint=contract_fingerprint,
        binding=BindingAudit(status="unbound", target_count=0),
    )


def _registered_invocation(factory: AuthorityFactory, authority: Any):
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
        model_call_id="model-1",
    )
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=SHA,
        candidate_count=2,
        authority=authority,
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=SHA,
        authority=authority,
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build,
        surface=surface,
        surface_fingerprint=SHA,
        model_call_surface_binding=binding,
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
        context = object()
        surface = object()
        first.register_runner_invocation(runner, authority=first_authority)
        first.register_tool_execution_context(context, authority=first_authority)
        build = first.create_provider_surface_build_identity(
            first_authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-cross",
        )
        first.register_frozen_surface(
            surface,
            surface_fingerprint=SHA,
            authority=first_authority,
            build_identity=build,
        )
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
                build_identity=build,
            )
        with pytest.raises(AuthorityPhaseError):
            second.register_pending(pending)
        with pytest.raises(AuthorityPhaseError):
            second.register_prepared(prepared, second_authority)


def test_external_none_token_prepared_cannot_be_registered_without_construction_seal() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        with pytest.raises(TypeError, match="spec_handle"):
            PreparedToolCall(
                tool_call_id="call-1",
                spec=SimpleNamespace(name="get_application"),  # type: ignore[arg-type]
                arguments={},
                typed_args={},
                arguments_digest=SHA,
                contract_fingerprint=SHA_B,
                binding=BindingAudit(status="unbound", target_count=0),
            )
        prepared = _prepared(factory, authority)
        assert factory.is_active(prepared)
        with pytest.raises(TypeError):
            factory.register_prepared(replace(prepared), authority)


def test_prepared_fields_match_every_identity_and_duplicate_claim_is_rejected() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, *_ = _registered_invocation(factory, authority)
        prepared = _prepared(factory, authority, kind="write")
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


def test_factory_ports_enforce_prepared_tool_phase_before_side_effects() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, runner, context, *_ = _registered_invocation(factory, authority)
        write_prepared = _prepared(factory, authority, kind="write")
        before_read = factory.active_count
        with pytest.raises(AuthorityPhaseError):
            factory.create_read_execution_identity(
                invocation,
                prepared=write_prepared,
                tool_call_id=write_prepared.tool_call_id,
                tool_name=write_prepared.spec.name,
                arguments_digest=write_prepared.arguments_digest,
            )
        assert factory.active_count == before_read

        read_prepared = _prepared(factory, authority, kind="read")
        read_pending = SimpleNamespace(
            operation_id="op-read-pending",
            conversation_id=11,
            tool_call_id=read_prepared.tool_call_id,
            tool_name=read_prepared.spec.name,
            pending_action_revision=1,
            arguments_digest=SHA,
            pending_confirmation_claim_id="read-claim",
        )
        factory.register_pending(read_pending)
        before_typed = factory.active_count
        with pytest.raises(AuthorityPhaseError):
            factory.create_typed_pending_identity(
                authority=authority,
                runner_invocation=runner,
                tool_context=context,
                prepared=read_prepared,
                pending=read_pending,
                operation_id="op-read-pending",
                pending_action_revision=1,
                tool_call_id=read_prepared.tool_call_id,
                tool_name=read_prepared.spec.name,
                arguments_digest=SHA,
            )
        assert factory.active_count == before_typed

        before_claim = factory.active_count
        with pytest.raises(AuthorityPhaseError):
            factory.issue_pending_claim(
                authority,
                prepared=read_prepared,
                pending=read_pending,
                operation_id="op-read-pending",
                tool_call_id=read_prepared.tool_call_id,
                tool_name=read_prepared.spec.name,
                arguments_digest=SHA,
                pending_confirmation_claim_id="read-claim",
            )
        assert factory.active_count == before_claim


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
        factory.begin_prepared_execution(
            prepared,
            authority=approval,
            use=AuthorityUse.APPROVED_WRITE_PREPARE,
        )
        with Session() as session:
            transaction = session.begin()
            with pytest.raises(AuthorityPhaseError):
                factory.issue_execution_claim(
                    approval,
                    prepared=prepared,
                    pending=pending,
                    operation_id="op-1",
                    tool_call_id="update_application_status",
                    tool_name="update_application_status",
                    effective_args_digest=SHA,
                    session=session,
                    transaction=transaction,
                )
            factory.register_transaction(transaction)
            claim = factory.issue_execution_claim(
                approval,
                prepared=prepared,
                pending=pending,
                operation_id="op-1",
                tool_call_id="update_application_status",
                tool_name="update_application_status",
                effective_args_digest=SHA,
                session=session,
                transaction=transaction,
            )
            assert isinstance(claim, ExecutionClaim)


def test_omitted_proof_requires_registered_operation_pending_transaction_and_claim_id(
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-1",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-1",
            tool_name="create_application_event",
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
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
        transaction = active_transaction
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
        prepared = _prepared(factory, authority, kind="write")
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


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("tool_name", "mutated_tool"),
        ("arguments_digest", SHA_B),
        ("pending_action_revision", 2),
        ("pending_confirmation_claim_id", "mutated-claim"),
    ),
)
def test_pending_mutation_fails_closed_before_claim_side_effect(
    field: str, replacement: object
) -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-1",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="pending-claim-1",
            arguments_digest=SHA,
            effective_args_digest=SHA,
        )
        factory.register_pending(pending)
        before = factory.active_count
        setattr(pending, field, replacement)
        with pytest.raises(AuthorityPhaseError):
            factory.issue_pending_claim(
                authority,
                prepared=prepared,
                pending=pending,
                operation_id="op-1",
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.spec.name,
                arguments_digest=SHA,
                pending_action_revision=1,
                pending_confirmation_claim_id="pending-claim-1",
            )
        assert factory.active_count == before


def test_provider_components_are_bound_to_exact_build_provenance() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        runner = object()
        context = object()
        factory.register_runner_invocation(runner, authority=authority)
        factory.register_tool_execution_context(context, authority=authority)
        build_one = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-1",
        )
        build_two = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-2",
        )
        surface_one = object()
        binding_one = object()
        gateway_one = object()
        factory.register_frozen_surface(
            surface_one,
            surface_fingerprint=SHA,
            candidate_count=2,
            authority=authority,
            build_identity=build_one,
        )
        factory.register_model_call_surface_binding(
            binding_one,
            surface=surface_one,
            surface_fingerprint=SHA,
            authority=authority,
            build_identity=build_one,
        )
        factory.register_gateway_session(
            gateway_one,
            authority=authority,
            build_identity=build_one,
            surface=surface_one,
            surface_fingerprint=SHA,
            model_call_surface_binding=binding_one,
        )
        with pytest.raises(AuthorityPhaseError):
            factory.register_frozen_surface(
                object(),
                surface_fingerprint=SHA,
                authority=authority,
                build_identity=build_one,
            )
        with pytest.raises(AuthorityPhaseError):
            factory.register_model_call_surface_binding(
                object(),
                surface=surface_one,
                surface_fingerprint=SHA,
                authority=authority,
                build_identity=build_one,
            )
        with pytest.raises(AuthorityPhaseError):
            factory.register_gateway_session(
                object(),
                authority=authority,
                build_identity=build_one,
            )
        with pytest.raises(AuthorityPhaseError):
            factory.create_provider_invocation_identity(
                build_two,
                surface=surface_one,
                surface_fingerprint=SHA,
                model_call_surface_binding=binding_one,
                gateway_session=gateway_one,
            )


def test_prepare_port_requires_registered_prepare_call_spec_and_exact_args_digest() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, *_ = _registered_invocation(factory, authority)
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
        digest = _args_digest({})
        prepare_identity = factory.create_new_turn_prepare_identity(
            invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id="call-1",
            tool_name="get_application",
            arguments_digest=digest,
        )
        parameters: dict[str, object] = {"type": "object", "properties": {}}
        contract = ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "name": "get_application",
                    "description": "",
                    "parameters": parameters,
                },
            },
            name="get_application",
            description="",
            parameters=parameters,
        )
        spec = replace(
            synthetic_tool_spec("get_application"),
            contract=contract,
            decoder=lambda value: value,
            executor=lambda args, context: args,
        )
        lease, (spec_handle,) = _bind_specs(factory, authority, spec)
        factory.register_tool_spec(
            spec_handle,
            catalog_lease=lease,
            authority=authority,
            prepare_identity=prepare_identity,
        )
        contract_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    materialize_provider_payloads((spec.contract,))[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        with pytest.raises(AuthorityPhaseError):
            factory.prepare_tool_call(
                authority,
                catalog_lease=lease,
                spec_handle=spec_handle,
                prepare_identity=prepare_identity,
                tool_call_id="call-1",
                arguments={},
                typed_args={},
                arguments_digest=SHA_B,
                contract_fingerprint=contract_fingerprint,
                binding=BindingAudit(status="unbound", target_count=0),
            )
        unregistered_handle = lease.resolve(spec.name)
        assert unregistered_handle is not None
        with pytest.raises(AuthorityPhaseError):
            factory.prepare_tool_call(
                authority,
                catalog_lease=lease,
                spec_handle=unregistered_handle,
                prepare_identity=prepare_identity,
                tool_call_id="call-1",
                arguments={},
                typed_args={},
                arguments_digest=_args_digest({}),
                contract_fingerprint=SHA_B,
                binding=BindingAudit(status="unbound", target_count=0),
            )
        second = _segment(factory)
        with pytest.raises(AuthorityPhaseError):
            factory.prepare_tool_call(
                second,
                catalog_lease=lease,
                spec_handle=spec_handle,
                prepare_identity=prepare_identity,
                tool_call_id="call-1",
                arguments={},
                typed_args={},
                arguments_digest=_args_digest({}),
                contract_fingerprint=SHA_B,
                binding=BindingAudit(status="unbound", target_count=0),
            )


def test_prepared_route_rejects_same_content_catalog_topology_replacement() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority)
        _bundle, lease, _specs_by_name = _ROUTES[id(authority)]
        catalog = object.__getattribute__(lease, "_catalog")
        ordered = object.__getattribute__(catalog, "_ordered")

        replacement = tuple(list(ordered))
        assert replacement == ordered
        assert replacement is not ordered
        object.__setattr__(catalog, "_ordered", replacement)

        with pytest.raises(AuthorityPhaseError, match="provenance"):
            factory.require_prepared_call(prepared, authority)


@pytest.mark.parametrize("mutation", ["resolver_descriptor", "resolver_callable"])
def test_registered_tool_spec_snapshot_rejects_resolver_identity_mutation(
    mutation: str,
) -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, *_ = _registered_invocation(factory, authority)
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
        prepare_identity = factory.create_new_turn_prepare_identity(
            invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id="call-binding-integrity",
            tool_name="get_application",
            arguments_digest=SHA,
        )
        calls = {"evil": 0}

        def evil_resolver(args: object, context: object) -> object:
            del args, context
            calls["evil"] += 1
            return object()

        descriptor = resolver_descriptor()
        resolver = resolver_binding(descriptor)
        parameters: dict[str, object] = {"type": "object", "properties": {}}
        contract = ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "name": "get_application",
                    "description": "",
                    "parameters": parameters,
                },
            },
            name="get_application",
            description="",
            parameters=parameters,
        )
        spec = replace(
            synthetic_tool_spec(
                "get_application",
                metadata=read_metadata("get_application", resolver_descriptors=(descriptor,)),
            ),
            contract=contract,
            decoder=lambda value: value,
            executor=lambda args, context: args,
            resolver_bindings=(resolver,),
        )
        lease, (spec_handle,) = _bind_specs(factory, authority, spec)
        factory.register_tool_spec(
            spec_handle,
            catalog_lease=lease,
            authority=authority,
            prepare_identity=prepare_identity,
        )

        if mutation == "resolver_descriptor":
            forged_descriptor = resolver_descriptor("application_event_parent")
            object.__setattr__(
                spec,
                "resolver_bindings",
                (resolver_binding(forged_descriptor),),
            )
        else:
            object.__setattr__(resolver, "resolve", evil_resolver)

        contract_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    materialize_provider_payloads((spec.contract,))[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        with pytest.raises(AuthorityPhaseError):
            factory.prepare_tool_call(
                authority,
                catalog_lease=lease,
                spec_handle=spec_handle,
                prepare_identity=prepare_identity,
                tool_call_id="call-binding-integrity",
                arguments={},
                typed_args={},
                arguments_digest=SHA,
                contract_fingerprint=contract_fingerprint,
                binding=BindingAudit(status="unbound", target_count=0),
            )
        assert calls["evil"] == 0
        assert not factory._prepared


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("preflight", lambda _args, _context: None),
        ("mutable_validator", lambda _args, _context: None),
        ("success_renderer", lambda _result: "forged"),
        ("result_metadata_projector", lambda _result: None),
        ("presentation", None),
        ("schema_failure_renderer", lambda _arguments, _detail: "forged"),
        (
            "editable_fields",
            (
                EditableFieldMetadataV1(
                    field="forged",
                    value_type="string",
                    options=None,
                    clearable=False,
                    clear_value=None,
                ),
            ),
        ),
        ("declared_failure_categories", frozenset({"conflict"})),
        (
            "exception_map",
            (ToolExceptionMapping(ValueError, "conflict", "forged_conflict"),),
        ),
    ),
)
def test_prepared_call_rejects_in_flight_tool_spec_semantic_mutation(
    field: str,
    replacement: object,
) -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")

        target: object = prepared.spec
        final_replacement = replacement
        if field == "editable_fields":
            target = prepared.spec.metadata
        elif field == "presentation":
            final_replacement = ToolPresentationBindingV1(
                implementation_id="forged_presentation_v1",
                confirmation_description=_forged_confirmation_description,
                pending_details_projector=_forged_pending_details,
                success_summary_projector=_forged_success_summary,
            )

        object.__setattr__(target, field, final_replacement)

        with pytest.raises(AuthorityPhaseError):
            factory.require_prepared_call(prepared, authority)


def test_closed_factory_rejects_every_public_registration_without_pollution() -> None:
    factory = AuthorityFactory()
    authority = _segment(factory)
    factory.close()
    with pytest.raises(AuthorityPhaseError):
        factory.register_runner_invocation(object(), authority=authority)
    with pytest.raises(AuthorityPhaseError):
        factory.register_tool_execution_context(object(), authority=authority)
    with pytest.raises(AuthorityPhaseError):
        factory.register_frozen_surface(object(), surface_fingerprint=SHA, authority=authority)
    with pytest.raises(AuthorityPhaseError):
        factory.register_gateway_session(object(), authority=authority)
    with pytest.raises(AuthorityPhaseError):
        factory.register_transaction(object())
    assert factory.active_count == 0
    assert factory.registry_size == 0


def test_pending_snapshot_is_checked_before_approval_typed_and_execution_side_effects() -> None:
    with execution_scope() as factory:
        pending = SimpleNamespace(
            operation_id="op-approval-mutation",
            conversation_id=11,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            pending_action_revision=1,
            arguments_digest=SHA,
            effective_args_digest=SHA,
        )
        factory.register_pending(pending)
        before = factory.active_count
        pending.tool_name = "mutated"
        with pytest.raises(AuthorityPhaseError):
            factory.create_approval_authority(
                operation_id="op-approval-mutation",
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
        assert factory.active_count == before

    with execution_scope() as factory:
        authority = _segment(factory)
        invocation, runner, context, *_ = _registered_invocation(factory, authority)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-typed-mutation",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        pending.pending_action_revision = 2
        with pytest.raises(AuthorityPhaseError):
            factory.create_typed_pending_identity(
                authority=authority,
                runner_invocation=runner,
                tool_context=context,
                prepared=prepared,
                pending=pending,
                operation_id="op-typed-mutation",
                pending_action_revision=1,
                tool_call_id=prepared.tool_call_id,
                tool_name=prepared.spec.name,
                arguments_digest=SHA,
            )

    with execution_scope() as factory:
        pending = SimpleNamespace(
            operation_id="op-execution-mutation",
            conversation_id=11,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            pending_action_revision=1,
            arguments_digest=SHA,
            effective_args_digest=SHA,
        )
        factory.register_pending(pending)
        approval = factory.create_approval_authority(
            operation_id="op-execution-mutation",
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
        with Session() as session:
            transaction = session.begin()
            factory.register_transaction(transaction)
            pending.effective_args_digest = SHA_B
            with pytest.raises(AuthorityPhaseError):
                factory.issue_execution_claim(
                    approval,
                    prepared=prepared,
                    pending=pending,
                    operation_id="op-execution-mutation",
                    tool_call_id="update_application_status",
                    tool_name="update_application_status",
                    effective_args_digest=SHA,
                    session=session,
                    transaction=transaction,
                )
            assert factory._transactions[id(transaction)].authority is None


def test_omitted_proof_requires_strict_hmac_fingerprints(
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-hmac",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-hmac",
            tool_name="create_application_event",
            proposal_fingerprint=SHA,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id="pending-hmac",
        )
        with pytest.raises((AuthorityPhaseError, ValueError)):
            factory.register_operation(operation)
        operation.proposal_fingerprint = HMAC
        pending = SimpleNamespace(
            operation_id="op-hmac",
            conversation_id=11,
            tool_call_id="call-hmac",
            tool_name="create_application_event",
            pending_action_revision=1,
            pending_confirmation_claim_id="pending-hmac",
            arguments_digest=SHA,
        )
        transaction = active_transaction
        factory.register_operation(operation)
        factory.register_pending(pending)
        factory.register_transaction(transaction)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=transaction,
        )
        assert proof.proposal_fingerprint == HMAC
        with pytest.raises(ValueError):
            TrustedLedgerOmittedTokenProof(
                operation_id=proof.operation_id,
                conversation_id=proof.conversation_id,
                status="proposed",
                adapter_kind=proof.adapter_kind,
                tool_call_id=proof.tool_call_id,
                tool_name=proof.tool_name,
                proposal_fingerprint=SHA,
                confirmation_token_fingerprint=HMAC_B,
                pending_operation_id=proof.pending_operation_id,
                pending_tool_call_id=proof.pending_tool_call_id,
                pending_tool_name=proof.pending_tool_name,
                pending_confirmation_claim_id=proof.pending_confirmation_claim_id,
                omitted_token_proof_instance_token=proof.omitted_token_proof_instance_token,
            )


def test_pending_ownership_is_single_authority_and_cross_authority_reuse_fails() -> None:
    with execution_scope() as factory:
        first = _segment(factory)
        second = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-second",
            trusted_scope=_scope(),
            capabilities=frozenset({"applications.read", "applications.write"}),
            capability_profile_fingerprint=SHA,
            binding_policy_fingerprint=SHA_B,
        )
        prepared_first = _prepared(factory, first, kind="write")
        prepared_second = _prepared(factory, second, kind="write")
        pending = SimpleNamespace(
            operation_id="op-owner",
            conversation_id=11,
            tool_call_id=prepared_first.tool_call_id,
            tool_name=prepared_first.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="owner-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        factory.issue_pending_claim(
            first,
            prepared=prepared_first,
            pending=pending,
            operation_id="op-owner",
            tool_call_id=prepared_first.tool_call_id,
            tool_name=prepared_first.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="owner-claim",
        )
        with pytest.raises(AuthorityPhaseError):
            factory.issue_pending_claim(
                second,
                prepared=prepared_second,
                pending=pending,
                operation_id="op-owner",
                tool_call_id=prepared_second.tool_call_id,
                tool_name=prepared_second.spec.name,
                arguments_digest=SHA,
                pending_confirmation_claim_id="owner-claim",
            )


def test_claim_and_proof_snapshots_and_opaque_tokens_are_cleaned_after_consume() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-clean",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="clean-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-clean",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="clean-claim",
        )
        token = claim.pending_claim_instance_token
        factory.mark_in_flight(claim)
        factory.consume(claim)
        assert not factory._claim_fields
        with pytest.raises(AuthorityPhaseError):
            factory.register_transaction(token)


def test_pending_claim_revoke_cleans_issued_token_after_token_field_mutation() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-mutated-claim-token",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="mutated-claim-token",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id=pending.operation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            arguments_digest=pending.arguments_digest,
            pending_confirmation_claim_id=pending.pending_confirmation_claim_id,
        )
        issued_token = claim.pending_claim_instance_token
        object.__setattr__(claim, "pending_claim_instance_token", object())

        assert factory.is_active(issued_token)
        factory.revoke(claim)

        assert id(issued_token) not in factory._objects
        assert id(issued_token) not in authority_composition._ACTIVE_OBJECTS


def test_omitted_proof_revoke_cleans_issued_token_after_token_field_mutation(
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-mutated-proof-token",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-mutated-proof-token",
            tool_name="create_application_event",
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id="mutated-proof-token",
        )
        pending = SimpleNamespace(
            operation_id=operation.id,
            conversation_id=operation.conversation_id,
            tool_call_id=operation.tool_call_id,
            tool_name=operation.tool_name,
            pending_action_revision=1,
            pending_confirmation_claim_id=operation.pending_confirmation_claim_id,
            arguments_digest=SHA,
        )
        factory.register_operation(operation)
        factory.register_pending(pending)
        factory.register_transaction(active_transaction)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=active_transaction,
        )
        issued_token = proof.omitted_token_proof_instance_token
        object.__setattr__(proof, "omitted_token_proof_instance_token", object())

        assert factory.is_active(issued_token)
        factory.revoke(proof)

        assert id(issued_token) not in factory._objects
        assert id(issued_token) not in authority_composition._ACTIVE_OBJECTS


def test_public_prepared_construction_seal_cannot_bind_caller_created_tool_spec() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        with pytest.raises(AuthorityPhaseError):
            factory.issue_prepared_construction_identity(authority)


@pytest.mark.parametrize("source", ["pending", "prepared", "authority"])
def test_pending_claim_lifecycle_revalidates_exact_sources(source: str) -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-source",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="source-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-source",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="source-claim",
        )
        if source == "pending":
            pending.tool_name = "mutated"
        elif source == "prepared":
            object.__setattr__(prepared, "arguments_digest", SHA_B)
        else:
            object.__setattr__(authority, "segment_id", "mutated")
        with pytest.raises(AuthorityPhaseError):
            factory.mark_in_flight(claim)


def test_execution_claim_lifecycle_revalidates_pending_and_prepared_sources() -> None:
    with execution_scope() as factory:
        pending = SimpleNamespace(
            operation_id="op-execution-source",
            conversation_id=11,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            pending_action_revision=1,
            effective_args_digest=SHA,
        )
        factory.register_pending(pending)
        authority = factory.create_approval_authority(
            operation_id="op-execution-source",
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
            authority,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            kind="write",
        )
        factory.begin_prepared_execution(
            prepared,
            authority=authority,
            use=AuthorityUse.APPROVED_WRITE_PREPARE,
        )
        with Session() as session:
            transaction = session.begin()
            factory.register_transaction(transaction)
            claim = factory.issue_execution_claim(
                authority,
                prepared=prepared,
                pending=pending,
                operation_id="op-execution-source",
                tool_call_id="update_application_status",
                tool_name="update_application_status",
                effective_args_digest=SHA,
                session=session,
                transaction=transaction,
            )
            pending.effective_args_digest = SHA_B
            with pytest.raises(AuthorityPhaseError):
                factory.mark_in_flight(claim)


@pytest.mark.parametrize("source", ["operation", "pending", "transaction"])
def test_omitted_proof_lifecycle_revalidates_registered_sources(
    source: str,
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-proof-source",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-proof-source",
            tool_name="create_application_event",
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id="proof-source-claim",
        )
        pending = SimpleNamespace(
            operation_id="op-proof-source",
            conversation_id=11,
            tool_call_id="call-proof-source",
            tool_name="create_application_event",
            pending_action_revision=1,
            pending_confirmation_claim_id="proof-source-claim",
            arguments_digest=SHA,
        )
        transaction = active_transaction
        factory.register_operation(operation)
        factory.register_pending(pending)
        factory.register_transaction(transaction)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=transaction,
        )
        if source == "operation":
            operation.status = "consumed"
        elif source == "pending":
            pending.pending_action_revision = 2
        else:
            factory._transactions[id(transaction)].authority = object()  # type: ignore[assignment]
        with pytest.raises(AuthorityPhaseError):
            factory.mark_in_flight(proof)


@pytest.mark.parametrize("case", ["operation_unowned", "pending_unowned", "conflict"])
def test_omitted_proof_sources_must_have_one_consistent_authority(
    case: str,
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        pending = SimpleNamespace(
            operation_id="op-proof-ownership",
            conversation_id=11,
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            pending_action_revision=1,
            pending_confirmation_claim_id="proof-ownership-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        authority_a = factory.create_approval_authority(
            operation_id="op-proof-ownership",
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
        authority_b = _segment(factory)
        operation = SimpleNamespace(
            id="op-proof-ownership",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="update_application_status",
            tool_name="update_application_status",
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id="proof-ownership-claim",
        )
        transaction = active_transaction
        if case == "operation_unowned":
            factory.register_operation(operation)
            factory.register_transaction(transaction, authority=authority_a)
        elif case == "pending_unowned":
            # Use a segment authority for the Operation, without taking the
            # Pending ownership that an Approval authority would establish.
            factory.revoke_authority(authority_a)
            factory.register_pending(pending)
            factory.register_operation(operation, authority=authority_b)
            factory.register_transaction(transaction, authority=authority_b)
        else:
            factory.register_operation(operation, authority=authority_a)
            factory.register_transaction(transaction, authority=authority_b)
        with pytest.raises(AuthorityPhaseError):
            factory.issue_omitted_token_proof(
                operation=operation,
                pending_pointer=pending,
                transaction=transaction,
            )


def test_omitted_proof_all_unowned_sources_are_a_valid_boundary(
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        operation = SimpleNamespace(
            id="op-proof-unowned",
            conversation_id=11,
            status="proposed",
            adapter_kind="typed",
            tool_call_id="call-proof-unowned",
            tool_name="create_application_event",
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id="proof-unowned-claim",
        )
        pending = SimpleNamespace(
            operation_id="op-proof-unowned",
            conversation_id=11,
            tool_call_id="call-proof-unowned",
            tool_name="create_application_event",
            pending_action_revision=1,
            pending_confirmation_claim_id="proof-unowned-claim",
            arguments_digest=SHA,
        )
        transaction = active_transaction
        factory.register_operation(operation)
        factory.register_pending(pending)
        factory.register_transaction(transaction)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=transaction,
        )
        assert factory.proof_state(proof) == "issued"


def test_revoke_uses_identity_cleanup_after_claim_source_mutation() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-revoke-source",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="revoke-source-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-revoke-source",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="revoke-source-claim",
        )
        pending.tool_name = "mutated"
        factory.revoke(claim)
        assert not factory._claims
        assert not factory._claim_fields
        assert not factory._pending[id(pending)].owners


def test_claim_lifecycle_revokes_after_consume_source_mismatch() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-finally-source",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="finally-source-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-finally-source",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="finally-source-claim",
        )
        with pytest.raises(AuthorityPhaseError):
            with factory.claim_lifecycle(claim):
                pending.pending_action_revision = 2
        assert not factory._claims
        assert not factory._claim_fields
        assert not factory._pending[id(pending)].owners


def test_pending_registration_rejects_foreign_opaque_role_tokens() -> None:
    with execution_scope() as first, execution_scope() as second:
        authority = _segment(first)
        prepared = _prepared(first, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-token-role",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="token-role-claim",
            arguments_digest=SHA,
        )
        first.register_pending(pending)
        claim = first.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-token-role",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="token-role-claim",
        )
        role_tokens = (
            authority.authority_instance_token,
            first.prepared_token(prepared),
            claim.pending_claim_instance_token,
        )
        for token in role_tokens:
            before = second.active_count
            with pytest.raises(AuthorityPhaseError):
                second.register_pending(token)
            assert second.active_count == before


def test_claim_lifecycle_cleans_entry_failure_after_source_mutation() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-entry-mutation",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="entry-mutation-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-entry-mutation",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="entry-mutation-claim",
        )
        pending.tool_name = "mutated-before-enter"
        with pytest.raises(AuthorityPhaseError):
            with factory.claim_lifecycle(claim):
                raise AssertionError("the body must not be entered")
        assert not factory._claims
        assert not factory._claim_fields
        assert not factory._pending[id(pending)].owners
        claim_object_ids = {id(claim), id(claim.pending_claim_instance_token)}
        assert not any(
            object_id in claim_object_ids and owner is factory
            for object_id, (_, owner) in authority_composition._ACTIVE_OBJECTS.items()
        )


def test_revoke_authority_uses_exact_identity_after_authority_mutation() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-authority-revoke",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="authority-revoke-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-authority-revoke",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="authority-revoke-claim",
        )
        claim_token = claim.pending_claim_instance_token
        object.__setattr__(claim, "pending_claim_instance_token", object())
        object.__setattr__(authority, "segment_id", "mutated-authority")
        factory.revoke_authority(authority)
        assert not factory._claims
        assert not factory._claim_fields
        assert factory.active_count == 0
        assert not factory.is_active(authority)
        assert not factory.is_active(claim)
        assert id(claim_token) not in factory._objects
        assert id(claim_token) not in authority_composition._ACTIVE_OBJECTS
        assert not any(
            owner is factory for _, owner in authority_composition._ACTIVE_OBJECTS.values()
        )


def test_revoke_authority_cleans_proof_issued_token_after_token_field_mutation(
    active_transaction: SessionTransaction,
) -> None:
    with execution_scope() as factory:
        pending = SimpleNamespace(
            operation_id="op-authority-proof-revoke",
            conversation_id=11,
            tool_call_id="call-authority-proof-revoke",
            tool_name="create_application_event",
            pending_action_revision=1,
            pending_confirmation_claim_id="authority-proof-revoke-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        authority = factory.create_approval_authority(
            operation_id=pending.operation_id,
            conversation_id=pending.conversation_id,
            conversation_scope_revision=0,
            trusted_scope=_scope(),
            pending_identity=pending,
            pending_action_revision=pending.pending_action_revision,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            effective_args_digest=pending.arguments_digest,
            capability_profile_fingerprint=SHA,
            binding_policy_fingerprint=SHA_B,
        )
        operation = SimpleNamespace(
            id=pending.operation_id,
            conversation_id=pending.conversation_id,
            status="proposed",
            adapter_kind="typed",
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            proposal_fingerprint=HMAC,
            confirmation_token_fingerprint=HMAC_B,
            pending_confirmation_claim_id=pending.pending_confirmation_claim_id,
        )
        factory.register_operation(operation, authority=authority)
        factory.register_transaction(active_transaction, authority=authority)
        proof = factory.issue_omitted_token_proof(
            operation=operation,
            pending_pointer=pending,
            transaction=active_transaction,
        )
        proof_token = proof.omitted_token_proof_instance_token
        object.__setattr__(proof, "omitted_token_proof_instance_token", object())

        factory.revoke_authority(authority)

        assert not factory._proofs
        assert not factory._proof_fields
        assert id(proof_token) not in factory._objects
        assert id(proof_token) not in authority_composition._ACTIVE_OBJECTS


@pytest.mark.parametrize("extra", [math.nan, object()])
def test_bundle_route_validation_is_atomic_for_noncanonical_payload(extra: object) -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        spec = synthetic_tool_spec("invalid_spec")
        parameters = materialize_provider_payloads((spec.contract,))[0]["function"]["parameters"]
        object.__setattr__(
            spec.contract,
            "payload",
            {
                "type": "function",
                "function": {
                    "name": "invalid_spec",
                    "description": "invalid_spec description",
                    "parameters": parameters,
                },
                "extra": extra,
            },
        )
        before = factory.active_count
        with pytest.raises((AuthorityPhaseError, ValueError, TypeError)):
            _bind_specs(factory, authority, spec)
        assert factory.active_count == before
        assert not factory._tool_specs
        assert not factory._tool_spec_fields
        assert id(spec) not in factory._objects
        assert not any(
            object_id == id(spec) and owner is factory
            for object_id, (_, owner) in authority_composition._ACTIVE_OBJECTS.items()
        )


def test_duplicate_claim_lifecycle_does_not_revoke_existing_owner() -> None:
    with execution_scope() as factory:
        authority = _segment(factory)
        prepared = _prepared(factory, authority, kind="write")
        pending = SimpleNamespace(
            operation_id="op-duplicate-lifecycle",
            conversation_id=11,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            pending_action_revision=1,
            pending_confirmation_claim_id="duplicate-lifecycle-claim",
            arguments_digest=SHA,
        )
        factory.register_pending(pending)
        claim = factory.issue_pending_claim(
            authority,
            prepared=prepared,
            pending=pending,
            operation_id="op-duplicate-lifecycle",
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=SHA,
            pending_confirmation_claim_id="duplicate-lifecycle-claim",
        )
        with factory.claim_lifecycle(claim):
            with pytest.raises(AuthorityPhaseError):
                with factory.claim_lifecycle(claim):
                    raise AssertionError("duplicate lifecycle must not enter")
            assert factory.claim_state(claim) == "in_flight"
            assert factory.is_active(claim)
        assert factory.claim_state(claim) is None
