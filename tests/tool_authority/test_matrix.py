from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG, MODEL_TOOL_NAMES
from offerpilot.ai.tool_runtime.legacy import LEGACY_DETERMINISTIC_NAMES


FIXTURE = Path(__file__).parents[1] / "fixtures" / "tool_authority" / "authority_manifest_v1.json"


def _manifest() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _resolver_metadata(resolver: BindingResolverSpec[Any]) -> dict[str, Any]:
    return {
        "resolver_id": resolver.resolver_id,
        "entity_kind": resolver.entity_kind,
        "arg_path": resolver.arg_path,
        "presence": resolver.presence,
        "identity_type": resolver.identity_type,
    }


def _catalog_manifest(catalog: ToolCatalog) -> dict[str, Any]:
    return catalog.authority_manifest


def test_model_catalog_matches_the_single_canonical_authority_manifest() -> None:
    manifest = _manifest()
    assert _catalog_manifest(MODEL_TOOL_CATALOG) == manifest
    assert tuple(item["name"] for item in manifest["tools"]) == MODEL_TOOL_NAMES
    assert not set(MODEL_TOOL_NAMES) & LEGACY_DETERMINISTIC_NAMES


def test_matrix_metadata_is_closed_and_exact() -> None:
    manifest = _manifest()
    assert len(manifest["tools"]) == 25
    for spec, expected in zip(MODEL_TOOL_CATALOG.specs, manifest["tools"]):
        assert spec.name == expected["name"]
        assert spec.kind == expected["kind"]
        assert spec.confirmation_policy == expected["confirmation_policy"]
        assert tuple(map(str, spec.required_capabilities)) == tuple(expected["required_capabilities"])
        assert spec.binding_contract.kind == expected["binding"]["kind"]
        assert spec.binding_contract.entity_kind == expected["binding"]["entity_kind"]
        assert [_resolver_metadata(item) for item in spec.binding_resolvers] == expected["resolvers"]

    update_event = MODEL_TOOL_CATALOG.resolve("update_application_event")
    assert update_event is not None
    assert [item.resolver_id for item in update_event.binding_resolvers] == [
        "application_event_parent",
        "application_identity_arg",
    ]
    update_note = MODEL_TOOL_CATALOG.resolve("update_note")
    assert update_note is not None
    assert [item.resolver_id for item in update_note.binding_resolvers] == [
        "note_application_parent",
        "application_identity_arg",
    ]


def test_authority_manifest_is_canonical_read_only_and_not_duplicated() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    assert raw == json.dumps(_manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    source = Path(__file__).parents[2] / "src" / "offerpilot" / "ai" / "tool_specs" / "catalog.py"
    assert "_EXPECTED_MATRIX" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("kind", "write"),
        ("confirmation_policy", "required"),
        ("required_capabilities", ["future.read"]),
        ("binding", {"kind": "none", "entity_kind": None}),
    ],
)
def test_catalog_rejects_authority_manifest_drift_before_provider(
    field: str, replacement: object
) -> None:
    manifest = copy.deepcopy(_manifest())
    manifest["tools"][0][field] = replacement
    with pytest.raises(ValueError):
        ToolCatalog(
            MODEL_TOOL_CATALOG.specs,
            expected_names=MODEL_TOOL_NAMES,
            authority_manifest=manifest,
        )


def test_catalog_rejects_unknown_resolver_and_mixed_kind() -> None:
    manifest = copy.deepcopy(_manifest())
    manifest["tools"][1]["resolvers"][0]["resolver_id"] = "future_resolver"
    with pytest.raises(ValueError):
        ToolCatalog(
            MODEL_TOOL_CATALOG.specs,
            expected_names=MODEL_TOOL_NAMES,
            authority_manifest=manifest,
        )

    manifest = copy.deepcopy(_manifest())
    manifest["tools"][7]["resolvers"][1]["entity_kind"] = "resume"
    with pytest.raises(ValueError):
        ToolCatalog(
            MODEL_TOOL_CATALOG.specs,
            expected_names=MODEL_TOOL_NAMES,
            authority_manifest=manifest,
        )


def test_catalog_rejects_illegal_contract_resolver_count() -> None:
    manifest = copy.deepcopy(_manifest())
    manifest["tools"][0]["resolvers"] = [
        {
            "resolver_id": "application_identity_arg",
            "entity_kind": "application",
            "arg_path": "application_id",
            "presence": "optional",
            "identity_type": "positive_int64",
        }
    ]
    with pytest.raises(ValueError):
        ToolCatalog(
            MODEL_TOOL_CATALOG.specs,
            expected_names=MODEL_TOOL_NAMES,
            authority_manifest=manifest,
        )


def test_non_typed_legacy_names_cannot_enter_the_typed_catalog() -> None:
    assert all(MODEL_TOOL_CATALOG.resolve(name) is None for name in LEGACY_DETERMINISTIC_NAMES)
    assert all(
        name not in {contract.name for contract in MODEL_TOOL_CATALOG.provider_contracts()}
        for name in LEGACY_DETERMINISTIC_NAMES
    )


def test_model_catalog_fails_closed_after_provider_or_authority_metadata_mutation() -> None:
    spec = MODEL_TOOL_CATALOG.resolve("get_application")
    assert spec is not None
    original_payload = copy.deepcopy(spec.contract.payload)
    original_binding = spec.binding_contract
    try:
        spec.contract.payload["function"]["description"] = "evil provider description"  # type: ignore[index]
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.provider_contracts()
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.authority_manifest
    finally:
        spec.contract.payload.clear()
        spec.contract.payload.update(original_payload)

    try:
        object.__setattr__(spec, "binding_contract", BindingContract("none"))
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
    finally:
        object.__setattr__(spec, "binding_contract", original_binding)


def test_provider_contract_projection_is_detached_from_catalog_storage() -> None:
    contracts = MODEL_TOOL_CATALOG.provider_contracts()
    original = copy.deepcopy(contracts[0].payload)
    try:
        contracts[0].payload["function"]["description"] = "evil detached description"  # type: ignore[index]
        assert MODEL_TOOL_CATALOG.provider_contracts()[0].payload["function"]["description"] != (
            "evil detached description"
        )
    finally:
        contracts[0].payload.clear()
        contracts[0].payload.update(original)


def test_schema_validator_projection_is_detached_from_catalog_storage() -> None:
    validator = MODEL_TOOL_CATALOG.validator_for("list_applications")
    original = copy.deepcopy(validator.schema)
    validator.schema["description"] = "evil schema description"
    assert MODEL_TOOL_CATALOG.validator_for("list_applications").schema == original


class _ResolutionContext:
    def __init__(self, parent_state: tuple[str, int | None] = ("unavailable", None)) -> None:
        self.parent_state = parent_state
        self.parent_calls: list[tuple[str, int]] = []
        self.binding_resolver_port = self

    def resolve_parent_identity(self, entity_kind: str, identity: int) -> tuple[str, int | None]:
        self.parent_calls.append((entity_kind, identity))
        return self.parent_state

    def binding_target_resolution(
        self, *, entity_kind: str, state: str, identity: int | None
    ) -> SimpleNamespace:
        return SimpleNamespace(entity_kind=entity_kind, state=state, identity=identity)


def test_resolvers_use_primitive_context_port_and_preserve_resolution_states() -> None:
    list_events = MODEL_TOOL_CATALOG.resolve("list_application_events")
    get_event = MODEL_TOOL_CATALOG.resolve("get_application_event")
    assert list_events is not None and get_event is not None
    context = _ResolutionContext(("detached", None))

    omitted = list_events.binding_resolvers[0].resolve({}, context)
    explicit_unavailable = list_events.binding_resolvers[0].resolve(
        {"application_id": True}, context
    )
    detached = get_event.binding_resolvers[0].resolve({"id": 9}, context)

    assert omitted.state == "omitted"
    assert explicit_unavailable.state == "unavailable"
    assert detached.state == "detached"
    assert context.parent_calls == [("application", 9)]


def test_update_note_has_required_parent_then_optional_explicit_application() -> None:
    spec = MODEL_TOOL_CATALOG.resolve("update_note")
    assert spec is not None
    assert [(resolver.arg_path, resolver.presence) for resolver in spec.binding_resolvers] == [
        ("id", "required"),
        ("application_id", "optional"),
    ]
