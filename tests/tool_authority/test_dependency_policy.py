from __future__ import annotations

import json
from pathlib import Path

import pytest

from offerpilot.ai.tool_authority.policy import DEPENDENCY_POLICY_VERSION
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_NAMES
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.context_projector.selector import (
    DEPENDENCY_POLICY_V1,
    DependencyPolicyV1,
    select_tools,
    ToolSelectionSignals,
)
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.context_projector.authority_surface import (
    AuthoritySurfaceView,
    intersect_authority_surface,
)
from offerpilot.ai.tool_authority.policy import (
    AGENT_TYPED_V1_PROFILE,
    CAPABILITY_POLICY_VERSION,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "tool_authority" / "dependency_policy_v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_dependency_policy_v1_matches_read_only_canonical_golden() -> None:
    expected = _fixture()
    assert DEPENDENCY_POLICY_V1.version == DEPENDENCY_POLICY_VERSION
    assert DEPENDENCY_POLICY_V1.catalog_names == MODEL_TOOL_NAMES
    assert DEPENDENCY_POLICY_V1.coverage == 25
    assert DEPENDENCY_POLICY_V1.canonical_manifest() == {
        "dependency_policy_version": expected["dependency_policy_version"],
        "catalog_names": expected["catalog_names"],
        "dependencies": expected["dependencies"],
    }
    assert DEPENDENCY_POLICY_V1.canonical_fingerprint == expected["canonical_sha256"]


def test_dependency_policy_rejects_unknown_missing_cycle_and_version_drift() -> None:
    dependencies = {
        name: tuple(DEPENDENCY_POLICY_V1.dependencies[name]) for name in MODEL_TOOL_NAMES
    }
    cases = []
    unknown_node = dict(dependencies)
    unknown_node["unknown"] = ()
    cases.append(unknown_node)
    missing_node = dict(dependencies)
    del missing_node[MODEL_TOOL_NAMES[-1]]
    cases.append(missing_node)
    unknown_dependency = dict(dependencies)
    unknown_dependency[MODEL_TOOL_NAMES[0]] = ("unknown",)
    cases.append(unknown_dependency)
    cycle = dict(dependencies)
    cycle["list_applications"] = ("get_application",)
    cases.append(cycle)
    for invalid in cases:
        with pytest.raises(ProjectionError):
            DependencyPolicyV1(
                version=DEPENDENCY_POLICY_VERSION,
                catalog_names=MODEL_TOOL_NAMES,
                dependencies=invalid,
            ).validate_closed(MODEL_TOOL_NAMES, MODEL_TOOL_NAMES)
    with pytest.raises(ProjectionError, match="unsupported_dependency_policy_version"):
        DependencyPolicyV1(
            version="dependency-policy-v2",
            catalog_names=MODEL_TOOL_NAMES,
            dependencies=dependencies,
        ).validate_closed(MODEL_TOOL_NAMES, MODEL_TOOL_NAMES)


def test_dependency_policy_rejects_open_or_unknown_selection() -> None:
    with pytest.raises(ProjectionError, match="tool_dependency_not_closed"):
        DEPENDENCY_POLICY_V1.validate_closed(("get_offer",), MODEL_TOOL_NAMES)
    with pytest.raises(ProjectionError, match="unknown_selected_tool"):
        DEPENDENCY_POLICY_V1.validate_closed(("unknown",), MODEL_TOOL_NAMES)


def test_selector_and_authority_surface_share_exact_policy_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    original = DependencyPolicyV1.validate_closed

    def validate(
        self: DependencyPolicyV1,
        selected_names: tuple[str, ...],
        catalog_names: tuple[str, ...],
    ) -> None:
        seen.append(self)
        original(self, selected_names, catalog_names)

    monkeypatch.setattr(DependencyPolicyV1, "validate_closed", validate)
    selection = select_tools(
        MODEL_TOOL_CATALOG.provider_contracts(),
        ToolSelectionSignals(page_kind="offers"),
        dependency_policy=DEPENDENCY_POLICY_V1,
    )
    intersect_authority_surface(
        MODEL_TOOL_CATALOG,
        selection,
        AuthoritySurfaceView(
            capability_profile_id="agent_typed_v1",
            capability_policy_version=CAPABILITY_POLICY_VERSION,
            dependency_policy_version=DEPENDENCY_POLICY_VERSION,
            capabilities=frozenset(AGENT_TYPED_V1_PROFILE.capabilities),
            context_type="workspace",
        ),
        dependency_policy=DEPENDENCY_POLICY_V1,
    )
    assert seen == [DEPENDENCY_POLICY_V1, DEPENDENCY_POLICY_V1]
