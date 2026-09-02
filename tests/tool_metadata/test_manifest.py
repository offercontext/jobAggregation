from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

from offerpilot.ai.tool_runtime.catalog import (
    ToolMetadataManifestV1,
    compile_tool_metadata_manifest,
    validate_tool_metadata_manifest,
)
from offerpilot.ai.tool_runtime.metadata import freeze_json
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog

from .golden import load_asset


_TEST_TOOL_CATALOG = build_model_tool_catalog()

TOP_LEVEL_KEYS = (
    "schema_version",
    "metadata_version",
    "catalog_profile",
    "typed_tools",
    "discovery_policy",
    "legacy_boundary",
    "compensation_operation_order",
)
TYPED_TOOL_KEYS = (
    "ordinal",
    "provider_name",
    "provider_contract_fingerprint",
    "domains",
    "dependencies",
    "provider_visibility",
    "required_capabilities",
    "binding",
    "confirmation_policy",
    "editable_fields",
    "operation",
)
WRITE_OPERATION_KEYS = (
    "kind",
    "adapter_kind",
    "result_contract",
    "result_bytes",
    "visible_bytes",
    "transport_bytes",
    "undo_bytes",
    "undo_policy",
    "undo_payload_kind",
    "compensation_kind",
    "undo_contract_version",
    "undo_builder_id",
    "undo_seed_phase",
)
DISCOVERY_POLICY_KEYS = (
    "selector_version",
    "discovery_policy_version",
    "page_domains",
    "attachment_domains",
    "lexical_rules",
    "no_signal_behavior",
    "invalid_input_behavior",
)
PAGE_DOMAIN_KEYS = ("page_kind", "domains")
ATTACHMENT_DOMAIN_KEYS = ("attachment_kind", "domains")
LEXICAL_RULE_KEYS = ("domain", "terms")
LEGACY_BOUNDARY_KEYS = (
    "boundary_version",
    "provider_visibility",
    "adapter_kind",
    "ordered_names",
    "chained_policies",
    "initial_route_bindings",
)
INITIAL_ROUTE_BINDING_KEYS = ("route_source", "adapter_ordinal")
COMPENSATION_OPERATION_ORDER = (
    "undo:update_application_status",
    "undo:create_application",
    "undo:create_application_event",
    "undo:add_note",
    "undo:create_offer",
)

Path = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class ManifestMutation:
    name: str
    path: Path
    replacement: object


_DROP_LAST = object()
_DUPLICATE_FIRST = object()
_REVERSE = object()
_APPEND_FIRST = object()


def _mutation(name: str, path: Path, replacement: object) -> ManifestMutation:
    return ManifestMutation(name, path, replacement)


def _at(value: object, path: Path) -> Any:
    current = value
    for part in path:
        current = current[part]  # type: ignore[index]
    return current


def _replace_at(root: dict[str, Any], path: Path, replacement: object) -> None:
    parent = _at(root, path[:-1])
    current = parent[path[-1]]
    if replacement is _DROP_LAST:
        replacement = current[:-1]
    elif replacement is _DUPLICATE_FIRST:
        replacement = [*current, copy.deepcopy(current[0])]
    elif replacement is _REVERSE:
        replacement = list(reversed(current))
    elif replacement is _APPEND_FIRST:
        replacement = [*current, copy.deepcopy(current[0])]
    parent[path[-1]] = copy.deepcopy(replacement)


OBJECT_PATHS: tuple[tuple[str, Path], ...] = (
    ("top-level", ()),
    ("typed-tool", ("typed_tools", 0)),
    ("binding", ("typed_tools", 1, "binding")),
    ("binding-contract", ("typed_tools", 1, "binding", "contract")),
    ("resolver", ("typed_tools", 1, "binding", "resolver_descriptors", 0)),
    ("editable-field", ("typed_tools", 2, "editable_fields", 0)),
    ("read-operation", ("typed_tools", 0, "operation")),
    ("write-operation", ("typed_tools", 2, "operation")),
    ("discovery-policy", ("discovery_policy",)),
    ("page-domain", ("discovery_policy", "page_domains", 0)),
    ("attachment-domain", ("discovery_policy", "attachment_domains", 0)),
    ("lexical-rule", ("discovery_policy", "lexical_rules", 0)),
    ("legacy-boundary", ("legacy_boundary",)),
    ("initial-route-binding", ("legacy_boundary", "initial_route_bindings", 0)),
)


def _shape_params() -> tuple[object, ...]:
    params: list[object] = []
    for label, path in OBJECT_PATHS:
        for shape in ("missing", "extra", "reordered"):
            if label == "read-operation" and shape == "reordered":
                continue
            params.append(pytest.param(path, shape, id=f"{label}-{shape}"))
    return tuple(params)


VALUE_MUTATIONS = (
    # Closed top-level values and exact primitive types.
    _mutation("schema-version-drift", ("schema_version",), 2),
    _mutation("schema-version-bool", ("schema_version",), True),
    _mutation("metadata-version-drift", ("metadata_version",), "tool-surface-metadata-v2"),
    _mutation("catalog-profile-drift", ("catalog_profile",), "agent_typed_v2"),
    # Exact Typed cardinality and ordinal topology.
    _mutation("typed-count-short", ("typed_tools",), _DROP_LAST),
    _mutation("typed-count-long", ("typed_tools",), _APPEND_FIRST),
    _mutation("ordinal-gap", ("typed_tools", 3, "ordinal"), 99),
    _mutation("ordinal-duplicate", ("typed_tools", 1, "ordinal"), 1),
    _mutation("ordinal-text", ("typed_tools", 0, "ordinal"), "1"),
    _mutation("ordinal-bool", ("typed_tools", 0, "ordinal"), True),
    # Domains and dependency closure.
    _mutation("domains-empty", ("typed_tools", 0, "domains"), []),
    _mutation("domains-duplicate", ("typed_tools", 0, "domains"), ["applications", "applications"]),
    _mutation("domains-noncanonical", ("typed_tools", 0, "domains"), ["events", "applications"]),
    _mutation("domains-unknown", ("typed_tools", 0, "domains"), ["unknown_domain"]),
    _mutation(
        "dependency-duplicate", ("typed_tools", 15, "dependencies"), ["get_offer", "get_offer"]
    ),
    _mutation("dependency-non-text", ("typed_tools", 1, "dependencies"), [1]),
    _mutation(
        "dependency-noncanonical", ("typed_tools", 15, "dependencies"), ["list_offers", "get_offer"]
    ),
    _mutation("dependency-self", ("typed_tools", 0, "dependencies"), ["list_applications"]),
    _mutation(
        "dependency-unknown", ("typed_tools", 0, "dependencies"), ["unknown_typed_dependency"]
    ),
    _mutation(
        "dependency-legacy", ("typed_tools", 0, "dependencies"), ["save_application_jd_version"]
    ),
    # Provider identity and capability closure, including same-shape drift.
    _mutation("provider-name-empty", ("typed_tools", 0, "provider_name"), ""),
    _mutation("provider-name-non-text", ("typed_tools", 0, "provider_name"), 1),
    _mutation(
        "fingerprint-malformed", ("typed_tools", 0, "provider_contract_fingerprint"), "sha256:nope"
    ),
    _mutation("fingerprint-non-text", ("typed_tools", 0, "provider_contract_fingerprint"), 1),
    _mutation("provider-visibility-drift", ("typed_tools", 0, "provider_visibility"), "forbidden"),
    _mutation("provider-visibility-non-text", ("typed_tools", 0, "provider_visibility"), 1),
    _mutation("capability-empty", ("typed_tools", 0, "required_capabilities"), []),
    _mutation(
        "capability-many",
        ("typed_tools", 0, "required_capabilities"),
        ["applications.read", "applications.write"],
    ),
    _mutation(
        "capability-unknown", ("typed_tools", 0, "required_capabilities"), ["applications.admin"]
    ),
    _mutation("capability-non-text", ("typed_tools", 0, "required_capabilities"), [1]),
    # Binding contract and resolver combinations must match the Provider schema exactly.
    _mutation("binding-kind-empty", ("typed_tools", 1, "binding", "contract", "kind"), ""),
    _mutation("binding-kind-unknown", ("typed_tools", 1, "binding", "contract", "kind"), "unknown"),
    _mutation(
        "binding-kind-valid-mismatch",
        ("typed_tools", 1, "binding", "contract", "kind"),
        "scoped_collection",
    ),
    _mutation("binding-entity-empty", ("typed_tools", 1, "binding", "contract", "entity_kind"), ""),
    _mutation(
        "binding-entity-unknown", ("typed_tools", 1, "binding", "contract", "entity_kind"), "offer"
    ),
    _mutation(
        "binding-entity-valid-mismatch",
        ("typed_tools", 1, "binding", "contract", "entity_kind"),
        "resume",
    ),
    _mutation(
        "binding-entity-null-mismatch",
        ("typed_tools", 1, "binding", "contract", "entity_kind"),
        None,
    ),
    _mutation(
        "unbound-entity-nonnull",
        ("typed_tools", 18, "binding", "contract", "entity_kind"),
        "resume",
    ),
    _mutation(
        "resolver-id-empty",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "resolver_id"),
        "",
    ),
    _mutation(
        "resolver-id-unknown",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "resolver_id"),
        "unknown_resolver",
    ),
    _mutation(
        "resolver-entity-unknown",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "entity_kind"),
        "offer",
    ),
    _mutation(
        "resolver-entity-mismatch",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "entity_kind"),
        "resume",
    ),
    _mutation(
        "resolver-arg-path-empty",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "arg_path"),
        "",
    ),
    _mutation(
        "resolver-arg-path-not-identifier",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "arg_path"),
        "application.id",
    ),
    _mutation(
        "resolver-presence-unknown",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "presence"),
        "sometimes",
    ),
    _mutation(
        "resolver-presence-empty",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "presence"),
        "",
    ),
    _mutation(
        "resolver-identity-unknown",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "identity_type"),
        "uuid",
    ),
    _mutation(
        "resolver-identity-empty",
        ("typed_tools", 1, "binding", "resolver_descriptors", 0, "identity_type"),
        "",
    ),
    _mutation(
        "resolver-count-long-duplicate",
        ("typed_tools", 1, "binding", "resolver_descriptors"),
        _DUPLICATE_FIRST,
    ),
    # Editable fields are a closed projection of exact Provider top-level properties.
    _mutation("editable-duplicate", ("typed_tools", 2, "editable_fields"), _DUPLICATE_FIRST),
    _mutation(
        "editable-value-type-unknown",
        ("typed_tools", 2, "editable_fields", 0, "value_type"),
        "object",
    ),
    _mutation("non-enum-options", ("typed_tools", 2, "editable_fields", 0, "options"), ["x"]),
    _mutation("enum-options-empty", ("typed_tools", 2, "editable_fields", 3, "options"), []),
    _mutation(
        "enum-options-duplicate",
        ("typed_tools", 2, "editable_fields", 3, "options"),
        ["pending", "pending"],
    ),
    _mutation(
        "enum-options-nonscalar", ("typed_tools", 2, "editable_fields", 3, "options"), [["pending"]]
    ),
    _mutation("clearable-non-bool", ("typed_tools", 2, "editable_fields", 0, "clearable"), 1),
    _mutation(
        "nonclearable-nonnull-clear-value",
        ("typed_tools", 2, "editable_fields", 0, "clear_value"),
        "",
    ),
    # Operation discriminator, compatibility projection, and exact integer byte budgets.
    _mutation("confirmation-unknown", ("typed_tools", 0, "confirmation_policy"), "optional"),
    _mutation("read-confirmation-mismatch", ("typed_tools", 0, "confirmation_policy"), "required"),
    _mutation("write-confirmation-mismatch", ("typed_tools", 2, "confirmation_policy"), "none"),
    _mutation("operation-kind-unknown", ("typed_tools", 0, "operation", "kind"), "unknown"),
    _mutation("operation-kind-external-write", ("typed_tools", 2, "operation", "kind"), "write"),
    _mutation(
        "write-adapter-kind-drift",
        ("typed_tools", 2, "operation", "adapter_kind"),
        "legacy_deterministic",
    ),
    _mutation(
        "write-result-contract-drift",
        ("typed_tools", 2, "operation", "result_contract"),
        "typed_json_v2",
    ),
    *(
        _mutation(f"{field}-drift", ("typed_tools", 2, "operation", field), value)
        for field, value in (
            ("result_bytes", 524287),
            ("visible_bytes", 262143),
            ("transport_bytes", 131071),
            ("undo_bytes", 65535),
        )
    ),
    *(
        _mutation(f"{field}-bool", ("typed_tools", 2, "operation", field), True)
        for field in ("result_bytes", "visible_bytes", "transport_bytes", "undo_bytes")
    ),
    # Every Undo member is closed and coupled to the exact five required bindings.
    _mutation("undo-policy-unknown", ("typed_tools", 2, "operation", "undo_policy"), "optional"),
    _mutation(
        "undo-policy-required-to-none", ("typed_tools", 2, "operation", "undo_policy"), "none"
    ),
    _mutation(
        "undo-policy-none-to-required", ("typed_tools", 7, "operation", "undo_policy"), "required"
    ),
    _mutation(
        "undo-payload-unknown",
        ("typed_tools", 2, "operation", "undo_payload_kind"),
        "unknown_payload",
    ),
    _mutation(
        "undo-payload-known-mismatch",
        ("typed_tools", 2, "operation", "undo_payload_kind"),
        "update_application_status",
    ),
    _mutation(
        "compensation-unknown", ("typed_tools", 2, "operation", "compensation_kind"), "undo:unknown"
    ),
    _mutation(
        "compensation-known-mismatch",
        ("typed_tools", 2, "operation", "compensation_kind"),
        "undo:update_application_status",
    ),
    _mutation(
        "undo-version-drift",
        ("typed_tools", 2, "operation", "undo_contract_version"),
        "write-undo-payload-v2",
    ),
    _mutation(
        "undo-builder-unknown",
        ("typed_tools", 2, "operation", "undo_builder_id"),
        "unknown_builder_v1",
    ),
    _mutation(
        "undo-builder-known-mismatch",
        ("typed_tools", 2, "operation", "undo_builder_id"),
        "add_note_delete_v1",
    ),
    _mutation(
        "undo-seed-unknown", ("typed_tools", 2, "operation", "undo_seed_phase"), "after_execute"
    ),
    _mutation(
        "undo-seed-known-mismatch",
        ("typed_tools", 2, "operation", "undo_seed_phase"),
        "before_execute",
    ),
    *(
        _mutation(f"required-{field}-null", ("typed_tools", 2, "operation", field), None)
        for field in (
            "undo_payload_kind",
            "compensation_kind",
            "undo_contract_version",
            "undo_builder_id",
            "undo_seed_phase",
        )
    ),
    _mutation(
        "none-payload-nonnull",
        ("typed_tools", 7, "operation", "undo_payload_kind"),
        "delete_application",
    ),
    _mutation(
        "none-compensation-nonnull",
        ("typed_tools", 7, "operation", "compensation_kind"),
        "undo:create_application",
    ),
    _mutation(
        "none-version-nonnull",
        ("typed_tools", 7, "operation", "undo_contract_version"),
        "write-undo-payload-v1",
    ),
    _mutation(
        "none-builder-nonnull",
        ("typed_tools", 7, "operation", "undo_builder_id"),
        "create_application_delete_v1",
    ),
    _mutation("none-seed-nonnull", ("typed_tools", 7, "operation", "undo_seed_phase"), "none"),
    # Discovery is a byte-exact, cardinality- and order-closed policy.
    _mutation(
        "selector-version-drift",
        ("discovery_policy", "selector_version"),
        "tool-surface-selector-v2",
    ),
    _mutation(
        "discovery-version-drift",
        ("discovery_policy", "discovery_policy_version"),
        "tool-discovery-policy-v2",
    ),
    _mutation("page-cardinality", ("discovery_policy", "page_domains"), _DROP_LAST),
    _mutation("attachment-cardinality", ("discovery_policy", "attachment_domains"), _DROP_LAST),
    _mutation("lexical-cardinality", ("discovery_policy", "lexical_rules"), _DROP_LAST),
    _mutation(
        "page-kind-unknown", ("discovery_policy", "page_domains", 0, "page_kind"), "unknown_page"
    ),
    _mutation(
        "page-kind-known-mismatch", ("discovery_policy", "page_domains", 0, "page_kind"), "calendar"
    ),
    _mutation(
        "page-domain-content", ("discovery_policy", "page_domains", 1, "domains"), ["events"]
    ),
    _mutation(
        "page-domain-unknown",
        ("discovery_policy", "page_domains", 1, "domains"),
        ["unknown_domain"],
    ),
    _mutation(
        "page-domain-noncanonical",
        ("discovery_policy", "page_domains", 2, "domains"),
        ["resumes", "applications"],
    ),
    _mutation(
        "attachment-kind-unknown",
        ("discovery_policy", "attachment_domains", 0, "attachment_kind"),
        "audio",
    ),
    _mutation(
        "attachment-kind-known-mismatch",
        ("discovery_policy", "attachment_domains", 0, "attachment_kind"),
        "document",
    ),
    _mutation(
        "attachment-domain-content",
        ("discovery_policy", "attachment_domains", 0, "domains"),
        ["applications"],
    ),
    _mutation(
        "attachment-domain-unknown",
        ("discovery_policy", "attachment_domains", 0, "domains"),
        ["unknown_domain"],
    ),
    _mutation(
        "lexical-domain-unknown",
        ("discovery_policy", "lexical_rules", 0, "domain"),
        "unknown_domain",
    ),
    _mutation(
        "lexical-domain-known-mismatch", ("discovery_policy", "lexical_rules", 0, "domain"), "jd"
    ),
    _mutation("lexical-terms-empty", ("discovery_policy", "lexical_rules", 0, "terms"), []),
    _mutation(
        "lexical-terms-duplicate",
        ("discovery_policy", "lexical_rules", 0, "terms"),
        ["投递", "投递"],
    ),
    _mutation("lexical-term-non-text", ("discovery_policy", "lexical_rules", 0, "terms"), [1]),
    _mutation(
        "lexical-term-content", ("discovery_policy", "lexical_rules", 0, "terms", 0), "未知词"
    ),
    _mutation("lexical-term-order", ("discovery_policy", "lexical_rules", 0, "terms"), _REVERSE),
    _mutation("page-order", ("discovery_policy", "page_domains"), _REVERSE),
    _mutation("attachment-order", ("discovery_policy", "attachment_domains"), _REVERSE),
    _mutation("lexical-rule-order", ("discovery_policy", "lexical_rules"), _REVERSE),
    _mutation("no-signal-behavior", ("discovery_policy", "no_signal_behavior"), "empty_catalog"),
    _mutation("invalid-input-behavior", ("discovery_policy", "invalid_input_behavior"), "ignore"),
    # Legacy is closed by kind, visibility, exact names/policies/routes, and exact ints.
    _mutation(
        "legacy-boundary-version",
        ("legacy_boundary", "boundary_version"),
        "legacy-deterministic-boundary-v2",
    ),
    _mutation("legacy-visibility", ("legacy_boundary", "provider_visibility"), "model_eligible"),
    _mutation("legacy-adapter-kind", ("legacy_boundary", "adapter_kind"), "typed"),
    _mutation(
        "legacy-name-content", ("legacy_boundary", "ordered_names", 0), "unknown_legacy_adapter"
    ),
    _mutation("legacy-name-non-text", ("legacy_boundary", "ordered_names", 0), 1),
    _mutation("legacy-name-cardinality", ("legacy_boundary", "ordered_names"), _DROP_LAST),
    _mutation(
        "legacy-name-duplicate",
        ("legacy_boundary", "ordered_names"),
        ["save_application_jd_version"] * 3,
    ),
    _mutation("legacy-name-order", ("legacy_boundary", "ordered_names"), _REVERSE),
    _mutation("legacy-policy-content", ("legacy_boundary", "chained_policies", 0), "forbidden"),
    _mutation("legacy-policy-cardinality", ("legacy_boundary", "chained_policies"), _DROP_LAST),
    _mutation("legacy-policy-order", ("legacy_boundary", "chained_policies"), _REVERSE),
    _mutation(
        "legacy-route-source",
        ("legacy_boundary", "initial_route_bindings", 0, "route_source"),
        "unknown_source",
    ),
    _mutation(
        "legacy-route-cardinality", ("legacy_boundary", "initial_route_bindings"), _DROP_LAST
    ),
    _mutation("legacy-route-order", ("legacy_boundary", "initial_route_bindings"), _REVERSE),
    _mutation(
        "legacy-route-ordinal-drift",
        ("legacy_boundary", "initial_route_bindings", 0, "adapter_ordinal"),
        2,
    ),
    _mutation(
        "legacy-route-ordinal-bool",
        ("legacy_boundary", "initial_route_bindings", 0, "adapter_ordinal"),
        True,
    ),
    _mutation(
        "legacy-route-ordinal-float",
        ("legacy_boundary", "initial_route_bindings", 0, "adapter_ordinal"),
        1.0,
    ),
    _mutation(
        "legacy-route-ordinal-text",
        ("legacy_boundary", "initial_route_bindings", 0, "adapter_ordinal"),
        "1",
    ),
    # Compensation kind/cardinality/order is derived from the exact required-Undo bindings.
    _mutation("compensation-kind", ("compensation_operation_order", 0), "undo:unknown"),
    _mutation("compensation-cardinality-short", ("compensation_operation_order",), _DROP_LAST),
    _mutation("compensation-cardinality-long", ("compensation_operation_order",), _APPEND_FIRST),
    _mutation("compensation-order", ("compensation_operation_order",), _REVERSE),
)


def _projection(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return cast(dict[str, Any], value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        assert isinstance(result, Mapping)
        return cast(dict[str, Any], result)
    raise AssertionError("ToolMetadataManifestV1 must expose its exact mapping projection")


def _compile_manifest(specs: Sequence[object] | None = None) -> ToolMetadataManifestV1:
    ordered_specs = tuple(_TEST_TOOL_CATALOG.specs if specs is None else specs)
    value = compile_tool_metadata_manifest(ordered_specs)
    assert isinstance(value, ToolMetadataManifestV1)
    return value


@pytest.fixture(scope="module")
def baseline_manifest_projection() -> dict[str, Any]:
    return _projection(_compile_manifest())


@pytest.mark.parametrize(("object_path", "shape"), _shape_params())
def test_manifest_validator_rejects_every_nested_object_shape_mutation(
    baseline_manifest_projection: dict[str, Any],
    object_path: Path,
    shape: str,
) -> None:
    projection = copy.deepcopy(baseline_manifest_projection)
    target = _at(projection, object_path)
    assert isinstance(target, dict)
    first_key = next(iter(target))

    if shape == "missing":
        del target[first_key]
    elif shape == "extra":
        target["unexpected_key"] = None
    else:
        target[first_key] = target.pop(first_key)

    with pytest.raises((TypeError, ValueError)):
        validate_tool_metadata_manifest(projection)


@pytest.mark.parametrize("mutation", VALUE_MUTATIONS, ids=lambda item: item.name)
def test_manifest_validator_rejects_closed_same_shape_value_mutations(
    baseline_manifest_projection: dict[str, Any],
    mutation: ManifestMutation,
) -> None:
    projection = copy.deepcopy(baseline_manifest_projection)
    _replace_at(projection, mutation.path, mutation.replacement)

    with pytest.raises((TypeError, ValueError)):
        validate_tool_metadata_manifest(projection)


def test_manifest_validator_rejects_dependency_cycle_from_baseline_projection(
    baseline_manifest_projection: dict[str, Any],
) -> None:
    projection = copy.deepcopy(baseline_manifest_projection)
    first_name = projection["typed_tools"][0]["provider_name"]
    second_name = projection["typed_tools"][1]["provider_name"]
    projection["typed_tools"][0]["dependencies"] = [second_name]
    projection["typed_tools"][1]["dependencies"] = [first_name]

    with pytest.raises((TypeError, ValueError)):
        validate_tool_metadata_manifest(projection)


def test_manifest_normal_assignment_cannot_replace_projection(
    baseline_manifest_projection: dict[str, Any],
) -> None:
    manifest = _compile_manifest()
    replacement = freeze_json(copy.deepcopy(baseline_manifest_projection))

    with pytest.raises((AttributeError, TypeError, ValueError)):
        setattr(manifest, "_projection", replacement)


def test_manifest_object_setattr_replacement_fails_before_projection_returns(
    baseline_manifest_projection: dict[str, Any],
) -> None:
    manifest = _compile_manifest()
    replacement = freeze_json(copy.deepcopy(baseline_manifest_projection))
    object.__setattr__(manifest, "_projection", replacement)

    with pytest.raises(
        (AttributeError, TypeError, ValueError), match="integrity|projection|replace"
    ):
        manifest.to_dict()


def test_production_manifest_has_exact_keys_order_and_baseline_projection() -> None:
    actual = _projection(_compile_manifest())
    expected = load_asset("tool_metadata_manifest_current.json")

    assert tuple(actual) == TOP_LEVEL_KEYS
    assert actual == expected
    assert actual["schema_version"] == 1
    assert actual["metadata_version"] == "tool-surface-metadata-v1"
    assert actual["catalog_profile"] == "agent_typed_v1"
    assert len(actual["typed_tools"]) == 26
    assert tuple(item["ordinal"] for item in actual["typed_tools"]) == tuple(range(1, 27))


def test_manifest_nested_keys_and_nullable_write_shape_are_exact() -> None:
    projection = _projection(_compile_manifest())
    typed = projection["typed_tools"]
    assert all(tuple(item) == TYPED_TOOL_KEYS for item in typed)

    discovery = projection["discovery_policy"]
    assert tuple(discovery) == DISCOVERY_POLICY_KEYS
    assert all(tuple(item) == PAGE_DOMAIN_KEYS for item in discovery["page_domains"])
    assert all(tuple(item) == ATTACHMENT_DOMAIN_KEYS for item in discovery["attachment_domains"])
    assert all(tuple(item) == LEXICAL_RULE_KEYS for item in discovery["lexical_rules"])
    legacy = projection["legacy_boundary"]
    assert tuple(legacy) == LEGACY_BOUNDARY_KEYS
    assert all(
        tuple(item) == INITIAL_ROUTE_BINDING_KEYS for item in legacy["initial_route_bindings"]
    )
    assert tuple(projection["compensation_operation_order"]) == COMPENSATION_OPERATION_ORDER
    assert legacy["provider_visibility"] == "forbidden"
    assert legacy["adapter_kind"] == "legacy_deterministic"

    for item in typed:
        binding = item["binding"]
        assert tuple(binding) == ("contract", "resolver_descriptors")
        assert tuple(binding["contract"]) == ("kind", "entity_kind")
        for descriptor in binding["resolver_descriptors"]:
            assert tuple(descriptor) == (
                "resolver_id",
                "entity_kind",
                "arg_path",
                "presence",
                "identity_type",
            )
        for editable in item["editable_fields"]:
            assert tuple(editable) == (
                "field",
                "value_type",
                "options",
                "clearable",
                "clear_value",
            )

        operation = item["operation"]
        if operation["kind"] == "read":
            assert tuple(operation) == ("kind",)
            assert item["confirmation_policy"] == "none"
        else:
            assert tuple(operation) == WRITE_OPERATION_KEYS
            assert item["confirmation_policy"] == "required"
            assert set(operation) == set(WRITE_OPERATION_KEYS)
            if operation["undo_policy"] == "none":
                assert operation["undo_payload_kind"] is None
                assert operation["compensation_kind"] is None
                assert operation["undo_contract_version"] is None
                assert operation["undo_builder_id"] is None
                assert operation["undo_seed_phase"] is None
            else:
                assert operation["undo_payload_kind"] is not None
                assert operation["compensation_kind"] is not None
                assert operation["undo_contract_version"] is not None
                assert operation["undo_builder_id"] is not None
                assert operation["undo_seed_phase"] is not None


def test_manifest_validator_rejects_duplicate_names_and_non_contiguous_ordinals() -> None:
    projection = _projection(_compile_manifest())

    duplicate = copy.deepcopy(projection)
    duplicate["typed_tools"][1]["provider_name"] = duplicate["typed_tools"][0]["provider_name"]
    with pytest.raises((TypeError, ValueError), match="duplicate|unique|name"):
        validate_tool_metadata_manifest(duplicate)

    ordinal_gap = copy.deepcopy(projection)
    ordinal_gap["typed_tools"][3]["ordinal"] = 99
    with pytest.raises((TypeError, ValueError), match="ordinal|order|contiguous"):
        validate_tool_metadata_manifest(ordinal_gap)


@pytest.mark.parametrize("mutation", ("self", "unknown", "legacy"))
def test_manifest_validator_rejects_invalid_dependencies(mutation: str) -> None:
    projection = copy.deepcopy(_projection(_compile_manifest()))
    first = projection["typed_tools"][0]
    if mutation == "self":
        first["dependencies"] = [first["provider_name"]]
    elif mutation == "unknown":
        first["dependencies"] = ["unknown_typed_dependency"]
    else:
        first["dependencies"] = ["save_application_jd_version"]

    with pytest.raises((TypeError, ValueError), match="depend|Legacy|unknown|self"):
        validate_tool_metadata_manifest(projection)


def test_manifest_validator_rejects_dependency_cycle_and_operation_union_drift() -> None:
    projection = _projection(_compile_manifest())
    cycle = copy.deepcopy(projection)
    first_name = cycle["typed_tools"][0]["provider_name"]
    second_name = cycle["typed_tools"][1]["provider_name"]
    cycle["typed_tools"][0]["dependencies"] = [second_name]
    cycle["typed_tools"][1]["dependencies"] = [first_name]
    with pytest.raises((TypeError, ValueError), match="cycle|depend"):
        validate_tool_metadata_manifest(cycle)

    read_with_write_key = copy.deepcopy(projection)
    read_item = next(
        item for item in read_with_write_key["typed_tools"] if item["operation"]["kind"] == "read"
    )
    read_item["operation"]["undo_policy"] = "none"
    with pytest.raises((TypeError, ValueError), match="operation|read|write|key"):
        validate_tool_metadata_manifest(read_with_write_key)


def test_manifest_validator_rejects_coordinated_required_undo_pair_and_order_swap() -> None:
    projection = copy.deepcopy(_projection(_compile_manifest()))
    by_name = {item["provider_name"]: item for item in projection["typed_tools"]}
    create_operation = by_name["create_application"]["operation"]
    note_operation = by_name["add_note"]["operation"]
    for field in ("undo_payload_kind", "compensation_kind"):
        create_operation[field], note_operation[field] = (
            note_operation[field],
            create_operation[field],
        )
    projection["compensation_operation_order"] = [
        "undo:update_application_status",
        "undo:add_note",
        "undo:create_application_event",
        "undo:create_application",
    ]

    with pytest.raises((TypeError, ValueError), match="compensation|Undo|order|binding"):
        validate_tool_metadata_manifest(projection)


def test_manifest_validator_rejects_extra_top_level_key_and_wrong_nullable_shape() -> None:
    projection = _projection(_compile_manifest())
    extra = copy.deepcopy(projection)
    extra["unexpected"] = True
    with pytest.raises((TypeError, ValueError), match="key|manifest|unexpected"):
        validate_tool_metadata_manifest(extra)

    nullable = copy.deepcopy(projection)
    write_item = next(
        item
        for item in nullable["typed_tools"]
        if item["operation"]["kind"] == "transactional_write"
    )
    del write_item["operation"]["undo_builder_id"]
    with pytest.raises((TypeError, ValueError), match="key|nullable|undo"):
        validate_tool_metadata_manifest(nullable)
