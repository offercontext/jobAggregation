from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from golden import load_golden

from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ProviderToolContract,
    ReadyToExecute,
    ToolFailure,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_runtime.journal import project_tool_proposed
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.agent_runtime.journal import EventInput
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    presentation_binding,
    read_metadata,
    write_metadata,
)


class FailingStartedRecorder:
    def __init__(self, *, fail_started: bool = False) -> None:
        self.events: list[Any] = []
        self.fail_started = fail_started
        self.recording_status = "healthy"

    def append_event(self, event: Any) -> None:
        if self.fail_started and event.event_type == "tool.started":
            self.recording_status = "degraded"
            raise RuntimeError("journal unavailable")
        self.events.append(event)


def _authority_context(
    recorder: FailingStartedRecorder,
    capabilities: frozenset[ToolCapability],
) -> tuple[ToolExecutionContext, AuthorityFactory, Any]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    factory = AuthorityFactory()
    authority = factory.create_segment_authority(
        conversation_id=1,
        conversation_scope_revision=0,
        segment_id=f"journal-{id(recorder)}",
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset(capabilities),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(sessions),
        events=ApplicationEventsRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        notes=NotesRepository(sessions),
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        run_recorder=cast(Any, recorder),
    )
    runner, surface, binding, gateway = object(), object(), object(), object()
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="journal-model",
    )
    fingerprint = "sha256:" + "f" * 64
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=fingerprint,
        candidate_count=8,
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
    return context, factory, invocation


def _digest(raw: str) -> str:
    try:
        value = json.loads(raw)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except Exception:
        encoded = raw.encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bundle(catalog: ToolCatalog) -> ToolMetadataBundleV1:
    source = compose_synthetic_bundle()
    manifest = dict(cast(dict[str, object], source["manifest"]))
    manifest["typed_tools"] = tuple(spec.name for spec in catalog.specs)
    return ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], source["legacy_boundary"]),
        compensation=cast(dict[str, object], source["compensation"]),
    )


def _lease(catalog: ToolCatalog, context: ToolExecutionContext) -> Any:
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    context.authority_factory.bind_segment_tool_catalog(
        context.authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    return lease


def _prepare(
    catalog: ToolCatalog,
    context: ToolExecutionContext,
    invocation: Any,
    call: ToolCall,
    **kwargs: Any,
) -> Any:
    factory = context.authority_factory
    attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
    identity = factory.create_new_turn_prepare_identity(
        invocation,
        attempt_id=attempt,
        candidate_ordinal=0,
        tool_call_id=call.id,
        tool_name=call.name,
        arguments_digest=_digest(call.args),
    )
    return prepare_call(
        _lease(catalog, context),
        context,
        call,
        call_identity=identity,
        **kwargs,
    )


def _execute_read(prepared: Any, context: ToolExecutionContext, invocation: Any) -> Any:
    identity = context.authority_factory.create_read_execution_identity(
        invocation,
        prepared=prepared,
        tool_call_id=prepared.tool_call_id,
        tool_name=prepared.spec.name,
        arguments_digest=prepared.arguments_digest,
    )
    return execute_prepared(prepared, context, call_identity=identity)


def _decode_mapping(values: Any) -> dict[str, Any]:
    return dict(values)


def _render_empty_items(result: Any) -> str:
    del result
    return '{"items":[]}'


def _runtime(
    recorder: FailingStartedRecorder, executor: Any
) -> tuple[ToolCatalog, ToolExecutionContext, ToolSpec[Any, Any], AuthorityFactory, Any]:
    parameters = {"properties": {"id": {"type": "integer"}}, "type": "object"}
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "description": "read",
                    "name": "list_applications",
                    "parameters": parameters,
                },
            },
            name="list_applications",
            description="read",
            parameters=parameters,
        ),
        metadata=replace(
            read_metadata(),
            required_capabilities=(ToolCapability.APPLICATIONS_READ,),
        ),
        resolver_bindings=(),
        undo_builder_binding=None,
        decoder=_decode_mapping,
        executor=executor,
        presentation=presentation_binding(),
        success_renderer=_render_empty_items,
    )
    context, factory, invocation = _authority_context(
        recorder,
        frozenset({ToolCapability.APPLICATIONS_READ}),
    )
    return ToolCatalog([spec], expected_names=(spec.name,)), context, spec, factory, invocation


def test_pipeline_journal_sequence_matches_first_phase_golden() -> None:
    recorder = FailingStartedRecorder()
    catalog, context, spec, factory, invocation = _runtime(
        recorder, lambda args, runtime: {"items": []}
    )
    prepared = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(id="read-1", name=spec.name, args="{}"),
    )
    assert isinstance(prepared, ReadyToExecute)

    _execute_read(prepared.prepared, context, invocation)

    expected = load_golden("journal_sequences_30c944f.json")["cases"]["read_success"]
    assert [event.event_type for event in recorder.events] == [
        item["event_type"] for item in expected
    ]
    started = recorder.events[1]
    assert started.facts["result_contract"] == "legacy_string_v1"
    factory.close()


def test_proposal_projection_uses_authority_entry_when_raw_spec_metadata_drifts() -> None:
    recorder = FailingStartedRecorder()
    catalog, _context, spec, factory, _invocation = _runtime(
        recorder, lambda args, runtime: {"items": []}
    )
    authority_entry = _bundle(catalog).authority_view().entries[spec.name]

    object.__setattr__(spec.metadata, "operation", write_metadata().operation)
    object.__setattr__(spec.metadata, "confirmation_policy", "required")

    assert project_tool_proposed(
        cast(Any, recorder),
        authority_entry,
        ToolCall(id="read-1", name=spec.name, args="{}"),
    )
    assert recorder.events[0].facts["tool_kind"] == "read"
    assert recorder.events[0].facts["proposal_outcome"] == "execution_allowed"
    factory.close()


def test_proposal_projection_rejects_cross_or_fake_authority_entries() -> None:
    recorder = FailingStartedRecorder()
    catalog, _context, spec, factory, _invocation = _runtime(
        recorder, lambda args, runtime: {"items": []}
    )
    authority_entry = _bundle(catalog).authority_view().entries[spec.name]
    call = ToolCall(id="read-1", name=spec.name, args="{}")

    with pytest.raises(ValueError, match="does not match"):
        project_tool_proposed(
            cast(Any, recorder),
            replace(authority_entry, provider_name="get_application"),
            call,
        )
    with pytest.raises(TypeError, match="exact ToolAuthorityEntryV1"):
        project_tool_proposed(
            cast(Any, recorder),
            cast(
                Any,
                SimpleNamespace(
                    provider_name=spec.name,
                    operation_kind=authority_entry.operation_kind,
                    confirmation_policy=authority_entry.confirmation_policy,
                ),
            ),
            call,
        )

    assert recorder.events == []
    factory.close()


def _assert_golden_case(case_name: str, events: list[EventInput]) -> None:
    expected = load_golden("journal_sequences_30c944f.json")["cases"][case_name]
    assert [
        {"event_type": event.event_type, "facts": dict(event.facts)} for event in events
    ] == expected


def _write_runtime(
    recorder: FailingStartedRecorder,
    executor: Any,
) -> tuple[ToolCatalog, ToolExecutionContext, ToolSpec[Any, Any], AuthorityFactory, Any]:
    parameters = {
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string"},
        },
        "required": ["id", "status"],
        "type": "object",
    }
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "description": "write",
                    "name": "update_application_status",
                    "parameters": parameters,
                },
            },
            name="update_application_status",
            description="write",
            parameters=parameters,
        ),
        metadata=replace(
            write_metadata(),
            required_capabilities=(ToolCapability.APPLICATIONS_WRITE,),
        ),
        resolver_bindings=(),
        undo_builder_binding=None,
        decoder=_decode_mapping,
        executor=executor,
        presentation=presentation_binding(),
    )
    context, factory, invocation = _authority_context(
        recorder,
        frozenset({ToolCapability.APPLICATIONS_WRITE}),
    )
    return ToolCatalog([spec], expected_names=(spec.name,)), context, spec, factory, invocation


def _append_approval_requested(recorder: FailingStartedRecorder) -> None:
    recorder.events.append(
        EventInput(
            event_type="approval.requested",
            facts={
                "confirmation_mode": "required",
                "pending_identity_fingerprint": "b" * 64,
                "tool_call_id": "write-1",
            },
        )
    )


def _append_approval_decided(recorder: FailingStartedRecorder, decision: str) -> None:
    recorder.events.append(
        EventInput(
            event_type="approval.decided",
            facts={
                "confirmation_attempt_id": "00000000-0000-0000-0000-000000000005",
                "decided_input_fingerprint": "c" * 64,
                "decision": decision,
                "original_input_fingerprint": "c" * 64,
                "tool_call_id": "write-1",
            },
        )
    )


def test_executor_exception_journal_sequence_matches_first_phase_golden() -> None:
    recorder = FailingStartedRecorder()

    def fail(args: Any, context: ToolExecutionContext) -> Any:
        del args, context
        raise RuntimeError("private executor detail")

    catalog, context, spec, factory, invocation = _runtime(recorder, fail)
    prepared = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(id="read-1", name=spec.name, args="{}"),
    )
    assert isinstance(prepared, ReadyToExecute)

    _execute_read(prepared.prepared, context, invocation)

    _assert_golden_case("executor_exception", recorder.events)
    factory.close()


def test_write_waiting_and_rejection_sequences_match_first_phase_golden() -> None:
    recorder = FailingStartedRecorder()
    catalog, context, spec, factory, invocation = _write_runtime(
        recorder, lambda args, runtime: args
    )
    prepared = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(
            id="write-1",
            name=spec.name,
            args='{"id":1,"status":"offer"}',
        ),
        pending_identity="write-1:update_application_status",
        pending_action_revision=1,
    )
    assert isinstance(prepared, ConfirmationRequired)
    _append_approval_requested(recorder)

    _assert_golden_case("write_waiting_confirmation", recorder.events)

    _append_approval_decided(recorder, "rejected")
    _assert_golden_case("confirmation_rejected", recorder.events)
    assert not any(
        event.event_type in {"tool.started", "tool.completed", "tool.failed"}
        for event in recorder.events
    )
    factory.close()


def test_pre_execution_stale_claim_sequence_matches_first_phase_golden() -> None:
    recorder = FailingStartedRecorder()
    catalog, context, spec, factory, invocation = _write_runtime(
        recorder, lambda args, runtime: args
    )
    prepared = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(
            id="write-1",
            name=spec.name,
            args='{"id":1,"status":"offer"}',
        ),
        pending_identity="write-1:update_application_status",
        pending_action_revision=1,
    )
    assert isinstance(prepared, ConfirmationRequired)
    _append_approval_requested(recorder)
    _append_approval_decided(recorder, "approved")

    approval_factory = AuthorityFactory()
    pending = SimpleNamespace(
        operation_id="operation-1",
        conversation_id=1,
        tool_call_id="write-1",
        tool_name=spec.name,
        pending_action_revision=1,
        effective_args_digest=prepared.prepared.arguments_digest,
    )
    approval_factory.register_pending(pending)
    approval_authority = approval_factory.create_approval_authority(
        operation_id=pending.operation_id,
        conversation_id=pending.conversation_id,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending,
        pending_action_revision=pending.pending_action_revision,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        effective_args_digest=pending.effective_args_digest,
        capabilities=frozenset({ToolCapability.APPLICATIONS_WRITE}),
    )
    approval_context = ToolExecutionContext(
        authority=approval_authority,
        applications=context.applications,
        events=context.events,
        jd_analyses=context.jd_analyses,
        notes=context.notes,
        offers=context.offers,
        resumes=context.resumes,
        run_recorder=cast(Any, recorder),
        operation_executor=lambda *_args: pytest.fail("stale claim reached operation executor"),
    )
    approval_factory.register_tool_execution_context(approval_context, authority=approval_authority)
    prepare_identity = approval_factory.create_approved_write_prepare_identity(
        approval_authority,
        approval_context=approval_context,
        request_identity=object(),
    )
    approved = prepare_call(
        _lease(catalog, approval_context),
        approval_context,
        ToolCall(
            id="write-1",
            name=spec.name,
            args='{"id":1,"status":"offer"}',
        ),
        call_identity=prepare_identity,
        pending_identity=pending,
        pending_action_revision=1,
        record_proposal=False,
    )
    assert isinstance(approved, ConfirmationRequired)

    record = execute_prepared(
        approved.prepared,
        approval_context,
        call_identity=prepare_identity,
        confirmation_claimer=lambda call: ToolFailure(
            "stale_state",
            "confirmation_claim_lost",
        ),
    )

    assert record.execution_started is False
    _assert_golden_case("pre_execution_stale_claim", recorder.events)
    approval_factory.close()
    factory.close()


def test_pre_execution_validation_sequence_matches_first_phase_golden() -> None:
    recorder = FailingStartedRecorder()
    parameters = {
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
        "type": "object",
    }
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "description": "read",
                    "name": "get_application",
                    "parameters": parameters,
                },
            },
            name="get_application",
            description="read",
            parameters=parameters,
        ),
        metadata=replace(
            read_metadata(),
            required_capabilities=(ToolCapability.APPLICATIONS_READ,),
        ),
        resolver_bindings=(),
        undo_builder_binding=None,
        decoder=_decode_mapping,
        executor=lambda args, runtime: args,
        presentation=presentation_binding(),
    )
    context, factory, invocation = _authority_context(
        recorder,
        frozenset({ToolCapability.APPLICATIONS_READ}),
    )
    catalog = ToolCatalog([spec], expected_names=(spec.name,))

    rejected = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(id="read-1", name=spec.name, args="not-json"),
    )

    assert not isinstance(rejected, ReadyToExecute)
    _assert_golden_case("pre_execution_validation_failure", recorder.events)
    factory.close()


def test_started_projection_failure_degrades_and_suppresses_terminal_event() -> None:
    calls = 0

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del context
        calls += 1
        return args

    recorder = FailingStartedRecorder(fail_started=True)
    catalog, context, spec, factory, invocation = _runtime(recorder, executor)
    prepared = _prepare(
        catalog,
        context,
        invocation,
        ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
    )
    assert isinstance(prepared, ReadyToExecute)

    record = _execute_read(prepared.prepared, context, invocation)

    assert calls == 1
    assert record.execution_started is True
    assert [event.event_type for event in recorder.events] == ["tool.proposed"]
    factory.close()
