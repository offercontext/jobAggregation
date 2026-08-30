from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from offerpilot.db import init_database
from offerpilot.models import (
    Application,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReviewProposal,
)
import offerpilot.review_readiness.candidates as candidates_module
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


@pytest.mark.parametrize(("count", "expected_state"), [(5, "ready"), (6, "not_eligible")])
def test_candidate_projection_enforces_evidence_count_boundary(
    tmp_path,
    count,
    expected_state,
) -> None:
    evidence = [
        {
            "source": "interview_note",
            "path": "/difficulty_points",
            "excerpt": "cache consistency tradeoffs",
        }
        for _index in range(count)
    ]
    session_factory = init_database(tmp_path / f"evidence-{count}.sqlite3")
    seeded = seed_review_candidate(
        session_factory,
        practice_focuses=[
            {"id": "focus-boundary", "text": "Boundary", "evidence_refs": evidence}
        ],
    )

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )

    assert projection.state == expected_state


def test_candidate_projection_rejects_duplicate_focus_identity(tmp_path) -> None:
    focus = {
        "id": "duplicate-focus",
        "text": "Boundary",
        "evidence_refs": [
            {
                "source": "interview_note",
                "path": "/difficulty_points",
                "excerpt": "cache consistency tradeoffs",
            }
        ],
    }
    session_factory = init_database(tmp_path / "duplicate-focus.sqlite3")
    seeded = seed_review_candidate(
        session_factory,
        practice_focuses=[focus, dict(focus)],
    )

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )

    assert projection.state == "not_eligible"


@pytest.mark.parametrize(
    "event_status",
    ["todo", "in_progress", "cancelled", "unrecognized-status"],
)
def test_candidate_projection_rejects_every_noncompleted_lifecycle(
    tmp_path,
    event_status,
) -> None:
    session_factory = init_database(tmp_path / f"lifecycle-{event_status}.sqlite3")
    seeded = seed_review_candidate(session_factory, event_status=event_status)

    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )

    assert projection.state == "not_eligible"


def test_candidate_projection_exposes_source_missing_unavailable_and_confirmed_states(
    tmp_path,
) -> None:
    missing_factory = init_database(tmp_path / "source-missing.sqlite3")
    with missing_factory() as session:
        missing = project_readiness_candidates(999_991, 999_992, session)
        unavailable = project_readiness_candidates(True, 1, session)
    assert missing.state == "source_missing"
    assert unavailable.state == "unavailable"

    deleted_factory = init_database(tmp_path / "source-unavailable.sqlite3")
    deleted = seed_review_candidate(deleted_factory)
    with deleted_factory() as session:
        application = session.get(Application, deleted["application_id"])
        assert application is not None
        application.deleted_at = datetime.now(timezone.utc)
        session.commit()
    with deleted_factory() as session:
        projection = project_readiness_candidates(
            deleted["note_id"], deleted["proposal_id"], session
        )
    assert projection.state == "unavailable"

    confirmed_factory = init_database(tmp_path / "already-confirmed.sqlite3")
    confirmed = seed_review_candidate(confirmed_factory)
    with confirmed_factory() as session:
        session.add(
            InterviewReadinessSignal(
                application_id=confirmed["application_id"],
                source_event_id=confirmed["event_id"],
                source_note_id=confirmed["note_id"],
                source_proposal_id=confirmed["proposal_id"],
                focus_id=confirmed["focus_id"],
                revision=1,
            )
        )
        session.commit()
    with confirmed_factory() as session:
        projection = project_readiness_candidates(
            confirmed["note_id"], confirmed["proposal_id"], session
        )
    assert projection.state == "already_confirmed"
    assert [item.focus_id for item in projection.candidates] == [confirmed["focus_id"]]


@pytest.mark.parametrize(
    ("focus_id", "expected_state"),
    [("f" * 128, "ready"), ("f" * 129, "not_eligible")],
)
def test_candidate_projection_enforces_focus_id_utf8_boundary(
    tmp_path,
    focus_id,
    expected_state,
) -> None:
    session_factory = init_database(tmp_path / f"focus-id-{len(focus_id)}.sqlite3")
    seeded = seed_review_candidate(session_factory, focus_id=focus_id)
    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )
    assert projection.state == expected_state


@pytest.mark.parametrize(
    ("codepoints", "expected_state"),
    [(2_000, "ready"), (2_001, "not_eligible")],
)
def test_candidate_projection_enforces_excerpt_codepoint_and_utf8_boundary(
    tmp_path,
    codepoints,
    expected_state,
) -> None:
    excerpt = "😀" * codepoints
    session_factory = init_database(tmp_path / f"excerpt-{codepoints}.sqlite3")
    seeded = seed_review_candidate(
        session_factory,
        evidence_excerpt=excerpt,
        difficulty_points=excerpt,
    )
    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )
    assert len(excerpt.encode("utf-8")) == codepoints * 4
    assert projection.state == expected_state


@pytest.mark.parametrize(
    ("extra_byte", "expected_state"),
    [(False, "ready"), (True, "not_eligible")],
)
def test_candidate_projection_enforces_exact_total_evidence_byte_boundary(
    tmp_path,
    extra_byte,
    expected_state,
) -> None:
    first = "界" * 2_000
    second = "文" * 2_000
    third = "测" * 1_461 + ("xx" if extra_byte else "x")
    evidence = [
        {
            "source": "interview_note",
            "path": "/difficulty_points",
            "excerpt": excerpt,
        }
        for excerpt in (first, second, third)
    ]
    difficulty_points = first + second + third
    session_factory = init_database(tmp_path / f"total-{extra_byte}.sqlite3")
    seeded = seed_review_candidate(
        session_factory,
        difficulty_points=difficulty_points,
        practice_focuses=[
            {"id": "focus-total", "text": "Boundary", "evidence_refs": evidence}
        ],
    )
    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )
    assert sum(len(item["excerpt"].encode("utf-8")) for item in evidence) == (
        16_385 if extra_byte else 16_384
    )
    assert projection.state == expected_state


@pytest.mark.parametrize(
    ("envelope_bytes", "expected_state"),
    [(32_768, "ready"), (32_769, "not_eligible")],
)
def test_candidate_projection_enforces_canonical_envelope_byte_boundary(
    tmp_path,
    monkeypatch,
    envelope_bytes,
    expected_state,
) -> None:
    session_factory = init_database(tmp_path / f"envelope-{envelope_bytes}.sqlite3")
    seeded = seed_review_candidate(session_factory)
    monkeypatch.setattr(
        candidates_module,
        "canonical_product_action_json",
        lambda _value: "x" * envelope_bytes,
    )
    with session_factory() as session:
        projection = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        )
    assert projection.state == expected_state
