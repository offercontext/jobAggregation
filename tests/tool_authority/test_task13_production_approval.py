from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import AgentTurnResult, PendingAction
from offerpilot.ai.agent_loop import AgentLoopInvocation, ApprovedWriteSeed
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    AuthorityFactory,
    AuthorityPhaseError,
)
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_authority.visibility import AuthorityApplicationVisibilityQuery
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import ConfirmationRequired
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationReplay,
    TerminalPayload,
    WriteOperationCoordinator,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
    operation_request_fingerprint,
)
from offerpilot.pilot_runtime.continuation import (
    ApprovalAuthorityResolver,
    ConfirmationApprovedWritePort,
    ConfirmationCoordinator,
    ConfirmationDependencies,
    ConfirmationReplayError,
)
from offerpilot.pilot_runtime.composition import _AgentDriver
from offerpilot.pilot_runtime.contracts import (
    ConfirmationRequest,
    PreparedStreamExecution,
    RuntimeTransportContext,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.persistence import ChatPersistenceCoordinator
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies
from offerpilot.db import init_database
from offerpilot.models import Conversation
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NoteCreate, NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


def _revision(tool_call_id: str, tool_name: str, raw_args: str) -> int:
    normalized = json.dumps(json.loads(raw_args), separators=(",", ":"))
    payload = json.dumps(
        {"args": normalized, "tool_call_id": tool_call_id, "tool_name": tool_name},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & (
        (1 << 63) - 1
    )


def _approval_harness(tmp_path) -> SimpleNamespace:
    sessions = init_database(tmp_path / "task13-production.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    applications = ApplicationsRepository(sessions)
    first = applications.create(ApplicationCreate("A", "Backend"))
    second = applications.create(ApplicationCreate("B", "Frontend"))
    notes = NotesRepository(sessions)
    note = notes.create(NoteCreate(application_id=first.id, company="A"))
    chat = ChatRepository(sessions, repository)
    conversation = chat.create_conversation("approval")
    operation_id = str(uuid4())
    proposal = {"id": note.id, "questions": "approved"}
    raw_args = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
    pending = PendingAction(
        "scoped-call", "update_note", raw_args, "update_note", operation_id
    )
    revision = _revision(pending.tool_call_id, pending.tool_name, raw_args)
    digest = "sha256:" + hashlib.sha256(raw_args.encode()).hexdigest()
    token_fingerprint = ledger_fingerprint(
        key, "write-operation-confirmation-token-v1", b"scoped-token"
    )
    proposal_fingerprint = ledger_fingerprint(
        key, "write-operation-proposal-v1", proposal
    )
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.context_type = "application"
        owner.context_ref = str(first.id)
        owner.scope_revision = 1
        owner.pending_operation_id = operation_id
        owner.pending_tool_call_id = pending.tool_call_id
        owner.pending_tool_name = pending.tool_name
        owner.pending_args = raw_args
        owner.pending_human = pending.human
        repository.create_primary(
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            adapter_kind="typed",
            proposal_fingerprint=proposal_fingerprint,
            confirmation_token_fingerprint=token_fingerprint,
            authorization_scope_fingerprint=authorization_scope_fingerprint(
                key,
                conversation_id=conversation.id,
                conversation_scope_revision=1,
                context_type="application",
                context_ref=first.id,
                mode="general",
                capability_profile_id="agent_typed_v1",
                capability_policy_version="capability-policy-v1",
                binding_policy_version="binding-policy-v1",
                capability_profile_fingerprint="sha256:" + "0" * 64,
                binding_policy_fingerprint="sha256:" + "0" * 64,
            ),
        )
        session.commit()
    request_fingerprint = operation_request_fingerprint(
        key,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=token_fingerprint,
        proposal_fingerprint=proposal_fingerprint,
    )
    return SimpleNamespace(
        sessions=sessions,
        repository=repository,
        coordinator=WriteOperationCoordinator(repository),
        applications=applications,
        notes=notes,
        chat=chat,
        conversation=conversation,
        operation_id=operation_id,
        pending=pending,
        revision=revision,
        digest=digest,
        first_id=first.id,
        second_id=second.id,
        note_id=note.id,
        request_fingerprint=request_fingerprint,
    )


def _approval_context_resolver(harness: SimpleNamespace):
    def resolve_context(**kwargs) -> ToolExecutionContext:
        factory = AuthorityFactory()
        try:
            authority = ApprovalAuthorityResolver(
                harness.repository,
                factory,
                capabilities=frozenset(ToolCapability),
            ).resolve(**kwargs)
            return ToolExecutionContext(
                authority=authority,
                applications=ApplicationsRepository(harness.sessions),
                events=ApplicationEventsRepository(harness.sessions),
                notes=NotesRepository(harness.sessions),
                offers=OffersRepository(harness.sessions),
                resumes=ResumesRepository(harness.sessions),
                jd_analyses=JDAnalysesRepository(harness.sessions),
                run_recorder=NullRunRecorder(),
            )
        except BaseException:
            factory.close()
            raise

    return resolve_context


def test_production_approve_modify_invokes_minimal_resolver_before_source(
    tmp_path, monkeypatch
) -> None:
    harness = _approval_harness(tmp_path)
    conversation = harness.chat.get_conversation(harness.conversation.id)
    assert conversation is not None
    resolver_calls: list[tuple[str, int]] = []
    source_calls = 0
    original_resolve = ApprovalAuthorityResolver.resolve

    def tracked_resolve(self, **kwargs):
        resolver_calls.append(
            (str(getattr(kwargs["operation"], "id", "")), kwargs["conversation_id"])
        )
        return original_resolve(self, **kwargs)

    def forbidden_source() -> tuple[object, ...]:
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("approval origin must not load continuation source")

    monkeypatch.setattr(ApprovalAuthorityResolver, "resolve", tracked_resolve)
    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=ChatPersistenceCoordinator(harness.chat),
            conversations=harness.chat,
            write_operations=harness.repository,
            write_coordinator=harness.coordinator,
            catalog=MODEL_TOOL_CATALOG,
            source_loader=forbidden_source,
            applications=ApplicationsRepository(harness.sessions),
            approval_context_resolver=_approval_context_resolver(harness),
        )
    )

    session = coordinator.approve_modify(
        ConfirmationRequest(
            conversation_id=conversation.id,
            operation_id=harness.operation_id,
            approved=True,
            confirmation_token="scoped-token",
        ),
        pending=harness.pending,
        conversation=conversation,
        catalog=MODEL_TOOL_CATALOG,
        source_loader=forbidden_source,
    )

    try:
        assert session.pending == harness.pending
        assert isinstance(session.approval_context, ToolExecutionContext)
        assert resolver_calls == [(harness.operation_id, conversation.id)]
        assert source_calls == 0
    finally:
        session.approval_context.authority_factory.close()


def test_real_typed_approval_uses_canonical_visibility_in_both_snapshots(
    tmp_path, monkeypatch
) -> None:
    harness = _approval_harness(tmp_path)
    operation = harness.repository.get(harness.operation_id)
    assert operation is not None
    visibility_calls: list[tuple[object, int]] = []
    original_visibility = AuthorityApplicationVisibilityQuery.execute_on_session

    def tracked_visibility(self, session, application_id):
        visibility_calls.append((session, application_id))
        return original_visibility(self, session, application_id)

    monkeypatch.setattr(
        AuthorityApplicationVisibilityQuery,
        "execute_on_session",
        tracked_visibility,
    )
    factory = AuthorityFactory()
    try:
        authority = ApprovalAuthorityResolver(
            harness.repository,
            factory,
            capabilities=frozenset(ToolCapability),
        ).resolve(
            operation=operation,
            pending=harness.pending,
            conversation_id=harness.conversation.id,
            pending_action_revision=harness.revision,
            effective_args_digest=harness.digest,
        )
        assert isinstance(authority, ApprovalExecutionAuthority)
        context = ToolExecutionContext(
            authority=authority,
            applications=ApplicationsRepository(harness.sessions),
            events=ApplicationEventsRepository(harness.sessions),
            notes=NotesRepository(harness.sessions),
            offers=OffersRepository(harness.sessions),
            resumes=ResumesRepository(harness.sessions),
            jd_analyses=JDAnalysesRepository(harness.sessions),
            run_recorder=NullRunRecorder(),
        )
        prepare_identity = factory.create_approved_write_prepare_identity(
            authority,
            approval_context=context,
            request_identity=object(),
        )
        prepared_result = prepare_call(
            MODEL_TOOL_CATALOG,
            context,
            ToolCall(
                harness.pending.tool_call_id,
                harness.pending.tool_name,
                harness.pending.args,
            ),
            call_identity=prepare_identity,
            pending_identity=harness.pending,
            pending_action_revision=harness.revision,
            record_proposal=False,
        )
        assert isinstance(prepared_result, ConfirmationRequired)

        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=prepared_result.prepared,
            context=context,
            prepare_identity=prepare_identity,
            request_fingerprint=harness.request_fingerprint,
        )

        assert isinstance(execution, OperationCommitted)
        assert record is not None
        assert [application_id for _session, application_id in visibility_calls] == [
            harness.first_id,
            harness.first_id,
        ]
        assert visibility_calls[0][0] is not visibility_calls[1][0]
        updated = NotesRepository(harness.sessions).get(harness.note_id)
        assert updated is not None
        assert updated.questions == "approved"
    finally:
        factory.close()


def test_runtime_real_typed_origin_is_provider_and_source_free(tmp_path) -> None:
    harness = _approval_harness(tmp_path)
    provider_calls = 0
    source_calls = 0
    approval_contexts: list[ToolExecutionContext] = []

    def forbidden_model(_request, _conversation):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("origin approval must not resolve a Provider")

    def forbidden_source(*_args, **_kwargs):
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("origin approval must not load continuation source")

    resolve_context = _approval_context_resolver(harness)

    def tracked_approval_context(**kwargs) -> ToolExecutionContext:
        context = resolve_context(**kwargs)
        approval_contexts.append(context)
        return context

    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=ChatPersistenceCoordinator(harness.chat),
            conversations=harness.chat,
            write_operations=harness.repository,
            write_coordinator=harness.coordinator,
            catalog=MODEL_TOOL_CATALOG,
            source_loader=forbidden_source,
            applications=harness.applications,
            approval_context_resolver=tracked_approval_context,
        )
    )

    class Conversations:
        def load(self, conversation_id: int):
            return harness.chat.get_conversation(conversation_id)

    class OriginDriver:
        def execute(self, invocation):
            seed = invocation.seed
            assert isinstance(seed, ApprovedWriteSeed)
            continuation = seed.continuation
            pending = continuation.pending
            context = invocation.tool_context
            factory = context.authority_factory
            prepare_identity = factory.create_approved_write_prepare_identity(
                context.authority,
                approval_context=context,
                request_identity=seed,
            )
            prepared = prepare_call(
                invocation.catalog,
                context,
                ToolCall(pending.tool_call_id, pending.tool_name, pending.args),
                call_identity=prepare_identity,
                pending_identity=pending,
                pending_action_revision=pending.pending_action_revision,
                record_proposal=False,
            )
            assert isinstance(prepared, ConfirmationRequired)
            record = execute_prepared(
                prepared.prepared,
                context,
                call_identity=prepare_identity,
                confirmation_claimer=lambda value: continuation.claim(pending, value),
            )
            assert record.terminal_persisted
            visible = record.persisted_visible_result
            assert isinstance(visible, str)
            origin = Message(
                role="tool", content=visible, tool_call_id=pending.tool_call_id
            )
            continuation.record_result(pending, origin, record)
            return AgentTurnResult(
                added=[origin, Message(role="assistant", content="done")],
                reply="done",
                pending=None,
                records=(record,),
                failures=(),
            )

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=ChatPersistenceCoordinator(harness.chat),
            confirmation_coordinator=coordinator,
            model_resolver=forbidden_model,
            source_loader=forbidden_source,
            agent_driver=OriginDriver(),
            catalog=MODEL_TOOL_CATALOG,
        )
    )

    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=harness.conversation.id,
            operation_id=harness.operation_id,
            approved=True,
            confirmation_token="scoped-token",
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert getattr(outcome, "message", None) == "done"
    assert provider_calls == 0
    assert source_calls == 0
    assert len(approval_contexts) == 1
    with pytest.raises(AuthorityPhaseError, match="closed"):
        _ = approval_contexts[0].scope_constraint
    operation = harness.repository.get(harness.operation_id)
    assert operation is not None
    assert operation.status == "committed"
    updated = NotesRepository(harness.sessions).get(harness.note_id)
    assert updated is not None
    assert updated.questions == "approved"


def test_production_agent_driver_retains_approval_context_seals(tmp_path) -> None:
    harness = _approval_harness(tmp_path)
    conversation = harness.chat.get_conversation(harness.conversation.id)
    assert conversation is not None
    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=ChatPersistenceCoordinator(harness.chat),
            conversations=harness.chat,
            write_operations=harness.repository,
            write_coordinator=harness.coordinator,
            catalog=MODEL_TOOL_CATALOG,
            applications=harness.applications,
            approval_context_resolver=_approval_context_resolver(harness),
        )
    )
    session = coordinator.approve_modify(
        ConfirmationRequest(
            conversation_id=conversation.id,
            operation_id=harness.operation_id,
            approved=True,
            confirmation_token="scoped-token",
        ),
        pending=harness.pending,
        conversation=conversation,
        catalog=MODEL_TOOL_CATALOG,
    )
    origin = session.approval_context
    context = origin.with_runtime_dependencies(
        run_recorder=NullRunRecorder(),
        operation_executor=session.execute_operation,
    )
    invocation = AgentLoopInvocation(
        seed=ApprovedWriteSeed(ConfirmationApprovedWritePort(session)),
        model=None,
        catalog=MODEL_TOOL_CATALOG,
        tool_context=context,
        auto_approve=False,
        max_iterations=1,
        run_recorder=NullRunRecorder(),
        event_sink=None,
        runtime_signal_sink=None,
        cancel_check=None,
    )
    received: list[ToolExecutionContext] = []

    class Runner:
        def run(self, candidate: AgentLoopInvocation) -> AgentTurnResult:
            received.append(candidate.tool_context)
            return AgentTurnResult([], "", None)

    driver = _AgentDriver()
    driver._runner = Runner()  # type: ignore[assignment]
    try:
        result = driver.execute(invocation)
        assert result.reply == ""
        assert len(received) == 1
        rebound = received[0]
        assert rebound.authority is origin.authority
        assert rebound.authority_factory is origin.authority_factory
        assert rebound.scope_constraint is origin.scope_constraint
        assert rebound.operation_executor is session.execute_operation
    finally:
        origin.authority_factory.close()


def test_unclaimed_timeout_closes_approval_authority(tmp_path) -> None:
    harness = _approval_harness(tmp_path)
    conversation = harness.chat.get_conversation(harness.conversation.id)
    assert conversation is not None
    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=ChatPersistenceCoordinator(harness.chat),
            conversations=harness.chat,
            write_operations=harness.repository,
            write_coordinator=harness.coordinator,
            catalog=MODEL_TOOL_CATALOG,
            applications=harness.applications,
            approval_context_resolver=_approval_context_resolver(harness),
        )
    )
    session = coordinator.approve_modify(
        ConfirmationRequest(
            conversation_id=conversation.id,
            operation_id=harness.operation_id,
            approved=True,
            confirmation_token="scoped-token",
        ),
        pending=harness.pending,
        conversation=conversation,
        catalog=MODEL_TOOL_CATALOG,
    )
    context = session.approval_context

    assert coordinator.timeout_convergence(session) is None
    assert session.state.active is False
    with pytest.raises(AuthorityPhaseError, match="closed"):
        _ = context.scope_constraint


@pytest.mark.parametrize("transport_mode", ("sync", "stream"))
def test_replay_exit_closes_approval_authority(
    tmp_path,
    transport_mode: str,
) -> None:
    harness = _approval_harness(tmp_path)
    approval_contexts: list[ToolExecutionContext] = []
    resolve_context = _approval_context_resolver(harness)

    def tracked_approval_context(**kwargs) -> ToolExecutionContext:
        context = resolve_context(**kwargs)
        approval_contexts.append(context)
        return context

    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=ChatPersistenceCoordinator(harness.chat),
            conversations=harness.chat,
            write_operations=harness.repository,
            write_coordinator=harness.coordinator,
            catalog=MODEL_TOOL_CATALOG,
            applications=harness.applications,
            approval_context_resolver=tracked_approval_context,
        )
    )

    class Conversations:
        def load(self, conversation_id: int):
            return harness.chat.get_conversation(conversation_id)

    replay = OperationReplay(
        operation_id=harness.operation_id,
        payload=TerminalPayload(
            status="committed",
            result_contract="tool_success_v1",
            result_json="{}",
            visible_result="saved",
            transport_json="{}",
            undo_json=None,
            failure_category=None,
            failure_code=None,
            digest="sha256:" + "0" * 64,
        ),
        delivery_status="pending",
        delivery_generation=0,
        delivery_lease_expires_at=None,
    )

    class ReplayDriver:
        def execute(self, _invocation):
            raise ConfirmationReplayError(replay)

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=ChatPersistenceCoordinator(harness.chat),
            confirmation_coordinator=coordinator,
            agent_driver=ReplayDriver(),
            catalog=MODEL_TOOL_CATALOG,
        )
    )
    request = ConfirmationRequest(
        conversation_id=harness.conversation.id,
        operation_id=harness.operation_id,
        approved=True,
        confirmation_token="scoped-token",
    )

    with pytest.raises(ConfirmationReplayError):
        if transport_mode == "sync":
            runtime.continue_confirmation(
                request,
                invocation_control=InMemoryRuntimeInvocationControl(),
            )
        else:
            prepared = runtime.prepare_stream(
                request,
                transport=RuntimeTransportContext(
                    mode="stream",
                    transport_run_id=uuid4(),
                    stream_version="pilot-sse-v1",
                ),
                invocation_control=InMemoryRuntimeInvocationControl(),
            )
            assert isinstance(prepared, PreparedStreamExecution)
            assert prepared.begin()
            prepared.opaque_state.cell.execution_owner = object()

            class Sink:
                def emit(self, _event: object) -> None:
                    return None

            class Host:
                def run(self, thunk, _control):
                    return thunk()

            runtime.execute_prepared_stream(
                prepared,
                event_sink=Sink(),
                signal_sink=None,
                execution_host=Host(),
                cancel_check=lambda: False,
            )

    assert len(approval_contexts) == 1
    with pytest.raises(AuthorityPhaseError, match="closed"):
        _ = approval_contexts[0].scope_constraint
