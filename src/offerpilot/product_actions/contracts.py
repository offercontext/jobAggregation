"""Leaf contracts for the Provider-invisible Product Action boundary."""

from __future__ import annotations

import json
import math
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, ClassVar, Literal, NoReturn, SupportsIndex, TypeAlias, cast
from uuid import UUID


PRODUCT_ACTION_NAMES = (
    "confirm_interview_story",
    "save_review_readiness_signal",
)
PRODUCT_ACTION_COMPENSATION_NAMES = (
    "undo:confirm_interview_story",
    "undo:save_review_readiness_signal",
)
EXPECTED_PREFIX = {
    "proposed": ((1, "proposed"),),
    "rejected": ((1, "proposed"), (2, "rejected")),
    "committed": ((1, "proposed"), (2, "approved"), (3, "claimed"), (4, "committed")),
    "failed": ((1, "proposed"), (2, "approved"), (3, "claimed"), (4, "failed")),
}

ProductActionName: TypeAlias = Literal[
    "confirm_interview_story",
    "save_review_readiness_signal",
]
ProductActionRequestOrigin: TypeAlias = Literal["current", "historical_story_bridge"]
JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
FrozenJSONValue: TypeAlias = (
    JSONScalar | tuple["FrozenJSONValue", ...] | MappingProxyType[str, "FrozenJSONValue"]
)

_MAX_INT64 = 2**63 - 1
_ROUTE_BYTES = 16_384
_PROOF_CONSTRUCTION_SEAL = object()


class ProductActionContractError(ValueError):
    """Untrusted Product Action input violates the closed V1 contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductActionIntegrityError(RuntimeError):
    """Persisted Product Action state is partial, mismatched, or unverifiable."""

    def __init__(self, code: str = "product_action_integrity_error") -> None:
        self.code = code
        super().__init__(code)


def _duplicate_aware_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ProductActionContractError("duplicate_json_key")
        result[key] = value
    return result


def _deny_nonfinite(_value: str) -> NoReturn:
    raise ProductActionContractError("non_finite_number")


def _require_json_value(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        if type(value) is int and not -(2**63) <= value <= _MAX_INT64:
            raise ProductActionContractError("integer_out_of_range")
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ProductActionContractError("invalid_unicode") from exc
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ProductActionContractError("non_finite_number")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _require_json_value(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ProductActionContractError("invalid_json_key")
            _require_json_value(item)
        return
    raise ProductActionContractError("invalid_json_value")


def _canonical_json(value: JSONValue) -> str:
    _require_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductActionContractError("invalid_json_value") from exc


def canonical_product_action_json(value: JSONValue) -> str:
    """Canonical JSON used by every Product Action HMAC envelope."""

    return _canonical_json(value)


def decode_product_action_request_v1(
    raw: bytes,
    *,
    max_bytes: int = _ROUTE_BYTES,
) -> dict[str, JSONValue]:
    """Decode raw bytes without allowing a framework coercion step first."""

    if type(raw) is not bytes:
        raise ProductActionContractError("raw_body_required")
    if type(max_bytes) is not int or type(max_bytes) is bool or max_bytes < 1:
        raise ProductActionContractError("invalid_byte_limit")
    if len(raw) > max_bytes:
        raise ProductActionContractError("route_payload_too_large")
    try:
        source = raw.decode("utf-8", errors="strict")
        value = json.loads(
            source,
            object_pairs_hook=_duplicate_aware_object,
            parse_constant=_deny_nonfinite,
        )
    except ProductActionContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ProductActionContractError("invalid_json") from exc
    if type(value) is not dict:
        raise ProductActionContractError("json_object_required")
    result = cast(dict[str, JSONValue], value)
    _require_json_value(result)
    _canonical_json(result)
    return result


def _require_exact_keys(value: dict[str, JSONValue], expected: tuple[str, ...]) -> None:
    if len(value) != len(expected) or set(value) != set(expected):
        raise ProductActionContractError("invalid_exact_shape")


def _require_positive_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ProductActionContractError(f"{field}_exact_integer")
    integer = value
    if not 1 <= integer <= _MAX_INT64:
        raise ProductActionContractError(f"{field}_positive_integer")
    return integer


def _require_optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field)


def _require_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ProductActionContractError(f"{field}_invalid_sha256")
    return value


def require_product_action_hmac(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 76
        or not value.startswith("hmac-sha256:")
        or any(character not in "0123456789abcdef" for character in value[12:])
    ):
        raise ProductActionContractError(f"{field}_invalid_hmac")
    return value


def _require_uuid(value: object, field: str) -> str:
    if type(value) is not str:
        raise ProductActionContractError(f"{field}_invalid_uuid")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ProductActionContractError(f"{field}_invalid_uuid") from exc
    if value != normalized:
        raise ProductActionContractError(f"{field}_invalid_uuid")
    return normalized


def require_product_action_uuid(value: object, field: str) -> str:
    return _require_uuid(value, field)


def _require_focus_id(value: object) -> str:
    if type(value) is not str:
        raise ProductActionContractError("focus_id_invalid")
    encoded = value.encode("utf-8")
    if not 1 <= len(encoded) <= 128 or any(ord(character) < 0x20 for character in value):
        raise ProductActionContractError("focus_id_invalid")
    return value


def _require_user_note(value: object) -> str:
    if type(value) is not str:
        raise ProductActionContractError("user_note_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductActionContractError("user_note_invalid") from exc
    if len(value) > 500 or len(encoded) > 2_048:
        raise ProductActionContractError("user_note_too_large")
    return value


def _freeze_json(value: JSONValue) -> FrozenJSONValue:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return cast(JSONScalar, value)


SIGNAL_ROUTE_FIELDS = (
    "application_id",
    "event_id",
    "note_id",
    "proposal_id",
    "proposal_schema_version",
    "focus_id",
    "expected_note_revision",
    "expected_source_fingerprint",
    "expected_proposal_hash",
    "expected_candidate_fingerprint",
    "user_note",
    "domain_idempotency_key",
)
STORY_ROUTE_FIELDS = (
    "attempt_id",
    "generation_revision",
    "proposal_hash",
    "source_fingerprint",
    "target_story_id",
    "expected_current_version_id",
    "expected_story_revision",
    "product_action_generation",
)


@dataclass(frozen=True, slots=True, repr=False)
class DecodedProductActionRouteV1:
    action_name: ProductActionName
    request_origin: ProductActionRequestOrigin
    source_kind: Literal["story_proposal", "review_focus"]
    source_id: int
    source_revision: int
    payload: MappingProxyType[str, FrozenJSONValue]
    canonical_json: str

    @property
    def source_identity(self) -> tuple[str, int, int]:
        return self.source_kind, self.source_id, self.source_revision


def decode_product_action_route_payload(
    raw: bytes,
    *,
    action_name: str,
    request_origin: str,
) -> DecodedProductActionRouteV1:
    if action_name not in PRODUCT_ACTION_NAMES:
        raise ProductActionContractError("unknown_product_action")
    if request_origin not in {"current", "historical_story_bridge"}:
        raise ProductActionContractError("invalid_request_origin")
    if request_origin == "historical_story_bridge" and action_name != "confirm_interview_story":
        raise ProductActionContractError("invalid_request_origin")
    payload = decode_product_action_request_v1(raw)
    if action_name == "save_review_readiness_signal":
        try:
            _require_exact_keys(payload, SIGNAL_ROUTE_FIELDS)
        except ProductActionContractError as exc:
            if set(payload) == set(STORY_ROUTE_FIELDS):
                raise ProductActionContractError("action_source_mismatch") from exc
            raise
        for field in ("application_id", "event_id", "note_id", "proposal_id"):
            _require_positive_int(payload[field], field)
        if type(payload["proposal_schema_version"]) is not int:
            raise ProductActionContractError("proposal_schema_version_exact_integer")
        if payload["proposal_schema_version"] != 2:
            raise ProductActionContractError("proposal_schema_version_invalid")
        _require_positive_int(payload["expected_note_revision"], "expected_note_revision")
        _require_focus_id(payload["focus_id"])
        _require_sha256(payload["expected_source_fingerprint"], "expected_source_fingerprint")
        _require_sha256(payload["expected_proposal_hash"], "expected_proposal_hash")
        _require_sha256(payload["expected_candidate_fingerprint"], "candidate_fingerprint")
        _require_user_note(payload["user_note"])
        _require_uuid(payload["domain_idempotency_key"], "domain_idempotency_key")
        source_kind: Literal["story_proposal", "review_focus"] = "review_focus"
        source_id = cast(int, payload["proposal_id"])
        source_revision = cast(int, payload["expected_note_revision"])
    else:
        try:
            _require_exact_keys(payload, STORY_ROUTE_FIELDS)
        except ProductActionContractError as exc:
            if set(payload) == set(SIGNAL_ROUTE_FIELDS):
                raise ProductActionContractError("action_source_mismatch") from exc
            raise
        for field in ("attempt_id", "generation_revision", "product_action_generation"):
            _require_positive_int(payload[field], field)
        target = _require_optional_positive_int(payload["target_story_id"], "target_story_id")
        current = _require_optional_positive_int(
            payload["expected_current_version_id"], "expected_current_version_id"
        )
        revision = _require_optional_positive_int(
            payload["expected_story_revision"], "expected_story_revision"
        )
        if (target is None) != (current is None) or (target is None) != (revision is None):
            raise ProductActionContractError("story_target_cas")
        _require_sha256(payload["proposal_hash"], "proposal_hash")
        _require_sha256(payload["source_fingerprint"], "source_fingerprint")
        source_kind = "story_proposal"
        source_id = cast(int, payload["attempt_id"])
        source_revision = cast(int, payload["generation_revision"])
    if action_name == "save_review_readiness_signal" and set(payload) == set(STORY_ROUTE_FIELDS):
        raise ProductActionContractError("action_source_mismatch")
    canonical = _canonical_json(payload)
    if len(canonical.encode("utf-8")) > _ROUTE_BYTES:
        raise ProductActionContractError("route_payload_too_large")
    return DecodedProductActionRouteV1(
        cast(ProductActionName, action_name),
        cast(ProductActionRequestOrigin, request_origin),
        source_kind,
        source_id,
        source_revision,
        cast(MappingProxyType[str, FrozenJSONValue], _freeze_json(payload)),
        canonical,
    )


def materialize_frozen_json(value: FrozenJSONValue) -> JSONValue:
    if type(value) is MappingProxyType:
        return {
            key: materialize_frozen_json(item)
            for key, item in value.items()
        }
    if type(value) is tuple:
        return [materialize_frozen_json(item) for item in value]
    return cast(JSONScalar, value)


def tagged_optional(value: JSONValue | None) -> dict[str, JSONValue]:
    return {
        "state": "absent" if value is None else "present",
        "value": value,
    }


class _OpaqueProductActionProof:
    __slots__ = ("_registry_token", "_incarnation", "_nonce", "_seal")
    binding_fields: ClassVar[tuple[str, ...]] = ()
    _registry_token: object
    _incarnation: object
    _nonce: object
    _seal: tuple[object, object, object]

    def __new__(
        cls,
        seal: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> "_OpaqueProductActionProof":
        if seal is not _PROOF_CONSTRUCTION_SEAL:
            raise TypeError("Product Action proofs are factory-created")
        return object.__new__(cls)

    def __init__(
        self,
        seal: object | None = None,
        registry_token: object | None = None,
        incarnation: object | None = None,
        nonce: object | None = None,
    ) -> None:
        if seal is not _PROOF_CONSTRUCTION_SEAL or None in {
            registry_token,
            incarnation,
            nonce,
        }:
            raise TypeError("Product Action proofs are factory-created")
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(self, "_seal", (registry_token, incarnation, nonce))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action proofs are sealed")

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    __str__ = __repr__

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("Product Action proofs cannot be serialized or copied")

    def __reduce__(self) -> NoReturn:
        self._serialization_error()

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


class ProductActionRouteProof(_OpaqueProductActionProof):
    __slots__ = ()


class HistoricalStoryRouteProof(_OpaqueProductActionProof):
    __slots__ = ()


class SignalOwnerRecoveryProof(_OpaqueProductActionProof):
    __slots__ = ()
    allowed_decisions = ("approve", "modify", "reject")
    binding_fields = (
        "canonical_owner",
        "application_id",
        "event_id",
        "note_id",
        "proposal_id",
        "source_revision",
        "semantic_claim_fingerprint",
        "operation_id",
        "action_call_id",
        "route_payload_fingerprint",
        "route_binding_fingerprint",
        "request_origin",
        "allowed_decisions",
    )


class StoryOwnerRecoveryProof(_OpaqueProductActionProof):
    __slots__ = ()
    allowed_decisions = ("approve", "modify", "reject")
    binding_fields = (
        "canonical_owner",
        "attempt_id",
        "generation_revision",
        "proposal_hash",
        "operation_id",
        "action_call_id",
        "route_payload_fingerprint",
        "route_binding_fingerprint",
        "request_origin",
        "allowed_decisions",
        "product_action_generation",
    )


class RejectionOnlyRecoveryProof(_OpaqueProductActionProof):
    __slots__ = ()
    live_source_state = "not_observed"
    allowed_decisions = ("reject",)
    signal_binding_fields = (
        "canonical_owner",
        "application_id",
        "event_id",
        "note_id",
        "proposal_id",
        "live_source_state",
        "semantic_claim_fingerprint",
        "operation_id",
        "action_call_id",
        "route_payload_fingerprint",
        "route_binding_fingerprint",
        "allowed_decisions",
    )
    story_binding_fields = (
        "canonical_owner",
        "attempt_id",
        "live_source_state",
        "generation_revision",
        "product_action_generation",
        "proposal_hash",
        "operation_id",
        "action_call_id",
        "route_payload_fingerprint",
        "route_binding_fingerprint",
        "allowed_decisions",
    )
    binding_fields_by_action = MappingProxyType(
        {
            "save_review_readiness_signal": signal_binding_fields,
            "confirm_interview_story": story_binding_fields,
        }
    )

    @classmethod
    def binding_fields_for(cls, action_name: str) -> tuple[str, ...]:
        try:
            return cls.binding_fields_by_action[action_name]
        except KeyError as exc:
            raise ValueError("Rejection-only proof action is invalid") from exc


class ProductActionExecutionAuthorization(_OpaqueProductActionProof):
    __slots__ = ()


ProductActionProof: TypeAlias = (
    ProductActionRouteProof
    | HistoricalStoryRouteProof
    | SignalOwnerRecoveryProof
    | StoryOwnerRecoveryProof
    | RejectionOnlyRecoveryProof
    | ProductActionExecutionAuthorization
)
_PROOF_TYPES = (
    ProductActionRouteProof,
    HistoricalStoryRouteProof,
    SignalOwnerRecoveryProof,
    StoryOwnerRecoveryProof,
    RejectionOnlyRecoveryProof,
    ProductActionExecutionAuthorization,
)


@dataclass(slots=True)
class _ProofRecord:
    proof: ProductActionProof
    proof_type: type[ProductActionProof]
    action_name: ProductActionName
    binding: tuple[object, ...]
    state: Literal["issued", "in_flight", "consumed", "revoked"]


class _ProofClaim(AbstractContextManager[ProductActionProof]):
    __slots__ = ("_registry", "_proof", "_closed")

    def __init__(self, registry: "ProductActionProofRegistryV1", proof: ProductActionProof) -> None:
        self._registry = registry
        self._proof = proof
        self._closed = False

    def __enter__(self) -> ProductActionProof:
        if self._closed:
            raise ValueError("Product Action proof claim is closed")
        return self._proof

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if self._closed:
            return
        self._closed = True
        if exc_type is None:
            self._registry._finish(self._proof, "consumed")
        else:
            self._registry._finish(self._proof, "revoked")


class ProductActionProofRegistryV1:
    """One container-local registry with a one-way proof lifecycle."""

    __slots__ = (
        "_registry_token",
        "_incarnation",
        "_records",
        "_lock",
        "_integrity_seal",
    )
    _registry_token: object
    _incarnation: object
    _records: dict[int, _ProofRecord]
    _lock: RLock
    _integrity_seal: tuple[object, object, dict[int, _ProofRecord], RLock]

    def __init__(self) -> None:
        registry_token = object()
        incarnation = object()
        records: dict[int, _ProofRecord] = {}
        lock = RLock()
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(
            self,
            "_integrity_seal",
            (registry_token, incarnation, records, lock),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action proof Registry is sealed")

    def _ensure_integrity(self) -> None:
        if self._integrity_seal != (
            self._registry_token,
            self._incarnation,
            self._records,
            self._lock,
        ):
            raise ValueError("Product Action proof Registry integrity drift")

    def _issue(
        self,
        proof_type: type[Any],
        *,
        action_name: str,
        binding: tuple[object, ...],
    ) -> ProductActionProof:
        self._ensure_integrity()
        if proof_type not in _PROOF_TYPES:
            raise TypeError("Product Action proof type is outside the closed union")
        if action_name not in PRODUCT_ACTION_NAMES:
            raise ValueError("Product Action proof action is invalid")
        if type(binding) is not tuple:
            raise TypeError("Product Action proof binding must be an exact tuple")
        nonce = object()
        proof = proof_type(
            _PROOF_CONSTRUCTION_SEAL,
            self._registry_token,
            self._incarnation,
            nonce,
        )
        record = _ProofRecord(
            proof,
            proof_type,
            cast(ProductActionName, action_name),
            binding,
            "issued",
        )
        with self._lock:
            self._records[id(proof)] = record
        return proof

    def _record(self, proof: object) -> _ProofRecord:
        self._ensure_integrity()
        if type(proof) not in _PROOF_TYPES:
            raise TypeError("Product Action proof has the wrong union type")
        typed = cast(ProductActionProof, proof)
        try:
            if (
                typed._seal
                != (typed._registry_token, typed._incarnation, typed._nonce)
                or typed._registry_token is not self._registry_token
                or typed._incarnation is not self._incarnation
            ):
                raise ValueError
        except (AttributeError, ValueError) as exc:
            raise ValueError("Product Action proof provenance mismatch") from exc
        record = self._records.get(id(typed))
        if record is None or record.proof is not typed:
            raise ValueError("Product Action proof provenance mismatch")
        return record

    def require_issued(
        self,
        proof: object,
        *,
        proof_type: type[Any],
        action_name: str,
        expected_binding: tuple[object, ...],
    ) -> None:
        with self._lock:
            record = self._record(proof)
            if record.proof_type is not proof_type:
                raise TypeError("Product Action proof type mismatch")
            if record.action_name != action_name:
                raise ValueError("Product Action proof action mismatch")
            if record.binding != expected_binding:
                raise ValueError("Product Action proof binding mismatch")
            if record.state != "issued":
                raise ValueError(f"Product Action proof is {record.state}")

    def _issued_identity(self, proof: object) -> tuple[ProductActionName, tuple[object, ...]]:
        record = self._record(proof)
        with self._lock:
            if record.state != "issued":
                raise ValueError(f"Product Action proof is {record.state}")
            return record.action_name, record.binding

    def claim(
        self,
        proof: object,
        *,
        proof_type: type[Any],
        action_name: str,
        expected_binding: tuple[object, ...],
    ) -> AbstractContextManager[ProductActionProof]:
        self.require_issued(
            proof,
            proof_type=proof_type,
            action_name=action_name,
            expected_binding=expected_binding,
        )
        record = self._record(proof)
        with self._lock:
            if record.state != "issued":
                raise ValueError(f"Product Action proof is {record.state}")
            record.state = "in_flight"
        return _ProofClaim(self, record.proof)

    def _finish(
        self,
        proof: ProductActionProof,
        state: Literal["consumed", "revoked"],
    ) -> None:
        record = self._record(proof)
        with self._lock:
            if record.state != "in_flight":
                raise ValueError(f"Product Action proof is {record.state}")
            record.state = state

    def revoke(self, proof: object) -> None:
        record = self._record(proof)
        with self._lock:
            if record.state not in {"issued", "in_flight"}:
                raise ValueError(f"Product Action proof is {record.state}")
            record.state = "revoked"


__all__ = [
    "EXPECTED_PREFIX",
    "HistoricalStoryRouteProof",
    "PRODUCT_ACTION_COMPENSATION_NAMES",
    "PRODUCT_ACTION_NAMES",
    "ProductActionContractError",
    "ProductActionExecutionAuthorization",
    "ProductActionIntegrityError",
    "ProductActionProofRegistryV1",
    "ProductActionRouteProof",
    "RejectionOnlyRecoveryProof",
    "SignalOwnerRecoveryProof",
    "StoryOwnerRecoveryProof",
    "canonical_product_action_json",
    "decode_product_action_request_v1",
    "decode_product_action_route_payload",
    "materialize_frozen_json",
    "require_product_action_hmac",
    "require_product_action_uuid",
    "tagged_optional",
]
