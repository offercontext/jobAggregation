from __future__ import annotations

import ast
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.agent_loop import build_segment_surface_gate
from offerpilot.ai.tool_authority import AuthorityFactory, AuthorityPhaseError
from offerpilot.ai.tool_authority import composition as authority_composition_module
from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime import pipeline as pipeline_module
from offerpilot.ai.tool_runtime.catalog import (
    SegmentToolCatalogLease,
    ToolCatalog,
    compile_tool_metadata_manifest,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ToolSpec,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    ToolMetadataBundleV1,
    WriteOperationMetadataV1,
)
from offerpilot.ai.types import Message
from offerpilot.ai import write_operations as write_operations_module
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components
from tests.agent_loop.helpers import ToolDefinition, runtime


def _bundle(catalog: ToolCatalog) -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(catalog.specs)
    return ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )


def _bind_surface(
    bundle: ToolMetadataBundleV1,
    lease: SegmentToolCatalogLease,
    *,
    provider_bundle: ToolMetadataBundleV1 | None = None,
) -> tuple[AuthorityFactory, object, object]:
    catalog, context = runtime()
    del catalog
    views = bundle if provider_bundle is None else provider_bundle
    gate = build_segment_surface_gate(
        (Message(role="user", content="hello"),),
        catalog=bundle._typed_catalog,
        catalog_lease=lease,
        context=context,
        authority=context.authority,
        provider_view=views.provider_view(),
        discovery_view=views.discovery_view(),
        authority_metadata_view=views.authority_view(),
        policy=validate_startup_policy(bundle._typed_catalog.authority_manifest),
    )
    return context.authority_factory, context.authority, gate


def _prepare_identity(
    factory: AuthorityFactory,
    authority: object,
    *,
    tool_name: str,
    tool_call_id: str = "call-authority-view",
    arguments: dict[str, object] | None = None,
) -> tuple[object, str]:
    values = {} if arguments is None else arguments
    arguments_digest = _digest(values)
    runner = object()
    context = object()
    surface = object()
    binding = object()
    gateway = object()
    factory.register_runner_invocation(runner, authority=authority)  # type: ignore[arg-type]
    factory.register_tool_execution_context(context, authority=authority)  # type: ignore[arg-type]
    build = factory.create_provider_surface_build_identity(
        authority,  # type: ignore[arg-type]
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-authority-view",
    )
    surface_fingerprint = "sha256:" + "a" * 64
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=surface_fingerprint,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,  # type: ignore[arg-type]
        build_identity=build,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=surface_fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
    identity = factory.create_new_turn_prepare_identity(
        invocation,
        attempt_id=attempt,
        candidate_ordinal=0,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments_digest=arguments_digest,
    )
    return identity, arguments_digest


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _contract_fingerprint(spec: ToolSpec[Any, Any]) -> str:
    payload = json.dumps(
        materialize_provider_payloads((spec.contract,))[0],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _register_route(
    factory: AuthorityFactory,
    authority: object,
    lease: SegmentToolCatalogLease,
    handle: object,
    prepare_identity: object,
) -> ToolSpec[Any, Any]:
    return factory.register_tool_spec(  # type: ignore[return-value]
        handle,  # type: ignore[arg-type]
        catalog_lease=lease,
        authority=authority,  # type: ignore[arg-type]
        prepare_identity=prepare_identity,  # type: ignore[arg-type]
    )


def _prepare(
    factory: AuthorityFactory,
    authority: object,
    lease: SegmentToolCatalogLease,
    handle: object,
    prepare_identity: object,
    *,
    arguments_digest: str,
    contract_fingerprint: str | None = None,
) -> PreparedToolCall[Any, Any]:
    fingerprint = (
        _contract_fingerprint(lease.require_spec(handle))
        if contract_fingerprint is None
        else contract_fingerprint
    )
    return factory.prepare_tool_call(
        authority,  # type: ignore[arg-type]
        prepare_identity=prepare_identity,  # type: ignore[arg-type]
        tool_call_id="call-authority-view",
        catalog_lease=lease,
        spec_handle=handle,
        arguments={},
        typed_args={},
        arguments_digest=arguments_digest,
        contract_fingerprint=fingerprint,
        binding=BindingAudit(status="unbound", target_count=0),
    )


def _forged_handle(handle: object) -> object:
    handle_type = type(handle)
    forged = object.__new__(handle_type)
    for name in getattr(handle_type, "__slots__", ()):
        if name != "__weakref__":
            object.__setattr__(forged, name, object.__getattribute__(handle, name))
    return forged


def test_surface_gate_binds_one_exact_authority_view_and_segment_lease() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, arguments_digest = _prepare_identity(
        factory,
        authority,
        tool_name=handle.tool_name,
    )

    spec = _register_route(factory, authority, lease, handle, identity)
    prepared = _prepare(
        factory,
        authority,
        lease,
        handle,
        identity,
        arguments_digest=arguments_digest,
    )

    assert spec is lease.require_spec(handle)
    assert prepared.spec is spec
    assert factory.is_active(prepared)


def test_surface_gate_rejects_views_from_another_bundle_over_the_same_catalog() -> None:
    catalog, _ = runtime()
    first = _bundle(catalog)
    second = _bundle(catalog)
    lease = first.open_segment_lease()

    with pytest.raises(
        (AuthorityPhaseError, ProjectionError, TypeError, ValueError),
        match="[Bb]undle|provenance",
    ):
        _bind_surface(first, lease, provider_bundle=second)


@pytest.mark.parametrize("foreign_kind", ("sibling_segment", "cross_bundle"))
def test_authority_rejects_cross_segment_and_cross_bundle_handles(
    foreign_kind: str,
) -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    bound_lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, bound_lease)
    if foreign_kind == "sibling_segment":
        foreign_lease = bundle.open_segment_lease()
    else:
        foreign_lease = _bundle(catalog).open_segment_lease()
    handle = foreign_lease.resolve("list_applications")
    assert handle is not None
    identity, _ = _prepare_identity(factory, authority, tool_name=handle.tool_name)
    before = factory.active_count

    with pytest.raises(
        (AuthorityPhaseError, TypeError, ValueError),
        match="[Bb]undle|segment|lease|provenance",
    ):
        _register_route(factory, authority, bound_lease, handle, identity)

    assert factory.active_count == before


@pytest.mark.parametrize("bad_handle", ("raw_spec", "tool_name", "copied_fields"))
def test_authority_accepts_only_the_exact_opaque_typed_route_handle(
    bad_handle: str,
) -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, _ = _prepare_identity(factory, authority, tool_name=handle.tool_name)
    invalid: object
    if bad_handle == "raw_spec":
        invalid = lease.require_spec(handle)
    elif bad_handle == "tool_name":
        invalid = handle.tool_name
    else:
        invalid = _forged_handle(handle)
    before = factory.active_count

    with pytest.raises(
        (AuthorityPhaseError, TypeError, ValueError),
        match="handle|route|segment|lease|provenance|ToolSpec",
    ):
        _register_route(factory, authority, lease, invalid, identity)

    assert factory.active_count == before


def test_closed_segment_handle_fails_before_authority_registration() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, _ = _prepare_identity(factory, authority, tool_name=handle.tool_name)
    before = factory.active_count
    lease.close()

    with pytest.raises(
        (AuthorityPhaseError, RuntimeError, TypeError, ValueError),
        match="closed|revoked|lease|provenance",
    ):
        _register_route(factory, authority, lease, handle, identity)

    assert factory.active_count == before


def test_registered_handle_is_revoked_when_lease_closes_before_prepare() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, arguments_digest = _prepare_identity(
        factory,
        authority,
        tool_name=handle.tool_name,
    )
    _register_route(factory, authority, lease, handle, identity)
    contract_fingerprint = _contract_fingerprint(lease.require_spec(handle))
    before = factory.active_count
    lease.close()

    with pytest.raises(
        (AuthorityPhaseError, RuntimeError, TypeError, ValueError),
        match="closed|revoked|lease|provenance",
    ):
        _prepare(
            factory,
            authority,
            lease,
            handle,
            identity,
            arguments_digest=arguments_digest,
            contract_fingerprint=contract_fingerprint,
        )

    assert factory.active_count == before


def test_register_route_rejects_same_content_catalog_topology_replacement() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, _ = _prepare_identity(factory, authority, tool_name=handle.tool_name)
    ordered = object.__getattribute__(catalog, "_ordered")
    replacement = tuple(list(ordered))
    assert replacement == ordered
    assert replacement is not ordered
    before = factory.active_count

    object.__setattr__(catalog, "_ordered", replacement)
    try:
        with pytest.raises(AuthorityPhaseError, match="provenance"):
            _register_route(factory, authority, lease, handle, identity)
    finally:
        object.__setattr__(catalog, "_ordered", ordered)

    assert factory.active_count == before


@pytest.mark.parametrize("mutation", ("executor", "metadata"))
def test_register_route_rejects_spec_drift_after_handle_resolution(
    mutation: str,
) -> None:
    source, _ = runtime(ToolDefinition("list_applications"))
    specs = tuple(replace(spec, metadata=replace(spec.metadata)) for spec in source.specs)
    catalog = ToolCatalog(specs, expected_names=tuple(spec.name for spec in specs))
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    tool_name = "list_applications" if mutation == "executor" else "update_application_event"
    handle = lease.resolve(tool_name)
    assert handle is not None
    spec = lease.require_spec(handle)
    identity, _ = _prepare_identity(factory, authority, tool_name=tool_name)
    before = factory.active_count

    if mutation == "executor":
        target = spec
        field = "executor"
        original = spec.executor

        def injected_executor(_args: object, _context: object) -> str:
            return "injected"

        replacement: object = injected_executor
    else:
        target = spec.metadata
        field = "operation"
        original = spec.metadata.operation
        assert type(original) is WriteOperationMetadataV1
        replacement = replace(original, result_bytes=original.result_bytes - 1)

    object.__setattr__(target, field, replacement)
    try:
        with pytest.raises(AuthorityPhaseError, match="integrity|provenance"):
            _register_route(factory, authority, lease, handle, identity)
    finally:
        object.__setattr__(target, field, original)

    assert factory.active_count == before


def test_duplicate_handle_registration_failure_is_atomic() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    first = lease.resolve("list_applications")
    second = lease.resolve("list_applications")
    assert first is not None and second is not None and second is not first
    identity, _ = _prepare_identity(factory, authority, tool_name=first.tool_name)
    _register_route(factory, authority, lease, first, identity)
    before = factory.active_count

    with pytest.raises(AuthorityPhaseError, match="already bound"):
        _register_route(factory, authority, lease, second, identity)

    assert factory.active_count == before
    assert id(second) not in factory._tool_specs
    assert not factory.is_active(second)


def test_surface_gate_rejects_same_specs_from_foreign_dispatch_catalog() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    foreign_catalog = ToolCatalog(
        catalog.specs,
        expected_names=tuple(spec.name for spec in catalog.specs),
    )
    _, context = runtime()

    with pytest.raises(ProjectionError, match="catalog|Catalog|provenance|mismatch"):
        build_segment_surface_gate(
            (Message(role="user", content="hello"),),
            catalog=foreign_catalog,
            catalog_lease=lease,
            context=context,
            authority=context.authority,
            provider_view=bundle.provider_view(),
            discovery_view=bundle.discovery_view(),
            authority_metadata_view=bundle.authority_view(),
            policy=validate_startup_policy(catalog.authority_manifest),
        )


def test_authority_uses_bound_view_and_handle_without_name_lookup_or_second_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, arguments_digest = _prepare_identity(
        factory,
        authority,
        tool_name=handle.tool_name,
    )

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("Authority must consume the bound view and exact route handle")

    monkeypatch.setattr(SegmentToolCatalogLease, "resolve", forbidden)
    monkeypatch.setattr(ToolCatalog, "__init__", forbidden)
    monkeypatch.setattr(
        ToolCatalog,
        "authority_manifest",
        property(forbidden),
    )
    monkeypatch.setattr(
        "offerpilot.ai.tool_runtime.catalog.compile_tool_metadata_manifest",
        forbidden,
    )

    _register_route(factory, authority, lease, handle, identity)
    prepared = _prepare(
        factory,
        authority,
        lease,
        handle,
        identity,
        arguments_digest=arguments_digest,
    )

    assert prepared.spec is lease.require_spec(handle)


def test_authority_bound_handle_rejects_copy_and_deepcopy() -> None:
    catalog, _ = runtime()
    bundle = _bundle(catalog)
    lease = bundle.open_segment_lease()
    factory, authority, _ = _bind_surface(bundle, lease)
    handle = lease.resolve("list_applications")
    assert handle is not None
    identity, _ = _prepare_identity(factory, authority, tool_name=handle.tool_name)
    _register_route(factory, authority, lease, handle, identity)

    for operation in (copy.copy, copy.deepcopy):
        with pytest.raises((TypeError, ValueError)):
            operation(handle)


def _module_source(module: object) -> str:
    source_path = getattr(module, "__file__", None)
    assert isinstance(source_path, str)
    return Path(source_path).read_text(encoding="utf-8")


def _attribute_paths(source: str) -> tuple[str, ...]:
    paths: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        components = [node.attr]
        root = node.value
        while isinstance(root, ast.Attribute):
            components.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name):
            components.append(root.id)
        paths.append(".".join(reversed(components)))
    return tuple(paths)


@pytest.mark.parametrize(
    "module",
    (authority_composition_module, pipeline_module),
)
def test_authority_semantic_decisions_never_read_raw_spec_metadata(module: object) -> None:
    source = _module_source(module)
    paths = _attribute_paths(source)

    for forbidden_suffix in (
        "metadata.operation",
        "metadata.confirmation_policy",
        "metadata.required_capabilities",
        "metadata.binding",
    ):
        assert not any(path.endswith(forbidden_suffix) for path in paths)


def test_claimed_write_authorization_never_accepts_a_raw_spec() -> None:
    source = _module_source(write_operations_module)
    names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}

    assert "require_authority_spec" not in names
