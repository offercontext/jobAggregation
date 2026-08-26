from __future__ import annotations

import json
from pathlib import Path

import pytest

from offerpilot.ai.tool_authority.policy import (
    AGENT_TYPED_V1_PROFILE,
    CAPABILITY_POLICY_VERSION,
    DEPENDENCY_POLICY_VERSION,
)
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG, MODEL_TOOL_NAMES
from offerpilot.context_projector.authority_surface import (
    AuthoritySurfaceView,
    intersect_authority_surface,
)
from offerpilot.context_projector.contracts import ProjectionError, canonical_json, sha256_hex
from offerpilot.context_projector.selector import ToolSelectionSignals, select_tools
from offerpilot.pilot_runtime.compensation import prepare_compensation_handler_components


FIXTURE = Path(__file__).parents[1] / "fixtures" / "tool_authority" / "dependency_policy_v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _bundle() -> ToolMetadataBundleV1:
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    return ToolMetadataBundleV1(
        typed_catalog=MODEL_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=manifest.to_dict()["legacy_boundary"],
        compensation=prepare_compensation_handler_components().metadata_projection(),
    )


def test_dependency_policy_v1_matches_read_only_canonical_golden() -> None:
    expected = _fixture()
    discovery = _bundle().discovery_view()
    actual = {
        "dependency_policy_version": expected["dependency_policy_version"],
        "catalog_names": [entry.provider_name for entry in discovery.ordered_entries],
        "dependencies": {
            entry.provider_name: list(entry.dependencies) for entry in discovery.ordered_entries
        },
    }
    assert actual["dependency_policy_version"] == DEPENDENCY_POLICY_VERSION
    assert actual["catalog_names"] == list(MODEL_TOOL_NAMES)
    assert actual["dependencies"] == expected["dependencies"]
    assert "sha256:" + sha256_hex(canonical_json(actual)) == expected["canonical_sha256"]


def test_runtime_has_no_injected_dependency_policy_compatibility_path() -> None:
    bundle = _bundle()
    with pytest.raises(TypeError):
        select_tools(
            bundle.discovery_view(),
            bundle.authority_view(),
            ToolSelectionSignals(page_kind="offers"),
            dependency_policy=object(),  # type: ignore[call-arg]
        )


def test_production_source_has_no_catalog_drift_or_injected_surface_fallback() -> None:
    source_root = Path(__file__).parents[2] / "src"
    production = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
    assert "typed_catalog_drift" not in production
    assert "_project_injected_surface" not in production
    assert "injected-surface-v1" not in production


def test_authority_filter_rejects_a_dependency_open_capability_surface() -> None:
    bundle = _bundle()
    selection = select_tools(
        bundle.discovery_view(),
        bundle.authority_view(),
        ToolSelectionSignals(page_kind="workspace", trusted_domains=("offers",)),
    )
    offers_write = next(
        capability
        for capability in AGENT_TYPED_V1_PROFILE.capabilities
        if str(capability) == "offers.write"
    )
    with pytest.raises(ProjectionError, match="tool_dependency_not_closed"):
        intersect_authority_surface(
            bundle.discovery_view(),
            bundle.authority_view(),
            selection,
            AuthoritySurfaceView(
                capability_profile_id="agent_typed_v1",
                capability_policy_version=CAPABILITY_POLICY_VERSION,
                dependency_policy_version=DEPENDENCY_POLICY_VERSION,
                capabilities=frozenset({offers_write}),
                context_type="workspace",
            ),
        )


def test_selector_and_authority_surface_require_views_from_the_same_bundle() -> None:
    first = _bundle()
    second = _bundle()
    selection = select_tools(
        first.discovery_view(),
        first.authority_view(),
        ToolSelectionSignals(page_kind="workspace"),
    )
    with pytest.raises(ProjectionError, match="Bundle"):
        intersect_authority_surface(
            first.discovery_view(),
            second.authority_view(),
            selection,
            AuthoritySurfaceView(
                capability_profile_id="agent_typed_v1",
                capability_policy_version=CAPABILITY_POLICY_VERSION,
                dependency_policy_version=DEPENDENCY_POLICY_VERSION,
                capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
                context_type="workspace",
            ),
        )
