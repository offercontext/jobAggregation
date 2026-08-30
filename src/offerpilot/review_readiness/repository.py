"""Session-bound aggregate writes and exact reads for readiness Signals."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.models import (
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.contracts import (
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    JSONValue,
    canonical_product_action_json,
)
from offerpilot.product_actions.compensation import (
    ProductActionCompensationStale,
    _CompensationExecutionUowClaimV1,
    _CompensationExecutionUowV1,
    readiness_signal_retraction_domain_key,
)
from offerpilot.review_readiness.contracts import (
    ReadinessCandidateV1,
    ReadinessEvidenceV1,
    ReadinessSignalAggregateV1,
    ReadinessSignalWriteResultV1,
)


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessSignalRetractionResultV1:
    signal_id: int
    retracted_version_id: int
    signal_revision: int


_COMPENSATION_DOMAIN_CLAIM_SEAL = object()


class _CompensationDomainClaimV1:
    __slots__ = ("_incarnation", "_nonce", "_repository_token")

    def __init__(
        self,
        construction_seal: object,
        repository_token: object,
        incarnation: object,
        nonce: int,
    ) -> None:
        if construction_seal is not _COMPENSATION_DOMAIN_CLAIM_SEAL:
            raise TypeError("Compensation domain claims are Repository-created")
        if type(nonce) is not int or nonce < 1:
            raise TypeError("Compensation domain claim nonce is invalid")
        self._repository_token = repository_token
        self._incarnation = incarnation
        self._nonce = nonce


def _exact_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductActionIntegrityError(f"{field}_exact_positive_integer")
    return value


def _canonical_uuid(value: object, field: str) -> str:
    if type(value) is not str:
        raise ProductActionIntegrityError(f"{field}_invalid_uuid")
    try:
        normalized = str(UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ProductActionIntegrityError(f"{field}_invalid_uuid") from exc
    if normalized != value:
        raise ProductActionIntegrityError(f"{field}_invalid_uuid")
    return normalized


def _sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ProductActionIntegrityError(f"{field}_invalid_sha256")
    return value


def _utf8_bytes(value: object, field: str, limit: int) -> bytes:
    if type(value) is not str:
        raise ProductActionIntegrityError(f"{field}_invalid_text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductActionIntegrityError(f"{field}_invalid_text") from exc
    if len(encoded) > limit:
        raise ProductActionIntegrityError(f"{field}_too_large")
    return encoded


class ReadinessSignalRepository:
    """Own Signal aggregate writes but never transaction lifecycle or Ledger state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        proof_registry: ProductActionProofRegistryV1,
    ) -> None:
        if not callable(session_factory) or type(proof_registry) is not ProductActionProofRegistryV1:
            raise TypeError("Readiness Signal Repository composition is invalid")
        self._session_factory = session_factory
        self._proof_registry = proof_registry
        self._compensation_domain_registry_token = object()
        self._compensation_domain_registry_incarnation = object()
        self._compensation_domain_registry_lock = RLock()
        self._compensation_domain_registry_next_nonce = 1
        self._compensation_domain_registry_records: dict[
            int,
            tuple[
                _CompensationDomainClaimV1,
                ProductActionExecutionAuthorization,
                tuple[object, ...],
                int,
            ],
        ] = {}

    def create_signal_in_session(
        self,
        session: Session,
        *,
        candidate: ReadinessCandidateV1,
        user_note: str,
        domain_idempotency_key: str,
        operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
    ) -> ReadinessSignalWriteResultV1:
        if type(candidate) is not ReadinessCandidateV1:
            raise TypeError("Readiness Signal write requires an exact candidate")
        with self._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="save_review_readiness_signal",
            expected_binding=authorization_binding,
        ):
            existing = session.scalar(
                select(InterviewReadinessSignal).where(
                    InterviewReadinessSignal.source_proposal_id
                    == candidate.proposal_id,
                    InterviewReadinessSignal.focus_id == candidate.focus_id,
                )
            )
            if existing is not None:
                raise ProductActionIntegrityError("readiness_signal_duplicate")
            now = datetime.now(timezone.utc)
            signal = InterviewReadinessSignal(
                application_id=candidate.application_id,
                source_event_id=candidate.event_id,
                source_note_id=candidate.note_id,
                source_proposal_id=candidate.proposal_id,
                focus_id=candidate.focus_id,
                current_version_id=None,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(signal)
            session.flush()
            version = InterviewReadinessSignalVersion(
                signal_id=signal.id,
                version_number=1,
                parent_version_id=None,
                disposition="active",
                schema_version="readiness-signal-v1",
                statement_text=candidate.statement_text,
                user_note=user_note,
                source_note_revision=candidate.source_note_revision,
                source_note_fingerprint=candidate.source_note_fingerprint,
                source_proposal_hash=candidate.source_proposal_hash,
                candidate_fingerprint=candidate.candidate_fingerprint,
                domain_idempotency_key=domain_idempotency_key,
                write_operation_id=operation_id,
                created_at=now,
            )
            session.add(version)
            session.flush()
            session.add_all(
                InterviewReadinessSignalEvidence(
                    signal_version_id=version.id,
                    ordinal=item.ordinal,
                    source_path=item.source_path,
                    excerpt=item.excerpt,
                    excerpt_sha256=item.excerpt_sha256,
                    source_field_sha256=item.source_field_sha256,
                )
                for item in candidate.evidence
            )
            session.flush()
            stored_ordinals = tuple(
                session.scalars(
                    select(InterviewReadinessSignalEvidence.ordinal)
                    .where(
                        InterviewReadinessSignalEvidence.signal_version_id
                        == version.id
                    )
                    .order_by(InterviewReadinessSignalEvidence.ordinal)
                )
            )
            if stored_ordinals != tuple(range(len(candidate.evidence))):
                raise ProductActionIntegrityError("readiness_signal_evidence_prefix")
            signal.current_version_id = version.id
            signal.updated_at = now
            session.flush()
            return ReadinessSignalWriteResultV1(signal.id, version.id, signal.revision)

    def load_by_operation_in_session(
        self,
        session: Session,
        operation_id: str,
    ) -> ReadinessSignalAggregateV1 | None:
        version = session.scalar(
            select(InterviewReadinessSignalVersion).where(
                InterviewReadinessSignalVersion.write_operation_id == operation_id
            )
        )
        if version is None:
            return None
        signal = session.get(InterviewReadinessSignal, version.signal_id)
        if signal is None:
            raise ProductActionIntegrityError("readiness_signal_missing")
        if signal.current_version_id != version.id:
            raise ProductActionIntegrityError("readiness_signal_pointer")
        evidence_rows = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(
                    InterviewReadinessSignalEvidence.signal_version_id == version.id
                )
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        if not 1 <= len(evidence_rows) <= 5 or tuple(
            row.ordinal for row in evidence_rows
        ) != tuple(range(len(evidence_rows))):
            raise ProductActionIntegrityError("readiness_signal_evidence_prefix")
        evidence = tuple(
            ReadinessEvidenceV1(
                row.ordinal,
                row.source_path,
                row.excerpt,
                row.excerpt_sha256,
                row.source_field_sha256,
            )
            for row in evidence_rows
        )
        return ReadinessSignalAggregateV1(
            signal_id=signal.id,
            application_id=signal.application_id,
            source_event_id=signal.source_event_id,
            source_note_id=signal.source_note_id,
            source_proposal_id=signal.source_proposal_id,
            focus_id=signal.focus_id,
            current_version_id=signal.current_version_id,
            signal_revision=signal.revision,
            version_number=version.version_number,
            disposition=version.disposition,
            statement_text=version.statement_text,
            user_note=version.user_note,
            source_note_revision=version.source_note_revision,
            source_note_fingerprint=version.source_note_fingerprint,
            source_proposal_hash=version.source_proposal_hash,
            candidate_fingerprint=version.candidate_fingerprint,
            domain_idempotency_key=version.domain_idempotency_key,
            write_operation_id=version.write_operation_id,
            evidence=evidence,
        )

    def retract_signal_in_session(
        self,
        session: Session,
        *,
        signal_id: int,
        expected_current_version_id: int,
        expected_signal_revision: int,
        parent_operation_id: str,
        compensation_operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        execution_uow: _CompensationExecutionUowV1 | None = None,
    ) -> dict[str, JSONValue]:
        """Append a retracted Version without owning the surrounding transaction."""

        if (
            type(authorization_binding) is not tuple
            or len(authorization_binding) != 7
            or authorization_binding[0]
            != "product_action_compensation_execution_v1"
            or authorization_binding[1] != compensation_operation_id
            or authorization_binding[2] != parent_operation_id
            or authorization_binding[4] != "undo:save_review_readiness_signal"
            or type(authorization_binding[5]) is not str
            or type(authorization_binding[6]) is not str
            or len(authorization_binding[6]) != 76
            or not authorization_binding[6].startswith("hmac-sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in authorization_binding[6][12:]
            )
        ):
            try:
                self._proof_registry.revoke(authorization)
            except (TypeError, ValueError):
                pass
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_authorization_binding"
            )
        if type(execution_uow) is not _CompensationExecutionUowV1:
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_execution_uow"
            )
        try:
            execution_claim_context = execution_uow._claim_signal(
                session,
                operation_id=compensation_operation_id,
                parent_operation_id=parent_operation_id,
                authorization_binding=authorization_binding,
            )
        except AttributeError as exc:
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_execution_uow"
            ) from exc
        with execution_claim_context as execution_claim:
            with self._proof_registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="save_review_readiness_signal",
                expected_binding=authorization_binding,
            ):
                with self._compensation_domain_registry_lock:
                    nonce = self._compensation_domain_registry_next_nonce
                    self._compensation_domain_registry_next_nonce += 1
                    claim = _CompensationDomainClaimV1(
                        _COMPENSATION_DOMAIN_CLAIM_SEAL,
                        self._compensation_domain_registry_token,
                        self._compensation_domain_registry_incarnation,
                        nonce,
                    )
                    self._compensation_domain_registry_records[id(claim)] = (
                        claim,
                        authorization,
                        authorization_binding,
                        nonce,
                    )
                try:
                    return self._retract_signal_authorized_in_session(
                        session,
                        signal_id=signal_id,
                        expected_current_version_id=expected_current_version_id,
                        expected_signal_revision=expected_signal_revision,
                        parent_operation_id=parent_operation_id,
                        compensation_operation_id=compensation_operation_id,
                        authorization=authorization,
                        authorization_binding=authorization_binding,
                        domain_claim=claim,
                        execution_claim=execution_claim,
                    )
                finally:
                    with self._compensation_domain_registry_lock:
                        self._compensation_domain_registry_records.pop(id(claim), None)

    def _retract_signal_authorized_in_session(
        self,
        session: Session,
        *,
        signal_id: int,
        expected_current_version_id: int,
        expected_signal_revision: int,
        parent_operation_id: str,
        compensation_operation_id: str,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        domain_claim: _CompensationDomainClaimV1,
        execution_claim: _CompensationExecutionUowClaimV1 | None = None,
    ) -> dict[str, JSONValue]:

        with self._compensation_domain_registry_lock:
            record = self._compensation_domain_registry_records.pop(
                id(domain_claim),
                None,
            )
            if (
                type(domain_claim) is not _CompensationDomainClaimV1
                or record is None
                or record[0] is not domain_claim
                or record[1] is not authorization
                or record[2] != authorization_binding
                or record[3] != domain_claim._nonce
                or domain_claim._repository_token
                is not self._compensation_domain_registry_token
                or domain_claim._incarnation
                is not self._compensation_domain_registry_incarnation
            ):
                raise ProductActionIntegrityError(
                    "readiness_signal_compensation_domain_claim"
                )
        if type(execution_claim) is not _CompensationExecutionUowClaimV1:
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_execution_uow"
            )

        signal_id = _exact_positive_int(signal_id, "signal_id")
        expected_current_version_id = _exact_positive_int(
            expected_current_version_id,
            "expected_current_version_id",
        )
        expected_signal_revision = _exact_positive_int(
            expected_signal_revision,
            "expected_signal_revision",
        )
        parent_operation_id = _canonical_uuid(
            parent_operation_id,
            "parent_operation_id",
        )
        compensation_operation_id = _canonical_uuid(
            compensation_operation_id,
            "compensation_operation_id",
        )
        compensation = session.get(WriteOperation, compensation_operation_id)
        compensation_prefix = tuple(
            (row.seq, row.state)
            for row in session.execute(
                select(
                    WriteOperationTransition.seq,
                    WriteOperationTransition.state,
                )
                .where(
                    WriteOperationTransition.operation_id
                    == compensation_operation_id
                )
                .order_by(WriteOperationTransition.seq)
            )
        )
        if (
            compensation is None
            or compensation.operation_role != "compensation"
            or compensation.adapter_kind != "compensation"
            or compensation.tool_name != "undo:save_review_readiness_signal"
            or compensation.parent_operation_id != parent_operation_id
            or compensation.status != "proposed"
            or compensation_prefix
            != ((1, "proposed"), (2, "approved"), (3, "claimed"))
        ):
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_operation"
            )
        expected_undo_json = canonical_product_action_json(
            {
                "kind": "retract_review_readiness_signal_v1",
                "signal_id": signal_id,
                "created_version_id": expected_current_version_id,
                "expected_current_version_id": expected_current_version_id,
                "expected_signal_revision": expected_signal_revision,
                "parent_operation_id": parent_operation_id,
            }
        )
        if (
            authorization_binding[3] != compensation.parent_terminal_payload_sha256
            or authorization_binding[5] != expected_undo_json
        ):
            raise ProductActionIntegrityError(
                "readiness_signal_compensation_authorization_binding"
            )
        signal = session.get(InterviewReadinessSignal, signal_id)
        if signal is None or type(signal.revision) is not int:
            raise ProductActionIntegrityError("readiness_signal_owner_integrity")
        if (
            signal.current_version_id != expected_current_version_id
            or signal.revision != expected_signal_revision
        ):
            raise ProductActionCompensationStale("readiness_signal_undo_stale")
        parent = session.get(
            InterviewReadinessSignalVersion,
            expected_current_version_id,
        )
        if (
            parent is None
            or parent.signal_id != signal.id
            or type(parent.version_number) is not int
            or parent.version_number < 1
            or parent.disposition != "active"
            or parent.schema_version != "readiness-signal-v1"
            or parent.write_operation_id != parent_operation_id
            or type(parent.source_note_revision) is not int
            or parent.source_note_revision < 1
        ):
            raise ProductActionIntegrityError("readiness_signal_parent_integrity")
        statement_bytes = _utf8_bytes(
            parent.statement_text,
            "readiness_signal_statement",
            4_096,
        )
        del statement_bytes
        _utf8_bytes(parent.user_note, "readiness_signal_user_note", 2_048)
        for field in (
            "source_note_fingerprint",
            "source_proposal_hash",
            "candidate_fingerprint",
        ):
            _sha256(getattr(parent, field), field)
        evidence_rows = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(
                    InterviewReadinessSignalEvidence.signal_version_id == parent.id
                )
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        if not 1 <= len(evidence_rows) <= 5 or tuple(
            row.ordinal for row in evidence_rows
        ) != tuple(range(len(evidence_rows))):
            raise ProductActionIntegrityError("readiness_signal_evidence_prefix")
        total_excerpt_bytes = 0
        for row in evidence_rows:
            if row.source_path not in {
                "/questions",
                "/self_reflection",
                "/difficulty_points",
                "/mood",
            }:
                raise ProductActionIntegrityError("readiness_signal_evidence_path")
            excerpt_bytes = _utf8_bytes(
                row.excerpt,
                "readiness_signal_evidence_excerpt",
                8_192,
            )
            total_excerpt_bytes += len(excerpt_bytes)
            if total_excerpt_bytes > 16_384:
                raise ProductActionIntegrityError(
                    "readiness_signal_evidence_excerpt_total_too_large"
                )
            if _sha256(row.excerpt_sha256, "excerpt_sha256") != (
                "sha256:" + hashlib.sha256(excerpt_bytes).hexdigest()
            ):
                raise ProductActionIntegrityError("readiness_signal_evidence_excerpt_hash")
            _sha256(row.source_field_sha256, "source_field_sha256")
        now = datetime.now(timezone.utc)
        retracted = InterviewReadinessSignalVersion(
            signal_id=signal.id,
            version_number=parent.version_number + 1,
            parent_version_id=parent.id,
            disposition="retracted",
            schema_version="readiness-signal-v1",
            statement_text=parent.statement_text,
            user_note=parent.user_note,
            source_note_revision=parent.source_note_revision,
            source_note_fingerprint=parent.source_note_fingerprint,
            source_proposal_hash=parent.source_proposal_hash,
            candidate_fingerprint=parent.candidate_fingerprint,
            domain_idempotency_key=readiness_signal_retraction_domain_key(
                compensation_operation_id
            ),
            write_operation_id=compensation_operation_id,
            created_at=now,
        )
        session.add(retracted)
        session.flush()
        session.add_all(
            InterviewReadinessSignalEvidence(
                signal_version_id=retracted.id,
                ordinal=row.ordinal,
                source_path=row.source_path,
                excerpt=row.excerpt,
                excerpt_sha256=row.excerpt_sha256,
                source_field_sha256=row.source_field_sha256,
            )
            for row in evidence_rows
        )
        session.flush()
        copied_ordinals = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence.ordinal)
                .where(
                    InterviewReadinessSignalEvidence.signal_version_id == retracted.id
                )
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        if copied_ordinals != tuple(range(len(evidence_rows))):
            raise ProductActionIntegrityError("readiness_signal_evidence_copy")
        signal.current_version_id = retracted.id
        signal.revision += 1
        signal.updated_at = now
        session.flush()
        domain_result = ReadinessSignalRetractionResultV1(
            signal.id,
            retracted.id,
            signal.revision,
        )
        try:
            return execution_claim.terminalize_signal(session, domain_result)
        except BaseException:
            session.rollback()
            raise

    def load_by_operation(self, operation_id: str) -> ReadinessSignalAggregateV1 | None:
        with self._session_factory() as session:
            return self.load_by_operation_in_session(session, operation_id)


__all__ = ["ReadinessSignalRepository", "ReadinessSignalRetractionResultV1"]
