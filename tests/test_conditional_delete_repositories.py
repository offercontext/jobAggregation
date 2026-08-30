from datetime import datetime, timezone

import pytest

from offerpilot.db import init_database
from offerpilot.models import (
    APPLICATION_FOREIGN_KEY_MODELS,
    ApplicationEvent,
    ApplicationEvidenceBundle,
    ApplicationJDVersion,
    ApplicationMaterialKit,
    ApplicationOutcome,
    ApplicationSubmissionSnapshot,
    MaterialRevisionProposal,
    OpportunityFitReview,
    OpportunityFitReviewSession,
    OpportunityFitReviewStage,
    Base,
    InterviewNote,
    InterviewReadinessSignal,
    JDAnalysis,
    Offer,
    Question,
    Resume,
    ResumeMatch,
)
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.notes import NoteCreate, NotesRepository


def _application_dependency(model, application_id):
    if model is ApplicationJDVersion:
        return model(
            application_id=application_id,
            version_number=1,
            jd_text="JD",
            content_sha256="0" * 64,
            source_kind="ui",
            idempotency_key="application-jd-version-dependency",
            request_fingerprint_sha256="1" * 64,
        )
    if model is ApplicationEvent:
        return model(application_id=application_id, event_type="interview")
    if model is InterviewNote:
        return model(application_id=application_id, company="A", position="Engineer")
    if model is InterviewReadinessSignal:
        return model(application_id=application_id, focus_id="conditional-delete-focus")
    if model is Offer:
        return model(application_id=application_id, company_name="A", position_name="Engineer")
    if model is JDAnalysis:
        return model(application_id=application_id, jd_text="JD", result="{}")
    if model is ApplicationMaterialKit:
        return model(application_id=application_id)
    if model is ApplicationEvidenceBundle:
        return model(
            application_id=application_id,
            sequence=1,
            submitted_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            confirmed_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            confirmation_kind="user_asserted",
            idempotency_key="8f4a6b48-b554-49a0-bccf-b1bf211ef824",
            snapshot_json="{}",
            bundle_sha256="0" * 64,
        )
    if model is MaterialRevisionProposal:
        return model(
            application_id=application_id,
            material_kit_id=1,
            source_fingerprint_sha256="0" * 64,
            source_snapshot_json="{}",
            proposal_json="{}",
            proposal_sha256="1" * 64,
        )
    if model is OpportunityFitReview:
        return model(
            application_id=application_id,
            idempotency_key="8f4a6b48-b554-49a0-bccf-b1bf211ef824",
            source_fingerprint_sha256="0" * 64,
            source_snapshot_json="{}",
            triage_json="{}",
            triage_sha256="1" * 64,
        )
    if model is OpportunityFitReviewSession:
        return model(
            application_id=application_id,
            triage_idempotency_key="8f4a6b48-b554-49a0-bccf-b1bf211ef824",
        )
    if model is Question:
        return model(application_id=application_id, question="Why?")
    raise AssertionError(f"dependency {model.__name__} needs related rows")


def test_application_dependency_test_matrix_covers_every_model_fk():
    actual = {
        mapper.class_
        for mapper in Base.registry.mappers
        if any(
            foreign_key.target_fullname == "applications.id"
            for column in mapper.local_table.columns
            for foreign_key in column.foreign_keys
        )
    }

    assert set(APPLICATION_FOREIGN_KEY_MODELS) == actual


@pytest.mark.parametrize("dependency_model", APPLICATION_FOREIGN_KEY_MODELS)
def test_application_delete_if_matches_rejects_every_fk_dependency(tmp_path, dependency_model):
    session_factory = init_database(tmp_path / "data.db")
    repo = ApplicationsRepository(session_factory)
    app = repo.create(ApplicationCreate(company_name="A", position_name="Engineer"))
    expected = {
        "company_name": app.company_name,
        "position_name": app.position_name,
        "job_url": app.job_url,
        "status": app.status,
        "source": app.source,
        "notes": app.notes,
        "applied_at": app.applied_at.isoformat(),
        "closed_reason": app.closed_reason,
        "updated_at": app.updated_at.isoformat(),
    }
    with session_factory() as session:
        if dependency_model is ResumeMatch:
            resume = Resume(name="Main")
            session.add(resume)
            session.flush()
            dependency = ResumeMatch(
                resume_id=resume.id,
                application_id=app.id,
                jd_text="JD",
                result="{}",
            )
        elif dependency_model is MaterialRevisionProposal:
            kit = ApplicationMaterialKit(application_id=app.id)
            session.add(kit)
            session.flush()
            dependency = _application_dependency(dependency_model, app.id)
            dependency.material_kit_id = kit.id
        elif dependency_model is OpportunityFitReviewStage:
            review_session = OpportunityFitReviewSession(
                application_id=app.id,
                triage_idempotency_key="stage-parent-review",
            )
            session.add(review_session)
            session.flush()
            dependency = OpportunityFitReviewStage(
                review_id=review_session.id,
                application_id=app.id,
                stage="triage",
                idempotency_key="stage-parent-key",
                source_snapshot_json="{}",
                source_fingerprint_sha256="0" * 64,
                proposal_json="{}",
                proposal_sha256="1" * 64,
            )
        elif dependency_model in {ApplicationSubmissionSnapshot, ApplicationOutcome}:
            resume = Resume(name="Main", content_json="{}")
            jd_version = ApplicationJDVersion(
                application_id=app.id,
                version_number=1,
                jd_text="JD",
                content_sha256="0" * 64,
                source_kind="ui",
                idempotency_key="application-outcome-jd-version",
                request_fingerprint_sha256="1" * 64,
            )
            session.add_all([resume, jd_version])
            session.flush()
            snapshot = ApplicationSubmissionSnapshot(
                application_id=app.id,
                resume_id=resume.id,
                jd_version_id=jd_version.id,
                resume_snapshot_json="{}",
                resume_snapshot_hash="2" * 64,
                jd_snapshot="JD",
                jd_snapshot_hash="0" * 64,
                source_kind="ui",
                idempotency_key="application-submission-snapshot-dependency",
                request_fingerprint_sha256="3" * 64,
                submitted_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            )
            if dependency_model is ApplicationSubmissionSnapshot:
                dependency = snapshot
            else:
                session.add(snapshot)
                session.flush()
                dependency = ApplicationOutcome(
                    application_id=app.id,
                    submission_snapshot_id=snapshot.id,
                    stage="interview",
                    result="advanced",
                    feedback_tags_json="[]",
                    source_kind="ui",
                    idempotency_key="application-outcome-dependency",
                    request_fingerprint_sha256="4" * 64,
                    occurred_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
                )
        else:
            dependency = _application_dependency(dependency_model, app.id)
        session.add(dependency)
        session.commit()
        dependency_id = dependency.id

    assert repo.delete_if_matches(app.id, expected) is False
    assert repo.get(app.id) is not None
    with session_factory() as session:
        preserved = session.get(dependency_model, dependency_id)
        assert preserved is not None
        assert preserved.application_id == app.id


def test_application_delete_if_matches_is_conditional(tmp_path):
    repo = ApplicationsRepository(init_database(tmp_path / "data.db"))
    app = repo.create(ApplicationCreate(company_name="A", position_name="Engineer"))
    expected = {
        "company_name": app.company_name,
        "position_name": app.position_name,
        "job_url": app.job_url,
        "status": app.status,
        "source": app.source,
        "notes": app.notes,
        "applied_at": app.applied_at.isoformat(),
        "closed_reason": app.closed_reason,
        "updated_at": app.updated_at.isoformat(),
    }

    assert repo.delete_if_matches(app.id, {**expected, "notes": "changed"}) is False
    assert repo.get(app.id) is not None
    assert repo.delete_if_matches(app.id, expected) is True
    assert repo.get(app.id) is None


def test_event_delete_if_matches_is_conditional(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    applications = ApplicationsRepository(session_factory)
    events = ApplicationEventsRepository(session_factory)
    app = applications.create(ApplicationCreate(company_name="A", position_name="Engineer"))
    event = events.create(
        ApplicationEventCreate(
            application_id=app.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            duration_minutes=60,
            location="Room A",
        )
    )
    note = NotesRepository(session_factory).create(
        NoteCreate(
            application_id=app.id,
            application_event_id=event.id,
            company="A",
        )
    )
    expected = {
        "application_id": app.id,
        "event_type": "interview",
        "subtype": "",
        "tags": [],
        "round": 0,
        "scheduled_at": "2026-08-01T10:00:00Z",
        "duration_minutes": 60,
        "location": "Room A",
        "notes": "",
        "remind_at": None,
        "status": "todo",
    }

    assert events.delete_if_matches(event.id, {**expected, "location": "changed"}) is False
    assert events.get(event.id) is not None
    assert NotesRepository(session_factory).get(note.id).content_revision == 1
    assert events.delete_if_matches(event.id, expected) is True
    assert events.get(event.id) is None
    unbound = NotesRepository(session_factory).get(note.id)
    assert unbound is not None
    assert unbound.application_event_id is None
    assert unbound.content_revision == 2


def test_note_delete_if_matches_is_conditional(tmp_path):
    repo = NotesRepository(init_database(tmp_path / "data.db"))
    note = repo.create(
        NoteCreate(
            company="A",
            position="Engineer",
            date="2026-08-01",
            questions="Original",
        )
    )
    expected = {
        "application_id": None,
        "company": "A",
        "position": "Engineer",
        "round": "",
        "date": "2026-08-01",
        "questions": "Original",
        "self_reflection": "",
        "difficulty_points": "",
        "mood": "",
    }

    assert repo.delete_if_matches(note.id, {**expected, "questions": "changed"}) is False
    assert repo.get(note.id) is not None
    assert repo.delete_if_matches(note.id, expected) is True
    assert repo.get(note.id) is None
