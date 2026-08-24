from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from offerpilot.ai.tool_authority.policy import (
    AGENT_TYPED_V1_PROFILE,
    APPLICATION_COLLECTION_SCOPE_VERSION,
    BINDING_AGGREGATION_VERSION,
    BINDING_POLICY_VERSION,
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
    EXPECTED_CAPABILITIES,
    PROFILE_ID,
    SCOPE_DENIAL_VERSION,
    AuthorityPolicyError,
    BindingDecision,
    CapabilityProfile,
    binding_policy_fingerprint,
    binding_policy_input,
    capability_profile_fingerprint,
    capability_profile_input,
    decide_binding,
    validate_capability_profile,
    validate_startup_policy,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "tool_authority"


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURES / "authority_manifest_v1.json").read_text(encoding="utf-8"))


def _reviewed_policy() -> dict[str, Any]:
    return json.loads((FIXTURES / "policy_fingerprints_v1.json").read_text(encoding="utf-8"))


def _resolution(
    state: str,
    identity: int | None = None,
    *,
    presence: str = "required",
    entity_kind: str = "application",
) -> SimpleNamespace:
    return SimpleNamespace(
        entity_kind=entity_kind,
        state=state,
        identity=identity,
        presence=presence,
    )


def test_profile_and_six_policy_versions_are_closed_and_exact() -> None:
    assert PROFILE_ID == "agent_typed_v1"
    assert CAPABILITY_POLICY_VERSION == "capability-policy-v1"
    assert BINDING_POLICY_VERSION == "binding-policy-v1"
    assert BINDING_AGGREGATION_VERSION == "binding-aggregation-v1"
    assert APPLICATION_COLLECTION_SCOPE_VERSION == "application-collection-scope-v1"
    assert SCOPE_DENIAL_VERSION == "scope-denial-v1"
    assert DEPENDENCY_POLICY_VERSION == "dependency-policy-v1"
    assert EXPECTED_CAPABILITIES == (
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
    assert AGENT_TYPED_V1_PROFILE.profile_id == PROFILE_ID
    assert AGENT_TYPED_V1_PROFILE.capabilities == EXPECTED_CAPABILITIES
    assert AGENT_TYPED_V1_PROFILE.capability_policy_version == CAPABILITY_POLICY_VERSION


def test_profile_fingerprint_matches_independent_reviewed_fixture() -> None:
    fixture = json.loads(
        (FIXTURES / "capability_profile_agent_typed_v1.json").read_text(encoding="utf-8")
    )
    assert capability_profile_input(AGENT_TYPED_V1_PROFILE) == {
        "schema": "capability-profile-v1",
        "profile_id": PROFILE_ID,
        "capabilities": list(EXPECTED_CAPABILITIES),
    }
    assert capability_profile_fingerprint(AGENT_TYPED_V1_PROFILE) == fixture["fingerprint"]
    assert validate_capability_profile(AGENT_TYPED_V1_PROFILE, fixture["fingerprint"]) is AGENT_TYPED_V1_PROFILE


@pytest.mark.parametrize(
    ("capabilities", "error"),
    [
        ((), ValueError),
        (("applications.read",), ValueError),
        (("applications.read", "future.read"), ValueError),
        (("applications.read", "applications.read"), ValueError),
        (("applications.write", *EXPECTED_CAPABILITIES[2:]), ValueError),
    ],
)
def test_profile_rejects_empty_partial_unknown_duplicate_and_reordered_sets(
    capabilities: tuple[str, ...], error: type[Exception]
) -> None:
    profile = CapabilityProfile(
        profile_id=PROFILE_ID,
        capabilities=capabilities,
        capability_policy_version=CAPABILITY_POLICY_VERSION,
    )
    with pytest.raises(error):
        validate_capability_profile(profile)


def test_future_tool_capability_enum_member_does_not_enter_old_profile() -> None:
    class FutureCapability:
        value = "future.read"

    assert AGENT_TYPED_V1_PROFILE.capabilities == EXPECTED_CAPABILITIES
    with pytest.raises(AuthorityPolicyError):
        CapabilityProfile.from_capabilities(
            [*EXPECTED_CAPABILITIES, FutureCapability()],
            profile_id=PROFILE_ID,
            capability_policy_version=CAPABILITY_POLICY_VERSION,
        )


@pytest.mark.parametrize(
    "field",
    ["profile_id", "capability_policy_version", "capabilities"],
)
def test_profile_identity_drift_fails_closed(field: str) -> None:
    values = {
        "profile_id": PROFILE_ID,
        "capability_policy_version": CAPABILITY_POLICY_VERSION,
        "capabilities": EXPECTED_CAPABILITIES,
    }
    values[field] = "wrong-v1" if field != "capabilities" else EXPECTED_CAPABILITIES[:-1]
    profile = CapabilityProfile(**values)
    with pytest.raises(AuthorityPolicyError):
        validate_capability_profile(profile)


def test_binding_policy_input_uses_one_ordered_manifest_projection() -> None:
    manifest = _manifest()
    value = binding_policy_input(manifest)
    assert value["schema"] == "binding-policy-v1"
    assert value["aggregation_rule_version"] == BINDING_AGGREGATION_VERSION
    assert value["collection_scope_rule_version"] == APPLICATION_COLLECTION_SCOPE_VERSION
    assert value["public_denial_rule_version"] == SCOPE_DENIAL_VERSION
    assert len(value["tools"]) == 25
    assert set(value["tools"][0]) == {
        "name",
        "tool_kind",
        "confirmation_policy",
        "required_capabilities",
        "contract_kind",
        "entity_kind_or_null",
        "resolvers",
    }
    assert value["tools"][7]["resolvers"][0]["resolver_id"] == "application_event_parent"
    assert binding_policy_fingerprint(manifest) == _reviewed_policy()["binding_policy"]["fingerprint"]


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_manifest_schema_version_requires_exact_integer(schema_version: object) -> None:
    manifest = _manifest()
    manifest["schema_version"] = schema_version
    with pytest.raises(AuthorityPolicyError):
        validate_startup_policy(manifest, expected_policy=_reviewed_policy())


@pytest.mark.parametrize("ordinal", [True, 1.0, "1"])
def test_manifest_ordinal_requires_exact_integer(ordinal: object) -> None:
    manifest = _manifest()
    manifest["tools"][0]["ordinal"] = ordinal
    with pytest.raises(AuthorityPolicyError):
        validate_startup_policy(manifest, expected_policy=_reviewed_policy())


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("tools", 0, "kind"), "write"),
        (("tools", 0, "confirmation_policy"), "required"),
        (("tools", 0, "required_capabilities", 0), "future.read"),
        (("tools", 0, "binding", "kind"), "none"),
        (("tools", 1, "resolvers", 0, "resolver_id"), "future_resolver"),
        (("tools", 1, "resolvers", 0, "arg_path"), "other_id"),
        (("tools", 1, "resolvers", 0, "presence"), "optional"),
        (("tools", 1, "resolvers", 0, "identity_type"), "string"),
        (("tools", 1, "resolvers", 0, "entity_kind"), "resume"),
    ],
)
def test_each_manifest_semantic_mutation_fails_startup(
    path: tuple[object, ...], replacement: object
) -> None:
    manifest = _manifest()
    target: Any = manifest
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    with pytest.raises(AuthorityPolicyError):
        validate_startup_policy(manifest, expected_policy=_reviewed_policy())


@pytest.mark.parametrize(
    ("keyword", "replacement"),
    [
        ("binding_aggregation_version", "binding-aggregation-v2"),
        ("collection_scope_version", "application-collection-scope-v2"),
        ("public_denial_version", "scope-denial-v2"),
    ],
)
def test_each_binding_rule_version_mutation_fails_startup(
    keyword: str, replacement: str
) -> None:
    with pytest.raises(AuthorityPolicyError):
        validate_startup_policy(
            _manifest(), expected_policy=_reviewed_policy(), **{keyword: replacement}
        )


def test_startup_policy_validates_both_independent_golden_digests() -> None:
    result = validate_startup_policy(_manifest(), expected_policy=_reviewed_policy())
    assert result.capability_profile is AGENT_TYPED_V1_PROFILE
    assert result.binding_policy_fingerprint == _reviewed_policy()["binding_policy"]["fingerprint"]


def test_binding_truth_table_none_and_non_application_only() -> None:
    none = decide_binding("none", scope_bound=True, bound_identities={37}, resolutions=[])
    assert none == BindingDecision(status="unbound", allowed=True, reason=None)
    allowed = decide_binding(
        "non_application_only", scope_bound=False, bound_identities=set(), resolutions=[]
    )
    assert allowed.allowed is True
    denied = decide_binding(
        "non_application_only", scope_bound=True, bound_identities={37}, resolutions=[]
    )
    assert denied.allowed is False
    assert denied.reason == "scope_forbidden"


@pytest.mark.parametrize("contract", ["enforce_if_bound", "scoped_collection", "optional_target"])
@pytest.mark.parametrize("state", ["resolved", "omitted", "detached", "unavailable"])
def test_binding_truth_table_unbound_scope_is_fail_safe_for_non_application_scope(
    contract: str, state: str
) -> None:
    resolution = _resolution(
        state,
        identity=37 if state == "resolved" else None,
        presence="required" if contract != "optional_target" else "optional",
    )
    decision = decide_binding(
        contract, scope_bound=False, bound_identities=set(), resolutions=[resolution]
    )
    assert decision.allowed is True
    assert decision.status == "unbound"


@pytest.mark.parametrize("contract", ["enforce_if_bound", "scoped_collection", "optional_target"])
def test_binding_truth_table_bound_scope_matches_and_rejects_identity(
    contract: str,
) -> None:
    matched = decide_binding(
        contract,
        scope_bound=True,
        bound_identities={37},
        resolutions=[_resolution("resolved", 37)],
    )
    mismatched = decide_binding(
        contract,
        scope_bound=True,
        bound_identities={37},
        resolutions=[_resolution("resolved", 38)],
    )
    assert matched == BindingDecision(status="matched", allowed=True, reason=None)
    assert mismatched == BindingDecision(
        status="mismatched", allowed=False, reason="binding_mismatched"
    )


@pytest.mark.parametrize("state", ["omitted", "detached", "unavailable"])
def test_required_bound_target_is_unavailable(state: str) -> None:
    decision = decide_binding(
        "enforce_if_bound",
        scope_bound=True,
        bound_identities={37},
        resolutions=[_resolution(state)],
    )
    reason = "binding_detached" if state == "detached" else "binding_unavailable"
    assert decision == BindingDecision(
        status="unavailable", allowed=False, reason=reason
    )


def test_optional_omitted_is_ignored_but_explicit_unavailable_is_denied() -> None:
    omitted = decide_binding(
        "optional_target",
        scope_bound=True,
        bound_identities={37},
        resolutions=[_resolution("omitted", presence="optional")],
    )
    unavailable = decide_binding(
        "optional_target",
        scope_bound=True,
        bound_identities={37},
        resolutions=[_resolution("unavailable", presence="optional")],
    )
    assert omitted == BindingDecision(status="matched", allowed=True, reason=None)
    assert unavailable == BindingDecision(
        status="unavailable", allowed=False, reason="binding_unavailable"
    )


def test_binding_policy_rejects_unknown_contract_and_mixed_entity_resolution() -> None:
    with pytest.raises(AuthorityPolicyError):
        decide_binding("future_contract", scope_bound=False, bound_identities=set(), resolutions=[])
    with pytest.raises(AuthorityPolicyError):
        decide_binding(
            "enforce_if_bound",
            scope_bound=True,
            bound_identities={37},
            resolutions=[
                _resolution("resolved", 37, entity_kind="application"),
                _resolution("resolved", 37, entity_kind="resume"),
            ],
        )


def test_policy_does_not_offer_a_writer_or_mutate_inputs() -> None:
    source = _manifest()
    before = copy.deepcopy(source)
    binding_policy_input(source)
    assert source == before
    policy_source = Path(__file__).with_name("golden.py").read_text(encoding="utf-8")
    assert "write_text" not in policy_source
    assert "write_bytes" not in policy_source
