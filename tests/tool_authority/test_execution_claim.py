from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from types import SimpleNamespace
import pytest

import offerpilot.ai.tool_authority.composition as authority_composition
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    AuthorityFactory,
    AuthorityPhaseError,
    AuthorityUse,
    ExecutionClaim,
    TrustedContextScope,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ProviderToolContract,
    ToolFailure,
)
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.types import ToolCall
from offerpilot.db import init_database
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from tests.tool_metadata.factories import (
    compose_synthetic_bundle,
    synthetic_tool_spec,
    write_metadata,
)


ARGUMENTS = {"value": 1}
ARGUMENTS_JSON = '{"value":1}'
ARGUMENTS_DIGEST = "sha256:" + hashlib.sha256(ARGUMENTS_JSON.encode()).hexdigest()
_LEASES: list[object] = []


@pytest.fixture(autouse=True)
def _close_segment_leases():
    yield
    while _LEASES:
        getattr(_LEASES.pop(), "close")()


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
        capabilities=frozenset({"applications.write"}),
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
    contract = ProviderToolContract(
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
    )
    metadata = replace(write_metadata("sealed_write"), editable_fields=())
    spec = replace(
        synthetic_tool_spec("sealed_write", metadata=metadata),
        contract=contract,
        decoder=lambda value: dict(value),
        executor=executor,
    )
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    source = compose_synthetic_bundle()
    manifest = {**source["manifest"], "typed_tools": (spec.name,)}
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    _LEASES.append(lease)
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
    result = prepare_call(
        lease,
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
    factory.begin_prepared_execution(
        prepared,  # type: ignore[arg-type]
        authority=authority,
        use=AuthorityUse.APPROVED_WRITE_PREPARE,
    )
    if not transaction.in_transaction():
        transaction.begin()
    outer_transaction = transaction.get_transaction()
    assert outer_transaction is not None
    factory.register_execution_transaction(transaction, outer_transaction, authority=authority)
    claim = factory.issue_execution_claim(
        authority,
        prepared=prepared,  # type: ignore[arg-type]
        pending=pending,
        operation_id="operation-1",
        tool_call_id="call-1",
        tool_name="sealed_write",
        effective_args_digest=ARGUMENTS_DIGEST,
        session=transaction,
        transaction=outer_transaction,
    )
    execute_identity = factory.create_approved_write_execute_identity(
        prepare_identity,  # type: ignore[arg-type]
        prepared=prepared,  # type: ignore[arg-type]
        execution_claim=claim,
    )
    return claim, execute_identity


def _execute_claim(prepared, context, session, claim, execute_identity):
    return execute_prepared(
        prepared,
        context.bind(session),
        call_identity=execute_identity,
        execution_claim=claim,
        locked_effective_args_digest=ARGUMENTS_DIGEST,
    )


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


def test_execution_claim_requires_consumed_outer_prepare_transition(tmp_path) -> None:
    factory, authority, _context, pending, prepared, _prepare_identity, sessions = _setup(
        tmp_path,
        lambda _args, _context: {"ok": True},
    )
    try:
        with sessions() as session:
            session.begin()
            outer_transaction = session.get_transaction()
            assert outer_transaction is not None
            factory.register_execution_transaction(
                session,
                outer_transaction,
                authority=authority,
            )

            with pytest.raises(AuthorityPhaseError, match="outer approval transition"):
                factory.issue_execution_claim(
                    authority,
                    prepared=prepared,
                    pending=pending,
                    operation_id="operation-1",
                    tool_call_id="call-1",
                    tool_name="sealed_write",
                    effective_args_digest=ARGUMENTS_DIGEST,
                    session=session,
                    transaction=outer_transaction,
                )
    finally:
        factory.close()


def test_approved_outer_execution_requires_exact_prepared_origin(tmp_path) -> None:
    factory, authority, context, _pending, prepared, origin, _sessions = _setup(
        tmp_path,
        lambda args, _context: args,
    )
    try:
        sibling = factory.create_approved_write_prepare_identity(
            authority,
            approval_context=context,
            request_identity=object(),
        )

        with pytest.raises(AuthorityPhaseError, match="origin"):
            execute_prepared(
                prepared,
                context,
                call_identity=sibling,
                confirmation_claimer=lambda _prepared: None,
            )

        assert factory._prepared_origins[id(prepared)] is origin
        assert factory._prepared_execution_states[id(prepared)] == set()
    finally:
        factory.close()


def test_approved_inner_identity_requires_exact_prepared_origin(tmp_path) -> None:
    factory, authority, context, pending, prepared, origin, sessions = _setup(
        tmp_path,
        lambda args, _context: args,
    )
    try:
        factory.begin_prepared_execution(
            prepared,
            authority=authority,
            use=AuthorityUse.APPROVED_WRITE_PREPARE,
        )
        with sessions() as session:
            transaction = session.begin()
            factory.register_execution_transaction(session, transaction, authority=authority)
            claim = factory.issue_execution_claim(
                authority,
                prepared=prepared,
                pending=pending,
                operation_id="operation-1",
                tool_call_id="call-1",
                tool_name="sealed_write",
                effective_args_digest=ARGUMENTS_DIGEST,
                session=session,
                transaction=transaction,
            )
            sibling = factory.create_approved_write_prepare_identity(
                authority,
                approval_context=context,
                request_identity=object(),
            )

            with pytest.raises(AuthorityPhaseError, match="origin"):
                factory.create_approved_write_execute_identity(
                    sibling,
                    prepared=prepared,
                    execution_claim=claim,
                )

            assert factory._prepared_origins[id(prepared)] is origin
            factory.revoke(claim)
    finally:
        factory.close()


def test_revoke_authority_clears_prepared_execution_state(tmp_path) -> None:
    factory, authority, _context, _pending, prepared, _origin, _sessions = _setup(
        tmp_path,
        lambda args, _context: args,
    )
    try:
        prepared_id = id(prepared)
        assert prepared_id in factory._prepared_execution_states

        factory.revoke_authority(authority)

        assert prepared_id not in factory._prepared_execution_states
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
    stages: list[str] = []

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
                stage_sink=stages.append,
            )
            assert isinstance(record.outcome, ToolFailure)
            assert calls == 1
            assert factory.claim_state(claim) is None
            first_stages = tuple(stages)
            outer_transaction = session.get_transaction()
            assert outer_transaction is not None
            with pytest.raises(AuthorityPhaseError, match="execution transition"):
                factory.issue_execution_claim(
                    authority,
                    prepared=prepared,
                    pending=pending,
                    operation_id="operation-1",
                    tool_call_id="call-1",
                    tool_name="sealed_write",
                    effective_args_digest=ARGUMENTS_DIGEST,
                    session=session,
                    transaction=outer_transaction,
                )
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                    stage_sink=stages.append,
                )
            assert calls == 1
            assert tuple(stages) == first_stages
    finally:
        factory.close()


def test_base_exception_revokes_claim_and_propagates(tmp_path) -> None:
    calls = 0
    stages: list[str] = []

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
                    stage_sink=stages.append,
                )
            assert calls == 1
            assert factory.claim_state(claim) is None
            first_stages = tuple(stages)
            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                    stage_sink=stages.append,
                )
            assert calls == 1
            assert tuple(stages) == first_stages
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


@pytest.mark.parametrize("end_transaction", ("commit", "rollback", "close"))
def test_ended_outer_transaction_revokes_claim_before_executor(
    tmp_path, end_transaction: str
) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            getattr(session, end_transaction)()
            with pytest.raises(AuthorityPhaseError):
                _execute_claim(prepared, context, session, claim, execute_identity)
            assert factory.claim_state(claim) is None
        assert calls == 0
    finally:
        factory.close()


def test_execution_claim_requires_current_active_outer_transaction(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1

    factory, authority, _context, pending, prepared, _prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            inactive_outer = session.begin()
            session.rollback()
            factory.register_transaction(inactive_outer, authority=authority)
            with pytest.raises(AuthorityPhaseError):
                factory.issue_execution_claim(
                    authority,
                    prepared=prepared,
                    pending=pending,
                    operation_id="operation-1",
                    tool_call_id="call-1",
                    tool_name="sealed_write",
                    effective_args_digest=ARGUMENTS_DIGEST,
                    session=session,
                    transaction=inactive_outer,
                )
        assert calls == 0
    finally:
        factory.close()


def test_nested_savepoint_uses_outer_transaction_identity(tmp_path) -> None:
    calls = 0

    def executor(_args, context):
        nonlocal calls
        calls += 1
        assert context.bound_session.get_transaction() is outer_transaction
        assert context.bound_session.get_nested_transaction() is nested_transaction
        return {"ok": True}

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            session.begin()
            outer_transaction = session.get_transaction()
            assert outer_transaction is not None
            claim, execute_identity = _issue(
                factory, authority, pending, prepared, prepare_identity, session
            )
            with session.begin_nested() as nested_transaction:
                record = _execute_claim(prepared, context, session, claim, execute_identity)
            assert record.execution_started
            assert calls == 1
    finally:
        factory.close()


def test_nested_transaction_cannot_replace_outer_claim_identity(tmp_path) -> None:
    calls = 0

    def executor(_args, _context):
        nonlocal calls
        calls += 1

    factory, authority, _context, pending, prepared, _prepare_identity, sessions = _setup(
        tmp_path, executor
    )
    try:
        with sessions() as session:
            session.begin()
            with session.begin_nested() as nested_transaction:
                factory.register_transaction(nested_transaction, authority=authority)
                with pytest.raises(AuthorityPhaseError):
                    factory.issue_execution_claim(
                        authority,
                        prepared=prepared,
                        pending=pending,
                        operation_id="operation-1",
                        tool_call_id="call-1",
                        tool_name="sealed_write",
                        effective_args_digest=ARGUMENTS_DIGEST,
                        session=session,
                        transaction=nested_transaction,
                    )
        assert calls == 0
    finally:
        factory.close()


def test_execution_claim_revoke_cleans_issued_token_after_token_field_mutation(
    tmp_path,
) -> None:
    factory, authority, _context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path,
        lambda _args, _context: {"ok": True},
    )
    try:
        with sessions() as session:
            claim, _execute_identity = _issue(
                factory,
                authority,
                pending,
                prepared,
                prepare_identity,
                session,
            )
            issued_token = claim.execution_claim_instance_token
            object.__setattr__(claim, "execution_claim_instance_token", object())

            assert factory.is_active(issued_token)
            factory.revoke(claim)

            assert id(issued_token) not in factory._objects
            assert id(issued_token) not in authority_composition._ACTIVE_OBJECTS
    finally:
        factory.close()
