"""Composition-root factories and bounded identity registries.

The registry in this module is intentionally execution-scoped.  It keeps strong
references while an execution is live so an opaque identity cannot disappear
underneath a running call, then drops every reference when the scope exits.
There is no durable or process-wide completed-history store.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Literal, Mapping, cast

from offerpilot.ai.tool_runtime.contracts import PreparedToolCall

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
    PendingInstanceToken,
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
    constant_time_equal,
    require_positive_int64,
)


_ACTIVE_AUTHORITIES: dict[int, tuple[ToolExecutionAuthority, "AuthorityFactory"]] = {}


def _authority_token(authority: ToolExecutionAuthority) -> AuthorityInstanceToken:
    if isinstance(authority, SegmentExecutionAuthority):
        return authority.authority_instance_token
    if isinstance(authority, ApprovalExecutionAuthority):
        return authority.approval_authority_instance_token
    raise AuthorityPhaseError("unknown authority type")


def _opaque_identity(value: object, field_name: str) -> object:
    if value is None:
        raise AuthorityPhaseError(f"{field_name} identity is required")
    return value


class _Lifecycle:
    __slots__ = ("value", "state", "authority", "prepared", "pending", "transaction")

    def __init__(
        self,
        value: object,
        *,
        authority: ToolExecutionAuthority | None = None,
        prepared: object | None = None,
        pending: object | None = None,
        transaction: object | None = None,
    ) -> None:
        self.value = value
        self.state: Literal["issued", "in_flight"] = "issued"
        self.authority = authority
        self.prepared = prepared
        self.pending = pending
        self.transaction = transaction


class _AuthorityRecord:
    __slots__ = ("authority", "token", "kind")

    def __init__(self, authority: ToolExecutionAuthority, token: AuthorityInstanceToken) -> None:
        self.authority = authority
        self.token = token
        self.kind = "segment" if isinstance(authority, SegmentExecutionAuthority) else "approval"


class AuthorityFactory:
    """Factory plus bounded registry for one explicit execution scope."""

    def __init__(self) -> None:
        self._closed = False
        self._authorities: dict[int, _AuthorityRecord] = {}
        self._pending: dict[int, tuple[object, PendingInstanceToken]] = {}
        self._prepared: dict[int, tuple[object, PreparedInstanceToken, ToolExecutionAuthority]] = {}
        self._constraints: dict[int, tuple[ApplicationScopeConstraint, ToolExecutionAuthority]] = {}
        self._resolutions: dict[int, tuple[BindingTargetResolution, ToolExecutionAuthority]] = {}
        self._calls: dict[int, tuple[AuthorityCallIdentity, ToolExecutionAuthority, str]] = {}
        self._claims: dict[int, _Lifecycle] = {}
        self._proofs: dict[int, _Lifecycle] = {}
        self._objects: dict[int, object] = {}

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

    @property
    def active_count(self) -> int:
        return (
            len(self._authorities)
            + len(self._pending)
            + len(self._prepared)
            + len(self._constraints)
            + len(self._resolutions)
            + len(self._calls)
            + len(self._claims)
            + len(self._proofs)
        )

    @property
    def registry_size(self) -> int:
        return self.active_count

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for authority_id, (authority, owner) in tuple(_ACTIVE_AUTHORITIES.items()):
            if owner is self:
                del _ACTIVE_AUTHORITIES[authority_id]
        self._authorities.clear()
        self._pending.clear()
        self._prepared.clear()
        self._constraints.clear()
        self._resolutions.clear()
        self._calls.clear()
        self._claims.clear()
        self._proofs.clear()
        self._objects.clear()

    def _register_authority(
        self, authority: ToolExecutionAuthority, token: AuthorityInstanceToken
    ) -> ToolExecutionAuthority:
        self._ensure_open()
        authority_id = id(authority)
        self._authorities[authority_id] = _AuthorityRecord(authority, token)
        _ACTIVE_AUTHORITIES[authority_id] = (authority, self)
        return authority

    def _authority_record(self, authority: object) -> _AuthorityRecord:
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
        capability_profile_fingerprint: str = "",
        binding_policy_fingerprint: str = "",
    ) -> SegmentExecutionAuthority:
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
        capability_profile_fingerprint: str = "",
        binding_policy_fingerprint: str = "",
    ) -> ApprovalExecutionAuthority:
        self._ensure_open()
        pending_token = self._pending_token(pending_identity)
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
        return cast(ApprovalExecutionAuthority, self._register_authority(authority, token))

    approval_authority = create_approval_authority
    new_approval_authority = create_approval_authority

    def revoke_authority(self, authority: ToolExecutionAuthority) -> None:
        record = self._authority_record(authority)
        for claim_id, lifecycle in tuple(self._claims.items()):
            if lifecycle.authority is authority:
                del self._claims[claim_id]
        for prepared_id, (_, _, owner) in tuple(self._prepared.items()):
            if owner is authority:
                del self._prepared[prepared_id]
        for constraint_id, (_, owner) in tuple(self._constraints.items()):
            if owner is authority:
                del self._constraints[constraint_id]
        for resolution_id, (_, owner) in tuple(self._resolutions.items()):
            if owner is authority:
                del self._resolutions[resolution_id]
        for call_id, (_, owner, _) in tuple(self._calls.items()):
            if owner is authority:
                del self._calls[call_id]
        del self._authorities[id(authority)]
        _ACTIVE_AUTHORITIES.pop(id(authority), None)
        del record

    def register_pending(self, pending: object) -> PendingInstanceToken:
        self._ensure_open()
        if type(pending) is PendingInstanceToken:
            for original, token in self._pending.values():
                if token is pending:
                    return token
            raise AuthorityPhaseError("pending token is not active in this scope")
        pending_id = id(pending)
        existing = self._pending.get(pending_id)
        if existing is not None and existing[0] is pending:
            return existing[1]
        token = cast(PendingInstanceToken, _new_opaque_handle(PendingInstanceToken))
        self._pending[pending_id] = (pending, token)
        self._objects[pending_id] = pending
        return token

    def _pending_token(self, pending: object) -> PendingInstanceToken:
        if type(pending) is PendingInstanceToken:
            for original, token in self._pending.values():
                if token is pending:
                    return pending
            raise AuthorityPhaseError("pending identity is not active")
        pending_id = id(pending)
        found = self._pending.get(pending_id)
        if found is None or found[0] is not pending:
            raise AuthorityPhaseError("pending object is not registered")
        return found[1]

    def pending_token(self, pending: object) -> PendingInstanceToken:
        return self._pending_token(pending)

    def register_prepared(
        self,
        prepared: PreparedToolCall[Any, Any],
        authority: ToolExecutionAuthority,
    ) -> PreparedInstanceToken:
        self._ensure_open()
        self._authority_record(authority)
        prepared_id = id(prepared)
        existing = self._prepared.get(prepared_id)
        if existing is not None:
            if existing[0] is not prepared or existing[2] is not authority:
                raise AuthorityPhaseError("PreparedToolCall identity is already bound elsewhere")
            return existing[1]
        current_token = prepared.authority_instance_token
        if current_token is not None and current_token is not _authority_token(authority):
            raise AuthorityPhaseError("PreparedToolCall authority token mismatch")
        # ``PreparedToolCall`` remains frozen to callers; only this factory may
        # bind its opaque handle after the pipeline constructs the object.
        if current_token is None:
            object.__setattr__(prepared, "authority_instance_token", _authority_token(authority))
        token = cast(PreparedInstanceToken, _new_opaque_handle(PreparedInstanceToken))
        self._prepared[prepared_id] = (prepared, token, authority)
        self._objects[prepared_id] = prepared
        return token

    bind_prepared = register_prepared

    def create_application_scope_constraint(
        self,
        authority: ToolExecutionAuthority,
        *,
        mode: Literal["unrestricted", "restricted"] | None = None,
        allowed_identities: frozenset[int] | None = None,
    ) -> ApplicationScopeConstraint:
        """Create the sole constraint allowed for the authority's Scope."""

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
        self._constraints[id(constraint)] = (constraint, authority)
        self._objects[id(constraint)] = constraint
        return constraint

    scope_constraint = create_application_scope_constraint

    def register_scope_constraint(
        self, constraint: ApplicationScopeConstraint, authority: ToolExecutionAuthority
    ) -> ApplicationScopeConstraint:
        self._authority_record(authority)
        if constraint.authority_instance_token is not _authority_token(authority):
            raise AuthorityPhaseError("scope constraint token mismatch")
        self._constraints[id(constraint)] = (constraint, authority)
        self._objects[id(constraint)] = constraint
        return constraint

    def require_scope_constraint(
        self, constraint: object, authority: ToolExecutionAuthority
    ) -> None:
        self._authority_record(authority)
        found = self._constraints.get(id(constraint))
        if found is None or found[0] is not constraint or found[1] is not authority:
            raise AuthorityPhaseError("scope constraint is not the registered object")
        if constraint.authority_instance_token is not _authority_token(authority):
            raise AuthorityPhaseError("scope constraint token mismatch")

    def create_binding_target_resolution(
        self,
        authority: ToolExecutionAuthority,
        *,
        entity_kind: str,
        state: Literal["resolved", "omitted", "detached", "unavailable"],
        identity: int | None,
    ) -> BindingTargetResolution:
        self._authority_record(authority)
        resolution = BindingTargetResolution(
            entity_kind=entity_kind,
            state=state,
            identity=identity,
            authority_instance_token=_authority_token(authority),
        )
        self._resolutions[id(resolution)] = (resolution, authority)
        self._objects[id(resolution)] = resolution
        return resolution

    binding_target_resolution = create_binding_target_resolution

    def register_binding_target_resolution(
        self, resolution: BindingTargetResolution, authority: ToolExecutionAuthority
    ) -> BindingTargetResolution:
        self._authority_record(authority)
        if resolution.authority_instance_token is not _authority_token(authority):
            raise AuthorityPhaseError("binding resolution token mismatch")
        self._resolutions[id(resolution)] = (resolution, authority)
        self._objects[id(resolution)] = resolution
        return resolution

    def require_binding_target_resolution(
        self, resolution: object, authority: ToolExecutionAuthority
    ) -> None:
        self._authority_record(authority)
        found = self._resolutions.get(id(resolution))
        if found is None or found[0] is not resolution or found[1] is not authority:
            raise AuthorityPhaseError("binding resolution is not the registered object")
        if resolution.authority_instance_token is not _authority_token(authority):
            raise AuthorityPhaseError("binding resolution token mismatch")

    def prepared_token(self, prepared: object) -> PreparedInstanceToken:
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

    def _prepared_record(self, prepared: object) -> tuple[object, PreparedInstanceToken, ToolExecutionAuthority]:
        self.prepared_token(prepared)
        found = self._prepared[id(prepared)]
        if found[0] is not prepared:
            raise AuthorityPhaseError("PreparedToolCall identity mismatch")
        return found

    def _register_call(
        self, call: AuthorityCallIdentity, authority: ToolExecutionAuthority, use: AuthorityUse
    ) -> AuthorityCallIdentity:
        self._authority_record(authority)
        self._calls[id(call)] = (call, authority, use.value)
        self._objects[id(call)] = call
        return call

    def create_provider_surface_build_identity(
        self,
        authority: SegmentExecutionAuthority,
        *,
        runner_invocation: object,
        tool_context: object,
        model_call_id: str,
    ) -> ProviderSurfaceBuildIdentity:
        self._segment_record(authority)
        call = ProviderSurfaceBuildIdentity(
            authority_instance_token=authority.authority_instance_token,
            runner_invocation=_opaque_identity(runner_invocation, "runner_invocation"),
            segment_id=authority.segment_id,
            tool_context=_opaque_identity(tool_context, "tool_context"),
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
        authority = self._call_authority(build_identity, AuthorityUse.PROVIDER_SURFACE_BUILD)
        if not isinstance(authority, SegmentExecutionAuthority):
            raise AuthorityPhaseError("Segment authority is required")
        call = ProviderInvocationIdentity(
            authority_instance_token=authority.authority_instance_token,
            runner_invocation=build_identity.runner_invocation,
            segment_id=build_identity.segment_id,
            tool_context=build_identity.tool_context,
            model_call_id=build_identity.model_call_id,
            surface=_opaque_identity(surface, "surface"),
            surface_fingerprint=surface_fingerprint,
            model_call_surface_binding=_opaque_identity(
                model_call_surface_binding, "model_call_surface_binding"
            ),
            gateway_session=_opaque_identity(gateway_session, "gateway_session"),
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
        authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
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
        authority = self._call_authority(invocation_identity, AuthorityUse.PROVIDER_INVOKE)
        prepared_record = self._prepared_record(prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
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
        self._segment_record(authority)
        prepared_record = self._prepared_record(prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
        pending_token = self._pending_token(pending)
        call = TypedPendingCallIdentity(
            authority_instance_token=authority.authority_instance_token,
            runner_invocation=_opaque_identity(runner_invocation, "runner_invocation"),
            segment_id=authority.segment_id,
            tool_context=_opaque_identity(tool_context, "tool_context"),
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
        self._approval_record(authority)
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
        authority = self._call_authority(prepare_identity, AuthorityUse.APPROVED_WRITE_PREPARE)
        if not isinstance(authority, ApprovalExecutionAuthority):
            raise AuthorityPhaseError("Approval authority is required")
        prepared_record = self._prepared_record(prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
        claim_lifecycle = self._claim_lifecycle(execution_claim)
        if claim_lifecycle.authority is not authority or claim_lifecycle.prepared is not prepared:
            raise AuthorityPhaseError("ExecutionClaim is not bound to this authority/prepared call")
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
        self._ensure_open()
        found = self._calls.get(id(call))
        if found is None or found[0] is not call or found[2] != expected_use.value:
            raise AuthorityPhaseError("call identity is not active for this phase")
        return found[1]

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
    ) -> PendingAuthorityClaim:
        self._segment_record(authority)
        prepared_record = self._prepared_record(prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
        pending_token = self._pending_token(pending)
        require_positive_int64(pending_action_revision, "pending_action_revision")
        claim_token = cast(PendingInstanceToken, _new_opaque_handle(PendingInstanceToken))
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
            pending_identity=pending_token,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_digest=arguments_digest,
            prepared_instance_token=prepared_record[1],
            pending_claim_instance_token=claim_token,
        )
        self._claims[id(claim)] = _Lifecycle(
            claim,
            authority=authority,
            prepared=prepared,
            pending=pending,
        )
        self._objects[id(claim)] = claim
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
        self._approval_record(authority)
        prepared_record = self._prepared_record(prepared)
        if prepared_record[2] is not authority:
            raise AuthorityPhaseError("PreparedToolCall belongs to another authority")
        pending_token = self._pending_token(pending)
        if pending_token is not authority.pending_identity:
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
        claim_token = cast(ExecutionClaimInstanceToken, _new_opaque_handle(ExecutionClaimInstanceToken))
        claim = ExecutionClaim(
            operation_id=operation_id,
            conversation_id=authority.conversation_id,
            pending_identity=pending_token,
            pending_action_revision=revision,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            effective_args_digest=effective_args_digest,
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
        self._objects[id(claim)] = claim
        return claim

    execution_claim = issue_execution_claim

    def issue_omitted_token_proof(
        self,
        operation: object | None = None,
        *,
        operation_id: str | None = None,
        conversation_id: int | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        proposal_fingerprint: str | None = None,
        confirmation_token_fingerprint: str | None = None,
        pending_operation_id: str | None = None,
        pending_tool_call_id: str | None = None,
        pending_tool_name: str | None = None,
        adapter_kind: str = "typed",
        pending_pointer: object | None = None,
    ) -> TrustedLedgerOmittedTokenProof:
        self._ensure_open()
        if operation is not None:
            self._objects[id(operation)] = operation
            if operation_id is None:
                operation_id = cast(str | None, getattr(operation, "operation_id", None))
                if operation_id is None:
                    operation_id = cast(str | None, getattr(operation, "id", None))
            if conversation_id is None:
                conversation_id = cast(int | None, getattr(operation, "conversation_id", None))
            if tool_call_id is None:
                tool_call_id = cast(str | None, getattr(operation, "tool_call_id", None))
            if tool_name is None:
                tool_name = cast(str | None, getattr(operation, "tool_name", None))
            if proposal_fingerprint is None:
                proposal_fingerprint = cast(str | None, getattr(operation, "proposal_fingerprint", None))
            if confirmation_token_fingerprint is None:
                confirmation_token_fingerprint = cast(
                    str | None, getattr(operation, "confirmation_token_fingerprint", None)
                )
            status = getattr(operation, "status", "proposed")
            if status != "proposed":
                raise AuthorityPhaseError("omitted-token proof requires a proposed operation")
        if type(operation) is str and operation_id is None:
            operation_id = operation
        if operation_id is None or conversation_id is None or tool_call_id is None or tool_name is None:
            raise AuthorityPhaseError("operation identity is incomplete")
        if proposal_fingerprint is None or confirmation_token_fingerprint is None:
            raise AuthorityPhaseError("operation fingerprints are incomplete")
        proof_token = cast(
            OmittedTokenProofInstanceToken,
            _new_opaque_handle(OmittedTokenProofInstanceToken),
        )
        proof = TrustedLedgerOmittedTokenProof(
            operation_id=operation_id,
            conversation_id=conversation_id,
            status="proposed",
            adapter_kind=adapter_kind,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            proposal_fingerprint=proposal_fingerprint,
            confirmation_token_fingerprint=confirmation_token_fingerprint,
            pending_operation_id=pending_operation_id or operation_id,
            pending_tool_call_id=pending_tool_call_id or tool_call_id,
            pending_tool_name=pending_tool_name or tool_name,
            omitted_token_proof_instance_token=proof_token,
        )
        self._proofs[id(proof)] = _Lifecycle(proof, pending=pending_pointer, transaction=operation)
        self._objects[id(proof)] = proof
        return proof

    omitted_token_proof = issue_omitted_token_proof

    def _claim_lifecycle(self, claim: object) -> _Lifecycle:
        self._ensure_open()
        lifecycle = self._claims.get(id(claim))
        if lifecycle is None or lifecycle.value is not claim:
            raise AuthorityPhaseError("claim is not active in this scope")
        return lifecycle

    def _proof_lifecycle(self, proof: object) -> _Lifecycle:
        self._ensure_open()
        lifecycle = self._proofs.get(id(proof))
        if lifecycle is None or lifecycle.value is not proof:
            raise AuthorityPhaseError("proof is not active in this scope")
        return lifecycle

    def mark_in_flight(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        if isinstance(value, TrustedLedgerOmittedTokenProof):
            lifecycle = self._proof_lifecycle(value)
        else:
            lifecycle = self._claim_lifecycle(value)
        if lifecycle.state != "issued":
            raise AuthorityPhaseError("one-shot value is not in issued state")
        lifecycle.state = "in_flight"

    enter_in_flight = mark_in_flight

    def consume(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        if isinstance(value, TrustedLedgerOmittedTokenProof):
            lifecycle = self._proof_lifecycle(value)
            table = self._proofs
        else:
            lifecycle = self._claim_lifecycle(value)
            table = self._claims
        if lifecycle.state != "in_flight":
            raise AuthorityPhaseError("one-shot value must be in flight before consume")
        table.pop(id(value), None)
        self._objects.pop(id(value), None)

    consume_claim = consume

    def revoke(self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof) -> None:
        if isinstance(value, TrustedLedgerOmittedTokenProof):
            lifecycle = self._proof_lifecycle(value)
            table = self._proofs
        else:
            lifecycle = self._claim_lifecycle(value)
            table = self._claims
        if lifecycle.state not in {"issued", "in_flight"}:
            raise AuthorityPhaseError("one-shot value is already finalized")
        table.pop(id(value), None)
        self._objects.pop(id(value), None)

    revoke_claim = revoke

    def claim_state(self, claim: ExecutionClaim | PendingAuthorityClaim) -> str | None:
        lifecycle = self._claims.get(id(claim))
        if lifecycle is None or lifecycle.value is not claim:
            return None
        return lifecycle.state

    def proof_state(self, proof: TrustedLedgerOmittedTokenProof) -> str | None:
        lifecycle = self._proofs.get(id(proof))
        if lifecycle is None or lifecycle.value is not proof:
            return None
        return lifecycle.state

    @contextmanager
    def claim_lifecycle(
        self, value: ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof
    ) -> Iterator[ExecutionClaim | PendingAuthorityClaim | TrustedLedgerOmittedTokenProof]:
        self.mark_in_flight(value)
        try:
            yield value
        except BaseException:
            self.revoke(value)
            raise
        else:
            self.consume(value)

    # Helpers for tests and future pipeline ports.  They never expose token
    # values as text and deliberately return only bounded counts/booleans.
    def is_active(self, value: object) -> bool:
        return (
            (id(value) in self._authorities and self._authorities[id(value)].authority is value)
            or (id(value) in self._prepared and self._prepared[id(value)][0] is value)
            or (id(value) in self._constraints and self._constraints[id(value)][0] is value)
            or (id(value) in self._resolutions and self._resolutions[id(value)][0] is value)
            or (id(value) in self._claims and self._claims[id(value)].value is value)
            or (id(value) in self._proofs and self._proofs[id(value)].value is value)
        )

    def require_active(self, value: object) -> None:
        """Require the exact object identity registered by this scope."""

        if not self.is_active(value):
            raise AuthorityPhaseError("transient value is not the registered object")


def _active_factory(authority: ToolExecutionAuthority) -> AuthorityFactory:
    found = _ACTIVE_AUTHORITIES.get(id(authority))
    if found is None or found[0] is not authority:
        raise AuthorityPhaseError("authority is not active")
    return found[1]


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
    found = factory._calls.get(id(call_identity))
    if found is None or found[0] is not call_identity or found[1] is not authority:
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
