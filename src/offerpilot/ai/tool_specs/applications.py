from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.application_status import APPLICATION_STATUS_IDS, normalize_application_status
from offerpilot.ai.tool_runtime.context import ToolCapability, ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    JSONValue,
    ToolFailure,
    ToolSpec,
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


_APPLICATION_IDENTITY_RESOLVER = BindingResolverSpec(
    resolver_id="application_identity_arg",
    entity_kind="application",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
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
        item for item in context.applications.list() if item.company_name.strip().casefold() == company.casefold()
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


def application_specs() -> tuple[ToolSpec[Any, Any], ...]:
    statuses = cast(list[JSONValue], list(APPLICATION_STATUS_IDS))
    specs: tuple[ToolSpec[Any, Any], ...] = (
        ToolSpec(
            contract=provider_contract(
                "list_applications",
                "List job applications. Optionally filter by canonical application status.",
                {"type": "object", "properties": {"status": {"type": "string", "enum": statuses, "description": "Optional status filter."}}},
            ),
            kind="read", decoder=_decode, executor=_list,
            required_capabilities=frozenset({ToolCapability.APPLICATIONS_READ}),
            binding_contract=BindingContract("scoped_collection", "application"),
            success_renderer=spaced_json,
        ),
        ToolSpec(
            contract=provider_contract(
                "get_application",
                "Get one job application by id. Use an id returned by list_applications.",
                {"type": "object", "properties": {"id": {"type": "integer", "description": "Application id returned by list_applications."}}, "required": ["id"]},
            ),
            kind="read", decoder=_decode, executor=_get,
            required_capabilities=frozenset({ToolCapability.APPLICATIONS_READ}),
            binding_contract=BindingContract("enforce_if_bound", "application"),
            binding_resolvers=(_APPLICATION_IDENTITY_RESOLVER,), declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=spaced_json,
        ),
        ToolSpec(
            contract=provider_contract(
                "create_application",
                "Create a job application record. If the same company already has records but this is a new position, ask the user before creating it; set confirmed_new_position=true only after the user explicitly confirms the new position should be added.",
                {"type": "object", "properties": {"company_name": {"type": "string"}, "position_name": {"type": "string"}, "job_url": {"type": "string"}, "status": {"type": "string", "enum": statuses}, "confirmed_new_position": {"type": "boolean", "description": "Set true only when the user explicitly confirmed creating a new position for an existing company."}, "closed_reason": {"type": "string", "description": "Required when status is closed."}}, "required": ["company_name", "position_name"]},
            ),
            kind="write", decoder=_decode, executor=_create,
            required_capabilities=frozenset({ToolCapability.APPLICATIONS_WRITE}), confirmation_policy="required",
            binding_contract=BindingContract("non_application_only"),
            preflight=_validate_create, mutable_validator=_validate_create,
            declared_failure_categories=frozenset({"validation_error", "conflict"}), exception_map=INPUT_EXCEPTION_MAP,
            success_renderer=spaced_json,
            schema_failure_renderer=_status_schema_failure,
        ),
        ToolSpec(
            contract=provider_contract(
                "update_application_status",
                "Update one job application's status. Use an id returned by list_applications.",
                {"type": "object", "properties": {"id": {"type": "integer", "description": "Application id returned by list_applications."}, "status": {"type": "string", "enum": statuses}, "closed_reason": {"type": "string", "description": "Required when status is closed."}}, "required": ["id", "status"]},
            ),
            kind="write", decoder=_decode, executor=_update,
            required_capabilities=frozenset({ToolCapability.APPLICATIONS_WRITE}),
            binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_APPLICATION_IDENTITY_RESOLVER,),
            confirmation_policy="required", declared_failure_categories=frozenset({"validation_error", "not_found", "conflict"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP + CONFLICT_EXCEPTION_MAP, success_renderer=spaced_json,
            schema_failure_renderer=_status_schema_failure,
        ),
    )
    return specs
