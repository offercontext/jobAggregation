from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import sqlite3
from threading import RLock
from types import MappingProxyType
from typing import Literal, NoReturn, TypeVar, cast
from uuid import UUID, uuid4
from weakref import ReferenceType, ref

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from offerpilot.ai.tool_runtime.contracts import JSONValue, TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.legacy import (
    LegacyDeterministicAdapterSpec,
    LegacyProofDeterministicCatalog,
    LegacyStaticAdapterCatalogV1,
    _create_legacy_proof_route_handle,
    _decode_legacy_arguments_object,
    prepare_legacy_arguments,
)
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyApprovedConfirmationInput,
    LegacyClaimLease,
    LegacyConfirmationLookupIdentity,
    LegacyPreparationRegistry,
    LegacyRouteIssuanceLease,
    LegacyRouteProof,
    LegacyRouteProofConsumerPort,
    LegacyRouteProofRegistrationPort,
    LegacyRouteProofRegistry,
    PreparedLegacyCall,
)
from offerpilot.ai.tool_runtime.metadata import (
    BundleInstanceToken,
    LegacyDeterministicBoundaryV1,
    freeze_json,
)
from offerpilot.ai.tool_runtime.protocol_seals import verify_legacy_boundary
from offerpilot.ai.tool_runtime.validation import canonical_json
from offerpilot.ai.write_operations import (
    LedgerKeyDomain,
    ledger_fingerprint,
    operation_request_fingerprint,
)


_VALUE_SEAL = object()
_RLOCK_TYPE = type(RLock())
_EXPECTED_SNAPSHOT_KEYS = frozenset(
    {
        "adapter_kind",
        "operation_role",
        "route_source",
        "conversation_id",
        "conversation_scope_revision",
        "pending_operation_id",
        "operation_id",
        "tool_call_id",
        "tool_name",
        "fingerprint_key_id",
        "raw_args",
        "normalized_args",
        "proposal_fingerprint",
        "confirmation_token_fingerprint",
        "authorization_scope_fingerprint",
        "input_fingerprint",
        "operation_request_fingerprint",
        "pending_confirmation_claim_id",
        "pending_confirmation_claimed_at",
    }
)
_TRANSACTION_CONTROL_KEYWORDS = frozenset(
    {"ABORT", "BEGIN", "COMMIT", "END", "RELEASE", "ROLLBACK", "SAVEPOINT"}
)
_RegistryKey = TypeVar("_RegistryKey")
_RegistryValue = TypeVar("_RegistryValue")


class _StateRegistry(dict[_RegistryKey, _RegistryValue]):
    __slots__ = ("__weakref__",)


def _require_canonical_uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"Legacy {label} must be an exact UUID string")
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Legacy {label} must be a canonical UUID") from exc
    if value != canonical:
        raise ValueError(f"Legacy {label} must be a canonical UUID")
    return canonical


def _require_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if type(value) is not str:
        raise TypeError(f"Legacy {label} must be exact text")
    if not value or len(value) > maximum:
        raise ValueError(f"Legacy {label} is empty or too long")
    return value


def _materialize_json(value: object) -> JSONValue:
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("Legacy normalized argument keys must be exact strings")
            result[key] = _materialize_json(child)
        return result
    if type(value) is tuple:
        return [_materialize_json(child) for child in value]
    if type(value) is list:
        return [_materialize_json(child) for child in value]
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JSONValue, value)
    raise TypeError("Legacy normalized arguments contain a non-JSON value")


def _same_json_value(left: object, right: object) -> bool:
    """Compare canonical JSON without Python's bool/int equality collapse."""

    return canonical_json(_materialize_json(left)) == canonical_json(_materialize_json(right))


def _canonical_claimed_at(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not datetime:
        raise TypeError("Legacy claimed-at must be an exact datetime")
    candidate = value
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    else:
        candidate = candidate.astimezone(timezone.utc)
    return candidate.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_outer_transaction(session: object) -> object:
    if not isinstance(session, Session):
        raise TypeError("Legacy proof requires an exact SQLAlchemy Session")
    try:
        transaction = session.get_transaction()
        nested_transaction = session.get_nested_transaction()
        active = session.is_active
        in_transaction = session.in_transaction()
        in_nested_transaction = session.in_nested_transaction()
    except (AttributeError, TypeError) as exc:
        raise TypeError("Legacy proof requires an exact SQLAlchemy Session") from exc
    if (
        active is not True
        or in_transaction is not True
        or transaction is None
        or nested_transaction is not None
        or in_nested_transaction is not False
        or getattr(transaction, "session", None) is not session
        or getattr(transaction, "parent", object()) is not None
        or getattr(transaction, "nested", None) is not False
        or getattr(transaction, "is_active", None) is not True
    ):
        raise ValueError("Legacy proof requires the caller's active outer transaction")
    return transaction


def _leading_sql_keyword(statement: object) -> str | None:
    if type(statement) is not str:
        return None
    candidate = statement.lstrip()
    while candidate:
        if candidate.startswith(";"):
            candidate = candidate[1:].lstrip()
            continue
        if candidate.startswith("--"):
            newline = candidate.find("\n")
            if newline < 0:
                return None
            candidate = candidate[newline + 1 :].lstrip()
            continue
        if candidate.startswith("/*"):
            closing = candidate.find("*/", 2)
            if closing < 0:
                return None
            candidate = candidate[closing + 2 :].lstrip()
            continue
        break
    if not candidate:
        return None
    return candidate.split(None, 1)[0].rstrip(";").upper()


def _require_unbegun_session(session: object) -> Session:
    if not isinstance(session, Session):
        raise TypeError("Legacy proof requires an exact SQLAlchemy Session")
    try:
        active = session.is_active
        in_transaction = session.in_transaction()
        transaction = session.get_transaction()
    except (AttributeError, TypeError) as exc:
        raise TypeError("Legacy proof requires an exact SQLAlchemy Session") from exc
    if active is not True or in_transaction is not False or transaction is not None:
        raise ValueError("Legacy proof issuance requires an unbegun Session for BEGIN IMMEDIATE")
    return session


def _same_identity_tuple(left: object, right: tuple[object, ...]) -> bool:
    return (
        type(left) is tuple
        and len(left) == len(right)
        and all(expected is actual for expected, actual in zip(left, right))
    )


class _SealedValue(TransientToolRuntimeValue):
    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy confirmation route value is sealed")


class LockedLegacyRouteEvidence(_SealedValue):
    """Opaque, verifier-registered evidence consumed in the issuing stack."""

    __slots__ = ("_verifier", "_identity", "_integrity_seal")
    _verifier: LegacyPendingIdentityVerifierPort
    _identity: object
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        verifier: LegacyPendingIdentityVerifierPort,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Locked Legacy evidence is verifier factory-created")
        identity = object()
        object.__setattr__(self, "_verifier", verifier)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_integrity_seal", (verifier, identity))

    def _require_verifier(self, verifier: LegacyPendingIdentityVerifierPort) -> object:
        try:
            if self._verifier is not verifier or not _same_identity_tuple(
                self._integrity_seal,
                (self._verifier, self._identity),
            ):
                raise ValueError("Locked Legacy evidence has no verifier identity")
            return self._identity
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Locked Legacy evidence has no verifier identity") from exc


class _TransactionControlFence(_SealedValue):
    __slots__ = (
        "_identity",
        "_violated",
        "_internal_depth",
        "_authorizer_observations",
        "_integrity_seal",
    )
    _identity: object
    _violated: bool
    _internal_depth: int
    _authorizer_observations: int
    _integrity_seal: tuple[object, ...]

    def __init__(self) -> None:
        identity = object()
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_violated", False)
        object.__setattr__(self, "_internal_depth", 0)
        object.__setattr__(self, "_authorizer_observations", 0)
        self._seal()

    def _seal(self) -> None:
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                self,
                self._identity,
                self._violated,
                self._internal_depth,
                self._authorizer_observations,
            ),
        )

    def _require_integrity(self) -> None:
        try:
            if (
                type(self._violated) is not bool
                or type(self._internal_depth) is not int
                or self._internal_depth < 0
                or type(self._authorizer_observations) is not int
                or self._authorizer_observations < 0
                or not _same_identity_tuple(
                    self._integrity_seal,
                    (
                        self,
                        self._identity,
                        self._violated,
                        self._internal_depth,
                        self._authorizer_observations,
                    ),
                )
            ):
                raise ValueError("Legacy transaction-control fence integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy transaction-control fence integrity drift") from exc

    @property
    def violated(self) -> bool:
        self._require_integrity()
        return self._violated

    @property
    def authorizer_observations(self) -> int:
        self._require_integrity()
        return self._authorizer_observations

    def _enter_internal(self) -> None:
        self._require_integrity()
        object.__setattr__(self, "_internal_depth", self._internal_depth + 1)
        self._seal()

    def _exit_internal(self) -> None:
        self._require_integrity()
        if self._internal_depth <= 0:
            raise ValueError("Legacy transaction-control fence is not internal")
        object.__setattr__(self, "_internal_depth", self._internal_depth - 1)
        self._seal()

    def _observe(self, statement: object) -> None:
        self._require_integrity()
        if (
            self._internal_depth == 0
            and _leading_sql_keyword(statement) in _TRANSACTION_CONTROL_KEYWORDS
        ):
            self._violate()
            raise ValueError("Legacy issuance forbids transaction control SQL")

    def _authorize(self, action: int, _operation: str | None) -> int:
        try:
            self._require_integrity()
            if action not in {sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_TRANSACTION}:
                return sqlite3.SQLITE_OK
            if self._internal_depth == 0:
                self._violate()
                return sqlite3.SQLITE_DENY
            object.__setattr__(
                self,
                "_authorizer_observations",
                self._authorizer_observations + 1,
            )
            self._seal()
            return sqlite3.SQLITE_OK
        except BaseException:
            return sqlite3.SQLITE_DENY

    def _violate(self) -> None:
        self._require_integrity()
        object.__setattr__(self, "_violated", True)
        self._seal()


@dataclass(slots=True, eq=False)
class _EvidenceState:
    evidence: LockedLegacyRouteEvidence
    identity: object
    kind: Literal["read", "locked"]
    snapshot: Mapping[str, object]
    session: Session | None
    transaction: object | None
    issuance_lease: LegacyRouteIssuanceLease | None = None
    claim_lease: LegacyClaimLease | None = None
    prepared_call: PreparedLegacyCall | None = None
    integrity_seal: tuple[object, ...] = ()


@dataclass(slots=True, eq=False)
class _IssuanceState:
    lease: LegacyRouteIssuanceLease
    session: Session
    transaction: object
    connection: Connection
    connection_transaction: object
    transaction_guard_name: str
    transaction_control_fence: _TransactionControlFence
    dbapi_connection: sqlite3.Connection
    transaction_control_authorizer: Callable[
        [int, str | None, str | None, str | None, str | None], int
    ]
    session_token: object
    prepared_call: PreparedLegacyCall
    transaction_control_listener: Callable[..., None]
    locked_evidence: LockedLegacyRouteEvidence | None = None
    locked_snapshot: Mapping[str, object] | None = None
    claim_lease: LegacyClaimLease | None = None
    claimed_snapshot: Mapping[str, object] | None = None
    proof_snapshot_consumed: bool = False
    integrity_seal: tuple[object, ...] = ()


def _transaction_guard_statement(command: Literal["RELEASE", "SAVEPOINT"], name: str) -> str:
    if (
        type(name) is not str
        or not name.startswith("offerpilot_legacy_")
        or not name.replace("_", "").isalnum()
    ):
        raise ValueError("Legacy transaction guard identity drift")
    if command == "RELEASE":
        return f'RELEASE SAVEPOINT "{name}"'
    return f'SAVEPOINT "{name}"'


def _verify_transaction_guard(state: _IssuanceState) -> None:
    fence = state.transaction_control_fence
    observations = fence.authorizer_observations
    nonce = uuid4().hex
    fence._enter_internal()
    try:
        state.connection.exec_driver_sql(
            f"{_transaction_guard_statement('RELEASE', state.transaction_guard_name)} /* {nonce} */"
        )
        state.connection.exec_driver_sql(
            f"{_transaction_guard_statement('SAVEPOINT', state.transaction_guard_name)} "
            f"/* {nonce} */"
        )
        if fence.authorizer_observations != observations + 2:
            raise ValueError("Legacy issuance database authorizer provenance changed")
    except BaseException as exc:
        fence._violate()
        raise ValueError("Legacy issuance database transaction ABA provenance changed") from exc
    finally:
        fence._exit_internal()


def _release_transaction_guard(
    connection: Connection,
    fence: _TransactionControlFence,
    guard_name: str,
) -> None:
    try:
        fence._enter_internal()
        try:
            connection.exec_driver_sql(_transaction_guard_statement("RELEASE", guard_name))
        finally:
            fence._exit_internal()
    except BaseException:
        pass


def _remove_transaction_control_listener(
    connection: Connection,
    listener: Callable[..., None],
) -> None:
    try:
        if event.contains(connection, "before_cursor_execute", listener):
            event.remove(connection, "before_cursor_execute", listener)
    except BaseException:
        pass


def _remove_transaction_control_authorizer(
    dbapi_connection: sqlite3.Connection,
) -> None:
    try:
        dbapi_connection.set_authorizer(None)
    except BaseException:
        pass


def _close_transaction_control_boundary(
    connection: Connection,
    fence: _TransactionControlFence,
    guard_name: str,
    listener: Callable[..., None],
    dbapi_connection: sqlite3.Connection,
) -> None:
    _remove_transaction_control_listener(connection, listener)
    _release_transaction_guard(connection, fence, guard_name)
    _remove_transaction_control_authorizer(dbapi_connection)


class LegacyPendingIdentityVerifierPort(_SealedValue):
    """Exact caller-transaction verifier and issuance-lease capability."""

    __slots__ = (
        "_backend",
        "_read_snapshot_backend",
        "_locked_recheck_backend",
        "_claim_cas_backend",
        "_ledger_key",
        "_key_id",
        "_secret",
        "_lock",
        "_preparation_registry",
        "_proof_registry",
        "_issuer_instance_token",
        "_runtime_container_token",
        "_evidence",
        "_issuance",
        "_evidence_root",
        "_issuance_root",
        "_integrity_seal",
    )
    _backend: object
    _read_snapshot_backend: Callable[
        [Session, LegacyConfirmationLookupIdentity, LegacyApprovedConfirmationInput],
        object,
    ]
    _locked_recheck_backend: Callable[
        [Session, LegacyRouteIssuanceLease, PreparedLegacyCall],
        object,
    ]
    _claim_cas_backend: Callable[
        [Session, LegacyRouteIssuanceLease, PreparedLegacyCall],
        object,
    ]
    _ledger_key: LedgerKeyDomain
    _key_id: str
    _secret: bytes
    _lock: RLock | None
    _preparation_registry: LegacyPreparationRegistry | None
    _proof_registry: LegacyRouteProofRegistry | None
    _issuer_instance_token: object | None
    _runtime_container_token: object | None
    _evidence: _StateRegistry[LockedLegacyRouteEvidence, _EvidenceState]
    _issuance: _StateRegistry[LegacyRouteIssuanceLease, _IssuanceState]
    _evidence_root: ReferenceType[_StateRegistry[LockedLegacyRouteEvidence, _EvidenceState]]
    _issuance_root: ReferenceType[_StateRegistry[LegacyRouteIssuanceLease, _IssuanceState]]
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        backend: object,
        ledger_key: object,
        lock: RLock | None = None,
        preparation_registry: LegacyPreparationRegistry | None = None,
        proof_registry: LegacyRouteProofRegistry | None = None,
        issuer_instance_token: object | None = None,
        runtime_container_token: object | None = None,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy verifier Ports are factory-created")
        read_snapshot = getattr(backend, "read_snapshot", None)
        locked_recheck = getattr(backend, "locked_recheck", None)
        claim_cas = getattr(backend, "claim_cas", None)
        if not callable(read_snapshot) or not callable(locked_recheck) or not callable(claim_cas):
            raise TypeError("Legacy verifier backend requires read, locked, and claim callbacks")
        key_id = _require_canonical_uuid(
            getattr(ledger_key, "key_id", None),
            "fingerprint key id",
        )
        secret = getattr(ledger_key, "secret", None)
        if type(secret) is not bytes or len(secret) != 32:
            raise TypeError("Legacy verifier key secret must be exact 32-byte material")
        bound_values = (
            lock,
            preparation_registry,
            proof_registry,
            issuer_instance_token,
            runtime_container_token,
        )
        if any(value is None for value in bound_values) and any(
            value is not None for value in bound_values
        ):
            raise TypeError("Legacy verifier topology must be bound atomically")
        object.__setattr__(self, "_backend", backend)
        typed_read_snapshot = cast(
            Callable[
                [Session, LegacyConfirmationLookupIdentity, LegacyApprovedConfirmationInput],
                object,
            ],
            read_snapshot,
        )
        typed_locked_recheck = cast(
            Callable[
                [Session, LegacyRouteIssuanceLease, PreparedLegacyCall],
                object,
            ],
            locked_recheck,
        )
        typed_claim_cas = cast(
            Callable[
                [Session, LegacyRouteIssuanceLease, PreparedLegacyCall],
                object,
            ],
            claim_cas,
        )
        typed_ledger_key = cast(LedgerKeyDomain, ledger_key)
        object.__setattr__(self, "_read_snapshot_backend", typed_read_snapshot)
        object.__setattr__(self, "_locked_recheck_backend", typed_locked_recheck)
        object.__setattr__(self, "_claim_cas_backend", typed_claim_cas)
        object.__setattr__(self, "_ledger_key", typed_ledger_key)
        object.__setattr__(self, "_key_id", key_id)
        object.__setattr__(self, "_secret", secret)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_preparation_registry", preparation_registry)
        object.__setattr__(self, "_proof_registry", proof_registry)
        object.__setattr__(self, "_issuer_instance_token", issuer_instance_token)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        evidence: _StateRegistry[LockedLegacyRouteEvidence, _EvidenceState] = _StateRegistry()
        issuance: _StateRegistry[LegacyRouteIssuanceLease, _IssuanceState] = _StateRegistry()
        evidence_root = ref(evidence)
        issuance_root = ref(issuance)
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_issuance", issuance)
        object.__setattr__(self, "_evidence_root", evidence_root)
        object.__setattr__(self, "_issuance_root", issuance_root)
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                backend,
                typed_read_snapshot,
                typed_locked_recheck,
                typed_claim_cas,
                typed_ledger_key,
                secret,
                lock,
                preparation_registry,
                proof_registry,
                issuer_instance_token,
                runtime_container_token,
                evidence_root,
                issuance_root,
            ),
        )

    @classmethod
    def _configuration(
        cls, *, backend: object, ledger_key: object
    ) -> LegacyPendingIdentityVerifierPort:
        return cls(_VALUE_SEAL, backend=backend, ledger_key=ledger_key)

    @classmethod
    def _bind(
        cls,
        template: LegacyPendingIdentityVerifierPort,
        *,
        lock: RLock,
        preparation_registry: LegacyPreparationRegistry,
        proof_registry: LegacyRouteProofRegistry,
        issuer_instance_token: object,
        runtime_container_token: object,
    ) -> LegacyPendingIdentityVerifierPort:
        template._require_integrity(require_bound=False)
        if template._lock is not None:
            raise ValueError("Legacy verifier Port is already bound")
        return cls(
            _VALUE_SEAL,
            backend=template._backend,
            ledger_key=template._ledger_key,
            lock=lock,
            preparation_registry=preparation_registry,
            proof_registry=proof_registry,
            issuer_instance_token=issuer_instance_token,
            runtime_container_token=runtime_container_token,
        )

    def _require_integrity(self, *, require_bound: bool = True) -> None:
        try:
            current = (
                self._backend,
                self._read_snapshot_backend,
                self._locked_recheck_backend,
                self._claim_cas_backend,
                self._ledger_key,
                self._secret,
                self._lock,
                self._preparation_registry,
                self._proof_registry,
                self._issuer_instance_token,
                self._runtime_container_token,
                self._evidence_root,
                self._issuance_root,
            )
            bound_values = current[6:11]
            if (
                not _same_identity_tuple(self._integrity_seal, current)
                or getattr(self._ledger_key, "key_id", None) != self._key_id
                or getattr(self._ledger_key, "secret", None) is not self._secret
                or type(self._evidence) is not _StateRegistry
                or type(self._issuance) is not _StateRegistry
                or self._evidence_root() is not self._evidence
                or self._issuance_root() is not self._issuance
                or (require_bound and any(value is None for value in bound_values))
            ):
                raise ValueError("Legacy pending identity verifier integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy pending identity verifier integrity drift") from exc

    def _topology(
        self,
    ) -> tuple[RLock, LegacyPreparationRegistry, LegacyRouteProofRegistry, object, object]:
        self._require_integrity()
        return (
            cast(RLock, self._lock),
            cast(LegacyPreparationRegistry, self._preparation_registry),
            cast(LegacyRouteProofRegistry, self._proof_registry),
            self._issuer_instance_token,
            self._runtime_container_token,
        )

    @staticmethod
    def _evidence_state_identity(state: _EvidenceState) -> tuple[object, ...]:
        return (
            state,
            state.evidence,
            state.identity,
            state.kind,
            state.snapshot,
            state.session,
            state.transaction,
            state.issuance_lease,
            state.claim_lease,
            state.prepared_call,
        )

    def _seal_evidence_state(self, state: _EvidenceState) -> None:
        state.integrity_seal = self._evidence_state_identity(state)

    def _require_evidence_state_integrity(
        self,
        evidence: LockedLegacyRouteEvidence,
        state: _EvidenceState,
    ) -> None:
        try:
            identity = evidence._require_verifier(self)
            if (
                type(state) is not _EvidenceState
                or not _same_identity_tuple(
                    state.integrity_seal,
                    self._evidence_state_identity(state),
                )
                or state.evidence is not evidence
                or state.identity is not identity
                or self._evidence.get(evidence) is not state
                or not isinstance(state.snapshot, Mapping)
                or state.kind not in {"read", "locked"}
            ):
                raise ValueError("Legacy evidence Registry state identity drift")
            if state.kind == "read":
                if any(
                    value is not None
                    for value in (
                        state.session,
                        state.transaction,
                        state.issuance_lease,
                        state.claim_lease,
                        state.prepared_call,
                    )
                ):
                    raise ValueError("Legacy read evidence state shape drift")
            elif (
                not isinstance(state.session, Session)
                or state.transaction is None
                or type(state.issuance_lease) is not LegacyRouteIssuanceLease
                or state.claim_lease is not None
                or type(state.prepared_call) is not PreparedLegacyCall
            ):
                raise ValueError("Legacy locked evidence state shape drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy evidence Registry state integrity drift") from exc

    @staticmethod
    def _issuance_state_identity(state: _IssuanceState) -> tuple[object, ...]:
        return (
            state,
            state.lease,
            state.session,
            state.transaction,
            state.connection,
            state.connection_transaction,
            state.transaction_guard_name,
            state.transaction_control_fence,
            state.dbapi_connection,
            state.transaction_control_authorizer,
            state.session_token,
            state.prepared_call,
            state.transaction_control_listener,
            state.locked_evidence,
            state.locked_snapshot,
            state.claim_lease,
            state.claimed_snapshot,
            state.proof_snapshot_consumed,
        )

    def _seal_issuance_state(self, state: _IssuanceState) -> None:
        state.integrity_seal = self._issuance_state_identity(state)

    def _require_issuance_state_integrity(
        self,
        issuance_lease: LegacyRouteIssuanceLease,
        state: _IssuanceState,
    ) -> None:
        try:
            if (
                type(state) is not _IssuanceState
                or not _same_identity_tuple(
                    state.integrity_seal,
                    self._issuance_state_identity(state),
                )
                or state.lease is not issuance_lease
                or self._issuance.get(issuance_lease) is not state
                or not isinstance(state.session, Session)
                or not isinstance(state.connection, Connection)
                or type(state.transaction_guard_name) is not str
                or type(state.transaction_control_fence) is not _TransactionControlFence
                or not isinstance(state.dbapi_connection, sqlite3.Connection)
                or not callable(state.transaction_control_authorizer)
                or type(state.session_token) is not object
                or type(state.prepared_call) is not PreparedLegacyCall
                or not callable(state.transaction_control_listener)
                or type(state.proof_snapshot_consumed) is not bool
            ):
                raise ValueError("Legacy issuance Registry state identity drift")
            state.transaction_control_fence._require_integrity()
            preparation = cast(LegacyPreparationRegistry, self._preparation_registry)
            state.prepared_call._require_registry(preparation)
            if state.locked_evidence is not None:
                if type(state.locked_evidence) is not LockedLegacyRouteEvidence or not isinstance(
                    state.locked_snapshot, Mapping
                ):
                    raise ValueError("Legacy issuance locked state shape drift")
            elif state.locked_snapshot is not None and not isinstance(
                state.locked_snapshot,
                Mapping,
            ):
                raise ValueError("Legacy issuance locked snapshot shape drift")
            if state.claim_lease is None:
                if state.claimed_snapshot is not None or state.proof_snapshot_consumed:
                    raise ValueError("Legacy issuance unclaimed state shape drift")
            elif type(state.claim_lease) is not LegacyClaimLease:
                raise ValueError("Legacy issuance claim state shape drift")
            elif state.proof_snapshot_consumed:
                if state.claimed_snapshot is not None:
                    raise ValueError("Legacy issuance consumed proof state shape drift")
            elif not isinstance(state.claimed_snapshot, Mapping):
                raise ValueError("Legacy issuance claimed state shape drift")
            if state.claim_lease is not None and (
                state.locked_evidence is not None or state.locked_snapshot is not None
            ):
                raise ValueError("Legacy issuance locked/claim transition drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy issuance Registry state integrity drift") from exc

    def _validate_snapshot_shape(
        self,
        raw: object,
        *,
        lookup_identity: LegacyConfirmationLookupIdentity,
        confirmation_input: LegacyApprovedConfirmationInput,
        claimed: bool,
    ) -> Mapping[str, object]:
        if not isinstance(raw, Mapping) or frozenset(raw) != _EXPECTED_SNAPSHOT_KEYS:
            raise ValueError("Legacy pending identity snapshot has the wrong exact shape")
        snapshot = dict(raw)
        if snapshot["adapter_kind"] != "legacy_deterministic":
            raise ValueError("Legacy pending adapter identity mismatch")
        if snapshot["operation_role"] != "primary":
            raise ValueError("Legacy pending operation-role identity mismatch")
        if snapshot["route_source"] != "confirmation_resume":
            raise ValueError("Legacy pending route source mismatch")
        conversation_id = snapshot["conversation_id"]
        if type(conversation_id) is not int or not 0 < conversation_id <= 2**63 - 1:
            raise ValueError("Legacy conversation identity is invalid")
        if conversation_id != lookup_identity.conversation_id:
            raise ValueError("Legacy conversation identity mismatch")
        scope_revision = snapshot["conversation_scope_revision"]
        if type(scope_revision) is not int or scope_revision < 0:
            raise ValueError("Legacy conversation scope revision is invalid")
        operation_id = _require_canonical_uuid(snapshot["operation_id"], "operation id")
        pending_operation_id = _require_canonical_uuid(
            snapshot["pending_operation_id"],
            "pending operation id",
        )
        if pending_operation_id != operation_id:
            raise ValueError("Legacy pending operation identity mismatch")
        if (
            confirmation_input.operation_id is not None
            and confirmation_input.operation_id != operation_id
        ):
            raise ValueError("Legacy confirmation operation identity mismatch")
        _require_text(snapshot["tool_call_id"], "tool call id")
        _require_text(snapshot["tool_name"], "persisted protocol name")
        key_id = _require_canonical_uuid(snapshot["fingerprint_key_id"], "fingerprint key id")
        if key_id != self._key_id:
            raise ValueError("Legacy fingerprint key identity mismatch")
        decoded = _decode_legacy_arguments_object(snapshot["raw_args"])
        normalized = _materialize_json(snapshot["normalized_args"])
        if type(normalized) is not dict or not _same_json_value(normalized, decoded):
            raise ValueError("Legacy snapshot normalized JSON argument identity is stale")
        proposal = _require_text(snapshot["proposal_fingerprint"], "proposal fingerprint")
        expected_proposal = ledger_fingerprint(
            self._ledger_key,
            "write-operation-proposal-v1",
            decoded,
        )
        if not hmac.compare_digest(proposal, expected_proposal):
            raise ValueError("Legacy proposal fingerprint mismatch")
        token_fingerprint = _require_text(
            snapshot["confirmation_token_fingerprint"],
            "confirmation token fingerprint",
        )
        try:
            token_bytes = confirmation_input.confirmation_token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Legacy confirmation token must be ASCII") from exc
        expected_token = ledger_fingerprint(
            self._ledger_key,
            "write-operation-confirmation-token-v1",
            token_bytes,
        )
        if not hmac.compare_digest(token_fingerprint, expected_token):
            raise ValueError("Legacy confirmation token fingerprint mismatch")
        if snapshot["authorization_scope_fingerprint"] is not None:
            raise ValueError("Legacy authorization scope fingerprint must stay null")
        if snapshot["input_fingerprint"] is not None:
            raise ValueError("Legacy input fingerprint must stay null before execution")
        if snapshot["operation_request_fingerprint"] is not None:
            raise ValueError("Legacy operation request fingerprint must stay null before execution")
        claim_id = snapshot["pending_confirmation_claim_id"]
        claimed_at = snapshot["pending_confirmation_claimed_at"]
        if claimed:
            _require_text(claim_id, "pending claim id")
            if claimed_at is None:
                raise ValueError("Legacy claimed identity requires claimed-at")
            _canonical_claimed_at(claimed_at)
        elif claim_id != "" or claimed_at is not None:
            raise ValueError("Legacy proposed claim identity must be unclaimed")
        frozen_normalized = freeze_json(decoded)
        if not isinstance(frozen_normalized, Mapping):
            raise TypeError("Legacy normalized snapshot arguments must remain an object")
        snapshot["normalized_args"] = frozen_normalized
        return MappingProxyType(snapshot)

    def _register_evidence(
        self,
        *,
        kind: Literal["read", "locked"],
        snapshot: Mapping[str, object],
        session: Session | None,
        transaction: object | None,
        issuance_lease: LegacyRouteIssuanceLease | None = None,
        claim_lease: LegacyClaimLease | None = None,
        prepared_call: PreparedLegacyCall | None = None,
    ) -> LockedLegacyRouteEvidence:
        lock, _preparation, _proof, _issuer_token, _runtime = self._topology()
        with lock:
            evidence = LockedLegacyRouteEvidence(_VALUE_SEAL, verifier=self)
            identity = evidence._require_verifier(self)
            state = _EvidenceState(
                evidence=evidence,
                identity=identity,
                kind=kind,
                snapshot=snapshot,
                session=session,
                transaction=transaction,
                issuance_lease=issuance_lease,
                claim_lease=claim_lease,
                prepared_call=prepared_call,
            )
            self._seal_evidence_state(state)
            self._evidence[evidence] = state
            return evidence

    def _consume_evidence(
        self,
        evidence: LockedLegacyRouteEvidence,
        *,
        kind: Literal["read", "locked"],
        issuer_instance_token: object,
    ) -> _EvidenceState:
        lock, _preparation, _proof, expected_issuer, _runtime = self._topology()
        with lock:
            if issuer_instance_token is not expected_issuer:
                raise ValueError("Locked Legacy evidence has the wrong issuer identity")
            if type(evidence) is not LockedLegacyRouteEvidence:
                raise TypeError("Legacy issuer requires exact Locked Evidence")
            state = self._evidence.get(evidence)
            if state is None:
                raise ValueError("Locked Legacy evidence is forged or already consumed")
            self._require_evidence_state_integrity(evidence, state)
            if state.kind != kind:
                raise ValueError("Locked Legacy evidence is forged or already consumed")
            self._evidence.pop(evidence, None)
            return state

    def _revoke_evidence(self, evidence: object) -> None:
        if self._lock is None:
            return
        with self._lock:
            if type(evidence) is LockedLegacyRouteEvidence:
                self._evidence.pop(evidence, None)

    def read_snapshot(
        self,
        read_session: Session,
        lookup_identity: LegacyConfirmationLookupIdentity,
        confirmation_input: LegacyApprovedConfirmationInput,
    ) -> LockedLegacyRouteEvidence:
        self._require_integrity()
        if type(lookup_identity) is not LegacyConfirmationLookupIdentity:
            raise TypeError("Legacy prepare requires exact lookup identity")
        if type(confirmation_input) is not LegacyApprovedConfirmationInput:
            raise TypeError("Legacy prepare requires exact approved confirmation input")
        transaction = _require_outer_transaction(read_session)
        failure: BaseException | None = None
        snapshot: Mapping[str, object] | None = None
        try:
            raw = self._read_snapshot_backend(
                read_session,
                lookup_identity,
                confirmation_input,
            )
            snapshot = self._validate_snapshot_shape(
                raw,
                lookup_identity=lookup_identity,
                confirmation_input=confirmation_input,
                claimed=False,
            )
        except BaseException as exc:
            failure = exc
        try:
            rollback = getattr(transaction, "rollback")
            rollback()
        except BaseException as cleanup_error:
            if failure is None:
                failure = ValueError("Legacy read transaction could not close safely")
                failure.__cause__ = cleanup_error
        if failure is not None:
            raise failure
        if snapshot is None or read_session.in_transaction():
            raise ValueError("Legacy read transaction must close before pure preparation")
        return self._register_evidence(
            kind="read",
            snapshot=snapshot,
            session=None,
            transaction=None,
        )

    def _prepared_fingerprints(
        self,
        *,
        snapshot: Mapping[str, object],
        confirmation_input: LegacyApprovedConfirmationInput,
        effective_args: Mapping[str, JSONValue],
    ) -> Mapping[str, object]:
        operation_id = cast(str, snapshot["operation_id"])
        tool_call_id = cast(str, snapshot["tool_call_id"])
        proposal = cast(str, snapshot["proposal_fingerprint"])
        token_fingerprint = cast(str, snapshot["confirmation_token_fingerprint"])
        edits = confirmation_input.edited_args
        typed_edits = (
            cast(Mapping[str, JSONValue], _materialize_json(edits)) if edits is not None else None
        )
        persisted = cast(dict[str, JSONValue], _materialize_json(snapshot["normalized_args"]))
        return MappingProxyType(
            {
                "persisted_args_fingerprint": ledger_fingerprint(
                    self._ledger_key,
                    "legacy-route-persisted-args-v1",
                    persisted,
                ),
                "effective_input_fingerprint": ledger_fingerprint(
                    self._ledger_key,
                    "write-operation-legacy-input-v1",
                    dict(effective_args),
                ),
                "operation_request_fingerprint": operation_request_fingerprint(
                    self._ledger_key,
                    operation_id=operation_id,
                    tool_call_id=tool_call_id,
                    approved=True,
                    edited_args_present=confirmation_input.edited_args_present,
                    edited_args=typed_edits,
                    rejection_feedback_present=False,
                    rejection_feedback="",
                    confirmation_token_fingerprint=token_fingerprint,
                    proposal_fingerprint=proposal,
                ),
            }
        )

    def _prepared_identity_inputs(
        self,
        prepared_call: PreparedLegacyCall,
    ) -> tuple[
        LegacyDeterministicAdapterSpec,
        str,
        str,
        Mapping[str, object],
        LegacyConfirmationLookupIdentity,
        LegacyApprovedConfirmationInput,
    ]:
        _lock, preparation, _proof, _issuer, _runtime = self._topology()
        adapter, raw_args, effective_args, metadata, _binding, _prepared = (
            preparation._inspect_prepared(prepared_call)
        )
        lookup = LegacyConfirmationLookupIdentity(
            conversation_id=cast(int, metadata["conversation_id"]),
        )
        confirmation_input = LegacyApprovedConfirmationInput(
            decision="approved",
            operation_id=cast(str, metadata["operation_id"]),
            confirmation_token=cast(str, metadata["confirmation_token"]),
            edited_args_present=cast(bool, metadata["edited_args_present"]),
            edited_args=metadata["edited_args"],
            rejection_feedback_present=False,
            rejection_feedback="",
        )
        return (
            cast(LegacyDeterministicAdapterSpec, adapter),
            raw_args,
            effective_args,
            metadata,
            lookup,
            confirmation_input,
        )

    def _validate_prepared_snapshot_identity(
        self,
        *,
        snapshot: Mapping[str, object],
        raw_args: str,
        effective_args: str,
        metadata: Mapping[str, object],
        confirmation_input: LegacyApprovedConfirmationInput,
    ) -> tuple[dict[str, JSONValue], Mapping[str, object]]:
        exact_fields = (
            ("conversation_id", "conversation_id"),
            ("conversation_scope_revision", "conversation_scope_revision"),
            ("pending_operation_id", "pending_operation_id"),
            ("operation_id", "operation_id"),
            ("tool_call_id", "tool_call_id"),
            ("tool_name", "tool_name"),
            ("fingerprint_key_id", "fingerprint_key_id"),
            ("proposal_fingerprint", "proposal_fingerprint"),
            ("confirmation_token_fingerprint", "confirmation_token_fingerprint"),
        )
        for snapshot_key, metadata_key in exact_fields:
            if snapshot[snapshot_key] != metadata[metadata_key]:
                raise ValueError(f"Legacy locked identity mismatch: {snapshot_key}")
        if snapshot["raw_args"] != raw_args:
            raise ValueError("Legacy locked raw argument identity mismatch")
        decoded_raw = _decode_legacy_arguments_object(raw_args)
        if not _same_json_value(snapshot["normalized_args"], decoded_raw):
            raise ValueError("Legacy locked normalized argument identity mismatch")
        effective_object = _decode_legacy_arguments_object(effective_args)
        fingerprints = self._prepared_fingerprints(
            snapshot=snapshot,
            confirmation_input=confirmation_input,
            effective_args=effective_object,
        )
        for key, value in fingerprints.items():
            if metadata.get(key) != value:
                raise ValueError(f"Legacy prepared fingerprint identity mismatch: {key}")
        return decoded_raw, fingerprints

    def open_issuance_lease(
        self,
        write_session: Session,
        prepared_call: PreparedLegacyCall,
    ) -> LegacyRouteIssuanceLease:
        lock, preparation, _proof, _issuer, runtime = self._topology()
        preparation._inspect_prepared(prepared_call)
        _require_unbegun_session(write_session)
        with lock:
            preparation._inspect_prepared(prepared_call)
            if any(state.prepared_call is prepared_call for state in self._issuance.values()):
                raise ValueError("Legacy PreparedCall already has an issuance lease")
            _require_unbegun_session(write_session)
            lease: LegacyRouteIssuanceLease | None = None
            connection: Connection | None = None
            fence: _TransactionControlFence | None = None
            guard_name: str | None = None
            listener: Callable[..., None] | None = None
            dbapi_connection: sqlite3.Connection | None = None
            authorizer: (
                Callable[[int, str | None, str | None, str | None, str | None], int] | None
            ) = None
            try:
                write_session.execute(text("BEGIN IMMEDIATE"))
                transaction = _require_outer_transaction(write_session)
                connection = write_session.connection()
                connection_transaction = connection.get_transaction()
                if (
                    connection.in_transaction() is not True
                    or connection_transaction is None
                    or getattr(connection_transaction, "is_active", None) is not True
                ):
                    raise ValueError("Legacy issuance requires an active connection transaction")
                fence = _TransactionControlFence()
                guard_name = f"offerpilot_legacy_{uuid4().hex}"

                raw_dbapi_connection = connection.connection.driver_connection
                if not isinstance(raw_dbapi_connection, sqlite3.Connection):
                    raise TypeError("Legacy issuance requires the SQLite DBAPI connection")
                dbapi_connection = raw_dbapi_connection

                def authorize_transaction_control(
                    action: int,
                    _arg1: str | None,
                    _arg2: str | None,
                    _database: str | None,
                    _trigger: str | None,
                ) -> int:
                    return fence._authorize(action, _arg1)

                authorizer = authorize_transaction_control
                dbapi_connection.set_authorizer(authorizer)
                observations = fence.authorizer_observations
                fence._enter_internal()
                try:
                    connection.exec_driver_sql(
                        f"{_transaction_guard_statement('SAVEPOINT', guard_name)} "
                        f"/* {uuid4().hex} */"
                    )
                    if fence.authorizer_observations != observations + 1:
                        raise ValueError("Legacy issuance database authorizer provenance changed")
                finally:
                    fence._exit_internal()

                def reject_transaction_control(
                    _connection: object,
                    _cursor: object,
                    statement: object,
                    _parameters: object,
                    _context: object,
                    _executemany: object,
                ) -> None:
                    fence._observe(statement)

                listener = reject_transaction_control
                session_token = object()
                lease = LegacyRouteIssuanceLease._create(
                    lock=lock,
                    session_token=session_token,
                    runtime_container_token=runtime,
                    provenance_port=self,
                )
                state = _IssuanceState(
                    lease=lease,
                    session=write_session,
                    transaction=transaction,
                    connection=connection,
                    connection_transaction=connection_transaction,
                    transaction_guard_name=guard_name,
                    transaction_control_fence=fence,
                    dbapi_connection=dbapi_connection,
                    transaction_control_authorizer=authorizer,
                    session_token=session_token,
                    prepared_call=prepared_call,
                    transaction_control_listener=listener,
                )
                self._seal_issuance_state(state)
                self._issuance[lease] = state
                event.listen(connection, "before_cursor_execute", listener)
                lease._attach_cleanup(
                    lock=lock,
                    runtime_container_token=runtime,
                    owner=fence,
                    callback=lambda: _close_transaction_control_boundary(
                        connection,
                        fence,
                        guard_name,
                        listener,
                        dbapi_connection,
                    ),
                )
                lease._attach_cleanup(
                    lock=lock,
                    runtime_container_token=runtime,
                    owner=self,
                    callback=lambda: self._close_issuance(lease),
                )
                lease._attach_cleanup(
                    lock=lock,
                    runtime_container_token=runtime,
                    owner=prepared_call,
                    callback=lambda: preparation._revoke_prepared(prepared_call),
                )
                return lease
            except BaseException:
                if lease is not None:
                    self._close_issuance(lease)
                    lease.close()
                if (
                    connection is not None
                    and fence is not None
                    and guard_name is not None
                    and listener is not None
                    and dbapi_connection is not None
                ):
                    _close_transaction_control_boundary(
                        connection,
                        fence,
                        guard_name,
                        listener,
                        dbapi_connection,
                    )
                elif dbapi_connection is not None:
                    _remove_transaction_control_authorizer(dbapi_connection)
                preparation._revoke_prepared(prepared_call)
                try:
                    if write_session.in_transaction():
                        write_session.rollback()
                except BaseException:
                    pass
                raise

    def _require_issuance_transaction(
        self,
        issuance_lease: LegacyRouteIssuanceLease,
    ) -> _IssuanceState:
        lock, _preparation, _proof, _issuer, _runtime = self._topology()
        with lock:
            if type(issuance_lease) is not LegacyRouteIssuanceLease:
                raise TypeError("Legacy transaction provenance requires an exact issuance lease")
            state = self._issuance.get(issuance_lease)
            if state is None or state.lease is not issuance_lease:
                raise ValueError(
                    "Legacy issuance transaction provenance is closed, revoked, or has the wrong Session"
                )
            self._require_issuance_state_integrity(issuance_lease, state)
            if state.transaction_control_fence.violated:
                raise ValueError("Legacy issuance transaction-control provenance changed")
            current_transaction = _require_outer_transaction(state.session)
            current_connection = state.session.connection()
            current_connection_transaction = current_connection.get_transaction()
            if (
                current_transaction is not state.transaction
                or current_connection is not state.connection
                or current_connection.connection.driver_connection is not state.dbapi_connection
                or current_connection.in_transaction() is not True
                or current_connection_transaction is not state.connection_transaction
                or getattr(current_connection_transaction, "is_active", None) is not True
            ):
                raise ValueError("Legacy issuance transaction provenance changed")
            _verify_transaction_guard(state)
            return state

    def _require_execution_context(
        self,
        issuance_lease: LegacyRouteIssuanceLease,
        context: object,
    ) -> None:
        from offerpilot.pilot_runtime.contracts import LegacyExecutionContext

        state = self._require_issuance_transaction(issuance_lease)
        if type(context) is not LegacyExecutionContext:
            raise TypeError("Legacy executor requires the exact execution context")
        context.require_integrity()
        if context.session is not state.session:
            raise ValueError("Legacy execution context has the wrong Session provenance")
        if _require_outer_transaction(context.session) is not state.transaction:
            raise ValueError("Legacy execution context transaction provenance changed")

    def _issuance_state(
        self,
        write_session: Session,
        issuance_lease: LegacyRouteIssuanceLease,
        prepared_call: PreparedLegacyCall | None = None,
    ) -> _IssuanceState:
        lock, _preparation, _proof, _issuer, runtime = self._topology()
        with lock:
            if type(issuance_lease) is not LegacyRouteIssuanceLease:
                raise TypeError("Legacy proof requires an exact issuance lease")
            state = self._require_issuance_transaction(issuance_lease)
            if state.session is not write_session or (
                prepared_call is not None and state.prepared_call is not prepared_call
            ):
                raise ValueError(
                    "Legacy issuance attempt is closed, revoked, or has the wrong Session or preparation"
                )
            issuance_lease._require_live(
                lock=lock,
                runtime_container_token=runtime,
                session_token=state.session_token,
            )
            return state

    def bind_claim(
        self,
        write_session: Session,
        issuance_lease: LegacyRouteIssuanceLease,
        locked_evidence: LockedLegacyRouteEvidence,
    ) -> LegacyClaimLease:
        lock, _preparation, _proof, _issuer, _runtime = self._topology()
        try:
            with lock:
                state = self._issuance_state(write_session, issuance_lease)
                if type(locked_evidence) is not LockedLegacyRouteEvidence:
                    raise TypeError("Legacy claim requires exact locked mutable-recheck evidence")
                evidence_state = self._evidence.get(locked_evidence)
                if evidence_state is None:
                    raise ValueError(
                        "Legacy claim requires one exact locked mutable-recheck evidence in order"
                    )
                self._require_evidence_state_integrity(locked_evidence, evidence_state)
                if (
                    evidence_state.kind != "locked"
                    or evidence_state.session is not write_session
                    or evidence_state.transaction is not state.transaction
                    or evidence_state.issuance_lease is not issuance_lease
                    or evidence_state.prepared_call is not state.prepared_call
                    or state.locked_evidence is not locked_evidence
                    or state.locked_snapshot is not evidence_state.snapshot
                    or state.claim_lease is not None
                ):
                    raise ValueError(
                        "Legacy claim requires one exact locked mutable-recheck evidence in order"
                    )
                self._evidence.pop(locked_evidence, None)
                preclaim_snapshot = evidence_state.snapshot
                state.locked_evidence = None
                self._seal_issuance_state(state)

            raw_claimed = self._claim_cas_backend(
                write_session,
                issuance_lease,
                state.prepared_call,
            )
            (
                _adapter,
                _raw_args,
                _effective_args,
                metadata,
                lookup,
                confirmation_input,
            ) = self._prepared_identity_inputs(state.prepared_call)
            claimed_snapshot = self._validate_snapshot_shape(
                raw_claimed,
                lookup_identity=lookup,
                confirmation_input=confirmation_input,
                claimed=True,
            )
            for key in _EXPECTED_SNAPSHOT_KEYS - {
                "pending_confirmation_claim_id",
                "pending_confirmation_claimed_at",
                "normalized_args",
            }:
                if claimed_snapshot[key] != preclaim_snapshot[key]:
                    raise ValueError(f"Legacy claim CAS changed locked identity: {key}")
            if not _same_json_value(
                claimed_snapshot["normalized_args"],
                preclaim_snapshot["normalized_args"],
            ):
                raise ValueError("Legacy claim CAS changed locked normalized arguments")
            operation_id = _require_canonical_uuid(
                claimed_snapshot["operation_id"],
                "claim operation id",
            )
            claim_id = _require_text(
                claimed_snapshot["pending_confirmation_claim_id"],
                "claim id",
            )
            claimed_at = claimed_snapshot["pending_confirmation_claimed_at"]
            if type(claimed_at) is not datetime:
                raise TypeError("Legacy claimed-at must be an exact datetime")
            if operation_id != metadata.get("operation_id") or claim_id != operation_id:
                raise ValueError("Legacy claim identity does not match the prepared operation")
            _canonical_claimed_at(claimed_at)
            claim = LegacyClaimLease._create(
                issuance_lease=issuance_lease,
                operation_id=operation_id,
                claim_id=claim_id,
                claimed_at=claimed_at,
            )
            with lock:
                current_state = self._issuance_state(
                    write_session,
                    issuance_lease,
                    state.prepared_call,
                )
                if (
                    current_state is not state
                    or current_state.claim_lease is not None
                    or current_state.locked_snapshot is not preclaim_snapshot
                ):
                    raise ValueError("Legacy claim or mutable-recheck identity became stale")
                current_state.locked_snapshot = None
                current_state.claimed_snapshot = claimed_snapshot
                current_state.claim_lease = claim
                self._seal_issuance_state(current_state)
                return claim
        except BaseException:
            if type(issuance_lease) is LegacyRouteIssuanceLease:
                issuance_lease.close()
            raise

    def locked_recheck(
        self,
        write_session: Session,
        issuance_lease: LegacyRouteIssuanceLease,
        prepared_call: PreparedLegacyCall,
    ) -> LockedLegacyRouteEvidence:
        lock, preparation, _proof, _issuer, _runtime = self._topology()
        try:
            with lock:
                state = self._issuance_state(write_session, issuance_lease, prepared_call)
                if (
                    state.locked_evidence is not None
                    or state.locked_snapshot is not None
                    or state.claim_lease is not None
                ):
                    raise ValueError("Legacy locked mutable recheck can occur only once in order")
                (
                    adapter,
                    raw_args,
                    effective_args,
                    metadata,
                    lookup,
                    confirmation_input,
                ) = self._prepared_identity_inputs(prepared_call)
            raw = self._locked_recheck_backend(
                write_session,
                issuance_lease,
                prepared_call,
            )
            snapshot = self._validate_snapshot_shape(
                raw,
                lookup_identity=lookup,
                confirmation_input=confirmation_input,
                claimed=False,
            )
            self._validate_prepared_snapshot_identity(
                snapshot=snapshot,
                raw_args=raw_args,
                effective_args=effective_args,
                metadata=metadata,
                confirmation_input=confirmation_input,
            )
            with lock:
                current_state = self._issuance_state(
                    write_session,
                    issuance_lease,
                    prepared_call,
                )
                if (
                    current_state is not state
                    or current_state.locked_evidence is not None
                    or current_state.locked_snapshot is not None
                    or current_state.claim_lease is not None
                ):
                    raise ValueError("Legacy locked mutable-recheck identity became stale")
                evidence = self._register_evidence(
                    kind="locked",
                    snapshot=snapshot,
                    session=write_session,
                    transaction=state.transaction,
                    issuance_lease=issuance_lease,
                    prepared_call=prepared_call,
                )
                current_state.locked_evidence = evidence
                current_state.locked_snapshot = snapshot
                self._seal_issuance_state(current_state)
                return evidence
        except BaseException:
            if type(issuance_lease) is LegacyRouteIssuanceLease:
                issuance_lease.close()
            raise

    def _proof_safe_snapshot(
        self,
        *,
        issuer_instance_token: object,
        write_session: Session,
        issuance_lease: LegacyRouteIssuanceLease,
        claim_lease: LegacyClaimLease,
        prepared_call: PreparedLegacyCall,
    ) -> Mapping[str, object]:
        lock, _preparation, _proof, expected_issuer, _runtime = self._topology()
        with lock:
            if issuer_instance_token is not expected_issuer:
                raise ValueError("Legacy proof has the wrong issuer identity")
            state = self._issuance_state(write_session, issuance_lease, prepared_call)
            if (
                type(claim_lease) is not LegacyClaimLease
                or state.claim_lease is not claim_lease
                or state.claimed_snapshot is None
                or state.proof_snapshot_consumed
            ):
                raise ValueError("Legacy proof requires the exact live claimed identity")
            claim_identity, operation_id, claim_id, claimed_at = claim_lease._snapshot(
                issuance_lease=issuance_lease,
            )
            del claim_identity
            snapshot = state.claimed_snapshot
            (
                adapter,
                raw_args,
                effective_args,
                metadata,
                _lookup,
                confirmation_input,
            ) = self._prepared_identity_inputs(prepared_call)
            decoded_raw, fingerprints = self._validate_prepared_snapshot_identity(
                snapshot=snapshot,
                raw_args=raw_args,
                effective_args=effective_args,
                metadata=metadata,
                confirmation_input=confirmation_input,
            )
            if snapshot["pending_confirmation_claim_id"] != claim_id:
                raise ValueError("Legacy locked claim identity mismatch")
            if _canonical_claimed_at(snapshot["pending_confirmation_claimed_at"]) != (
                _canonical_claimed_at(claimed_at)
            ):
                raise ValueError("Legacy locked claim timestamp mismatch")
            if operation_id != snapshot["operation_id"]:
                raise ValueError("Legacy locked operation and claim mismatch")
            pending_identity = {
                "schema": "legacy-route-pending-identity-v1",
                "adapter_kind": "legacy_deterministic",
                "operation_role": "primary",
                "route_source": "confirmation_resume",
                "conversation_id": snapshot["conversation_id"],
                "conversation_scope_revision": snapshot["conversation_scope_revision"],
                "pending_claim_identity": {
                    "claim_id": claim_id,
                    "claimed_at": _canonical_claimed_at(claimed_at),
                },
                "operation_id": snapshot["operation_id"],
                "tool_call_id": snapshot["tool_call_id"],
                "tool_name": snapshot["tool_name"],
                "fingerprint_key_id": snapshot["fingerprint_key_id"],
                "normalized_args": decoded_raw,
            }
        route_digest = ledger_fingerprint(
            self._ledger_key,
            "legacy-route-pending-identity-v1",
            cast(JSONValue, pending_identity),
        )
        safe_snapshot = MappingProxyType(
            {
                "adapter_kind": "legacy_deterministic",
                "operation_role": "primary",
                "route_source": "confirmation_resume",
                "operation_id": snapshot["operation_id"],
                "tool_call_id": snapshot["tool_call_id"],
                "persisted_protocol_name": snapshot["tool_name"],
                "fingerprint_key_id": snapshot["fingerprint_key_id"],
                "conversation_id": snapshot["conversation_id"],
                "conversation_scope_revision": snapshot["conversation_scope_revision"],
                "claim_id": claim_id,
                "claimed_at": _canonical_claimed_at(claimed_at),
                "pending_identity_digest": route_digest,
                "effective_input_fingerprint": fingerprints["effective_input_fingerprint"],
                "operation_request_fingerprint": fingerprints["operation_request_fingerprint"],
                "adapter_ordinal": adapter.ordinal,
            }
        )
        with lock:
            current_state = self._issuance_state(
                write_session,
                issuance_lease,
                prepared_call,
            )
            if (
                current_state is not state
                or current_state.claim_lease is not claim_lease
                or current_state.claimed_snapshot is not snapshot
                or current_state.proof_snapshot_consumed
            ):
                raise ValueError("Legacy claim or issuance identity became stale")
            current_state.claimed_snapshot = None
            current_state.proof_snapshot_consumed = True
            self._seal_issuance_state(current_state)
            return safe_snapshot

    def _consume_read_evidence(
        self,
        evidence: LockedLegacyRouteEvidence,
        *,
        issuer_instance_token: object,
    ) -> Mapping[str, object]:
        return self._consume_evidence(
            evidence,
            kind="read",
            issuer_instance_token=issuer_instance_token,
        ).snapshot

    def _close_issuance(self, issuance_lease: LegacyRouteIssuanceLease) -> None:
        seal = getattr(self, "_integrity_seal", ())
        sealed_lock = seal[6] if type(seal) is tuple and len(seal) == 13 else None
        sealed_preparation = seal[7] if type(seal) is tuple and len(seal) == 13 else None
        lock = sealed_lock if type(sealed_lock) is _RLOCK_TYPE else self._lock
        preparation = (
            sealed_preparation
            if type(sealed_preparation) is LegacyPreparationRegistry
            else self._preparation_registry
        )
        if type(lock) is not _RLOCK_TYPE or type(preparation) is not LegacyPreparationRegistry:
            return
        with lock:
            sealed_evidence_root = seal[11] if type(seal) is tuple and len(seal) == 13 else None
            sealed_issuance_root = seal[12] if type(seal) is tuple and len(seal) == 13 else None
            evidence_maps: list[dict[LockedLegacyRouteEvidence, _EvidenceState]] = []
            issuance_maps: list[dict[LegacyRouteIssuanceLease, _IssuanceState]] = []

            def append_evidence_map(candidate: object) -> None:
                if isinstance(candidate, ReferenceType):
                    candidate = candidate()
                if isinstance(candidate, dict) and all(
                    existing is not candidate for existing in evidence_maps
                ):
                    evidence_maps.append(
                        cast(dict[LockedLegacyRouteEvidence, _EvidenceState], candidate)
                    )

            def append_issuance_map(candidate: object) -> None:
                if isinstance(candidate, ReferenceType):
                    candidate = candidate()
                if isinstance(candidate, dict) and all(
                    existing is not candidate for existing in issuance_maps
                ):
                    issuance_maps.append(
                        cast(dict[LegacyRouteIssuanceLease, _IssuanceState], candidate)
                    )

            append_evidence_map(getattr(self, "_evidence", None))
            append_evidence_map(getattr(self, "_evidence_root", None))
            append_evidence_map(sealed_evidence_root)
            append_issuance_map(getattr(self, "_issuance", None))
            append_issuance_map(getattr(self, "_issuance_root", None))
            append_issuance_map(sealed_issuance_root)

            states: list[_IssuanceState] = []
            for issuance_map in issuance_maps:
                state = issuance_map.pop(issuance_lease, None)
                if state is not None and all(existing is not state for existing in states):
                    states.append(state)
            prepared_calls: list[PreparedLegacyCall] = []
            boundaries: list[
                tuple[
                    Connection,
                    _TransactionControlFence,
                    str,
                    Callable[..., None],
                    sqlite3.Connection,
                ]
            ] = []
            for state in states:
                candidates: list[tuple[object, object, object, object, object]] = [
                    (
                        state.connection,
                        state.transaction_control_fence,
                        state.transaction_guard_name,
                        state.transaction_control_listener,
                        state.dbapi_connection,
                    )
                ]
                if type(state.integrity_seal) is tuple and len(state.integrity_seal) == 18:
                    candidates.append(
                        (
                            state.integrity_seal[4],
                            state.integrity_seal[7],
                            state.integrity_seal[6],
                            state.integrity_seal[12],
                            state.integrity_seal[8],
                        )
                    )
                    sealed_prepared = state.integrity_seal[11]
                    if type(sealed_prepared) is PreparedLegacyCall:
                        prepared_calls.append(sealed_prepared)
                if type(state.prepared_call) is PreparedLegacyCall and all(
                    existing is not state.prepared_call for existing in prepared_calls
                ):
                    prepared_calls.append(state.prepared_call)
                for connection, fence, guard_name, listener, dbapi_connection in candidates:
                    if (
                        isinstance(connection, Connection)
                        and type(fence) is _TransactionControlFence
                        and type(guard_name) is str
                        and callable(listener)
                        and isinstance(dbapi_connection, sqlite3.Connection)
                        and all(
                            existing[0] is not connection or existing[3] is not listener
                            for existing in boundaries
                        )
                    ):
                        boundaries.append(
                            (
                                connection,
                                fence,
                                guard_name,
                                cast(Callable[..., None], listener),
                                dbapi_connection,
                            )
                        )
            evidence_entries: list[tuple[LockedLegacyRouteEvidence, _EvidenceState]] = []
            for evidence_map in evidence_maps:
                for evidence, evidence_state in tuple(evidence_map.items()):
                    sealed_lease = (
                        evidence_state.integrity_seal[7]
                        if type(evidence_state.integrity_seal) is tuple
                        and len(evidence_state.integrity_seal) == 10
                        else None
                    )
                    if (
                        evidence_state.issuance_lease is issuance_lease
                        or sealed_lease is issuance_lease
                    ):
                        evidence_map.pop(evidence, None)
                        if all(
                            existing_state is not evidence_state
                            for _existing_evidence, existing_state in evidence_entries
                        ):
                            evidence_entries.append((evidence, evidence_state))
            for _evidence, evidence_state in evidence_entries:
                for candidate in (
                    evidence_state.prepared_call,
                    (
                        evidence_state.integrity_seal[9]
                        if type(evidence_state.integrity_seal) is tuple
                        and len(evidence_state.integrity_seal) == 10
                        else None
                    ),
                ):
                    if type(candidate) is PreparedLegacyCall and all(
                        existing is not candidate for existing in prepared_calls
                    ):
                        prepared_calls.append(candidate)
            for connection, fence, guard_name, listener, dbapi_connection in boundaries:
                _close_transaction_control_boundary(
                    connection,
                    fence,
                    guard_name,
                    listener,
                    dbapi_connection,
                )
            for prepared_call in prepared_calls:
                preparation._revoke_prepared(prepared_call)
            preparation._revoke_lease(issuance_lease)


def build_legacy_pending_identity_verifier_port(
    *,
    backend: object,
    ledger_key: object,
) -> LegacyPendingIdentityVerifierPort:
    return LegacyPendingIdentityVerifierPort._configuration(
        backend=backend,
        ledger_key=ledger_key,
    )


class LegacyRouteProofIssuer(_SealedValue):
    __slots__ = (
        "_preparation_registry",
        "_proof_registry",
        "_registration_port",
        "_pending_identity_verifier_port",
        "_ordered_adapters",
        "_bundle_instance_token",
        "_catalog_instance_token",
        "_issuer_instance_token",
        "_integrity_seal",
    )
    _preparation_registry: LegacyPreparationRegistry
    _proof_registry: LegacyRouteProofRegistry
    _registration_port: LegacyRouteProofRegistrationPort
    _pending_identity_verifier_port: LegacyPendingIdentityVerifierPort
    _ordered_adapters: tuple[LegacyDeterministicAdapterSpec, ...]
    _bundle_instance_token: BundleInstanceToken
    _catalog_instance_token: object
    _issuer_instance_token: object
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        preparation_registry: LegacyPreparationRegistry,
        proof_registry: LegacyRouteProofRegistry,
        registration_port: LegacyRouteProofRegistrationPort,
        pending_identity_verifier_port: LegacyPendingIdentityVerifierPort,
        ordered_adapters: tuple[LegacyDeterministicAdapterSpec, ...],
        bundle_instance_token: BundleInstanceToken,
        catalog_instance_token: object,
        issuer_instance_token: object,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy proof issuers are factory-created")
        object.__setattr__(self, "_preparation_registry", preparation_registry)
        object.__setattr__(self, "_proof_registry", proof_registry)
        object.__setattr__(self, "_registration_port", registration_port)
        object.__setattr__(self, "_pending_identity_verifier_port", pending_identity_verifier_port)
        object.__setattr__(self, "_ordered_adapters", ordered_adapters)
        object.__setattr__(self, "_bundle_instance_token", bundle_instance_token)
        object.__setattr__(self, "_catalog_instance_token", catalog_instance_token)
        object.__setattr__(self, "_issuer_instance_token", issuer_instance_token)
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                preparation_registry,
                proof_registry,
                registration_port,
                pending_identity_verifier_port,
                ordered_adapters,
                bundle_instance_token,
                catalog_instance_token,
                issuer_instance_token,
            ),
        )

    def _require_integrity(self) -> None:
        try:
            current = (
                self._preparation_registry,
                self._proof_registry,
                self._registration_port,
                self._pending_identity_verifier_port,
                self._ordered_adapters,
                self._bundle_instance_token,
                self._catalog_instance_token,
                self._issuer_instance_token,
            )
            if not _same_identity_tuple(self._integrity_seal, current):
                raise ValueError("Legacy proof issuer integrity drift")
            self._pending_identity_verifier_port._require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof issuer integrity drift") from exc

    @property
    def preparation_registry(self) -> LegacyPreparationRegistry:
        self._require_integrity()
        return self._preparation_registry

    @property
    def proof_registry(self) -> LegacyRouteProofRegistry:
        self._require_integrity()
        return self._proof_registry

    @property
    def pending_identity_verifier_port(self) -> LegacyPendingIdentityVerifierPort:
        self._require_integrity()
        return self._pending_identity_verifier_port

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._require_integrity()
        return self._bundle_instance_token

    def _adapter_for(self, protocol_name: object) -> LegacyDeterministicAdapterSpec:
        if type(protocol_name) is not str:
            raise TypeError("Legacy persisted protocol name must be exact text")
        matches = tuple(
            adapter for adapter in self._ordered_adapters if adapter.name == protocol_name
        )
        if len(matches) != 1:
            raise ValueError("Legacy persisted protocol name is outside the exact Catalog")
        return matches[0]

    def prepare_server_loaded(
        self,
        read_session: Session,
        lookup_identity: LegacyConfirmationLookupIdentity,
        confirmation_input: LegacyApprovedConfirmationInput,
    ) -> PreparedLegacyCall:
        self._require_integrity()
        evidence = self._pending_identity_verifier_port.read_snapshot(
            read_session,
            lookup_identity,
            confirmation_input,
        )
        binding = None
        try:
            snapshot = self._pending_identity_verifier_port._consume_read_evidence(
                evidence,
                issuer_instance_token=self._issuer_instance_token,
            )
            adapter = self._adapter_for(snapshot["tool_name"])
            binding = self._preparation_registry._open_binding(adapter=adapter)
            persisted = _decode_legacy_arguments_object(snapshot["raw_args"])
            if not _same_json_value(snapshot["normalized_args"], persisted):
                raise ValueError("Legacy persisted normalized arguments mismatch")
            if confirmation_input.edited_args_present:
                edits = confirmation_input.edited_args
                if edits is None:
                    raise ValueError("Legacy explicit-null edited arguments are invalid")
            else:
                edits = None
            effective_args, _description = prepare_legacy_arguments(
                binding,
                cast(str, snapshot["raw_args"]),
                cast(Mapping[str, JSONValue] | None, edits),
                validate_unedited=True,
            )
            effective_object = _decode_legacy_arguments_object(effective_args)
            fingerprints = self._pending_identity_verifier_port._prepared_fingerprints(
                snapshot=snapshot,
                confirmation_input=confirmation_input,
                effective_args=effective_object,
            )
            metadata = {
                "conversation_id": snapshot["conversation_id"],
                "conversation_scope_revision": snapshot["conversation_scope_revision"],
                "pending_operation_id": snapshot["pending_operation_id"],
                "operation_id": snapshot["operation_id"],
                "tool_call_id": snapshot["tool_call_id"],
                "tool_name": snapshot["tool_name"],
                "fingerprint_key_id": snapshot["fingerprint_key_id"],
                "proposal_fingerprint": snapshot["proposal_fingerprint"],
                "confirmation_token_fingerprint": snapshot["confirmation_token_fingerprint"],
                "confirmation_token": confirmation_input.confirmation_token,
                "edited_args_present": confirmation_input.edited_args_present,
                "edited_args": confirmation_input.edited_args,
                **fingerprints,
            }
            return self._preparation_registry._prepare_call(
                binding=binding,
                raw_args=cast(str, snapshot["raw_args"]),
                effective_args=effective_args,
                metadata=metadata,
            )
        except BaseException:
            if binding is not None:
                self._preparation_registry._revoke_binding(binding)
            raise
        finally:
            self._pending_identity_verifier_port._revoke_evidence(evidence)

    def issue_after_claim(
        self,
        write_session: Session,
        issuance_lease: LegacyRouteIssuanceLease,
        claim_lease: LegacyClaimLease,
        prepared_call: PreparedLegacyCall,
    ) -> LegacyRouteProof:
        self._require_integrity()
        try:
            safe = self._pending_identity_verifier_port._proof_safe_snapshot(
                issuer_instance_token=self._issuer_instance_token,
                write_session=write_session,
                issuance_lease=issuance_lease,
                claim_lease=claim_lease,
                prepared_call=prepared_call,
            )
            return self._registration_port.issue(
                prepared_call=prepared_call,
                issuance_lease=issuance_lease,
                claim_lease=claim_lease,
                persisted_protocol_name=cast(str, safe["persisted_protocol_name"]),
                route_source="confirmation_resume",
                safe_metadata=safe,
            )
        except BaseException:
            if type(issuance_lease) is LegacyRouteIssuanceLease:
                issuance_lease.close()
            raise


class LegacyConfirmationRouteComponents(_SealedValue):
    __slots__ = (
        "_preparation_registry",
        "_proof_registry",
        "_proof_issuer",
        "_proof_consumer_port",
        "_pending_identity_verifier_port",
        "_catalog",
        "_bundle_instance_token",
        "_catalog_instance_token",
        "_integrity_seal",
    )
    _preparation_registry: LegacyPreparationRegistry
    _proof_registry: LegacyRouteProofRegistry
    _proof_issuer: LegacyRouteProofIssuer
    _proof_consumer_port: LegacyRouteProofConsumerPort
    _pending_identity_verifier_port: LegacyPendingIdentityVerifierPort
    _catalog: LegacyProofDeterministicCatalog
    _bundle_instance_token: BundleInstanceToken
    _catalog_instance_token: object
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        preparation_registry: LegacyPreparationRegistry,
        proof_registry: LegacyRouteProofRegistry,
        proof_issuer: LegacyRouteProofIssuer,
        proof_consumer_port: LegacyRouteProofConsumerPort,
        pending_identity_verifier_port: LegacyPendingIdentityVerifierPort,
        catalog: LegacyProofDeterministicCatalog,
        bundle_instance_token: BundleInstanceToken,
        catalog_instance_token: object,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy confirmation components are factory-created")
        values = (
            preparation_registry,
            proof_registry,
            proof_issuer,
            proof_consumer_port,
            pending_identity_verifier_port,
            catalog,
            bundle_instance_token,
            catalog_instance_token,
        )
        for slot, value in zip(self.__slots__[:-1], values):
            object.__setattr__(self, slot, value)
        object.__setattr__(self, "_integrity_seal", values)

    def _require_integrity(self) -> None:
        try:
            current = (
                self._preparation_registry,
                self._proof_registry,
                self._proof_issuer,
                self._proof_consumer_port,
                self._pending_identity_verifier_port,
                self._catalog,
                self._bundle_instance_token,
                self._catalog_instance_token,
            )
            if not _same_identity_tuple(self._integrity_seal, current):
                raise ValueError("Legacy confirmation component integrity drift")
            self._catalog._ensure_integrity()
            self._proof_issuer._require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy confirmation component integrity drift") from exc

    @property
    def preparation_registry(self) -> LegacyPreparationRegistry:
        self._require_integrity()
        return self._preparation_registry

    @property
    def proof_registry(self) -> LegacyRouteProofRegistry:
        self._require_integrity()
        return self._proof_registry

    @property
    def proof_issuer(self) -> LegacyRouteProofIssuer:
        self._require_integrity()
        return self._proof_issuer

    @property
    def proof_consumer_port(self) -> LegacyRouteProofConsumerPort:
        self._require_integrity()
        return self._proof_consumer_port

    @property
    def pending_identity_verifier_port(self) -> LegacyPendingIdentityVerifierPort:
        self._require_integrity()
        return self._pending_identity_verifier_port

    @property
    def catalog(self) -> LegacyProofDeterministicCatalog:
        self._require_integrity()
        return self._catalog

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._require_integrity()
        return self._bundle_instance_token

    @property
    def catalog_instance_token(self) -> object:
        self._require_integrity()
        return self._catalog_instance_token


def build_unpublished_legacy_confirmation_components(
    *,
    catalog: LegacyStaticAdapterCatalogV1,
    legacy_boundary: LegacyDeterministicBoundaryV1,
    runtime_container_token: object,
    pending_identity_verifier_port: LegacyPendingIdentityVerifierPort,
) -> LegacyConfirmationRouteComponents:
    if type(catalog) is not LegacyStaticAdapterCatalogV1:
        raise TypeError("Legacy confirmation catalog must be exact")
    if type(legacy_boundary) is not LegacyDeterministicBoundaryV1:
        raise TypeError("Legacy confirmation boundary must be exact")
    if type(runtime_container_token) is not object:
        raise TypeError("Legacy confirmation runtime container must be opaque")
    if type(pending_identity_verifier_port) is not LegacyPendingIdentityVerifierPort:
        raise TypeError("Legacy confirmation verifier Port must be exact")
    catalog.require_integrity()
    pending_identity_verifier_port._require_integrity(require_bound=False)
    adapters = catalog.ordered_adapters
    bindings = legacy_boundary.ordered_adapter_bindings
    if len(adapters) != len(bindings) or any(
        adapter.ordinal != binding.ordinal
        or adapter.name != binding.name
        or adapter.chained_policy != binding.chained_policy
        for adapter, binding in zip(adapters, bindings)
    ):
        raise ValueError("Legacy confirmation Catalog and boundary identity mismatch")
    verify_legacy_boundary(
        tuple(adapter.name for adapter in adapters),
        "forbidden",
        "legacy_deterministic",
    )
    bundle_token = legacy_boundary.bundle_instance_token
    if type(bundle_token) is not BundleInstanceToken:
        raise TypeError("Legacy confirmation Bundle instance token must be exact")
    lock = RLock()
    catalog_token = object()
    issuer_instance_token = object()
    preparation_registry = LegacyPreparationRegistry._create(
        lock=lock,
        runtime_container_token=runtime_container_token,
        ordered_adapters=adapters,
    )
    proof_registry = LegacyRouteProofRegistry._create(
        preparation_registry=preparation_registry,
        catalog_instance_token=catalog_token,
        bundle_instance_token=bundle_token,
        runtime_container_token=runtime_container_token,
        ordered_adapters=adapters,
        handle_factory=_create_legacy_proof_route_handle,
    )
    registration_port = proof_registry._create_registration_port()
    consumer_port = proof_registry._create_consumer_port()
    verifier = LegacyPendingIdentityVerifierPort._bind(
        pending_identity_verifier_port,
        lock=lock,
        preparation_registry=preparation_registry,
        proof_registry=proof_registry,
        issuer_instance_token=issuer_instance_token,
        runtime_container_token=runtime_container_token,
    )
    proof_catalog = LegacyProofDeterministicCatalog._create(
        proof_consumer_port=consumer_port,
        bundle_instance_token=bundle_token,
        catalog_instance_token=catalog_token,
    )
    issuer = LegacyRouteProofIssuer(
        _VALUE_SEAL,
        preparation_registry=preparation_registry,
        proof_registry=proof_registry,
        registration_port=registration_port,
        pending_identity_verifier_port=verifier,
        ordered_adapters=adapters,
        bundle_instance_token=bundle_token,
        catalog_instance_token=catalog_token,
        issuer_instance_token=issuer_instance_token,
    )
    components = LegacyConfirmationRouteComponents(
        _VALUE_SEAL,
        preparation_registry=preparation_registry,
        proof_registry=proof_registry,
        proof_issuer=issuer,
        proof_consumer_port=consumer_port,
        pending_identity_verifier_port=verifier,
        catalog=proof_catalog,
        bundle_instance_token=bundle_token,
        catalog_instance_token=catalog_token,
    )
    components._require_integrity()
    return components


__all__ = [
    "LegacyConfirmationRouteComponents",
    "LegacyPendingIdentityVerifierPort",
    "LegacyRouteProofIssuer",
    "build_legacy_pending_identity_verifier_port",
    "build_unpublished_legacy_confirmation_components",
]
