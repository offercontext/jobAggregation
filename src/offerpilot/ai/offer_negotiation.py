from __future__ import annotations

import json
import hashlib
import math
import re
from collections.abc import Callable
from time import perf_counter
from typing import Any

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply
from offerpilot.repositories.json_contract import canonical_json

OFFER_NEGOTIATION_FIELDS = (
    "proposal_status",
    "communication_goals",
    "clarification_questions",
    "talking_points",
    "preparation_checks",
)
_ARRAY_FIELDS = OFFER_NEGOTIATION_FIELDS[1:]
_ITEM_FIELDS = {"id", "topic", "evidence_refs"}
_REF_FIELDS = {"source", "path", "excerpt"}
_ALLOWED_SOURCES = {"offer_snapshot", "user_brief"}
_ALLOWED_TOPICS = {
    "offer_fact",
    "user_goal",
    "user_concern",
    "user_scenario",
    "comparison_dimension",
}
_TOPIC_LABELS = {
    "offer_fact": "Offer 固定事实",
    "user_goal": "本次谈薪目标",
    "user_concern": "本次谈薪顾虑",
    "user_scenario": "本次沟通场景",
    "comparison_dimension": "自定义比较维度",
}
_FIXED_OFFER_EVIDENCE_PATHS = {
    "/offer_snapshot/company_name",
    "/offer_snapshot/position_name",
    "/offer_snapshot/status",
    "/offer_snapshot/base_monthly",
    "/offer_snapshot/months_per_year",
    "/offer_snapshot/signing_bonus",
    "/offer_snapshot/equity",
    "/offer_snapshot/perks",
    "/offer_snapshot/deadline",
    "/offer_snapshot/notes",
}
_TOPIC_ANCHORS = {
    "offer_fact": _FIXED_OFFER_EVIDENCE_PATHS,
    "user_goal": {"/user_brief/goal"},
    "user_concern": {"/user_brief/concerns"},
    "user_scenario": {"/user_brief/scenario"},
}
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
_DIMENSION_VALUE_PATH_RE = re.compile(
    r"^/offer_snapshot/dimensions/dimension_[0-9]{3}/value_text$"
)

OFFER_NEGOTIATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(OFFER_NEGOTIATION_FIELDS),
    "properties": {
        "proposal_status": {"enum": ["normal", "safe_empty"]},
        **{
            field: {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "topic", "evidence_refs"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "topic": {"enum": sorted(_ALLOWED_TOPICS)},
                        "evidence_refs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["source", "path", "excerpt"],
                                "properties": {
                                    "source": {"enum": sorted(_ALLOWED_SOURCES)},
                                    "path": {"type": "string", "minLength": 1},
                                    "excerpt": {"type": "string", "minLength": 1, "maxLength": 400},
                                },
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
        "company_name", "position_name", "status", "base_monthly", "months_per_year",
        "signing_bonus", "equity", "perks", "deadline", "notes",
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
            raise OfferNegotiationModelError("safe_empty must have empty arrays", "invalid_item_shape")
        return safe_empty_offer_negotiation_proposal()
    seen_ids: set[str] = set()
    normalized: dict[str, Any] = {"proposal_status": "normal"}
    for field in _ARRAY_FIELDS:
        items = payload.get(field)
        if not isinstance(items, list):
            raise OfferNegotiationModelError(f"{field} must be an array", "invalid_field_type")
        if len(items) > 8:
            raise OfferNegotiationModelError(f"{field} exceeds the limit", "limit_exceeded")
        normalized[field] = []
        for item in items:
            checked = _validate_item(item, snapshot, field)
            if checked["id"] in seen_ids:
                raise OfferNegotiationModelError("item ids must be unique", "duplicate_item_id")
            seen_ids.add(checked["id"])
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
        response_format = OFFER_NEGOTIATION_RESPONSE_FORMAT if getattr(model, "supports_json_schema", False) is True else None
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
                diagnostic_map.get("status_code", getattr(exc, "status_code", getattr(exc, "http_status", None))),
            )
            try:
                error.http_status = int(status) if status is not None else None
            except (TypeError, ValueError):
                error.http_status = None
            error.timeout = bool(diagnostic_map.get("timeout", False)) or isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
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
            last_category = "duplicate_json_key" if "duplicate" in str(exc).lower() else "invalid_json"
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
    topic = item.get("topic")
    refs = item.get("evidence_refs")
    if not isinstance(item_id, str) or not item_id:
        raise OfferNegotiationModelError("item id is invalid", "invalid_item_shape")
    if len(item_id) > 64:
        raise OfferNegotiationModelError("item id exceeds the limit", "limit_exceeded")
    if not _ID_RE.fullmatch(item_id):
        raise OfferNegotiationModelError("item id is invalid", "invalid_item_shape")
    if not isinstance(topic, str) or topic not in _ALLOWED_TOPICS:
        raise OfferNegotiationModelError("topic is invalid", "invalid_field_type")
    if not isinstance(refs, list) or not refs:
        raise OfferNegotiationModelError("evidence_refs is invalid", "missing_evidence_ref")
    if len(refs) > 4:
        raise OfferNegotiationModelError("evidence_refs exceeds the limit", "limit_exceeded")
    checked_refs = [_validate_ref(ref, snapshot) for ref in refs]
    _validate_topic_anchor(topic, checked_refs)
    rendered = _render_item(item_id, field, topic, checked_refs)
    return {
        "id": item_id,
        "text": rendered[0],
        "rationale": rendered[1],
        "evidence_refs": checked_refs,
    }


def _render_item(
    item_id: str,
    field: str,
    topic: str,
    evidence_refs: list[dict[str, str]],
) -> tuple[str, str]:
    del item_id, evidence_refs
    label = _TOPIC_LABELS[topic]
    text_templates = {
        "communication_goals": f"可以围绕{label}准备沟通请求。",
        "clarification_questions": f"可向对方确认与{label}相关的问题。",
        "talking_points": f"准备围绕{label}表达你的诉求。",
        "preparation_checks": f"请在沟通前确认{label}相关信息。",
    }
    text = text_templates[field]
    rationale = "该建议由系统依据已提供的冻结来源生成，最终沟通内容由你决定。"
    return text, rationale


def _validate_topic_anchor(topic: str, evidence_refs: list[dict[str, str]]) -> None:
    paths = {ref["path"] for ref in evidence_refs}
    if topic == "comparison_dimension":
        matched = any(_DIMENSION_VALUE_PATH_RE.fullmatch(path) for path in paths)
    else:
        matched = bool(paths & _TOPIC_ANCHORS[topic])
    if not matched:
        raise OfferNegotiationModelError(
            "topic evidence anchor is missing",
            "topic_evidence_mismatch",
        )


def _validate_ref(ref: Any, snapshot: dict[str, Any]) -> dict[str, str]:
    if not isinstance(ref, dict) or set(ref) != _REF_FIELDS:
        raise OfferNegotiationModelError("evidence reference shape is invalid", "invalid_evidence_shape")
    source, path, excerpt = ref.get("source"), ref.get("path"), ref.get("excerpt")
    if not isinstance(source, str) or not isinstance(path, str) or not isinstance(excerpt, str):
        raise OfferNegotiationModelError("evidence reference field type is invalid", "invalid_evidence_shape")
    if source not in _ALLOWED_SOURCES:
        raise OfferNegotiationModelError("evidence reference is unknown", "unknown_evidence_ref")
    if excerpt == "":
        raise OfferNegotiationModelError("evidence excerpt shape is invalid", "invalid_evidence_shape")
    if len(excerpt) > 400:
        raise OfferNegotiationModelError("evidence excerpt exceeds the limit", "limit_exceeded")
    if not excerpt.strip():
        raise OfferNegotiationModelError("evidence excerpt is invalid", "excerpt_mismatch")
    if source == "offer_snapshot":
        value = _resolve_snapshot_path(snapshot, source, path)
        if not isinstance(value, (str, int)) or value is None:
            raise OfferNegotiationModelError("offer evidence path is invalid", "unknown_evidence_ref")
        expected = str(value)
        if isinstance(value, int) and excerpt != expected:
            raise OfferNegotiationModelError("numeric evidence must be exact", "excerpt_mismatch")
        if isinstance(value, str) and excerpt not in value:
            raise OfferNegotiationModelError("evidence excerpt does not match", "excerpt_mismatch")
    else:
        value = _resolve_snapshot_path(snapshot, source, path)
        if not isinstance(value, str) or excerpt not in value:
            raise OfferNegotiationModelError("user brief excerpt does not match", "excerpt_mismatch")
    return {"source": source, "path": path, "excerpt": excerpt}


def _resolve_snapshot_path(snapshot: dict[str, Any], source: str, path: str) -> Any:
    if source == "offer_snapshot":
        fixed_fields = {
            "company_name", "position_name", "status", "base_monthly", "months_per_year",
            "signing_bonus", "equity", "perks", "deadline", "notes",
        }
        prefix = "/offer_snapshot/"
        if path.startswith(prefix):
            field = path[len(prefix):]
            if field in fixed_fields:
                value = snapshot.get("offer_snapshot", {}).get(field)
                if value is None or isinstance(value, (dict, list, bool)):
                    raise OfferNegotiationModelError("offer evidence path is invalid", "unknown_evidence_ref")
                return value
        dimension_match = re.fullmatch(r"/offer_snapshot/dimensions/(dimension_[0-9]{3})/value_text", path)
        if dimension_match:
            path_id = dimension_match.group(1)
            dimensions = snapshot.get("offer_snapshot", {}).get("dimensions", [])
            dimension = next((item for item in dimensions if item.get("path_id") == path_id), None)
            if dimension is None or not isinstance(dimension.get("value_text"), str) or not dimension["value_text"]:
                raise OfferNegotiationModelError("missing dimension value has no evidence", "unknown_evidence_ref")
            return dimension["value_text"]
    elif source == "user_brief":
        if path in {"/user_brief/goal", "/user_brief/concerns", "/user_brief/scenario"}:
            value = snapshot.get("user_brief", {}).get(path.rsplit("/", 1)[-1])
            if isinstance(value, str) and value:
                return value
    raise OfferNegotiationModelError("evidence path is unknown", "unknown_evidence_ref")


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise OfferNegotiationModelError("non-finite value", "invalid_field_type")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _evidence_catalog(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    offer_snapshot = snapshot.get("offer_snapshot", {})
    if isinstance(offer_snapshot, dict):
        for field, value in offer_snapshot.items():
            if (
                isinstance(value, (str, int))
                and not isinstance(value, bool)
                and (not isinstance(value, str) or value.strip())
            ):
                catalog.append(
                    {
                        "source": "offer_snapshot",
                        "path": f"/offer_snapshot/{field}",
                        "excerpt": str(value),
                    }
                )
    dimensions = offer_snapshot.get("dimensions", [])
    if isinstance(dimensions, list):
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            value = dimension.get("value_text")
            path_id = dimension.get("path_id")
            if isinstance(path_id, str) and isinstance(value, str) and value.strip():
                catalog.append(
                    {
                        "source": "offer_snapshot",
                        "path": f"/offer_snapshot/dimensions/{path_id}/value_text",
                        "excerpt": value,
                    }
                )
    user_brief = snapshot.get("user_brief", {})
    if isinstance(user_brief, dict):
        for field, value in user_brief.items():
            if isinstance(value, str) and value:
                catalog.append(
                    {
                        "source": "user_brief",
                        "path": f"/user_brief/{field}",
                        "excerpt": value,
                    }
                )
    return sorted(catalog, key=lambda item: (item["source"], item["path"]))


def _provider_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project the persisted snapshot to the minimum Provider input."""
    offer_snapshot = snapshot.get("offer_snapshot")
    if not isinstance(offer_snapshot, dict):
        return snapshot
    projected_offer = dict(offer_snapshot)
    projected_dimensions: list[dict[str, str]] = []
    for dimension in offer_snapshot.get("dimensions", []):
        if not isinstance(dimension, dict):
            continue
        path_id = dimension.get("path_id")
        value_text = dimension.get("value_text")
        if isinstance(path_id, str) and isinstance(value_text, str) and value_text.strip():
            projected_dimensions.append({"path_id": path_id, "value_text": value_text})
    projected_offer["dimensions"] = projected_dimensions
    return {**snapshot, "offer_snapshot": projected_offer}


def _system_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "只输出严格 JSON，不要 Markdown。Provider 只能返回受限的主题枚举，不能输出自由文本或 intent。"
        "topic 只能是 offer_fact、user_goal、user_concern、user_scenario、comparison_dimension。"
        "communication_goals、clarification_questions、talking_points、preparation_checks "
        "分别表示沟通请求、待澄清问题、表达要点和沟通前检查。每条记录必须包含 id、topic、evidence_refs；"
        "topic 必须至少有一条匹配的证据锚点：user_goal=/user_brief/goal，"
        "user_concern=/user_brief/concerns，user_scenario=/user_brief/scenario，"
        "comparison_dimension=/offer_snapshot/dimensions/dimension_NNN/value_text，"
        "offer_fact 只能引用固定 Offer 字段路径。每个 evidence_refs 必须来自输入目录，"
        "excerpt 必须逐字连续匹配。系统会根据数组字段/topic 生成中文 text/rationale，模型不得自行写入决定、排名、"
        "优劣、市场薪酬、法律结论、公司政策或录用概率。没有可验证建议时输出 proposal_status=safe_empty 和四个空数组。"
        + json.dumps(OFFER_NEGOTIATION_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        + "\n只能从以下 evidence_catalog 逐条选择 source/path/excerpt；不得创造目录外引用："
        + json.dumps(_evidence_catalog(snapshot), ensure_ascii=False, separators=(",", ":"))
    )


def _generation_prompt(snapshot: dict[str, Any]) -> str:
    return "基于以下冻结输入生成 Offer 谈薪准备建议，只能引用输入中的路径和原文：" + canonical_json(_provider_snapshot(snapshot))


def _repair_prompt(category: str) -> str:
    return (
        "上次输出未通过机器校验。只修复失败类别：" + category
        + "。请重新输出完整严格 JSON；不要输出解释、原始模型内容或输入快照。"
        + "只能返回受限 topic/evidence_refs，不得返回 intent、text 或 rationale。"
        + json.dumps(OFFER_NEGOTIATION_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    )
