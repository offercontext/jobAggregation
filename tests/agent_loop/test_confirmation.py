from __future__ import annotations

import json
from typing import Any

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.confirmation import prepare_pending_action
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.contracts import (
    ProviderToolContract,
    ToolExceptionMapping,
    ToolSpec,
    WriteContract,
)
from offerpilot.ai.write_operations import (
    OperationReplay,
    TerminalPayload,
    VerifiedPendingReplay,
)
from offerpilot.pilot_runtime.continuation import _runtime_replay
from offerpilot.pilot_runtime.contracts import ConfirmationRequiredOutcome


_EDITABLE_FIELDS = (
    {"field": "status", "type": "enum", "options": ["offer", "rejected"]},
    {"field": "title", "type": "string"},
    {"field": "score", "type": "number"},
    {"field": "active", "type": "boolean"},
    {"field": "remind_at", "type": "datetime", "clearable": True, "clear_value": ""},
)


def editable_catalog(
    *,
    editable_fields: tuple[dict[str, Any], ...] = _EDITABLE_FIELDS,
) -> ToolCatalog:
    name = "update_application_status"
    schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
    contract = ProviderToolContract(
        payload={
            "type": "function",
            "function": {"name": name, "description": name, "parameters": schema},
        },
        name=name,
        description=name,
        parameters=schema,
    )
    spec = ToolSpec(
        contract=contract,
        kind="write",
        decoder=lambda values: dict(values),
        executor=lambda args, _context: args,
        confirmation_policy="required",
        editable_fields=editable_fields,
        declared_failure_categories=frozenset({"internal_error"}),
        exception_map=(ToolExceptionMapping(Exception, "internal_error", "test_error"),),
        success_renderer=str,
        confirmation_description=lambda _args: "change status",
        write_contract=WriteContract(),
    )
    return ToolCatalog((spec,), expected_names=(name,))


def pending(args: object | None = None) -> PendingAction:
    return PendingAction(
        tool_call_id="w1",
        tool_name="update_application_status",
        args=json.dumps(args if args is not None else {"id": 7, "status": "offer"}),
        human="change status",
        operation_id="operation-1",
    )


def test_prepare_pending_action_merges_editable_fields_and_preserves_identity() -> None:
    original = pending({"id": 7, "status": "offer", "title": "old"})

    prepared = prepare_pending_action(
        original,
        editable_catalog(),
        {"status": "rejected", "title": "new"},
    )

    assert prepared is not original
    assert prepared.tool_call_id == original.tool_call_id
    assert prepared.tool_name == original.tool_name
    assert prepared.operation_id == original.operation_id
    assert prepared.human == "change status"
    assert json.loads(prepared.args) == {"id": 7, "status": "rejected", "title": "new"}
    assert json.loads(original.args) == {"id": 7, "status": "offer", "title": "old"}


def test_prepare_pending_action_none_edits_return_same_pending() -> None:
    original = pending()

    assert prepare_pending_action(original, editable_catalog(), None) is original


@pytest.mark.parametrize("edited", [["status"], "status", 1, True])
def test_prepare_pending_action_rejects_non_object_edits(edited: object) -> None:
    with pytest.raises(ValueError, match="object"):
        prepare_pending_action(pending(), editable_catalog(), edited)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["id", "unknown"])
def test_prepare_pending_action_rejects_non_editable_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        prepare_pending_action(pending(), editable_catalog(), {field: 1})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "waiting"),
        ("status", 1),
        ("title", 3),
        ("score", "3"),
        ("score", True),
        ("active", 1),
        ("remind_at", "not-a-date"),
    ],
)
def test_prepare_pending_action_rejects_invalid_edit_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        prepare_pending_action(pending(), editable_catalog(), {field: value})


def test_prepare_pending_action_accepts_declared_clear_sentinel() -> None:
    prepared = prepare_pending_action(
        pending({"id": 7, "status": "offer", "remind_at": "2026-07-10T12:30:00Z"}),
        editable_catalog(),
        {"remind_at": ""},
    )

    assert json.loads(prepared.args) == {
        "id": 7,
        "status": "offer",
        "remind_at": "",
    }


def test_prepare_pending_action_rejects_unknown_pending_tool() -> None:
    missing = PendingAction("w1", "missing", "{}", "missing", "operation-1")

    with pytest.raises(ValueError, match="missing"):
        prepare_pending_action(missing, editable_catalog(), {})


@pytest.mark.parametrize("raw_args", ["{", "[]", '"text"', "null"])
def test_prepare_pending_action_rejects_non_object_original_args(raw_args: str) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        prepare_pending_action(
            pending(raw_args),  # type: ignore[arg-type]
            editable_catalog(),
            {},
        )


@pytest.mark.parametrize(
    "raw_args",
    (pytest.param('{"malformed":', id="malformed"), pytest.param("x" * 65_537, id="oversized")),
)
def test_pending_identity_can_be_carried_without_eager_argument_decode(raw_args: str) -> None:
    action = PendingAction(
        "w1",
        "update_application_status",
        raw_args,
        "private",
        "operation-1",
    )

    assert action.args is raw_args
    assert action.operation_id == "operation-1"


def test_chained_replay_projects_only_repository_verified_typed_arguments() -> None:
    child = VerifiedPendingReplay(
        adapter_kind="typed",
        conversation_id=7,
        operation_id="child-operation",
        tool_call_id="child-call",
        tool_name="update_application_status",
        raw_args='{"id":7,"status":"offer"}',
        human="change status",
        confirmation_token_fingerprint="hmac-sha256:" + "0" * 64,
        decoded_args={"id": 7, "status": "offer"},
    )
    replay = OperationReplay(
        operation_id="origin-operation",
        payload=TerminalPayload(
            status="committed",
            result_contract="typed_json_v1",
            result_json="{}",
            visible_result="done",
            transport_json="{}",
            undo_json=None,
            failure_category=None,
            failure_code=None,
            digest="sha256:" + "0" * 64,
        ),
        delivery_status="completed",
        delivery_generation=1,
        delivery_lease_expires_at=None,
        delivery_outcome="chained_pending",
        chained_pending=child,
    )

    outcome = _runtime_replay(replay, 7)

    assert isinstance(outcome, ConfirmationRequiredOutcome)
    assert outcome.pending_action.operation_id == "child-operation"
    assert dict(outcome.pending_action.args) == {"id": 7, "status": "offer"}
