from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.catalog import build_tool_spec
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    JSONValue,
    ToolExecutionRecord,
    ToolSpec,
    ToolSuccess,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
    UndoBuilderBinding,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import (
    CompensationKind,
    ToolCapability,
    ToolDomain,
    UndoPayloadKind,
    UndoPolicy,
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


def _resolver(
    *,
    implementation_id: str,
    resolver_id: str,
    arg_path: str,
    presence: str,
    resolve: Any,
) -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id=cast(Any, resolver_id),
        entity_kind="application",
        arg_path=arg_path,
        presence=cast(Any, presence),
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id=implementation_id,
        resolve=resolve,
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


def _event_create(
    args: EventArgs, context: ToolExecutionContext, tool_name: str
) -> ApplicationEventCreate:
    application_id = integer(args, "application_id", tool_name)
    if (
        context.applications.get_application_scoped(context.scope_constraint, application_id)
        is None
    ):
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
            "subtype": {
                "type": "string",
                "description": "Mutually exclusive detail under event_type, e.g. written_test.subtype=assessment.",
            },
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


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def _format_pending_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=8)))
    return parsed.strftime("%Y-%m-%d %H:%M")


def _describe_create_application_event(args: Mapping[str, Any]) -> str:
    labels = {
        "written_test": "笔试",
        "interview": "面试",
        "offer_step": "Offer 进展",
        "deadline": "截止",
        "custom": "自定义",
    }
    title = labels.get(str(args.get("event_type") or ""), "日程")
    shown_time = _format_pending_datetime(str(args.get("scheduled_at") or ""))
    duration = args.get("duration_minutes")
    shown_duration = f"{duration} 分钟" if duration not in (None, "") else ""
    details = " · ".join(value for value in (title, shown_time, shown_duration) if value)
    return f"新建日程：{details}" if details else "新建日程"


def _describe_update_application_event(args: Mapping[str, Any]) -> str:
    return f"更新日程 #{args.get('id', '')}"


def _describe_delete_application_event(args: Mapping[str, Any]) -> str:
    return f"删除日程 #{args.get('id', '')}"


def _project_create_application_event_success(result: object) -> str:
    if not isinstance(result, Mapping):
        return ""
    record_id = result.get("application_event_id") or result.get("id")
    return f"✅ 创建成功：日程 #{record_id} 已保存。" if record_id else ""


def _short_preview(value: str, max_length: int = 180) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def _pending_create_application_event(
    args: Mapping[str, Any], context: ToolExecutionContext
) -> dict[str, Any]:
    try:
        application_id = int(str(args.get("application_id")))
    except (TypeError, ValueError):
        return {}
    application = context.applications.get(application_id)
    if application is None:
        return {}
    labels = {
        "written_test": "笔试",
        "interview": "面试",
        "offer_step": "Offer 进展",
        "deadline": "截止",
        "custom": "自定义",
    }
    event_label = labels.get(str(args.get("event_type") or ""), "日程")
    time_label = _format_pending_datetime(str(args.get("scheduled_at") or ""))
    duration = args.get("duration_minutes")
    try:
        duration_label = "" if duration in (None, "") else f"{int(cast(Any, duration))} 分钟"
    except (TypeError, ValueError):
        duration_label = str(duration)
    target: dict[str, Any] = {
        "id": f"application-event-draft-{application.id}",
        "kind": "application_event",
        "title": event_label,
        "meta": " · ".join(value for value in (time_label, duration_label) if value),
        "source": "pending_action",
    }
    notes = str(args.get("notes") or "")
    if notes:
        target["snippet"] = _short_preview(notes)
    evidence = {
        "id": f"application-{application.id}",
        "kind": "application",
        "title": application.company_name,
        "meta": " · ".join(
            value for value in (application.position_name, application.status) if value
        ),
        "source": "pending_action",
    }
    fields = (
        "event_type",
        "subtype",
        "scheduled_at",
        "duration_minutes",
        "location",
        "notes",
        "remind_at",
    )
    proposed_changes = [
        {"field": key, "before": "", "after": args[key]}
        for key in fields
        if args.get(key) not in (None, "", [])
    ]
    return {"target": target, "proposed_changes": proposed_changes, "evidence": [evidence]}


def _capture_no_undo_seed(context: object, args: object) -> None:
    del context, args
    return None


def _record_payload(record: ToolExecutionRecord[Any, Any]) -> dict[str, Any]:
    if not isinstance(record.outcome, ToolSuccess) or not isinstance(record.outcome.result, dict):
        return {}
    return cast(dict[str, Any], record.outcome.result)


def _canonical_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_fingerprint(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "application_id",
        "event_type",
        "subtype",
        "tags",
        "round",
        "scheduled_at",
        "duration_minutes",
        "location",
        "notes",
        "remind_at",
        "status",
    )
    result = {field: payload.get(field) for field in fields}
    result["scheduled_at"] = _canonical_datetime(result["scheduled_at"])
    result["remind_at"] = _canonical_datetime(result["remind_at"])
    return result


def _build_create_application_event_undo(
    seed: object, record: ToolExecutionRecord[Any, Any]
) -> dict[str, Any]:
    del seed
    payload = _record_payload(record)
    event_id = payload.get("application_event_id") or payload.get("id")
    try:
        resolved_id = int(str(event_id))
    except (TypeError, ValueError):
        return {}
    return {
        "kind": "delete_application_event",
        "label": "撤销新建日程",
        "application_event_id": resolved_id,
        "expected_after": _event_fingerprint(payload),
    }


def _editable(
    field: str,
    value_type: str,
    *,
    options: tuple[str, ...] | None = None,
    clearable: bool = False,
    clear_value: str | int | None = None,
) -> EditableFieldMetadataV1:
    return EditableFieldMetadataV1(
        field=field,
        value_type=cast(Any, value_type),
        options=options,
        clearable=clearable,
        clear_value=clear_value,
    )


def application_event_specs() -> tuple[ToolSpec[Any, Any], ...]:
    id_schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {"id": {"type": "integer", "description": "Application event id."}},
        "required": ["id"],
    }
    event_fields = (
        _editable("event_type", "enum", options=EVENT_TYPES),
        _editable("subtype", "string"),
        _editable("scheduled_at", "datetime"),
        _editable("remind_at", "datetime", clearable=True, clear_value=""),
        _editable("duration_minutes", "number"),
        _editable("round", "number", clearable=True, clear_value=0),
        _editable("location", "string"),
        _editable("notes", "long_text"),
        _editable("status", "string"),
    )
    create_operation = WriteOperationMetadataV1(
        undo_policy=UndoPolicy.REQUIRED,
        undo_payload_kind=UndoPayloadKind.DELETE_APPLICATION_EVENT,
        compensation_kind=CompensationKind.UNDO_CREATE_APPLICATION_EVENT,
        undo_contract_version="write-undo-payload-v1",
        undo_builder_id="create_application_event_delete_v1",
        undo_seed_phase="none",
    )
    create_undo = UndoBuilderBinding(
        descriptor=create_operation,
        implementation_id="create_application_event_delete_v1",
        capture_seed=_capture_no_undo_seed,
        build_undo=_build_create_application_event_undo,
    )
    list_resolver = _resolver(
        implementation_id="list_application_events_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="optional",
        resolve=_application_optional_binding,
    )
    get_resolver = _resolver(
        implementation_id="get_application_event_application_event_parent_v1",
        resolver_id="application_event_parent",
        arg_path="id",
        presence="required",
        resolve=_event_binding,
    )
    create_resolver = _resolver(
        implementation_id="create_application_event_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="required",
        resolve=_application_required_binding,
    )
    update_parent = _resolver(
        implementation_id="update_application_event_application_event_parent_v1",
        resolver_id="application_event_parent",
        arg_path="id",
        presence="required",
        resolve=_event_binding,
    )
    update_application = _resolver(
        implementation_id="update_application_event_application_identity_arg_v1",
        resolver_id="application_identity_arg",
        arg_path="application_id",
        presence="required",
        resolve=_application_required_binding,
    )
    delete_resolver = _resolver(
        implementation_id="delete_application_event_application_event_parent_v1",
        resolver_id="application_event_parent",
        arg_path="id",
        presence="required",
        resolve=_event_binding,
    )
    return (
        build_tool_spec(
            contract=provider_contract(
                "list_application_events",
                "List application events such as written tests, interviews, offer steps, deadlines, or custom events.",
                {
                    "type": "object",
                    "properties": {
                        "month": {
                            "type": "string",
                            "description": "Optional YYYY-MM month filter.",
                        },
                        "application_id": {"type": "integer"},
                        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
                    },
                },
            ),
            domains=(ToolDomain.EVENTS,),
            dependencies=(),
            required_capability=ToolCapability.APPLICATION_EVENTS_READ,
            binding_contract=BindingContract("scoped_collection", "application"),
            resolver_bindings=(list_resolver,),
            confirmation_policy="none",
            editable_fields=(),
            operation=ReadOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="list_application_events_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_list,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "get_application_event", "Get one application event by id.", id_schema
            ),
            domains=(ToolDomain.EVENTS,),
            dependencies=("list_application_events",),
            required_capability=ToolCapability.APPLICATION_EVENTS_READ,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(get_resolver,),
            confirmation_policy="none",
            editable_fields=(),
            operation=ReadOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="get_application_event_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_get,
            declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "create_application_event",
                "Create an application event. Use written_test.subtype=assessment for assessments.",
                _event_schema(["application_id", "event_type", "scheduled_at", "duration_minutes"]),
            ),
            domains=(ToolDomain.EVENTS,),
            dependencies=(),
            required_capability=ToolCapability.APPLICATION_EVENTS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(create_resolver,),
            confirmation_policy="required",
            editable_fields=event_fields,
            operation=create_operation,
            undo_builder_binding=create_undo,
            presentation=ToolPresentationBindingV1(
                implementation_id="create_application_event_presentation_v1",
                confirmation_description=_describe_create_application_event,
                pending_details_projector=_pending_create_application_event,
                success_summary_projector=_project_create_application_event_success,
            ),
            decoder=_decode,
            executor=_create,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "update_application_event",
                "Update an existing application event.",
                _event_schema(
                    ["id", "application_id", "event_type", "scheduled_at", "duration_minutes"]
                ),
            ),
            domains=(ToolDomain.EVENTS,),
            dependencies=("get_application_event",),
            required_capability=ToolCapability.APPLICATION_EVENTS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(update_parent, update_application),
            confirmation_policy="required",
            editable_fields=event_fields,
            operation=WriteOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="update_application_event_presentation_v1",
                confirmation_description=_describe_update_application_event,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_update,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "delete_application_event", "Delete an application event by id.", id_schema
            ),
            domains=(ToolDomain.EVENTS,),
            dependencies=("get_application_event",),
            required_capability=ToolCapability.APPLICATION_EVENTS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(delete_resolver,),
            confirmation_policy="required",
            editable_fields=(),
            operation=WriteOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="delete_application_event_presentation_v1",
                confirmation_description=_describe_delete_application_event,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ),
            decoder=_decode,
            executor=_delete,
            declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
    )
