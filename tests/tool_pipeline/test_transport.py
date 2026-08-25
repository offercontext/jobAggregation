from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from offerpilot.ai.tool_runtime.contracts import (
    BindingAudit,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    ToolResultMetadata,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.transport import project_transport_event
from tests.tool_metadata.factories import synthetic_tool_spec


def _spec(*, renderer: Any | None = None, metadata: Any | None = None) -> ToolSpec[dict[str, Any], dict[str, Any]]:
    return replace(
        synthetic_tool_spec("read_one"),
        result_metadata_projector=metadata,
        success_renderer=renderer or (lambda result: "visible success"),
    )


def _record(spec: ToolSpec[Any, Any], outcome: Any) -> ToolExecutionRecord[Any, Any]:
    prepared = PreparedToolCall(
        arguments={},
        arguments_digest="sha256:" + "a" * 64,
        binding=BindingAudit("unavailable", 0),
        contract_fingerprint="sha256:" + "b" * 64,
        spec=spec,
        tool_call_id="read-1",
        typed_args={},
    )
    return ToolExecutionRecord(execution_started=True, outcome=outcome, prepared=prepared)


@pytest.mark.parametrize(
    "category",
    (
        "validation_error",
        "permission_denied",
        "confirmation_rejected",
        "stale_state",
        "conflict",
        "not_found",
        "provider_error",
        "internal_error",
    ),
)
def test_compatibility_renderer_is_total_for_every_failure_category(category: str) -> None:
    rendered = render_compatibility(
        _spec(),
        ToolFailure(category=cast(Any, category), code="stable_code"),
    )

    assert rendered.startswith("错误：")
    assert "stable_code" not in rendered


def test_transport_projection_uses_typed_metadata_not_visible_string_parsing() -> None:
    spec = _spec(
        metadata=lambda result: ToolResultMetadata(
            affected_resources=({"type": "application", "id": 1},),
            changed_entities=({"type": "application", "id": 1},),
            evidence=({"kind": "repository"},),
        )
    )
    outcome = ToolSuccess({"opaque": "not encoded in visible success"})
    record = _record(spec, outcome)

    payload = project_transport_event(spec, record)

    assert payload == {
        "affected_resources": [{"type": "application", "id": 1}],
        "changed_entities": [{"type": "application", "id": 1}],
        "evidence": [{"kind": "repository"}],
        "status": "success",
        "summary": "visible success",
        "tool_call_id": "read-1",
        "tool_name": "read_one",
    }
    assert record.outcome is outcome


def test_renderer_or_transport_failure_never_changes_outcome() -> None:
    executor_calls = 1

    def fail(value: Any) -> Any:
        del value
        raise RuntimeError("projector failed")

    outcome = ToolSuccess({"ok": True})
    renderer_record = _record(_spec(renderer=fail), outcome)
    with pytest.raises(RuntimeError, match="projector failed"):
        render_compatibility(renderer_record.prepared.spec, outcome)
    assert renderer_record.outcome is outcome
    assert executor_calls == 1

    transport_record = _record(_spec(metadata=fail), outcome)
    with pytest.raises(RuntimeError, match="projector failed"):
        project_transport_event(transport_record.prepared.spec, transport_record)
    assert transport_record.outcome is outcome
    assert executor_calls == 1
