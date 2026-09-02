from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from golden import BASELINE, FIXTURES, canonical_json, load_golden


ASSET_NAMES = (
    "provider_manifest_30c944f.json",
    "tool_outcomes_30c944f.json",
    "journal_sequences_30c944f.json",
)
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "auth_token",
        "confirmation_secret",
        "exception",
        "private_key",
        "secret",
        "stack_trace",
        "traceback",
    }
)
REAL_USER_CANARIES = (
    "yuqi.chen",
    "candidate secret",
    "sk-secret-value",
    "真实简历",
    "真实职位描述",
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
SQLITE_HEADER = "SQLite format 3"


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        assert not (FORBIDDEN_KEYS & {str(key).lower() for key in value})
        for nested in value.values():
            _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk(nested)


@pytest.mark.parametrize("asset_name", ASSET_NAMES)
def test_golden_asset_is_canonical_private_and_pinned(asset_name: str) -> None:
    value = load_golden(asset_name)
    raw = (FIXTURES / asset_name).read_text(encoding="utf-8")

    assert value["baseline"] == BASELINE
    assert raw == canonical_json(value) + "\n"
    assert not WINDOWS_ABSOLUTE_PATH.search(raw)
    assert SQLITE_HEADER not in raw
    assert "Traceback (most recent call last)" not in raw
    for canary in REAL_USER_CANARIES:
        assert canary not in raw
    _walk(value)


def test_golden_loader_has_no_writer_or_update_helper() -> None:
    source_path = Path(__file__).with_name("golden.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert function_names == {"canonical_json", "load_golden"}
    assert not any(token in source_path.read_text(encoding="utf-8") for token in ("write_text", "open("))


def test_current_provider_golden_is_canonical_and_includes_create_offer() -> None:
    path = FIXTURES / "provider_manifest_current.json"
    value = load_golden("provider_manifest_current.json")
    raw = path.read_text(encoding="utf-8")

    assert value["baseline"] == "create-offer-current-v1"
    assert raw == canonical_json(value) + "\n"
    assert len(value["tools"]) == 26
    assert value["tools"][16]["function"]["name"] == "create_offer"
    assert value["tools"][16]["function"]["parameters"]["required"] == ["application_id"]
