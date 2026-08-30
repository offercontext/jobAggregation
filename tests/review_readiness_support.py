from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.interview_review_proposals import build_interview_review_snapshot
from offerpilot.models import (
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReviewProposal,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text


def seed_review_candidate(
    session_factory: sessionmaker[Session],
    *,
    event_status: str = "completed",
    proposal_schema_version: int = 2,
    focus_id: str = "focus-1",
    focus_text: str = "下次先澄清约束条件，再说明缓存一致性的取舍。",
    evidence_excerpt: str = "cache consistency tradeoffs",
    difficulty_points: str = "I struggled to explain cache consistency tradeoffs.",
    practice_focuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        application = Application(
            company_name="Acme",
            position_name="Backend",
            source="web",
        )
        session.add(application)
        session.flush()
        event = ApplicationEvent(
            application_id=application.id,
            event_type="interview",
            subtype="technical",
            round=2,
            scheduled_at=datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
            duration_minutes=45,
            status=event_status,
        )
        note = InterviewNote(
            application_id=application.id,
            company="Acme",
            position="Backend",
            round="technical",
            date="2026-08-20",
            questions="How would you design a cache?",
            self_reflection="I should clarify constraints before choosing a strategy.",
            difficulty_points=difficulty_points,
            mood="nervous",
        )
        session.add_all((event, note))
        session.flush()
        note.application_event_id = event.id
        session.commit()
    with session_factory() as session:
        note = session.get(InterviewNote, note.id)
        event = session.get(ApplicationEvent, event.id)
        application = session.get(Application, application.id)
        assert note is not None and event is not None and application is not None
        snapshot = build_interview_review_snapshot(note, event)
        snapshot_json = canonical_json(snapshot)
        proposal_payload = {
            "summary": {
                "text": "The review identifies one grounded preparation focus.",
                "evidence_refs": [
                    {
                        "source": "interview_note",
                        "path": "/self_reflection",
                        "excerpt": "clarify constraints",
                    }
                ],
            },
            "observations": [],
            "clarifications": [],
            "practice_focuses": practice_focuses
            or [
                {
                    "id": focus_id,
                    "text": focus_text,
                    "evidence_refs": [
                        {
                            "source": "interview_note",
                            "path": "/difficulty_points",
                            "excerpt": evidence_excerpt,
                        }
                    ],
                }
            ],
            "next_questions": [],
        }
        proposal_json = canonical_json(proposal_payload)
        proposal = InterviewReviewProposal(
            note_id=note.id,
            application_event_id=event.id,
            idempotency_key="review-proposal-1",
            input_snapshot_json=snapshot_json,
            source_fingerprint=sha256_text(snapshot_json),
            proposal_json=proposal_json,
            proposal_hash=sha256_text(proposal_json),
            proposal_schema_version=proposal_schema_version,
            source_note_revision=(
                note.content_revision if proposal_schema_version == 2 else None
            ),
        )
        session.add(proposal)
        session.commit()
        return {
            "application_id": application.id,
            "event_id": event.id,
            "note_id": note.id,
            "proposal_id": proposal.id,
            "note_revision": note.content_revision,
            "focus_id": focus_id,
            "focus_text": focus_text,
        }
