from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from offerpilot.ai.tool_runtime.legacy import LegacyDeterministicAdapter, LegacyDeterministicCatalog
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    TerminalPayload,
    ledger_fingerprint,
)
from offerpilot.pilot_runtime import (
    CompletionReason,
    DeterministicPilotAdapter,
    MessageOutcome,
    PreparationKind,
    PreparedStreamExecution,
    RuntimeFailureCode,
    RuntimeFailureOutcome,
    RuntimeTransportContext,
    StartTurnRequest,
    ConfirmationRequest,
    PilotActionDescriptor,
    StreamExecutionMode,
    freeze_json_mapping,
)
from offerpilot.chat_transport import PreparedStreamGuard
from offerpilot.pilot_runtime.deterministic import _confirmation_token
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies


class _Conversation:
    id = 7
    context_type = "application"
    context_ref = "11"
    mode = "general"
    archived_at = None


class _Applications:
    def get(self, application_id: int) -> object:
        return SimpleNamespace(id=application_id, company_name="Example", position_name="Backend")


class _JD:
    def get_current(self, _application_id: int) -> object | None:
        return None

    def get_version(self, _application_id: int, _version_id: int) -> object | None:
        return None

    def bind(self, _session: object) -> "_JD":
        return self


class _Persistence:
    def __init__(self) -> None:
        self.pending: object | None = None
        self.clarification: object | None = None
        self.message_ids = 0
        self.resolve_calls = 0
        self.resolve_result: object | None = object()

    def get_pending_action(self, _conversation_id: int) -> object | None:
        return self.pending

    def get_pending_clarification(self, _conversation_id: int) -> object | None:
        return self.clarification

    def persist_initial_pending(self, _conversation_id: int, messages: object, pending: object) -> object:
        self.pending = pending
        self.message_ids += len(tuple(messages))
        return SimpleNamespace(persisted=True, message_ids=(self.message_ids - 1, self.message_ids))

    def persist_clarification(self, _conversation_id: int, messages: object, pending: object, question: str) -> object:
        self.clarification = SimpleNamespace(pending=pending, question=question)
        self.message_ids += len(tuple(messages)) + 1
        return SimpleNamespace(persisted=True, message_ids=(self.message_ids - 1, self.message_ids))

    def persist_initial_user_message(self, _conversation_id: int, _content: str) -> object:
        self.message_ids += 1
        return SimpleNamespace(persisted=True, message_id=self.message_ids)

    def clear_pending_clarification(self, _conversation_id: int) -> object:
        self.clarification = None
        return SimpleNamespace(persisted=True)

    def persist_assistant_message(self, _conversation_id: int, _content: str) -> object:
        self.message_ids += 1
        return SimpleNamespace(persisted=True, message_id=self.message_ids)

    def resolve_pending_confirmation(self, _conversation_id: int, *_args: object, **_kwargs: object) -> object | None:
        self.resolve_calls += 1
        if self.resolve_result is None:
            return None
        self.pending = None
        return self.resolve_result


class _Operations:
    def __init__(self) -> None:
        self.key = SimpleNamespace(key_id="test", secret=b"k" * 32)
        self.operation: object | None = None

    def get(self, _operation_id: str) -> object | None:
        return self.operation


class _Coordinator:
    def __init__(self, operations: _Operations) -> None:
        self.operations = operations
        self.execute_calls = 0
        self.reject_calls = 0

    def execute_legacy(self, **kwargs: object) -> OperationCommitted:
        self.execute_calls += 1
        value = kwargs["executor"](object())
        payload = TerminalPayload(
            status="committed",
            result_contract="legacy_string_v1",
            result_json=json.dumps({"value": value}),
            visible_result=str(value),
            transport_json="{}",
            undo_json=None,
            failure_category=None,
            failure_code=None,
            digest="sha256:test",
        )
        return OperationCommitted(str(kwargs["operation_id"]), payload, None)

    def reject_primary(self, **kwargs: object) -> OperationFailed:
        self.reject_calls += 1
        payload = TerminalPayload(
            status="rejected",
            result_contract="rejection_json_v1",
            result_json="{}",
            visible_result=str(kwargs["visible_result"]),
            transport_json="{}",
            undo_json=None,
            failure_category=None,
            failure_code=None,
            digest="sha256:test",
        )
        return OperationFailed(str(kwargs["operation_id"]), payload, None)


def _catalog_factory(counter: list[int]) -> object:
    def factory(_jd: object, _outcomes: object) -> LegacyDeterministicCatalog:
        def execute(_args: str) -> str:
            counter[0] += 1
            return '{"ok":true}'

        adapters = tuple(
            LegacyDeterministicAdapter(
                name=name,
                editable_fields=(
                    ({"field": "jd_text", "type": "long_text"}, {"field": "source_url", "type": "string"})
                    if name == "save_application_jd_version"
                    else ()
                ),
                validate=lambda _args: "",
                describe=lambda _args: "确认 deterministic write",
                execute=execute,
            )
            for name in (
                "save_application_jd_version",
                "create_application_submission_snapshot",
                "record_application_outcome",
            )
        )
        return LegacyDeterministicCatalog(adapters)

    return factory


def _adapter(persistence: _Persistence, *, execute_counter: list[int] | None = None) -> tuple[DeterministicPilotAdapter, _Operations, _Coordinator]:
    operations = _Operations()
    coordinator = _Coordinator(operations)
    adapter = DeterministicPilotAdapter(
        persistence=persistence,
        applications=_Applications(),
        application_jd_versions=_JD(),
        application_outcomes=object(),
        write_operations=operations,
        write_coordinator=coordinator,
        legacy_catalog_factory=_catalog_factory(execute_counter or [0]),
        id_factory=lambda: "call-deterministic-jd-1",
        key_factory=lambda: "key-deterministic-jd-1",
    )
    return adapter, operations, coordinator


def test_initial_and_clarification_are_provider_free_and_typed() -> None:
    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    first = adapter.start_turn(StartTurnRequest(message="保存 JD"), _Conversation())
    assert first.preparation_kind is PreparationKind.DETERMINISTIC_INITIAL
    assert first.execution_mode is StreamExecutionMode.DIRECT
    assert first.events[0].__class__.__name__ == "MetaEvent"
    assert isinstance(first.outcome, MessageOutcome)
    assert first.outcome.message == "请粘贴完整岗位描述"
    assert coordinator.execute_calls == 0

    second = adapter.start_turn(
        StartTurnRequest(message="职位：后端工程师\n负责 API"),
        _Conversation(),
    )
    assert second.outcome.__class__.__name__ == "ConfirmationRequiredOutcome"
    assert persistence.pending is not None
    assert coordinator.execute_calls == 0


def test_stream_initial_is_direct_and_pending_replay_does_not_reexecute() -> None:
    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    execution = adapter.prepare_stream(
        StartTurnRequest(message="保存 JD：职位：后端工程师"),
        _Conversation(),
        transport=RuntimeTransportContext(mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"),
    )
    assert execution.execution_mode is StreamExecutionMode.DIRECT
    assert [type(event).__name__ for event in execution.events[:3]] == [
        "MetaEvent", "UserMessageSavedEvent", "StatusEvent"
    ]
    assert coordinator.execute_calls == 0

    replay = adapter.prepare_stream(
        StartTurnRequest(message="替换文本"),
        _Conversation(),
        transport=RuntimeTransportContext(mode="stream", transport_run_id=uuid4(), stream_version="pilot-sse-v1"),
    )
    assert replay.pending_replay is True
    assert coordinator.execute_calls == 0


def test_confirm_approve_edit_reject_and_cas_are_provider_free() -> None:
    persistence = _Persistence()
    execute_counter = [0]
    adapter, operations, coordinator = _adapter(persistence, execute_counter=execute_counter)
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    pending = persistence.pending
    assert pending is not None
    token = _confirmation_token(pending)
    operations.operation = SimpleNamespace(
        id=pending.operation_id,
        proposal_fingerprint="proposal",
        confirmation_token_fingerprint=ledger_fingerprint(
            operations.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        ),
    )
    approved = adapter.confirm(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            confirmation_token=token,
            edited_args=freeze_json_mapping({"jd_text": "修改后的岗位"}),
        ),
        _Conversation(),
    )
    assert approved.outcome.__class__.__name__ == "MessageOutcome"
    assert execute_counter[0] == 1
    assert coordinator.reject_calls == 0

    persistence.pending = pending
    rejected = adapter.confirm(
        ConfirmationRequest(conversation_id=7, approved=False, confirmation_token=token),
        _Conversation(),
    )
    assert rejected.outcome.__class__.__name__ == "MessageOutcome"
    assert coordinator.reject_calls == 1
    assert execute_counter[0] == 1

    persistence.pending = pending
    persistence.resolve_result = None
    stale = adapter.confirm(
        ConfirmationRequest(conversation_id=7, approved=True, confirmation_token=token),
        _Conversation(),
    )
    assert isinstance(stale.outcome, RuntimeFailureOutcome)
    assert stale.outcome.code is RuntimeFailureCode.STALE_PENDING_ACTION
    assert execute_counter[0] == 2


def test_bridge_rejects_model_surface_dependencies_and_unknown_client_actions() -> None:
    persistence = _Persistence()
    with pytest.raises(TypeError):
        DeterministicPilotAdapter(
            persistence=persistence,
            applications=_Applications(),
            application_jd_versions=_JD(),
            application_outcomes=object(),
            model_catalog=object(),
        )

    adapter, _operations, coordinator = _adapter(persistence)
    with pytest.raises(ValueError, match="unsupported pilot action"):
        adapter.start_turn(
            StartTurnRequest(
                message="保存岗位资料",
                pilot_action=PilotActionDescriptor(
                    kind="unknown_client_tool",
                ),
            ),
            _Conversation(),
        )
    assert coordinator.execute_calls == 0


def test_pending_non_legacy_action_is_not_overwritten_by_deterministic_route() -> None:
    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    persistence.pending = SimpleNamespace(
        tool_call_id="model-call",
        tool_name="update_application_status",
        args=json.dumps({"id": 11, "status": "offer"}),
        human="model proposal",
        operation_id="model-operation",
    )
    execution = adapter.start_turn(
        StartTurnRequest(message="保存 JD：新文本"),
        _Conversation(),
    )
    assert isinstance(execution.outcome, RuntimeFailureOutcome)
    assert execution.outcome.code is RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED
    assert coordinator.execute_calls == 0


def test_terminal_replay_is_ledger_first_and_does_not_read_pending_or_execute() -> None:
    class _NoPending(_Persistence):
        def get_pending_action(self, _conversation_id: int) -> object | None:
            raise AssertionError("terminal replay must not read Pending")

    class _TerminalOperations(_Operations):
        def __init__(self) -> None:
            super().__init__()
            self.replay_calls = 0
            self.operation_id = str(uuid4())
            self.operation = SimpleNamespace(
                id=self.operation_id,
                conversation_id=7,
                status="committed",
                tool_call_id="call-terminal-replay",
                tool_name="save_application_jd_version",
                proposal_fingerprint="proposal",
                confirmation_token_fingerprint="",
            )
            token = "terminal-token"
            self.operation.confirmation_token_fingerprint = ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                token.encode("ascii"),
            )
            self.replay_result = OperationReplay(
                self.operation_id,
                TerminalPayload(
                    status="committed",
                    result_contract="legacy_string_v1",
                    result_json=json.dumps({"ok": True}),
                    visible_result="saved",
                    transport_json="{}",
                    undo_json=None,
                    failure_category=None,
                    failure_code=None,
                    digest="sha256:replay",
                ),
                "completed",
                1,
                None,
                "final_response",
                "岗位资料已保存。",
            )

        def replay(self, _operation: object, _fingerprint: str) -> OperationReplay:
            self.replay_calls += 1
            return self.replay_result

    persistence = _NoPending()
    operations = _TerminalOperations()
    coordinator = _Coordinator(operations)
    adapter = DeterministicPilotAdapter(
        persistence=persistence,
        applications=_Applications(),
        application_jd_versions=_JD(),
        application_outcomes=object(),
        write_operations=operations,
        write_coordinator=coordinator,
        legacy_catalog_factory=_catalog_factory([0]),
    )

    execution = adapter.confirm(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token="terminal-token",
        ),
        _Conversation(),
    )

    assert execution.preparation_kind is PreparationKind.REPLAY
    assert execution.outcome.__class__.__name__ == "OperationReplayOutcome"
    assert operations.replay_calls == 1
    assert coordinator.execute_calls == 0
    assert coordinator.reject_calls == 0


def test_runtime_confirmation_stream_is_precomputed_and_never_enters_agent_host() -> None:
    class _Gateway:
        def create(self, _request: object) -> _Conversation:
            return _Conversation()

        def load(self, _conversation_id: int) -> _Conversation:
            return _Conversation()

    class _Host:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, _thunk: object, _control: object) -> object:
            self.calls += 1
            raise AssertionError("deterministic confirmation must not start an Agent host")

    persistence = _Persistence()
    adapter, operations, coordinator = _adapter(persistence)
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            persistence=persistence,
            deterministic=adapter,
            route_selector=lambda _request, _conversation: "model",
        )
    )
    sync_control = InMemoryRuntimeInvocationControl()
    runtime.start_turn(
        StartTurnRequest(
            message="保存 JD：岗位",
            context_type="application",
            context_ref="11",
        ),
        execution_host=_Host(),
        invocation_control=sync_control,
        cancel_check=lambda: False,
    )
    pending = persistence.pending
    assert pending is not None
    token = _confirmation_token(pending)
    operations.operation = SimpleNamespace(
        id=pending.operation_id,
        proposal_fingerprint="proposal",
        confirmation_token_fingerprint=ledger_fingerprint(
            operations.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        ),
    )

    host = _Host()
    stream_control = InMemoryRuntimeInvocationControl()
    prepared = runtime.prepare_stream(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            confirmation_token=token,
        ),
        transport=RuntimeTransportContext(
            mode="stream",
            transport_run_id=uuid4(),
            stream_version="pilot-sse-v1",
        ),
        invocation_control=stream_control,
    )
    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.execution_mode is StreamExecutionMode.DIRECT
    assert coordinator.execute_calls == 1
    assert host.calls == 0

    seen: list[str] = []

    class _Sink:
        def emit(self, event: object) -> None:
            seen.append(type(event).__name__)

    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    guard._execute = lambda: runtime.execute_prepared_stream(
        prepared,
        event_sink=_Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    result = guard.execute_once()
    assert isinstance(result, MessageOutcome)
    assert host.calls == 0
    assert seen[-1] == "CompletedEvent"
    assert guard.complete(CompletionReason.NORMAL) is True
