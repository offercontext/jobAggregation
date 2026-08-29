from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.catalog import build_tool_spec
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    JSONValue,
    ToolSpec,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolPresentationBindingV1,
    WriteOperationMetadataV1,
)
from offerpilot.ai.tool_runtime.policy_types import ToolCapability, ToolDomain
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
)
from offerpilot.repositories.offers import OfferCreate


OFFER_STATUSES = ("pending", "negotiating", "accepted", "declined", "expired")


class OfferArgs(TypedDict, total=False):
    id: int
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
    update_resolver = _offer_resolver("update_offer_offer_application_parent_v1")
    assessment_resolver = _offer_resolver("save_offer_assessment_offer_application_parent_v1")
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
