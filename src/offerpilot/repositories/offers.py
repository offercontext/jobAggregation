from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from builtins import list as BuiltinList

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    ToolExecutionAuthority,
)
from offerpilot.models import Application, Offer
from offerpilot.repositories.applications import _restricted_scope_id
from offerpilot.repositories.session_binding import (
    ScopedRepositoryBinding,
    ScopeAccessDenied,
    bind_scoped_repository,
    finish_repository_write,
    repository_session,
)


@dataclass
class OfferCreate:
    company_name: str
    position_name: str
    application_id: Optional[int] = None
    status: str = "pending"
    base_monthly: int = 0
    months_per_year: int = 12
    signing_bonus: int = 0
    equity: str = ""
    perks: str = ""
    deadline: str = ""
    notes: str = ""
    assessment: str = ""


class OffersRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        session: Session | None = None,
        scope_binding: ScopedRepositoryBinding | None = None,
    ):
        self._session_factory = session_factory
        self._session = session
        self._scope_binding = scope_binding

    def bind(self, session: Session) -> "OffersRepository":
        return OffersRepository(self._session_factory, session)

    def bind_scoped(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        *,
        authority_factory: AuthorityFactory,
        authority: ToolExecutionAuthority,
    ) -> "OffersRepository":
        binding = bind_scoped_repository(
            session,
            constraint,
            authority_factory=authority_factory,
            authority=authority,
        )
        return OffersRepository(self._session_factory, session, binding)

    def _require_scoped(self, constraint: object) -> ScopedRepositoryBinding:
        binding = self._scope_binding
        if binding is None or self._session is None:
            raise AuthorityPhaseError("scoped repository requires a caller-owned bound Session")
        binding.require(constraint)
        return binding

    def create(self, data: OfferCreate) -> Offer:
        offer = Offer(
            application_id=data.application_id,
            company_name=data.company_name,
            position_name=data.position_name,
            status=data.status or "pending",
            base_monthly=data.base_monthly,
            months_per_year=data.months_per_year or 12,
            signing_bonus=data.signing_bonus,
            equity=data.equity,
            perks=data.perks,
            deadline=data.deadline,
            notes=data.notes,
            assessment=data.assessment,
        )
        with repository_session(self._session_factory, self._session) as session:
            session.add(offer)
            finish_repository_write(session, self._session)
            session.refresh(offer)
            return offer

    def list(self, status: str = "") -> list[Offer]:
        statement = select(Offer)
        if status:
            statement = statement.where(Offer.status == status)
        statement = statement.order_by(Offer.created_at.desc())
        with repository_session(self._session_factory, self._session) as session:
            return list(session.scalars(statement))

    def get(self, offer_id: int) -> Optional[Offer]:
        with repository_session(self._session_factory, self._session) as session:
            return session.get(Offer, offer_id)

    def list_offers_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        status: str = "",
    ) -> BuiltinList[Offer]:
        binding = self._require_scoped(constraint)
        session = binding.session
        if constraint.mode == "unrestricted":
            statement = select(Offer)
            if status:
                statement = statement.where(Offer.status == status)
            statement = statement.order_by(Offer.created_at.desc())
            with session.no_autoflush:
                return list(session.scalars(statement))

        allowed_id = _restricted_scope_id(constraint)
        scope_parent = (
            select(Application.id.label("_scope_application_id"))
            .where(Application.id == allowed_id, Application.deleted_at.is_(None))
            .cte("scoped_application")
        )
        join_condition = Offer.application_id == scope_parent.c._scope_application_id
        if status:
            join_condition = and_(join_condition, Offer.status == status)
        statement = (
            select(Offer, scope_parent.c._scope_application_id)
            .select_from(scope_parent.outerjoin(Offer, join_condition))
            .order_by(Offer.created_at.desc())
        )
        with session.no_autoflush:
            rows = session.execute(statement).all()
        if not rows or rows[0][1] is None:
            raise ScopeAccessDenied("application scope is unavailable")
        return [row[0] for row in rows if row[0] is not None]

    def get_offer_scoped(
        self,
        constraint: ApplicationScopeConstraint,
        offer_id: int,
    ) -> Optional[Offer]:
        binding = self._require_scoped(constraint)
        session = binding.session
        if constraint.mode == "unrestricted":
            with session.no_autoflush:
                return session.get(Offer, offer_id)
        allowed_id = _restricted_scope_id(constraint)
        with session.no_autoflush:
            offer = session.scalar(
                select(Offer)
                .join(Application, Application.id == Offer.application_id)
                .where(Offer.id == offer_id)
                .where(Offer.application_id == allowed_id)
                .where(Application.id == allowed_id)
                .where(Application.deleted_at.is_(None))
            )
        if offer is None:
            raise ScopeAccessDenied("application scope denied")
        return offer

    def update(self, offer_id: int, data: OfferCreate) -> Optional[Offer]:
        with repository_session(self._session_factory, self._session) as session:
            offer = session.get(Offer, offer_id)
            if offer is None:
                return None
            offer.company_name = data.company_name
            offer.position_name = data.position_name
            offer.status = data.status or "pending"
            offer.base_monthly = data.base_monthly
            offer.months_per_year = data.months_per_year or offer.months_per_year
            offer.signing_bonus = data.signing_bonus
            offer.equity = data.equity
            offer.perks = data.perks
            offer.deadline = data.deadline
            offer.notes = data.notes
            offer.assessment = data.assessment
            finish_repository_write(session, self._session)
            session.refresh(offer)
            return offer

    def delete(self, offer_id: int) -> None:
        with repository_session(self._session_factory, self._session) as session:
            offer = session.get(Offer, offer_id)
            if offer is not None:
                session.delete(offer)
                finish_repository_write(session, self._session)
