from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ToolPresentationBindingV1,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG, MODEL_TOOL_NAMES
from offerpilot.ai.tool_runtime.legacy import LEGACY_DETERMINISTIC_NAMES


FIXTURE = Path(__file__).parents[1] / "fixtures" / "tool_authority" / "authority_manifest_v1.json"


def _manifest() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _resolver_metadata(resolver: BindingResolverDescriptorV1) -> dict[str, Any]:
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
        operation_kind = (
            "write" if type(spec.metadata.operation) is WriteOperationMetadataV1 else "read"
        )
        assert operation_kind == expected["kind"]
        assert spec.metadata.confirmation_policy == expected["confirmation_policy"]
        assert tuple(map(str, spec.metadata.required_capabilities)) == tuple(
            expected["required_capabilities"]
        )
        assert spec.metadata.binding.contract.kind == expected["binding"]["kind"]
        assert spec.metadata.binding.contract.entity_kind == expected["binding"]["entity_kind"]
        assert [_resolver_metadata(item.descriptor) for item in spec.resolver_bindings] == expected[
            "resolvers"
        ]

    update_event = MODEL_TOOL_CATALOG.resolve("update_application_event")
    assert update_event is not None
    assert [item.descriptor.resolver_id for item in update_event.resolver_bindings] == [
        "application_event_parent",
        "application_identity_arg",
    ]
    update_note = MODEL_TOOL_CATALOG.resolve("update_note")
    assert update_note is not None
    assert [item.descriptor.resolver_id for item in update_note.resolver_bindings] == [
        "note_application_parent",
        "application_identity_arg",
    ]


def test_authority_manifest_is_canonical_read_only_and_not_duplicated() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    assert (
        raw
        == json.dumps(_manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )
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
    original_description = spec.contract.description
    original_metadata = spec.metadata
    try:
        object.__setattr__(spec.contract, "description", "evil provider description")
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.provider_contracts()
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.authority_manifest
    finally:
        object.__setattr__(spec.contract, "description", original_description)

    try:
        object.__setattr__(
            spec,
            "metadata",
            replace(original_metadata, domains=tuple(reversed(original_metadata.domains))),
        )
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
    finally:
        object.__setattr__(spec, "metadata", original_metadata)


@pytest.mark.parametrize(
    "field",
    (
        "decoder",
        "executor",
        "preflight",
        "mutable_validator",
        "success_renderer",
        "result_metadata_projector",
        "schema_failure_renderer",
    ),
)
def test_model_catalog_fails_closed_after_execution_callable_mutation(field: str) -> None:
    spec = MODEL_TOOL_CATALOG.resolve("get_application")
    assert spec is not None
    original = getattr(spec, field)
    try:
        object.__setattr__(spec, field, lambda *_args, **_kwargs: {"forged": True})
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.specs
    finally:
        object.__setattr__(spec, field, original)


def _forged_confirmation_description(_args: object) -> str:
    return "forged confirmation"


def _forged_pending_details(_args: object) -> dict[str, bool]:
    return {"forged": True}


def _forged_success_summary(_result: object) -> str:
    return "forged success"


def test_model_catalog_fails_closed_after_presentation_binding_mutation() -> None:
    spec = MODEL_TOOL_CATALOG.resolve("get_application")
    assert spec is not None
    original = spec.presentation
    forged = ToolPresentationBindingV1(
        implementation_id="forged_presentation_v1",
        confirmation_description=_forged_confirmation_description,
        pending_details_projector=_forged_pending_details,
        success_summary_projector=_forged_success_summary,
    )
    try:
        object.__setattr__(spec, "presentation", forged)
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
    finally:
        object.__setattr__(spec, "presentation", original)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        (
            "editable_fields",
            (
                EditableFieldMetadataV1(
                    field="id",
                    value_type="number",
                    options=None,
                    clearable=False,
                    clear_value=None,
                ),
            ),
        ),
        ("declared_failure_categories", frozenset()),
    ),
)
def test_model_catalog_fails_closed_after_execution_metadata_mutation(
    field: str,
    replacement: object,
) -> None:
    spec = MODEL_TOOL_CATALOG.resolve("get_application")
    assert spec is not None
    target = spec.metadata if field == "editable_fields" else spec
    original = getattr(target, field)
    try:
        object.__setattr__(target, field, replacement)
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
    finally:
        object.__setattr__(target, field, original)


def test_model_catalog_fails_closed_after_exception_map_or_operation_mutation() -> None:
    read_spec = MODEL_TOOL_CATALOG.resolve("get_application")
    write_spec = MODEL_TOOL_CATALOG.resolve("update_application_status")
    assert read_spec is not None
    assert write_spec is not None
    original_exception_map = read_spec.exception_map
    original_operation = write_spec.metadata.operation
    assert original_exception_map
    assert type(original_operation) is WriteOperationMetadataV1
    try:
        forged_mapping = replace(
            original_exception_map[0],
            compatibility_detail=lambda _error: "forged private detail",
        )
        object.__setattr__(
            read_spec,
            "exception_map",
            (forged_mapping, *original_exception_map[1:]),
        )
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("get_application")
    finally:
        object.__setattr__(read_spec, "exception_map", original_exception_map)

    try:
        object.__setattr__(
            write_spec.metadata,
            "operation",
            replace(original_operation, visible_bytes=original_operation.visible_bytes - 1),
        )
        with pytest.raises(ValueError, match="catalog integrity drift"):
            MODEL_TOOL_CATALOG.resolve("update_application_status")
    finally:
        object.__setattr__(write_spec.metadata, "operation", original_operation)


def test_provider_contract_projection_is_immutable_and_materializes_detached() -> None:
    contracts = MODEL_TOOL_CATALOG.provider_contracts()
    projected_function = contracts[0].payload["function"]
    assert isinstance(projected_function, Mapping)
    assert not isinstance(projected_function, dict)
    with pytest.raises(TypeError):
        projected_function["description"] = "evil immutable description"  # type: ignore[index]

    detached = materialize_provider_payloads((contracts[0],))[0]
    detached["function"]["description"] = "evil detached description"
    assert (
        MODEL_TOOL_CATALOG.provider_contracts()[0].payload["function"]["description"]
        != "evil detached description"
    )


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

    omitted = list_events.resolver_bindings[0].resolve({}, context)
    explicit_unavailable = list_events.resolver_bindings[0].resolve(
        {"application_id": True}, context
    )
    detached = get_event.resolver_bindings[0].resolve({"id": 9}, context)

    assert omitted.state == "omitted"
    assert explicit_unavailable.state == "unavailable"
    assert detached.state == "detached"
    assert context.parent_calls == [("application", 9)]


def test_update_note_has_required_parent_then_optional_explicit_application() -> None:
    spec = MODEL_TOOL_CATALOG.resolve("update_note")
    assert spec is not None
    assert [
        (resolver.descriptor.arg_path, resolver.descriptor.presence)
        for resolver in spec.resolver_bindings
    ] == [
        ("id", "required"),
        ("application_id", "optional"),
    ]
