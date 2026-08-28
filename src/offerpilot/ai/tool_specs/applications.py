from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict, cast

from offerpilot.application_status import APPLICATION_STATUS_IDS, normalize_application_status
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.catalog import build_tool_spec
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    JSONValue,
    ToolFailure,
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
    CONFLICT_EXCEPTION_MAP,
    INPUT_EXCEPTION_MAP,
    NOT_FOUND_EXCEPTION_MAP,
    ToolInputError,
    ToolRecordNotFound,
    ToolStateConflict,
    application_json,
    decode_mapping,
    integer,
    provider_contract,
    resolve_identity_argument,
    spaced_json,
)
from offerpilot.repositories.applications import ApplicationCreate


class ApplicationArgs(TypedDict, total=False):
    id: int
    status: str
    company_name: str
    position_name: str
    job_url: str
    confirmed_new_position: bool
    closed_reason: str


def _decode(values: Mapping[str, JSONValue]) -> ApplicationArgs:
    return cast(ApplicationArgs, decode_mapping(values))


def _status_schema_failure(arguments: Mapping[str, JSONValue], code: str) -> str | None:
    del code
    status = arguments.get("status")
    if isinstance(status, str) and status not in APPLICATION_STATUS_IDS:
        return f"invalid application status: {status}"
    return None


def _app_binding(args: ApplicationArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="id",
        presence="required",
    )


def _application_identity_resolver(implementation_id: str) -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id="application_identity_arg",
        entity_kind="application",
        arg_path="id",
        presence="required",
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id=implementation_id,
        resolve=_app_binding,
    )


def _list(args: ApplicationArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [
        application_json(app)
        for app in context.applications.list_applications_scoped(
            context.scope_constraint,
            status=str(args.get("status") or ""),
        )
    ]


def _get(args: ApplicationArgs, context: ToolExecutionContext) -> dict[str, Any]:
    app = context.applications.get_application_scoped(
        context.scope_constraint,
        integer(args, "id", "get_application"),
    )
    if app is None:
        raise ToolRecordNotFound("application not found")
    return application_json(app)


def _validate_create(args: ApplicationArgs, context: ToolExecutionContext) -> ToolFailure | None:
    company = str(args.get("company_name") or "").strip()
    position = str(args.get("position_name") or "").strip()
    if not company or not position or args.get("confirmed_new_position") is True:
        return None
    same_company = [
        item
        for item in context.applications.list()
        if item.company_name.strip().casefold() == company.casefold()
    ]
    if not same_company:
        return None
    if any(item.position_name.strip().casefold() == position.casefold() for item in same_company):
        return None
    existing_positions = "、".join(
        sorted({item.position_name for item in same_company if item.position_name})
    )
    detail = (
        "create_application requires explicit user confirmation before adding a new position "
        f"for existing company {company}. Existing positions: {existing_positions or 'unknown'}."
    )
    return ToolFailure("conflict", "new_position_confirmation_required", detail)


def _create(args: ApplicationArgs, context: ToolExecutionContext) -> dict[str, Any]:
    try:
        status = normalize_application_status(str(args.get("status") or "applied"))
        app = context.applications.create(
            ApplicationCreate(
                company_name=str(args["company_name"]),
                position_name=str(args["position_name"]),
                job_url=str(args.get("job_url") or ""),
                status=status,
                source="ai",
                closed_reason=str(args.get("closed_reason") or ""),
            )
        )
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc
    return application_json(app)


def _update(args: ApplicationArgs, context: ToolExecutionContext) -> dict[str, Any]:
    try:
        updated = context.applications.update_application_status_scoped(
            context.scope_constraint,
            integer(args, "id", "update_application_status"),
            normalize_application_status(str(args["status"])),
            str(args.get("closed_reason") or ""),
        )
    except ValueError as exc:
        message = str(exc)
        error = ToolStateConflict if "cannot be reopened" in message else ToolInputError
        raise error(message) from exc
    if updated is None:
        raise ToolRecordNotFound("application not found")
    return application_json(updated)


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def _describe_create_application(args: Mapping[str, Any]) -> str:
    return f"新建投递：{args.get('company_name', '')} - {args.get('position_name', '')}"


def _describe_update_application_status(args: Mapping[str, Any]) -> str:
    return f"将投递 #{args.get('id', '')} 的状态改为 {args.get('status', '')}"


def _project_create_application_success(result: object) -> str:
    if not isinstance(result, Mapping):
        return ""
    record_id = result.get("application_id") or result.get("id")
    company = str(result.get("company_name") or "").strip()
    position = str(result.get("position_name") or "").strip()
    meta = " · ".join(value for value in (company, position) if value)
    return f"✅ 创建成功：投递记录 #{record_id} 已保存（{meta}）。" if record_id and meta else ""


def _short_preview(value: str, max_length: int = 180) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def _pending_create_application(
    args: Mapping[str, Any], context: object | None = None
) -> dict[str, Any]:
    del context
    company = str(args.get("company_name") or "").strip()
    position = str(args.get("position_name") or "").strip()
    status = str(args.get("status") or "applied").strip() or "applied"
    if not company and not position:
        return {}
    target: dict[str, Any] = {
        "id": f"application-draft-{company or 'unknown'}-{position or 'unknown'}",
        "kind": "application",
        "title": company or "公司待补充",
        "meta": " · ".join(value for value in (position, status) if value),
        "source": "pending_action",
    }
    notes = str(args.get("notes") or "").strip()
    if notes:
        target["snippet"] = _short_preview(notes)
    proposed_changes = [
        {"field": key, "before": "", "after": value}
        for key, value in (
            ("company_name", company),
            ("position_name", position),
            ("status", status),
            ("job_url", str(args.get("job_url") or "").strip()),
            ("notes", notes),
        )
        if value
    ]
    details: dict[str, Any] = {
        "target": target,
        "proposed_changes": proposed_changes,
        "evidence": [],
    }
    if status == "interview":
        details["workflow"] = {
            "current_step": 1,
            "total_steps": 2,
            "current_label": "新建投递",
            "next_label": "保存面试复盘",
            "description": "确认后我会继续保存这次面试复盘。",
        }
    return details


def _pending_update_application_status(
    args: Mapping[str, Any], context: ToolExecutionContext
) -> dict[str, Any]:
    application_id = args.get("id")
    try:
        resolved_id = int(str(application_id))
    except (TypeError, ValueError):
        return {}
    application = context.applications.get(resolved_id)
    if application is None:
        return {}
    target: dict[str, Any] = {
        "id": f"application-{application.id}",
        "kind": "application",
        "title": application.company_name,
        "meta": " · ".join(
            value for value in (application.position_name, application.status) if value
        ),
        "source": "pending_action",
    }
    if application.notes:
        target["snippet"] = _short_preview(application.notes)
    proposed_status = args.get("status")
    proposed_changes = []
    if isinstance(proposed_status, str) and proposed_status:
        proposed_changes.append(
            {"field": "status", "before": application.status, "after": proposed_status}
        )
    return {"target": target, "proposed_changes": proposed_changes, "evidence": [target]}


def _capture_no_undo_seed(context: object, args: object) -> None:
    del context, args
    return None


def _capture_status_undo_seed(
    context: ToolExecutionContext, args: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        application_id = int(str(args.get("id")))
    except (TypeError, ValueError):
        return {}
    application = context.applications.get(application_id)
    if application is None:
        return {}
    return {
        "application_id": application.id,
        "status": application.status,
        "closed_reason": application.closed_reason,
    }


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


def _application_fingerprint(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "company_name",
        "position_name",
        "job_url",
        "status",
        "source",
        "notes",
        "applied_at",
        "closed_reason",
        "updated_at",
    )
    result = {field: payload.get(field) for field in fields}
    result["applied_at"] = _canonical_datetime(result["applied_at"])
    result["updated_at"] = _canonical_datetime(result["updated_at"])
    return result


def _build_create_application_undo(
    seed: object, record: ToolExecutionRecord[Any, Any]
) -> dict[str, Any]:
    del seed
    payload = _record_payload(record)
    application_id = payload.get("application_id") or payload.get("id")
    try:
        resolved_id = int(str(application_id))
    except (TypeError, ValueError):
        return {}
    return {
        "kind": "delete_application",
        "label": "撤销新建投递",
        "application_id": resolved_id,
        "expected_after": _application_fingerprint(payload),
    }


def _build_update_application_status_undo(
    seed: object, record: ToolExecutionRecord[Any, Any]
) -> dict[str, Any]:
    if not isinstance(seed, Mapping) or not seed:
        return {}
    payload = _record_payload(record)
    return {
        "kind": "update_application_status",
        "label": "撤销更新投递状态",
        "application_id": seed["application_id"],
        "before": {
            "status": seed["status"],
            "closed_reason": seed["closed_reason"],
        },
        "expected_after": {
            "status": str(payload.get("status") or ""),
            "closed_reason": str(payload.get("closed_reason") or ""),
        },
    }


def _editable(
    field: str,
    value_type: str,
    *,
    options: tuple[str, ...] | None = None,
) -> EditableFieldMetadataV1:
    return EditableFieldMetadataV1(
        field=field,
        value_type=cast(Any, value_type),
        options=options,
        clearable=False,
        clear_value=None,
    )


def application_specs() -> tuple[ToolSpec[Any, Any], ...]:
    statuses = cast(list[JSONValue], list(APPLICATION_STATUS_IDS))
    status_options = tuple(APPLICATION_STATUS_IDS)
    create_operation = WriteOperationMetadataV1(
        undo_policy=UndoPolicy.REQUIRED,
        undo_payload_kind=UndoPayloadKind.DELETE_APPLICATION,
        compensation_kind=CompensationKind.UNDO_CREATE_APPLICATION,
        undo_contract_version="write-undo-payload-v1",
        undo_builder_id="create_application_delete_v1",
        undo_seed_phase="none",
    )
    create_undo = UndoBuilderBinding(
        descriptor=create_operation,
        implementation_id="create_application_delete_v1",
        capture_seed=_capture_no_undo_seed,
        build_undo=_build_create_application_undo,
    )
    update_operation = WriteOperationMetadataV1(
        undo_policy=UndoPolicy.REQUIRED,
        undo_payload_kind=UndoPayloadKind.UPDATE_APPLICATION_STATUS,
        compensation_kind=CompensationKind.UNDO_UPDATE_APPLICATION_STATUS,
        undo_contract_version="write-undo-payload-v1",
        undo_builder_id="update_application_status_restore_v1",
        undo_seed_phase="before_execute",
    )
    update_undo = UndoBuilderBinding(
        descriptor=update_operation,
        implementation_id="update_application_status_restore_v1",
        capture_seed=_capture_status_undo_seed,
        build_undo=_build_update_application_status_undo,
    )
    get_resolver = _application_identity_resolver("get_application_application_identity_arg_v1")
    update_resolver = _application_identity_resolver(
        "update_application_status_application_identity_arg_v1"
    )
    return (
        build_tool_spec(
            contract=provider_contract(
                "list_applications",
                "List job applications. Optionally filter by canonical application status.",
                {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": statuses,
                            "description": "Optional status filter.",
                        }
                    },
                },
            ),
            domains=(ToolDomain.APPLICATIONS,),
            dependencies=(),
            required_capability=ToolCapability.APPLICATIONS_READ,
            binding_contract=BindingContract("scoped_collection", "application"),
            resolver_bindings=(),
            confirmation_policy="none",
            editable_fields=(),
            operation=ReadOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="list_applications_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=spaced_json,
            ),
            decoder=_decode,
            executor=_list,
            success_renderer=spaced_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "get_application",
                "Get one job application by id. Use an id returned by list_applications.",
                {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "Application id returned by list_applications.",
                        }
                    },
                    "required": ["id"],
                },
            ),
            domains=(ToolDomain.APPLICATIONS,),
            dependencies=("list_applications",),
            required_capability=ToolCapability.APPLICATIONS_READ,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(get_resolver,),
            confirmation_policy="none",
            editable_fields=(),
            operation=ReadOperationMetadataV1(),
            undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="get_application_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=spaced_json,
            ),
            decoder=_decode,
            executor=_get,
            declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP,
            success_renderer=spaced_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "create_application",
                "Create a job application record. If the same company already has records but this is a new position, ask the user before creating it; set confirmed_new_position=true only after the user explicitly confirms the new position should be added.",
                {
                    "type": "object",
                    "properties": {
                        "company_name": {"type": "string"},
                        "position_name": {"type": "string"},
                        "job_url": {"type": "string"},
                        "status": {"type": "string", "enum": statuses},
                        "confirmed_new_position": {
                            "type": "boolean",
                            "description": "Set true only when the user explicitly confirmed creating a new position for an existing company.",
                        },
                        "closed_reason": {
                            "type": "string",
                            "description": "Required when status is closed.",
                        },
                    },
                    "required": ["company_name", "position_name"],
                },
            ),
            domains=(ToolDomain.APPLICATIONS,),
            dependencies=(),
            required_capability=ToolCapability.APPLICATIONS_WRITE,
            binding_contract=BindingContract("non_application_only"),
            resolver_bindings=(),
            confirmation_policy="required",
            editable_fields=(
                _editable("company_name", "string"),
                _editable("position_name", "string"),
                _editable("job_url", "string"),
                _editable("status", "enum", options=status_options),
                _editable("closed_reason", "long_text"),
            ),
            operation=create_operation,
            undo_builder_binding=create_undo,
            presentation=ToolPresentationBindingV1(
                implementation_id="create_application_presentation_v1",
                confirmation_description=_describe_create_application,
                pending_details_projector=_pending_create_application,
                success_summary_projector=_project_create_application_success,
            ),
            decoder=_decode,
            executor=_create,
            preflight=_validate_create,
            mutable_validator=_validate_create,
            declared_failure_categories=frozenset({"validation_error", "conflict"}),
            exception_map=INPUT_EXCEPTION_MAP,
            success_renderer=spaced_json,
            schema_failure_renderer=_status_schema_failure,
        ),
        build_tool_spec(
            contract=provider_contract(
                "update_application_status",
                "Update one job application's status. Use an id returned by list_applications.",
                {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "Application id returned by list_applications.",
                        },
                        "status": {"type": "string", "enum": statuses},
                        "closed_reason": {
                            "type": "string",
                            "description": "Required when status is closed.",
                        },
                    },
                    "required": ["id", "status"],
                },
            ),
            domains=(ToolDomain.APPLICATIONS,),
            dependencies=("get_application",),
            required_capability=ToolCapability.APPLICATIONS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(update_resolver,),
            confirmation_policy="required",
            editable_fields=(
                _editable("status", "enum", options=status_options),
                _editable("closed_reason", "long_text"),
            ),
            operation=update_operation,
            undo_builder_binding=update_undo,
            presentation=ToolPresentationBindingV1(
                implementation_id="update_application_status_presentation_v1",
                confirmation_description=_describe_update_application_status,
                pending_details_projector=_pending_update_application_status,
                success_summary_projector=spaced_json,
            ),
            decoder=_decode,
            executor=_update,
            declared_failure_categories=frozenset({"validation_error", "not_found", "conflict"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP + CONFLICT_EXCEPTION_MAP,
            success_renderer=spaced_json,
            schema_failure_renderer=_status_schema_failure,
        ),
    )
