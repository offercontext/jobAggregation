"""Owner-scoped, Provider-invisible Product Action compensation core."""

from __future__ import annotations

import hmac
import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    return ledger_fingerprint(
        key,
        "product-action-compensation-request-v1",
        {
            "request_kind": "product_action_compensation_v1",
            "operation_id": normalized_operation,
            "parent_operation_id": normalized_parent,
            "parent_action_name": parent_action_name,
            "parent_terminal_payload_sha256": parent_terminal_payload_sha256,
            "compensation_kind": compensation_kind,
        },
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


@dataclass(slots=True, repr=False)
class _LiveUndoProofRecord:
    proof: ReadinessSignalProductActionUndoProof
    value: _SignalUndoProofRecord
    state: Literal["issued", "in_flight"]


class _SignalUndoProofClaim(AbstractContextManager[_SignalUndoProofRecord]):
    __slots__ = ("_registry", "_proof", "_record", "_closed")

    def __init__(
        self,
        registry: "ProductActionCompensationProofRegistryV1",
        proof: ReadinessSignalProductActionUndoProof,
        record: _LiveUndoProofRecord,
    ) -> None:
        self._registry = registry
        self._proof = proof
        self._record = record
        self._closed = False

    def __enter__(self) -> _SignalUndoProofRecord:
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
        ReadinessSignalProductActionUndoProof,
        Literal["consumed", "revoked"],
    ]
    _lock: RLock
    _seal: tuple[
        object,
        object,
        dict[int, _LiveUndoProofRecord],
        WeakKeyDictionary[
            ReadinessSignalProductActionUndoProof,
            Literal["consumed", "revoked"],
        ],
        RLock,
    ]

    def __init__(self) -> None:
        registry_token = object()
        incarnation = object()
        records: dict[int, _LiveUndoProofRecord] = {}
        retired: WeakKeyDictionary[
            ReadinessSignalProductActionUndoProof,
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

    def _record(
        self,
        proof: object,
    ) -> _LiveUndoProofRecord:
        self._ensure_integrity()
        if type(proof) is not ReadinessSignalProductActionUndoProof:
            raise ProductActionCompensationError(
                "product_action_compensation_proof_invalid",
                status_code=404,
            )
        try:
            if proof._seal != (
                proof._registry_token,
                proof._incarnation,
                proof._nonce,
            ) or (
                proof._registry_token is not self._registry_token
                or proof._incarnation is not self._incarnation
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
        record = self._records.get(id(proof))
        if record is None or record.proof is not proof:
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

    def _finish(
        self,
        proof: ReadinessSignalProductActionUndoProof,
        record: _LiveUndoProofRecord,
        state: Literal["consumed", "revoked"],
    ) -> None:
        with self._lock:
            if record.state != "in_flight" or self._records.pop(id(proof), None) is not record:
                raise ProductActionIntegrityError("compensation_proof_registry_integrity")
            self._retired[proof] = state

    def revoke(self, proof: object) -> None:
        if type(proof) is not ReadinessSignalProductActionUndoProof:
            return
        with self._lock:
            record = self._records.pop(id(proof), None)
            if record is not None and record.proof is proof:
                self._retired[proof] = "revoked"


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
        result.get("schema_version") != 1
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
    catalog: ProductActionCompensationCatalogV1 | None = None,
) -> _SealedProductActionCompensationHandlerV1:
    return _SealedProductActionCompensationHandlerV1(
        handler,
        catalog=catalog or ProductActionCompensationCatalogV1(),
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
        record: _SignalUndoProofRecord,
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
        record: _SignalUndoProofRecord,
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
        record: _SignalUndoProofRecord,
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
        record: _SignalUndoProofRecord,
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
            if record.compensation_kind == "undo:save_review_readiness_signal" and (
                set(result)
                != {
                    "kind",
                    "signal_id",
                    "retracted_version_id",
                    "signal_revision",
                }
                or type(result.get("signal_id")) is not int
                or result.get("signal_id") != record.signal_id
                or type(result.get("retracted_version_id")) is not int
                or cast(int, result.get("retracted_version_id")) < 1
                or type(result.get("signal_revision")) is not int
                or cast(int, result.get("signal_revision")) < 2
            ):
                raise ProductActionIntegrityError(
                    "product_action_compensation_terminal_codec"
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
        proof: ReadinessSignalProductActionUndoProof,
        record: _SignalUndoProofRecord,
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
                        claim = self._proof_registry.claim_signal(proof)
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
        record: _SignalUndoProofRecord,
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
        record: _SignalUndoProofRecord,
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
        record: _SignalUndoProofRecord,
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

                    def terminalize_signal(
                        terminal_session: Session,
                        domain_result: object,
                    ) -> dict[str, JSONValue]:
                        from offerpilot.review_readiness.repository import (
                            ReadinessSignalRetractionResultV1,
                        )

                        if (
                            terminal_session is not session
                            or type(domain_result)
                            is not ReadinessSignalRetractionResultV1
                        ):
                            raise ProductActionIntegrityError(
                                "product_action_compensation_execution_uow_domain"
                            )
                        projected_result: dict[str, JSONValue] = {
                            "signal_id": domain_result.signal_id,
                            "retracted_version_id": domain_result.retracted_version_id,
                            "signal_revision": domain_result.signal_revision,
                        }
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
                        terminalize=terminalize_signal,
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
        proof: ReadinessSignalProductActionUndoProof,
    ) -> ProductActionCompensationResultV1:
        record = self._proof_registry.peek_signal(proof)
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
