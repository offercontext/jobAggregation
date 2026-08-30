from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from builtins import list as BuiltinList

from sqlalchemy import and_, delete, exists, insert, literal, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from offerpilot.models import Application, ApplicationEvent, InterviewNote
from offerpilot.repositories.applications import _restricted_scope_id
from offerpilot.repositories.notes import _revisioned_note_values
from offerpilot.repositories.session_binding import (
    ScopedRepositoryBinding,
    ScopeAccessDenied,
    attach_scoped_repository,
    bind_scoped_repository,
    finish_repository_write,
    require_scoped_optional_id,
    require_scoped_positive_int64,
    repository_session,
    scoped_authority_phase_error,
)

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.contracts import ApplicationScopeConstraint, ToolExecutionAuthority
    from offerpilot.repositories.session_binding import AuthorityFactoryProtocol


@dataclass
class ApplicationEventCreate:
    application_id: int
    event_type: str
    scheduled_at: datetime
    duration_minutes: int
    subtype: str = ""
    tags: list[str] | None = None
    round: int = 0
    location: str = ""
    notes: str = ""
    remind_at: datetime | None = None
    status: str = "todo"


@dataclass
class ApplicationEventWithApplication:
    event: ApplicationEvent
    company_name: str
    position_name: str


class ApplicationEventsRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        session: Session | None = None,
    ):
        self._session_factory = session_factory
        self._session = session
        self._scope_binding: ScopedRepositoryBinding | None = None

    def bind(self, session: Session) -> "ApplicationEventsRepository":
        return ApplicationEventsRepository(self._session_factory, session)

    def bind_scoped(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        *,
        authority_factory: AuthorityFactoryProtocol,
        authority: ToolExecutionAuthority,
    ) -> "ApplicationEventsRepository":
        repository = ApplicationEventsRepository(self._session_factory, session)
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

    def create(self, data: ApplicationEventCreate) -> ApplicationEvent:
        event = ApplicationEvent(
            application_id=data.application_id,
            event_type=data.event_type,
            subtype=data.subtype,
            round=data.round,
            scheduled_at=data.scheduled_at,
            duration_minutes=data.duration_minutes,
            location=data.location,
            notes=data.notes,
            remind_at=data.remind_at,
            status=data.status or "todo",
        )
        event.tags = data.tags or []
        with repository_session(self._session_factory, self._session) as session:
            session.add(event)
            finish_repository_write(session, self._session)
            session.refresh(event)
            return event

    def create_application_event_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        data: ApplicationEventCreate,
    ) -> ApplicationEvent:
        binding = self._require_scoped(constraint)
        application_id = require_scoped_positive_int64(
            data.application_id, "application_id"
        )
        values = _event_values(data)
        if constraint.mode == "unrestricted":
            statement = (
                insert(ApplicationEvent)
                .values(**_event_values(data, tags_key="_tags"))
                .returning(ApplicationEvent)
            )
        else:
            allowed_id = _restricted_scope_id(constraint)
            if application_id != allowed_id:
                raise ScopeAccessDenied("application scope denied")
            columns = tuple(values)
            source = select(
                *(literal(values[column]) for column in columns)
            ).select_from(Application)
            source = source.where(
                Application.id == allowed_id,
                Application.deleted_at.is_(None),
            )
            statement = (
                insert(ApplicationEvent)
                .from_select(columns, source)
                .returning(ApplicationEvent)
            )
        with binding.session.no_autoflush:
            rows = list(binding.session.scalars(statement))
        if len(rows) != 1:
            raise ScopeAccessDenied("application scope denied")
        return rows[0]

    def list(
        self,
        month: str = "",
        application_id: int = 0,
        event_type: str = "",
    ) -> list[ApplicationEventWithApplication]:
        statement = (
            select(ApplicationEvent, Application.company_name, Application.position_name)
            .join(Application, Application.id == ApplicationEvent.application_id)
            .where(Application.deleted_at.is_(None))
            .order_by(ApplicationEvent.scheduled_at.asc(), ApplicationEvent.id.asc())
        )
        if month:
            bounds = _month_bounds(month)
            if bounds is not None:
                start, end = bounds
                statement = statement.where(ApplicationEvent.scheduled_at >= start)
                statement = statement.where(ApplicationEvent.scheduled_at < end)
        if application_id > 0:
            statement = statement.where(ApplicationEvent.application_id == application_id)
        if event_type:
            statement = statement.where(ApplicationEvent.event_type == event_type)

        with repository_session(self._session_factory, self._session) as session:
            rows = session.execute(statement).all()
            return [
                ApplicationEventWithApplication(
                    event=row[0], company_name=row[1], position_name=row[2]
                )
                for row in rows
            ]

    def get(self, event_id: int) -> Optional[ApplicationEvent]:
        with repository_session(self._session_factory, self._session) as session:
            return _get_visible_event(session, event_id)

    def list_application_events_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        month: str = "",
        application_id: int | None = None,
        event_type: str = "",
    ) -> BuiltinList[ApplicationEventWithApplication]:
        binding = self._require_scoped(constraint)
        require_scoped_optional_id(application_id, "application_id")
        session = binding.session
        if constraint.mode == "unrestricted":
            statement = (
                select(ApplicationEvent, Application.company_name, Application.position_name)
                .join(Application, Application.id == ApplicationEvent.application_id)
                .where(Application.deleted_at.is_(None))
                .order_by(ApplicationEvent.scheduled_at.asc(), ApplicationEvent.id.asc())
            )
            statement = _event_filters(statement, month, application_id, event_type)
            with session.no_autoflush:
                rows = session.execute(statement).all()
            return [
                ApplicationEventWithApplication(event=row[0], company_name=row[1], position_name=row[2])
                for row in rows
            ]

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(
                Application.id.label("_scope_application_id"),
                Application.company_name.label("_scope_company_name"),
                Application.position_name.label("_scope_position_name"),
            )
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application")
        )
        join_condition = ApplicationEvent.application_id == scope_parent.c._scope_application_id
        if application_id is not None:
            join_condition = and_(join_condition, ApplicationEvent.application_id == application_id)
        if month:
            bounds = _month_bounds(month)
            if bounds is not None:
                start, end = bounds
                join_condition = and_(
                    join_condition,
                    ApplicationEvent.scheduled_at >= start,
                    ApplicationEvent.scheduled_at < end,
                )
        if event_type:
            join_condition = and_(join_condition, ApplicationEvent.event_type == event_type)
        statement = (
            select(
                ApplicationEvent,
                scope_parent.c._scope_company_name,
                scope_parent.c._scope_position_name,
                scope_parent.c._scope_application_id,
            )
            .select_from(scope_parent.outerjoin(ApplicationEvent, join_condition))
            .order_by(ApplicationEvent.scheduled_at.asc(), ApplicationEvent.id.asc())
        )
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0][3] is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [
            ApplicationEventWithApplication(event=row[0], company_name=row[1], position_name=row[2])
            for row in rows
            if row[0] is not None
        ]

    def get_application_event_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        event_id: int,
    ) -> Optional[ApplicationEvent]:
        binding = self._require_scoped(constraint)
        event_id = require_scoped_positive_int64(event_id, "application event id")
        session = binding.session
        if constraint.mode == "unrestricted":
            return _get_visible_event(session, event_id)
        allowed_id = _restricted_scope_id(constraint)
        with session.no_autoflush:
            event = session.scalar(
                select(ApplicationEvent)
                .join(Application, Application.id == ApplicationEvent.application_id)
                .where(ApplicationEvent.id == event_id)
                .where(ApplicationEvent.application_id == allowed_id)
                .where(Application.id == allowed_id)
                .where(Application.deleted_at.is_(None))
            )
        if event is None:
            raise ScopeAccessDenied("application scope denied")
        return event

    def update(self, event_id: int, data: ApplicationEventCreate) -> Optional[ApplicationEvent]:
        with repository_session(self._session_factory, self._session) as session:
            event = _get_visible_event(session, event_id)
            if event is None:
                return None
            event.application_id = data.application_id
            event.event_type = data.event_type
            event.subtype = data.subtype
            event.tags = data.tags or []
            event.round = data.round
            event.scheduled_at = data.scheduled_at
            event.duration_minutes = data.duration_minutes
            event.location = data.location
            event.notes = data.notes
            event.remind_at = data.remind_at
            event.status = data.status or event.status
            finish_repository_write(session, self._session)
            session.refresh(event)
            return event

    def update_application_event_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        event_id: int,
        data: ApplicationEventCreate,
    ) -> Optional[ApplicationEvent]:
        binding = self._require_scoped(constraint)
        event_id = require_scoped_positive_int64(event_id, "application event id")
        application_id = require_scoped_positive_int64(
            data.application_id, "application_id"
        )
        active_parent = exists(
            select(Application.id).where(
                Application.id == ApplicationEvent.application_id,
                Application.deleted_at.is_(None),
            )
        )
        statement = (
            update(ApplicationEvent)
            .where(ApplicationEvent.id == event_id)
            .where(active_parent)
        )
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            if application_id != allowed_id:
                raise ScopeAccessDenied("application scope denied")
            statement = statement.where(ApplicationEvent.application_id == allowed_id)
        statement = (
            statement.values(**_event_values(data, tags_key="_tags"))
            .returning(ApplicationEvent)
            .execution_options(populate_existing=True)
        )
        with binding.session.no_autoflush:
            rows = list(binding.session.scalars(statement))
        if len(rows) != 1:
            if constraint.mode == "restricted" or len(rows) > 1:
                raise ScopeAccessDenied("application scope denied")
            return None
        return rows[0]

    def delete(self, event_id: int) -> bool:
        with repository_session(self._session_factory, self._session) as session:
            active_parent = exists(
                select(Application.id).where(
                    Application.id == ApplicationEvent.application_id,
                    Application.deleted_at.is_(None),
                )
            )
            deleted = _delete_application_event_owned(
                session,
                event_id,
                (active_parent,),
            )
            if deleted:
                finish_repository_write(session, self._session)
            return deleted

    def delete_application_event_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        event_id: int,
    ) -> bool:
        binding = self._require_scoped(constraint)
        event_id = require_scoped_positive_int64(event_id, "application event id")
        active_parent = exists(
            select(Application.id).where(
                Application.id == ApplicationEvent.application_id,
                Application.deleted_at.is_(None),
            )
        )
        predicates: list[ColumnElement[bool]] = [active_parent]
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            predicates.append(ApplicationEvent.application_id == allowed_id)
        with binding.session.no_autoflush:
            deleted = _delete_application_event_owned(
                binding.session,
                event_id,
                tuple(predicates),
            )
        if not deleted:
            if constraint.mode == "restricted":
                raise ScopeAccessDenied("application scope denied")
            return False
        return True

    def delete_if_matches(self, event_id: int, expected: dict[str, object]) -> bool:
        scheduled_at = _expected_datetime(expected.get("scheduled_at"))
        remind_at = _expected_datetime(expected.get("remind_at"))
        tags = expected.get("tags")
        encoded_tags = json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False)
        predicates: list[ColumnElement[bool]] = [
            ApplicationEvent.application_id == expected.get("application_id"),
            ApplicationEvent.event_type == expected.get("event_type"),
            ApplicationEvent.subtype == expected.get("subtype"),
            ApplicationEvent._tags == encoded_tags,
            ApplicationEvent.round == expected.get("round"),
            ApplicationEvent.scheduled_at == scheduled_at,
            ApplicationEvent.duration_minutes == expected.get("duration_minutes"),
            ApplicationEvent.location == expected.get("location"),
            ApplicationEvent.notes == expected.get("notes"),
            ApplicationEvent.status == expected.get("status"),
        ]
        predicates.append(
            ApplicationEvent.remind_at.is_(None)
            if remind_at is None
            else ApplicationEvent.remind_at == remind_at
        )
        with repository_session(self._session_factory, self._session) as session:
            deleted = _delete_application_event_owned(
                session,
                event_id,
                tuple(predicates),
            )
            if deleted:
                finish_repository_write(session, self._session)
            return deleted


def _delete_application_event_owned(
    session: Session,
    event_id: int,
    predicates: tuple[ColumnElement[bool], ...],
) -> bool:
    """Unbind surviving Notes and delete one exact Event under caller ownership."""

    exact_event = and_(ApplicationEvent.id == event_id, *predicates)
    event_still_matches = exists(select(ApplicationEvent.id).where(exact_event))
    connection = session.connection()
    driver_connection = getattr(connection.connection, "driver_connection", None)
    if (
        connection.dialect.name == "sqlite"
        and driver_connection is not None
        and not driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    with session.begin_nested():
        session.execute(
            update(InterviewNote)
            .where(
                InterviewNote.application_event_id == event_id,
                event_still_matches,
            )
            .values(**_revisioned_note_values({"application_event_id": None}))
        )
        result = session.execute(delete(ApplicationEvent).where(exact_event))
        return getattr(result, "rowcount", 0) == 1


def _get_visible_event(session: Session, event_id: int) -> Optional[ApplicationEvent]:
    event = session.scalar(
        select(ApplicationEvent)
        .join(Application, Application.id == ApplicationEvent.application_id)
        .where(ApplicationEvent.id == event_id)
        .where(Application.deleted_at.is_(None))
    )
    return event


def _event_values(
    data: ApplicationEventCreate,
    *,
    tags_key: str = "tags",
) -> dict[str, object]:
    return {
        "application_id": data.application_id,
        "event_type": data.event_type,
        "subtype": data.subtype,
        tags_key: json.dumps(data.tags or [], ensure_ascii=False),
        "round": data.round,
        "scheduled_at": data.scheduled_at,
        "duration_minutes": data.duration_minutes,
        "location": data.location,
        "notes": data.notes,
        "remind_at": data.remind_at,
        "status": data.status or "todo",
    }


def _event_filters(
    statement: Select[Any],
    month: str,
    application_id: int | None,
    event_type: str,
) -> Select[Any]:
    if month:
        bounds = _month_bounds(month)
        if bounds is not None:
            start, end = bounds
            statement = statement.where(ApplicationEvent.scheduled_at >= start)
            statement = statement.where(ApplicationEvent.scheduled_at < end)
    if application_id is not None:
        statement = statement.where(ApplicationEvent.application_id == application_id)
    if event_type:
        statement = statement.where(ApplicationEvent.event_type == event_type)
    return statement


def duration_minutes(duration: str | int) -> int:
    if isinstance(duration, int):
        return duration
    return int(str(duration).removesuffix("m") or "0")


def _expected_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _month_bounds(month: str) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.strptime(month, "%Y-%m")
    except ValueError:
        return None
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end
