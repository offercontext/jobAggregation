from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    JSONValue,
    ToolSpec,
)
from offerpilot.ai.tool_specs.common import (
    INPUT_EXCEPTION_MAP,
    NOT_FOUND_EXCEPTION_MAP,
    ToolInputError,
    ToolRecordNotFound,
    compact_json,
    decode_mapping,
    event_json,
    event_with_application_json,
    integer,
    optional_integer,
    provider_contract,
    resolve_identity_argument,
    resolve_parent_application,
)
from offerpilot.repositories.application_events import ApplicationEventCreate


EVENT_TYPES = ("written_test", "interview", "offer_step", "deadline", "custom")


class EventArgs(TypedDict, total=False):
    id: int
    application_id: int
    event_type: str
    subtype: str
    tags: list[str]
    scheduled_at: str
    remind_at: str
    duration_minutes: int
    round: int
    location: str
    notes: str
    status: str
    month: str


def _decode(values: Mapping[str, JSONValue]) -> EventArgs:
    return cast(EventArgs, decode_mapping(values))


def _application_optional_binding(args: EventArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="application_id",
        presence="optional",
    )


def _application_required_binding(args: EventArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="application_id",
        presence="required",
    )


def _event_binding(args: EventArgs, context: ToolExecutionContext) -> object:
    return resolve_parent_application(
        args,
        context,
        arg_path="id",
        entity_kind="application",
    )


_APPLICATION_OPTIONAL_RESOLVER = BindingResolverSpec(
    resolver_id="application_identity_arg",
    entity_kind="application",
    arg_path="application_id",
    presence="optional",
    identity_type="positive_int64",
    resolve=_application_optional_binding,
)
_APPLICATION_REQUIRED_RESOLVER = BindingResolverSpec(
    resolver_id="application_identity_arg",
    entity_kind="application",
    arg_path="application_id",
    presence="required",
    identity_type="positive_int64",
    resolve=_application_required_binding,
)
_APPLICATION_EVENT_PARENT_RESOLVER = BindingResolverSpec(
    resolver_id="application_event_parent",
    entity_kind="application",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
    resolve=_event_binding,
)


def _list(args: EventArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    rows = context.events.list_application_events_scoped(
        context.scope_constraint,
        month=str(args.get("month") or ""),
        application_id=args.get("application_id"),
        event_type=str(args.get("event_type") or ""),
    )
    return [event_with_application_json(item) for item in rows]


def _get(args: EventArgs, context: ToolExecutionContext) -> dict[str, Any]:
    event = context.events.get_application_event_scoped(
        context.scope_constraint,
        integer(args, "id", "get_application_event"),
    )
    if event is None:
        raise ToolRecordNotFound("application event not found")
    return event_json(event)


def _event_create(args: EventArgs, context: ToolExecutionContext, tool_name: str) -> ApplicationEventCreate:
    application_id = integer(args, "application_id", tool_name)
    if context.applications.get_application_scoped(
        context.scope_constraint, application_id
    ) is None:
        raise ToolRecordNotFound("application not found")
    event_type = str(args.get("event_type") or "")
    if event_type not in EVENT_TYPES:
        raise ToolInputError("invalid event type")
    scheduled_raw = str(args.get("scheduled_at") or "")
    if not scheduled_raw:
        raise ToolInputError(f"{tool_name} requires scheduled_at")
    try:
        scheduled_at = datetime.fromisoformat(scheduled_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError("scheduled_at must be RFC3339") from exc
    remind_at = None
    remind_raw = str(args.get("remind_at") or "")
    if remind_raw:
        try:
            remind_at = datetime.fromisoformat(remind_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolInputError("remind_at must be RFC3339") from exc
    duration = integer(args, "duration_minutes", tool_name)
    if duration <= 0:
        raise ToolInputError("duration_minutes must be greater than 0")
    raw_tags = args.get("tags") or []
    tags = [str(item).strip() for item in cast(list[object], raw_tags) if str(item).strip()]
    return ApplicationEventCreate(
        application_id=application_id,
        event_type=event_type,
        subtype=str(args.get("subtype") or ""),
        tags=tags,
        scheduled_at=scheduled_at,
        duration_minutes=duration,
        round=optional_integer(args, "round"),
        location=str(args.get("location") or ""),
        notes=str(args.get("notes") or ""),
        remind_at=remind_at,
        status=str(args.get("status") or "todo"),
    )


def _create(args: EventArgs, context: ToolExecutionContext) -> dict[str, Any]:
    return event_json(
        context.events.create_application_event_scoped(
            context.scope_constraint,
            _event_create(args, context, "create_application_event"),
        )
    )


def _update(args: EventArgs, context: ToolExecutionContext) -> dict[str, Any]:
    event_id = integer(args, "id", "update_application_event")
    event = context.events.update_application_event_scoped(
        context.scope_constraint,
        event_id,
        _event_create(args, context, "update_application_event"),
    )
    if event is None:
        raise ToolRecordNotFound("application event not found")
    return event_json(event)


def _delete(args: EventArgs, context: ToolExecutionContext) -> dict[str, bool]:
    return {
        "deleted": context.events.delete_application_event_scoped(
            context.scope_constraint,
            integer(args, "id", "delete_application_event"),
        )
    }


def _event_schema(required: list[JSONValue]) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "application_id": {"type": "integer"},
            "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
            "subtype": {"type": "string", "description": "Mutually exclusive detail under event_type, e.g. written_test.subtype=assessment."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "scheduled_at": {"type": "string", "description": "RFC3339 datetime."},
            "remind_at": {"type": "string", "description": "Optional RFC3339 reminder datetime."},
            "duration_minutes": {"type": "integer"},
            "round": {"type": "integer"},
            "location": {"type": "string"},
            "notes": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": required,
    }


def application_event_specs() -> tuple[ToolSpec[Any, Any], ...]:
    read = frozenset({ToolCapability.APPLICATION_EVENTS_READ})
    write = frozenset({ToolCapability.APPLICATION_EVENTS_WRITE})
    id_schema: dict[str, JSONValue] = {"type": "object", "properties": {"id": {"type": "integer", "description": "Application event id."}}, "required": ["id"]}
    return (
        ToolSpec(
            contract=provider_contract(
                "list_application_events",
                "List application events such as written tests, interviews, offer steps, deadlines, or custom events.",
                {"type": "object", "properties": {"month": {"type": "string", "description": "Optional YYYY-MM month filter."}, "application_id": {"type": "integer"}, "event_type": {"type": "string", "enum": list(EVENT_TYPES)}}},
            ),
            kind="read", decoder=_decode, executor=_list, required_capabilities=read,
            binding_contract=BindingContract("scoped_collection", "application"),
            binding_resolvers=(_APPLICATION_OPTIONAL_RESOLVER,), success_renderer=compact_json,
        ),
        ToolSpec(
            contract=provider_contract("get_application_event", "Get one application event by id.", id_schema),
            kind="read", decoder=_decode, executor=_get, required_capabilities=read,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            binding_resolvers=(_APPLICATION_EVENT_PARENT_RESOLVER,), declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        ToolSpec(
            contract=provider_contract("create_application_event", "Create an application event. Use written_test.subtype=assessment for assessments.", _event_schema(["application_id", "event_type", "scheduled_at", "duration_minutes"])),
            kind="write", decoder=_decode, executor=_create, required_capabilities=write,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            binding_resolvers=(_APPLICATION_REQUIRED_RESOLVER,), confirmation_policy="required",
            declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        ToolSpec(
            contract=provider_contract("update_application_event", "Update an existing application event.", _event_schema(["id", "application_id", "event_type", "scheduled_at", "duration_minutes"])),
            kind="write", decoder=_decode, executor=_update, required_capabilities=write,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            binding_resolvers=(_APPLICATION_EVENT_PARENT_RESOLVER, _APPLICATION_REQUIRED_RESOLVER), confirmation_policy="required",
            declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        ToolSpec(
            contract=provider_contract("delete_application_event", "Delete an application event by id.", id_schema),
            kind="write", decoder=_decode, executor=_delete, required_capabilities=write,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            binding_resolvers=(_APPLICATION_EVENT_PARENT_RESOLVER,), confirmation_policy="required",
            declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
    )
