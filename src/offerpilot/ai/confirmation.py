from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_runtime.catalog import SegmentToolCatalogLease, SegmentToolSpecHandle
from offerpilot.ai.tool_runtime.contracts import ToolSpec
from offerpilot.ai.tool_runtime.validation import ArgumentValidationError, parse_arguments


def prepare_pending_action(
    pending: PendingAction,
    catalog_lease: SegmentToolCatalogLease,
    spec_handle: SegmentToolSpecHandle,
    edited_args: dict[str, Any] | None,
) -> PendingAction:
    """Validate editable confirmation fields and return the effective Pending."""

    if type(catalog_lease) is not SegmentToolCatalogLease:
        raise TypeError("Pending preparation requires an exact Segment Catalog lease")
    if type(spec_handle) is not SegmentToolSpecHandle:
        raise TypeError("Pending preparation requires an exact Segment Spec handle")
    try:
        spec = catalog_lease.require_spec(spec_handle)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("Pending preparation route is unavailable") from exc
    if spec.name != pending.tool_name:
        raise ValueError(f'unknown pending tool "{pending.tool_name}"')
    if edited_args is None:
        return pending
    if not isinstance(edited_args, dict):
        raise ValueError("edited arguments must be a JSON object")

    original_args = _parse_json_object(
        pending.args,
        "pending arguments must be a valid JSON object",
    )
    editable_fields = {
        descriptor.field: descriptor.to_compat_descriptor()
        for descriptor in spec.metadata.editable_fields
    }
    non_editable = [str(field) for field in edited_args if field not in editable_fields]
    if non_editable:
        raise ValueError("non-editable fields: " + ", ".join(sorted(non_editable)))

    for field, value in edited_args.items():
        _validate_edited_value(str(field), value, editable_fields[field])

    encoded_args = _encode_json_object({**original_args, **edited_args})
    return PendingAction(
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        args=encoded_args,
        human=_spec_confirmation_description(spec, encoded_args, pending.human),
        operation_id=pending.operation_id,
    )


def _parse_json_object(raw: str, error_message: str) -> dict[str, Any]:
    try:
        parsed = parse_arguments(raw)
    except ArgumentValidationError as exc:
        raise ValueError(error_message) from exc
    return cast(dict[str, Any], parsed)


def _encode_json_object(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _same_json_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(left_value, right_value)
            for left_value, right_value in zip(left, right, strict=True)
        )
    return bool(left == right)


def _is_json_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_edited_value(
    field: str,
    value: Any,
    descriptor: Mapping[str, Any],
) -> None:
    if (
        descriptor.get("clearable") is True
        and "clear_value" in descriptor
        and _is_json_scalar(descriptor["clear_value"])
        and _same_json_value(value, descriptor["clear_value"])
    ):
        return
    field_type = descriptor.get("type")
    if field_type in {"string", "long_text"}:
        if not isinstance(value, str):
            raise ValueError(f'edited field "{field}" must be a string')
        return
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'edited field "{field}" must be a finite number')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f'edited field "{field}" must be a finite number')
        return
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f'edited field "{field}" must be a boolean')
        return
    if field_type == "enum":
        options = descriptor.get("options")
        if not isinstance(value, str) or not isinstance(options, list) or value not in options:
            raise ValueError(f'edited field "{field}" must be one of the configured options')
        return
    if field_type == "datetime":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'edited field "{field}" must be an ISO/RFC3339 datetime string')
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f'edited field "{field}" must be an ISO/RFC3339 datetime string'
            ) from exc
        return
    raise ValueError(f'edited field "{field}" has unknown descriptor type "{field_type}"')


def _spec_confirmation_description(
    spec: ToolSpec[Any, Any] | None,
    args: str,
    fallback: str,
) -> str:
    if spec is None:
        return fallback
    try:
        parsed = parse_arguments(args)
        human = spec.presentation.confirmation_description(spec.decoder(parsed))
    except Exception:
        return fallback
    return str(human or fallback)


__all__ = ["prepare_pending_action"]
