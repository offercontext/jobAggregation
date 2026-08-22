from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, RLock
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4
from dataclasses import replace

import pytest

from offerpilot.ai.agent import PendingAction
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.pipeline import prepare_call
from offerpilot.ai.tool_runtime.contracts import ToolFailure, ToolSuccess
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    OperationCommitted,
    OperationReplay,
    TerminalPayload,
    ledger_fingerprint,
    WriteOperationCoordinator,
    WriteOperationError,
    WriteOperationRepository,
    load_or_create_ledger_key,
)
from offerpilot.agent_runtime.journal import NullRunRecorder, NullRunRecorderFactory
from offerpilot.chat_transport import SseAgentExecutionHost, outcome_http_payload
from offerpilot.db import init_database
from offerpilot.pilot_runtime.persistence import ChatPersistenceCoordinator
from offerpilot.repositories.application_events import ApplicationEventsRepository
from offerpilot.repositories.applications import ApplicationsRepository
from offerpilot.repositories.chat import ChatRepository
from offerpilot.repositories.jd import JDAnalysesRepository
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.offers import OffersRepository
from offerpilot.repositories.resumes import ResumesRepository
from offerpilot.pilot_runtime.contracts import (
    AssistantDeltaEvent,
    AssistantMessageEvent,
    CompletedEvent,
    ConfirmationRequest,
    ConfirmationRequiredOutcome,
    MetaEvent,
    PreparedStreamExecution,
    RuntimeFailureOutcome,
    RuntimeTransportContext,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
    ConfirmationReplayError,
    DeliveryBundle,
    _confirmation_token,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.errors import RuntimeAgentTimedOut, RuntimeFailureCode
from offerpilot.pilot_runtime.persistence import PersistenceResult, PersistenceStatus
import offerpilot.pilot_runtime.composition as composition_module
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies
from offerpilot.pilot_runtime.service import ResolvedModel


class _Operations:
    def __init__(self, *, status: str = "proposed", delivery_outcome: str = "final_response") -> None:
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
        self.delivery_outcome = delivery_outcome
        token = "t" * 64
        self.operation.confirmation_token_fingerprint = ledger_fingerprint(
            self.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        self.token = token
        self.replay_calls = 0
        self.converge_calls = 0
        self.heartbeat_calls = 0

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
            self.delivery_outcome,
            "saved",
        )

    def converge_expired_delivery(self, _operation_id: str) -> OperationReplay:
        self.converge_calls += 1
        return self.replay(self.operation, "ignored")

    def heartbeat(self, _ownership: DeliveryOwnership) -> bool:
        self.heartbeat_calls += 1
        return True


class _Persistence:
    def __init__(self, pending: PendingAction | None) -> None:
        self.pending = pending
        self.pending_reads = 0

    def get_pending_action(self, _conversation_id: int) -> PendingAction | None:
        self.pending_reads += 1
        return self.pending

    def list_messages(self, _conversation_id: int) -> tuple[object, ...]:
        return ()


class _WriteCoordinator:
    def __init__(self) -> None:
        self.reject_calls = 0
        self.execute_calls = 0

    def reject_primary(self, **_kwargs: object) -> object:
        self.reject_calls += 1
        return SimpleNamespace(
            operation_id="operation",
            ownership=DeliveryOwnership(str(_kwargs["operation_id"]), 1, b"owner", "owner"),
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
                DeliveryOwnership(str(_kwargs["operation_id"]), 1, b"owner", "owner"),
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


def test_terminal_replay_preserves_ledger_tool_metadata_for_http_and_sse() -> None:
    class RichOperations(_Operations):
        def replay(self, _operation: object, _fingerprint: str) -> OperationReplay:
            self.replay_calls += 1
            return OperationReplay(
                self.operation_id,
                TerminalPayload(
                    status="committed",
                    result_contract="typed_json_v1",
                    result_json='{"ok":true}',
                    visible_result="tool-visible",
                    transport_json=json.dumps(
                        {
                            "tool_call_id": "ledger-call",
                            "tool_name": "save_offer_assessment",
                            "status": "success",
                            "summary": "saved summary",
                            "evidence": [{"source": "offer", "id": 1}],
                            "affected_resources": [{"kind": "offer", "id": 1}],
                            "changed_entities": [{"entity": "offer", "id": 1}],
                        }
                    ),
                    undo_json=None,
                    failure_category=None,
                    failure_code=None,
                    digest="sha256:result",
                ),
                "completed",
                1,
                None,
                "final_response",
                None,
            )

    operations = RichOperations(status="committed")
    coordinator = ConfirmationCoordinator(_deps(_Persistence(None), operations))
    outcome = coordinator.replay_outcome(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        )
    )

    assert outcome is not None
    assert outcome.message == "操作已完成。"
    assert outcome.tool_call_id == "ledger-call"
    assert outcome.tool_name == "save_offer_assessment"
    assert outcome.visible_result == "tool-visible"
    assert outcome.summary == "saved summary"
    assert outcome.evidence == ({"source": "offer", "id": 1},)
    assert outcome.affected_resources == ({"kind": "offer", "id": 1},)
    assert outcome.changed_entities == ({"entity": "offer", "id": 1},)

    http_payload = outcome_http_payload(outcome)
    assert http_payload["message"] == "操作已完成。"
    events = PilotRuntime._ledger_direct_events(outcome)
    tool_call = next(event for event in events if isinstance(event, ToolCallEvent))
    tool_result = next(event for event in events if isinstance(event, ToolResultEvent))
    assert tool_call.tool_call_id == "ledger-call"
    assert tool_call.tool_name == "save_offer_assessment"
    assert tool_result.tool_call_id == "ledger-call"
    assert tool_result.tool_name == "save_offer_assessment"
    assert tool_result.visible_result == "tool-visible"
    assert tool_result.summary == "saved summary"
    assert tool_result.evidence == ({"source": "offer", "id": 1},)
    assert tool_result.affected_resources == ({"kind": "offer", "id": 1},)
    assert tool_result.changed_entities == ({"entity": "offer", "id": 1},)
    assert all(event.tool_call_id != "replay-tool" for event in events if isinstance(event, (ToolCallEvent, ToolResultEvent)))
    assert all(event.tool_name != "replayed_write" for event in events if isinstance(event, (ToolCallEvent, ToolResultEvent)))

def test_chained_terminal_replay_loads_child_pending_only_after_ledger_replay() -> None:
    operations = _Operations(status="committed", delivery_outcome="chained_pending")
    child = PendingAction(
        "child-call",
        "create_application",
        '{"company_name":"child"}',
        "child",
        str(uuid4()),
    )
    persistence = _Persistence(child)
    coordinator = ConfirmationCoordinator(_deps(persistence, operations))
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )

    outcome = coordinator.replay_outcome(request)

    assert isinstance(outcome, ConfirmationRequiredOutcome)
    assert outcome.replayed is True
    assert outcome.pending_action is not None
    assert outcome.pending_action.tool_name == child.tool_name
    assert operations.replay_calls == 1
    assert persistence.pending_reads == 1


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


def test_reject_claim_race_keeps_terminal_replay_without_delivery_lease() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )

    class ReplayRejectCoordinator(_WriteCoordinator):
        def reject_primary(self, **kwargs: object) -> object:
            self.reject_calls += 1
            operations.operation.status = "rejected"
            return operations.replay(operations.operation, str(kwargs["request_fingerprint"]))

    persistence = _Persistence(pending)
    coordinator = ConfirmationCoordinator(
        _deps(persistence, operations, ReplayRejectCoordinator())
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=False,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )
    session = coordinator.reject(request, pending=pending)

    assert session.on_confirmation_attempt(pending, None) is None
    assert isinstance(session.state.terminal_execution, OperationReplay)
    assert session.state.delivery_heartbeat is None


def test_timeout_before_claim_closes_session_without_delivery_or_late_work() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    deliveries: list[object] = []
    persistence.persist_confirmation_delivery = lambda **kwargs: (  # type: ignore[attr-defined]
        deliveries.append(kwargs) or PersistenceResult(PersistenceStatus.PERSISTED)
    )
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))
    session = coordinator.approve_modify(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )

    assert coordinator.timeout_convergence(session) is None
    assert session.state.timed_out is True
    assert session.state.active is False
    assert session.state.confirmation_attempted is False
    assert deliveries == []


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


def test_approved_resume_injects_session_executor_and_loads_source_once_after_terminal() -> None:
    """RED: the driver must receive the Ledger executor and a single-use loader.

    The old extracted route eagerly loaded source before ``resume_after_confirm``
    and passed the resolver's context unchanged.  A real driver consequently
    either executed the provider directly or loaded the source twice.
    """

    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    write = _WriteCoordinator()
    sources = SimpleNamespace(calls=0)

    def load_source(*_args: object, **_kwargs: object) -> tuple[Message, ...]:
        sources.calls += 1
        return (Message(role="assistant", content="history"),)

    context = SimpleNamespace(operation_executor=None)
    observed: dict[str, object] = {}

    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def fingerprint_pending_identity(self, _value: object) -> str:
            return "pending-fingerprint"

        def capture_context(self, *_args: object, **_kwargs: object) -> None:
            self.events.append("context")

        def append_event(self, event: object) -> None:
            self.events.append(str(getattr(event, "event_type", "event")))

        def resume(self, *_args: object, **_kwargs: object) -> None:
            self.events.append("resume")

        def finish(self, *_args: object, **_kwargs: object) -> None:
            self.events.append("finish")

        def abandon(self, *_args: object, **_kwargs: object) -> None:
            self.events.append("abandon")

    recorder = Recorder()

    class Journal:
        def resume_waiting_run(self, *_args: object, **_kwargs: object) -> Recorder:
            return recorder

    class Driver:
        def resume_after_confirm(self, messages: list[Message], pending: PendingAction, approved: bool, auto_approve: bool, max_iter: int, **kwargs: object) -> object:
            del approved, auto_approve, max_iter
            observed["messages"] = messages
            observed["run_recorder"] = kwargs["run_recorder"]
            tool_context = kwargs["tool_context"]
            executor = getattr(tool_context, "operation_executor", None)
            observed["executor"] = executor
            assert callable(executor)
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id="call-1",
                spec=SimpleNamespace(name="create_application"),
                arguments_digest="digest",
            )
            authorization = kwargs["confirmation_attempt_sink"](pending, prepared)
            assert not isinstance(authorization, ToolFailure)
            record = executor(prepared, tool_context, authorization)
            origin = Message(role="tool", content="saved", tool_call_id="call-1")
            kwargs["confirmation_result_sink"](pending, True, origin, record)
            loader = kwargs["continuation_message_loader"]
            assert loader() == (Message(role="assistant", content="history"),)
            assert loader() == (Message(role="assistant", content="history"),)
            return SimpleNamespace(
                added=(origin, Message(role="assistant", content="done")),
                reply="done",
                pending=None,
                records=(record,),
                failures=(),
            )

    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    def resolve(_request: object, _conversation: object) -> ResolvedModel:
        return ResolvedModel(model=object(), tool_context=context)

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=resolve,
            agent_driver=Driver(),
            source_loader=SimpleNamespace(load=load_source),  # type: ignore[arg-type]
            journal=Journal(),
        )
    )
    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert getattr(outcome, "message", None) == "done"
    assert callable(observed["executor"])
    assert observed["messages"] == []
    assert observed["run_recorder"] is recorder
    assert "context" in recorder.events
    assert sources.calls == 1


def test_sync_confirmation_defers_origin_tool_result_until_authoritative_delivery() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.CAS_LOST
    )
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))
    context = SimpleNamespace(operation_executor=None)
    events: list[object] = []

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(
            self,
            _messages: list[Message],
            current: PendingAction,
            _approved: bool,
            _auto_approve: bool,
            _max_iter: int,
            **kwargs: object,
        ) -> object:
            sink = kwargs["event_sink"]
            sink({
                "event": "tool_call",
                "data": {
                    "tool_call_id": current.tool_call_id,
                    "tool_name": current.tool_name,
                    "kind": "write",
                    "confirm_mode": "approved",
                },
            })
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id=current.tool_call_id,
                spec=SimpleNamespace(name=current.tool_name),
                arguments_digest="digest",
            )
            authorization = kwargs["confirmation_attempt_sink"](current, prepared)
            record = kwargs["tool_context"].operation_executor(
                prepared, kwargs["tool_context"], authorization
            )
            origin = Message(role="tool", content="saved", tool_call_id=current.tool_call_id)
            kwargs["confirmation_result_sink"](current, True, origin, record)
            sink({
                "event": "tool_result",
                "data": {
                    "tool_call_id": current.tool_call_id,
                    "tool_name": current.tool_name,
                    "status": "success",
                    "summary": "saved",
                    "visible_result": "saved",
                    "operation_id": operations.operation_id,
                    "write_status": "success",
                },
            })
            return SimpleNamespace(
                added=(origin,),
                reply="",
                pending=None,
                records=(record,),
                failures=(),
            )

    class Sink:
        def emit(self, event: object) -> None:
            events.append(event)

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=context
            ),
            agent_driver=Driver(),
        )
    )
    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        event_sink=Sink(),  # type: ignore[arg-type]
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert getattr(outcome, "code", None).value == "stale_pending_action"
    assert not any(isinstance(event, ToolResultEvent) for event in events)


def test_missing_delivery_heartbeat_fails_closed_before_executor_or_delivery() -> None:
    class NoHeartbeatOperations(_Operations):
        heartbeat = None

    operations = NoHeartbeatOperations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    delivery_calls: list[object] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        delivery_calls.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    session = coordinator.approve_modify(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )
    prepared = SimpleNamespace(
        pending_identity="call-1:create_application",
        pending_action_revision=1,
        tool_call_id="call-1",
        spec=SimpleNamespace(name="create_application"),
        arguments_digest="digest",
    )

    with pytest.raises(WriteOperationError) as raised:
        session.on_confirmation_attempt(pending, prepared)

    assert raised.value.code == "operation_unavailable"
    assert write.execute_calls == 0
    assert operations.operation.status == "proposed"
    assert persistence.pending is pending
    assert delivery_calls == []


def test_missing_delivery_heartbeat_maps_runtime_confirmation_to_503() -> None:
    class NoHeartbeatOperations(_Operations):
        heartbeat = None

    operations = NoHeartbeatOperations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    delivery_calls: list[object] = []
    persistence.persist_confirmation_delivery = lambda **kwargs: (  # type: ignore[attr-defined]
        delivery_calls.append(kwargs) or PersistenceResult(PersistenceStatus.PERSISTED)
    )
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(
            self,
            _messages: list[Message],
            current: PendingAction,
            _approved: bool,
            _auto_approve: bool,
            _max_iter: int,
            **kwargs: object,
        ) -> object:
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id=current.tool_call_id,
                spec=SimpleNamespace(name=current.tool_name),
                arguments_digest="digest",
            )
            kwargs["confirmation_attempt_sink"](current, prepared)
            raise AssertionError("heartbeat guard must stop before Agent execution")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=SimpleNamespace(operation_executor=None)
            ),
            agent_driver=Driver(),
        )
    )
    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert outcome.status_code == 503
    assert operations.operation.status == "proposed"
    assert persistence.pending is pending
    assert delivery_calls == []


def test_delivery_capability_failure_resets_in_progress_and_stops_heartbeat() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))
    session = coordinator.reject(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )
    assert session.on_confirmation_attempt(pending, None) is None
    session.on_confirmation_result(
        pending,
        False,
        Message(role="tool", content="cancelled", tool_call_id=pending.tool_call_id),
        None,
    )

    with pytest.raises(WriteOperationError):
        coordinator.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content="cancelled"),)),
        )

    assert session.state.delivery_in_progress is False
    assert session.state.delivery_heartbeat is None


def test_delivery_without_lease_cannot_use_legacy_ownership_none_atom() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    delivery_calls: list[object] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        delivery_calls.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))
    session = coordinator.reject(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )
    assert session.on_confirmation_attempt(pending, None) is None
    session.on_confirmation_result(
        pending,
        False,
        Message(role="tool", content="cancelled", tool_call_id=pending.tool_call_id),
        None,
    )
    coordinator.stop_heartbeat(session)
    with session.state.lock:
        session.state.delivery_ownership = None

    with pytest.raises(WriteOperationError) as raised:
        coordinator.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content="cancelled"),)),
        )

    assert raised.value.code == "operation_unavailable"
    assert session.state.delivery_in_progress is False
    assert delivery_calls == []


def test_journal_does_not_suspend_chained_pending_when_delivery_failed() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.suspend_calls = 0
            self.finish_calls = 0

        def suspend(self, *_args: object, **_kwargs: object) -> None:
            self.suspend_calls += 1

        def finish(self, *_args: object, **_kwargs: object) -> None:
            self.finish_calls += 1

    recorder = Recorder()
    child = PendingAction(
        "child-call", "create_application", "{}", "child", str(uuid4())
    )
    runtime = PilotRuntime(RuntimeDependencies())
    runtime._close_ledger_journal(
        recorder,
        True,
        RuntimeFailureOutcome(
            RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
            "delivery failed",
            503,
            retryable=True,
        ),
        InMemoryRuntimeInvocationControl(),
        pending=child,
    )

    assert recorder.suspend_calls == 0
    assert recorder.finish_calls == 1


def test_reject_preheader_does_not_touch_conversation_or_model() -> None:
    """RED: rejection is the direct Ledger worker path."""

    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", '{"malformed":', "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    calls = {"conversation": 0, "model": 0}

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            calls["conversation"] += 1
            raise AssertionError("rejection must not load conversation")

    def resolve(_request: object, _conversation: object) -> ResolvedModel:
        calls["model"] += 1
        raise AssertionError("rejection must not resolve model")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=resolve,
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
    assert calls == {"conversation": 0, "model": 0}


def test_reject_session_does_not_load_conversation_for_generation() -> None:
    """Rejection must stay Ledger/Pending-only through session construction."""

    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", '{"malformed":', "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            raise AssertionError("rejection session must not load conversation")

    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=persistence,
            conversations=Conversations(),
            write_operations=operations,
            write_coordinator=_WriteCoordinator(),
        )
    )

    session = coordinator.reject(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        pending=pending,
    )

    assert session.state.continuation_generation is None


def test_real_sqlite_coordinator_executes_prepared_call_once_and_persists_delivery(
    tmp_path: Any,
) -> None:
    """The confirmation seam must exercise the production Ledger coordinator."""

    sessions = init_database(tmp_path / "offerpilot.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    operations = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, operations)
    persistence = ChatPersistenceCoordinator(chat)
    conversation = chat.create_conversation("workspace", "", "general")
    operation_id = str(uuid4())
    pending = PendingAction(
        "real-call",
        "save_offer_assessment",
        '{"id":1,"assessment":"ok"}',
        "save assessment",
        operation_id,
    )
    assert chat.persist_pending_action(conversation.id, pending, [])
    calls: list[str] = []
    base_spec = MODEL_TOOL_CATALOG.resolve("save_offer_assessment")
    assert base_spec is not None

    def execute(_args: object, _context: object) -> dict[str, object]:
        calls.append("executor")
        return {"ok": True}

    spec = replace(base_spec, binding_resolvers=(), executor=execute)

    class Catalog:
        def resolve(self, name: str) -> object | None:
            return spec if name == spec.name else None

        def validator_for(self, _name: str) -> object:
            return MODEL_TOOL_CATALOG.validator_for(spec.name)

        def provider_contracts(self) -> tuple[object, ...]:
            return (spec.contract,)

    context = ToolExecutionContext(
        capabilities=frozenset(ToolCapability),
        current_bindings={},
        applications=ApplicationsRepository(sessions),
        events=ApplicationEventsRepository(sessions),
        notes=NotesRepository(sessions),
        offers=OffersRepository(sessions),
        resumes=ResumesRepository(sessions),
        jd_analyses=JDAnalysesRepository(sessions),
        run_recorder=NullRunRecorder(),
    )
    prepared_result = prepare_call(
        Catalog(),
        context,
        ToolCall("real-call", spec.name, pending.args),
        pending_identity="real-call:save_offer_assessment",
        pending_action_revision=1,
        record_proposal=False,
    )
    prepared = getattr(prepared_result, "prepared", None)
    assert prepared is not None

    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=persistence,
            conversations=chat,
            write_operations=operations,
            write_coordinator=WriteOperationCoordinator(operations),
            catalog=Catalog(),
        )
    )
    request = ConfirmationRequest(
        conversation_id=conversation.id,
        approved=True,
        operation_id=operation_id,
        confirmation_token=_confirmation_token(pending),
    )
    session = coordinator.approve_modify(request, pending=pending, catalog=Catalog())
    authorization = session.on_confirmation_attempt(pending, prepared)
    assert not isinstance(authorization, ToolFailure)
    record = session.execute_operation(prepared, context, cast(Any, authorization))
    session.on_confirmation_result(
        pending,
        True,
        Message(role="tool", content="saved", tool_call_id=pending.tool_call_id),
        record,
    )
    delivered = coordinator.final_delivery(
        session,
        DeliveryBundle((Message(role="assistant", content="done"),)),
    )

    assert calls == ["executor"]
    assert getattr(delivered, "status", None) == PersistenceStatus.PERSISTED
    operation = operations.get(operation_id)
    assert operation is not None
    assert operation.status == "committed"
    assert operation.delivery_status == "completed"


def test_real_sqlite_two_connections_have_one_claim_and_one_executor(
    tmp_path: Any,
) -> None:
    """Two confirmation workers must converge on one Ledger executor."""

    sessions = init_database(tmp_path / "race.db")
    key = load_or_create_ledger_key(tmp_path, sessions)
    operations = WriteOperationRepository(sessions, key)
    chat = ChatRepository(sessions, operations)
    persistence = ChatPersistenceCoordinator(chat)
    conversation = chat.create_conversation("workspace", "", "general")
    operation_id = str(uuid4())
    pending = PendingAction(
        "race-call",
        "save_offer_assessment",
        '{"id":1,"assessment":"race"}',
        "save assessment",
        operation_id,
    )
    assert chat.persist_pending_action(conversation.id, pending, [])
    base_spec = MODEL_TOOL_CATALOG.resolve("save_offer_assessment")
    assert base_spec is not None
    calls = 0
    calls_lock = Lock()
    first_executor_entered = Event()
    release_first_executor = Event()

    def execute(_args: object, _context: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            ordinal = calls
        if ordinal == 1:
            first_executor_entered.set()
            assert release_first_executor.wait(10)
        return {"ok": True}

    spec = replace(base_spec, binding_resolvers=(), executor=execute)

    class Catalog:
        def resolve(self, name: str) -> object | None:
            return spec if name == spec.name else None

        def validator_for(self, _name: str) -> object:
            return MODEL_TOOL_CATALOG.validator_for(spec.name)

        def provider_contracts(self) -> tuple[object, ...]:
            return (spec.contract,)

    catalog = Catalog()

    def context() -> ToolExecutionContext:
        return ToolExecutionContext(
            capabilities=frozenset(ToolCapability),
            current_bindings={},
            applications=ApplicationsRepository(sessions),
            events=ApplicationEventsRepository(sessions),
            notes=NotesRepository(sessions),
            offers=OffersRepository(sessions),
            resumes=ResumesRepository(sessions),
            jd_analyses=JDAnalysesRepository(sessions),
            run_recorder=NullRunRecorder(),
        )

    prepared_result = prepare_call(
        catalog,
        context(),
        ToolCall("race-call", spec.name, pending.args),
        pending_identity="race-call:save_offer_assessment",
        pending_action_revision=1,
        record_proposal=False,
    )
    prepared = getattr(prepared_result, "prepared", None)
    assert prepared is not None
    request = ConfirmationRequest(
        conversation_id=conversation.id,
        approved=True,
        operation_id=operation_id,
        confirmation_token=_confirmation_token(pending),
    )

    def worker() -> str:
        coordinator = ConfirmationCoordinator(
            ConfirmationDependencies(
                persistence=persistence,
                conversations=chat,
                write_operations=operations,
                write_coordinator=WriteOperationCoordinator(operations),
                catalog=catalog,
            )
        )
        try:
            session = coordinator.approve_modify(request, pending=pending, catalog=catalog)
        except ConfirmationReplayError:
            return "replay"
        except WriteOperationError as exc:
            # The second connection may observe the first owner's live
            # delivery lease.  The production route maps this exact Ledger
            # state to HTTP 409; it must not claim or execute a second write.
            if exc.code == "operation_delivery_pending":
                return "in_progress"
            raise
        authorization = session.on_confirmation_attempt(pending, prepared)
        assert not isinstance(authorization, ToolFailure)
        try:
            record = session.execute_operation(prepared, context(), cast(Any, authorization))
        except ConfirmationReplayError:
            return "replay"
        session.on_confirmation_result(
            pending,
            True,
            Message(role="tool", content="saved", tool_call_id=pending.tool_call_id),
            record,
        )
        delivered = coordinator.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content="done"),)),
        )
        assert getattr(delivered, "status", None) is PersistenceStatus.PERSISTED
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert first_executor_entered.wait(10)
        second = pool.submit(worker)
        release_first_executor.set()
        results = {first.result(timeout=15), second.result(timeout=15)}

    assert results <= {"committed", "replay", "in_progress"}
    assert "committed" in results
    assert len(results) == 2
    assert calls == 1
    operation = operations.get(operation_id)
    assert operation is not None
    assert operation.status == "committed"
    assert operation.delivery_status == "completed"


def test_delivery_race_has_one_active_owner_call() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)

    class BlockingPersistence(_Persistence):
        def __init__(self) -> None:
            super().__init__(pending)
            self.entered = Event()
            self.release = Event()
            self.calls = 0
            self.payloads: list[dict[str, object]] = []

        def persist_confirmation_delivery(self, **kwargs: object) -> PersistenceResult:
            self.calls += 1
            self.payloads.append(kwargs)
            self.entered.set()
            assert self.release.wait(10)
            return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence = BlockingPersistence()
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
    assert session.on_confirmation_attempt(pending, None) is None
    session.on_confirmation_result(
        pending,
        False,
        Message(role="tool", content="cancelled", tool_call_id=pending.tool_call_id),
        None,
    )

    def deliver() -> object:
        return coordinator.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content="done"),)),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deliver)
        assert persistence.entered.wait(10)
        second = pool.submit(deliver)
        assert second.result(timeout=10) is None
        persistence.release.set()
        assert getattr(first.result(timeout=10), "status", None) is PersistenceStatus.PERSISTED
    assert persistence.calls == 1


def test_timeout_after_terminal_preserves_authoritative_undo_payload() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    captured: list[dict[str, object]] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        captured.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]

    class UndoWriteCoordinator(_WriteCoordinator):
        def execute_primary(self, **kwargs: object) -> object:
            self.execute_calls += 1
            payload = TerminalPayload(
                status="committed",
                result_contract="typed_json_v1",
                result_json='{"id":1}',
                visible_result="created",
                transport_json="{}",
                undo_json='{"kind":"delete_application","id":1}',
                failure_category=None,
                failure_code=None,
                digest="sha256:undo",
            )
            return (
                OperationCommitted(
                    str(kwargs["operation_id"]),
                    payload,
                    DeliveryOwnership(str(kwargs["operation_id"]), 1, b"owner", "owner"),
                ),
                SimpleNamespace(
                    outcome=ToolSuccess({"id": 1}),
                    terminal_persisted=True,
                    replayed=False,
                ),
            )

    write = UndoWriteCoordinator()
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
    authorization = session.on_confirmation_attempt(pending, prepared)
    assert not isinstance(authorization, ToolFailure)
    session.execute_operation(prepared, object(), cast(Any, authorization))

    fallback = coordinator.timeout_convergence(session)

    assert getattr(fallback, "status", None) is PersistenceStatus.PERSISTED
    assert session.state.succeeded is True
    assert captured[0]["undo"] == {"kind": "delete_application", "id": 1}


def test_timeout_during_executor_late_terminal_fallback_clears_once() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    deliveries: list[dict[str, object]] = []
    persistence.persist_confirmation_delivery = lambda **kwargs: (  # type: ignore[attr-defined]
        deliveries.append(kwargs) or PersistenceResult(PersistenceStatus.PERSISTED)
    )
    entered = Event()
    release = Event()

    class LateWriteCoordinator(_WriteCoordinator):
        def execute_primary(self, **kwargs: object) -> object:
            entered.set()
            assert release.wait(10)
            return super().execute_primary(**kwargs)

    write = LateWriteCoordinator()
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
    authorization = session.on_confirmation_attempt(pending, prepared)
    assert not isinstance(authorization, ToolFailure)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.execute_operation, prepared, object(), authorization)
        assert entered.wait(10)
        assert coordinator.timeout_convergence(session) is None
        release.set()
        record = future.result(timeout=10)

    session.on_confirmation_result(
        pending,
        True,
        Message(role="tool", content="saved", tool_call_id=pending.tool_call_id),
        record,
    )

    assert session.state.fallback_persisted is True
    assert session.state.active is False
    assert len(deliveries) == 1
    with pytest.raises(Exception):
        session.continuation_message_loader()


def test_rejection_stream_uses_complete_typed_events_without_user_message_saved() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            raise AssertionError("rejection stream must not load conversation")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
        )
    )
    prepared = runtime.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        transport=RuntimeTransportContext(
            mode="stream",
            transport_run_id=uuid4(),
            stream_version="pilot-sse-v1",
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(prepared, PreparedStreamExecution)
    state = cast(Any, prepared.opaque_state)
    assert [type(event) for event in state.events] == [
        MetaEvent,
        StatusEvent,
        ToolCallEvent,
        ToolResultEvent,
        AssistantMessageEvent,
    ]
    assert cast(Any, state.events[2]).confirm_mode == "rejected"
    assert cast(Any, state.events[3]).status == "error"
    assert not any(type(event).__name__ == "UserMessageSavedEvent" for event in state.events)


def test_approved_stream_orders_meta_status_tool_result_assistant_completed() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    context = SimpleNamespace(operation_executor=None)
    sources = SimpleNamespace(calls=0)
    recorder_events: list[str] = []

    class Recorder:
        def fingerprint_pending_identity(self, _value: object) -> str:
            return "pending-fingerprint"

        def capture_context(self, *_args: object, **_kwargs: object) -> None:
            recorder_events.append("context")

        def append_event(self, event: object) -> None:
            recorder_events.append(str(getattr(event, "event_type", "event")))

        def resume(self, *_args: object, **_kwargs: object) -> None:
            recorder_events.append("resume")

        def finish(self, *_args: object, **_kwargs: object) -> None:
            recorder_events.append("finish")

        def abandon(self, *_args: object, **_kwargs: object) -> None:
            recorder_events.append("abandon")

    recorder = Recorder()

    class Journal:
        def resume_waiting_run(self, *_args: object, **_kwargs: object) -> Recorder:
            return recorder

    def load_source(*_args: object, **_kwargs: object) -> tuple[Message, ...]:
        sources.calls += 1
        return (Message(role="assistant", content="history"),)

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(self, messages: list[Message], pending: PendingAction, approved: bool, auto_approve: bool, max_iter: int, **kwargs: object) -> object:
            del messages, approved, auto_approve, max_iter
            sink = kwargs["event_sink"]
            assert kwargs["run_recorder"] is recorder
            sink({
                "event": "tool_call",
                "data": {
                    "tool_call_id": "call-1",
                    "tool_name": "create_application",
                    "kind": "write",
                    "confirm_mode": "approved",
                },
            })
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id="call-1",
                spec=SimpleNamespace(name="create_application"),
                arguments_digest="digest",
            )
            authorization = kwargs["confirmation_attempt_sink"](pending, prepared)
            record = kwargs["tool_context"].operation_executor(
                prepared, kwargs["tool_context"], authorization
            )
            origin = Message(role="tool", content="saved", tool_call_id="call-1")
            kwargs["confirmation_result_sink"](pending, True, origin, record)
            sink({
                "event": "tool_result",
                "data": {
                    "tool_call_id": "call-1",
                    "tool_name": "create_application",
                    "status": "success",
                    "summary": "saved",
                    "visible_result": "saved",
                    "operation_id": operations.operation_id,
                    "write_status": "success",
                },
            })
            sink({"event": "assistant_delta", "data": {"delta": "done"}})
            assert kwargs["continuation_message_loader"]() == (
                Message(role="assistant", content="history"),
            )
            return SimpleNamespace(
                added=(origin, Message(role="assistant", content="done")),
                reply="done",
                pending=None,
                records=(record,),
                failures=(),
            )

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=context
            ),
            agent_driver=Driver(),
            source_loader=SimpleNamespace(load=load_source),  # type: ignore[arg-type]
            journal=Journal(),
        )
    )
    transport = RuntimeTransportContext(
        mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = runtime.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        transport=transport,
        invocation_control=control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.begin()
    cast(Any, prepared.opaque_state).cell.execution_owner = object()
    events: list[object] = []

    class Sink:
        def emit(self, event: object) -> None:
            events.append(event)

    outcome = runtime.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=SseAgentExecutionHost(timeout_seconds=0.5, poll_seconds=0.005),
        cancel_check=lambda: False,
    )

    assert getattr(outcome, "message", None) == "done"
    assert [type(event) for event in events] == [
        MetaEvent,
        StatusEvent,
        ToolCallEvent,
        AssistantDeltaEvent,
        ToolResultEvent,
        AssistantMessageEvent,
        CompletedEvent,
    ]
    assert sources.calls == 1
    assert "context" in recorder_events


def test_slow_stream_drops_late_chained_pending_after_fallback_delivery() -> None:
    """A timed-out continuation must not leave a late Pending card behind."""

    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    deliveries: list[dict[str, object]] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        deliveries.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED)

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    entered = Event()
    release = Event()
    finished = Event()
    context = SimpleNamespace(operation_executor=None)

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(
            self,
            _messages: list[Message],
            current: PendingAction,
            _approved: bool,
            _auto_approve: bool,
            _max_iter: int,
            **kwargs: object,
        ) -> object:
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id=current.tool_call_id,
                spec=SimpleNamespace(name=current.tool_name),
                arguments_digest="digest",
            )
            authorization = kwargs["confirmation_attempt_sink"](current, prepared)
            record = kwargs["tool_context"].operation_executor(
                prepared, kwargs["tool_context"], authorization
            )
            origin = Message(role="tool", content="saved", tool_call_id=current.tool_call_id)
            kwargs["confirmation_result_sink"](current, True, origin, record)
            entered.set()
            assert release.wait(10)
            child = PendingAction(
                "late-child", "create_application", "{}", "late", str(uuid4())
            )
            try:
                return SimpleNamespace(
                    added=(origin,),
                    reply="",
                    pending=child,
                    records=(record,),
                    failures=(),
                )
            finally:
                finished.set()

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=context
            ),
            agent_driver=Driver(),
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )
    prepared = runtime.prepare_stream(
        request,
        transport=RuntimeTransportContext(
            mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.begin()
    cast(Any, prepared.opaque_state).cell.execution_owner = object()

    class Sink:
        def emit(self, _event: object) -> None:
            return None

    outcome = runtime.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=SseAgentExecutionHost(timeout_seconds=0.05, poll_seconds=0.005),
        cancel_check=lambda: False,
    )

    assert entered.is_set()
    assert getattr(outcome, "persisted", False) is True
    assert len(deliveries) == 1
    assert deliveries[0]["chained_pending"] is None
    release.set()
    assert finished.wait(10)


def test_stream_provider_failure_is_502_and_does_not_clear_pending() -> None:
    operations = _Operations(status="proposed")
    pending = PendingAction("call-1", "create_application", "{}", "create", operations.operation_id)
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))
    context = SimpleNamespace(operation_executor=None)

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("provider down")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=context
            ),
            agent_driver=Driver(),
        )
    )
    prepared = runtime.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        transport=RuntimeTransportContext(
            mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.begin()
    cast(Any, prepared.opaque_state).cell.execution_owner = object()

    class Sink:
        def emit(self, _event: object) -> None:
            return None

    class Host:
        def run(self, thunk: object, _control: object) -> object:
            return cast(Any, thunk)()

    outcome = runtime.execute_prepared_stream(
        prepared,
        event_sink=Sink(),
        signal_sink=None,
        execution_host=Host(),  # type: ignore[arg-type]
        cancel_check=lambda: False,
    )

    assert getattr(outcome, "code", None).value == "ai_provider_error"
    assert getattr(outcome, "status_code", None) == 502
    assert persistence.pending is not None


class _ConfirmationJournalRecorder:
    """Small healthy recorder used as the enabled Journal control case."""

    run_id = "run-confirmation"
    segment_id = "segment-confirmation"
    diagnostics: list[str] = []

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fingerprint_pending_identity(self, _value: object) -> str:
        self.calls.append("fingerprint_pending_identity")
        return "pending-fingerprint"

    def capture_context(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("capture_context")
        return "snapshot-confirmation"

    def append_event(self, event: object) -> None:
        self.calls.append(str(getattr(event, "event_type", "append_event")))

    def resume(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("resume")

    def suspend(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("suspend")

    def finish(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("finish")

    def abandon(self, *_args: object, **_kwargs: object) -> None:
        self.calls.append("abandon")


class _ConfirmationJournalFactory:
    def __init__(self, recorder: object) -> None:
        self.recorder = recorder
        self.resume_calls = 0

    def resume_waiting_run(self, *_args: object, **_kwargs: object) -> object:
        self.resume_calls += 1
        return self.recorder


class _DisabledConfirmationJournal(NullRunRecorderFactory):
    def __init__(self) -> None:
        super().__init__()
        self.resume_calls = 0

    def resume_waiting_run(
        self,
        conversation_id: int,
        waiting_tool_call_id: str,
        command: object,
    ) -> NullRunRecorder:
        self.resume_calls += 1
        return super().resume_waiting_run(
            conversation_id,
            waiting_tool_call_id,
            cast(Any, command),
        )


class _DegradedConfirmationJournalRecorder(_ConfirmationJournalRecorder):
    """Every recorder hook fails ordinarily; Runtime must fail open."""

    def _fail(self, name: str) -> None:
        self.calls.append(name)
        raise RuntimeError("journal recorder degraded")

    def fingerprint_pending_identity(self, _value: object) -> str:
        self._fail("fingerprint_pending_identity")
        raise AssertionError("unreachable")

    def capture_context(self, *_args: object, **_kwargs: object) -> str:
        self._fail("capture_context")
        raise AssertionError("unreachable")

    def append_event(self, event: object) -> None:
        self._fail(str(getattr(event, "event_type", "append_event")))

    def resume(self, *_args: object, **_kwargs: object) -> None:
        self._fail("resume")

    def suspend(self, *_args: object, **_kwargs: object) -> None:
        self._fail("suspend")

    def finish(self, *_args: object, **_kwargs: object) -> None:
        self._fail("finish")

    def abandon(self, *_args: object, **_kwargs: object) -> None:
        self._fail("abandon")


class _ConfirmationJournalBaseException(BaseException):
    pass


class _BaseExceptionConfirmationJournalRecorder(_ConfirmationJournalRecorder):
    def __init__(self, error: _ConfirmationJournalBaseException) -> None:
        super().__init__()
        self.error = error

    def capture_context(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("capture_context")
        raise self.error


def _confirmation_journal_for_mode(mode: str) -> tuple[object, object]:
    if mode == "enabled":
        recorder = _ConfirmationJournalRecorder()
        return _ConfirmationJournalFactory(recorder), recorder
    if mode == "disabled":
        journal = _DisabledConfirmationJournal()
        return journal, journal
    if mode == "degraded":
        recorder = _DegradedConfirmationJournalRecorder()
        return _ConfirmationJournalFactory(recorder), recorder
    if mode == "base_exception":
        recorder = _BaseExceptionConfirmationJournalRecorder(
            _ConfirmationJournalBaseException("journal base exception")
        )
        return _ConfirmationJournalFactory(recorder), recorder
    raise AssertionError(f"unsupported Journal mode: {mode}")


def _confirmation_outcome_signature(outcome: object) -> tuple[object, ...]:
    code = getattr(outcome, "code", None)
    if isinstance(outcome, RuntimeFailureOutcome):
        return (
            "failure",
            code.value if isinstance(code, RuntimeFailureCode) else str(code),
            getattr(outcome, "status_code", None),
            getattr(outcome, "retryable", None),
        )
    return (
        type(outcome).__name__,
        getattr(outcome, "message", None),
        getattr(outcome, "write_status", None),
        getattr(outcome, "persisted", None),
    )


def _run_confirmation_journal_case(
    mode: str,
    case: str,
) -> tuple[dict[str, object], object]:
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    deliveries: list[dict[str, object]] = []

    def persist_confirmation_delivery(**kwargs: object) -> PersistenceResult:
        deliveries.append(kwargs)
        return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=(1,))

    persistence.persist_confirmation_delivery = persist_confirmation_delivery  # type: ignore[attr-defined]
    write = _WriteCoordinator()
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, write))
    journal, recorder = _confirmation_journal_for_mode(mode)
    provider_calls = 0
    resolver_calls = 0
    context = SimpleNamespace(operation_executor=None)

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(
            self,
            _messages: list[Message],
            current: PendingAction,
            _approved: bool,
            _auto_approve: bool,
            _max_iter: int,
            **kwargs: object,
        ) -> object:
            nonlocal provider_calls
            provider_calls += 1
            if case == "timeout":
                raise RuntimeAgentTimedOut()
            prepared = SimpleNamespace(
                pending_identity="call-1:create_application",
                pending_action_revision=1,
                tool_call_id=current.tool_call_id,
                spec=SimpleNamespace(name=current.tool_name),
                arguments_digest="digest",
            )
            authorization = cast(Any, kwargs["confirmation_attempt_sink"])(
                current, prepared
            )
            record = cast(Any, kwargs["tool_context"]).operation_executor(
                prepared,
                kwargs["tool_context"],
                authorization,
            )
            origin = Message(
                role="tool",
                content="saved",
                tool_call_id=current.tool_call_id,
            )
            cast(Any, kwargs["confirmation_result_sink"])(
                current,
                True,
                origin,
                record,
            )
            return SimpleNamespace(
                added=(origin, Message(role="assistant", content="done")),
                reply="done",
                pending=None,
                records=(record,),
                failures=(),
            )

    def resolve(_request: object, _conversation: object) -> ResolvedModel:
        nonlocal resolver_calls
        resolver_calls += 1
        return ResolvedModel(model=object(), tool_context=context)

    dependencies = RuntimeDependencies(
        conversations=Conversations(),
        persistence=persistence,  # type: ignore[arg-type]
        confirmation_coordinator=coordinator,
        journal=cast(Any, journal),
        model_resolver=resolve if case != "reject" else None,
        agent_driver=Driver() if case != "reject" else None,
    )
    runtime = PilotRuntime(dependencies)
    request = ConfirmationRequest(
        conversation_id=7,
        approved=case != "reject",
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )
    outcome = runtime.continue_confirmation(
        request,
        invocation_control=InMemoryRuntimeInvocationControl(),
    )
    pending_snapshot = (
        persistence.pending.tool_call_id,
        persistence.pending.tool_name,
        persistence.pending.args,
        persistence.pending.operation_id is not None,
    ) if persistence.pending is not None else None
    snapshot = {
        "outcome": _confirmation_outcome_signature(outcome),
        "provider_calls": provider_calls,
        "resolver_calls": resolver_calls,
        "executor_calls": write.execute_calls,
        "reject_calls": write.reject_calls,
        "delivery_calls": len(deliveries),
        "pending": pending_snapshot,
        "ledger": (
            operations.operation.status,
            operations.operation.delivery_status,
        ),
        "pending_reads": persistence.pending_reads,
    }
    return snapshot, recorder


@pytest.mark.parametrize("case", ("approve", "reject", "timeout"))
def test_confirmation_journal_disabled_and_degraded_are_enabled_equivalent(
    case: str,
) -> None:
    enabled, enabled_recorder = _run_confirmation_journal_case("enabled", case)
    disabled, disabled_journal = _run_confirmation_journal_case("disabled", case)
    degraded, degraded_recorder = _run_confirmation_journal_case("degraded", case)

    assert disabled == enabled
    assert degraded == enabled
    assert cast(Any, disabled_journal).resume_calls == 1
    assert cast(Any, degraded_recorder).calls
    assert cast(Any, enabled_recorder).calls

    expected = {
        "approve": (1, 1, 1),
        "reject": (0, 0, 1),
        "timeout": (1, 0, 0),
    }[case]
    assert (
        enabled["provider_calls"],
        enabled["executor_calls"],
        enabled["delivery_calls"],
    ) == expected


def test_confirmation_journal_base_exception_is_propagated_unchanged() -> None:
    journal, recorder = _confirmation_journal_for_mode("base_exception")
    operations = _Operations(status="proposed")
    pending = PendingAction(
        "call-1", "create_application", "{}", "create", operations.operation_id
    )
    persistence = _Persistence(pending)
    persistence.persist_confirmation_delivery = lambda **_kwargs: PersistenceResult(  # type: ignore[attr-defined]
        PersistenceStatus.PERSISTED
    )
    coordinator = ConfirmationCoordinator(_deps(persistence, operations, _WriteCoordinator()))

    class Conversations:
        def load(self, _conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    class Driver:
        def resume_after_confirm(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Journal BaseException must abort before Agent")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            persistence=persistence,  # type: ignore[arg-type]
            confirmation_coordinator=coordinator,
            model_resolver=lambda _request, _conversation: ResolvedModel(
                model=object(), tool_context=SimpleNamespace(operation_executor=None)
            ),
            agent_driver=Driver(),
            journal=cast(Any, journal),
        )
    )
    error = cast(Any, recorder).error
    with pytest.raises(_ConfirmationJournalBaseException) as raised:
        runtime.continue_confirmation(
            ConfirmationRequest(
                conversation_id=7,
                approved=True,
                operation_id=operations.operation_id,
                confirmation_token=operations.token,
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )

    assert raised.value is error
    assert cast(Any, recorder).calls == ["capture_context"]
    assert persistence.pending is pending
    assert operations.operation.status == "proposed"


def test_atomic_timeout_delivery_keeps_concurrent_same_operation_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition_module.sqlalchemy_event, "listen", lambda *_args: None)

    class Repository:
        session_factory = object()

        def __init__(self) -> None:
            self.owner_calls = 0

        def prepare_owner(self, operation_id: str, generation: int = 1) -> object:
            self.owner_calls += 1
            return SimpleNamespace(operation_id=operation_id, generation=generation, call=self.owner_calls)

    class Chat:
        def __init__(self) -> None:
            self.resolved: list[object] = []

        def bind(self, _session: object) -> "Chat":
            return self

        def resolve_pending_confirmation(self, *args: object, **kwargs: object) -> object:
            self.resolved.append(kwargs["delivery_ownership"])
            return object()

    class Session:
        def get(self, _model: object, _operation_id: str) -> object:
            return SimpleNamespace(
                status="committed",
                undo_json=None,
                visible_result="saved",
            )

        def scalars(self, _statement: object) -> list[int]:
            return []

    repository = Repository()
    chat = Chat()
    delivery = composition_module._AtomicTimeoutDelivery(chat, repository)

    def state() -> SimpleNamespace:
        return SimpleNamespace(
            identity=SimpleNamespace(operation_id="same-operation", conversation_id=7),
            lock=RLock(),
            timed_out=True,
            active=True,
            confirmation_attempted=True,
            origin_tool_message=None,
            transactional_delivery_persisted=False,
            pending=SimpleNamespace(tool_call_id="tool-1"),
            claim_id="claim",
        )

    state_a = state()
    state_b = state()
    handle_a = delivery.register(state_a)
    owner_a = repository.prepare_owner("same-operation")
    handle_b = delivery.register(state_b)
    owner_b = repository.prepare_owner("same-operation")

    assert handle_a is not handle_b
    assert owner_a is not owner_b
    delivery.unregister(state_a, handle_a)

    delivery._before_commit(Session())

    assert chat.resolved == [owner_b]
    delivery.unregister(state_b, handle_b)


def test_atomic_timeout_delivery_restores_nested_registration_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(composition_module.sqlalchemy_event, "listen", lambda *_args: None)
    repository = SimpleNamespace(
        session_factory=object(),
        prepare_owner=lambda _operation_id, _generation=1: object(),
    )
    delivery = composition_module._AtomicTimeoutDelivery(object(), repository)
    state_a = object()
    state_b = object()
    handle_a = delivery.register(state_a)
    handle_b = delivery.register(state_b)
    try:
        delivery.unregister(state_b, handle_b)
        assert composition_module._ACTIVE_TIMEOUT_DELIVERY.get() == (delivery, handle_a)
    finally:
        delivery.unregister(state_b, handle_b)
        delivery.unregister(state_a, handle_a)
    assert composition_module._ACTIVE_TIMEOUT_DELIVERY.get() is None
