from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeVar

from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.contracts import (
        ApplicationScopeConstraint,
        ToolExecutionAuthority,
    )

    class AuthorityFactoryProtocol(Protocol):
        def issue_repository_binding_ticket(
            self,
            *,
            repository: object,
            session: object,
            constraint: ApplicationScopeConstraint,
            authority: ToolExecutionAuthority,
        ) -> object: ...

        def register_repository_binding(
            self,
            binding: object,
            *,
            ticket: object,
            repository: object,
            session: object,
            constraint: ApplicationScopeConstraint,
            authority: ToolExecutionAuthority,
        ) -> object: ...

        def revoke_repository_binding_ticket(self, ticket: object) -> None: ...

        def require_scope_constraint(
            self,
            constraint: object,
            authority: ToolExecutionAuthority,
        ) -> None: ...


_SCOPED_BINDING_SEAL = object()
_MAX_INT64 = 2**63 - 1
RepositoryT = TypeVar("RepositoryT")


def scoped_authority_phase_error(message: str) -> Exception:
    """Load the leaf error only after repository modules have cold-started."""

    from offerpilot.ai.tool_authority.contracts import AuthorityPhaseError

    return AuthorityPhaseError(message)


class ScopeAccessDenied(LookupError):
    """A scoped query cannot prove that its target is visible to the scope."""


@dataclass(frozen=True, slots=True, init=False)
class ScopedRepositoryBinding:
    """Caller-owned Session plus the exact factory-bound scope capability.

    Repository methods intentionally retain the original constraint object
    rather than copying its fields.  The factory validation is repeated before
    every scoped statement so a revoked or mutated transient value cannot
    reach SQL.
    """

    session: Session
    constraint: ApplicationScopeConstraint
    authority_factory: AuthorityFactoryProtocol
    authority: ToolExecutionAuthority
    _seal: object = field(init=False, repr=False, compare=False)
    _ticket: object = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        session: Session,
        constraint: ApplicationScopeConstraint,
        authority_factory: AuthorityFactoryProtocol,
        authority: ToolExecutionAuthority,
        *,
        _seal: object | None = None,
        _ticket: object | None = None,
    ) -> None:
        if _seal is not _SCOPED_BINDING_SEAL or _ticket is None:
            raise scoped_authority_phase_error("scoped repository binding is factory-sealed")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "constraint", constraint)
        object.__setattr__(self, "authority_factory", authority_factory)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "_seal", _SCOPED_BINDING_SEAL)
        object.__setattr__(self, "_ticket", _ticket)

    def _assert_sealed(self) -> None:
        if type(self) is not ScopedRepositoryBinding or self._seal is not _SCOPED_BINDING_SEAL:
            raise scoped_authority_phase_error("scoped repository binding seal mismatch")

    def require(self, constraint: object, *, repository: object) -> None:
        self._assert_sealed()
        from offerpilot.ai.tool_authority.composition import require_repository_binding

        require_repository_binding(
            self,
            repository=repository,
            session=self.session,
            constraint=constraint,
        )


def bind_scoped_repository(
    session: Session,
    constraint: ApplicationScopeConstraint,
    *,
    authority_factory: AuthorityFactoryProtocol,
    authority: ToolExecutionAuthority,
    repository: object,
) -> ScopedRepositoryBinding:
    """Create a sealed repository binding for one caller-owned Session."""

    # These imports are deliberately delayed: importing a repository must not
    # initialize the composition/runtime package, which imports repositories.
    from offerpilot.ai.tool_authority.composition import AuthorityFactory as RuntimeAuthorityFactory
    from offerpilot.ai.tool_authority.contracts import ApplicationScopeConstraint

    if type(session) is not Session:
        # SQLAlchemy may provide Session subclasses in tests/app integrations;
        # the runtime check below is deliberately structural for those.
        if not isinstance(session, Session):
            raise scoped_authority_phase_error("scoped repository requires a caller-owned Session")
    if type(constraint) is not ApplicationScopeConstraint:
        raise scoped_authority_phase_error("scoped repository requires ApplicationScopeConstraint")
    if type(authority_factory) is not RuntimeAuthorityFactory:
        raise scoped_authority_phase_error("scoped repository requires the active AuthorityFactory")
    ticket = authority_factory.issue_repository_binding_ticket(
        repository=repository,
        session=session,
        constraint=constraint,
        authority=authority,
    )
    binding = ScopedRepositoryBinding(
        session,
        constraint,
        authority_factory,
        authority,
        _seal=_SCOPED_BINDING_SEAL,
        _ticket=ticket,
    )
    try:
        authority_factory.register_repository_binding(
            binding,
            ticket=ticket,
            repository=repository,
            session=session,
            constraint=constraint,
            authority=authority,
        )
    except Exception:
        try:
            authority_factory.revoke_repository_binding_ticket(ticket)
        except Exception:
            pass
        raise
    binding.require(constraint, repository=repository)
    return binding


def attach_scoped_repository(
    repository: RepositoryT,
    binding: ScopedRepositoryBinding,
) -> RepositoryT:
    """Attach only a factory-sealed binding to a repository clone."""

    binding._assert_sealed()
    setattr(repository, "_scope_binding", binding)
    return repository


def require_scoped_positive_int64(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_INT64:
        raise scoped_authority_phase_error(f"{label} must be a positive int64")
    return value


def require_scoped_optional_id(value: object, label: str) -> None:
    if value is None:
        return
    require_scoped_positive_int64(value, label)


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
