from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
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
    integer,
    offer_json,
    provider_contract,
    resolve_parent_application,
    resolve_identity_argument,
)
from offerpilot.repositories.offers import OfferCreate


OFFER_STATUSES = ("pending", "negotiating", "accepted", "declined", "expired")


class OfferArgs(TypedDict, total=False):
    id: int
    application_id: int
    ids: list[int]
    status: str
    company_name: str
    position_name: str
    base_monthly: int
    months_per_year: int
    signing_bonus: int
    equity: str
    perks: str
    deadline: str
    notes: str
    assessment: str


def _decode(values: Mapping[str, JSONValue]) -> OfferArgs:
    return cast(OfferArgs, decode_mapping(values))


def _offer_binding(args: OfferArgs, context: ToolExecutionContext) -> object:
    return resolve_parent_application(
        args,
        context,
        arg_path="id",
        entity_kind="application",
    )


def _offer_resolver(implementation_id: str) -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id="offer_application_parent",
        entity_kind="application",
        arg_path="id",
        presence="required",
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id=implementation_id,
        resolve=_offer_binding,
    )


def _application_required_binding(args: OfferArgs, context: ToolExecutionContext) -> object:
    return resolve_identity_argument(
        args,
        context,
        entity_kind="application",
        arg_path="application_id",
        presence="required",
    )


def _create_resolver() -> ResolverImplementationBinding:
    descriptor = BindingResolverDescriptorV1(
        resolver_id="application_identity_arg",
        entity_kind="application",
        arg_path="application_id",
        presence="required",
        identity_type="positive_int64",
    )
    return ResolverImplementationBinding(
        descriptor=descriptor,
        implementation_id="create_offer_application_identity_arg_v1",
        resolve=_application_required_binding,
    )


def _list(args: OfferArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [
        offer_json(offer)
        for offer in context.offers.list_offers_scoped(
            context.scope_constraint,
            status=str(args.get("status") or ""),
        )
    ]


def _get(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    offer = context.offers.get_offer_scoped(
        context.scope_constraint,
        integer(args, "id", "get_offer"),
    )
    if offer is None:
        raise ToolRecordNotFound("offer not found")
    return offer_json(offer)


def _compare(args: OfferArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    ids = args.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ToolInputError("compare_offers requires ids")
    result = []
    for raw_id in ids:
        offer = context.offers.get(int(raw_id))
        if offer is not None:
            result.append(offer_json(offer))
    return result


def _value(args: OfferArgs, key: str, current: object) -> object:
    return args.get(cast(Any, key)) if args.get(cast(Any, key)) is not None else current


def _create_data(args: OfferArgs, existing: Any) -> OfferCreate:
    status = str(_value(args, "status", existing.status))
    if status not in OFFER_STATUSES:
        raise ToolInputError("invalid offer status")
    base = int(cast(Any, _value(args, "base_monthly", existing.base_monthly)))
    months = int(cast(Any, _value(args, "months_per_year", existing.months_per_year)))
    bonus = int(cast(Any, _value(args, "signing_bonus", existing.signing_bonus)))
    if base < 0 or bonus < 0:
        raise ToolInputError("base_monthly and signing_bonus must be non-negative")
    if months < 1:
        raise ToolInputError("months_per_year must be at least 1")
    return OfferCreate(
        application_id=existing.application_id,
        company_name=str(_value(args, "company_name", existing.company_name)),
        position_name=str(_value(args, "position_name", existing.position_name)),
        status=status,
        base_monthly=base,
        months_per_year=months,
        signing_bonus=bonus,
        equity=str(_value(args, "equity", existing.equity)),
        perks=str(_value(args, "perks", existing.perks)),
        deadline=str(_value(args, "deadline", existing.deadline)),
        notes=str(_value(args, "notes", existing.notes)),
        assessment=str(_value(args, "assessment", existing.assessment)),
    )


def _create(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    application_id = integer(args, "application_id", "create_offer")
    if application_id < 1:
        raise ToolInputError("create_offer requires a positive application_id")
    application = context.applications.get_application_scoped(
        context.scope_constraint,
        application_id,
    )
    if application is None:
        raise ToolRecordNotFound("application not found")

    status = str(args.get("status") or "pending")
    if status not in OFFER_STATUSES:
        raise ToolInputError("invalid offer status")
    try:
        base = int(cast(Any, args.get("base_monthly", 0)))
        months = int(cast(Any, args.get("months_per_year", 12)))
        bonus = int(cast(Any, args.get("signing_bonus", 0)))
    except (TypeError, ValueError) as exc:
        raise ToolInputError("offer amounts must be numeric") from exc
    if base < 0 or bonus < 0:
        raise ToolInputError("base_monthly and signing_bonus must be non-negative")
    if months < 1:
        raise ToolInputError("months_per_year must be at least 1")

    offer = context.offers.create_offer_scoped(
        context.scope_constraint,
        OfferCreate(
            application_id=application_id,
            company_name=str(args.get("company_name") or application.company_name),
            position_name=str(args.get("position_name") or application.position_name),
            status=status,
            base_monthly=base,
            months_per_year=months,
            signing_bonus=bonus,
            equity=str(args.get("equity") or ""),
            perks=str(args.get("perks") or ""),
            deadline=str(args.get("deadline") or ""),
            notes=str(args.get("notes") or ""),
            assessment=str(args.get("assessment") or ""),
        ),
    )
    return offer_json(offer)


def _update(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    offer_id = integer(args, "id", "update_offer")
    existing = context.offers.get_offer_scoped(context.scope_constraint, offer_id)
    if existing is None:
        raise ToolRecordNotFound("offer not found")
    updated = context.offers.update_offer_scoped(
        context.scope_constraint,
        offer_id,
        _create_data(args, existing),
    )
    if updated is None:
        raise ToolRecordNotFound("offer not found")
    return offer_json(updated)


def _assessment(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    offer_id = integer(args, "id", "save_offer_assessment")
    updated = context.offers.save_offer_assessment_scoped(
        context.scope_constraint,
        offer_id,
        str(args.get("assessment") or ""),
    )
    if updated is None:
        raise ToolRecordNotFound("offer not found")
    return offer_json(updated)


def _offer_schema(required: list[JSONValue]) -> dict[str, JSONValue]:
    return {"type": "object", "properties": {"id": {"type": "integer"}, "company_name": {"type": "string"}, "position_name": {"type": "string"}, "status": {"type": "string", "enum": list(OFFER_STATUSES)}, "base_monthly": {"type": "integer"}, "months_per_year": {"type": "integer"}, "signing_bonus": {"type": "integer"}, "equity": {"type": "string"}, "perks": {"type": "string"}, "deadline": {"type": "string"}, "notes": {"type": "string"}, "assessment": {"type": "string"}}, "required": required}


def _create_offer_schema() -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": {
            "application_id": {
                "type": "integer",
                "description": "Existing application id to which this offer belongs.",
            },
            "company_name": {"type": "string"},
            "position_name": {"type": "string"},
            "status": {"type": "string", "enum": list(OFFER_STATUSES)},
            "base_monthly": {"type": "integer"},
            "months_per_year": {"type": "integer"},
            "signing_bonus": {"type": "integer"},
            "equity": {"type": "string"},
            "perks": {"type": "string"},
            "deadline": {"type": "string"},
            "notes": {"type": "string"},
            "assessment": {"type": "string"},
        },
        "required": ["application_id"],
    }


def _empty_confirmation_description(args: object) -> str:
    del args
    return ""


def _empty_pending_details(args: object, context: object | None = None) -> dict[str, object]:
    del args, context
    return {}


def _describe_update_offer(args: Mapping[str, Any]) -> str:
    return f"更新 Offer #{args.get('id', '')}"


def _describe_save_offer_assessment(args: Mapping[str, Any]) -> str:
    return f"保存 Offer 评估 #{args.get('id', '')}"


def _describe_create_offer(args: Mapping[str, Any]) -> str:
    target = " · ".join(
        value
        for value in (
            str(args.get("company_name") or "").strip(),
            str(args.get("position_name") or "").strip(),
        )
        if value
    )
    if not target:
        target = f"投递 #{args.get('application_id', '')}"
    base = args.get("base_monthly")
    months = args.get("months_per_year")
    salary = f"{base} × {months}" if base not in (None, "") and months not in (None, "") else ""
    return f"新建 Offer：{target}{f' · {salary}' if salary else ''}"


def _project_create_offer_success(result: object) -> str:
    if not isinstance(result, Mapping):
        return ""
    record_id = result.get("offer_id") or result.get("id")
    company = str(result.get("company_name") or "").strip()
    position = str(result.get("position_name") or "").strip()
    meta = " · ".join(value for value in (company, position) if value)
    return f"✅ 创建成功：Offer #{record_id} 已保存（{meta}）。" if record_id and meta else ""


def _pending_create_offer(
    args: Mapping[str, Any], context: object | None = None
) -> dict[str, Any]:
    application_id = args.get("application_id")
    target: dict[str, Any] = {
        "id": f"offer-draft-{application_id or 'unknown'}",
        "kind": "offer",
        "title": "新建 Offer",
        "meta": " · ".join(
            value
            for value in (
                str(args.get("company_name") or "").strip(),
                str(args.get("position_name") or "").strip(),
            )
            if value
        ),
        "source": "pending_action",
    }
    if context is not None and application_id not in (None, ""):
        try:
            tool_context = cast(Any, context)
            application = tool_context.applications.get(int(str(application_id)))
        except (AttributeError, TypeError, ValueError):
            application = None
        if application is not None:
            target["title"] = application.company_name
            target["meta"] = " · ".join(
                value
                for value in (application.position_name, "Offer")
                if value
            )
    proposed_changes = [
        {"field": field, "before": "", "after": args[field]}
        for field in (
            "company_name",
            "position_name",
            "status",
            "base_monthly",
            "months_per_year",
            "signing_bonus",
            "equity",
            "perks",
            "deadline",
            "notes",
            "assessment",
        )
        if field in args and args[field] not in (None, "")
    ]
    return {"target": target, "proposed_changes": proposed_changes, "evidence": [target]}


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


def _capture_no_undo_seed(context: object, args: object) -> None:
    del context, args
    return None


def _record_payload(record: ToolExecutionRecord[Any, Any]) -> dict[str, Any]:
    if not isinstance(record.outcome, ToolSuccess) or not isinstance(record.outcome.result, dict):
        return {}
    return cast(dict[str, Any], record.outcome.result)


def _offer_fingerprint(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "application_id",
        "company_name",
        "position_name",
        "status",
        "base_monthly",
        "months_per_year",
        "signing_bonus",
        "equity",
        "perks",
        "deadline",
        "notes",
        "assessment",
        "total_cash",
        "created_at",
        "updated_at",
    )
    result = {field: payload.get(field) for field in fields}
    result["created_at"] = _canonical_datetime(result["created_at"])
    result["updated_at"] = _canonical_datetime(result["updated_at"])
    return result


def _build_create_offer_undo(
    seed: object, record: ToolExecutionRecord[Any, Any]
) -> dict[str, Any]:
    del seed
    payload = _record_payload(record)
    offer_id = payload.get("offer_id") or payload.get("id")
    try:
        resolved_id = int(str(offer_id))
    except (TypeError, ValueError):
        return {}
    return {
        "kind": UndoPayloadKind.DELETE_OFFER.value,
        "label": "撤销新建 Offer",
        "offer_id": resolved_id,
        "expected_after": _offer_fingerprint(payload),
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


def offer_specs() -> tuple[ToolSpec[Any, Any], ...]:
    id_schema: dict[str, JSONValue] = {"type": "object", "properties": {"id": {"type": "integer", "description": "Offer id."}}, "required": ["id"]}
    offer_fields = (
        _editable("company_name", "string"), _editable("position_name", "string"),
        _editable("status", "enum", options=OFFER_STATUSES),
        _editable("base_monthly", "number", clearable=True, clear_value=0),
        _editable("months_per_year", "number"),
        _editable("signing_bonus", "number", clearable=True, clear_value=0),
        _editable("equity", "string"), _editable("perks", "long_text"),
        _editable("deadline", "datetime", clearable=True, clear_value=""),
        _editable("notes", "long_text"), _editable("assessment", "long_text"),
    )
    get_resolver = _offer_resolver("get_offer_offer_application_parent_v1")
    create_resolver = _create_resolver()
    update_resolver = _offer_resolver("update_offer_offer_application_parent_v1")
    assessment_resolver = _offer_resolver("save_offer_assessment_offer_application_parent_v1")
    create_operation = WriteOperationMetadataV1(
        undo_policy=UndoPolicy.REQUIRED,
        undo_payload_kind=UndoPayloadKind.DELETE_OFFER,
        compensation_kind=CompensationKind.UNDO_CREATE_OFFER,
        undo_contract_version="write-undo-payload-v1",
        undo_builder_id="create_offer_delete_v1",
        undo_seed_phase="none",
    )
    create_undo = UndoBuilderBinding(
        descriptor=create_operation,
        implementation_id="create_offer_delete_v1",
        capture_seed=_capture_no_undo_seed,
        build_undo=_build_create_offer_undo,
    )
    return (
        build_tool_spec(
            contract=provider_contract("list_offers", "List offers. The returned id is an offer id, not an application id; use application_id only when it is present.", {"type": "object", "properties": {"status": {"type": "string", "enum": list(OFFER_STATUSES)}}}),
            domains=(ToolDomain.OFFERS,), dependencies=(), required_capability=ToolCapability.OFFERS_READ,
            binding_contract=BindingContract("scoped_collection", "application"), resolver_bindings=(),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="list_offers_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_list, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("get_offer", "Get one offer by offer id. Offer id is not an application id.", id_schema),
            domains=(ToolDomain.OFFERS,), dependencies=("list_offers",), required_capability=ToolCapability.OFFERS_READ,
            binding_contract=BindingContract("enforce_if_bound", "application"), resolver_bindings=(get_resolver,),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="get_offer_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_get, declared_failure_categories=frozenset({"not_found"}),
            exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("compare_offers", "Compare offers by offer ids. Missing ids are skipped.", {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}),
            domains=(ToolDomain.OFFERS,), dependencies=("get_offer", "list_offers"), required_capability=ToolCapability.OFFERS_READ,
            binding_contract=BindingContract("non_application_only"), resolver_bindings=(),
            confirmation_policy="none", editable_fields=(), operation=ReadOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="compare_offers_presentation_v1",
                confirmation_description=_empty_confirmation_description,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_compare, declared_failure_categories=frozenset({"validation_error"}),
            exception_map=INPUT_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract(
                "create_offer",
                "Create an offer bound to an existing application. Company and position default to the application values.",
                _create_offer_schema(),
            ),
            domains=(ToolDomain.OFFERS,),
            dependencies=(),
            required_capability=ToolCapability.OFFERS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"),
            resolver_bindings=(create_resolver,),
            confirmation_policy="required",
            editable_fields=offer_fields,
            operation=create_operation,
            undo_builder_binding=create_undo,
            presentation=ToolPresentationBindingV1(
                implementation_id="create_offer_presentation_v1",
                confirmation_description=_describe_create_offer,
                pending_details_projector=_pending_create_offer,
                success_summary_projector=_project_create_offer_success,
            ),
            decoder=_decode,
            executor=_create,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP,
            success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("update_offer", "Update an offer. Missing fields keep existing values.", _offer_schema(["id"])),
            domains=(ToolDomain.OFFERS,), dependencies=("get_offer",), required_capability=ToolCapability.OFFERS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"), resolver_bindings=(update_resolver,),
            confirmation_policy="required", editable_fields=offer_fields, operation=WriteOperationMetadataV1(),
            undo_builder_binding=None, presentation=ToolPresentationBindingV1(
                implementation_id="update_offer_presentation_v1",
                confirmation_description=_describe_update_offer,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_update,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
        build_tool_spec(
            contract=provider_contract("save_offer_assessment", "Save or replace the assessment text for an offer.", {"type": "object", "properties": {"id": {"type": "integer"}, "assessment": {"type": "string"}}, "required": ["id", "assessment"]}),
            domains=(ToolDomain.OFFERS,), dependencies=("get_offer",), required_capability=ToolCapability.OFFERS_WRITE,
            binding_contract=BindingContract("enforce_if_bound", "application"), resolver_bindings=(assessment_resolver,),
            confirmation_policy="required", editable_fields=(_editable("assessment", "long_text"),),
            operation=WriteOperationMetadataV1(), undo_builder_binding=None,
            presentation=ToolPresentationBindingV1(
                implementation_id="save_offer_assessment_presentation_v1",
                confirmation_description=_describe_save_offer_assessment,
                pending_details_projector=_empty_pending_details,
                success_summary_projector=compact_json,
            ), decoder=_decode, executor=_assessment,
            declared_failure_categories=frozenset({"validation_error", "not_found"}),
            exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json,
        ),
    )
