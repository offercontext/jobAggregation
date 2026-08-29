from __future__ import annotations

import hashlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

import test_write_operations as support
import tests.test_write_operation_acceptance_matrix as legacy_support

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityFactory, AuthorityUse, TrustedContextScope
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_runtime.policy_types import ToolCapability
from offerpilot.ai.tool_runtime.contracts import ConfirmationRequired
from offerpilot.ai.tool_runtime.pipeline import Rejected, prepare_call
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyApprovedConfirmationInput,
    LegacyConfirmationLookupIdentity,
)
from offerpilot.ai.tool_specs import legacy as legacy_specs
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.ai.types import ToolCall
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationUnknown,
    WriteOperationCoordinator,
    WriteOperationRepository,
    ledger_fingerprint,
    load_or_create_ledger_key,
    operation_request_fingerprint,
)
from offerpilot.db import init_database
from offerpilot.models import Conversation, InterviewNote, WriteOperation
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NoteCreate, NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from tests.tool_metadata.factories import compose_synthetic_bundle
from tests.tool_authority.test_pending_claim import create_primary_with_typed_route


_TEST_TOOL_CATALOG = build_model_tool_catalog()


_PREPARED_LEGACY_EXECUTION_ARGS: list[str] = []


def _capture_prepared_legacy_execute(encoded_args: str, _context: object) -> str:
    _PREPARED_LEGACY_EXECUTION_ARGS.append(encoded_args)
    return '{"ok":true}'


class _PreparedAcceptanceLegacyBoundRoute(legacy_support._AcceptanceLegacyBoundRoute):
    def prepared_call(self):
        self.identity_events.append(("projection", self.prepared))
        return self.prepared

    def execute(self, prepared=None):
        execution_input = self.prepared if prepared is None else prepared
        self.identity_events.append(("execution", execution_input))
        if execution_input is not self.prepared:
            raise ValueError("Legacy prepared projection and execution identity mismatch")
        return super().execute()


def _scoped_approval_harness(
    tmp_path,
    *,
    tool_name: str,
    proposal: dict[str, object],
    effective: dict[str, object] | None = None,
):
    sessions = init_database(tmp_path / f"{tool_name}.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    repository = WriteOperationRepository(sessions, key)
    applications = ApplicationsRepository(sessions)
    first = applications.create(ApplicationCreate("A", "Backend"))
    second = applications.create(ApplicationCreate("B", "Frontend"))
    notes = NotesRepository(sessions)
    bound_note = notes.create(NoteCreate(application_id=first.id, company="A"))
    conversation = ChatRepository(sessions, repository).create_conversation("approval")
    operation_id = str(uuid4())
    raw_proposal = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
    decided = proposal if effective is None else effective
    raw_effective = json.dumps(decided, sort_keys=True, separators=(",", ":"))
    pending = PendingAction("scoped-call", tool_name, raw_proposal, tool_name, operation_id)
    revision = support._pending_revision(pending.tool_call_id, tool_name, raw_effective)
    digest = "sha256:" + hashlib.sha256(raw_effective.encode()).hexdigest()
    token_fingerprint = ledger_fingerprint(
        key, "write-operation-confirmation-token-v1", b"scoped-token"
    )
    proposal_fingerprint = ledger_fingerprint(key, "write-operation-proposal-v1", proposal)
    with sessions() as session:
        owner = session.get(Conversation, conversation.id)
        assert owner is not None
        owner.context_type = "application"
        owner.context_ref = str(first.id)
        owner.scope_revision = 1
        owner.pending_operation_id = operation_id
        owner.pending_tool_call_id = pending.tool_call_id
        owner.pending_tool_name = tool_name
        owner.pending_args = raw_proposal
        owner.pending_human = tool_name
        create_primary_with_typed_route(
            repository,
            session,
            operation_id=operation_id,
            conversation_id=conversation.id,
            tool_call_id=pending.tool_call_id,
            tool_name=tool_name,
            raw_args=raw_proposal,
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
    factory = AuthorityFactory()
    pending_identity = SimpleNamespace(
        conversation_id=conversation.id,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=tool_name,
        pending_action_revision=revision,
        arguments_digest=digest,
        effective_args_digest=digest,
    )
    factory.register_pending(pending_identity)
    authority = factory.create_approval_authority(
        operation_id=operation_id,
        conversation_id=conversation.id,
        conversation_scope_revision=1,
        trusted_scope=TrustedContextScope("application", first.id, "general"),
        pending_identity=pending_identity,
        pending_action_revision=revision,
        tool_call_id=pending.tool_call_id,
        tool_name=tool_name,
        effective_args_digest=digest,
        capabilities=frozenset(ToolCapability),
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=applications,
        events=ApplicationEventsRepository(sessions),
        notes=notes,
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    original_spec = _TEST_TOOL_CATALOG.resolve(tool_name)
    assert original_spec is not None
    executor_calls: list[object] = []

    def executor(args, _context):
        executor_calls.append(dict(args))
        return {"ok": True}

    spec = replace(
        original_spec,
        metadata=replace(original_spec.metadata, dependencies=()),
        executor=executor,
    )
    catalog = ToolCatalog((spec,), expected_names=(tool_name,))
    source = compose_synthetic_bundle()
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest={**source["manifest"], "typed_tools": (tool_name,)},
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    factory.bind_segment_tool_catalog(
        authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    prepare_identity = factory.create_approved_write_prepare_identity(
        authority,
        approval_context=context,
        request_identity=object(),
    )
    prepared_result = prepare_call(
        lease,
        context,
        ToolCall(pending.tool_call_id, tool_name, raw_effective),
        call_identity=prepare_identity,
        pending_identity=pending_identity,
        pending_action_revision=revision,
        record_proposal=False,
    )
    request_fingerprint = operation_request_fingerprint(
        key,
        operation_id=operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=effective is not None,
        edited_args=effective,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=token_fingerprint,
        proposal_fingerprint=proposal_fingerprint,
    )
    return SimpleNamespace(
        sessions=sessions,
        repository=repository,
        coordinator=WriteOperationCoordinator(repository),
        factory=factory,
        lease=lease,
        conversation=conversation,
        operation_id=operation_id,
        first_id=first.id,
        second_id=second.id,
        note_id=bound_note.id,
        context=context,
        prepare_identity=prepare_identity,
        prepared_result=prepared_result,
        request_fingerprint=request_fingerprint,
        executor_calls=executor_calls,
    )


def _execute_scoped(harness):
    assert isinstance(harness.prepared_result, ConfirmationRequired)
    return harness.coordinator.execute_primary(
        operation_id=harness.operation_id,
        conversation_id=harness.conversation.id,
        prepared=harness.prepared_result.prepared,
        context=harness.context,
        prepare_identity=harness.prepare_identity,
        request_fingerprint=harness.request_fingerprint,
        parent_route_binder=None,
    )


def test_execute_legacy_has_no_caller_supplied_input_fingerprint() -> None:
    parameters = inspect.signature(WriteOperationCoordinator.execute_legacy).parameters

    assert "input_fingerprint" not in parameters
    assert {
        "operation_id",
        "conversation_id",
        "tool_call_id",
        "tool_name",
        "request_fingerprint",
        "route_binder",
    } <= parameters.keys()


def test_execute_legacy_fingerprints_the_same_exact_edited_prepared_call_it_executes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _PREPARED_LEGACY_EXECUTION_ARGS.clear()
    monkeypatch.setattr(
        legacy_specs,
        "_execute_static_jd",
        _capture_prepared_legacy_execute,
    )
    sessions, repository, chat, coordinator, components = legacy_support._legacy_harness(tmp_path)
    conversation, pending = legacy_support._propose(
        chat,
        "save_application_jd_version",
        "jd_deterministic_action",
    )
    operation = repository.get(pending.operation_id)
    assert operation is not None
    edited_args = {"jd_text": "proof-prepared edited JD"}
    expected_canonical_args = {**json.loads(pending.args), **edited_args}
    expected_encoded_args = json.dumps(
        expected_canonical_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    confirmation_token = legacy_support._legacy_confirmation_token(pending)
    identity_events: list[tuple[str, object]] = []

    @contextmanager
    def bind(write_session):
        routes = components.confirmation_routes
        with sessions() as read_session:
            with read_session.begin():
                prepared = routes.proof_issuer.prepare_server_loaded(
                    read_session,
                    LegacyConfirmationLookupIdentity(conversation_id=conversation.id),
                    LegacyApprovedConfirmationInput(
                        decision="approved",
                        operation_id=pending.operation_id,
                        confirmation_token=confirmation_token,
                        edited_args_present=True,
                        edited_args=edited_args,
                        rejection_feedback_present=False,
                        rejection_feedback="",
                    ),
                )
        lease = routes.pending_identity_verifier_port.open_issuance_lease(
            write_session,
            prepared,
        )
        route = _PreparedAcceptanceLegacyBoundRoute(
            components=components,
            sessions=sessions,
            write_session=write_session,
            lease=lease,
            prepared=prepared,
            pending=pending,
            parent_route_probe=None,
        )
        route.identity_events = identity_events
        try:
            yield route
        finally:
            lease.close()

    request_fingerprint = operation_request_fingerprint(
        repository.key,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        approved=True,
        edited_args_present=True,
        edited_args=edited_args,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=ledger_fingerprint(
            repository.key,
            "write-operation-confirmation-token-v1",
            confirmation_token.encode("ascii"),
        ),
        proposal_fingerprint=operation.proposal_fingerprint,
    )

    execution = coordinator.execute_legacy(
        operation_id=pending.operation_id,
        conversation_id=conversation.id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        request_fingerprint=request_fingerprint,
        route_binder=bind,
    )

    assert isinstance(execution, OperationCommitted), execution
    assert _PREPARED_LEGACY_EXECUTION_ARGS == [expected_encoded_args]
    assert [event for event, _prepared in identity_events] == ["projection", "execution"]
    assert identity_events[0][1] is identity_events[1][1]
    persisted = repository.get(pending.operation_id)
    assert persisted is not None
    assert persisted.input_fingerprint == ledger_fingerprint(
        repository.key,
        "write-operation-legacy-input-v1",
        expected_canonical_args,
    )
    assert execution.ownership is not None
    execution.ownership.revoke_parent_route()


def test_locked_scope_revision_change_rolls_back_before_executor(tmp_path) -> None:
    calls = 0
    decisions: list[str] = []

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = support._primary_execution_harness(tmp_path, executor)
    with harness.sessions() as session:
        conversation = session.get(Conversation, harness.conversation.id)
        assert conversation is not None
        conversation.context_type = "global"
        conversation.scope_revision = 1
        session.commit()
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
            approval_decided_callback=lambda _session: decisions.append("approval.decided"),
        )
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "authorization_scope_changed"
        assert record is None
        assert calls == 0
        assert decisions == []
        with harness.sessions() as session:
            operation = session.get(WriteOperation, harness.operation_id)
            conversation = session.get(Conversation, harness.conversation.id)
            assert operation is not None and operation.status == "proposed"
            assert conversation is not None
            assert conversation.pending_operation_id == harness.operation_id
            assert conversation.pending_confirmation_claim_id == ""
    finally:
        harness.lease.close()
        harness.factory.close()


def test_locked_scope_aba_is_rejected_by_revision(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = support._primary_execution_harness(tmp_path, executor)
    with harness.sessions() as session:
        conversation = session.get(Conversation, harness.conversation.id)
        assert conversation is not None
        conversation.context_type = "global"
        conversation.scope_revision = 1
        session.commit()
    with harness.sessions() as session:
        conversation = session.get(Conversation, harness.conversation.id)
        assert conversation is not None
        conversation.context_type = "workspace"
        conversation.scope_revision = 2
        session.commit()
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
        )
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "authorization_scope_changed"
        assert record is None
        assert calls == 0
    finally:
        harness.lease.close()
        harness.factory.close()


def test_locked_claim_cas_does_not_overwrite_another_attempt(tmp_path) -> None:
    calls = 0
    decisions: list[str] = []

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    harness = support._primary_execution_harness(tmp_path, executor)
    with harness.sessions() as session:
        conversation = session.get(Conversation, harness.conversation.id)
        assert conversation is not None
        conversation.pending_confirmation_claim_id = "another-attempt"
        session.commit()
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
            approval_decided_callback=lambda _session: decisions.append("approval.decided"),
        )
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "operation_identity_conflict"
        assert record is None
        assert calls == 0
        assert decisions == []
    finally:
        harness.lease.close()
        harness.factory.close()


@pytest.mark.parametrize("mutation", ("reparent", "delete"))
def test_second_connection_target_change_is_denied_before_claim_and_executor(
    tmp_path, mutation: str
) -> None:
    harness = _scoped_approval_harness(
        tmp_path,
        tool_name="update_note",
        proposal={"id": 1, "company": "updated"},
    )
    assert harness.note_id == 1
    with harness.sessions() as session:
        if mutation == "reparent":
            session.execute(
                update(InterviewNote)
                .where(InterviewNote.id == harness.note_id)
                .values(application_id=harness.second_id)
            )
        else:
            session.execute(delete(InterviewNote).where(InterviewNote.id == harness.note_id))
        session.commit()
    try:
        execution, record = _execute_scoped(harness)
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "scope_access_denied"
        assert record is None
        assert harness.executor_calls == []
        with harness.sessions() as session:
            operation = session.get(WriteOperation, harness.operation_id)
            conversation = session.get(Conversation, harness.conversation.id)
            assert operation is not None and operation.status == "proposed"
            assert conversation is not None
            assert conversation.pending_confirmation_claim_id == ""
    finally:
        harness.lease.close()
        harness.factory.close()


def test_second_connection_parent_delete_denies_standalone_add_note(tmp_path) -> None:
    harness = _scoped_approval_harness(
        tmp_path,
        tool_name="add_note",
        proposal={"company": "Standalone"},
    )
    ApplicationsRepository(harness.sessions).delete(harness.first_id)
    try:
        execution, record = _execute_scoped(harness)
        assert isinstance(execution, OperationUnknown)
        assert execution.code == "authorization_scope_unavailable"
        assert record is None
        assert harness.executor_calls == []
    finally:
        harness.lease.close()
        harness.factory.close()


def test_modify_to_cross_application_is_rejected_during_prepare(tmp_path) -> None:
    harness = _scoped_approval_harness(
        tmp_path,
        tool_name="update_application_status",
        proposal={"id": 1, "status": "interview"},
        effective={"id": 2, "status": "interview"},
    )
    try:
        assert isinstance(harness.prepared_result, Rejected)
        assert harness.prepared_result.failure.code == "scope_access_denied"
        assert harness.executor_calls == []
        with harness.sessions() as session:
            operation = session.get(WriteOperation, harness.operation_id)
            conversation = session.get(Conversation, harness.conversation.id)
            assert operation is not None and operation.status == "proposed"
            assert conversation is not None
            assert conversation.pending_confirmation_claim_id == ""
    finally:
        harness.lease.close()
        harness.factory.close()


def test_approve_reject_race_has_one_terminal_winner_and_one_executor(tmp_path) -> None:
    entered = Event()
    release = Event()
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(10)
        return {"ok": True}

    harness = support._primary_execution_harness(
        tmp_path,
        executor,
        tool_name="save_offer_assessment",
    )
    harness.factory.begin_prepared_execution(
        harness.prepared,
        authority=harness.context.authority,
        use=AuthorityUse.APPROVED_WRITE_PREPARE,
    )
    reject_fingerprint = operation_request_fingerprint(
        harness.repository.key,
        operation_id=harness.operation_id,
        tool_call_id=harness.pending.tool_call_id,
        approved=False,
        edited_args_present=False,
        edited_args=None,
        rejection_feedback_present=False,
        rejection_feedback="",
        confirmation_token_fingerprint=ledger_fingerprint(
            harness.repository.key,
            "write-operation-confirmation-token-v1",
            b"synthetic-token",
        ),
        proposal_fingerprint=ledger_fingerprint(
            harness.repository.key,
            "write-operation-proposal-v1",
            {},
        ),
    )

    def approve():
        return harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
        )

    def reject():
        return harness.coordinator.reject_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            tool_call_id=harness.pending.tool_call_id,
            tool_name=harness.pending.tool_name,
            request_fingerprint=reject_fingerprint,
            visible_result="rejected",
            confirmation_token="synthetic-token",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            approval_future = pool.submit(approve)
            assert entered.wait(10)
            rejection_future = pool.submit(reject)
            release.set()
            approval, approval_record = approval_future.result(timeout=10)
            rejection = rejection_future.result(timeout=10)
        assert approval_record is not None
        assert isinstance(rejection, OperationUnknown)
        assert rejection.code == "operation_input_conflict"
        assert rejection.operation_id == approval.operation_id
        assert calls == 1
    finally:
        release.set()
        harness.lease.close()
        harness.factory.close()


def test_decision_callback_is_after_claim_and_before_executor(tmp_path) -> None:
    events: list[str] = []

    def executor(_args, _context):
        events.append("executor")
        return {"ok": True}

    harness = support._primary_execution_harness(
        tmp_path,
        executor,
        tool_name="save_offer_assessment",
    )
    harness.factory.begin_prepared_execution(
        harness.prepared,
        authority=harness.context.authority,
        use=AuthorityUse.APPROVED_WRITE_PREPARE,
    )
    try:
        execution, record = harness.coordinator.execute_primary(
            operation_id=harness.operation_id,
            conversation_id=harness.conversation.id,
            prepared=harness.prepared,
            context=harness.context,
            prepare_identity=harness.prepare_identity,
            request_fingerprint=harness.request_fingerprint,
            parent_route_binder=None,
            approval_decided_callback=lambda _session: events.append("approval.decided"),
        )
        assert record is not None
        assert execution.operation_id == harness.operation_id
        assert events == ["approval.decided", "executor"]
    finally:
        harness.lease.close()
        harness.factory.close()
