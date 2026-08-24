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
    VerifiedPendingReplay,
    ledger_fingerprint,
)
from offerpilot.pilot_runtime import (
    CompletedEvent,
    CompletionReason,
    ConfirmationRequiredEvent,
    ConfirmationRequiredOutcome,
    DeterministicPilotAdapter,
    ErrorEvent,
    ImmediateHttpOutcome,
    InvocationState,
    MessageOutcome,
    OperationReplayOutcome,
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
from offerpilot.ai.tool_specs.legacy import build_legacy_deterministic_catalog
from offerpilot.chat_transport import (
    PreparedStreamGuard,
    event_sse_payload,
    outcome_http_payload,
)
from offerpilot.pilot_runtime.deterministic import (
    _LEGACY_EDITABLE_FIELDS,
    _confirmation_token,
    _invoke as deterministic_invoke,
)
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies, _invoke


class _Conversation:
    id = 7
    context_type = "application"
    context_ref = "11"
    mode = "general"
    archived_at = None


class _Gateway:
    def create(self, _request: object) -> _Conversation:
        return _Conversation()

    def load(self, _conversation_id: int) -> _Conversation:
        return _Conversation()


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


class _ConflictCoordinator(_Coordinator):
    def __init__(self, operations: _Operations, failure_code: str) -> None:
        super().__init__(operations)
        self.failure_code = failure_code

    def execute_legacy(self, **kwargs: object) -> OperationFailed:
        self.execute_calls += 1
        payload = TerminalPayload(
            status="failed",
            result_contract="legacy_string_v1",
            result_json="{}",
            visible_result="岗位资料已发生变化。",
            transport_json="{}",
            undo_json=None,
            failure_category="conflict",
            failure_code=self.failure_code,
            digest="sha256:conflict",
        )
        return OperationFailed(str(kwargs["operation_id"]), payload, None)


class _ReplacePersistence(_Persistence):
    def __init__(self, replacement_result: object | None) -> None:
        super().__init__()
        self.replacement_result = replacement_result
        self.replace_calls = 0

    def replace_pending_confirmation(self, _conversation_id: int, _pending: object, replacement: object, *_args: object, **_kwargs: object) -> object | None:
        self.replace_calls += 1
        if self.replacement_result is not None:
            self.pending = replacement
        return self.replacement_result


def _operation_for_pending(operations: _Operations, pending: object, token: str) -> None:
    operations.operation = SimpleNamespace(
        id=pending.operation_id,
        proposal_fingerprint="proposal",
        confirmation_token_fingerprint=ledger_fingerprint(
            operations.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        ),
    )


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


class _JournalPersistence(_Persistence):
    def list_messages(self, _conversation_id: int) -> tuple[object, ...]:
        return tuple(SimpleNamespace(id=index + 1, role="assistant") for index in range(self.message_ids))


class _StrictRecorder:
    run_id = "deterministic-run"
    segment_id = "deterministic-segment"

    def __init__(self) -> None:
        self.events: list[object] = []
        self.suspends: list[object] = []

    def append_event(self, event: object) -> None:
        self.events.append(event)

    def capture_context(self, *_args: object, **_kwargs: object) -> None:
        return None

    def attach_input_message(self, _message_id: int) -> None:
        return None

    def fingerprint_pending_identity(self, _identity: object) -> str:
        return "pending-fingerprint"

    def suspend(self, command: object) -> None:
        self.suspends.append(command)

    def finish(self, _command: object) -> None:
        return None

    def abandon(self) -> None:
        return None


class _StrictJournal:
    def __init__(self) -> None:
        self.recorder = _StrictRecorder()
        self.resume_calls = 0

    def start_run(self, _command: object) -> _StrictRecorder:
        return self.recorder

    def resume_waiting_run(
        self,
        _conversation_id: int,
        _tool_call_id: str,
        _build_segment: object,
    ) -> _StrictRecorder:
        self.resume_calls += 1
        return self.recorder


def test_deterministic_initial_journal_suspends_closed_legacy_pending() -> None:
    persistence = _JournalPersistence()
    adapter, _operations, coordinator = _adapter(persistence)
    journal = _StrictJournal()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            persistence=persistence,
            deterministic=adapter,
            journal=journal,
        )
    )

    outcome = runtime.start_turn(
        StartTurnRequest(
            message="保存 JD：岗位",
            context_type="application",
            context_ref="11",
        ),
        execution_host=object(),  # deterministic route must not inspect the host
        invocation_control=InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )

    assert isinstance(outcome, ConfirmationRequiredOutcome)
    assert coordinator.execute_calls == 0
    assert len(journal.recorder.suspends) == 1
    assert journal.recorder.suspends[0].tool_name == "save_application_jd_version"


def test_deterministic_chained_journal_suspends_replacement_on_same_run() -> None:
    persistence = _JournalPersistence()
    adapter, _operations, _coordinator = _adapter(persistence)
    old_pending = SimpleNamespace(
        tool_call_id="old-call",
        tool_name="save_application_jd_version",
        args='{"application_id":11,"jd_text":"old","idempotency_key":"key-old"}',
        human="old",
        operation_id="old-operation",
    )
    new_pending = SimpleNamespace(
        tool_call_id="new-call",
        tool_name="save_application_jd_version",
        args='{"application_id":11,"jd_text":"new","idempotency_key":"key-new"}',
        human="new",
        operation_id="new-operation",
    )
    persistence.pending = old_pending
    original = adapter.pending_action(_Conversation())
    assert original is not None
    journal = _StrictJournal()
    runtime = PilotRuntime(RuntimeDependencies(persistence=persistence, deterministic=adapter))
    control = InMemoryRuntimeInvocationControl()

    persistence.pending = new_pending
    runtime._finish_deterministic_confirmation_journal(  # type: ignore[attr-defined]
        {"recorder": journal.recorder, "started": True},
        original,
        _Conversation(),
        MessageOutcome("继续确认", conversation_id=7),
        control,
    )

    assert len(journal.recorder.suspends) == 1
    assert journal.recorder.suspends[0].tool_call_id == "new-call"


@pytest.mark.parametrize("route_value", ("unknown", "deterministic-default", "confirmation"))
def test_sync_unknown_route_fails_closed_before_model_side_effects(route_value: str) -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("unknown route must not resolve a model")

    provider = _NoProvider()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            model_resolver=provider,
            route_selector=lambda _request, _conversation: route_value,
        )
    )
    control = InMemoryRuntimeInvocationControl()

    outcome = runtime.start_turn(
        StartTurnRequest(message="普通消息", conversation_id=7),
        execution_host=object(),
        invocation_control=control,
        cancel_check=lambda: False,
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert outcome.status_code == 503
    assert provider.calls == 0
    assert control.state is InvocationState.COMPLETED


@pytest.mark.parametrize("route_value", ("unknown", "deterministic-default", "confirmation"))
def test_stream_unknown_route_matches_sync_fail_closed_boundary(route_value: str) -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("unknown route must not resolve a model")

    provider = _NoProvider()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            model_resolver=provider,
            route_selector=lambda _request, _conversation: route_value,
        )
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = runtime.prepare_stream(
        StartTurnRequest(message="普通消息", conversation_id=7),
        transport=RuntimeTransportContext(
            mode="stream",
            transport_run_id=uuid4(),
            stream_version="pilot-sse-v1",
        ),
        invocation_control=control,
    )

    assert isinstance(prepared, ImmediateHttpOutcome)
    assert prepared.status_code == 503
    assert prepared.payload["error_code"] == RuntimeFailureCode.OPERATION_UNAVAILABLE.value
    assert provider.calls == 0
    assert control.state is InvocationState.COMPLETED


def test_route_selector_exception_is_typed_and_terminal_in_both_transports() -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("route failure must not resolve a model")

    def broken_selector(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("route selector failed")

    provider = _NoProvider()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            model_resolver=provider,
            route_selector=broken_selector,
        )
    )
    sync_control = InMemoryRuntimeInvocationControl()
    sync_outcome = runtime.start_turn(
        StartTurnRequest(message="普通消息", conversation_id=7),
        execution_host=object(),
        invocation_control=sync_control,
        cancel_check=lambda: False,
    )
    stream_control = InMemoryRuntimeInvocationControl()
    stream_outcome = runtime.prepare_stream(
        StartTurnRequest(message="普通消息", conversation_id=7),
        transport=RuntimeTransportContext(
            mode="stream",
            transport_run_id=uuid4(),
            stream_version="pilot-sse-v1",
        ),
        invocation_control=stream_control,
    )

    assert isinstance(sync_outcome, RuntimeFailureOutcome)
    assert sync_outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert sync_outcome.status_code == 503
    assert isinstance(stream_outcome, ImmediateHttpOutcome)
    assert stream_outcome.status_code == 503
    assert stream_outcome.payload["error_code"] == RuntimeFailureCode.OPERATION_UNAVAILABLE.value
    assert provider.calls == 0
    assert sync_control.state is InvocationState.COMPLETED
    assert stream_control.state is InvocationState.COMPLETED


def test_deterministic_success_keeps_closed_replayed_false_projection() -> None:
    deterministic = MessageOutcome(
        "岗位资料已保存。",
        conversation_id=7,
        operation_id="operation-1",
        write_status="success",
        legacy_projection=True,
    )
    model = MessageOutcome("普通回复", conversation_id=7)
    replay = OperationReplayOutcome("operation-1", conversation_id=7, message="岗位资料已保存。")

    assert outcome_http_payload(deterministic) == {
        "type": "message",
        "message": "岗位资料已保存。",
        "conversation_id": 7,
        "write_status": "success",
        "operation_id": "operation-1",
        "replayed": False,
    }
    assert outcome_http_payload(model) == {
        "type": "message",
        "message": "普通回复",
        "conversation_id": 7,
    }
    assert outcome_http_payload(replay) == {
        "type": "message",
        "operation_id": "operation-1",
        "message": "岗位资料已保存。",
        "replayed": True,
        "conversation_id": 7,
    }
    assert event_sse_payload(CompletedEvent(response=deterministic)) == {
        "persisted": True,
        "response": {
            "type": "message",
            "message": "岗位资料已保存。",
            "conversation_id": 7,
            "write_status": "success",
            "operation_id": "operation-1",
            "replayed": False,
        },
    }
    assert event_sse_payload(CompletedEvent(response=model)) == {
        "persisted": True,
        "response": {
            "type": "message",
            "message": "普通回复",
            "conversation_id": 7,
        },
    }


def test_confirmation_feedback_is_not_in_repr_but_presence_is_retained() -> None:
    request = ConfirmationRequest(
        conversation_id=7,
        approved=False,
        rejection_feedback="secret feedback",
        rejection_feedback_present=True,
    )

    assert request.rejection_feedback_present is True
    assert "secret feedback" not in repr(request)


def test_legacy_editable_projection_matches_closed_source_catalog() -> None:
    catalog = build_legacy_deterministic_catalog(object(), object())
    for name, expected in _LEGACY_EDITABLE_FIELDS.items():
        adapter = catalog.resolve_server_loaded(SimpleNamespace(tool_name=name))
        assert adapter is not None
        assert tuple(dict(item) for item in adapter.editable_fields) == expected


def test_invoke_does_not_retry_a_body_type_error() -> None:
    calls = 0

    def body_failure(*, value: object) -> object:
        nonlocal calls
        calls += 1
        raise TypeError("body failure")

    with pytest.raises(TypeError, match="body failure"):
        _invoke(body_failure, {"value": "payload"}, ("fallback",))
    assert calls == 1


@pytest.mark.parametrize("error", (TypeError("body type error"), RuntimeError("body failure")))
def test_deterministic_invoke_does_not_retry_varargs_body_exception(error: Exception) -> None:
    calls = 0
    secondary_effects: list[object] = []

    def body_failure(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        secondary_effects.append((args, kwargs))
        raise error

    with pytest.raises(type(error), match=str(error)):
        deterministic_invoke(body_failure, {"request": "payload"}, ("fallback",))

    assert calls == 1
    assert len(secondary_effects) == 1


def test_deterministic_invoke_forwards_all_named_to_var_kwargs() -> None:
    calls = 0
    received: dict[str, object] = {}
    pending = object()
    named = {
        "operation_id": "operation-1",
        "claim_id": "claim-1",
        "pending": pending,
    }

    def sink(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        received.update(kwargs)
        return "ok"

    assert deterministic_invoke(sink, named, ("unused fallback",)) == "ok"
    assert calls == 1
    assert received == named


def test_deterministic_invoke_merges_explicit_and_var_kwargs_without_duplicates() -> None:
    calls = 0
    received: dict[str, object] = {}
    pending = object()
    named = {
        "operation_id": "operation-1",
        "claim_id": "claim-1",
        "pending": pending,
    }

    def sink(operation_id: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        received["operation_id"] = operation_id
        received.update(kwargs)
        return "ok"

    assert deterministic_invoke(sink, named, ("unused fallback",)) == "ok"
    assert calls == 1
    assert received == named


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


@pytest.mark.parametrize(
    ("message", "outcome_type"),
    (("职位：后端工程师\n负责 API", ConfirmationRequiredOutcome), ("取消", MessageOutcome)),
)
def test_runtime_clarification_text_and_cancel_use_deterministic_sync_route(
    message: str,
    outcome_type: type[object],
) -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("pending clarification must not resolve a model")

    class _Host:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("deterministic clarification must not enter an Agent host")

    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    adapter.start_turn(StartTurnRequest(message="保存 JD"), _Conversation())
    provider = _NoProvider()
    host = _Host()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            persistence=persistence,
            deterministic=adapter,
            model_resolver=provider,
            route_selector=lambda _request, _conversation: "model",
        )
    )

    outcome = runtime.start_turn(
        StartTurnRequest(message=message, conversation_id=7),
        execution_host=host,
        invocation_control=InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )

    assert isinstance(outcome, outcome_type)
    assert provider.calls == 0
    assert host.calls == 0
    assert coordinator.execute_calls == 0


@pytest.mark.parametrize(
    ("message", "outcome_type"),
    (("职位：后端工程师\n负责 API", ConfirmationRequiredOutcome), ("取消", MessageOutcome)),
)
def test_runtime_clarification_text_and_cancel_use_deterministic_stream_route(
    message: str,
    outcome_type: type[object],
) -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("pending clarification must not resolve a model")

    class _Host:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("deterministic clarification must not enter an Agent host")

    class _Sink:
        def emit(self, _event: object) -> None:
            return None

    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    adapter.start_turn(StartTurnRequest(message="保存 JD"), _Conversation())
    provider = _NoProvider()
    host = _Host()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            persistence=persistence,
            deterministic=adapter,
            model_resolver=provider,
            route_selector=lambda _request, _conversation: "model",
        )
    )
    control = InMemoryRuntimeInvocationControl()
    prepared = runtime.prepare_stream(
        StartTurnRequest(message=message, conversation_id=7),
        transport=RuntimeTransportContext(
            mode="stream",
            transport_run_id=uuid4(),
            stream_version="pilot-sse-v1",
        ),
        invocation_control=control,
    )

    assert isinstance(prepared, PreparedStreamExecution)
    assert prepared.execution_mode is StreamExecutionMode.DIRECT
    assert provider.calls == 0
    assert coordinator.execute_calls == 0

    guard = PreparedStreamGuard(prepared=prepared)
    assert guard.begin_execution() is True
    guard._execute = lambda: runtime.execute_prepared_stream(
        prepared,
        event_sink=_Sink(),
        signal_sink=None,
        execution_host=host,
        cancel_check=lambda: False,
    )
    outcome = guard.execute_once()
    assert isinstance(outcome, outcome_type)
    assert host.calls == 0
    assert provider.calls == 0
    assert coordinator.execute_calls == 0


def test_deterministic_confirmation_shape_keeps_pending_nested_only() -> None:
    persistence = _Persistence()
    adapter, _operations, _coordinator = _adapter(persistence)
    execution = adapter.start_turn(
        StartTurnRequest(message="保存 JD：岗位"),
        _Conversation(),
    )
    assert isinstance(execution.outcome, ConfirmationRequiredOutcome)
    payload = outcome_http_payload(execution.outcome)
    assert set(payload) == {"type", "conversation_id", "pending_action"}
    assert "operation_id" not in payload
    assert "message" not in payload
    assert "replayed" not in payload
    confirmation_event = execution.events[-1]
    assert isinstance(confirmation_event, ConfirmationRequiredEvent)
    assert event_sse_payload(confirmation_event) == {
        "pending_action": payload["pending_action"],
    }


def test_chained_pending_replay_keeps_only_baseline_replay_metadata() -> None:
    class _ChainedOperations(_Operations):
        def __init__(self) -> None:
            super().__init__()
            self.operation_id = str(uuid4())
            self.operation = SimpleNamespace(
                id=self.operation_id,
                conversation_id=7,
                status="committed",
                tool_call_id="call-replayed",
                tool_name="save_application_jd_version",
                proposal_fingerprint="proposal",
                confirmation_token_fingerprint=ledger_fingerprint(
                    self.key,
                    "write-operation-confirmation-token-v1",
                    b"terminal-token",
                ),
            )
            self.chained_pending: VerifiedPendingReplay | None = None

        def replay(self, _operation: object, _fingerprint: str) -> OperationReplay:
            return OperationReplay(
                self.operation_id,
                TerminalPayload(
                    status="committed",
                    result_contract="legacy_string_v1",
                    result_json="{}",
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
                "chained_pending",
                "",
                chained_pending=self.chained_pending,
            )

    persistence = _Persistence()
    operations = _ChainedOperations()
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
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    child = persistence.pending
    assert child is not None
    operations.chained_pending = VerifiedPendingReplay(
        adapter_kind="legacy_deterministic",
        conversation_id=7,
        operation_id=child.operation_id,
        tool_call_id=child.tool_call_id,
        tool_name=child.tool_name,
        raw_args=child.args,
        human=child.human,
        confirmation_token_fingerprint=ledger_fingerprint(
            operations.key,
            "write-operation-confirmation-token-v1",
            _confirmation_token(child).encode("ascii"),
        ),
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
    assert isinstance(execution.outcome, ConfirmationRequiredOutcome)
    payload = outcome_http_payload(execution.outcome)
    assert set(payload) == {
        "type",
        "conversation_id",
        "pending_action",
        "operation_id",
        "replayed",
    }
    assert payload["operation_id"] == operations.operation_id
    assert payload["replayed"] is True
    assert isinstance(execution.events[-1], ConfirmationRequiredEvent)
    assert event_sse_payload(execution.events[-1]) == {
        "pending_action": payload["pending_action"],
    }


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
    replay_payload = outcome_http_payload(replay.outcome)
    assert set(replay_payload) == {"type", "conversation_id", "pending_action"}
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


@pytest.mark.parametrize("replacement_result", [object(), None])
def test_stale_cas_uses_typed_pending_projection_and_never_resolves_twice(
    replacement_result: object | None,
) -> None:
    persistence = _ReplacePersistence(replacement_result)
    operations = _Operations()
    coordinator = _ConflictCoordinator(operations, "application_jd_stale_current_version")
    adapter = DeterministicPilotAdapter(
        persistence=persistence,
        applications=_Applications(),
        application_jd_versions=_JD(),
        application_outcomes=object(),
        write_operations=operations,
        write_coordinator=coordinator,
        legacy_catalog_factory=_catalog_factory([0]),
        id_factory=lambda: "call-stale-cas-1",
        key_factory=lambda: "key-stale-cas-0001",
    )
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    pending = persistence.pending
    assert pending is not None
    token = _confirmation_token(pending)
    _operation_for_pending(operations, pending, token)

    execution = adapter.confirm(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            confirmation_token=token,
        ),
        _Conversation(),
    )
    assert isinstance(execution.outcome, RuntimeFailureOutcome)
    assert execution.outcome.code is (
        RuntimeFailureCode.APPLICATION_JD_STALE_CURRENT_VERSION
        if replacement_result is not None
        else RuntimeFailureCode.STALE_PENDING_ACTION
    )
    assert persistence.replace_calls == 1
    assert persistence.resolve_calls == 0

    if replacement_result is not None:
        pending_action = execution.outcome.pending_action
        assert pending_action is not None
        expected_pending = event_sse_payload(
            ErrorEvent(
                execution.outcome.code,
                execution.outcome.message,
                execution.outcome.retryable,
                execution.outcome.degraded,
                pending_action=pending_action,
            )
        )["pending_action"]
        expected = {
            "error": "当前岗位资料已变化，请重新确认保存。",
            "error_code": "application_jd_stale_current_version",
            "pending_action": expected_pending,
        }
        assert outcome_http_payload(execution.outcome) == expected
        assert event_sse_payload(
            ErrorEvent(
                execution.outcome.code,
                execution.outcome.message,
                execution.outcome.retryable,
                execution.outcome.degraded,
                pending_action=pending_action,
            )
        ) == {
            "code": "application_jd_stale_current_version",
            "message": "当前岗位资料已变化，请重新确认保存。",
            "retryable": False,
            "degraded": False,
            "pending_action": expected_pending,
        }
        persistence.pending = pending
        runtime = PilotRuntime(
            RuntimeDependencies(
                conversations=_Gateway(),
                persistence=persistence,
                deterministic=adapter,
            )
        )
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
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(prepared, ImmediateHttpOutcome)
        stream_payload = outcome_http_payload(prepared)
        assert set(stream_payload) == {"error", "error_code", "pending_action"}
        assert stream_payload["error"] == expected["error"]
        assert stream_payload["error_code"] == expected["error_code"]
        assert isinstance(stream_payload["pending_action"], dict)
    else:
        assert execution.outcome.pending_action is None


def test_confirmation_presence_compatibility_rejects_wrong_fields_before_ledger() -> None:
    persistence = _Persistence()
    adapter, operations, coordinator = _adapter(persistence)
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    pending = persistence.pending
    assert pending is not None
    token = _confirmation_token(pending)
    _operation_for_pending(operations, pending, token)

    rejected_with_edit = adapter.confirm(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            confirmation_token=token,
            edited_args=freeze_json_mapping({}),
        ),
        _Conversation(),
    )
    rejected_with_feedback = adapter.confirm(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            confirmation_token=token,
            rejection_feedback="",
            rejection_feedback_present=True,
        ),
        _Conversation(),
    )
    for execution in (rejected_with_edit, rejected_with_feedback):
        assert isinstance(execution.outcome, RuntimeFailureOutcome)
        assert execution.outcome.code is RuntimeFailureCode.INVALID_CONFIRMATION
        assert execution.outcome.status_code == 422
    assert coordinator.execute_calls == 0
    assert coordinator.reject_calls == 0


def test_confirmation_fingerprint_preserves_presence_and_complete_feedback() -> None:
    persistence = _Persistence()
    adapter, operations, _coordinator = _adapter(persistence)
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    pending = persistence.pending
    assert pending is not None
    token = _confirmation_token(pending)
    _operation_for_pending(operations, pending, token)

    missing = adapter._request_fingerprint(
        pending,
        ConfirmationRequest(conversation_id=7, approved=False, confirmation_token=token),
        token,
    )
    explicit_empty = adapter._request_fingerprint(
        pending,
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            confirmation_token=token,
            rejection_feedback="",
            rejection_feedback_present=True,
        ),
        token,
    )
    first_feedback = adapter._request_fingerprint(
        pending,
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            confirmation_token=token,
            rejection_feedback="keep this",
            rejection_feedback_present=True,
        ),
        token,
    )
    second_feedback = adapter._request_fingerprint(
        pending,
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            confirmation_token=token,
            rejection_feedback="keep that",
            rejection_feedback_present=True,
        ),
        token,
    )
    assert missing != explicit_empty
    assert first_feedback != second_feedback


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


def test_malformed_pilot_action_descriptor_json_is_rejected_before_context_creation() -> None:
    class _CountingGateway(_Gateway):
        def __init__(self) -> None:
            self.create_calls = 0

        def create(self, request: object) -> _Conversation:
            self.create_calls += 1
            return super().create(request)

    class _Host:
        def run(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("malformed action must not start an Agent host")

    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    gateway = _CountingGateway()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=gateway,
            persistence=persistence,
            deterministic=adapter,
            route_selector=lambda _request, _conversation: "deterministic",
        )
    )
    outcome = runtime.start_turn(
        StartTurnRequest(
            message="保存岗位资料",
            context_type="application",
            context_ref="11",
            pilot_action=PilotActionDescriptor(
                kind="application_jd_save",
                value='{"type":"application_jd_save",',
            ),
        ),
        execution_host=_Host(),
        invocation_control=InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )
    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.INVALID_CONFIRMATION
    assert outcome.status_code == 422
    assert gateway.create_calls == 0
    assert persistence.message_ids == 0
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


def test_runtime_pending_guard_precedes_trusted_route_and_does_not_replay() -> None:
    class _NoProvider:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("pending guard must run before provider resolution")

    class _Host:
        def run(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("pending guard must not start an Agent host")

    persistence = _Persistence()
    adapter, _operations, coordinator = _adapter(persistence)
    adapter.start_turn(StartTurnRequest(message="保存 JD：岗位"), _Conversation())
    before_messages = persistence.message_ids
    provider = _NoProvider()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=_Gateway(),
            persistence=persistence,
            deterministic=adapter,
            model_resolver=provider,
            route_selector=lambda _request, _conversation: "model",
        )
    )
    outcome = runtime.start_turn(
        StartTurnRequest(message="普通新消息", conversation_id=7),
        execution_host=_Host(),
        invocation_control=InMemoryRuntimeInvocationControl(),
        cancel_check=lambda: False,
    )
    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED
    assert outcome.status_code == 409
    assert persistence.message_ids == before_messages
    assert provider.calls == 0
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
