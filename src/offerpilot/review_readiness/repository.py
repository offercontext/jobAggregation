"""Session-bound aggregate writes and exact reads for readiness Signals."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.models import (
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
)
from offerpilot.product_actions.contracts import (
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
)
from offerpilot.review_readiness.contracts import (
    ReadinessCandidateV1,
    ReadinessEvidenceV1,
    ReadinessSignalAggregateV1,
    ReadinessSignalWriteResultV1,
)


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

    def load_by_operation(self, operation_id: str) -> ReadinessSignalAggregateV1 | None:
        with self._session_factory() as session:
            return self.load_by_operation_in_session(session, operation_id)


__all__ = ["ReadinessSignalRepository"]
