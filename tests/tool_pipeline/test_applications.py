from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from domain_harness import execute_case
from golden import load_golden

from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_specs.applications import _validate_create, application_specs


APPLICATION_TOOLS = (
    "list_applications",
    "get_application",
    "create_application",
    "update_application_status",
)


def _cases() -> list[dict[str, Any]]:
    return [
        case
        for case in load_golden("tool_outcomes_30c944f.json")["cases"]
        if case["tool_name"] in APPLICATION_TOOLS
    ]


def test_application_specs_preserve_provider_contracts() -> None:
    specs = application_specs()
    manifest = load_golden("provider_manifest_30c944f.json")
    expected = [
        payload
        for payload in manifest["tools"]
        if payload["function"]["name"] in APPLICATION_TOOLS
    ]

    assert tuple(spec.name for spec in specs) == APPLICATION_TOOLS
    assert materialize_provider_payloads(tuple(spec.contract for spec in specs)) == expected


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        (("Mobile", "Backend"), "Backend、Mobile"),
        (("",), "unknown"),
    ],
)
def test_new_position_preflight_preserves_baseline_position_rendering(
    positions: tuple[str, ...],
    expected: str,
) -> None:
    applications = SimpleNamespace(
        list=lambda: [
            SimpleNamespace(company_name="Alpha", position_name=position)
            for position in positions
        ]
    )

    failure = _validate_create(
        {"company_name": "Alpha", "position_name": "New Role"},
        SimpleNamespace(applications=applications),
    )

    assert failure is not None
    assert failure.compatibility_detail == (
        "create_application requires explicit user confirmation before adding a new position "
        f"for existing company Alpha. Existing positions: {expected}."
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda case: f"{case['tool_name']}:{case['case']}")
def test_application_spec_matches_baseline_case(
    case: dict[str, Any],
    tmp_path: Path,
) -> None:
    visible, projection, handler_calls = execute_case(
        application_specs(), case, tmp_path / "case.db"
    )

    assert visible == case["visible_result"]
    assert handler_calls == case["handler_calls"]
    if case["business_projection"]:
        assert projection == {
            "table": case["business_projection"]["table"],
            "row_count": case["business_projection"]["row_count"],
        }
