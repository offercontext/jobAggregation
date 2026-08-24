from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    AuthorityPhaseError,
    ToolExecutionAuthority,
)


class ScopeAccessDenied(LookupError):
    """A scoped query cannot prove that its target is visible to the scope."""


@dataclass(frozen=True, slots=True)
class ScopedRepositoryBinding:
    """Caller-owned Session plus the exact factory-bound scope capability.

    Repository methods intentionally retain the original constraint object
    rather than copying its fields.  The factory validation is repeated before
    every scoped statement so a revoked or mutated transient value cannot
    reach SQL.
    """

    session: Session
    constraint: ApplicationScopeConstraint
    authority_factory: AuthorityFactory
    authority: ToolExecutionAuthority

    def require(self, constraint: object) -> None:
        if constraint is not self.constraint:
            raise AuthorityPhaseError("scoped repository constraint identity mismatch")
        self.authority_factory.require_scope_constraint(self.constraint, self.authority)


def bind_scoped_repository(
    session: Session,
    constraint: ApplicationScopeConstraint,
    *,
    authority_factory: AuthorityFactory,
    authority: ToolExecutionAuthority,
) -> ScopedRepositoryBinding:
    """Create a sealed repository binding for one caller-owned Session."""

    if type(session) is not Session:
        # SQLAlchemy may provide Session subclasses in tests/app integrations;
        # the runtime check below is deliberately structural for those.
        if not isinstance(session, Session):
            raise AuthorityPhaseError("scoped repository requires a caller-owned Session")
    if type(constraint) is not ApplicationScopeConstraint:
        raise AuthorityPhaseError("scoped repository requires ApplicationScopeConstraint")
    if not isinstance(authority_factory, AuthorityFactory):
        raise AuthorityPhaseError("scoped repository requires the active AuthorityFactory")
    binding = ScopedRepositoryBinding(session, constraint, authority_factory, authority)
    binding.require(constraint)
    return binding


@contextmanager
def repository_session(
    factory: sessionmaker[Session],
    bound: Session | None,
) -> Iterator[Session]:
    if bound is not None:
        yield bound
        return
    with factory() as session:
        yield session


def finish_repository_write(session: Session, bound: Session | None) -> None:
    if bound is None:
        session.commit()
    else:
        session.flush()


def rollback_repository_write(session: Session, bound: Session | None) -> None:
    if bound is None:
        session.rollback()
