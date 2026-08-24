from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from typing import Any, cast

from offerpilot.application_status import APPLICATION_STATUS_IDS
from offerpilot.ai.tool_runtime.catalog import ToolCatalog, authority_manifest_for_specs
from offerpilot.ai.tool_runtime.contracts import JSONValue, ToolSpec, UndoPolicy, WriteContract
from offerpilot.ai.tool_specs.application_events import application_event_specs
from offerpilot.ai.tool_specs.applications import application_specs
from offerpilot.ai.tool_specs.jd_analyses import jd_analysis_specs
from offerpilot.ai.tool_specs.notes import note_specs
from offerpilot.ai.tool_specs.offers import offer_specs
from offerpilot.ai.tool_specs.resumes import resume_specs


MODEL_TOOL_NAMES = (
    "list_applications",
    "get_application",
    "create_application",
    "update_application_status",
    "list_application_events",
    "get_application_event",
    "create_application_event",
    "update_application_event",
    "delete_application_event",
    "list_notes",
    "add_note",
    "update_note",
    "delete_note",
    "list_offers",
    "get_offer",
    "compare_offers",
    "update_offer",
    "save_offer_assessment",
    "list_resumes",
    "get_resume",
    "resume_update_career_intent",
    "resume_rewrite_highlight",
    "list_resume_matches",
    "list_jd_analyses",
    "get_jd_analysis",
)

_EVENT_TYPES = ("written_test", "interview", "offer_step", "deadline", "custom")
_OFFER_STATUSES = ("pending", "negotiating", "accepted", "declined", "expired")
_EDITABLE_FIELDS: dict[str, tuple[dict[str, JSONValue], ...]] = {
    "create_application": (
        {"field": "company_name", "type": "string"}, {"field": "position_name", "type": "string"},
        {"field": "job_url", "type": "string"}, {"field": "status", "type": "enum", "options": list(APPLICATION_STATUS_IDS)},
        {"field": "closed_reason", "type": "long_text"},
    ),
    "update_application_status": (
        {"field": "status", "type": "enum", "options": list(APPLICATION_STATUS_IDS)},
        {"field": "closed_reason", "type": "long_text"},
    ),
    "create_application_event": (), "update_application_event": (), "delete_application_event": (),
    "add_note": (), "update_note": (), "delete_note": (), "update_offer": (),
    "save_offer_assessment": ({"field": "assessment", "type": "long_text"},),
    "resume_update_career_intent": (),
    "resume_rewrite_highlight": ({"field": "text", "type": "long_text"},),
}
_EVENT_FIELDS: tuple[dict[str, JSONValue], ...] = (
    {"field": "event_type", "type": "enum", "options": list(_EVENT_TYPES)}, {"field": "subtype", "type": "string"},
    {"field": "scheduled_at", "type": "datetime"}, {"field": "remind_at", "type": "datetime", "clearable": True, "clear_value": ""},
    {"field": "duration_minutes", "type": "number"}, {"field": "round", "type": "number", "clearable": True, "clear_value": 0},
    {"field": "location", "type": "string"}, {"field": "notes", "type": "long_text"}, {"field": "status", "type": "string"},
)
_NOTE_FIELDS: tuple[dict[str, JSONValue], ...] = (
    {"field": "company", "type": "string"}, {"field": "position", "type": "string"}, {"field": "round", "type": "string"},
    {"field": "date", "type": "datetime"}, {"field": "allow_placeholder_date", "type": "boolean"},
    {"field": "questions", "type": "long_text"}, {"field": "self_reflection", "type": "long_text"},
    {"field": "difficulty_points", "type": "long_text"}, {"field": "mood", "type": "long_text"},
)
_OFFER_FIELDS: tuple[dict[str, JSONValue], ...] = (
    {"field": "company_name", "type": "string"}, {"field": "position_name", "type": "string"},
    {"field": "status", "type": "enum", "options": list(_OFFER_STATUSES)},
    {"field": "base_monthly", "type": "number", "clearable": True, "clear_value": 0},
    {"field": "months_per_year", "type": "number"},
    {"field": "signing_bonus", "type": "number", "clearable": True, "clear_value": 0},
    {"field": "equity", "type": "string"}, {"field": "perks", "type": "long_text"},
    {"field": "deadline", "type": "datetime", "clearable": True, "clear_value": ""},
    {"field": "notes", "type": "long_text"}, {"field": "assessment", "type": "long_text"},
)
_EDITABLE_FIELDS.update({"create_application_event": _EVENT_FIELDS, "update_application_event": _EVENT_FIELDS, "add_note": _NOTE_FIELDS, "update_note": _NOTE_FIELDS, "update_offer": _OFFER_FIELDS})


def editable_fields_for_tool(name: str) -> list[dict[str, JSONValue]]:
    return deepcopy(list(_EDITABLE_FIELDS.get(name, ())))


def _arguments(args: object) -> dict[str, Any]:
    return cast(dict[str, Any], args) if isinstance(args, dict) else {}


def _confirmation_description(name: str, args: object) -> str:
    values = _arguments(args)
    if name == "create_application":
        return f"新建投递：{values.get('company_name', '')} - {values.get('position_name', '')}"
    if name == "update_application_status":
        return f"将投递 #{values.get('id', '')} 的状态改为 {values.get('status', '')}"
    if name == "create_application_event":
        labels = {"written_test": "笔试", "interview": "面试", "offer_step": "Offer 进展", "deadline": "截止", "custom": "自定义"}
        title = labels.get(str(values.get("event_type") or ""), "日程")
        raw_time = str(values.get("scheduled_at") or "")
        try:
            parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            shown_time = parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            shown_time = raw_time
        duration = values.get("duration_minutes")
        shown_duration = f"{duration} 分钟" if duration not in (None, "") else ""
        details = " · ".join(value for value in (title, shown_time, shown_duration) if value)
        return f"新建日程：{details}" if details else "新建日程"
    actions = {
        "update_application_event": "更新日程", "delete_application_event": "删除日程",
        "update_note": "更新复盘", "delete_note": "删除复盘", "update_offer": "更新 Offer",
        "save_offer_assessment": "保存 Offer 评估", "resume_update_career_intent": "更新简历求职意向",
        "resume_rewrite_highlight": "改写简历亮点",
    }
    if name == "add_note":
        details = " · ".join(str(values.get(key) or "").strip() for key in ("company", "position", "round") if str(values.get(key) or "").strip())
        return f"新增复盘：{details}" if details else "新增复盘"
    return f"{actions.get(name, name)} #{values.get('id', '')}"


def _with_runtime_metadata(spec: ToolSpec[Any, Any]) -> ToolSpec[Any, Any]:
    if spec.kind != "write":
        return spec

    def describe(args: Any) -> str:
        return _confirmation_description(spec.name, args)

    return replace(
        spec,
        editable_fields=tuple(editable_fields_for_tool(spec.name)),
        confirmation_description=describe,
        write_contract=WriteContract(
            undo_policy=(
                UndoPolicy.REQUIRED
                if spec.name in {
                    "create_application",
                    "update_application_status",
                    "create_application_event",
                    "add_note",
                }
                else UndoPolicy.NONE
            )
        ),
    )


def build_model_tool_catalog() -> ToolCatalog:
    raw_specs = (
        *application_specs(),
        *application_event_specs(),
        *note_specs(),
        *offer_specs(),
        *resume_specs(),
        *jd_analysis_specs(),
    )
    specs = tuple(_with_runtime_metadata(spec) for spec in raw_specs)
    authority_manifest = authority_manifest_for_specs(specs, strict=True)
    return ToolCatalog(
        specs,
        expected_names=MODEL_TOOL_NAMES,
        authority_manifest=authority_manifest,
    )


MODEL_TOOL_CATALOG = build_model_tool_catalog()
