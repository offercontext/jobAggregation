from __future__ import annotations

import json
from pathlib import Path

import pytest

from offerpilot.event_lifecycle import classify_event_lifecycle_v1


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "review_readiness"
    / "event_lifecycle_v1.json"
)


def test_backend_classifier_matches_every_pinned_event_lifecycle_case() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["contract"] == "event_lifecycle_v1"
    assert fixture["unknown_fallback"] == "unknown"
    assert [
        classify_event_lifecycle_v1(case["status"])
        for case in fixture["cases"]
    ] == [case["expected"] for case in fixture["cases"]]


def test_unknown_fallback_is_pinned_by_the_shared_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert classify_event_lifecycle_v1("not-in-the-fixture") == fixture["unknown_fallback"]


@pytest.mark.parametrize(
    "status",
    [False, 1, 1.0, b"done", ("done",), {"status": "done"}],
)
def test_event_lifecycle_unknown_fallback_is_exact_type_safe(status: object) -> None:
    assert classify_event_lifecycle_v1(status) == "unknown"
