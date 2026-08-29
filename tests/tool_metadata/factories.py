"""Small, deterministic values used by the metadata contract tests.

These helpers deliberately stay on the test side of the boundary.  They do
not write fixtures, import a production catalog, or call any repository.  A
caller gets a new DTO/container on every invocation so the tests can mutate a
probe without changing another case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    ProviderToolContract,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolBindingMetadataV1,
    ToolPresentationBindingV1,
    ToolSurfaceMetadataV1,
    UndoBuilderBinding,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import (
    CompensationKind,
    OperationKind,
    ProviderVisibility,
    ToolCapability,
    ToolDomain,
    UndoPayloadKind,
    UndoPolicy,
)


def _resolve_application_identity(args: Mapping[str, Any], context: object) -> int:
    del context
    return cast(int, args["id"])


def _capture_undo_seed(context: object, args: object) -> None:
    del context, args
    return None


def _build_undo_payload(seed: object, record: object) -> dict[str, object]:
    del seed, record
    return {"kind": "synthetic_undo"}


def _describe_confirmation(args: Mapping[str, Any]) -> str:
    del args
    return "synthetic confirmation"


def _project_pending_details(args: Mapping[str, Any]) -> dict[str, object]:
    del args
    return {"kind": "synthetic_pending"}


def _project_success_summary(result: object) -> str:
    del result
    return "synthetic success"


def _decode_arguments(values: Mapping[str, Any]) -> dict[str, Any]:
    return dict(values)


def _execute_arguments(args: Mapping[str, Any], context: object) -> dict[str, Any]:
    del context
    return dict(args)


def forbid_call(*args: object, **kwargs: object) -> None:
    """Callable that makes accidental execution in a constructor fail loudly."""

    del args, kwargs
    raise AssertionError("synthetic metadata callable must not be invoked")


def synthetic_provider_contract(name: str = "synthetic_tool") -> ProviderToolContract:
    """Return a fresh, valid Provider envelope with editable test fields."""

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "status": {"type": "string", "enum": ["active", "closed"]},
            "title": {"type": "string"},
        },
        "required": ["id"],
        "additionalProperties": False,
    }
    payload = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": parameters,
            "strict": False,
        },
    }
    return ProviderToolContract(
        payload=payload,
        name=name,
        description=f"{name} description",
        parameters=parameters,
    )


def resolver_descriptor(
    resolver_id: str = "application_identity_arg",
    arg_path: str = "id",
) -> BindingResolverDescriptorV1:
    """Return the baseline-compatible required application identity descriptor."""

    return BindingResolverDescriptorV1(
        resolver_id=resolver_id,
        entity_kind="application",
        arg_path=arg_path,
        presence="required",
        identity_type="positive_int64",
    )


def resolver_binding(descriptor: BindingResolverDescriptorV1) -> ResolverImplementationBinding:
    """Pair an exact descriptor with a stable, named implementation."""

    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id="synthetic_application_identity_v1",
        resolve=_resolve_application_identity,
    )


def presentation_binding() -> ToolPresentationBindingV1:
    """Return a fresh presentation group with named, stable callables."""

    return ToolPresentationBindingV1(
        implementation_id="synthetic_presentation_v1",
        confirmation_description=_describe_confirmation,
        pending_details_projector=_project_pending_details,
        success_summary_projector=_project_success_summary,
    )


def _editable_status() -> EditableFieldMetadataV1:
    return EditableFieldMetadataV1(
        field="status",
        value_type="enum",
        options=("active", "closed"),
        clearable=False,
        clear_value=None,
    )


def read_metadata(
    name: str = "synthetic_tool",
    domains: Sequence[ToolDomain] = (ToolDomain.APPLICATIONS,),
    dependencies: Sequence[str] = (),
    resolver_descriptors: Sequence[BindingResolverDescriptorV1] = (),
) -> ToolSurfaceMetadataV1:
    del name
    descriptors = tuple(resolver_descriptors)
    return ToolSurfaceMetadataV1(
        domains=tuple(domains),
        dependencies=tuple(dependencies),
        provider_visibility=ProviderVisibility.MODEL_ELIGIBLE,
        required_capabilities=(ToolCapability.APPLICATIONS_READ,),
        binding=ToolBindingMetadataV1(
            contract=BindingContract(
                kind="enforce_if_bound" if descriptors else "none",
                entity_kind="application" if descriptors else None,
            ),
            resolver_descriptors=descriptors,
        ),
        confirmation_policy="none",
        editable_fields=(),
        operation=ReadOperationMetadataV1(kind=OperationKind.READ),
    )


def write_metadata(
    name: str = "synthetic_write",
    undo_policy: UndoPolicy = UndoPolicy.NONE,
    dependencies: Sequence[str] = (),
    resolver_descriptors: Sequence[BindingResolverDescriptorV1] = (),
) -> ToolSurfaceMetadataV1:
    del name
    descriptors = tuple(resolver_descriptors)
    required = undo_policy is UndoPolicy.REQUIRED
    operation = WriteOperationMetadataV1(
        kind=OperationKind.TRANSACTIONAL_WRITE,
        adapter_kind="typed",
        result_contract="typed_json_v1",
        result_bytes=512 * 1024,
        visible_bytes=256 * 1024,
        transport_bytes=128 * 1024,
        undo_bytes=64 * 1024,
        undo_policy=undo_policy,
        undo_payload_kind=(UndoPayloadKind.DELETE_APPLICATION if required else None),
        compensation_kind=(
            CompensationKind.UNDO_CREATE_APPLICATION if required else None
        ),
        undo_contract_version=("synthetic_undo_v1" if required else None),
        undo_builder_id=("synthetic_undo_builder_v1" if required else None),
        undo_seed_phase=("none" if required else None),
    )
    return ToolSurfaceMetadataV1(
        domains=(ToolDomain.APPLICATIONS,),
        dependencies=tuple(dependencies),
        provider_visibility=ProviderVisibility.MODEL_ELIGIBLE,
        required_capabilities=(ToolCapability.APPLICATIONS_WRITE,),
        binding=ToolBindingMetadataV1(
            contract=BindingContract(
                kind="enforce_if_bound" if descriptors else "none",
                entity_kind="application" if descriptors else None,
            ),
            resolver_descriptors=descriptors,
        ),
        confirmation_policy="required",
        editable_fields=(_editable_status(),),
        operation=operation,
    )


def _undo_builder_binding(operation: WriteOperationMetadataV1) -> UndoBuilderBinding:
    return UndoBuilderBinding(
        descriptor=operation,
        implementation_id="synthetic_undo_builder_v1",
        capture_seed=_capture_undo_seed,
        build_undo=_build_undo_payload,
    )


def runtime_bindings() -> dict[str, object]:
    """Return fresh resolver, Undo, and presentation binding groups."""

    descriptor = resolver_descriptor()
    return {
        "resolver_bindings": (resolver_binding(descriptor),),
        "undo_builder_binding": None,
        "presentation": presentation_binding(),
    }


def synthetic_tool_spec(
    name: str = "synthetic_tool",
    metadata: ToolSurfaceMetadataV1 | None = None,
) -> ToolSpec[dict[str, Any], dict[str, Any]]:
    """Build only the final ToolSpec shape."""

    surface = metadata if metadata is not None else read_metadata(name=name)
    operation = surface.operation
    is_write = isinstance(operation, WriteOperationMetadataV1)
    final_resolver_bindings = tuple(
        resolver_binding(descriptor)
        for descriptor in surface.binding.resolver_descriptors
    )
    undo_builder = (
        _undo_builder_binding(operation)
        if is_write and operation.undo_policy is UndoPolicy.REQUIRED
        else None
    )
    return ToolSpec(
        contract=synthetic_provider_contract(name),
        metadata=surface,
        resolver_bindings=final_resolver_bindings,
        undo_builder_binding=undo_builder,
        decoder=_decode_arguments,
        executor=_execute_arguments,
        presentation=presentation_binding(),
    )


def synthetic_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "metadata_version": "tool-surface-metadata-v1",
        "typed_tools": tuple(["synthetic_tool"]),
        "discovery_policy": {"provider_visibility": "model_eligible"},
    }


def synthetic_legacy_boundary() -> dict[str, object]:
    return {
        "visibility": "forbidden",
        "ordered_adapters": tuple(["synthetic_legacy"]),
    }


def synthetic_compensation_view() -> dict[str, object]:
    return {
        "ordered_operations": tuple(["undo:create_application"]),
        "handler_ids": tuple(["synthetic_undo_builder_v1"]),
    }


def compose_synthetic_bundle() -> dict[str, object]:
    return {
        "manifest": synthetic_manifest(),
        "legacy_boundary": synthetic_legacy_boundary(),
        "compensation": synthetic_compensation_view(),
        "runtime": runtime_bindings(),
    }


__all__ = [
    "compose_synthetic_bundle",
    "forbid_call",
    "presentation_binding",
    "read_metadata",
    "resolver_binding",
    "resolver_descriptor",
    "runtime_bindings",
    "synthetic_compensation_view",
    "synthetic_legacy_boundary",
    "synthetic_manifest",
    "synthetic_provider_contract",
    "synthetic_tool_spec",
    "write_metadata",
]
