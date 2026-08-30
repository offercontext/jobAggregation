from __future__ import annotations

from datetime import timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.interview_review_proposals import (
    InterviewReviewDiagnosticSink,
    build_interview_review_snapshot,
    generate_interview_review_proposal,
)
from offerpilot.models import Application, ApplicationEvent, InterviewNote, InterviewReviewProposal
from offerpilot.repositories.json_contract import canonical_json, parse_json_object, sha256_text


class InterviewReviewNotFound(Exception):
    pass


class InterviewReviewEventRequired(ValueError):
    pass


class InterviewReviewValidationError(ValueError):
    pass


class InterviewReviewConflictError(ValueError):
    pass


class InterviewReviewProposalsRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def list(self, note_id: int) -> list[InterviewReviewProposal]:
        with self._session_factory() as session:
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewReviewNotFound()
            proposals = list(
                session.scalars(
                    select(InterviewReviewProposal)
                    .where(InterviewReviewProposal.note_id == note_id)
                    .order_by(
                        InterviewReviewProposal.created_at.desc(),
                        InterviewReviewProposal.id.desc(),
                    )
                )
            )
            return [_with_source_status(session, note, proposal) for proposal in proposals]

    def get(self, note_id: int, proposal_id: int) -> InterviewReviewProposal | None:
        with self._session_factory() as session:
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewReviewNotFound()
            proposal = session.scalar(
                select(InterviewReviewProposal)
                .where(InterviewReviewProposal.note_id == note_id)
                .where(InterviewReviewProposal.id == proposal_id)
            )
            if proposal is None:
                return None
            return _with_source_status(session, note, proposal)

    def get_by_idempotency_key(
        self, note_id: int, idempotency_key: str
    ) -> InterviewReviewProposal | None:
        with self._session_factory() as session:
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewReviewNotFound()
            proposal = _find_by_key(session, note_id, idempotency_key)
            if proposal is None:
                return None
            return _with_source_status(session, note, proposal)

    def create_generated(
        self,
        note_id: int,
        idempotency_key: str,
        model: ChatModel,
        *,
        on_diagnostic: InterviewReviewDiagnosticSink | None = None,
    ) -> tuple[InterviewReviewProposal, bool]:
        with self._session_factory() as session:
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewReviewNotFound()
            existing = _find_by_key(session, note_id, idempotency_key)
            if existing is not None:
                return _with_source_status(session, note, existing), False
            snapshot = _current_snapshot(session, note)
            snapshot_json = canonical_json(snapshot)
            source_fingerprint = sha256_text(snapshot_json)
            source_note_revision = note.content_revision
            source_resource = (
                note.id,
                note.application_id,
                note.application_event_id,
                snapshot["event"]["id"],
                snapshot["event"]["application_id"],
            )

        proposal = generate_interview_review_proposal(
            model,
            snapshot,
            on_diagnostic=on_diagnostic,
        )
        proposal_json = canonical_json(proposal)
        proposal_hash = sha256_text(proposal_json)

        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewReviewNotFound()
            existing = _find_by_key(session, note_id, idempotency_key)
            if existing is not None:
                return _with_source_status(session, note, existing), False
            current_snapshot = _current_snapshot(session, note)
            current_fingerprint = sha256_text(canonical_json(current_snapshot))
            current_resource = (
                note.id,
                note.application_id,
                note.application_event_id,
                current_snapshot["event"]["id"],
                current_snapshot["event"]["application_id"],
            )
            if (
                current_resource != source_resource
                or note.content_revision != source_note_revision
                or current_fingerprint != source_fingerprint
            ):
                raise InterviewReviewConflictError("interview review source changed")
            event_id = int(current_snapshot["event"]["id"])
            stored = InterviewReviewProposal(
                note_id=note_id,
                application_event_id=event_id,
                idempotency_key=idempotency_key,
                input_snapshot_json=snapshot_json,
                source_fingerprint=source_fingerprint,
                proposal_json=proposal_json,
                proposal_hash=proposal_hash,
                proposal_schema_version=2,
                source_note_revision=source_note_revision,
            )
            session.add(stored)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                existing = _find_by_key(session, note_id, idempotency_key)
                if existing is None:
                    raise InterviewReviewConflictError("interview review could not be stored") from exc
                return _with_source_status(session, note, existing), False
            session.refresh(stored)
            setattr(stored, "source_status", "current")
            _normalize_timestamp(stored)
            return stored, True


def _visible_note(session: Session, note_id: int) -> InterviewNote | None:
    return session.scalar(
        select(InterviewNote)
        .outerjoin(Application, Application.id == InterviewNote.application_id)
        .where(InterviewNote.id == note_id)
        .where(
            (InterviewNote.application_id.is_(None))
            | (Application.deleted_at.is_(None))
        )
    )


def _visible_event(session: Session, event_id: int) -> ApplicationEvent | None:
    return session.scalar(
        select(ApplicationEvent)
        .join(Application, Application.id == ApplicationEvent.application_id)
        .where(ApplicationEvent.id == event_id)
        .where(Application.deleted_at.is_(None))
    )


def _current_snapshot(session: Session, note: InterviewNote) -> dict[str, Any]:
    if note.application_event_id is None:
        raise InterviewReviewEventRequired()
    event = _visible_event(session, note.application_event_id)
    if (
        event is None
        or event.event_type != "interview"
        or note.application_id is None
        or event.application_id != note.application_id
    ):
        raise InterviewReviewNotFound()
    return build_interview_review_snapshot(note, event)


def _find_by_key(
    session: Session, note_id: int, idempotency_key: str
) -> InterviewReviewProposal | None:
    return session.scalar(
        select(InterviewReviewProposal)
        .where(InterviewReviewProposal.note_id == note_id)
        .where(InterviewReviewProposal.idempotency_key == idempotency_key)
    )


def _with_source_status(
    session: Session,
    note: InterviewNote,
    proposal: InterviewReviewProposal,
) -> InterviewReviewProposal:
    source_status = "source_changed"
    try:
        stored_snapshot = parse_json_object("interview review snapshot", proposal.input_snapshot_json)
        stored_event_id = stored_snapshot.get("event", {}).get("id")
        if note.application_event_id is not None and note.application_event_id == stored_event_id:
            event = _visible_event(session, note.application_event_id)
            if (
                event is not None
                and event.event_type == "interview"
                and note.application_id is not None
                and event.application_id == note.application_id
            ):
                current_snapshot = build_interview_review_snapshot(note, event)
                revision_matches = (
                    proposal.proposal_schema_version == 1
                    or (
                        proposal.proposal_schema_version == 2
                        and proposal.source_note_revision == note.content_revision
                    )
                )
                if (
                    revision_matches
                    and sha256_text(canonical_json(current_snapshot))
                    == proposal.source_fingerprint
                ):
                    source_status = "current"
    except (TypeError, ValueError, KeyError):
        source_status = "source_changed"
    setattr(proposal, "source_status", source_status)
    _normalize_timestamp(proposal)
    return proposal


def _normalize_timestamp(proposal: InterviewReviewProposal) -> None:
    value = proposal.created_at
    if value.tzinfo is None or value.utcoffset() is None:
        proposal.created_at = value.replace(tzinfo=timezone.utc)
    else:
        proposal.created_at = value.astimezone(timezone.utc)
