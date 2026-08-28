from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from threading import RLock
from typing import Literal, NoReturn, Protocol, cast
from uuid import UUID

from offerpilot.ai.tool_runtime.contracts import JSONValue, TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.metadata import FrozenJSONValue, canonical_json_bytes, freeze_json


_VALUE_SEAL = object()
_ROUTE_SOURCE = "confirmation_resume"
_RLOCK_TYPE = type(RLock())
_NO_EXECUTION_CONTEXT = object()
_PROOF_SAFE_METADATA_KEYS = frozenset(
    {
        "adapter_kind",
        "operation_role",
        "route_source",
        "operation_id",
        "tool_call_id",
        "persisted_protocol_name",
        "fingerprint_key_id",
        "conversation_id",
        "conversation_scope_revision",
        "claim_id",
        "claimed_at",
        "pending_identity_digest",
        "effective_input_fingerprint",
        "operation_request_fingerprint",
        "adapter_ordinal",
    }
)


class _LegacyAdapter(Protocol):
    @property
    def ordinal(self) -> int: ...

    @property
    def name(self) -> str: ...

    @property
    def editable_fields(self) -> tuple[Mapping[str, object], ...]: ...

    @property
    def describe(self) -> Callable[[str], str]: ...

    @property
    def validate(self) -> Callable[[str], str]: ...

    @property
    def presentation(self) -> object: ...

    @property
    def execute(self) -> Callable[..., str]: ...

    def require_integrity(self) -> None: ...


class _LegacyIssuanceProvenancePort(Protocol):
    def _require_issuance_transaction(
        self,
        issuance_lease: LegacyRouteIssuanceLease,
    ) -> object: ...

    def _require_execution_context(
        self,
        issuance_lease: LegacyRouteIssuanceLease,
        context: object,
    ) -> None: ...

    def _close_issuance(self, issuance_lease: LegacyRouteIssuanceLease) -> None: ...


def _canonical_uuid(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise TypeError(f"Legacy {label} must be an exact UUID string")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Legacy {label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"Legacy {label} must be a canonical UUID")
    return canonical


def _require_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if type(value) is not str:
        raise TypeError(f"Legacy {label} must be exact text")
    if not value or len(value) > maximum:
        raise ValueError(f"Legacy {label} is empty or too long")
    return value


def _freeze_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"Legacy {label} must be a JSON object")
    frozen = freeze_json(cast(Mapping[str, object], value))
    if not isinstance(frozen, Mapping):
        raise TypeError(f"Legacy {label} must be a JSON object")
    return cast(Mapping[str, object], frozen)


def _reject_argument_material(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {
                "confirmation_token",
                "edited_args",
                "raw_args",
                "effective_args",
                "encoded_args",
                "normalized_args",
                "arguments",
            }:
                raise ValueError(
                    "Legacy Proof Registry metadata cannot retain secrets or arguments"
                )
            _reject_argument_material(child)
    elif type(value) is tuple:
        for child in cast(tuple[object, ...], value):
            _reject_argument_material(child)


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
        raise AttributeError("Legacy proof values are sealed")


class LegacyApprovedConfirmationInput(_SealedValue):
    """Closed, deeply frozen input produced after HTTP confirmation validation."""

    __slots__ = (
        "_decision",
        "_operation_id",
        "_confirmation_token",
        "_edited_args_present",
        "_edited_args",
        "_rejection_feedback_present",
        "_rejection_feedback",
        "_integrity_seal",
    )
    _decision: str
    _operation_id: str | None
    _confirmation_token: str
    _edited_args_present: bool
    _edited_args: Mapping[str, object] | None
    _rejection_feedback_present: bool
    _rejection_feedback: str
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        *,
        decision: str,
        operation_id: str | None,
        confirmation_token: str,
        edited_args_present: bool,
        edited_args: object,
        rejection_feedback_present: bool,
        rejection_feedback: str,
    ) -> None:
        if type(decision) is not str or decision != "approved":
            raise ValueError("Legacy confirmation input must be approved")
        canonical_operation_id = _canonical_uuid(
            operation_id,
            "confirmation operation id",
            optional=True,
        )
        token = _require_text(confirmation_token, "confirmation token")
        if type(edited_args_present) is not bool:
            raise TypeError("Legacy edited-args state must be an exact boolean")
        if edited_args_present:
            if edited_args is None:
                raise ValueError("Legacy explicit-null edited args are invalid")
            frozen_edits: Mapping[str, object] | None = _freeze_mapping(
                edited_args,
                "edited args",
            )
        else:
            if edited_args is not None:
                raise ValueError("Legacy missing edited args cannot contain a value")
            frozen_edits = None
        if type(rejection_feedback_present) is not bool:
            raise TypeError("Legacy rejection feedback state must be an exact boolean")
        if rejection_feedback_present:
            raise ValueError("Approved Legacy confirmation cannot contain rejection feedback")
        if type(rejection_feedback) is not str or rejection_feedback:
            raise ValueError("Approved Legacy confirmation rejection feedback must be empty")
        object.__setattr__(self, "_decision", decision)
        object.__setattr__(self, "_operation_id", canonical_operation_id)
        object.__setattr__(self, "_confirmation_token", token)
        object.__setattr__(self, "_edited_args_present", edited_args_present)
        object.__setattr__(self, "_edited_args", frozen_edits)
        object.__setattr__(self, "_rejection_feedback_present", False)
        object.__setattr__(self, "_rejection_feedback", "")
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                decision,
                canonical_operation_id,
                token,
                edited_args_present,
                frozen_edits,
            ),
        )

    def _require_integrity(self) -> None:
        try:
            current = (
                self._decision,
                self._operation_id,
                self._confirmation_token,
                self._edited_args_present,
                self._edited_args,
            )
            if not _same_identity_tuple(self._integrity_seal, current):
                raise ValueError("Legacy confirmation input integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy confirmation input integrity drift") from exc

    @property
    def decision(self) -> Literal["approved"]:
        self._require_integrity()
        return "approved"

    @property
    def operation_id(self) -> str | None:
        self._require_integrity()
        return self._operation_id

    @property
    def confirmation_token(self) -> str:
        self._require_integrity()
        return self._confirmation_token

    @property
    def edited_args_present(self) -> bool:
        self._require_integrity()
        return self._edited_args_present

    @property
    def edited_args(self) -> Mapping[str, object] | None:
        self._require_integrity()
        return self._edited_args

    @property
    def rejection_feedback_present(self) -> Literal[False]:
        self._require_integrity()
        return False

    @property
    def rejection_feedback(self) -> Literal[""]:
        self._require_integrity()
        return ""


class LegacyConfirmationLookupIdentity(_SealedValue):
    __slots__ = ("_conversation_id", "_integrity_seal")
    _conversation_id: int
    _integrity_seal: tuple[object, ...]

    def __init__(self, *, conversation_id: int) -> None:
        if type(conversation_id) is not int or not 0 < conversation_id <= 2**63 - 1:
            raise ValueError("Legacy conversation id must be a positive int64")
        object.__setattr__(self, "_conversation_id", conversation_id)
        object.__setattr__(self, "_integrity_seal", (conversation_id,))

    def _require_integrity(self) -> None:
        try:
            if (
                type(self._conversation_id) is not int
                or not 0 < self._conversation_id <= 2**63 - 1
                or not _same_identity_tuple(
                    self._integrity_seal,
                    (self._conversation_id,),
                )
            ):
                raise ValueError("Legacy lookup identity integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy lookup identity integrity drift") from exc

    @property
    def conversation_id(self) -> int:
        self._require_integrity()
        return self._conversation_id


class _RegistryOpaqueValue(_SealedValue):
    __slots__ = ("_registry", "_identity", "_integrity_seal")
    _registry: object
    _identity: object
    _integrity_seal: tuple[object, object]

    def __init__(self, seal: object | None = None, *, registry: object) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy proof identity values are factory-created")
        identity = object()
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_integrity_seal", (registry, identity))

    def _require_registry(self, registry: object) -> object:
        try:
            if (
                self._registry is not registry
                or type(self._integrity_seal) is not tuple
                or len(self._integrity_seal) != 2
                or self._integrity_seal[0] is not registry
                or self._integrity_seal[1] is not self._identity
            ):
                raise ValueError(
                    "Legacy proof value has the wrong Catalog/Bundle Registry identity"
                )
            return self._identity
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "Legacy proof value has the wrong Catalog/Bundle Registry identity"
            ) from exc


class LegacyPreparationBinding(_RegistryOpaqueValue):
    __slots__ = ()

    @property
    def editable_fields(self) -> tuple[Mapping[str, object], ...]:
        registry = cast(LegacyPreparationRegistry, self._registry)
        return registry._binding_editable_fields(self)

    def describe(self, encoded_args: str) -> str:
        registry = cast(LegacyPreparationRegistry, self._registry)
        return registry._binding_describe(self, encoded_args)

    def validate(self, encoded_args: str) -> str:
        registry = cast(LegacyPreparationRegistry, self._registry)
        return registry._binding_validate(self, encoded_args)

    @property
    def presentation(self) -> object:
        registry = cast(LegacyPreparationRegistry, self._registry)
        return registry._binding_presentation(self)


class PreparedLegacyCall(_RegistryOpaqueValue):
    __slots__ = ()

    def close(self) -> None:
        registry = cast(LegacyPreparationRegistry, self._registry)
        registry._revoke_prepared(self)

    def __enter__(self) -> PreparedLegacyCall:
        registry = cast(LegacyPreparationRegistry, self._registry)
        registry._require_live_prepared(self)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False


class PreparedLegacyInputV1(_SealedValue):
    """Factory-only exact effective input projected from one PreparedCall."""

    __slots__ = (
        "_canonical_args",
        "_encoded_args",
        "_confirmation_human",
        "_integrity_seal",
    )
    _canonical_args: Mapping[str, JSONValue]
    _encoded_args: str
    _confirmation_human: str
    _integrity_seal: tuple[str, str]

    def __init__(
        self,
        seal: object | None = None,
        *,
        canonical_args: Mapping[str, JSONValue],
        encoded_args: str,
        confirmation_human: str,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy prepared inputs are Port-created")
        frozen = _freeze_mapping(canonical_args, "prepared canonical arguments")
        encoded = _require_text(encoded_args, "prepared encoded arguments", maximum=131072)
        canonical_encoded = canonical_json_bytes(cast(FrozenJSONValue, frozen)).decode("utf-8")
        if encoded != canonical_encoded:
            raise ValueError("Legacy prepared argument representations drifted")
        human = _require_text(confirmation_human, "prepared confirmation human")
        object.__setattr__(
            self,
            "_canonical_args",
            cast(Mapping[str, JSONValue], frozen),
        )
        object.__setattr__(self, "_encoded_args", encoded)
        object.__setattr__(self, "_confirmation_human", human)
        object.__setattr__(self, "_integrity_seal", (encoded, human))

    def _require_integrity(self) -> None:
        try:
            canonical_encoded = canonical_json_bytes(
                cast(FrozenJSONValue, self._canonical_args)
            ).decode("utf-8")
            if canonical_encoded != self._encoded_args or self._integrity_seal != (
                self._encoded_args,
                self._confirmation_human,
            ):
                raise ValueError("Legacy prepared input identity drift")
            _require_text(self._confirmation_human, "prepared confirmation human")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy prepared input identity drift") from exc

    @property
    def canonical_args(self) -> Mapping[str, JSONValue]:
        self._require_integrity()
        return self._canonical_args

    @property
    def encoded_args(self) -> str:
        self._require_integrity()
        return self._encoded_args

    @property
    def confirmation_human(self) -> str:
        self._require_integrity()
        return self._confirmation_human


class LegacyPreparedInputPort(_SealedValue):
    """Read-only projection Port bound to one exact Preparation Registry."""

    __slots__ = ("_registry", "_integrity_seal")
    _registry: LegacyPreparationRegistry
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        registry: LegacyPreparationRegistry,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy prepared-input Ports are factory-created")
        if type(registry) is not LegacyPreparationRegistry:
            raise TypeError("Legacy prepared-input Port requires the exact Registry")
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_integrity_seal", (registry,))

    @classmethod
    def _create(cls, registry: LegacyPreparationRegistry) -> LegacyPreparedInputPort:
        return cls(_VALUE_SEAL, registry=registry)

    def _require_integrity(self) -> None:
        try:
            if not _same_identity_tuple(self._integrity_seal, (self._registry,)):
                raise ValueError("Legacy prepared-input Port identity drift")
            self._registry._require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy prepared-input Port identity drift") from exc

    def require(
        self,
        prepared: PreparedLegacyCall,
        *,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> PreparedLegacyInputV1:
        self._require_integrity()
        inspected = False
        try:
            adapter, _raw_args, effective_args, metadata, _binding, _identity = (
                self._registry._inspect_prepared(prepared)
            )
            inspected = True
            expected_operation = _canonical_uuid(operation_id, "prepared operation id")
            expected_call = _require_text(tool_call_id, "prepared tool-call id")
            expected_name = _require_text(tool_name, "prepared tool name")
            if (
                metadata.get("operation_id") != expected_operation
                or metadata.get("tool_call_id") != expected_call
                or metadata.get("tool_name") != expected_name
                or adapter.name != expected_name
            ):
                raise ValueError("Legacy prepared operation/tool identity mismatch")
            try:
                decoded = json.loads(effective_args)
            except json.JSONDecodeError as exc:
                raise ValueError("Legacy prepared arguments are not valid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("Legacy prepared arguments must be a JSON object")
            adapter.require_integrity()
            human = adapter.describe(effective_args)
            if type(human) is not str:
                raise TypeError("Legacy prepared confirmation human must be exact text")
            return PreparedLegacyInputV1(
                _VALUE_SEAL,
                canonical_args=cast(Mapping[str, JSONValue], decoded),
                encoded_args=effective_args,
                confirmation_human=human,
            )
        except BaseException:
            if inspected:
                self._registry._revoke_prepared(prepared)
            raise


class LegacyRouteProof(_RegistryOpaqueValue):
    __slots__ = ()


class LegacyRouteIssuanceLease(_SealedValue):
    """Transaction-scoped cleanup fence shared by both proof Registries."""

    __slots__ = (
        "_lock",
        "_session_token",
        "_runtime_container_token",
        "_provenance_port",
        "_status",
        "_cleanup",
        "_cleanup_roots",
        "_integrity_seal",
    )
    _lock: RLock
    _session_token: object
    _runtime_container_token: object
    _provenance_port: _LegacyIssuanceProvenancePort
    _status: Literal["open", "closing", "closed"]
    _cleanup: list[tuple[object, Callable[[], None]]]
    _cleanup_roots: tuple[tuple[object, Callable[[], None]], ...]
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        lock: RLock,
        session_token: object,
        runtime_container_token: object,
        provenance_port: object,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy issuance leases are verifier-created")
        if type(lock) is not _RLOCK_TYPE:
            raise TypeError("Legacy issuance lease requires the shared exact RLock")
        if not callable(getattr(provenance_port, "_require_issuance_transaction", None)):
            raise TypeError("Legacy issuance lease requires transaction provenance")
        if not callable(getattr(provenance_port, "_require_execution_context", None)):
            raise TypeError("Legacy issuance lease requires execution-context provenance")
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_session_token", session_token)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        typed_provenance_port = cast(_LegacyIssuanceProvenancePort, provenance_port)
        object.__setattr__(self, "_provenance_port", typed_provenance_port)
        object.__setattr__(self, "_status", "open")
        object.__setattr__(self, "_cleanup", [])
        object.__setattr__(self, "_cleanup_roots", ())
        self._seal()

    def _seal(self) -> None:
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                self,
                self._lock,
                self._session_token,
                self._runtime_container_token,
                self._provenance_port,
                self._status,
                self._cleanup,
                self._cleanup_roots,
            ),
        )

    @staticmethod
    def _same_cleanup_roots(
        left: object,
        right: tuple[tuple[object, Callable[[], None]], ...],
    ) -> bool:
        return (
            type(left) is tuple
            and len(left) == len(right)
            and all(
                type(expected) is tuple
                and len(expected) == 2
                and expected[0] is actual[0]
                and expected[1] is actual[1]
                for expected, actual in zip(left, right)
            )
        )

    def _require_integrity(self) -> None:
        try:
            roots = tuple(self._cleanup)
            if (
                type(self._cleanup) is not list
                or not self._same_cleanup_roots(self._cleanup_roots, roots)
                or not _same_identity_tuple(
                    self._integrity_seal,
                    (
                        self,
                        self._lock,
                        self._session_token,
                        self._runtime_container_token,
                        self._provenance_port,
                        self._status,
                        self._cleanup,
                        self._cleanup_roots,
                    ),
                )
            ):
                raise ValueError("Legacy issuance lease integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy issuance lease integrity drift") from exc

    @classmethod
    def _create(
        cls,
        *,
        lock: RLock,
        session_token: object,
        runtime_container_token: object,
        provenance_port: object,
    ) -> LegacyRouteIssuanceLease:
        return cls(
            _VALUE_SEAL,
            lock=lock,
            session_token=session_token,
            runtime_container_token=runtime_container_token,
            provenance_port=provenance_port,
        )

    def _require_live(
        self,
        *,
        lock: RLock,
        runtime_container_token: object,
        session_token: object | None = None,
        execution_context: object = _NO_EXECUTION_CONTEXT,
    ) -> None:
        try:
            self._require_integrity()
            provenance_port = self._provenance_port
            if (
                self._lock is not lock
                or self._runtime_container_token is not runtime_container_token
                or self._status != "open"
                or (session_token is not None and self._session_token is not session_token)
                or not callable(getattr(provenance_port, "_require_issuance_transaction", None))
                or not callable(getattr(provenance_port, "_require_execution_context", None))
            ):
                raise ValueError("Legacy issuance lease is closed or has the wrong Session")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy issuance lease is closed or has the wrong Session") from exc
        try:
            provenance_port._require_issuance_transaction(self)
            if execution_context is not _NO_EXECUTION_CONTEXT:
                provenance_port._require_execution_context(self, execution_context)
        except BaseException:
            self.close()
            raise

    def _attach_cleanup(
        self,
        *,
        lock: RLock,
        runtime_container_token: object,
        owner: object,
        callback: Callable[[], None],
    ) -> None:
        with lock:
            self._require_live(
                lock=lock,
                runtime_container_token=runtime_container_token,
            )
            callbacks = self._cleanup
            if all(existing is not owner for existing, _callback in callbacks):
                callbacks.append((owner, callback))
                object.__setattr__(self, "_cleanup_roots", tuple(callbacks))
                self._seal()

    def _run_bound_projection(self, callback: Callable[[], object]) -> None:
        self._require_integrity()
        if not callable(callback):
            raise TypeError("Legacy bound projection requires an exact callable")
        provenance_port = self._provenance_port
        run_bound = getattr(provenance_port, "_run_bound_projection", None)
        if not callable(run_bound):
            raise ValueError("Legacy bound projection provenance is unavailable")
        run_bound(self, callback)

    def _run_verified_executor(self, callback: Callable[[], str]) -> str:
        self._require_integrity()
        if not callable(callback):
            raise TypeError("Legacy executor requires an exact callable")
        provenance_port = self._provenance_port
        run_executor = getattr(provenance_port, "_run_verified_executor", None)
        if not callable(run_executor):
            raise ValueError("Legacy executor transaction provenance is unavailable")
        return cast(str, run_executor(self, callback))

    def close(self, *, outcome: str | None = None) -> None:
        del outcome
        seal = getattr(self, "_integrity_seal", ())
        if type(seal) is not tuple or len(seal) != 8 or seal[0] is not self:
            try:
                object.__setattr__(self, "_status", "closed")
            except BaseException:
                pass
            return
        lock = seal[1]
        session_token = seal[2]
        runtime_container_token = seal[3]
        provenance = seal[4]
        sealed_status = seal[5]
        sealed_cleanup = seal[6]
        sealed_roots = seal[7]
        if (
            type(lock) is not _RLOCK_TYPE
            or type(session_token) is not object
            or type(runtime_container_token) is not object
            or not callable(getattr(provenance, "_close_issuance", None))
            or type(sealed_status) is not str
            or sealed_status not in ("open", "closing", "closed")
            or type(sealed_cleanup) is not list
            or type(sealed_roots) is not tuple
            or any(
                type(candidate) is not tuple or len(candidate) != 2 or not callable(candidate[1])
                for candidate in sealed_roots
            )
            or len({id(candidate[0]) for candidate in sealed_roots}) != len(sealed_roots)
        ):
            try:
                object.__setattr__(self, "_status", "closed")
            except BaseException:
                pass
            return
        try:
            with lock:
                if sealed_status == "closed":
                    return
                roots = cast(tuple[tuple[object, Callable[[], None]], ...], sealed_roots)
                object.__setattr__(self, "_lock", lock)
                object.__setattr__(self, "_session_token", session_token)
                object.__setattr__(self, "_runtime_container_token", runtime_container_token)
                object.__setattr__(self, "_provenance_port", provenance)
                object.__setattr__(self, "_status", "closing")
                object.__setattr__(self, "_cleanup", list(roots))
                object.__setattr__(self, "_cleanup_roots", roots)
                self._seal()
                try:
                    cast(_LegacyIssuanceProvenancePort, provenance)._close_issuance(self)
                except BaseException:
                    pass
                for _owner, callback in roots:
                    try:
                        callback()
                    except BaseException:
                        pass
                object.__setattr__(self, "_cleanup", [])
                object.__setattr__(self, "_cleanup_roots", ())
                object.__setattr__(self, "_status", "closed")
                self._seal()
        except BaseException:
            try:
                object.__setattr__(self, "_status", "closed")
            except BaseException:
                pass

    def __enter__(self) -> LegacyRouteIssuanceLease:
        with self._lock:
            self._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False


class LegacyClaimLease(_SealedValue):
    __slots__ = (
        "_issuance_lease",
        "_operation_id",
        "_claim_id",
        "_claimed_at",
        "_identity",
        "_integrity_seal",
    )
    _issuance_lease: LegacyRouteIssuanceLease
    _operation_id: str
    _claim_id: str
    _claimed_at: datetime
    _identity: object
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        issuance_lease: LegacyRouteIssuanceLease,
        operation_id: str,
        claim_id: str,
        claimed_at: datetime,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy claim leases are verifier-created")
        canonical_operation = _canonical_uuid(operation_id, "claim operation id")
        canonical_claim = _require_text(claim_id, "claim id")
        if type(claimed_at) is not datetime:
            raise TypeError("Legacy claimed-at must be an exact datetime")
        identity = object()
        object.__setattr__(self, "_issuance_lease", issuance_lease)
        object.__setattr__(self, "_operation_id", canonical_operation)
        object.__setattr__(self, "_claim_id", canonical_claim)
        object.__setattr__(self, "_claimed_at", claimed_at)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(
            self,
            "_integrity_seal",
            (issuance_lease, canonical_operation, canonical_claim, claimed_at, identity),
        )

    @classmethod
    def _create(
        cls,
        *,
        issuance_lease: LegacyRouteIssuanceLease,
        operation_id: str,
        claim_id: str,
        claimed_at: datetime,
    ) -> LegacyClaimLease:
        return cls(
            _VALUE_SEAL,
            issuance_lease=issuance_lease,
            operation_id=operation_id,
            claim_id=claim_id,
            claimed_at=claimed_at,
        )

    def _snapshot(
        self,
        *,
        issuance_lease: LegacyRouteIssuanceLease,
    ) -> tuple[object, str, str, datetime]:
        try:
            if self._issuance_lease is not issuance_lease or not _same_identity_tuple(
                self._integrity_seal,
                (
                    self._issuance_lease,
                    self._operation_id,
                    self._claim_id,
                    self._claimed_at,
                    self._identity,
                ),
            ):
                raise ValueError("Legacy claim lease identity drift")
            return (
                self._identity,
                self._operation_id,
                self._claim_id,
                self._claimed_at,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy claim lease identity drift") from exc


@dataclass(slots=True, eq=False)
class _PreparationEntry:
    binding: LegacyPreparationBinding
    binding_identity: object
    adapter: _LegacyAdapter
    status: Literal[
        "preparation_open",
        "prepared",
        "consumed",
        "executing",
        "executor_returned",
        "cleared",
        "revoked",
    ]
    raw_args: str | None = None
    effective_args: str | None = None
    prepared_call: PreparedLegacyCall | None = None
    prepared_identity: object | None = None
    metadata: Mapping[str, object] | None = None
    issuance_lease: LegacyRouteIssuanceLease | None = None
    integrity_seal: tuple[object, ...] = ()


class LegacyPreparationRegistry(TransientToolRuntimeValue):
    """The sole long-lived owner of raw/effective Legacy argument strings."""

    __slots__ = (
        "_lock",
        "_runtime_container_token",
        "_ordered_adapters",
        "_entries",
        "_prepared",
        "_integrity_seal",
    )
    _lock: RLock
    _runtime_container_token: object
    _ordered_adapters: tuple[_LegacyAdapter, ...]
    _entries: dict[LegacyPreparationBinding, _PreparationEntry]
    _prepared: dict[PreparedLegacyCall, _PreparationEntry]
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        lock: RLock,
        runtime_container_token: object,
        ordered_adapters: Sequence[_LegacyAdapter],
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy Preparation Registry is factory-created")
        ordered = tuple(ordered_adapters)
        if type(lock) is not _RLOCK_TYPE or not ordered:
            raise TypeError("Legacy Preparation Registry requires shared lock and adapters")
        for adapter in ordered:
            adapter.require_integrity()
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        object.__setattr__(self, "_ordered_adapters", ordered)
        entries: dict[LegacyPreparationBinding, _PreparationEntry] = {}
        prepared: dict[PreparedLegacyCall, _PreparationEntry] = {}
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_prepared", prepared)
        object.__setattr__(
            self,
            "_integrity_seal",
            (lock, runtime_container_token, ordered, entries, prepared),
        )

    @classmethod
    def _create(
        cls,
        *,
        lock: RLock,
        runtime_container_token: object,
        ordered_adapters: Sequence[_LegacyAdapter],
    ) -> LegacyPreparationRegistry:
        return cls(
            _VALUE_SEAL,
            lock=lock,
            runtime_container_token=runtime_container_token,
            ordered_adapters=ordered_adapters,
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy Preparation Registry is sealed")

    @property
    def shared_lock(self) -> RLock:
        self._require_integrity()
        return self._lock

    @property
    def runtime_container_token(self) -> object:
        self._require_integrity()
        return self._runtime_container_token

    def _require_integrity(self) -> None:
        try:
            if not _same_identity_tuple(
                self._integrity_seal,
                (
                    self._lock,
                    self._runtime_container_token,
                    self._ordered_adapters,
                    self._entries,
                    self._prepared,
                ),
            ):
                raise ValueError("Legacy Preparation Registry integrity drift")
            for adapter in self._ordered_adapters:
                adapter.require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy Preparation Registry integrity drift") from exc

    def _cleanup_roots(
        self,
    ) -> tuple[
        list[dict[LegacyPreparationBinding, _PreparationEntry]],
        list[dict[PreparedLegacyCall, _PreparationEntry]],
    ]:
        seal = getattr(self, "_integrity_seal", ())
        entry_maps: list[dict[LegacyPreparationBinding, _PreparationEntry]] = []
        prepared_maps: list[dict[PreparedLegacyCall, _PreparationEntry]] = []
        for candidate in (
            self._entries,
            seal[3] if type(seal) is tuple and len(seal) == 5 else None,
        ):
            if isinstance(candidate, dict) and all(
                existing is not candidate for existing in entry_maps
            ):
                entry_maps.append(
                    cast(dict[LegacyPreparationBinding, _PreparationEntry], candidate)
                )
        for candidate in (
            self._prepared,
            seal[4] if type(seal) is tuple and len(seal) == 5 else None,
        ):
            if isinstance(candidate, dict) and all(
                existing is not candidate for existing in prepared_maps
            ):
                prepared_maps.append(cast(dict[PreparedLegacyCall, _PreparationEntry], candidate))
        return entry_maps, prepared_maps

    def _cleanup_lock(self) -> RLock | None:
        seal = getattr(self, "_integrity_seal", ())
        sealed_lock = seal[0] if type(seal) is tuple and len(seal) == 5 else None
        if type(sealed_lock) is _RLOCK_TYPE:
            return sealed_lock
        if type(self._lock) is _RLOCK_TYPE:
            return self._lock
        return None

    @staticmethod
    def _entry_identity(entry: _PreparationEntry) -> tuple[object, ...]:
        return (
            entry,
            entry.binding,
            entry.binding_identity,
            entry.adapter,
            entry.status,
            entry.raw_args,
            entry.effective_args,
            entry.prepared_call,
            entry.prepared_identity,
            entry.metadata,
            entry.issuance_lease,
        )

    def _seal_entry(self, entry: _PreparationEntry) -> None:
        entry.integrity_seal = self._entry_identity(entry)

    def _require_entry_integrity(self, entry: _PreparationEntry) -> None:
        try:
            if type(entry) is not _PreparationEntry or not _same_identity_tuple(
                entry.integrity_seal,
                self._entry_identity(entry),
            ):
                raise ValueError("Legacy preparation entry integrity drift")
            if (
                entry.binding._require_registry(self) is not entry.binding_identity
                or all(candidate is not entry.adapter for candidate in self._ordered_adapters)
                or self._entries.get(entry.binding) is not entry
            ):
                raise ValueError("Legacy preparation entry identity drift")
            entry.adapter.require_integrity()
            if entry.status == "preparation_open":
                if any(
                    value is not None
                    for value in (
                        entry.raw_args,
                        entry.effective_args,
                        entry.prepared_call,
                        entry.prepared_identity,
                        entry.metadata,
                        entry.issuance_lease,
                    )
                ) or any(candidate is entry for candidate in self._prepared.values()):
                    raise ValueError("Legacy open preparation entry shape drift")
                return
            prepared_call = entry.prepared_call
            if (
                type(prepared_call) is not PreparedLegacyCall
                or prepared_call._require_registry(self) is not entry.prepared_identity
                or self._prepared.get(prepared_call) is not entry
            ):
                raise ValueError("Legacy prepared entry Registry identity drift")
            if entry.status == "prepared":
                if (
                    type(entry.raw_args) is not str
                    or type(entry.effective_args) is not str
                    or not isinstance(entry.metadata, Mapping)
                    or entry.issuance_lease is not None
                ):
                    raise ValueError("Legacy prepared entry shape drift")
            elif entry.status in {"consumed", "executing", "executor_returned"}:
                if (
                    entry.raw_args is not None
                    or type(entry.effective_args) is not str
                    or entry.metadata is not None
                    or type(entry.issuance_lease) is not LegacyRouteIssuanceLease
                ):
                    raise ValueError("Legacy consumed preparation entry shape drift")
            elif entry.status == "cleared":
                if (
                    entry.raw_args is not None
                    or entry.effective_args is not None
                    or entry.metadata is not None
                    or type(entry.issuance_lease) is not LegacyRouteIssuanceLease
                ):
                    raise ValueError("Legacy cleared preparation entry shape drift")
            else:
                raise ValueError("Legacy preparation entry state drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy preparation entry integrity drift") from exc

    def _revoke_entry(self, entry: _PreparationEntry) -> None:
        entry_maps, prepared_maps = self._cleanup_roots()
        for prepared_map in prepared_maps:
            for prepared, candidate in tuple(prepared_map.items()):
                if candidate is entry:
                    prepared_map.pop(prepared, None)
        for entry_map in entry_maps:
            for binding, candidate in tuple(entry_map.items()):
                if candidate is entry:
                    entry_map.pop(binding, None)
        entry.raw_args = None
        entry.effective_args = None
        entry.metadata = None
        entry.status = "revoked"
        entry.prepared_call = None
        entry.prepared_identity = None
        entry.issuance_lease = None
        self._seal_entry(entry)

    def _require_adapter(self, adapter: object) -> _LegacyAdapter:
        if all(candidate is not adapter for candidate in self._ordered_adapters):
            raise ValueError("Legacy Adapter is outside the exact sealed Catalog")
        exact_adapter = cast(_LegacyAdapter, adapter)
        exact_adapter.require_integrity()
        return exact_adapter

    def _open_binding(self, *, adapter: object) -> LegacyPreparationBinding:
        with self._lock:
            self._require_integrity()
            exact_adapter = self._require_adapter(adapter)
            binding = LegacyPreparationBinding(_VALUE_SEAL, registry=self)
            identity = binding._require_registry(self)
            entry = _PreparationEntry(
                binding=binding,
                binding_identity=identity,
                adapter=exact_adapter,
                status="preparation_open",
            )
            self._entries[binding] = entry
            self._seal_entry(entry)
            return binding

    def _binding_entry(self, binding: object) -> _PreparationEntry:
        self._require_integrity()
        if type(binding) is not LegacyPreparationBinding:
            raise TypeError("Legacy preparation requires an exact Binding")
        binding._require_registry(self)
        entry = self._entries.get(binding)
        if entry is None or entry.binding is not binding:
            raise ValueError("Legacy Preparation Binding has no Registry authority")
        self._require_entry_integrity(entry)
        return entry

    def _live_binding_entry(self, binding: object) -> _PreparationEntry:
        entry = self._binding_entry(binding)
        if entry.status != "preparation_open":
            raise ValueError("Legacy Preparation Binding capability is no longer live")
        return entry

    def _binding_editable_fields(
        self,
        binding: LegacyPreparationBinding,
    ) -> tuple[Mapping[str, object], ...]:
        with self._lock:
            return self._live_binding_entry(binding).adapter.editable_fields

    def _binding_describe(self, binding: LegacyPreparationBinding, encoded_args: str) -> str:
        if type(encoded_args) is not str:
            raise TypeError("Legacy encoded arguments must be exact text")
        with self._lock:
            describe = self._live_binding_entry(binding).adapter.describe
            return describe(encoded_args)

    def _binding_validate(self, binding: LegacyPreparationBinding, encoded_args: str) -> str:
        if type(encoded_args) is not str:
            raise TypeError("Legacy encoded arguments must be exact text")
        with self._lock:
            validate = self._live_binding_entry(binding).adapter.validate
            return validate(encoded_args)

    def _binding_presentation(self, binding: LegacyPreparationBinding) -> object:
        with self._lock:
            return self._live_binding_entry(binding).adapter.presentation

    def _revoke_binding(self, binding: object) -> None:
        lock = self._cleanup_lock()
        if lock is None:
            return
        with lock:
            try:
                if type(binding) is not LegacyPreparationBinding:
                    return
                binding._require_registry(self)
            except (TypeError, ValueError):
                return
            entry_maps, _prepared_maps = self._cleanup_roots()
            entry = next(
                (entry_map[binding] for entry_map in entry_maps if binding in entry_map),
                None,
            )
            if entry is not None:
                self._revoke_entry(entry)

    def _prepare_call(
        self,
        *,
        binding: LegacyPreparationBinding,
        raw_args: str,
        effective_args: str,
        metadata: Mapping[str, object],
    ) -> PreparedLegacyCall:
        if type(raw_args) is not str or type(effective_args) is not str:
            raise TypeError("Legacy raw/effective arguments must be exact encoded text")
        frozen_metadata = _freeze_mapping(metadata, "preparation metadata")
        with self._lock:
            self._require_integrity()
            entry = self._binding_entry(binding)
            if entry.status != "preparation_open":
                raise ValueError("Legacy preparation attempt is already prepared or revoked")
            prepared = PreparedLegacyCall(_VALUE_SEAL, registry=self)
            prepared_identity = prepared._require_registry(self)
            entry.raw_args = raw_args
            entry.effective_args = effective_args
            entry.prepared_call = prepared
            entry.prepared_identity = prepared_identity
            entry.metadata = frozen_metadata
            entry.status = "prepared"
            self._prepared[prepared] = entry
            self._seal_entry(entry)
            return prepared

    def _prepared_entry(self, prepared_call: object) -> _PreparationEntry:
        self._require_integrity()
        if type(prepared_call) is not PreparedLegacyCall:
            raise TypeError("Legacy proof requires an exact PreparedCall")
        prepared_call._require_registry(self)
        entry = self._prepared.get(prepared_call)
        if entry is None or entry.prepared_call is not prepared_call:
            raise ValueError("Legacy PreparedCall has no Registry authority")
        self._require_entry_integrity(entry)
        return entry

    def _inspect_prepared(
        self,
        prepared_call: PreparedLegacyCall,
    ) -> tuple[_LegacyAdapter, str, str, Mapping[str, object], object, object]:
        with self._lock:
            self._require_integrity()
            entry = self._prepared_entry(prepared_call)
            if entry.status != "prepared":
                raise ValueError("Legacy PreparedCall is consumed or revoked")
            if (
                entry.raw_args is None
                or entry.effective_args is None
                or entry.metadata is None
                or entry.prepared_identity is None
            ):
                raise ValueError("Legacy PreparedCall identity is incomplete")
            return (
                entry.adapter,
                entry.raw_args,
                entry.effective_args,
                entry.metadata,
                entry.binding_identity,
                entry.prepared_identity,
            )

    def _require_live_prepared(self, prepared_call: PreparedLegacyCall) -> None:
        with self._lock:
            self._require_integrity()
            entry = self._prepared_entry(prepared_call)
            if entry.status != "prepared":
                raise ValueError("Legacy preparation attempt is consumed, closed, or revoked")

    def _consume_for_proof(
        self,
        *,
        prepared_call: PreparedLegacyCall,
        adapter: object,
        issuance_lease: LegacyRouteIssuanceLease,
    ) -> tuple[object, object]:
        with self._lock:
            self._require_integrity()
            issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
            entry = self._prepared_entry(prepared_call)
            if entry.status != "prepared":
                raise ValueError("Legacy PreparedCall is consumed or revoked")
            if entry.adapter is not adapter:
                raise ValueError("Legacy PreparedCall Adapter identity mismatch")
            if entry.prepared_identity is None or entry.metadata is None:
                raise ValueError("Legacy PreparedCall identity is incomplete")
            entry.status = "consumed"
            entry.issuance_lease = issuance_lease
            entry.raw_args = None
            entry.metadata = None
            self._seal_entry(entry)
            issuance_lease._attach_cleanup(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
                owner=self,
                callback=lambda: self._revoke_lease(issuance_lease),
            )
            return entry.binding_identity, entry.prepared_identity

    def _borrow_for_execute(
        self,
        *,
        prepared_identity: object,
        adapter: object,
        issuance_lease: LegacyRouteIssuanceLease,
    ) -> str:
        with self._lock:
            self._require_integrity()
            issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
            matches = [
                entry
                for entry in self._prepared.values()
                if entry.prepared_identity is prepared_identity
            ]
            if len(matches) != 1:
                raise ValueError("Legacy prepared execution identity is missing or ambiguous")
            entry = matches[0]
            self._require_entry_integrity(entry)
            if (
                entry.status != "consumed"
                or entry.adapter is not adapter
                or entry.issuance_lease is not issuance_lease
                or entry.effective_args is None
            ):
                raise ValueError("Legacy prepared arguments are cleared or revoked")
            entry.status = "executing"
            self._seal_entry(entry)
            return entry.effective_args

    def _finish_execute(self, *, prepared_identity: object) -> None:
        lock = self._cleanup_lock()
        if lock is None:
            return
        with lock:
            _entry_maps, prepared_maps = self._cleanup_roots()
            entries: list[_PreparationEntry] = []
            for prepared_map in prepared_maps:
                for entry in tuple(prepared_map.values()):
                    if all(existing is not entry for existing in entries):
                        entries.append(entry)
            for entry in entries:
                sealed_prepared_identity = (
                    entry.integrity_seal[8]
                    if type(entry.integrity_seal) is tuple and len(entry.integrity_seal) == 11
                    else None
                )
                if (
                    entry.prepared_identity is prepared_identity
                    or sealed_prepared_identity is prepared_identity
                ):
                    entry.raw_args = None
                    entry.effective_args = None
                    entry.metadata = None
                    entry.status = "executor_returned"
                    self._seal_entry(entry)
                    entry.status = "cleared"
                    self._seal_entry(entry)
                    return

    def _revoke_prepared(self, prepared_call: object) -> None:
        lock = self._cleanup_lock()
        if lock is None:
            return
        with lock:
            try:
                if type(prepared_call) is not PreparedLegacyCall:
                    return
                prepared_call._require_registry(self)
            except (TypeError, ValueError):
                return
            entry_maps, prepared_maps = self._cleanup_roots()
            entry = next(
                (
                    prepared_map[prepared_call]
                    for prepared_map in prepared_maps
                    if prepared_call in prepared_map
                ),
                None,
            )
            if entry is None:
                entry = next(
                    (
                        candidate
                        for entry_map in entry_maps
                        for candidate in entry_map.values()
                        if (
                            candidate.prepared_call is prepared_call
                            or (
                                type(candidate.integrity_seal) is tuple
                                and len(candidate.integrity_seal) == 11
                                and candidate.integrity_seal[7] is prepared_call
                            )
                        )
                    ),
                    None,
                )
            if entry is not None:
                self._revoke_entry(entry)

    def _revoke_lease(self, issuance_lease: LegacyRouteIssuanceLease) -> None:
        lock = self._cleanup_lock()
        if lock is None:
            return
        with lock:
            entry_maps, _prepared_maps = self._cleanup_roots()
            entries: list[_PreparationEntry] = []
            for entry_map in entry_maps:
                for entry in tuple(entry_map.values()):
                    if all(existing is not entry for existing in entries):
                        entries.append(entry)
            for entry in entries:
                sealed_issuance_lease = (
                    entry.integrity_seal[10]
                    if type(entry.integrity_seal) is tuple and len(entry.integrity_seal) == 11
                    else None
                )
                if (
                    entry.issuance_lease is issuance_lease
                    or sealed_issuance_lease is issuance_lease
                ):
                    self._revoke_entry(entry)


@dataclass(slots=True, eq=False)
class _ProofEntry:
    proof: LegacyRouteProof
    proof_identity: object
    issuer_token: object
    catalog_token: object
    bundle_token: object
    runtime_container_token: object
    issuance_lease: LegacyRouteIssuanceLease
    claim_identity: object
    binding_identity: object
    prepared_identity: object
    adapter: _LegacyAdapter
    safe_metadata: Mapping[str, object]
    state: Literal["issued", "resolved", "revoked"] = "issued"
    handle: object | None = None
    execution_started: bool = False
    integrity_seal: tuple[object, ...] = ()


class LegacyRouteProofRegistry(TransientToolRuntimeValue):
    __slots__ = (
        "_lock",
        "_preparation_registry",
        "_catalog_instance_token",
        "_bundle_instance_token",
        "_runtime_container_token",
        "_ordered_adapters",
        "_handle_factory",
        "_handle_registry_identity",
        "_handle_type",
        "_issuer_token",
        "_consumer_token",
        "_proofs",
        "_handles",
        "_registration_port",
        "_consumer_port",
        "_integrity_seal",
    )
    _lock: RLock
    _preparation_registry: LegacyPreparationRegistry
    _catalog_instance_token: object
    _bundle_instance_token: object
    _runtime_container_token: object
    _ordered_adapters: tuple[_LegacyAdapter, ...]
    _handle_factory: Callable[..., object]
    _handle_registry_identity: object
    _handle_type: type[object] | None
    _issuer_token: object
    _consumer_token: object
    _proofs: dict[LegacyRouteProof, _ProofEntry]
    _handles: dict[object, _ProofEntry]
    _registration_port: LegacyRouteProofRegistrationPort | None
    _consumer_port: LegacyRouteProofConsumerPort | None
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        preparation_registry: LegacyPreparationRegistry,
        catalog_instance_token: object,
        bundle_instance_token: object,
        runtime_container_token: object,
        ordered_adapters: Sequence[_LegacyAdapter],
        handle_factory: Callable[..., object],
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy Proof Registry is factory-created")
        if type(preparation_registry) is not LegacyPreparationRegistry:
            raise TypeError("Legacy Proof Registry requires the exact Preparation Registry")
        if not callable(handle_factory):
            raise TypeError("Legacy Proof Registry requires an opaque handle factory")
        provided_adapters = tuple(ordered_adapters)
        expected_adapters = preparation_registry._ordered_adapters
        if len(provided_adapters) != len(expected_adapters) or any(
            provided is not expected
            for provided, expected in zip(provided_adapters, expected_adapters)
        ):
            raise ValueError("Legacy Proof and Preparation Registry adapters differ")
        ordered = expected_adapters
        lock = preparation_registry.shared_lock
        issuer_token = object()
        consumer_token = object()
        handle_registry_identity = object()
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_preparation_registry", preparation_registry)
        object.__setattr__(self, "_catalog_instance_token", catalog_instance_token)
        object.__setattr__(self, "_bundle_instance_token", bundle_instance_token)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        object.__setattr__(self, "_ordered_adapters", ordered)
        object.__setattr__(self, "_handle_factory", handle_factory)
        object.__setattr__(self, "_handle_registry_identity", handle_registry_identity)
        object.__setattr__(self, "_handle_type", None)
        object.__setattr__(self, "_issuer_token", issuer_token)
        object.__setattr__(self, "_consumer_token", consumer_token)
        object.__setattr__(self, "_proofs", {})
        object.__setattr__(self, "_handles", {})
        object.__setattr__(self, "_registration_port", None)
        object.__setattr__(self, "_consumer_port", None)
        self._seal_registry()

    @classmethod
    def _create(
        cls,
        *,
        preparation_registry: LegacyPreparationRegistry,
        catalog_instance_token: object,
        bundle_instance_token: object,
        runtime_container_token: object,
        ordered_adapters: Sequence[_LegacyAdapter],
        handle_factory: Callable[..., object],
    ) -> LegacyRouteProofRegistry:
        return cls(
            _VALUE_SEAL,
            preparation_registry=preparation_registry,
            catalog_instance_token=catalog_instance_token,
            bundle_instance_token=bundle_instance_token,
            runtime_container_token=runtime_container_token,
            ordered_adapters=ordered_adapters,
            handle_factory=handle_factory,
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy Proof Registry is sealed")

    def _seal_registry(self) -> None:
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                self._lock,
                self._preparation_registry,
                self._catalog_instance_token,
                self._bundle_instance_token,
                self._runtime_container_token,
                self._ordered_adapters,
                self._handle_factory,
                self._handle_registry_identity,
                self._handle_type,
                self._issuer_token,
                self._consumer_token,
                self._proofs,
                self._handles,
                self._registration_port,
                self._consumer_port,
            ),
        )

    @property
    def catalog_instance_token(self) -> object:
        self._require_integrity()
        return self._catalog_instance_token

    @property
    def bundle_instance_token(self) -> object:
        self._require_integrity()
        return self._bundle_instance_token

    def _require_integrity(self) -> None:
        try:
            if not _same_identity_tuple(
                self._integrity_seal,
                (
                    self._lock,
                    self._preparation_registry,
                    self._catalog_instance_token,
                    self._bundle_instance_token,
                    self._runtime_container_token,
                    self._ordered_adapters,
                    self._handle_factory,
                    self._handle_registry_identity,
                    self._handle_type,
                    self._issuer_token,
                    self._consumer_token,
                    self._proofs,
                    self._handles,
                    self._registration_port,
                    self._consumer_port,
                ),
            ):
                raise ValueError("Legacy Proof Registry integrity drift")
            self._preparation_registry._require_integrity()
            for adapter in self._ordered_adapters:
                adapter.require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy Proof Registry integrity drift") from exc

    def _create_registration_port(self) -> LegacyRouteProofRegistrationPort:
        with self._lock:
            self._require_integrity()
            if self._registration_port is not None:
                raise ValueError("Legacy proof issuer registration Port already exists")
            port = LegacyRouteProofRegistrationPort(
                _VALUE_SEAL,
                registry=self,
                issuer_token=self._issuer_token,
            )
            object.__setattr__(self, "_registration_port", port)
            self._seal_registry()
            return port

    def _create_consumer_port(self) -> LegacyRouteProofConsumerPort:
        with self._lock:
            self._require_integrity()
            if self._consumer_port is not None:
                raise ValueError("Legacy proof consumer Port already exists")
            port = LegacyRouteProofConsumerPort(
                _VALUE_SEAL,
                proof_registry=self,
                preparation_registry=self._preparation_registry,
                consumer_token=self._consumer_token,
                bundle_instance_token=self._bundle_instance_token,
            )
            object.__setattr__(self, "_consumer_port", port)
            self._seal_registry()
            return port

    def _issue(
        self,
        *,
        registration_port: LegacyRouteProofRegistrationPort,
        prepared_call: PreparedLegacyCall,
        issuance_lease: LegacyRouteIssuanceLease,
        claim_lease: LegacyClaimLease,
        persisted_protocol_name: str,
        route_source: str,
        safe_metadata: Mapping[str, object],
    ) -> LegacyRouteProof:
        frozen_metadata = _freeze_mapping(safe_metadata, "proof metadata")
        if frozenset(frozen_metadata) != _PROOF_SAFE_METADATA_KEYS:
            raise ValueError("Legacy proof-safe metadata has the wrong exact shape")
        _reject_argument_material(frozen_metadata)
        with self._lock:
            self._require_integrity()
            registration_port._require_port(self, self._issuer_token)
            if registration_port is not self._registration_port:
                raise ValueError("Legacy proof has the wrong issuer registration Port")
            if type(route_source) is not str or route_source != _ROUTE_SOURCE:
                raise ValueError("Legacy proof route source must be confirmation_resume")
            if frozen_metadata.get("adapter_kind") != "legacy_deterministic":
                raise ValueError("Legacy proof Adapter kind identity mismatch")
            if frozen_metadata.get("operation_role") != "primary":
                raise ValueError("Legacy proof operation role identity mismatch")
            if frozen_metadata.get("route_source") != route_source:
                raise ValueError("Legacy proof route source identity mismatch")
            if type(persisted_protocol_name) is not str:
                raise TypeError("Legacy persisted protocol name must be exact text")
            matching_adapters = tuple(
                candidate
                for candidate in self._ordered_adapters
                if candidate.name == persisted_protocol_name
            )
            if len(matching_adapters) != 1:
                raise ValueError("Legacy persisted protocol name is outside the exact Catalog")
            adapter = matching_adapters[0]
            adapter.require_integrity()
            issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
            claim_identity, claim_operation_id, claim_id, _claimed_at = claim_lease._snapshot(
                issuance_lease=issuance_lease,
            )
            metadata_operation_id = frozen_metadata.get("operation_id")
            if metadata_operation_id is not None and metadata_operation_id != claim_operation_id:
                raise ValueError("Legacy proof operation and claim identity mismatch")
            if frozen_metadata.get("claim_id") != claim_id:
                raise ValueError("Legacy proof claim identity mismatch")
            if frozen_metadata.get("persisted_protocol_name") != persisted_protocol_name:
                raise ValueError("Legacy proof persisted protocol identity mismatch")
            if frozen_metadata.get("adapter_ordinal") != adapter.ordinal:
                raise ValueError("Legacy proof Adapter ordinal identity mismatch")
            binding_identity, prepared_identity = self._preparation_registry._consume_for_proof(
                prepared_call=prepared_call,
                adapter=adapter,
                issuance_lease=issuance_lease,
            )
            proof: LegacyRouteProof | None = None
            entry: _ProofEntry | None = None
            try:
                proof = LegacyRouteProof(_VALUE_SEAL, registry=self)
                proof_identity = proof._require_registry(self)
                entry = _ProofEntry(
                    proof=proof,
                    proof_identity=proof_identity,
                    issuer_token=self._issuer_token,
                    catalog_token=self._catalog_instance_token,
                    bundle_token=self._bundle_instance_token,
                    runtime_container_token=self._runtime_container_token,
                    issuance_lease=issuance_lease,
                    claim_identity=claim_identity,
                    binding_identity=binding_identity,
                    prepared_identity=prepared_identity,
                    adapter=adapter,
                    safe_metadata=frozen_metadata,
                )
                self._proofs[proof] = entry
                self._seal_entry(entry)
                issuance_lease._attach_cleanup(
                    lock=self._lock,
                    runtime_container_token=self._runtime_container_token,
                    owner=self,
                    callback=lambda: self._revoke_lease(issuance_lease),
                )
                return proof
            except BaseException:
                if entry is not None:
                    entry.state = "revoked"
                    self._seal_entry(entry)
                    if entry.handle is not None:
                        self._handles.pop(entry.handle, None)
                if proof is not None:
                    self._proofs.pop(proof, None)
                self._preparation_registry._revoke_prepared(prepared_call)
                raise

    def _proof_entry(self, proof: object) -> _ProofEntry:
        if type(proof) is not LegacyRouteProof:
            raise TypeError("Legacy Catalog requires an exact route proof")
        proof._require_registry(self)
        entry = self._proofs.get(proof)
        if entry is None or entry.proof is not proof:
            raise ValueError(
                "Legacy route proof is revoked, closed, or has the wrong Catalog Registry"
            )
        return entry

    def _require_entry_integrity(self, entry: _ProofEntry) -> None:
        try:
            proof_identity = entry.proof._require_registry(self)
            if (
                type(entry) is not _ProofEntry
                or not _same_identity_tuple(
                    entry.integrity_seal,
                    self._entry_identity(entry),
                )
                or proof_identity is not entry.proof_identity
                or self._proofs.get(entry.proof) is not entry
                or entry.issuer_token is not self._issuer_token
                or entry.catalog_token is not self._catalog_instance_token
                or entry.bundle_token is not self._bundle_instance_token
                or entry.runtime_container_token is not self._runtime_container_token
                or all(candidate is not entry.adapter for candidate in self._ordered_adapters)
                or frozenset(entry.safe_metadata) != _PROOF_SAFE_METADATA_KEYS
                or entry.safe_metadata.get("adapter_kind") != "legacy_deterministic"
                or entry.safe_metadata.get("operation_role") != "primary"
                or entry.safe_metadata.get("route_source") != _ROUTE_SOURCE
                or entry.safe_metadata.get("persisted_protocol_name") != entry.adapter.name
                or entry.safe_metadata.get("adapter_ordinal") != entry.adapter.ordinal
                or type(entry.execution_started) is not bool
            ):
                raise ValueError("Legacy proof entry integrity drift")
            _reject_argument_material(entry.safe_metadata)
            entry.adapter.require_integrity()
            if entry.state == "resolved":
                if entry.handle is None or self._handles.get(entry.handle) is not entry:
                    raise ValueError("Legacy resolved route handle integrity drift")
            elif entry.state == "issued":
                if entry.handle is not None or entry.execution_started:
                    raise ValueError("Legacy issued proof entry state drift")
            else:
                raise ValueError("Legacy proof entry state drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof entry or Adapter integrity drift") from exc

    @staticmethod
    def _entry_identity(entry: _ProofEntry) -> tuple[object, ...]:
        return (
            entry,
            entry.proof,
            entry.proof_identity,
            entry.issuer_token,
            entry.catalog_token,
            entry.bundle_token,
            entry.runtime_container_token,
            entry.issuance_lease,
            entry.claim_identity,
            entry.binding_identity,
            entry.prepared_identity,
            entry.adapter,
            entry.safe_metadata,
            entry.state,
            entry.handle,
            entry.execution_started,
        )

    def _seal_entry(self, entry: _ProofEntry) -> None:
        entry.integrity_seal = self._entry_identity(entry)

    def _require_handle_integrity(self, handle: object) -> None:
        handle_type = self._handle_type
        if handle_type is None or type(handle) is not handle_type:
            raise TypeError("Legacy route requires the exact proof-derived handle type")
        ensure_integrity = getattr(handle, "_ensure_integrity", None)
        if not callable(ensure_integrity):
            raise TypeError("Legacy proof-derived handle has no integrity boundary")
        try:
            ensure_integrity(self._handle_registry_identity)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof-derived route handle integrity drift") from exc

    def _consume(
        self,
        *,
        consumer_port: LegacyRouteProofConsumerPort,
        proof: LegacyRouteProof,
    ) -> object:
        with self._lock:
            self._require_integrity()
            consumer_port._require_port(self, self._consumer_token)
            if consumer_port is not self._consumer_port:
                raise ValueError("Legacy proof has the wrong Catalog consumer Port")
            entry = self._proof_entry(proof)
            self._require_entry_integrity(entry)
            entry.issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
            if entry.state == "resolved":
                raise ValueError("Legacy route proof was already resolved once")
            if entry.state != "issued":
                raise ValueError("Legacy route proof is revoked or expired")
            if (
                entry.issuer_token is not self._issuer_token
                or entry.catalog_token is not self._catalog_instance_token
                or entry.bundle_token is not self._bundle_instance_token
                or entry.runtime_container_token is not self._runtime_container_token
            ):
                raise ValueError("Legacy proof issuer, Catalog, or Bundle identity mismatch")
            handle = self._handle_factory(
                registry_identity=self._handle_registry_identity,
            )
            ensure_integrity = getattr(handle, "_ensure_integrity", None)
            if not callable(ensure_integrity):
                raise TypeError("Legacy handle factory returned a value without integrity checks")
            ensure_integrity(self._handle_registry_identity)
            if self._handle_type is None:
                object.__setattr__(self, "_handle_type", type(handle))
                self._seal_registry()
            elif type(handle) is not self._handle_type:
                raise TypeError("Legacy handle factory changed the exact handle type")
            if handle in self._handles:
                raise ValueError("Legacy route handle factory reused an identity")
            entry.state = "resolved"
            entry.handle = handle
            self._handles[handle] = entry
            self._seal_entry(entry)
            return handle

    def _execute(
        self,
        *,
        consumer_port: LegacyRouteProofConsumerPort,
        handle: object,
        context: object,
        before_execute: Callable[[], object] | None,
    ) -> str:
        with self._lock:
            self._require_integrity()
            consumer_port._require_port(self, self._consumer_token)
            if consumer_port is not self._consumer_port:
                raise ValueError("Legacy route handle has the wrong consumer Port")
            self._require_handle_integrity(handle)
            entry = self._handles.get(handle)
            if entry is None or entry.handle is not handle:
                raise ValueError("Legacy route handle is revoked or has the wrong Registry")
            self._require_entry_integrity(entry)
            entry.issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
                execution_context=context,
            )
            if entry.state != "resolved" or entry.execution_started:
                raise ValueError("Legacy route handle can execute only once")
            entry.execution_started = True
            self._seal_entry(entry)
            try:
                encoded_args = self._preparation_registry._borrow_for_execute(
                    prepared_identity=entry.prepared_identity,
                    adapter=entry.adapter,
                    issuance_lease=entry.issuance_lease,
                )
                if before_execute is not None:
                    entry.issuance_lease._run_bound_projection(before_execute)
                execute = entry.adapter.execute
                return entry.issuance_lease._run_verified_executor(
                    lambda: execute(encoded_args, context)
                )
            finally:
                self._preparation_registry._finish_execute(
                    prepared_identity=entry.prepared_identity,
                )

    def _require_route_identity(
        self,
        *,
        consumer_port: LegacyRouteProofConsumerPort,
        handle: object,
    ) -> tuple[int, str, str]:
        """Verify one proof-derived handle without borrowing args or executing it."""

        with self._lock:
            self._require_integrity()
            consumer_port._require_port(self, self._consumer_token)
            if consumer_port is not self._consumer_port:
                raise ValueError("Legacy route handle has the wrong consumer Port")
            self._require_handle_integrity(handle)
            entry = self._handles.get(handle)
            if entry is None or entry.handle is not handle:
                raise ValueError("Legacy route handle is revoked or has the wrong Registry")
            self._require_entry_integrity(entry)
            entry.issuance_lease._require_live(
                lock=self._lock,
                runtime_container_token=self._runtime_container_token,
            )
            if entry.state != "resolved":
                raise ValueError("Legacy route handle is revoked or unresolved")
            adapter = entry.adapter
            adapter.require_integrity()
            return (
                adapter.ordinal,
                adapter.name,
                cast(str, entry.safe_metadata["route_source"]),
            )

    def _revoke_lease(self, issuance_lease: LegacyRouteIssuanceLease) -> None:
        seal = getattr(self, "_integrity_seal", ())
        sealed_lock = seal[0] if type(seal) is tuple and len(seal) == 15 else None
        lock = sealed_lock if type(sealed_lock) is _RLOCK_TYPE else self._lock
        if type(lock) is not _RLOCK_TYPE:
            return
        with lock:
            proof_maps: list[dict[LegacyRouteProof, _ProofEntry]] = []
            handle_maps: list[dict[object, _ProofEntry]] = []
            for candidate in (
                self._proofs,
                seal[11] if type(seal) is tuple and len(seal) == 15 else None,
            ):
                if isinstance(candidate, dict) and all(
                    existing is not candidate for existing in proof_maps
                ):
                    proof_maps.append(cast(dict[LegacyRouteProof, _ProofEntry], candidate))
            for candidate in (
                self._handles,
                seal[12] if type(seal) is tuple and len(seal) == 15 else None,
            ):
                if isinstance(candidate, dict) and all(
                    existing is not candidate for existing in handle_maps
                ):
                    handle_maps.append(cast(dict[object, _ProofEntry], candidate))
            entries: list[_ProofEntry] = []
            for proof_map in proof_maps:
                for entry in tuple(proof_map.values()):
                    if type(entry) is _ProofEntry and all(
                        existing is not entry for existing in entries
                    ):
                        entries.append(entry)
            for handle_map in handle_maps:
                for entry in tuple(handle_map.values()):
                    if type(entry) is _ProofEntry and all(
                        existing is not entry for existing in entries
                    ):
                        entries.append(entry)
            for entry in entries:
                sealed_issuance_lease = (
                    entry.integrity_seal[7]
                    if type(entry.integrity_seal) is tuple and len(entry.integrity_seal) == 16
                    else None
                )
                if (
                    entry.issuance_lease is issuance_lease
                    or sealed_issuance_lease is issuance_lease
                ):
                    for proof_map in proof_maps:
                        for proof, candidate in tuple(proof_map.items()):
                            if candidate is entry:
                                proof_map.pop(proof, None)
                    for handle_map in handle_maps:
                        for handle, candidate in tuple(handle_map.items()):
                            if candidate is entry:
                                handle_map.pop(handle, None)
                    entry.state = "revoked"
                    entry.handle = None
                    self._seal_entry(entry)


class LegacyRouteProofRegistrationPort(_SealedValue):
    """Issuer-only capability; deliberately has no generic ``register`` API."""

    __slots__ = ("_registry", "_issuer_token", "_integrity_seal")
    _registry: LegacyRouteProofRegistry
    _issuer_token: object
    _integrity_seal: tuple[object, object]

    def __init__(
        self,
        seal: object | None = None,
        *,
        registry: LegacyRouteProofRegistry,
        issuer_token: object,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy proof issuer registration Ports are factory-created")
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_issuer_token", issuer_token)
        object.__setattr__(self, "_integrity_seal", (registry, issuer_token))

    def _require_port(self, registry: LegacyRouteProofRegistry, issuer_token: object) -> None:
        try:
            if (
                self._registry is not registry
                or self._issuer_token is not issuer_token
                or not _same_identity_tuple(
                    self._integrity_seal,
                    (registry, issuer_token),
                )
            ):
                raise ValueError("Legacy proof issuer registration Port identity mismatch")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof issuer registration Port identity mismatch") from exc

    def issue(
        self,
        *,
        prepared_call: PreparedLegacyCall,
        issuance_lease: LegacyRouteIssuanceLease,
        claim_lease: LegacyClaimLease,
        persisted_protocol_name: str,
        route_source: str,
        safe_metadata: Mapping[str, object],
    ) -> LegacyRouteProof:
        return self._registry._issue(
            registration_port=self,
            prepared_call=prepared_call,
            issuance_lease=issuance_lease,
            claim_lease=claim_lease,
            persisted_protocol_name=persisted_protocol_name,
            route_source=route_source,
            safe_metadata=safe_metadata,
        )


class LegacyRouteProofConsumerPort(_SealedValue):
    __slots__ = (
        "_proof_registry",
        "_preparation_registry",
        "_consumer_token",
        "_bundle_instance_token",
        "_integrity_seal",
    )
    _proof_registry: LegacyRouteProofRegistry
    _preparation_registry: LegacyPreparationRegistry
    _consumer_token: object
    _bundle_instance_token: object
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        proof_registry: LegacyRouteProofRegistry,
        preparation_registry: LegacyPreparationRegistry,
        consumer_token: object,
        bundle_instance_token: object,
    ) -> None:
        if seal is not _VALUE_SEAL:
            raise TypeError("Legacy proof consumer Ports are factory-created")
        object.__setattr__(self, "_proof_registry", proof_registry)
        object.__setattr__(self, "_preparation_registry", preparation_registry)
        object.__setattr__(self, "_consumer_token", consumer_token)
        object.__setattr__(self, "_bundle_instance_token", bundle_instance_token)
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                proof_registry,
                preparation_registry,
                consumer_token,
                bundle_instance_token,
            ),
        )

    def _require_port(
        self,
        proof_registry: LegacyRouteProofRegistry,
        consumer_token: object,
    ) -> None:
        try:
            if (
                self._proof_registry is not proof_registry
                or self._consumer_token is not consumer_token
                or not _same_identity_tuple(
                    self._integrity_seal,
                    (
                        self._proof_registry,
                        self._preparation_registry,
                        self._consumer_token,
                        self._bundle_instance_token,
                    ),
                )
            ):
                raise ValueError("Legacy proof consumer Port identity mismatch")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof consumer Port identity mismatch") from exc

    @property
    def proof_registry(self) -> LegacyRouteProofRegistry:
        self._require_port(self._proof_registry, self._consumer_token)
        return self._proof_registry

    @property
    def preparation_registry(self) -> LegacyPreparationRegistry:
        self._require_port(self._proof_registry, self._consumer_token)
        return self._preparation_registry

    @property
    def bundle_instance_token(self) -> object:
        self._require_port(self._proof_registry, self._consumer_token)
        return self._bundle_instance_token

    @property
    def catalog_instance_token(self) -> object:
        self._require_port(self._proof_registry, self._consumer_token)
        return self._proof_registry.catalog_instance_token

    def consume(self, proof: LegacyRouteProof) -> object:
        return self._proof_registry._consume(consumer_port=self, proof=proof)

    def require_route_identity(self, handle: object) -> tuple[int, str, str]:
        """Validate a live route and return only its closed routing identity."""

        return self._proof_registry._require_route_identity(
            consumer_port=self,
            handle=handle,
        )

    def execute(
        self,
        handle: object,
        context: object,
        *,
        before_execute: Callable[[], object] | None = None,
    ) -> str:
        return self._proof_registry._execute(
            consumer_port=self,
            handle=handle,
            context=context,
            before_execute=before_execute,
        )


__all__ = [
    "LegacyApprovedConfirmationInput",
    "LegacyClaimLease",
    "LegacyConfirmationLookupIdentity",
    "LegacyPreparationBinding",
    "LegacyPreparationRegistry",
    "LegacyPreparedInputPort",
    "LegacyRouteIssuanceLease",
    "LegacyRouteProof",
    "LegacyRouteProofConsumerPort",
    "LegacyRouteProofRegistry",
    "PreparedLegacyCall",
    "PreparedLegacyInputV1",
]
