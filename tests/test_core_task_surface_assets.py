from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest


BASELINE = "93fb0063118761f2c76e71e4209000feee0f755b"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "core_task_surface"
REPOSITORY_ROOT = FIXTURE_ROOT.parents[2]
ASSET_NAMES = (
    "core_task_entrypoints_93fb006.json",
    "core_task_request_counts_93fb006.json",
    "core_task_visible_copy_93fb006.json",
    "interview_index_api_93fb006.json",
)

# These are intentionally literal pins.  They must be updated only after a
# human-reviewed fixture replacement, never by a test or a helper.
ASSET_SHA256: dict[str, str] = {
    "core_task_entrypoints_93fb006.json": "8ce204c055e8c151eb4e61fa2982bd2702f08d265cfbae357ef349abf34e005a",
    "core_task_request_counts_93fb006.json": "a9878d9aaea402aef2bb86906c06492955c03e045bfc052e8fc5da86d00958eb",
    "core_task_visible_copy_93fb006.json": "412a9a8f8bd22a0ad3d25b60dfd84dfdf11adc05bbd84a01dc77607cc59c4fb8",
    "interview_index_api_93fb006.json": "3c1080a68bd8c9ec135b879ea715341d808e826f7b88c069ee2ddb36b412293d",
}

CORE_TASK_IDS = frozenset(
    {
        "application.opportunity_fit",
        "application.material_kit",
        "application.interview_prepare",
        "application.interview_review",
        "application.general_review",
        "application.offer_review",
        "application.record_outcome",
        "interview.free_practice",
        "materials.resume",
        "materials.story",
        "materials.reference",
    }
)
ENTRYPOINT_CATEGORIES = frozenset({"core_task", "navigation_only", "record_management"})
ENTRYPOINT_KEYS = frozenset({"file", "qualified_symbol", "category", "task_id"})
REQUEST_KEYS = frozenset(
    {
        "flow",
        "http_reads",
        "http_mutations",
        "provider_calls",
        "tool_executor_calls",
        "sse_subscriptions",
        "domain_writes",
    }
)
VISIBLE_COPY_KEYS = frozenset({"file", "lexeme", "replacement"})
INTERVIEW_SCENARIO_KEYS = frozenset({"scenario", "list", "get"})
INTERVIEW_LIST_KEYS = frozenset({"items", "next_cursor"})
INTERVIEW_ITEM_KEYS = frozenset(
    {
        "application_id",
        "event_id",
        "company_name",
        "position_name",
        "scheduled_at",
        "note_id",
        "note_source_status",
        "has_review_proposal",
        "review_summary",
        "has_confirmed_knowledge",
        "preparation_available",
    }
)
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _load(name: str) -> tuple[Any, bytes]:
    raw = (FIXTURE_ROOT / name).read_bytes()
    return json.loads(raw.decode("utf-8")), raw


def _assert_envelope(value: Any, name: str) -> list[Any]:
    assert isinstance(value, dict), name
    assert set(value) == {"schema_version", "source_baseline", "items"}, name
    assert type(value["schema_version"]) is int and value["schema_version"] == 1
    assert value["source_baseline"] == BASELINE
    assert isinstance(value["items"], list)
    return value["items"]


def _baseline_has_file(relative_path: str) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{BASELINE}:{relative_path}"],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def test_core_task_assets_are_fixed_read_only_envelopes() -> None:
    assert tuple(sorted(path.name for path in FIXTURE_ROOT.glob("*.json"))) == tuple(
        sorted(ASSET_NAMES)
    )
    assert subprocess.run(
        ["git", "cat-file", "-e", BASELINE],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    for name in ASSET_NAMES:
        value, raw = _load(name)
        _assert_envelope(value, name)
        expected = ASSET_SHA256[name]
        assert HEX_SHA256.fullmatch(expected), f"missing fixed SHA-256 pin for {name}"
        assert hashlib.sha256(raw).hexdigest() == expected
        assert raw == _canonical_bytes(value)


def test_entrypoint_asset_uses_closed_categories_and_task_identity() -> None:
    items = _assert_envelope(_load(ASSET_NAMES[0])[0], ASSET_NAMES[0])
    assert items
    keys: set[tuple[str, str]] = set()
    core_tasks: set[str] = set()
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == ENTRYPOINT_KEYS
        assert isinstance(item["file"], str) and item["file"].startswith("web/src/")
        assert isinstance(item["qualified_symbol"], str) and item["qualified_symbol"]
        key = (item["file"], item["qualified_symbol"])
        assert key not in keys
        keys.add(key)
        assert item["category"] in ENTRYPOINT_CATEGORIES
        if item["category"] == "core_task":
            assert item["task_id"] in CORE_TASK_IDS
            core_tasks.add(item["task_id"])
        else:
            assert item["task_id"] is None
    assert core_tasks == CORE_TASK_IDS


def test_request_count_asset_is_closed_non_negative_integer_budget() -> None:
    items = _assert_envelope(_load(ASSET_NAMES[1])[0], ASSET_NAMES[1])
    assert items
    flows: set[str] = set()
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == REQUEST_KEYS
        assert isinstance(item["flow"], str) and item["flow"]
        assert item["flow"] not in flows
        flows.add(item["flow"])
        for key in REQUEST_KEYS - {"flow"}:
            assert type(item[key]) is int and item[key] >= 0


def test_visible_copy_asset_is_unique_and_anchored_to_the_baseline() -> None:
    items = _assert_envelope(_load(ASSET_NAMES[2])[0], ASSET_NAMES[2])
    assert items
    keys: set[tuple[str, str]] = set()
    for item in items:
        assert isinstance(item, dict)
        assert set(item) == VISIBLE_COPY_KEYS
        assert isinstance(item["file"], str) and item["file"].startswith("web/src/")
        assert isinstance(item["lexeme"], str) and item["lexeme"]
        assert isinstance(item["replacement"], str) and item["replacement"]
        key = (item["file"], item["lexeme"])
        assert key not in keys
        keys.add(key)
        assert _baseline_has_file(item["file"])
        baseline_text = subprocess.run(
            ["git", "show", f"{BASELINE}:{item['file']}"],
            cwd=REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            text=True,
        ).stdout
        assert item["lexeme"] in baseline_text
        assert item["lexeme"] not in item["replacement"]


def test_interview_index_golden_preserves_the_baseline_list_and_get_payload_shape() -> None:
    items = _assert_envelope(_load(ASSET_NAMES[3])[0], ASSET_NAMES[3])
    assert items
    scenarios: set[str] = set()
    for scenario in items:
        assert isinstance(scenario, dict)
        assert set(scenario) == INTERVIEW_SCENARIO_KEYS
        assert isinstance(scenario["scenario"], str) and scenario["scenario"]
        assert scenario["scenario"] not in scenarios
        scenarios.add(scenario["scenario"])
        listing = scenario["list"]
        assert isinstance(listing, dict) and set(listing) == INTERVIEW_LIST_KEYS
        assert isinstance(listing["items"], list)
        assert listing["next_cursor"] is None or isinstance(listing["next_cursor"], str)
        assert isinstance(scenario["get"], dict)
        assert set(scenario["get"]) == INTERVIEW_ITEM_KEYS
        for item in listing["items"]:
            assert isinstance(item, dict)
            assert set(item) == INTERVIEW_ITEM_KEYS


def test_production_python_does_not_read_review_only_core_task_assets() -> None:
    source_root = REPOSITORY_ROOT / "src" / "offerpilot"
    forbidden = set(ASSET_NAMES) | {"tests/fixtures/core_task_surface", "core_task_surface"}
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("tests") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("tests")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.replace("\\", "/")
                assert not any(token in normalized for token in forbidden)
