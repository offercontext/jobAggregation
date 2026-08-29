from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict, replace
from functools import partial
from typing import Any, cast

import pytest

from offerpilot.ai.tool_runtime.contracts import UndoPolicy as RuntimeUndoPolicy
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
    ToolSurfaceMetadataV1,
    UndoBuilderBinding,
    WriteOperationMetadataV1,
    validate_tool_spec_components,
)
from offerpilot.ai.tool_runtime.policy_types import (
    ToolCapability,
    ToolDomain,
    UndoPolicy,
)

from .factories import (
    _build_undo_payload,
    _capture_undo_seed,
    _resolve_application_identity,
    forbid_call,
    presentation_binding,
    read_metadata,
    resolver_binding,
    resolver_descriptor,
    synthetic_provider_contract,
    write_metadata,
)


_UNSET = object()


def _components(
    metadata: ToolSurfaceMetadataV1,
    *,
    resolver_bindings: tuple[ResolverImplementationBinding, ...] | None = None,
    undo_builder_binding: UndoBuilderBinding | None | object = _UNSET,
    presentation: ToolPresentationBindingV1 | None | object = _UNSET,
) -> object:
    descriptors = metadata.binding.resolver_descriptors
    bindings = (
        tuple(resolver_binding(descriptor) for descriptor in descriptors)
        if resolver_bindings is None
        else resolver_bindings
    )
    if presentation is _UNSET:
        presentation = presentation_binding()
    if isinstance(metadata.operation, WriteOperationMetadataV1) and (
        metadata.operation.undo_policy is UndoPolicy.REQUIRED
    ) and undo_builder_binding is _UNSET:
        undo_builder_binding = UndoBuilderBinding(
            descriptor=metadata.operation,
            implementation_id=cast(str, metadata.operation.undo_builder_id),
            capture_seed=_capture_undo_seed,
            build_undo=_build_undo_payload,
        )
    elif undo_builder_binding is _UNSET:
        undo_builder_binding = None
    return validate_tool_spec_components(
        provider_contract=synthetic_provider_contract(),
        metadata=metadata,
        resolver_bindings=bindings,
        undo_builder_binding=undo_builder_binding,
        presentation=presentation,
    )


def test_read_and_write_operation_unions_are_exclusive() -> None:
    assert _components(read_metadata()) is not None
    assert _components(write_metadata(undo_policy=UndoPolicy.NONE)) is not None

    with pytest.raises((TypeError, ValueError)):
        _components(replace(read_metadata(), confirmation_policy="required"))
    with pytest.raises((TypeError, ValueError)):
        _components(replace(write_metadata(), confirmation_policy="none"))


@pytest.mark.parametrize("duplicate_kind", ("domain", "dependency", "capability"))
def test_duplicate_domain_capability_or_dependency_is_rejected(duplicate_kind: str) -> None:
    if duplicate_kind == "domain":
        metadata = read_metadata(domains=(ToolDomain.APPLICATIONS, ToolDomain.APPLICATIONS))
    elif duplicate_kind == "dependency":
        metadata = read_metadata(dependencies=("get_application", "get_application"))
    else:
        metadata = replace(
            read_metadata(),
            required_capabilities=(
                ToolCapability.APPLICATIONS_READ,
                ToolCapability.APPLICATIONS_READ,
            ),
        )
    with pytest.raises((TypeError, ValueError), match="duplicate|unique|repeat"):
        _components(metadata)


def test_dependency_declaration_must_already_be_in_canonical_order() -> None:
    with pytest.raises((TypeError, ValueError), match="canonical|order"):
        _components(read_metadata(dependencies=("z_dependency", "a_dependency")))


def test_editable_field_must_exist_in_provider_schema() -> None:
    valid = replace(
        read_metadata(),
        editable_fields=(
            EditableFieldMetadataV1(
                field="status",
                value_type="enum",
                options=("active", "closed"),
                clearable=False,
                clear_value=None,
            ),
        ),
    )
    assert _components(valid) is not None

    missing = replace(
        valid,
        editable_fields=(
            EditableFieldMetadataV1(
                field="not_in_provider_schema",
                value_type="long_text",
                options=None,
                clearable=False,
                clear_value=None,
            ),
        ),
    )
    with pytest.raises((TypeError, ValueError), match="schema|editable|property"):
        _components(missing)


def test_resolver_count_and_ordinal_are_closed() -> None:
    first = resolver_descriptor()
    second = BindingResolverDescriptorV1(
        resolver_id="application_event_parent",
        entity_kind="application",
        arg_path="event_id",
        presence="required",
        identity_type="positive_int64",
    )
    metadata = read_metadata(resolver_descriptors=(first, second))

    with pytest.raises((TypeError, ValueError), match="resolver|binding|ordinal"):
        _components(metadata, resolver_bindings=(resolver_binding(first),))
    with pytest.raises((TypeError, ValueError), match="resolver|binding|ordinal"):
        _components(
            metadata,
            resolver_bindings=(resolver_binding(second), resolver_binding(first)),
        )


def test_resolver_entity_presence_and_identity_type_are_closed() -> None:
    with pytest.raises((TypeError, ValueError), match="entity"):
        _components(
            read_metadata(
                resolver_descriptors=(
                    BindingResolverDescriptorV1(
                        resolver_id="application_identity_arg",
                        entity_kind="resume",
                        arg_path="id",
                        presence="required",
                        identity_type="positive_int64",
                    ),
                )
            )
        )

    for presence in ("missing", "requiredly"):
        with pytest.raises((TypeError, ValueError), match="presence"):
            BindingResolverDescriptorV1(
                resolver_id="application_identity_arg",
                entity_kind="application",
                arg_path="id",
                presence=cast(Any, presence),
                identity_type="positive_int64",
            )
    with pytest.raises((TypeError, ValueError), match="identity"):
        BindingResolverDescriptorV1(
            resolver_id="application_identity_arg",
            entity_kind="application",
            arg_path="id",
            presence="required",
            identity_type=cast(Any, "integer"),
        )


def test_resolver_binding_requires_exact_descriptor_object_identity() -> None:
    descriptor = resolver_descriptor()
    metadata = read_metadata(resolver_descriptors=(descriptor,))
    equal_but_distinct = replace(descriptor)
    binding = resolver_binding(equal_but_distinct)

    with pytest.raises((TypeError, ValueError), match="descriptor|identity"):
        _components(metadata, resolver_bindings=(binding,))


def test_resolver_implementation_id_is_stable_and_nonempty() -> None:
    descriptor = resolver_descriptor()
    binding = resolver_binding(descriptor)
    metadata = read_metadata(resolver_descriptors=(descriptor,))
    assert _components(metadata, resolver_bindings=(binding,)) is not None

    changed = replace(binding, implementation_id="synthetic_application_identity_v2")
    with pytest.raises((TypeError, ValueError), match="implementation|identity|seal"):
        _components(metadata, resolver_bindings=(changed,))

    assert _components(metadata, resolver_bindings=(binding,)) is not None
    object.__setattr__(binding, "implementation_id", "synthetic_application_identity_v2")
    with pytest.raises((TypeError, ValueError), match="implementation|identity|seal"):
        _components(metadata, resolver_bindings=(binding,))
    with pytest.raises((TypeError, ValueError), match="implementation"):
        ResolverImplementationBinding(
            descriptor=descriptor,
            implementation_id="",
            resolve=_resolve_application_identity,
        )


def test_resolver_implementation_rejects_lambda_and_partial() -> None:
    descriptor = resolver_descriptor()
    with pytest.raises((TypeError, ValueError), match="named|lambda|partial|implementation"):
        ResolverImplementationBinding(
            descriptor=descriptor,
            implementation_id="synthetic_lambda",
            resolve=lambda args, context: args["id"],
        )
    with pytest.raises((TypeError, ValueError), match="named|lambda|partial|implementation"):
        ResolverImplementationBinding(
            descriptor=descriptor,
            implementation_id="synthetic_partial",
            resolve=partial(_resolve_application_identity),
        )


def test_required_undo_requires_exact_operation_descriptor_and_callable_binding() -> None:
    metadata = write_metadata(undo_policy=UndoPolicy.REQUIRED)
    operation = cast(WriteOperationMetadataV1, metadata.operation)
    binding = UndoBuilderBinding(
        descriptor=operation,
        implementation_id=cast(str, operation.undo_builder_id),
        capture_seed=_capture_undo_seed,
        build_undo=_build_undo_payload,
    )
    assert _components(metadata, undo_builder_binding=binding) is not None

    with pytest.raises((TypeError, ValueError), match="Undo|undo|binding"):
        _components(metadata, undo_builder_binding=None)

    with pytest.raises((TypeError, ValueError), match="descriptor|identity"):
        _components(
            metadata,
            undo_builder_binding=replace(binding, descriptor=replace(operation)),
        )
    with pytest.raises((TypeError, ValueError), match="implementation|undo"):
        _components(
            metadata,
            undo_builder_binding=replace(binding, implementation_id="wrong_undo_id"),
        )
    with pytest.raises((TypeError, ValueError), match="Undo|undo|callable|identity|seal"):
        _components(
            metadata,
            undo_builder_binding=replace(binding, capture_seed=forbid_call),
        )
    with pytest.raises((TypeError, ValueError), match="Undo|undo|callable|identity|seal"):
        _components(
            metadata,
            undo_builder_binding=replace(
                binding,
                capture_seed=_build_undo_payload,
                build_undo=_capture_undo_seed,
            ),
        )


def test_non_required_operation_cannot_have_an_undo_builder() -> None:
    metadata = write_metadata(undo_policy=UndoPolicy.NONE)
    operation = cast(WriteOperationMetadataV1, metadata.operation)
    binding = UndoBuilderBinding(
        descriptor=operation,
        implementation_id="synthetic_unexpected_undo",
        capture_seed=_capture_undo_seed,
        build_undo=_build_undo_payload,
    )
    with pytest.raises((TypeError, ValueError), match="Undo|undo|binding"):
        _components(metadata, undo_builder_binding=binding)


def test_missing_presentation_binding_fails_closed() -> None:
    with pytest.raises((TypeError, ValueError), match="presentation"):
        _components(read_metadata(), presentation=None)


def test_callable_replacement_after_sealing_fails_closed() -> None:
    descriptor = resolver_descriptor()
    metadata = read_metadata(resolver_descriptors=(descriptor,))
    binding = resolver_binding(descriptor)
    assert _components(metadata, resolver_bindings=(binding,)) is not None

    object.__setattr__(binding, "resolve", forbid_call)
    with pytest.raises((TypeError, ValueError), match="callable|identity|seal|integrity"):
        _components(metadata, resolver_bindings=(binding,))


def test_metadata_and_runtime_bindings_reject_generic_serialization() -> None:
    metadata = read_metadata()
    with pytest.raises((TypeError, ValueError)):
        json.dumps(metadata)

    binding = presentation_binding()
    with pytest.raises((TypeError, ValueError)):
        json.dumps(binding)
    for operation in (
        copy.copy,
        copy.deepcopy,
        pickle.dumps,
        asdict,
        lambda value: value.to_json(),
    ):
        with pytest.raises((TypeError, ValueError)):
            operation(binding)
    assert "function" not in repr(binding)
    assert "0x" not in repr(binding)


def test_runtime_write_contract_uses_the_leaf_undo_policy_identity() -> None:
    assert RuntimeUndoPolicy is UndoPolicy


def test_forbid_call_is_not_used_during_validation() -> None:
    descriptor = resolver_descriptor()
    binding = ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id="synthetic_application_identity_v1",
        resolve=_resolve_application_identity,
    )
    presentation = ToolPresentationBindingV1(
        implementation_id="synthetic_presentation_v1",
        confirmation_description=forbid_call,
        pending_details_projector=forbid_call,
        success_summary_projector=forbid_call,
    )
    assert _components(
        read_metadata(resolver_descriptors=(descriptor,)),
        resolver_bindings=(binding,),
        presentation=presentation,
    ) is not None
