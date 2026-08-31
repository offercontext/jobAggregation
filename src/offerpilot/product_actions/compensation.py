"""Owner-scoped, Provider-invisible Product Action compensation core."""

from __future__ import annotations

import hmac
import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    NoReturn,
    Protocol,
    SupportsIndex,
    cast,
)
from uuid import UUID, uuid5
from weakref import WeakKeyDictionary

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.write_operations import (
    LedgerKeyDomain,
    build_terminal_payload,
    ledger_fingerprint,
    payload_from_operation,
)
from offerpilot.models import (
    InterviewStory,
    InterviewStoryProposalAttempt,
    InterviewStoryUserAssertion,
    InterviewStoryVersion,
    InterviewStoryVersionEvidenceLink,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import ProductActionCompensationCatalogV1
from offerpilot.product_actions.contracts import (
    EXPECTED_PREFIX,
    JSONValue,
    ProductActionContractError,
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    canonical_product_action_json,
    require_product_action_hmac,
)
from offerpilot.product_actions.issuer import LedgerKeyProfileStoreV1

if TYPE_CHECKING:
    from offerpilot.review_readiness.repository import ReadinessSignalRepository
    from offerpilot.repositories.interview_stories import InterviewStoriesRepository


PRODUCT_ACTION_COMPENSATION_NAMESPACE = UUID(
    "1c914194-602c-54fa-b770-853a5ac87a2b"
)
READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE = UUID(
    "6fc5aa59-d7c6-53e0-9f3e-98aac460b593"
)

_PARENT_COMPENSATION = {
    "confirm_interview_story": "undo:confirm_interview_story",
    "save_review_readiness_signal": "undo:save_review_readiness_signal",
}
_UNDO_PROOF_CONSTRUCTION_SEAL = object()
_COMPENSATION_TRANSITION_NAMESPACE = UUID("a7a83c80-9f28-53bf-823f-b600ee4a7a72")
_RESULT_BYTES = 4 * 1024
_VISIBLE_BYTES = 1 * 1024
_TRANSPORT_BYTES = 4 * 1024
_UNDO_BYTES = 4 * 1024
_AGGREGATE_BYTES = 12 * 1024
_STORY_VERSION_ORIGIN_KINDS = frozenset({"manual", "proposal"})
_STORY_SOURCE_KINDS = frozenset(
    {"resume_version", "interview_note", "mock_turn"}
)
_STORY_TIME_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)
_STORY_TIME_FUTURE_SKEW = timedelta(minutes=5)


def _enforce_compensation_terminal_budgets(
    result_json: str,
    visible_result: str,
    transport_json: str,
    undo_json: str | None,
) -> None:
    values = (result_json, visible_result, transport_json, undo_json or "")
    sizes = tuple(len(value.encode("utf-8")) for value in values)
    if (
        sizes[0] > _RESULT_BYTES
        or sizes[1] > _VISIBLE_BYTES
        or sizes[2] > _TRANSPORT_BYTES
        or sizes[3] > _UNDO_BYTES
        or sum(sizes) > _AGGREGATE_BYTES
    ):
        raise ProductActionIntegrityError("product_action_compensation_terminal_budget")


def _execution_authorization_state(
    registry: ProductActionProofRegistryV1,
    authorization: ProductActionExecutionAuthorization,
    *,
    action_name: str,
    binding: tuple[object, ...],
) -> Literal["issued", "in_flight", "consumed", "revoked", "invalid"]:
    with registry._lock:
        live = registry._records.get(id(authorization))
        if live is not None:
            if (
                live.proof is authorization
                and live.proof_type is ProductActionExecutionAuthorization
                and live.action_name == action_name
                and live.binding == binding
                and live.state in {"issued", "in_flight"}
            ):
                return cast(Literal["issued", "in_flight"], live.state)
            return "invalid"
        retired = registry._retired.get(authorization)
        if (
            retired is None
            or retired.proof_type is not ProductActionExecutionAuthorization
            or retired.action_name != action_name
            or retired.binding != binding
            or retired.publication_refreshable is not False
            or retired.state not in {"consumed", "revoked"}
        ):
            return "invalid"
        return cast(Literal["consumed", "revoked"], retired.state)


def _revoke_execution_authorization_if_live(
    registry: ProductActionProofRegistryV1,
    authorization: ProductActionExecutionAuthorization,
    *,
    action_name: str,
    binding: tuple[object, ...],
) -> Literal["consumed", "revoked", "invalid"]:
    state = _execution_authorization_state(
        registry,
        authorization,
        action_name=action_name,
        binding=binding,
    )
    if state in {"issued", "in_flight"}:
        registry.revoke(authorization)
        state = _execution_authorization_state(
            registry,
            authorization,
            action_name=action_name,
            binding=binding,
        )
    return cast(Literal["consumed", "revoked", "invalid"], state)


class ProductActionCompensationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)


class ProductActionCompensationStale(RuntimeError):
    """The exact owner aggregate changed after Undo authorization."""

    code = "product_action_compensation_stale"

    def __init__(self, code: str = "product_action_compensation_stale") -> None:
        if type(code) is not str or not code.isascii() or not 1 <= len(code) <= 128:
            raise ValueError("Compensation stale code must be bounded ASCII")
        self.code = code
        super().__init__(code)


def _canonical_uuid(value: object, field: str) -> str:
    if type(value) is not str:
        raise ProductActionContractError(f"{field}_invalid_uuid")
    try:
        normalized = str(UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ProductActionContractError(f"{field}_invalid_uuid") from exc
    if normalized != value:
        raise ProductActionContractError(f"{field}_invalid_uuid")
    return normalized


def _require_parent_mapping(parent_action_name: str, compensation_kind: str) -> None:
    if _PARENT_COMPENSATION.get(parent_action_name) != compensation_kind:
        raise ProductActionContractError("product_action_compensation_mapping")


def product_action_compensation_operation_id(
    parent_operation_id: str,
    compensation_kind: str,
) -> str:
    """Return the closed, deterministic identity for one required Undo."""

    parent = _canonical_uuid(parent_operation_id, "parent_operation_id")
    if compensation_kind not in _PARENT_COMPENSATION.values():
        raise ProductActionContractError("unknown_product_action_compensation")
    return str(
        uuid5(
            PRODUCT_ACTION_COMPENSATION_NAMESPACE,
            parent + ":" + compensation_kind,
        )
    )


def readiness_signal_retraction_domain_key(compensation_operation_id: str) -> str:
    operation_id = _canonical_uuid(compensation_operation_id, "operation_id")
    return str(
        uuid5(
            READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE,
            operation_id + ":signal-retraction",
        )
    )


def product_action_compensation_request_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_id: str,
    parent_operation_id: str,
    parent_action_name: str,
    parent_terminal_payload_sha256: str,
    compensation_kind: str,
) -> str:
    normalized_operation = _canonical_uuid(operation_id, "operation_id")
    normalized_parent = _canonical_uuid(parent_operation_id, "parent_operation_id")
    _require_parent_mapping(parent_action_name, compensation_kind)
    if normalized_operation != product_action_compensation_operation_id(
        normalized_parent,
        compensation_kind,
    ):
        raise ProductActionContractError("product_action_compensation_operation_id")
    if (
        type(parent_terminal_payload_sha256) is not str
        or len(parent_terminal_payload_sha256) != 71
        or not parent_terminal_payload_sha256.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in parent_terminal_payload_sha256[7:]
        )
    ):
        raise ProductActionContractError("parent_terminal_payload_invalid_sha256")
    payload: dict[str, JSONValue] = {
        "request_kind": "product_action_compensation_v1",
        "operation_id": normalized_operation,
        "parent_operation_id": normalized_parent,
        "parent_action_name": parent_action_name,
        "parent_terminal_payload_sha256": parent_terminal_payload_sha256,
        "compensation_kind": compensation_kind,
    }
    return ledger_fingerprint(
        key,
        "product-action-compensation-request-v1",
        payload,
    )


def product_action_compensation_input_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_request_fingerprint: str,
    parent_terminal_payload_sha256: str,
    validated_undo_json: dict[str, JSONValue],
) -> str:
    require_product_action_hmac(
        operation_request_fingerprint,
        "operation_request_fingerprint",
    )
    if (
        type(parent_terminal_payload_sha256) is not str
        or len(parent_terminal_payload_sha256) != 71
        or not parent_terminal_payload_sha256.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in parent_terminal_payload_sha256[7:]
        )
    ):
        raise ProductActionContractError("parent_terminal_payload_invalid_sha256")
    if type(validated_undo_json) is not dict:
        raise ProductActionContractError("validated_undo_json")
    return ledger_fingerprint(
        key,
        "product-action-compensation-input-v1",
        cast(
            JSONValue,
            {
                "operation_request_fingerprint": operation_request_fingerprint,
                "parent_terminal_payload_sha256": parent_terminal_payload_sha256,
                "validated_undo_json": validated_undo_json,
            },
        ),
    )


class ReadinessSignalProductActionUndoProof:
    """Opaque, request-local evidence issued only after owner validation."""

    __slots__ = ("_registry_token", "_incarnation", "_nonce", "_seal", "__weakref__")
    _registry_token: object
    _incarnation: object
    _nonce: object
    _seal: tuple[object, object, object]

    def __new__(
        cls,
        construction_seal: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> "ReadinessSignalProductActionUndoProof":
        if construction_seal is not _UNDO_PROOF_CONSTRUCTION_SEAL:
            raise TypeError("Product Action compensation proofs are issuer-created")
        return object.__new__(cls)

    def __init__(
        self,
        construction_seal: object | None = None,
        registry_token: object | None = None,
        incarnation: object | None = None,
        nonce: object | None = None,
    ) -> None:
        if construction_seal is not _UNDO_PROOF_CONSTRUCTION_SEAL or None in {
            registry_token,
            incarnation,
            nonce,
        }:
            raise TypeError("Product Action compensation proofs are issuer-created")
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(
            self,
            "_seal",
            (registry_token, incarnation, nonce),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action compensation proofs are sealed")

    def __repr__(self) -> str:
        return "<ReadinessSignalProductActionUndoProof>"

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("Product Action compensation proofs cannot be copied or serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        self._serialization_error()

    def __getstate__(self) -> NoReturn:
        self._serialization_error()

    def __copy__(self) -> NoReturn:
        self._serialization_error()

    def __deepcopy__(self, memo: dict[int, Any]) -> NoReturn:
        del memo
        self._serialization_error()


class InterviewStoryProductActionUndoProof:
    """Opaque, request-local evidence for one exact Story owner."""

    __slots__ = ("_registry_token", "_incarnation", "_nonce", "_seal", "__weakref__")
    _registry_token: object
    _incarnation: object
    _nonce: object
    _seal: tuple[object, object, object]

    def __new__(
        cls,
        construction_seal: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> "InterviewStoryProductActionUndoProof":
        if construction_seal is not _UNDO_PROOF_CONSTRUCTION_SEAL:
            raise TypeError("Product Action compensation proofs are issuer-created")
        return object.__new__(cls)

    def __init__(
        self,
        construction_seal: object | None = None,
        registry_token: object | None = None,
        incarnation: object | None = None,
        nonce: object | None = None,
    ) -> None:
        if construction_seal is not _UNDO_PROOF_CONSTRUCTION_SEAL or None in {
            registry_token,
            incarnation,
            nonce,
        }:
            raise TypeError("Product Action compensation proofs are issuer-created")
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(self, "_seal", (registry_token, incarnation, nonce))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action compensation proofs are sealed")

    def __repr__(self) -> str:
        return "<InterviewStoryProductActionUndoProof>"

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("Product Action compensation proofs cannot be copied or serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        self._serialization_error()

    def __getstate__(self) -> NoReturn:
        self._serialization_error()

    def __copy__(self) -> NoReturn:
        self._serialization_error()

    def __deepcopy__(self, memo: dict[int, Any]) -> NoReturn:
        del memo
        self._serialization_error()


@dataclass(frozen=True, slots=True, repr=False)
class _SignalUndoProofRecord:
    application_id: int
    signal_id: int
    expected_current_version_id: int
    expected_signal_revision: int
    parent_operation_id: str
    parent_action_name: str
    parent_terminal_payload_sha256: str
    compensation_kind: str
    validated_undo_json: MappingProxyType[str, JSONValue]
    owner_state: Literal["active", "terminal_committed", "terminal_failed"]
    active_aggregate_sha256: str
    owner_snapshot_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class _StoryUndoProofRecord:
    story_id: int
    source_attempt_id: int
    expected_current_version_id: int
    expected_story_revision: int
    parent_operation_id: str
    parent_action_name: str
    parent_terminal_payload_sha256: str
    compensation_kind: str
    validated_undo_json: MappingProxyType[str, JSONValue]
    owner_state: Literal["active", "terminal_committed", "terminal_failed"]
    created_version_snapshot_sha256: str
    previous_version_snapshot_sha256: str | None
    story_history_snapshot_sha256: str
    confirmed_effective_payload_sha256: str
    owner_snapshot_sha256: str


_UndoProofRecord = _SignalUndoProofRecord | _StoryUndoProofRecord
_UndoProof = ReadinessSignalProductActionUndoProof | InterviewStoryProductActionUndoProof


@dataclass(slots=True, repr=False)
class _LiveUndoProofRecord:
    proof: _UndoProof
    value: _UndoProofRecord
    state: Literal["issued", "in_flight"]


class _SignalUndoProofClaim(AbstractContextManager[_UndoProofRecord]):
    __slots__ = ("_registry", "_proof", "_record", "_closed")

    def __init__(
        self,
        registry: "ProductActionCompensationProofRegistryV1",
        proof: _UndoProof,
        record: _LiveUndoProofRecord,
    ) -> None:
        self._registry = registry
        self._proof = proof
        self._record = record
        self._closed = False

    def __enter__(self) -> _UndoProofRecord:
        if self._closed:
            raise ProductActionCompensationError(
                "product_action_compensation_proof_invalid",
                status_code=404,
            )
        return self._record.value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if self._closed:
            return
        self._closed = True
        self._registry._finish(
            self._proof,
            self._record,
            "consumed" if exc_type is None else "revoked",
        )


class ProductActionCompensationProofRegistryV1:
    """Request-local proof registry with exact once-only consumption."""

    __slots__ = (
        "_registry_token",
        "_incarnation",
        "_records",
        "_retired",
        "_lock",
        "_seal",
    )
    _registry_token: object
    _incarnation: object
    _records: dict[int, _LiveUndoProofRecord]
    _retired: WeakKeyDictionary[
        _UndoProof,
        Literal["consumed", "revoked"],
    ]
    _lock: RLock
    _seal: tuple[
        object,
        object,
        dict[int, _LiveUndoProofRecord],
        WeakKeyDictionary[
            _UndoProof,
            Literal["consumed", "revoked"],
        ],
        RLock,
    ]

    def __init__(self) -> None:
        registry_token = object()
        incarnation = object()
        records: dict[int, _LiveUndoProofRecord] = {}
        retired: WeakKeyDictionary[
            _UndoProof,
            Literal["consumed", "revoked"],
        ] = WeakKeyDictionary()
        lock = RLock()
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_retired", retired)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(
            self,
            "_seal",
            (registry_token, incarnation, records, retired, lock),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action compensation proof registry is sealed")

    def _ensure_integrity(self) -> None:
        if self._seal != (
            self._registry_token,
            self._incarnation,
            self._records,
            self._retired,
            self._lock,
        ):
            raise ProductActionIntegrityError("compensation_proof_registry_integrity")

    def _issue_signal(self, record: _SignalUndoProofRecord) -> ReadinessSignalProductActionUndoProof:
        self._ensure_integrity()
        if type(record) is not _SignalUndoProofRecord:
            raise TypeError("Signal Undo proof record is invalid")
        nonce = object()
        proof = ReadinessSignalProductActionUndoProof(
            _UNDO_PROOF_CONSTRUCTION_SEAL,
            self._registry_token,
            self._incarnation,
            nonce,
        )
        with self._lock:
            self._records[id(proof)] = _LiveUndoProofRecord(proof, record, "issued")
        return proof

    def _issue_story(self, record: _StoryUndoProofRecord) -> InterviewStoryProductActionUndoProof:
        self._ensure_integrity()
        if type(record) is not _StoryUndoProofRecord:
            raise TypeError("Story Undo proof record is invalid")
        nonce = object()
        proof = InterviewStoryProductActionUndoProof(
            _UNDO_PROOF_CONSTRUCTION_SEAL,
            self._registry_token,
            self._incarnation,
            nonce,
        )
        with self._lock:
            self._records[id(proof)] = _LiveUndoProofRecord(proof, record, "issued")
        return proof

    def _record(
        self,
        proof: object,
    ) -> _LiveUndoProofRecord:
        self._ensure_integrity()
        if type(proof) not in {
            ReadinessSignalProductActionUndoProof,
            InterviewStoryProductActionUndoProof,
        }:
            raise ProductActionCompensationError(
                "product_action_compensation_proof_invalid",
                status_code=404,
            )
        typed_proof = cast(_UndoProof, proof)
        try:
            if typed_proof._seal != (
                typed_proof._registry_token,
                typed_proof._incarnation,
                typed_proof._nonce,
            ) or (
                typed_proof._registry_token is not self._registry_token
                or typed_proof._incarnation is not self._incarnation
            ):
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
        except AttributeError as exc:
            raise ProductActionCompensationError(
                "product_action_compensation_proof_invalid",
                status_code=404,
            ) from exc
        record = self._records.get(id(typed_proof))
        if record is None or record.proof is not typed_proof:
            raise ProductActionCompensationError(
                "product_action_compensation_proof_invalid",
                status_code=404,
            )
        return record

    def peek_signal(
        self,
        proof: ReadinessSignalProductActionUndoProof,
    ) -> _SignalUndoProofRecord:
        with self._lock:
            record = self._record(proof)
            if record.state != "issued":
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
            if type(record.value) is not _SignalUndoProofRecord:
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
            return record.value

    def peek(self, proof: _UndoProof) -> _UndoProofRecord:
        with self._lock:
            record = self._record(proof)
            if record.state != "issued":
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
            return record.value

    def claim_signal(
        self,
        proof: ReadinessSignalProductActionUndoProof,
    ) -> _SignalUndoProofClaim:
        with self._lock:
            record = self._record(proof)
            if record.state != "issued":
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
            record.state = "in_flight"
            return _SignalUndoProofClaim(self, proof, record)

    def claim(self, proof: _UndoProof) -> _SignalUndoProofClaim:
        with self._lock:
            record = self._record(proof)
            if record.state != "issued":
                raise ProductActionCompensationError(
                    "product_action_compensation_proof_invalid",
                    status_code=404,
                )
            record.state = "in_flight"
            return _SignalUndoProofClaim(self, proof, record)

    def _finish(
        self,
        proof: _UndoProof,
        record: _LiveUndoProofRecord,
        state: Literal["consumed", "revoked"],
    ) -> None:
        with self._lock:
            if record.state != "in_flight" or self._records.pop(id(proof), None) is not record:
                raise ProductActionIntegrityError("compensation_proof_registry_integrity")
            self._retired[proof] = state

    def revoke(self, proof: object) -> None:
        if type(proof) not in {
            ReadinessSignalProductActionUndoProof,
            InterviewStoryProductActionUndoProof,
        }:
            return
        typed_proof = cast(_UndoProof, proof)
        self._ensure_integrity()
        with self._lock:
            record = self._records.get(id(typed_proof))
            if record is not None and record.proof is typed_proof:
                self._records.pop(id(typed_proof))
                self._retired[typed_proof] = "revoked"


def _decode_json_object(raw: str | None, code: str) -> dict[str, JSONValue]:
    if raw is None:
        raise ProductActionIntegrityError(code)
    try:
        def reject_duplicates(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
            value: dict[str, JSONValue] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON member")
                value[key] = item
            return value

        def reject_constant(_value: str) -> NoReturn:
            raise ValueError("non-finite JSON number")

        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(code) from exc
    if type(value) is not dict:
        raise ProductActionIntegrityError(code)
    if canonical_product_action_json(cast(JSONValue, value)) != raw:
        raise ProductActionIntegrityError(code)
    return cast(dict[str, JSONValue], value)


def _validate_signal_parent(
    parent: WriteOperation | None,
) -> tuple[str, dict[str, JSONValue], dict[str, JSONValue]]:
    if (
        parent is None
        or parent.operation_role != "primary"
        or parent.adapter_kind != "product_action"
        or parent.tool_name != "save_review_readiness_signal"
        or parent.status != "committed"
        or parent.conversation_id is not None
        or parent.agent_run_id is not None
        or parent.undo_json is None
    ):
        raise ProductActionCompensationError(
            "product_action_compensation_not_found",
            status_code=404,
        )
    try:
        payload = payload_from_operation(parent)
    except Exception as exc:
        raise ProductActionIntegrityError("product_action_parent_terminal_digest") from exc
    result = _decode_json_object(parent.result_json, "product_action_parent_result")
    undo = _decode_json_object(parent.undo_json, "product_action_parent_undo")
    if set(result) != {
        "schema_version",
        "action_name",
        "outcome",
        "signal_id",
        "signal_version_id",
        "signal_revision",
        "source_status",
    } or (
        type(result.get("schema_version")) is not int
        or result.get("schema_version") != 1
        or result.get("action_name") != "save_review_readiness_signal"
        or result.get("outcome") != "created"
        or result.get("source_status") != "current"
        or type(result.get("signal_id")) is not int
        or cast(int, result.get("signal_id")) < 1
        or type(result.get("signal_version_id")) is not int
        or cast(int, result.get("signal_version_id")) < 1
        or type(result.get("signal_revision")) is not int
        or cast(int, result.get("signal_revision")) < 1
    ):
        raise ProductActionIntegrityError("product_action_parent_result")
    if set(undo) != {
        "kind",
        "signal_id",
        "created_version_id",
        "expected_current_version_id",
        "expected_signal_revision",
        "parent_operation_id",
    } or (
        undo.get("kind") != "retract_review_readiness_signal_v1"
        or type(undo.get("signal_id")) is not int
        or undo.get("signal_id") != result.get("signal_id")
        or type(undo.get("created_version_id")) is not int
        or undo.get("created_version_id") != result.get("signal_version_id")
        or type(undo.get("expected_current_version_id")) is not int
        or undo.get("expected_current_version_id") != result.get("signal_version_id")
        or type(undo.get("expected_signal_revision")) is not int
        or undo.get("expected_signal_revision") != result.get("signal_revision")
        or undo.get("parent_operation_id") != parent.id
    ):
        raise ProductActionIntegrityError("product_action_parent_undo")
    return payload.digest, undo, result


def _signal_version_snapshot(
    session: Session,
    *,
    signal: InterviewReadinessSignal,
    version_id: int,
    expected_disposition: Literal["active", "retracted"],
    expected_write_operation_id: str,
    expected_parent_version_id: int | None,
    expected_version_number: int | None = None,
) -> tuple[InterviewReadinessSignalVersion, tuple[InterviewReadinessSignalEvidence, ...], dict[str, JSONValue]]:
    version = session.get(InterviewReadinessSignalVersion, version_id)
    if (
        version is None
        or version.signal_id != signal.id
        or type(version.version_number) is not int
        or version.version_number < 1
        or (
            expected_version_number is not None
            and version.version_number != expected_version_number
        )
        or version.parent_version_id != expected_parent_version_id
        or version.disposition != expected_disposition
        or version.schema_version != "readiness-signal-v1"
        or version.write_operation_id != expected_write_operation_id
        or type(version.source_note_revision) is not int
        or version.source_note_revision < 1
        or type(version.statement_text) is not str
        or len(version.statement_text.encode("utf-8")) > 4_096
        or type(version.user_note) is not str
        or len(version.user_note.encode("utf-8")) > 2_048
    ):
        raise ProductActionIntegrityError("readiness_signal_version_integrity")
    for field in (
        "source_note_fingerprint",
        "source_proposal_hash",
        "candidate_fingerprint",
    ):
        value = getattr(version, field)
        if (
            type(value) is not str
            or len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ProductActionIntegrityError("readiness_signal_version_integrity")
    try:
        domain_key = str(UUID(version.domain_idempotency_key))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(
            "readiness_signal_version_integrity"
        ) from exc
    if domain_key != version.domain_idempotency_key:
        raise ProductActionIntegrityError("readiness_signal_version_integrity")
    evidence = tuple(
        session.scalars(
            select(InterviewReadinessSignalEvidence)
            .where(InterviewReadinessSignalEvidence.signal_version_id == version.id)
            .order_by(InterviewReadinessSignalEvidence.ordinal, InterviewReadinessSignalEvidence.id)
        )
    )
    if not 1 <= len(evidence) <= 5 or tuple(row.ordinal for row in evidence) != tuple(
        range(len(evidence))
    ):
        raise ProductActionIntegrityError("readiness_signal_evidence_prefix")
    total_excerpt_bytes = 0
    projected_evidence: list[JSONValue] = []
    for row in evidence:
        if row.source_path not in {
            "/questions",
            "/self_reflection",
            "/difficulty_points",
            "/mood",
        } or type(row.excerpt) is not str:
            raise ProductActionIntegrityError("readiness_signal_evidence_integrity")
        excerpt = row.excerpt.encode("utf-8")
        total_excerpt_bytes += len(excerpt)
        if len(excerpt) > 8_192 or total_excerpt_bytes > 16_384:
            raise ProductActionIntegrityError("readiness_signal_evidence_integrity")
        expected_excerpt = "sha256:" + hashlib.sha256(excerpt).hexdigest()
        if row.excerpt_sha256 != expected_excerpt:
            raise ProductActionIntegrityError("readiness_signal_evidence_integrity")
        source_hash = row.source_field_sha256
        if (
            type(source_hash) is not str
            or len(source_hash) != 71
            or not source_hash.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in source_hash[7:])
        ):
            raise ProductActionIntegrityError("readiness_signal_evidence_integrity")
        projected_evidence.append(
            {
                "ordinal": row.ordinal,
                "source_path": row.source_path,
                "excerpt": row.excerpt,
                "excerpt_sha256": row.excerpt_sha256,
                "source_field_sha256": row.source_field_sha256,
            }
        )
    projected: dict[str, JSONValue] = {
        "id": version.id,
        "signal_id": version.signal_id,
        "version_number": version.version_number,
        "parent_version_id": version.parent_version_id,
        "disposition": version.disposition,
        "schema_version": version.schema_version,
        "statement_text": version.statement_text,
        "user_note": version.user_note,
        "source_note_revision": version.source_note_revision,
        "source_note_fingerprint": version.source_note_fingerprint,
        "source_proposal_hash": version.source_proposal_hash,
        "candidate_fingerprint": version.candidate_fingerprint,
        "domain_idempotency_key": version.domain_idempotency_key,
        "write_operation_id": version.write_operation_id,
        "evidence": projected_evidence,
    }
    return version, evidence, projected


def _signal_owner_snapshot_sha256(
    signal: InterviewReadinessSignal,
    *versions: dict[str, JSONValue],
) -> str:
    payload: dict[str, JSONValue] = {
        "signal": {
            "id": signal.id,
            "application_id": signal.application_id,
            "focus_id": signal.focus_id,
            "current_version_id": signal.current_version_id,
            "revision": signal.revision,
        },
        "versions": list(versions),
    }
    raw = canonical_product_action_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _signal_active_aggregate_sha256(
    signal: InterviewReadinessSignal,
    active_version: dict[str, JSONValue],
) -> str:
    payload: dict[str, JSONValue] = {
        "owner": {
            "signal_id": signal.id,
            "application_id": signal.application_id,
            "focus_id": signal.focus_id,
        },
        "active_version": active_version,
    }
    raw = canonical_product_action_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_legitimate_signal_drift(
    session: Session,
    *,
    signal: InterviewReadinessSignal,
    active: InterviewReadinessSignalVersion,
    original_revision: int,
) -> bool:
    """Distinguish an exact later edit from corruption disguised as CAS drift."""

    if type(signal.revision) is not int or signal.revision < 1:
        raise ProductActionIntegrityError("readiness_signal_owner_integrity")
    versions = tuple(
        session.scalars(
            select(InterviewReadinessSignalVersion)
            .where(InterviewReadinessSignalVersion.signal_id == signal.id)
            .order_by(
                InterviewReadinessSignalVersion.version_number,
                InterviewReadinessSignalVersion.id,
            )
        )
    )
    if signal.current_version_id == active.id:
        if versions != (active,):
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_cardinality"
            )
        if signal.revision == original_revision:
            return False
        if signal.revision <= original_revision:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_revision"
            )
        return True
    if (
        signal.current_version_id is None
        or signal.revision < original_revision
        or len(versions) < 2
        or versions[0] is not active
        or versions[-1].id != signal.current_version_id
    ):
        raise ProductActionIntegrityError(
            "product_action_compensation_owner_drift"
        )
    previous = active
    for expected_number, version in enumerate(versions[1:], start=active.version_number + 1):
        if type(version.write_operation_id) is not str:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_drift"
            )
        _canonical_uuid(
            version.write_operation_id,
            "readiness_signal_later_write_operation_id",
        )
        _signal_version_snapshot(
            session,
            signal=signal,
            version_id=version.id,
            expected_disposition="active",
            expected_write_operation_id=version.write_operation_id,
            expected_parent_version_id=previous.id,
            expected_version_number=expected_number,
        )
        previous = version
    return True


def _validate_retracted_copy(
    *,
    active: InterviewReadinessSignalVersion,
    active_evidence: tuple[InterviewReadinessSignalEvidence, ...],
    retracted: InterviewReadinessSignalVersion,
    retracted_evidence: tuple[InterviewReadinessSignalEvidence, ...],
) -> None:
    if (
        retracted.statement_text != active.statement_text
        or retracted.user_note != active.user_note
        or retracted.source_note_revision != active.source_note_revision
        or retracted.source_note_fingerprint != active.source_note_fingerprint
        or retracted.source_proposal_hash != active.source_proposal_hash
        or retracted.candidate_fingerprint != active.candidate_fingerprint
        or len(retracted_evidence) != len(active_evidence)
        or any(
            (
                child.ordinal,
                child.source_path,
                child.excerpt,
                child.excerpt_sha256,
                child.source_field_sha256,
            )
            != (
                parent.ordinal,
                parent.source_path,
                parent.excerpt,
                parent.excerpt_sha256,
                parent.source_field_sha256,
            )
            for parent, child in zip(active_evidence, retracted_evidence, strict=True)
        )
    ):
        raise ProductActionIntegrityError("readiness_signal_retraction_copy")


def _validate_parent_transition_prefix(
    session: Session,
    parent: WriteOperation,
) -> None:
    rows = tuple(
        session.scalars(
            select(WriteOperationTransition)
            .where(WriteOperationTransition.operation_id == parent.id)
            .order_by(WriteOperationTransition.seq, WriteOperationTransition.id)
        )
    )
    prefix = tuple((row.seq, row.state) for row in rows)
    if (
        any(type(row.seq) is not int for row in rows)
        or EXPECTED_PREFIX.get(parent.status) != prefix
    ):
        raise ProductActionIntegrityError("product_action_parent_transition_prefix")


class ReadinessSignalUndoIssuer:
    """Issue one owner-bound Signal Undo proof after capability short-circuit."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        catalog: ProductActionCompensationCatalogV1,
        proof_registry: ProductActionCompensationProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
        capability_check: Callable[[str], bool],
    ) -> None:
        if (
            not callable(session_factory)
            or type(catalog) is not ProductActionCompensationCatalogV1
            or type(proof_registry) is not ProductActionCompensationProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or not callable(capability_check)
        ):
            raise TypeError("Readiness Signal Undo Issuer composition is invalid")
        self._session_factory = session_factory
        self._catalog = catalog
        self._proof_registry = proof_registry
        self._key_profiles = key_profiles
        self._capability_check = capability_check

    def issue(
        self,
        *,
        application_id: int,
        signal_id: int,
        parent_operation_id: str,
    ) -> ReadinessSignalProductActionUndoProof:
        if not self._capability_check(
            "application.interview_readiness_feedback.write"
        ):
            raise ProductActionCompensationError(
                "product_action_compensation_permission_denied",
                status_code=403,
            )
        if type(application_id) is not int or application_id < 1:
            raise ProductActionCompensationError(
                "product_action_compensation_not_found",
                status_code=404,
            )
        if type(signal_id) is not int or signal_id < 1:
            raise ProductActionCompensationError(
                "product_action_compensation_not_found",
                status_code=404,
            )
        parent_operation_id = _canonical_uuid(
            parent_operation_id,
            "parent_operation_id",
        )
        with self._session_factory() as session:
            signal = session.get(InterviewReadinessSignal, signal_id)
            if (
                signal is None
                or signal.application_id != application_id
                or signal.current_version_id is None
                or type(signal.revision) is not int
            ):
                raise ProductActionCompensationError(
                    "product_action_compensation_not_found",
                    status_code=404,
                )
            parent = session.get(WriteOperation, parent_operation_id)
            if parent is not None:
                _validate_parent_transition_prefix(session, parent)
            parent_digest, undo, parent_result = _validate_signal_parent(parent)
            if parent_result["signal_id"] != signal.id:
                raise ProductActionCompensationError(
                    "product_action_compensation_not_found",
                    status_code=404,
                )
            original_version_id = cast(int, undo["expected_current_version_id"])
            original_revision = cast(int, undo["expected_signal_revision"])
            active, active_evidence, active_projection = _signal_version_snapshot(
                session,
                signal=signal,
                version_id=original_version_id,
                expected_disposition="active",
                expected_write_operation_id=parent_operation_id,
                expected_parent_version_id=None,
                expected_version_number=1,
            )
            operation_id = product_action_compensation_operation_id(
                parent_operation_id,
                "undo:save_review_readiness_signal",
            )
            compensation = session.get(WriteOperation, operation_id)
            owner_state: Literal[
                "active", "terminal_committed", "terminal_failed"
            ]
            drifted = (
                True
                if compensation is not None and compensation.status == "committed"
                else _validate_legitimate_signal_drift(
                    session,
                    signal=signal,
                    active=active,
                    original_revision=original_revision,
                )
            )
            if compensation is not None and compensation.status == "failed":
                if not drifted:
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                owner_state = "terminal_failed"
                expected_current = signal.current_version_id
                expected_revision = signal.revision
                owner_snapshot = _signal_owner_snapshot_sha256(
                    signal,
                    active_projection,
                )
            elif not drifted:
                if compensation is not None and compensation.status == "committed":
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                expected_current = original_version_id
                expected_revision = original_revision
                owner_state = "active"
                owner_snapshot = _signal_owner_snapshot_sha256(
                    signal,
                    active_projection,
                )
            else:
                if compensation is None:
                    raise ProductActionCompensationError(
                        "product_action_compensation_not_found",
                        status_code=404,
                    )
                if compensation.status != "committed":
                    raise ProductActionIntegrityError(
                        "product_action_compensation_orphan_domain"
                    )
                result = _decode_json_object(
                    compensation.result_json,
                    "product_action_compensation_result",
                )
                if (
                    set(result) != {
                        "kind",
                        "signal_id",
                        "retracted_version_id",
                        "signal_revision",
                    }
                    or result.get("kind")
                    != "review_readiness_signal_retracted_v1"
                    or result.get("signal_id") != signal.id
                    or type(result.get("retracted_version_id")) is not int
                    or result.get("retracted_version_id") != signal.current_version_id
                    or result.get("signal_revision") != signal.revision
                    or signal.revision != original_revision + 1
                ):
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                retracted, retracted_evidence, retracted_projection = (
                    _signal_version_snapshot(
                        session,
                        signal=signal,
                        version_id=signal.current_version_id,
                        expected_disposition="retracted",
                        expected_write_operation_id=operation_id,
                        expected_parent_version_id=original_version_id,
                        expected_version_number=active.version_number + 1,
                    )
                )
                if retracted.domain_idempotency_key != readiness_signal_retraction_domain_key(
                    operation_id
                ):
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                _validate_retracted_copy(
                    active=active,
                    active_evidence=active_evidence,
                    retracted=retracted,
                    retracted_evidence=retracted_evidence,
                )
                owner_state = "terminal_committed"
                expected_current = signal.current_version_id
                expected_revision = signal.revision
                owner_snapshot = _signal_owner_snapshot_sha256(
                    signal,
                    active_projection,
                    retracted_projection,
                )
            record = _SignalUndoProofRecord(
                application_id=application_id,
                signal_id=signal.id,
                expected_current_version_id=expected_current,
                expected_signal_revision=expected_revision,
                parent_operation_id=parent_operation_id,
                parent_action_name="save_review_readiness_signal",
                parent_terminal_payload_sha256=parent_digest,
                compensation_kind="undo:save_review_readiness_signal",
                validated_undo_json=MappingProxyType(dict(undo)),
                owner_state=owner_state,
                active_aggregate_sha256=_signal_active_aggregate_sha256(
                    signal,
                    active_projection,
                ),
                owner_snapshot_sha256=owner_snapshot,
            )
        return self._proof_registry._issue_signal(record)


def _validate_story_parent(
    parent: WriteOperation | None,
) -> tuple[str, dict[str, JSONValue], dict[str, JSONValue]]:
    if (
        parent is None
        or parent.operation_role != "primary"
        or parent.adapter_kind != "product_action"
        or parent.tool_name != "confirm_interview_story"
        or parent.status != "committed"
        or parent.conversation_id is not None
        or parent.agent_run_id is not None
        or parent.undo_json is None
    ):
        raise ProductActionCompensationError(
            "product_action_compensation_not_found",
            status_code=404,
        )
    try:
        payload = payload_from_operation(parent)
    except Exception as exc:
        raise ProductActionIntegrityError("product_action_parent_terminal_digest") from exc
    result = _decode_json_object(parent.result_json, "product_action_parent_result")
    undo = _decode_json_object(parent.undo_json, "product_action_parent_undo")
    if set(result) != {
        "schema_version",
        "action_name",
        "outcome",
        "story_id",
        "story_version_id",
        "story_revision",
    } or (
        type(result.get("schema_version")) is not int
        or result.get("schema_version") != 1
        or result.get("action_name") != "confirm_interview_story"
        or result.get("outcome") not in {"created", "version_appended"}
        or type(result.get("story_id")) is not int
        or cast(int, result.get("story_id")) < 1
        or type(result.get("story_version_id")) is not int
        or cast(int, result.get("story_version_id")) < 1
        or type(result.get("story_revision")) is not int
        or cast(int, result.get("story_revision")) < 1
    ):
        raise ProductActionIntegrityError("product_action_parent_result")
    story_id = cast(int, result["story_id"])
    version_id = cast(int, result["story_version_id"])
    revision = cast(int, result["story_revision"])
    if result["outcome"] == "created":
        if set(undo) != {
            "kind",
            "story_id",
            "created_version_id",
            "expected_current_version_id",
            "expected_story_revision",
            "expected_status",
        } or (
            undo.get("kind") != "archive_created_story_v1"
            or type(undo.get("story_id")) is not int
            or cast(int, undo.get("story_id")) < 1
            or type(undo.get("created_version_id")) is not int
            or cast(int, undo.get("created_version_id")) < 1
            or type(undo.get("expected_current_version_id")) is not int
            or cast(int, undo.get("expected_current_version_id")) < 1
            or type(undo.get("expected_story_revision")) is not int
            or cast(int, undo.get("expected_story_revision")) != 1
            or undo.get("story_id") != story_id
            or undo.get("created_version_id") != version_id
            or undo.get("expected_current_version_id") != version_id
            or undo.get("expected_story_revision") != revision
            or undo.get("expected_status") != "active"
            or revision != 1
        ):
            raise ProductActionIntegrityError("product_action_parent_undo")
    elif set(undo) != {
        "kind",
        "story_id",
        "created_version_id",
        "previous_current_version_id",
        "previous_title",
        "expected_post_revision",
    } or (
        undo.get("kind") != "restore_story_pointer_v1"
        or type(undo.get("story_id")) is not int
        or cast(int, undo.get("story_id")) < 1
        or type(undo.get("created_version_id")) is not int
        or cast(int, undo.get("created_version_id")) < 1
        or undo.get("story_id") != story_id
        or undo.get("created_version_id") != version_id
        or type(undo.get("previous_current_version_id")) is not int
        or cast(int, undo.get("previous_current_version_id")) < 1
        or undo.get("previous_current_version_id") == version_id
        or type(undo.get("previous_title")) is not str
        or not cast(str, undo.get("previous_title")).strip()
        or len(cast(str, undo.get("previous_title"))) > 200
        or type(undo.get("expected_post_revision")) is not int
        or cast(int, undo.get("expected_post_revision")) < 2
        or undo.get("expected_post_revision") != revision
        or revision < 2
    ):
        raise ProductActionIntegrityError("product_action_parent_undo")
    return payload.digest, undo, result


def _story_attempt_lineage(
    session: Session,
    *,
    parent_operation_id: str,
    story_id: int,
    version_id: int,
    outcome: str,
) -> InterviewStoryProposalAttempt:
    attempts = tuple(
        session.scalars(
            select(InterviewStoryProposalAttempt).where(
                InterviewStoryProposalAttempt.product_action_operation_id
                == parent_operation_id
            )
        )
    )
    if len(attempts) != 1:
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    attempt = attempts[0]
    if (
        attempt.attempt_status != "confirmed"
        or attempt.product_action_operation_id != parent_operation_id
        or type(attempt.product_action_generation) is not int
        or attempt.product_action_generation < 1
        or attempt.confirmed_story_id != story_id
        or attempt.confirmed_story_version_id != version_id
        or attempt.confirmed_at is None
        or type(attempt.confirmation_payload_hash) is not str
        or len(attempt.confirmation_payload_hash) != 71
        or not attempt.confirmation_payload_hash.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in attempt.confirmation_payload_hash[7:]
        )
        or (outcome == "created" and attempt.target_story_id is not None)
        or (outcome == "version_appended" and attempt.target_story_id != story_id)
    ):
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    _story_datetime_utc(
        attempt.confirmed_at,
        "interview_story_undo_attempt_lineage",
    )
    return attempt


def _manual_story_content_from_canonical(
    content: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    title = cast(dict[str, JSONValue], content["title"])
    blocks = cast(list[dict[str, JSONValue]], content["blocks"])
    labels = cast(list[dict[str, JSONValue]], content["capability_labels"])
    questions = cast(
        list[dict[str, JSONValue]],
        content["applicable_questions"],
    )
    return {
        "title": title["text"],
        "blocks": [
            {
                "kind": block["kind"],
                "text": block["text"],
                "fact_mode": block["fact_mode"],
            }
            for block in blocks
        ],
        "capability_labels": [item["text"] for item in labels],
        "applicable_questions": [item["text"] for item in questions],
        "fact_gap_codes": content["fact_gap_codes"],
    }


def _story_version_snapshot(
    session: Session,
    *,
    story_id: int,
    version_id: int,
    expected_origin_kind: str | None = None,
) -> tuple[InterviewStoryVersion, dict[str, JSONValue]]:
    version = session.get(InterviewStoryVersion, version_id)
    if (
        version is None
        or version.story_id != story_id
        or type(version.version_number) is not int
        or version.version_number < 1
        or version.origin_kind not in _STORY_VERSION_ORIGIN_KINDS
        or (
            expected_origin_kind is not None
            and version.origin_kind != expected_origin_kind
        )
        or not isinstance(version.confirmed_at, datetime)
        or type(version.content_json) is not str
        or len(version.content_json.encode("utf-8")) > 65_536
        or type(version.content_hash) is not str
        or version.content_hash
        != hashlib.sha256(version.content_json.encode("utf-8")).hexdigest()
        or type(version.source_fingerprint) is not str
        or len(version.source_fingerprint.removeprefix("sha256:")) != 64
        or any(
            character not in "0123456789abcdef"
            for character in version.source_fingerprint.removeprefix("sha256:")
        )
    ):
        raise ProductActionIntegrityError("interview_story_undo_version_integrity")
    _story_datetime_utc(
        version.confirmed_at,
        "interview_story_undo_version_integrity",
    )
    content = _decode_json_object(
        version.content_json,
        "interview_story_undo_version_content",
    )
    try:
        from offerpilot.repositories.interview_stories import canonical_story_content

        raw_content = _manual_story_content_from_canonical(content)
        if canonical_story_content(raw_content) != content:
            raise ValueError("Story content is not canonical")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_version_content"
        ) from exc
    links = tuple(
        session.scalars(
            select(InterviewStoryVersionEvidenceLink)
            .where(InterviewStoryVersionEvidenceLink.story_version_id == version.id)
            .order_by(InterviewStoryVersionEvidenceLink.id)
        )
    )
    assertions = tuple(
        session.scalars(
            select(InterviewStoryUserAssertion)
            .where(InterviewStoryUserAssertion.story_version_id == version.id)
            .order_by(InterviewStoryUserAssertion.id)
        )
    )
    blocks = cast(list[dict[str, JSONValue]], content["blocks"])
    labels = cast(list[dict[str, JSONValue]], content["capability_labels"])
    questions = cast(
        list[dict[str, JSONValue]],
        content["applicable_questions"],
    )
    expected_targets = {
        ("title", "title"),
        *(("block", cast(str, block["id"])) for block in blocks),
        *(("capability_label", cast(str, item["id"])) for item in labels),
        *(("applicable_question", cast(str, item["id"])) for item in questions),
    }
    assertions_by_id = {str(assertion.id): assertion for assertion in assertions}
    assertion_ids = set(assertions_by_id)
    if not 1 <= len(links) <= len(expected_targets) * 5:
        raise ProductActionIntegrityError("interview_story_undo_evidence_integrity")
    projected_links: list[JSONValue] = []
    linked_targets: set[tuple[str, str]] = set()
    link_identities: set[tuple[str, ...]] = set()
    for link in links:
        identity: dict[str, JSONValue] = {
            "target_kind": link.target_kind,
            "target_id": link.target_id,
            "source_kind": link.source_kind,
            "source_stable_id": link.source_stable_id,
            "source_version_or_snapshot": link.source_version_or_snapshot,
            "source_path": link.source_path,
            "text_location": link.text_location,
            "excerpt": link.excerpt,
            "source_fingerprint": link.source_fingerprint,
        }
        if (
            type(link.id) is not int
            or link.id < 1
            or link.story_version_id != version.id
            or any(type(value) is not str for value in identity.values())
            or len(link.excerpt.encode("utf-8")) > 8_192
            or (link.target_kind, link.target_id) not in expected_targets
            or (
                link.source_kind == "user_assertion"
                and link.source_stable_id not in assertion_ids
            )
            or (
                link.source_kind == "user_assertion"
                and link.excerpt
                not in assertions_by_id[link.source_stable_id].statement_text
            )
            or link.link_hash
            != hashlib.sha256(
                canonical_product_action_json(identity).encode("utf-8")
            ).hexdigest()
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_evidence_integrity"
            )
        link_identity = tuple(cast(str, value) for value in identity.values())
        if link_identity in link_identities:
            raise ProductActionIntegrityError(
                "interview_story_undo_evidence_integrity"
            )
        link_identities.add(link_identity)
        linked_targets.add((link.target_kind, link.target_id))
        projected_links.append(
            {
                "id": link.id,
                "story_version_id": link.story_version_id,
                **identity,
                "link_hash": link.link_hash,
            }
        )
    if linked_targets != expected_targets:
        raise ProductActionIntegrityError("interview_story_undo_evidence_integrity")
    projected_assertions: list[JSONValue] = []
    for assertion in assertions:
        if (
            type(assertion.id) is not int
            or assertion.id < 1
            or assertion.story_version_id != version.id
            or not isinstance(assertion.confirmed_at, datetime)
            or type(assertion.statement_text) is not str
            or len(assertion.statement_text.encode("utf-8")) > 16_384
            or assertion.statement_hash
            != hashlib.sha256(assertion.statement_text.encode("utf-8")).hexdigest()
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_assertion_integrity"
            )
        _story_datetime_utc(
            assertion.confirmed_at,
            "interview_story_undo_assertion_integrity",
        )
        projected_assertions.append(
            {
                "id": assertion.id,
                "story_version_id": assertion.story_version_id,
                "statement_text": assertion.statement_text,
                "statement_hash": assertion.statement_hash,
                "confirmed_at": assertion.confirmed_at.isoformat(),
            }
        )
    projected: dict[str, JSONValue] = {
        "id": version.id,
        "story_id": version.story_id,
        "version_number": version.version_number,
        "content": content,
        "content_hash": version.content_hash,
        "source_fingerprint": version.source_fingerprint,
        "origin_kind": version.origin_kind,
        "confirmed_at": version.confirmed_at.isoformat(),
        "evidence_links": projected_links,
        "assertions": projected_assertions,
    }
    return version, projected


def _story_version_snapshot_sha256(projected: dict[str, JSONValue]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_product_action_json(projected).encode("utf-8")
    ).hexdigest()


def _story_effective_payload_sha256(
    projected: dict[str, JSONValue],
    undo: dict[str, JSONValue],
) -> str:
    content = cast(dict[str, JSONValue], projected["content"])
    assertions = cast(list[dict[str, JSONValue]], projected["assertions"])
    links = cast(list[dict[str, JSONValue]], projected["evidence_links"])
    assertion_tokens = {
        str(assertion["id"]): f"assertion_{ordinal:03d}"
        for ordinal, assertion in enumerate(assertions, 1)
    }
    client_links: list[JSONValue] = []
    for link in links:
        source_stable_id = cast(str, link["source_stable_id"])
        if link["source_kind"] == "user_assertion":
            source_stable_id = assertion_tokens.get(source_stable_id, "")
            if not source_stable_id:
                raise ProductActionIntegrityError(
                    "interview_story_undo_assertion_integrity"
                )
        client_links.append(
            {
                "target_kind": link["target_kind"],
                "target_id": link["target_id"],
                "source_kind": link["source_kind"],
                "source_stable_id": source_stable_id,
                "source_version_or_snapshot": link["source_version_or_snapshot"],
                "source_path": link["source_path"],
                "excerpt": link["excerpt"],
                "text_location": link["text_location"],
            }
        )
    if undo["kind"] == "archive_created_story_v1":
        expected_current_version_id: JSONValue = None
        expected_story_revision: JSONValue = None
    else:
        expected_current_version_id = undo["previous_current_version_id"]
        expected_story_revision = cast(int, undo["expected_post_revision"]) - 1
    payload: dict[str, JSONValue] = {
        "content": _manual_story_content_from_canonical(content),
        "evidence_links": client_links,
        "expected_current_version_id": expected_current_version_id,
        "expected_story_revision": expected_story_revision,
    }
    return "sha256:" + hashlib.sha256(
        canonical_product_action_json(payload).encode("utf-8")
    ).hexdigest()


def _validate_story_confirmation_payload(
    attempt: InterviewStoryProposalAttempt,
    projected: dict[str, JSONValue],
    undo: dict[str, JSONValue],
) -> str:
    input_payload = _decode_json_object(
        attempt.input_snapshot_json,
        "interview_story_undo_attempt_input",
    )
    input_assertions = input_payload.get("assertions")
    projected_assertions = cast(
        list[dict[str, JSONValue]],
        projected["assertions"],
    )
    if (
        type(input_assertions) is not list
        or any(type(item) is not str for item in input_assertions)
        or input_assertions
        != [item["statement_text"] for item in projected_assertions]
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_assertion_integrity"
        )
    expected = _story_effective_payload_sha256(projected, undo)
    if not hmac.compare_digest(attempt.confirmation_payload_hash, expected):
        raise ProductActionIntegrityError(
            "interview_story_undo_confirmation_payload"
        )
    return expected


def _story_history_snapshot_sha256(
    session: Session,
    *,
    story_id: int,
    created_version_id: int,
    previous_version_id: int | None,
) -> str:
    created = session.get(InterviewStoryVersion, created_version_id)
    if created is None or created.story_id != story_id:
        raise ProductActionIntegrityError("interview_story_undo_history_integrity")
    versions = tuple(
        session.scalars(
            select(InterviewStoryVersion)
            .where(
                InterviewStoryVersion.story_id == story_id,
                InterviewStoryVersion.version_number <= created.version_number,
            )
            .order_by(InterviewStoryVersion.version_number)
        )
    )
    if (
        not versions
        or tuple(version.version_number for version in versions)
        != tuple(range(1, len(versions) + 1))
        or versions[-1].id != created.id
        or any(
            type(version.id) is not int
            or version.id < 1
            or version.story_id != story_id
            or type(version.content_hash) is not str
            or type(version.source_fingerprint) is not str
            or version.origin_kind not in _STORY_VERSION_ORIGIN_KINDS
            or not isinstance(version.confirmed_at, datetime)
            for version in versions
        )
    ):
        raise ProductActionIntegrityError("interview_story_undo_history_integrity")
    if previous_version_id is not None:
        positions = {
            version.id: version.version_number
            for version in versions
        }
        if (
            previous_version_id not in positions
            or positions[previous_version_id] >= positions[created_version_id]
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_pointer"
            )
    projected: dict[str, JSONValue] = {
        "story_id": story_id,
        "created_version_id": created_version_id,
        "previous_version_id": previous_version_id,
        "versions": [
            {
                "id": version.id,
                "version_number": version.version_number,
                "content_hash": version.content_hash,
                "source_fingerprint": version.source_fingerprint,
                "origin_kind": version.origin_kind,
                "confirmed_at": version.confirmed_at.isoformat(),
            }
            for version in versions
        ],
    }
    return "sha256:" + hashlib.sha256(
        canonical_product_action_json(projected).encode("utf-8")
    ).hexdigest()


def _story_datetime_utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime):
        raise ProductActionIntegrityError(code)
    try:
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
    except (OverflowError, ValueError) as exc:
        raise ProductActionIntegrityError(code) from exc
    if (
        normalized < _STORY_TIME_FLOOR
        or normalized > datetime.now(timezone.utc) + _STORY_TIME_FUTURE_SKEW
    ):
        raise ProductActionIntegrityError(code)
    return normalized


def _validate_story_attempt_version_time(
    attempt: InterviewStoryProposalAttempt,
    version: InterviewStoryVersion,
) -> None:
    attempt_time = _story_datetime_utc(
        attempt.confirmed_at,
        "interview_story_undo_attempt_lineage",
    )
    version_time = _story_datetime_utc(
        version.confirmed_at,
        "interview_story_undo_version_integrity",
    )
    if attempt_time < version_time or attempt_time - version_time > timedelta(minutes=5):
        raise ProductActionIntegrityError(
            "interview_story_undo_attempt_lineage"
        )


def _validate_story_lifecycle_timestamps(story: InterviewStory) -> None:
    created_at = _story_datetime_utc(
        story.created_at,
        "interview_story_undo_lifecycle",
    )
    updated_at = _story_datetime_utc(
        story.updated_at,
        "interview_story_undo_lifecycle",
    )
    if updated_at < created_at or story.status not in {"active", "archived"}:
        raise ProductActionIntegrityError("interview_story_undo_lifecycle")
    if story.status == "active":
        if story.archived_at is not None:
            raise ProductActionIntegrityError("interview_story_undo_lifecycle")
        return
    archived_at = _story_datetime_utc(
        story.archived_at,
        "interview_story_undo_lifecycle",
    )
    if archived_at < created_at or archived_at > updated_at:
        raise ProductActionIntegrityError("interview_story_undo_lifecycle")


def _story_projected_source_lineage(
    projected: dict[str, JSONValue],
) -> tuple[
    list[dict[str, JSONValue]],
    list[dict[str, JSONValue]],
    list[str],
    list[dict[str, JSONValue]],
]:
    links = cast(list[dict[str, JSONValue]], projected["evidence_links"])
    assertions = cast(list[dict[str, JSONValue]], projected["assertions"])
    assertion_tokens = {
        str(assertion["id"]): f"assertion_{ordinal:03d}"
        for ordinal, assertion in enumerate(assertions, 1)
    }
    assertion_statements = [
        cast(str, assertion["statement_text"])
        for assertion in assertions
    ]
    sources_by_identity: dict[
        tuple[str, str, str, str],
        dict[str, JSONValue],
    ] = {}
    selections_by_identity: dict[
        tuple[str, int, str],
        dict[str, JSONValue],
    ] = {}
    client_links: list[dict[str, JSONValue]] = []
    for link in links:
        source_kind = cast(str, link["source_kind"])
        persisted_stable_id = cast(str, link["source_stable_id"])
        source_version = cast(str, link["source_version_or_snapshot"])
        source_path = cast(str, link["source_path"])
        source_fingerprint = cast(str, link["source_fingerprint"])
        excerpt = cast(str, link["excerpt"])
        request_stable_id = persisted_stable_id
        source_excerpt = excerpt
        if source_kind == "user_assertion":
            request_stable_id = assertion_tokens.get(persisted_stable_id, "")
            if not request_stable_id:
                raise ProductActionIntegrityError(
                    "interview_story_undo_assertion_integrity"
                )
            statement = assertion_statements[int(request_stable_id[-3:]) - 1]
            source_excerpt = statement
            if (
                source_version != "pending_confirmation"
                or source_path != "/statement"
                or excerpt not in statement
                or source_fingerprint
                != hashlib.sha256(statement.encode("utf-8")).hexdigest()
            ):
                raise ProductActionIntegrityError(
                    "interview_story_undo_assertion_integrity"
                )
        else:
            if source_kind not in _STORY_SOURCE_KINDS:
                raise ProductActionIntegrityError(
                    "interview_story_undo_evidence_integrity"
                )
            try:
                if source_kind == "mock_turn":
                    source_id_text, separator, turn_number = (
                        persisted_stable_id.partition(":")
                    )
                    source_id = int(source_id_text)
                    if (
                        not separator
                        or len(turn_number) != 3
                        or not turn_number.isascii()
                        or not turn_number.isdigit()
                    ):
                        raise ValueError
                else:
                    source_id = int(persisted_stable_id)
            except ValueError as exc:
                raise ProductActionIntegrityError(
                    "interview_story_undo_evidence_integrity"
                ) from exc
            if source_id < 1:
                raise ProductActionIntegrityError(
                    "interview_story_undo_evidence_integrity"
                )
            selection_identity = (source_kind, source_id, source_path)
            selections_by_identity[selection_identity] = {
                "source_kind": source_kind,
                "source_id": source_id,
                "path": source_path,
            }
        source_identity = (
            source_kind,
            request_stable_id,
            source_version,
            source_path,
        )
        source: dict[str, JSONValue] = {
            "source_kind": source_kind,
            "source_stable_id": request_stable_id,
            "source_version_or_snapshot": source_version,
            "path": source_path,
            "excerpt": source_excerpt,
            "source_fingerprint": source_fingerprint,
        }
        existing = sources_by_identity.get(source_identity)
        if existing is not None and existing != source:
            raise ProductActionIntegrityError(
                "interview_story_undo_evidence_integrity"
            )
        sources_by_identity[source_identity] = source
        client_links.append(
            {
                "target_kind": link["target_kind"],
                "target_id": link["target_id"],
                "source_kind": source_kind,
                "source_stable_id": request_stable_id,
                "source_version_or_snapshot": source_version,
                "source_path": source_path,
                "excerpt": excerpt,
                "text_location": link["text_location"],
            }
        )
    sources = sorted(
        sources_by_identity.values(),
        key=lambda item: (
            cast(str, item["source_kind"]),
            cast(str, item["source_stable_id"]),
            cast(str, item["path"]),
        ),
    )
    selections = sorted(
        selections_by_identity.values(),
        key=lambda item: (
            cast(str, item["source_kind"]),
            str(item["source_id"]),
            cast(str, item["path"]),
        ),
    )
    return sources, selections, assertion_statements, client_links


def _validate_projected_source_snapshot(
    attempt: InterviewStoryProposalAttempt,
    projected: dict[str, JSONValue],
    *,
    expected_sources: list[dict[str, JSONValue]] | None = None,
) -> tuple[list[dict[str, JSONValue]], list[str], list[dict[str, JSONValue]]]:
    sources, selections, assertions, client_links = (
        _story_projected_source_lineage(projected)
    )
    exact_sources = sources if expected_sources is None else expected_sources
    if expected_sources is not None:
        catalog = {
            (
                cast(str, source.get("source_kind")),
                cast(str, source.get("source_stable_id")),
                cast(str, source.get("source_version_or_snapshot")),
                cast(str, source.get("path")),
            ): source
            for source in expected_sources
        }
        for source in sources:
            identity = (
                cast(str, source["source_kind"]),
                cast(str, source["source_stable_id"]),
                cast(str, source["source_version_or_snapshot"]),
                cast(str, source["path"]),
            )
            expected = catalog.get(identity)
            if (
                expected is None
                or source["source_fingerprint"]
                != expected.get("source_fingerprint")
                or cast(str, source["excerpt"])
                not in cast(str, expected.get("excerpt"))
            ):
                raise ProductActionIntegrityError(
                    "interview_story_undo_attempt_lineage"
                )
    source_fingerprint = hashlib.sha256(
        canonical_product_action_json(cast(JSONValue, exact_sources)).encode(
            "utf-8"
        )
    ).hexdigest()
    if (
        attempt.source_fingerprint != source_fingerprint
        or projected["source_fingerprint"] != source_fingerprint
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_attempt_lineage"
        )
    return selections, assertions, client_links


def _validate_proposal_previous_attempt_lineage(
    attempt: InterviewStoryProposalAttempt,
    projected: dict[str, JSONValue],
    prior_undo: dict[str, JSONValue],
    prior_result: dict[str, JSONValue],
) -> None:
    input_payload = _decode_json_object(
        attempt.input_snapshot_json,
        "interview_story_undo_previous_attempt_input",
    )
    if set(input_payload) != {
        "schema",
        "request_fingerprint",
        "target_story_id",
        "expected_current_version_id",
        "expected_story_revision",
        "selections",
        "assertions",
        "sources",
    }:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    sources = input_payload.get("sources")
    selections = input_payload.get("selections")
    assertions = input_payload.get("assertions")
    if (
        input_payload.get("schema") != "interview-story-v1"
        or type(sources) is not list
        or any(type(item) is not dict for item in sources)
        or type(selections) is not list
        or any(type(item) is not dict for item in selections)
        or type(assertions) is not list
        or any(type(item) is not str for item in assertions)
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    exact_sources = cast(list[dict[str, JSONValue]], sources)
    reconstructed_selections, reconstructed_assertions, _links = (
        _validate_projected_source_snapshot(
            attempt,
            projected,
            expected_sources=exact_sources,
        )
    )
    if (
        selections != reconstructed_selections
        or assertions != reconstructed_assertions
        or attempt.target_story_id != input_payload.get("target_story_id")
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    if prior_result["outcome"] == "created":
        expected_target: JSONValue = None
        expected_pointer: JSONValue = None
        expected_revision: JSONValue = None
    else:
        expected_target = prior_result["story_id"]
        expected_pointer = prior_undo["previous_current_version_id"]
        expected_revision = cast(int, prior_undo["expected_post_revision"]) - 1
    if (
        input_payload.get("target_story_id") != expected_target
        or input_payload.get("expected_current_version_id") != expected_pointer
        or input_payload.get("expected_story_revision") != expected_revision
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    try:
        from offerpilot.repositories.interview_stories import (
            story_request_fingerprint,
        )

        request_fingerprint = story_request_fingerprint(
            target_story_id=cast(int | None, expected_target),
            expected_current_version_id=cast(int | None, expected_pointer),
            expected_story_revision=cast(int | None, expected_revision),
            selections=cast(list[dict[str, Any]], selections),
            assertions=cast(list[str], assertions),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        ) from exc
    if input_payload.get("request_fingerprint") != request_fingerprint:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )


def _validate_manual_previous_attempt_lineage(
    session: Session,
    attempt: InterviewStoryProposalAttempt,
    previous: InterviewStoryVersion,
    projected: dict[str, JSONValue],
    *,
    expected_cas: tuple[int | None, int | None, int | None] | None = None,
) -> None:
    input_payload = _decode_json_object(
        attempt.input_snapshot_json,
        "interview_story_undo_previous_manual_input",
    )
    payload_keys = set(input_payload)
    legacy_shape = payload_keys == {"operation", "request_fingerprint"}
    raw_cas_shape = payload_keys == {
        "operation",
        "request_fingerprint",
        "target_story_id",
        "expected_current_version_id",
        "expected_story_revision",
    }
    if not legacy_shape and not raw_cas_shape:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    manual_marker = canonical_product_action_json(
        {"proposal_status": "manual"}
    )
    entry_context = canonical_product_action_json({"operation": "manual_save"})
    if (
        input_payload.get("operation") != "manual_save"
        or type(input_payload.get("request_fingerprint")) is not str
        or attempt.attempt_status != "confirmed"
        or attempt.product_action_operation_id is not None
        or attempt.product_action_generation != 0
        or attempt.generation_revision != 1
        or attempt.target_story_id != previous.story_id
        or attempt.confirmed_story_id != previous.story_id
        or attempt.confirmed_story_version_id != previous.id
        or attempt.entrypoint != "ui"
        or attempt.entry_context_json != entry_context
        or attempt.provider_call_token != ""
        or attempt.provider_lease_until is not None
        or attempt.proposal_json != manual_marker
        or attempt.proposal_hash
        != hashlib.sha256(manual_marker.encode("utf-8")).hexdigest()
        or attempt.failure_category != ""
        or type(attempt.idempotency_key) is not str
        or not 16 <= len(attempt.idempotency_key) <= 128
        or not attempt.idempotency_key.isascii()
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in attempt.idempotency_key
        )
        or attempt.confirmation_token_hash
        != hashlib.sha256(attempt.idempotency_key.encode("utf-8")).hexdigest()
        or attempt.confirmation_payload_hash
        != hashlib.sha256(attempt.input_snapshot_json.encode("utf-8")).hexdigest()
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    attempt_time = _story_datetime_utc(
        attempt.confirmed_at,
        "interview_story_undo_previous_manual_lineage",
    )
    version_time = _story_datetime_utc(
        previous.confirmed_at,
        "interview_story_undo_previous_manual_lineage",
    )
    if attempt_time < version_time or attempt_time - version_time > timedelta(minutes=5):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    selections, assertions, client_links = _validate_projected_source_snapshot(
        attempt,
        projected,
    )
    history = tuple(
        session.scalars(
            select(InterviewStoryVersion)
            .where(
                InterviewStoryVersion.story_id == previous.story_id,
                InterviewStoryVersion.version_number <= previous.version_number,
            )
            .order_by(InterviewStoryVersion.version_number)
        )
    )
    if (
        len(history) != previous.version_number
        or tuple(version.version_number for version in history)
        != tuple(range(1, previous.version_number + 1))
        or history[-1].id != previous.id
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    content = cast(dict[str, JSONValue], projected["content"])
    raw_content = _manual_story_content_from_canonical(content)
    request_fingerprint = cast(str, input_payload["request_fingerprint"])
    if raw_cas_shape:
        raw_cas = (
            input_payload.get("target_story_id"),
            input_payload.get("expected_current_version_id"),
            input_payload.get("expected_story_revision"),
        )
        if raw_cas == (None, None, None):
            if previous.version_number != 1:
                raise ProductActionIntegrityError(
                    "interview_story_undo_previous_manual_lineage"
                )
        elif (
            type(raw_cas[0]) is not int
            or raw_cas[0] != previous.story_id
            or type(raw_cas[1]) is not int
            or raw_cas[1] < 1
            or type(raw_cas[2]) is not int
            or raw_cas[2] < 1
            or previous.version_number == 1
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        if expected_cas is not None and raw_cas != expected_cas:
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        target_story_id, expected_pointer, expected_revision = cast(
            tuple[int | None, int | None, int | None],
            raw_cas,
        )
    elif expected_cas is not None:
        target_story_id, expected_pointer, expected_revision = expected_cas
    elif previous.version_number == 1:
        target_story_id = None
        expected_pointer = None
        expected_revision = None
    else:
        # Legacy rows did not persist raw CAS. A caller must first reconstruct
        # one unique owner state from the immutable Version/lifecycle chain.
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    try:
        from offerpilot.repositories.interview_stories import (
            _manual_request_fingerprint,
        )

        candidates = {
            _manual_request_fingerprint(
                target_story_id=target_story_id,
                content=raw_content,
                evidence_links=link_shape,
                selections=cast(list[dict[str, Any]], selections),
                assertions=assertions,
                expected_current_version_id=expected_pointer,
                expected_story_revision=expected_revision,
            )
            for link_shape in _manual_evidence_link_input_shapes(
                client_links,
                selections,
            )
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        ) from exc
    if not any(
        hmac.compare_digest(candidate, request_fingerprint)
        for candidate in candidates
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )


def _manual_evidence_link_input_shapes(
    frozen_links: list[dict[str, JSONValue]],
    selections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ...]:
    """Rebuild the bounded public link encodings accepted by manual saves."""

    selection_ids: dict[tuple[str, str, str], int] = {}
    for selection in selections:
        source_kind = selection.get("source_kind")
        source_id = selection.get("source_id")
        path = selection.get("path")
        if (
            type(source_kind) is not str
            or type(source_id) is not int
            or source_id < 1
            or type(path) is not str
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        identity = (source_kind, str(source_id), path)
        if identity in selection_ids:
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        selection_ids[identity] = source_id

    def rebuild(
        *,
        client_source_ids: bool,
        string_source_ids: bool,
        include_source_version: bool,
        include_empty_text_location: bool,
    ) -> list[dict[str, Any]]:
        rebuilt: list[dict[str, Any]] = []
        for link in frozen_links:
            required = {
                "target_kind",
                "target_id",
                "source_kind",
                "source_stable_id",
                "source_version_or_snapshot",
                "source_path",
                "excerpt",
                "text_location",
            }
            if set(link) != required or any(
                type(link[field]) is not str for field in required
            ):
                raise ProductActionIntegrityError(
                    "interview_story_undo_previous_manual_lineage"
                )
            identity = (
                cast(str, link["source_kind"]),
                cast(str, link["source_stable_id"]),
                cast(str, link["source_path"]),
            )
            source_id = selection_ids.get(identity)
            if source_id is None and identity[0] == "mock_turn":
                attempt_id, separator, turn_number = identity[1].partition(":")
                if (
                    separator
                    and len(turn_number) == 3
                    and turn_number.isascii()
                    and turn_number.isdigit()
                ):
                    source_id = selection_ids.get(
                        (identity[0], attempt_id, identity[2])
                    )
            raw: dict[str, Any] = {
                "target_kind": link["target_kind"],
                "target_id": link["target_id"],
                "source_kind": link["source_kind"],
                "source_path": link["source_path"],
                "excerpt": link["excerpt"],
            }
            if client_source_ids and source_id is not None:
                raw["source_id"] = str(source_id) if string_source_ids else source_id
            else:
                raw["source_stable_id"] = link["source_stable_id"]
                if include_source_version or (
                    client_source_ids and source_id is None
                ):
                    raw["source_version_or_snapshot"] = link[
                        "source_version_or_snapshot"
                    ]
            if include_empty_text_location or link["text_location"] != "":
                raw["text_location"] = link["text_location"]
            rebuilt.append(raw)
        return rebuilt

    shapes = (
        rebuild(
            client_source_ids=False,
            string_source_ids=False,
            include_source_version=True,
            include_empty_text_location=True,
        ),
        rebuild(
            client_source_ids=False,
            string_source_ids=False,
            include_source_version=True,
            include_empty_text_location=False,
        ),
        rebuild(
            client_source_ids=False,
            string_source_ids=False,
            include_source_version=False,
            include_empty_text_location=False,
        ),
        rebuild(
            client_source_ids=True,
            string_source_ids=False,
            include_source_version=False,
            include_empty_text_location=False,
        ),
        rebuild(
            client_source_ids=True,
            string_source_ids=False,
            include_source_version=False,
            include_empty_text_location=True,
        ),
        rebuild(
            client_source_ids=True,
            string_source_ids=True,
            include_source_version=False,
            include_empty_text_location=False,
        ),
    )
    unique: dict[str, list[dict[str, Any]]] = {}
    for shape in shapes:
        unique.setdefault(
            canonical_product_action_json(cast(JSONValue, shape)),
            shape,
        )
    return tuple(unique.values())


def _validate_proposal_version_origin_lineage(
    session: Session,
    version: InterviewStoryVersion,
    projection: dict[str, JSONValue],
    attempt: InterviewStoryProposalAttempt,
) -> None:
    prior_operation_id = attempt.product_action_operation_id
    if prior_operation_id is None:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    prior_parent = session.get(WriteOperation, prior_operation_id)
    if prior_parent is not None:
        _validate_parent_transition_prefix(session, prior_parent)
    try:
        _prior_digest, prior_undo, prior_result = _validate_story_parent(
            prior_parent
        )
    except ProductActionCompensationError as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        ) from exc
    prior_attempt = _story_attempt_lineage(
        session,
        parent_operation_id=prior_operation_id,
        story_id=version.story_id,
        version_id=version.id,
        outcome=cast(str, prior_result["outcome"]),
    )
    if prior_attempt.id != attempt.id:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    _validate_story_attempt_version_time(prior_attempt, version)
    _validate_proposal_previous_attempt_lineage(
        prior_attempt,
        projection,
        prior_undo,
        prior_result,
    )
    _validate_story_confirmation_payload(
        prior_attempt,
        projection,
        prior_undo,
    )


def _validate_manual_history_attempt_sequence(
    session: Session,
    previous: InterviewStoryVersion,
    key_profiles: LedgerKeyProfileStoreV1,
) -> None:
    history = tuple(
        session.scalars(
            select(InterviewStoryVersion)
            .where(
                InterviewStoryVersion.story_id == previous.story_id,
                InterviewStoryVersion.version_number <= previous.version_number,
            )
            .order_by(InterviewStoryVersion.version_number)
        )
    )
    if (
        len(history) != previous.version_number
        or tuple(version.version_number for version in history)
        != tuple(range(1, previous.version_number + 1))
        or history[-1].id != previous.id
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )
    attempts = {
        version.id: _story_attempt_for_version(session, version)
        for version in history
    }
    target_time = _story_datetime_utc(
        attempts[previous.id].confirmed_at,
        "interview_story_undo_previous_manual_lineage",
    )
    lifecycle_attempts = tuple(
        attempt
        for attempt in _story_lifecycle_attempts(session, previous.story_id)
        if _story_datetime_utc(
            attempt.confirmed_at,
            "interview_story_undo_lifecycle_lineage",
        )
        <= target_time
    )
    proposal_parent_ids = {
        attempt.product_action_operation_id
        for attempt in attempts.values()
        if attempt.product_action_operation_id is not None
    }
    compensations: tuple[WriteOperation, ...] = ()
    if proposal_parent_ids:
        compensations = tuple(
            operation
            for operation in session.scalars(
                select(WriteOperation).where(
                    WriteOperation.operation_role == "compensation",
                    WriteOperation.tool_name == "undo:confirm_interview_story",
                    WriteOperation.status == "committed",
                    WriteOperation.parent_operation_id.in_(proposal_parent_ids),
                )
            )
            if _story_datetime_utc(
                operation.committed_at,
                "interview_story_undo_later_compensation_lineage",
            )
            <= target_time
        )
    events: list[
        tuple[
            datetime,
            int,
            InterviewStoryVersion
            | InterviewStoryProposalAttempt
            | WriteOperation,
        ]
    ] = []
    for version in history:
        events.append(
            (
                _story_datetime_utc(
                    attempts[version.id].confirmed_at,
                    "interview_story_undo_previous_manual_lineage",
                ),
                0,
                version,
            )
        )
    for attempt in lifecycle_attempts:
        events.append(
            (
                _story_datetime_utc(
                    attempt.confirmed_at,
                    "interview_story_undo_lifecycle_lineage",
                ),
                1,
                attempt,
            )
        )
    for operation in compensations:
        events.append(
            (
                _story_datetime_utc(
                    operation.committed_at,
                    "interview_story_undo_later_compensation_lineage",
                ),
                1,
                operation,
            )
        )
    events.sort(key=lambda item: (item[0], item[1], str(item[2].id)))
    state: _StoryLineageState | None = None
    version_titles: dict[int, str] = {}
    expected_version_number = 1
    last_time: datetime | None = None
    for event_time, _priority, event in events:
        if last_time is not None and event_time < last_time:
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        last_time = event_time
        if isinstance(event, InterviewStoryVersion):
            if event.version_number != expected_version_number:
                raise ProductActionIntegrityError(
                    "interview_story_undo_previous_manual_lineage"
                )
            exact, projection = _story_version_snapshot(
                session,
                story_id=previous.story_id,
                version_id=event.id,
            )
            attempt = attempts[event.id]
            title = _story_projection_title(projection)
            version_titles[event.id] = title
            if state is None:
                if event.version_number != 1:
                    raise ProductActionIntegrityError(
                        "interview_story_undo_previous_manual_lineage"
                    )
                if event.origin_kind == "manual":
                    _validate_manual_previous_attempt_lineage(
                        session,
                        attempt,
                        exact,
                        projection,
                        expected_cas=(None, None, None),
                    )
                elif event.origin_kind == "proposal":
                    _validate_proposal_version_origin_lineage(
                        session,
                        exact,
                        projection,
                        attempt,
                    )
                    parent = session.get(
                        WriteOperation,
                        attempt.product_action_operation_id,
                    )
                    _digest, parent_undo, parent_result = _validate_story_parent(
                        parent
                    )
                    if (
                        parent_result["outcome"] != "created"
                        or parent_result["story_id"] != previous.story_id
                        or parent_result["story_version_id"] != event.id
                        or parent_result["story_revision"] != 1
                        or parent_undo["kind"] != "archive_created_story_v1"
                        or parent_undo["expected_story_revision"] != 1
                    ):
                        raise ProductActionIntegrityError(
                            "interview_story_undo_previous_attempt_lineage"
                        )
                else:
                    raise ProductActionIntegrityError(
                        "interview_story_undo_previous_manual_lineage"
                    )
                state = _StoryLineageState(
                    current_version_id=event.id,
                    story_revision=1,
                    title=title,
                    status="active",
                )
            else:
                state = _validate_story_later_version_event(
                    session,
                    story_id=previous.story_id,
                    version=event,
                    attempt=attempt,
                    state=state,
                )
            expected_version_number += 1
        elif isinstance(event, WriteOperation):
            if state is None:
                raise ProductActionIntegrityError(
                    "interview_story_undo_previous_manual_lineage"
                )
            state = _validate_committed_story_drift_compensation(
                session,
                operation=event,
                state=state,
                version_titles=version_titles,
                key_profiles=key_profiles,
            )
        else:
            if state is None:
                raise ProductActionIntegrityError(
                    "interview_story_undo_previous_manual_lineage"
                )
            state = _validate_story_lifecycle_event(event, state)
    if (
        state is None
        or state.current_version_id != previous.id
        or state.status != "active"
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_manual_lineage"
        )


def _validated_previous_story_version_snapshot(
    session: Session,
    *,
    story_id: int,
    previous_version_id: int,
    previous_title: str,
    key_profiles: LedgerKeyProfileStoreV1,
) -> tuple[str, InterviewStoryVersion]:
    previous, projection = _story_version_snapshot(
        session,
        story_id=story_id,
        version_id=previous_version_id,
    )
    content = cast(dict[str, JSONValue], projection["content"])
    title = cast(dict[str, JSONValue], content["title"])
    if title.get("text") != previous_title:
        raise ProductActionIntegrityError("interview_story_undo_previous_title")
    attempts = tuple(
        session.scalars(
            select(InterviewStoryProposalAttempt).where(
                InterviewStoryProposalAttempt.confirmed_story_version_id
                == previous.id
            )
        )
    )
    if len(attempts) != 1:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    attempt = attempts[0]
    if previous.origin_kind == "manual":
        if attempt.product_action_operation_id is not None:
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_manual_lineage"
            )
        # Reconstruct the exact predecessor CAS before validating legacy manual
        # rows, which did not persist those raw fields themselves.
        _validate_manual_history_attempt_sequence(
            session,
            previous,
            key_profiles,
        )
        return _story_version_snapshot_sha256(projection), previous
    if previous.origin_kind != "proposal" or attempt.product_action_operation_id is None:
        raise ProductActionIntegrityError(
            "interview_story_undo_previous_attempt_lineage"
        )
    _validate_proposal_version_origin_lineage(
        session,
        previous,
        projection,
        attempt,
    )
    _validate_manual_history_attempt_sequence(
        session,
        previous,
        key_profiles,
    )
    return _story_version_snapshot_sha256(projection), previous


def _story_projection_title(projected: dict[str, JSONValue]) -> str:
    content = cast(dict[str, JSONValue], projected["content"])
    title = cast(dict[str, JSONValue], content["title"])
    value = title.get("text")
    if type(value) is not str or not value.strip() or len(value) > 200:
        raise ProductActionIntegrityError("interview_story_undo_version_content")
    return value


def _validate_story_title_relation(
    story: InterviewStory,
    undo: dict[str, JSONValue],
    *,
    created_title: str,
) -> None:
    if undo["kind"] == "archive_created_story_v1":
        if (
            story.current_version_id == undo["created_version_id"]
            and story.story_revision
            in {
                cast(int, undo["expected_story_revision"]),
                cast(int, undo["expected_story_revision"]) + 1,
            }
            and story.title != created_title
        ):
            raise ProductActionIntegrityError("interview_story_undo_title_relation")
        return
    if (
        story.current_version_id == undo["created_version_id"]
        and story.story_revision == undo["expected_post_revision"]
        and story.title != created_title
    ) or (
        story.current_version_id == undo["previous_current_version_id"]
        and story.story_revision == cast(int, undo["expected_post_revision"]) + 1
        and story.title != undo["previous_title"]
    ):
        raise ProductActionIntegrityError("interview_story_undo_title_relation")


def _story_owner_snapshot_sha256(
    story: InterviewStory,
    *,
    source_attempt_id: int,
    created_version_sha256: str,
) -> str:
    updated_at = story.updated_at.isoformat() if isinstance(story.updated_at, datetime) else None
    archived_at = story.archived_at.isoformat() if isinstance(story.archived_at, datetime) else None
    payload: dict[str, JSONValue] = {
        "story": {
            "id": story.id,
            "title": story.title,
            "status": story.status,
            "current_version_id": story.current_version_id,
            "story_revision": story.story_revision,
            "updated_at": updated_at,
            "archived_at": archived_at,
        },
        "source_attempt_id": source_attempt_id,
        "created_version_sha256": created_version_sha256,
    }
    return "sha256:" + hashlib.sha256(
        canonical_product_action_json(payload).encode("utf-8")
    ).hexdigest()


def _story_is_original_post_state(
    story: InterviewStory,
    undo: dict[str, JSONValue],
) -> bool:
    if undo["kind"] == "archive_created_story_v1":
        return (
            story.status == "active"
            and story.current_version_id == undo["expected_current_version_id"]
            and story.story_revision == undo["expected_story_revision"]
        )
    return (
        story.status == "active"
        and story.current_version_id == undo["created_version_id"]
        and story.story_revision == undo["expected_post_revision"]
    )


def _story_is_terminal_post_state(
    story: InterviewStory,
    undo: dict[str, JSONValue],
) -> bool:
    if undo["kind"] == "archive_created_story_v1":
        return (
            story.status == "archived"
            and story.current_version_id == undo["created_version_id"]
            and story.story_revision == cast(int, undo["expected_story_revision"]) + 1
        )
    return (
        story.status == "active"
        and story.current_version_id == undo["previous_current_version_id"]
        and story.title == undo["previous_title"]
        and story.story_revision == cast(int, undo["expected_post_revision"]) + 1
    )


@dataclass(frozen=True, slots=True)
class _StoryLineageState:
    current_version_id: int
    story_revision: int
    title: str
    status: Literal["active", "archived"]
    archived_after: datetime | None = None
    archived_before: datetime | None = None


def _story_attempt_for_version(
    session: Session,
    version: InterviewStoryVersion,
) -> InterviewStoryProposalAttempt:
    attempts = tuple(
        session.scalars(
            select(InterviewStoryProposalAttempt).where(
                InterviewStoryProposalAttempt.confirmed_story_version_id
                == version.id
            )
        )
    )
    if len(attempts) != 1:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_attempt_lineage"
        )
    return attempts[0]


def _story_lifecycle_attempts(
    session: Session,
    story_id: int,
) -> tuple[InterviewStoryProposalAttempt, ...]:
    return tuple(
        session.scalars(
            select(InterviewStoryProposalAttempt)
            .where(
                InterviewStoryProposalAttempt.target_story_id == story_id,
                InterviewStoryProposalAttempt.confirmed_story_id == story_id,
                InterviewStoryProposalAttempt.confirmed_story_version_id.is_(None),
                InterviewStoryProposalAttempt.entrypoint == "internal",
            )
            .order_by(
                InterviewStoryProposalAttempt.confirmed_at,
                InterviewStoryProposalAttempt.id,
            )
        )
    )


def _story_canonical_iso_datetime(
    value: object,
    code: str,
) -> datetime:
    if type(value) is not str:
        raise ProductActionIntegrityError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProductActionIntegrityError(code) from exc
    exact = _story_datetime_utc(parsed, code)
    if exact.isoformat() != value:
        raise ProductActionIntegrityError(code)
    return exact


def _story_state_archived_at_matches(
    state: _StoryLineageState,
    archived_at: object,
    code: str,
) -> datetime | None:
    if state.status == "active":
        if archived_at is not None:
            raise ProductActionIntegrityError(code)
        return None
    exact = _story_canonical_iso_datetime(archived_at, code)
    if (
        state.archived_after is None
        or state.archived_before is None
        or exact < state.archived_after
        or exact > state.archived_before
    ):
        raise ProductActionIntegrityError(code)
    return exact


def _validate_story_lifecycle_event(
    attempt: InterviewStoryProposalAttempt,
    state: _StoryLineageState,
) -> _StoryLineageState:
    code = "interview_story_undo_lifecycle_lineage"
    payload = _decode_json_object(attempt.input_snapshot_json, code)
    if set(payload) != {
        "operation",
        "request_fingerprint",
        "target_story_id",
        "expected_story_revision",
        "desired_status",
        "transitioned_at",
        "before",
        "after",
    }:
        raise ProductActionIntegrityError(code)
    before = payload.get("before")
    after = payload.get("after")
    target_story_id = payload.get("target_story_id")
    expected_story_revision = payload.get("expected_story_revision")
    if (
        payload.get("operation") != "story_lifecycle_v1"
        or type(payload.get("request_fingerprint")) is not str
        or type(target_story_id) is not int
        or target_story_id < 1
        or type(expected_story_revision) is not int
        or expected_story_revision < 1
        or payload.get("desired_status") not in {"active", "archived"}
        or type(before) is not dict
        or type(after) is not dict
        or set(before)
        != {
            "status",
            "story_revision",
            "archived_at",
            "current_version_id",
            "title",
        }
        or set(after)
        != {
            "status",
            "story_revision",
            "archived_at",
            "current_version_id",
            "title",
        }
    ):
        raise ProductActionIntegrityError(code)
    before_exact = before
    after_exact = after
    event_time = _story_canonical_iso_datetime(payload["transitioned_at"], code)
    confirmed_at = _story_datetime_utc(attempt.confirmed_at, code)
    marker = canonical_product_action_json(
        {"proposal_status": "story_lifecycle"}
    )
    fingerprint_fields = dict(payload)
    request_fingerprint = cast(str, fingerprint_fields.pop("request_fingerprint"))
    try:
        from offerpilot.repositories.interview_stories import (
            _story_lifecycle_idempotency_key,
            _story_lifecycle_legacy_idempotency_key,
            _story_lifecycle_request_fingerprint,
        )

        expected_fingerprint = _story_lifecycle_request_fingerprint(
            cast(dict[str, Any], fingerprint_fields)
        )
        key_story_id = cast(int, payload["target_story_id"])
        key_revision = cast(int, payload["expected_story_revision"])
        key_status = cast(str, payload["desired_status"])
        expected_keys = {
            _story_lifecycle_idempotency_key(
                story_id=key_story_id,
                expected_story_revision=key_revision,
                desired_status=key_status,
            ),
            _story_lifecycle_legacy_idempotency_key(
                story_id=key_story_id,
                expected_story_revision=key_revision,
                desired_status=key_status,
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(code) from exc
    if (
        attempt.target_story_id != payload["target_story_id"]
        or attempt.confirmed_story_id != payload["target_story_id"]
        or attempt.confirmed_story_version_id is not None
        or attempt.idempotency_key not in expected_keys
        or attempt.entrypoint != "internal"
        or attempt.entry_context_json
        != canonical_product_action_json({"operation": "story_lifecycle_v1"})
        or attempt.attempt_status != "confirmed"
        or attempt.generation_revision != 1
        or attempt.provider_call_token != ""
        or attempt.provider_lease_until is not None
        or attempt.product_action_operation_id is not None
        or attempt.product_action_generation != 0
        or attempt.repair_count != 0
        or attempt.failure_category != ""
        or attempt.source_fingerprint != request_fingerprint
        or attempt.proposal_json != marker
        or attempt.proposal_hash
        != hashlib.sha256(marker.encode("utf-8")).hexdigest()
        or attempt.confirmation_token_hash
        != hashlib.sha256(attempt.idempotency_key.encode("utf-8")).hexdigest()
        or attempt.confirmation_payload_hash
        != hashlib.sha256(attempt.input_snapshot_json.encode("utf-8")).hexdigest()
        or not hmac.compare_digest(request_fingerprint, expected_fingerprint)
        or confirmed_at != event_time
    ):
        raise ProductActionIntegrityError(code)
    _story_state_archived_at_matches(state, before_exact["archived_at"], code)
    if (
        before_exact["status"] != state.status
        or before_exact["story_revision"] != state.story_revision
        or before_exact["current_version_id"] != state.current_version_id
        or before_exact["title"] != state.title
        or payload["expected_story_revision"] != state.story_revision
        or after_exact["story_revision"] != state.story_revision + 1
        or after_exact["current_version_id"] != state.current_version_id
        or after_exact["title"] != state.title
    ):
        raise ProductActionIntegrityError(code)
    if state.status == "active" and payload["desired_status"] == "archived":
        archived_at = _story_canonical_iso_datetime(
            after_exact["archived_at"],
            code,
        )
        if after_exact["status"] != "archived" or archived_at != event_time:
            raise ProductActionIntegrityError(code)
        return _StoryLineageState(
            current_version_id=state.current_version_id,
            story_revision=state.story_revision + 1,
            title=state.title,
            status="archived",
            archived_after=archived_at,
            archived_before=archived_at,
        )
    if state.status == "archived" and payload["desired_status"] == "active":
        if after_exact["status"] != "active" or after_exact["archived_at"] is not None:
            raise ProductActionIntegrityError(code)
        return _StoryLineageState(
            current_version_id=state.current_version_id,
            story_revision=state.story_revision + 1,
            title=state.title,
            status="active",
        )
    raise ProductActionIntegrityError(code)


def _validate_story_later_version_event(
    session: Session,
    *,
    story_id: int,
    version: InterviewStoryVersion,
    attempt: InterviewStoryProposalAttempt,
    state: _StoryLineageState,
) -> _StoryLineageState:
    if state.status != "active":
        raise ProductActionIntegrityError(
            "interview_story_undo_later_version_lineage"
        )
    exact, projection = _story_version_snapshot(
        session,
        story_id=story_id,
        version_id=version.id,
    )
    if exact is not version:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_version_lineage"
        )
    if version.origin_kind == "manual":
        if attempt.product_action_operation_id is not None:
            raise ProductActionIntegrityError(
                "interview_story_undo_later_attempt_lineage"
            )
        _validate_manual_previous_attempt_lineage(
            session,
            attempt,
            version,
            projection,
            expected_cas=(
                story_id,
                state.current_version_id,
                state.story_revision,
            ),
        )
    elif version.origin_kind == "proposal":
        parent_operation_id = attempt.product_action_operation_id
        if parent_operation_id is None:
            raise ProductActionIntegrityError(
                "interview_story_undo_later_attempt_lineage"
            )
        parent = session.get(WriteOperation, parent_operation_id)
        if parent is not None:
            _validate_parent_transition_prefix(session, parent)
        try:
            _digest, parent_undo, parent_result = _validate_story_parent(parent)
        except ProductActionCompensationError as exc:
            raise ProductActionIntegrityError(
                "interview_story_undo_later_attempt_lineage"
            ) from exc
        exact_attempt = _story_attempt_lineage(
            session,
            parent_operation_id=parent_operation_id,
            story_id=story_id,
            version_id=version.id,
            outcome=cast(str, parent_result["outcome"]),
        )
        if (
            exact_attempt.id != attempt.id
            or parent_result["outcome"] != "version_appended"
            or parent_result["story_revision"] != state.story_revision + 1
            or parent_undo["kind"] != "restore_story_pointer_v1"
            or parent_undo["previous_current_version_id"]
            != state.current_version_id
            or parent_undo["previous_title"] != state.title
            or parent_undo["expected_post_revision"]
            != state.story_revision + 1
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_later_attempt_lineage"
            )
        _validate_story_attempt_version_time(exact_attempt, version)
        _validate_proposal_previous_attempt_lineage(
            exact_attempt,
            projection,
            parent_undo,
            parent_result,
        )
        _validate_story_confirmation_payload(
            exact_attempt,
            projection,
            parent_undo,
        )
    else:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_version_lineage"
        )
    return _StoryLineageState(
        current_version_id=version.id,
        story_revision=state.story_revision + 1,
        title=_story_projection_title(projection),
        status="active",
    )


def _validate_committed_story_drift_compensation(
    session: Session,
    *,
    operation: WriteOperation,
    state: _StoryLineageState,
    version_titles: dict[int, str],
    key_profiles: LedgerKeyProfileStoreV1,
) -> _StoryLineageState:
    parent = (
        session.get(WriteOperation, operation.parent_operation_id)
        if operation.parent_operation_id is not None
        else None
    )
    if parent is not None:
        _validate_parent_transition_prefix(session, parent)
    try:
        parent_digest, undo, parent_result = _validate_story_parent(parent)
    except ProductActionCompensationError as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        ) from exc
    _validate_parent_transition_prefix(session, operation)
    expected_operation_id = product_action_compensation_operation_id(
        cast(str, operation.parent_operation_id),
        "undo:confirm_interview_story",
    )
    if (
        operation.id != expected_operation_id
        or operation.operation_role != "compensation"
        or operation.adapter_kind != "compensation"
        or operation.tool_name != "undo:confirm_interview_story"
        or operation.status != "committed"
        or operation.parent_terminal_payload_sha256 != parent_digest
        or operation.conversation_id is not None
        or operation.agent_run_id is not None
        or operation.tool_call_id is not None
        or operation.proposal_fingerprint is not None
        or operation.confirmation_token_fingerprint is not None
        or operation.authorization_scope_fingerprint is not None
        or operation.operation_request_fingerprint is None
        or operation.input_fingerprint is None
        or operation.result_contract != "compensation_json_v1"
        or operation.result_json is None
        or operation.visible_result
        != _HANDLER_PROFILES["undo:confirm_interview_story"].committed_visible
        or operation.transport_json is None
        or operation.undo_json is not None
        or operation.failure_category is not None
        or operation.failure_code is not None
        or operation.delivery_status != "not_applicable"
        or operation.delivery_generation != 0
        or operation.delivery_outcome != "none"
        or operation.delivery_message_count != 0
        or operation.delivery_owner_token_fingerprint is not None
        or operation.delivery_lease_expires_at is not None
        or operation.delivery_manifest_sha256 is not None
        or operation.delivery_next_operation_id is not None
        or operation.delivery_failure_code is not None
        or operation.approved_at is None
        or operation.claimed_at is None
        or operation.committed_at is None
        or operation.failed_at is not None
        or operation.rejected_at is not None
        or operation.delivered_at != operation.committed_at
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    key = key_profiles.resolve(operation.fingerprint_key_id)
    expected_request = product_action_compensation_request_fingerprint(
        key,
        operation_id=operation.id,
        parent_operation_id=cast(str, operation.parent_operation_id),
        parent_action_name="confirm_interview_story",
        parent_terminal_payload_sha256=parent_digest,
        compensation_kind="undo:confirm_interview_story",
    )
    expected_input = product_action_compensation_input_fingerprint(
        key,
        operation_request_fingerprint=expected_request,
        parent_terminal_payload_sha256=parent_digest,
        validated_undo_json=undo,
    )
    if (
        not hmac.compare_digest(
            operation.operation_request_fingerprint,
            expected_request,
        )
        or not hmac.compare_digest(operation.input_fingerprint, expected_input)
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    _enforce_compensation_terminal_budgets(
        operation.result_json,
        operation.visible_result,
        operation.transport_json,
        operation.undo_json,
    )
    try:
        payload_from_operation(operation)
    except Exception as exc:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        ) from exc
    result = _decode_json_object(
        operation.result_json,
        "interview_story_undo_later_compensation_result",
    )
    transport = _decode_json_object(
        operation.transport_json,
        "interview_story_undo_later_compensation_transport",
    )
    if (
        set(result)
        != {
            "kind",
            "story_id",
            "current_version_id",
            "story_revision",
            "status",
        }
        or result.get("kind") != "interview_story_undo_applied_v1"
        or result.get("story_id") != parent_result["story_id"]
        or transport
        != {
            "schema_version": 1,
            "operation_id": operation.id,
            "compensation_kind": "undo:confirm_interview_story",
            "status": "committed",
            "result": result,
        }
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    created_version_id = cast(int, parent_result["story_version_id"])
    created_title = version_titles.get(created_version_id)
    if created_title is None:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    if undo["kind"] == "archive_created_story_v1":
        if (
            state.status != "active"
            or state.current_version_id != created_version_id
            or state.story_revision != undo["expected_story_revision"]
            or state.title != created_title
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_later_compensation_lineage"
            )
        next_state = _StoryLineageState(
            current_version_id=created_version_id,
            story_revision=state.story_revision + 1,
            title=created_title,
            status="archived",
            archived_after=_story_datetime_utc(
                operation.claimed_at,
                "interview_story_undo_later_compensation_lineage",
            ),
            archived_before=_story_datetime_utc(
                operation.committed_at,
                "interview_story_undo_later_compensation_lineage",
            ),
        )
    elif undo["kind"] == "restore_story_pointer_v1":
        previous_version_id = cast(int, undo["previous_current_version_id"])
        if (
            state.status != "active"
            or state.current_version_id != created_version_id
            or state.story_revision != undo["expected_post_revision"]
            or state.title != created_title
            or version_titles.get(previous_version_id) != undo["previous_title"]
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_later_compensation_lineage"
            )
        next_state = _StoryLineageState(
            current_version_id=previous_version_id,
            story_revision=state.story_revision + 1,
            title=cast(str, undo["previous_title"]),
            status="active",
        )
    else:
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    if (
        result.get("current_version_id") != next_state.current_version_id
        or result.get("story_revision") != next_state.story_revision
        or result.get("status") != next_state.status
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_compensation_lineage"
        )
    return next_state


def _validate_legitimate_story_drift(
    session: Session,
    *,
    story: InterviewStory,
    created: InterviewStoryVersion,
    created_projection: dict[str, JSONValue],
    source_attempt: InterviewStoryProposalAttempt,
    parent_result: dict[str, JSONValue],
    undo: dict[str, JSONValue],
    key_profiles: LedgerKeyProfileStoreV1,
) -> bool:
    """Prove post-parent drift from immutable append/compensation lineage."""

    _validate_story_lifecycle_timestamps(story)
    versions = tuple(
        session.scalars(
            select(InterviewStoryVersion)
            .where(InterviewStoryVersion.story_id == story.id)
            .order_by(
                InterviewStoryVersion.version_number,
                InterviewStoryVersion.id,
            )
        )
    )
    if (
        len(versions) < created.version_number
        or tuple(version.version_number for version in versions)
        != tuple(range(1, len(versions) + 1))
        or versions[created.version_number - 1].id != created.id
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_version_lineage"
        )
    later_versions = versions[created.version_number :]
    target_time = _story_datetime_utc(
        source_attempt.confirmed_at,
        "interview_story_undo_attempt_lineage",
    )
    later_lifecycle_attempts = tuple(
        attempt
        for attempt in _story_lifecycle_attempts(session, story.id)
        if _story_datetime_utc(
            attempt.confirmed_at,
            "interview_story_undo_lifecycle_lineage",
        )
        > target_time
    )
    attempts = {
        version.id: _story_attempt_for_version(session, version)
        for version in later_versions
    }
    proposal_parent_ids = {
        attempt.product_action_operation_id
        for attempt in attempts.values()
        if attempt.product_action_operation_id is not None
    }
    compensations: tuple[WriteOperation, ...] = ()
    if proposal_parent_ids:
        compensations = tuple(
            session.scalars(
                select(WriteOperation).where(
                    WriteOperation.operation_role == "compensation",
                    WriteOperation.tool_name == "undo:confirm_interview_story",
                    WriteOperation.status == "committed",
                    WriteOperation.parent_operation_id.in_(proposal_parent_ids),
                )
            )
        )
    if not later_versions and not compensations and not later_lifecycle_attempts:
        if _story_is_original_post_state(story, undo):
            return False
        raise ProductActionIntegrityError(
            "interview_story_undo_later_version_lineage"
        )
    events: list[
        tuple[
            datetime,
            int,
            InterviewStoryVersion
            | InterviewStoryProposalAttempt
            | WriteOperation,
        ]
    ] = []
    for version in later_versions:
        attempt = attempts[version.id]
        events.append(
            (
                _story_datetime_utc(
                    attempt.confirmed_at,
                    "interview_story_undo_later_attempt_lineage",
                ),
                0,
                version,
            )
        )
    for operation in compensations:
        events.append(
            (
                _story_datetime_utc(
                    operation.committed_at,
                    "interview_story_undo_later_compensation_lineage",
                ),
                1,
                operation,
            )
        )
    for attempt in later_lifecycle_attempts:
        events.append(
            (
                _story_datetime_utc(
                    attempt.confirmed_at,
                    "interview_story_undo_lifecycle_lineage",
                ),
                1,
                attempt,
            )
        )
    events.sort(key=lambda item: (item[0], item[1], str(item[2].id)))
    state = _StoryLineageState(
        current_version_id=created.id,
        story_revision=cast(int, parent_result["story_revision"]),
        title=_story_projection_title(created_projection),
        status="active",
    )
    version_titles = {
        version.id: _story_projection_title(
            _story_version_snapshot(
                session,
                story_id=story.id,
                version_id=version.id,
            )[1]
        )
        for version in versions
    }
    expected_version_number = created.version_number + 1
    for event_time, _priority, event in events:
        if event_time < target_time:
            raise ProductActionIntegrityError(
                "interview_story_undo_later_event_order"
            )
        if isinstance(event, InterviewStoryVersion):
            if event.version_number != expected_version_number:
                raise ProductActionIntegrityError(
                    "interview_story_undo_later_version_lineage"
                )
            state = _validate_story_later_version_event(
                session,
                story_id=story.id,
                version=event,
                attempt=attempts[event.id],
                state=state,
            )
            expected_version_number += 1
        elif isinstance(event, WriteOperation):
            state = _validate_committed_story_drift_compensation(
                session,
                operation=event,
                state=state,
                version_titles=version_titles,
                key_profiles=key_profiles,
            )
        else:
            state = _validate_story_lifecycle_event(event, state)
    if (
        story.current_version_id != state.current_version_id
        or story.story_revision != state.story_revision
        or story.title != state.title
        or story.status != state.status
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_later_owner_state"
        )
    if state.status == "active":
        if story.archived_at is not None:
            raise ProductActionIntegrityError(
                "interview_story_undo_later_owner_state"
            )
    else:
        archived_at = _story_datetime_utc(
            story.archived_at,
            "interview_story_undo_later_owner_state",
        )
        if (
            state.archived_after is None
            or state.archived_before is None
            or archived_at < state.archived_after
            or archived_at > state.archived_before
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_later_owner_state"
            )
    return True


class InterviewStoryUndoIssuer:
    """Issue one owner-bound Story Undo proof after capability short-circuit."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        catalog: ProductActionCompensationCatalogV1,
        proof_registry: ProductActionCompensationProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
        capability_check: Callable[[str], bool],
    ) -> None:
        if (
            not callable(session_factory)
            or type(catalog) is not ProductActionCompensationCatalogV1
            or type(proof_registry) is not ProductActionCompensationProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or not callable(capability_check)
        ):
            raise TypeError("Interview Story Undo Issuer composition is invalid")
        self._session_factory = session_factory
        self._catalog = catalog
        self._proof_registry = proof_registry
        self._key_profiles = key_profiles
        self._capability_check = capability_check

    def issue(
        self,
        *,
        story_id: int,
        parent_operation_id: str,
    ) -> InterviewStoryProductActionUndoProof:
        if not self._capability_check("stories.write"):
            raise ProductActionCompensationError(
                "product_action_compensation_permission_denied",
                status_code=403,
            )
        if type(story_id) is not int or story_id < 1:
            raise ProductActionCompensationError(
                "product_action_compensation_not_found",
                status_code=404,
            )
        parent_operation_id = _canonical_uuid(
            parent_operation_id,
            "parent_operation_id",
        )
        with self._session_factory() as session:
            story = session.get(InterviewStory, story_id)
            if story is None or story.current_version_id is None:
                raise ProductActionCompensationError(
                    "product_action_compensation_not_found",
                    status_code=404,
                )
            _validate_story_lifecycle_timestamps(story)
            parent = session.get(WriteOperation, parent_operation_id)
            if parent is not None:
                _validate_parent_transition_prefix(session, parent)
            parent_digest, undo, result = _validate_story_parent(parent)
            if result["story_id"] != story.id:
                raise ProductActionCompensationError(
                    "product_action_compensation_not_found",
                    status_code=404,
                )
            attempt = _story_attempt_lineage(
                session,
                parent_operation_id=parent_operation_id,
                story_id=story.id,
                version_id=cast(int, result["story_version_id"]),
                outcome=cast(str, result["outcome"]),
            )
            created, created_projection = _story_version_snapshot(
                session,
                story_id=story.id,
                version_id=cast(int, result["story_version_id"]),
                expected_origin_kind="proposal",
            )
            _validate_story_attempt_version_time(attempt, created)
            if attempt.source_fingerprint != created.source_fingerprint:
                raise ProductActionIntegrityError(
                    "interview_story_undo_attempt_lineage"
                )
            created_digest = _story_version_snapshot_sha256(created_projection)
            confirmed_payload_digest = _validate_story_confirmation_payload(
                attempt,
                created_projection,
                undo,
            )
            previous_digest: str | None = None
            previous_version_id: int | None = None
            if undo["kind"] == "restore_story_pointer_v1":
                previous_version_id = cast(
                    int,
                    undo["previous_current_version_id"],
                )
                previous_digest, previous = (
                    _validated_previous_story_version_snapshot(
                        session,
                        story_id=story.id,
                        previous_version_id=previous_version_id,
                        previous_title=cast(str, undo["previous_title"]),
                        key_profiles=self._key_profiles,
                    )
                )
                if previous.version_number >= created.version_number:
                    raise ProductActionIntegrityError(
                        "interview_story_undo_previous_pointer"
                    )
            history_digest = _story_history_snapshot_sha256(
                session,
                story_id=story.id,
                created_version_id=created.id,
                previous_version_id=previous_version_id,
            )
            _validate_story_title_relation(
                story,
                undo,
                created_title=_story_projection_title(created_projection),
            )
            operation_id = product_action_compensation_operation_id(
                parent_operation_id,
                "undo:confirm_interview_story",
            )
            compensation = session.get(WriteOperation, operation_id)
            drifted = (
                False
                if compensation is not None and compensation.status == "committed"
                else _validate_legitimate_story_drift(
                    session,
                    story=story,
                    created=created,
                    created_projection=created_projection,
                    source_attempt=attempt,
                    parent_result=result,
                    undo=undo,
                    key_profiles=self._key_profiles,
                )
            )
            if compensation is not None and compensation.status == "committed":
                if not _story_is_terminal_post_state(story, undo):
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                owner_state: Literal[
                    "active", "terminal_committed", "terminal_failed"
                ] = "terminal_committed"
            elif compensation is not None and compensation.status == "failed":
                if not drifted:
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_domain"
                    )
                owner_state = "terminal_failed"
            else:
                owner_state = "active"
            record = _StoryUndoProofRecord(
                story_id=story.id,
                source_attempt_id=attempt.id,
                expected_current_version_id=story.current_version_id,
                expected_story_revision=story.story_revision,
                parent_operation_id=parent_operation_id,
                parent_action_name="confirm_interview_story",
                parent_terminal_payload_sha256=parent_digest,
                compensation_kind="undo:confirm_interview_story",
                validated_undo_json=MappingProxyType(dict(undo)),
                owner_state=owner_state,
                created_version_snapshot_sha256=created_digest,
                previous_version_snapshot_sha256=previous_digest,
                story_history_snapshot_sha256=history_digest,
                confirmed_effective_payload_sha256=confirmed_payload_digest,
                owner_snapshot_sha256=_story_owner_snapshot_sha256(
                    story,
                    source_attempt_id=attempt.id,
                    created_version_sha256=created_digest,
                ),
            )
        return self._proof_registry._issue_story(record)


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionCompensationResultV1:
    operation_id: str
    compensation_kind: str
    status: Literal["committed", "failed"]
    result: MappingProxyType[str, JSONValue]
    replayed: bool


_EXECUTION_UOW_CONSTRUCTION_SEAL = object()


class _CompensationExecutionUowV1:
    __slots__ = (
        "_incarnation",
        "_nonce",
        "_registry",
        "_registry_token",
        "_seal",
        "__weakref__",
    )
    _registry: _CompensationExecutionUowRegistryV1
    _registry_token: object
    _incarnation: object
    _nonce: object
    _seal: tuple[object, object, object, object]

    def __new__(
        cls,
        construction_seal: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> "_CompensationExecutionUowV1":
        if construction_seal is not _EXECUTION_UOW_CONSTRUCTION_SEAL:
            raise TypeError("Compensation execution UoWs are Coordinator-created")
        return object.__new__(cls)

    def __init__(
        self,
        construction_seal: object,
        registry: "_CompensationExecutionUowRegistryV1",
        registry_token: object,
        incarnation: object,
        nonce: object,
    ) -> None:
        if (
            construction_seal is not _EXECUTION_UOW_CONSTRUCTION_SEAL
            or type(registry) is not _CompensationExecutionUowRegistryV1
            or registry_token is not registry._registry_token
            or incarnation is not registry._incarnation
        ):
            raise TypeError("Compensation execution UoWs are Coordinator-created")
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(
            self,
            "_seal",
            (registry, registry_token, incarnation, nonce),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Compensation execution UoWs are sealed")

    def _claim_signal(
        self,
        session: Session,
        *,
        operation_id: str,
        parent_operation_id: str,
        authorization_binding: tuple[object, ...],
    ) -> "_CompensationExecutionUowClaimV1":
        return self._registry._claim(
            self,
            session,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            authorization_binding=authorization_binding,
        )

    def _claim_story(
        self,
        session: Session,
        *,
        operation_id: str,
        parent_operation_id: str,
        authorization_binding: tuple[object, ...],
    ) -> "_CompensationExecutionUowClaimV1":
        return self._registry._claim(
            self,
            session,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            authorization_binding=authorization_binding,
        )


@dataclass(slots=True, repr=False)
class _LiveCompensationExecutionUowRecordV1:
    uow: _CompensationExecutionUowV1
    session: Session
    operation_id: str
    parent_operation_id: str
    authorization_binding: tuple[object, ...]
    terminalize: Callable[[Session, object], dict[str, JSONValue]]
    state: Literal["issued", "in_flight"]


class _CompensationExecutionUowClaimV1:
    __slots__ = ("_closed", "_record", "_registry", "_uow")

    def __init__(
        self,
        registry: "_CompensationExecutionUowRegistryV1",
        uow: _CompensationExecutionUowV1,
        record: _LiveCompensationExecutionUowRecordV1,
    ) -> None:
        self._registry = registry
        self._uow = uow
        self._record = record
        self._closed = False

    def __enter__(self) -> "_CompensationExecutionUowClaimV1":
        if self._closed:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow"
            )
        return self

    def terminalize_signal(
        self,
        session: Session,
        domain_result: object,
    ) -> dict[str, JSONValue]:
        if self._closed:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow"
            )
        projected = self._registry._terminalize(
            self._uow,
            self._record,
            session,
            domain_result,
        )
        self._closed = True
        return projected

    def terminalize_story(
        self,
        session: Session,
        domain_result: object,
    ) -> dict[str, JSONValue]:
        return self.terminalize_signal(session, domain_result)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if self._closed:
            return
        self._closed = True
        self._registry._revoke(self._uow, self._record)
        if exc_type is None:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow_unclaimed"
            )


class _CompensationExecutionUowRegistryV1:
    __slots__ = (
        "_incarnation",
        "_issue_token",
        "_lock",
        "_records",
        "_registry_token",
        "_retired",
        "_seal",
    )
    _registry_token: object
    _incarnation: object
    _issue_token: object
    _records: dict[int, _LiveCompensationExecutionUowRecordV1]
    _retired: WeakKeyDictionary[
        _CompensationExecutionUowV1,
        Literal["consumed", "revoked"],
    ]
    _lock: RLock
    _seal: tuple[
        object,
        object,
        object,
        dict[int, _LiveCompensationExecutionUowRecordV1],
        WeakKeyDictionary[
            _CompensationExecutionUowV1,
            Literal["consumed", "revoked"],
        ],
        RLock,
    ]

    def __init__(self, issue_token: object) -> None:
        if type(issue_token) is not object:
            raise TypeError("Compensation execution UoW issue token must be opaque")
        registry_token = object()
        incarnation = object()
        records: dict[int, _LiveCompensationExecutionUowRecordV1] = {}
        retired: WeakKeyDictionary[
            _CompensationExecutionUowV1,
            Literal["consumed", "revoked"],
        ] = WeakKeyDictionary()
        lock = RLock()
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_incarnation", incarnation)
        object.__setattr__(self, "_issue_token", issue_token)
        object.__setattr__(self, "_records", records)
        object.__setattr__(self, "_retired", retired)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(
            self,
            "_seal",
            (registry_token, incarnation, issue_token, records, retired, lock),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Compensation execution UoW Registries are sealed")

    def _ensure_integrity(self) -> None:
        if self._seal != (
            self._registry_token,
            self._incarnation,
            self._issue_token,
            self._records,
            self._retired,
            self._lock,
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow_registry"
            )

    def _issue(
        self,
        session: Session,
        *,
        issue_token: object,
        operation_id: str,
        parent_operation_id: str,
        authorization_binding: tuple[object, ...],
        terminalize: Callable[[Session, object], dict[str, JSONValue]],
    ) -> _CompensationExecutionUowV1:
        self._ensure_integrity()
        if (
            issue_token is not self._issue_token
            or not isinstance(session, Session)
            or not callable(terminalize)
        ):
            raise TypeError("Compensation execution UoW composition is invalid")
        nonce = object()
        uow = _CompensationExecutionUowV1(
            _EXECUTION_UOW_CONSTRUCTION_SEAL,
            self,
            self._registry_token,
            self._incarnation,
            nonce,
        )
        record = _LiveCompensationExecutionUowRecordV1(
            uow,
            session,
            operation_id,
            parent_operation_id,
            authorization_binding,
            terminalize,
            "issued",
        )
        with self._lock:
            self._records[id(uow)] = record
        return uow

    def _record(
        self,
        uow: object,
    ) -> _LiveCompensationExecutionUowRecordV1:
        self._ensure_integrity()
        if type(uow) is not _CompensationExecutionUowV1:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow"
            )
        try:
            if (
                uow._seal
                != (uow._registry, uow._registry_token, uow._incarnation, uow._nonce)
                or uow._registry is not self
                or uow._registry_token is not self._registry_token
                or uow._incarnation is not self._incarnation
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow"
                )
        except AttributeError as exc:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow"
            ) from exc
        record = self._records.get(id(uow))
        if record is None or record.uow is not uow:
            raise ProductActionIntegrityError(
                "product_action_compensation_execution_uow"
            )
        return record

    def _claim(
        self,
        uow: _CompensationExecutionUowV1,
        session: Session,
        *,
        operation_id: str,
        parent_operation_id: str,
        authorization_binding: tuple[object, ...],
    ) -> _CompensationExecutionUowClaimV1:
        with self._lock:
            record = self._record(uow)
            if (
                record.state != "issued"
                or record.session is not session
                or record.operation_id != operation_id
                or record.parent_operation_id != parent_operation_id
                or record.authorization_binding != authorization_binding
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow"
                )
            record.state = "in_flight"
        return _CompensationExecutionUowClaimV1(self, uow, record)

    def _terminalize(
        self,
        uow: _CompensationExecutionUowV1,
        record: _LiveCompensationExecutionUowRecordV1,
        session: Session,
        domain_result: object,
    ) -> dict[str, JSONValue]:
        with self._lock:
            current = self._record(uow)
            if current is not record or record.state != "in_flight" or record.session is not session:
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow"
                )
        try:
            projected = record.terminalize(session, domain_result)
        except BaseException:
            self._revoke(uow, record)
            raise
        if type(projected) is not dict or "kind" in projected:
            self._revoke(uow, record)
            raise ProductActionIntegrityError(
                "product_action_compensation_handler_projection"
            )
        with self._lock:
            if self._records.pop(id(uow), None) is not record:
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow_registry"
                )
            self._retired[uow] = "consumed"
        return projected

    def _revoke(
        self,
        uow: _CompensationExecutionUowV1,
        expected: _LiveCompensationExecutionUowRecordV1 | None = None,
    ) -> Literal["revoked", "consumed"]:
        with self._lock:
            record = self._records.get(id(uow))
            if record is None:
                state = self._retired.get(uow)
                if state in {"consumed", "revoked"}:
                    return state
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow"
                )
            if expected is not None and record is not expected:
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow_registry"
                )
            if self._records.pop(id(uow), None) is not record:
                raise ProductActionIntegrityError(
                    "product_action_compensation_execution_uow_registry"
                )
            self._retired[uow] = "revoked"
            return "revoked"

    def _state(
        self,
        uow: _CompensationExecutionUowV1,
    ) -> Literal["issued", "in_flight", "consumed", "revoked", "invalid"]:
        with self._lock:
            try:
                record = self._record(uow)
            except ProductActionIntegrityError:
                retired = self._retired.get(uow)
                return retired if retired in {"consumed", "revoked"} else "invalid"
            return record.state


class _ProductActionCompensationHandlerV1(Protocol):
    """Closed method surface used by future domain-specific Undo handlers."""

    compensation_kind: str

    def revalidate_owner_in_session(
        self,
        session: Session,
        owner_record: object,
    ) -> None: ...

    def execute_in_session(
        self,
        session: Session,
        *,
        owner_record: object,
        compensation_operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        execution_uow: _CompensationExecutionUowV1,
    ) -> dict[str, JSONValue]: ...

    def validate_terminal_in_session(
        self,
        session: Session,
        owner_record: object,
        operation: WriteOperation,
        result: dict[str, JSONValue],
    ) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class _CompensationHandlerProfileV1:
    parent_action_name: str
    capability: str
    committed_result_kind: str
    stale_codes: tuple[str, ...]
    committed_visible: str
    failed_visible: str


_HANDLER_PROFILES = MappingProxyType(
    {
        "undo:confirm_interview_story": _CompensationHandlerProfileV1(
            "confirm_interview_story",
            "stories.write",
            "interview_story_undo_applied_v1",
            ("interview_story_undo_stale",),
            "已撤销经历素材写入，并保留历史版本。",
            "当前经历素材已变化，无法安全撤销。",
        ),
        "undo:save_review_readiness_signal": _CompensationHandlerProfileV1(
            "save_review_readiness_signal",
            "application.interview_readiness_feedback.write",
            "review_readiness_signal_retracted_v1",
            ("readiness_signal_undo_stale",),
            "已撤销准备重点，并保留历史版本。",
            "当前准备重点已变化，无法安全撤销。",
        ),
    }
)


class _SealedProductActionCompensationHandlerV1:
    """Immutable method snapshot; ordinary handlers never enter Coordinator state."""

    __slots__ = (
        "compensation_kind",
        "parent_action_name",
        "capability",
        "committed_result_kind",
        "declared_stale_codes",
        "terminal_budgets",
        "committed_visible",
        "failed_visible",
        "_revalidate_owner",
        "_execute",
        "_validate_terminal",
        "_seal",
    )
    compensation_kind: str
    parent_action_name: str
    capability: str
    committed_result_kind: str
    declared_stale_codes: tuple[str, ...]
    terminal_budgets: tuple[int, int, int, int, int]
    committed_visible: str
    failed_visible: str
    _revalidate_owner: Callable[[Session, object], None]
    _execute: Callable[..., dict[str, JSONValue]]
    _validate_terminal: Callable[
        [Session, object, WriteOperation, dict[str, JSONValue]],
        None,
    ]
    _seal: tuple[object, ...]

    def __init__(
        self,
        handler: _ProductActionCompensationHandlerV1,
        *,
        catalog: ProductActionCompensationCatalogV1,
    ) -> None:
        compensation_kind = handler.compensation_kind
        profile = _HANDLER_PROFILES.get(compensation_kind)
        metadata = catalog.resolve_metadata(compensation_kind)
        revalidate = handler.revalidate_owner_in_session
        execute = handler.execute_in_session
        validate_terminal = handler.validate_terminal_in_session
        if (
            type(catalog) is not ProductActionCompensationCatalogV1
            or type(compensation_kind) is not str
            or profile is None
            or metadata is None
            or metadata.parent_action_name != profile.parent_action_name
            or metadata.capability != profile.capability
            or not callable(revalidate)
            or not callable(execute)
            or not callable(validate_terminal)
        ):
            raise TypeError("Product Action compensation handler contract is invalid")
        object.__setattr__(self, "compensation_kind", compensation_kind)
        object.__setattr__(self, "parent_action_name", profile.parent_action_name)
        object.__setattr__(self, "capability", profile.capability)
        object.__setattr__(self, "committed_result_kind", profile.committed_result_kind)
        object.__setattr__(self, "declared_stale_codes", profile.stale_codes)
        object.__setattr__(
            self,
            "terminal_budgets",
            (_RESULT_BYTES, _VISIBLE_BYTES, _TRANSPORT_BYTES, _UNDO_BYTES, _AGGREGATE_BYTES),
        )
        object.__setattr__(self, "committed_visible", profile.committed_visible)
        object.__setattr__(self, "failed_visible", profile.failed_visible)
        object.__setattr__(self, "_revalidate_owner", revalidate)
        object.__setattr__(self, "_execute", execute)
        object.__setattr__(self, "_validate_terminal", validate_terminal)
        object.__setattr__(
            self,
            "_seal",
            (
                compensation_kind,
                profile,
                revalidate,
                execute,
                validate_terminal,
            ),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action compensation handler is sealed")

    def __repr__(self) -> str:
        return f"<SealedProductActionCompensationHandlerV1 {self.compensation_kind}>"

    def __copy__(self) -> NoReturn:
        raise TypeError("Sealed compensation handlers cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("Sealed compensation handlers cannot be copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("Sealed compensation handlers cannot be serialized")


def _seal_product_action_compensation_handler(
    handler: _ProductActionCompensationHandlerV1,
    *,
    catalog: ProductActionCompensationCatalogV1,
) -> _SealedProductActionCompensationHandlerV1:
    return _SealedProductActionCompensationHandlerV1(
        handler,
        catalog=catalog,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _CompensationState:
    classification: Literal["all_absent", "exact_proposed", "exact_terminal"]
    operation: WriteOperation | None
    prefix: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, repr=False)
class _ProposalReconciliation:
    classification: Literal["absent", "proposed", "terminal"]
    result: ProductActionCompensationResultV1 | None = None


def _transition_id(operation_id: str, seq: int) -> str:
    return str(uuid5(_COMPENSATION_TRANSITION_NAMESPACE, f"{operation_id}:{seq}"))


def _append_transition(
    session: Session,
    operation_id: str,
    seq: int,
    state: str,
    created_at: datetime,
) -> None:
    session.add(
        WriteOperationTransition(
            id=_transition_id(operation_id, seq),
            operation_id=operation_id,
            seq=seq,
            state=state,
            created_at=created_at,
        )
    )


def _begin_immediate(session: Session) -> None:
    if session.in_transaction():
        raise ProductActionIntegrityError("product_action_compensation_uow")
    session.execute(text("BEGIN IMMEDIATE"))


class _ReadinessSignalCompensationHandlerV1:
    compensation_kind = "undo:save_review_readiness_signal"

    def __init__(
        self,
        repository: "ReadinessSignalRepository",
        revalidate_owner: Callable[[Session, _SignalUndoProofRecord], None],
        validate_terminal: Callable[
            [Session, _SignalUndoProofRecord, WriteOperation, dict[str, JSONValue]],
            None,
        ],
    ) -> None:
        self._repository = repository
        self._revalidate_owner = revalidate_owner
        self._validate_terminal = validate_terminal

    def revalidate_owner_in_session(
        self,
        session: Session,
        owner_record: object,
    ) -> None:
        if type(owner_record) is not _SignalUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        self._revalidate_owner(session, owner_record)

    def execute_in_session(
        self,
        session: Session,
        *,
        owner_record: object,
        compensation_operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        execution_uow: _CompensationExecutionUowV1,
    ) -> dict[str, JSONValue]:
        if type(owner_record) is not _SignalUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        domain = self._repository.retract_signal_in_session(
            session,
            signal_id=owner_record.signal_id,
            expected_current_version_id=cast(
                int,
                owner_record.validated_undo_json["expected_current_version_id"],
            ),
            expected_signal_revision=cast(
                int,
                owner_record.validated_undo_json["expected_signal_revision"],
            ),
            parent_operation_id=owner_record.parent_operation_id,
            compensation_operation_id=compensation_operation_id,
            authorization=authorization,
            authorization_binding=authorization_binding,
            execution_uow=execution_uow,
        )
        return domain

    def validate_terminal_in_session(
        self,
        session: Session,
        owner_record: object,
        operation: WriteOperation,
        result: dict[str, JSONValue],
    ) -> None:
        if type(owner_record) is not _SignalUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        self._validate_terminal(session, owner_record, operation, result)


def _revalidate_story_owner_in_session(
    session: Session,
    record: _StoryUndoProofRecord,
    key_profiles: LedgerKeyProfileStoreV1,
) -> None:
    story = session.get(InterviewStory, record.story_id)
    parent = session.get(WriteOperation, record.parent_operation_id)
    if story is None or story.current_version_id is None:
        raise ProductActionIntegrityError("product_action_compensation_owner")
    _validate_story_lifecycle_timestamps(story)
    if parent is not None:
        _validate_parent_transition_prefix(session, parent)
    digest, undo, result = _validate_story_parent(parent)
    if (
        digest != record.parent_terminal_payload_sha256
        or undo != dict(record.validated_undo_json)
        or result.get("story_id") != record.story_id
    ):
        raise ProductActionIntegrityError("product_action_compensation_parent_binding")
    attempt = _story_attempt_lineage(
        session,
        parent_operation_id=record.parent_operation_id,
        story_id=record.story_id,
        version_id=cast(int, result["story_version_id"]),
        outcome=cast(str, result["outcome"]),
    )
    if attempt.id != record.source_attempt_id:
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    created, projection = _story_version_snapshot(
        session,
        story_id=record.story_id,
        version_id=cast(int, result["story_version_id"]),
        expected_origin_kind="proposal",
    )
    _validate_story_attempt_version_time(attempt, created)
    if attempt.source_fingerprint != created.source_fingerprint:
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    created_digest = _story_version_snapshot_sha256(projection)
    if not hmac.compare_digest(
        created_digest,
        record.created_version_snapshot_sha256,
    ):
        raise ProductActionIntegrityError("interview_story_undo_version_integrity")
    confirmed_payload_digest = _validate_story_confirmation_payload(
        attempt,
        projection,
        undo,
    )
    if not hmac.compare_digest(
        confirmed_payload_digest,
        record.confirmed_effective_payload_sha256,
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_confirmation_payload"
        )
    previous_version_id: int | None = None
    if undo["kind"] == "restore_story_pointer_v1":
        previous_version_id = cast(int, undo["previous_current_version_id"])
        previous_digest, previous = _validated_previous_story_version_snapshot(
            session,
            story_id=story.id,
            previous_version_id=previous_version_id,
            previous_title=cast(str, undo["previous_title"]),
            key_profiles=key_profiles,
        )
        if (
            previous.version_number >= created.version_number
            or record.previous_version_snapshot_sha256 is None
            or not hmac.compare_digest(
                previous_digest,
                record.previous_version_snapshot_sha256,
            )
        ):
            raise ProductActionIntegrityError("interview_story_undo_previous_pointer")
    elif record.previous_version_snapshot_sha256 is not None:
        raise ProductActionIntegrityError("interview_story_undo_previous_pointer")
    history_digest = _story_history_snapshot_sha256(
        session,
        story_id=story.id,
        created_version_id=created.id,
        previous_version_id=previous_version_id,
    )
    if not hmac.compare_digest(
        history_digest,
        record.story_history_snapshot_sha256,
    ):
        raise ProductActionIntegrityError("interview_story_undo_history_integrity")
    _validate_story_title_relation(
        story,
        undo,
        created_title=_story_projection_title(projection),
    )
    drifted = (
        False
        if record.owner_state == "terminal_committed"
        else _validate_legitimate_story_drift(
            session,
            story=story,
            created=created,
            created_projection=projection,
            source_attempt=attempt,
            parent_result=result,
            undo=undo,
            key_profiles=key_profiles,
        )
    )
    if record.owner_state == "terminal_committed":
        if not _story_is_terminal_post_state(story, undo):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
    elif record.owner_state == "terminal_failed":
        if not drifted:
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
    elif record.owner_state != "active":
        raise ProductActionIntegrityError("product_action_compensation_owner_state")
    snapshot = _story_owner_snapshot_sha256(
        story,
        source_attempt_id=attempt.id,
        created_version_sha256=created_digest,
    )
    if (
        story.current_version_id != record.expected_current_version_id
        or story.story_revision != record.expected_story_revision
        or not hmac.compare_digest(snapshot, record.owner_snapshot_sha256)
    ):
        raise ProductActionIntegrityError("product_action_compensation_owner_snapshot")


def _validate_story_terminal_domain_in_session(
    session: Session,
    record: _StoryUndoProofRecord,
    operation: WriteOperation,
    result: dict[str, JSONValue],
    key_profiles: LedgerKeyProfileStoreV1,
) -> None:
    story = session.get(InterviewStory, record.story_id)
    parent = session.get(WriteOperation, record.parent_operation_id)
    if story is None:
        raise ProductActionIntegrityError("product_action_compensation_terminal_domain")
    _validate_story_lifecycle_timestamps(story)
    if parent is not None:
        _validate_parent_transition_prefix(session, parent)
    digest, undo, parent_result = _validate_story_parent(parent)
    if (
        digest != record.parent_terminal_payload_sha256
        or undo != dict(record.validated_undo_json)
        or parent_result.get("story_id") != record.story_id
    ):
        raise ProductActionIntegrityError("product_action_compensation_parent_binding")
    attempt = _story_attempt_lineage(
        session,
        parent_operation_id=record.parent_operation_id,
        story_id=record.story_id,
        version_id=cast(int, parent_result["story_version_id"]),
        outcome=cast(str, parent_result["outcome"]),
    )
    if attempt.id != record.source_attempt_id:
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    created, projection = _story_version_snapshot(
        session,
        story_id=story.id,
        version_id=cast(int, parent_result["story_version_id"]),
        expected_origin_kind="proposal",
    )
    _validate_story_attempt_version_time(attempt, created)
    if attempt.source_fingerprint != created.source_fingerprint:
        raise ProductActionIntegrityError("interview_story_undo_attempt_lineage")
    if not hmac.compare_digest(
        _story_version_snapshot_sha256(projection),
        record.created_version_snapshot_sha256,
    ):
        raise ProductActionIntegrityError("interview_story_undo_version_integrity")
    confirmed_payload_digest = _validate_story_confirmation_payload(
        attempt,
        projection,
        undo,
    )
    if not hmac.compare_digest(
        confirmed_payload_digest,
        record.confirmed_effective_payload_sha256,
    ):
        raise ProductActionIntegrityError(
            "interview_story_undo_confirmation_payload"
        )
    previous_version_id: int | None = None
    if undo["kind"] == "restore_story_pointer_v1":
        previous_version_id = cast(int, undo["previous_current_version_id"])
        previous_digest, previous = _validated_previous_story_version_snapshot(
            session,
            story_id=story.id,
            previous_version_id=previous_version_id,
            previous_title=cast(str, undo["previous_title"]),
            key_profiles=key_profiles,
        )
        if (
            previous.version_number >= created.version_number
            or record.previous_version_snapshot_sha256 is None
            or not hmac.compare_digest(
                previous_digest,
                record.previous_version_snapshot_sha256,
            )
        ):
            raise ProductActionIntegrityError(
                "interview_story_undo_previous_pointer"
            )
    elif record.previous_version_snapshot_sha256 is not None:
        raise ProductActionIntegrityError("interview_story_undo_previous_pointer")
    if not hmac.compare_digest(
        _story_history_snapshot_sha256(
            session,
            story_id=story.id,
            created_version_id=created.id,
            previous_version_id=previous_version_id,
        ),
        record.story_history_snapshot_sha256,
    ):
        raise ProductActionIntegrityError("interview_story_undo_history_integrity")
    _validate_story_title_relation(
        story,
        undo,
        created_title=_story_projection_title(projection),
    )
    if operation.status == "failed":
        if not _validate_legitimate_story_drift(
            session,
            story=story,
            created=created,
            created_projection=projection,
            source_attempt=attempt,
            parent_result=parent_result,
            undo=undo,
            key_profiles=key_profiles,
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
        return
    if not _story_is_terminal_post_state(story, undo):
        raise ProductActionIntegrityError("product_action_compensation_terminal_domain")
    if (
        result.get("story_id") != story.id
        or result.get("current_version_id") != story.current_version_id
        or result.get("story_revision") != story.story_revision
        or result.get("status") != story.status
    ):
        raise ProductActionIntegrityError("product_action_compensation_terminal_domain")


class _InterviewStoryCompensationHandlerV1:
    compensation_kind = "undo:confirm_interview_story"

    def __init__(
        self,
        repository: "InterviewStoriesRepository",
        key_profiles: LedgerKeyProfileStoreV1,
    ) -> None:
        self._repository = repository
        self._key_profiles = key_profiles

    def revalidate_owner_in_session(
        self,
        session: Session,
        owner_record: object,
    ) -> None:
        if type(owner_record) is not _StoryUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        _revalidate_story_owner_in_session(
            session,
            owner_record,
            self._key_profiles,
        )

    def execute_in_session(
        self,
        session: Session,
        *,
        owner_record: object,
        compensation_operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        execution_uow: _CompensationExecutionUowV1,
    ) -> dict[str, JSONValue]:
        if type(owner_record) is not _StoryUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        return self._repository.undo_product_action_in_session(
            session,
            story_id=owner_record.story_id,
            source_attempt_id=owner_record.source_attempt_id,
            parent_operation_id=owner_record.parent_operation_id,
            compensation_operation_id=compensation_operation_id,
            validated_undo_json=dict(owner_record.validated_undo_json),
            authorization=authorization,
            authorization_binding=authorization_binding,
            execution_uow=execution_uow,
        )

    def validate_terminal_in_session(
        self,
        session: Session,
        owner_record: object,
        operation: WriteOperation,
        result: dict[str, JSONValue],
    ) -> None:
        if type(owner_record) is not _StoryUndoProofRecord:
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_record"
            )
        _validate_story_terminal_domain_in_session(
            session,
            owner_record,
            operation,
            result,
            self._key_profiles,
        )


class ProductActionCompensationCoordinator:
    """Sole owner of Product compensation proposal, execution, and replay."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        catalog: ProductActionCompensationCatalogV1,
        proof_registry: ProductActionCompensationProofRegistryV1,
        execution_registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
        capability_check: Callable[[str], bool],
        readiness_repository: "ReadinessSignalRepository",
        story_repository: "InterviewStoriesRepository | None" = None,
    ) -> None:
        if (
            not callable(session_factory)
            or type(catalog) is not ProductActionCompensationCatalogV1
            or type(proof_registry) is not ProductActionCompensationProofRegistryV1
            or type(execution_registry) is not ProductActionProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or not callable(capability_check)
            or not callable(
                getattr(readiness_repository, "retract_signal_in_session", None)
            )
            or (
                story_repository is not None
                and not callable(
                    getattr(story_repository, "undo_product_action_in_session", None)
                )
            )
        ):
            raise TypeError("Product Action Compensation Coordinator composition is invalid")
        self._session_factory = session_factory
        self._catalog = catalog
        self._proof_registry = proof_registry
        if getattr(readiness_repository, "_proof_registry", None) is not execution_registry:
            raise TypeError(
                "Product Action Compensation execution Registry composition is invalid"
            )
        self._execution_registry = execution_registry
        self._execution_uow_issue_token = object()
        self._execution_uow_registry = _CompensationExecutionUowRegistryV1(
            self._execution_uow_issue_token
        )
        self._key_profiles = key_profiles
        self._capability_check = capability_check
        self._readiness_repository = readiness_repository
        signal_handler = _seal_product_action_compensation_handler(
            _ReadinessSignalCompensationHandlerV1(
                readiness_repository,
                self._validate_live_owner,
                self._validate_signal_terminal_domain,
            ),
            catalog=catalog,
        )
        handlers: dict[str, _SealedProductActionCompensationHandlerV1] = {
            signal_handler.compensation_kind: signal_handler,
        }
        if story_repository is not None:
            if getattr(story_repository, "_proof_registry", None) is not execution_registry:
                raise TypeError(
                    "Product Action Compensation Story Registry composition is invalid"
                )
            story_handler = _seal_product_action_compensation_handler(
                _InterviewStoryCompensationHandlerV1(
                    story_repository,
                    key_profiles,
                ),
                catalog=catalog,
            )
            handlers[story_handler.compensation_kind] = story_handler
        self._story_repository = story_repository
        self._handlers = MappingProxyType(handlers)

    def _handler(
        self,
        compensation_kind: str,
    ) -> _SealedProductActionCompensationHandlerV1:
        handler = self._handlers.get(compensation_kind)
        if handler is None:
            raise ProductActionIntegrityError(
                "product_action_compensation_handler_missing"
            )
        return handler

    @staticmethod
    def _load_state(session: Session, operation_id: str) -> _CompensationState:
        operation = session.get(WriteOperation, operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == operation_id)
                .order_by(WriteOperationTransition.seq, WriteOperationTransition.id)
            )
        )
        if operation is None and not transitions:
            return _CompensationState("all_absent", None, ())
        if operation is None or not transitions:
            raise ProductActionIntegrityError("partial_product_action_compensation")
        prefix = tuple((item.seq, item.state) for item in transitions)
        expected = EXPECTED_PREFIX.get(operation.status)
        if operation.status == "rejected" or expected is None or prefix != expected:
            raise ProductActionIntegrityError("product_action_compensation_transition_prefix")
        classification: Literal["exact_proposed", "exact_terminal"] = (
            "exact_proposed" if operation.status == "proposed" else "exact_terminal"
        )
        return _CompensationState(classification, operation, prefix)

    def _request_fingerprint(
        self,
        record: _UndoProofRecord,
        operation_id: str,
        *,
        key_id: str | None = None,
    ) -> str:
        key = self._key_profiles.active() if key_id is None else self._key_profiles.resolve(key_id)
        return product_action_compensation_request_fingerprint(
            key,
            operation_id=operation_id,
            parent_operation_id=record.parent_operation_id,
            parent_action_name=record.parent_action_name,
            parent_terminal_payload_sha256=record.parent_terminal_payload_sha256,
            compensation_kind=record.compensation_kind,
        )

    def _input_fingerprint(
        self,
        record: _UndoProofRecord,
        operation: WriteOperation,
    ) -> str:
        key = self._key_profiles.resolve(operation.fingerprint_key_id)
        return product_action_compensation_input_fingerprint(
            key,
            operation_request_fingerprint=cast(str, operation.operation_request_fingerprint),
            parent_terminal_payload_sha256=record.parent_terminal_payload_sha256,
            validated_undo_json=dict(record.validated_undo_json),
        )

    @staticmethod
    def _validate_proposed_operation(
        operation: WriteOperation,
        record: _UndoProofRecord,
        request_fingerprint: str,
    ) -> None:
        if (
            operation.operation_role != "compensation"
            or operation.parent_operation_id != record.parent_operation_id
            or operation.parent_terminal_payload_sha256
            != record.parent_terminal_payload_sha256
            or operation.conversation_id is not None
            or operation.agent_run_id is not None
            or operation.tool_call_id is not None
            or operation.tool_name != record.compensation_kind
            or operation.adapter_kind != "compensation"
            or operation.status != "proposed"
            or operation.proposal_fingerprint is not None
            or operation.confirmation_token_fingerprint is not None
            or operation.authorization_scope_fingerprint is not None
            or operation.input_fingerprint is not None
            or operation.operation_request_fingerprint is None
            or not hmac.compare_digest(
                operation.operation_request_fingerprint,
                request_fingerprint,
            )
            or operation.result_contract is not None
            or operation.result_json is not None
            or operation.visible_result is not None
            or operation.transport_json is not None
            or operation.undo_json is not None
            or operation.terminal_payload_sha256 is not None
            or operation.failure_category is not None
            or operation.failure_code is not None
            or operation.delivery_status != "pending"
            or operation.delivery_generation != 0
            or operation.delivery_outcome is not None
            or operation.delivery_message_count is not None
            or operation.delivery_owner_token_fingerprint is not None
            or operation.delivery_lease_expires_at is not None
            or operation.delivery_manifest_sha256 is not None
            or operation.delivery_next_operation_id is not None
            or operation.delivery_failure_code is not None
            or operation.approved_at is not None
            or operation.claimed_at is not None
            or operation.rejected_at is not None
            or operation.committed_at is not None
            or operation.failed_at is not None
            or operation.delivered_at is not None
            or type(operation.created_at) is not datetime
            or type(operation.updated_at) is not datetime
            or operation.updated_at != operation.created_at
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_proposed_shape"
            )

    @staticmethod
    def _validate_live_owner(
        session: Session,
        record: _SignalUndoProofRecord,
    ) -> None:
        signal = session.get(InterviewReadinessSignal, record.signal_id)
        parent = session.get(WriteOperation, record.parent_operation_id)
        if signal is None or signal.application_id != record.application_id:
            raise ProductActionIntegrityError("product_action_compensation_owner")
        original_current = cast(
            int,
            record.validated_undo_json["expected_current_version_id"],
        )
        original_revision = cast(
            int,
            record.validated_undo_json["expected_signal_revision"],
        )
        digest, undo, parent_result = _validate_signal_parent(parent)
        if parent is not None:
            _validate_parent_transition_prefix(session, parent)
        if (
            parent_result.get("signal_id") != record.signal_id
            or parent_result.get("signal_version_id") != original_current
            or parent_result.get("signal_revision") != original_revision
            or digest != record.parent_terminal_payload_sha256
            or undo != dict(record.validated_undo_json)
        ):
            raise ProductActionIntegrityError("product_action_compensation_parent_binding")
        active, active_evidence, active_projection = _signal_version_snapshot(
            session,
            signal=signal,
            version_id=original_current,
            expected_disposition="active",
            expected_write_operation_id=record.parent_operation_id,
            expected_parent_version_id=None,
            expected_version_number=1,
        )
        active_digest = _signal_active_aggregate_sha256(
            signal,
            active_projection,
        )
        if not hmac.compare_digest(
            active_digest,
            record.active_aggregate_sha256,
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_active_aggregate"
            )
        if record.owner_state == "active":
            drifted = _validate_legitimate_signal_drift(
                session,
                signal=signal,
                active=active,
                original_revision=original_revision,
            )
            if drifted:
                return
            if (
                signal.current_version_id != record.expected_current_version_id
                or signal.revision != record.expected_signal_revision
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_owner_changed"
                )
            snapshot = _signal_owner_snapshot_sha256(signal, active_projection)
        elif record.owner_state == "terminal_committed":
            if (
                signal.current_version_id != record.expected_current_version_id
                or signal.revision != record.expected_signal_revision
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_owner_changed"
                )
            retracted, retracted_evidence, retracted_projection = (
                _signal_version_snapshot(
                    session,
                    signal=signal,
                    version_id=record.expected_current_version_id,
                    expected_disposition="retracted",
                    expected_write_operation_id=product_action_compensation_operation_id(
                        record.parent_operation_id,
                        record.compensation_kind,
                    ),
                    expected_parent_version_id=original_current,
                    expected_version_number=active.version_number + 1,
                )
            )
            if retracted.domain_idempotency_key != readiness_signal_retraction_domain_key(
                retracted.write_operation_id
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_domain"
                )
            _validate_retracted_copy(
                active=active,
                active_evidence=active_evidence,
                retracted=retracted,
                retracted_evidence=retracted_evidence,
            )
            version_ids = tuple(
                session.scalars(
                    select(InterviewReadinessSignalVersion.id)
                    .where(InterviewReadinessSignalVersion.signal_id == signal.id)
                    .order_by(InterviewReadinessSignalVersion.version_number)
                )
            )
            if version_ids != (active.id, retracted.id):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_domain"
                )
            snapshot = _signal_owner_snapshot_sha256(
                signal,
                active_projection,
                retracted_projection,
            )
        else:
            if record.owner_state != "terminal_failed":
                raise ProductActionIntegrityError(
                    "product_action_compensation_owner_state"
                )
            if (
                signal.current_version_id != record.expected_current_version_id
                or signal.revision != record.expected_signal_revision
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_owner_changed"
                )
            if not _validate_legitimate_signal_drift(
                session,
                signal=signal,
                active=active,
                original_revision=original_revision,
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_domain"
                )
            snapshot = _signal_owner_snapshot_sha256(signal, active_projection)
        if not hmac.compare_digest(snapshot, record.owner_snapshot_sha256):
            raise ProductActionIntegrityError(
                "product_action_compensation_owner_snapshot"
            )

    @staticmethod
    def _validate_signal_terminal_domain(
        session: Session,
        record: _SignalUndoProofRecord,
        operation: WriteOperation,
        result: dict[str, JSONValue],
    ) -> None:
        parent = session.get(WriteOperation, record.parent_operation_id)
        if parent is not None:
            _validate_parent_transition_prefix(session, parent)
        digest, undo, parent_result = _validate_signal_parent(parent)
        original_version_id = cast(
            int,
            record.validated_undo_json["expected_current_version_id"],
        )
        original_revision = cast(
            int,
            record.validated_undo_json["expected_signal_revision"],
        )
        if (
            digest != record.parent_terminal_payload_sha256
            or undo != dict(record.validated_undo_json)
            or parent_result.get("signal_id") != record.signal_id
            or parent_result.get("signal_version_id") != original_version_id
            or parent_result.get("signal_revision") != original_revision
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_parent_binding"
            )
        signal = session.get(InterviewReadinessSignal, record.signal_id)
        if signal is None or signal.application_id != record.application_id:
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
        active, active_evidence, active_projection = _signal_version_snapshot(
            session,
            signal=signal,
            version_id=original_version_id,
            expected_disposition="active",
            expected_write_operation_id=record.parent_operation_id,
            expected_parent_version_id=None,
            expected_version_number=1,
        )
        active_digest = _signal_active_aggregate_sha256(signal, active_projection)
        if not hmac.compare_digest(
            active_digest,
            record.active_aggregate_sha256,
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_active_aggregate"
            )
        if operation.status == "failed":
            if not _validate_legitimate_signal_drift(
                session,
                signal=signal,
                active=active,
                original_revision=original_revision,
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_domain"
                )
            return
        retracted_version_id = result.get("retracted_version_id")
        if (
            type(retracted_version_id) is not int
            or signal.current_version_id != retracted_version_id
            or signal.revision != original_revision + 1
            or result.get("signal_revision") != signal.revision
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
        retracted, retracted_evidence, _retracted_projection = (
            _signal_version_snapshot(
                session,
                signal=signal,
                version_id=retracted_version_id,
                expected_disposition="retracted",
                expected_write_operation_id=operation.id,
                expected_parent_version_id=original_version_id,
                expected_version_number=active.version_number + 1,
            )
        )
        if retracted.domain_idempotency_key != readiness_signal_retraction_domain_key(
            operation.id
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )
        _validate_retracted_copy(
            active=active,
            active_evidence=active_evidence,
            retracted=retracted,
            retracted_evidence=retracted_evidence,
        )
        version_ids = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion.id)
                .where(InterviewReadinessSignalVersion.signal_id == signal.id)
                .order_by(InterviewReadinessSignalVersion.version_number)
            )
        )
        if version_ids != (active.id, retracted.id):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_domain"
            )

    def _validate_state(
        self,
        session: Session,
        state: _CompensationState,
        record: _UndoProofRecord,
        operation_id: str,
    ) -> ProductActionCompensationResultV1 | None:
        operation = state.operation
        if operation is None:
            if state.classification != "all_absent":
                raise ProductActionIntegrityError("partial_product_action_compensation")
            return None
        request = self._request_fingerprint(
            record,
            operation_id,
            key_id=operation.fingerprint_key_id,
        )
        if state.classification == "exact_proposed":
            self._validate_proposed_operation(operation, record, request)
            return None
        if (
            operation.operation_role != "compensation"
            or operation.parent_operation_id != record.parent_operation_id
            or operation.parent_terminal_payload_sha256
            != record.parent_terminal_payload_sha256
            or operation.conversation_id is not None
            or operation.agent_run_id is not None
            or operation.tool_call_id is not None
            or operation.tool_name != record.compensation_kind
            or operation.adapter_kind != "compensation"
            or operation.proposal_fingerprint is not None
            or operation.confirmation_token_fingerprint is not None
            or operation.authorization_scope_fingerprint is not None
            or operation.operation_request_fingerprint is None
            or not hmac.compare_digest(operation.operation_request_fingerprint, request)
            or operation.result_contract != "compensation_json_v1"
            or operation.undo_json is not None
            or operation.rejected_at is not None
            or operation.delivery_status != "not_applicable"
            or operation.delivery_generation != 0
            or operation.delivery_outcome != "none"
            or operation.delivery_message_count != 0
            or operation.delivery_owner_token_fingerprint is not None
            or operation.delivery_lease_expires_at is not None
            or operation.delivery_manifest_sha256 is not None
            or operation.delivery_next_operation_id is not None
            or operation.delivery_failure_code is not None
        ):
            raise ProductActionIntegrityError("product_action_compensation_terminal_shape")
        expected_input = self._input_fingerprint(record, operation)
        if operation.input_fingerprint is None or not hmac.compare_digest(
            operation.input_fingerprint,
            expected_input,
        ):
            raise ProductActionIntegrityError("product_action_compensation_input")
        if (
            operation.result_json is None
            or operation.visible_result is None
            or operation.transport_json is None
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_shape"
            )
        _enforce_compensation_terminal_budgets(
            operation.result_json,
            operation.visible_result,
            operation.transport_json,
            operation.undo_json,
        )
        try:
            payload_from_operation(operation)
        except Exception as exc:
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_digest"
            ) from exc
        result = _decode_json_object(
            operation.result_json,
            "product_action_compensation_result",
        )
        transport = _decode_json_object(
            operation.transport_json,
            "product_action_compensation_transport",
        )
        expected_transport: dict[str, JSONValue] = {
            "schema_version": 1,
            "operation_id": operation.id,
            "compensation_kind": record.compensation_kind,
            "status": operation.status,
            "result": result,
        }
        terminal_at = (
            operation.committed_at
            if operation.status == "committed"
            else operation.failed_at
        )
        if (
            operation.approved_at is None
            or operation.claimed_at is None
            or terminal_at is None
            or operation.delivered_at != terminal_at
            or transport != expected_transport
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_codec"
            )
        handler = self._handler(record.compensation_kind)
        if operation.status == "committed":
            if (
                result.get("kind") != handler.committed_result_kind
                or operation.visible_result != handler.committed_visible
                or operation.failure_category is not None
                or operation.failure_code is not None
                or operation.committed_at is None
                or operation.failed_at is not None
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_codec"
                )
            if type(record) is _SignalUndoProofRecord:
                if set(result) != {
                    "kind",
                    "signal_id",
                    "retracted_version_id",
                    "signal_revision",
                } or (
                    type(result.get("signal_id")) is not int
                    or result.get("signal_id") != record.signal_id
                    or type(result.get("retracted_version_id")) is not int
                    or cast(int, result.get("retracted_version_id")) < 1
                    or type(result.get("signal_revision")) is not int
                    or cast(int, result.get("signal_revision")) < 2
                ):
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_codec"
                    )
            elif type(record) is _StoryUndoProofRecord:
                if set(result) != {
                    "kind",
                    "story_id",
                    "current_version_id",
                    "story_revision",
                    "status",
                } or (
                    type(result.get("story_id")) is not int
                    or result.get("story_id") != record.story_id
                    or type(result.get("current_version_id")) is not int
                    or cast(int, result.get("current_version_id")) < 1
                    or type(result.get("story_revision")) is not int
                    or cast(int, result.get("story_revision")) < 2
                    or result.get("status") not in {"active", "archived"}
                ):
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_codec"
                    )
            else:
                raise ProductActionIntegrityError(
                    "product_action_compensation_owner_record"
                )
        elif set(result) != {"kind", "code"} or (
            result.get("kind") != handler.committed_result_kind
            or type(result.get("code")) is not str
            or result.get("code") != operation.failure_code
            or operation.visible_result != handler.failed_visible
            or operation.failure_category != "stale_state"
            or operation.failure_code not in handler.declared_stale_codes
            or operation.failed_at is None
            or operation.committed_at is not None
        ):
            raise ProductActionIntegrityError(
                "product_action_compensation_terminal_codec"
            )
        handler._validate_terminal(
            session,
            record,
            operation,
            result,
        )
        return ProductActionCompensationResultV1(
            operation.id,
            operation.tool_name,
            cast(Literal["committed", "failed"], operation.status),
            MappingProxyType(result),
            True,
        )

    def _publish(
        self,
        proof: _UndoProof,
        record: _UndoProofRecord,
        operation_id: str,
    ) -> tuple[
        _SignalUndoProofClaim,
        ProductActionCompensationResultV1 | None,
    ]:
        claim: _SignalUndoProofClaim | None = None
        for attempt in range(2):
            with self._session_factory() as session:
                try:
                    _begin_immediate(session)
                    handler = self._handler(record.compensation_kind)
                    if not self._capability_check(handler.capability):
                        raise ProductActionCompensationError(
                            "product_action_compensation_permission_denied",
                            status_code=403,
                        )
                    state = self._load_state(session, operation_id)
                    replay = self._validate_state(session, state, record, operation_id)
                    if replay is None:
                        handler._revalidate_owner(session, record)
                    if claim is None:
                        claim = self._proof_registry.claim(proof)
                        claimed_record = claim.__enter__()
                        if claimed_record is not record:
                            raise ProductActionIntegrityError(
                                "compensation_proof_registry_integrity"
                            )
                    if replay is not None:
                        session.rollback()
                        return claim, replay
                    if state.classification == "all_absent":
                        key = self._key_profiles.active()
                        request = self._request_fingerprint(record, operation_id)
                        now = datetime.now(timezone.utc)
                        operation = WriteOperation(
                            id=operation_id,
                            operation_role="compensation",
                            parent_operation_id=record.parent_operation_id,
                            parent_terminal_payload_sha256=(
                                record.parent_terminal_payload_sha256
                            ),
                            conversation_id=None,
                            agent_run_id=None,
                            tool_call_id=None,
                            tool_name=record.compensation_kind,
                            adapter_kind="compensation",
                            status="proposed",
                            fingerprint_key_id=key.key_id,
                            proposal_fingerprint=None,
                            input_fingerprint=None,
                            confirmation_token_fingerprint=None,
                            authorization_scope_fingerprint=None,
                            operation_request_fingerprint=request,
                            delivery_status="pending",
                            delivery_generation=0,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(operation)
                        session.flush()
                        _append_transition(session, operation_id, 1, "proposed", now)
                        session.flush()
                        reverse = self._load_state(session, operation_id)
                        self._validate_state(session, reverse, record, operation_id)
                    try:
                        session.commit()
                    except DBAPIError:
                        try:
                            session.rollback()
                        except BaseException:
                            pass
                        reconciled = self._reconcile_proposal(record, operation_id)
                        if reconciled.classification == "terminal":
                            if reconciled.result is None:
                                raise ProductActionIntegrityError(
                                    "product_action_compensation_terminal_missing"
                                )
                            return claim, reconciled.result
                        if reconciled.classification == "proposed":
                            return claim, None
                        if attempt == 0:
                            continue
                        raise ProductActionCompensationError(
                            "operation_result_unknown",
                            status_code=503,
                            retryable=True,
                        )
                    return claim, None
                except BaseException:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    raise
        raise ProductActionCompensationError(
            "operation_result_unknown",
            status_code=503,
            retryable=True,
        )

    def _reconcile_proposal(
        self,
        record: _UndoProofRecord,
        operation_id: str,
    ) -> _ProposalReconciliation:
        try:
            with self._session_factory() as session:
                state = self._load_state(session, operation_id)
                validated = self._validate_state(session, state, record, operation_id)
                if state.classification == "all_absent":
                    return _ProposalReconciliation("absent")
                if state.classification == "exact_proposed":
                    self._handler(record.compensation_kind)._revalidate_owner(
                        session,
                        record,
                    )
                    return _ProposalReconciliation("proposed")
                if validated is None:
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_missing"
                    )
                return _ProposalReconciliation("terminal", validated)
        except DBAPIError as exc:
            raise ProductActionCompensationError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            ) from exc

    def _reconcile_execution(
        self,
        record: _UndoProofRecord,
        operation_id: str,
    ) -> ProductActionCompensationResultV1:
        try:
            with self._session_factory() as session:
                state = self._load_state(session, operation_id)
                if state.classification == "all_absent":
                    raise ProductActionIntegrityError(
                        "product_action_compensation_execution_absent"
                    )
                validated = self._validate_state(session, state, record, operation_id)
                if state.classification == "exact_proposed":
                    raise ProductActionCompensationError(
                        "operation_result_unknown",
                        status_code=503,
                        retryable=True,
                    )
                if validated is None:
                    raise ProductActionIntegrityError(
                        "product_action_compensation_terminal_missing"
                    )
                return validated
        except DBAPIError as exc:
            raise ProductActionCompensationError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            ) from exc

    @staticmethod
    def _apply_terminal(
        operation: WriteOperation,
        *,
        status: Literal["committed", "failed"],
        input_fingerprint: str,
        result: dict[str, JSONValue],
        visible_result: str,
        transport: dict[str, JSONValue],
        failure_code: str | None,
        timestamp: datetime,
    ) -> None:
        payload = build_terminal_payload(
            status=status,
            result_contract="compensation_json_v1",
            result=result,
            visible_result=visible_result,
            transport=transport,
            undo=None,
            failure_category="stale_state" if status == "failed" else None,
            failure_code=failure_code,
            budgets=(_RESULT_BYTES, _VISIBLE_BYTES, _TRANSPORT_BYTES, _UNDO_BYTES),
        )
        _enforce_compensation_terminal_budgets(
            payload.result_json,
            payload.visible_result,
            payload.transport_json,
            payload.undo_json,
        )
        operation.status = status
        operation.input_fingerprint = input_fingerprint
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.undo_json = None
        operation.terminal_payload_sha256 = payload.digest
        operation.failure_category = payload.failure_category
        operation.failure_code = payload.failure_code
        operation.delivery_status = "not_applicable"
        operation.delivery_generation = 0
        operation.delivery_outcome = "none"
        operation.delivery_message_count = 0
        operation.delivery_owner_token_fingerprint = None
        operation.delivery_lease_expires_at = None
        operation.delivery_manifest_sha256 = None
        operation.delivery_next_operation_id = None
        operation.delivery_failure_code = None
        operation.delivered_at = timestamp
        operation.updated_at = timestamp
        operation.approved_at = timestamp
        operation.claimed_at = timestamp
        if status == "committed":
            operation.committed_at = timestamp
        else:
            operation.failed_at = timestamp

    def _execute_proposed(
        self,
        record: _UndoProofRecord,
        operation_id: str,
    ) -> ProductActionCompensationResultV1:
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                handler = self._handler(record.compensation_kind)
                if not self._capability_check(handler.capability):
                    raise ProductActionCompensationError(
                        "product_action_compensation_permission_denied",
                        status_code=403,
                    )
                handler._revalidate_owner(session, record)
                state = self._load_state(session, operation_id)
                replay = self._validate_state(session, state, record, operation_id)
                if replay is not None:
                    session.rollback()
                    return replay
                if state.classification != "exact_proposed" or state.operation is None:
                    raise ProductActionIntegrityError(
                        "product_action_compensation_execution_absent"
                    )
                operation = state.operation
                input_fingerprint = self._input_fingerprint(record, operation)
                now = datetime.now(timezone.utc)
                _append_transition(session, operation_id, 2, "approved", now)
                _append_transition(session, operation_id, 3, "claimed", now)
                session.flush()
                savepoint = session.begin_nested()
                authorization: ProductActionExecutionAuthorization | None = None
                authorization_binding: tuple[object, ...] | None = None
                execution_uow: _CompensationExecutionUowV1 | None = None
                try:
                    authorization_binding = (
                        "product_action_compensation_execution_v1",
                        operation_id,
                        record.parent_operation_id,
                        record.parent_terminal_payload_sha256,
                        record.compensation_kind,
                        canonical_product_action_json(
                            dict(record.validated_undo_json)
                        ),
                        input_fingerprint,
                    )
                    authorization = cast(
                        ProductActionExecutionAuthorization,
                        self._execution_registry._issue(
                            ProductActionExecutionAuthorization,
                            action_name=handler.parent_action_name,
                            binding=authorization_binding,
                            publication_refreshable=False,
                        ),
                    )

                    def terminalize_domain(
                        terminal_session: Session,
                        domain_result: object,
                    ) -> dict[str, JSONValue]:
                        if terminal_session is not session:
                            raise ProductActionIntegrityError(
                                "product_action_compensation_execution_uow_domain"
                            )
                        if type(record) is _SignalUndoProofRecord:
                            from offerpilot.review_readiness.repository import (
                                ReadinessSignalRetractionResultV1,
                            )

                            if type(domain_result) is not ReadinessSignalRetractionResultV1:
                                raise ProductActionIntegrityError(
                                    "product_action_compensation_execution_uow_domain"
                                )
                            projected_result: dict[str, JSONValue] = {
                                "signal_id": domain_result.signal_id,
                                "retracted_version_id": domain_result.retracted_version_id,
                                "signal_revision": domain_result.signal_revision,
                            }
                        elif type(record) is _StoryUndoProofRecord:
                            from offerpilot.repositories.interview_stories import (
                                StoryUndoResultV1,
                            )

                            if type(domain_result) is not StoryUndoResultV1:
                                raise ProductActionIntegrityError(
                                    "product_action_compensation_execution_uow_domain"
                                )
                            projected_result = {
                                "story_id": domain_result.story_id,
                                "current_version_id": domain_result.current_version_id,
                                "story_revision": domain_result.story_revision,
                                "status": domain_result.status,
                            }
                        else:
                            raise ProductActionIntegrityError(
                                "product_action_compensation_owner_record"
                            )
                        terminal_result: dict[str, JSONValue] = {
                            "kind": handler.committed_result_kind,
                            **projected_result,
                        }
                        terminal_transport: dict[str, JSONValue] = {
                            "schema_version": 1,
                            "operation_id": operation_id,
                            "compensation_kind": record.compensation_kind,
                            "status": "committed",
                            "result": terminal_result,
                        }
                        self._apply_terminal(
                            operation,
                            status="committed",
                            input_fingerprint=input_fingerprint,
                            result=terminal_result,
                            visible_result=handler.committed_visible,
                            transport=terminal_transport,
                            failure_code=None,
                            timestamp=now,
                        )
                        _append_transition(
                            terminal_session,
                            operation_id,
                            4,
                            "committed",
                            now,
                        )
                        terminal_session.flush()
                        reverse = self._load_state(terminal_session, operation_id)
                        if (
                            self._validate_state(
                                terminal_session,
                                reverse,
                                record,
                                operation_id,
                            )
                            is None
                        ):
                            raise ProductActionIntegrityError(
                                "product_action_compensation_terminal_missing"
                            )
                        return projected_result

                    execution_uow = self._execution_uow_registry._issue(
                        session,
                        issue_token=self._execution_uow_issue_token,
                        operation_id=operation_id,
                        parent_operation_id=record.parent_operation_id,
                        authorization_binding=authorization_binding,
                        terminalize=terminalize_domain,
                    )
                    projected = handler._execute(
                        session,
                        owner_record=record,
                        compensation_operation_id=operation_id,
                        authorization=authorization,
                        authorization_binding=authorization_binding,
                        execution_uow=execution_uow,
                    )
                    authorization_state = _execution_authorization_state(
                        self._execution_registry,
                        authorization,
                        action_name=handler.parent_action_name,
                        binding=authorization_binding,
                    )
                    if authorization_state != "consumed":
                        _revoke_execution_authorization_if_live(
                            self._execution_registry,
                            authorization,
                            action_name=handler.parent_action_name,
                            binding=authorization_binding,
                        )
                        raise ProductActionIntegrityError(
                            "product_action_compensation_authorization_unclaimed"
                        )
                    if self._execution_uow_registry._state(execution_uow) != "consumed":
                        self._execution_uow_registry._revoke(execution_uow)
                        raise ProductActionIntegrityError(
                            "product_action_compensation_execution_uow_unclaimed"
                        )
                except ProductActionCompensationStale as exc:
                    if savepoint.is_active:
                        savepoint.rollback()
                    if authorization is None or authorization_binding is None:
                        raise ProductActionIntegrityError(
                            "product_action_compensation_authorization_missing"
                        ) from exc
                    authorization_state = _revoke_execution_authorization_if_live(
                        self._execution_registry,
                        authorization,
                        action_name=handler.parent_action_name,
                        binding=authorization_binding,
                    )
                    if authorization_state != "revoked":
                        raise ProductActionIntegrityError(
                            "product_action_compensation_authorization_stale"
                        ) from exc
                    if execution_uow is None:
                        raise ProductActionIntegrityError(
                            "product_action_compensation_execution_uow_missing"
                        ) from exc
                    execution_uow_state = self._execution_uow_registry._state(
                        execution_uow
                    )
                    if execution_uow_state in {"issued", "in_flight"}:
                        execution_uow_state = self._execution_uow_registry._revoke(
                            execution_uow
                        )
                    if execution_uow_state != "revoked":
                        raise ProductActionIntegrityError(
                            "product_action_compensation_execution_uow_stale"
                        ) from exc
                    if exc.code not in handler.declared_stale_codes:
                        raise
                    result: dict[str, JSONValue] = {
                        "kind": handler.committed_result_kind,
                        "code": exc.code,
                    }
                    status: Literal["committed", "failed"] = "failed"
                    visible = handler.failed_visible
                    failure_code: str | None = exc.code
                except BaseException:
                    try:
                        if savepoint.is_active:
                            savepoint.rollback()
                    finally:
                        if authorization is not None and authorization_binding is not None:
                            _revoke_execution_authorization_if_live(
                                self._execution_registry,
                                authorization,
                                action_name=handler.parent_action_name,
                                binding=authorization_binding,
                            )
                        if execution_uow is not None:
                            execution_uow_state = self._execution_uow_registry._state(
                                execution_uow
                            )
                            if execution_uow_state in {"issued", "in_flight"}:
                                self._execution_uow_registry._revoke(execution_uow)
                        raise
                else:
                    savepoint.commit()
                    if type(projected) is not dict or "kind" in projected:
                        raise ProductActionIntegrityError(
                            "product_action_compensation_handler_projection"
                        )
                    result = {"kind": handler.committed_result_kind, **projected}
                    status = "committed"
                    visible = handler.committed_visible
                    failure_code = None
                if status == "failed":
                    transport: dict[str, JSONValue] = {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "compensation_kind": record.compensation_kind,
                        "status": status,
                        "result": result,
                    }
                    self._apply_terminal(
                        operation,
                        status=status,
                        input_fingerprint=input_fingerprint,
                        result=result,
                        visible_result=visible,
                        transport=transport,
                        failure_code=failure_code,
                        timestamp=now,
                    )
                    _append_transition(session, operation_id, 4, status, now)
                    session.flush()
                    reverse = self._load_state(session, operation_id)
                    validated = self._validate_state(
                        session,
                        reverse,
                        record,
                        operation_id,
                    )
                    if validated is None:
                        raise ProductActionIntegrityError(
                            "product_action_compensation_terminal_missing"
                        )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_execution(record, operation_id)
                return ProductActionCompensationResultV1(
                    operation_id,
                    record.compensation_kind,
                    status,
                    MappingProxyType(result),
                    False,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def execute(
        self,
        proof: _UndoProof,
    ) -> ProductActionCompensationResultV1:
        record = self._proof_registry.peek(proof)
        handler = self._handler(record.compensation_kind)
        if not self._capability_check(handler.capability):
            self._proof_registry.revoke(proof)
            raise ProductActionCompensationError(
                "product_action_compensation_permission_denied",
                status_code=403,
            )
        operation_id = product_action_compensation_operation_id(
            record.parent_operation_id,
            record.compensation_kind,
        )
        claim: _SignalUndoProofClaim | None = None
        try:
            claim, published = self._publish(proof, record, operation_id)
            if published is not None:
                result = published
            else:
                result = self._execute_proposed(record, operation_id)
        except BaseException as exc:
            if claim is not None:
                claim.__exit__(type(exc), exc, exc.__traceback__)
            else:
                self._proof_registry.revoke(proof)
            raise
        claim.__exit__(None, None, None)
        return result


__all__ = [
    "PRODUCT_ACTION_COMPENSATION_NAMESPACE",
    "READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE",
    "InterviewStoryProductActionUndoProof",
    "InterviewStoryUndoIssuer",
    "ProductActionCompensationCoordinator",
    "ProductActionCompensationError",
    "ProductActionCompensationProofRegistryV1",
    "ProductActionCompensationResultV1",
    "ProductActionCompensationStale",
    "ReadinessSignalProductActionUndoProof",
    "ReadinessSignalUndoIssuer",
    "product_action_compensation_input_fingerprint",
    "product_action_compensation_operation_id",
    "product_action_compensation_request_fingerprint",
    "readiness_signal_retraction_domain_key",
]
