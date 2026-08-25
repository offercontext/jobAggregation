from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.tool_authority.policy import (
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
    PROFILE_ID,
)
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.context_projector.contracts import ProjectionError, canonical_json, sha256_hex
from offerpilot.context_projector.selector import (
    DependencyPolicyV1,
    ToolSelection,
    require_dependency_policy_v1,
)


@dataclass(frozen=True, slots=True)
class AuthoritySurfaceView:
    """The non-identifying authority facts the projector is allowed to consume."""

    capability_profile_id: str
    capability_policy_version: str
    dependency_policy_version: str
    capabilities: frozenset[object]
    context_type: Literal["workspace", "global", "application", "mode"]

    @classmethod
    def from_authority(cls, authority: SegmentExecutionAuthority) -> "AuthoritySurfaceView":
        if type(authority) is not SegmentExecutionAuthority:
            raise ProjectionError("segment_authority_required")
        return cls(
            capability_profile_id=authority.capability_profile_id,
            capability_policy_version=authority.capability_policy_version,
            dependency_policy_version=DEPENDENCY_POLICY_VERSION,
            capabilities=authority.capabilities,
            context_type=authority.trusted_scope.context_type,
        )


def intersect_authority_surface(
    catalog: ToolCatalog,
    selection: ToolSelection,
    view: AuthoritySurfaceView,
    *,
    dependency_policy: DependencyPolicyV1,
) -> ToolSelection:
    """Remove whole Provider envelopes denied by capability or scope policy."""

    if type(catalog) is not ToolCatalog:
        raise ProjectionError("typed_catalog_required")
    dependency_policy = require_dependency_policy_v1(dependency_policy)
    catalog_contracts = catalog.provider_contracts()
    catalog_names = tuple(contract.name for contract in catalog_contracts)
    catalog_payloads = dict(zip(catalog_names, catalog.materialize_provider_payloads()))
    if catalog_names != dependency_policy.catalog_names:
        raise ProjectionError("dependency_catalog_mismatch")
    if view.capability_profile_id != PROFILE_ID:
        raise ProjectionError("unknown_capability_profile")
    if view.capability_policy_version != CAPABILITY_POLICY_VERSION:
        raise ProjectionError("unsupported_capability_policy_version")
    if view.dependency_policy_version != dependency_policy.version:
        raise ProjectionError("unsupported_dependency_policy_version")
    capability_values = frozenset(str(capability) for capability in view.capabilities)
    selected_names = set(selection.names)
    allowed_names: set[str] = set()
    for name in catalog_names:
        if name not in selected_names:
            continue
        spec = catalog.resolve(name)
        if spec is None:
            raise ProjectionError("provider_catalog_resolution_failed")
        required = frozenset(
            str(capability) for capability in spec.metadata.required_capabilities
        )
        if not required.issubset(capability_values):
            continue
        if (
            view.context_type == "application"
            and spec.metadata.binding.contract.kind == "non_application_only"
        ):
            continue
        allowed_names.add(name)
    tools = tuple(contract for contract in catalog_contracts if contract.name in allowed_names)
    names = tuple(contract.name for contract in tools)
    if not tools:
        raise ProjectionError("empty_authority_surface")
    dependency_policy.validate_closed(names, catalog_names)
    return ToolSelection(
        tools=tools,
        names=names,
        envelope_fingerprint=sha256_hex(
            canonical_json([catalog_payloads[name] for name in names])
        ),
        fallback_all=selection.fallback_all,
        domains=selection.domains,
    )


__all__ = ["AuthoritySurfaceView", "intersect_authority_surface"]
