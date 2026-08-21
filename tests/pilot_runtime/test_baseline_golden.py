from __future__ import annotations

import ast
import json
from pathlib import Path


GOLDEN = Path(__file__).parents[1] / "fixtures" / "pilot_runtime" / "baseline_golden.json"


def test_pilot_runtime_baseline_golden_is_canonical_and_pinned() -> None:
    raw = GOLDEN.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schema_version"] == 1
    assert value["source_baseline"] == "b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2"
    assert raw == json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    assert value["required_existing_tests"] == [
        "test_chat_stream_emits_pilot_sse_v1_sequence",
        "test_deterministic_pilot_jd_stream_uses_fixed_events_without_model",
        "test_journal_hitl_pending_approve_executes_once_and_finishes_healthy",
        "test_journal_hitl_pending_reject_records_ledger_and_no_tool_execution",
        "test_journal_hitl_chained_pending_keeps_ledger_and_run_causal",
        "test_chat_confirm_timeout_after_write_returns_completed_fallback",
        "test_chat_confirm_stream_cancel_persists_tool_result",
        "test_chat_fails_closed_before_model_for_invalid_application_scope",
    ]
    assert value["sse_sequences"]["hitl_chain_confirm"] == [
        "meta",
        "status",
        "tool_call",
        "tool_call",
        "tool_result",
        "status",
        "confirmation_required",
        "completed",
    ]
    assert value["sse_sequences"]["hitl_confirm"] == [
        "meta",
        "status",
        "tool_call",
        "tool_result",
        "assistant_message",
        "completed",
    ]
    assert value["sse_sequences"]["hitl_entry"] == [
        "meta",
        "user_message_saved",
        "status",
        "tool_call",
        "status",
        "confirmation_required",
        "completed",
    ]
    assert value["sse_sequences"]["initial_model"] == [
        "meta",
        "user_message_saved",
        "status",
        "assistant_message",
        "completed",
    ]
    assert value["preheader_http"]["source_load_failed"] == {
        "error_code": "source_load_failed",
        "status": 503,
    }
    assert value["preheader_http"]["stale_pending_action"] == {
        "error_code": "stale_pending_action",
        "status": 409,
    }


def test_pilot_runtime_baseline_required_tests_are_defined() -> None:
    value = json.loads(GOLDEN.read_text(encoding="utf-8"))
    tests_root = Path(__file__).parents[1]
    defined_names = {
        node.name
        for path in tests_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = sorted(set(value["required_existing_tests"]) - defined_names)
    assert not missing, f"required tests missing from {tests_root}: {missing}"
