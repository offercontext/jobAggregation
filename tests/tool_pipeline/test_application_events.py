from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from domain_harness import execute_case
from golden import load_golden

from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_specs.application_events import application_event_specs


EVENT_TOOLS = (
    "list_application_events",
    "get_application_event",
    "create_application_event",
    "update_application_event",
    "delete_application_event",
)


def _cases() -> list[dict[str, Any]]:
    return [
        case
        for case in load_golden("tool_outcomes_30c944f.json")["cases"]
        if case["tool_name"] in EVENT_TOOLS
    ]


def test_application_event_specs_preserve_provider_contracts() -> None:
    specs = application_event_specs()
    manifest = load_golden("provider_manifest_30c944f.json")
    expected = [
        payload
        for payload in manifest["tools"]
        if payload["function"]["name"] in EVENT_TOOLS
    ]

    assert tuple(spec.name for spec in specs) == EVENT_TOOLS
    assert materialize_provider_payloads(tuple(spec.contract for spec in specs)) == expected


@pytest.mark.parametrize("case", _cases(), ids=lambda case: f"{case['tool_name']}:{case['case']}")
def test_application_event_spec_matches_baseline_case(
    case: dict[str, Any],
    tmp_path: Path,
) -> None:
    visible, projection, handler_calls = execute_case(
        application_event_specs(), case, tmp_path / "case.db"
    )

    assert visible == case["visible_result"]
    assert handler_calls == case["handler_calls"]
    if case["business_projection"]:
        assert projection == {
            "table": case["business_projection"]["table"],
            "row_count": case["business_projection"]["row_count"],
        }
