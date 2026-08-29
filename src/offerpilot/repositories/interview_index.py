from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, exists, func, nullslast, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from offerpilot.ai.interview_review_proposals import build_interview_review_snapshot
from offerpilot.models import (
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReviewProposal,
    KnowledgeCapturedSourceMetadata,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text


@dataclass(frozen=True)
class InterviewIndexItem:
    application_id: int
    event_id: int
    company_name: str
    position_name: str
    scheduled_at: object
    note_id: int | None
    note_source_status: str | None
    has_review_proposal: bool
    review_summary: str | None
    has_confirmed_knowledge: bool
    event_status: str
    duration_minutes: int | None
    scheduled_at_state: str
    preparation_available: bool


class InterviewIndexRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def list(self, *, limit: int = 50, cursor: str = "") -> tuple[list[InterviewIndexItem], str | None]:
        offset = _parse_cursor(cursor)
        statement = _event_index_statement()
        statement = statement.offset(offset).limit(limit + 1)
        with self._session_factory() as session:
            rows = session.execute(statement).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [_item(row) for row in rows]
        return items, str(offset + limit) if has_more else None

    def get(self, event_id: int) -> InterviewIndexItem | None:
        statement = _event_index_statement().where(ApplicationEvent.id == event_id)
        with self._session_factory() as session:
            row = session.execute(statement).first()
            return None if row is None else _item(row)


def _event_index_statement() -> Select[Any]:
    """Build the shared, one-row-per-interview-event read projection."""
    note_ranked = (
        select(
            InterviewNote.id.label("note_id"),
            InterviewNote.application_event_id,
            func.row_number()
            .over(
                partition_by=InterviewNote.application_event_id,
                order_by=(InterviewNote.created_at.desc(), InterviewNote.id.desc()),
            )
            .label("note_rn"),
        )
        .where(InterviewNote.application_event_id.is_not(None))
        .subquery("latest_event_notes")
    )
    review_ranked = (
        select(
            InterviewReviewProposal.id.label("review_id"),
            InterviewReviewProposal.application_event_id,
            func.row_number()
            .over(
                partition_by=InterviewReviewProposal.application_event_id,
                order_by=(
                    InterviewReviewProposal.created_at.desc(),
                    InterviewReviewProposal.id.desc(),
                ),
            )
            .label("review_rn"),
        )
        .where(InterviewReviewProposal.application_event_id.is_not(None))
        .subquery("latest_event_reviews")
    )
    has_confirmed_knowledge = exists(
        select(1).where(
            KnowledgeCapturedSourceMetadata.application_event_id == ApplicationEvent.id
        )
    )
    return (
        select(
            ApplicationEvent,
            Application.company_name,
            Application.position_name,
            InterviewNote,
            InterviewReviewProposal,
            has_confirmed_knowledge.label("has_confirmed_knowledge"),
        )
        .join(Application, Application.id == ApplicationEvent.application_id)
        .outerjoin(
            note_ranked,
            and_(
                note_ranked.c.application_event_id == ApplicationEvent.id,
                note_ranked.c.note_rn == 1,
            ),
        )
        .outerjoin(InterviewNote, InterviewNote.id == note_ranked.c.note_id)
        .outerjoin(
            review_ranked,
            and_(
                review_ranked.c.application_event_id == ApplicationEvent.id,
                review_ranked.c.review_rn == 1,
            ),
        )
        .outerjoin(
            InterviewReviewProposal,
            InterviewReviewProposal.id == review_ranked.c.review_id,
        )
        .where(Application.deleted_at.is_(None))
        .where(ApplicationEvent.event_type == "interview")
        .order_by(
            nullslast(ApplicationEvent.scheduled_at.asc()),
            ApplicationEvent.created_at.desc(),
            ApplicationEvent.id.desc(),
        )
    )


def _parse_cursor(value: str) -> int:
    if not value:
        return 0
    try:
        offset = int(value)
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if offset < 0:
        raise ValueError("cursor must be a non-negative integer")
    return offset


def _item(
    row: Any,
) -> InterviewIndexItem:
    event, company_name, position_name, note, review, has_confirmed_knowledge = row
    scheduled_at = event.scheduled_at or datetime.min
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    scheduled_at_state = "present" if event.scheduled_at is not None else "absent"
    duration = event.duration_minutes
    preparation_available = (
        event.status in {"todo", "pending", "scheduled", "in_progress"}
        and event.scheduled_at is not None
        and type(duration) is int
        and 1 <= duration <= 10080
    )
    return InterviewIndexItem(
        application_id=event.application_id,
        event_id=event.id,
        company_name=company_name,
        position_name=position_name,
        scheduled_at=scheduled_at,
        note_id=note.id if note is not None else None,
        note_source_status=_note_source_status(event, note, review),
        has_review_proposal=review is not None,
        review_summary=_review_summary(review),
        has_confirmed_knowledge=bool(has_confirmed_knowledge),
        event_status=event.status,
        duration_minutes=duration,
        scheduled_at_state=scheduled_at_state,
        preparation_available=preparation_available,
    )


def _review_summary(proposal: InterviewReviewProposal | None) -> str | None:
    if proposal is None:
        return None
    try:
        payload = json.loads(proposal.proposal_json)
        summary = payload.get("summary") if isinstance(payload, dict) else None
        text = summary.get("text") if isinstance(summary, dict) else None
        return text if isinstance(text, str) and text else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _note_source_status(
    event: ApplicationEvent,
    note: InterviewNote | None,
    proposal: InterviewReviewProposal | None,
) -> str | None:
    if proposal is None:
        return "current" if note is not None else None
    if note is None or note.application_event_id != event.id:
        return "source_changed"
    try:
        current = build_interview_review_snapshot(note, event)
        return "current" if sha256_text(canonical_json(current)) == proposal.source_fingerprint else "source_changed"
    except (TypeError, ValueError, KeyError):
        return "source_changed"
