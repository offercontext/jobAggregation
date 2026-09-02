from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from builtins import list as BuiltinList

from sqlalchemy import and_, case, exists, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.application_status import (
    FIRST_STATUS_TIMESTAMP_ATTR,
    mark_first_status_timestamp,
    normalize_application_status,
)
from offerpilot.models import APPLICATION_FOREIGN_KEY_MODELS, Application
from offerpilot.repositories.session_binding import (
    ScopedRepositoryBinding,
    ScopeAccessDenied,
    attach_scoped_repository,
    bind_scoped_repository,
    finish_repository_write,
    require_scoped_positive_int64,
    repository_session,
    scoped_authority_phase_error,
)

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.contracts import ApplicationScopeConstraint, ToolExecutionAuthority
    from offerpilot.repositories.session_binding import AuthorityFactoryProtocol


@dataclass
class ApplicationCreate:
    company_name: str
    position_name: str
    job_url: str = ""
    status: str = "applied"
    source: str = "cli"
    notes: str = ""
    applied_at: Optional[datetime] = None
    closed_reason: str = ""


APPLICATION_INDEX_TEXT_CODEPOINT_CAP = 160


@dataclass(frozen=True)
class ApplicationListIndexRow:
    id: int
    company_name: str
    position_name: str
    status: str
    full_payload_byte_upper_bound: int


class ApplicationsRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        session: Session | None = None,
    ):
        self._session_factory = session_factory
        self._session = session
        self._scope_binding: ScopedRepositoryBinding | None = None

    def bind(self, session: Session) -> "ApplicationsRepository":
        return ApplicationsRepository(self._session_factory, session)

    def bind_scoped(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        *,
        authority_factory: AuthorityFactoryProtocol,
        authority: ToolExecutionAuthority,
    ) -> "ApplicationsRepository":
        repository = ApplicationsRepository(self._session_factory, session)
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

    def create(self, data: ApplicationCreate) -> Application:
        now = datetime.now(timezone.utc)
        applied_at = data.applied_at or now
        status = normalize_application_status(data.status)
        closed_reason = data.closed_reason.strip() if status == "closed" else ""
        if status == "closed" and not closed_reason:
            raise ValueError("closed_reason is required when closing an application")
        app = Application(
            company_name=data.company_name,
            position_name=data.position_name,
            job_url=data.job_url,
            status=status,
            source=data.source or "cli",
            notes=data.notes,
            applied_at=applied_at,
            closed_reason=closed_reason,
            updated_at=now,
        )
        mark_first_status_timestamp(app, status, now)
        with repository_session(self._session_factory, self._session) as session:
            session.add(app)
            finish_repository_write(session, self._session)
            session.refresh(app)
            return app

    def list(self, status: str = "") -> list[Application]:
        statement = select(Application).where(Application.deleted_at.is_(None))
        if status:
            statement = statement.where(Application.status == normalize_application_status(status))
        statement = statement.order_by(Application.applied_at.desc())
        with repository_session(self._session_factory, self._session) as session:
            return [_normalize_model_status(item) for item in session.scalars(statement)]

    def get(self, app_id: int) -> Optional[Application]:
        with repository_session(self._session_factory, self._session) as session:
            app = session.get(Application, app_id)
            if app is None or app.deleted_at is not None:
                return None
            return _normalize_model_status(app)

    def list_applications_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        status: str = "",
        limit: int | None = None,
    ) -> BuiltinList[Application]:
        binding = self._require_scoped(constraint)
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("application list limit must be a positive integer")
        session = binding.session
        if constraint.mode == "unrestricted":
            with session.no_autoflush:
                statement = select(Application).where(Application.deleted_at.is_(None))
                if status:
                    statement = statement.where(Application.status == normalize_application_status(status))
                statement = statement.order_by(Application.applied_at.desc(), Application.id.desc())
                if limit is not None:
                    statement = statement.limit(limit)
                return [_normalize_model_status(item) for item in session.scalars(statement)]

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(Application.id.label("_scope_application_id"))
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application")
        )
        join_condition = Application.id == scope_parent.c._scope_application_id
        if status:
            join_condition = and_(
                join_condition,
                Application.status == normalize_application_status(status),
            )
        statement = (
            select(Application, scope_parent.c._scope_application_id)
            .select_from(scope_parent.outerjoin(Application, join_condition))
            .order_by(Application.applied_at.desc(), Application.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0][1] is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [_normalize_model_status(row[0]) for row in rows if row[0] is not None]

    def list_application_index_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        status: str = "",
        limit: int | None = None,
    ) -> BuiltinList[ApplicationListIndexRow]:
        binding = self._require_scoped(constraint)
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("application index limit must be a positive integer")
        session = binding.session
        columns = (
            Application.id.label("application_id"),
            func.substr(
                Application.company_name,
                1,
                APPLICATION_INDEX_TEXT_CODEPOINT_CAP,
            ).label("company_name"),
            func.substr(
                Application.position_name,
                1,
                APPLICATION_INDEX_TEXT_CODEPOINT_CAP,
            ).label("position_name"),
            func.substr(
                Application.status,
                1,
                APPLICATION_INDEX_TEXT_CODEPOINT_CAP,
            ).label("status"),
            _application_payload_upper_bound().label("full_payload_byte_upper_bound"),
        )
        status_filter = normalize_application_status(status) if status else ""
        if constraint.mode == "unrestricted":
            statement = select(*columns).where(Application.deleted_at.is_(None))
            if status_filter:
                statement = statement.where(Application.status == status_filter)
            statement = statement.order_by(Application.applied_at.desc(), Application.id.desc())
            if limit is not None:
                statement = statement.limit(limit)
            with session.no_autoflush:
                rows = session.execute(statement).all()
            return [_application_index_row(row) for row in rows]

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(Application.id.label("_scope_application_id"))
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application_index")
        )
        join_condition = Application.id == scope_parent.c._scope_application_id
        if status_filter:
            join_condition = and_(join_condition, Application.status == status_filter)
        statement = (
            select(*columns, scope_parent.c._scope_application_id)
            .select_from(scope_parent.outerjoin(Application, join_condition))
            .order_by(Application.applied_at.desc(), Application.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0]._scope_application_id is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [_application_index_row(row) for row in rows if row.application_id is not None]

    def get_application_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        app_id: int,
    ) -> Optional[Application]:
        binding = self._require_scoped(constraint)
        app_id = require_scoped_positive_int64(app_id, "application id")
        session = binding.session
        if constraint.mode == "unrestricted":
            with session.no_autoflush:
                app = session.scalar(
                    select(Application)
                    .where(Application.id == app_id)
                    .where(Application.deleted_at.is_(None))
                )
        else:
            allowed_id = _restricted_scope_id(constraint)
            with session.no_autoflush:
                app = session.scalar(
                    select(Application)
                    .where(Application.id == app_id)
                    .where(Application.id == allowed_id)
                    .where(Application.deleted_at.is_(None))
                )
        if app is None and constraint.mode == "restricted":
            raise ScopeAccessDenied("application scope denied")
        return _normalize_model_status(app) if app is not None else None

    def update_full(self, app_id: int, data: ApplicationCreate) -> Optional[Application]:
        with repository_session(self._session_factory, self._session) as session:
            app = session.get(Application, app_id)
            if app is None or app.deleted_at is not None:
                return None
            status = normalize_application_status(data.status)
            if app.status == "closed" and status != "closed":
                raise ValueError("closed application cannot be reopened")
            entering_closed = app.status != "closed" and status == "closed"
            closed_reason = data.closed_reason.strip()
            if status == "closed" and not entering_closed and not closed_reason:
                closed_reason = app.closed_reason
            if status == "closed" and not closed_reason:
                raise ValueError("closed_reason is required when closing an application")
            app.company_name = data.company_name
            app.position_name = data.position_name
            app.job_url = data.job_url
            app.status = status
            app.source = data.source or app.source
            app.notes = data.notes
            if status == "closed":
                app.closed_reason = closed_reason
            else:
                app.closed_reason = ""
            now = datetime.now(timezone.utc)
            mark_first_status_timestamp(app, status, now)
            app.updated_at = now
            finish_repository_write(session, self._session)
            session.refresh(app)
            return app

    def update_application_status_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        app_id: int,
        status: str,
        closed_reason: str = "",
    ) -> Optional[Application]:
        """Update status through the final authority-constrained statement."""

        binding = self._require_scoped(constraint)
        app_id = require_scoped_positive_int64(app_id, "application id")
        normalized_status = normalize_application_status(status)
        normalized_reason = closed_reason.strip() if normalized_status == "closed" else ""

        now = datetime.now(timezone.utc)
        transition_allowed = None
        transition_error = ""
        if normalized_status != "closed":
            transition_allowed = Application.status != "closed"
            transition_error = "closed application cannot be reopened"
        elif not normalized_reason:
            transition_allowed = and_(
                Application.status == "closed",
                Application.closed_reason != "",
            )
            transition_error = "closed_reason is required when closing an application"

        desired_reason: object
        if normalized_status != "closed":
            desired_reason = ""
        elif normalized_reason:
            desired_reason = normalized_reason
        else:
            desired_reason = Application.closed_reason

        def guarded_value(column: object, value: object) -> object:
            if transition_allowed is None:
                return value
            return case(
                (transition_allowed, value),
                else_=column,
            )

        values: dict[str, object] = {
            "status": guarded_value(Application.status, normalized_status),
            "closed_reason": guarded_value(Application.closed_reason, desired_reason),
            "updated_at": guarded_value(Application.updated_at, now),
        }
        timestamp_attr = FIRST_STATUS_TIMESTAMP_ATTR[normalized_status]
        timestamp_column = getattr(Application, timestamp_attr)
        desired_timestamp = case(
            (timestamp_column.is_(None), now), else_=timestamp_column
        )
        values[timestamp_attr] = guarded_value(
            timestamp_column,
            desired_timestamp,
        )
        statement = (
            update(Application)
            .where(Application.id == app_id)
            .where(Application.deleted_at.is_(None))
        )
        if constraint.mode == "restricted":
            allowed_id = _restricted_scope_id(constraint)
            statement = statement.where(Application.id == allowed_id)
        statement = statement.values(**values).returning(
            Application,
            (
                transition_allowed
                if transition_allowed is not None
                else Application.id.is_not(None)
            ).label("transition_allowed"),
        )
        with binding.session.no_autoflush:
            rows = list(
                binding.session.execute(
                    statement.execution_options(populate_existing=True)
                )
            )
        if len(rows) != 1:
            if constraint.mode == "restricted" or len(rows) > 1:
                raise ScopeAccessDenied("application scope denied")
            return None
        app, allowed = rows[0]
        if not allowed:
            raise ValueError(transition_error)
        return _normalize_model_status(app)

    def delete(self, app_id: int) -> None:
        with repository_session(self._session_factory, self._session) as session:
            app = session.get(Application, app_id)
            if app is not None and app.deleted_at is None:
                app.deleted_at = datetime.now(timezone.utc)
                finish_repository_write(session, self._session)

    def delete_if_matches(self, app_id: int, expected: dict[str, Any]) -> bool:
        applied_at = expected.get("applied_at")
        if isinstance(applied_at, str):
            applied_at = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
        updated_at = expected.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        with repository_session(self._session_factory, self._session) as session:
            statement = (
                update(Application)
                .where(Application.id == app_id)
                .where(Application.deleted_at.is_(None))
                .where(Application.company_name == expected.get("company_name"))
                .where(Application.position_name == expected.get("position_name"))
                .where(Application.job_url == expected.get("job_url"))
                .where(Application.status == expected.get("status"))
                .where(Application.source == expected.get("source"))
                .where(Application.notes == expected.get("notes"))
                .where(Application.applied_at == applied_at)
                .where(Application.closed_reason == expected.get("closed_reason"))
                .where(Application.updated_at == updated_at)
            )
            for dependency_model in APPLICATION_FOREIGN_KEY_MODELS:
                statement = statement.where(
                    ~exists().where(dependency_model.application_id == app_id)
                )
            result = session.execute(statement.values(deleted_at=datetime.now(timezone.utc)))
            finish_repository_write(session, self._session)
            return getattr(result, "rowcount", 0) == 1

    def restore_status_if_matches(
        self,
        app_id: int,
        *,
        expected_status: str,
        expected_closed_reason: str,
        status: str,
        closed_reason: str,
    ) -> bool:
        with repository_session(self._session_factory, self._session) as session:
            result = session.execute(
                update(Application)
                .where(Application.id == app_id)
                .where(Application.deleted_at.is_(None))
                .where(Application.status == normalize_application_status(expected_status))
                .where(Application.closed_reason == expected_closed_reason)
                .values(
                    status=normalize_application_status(status),
                    closed_reason=closed_reason,
                )
            )
            finish_repository_write(session, self._session)
            return getattr(result, "rowcount", 0) == 1

    def dashboard(self) -> dict[str, Any]:
        apps = self.list()
        board: dict[str, list[Application]] = {}
        for app in apps:
            board.setdefault(app.status, []).append(app)
        return {"total": len(apps), "board": board}


def _normalize_model_status(app: Application) -> Application:
    app.status = normalize_application_status(app.status)
    for attr in FIRST_STATUS_TIMESTAMP_ATTR.values():
        value = getattr(app, attr)
        if value is not None and value.tzinfo is None:
            setattr(app, attr, value.replace(tzinfo=timezone.utc))
    return app


def _restricted_scope_id(constraint: ApplicationScopeConstraint) -> int:
    if constraint.mode != "restricted" or len(constraint.allowed_identities) != 1:
        raise scoped_authority_phase_error(
            "restricted Application scope must contain one identity"
        )
    identity = next(iter(constraint.allowed_identities))
    return require_scoped_positive_int64(identity, "restricted Application scope identity")


def _application_payload_upper_bound() -> Any:
    text_columns = (
        Application.company_name,
        Application.position_name,
        Application.job_url,
        Application.status,
        Application.source,
        Application.notes,
        Application.closed_reason,
    )
    text_codepoints = sum(func.length(func.coalesce(column, "")) for column in text_columns)
    # Python's ensure_ascii=False JSON encoding uses at most six UTF-8 bytes per
    # source codepoint (for escaped controls). The fixed allowance covers keys,
    # numeric identities, nullable timestamps, separators, and list framing.
    return text_codepoints * 6 + 1_024


def _application_index_row(row: Any) -> ApplicationListIndexRow:
    return ApplicationListIndexRow(
        id=int(row.application_id),
        company_name=str(row.company_name or ""),
        position_name=str(row.position_name or ""),
        status=normalize_application_status(str(row.status or "applied")),
        full_payload_byte_upper_bound=int(row.full_payload_byte_upper_bound),
    )
