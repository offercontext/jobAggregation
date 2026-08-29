from __future__ import annotations

import copy
import hashlib
import json
import pickle
from dataclasses import FrozenInstanceError, asdict, replace
from typing import Any

import pytest

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    BindingTargetResolution,
    TrustedContextScope,
    execution_scope,
)
from offerpilot.ai.tool_runtime.catalog import SegmentToolSpecHandle, ToolCatalog
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from tests.tool_metadata.factories import compose_synthetic_bundle, synthetic_tool_spec


def _prepared(factory: AuthorityFactory) -> PreparedToolCall[Any, Any]:
    authority = factory.create_segment_authority(
        conversation_id=11,
        conversation_scope_revision=0,
        segment_id="segment-serialization",
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset(),
    )
    spec = synthetic_tool_spec()
    catalog = ToolCatalog((spec,), expected_names=(spec.name,))
    source = compose_synthetic_bundle()
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=source["manifest"],
        legacy_boundary=source["legacy_boundary"],
        compensation=source["compensation"],
    )
    lease = bundle.open_segment_lease()
    factory.bind_segment_tool_catalog(
        authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    spec_handle = lease.resolve(spec.name)
    assert spec_handle is not None
    runner = object()
    context = object()
    surface = object()
    binding = object()
    gateway = object()
    fingerprint = "sha256:" + "c" * 64
    factory.register_runner_invocation(runner, authority=authority)
    factory.register_tool_execution_context(context, authority=authority)
    build = factory.create_provider_surface_build_identity(
        authority,
        runner_invocation=runner,
        tool_context=context,
        model_call_id="model-serialization",
    )
    factory.register_frozen_surface(
        surface,
        surface_fingerprint=fingerprint,
        authority=authority,
        build_identity=build,
    )
    factory.register_model_call_surface_binding(
        binding,
        surface=surface,
        surface_fingerprint=fingerprint,
        authority=authority,
        build_identity=build,
    )
    factory.register_gateway_session(
        gateway,
        authority=authority,
        build_identity=build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
    )
    invocation = factory.create_provider_invocation_identity(
        build,
        surface=surface,
        surface_fingerprint=fingerprint,
        model_call_surface_binding=binding,
        gateway_session=gateway,
    )
    attempt = factory.issue_provider_attempt(invocation, candidate_ordinal=0)
    prepare_identity = factory.create_new_turn_prepare_identity(
        invocation,
        attempt_id=attempt,
        candidate_ordinal=0,
        tool_call_id="call-serialization",
        tool_name=spec.name,
        arguments_digest="sha256:" + hashlib.sha256(b"{}").hexdigest(),
    )
    factory.register_tool_spec(
        spec_handle,
        catalog_lease=lease,
        authority=authority,
        prepare_identity=prepare_identity,
    )
    contract_fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                materialize_provider_payloads((spec.contract,))[0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    return factory.prepare_tool_call(
        authority,
        prepare_identity=prepare_identity,
        tool_call_id="call-serialization",
        catalog_lease=lease,
        spec_handle=spec_handle,
        arguments={},
        typed_args={},
        arguments_digest="sha256:" + hashlib.sha256(b"{}").hexdigest(),
        contract_fingerprint=contract_fingerprint,
        binding=BindingAudit(status="unbound", target_count=0),
    )


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps])
def test_security_values_reject_copy_and_pickle(operation: object) -> None:
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-serialization",
            trusted_scope=TrustedContextScope("workspace", None, "general"),
            capabilities=frozenset(),
        )
        values: tuple[object, ...] = (
            authority,
            ApplicationScopeConstraint(
                entity_kind="application",
                mode="unrestricted",
                allowed_identities=frozenset(),
                authority_instance_token=authority.authority_instance_token,
            ),
            BindingTargetResolution(
                entity_kind="application",
                state="omitted",
                identity=None,
                authority_instance_token=authority.authority_instance_token,
            ),
        )
        for value in values:
            with pytest.raises((TypeError, ValueError)):
                operation(value)  # type: ignore[operator]


def test_dataclass_helpers_and_generic_json_checkpoint_reject_transient_values() -> None:
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-serialization",
            trusted_scope=TrustedContextScope("workspace", None, "general"),
            capabilities=frozenset(),
        )
        constraint = ApplicationScopeConstraint(
            entity_kind="application",
            mode="unrestricted",
            allowed_identities=frozenset(),
            authority_instance_token=authority.authority_instance_token,
        )
        resolution = BindingTargetResolution(
            entity_kind="application",
            state="omitted",
            identity=None,
            authority_instance_token=authority.authority_instance_token,
        )
        for value in (authority, constraint, resolution):
            with pytest.raises(TypeError):
                asdict(value)
            with pytest.raises(TypeError):
                value.to_json()  # type: ignore[union-attr]
        with pytest.raises(TypeError):
            factory.require_active(replace(authority))


def test_opaque_handles_have_no_value_serialization_or_raw_token_repr() -> None:
    with execution_scope() as factory:
        authority = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-serialization",
            trusted_scope=TrustedContextScope("workspace", None, "general"),
            capabilities=frozenset(),
        )
        token = authority.authority_instance_token
        assert "0x" not in repr(token)
        assert "0x" not in repr(authority)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(token)
        with pytest.raises(TypeError):
            token.to_json()


def test_prepared_tool_call_carries_opaque_authority_handle_and_is_transient() -> None:
    with execution_scope() as factory:
        prepared = _prepared(factory)
        assert prepared.authority_instance_token is not None
        assert type(prepared.spec_handle) is SegmentToolSpecHandle
        assert "spec_handle" in PreparedToolCall.__dataclass_fields__
        assert not hasattr(prepared, "__dict__")
        assert "0x" not in repr(prepared)
        assert "sha256:" not in repr(prepared)
        assert prepared.spec_handle.tool_name not in repr(prepared)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, asdict):
            with pytest.raises(TypeError):
                operation(prepared)
        with pytest.raises(TypeError):
            json.dumps(prepared)
        with pytest.raises(TypeError):
            prepared.to_json()
        with pytest.raises(TypeError):
            replace(prepared, spec_handle=prepared.spec_handle)
        with pytest.raises((AttributeError, FrozenInstanceError)):
            prepared.spec_handle = prepared.spec_handle
        with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
            prepared.dynamic_handle = prepared.spec_handle
