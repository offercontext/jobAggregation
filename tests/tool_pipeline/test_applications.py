from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from domain_harness import execute_case
from golden import load_golden

from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
from offerpilot.ai.tool_specs.applications import (
    LIST_APPLICATIONS_RESULT_BYTE_CAP,
    _list,
    _validate_create,
    application_specs,
)


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


def test_large_application_list_uses_bounded_self_describing_projection() -> None:
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            id=index,
            company_name=f"Company {index}",
            position_name=f"Platform Engineer {index}",
            job_url="https://example.test/" + "x" * 512,
            status="applied",
            source="manual",
            notes="n" * 1_024,
            applied_at=now,
            first_pending_at=None,
            first_applied_at=now,
            first_written_test_at=None,
            first_interview_at=None,
            first_offer_at=None,
            closed_reason="",
            closed_at=None,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
        for index in range(1, 501)
    ]
    observed_limit: list[int | None] = []

    def list_index_scoped(_constraint: object, *, status: str, limit: int | None = None):
        assert status == ""
        observed_limit.append(limit)
        selected = rows if limit is None else rows[:limit]
        return [
            SimpleNamespace(
                id=row.id,
                company_name=row.company_name,
                position_name=row.position_name,
                status=row.status,
                full_payload_byte_upper_bound=100_000,
            )
            for row in selected
        ]

    def unexpected_full_read(*_args: object, **_kwargs: object):
        raise AssertionError("oversized lists must not load full Application rows")

    result = _list(
        {},
        SimpleNamespace(
            applications=SimpleNamespace(
                list_application_index_scoped=list_index_scoped,
                list_applications_scoped=unexpected_full_read,
            ),
            scope_constraint=object(),
        ),
    )
    rendered = json.dumps(result, ensure_ascii=False)
    metadata = result[-1]

    assert observed_limit == [257]
    assert len(rendered.encode("utf-8")) <= LIST_APPLICATIONS_RESULT_BYTE_CAP
    assert metadata == {
        "record_type": "application_list_summary",
        "returned_count": len(result) - 1,
        "results_omitted": True,
        "details_omitted": True,
        "full_details_tool": "get_application",
        "refine_with": "status",
    }
    assert result[0] == {
        "id": 1,
        "company_name": "Company 1",
        "position_name": "Platform Engineer 1",
        "status": "applied",
    }
    assert all(set(item) == {"id", "company_name", "position_name", "status"} for item in result[:-1])


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
