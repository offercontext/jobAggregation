from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from offerpilot.ai.tool_authority.contracts import SegmentExecutionAuthority
from offerpilot.ai.tool_authority.policy import (
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
    PROFILE_ID,
)
from offerpilot.ai.tool_runtime.metadata import (
    ToolDiscoveryEntryV1,
    ToolAuthorityMetadataView,
    ToolDiscoveryMetadataView,
)
from offerpilot.ai.tool_runtime.contracts import ProviderToolContract
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.context_projector.selector import (
    ToolSelectionResult,
    _issue_tool_selection_result,
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
    discovery_view: ToolDiscoveryMetadataView,
    authority_metadata_view: ToolAuthorityMetadataView,
    selection: ToolSelectionResult,
    view: AuthoritySurfaceView,
) -> ToolSelectionResult:
    """Remove whole Provider envelopes denied by capability or scope policy."""

    if (
        type(discovery_view) is not ToolDiscoveryMetadataView
        or type(authority_metadata_view) is not ToolAuthorityMetadataView
    ):
        raise ProjectionError("exact_metadata_views_required")
    if type(selection) is not ToolSelectionResult:
        raise ProjectionError("tool_selection_result_required")
    if type(view) is not AuthoritySurfaceView:
        raise ProjectionError("authority_surface_view_required")
    selection._ensure_integrity()
    try:
        discovery_token = discovery_view.bundle_instance_token
        authority_token = authority_metadata_view.bundle_instance_token
        if (
            discovery_token is not authority_token
            or selection.bundle_instance_token is not discovery_token
        ):
            raise ProjectionError("cross_Bundle_authority_surface")
        discovery_entries = discovery_view.ordered_entries
        authority_entries = authority_metadata_view.entries
    except ProjectionError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProjectionError("metadata_Bundle_integrity_failure") from exc

    if view.capability_profile_id != PROFILE_ID:
        raise ProjectionError("unknown_capability_profile")
    if view.capability_policy_version != CAPABILITY_POLICY_VERSION:
        raise ProjectionError("unsupported_capability_policy_version")
    if view.dependency_policy_version != DEPENDENCY_POLICY_VERSION:
        raise ProjectionError("unsupported_dependency_policy_version")
    if type(view.capabilities) is not frozenset:
        raise ProjectionError("invalid_capability_surface")

    if tuple(entry.provider_name for entry in discovery_entries) != tuple(authority_entries):
        raise ProjectionError("authority_discovery_view_mismatch")

    selected_rows: list[tuple[ToolDiscoveryEntryV1, ProviderToolContract]] = []
    selected_index = 0
    for entry in discovery_entries:
        if (
            selected_index >= len(selection.selected_names)
            or entry.provider_name != selection.selected_names[selected_index]
        ):
            continue
        contract = selection.provider_contracts[selected_index]
        if entry.provider_contract is not contract:
            raise ProjectionError("selection_Bundle_provenance_mismatch")
        selected_rows.append((entry, contract))
        selected_index += 1
    if selected_index != len(selection.selected_names):
        raise ProjectionError("selection_Bundle_provenance_mismatch")

    capability_values = frozenset(str(capability) for capability in view.capabilities)
    allowed_rows: list[tuple[ToolDiscoveryEntryV1, ProviderToolContract]] = []
    for discovery_entry, contract in selected_rows:
        name = discovery_entry.provider_name
        authority_entry = authority_entries.get(name)
        if authority_entry is None:
            raise ProjectionError("authority_metadata_missing")
        required = frozenset(
            str(capability) for capability in authority_entry.required_capabilities
        )
        if not required.issubset(capability_values):
            continue
        if (
            view.context_type == "application"
            and authority_entry.binding.contract.kind == "non_application_only"
        ):
            continue
        allowed_rows.append((discovery_entry, contract))
    if not allowed_rows:
        raise ProjectionError("empty_authority_surface")

    allowed_names = tuple(entry.provider_name for entry, _ in allowed_rows)
    allowed_name_set = set(allowed_names)
    for discovery_entry, _ in allowed_rows:
        if any(dependency not in allowed_name_set for dependency in discovery_entry.dependencies):
            raise ProjectionError("tool_dependency_not_closed")
    if allowed_names == selection.selected_names:
        return selection

    return _issue_tool_selection_result(
        discovery_view,
        authority_metadata_view,
        provider_contracts=tuple(contract for _, contract in allowed_rows),
        selected_names=allowed_names,
        selected_domains=selection.selected_domains,
        dependency_closure=allowed_names,
        full_catalog_fallback=selection.full_catalog_fallback,
        fallback_reason=selection.fallback_reason,
        diagnostics=selection.diagnostics,
    )


__all__ = ["AuthoritySurfaceView", "intersect_authority_surface"]
