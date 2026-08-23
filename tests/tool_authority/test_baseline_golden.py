from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

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
    assert tuple(baseline["model_tool_names"]) == MODEL_TOOL_NAMES
    assert tuple(baseline["legacy_tool_names"]) == LEGACY_TOOL_NAMES
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

    expected = {
        "list_applications": ("read", "scoped_collection", "application", ("applications.read",)),
        "get_application": ("read", "enforce_if_bound", "application", ("applications.read",)),
        "create_application": ("write", "non_application_only", None, ("applications.write",)),
        "update_application_status": ("write", "enforce_if_bound", "application", ("applications.write",)),
        "list_application_events": ("read", "scoped_collection", "application", ("application_events.read",)),
        "get_application_event": ("read", "enforce_if_bound", "application", ("application_events.read",)),
        "create_application_event": ("write", "enforce_if_bound", "application", ("application_events.write",)),
        "update_application_event": ("write", "enforce_if_bound", "application", ("application_events.write",)),
        "delete_application_event": ("write", "enforce_if_bound", "application", ("application_events.write",)),
        "list_notes": ("read", "scoped_collection", "application", ("notes.read",)),
        "add_note": ("write", "optional_target", "application", ("notes.write",)),
        "update_note": ("write", "enforce_if_bound", "application", ("notes.write",)),
        "delete_note": ("write", "enforce_if_bound", "application", ("notes.write",)),
        "list_offers": ("read", "scoped_collection", "application", ("offers.read",)),
        "get_offer": ("read", "enforce_if_bound", "application", ("offers.read",)),
        "compare_offers": ("read", "non_application_only", None, ("offers.read",)),
        "update_offer": ("write", "enforce_if_bound", "application", ("offers.write",)),
        "save_offer_assessment": ("write", "enforce_if_bound", "application", ("offers.write",)),
        "list_resumes": ("read", "none", None, ("resumes.read",)),
        "get_resume": ("read", "enforce_if_bound", "resume", ("resumes.read",)),
        "resume_update_career_intent": ("write", "enforce_if_bound", "resume", ("resumes.write",)),
        "resume_rewrite_highlight": ("write", "enforce_if_bound", "resume", ("resumes.write",)),
        "list_resume_matches": ("read", "enforce_if_bound", "resume", ("resumes.read",)),
        "list_jd_analyses": ("read", "scoped_collection", "application", ("jd_analyses.read",)),
        "get_jd_analysis": ("read", "enforce_if_bound", "application", ("jd_analyses.read",)),
    }
    for item in tools:
        kind, binding_kind, entity_kind, capabilities = expected[item["name"]]
        assert item["kind"] == kind
        assert item["confirmation_policy"] == ("required" if kind == "write" else "none")
        assert tuple(item["required_capabilities"]) == capabilities
        assert item["binding"] == {"kind": binding_kind, "entity_kind": entity_kind}
        assert all(set(resolver) == {"resolver_id", "entity_kind", "arg_path", "presence", "identity_type"} for resolver in item["resolvers"])
        assert all(resolver["identity_type"] == "positive_int64" for resolver in item["resolvers"])

    resolver_expectations = {
        "get_application": [("application_identity_arg", "application", "id", "required")],
        "update_application_status": [("application_identity_arg", "application", "id", "required")],
        "list_application_events": [("application_identity_arg", "application", "application_id", "optional")],
        "get_application_event": [("application_event_parent", "application", "id", "required")],
        "create_application_event": [("application_identity_arg", "application", "application_id", "required")],
        "update_application_event": [
            ("application_event_parent", "application", "id", "required"),
            ("application_identity_arg", "application", "application_id", "required"),
        ],
        "delete_application_event": [("application_event_parent", "application", "id", "required")],
        "list_notes": [("application_identity_arg", "application", "application_id", "optional")],
        "add_note": [("application_identity_arg", "application", "application_id", "optional")],
        "update_note": [
            ("note_application_parent", "application", "id", "required"),
            ("application_identity_arg", "application", "application_id", "optional"),
        ],
        "delete_note": [("note_application_parent", "application", "id", "required")],
        "get_offer": [("offer_application_parent", "application", "id", "required")],
        "update_offer": [("offer_application_parent", "application", "id", "required")],
        "save_offer_assessment": [("offer_application_parent", "application", "id", "required")],
        "get_resume": [("resume_identity_arg", "resume", "id", "required")],
        "resume_update_career_intent": [("resume_identity_arg", "resume", "id", "required")],
        "resume_rewrite_highlight": [("resume_identity_arg", "resume", "id", "required")],
        "list_resume_matches": [("resume_identity_arg", "resume", "resume_id", "required")],
        "list_jd_analyses": [("application_identity_arg", "application", "application_id", "optional")],
        "get_jd_analysis": [("jd_analysis_application_parent", "application", "id", "required")],
    }
    for item in tools:
        actual = [
            (r["resolver_id"], r["entity_kind"], r["arg_path"], r["presence"])
            for r in item["resolvers"]
        ]
        assert actual == resolver_expectations.get(item["name"], [])


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
