from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ReadyToExecute,
    ToolExecutionRecord,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.types import ToolCall
from offerpilot.db import init_database
from offerpilot.models import (
    Application,
    ApplicationEvent,
    InterviewNote,
    JDAnalysis,
    Offer,
    Resume,
)
from offerpilot.repositories.application_events import (
    ApplicationEventCreate,
    ApplicationEventsRepository,
)
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository, JDAnalysisCreate
from offerpilot.repositories.notes import NoteCreate, NotesRepository
from offerpilot.repositories.offers import OfferCreate, OffersRepository
from offerpilot.repositories.resumes import ResumeCreate, ResumeMatchCreate, ResumesRepository
from tests.tool_metadata.factories import compose_synthetic_bundle


DYNAMIC_TIME_FIELD = re.compile(
    r'("(?:created_at|updated_at|applied_at|closed_at|deleted_at|first_pending_at|'
    r'first_applied_at|first_written_test_at|first_interview_at|first_offer_at)":\s*)"[^"]*"'
)
MODEL_BY_TABLE = {
    "applications": Application,
    "application_events": ApplicationEvent,
    "interview_notes": InterviewNote,
    "offers": Offer,
    "resumes": Resume,
    "jd_analyses": JDAnalysis,
}


@dataclass
class Harness:
    context: ToolExecutionContext
    session_factory: sessionmaker[Session]
    authority_factory: AuthorityFactory
    invocation_identity: object

    def close(self) -> None:
        self.authority_factory.close()
        self.session_factory.kw["bind"].dispose()

    def prepare_identity(self, call: ToolCall) -> object:
        attempt = self.authority_factory.issue_provider_attempt(
            cast(Any, self.invocation_identity), candidate_ordinal=0
        )
        return self.authority_factory.create_new_turn_prepare_identity(
            cast(Any, self.invocation_identity),
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_digest=_arguments_digest(call.args),
        )

    def read_identity(self, prepared: Any) -> object:
        return self.authority_factory.create_read_execution_identity(
            cast(Any, self.invocation_identity),
            prepared=prepared,
            tool_call_id=prepared.tool_call_id,
            tool_name=prepared.spec.name,
            arguments_digest=prepared.arguments_digest,
        )


def normalize_visible(value: str) -> str:
    return DYNAMIC_TIME_FIELD.sub(r'\1"<timestamp>"', value)


def build_harness(path: Path, tool_name: str, case: str) -> Harness:
    mode = "full"
    closed = False
    deleted_resume = False
    if tool_name == "create_application" and case in {
        "success",
        "invalid_application_status",
        "closed_application_without_reason",
    }:
        mode = "empty"
    elif tool_name == "create_application_event" and case == "success":
        mode = "without_event"
    elif tool_name == "add_note" and case == "success":
        mode = "without_note"
    if case == "closed_application_cannot_reopen":
        closed = True
    if case == "deleted_resume":
        deleted_resume = True

    session_factory = init_database(path)
    applications = ApplicationsRepository(session_factory)
    events = ApplicationEventsRepository(session_factory)
    notes = NotesRepository(session_factory)
    offers = OffersRepository(session_factory)
    resumes = ResumesRepository(session_factory)
    jd_analyses = JDAnalysesRepository(session_factory)
    if mode != "empty":
        app = applications.create(
            ApplicationCreate(
                company_name="Alpha",
                position_name="Platform Engineer",
                status="closed" if closed else "applied",
                closed_reason="position filled" if closed else "",
            )
        )
        if mode != "without_event":
            events.create(
                ApplicationEventCreate(
                    application_id=app.id,
                    event_type="interview",
                    scheduled_at=datetime(2026, 8, 19, 9, tzinfo=timezone.utc),
                    duration_minutes=30,
                )
            )
        if mode != "without_note":
            notes.create(
                NoteCreate(
                    application_id=app.id,
                    company=app.company_name,
                    position=app.position_name,
                    date="2026-08-18",
                    questions="Q0",
                )
            )
        offers.create(
            OfferCreate(
                application_id=app.id,
                company_name=app.company_name,
                position_name=app.position_name,
                base_monthly=25000,
                months_per_year=14,
            )
        )
        resume = resumes.create(
            ResumeCreate(
                title="Synthetic Resume",
                source="manual",
                content_json={
                    "career_intent": {"target_roles": []},
                    "experience": [{"company": "Synthetic Co", "highlights": ["Built APIs"]}],
                },
            )
        )
        resumes.create_match(
            ResumeMatchCreate(
                resume_id=resume.id,
                application_id=app.id,
                jd_text="Synthetic backend role",
                result='{"match_score":88}',
            )
        )
        jd_analyses.create(
            JDAnalysisCreate(
                application_id=app.id,
                jd_source="manual",
                jd_text="Synthetic backend engineer",
                result='{"summary":"Synthetic role"}',
            )
        )
        if deleted_resume:
            resumes.delete(resume.id)

    authority_factory = AuthorityFactory()
    authority = authority_factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id="domain-harness",
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset(ToolCapability),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=applications,
        events=events,
        jd_analyses=jd_analyses,
        notes=notes,
        offers=offers,
        resumes=resumes,
        run_recorder=cast(Any, NullRunRecorder()),
    )
    runner, surface, binding, gateway = object(), object(), object(), object()
    authority_factory.register_runner_invocation(runner, authority=authority)
    authority_factory.register_tool_execution_context(context, authority=authority)
    build_identity = authority_factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="domain-model-call",
    )
    surface_fingerprint = "sha256:" + "e" * 64
    authority_factory.register_frozen_surface(
        surface,
        surface_fingerprint=surface_fingerprint,
        candidate_count=64,
        authority=authority,
        build_identity=build_identity,
    )
    authority_factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        authority=authority,
        build_identity=build_identity,
    )
    authority_factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build_identity,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
    )
    invocation_identity = authority_factory.create_provider_invocation_identity(
        build_identity,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    return Harness(
        context=context,
        session_factory=session_factory,
        authority_factory=authority_factory,
        invocation_identity=invocation_identity,
    )


def execute_case(
    specs: tuple[ToolSpec[Any, Any], ...],
    case: dict[str, Any],
    path: Path,
) -> tuple[str, dict[str, Any], int]:
    harness = build_harness(path, case["tool_name"], case["case"])
    try:
        executor_calls = 0
        stages: list[str] = []
        selected = next(spec for spec in specs if spec.name == case["tool_name"])

        def counted_executor(args: Any, context: ToolExecutionContext) -> Any:
            nonlocal executor_calls
            executor_calls += 1
            return selected.executor(args, context)

        instrumented = tuple(
            replace(spec, executor=counted_executor) if spec.name == selected.name else spec
            for spec in specs
        )
        catalog = ToolCatalog(
            instrumented,
            expected_names=tuple(spec.name for spec in instrumented),
        )
        source = compose_synthetic_bundle()
        manifest = dict(cast(dict[str, object], source["manifest"]))
        manifest["typed_tools"] = tuple(spec.name for spec in instrumented)
        bundle = ToolMetadataBundleV1(
            typed_catalog=catalog,
            manifest=manifest,
            legacy_boundary=cast(dict[str, object], source["legacy_boundary"]),
            compensation=cast(dict[str, object], source["compensation"]),
        )
        catalog_lease = bundle.open_segment_lease()
        harness.authority_factory.bind_segment_tool_catalog(
            harness.context.authority,
            authority_metadata_view=bundle.authority_view(),
            catalog_lease=catalog_lease,
        )
        spec = next(spec for spec in instrumented if spec.name == case["tool_name"])
        tool_call = ToolCall(
            id="golden-call",
            name=case["tool_name"],
            args=_canonical_arguments(case["arguments"]),
        )
        prepared = prepare_call(
            catalog_lease,
            harness.context,
            tool_call,
            call_identity=harness.prepare_identity(tool_call),
            pending_action_revision=1,
            pending_identity="golden-pending",
            stage_sink=stages.append,
        )
        if isinstance(prepared, Rejected):
            visible = render_compatibility(spec, prepared.failure)
            compatibility_handler_calls = 0 if stages[-1:] == ["preflight"] else 1
        else:
            assert isinstance(prepared, (ReadyToExecute, ConfirmationRequired))
            if isinstance(prepared, ReadyToExecute):
                record = execute_prepared(
                    prepared.prepared,
                    harness.context,
                    call_identity=harness.read_identity(prepared.prepared),
                )
            else:
                approval_factory = AuthorityFactory()
                pending = SimpleNamespace(
                    operation_id="golden-operation",
                    conversation_id=1,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    pending_action_revision=1,
                    effective_args_digest=prepared.prepared.arguments_digest,
                )
                approval_factory.register_pending(pending)
                authority = approval_factory.create_approval_authority(
                    operation_id=pending.operation_id,
                    conversation_id=pending.conversation_id,
                    conversation_scope_revision=0,
                    trusted_scope=TrustedContextScope("workspace", None, "general"),
                    pending_identity=pending,
                    pending_action_revision=pending.pending_action_revision,
                    tool_call_id=pending.tool_call_id,
                    tool_name=pending.tool_name,
                    effective_args_digest=pending.effective_args_digest,
                    capabilities=frozenset(ToolCapability),
                )

                def execute_operation(
                    approved: Any,
                    approved_context: ToolExecutionContext,
                    _prepare_identity: object,
                ) -> ToolExecutionRecord[Any, Any]:
                    with harness.session_factory() as session:
                        bound_context = approved_context.bind(session)
                        try:
                            outcome: ToolSuccess[Any] | ToolFailure = ToolSuccess(
                                approved.spec.executor(approved.typed_args, bound_context)
                            )
                        except Exception as exc:
                            mapping = next(
                                (
                                    item
                                    for item in approved.spec.exception_map
                                    if isinstance(exc, item.exception_type)
                                ),
                                None,
                            )
                            outcome = (
                                ToolFailure("internal_error", "executor_exception")
                                if mapping is None
                                else ToolFailure(
                                    mapping.category,
                                    mapping.code,
                                    (
                                        mapping.compatibility_detail(exc)
                                        if mapping.compatibility_detail is not None
                                        else ""
                                    ),
                                )
                            )
                        session.commit()
                    visible_result = render_compatibility(approved.spec, outcome)
                    return ToolExecutionRecord(
                        prepared=approved,
                        outcome=outcome,
                        execution_started=True,
                        operation_id=pending.operation_id,
                        terminal_persisted=True,
                        persisted_visible_result=visible_result,
                        persisted_transport={"status": "success"},
                    )

                approval_context = ToolExecutionContext(
                    authority=authority,
                    applications=harness.context.applications,
                    events=harness.context.events,
                    notes=harness.context.notes,
                    offers=harness.context.offers,
                    resumes=harness.context.resumes,
                    jd_analyses=harness.context.jd_analyses,
                    run_recorder=cast(Any, NullRunRecorder()),
                    operation_executor=execute_operation,
                )
                approval_factory.register_tool_execution_context(
                    approval_context, authority=authority
                )
                approval_lease = bundle.open_segment_lease()
                approval_factory.bind_segment_tool_catalog(
                    authority,
                    authority_metadata_view=bundle.authority_view(),
                    catalog_lease=approval_lease,
                )
                prepare_identity = approval_factory.create_approved_write_prepare_identity(
                    authority,
                    approval_context=approval_context,
                    request_identity=object(),
                )
                approved = prepare_call(
                    approval_lease,
                    approval_context,
                    tool_call,
                    call_identity=prepare_identity,
                    pending_identity=pending,
                    pending_action_revision=1,
                    record_proposal=False,
                )
                assert isinstance(approved, ConfirmationRequired)
                try:
                    record = execute_prepared(
                        approved.prepared,
                        approval_context,
                        call_identity=prepare_identity,
                        confirmation_claimer=lambda _call: None,
                    )
                finally:
                    approval_factory.close()
            visible = render_compatibility(spec, record.outcome)
            compatibility_handler_calls = executor_calls
        projection: dict[str, Any] = {}
        expected_projection = case["business_projection"]
        if expected_projection:
            table = expected_projection["table"]
            model = MODEL_BY_TABLE[table]
            with harness.session_factory() as session:
                projection = {
                    "table": table,
                    "row_count": int(session.scalar(select(func.count()).select_from(model)) or 0),
                }
        return normalize_visible(visible), projection, compatibility_handler_calls
    finally:
        harness.close()


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _arguments_digest(raw: str) -> str:
    try:
        value = json.loads(raw)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        encoded = raw.encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
