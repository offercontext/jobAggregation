from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
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
from offerpilot.models import Application
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository, JDAnalysisCreate
from offerpilot.repositories.notes import NoteCreate, NotesRepository
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.repositories.session_binding import (
    ScopeAccessDenied,
    ScopedRepositoryBinding,
    _SCOPED_BINDING_SEAL,
)


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
        ("list_application_index_scoped", ()),
        ("list_application_events_scoped", ()),
        ("list_notes_scoped", ()),
        ("list_offers_scoped", ()),
        ("list_jd_analyses_scoped", ()),
    )

    repositories = (repositories[0], repositories[0], *repositories[1:])
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


@pytest.mark.parametrize(
    ("module_name", "symbol"),
    (
        ("offerpilot.repositories.applications", "ApplicationsRepository"),
        ("offerpilot.repositories.application_events", "ApplicationEventsRepository"),
        ("offerpilot.repositories.notes", "NotesRepository"),
        ("offerpilot.repositories.offers", "OffersRepository"),
        ("offerpilot.repositories.jd", "JDAnalysesRepository"),
    ),
)
def test_repository_cold_import_does_not_initialize_composition_cycle(
    module_name: str, symbol: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"from {module_name} import {symbol}"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_repository_modules_do_not_import_authority_aggregate_at_module_scope() -> None:
    repository_root = Path(__file__).parents[2] / "src" / "offerpilot" / "repositories"
    for module_name in (
        "applications.py",
        "application_events.py",
        "notes.py",
        "offers.py",
        "jd.py",
        "session_binding.py",
    ):
        tree = ast.parse((repository_root / module_name).read_text(encoding="utf-8"))
        aggregate_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "offerpilot.ai.tool_authority"
        ]
        assert aggregate_imports == [], module_name


def test_scoped_binding_cannot_be_forged_or_subclass_factory_bypassed(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    class EvilFactory(AuthorityFactory):
        pass

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            with pytest.raises(AuthorityPhaseError):
                ScopedRepositoryBinding(session, constraint, factory, authority)
            with pytest.raises(AuthorityPhaseError):
                _bind_scoped(
                    seeded["applications"],
                    session,
                    EvilFactory(),
                    authority,
                    constraint,
                )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements == []


def test_registered_binding_rejects_cross_repository_and_attribute_injection(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    class EvilFactory(AuthorityFactory):
        def require_scope_constraint(self, constraint, authority):
            del constraint, authority

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            bound = _bind_scoped(seeded["applications"], session, factory, authority, constraint)
            binding = bound._scope_binding
            assert binding is not None

            with pytest.raises(AuthorityPhaseError):
                ScopedRepositoryBinding(
                    session,
                    constraint,
                    factory,
                    authority,
                    _seal=_SCOPED_BINDING_SEAL,
                )

            foreign_repository = seeded["applications"].bind(session)
            object.__setattr__(foreign_repository, "_scope_binding", binding)
            with pytest.raises(AuthorityPhaseError):
                foreign_repository.list_applications_scoped(constraint)
            assert statements == []

            forged = ApplicationScopeConstraint(
                entity_kind="application",
                mode="restricted",
                allowed_identities=frozenset({seeded["second"].id}),
                authority_instance_token=authority.authority_instance_token,
            )
            object.__setattr__(binding, "constraint", forged)
            object.__setattr__(binding, "authority_factory", EvilFactory())
            object.__setattr__(bound, "_scope_binding", binding)
            with pytest.raises(AuthorityPhaseError):
                bound.list_applications_scoped(forged)
            assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_evil_factory_cannot_register_or_use_a_binding(seeded) -> None:
    trusted_factory = AuthorityFactory()
    _, authority, _ = _constraint(trusted_factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    class EvilFactory(AuthorityFactory):
        def require_scope_constraint(self, constraint, authority):
            del constraint, authority

        def require_repository_binding(
            self, binding, *, repository, session, constraint
        ) -> None:
            del binding, repository, session, constraint

    forged = ApplicationScopeConstraint(
        entity_kind="application",
        mode="restricted",
        allowed_identities=frozenset({seeded["second"].id}),
        authority_instance_token=authority.authority_instance_token,
    )
    evil_factory = EvilFactory()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            target = seeded["applications"].bind(session)
            binding = ScopedRepositoryBinding(
                session,
                forged,
                evil_factory,
                authority,
                _seal=_SCOPED_BINDING_SEAL,
                _ticket=object(),
            )
            with pytest.raises(AuthorityPhaseError):
                evil_factory.issue_repository_binding_ticket(
                    repository=target,
                    session=session,
                    constraint=forged,
                    authority=authority,
                )
            with pytest.raises(AuthorityPhaseError):
                evil_factory.register_repository_binding(
                    binding,
                    ticket=object(),
                    repository=target,
                    session=session,
                    constraint=forged,
                    authority=authority,
                )
            with pytest.raises(AuthorityPhaseError):
                evil_factory.revoke_repository_binding_ticket(object())
            with pytest.raises(AuthorityPhaseError):
                binding.require(forged, repository=target)
            assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        evil_factory.close()
        trusted_factory.close()


@pytest.mark.parametrize("mutation", ("constraint", "authority"))
def test_binding_is_active_revalidates_registered_sources(seeded, mutation: str) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)

    with seeded["session_factory"]() as session:
        bound = _bind_scoped(seeded["applications"], session, factory, authority, constraint)
        binding = bound._scope_binding
        assert binding is not None
        assert factory.is_active(binding)

        if mutation == "constraint":
            object.__setattr__(constraint, "allowed_identities", frozenset({seeded["second"].id}))
        else:
            object.__setattr__(
                authority,
                "conversation_scope_revision",
                authority.conversation_scope_revision + 1,
            )

        assert factory.is_active(binding) is False


@pytest.mark.parametrize("cleanup", ("revoke", "close"))
def test_binding_registry_cleanup_fails_closed_without_sql(seeded, cleanup: str) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            bound = _bind_scoped(seeded["applications"], session, factory, authority, constraint)
            assert factory.active_count > 0
            binding = bound._scope_binding
            assert binding is not None
            assert factory.is_active(binding)
            if cleanup == "revoke":
                factory.revoke_authority(authority)
            else:
                factory.close()
            assert factory.is_active(binding) is False
            with pytest.raises(AuthorityPhaseError):
                bound.list_applications_scoped(constraint)
            assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_scoped_point_and_application_filter_ids_are_exact_positive_int64(seeded) -> None:
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(factory)
    invalid_ids: tuple[object, ...] = (True, 1.5, "1", 0, -1, 2**63)
    point_repositories = (
        ("applications", "get_application_scoped", seeded["first"].id),
        ("events", "get_application_event_scoped", seeded["first"].id),
        ("notes", "get_note_scoped", seeded["first_note"].id),
        ("offers", "get_offer_scoped", seeded["first_offer"].id),
        ("analyses", "get_jd_analysis_scoped", seeded["first_analysis"].id),
    )
    filter_repositories = (
        ("events", "list_application_events_scoped"),
        ("notes", "list_notes_scoped"),
        ("analyses", "list_jd_analyses_scoped"),
    )
    engine = seeded["session_factory"].kw["bind"]
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with seeded["session_factory"]() as session:
            bound = {
                key: _bind_scoped(seeded[key], session, factory, authority, constraint)
                for key in {key for key, _, _ in point_repositories}
            }
            for key, method_name, valid_id in point_repositories:
                for value in invalid_ids:
                    with pytest.raises(AuthorityPhaseError):
                        getattr(bound[key], method_name)(constraint, value)
                    assert statements == []
                assert getattr(bound[key], method_name)(constraint, valid_id) is not None
                statements.clear()

            for key, method_name in filter_repositories:
                for value in invalid_ids:
                    with pytest.raises(AuthorityPhaseError):
                        getattr(bound[key], method_name)(constraint, application_id=value)
                    assert statements == []
                statements.clear()
    finally:
        event.remove(engine, "before_cursor_execute", capture)


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
            index_rows = apps.list_application_index_scoped(constraint, limit=257)
            assert [row.id for row in index_rows] == [seeded["first"].id]
            assert len(statements) == 1
            statements.clear()
            apps.get_application_scoped(constraint, seeded["first"].id)
            assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_application_index_limit_has_stable_id_tie_breaker(tmp_path) -> None:
    session_factory = init_database(tmp_path / "stable-index.db")
    applied_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    with session_factory() as session:
        session.add_all(
            Application(
                company_name=f"Company {index}",
                position_name="Engineer",
                status="applied",
                applied_at=applied_at,
                created_at=applied_at,
                updated_at=applied_at,
            )
            for index in range(1, 259)
        )
        session.commit()
    factory = AuthorityFactory()
    _, authority, constraint = _constraint(
        factory,
        context_type="workspace",
        context_ref=None,
    )
    with session_factory() as session:
        repository = ApplicationsRepository(session_factory).bind_scoped(
            session,
            constraint,
            authority_factory=factory,
            authority=authority,
        )
        first = repository.list_application_index_scoped(constraint, limit=257)
        second = repository.list_application_index_scoped(constraint, limit=257)

    assert [row.id for row in first] == list(range(258, 1, -1))
    assert [row.id for row in second] == [row.id for row in first]
