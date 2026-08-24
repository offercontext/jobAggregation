from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from builtins import list as BuiltinList

from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    ToolExecutionAuthority,
)
from offerpilot.models import Application, ApplicationJDVersion, JDAnalysis
from offerpilot.repositories.application_jd_versions import JDVersionConflictError
from offerpilot.repositories.applications import _restricted_scope_id
from offerpilot.repositories.session_binding import (
    ScopedRepositoryBinding,
    ScopeAccessDenied,
    bind_scoped_repository,
    repository_session,
)


@dataclass
class JDAnalysisCreate:
    jd_source: str
    jd_text: str
    result: str
    application_id: Optional[int] = None
    jd_version_id: Optional[int] = None


class JDAnalysesRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        session: Session | None = None,
        scope_binding: ScopedRepositoryBinding | None = None,
    ):
        self._session_factory = session_factory
        self._session = session
        self._scope_binding = scope_binding

    def bind(self, session: Session) -> "JDAnalysesRepository":
        return JDAnalysesRepository(self._session_factory, session)

    def bind_scoped(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        *,
        authority_factory: AuthorityFactory,
        authority: ToolExecutionAuthority,
    ) -> "JDAnalysesRepository":
        binding = bind_scoped_repository(
            session,
            constraint,
            authority_factory=authority_factory,
            authority=authority,
        )
        return JDAnalysesRepository(self._session_factory, session, binding)

    def _require_scoped(self, constraint: object) -> ScopedRepositoryBinding:
        binding = self._scope_binding
        if binding is None or self._session is None:
            raise AuthorityPhaseError("scoped repository requires a caller-owned bound Session")
        binding.require(constraint)
        return binding

    def create(self, data: JDAnalysisCreate) -> JDAnalysis:
        analysis = JDAnalysis(
            application_id=data.application_id,
            jd_source=data.jd_source,
            jd_text=data.jd_text,
            result=data.result,
            jd_version_id=data.jd_version_id,
        )
        with self._session_factory() as session:
            session.add(analysis)
            session.commit()
            session.refresh(analysis)
            return analysis

    def create_for_current(self, data: JDAnalysisCreate) -> JDAnalysis:
        if data.application_id is None or data.jd_version_id is None:
            return self.create(data)
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = session.scalar(
                select(ApplicationJDVersion)
                .where(ApplicationJDVersion.application_id == data.application_id)
                .order_by(desc(ApplicationJDVersion.version_number))
                .limit(1)
            )
            if current is None or current.id != data.jd_version_id:
                raise JDVersionConflictError("requested JD version is not current")
            analysis = JDAnalysis(
                application_id=data.application_id,
                jd_source=data.jd_source,
                jd_text=data.jd_text,
                result=data.result,
                jd_version_id=data.jd_version_id,
            )
            session.add(analysis)
            session.commit()
            session.refresh(analysis)
            return analysis

    def list(self, application_id: int = 0) -> list[JDAnalysis]:
        statement = select(JDAnalysis)
        if application_id > 0:
            statement = statement.where(JDAnalysis.application_id == application_id)
        statement = statement.order_by(JDAnalysis.created_at.desc())
        with repository_session(self._session_factory, self._session) as session:
            return list(session.scalars(statement))

    def get(self, analysis_id: int) -> Optional[JDAnalysis]:
        with repository_session(self._session_factory, self._session) as session:
            return session.get(JDAnalysis, analysis_id)

    def list_jd_analyses_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        application_id: int = 0,
    ) -> BuiltinList[JDAnalysis]:
        binding = self._require_scoped(constraint)
        session = binding.session
        if constraint.mode == "unrestricted":
            statement = select(JDAnalysis)
            if application_id > 0:
                statement = statement.where(JDAnalysis.application_id == application_id)
            statement = statement.order_by(JDAnalysis.created_at.desc())
            with session.no_autoflush:
                return list(session.scalars(statement))

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(Application.id.label("_scope_application_id"))
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application")
        )
        join_condition = JDAnalysis.application_id == scope_parent.c._scope_application_id
        if application_id > 0:
            join_condition = join_condition & (JDAnalysis.application_id == application_id)
        statement = (
            select(JDAnalysis, scope_parent.c._scope_application_id)
            .select_from(scope_parent.outerjoin(JDAnalysis, join_condition))
            .order_by(JDAnalysis.created_at.desc())
        )
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0][1] is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [row[0] for row in rows if row[0] is not None]

    def get_jd_analysis_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        analysis_id: int,
    ) -> Optional[JDAnalysis]:
        binding = self._require_scoped(constraint)
        session = binding.session
        if constraint.mode == "unrestricted":
            with session.no_autoflush:
                return session.get(JDAnalysis, analysis_id)
        allowed_id = _restricted_scope_id(constraint)
        with session.no_autoflush:
            analysis = session.scalar(
                select(JDAnalysis)
                .join(Application, Application.id == JDAnalysis.application_id)
                .where(JDAnalysis.id == analysis_id)
                .where(JDAnalysis.application_id == allowed_id)
                .where(Application.id == allowed_id)
                .where(Application.deleted_at.is_(None))
            )
        if analysis is None:
            raise ScopeAccessDenied("application scope denied")
        return analysis
