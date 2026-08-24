from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    SegmentExecutionAuthority,
    TrustedContextScope,
)
from offerpilot.db import init_database
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository, JDAnalysisCreate
from offerpilot.repositories.notes import NoteCreate, NotesRepository
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.repositories.session_binding import ScopeAccessDenied


_DIGEST = "sha256:" + "a" * 64
_BINDING_DIGEST = "sha256:" + "b" * 64


def _authority(
    factory: AuthorityFactory,
    *,
    context_type: str,
    context_ref: int | None,
) -> SegmentExecutionAuthority:
    return factory.create_segment_authority(
        conversation_id=11,
        conversation_scope_revision=0,
        segment_id="scoped-read-segment",
        trusted_scope=TrustedContextScope(context_type, context_ref, "general"),
        capabilities=frozenset({"applications.read", "application_events.read", "notes.read", "offers.read", "jd_analyses.read"}),
        capability_profile_fingerprint=_DIGEST,
        binding_policy_fingerprint=_BINDING_DIGEST,
    )


def _constraint(
    factory: AuthorityFactory,
    *,
    context_type: str = "application",
    context_ref: int | None = 1,
) -> tuple[AuthorityFactory, SegmentExecutionAuthority, ApplicationScopeConstraint]:
    authority = _authority(factory, context_type=context_type, context_ref=context_ref)
    return factory, authority, factory.create_application_scope_constraint(authority)


def _seed(path):
    session_factory = init_database(path)
    applications = ApplicationsRepository(session_factory)
    first = applications.create(ApplicationCreate(company_name="A", position_name="Backend"))
    second = applications.create(ApplicationCreate(company_name="B", position_name="Frontend"))
    empty = applications.create(ApplicationCreate(company_name="Empty", position_name="Role"))
    deleted = applications.create(ApplicationCreate(company_name="Deleted", position_name="Role"))

    events = ApplicationEventsRepository(session_factory)
    events.create(
        ApplicationEventCreate(
            application_id=first.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 20, 9, tzinfo=timezone.utc),
            duration_minutes=30,
        )
    )
    events.create(
        ApplicationEventCreate(
            application_id=second.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 21, 9, tzinfo=timezone.utc),
            duration_minutes=30,
        )
    )

    notes = NotesRepository(session_factory)
    first_note = notes.create(NoteCreate(application_id=first.id, company="A"))
    second_note = notes.create(NoteCreate(application_id=second.id, company="B"))
    detached_note = notes.create(NoteCreate(company="Standalone"))

    offers = OffersRepository(session_factory)
    first_offer = offers.create(OfferCreate(application_id=first.id, company_name="A", position_name="Backend"))
    second_offer = offers.create(OfferCreate(application_id=second.id, company_name="B", position_name="Frontend"))
    detached_offer = offers.create(OfferCreate(company_name="Standalone", position_name="Role"))

    analyses = JDAnalysesRepository(session_factory)
    first_analysis = analyses.create(
        JDAnalysisCreate(application_id=first.id, jd_source="manual", jd_text="A", result="{}")
    )
    second_analysis = analyses.create(
        JDAnalysisCreate(application_id=second.id, jd_source="manual", jd_text="B", result="{}")
    )
    detached_analysis = analyses.create(
        JDAnalysisCreate(jd_source="manual", jd_text="Standalone", result="{}")
    )

    applications.delete(deleted.id)
    deleted_note = notes.create(NoteCreate(application_id=deleted.id, company="Deleted"))
    deleted_offer = offers.create(OfferCreate(application_id=deleted.id, company_name="Deleted", position_name="Role"))
    deleted_analysis = analyses.create(
        JDAnalysisCreate(application_id=deleted.id, jd_source="manual", jd_text="Deleted", result="{}")
    )

    return {
        "session_factory": session_factory,
        "applications": applications,
        "events": events,
        "notes": notes,
        "offers": offers,
        "analyses": analyses,
        "first": first,
        "second": second,
        "empty": empty,
        "deleted": deleted,
        "first_note": first_note,
        "second_note": second_note,
        "detached_note": detached_note,
        "deleted_note": deleted_note,
        "first_offer": first_offer,
        "second_offer": second_offer,
        "detached_offer": detached_offer,
        "deleted_offer": deleted_offer,
        "first_analysis": first_analysis,
        "second_analysis": second_analysis,
        "detached_analysis": detached_analysis,
        "deleted_analysis": deleted_analysis,
    }


def _bind_scoped(repo, session, factory, authority, constraint):
    return repo.bind_scoped(
        session,
        constraint,
        authority_factory=factory,
        authority=authority,
    )


@pytest.fixture()
def seeded(tmp_path):
    return _seed(tmp_path / "scoped-reads.db")


def test_scoped_ports_require_caller_owned_session_and_registered_constraint(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    repositories = (
        seeded["applications"],
        seeded["events"],
        seeded["notes"],
        seeded["offers"],
        seeded["analyses"],
    )
    methods = (
        ("list_applications_scoped", ()),
        ("list_application_events_scoped", ()),
        ("list_notes_scoped", ()),
        ("list_offers_scoped", ()),
        ("list_jd_analyses_scoped", ()),
    )

    with seeded["session_factory"]() as session:
        for repo, (method_name, args) in zip(repositories, methods):
            with pytest.raises(AuthorityPhaseError):
                getattr(repo, method_name)(constraint, *args)

            bound = _bind_scoped(repo, session, factory, authority, constraint)
            fabricated = ApplicationScopeConstraint(
                entity_kind=constraint.entity_kind,
                mode=constraint.mode,
                allowed_identities=constraint.allowed_identities,
                authority_instance_token=constraint.authority_instance_token,
            )
            with patch.object(session, "execute", side_effect=AssertionError("SQL before guard")) as execute:
                with pytest.raises(AuthorityPhaseError):
                    getattr(bound, method_name)(fabricated, *args)
            execute.assert_not_called()


def test_scoped_collection_ports_are_exact_and_filter_in_sql(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    with seeded["session_factory"]() as session:
        apps = _bind_scoped(seeded["applications"], session, factory, authority, constraint)
        events = _bind_scoped(seeded["events"], session, factory, authority, constraint)
        notes = _bind_scoped(seeded["notes"], session, factory, authority, constraint)
        offers = _bind_scoped(seeded["offers"], session, factory, authority, constraint)
        analyses = _bind_scoped(seeded["analyses"], session, factory, authority, constraint)

        assert [row.id for row in apps.list_applications_scoped(constraint)] == [seeded["first"].id]
        assert [row.event.id for row in events.list_application_events_scoped(constraint)] == [1]
        assert [row.id for row in notes.list_notes_scoped(constraint)] == [seeded["first_note"].id]
        assert [row.id for row in offers.list_offers_scoped(constraint)] == [seeded["first_offer"].id]
        assert [row.id for row in analyses.list_jd_analyses_scoped(constraint)] == [seeded["first_analysis"].id]

        assert events.list_application_events_scoped(constraint, application_id=1) != []
        assert notes.list_notes_scoped(constraint, application_id=1) != []
        assert analyses.list_jd_analyses_scoped(constraint, application_id=1) != []


@pytest.mark.parametrize(
    ("repo_key", "method_name", "record_key"),
    (
        ("applications", "get_application_scoped", "second"),
        ("events", "get_application_event_scoped", "second"),
        ("notes", "get_note_scoped", "second_note"),
        ("offers", "get_offer_scoped", "second_offer"),
        ("analyses", "get_jd_analysis_scoped", "second_analysis"),
    ),
)
def test_scoped_point_reads_deny_cross_application_without_body_read(
    seeded, repo_key: str, method_name: str, record_key: str
) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    with seeded["session_factory"]() as session:
        try:
            bound = _bind_scoped(seeded[repo_key], session, factory, authority, constraint)
            with pytest.raises(ScopeAccessDenied):
                getattr(bound, method_name)(constraint, getattr(seeded[record_key], "id"))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1


def test_restricted_collection_distinguishes_active_empty_parent_from_deleted_parent(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    with seeded["session_factory"]() as session:
        bound = _bind_scoped(seeded["notes"], session, factory, authority, constraint)
        assert [note.id for note in bound.list_notes_scoped(constraint)] == [seeded["first_note"].id]

    empty_factory = AuthorityFactory()
    empty_authority = _authority(
        empty_factory, context_type="application", context_ref=seeded["empty"].id
    )
    empty_constraint = empty_factory.create_application_scope_constraint(empty_authority)
    with seeded["session_factory"]() as session:
        bound = _bind_scoped(seeded["notes"], session, empty_factory, empty_authority, empty_constraint)
        assert bound.list_notes_scoped(empty_constraint) == []

    deleted_factory = AuthorityFactory()
    deleted_authority = _authority(
        deleted_factory, context_type="application", context_ref=seeded["deleted"].id
    )
    deleted_constraint = deleted_factory.create_application_scope_constraint(deleted_authority)
    with seeded["session_factory"]() as session:
        bound = _bind_scoped(seeded["notes"], session, deleted_factory, deleted_authority, deleted_constraint)
        with pytest.raises(ScopeAccessDenied):
            bound.list_notes_scoped(deleted_constraint)


def test_unrestricted_scoped_ports_preserve_detached_baseline(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory, context_type="workspace", context_ref=None)
    with seeded["session_factory"]() as session:
        notes = _bind_scoped(seeded["notes"], session, factory, authority, constraint)
        offers = _bind_scoped(seeded["offers"], session, factory, authority, constraint)
        analyses = _bind_scoped(seeded["analyses"], session, factory, authority, constraint)
        assert seeded["detached_note"].id in {row.id for row in notes.list_notes_scoped(constraint)}
        assert seeded["detached_offer"].id in {row.id for row in offers.list_offers_scoped(constraint)}
        assert seeded["detached_analysis"].id in {row.id for row in analyses.list_jd_analyses_scoped(constraint)}


def test_every_final_scoped_read_is_one_statement(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            apps = _bind_scoped(seeded["applications"], session, factory, authority, constraint)
            rows = apps.list_applications_scoped(constraint)
            assert [row.id for row in rows] == [seeded["first"].id]
            assert len(statements) == 1
            statements.clear()
            apps.get_application_scoped(constraint, seeded["first"].id)
            assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)
