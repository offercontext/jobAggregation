from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from offerpilot.api import _interview_preparation_request_payload
from offerpilot.models import ApplicationJDVersion
from offerpilot.repositories.interview_preparation_proposals import (
    InterviewPreparationProposalsRepository,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text
from tests.test_interview_preparation_repository import (
    JD_TEXT,
    SafeEmptyModel,
    _generate,
    _setup,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "review_readiness"
PRODUCTION = ROOT / "src" / "offerpilot"
BASELINE_FIXTURE = FIXTURES / "review_to_readiness_baseline_c5a020c.json"
LIFECYCLE_FIXTURE = FIXTURES / "event_lifecycle_v1.json"
PREPARATION_FIXTURE = FIXTURES / "interview_preparation_v1_c5a020c.json"

RAW_FIXTURE_SHA256 = {
    "event_lifecycle_v1.json": "99c8af7f91379e3ed8448a421829bb1dd664bef9526f9f6ecbd0d2057393b6ad",
    "interview_preparation_v1_c5a020c.json": (
        "e6cb81f3251250be03296ab738232731eae67e9a4a19eeaf0a1eb44278a4aaed"
    ),
    "review_to_readiness_baseline_c5a020c.json": (
        "81062c12f5066a5ba0a725ee3553081d7dcfe15dba25a288673b1eed66504f8b"
    ),
}

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


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_all_review_readiness_fixture_bytes_are_pinned() -> None:
    actual = {path.name for path in FIXTURES.glob("*.json")}
    assert actual == set(RAW_FIXTURE_SHA256)
    for name, expected in RAW_FIXTURE_SHA256.items():
        assert _raw_sha256(FIXTURES / name) == expected


def test_review_to_readiness_baseline_is_closed_unique_and_pinned() -> None:
    baseline = _load(BASELINE_FIXTURE)

    assert baseline == EXPECTED_BASELINE
    assert set(baseline) == set(EXPECTED_BASELINE)
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


def test_interview_preparation_v1_request_and_snapshot_match_pinned_builder(tmp_path) -> None:
    fixture = _load(PREPARATION_FIXTURE)
    assert set(fixture) == {
        "schema_version",
        "source_baseline",
        "contract",
        "request_identity",
        "canonical_request_json",
        "canonical_request_sha256",
        "input_snapshot_json",
        "source_fingerprint",
    }
    assert fixture["schema_version"] == 1
    assert fixture["source_baseline"] == EXPECTED_BASELINE["source_baseline"]
    assert fixture["contract"] == "interview_preparation_v1_c5a020c"

    request = json.loads(fixture["canonical_request_json"])
    assert canonical_json(request) == fixture["canonical_request_json"]
    assert sha256_text(fixture["canonical_request_json"]) == fixture[
        "canonical_request_sha256"
    ]
    parsed = _interview_preparation_request_payload(request)
    assert isinstance(parsed, dict)
    assert canonical_json(parsed) == fixture["canonical_request_json"]

    factory, ids = _setup(tmp_path)
    with factory() as session:
        version = ApplicationJDVersion(
            application_id=ids[0],
            version_number=3,
            jd_text=JD_TEXT,
            content_sha256="jd-content-sha256",
            source_kind="ui",
            idempotency_key="jd-version-baseline-0001",
            request_fingerprint_sha256="jd-request-sha256",
        )
        session.add(version)
        session.commit()
        version_id = version.id

    assert fixture["request_identity"] == {
        "application_id": ids[0],
        "application_event_id": ids[1],
        "idempotency_key": request["idempotency_key"],
    }
    assert request["resume_id"] == ids[2]
    assert request["jd_version_id"] == version_id
    result = _generate(
        InterviewPreparationProposalsRepository(factory),
        ids,
        request["idempotency_key"],
        SafeEmptyModel(),
        jd_version_id=version_id,
    )
    assert result.proposal is not None
    assert result.proposal.input_snapshot_json == fixture["input_snapshot_json"]
    assert result.proposal.source_fingerprint == fixture["source_fingerprint"]
    assert sha256_text(result.proposal.input_snapshot_json) == fixture["source_fingerprint"]


def test_production_does_not_import_or_read_review_only_fixtures() -> None:
    forbidden_literals = {
        "review_to_readiness_baseline_c5a020c.json",
        "event_lifecycle_v1.json",
        "interview_preparation_v1_c5a020c.json",
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
