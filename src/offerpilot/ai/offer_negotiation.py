from __future__ import annotations

import json
import hashlib
import math
import re
from collections.abc import Callable
from time import perf_counter
from typing import Any

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.offer_negotiation_templates import (
    TEMPLATE_IDS,
    TemplateContractError,
    provider_evidence_catalog,
    provider_template_catalog,
    render_template,
)
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply

OFFER_NEGOTIATION_FIELDS = (
    "proposal_status",
    "communication_goals",
    "clarification_questions",
    "talking_points",
    "preparation_checks",
)
_ARRAY_FIELDS = OFFER_NEGOTIATION_FIELDS[1:]
_ITEM_FIELDS = {"id", "template_id", "evidence_ref_ids"}
_SHAPE_CATEGORIES = {
    "invalid_json",
    "duplicate_json_key",
    "unexpected_field",
    "invalid_item_shape",
    "invalid_evidence_shape",
    "missing_field",
    "invalid_field_type",
    "missing_evidence_ref",
}
OfferNegotiationDiagnosticSink = Callable[[dict[str, Any]], None]
_ID_RE = re.compile(r"^[\x21-\x7e]{1,64}$")

OFFER_NEGOTIATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(OFFER_NEGOTIATION_FIELDS),
    "properties": {
        "proposal_status": {"enum": ["normal", "safe_empty"]},
        **{
            field: {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "template_id", "evidence_ref_ids"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "template_id": {"enum": list(TEMPLATE_IDS)},
                        "evidence_ref_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                        },
                    },
                },
            }
            for field in _ARRAY_FIELDS
        },
    },
}

OFFER_NEGOTIATION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "offer_negotiation_proposal",
        "strict": True,
        "schema": OFFER_NEGOTIATION_JSON_SCHEMA,
    },
}


class OfferNegotiationModelError(ValueError):
    def __init__(self, message: str, validation_category: str = "invalid_json") -> None:
        super().__init__(message)
        self.validation_category = validation_category
        self.provider_request_id = ""
        self.repair_count = 0
        self.elapsed_ms = 0
        self.http_status: int | None = None
        self.timeout = False


def _redact_provider_request_id(value: object) -> str:
    request_id = str(value or "")
    if not request_id:
        return ""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]
    return f"request-redacted-{digest}"


def safe_empty_offer_negotiation_proposal() -> dict[str, Any]:
    return {
        "proposal_status": "safe_empty",
        "communication_goals": [],
        "clarification_questions": [],
        "talking_points": [],
        "preparation_checks": [],
    }


def build_offer_negotiation_snapshot(
    *,
    offer: dict[str, Any],
    dimensions: list[dict[str, Any]],
    user_brief: dict[str, str],
    idempotency_key: str,
) -> dict[str, Any]:
    del idempotency_key
    for field in ("goal", "concerns", "scenario"):
        value = user_brief.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must not be blank")
    fields = (
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
    )
    offer_snapshot = {field: offer.get(field) for field in fields}
    sorted_dimensions = sorted(dimensions, key=lambda item: int(item["id"]))
    canonical_dimensions = [
        {
            "path_id": f"dimension_{index:03d}",
            "label": str(item["label"]),
            "value_text": item.get("value_text"),
        }
        for index, item in enumerate(sorted_dimensions, start=1)
    ]
    return {
        "snapshot_version": 1,
        "offer_snapshot": {**offer_snapshot, "dimensions": canonical_dimensions},
        "user_brief": {
            "goal": user_brief.get("goal", ""),
            "concerns": user_brief.get("concerns", ""),
            "scenario": user_brief.get("scenario", ""),
        },
    }


def validate_offer_negotiation(payload: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate the constrained Provider contract and render public text server-side."""
    _reject_non_finite(payload)
    if not isinstance(payload, dict):
        raise OfferNegotiationModelError("proposal must be an object", "invalid_item_shape")
    if set(payload) != set(OFFER_NEGOTIATION_FIELDS):
        raise OfferNegotiationModelError("proposal fields are invalid", "unexpected_field")
    status = payload.get("proposal_status")
    if status not in {"normal", "safe_empty"}:
        raise OfferNegotiationModelError("proposal_status is invalid", "invalid_field_type")
    if status == "safe_empty":
        if any(payload[field] != [] for field in _ARRAY_FIELDS):
            raise OfferNegotiationModelError(
                "safe_empty must have empty arrays", "invalid_item_shape"
            )
        return safe_empty_offer_negotiation_proposal()
    seen_ids: set[str] = set()
    seen_templates: set[tuple[str, str, tuple[str, ...]]] = set()
    normalized: dict[str, Any] = {"proposal_status": "normal"}
    for field in _ARRAY_FIELDS:
        items = payload.get(field)
        if not isinstance(items, list):
            raise OfferNegotiationModelError(f"{field} must be an array", "invalid_field_type")
        if len(items) > 3:
            raise OfferNegotiationModelError(f"{field} exceeds the limit", "limit_exceeded")
        normalized[field] = []
        for item in items:
            checked = _validate_item(item, snapshot, field)
            if checked["id"] in seen_ids:
                raise OfferNegotiationModelError("item ids must be unique", "duplicate_item_id")
            seen_ids.add(checked["id"])
            raw_template_id = item.get("template_id")
            raw_evidence_ids = item.get("evidence_ref_ids")
            template_key = (
                field,
                str(raw_template_id),
                tuple(raw_evidence_ids) if isinstance(raw_evidence_ids, list) else (),
            )
            if template_key in seen_templates:
                raise OfferNegotiationModelError(
                    "template selections must be unique",
                    "duplicate_template",
                )
            seen_templates.add(template_key)
            normalized[field].append(checked)
    if all(not normalized[field] for field in _ARRAY_FIELDS):
        return safe_empty_offer_negotiation_proposal()
    return normalized


def generate_offer_negotiation_proposal(
    model: ChatModel,
    snapshot: dict[str, Any],
    *,
    on_diagnostic: OfferNegotiationDiagnosticSink | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    last_category = "invalid_json"
    provider_request_id = ""
    for attempt in range(2):
        prompt = _generation_prompt(snapshot) if attempt == 0 else _repair_prompt(last_category)
        response_format = (
            OFFER_NEGOTIATION_RESPONSE_FORMAT
            if getattr(model, "supports_json_schema", False) is True
            else None
        )
        try:
            assistant = model.complete(
                [
                    Message(role="system", content=_system_prompt(snapshot)),
                    Message(role="user", content=prompt),
                ],
                [],
                response_format=response_format,
            )
        except Exception as exc:
            error = OfferNegotiationModelError("provider request failed", "provider_error")
            diagnostic = getattr(exc, "diagnostic", None)
            diagnostic_map = diagnostic if isinstance(diagnostic, dict) else {}
            error.provider_request_id = _redact_provider_request_id(
                diagnostic_map.get("provider_request_id", getattr(exc, "provider_request_id", ""))
            )
            status = diagnostic_map.get(
                "http_status",
                diagnostic_map.get(
                    "status_code", getattr(exc, "status_code", getattr(exc, "http_status", None))
                ),
            )
            try:
                error.http_status = int(status) if status is not None else None
            except (TypeError, ValueError):
                error.http_status = None
            error.timeout = (
                bool(diagnostic_map.get("timeout", False))
                or isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.lower()
            )
            error.repair_count = attempt
            error.elapsed_ms = int((perf_counter() - started) * 1000)
            _emit_diagnostic(
                on_diagnostic,
                failure_category="provider_error",
                repair_attempted=attempt > 0,
                repair_count=attempt,
                elapsed_ms=error.elapsed_ms,
                provider_request_id=error.provider_request_id,
                http_status=error.http_status,
                timeout=error.timeout,
            )
            raise error from exc
        provider_request_id = _redact_provider_request_id(
            getattr(assistant, "provider_blocks", {}).get("request_id")
        )
        try:
            parsed = parse_json_reply(
                assistant.content,
                allow_fenced=False,
                reject_non_finite=True,
                reject_duplicate_keys=True,
            )
            return validate_offer_negotiation(parsed, snapshot)
        except OfferNegotiationModelError as exc:
            last_category = exc.validation_category
        except (TypeError, ValueError, RuntimeError) as exc:
            last_category = (
                "duplicate_json_key" if "duplicate" in str(exc).lower() else "invalid_json"
            )
        if last_category not in _SHAPE_CATEGORIES:
            error = OfferNegotiationModelError("proposal is not verifiable", last_category)
            error.provider_request_id = provider_request_id
            error.repair_count = attempt
            error.elapsed_ms = int((perf_counter() - started) * 1000)
            _emit_diagnostic(
                on_diagnostic,
                failure_category=last_category,
                repair_attempted=attempt > 0,
                repair_count=attempt,
                elapsed_ms=error.elapsed_ms,
                provider_request_id=provider_request_id,
            )
            raise error
    _emit_diagnostic(
        on_diagnostic,
        failure_category=last_category,
        repair_attempted=True,
        repair_count=1,
        elapsed_ms=int((perf_counter() - started) * 1000),
        provider_request_id=provider_request_id,
    )
    return safe_empty_offer_negotiation_proposal()


def _emit_diagnostic(
    sink: OfferNegotiationDiagnosticSink | None,
    *,
    failure_category: str,
    repair_attempted: bool,
    repair_count: int,
    elapsed_ms: int,
    provider_request_id: str,
    http_status: int | None = None,
    timeout: bool = False,
) -> None:
    if sink is None:
        return
    sink(
        {
            "failure_category": failure_category,
            "failure_categories": [failure_category],
            "repair_attempted": repair_attempted,
            "repair_count": repair_count,
            "elapsed_ms": max(0, elapsed_ms),
            "provider_request_id": provider_request_id,
            "http_status": http_status,
            "timeout": timeout,
        }
    )


def _validate_item(item: Any, snapshot: dict[str, Any], field: str) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _ITEM_FIELDS:
        raise OfferNegotiationModelError("item fields are invalid", "invalid_item_shape")
    item_id = item.get("id")
    template_id = item.get("template_id")
    evidence_ref_ids = item.get("evidence_ref_ids")
    if not isinstance(item_id, str) or not item_id:
        raise OfferNegotiationModelError("item id is invalid", "invalid_item_shape")
    if len(item_id) > 64:
        raise OfferNegotiationModelError("item id exceeds the limit", "limit_exceeded")
    if not _ID_RE.fullmatch(item_id):
        raise OfferNegotiationModelError("item id is invalid", "invalid_item_shape")
    if not isinstance(template_id, str) or template_id not in TEMPLATE_IDS:
        raise OfferNegotiationModelError("template_id is invalid", "invalid_field_type")
    if not isinstance(evidence_ref_ids, list) or not evidence_ref_ids:
        raise OfferNegotiationModelError(
            "evidence_ref_ids is invalid",
            "missing_evidence_ref",
        )
    if len(evidence_ref_ids) > 4:
        raise OfferNegotiationModelError(
            "evidence_ref_ids exceeds the limit",
            "limit_exceeded",
        )
    if any(not isinstance(value, str) or not value for value in evidence_ref_ids):
        raise OfferNegotiationModelError(
            "evidence_ref_ids contains an invalid value",
            "invalid_evidence_shape",
        )
    if len(set(evidence_ref_ids)) != len(evidence_ref_ids):
        raise OfferNegotiationModelError(
            "evidence_ref_ids must be unique",
            "invalid_evidence_shape",
        )
    try:
        text, rationale, checked_refs = render_template(
            section=field,
            template_id=template_id,
            evidence_ref_ids=evidence_ref_ids,
            snapshot=snapshot,
        )
    except TemplateContractError as exc:
        raise OfferNegotiationModelError(str(exc), exc.category) from exc
    return {
        "id": item_id,
        "text": text,
        "rationale": rationale,
        "evidence_refs": checked_refs,
    }


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise OfferNegotiationModelError("non-finite value", "invalid_field_type")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _system_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "只输出严格 JSON，不要 Markdown。Provider 只能选择服务端提供的封闭 template_id 和 evidence_ref_ids，"
        "不能输出自由文本或 intent。"
        "communication_goals、clarification_questions、talking_points、preparation_checks "
        "分别表示沟通目标、待澄清问题、可直接参考的表达和沟通前检查。每组最多选择三条，"
        "同一个 template_id 与 evidence_ref_ids 组合不得重复。每条记录必须包含 id、template_id、evidence_ref_ids；"
        "template_id、所属分组和 evidence_ref_ids 必须逐字匹配 template_catalog 中的一项。"
        "服务端会生成最终中文 text/rationale，模型不得返回 text、rationale、topic、决定、排名、优劣、"
        "市场薪酬、法律结论、公司政策或录用概率。没有可验证建议时输出 proposal_status=safe_empty 和四个空数组。"
        + json.dumps(OFFER_NEGOTIATION_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        + "\nevidence_catalog："
        + json.dumps(provider_evidence_catalog(snapshot), ensure_ascii=False, separators=(",", ":"))
        + "\ntemplate_catalog："
        + json.dumps(provider_template_catalog(snapshot), ensure_ascii=False, separators=(",", ":"))
    )


def _generation_prompt(snapshot: dict[str, Any]) -> str:
    del snapshot
    return (
        "从 system 消息中的 template_catalog 选择一组精简、互不重复且可执行的 Offer 谈薪准备内容。"
    )


def _repair_prompt(category: str) -> str:
    return (
        "上次输出未通过机器校验。只修复失败类别："
        + category
        + "。请重新输出完整严格 JSON；不要输出解释、原始模型内容或输入快照。"
        + "只能返回受限 template_id/evidence_ref_ids，不得返回 intent、topic、evidence_refs、text 或 rationale。"
        + json.dumps(OFFER_NEGOTIATION_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    )
