from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, cast

from builtins import list as BuiltinList

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

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
        binding = bind_scoped_repository(
            session,
            constraint,
            authority_factory=authority_factory,
            authority=authority,
        )
        return attach_scoped_repository(NotesRepository(self._session_factory, session), binding)

    def _require_scoped(self, constraint: object) -> ScopedRepositoryBinding:
        binding = self._scope_binding
        if binding is None or self._session is None:
            raise scoped_authority_phase_error(
                "scoped repository requires a caller-owned bound Session"
            )
        binding.require(constraint)
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
            note.company = data.company
            note.position = data.position
            note.round = data.round
            note.date = data.date
            note.questions = data.questions
            note.self_reflection = data.self_reflection
            note.difficulty_points = data.difficulty_points
            note.mood = data.mood
            note.application_event_id = event_id
            try:
                finish_repository_write(session, self._session)
            except IntegrityError as exc:
                rollback_repository_write(session, self._session)
                if event_id is not None:
                    raise NoteBindingError(409, "Interview event already has a note") from exc
                raise
            session.refresh(note)
            return note

    def delete(self, note_id: int) -> None:
        with repository_session(self._session_factory, self._session) as session:
            note = cast(Optional[InterviewNote], session.scalar(self._visible_note_statement(note_id)))
            if note is not None:
                session.delete(note)
                finish_repository_write(session, self._session)

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
