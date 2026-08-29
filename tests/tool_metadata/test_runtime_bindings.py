from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    ToolPresentationBindingV1,
)

from .factories import (
    compose_synthetic_bundle,
    presentation_binding,
    read_metadata,
    resolver_binding,
    resolver_descriptor,
    runtime_bindings,
    synthetic_compensation_view,
    synthetic_legacy_boundary,
    synthetic_manifest,
    synthetic_provider_contract,
    synthetic_tool_spec,
)


def test_runtime_binding_groups_are_fresh_and_have_exact_roles() -> None:
    first = runtime_bindings()
    second = runtime_bindings()

    assert first is not second
    assert tuple(first) == (
        "resolver_bindings",
        "undo_builder_binding",
        "presentation",
    )
    assert first["resolver_bindings"] is not second["resolver_bindings"]
    assert first["resolver_bindings"] != ()
    assert first["undo_builder_binding"] is None
    assert isinstance(first["presentation"], ToolPresentationBindingV1)
    assert first["presentation"] is not second["presentation"]


def test_resolver_binding_keeps_exact_descriptor_and_stable_named_callable() -> None:
    descriptor = resolver_descriptor()
    binding = resolver_binding(descriptor)

    assert binding.descriptor is descriptor
    assert binding.implementation_id == "synthetic_application_identity_v1"
    assert inspect.isfunction(binding.resolve)
    assert binding.resolve.__name__ == "_resolve_application_identity"
    assert binding.resolve.__module__.endswith("tool_metadata.factories")


def test_resolver_binding_does_not_accept_a_structurally_equal_descriptor_copy() -> None:
    descriptor = resolver_descriptor()
    copied = replace(descriptor)
    assert copied == descriptor
    assert copied is not descriptor

    metadata = read_metadata(resolver_descriptors=(descriptor,))
    binding = resolver_binding(copied)
    assert metadata.binding.resolver_descriptors[0] is descriptor
    assert binding.descriptor is copied

    # The constructor may accept an independently-created DTO; the component
    # validator must reject it when it is not the exact metadata object.
    from offerpilot.ai.tool_runtime.metadata import validate_tool_spec_components

    with pytest.raises((TypeError, ValueError), match="descriptor|identity"):
        validate_tool_spec_components(
            provider_contract=synthetic_provider_contract(),
            metadata=metadata,
            resolver_bindings=(binding,),
            undo_builder_binding=None,
            presentation=presentation_binding(),
        )


def test_presentation_binding_is_runtime_only_and_all_callables_are_named() -> None:
    binding = presentation_binding()

    assert binding.implementation_id == "synthetic_presentation_v1"
    for callback in (
        binding.confirmation_description,
        binding.pending_details_projector,
        binding.success_summary_projector,
    ):
        assert inspect.isfunction(callback)
        assert callback.__name__ != "<lambda>"
        assert callback.__module__.endswith("tool_metadata.factories")


def test_synthetic_provider_and_spec_helpers_snapshot_nested_values() -> None:
    first = synthetic_provider_contract()
    second = synthetic_provider_contract()
    assert first is not second
    assert first.payload is not second.payload
    assert first.parameters is not second.parameters
    assert first.parameters["properties"] is not second.parameters["properties"]

    first_spec = synthetic_tool_spec()
    second_spec = synthetic_tool_spec()
    assert first_spec is not second_spec
    assert first_spec.contract is not second_spec.contract
    assert first_spec.contract.parameters is not second_spec.contract.parameters


def test_synthetic_views_are_fresh_and_immutable_at_the_container_boundary() -> None:
    manifest = synthetic_manifest()
    manifest_again = synthetic_manifest()
    legacy = synthetic_legacy_boundary()
    legacy_again = synthetic_legacy_boundary()
    compensation = synthetic_compensation_view()
    compensation_again = synthetic_compensation_view()
    bundle = compose_synthetic_bundle()
    bundle_again = compose_synthetic_bundle()

    assert manifest is not manifest_again
    assert legacy is not legacy_again
    assert compensation is not compensation_again
    assert bundle is not bundle_again
    assert manifest["typed_tools"] is not manifest_again["typed_tools"]
    assert legacy["ordered_adapters"] is not legacy_again["ordered_adapters"]
    assert compensation["ordered_operations"] is not compensation_again["ordered_operations"]
    assert bundle["manifest"] is not bundle_again["manifest"]


def test_descriptor_contract_rejects_unknown_resolver_shape_before_runtime_lookup() -> None:
    with pytest.raises((TypeError, ValueError), match="resolver|entity|presence|identity"):
        BindingResolverDescriptorV1(
            resolver_id="unknown_resolver",
            entity_kind="application",
            arg_path="id",
            presence="required",
            identity_type="positive_int64",
        )


def test_resolver_binding_replacement_is_not_a_new_implementation_identity() -> None:
    descriptor = resolver_descriptor()
    original = resolver_binding(descriptor)

    from offerpilot.ai.tool_runtime.metadata import validate_tool_spec_components

    metadata = read_metadata(resolver_descriptors=(descriptor,))
    assert original.descriptor is descriptor
    assert validate_tool_spec_components(
        provider_contract=synthetic_provider_contract(),
        metadata=metadata,
        resolver_bindings=(original,),
        undo_builder_binding=None,
        presentation=presentation_binding(),
    ) is not None
    object.__setattr__(original, "implementation_id", "synthetic_application_identity_v2")
    with pytest.raises((TypeError, ValueError), match="implementation|identity|seal"):
        validate_tool_spec_components(
            provider_contract=synthetic_provider_contract(),
            metadata=metadata,
            resolver_bindings=(original,),
            undo_builder_binding=None,
            presentation=presentation_binding(),
        )
