from __future__ import annotations

from typing import Any, cast

import pytest

from offerpilot.ai.agent_contracts import AgentLoopControlError, ChatRunCancelled
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ExecutionAuthorization,
    ProviderToolContract,
    ReadyToExecute,
    ToolExceptionMapping,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
    WriteContract,
)
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.pilot_runtime.errors import RuntimeCancelled, RuntimeTransportAborted


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.recording_status = "healthy"

    def append_event(self, event: Any) -> None:
        self.events.append(event)


def _context(
    recorder: Recorder,
    *,
    capabilities: frozenset[ToolCapability] | None = None,
) -> ToolExecutionContext:
    dependency = cast(Any, object())
    return ToolExecutionContext(
        applications=dependency,
        capabilities=capabilities
        if capabilities is not None
        else frozenset({ToolCapability.APPLICATIONS_READ, ToolCapability.APPLICATIONS_WRITE}),
        current_bindings={},
        events=dependency,
        jd_analyses=dependency,
        notes=dependency,
        offers=dependency,
        resumes=dependency,
        run_recorder=cast(Any, recorder),
    )


def _spec(
    *,
    kind: str = "read",
    decoder: Any | None = None,
    binding_resolvers: tuple[Any, ...] | None = None,
    executor: Any | None = None,
    preflight: Any | None = None,
    mutable_validator: Any | None = None,
    required_capabilities: frozenset[ToolCapability] | None = None,
    exception_map: tuple[ToolExceptionMapping, ...] = (),
) -> ToolSpec[dict[str, Any], dict[str, Any]]:
    name = "write_application" if kind == "write" else "read_application"
    parameters = {
        "additionalProperties": True,
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
        "type": "object",
    }
    return ToolSpec(
        confirmation_policy="required" if kind == "write" else "none",
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {
                    "description": name,
                    "name": name,
                    "parameters": parameters,
                },
            },
            name=name,
            description=name,
            parameters=parameters,
        ),
        declared_failure_categories=frozenset(mapping.category for mapping in exception_map),
        decoder=decoder or (lambda values: dict(values)),
        binding_resolvers=binding_resolvers or (),
        exception_map=exception_map,
        executor=executor or (lambda args, context: args),
        kind=cast(Any, kind),
        write_contract=WriteContract() if kind == "write" else None,
        mutable_validator=mutable_validator,
        preflight=preflight,
        required_capabilities=required_capabilities
        if required_capabilities is not None
        else frozenset(
            {
                ToolCapability.APPLICATIONS_WRITE
                if kind == "write"
                else ToolCapability.APPLICATIONS_READ
            }
        ),
        success_renderer=lambda result: str(result),
    )


def _catalog(spec: ToolSpec[Any, Any]) -> ToolCatalog:
    return ToolCatalog([spec], expected_names=(spec.name,))


def test_prepare_and_read_execute_have_exact_stage_order() -> None:
    trace: list[str] = []
    recorder = Recorder()
    spec = _spec()

    prepared_result = prepare_call(
        _catalog(spec),
        _context(recorder),
        ToolCall(id="read-1", name=spec.name, args='{"id":1,"extra":"kept"}'),
        stage_sink=trace.append,
    )

    assert isinstance(prepared_result, ReadyToExecute)
    assert trace == ["parse", "schema", "decode", "capability", "binding", "preflight"]
    assert prepared_result.prepared.arguments == {"id": 1, "extra": "kept"}

    trace.clear()
    record = execute_prepared(
        prepared_result.prepared,
        _context(recorder),
        stage_sink=trace.append,
    )

    assert isinstance(record.outcome, ToolSuccess)
    assert trace == ["mutable", "tool.started", "executor", "tool.completed"]


def test_confirmed_write_claims_and_matches_authorization_before_executor() -> None:
    trace: list[str] = []
    calls = 0

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del context
        calls += 1
        return args

    spec = _spec(kind="write", executor=executor)
    recorder = Recorder()
    prepared_result = prepare_call(
        _catalog(spec),
        _context(recorder),
        ToolCall(id="write-1", name=spec.name, args='{"id":1}'),
        pending_action_revision=4,
        pending_identity="trusted-pending",
    )
    assert isinstance(prepared_result, ConfirmationRequired)

    def claim(prepared: Any) -> ExecutionAuthorization:
        return ExecutionAuthorization(
            arguments_digest=prepared.arguments_digest,
            pending_action_revision=4,
            pending_identity="trusted-pending",
            tool_call_id="write-1",
            tool_name=spec.name,
        )

    record = execute_prepared(
        prepared_result.prepared,
        _context(recorder),
        confirmation_claimer=claim,
        stage_sink=trace.append,
    )

    assert isinstance(record.outcome, ToolSuccess)
    assert calls == 1
    assert trace == [
        "mutable",
        "claim",
        "authorization",
        "authorization_match",
        "tool.started",
        "executor",
        "tool.completed",
    ]


@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    (
        ("unknown", "unknown_tool"),
        ("invalid_json", "invalid_json"),
        ("missing_capability", "missing_capability"),
        ("preflight", "preflight_failed"),
    ),
)
def test_prepare_failure_never_executes_or_writes_execution_terminal_events(
    failure_kind: str,
    expected_code: str,
) -> None:
    calls = 0

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del context
        calls += 1
        return args

    preflight = (
        (lambda args, context: ToolFailure("stale_state", "preflight_failed"))
        if failure_kind == "preflight"
        else None
    )
    spec = _spec(executor=executor, preflight=preflight)
    recorder = Recorder()
    name = "unknown" if failure_kind == "unknown" else spec.name
    args = "{" if failure_kind == "invalid_json" else '{"id":1}'
    capabilities = (
        frozenset()
        if failure_kind == "missing_capability"
        else frozenset({ToolCapability.APPLICATIONS_READ})
    )

    result = prepare_call(
        _catalog(spec),
        _context(recorder, capabilities=capabilities),
        ToolCall(id="read-1", name=name, args=args),
    )

    assert isinstance(result, Rejected)
    assert result.failure.code == expected_code
    assert calls == 0
    assert not any(
        event.event_type in {"tool.started", "tool.completed", "tool.failed"}
        for event in recorder.events
    )


@pytest.mark.parametrize("failure_kind", ("mutable", "claim", "authorization"))
def test_write_gate_failure_keeps_executor_at_zero(failure_kind: str) -> None:
    calls = 0

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del context
        calls += 1
        return args

    mutable = (
        (lambda args, context: ToolFailure("stale_state", "mutable_failed"))
        if failure_kind == "mutable"
        else None
    )
    spec = _spec(kind="write", executor=executor, mutable_validator=mutable)
    recorder = Recorder()
    prepared_result = prepare_call(
        _catalog(spec),
        _context(recorder),
        ToolCall(id="write-1", name=spec.name, args='{"id":1}'),
        pending_action_revision=4,
        pending_identity="trusted-pending",
    )
    assert isinstance(prepared_result, ConfirmationRequired)

    def claim(prepared: Any) -> ExecutionAuthorization | ToolFailure:
        if failure_kind == "claim":
            return ToolFailure("conflict", "claim_failed")
        return ExecutionAuthorization(
            arguments_digest=("sha256:" + "0" * 64)
            if failure_kind == "authorization"
            else prepared.arguments_digest,
            pending_action_revision=4,
            pending_identity="trusted-pending",
            tool_call_id="write-1",
            tool_name=spec.name,
        )

    record = execute_prepared(
        prepared_result.prepared,
        _context(recorder),
        confirmation_claimer=claim,
    )

    assert isinstance(record.outcome, ToolFailure)
    assert calls == 0
    assert record.execution_started is False
    assert not any(
        event.event_type in {"tool.started", "tool.completed", "tool.failed"}
        for event in recorder.events
    )


def test_executor_exception_is_called_once_and_mapped_by_spec() -> None:
    calls = 0

    class DomainConflict(RuntimeError):
        pass

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del args, context
        calls += 1
        raise DomainConflict("private database detail")

    spec = _spec(
        executor=executor,
        exception_map=(
            ToolExceptionMapping(DomainConflict, "conflict", "domain_conflict"),
        ),
    )
    recorder = Recorder()
    prepared = prepare_call(
        _catalog(spec),
        _context(recorder),
        ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
    )
    assert isinstance(prepared, ReadyToExecute)

    record = execute_prepared(prepared.prepared, _context(recorder))

    assert record.outcome == ToolFailure("conflict", "domain_conflict")
    assert record.execution_started is True
    assert calls == 1
    assert [event.event_type for event in recorder.events][-2:] == ["tool.started", "tool.failed"]
    assert "private database detail" not in repr(record)


@pytest.mark.parametrize(
    ("stage", "control_factory"),
    (
        ("decoder", lambda: ChatRunCancelled("decoder control")),
        ("decoder", lambda: RuntimeCancelled()),
        ("decoder", lambda: RuntimeTransportAborted()),
        ("binding", lambda: ChatRunCancelled("binding control")),
        ("binding", lambda: RuntimeCancelled()),
        ("binding", lambda: RuntimeTransportAborted()),
        ("preflight", lambda: ChatRunCancelled("preflight control")),
        ("preflight", lambda: RuntimeCancelled()),
        ("preflight", lambda: RuntimeTransportAborted()),
    ),
    ids=lambda value: value if isinstance(value, str) else type(value()).__name__.lower(),
)
def test_prepare_stage_control_error_propagates_without_rejected_mapping(
    stage: str,
    control_factory: Any,
) -> None:
    control = control_factory()

    def raise_control(*_args: Any) -> Any:
        raise control

    spec = _spec(
        decoder=raise_control if stage == "decoder" else None,
        binding_resolvers=(raise_control,) if stage == "binding" else None,
        preflight=raise_control if stage == "preflight" else None,
    )

    with pytest.raises(AgentLoopControlError) as raised:
        prepare_call(
            _catalog(spec),
            _context(Recorder()),
            ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
        )
    assert raised.value is control


@pytest.mark.parametrize(
    "control_factory",
    (
        lambda: ChatRunCancelled("mutable control"),
        lambda: RuntimeCancelled(),
        lambda: RuntimeTransportAborted(),
    ),
    ids=("chat_run_cancelled", "runtime_cancelled", "runtime_transport_aborted"),
)
def test_mutable_validator_control_error_propagates_without_tool_failure(
    control_factory: Any,
) -> None:
    control = control_factory()

    def raise_control(*_args: Any) -> Any:
        raise control

    spec = _spec(mutable_validator=raise_control)
    prepared = prepare_call(
        _catalog(spec),
        _context(Recorder()),
        ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
    )
    assert isinstance(prepared, ReadyToExecute)

    with pytest.raises(AgentLoopControlError) as raised:
        execute_prepared(prepared.prepared, _context(Recorder()))
    assert raised.value is control


@pytest.mark.parametrize(
    "control_factory",
    (
        lambda: ChatRunCancelled("cancelled"),
        lambda: RuntimeCancelled(),
        lambda: RuntimeTransportAborted(),
    ),
    ids=("chat_run_cancelled", "runtime_cancelled", "runtime_transport_aborted"),
)
def test_confirmation_claimer_control_error_propagates_without_tool_failure(
    control_factory: Any,
) -> None:
    spec = _spec(kind="write")
    prepared = prepare_call(
        _catalog(spec),
        _context(Recorder()),
        ToolCall(id="write-1", name=spec.name, args='{"id":1}'),
        pending_action_revision=4,
        pending_identity="trusted-pending",
    )
    assert isinstance(prepared, ConfirmationRequired)
    control = control_factory()

    def claim(_prepared: Any) -> ExecutionAuthorization:
        raise control

    with pytest.raises(AgentLoopControlError) as raised:
        execute_prepared(
            prepared.prepared,
            _context(Recorder()),
            confirmation_claimer=claim,
        )
    assert raised.value is control


@pytest.mark.parametrize(
    "control_factory",
    (
        lambda: ChatRunCancelled("cancelled"),
        lambda: RuntimeCancelled(),
        lambda: RuntimeTransportAborted(),
    ),
    ids=("chat_run_cancelled", "runtime_cancelled", "runtime_transport_aborted"),
)
def test_executor_control_error_propagates_without_tool_failure(
    control_factory: Any,
) -> None:
    control = control_factory()

    def executor(_args: dict[str, Any], _context: ToolExecutionContext) -> dict[str, Any]:
        raise control

    spec = _spec(executor=executor)
    prepared = prepare_call(
        _catalog(spec),
        _context(Recorder()),
        ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
    )
    assert isinstance(prepared, ReadyToExecute)

    with pytest.raises(AgentLoopControlError) as raised:
        execute_prepared(prepared.prepared, _context(Recorder()))
    assert raised.value is control


@pytest.mark.parametrize("base_error", (KeyboardInterrupt(), SystemExit(), BaseException("stop")))
def test_base_exception_propagates_without_second_executor_call(base_error: BaseException) -> None:
    calls = 0

    def executor(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
        nonlocal calls
        del args, context
        calls += 1
        raise base_error

    spec = _spec(executor=executor)
    recorder = Recorder()
    prepared = prepare_call(
        _catalog(spec),
        _context(recorder),
        ToolCall(id="read-1", name=spec.name, args='{"id":1}'),
    )
    assert isinstance(prepared, ReadyToExecute)

    with pytest.raises(type(base_error)):
        execute_prepared(prepared.prepared, _context(recorder))

    assert calls == 1
