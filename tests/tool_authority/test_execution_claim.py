from __future__ import annotations

import hashlib
from dataclasses import fields
from types import SimpleNamespace
import pytest

from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    AuthorityFactory,
    AuthorityPhaseError,
    ExecutionClaim,
    TrustedContextScope,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    ConfirmationRequired,
    ProviderToolContract,
    ToolFailure,
    ToolSpec,
    WriteContract,
)
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository


ARGUMENTS = {"value": 1}
ARGUMENTS_JSON = '{"value":1}'
ARGUMENTS_DIGEST = "sha256:" + hashlib.sha256(ARGUMENTS_JSON.encode()).hexdigest()


class Cancelled(BaseException):
    pass


def _clone_claim(claim: ExecutionClaim) -> ExecutionClaim:
    clone = object.__new__(ExecutionClaim)
    for item in fields(claim):
        object.__setattr__(clone, item.name, getattr(claim, item.name))
    return clone


def _setup(tmp_path, executor):
    sessions = init_database(tmp_path / "claim.db")
    factory = AuthorityFactory()
    pending = SimpleNamespace(
        operation_id="operation-1",
        conversation_id=1,
        tool_call_id="call-1",
        tool_name="sealed_write",
        pending_action_revision=1,
        effective_args_digest=ARGUMENTS_DIGEST,
    )
    factory.register_pending(pending)
    authority = factory.create_approval_authority(
        operation_id="operation-1",
        conversation_id=1,
        conversation_scope_revision=0,
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        pending_identity=pending,
        pending_action_revision=1,
        tool_call_id="call-1",
        tool_name="sealed_write",
        effective_args_digest=ARGUMENTS_DIGEST,
    )
    context = ToolExecutionContext(
        authority=authority,
        applications=ApplicationsRepository(sessions),
        events=ApplicationEventsRepository(sessions),
        notes=NotesRepository(sessions),
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "name": "sealed_write",
                    "description": "sealed write",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            },
            name="sealed_write",
            description="sealed write",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        kind="write",
        decoder=lambda value: dict(value),
        executor=executor,
        confirmation_policy="required",
        binding_contract=BindingContract("none"),
        write_contract=WriteContract(),
    )
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    prepare_identity = factory.create_approved_write_prepare_identity(
        authority,
        approval_context=context,
        request_identity=object(),
    )
    result = prepare_call(
        catalog,
        context,
        ToolCall("call-1", "sealed_write", ARGUMENTS_JSON),
        call_identity=prepare_identity,
        pending_identity=pending,
        pending_action_revision=1,
        record_proposal=False,
    )
    assert isinstance(result, ConfirmationRequired)
    return factory, authority, context, pending, result.prepared, prepare_identity, sessions


def _issue(
    factory: AuthorityFactory,
    authority: ApprovalExecutionAuthority,
    pending: object,
    prepared: object,
    prepare_identity: object,
    transaction: object,
):
    factory.register_transaction(transaction, authority=authority)
    claim = factory.issue_execution_claim(
        authority,
        prepared=prepared,  # type: ignore[arg-type]
        pending=pending,
        operation_id="operation-1",
        tool_call_id="call-1",
        tool_name="sealed_write",
        effective_args_digest=ARGUMENTS_DIGEST,
        transaction=transaction,
    )
    execute_identity = factory.create_approved_write_execute_identity(
        prepare_identity,  # type: ignore[arg-type]
        prepared=prepared,  # type: ignore[arg-type]
        execution_claim=claim,
    )
    return claim, execute_identity


def test_write_without_operation_executor_fails_closed(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1

    factory, _authority, context, _pending, prepared, prepare_identity, _sessions = _setup(
        tmp_path, executor
    )
    try:
        record = execute_prepared(
            prepared,
            context,
            call_identity=prepare_identity,
            confirmation_claimer=lambda _prepared: None,
        )
        assert isinstance(record.outcome, ToolFailure)
        assert record.outcome.code == "confirmation_claim_required"
        assert calls == 0
    finally:
        factory.close()


def test_forged_execution_claim_is_rejected_before_executor(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            forged = _clone_claim(claim)
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=forged,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
        assert calls == 0
    finally:
        factory.close()


def test_changed_typed_args_revoke_claim_before_executor(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            prepared.typed_args["value"] = 2
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
            assert factory.claim_state(claim) is None
        assert calls == 0
    finally:
        factory.close()


def test_legal_claim_is_consumed_once_even_when_executor_raises(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        raise ValueError("domain failure")

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            record = execute_prepared(
                prepared,
                context.bind(session),
                call_identity=execute_identity,
                execution_claim=claim,
                locked_effective_args_digest=ARGUMENTS_DIGEST,
            )
            assert isinstance(record.outcome, ToolFailure)
            assert calls == 1
            assert factory.claim_state(claim) is None
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
            assert calls == 1
    finally:
        factory.close()


def test_base_exception_revokes_claim_and_propagates(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        raise Cancelled()

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            with pytest.raises(Cancelled):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
            assert calls == 1
            assert factory.claim_state(claim) is None
    finally:
        factory.close()


def test_execute_identity_rejects_different_same_authority_context(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    foreign_context = ToolExecutionContext(
        authority=authority,
        applications=context.applications,
        events=context.events,
        notes=context.notes,
        offers=context.offers,
        resumes=context.resumes,
        jd_analyses=context.jd_analyses,
        run_recorder=NullRunRecorder(),
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    foreign_context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
        assert calls == 0
    finally:
        factory.close()
