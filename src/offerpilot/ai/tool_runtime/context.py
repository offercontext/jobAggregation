from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.agent_runtime.journal import RunRecorder
from offerpilot.ai.tool_authority.contracts import (
    ApplicationScopeConstraint,
    AuthorityPhaseError,
    BindingTargetResolution,
    ToolExecutionAuthority,
)
from offerpilot.ai.tool_runtime.contracts import (
    ToolFailure,
    TransientToolRuntimeValue,
)
from offerpilot.models import ApplicationEvent, InterviewNote, JDAnalysis, Offer
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.composition import AuthorityFactory


@dataclass(frozen=True, init=False, repr=False)
class ToolExecutionContext(TransientToolRuntimeValue):
    """Exact-authority execution dependencies for one live Typed segment.

    Authorization is deliberately not accepted as raw constructor data.  The
    active factory supplies the one scope constraint, and every bound clone
    retains that exact object while swapping all repositories onto one
    caller-owned Session.
    """

    authority: ToolExecutionAuthority = field(repr=False)
    applications: ApplicationsRepository = field(repr=False)
    events: ApplicationEventsRepository = field(repr=False)
    notes: NotesRepository = field(repr=False)
    offers: OffersRepository = field(repr=False)
    resumes: ResumesRepository = field(repr=False)
    jd_analyses: JDAnalysesRepository = field(repr=False)
    run_recorder: RunRecorder = field(repr=False, compare=False)
    _authority_factory: AuthorityFactory = field(repr=False, compare=False)
    _scope_constraint: ApplicationScopeConstraint = field(repr=False, compare=False)
    _session_factory: sessionmaker[Session] = field(repr=False, compare=False)
    operation_executor: Any = field(default=None, repr=False, compare=False)
    _bound_session: Session | None = field(default=None, repr=False, compare=False)
    _origin_context: "ToolExecutionContext | None" = field(default=None, repr=False, compare=False)

    def __init__(
        self,
        *,
        authority: ToolExecutionAuthority,
        applications: ApplicationsRepository,
        events: ApplicationEventsRepository,
        notes: NotesRepository,
        offers: OffersRepository,
        resumes: ResumesRepository,
        jd_analyses: JDAnalysesRepository,
        run_recorder: RunRecorder,
        operation_executor: Any = None,
    ) -> None:
        from offerpilot.ai.tool_authority.composition import _active_factory

        factory = _active_factory(authority)
        constraint = factory.create_application_scope_constraint(authority)
        repository_factory = self._require_common_session_factory(
            applications, events, notes, offers, resumes, jd_analyses
        )
        self._assign(
            authority=authority,
            applications=applications,
            events=events,
            notes=notes,
            offers=offers,
            resumes=resumes,
            jd_analyses=jd_analyses,
            run_recorder=run_recorder,
            operation_executor=operation_executor,
            authority_factory=factory,
            scope_constraint=constraint,
            bound_session=None,
            origin_context=None,
            repository_factory=repository_factory,
        )

    @staticmethod
    def _require_common_session_factory(
        applications: ApplicationsRepository,
        events: ApplicationEventsRepository,
        notes: NotesRepository,
        offers: OffersRepository,
        resumes: ResumesRepository,
        jd_analyses: JDAnalysesRepository,
    ) -> sessionmaker[Session]:
        factories = tuple(
            getattr(repository, "_session_factory", None)
            for repository in (applications, events, notes, offers, resumes, jd_analyses)
        )
        first = factories[0]
        if first is None or any(item is not first for item in factories[1:]):
            raise TypeError("ToolExecutionContext repositories must share one Session factory")
        return cast(sessionmaker[Session], first)

    def _assign(
        self,
        *,
        authority: ToolExecutionAuthority,
        applications: ApplicationsRepository,
        events: ApplicationEventsRepository,
        notes: NotesRepository,
        offers: OffersRepository,
        resumes: ResumesRepository,
        jd_analyses: JDAnalysesRepository,
        run_recorder: RunRecorder,
        operation_executor: Any,
        authority_factory: AuthorityFactory,
        scope_constraint: ApplicationScopeConstraint,
        bound_session: Session | None,
        origin_context: "ToolExecutionContext | None",
        repository_factory: sessionmaker[Session],
    ) -> None:
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "applications", applications)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "notes", notes)
        object.__setattr__(self, "offers", offers)
        object.__setattr__(self, "resumes", resumes)
        object.__setattr__(self, "jd_analyses", jd_analyses)
        object.__setattr__(self, "run_recorder", run_recorder)
        object.__setattr__(self, "operation_executor", operation_executor)
        object.__setattr__(self, "_authority_factory", authority_factory)
        object.__setattr__(self, "_scope_constraint", scope_constraint)
        object.__setattr__(self, "_bound_session", bound_session)
        object.__setattr__(self, "_origin_context", origin_context)
        object.__setattr__(self, "_session_factory", repository_factory)

    @property
    def authority_factory(self) -> AuthorityFactory:
        return self._authority_factory

    @property
    def scope_constraint(self) -> ApplicationScopeConstraint:
        self._authority_factory.require_scope_constraint(self._scope_constraint, self.authority)
        return self._scope_constraint

    @property
    def bound_session(self) -> Session | None:
        return self._bound_session

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def with_runtime_dependencies(
        self,
        *,
        run_recorder: RunRecorder,
        operation_executor: Any,
    ) -> "ToolExecutionContext":
        """Clone an unbound origin while retaining its sealed authority scope."""

        if self._bound_session is not None or self._origin_context is not None:
            raise AuthorityPhaseError(
                "runtime dependencies require an unbound ToolExecutionContext origin"
            )
        constraint = self.scope_constraint
        clone = object.__new__(ToolExecutionContext)
        clone._assign(
            authority=self.authority,
            applications=self.applications,
            events=self.events,
            notes=self.notes,
            offers=self.offers,
            resumes=self.resumes,
            jd_analyses=self.jd_analyses,
            run_recorder=run_recorder,
            operation_executor=operation_executor,
            authority_factory=self._authority_factory,
            scope_constraint=constraint,
            bound_session=None,
            origin_context=None,
            repository_factory=self._session_factory,
        )
        return clone

    def require_bound_origin(
        self,
        origin: "ToolExecutionContext",
        session: Session,
    ) -> None:
        """Require this context to be the exact transaction carrier derived from origin."""

        if (
            type(origin) is not ToolExecutionContext
            or self._origin_context is not origin
            or origin._origin_context is not None
            or origin._bound_session is not None
            or self._bound_session is not session
            or self.authority is not origin.authority
            or self._authority_factory is not origin._authority_factory
            or self._scope_constraint is not origin._scope_constraint
            or self._session_factory is not origin._session_factory
            or self.operation_executor is not origin.operation_executor
            or self.run_recorder is not origin.run_recorder
        ):
            raise AuthorityPhaseError(
                "bound ToolExecutionContext does not match its approval origin"
            )
        for bound, source in (
            (self.applications, origin.applications),
            (self.events, origin.events),
            (self.notes, origin.notes),
            (self.offers, origin.offers),
            (self.resumes, origin.resumes),
            (self.jd_analyses, origin.jd_analyses),
        ):
            if (
                type(bound) is not type(source)
                or getattr(bound, "_session", None) is not session
                or getattr(bound, "_session_factory", None)
                is not getattr(source, "_session_factory", None)
            ):
                raise AuthorityPhaseError("bound ToolExecutionContext repository carrier mismatch")

    def bind(self, session: Session) -> "ToolExecutionContext":
        if not isinstance(session, Session):
            raise TypeError("ToolExecutionContext.bind requires a caller-owned Session")
        factory = self._authority_factory
        constraint = self.scope_constraint
        bound = object.__new__(ToolExecutionContext)
        bound._assign(
            authority=self.authority,
            applications=self.applications.bind_scoped(
                session,
                constraint,
                authority_factory=factory,
                authority=self.authority,
            ),
            events=self.events.bind_scoped(
                session,
                constraint,
                authority_factory=factory,
                authority=self.authority,
            ),
            notes=self.notes.bind_scoped(
                session,
                constraint,
                authority_factory=factory,
                authority=self.authority,
            ),
            offers=self.offers.bind_scoped(
                session,
                constraint,
                authority_factory=factory,
                authority=self.authority,
            ),
            resumes=self.resumes.bind(session),
            jd_analyses=self.jd_analyses.bind_scoped(
                session,
                constraint,
                authority_factory=factory,
                authority=self.authority,
            ),
            run_recorder=self.run_recorder,
            operation_executor=self.operation_executor,
            authority_factory=factory,
            scope_constraint=constraint,
            bound_session=session,
            origin_context=self,
            repository_factory=self._session_factory,
        )
        return bound

    def binding_target_resolution(
        self,
        *,
        entity_kind: Literal["application", "resume"],
        state: Literal["resolved", "omitted", "detached", "unavailable"],
        identity: int | None,
    ) -> BindingTargetResolution:
        return self._authority_factory.create_binding_target_resolution(
            self.authority,
            entity_kind=entity_kind,
            state=state,
            identity=identity,
        )

    def resolver_context(self, resolver_id: str) -> "_BindingResolverContext":
        return _BindingResolverContext(self, resolver_id)

    def _resolve_parent_identity(self, resolver_id: str, identity: int) -> tuple[str, int | None]:
        model: type[Any]
        if resolver_id == "application_event_parent":
            model = ApplicationEvent
        elif resolver_id == "note_application_parent":
            model = InterviewNote
        elif resolver_id == "offer_application_parent":
            model = Offer
        elif resolver_id == "jd_analysis_application_parent":
            model = JDAnalysis
        else:
            return ("unavailable", None)
        statement = select(model.application_id).where(model.id == identity)
        if self._bound_session is not None:
            row = self._bound_session.execute(statement).one_or_none()
        else:
            with self._session_factory() as session:
                row = session.execute(statement).one_or_none()
        if row is None:
            return ("unavailable", None)
        parent = row[0]
        if parent is None:
            return ("detached", None)
        if type(parent) is not int or not 1 <= parent <= 2**63 - 1:
            return ("unavailable", None)
        return ("resolved", parent)


class _BindingResolverContext:
    __slots__ = ("_context", "_resolver_id")

    def __init__(self, context: ToolExecutionContext, resolver_id: str) -> None:
        self._context = context
        self._resolver_id = resolver_id

    @property
    def applications(self) -> ApplicationsRepository:
        return self._context.applications

    @property
    def events(self) -> ApplicationEventsRepository:
        return self._context.events

    @property
    def notes(self) -> NotesRepository:
        return self._context.notes

    @property
    def offers(self) -> OffersRepository:
        return self._context.offers

    @property
    def resumes(self) -> ResumesRepository:
        return self._context.resumes

    @property
    def jd_analyses(self) -> JDAnalysesRepository:
        return self._context.jd_analyses

    @property
    def authority(self) -> ToolExecutionAuthority:
        return self._context.authority

    @property
    def authority_factory(self) -> AuthorityFactory:
        return self._context.authority_factory

    def binding_target_resolution(self, **values: Any) -> BindingTargetResolution:
        return self._context.binding_target_resolution(**values)

    def resolve_parent_identity(self, entity_kind: str, identity: int) -> tuple[str, int | None]:
        if entity_kind != "application":
            return ("unavailable", None)
        return self._context._resolve_parent_identity(self._resolver_id, identity)


def scope_access_denied() -> ToolFailure:
    return ToolFailure(
        category="permission_denied",
        code="scope_access_denied",
        compatibility_detail="permission denied",
    )


__all__ = [
    "ToolExecutionContext",
    "scope_access_denied",
]
