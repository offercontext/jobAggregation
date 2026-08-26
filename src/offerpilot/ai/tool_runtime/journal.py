from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from offerpilot.agent_runtime.journal import EventInput, RunRecorder
from offerpilot.ai.tool_runtime.contracts import (
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
)
from offerpilot.ai.tool_runtime.metadata import ToolAuthorityEntryV1
from offerpilot.ai.tool_runtime.policy_types import OperationKind
from offerpilot.ai.types import ToolCall


def project_tool_proposed(
    recorder: RunRecorder,
    authority_entry: ToolAuthorityEntryV1,
    call: ToolCall,
) -> bool:
    if type(authority_entry) is not ToolAuthorityEntryV1:
        raise TypeError("tool proposal requires an exact ToolAuthorityEntryV1")
    authority_entry.__post_init__()
    if call.name != authority_entry.provider_name:
        raise ValueError("tool call does not match its Authority entry")
    return _append(
        recorder,
        EventInput(
            event_type="tool.proposed",
            facts={
                "tool_call_id": call.id,
                "tool_name": authority_entry.provider_name,
                "tool_kind": (
                    "write"
                    if authority_entry.operation_kind is OperationKind.TRANSACTIONAL_WRITE
                    else "read"
                ),
                "args_shape_digest": _journal_shape_digest(call.args),
                "proposal_outcome": (
                    "confirmation_required"
                    if authority_entry.confirmation_policy == "required"
                    else "execution_allowed"
                ),
            },
            source_ref_type="tool_call",
            source_ref_id=call.id,
        ),
    )


def project_tool_started(recorder: RunRecorder, prepared: PreparedToolCall[Any, Any]) -> bool:
    return _append(recorder, _tool_started_event(prepared))


def project_tool_started_bound(
    recorder: RunRecorder, session: Any, prepared: PreparedToolCall[Any, Any]
) -> bool:
    draft = prepared.journal_started_draft
    append_prepared = getattr(recorder, "append_prepared_event_bound", None)
    if draft is not None and callable(append_prepared):
        return bool(append_prepared(session, draft))
    if isinstance(draft, EventInput):
        return _append(recorder, draft)
    return False


def prepare_tool_started_draft(
    recorder: RunRecorder, prepared: PreparedToolCall[Any, Any]
) -> object | None:
    event = _tool_started_event(prepared)
    prepare = getattr(recorder, "prepare_event_draft", None)
    if callable(prepare):
        return cast(object | None, prepare(event))
    return event


def _tool_started_event(prepared: PreparedToolCall[Any, Any]) -> EventInput:
    return EventInput(
        event_type="tool.started",
        facts={
            "tool_call_id": prepared.tool_call_id,
            "tool_name": prepared.spec.name,
            "result_contract": "legacy_string_v1",
        },
        source_ref_type="tool_call",
        source_ref_id=prepared.tool_call_id,
    )


def project_tool_terminal(
    recorder: RunRecorder,
    record: ToolExecutionRecord[Any, Any],
    *,
    started_recorded: bool,
    visible_result: str,
) -> bool:
    if not record.execution_started or not started_recorded:
        return False
    if isinstance(record.outcome, ToolFailure):
        if record.outcome.category == "confirmation_rejected":
            return False
        event = EventInput(
            event_type="tool.failed",
            facts={
                "tool_call_id": record.prepared.tool_call_id,
                "tool_name": record.prepared.spec.name,
                "failure_category": (
                    "provider_error"
                    if record.outcome.category == "provider_error"
                    else "tool_error"
                ),
            },
            source_ref_type="tool_call",
            source_ref_id=record.prepared.tool_call_id,
        )
    else:
        event = EventInput(
            event_type="tool.completed",
            facts={
                "tool_call_id": record.prepared.tool_call_id,
                "tool_name": record.prepared.spec.name,
                "outcome": "completed",
                "result_shape_digest": _journal_shape_digest(visible_result),
            },
            source_ref_type="tool_call",
            source_ref_id=record.prepared.tool_call_id,
        )
    return _append(recorder, event)


def _append(recorder: RunRecorder, event: EventInput) -> bool:
    if getattr(recorder, "recording_status", "healthy") == "degraded":
        return False
    try:
        recorder.append_event(event)
    except Exception:
        marker = getattr(recorder, "mark_degraded", None)
        if callable(marker):
            try:
                marker("journal_tool_projection_failed")
            except Exception:
                pass
        return False
    return getattr(recorder, "recording_status", "healthy") != "degraded"


def _journal_shape_digest(raw: str) -> str:
    if len(raw) > 65_536:
        value: object = {"type": "oversized"}
    else:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            value = raw
    shape = _journal_value_shape(value)
    encoded = json.dumps(shape, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def journal_shape_digest(raw: str) -> str:
    """Return the frozen Phase 1 shape digest without exposing payload contents."""

    return _journal_shape_digest(raw)


def _journal_value_shape(value: object, *, depth: int = 0) -> object:
    if depth >= 16:
        return {"type": "truncated"}
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if len(mapping) > 64:
            return {"type": "object", "field_count": len(mapping), "truncated": True}
        return {
            "type": "object",
            "fields": {
                str(key): _journal_value_shape(item, depth=depth + 1)
                for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
            },
        }
    if type(value) is list:
        sequence = cast(list[object], value)
        return {
            "type": "array",
            "length": len(sequence),
            "items": [_journal_value_shape(item, depth=depth + 1) for item in sequence[:16]],
        }
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) in {int, float}:
        return {"type": "number"}
    if type(value) is str:
        return {"type": "string"}
    return {"type": "unsupported"}
