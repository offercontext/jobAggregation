from __future__ import annotations

import copy
import hashlib
import json
import pickle
from dataclasses import asdict, fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from offerpilot.ai.tool_authority import (
    AuthorityCallIdentity,
    AuthorityFactory,
    AuthorityPhaseError,
    AuthorityUse,
    ExecutionClaim,
    PendingAuthorityClaim,
    SegmentExecutionAuthority,
    TrustedContextScope,
    TrustedLedgerOmittedTokenProof,
    execution_scope,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolSpec,
    TransientToolRuntimeValue,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    read_metadata,
    synthetic_tool_spec,
    write_metadata,
)


ROOT = Path(__file__).resolve().parents[2]
ARG_DIGEST = "sha256:" + hashlib.sha256(b"{}").hexdigest()
SURFACE_FINGERPRINT = "sha256:" + "c" * 64
PROPOSAL_HMAC = "hmac-sha256:" + "d" * 64
TOKEN_HMAC = "hmac-sha256:" + "e" * 64
_LEASES: list[object] = []
_ROUTES: dict[int, tuple[object, object, dict[str, ToolSpec[Any, Any]]]] = {}


@pytest.fixture(autouse=True)
def _close_segment_leases():
    yield
    _ROUTES.clear()
    while _LEASES:
        getattr(_LEASES.pop(), "close")()


_PRIVATE_FIELD_NAMES = {
    "authority_instance_token",
    "approval_authority_instance_token",
    "pending_identity",
    "pending_claim_instance_token",
    "execution_claim_instance_token",
    "omitted_token_proof_instance_token",
    "prepared_instance_token",
    "session",
    "transaction",
}
_PUBLIC_CONVERSATION_FIELDS = {
    "id",
    "title",
    "title_source",
    "mode",
    "context_type",
    "context_ref",
    "pinned_at",
    "archived_at",
    "pending_action",
    "pending_clarification",
    "last_write_undo",
    "created_at",
    "updated_at",
}


def _segment(factory: AuthorityFactory, *, segment_id: str) -> SegmentExecutionAuthority:
    return factory.create_segment_authority(
        conversation_id=8_188_811,
        conversation_scope_revision=73,
        segment_id=segment_id,
        trusted_scope=TrustedContextScope(
            context_type="application",
            context_ref=8_177_331,
            mode="privacy-mode-canary-34af",
        ),
        capability_profile_id="privacy-profile-canary-770c",
        capabilities=frozenset({"applications.read", "offers.read"}),
        capability_policy_version="privacy-capability-policy-canary",
        binding_policy_version="privacy-binding-policy-canary",
        capability_profile_fingerprint="sha256:" + "a" * 64,
        binding_policy_fingerprint="sha256:" + "b" * 64,
    )


def _transient_contract_types() -> tuple[type[TransientToolRuntimeValue], ...]:
    from offerpilot.ai.tool_authority import contracts
    from offerpilot.ai.tool_runtime.contracts import PreparedToolCall

    names = (
        "TrustedContextScope",
        "SegmentExecutionAuthority",
        "ApprovalExecutionAuthority",
        "ProviderSurfaceBuildIdentity",
        "ProviderInvocationIdentity",
        "NewTurnPrepareCallIdentity",
        "ReadExecutionCallIdentity",
        "TypedPendingCallIdentity",
        "ApprovedWritePrepareCallIdentity",
        "ApprovedWriteExecuteCallIdentity",
        "ApplicationScopeConstraint",
        "BindingTargetResolution",
        "PendingAuthorityClaim",
        "ExecutionClaim",
        "TrustedLedgerOmittedTokenProof",
    )
    return tuple(getattr(contracts, name) for name in names) + (PreparedToolCall,)


def _security_runtime_types() -> tuple[type[TransientToolRuntimeValue], ...]:
    from offerpilot.ai.agent_loop import SegmentSurfaceGate
    from offerpilot.ai.tool_authority import contracts
    from offerpilot.ai.tool_runtime.context import ToolExecutionContext
    from offerpilot.context_projector.binding import BoundProviderResponse

    authority_types = tuple(
        value
        for value in vars(contracts).values()
        if isinstance(value, type)
        and value is not TransientToolRuntimeValue
        and issubclass(value, TransientToolRuntimeValue)
        and value.__module__ == contracts.__name__
    )
    return tuple(
        dict.fromkeys(
            (
                *authority_types,
                PreparedToolCall,
                ToolExecutionContext,
                SegmentSurfaceGate,
                BoundProviderResponse,
            )
        )
    )


def _contract_fingerprint(spec: ToolSpec[Any, Any]) -> str:
    raw = json.dumps(
        materialize_provider_payloads((spec.contract,))[0],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _registered_invocation(
    factory: AuthorityFactory,
    authority: SegmentExecutionAuthority,
) -> tuple[object, object, object, object]:
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
        model_call_id="privacy-model-call",
    )
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=SURFACE_FINGERPRINT,
        authority=authority,
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=SURFACE_FINGERPRINT,
        authority=authority,
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build,
        surface=surface,
        surface_fingerprint=SURFACE_FINGERPRINT,
        model_call_surface_binding=binding,
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=SURFACE_FINGERPRINT,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return runner, context, build, invocation


def _prepared(
    factory: AuthorityFactory,
    authority: object,
    *,
    tool_call_id: str,
    tool_name: str,
    kind: str,
    invocation: object | None = None,
) -> tuple[PreparedToolCall[Any, Any], object]:
    route = _ROUTES.get(id(authority))
    if route is None:
        specs: list[ToolSpec[Any, Any]] = []
        for candidate_name, candidate_kind in (
            ("get_application", "read"),
            ("create_application", "write"),
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
        bundle = ToolMetadataBundleV1(
            typed_catalog=catalog,
            manifest={**source["manifest"], "typed_tools": tuple(spec.name for spec in specs)},
            legacy_boundary=source["legacy_boundary"],
            compensation=source["compensation"],
        )
        lease = bundle.open_segment_lease()
        _LEASES.append(lease)
        factory.bind_segment_tool_catalog(
            authority,  # type: ignore[arg-type]
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=lease,
        )
        route = (bundle, lease, {spec.name: spec for spec in specs})
        _ROUTES[id(authority)] = route
    _, lease, specs_by_name = route
    spec = specs_by_name[tool_name]
    if type(authority).__name__ == "ApprovalExecutionAuthority":
        prepare_identity = factory.create_approved_write_prepare_identity(
            authority,  # type: ignore[arg-type]
            approval_context=object(),
            request_identity=object(),
        )
    else:
        assert invocation is not None
        attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)  # type: ignore[arg-type]
        prepare_identity = factory.create_new_turn_prepare_identity(
            invocation,  # type: ignore[arg-type]
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_digest=ARG_DIGEST,
        )
    spec_handle = lease.resolve(tool_name)
    assert spec_handle is not None
    factory.register_tool_spec(
        spec_handle,
        catalog_lease=lease,
        authority=authority,  # type: ignore[arg-type]
        prepare_identity=prepare_identity,  # type: ignore[arg-type]
    )
    prepared = factory.prepare_tool_call(
        authority,  # type: ignore[arg-type]
        catalog_lease=lease,
        spec_handle=spec_handle,
        prepare_identity=prepare_identity,  # type: ignore[arg-type]
        tool_call_id=tool_call_id,
        arguments={},
        typed_args={},
        arguments_digest=ARG_DIGEST,
        contract_fingerprint=_contract_fingerprint(spec),
        binding=BindingAudit(status="unbound", target_count=0),
    )
    return prepared, prepare_identity


def _live_security_graph(
    factory: AuthorityFactory,
    execution_session: Session,
    proof_session: Session,
) -> tuple[
    tuple[object, ...], PendingAuthorityClaim, ExecutionClaim, TrustedLedgerOmittedTokenProof
]:
    segment = _segment(factory, segment_id="privacy-live-segment")
    constraint = factory.create_application_scope_constraint(segment)
    resolution = factory.create_binding_target_resolution(
        segment,
        entity_kind="application",
        state="resolved",
        identity=8_177_331,
    )
    runner, context, build_identity, invocation = _registered_invocation(factory, segment)
    read_prepared, read_prepare_identity = _prepared(
        factory,
        segment,
        tool_call_id="privacy-read-call",
        tool_name="get_application",
        kind="read",
        invocation=invocation,
    )
    read_identity = factory.create_read_execution_identity(
        invocation,  # type: ignore[arg-type]
        prepared=read_prepared,
        tool_call_id=read_prepared.tool_call_id,
        tool_name=read_prepared.spec.name,
        arguments_digest=read_prepared.arguments_digest,
    )
    write_prepared, write_prepare_identity = _prepared(
        factory,
        segment,
        tool_call_id="privacy-pending-call",
        tool_name="create_application",
        kind="write",
        invocation=invocation,
    )
    pending = SimpleNamespace(
        operation_id="privacy-pending-operation",
        conversation_id=segment.conversation_id,
        tool_call_id=write_prepared.tool_call_id,
        tool_name=write_prepared.spec.name,
        pending_action_revision=1,
        pending_confirmation_claim_id="privacy-pending-claim-id",
        arguments_digest=ARG_DIGEST,
    )
    factory.register_pending(pending)
    typed_pending_identity = factory.create_typed_pending_identity(
        authority=segment,
        runner_invocation=runner,
        tool_context=context,
        prepared=write_prepared,
        pending=pending,
        operation_id=pending.operation_id,
        pending_action_revision=1,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        arguments_digest=ARG_DIGEST,
    )
    pending_claim = factory.issue_pending_claim(
        segment,
        prepared=write_prepared,
        pending=pending,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        arguments_digest=ARG_DIGEST,
    )

    approval_pending = SimpleNamespace(
        operation_id="privacy-approved-operation",
        conversation_id=segment.conversation_id,
        tool_call_id="privacy-approved-call",
        tool_name="update_application_status",
        pending_action_revision=1,
        pending_confirmation_claim_id="privacy-approved-claim-id",
        effective_args_digest=ARG_DIGEST,
    )
    factory.register_pending(approval_pending)
    approval = factory.create_approval_authority(
        operation_id=approval_pending.operation_id,
        conversation_id=segment.conversation_id,
        conversation_scope_revision=segment.conversation_scope_revision,
        trusted_scope=segment.trusted_scope,
        pending_identity=approval_pending,
        pending_action_revision=1,
        tool_call_id=approval_pending.tool_call_id,
        tool_name=approval_pending.tool_name,
        effective_args_digest=ARG_DIGEST,
        capabilities=frozenset({"applications.write"}),
        capability_profile_fingerprint=segment.capability_profile_fingerprint,
        binding_policy_fingerprint=segment.binding_policy_fingerprint,
    )
    approved_prepared, approved_prepare_identity = _prepared(
        factory,
        approval,
        tool_call_id=approval_pending.tool_call_id,
        tool_name=approval_pending.tool_name,
        kind="write",
    )
    factory.begin_prepared_execution(
        approved_prepared,
        authority=approval,
        use=AuthorityUse.APPROVED_WRITE_PREPARE,
    )
    execution_transaction = execution_session.begin()
    factory.register_transaction(execution_transaction)
    execution_claim = factory.issue_execution_claim(
        approval,
        prepared=approved_prepared,
        pending=approval_pending,
        operation_id=approval_pending.operation_id,
        tool_call_id=approval_pending.tool_call_id,
        tool_name=approval_pending.tool_name,
        effective_args_digest=ARG_DIGEST,
        session=execution_session,
        transaction=execution_transaction,
    )
    execute_identity = factory.create_approved_write_execute_identity(
        approved_prepare_identity,  # type: ignore[arg-type]
        prepared=approved_prepared,
        execution_claim=execution_claim,
    )

    proof_operation = SimpleNamespace(
        id="privacy-proof-operation",
        conversation_id=segment.conversation_id,
        status="proposed",
        adapter_kind="typed",
        tool_call_id="privacy-proof-call",
        tool_name="create_application_event",
        proposal_fingerprint=PROPOSAL_HMAC,
        confirmation_token_fingerprint=TOKEN_HMAC,
        pending_confirmation_claim_id="privacy-proof-claim-id",
    )
    proof_pending = SimpleNamespace(
        operation_id=proof_operation.id,
        conversation_id=segment.conversation_id,
        tool_call_id=proof_operation.tool_call_id,
        tool_name=proof_operation.tool_name,
        pending_action_revision=1,
        pending_confirmation_claim_id=proof_operation.pending_confirmation_claim_id,
        arguments_digest=ARG_DIGEST,
    )
    proof_transaction = proof_session.begin()
    factory.register_operation(proof_operation)
    factory.register_pending(proof_pending)
    factory.register_transaction(proof_transaction)
    proof = factory.issue_omitted_token_proof(
        operation=proof_operation,
        pending_pointer=proof_pending,
        transaction=proof_transaction,
    )
    values = (
        segment.trusted_scope,
        segment,
        approval,
        constraint,
        resolution,
        build_identity,
        invocation,
        read_prepare_identity,
        read_identity,
        write_prepare_identity,
        typed_pending_identity,
        approved_prepare_identity,
        execute_identity,
        read_prepared,
        write_prepared,
        approved_prepared,
        pending_claim,
        execution_claim,
        proof,
    )
    return values, pending_claim, execution_claim, proof


def _checkpoint_payload(value: object) -> str:
    return json.dumps(
        {"checkpoint": value},
        default=lambda item: item.to_json(),  # type: ignore[union-attr]
    )


def test_all_security_contracts_inherit_the_nonserializable_runtime_boundary() -> None:
    expected_live_types = set(_transient_contract_types())
    security_types = set(_security_runtime_types())
    assert expected_live_types <= security_types
    for contract_type in security_types:
        assert issubclass(contract_type, TransientToolRuntimeValue), contract_type.__name__
        value = object.__new__(contract_type)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(value)
        with pytest.raises(TypeError):
            value.to_json()


def test_every_live_security_value_rejects_all_generic_serialization_paths() -> None:
    with (
        execution_scope() as factory,
        Session() as execution_session,
        Session() as proof_session,
    ):
        values, _, _, _ = _live_security_graph(
            factory,
            execution_session,
            proof_session,
        )
        expected_types = {item.__name__ for item in _transient_contract_types()}
        assert expected_types.issubset({type(value).__name__ for value in values})
        for value in values:
            for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                with pytest.raises(TypeError):
                    operation(value)
            for operation in (
                asdict,
                replace,
                lambda item: json.dumps({"value": item}),
                _checkpoint_payload,
            ):
                with pytest.raises(TypeError):
                    operation(value)  # type: ignore[operator]
            with pytest.raises(TypeError):
                value.to_json()  # type: ignore[union-attr]


def test_live_claim_proof_and_identity_registries_clear_on_normal_completion() -> None:
    factory = AuthorityFactory()
    with factory, Session() as execution_session, Session() as proof_session:
        _, pending_claim, execution_claim, proof = _live_security_graph(
            factory,
            execution_session,
            proof_session,
        )
        for value in (pending_claim, execution_claim, proof):
            factory.mark_in_flight(value)
            factory.consume(value)
        assert factory.claim_state(pending_claim) is None
        assert factory.claim_state(execution_claim) is None
        assert factory.proof_state(proof) is None
    assert factory.active_count == 0


@pytest.mark.parametrize(
    "termination",
    ["exception", "rollback", "cancellation"],
)
def test_live_claim_proof_and_identity_registries_clear_on_abnormal_termination(
    termination: str,
) -> None:
    factory = AuthorityFactory()
    failure: BaseException = (
        KeyboardInterrupt("privacy-cancel")
        if termination == "cancellation"
        else RuntimeError("privacy-rollback")
    )
    with pytest.raises(type(failure)):
        with factory, Session() as execution_session, Session() as proof_session:
            _, pending_claim, execution_claim, proof = _live_security_graph(
                factory,
                execution_session,
                proof_session,
            )
            if termination == "rollback":
                execution_session.rollback()
                proof_session.rollback()
                for value in (pending_claim, execution_claim, proof):
                    factory.revoke(value)
                assert factory.claim_state(pending_claim) is None
                assert factory.claim_state(execution_claim) is None
                assert factory.proof_state(proof) is None
            raise failure
    assert factory.active_count == 0


def test_authority_canaries_are_type_only_and_rejected_by_generic_payloads() -> None:
    with execution_scope() as factory:
        authority = _segment(factory, segment_id="privacy-segment-canary-831f")
        constraint = factory.create_application_scope_constraint(authority)
        resolution = factory.create_binding_target_resolution(
            authority,
            entity_kind="application",
            state="resolved",
            identity=8_177_331,
        )
        values = (authority, constraint, resolution)
        private_canaries = (
            "privacy-segment-canary-831f",
            "privacy-mode-canary-34af",
            "privacy-profile-canary-770c",
            "applications.read",
            "8177331",
            "sha256:" + "a" * 64,
        )
        for value in values:
            rendered = repr(value)
            assert rendered == f"<{type(value).__name__}>"
            assert all(canary not in rendered for canary in private_canaries)
            with pytest.raises(TypeError):
                json.dumps({"runtime": value})
            with pytest.raises(TypeError):
                asdict(value)
            with pytest.raises(TypeError):
                replace(value)


def test_security_contract_private_fields_are_never_repr_enabled() -> None:
    for contract_type in _transient_contract_types():
        for contract_field in fields(contract_type):
            if contract_field.name in _PRIVATE_FIELD_NAMES:
                assert contract_field.repr is False, (
                    contract_type.__name__,
                    contract_field.name,
                )


@pytest.mark.parametrize("failure", [Exception("rollback"), KeyboardInterrupt("cancel")])
def test_execution_scope_clears_bounded_registries_on_failure_and_cancellation(
    failure: BaseException,
) -> None:
    factory = AuthorityFactory()
    with pytest.raises(type(failure)):
        with factory:
            authority = _segment(factory, segment_id="cleanup-segment")
            factory.create_application_scope_constraint(authority)
            factory.create_binding_target_resolution(
                authority,
                entity_kind="application",
                state="resolved",
                identity=8_177_331,
            )
            assert factory.active_count > 0
            raise failure
    assert factory.active_count == 0


def test_scope_exit_revokes_authority_and_call_identity_registry_entries() -> None:
    with execution_scope() as factory:
        authority = _segment(factory, segment_id="cleanup-normal")
        runner = object()
        context = object()
        factory.register_runner_invocation(runner, authority=authority)
        factory.register_tool_execution_context(context, authority=authority)
        call_identity: AuthorityCallIdentity = factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=context,
            model_call_id="model-cleanup",
        )
        assert factory.active_count > 0
    assert factory.active_count == 0
    with pytest.raises(AuthorityPhaseError):
        factory.require_active(authority)
    with pytest.raises(AuthorityPhaseError):
        factory.require_active(call_identity)


def test_public_conversation_http_and_sse_contracts_exclude_private_revision_and_hmac() -> None:
    from offerpilot.schemas import ConversationOut

    assert set(ConversationOut.model_fields) == _PUBLIC_CONVERSATION_FIELDS
    assert "scope_revision" not in ConversationOut.model_fields
    assert "authorization_scope_fingerprint" not in ConversationOut.model_fields

    api_source = (ROOT / "src" / "offerpilot" / "api.py").read_text(encoding="utf-8")
    conversation_projection = api_source[
        api_source.index("def _conversation_json(") : api_source.index(
            "def _conversation_context_label(", api_source.index("def _conversation_json(")
        )
    ]
    assert "scope_revision" not in conversation_projection
    assert "authorization_scope_fingerprint" not in conversation_projection

    transport_source = (ROOT / "src" / "offerpilot" / "chat_transport.py").read_text(
        encoding="utf-8"
    )
    envelope = transport_source[
        transport_source.index("def runtime_sse_envelope(") : transport_source.index(
            "def prepared_stream_metadata(",
            transport_source.index("def runtime_sse_envelope("),
        )
    ]
    assert "scope_revision" not in envelope
    assert "authorization_scope_fingerprint" not in envelope

    # Projector sources are the complete Agent Prompt construction boundary;
    # events/trace are the closed Journal and diagnostic reconstruction
    # boundary.  These private persistence names must not become a supported
    # field in any of them.
    boundary_files = (
        ROOT / "src" / "offerpilot" / "agent_runtime" / "events.py",
        ROOT / "src" / "offerpilot" / "agent_runtime" / "trace.py",
        ROOT / "src" / "offerpilot" / "context_projector" / "contracts.py",
        ROOT / "src" / "offerpilot" / "context_projector" / "projector.py",
        ROOT / "src" / "offerpilot" / "pilot_runtime" / "event_sink.py",
    )
    for path in boundary_files:
        source = path.read_text(encoding="utf-8")
        assert "scope_revision" not in source, path
        assert "authorization_scope_fingerprint" not in source, path


def test_claim_contract_does_not_gain_raw_request_or_provider_payload_fields() -> None:
    claim_fields = {item.name for item in fields(PendingAuthorityClaim)}
    assert not claim_fields.intersection(
        {
            "args",
            "arguments",
            "request",
            "request_payload",
            "provider_answer",
            "provider_page_context",
            "repository",
            "exception",
            "traceback",
            "confirmation_token",
            "authorization_scope_fingerprint",
        }
    )
    assert {
        "arguments_digest",
        "pending_identity",
        "pending_claim_instance_token",
    }.issubset(claim_fields)


def test_live_authority_graph_is_rejected_by_real_public_boundaries() -> None:
    from offerpilot.agent_runtime.events import (
        JournalEventValidationError,
        prepare_event,
    )
    from offerpilot.ai.types import Message
    from offerpilot.context_projector.budget import canonical_messages
    from offerpilot.context_projector.contracts import FrozenMessage, ProjectionError
    from offerpilot.pilot_runtime import MessageOutcome
    from offerpilot.pilot_runtime.event_sink import runtime_event_payload

    with (
        execution_scope() as factory,
        Session() as execution_session,
        Session() as proof_session,
    ):
        values, _, _, _ = _live_security_graph(
            factory,
            execution_session,
            proof_session,
        )
        authority = values[1]
        canaries = {
            "privacy-live-segment",
            "privacy-mode-canary-34af",
            "privacy-profile-canary-770c",
            "applications.read",
            "offers.read",
            str(8_177_331),
            PROPOSAL_HMAC,
            TOKEN_HMAC,
            "privacy-proof-claim-id",
        }
        # Prompt/model-surface canonicalization is a real projector boundary.
        frozen = FrozenMessage.freeze(
            Message(role="system", content=authority),  # type: ignore[arg-type]
        )
        with pytest.raises(ProjectionError):
            canonical_messages((frozen,))

        # Journal rejection also closes Trace because Trace is reconstructed
        # exclusively from accepted Journal events.
        with pytest.raises((JournalEventValidationError, TypeError)):
            prepare_event(
                event_type="tool.proposed",
                execution_segment_id="privacy-boundary-segment",
                facts={
                    "tool_call_id": "privacy-boundary-call",
                    "tool_name": authority,
                    "tool_kind": "read",
                    "args_shape_digest": ARG_DIGEST,
                    "proposal_outcome": "execution_allowed",
                },
            )

        # Typed HTTP/SSE projection refuses the transient value rather than
        # relying on json.dumps(default=str) to hide it later.
        with pytest.raises(TypeError):
            MessageOutcome(message=authority)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            runtime_event_payload(authority)  # type: ignore[arg-type]

        # Logs receive type-only reprs; generic/checkpoint payloads reject the
        # object before a private field or opaque proof token can be rendered.
        for value in values:
            rendered = repr(value)
            assert rendered == f"<{type(value).__name__}>"
            assert all(canary not in rendered for canary in canaries)
            with pytest.raises(TypeError):
                _checkpoint_payload(value)

        manifest_blob = (
            ROOT / "tests" / "fixtures" / "tool_authority" / "authority_manifest_v1.json"
        ).read_text(encoding="utf-8")
        # Static per-tool required capability names are intentionally public
        # manifest metadata; the request-scoped capability *set* and all
        # identity/scope/claim/HMAC values remain private.
        manifest_private_canaries = canaries - {"applications.read", "offers.read"}
        assert all(canary not in manifest_blob for canary in manifest_private_canaries)
