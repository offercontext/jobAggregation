from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offerpilot.ai.agent_contracts import (
    AgentToolCall,
    AgentToolResult,
    AgentTurnResult,
    PendingAction,
    StalePendingActionError,
)
from offerpilot.ai.agent_loop import (
    AgentLoopInvocation,
    AgentLoopRunner,
    ApprovedWriteSeed,
    NewTurnSeed,
    _delivery_error_payload,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingTarget,
    ConfirmationRequired,
    ExecutionAuthorization,
    ProviderToolContract,
    ToolExecutionRecord,
    ToolFailure,
    ToolSpec,
    ToolSuccess,
    WriteContract,
)
from offerpilot.ai.tool_runtime.pipeline import Rejected, execute_prepared, prepare_call
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    OperationCommitted,
    OperationReplay,
    TerminalPayload,
    WriteOperationError,
    ledger_fingerprint,
)
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.chat_transport import (
    encode_sse_event,
    event_sse_name,
    event_sse_payload,
    outcome_http_payload,
    outcome_http_response,
    outcome_http_status,
    runtime_stream_response,
)
from offerpilot.pilot_runtime.continuation import (
    ConfirmationCoordinator,
    ConfirmationDependencies,
)
from offerpilot.pilot_runtime.contracts import (
    ConfirmationRequest,
    ErrorEvent,
    ImmediateHttpOutcome,
    RuntimeFailureOutcome,
    StartTurnRequest,
)
from offerpilot.pilot_runtime.errors import RuntimeFailureCode
from offerpilot.pilot_runtime.event_sink import InMemoryRuntimeInvocationControl
from offerpilot.pilot_runtime.persistence import PersistenceResult, PersistenceStatus
from offerpilot.pilot_runtime.service import PilotRuntime, RuntimeDependencies

from .golden import BASELINE, FIXTURES, canonical_json, load_golden


MODEL_TOOL_NAMES = (
    "list_applications",
    "get_application",
    "create_application",
    "update_application_status",
    "list_application_events",
    "get_application_event",
    "create_application_event",
    "update_application_event",
    "delete_application_event",
    "list_notes",
    "add_note",
    "update_note",
    "delete_note",
    "list_offers",
    "get_offer",
    "compare_offers",
    "update_offer",
    "save_offer_assessment",
    "list_resumes",
    "get_resume",
    "resume_update_career_intent",
    "resume_rewrite_highlight",
    "list_resume_matches",
    "list_jd_analyses",
    "get_jd_analysis",
)
LEGACY_TOOL_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
CAPABILITIES = (
    "applications.read",
    "applications.write",
    "application_events.read",
    "application_events.write",
    "notes.read",
    "notes.write",
    "offers.read",
    "offers.write",
    "resumes.read",
    "resumes.write",
    "jd_analyses.read",
)
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "auth_token",
        "confirmation_secret",
        "exception",
        "private_key",
        "secret",
        "stack_trace",
        "traceback",
    }
)
REAL_USER_CANARIES = (
    "yuqi.chen",
    "candidate secret",
    "sk-secret-value",
    "真实简历",
    "真实职位描述",
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)+")
UNC_ABSOLUTE_PATH = re.compile(r"\\\\(?:[^\\/\s]+[\\/]){2,}[^\\/\\s]*")
TRACEBACK_TEXT = re.compile(r"traceback|stack trace", re.IGNORECASE)
EXCEPTION_EXPRESSION = re.compile(r"(?:exception|[A-Za-z_][A-Za-z0-9_]*Error)\s*\(")
EXCEPTION_CLASS_NAME = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVIEWED_CAPABILITY_FINGERPRINT = (
    "sha256:be49ef8b3335740931fc3dee690948d87fed499921cd9c6f4f8586c786ec7487"
)
REVIEWED_BINDING_FINGERPRINT = (
    "sha256:0cb3e67abade3b5e986414267facd41d49f866050f90a97d50aec32d54320ca7"
)


def _assert_private_string(value: str) -> None:
    assert not WINDOWS_ABSOLUTE_PATH.search(value)
    assert not POSIX_ABSOLUTE_PATH.search(value)
    assert not UNC_ABSOLUTE_PATH.search(value)
    assert not TRACEBACK_TEXT.search(value)
    assert not EXCEPTION_EXPRESSION.search(value)
    assert not EXCEPTION_CLASS_NAME.search(value)
    for canary in REAL_USER_CANARIES:
        assert canary not in value


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        assert not FORBIDDEN_KEYS & lowered
        for key, nested in value.items():
            if isinstance(key, str):
                _assert_private_string(key)
            _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested)
    elif isinstance(value, str):
        _assert_private_string(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fixture_identity(name: str) -> dict[str, Any]:
    path = Path(__file__).parents[1] / "fixtures" / name
    raw = path.read_bytes()
    value = load_golden(name)
    return {
        "raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": _digest(value),
        "baseline": value.get("baseline"),
        "source_baseline": value.get("source_baseline"),
    }


def test_authority_assets_are_canonical_private_and_pinned() -> None:
    names = (
        "baseline_2427fa6.json",
        "authority_manifest_v1.json",
        "policy_fingerprints_v1.json",
    )
    for name in names:
        value = load_golden(f"tool_authority/{name}")
        raw = (FIXTURES / name).read_bytes()
        decoded = raw.decode("utf-8")
        assert raw == (canonical_json(value) + "\n").encode("utf-8")
        assert "SQLite format 3" not in decoded
        _walk(value)


def test_baseline_references_exact_repository_and_production() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    assert baseline["schema_version"] == 1
    assert baseline["repository_baseline"] == "1574d0e891391c817c325f598b4f22f8a783833"
    assert baseline["production_baseline"] == "2427fa6"
    assert BASELINE == baseline["production_baseline"]
    assert tuple(baseline["typed_tool_names"]) == MODEL_TOOL_NAMES
    assert tuple(baseline["legacy_deterministic_names"]) == LEGACY_TOOL_NAMES
    assert not set(MODEL_TOOL_NAMES) & set(LEGACY_TOOL_NAMES)


def test_provider_envelopes_and_schema_fingerprints_are_fully_pinned() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    provider = load_golden("tool_pipeline/provider_manifest_30c944f.json")
    pinned = baseline["provider_manifest"]
    assert pinned["fixture"] == "tool_pipeline/provider_manifest_30c944f.json"
    assert pinned["baseline"] == provider["baseline"] == "30c944f3bda1d99b303f8e9875a170a552f79af7"
    assert pinned["canonical_sha256"] == _digest(provider)
    assert pinned["envelopes_sha256"] == _digest(provider["tools"])
    assert pinned["schema_fingerprints_sha256"] == _digest(provider["schema_fingerprints"])
    assert tuple(pinned["tool_names"]) == MODEL_TOOL_NAMES
    assert pinned["schema_fingerprints"] == provider["schema_fingerprints"]
    assert set(pinned["envelope_fingerprints"]) == set(MODEL_TOOL_NAMES)
    expected_envelopes = {
        item["function"]["name"]: _digest(item) for item in provider["tools"]
    }
    assert pinned["envelope_fingerprints"] == expected_envelopes


class _ImmediateStreamRuntime:
    def __init__(self, outcome: ImmediateHttpOutcome) -> None:
        self.outcome = outcome

    def prepare_stream(self, _request: object, **_kwargs: object) -> ImmediateHttpOutcome:
        return self.outcome


def _immediate_transport_projection(
    outcome: RuntimeFailureOutcome,
    request: StartTurnRequest | ConfirmationRequest,
) -> dict[str, Any]:
    control = InMemoryRuntimeInvocationControl()
    immediate = PilotRuntime(RuntimeDependencies())._stream_immediate(
        outcome,
        cast(Any, control),
    )
    assert isinstance(immediate, ImmediateHttpOutcome)
    sync_response = outcome_http_response(immediate)
    stream_response = runtime_stream_response(
        _ImmediateStreamRuntime(immediate),
        request,
    )
    stream: dict[str, Any] = {
        "direct": immediate.response_payload["_runtime_stream_direct"],
        "http_status": stream_response.status_code,
        "media_type": str(stream_response.media_type).split(";", 1)[0],
    }
    if str(stream_response.media_type).startswith("text/event-stream"):
        lines = bytes(stream_response.body).decode("utf-8").splitlines()
        encoded = next(
            line.removeprefix("data: ")
            for line in lines
            if line.startswith("data: ")
        )
        envelope = json.loads(encoded)
        event = ErrorEvent(
            outcome.code,
            outcome.message,
            outcome.retryable,
            outcome.degraded,
        )
        typed_event = {"event": event_sse_name(event), "data": event_sse_payload(event)}
        assert envelope["event"] == typed_event["event"]
        assert envelope["data"] == typed_event["data"]
        stream["sse"] = typed_event
    else:
        stream["body"] = json.loads(bytes(stream_response.body).decode("utf-8"))
    return {
        "sync_http": {
            "status": sync_response.status_code,
            "body": json.loads(bytes(sync_response.body).decode("utf-8")),
        },
        "sync_media_type": str(sync_response.media_type).split(";", 1)[0],
        "stream": stream,
    }


def _postheader_error_projection(outcome: RuntimeFailureOutcome) -> dict[str, Any]:
    event = ErrorEvent(outcome.code, outcome.message, outcome.retryable, outcome.degraded)
    encoded = encode_sse_event(event, seq=1, run_id="authority-run")
    lines = encoded.splitlines()
    event_name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
    data = json.loads(next(line.removeprefix("data: ") for line in lines if line.startswith("data: ")))
    assert event_name == event_sse_name(event)
    assert data["event"] == event_name
    assert data["data"] == event_sse_payload(event)
    return {
        "http_status": 200,
        "event": event_name,
        "data": data["data"],
    }


def test_existing_fixture_identities_and_compatibility_facts_are_pinned() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    expected = {
        "tool_pipeline/provider_manifest_30c944f.json": _fixture_identity(
            "tool_pipeline/provider_manifest_30c944f.json"
        ),
        "tool_pipeline/tool_outcomes_30c944f.json": _fixture_identity(
            "tool_pipeline/tool_outcomes_30c944f.json"
        ),
        "tool_pipeline/journal_sequences_30c944f.json": _fixture_identity(
            "tool_pipeline/journal_sequences_30c944f.json"
        ),
        "agent_loop/baseline_aaecf5d.json": _fixture_identity(
            "agent_loop/baseline_aaecf5d.json"
        ),
        "pilot_runtime/baseline_golden.json": _fixture_identity(
            "pilot_runtime/baseline_golden.json"
        ),
    }
    assert baseline["existing_fixture_identities"] == expected

    pilot = load_golden("pilot_runtime/baseline_golden.json")
    agent = load_golden("agent_loop/baseline_aaecf5d.json")
    compatibility = baseline["compatibility"]
    preheader_cases: dict[
        str, tuple[RuntimeFailureOutcome, StartTurnRequest | ConfirmationRequest]
    ] = {
        "source_load_failed": (
            PilotRuntime._failure(
                RuntimeFailureCode.SOURCE_LOAD_FAILED,
                "上下文暂时无法加载，请稍后重试。",
                503,
                retryable=True,
            ),
            StartTurnRequest(message="synthetic source failure"),
        ),
        "stale_pending_action": (
            PilotRuntime._provider_confirmation_failure(StalePendingActionError()),
            ConfirmationRequest(conversation_id=1, approved=True),
        ),
    }
    for name, (outcome, request) in preheader_cases.items():
        actual = _immediate_transport_projection(outcome, request)
        body = actual["sync_http"]["body"]
        assert compatibility["preheader_http"][name] == {
            "error_code": body["error_code"],
            "message": body["error"],
            "status": actual["sync_http"]["status"],
            "stream": actual["stream"],
        }
    replay_cases = {
        "operation_delivery_pending": PilotRuntime._failure(
            RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
            "确认结果正在处理中，请刷新对话查看结果。",
            409,
            retryable=True,
        ),
        "operation_delivery_unknown": PilotRuntime._failure(
            RuntimeFailureCode.OPERATION_DELIVERY_UNKNOWN,
            "对话结果暂时无法保存。",
            503,
            retryable=True,
        ),
        "operation_integrity_error": PilotRuntime._confirmation_failure(
            WriteOperationError("operation_integrity_error")
        ),
    }
    replay_request = ConfirmationRequest(conversation_id=1, approved=True)
    for name, outcome in replay_cases.items():
        actual = _immediate_transport_projection(outcome, replay_request)
        body = actual["sync_http"]["body"]
        assert compatibility["replay_http"][name] == {
            "error_code": body["error_code"],
            "message": body["error"],
            "status": actual["sync_http"]["status"],
            "stream": actual["stream"],
        }
    assert compatibility["tool_failure_messages"] == {
        "validation_error": "工具参数验证失败，请检查后重试。",
        "permission_denied": "权限不足，无法执行该操作。",
        "confirmation_rejected": "操作已取消。",
        "stale_state": "当前状态已变化，请刷新后重试。",
        "conflict": "操作冲突，请刷新后重试。",
        "not_found": "记录不存在。",
        "provider_error": "服务暂时不可用，请稍后重试。",
        "internal_error": "工具执行失败，请稍后重试。",
    }
    assert compatibility["sse"] == {
        "stream_version": "pilot-sse-v1",
        "envelope_fields": ["seq", "event", "data"],
        "initial_model": pilot["sse_sequences"]["initial_model"],
        "hitl_entry": pilot["sse_sequences"]["hitl_entry"],
        "hitl_confirm": pilot["sse_sequences"]["hitl_confirm"],
        "hitl_chain_confirm": pilot["sse_sequences"]["hitl_chain_confirm"],
        "agent_new_turn_final": agent["sse_sequences"]["new_turn_final"],
        "agent_new_turn_pending": agent["sse_sequences"]["new_turn_pending"],
        "agent_confirmation_final": agent["sse_sequences"]["confirmation_final"],
        "agent_confirmation_pending": agent["sse_sequences"]["confirmation_pending"],
    }


class _DecodeProbeError(ValueError):
    pass


class _BindingProbeError(ValueError):
    pass


class _ClaimProbeError(ValueError):
    pass


def _probe_context(
    *, capabilities: frozenset[ToolCapability] = frozenset()
) -> ToolExecutionContext:
    repository = cast(Any, object())
    return ToolExecutionContext(
        capabilities=capabilities,
        current_bindings={},
        applications=repository,
        events=repository,
        notes=repository,
        offers=repository,
        resumes=repository,
        jd_analyses=repository,
        run_recorder=NullRunRecorder(),
    )


def _probe_spec(
    name: str,
    *,
    parameters: dict[str, Any],
    decoder: Any,
    required_capabilities: frozenset[ToolCapability] = frozenset(),
    binding_resolvers: tuple[Any, ...] = (),
    preflight: Any = None,
) -> ToolSpec[Any, Any]:
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": parameters,
            },
        },
        name=name,
        description=name,
        parameters=parameters,
    )
    return ToolSpec(
        contract=contract,
        kind="read",
        decoder=decoder,
        executor=lambda args, _context: args,
        required_capabilities=required_capabilities,
        binding_resolvers=binding_resolvers,
        preflight=preflight,
        declared_failure_categories=frozenset(
            {"validation_error", "permission_denied", "stale_state", "internal_error"}
        ),
        success_renderer=str,
    )


def _identity_decoder(values: dict[str, Any]) -> dict[str, Any]:
    return dict(values)


def _raise_decode(_values: dict[str, Any]) -> dict[str, Any]:
    raise _DecodeProbeError


def _raise_binding(_args: dict[str, Any], _context: ToolExecutionContext) -> BindingTarget:
    raise _BindingProbeError


def _return_preflight_failure(
    _args: dict[str, Any], _context: ToolExecutionContext
) -> ToolFailure:
    return ToolFailure("stale_state", "preflight_failed")


def _pipeline_projection(spec: ToolSpec[Any, Any], call: ToolCall, context: ToolExecutionContext) -> dict[str, Any]:
    catalog = ToolCatalog([spec], expected_names=(spec.name,))
    result = prepare_call(catalog, context, call)
    assert isinstance(result, Rejected)
    failure = result.failure
    assert isinstance(failure, ToolFailure)
    rendered = render_compatibility(spec, failure)
    event = AgentToolResult(
        tool_call_id=call.id,
        operation_id="",
        payload=_delivery_error_payload(call.id, spec.name, rendered),
    )
    return {
        "failure": {
            "category": failure.category,
            "code": failure.code,
            "compatibility_detail": failure.compatibility_detail,
        },
        "rendered_message": rendered,
        "agent_tool_result": {
            "event": "tool_result",
            "payload": json.loads(canonical_json(dict(event.payload))),
        },
    }


def test_pre_executor_tool_pipeline_is_produced_by_runtime() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    pipeline = baseline["compatibility"]["pre_executor"]["tool_pipeline"]
    metadata = {
        "schema_missing_required": (
            "helper.prepare_call_render_agent_tool_result_projection_v1",
            "scenario.schema_missing_required_get_application_id",
            "origin.schema_missing_required",
        ),
        "decode_exception": (
            "helper.prepare_call_render_agent_tool_result_projection_v1",
            "scenario.decoder_exception_after_schema",
            "origin.decoder_exception",
        ),
        "capability_missing": (
            "helper.prepare_call_render_agent_tool_result_projection_v1",
            "scenario.missing_applications_read_capability",
            "origin.missing_capability",
        ),
        "binding_exception": (
            "helper.prepare_call_render_agent_tool_result_projection_v1",
            "scenario.binding_resolver_exception_after_capability",
            "origin.binding_resolver_exception",
        ),
        "preflight_returned_failure": (
            "helper.prepare_call_render_agent_tool_result_projection_v1",
            "scenario.preflight_returned_stale_tool_failure",
            "origin.preflight_returned_tool_failure",
        ),
    }
    assert set(pipeline) == set(metadata)
    simple_parameters = {"type": "object", "additionalProperties": False}
    schema_parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    }
    cases = {
        "schema_missing_required": (
            _probe_spec(
                "get_application",
                parameters=schema_parameters,
                decoder=_identity_decoder,
            ),
            _probe_context(),
            ToolCall("schema-call", "get_application", "{}"),
        ),
        "decode_exception": (
            _probe_spec(
                "authority_decode_probe",
                parameters=simple_parameters,
                decoder=_raise_decode,
            ),
            _probe_context(),
            ToolCall("decode-call", "authority_decode_probe", "{}"),
        ),
        "capability_missing": (
            _probe_spec(
                "authority_capability_probe",
                parameters=simple_parameters,
                decoder=_identity_decoder,
                required_capabilities=frozenset({ToolCapability.APPLICATIONS_READ}),
            ),
            _probe_context(),
            ToolCall("capability-call", "authority_capability_probe", "{}"),
        ),
        "binding_exception": (
            _probe_spec(
                "authority_binding_probe",
                parameters=simple_parameters,
                decoder=_identity_decoder,
                binding_resolvers=(_raise_binding,),
            ),
            _probe_context(),
            ToolCall("binding-call", "authority_binding_probe", "{}"),
        ),
        "preflight_returned_failure": (
            _probe_spec(
                "authority_preflight_probe",
                parameters=simple_parameters,
                decoder=_identity_decoder,
                preflight=_return_preflight_failure,
            ),
            _probe_context(),
            ToolCall("preflight-call", "authority_preflight_probe", "{}"),
        ),
    }
    for stage, (spec, context, call) in cases.items():
        expected = pipeline[stage]
        assert "sync_http" not in expected
        assert "sync" not in expected
        assert "sse" not in expected
        actual = _pipeline_projection(spec, call, context)
        assert expected["failure"] == actual["failure"]
        assert expected["rendered_message"] == actual["rendered_message"]
        assert expected["agent_tool_result"] == actual["agent_tool_result"]
        assert (
            expected["production_entrypoint"],
            expected["scenario"],
            expected["failure_origin"],
        ) == metadata[stage]


def _postheader_route_projection(outcome: Any) -> dict[str, Any]:
    assert isinstance(outcome, RuntimeFailureOutcome)
    event = ErrorEvent(
        outcome.code,
        outcome.message,
        outcome.retryable,
        outcome.degraded,
    )
    return {
        "outcome": {
            "code": outcome.code.value,
            "message": outcome.message,
            "status_code": outcome.status_code,
            "retryable": outcome.retryable,
            "degraded": outcome.degraded,
        },
        "sync_http": {
            "status": outcome_http_status(outcome),
            "body": outcome_http_payload(outcome),
        },
        "postheader_stream": {
            "http_status": 200,
            "event": event_sse_name(event),
            "data": event_sse_payload(event),
        },
    }


def test_confirmation_routes_use_phase_specific_runtime_mapping_and_transport() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    confirmation = baseline["compatibility"]["pre_executor"]["confirmation"]
    metadata = {
        "approve_stale": (
            "entrypoint.provider_confirmation_failure_v1",
            "scenario.approved_continuation_stale",
            "origin.provider_stale_pending_action",
        ),
        "modify_invalid_preheader": (
            "entrypoint.confirmation_failure_v1",
            "scenario.modify_invalid_confirmation_preheader",
            "origin.confirmation_invalid_confirmation",
        ),
        "modify_invalid_postheader": (
            "transport.encode_sse_event_error_v1",
            "scenario.modify_invalid_confirmation_postheader",
            "origin.confirmation_invalid_confirmation",
        ),
    }
    assert set(confirmation) == set(metadata)
    stale = PilotRuntime._provider_confirmation_failure(StalePendingActionError())
    stale_expected = confirmation["approve_stale"]
    stale_route = _postheader_route_projection(stale)
    assert stale_expected["outcome"] == stale_route["outcome"]
    assert stale_expected["sync_http"] == stale_route["sync_http"]
    assert stale_expected["postheader_stream"] == stale_route["postheader_stream"]
    stale_preheader = _immediate_transport_projection(
        stale,
        ConfirmationRequest(conversation_id=1, approved=True),
    )
    assert stale_expected["preheader_stream"] == stale_preheader["stream"]
    assert stale_preheader["stream"]["direct"] is False
    assert stale_preheader["stream"]["http_status"] == 200
    assert stale_preheader["stream"]["media_type"] == "text/event-stream"
    assert stale_expected["preheader_stream"]["direct"] is False
    assert (
        stale_expected["production_entrypoint"],
        stale_expected["scenario"],
        stale_expected["failure_origin"],
    ) == metadata["approve_stale"]

    invalid = PilotRuntime._confirmation_failure(
        WriteOperationError("invalid_confirmation")
    )
    invalid_preheader_expected = confirmation["modify_invalid_preheader"]
    invalid_preheader = _immediate_transport_projection(
        invalid,
        ConfirmationRequest(conversation_id=1, approved=True),
    )
    invalid_route = _postheader_route_projection(invalid)
    assert invalid_preheader_expected["outcome"] == invalid_route["outcome"]
    assert invalid_preheader_expected["sync_http"] == invalid_preheader["sync_http"]
    assert invalid_preheader_expected["sync_media_type"] == invalid_preheader["sync_media_type"]
    assert invalid_preheader["sync_media_type"] == "application/json"
    assert invalid_preheader_expected["preheader_stream"] == invalid_preheader["stream"]
    assert invalid_preheader["stream"]["direct"] is True
    assert invalid_preheader["stream"]["http_status"] == 422
    assert invalid_preheader["stream"]["media_type"] == "application/json"
    assert "sse" not in invalid_preheader["stream"]
    assert (
        invalid_preheader_expected["production_entrypoint"],
        invalid_preheader_expected["scenario"],
        invalid_preheader_expected["failure_origin"],
    ) == metadata["modify_invalid_preheader"]

    invalid_postheader_expected = confirmation["modify_invalid_postheader"]
    invalid_postheader = _postheader_error_projection(invalid)
    assert invalid_postheader_expected["postheader_stream"] == invalid_postheader
    assert invalid_postheader["http_status"] == 200
    assert (
        invalid_postheader_expected["production_entrypoint"],
        invalid_postheader_expected["scenario"],
        invalid_postheader_expected["failure_origin"],
    ) == metadata["modify_invalid_postheader"]


def _prepared_write_probe() -> tuple[ToolSpec[Any, Any], ToolExecutionContext, Any]:
    spec = replace(
        _probe_spec(
            "authority_execute_probe",
            parameters={"type": "object", "additionalProperties": False},
            decoder=_identity_decoder,
        ),
        kind="write",
        confirmation_policy="required",
        write_contract=WriteContract(),
    )
    context = _probe_context()
    catalog = ToolCatalog([spec], expected_names=(spec.name,))
    prepared_result = prepare_call(
        catalog,
        context,
        ToolCall("execute-call", spec.name, "{}"),
        pending_identity="execute-call:authority_execute_probe",
        pending_action_revision=1,
        record_proposal=False,
    )
    assert isinstance(prepared_result, ConfirmationRequired)
    return spec, context, prepared_result.prepared


def _raise_claim(_prepared: object) -> ExecutionAuthorization:
    raise _ClaimProbeError


def _mismatched_authorization(prepared: Any) -> ExecutionAuthorization:
    return ExecutionAuthorization(
        pending_identity="mismatched-authority-identity",
        pending_action_revision=prepared.pending_action_revision or 1,
        tool_call_id=prepared.tool_call_id,
        tool_name=prepared.spec.name,
        arguments_digest=prepared.arguments_digest,
    )


def _run_stale_promotion(mode: str) -> RuntimeFailureOutcome:
    spec, context, _prepared = _prepared_write_probe()
    pending = PendingAction(
        "execute-call",
        spec.name,
        "{}",
        "confirm",
        "00000000-0000-0000-0000-000000000003",
    )
    continuation = _PromotionContinuation(pending, mode)

    def operation_executor(*_args: object) -> ToolExecutionRecord[Any, Any]:
        raise AssertionError("stale confirmation must not execute")

    invocation = AgentLoopInvocation(
        seed=ApprovedWriteSeed(continuation),
        model=cast(Any, _CountingModel(Assistant(content="must not run"))),
        catalog=ToolCatalog([spec], expected_names=(spec.name,)),
        tool_context=replace(context, operation_executor=operation_executor),
        auto_approve=False,
        max_iterations=2,
        run_recorder=NullRunRecorder(),
        event_sink=_CountingEventSink(),
        runtime_signal_sink=None,
        cancel_check=None,
    )
    with pytest.raises(StalePendingActionError) as promoted:
        AgentLoopRunner().run(invocation)
    return PilotRuntime._provider_confirmation_failure(promoted.value)


def test_execute_prepared_failures_promote_to_one_verified_stale_route() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    expected_cases = baseline["compatibility"]["pre_executor"]["execute_prepared"]
    confirmation = baseline["compatibility"]["pre_executor"]["confirmation"]
    metadata = {
        "confirmation_claim_failed": (
            "entrypoint.execute_prepared_v1",
            "scenario.execute_prepared_claim_failure",
            "origin.confirmation_claim_failed",
        ),
        "authorization_mismatch": (
            "entrypoint.execute_prepared_v1",
            "scenario.execute_prepared_authorization_mismatch",
            "origin.authorization_mismatch",
        ),
    }
    assert set(expected_cases) == set(metadata)
    for mode, claimer in {
        "confirmation_claim_failed": _raise_claim,
        "authorization_mismatch": _mismatched_authorization,
    }.items():
        spec, context, prepared = _prepared_write_probe()
        record = execute_prepared(
            prepared,
            context,
            confirmation_claimer=claimer,
        )
        assert isinstance(record.outcome, ToolFailure)
        expected = expected_cases[mode]
        assert expected["failure"] == {
            "category": record.outcome.category,
            "code": record.outcome.code,
            "compatibility_detail": record.outcome.compatibility_detail,
        }
        assert expected["rendered_message"] == render_compatibility(
            spec, record.outcome
        )
        assert (
            expected["production_entrypoint"],
            expected["scenario"],
            expected["failure_origin"],
        ) == metadata[mode]
        promoted = _run_stale_promotion(mode)
        route = _postheader_route_projection(promoted)
        assert expected["public_route"] == "route.approve_stale"
        assert route == {
            key: confirmation["approve_stale"][key]
            for key in ("outcome", "sync_http", "postheader_stream")
        }


class _CountingModel:
    def __init__(self, *responses: Assistant) -> None:
        self.responses = list(responses)
        self.model_calls = 0
        self.provider_calls = 0

    def complete(
        self,
        _messages: list[Message],
        _tools: list[Any],
        _response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        self.model_calls += 1
        self.provider_calls += 1
        return self.responses.pop(0)


class _CountingEventSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


class _CountingContinuation:
    def __init__(self, pending: PendingAction) -> None:
        self._pending = pending

    @property
    def pending(self) -> PendingAction:
        return self._pending

    def claim(
        self,
        pending: PendingAction,
        prepared: object,
    ) -> ExecutionAuthorization:
        return ExecutionAuthorization(
            pending_identity=getattr(prepared, "pending_identity"),
            pending_action_revision=getattr(prepared, "pending_action_revision"),
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            arguments_digest=getattr(prepared, "arguments_digest"),
            operation_id=pending.operation_id,
        )

    def record_result(
        self,
        _pending: PendingAction,
        _tool_message: Message,
        _record: ToolExecutionRecord[Any, Any],
    ) -> None:
        return None

    def load_continuation_messages(self) -> tuple[Message, ...]:
        return (Message(role="user", content="continue"),)

    def delivery_fence(self) -> bool:
        return True


class _PromotionContinuation(_CountingContinuation):
    def __init__(self, pending: PendingAction, mode: str) -> None:
        super().__init__(pending)
        self.mode = mode

    def claim(
        self,
        pending: PendingAction,
        prepared: object,
    ) -> ExecutionAuthorization:
        if self.mode == "confirmation_claim_failed":
            return _raise_claim(prepared)
        return _mismatched_authorization(prepared)


def _counted_write_spec(counter: dict[str, int]) -> ToolSpec[Any, Any]:
    name = "authority_counted_write"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {"name": name, "description": name, "parameters": parameters},
        },
        name=name,
        description=name,
        parameters=parameters,
    )

    def execute(_args: dict[str, Any], _context: ToolExecutionContext) -> dict[str, bool]:
        counter["tool_spec_executor_calls"] += 1
        return {"ok": True}

    return ToolSpec(
        contract=contract,
        kind="write",
        decoder=lambda values: dict(values),
        executor=execute,
        confirmation_policy="required",
        write_contract=WriteContract(),
        declared_failure_categories=frozenset({"internal_error"}),
        success_renderer=str,
    )


def _run_agent_call_count_case(case: str) -> dict[str, object]:
    counter = {"tool_spec_executor_calls": 0, "operation_executor_calls": 0}
    spec = _counted_write_spec(counter)
    catalog = ToolCatalog([spec], expected_names=(spec.name,))
    sink = _CountingEventSink()
    if case == "new_turn":
        model = _CountingModel(
            Assistant(tool_calls=[ToolCall("new-call", spec.name, "{}")])
        )
        seed: NewTurnSeed | ApprovedWriteSeed = NewTurnSeed(
            (Message(role="user", content="synthetic request"),)
        )
        context = _probe_context()
    else:
        pending_args = '{"value":"changed"}' if case == "modify" else "{}"
        pending = PendingAction(
            f"{case}-call",
            spec.name,
            pending_args,
            "confirm",
            f"00000000-0000-0000-0000-00000000000{2 if case == 'modify' else 1}",
        )
        continuation = _CountingContinuation(pending)
        model = _CountingModel(Assistant(content="done"))

        def operation_executor(
            prepared: object,
            _context: object,
            authorization: ExecutionAuthorization,
        ) -> ToolExecutionRecord[Any, Any]:
            counter["operation_executor_calls"] += 1
            return ToolExecutionRecord(
                prepared=cast(Any, prepared),
                outcome=ToolSuccess({"ok": True}),
                execution_started=True,
                operation_id=authorization.operation_id,
                terminal_persisted=True,
                persisted_visible_result="saved",
                persisted_transport={"status": "success"},
            )

        seed = ApprovedWriteSeed(continuation)
        context = replace(_probe_context(), operation_executor=operation_executor)
    invocation = AgentLoopInvocation(
        seed=seed,
        model=cast(Any, model),
        catalog=catalog,
        tool_context=context,
        auto_approve=False,
        max_iterations=4,
        run_recorder=NullRunRecorder(),
        event_sink=sink,
        runtime_signal_sink=None,
        cancel_check=None,
    )
    result = AgentLoopRunner().run(invocation)
    assert isinstance(result, AgentTurnResult)
    return {
        "model_calls": model.model_calls,
        "provider_calls": model.provider_calls,
        "tool_calls": sum(isinstance(event, AgentToolCall) for event in sink.events),
        "tool_spec_executor_calls": counter["tool_spec_executor_calls"],
        "operation_executor_calls": counter["operation_executor_calls"],
        "provider_free": model.provider_calls == 0,
    }


class _AuthorityOperations:
    def __init__(
        self,
        *,
        status: str,
        delivery_status: str = "completed",
        delivery_outcome: str = "final_response",
    ) -> None:
        self.key = SimpleNamespace(key_id="authority", secret=b"k" * 32)
        self.operation_id = "00000000-0000-0000-0000-000000000001"
        self.operation = SimpleNamespace(
            id=self.operation_id,
            conversation_id=7,
            status=status,
            tool_call_id="call-1",
            tool_name="create_application",
            proposal_fingerprint="proposal",
            confirmation_token_fingerprint="",
            delivery_status=delivery_status,
        )
        self.delivery_status = delivery_status
        self.delivery_outcome = delivery_outcome
        self.replay_calls = 0
        self.converge_calls = 0

        token = "t" * 64
        self.token = token
        self.operation.confirmation_token_fingerprint = ledger_fingerprint(
            cast(Any, self.key),
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )

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
                transport_json=(
                    '{"tool_call_id":"call-1","tool_name":"create_application",'
                    '"summary":"saved","evidence":[],"affected_resources":[],"changed_entities":[]}'
                ),
                undo_json=None,
                failure_category=None,
                failure_code=None,
                digest="sha256:authority",
            ),
            self.delivery_status,
            1,
            None,
            self.delivery_outcome,
            "saved",
        )

    def converge_expired_delivery(self, _operation_id: str) -> OperationReplay:
        self.converge_calls += 1
        self.delivery_status = "completed"
        return self.replay(self.operation, "authority")

    def heartbeat(self, _ownership: DeliveryOwnership) -> bool:
        return True


class _AuthorityPersistence:
    def __init__(self, pending: PendingAction | None) -> None:
        self.pending = pending
        self.pending_reads = 0

    def get_pending_action(self, _conversation_id: int) -> PendingAction | None:
        self.pending_reads += 1
        return self.pending

    def list_messages(self, _conversation_id: int) -> tuple[object, ...]:
        return ()

    def persist_confirmation_delivery(self, **_kwargs: object) -> PersistenceResult:
        return PersistenceResult(PersistenceStatus.PERSISTED, message_ids=(1,))


class _AuthorityWriteCoordinator:
    def __init__(self) -> None:
        self.reject_calls = 0
        self.execute_calls = 0

    def reject_primary(self, **kwargs: object) -> object:
        self.reject_calls += 1
        return SimpleNamespace(
            operation_id=str(kwargs["operation_id"]),
            ownership=DeliveryOwnership(str(kwargs["operation_id"]), 1, b"owner", "owner"),
            payload=SimpleNamespace(
                status="rejected",
                visible_result="已取消这次操作。",
                undo_json=None,
            ),
        )

    def execute_primary(self, **kwargs: object) -> object:
        self.execute_calls += 1
        operation_id = str(kwargs["operation_id"])
        return (
            OperationCommitted(
                operation_id,
                TerminalPayload(
                    status="committed",
                    result_contract="typed_json_v1",
                    result_json='{"ok":true}',
                    visible_result="saved",
                    transport_json="{}",
                    undo_json=None,
                    failure_category=None,
                    failure_code=None,
                    digest="sha256:authority",
                ),
                DeliveryOwnership(operation_id, 1, b"owner", "owner"),
            ),
            SimpleNamespace(
                outcome=ToolSuccess({"ok": True}),
                terminal_persisted=True,
                replayed=False,
            ),
        )


class _AuthorityConversations:
    def __init__(self) -> None:
        self.load_calls = 0

    def load(self, _conversation_id: int) -> object:
        self.load_calls += 1
        return SimpleNamespace(id=7, archived_at=None)


def _run_ledger_call_count_case(case: str) -> dict[str, object]:
    status = "proposed" if case == "reject" else "committed"
    delivery_status = "pending" if case == "delivery_recovery" else "completed"
    delivery_outcome = "chained_pending" if case == "chained_replay" else "final_response"
    operations = _AuthorityOperations(
        status=status,
        delivery_status=delivery_status,
        delivery_outcome=delivery_outcome,
    )
    child = (
        PendingAction(
            "child-call",
            "create_application",
            "{}",
            "child",
            "00000000-0000-0000-0000-000000000009",
        )
        if case == "chained_replay"
        else None
    )
    pending = (
        PendingAction(
            "call-1",
            "create_application",
            "{}",
            "confirm",
            operations.operation_id,
        )
        if case == "reject"
        else child
    )
    persistence = _AuthorityPersistence(pending)
    write = _AuthorityWriteCoordinator()
    coordinator = ConfirmationCoordinator(
        ConfirmationDependencies(
            persistence=persistence,
            write_operations=cast(Any, operations),
            write_coordinator=cast(Any, write),
        )
    )
    counts = {"model_calls": 0, "provider_calls": 0}

    def resolve_model(_request: object, _conversation: object) -> object:
        counts["provider_calls"] += 1
        raise AssertionError("provider must not run on provider-free Ledger route")

    class Driver:
        def execute(self, _invocation: object) -> object:
            counts["model_calls"] += 1
            raise AssertionError("AgentLoop must not run on provider-free Ledger route")

    runtime = PilotRuntime(
        RuntimeDependencies(
            conversations=cast(Any, _AuthorityConversations()),
            persistence=cast(Any, persistence),
            confirmation_coordinator=coordinator,
            model_resolver=cast(Any, resolve_model),
            agent_driver=cast(Any, Driver()),
        )
    )
    outcome = runtime.continue_confirmation(
        ConfirmationRequest(
            conversation_id=7,
            approved=case != "reject",
            operation_id=operations.operation_id,
            confirmation_token=operations.token,
        ),
        invocation_control=cast(Any, InMemoryRuntimeInvocationControl()),
    )
    assert outcome is not None
    if case == "reject":
        assert write.reject_calls == 1
        assert operations.replay_calls == 0
        assert operations.converge_calls == 0
    elif case == "delivery_recovery":
        assert write.execute_calls == 0
        assert operations.converge_calls == 1
    else:
        assert write.execute_calls == 0
        assert operations.replay_calls >= 1
    return {
        "model_calls": counts["model_calls"],
        "provider_calls": counts["provider_calls"],
        "tool_calls": 0,
        "tool_spec_executor_calls": 0,
        "operation_executor_calls": write.execute_calls,
        "provider_free": counts["provider_calls"] == 0,
    }


def test_call_count_and_provider_free_baselines_are_produced_by_real_harnesses() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    assert baseline["call_count_harness"] == {
        "level": "agent_loop_and_ledger_route_harness",
        "required_gates": ["task17_pilot_runtime_approve_modify_endpoint"],
        "wire_level_endpoint_claim": False,
    }
    actual = {
        case: _run_agent_call_count_case(case)
        for case in ("new_turn", "approve", "modify")
    }
    actual.update(
        {
            case: _run_ledger_call_count_case(case)
            for case in ("reject", "final_replay", "chained_replay", "delivery_recovery")
        }
    )
    assert baseline["call_count_baselines"] == actual


def test_authority_manifest_is_the_single_ordered_typed_matrix() -> None:
    authority = load_golden("tool_authority/authority_manifest_v1.json")
    assert authority.keys() == {"schema_version", "tools"}
    assert authority["schema_version"] == 1
    tools = authority["tools"]
    assert len(tools) == len(MODEL_TOOL_NAMES) == 25
    assert tuple(item["name"] for item in tools) == MODEL_TOOL_NAMES
    assert tuple(item["ordinal"] for item in tools) == tuple(range(1, 26))
    assert all(set(item) == {"ordinal", "name", "kind", "confirmation_policy", "required_capabilities", "binding", "resolvers"} for item in tools)
    assert not set(item["name"] for item in tools) & set(LEGACY_TOOL_NAMES)

    allowed_capabilities = set(CAPABILITIES)
    allowed_binding_kinds = {
        "none",
        "enforce_if_bound",
        "scoped_collection",
        "optional_target",
        "non_application_only",
    }
    allowed_entity_kinds = {None, "application", "resume"}
    allowed_resolver_presence = {"required", "optional"}
    for item in tools:
        assert item["kind"] in {"read", "write"}
        assert item["confirmation_policy"] == (
            "required" if item["kind"] == "write" else "none"
        )
        assert len(item["required_capabilities"]) == 1
        assert set(item["required_capabilities"]) <= allowed_capabilities
        assert set(item["binding"]) == {"kind", "entity_kind"}
        assert item["binding"]["kind"] in allowed_binding_kinds
        assert item["binding"]["entity_kind"] in allowed_entity_kinds
        assert all(
            set(resolver)
            == {"resolver_id", "entity_kind", "arg_path", "presence", "identity_type"}
            for resolver in item["resolvers"]
        )
        assert all(resolver["resolver_id"] for resolver in item["resolvers"])
        assert all(
            resolver["entity_kind"] in {"application", "resume"}
            for resolver in item["resolvers"]
        )
        assert all(resolver["arg_path"] for resolver in item["resolvers"])
        assert all(
            resolver["presence"] in allowed_resolver_presence
            for resolver in item["resolvers"]
        )
        assert all(
            resolver["identity_type"] == "positive_int64"
            for resolver in item["resolvers"]
        )
    assert {item["binding"]["kind"] for item in tools} == allowed_binding_kinds
    assert {item["binding"]["entity_kind"] for item in tools} == allowed_entity_kinds
    assert {
        capability
        for item in tools
        for capability in item["required_capabilities"]
    } == allowed_capabilities


def test_policy_fingerprints_are_independent_fixed_reviewed_digests() -> None:
    policy = load_golden("tool_authority/policy_fingerprints_v1.json")
    authority = load_golden("tool_authority/authority_manifest_v1.json")
    assert policy.keys() == {"schema_version", "capability_profile", "binding_policy"}
    assert policy["schema_version"] == 1
    profile = policy["capability_profile"]
    assert profile == {
        "schema": "capability-profile-v1",
        "profile_id": "agent_typed_v1",
        "capabilities": list(CAPABILITIES),
        "fingerprint": profile["fingerprint"],
    }
    assert SHA256.fullmatch(profile["fingerprint"])
    assert profile["fingerprint"] == REVIEWED_CAPABILITY_FINGERPRINT
    profile_input = {
        "schema": profile["schema"],
        "profile_id": profile["profile_id"],
        "capabilities": profile["capabilities"],
    }
    assert profile["fingerprint"] == _digest(profile_input)

    binding = policy["binding_policy"]
    assert set(binding) == {
        "schema",
        "aggregation_rule_version",
        "collection_scope_rule_version",
        "public_denial_rule_version",
        "fingerprint",
    }
    assert binding["schema"] == "binding-policy-v1"
    assert binding["aggregation_rule_version"] == "binding-aggregation-v1"
    assert binding["collection_scope_rule_version"] == "application-collection-scope-v1"
    assert binding["public_denial_rule_version"] == "scope-denial-v1"
    assert SHA256.fullmatch(binding["fingerprint"])
    assert binding["fingerprint"] == REVIEWED_BINDING_FINGERPRINT
    semantic_tools = []
    for item in authority["tools"]:
        semantic_tools.append(
            {
                "name": item["name"],
                "tool_kind": item["kind"],
                "confirmation_policy": item["confirmation_policy"],
                "required_capabilities": item["required_capabilities"],
                "contract_kind": item["binding"]["kind"],
                "entity_kind_or_null": item["binding"]["entity_kind"],
                "resolvers": item["resolvers"],
            }
        )
    binding_input = {
        "schema": binding["schema"],
        "aggregation_rule_version": binding["aggregation_rule_version"],
        "collection_scope_rule_version": binding["collection_scope_rule_version"],
        "public_denial_rule_version": binding["public_denial_rule_version"],
        "tools": semantic_tools,
    }
    assert binding["fingerprint"] == _digest(binding_input)


def test_dependency_closure_manifest_pins_current_catalog_coverage() -> None:
    baseline = load_golden("tool_authority/baseline_2427fa6.json")
    from offerpilot.context_projector.selector import _DEPENDENCIES

    closure = baseline["dependency_closure"]
    assert closure["dependency_policy_version"] == "dependency-policy-v1"
    assert tuple(closure["catalog_names"]) == MODEL_TOOL_NAMES
    assert closure["coverage"] == 25
    expected = {
        name: sorted(_DEPENDENCIES.get(name, ())) for name in MODEL_TOOL_NAMES
    }
    assert closure["dependencies"] == expected
    assert closure["canonical_sha256"] == _digest(
        {
            "dependency_policy_version": closure["dependency_policy_version"],
            "catalog_names": closure["catalog_names"],
            "dependencies": closure["dependencies"],
        }
    )


def test_golden_loader_has_only_reading_helpers_and_no_update_mechanism() -> None:
    source_path = Path(__file__).with_name("golden.py")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function_names == {"canonical_json", "load_golden"}
    assert not any(token in source for token in ("write_text", "write_bytes", "open("))
