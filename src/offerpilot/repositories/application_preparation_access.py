"""Read-only eligibility for interview preparation, not a new write authority."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from offerpilot.models import Application, WriteOperation, WriteOperationTransition


def can_prepare_application(session: Session, application: Application) -> bool:
    if application.deleted_at is not None:
        return False
    if application.source in {"cli", "manual", "web"}:
        return True
    if application.source != "ai":
        return False

    # Restrict before materializing payloads, and never parse malformed JSON in SQL.
    result = case(
        (func.json_valid(WriteOperation.result_json), WriteOperation.result_json),
        else_="null",
    )
    operations = list(
        session.scalars(
            select(WriteOperation)
            .where(
                WriteOperation.operation_role == "primary",
                WriteOperation.adapter_kind == "typed",
                WriteOperation.tool_name == "create_application",
                WriteOperation.status == "committed",
                func.json_extract(result, "$.application_id") == application.id,
            )
            .limit(2)
        )
    )
    if len(operations) != 1:
        return False
    operation = operations[0]
    if (
        operation.result_contract != "typed_json_v1"
        or operation.failure_category is not None
        or operation.failure_code is not None
        or not operation.input_fingerprint
        or not operation.operation_request_fingerprint
        or not operation.confirmation_token_fingerprint
        or not operation.authorization_scope_fingerprint
    ):
        return False

    # Runtime imports this repository indirectly. Keep its established terminal
    # validator behind the function boundary, without introducing a module cycle.
    from offerpilot.ai.write_operations import WriteOperationError, payload_from_operation

    try:
        payload = json.loads(payload_from_operation(operation).result_json)
        if (
            not isinstance(payload, dict)
            or payload.get("record_type") != "application"
            or payload.get("source") != "ai"
            or type(payload.get("id")) is not int
            or payload["id"] != application.id
            or type(payload.get("application_id")) is not int
            or payload["application_id"] != application.id
        ):
            return False
        created_at = datetime.fromisoformat(payload["created_at"])
        if _utc(created_at) != _utc(application.created_at):
            return False
    except (WriteOperationError, ValueError, TypeError, KeyError, OverflowError):
        return False

    transitions = list(
        session.execute(
            select(
                WriteOperationTransition.seq,
                WriteOperationTransition.state,
            )
            .where(WriteOperationTransition.operation_id == operation.id)
            .order_by(WriteOperationTransition.seq)
            .limit(5)
        )
    )
    return [tuple(row) for row in transitions] == [
        (1, "proposed"),
        (2, "approved"),
        (3, "claimed"),
        (4, "committed"),
    ]


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )
