from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from offerpilot.ai.agent import PendingAction
from offerpilot.ai.tool_runtime.contracts import ToolFailure, ToolSuccess
from offerpilot.ai.types import Message
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationReplay,
    TerminalPayload,
    ledger_fingerprint,
)
from offerpilot.pilot_runtime.contracts import ConfirmationRequest
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
    DeliveryBundle,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.persistence import PersistenceResult, PersistenceStatus
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies


class _Operations:
    def __init__(self, *, status: str = "proposed") -> None:
        self.key = SimpleNamespace(key_id="test", secret=b"k" * 32)
        self.operation_id = str(uuid4())
        self.operation = SimpleNamespace(
            id=self.operation_id,
            conversation_id=7,
            status=status,
            tool_call_id="call-1",
            tool_name="create_application",
            proposal_fingerprint="proposal",
            confirmation_token_fingerprint="",
            delivery_status="completed",
        )
        token = "t" * 64
        self.operation.confirmation_token_fingerprint = ledger_fingerprint(
            self.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        self.token = token
        self.replay_calls = 0
        self.converge_calls = 0

    def get(self, _operation_id: str) -> object:
        return self.operation

    def replay(self, _operation: object, _fingerprint: str) -> OperationReplay:
        self.replay_calls += 1
        return OperationReplay(
            self.operation_id,
            TerminalPayload(
                status="committed",
                result_contract="typed_json_v1",
                result_json='{"ok":true}',
                visible_result="saved",
                transport_json="{}",
                undo_json=None,
                failure_category=None,
                failure_code=None,
                digest="sha256:result",
            ),
            "completed",
            1,
            None,
            "final_response",
            "saved",
        )

    def converge_expired_delivery(self, _operation_id: str) -> OperationReplay:
        self.converge_calls += 1
        return self.replay(self.operation, "ignored")


class _Persistence:
    def __init__(self, pending: PendingAction | None) -> None:
        self.pending = pending
        self.pending_reads = 0

    def get_pending_action(self, _conversation_id: int) -> PendingAction | None:
        self.pending_reads += 1
        return self.pending


class _WriteCoordinator:
    def __init__(self) -> None:
        self.reject_calls = 0
        self.execute_calls = 0

    def reject_primary(self, **_kwargs: object) -> object:
        self.reject_calls += 1
        return SimpleNamespace(
            operation_id="operation",
            ownership=None,
            payload=SimpleNamespace(
                status="rejected",
                visible_result="已取消这次操作。",
                undo_json=None,
            ),
        )

    def execute_primary(self, **_kwargs: object) -> object:
        self.execute_calls += 1
        return (
            OperationCommitted(
                str(_kwargs["operation_id"]),
                TerminalPayload(
                    status="committed",
                    result_contract="typed_json_v1",
                    result_json='{"ok":true}',
                    visible_result="saved",
                    transport_json="{}",
                    undo_json=None,
                    failure_category=None,
                    failure_code=None,
                    digest="sha256:result",
                ),
                None,
            ),
            SimpleNamespace(
                outcome=ToolSuccess({"ok": True}),
                terminal_persisted=True,
                replayed=False,
            ),
        )


def _deps(
    persistence: object,
    operations: object,
    write_coordinator: object | None = None,
) -> ConfirmationDependencies:
    return ConfirmationDependencies(
        persistence=persistence,
        write_operations=operations,
        write_coordinator=write_coordinator,
    )


def test_terminal_replay_is_ledger_first_and_never_reads_pending() -> None:
    operations = _Operations(status="committed")
    persistence = _Persistence(None)
    coordinator = ConfirmationCoordinator(_deps(persistence, operations))
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )

    replay = coordinator.terminal_replay(request)

    assert replay is not None
    assert replay.operation_id == operations.operation_id
    assert operations.replay_calls == 1
    assert persistence.pending_reads == 0


def test_terminal_replay_rejects_wrong_token_without_pending_or_runtime_calls() -> None:
    operations = _Operations(status="committed")
    persistence = _Persistence(None)
    coordinator = ConfirmationCoordinator(_deps(persistence, operations))
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token="x" * 64,
    )

    with pytest.raises(Exception) as raised:
        coordinator.terminal_replay(request)

    assert getattr(raised.value, "code", None) == "operation_input_conflict"
    assert operations.replay_calls == 0
    assert persistence.pending_reads == 0


def test_reject_uses_ledger_cas_without_decoding_or_catalog() -> None:
    pending = PendingAction(
        tool_call_id="call-1",
        tool_name="create_application",
        args='{"malformed":',
        human="create",
        operation_id=str(uuid4()),
    )
    operations = _Operations(status="proposed")
    pending = PendingAction(
        pending.tool_call_id,
        pending.tool_name,
        pending.args,
        pending.human,
        operations.operation_id,
    )
    persistence = _Persistence(pending)
    write_coordinator = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(
        _deps(persistence, operations, write_coordinator)
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=False,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
        rejection_feedback="kept private",
        rejection_feedback_present=True,
    )

    session = coordinator.reject(request)
    result = session.on_confirmation_attempt(pending, None)

    assert result is None
    assert write_coordinator.reject_calls == 1
    assert session.state.rejection_feedback == "kept private"
    assert "kept private" not in repr(session.state)


def test_approve_claims_executes_once_and_delivers_once() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1",
        "create_application",
        "{}",
        "create",
        operations.operation_id,
    )
    persistence = _Persistence(pending)
    deliveries: list[object] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        deliveries.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )
    session = coordinator.approve_modify(request, pending=pending)
    prepared = SimpleNamespace(
        pending_identity="call-1:create_application",
        pending_action_revision=1,
        tool_call_id="call-1",
        spec=SimpleNamespace(name="create_application"),
        arguments_digest="digest",
    )

    authorization = session.on_confirmation_attempt(pending, cast(Any, prepared))
    assert authorization is not None
    record = session.execute_operation(prepared, object(), authorization)  # type: ignore[arg-type]
    session.on_confirmation_result(
        pending,
        True,
        Message(role="tool", content="saved", tool_call_id="call-1"),
        record,
    )
    coordinator.final_delivery(
        session,
        DeliveryBundle((Message(role="assistant", content="done"),)),
    )

    assert write.execute_calls == 1
    assert len(deliveries) == 1


def test_timeout_after_terminal_converges_fallback_and_ignores_late_bundle() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    deliveries: list[object] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        deliveries.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    session = coordinator.reject(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )
    session.on_confirmation_attempt(pending, None)
    fallback = coordinator.timeout_convergence(session)
    assert fallback is not None
    assert len(deliveries) == 1
    assert cast(Any, deliveries[0])["delivery_failure_code"] == "operation_delivery_failed"
    session.on_confirmation_result(
        pending,
        False,
        Message(role="tool", content="late", tool_call_id="call-1"),
        None,
    )
    assert len(deliveries) == 1


def test_cancel_before_claim_never_reaches_rejection_cas() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    session = coordinator.reject(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )

    coordinator.cancel_cleanup(session)
    result = session.on_confirmation_attempt(pending, None)

    assert isinstance(result, ToolFailure)
    assert getattr(result, "code", None) == "confirmation_claim_lost"
    assert write.reject_calls == 0


def test_service_reject_routes_directly_without_agent_driver() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    conversation = SimpleNamespace(id=7, archived_at=None)

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return conversation

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
        )
    )
    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert getattr(outcome, "write_status", None) == "cancelled"
    assert write.reject_calls == 1
