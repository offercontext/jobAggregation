from __future__ import annotations

import ast
import json
from pathlib import Path


TESTS_ROOT = Path(__file__).parents[1]
GOLDEN = TESTS_ROOT / "fixtures" / "agent_loop" / "baseline_aaecf5d.json"


def test_agent_loop_baseline_is_canonical_and_pinned() -> None:
    raw = GOLDEN.read_text(encoding="utf-8")
    value = json.loads(raw)

    assert value["schema_version"] == 1
    assert value["source_baseline"] == "aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb"
    assert raw == json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    assert value["tool_call_selection"] == {
        "read_read": ["read-1", "read-2"],
        "read_write": ["read-1"],
        "write_read": ["write-1"],
        "write_write": ["write-1"],
    }
    assert value["provider_free_routes"] == [
        "deterministic_action",
        "reject",
        "terminal_replay",
        "delivery_recovery",
    ]
    assert value["agent_outcomes"] == {
        "final": {"pending": False, "reply": "non-empty"},
        "pending": {"pending": True, "reply": ""},
    }
    assert value["approved_origin_event_order"] == ["tool_call", "tool_result"]
    assert value["sse_sequences"] == {
        "confirmation_final": [
            "meta",
            "status",
            "tool_call",
            "tool_result",
            "assistant_message",
            "completed",
        ],
        "confirmation_pending": [
            "meta",
            "status",
            "tool_call",
            "tool_call",
            "tool_result",
            "status",
            "confirmation_required",
            "completed",
        ],
        "new_turn_final": [
            "meta",
            "user_message_saved",
            "status",
            "assistant_message",
            "completed",
        ],
        "new_turn_pending": [
            "meta",
            "user_message_saved",
            "status",
            "tool_call",
            "status",
            "confirmation_required",
            "completed",
        ],
    }


def test_agent_loop_baseline_required_tests_exist() -> None:
    value = json.loads(GOLDEN.read_text(encoding="utf-8"))
    defined = {
        node.name
        for path in TESTS_ROOT.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    missing = sorted(set(value["required_existing_tests"]) - defined)
    assert not missing, f"baseline characterization tests missing: {missing}"


def test_agent_loop_baseline_has_no_update_mechanism() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=__file__)
    forbidden_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert forbidden_calls.isdisjoint({"write_text", "write_bytes", "open"})
