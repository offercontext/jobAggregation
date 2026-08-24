from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.write_operations import (
    LedgerOperationPreheader,
    LedgerPendingPointer,
    OperationReplay,
    TerminalPayload,
    VerifiedPendingReplay,
    WriteOperationError,
    ledger_fingerprint,
)
from offerpilot.pilot_runtime import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
    _confirmation_token,
)
from offerpilot.pilot_runtime.contracts import (
    ConfirmationRequest,
    ConfirmationRequiredOutcome,
    ImmediateHttpOutcome,
    OperationReplayOutcome,
    PreparationKind,
    PreparedStreamExecution,
    RuntimeTransportContext,
    RuntimeFailureOutcome,
)
from offerpilot.pilot_runtime.errors import RuntimeCancelled
from offerpilot.pilot_runtime.errors import RuntimeFailureCode
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies
from offerpilot.pilot_runtime.deterministic import (
    DeterministicDependencies,
    DeterministicPilotAdapter,
)


class _LegacyTerminalOperations:
    def __init__(
        self,
        *,
        chained: bool,
        status: str = "committed",
        adapter_kind: str = "legacy_deterministic",
        tool_name: str = "save_application_jd_version",
        pointer_tool_name: str | None = None,
    ) -> None:
        self.operation_id = str(uuid4())
        self.key = SimpleNamespace(key_id="test", secret=b"k" * 32)
        self.token = "t" * 64
        self.preheader_calls = 0
        self.get_calls = 0
        self.replay_calls = 0
        self.pointer_tool_name = pointer_tool_name or tool_name
        self.operation = SimpleNamespace(
            id=self.operation_id,
            conversation_id=7,
            status=status,
            adapter_kind=adapter_kind,
            tool_call_id="legacy-call",
            tool_name=tool_name,
            proposal_fingerprint="proposal",
            confirmation_token_fingerprint=ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                self.token.encode("ascii"),
            ),
        )
        child = PendingAction(
            "child-call",
            tool_name,
            '{"application_id":1,"jd_text":"child"}',
            "请确认将这份岗位资料保存到当前投递。",
            str(uuid4()),
        )
        self.chained_pending = (
            VerifiedPendingReplay(
                adapter_kind="legacy_deterministic",
                conversation_id=7,
                operation_id=child.operation_id,
                tool_call_id=child.tool_call_id,
                tool_name=child.tool_name,
                raw_args=child.args,
                human=child.human,
                confirmation_token_fingerprint=ledger_fingerprint(
                    self.key,
                    "write-operation-confirmation-token-v1",
                    _confirmation_token(child).encode("ascii"),
                ),
                decoded_args=json.loads(child.args),
            )
            if chained
            else None
        )

    def get(self, _operation_id: str) -> object:
        self.get_calls += 1
        return self.operation

    def operation_preheader(
        self, *, conversation_id: int, operation_id: str | None
    ) -> LedgerOperationPreheader:
        self.preheader_calls += 1
        assert self.preheader_calls == 1, "route classification must use one bounded preheader"
        assert conversation_id == 7
        assert operation_id == self.operation_id
        return LedgerOperationPreheader(
            self.operation,
            LedgerPendingPointer(
                conversation_id=7,
                operation_id=self.operation_id,
                tool_call_id="legacy-call",
                tool_name=self.pointer_tool_name,
                pending_confirmation_claim_id="",
            ),
        )

    def replay(self, _operation: object, _request_fingerprint: str) -> OperationReplay:
        self.replay_calls += 1
        return OperationReplay(
            self.operation_id,
            TerminalPayload(
                status="committed",
                result_contract="legacy_string_v1",
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
            "chained_pending" if self.chained_pending is not None else "final_response",
            "saved",
            chained_pending=self.chained_pending,
        )


class _ConversationBodyReadSpy:
    def __init__(self) -> None:
        self.calls = 0

    def load(self, _conversation_id: int) -> object:
        self.calls += 1
        raise AssertionError("terminal Legacy replay must not read Conversation body")


def _deterministic_adapter(operations: _LegacyTerminalOperations) -> DeterministicPilotAdapter:
    return DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, SimpleNamespace()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
        )
    )


@pytest.mark.parametrize("mode", ("sync", "stream"))
@pytest.mark.parametrize("chained", (False, True), ids=("terminal", "chained"))
def test_legacy_replay_never_loads_conversation_body_before_ledger(
    mode: str,
    chained: bool,
) -> None:
    operations = _LegacyTerminalOperations(chained=chained)
    conversations = _ConversationBodyReadSpy()
    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(write_operations=cast(Any, operations))
    )
    deterministic = _deterministic_adapter(operations)
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=deterministic,
            confirmation_coordinator=coordinator,
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )
    control = InMemoryRuntimeInvocationControl()

    if mode == "sync":
        outcome = runtime.continue_confirmation(
            request,
            transport=RuntimeTransportContext(mode="sync"),
            invocation_control=control,
        )
        assert isinstance(
            outcome,
            ConfirmationRequiredOutcome if chained else OperationReplayOutcome,
        )
    else:
        outcome = runtime.prepare_stream(
            request,
            transport=RuntimeTransportContext(
                mode="stream",
                transport_run_id=uuid4(),
                stream_version="pilot-sse-v1",
            ),
            invocation_control=control,
        )
        assert isinstance(outcome, PreparedStreamExecution)
        assert outcome.preparation_kind is PreparationKind.REPLAY

    assert conversations.calls == 0
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 1


def test_sync_cancel_stops_before_legacy_route_ledger_or_conversation() -> None:
    operations = _LegacyTerminalOperations(chained=False)
    conversations = _ConversationBodyReadSpy()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=_deterministic_adapter(operations),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    )

    with pytest.raises(RuntimeCancelled):
        runtime.continue_confirmation(
            ConfirmationRequest(
                conversation_id=7,
                approved=True,
                operation_id=operations.operation_id,
                confirmation_token=operations.token,
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
            cancel_check=lambda: True,
        )

    assert operations.preheader_calls == 0
    assert operations.get_calls == 0
    assert operations.replay_calls == 0
    assert conversations.calls == 0


def test_invalid_confirmation_stops_before_legacy_route_preheader() -> None:
    operations = _LegacyTerminalOperations(chained=False)

    class Validator:
        @staticmethod
        def validate(_request: object) -> None:
            raise ValueError("invalid")

    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, _ConversationBodyReadSpy()),
            deterministic=_deterministic_adapter(operations),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
            validator=Validator(),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.status_code == 422
    assert operations.preheader_calls == 0
    assert operations.get_calls == 0
    assert operations.replay_calls == 0


@pytest.mark.parametrize("mode", ("sync", "stream"))
def test_missing_legacy_adapter_fails_closed_before_replay_or_conversation(mode: str) -> None:
    operations = _LegacyTerminalOperations(chained=False)
    conversations = _ConversationBodyReadSpy()
    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )

    if mode == "sync":
        outcome = runtime.continue_confirmation(
            request,
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(outcome, RuntimeFailureOutcome)
        assert outcome.status_code == 400
    else:
        outcome = runtime.prepare_stream(
            request,
            transport=RuntimeTransportContext(
                mode="stream",
                transport_run_id=uuid4(),
                stream_version="pilot-sse-v1",
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(outcome, ImmediateHttpOutcome)
        assert outcome.status_code == 400

    assert conversations.calls == 0
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 0


def test_live_legacy_preflight_falls_through_to_old_deterministic_confirmation() -> None:
    operations = _LegacyTerminalOperations(chained=False, status="proposed")
    pending = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        '{"application_id":1,"jd_text":"live"}',
        "请确认将这份岗位资料保存到当前投递。",
        operations.operation_id,
    )
    calls = {"conversation": 0}

    class Persistence:
        @staticmethod
        def get_pending_action(_conversation_id: int) -> PendingAction:
            return pending

    adapter = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, Persistence()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
        )
    )

    class Conversations:
        @staticmethod
        def load(_conversation_id: int) -> object:
            calls["conversation"] += 1
            return SimpleNamespace(id=7, archived_at=None)

    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            deterministic=adapter,
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=_confirmation_token(pending),
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert calls == {"conversation": 1}
    assert operations.preheader_calls == 1
    assert operations.get_calls == 1
    assert operations.replay_calls == 0


def test_typed_and_reject_do_not_enter_legacy_preflight_route() -> None:
    typed = _LegacyTerminalOperations(
        chained=False,
        adapter_kind="typed",
    )
    typed_request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=typed.operation_id,
        confirmation_token=typed.token,
    )
    typed_result = _deterministic_adapter(typed).preflight_confirmation(
        typed_request,
        preheader=typed.operation_preheader(
            conversation_id=7,
            operation_id=typed.operation_id,
        ),
    )
    assert typed_result.matched is False
    assert typed_result.execution is None
    assert typed.preheader_calls == 1
    assert typed.replay_calls == 0

    rejected = _LegacyTerminalOperations(chained=False)
    rejected_result = _deterministic_adapter(rejected).preflight_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=False,
            operation_id=rejected.operation_id,
            confirmation_token=rejected.token,
        )
    )
    assert rejected_result.matched is False
    assert rejected.preheader_calls == 0
    assert rejected.replay_calls == 0


@pytest.mark.parametrize(
    ("operations", "expected_code", "expected_message", "expected_retryable"),
    (
        (
            _LegacyTerminalOperations(
                chained=False,
                status="proposed",
                tool_name="not_a_legacy_tool",
            ),
            RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            "无法确认写入结果，请保留原请求后重试。",
            False,
        ),
        (
            _LegacyTerminalOperations(
                chained=False,
                status="proposed",
                pointer_tool_name="record_application_outcome",
            ),
            RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
            "对话结果暂时无法保存。",
            True,
        ),
    ),
    ids=("invalid-legacy-name", "pointer-mismatch"),
)
def test_invalid_legacy_route_identity_fails_before_conversation(
    operations: _LegacyTerminalOperations,
    expected_code: RuntimeFailureCode,
    expected_message: str,
    expected_retryable: bool,
) -> None:
    conversations = _ConversationBodyReadSpy()
    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=_deterministic_adapter(operations),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is expected_code
    assert outcome.message == expected_message
    assert outcome.status_code == 409
    assert outcome.retryable is expected_retryable
    assert conversations.calls == 0
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 0


def test_preheader_error_is_authoritative_and_never_retried() -> None:
    class FailingOperations(_LegacyTerminalOperations):
        def operation_preheader(self, **_kwargs: object) -> LedgerOperationPreheader:
            self.preheader_calls += 1
            raise WriteOperationError("operation_unavailable")

    operations = FailingOperations(chained=False)
    conversations = _ConversationBodyReadSpy()
    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=_deterministic_adapter(operations),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    ).continue_confirmation(
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
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 0
    assert conversations.calls == 0


def test_mismatched_deterministic_and_coordinator_repositories_fail_closed() -> None:
    authoritative = _LegacyTerminalOperations(chained=False)
    alternate = _LegacyTerminalOperations(chained=False)
    conversations = _ConversationBodyReadSpy()
    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=_deterministic_adapter(alternate),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, authoritative))
            ),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=authoritative.operation_id,
            confirmation_token=authoritative.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert outcome.status_code == 503
    assert authoritative.preheader_calls == 1
    assert authoritative.get_calls == 0
    assert authoritative.replay_calls == 0
    assert alternate.preheader_calls == 0
    assert alternate.get_calls == 0
    assert alternate.replay_calls == 0
    assert conversations.calls == 0


def test_real_adapter_live_preflight_and_confirm_rechecks_operation_once() -> None:
    operations = _LegacyTerminalOperations(chained=False, status="proposed")
    pending = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        '{"application_id":1,"jd_text":"live"}',
        "请确认将这份岗位资料保存到当前投递。",
        operations.operation_id,
    )

    class Persistence:
        @staticmethod
        def get_pending_action(_conversation_id: int) -> PendingAction:
            return pending

    adapter = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, Persistence()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=_confirmation_token(pending),
    )
    preheader = operations.operation_preheader(
        conversation_id=7,
        operation_id=operations.operation_id,
    )
    preflight = adapter.preflight_confirmation(request, preheader=preheader)

    execution = adapter.confirm(
        request,
        SimpleNamespace(id=7),
        preflight=preflight,
    )

    assert isinstance(execution.outcome, RuntimeFailureOutcome)
    assert execution.outcome.code is RuntimeFailureCode.OPERATION_UNAVAILABLE
    assert operations.preheader_calls == 1
    assert operations.get_calls == 1
    assert operations.replay_calls == 0


@pytest.mark.parametrize("mode", ("sync", "stream"))
def test_proposed_legacy_route_terminalizes_before_conversation_and_replays_specialized(
    mode: str,
) -> None:
    operations = _LegacyTerminalOperations(chained=False, status="proposed")
    pending = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        '{"application_id":1,"jd_text":"live"}',
        "请确认将这份岗位资料保存到当前投递。",
        operations.operation_id,
    )

    class Persistence:
        @staticmethod
        def get_pending_action(_conversation_id: int) -> PendingAction:
            return pending

    deterministic = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, Persistence()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
        )
    )

    class Conversations:
        calls = 0

        @classmethod
        def load(cls, _conversation_id: int) -> object:
            cls.calls += 1
            operations.operation.status = "committed"
            return SimpleNamespace(id=7, archived_at=None)

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            deterministic=deterministic,
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )

    if mode == "sync":
        result = runtime.continue_confirmation(
            request,
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(result, OperationReplayOutcome)
    else:
        result = runtime.prepare_stream(
            request,
            transport=RuntimeTransportContext(
                mode="stream",
                transport_run_id=uuid4(),
                stream_version="pilot-sse-v1",
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(result, PreparedStreamExecution)
        assert result.preparation_kind is PreparationKind.REPLAY

    assert Conversations.calls == 1
    assert operations.preheader_calls == 1
    assert operations.get_calls == 1
    assert operations.replay_calls == 1


@pytest.mark.parametrize("mode", ("sync", "stream"))
def test_typed_approved_route_never_touches_deterministic_adapter_or_repository(
    mode: str,
) -> None:
    operations = _LegacyTerminalOperations(
        chained=False,
        adapter_kind="typed",
    )

    class TypedCoordinator:
        dependencies = SimpleNamespace(write_operations=operations)
        replay_calls = 0

        @staticmethod
        def operation_preheader(request: ConfirmationRequest) -> LedgerOperationPreheader:
            return operations.operation_preheader(
                conversation_id=request.conversation_id,
                operation_id=request.operation_id,
            )

        @classmethod
        def replay_outcome(
            cls,
            request: ConfirmationRequest,
            *,
            preheader: LedgerOperationPreheader | None = None,
        ) -> OperationReplayOutcome:
            cls.replay_calls += 1
            assert type(preheader) is LedgerOperationPreheader
            return OperationReplayOutcome(
                operation_id=operations.operation_id,
                conversation_id=request.conversation_id,
                message="typed replay",
                write_status="success",
                tool_call_id="typed-call",
                tool_name="create_application",
                visible_result="typed replay",
                summary="typed replay",
            )

    class PoisonDeterministic:
        preflight_calls = 0

        @classmethod
        def preflight_confirmation(cls, *_args: object, **_kwargs: object) -> object:
            cls.preflight_calls += 1
            raise AssertionError("typed route must not invoke deterministic preflight")

    runtime = PilotRuntime(
        RuntimeDependencies(
            deterministic=cast(Any, PoisonDeterministic()),
            confirmation_coordinator=cast(Any, TypedCoordinator()),
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        operation_id=operations.operation_id,
        confirmation_token=operations.token,
    )

    if mode == "sync":
        result = runtime.continue_confirmation(
            request,
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(result, OperationReplayOutcome)
    else:
        result = runtime.prepare_stream(
            request,
            transport=RuntimeTransportContext(
                mode="stream",
                transport_run_id=uuid4(),
                stream_version="pilot-sse-v1",
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(result, PreparedStreamExecution)
        assert result.preparation_kind is PreparationKind.REPLAY

    assert TypedCoordinator.replay_calls == 1
    assert PoisonDeterministic.preflight_calls == 0
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 0


def test_legacy_route_rejects_lookalike_adapter_before_preflight_or_conversation() -> None:
    operations = _LegacyTerminalOperations(chained=False, status="proposed")
    conversations = _ConversationBodyReadSpy()

    class LookalikeAdapter:
        dependencies = SimpleNamespace(write_operations=operations)
        preflight_calls = 0

        @classmethod
        def preflight_confirmation(cls, *_args: object, **_kwargs: object) -> object:
            cls.preflight_calls += 1
            raise AssertionError("lookalike adapter must not mint a route decision")

    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, conversations),
            deterministic=cast(Any, LookalikeAdapter()),
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.status_code == 400
    assert LookalikeAdapter.preflight_calls == 0
    assert conversations.calls == 0
    assert operations.preheader_calls == 1
    assert operations.get_calls == 0
    assert operations.replay_calls == 0


def test_proposed_legacy_fresh_route_identity_mismatch_fails_closed() -> None:
    operations = _LegacyTerminalOperations(chained=False, status="proposed")
    pending = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        '{"application_id":1,"jd_text":"live"}',
        "请确认将这份岗位资料保存到当前投递。",
        operations.operation_id,
    )

    class Persistence:
        @staticmethod
        def get_pending_action(_conversation_id: int) -> PendingAction:
            return pending

    deterministic = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, Persistence()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
        )
    )

    class Conversations:
        @staticmethod
        def load(_conversation_id: int) -> object:
            operations.operation.tool_call_id = "alternate-call"
            return SimpleNamespace(id=7, archived_at=None)

    outcome = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            deterministic=deterministic,
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    ).continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=True,
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=InMemoryRuntimeInvocationControl(),
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.code is RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT
    assert outcome.status_code == 409
    assert outcome.retryable is False
    assert operations.preheader_calls == 1
    assert operations.get_calls == 1
    assert operations.replay_calls == 0


@pytest.mark.parametrize("mode", ("sync", "stream"))
def test_omitted_operation_id_rejects_pending_replacement_after_legacy_preheader(
    mode: str,
) -> None:
    class OmittedIdOperations(_LegacyTerminalOperations):
        def operation_preheader(
            self, *, conversation_id: int, operation_id: str | None
        ) -> LedgerOperationPreheader:
            self.preheader_calls += 1
            assert self.preheader_calls == 1
            assert conversation_id == 7
            assert operation_id is None
            return LedgerOperationPreheader(
                self.operation,
                LedgerPendingPointer(
                    conversation_id=7,
                    operation_id=self.operation_id,
                    tool_call_id="legacy-call",
                    tool_name="save_application_jd_version",
                    pending_confirmation_claim_id="",
                ),
            )

    operations = OmittedIdOperations(chained=False, status="proposed")
    shared_args = json.dumps(
        {
            "application_id": 1,
            "expected_current_version_id": None,
            "idempotency_key": "legacy-replacement-race",
            "jd_text": "live",
            "source_url": None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    operations.operation.raw_args = shared_args
    replacement = PendingAction(
        "legacy-call",
        "save_application_jd_version",
        shared_args,
        "请确认将这份岗位资料保存到当前投递。",
        str(uuid4()),
    )
    shared_token = _confirmation_token(replacement)
    operations.operation.confirmation_token_fingerprint = ledger_fingerprint(
        operations.key,
        "write-operation-confirmation-token-v1",
        shared_token.encode("ascii"),
    )

    class Persistence:
        @staticmethod
        def get_pending_action(_conversation_id: int) -> PendingAction:
            return replacement

    class PoisonCoordinator:
        calls = 0

        @classmethod
        def execute_legacy(cls, **_kwargs: object) -> object:
            cls.calls += 1
            raise AssertionError("replacement Pending must not reach the executor")

    deterministic = DeterministicPilotAdapter(
        DeterministicDependencies(
            persistence=cast(Any, Persistence()),
            applications=SimpleNamespace(),
            application_jd_versions=SimpleNamespace(),
            application_outcomes=SimpleNamespace(),
            write_operations=operations,
            write_coordinator=PoisonCoordinator(),
        )
    )

    class Conversations:
        @staticmethod
        def load(_conversation_id: int) -> object:
            return SimpleNamespace(id=7, archived_at=None)

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=Conversations(),
            deterministic=deterministic,
            confirmation_coordinator=ConfirmationCoordinator(
                ConfirmationDependencies(write_operations=cast(Any, operations))
            ),
        )
    )
    request = ConfirmationRequest(
        conversation_id=7,
        approved=True,
        confirmation_token=shared_token,
    )

    if mode == "sync":
        outcome = runtime.continue_confirmation(
            request,
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(outcome, RuntimeFailureOutcome)
        assert outcome.code is RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT
        assert outcome.status_code == 409
        assert outcome.retryable is False
    else:
        outcome = runtime.prepare_stream(
            request,
            transport=RuntimeTransportContext(
                mode="stream",
                transport_run_id=uuid4(),
                stream_version="pilot-sse-v1",
            ),
            invocation_control=InMemoryRuntimeInvocationControl(),
        )
        assert isinstance(outcome, ImmediateHttpOutcome)
        assert outcome.status_code == 409
        assert outcome.payload["error_code"] == RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT.value
        assert outcome.payload["_runtime_stream_retryable"] is False

    assert operations.preheader_calls == 1
    assert operations.get_calls == 1
    assert operations.replay_calls == 0
    assert PoisonCoordinator.calls == 0
