from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "review_readiness"
PRODUCTION = ROOT / "src" / "offerpilot"
BASELINE_FIXTURE = FIXTURES / "review_to_readiness_baseline_c5a020c.json"
LIFECYCLE_FIXTURE = FIXTURES / "event_lifecycle_v1.json"

BASELINE_SHA256 = "a0acdef611cf8795691aa44739763e8b9db210aa79443298a5f8f77de4f7aa73"
LIFECYCLE_SHA256 = "efb90505b17de008accd5303fe16d50e3c50a661e48a205ecf77f96a0f688b04"

EXPECTED_BASELINE = {
    "schema_version": 1,
    "source_baseline": "c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff",
    "provider_tools": 25,
    "legacy_deterministic": 3,
    "agent_compensations": 4,
    "product_actions": [
        "confirm_interview_story",
        "save_review_readiness_signal",
    ],
    "product_action_compensations": [
        "undo:confirm_interview_story",
        "undo:save_review_readiness_signal",
    ],
    "production_contributors_disabled": [
        "confirmed_memory",
        "knowledge_context",
        "older_conversation_summary",
    ],
}

EXPECTED_LIFECYCLE_CASES: tuple[tuple[object, str], ...] = (
    ("todo", "scheduled"),
    ("pending", "scheduled"),
    ("scheduled", "scheduled"),
    ("in_progress", "in_progress"),
    ("done", "completed"),
    ("completed", "completed"),
    ("cancelled", "cancelled"),
    ("deleted", "cancelled"),
    ("soft_deleted", "cancelled"),
    ("unexpected", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    (0, "unknown"),
    (True, "unknown"),
    ([], "unknown"),
    ({}, "unknown"),
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_review_to_readiness_baseline_is_closed_unique_and_pinned() -> None:
    baseline = _load(BASELINE_FIXTURE)

    assert baseline == EXPECTED_BASELINE
    assert set(baseline) == set(EXPECTED_BASELINE)
    assert _canonical_sha256(baseline) == BASELINE_SHA256
    for key in (
        "product_actions",
        "product_action_compensations",
        "production_contributors_disabled",
    ):
        values = baseline[key]
        assert len(values) == len(set(values)), f"duplicate values in {key}"


def test_event_lifecycle_fixture_freezes_all_aliases_and_unknown_types() -> None:
    fixture = _load(LIFECYCLE_FIXTURE)

    assert set(fixture) == {"schema_version", "contract", "cases"}
    assert fixture["schema_version"] == 1
    assert fixture["contract"] == "event_lifecycle_v1"
    assert all(set(case) == {"status", "expected"} for case in fixture["cases"])
    assert tuple((case["status"], case["expected"]) for case in fixture["cases"]) == (
        EXPECTED_LIFECYCLE_CASES
    )
    identities = [
        json.dumps(case["status"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for case in fixture["cases"]
    ]
    assert len(identities) == len(set(identities))
    assert {case["expected"] for case in fixture["cases"]} == {
        "scheduled",
        "in_progress",
        "completed",
        "cancelled",
        "unknown",
    }
    assert _canonical_sha256(fixture) == LIFECYCLE_SHA256


def test_production_does_not_import_or_read_review_only_fixtures() -> None:
    forbidden_literals = {
        "review_to_readiness_baseline_c5a020c.json",
        "event_lifecycle_v1.json",
        "tests/fixtures/review_readiness",
        "tests\\fixtures\\review_readiness",
        "tests.fixtures.review_readiness",
    }
    violations: list[str] = []

    for path in sorted(PRODUCTION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.replace("\\", "/")
                if any(
                    forbidden.replace("\\", "/") in normalized
                    for forbidden in forbidden_literals
                ):
                    violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("tests.fixtures.review_readiness"):
                        violations.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                        )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "tests.fixtures.review_readiness"
            ):
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")

    assert violations == []
