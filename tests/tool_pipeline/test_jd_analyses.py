from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from domain_harness import execute_case
from golden import load_golden

from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_specs.jd_analyses import jd_analysis_specs


JD_TOOLS = ("list_jd_analyses", "get_jd_analysis")


def _cases() -> list[dict[str, Any]]:
    return [case for case in load_golden("tool_outcomes_30c944f.json")["cases"] if case["tool_name"] in JD_TOOLS]


def test_jd_analysis_specs_preserve_provider_contracts() -> None:
    specs = jd_analysis_specs()
    manifest = load_golden("provider_manifest_30c944f.json")
    expected = [payload for payload in manifest["tools"] if payload["function"]["name"] in JD_TOOLS]
    assert tuple(spec.name for spec in specs) == JD_TOOLS
    assert materialize_provider_payloads(tuple(spec.contract for spec in specs)) == expected


@pytest.mark.parametrize("case", _cases(), ids=lambda case: f"{case['tool_name']}:{case['case']}")
def test_jd_analysis_spec_matches_baseline_case(case: dict[str, Any], tmp_path: Path) -> None:
    visible, projection, handler_calls = execute_case(
        jd_analysis_specs(), case, tmp_path / "case.db"
    )
    assert visible == case["visible_result"]
    assert handler_calls == case["handler_calls"]
