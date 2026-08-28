from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, cast

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.agent_loop import _pending_presentation_snapshot
from offerpilot.ai.confirmation import prepare_pending_action
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.metadata import canonical_json_bytes, freeze_json
from offerpilot.ai.write_operations import (
    OperationReplay,
    TerminalPayload,
    VerifiedPendingReplay,
)
from offerpilot.pilot_runtime.continuation import _runtime_replay
from offerpilot.pilot_runtime.contracts import ConfirmationRequiredOutcome
from tests.tool_metadata.test_production_bundle import _production_components


@contextmanager
def editable_route(tool_name: str = "update_application_status"):
    bundle = _production_components().bundle
    lease = bundle.open_segment_lease()
    handle = lease.resolve(tool_name)
    assert handle is not None
    try:
        yield lease, handle
    finally:
        lease.close()


def pending(
    args: object | None = None,
    *,
    tool_name: str = "update_application_status",
) -> PendingAction:
    return PendingAction(
        tool_call_id="w1",
        tool_name=tool_name,
        args=json.dumps(args if args is not None else {"id": 7, "status": "offer"}),
        human="change status",
        operation_id="operation-1",
    )


def test_prepare_pending_action_merges_editable_fields_and_preserves_identity() -> None:
    original = pending({"id": 7, "status": "offer", "closed_reason": "old"})

    with editable_route() as (lease, handle):
        prepared = prepare_pending_action(
            original,
            lease,
            handle,
            {"status": "closed", "closed_reason": "new"},
        )

    assert prepared is not original
    assert prepared.tool_call_id == original.tool_call_id
    assert prepared.tool_name == original.tool_name
    assert prepared.operation_id == original.operation_id
    assert prepared.human
    assert json.loads(prepared.args) == {
        "id": 7,
        "status": "closed",
        "closed_reason": "new",
    }
    assert json.loads(original.args) == {"id": 7, "status": "offer", "closed_reason": "old"}


def test_prepare_pending_action_none_edits_return_same_pending() -> None:
    original = pending()

    with editable_route() as (lease, handle):
        assert prepare_pending_action(original, lease, handle, None) is original


def test_production_pending_presentation_snapshot_is_immutable_and_canonicalizable() -> None:
    action = pending(
        {"company_name": "Acme", "position_name": "Engineer", "status": "applied"},
        tool_name="create_application",
    )

    with editable_route("create_application") as (lease, handle):
        spec = lease.require_spec(handle)
        snapshot = _pending_presentation_snapshot(
            action,
            spec,
            cast(ToolExecutionContext, cast(Any, object())),
        )

    frozen = freeze_json({"editable_fields": snapshot.editable_fields, "details": snapshot.details})
    assert canonical_json_bytes(frozen)
    with pytest.raises(TypeError):
        snapshot.details["mutated"] = True  # type: ignore[index]


@pytest.mark.parametrize("edited", [["status"], "status", 1, True])
def test_prepare_pending_action_rejects_non_object_edits(edited: object) -> None:
    with pytest.raises(ValueError, match="object"):
        with editable_route() as (lease, handle):
            prepare_pending_action(pending(), lease, handle, edited)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["id", "unknown"])
def test_prepare_pending_action_rejects_non_editable_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        with editable_route() as (lease, handle):
            prepare_pending_action(pending(), lease, handle, {field: 1})


@pytest.mark.parametrize(
    ("tool_name", "field", "value"),
    [
        ("update_application_status", "status", "waiting"),
        ("update_application_status", "status", 1),
        ("update_application_status", "closed_reason", 3),
        ("create_application_event", "duration_minutes", "3"),
        ("create_application_event", "duration_minutes", True),
        ("add_note", "allow_placeholder_date", 1),
        ("create_application_event", "remind_at", "not-a-date"),
    ],
)
def test_prepare_pending_action_rejects_invalid_edit_values(
    tool_name: str,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        with editable_route(tool_name) as (lease, handle):
            prepare_pending_action(
                pending(tool_name=tool_name),
                lease,
                handle,
                {field: value},
            )


def test_prepare_pending_action_accepts_declared_clear_sentinel() -> None:
    tool_name = "create_application_event"
    with editable_route(tool_name) as (lease, handle):
        prepared = prepare_pending_action(
            pending(
                {"id": 7, "remind_at": "2026-07-10T12:30:00Z"},
                tool_name=tool_name,
            ),
            lease,
            handle,
            {"remind_at": ""},
        )

    assert json.loads(prepared.args) == {
        "id": 7,
        "remind_at": "",
    }


def test_prepare_pending_action_rejects_unknown_pending_tool() -> None:
    missing = PendingAction("w1", "missing", "{}", "missing", "operation-1")

    with pytest.raises(ValueError, match="missing"):
        with editable_route() as (lease, handle):
            prepare_pending_action(missing, lease, handle, {})


@pytest.mark.parametrize("raw_args", ["{", "[]", '"text"', "null"])
def test_prepare_pending_action_rejects_non_object_original_args(raw_args: str) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        with editable_route() as (lease, handle):
            prepare_pending_action(
                pending(raw_args),  # type: ignore[arg-type]
                lease,
                handle,
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
