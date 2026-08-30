from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional, cast

from builtins import list as BuiltinList

from sqlalchemy import (
    and_,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from offerpilot.models import Application, ApplicationEvent, InterviewNote
from offerpilot.repositories.applications import _restricted_scope_id
from offerpilot.repositories.session_binding import (
    ScopedRepositoryBinding,
    ScopeAccessDenied,
    attach_scoped_repository,
    bind_scoped_repository,
    finish_repository_write,
    require_scoped_optional_id,
    require_scoped_positive_int64,
    repository_session,
    rollback_repository_write,
    scoped_authority_phase_error,
)

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.contracts import ApplicationScopeConstraint, ToolExecutionAuthority
    from offerpilot.repositories.session_binding import AuthorityFactoryProtocol


class NoteBindingError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class NoteCreate:
    company: str
    position: str = ""
    round: str = ""
    date: str = ""
    questions: str = ""
    self_reflection: str = ""
    difficulty_points: str = ""
    mood: str = ""
    application_id: int | None = None
    application_event_id: int | None = None


class _Unset:
    pass


UNSET: _Unset = _Unset()


@dataclass
class NoteUpdate:
    company: str = ""
    position: str = ""
    round: str = ""
    date: str = ""
    questions: str = ""
    self_reflection: str = ""
    difficulty_points: str = ""
    mood: str = ""
    application_id: int | None | _Unset = UNSET
    application_event_id: int | None | _Unset = UNSET


class NotesRepository:
    def __init__(self, session_factory: sessionmaker[Session], session: Session | None = None):
        self._session_factory = session_factory
        self._session = session
        self._scope_binding: ScopedRepositoryBinding | None = None

    def bind(self, session: Session) -> "NotesRepository":
        return NotesRepository(self._session_factory, session)

    def bind_scoped(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        *,
        authority_factory: AuthorityFactoryProtocol,
        authority: ToolExecutionAuthority,
    ) -> "NotesRepository":
        repository = NotesRepository(self._session_factory, session)
        binding = bind_scoped_repository(
            session,
            constraint,
            authority_factory=authority_factory,
            authority=authority,
            repository=repository,
        )
        return attach_scoped_repository(repository, binding)

    def _require_scoped(self, constraint: object) -> ScopedRepositoryBinding:
        binding = self._scope_binding
        if binding is None or self._session is None:
            raise scoped_authority_phase_error(
                "scoped repository requires a caller-owned bound Session"
            )
        if type(binding) is not ScopedRepositoryBinding:
            raise scoped_authority_phase_error("scoped repository binding has an invalid type")
        binding.require(constraint, repository=self)
        return binding

    def create(self, data: NoteCreate) -> InterviewNote:
        note = InterviewNote(
            application_id=data.application_id,
            application_event_id=data.application_event_id,
            company=data.company,
            position=data.position,
            round=data.round,
            date=data.date,
            questions=data.questions,
            self_reflection=data.self_reflection,
            difficulty_points=data.difficulty_points,
            mood=data.mood,
        )
        with repository_session(self._session_factory, self._session) as session:
            self._validate_event_binding(session, data.application_id, data.application_event_id)
            session.add(note)
            try:
                finish_repository_write(session, self._session)
            except IntegrityError as exc:
                rollback_repository_write(session, self._session)
                if data.application_event_id is not None:
                    raise NoteBindingError(409, "Interview event already has a note") from exc
                raise
            session.refresh(note)
            return note

    def create_note_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        data: NoteCreate,
    ) -> InterviewNote:
        binding = self._require_scoped(constraint)
        _require_note_company(data.company)
        require_scoped_optional_id(data.application_id, "application_id")
        require_scoped_optional_id(data.application_event_id, "application_event_id")
        if data.application_event_id is not None and data.application_id is None:
            raise NoteBindingError(422, "application_event_id requires an application")

        values = _note_create_values(data)
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            if data.application_id is not None and data.application_id != allowed_id:
                raise ScopeAccessDenied("application scope denied")
            columns = tuple(values)
            source = select(
                *(
                    value if isinstance(value, ColumnElement) else literal(value)
                    for value in values.values()
                )
            ).select_from(Application)
            source = source.where(
                Application.id == allowed_id,
                Application.deleted_at.is_(None),
            )
            if data.application_event_id is not None:
                source = source.where(
                    _valid_interview_event(
                        data.application_event_id,
                        data.application_id,
                    ),
                    _note_event_available(data.application_event_id),
                )
            statement = (
                insert(InterviewNote)
                .from_select(columns, source)
                .returning(InterviewNote)
            )
        elif data.application_event_id is not None:
            columns = tuple(values)
            source = select(
                *(
                    value if isinstance(value, ColumnElement) else literal(value)
                    for value in values.values()
                )
            ).where(
                _valid_interview_event(
                    data.application_event_id,
                    data.application_id,
                ),
                _note_event_available(data.application_event_id),
            )
            statement = (
                insert(InterviewNote)
                .from_select(columns, source)
                .returning(InterviewNote)
            )
        else:
            statement = (
                insert(InterviewNote)
                .values(**values)
                .returning(InterviewNote)
            )
        with binding.session.no_autoflush:
            rows = list(binding.session.scalars(statement))
        if len(rows) != 1:
            if constraint.mode == "restricted" or len(rows) > 1:
                raise ScopeAccessDenied("application scope denied")
            if data.application_event_id is not None:
                with binding.session.no_autoflush:
                    classification = binding.session.execute(
                        select(
                            _valid_interview_event(
                                data.application_event_id,
                                data.application_id,
                            ).label("event_allowed"),
                            _note_event_available(data.application_event_id).label(
                                "event_available"
                            ),
                        )
                    ).one()
                if not classification.event_allowed:
                    raise NoteBindingError(
                        422,
                        "application_event_id must reference an interview event for the application",
                    )
                if not classification.event_available:
                    raise NoteBindingError(409, "Interview event already has a note")
                raise RuntimeError(
                    "scoped note create returned no row after valid guards"
                )
            raise ScopeAccessDenied("application scope denied")
        return rows[0]

    def list(self, application_id: int = 0) -> list[InterviewNote]:
        statement = (
            select(InterviewNote)
            .outerjoin(Application, Application.id == InterviewNote.application_id)
            .where(
                or_(
                    InterviewNote.application_id.is_(None),
                    Application.deleted_at.is_(None),
                )
            )
        )
        if application_id > 0:
            statement = statement.where(InterviewNote.application_id == application_id)
        statement = statement.order_by(InterviewNote.created_at.desc(), InterviewNote.id.desc())
        with repository_session(self._session_factory, self._session) as session:
            return list(session.scalars(statement))

    def get(self, note_id: int) -> Optional[InterviewNote]:
        with repository_session(self._session_factory, self._session) as session:
            return cast(Optional[InterviewNote], session.scalar(self._visible_note_statement(note_id)))

    def list_notes_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        application_id: int | None = None,
    ) -> BuiltinList[InterviewNote]:
        binding = self._require_scoped(constraint)
        require_scoped_optional_id(application_id, "application_id")
        session = binding.session
        if constraint.mode == "unrestricted":
            statement = (
                select(InterviewNote)
                .outerjoin(Application, Application.id == InterviewNote.application_id)
                .where(
                    or_(
                        InterviewNote.application_id.is_(None),
                        Application.deleted_at.is_(None),
                    )
                )
            )
            if application_id is not None:
                statement = statement.where(InterviewNote.application_id == application_id)
            statement = statement.order_by(InterviewNote.created_at.desc(), InterviewNote.id.desc())
            with session.no_autoflush:
                return list(session.scalars(statement))

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(Application.id.label("_scope_application_id"))
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application")
        )
        join_condition = InterviewNote.application_id == scope_parent.c._scope_application_id
        if application_id is not None:
            join_condition = and_(join_condition, InterviewNote.application_id == application_id)
        statement = (
            select(InterviewNote, scope_parent.c._scope_application_id)
            .select_from(scope_parent.outerjoin(InterviewNote, join_condition))
            .order_by(InterviewNote.created_at.desc(), InterviewNote.id.desc())
        )
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0][1] is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [row[0] for row in rows if row[0] is not None]

    def get_note_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        note_id: int,
    ) -> Optional[InterviewNote]:
        binding = self._require_scoped(constraint)
        note_id = require_scoped_positive_int64(note_id, "note id")
        session = binding.session
        if constraint.mode == "unrestricted":
            with session.no_autoflush:
                return cast(Optional[InterviewNote], session.scalar(self._visible_note_statement(note_id)))
        allowed_id = _restricted_scope_id(constraint)
        with session.no_autoflush:
            note = session.scalar(
                select(InterviewNote)
                .join(Application, Application.id == InterviewNote.application_id)
                .where(InterviewNote.id == note_id)
                .where(InterviewNote.application_id == allowed_id)
                .where(Application.id == allowed_id)
                .where(Application.deleted_at.is_(None))
            )
        if note is None:
            raise ScopeAccessDenied("application scope denied")
        return note

    def update(self, note_id: int, data: NoteUpdate) -> Optional[InterviewNote]:
        with repository_session(self._session_factory, self._session) as session:
            note = session.scalar(self._visible_note_statement(note_id))
            if note is None:
                return None
            if data.application_id is not UNSET and data.application_id != note.application_id:
                raise NoteBindingError(422, "application_id cannot be changed")
            event_id: int | None
            if data.application_event_id is UNSET:
                event_id = note.application_event_id
            else:
                event_id = cast(int | None, data.application_event_id)
            if event_id != note.application_event_id:
                self._validate_event_binding(session, note.application_id, event_id, note.id)
            values = _note_update_values(data)
            values["application_event_id"] = event_id
            statement = (
                update(InterviewNote)
                .where(InterviewNote.id == note_id)
                .values(**_revisioned_note_values(values))
                .returning(InterviewNote)
                .execution_options(populate_existing=True, synchronize_session=False)
            )
            try:
                updated = session.scalar(statement)
                if updated is None:
                    return None
                finish_repository_write(session, self._session)
            except IntegrityError as exc:
                rollback_repository_write(session, self._session)
                if event_id is not None:
                    raise NoteBindingError(409, "Interview event already has a note") from exc
                raise
            session.refresh(updated)
            return updated

    def update_note_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        note_id: int,
        data: NoteUpdate,
    ) -> Optional[InterviewNote]:
        binding = self._require_scoped(constraint)
        note_id = require_scoped_positive_int64(note_id, "note id")
        _require_note_company(data.company)
        if data.application_id is not UNSET:
            require_scoped_optional_id(data.application_id, "application_id")
        if data.application_event_id is not UNSET:
            require_scoped_optional_id(data.application_event_id, "application_event_id")

        visible_parent = exists(
            select(Application.id).where(
                Application.id == InterviewNote.application_id,
                Application.deleted_at.is_(None),
            )
        )
        statement = update(InterviewNote).where(InterviewNote.id == note_id)
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            if data.application_id is not UNSET and data.application_id != allowed_id:
                raise ScopeAccessDenied("application scope denied")
            statement = statement.where(
                InterviewNote.application_id == allowed_id,
                visible_parent,
            )
        else:
            statement = statement.where(
                or_(InterviewNote.application_id.is_(None), visible_parent)
            )

        values = _revisioned_note_values(_note_update_values(data))
        application_change = data.application_id is not UNSET
        application_allowed: ColumnElement[bool]
        if application_change:
            if data.application_id is None:
                application_allowed = InterviewNote.application_id.is_(None)
            else:
                application_allowed = InterviewNote.application_id == data.application_id
        else:
            application_allowed = literal(True)
        event_id: int | None = None
        event_allowed: ColumnElement[bool]
        duplicate_allowed: ColumnElement[bool]
        if data.application_event_id is not UNSET:
            event_id = cast(int | None, data.application_event_id)
            if event_id is not None:
                event_allowed = _valid_interview_event(
                    event_id, InterviewNote.application_id
                )
                duplicate_allowed = _note_event_available(
                    event_id,
                    excluding_note_id=note_id,
                )
            else:
                event_allowed = literal(True)
                duplicate_allowed = literal(True)
        else:
            event_allowed = literal(True)
            duplicate_allowed = literal(True)
        domain_allowed = and_(application_allowed, event_allowed, duplicate_allowed)
        statement = (
            statement.where(domain_allowed)
            .values(**values)
            .returning(InterviewNote)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        with binding.session.no_autoflush:
            rows = list(binding.session.scalars(statement))
        if len(rows) != 1:
            if constraint.mode == "restricted" or len(rows) > 1:
                raise ScopeAccessDenied("application scope denied")
            classification_statement = select(
                InterviewNote.id,
                application_allowed.label("application_allowed"),
                event_allowed.label("event_allowed"),
                duplicate_allowed.label("duplicate_allowed"),
            ).where(
                InterviewNote.id == note_id,
                or_(InterviewNote.application_id.is_(None), visible_parent),
            )
            with binding.session.no_autoflush:
                classification_rows = list(
                    binding.session.execute(classification_statement)
                )
            if len(classification_rows) != 1:
                if len(classification_rows) > 1:
                    raise ScopeAccessDenied("application scope denied")
                return None
            (
                _matched_note_id,
                application_is_allowed,
                event_is_allowed,
                duplicate_is_allowed,
            ) = classification_rows[0]
        else:
            return rows[0]
        if not application_is_allowed:
            raise NoteBindingError(422, "application_id cannot be changed")
        if not event_is_allowed:
            raise NoteBindingError(
                422,
                "application_event_id must reference an interview event for the application",
            )
        if not duplicate_is_allowed:
            raise NoteBindingError(409, "Interview event already has a note")
        raise RuntimeError("scoped note update returned no row after valid guards")

    def delete(self, note_id: int) -> None:
        with repository_session(self._session_factory, self._session) as session:
            note = cast(Optional[InterviewNote], session.scalar(self._visible_note_statement(note_id)))
            if note is not None:
                session.delete(note)
                finish_repository_write(session, self._session)

    def delete_note_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        note_id: int,
    ) -> bool:
        binding = self._require_scoped(constraint)
        note_id = require_scoped_positive_int64(note_id, "note id")
        visible_parent = exists(
            select(Application.id).where(
                Application.id == InterviewNote.application_id,
                Application.deleted_at.is_(None),
            )
        )
        statement = delete(InterviewNote).where(InterviewNote.id == note_id)
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            statement = statement.where(
                InterviewNote.application_id == allowed_id,
                visible_parent,
            )
        else:
            statement = statement.where(
                or_(InterviewNote.application_id.is_(None), visible_parent)
            )
        returning_statement = statement.returning(InterviewNote.id)
        with binding.session.no_autoflush:
            rows = list(binding.session.scalars(returning_statement))
        if len(rows) != 1:
            if constraint.mode == "restricted" or len(rows) > 1:
                raise ScopeAccessDenied("application scope denied")
            return False
        return True

    def delete_if_matches(self, note_id: int, expected: dict[str, object]) -> bool:
        statement = (
            delete(InterviewNote)
            .where(InterviewNote.id == note_id)
            .where(InterviewNote.company == expected.get("company"))
            .where(InterviewNote.position == expected.get("position"))
            .where(InterviewNote.round == expected.get("round"))
            .where(InterviewNote.date == expected.get("date"))
            .where(InterviewNote.questions == expected.get("questions"))
            .where(InterviewNote.self_reflection == expected.get("self_reflection"))
            .where(InterviewNote.difficulty_points == expected.get("difficulty_points"))
            .where(InterviewNote.mood == expected.get("mood"))
        )
        application_id = expected.get("application_id")
        statement = (
            statement.where(InterviewNote.application_id.is_(None))
            if application_id is None
            else statement.where(InterviewNote.application_id == application_id)
        )
        visible_application = exists(
            select(Application.id)
            .where(Application.id == InterviewNote.application_id)
            .where(Application.deleted_at.is_(None))
        )
        statement = statement.where(
            or_(InterviewNote.application_id.is_(None), visible_application)
        )
        with repository_session(self._session_factory, self._session) as session:
            result = session.execute(statement)
            finish_repository_write(session, self._session)
            return getattr(result, "rowcount", 0) == 1

    @staticmethod
    def _visible_note_statement(note_id: int) -> Select[tuple[InterviewNote]]:
        return (
            select(InterviewNote)
            .outerjoin(Application, Application.id == InterviewNote.application_id)
            .where(InterviewNote.id == note_id)
            .where(
                or_(
                    InterviewNote.application_id.is_(None),
                    Application.deleted_at.is_(None),
                )
            )
        )

    @staticmethod
    def _validate_event_binding(
        session: Session,
        application_id: int | None,
        event_id: int | None,
        note_id: int | None = None,
    ) -> None:
        if event_id is None:
            return
        if application_id is None:
            raise NoteBindingError(422, "application_event_id requires an application")
        event = session.scalar(
            select(ApplicationEvent)
            .join(Application, Application.id == ApplicationEvent.application_id)
            .where(ApplicationEvent.id == event_id)
            .where(Application.deleted_at.is_(None))
        )
        if event is None or event.event_type != "interview" or event.application_id != application_id:
            raise NoteBindingError(
                422,
                "application_event_id must reference an interview event for the application",
            )
        existing_statement = select(InterviewNote.id).where(
            InterviewNote.application_event_id == event_id
        )
        if note_id is not None:
            existing_statement = existing_statement.where(InterviewNote.id != note_id)
        existing = session.scalar(existing_statement)
        if existing is not None:
            raise NoteBindingError(409, "Interview event already has a note")


def _note_create_values(data: NoteCreate) -> dict[str, object]:
    return {
        "application_id": data.application_id,
        "application_event_id": data.application_event_id,
        "company": data.company,
        "position": data.position,
        "round": data.round,
        "date": data.date,
        "questions": data.questions,
        "self_reflection": data.self_reflection,
        "difficulty_points": data.difficulty_points,
        "mood": data.mood,
    }


def _require_note_company(company: object) -> str:
    if type(company) is not str:
        raise ValueError("company must be a string")
    return company


def _note_update_values(data: NoteUpdate) -> dict[str, object]:
    values: dict[str, object] = {
        "company": data.company,
        "position": data.position,
        "round": data.round,
        "date": data.date,
        "questions": data.questions,
        "self_reflection": data.self_reflection,
        "difficulty_points": data.difficulty_points,
        "mood": data.mood,
    }
    if data.application_id is not UNSET:
        values["application_id"] = data.application_id
    if data.application_event_id is not UNSET:
        values["application_event_id"] = data.application_event_id
    return values


def _revisioned_note_values(values: Mapping[str, object]) -> dict[str, object]:
    return {
        **values,
        "content_revision": InterviewNote.content_revision + 1,
        "updated_at": func.current_timestamp(),
    }


def _valid_interview_event(
    event_id: int,
    application_id: Any,
) -> ColumnElement[bool]:
    return exists(
        select(ApplicationEvent.id)
        .join(Application, Application.id == ApplicationEvent.application_id)
        .where(
            ApplicationEvent.id == event_id,
            ApplicationEvent.event_type == "interview",
            ApplicationEvent.application_id == application_id,
            Application.deleted_at.is_(None),
        )
    )


def _note_event_available(
    event_id: int,
    *,
    excluding_note_id: int | None = None,
) -> ColumnElement[bool]:
    note_table = InterviewNote.__table__.alias("note_event_owner")
    statement = select(note_table.c.id).where(
        note_table.c.application_event_id == event_id
    )
    if excluding_note_id is not None:
        statement = statement.where(note_table.c.id != excluding_note_id)
    return ~exists(statement)
