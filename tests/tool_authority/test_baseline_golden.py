from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from offerpilot.ai.agent_contracts import AgentToolResult, StalePendingActionError
from offerpilot.ai.agent_loop import _delivery_error_payload
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingTarget,
    ProviderToolContract,
    ToolFailure,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.pipeline import Rejected, prepare_call
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.types import ToolCall
from offerpilot.ai.write_operations import WriteOperationError
from offerpilot.agent_runtime.journal import NullRunRecorder
from offerpilot.chat_transport import (
    event_sse_name,
    event_sse_payload,
    outcome_http_payload,
    outcome_http_status,
)
from offerpilot.pilot_runtime.contracts import ErrorEvent, RuntimeFailureOutcome
from offerpilot.pilot_runtime.service import PilotRuntime

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
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        assert not FORBIDDEN_KEYS & lowered
        for nested in value.values():
            _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fixture_identity(name: str) -> dict[str, Any]:
    path = Path(__file__).parents[1] / "fixtures" / name
    raw = path.read_bytes()
    value = json.loads(raw)
    return {
        "raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": _digest(value),
        "baseline": value.get("baseline"),
        "source_baseline": value.get("source_baseline"),
    }


def _load_existing(name: str) -> Any:
    return json.loads((Path(__file__).parents[1] / "fixtures" / name).read_text(encoding="utf-8"))


def test_authority_assets_are_canonical_private_and_pinned() -> None:
    names = (
        "baseline_2427fa6.json",
        "authority_manifest_v1.json",
        "policy_fingerprints_v1.json",
    )
    for name in names:
        value = load_golden(name)
        raw = (FIXTURES / name).read_text(encoding="utf-8")
        assert raw == canonical_json(value) + "\n"
        assert not WINDOWS_ABSOLUTE_PATH.search(raw)
        assert "SQLite format 3" not in raw
        assert "Traceback (most recent call last)" not in raw
        for canary in REAL_USER_CANARIES:
            assert canary not in raw
        _walk(value)


def test_baseline_references_exact_repository_and_production() -> None:
    baseline = load_golden("baseline_2427fa6.json")
    assert baseline["schema_version"] == 1
    assert baseline["repository_baseline"] == "1574d0e891391c817c325f598b4f22f8a783833"
    assert baseline["production_baseline"] == "2427fa6"
    assert BASELINE == baseline["production_baseline"]
    assert tuple(baseline["typed_tool_names"]) == MODEL_TOOL_NAMES
    assert tuple(baseline["legacy_deterministic_names"]) == LEGACY_TOOL_NAMES
    assert not set(MODEL_TOOL_NAMES) & set(LEGACY_TOOL_NAMES)


def test_provider_envelopes_and_schema_fingerprints_are_fully_pinned() -> None:
    baseline = load_golden("baseline_2427fa6.json")
    provider = _load_existing("tool_pipeline/provider_manifest_30c944f.json")
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


def test_existing_fixture_identities_and_compatibility_facts_are_pinned() -> None:
    baseline = load_golden("baseline_2427fa6.json")
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

    pilot = _load_existing("pilot_runtime/baseline_golden.json")
    agent = _load_existing("agent_loop/baseline_aaecf5d.json")
    compatibility = baseline["compatibility"]
    assert compatibility["preheader_http"] == {
        "source_load_failed": {
            "error_code": "source_load_failed",
            "message": "上下文暂时无法加载，请稍后重试。",
            "status": 503,
        },
        "stale_pending_action": {
            "error_code": "stale_pending_action",
            "message": "待确认操作已过期或正在处理中，请刷新对话后重试。",
            "status": 409,
        },
    }
    assert compatibility["replay_http"] == {
        "operation_delivery_pending": {"error_code": "operation_delivery_pending", "status": 409},
        "operation_delivery_unknown": {"error_code": "operation_delivery_unknown", "status": 503},
        "operation_integrity_error": {"error_code": "operation_integrity_error", "status": 409},
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
    baseline = load_golden("baseline_2427fa6.json")
    pipeline = baseline["compatibility"]["pre_executor"]["tool_pipeline"]
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
        assert isinstance(expected["production_entrypoint"], str)
        assert isinstance(expected["scenario"], str)
        assert expected["failure_origin"] in {
            "schema_missing_required",
            "decoder_exception",
            "missing_capability",
            "binding_resolver_exception",
            "preflight_returned_tool_failure",
        }


def _confirmation_route_projection(outcome: Any) -> dict[str, Any]:
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
        "sse": {
            "http_status": 200,
            "event": event_sse_name(event),
            "data": event_sse_payload(event),
        },
    }


def test_confirmation_routes_use_runtime_mapping_and_transport() -> None:
    baseline = load_golden("baseline_2427fa6.json")
    confirmation = baseline["compatibility"]["pre_executor"]["confirmation"]
    cases = {
        "approve_stale": PilotRuntime._provider_confirmation_failure(
            StalePendingActionError()
        ),
        "modify_invalid": PilotRuntime._confirmation_failure(
            WriteOperationError("invalid_confirmation")
        ),
    }
    for action, outcome in cases.items():
        expected = confirmation[action]
        actual = _confirmation_route_projection(outcome)
        assert expected["outcome"] == actual["outcome"]
        assert expected["sync_http"] == actual["sync_http"]
        assert expected["sse"] == actual["sse"]
        assert expected["sse"]["http_status"] == 200
        assert expected["sse"]["event"] == "error"
        assert isinstance(expected["production_entrypoint"], str)
        assert isinstance(expected["scenario"], str)


def test_call_count_and_provider_free_baselines_are_explicit() -> None:
    baseline = load_golden("baseline_2427fa6.json")
    assert baseline["call_count_baselines"] == {
        "new_turn": {"model_calls": 1, "provider_calls": 1, "tool_calls": 1, "provider_free": False},
        "approve": {"model_calls": 1, "provider_calls": 1, "tool_calls": 1, "provider_free": False},
        "modify": {"model_calls": 1, "provider_calls": 1, "tool_calls": 1, "provider_free": False},
        "reject": {"model_calls": 0, "provider_calls": 0, "tool_calls": 0, "provider_free": True},
        "final_replay": {"model_calls": 0, "provider_calls": 0, "tool_calls": 0, "provider_free": True},
        "chained_replay": {"model_calls": 0, "provider_calls": 0, "tool_calls": 0, "provider_free": True},
        "delivery_recovery": {"model_calls": 0, "provider_calls": 0, "tool_calls": 0, "provider_free": True},
    }


def test_authority_manifest_is_the_single_ordered_typed_matrix() -> None:
    authority = load_golden("authority_manifest_v1.json")
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
    policy = load_golden("policy_fingerprints_v1.json")
    authority = load_golden("authority_manifest_v1.json")
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
    baseline = load_golden("baseline_2427fa6.json")
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
