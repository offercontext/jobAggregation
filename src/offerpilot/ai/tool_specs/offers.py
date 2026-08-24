from __future__ import annotations

from collections.abc import Mapping
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


_OFFER_PARENT_RESOLVER = BindingResolverSpec(
    resolver_id="offer_application_parent",
    entity_kind="application",
    arg_path="id",
    presence="required",
    identity_type="positive_int64",
    resolve=_offer_binding,
)


def _list(args: OfferArgs, context: ToolExecutionContext) -> list[dict[str, Any]]:
    return [offer_json(offer) for offer in context.offers.list(status=str(args.get("status") or ""))]


def _get(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    offer = context.offers.get(integer(args, "id", "get_offer"))
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
    existing = context.offers.get(offer_id)
    if existing is None:
        raise ToolRecordNotFound("offer not found")
    updated = context.offers.update(offer_id, _create_data(args, existing))
    if updated is None:
        raise ToolRecordNotFound("offer not found")
    return offer_json(updated)


def _assessment(args: OfferArgs, context: ToolExecutionContext) -> dict[str, Any]:
    offer_id = integer(args, "id", "save_offer_assessment")
    existing = context.offers.get(offer_id)
    if existing is None:
        raise ToolRecordNotFound("offer not found")
    copied = dict(args)
    copied["assessment"] = str(args.get("assessment") or "")
    updated = context.offers.update(offer_id, _create_data(cast(OfferArgs, copied), existing))
    if updated is None:
        raise ToolRecordNotFound("offer not found")
    return offer_json(updated)


def _offer_schema(required: list[JSONValue]) -> dict[str, JSONValue]:
    return {"type": "object", "properties": {"id": {"type": "integer"}, "company_name": {"type": "string"}, "position_name": {"type": "string"}, "status": {"type": "string", "enum": list(OFFER_STATUSES)}, "base_monthly": {"type": "integer"}, "months_per_year": {"type": "integer"}, "signing_bonus": {"type": "integer"}, "equity": {"type": "string"}, "perks": {"type": "string"}, "deadline": {"type": "string"}, "notes": {"type": "string"}, "assessment": {"type": "string"}}, "required": required}


def offer_specs() -> tuple[ToolSpec[Any, Any], ...]:
    read = frozenset({ToolCapability.OFFERS_READ})
    write = frozenset({ToolCapability.OFFERS_WRITE})
    id_schema: dict[str, JSONValue] = {"type": "object", "properties": {"id": {"type": "integer", "description": "Offer id."}}, "required": ["id"]}
    return (
        ToolSpec(contract=provider_contract("list_offers", "List offers. The returned id is an offer id, not an application id; use application_id only when it is present.", {"type": "object", "properties": {"status": {"type": "string", "enum": list(OFFER_STATUSES)}}}), kind="read", decoder=_decode, executor=_list, required_capabilities=read, binding_contract=BindingContract("scoped_collection", "application"), success_renderer=compact_json),
        ToolSpec(contract=provider_contract("get_offer", "Get one offer by offer id. Offer id is not an application id.", id_schema), kind="read", decoder=_decode, executor=_get, required_capabilities=read, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_OFFER_PARENT_RESOLVER,), declared_failure_categories=frozenset({"not_found"}), exception_map=NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("compare_offers", "Compare offers by offer ids. Missing ids are skipped.", {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}), kind="read", decoder=_decode, executor=_compare, required_capabilities=read, binding_contract=BindingContract("non_application_only"), declared_failure_categories=frozenset({"validation_error"}), exception_map=INPUT_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("update_offer", "Update an offer. Missing fields keep existing values.", _offer_schema(["id"])), kind="write", decoder=_decode, executor=_update, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_OFFER_PARENT_RESOLVER,), confirmation_policy="required", declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
        ToolSpec(contract=provider_contract("save_offer_assessment", "Save or replace the assessment text for an offer.", {"type": "object", "properties": {"id": {"type": "integer"}, "assessment": {"type": "string"}}, "required": ["id", "assessment"]}), kind="write", decoder=_decode, executor=_assessment, required_capabilities=write, binding_contract=BindingContract("enforce_if_bound", "application"), binding_resolvers=(_OFFER_PARENT_RESOLVER,), confirmation_policy="required", declared_failure_categories=frozenset({"validation_error", "not_found"}), exception_map=INPUT_EXCEPTION_MAP + NOT_FOUND_EXCEPTION_MAP, success_renderer=compact_json),
    )
