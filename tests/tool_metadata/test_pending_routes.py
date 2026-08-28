from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import asdict
import pickle
from typing import Any

import pytest

from offerpilot.ai import confirmation as confirmation_runtime
from offerpilot.ai import write_operations as write_runtime
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.write_operations import pending_action_identity
from offerpilot.ai.tool_runtime.legacy import LegacyRouteSourceV1
from offerpilot.ai.tool_runtime.metadata import (
    CommittedPrimaryOperationIdentityV1,
    OperationRouteIdentityV1,
)
from offerpilot.repositories import chat as chat_runtime


def _api(name: str) -> Any:
    value = next(
        (
            candidate
            for module in (write_runtime, confirmation_runtime, chat_runtime)
            if (candidate := getattr(module, name, None)) is not None
        ),
        None,
    )
    assert value is not None, f"Task 11 API is missing from its frozen production modules: {name}"
    return value


def _identity(
    *,
    conversation_id: int = 41,
    operation_id: str = "pending-operation",
    tool_call_id: str = "pending-call",
    tool_name: str = "create_application",
    revision: int = 1,
    claim_id: str = "pending-claim",
    digest_digit: str = "1",
) -> Any:
    identity_type = _api("PendingRouteIdentityV1")
    return identity_type(
        conversation_id=conversation_id,
        operation_id=operation_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        pending_action_revision=revision,
        pending_confirmation_claim_id=claim_id,
        arguments_digest="sha256:" + digest_digit * 64,
    )


def _assert_transient(value: object) -> None:
    for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
        with pytest.raises((TypeError, ValueError)):
            operation(value)
    assert "0x" not in repr(value)
    assert "pending-operation" not in repr(value)


def _operation_identity(value: Any) -> OperationRouteIdentityV1:
    return OperationRouteIdentityV1(
        operation_id=value.operation_id,
        tool_call_id=value.tool_call_id,
        revision=value.pending_action_revision,
        arguments_digest=value.arguments_digest,
    )


def _production_components() -> Any:
    from tests.tool_metadata.test_production_bundle import _production_components

    return _production_components()


def _pending_port(components: Any) -> Any:
    return components.pending_persistence_route_port


def _initial_legacy_route(components: Any, source: str) -> tuple[Any, Any]:
    owner = components.initial_routes.owner_lease_factory.open()
    issuer = components.initial_routes.initial_issuer_for(LegacyRouteSourceV1(source))
    request_lease = issuer.open_request_lease(owner)
    token = issuer.issue(request_lease)
    return owner, components.initial_routes.initial_route_port.resolve_initial(token)


def _pending_identity_for(
    pending: PendingAction,
    conversation_id: int,
    *,
    clarification: bool = False,
) -> Any:
    digest, revision = pending_action_identity(
        pending.tool_call_id,
        pending.tool_name,
        pending.args,
    )
    if not clarification:
        digest = pending.arguments_digest or digest
        revision = pending.pending_action_revision or revision
    identity_type = _api("PendingRouteIdentityV1")
    return identity_type(
        conversation_id=conversation_id,
        operation_id="" if clarification else pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name="" if clarification else pending.tool_name,
        pending_action_revision=revision,
        pending_confirmation_claim_id=(
            ""
            if clarification
            else str(pending.pending_confirmation_claim_id or pending.operation_id)
        ),
        arguments_digest=digest,
    )


@contextmanager
def issued_typed_pending_route(
    pending: PendingAction,
    conversation_id: int,
    *,
    claim: object,
    components: Any | None = None,
):
    components = _production_components() if components is None else components
    pending_port = _pending_port(components)
    lease = components.bundle.open_segment_lease()
    spec_handle = lease.resolve(pending.tool_name)
    assert spec_handle is not None
    identity = _pending_identity_for(pending, conversation_id)
    operation = components.operation_port.bind_typed_write(
        lease,
        spec_handle,
        _operation_identity(identity),
        claim,
    )
    handle = pending_port.bind_typed_pending(operation, identity, claim)
    try:
        yield handle, identity
    finally:
        pending_port.revoke_pending(handle)
        lease.close()


@contextmanager
def issued_legacy_pending_route(
    pending: PendingAction,
    conversation_id: int,
    *,
    source: str,
    components: Any | None = None,
):
    if components is None:
        components = getattr(pending, "_test_route_components", None)
    components = _production_components() if components is None else components
    pending_port = _pending_port(components)
    owner, initial_route = _initial_legacy_route(components, source)
    identity = _pending_identity_for(pending, conversation_id)
    operation = components.operation_port.bind_legacy(
        initial_route,
        _operation_identity(identity),
    )
    handle = pending_port.bind_legacy_pending(operation, identity)
    try:
        yield handle, identity
    finally:
        pending_port.revoke_pending(handle)
        owner.close()


@contextmanager
def issued_clarification_pending_route(
    pending: PendingAction,
    conversation_id: int,
    *,
    components: Any | None = None,
):
    components = _production_components() if components is None else components
    pending_port = _pending_port(components)
    identity = _pending_identity_for(pending, conversation_id, clarification=True)
    handle = pending_port.bind_clarification_pending(identity)
    try:
        yield handle, identity
    finally:
        pending_port.revoke_pending(handle)


def issued_typed_primary_parent(
    pending: PendingAction,
    conversation_id: int,
    *,
    claim: object,
    components: Any | None = None,
) -> Any:
    components = _production_components() if components is None else components
    pending_port = _pending_port(components)
    lease = components.bundle.open_segment_lease()
    try:
        spec_handle = lease.resolve(pending.tool_name)
        assert spec_handle is not None
        identity = _pending_identity_for(pending, conversation_id)
        operation = components.operation_port.bind_typed_write(
            lease,
            spec_handle,
            _operation_identity(identity),
            claim,
        )
        return pending_port.bind_primary_parent(operation, identity)
    finally:
        lease.close()


def issued_legacy_primary_parent(
    pending: PendingAction,
    conversation_id: int,
    *,
    source: str,
    components: Any | None = None,
) -> Any:
    components = _production_components() if components is None else components
    pending_port = _pending_port(components)
    owner, initial_route = _initial_legacy_route(components, source)
    try:
        identity = _pending_identity_for(pending, conversation_id)
        operation = components.operation_port.bind_legacy(
            initial_route,
            _operation_identity(identity),
        )
        return pending_port.bind_primary_parent(operation, identity)
    finally:
        owner.close()


def test_pending_route_api_is_closed_exact_and_not_caller_constructible() -> None:
    for name in (
        "PendingRouteIdentityV1",
        "TypedPendingRouteHandle",
        "LegacyPendingRouteHandle",
        "ClarificationPendingRouteHandle",
        "ChainedPendingTopologyPolicyV1",
    ):
        value_type = _api(name)
        if name.endswith("Handle"):
            with pytest.raises(TypeError):
                value_type()

    components = _production_components()
    port = _pending_port(components)
    for method_name in (
        "bind_typed_pending",
        "require_typed_pending",
        "bind_legacy_pending",
        "require_legacy_pending",
        "bind_clarification_pending",
        "require_clarification_pending",
        "revoke_pending",
    ):
        assert callable(getattr(port, method_name, None)), (
            f"Task 11 Operation Port is missing {method_name}"
        )
    assert type(port.chained_topology_policy) is _api("ChainedPendingTopologyPolicyV1")


def test_typed_initial_replacement_and_continuation_require_fresh_exact_handles() -> None:
    components = _production_components()
    port = _pending_port(components)
    first_lease = components.bundle.open_segment_lease()
    first_spec = first_lease.resolve("create_application")
    assert first_spec is not None
    first_identity = _identity()
    first_claim = object()
    first_route = components.operation_port.bind_typed_write(
        first_lease,
        first_spec,
        _operation_identity(first_identity),
        first_claim,
    )
    first = port.bind_typed_pending(first_route, first_identity, first_claim)
    _assert_transient(first)
    assert port.require_typed_pending(first, first_identity, first_claim).operation_name == (
        "create_application"
    )

    replacement_identity = _identity(revision=2, claim_id="replacement-claim", digest_digit="2")
    replacement_claim = object()
    replacement_route = components.operation_port.bind_typed_write(
        first_lease,
        first_spec,
        _operation_identity(replacement_identity),
        replacement_claim,
    )
    replacement = port.bind_typed_pending(
        replacement_route,
        replacement_identity,
        replacement_claim,
    )
    assert replacement is not first
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_pending(first, replacement_identity, replacement_claim)

    continuation_lease = components.bundle.open_segment_lease()
    continuation_spec = continuation_lease.resolve("create_application")
    assert continuation_spec is not None
    continuation_identity = _identity(
        revision=3,
        claim_id="continuation-claim",
        digest_digit="3",
    )
    continuation_claim = object()
    continuation_route = components.operation_port.bind_typed_write(
        continuation_lease,
        continuation_spec,
        _operation_identity(continuation_identity),
        continuation_claim,
    )
    continuation = port.bind_typed_pending(
        continuation_route,
        continuation_identity,
        continuation_claim,
    )
    assert continuation is not first
    first_lease.close()
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_pending(first, first_identity, first_claim)
    assert (
        port.require_typed_pending(
            continuation,
            continuation_identity,
            continuation_claim,
        ).operation_name
        == "create_application"
    )
    continuation_lease.close()


def test_legacy_initial_and_proof_routes_are_distinct_exact_pending_origins(
    tmp_path: Any,
) -> None:
    from tests.tool_metadata import test_legacy_confirmation_proof as proof_fixtures
    from tests.tool_metadata.test_production_bundle import (
        _proof_ready_production_components,
    )

    initial_components = _production_components()
    initial_port = _pending_port(initial_components)
    owner, initial_route = _initial_legacy_route(initial_components, "jd_clarification")
    initial_identity = _identity(
        operation_id="legacy-initial-operation",
        tool_call_id="legacy-initial-call",
        tool_name="save_application_jd_version",
        digest_digit="4",
    )
    try:
        initial_operation = initial_components.operation_port.bind_legacy(
            initial_route,
            _operation_identity(initial_identity),
        )
        initial_handle = initial_port.bind_legacy_pending(
            initial_operation,
            initial_identity,
        )
        assert (
            initial_port.require_legacy_pending(
                initial_handle,
                initial_identity,
            ).operation_name
            == "save_application_jd_version"
        )
    finally:
        owner.close()

    proof_components, backend = _proof_ready_production_components(tmp_path)
    proof_port = _pending_port(proof_components)
    confirmation = proof_components.confirmation_routes
    prepared, _ = proof_fixtures._prepare(confirmation, backend)
    proof, issuance_lease, write_session = proof_fixtures._issue(
        confirmation,
        backend,
        prepared,
    )
    proof_route = confirmation.catalog.resolve_server_loaded(proof)
    proof_identity = _identity(
        operation_id=proof_fixtures.OPERATION_ID,
        tool_call_id="legacy-proof-call",
        tool_name="save_application_jd_version",
        digest_digit="5",
    )
    try:
        proof_operation = proof_components.operation_port.bind_legacy(
            proof_route,
            _operation_identity(proof_identity),
        )
        proof_handle = proof_port.bind_legacy_pending(
            proof_operation,
            proof_identity,
        )
        assert proof_handle is not initial_handle
        with pytest.raises((TypeError, ValueError)):
            initial_port.require_legacy_pending(
                proof_handle,
                proof_identity,
            )
        assert (
            proof_port.require_legacy_pending(
                proof_handle,
                proof_identity,
            ).operation_name
            == "save_application_jd_version"
        )
    finally:
        proof_fixtures._close_issuance(issuance_lease, write_session)


def test_trusted_chained_topology_matrix_consumes_exact_parent_and_child_handles() -> None:
    components = _production_components()
    operation_port = components.operation_port
    port = _pending_port(components)
    policy = port.chained_topology_policy
    lease = components.bundle.open_segment_lease()
    spec = lease.resolve("create_application")
    assert spec is not None
    typed_parent_identity = _identity()
    typed_parent_claim = object()
    typed_operation = operation_port.bind_typed_write(
        lease,
        spec,
        _operation_identity(typed_parent_identity),
        typed_parent_claim,
    )
    typed_parent = port.bind_primary_parent(typed_operation, typed_parent_identity)
    typed_child_identity = _identity(
        operation_id="typed-child",
        tool_call_id="typed-child-call",
        revision=2,
        claim_id="typed-child-claim",
        digest_digit="2",
    )
    typed_child_claim = object()
    typed_child_operation = operation_port.bind_typed_write(
        lease,
        spec,
        _operation_identity(typed_child_identity),
        typed_child_claim,
    )
    typed_child = port.bind_typed_pending(
        typed_child_operation,
        typed_child_identity,
        typed_child_claim,
    )
    committed_parent = CommittedPrimaryOperationIdentityV1(
        operation_id="committed-parent",
        primary_tool="update_application_status",
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + "9" * 64,
    )
    compensation_binding = components.bundle.compensation_view().ordered_handler_bindings[0]
    compensation_handler = components.compensation_registry.bind_handler(compensation_binding)
    compensation_operation = operation_port.bind_compensation(
        committed_parent,
        compensation_handler,
    )
    compensation_parent = port.bind_compensation_parent(
        compensation_operation,
        committed_parent,
        compensation_handler,
    )

    jd_owner, jd_route = _initial_legacy_route(components, "jd_clarification")
    snapshot_owner, snapshot_route = _initial_legacy_route(
        components,
        "submission_snapshot_action",
    )
    outcome_owner, outcome_route = _initial_legacy_route(components, "outcome_recording_action")
    jd_identity = _identity(
        operation_id="legacy-parent",
        tool_call_id="legacy-parent-call",
        tool_name="save_application_jd_version",
        digest_digit="6",
    )
    jd_child_identity = _identity(
        operation_id="legacy-child",
        tool_call_id="legacy-child-call",
        tool_name="save_application_jd_version",
        revision=2,
        claim_id="legacy-child-claim",
        digest_digit="8",
    )
    outcome_identity = _identity(
        operation_id="legacy-outcome",
        tool_call_id="legacy-outcome-call",
        tool_name="record_application_outcome",
        digest_digit="7",
    )
    snapshot_parent_identity = _identity(
        operation_id="legacy-snapshot-parent",
        tool_call_id="legacy-snapshot-parent-call",
        tool_name="create_application_submission_snapshot",
        digest_digit="3",
    )
    snapshot_child_identity = _identity(
        operation_id="legacy-snapshot-child",
        tool_call_id="legacy-snapshot-child-call",
        tool_name="create_application_submission_snapshot",
        revision=2,
        claim_id="legacy-snapshot-child-claim",
        digest_digit="4",
    )
    outcome_parent_identity = _identity(
        operation_id="legacy-outcome-parent",
        tool_call_id="legacy-outcome-parent-call",
        tool_name="record_application_outcome",
        digest_digit="5",
    )
    outcome_child_identity = _identity(
        operation_id="legacy-outcome-child",
        tool_call_id="legacy-outcome-child-call",
        tool_name="record_application_outcome",
        revision=2,
        claim_id="legacy-outcome-child-claim",
        digest_digit="7",
    )
    try:
        jd_operation = operation_port.bind_legacy(jd_route, _operation_identity(jd_identity))
        jd_parent = port.bind_primary_parent(jd_operation, jd_identity)
        jd_child_operation = operation_port.bind_legacy(
            jd_route,
            _operation_identity(jd_child_identity),
        )
        jd_child = port.bind_legacy_pending(jd_child_operation, jd_child_identity)
        outcome_operation = operation_port.bind_legacy(
            outcome_route,
            _operation_identity(outcome_identity),
        )
        outcome_child = port.bind_legacy_pending(outcome_operation, outcome_identity)
        snapshot_parent_operation = operation_port.bind_legacy(
            snapshot_route,
            _operation_identity(snapshot_parent_identity),
        )
        snapshot_parent = port.bind_primary_parent(
            snapshot_parent_operation,
            snapshot_parent_identity,
        )
        snapshot_child_operation = operation_port.bind_legacy(
            snapshot_route,
            _operation_identity(snapshot_child_identity),
        )
        snapshot_child = port.bind_legacy_pending(
            snapshot_child_operation,
            snapshot_child_identity,
        )
        outcome_parent_operation = operation_port.bind_legacy(
            outcome_route,
            _operation_identity(outcome_parent_identity),
        )
        outcome_parent = port.bind_primary_parent(
            outcome_parent_operation,
            outcome_parent_identity,
        )
        outcome_forbidden_child_operation = operation_port.bind_legacy(
            outcome_route,
            _operation_identity(outcome_child_identity),
        )
        outcome_forbidden_child = port.bind_legacy_pending(
            outcome_forbidden_child_operation,
            outcome_child_identity,
        )

        assert policy.require_transition(typed_parent, typed_child) is None
        assert policy.require_transition(jd_parent, jd_child) is None
        for forbidden_parent, forbidden_child in (
            (snapshot_parent, snapshot_child),
            (outcome_parent, outcome_forbidden_child),
        ):
            with pytest.raises((TypeError, ValueError)):
                policy.require_transition(forbidden_parent, forbidden_child)
        for parent, child in (
            (typed_parent, jd_child),
            (jd_parent, typed_child),
            (jd_parent, outcome_child),
            (compensation_parent, typed_child),
            (compensation_parent, jd_child),
        ):
            with pytest.raises((TypeError, ValueError)):
                policy.require_transition(parent, child)
    finally:
        jd_owner.close()
        snapshot_owner.close()
        outcome_owner.close()
        lease.close()


def test_clarification_handle_is_the_only_operationless_pending_route() -> None:
    components = _production_components()
    port = _pending_port(components)
    identity = _identity(
        operation_id="",
        tool_call_id="clarification-call",
        tool_name="",
        claim_id="",
        digest_digit="8",
    )
    handle = port.bind_clarification_pending(identity)
    _assert_transient(handle)
    assert port.require_clarification_pending(handle, identity) is None

    operation_identity = _identity()
    with pytest.raises((TypeError, ValueError)):
        port.bind_clarification_pending(operation_identity)
