from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal
from uuid import uuid4

from offerpilot.ai.agent_contracts import PendingAction


MAX_JD_UTF8_BYTES = 60_000
_JD_ACTION_KEYS = {"type", "jdText", "sourceUrl"}
_SNAPSHOT_ACTION_KEYS = {
    "type", "resumeId", "jdVersionId", "materialKitId", "submittedAt", "note"
}
_OUTCOME_ACTION_KEYS = {
    "type", "snapshotId", "eventId", "stage", "result", "feedbackText",
    "reflectionText", "nextActionText", "feedbackTags", "occurredAt",
}
_CANCEL_MESSAGES = frozenset({"取消", "算了", "先不用", "不用了", "不保存", "不要保存"})
_JD_TARGET = r"(?:JD|jd|岗位描述|职位描述|岗位资料)"
_JD_COMMAND_PREFIX = r"(?:给\s*)?(?:当前\s*)?(?:投递\s*)?"
_JD_COMMAND_HEAD = re.compile(
    rf"^\s*{_JD_COMMAND_PREFIX}(?:保存|更新|补充|录入)\s*"
    rf"(?:当前\s*)?(?:投递\s*)?{_JD_TARGET}\s*$"
)
_JD_COMMAND_WITH_BODY = re.compile(
    rf"^\s*{_JD_COMMAND_PREFIX}(?:保存|更新|补充|录入)\s*"
    rf"(?:当前\s*)?(?:投递\s*)?{_JD_TARGET}"
    rf"(?:[ \t]*(?::|：)|\r?\n)([\s\S]+)$"
)
_ASCII_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class PilotAction:
    jd_text: str | None
    source_url: str | None


@dataclass(frozen=True)
class PilotSubmissionSnapshotAction:
    resume_id: int
    jd_version_id: int
    material_kit_id: int | None
    submitted_at: str
    note: str


@dataclass(frozen=True)
class PilotOutcomeAction:
    snapshot_id: int
    event_id: int | None
    stage: str
    result: str
    feedback_text: str
    reflection_text: str
    next_action_text: str
    feedback_tags: tuple[str, ...]
    occurred_at: str


@dataclass(frozen=True)
class PilotActionDecision:
    kind: Literal["normal_agent", "collecting_jd", "pending_confirmation", "cancelled"]
    jd_text: str | None = None
    source_url: str | None = None
    question: str = ""


def parse_pilot_action(
    payload: Any,
) -> PilotAction | PilotSubmissionSnapshotAction | PilotOutcomeAction:
    if not isinstance(payload, dict):
        raise ValueError("pilot_action must be an object")
    action_type = payload.get("type")
    if action_type == "application_submission_snapshot":
        if set(payload) - _SNAPSHOT_ACTION_KEYS:
            raise ValueError("unsupported pilot_action fields")
        return PilotSubmissionSnapshotAction(
            resume_id=_positive_int(payload.get("resumeId"), "resumeId"),
            jd_version_id=_positive_int(payload.get("jdVersionId"), "jdVersionId"),
            material_kit_id=_optional_positive_int(payload.get("materialKitId"), "materialKitId"),
            submitted_at=_required_string(payload.get("submittedAt"), "submittedAt"),
            note=_optional_string(payload.get("note"), "note"),
        )
    if action_type == "application_outcome_record":
        if set(payload) - _OUTCOME_ACTION_KEYS:
            raise ValueError("unsupported pilot_action fields")
        raw_tags = payload.get("feedbackTags", [])
        if not isinstance(raw_tags, list) or any(not isinstance(item, str) for item in raw_tags):
            raise ValueError("feedbackTags must be an array of strings")
        return PilotOutcomeAction(
            snapshot_id=_positive_int(payload.get("snapshotId"), "snapshotId"),
            event_id=_optional_positive_int(payload.get("eventId"), "eventId"),
            stage=_required_string(payload.get("stage"), "stage"),
            result=_required_string(payload.get("result"), "result"),
            feedback_text=_optional_string(payload.get("feedbackText"), "feedbackText"),
            reflection_text=_optional_string(payload.get("reflectionText"), "reflectionText"),
            next_action_text=_optional_string(payload.get("nextActionText"), "nextActionText"),
            feedback_tags=tuple(raw_tags),
            occurred_at=_required_string(payload.get("occurredAt"), "occurredAt"),
        )
    if set(payload) - _JD_ACTION_KEYS or action_type != "application_jd_save":
        raise ValueError("unsupported pilot_action")
    source_url = payload.get("sourceUrl")
    if source_url is not None and not isinstance(source_url, str):
        raise ValueError("sourceUrl must be a string or null")
    jd_text = payload.get("jdText")
    if jd_text is None:
        return PilotAction(jd_text=None, source_url=source_url)
    if not isinstance(jd_text, str):
        raise ValueError("jdText is required")
    if not jd_text.strip():
        return PilotAction(jd_text=None, source_url=source_url)
    if len(jd_text.encode("utf-8")) > MAX_JD_UTF8_BYTES:
        raise ValueError("jdText is too large")
    return PilotAction(jd_text=jd_text, source_url=source_url)


def build_submission_snapshot_pending_action(
    *, application_id: int, action: PilotSubmissionSnapshotAction,
    id_factory: Callable[[], str], key_factory: Callable[[], str],
) -> PendingAction:
    args = {
        "application_id": application_id,
        "resume_id": action.resume_id,
        "jd_version_id": action.jd_version_id,
        "material_kit_id": action.material_kit_id,
        "submitted_at": action.submitted_at,
        "note": action.note,
        "idempotency_key": key_factory(),
    }
    return PendingAction(
        tool_call_id=id_factory(),
        tool_name="create_application_submission_snapshot",
        args=json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        human="请确认冻结这次实际投递使用的简历、岗位资料和材料。",
        operation_id=str(uuid4()),
    )


def build_outcome_pending_action(
    *, application_id: int, action: PilotOutcomeAction,
    id_factory: Callable[[], str], key_factory: Callable[[], str],
) -> PendingAction:
    args = {
        "application_id": application_id,
        "submission_snapshot_id": action.snapshot_id,
        "application_event_id": action.event_id,
        "stage": action.stage,
        "result": action.result,
        "feedback_text": action.feedback_text,
        "reflection_text": action.reflection_text,
        "next_action_text": action.next_action_text,
        "feedback_tags": list(action.feedback_tags),
        "occurred_at": action.occurred_at,
        "idempotency_key": key_factory(),
    }
    return PendingAction(
        tool_call_id=id_factory(),
        tool_name="record_application_outcome",
        args=json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        human="请确认记录这次投递进展、原始反馈和下一步行动。",
        operation_id=str(uuid4()),
    )


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_positive_int(value: object, name: str) -> int | None:
    return None if value is None else _positive_int(value, name)


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _optional_string(value: object, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def match_application_jd_command(message: str) -> str | None:
    if not isinstance(message, str):
        return None
    matched = _JD_COMMAND_WITH_BODY.fullmatch(message)
    if matched is None:
        return None
    body = matched.group(1)
    return body if body.strip() else None


def is_pilot_cancel_message(message: str) -> bool:
    return isinstance(message, str) and message.strip() in _CANCEL_MESSAGES


def decide_pilot_action(
    message: str,
    *,
    has_current_jd: bool,
    collecting_jd: bool = False,
) -> PilotActionDecision:
    if collecting_jd:
        if is_pilot_cancel_message(message):
            return PilotActionDecision(kind="cancelled")
        if isinstance(message, str) and message.strip():
            return PilotActionDecision(kind="pending_confirmation", jd_text=message)
        return PilotActionDecision(kind="collecting_jd", question="请粘贴完整岗位描述")

    body = match_application_jd_command(message)
    if body is not None:
        return PilotActionDecision(kind="pending_confirmation", jd_text=body)
    if isinstance(message, str) and _JD_COMMAND_HEAD.fullmatch(message):
        return PilotActionDecision(kind="collecting_jd", question="请粘贴完整岗位描述")
    return PilotActionDecision(kind="normal_agent")


def build_pilot_pending_action(
    *,
    application_id: int,
    current_version_id: int | None,
    jd_text: str,
    source_url: str | None,
    id_factory: Callable[[], str],
    key_factory: Callable[[], str],
) -> PendingAction:
    if type(application_id) is not int or application_id <= 0:
        raise ValueError("application_id must be a positive integer")
    if not isinstance(jd_text, str):
        raise ValueError("jd_text is required")
    if len(jd_text.encode("utf-8")) > MAX_JD_UTF8_BYTES:
        raise ValueError("jd_text is too large")
    if source_url is not None and not isinstance(source_url, str):
        raise ValueError("source_url must be a string or null")
    tool_call_id = id_factory()
    idempotency_key = key_factory()
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ValueError("tool_call_id is invalid")
    if not isinstance(idempotency_key, str) or _ASCII_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key is invalid")
    args = json.dumps(
        {
            "application_id": application_id,
            "jd_text": jd_text,
            "source_url": source_url,
            "expected_current_version_id": current_version_id,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PendingAction(
        tool_call_id=tool_call_id,
        tool_name="save_application_jd_version",
        args=args,
        human="请确认将这份岗位资料保存到当前投递。",
        operation_id=str(uuid4()),
    )
