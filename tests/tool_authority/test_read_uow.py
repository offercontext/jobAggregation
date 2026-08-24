from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from sqlalchemy import event, update

from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import ReadyToExecute, ToolFailure
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_specs.application_events import application_event_specs
from offerpilot.ai.tool_specs.offers import offer_specs
from offerpilot.ai.types import ToolCall
from offerpilot.db import init_database
from offerpilot.models import ApplicationEvent, Offer
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from offerpilot.repositories.session_binding import ScopeAccessDenied


def _digest(raw: str) -> str:
    encoded = json.dumps(
        json.loads(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass
class Runtime:
    factory: AuthorityFactory
    context: ToolExecutionContext
    invocation: Any
    session_factory: Any

    def prepare(self, catalog: ToolCatalog, call: ToolCall) -> Any:
        attempt = self.factory.issue_provider_attempt(self.invocation, candidate_ordinal=0)
        identity = self.factory.create_new_turn_prepare_identity(
            self.invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_digest=_digest(call.args),
        )
        return prepare_call(catalog, self.context, call, call_identity=identity)

    def execute(self, prepared: Any) -> Any:
        identity = self.factory.create_read_execution_identity(
            self.invocation,
            prepared=prepared,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=prepared.arguments_digest,
        )
        return execute_prepared(prepared, self.context, call_identity=identity)


class BarrierRecorder:
    def __init__(self, writer: Any | None = None) -> None:
        self.events: list[Any] = []
        self.recording_status = "healthy"
        self.writer = writer
        self.writer_active = False

    def append_event(self, event_input: Any) -> None:
        self.events.append(event_input)
        if event_input.event_type == "tool.started" and self.writer is not None:
            self.writer_active = True
            try:
                self.writer()
            finally:
                self.writer_active = False


def _runtime(tmp_path: Any, recorder: BarrierRecorder) -> tuple[Runtime, int, int, int]:
    session_factory = init_database(tmp_path / "read-uow.db")
    applications = ApplicationsRepository(session_factory)
    app_a = applications.create(ApplicationCreate("A", "Role A"))
    app_b = applications.create(ApplicationCreate("B", "Role B"))
    offers = OffersRepository(session_factory)
    offer = offers.create(
        OfferCreate(
            company_name="A",
            position_name="Role A",
            application_id=app_a.id,
            base_monthly=100,
            months_per_year=12,
        )
    )
    factory = AuthorityFactory()
    authority = factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id="segment-read-uow",
        trusted_scope=TrustedContextScope("application", app_a.id, "general"),
        capabilities=frozenset({"offers.read"}),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=applications,
        events=ApplicationEventsRepository(session_factory),
        notes=NotesRepository(session_factory),
        offers=offers,
        resumes=ResumesRepository(session_factory),
        jd_analyses=JDAnalysesRepository(session_factory),
        run_recorder=cast(Any, recorder),
    )
    runner, surface, binding, gateway = object(), object(), object(), object()
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-read-uow",
    )
    fingerprint = "sha256:" + "d" * 64
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=fingerprint,
        candidate_count=16,
        authority=authority,
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=fingerprint,
        authority=authority,
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return Runtime(factory, context, invocation, session_factory), app_a.id, app_b.id, offer.id


def test_reparent_after_resolver_rollback_before_final_sql_denies_without_body(
    tmp_path: Any,
) -> None:
    recorder = BarrierRecorder()
    runtime, app_a_id, app_b_id, offer_id = _runtime(tmp_path, recorder)
    executor_calls = 0
    target_sql_after_started: list[str] = []
    try:
        def writer() -> None:
            with runtime.session_factory() as session:
                session.execute(
                    update(Offer).where(Offer.id == offer_id).values(application_id=app_b_id)
                )
                session.commit()

        recorder.writer = writer
        selected = next(spec for spec in offer_specs() if spec.name == "get_offer")
        original_executor = selected.executor

        def counted_executor(args: Any, context: ToolExecutionContext) -> Any:
            nonlocal executor_calls
            executor_calls += 1
            return original_executor(args, context)

        selected = replace(selected, executor=counted_executor)
        catalog = ToolCatalog([selected], expected_names=(selected.name,))
        call = ToolCall(id="get-offer-race", name="get_offer", args=f'{{"id":{offer_id}}}')
        prepared = runtime.prepare(catalog, call)
        assert isinstance(prepared, ReadyToExecute)

        def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            if (
                recorder.events
                and recorder.events[-1].event_type == "tool.started"
                and not recorder.writer_active
                and "offers" in statement.lower()
            ):
                target_sql_after_started.append(statement)

        engine = runtime.session_factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", capture)
        try:
            record = runtime.execute(prepared.prepared)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert record.execution_started is True
        assert record.outcome == ToolFailure(
            "permission_denied", "scope_access_denied", "permission denied"
        )
        assert executor_calls == 1
        assert len(target_sql_after_started) == 1
        assert [item.event_type for item in recorder.events] == [
            "tool.proposed",
            "tool.started",
            "tool.failed",
        ]
        assert recorder.events[-1].facts["failure_category"] == "tool_error"
        assert render_compatibility(selected, record.outcome) == "错误：permission denied"
        with runtime.session_factory() as session:
            assert session.get(Offer, offer_id).application_id == app_b_id
        assert app_a_id != app_b_id
    finally:
        runtime.factory.close()
        runtime.session_factory.kw["bind"].dispose()


def test_cross_application_detached_and_missing_are_publicly_equivalent(tmp_path: Any) -> None:
    recorder = BarrierRecorder()
    runtime, _app_a_id, app_b_id, _offer_id = _runtime(tmp_path, recorder)
    try:
        other = OffersRepository(runtime.session_factory).create(
            OfferCreate(
                company_name="B",
                position_name="Role B",
                application_id=app_b_id,
                base_monthly=200,
                months_per_year=12,
            )
        )
        detached = OffersRepository(runtime.session_factory).create(
            OfferCreate(
                company_name="Detached",
                position_name="Role",
                application_id=None,
                base_monthly=300,
                months_per_year=12,
            )
        )
        selected = next(spec for spec in offer_specs() if spec.name == "get_offer")
        failures: list[ToolFailure] = []
        visible: list[str] = []
        downstream_event_counts: list[int] = []
        for ordinal, identity in enumerate((other.id, detached.id, 2**31), start=1):
            before = len(recorder.events)
            invocation_spec = replace(selected)
            invocation_catalog = ToolCatalog(
                [invocation_spec], expected_names=(invocation_spec.name,)
            )
            call = ToolCall(
                id=f"denied-{ordinal}",
                name="get_offer",
                args=f'{{"id":{identity}}}',
            )
            result = runtime.prepare(invocation_catalog, call)
            assert isinstance(result, Rejected)
            failures.append(result.failure)
            visible.append(render_compatibility(selected, result.failure))
            downstream_event_counts.append(len(recorder.events) - before)

        assert failures == [
            ToolFailure("permission_denied", "scope_access_denied", "permission denied")
        ] * 3
        assert visible == ["错误：permission denied"] * 3
        assert downstream_event_counts == [1, 1, 1]
        assert all(event.event_type == "tool.proposed" for event in recorder.events)
    finally:
        runtime.factory.close()
        runtime.session_factory.kw["bind"].dispose()


def test_delete_event_executor_rechecks_reparented_target_in_final_scoped_sql(
    tmp_path: Any,
) -> None:
    recorder = BarrierRecorder()
    runtime, app_a_id, app_b_id, _offer_id = _runtime(tmp_path, recorder)
    try:
        event_row = ApplicationEventsRepository(runtime.session_factory).create(
            ApplicationEventCreate(
                application_id=app_a_id,
                event_type="interview",
                scheduled_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                duration_minutes=30,
            )
        )
        with runtime.session_factory() as writer:
            writer.execute(
                update(ApplicationEvent)
                .where(ApplicationEvent.id == event_row.id)
                .values(application_id=app_b_id)
            )
            writer.commit()
        spec = next(
            item
            for item in application_event_specs()
            if item.name == "delete_application_event"
        )
        with runtime.session_factory() as session:
            bound = runtime.context.bind(session)
            with pytest.raises(ScopeAccessDenied):
                spec.executor({"id": event_row.id}, bound)
            session.rollback()
        with runtime.session_factory() as session:
            persisted = session.get(ApplicationEvent, event_row.id)
            assert persisted is not None
            assert persisted.application_id == app_b_id
    finally:
        runtime.factory.close()
        runtime.session_factory.kw["bind"].dispose()


def test_assessment_executor_cannot_mutate_target_deleted_after_prepare(
    tmp_path: Any,
) -> None:
    recorder = BarrierRecorder()
    runtime, _app_a_id, _app_b_id, offer_id = _runtime(tmp_path, recorder)
    try:
        with runtime.session_factory() as writer:
            target = writer.get(Offer, offer_id)
            assert target is not None
            writer.delete(target)
            writer.commit()
        spec = next(
            item for item in offer_specs() if item.name == "save_offer_assessment"
        )
        with runtime.session_factory() as session:
            bound = runtime.context.bind(session)
            with pytest.raises(ScopeAccessDenied):
                spec.executor({"id": offer_id, "assessment": "must not write"}, bound)
            session.rollback()
        with runtime.session_factory() as session:
            assert session.get(Offer, offer_id) is None
    finally:
        runtime.factory.close()
        runtime.session_factory.kw["bind"].dispose()
