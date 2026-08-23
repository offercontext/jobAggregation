from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeAlias, cast

from offerpilot.ai.control import AgentLoopControlError
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import (
    ToolExecutionContext,
    audit_bindings,
    require_capabilities,
)
from offerpilot.ai.tool_runtime.contracts import (
    ConfirmationRequired,
    ExecutionAuthorization,
    JSONValue,
    PreparedToolCall,
    ReadyToExecute,
    ToolExceptionMapping,
    ToolExecutionRecord,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
    TransientToolRuntimeValue,
)
from offerpilot.ai.tool_runtime.journal import (
    prepare_tool_started_draft,
    project_tool_proposed,
    project_tool_started,
    project_tool_terminal,
)
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.validation import (
    ArgumentValidationError,
    canonical_json,
    lossless_typed_copy,
    parse_arguments,
    validate_arguments,
)
from offerpilot.ai.types import ToolCall


@dataclass(frozen=True)
class Rejected(TransientToolRuntimeValue):
    failure: ToolFailure = field(repr=False)


PrepareResult: TypeAlias = ConfirmationRequired[Any, Any] | ReadyToExecute[Any, Any] | Rejected
StageSink: TypeAlias = Callable[[str], None]
ConfirmationClaimer: TypeAlias = Callable[
    [PreparedToolCall[Any, Any]], ExecutionAuthorization | ToolFailure
]


def prepare_call(
    catalog: ToolCatalog,
    context: ToolExecutionContext,
    call: ToolCall,
    *,
    pending_identity: object | None = None,
    pending_action_revision: int | None = None,
    stage_sink: StageSink | None = None,
    record_proposal: bool = True,
) -> PrepareResult:
    spec = catalog.resolve(call.name)
    if spec is None:
        return Rejected(
            ToolFailure(
                category="validation_error",
                code="unknown_tool",
                compatibility_detail=f'未知工具 "{call.name}"',
            )
        )

    if record_proposal:
        project_tool_proposed(context.run_recorder, spec, call)
    _stage(stage_sink, "parse")
    try:
        parsed = parse_arguments(call.args)
    except ArgumentValidationError as exc:
        return Rejected(_validation_failure(exc.code))

    _stage(stage_sink, "schema")
    try:
        validated = validate_arguments(catalog.validator_for(spec.name), parsed)
    except ArgumentValidationError as exc:
        return Rejected(_schema_validation_failure(spec, parsed, exc.code))

    _stage(stage_sink, "decode")
    try:
        copied = lossless_typed_copy(validated)
        typed_args = spec.decoder(cast(Mapping[str, JSONValue], copied))
    except ArgumentValidationError as exc:
        return Rejected(_validation_failure(exc.code))
    except AgentLoopControlError:
        raise
    except Exception:
        return Rejected(ToolFailure("internal_error", "argument_decode_failed"))

    _stage(stage_sink, "capability")
    permission = require_capabilities(spec, context)
    if permission is not None:
        return Rejected(permission)

    _stage(stage_sink, "binding")
    try:
        binding = audit_bindings(spec, typed_args, context)
    except AgentLoopControlError:
        raise
    except Exception:
        return Rejected(ToolFailure("internal_error", "binding_resolution_failed"))

    _stage(stage_sink, "preflight")
    if spec.preflight is not None:
        try:
            preflight_failure = spec.preflight(typed_args, context)
        except AgentLoopControlError:
            raise
        except Exception as exc:
            return Rejected(_map_exception(spec, exc))
        if preflight_failure is not None:
            return Rejected(preflight_failure)

    arguments = cast(dict[str, JSONValue], lossless_typed_copy(validated))
    prepared = PreparedToolCall(
        arguments=arguments,
        arguments_digest=_arguments_digest(arguments),
        binding=binding,
        contract_fingerprint=_contract_fingerprint(spec.contract.payload),
        pending_action_revision=pending_action_revision,
        pending_identity=pending_identity,
        spec=spec,
        tool_call_id=call.id,
        typed_args=typed_args,
    )
    if spec.kind == "write":
        prepared = replace(
            prepared,
            journal_started_draft=prepare_tool_started_draft(context.run_recorder, prepared),
        )
    if spec.confirmation_policy == "required":
        return ConfirmationRequired(prepared)
    return ReadyToExecute(prepared)


def execute_prepared(
    prepared: PreparedToolCall[Any, Any],
    context: ToolExecutionContext,
    *,
    confirmation_claimer: ConfirmationClaimer | None = None,
    stage_sink: StageSink | None = None,
) -> ToolExecutionRecord[Any, Any]:
    spec = prepared.spec
    _stage(stage_sink, "mutable")
    if spec.mutable_validator is not None:
        try:
            mutable_failure = spec.mutable_validator(prepared.typed_args, context)
        except AgentLoopControlError:
            raise
        except Exception as exc:
            return _failed_record(prepared, _map_exception(spec, exc))
        if mutable_failure is not None:
            return _failed_record(prepared, mutable_failure)

    if spec.kind == "write":
        _stage(stage_sink, "claim")
        if confirmation_claimer is None:
            return _failed_record(
                prepared,
                ToolFailure("conflict", "confirmation_claim_required"),
            )
        try:
            authorization = confirmation_claimer(prepared)
        except AgentLoopControlError:
            raise
        except Exception:
            return _failed_record(prepared, ToolFailure("conflict", "confirmation_claim_failed"))
        if isinstance(authorization, ToolFailure):
            return _failed_record(prepared, authorization)
        _stage(stage_sink, "authorization")
        _stage(stage_sink, "authorization_match")
        if not _authorization_matches(prepared, authorization):
            return _failed_record(
                prepared,
                ToolFailure("stale_state", "authorization_mismatch"),
            )
        if context.operation_executor is not None:
            record = cast(
                ToolExecutionRecord[Any, Any],
                context.operation_executor(prepared, context, authorization),
            )
            if record.replayed or not record.execution_started:
                return record
            started_recorded = record.journal_started_recorded
            if record.persisted_visible_result is None:
                raise RuntimeError("persisted operation result is missing")
            project_tool_terminal(
                context.run_recorder,
                record,
                started_recorded=started_recorded,
                visible_result=record.persisted_visible_result,
            )
            return record

    started_recorded = project_tool_started(context.run_recorder, prepared)
    _stage(stage_sink, "tool.started")
    _stage(stage_sink, "executor")
    try:
        result = spec.executor(prepared.typed_args, context)
    except AgentLoopControlError:
        raise
    except Exception as exc:
        record = ToolExecutionRecord(
            execution_started=True,
            outcome=_map_exception(spec, exc),
            prepared=prepared,
        )
        _stage(stage_sink, "tool.failed")
        project_tool_terminal(
            context.run_recorder,
            record,
            started_recorded=started_recorded,
            visible_result=render_compatibility(spec, record.outcome),
        )
        return record

    record = ToolExecutionRecord(
        execution_started=True,
        outcome=ToolSuccess(result),
        prepared=prepared,
    )
    _stage(stage_sink, "tool.completed")
    project_tool_terminal(
        context.run_recorder,
        record,
        started_recorded=started_recorded,
        visible_result=render_compatibility(spec, record.outcome),
    )
    return record


def _validation_failure(code: str) -> ToolFailure:
    return ToolFailure(
        category="validation_error",
        code=code,
        compatibility_detail="工具参数验证失败，请检查后重试。",
    )


def _schema_validation_failure(
    spec: ToolSpec[Any, Any],
    arguments: Mapping[str, JSONValue],
    code: str,
) -> ToolFailure:
    if spec.schema_failure_renderer is not None:
        try:
            detail = spec.schema_failure_renderer(arguments, code)
        except Exception:
            detail = None
        if detail:
            return ToolFailure("validation_error", code, detail)
    required = spec.contract.parameters.get("required")
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in arguments]
        if missing:
            return ToolFailure(
                category="validation_error",
                code=code,
                compatibility_detail=f"{spec.name} requires {missing[0]}",
            )
    return _validation_failure(code)


def _arguments_digest(arguments: dict[str, JSONValue]) -> str:
    encoded = canonical_json(arguments).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _contract_fingerprint(payload: Mapping[str, JSONValue]) -> str:
    encoded = canonical_json(dict(payload)).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _authorization_matches(
    prepared: PreparedToolCall[Any, Any],
    authorization: ExecutionAuthorization,
) -> bool:
    return (
        prepared.pending_identity is not None
        and prepared.pending_action_revision is not None
        and authorization.pending_identity == prepared.pending_identity
        and authorization.pending_action_revision == prepared.pending_action_revision
        and authorization.tool_call_id == prepared.tool_call_id
        and authorization.tool_name == prepared.spec.name
        and authorization.arguments_digest == prepared.arguments_digest
    )


def _map_exception(spec: ToolSpec[Any, Any], error: Exception) -> ToolFailure:
    for mapping in spec.exception_map:
        if isinstance(error, mapping.exception_type):
            return _mapped_failure(mapping, error)
    return ToolFailure("internal_error", "executor_exception")


def _mapped_failure(mapping: ToolExceptionMapping, error: Exception) -> ToolFailure:
    detail = ""
    if mapping.compatibility_detail is not None:
        try:
            detail = mapping.compatibility_detail(error)
        except Exception:
            detail = ""
    return ToolFailure(mapping.category, mapping.code, detail)


def _failed_record(
    prepared: PreparedToolCall[Any, Any],
    failure: ToolFailure,
) -> ToolExecutionRecord[Any, Any]:
    return ToolExecutionRecord(
        execution_started=False,
        outcome=failure,
        prepared=prepared,
    )


def _stage(sink: StageSink | None, value: str) -> None:
    if sink is None:
        return
    try:
        sink(value)
    except Exception:
        return
