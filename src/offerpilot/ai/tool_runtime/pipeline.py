from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias, cast

from offerpilot.ai.control import AgentLoopControlError
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    ApprovedWritePrepareCallIdentity,
    AuthorityCallIdentity,
    AuthorityPhaseError,
    AuthorityUse,
    NewTurnPrepareCallIdentity,
    ReadExecutionCallIdentity,
    SegmentExecutionAuthority,
    require_authority_phase,
    require_authority_spec,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import (
    ToolExecutionContext,
    audit_bindings,
    pre_resolver_scope_policy,
    require_capabilities,
    scope_access_denied,
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
from offerpilot.repositories.session_binding import ScopeAccessDenied


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
    call_identity: AuthorityCallIdentity | None = None,
    pending_identity: object | None = None,
    pending_action_revision: int | None = None,
    stage_sink: StageSink | None = None,
    record_proposal: bool = True,
) -> PrepareResult:
    use = _prepare_use(context, call_identity)
    _stage(stage_sink, "authority.prelookup")
    if call_identity is None:
        raise AuthorityPhaseError("prepare_call requires a registered call identity")
    require_authority_phase(context.authority, use, call_identity)
    _require_context_identity(context, call_identity)

    _stage(stage_sink, "catalog.lookup")
    spec = catalog.resolve(call.name)
    if spec is None:
        return Rejected(
            ToolFailure(
                category="validation_error",
                code="unknown_tool",
                compatibility_detail=f'未知工具 "{call.name}"',
            )
        )

    _stage(stage_sink, "authority.postlookup")
    factory = context.authority_factory
    factory.register_tool_spec(
        spec,
        authority=context.authority,
        prepare_identity=cast(
            NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity,
            call_identity,
        ),
    )
    require_authority_spec(context.authority, use, spec)
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

    _stage(stage_sink, "scope_policy")
    scope_failure = pre_resolver_scope_policy(spec, context)
    if scope_failure is not None:
        return Rejected(scope_failure)

    _stage(stage_sink, "binding.resolve")
    try:
        binding, binding_allowed = audit_bindings(spec, typed_args, context)
    except AgentLoopControlError:
        raise
    except Exception:
        return Rejected(ToolFailure("internal_error", "binding_resolution_failed"))
    _stage(stage_sink, "binding.policy")
    if not binding_allowed:
        return Rejected(scope_access_denied())

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
    prepared = factory.prepare_tool_call(
        context.authority,
        prepare_identity=cast(
            NewTurnPrepareCallIdentity | ApprovedWritePrepareCallIdentity,
            call_identity,
        ),
        tool_call_id=call.id,
        spec=spec,
        arguments=arguments,
        typed_args=typed_args,
        arguments_digest=_arguments_digest(arguments),
        contract_fingerprint=_contract_fingerprint(spec.contract.payload),
        binding=binding,
    )
    object.__setattr__(
        prepared,
        "prepared_instance_token",
        factory.prepared_token(prepared),
    )
    if spec.kind == "write":
        # The draft is transport compatibility data, not authorization.  It is
        # attached to the exact factory-created Prepared object and never used
        # to reconstruct authority or constraint state.
        object.__setattr__(
            prepared,
            "journal_started_draft",
            prepare_tool_started_draft(context.run_recorder, prepared),
        )
        object.__setattr__(prepared, "pending_identity", pending_identity)
        object.__setattr__(
            prepared, "pending_action_revision", pending_action_revision
        )
    _stage(stage_sink, "prepared")
    if spec.confirmation_policy == "required":
        return ConfirmationRequired(prepared)
    return ReadyToExecute(prepared)


def execute_prepared(
    prepared: PreparedToolCall[Any, Any],
    context: ToolExecutionContext,
    *,
    call_identity: AuthorityCallIdentity | None = None,
    confirmation_claimer: ConfirmationClaimer | None = None,
    stage_sink: StageSink | None = None,
) -> ToolExecutionRecord[Any, Any]:
    if prepared.spec.kind == "read":
        return _execute_read(
            prepared,
            context,
            call_identity=call_identity,
            stage_sink=stage_sink,
        )
    # Task 10 replaces this isolated compatibility branch with a sealed
    # one-shot ExecutionClaim.  Task 9 neither creates nor imitates that claim.
    return _execute_compatibility_write(
        prepared,
        context,
        confirmation_claimer=confirmation_claimer,
        stage_sink=stage_sink,
    )


def _execute_compatibility_write(
    prepared: PreparedToolCall[Any, Any],
    context: ToolExecutionContext,
    *,
    confirmation_claimer: ConfirmationClaimer | None,
    stage_sink: StageSink | None,
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
    _stage(stage_sink, "claim")
    if confirmation_claimer is None:
        return _failed_record(
            prepared, ToolFailure("conflict", "confirmation_claim_required")
        )
    try:
        authorization = confirmation_claimer(prepared)
    except AgentLoopControlError:
        raise
    except Exception:
        return _failed_record(
            prepared, ToolFailure("conflict", "confirmation_claim_failed")
        )
    if isinstance(authorization, ToolFailure):
        return _failed_record(prepared, authorization)
    _stage(stage_sink, "authorization")
    _stage(stage_sink, "authorization_match")
    if not _authorization_matches(prepared, authorization):
        return _failed_record(
            prepared, ToolFailure("stale_state", "authorization_mismatch")
        )
    if context.operation_executor is not None:
        record = cast(
            ToolExecutionRecord[Any, Any],
            context.operation_executor(prepared, context, authorization),
        )
        if record.replayed or not record.execution_started:
            return record
        if record.persisted_visible_result is None:
            raise RuntimeError("persisted operation result is missing")
        project_tool_terminal(
            context.run_recorder,
            record,
            started_recorded=record.journal_started_recorded,
            visible_result=record.persisted_visible_result,
        )
        return record
    started_recorded = project_tool_started(context.run_recorder, prepared)
    _stage(stage_sink, "tool.started")
    _stage(stage_sink, "executor")
    try:
        with context.session_factory() as session:
            bound_context = context.bind(session)
            result = spec.executor(prepared.typed_args, bound_context)
            session.commit()
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


def _execute_read(
    prepared: PreparedToolCall[Any, Any],
    context: ToolExecutionContext,
    *,
    call_identity: AuthorityCallIdentity | None,
    stage_sink: StageSink | None,
) -> ToolExecutionRecord[Any, Any]:
    _stage(stage_sink, "authority.prelookup")
    if call_identity is None or type(call_identity) is not ReadExecutionCallIdentity:
        raise AuthorityPhaseError("read execution requires a registered read call identity")
    require_authority_phase(context.authority, AuthorityUse.READ_EXECUTE, call_identity)
    _require_context_identity(context, call_identity)
    if call_identity.prepared is not prepared:
        raise AuthorityPhaseError("read identity belongs to another PreparedToolCall")
    if prepared.prepared_instance_token is not call_identity.prepared_instance_token:
        raise AuthorityPhaseError("PreparedToolCall registry token mismatch")

    spec = prepared.spec
    _stage(stage_sink, "authority.postlookup")
    require_authority_spec(context.authority, AuthorityUse.READ_EXECUTE, spec)

    with context.session_factory() as session:
        bound_context = context.bind(session)
        _stage(stage_sink, "capability")
        permission = require_capabilities(spec, bound_context)
        if permission is not None:
            session.rollback()
            return _failed_record(prepared, permission)

        _stage(stage_sink, "binding.resolve")
        try:
            binding, binding_allowed = audit_bindings(
                spec, prepared.typed_args, bound_context
            )
        except AgentLoopControlError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            return _failed_record(
                prepared, ToolFailure("internal_error", "binding_resolution_failed")
            )
        _stage(stage_sink, "binding.policy")
        del binding
        if not binding_allowed:
            session.rollback()
            return _failed_record(prepared, scope_access_denied())

        # End the resolver snapshot before the externally visible start event.
        # The same Session object is retained, but the final scoped statement
        # begins a fresh SQLite snapshot and is the authorization/data
        # linearization point.
        session.rollback()
        _stage(stage_sink, "binding.rollback")

        started_recorded = project_tool_started(context.run_recorder, prepared)
        _stage(stage_sink, "tool.started")
        _stage(stage_sink, "executor")
        try:
            result = spec.executor(prepared.typed_args, bound_context)
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


def _prepare_use(
    context: ToolExecutionContext,
    call_identity: AuthorityCallIdentity | None,
) -> AuthorityUse:
    authority = context.authority
    if isinstance(authority, SegmentExecutionAuthority):
        if call_identity is not None and type(call_identity) is not NewTurnPrepareCallIdentity:
            raise AuthorityPhaseError("Segment prepare requires NewTurnPrepareCallIdentity")
        return AuthorityUse.NEW_TURN_PREPARE
    if isinstance(authority, ApprovalExecutionAuthority):
        if call_identity is not None and type(call_identity) is not ApprovedWritePrepareCallIdentity:
            raise AuthorityPhaseError("approval prepare requires ApprovedWritePrepareCallIdentity")
        return AuthorityUse.APPROVED_WRITE_PREPARE
    raise AuthorityPhaseError("unknown ToolExecutionAuthority")


def _require_context_identity(
    context: ToolExecutionContext, call_identity: AuthorityCallIdentity
) -> None:
    identity_context = getattr(call_identity, "tool_context", None)
    if identity_context is not None and identity_context is not context:
        raise AuthorityPhaseError("call identity belongs to another ToolExecutionContext")
    approval_context = getattr(call_identity, "approval_context", None)
    if approval_context is not None and approval_context is not context:
        raise AuthorityPhaseError("approval identity belongs to another ToolExecutionContext")


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
    if isinstance(error, ScopeAccessDenied):
        return scope_access_denied()
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
