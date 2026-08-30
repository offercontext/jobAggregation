from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, Literal, NoReturn, Protocol, SupportsIndex, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    ApprovedWritePrepareCallIdentity,
    AuthorityFactory,
    AuthorityPhaseError,
    AuthorityUse,
    PendingAuthorityClaim,
    require_authority_phase,
)
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_authority.visibility import (
    AuthorityApplicationVisibilityError,
    AuthorityApplicationVisibilityQuery,
)
from offerpilot.ai.pending_replay import (
    PendingReplayArgsDecoderV1,
    PendingReplayIntegrityError,
)
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    JSONValue,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    TransientToolRuntimeValue,
    UndoPolicy,
)
from offerpilot.ai.tool_runtime.metadata import (
    CommittedPrimaryOperationIdentityV1,
    CompensationHandle,
    FrozenJSONValue,
    LegacyAdapterBindingV1,
    LegacyWriteHandle,
    OperationRouteEntryV1,
    OperationRouteIdentityV1,
    ToolAuthorityEntryV1,
    ToolOperationMetadataPort,
    TypedWriteHandle,
    WriteOperationMetadataV1,
    materialize_json,
)
from offerpilot.ai.tool_runtime.pipeline import (
    _audit_entry_bindings,
    _pre_resolver_entry_scope_policy,
    execute_prepared,
)
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.journal import project_tool_started_bound
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyPreparedInputPort,
    PreparedLegacyCall,
    PreparedLegacyInputV1,
)
from offerpilot.ai.tool_runtime.legacy import LegacyArgumentPreparationError
from offerpilot.ai.tool_runtime.transport import project_transport_event
from offerpilot.ai.tool_runtime.validation import (
    ArgumentValidationError,
    canonical_json,
    parse_arguments,
)
from offerpilot.models import (
    ChatMessage,
    Conversation,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.context_projector.loader import WORK_DEADLINE_SECONDS, database_coordinator


LEDGER_KEY_FILENAME = "write-operation-ledger.key"
DELIVERY_OWNER_LEASE_SECONDS = 120
DELIVERY_OWNER_HEARTBEAT_SECONDS = 30
COMPENSATION_ID_NAMESPACE = UUID("4079900d-84a6-5cff-aa63-65089c4ccccd")
_TERMINAL_STATUSES = frozenset({"committed", "failed", "rejected"})


def _require_pending_digest(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("Pending route arguments digest is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PendingRouteIdentityV1:
    """Primitive locked identity for one transient Pending persistence route."""

    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    pending_action_revision: int
    pending_confirmation_claim_id: str
    arguments_digest: str

    def __post_init__(self) -> None:
        if type(self.conversation_id) is not int or self.conversation_id <= 0:
            raise ValueError("Pending route conversation id must be positive")
        if type(self.tool_call_id) is not str or not self.tool_call_id:
            raise ValueError("Pending route tool call id is required")
        if type(self.tool_name) is not str:
            raise TypeError("Pending route tool name must be text")
        if type(self.operation_id) is not str:
            raise TypeError("Pending route operation id must be text")
        if type(self.pending_confirmation_claim_id) is not str:
            raise TypeError("Pending route claim id must be text")
        if type(self.pending_action_revision) is not int or self.pending_action_revision <= 0:
            raise ValueError("Pending route revision must be positive")
        _require_pending_digest(self.arguments_digest)
        operationless = self.operation_id == ""
        if operationless != (self.pending_confirmation_claim_id == ""):
            raise ValueError("Pending route operation and claim identities must agree")
        if not operationless and not self.tool_name:
            raise ValueError("Operation-bearing Pending route requires a tool name")


def _pending_identity_snapshot(value: PendingRouteIdentityV1) -> tuple[object, ...]:
    value.__post_init__()
    return (
        value.conversation_id,
        value.operation_id,
        value.tool_call_id,
        value.tool_name,
        value.pending_action_revision,
        value.pending_confirmation_claim_id,
        value.arguments_digest,
    )


_PENDING_HANDLE_CONSTRUCTION_SEAL = object()
_PENDING_CLAIM_NOT_PROVIDED = object()


class _PendingHandleLifecycle:
    __slots__ = ("active", "lock")

    def __init__(self) -> None:
        self.active = True
        self.lock = RLock()


class _PendingRouteHandle(TransientToolRuntimeValue):
    __slots__ = ("_port", "_port_token", "_handle_token", "_lifecycle", "_seal")
    _port: PendingPersistenceRoutePort
    _port_token: object
    _handle_token: object
    _lifecycle: _PendingHandleLifecycle
    _seal: tuple[
        PendingPersistenceRoutePort,
        object,
        object,
        _PendingHandleLifecycle,
    ]

    def __new__(cls, seal: object | None = None, **kwargs: object) -> "_PendingRouteHandle":
        del kwargs
        if seal is not _PENDING_HANDLE_CONSTRUCTION_SEAL:
            raise TypeError("Pending route handles are Port-created")
        return object.__new__(cls)

    def __init__(
        self,
        seal: object | None = None,
        *,
        port: "PendingPersistenceRoutePort",
        port_token: object,
        handle_token: object,
    ) -> None:
        if seal is not _PENDING_HANDLE_CONSTRUCTION_SEAL:
            raise TypeError("Pending route handles are Port-created")
        lifecycle = _PendingHandleLifecycle()
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_port_token", port_token)
        object.__setattr__(self, "_handle_token", handle_token)
        object.__setattr__(self, "_lifecycle", lifecycle)
        object.__setattr__(self, "_seal", (port, port_token, handle_token, lifecycle))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Pending route handle components are sealed")

    def _require(self, port: "PendingPersistenceRoutePort") -> None:
        self._require_provenance(port)
        with self._lifecycle.lock:
            if self._lifecycle.active is not True:
                raise ValueError("Pending route handle is revoked")

    def _require_provenance(self, port: "PendingPersistenceRoutePort") -> None:
        if (
            self._seal != (self._port, self._port_token, self._handle_token, self._lifecycle)
            or self._port is not port
            or self._port_token is not port._port_token
            or type(self._lifecycle) is not _PendingHandleLifecycle
        ):
            raise ValueError("Pending route handle provenance drift")

    def _revoke(self, port: "PendingPersistenceRoutePort") -> None:
        self._require(port)
        with self._lifecycle.lock:
            self._lifecycle.active = False


class TypedPendingRouteHandle(_PendingRouteHandle):
    __slots__ = ()


class LegacyPendingRouteHandle(_PendingRouteHandle):
    __slots__ = ()


class ClarificationPendingRouteHandle(_PendingRouteHandle):
    __slots__ = ()


class _PrimaryParentRouteHandle(_PendingRouteHandle):
    __slots__ = ()


class _CompensationParentRouteHandle(_PendingRouteHandle):
    __slots__ = ()


PendingPersistenceRouteHandle = (
    TypedPendingRouteHandle | LegacyPendingRouteHandle | ClarificationPendingRouteHandle
)


@dataclass(frozen=True, slots=True)
class _PendingRouteResolution:
    identity: PendingRouteIdentityV1
    route: OperationRouteEntryV1 | None
    adapter_kind: Literal["typed", "legacy_deterministic", "clarification"]
    claim: object | None


@dataclass(frozen=True, slots=True)
class _PendingIssuedRecord:
    handle: _PendingRouteHandle
    upstream_handle: object | None
    identity: PendingRouteIdentityV1 | CommittedPrimaryOperationIdentityV1
    identity_snapshot: tuple[object, ...]
    route: OperationRouteEntryV1 | None
    legacy_binding: LegacyAdapterBindingV1 | None
    claim: object | None
    role: Literal["pending", "primary_parent", "compensation_parent"]


def _operation_identity_from_pending(value: PendingRouteIdentityV1) -> OperationRouteIdentityV1:
    return OperationRouteIdentityV1(
        operation_id=value.operation_id,
        tool_call_id=value.tool_call_id,
        revision=value.pending_action_revision,
        arguments_digest=value.arguments_digest,
    )


class ChainedPendingTopologyPolicyV1(TransientToolRuntimeValue):
    """Exact parent/child topology verifier issued by one Pending Port."""

    __slots__ = ("_port",)
    _port: PendingPersistenceRoutePort

    def __init__(self, port: "PendingPersistenceRoutePort") -> None:
        object.__setattr__(self, "_port", port)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Pending topology policy is sealed")

    def require_transition(self, parent: object, child: object) -> None:
        parent_record = self._port._require_parent(parent)
        child_record = self._port._require_child(child)
        if parent_record.role != "primary_parent" or parent_record.route is None:
            raise ValueError("Compensation routes cannot parent Pending actions")
        if child_record.route is None:
            raise ValueError("Clarification routes cannot be chained operation children")
        parent_route = parent_record.route
        child_route = child_record.route
        if parent_route.adapter_kind == "typed" and child_route.adapter_kind == "typed":
            return
        parent_binding = parent_record.legacy_binding
        child_binding = child_record.legacy_binding
        if (
            parent_route.adapter_kind == "legacy_deterministic"
            and child_route.adapter_kind == "legacy_deterministic"
            and type(parent_binding) is LegacyAdapterBindingV1
            and child_binding is parent_binding
            and parent_binding.chained_policy == "same_adapter_only"
        ):
            return
        raise ValueError("Pending chained topology is not permitted")


class PendingPersistenceRoutePort(TransientToolRuntimeValue):
    """Sealed wrapper from exact Operation handles to persistence-only routes."""

    __slots__ = (
        "_operation_port",
        "_bundle_token",
        "_port_token",
        "_lock",
        "_records",
        "_policy",
        "_integrity_seal",
    )
    _operation_port: ToolOperationMetadataPort
    _bundle_token: object
    _port_token: object
    _lock: RLock
    _records: dict[int, _PendingIssuedRecord]
    _policy: ChainedPendingTopologyPolicyV1
    _integrity_seal: tuple[
        ToolOperationMetadataPort,
        object,
        object,
        RLock,
        dict[int, _PendingIssuedRecord],
        ChainedPendingTopologyPolicyV1,
    ]

    def __init__(self, *, operation_port: ToolOperationMetadataPort) -> None:
        if type(operation_port) is not ToolOperationMetadataPort:
            raise TypeError("Pending Port requires an exact Operation Port")
        bundle_token = operation_port.bundle_instance_token
        port_token = object()
        lock = RLock()
        records: dict[int, _PendingIssuedRecord] = {}
        object.__setattr__(self, "_operation_port", operation_port)
        object.__setattr__(self, "_bundle_token", bundle_token)
        object.__setattr__(self, "_port_token", port_token)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_records", records)
        policy = ChainedPendingTopologyPolicyV1(self)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(
            self,
            "_integrity_seal",
            (operation_port, bundle_token, port_token, lock, records, policy),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Pending Persistence Route Port is sealed")

    def _ensure_integrity(self) -> None:
        if (
            self._integrity_seal
            != (
                self._operation_port,
                self._bundle_token,
                self._port_token,
                self._lock,
                self._records,
                self._policy,
            )
            or self._operation_port.bundle_instance_token is not self._bundle_token
        ):
            raise ValueError("Pending Persistence Route Port integrity drift")

    @property
    def chained_topology_policy(self) -> ChainedPendingTopologyPolicyV1:
        self._ensure_integrity()
        return self._policy

    @property
    def bundle_instance_token(self) -> object:
        self._ensure_integrity()
        return self._bundle_token

    def _issue(
        self,
        handle_type: type[_PendingRouteHandle],
        *,
        upstream_handle: object | None,
        identity: PendingRouteIdentityV1 | CommittedPrimaryOperationIdentityV1,
        snapshot: tuple[object, ...],
        route: OperationRouteEntryV1 | None,
        legacy_binding: LegacyAdapterBindingV1 | None,
        claim: object | None,
        role: Literal["pending", "primary_parent", "compensation_parent"],
    ) -> _PendingRouteHandle:
        handle = handle_type(
            _PENDING_HANDLE_CONSTRUCTION_SEAL,
            port=self,
            port_token=self._port_token,
            handle_token=object(),
        )
        record = _PendingIssuedRecord(
            handle,
            upstream_handle,
            identity,
            snapshot,
            route,
            legacy_binding,
            claim,
            role,
        )
        with self._lock:
            self._records[id(handle)] = record
        return handle

    def bind_typed_pending(
        self,
        operation_handle: TypedWriteHandle,
        identity: PendingRouteIdentityV1,
        claim: object,
    ) -> TypedPendingRouteHandle:
        self._ensure_integrity()
        if type(identity) is not PendingRouteIdentityV1 or not identity.operation_id:
            raise TypeError("Typed Pending requires an exact operation-bearing identity")
        route, legacy_binding = self._snapshot_primary(operation_handle, identity, claim=claim)
        if legacy_binding is not None:
            raise ValueError("Typed Pending cannot carry a Legacy binding")
        if route.operation_name != identity.tool_name:
            raise ValueError("Typed Pending operation name mismatch")
        return cast(
            TypedPendingRouteHandle,
            self._issue(
                TypedPendingRouteHandle,
                upstream_handle=operation_handle,
                identity=identity,
                snapshot=_pending_identity_snapshot(identity),
                route=route,
                legacy_binding=None,
                claim=claim,
                role="pending",
            ),
        )

    def require_typed_pending(
        self,
        handle: TypedPendingRouteHandle,
        identity: PendingRouteIdentityV1,
        claim: object,
    ) -> OperationRouteEntryV1:
        record = self._require_record(handle, TypedPendingRouteHandle, "pending")
        if (
            identity is not record.identity
            or _pending_identity_snapshot(identity) != record.identity_snapshot
            or claim is not record.claim
        ):
            raise ValueError("Typed Pending identity mismatch")
        route, legacy_binding = self._snapshot_primary(
            record.upstream_handle,
            identity,
            claim=claim,
        )
        if route is not record.route or legacy_binding is not None:
            raise ValueError("Typed Pending route drift")
        return route

    def bind_legacy_pending(
        self,
        operation_handle: LegacyWriteHandle,
        identity: PendingRouteIdentityV1,
    ) -> LegacyPendingRouteHandle:
        self._ensure_integrity()
        if type(identity) is not PendingRouteIdentityV1 or not identity.operation_id:
            raise TypeError("Legacy Pending requires an exact operation-bearing identity")
        route, legacy_binding = self._snapshot_primary(operation_handle, identity)
        if type(legacy_binding) is not LegacyAdapterBindingV1:
            raise ValueError("Legacy Pending requires an exact Adapter binding")
        if route.operation_name != identity.tool_name:
            raise ValueError("Legacy Pending operation name mismatch")
        return cast(
            LegacyPendingRouteHandle,
            self._issue(
                LegacyPendingRouteHandle,
                upstream_handle=operation_handle,
                identity=identity,
                snapshot=_pending_identity_snapshot(identity),
                route=route,
                legacy_binding=legacy_binding,
                claim=None,
                role="pending",
            ),
        )

    def require_legacy_pending(
        self,
        handle: LegacyPendingRouteHandle,
        identity: PendingRouteIdentityV1,
    ) -> OperationRouteEntryV1:
        record = self._require_record(handle, LegacyPendingRouteHandle, "pending")
        if (
            identity is not record.identity
            or _pending_identity_snapshot(identity) != record.identity_snapshot
        ):
            raise ValueError("Legacy Pending identity mismatch")
        route, legacy_binding = self._snapshot_primary(record.upstream_handle, identity)
        if route is not record.route or legacy_binding is not record.legacy_binding:
            raise ValueError("Legacy Pending route drift")
        return route

    def bind_clarification_pending(
        self,
        identity: PendingRouteIdentityV1,
    ) -> ClarificationPendingRouteHandle:
        self._ensure_integrity()
        if type(identity) is not PendingRouteIdentityV1 or identity.operation_id:
            raise TypeError("Clarification Pending must be operationless")
        return cast(
            ClarificationPendingRouteHandle,
            self._issue(
                ClarificationPendingRouteHandle,
                upstream_handle=None,
                identity=identity,
                snapshot=_pending_identity_snapshot(identity),
                route=None,
                legacy_binding=None,
                claim=None,
                role="pending",
            ),
        )

    def require_clarification_pending(
        self,
        handle: ClarificationPendingRouteHandle,
        identity: PendingRouteIdentityV1,
    ) -> None:
        record = self._require_record(handle, ClarificationPendingRouteHandle, "pending")
        if (
            identity is not record.identity
            or _pending_identity_snapshot(identity) != record.identity_snapshot
        ):
            raise ValueError("Clarification Pending identity mismatch")

    def _snapshot_primary(
        self,
        handle: object,
        identity: PendingRouteIdentityV1,
        *,
        claim: object = _PENDING_CLAIM_NOT_PROVIDED,
    ) -> tuple[OperationRouteEntryV1, LegacyAdapterBindingV1 | None]:
        operation_identity = _operation_identity_from_pending(identity)
        state = self._operation_port._state
        with state.lock:
            if type(handle) is TypedWriteHandle:
                handle._ensure_integrity()
                typed_issued = state.typed.get(id(handle))
                if typed_issued is None or typed_issued[0] is not handle:
                    raise ValueError("Typed primary parent was not issued by this Operation Port")
                _, lease, spec_handle, _expected, snapshot, expected_claim, route = typed_issued
                if snapshot != (
                    operation_identity.operation_id,
                    operation_identity.tool_call_id,
                    operation_identity.revision,
                    operation_identity.arguments_digest,
                ) or (claim is not _PENDING_CLAIM_NOT_PROVIDED and claim is not expected_claim):
                    raise ValueError("Typed primary parent identity mismatch")
                try:
                    lease.require_spec(spec_handle)
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    raise ValueError("Typed primary route is revoked") from exc
                return route, None
            if type(handle) is LegacyWriteHandle:
                handle._ensure_integrity()
                legacy_issued = state.legacy.get(id(handle))
                if legacy_issued is None or legacy_issued[0] is not handle:
                    raise ValueError("Legacy primary parent was not issued by this Operation Port")
                _, route_handle, _expected, snapshot, route = legacy_issued
                if snapshot != (
                    operation_identity.operation_id,
                    operation_identity.tool_call_id,
                    operation_identity.revision,
                    operation_identity.arguments_digest,
                ):
                    raise ValueError("Legacy primary parent identity mismatch")
                try:
                    binding = self._operation_port._legacy_route_issuer_port.require_route(
                        route_handle
                    )
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    raise ValueError("Legacy primary route is revoked") from exc
                if (
                    type(binding) is not LegacyAdapterBindingV1
                    or binding.name != route.operation_name
                    or binding.adapter_kind != route.adapter_kind
                ):
                    raise ValueError("Legacy primary Adapter binding drift")
                return route, binding
        raise TypeError("Primary parent requires a Typed or Legacy operation handle")

    def bind_primary_parent(
        self,
        operation_handle: TypedWriteHandle | LegacyWriteHandle,
        identity: PendingRouteIdentityV1,
    ) -> _PrimaryParentRouteHandle:
        if type(identity) is not PendingRouteIdentityV1 or not identity.operation_id:
            raise TypeError("Primary parent requires an operation-bearing identity")
        route, legacy_binding = self._snapshot_primary(operation_handle, identity)
        if route.operation_name != identity.tool_name or route.operation_role != "primary":
            raise ValueError("Primary parent route mismatch")
        return cast(
            _PrimaryParentRouteHandle,
            self._issue(
                _PrimaryParentRouteHandle,
                upstream_handle=None,
                identity=identity,
                snapshot=_pending_identity_snapshot(identity),
                route=route,
                legacy_binding=legacy_binding,
                claim=None,
                role="primary_parent",
            ),
        )

    def bind_compensation_parent(
        self,
        operation_handle: CompensationHandle,
        parent: CommittedPrimaryOperationIdentityV1,
        handler_handle: object,
    ) -> _CompensationParentRouteHandle:
        route = self._operation_port.require_compensation(
            operation_handle,
            parent,
            handler_handle,
        )
        return cast(
            _CompensationParentRouteHandle,
            self._issue(
                _CompensationParentRouteHandle,
                upstream_handle=None,
                identity=parent,
                snapshot=(
                    parent.operation_id,
                    parent.primary_tool,
                    parent.operation_role,
                    parent.adapter_kind,
                    parent.status,
                    parent.terminal_payload_digest,
                ),
                route=route,
                legacy_binding=None,
                claim=None,
                role="compensation_parent",
            ),
        )

    def _require_record(
        self,
        handle: object,
        handle_type: type[_PendingRouteHandle],
        role: Literal["pending", "primary_parent", "compensation_parent"],
    ) -> _PendingIssuedRecord:
        self._ensure_integrity()
        if type(handle) is not handle_type:
            raise TypeError("Pending route handle has the wrong type")
        typed_handle = handle
        typed_handle._require(self)
        with self._lock:
            record = self._records.get(id(handle))
            if record is None or record.handle is not handle or record.role != role:
                raise ValueError("Pending route handle was not issued by this Port")
            identity = record.identity
            if type(identity) is PendingRouteIdentityV1:
                current_identity_snapshot = _pending_identity_snapshot(identity)
            elif type(identity) is CommittedPrimaryOperationIdentityV1:
                identity.__post_init__()
                current_identity_snapshot = (
                    identity.operation_id,
                    identity.primary_tool,
                    identity.operation_role,
                    identity.adapter_kind,
                    identity.status,
                    identity.terminal_payload_digest,
                )
            else:
                raise ValueError("Pending route identity has the wrong type")
            if current_identity_snapshot != record.identity_snapshot:
                raise ValueError("Pending route identity drift")
            if record.route is None:
                if record.legacy_binding is not None:
                    raise ValueError("Operationless Pending route carries a Legacy binding")
            elif record.route.adapter_kind == "legacy_deterministic":
                if type(record.legacy_binding) is not LegacyAdapterBindingV1:
                    raise ValueError("Legacy Pending route lost its exact Adapter binding")
            elif record.legacy_binding is not None:
                raise ValueError("Non-Legacy Pending route carries a Legacy binding")
            return record

    def _require_parent(self, handle: object) -> _PendingIssuedRecord:
        if type(handle) is _PrimaryParentRouteHandle:
            return self._require_record(handle, _PrimaryParentRouteHandle, "primary_parent")
        if type(handle) is _CompensationParentRouteHandle:
            return self._require_record(
                handle,
                _CompensationParentRouteHandle,
                "compensation_parent",
            )
        raise TypeError("Pending topology parent handle has the wrong type")

    def _require_child(self, handle: object) -> _PendingIssuedRecord:
        if type(handle) is TypedPendingRouteHandle:
            return self._require_record(handle, TypedPendingRouteHandle, "pending")
        if type(handle) is LegacyPendingRouteHandle:
            return self._require_record(handle, LegacyPendingRouteHandle, "pending")
        if type(handle) is ClarificationPendingRouteHandle:
            return self._require_record(handle, ClarificationPendingRouteHandle, "pending")
        raise TypeError("Pending topology child handle has the wrong type")

    def resolve_for_persistence(
        self,
        handle: PendingPersistenceRouteHandle,
        *,
        conversation_id: int,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
        pending_action_revision: int,
        pending_confirmation_claim_id: str,
        arguments_digest: str,
    ) -> _PendingRouteResolution:
        expected = (
            conversation_id,
            operation_id,
            tool_call_id,
            tool_name,
            pending_action_revision,
            pending_confirmation_claim_id,
            arguments_digest,
        )
        record = self._require_child(handle)
        if record.identity_snapshot != expected:
            raise ValueError("Pending persistence route identity mismatch")
        identity = cast(PendingRouteIdentityV1, record.identity)
        if type(handle) is TypedPendingRouteHandle:
            route = self.require_typed_pending(handle, identity, record.claim)
            return _PendingRouteResolution(identity, route, "typed", record.claim)
        if type(handle) is LegacyPendingRouteHandle:
            route = self.require_legacy_pending(handle, identity)
            return _PendingRouteResolution(identity, route, "legacy_deterministic", None)
        self.require_clarification_pending(cast(ClarificationPendingRouteHandle, handle), identity)
        return _PendingRouteResolution(identity, None, "clarification", None)

    def revoke_pending(self, handle: object) -> None:
        if not isinstance(handle, _PendingRouteHandle):
            raise TypeError("Pending route handle has the wrong type")
        handle._require_provenance(self)
        with handle._lifecycle.lock:
            if handle._lifecycle.active is not True:
                return
            handle._lifecycle.active = False
        with self._lock:
            record = self._records.get(id(handle))
            if record is not None and record.handle is handle:
                del self._records[id(handle)]


def build_pending_persistence_route_port(
    *,
    operation_port: ToolOperationMetadataPort,
) -> PendingPersistenceRoutePort:
    return PendingPersistenceRoutePort(operation_port=operation_port)


def require_pending_persistence_route(
    route_handle: PendingPersistenceRouteHandle,
    *,
    conversation_id: int,
    operation_id: str,
    tool_call_id: str,
    tool_name: str,
    pending_action_revision: int,
    pending_confirmation_claim_id: str,
    arguments_digest: str,
) -> _PendingRouteResolution:
    if not isinstance(route_handle, _PendingRouteHandle):
        raise TypeError("Pending persistence requires an exact route handle")
    return route_handle._port.resolve_for_persistence(
        route_handle,
        conversation_id=conversation_id,
        operation_id=operation_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        pending_action_revision=pending_action_revision,
        pending_confirmation_claim_id=pending_confirmation_claim_id,
        arguments_digest=arguments_digest,
    )


def require_chained_pending_transition(
    parent_route_handle: object,
    child_route_handle: PendingPersistenceRouteHandle,
) -> None:
    if not isinstance(parent_route_handle, _PendingRouteHandle):
        raise TypeError("Chained Pending requires an exact parent route")
    if not isinstance(child_route_handle, _PendingRouteHandle):
        raise TypeError("Chained Pending requires an exact child route")
    if parent_route_handle._port is not child_route_handle._port:
        raise ValueError("Chained Pending routes have different Port provenance")
    parent_route_handle._port.chained_topology_policy.require_transition(
        parent_route_handle,
        child_route_handle,
    )


def pending_route_claim_for_cleanup(
    route_handle: PendingPersistenceRouteHandle,
) -> object | None:
    if type(route_handle) is not TypedPendingRouteHandle:
        return None
    record = route_handle._port._require_record(
        route_handle,
        TypedPendingRouteHandle,
        "pending",
    )
    return record.claim


def abandon_pending_persistence_route(
    route_handle: PendingPersistenceRouteHandle,
) -> None:
    """Revoke an unconsumed exact Pending route and its Typed authority claim."""

    if not isinstance(route_handle, _PendingRouteHandle):
        raise TypeError("Pending persistence requires an exact route handle")
    port = route_handle._port
    claim = pending_route_claim_for_cleanup(route_handle)
    if claim is not None:
        if type(claim) is not PendingAuthorityClaim:
            raise AuthorityPhaseError("Typed Pending claim has an invalid type")
        from offerpilot.ai.tool_authority import composition

        with composition._ACTIVE_AUTHORITIES_LOCK:
            found = composition._ACTIVE_OBJECTS.get(id(claim))
        if found is None or found[0] is not claim or type(found[1]) is not AuthorityFactory:
            raise AuthorityPhaseError("Typed Pending claim is not active")
        found[1]._revoke_lifecycle_after_entry_failure(claim, owns_in_flight=False)
    port.revoke_pending(route_handle)


class WriteOperationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class LedgerKeyDomain:
    key_id: str
    secret: bytes


class _Transient:
    __slots__ = ()

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("transient write operation value cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("transient write operation value cannot be serialized")


class DeliveryOwnership(_Transient):
    __slots__ = (
        "operation_id",
        "generation",
        "__raw_token",
        "fingerprint",
        "parent_route_handle",
        "child_route_handle",
    )
    parent_route_handle: object | None
    child_route_handle: PendingPersistenceRouteHandle | None

    def __init__(
        self, operation_id: str, generation: int, raw_token: bytes, fingerprint: str
    ) -> None:
        self.operation_id = operation_id
        self.generation = generation
        self.__raw_token = raw_token
        self.fingerprint = fingerprint
        self.parent_route_handle: object | None = None
        self.child_route_handle: object | None = None

    def bind_parent_route(self, handle: object) -> None:
        if self.parent_route_handle is not None:
            raise ValueError("delivery parent route is already bound")
        if type(handle) not in {_PrimaryParentRouteHandle, _CompensationParentRouteHandle}:
            raise TypeError("delivery parent route has the wrong type")
        self.parent_route_handle = handle

    def bind_chained_transition(
        self,
        child_route_handle: PendingPersistenceRouteHandle,
    ) -> None:
        if self.child_route_handle is not None:
            raise ValueError("delivery child route is already bound")
        parent_route_handle = self.parent_route_handle
        if parent_route_handle is None:
            raise TypeError("chained Pending delivery requires an exact parent route")
        require_chained_pending_transition(parent_route_handle, child_route_handle)
        self.child_route_handle = child_route_handle

    def require_chained_transition(
        self,
        *,
        parent: WriteOperation,
        child: WriteOperation,
    ) -> None:
        parent_route_handle = self.parent_route_handle
        child_route_handle = self.child_route_handle
        if parent_route_handle is None or child_route_handle is None:
            raise TypeError("chained Pending delivery route is incomplete")
        if not isinstance(parent_route_handle, _PendingRouteHandle):
            raise TypeError("chained Pending delivery parent route is invalid")
        require_chained_pending_transition(parent_route_handle, child_route_handle)
        port = parent_route_handle._port
        parent_record = port._require_parent(parent_route_handle)
        child_record = port._require_child(child_route_handle)
        parent_identity = parent_record.identity
        child_identity = child_record.identity
        if (
            type(parent_identity) is not PendingRouteIdentityV1
            or type(child_identity) is not PendingRouteIdentityV1
            or parent_record.route is None
            or child_record.route is None
            or parent_identity.operation_id != parent.id
            or parent_identity.conversation_id != parent.conversation_id
            or parent_identity.tool_call_id != parent.tool_call_id
            or parent_identity.tool_name != parent.tool_name
            or parent_record.route.adapter_kind != parent.adapter_kind
            or child_identity.operation_id != child.id
            or child_identity.conversation_id != child.conversation_id
            or child_identity.tool_call_id != child.tool_call_id
            or child_identity.tool_name != child.tool_name
            or child_record.route.adapter_kind != child.adapter_kind
        ):
            raise ValueError("chained Pending delivery operation identity mismatch")

    def revoke_parent_route(self) -> None:
        child = self.child_route_handle
        if child is not None:
            if not isinstance(child, _PendingRouteHandle):
                raise ValueError("delivery child route integrity drift")
            child._port.revoke_pending(child)
            self.child_route_handle = None
        handle = self.parent_route_handle
        if handle is None:
            return
        if not isinstance(handle, _PendingRouteHandle):
            raise ValueError("delivery parent route integrity drift")
        handle._port.revoke_pending(handle)
        self.parent_route_handle = None

    @property
    def raw_token(self) -> bytes:
        return self.__raw_token

    def __repr__(self) -> str:
        return (
            "DeliveryOwnership(operation_id="
            f"{self.operation_id!r}, generation={self.generation!r}, "
            f"fingerprint={self.fingerprint!r})"
        )

    def public_identity(self) -> dict[str, str | int]:
        """Return the only representation safe for logs, events, and transport."""

        return {
            "operation_id": self.operation_id,
            "generation": self.generation,
            "fingerprint": self.fingerprint,
        }


class DeliveryHeartbeat(_Transient):
    def __init__(
        self, repository: "WriteOperationRepository", ownership: DeliveryOwnership
    ) -> None:
        self._repository = repository
        self._ownership = ownership
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> "DeliveryHeartbeat":
        self._thread.start()
        return self

    def fence(self) -> bool:
        return not self._stop.is_set() and self._repository.heartbeat(self._ownership)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            while not self._stop.wait(DELIVERY_OWNER_HEARTBEAT_SECONDS):
                if not self._repository.heartbeat(self._ownership):
                    self._stop.set()
                    return
        except Exception:
            self._stop.set()


@dataclass(frozen=True, slots=True, repr=False)
class LedgerPendingPointer:
    """Bounded Conversation projection used before a confirmation decision.

    Deliberately absent are Pending arguments, human text, generation fields,
    and every lazy ORM relationship.  This is an identity/CAS carrier only.
    """

    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    pending_confirmation_claim_id: str


@dataclass(frozen=True, slots=True, repr=False)
class LedgerOperationPreheader:
    """One Ledger row plus its exact, bounded owning-Conversation pointer."""

    operation: WriteOperation
    pending_pointer: LedgerPendingPointer | None


@dataclass(frozen=True)
class TerminalPayload:
    status: Literal["committed", "failed", "rejected"]
    result_contract: str
    result_json: str
    visible_result: str
    transport_json: str
    undo_json: str | None
    failure_category: str | None
    failure_code: str | None
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedPendingReplay(_Transient):
    """One operation-owned Pending snapshot verified after delivery topology."""

    adapter_kind: Literal["typed", "legacy_deterministic"]
    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    raw_args: str = field(repr=False)
    human: str
    confirmation_token_fingerprint: str = field(repr=False)
    decoded_args: dict[str, JSONValue] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationCommitted(_Transient):
    operation_id: str
    payload: TerminalPayload
    ownership: DeliveryOwnership | None
    replayed: bool = False


@dataclass(frozen=True)
class OperationFailed(_Transient):
    operation_id: str
    payload: TerminalPayload
    ownership: DeliveryOwnership | None
    replayed: bool = False


@dataclass(frozen=True)
class OperationReplay(_Transient):
    operation_id: str
    payload: TerminalPayload
    delivery_status: str
    delivery_generation: int
    delivery_lease_expires_at: int | None
    delivery_outcome: str | None = None
    final_message: str | None = None
    replayed: bool = True
    chained_pending: VerifiedPendingReplay | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationUnknown(_Transient):
    operation_id: str
    code: str
    retryable: bool


OperationExecution = OperationCommitted | OperationFailed | OperationReplay | OperationUnknown


def load_or_create_ledger_key(
    data_dir: Path,
    session_factory: sessionmaker[Session],
) -> LedgerKeyDomain:
    key_path = data_dir.resolve() / LEDGER_KEY_FILENAME
    if key_path.exists():
        return _read_ledger_key(key_path)
    with session_factory() as session:
        existing = session.scalar(select(func.count()).select_from(WriteOperation)) or 0
    if existing:
        raise WriteOperationError("operation_unavailable")
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = key_path.with_name(f".{LEDGER_KEY_FILENAME}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        if key_path.exists():
            return _read_ledger_key(key_path)
        raise WriteOperationError("operation_unavailable") from exc
    temp_path = key_path.with_name(f".{LEDGER_KEY_FILENAME}.{uuid4().hex}.tmp")
    try:
        os.close(lock_fd)
        if key_path.exists():
            return _read_ledger_key(key_path)
        with session_factory() as session:
            existing = session.scalar(select(func.count()).select_from(WriteOperation)) or 0
        if existing:
            raise WriteOperationError("operation_unavailable")
        domain = LedgerKeyDomain(str(uuid4()), secrets.token_bytes(32))
        encoded = base64.urlsafe_b64encode(domain.secret).decode("ascii").rstrip("=")
        payload = canonical_json({"schema_version": 1, "key_id": domain.key_id, "secret": encoded})
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(temp_path, flags, 0o600)
        try:
            os.write(fd, (payload + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, key_path)
        if os.name != "nt":
            os.chmod(key_path, 0o600)
        return domain
    except WriteOperationError:
        raise
    except Exception as exc:
        raise WriteOperationError("operation_unavailable") from exc
    finally:
        try:
            temp_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_ledger_key(path: Path) -> LedgerKeyDomain:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "key_id", "secret"}:
            raise ValueError
        key_id = str(UUID(cast(str, payload["key_id"])))
        if payload["schema_version"] != 1 or payload["key_id"] != key_id:
            raise ValueError
        raw = cast(str, payload["secret"])
        secret = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        if len(secret) != 32:
            raise ValueError
        if os.name != "nt":
            os.chmod(path, 0o600)
        return LedgerKeyDomain(key_id, secret)
    except Exception as exc:
        raise WriteOperationError("operation_unavailable") from exc


def ledger_fingerprint(key: LedgerKeyDomain, domain: str, value: JSONValue | bytes) -> str:
    encoded = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    digest = hmac.new(
        key.secret,
        domain.encode("ascii") + b"\0" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return "hmac-sha256:" + digest


def operation_request_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_id: str,
    tool_call_id: str,
    approved: bool,
    edited_args_present: bool,
    edited_args: Mapping[str, JSONValue] | None,
    rejection_feedback_present: bool,
    rejection_feedback: str,
    confirmation_token_fingerprint: str,
    proposal_fingerprint: str,
) -> str:
    value: dict[str, JSONValue] = {
        "request_kind": "confirmation_v1",
        "operation_id": operation_id,
        "tool_call_id": tool_call_id,
        "decision": "approved" if approved else "rejected",
        "edited_args_present": edited_args_present,
        "edited_args": dict(edited_args or {}) if edited_args_present else None,
        "rejection_feedback_present": rejection_feedback_present,
        "rejection_feedback": rejection_feedback if rejection_feedback_present else None,
        "confirmation_token_fingerprint": confirmation_token_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
    }
    return ledger_fingerprint(key, "write-operation-request-v1", value)


def compensation_operation_id(parent_operation_id: str, compensation_kind: str) -> str:
    parent = str(UUID(parent_operation_id))
    if type(compensation_kind) is not str or not compensation_kind:
        raise ValueError("compensation kind is required")
    return str(uuid5(COMPENSATION_ID_NAMESPACE, parent + ":" + compensation_kind))


def _json_value(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite result")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__table__"):
        return {
            str(column.name): _json_value(getattr(value, column.name))
            for column in value.__table__.columns
        }
    raise ValueError("unsupported result value")


def build_terminal_payload(
    *,
    status: Literal["committed", "failed", "rejected"],
    result_contract: str,
    result: Any,
    visible_result: str,
    transport: Mapping[str, Any] | None,
    undo: Mapping[str, Any] | None,
    failure_category: str | None,
    failure_code: str | None,
    budgets: tuple[int, int, int, int] = (512 * 1024, 256 * 1024, 128 * 1024, 64 * 1024),
) -> TerminalPayload:
    result_json = canonical_json(_json_value(result))
    transport_json = canonical_json(_json_value(dict(transport or {})))
    undo_json = canonical_json(_json_value(dict(undo))) if undo is not None else None
    encoded = (
        result_json.encode("utf-8"),
        visible_result.encode("utf-8"),
        transport_json.encode("utf-8"),
        undo_json.encode("utf-8") if undo_json is not None else b"",
    )
    if any(len(item) > limit for item, limit in zip(encoded, budgets)):
        raise WriteOperationError("operation_result_too_large")
    envelope: dict[str, JSONValue] = {
        "status": status,
        "result_contract": result_contract,
        "result_json": json.loads(result_json),
        "visible_result": visible_result,
        "transport_json": json.loads(transport_json),
        "undo_json": json.loads(undo_json) if undo_json is not None else None,
        "failure_category": failure_category,
        "failure_code": failure_code,
    }
    canonical = canonical_json(envelope).encode("utf-8")
    if len(canonical) > 1024 * 1024:
        raise WriteOperationError("operation_result_too_large")
    return TerminalPayload(
        status=status,
        result_contract=result_contract,
        result_json=result_json,
        visible_result=visible_result,
        transport_json=transport_json,
        undo_json=undo_json,
        failure_category=failure_category,
        failure_code=failure_code,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def payload_from_operation(operation: WriteOperation) -> TerminalPayload:
    if operation.status not in _TERMINAL_STATUSES:
        raise WriteOperationError("operation_not_committed", retryable=True)
    payload = build_terminal_payload(
        status=cast(Any, operation.status),
        result_contract=operation.result_contract or "",
        result=json.loads(operation.result_json or "null"),
        visible_result=operation.visible_result or "",
        transport=json.loads(operation.transport_json or "{}"),
        undo=json.loads(operation.undo_json) if operation.undo_json is not None else None,
        failure_category=operation.failure_category,
        failure_code=operation.failure_code,
    )
    if not hmac.compare_digest(payload.digest, operation.terminal_payload_sha256 or ""):
        raise WriteOperationError("operation_integrity_error")
    return payload


def _undo_digest(undo_json: str | None) -> str | None:
    if undo_json is None:
        return None
    canonical = canonical_json(json.loads(undo_json)).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _chained_manifest_v1(operation: WriteOperation | None) -> JSONValue:
    if operation is None:
        return None
    return {
        "operation_id": operation.id,
        "tool_call_id": operation.tool_call_id,
        "tool_name": operation.tool_name,
        "proposal_fingerprint": operation.proposal_fingerprint,
    }


def _chained_manifest_v2(
    operation: WriteOperation | None,
    *,
    pending_args: str | None,
    pending_human: str | None,
) -> JSONValue:
    if operation is None:
        return None
    if pending_args is None or pending_human is None:
        raise WriteOperationError("operation_delivery_unknown")
    return {
        "operation_id": operation.id,
        "tool_call_id": operation.tool_call_id,
        "tool_name": operation.tool_name,
        "adapter_kind": operation.adapter_kind,
        "proposal_fingerprint": operation.proposal_fingerprint,
        "confirmation_token_fingerprint": operation.confirmation_token_fingerprint,
        "pending_args": pending_args,
        "pending_human": pending_human,
    }


def _validated_delivery_adapter(
    operation: WriteOperation,
    child: WriteOperation,
) -> Literal["typed", "legacy_deterministic"] | None:
    """Return the only adapter topology that delivery replay can verify."""

    if operation.adapter_kind == "typed" and child.adapter_kind == "typed":
        return "typed"
    if (
        operation.adapter_kind == "legacy_deterministic"
        and child.adapter_kind == "legacy_deterministic"
        and child.tool_name == operation.tool_name
    ):
        return "legacy_deterministic"
    return None


def _pending_confirmation_token(
    tool_call_id: str,
    tool_name: str,
    canonical_arguments: Mapping[str, JSONValue],
) -> str:
    identity = json.dumps(
        [tool_call_id, tool_name, canonical_json(dict(canonical_arguments))],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _legacy_pending_confirmation_token(
    tool_call_id: str,
    tool_name: str,
    raw_arguments: str,
) -> tuple[str, JSONValue]:
    """Run the exact closed Legacy token codec and retain its decoded value."""

    try:
        decoded = cast(JSONValue, json.loads(raw_arguments))
        canonical_arguments = json.dumps(
            decoded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        decoded = raw_arguments
        canonical_arguments = raw_arguments
    identity = json.dumps(
        [tool_call_id, tool_name, canonical_arguments],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), decoded


def _valid_delivery_messages(
    operation: WriteOperation,
    messages: Sequence[ChatMessage],
) -> bool:
    if (
        len(messages) < 2
        or [item.delivery_ordinal for item in messages] != list(range(len(messages)))
        or messages[0].role != "tool"
        or messages[0].tool_call_id != operation.tool_call_id
        or messages[0].delivery_kind != "origin_tool_result"
        or any(
            item.operation_id != operation.id or item.conversation_id != operation.conversation_id
            for item in messages
        )
    ):
        return False
    return all(
        item.delivery_kind == "continuation_message"
        and (
            (item.role == "assistant" and item.tool_call_id == "")
            or (item.role == "tool" and item.tool_call_id != "")
        )
        for item in messages[1:]
    )


class WriteOperationRepository:
    def __init__(self, session_factory: sessionmaker[Session], key: LedgerKeyDomain):
        self.session_factory = session_factory
        self.key = key
        bind = session_factory.kw.get("bind")
        database = getattr(getattr(bind, "url", None), "database", None)
        self._database_gate = database_coordinator(str(database)) if database else None

    def get(self, operation_id: str) -> WriteOperation | None:
        with self.session_factory() as session:
            return session.get(WriteOperation, operation_id)

    def operation_preheader(
        self,
        *,
        conversation_id: int,
        operation_id: str | None,
    ) -> LedgerOperationPreheader:
        """Load the confirmation identity without selecting Pending content.

        An explicit operation id is always the first database read.  When the
        id is omitted, the only bootstrap read is the five-column Conversation
        pointer projection; the Ledger is never scanned or guessed.
        """

        with self.session_factory() as session:
            operation: WriteOperation | None = None
            if operation_id is not None:
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if operation.conversation_id != conversation_id:
                    raise WriteOperationError("operation_identity_conflict")
                if operation.status in _TERMINAL_STATUSES:
                    return LedgerOperationPreheader(operation, None)

            pointer_row = session.execute(
                select(
                    Conversation.id,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                    Conversation.pending_confirmation_claim_id,
                ).where(Conversation.id == conversation_id)
            ).one_or_none()
            if pointer_row is None:
                raise WriteOperationError("operation_unavailable")
            pointer = LedgerPendingPointer(
                conversation_id=int(pointer_row.id),
                operation_id=str(pointer_row.pending_operation_id or ""),
                tool_call_id=str(pointer_row.pending_tool_call_id or ""),
                tool_name=str(pointer_row.pending_tool_name or ""),
                pending_confirmation_claim_id=str(pointer_row.pending_confirmation_claim_id or ""),
            )
            if operation is None:
                if not pointer.operation_id:
                    raise WriteOperationError("stale_pending_action")
                operation = session.get(WriteOperation, pointer.operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if operation.conversation_id != conversation_id:
                    raise WriteOperationError("operation_identity_conflict")
            return LedgerOperationPreheader(operation, pointer)

    def create_primary(
        self,
        session: Session,
        *,
        route_handle: PendingPersistenceRouteHandle,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        pending_action_revision: int,
        pending_confirmation_claim_id: str,
        arguments_digest: str,
        proposal_fingerprint: str,
        confirmation_token_fingerprint: str,
        authorization_scope_fingerprint: str | None = None,
        agent_run_id: str | None = None,
    ) -> WriteOperation:
        route = require_pending_persistence_route(
            route_handle,
            conversation_id=conversation_id,
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            pending_action_revision=pending_action_revision,
            pending_confirmation_claim_id=pending_confirmation_claim_id,
            arguments_digest=arguments_digest,
        )
        if route.route is None or route.adapter_kind == "clarification":
            raise WriteOperationError("operation_not_transactional")
        adapter_kind = route.adapter_kind
        operation = WriteOperation(
            id=str(UUID(operation_id)),
            operation_role="primary",
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            adapter_kind=adapter_kind,
            status="proposed",
            fingerprint_key_id=self.key.key_id,
            proposal_fingerprint=proposal_fingerprint,
            confirmation_token_fingerprint=confirmation_token_fingerprint,
            authorization_scope_fingerprint=authorization_scope_fingerprint,
            delivery_status="pending",
            delivery_generation=0,
        )
        session.add(operation)
        self.append_transition(session, operation.id, 1, "proposed")
        session.flush()
        return operation

    @staticmethod
    def append_transition(
        session: Session,
        operation_id: str,
        seq: int,
        state: str,
    ) -> None:
        session.add(
            WriteOperationTransition(
                id=str(uuid4()), operation_id=operation_id, seq=seq, state=state
            )
        )

    def replay(
        self,
        operation: WriteOperation,
        request_fingerprint: str,
    ) -> OperationReplay:
        if operation.fingerprint_key_id != self.key.key_id:
            raise WriteOperationError("operation_unavailable")
        if not operation.operation_request_fingerprint or not hmac.compare_digest(
            operation.operation_request_fingerprint, request_fingerprint
        ):
            raise WriteOperationError("operation_input_conflict")
        payload = payload_from_operation(operation)
        final_message = None
        chained_pending = None
        if operation.delivery_status in {"completed", "failed"}:
            try:
                with self.session_factory() as session:
                    current = session.get(WriteOperation, operation.id)
                    if current is None:
                        raise WriteOperationError("operation_delivery_unknown", retryable=True)
                    final_message, chained_pending = self._verify_delivery(session, current)
            except WriteOperationError:
                raise
            except Exception as exc:
                raise WriteOperationError("operation_delivery_unknown", retryable=True) from exc
        return OperationReplay(
            operation.id,
            payload,
            operation.delivery_status,
            operation.delivery_generation,
            operation.delivery_lease_expires_at,
            operation.delivery_outcome,
            final_message,
            chained_pending=chained_pending,
        )

    def _verify_delivery(
        self, session: Session, operation: WriteOperation
    ) -> tuple[str, VerifiedPendingReplay | None]:
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.operation_id == operation.id)
                .order_by(ChatMessage.delivery_ordinal.asc())
            )
        )
        if len(messages) != operation.delivery_message_count or not _valid_delivery_messages(
            operation, messages
        ):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        manifest_messages: list[JSONValue] = [
            {
                "role": item.role,
                "content": item.content,
                "tool_calls": item.tool_calls,
                "tool_call_id": item.tool_call_id,
                "provider_blocks": item.provider_blocks,
                "delivery_kind": item.delivery_kind,
                "delivery_ordinal": item.delivery_ordinal,
            }
            for item in messages
        ]
        child = (
            session.get(WriteOperation, operation.delivery_next_operation_id)
            if operation.delivery_next_operation_id
            else None
        )
        adapter_kind: Literal["typed", "legacy_deterministic"] | None = None
        pending_row = None
        if operation.delivery_outcome == "chained_pending":
            if (
                child is None
                or operation.operation_role != "primary"
                or child.operation_role != "primary"
                or child.status != "proposed"
                or child.conversation_id != operation.conversation_id
            ):
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            adapter_kind = _validated_delivery_adapter(operation, child)
            if adapter_kind is None:
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            pending_row = session.execute(
                select(
                    Conversation.id,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                    Conversation.pending_args,
                    Conversation.pending_human,
                )
                .where(Conversation.id == operation.conversation_id)
                .where(Conversation.pending_operation_id == child.id)
            ).one_or_none()
            if (
                pending_row is None
                or pending_row.pending_operation_id != child.id
                or pending_row.pending_tool_call_id != child.tool_call_id
                or pending_row.pending_tool_name != child.tool_name
            ):
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
        elif operation.delivery_next_operation_id is not None:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        manifest_v2: dict[str, JSONValue] = {
            "delivery_manifest_version": 2,
            "operation_id": operation.id,
            "status": operation.status,
            "terminal_payload_sha256": operation.terminal_payload_sha256,
            "delivery_generation": operation.delivery_generation,
            "messages": manifest_messages,
            "outcome": operation.delivery_outcome,
            "failure_code": operation.delivery_failure_code,
            "next_operation_id": operation.delivery_next_operation_id,
            "old_pending_disposition": (
                "replaced" if operation.delivery_outcome == "chained_pending" else "cleared"
            ),
            "validated_undo_digest": _undo_digest(operation.undo_json),
            "chained_operation": _chained_manifest_v2(
                child,
                pending_args=(pending_row.pending_args if pending_row is not None else None),
                pending_human=(pending_row.pending_human if pending_row is not None else None),
            ),
        }
        stored_digest = operation.delivery_manifest_sha256 or ""
        v2_digest = (
            "sha256:" + hashlib.sha256(canonical_json(manifest_v2).encode("utf-8")).hexdigest()
        )
        historical_v1 = False
        if not hmac.compare_digest(v2_digest, stored_digest):
            manifest_v1 = dict(manifest_v2)
            del manifest_v1["delivery_manifest_version"]
            manifest_v1["chained_operation"] = _chained_manifest_v1(child)
            v1_digest = (
                "sha256:" + hashlib.sha256(canonical_json(manifest_v1).encode("utf-8")).hexdigest()
            )
            if not hmac.compare_digest(v1_digest, stored_digest):
                if adapter_kind is not None:
                    raise WriteOperationError("operation_integrity_error")
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            historical_v1 = True
        final = next(
            (item.content for item in reversed(messages) if item.role == "assistant"), None
        )
        if final is None:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        if adapter_kind is None:
            return final, None

        assert child is not None and pending_row is not None

        decoded_args: dict[str, JSONValue] | None = None
        if adapter_kind == "typed":
            try:
                decoded_args = PendingReplayArgsDecoderV1().decode(pending_row.pending_args)
            except PendingReplayIntegrityError as exc:
                raise WriteOperationError("operation_integrity_error") from exc
            expected_proposal = ledger_fingerprint(
                self.key, "write-operation-proposal-v1", decoded_args
            )
            if not hmac.compare_digest(expected_proposal, child.proposal_fingerprint or ""):
                raise WriteOperationError("operation_integrity_error")
            token = _pending_confirmation_token(
                child.tool_call_id or "", child.tool_name, decoded_args
            )
            expected_token = ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                token.encode("ascii"),
            )
            if not hmac.compare_digest(expected_token, child.confirmation_token_fingerprint or ""):
                raise WriteOperationError("operation_integrity_error")

        if adapter_kind == "legacy_deterministic":
            token, legacy_arguments = _legacy_pending_confirmation_token(
                child.tool_call_id or "",
                child.tool_name,
                pending_row.pending_args,
            )
            expected_proposal = ledger_fingerprint(
                self.key, "write-operation-proposal-v1", legacy_arguments
            )
            if not hmac.compare_digest(expected_proposal, child.proposal_fingerprint or ""):
                raise WriteOperationError("operation_integrity_error")
            expected_token = ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                token.encode("ascii"),
            )
            if not hmac.compare_digest(expected_token, child.confirmation_token_fingerprint or ""):
                raise WriteOperationError("operation_integrity_error")
            if not isinstance(legacy_arguments, dict):
                raise WriteOperationError("operation_integrity_error")
            decoded_args = legacy_arguments

        return final, VerifiedPendingReplay(
            adapter_kind=adapter_kind,
            conversation_id=int(pending_row.id),
            operation_id=child.id,
            tool_call_id=child.tool_call_id or "",
            tool_name=child.tool_name,
            raw_args=pending_row.pending_args,
            human=("请确认此待处理操作。" if historical_v1 else pending_row.pending_human),
            confirmation_token_fingerprint=child.confirmation_token_fingerprint or "",
            decoded_args=decoded_args,
        )

    def prepare_owner(self, operation_id: str, generation: int = 1) -> DeliveryOwnership:
        raw = secrets.token_bytes(32)
        fingerprint = ledger_fingerprint(self.key, "write-operation-delivery-owner-v1", raw)
        return DeliveryOwnership(operation_id, generation, raw, fingerprint)

    def complete_delivery(
        self,
        session: Session,
        ownership: DeliveryOwnership,
        *,
        outcome: Literal["final_response", "chained_pending", "fallback"],
        next_operation_id: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        expected = ledger_fingerprint(
            self.key, "write-operation-delivery-owner-v1", ownership.raw_token
        )
        if not hmac.compare_digest(expected, ownership.fingerprint):
            return False
        operation = session.get(WriteOperation, ownership.operation_id)
        sqlite_now = session.scalar(select(func.unixepoch("now")))
        if (
            operation is None
            or operation.adapter_kind == "product_action"
            or operation.delivery_status != "pending"
            or operation.delivery_generation != ownership.generation
            or not hmac.compare_digest(
                operation.delivery_owner_token_fingerprint or "", ownership.fingerprint
            )
            or operation.delivery_lease_expires_at is None
            or sqlite_now is None
            or operation.delivery_lease_expires_at <= int(sqlite_now)
        ):
            return False
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.operation_id == operation.id)
                .order_by(ChatMessage.delivery_ordinal.asc())
            )
        )
        if not _valid_delivery_messages(operation, messages):
            raise WriteOperationError("operation_delivery_unknown")
        manifest_messages: list[JSONValue] = [
            {
                "role": item.role,
                "content": item.content,
                "tool_calls": item.tool_calls,
                "tool_call_id": item.tool_call_id,
                "provider_blocks": item.provider_blocks,
                "delivery_kind": item.delivery_kind,
                "delivery_ordinal": item.delivery_ordinal,
            }
            for item in messages
        ]
        child = session.get(WriteOperation, next_operation_id) if next_operation_id else None
        pending_identity = None
        if outcome == "chained_pending":
            if child is None:
                raise WriteOperationError("operation_delivery_unknown")
            try:
                ownership.require_chained_transition(
                    parent=operation,
                    child=child,
                )
            except (TypeError, ValueError):
                raise WriteOperationError("operation_delivery_unknown") from None
            pending_identity = session.execute(
                select(
                    Conversation.id,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                    Conversation.pending_args,
                    Conversation.pending_human,
                ).where(Conversation.id == operation.conversation_id)
            ).one_or_none()
            if (
                child is None
                or child.operation_role != "primary"
                or child.status != "proposed"
                or child.conversation_id != operation.conversation_id
                or pending_identity is None
                or pending_identity.pending_operation_id != child.id
                or pending_identity.pending_tool_call_id != child.tool_call_id
                or pending_identity.pending_tool_name != child.tool_name
                or _validated_delivery_adapter(operation, child) is None
            ):
                raise WriteOperationError("operation_delivery_unknown")
        else:
            if ownership.child_route_handle is not None:
                raise WriteOperationError("operation_delivery_unknown")
            conversation_id = session.scalar(
                select(Conversation.id)
                .where(Conversation.id == operation.conversation_id)
                .where(Conversation.pending_operation_id != operation.id)
            )
            if conversation_id is None or next_operation_id is not None:
                raise WriteOperationError("operation_delivery_unknown")
        manifest: dict[str, JSONValue] = {
            "delivery_manifest_version": 2,
            "operation_id": operation.id,
            "status": operation.status,
            "terminal_payload_sha256": operation.terminal_payload_sha256,
            "delivery_generation": ownership.generation,
            "messages": manifest_messages,
            "outcome": outcome,
            "failure_code": failure_code,
            "next_operation_id": next_operation_id,
            "old_pending_disposition": ("replaced" if outcome == "chained_pending" else "cleared"),
            "validated_undo_digest": _undo_digest(operation.undo_json),
            "chained_operation": _chained_manifest_v2(
                child,
                pending_args=(
                    pending_identity.pending_args if pending_identity is not None else None
                ),
                pending_human=(
                    pending_identity.pending_human if pending_identity is not None else None
                ),
            ),
        }
        digest = "sha256:" + hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        operation.delivery_status = "failed" if outcome == "fallback" else "completed"
        operation.delivery_failure_code = failure_code if outcome == "fallback" else None
        operation.delivery_outcome = outcome
        operation.delivery_message_count = len(messages)
        operation.delivery_manifest_sha256 = digest
        operation.delivery_next_operation_id = next_operation_id
        operation.delivery_owner_token_fingerprint = None
        operation.delivery_lease_expires_at = None
        operation.delivered_at = datetime.now(timezone.utc)
        operation.updated_at = datetime.now(timezone.utc)
        session.flush()
        ownership.revoke_parent_route()
        return True

    def heartbeat(self, ownership: DeliveryOwnership) -> bool:
        expected = ledger_fingerprint(
            self.key, "write-operation-delivery-owner-v1", ownership.raw_token
        )
        if not hmac.compare_digest(expected, ownership.fingerprint):
            return False
        try:
            gate = (
                self._database_gate.writer(monotonic() + WORK_DEADLINE_SECONDS)
                if self._database_gate is not None
                else nullcontext()
            )
            with gate:
                with self.session_factory() as session:
                    result = session.execute(
                        update(WriteOperation)
                        .where(WriteOperation.id == ownership.operation_id)
                        .where(WriteOperation.adapter_kind != "product_action")
                        .where(WriteOperation.delivery_status == "pending")
                        .where(WriteOperation.delivery_generation == ownership.generation)
                        .where(
                            WriteOperation.delivery_owner_token_fingerprint == ownership.fingerprint
                        )
                        .where(WriteOperation.delivery_lease_expires_at > func.unixepoch("now"))
                        .values(
                            delivery_lease_expires_at=func.unixepoch("now")
                            + DELIVERY_OWNER_LEASE_SECONDS
                        )
                    )
                    session.commit()
                    return getattr(result, "rowcount", 0) == 1
        except Exception:
            return False

    def converge_expired_delivery(self, operation_id: str) -> OperationReplay | OperationUnknown:
        try:
            with self.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                now_epoch = int(session.scalar(select(func.unixepoch("now"))) or 0)
                operation = session.get(WriteOperation, operation_id)
                if (
                    operation is None
                    or operation.adapter_kind == "product_action"
                    or operation.status not in _TERMINAL_STATUSES
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_result_unknown", True)
                payload = payload_from_operation(operation)
                if operation.conversation_id is None:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if operation.delivery_status != "pending":
                    session.rollback()
                    return OperationReplay(
                        operation.id,
                        payload,
                        operation.delivery_status,
                        operation.delivery_generation,
                        operation.delivery_lease_expires_at,
                    )
                if (
                    operation.delivery_lease_expires_at is not None
                    and operation.delivery_lease_expires_at > now_epoch
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_pending", True)
                messages = list(
                    session.scalars(
                        select(ChatMessage)
                        .where(ChatMessage.operation_id == operation.id)
                        .order_by(ChatMessage.delivery_ordinal.asc())
                    )
                )
                if len(messages) > 1:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if messages and (
                    messages[0].delivery_ordinal != 0
                    or messages[0].role != "tool"
                    or messages[0].tool_call_id != (operation.tool_call_id or "")
                    or messages[0].delivery_kind != "origin_tool_result"
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if not messages:
                    session.add(
                        ChatMessage(
                            conversation_id=operation.conversation_id,
                            role="tool",
                            content=payload.visible_result,
                            tool_call_id=operation.tool_call_id or "",
                            operation_id=operation.id,
                            delivery_kind="origin_tool_result",
                            delivery_ordinal=0,
                        )
                    )
                session.add(
                    ChatMessage(
                        conversation_id=operation.conversation_id,
                        role="assistant",
                        content=(
                            "操作已提交，但后续说明生成失败。"
                            if operation.status == "committed"
                            else "操作未完成，请查看工具结果后重试。"
                        ),
                        operation_id=operation.id,
                        delivery_kind="continuation_message",
                        delivery_ordinal=1,
                    )
                )
                takeover = self.prepare_owner(operation.id, operation.delivery_generation + 1)
                operation.delivery_generation = takeover.generation
                operation.delivery_owner_token_fingerprint = takeover.fingerprint
                operation.delivery_lease_expires_at = now_epoch + DELIVERY_OWNER_LEASE_SECONDS
                conversation_update = (
                    update(Conversation)
                    .where(Conversation.id == operation.conversation_id)
                    .where(Conversation.pending_operation_id == operation.id)
                    .values(
                        pending_operation_id="",
                        pending_tool_call_id="",
                        pending_tool_name="",
                        pending_args="",
                        pending_human="",
                        pending_confirmation_claim_id="",
                        pending_confirmation_claimed_at=None,
                    )
                )
                if operation.status == "committed" and operation.undo_json:
                    conversation_update = conversation_update.values(
                        last_write_undo_json=operation.undo_json,
                        last_write_operation_id=operation.id,
                    )
                elif operation.status != "rejected":
                    conversation_update = conversation_update.values(
                        last_write_undo_json="",
                        last_write_operation_id="",
                    )
                session.execute(conversation_update)
                session.flush()
                if not self.complete_delivery(
                    session,
                    takeover,
                    outcome="fallback",
                    failure_code="operation_delivery_failed",
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                session.commit()
                return OperationReplay(
                    operation.id,
                    payload,
                    "failed",
                    takeover.generation,
                    None,
                )
        except OperationalError:
            return OperationUnknown(operation_id, "operation_busy", True)


class LegacyApprovedBoundRoute(Protocol):
    """One same-transaction approved Legacy proof route."""

    def prepared_call(self) -> PreparedLegacyCall: ...

    def prepared_input_port(self) -> LegacyPreparedInputPort: ...

    def accept_prepared_input(
        self,
        prepared: PreparedLegacyCall,
        projected: PreparedLegacyInputV1,
    ) -> None: ...

    def execute(self, prepared: PreparedLegacyCall) -> str: ...

    def primary_parent_route_handle(self) -> object: ...


LegacyApprovedRouteBinder = Callable[[Session], AbstractContextManager[LegacyApprovedBoundRoute]]
PrimaryParentRouteBinder = Callable[[PendingRouteIdentityV1, object], object]
CompensationExecutor = Callable[[Session, Mapping[str, Any]], str]


class _ClaimedWriteFailure(Exception):
    def __init__(self, record: ToolExecutionRecord[Any, Any]) -> None:
        super().__init__("claimed write failed")
        self.record = record


@dataclass(frozen=True)
class _LockedPendingIdentity:
    raw_args: str
    arguments_digest: str
    pending_action_revision: int


def _constant_time_text_equal(left: object, right: object) -> bool:
    if type(left) is not str or type(right) is not str or len(left) != len(right):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _locked_pending_identity(
    tool_call_id: str,
    tool_name: str,
    raw_args: str,
) -> _LockedPendingIdentity:
    try:
        arguments = parse_arguments(raw_args)
        normalized_args = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        arguments_digest = (
            "sha256:"
            + hashlib.sha256(canonical_json(cast(JSONValue, arguments)).encode("utf-8")).hexdigest()
        )
    except (ArgumentValidationError, TypeError, UnicodeError, ValueError) as exc:
        raise WriteOperationError("operation_identity_conflict") from exc
    revision_payload = json.dumps(
        {
            "args": normalized_args,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    revision = int.from_bytes(hashlib.sha256(revision_payload).digest()[:8], "big") & (
        (1 << 63) - 1
    )
    return _LockedPendingIdentity(raw_args, arguments_digest, revision)


def pending_action_identity(
    tool_call_id: str,
    tool_name: str,
    raw_args: str,
) -> tuple[str, int]:
    """Return the exact digest/revision pair used by the locked approval atom."""

    locked = _locked_pending_identity(tool_call_id, tool_name, raw_args)
    return locked.arguments_digest, locked.pending_action_revision


def compensation_request_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_id: str,
    parent_operation_id: str,
    compensation_kind: str,
    conversation_id: int,
) -> str:
    return ledger_fingerprint(
        key,
        "write-operation-request-v1",
        {
            "request_kind": "compensation_v1",
            "operation_id": operation_id,
            "parent_operation_id": parent_operation_id,
            "compensation_kind": compensation_kind,
            "conversation_id": conversation_id,
        },
    )


class WriteOperationCoordinator:
    def __init__(self, repository: WriteOperationRepository):
        self.repository = repository

    def execute_primary(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        prepared: PreparedToolCall[Any, Any],
        context: ToolExecutionContext,
        prepare_identity: ApprovedWritePrepareCallIdentity,
        request_fingerprint: str,
        parent_route_binder: PrimaryParentRouteBinder | None,
        edited_args_present: bool = False,
        edited_args: Mapping[str, JSONValue] | None = None,
        approval_decided_callback: Callable[[object | None], None] | None = None,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        owner = self.repository.prepare_owner(operation_id)
        owner_handed_off = False
        try:
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                outer_transaction = session.get_transaction()
                if outer_transaction is None:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_result_unknown", True), None
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay, None
                authority = context.authority
                if type(authority) is not ApprovalExecutionAuthority:
                    raise WriteOperationError("operation_identity_conflict")
                if type(prepare_identity) is not ApprovedWritePrepareCallIdentity:
                    raise WriteOperationError("operation_identity_conflict")
                factory = context.authority_factory
                conversation = session.get(Conversation, conversation_id)
                if conversation is None:
                    raise WriteOperationError("operation_identity_conflict")
                locked_pending, authority_entry = self._verify_primary(
                    session,
                    operation,
                    conversation,
                    conversation_id,
                    prepared,
                    authority,
                    prepare_identity,
                    request_fingerprint,
                    edited_args_present,
                    edited_args,
                    factory,
                )
                bound_context = context.bind(session)
                try:
                    scope_failure = _pre_resolver_entry_scope_policy(
                        authority_entry,
                        bound_context,
                    )
                    if scope_failure is not None:
                        raise WriteOperationError("scope_access_denied")
                    _binding, binding_allowed = _audit_entry_bindings(
                        authority_entry,
                        prepared.spec,
                        prepared.typed_args,
                        bound_context,
                    )
                except WriteOperationError:
                    raise
                except Exception as exc:
                    raise WriteOperationError("operation_not_committed", retryable=True) from exc
                if not binding_allowed:
                    raise WriteOperationError("scope_access_denied")
                if prepared.spec.mutable_validator is not None:
                    failure = prepared.spec.mutable_validator(prepared.typed_args, bound_context)
                    try:
                        factory.register_execution_transaction(
                            session,
                            outer_transaction,
                            authority=authority,
                        )
                    except AuthorityPhaseError as exc:
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    if failure is not None:
                        claimed = session.execute(
                            update(Conversation)
                            .where(Conversation.id == conversation_id)
                            .where(Conversation.pending_operation_id == operation_id)
                            .where(Conversation.pending_tool_call_id == prepared.tool_call_id)
                            .where(Conversation.pending_tool_name == prepared.spec.name)
                            .where(Conversation.pending_args == locked_pending.raw_args)
                            .where(Conversation.pending_confirmation_claim_id == "")
                            .values(
                                pending_confirmation_claim_id=operation_id,
                                pending_confirmation_claimed_at=datetime.now(timezone.utc),
                            )
                        )
                        if getattr(claimed, "rowcount", 0) != 1:
                            raise WriteOperationError("operation_identity_conflict")
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = ledger_fingerprint(
                            self.repository.key,
                            "write-operation-input-v1",
                            {"arguments_digest": locked_pending.arguments_digest},
                        )
                        self.repository.append_transition(session, operation_id, 2, "approved")
                        self.repository.append_transition(session, operation_id, 3, "claimed")
                        if approval_decided_callback is not None:
                            approval_decided_callback(session)
                        return self._commit_failure(
                            session,
                            operation,
                            prepared,
                            failure,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                        )
                try:
                    # Imported at the runtime leaf to avoid loading the eager
                    # Pilot Runtime package while this coordinator module is
                    # itself being initialized.
                    from offerpilot.pilot_runtime.primary_undo import (
                        build_primary_undo,
                        capture_primary_undo,
                    )

                    undo_binding = prepared.spec.undo_builder_binding
                    undo_checkpoint = (
                        capture_primary_undo(
                            undo_binding,
                            session,
                            outer_transaction,
                            bound_context,
                            prepared.typed_args,
                        )
                        if undo_binding is not None
                        else None
                    )
                    factory.register_execution_transaction(
                        session,
                        outer_transaction,
                        authority=authority,
                    )
                except AuthorityPhaseError as exc:
                    raise WriteOperationError("operation_not_committed", retryable=True) from exc
                except Exception as exc:
                    raise WriteOperationError("operation_not_committed", retryable=True) from exc
                claimed = session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .where(Conversation.pending_operation_id == operation_id)
                    .where(Conversation.pending_tool_call_id == prepared.tool_call_id)
                    .where(Conversation.pending_tool_name == prepared.spec.name)
                    .where(Conversation.pending_args == locked_pending.raw_args)
                    .where(Conversation.pending_confirmation_claim_id == "")
                    .values(
                        pending_confirmation_claim_id=operation_id,
                        pending_confirmation_claimed_at=datetime.now(timezone.utc),
                    )
                )
                if getattr(claimed, "rowcount", 0) != 1:
                    raise WriteOperationError("operation_identity_conflict")
                self.repository.append_transition(session, operation_id, 2, "approved")
                self.repository.append_transition(session, operation_id, 3, "claimed")
                execution_claim = None
                try:
                    execution_claim = factory.issue_execution_claim(
                        authority,
                        prepared=prepared,
                        pending=prepared.pending_identity,
                        operation_id=operation_id,
                        tool_call_id=prepared.tool_call_id,
                        tool_name=prepared.spec.name,
                        effective_args_digest=locked_pending.arguments_digest,
                        pending_action_revision=locked_pending.pending_action_revision,
                        session=session,
                        transaction=outer_transaction,
                    )
                    execute_identity = factory.create_approved_write_execute_identity(
                        prepare_identity,
                        prepared=prepared,
                        execution_claim=execution_claim,
                    )
                except AuthorityPhaseError as exc:
                    if (
                        execution_claim is not None
                        and factory.claim_state(execution_claim) is not None
                    ):
                        factory.revoke(execution_claim)
                    raise WriteOperationError("operation_identity_conflict") from exc
                if approval_decided_callback is not None:
                    approval_decided_callback(session)
                executor_started = False
                started_recorded = False

                def mark_execution_stage(stage: str) -> None:
                    nonlocal executor_started
                    if stage == "executor":
                        executor_started = True

                try:
                    with session.begin_nested():
                        started_recorded = project_tool_started_bound(
                            bound_context.run_recorder, session, prepared
                        )
                        dispatched = execute_prepared(
                            prepared,
                            bound_context,
                            call_identity=execute_identity,
                            execution_claim=execution_claim,
                            locked_effective_args_digest=locked_pending.arguments_digest,
                            stage_sink=mark_execution_stage,
                        )
                        if isinstance(dispatched.outcome, ToolFailure):
                            raise _ClaimedWriteFailure(
                                ToolExecutionRecord(
                                    prepared,
                                    dispatched.outcome,
                                    True,
                                    operation_id,
                                    False,
                                    False,
                                    None,
                                    None,
                                    started_recorded,
                                )
                            )
                        result = dispatched.outcome.result
                        record = ToolExecutionRecord(
                            prepared, dispatched.outcome, True, operation_id, False
                        )
                        undo = (
                            build_primary_undo(
                                undo_checkpoint,
                                session,
                                outer_transaction,
                                record,
                            )
                            if undo_checkpoint is not None
                            else None
                        )
                        write_contract = prepared.spec.metadata.operation
                        if type(write_contract) is not WriteOperationMetadataV1:
                            raise WriteOperationError("operation_not_transactional")
                        if write_contract.undo_policy is UndoPolicy.REQUIRED and not undo:
                            raise WriteOperationError("operation_projection_failed")
                        if write_contract.undo_policy is UndoPolicy.NONE and undo is not None:
                            raise WriteOperationError("operation_projection_failed")
                        visible_value = prepared.spec.presentation.success_summary_projector(result)
                        if not isinstance(visible_value, str):
                            raise WriteOperationError("operation_projection_failed")
                        visible = visible_value
                        transport = project_transport_event(prepared.spec, record)
                        payload = build_terminal_payload(
                            status="committed",
                            result_contract=write_contract.result_contract,
                            result=result,
                            visible_result=visible,
                            transport=transport,
                            undo=undo,
                            failure_category=None,
                            failure_code=None,
                            budgets=(
                                write_contract.result_bytes,
                                write_contract.visible_bytes,
                                write_contract.transport_bytes,
                                write_contract.undo_bytes,
                            ),
                        )
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = ledger_fingerprint(
                            self.repository.key,
                            "write-operation-input-v1",
                            {"arguments_digest": locked_pending.arguments_digest},
                        )
                        self._set_terminal(operation, payload, owner)
                        self.repository.append_transition(session, operation_id, 4, "committed")
                        session.flush()
                        if parent_route_binder is not None:
                            owner.bind_parent_route(
                                parent_route_binder(
                                    PendingRouteIdentityV1(
                                        conversation_id=conversation_id,
                                        operation_id=operation_id,
                                        tool_call_id=prepared.tool_call_id,
                                        tool_name=prepared.spec.name,
                                        pending_action_revision=(
                                            locked_pending.pending_action_revision
                                        ),
                                        pending_confirmation_claim_id=operation_id,
                                        arguments_digest=locked_pending.arguments_digest,
                                    ),
                                    execution_claim,
                                )
                            )
                        record = ToolExecutionRecord(
                            prepared,
                            record.outcome,
                            True,
                            operation_id,
                            False,
                            True,
                            payload.visible_result,
                            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
                            started_recorded,
                        )
                except _ClaimedWriteFailure as exc:
                    failure = cast(ToolFailure, exc.record.outcome)
                    try:
                        return self._commit_failure(
                            session,
                            operation,
                            prepared,
                            failure,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            record=exc.record,
                        )
                    except Exception:
                        return self._commit_projection_failure(
                            session,
                            operation,
                            prepared,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            started_recorded=started_recorded,
                        )
                except Exception as exc:
                    if executor_started:
                        return self._commit_projection_failure(
                            session,
                            operation,
                            prepared,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            started_recorded=started_recorded,
                        )
                    if isinstance(exc, WriteOperationError):
                        raise
                    failure = _map_exception(prepared, exc)
                    if failure.category == "internal_error":
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    record = ToolExecutionRecord(
                        prepared,
                        failure,
                        False,
                        operation_id,
                        False,
                        False,
                        None,
                        None,
                        started_recorded,
                    )
                    committed_failure = self._commit_failure(
                        session,
                        operation,
                        prepared,
                        failure,
                        request_fingerprint,
                        owner,
                        arguments_digest=locked_pending.arguments_digest,
                        record=record,
                    )
                    return committed_failure
                finally:
                    if factory.claim_state(execution_claim) is not None:
                        factory.revoke(execution_claim)
                try:
                    session.commit()
                except OperationalError:
                    return (
                        self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_result_unknown",
                            proposed_code="operation_not_committed",
                        ),
                        None,
                    )
                owner_handed_off = True
                return OperationCommitted(operation_id, payload, owner), record
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable), None
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation_id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_busy",
                ),
                None,
            )
        except BaseException:
            raise
        finally:
            if not owner_handed_off:
                owner.revoke_parent_route()

    def reject_primary(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        request_fingerprint: str,
        visible_result: str,
        confirmation_token: str | None = None,
    ) -> OperationExecution:
        owner = self.repository.prepare_owner(operation_id)
        try:
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                outer_transaction = session.get_transaction()
                if outer_transaction is None:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if (
                    operation.conversation_id != conversation_id
                    or operation.tool_call_id != tool_call_id
                    or operation.tool_name != tool_name
                ):
                    raise WriteOperationError("operation_identity_conflict")
                pointer_row = session.execute(
                    select(
                        Conversation.id,
                        Conversation.pending_operation_id,
                        Conversation.pending_tool_call_id,
                        Conversation.pending_tool_name,
                        Conversation.pending_confirmation_claim_id,
                    ).where(Conversation.id == conversation_id)
                ).one_or_none()
                if pointer_row is None:
                    raise WriteOperationError("operation_unavailable")
                pointer = LedgerPendingPointer(
                    conversation_id=int(pointer_row.id),
                    operation_id=str(pointer_row.pending_operation_id or ""),
                    tool_call_id=str(pointer_row.pending_tool_call_id or ""),
                    tool_name=str(pointer_row.pending_tool_name or ""),
                    pending_confirmation_claim_id=str(
                        pointer_row.pending_confirmation_claim_id or ""
                    ),
                )
                if (
                    pointer.operation_id != operation_id
                    or pointer.tool_call_id != tool_call_id
                    or pointer.tool_name != tool_name
                    or pointer.pending_confirmation_claim_id != ""
                ):
                    raise WriteOperationError("operation_identity_conflict")

                if confirmation_token is not None:
                    try:
                        token_bytes = confirmation_token.encode("ascii")
                    except UnicodeEncodeError as exc:
                        raise WriteOperationError("invalid_confirmation") from exc
                    supplied_fingerprint = ledger_fingerprint(
                        self.repository.key,
                        "write-operation-confirmation-token-v1",
                        token_bytes,
                    )
                    if not hmac.compare_digest(
                        supplied_fingerprint,
                        operation.confirmation_token_fingerprint or "",
                    ):
                        raise WriteOperationError("operation_input_conflict")

                payload = build_terminal_payload(
                    status="rejected",
                    result_contract="rejection_json_v1",
                    result={"message": visible_result},
                    visible_result=visible_result,
                    transport={
                        "status": "cancelled",
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                    },
                    undo=None,
                    failure_category=None,
                    failure_code=None,
                )

                factory = AuthorityFactory()
                factory.register_operation(operation)
                pointer_revision = int.from_bytes(
                    hashlib.sha256(
                        canonical_json(
                            {
                                "conversation_id": pointer.conversation_id,
                                "operation_id": pointer.operation_id,
                                "tool_call_id": pointer.tool_call_id,
                                "tool_name": pointer.tool_name,
                                "pending_confirmation_claim_id": (
                                    pointer.pending_confirmation_claim_id
                                ),
                            }
                        ).encode("utf-8")
                    ).digest()[:8],
                    "big",
                ) & ((1 << 63) - 1)
                pointer_digest = (
                    "sha256:"
                    + hashlib.sha256(
                        canonical_json(
                            {
                                "operation_id": pointer.operation_id,
                                "tool_call_id": pointer.tool_call_id,
                                "tool_name": pointer.tool_name,
                            }
                        ).encode("utf-8")
                    ).hexdigest()
                )
                factory.register_pending(
                    pointer,
                    conversation_id=pointer.conversation_id,
                    operation_id=pointer.operation_id,
                    tool_call_id=pointer.tool_call_id,
                    tool_name=pointer.tool_name,
                    pending_action_revision=pointer_revision,
                    pending_confirmation_claim_id=(pointer.pending_confirmation_claim_id),
                    arguments_digest=pointer_digest,
                    effective_args_digest=pointer_digest,
                )
                factory.register_transaction(outer_transaction)
                proof = factory.issue_omitted_token_proof(
                    operation,
                    pending_pointer=pointer,
                    transaction=outer_transaction,
                )

                expected_adapter_kind = operation.adapter_kind
                expected_proposal_fingerprint = operation.proposal_fingerprint
                expected_confirmation_token_fingerprint = operation.confirmation_token_fingerprint
                expected_authorization_scope_fingerprint = operation.authorization_scope_fingerprint
                expected_fingerprint_key_id = operation.fingerprint_key_id

                def reject_cas() -> None:
                    cleared = session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .where(Conversation.pending_operation_id == operation_id)
                        .where(Conversation.pending_tool_call_id == tool_call_id)
                        .where(Conversation.pending_tool_name == tool_name)
                        .where(Conversation.pending_confirmation_claim_id == "")
                        .values(
                            pending_operation_id="",
                            pending_tool_call_id="",
                            pending_tool_name="",
                            pending_confirmation_claim_id="",
                            pending_confirmation_claimed_at=None,
                            pending_args="",
                            pending_human="",
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    if getattr(cleared, "rowcount", 0) != 1:
                        raise WriteOperationError("operation_identity_conflict")
                    now = datetime.now(timezone.utc)
                    terminalized = session.execute(
                        update(WriteOperation)
                        .where(WriteOperation.id == operation_id)
                        .where(WriteOperation.operation_role == "primary")
                        .where(WriteOperation.status == "proposed")
                        .where(WriteOperation.conversation_id == conversation_id)
                        .where(WriteOperation.adapter_kind == expected_adapter_kind)
                        .where(WriteOperation.tool_call_id == tool_call_id)
                        .where(WriteOperation.tool_name == tool_name)
                        .where(WriteOperation.proposal_fingerprint == expected_proposal_fingerprint)
                        .where(
                            WriteOperation.confirmation_token_fingerprint
                            == expected_confirmation_token_fingerprint
                        )
                        .where(
                            WriteOperation.authorization_scope_fingerprint
                            == expected_authorization_scope_fingerprint
                        )
                        .where(WriteOperation.fingerprint_key_id == expected_fingerprint_key_id)
                        .where(WriteOperation.operation_request_fingerprint.is_(None))
                        .values(
                            status=payload.status,
                            operation_request_fingerprint=request_fingerprint,
                            result_contract=payload.result_contract,
                            result_json=payload.result_json,
                            visible_result=payload.visible_result,
                            transport_json=payload.transport_json,
                            undo_json=payload.undo_json,
                            terminal_payload_sha256=payload.digest,
                            failure_category=payload.failure_category,
                            failure_code=payload.failure_code,
                            rejected_at=now,
                            delivery_status="pending",
                            delivery_generation=owner.generation,
                            delivery_owner_token_fingerprint=owner.fingerprint,
                            delivery_lease_expires_at=cast(
                                Any,
                                func.unixepoch("now") + DELIVERY_OWNER_LEASE_SECONDS,
                            ),
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(terminalized, "rowcount", 0) != 1:
                        raise WriteOperationError("operation_identity_conflict")
                    session.expire(operation)
                    session.refresh(operation)
                    self.repository.append_transition(session, operation_id, 2, "rejected")
                    session.add_all(
                        (
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="tool",
                                content=canonical_json(
                                    {
                                        "status": "cancelled",
                                        "message": "用户取消了该操作，未执行。",
                                    }
                                ),
                                tool_call_id=tool_call_id,
                                operation_id=operation_id,
                                delivery_kind="origin_tool_result",
                                delivery_ordinal=0,
                            ),
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=visible_result,
                                operation_id=operation_id,
                                delivery_kind="continuation_message",
                                delivery_ordinal=1,
                            ),
                        )
                    )
                    session.flush()
                    if not self.repository.complete_delivery(
                        session,
                        owner,
                        outcome="final_response",
                    ):
                        raise WriteOperationError("operation_delivery_unknown")

                try:
                    with factory.claim_lifecycle(proof):
                        reject_cas()
                        session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                finally:
                    factory.close()
                return OperationFailed(operation_id, payload, None)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_result_unknown",
                proposed_code="operation_busy",
            )

    def execute_legacy(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        request_fingerprint: str,
        route_binder: LegacyApprovedRouteBinder,
    ) -> OperationExecution:
        owner = self.repository.prepare_owner(operation_id)
        owner_handed_off = False
        try:
            with self.repository.session_factory() as read_session:
                observed = read_session.get(WriteOperation, operation_id)
                if observed is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if observed.status in _TERMINAL_STATUSES:
                    return self.repository.replay(observed, request_fingerprint)
                if (
                    observed.conversation_id != conversation_id
                    or observed.tool_call_id != tool_call_id
                    or observed.tool_name != tool_name
                    or observed.adapter_kind != "legacy_deterministic"
                ):
                    raise WriteOperationError("operation_identity_conflict")

            with self.repository.session_factory() as session:
                with route_binder(session) as bound_route:
                    operation = session.get(WriteOperation, operation_id)
                    if operation is None:
                        raise WriteOperationError("operation_result_unknown", retryable=True)
                    if operation.status in _TERMINAL_STATUSES:
                        raise WriteOperationError("operation_not_committed", retryable=True)
                    if (
                        operation.conversation_id != conversation_id
                        or operation.tool_call_id != tool_call_id
                        or operation.tool_name != tool_name
                        or operation.adapter_kind != "legacy_deterministic"
                    ):
                        raise WriteOperationError("operation_identity_conflict")
                    prepared_call = bound_route.prepared_call()
                    prepared_input_port = bound_route.prepared_input_port()
                    if type(prepared_call) is not PreparedLegacyCall:
                        raise WriteOperationError("operation_not_committed", retryable=True)
                    if type(prepared_input_port) is not LegacyPreparedInputPort:
                        raise WriteOperationError("operation_not_committed", retryable=True)
                    prepared_input = prepared_input_port.require(
                        prepared_call,
                        operation_id=operation_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                    bound_route.accept_prepared_input(prepared_call, prepared_input)
                    input_fingerprint = ledger_fingerprint(
                        self.repository.key,
                        "write-operation-legacy-input-v1",
                        materialize_json(cast(FrozenJSONValue, prepared_input.canonical_args)),
                    )
                    self.repository.append_transition(session, operation_id, 2, "approved")
                    self.repository.append_transition(session, operation_id, 3, "claimed")
                    try:
                        visible = bound_route.execute(prepared_call)
                        owner.bind_parent_route(bound_route.primary_parent_route_handle())
                        if type(visible) is not str:
                            raise WriteOperationError("operation_projection_failed")
                        payload = build_terminal_payload(
                            status="committed",
                            result_contract="legacy_string_v1",
                            result={"value": visible},
                            visible_result=visible,
                            transport={
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "status": "success",
                                "summary": visible[:500],
                            },
                            undo=None,
                            failure_category=None,
                            failure_code=None,
                        )
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = input_fingerprint
                        self._set_terminal(operation, payload, owner)
                        self.repository.append_transition(session, operation_id, 4, "committed")
                        session.flush()
                    except ValueError as exc:
                        if owner.parent_route_handle is None:
                            owner.bind_parent_route(bound_route.primary_parent_route_handle())
                        code = str(exc)
                        if not code.isascii() or not code or len(code) > 128:
                            raise WriteOperationError(
                                "operation_not_committed", retryable=True
                            ) from exc
                        payload = build_terminal_payload(
                            status="failed",
                            result_contract="legacy_string_v1",
                            result={"code": code},
                            visible_result="错误：" + code,
                            transport={
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "status": "error",
                                "summary": "",
                            },
                            undo=None,
                            failure_category="conflict",
                            failure_code=code,
                        )
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = input_fingerprint
                        self._set_terminal(operation, payload, owner)
                        self.repository.append_transition(session, operation_id, 4, "failed")
                try:
                    session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                if payload.status == "committed":
                    owner_handed_off = True
                    return OperationCommitted(operation_id, payload, owner)
                owner_handed_off = True
                return OperationFailed(operation_id, payload, owner)
        except LegacyArgumentPreparationError:
            return OperationUnknown(operation_id, "invalid_confirmation", False)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_result_unknown",
                proposed_code="operation_busy",
            )
        except Exception:
            return OperationUnknown(operation_id, "operation_not_committed", True)
        finally:
            if not owner_handed_off:
                owner.revoke_parent_route()

    def execute_compensation(
        self,
        *,
        parent: CommittedPrimaryOperationIdentityV1,
        conversation_id: int,
        operation_port: ToolOperationMetadataPort,
        route_handle: CompensationHandle,
        handler_handle: object,
        executor: CompensationExecutor,
    ) -> OperationExecution:
        committed_parent = parent
        if type(operation_port) is not ToolOperationMetadataPort:
            return OperationUnknown("", "operation_identity_conflict", False)
        try:
            route = operation_port.require_compensation(
                route_handle,
                parent,
                handler_handle,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return OperationUnknown("", "operation_identity_conflict", False)
        parent_operation_id = parent.operation_id
        compensation_kind = route.operation_name
        operation_id = compensation_operation_id(parent_operation_id, compensation_kind)
        request_fingerprint = compensation_request_fingerprint(
            self.repository.key,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            compensation_kind=compensation_kind,
            conversation_id=conversation_id,
        )
        try:
            # Persist the deterministic proposal separately so a commit-unknown
            # execution can never be retried by the same request.
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    parent_row = session.get(WriteOperation, parent_operation_id)
                    if (
                        parent_row is None
                        or parent_row.operation_role != "primary"
                        or parent_row.status != "committed"
                        or parent_row.conversation_id != conversation_id
                        or parent_row.undo_json is None
                    ):
                        raise WriteOperationError("operation_identity_conflict")
                    parent_payload = payload_from_operation(parent_row)
                    undo = json.loads(parent_row.undo_json)
                    if (
                        parent_row.id != parent_operation_id
                        or parent_row.tool_name != committed_parent.primary_tool
                        or parent_row.adapter_kind != "typed"
                        or parent_payload.digest != committed_parent.terminal_payload_digest
                    ):
                        raise WriteOperationError("operation_identity_conflict")
                    operation = WriteOperation(
                        id=operation_id,
                        operation_role="compensation",
                        parent_operation_id=parent_operation_id,
                        parent_terminal_payload_sha256=parent_payload.digest,
                        conversation_id=conversation_id,
                        tool_call_id=None,
                        tool_name=compensation_kind,
                        adapter_kind="compensation",
                        status="proposed",
                        fingerprint_key_id=self.repository.key.key_id,
                        operation_request_fingerprint=request_fingerprint,
                        delivery_status="pending",
                        delivery_generation=0,
                    )
                    session.add(operation)
                    self.repository.append_transition(session, operation_id, 1, "proposed")
                    try:
                        session.commit()
                    except OperationalError:
                        return self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_not_committed",
                            proposed_code="operation_not_committed",
                        )
                else:
                    self._verify_compensation(operation, request_fingerprint)
                    if operation.status in _TERMINAL_STATUSES:
                        replay = self.repository.replay(operation, request_fingerprint)
                        session.rollback()
                        return replay
                    try:
                        session.commit()
                    except OperationalError:
                        return self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_not_committed",
                            proposed_code="operation_not_committed",
                        )

            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                self._verify_compensation(operation, request_fingerprint)
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay
                parent_row = session.get(WriteOperation, parent_operation_id)
                if (
                    parent_row is None
                    or parent_row.operation_role != "primary"
                    or parent_row.status != "committed"
                    or parent_row.conversation_id != conversation_id
                    or parent_row.undo_json is None
                ):
                    raise WriteOperationError("operation_integrity_error")
                parent_payload = payload_from_operation(parent_row)
                undo = cast(dict[str, Any], json.loads(parent_row.undo_json))
                if (
                    operation.parent_terminal_payload_sha256 != parent_payload.digest
                    or parent_row.id != parent_operation_id
                    or parent_row.tool_name != committed_parent.primary_tool
                    or parent_row.adapter_kind != "typed"
                    or parent_payload.digest != committed_parent.terminal_payload_digest
                ):
                    raise WriteOperationError("operation_integrity_error")
                input_fingerprint = ledger_fingerprint(
                    self.repository.key,
                    "write-operation-input-v1",
                    {
                        "operation_request_fingerprint": request_fingerprint,
                        "parent_terminal_payload_sha256": parent_payload.digest,
                        "undo": undo,
                    },
                )
                self.repository.append_transition(session, operation_id, 2, "approved")
                self.repository.append_transition(session, operation_id, 3, "claimed")
                try:
                    with session.begin_nested():
                        message = executor(session, undo)
                except ValueError as exc:
                    code = str(exc)
                    if not code.isascii() or not code or len(code) > 128:
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    payload = build_terminal_payload(
                        status="failed",
                        result_contract="compensation_json_v1",
                        result={"code": code},
                        visible_result="当前记录已被修改，无法安全撤销。",
                        transport={},
                        undo=None,
                        failure_category="conflict",
                        failure_code=code,
                    )
                else:
                    payload = build_terminal_payload(
                        status="committed",
                        result_contract="compensation_json_v1",
                        result={"message": message},
                        visible_result=message,
                        transport={},
                        undo=None,
                        failure_category=None,
                        failure_code=None,
                    )
                operation.input_fingerprint = input_fingerprint
                self._set_compensation_terminal(operation, payload)
                self.repository.append_transition(session, operation_id, 4, payload.status)
                if payload.status == "committed":
                    session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .where(Conversation.last_write_operation_id == parent_operation_id)
                        .values(last_write_operation_id="", last_write_undo_json="")
                    )
                try:
                    session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                if payload.status == "committed":
                    return OperationCommitted(operation_id, payload, None)
                return OperationFailed(operation_id, payload, None)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_busy",
                proposed_code="operation_busy",
            )
        except Exception:
            return OperationUnknown(operation_id, "operation_not_committed", True)

    def _reconcile_commit_unknown(
        self,
        operation_id: str,
        request_fingerprint: str,
        *,
        absent_code: str,
        proposed_code: str,
    ) -> OperationExecution:
        """Resolve a lost COMMIT response from authoritative Ledger state."""
        try:
            with self.repository.session_factory() as session:
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    return OperationUnknown(operation_id, absent_code, True)
                if operation.status not in _TERMINAL_STATUSES:
                    return OperationUnknown(operation_id, proposed_code, True)
                return self.repository.replay(operation, request_fingerprint)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return OperationUnknown(operation_id, "operation_result_unknown", True)

    def _verify_compensation(self, operation: WriteOperation, request_fingerprint: str) -> None:
        if (
            operation.operation_role != "compensation"
            or operation.adapter_kind != "compensation"
            or operation.fingerprint_key_id != self.repository.key.key_id
            or not operation.operation_request_fingerprint
            or not hmac.compare_digest(operation.operation_request_fingerprint, request_fingerprint)
        ):
            raise WriteOperationError("operation_input_conflict")

    @staticmethod
    def _set_compensation_terminal(operation: WriteOperation, payload: TerminalPayload) -> None:
        operation.status = payload.status
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.undo_json = None
        operation.terminal_payload_sha256 = payload.digest
        operation.failure_category = payload.failure_category
        operation.failure_code = payload.failure_code
        now = datetime.now(timezone.utc)
        if payload.status == "committed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.committed_at = now
        else:
            operation.approved_at = now
            operation.claimed_at = now
            operation.failed_at = now
        operation.delivery_status = "not_applicable"
        operation.delivery_outcome = "none"
        operation.delivery_message_count = 0
        operation.delivery_generation = 0
        operation.delivered_at = now
        operation.updated_at = now

    def _verify_primary(
        self,
        session: Session,
        operation: WriteOperation,
        conversation: Conversation,
        conversation_id: int,
        prepared: PreparedToolCall[Any, Any],
        authority: ApprovalExecutionAuthority,
        prepare_identity: ApprovedWritePrepareCallIdentity,
        request_fingerprint: str,
        edited_args_present: bool,
        edited_args: Mapping[str, JSONValue] | None,
        factory: Any,
    ) -> tuple[_LockedPendingIdentity, ToolAuthorityEntryV1]:
        if operation.conversation_id is None:
            raise WriteOperationError("operation_unavailable")
        if operation.conversation_id != conversation_id or conversation.id != conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        if operation.authorization_scope_fingerprint is None:
            raise WriteOperationError("authorization_scope_unbound")
        locked_proposal = _locked_pending_identity(
            conversation.pending_tool_call_id,
            conversation.pending_tool_name,
            conversation.pending_args,
        )
        try:
            require_authority_phase(
                authority,
                AuthorityUse.APPROVED_WRITE_PREPARE,
                prepare_identity,
            )
            authority_entry = factory.require_prepared_route(
                prepared,
                authority=authority,
                use=AuthorityUse.APPROVED_WRITE_PREPARE,
            )
            persisted_values = parse_arguments(conversation.pending_args)
            prepared_values = cast(dict[str, JSONValue], dict(prepared.arguments))
            prepared_arguments = canonical_json(cast(JSONValue, prepared_values))
            if type(edited_args_present) is not bool:
                raise TypeError("edited_args_present must be bool")
            if edited_args_present:
                if not isinstance(edited_args, Mapping):
                    raise TypeError("edited_args must be a mapping when present")
                patch = dict(edited_args)
                if any(type(key) is not str for key in patch):
                    raise TypeError("edited_args keys must be strings")
                editable_fields = {
                    descriptor.field for descriptor in prepared.spec.metadata.editable_fields
                }
                if any(key not in editable_fields for key in patch):
                    raise ValueError("edited_args contains a non-editable field")
                expected_values = {**persisted_values, **patch}
            else:
                if edited_args is not None:
                    raise TypeError("edited_args must be absent when presence is false")
                patch = None
                expected_values = persisted_values
            expected_arguments = canonical_json(cast(JSONValue, expected_values))
            # The revision is the Pending identity, not the canonical digest.
            # Keep the validated mapping's insertion order here so it matches
            # AgentLoop's `_pending_action_revision` and the persisted
            # proposal bytes.  Canonical JSON remains the equality/digest
            # representation below.
            prepared_identity_arguments = json.dumps(
                prepared_values,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            effective = _locked_pending_identity(
                prepared.tool_call_id,
                prepared.spec.name,
                prepared_identity_arguments,
            )
            prepared_pending_token = factory.pending_token(prepared.pending_identity)
        except (
            ArgumentValidationError,
            AuthorityPhaseError,
            TypeError,
            ValueError,
        ) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if (
            operation.operation_role != "primary"
            or operation.adapter_kind != "typed"
            or operation.status != "proposed"
            or operation.fingerprint_key_id != self.repository.key.key_id
            or operation.tool_call_id != prepared.tool_call_id
            or operation.tool_name != prepared.spec.name
            or conversation.pending_operation_id != operation.id
            or conversation.pending_tool_call_id != prepared.tool_call_id
            or conversation.pending_tool_name != prepared.spec.name
            or authority.operation_id != operation.id
            or authority.conversation_id != conversation_id
            or authority.tool_call_id != prepared.tool_call_id
            or authority.tool_name != prepared.spec.name
            or prepare_identity.operation_id != operation.id
            or prepare_identity.tool_call_id != prepared.tool_call_id
            or prepare_identity.tool_name != prepared.spec.name
            or prepared.pending_action_revision != effective.pending_action_revision
            or authority.pending_action_revision != effective.pending_action_revision
            or prepare_identity.pending_action_revision != effective.pending_action_revision
            or prepared_pending_token is not authority.pending_identity
            or prepare_identity.pending_identity is not authority.pending_identity
            or not _constant_time_text_equal(prepared.arguments_digest, effective.arguments_digest)
            or not _constant_time_text_equal(
                authority.effective_args_digest, effective.arguments_digest
            )
            or not _constant_time_text_equal(
                prepare_identity.effective_args_digest, effective.arguments_digest
            )
            or not _constant_time_text_equal(
                operation.proposal_fingerprint,
                ledger_fingerprint(
                    self.repository.key,
                    "write-operation-proposal-v1",
                    persisted_values,
                ),
            )
        ):
            raise WriteOperationError("operation_identity_conflict")
        expected_request = operation_request_fingerprint(
            self.repository.key,
            operation_id=operation.id,
            tool_call_id=prepared.tool_call_id,
            approved=True,
            edited_args_present=edited_args_present,
            edited_args=patch,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=operation.confirmation_token_fingerprint or "",
            proposal_fingerprint=operation.proposal_fingerprint or "",
        )
        if not (
            _constant_time_text_equal(prepared_arguments, expected_arguments)
            and _constant_time_text_equal(request_fingerprint, expected_request)
        ):
            raise WriteOperationError("operation_input_conflict")
        try:
            scope = authority.trusted_scope
            context_ref: int | None = None
            if conversation.context_type == "application":
                context_ref = int(conversation.context_ref)
            if (
                authority.conversation_scope_revision != conversation.scope_revision
                or scope.context_type != conversation.context_type
                or scope.context_ref != context_ref
                or scope.mode != conversation.mode
            ):
                raise WriteOperationError("authorization_scope_changed")
            locked_scope_fingerprint = authorization_scope_fingerprint(
                self.repository.key,
                conversation_id=conversation.id,
                conversation_scope_revision=conversation.scope_revision,
                context_type=conversation.context_type,
                context_ref=context_ref,
                mode=conversation.mode,
                capability_profile_id=authority.capability_profile_id,
                capability_policy_version=authority.capability_policy_version,
                binding_policy_version=authority.binding_policy_version,
                capability_profile_fingerprint=authority.capability_profile_fingerprint,
                binding_policy_fingerprint=authority.binding_policy_fingerprint,
            )
        except WriteOperationError:
            raise
        except Exception as exc:
            raise WriteOperationError("operation_not_committed", retryable=True) from exc
        if not _constant_time_text_equal(
            operation.authorization_scope_fingerprint,
            locked_scope_fingerprint,
        ):
            raise WriteOperationError("authorization_scope_changed")
        if context_ref is not None:
            try:
                active_parent = AuthorityApplicationVisibilityQuery().execute_on_session(
                    session, context_ref
                )
            except AuthorityApplicationVisibilityError as exc:
                raise WriteOperationError("operation_not_committed", retryable=True) from exc
            if active_parent is None:
                raise WriteOperationError("authorization_scope_unavailable")
        return (
            _LockedPendingIdentity(
                locked_proposal.raw_args,
                effective.arguments_digest,
                effective.pending_action_revision,
            ),
            authority_entry,
        )

    def _commit_failure(
        self,
        session: Session,
        operation: WriteOperation,
        prepared: PreparedToolCall[Any, Any],
        failure: ToolFailure,
        request_fingerprint: str,
        owner: DeliveryOwnership,
        *,
        arguments_digest: str,
        record: ToolExecutionRecord[Any, Any] | None = None,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        if failure.category == "internal_error" and (
            record is None or not record.execution_started
        ):
            raise WriteOperationError("operation_not_committed", retryable=True)
        # A validator failure discovered inside the ledger transaction already
        # owns a durable terminal and must flow through this request's delivery.
        resolved = record or ToolExecutionRecord(
            prepared, failure, False, operation.id, False, True
        )
        visible = render_compatibility(prepared.spec, failure)
        payload = build_terminal_payload(
            status="failed",
            result_contract="typed_json_v1",
            result={"category": failure.category, "code": failure.code},
            visible_result=visible,
            transport=project_transport_event(prepared.spec, resolved),
            undo=None,
            failure_category=failure.category,
            failure_code=failure.code,
        )
        operation.operation_request_fingerprint = request_fingerprint
        operation.input_fingerprint = ledger_fingerprint(
            self.repository.key,
            "write-operation-input-v1",
            {"arguments_digest": arguments_digest},
        )
        self._set_terminal(operation, payload, owner)
        self.repository.append_transition(session, operation.id, 4, "failed")
        try:
            session.commit()
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation.id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_not_committed",
                ),
                None,
            )
        persisted = ToolExecutionRecord(
            prepared,
            resolved.outcome,
            resolved.execution_started,
            operation.id,
            False,
            True,
            payload.visible_result,
            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
            resolved.journal_started_recorded,
        )
        return OperationFailed(operation.id, payload, owner), persisted

    def _commit_projection_failure(
        self,
        session: Session,
        operation: WriteOperation,
        prepared: PreparedToolCall[Any, Any],
        request_fingerprint: str,
        owner: DeliveryOwnership,
        *,
        arguments_digest: str,
        started_recorded: bool,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        """Durably terminalize after dispatch without invoking fallible projections again."""

        failure = ToolFailure("internal_error", "operation_projection_failed")
        payload = build_terminal_payload(
            status="failed",
            result_contract="typed_json_v1",
            result={"category": failure.category, "code": failure.code},
            visible_result="错误：操作结果处理失败，操作未提交。",
            transport={
                "status": "error",
                "tool_call_id": prepared.tool_call_id,
                "tool_name": prepared.spec.name,
                "category": failure.category,
                "code": failure.code,
            },
            undo=None,
            failure_category=failure.category,
            failure_code=failure.code,
        )
        operation.operation_request_fingerprint = request_fingerprint
        operation.input_fingerprint = ledger_fingerprint(
            self.repository.key,
            "write-operation-input-v1",
            {"arguments_digest": arguments_digest},
        )
        self._set_terminal(operation, payload, owner)
        self.repository.append_transition(session, operation.id, 4, "failed")
        try:
            session.commit()
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation.id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_not_committed",
                ),
                None,
            )
        record = ToolExecutionRecord(
            prepared,
            failure,
            True,
            operation.id,
            False,
            True,
            payload.visible_result,
            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
            started_recorded,
        )
        return OperationFailed(operation.id, payload, owner), record

    @staticmethod
    def _set_terminal(
        operation: WriteOperation,
        payload: TerminalPayload,
        owner: DeliveryOwnership,
    ) -> None:
        operation.status = payload.status
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.undo_json = payload.undo_json
        operation.terminal_payload_sha256 = payload.digest
        operation.failure_category = payload.failure_category
        operation.failure_code = payload.failure_code
        now = datetime.now(timezone.utc)
        if payload.status == "committed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.committed_at = now
        elif payload.status == "failed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.failed_at = now
        elif payload.status == "rejected":
            operation.rejected_at = now
        operation.delivery_status = "pending"
        operation.delivery_generation = owner.generation
        operation.delivery_owner_token_fingerprint = owner.fingerprint
        operation.delivery_lease_expires_at = cast(
            Any, func.unixepoch("now") + DELIVERY_OWNER_LEASE_SECONDS
        )
        operation.updated_at = now


def _map_exception(
    prepared: PreparedToolCall[Any, Any],
    error: Exception,
) -> ToolFailure:
    for mapping in prepared.spec.exception_map:
        if isinstance(error, mapping.exception_type):
            detail = ""
            if mapping.compatibility_detail is not None:
                try:
                    detail = mapping.compatibility_detail(error)
                except Exception:
                    detail = ""
            return ToolFailure(mapping.category, mapping.code, detail)
    return ToolFailure("internal_error", "executor_exception")
