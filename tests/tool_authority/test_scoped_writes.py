from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect
import sqlite3
import textwrap
from threading import Event, Thread
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DataError, IntegrityError

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    SegmentExecutionAuthority,
    TrustedContextScope,
)
from offerpilot.db import init_database
from offerpilot.models import Application, ApplicationEvent, InterviewNote, Offer
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.notes import (
    NoteBindingError,
    NoteCreate,
    NoteUpdate,
    NotesRepository,
)
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.repositories.session_binding import ScopeAccessDenied


_DIGEST = "sha256:" + "a" * 64
_BINDING_DIGEST = "sha256:" + "b" * 64
_WHEN = datetime(2026, 8, 24, 9, tzinfo=timezone.utc)


def _authority(
    factory: AuthorityFactory,
    *,
    context_type: str,
    context_ref: int | None,
) -> SegmentExecutionAuthority:
    return factory.create_segment_authority(
        conversation_id=19,
        conversation_scope_revision=0,
        segment_id="scoped-write-segment",
        trusted_scope=TrustedContextScope(context_type, context_ref, "general"),
        capabilities=frozenset(
            {
                "applications.write",
                "application_events.write",
                "notes.write",
                "offers.write",
            }
        ),
        capability_profile_fingerprint=_DIGEST,
        binding_policy_fingerprint=_BINDING_DIGEST,
    )


def _scope(
    factory: AuthorityFactory,
    application_id: int | None,
    *,
    context_type: str = "application",
) -> tuple[SegmentExecutionAuthority, ApplicationScopeConstraint]:
    authority = _authority(
        factory,
        context_type=context_type,
        context_ref=application_id,
    )
    return authority, factory.create_application_scope_constraint(authority)


def _bind(repository, session, factory, authority, constraint):
    return repository.bind_scoped(
        session,
        constraint,
        authority_factory=factory,
        authority=authority,
    )


@pytest.fixture()
def seeded(tmp_path):
    session_factory = init_database(tmp_path / "scoped-writes.db")
    applications = ApplicationsRepository(session_factory)
    first = applications.create(ApplicationCreate(company_name="A", position_name="Backend"))
    second = applications.create(ApplicationCreate(company_name="B", position_name="Frontend"))
    deleted = applications.create(ApplicationCreate(company_name="Deleted", position_name="Role"))
    applications.delete(deleted.id)

    events = ApplicationEventsRepository(session_factory)
    first_event = events.create(
        ApplicationEventCreate(
            application_id=first.id,
            event_type="interview",
            scheduled_at=_WHEN,
            duration_minutes=30,
        )
    )
    second_event = events.create(
        ApplicationEventCreate(
            application_id=second.id,
            event_type="interview",
            scheduled_at=_WHEN,
            duration_minutes=30,
        )
    )
    deleted_event = events.create(
        ApplicationEventCreate(
            application_id=deleted.id,
            event_type="interview",
            scheduled_at=_WHEN,
            duration_minutes=30,
        )
    )

    notes = NotesRepository(session_factory)
    first_note = notes.create(NoteCreate(application_id=first.id, company="A"))
    second_note = notes.create(NoteCreate(application_id=second.id, company="B"))
    detached_note = notes.create(NoteCreate(company="Standalone"))
    deleted_note = notes.create(NoteCreate(application_id=deleted.id, company="Deleted"))

    offers = OffersRepository(session_factory)
    first_offer = offers.create(
        OfferCreate(application_id=first.id, company_name="A", position_name="Backend")
    )
    second_offer = offers.create(
        OfferCreate(application_id=second.id, company_name="B", position_name="Frontend")
    )
    detached_offer = offers.create(
        OfferCreate(company_name="Standalone", position_name="Role")
    )
    deleted_offer = offers.create(
        OfferCreate(
            application_id=deleted.id,
            company_name="Deleted",
            position_name="Role",
        )
    )
    return {
        "session_factory": session_factory,
        "applications": applications,
        "events": events,
        "notes": notes,
        "offers": offers,
        "first_id": first.id,
        "second_id": second.id,
        "deleted_id": deleted.id,
        "first_event_id": first_event.id,
        "second_event_id": second_event.id,
        "deleted_event_id": deleted_event.id,
        "first_note_id": first_note.id,
        "second_note_id": second_note.id,
        "detached_note_id": detached_note.id,
        "deleted_note_id": deleted_note.id,
        "first_offer_id": first_offer.id,
        "second_offer_id": second_offer.id,
        "detached_offer_id": detached_offer.id,
        "deleted_offer_id": deleted_offer.id,
    }


def _event_data(application_id: object, *, notes: str = "updated") -> ApplicationEventCreate:
    return ApplicationEventCreate(
        application_id=application_id,  # type: ignore[arg-type]
        event_type="interview",
        scheduled_at=_WHEN,
        duration_minutes=45,
        notes=notes,
    )


def _note_data(application_id: object, *, company: str = "Updated") -> NoteCreate:
    return NoteCreate(application_id=application_id, company=company)  # type: ignore[arg-type]


def _offer_data(application_id: object, *, notes: str = "updated") -> OfferCreate:
    return OfferCreate(
        application_id=application_id,  # type: ignore[arg-type]
        company_name="A",
        position_name="Backend",
        notes=notes,
    )


def test_exact_nine_ports_require_a_factory_registered_caller_session_binding(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    fabricated = ApplicationScopeConstraint(
        entity_kind=constraint.entity_kind,
        mode=constraint.mode,
        allowed_identities=constraint.allowed_identities,
        authority_instance_token=constraint.authority_instance_token,
    )
    cases = (
        (seeded["applications"], "update_application_status_scoped", (seeded["first_id"], "interview", "")),
        (seeded["events"], "create_application_event_scoped", (_event_data(seeded["first_id"]),)),
        (seeded["events"], "update_application_event_scoped", (seeded["first_event_id"], _event_data(seeded["first_id"]))),
        (seeded["events"], "delete_application_event_scoped", (seeded["first_event_id"],)),
        (seeded["notes"], "create_note_scoped", (_note_data(seeded["first_id"]),)),
        (seeded["notes"], "update_note_scoped", (seeded["first_note_id"], NoteUpdate(company="Updated"))),
        (seeded["notes"], "delete_note_scoped", (seeded["first_note_id"],)),
        (seeded["offers"], "update_offer_scoped", (seeded["first_offer_id"], _offer_data(seeded["first_id"]))),
        (seeded["offers"], "save_offer_assessment_scoped", (seeded["first_offer_id"], "strong")),
    )

    with seeded["session_factory"]() as session:
        for repository, method_name, args in cases:
            with pytest.raises(AuthorityPhaseError):
                getattr(repository, method_name)(constraint, *args)
            bound = _bind(repository, session, factory, authority, constraint)
            with patch.object(session, "execute", side_effect=AssertionError("SQL before guard")) as execute:
                with pytest.raises(AuthorityPhaseError):
                    getattr(bound, method_name)(fabricated, *args)
            execute.assert_not_called()


@pytest.mark.parametrize(
    "allowed_identities",
    (
        frozenset(),
        frozenset({1, 2}),
    ),
)
def test_zero_or_multiple_restricted_binding_identities_fail_before_sql(
    seeded, allowed_identities: frozenset[int]
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        applications = _bind(
            seeded["applications"], session, factory, authority, constraint
        )
        object.__setattr__(constraint, "allowed_identities", allowed_identities)
        with patch.object(session, "execute", side_effect=AssertionError("SQL before guard")) as execute:
            with pytest.raises(AuthorityPhaseError):
                applications.update_application_status_scoped(
                    constraint, seeded["first_id"], "interview", ""
                )
        execute.assert_not_called()


@pytest.mark.parametrize("invalid", (True, 1.5, "1", 0, -1, 2**63))
def test_every_target_and_application_binding_is_exact_positive_int64_before_sql(
    seeded, invalid: object
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            apps = _bind(seeded["applications"], session, factory, authority, constraint)
            events = _bind(seeded["events"], session, factory, authority, constraint)
            notes = _bind(seeded["notes"], session, factory, authority, constraint)
            offers = _bind(seeded["offers"], session, factory, authority, constraint)
            calls = (
                (apps.update_application_status_scoped, (constraint, invalid, "interview", "")),
                (events.create_application_event_scoped, (constraint, _event_data(invalid))),
                (events.update_application_event_scoped, (constraint, invalid, _event_data(seeded["first_id"]))),
                (events.update_application_event_scoped, (constraint, seeded["first_event_id"], _event_data(invalid))),
                (events.delete_application_event_scoped, (constraint, invalid)),
                (notes.create_note_scoped, (constraint, _note_data(invalid))),
                (
                    notes.create_note_scoped,
                    (
                        constraint,
                        NoteCreate(
                            application_id=seeded["first_id"],
                            application_event_id=invalid,  # type: ignore[arg-type]
                            company="x",
                        ),
                    ),
                ),
                (notes.update_note_scoped, (constraint, invalid, NoteUpdate(company="x"))),
                (
                    notes.update_note_scoped,
                    (
                        constraint,
                        seeded["first_note_id"],
                        NoteUpdate(application_id=invalid),  # type: ignore[arg-type]
                    ),
                ),
                (
                    notes.update_note_scoped,
                    (
                        constraint,
                        seeded["first_note_id"],
                        NoteUpdate(application_event_id=invalid),  # type: ignore[arg-type]
                    ),
                ),
                (notes.delete_note_scoped, (constraint, invalid)),
                (offers.update_offer_scoped, (constraint, invalid, _offer_data(seeded["first_id"]))),
                (offers.update_offer_scoped, (constraint, seeded["first_offer_id"], _offer_data(invalid))),
                (offers.save_offer_assessment_scoped, (constraint, invalid, "x")),
            )
            for method, args in calls:
                with pytest.raises(AuthorityPhaseError):
                    method(*args)
                assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_restricted_mutations_are_guarded_and_event_delete_owns_note_unbind(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            apps = _bind(seeded["applications"], session, factory, authority, constraint)
            events = _bind(seeded["events"], session, factory, authority, constraint)
            notes = _bind(seeded["notes"], session, factory, authority, constraint)
            offers = _bind(seeded["offers"], session, factory, authority, constraint)
            calls = (
                lambda: apps.update_application_status_scoped(
                    constraint, seeded["first_id"], "interview", ""
                ),
                lambda: events.create_application_event_scoped(
                    constraint, _event_data(seeded["first_id"], notes="created")
                ),
                lambda: events.update_application_event_scoped(
                    constraint,
                    seeded["first_event_id"],
                    _event_data(seeded["first_id"]),
                ),
                lambda: events.delete_application_event_scoped(
                    constraint, seeded["first_event_id"]
                ),
                lambda: notes.create_note_scoped(
                    constraint, _note_data(seeded["first_id"], company="Created")
                ),
                lambda: notes.update_note_scoped(
                    constraint,
                    seeded["first_note_id"],
                    NoteUpdate(company="Updated"),
                ),
                lambda: notes.delete_note_scoped(constraint, seeded["first_note_id"]),
                lambda: offers.update_offer_scoped(
                    constraint,
                    seeded["first_offer_id"],
                    _offer_data(seeded["first_id"]),
                ),
                lambda: offers.save_offer_assessment_scoped(
                    constraint, seeded["first_offer_id"], "strong"
                ),
            )
            for index, call in enumerate(calls):
                statements.clear()
                assert call() is not None
                if index == 3:
                    note_updates = [
                        statement
                        for statement in statements
                        if statement.upper().startswith("UPDATE INTERVIEW_NOTES")
                    ]
                    event_deletes = [
                        statement
                        for statement in statements
                        if statement.upper().startswith("DELETE FROM APPLICATION_EVENTS")
                    ]
                    assert len(note_updates) == 1
                    assert len(event_deletes) == 1
                    assert all(
                        "EXISTS" in statement.upper()
                        for statement in (*note_updates, *event_deletes)
                    )
                else:
                    assert len(statements) == 1
                    assert "RETURNING" in statements[0].upper()
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_scoped_application_status_preserves_closed_and_first_timestamp_predicates(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        applications = _bind(
            seeded["applications"], session, factory, authority, constraint
        )
        interview = applications.update_application_status_scoped(
            constraint, seeded["first_id"], "interview", ""
        )
        assert interview is not None
        first_interview_at = interview.first_interview_at
        assert first_interview_at is not None
        repeated = applications.update_application_status_scoped(
            constraint, seeded["first_id"], "interview", ""
        )
        assert repeated is not None
        assert repeated.first_interview_at == first_interview_at
        session.commit()
        with pytest.raises(ValueError, match="closed_reason is required"):
            applications.update_application_status_scoped(
                constraint, seeded["first_id"], "closed", ""
            )
        session.rollback()
        closed = applications.update_application_status_scoped(
            constraint, seeded["first_id"], "closed", "position filled"
        )
        assert closed is not None
        assert closed.closed_reason == "position filled"
        assert closed.closed_at is not None
        session.commit()
        unchanged = applications.update_application_status_scoped(
            constraint, seeded["first_id"], "closed", ""
        )
        assert unchanged is not None
        assert unchanged.closed_reason == "position filled"
        session.commit()
        with pytest.raises(ValueError, match="closed application cannot be reopened"):
            applications.update_application_status_scoped(
                constraint, seeded["first_id"], "interview", ""
            )
        session.rollback()
        session.expire_all()
        current = session.get(type(closed), seeded["first_id"])
        assert current is not None
        assert current.status == "closed"
        assert current.closed_reason == "position filled"


def test_scoped_application_status_does_not_relabel_unrelated_integrity_error(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        applications = _bind(
            seeded["applications"], session, factory, authority, constraint
        )
        session.execute(
            text(
                """
                CREATE TRIGGER unrelated_application_integrity
                BEFORE UPDATE ON applications
                BEGIN
                    SELECT RAISE(ABORT, 'unrelated application integrity failure');
                END
                """
            )
        )
        session.commit()

        with pytest.raises(IntegrityError) as raised:
            applications.update_application_status_scoped(
                constraint, seeded["first_id"], "interview", ""
            )

        assert "unrelated application integrity failure" in str(raised.value.orig)
        session.rollback()
        current = session.get(Application, seeded["first_id"])
        assert current is not None
        assert current.status == "applied"


@pytest.mark.parametrize(
    ("repository_key", "method_name", "args_key", "model", "field"),
    (
        ("applications", "update_application_status_scoped", "second_id", None, None),
        ("events", "update_application_event_scoped", "second_event_id", ApplicationEvent, "notes"),
        ("events", "delete_application_event_scoped", "second_event_id", ApplicationEvent, None),
        ("notes", "update_note_scoped", "second_note_id", InterviewNote, "company"),
        ("notes", "delete_note_scoped", "second_note_id", InterviewNote, None),
        ("offers", "update_offer_scoped", "second_offer_id", Offer, "notes"),
        ("offers", "save_offer_assessment_scoped", "second_offer_id", Offer, "assessment"),
    ),
)
def test_restricted_update_and_delete_fail_closed_for_cross_application_targets(
    seeded,
    repository_key: str,
    method_name: str,
    args_key: str,
    model,
    field: str | None,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    target_id = seeded[args_key]
    with seeded["session_factory"]() as session:
        repository = _bind(
            seeded[repository_key], session, factory, authority, constraint
        )
        if method_name == "update_application_status_scoped":
            args = (constraint, target_id, "interview", "")
        elif method_name == "update_application_event_scoped":
            args = (constraint, target_id, _event_data(seeded["first_id"]))
        elif method_name == "update_note_scoped":
            args = (constraint, target_id, NoteUpdate(company="forbidden"))
        elif method_name == "update_offer_scoped":
            args = (constraint, target_id, _offer_data(seeded["first_id"], notes="forbidden"))
        elif method_name == "save_offer_assessment_scoped":
            args = (constraint, target_id, "forbidden")
        else:
            args = (constraint, target_id)
        with pytest.raises(ScopeAccessDenied):
            getattr(repository, method_name)(*args)
        session.rollback()

    if model is not None:
        with seeded["session_factory"]() as session:
            row = session.get(model, target_id)
            assert row is not None
            if field is not None:
                assert getattr(row, field) != "forbidden"


def test_restricted_creates_reject_cross_scope_and_soft_deleted_parent_without_insert(
    seeded,
) -> None:
    for application_id in (seeded["second_id"], seeded["deleted_id"]):
        factory = AuthorityFactory()
        authority, constraint = _scope(factory, seeded["first_id"])
        with seeded["session_factory"]() as session:
            events = _bind(seeded["events"], session, factory, authority, constraint)
            notes = _bind(seeded["notes"], session, factory, authority, constraint)
            event_count = session.scalar(select(func.count()).select_from(ApplicationEvent))
            note_count = session.scalar(select(func.count()).select_from(InterviewNote))
            with pytest.raises(ScopeAccessDenied):
                events.create_application_event_scoped(
                    constraint, _event_data(application_id)
                )
            with pytest.raises(ScopeAccessDenied):
                notes.create_note_scoped(constraint, _note_data(application_id))
            assert session.scalar(select(func.count()).select_from(ApplicationEvent)) == event_count
            assert session.scalar(select(func.count()).select_from(InterviewNote)) == note_count


def test_restricted_mutations_recheck_parent_after_prepare_style_reparent_and_delete(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as setup:
        setup.execute(
            ApplicationEvent.__table__.update()
            .where(ApplicationEvent.id == seeded["first_event_id"])
            .values(application_id=seeded["second_id"])
        )
        setup.execute(
            InterviewNote.__table__.update()
            .where(InterviewNote.id == seeded["first_note_id"])
            .values(application_id=seeded["second_id"])
        )
        setup.execute(
            Offer.__table__.update()
            .where(Offer.id == seeded["first_offer_id"])
            .values(application_id=seeded["second_id"])
        )
        setup.commit()

    with seeded["session_factory"]() as session:
        events = _bind(seeded["events"], session, factory, authority, constraint)
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        offers = _bind(seeded["offers"], session, factory, authority, constraint)
        with pytest.raises(ScopeAccessDenied):
            events.update_application_event_scoped(
                constraint,
                seeded["first_event_id"],
                _event_data(seeded["first_id"]),
            )
        with pytest.raises(ScopeAccessDenied):
            notes.delete_note_scoped(constraint, seeded["first_note_id"])
        with pytest.raises(ScopeAccessDenied):
            offers.save_offer_assessment_scoped(
                constraint, seeded["first_offer_id"], "forbidden"
            )

    ApplicationsRepository(seeded["session_factory"]).delete(seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with pytest.raises(ScopeAccessDenied):
            notes.create_note_scoped(constraint, NoteCreate(company="Standalone"))


def test_soft_deleted_current_parent_denies_every_scoped_write_without_side_effect(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["deleted_id"])
    with seeded["session_factory"]() as session:
        apps = _bind(seeded["applications"], session, factory, authority, constraint)
        events = _bind(seeded["events"], session, factory, authority, constraint)
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        offers = _bind(seeded["offers"], session, factory, authority, constraint)
        before = (
            int(session.scalar(select(func.count()).select_from(ApplicationEvent)) or 0),
            int(session.scalar(select(func.count()).select_from(InterviewNote)) or 0),
            int(session.scalar(select(func.count()).select_from(Offer)) or 0),
        )
        calls = (
            lambda: apps.update_application_status_scoped(
                constraint, seeded["deleted_id"], "interview", ""
            ),
            lambda: events.create_application_event_scoped(
                constraint, _event_data(seeded["deleted_id"])
            ),
            lambda: events.update_application_event_scoped(
                constraint,
                seeded["deleted_event_id"],
                _event_data(seeded["deleted_id"]),
            ),
            lambda: events.delete_application_event_scoped(
                constraint, seeded["deleted_event_id"]
            ),
            lambda: notes.create_note_scoped(
                constraint, _note_data(seeded["deleted_id"])
            ),
            lambda: notes.update_note_scoped(
                constraint,
                seeded["deleted_note_id"],
                NoteUpdate(company="forbidden"),
            ),
            lambda: notes.delete_note_scoped(
                constraint, seeded["deleted_note_id"]
            ),
            lambda: offers.update_offer_scoped(
                constraint,
                seeded["deleted_offer_id"],
                _offer_data(seeded["deleted_id"], notes="forbidden"),
            ),
            lambda: offers.save_offer_assessment_scoped(
                constraint, seeded["deleted_offer_id"], "forbidden"
            ),
        )
        for call in calls:
            with pytest.raises(ScopeAccessDenied):
                call()
        session.rollback()
        after = (
            int(session.scalar(select(func.count()).select_from(ApplicationEvent)) or 0),
            int(session.scalar(select(func.count()).select_from(InterviewNote)) or 0),
            int(session.scalar(select(func.count()).select_from(Offer)) or 0),
        )
        assert after == before


def test_application_scope_standalone_add_note_keeps_null_parent_but_requires_active_scope(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        note = notes.create_note_scoped(
            constraint,
            NoteCreate(company="Standalone", position="Independent"),
        )
        assert note.application_id is None
        assert note.company == "Standalone"
        assert note.content_revision == 1
        session.commit()

    ApplicationsRepository(seeded["session_factory"]).delete(seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before = int(session.scalar(select(func.count()).select_from(InterviewNote)) or 0)
        with pytest.raises(ScopeAccessDenied):
            notes.create_note_scoped(
                constraint,
                NoteCreate(company="Must not persist"),
            )
        after = int(session.scalar(select(func.count()).select_from(InterviewNote)) or 0)
        assert after == before


def test_unrestricted_ports_preserve_detached_offer_and_note_mutation_semantics(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        offers = _bind(seeded["offers"], session, factory, authority, constraint)
        updated_note = notes.update_note_scoped(
            constraint,
            seeded["detached_note_id"],
            NoteUpdate(company="Detached updated"),
        )
        updated_offer = offers.save_offer_assessment_scoped(
            constraint,
            seeded["detached_offer_id"],
            "detached assessment",
        )
        assert updated_note is not None
        assert updated_note.company == "Detached updated"
        assert updated_offer is not None
        assert updated_offer.assessment == "detached assessment"


def test_unrestricted_creates_preserve_soft_deleted_parent_baseline(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    with seeded["session_factory"]() as session:
        events = _bind(seeded["events"], session, factory, authority, constraint)
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        created_event = events.create_application_event_scoped(
            constraint, _event_data(seeded["deleted_id"], notes="legacy baseline")
        )
        created_note = notes.create_note_scoped(
            constraint,
            NoteCreate(application_id=seeded["deleted_id"], company="Deleted baseline"),
        )
        assert created_event.application_id == seeded["deleted_id"]
        assert created_note.application_id == seeded["deleted_id"]


def test_scoped_note_write_preserves_stable_binding_domain_failures(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        created = notes.create_note_scoped(
            constraint,
            NoteCreate(
                application_id=seeded["first_id"],
                application_event_id=seeded["first_event_id"],
                company="A",
            ),
        )
        assert created.application_event_id == seeded["first_event_id"]
        session.commit()

    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with pytest.raises(ScopeAccessDenied):
            notes.create_note_scoped(
                constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["first_event_id"],
                    company="duplicate",
                ),
            )
        session.rollback()

    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        engine = seeded["session_factory"].kw["bind"]
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(ScopeAccessDenied):
                notes.create_note_scoped(
                    constraint,
                    NoteCreate(
                        application_id=seeded["first_id"],
                        application_event_id=seeded["second_event_id"],
                        company="A",
                    ),
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert "INSERT" in statements[0].upper()
        assert "RETURNING" in statements[0].upper()
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count
        session.rollback()

    event_factory = AuthorityFactory()
    event_authority, event_constraint = _scope(
        event_factory, None, context_type="workspace"
    )
    with seeded["session_factory"]() as session:
        notes = _bind(
            seeded["notes"],
            session,
            event_factory,
            event_authority,
            event_constraint,
        )
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        with pytest.raises(NoteBindingError) as duplicate_create_event:
            notes.create_note_scoped(
                event_constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["first_event_id"],
                    company="duplicate",
                ),
            )
        assert duplicate_create_event.value.status_code == 409
        assert str(duplicate_create_event.value) == "Interview event already has a note"
        with pytest.raises(NoteBindingError) as invalid_create_event:
            notes.create_note_scoped(
                event_constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["second_event_id"],
                    company="A",
                ),
            )
        assert invalid_create_event.value.status_code == 422
        assert str(invalid_create_event.value) == (
            "application_event_id must reference an interview event for the application"
        )
        assert invalid_create_event.value.__cause__ is None
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count
        session.rollback()

    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with pytest.raises(ScopeAccessDenied):
            notes.update_note_scoped(
                constraint,
                seeded["first_note_id"],
                NoteUpdate(
                    application_event_id=seeded["second_event_id"],
                    company="A",
                ),
            )
        session.rollback()

    workspace_factory = AuthorityFactory()
    workspace_authority, workspace_constraint = _scope(
        workspace_factory, None, context_type="workspace"
    )
    with seeded["session_factory"]() as session:
        notes = _bind(
            seeded["notes"],
            session,
            workspace_factory,
            workspace_authority,
            workspace_constraint,
        )
        with pytest.raises(NoteBindingError) as reparent:
            notes.update_note_scoped(
                workspace_constraint,
                seeded["first_note_id"],
                NoteUpdate(
                    application_id=seeded["second_id"],
                    company="A",
                ),
            )
        assert reparent.value.status_code == 422
        assert str(reparent.value) == "application_id cannot be changed"
        session.rollback()


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_scoped_note_write_rejects_non_string_company_before_sql(
    seeded,
    operation: str,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with (
            patch.object(
                session,
                "execute",
                side_effect=AssertionError("SQL executed before company validation"),
            ) as execute,
            patch.object(
                session,
                "scalars",
                side_effect=AssertionError("SQL executed before company validation"),
            ) as scalars,
        ):
            with pytest.raises(ValueError, match="company must be a string"):
                if operation == "insert":
                    notes.create_note_scoped(
                        constraint,
                        NoteCreate(
                            application_id=seeded["first_id"],
                            application_event_id=seeded["first_event_id"],
                            company=None,  # type: ignore[arg-type]
                        ),
                    )
                else:
                    notes.update_note_scoped(
                        constraint,
                        seeded["first_note_id"],
                        NoteUpdate(
                            application_event_id=seeded["first_event_id"],
                            company=None,  # type: ignore[arg-type]
                        ),
                    )
            execute.assert_not_called()
            scalars.assert_not_called()


def test_scoped_note_write_allows_empty_company(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        created = notes.create_note_scoped(
            constraint,
            NoteCreate(application_id=seeded["first_id"], company=""),
        )
        updated = notes.update_note_scoped(
            constraint,
            seeded["first_note_id"],
            NoteUpdate(company=""),
        )
        assert created.company == ""
        assert updated is not None
        assert updated.company == ""


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_scoped_note_write_preserves_real_unrelated_not_null_error(
    seeded,
    operation: str,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with pytest.raises(IntegrityError) as raised:
            if operation == "insert":
                notes.create_note_scoped(
                    constraint,
                    NoteCreate(
                        application_id=seeded["first_id"],
                        application_event_id=seeded["first_event_id"],
                        company="A",
                        position=None,  # type: ignore[arg-type]
                    ),
                )
            else:
                notes.update_note_scoped(
                    constraint,
                    seeded["first_note_id"],
                    NoteUpdate(
                        application_event_id=seeded["first_event_id"],
                        company="A",
                        position=None,  # type: ignore[arg-type]
                    ),
                )

        assert raised.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"
        assert str(raised.value.orig).casefold() == (
            "not null constraint failed: interview_notes.position"
        )
        session.rollback()


def test_scoped_note_update_prioritizes_immutable_application_over_valid_event(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        with pytest.raises(NoteBindingError) as raised:
            notes.update_note_scoped(
                constraint,
                seeded["first_note_id"],
                NoteUpdate(
                    application_id=seeded["second_id"],
                    application_event_id=seeded["first_event_id"],
                    company="A",
                ),
            )

        assert raised.value.status_code == 422
        assert str(raised.value) == "application_id cannot be changed"
        session.expire_all()
        current = session.get(InterviewNote, seeded["first_note_id"])
        assert current is not None
        assert current.application_id == seeded["first_id"]
        assert current.application_event_id is None
        session.rollback()


@pytest.mark.parametrize("operation", ("insert", "update"))
def test_scoped_note_write_does_not_relabel_unrelated_integrity_error(
    seeded,
    operation: str,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        trigger_detail = (
            "unique constraint failed: interview_notes.application_event_id"
            if operation == "insert"
            else "not null constraint failed: interview_notes.company"
        )
        session.execute(
            text(
                f"""
                CREATE TRIGGER unrelated_note_{operation}_integrity
                BEFORE {operation.upper()} ON interview_notes
                BEGIN
                    SELECT RAISE(ABORT, '{trigger_detail}');
                END
                """
            )
        )
        session.commit()

        with pytest.raises(IntegrityError) as raised:
            if operation == "insert":
                notes.create_note_scoped(
                    constraint,
                    NoteCreate(
                        application_id=seeded["first_id"],
                        application_event_id=seeded["first_event_id"],
                        company="A",
                    ),
                )
            else:
                notes.update_note_scoped(
                    constraint,
                    seeded["first_note_id"],
                    NoteUpdate(
                        application_event_id=seeded["first_event_id"],
                        company="A",
                    ),
                )

        assert trigger_detail in str(raised.value.orig).casefold()
        session.rollback()
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count
        current = session.get(InterviewNote, seeded["first_note_id"])
        assert current is not None
        assert current.application_event_id is None


def test_scoped_note_create_does_not_relabel_trigger_toobig(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        session.execute(
            text(
                """
                CREATE TRIGGER unrelated_note_insert_toobig
                BEFORE INSERT ON interview_notes
                BEGIN
                    SELECT zeroblob(2147483648);
                END
                """
            )
        )
        session.commit()

        with pytest.raises(DataError) as raised:
            notes.create_note_scoped(
                constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["first_event_id"],
                    company="A",
                ),
            )

        assert raised.value.orig.sqlite_errorcode == sqlite3.SQLITE_TOOBIG
        assert raised.value.orig.sqlite_errorname == "SQLITE_TOOBIG"
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count


def test_scoped_note_create_does_not_relabel_business_value_toobig(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        raw_connection = session.connection().connection.driver_connection
        previous_limit = raw_connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000)
        try:
            with pytest.raises(DataError) as raised:
                notes.create_note_scoped(
                    constraint,
                    NoteCreate(
                        application_id=seeded["first_id"],
                        application_event_id=seeded["first_event_id"],
                        company="x" * 20_000,
                    ),
                )
        finally:
            raw_connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)

        assert raised.value.orig.sqlite_errorcode == sqlite3.SQLITE_TOOBIG
        assert raised.value.orig.sqlite_errorname == "SQLITE_TOOBIG"
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count


@pytest.mark.parametrize(
    ("orig_type", "error_code", "error_name"),
    (
        (
            sqlite3.IntegrityError,
            sqlite3.SQLITE_CONSTRAINT_NOTNULL,
            "SQLITE_CONSTRAINT_UNIQUE",
        ),
        (
            sqlite3.IntegrityError,
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            "SQLITE_CONSTRAINT_NOTNULL",
        ),
        (
            sqlite3.IntegrityError,
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            "SQLITE_CONSTRAINT_UNIQUE",
        ),
        (
            Exception,
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            "SQLITE_CONSTRAINT_UNIQUE",
        ),
    ),
)
def test_scoped_note_create_preserves_integrity_errors_without_sqlite_matching(
    seeded,
    orig_type: type[Exception],
    error_code: int,
    error_name: str,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        orig = orig_type("arbitrary driver detail")
        orig.sqlite_errorcode = error_code  # type: ignore[attr-defined]
        orig.sqlite_errorname = error_name  # type: ignore[attr-defined]
        forged = IntegrityError("INSERT", {}, orig)

        with (
            patch.object(session, "scalars", side_effect=forged),
            pytest.raises(IntegrityError) as raised,
        ):
            notes.create_note_scoped(
                constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["first_event_id"],
                    company="A",
                ),
            )

        assert raised.value is forged


def test_scoped_note_create_preserves_trigger_unique_from_another_table(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        before_count = session.scalar(select(func.count()).select_from(InterviewNote))
        session.execute(text("CREATE TABLE unrelated_unique (value INTEGER UNIQUE)"))
        session.execute(text("INSERT INTO unrelated_unique (value) VALUES (1)"))
        session.execute(
            text(
                """
                CREATE TRIGGER unrelated_note_insert_unique
                BEFORE INSERT ON interview_notes
                BEGIN
                    INSERT INTO unrelated_unique (value) VALUES (1);
                END
                """
            )
        )
        session.commit()

        with pytest.raises(IntegrityError) as raised:
            notes.create_note_scoped(
                constraint,
                NoteCreate(
                    application_id=seeded["first_id"],
                    application_event_id=seeded["first_event_id"],
                    company="A",
                ),
            )

        assert raised.value.orig.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        assert raised.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
        assert session.scalar(select(func.count()).select_from(InterviewNote)) == before_count


def test_scoped_note_update_classifies_duplicate_without_integrity_error(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        notes.create_note_scoped(
            constraint,
            NoteCreate(
                application_id=seeded["first_id"],
                application_event_id=seeded["first_event_id"],
                company="bound",
            ),
        )

        with pytest.raises(NoteBindingError) as raised:
            notes.update_note_scoped(
                constraint,
                seeded["first_note_id"],
                NoteUpdate(
                    application_event_id=seeded["first_event_id"],
                    company="A",
                ),
            )

        assert raised.value.status_code == 409
        assert str(raised.value) == "Interview event already has a note"
        assert raised.value.__cause__ is None
        session.expire_all()
        current = session.get(InterviewNote, seeded["first_note_id"])
        assert current is not None
        assert current.application_event_id is None


def test_workspace_note_update_domain_failure_does_not_fire_update_trigger(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    with seeded["session_factory"]() as session:
        notes = _bind(
            seeded["notes"],
            session,
            factory,
            authority,
            constraint,
        )
        session.execute(text("CREATE TABLE note_update_audit (note_id INTEGER NOT NULL)"))
        session.execute(
            text(
                """
                CREATE TRIGGER audit_rejected_note_update
                AFTER UPDATE ON interview_notes
                BEGIN
                    INSERT INTO note_update_audit(note_id) VALUES (NEW.id);
                END
                """
            )
        )
        session.commit()

        with pytest.raises(NoteBindingError) as raised:
            notes.update_note_scoped(
                constraint,
                seeded["first_note_id"],
                NoteUpdate(
                    application_id=seeded["second_id"],
                    application_event_id=seeded["second_event_id"],
                    company="must not update",
                ),
            )

        assert raised.value.status_code == 422
        assert str(raised.value) == "application_id cannot be changed"
        assert session.scalar(text("SELECT count(*) FROM note_update_audit")) == 0
        assert session.scalar(select(func.count()).select_from(InterviewNote)) > 0
        session.commit()

    with seeded["session_factory"]() as session:
        assert session.scalar(text("SELECT count(*) FROM note_update_audit")) == 0
        current = session.get(InterviewNote, seeded["first_note_id"])
        assert current is not None
        assert current.company == "A"
        assert current.content_revision == 1


def test_scoped_note_consecutive_updates_each_increment_revision_in_bound_session(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        old_timestamp = datetime(2020, 1, 1)
        session.execute(
            text("UPDATE interview_notes SET updated_at=:old WHERE id=:note_id"),
            {"old": old_timestamp, "note_id": seeded["first_note_id"]},
        )
        session.commit()
        notes = _bind(seeded["notes"], session, factory, authority, constraint)

        first = notes.update_note_scoped(
            constraint,
            seeded["first_note_id"],
            NoteUpdate(application_id=seeded["first_id"], company="first"),
        )
        first_revision = first.content_revision if first is not None else None
        second = notes.update_note_scoped(
            constraint,
            seeded["first_note_id"],
            NoteUpdate(application_id=seeded["first_id"], company="second"),
        )

        assert first_revision == 2
        assert second is not None
        assert second.content_revision == 3
        assert second.updated_at > old_timestamp
        assert session.get_transaction() is not None


def test_scoped_event_delete_revisions_bound_note_once_and_keeps_outer_transaction(seeded) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        notes = _bind(seeded["notes"], session, factory, authority, constraint)
        events = _bind(seeded["events"], session, factory, authority, constraint)
        updated = notes.update_note_scoped(
            constraint,
            seeded["first_note_id"],
            NoteUpdate(
                application_id=seeded["first_id"],
                application_event_id=seeded["first_event_id"],
                company="A",
            ),
        )
        assert updated is not None
        session.commit()
        assert updated.content_revision == 2
        old_timestamp = datetime(2020, 1, 1)
        session.execute(
            text("UPDATE interview_notes SET updated_at=:old WHERE id=:note_id"),
            {"old": old_timestamp, "note_id": seeded["first_note_id"]},
        )
        session.commit()

        assert events.delete_application_event_scoped(
            constraint, seeded["first_event_id"]
        ) is True
        session.expire_all()
        note = session.get(InterviewNote, seeded["first_note_id"])
        assert note is not None
        assert note.application_event_id is None
        assert note.content_revision == 3
        assert note.updated_at > old_timestamp
        assert session.get_transaction() is not None
        session.rollback()

    with seeded["session_factory"]() as session:
        note = session.get(InterviewNote, seeded["first_note_id"])
        assert note is not None
        assert note.application_event_id == seeded["first_event_id"]
        assert note.content_revision == 2


def test_scoped_event_delete_cross_scope_changes_no_note_revision(seeded) -> None:
    bound_second = seeded["notes"].update(
        seeded["second_note_id"],
        NoteUpdate(
            application_id=seeded["second_id"],
            application_event_id=seeded["second_event_id"],
            company="B",
        ),
    )
    assert bound_second is not None
    assert bound_second.content_revision == 2
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        events = _bind(seeded["events"], session, factory, authority, constraint)
        before = session.get(InterviewNote, seeded["second_note_id"])
        assert before is not None
        before_revision = before.content_revision

        with pytest.raises(ScopeAccessDenied):
            events.delete_application_event_scoped(
                constraint, seeded["second_event_id"]
            )

        session.expire_all()
        after = session.get(InterviewNote, seeded["second_note_id"])
        assert after is not None
        assert after.content_revision == before_revision


def test_scoped_event_delete_trigger_abort_rolls_back_owner_primitive_only(seeded) -> None:
    bound_note = seeded["notes"].update(
        seeded["first_note_id"],
        NoteUpdate(
            application_id=seeded["first_id"],
            application_event_id=seeded["first_event_id"],
            company="A",
        ),
    )
    assert bound_note is not None
    assert bound_note.content_revision == 2
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, seeded["first_id"])
    with seeded["session_factory"]() as session:
        events = _bind(seeded["events"], session, factory, authority, constraint)
        session.execute(
            text(
                "CREATE TRIGGER reject_scoped_event_delete "
                "BEFORE DELETE ON application_events "
                "BEGIN SELECT RAISE(ABORT, 'scoped delete blocked'); END"
            )
        )
        session.commit()

        with pytest.raises(IntegrityError, match="scoped delete blocked"):
            events.delete_application_event_scoped(
                constraint, seeded["first_event_id"]
            )

        session.expire_all()
        stored = session.get(InterviewNote, seeded["first_note_id"])
        assert stored is not None
        assert stored.application_event_id == seeded["first_event_id"]
        assert stored.content_revision == 2
        assert session.get(ApplicationEvent, seeded["first_event_id"]) is not None


def test_workspace_note_update_classifies_after_guarded_mutation_under_write_lock(
    seeded,
) -> None:
    factory = AuthorityFactory()
    authority, constraint = _scope(factory, None, context_type="workspace")
    writer_started = Event()
    writer_finished = Event()
    writer_errors: list[BaseException] = []
    statements: list[str] = []

    def competing_writer() -> None:
        try:
            with seeded["session_factory"]() as other_session:
                writer_started.set()
                other_session.execute(
                    text(
                        "UPDATE application_events SET notes = 'concurrent' "
                        "WHERE id = :event_id"
                    ),
                    {"event_id": seeded["second_event_id"]},
                )
                other_session.commit()
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    writer = Thread(target=competing_writer)
    with seeded["session_factory"]() as session:
        notes = _bind(
            seeded["notes"],
            session,
            factory,
            authority,
            constraint,
        )
        main_connection = session.connection()

        def capture(conn, _cursor, statement, _parameters, _context, _executemany):
            if conn is not main_connection:
                return
            statements.append(statement)
            if statement.lstrip().upper().startswith("UPDATE INTERVIEW_NOTES"):
                writer.start()
                assert writer_started.wait(2)

        engine = seeded["session_factory"].kw["bind"]
        event.listen(engine, "after_cursor_execute", capture)
        try:
            with pytest.raises(NoteBindingError) as raised:
                notes.update_note_scoped(
                    constraint,
                    seeded["first_note_id"],
                    NoteUpdate(
                        application_event_id=seeded["second_event_id"],
                        company="must not update",
                    ),
                )
            assert raised.value.status_code == 422
            assert not writer_finished.wait(0.1)
            assert statements[0].lstrip().upper().startswith("UPDATE INTERVIEW_NOTES")
            assert statements[1].lstrip().upper().startswith("SELECT")
        finally:
            event.remove(engine, "after_cursor_execute", capture)
            session.rollback()

    assert writer_finished.wait(5)
    writer.join(timeout=1)
    assert writer_errors == []


def test_scoped_write_ports_have_no_unscoped_repository_or_orm_fallback() -> None:
    ports = (
        ApplicationsRepository.update_application_status_scoped,
        ApplicationEventsRepository.create_application_event_scoped,
        ApplicationEventsRepository.update_application_event_scoped,
        ApplicationEventsRepository.delete_application_event_scoped,
        NotesRepository.create_note_scoped,
        NotesRepository.update_note_scoped,
        NotesRepository.delete_note_scoped,
        OffersRepository.update_offer_scoped,
        OffersRepository.save_offer_assessment_scoped,
    )
    forbidden_self_calls = {
        "create",
        "get",
        "update",
        "update_full",
        "delete",
        "list",
    }
    forbidden_session_calls = {"add", "delete", "get", "refresh"}
    for port in ports:
        tree = ast.parse(textwrap.dedent(inspect.getsource(port)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "self":
                assert node.func.attr not in forbidden_self_calls, port.__qualname__
            if (
                isinstance(owner, ast.Attribute)
                and owner.attr == "session"
                and node.func.attr in forbidden_session_calls
            ):
                raise AssertionError(
                    f"{port.__qualname__} uses ORM fallback {node.func.attr}"
                )
