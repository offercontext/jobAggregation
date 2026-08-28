from __future__ import annotations

import copy
import importlib
import pickle
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from tests.tool_metadata.golden import load_asset


_TEST_TOOL_CATALOG = build_model_tool_catalog()


def _required_module(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Task 6 module is missing: {name}")


def _required_api(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    assert value is not None, f"Task 6 API is missing: {module.__name__}.{name}"
    return value


class _LegacyIssuerProbe:
    def __init__(self, boundary: object) -> None:
        self.bundle_instance_token = boundary.bundle_instance_token
        self.registry_token = object()
        self.route_handle = object()
        self.binding = boundary.ordered_adapter_bindings[0]

    def require_route(self, route_handle: object) -> object:
        if route_handle is not self.route_handle:
            raise ValueError("Legacy route provenance mismatch")
        return self.binding


def _operation_graph() -> tuple[object, ToolMetadataBundleV1, object, _LegacyIssuerProbe]:
    compensation_module = _required_module("offerpilot.pilot_runtime.compensation")
    components = _required_api(
        compensation_module,
        "prepare_compensation_handler_components",
    )()
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    bundle = ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], projection["legacy_boundary"]),
        compensation=components.metadata_projection(),
    )
    registry = components.bind(bundle.compensation_view())
    legacy_issuer = _LegacyIssuerProbe(bundle.legacy_boundary())
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    port_type = _required_api(metadata_module, "ToolOperationMetadataPort")
    port = port_type(
        operation_view=bundle.operation_view(),
        legacy_boundary=bundle.legacy_boundary(),
        compensation_view=bundle.compensation_view(),
        compensation_registry=registry,
        legacy_route_issuer_port=legacy_issuer,
    )
    return port, bundle, registry, legacy_issuer


def _route_projection(route: object) -> dict[str, object]:
    return {
        name: getattr(route, name)
        for name in (
            "ordinal",
            "operation_name",
            "operation_role",
            "adapter_kind",
            "operation_kind",
            "result_contract",
            "undo_policy",
        )
    }


def _assert_transient(value: object) -> None:
    with pytest.raises(TypeError):
        copy.copy(value)
    with pytest.raises(TypeError):
        copy.deepcopy(value)
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        asdict(value)
    to_json = getattr(value, "to_json", None)
    assert callable(to_json)
    with pytest.raises(TypeError):
        to_json()
    rendered = repr(value)
    assert "0x" not in rendered
    assert "operation_id" not in rendered
    assert "tool_call" not in rendered


def test_operation_port_projects_the_exact_closed_25_3_4_and_required_undo_matrix() -> None:
    port, bundle, registry, legacy_issuer = _operation_graph()
    matrix = load_asset("tool_operation_matrix_0c10e05.json")

    typed = tuple(_route_projection(item) for item in port.typed_primary_entries)
    legacy = tuple(_route_projection(item) for item in port.legacy_primary_entries)
    compensation = tuple(_route_projection(item) for item in port.compensation_entries)
    required_undo = tuple(
        {
            name: getattr(item, name)
            for name in (
                "primary_tool",
                "undo_payload_kind",
                "compensation_kind",
                "undo_contract_version",
                "undo_builder_id",
                "undo_seed_phase",
            )
        }
        for item in port.required_undo_entries
    )

    expected_typed = tuple(
        {
            "ordinal": item["ordinal"],
            "operation_name": item["name"],
            "operation_role": "primary",
            "adapter_kind": "typed",
            "operation_kind": item["operation_kind"],
            "result_contract": (
                "typed_json_v1" if item["operation_kind"] == "transactional_write" else None
            ),
            "undo_policy": item["undo_policy"],
        }
        for item in matrix["typed_operations"]
    )
    expected_legacy = tuple(
        {
            "ordinal": item["ordinal"],
            "operation_name": item["name"],
            "operation_role": item["operation_role"],
            "adapter_kind": item["adapter_kind"],
            "operation_kind": "transactional_write",
            "result_contract": item["result_contract"],
            "undo_policy": item["undo_policy"],
        }
        for item in matrix["legacy_operations"]
    )
    expected_compensation = tuple(
        {
            "ordinal": item["ordinal"],
            "operation_name": item["compensation_kind"],
            "operation_role": item["operation_role"],
            "adapter_kind": item["adapter_kind"],
            "operation_kind": "transactional_write",
            "result_contract": item["result_contract"],
            "undo_policy": "none",
        }
        for item in matrix["compensation_operations"]
    )

    assert typed == expected_typed
    assert legacy == expected_legacy
    assert compensation == expected_compensation
    assert required_undo == tuple(matrix["required_undo_bindings"])
    assert port.bundle_instance_token is bundle.bundle_instance_token
    assert port.compensation_registry_token is registry.registry_token
    assert port.legacy_route_registry_token is legacy_issuer.registry_token


def test_operation_port_route_dtos_are_frozen_and_contain_no_runtime_carrier() -> None:
    port, _, _, _ = _operation_graph()
    routes = (
        *port.typed_primary_entries,
        *port.legacy_primary_entries,
        *port.compensation_entries,
        *port.required_undo_entries,
    )

    for route in routes:
        assert is_dataclass(route)
        assert all(
            item.name
            not in {
                "session",
                "repository",
                "pending",
                "operation",
                "raw_args",
                "undo_payload",
            }
            for item in fields(route)
        )
        with pytest.raises((AttributeError, TypeError)):
            setattr(route, fields(route)[0].name, object())


def test_typed_write_handle_requires_exact_live_segment_and_claim_identity() -> None:
    port, bundle, _, _ = _operation_graph()
    lease = bundle.open_segment_lease()
    write_spec = lease.resolve("create_application")
    read_spec = lease.resolve("list_applications")
    assert write_spec is not None
    assert read_spec is not None
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    identity_type = _required_api(metadata_module, "OperationRouteIdentityV1")
    identity = identity_type(
        operation_id="operation-1",
        tool_call_id="call-1",
        revision=7,
        arguments_digest="sha256:" + "1" * 64,
    )
    live_claim_token = object()

    handle = port.bind_typed_write(lease, write_spec, identity, live_claim_token)
    _assert_transient(handle)
    assert (
        port.require_typed_write(
            handle,
            identity,
            live_claim_token,
        ).operation_name
        == "create_application"
    )

    with pytest.raises((TypeError, ValueError)):
        port.bind_typed_write(lease, read_spec, identity, live_claim_token)
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_write(handle, identity, object())
    equal_identity = identity_type(
        operation_id="operation-1",
        tool_call_id="call-1",
        revision=7,
        arguments_digest="sha256:" + "1" * 64,
    )
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_write(handle, equal_identity, live_claim_token)

    object.__setattr__(identity, "revision", 8)
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_write(handle, identity, live_claim_token)

    other_port, other_bundle, _, _ = _operation_graph()
    other_lease = other_bundle.open_segment_lease()
    other_spec = other_lease.resolve("create_application")
    assert other_spec is not None
    with pytest.raises((TypeError, ValueError)):
        port.bind_typed_write(other_lease, other_spec, identity, live_claim_token)
    with pytest.raises((TypeError, ValueError)):
        other_port.require_typed_write(handle, identity, live_claim_token)

    lease.close()
    with pytest.raises((TypeError, ValueError)):
        port.require_typed_write(handle, identity, live_claim_token)


def test_legacy_handle_requires_the_bound_issuer_port_and_exact_route_identity() -> None:
    port, _, _, issuer = _operation_graph()
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    identity_type = _required_api(metadata_module, "OperationRouteIdentityV1")
    identity = identity_type(
        operation_id="legacy-operation-1",
        tool_call_id="legacy-call-1",
        revision=3,
        arguments_digest="sha256:" + "3" * 64,
    )

    handle = port.bind_legacy(issuer.route_handle, identity)
    _assert_transient(handle)
    assert port.require_legacy(handle, identity).operation_name == issuer.binding.name

    with pytest.raises((TypeError, ValueError)):
        port.bind_legacy(object(), identity)
    equal_identity = identity_type(
        operation_id="legacy-operation-1",
        tool_call_id="legacy-call-1",
        revision=3,
        arguments_digest="sha256:" + "3" * 64,
    )
    with pytest.raises((TypeError, ValueError)):
        port.require_legacy(handle, equal_identity)
    other_port, _, _, _ = _operation_graph()
    with pytest.raises((TypeError, ValueError)):
        other_port.require_legacy(handle, identity)


def test_compensation_handle_requires_exact_committed_parent_and_handler_provenance() -> None:
    port, bundle, registry, _ = _operation_graph()
    view = bundle.compensation_view()
    handler_handle = registry.bind_handler(view.ordered_handler_bindings[0])
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    parent_type = _required_api(metadata_module, "CommittedPrimaryOperationIdentityV1")
    parent = parent_type(
        operation_id="parent-operation-1",
        primary_tool="update_application_status",
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + "4" * 64,
    )

    handle = port.bind_compensation(parent, handler_handle)
    _assert_transient(handle)
    assert (
        port.require_compensation(handle, parent, handler_handle).operation_name
        == "undo:update_application_status"
    )

    equal_parent = parent_type(
        operation_id="parent-operation-1",
        primary_tool="update_application_status",
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + "4" * 64,
    )
    with pytest.raises((TypeError, ValueError)):
        port.require_compensation(handle, equal_parent, handler_handle)
    other_port, other_bundle, other_registry, _ = _operation_graph()
    other_view = other_bundle.compensation_view()
    other_handler = other_registry.bind_handler(other_view.ordered_handler_bindings[0])
    with pytest.raises((TypeError, ValueError)):
        port.bind_compensation(parent, other_handler)
    with pytest.raises((TypeError, ValueError)):
        other_port.require_compensation(handle, parent, handler_handle)
    for field_name, wrong_value in (
        ("status", "failed"),
        ("operation_role", "compensation"),
        ("adapter_kind", "legacy_deterministic"),
    ):
        values = {
            "operation_id": "parent-operation-1",
            "primary_tool": "update_application_status",
            "operation_role": "primary",
            "adapter_kind": "typed",
            "status": "committed",
            "terminal_payload_digest": "sha256:" + "4" * 64,
        }
        values[field_name] = wrong_value
        with pytest.raises((TypeError, ValueError)):
            wrong_parent = parent_type(**values)
            port.bind_compensation(wrong_parent, handler_handle)

    changed_digest_parent = parent_type(
        operation_id="parent-operation-1",
        primary_tool="update_application_status",
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + "5" * 64,
    )
    with pytest.raises((TypeError, ValueError)):
        port.require_compensation(handle, changed_digest_parent, handler_handle)

    object.__setattr__(parent, "terminal_payload_digest", "sha256:" + "6" * 64)
    with pytest.raises((TypeError, ValueError)):
        port.require_compensation(handle, parent, handler_handle)


def test_operation_handles_are_explicitly_and_idempotently_revocable() -> None:
    port, bundle, registry, issuer = _operation_graph()
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    identity_type = _required_api(metadata_module, "OperationRouteIdentityV1")
    parent_type = _required_api(metadata_module, "CommittedPrimaryOperationIdentityV1")

    typed_identity = identity_type(
        operation_id="typed-operation-revoke",
        tool_call_id="typed-call-revoke",
        revision=1,
        arguments_digest="sha256:" + "7" * 64,
    )
    lease = bundle.open_segment_lease()
    spec_handle = lease.resolve("create_application")
    assert spec_handle is not None
    claim = object()
    typed_handle = port.bind_typed_write(lease, spec_handle, typed_identity, claim)

    legacy_identity = identity_type(
        operation_id="legacy-operation-revoke",
        tool_call_id="legacy-call-revoke",
        revision=1,
        arguments_digest="sha256:" + "8" * 64,
    )
    legacy_handle = port.bind_legacy(issuer.route_handle, legacy_identity)

    handler_handle = registry.bind_handler(bundle.compensation_view().ordered_handler_bindings[0])
    parent = parent_type(
        operation_id="parent-operation-revoke",
        primary_tool="update_application_status",
        operation_role="primary",
        adapter_kind="typed",
        status="committed",
        terminal_payload_digest="sha256:" + "9" * 64,
    )
    compensation_handle = port.bind_compensation(parent, handler_handle)

    other_port, _, _, _ = _operation_graph()
    with pytest.raises((TypeError, ValueError)):
        other_port.revoke_typed_write(typed_handle)
    with pytest.raises((TypeError, ValueError)):
        other_port.revoke_legacy(legacy_handle)
    with pytest.raises((TypeError, ValueError)):
        other_port.revoke_compensation(compensation_handle)
    with pytest.raises(TypeError):
        port.revoke_typed_write(object())
    with pytest.raises(TypeError):
        port.revoke_legacy(object())
    with pytest.raises(TypeError):
        port.revoke_compensation(object())
    assert port.require_typed_write(typed_handle, typed_identity, claim).operation_name == (
        "create_application"
    )

    port.revoke_typed_write(typed_handle)
    port.revoke_typed_write(typed_handle)
    port.revoke_legacy(legacy_handle)
    port.revoke_legacy(legacy_handle)
    port.revoke_compensation(compensation_handle)
    port.revoke_compensation(compensation_handle)

    with pytest.raises((TypeError, ValueError)):
        port.require_typed_write(typed_handle, typed_identity, claim)
    with pytest.raises((TypeError, ValueError)):
        port.require_legacy(legacy_handle, legacy_identity)
    with pytest.raises((TypeError, ValueError)):
        port.require_compensation(compensation_handle, parent, handler_handle)
    with pytest.raises((TypeError, ValueError)):
        registry.resolve(compensation_handle)
    lease.close()


def test_operation_route_handle_types_are_opaque_and_not_caller_constructible() -> None:
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")

    for name in ("TypedWriteHandle", "LegacyWriteHandle", "CompensationHandle"):
        handle_type = _required_api(metadata_module, name)
        with pytest.raises(TypeError):
            handle_type()


def test_operation_port_source_has_no_bare_name_collection_or_name_switch() -> None:
    metadata_module = _required_module("offerpilot.ai.tool_runtime.metadata")
    source_path = Path(cast(str, metadata_module.__file__))
    source = source_path.read_text(encoding="utf-8")

    forbidden = (
        "TYPED_WRITE_OPERATION_NAMES",
        "LEGACY_WRITE_OPERATION_NAMES",
        "COMPENSATION_OPERATION_NAMES",
        "REQUIRED_UNDO_OPERATION_NAMES",
        "WRITE_OPERATION_NAMES",
        "_typed_by_name",
        "_required_by_compensation",
        ".get(spec.name)",
        "compensation_names =",
    )
    assert all(name not in source for name in forbidden)
