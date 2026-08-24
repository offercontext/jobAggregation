"""Composition-root factories and bounded identity registries.

The registry in this module is intentionally execution-scoped.  It keeps strong
references while an execution is live so an opaque identity cannot disappear
underneath a running call, then drops every reference when the scope exits.
There is no durable or process-wide completed-history store.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
import hashlib
import json
from threading import RLock
from typing import Any, Iterator, Literal, Mapping, cast
from uuid import uuid4

from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    BindingContract,
    BindingResolverSpec,
    PreparedToolCall,
    ProviderToolContract,
    ToolSpec,
)

from .contracts import (
    ApprovedWriteExecuteCallIdentity,
    ApprovedWritePrepareCallIdentity,
    ApplicationScopeConstraint,
    ApprovalExecutionAuthority,
    AuthorityCallIdentity,
    AuthorityInstanceToken,
    AuthorityPhaseError,
    AuthorityUse,
    BindingTargetResolution,
    ExecutionClaim,
    ExecutionClaimInstanceToken,
    NewTurnPrepareCallIdentity,
    OmittedTokenProofInstanceToken,
    PendingAuthorityClaim,
    PendingClaimInstanceToken,
    PendingInstanceToken,
    PreparedConstructionIdentity,
    PreparedInstanceToken,
    ProviderInvocationIdentity,
    ProviderSurfaceBuildIdentity,
    ReadExecutionCallIdentity,
    SegmentExecutionAuthority,
    ToolExecutionAuthority,
    TrustedContextScope,
    TrustedLedgerOmittedTokenProof,
    TypedPendingCallIdentity,
    _new_opaque_handle,
    _OpaqueHandle,
    _require_digest,
    _require_hmac_digest,
    _require_text,
    constant_time_equal,
    require_nonnegative_int64,
    require_positive_int64,
)


_ACTIVE_AUTHORITIES: dict[int, tuple[ToolExecutionAuthority, "AuthorityFactory"]] = {}
_ACTIVE_AUTHORITIES_LOCK = RLock()
_ACTIVE_OBJECTS: dict[int, tuple[object, "AuthorityFactory"]] = {}
_ACTIVE_REPOSITORY_BINDINGS: dict[int, tuple[object, "AuthorityFactory"]] = {}


def _authority_token(authority: ToolExecutionAuthority) -> AuthorityInstanceToken:
    if isinstance(authority, SegmentExecutionAuthority):
        return authority.authority_instance_token
    if isinstance(authority, ApprovalExecutionAuthority):
        return authority.approval_authority_instance_token
    raise AuthorityPhaseError("unknown authority type")


def _opaque_identity(value: object, field_name: str) -> object:
    if value is None or isinstance(value, (str, bytes, int, float, bool, tuple, frozenset)):
        raise AuthorityPhaseError(f"{field_name} must be a caller-owned opaque object")
    return value


def _dataclass_snapshot(value: object) -> dict[str, object]:
    return {
        field.name: getattr(value, field.name)
        for field in dataclass_fields(cast(Any, value))
    }


def _canonical_arguments_digest(arguments: object) -> str:
    if not isinstance(arguments, Mapping):
        raise AuthorityPhaseError("Prepared arguments must be a mapping")
    if any(type(key) is not str for key in arguments):
        raise AuthorityPhaseError("Prepared argument keys must be strings")
    try:
        payload = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityPhaseError("Prepared arguments are not canonical JSON") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_contract_fingerprint(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise AuthorityPhaseError("ToolSpec contract payload must be a mapping")
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityPhaseError("ToolSpec contract payload is not canonical JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_snapshot(value: object, snapshot: Mapping[str, object], label: str) -> None:
    for name, expected in snapshot.items():
        current = getattr(value, name, None)
        if name.endswith("digest") or name.endswith("fingerprint"):
            if not isinstance(current, str) or not isinstance(expected, str):
                raise AuthorityPhaseError(f"{label} semantic fields changed")
            if not constant_time_equal(current, expected):
                raise AuthorityPhaseError(f"{label} semantic fields changed")
        elif type(expected) in {str, int, bool, type(None)}:
            if current != expected:
                raise AuthorityPhaseError(f"{label} semantic fields changed")
        elif isinstance(expected, frozenset):
            if current != expected:
                raise AuthorityPhaseError(f"{label} semantic fields changed")
        elif current is not expected:
            raise AuthorityPhaseError(f"{label} object identity changed")


class _Lifecycle:
    __slots__ = (
        "value",
        "state",
        "authority",
        "prepared",
        "pending",
        "transaction",
        "operation",
    )

    def __init__(
        self,
        value: object,
        *,
        authority: ToolExecutionAuthority | None = None,
        prepared: object | None = None,
        pending: object | None = None,
        transaction: object | None = None,
        operation: object | None = None,
    ) -> None:
        self.value = value
        self.state: Literal["issued", "in_flight"] = "issued"
        self.authority = authority
        self.prepared = prepared
        self.pending = pending
        self.transaction = transaction
        self.operation = operation


class _PendingRecord:
    __slots__ = ("value", "token", "semantic", "owners")

    def __init__(self, value: object, token: PendingInstanceToken) -> None:
        self.value = value
        self.token = token
        self.semantic: dict[str, object] = {}
        self.owners: set[int] = set()


class _RegisteredIdentity:
    __slots__ = (
        "value",
        "authority",
        "fingerprint",
        "candidate_count",
        "candidate_ordinal",
        "parent",
        "provenance",
        "semantic",
    )

    def __init__(
        self,
        value: object,
        *,
        authority: ToolExecutionAuthority | None = None,
        fingerprint: str | None = None,
        candidate_count: int | None = None,
        candidate_ordinal: int | None = None,
        parent: object | None = None,
        provenance: tuple[object, ...] = (),
        semantic: Mapping[str, object] | None = None,
    ) -> None:
        self.value = value
        self.authority = authority
        self.fingerprint = fingerprint
        self.candidate_count = candidate_count
        self.candidate_ordinal = candidate_ordinal
        self.parent = parent
        self.provenance = provenance
        self.semantic = dict(semantic or {})


class _RepositoryBindingTicket:
    """One-shot composition-root ticket for a Session-bound repository port."""

    __slots__ = ("factory", "repository", "session", "constraint", "authority", "used")

    def __init__(
        self,
        factory: "AuthorityFactory",
        *,
        repository: object,
        session: object,
        constraint: object,
        authority: ToolExecutionAuthority,
    ) -> None:
        self.factory = factory
        self.repository = repository
        self.session = session
        self.constraint = constraint
        self.authority = authority
        self.used = False


class _AuthorityRecord:
    __slots__ = ("authority", "token", "kind", "fields", "trusted_scope_fields")

    def __init__(self, authority: ToolExecutionAuthority, token: AuthorityInstanceToken) -> None:
        self.authority = authority
        self.token = token
        self.kind = "segment" if isinstance(authority, SegmentExecutionAuthority) else "approval"
        self.fields = _dataclass_snapshot(authority)
        trusted_scope = getattr(authority, "trusted_scope")
        self.trusted_scope_fields = _dataclass_snapshot(trusted_scope)


class AuthorityFactory:
    """Factory plus bounded registry for one explicit execution scope."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._closed = False
        self._authorities: dict[int, _AuthorityRecord] = {}
        self._pending: dict[int, _PendingRecord] = {}
        self._prepared: dict[int, tuple[object, PreparedInstanceToken, ToolExecutionAuthority]] = {}
        self._prepared_fields: dict[int, tuple[object, ...]] = {}
        self._prepared_origins: dict[
            int, NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity
        ] = {}
        self._prepared_construction: dict[int, _Lifecycle] = {}
        self._constraints: dict[
            int, tuple[ApplicationScopeConstraint, ToolExecutionAuthority, dict[str, object]]
        ] = {}
        self._resolutions: dict[
            int, tuple[BindingTargetResolution, ToolExecutionAuthority, dict[str, object]]
        ] = {}
        self._calls: dict[int, tuple[AuthorityCallIdentity, ToolExecutionAuthority, str]] = {}
        self._call_fields: dict[int, dict[str, object]] = {}
        self._claims: dict[int, _Lifecycle] = {}
        self._claim_fields: dict[int, dict[str, object]] = {}
        self._claim_keys: dict[tuple[object, ...], int] = {}
        self._proofs: dict[int, _Lifecycle] = {}
        self._proof_fields: dict[int, dict[str, object]] = {}
        self._proof_keys: dict[tuple[int, int, int], int] = {}
        self._runner_invocations: dict[int, _RegisteredIdentity] = {}
        self._tool_contexts: dict[int, _RegisteredIdentity] = {}
        self._surfaces: dict[int, _RegisteredIdentity] = {}
        self._bindings: dict[int, _RegisteredIdentity] = {}
        self._gateway_sessions: dict[int, _RegisteredIdentity] = {}
        self._repository_binding_tickets: dict[int, _RepositoryBindingTicket] = {}
        self._repository_bindings: dict[int, _RegisteredIdentity] = {}
        self._repository_binding_fields: dict[int, tuple[object, ...]] = {}
        self._attempts: dict[str, _RegisteredIdentity] = {}
        self._operations: dict[int, _RegisteredIdentity] = {}
        self._transactions: dict[int, _RegisteredIdentity] = {}
        self._tool_specs: dict[
            int,
            tuple[ToolSpec[Any, Any], ToolExecutionAuthority],
        ] = {}
        self._tool_spec_fields: dict[int, tuple[object, ...]] = {}
        self._tool_spec_preparations: dict[
            int,
            tuple[
                NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity,
                ToolSpec[Any, Any],
                ToolExecutionAuthority,
            ],
        ] = {}
        self._objects: dict[int, object] = {}
        self._attempt_sequence = 0

    def __enter__(self) -> "AuthorityFactory":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuthorityPhaseError("execution scope is closed")

    def _claim_object(self, value: object) -> None:
        """Claim an exact runtime object for this factory while it is live."""

        with _ACTIVE_AUTHORITIES_LOCK:
            found = _ACTIVE_OBJECTS.get(id(value))
            if found is not None:
                if found[0] is not value:
                    raise AuthorityPhaseError("runtime object address was reused")
                if found[1] is not self:
                    raise AuthorityPhaseError("runtime object belongs to another factory")
                return
            _ACTIVE_OBJECTS[id(value)] = (value, self)

    def _drop_object(self, value: object) -> None:
        with _ACTIVE_AUTHORITIES_LOCK:
            found = _ACTIVE_OBJECTS.get(id(value))
            if found is not None and found[0] is value and found[1] is self:
                del _ACTIVE_OBJECTS[id(value)]

    def _prune_global_objects(self) -> None:
        with _ACTIVE_AUTHORITIES_LOCK:
            for object_id, (_, owner) in tuple(_ACTIVE_OBJECTS.items()):
                if owner is self and object_id not in self._objects:
                    del _ACTIVE_OBJECTS[object_id]

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_count_unlocked()

    def _active_count_unlocked(self) -> int:
        return (
            len(self._authorities)
            + len(self._pending)
            + len(self._prepared)
            + len(self._prepared_construction)
            + len(self._constraints)
            + len(self._resolutions)
            + len(self._calls)
            + len(self._claims)
            + len(self._proofs)
            + len(self._runner_invocations)
            + len(self._tool_contexts)
            + len(self._surfaces)
            + len(self._bindings)
            + len(self._gateway_sessions)
            + len(self._repository_bindings)
            + len(self._attempts)
            + len(self._operations)
            + len(self._transactions)
            + len(self._tool_specs)
            + len(self._tool_spec_preparations)
        )

    @property
    def registry_size(self) -> int:
        return self.active_count

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with _ACTIVE_AUTHORITIES_LOCK:
                for authority_id, (_, owner) in tuple(_ACTIVE_AUTHORITIES.items()):
                    if owner is self:
                        del _ACTIVE_AUTHORITIES[authority_id]
                for object_id, (_, owner) in tuple(_ACTIVE_OBJECTS.items()):
                    if owner is self:
                        del _ACTIVE_OBJECTS[object_id]
                for binding_id, (_, owner) in tuple(_ACTIVE_REPOSITORY_BINDINGS.items()):
                    if owner is self:
                        del _ACTIVE_REPOSITORY_BINDINGS[binding_id]
            self._authorities.clear()
            self._pending.clear()
            self._prepared.clear()
            self._prepared_fields.clear()
            self._prepared_origins.clear()
            self._prepared_construction.clear()
            self._constraints.clear()
            self._resolutions.clear()
            self._calls.clear()
            self._call_fields.clear()
            self._claims.clear()
            self._claim_fields.clear()
            self._claim_keys.clear()
            self._proofs.clear()
            self._proof_fields.clear()
            self._proof_keys.clear()
            self._runner_invocations.clear()
            self._tool_contexts.clear()
            self._surfaces.clear()
            self._bindings.clear()
            self._gateway_sessions.clear()
            self._repository_binding_tickets.clear()
            self._repository_bindings.clear()
            self._repository_binding_fields.clear()
            self._attempts.clear()
            self._operations.clear()
            self._transactions.clear()
            self._tool_specs.clear()
            self._tool_spec_fields.clear()
            self._tool_spec_preparations.clear()
            self._objects.clear()

    def _register_authority(
        self, authority: ToolExecutionAuthority, token: AuthorityInstanceToken
    ) -> ToolExecutionAuthority:
        self._ensure_open()
        self._claim_object(authority)
        self._claim_object(token)
        authority_id = id(authority)
        self._authorities[authority_id] = _AuthorityRecord(authority, token)
        self._objects[authority_id] = authority
        self._objects[id(token)] = token
        with _ACTIVE_AUTHORITIES_LOCK:
            _ACTIVE_AUTHORITIES[authority_id] = (authority, self)
        return authority

    def _authority_record(self, authority: object) -> _AuthorityRecord:
        with self._lock:
            self._ensure_open()
            if type(authority) not in {SegmentExecutionAuthority, ApprovalExecutionAuthority}:
                raise AuthorityPhaseError("authority object has an invalid concrete type")
            record = self._authorities.get(id(authority))
            if record is None or record.authority is not authority:
                raise AuthorityPhaseError("authority object is not active in this execution scope")
            _validate_snapshot(authority, record.fields, "authority")
            _validate_snapshot(
                getattr(authority, "trusted_scope"),
                record.trusted_scope_fields,
                "trusted scope",
            )
            return record

    def _authority_record_identity_only(self, authority: object) -> _AuthorityRecord:
        """Return an active authority by exact object identity for cleanup.

        Normal ports use ``_authority_record`` and therefore reject semantic
        mutations.  Revocation must still be able to remove the object graph
        after such a mutation, so cleanup intentionally performs only the
        closed concrete-type and registry-identity checks.
        """

        with self._lock:
            self._ensure_open()
            if type(authority) not in {SegmentExecutionAuthority, ApprovalExecutionAuthority}:
                raise AuthorityPhaseError("authority object has an invalid concrete type")
            record = self._authorities.get(id(authority))
            if record is None or record.authority is not authority:
                raise AuthorityPhaseError("authority object is not active in this execution scope")
            return record

    def _segment_record(self, authority: object) -> _AuthorityRecord:
        record = self._authority_record(authority)
        if not isinstance(record.authority, SegmentExecutionAuthority):
            raise AuthorityPhaseError("Segment authority is required for this use")
        return record

    def _approval_record(self, authority: object) -> _AuthorityRecord:
        record = self._authority_record(authority)
        if not isinstance(record.authority, ApprovalExecutionAuthority):
            raise AuthorityPhaseError("Approval authority is required for this use")
        return record

    def create_segment_authority(
        self,
        *,
        conversation_id: int,
        conversation_scope_revision: int,
        segment_id: str,
        trusted_scope: TrustedContextScope,
        capability_profile_id: str = "agent_typed_v1",
        capabilities: frozenset[object] = frozenset(),
        capability_policy_version: str = "capability-policy-v1",
        binding_policy_version: str = "binding-policy-v1",
        capability_profile_fingerprint: str = "sha256:" + "0" * 64,
        binding_policy_fingerprint: str = "sha256:" + "0" * 64,
    ) -> SegmentExecutionAuthority:
        with self._lock:
            self._ensure_open()
            token = cast(AuthorityInstanceToken, _new_opaque_handle(AuthorityInstanceToken))
            authority = SegmentExecutionAuthority(
                conversation_id=conversation_id,
                conversation_scope_revision=conversation_scope_revision,
                segment_id=segment_id,
                trusted_scope=trusted_scope,
                capability_profile_id=capability_profile_id,
                capabilities=capabilities,
                capability_policy_version=capability_policy_version,
                binding_policy_version=binding_policy_version,
                capability_profile_fingerprint=capability_profile_fingerprint,
                binding_policy_fingerprint=binding_policy_fingerprint,
                authority_instance_token=token,
            )
            return cast(SegmentExecutionAuthority, self._register_authority(authority, token))

    # Common composition-root spellings used by runtime call sites.
    segment_authority = create_segment_authority
    new_segment_authority = create_segment_authority

    def create_approval_authority(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        conversation_scope_revision: int,
        trusted_scope: TrustedContextScope,
        pending_identity: PendingInstanceToken | object,
        pending_action_revision: int,
        tool_call_id: str,
        tool_name: str,
        effective_args_digest: str,
        capability_profile_id: str = "agent_typed_v1",
        capabilities: frozenset[object] = frozenset(),
        capability_policy_version: str = "capability-policy-v1",
        binding_policy_version: str = "binding-policy-v1",
        capability_profile_fingerprint: str = "sha256:" + "0" * 64,
        binding_policy_fingerprint: str = "sha256:" + "0" * 64,
    ) -> ApprovalExecutionAuthority:
        with self._lock:
            self._ensure_open()
            if type(pending_identity) is PendingInstanceToken:
                raise AuthorityPhaseError("Approval authority requires the registered Pending object")
            pending_record = self._pending_record_for_object(pending_identity)
            pending_token = pending_record.token
            self._require_pending_semantic(
                pending_record,
                conversation_id=conversation_id,
                operation_id=operation_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                pending_action_revision=pending_action_revision,
                effective_args_digest=effective_args_digest,
            )
            token = cast(AuthorityInstanceToken, _new_opaque_handle(AuthorityInstanceToken))
            authority = ApprovalExecutionAuthority(
                operation_id=operation_id,
                conversation_id=conversation_id,
                conversation_scope_revision=conversation_scope_revision,
                trusted_scope=trusted_scope,
                pending_identity=pending_token,
                pending_action_revision=pending_action_revision,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                effective_args_digest=effective_args_digest,
                capability_profile_id=capability_profile_id,
                capabilities=capabilities,
                capability_policy_version=capability_policy_version,
                binding_policy_version=binding_policy_version,
                capability_profile_fingerprint=capability_profile_fingerprint,
                binding_policy_fingerprint=binding_policy_fingerprint,
                approval_authority_instance_token=token,
            )
            self._claim_pending_owner(pending_record, authority)
            return cast(ApprovalExecutionAuthority, self._register_authority(authority, token))

    approval_authority = create_approval_authority
    new_approval_authority = create_approval_authority

    def revoke_authority(self, authority: ToolExecutionAuthority) -> None:
        with self._lock:
            record = self._authority_record_identity_only(authority)
            for claim_id, lifecycle in tuple(self._claims.items()):
                if lifecycle.authority is authority:
                    self._drop_claim_key(claim_id)
                    del self._claims[claim_id]
                    self._claim_fields.pop(claim_id, None)
                    claim_token = getattr(lifecycle.value, "pending_claim_instance_token", None)
                    if claim_token is None:
                        claim_token = getattr(
                            lifecycle.value, "execution_claim_instance_token", None
                        )
                    if claim_token is not None:
                        self._objects.pop(id(claim_token), None)
                        self._drop_object(claim_token)
                    self._objects.pop(claim_id, None)
                    self._drop_object(lifecycle.value)
            for proof_id, lifecycle in tuple(self._proofs.items()):
                if lifecycle.authority is authority:
                    self._drop_proof_key(proof_id)
                    del self._proofs[proof_id]
                    self._proof_fields.pop(proof_id, None)
                    proof_token = getattr(
                        lifecycle.value, "omitted_token_proof_instance_token", None
                    )
                    if proof_token is not None:
                        self._objects.pop(id(proof_token), None)
                        self._drop_object(proof_token)
                    self._objects.pop(proof_id, None)
                    self._drop_object(lifecycle.value)
            for prepared_id, (prepared, prepared_token, owner) in tuple(self._prepared.items()):
                if owner is authority:
                    del self._prepared[prepared_id]
                    self._prepared_fields.pop(prepared_id, None)
                    self._prepared_origins.pop(prepared_id, None)
                    self._objects.pop(prepared_id, None)
                    self._objects.pop(id(prepared_token), None)
                    self._drop_object(prepared)
                    self._drop_object(prepared_token)
            for construction_id, lifecycle in tuple(self._prepared_construction.items()):
                if lifecycle.authority is authority:
                    del self._prepared_construction[construction_id]
                    self._objects.pop(construction_id, None)
            for constraint_id, (_, owner, _) in tuple(self._constraints.items()):
                if owner is authority:
                    del self._constraints[constraint_id]
                    self._objects.pop(constraint_id, None)
            for resolution_id, (_, owner, _) in tuple(self._resolutions.items()):
                if owner is authority:
                    del self._resolutions[resolution_id]
                    self._objects.pop(resolution_id, None)
            for call_id, (_, owner, _) in tuple(self._calls.items()):
                if owner is authority:
                    del self._calls[call_id]
                    self._call_fields.pop(call_id, None)
                    self._objects.pop(call_id, None)
            for table in (
                self._runner_invocations,
                self._tool_contexts,
                self._surfaces,
                self._bindings,
                self._gateway_sessions,
                self._operations,
                self._transactions,
            ):
                for identity_id, registration in tuple(table.items()):
                    if registration.authority is authority:
                        del table[identity_id]
                        self._objects.pop(identity_id, None)
            for binding_id, registration in tuple(self._repository_bindings.items()):
                if registration.authority is authority:
                    del self._repository_bindings[binding_id]
                    self._repository_binding_fields.pop(binding_id, None)
                    self._objects.pop(binding_id, None)
                    self._drop_object(registration.value)
                    with _ACTIVE_AUTHORITIES_LOCK:
                        found_binding = _ACTIVE_REPOSITORY_BINDINGS.get(binding_id)
                        if found_binding is not None and found_binding[0] is registration.value:
                            del _ACTIVE_REPOSITORY_BINDINGS[binding_id]
            for ticket_id, ticket in tuple(self._repository_binding_tickets.items()):
                if ticket.authority is authority:
                    del self._repository_binding_tickets[ticket_id]
                    self._objects.pop(ticket_id, None)
                    self._drop_object(ticket)
            for spec_id, (spec, owner) in tuple(self._tool_specs.items()):
                if owner is authority:
                    del self._tool_specs[spec_id]
                    self._tool_spec_fields.pop(spec_id, None)
            for identity_id, (_, _, owner) in tuple(
                self._tool_spec_preparations.items()
            ):
                if owner is authority:
                    del self._tool_spec_preparations[identity_id]
            for attempt_id, registration in tuple(self._attempts.items()):
                if registration.authority is authority:
                    del self._attempts[attempt_id]
                    self._objects.pop(id(attempt_id), None)
            for pending_id, pending_record in tuple(self._pending.items()):
                if id(authority) in pending_record.owners:
                    pending_record.owners.discard(id(authority))
                    if not pending_record.owners:
                        del self._pending[pending_id]
                        self._objects.pop(pending_id, None)
                        self._objects.pop(id(pending_record.token), None)
            self._objects.pop(id(authority), None)
            self._objects.pop(id(record.token), None)
            self._prune_global_objects()
            del self._authorities[id(authority)]
            with _ACTIVE_AUTHORITIES_LOCK:
                _ACTIVE_AUTHORITIES.pop(id(authority), None)
            del record

    def register_pending(
        self,
        pending: object,
        *,
        conversation_id: int | None = None,
        operation_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        pending_action_revision: int | None = None,
        pending_confirmation_claim_id: str | None = None,
        arguments_digest: str | None = None,
        effective_args_digest: str | None = None,
    ) -> PendingInstanceToken:
        with self._lock:
            self._ensure_open()
            if isinstance(pending, _OpaqueHandle) and type(pending) is not PendingInstanceToken:
                raise AuthorityPhaseError("Pending registration requires the exact Pending role")
            if pending is None or isinstance(
                pending, (str, bytes, int, float, bool, tuple, frozenset)
            ):
                raise AuthorityPhaseError("Pending must be a caller-owned object identity")
            if type(pending) is PendingInstanceToken:
                for record in self._pending.values():
                    if record.token is pending:
                        return record.token
                raise AuthorityPhaseError("pending token is not active in this scope")
            pending_id = id(pending)
            existing = self._pending.get(pending_id)
            if existing is not None and existing.value is pending:
                if any(
                    value is not None
                    for value in (
                        conversation_id,
                        operation_id,
                        tool_call_id,
                        tool_name,
                        pending_action_revision,
                        pending_confirmation_claim_id,
                    )
                ):
                    self._set_pending_semantic(
                        existing,
                        conversation_id=conversation_id,
                        operation_id=operation_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        pending_action_revision=pending_action_revision,
                        pending_confirmation_claim_id=pending_confirmation_claim_id,
                        arguments_digest=arguments_digest,
                        effective_args_digest=effective_args_digest,
                    )
                return existing.token
            token = cast(PendingInstanceToken, _new_opaque_handle(PendingInstanceToken))
            record = _PendingRecord(pending, token)
            # Capture bounded semantic identity at first registration.  Later
            # reads never trust a replacement object's mutable fields.
            values = self._pending_semantic_from_object(pending)
            resolved_arguments_digest = (
                arguments_digest
                if arguments_digest is not None
                else cast(str | None, values["arguments_digest"])
            )
            resolved_effective_args_digest = (
                effective_args_digest
                if effective_args_digest is not None
                else cast(str | None, values["effective_args_digest"])
            )
            if resolved_arguments_digest is None:
                resolved_arguments_digest = resolved_effective_args_digest
            if resolved_effective_args_digest is None:
                resolved_effective_args_digest = resolved_arguments_digest
            if resolved_arguments_digest is None or resolved_effective_args_digest is None:
                raise AuthorityPhaseError("Pending argument digest is required at registration")
            resolved_core = (
                conversation_id
                if conversation_id is not None
                else cast(int | None, values["conversation_id"]),
                operation_id
                if operation_id is not None
                else cast(str | None, values["operation_id"]),
                tool_call_id
                if tool_call_id is not None
                else cast(str | None, values["tool_call_id"]),
                tool_name
                if tool_name is not None
                else cast(str | None, values["tool_name"]),
                pending_action_revision
                if pending_action_revision is not None
                else cast(int | None, values["pending_action_revision"]),
            )
            if any(value is None for value in resolved_core):
                raise AuthorityPhaseError("Pending semantic identity is required at registration")
            self._set_pending_semantic(
                record,
                conversation_id=conversation_id
                if conversation_id is not None
                else cast(int | None, values["conversation_id"]),
                operation_id=operation_id
                if operation_id is not None
                else cast(str | None, values["operation_id"]),
                tool_call_id=tool_call_id
                if tool_call_id is not None
                else cast(str | None, values["tool_call_id"]),
                tool_name=tool_name
                if tool_name is not None
                else cast(str | None, values["tool_name"]),
                pending_action_revision=(
                    pending_action_revision
                    if pending_action_revision is not None
                    else cast(int | None, values["pending_action_revision"])
                ),
                pending_confirmation_claim_id=(
                    pending_confirmation_claim_id
                    if pending_confirmation_claim_id is not None
                    else cast(str | None, values["pending_confirmation_claim_id"])
                ),
                arguments_digest=resolved_arguments_digest,
                effective_args_digest=resolved_effective_args_digest,
                allow_partial=True,
            )
            self._claim_object(pending)
            self._claim_object(token)
            self._pending[pending_id] = record
            self._objects[pending_id] = pending
            self._objects[id(token)] = token
            return token

    def _set_pending_semantic(
        self,
        record: _PendingRecord,
        *,
        conversation_id: int | None,
        operation_id: str | None,
        tool_call_id: str | None,
        tool_name: str | None,
        pending_action_revision: int | None,
        pending_confirmation_claim_id: str | None = None,
        arguments_digest: str | None = None,
        effective_args_digest: str | None = None,
        allow_partial: bool = False,
    ) -> None:
        values = {
            "conversation_id": conversation_id,
            "operation_id": operation_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "pending_action_revision": pending_action_revision,
            "pending_confirmation_claim_id": pending_confirmation_claim_id,
            "arguments_digest": arguments_digest,
            "effective_args_digest": effective_args_digest,
        }
        if not allow_partial and any(value is None for value in values.values()):
            raise AuthorityPhaseError("Pending semantic identity is incomplete")
        for name, value in values.items():
            if value is None:
                continue
            if name in {"conversation_id", "pending_action_revision"}:
                require_positive_int64(value, name)
            elif name in {"arguments_digest", "effective_args_digest"}:
                _require_digest(value, name)
            elif type(value) is not str or not value:
                raise AuthorityPhaseError(f"Pending {name} must be non-empty text")
            current = record.semantic.get(name)
            if current is not None:
                if name in {"arguments_digest", "effective_args_digest"}:
                    if not isinstance(current, str) or not constant_time_equal(
                        current, cast(str, value)
                    ):
                        raise AuthorityPhaseError(f"Pending {name} identity changed")
                elif current != value:
                    raise AuthorityPhaseError(f"Pending {name} identity changed")
            record.semantic[name] = value

    def _require_pending_semantic(self, record: _PendingRecord, **expected: object) -> None:
        self._validate_pending_snapshot(record)
        core = (
            expected.get("conversation_id"),
            expected.get("operation_id"),
            expected.get("tool_call_id"),
            expected.get("tool_name"),
            expected.get("pending_action_revision"),
        )
        if any(value is None for value in core):
            raise AuthorityPhaseError("Pending semantic identity is incomplete")
        required_names = (
            "conversation_id",
            "operation_id",
            "tool_call_id",
            "tool_name",
            "pending_action_revision",
        )
        for name in required_names:
            if name not in record.semantic:
                raise AuthorityPhaseError("Pending semantic identity was not sealed at registration")
        for name in ("arguments_digest", "effective_args_digest"):
            if expected.get(name) is not None and name not in record.semantic:
                raise AuthorityPhaseError("Pending argument digest was not sealed at registration")
        if (
            expected.get("pending_confirmation_claim_id") is not None
            and "pending_confirmation_claim_id" not in record.semantic
        ):
            raise AuthorityPhaseError("Pending confirmation identity was not sealed at registration")
        self._set_pending_semantic(
            record,
            conversation_id=cast(int | None, expected.get("conversation_id")),
            operation_id=cast(str | None, expected.get("operation_id")),
            tool_call_id=cast(str | None, expected.get("tool_call_id")),
            tool_name=cast(str | None, expected.get("tool_name")),
            pending_action_revision=cast(int | None, expected.get("pending_action_revision")),
            pending_confirmation_claim_id=cast(
                str | None, expected.get("pending_confirmation_claim_id")
            ),
            arguments_digest=cast(str | None, expected.get("arguments_digest")),
            effective_args_digest=cast(str | None, expected.get("effective_args_digest")),
            allow_partial=True,
        )

    def _validate_pending_snapshot(self, record: _PendingRecord) -> None:
        """Fail closed when a caller mutates a registered Pending in place."""

        current_values = self._pending_semantic_from_object(record.value)
        for name, expected in record.semantic.items():
            current = current_values.get(name)
            if current is None:
                if type(record.value) is object:
                    # A plain object has no mutable semantic surface; explicit
                    # registration kwargs are its complete bounded identity.
                    continue
                raise AuthorityPhaseError("Pending semantic identity was removed")
            if name in {"conversation_id", "pending_action_revision"}:
                require_positive_int64(current, name)
                if current != expected:
                    raise AuthorityPhaseError("Pending semantic identity changed")
            elif name in {"arguments_digest", "effective_args_digest"}:
                current_digest = _require_digest(current, name)
                if not isinstance(expected, str) or not constant_time_equal(
                    current_digest, expected
                ):
                    raise AuthorityPhaseError("Pending digest identity changed")
            else:
                _require_text(current, name)
                if current != expected:
                    raise AuthorityPhaseError("Pending semantic identity changed")

    @staticmethod
    def _pending_semantic_from_object(pending: object) -> dict[str, object | None]:
        """Read only bounded Pending identity fields at registration time."""

        names = (
            "conversation_id",
            "operation_id",
            "tool_call_id",
            "tool_name",
            "pending_action_revision",
            "pending_confirmation_claim_id",
            "arguments_digest",
            "effective_args_digest",
        )
        values: dict[str, object | None] = {}
        for name in names:
            values[name] = getattr(pending, name, None)
        if values["arguments_digest"] is None:
            values["arguments_digest"] = values["effective_args_digest"]
        if values["effective_args_digest"] is None:
            values["effective_args_digest"] = values["arguments_digest"]
        return values

    def _mark_pending_owner(self, pending: object, authority: ToolExecutionAuthority) -> None:
        record = self._pending.get(id(pending))
        if record is None or record.value is not pending:
            raise AuthorityPhaseError("Pending object is not registered")
        self._claim_pending_owner(record, authority)

    @staticmethod
    def _claim_pending_owner(
        record: _PendingRecord, authority: ToolExecutionAuthority
    ) -> None:
        owner_id = id(authority)
        if record.owners and any(existing != owner_id for existing in record.owners):
            raise AuthorityPhaseError("Pending is already owned by another authority")
        record.owners.add(owner_id)

    def _pending_token(self, pending: object) -> PendingInstanceToken:
        with self._lock:
            if type(pending) is PendingInstanceToken:
                for record in self._pending.values():
                    if record.token is pending:
                        self._validate_pending_snapshot(record)
                        return pending
                raise AuthorityPhaseError("pending identity is not active")
            pending_id = id(pending)
            found = self._pending.get(pending_id)
            if found is None or found.value is not pending:
                raise AuthorityPhaseError("pending object is not registered")
            self._validate_pending_snapshot(found)
            return found.token

    def _pending_record_for_object(self, pending: object) -> _PendingRecord:
        if type(pending) is PendingInstanceToken:
            raise AuthorityPhaseError("the registered Pending object is required")
        pending_id = id(pending)
        found = self._pending.get(pending_id)
        if found is None or found.value is not pending:
            raise AuthorityPhaseError("pending object is not registered")
        self._validate_pending_snapshot(found)
        return found

    def _pending_record_for_token(self, token: PendingInstanceToken) -> _PendingRecord:
        if type(token) is not PendingInstanceToken:
            raise AuthorityPhaseError("pending identity token has an invalid type")
        for record in self._pending.values():
            if record.token is token:
                self._validate_pending_snapshot(record)
                return record
        raise AuthorityPhaseError("pending identity is not active")

    def pending_token(self, pending: object) -> PendingInstanceToken:
        with self._lock:
            return self._pending_token(pending)

    def register_prepared(
        self,
        prepared: PreparedToolCall[Any, Any],
        authority: ToolExecutionAuthority,
    ) -> PreparedInstanceToken:
        with self._lock:
            self._ensure_open()
            self._authority_record(authority)
            existing = self._prepared.get(id(prepared))
            if existing is not None and existing[0] is prepared and existing[2] is authority:
                self._prepared_record(prepared)
                return existing[1]
            raise AuthorityPhaseError(
                "PreparedToolCall must be created through the controlled prepare port"
            )

    def prepare_tool_call(
        self,
        authority: ToolExecutionAuthority,
        *,
        prepare_identity: NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity,
        tool_call_id: str,
        spec: ToolSpec[Any, Any],
        arguments: Any,
        typed_args: Any,
        arguments_digest: str,
        contract_fingerprint: str,
        binding: BindingAudit,
    ) -> PreparedToolCall[Any, Any]:
        """Construct and bind PreparedToolCall at the controlled prepare port."""

        with self._lock:
            self._ensure_open()
            self._authority_record(authority)
            if type(prepare_identity) is NewTurnPrepareCallIdentity:
                identity_authority = self._call_authority(
                    prepare_identity,
                    AuthorityUse.NEW_TURN_PREPARE,
                )
            elif type(prepare_identity) is ApprovedWritePrepareCallIdentity:
                identity_authority = self._call_authority(
                    prepare_identity,
                    AuthorityUse.APPROVED_WRITE_PREPARE,
                )
            else:
                raise AuthorityPhaseError("PreparedToolCall requires a registered prepare identity")
            if identity_authority is not authority:
                raise AuthorityPhaseError("prepare identity belongs to another authority")
            spec_registration = self._tool_specs.get(id(spec))
            if spec_registration is None or spec_registration[0] is not spec:
                raise AuthorityPhaseError("ToolSpec is not registered for this prepare identity")
            preparation = self._tool_spec_preparations.get(id(prepare_identity))
            if (
                spec_registration[1] is not authority
                or preparation is None
                or preparation[0] is not prepare_identity
                or preparation[1] is not spec
                or preparation[2] is not authority
            ):
                raise AuthorityPhaseError("ToolSpec provenance does not match prepare identity")
            self._validate_registered_tool_spec(spec)
            identity_tool_call_id = getattr(prepare_identity, "tool_call_id")
            identity_tool_name = getattr(prepare_identity, "tool_name")
            identity_digest = getattr(
                prepare_identity,
                "arguments_digest",
                getattr(prepare_identity, "effective_args_digest", None),
            )
            if tool_call_id != identity_tool_call_id or spec.name != identity_tool_name:
                raise AuthorityPhaseError("PreparedToolCall identity does not match prepare call")
            expected_digest = _canonical_arguments_digest(arguments)
            _require_digest(arguments_digest, "arguments_digest")
            if not constant_time_equal(arguments_digest, expected_digest):
                raise AuthorityPhaseError("Prepared arguments digest is not canonical")
            if not isinstance(identity_digest, str) or not constant_time_equal(
                arguments_digest, identity_digest
            ):
                raise AuthorityPhaseError("Prepared digest does not match prepare call")
            expected_contract_fingerprint = _canonical_contract_fingerprint(spec.contract.payload)
            _require_digest(contract_fingerprint, "contract_fingerprint")
            if not constant_time_equal(contract_fingerprint, expected_contract_fingerprint):
                raise AuthorityPhaseError("Prepared contract fingerprint is not canonical")
            prepared = PreparedToolCall(
                tool_call_id=tool_call_id,
                spec=spec,
                arguments=arguments,
                typed_args=typed_args,
                arguments_digest=arguments_digest,
                contract_fingerprint=contract_fingerprint,
                binding=binding,
            )
            if not self._prepared_shape_is_controlled(prepared):
                raise AuthorityPhaseError("PreparedToolCall has an untrusted shape")
            self._claim_object(prepared)
            object.__setattr__(prepared, "authority_instance_token", _authority_token(authority))
            token = cast(PreparedInstanceToken, _new_opaque_handle(PreparedInstanceToken))
            self._claim_object(token)
            self._prepared[id(prepared)] = (prepared, token, authority)
            self._prepared_origins[id(prepared)] = prepare_identity
            self._prepared_fields[id(prepared)] = (
                prepared.tool_call_id,
                prepared.spec.name,
                prepared.arguments_digest,
                prepared.contract_fingerprint,
                prepared.spec,
                prepared.binding,
                prepared._replacement_guard,
                _dataclass_snapshot(prepared.binding),
                _canonical_arguments_digest(prepared.typed_args),
            )
            self._objects[id(prepared)] = prepared
            self._objects[id(token)] = token
            return prepared

    def register_tool_spec(
        self,
        spec: ToolSpec[Any, Any],
        *,
        authority: ToolExecutionAuthority,
        prepare_identity: NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity,
    ) -> ToolSpec[Any, Any]:
        """Register one exact, composition-root-validated ToolSpec for prepare."""

        with self._lock:
            self._ensure_open()
            self._authority_record(authority)
            if (
                type(spec) is not ToolSpec
                or type(spec.contract) is not ProviderToolContract
                or type(spec.binding_contract) is not BindingContract
            ):
                raise AuthorityPhaseError("ToolSpec must use the exact validated contract type")
            if not callable(spec.decoder) or not callable(spec.executor):
                raise AuthorityPhaseError("ToolSpec decoder/executor must be callable")
            if type(prepare_identity) is NewTurnPrepareCallIdentity:
                identity_authority = self._call_authority(
                    prepare_identity,
                    AuthorityUse.NEW_TURN_PREPARE,
                )
            elif type(prepare_identity) is ApprovedWritePrepareCallIdentity:
                identity_authority = self._call_authority(
                    prepare_identity,
                    AuthorityUse.APPROVED_WRITE_PREPARE,
                )
                if spec.kind != "write" or spec.confirmation_policy != "required":
                    raise AuthorityPhaseError("Approved prepare requires a confirmed write spec")
            else:
                raise AuthorityPhaseError("ToolSpec requires a registered prepare identity")
            if identity_authority is not authority:
                raise AuthorityPhaseError("prepare identity belongs to another authority")
            identity_tool_name = getattr(prepare_identity, "tool_name")
            if spec.name != identity_tool_name:
                raise AuthorityPhaseError("ToolSpec name does not match prepare identity")
            spec_snapshot = self._tool_spec_snapshot(spec)
            existing = self._tool_specs.get(id(spec))
            if existing is not None:
                if existing[0] is not spec or existing[1] is not authority:
                    raise AuthorityPhaseError("ToolSpec provenance changed")
                self._validate_registered_tool_spec(spec)
            else:
                self._tool_specs[id(spec)] = (spec, authority)
                self._tool_spec_fields[id(spec)] = spec_snapshot
            preparation = self._tool_spec_preparations.get(id(prepare_identity))
            if preparation is not None and (
                preparation[0] is not prepare_identity
                or preparation[1] is not spec
                or preparation[2] is not authority
            ):
                raise AuthorityPhaseError("prepare identity is already bound to another ToolSpec")
            self._tool_spec_preparations[id(prepare_identity)] = (
                prepare_identity,
                spec,
                authority,
            )
            return spec

    @staticmethod
    def _tool_spec_snapshot(spec: ToolSpec[Any, Any]) -> tuple[object, ...]:
        if type(spec.binding_contract) is not BindingContract:
            raise AuthorityPhaseError("ToolSpec binding contract type changed")

        resolver_snapshots: list[tuple[object, ...]] = []
        for resolver in spec.binding_resolvers:
            if isinstance(resolver, BindingResolverSpec):
                resolver_snapshots.append(
                    (
                        "descriptor",
                        resolver,
                        resolver.resolve,
                        resolver.resolver_id,
                        resolver.entity_kind,
                        resolver.arg_path,
                        resolver.presence,
                        resolver.identity_type,
                    )
                )
            else:
                # Legacy/custom specs may still carry a bare callable.  Its
                # object identity is the only safe snapshot available.
                resolver_snapshots.append(("callable", resolver))
        return (
            spec.contract,
            _canonical_contract_fingerprint(spec.contract.payload),
            spec.decoder,
            spec.executor,
            spec.kind,
            spec.confirmation_policy,
            spec.required_capabilities,
            spec.binding_contract,
            (spec.binding_contract.kind, spec.binding_contract.entity_kind),
            tuple(resolver_snapshots),
        )

    @staticmethod
    def _resolver_snapshots_match(
        current: tuple[tuple[object, ...], ...],
        expected: tuple[tuple[object, ...], ...],
    ) -> bool:
        if len(current) != len(expected):
            return False
        for current_item, expected_item in zip(current, expected):
            if current_item[0] != expected_item[0] or current_item[1] is not expected_item[1]:
                return False
            if current_item[0] == "descriptor":
                if current_item[2] is not expected_item[2] or current_item[3:] != expected_item[3:]:
                    return False
        return True

    def _validate_registered_tool_spec(self, spec: ToolSpec[Any, Any]) -> None:
        snapshot = self._tool_spec_fields.get(id(spec))
        if snapshot is None:
            raise AuthorityPhaseError("ToolSpec registration snapshot is missing")
        current = self._tool_spec_snapshot(spec)
        if current[0] is not snapshot[0] or not constant_time_equal(
            cast(str, current[1]), cast(str, snapshot[1])
        ):
            raise AuthorityPhaseError("ToolSpec contract identity changed")
        if current[2] is not snapshot[2] or current[3] is not snapshot[3]:
            raise AuthorityPhaseError("ToolSpec executor identity changed")
        if current[4:7] != snapshot[4:7]:
            raise AuthorityPhaseError("ToolSpec semantic identity changed")
        if current[7] is not snapshot[7] or current[8] != snapshot[8]:
            raise AuthorityPhaseError("ToolSpec binding contract identity changed")
        if not self._resolver_snapshots_match(
            cast(tuple[tuple[object, ...], ...], current[9]),
            cast(tuple[tuple[object, ...], ...], snapshot[9]),
        ):
            raise AuthorityPhaseError("ToolSpec binding resolver identity changed")

    def issue_prepared_construction_identity(
        self, authority: ToolExecutionAuthority, **_: object
    ) -> PreparedConstructionIdentity:
        with self._lock:
            self._ensure_open()
            del authority
            raise AuthorityPhaseError(
                "Prepared construction seals are private to the controlled prepare port"
            )

    prepared_construction_identity = issue_prepared_construction_identity

    def bind_new_prepared(
        self,
        prepared: PreparedToolCall[Any, Any],
        authority: ToolExecutionAuthority,
        construction_identity: PreparedConstructionIdentity,
    ) -> PreparedInstanceToken:
        with self._lock:
            self._ensure_open()
            del prepared, authority, construction_identity
            raise AuthorityPhaseError(
                "PreparedToolCall must be constructed by prepare_tool_call"
            )

    bind_prepared = bind_new_prepared

    @staticmethod
    def _prepared_shape_is_controlled(prepared: object) -> bool:
        if type(prepared) is not PreparedToolCall:
            return False
        if type(prepared.spec) is not ToolSpec or type(prepared.binding) is not BindingAudit:
            return False
        try:
            _require_text(prepared.tool_call_id, "tool_call_id")
            _require_text(prepared.spec.name, "tool_name")
            _require_digest(prepared.arguments_digest, "arguments_digest")
            _require_digest(prepared.contract_fingerprint, "contract_fingerprint")
        except (TypeError, ValueError):
            return False
        return True

    def create_application_scope_constraint(
        self,
        authority: ToolExecutionAuthority,
        *,
        mode: Literal["unrestricted", "restricted"] | None = None,
        allowed_identities: frozenset[int] | None = None,
    ) -> ApplicationScopeConstraint:
        """Create the sole constraint allowed for the authority's Scope."""

        with self._lock:
            self._authority_record(authority)
            scope = cast(Any, authority).trusted_scope
            expected_mode: Literal["unrestricted", "restricted"] = (
                "restricted" if scope.context_type == "application" else "unrestricted"
            )
            actual_mode = expected_mode if mode is None else mode
            expected_ids = (
                frozenset({cast(int, scope.context_ref)})
                if expected_mode == "restricted"
                else frozenset()
            )
            actual_ids = expected_ids if allowed_identities is None else allowed_identities
            if actual_mode != expected_mode or actual_ids != expected_ids:
                raise AuthorityPhaseError("scope constraint does not match trusted authority scope")
            constraint = ApplicationScopeConstraint(
                entity_kind="application",
                mode=actual_mode,
                allowed_identities=actual_ids,
                authority_instance_token=_authority_token(authority),
            )
            self._claim_object(constraint)
            self._constraints[id(constraint)] = (
                constraint,
                authority,
                _dataclass_snapshot(constraint),
            )
            self._objects[id(constraint)] = constraint
            return constraint

    scope_constraint = create_application_scope_constraint

    def register_scope_constraint(
        self, constraint: ApplicationScopeConstraint, authority: ToolExecutionAuthority
    ) -> ApplicationScopeConstraint:
        with self._lock:
            self.require_scope_constraint(constraint, authority)
            return constraint

    def require_scope_constraint(
        self, constraint: object, authority: ToolExecutionAuthority
    ) -> None:
        with self._lock:
            self._authority_record(authority)
            found = self._constraints.get(id(constraint))
            if found is None or found[0] is not constraint or found[1] is not authority:
                raise AuthorityPhaseError("scope constraint is not the registered object")
            _validate_snapshot(constraint, found[2], "scope constraint")
            if constraint.authority_instance_token is not _authority_token(authority):
                raise AuthorityPhaseError("scope constraint token mismatch")

    def issue_repository_binding_ticket(
        self,
        *,
        repository: object,
        session: object,
        constraint: ApplicationScopeConstraint,
        authority: ToolExecutionAuthority,
    ) -> object:
        """Issue a one-shot ticket for one exact repository binding attempt."""

        with self._lock:
            if type(self) is not AuthorityFactory:
                raise AuthorityPhaseError("repository binding requires the exact AuthorityFactory")
            self._ensure_open()
            AuthorityFactory.require_scope_constraint(self, constraint, authority)
            ticket = _RepositoryBindingTicket(
                self,
                repository=repository,
                session=session,
                constraint=constraint,
                authority=authority,
            )
            self._claim_object(ticket)
            self._repository_binding_tickets[id(ticket)] = ticket
            self._objects[id(ticket)] = ticket
            return ticket

    def revoke_repository_binding_ticket(self, ticket: object) -> None:
        """Drop an unused binding ticket after construction failure."""

        with self._lock:
            if type(self) is not AuthorityFactory:
                raise AuthorityPhaseError("repository binding requires the exact AuthorityFactory")
            self._ensure_open()
            found = self._repository_binding_tickets.get(id(ticket))
            if found is None or found is not ticket:
                raise AuthorityPhaseError("repository binding ticket is not active")
            del self._repository_binding_tickets[id(ticket)]
            self._objects.pop(id(ticket), None)
            self._drop_object(ticket)

    def register_repository_binding(
        self,
        binding: object,
        *,
        ticket: object,
        repository: object,
        session: object,
        constraint: ApplicationScopeConstraint,
        authority: ToolExecutionAuthority,
    ) -> object:
        """Register an exact Session-bound repository binding once."""

        with self._lock:
            if type(self) is not AuthorityFactory:
                raise AuthorityPhaseError("repository binding requires the exact AuthorityFactory")
            self._ensure_open()
            AuthorityFactory.require_scope_constraint(self, constraint, authority)
            ticket_record = self._repository_binding_tickets.get(id(ticket))
            if (
                ticket_record is None
                or ticket_record is not ticket
                or ticket_record.used
                or ticket_record.factory is not self
                or ticket_record.repository is not repository
                or ticket_record.session is not session
                or ticket_record.constraint is not constraint
                or ticket_record.authority is not authority
            ):
                raise AuthorityPhaseError("repository binding ticket mismatch")
            from offerpilot.repositories.session_binding import ScopedRepositoryBinding

            if type(binding) is not ScopedRepositoryBinding:
                raise AuthorityPhaseError("repository binding has an invalid concrete type")
            try:
                binding._assert_sealed()
            except AttributeError as exc:
                raise AuthorityPhaseError("repository binding seal is missing") from exc
            if (
                binding.session is not session
                or binding.constraint is not constraint
                or binding.authority is not authority
                or binding.authority_factory is not self
                or binding._ticket is not ticket
            ):
                raise AuthorityPhaseError("repository binding provenance mismatch")
            binding_id = id(binding)
            if binding_id in self._repository_bindings:
                raise AuthorityPhaseError("repository binding is already registered")
            self._claim_object(binding)
            self._repository_bindings[binding_id] = _RegisteredIdentity(
                binding,
                authority=authority,
                parent=repository,
                provenance=(session, constraint, self, ticket),
            )
            self._repository_binding_fields[binding_id] = (
                repository,
                session,
                constraint,
                authority,
                self,
                ticket,
            )
            self._objects[binding_id] = binding
            ticket_record.used = True
            del self._repository_binding_tickets[id(ticket)]
            self._objects.pop(id(ticket), None)
            self._drop_object(ticket)
            with _ACTIVE_AUTHORITIES_LOCK:
                _ACTIVE_REPOSITORY_BINDINGS[binding_id] = (binding, self)
            return binding

    def require_repository_binding(
        self,
        binding: object,
        *,
        repository: object,
        session: object,
        constraint: object,
    ) -> None:
        """Require exact binding/repository/session/constraint provenance."""

        with self._lock:
            if type(self) is not AuthorityFactory:
                raise AuthorityPhaseError("repository binding requires the exact AuthorityFactory")
            self._ensure_open()
            registration = self._repository_bindings.get(id(binding))
            if registration is None or registration.value is not binding:
                raise AuthorityPhaseError("repository binding is not active in this factory")
            expected = self._repository_binding_fields.get(id(binding))
            if expected is None:
                raise AuthorityPhaseError("repository binding snapshot is missing")
            expected_repository, expected_session, expected_constraint, expected_authority, expected_factory, expected_ticket = expected
            if (
                repository is not expected_repository
                or session is not expected_session
                or constraint is not expected_constraint
            ):
                raise AuthorityPhaseError("repository binding caller provenance mismatch")
            self._authority_record(expected_authority)
            AuthorityFactory.require_scope_constraint(
                self, expected_constraint, cast(ToolExecutionAuthority, expected_authority)
            )
            from offerpilot.repositories.session_binding import ScopedRepositoryBinding

            if type(binding) is not ScopedRepositoryBinding:
                raise AuthorityPhaseError("repository binding has an invalid concrete type")
            try:
                binding._assert_sealed()
            except AttributeError as exc:
                raise AuthorityPhaseError("repository binding seal is missing") from exc
            if not self._repository_binding_snapshot_matches(binding, expected):
                raise AuthorityPhaseError("repository binding snapshot changed")

    @staticmethod
    def _repository_binding_snapshot_matches(
        binding: object,
        expected: tuple[object, ...],
    ) -> bool:
        expected_repository, expected_session, expected_constraint, expected_authority, expected_factory, expected_ticket = expected
        try:
            from offerpilot.repositories.session_binding import ScopedRepositoryBinding

            if type(binding) is not ScopedRepositoryBinding:
                return False
            binding._assert_sealed()
            actual = (
                expected_repository,
                binding.session,
                binding.constraint,
                binding.authority,
                binding.authority_factory,
                binding._ticket,
            )
        except (AttributeError, AuthorityPhaseError):
            return False
        return (
            actual[0] is expected_repository
            and actual[1] is expected_session
            and actual[2] is expected_constraint
            and actual[3] is expected_authority
            and actual[4] is expected_factory
            and actual[5] is expected_ticket
        )

    def _repository_binding_sources_match(self, expected: tuple[object, ...]) -> bool:
        """Return whether a binding's registered authority and constraint remain valid."""

        if type(self) is not AuthorityFactory:
            return False
        _, _, expected_constraint, expected_authority, _, _ = expected
        try:
            self._authority_record(expected_authority)
            AuthorityFactory.require_scope_constraint(
                self,
                expected_constraint,
                cast(ToolExecutionAuthority, expected_authority),
            )
        except AuthorityPhaseError:
            return False
        return True


    def create_binding_target_resolution(
        self,
        authority: ToolExecutionAuthority,
        *,
        entity_kind: str,
        state: Literal["resolved", "omitted", "detached", "unavailable"],
        identity: int | None,
    ) -> BindingTargetResolution:
        with self._lock:
            self._authority_record(authority)
            resolution = BindingTargetResolution(
                entity_kind=cast(Literal["application", "resume"], entity_kind),
                state=state,
                identity=identity,
                authority_instance_token=_authority_token(authority),
            )
            self._claim_object(resolution)
            self._resolutions[id(resolution)] = (
                resolution,
                authority,
                _dataclass_snapshot(resolution),
            )
            self._objects[id(resolution)] = resolution
            return resolution

    binding_target_resolution = create_binding_target_resolution

    def register_binding_target_resolution(
        self, resolution: BindingTargetResolution, authority: ToolExecutionAuthority
    ) -> BindingTargetResolution:
        with self._lock:
            self.require_binding_target_resolution(resolution, authority)
            return resolution

    def require_binding_target_resolution(
        self, resolution: object, authority: ToolExecutionAuthority
    ) -> None:
        with self._lock:
            self._authority_record(authority)
            found = self._resolutions.get(id(resolution))
            if found is None or found[0] is not resolution or found[1] is not authority:
                raise AuthorityPhaseError("binding resolution is not the registered object")
            _validate_snapshot(resolution, found[2], "binding resolution")
            if resolution.authority_instance_token is not _authority_token(authority):
                raise AuthorityPhaseError("binding resolution token mismatch")

    def prepared_token(self, prepared: object) -> PreparedInstanceToken:
        with self._lock:
            self._ensure_open()
            if type(prepared) is PreparedInstanceToken:
                for original, token, _ in self._prepared.values():
                    if token is prepared:
                        return prepared
                raise AuthorityPhaseError("PreparedToolCall token is not active")
            found = self._prepared.get(id(prepared))
            if found is None or found[0] is not prepared:
                raise AuthorityPhaseError("PreparedToolCall is not registered")
            return found[1]

    def _prepared_record(
        self, prepared: object
    ) -> tuple[PreparedToolCall[Any, Any], PreparedInstanceToken, ToolExecutionAuthority]:
        self.prepared_token(prepared)
        found = self._prepared[id(prepared)]
        if found[0] is not prepared:
            raise AuthorityPhaseError("PreparedToolCall identity mismatch")
        fields = self._prepared_fields.get(id(prepared))
        if fields is None:
            raise AuthorityPhaseError("PreparedToolCall construction record is missing")
        original = cast(PreparedToolCall[Any, Any], prepared)
        current = (
            original.tool_call_id,
            original.spec.name,
            original.arguments_digest,
            original.contract_fingerprint,
        )
        if current[:2] != fields[:2] or not constant_time_equal(
            current[2], cast(str, fields[2])
        ):
            raise AuthorityPhaseError("PreparedToolCall semantic identity changed")
        if not constant_time_equal(current[3], cast(str, fields[3])):
            raise AuthorityPhaseError("PreparedToolCall contract identity changed")
        if (
            original.spec is not fields[4]
            or original.binding is not fields[5]
            or original._replacement_guard is not fields[6]
        ):
            raise AuthorityPhaseError("PreparedToolCall object identity changed")
        self._validate_registered_tool_spec(original.spec)
        _validate_snapshot(original.binding, cast(Mapping[str, object], fields[7]), "Prepared binding")
        current_arguments_digest = _canonical_arguments_digest(original.arguments)
        if not constant_time_equal(current_arguments_digest, cast(str, fields[2])):
            raise AuthorityPhaseError("Prepared arguments changed")
        current_typed_args_digest = _canonical_arguments_digest(original.typed_args)
        if not constant_time_equal(
            current_typed_args_digest,
            cast(str, fields[8]),
        ):
            raise AuthorityPhaseError("Prepared typed arguments changed")
        if original.authority_instance_token is not _authority_token(found[2]):
            raise AuthorityPhaseError("PreparedToolCall authority identity changed")
        return (original, found[1], found[2])

    def require_prepared_call(
        self,
        prepared: PreparedToolCall[Any, Any],
        authority: ToolExecutionAuthority,
    ) -> None:
        """Revalidate an exact Prepared object immediately before execution."""

        with self._lock:
            record = self._prepared_record(prepared)
            if record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")

    def _prepared_origin(
        self,
        prepared: PreparedToolCall[Any, Any],
    ) -> NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity:
        origin = self._prepared_origins.get(id(prepared))
        if origin is None:
            raise AuthorityPhaseError("PreparedToolCall origin provenance is missing")
        return origin

    @staticmethod
    def _require_prepared_call_fields(
        prepared: PreparedToolCall[Any, Any],
        *,
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
    ) -> None:
        """Constant-time compare every call identity against the Prepared original."""

        if tool_call_id != prepared.tool_call_id:
            raise AuthorityPhaseError("tool_call_id does not match PreparedToolCall")
        if tool_name != prepared.spec.name:
            raise AuthorityPhaseError("tool_name does not match PreparedToolCall spec")
        _require_digest(arguments_digest, "arguments_digest")
        if not constant_time_equal(arguments_digest, prepared.arguments_digest):
            raise AuthorityPhaseError("arguments digest does not match PreparedToolCall")

    @staticmethod
    def _require_prepared_phase(
        prepared: PreparedToolCall[Any, Any],
        *,
        kind: Literal["read", "write"],
        confirmation_policy: Literal["none", "required"],
    ) -> None:
        """Enforce the phase/spec pairing at every factory port.

        The public ``require_authority_spec`` gate is useful for callers, but
        execution ports must not rely on callers invoking it first.  Checking
        the exact registered Prepared spec here keeps wrong-phase requests
        fail-closed before pending/claim/identity state can be mutated.
        """

        if (
            prepared.spec.kind != kind
            or prepared.spec.confirmation_policy != confirmation_policy
        ):
            raise AuthorityPhaseError("PreparedToolCall spec is not valid for this phase")

    def _register_call(
        self, call: AuthorityCallIdentity, authority: ToolExecutionAuthority, use: AuthorityUse
    ) -> AuthorityCallIdentity:
        self._authority_record(authority)
        self._claim_object(call)
        self._calls[id(call)] = (call, authority, use.value)
        self._call_fields[id(call)] = {
            field.name: getattr(call, field.name)
            for field in dataclass_fields(cast(Any, call))
        }
        self._objects[id(call)] = call
        return call

    def _register_identity(
        self,
        table: dict[int, _RegisteredIdentity],
        value: object,
        *,
        authority: ToolExecutionAuthority | None = None,
        fingerprint: str | None = None,
        candidate_count: int | None = None,
        candidate_ordinal: int | None = None,
        parent: object | None = None,
        provenance: tuple[object, ...] = (),
        semantic: Mapping[str, object] | None = None,
    ) -> object:
        self._ensure_open()
        if value is None or isinstance(
            value, (str, bytes, int, float, bool, tuple, frozenset, _OpaqueHandle)
        ):
            raise AuthorityPhaseError("opaque runtime identity must be a caller-owned object")
        if authority is not None:
            self._authority_record(authority)
        existing = table.get(id(value))
        if existing is not None:
            if existing.value is not value:
                raise AuthorityPhaseError("identity object address was reused")
            if authority is not None and existing.authority not in {None, authority}:
                raise AuthorityPhaseError("identity belongs to another authority")
            if fingerprint is not None and (
                existing.fingerprint is None
                or not constant_time_equal(existing.fingerprint, fingerprint)
            ):
                raise AuthorityPhaseError("identity fingerprint changed")
            if parent is not None and existing.parent is not parent:
                raise AuthorityPhaseError("identity parent changed")
            if len(existing.provenance) != len(provenance) or any(
                current is not expected
                for current, expected in zip(existing.provenance, provenance)
            ):
                raise AuthorityPhaseError("identity provenance changed")
            if semantic is not None and existing.semantic != dict(semantic):
                raise AuthorityPhaseError("identity semantic fields changed")
            if existing.authority is None:
                existing.authority = authority
            return value
        self._claim_object(value)
        table[id(value)] = _RegisteredIdentity(
            value,
            authority=authority,
            fingerprint=fingerprint,
            candidate_count=candidate_count,
            candidate_ordinal=candidate_ordinal,
            parent=parent,
            provenance=provenance,
            semantic=semantic,
        )
        self._objects[id(value)] = value
        return value

    def _registered_identity(
        self,
        table: dict[int, _RegisteredIdentity],
        value: object,
        *,
        authority: ToolExecutionAuthority | None = None,
    ) -> _RegisteredIdentity:
        found = table.get(id(value))
        if found is None or found.value is not value:
            raise AuthorityPhaseError("runtime identity is not registered in this scope")
        if authority is not None and found.authority is not authority:
            raise AuthorityPhaseError("runtime identity belongs to another authority")
        if found.semantic:
            for name, expected in found.semantic.items():
                current = getattr(value, name, None)
                if name == "operation_id" and current is None:
                    current = getattr(value, "id", None)
                if name.endswith("digest") or name.endswith("fingerprint"):
                    if not isinstance(current, str) or not isinstance(expected, str):
                        raise AuthorityPhaseError("runtime identity semantic fields changed")
                    if not constant_time_equal(current, expected):
                        raise AuthorityPhaseError("runtime identity semantic fields changed")
                elif current != expected:
                    raise AuthorityPhaseError("runtime identity semantic fields changed")
        return found

    def _registered_authority_identity(
        self,
        table: dict[int, _RegisteredIdentity],
        value: object,
        authority: ToolExecutionAuthority,
        field_name: str,
    ) -> _RegisteredIdentity:
        found = self._registered_identity(table, value, authority=authority)
        if found.authority is not authority:
            raise AuthorityPhaseError(f"{field_name} was not registered for this authority")
        return found

    def _provider_build_for_registration(
        self,
        build_identity: object,
        authority: ToolExecutionAuthority,
    ) -> ProviderSurfaceBuildIdentity:
        if type(build_identity) is not ProviderSurfaceBuildIdentity:
            raise AuthorityPhaseError("provider registration requires the exact build identity")
        build_authority = self._call_authority(
            build_identity,
            AuthorityUse.PROVIDER_SURFACE_BUILD,
        )
        if build_authority is not authority:
            raise AuthorityPhaseError("provider build belongs to another authority")
        return build_identity

    @staticmethod
    def _same_provenance(
        actual: tuple[object, ...], expected: tuple[object, ...]
    ) -> bool:
        return len(actual) == len(expected) and all(
            current is expected_value
            for current, expected_value in zip(actual, expected)
        )

    def _reject_alternate_provenance(
        self,
        table: dict[int, _RegisteredIdentity],
        build_identity: ProviderSurfaceBuildIdentity,
        value: object,
        label: str,
    ) -> None:
        for registration in table.values():
            if self._same_provenance(registration.provenance, (build_identity,)) and (
                registration.value is not value
            ):
                raise AuthorityPhaseError(f"alternate {label} for this model call is forbidden")

    def _reject_alternate_build_provenance(
        self,
        table: dict[int, _RegisteredIdentity],
        build_identity: ProviderSurfaceBuildIdentity,
        value: object,
        label: str,
    ) -> None:
        for registration in table.values():
            if registration.provenance and registration.provenance[0] is build_identity and (
                registration.value is not value
            ):
                raise AuthorityPhaseError(f"alternate {label} for this model call is forbidden")

    def register_runner_invocation(
        self, value: object, authority: ToolExecutionAuthority | None = None
    ) -> object:
        with self._lock:
            self._ensure_open()
            return self._register_identity(self._runner_invocations, value, authority=authority)

    register_runner_identity = register_runner_invocation

    def register_tool_execution_context(
        self, value: object, authority: ToolExecutionAuthority | None = None
    ) -> object:
        with self._lock:
            self._ensure_open()
            return self._register_identity(self._tool_contexts, value, authority=authority)

    register_tool_context = register_tool_execution_context
    register_context = register_tool_execution_context

    def register_frozen_surface(
        self,
        value: object,
        *,
        surface_fingerprint: str,
        candidate_count: int = 1,
        authority: ToolExecutionAuthority,
        build_identity: ProviderSurfaceBuildIdentity | None = None,
    ) -> object:
        with self._lock:
            self._ensure_open()
            if type(authority) not in {SegmentExecutionAuthority, ApprovalExecutionAuthority}:
                raise AuthorityPhaseError("surface registration requires an authority")
            if build_identity is None:
                raise AuthorityPhaseError("surface registration requires a build identity")
            build = self._provider_build_for_registration(build_identity, authority)
            self._reject_alternate_provenance(self._surfaces, build, value, "surface")
            _require_digest(surface_fingerprint, "surface_fingerprint")
            require_positive_int64(candidate_count, "candidate_count")
            return self._register_identity(
                self._surfaces,
                value,
                authority=authority,
                fingerprint=surface_fingerprint,
                candidate_count=candidate_count,
                provenance=(build,),
            )

    register_surface = register_frozen_surface
    register_provider_surface = register_frozen_surface

    def register_model_call_surface_binding(
        self,
        value: object,
        *,
        surface: object,
        surface_fingerprint: str,
        authority: ToolExecutionAuthority,
        build_identity: ProviderSurfaceBuildIdentity | None = None,
    ) -> object:
        with self._lock:
            self._ensure_open()
            if type(authority) not in {SegmentExecutionAuthority, ApprovalExecutionAuthority}:
                raise AuthorityPhaseError("binding registration requires an authority")
            if build_identity is None:
                raise AuthorityPhaseError("binding registration requires a build identity")
            build = self._provider_build_for_registration(build_identity, authority)
            _require_digest(surface_fingerprint, "surface_fingerprint")
            surface_registration = self._surfaces.get(id(surface))
            if surface_registration is None or surface_registration.value is not surface:
                raise AuthorityPhaseError("Frozen surface must be registered first")
            if surface_registration.authority is not authority:
                raise AuthorityPhaseError("surface belongs to another authority")
            if not self._same_provenance(surface_registration.provenance, (build,)):
                raise AuthorityPhaseError("surface belongs to another model call")
            if surface_registration.fingerprint is None or not constant_time_equal(
                surface_registration.fingerprint,
                surface_fingerprint,
            ):
                raise AuthorityPhaseError("binding surface fingerprint mismatch")
            self._reject_alternate_provenance(self._bindings, build, value, "surface binding")
            return self._register_identity(
                self._bindings,
                value,
                authority=authority,
                fingerprint=surface_fingerprint,
                parent=surface,
                provenance=(build,),
            )

    register_surface_binding = register_model_call_surface_binding
    register_binding = register_model_call_surface_binding

    def register_gateway_session(
        self,
        value: object,
        authority: ToolExecutionAuthority,
        *,
        build_identity: ProviderSurfaceBuildIdentity | None = None,
        surface: object | None = None,
        surface_fingerprint: str | None = None,
        model_call_surface_binding: object | None = None,
    ) -> object:
        with self._lock:
            self._ensure_open()
            if type(authority) not in {SegmentExecutionAuthority, ApprovalExecutionAuthority}:
                raise AuthorityPhaseError("gateway registration requires an authority")
            if build_identity is None:
                raise AuthorityPhaseError("gateway registration requires a build identity")
            build = self._provider_build_for_registration(build_identity, authority)
            if surface is None or surface_fingerprint is None or model_call_surface_binding is None:
                raise AuthorityPhaseError(
                    "gateway registration requires exact surface and binding provenance"
                )
            _require_digest(surface_fingerprint, "surface_fingerprint")
            surface_record = self._registered_authority_identity(
                self._surfaces,
                surface,
                authority,
                "surface",
            )
            if not self._same_provenance(surface_record.provenance, (build,)):
                raise AuthorityPhaseError("gateway surface provenance does not match build")
            if surface_record.fingerprint is None or not constant_time_equal(
                surface_record.fingerprint,
                surface_fingerprint,
            ):
                raise AuthorityPhaseError("gateway surface fingerprint mismatch")
            binding_record = self._registered_authority_identity(
                self._bindings,
                model_call_surface_binding,
                authority,
                "model call surface binding",
            )
            if (
                binding_record.parent is not surface
                or not self._same_provenance(binding_record.provenance, (build,))
                or binding_record.fingerprint is None
                or not constant_time_equal(binding_record.fingerprint, surface_fingerprint)
            ):
                raise AuthorityPhaseError("gateway binding provenance does not match surface")
            self._reject_alternate_build_provenance(
                self._gateway_sessions,
                build,
                value,
                "gateway session",
            )
            return self._register_identity(
                self._gateway_sessions,
                value,
                authority=authority,
                fingerprint=surface_fingerprint,
                parent=surface,
                provenance=(build, model_call_surface_binding),
            )

    register_gateway = register_gateway_session
    register_session = register_gateway_session

    def register_operation(
        self, value: object, authority: ToolExecutionAuthority | None = None
    ) -> object:
        """Register a trusted Operation object and freeze only safe proof fields."""

        with self._lock:
            self._ensure_open()
            if value is None or isinstance(
                value, (str, bytes, int, float, bool, tuple, frozenset)
            ):
                raise AuthorityPhaseError("Operation must be a caller-owned object identity")
            required = (
                "status",
                "adapter_kind",
                "tool_call_id",
                "tool_name",
                "proposal_fingerprint",
                "confirmation_token_fingerprint",
                "pending_confirmation_claim_id",
            )
            semantic: dict[str, object] = {}
            operation_id = getattr(value, "operation_id", None)
            if operation_id is None:
                operation_id = getattr(value, "id", None)
            if type(operation_id) is not str or not operation_id:
                raise AuthorityPhaseError("Operation identity is incomplete")
            semantic["operation_id"] = operation_id
            for name in required:
                field_value = getattr(value, name, None)
                if name == "status":
                    if type(field_value) is not str or field_value != "proposed":
                        raise AuthorityPhaseError("Operation must be proposed")
                elif name in {"proposal_fingerprint", "confirmation_token_fingerprint"}:
                    _require_hmac_digest(field_value, name)
                else:
                    _require_text(field_value, name)
                semantic[name] = field_value
            conversation_id = getattr(value, "conversation_id", None)
            require_positive_int64(conversation_id, "conversation_id")
            semantic["conversation_id"] = conversation_id
            return self._register_identity(
                self._operations,
                value,
                authority=authority,
                semantic=semantic,
            )

    def register_transaction(
        self, value: object, authority: ToolExecutionAuthority | None = None
    ) -> object:
        with self._lock:
            self._ensure_open()
            return self._register_identity(self._transactions, value, authority=authority)

    register_operation_identity = register_operation
    register_transaction_identity = register_transaction

    def issue_provider_attempt(
        self,
        invocation_identity: ProviderInvocationIdentity,
        *,
        candidate_ordinal: int,
    ) -> str:
        with self._lock:
            authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
            surface = self._surfaces.get(id(invocation_identity.surface))
            if surface is None or surface.value is not invocation_identity.surface:
                raise AuthorityPhaseError("Provider surface is not registered")
            require_nonnegative_int64(candidate_ordinal, "candidate_ordinal")
            if surface.candidate_count is None or candidate_ordinal >= surface.candidate_count:
                raise AuthorityPhaseError("candidate ordinal is outside frozen surface")
            self._attempt_sequence += 1
            attempt_id = f"attempt-{uuid4().hex}"
            self._claim_object(attempt_id)
            self._attempts[attempt_id] = _RegisteredIdentity(
                attempt_id,
                authority=authority,
                fingerprint=invocation_identity.surface_fingerprint,
                candidate_ordinal=candidate_ordinal,
                parent=invocation_identity,
                semantic={"gateway_session": invocation_identity.gateway_session},
            )
            self._objects[id(attempt_id)] = attempt_id
            return attempt_id

    issue_gateway_attempt = issue_provider_attempt

    def validate_provider_attempt(
        self,
        invocation_identity: ProviderInvocationIdentity,
        *,
        attempt_id: str,
        candidate_ordinal: int,
        gateway_session: object,
    ) -> None:
        """Validate the exact session-issued attempt immediately before Provider I/O."""

        with self._lock:
            authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
            _require_text(attempt_id, "attempt_id")
            require_nonnegative_int64(candidate_ordinal, "candidate_ordinal")
            if invocation_identity.gateway_session is not gateway_session:
                raise AuthorityPhaseError("attempt Gateway Session mismatch")
            surface = self._registered_authority_identity(
                self._surfaces,
                invocation_identity.surface,
                authority,
                "surface",
            )
            if surface.candidate_count is None or candidate_ordinal >= surface.candidate_count:
                raise AuthorityPhaseError("candidate ordinal is outside frozen surface")
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.value != attempt_id:
                raise AuthorityPhaseError("attempt was not issued by this factory")
            if (
                attempt.authority is not authority
                or attempt.parent is not invocation_identity
                or attempt.candidate_ordinal != candidate_ordinal
                or attempt.semantic.get("gateway_session") is not gateway_session
                or attempt.fingerprint is None
                or not constant_time_equal(
                    attempt.fingerprint,
                    invocation_identity.surface_fingerprint,
                )
            ):
                raise AuthorityPhaseError("attempt does not match Provider invocation")

    validate_gateway_attempt = validate_provider_attempt

    def create_provider_surface_build_identity(
        self,
        authority: SegmentExecutionAuthority,
        *,
        runner_invocation: object,
        tool_context: object,
        model_call_id: str,
    ) -> ProviderSurfaceBuildIdentity:
        with self._lock:
            self._segment_record(authority)
            self._registered_authority_identity(
                self._runner_invocations, runner_invocation, authority, "runner invocation"
            )
            self._registered_authority_identity(
                self._tool_contexts, tool_context, authority, "tool context"
            )
            _require_text(model_call_id, "model_call_id")
            call = ProviderSurfaceBuildIdentity(
                authority_instance_token=authority.authority_instance_token,
                runner_invocation=runner_invocation,
                segment_id=authority.segment_id,
                tool_context=tool_context,
                model_call_id=model_call_id,
            )
            return cast(
                ProviderSurfaceBuildIdentity,
                self._register_call(call, authority, AuthorityUse.PROVIDER_SURFACE_BUILD),
            )

    provider_surface_build_identity = create_provider_surface_build_identity

    def create_provider_invocation_identity(
        self,
        build_identity: ProviderSurfaceBuildIdentity,
        *,
        surface: object,
        surface_fingerprint: str,
        model_call_surface_binding: object,
        gateway_session: object,
    ) -> ProviderInvocationIdentity:
        with self._lock:
            authority = self._call_authority(build_identity, AuthorityUse.PROVIDER_SURFACE_BUILD)
            if not isinstance(authority, SegmentExecutionAuthority):
                raise AuthorityPhaseError("Segment authority is required")
            self._registered_authority_identity(
                self._runner_invocations, build_identity.runner_invocation, authority, "runner invocation"
            )
            self._registered_authority_identity(
                self._tool_contexts, build_identity.tool_context, authority, "tool context"
            )
            _require_digest(surface_fingerprint, "surface_fingerprint")
            surface_record = self._registered_authority_identity(
                self._surfaces, surface, authority, "surface"
            )
            if not self._same_provenance(surface_record.provenance, (build_identity,)):
                raise AuthorityPhaseError("surface provenance does not match build identity")
            if surface_record.fingerprint is None or not constant_time_equal(
                surface_record.fingerprint, surface_fingerprint
            ):
                raise AuthorityPhaseError("surface fingerprint does not match frozen surface")
            binding_record = self._registered_authority_identity(
                self._bindings,
                model_call_surface_binding,
                authority,
                "model call surface binding",
            )
            if (
                binding_record.parent is not surface
                or not self._same_provenance(binding_record.provenance, (build_identity,))
                or binding_record.fingerprint is None
                or not constant_time_equal(binding_record.fingerprint, surface_fingerprint)
            ):
                raise AuthorityPhaseError("surface binding does not match frozen surface")
            gateway_record = self._registered_authority_identity(
                self._gateway_sessions, gateway_session, authority, "gateway session"
            )
            if (
                gateway_record.parent is not surface
                or len(gateway_record.provenance) != 2
                or gateway_record.provenance[0] is not build_identity
                or gateway_record.provenance[1] is not model_call_surface_binding
                or gateway_record.fingerprint is None
                or not constant_time_equal(gateway_record.fingerprint, surface_fingerprint)
            ):
                raise AuthorityPhaseError("gateway provenance does not match build identity")
            call = ProviderInvocationIdentity(
                authority_instance_token=authority.authority_instance_token,
                runner_invocation=build_identity.runner_invocation,
                segment_id=build_identity.segment_id,
                tool_context=build_identity.tool_context,
                model_call_id=build_identity.model_call_id,
                surface=surface,
                surface_fingerprint=surface_fingerprint,
                model_call_surface_binding=model_call_surface_binding,
                gateway_session=gateway_session,
            )
            return cast(
                ProviderInvocationIdentity,
                self._register_call(call, authority, AuthorityUse.PROVIDER_INVOKE),
            )

    provider_invocation_identity = create_provider_invocation_identity

    def create_new_turn_prepare_identity(
        self,
        invocation_identity: ProviderInvocationIdentity,
        *,
        attempt_id: str,
        candidate_ordinal: int,
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
    ) -> NewTurnPrepareCallIdentity:
        with self._lock:
            authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
            _require_text(attempt_id, "attempt_id")
            attempt_record = self._attempts.get(attempt_id)
            if attempt_record is None or attempt_record.value != attempt_id:
                raise AuthorityPhaseError("attempt was not issued by this factory")
            if attempt_record.authority is not authority or attempt_record.parent is not invocation_identity:
                raise AuthorityPhaseError("attempt does not belong to this Gateway invocation")
            if attempt_record.fingerprint is None or not constant_time_equal(
                attempt_record.fingerprint, invocation_identity.surface_fingerprint
            ):
                raise AuthorityPhaseError("attempt surface fingerprint mismatch")
            if attempt_record.candidate_ordinal != candidate_ordinal:
                raise AuthorityPhaseError("attempt candidate ordinal mismatch")
            require_nonnegative_int64(candidate_ordinal, "candidate_ordinal")
            _require_text(tool_call_id, "tool_call_id")
            _require_text(tool_name, "tool_name")
            _require_digest(arguments_digest, "arguments_digest")
            call = NewTurnPrepareCallIdentity(
                authority_instance_token=_authority_token(authority),
                runner_invocation=invocation_identity.runner_invocation,
                segment_id=invocation_identity.segment_id,
                tool_context=invocation_identity.tool_context,
                model_call_id=invocation_identity.model_call_id,
                surface=invocation_identity.surface,
                surface_fingerprint=invocation_identity.surface_fingerprint,
                model_call_surface_binding=invocation_identity.model_call_surface_binding,
                gateway_session=invocation_identity.gateway_session,
                attempt_id=attempt_id,
                candidate_ordinal=candidate_ordinal,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            return cast(
                NewTurnPrepareCallIdentity,
                self._register_call(call, authority, AuthorityUse.NEW_TURN_PREPARE),
            )

    new_turn_prepare_identity = create_new_turn_prepare_identity

    def create_read_execution_identity(
        self,
        invocation_identity: ProviderInvocationIdentity,
        *,
        prepared: PreparedToolCall[Any, Any],
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
    ) -> ReadExecutionCallIdentity:
        with self._lock:
            authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
            prepared_record = self._prepared_record(prepared)
            self._require_prepared_phase(
                prepared,
                kind="read",
                confirmation_policy="none",
            )
            self._require_prepared_call_fields(
                prepared,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            if prepared_record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
            origin = self._prepared_origin(prepared)
            if type(origin) is not NewTurnPrepareCallIdentity:
                raise AuthorityPhaseError("read PreparedToolCall has an invalid origin phase")
            if self._call_authority(origin, AuthorityUse.NEW_TURN_PREPARE) is not authority:
                raise AuthorityPhaseError("read PreparedToolCall origin belongs to another authority")
            self._require_read_origin_provenance(origin, invocation_identity)
            call = ReadExecutionCallIdentity(
                authority_instance_token=_authority_token(authority),
                runner_invocation=invocation_identity.runner_invocation,
                segment_id=invocation_identity.segment_id,
                tool_context=invocation_identity.tool_context,
                model_call_id=invocation_identity.model_call_id,
                surface=invocation_identity.surface,
                surface_fingerprint=invocation_identity.surface_fingerprint,
                model_call_surface_binding=invocation_identity.model_call_surface_binding,
                prepared=prepared,
                prepared_instance_token=prepared_record[1],
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            return cast(
                ReadExecutionCallIdentity,
                self._register_call(call, authority, AuthorityUse.READ_EXECUTE),
            )

    read_execution_identity = create_read_execution_identity

    @staticmethod
    def _require_read_origin_provenance(
        origin: NewTurnPrepareCallIdentity,
        invocation: ProviderInvocationIdentity,
    ) -> None:
        if (
            origin.runner_invocation is not invocation.runner_invocation
            or origin.segment_id != invocation.segment_id
            or origin.tool_context is not invocation.tool_context
            or origin.model_call_id != invocation.model_call_id
            or origin.surface is not invocation.surface
            or origin.model_call_surface_binding
            is not invocation.model_call_surface_binding
            or origin.gateway_session is not invocation.gateway_session
        ):
            raise AuthorityPhaseError(
                "read execution does not match PreparedToolCall origin provenance"
            )
        if not constant_time_equal(
            origin.surface_fingerprint,
            invocation.surface_fingerprint,
        ):
            raise AuthorityPhaseError(
                "read execution surface fingerprint does not match PreparedToolCall origin"
            )

    def create_typed_pending_identity(
        self,
        *,
        authority: SegmentExecutionAuthority,
        runner_invocation: object,
        tool_context: object,
        prepared: PreparedToolCall[Any, Any],
        pending: object,
        operation_id: str,
        pending_action_revision: int,
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
    ) -> TypedPendingCallIdentity:
        with self._lock:
            self._segment_record(authority)
            self._registered_authority_identity(
                self._runner_invocations, runner_invocation, authority, "runner invocation"
            )
            self._registered_authority_identity(
                self._tool_contexts, tool_context, authority, "tool context"
            )
            prepared_record = self._prepared_record(prepared)
            self._require_prepared_phase(
                prepared,
                kind="write",
                confirmation_policy="required",
            )
            self._require_prepared_call_fields(
                prepared,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            if prepared_record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
            pending_record = self._pending_record_for_object(pending)
            self._require_pending_semantic(
                pending_record,
                conversation_id=authority.conversation_id,
                operation_id=operation_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                pending_action_revision=pending_action_revision,
                arguments_digest=arguments_digest,
            )
            self._claim_pending_owner(pending_record, authority)
            pending_token = pending_record.token
            call = TypedPendingCallIdentity(
                authority_instance_token=authority.authority_instance_token,
                runner_invocation=runner_invocation,
                segment_id=authority.segment_id,
                tool_context=tool_context,
                prepared=prepared,
                prepared_instance_token=prepared_record[1],
                operation_id=operation_id,
                pending_identity=pending_token,
                pending_action_revision=pending_action_revision,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            return cast(
                TypedPendingCallIdentity,
                self._register_call(call, authority, AuthorityUse.TYPED_PENDING_CLAIM),
            )

    typed_pending_identity = create_typed_pending_identity

    def create_approved_write_prepare_identity(
        self,
        authority: ApprovalExecutionAuthority,
        *,
        approval_context: object,
        request_identity: object,
    ) -> ApprovedWritePrepareCallIdentity:
        with self._lock:
            self._approval_record(authority)
            self._pending_record_for_token(authority.pending_identity)
            call = ApprovedWritePrepareCallIdentity(
                approval_authority_instance_token=authority.authority_instance_token,
                approval_context=_opaque_identity(approval_context, "approval_context"),
                request_identity=_opaque_identity(request_identity, "request_identity"),
                operation_id=authority.operation_id,
                pending_identity=authority.pending_identity,
                pending_action_revision=authority.pending_action_revision,
                tool_call_id=authority.tool_call_id,
                tool_name=authority.tool_name,
                effective_args_digest=authority.effective_args_digest,
            )
            return cast(
                ApprovedWritePrepareCallIdentity,
                self._register_call(call, authority, AuthorityUse.APPROVED_WRITE_PREPARE),
            )

    approved_write_prepare_identity = create_approved_write_prepare_identity

    def create_approved_write_execute_identity(
        self,
        prepare_identity: ApprovedWritePrepareCallIdentity,
        *,
        prepared: PreparedToolCall[Any, Any],
        execution_claim: ExecutionClaim,
    ) -> ApprovedWriteExecuteCallIdentity:
        with self._lock:
            authority = self._call_authority(prepare_identity, AuthorityUse.APPROVED_WRITE_PREPARE)
            if not isinstance(authority, ApprovalExecutionAuthority):
                raise AuthorityPhaseError("Approval authority is required")
            prepared_record = self._prepared_record(prepared)
            if prepared_record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
            self._require_prepared_phase(
                prepared,
                kind="write",
                confirmation_policy="required",
            )
            claim_lifecycle = self._claim_lifecycle(execution_claim)
            claim_value = cast(ExecutionClaim, claim_lifecycle.value)
            if (
                claim_lifecycle.authority is not authority
                or claim_lifecycle.prepared is not prepared
                or claim_value.transaction is None
            ):
                raise AuthorityPhaseError("ExecutionClaim is not bound to this authority/prepared call")
            self._require_prepared_call_fields(
                prepared,
                tool_call_id=prepare_identity.tool_call_id,
                tool_name=prepare_identity.tool_name,
                arguments_digest=prepare_identity.effective_args_digest,
            )
            call = ApprovedWriteExecuteCallIdentity(
                approval_authority_instance_token=authority.authority_instance_token,
                approval_context=prepare_identity.approval_context,
                request_identity=prepare_identity.request_identity,
                operation_id=prepare_identity.operation_id,
                pending_identity=prepare_identity.pending_identity,
                pending_action_revision=prepare_identity.pending_action_revision,
                tool_call_id=prepare_identity.tool_call_id,
                tool_name=prepare_identity.tool_name,
                effective_args_digest=prepare_identity.effective_args_digest,
                prepared=prepared,
                prepared_instance_token=prepared_record[1],
                execution_claim=execution_claim,
                execution_claim_instance_token=execution_claim.execution_claim_instance_token,
            )
            return cast(
                ApprovedWriteExecuteCallIdentity,
                self._register_call(call, authority, AuthorityUse.APPROVED_WRITE_EXECUTE),
            )

    approved_write_execute_identity = create_approved_write_execute_identity

    def _call_authority(
        self, call: AuthorityCallIdentity, expected_use: AuthorityUse
    ) -> ToolExecutionAuthority:
        with self._lock:
            self._ensure_open()
            found = self._call_record(call)
            if found[2] != expected_use.value:
                raise AuthorityPhaseError("call identity is not active for this phase")
            return found[1]

    def _call_record(self, call: object) -> tuple[AuthorityCallIdentity, ToolExecutionAuthority, str]:
        with self._lock:
            found = self._calls.get(id(call))
            if found is None or found[0] is not call:
                raise AuthorityPhaseError("call identity is not active")
            snapshot = self._call_fields.get(id(call))
            if snapshot is None:
                raise AuthorityPhaseError("call identity snapshot is missing")
            for name, expected in snapshot.items():
                current = getattr(call, name, None)
                if name.endswith("digest") or name.endswith("fingerprint"):
                    if not isinstance(current, str) or not isinstance(expected, str):
                        raise AuthorityPhaseError("call identity semantic fields changed")
                    if not constant_time_equal(current, expected):
                        raise AuthorityPhaseError("call identity semantic fields changed")
                elif type(expected) in {str, int, bool, type(None)}:
                    if current != expected:
                        raise AuthorityPhaseError("call identity semantic fields changed")
                elif current is not expected:
                    raise AuthorityPhaseError("call identity object identity changed")
            return found

    @staticmethod
    def _claim_key(
        kind: str,
        authority: ToolExecutionAuthority,
        prepared: object,
        pending: object,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
        digest: str,
        revision: int,
    ) -> tuple[object, ...]:
        return (
            kind,
            id(authority),
            id(prepared),
            id(pending),
            operation_id,
            tool_call_id,
            tool_name,
            digest,
            revision,
        )

    def _drop_claim_key(self, claim_id: int) -> None:
        for key, active_id in tuple(self._claim_keys.items()):
            if active_id == claim_id:
                del self._claim_keys[key]

    def _drop_proof_key(self, proof_id: int) -> None:
        for key, active_id in tuple(self._proof_keys.items()):
            if active_id == proof_id:
                del self._proof_keys[key]

    def issue_pending_claim(
        self,
        authority: SegmentExecutionAuthority,
        *,
        prepared: PreparedToolCall[Any, Any],
        pending: object,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_digest: str,
        pending_action_revision: int = 1,
        pending_confirmation_claim_id: str | None = None,
    ) -> PendingAuthorityClaim:
        with self._lock:
            self._segment_record(authority)
            prepared_record = self._prepared_record(prepared)
            self._require_prepared_phase(
                prepared,
                kind="write",
                confirmation_policy="required",
            )
            self._require_prepared_call_fields(
                prepared,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
            )
            if prepared_record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
            pending_record = self._pending_record_for_object(pending)
            require_positive_int64(pending_action_revision, "pending_action_revision")
            self._require_pending_semantic(
                pending_record,
                conversation_id=authority.conversation_id,
                operation_id=operation_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                pending_action_revision=pending_action_revision,
                arguments_digest=arguments_digest,
                pending_confirmation_claim_id=pending_confirmation_claim_id,
            )
            claim_id = (
                pending_confirmation_claim_id
                if pending_confirmation_claim_id is not None
                else pending_record.semantic.get("pending_confirmation_claim_id")
            )
            if type(claim_id) is not str or not claim_id:
                raise AuthorityPhaseError("Pending confirmation claim identity is required")
            if pending_confirmation_claim_id is not None:
                self._set_pending_semantic(
                    pending_record,
                    conversation_id=authority.conversation_id,
                    operation_id=operation_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    pending_action_revision=pending_action_revision,
                    pending_confirmation_claim_id=pending_confirmation_claim_id,
                    allow_partial=True,
                )
            key = self._claim_key(
                "pending",
                authority,
                prepared,
                pending,
                operation_id,
                tool_call_id,
                tool_name,
                arguments_digest,
                pending_action_revision,
            )
            if key in self._claim_keys:
                raise AuthorityPhaseError("an equivalent Pending claim is already active")
            self._claim_pending_owner(pending_record, authority)
            claim_token = cast(
                PendingClaimInstanceToken,
                _new_opaque_handle(PendingClaimInstanceToken),
            )
            claim = PendingAuthorityClaim(
                conversation_id=authority.conversation_id,
                segment_id=authority.segment_id,
                authority_instance_token=authority.authority_instance_token,
                conversation_scope_revision=authority.conversation_scope_revision,
                trusted_scope=authority.trusted_scope,
                capability_profile_id=authority.capability_profile_id,
                capabilities=authority.capabilities,
                capability_policy_version=authority.capability_policy_version,
                binding_policy_version=authority.binding_policy_version,
                capability_profile_fingerprint=authority.capability_profile_fingerprint,
                binding_policy_fingerprint=authority.binding_policy_fingerprint,
                operation_id=operation_id,
                pending_identity=pending_record.token,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=arguments_digest,
                pending_confirmation_claim_id=claim_id,
                prepared_instance_token=prepared_record[1],
                pending_claim_instance_token=claim_token,
            )
            self._claims[id(claim)] = _Lifecycle(
                claim,
                authority=authority,
                prepared=prepared,
                pending=pending,
            )
            self._claim_object(claim_token)
            self._claim_object(claim)
            self._claim_fields[id(claim)] = _dataclass_snapshot(claim)
            self._claim_keys[key] = id(claim)
            self._objects[id(claim)] = claim
            self._objects[id(claim_token)] = claim_token
            return claim

    pending_authority_claim = issue_pending_claim

    def issue_execution_claim(
        self,
        authority: ApprovalExecutionAuthority,
        *,
        prepared: PreparedToolCall[Any, Any],
        pending: object,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
        effective_args_digest: str,
        pending_action_revision: int | None = None,
        transaction: object | None = None,
    ) -> ExecutionClaim:
        with self._lock:
            self._approval_record(authority)
            if transaction is None:
                raise AuthorityPhaseError("ExecutionClaim requires a registered transaction")
            transaction_record = self._registered_identity(self._transactions, transaction)
            if transaction_record.authority is not None and transaction_record.authority is not authority:
                raise AuthorityPhaseError("transaction belongs to another authority")
            prepared_record = self._prepared_record(prepared)
            if prepared_record[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
            self._require_prepared_phase(
                prepared,
                kind="write",
                confirmation_policy="required",
            )
            self._require_prepared_call_fields(
                prepared,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_digest=effective_args_digest,
            )
            pending_record = self._pending_record_for_object(pending)
            if pending_record.token is not authority.pending_identity:
                raise AuthorityPhaseError("Pending identity does not match approval authority")
            if operation_id != authority.operation_id or tool_call_id != authority.tool_call_id:
                raise AuthorityPhaseError("operation/tool identity does not match approval authority")
            if tool_name != authority.tool_name:
                raise AuthorityPhaseError("tool identity does not match approval authority")
            if not constant_time_equal(effective_args_digest, authority.effective_args_digest):
                raise AuthorityPhaseError("effective arguments digest does not match approval authority")
            revision = authority.pending_action_revision if pending_action_revision is None else pending_action_revision
            if revision != authority.pending_action_revision:
                raise AuthorityPhaseError("pending action revision does not match approval authority")
            self._require_pending_semantic(
                pending_record,
                conversation_id=authority.conversation_id,
                operation_id=authority.operation_id,
                tool_call_id=authority.tool_call_id,
                tool_name=authority.tool_name,
                pending_action_revision=revision,
                effective_args_digest=effective_args_digest,
            )
            key = self._claim_key(
                "execution",
                authority,
                prepared,
                pending,
                operation_id,
                tool_call_id,
                tool_name,
                effective_args_digest,
                revision,
            )
            if key in self._claim_keys:
                raise AuthorityPhaseError("an equivalent ExecutionClaim is already active")
            self._claim_pending_owner(pending_record, authority)
            if transaction_record.authority is None:
                transaction_record.authority = authority
            claim_token = cast(ExecutionClaimInstanceToken, _new_opaque_handle(ExecutionClaimInstanceToken))
            claim = ExecutionClaim(
                operation_id=operation_id,
                conversation_id=authority.conversation_id,
                pending_identity=pending_record.token,
                pending_action_revision=revision,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                effective_args_digest=effective_args_digest,
                transaction=transaction,
                approval_authority_instance_token=authority.authority_instance_token,
                prepared_instance_token=prepared_record[1],
                execution_claim_instance_token=claim_token,
            )
            self._claims[id(claim)] = _Lifecycle(
                claim,
                authority=authority,
                prepared=prepared,
                pending=pending,
                transaction=transaction,
            )
            self._claim_object(claim_token)
            self._claim_object(claim)
            self._claim_fields[id(claim)] = _dataclass_snapshot(claim)
            self._claim_keys[key] = id(claim)
            self._objects[id(claim)] = claim
            self._objects[id(claim_token)] = claim_token
            return claim

    execution_claim = issue_execution_claim

    def issue_omitted_token_proof(
        self,
        operation: object,
        *,
        pending_pointer: object,
        transaction: object,
    ) -> TrustedLedgerOmittedTokenProof:
        with self._lock:
            self._ensure_open()
            operation_record = self._registered_identity(self._operations, operation)
            pending_record = self._pending_record_for_object(pending_pointer)
            transaction_record = self._registered_identity(self._transactions, transaction)
            semantic = operation_record.semantic
            current_operation_id = getattr(operation, "operation_id", None)
            if current_operation_id is None:
                current_operation_id = getattr(operation, "id", None)
            if current_operation_id != semantic.get("operation_id"):
                raise AuthorityPhaseError("Operation identity changed after registration")
            for name in (
                "status",
                "adapter_kind",
                "tool_call_id",
                "tool_name",
                "proposal_fingerprint",
                "confirmation_token_fingerprint",
                "pending_confirmation_claim_id",
                "conversation_id",
            ):
                current_value = getattr(operation, name, None)
                expected_value = semantic.get(name)
                if name in {"proposal_fingerprint", "confirmation_token_fingerprint"}:
                    if not isinstance(current_value, str) or not isinstance(expected_value, str):
                        raise AuthorityPhaseError("Operation semantic identity changed")
                    if not constant_time_equal(current_value, expected_value):
                        raise AuthorityPhaseError("Operation semantic identity changed")
                elif current_value != expected_value:
                    raise AuthorityPhaseError("Operation semantic identity changed")
            status = getattr(operation, "status", None)
            if type(status) is not str or status != "proposed":
                raise AuthorityPhaseError("omitted-token proof requires a proposed operation")
            operation_id = semantic.get("operation_id")
            conversation_id = semantic.get("conversation_id")
            adapter_kind = semantic.get("adapter_kind")
            tool_call_id = semantic.get("tool_call_id")
            tool_name = semantic.get("tool_name")
            proposal_fingerprint = semantic.get("proposal_fingerprint")
            confirmation_token_fingerprint = semantic.get("confirmation_token_fingerprint")
            pending_confirmation_claim_id = semantic.get("pending_confirmation_claim_id")
            if not all(
                type(value) is str and bool(value)
                for value in (
                    operation_id,
                    adapter_kind,
                    tool_call_id,
                    tool_name,
                    pending_confirmation_claim_id,
                )
            ):
                raise AuthorityPhaseError("Operation semantic identity is incomplete")
            require_positive_int64(conversation_id, "conversation_id")
            if not isinstance(proposal_fingerprint, str) or not isinstance(
                confirmation_token_fingerprint, str
            ):
                raise AuthorityPhaseError("Operation fingerprints are incomplete")
            _require_hmac_digest(proposal_fingerprint, "proposal_fingerprint")
            _require_hmac_digest(
                confirmation_token_fingerprint,
                "confirmation_token_fingerprint",
            )
            pending_semantic = pending_record.semantic
            for name, expected in pending_semantic.items():
                if name in {"arguments_digest", "effective_args_digest"}:
                    continue
                if getattr(pending_pointer, name, None) != expected:
                    raise AuthorityPhaseError("Pending pointer semantic identity changed")
            for name, expected in (
                ("operation_id", operation_id),
                ("tool_call_id", tool_call_id),
                ("tool_name", tool_name),
                ("pending_confirmation_claim_id", pending_confirmation_claim_id),
            ):
                if pending_semantic.get(name) != expected:
                    raise AuthorityPhaseError("Pending pointer does not match Operation")
            if pending_semantic.get("conversation_id") != conversation_id:
                raise AuthorityPhaseError("Pending conversation identity does not match Operation")
            # Determine proof ownership from all three registered sources
            # before creating any one-shot value.  A partially bound tuple is
            # rejected; the only ownerless boundary is all three sources
            # being explicitly unowned.
            if len(pending_record.owners) > 1:
                raise AuthorityPhaseError("proof Pending has conflicting owners")
            pending_authority: ToolExecutionAuthority | None = None
            if pending_record.owners:
                pending_authority_id = next(iter(pending_record.owners))
                pending_authority_record = self._authorities.get(pending_authority_id)
                if pending_authority_record is None:
                    raise AuthorityPhaseError("proof Pending owner is not active")
                pending_authority = pending_authority_record.authority
            source_authorities = (
                operation_record.authority,
                pending_authority,
                transaction_record.authority,
            )
            if any(source is None for source in source_authorities):
                if not all(source is None for source in source_authorities):
                    raise AuthorityPhaseError("proof sources are not bound to one authority")
                proof_authority = None
            else:
                proof_authority = cast(ToolExecutionAuthority, source_authorities[0])
                if any(source is not proof_authority for source in source_authorities[1:]):
                    raise AuthorityPhaseError("proof sources belong to different authorities")
            proof_key = (id(operation), id(pending_pointer), id(transaction))
            if proof_key in self._proof_keys:
                raise AuthorityPhaseError("an equivalent omitted-token proof is already active")
            proof_token = cast(
                OmittedTokenProofInstanceToken,
                _new_opaque_handle(OmittedTokenProofInstanceToken),
            )
            proof = TrustedLedgerOmittedTokenProof(
                operation_id=cast(str, operation_id),
                conversation_id=cast(int, conversation_id),
                status="proposed",
                adapter_kind=cast(str, adapter_kind),
                tool_call_id=cast(str, tool_call_id),
                tool_name=cast(str, tool_name),
                proposal_fingerprint=proposal_fingerprint,
                confirmation_token_fingerprint=confirmation_token_fingerprint,
                pending_operation_id=cast(str, pending_semantic["operation_id"]),
                pending_tool_call_id=cast(str, pending_semantic["tool_call_id"]),
                pending_tool_name=cast(str, pending_semantic["tool_name"]),
                pending_confirmation_claim_id=cast(
                    str, pending_semantic["pending_confirmation_claim_id"]
                ),
                omitted_token_proof_instance_token=proof_token,
            )
            self._proofs[id(proof)] = _Lifecycle(
                proof,
                authority=proof_authority,
                pending=pending_pointer,
                transaction=transaction,
                operation=operation,
            )
            self._claim_object(proof_token)
            self._claim_object(proof)
            self._proof_fields[id(proof)] = _dataclass_snapshot(proof)
            self._proof_keys[proof_key] = id(proof)
            self._objects[id(proof)] = proof
            self._objects[id(proof_token)] = proof_token
            return proof

    omitted_token_proof = issue_omitted_token_proof

    def _validate_claim_sources(self, lifecycle: _Lifecycle) -> None:
        """Revalidate every exact source retained by a live claim."""

        authority = lifecycle.authority
        if authority is None:
            raise AuthorityPhaseError("claim authority provenance is missing")
        self._authority_record(authority)
        if lifecycle.prepared is None:
            raise AuthorityPhaseError("claim Prepared provenance is missing")
        prepared_record = self._prepared_record(lifecycle.prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("claim Prepared provenance changed")
        if lifecycle.pending is None:
            raise AuthorityPhaseError("claim Pending provenance is missing")
        pending_record = self._pending_record_for_object(lifecycle.pending)
        if id(authority) not in pending_record.owners:
            raise AuthorityPhaseError("claim Pending ownership changed")
        claim_pending_token = getattr(lifecycle.value, "pending_identity", None)
        if claim_pending_token is not pending_record.token:
            raise AuthorityPhaseError("claim Pending identity changed")
        if lifecycle.transaction is not None:
            transaction_record = self._registered_identity(
                self._transactions,
                lifecycle.transaction,
            )
            if transaction_record.authority is not authority:
                raise AuthorityPhaseError("claim transaction provenance changed")

    def _validate_proof_sources(self, lifecycle: _Lifecycle) -> None:
        """Revalidate the registered Operation/Pending/transaction proof inputs."""

        if lifecycle.operation is None or lifecycle.pending is None or lifecycle.transaction is None:
            raise AuthorityPhaseError("omitted-token proof provenance is incomplete")
        operation_record = self._registered_identity(self._operations, lifecycle.operation)
        pending_record = self._pending_record_for_object(lifecycle.pending)
        transaction_record = self._registered_identity(
            self._transactions,
            lifecycle.transaction,
        )
        authority = lifecycle.authority
        if operation_record.authority is not authority:
            raise AuthorityPhaseError("proof operation provenance changed")
        if transaction_record.authority is not authority:
            raise AuthorityPhaseError("proof transaction provenance changed")
        if authority is None:
            if pending_record.owners:
                raise AuthorityPhaseError("proof Pending ownership changed")
        elif pending_record.owners != {id(authority)}:
            raise AuthorityPhaseError("proof Pending ownership changed")
        proof = cast(TrustedLedgerOmittedTokenProof, lifecycle.value)
        semantic = operation_record.semantic
        if (
            proof.operation_id != semantic.get("operation_id")
            or proof.conversation_id != semantic.get("conversation_id")
            or proof.status != semantic.get("status")
            or proof.adapter_kind != semantic.get("adapter_kind")
            or proof.tool_call_id != semantic.get("tool_call_id")
            or proof.tool_name != semantic.get("tool_name")
            or proof.pending_confirmation_claim_id
            != semantic.get("pending_confirmation_claim_id")
        ):
            raise AuthorityPhaseError("proof operation source identity changed")
        for name, proof_value in (
            ("proposal_fingerprint", proof.proposal_fingerprint),
            ("confirmation_token_fingerprint", proof.confirmation_token_fingerprint),
        ):
            expected = semantic.get(name)
            if not isinstance(expected, str) or not constant_time_equal(proof_value, expected):
                raise AuthorityPhaseError("proof operation fingerprint changed")
        pending_semantic = pending_record.semantic
        if (
            proof.pending_operation_id != pending_semantic.get("operation_id")
            or proof.pending_tool_call_id != pending_semantic.get("tool_call_id")
            or proof.pending_tool_name != pending_semantic.get("tool_name")
            or proof.pending_confirmation_claim_id
            != pending_semantic.get("pending_confirmation_claim_id")
            or pending_semantic.get("conversation_id") != semantic.get("conversation_id")
        ):
            raise AuthorityPhaseError("proof Pending source identity changed")

    def _claim_lifecycle(
        self, claim: object, *, validate_sources: bool = True
    ) -> _Lifecycle:
        with self._lock:
            self._ensure_open()
            lifecycle = self._claims.get(id(claim))
            if lifecycle is None or lifecycle.value is not claim:
                raise AuthorityPhaseError("claim is not active in this scope")
            if validate_sources:
                snapshot = self._claim_fields.get(id(claim))
                if snapshot is None:
                    raise AuthorityPhaseError("claim snapshot is missing")
                _validate_snapshot(claim, snapshot, "claim")
                self._validate_claim_sources(lifecycle)
            return lifecycle

    def _proof_lifecycle(
        self, proof: object, *, validate_sources: bool = True
    ) -> _Lifecycle:
        with self._lock:
            self._ensure_open()
            lifecycle = self._proofs.get(id(proof))
            if lifecycle is None or lifecycle.value is not proof:
                raise AuthorityPhaseError("proof is not active in this scope")
            if validate_sources:
                snapshot = self._proof_fields.get(id(proof))
                if snapshot is None:
                    raise AuthorityPhaseError("proof snapshot is missing")
                _validate_snapshot(proof, snapshot, "proof")
                self._validate_proof_sources(lifecycle)
            return lifecycle

    def _release_claim_pending_owner(self, lifecycle: _Lifecycle) -> None:
        """Release a claim's Pending owner once no sibling claim uses it."""

        if lifecycle.pending is None or lifecycle.authority is None:
            return
        pending_record = self._pending.get(id(lifecycle.pending))
        if pending_record is None or pending_record.value is not lifecycle.pending:
            return
        for other in self._claims.values():
            if (
                other is not lifecycle
                and other.pending is lifecycle.pending
                and other.authority is lifecycle.authority
            ):
                return
        pending_record.owners.discard(id(lifecycle.authority))

    def _revoke_lifecycle_after_entry_failure(
        self,
        value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof,
        *,
        owns_in_flight: bool,
    ) -> None:
        """Clean only a lifecycle record owned by this context entry.

        A failed source validation leaves an active record in ``issued`` and
        must be cleaned.  A duplicate entry observes ``in_flight`` owned by a
        different lifecycle and must leave it untouched.
        """

        with self._lock:
            table: dict[int, _Lifecycle]
            if isinstance(value, TrustedLedgerOmittedTokenProof):
                table = self._proofs
            else:
                table = self._claims
            lifecycle = table.get(id(value))
            if lifecycle is None or lifecycle.value is not value:
                return
            if not owns_in_flight and lifecycle.state != "issued":
                return
            if owns_in_flight and lifecycle.state not in {"issued", "in_flight"}:
                return
            try:
                self.revoke(value)
            except AuthorityPhaseError:
                # Preserve the original entry/body/consume error if another
                # owner finalized the record before cleanup acquired the lock.
                pass

    def mark_in_flight(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        with self._lock:
            if isinstance(value, TrustedLedgerOmittedTokenProof):
                lifecycle = self._proof_lifecycle(value)
            else:
                lifecycle = self._claim_lifecycle(value)
            if lifecycle.state != "issued":
                raise AuthorityPhaseError("one-shot value is not in issued state")
            lifecycle.state = "in_flight"

    enter_in_flight = mark_in_flight

    def consume(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        with self._lock:
            if isinstance(value, TrustedLedgerOmittedTokenProof):
                lifecycle = self._proof_lifecycle(value)
                table = self._proofs
            else:
                lifecycle = self._claim_lifecycle(value)
                table = self._claims
            if lifecycle.state != "in_flight":
                raise AuthorityPhaseError("one-shot value must be in flight before consume")
            token: object | None = None
            if table is self._claims:
                self._drop_claim_key(id(value))
                token = getattr(value, "pending_claim_instance_token", None)
                if token is None:
                    token = getattr(value, "execution_claim_instance_token", None)
                self._claim_fields.pop(id(value), None)
            else:
                self._drop_proof_key(id(value))
                token = getattr(value, "omitted_token_proof_instance_token", None)
                self._proof_fields.pop(id(value), None)
            table.pop(id(value), None)
            self._objects.pop(id(value), None)
            self._drop_object(value)
            if token is not None:
                self._objects.pop(id(token), None)
                self._drop_object(token)

    consume_claim = consume

    def revoke(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        with self._lock:
            if isinstance(value, TrustedLedgerOmittedTokenProof):
                lifecycle = self._proof_lifecycle(value, validate_sources=False)
                table = self._proofs
            else:
                lifecycle = self._claim_lifecycle(value, validate_sources=False)
                table = self._claims
            if lifecycle.state not in {"issued", "in_flight"}:
                raise AuthorityPhaseError("one-shot value is already finalized")
            token: object | None = None
            if table is self._claims:
                self._drop_claim_key(id(value))
                token = getattr(value, "pending_claim_instance_token", None)
                if token is None:
                    token = getattr(value, "execution_claim_instance_token", None)
                self._claim_fields.pop(id(value), None)
                self._release_claim_pending_owner(lifecycle)
            else:
                self._drop_proof_key(id(value))
                token = getattr(value, "omitted_token_proof_instance_token", None)
                self._proof_fields.pop(id(value), None)
            table.pop(id(value), None)
            self._objects.pop(id(value), None)
            self._drop_object(value)
            if token is not None:
                self._objects.pop(id(token), None)
                self._drop_object(token)

    revoke_claim = revoke

    def claim_state(self, claim: ExecutionClaim | PendingAuthorityClaim) -> str | None:
        with self._lock:
            try:
                lifecycle = self._claim_lifecycle(claim)
            except AuthorityPhaseError:
                return None
            return lifecycle.state

    def proof_state(self, proof: TrustedLedgerOmittedTokenProof) -> str | None:
        with self._lock:
            try:
                lifecycle = self._proof_lifecycle(proof)
            except AuthorityPhaseError:
                return None
            return lifecycle.state

    @contextmanager
    def claim_lifecycle(
        self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof
    ) -> Iterator[ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof]:
        owns_in_flight = False
        completed = False
        try:
            self.mark_in_flight(value)
            owns_in_flight = True
            yield value
            self.consume(value)
            completed = True
        finally:
            if not completed:
                self._revoke_lifecycle_after_entry_failure(
                    value,
                    owns_in_flight=owns_in_flight,
                )

    # Helpers for tests and future pipeline ports.  They never expose token
    # values as text and deliberately return only bounded counts/booleans.
    def is_active(self, value: object) -> bool:
        with self._lock:
            if id(value) in self._authorities and self._authorities[id(value)].authority is value:
                return True
            if any(record.token is value for record in self._authorities.values()):
                return True
            if any(record.token is value or record.value is value for record in self._pending.values()):
                return True
            if id(value) in self._prepared and self._prepared[id(value)][0] is value:
                return True
            if any(token is value or original is value for original, token, _ in self._prepared.values()):
                return True
            if any(
                lifecycle.value is value for lifecycle in self._prepared_construction.values()
            ):
                return True
            constraint = self._constraints.get(id(value))
            if constraint is not None and constraint[0] is value:
                try:
                    _validate_snapshot(value, constraint[2], "scope constraint")
                except AuthorityPhaseError:
                    return False
                return True
            resolution = self._resolutions.get(id(value))
            if resolution is not None and resolution[0] is value:
                try:
                    _validate_snapshot(value, resolution[2], "binding resolution")
                except AuthorityPhaseError:
                    return False
                return True
            claim = self._claims.get(id(value))
            if claim is not None and claim.value is value:
                try:
                    self._claim_lifecycle(value)
                except AuthorityPhaseError:
                    return False
                return True
            if any(
                value is getattr(lifecycle.value, "pending_claim_instance_token", None)
                or value is getattr(lifecycle.value, "execution_claim_instance_token", None)
                for lifecycle in self._claims.values()
            ):
                return True
            proof = self._proofs.get(id(value))
            if proof is not None and proof.value is value:
                try:
                    self._proof_lifecycle(value)
                except AuthorityPhaseError:
                    return False
                return True
            if any(
                value is getattr(lifecycle.value, "omitted_token_proof_instance_token", None)
                for lifecycle in self._proofs.values()
            ):
                return True
            repository_binding = self._repository_bindings.get(id(value))
            if repository_binding is not None and repository_binding.value is value:
                snapshot = self._repository_binding_fields.get(id(value))
                return (
                    snapshot is not None
                    and self._repository_binding_sources_match(snapshot)
                    and self._repository_binding_snapshot_matches(value, snapshot)
                )
            call = self._calls.get(id(value))
            if call is not None and call[0] is value:
                try:
                    self._call_record(value)
                except AuthorityPhaseError:
                    return False
                return True
            for table in (
                self._runner_invocations,
                self._tool_contexts,
                self._surfaces,
                self._bindings,
                self._gateway_sessions,
                self._operations,
                self._transactions,
            ):
                found = table.get(id(value))
                if found is not None and found.value is value:
                    return True
            spec_registration = self._tool_specs.get(id(value))
            if spec_registration is not None and spec_registration[0] is value:
                try:
                    self._validate_registered_tool_spec(value)
                except AuthorityPhaseError:
                    return False
                return True
            return type(value) is str and any(
                found.value == value for found in self._attempts.values()
            )

    def require_active(self, value: object) -> None:
        """Require the exact object identity registered by this scope."""

        if not self.is_active(value):
            raise AuthorityPhaseError("transient value is not the registered object")


def _active_factory(authority: ToolExecutionAuthority) -> AuthorityFactory:
    with _ACTIVE_AUTHORITIES_LOCK:
        found = _ACTIVE_AUTHORITIES.get(id(authority))
        if found is None or found[0] is not authority:
            raise AuthorityPhaseError("authority is not active")
        return found[1]


def require_repository_binding(
    binding: object,
    *,
    repository: object,
    session: object,
    constraint: object,
) -> None:
    """Validate a repository binding through its owning live factory."""

    with _ACTIVE_AUTHORITIES_LOCK:
        found = _ACTIVE_REPOSITORY_BINDINGS.get(id(binding))
    if found is None or found[0] is not binding:
        raise AuthorityPhaseError("repository binding is not active")
    if type(found[1]) is not AuthorityFactory:
        raise AuthorityPhaseError("repository binding owner is not the exact AuthorityFactory")
    AuthorityFactory.require_repository_binding(
        found[1],
        binding,
        repository=repository,
        session=session,
        constraint=constraint,
    )


_PHASE_IDENTITIES: Mapping[str, type[AuthorityCallIdentity]] = {
    AuthorityUse.PROVIDER_SURFACE_BUILD.value: ProviderSurfaceBuildIdentity,
    AuthorityUse.PROVIDER_INVOKE.value: ProviderInvocationIdentity,
    AuthorityUse.NEW_TURN_PREPARE.value: NewTurnPrepareCallIdentity,
    AuthorityUse.READ_EXECUTE.value: ReadExecutionCallIdentity,
    AuthorityUse.TYPED_PENDING_CLAIM.value: TypedPendingCallIdentity,
    AuthorityUse.APPROVED_WRITE_PREPARE.value: ApprovedWritePrepareCallIdentity,
    AuthorityUse.APPROVED_WRITE_EXECUTE.value: ApprovedWriteExecuteCallIdentity,
}


def _use_value(use: str | AuthorityUse) -> str:
    if isinstance(use, AuthorityUse):
        return use.value
    if type(use) is not str:
        raise AuthorityPhaseError("authority use must be a closed use value")
    return use


def require_authority_phase(
    authority: ToolExecutionAuthority,
    use: str | AuthorityUse,
    call_identity: AuthorityCallIdentity,
) -> None:
    """Fail closed before Catalog lookup for an authority/use/identity tuple."""

    phase = _use_value(use)
    factory = _active_factory(authority)
    factory._authority_record(authority)
    expected_type = _PHASE_IDENTITIES.get(phase)
    if expected_type is None or type(call_identity) is not expected_type:
        raise AuthorityPhaseError("authority phase and call identity do not match")
    found = factory._call_record(call_identity)
    if found[1] is not authority:
        raise AuthorityPhaseError("call identity is not registered to this authority")
    if found[2] != phase:
        raise AuthorityPhaseError("call identity was issued for another phase")
    token = getattr(call_identity, "authority_instance_token", None)
    if token is None:
        token = getattr(call_identity, "approval_authority_instance_token", None)
    if token is not _authority_token(authority):
        raise AuthorityPhaseError("authority token identity mismatch")
    if isinstance(authority, SegmentExecutionAuthority):
        segment_id = getattr(call_identity, "segment_id", authority.segment_id)
        if segment_id != authority.segment_id:
            raise AuthorityPhaseError("segment identity mismatch")
    else:
        if not isinstance(authority, ApprovalExecutionAuthority):
            raise AuthorityPhaseError("unknown approval authority type")
        for name, expected in (
            ("operation_id", authority.operation_id),
            ("tool_call_id", authority.tool_call_id),
            ("tool_name", authority.tool_name),
            ("effective_args_digest", authority.effective_args_digest),
        ):
            actual = getattr(call_identity, name, expected)
            if name == "effective_args_digest":
                if not constant_time_equal(actual, expected):
                    raise AuthorityPhaseError(f"{name} identity mismatch")
            elif actual != expected:
                raise AuthorityPhaseError(f"{name} identity mismatch")


def require_authority_spec(
    authority: ToolExecutionAuthority,
    use: str | AuthorityUse,
    spec: object,
) -> None:
    """Validate the post-Catalog ToolSpec phase gate, still before resolver/SQL."""

    factory = _active_factory(authority)
    factory._authority_record(authority)
    if type(spec) is ToolSpec:
        registration = factory._tool_specs.get(id(spec))
        if registration is None or registration[0] is not spec:
            raise AuthorityPhaseError("ToolSpec is not registered for this authority")
        if registration[1] is not authority:
            raise AuthorityPhaseError("ToolSpec provenance does not match authority")
        factory._validate_registered_tool_spec(spec)
    phase = _use_value(use)
    kind = getattr(spec, "kind", None)
    confirmation_policy = getattr(spec, "confirmation_policy", None)
    if phase == AuthorityUse.READ_EXECUTE.value:
        if not isinstance(authority, SegmentExecutionAuthority):
            raise AuthorityPhaseError("Segment authority is required for read execution")
        if kind != "read":
            raise AuthorityPhaseError("read execution requires a read ToolSpec")
    elif phase == AuthorityUse.TYPED_PENDING_CLAIM.value:
        if not isinstance(authority, SegmentExecutionAuthority):
            raise AuthorityPhaseError("Segment authority is required for Pending claims")
        if kind != "write" or confirmation_policy != "required":
            raise AuthorityPhaseError("write authority requires confirmation")
    elif phase in {
        AuthorityUse.APPROVED_WRITE_PREPARE.value,
        AuthorityUse.APPROVED_WRITE_EXECUTE.value,
    }:
        if not isinstance(authority, ApprovalExecutionAuthority):
            raise AuthorityPhaseError("approval authority is required")
        if kind != "write" or confirmation_policy != "required":
            raise AuthorityPhaseError("write authority requires confirmation")
    elif phase in {
        AuthorityUse.PROVIDER_SURFACE_BUILD.value,
        AuthorityUse.PROVIDER_INVOKE.value,
        AuthorityUse.NEW_TURN_PREPARE.value,
    }:
        if not isinstance(authority, SegmentExecutionAuthority):
            raise AuthorityPhaseError("Segment authority is required for Provider phases")
    else:
        # Every unknown use is rejected rather than inferred.
        raise AuthorityPhaseError("unknown authority phase")


def execution_scope() -> AuthorityFactory:
    """Create a fresh execution-scoped authority factory."""

    return AuthorityFactory()


authority_scope = execution_scope
AuthorityRegistry = AuthorityFactory
AuthorityComposition = AuthorityFactory
ScopedAuthorityFactory = AuthorityFactory
ExecutionScope = AuthorityFactory


__all__ = [
    "AuthorityFactory",
    "AuthorityComposition",
    "AuthorityRegistry",
    "ExecutionScope",
    "ScopedAuthorityFactory",
    "authority_scope",
    "execution_scope",
    "require_authority_phase",
    "require_authority_spec",
]
