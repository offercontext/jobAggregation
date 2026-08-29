from __future__ import annotations

from typing import Any

from offerpilot.ai.tool_runtime.contracts import (
    ToolExecutionRecord,
    ToolFailure,
    ToolResultMetadata,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.rendering import render_compatibility


def project_transport_event(
    spec: ToolSpec[Any, Any],
    record: ToolExecutionRecord[Any, Any],
) -> dict[str, object]:
    metadata = ToolResultMetadata()
    if (
        not isinstance(record.outcome, ToolFailure)
        and spec.result_metadata_projector is not None
    ):
        metadata = spec.result_metadata_projector(record.outcome.result)
    summary = render_compatibility(spec, record.outcome)[:500]
    return {
        "tool_call_id": record.prepared.tool_call_id,
        "tool_name": spec.name,
        "status": "error" if isinstance(record.outcome, ToolFailure) else "success",
        "summary": summary,
        "evidence": [dict(item) for item in metadata.evidence],
        "affected_resources": [dict(item) for item in metadata.affected_resources],
        "changed_entities": [dict(item) for item in metadata.changed_entities],
    }
