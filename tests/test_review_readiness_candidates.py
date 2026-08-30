from __future__ import annotations

from sqlalchemy import update

from offerpilot.db import init_database
from offerpilot.models import InterviewNote, InterviewReviewProposal
from offerpilot.review_readiness.candidates import project_readiness_candidates

from tests.review_readiness_support import seed_review_candidate


def test_candidate_projection_requires_current_v2_completed_evidence(tmp_path) -> None:
    session_factory = init_database(tmp_path / "candidate.sqlite3")
    seeded = seed_review_candidate(session_factory)

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"],
            seeded["proposal_id"],
            session,
        )

    assert projection.state == "ready"
    assert len(projection.candidates) == 1
    candidate = projection.candidates[0]
    assert candidate.focus_id == seeded["focus_id"]
    assert candidate.statement_text == seeded["focus_text"]
    assert candidate.source_note_fingerprint.startswith("sha256:")
    assert candidate.source_proposal_hash.startswith("sha256:")
    assert candidate.candidate_fingerprint.startswith("sha256:")
    assert candidate.candidate_fingerprint == (
        "sha256:75de245cac6dd566e0a0098b0d0a8f2af2e510b7f267031bdf81e8cd5a8ee4a1"
    )
    assert candidate.evidence[0].source_path == "/difficulty_points"
    assert candidate.evidence[0].excerpt == "cache consistency tradeoffs"


def test_candidate_projection_closes_legacy_and_noncompleted_sources(tmp_path) -> None:
    legacy_factory = init_database(tmp_path / "legacy.sqlite3")
    legacy = seed_review_candidate(legacy_factory, proposal_schema_version=1)
    with legacy_factory() as session:
        legacy_projection = project_readiness_candidates(
            legacy["note_id"], legacy["proposal_id"], session
        )
    assert legacy_projection.state == "legacy_requires_regeneration"

    pending_factory = init_database(tmp_path / "pending.sqlite3")
    pending = seed_review_candidate(pending_factory, event_status="pending")
    with pending_factory() as session:
        pending_projection = project_readiness_candidates(
            pending["note_id"], pending["proposal_id"], session
        )
    assert pending_projection.state == "not_eligible"


def test_candidate_projection_preserves_order_and_caps_at_eight(tmp_path) -> None:
    focuses = [
        {
            "id": f"focus-{index}",
            "text": f"Preparation focus {index}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for index in range(10)
    ]
    session_factory = init_database(tmp_path / "ordered.sqlite3")
    seeded = seed_review_candidate(session_factory, practice_focuses=focuses)

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )

    assert projection.state == "ready"
    assert [item.focus_id for item in projection.candidates] == [
        f"focus-{index}" for index in range(8)
    ]


def test_candidate_projection_fails_closed_on_source_or_proposal_tamper(tmp_path) -> None:
    note_factory = init_database(tmp_path / "note-tamper.sqlite3")
    note_seeded = seed_review_candidate(note_factory)
    with note_factory() as session:
        session.execute(
            update(InterviewNote)
            .where(InterviewNote.id == note_seeded["note_id"])
            .values(content_revision=InterviewNote.content_revision + 1)
        )
        session.commit()
    with note_factory() as session:
        note_projection = project_readiness_candidates(
            note_seeded["note_id"], note_seeded["proposal_id"], session
        )
    assert note_projection.state == "source_changed"

    proposal_factory = init_database(tmp_path / "proposal-tamper.sqlite3")
    proposal_seeded = seed_review_candidate(proposal_factory)
    with proposal_factory() as session:
        session.execute(
            update(InterviewReviewProposal)
            .where(InterviewReviewProposal.id == proposal_seeded["proposal_id"])
            .values(proposal_json='{"practice_focuses":[]}')
        )
        session.commit()
    with proposal_factory() as session:
        proposal_projection = project_readiness_candidates(
            proposal_seeded["note_id"], proposal_seeded["proposal_id"], session
        )
    assert proposal_projection.state == "not_eligible"


def test_candidate_projection_enforces_codepoint_and_utf8_caps(tmp_path) -> None:
    session_factory = init_database(tmp_path / "caps.sqlite3")
    seeded = seed_review_candidate(session_factory, focus_text="界" * 1_001)

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )

    assert projection.state == "not_eligible"
