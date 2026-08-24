from __future__ import annotations

import copy
import pickle
from dataclasses import asdict, replace
from typing import Any

import pytest

from offerpilot.ai.tool_authority import (
    ApplicationScopeConstraint,
    AuthorityFactory,
    BindingTargetResolution,
    TrustedContextScope,
    execution_scope,
)
from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ProviderToolContract,
    ToolSpec,
)


def _prepared(factory: AuthorityFactory) -> PreparedToolCall[Any, Any]:
    authority = factory.create_segment_authority(
        conversation_id=11,
        conversation_scope_revision=0,
        segment_id="segment-serialization",
        trusted_scope=TrustedContextScope("workspace", None, "general"),
        capabilities=frozenset(),
    )
    spec = ToolSpec(
        contract=ProviderToolContract(
            payload={
                "type": "function",
                "function": {"name": "get_application", "description": "", "parameters": {}},
            },
            name="get_application",
            description="",
            parameters={},
        ),
        kind="read",
        decoder=lambda value: value,
        executor=lambda args, context: args,
    )
    prepared = PreparedToolCall(
        tool_call_id="call-serialization",
        spec=spec,
        arguments={},
        typed_args={},
        arguments_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        binding=BindingAudit(status="unbound", target_count=0),
        authority_instance_token=authority.authority_instance_token,
    )
    factory.register_prepared(prepared, authority)
    return prepared


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
        with pytest.raises(TypeError):
            copy.deepcopy(prepared)
        with pytest.raises(TypeError):
            asdict(prepared)
