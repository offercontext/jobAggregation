"""Leaf contracts for scoped tool authority.

This module deliberately contains no database, repository, provider, or runtime
composition imports.  Values in this module are request-scoped capabilities;
they are not transport or persistence DTOs.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal, NoReturn, SupportsIndex

from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue


MAX_INT64: Final = 2**63 - 1
"""Largest value accepted by a positive signed 64-bit identity."""


_OPAQUE_SEAL = object()


class AuthorityContractError(TypeError):
    """Base error for malformed or untrusted transient authority values."""


class AuthorityPhaseError(AuthorityContractError):
    """Raised when an authority/use/call identity combination is not valid."""


class _OpaqueHandle:
    """An identity-only object that cannot cross a serialization boundary.

    The constructor is intentionally sealed.  Composition owns the only helper
    that can mint a handle.  Equality is object identity and the representation
    never contains the object's address or a secret value.
    """

    __slots__ = ()

    def __new__(cls, seal: object | None = None) -> _OpaqueHandle:
        if seal is not _OPAQUE_SEAL:
            raise TypeError("opaque authority handles are factory-created")
        return object.__new__(cls)

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    __str__ = __repr__

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("opaque authority handle cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        self._serialization_error()

    def __getstate__(self) -> NoReturn:
        self._serialization_error()

    def __copy__(self) -> NoReturn:
        self._serialization_error()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        self._serialization_error()

    def to_json(self) -> NoReturn:
        self._serialization_error()


class AuthorityInstanceToken(_OpaqueHandle):
    """Identity token for one live Segment or Approval authority."""


class PendingInstanceToken(_OpaqueHandle):
    """Identity token for one live Pending pointer/proposal."""


class PreparedInstanceToken(_OpaqueHandle):
    """Identity token for one live PreparedToolCall object."""


class ExecutionClaimInstanceToken(_OpaqueHandle):
    """Identity token for one one-shot execution claim."""


class OmittedTokenProofInstanceToken(_OpaqueHandle):
    """Identity token for one plain-reject proof."""


def _new_opaque_handle(handle_type: type[_OpaqueHandle]) -> _OpaqueHandle:
    """Mint a handle for the composition root.

    Kept private so callers cannot create a token by importing a public factory
    function.  ``composition.py`` is the only production module using it.
    """

    return handle_type(_OPAQUE_SEAL)


def _require_token(value: object, expected: type[_OpaqueHandle], field_name: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{field_name} must be a factory-issued {expected.__name__}")


def require_positive_int64(value: object, field_name: str = "identity") -> int:
    """Validate an exact Python ``int`` in the positive signed-int64 range."""

    if type(value) is not int or not 1 <= value <= MAX_INT64:
        raise ValueError(f"{field_name} must be a positive signed int64")
    return value


def require_nonnegative_int64(value: object, field_name: str = "revision") -> int:
    """Validate an exact Python ``int`` in the 0..signed-int64 range."""

    if type(value) is not int or not 0 <= value <= MAX_INT64:
        raise ValueError(f"{field_name} must be a non-negative signed int64")
    return value


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def constant_time_equal(left: str, right: str) -> bool:
    """Compare fixed-length textual digests without exposing timing by length."""

    if type(left) is not str or type(right) is not str or len(left) != len(right):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class AuthorityUse(str, Enum):
    PROVIDER_SURFACE_BUILD = "provider_surface_build"
    PROVIDER_INVOKE = "provider_invoke"
    NEW_TURN_PREPARE = "new_turn_prepare"
    READ_EXECUTE = "read_execute"
    TYPED_PENDING_CLAIM = "typed_pending_claim"
    APPROVED_WRITE_PREPARE = "approved_write_prepare"
    APPROVED_WRITE_EXECUTE = "approved_write_execute"

    def __str__(self) -> str:
        return self.value


class AuthorityDiagnosticReason(str, Enum):
    CAPABILITY_MISSING = "capability_missing"
    BINDING_MISMATCHED = "binding_mismatched"
    BINDING_DETACHED = "binding_detached"
    BINDING_UNAVAILABLE = "binding_unavailable"
    SCOPE_FORBIDDEN = "scope_forbidden"
    AUTHORITY_POLICY_INVALID = "authority_policy_invalid"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, repr=False)
class TrustedContextScope(TransientToolRuntimeValue):
    """Canonical, already validated Conversation scope used by an Authority."""

    context_type: Literal["workspace", "global", "application", "mode"]
    context_ref: int | None = field(repr=False)
    mode: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.context_type not in {"workspace", "global", "application", "mode"}:
            raise ValueError("unknown trusted context type")
        if self.context_type == "application":
            require_positive_int64(self.context_ref, "context_ref")
        elif self.context_ref is not None:
            raise ValueError("non-application scope cannot have a context_ref")
        _require_text(self.mode, "mode")


class ToolExecutionAuthority(TransientToolRuntimeValue):
    """Nominal base for the two sealed authority phases."""

    __slots__ = ()


@dataclass(frozen=True, slots=True, repr=False)
class SegmentExecutionAuthority(ToolExecutionAuthority):
    conversation_id: int
    conversation_scope_revision: int
    segment_id: str
    trusted_scope: TrustedContextScope = field(repr=False)
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    capability_profile_id: str = "agent_typed_v1"
    capabilities: frozenset[object] = field(default_factory=frozenset, repr=False)
    capability_policy_version: str = "capability-policy-v1"
    binding_policy_version: str = "binding-policy-v1"
    capability_profile_fingerprint: str = ""
    binding_policy_fingerprint: str = ""

    def __post_init__(self) -> None:
        require_positive_int64(self.conversation_id, "conversation_id")
        require_nonnegative_int64(self.conversation_scope_revision, "conversation_scope_revision")
        _require_text(self.segment_id, "segment_id")
        _require_text(self.capability_profile_id, "capability_profile_id")
        _require_text(self.capability_policy_version, "capability_policy_version")
        _require_text(self.binding_policy_version, "binding_policy_version")
        if type(self.capabilities) is not frozenset:
            raise TypeError("capabilities must be a frozenset")
        if type(self.capability_profile_fingerprint) is not str:
            raise TypeError("capability_profile_fingerprint must be text")
        if type(self.binding_policy_fingerprint) is not str:
            raise TypeError("binding_policy_fingerprint must be text")
        _require_token(
            self.authority_instance_token,
            AuthorityInstanceToken,
            "authority_instance_token",
        )


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalExecutionAuthority(ToolExecutionAuthority):
    operation_id: str
    conversation_id: int
    conversation_scope_revision: int
    trusted_scope: TrustedContextScope = field(repr=False)
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    effective_args_digest: str
    approval_authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    capability_profile_id: str = "agent_typed_v1"
    capabilities: frozenset[object] = field(default_factory=frozenset, repr=False)
    capability_policy_version: str = "capability-policy-v1"
    binding_policy_version: str = "binding-policy-v1"
    capability_profile_fingerprint: str = ""
    binding_policy_fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_text(self.operation_id, "operation_id")
        require_positive_int64(self.conversation_id, "conversation_id")
        require_nonnegative_int64(self.conversation_scope_revision, "conversation_scope_revision")
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")
        _require_text(self.effective_args_digest, "effective_args_digest")
        require_positive_int64(self.pending_action_revision, "pending_action_revision")
        _require_text(self.capability_profile_id, "capability_profile_id")
        _require_text(self.capability_policy_version, "capability_policy_version")
        _require_text(self.binding_policy_version, "binding_policy_version")
        if type(self.capabilities) is not frozenset:
            raise TypeError("capabilities must be a frozenset")
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        _require_token(
            self.approval_authority_instance_token,
            AuthorityInstanceToken,
            "approval_authority_instance_token",
        )

    @property
    def authority_instance_token(self) -> AuthorityInstanceToken:
        return self.approval_authority_instance_token


class AuthorityCallIdentity(TransientToolRuntimeValue):
    """Nominal base for the seven sealed call-identity variants."""

    __slots__ = ()


# Compatibility spelling retained for type-only callers from the design draft.
AuthorityCallIdentityValue = AuthorityCallIdentity


@dataclass(frozen=True, slots=True, repr=False)
class ProviderSurfaceBuildIdentity(AuthorityCallIdentity):
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    runner_invocation: object = field(repr=False, compare=False)
    segment_id: str
    tool_context: object = field(repr=False, compare=False)
    model_call_id: str

    def __post_init__(self) -> None:
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        _require_text(self.segment_id, "segment_id")
        _require_text(self.model_call_id, "model_call_id")

    @property
    def runner_invocation_identity(self) -> object:
        return self.runner_invocation

    @property
    def tool_execution_context(self) -> object:
        return self.tool_context


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInvocationIdentity(AuthorityCallIdentity):
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    runner_invocation: object = field(repr=False, compare=False)
    segment_id: str
    tool_context: object = field(repr=False, compare=False)
    model_call_id: str
    surface: object = field(repr=False, compare=False)
    surface_fingerprint: str
    model_call_surface_binding: object = field(repr=False, compare=False)
    gateway_session: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        _require_text(self.segment_id, "segment_id")
        _require_text(self.model_call_id, "model_call_id")
        _require_text(self.surface_fingerprint, "surface_fingerprint")

    @property
    def runner_invocation_identity(self) -> object:
        return self.runner_invocation

    @property
    def tool_execution_context(self) -> object:
        return self.tool_context

    @property
    def binding(self) -> object:
        return self.model_call_surface_binding


@dataclass(frozen=True, slots=True, repr=False)
class NewTurnPrepareCallIdentity(AuthorityCallIdentity):
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    runner_invocation: object = field(repr=False, compare=False)
    segment_id: str
    tool_context: object = field(repr=False, compare=False)
    model_call_id: str
    surface: object = field(repr=False, compare=False)
    surface_fingerprint: str
    model_call_surface_binding: object = field(repr=False, compare=False)
    gateway_session: object = field(repr=False, compare=False)
    attempt_id: str
    candidate_ordinal: int
    tool_call_id: str
    tool_name: str
    arguments_digest: str

    def __post_init__(self) -> None:
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        for value, name in (
            (self.segment_id, "segment_id"),
            (self.model_call_id, "model_call_id"),
            (self.surface_fingerprint, "surface_fingerprint"),
            (self.attempt_id, "attempt_id"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.arguments_digest, "arguments_digest"),
        ):
            _require_text(value, name)
        if type(self.candidate_ordinal) is not int or self.candidate_ordinal < 0:
            raise ValueError("candidate_ordinal must be a non-negative int")


@dataclass(frozen=True, slots=True, repr=False)
class ReadExecutionCallIdentity(AuthorityCallIdentity):
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    runner_invocation: object = field(repr=False, compare=False)
    segment_id: str
    tool_context: object = field(repr=False, compare=False)
    model_call_id: str
    surface: object = field(repr=False, compare=False)
    surface_fingerprint: str
    model_call_surface_binding: object = field(repr=False, compare=False)
    prepared: object = field(repr=False, compare=False)
    prepared_instance_token: PreparedInstanceToken = field(repr=False, compare=False)
    tool_call_id: str
    tool_name: str
    arguments_digest: str

    def __post_init__(self) -> None:
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        _require_token(self.prepared_instance_token, PreparedInstanceToken, "prepared_instance_token")
        for value, name in (
            (self.segment_id, "segment_id"),
            (self.model_call_id, "model_call_id"),
            (self.surface_fingerprint, "surface_fingerprint"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.arguments_digest, "arguments_digest"),
        ):
            _require_text(value, name)


@dataclass(frozen=True, slots=True, repr=False)
class TypedPendingCallIdentity(AuthorityCallIdentity):
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    runner_invocation: object = field(repr=False, compare=False)
    segment_id: str
    tool_context: object = field(repr=False, compare=False)
    prepared: object = field(repr=False, compare=False)
    prepared_instance_token: PreparedInstanceToken = field(repr=False, compare=False)
    operation_id: str
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    arguments_digest: str

    def __post_init__(self) -> None:
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        _require_token(self.prepared_instance_token, PreparedInstanceToken, "prepared_instance_token")
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        require_positive_int64(self.pending_action_revision, "pending_action_revision")
        for value, name in (
            (self.segment_id, "segment_id"),
            (self.operation_id, "operation_id"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.arguments_digest, "arguments_digest"),
        ):
            _require_text(value, name)


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWritePrepareCallIdentity(AuthorityCallIdentity):
    approval_authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    approval_context: object = field(repr=False, compare=False)
    request_identity: object = field(repr=False, compare=False)
    operation_id: str
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    effective_args_digest: str

    @property
    def authority_instance_token(self) -> AuthorityInstanceToken:
        return self.approval_authority_instance_token

    def __post_init__(self) -> None:
        _require_token(
            self.approval_authority_instance_token,
            AuthorityInstanceToken,
            "approval_authority_instance_token",
        )
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        require_positive_int64(self.pending_action_revision, "pending_action_revision")
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.effective_args_digest, "effective_args_digest"),
        ):
            _require_text(value, name)


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWriteExecuteCallIdentity(AuthorityCallIdentity):
    approval_authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    approval_context: object = field(repr=False, compare=False)
    request_identity: object = field(repr=False, compare=False)
    operation_id: str
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    effective_args_digest: str
    prepared: object = field(repr=False, compare=False)
    prepared_instance_token: PreparedInstanceToken = field(repr=False, compare=False)
    execution_claim: object = field(repr=False, compare=False)
    execution_claim_instance_token: ExecutionClaimInstanceToken = field(repr=False, compare=False)

    @property
    def authority_instance_token(self) -> AuthorityInstanceToken:
        return self.approval_authority_instance_token

    def __post_init__(self) -> None:
        _require_token(
            self.approval_authority_instance_token,
            AuthorityInstanceToken,
            "approval_authority_instance_token",
        )
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        _require_token(self.prepared_instance_token, PreparedInstanceToken, "prepared_instance_token")
        _require_token(
            self.execution_claim_instance_token,
            ExecutionClaimInstanceToken,
            "execution_claim_instance_token",
        )
        require_positive_int64(self.pending_action_revision, "pending_action_revision")
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.effective_args_digest, "effective_args_digest"),
        ):
            _require_text(value, name)


@dataclass(frozen=True, slots=True, repr=False)
class PendingAuthorityClaim(TransientToolRuntimeValue):
    conversation_id: int
    segment_id: str
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    conversation_scope_revision: int
    trusted_scope: TrustedContextScope = field(repr=False)
    capability_profile_id: str
    capabilities: frozenset[object] = field(repr=False)
    capability_policy_version: str
    binding_policy_version: str
    capability_profile_fingerprint: str
    binding_policy_fingerprint: str
    operation_id: str
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    tool_call_id: str
    tool_name: str
    arguments_digest: str
    prepared_instance_token: PreparedInstanceToken = field(repr=False, compare=False)
    pending_claim_instance_token: PendingInstanceToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        require_positive_int64(self.conversation_id, "conversation_id")
        require_nonnegative_int64(self.conversation_scope_revision, "conversation_scope_revision")
        for value, name in (
            (self.segment_id, "segment_id"),
            (self.capability_profile_id, "capability_profile_id"),
            (self.capability_policy_version, "capability_policy_version"),
            (self.binding_policy_version, "binding_policy_version"),
            (self.operation_id, "operation_id"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.arguments_digest, "arguments_digest"),
        ):
            _require_text(value, name)
        if type(self.capabilities) is not frozenset:
            raise TypeError("capabilities must be a frozenset")
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        _require_token(self.prepared_instance_token, PreparedInstanceToken, "prepared_instance_token")
        _require_token(
            self.pending_claim_instance_token,
            PendingInstanceToken,
            "pending_claim_instance_token",
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionClaim(TransientToolRuntimeValue):
    operation_id: str
    conversation_id: int
    pending_identity: PendingInstanceToken = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    effective_args_digest: str
    approval_authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)
    prepared_instance_token: PreparedInstanceToken = field(repr=False, compare=False)
    execution_claim_instance_token: ExecutionClaimInstanceToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.operation_id, "operation_id")
        require_positive_int64(self.conversation_id, "conversation_id")
        require_positive_int64(self.pending_action_revision, "pending_action_revision")
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")
        _require_text(self.effective_args_digest, "effective_args_digest")
        _require_token(self.pending_identity, PendingInstanceToken, "pending_identity")
        _require_token(
            self.approval_authority_instance_token,
            AuthorityInstanceToken,
            "approval_authority_instance_token",
        )
        _require_token(self.prepared_instance_token, PreparedInstanceToken, "prepared_instance_token")
        _require_token(
            self.execution_claim_instance_token,
            ExecutionClaimInstanceToken,
            "execution_claim_instance_token",
        )

    @property
    def authority_instance_token(self) -> AuthorityInstanceToken:
        return self.approval_authority_instance_token


@dataclass(frozen=True, slots=True, repr=False)
class TrustedLedgerOmittedTokenProof(TransientToolRuntimeValue):
    operation_id: str
    conversation_id: int
    status: Literal["proposed"]
    adapter_kind: str
    tool_call_id: str
    tool_name: str
    proposal_fingerprint: str
    confirmation_token_fingerprint: str
    pending_operation_id: str
    pending_tool_call_id: str
    pending_tool_name: str
    omitted_token_proof_instance_token: OmittedTokenProofInstanceToken = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        require_positive_int64(self.conversation_id, "conversation_id")
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.adapter_kind, "adapter_kind"),
            (self.tool_call_id, "tool_call_id"),
            (self.tool_name, "tool_name"),
            (self.proposal_fingerprint, "proposal_fingerprint"),
            (self.confirmation_token_fingerprint, "confirmation_token_fingerprint"),
            (self.pending_operation_id, "pending_operation_id"),
            (self.pending_tool_call_id, "pending_tool_call_id"),
            (self.pending_tool_name, "pending_tool_name"),
        ):
            _require_text(value, name)
        _require_token(
            self.omitted_token_proof_instance_token,
            OmittedTokenProofInstanceToken,
            "omitted_token_proof_instance_token",
        )

    @property
    def proof_instance_token(self) -> OmittedTokenProofInstanceToken:
        return self.omitted_token_proof_instance_token


@dataclass(frozen=True, slots=True, repr=False)
class ApplicationScopeConstraint(TransientToolRuntimeValue):
    entity_kind: Literal["application"]
    mode: Literal["unrestricted", "restricted"]
    allowed_identities: frozenset[int] = field(repr=False)
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.entity_kind != "application":
            raise ValueError("application scope constraint has a fixed entity kind")
        if self.mode == "unrestricted":
            if self.allowed_identities != frozenset():
                raise ValueError("unrestricted constraint must have no identities")
        elif self.mode == "restricted":
            if len(self.allowed_identities) != 1:
                raise ValueError("restricted constraint must have one identity")
            for identity in self.allowed_identities:
                require_positive_int64(identity, "allowed_identity")
        else:
            raise ValueError("unknown application scope constraint mode")
        if type(self.allowed_identities) is not frozenset:
            raise TypeError("allowed_identities must be a frozenset")
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")


@dataclass(frozen=True, slots=True, repr=False)
class BindingTargetResolution(TransientToolRuntimeValue):
    entity_kind: str
    state: Literal["resolved", "omitted", "detached", "unavailable"]
    identity: int | None
    authority_instance_token: AuthorityInstanceToken = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.entity_kind, "entity_kind")
        if self.state not in {"resolved", "omitted", "detached", "unavailable"}:
            raise ValueError("unknown binding target state")
        if self.state == "resolved":
            require_positive_int64(self.identity, "identity")
        elif self.identity is not None:
            raise ValueError("only resolved targets may carry an identity")
        _require_token(self.authority_instance_token, AuthorityInstanceToken, "authority_instance_token")


__all__ = [
    "MAX_INT64",
    "AuthorityCallIdentity",
    "AuthorityCallIdentityValue",
    "AuthorityContractError",
    "AuthorityDiagnosticReason",
    "AuthorityInstanceToken",
    "AuthorityPhaseError",
    "AuthorityUse",
    "ApprovalExecutionAuthority",
    "ApplicationScopeConstraint",
    "BindingTargetResolution",
    "ExecutionClaim",
    "ExecutionClaimInstanceToken",
    "NewTurnPrepareCallIdentity",
    "OmittedTokenProofInstanceToken",
    "PendingAuthorityClaim",
    "PendingInstanceToken",
    "PreparedInstanceToken",
    "ProviderInvocationIdentity",
    "ProviderSurfaceBuildIdentity",
    "ReadExecutionCallIdentity",
    "SegmentExecutionAuthority",
    "ToolExecutionAuthority",
    "TrustedContextScope",
    "TrustedLedgerOmittedTokenProof",
    "TypedPendingCallIdentity",
    "_new_opaque_handle",
    "constant_time_equal",
    "require_nonnegative_int64",
    "require_positive_int64",
]
