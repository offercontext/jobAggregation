from __future__ import annotations

import inspect
import pickle
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


def _authority_context(
    tmp_path: Any,
    *,
    context_type: str = "application",
) -> tuple[AuthorityFactory, ToolExecutionContext, Any]:
    session_factory = init_database(tmp_path / "context.db")
    factory = AuthorityFactory()
    scope = TrustedContextScope(
        cast(Any, context_type),
        7 if context_type == "application" else None,
        "general",
    )
    authority = factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id="segment-context",
        trusted_scope=scope,
        capabilities=frozenset({"applications.read"}),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(session_factory),
        events=ApplicationEventsRepository(session_factory),
        notes=NotesRepository(session_factory),
        offers=OffersRepository(session_factory),
        resumes=ResumesRepository(session_factory),
        jd_analyses=JDAnalysesRepository(session_factory),
        run_recorder=cast(Any, NullRunRecorder()),
    )
    return factory, context, session_factory


def test_context_requires_authority_and_removes_raw_authorization_inputs(tmp_path: Any) -> None:
    factory, context, session_factory = _authority_context(tmp_path)
    try:
        parameters = inspect.signature(ToolExecutionContext).parameters
        assert "authority" in parameters
        assert "capabilities" not in parameters
        assert "current_bindings" not in parameters
        assert not hasattr(context, "capabilities")
        assert not hasattr(context, "current_bindings")
        assert context.authority.capabilities == frozenset({"applications.read"})
    finally:
        factory.close()
        session_factory.kw["bind"].dispose()


def test_context_bind_uses_one_session_and_exact_registered_constraint(tmp_path: Any) -> None:
    factory, context, session_factory = _authority_context(tmp_path)
    try:
        with session_factory() as session:
            bound = context.bind(session)

            assert bound.authority is context.authority
            assert bound.scope_constraint is context.scope_constraint
            assert bound.bound_session is session
            assert bound.applications._session is session
            assert bound.events._session is session
            assert bound.notes._session is session
            assert bound.offers._session is session
            assert bound.resumes._session is session
            assert bound.jd_analyses._session is session
            for repository in (
                bound.applications,
                bound.events,
                bound.notes,
                bound.offers,
                bound.jd_analyses,
            ):
                binding = repository._scope_binding
                assert binding is not None
                assert binding.session is session
                assert binding.constraint is context.scope_constraint
                assert binding.authority is context.authority
                assert binding.authority_factory is factory
    finally:
        factory.close()
        session_factory.kw["bind"].dispose()


def test_context_rejects_session_binding_after_authority_revocation(tmp_path: Any) -> None:
    factory, context, session_factory = _authority_context(tmp_path)
    factory.revoke_authority(context.authority)
    try:
        with session_factory() as session:
            with pytest.raises(Exception, match="authority|constraint|active"):
                context.bind(session)
    finally:
        factory.close()
        session_factory.kw["bind"].dispose()


def test_tool_execution_context_is_transient_and_hides_dependencies(tmp_path: Any) -> None:
    factory, context, session_factory = _authority_context(tmp_path, context_type="workspace")
    try:
        with pytest.raises(TypeError, match="transient tool runtime value"):
            pickle.dumps(context)
        assert "ApplicationsRepository" not in repr(context)
        assert "authority_instance_token" not in repr(context)
    finally:
        factory.close()
        session_factory.kw["bind"].dispose()


def test_bind_requires_a_caller_owned_sqlalchemy_session(tmp_path: Any) -> None:
    factory, context, session_factory = _authority_context(tmp_path)
    try:
        with pytest.raises(Exception):
            context.bind(cast(Session, object()))
    finally:
        factory.close()
        session_factory.kw["bind"].dispose()
