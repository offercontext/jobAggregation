from __future__ import annotations

import json
import math
from datetime import datetime
from time import perf_counter
from typing import Any, Callable

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply

INTERVIEW_REVIEW_UNGROUNDED_SUMMARY_V1 = (
    "本次复盘记录不足以形成有依据的表现判断，请先补充待澄清问题。"
)
INTERVIEW_REVIEW_UNGROUNDED_QUESTIONS_V1 = (
    "请补充本次面试中未记录的具体问题与回答。",
    "请补充你希望进一步澄清的内容。",
    "请补充你希望下次练习的具体场景。",
)
MAX_REVIEW_ITEMS = 10
MAX_EVIDENCE_REFS = 5

_NOTE_FIELDS = (
    "company",
    "position",
    "round",
    "date",
    "questions",
    "self_reflection",
    "difficulty_points",
    "mood",
)
_EVENT_FIELDS = (
    "id",
    "application_id",
    "event_type",
    "subtype",
    "round",
    "scheduled_at",
    "duration_minutes",
    "status",
)
_NOTE_EVIDENCE_PATHS = {f"/{field}" for field in _NOTE_FIELDS[4:]}
_TOP_LEVEL_FIELDS = {
    "summary",
    "observations",
    "clarifications",
    "practice_focuses",
    "next_questions",
}
_SUMMARY_FIELDS = {"text", "evidence_refs"}
_OBSERVATION_FIELDS = {"id", "text", "evidence_refs"}
_QUESTION_FIELDS = {"id", "question", "evidence_refs"}


INTERVIEW_REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "observations",
        "clarifications",
        "practice_focuses",
        "next_questions",
    ],
    "properties": {
        "summary": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "evidence_refs"],
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "evidence_refs": {"$ref": "#/$defs/evidence_refs"},
            },
        },
        "observations": {"$ref": "#/$defs/observations"},
        "clarifications": {"$ref": "#/$defs/questions"},
        "practice_focuses": {"$ref": "#/$defs/observations"},
        "next_questions": {"$ref": "#/$defs/questions"},
    },
    "$defs": {
        "evidence_refs": {
            "type": "array",
            "maxItems": MAX_EVIDENCE_REFS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "path", "excerpt"],
                "properties": {
                    "source": {"const": "interview_note"},
                    "path": {"enum": sorted(_NOTE_EVIDENCE_PATHS)},
                    "excerpt": {"type": "string", "minLength": 1},
                },
            },
        },
        "observations": {
            "type": "array",
            "maxItems": MAX_REVIEW_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "text", "evidence_refs"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                    "evidence_refs": {"$ref": "#/$defs/evidence_refs"},
                },
            },
        },
        "questions": {
            "type": "array",
            "maxItems": MAX_REVIEW_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "question", "evidence_refs"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "question": {"type": "string", "minLength": 1},
                    "evidence_refs": {"$ref": "#/$defs/evidence_refs"},
                },
            },
        },
    },
}

INTERVIEW_REVIEW_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "interview_review_proposal",
        "strict": True,
        "schema": INTERVIEW_REVIEW_JSON_SCHEMA,
    },
}


class InterviewReviewModelError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        failure_category: str = "unverifiable",
        validation_category: str | None = None,
        retry_count: int = 0,
        duration_ms: int = 0,
        provider_request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.validation_category = validation_category or failure_category
        self.retry_count = retry_count
        self.duration_ms = duration_ms
        self.provider_request_id = provider_request_id


class _InterviewReviewProviderError(Exception):
    pass


InterviewReviewDiagnosticSink = Callable[[dict[str, Any]], None]


def safe_empty_interview_review_proposal() -> dict[str, Any]:
    return {
        "summary": {
            "text": INTERVIEW_REVIEW_UNGROUNDED_SUMMARY_V1,
            "evidence_refs": [],
        },
        "observations": [],
        "clarifications": [
            {
                "id": "clarification-1",
                "question": INTERVIEW_REVIEW_UNGROUNDED_QUESTIONS_V1[0],
                "evidence_refs": [],
            }
        ],
        "practice_focuses": [],
        "next_questions": [],
    }


def build_interview_review_snapshot(note: Any, event: Any) -> dict[str, Any]:
    return {
        "note": {field: _model_value(note, field) for field in _NOTE_FIELDS},
        "event": {field: _snapshot_value(_model_value(event, field)) for field in _EVENT_FIELDS},
    }


def validate_interview_review(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    _assert_finite_json(payload)
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise InterviewReviewModelError(
            "invalid top-level fields", validation_category="unexpected_field"
        )
    note = snapshot.get("note")
    if not isinstance(note, dict) or any(not isinstance(note.get(field), str) for field in _NOTE_FIELDS):
        raise InterviewReviewModelError(
            "invalid interview note snapshot", validation_category="invalid_structure"
        )

    summary = payload["summary"]
    if not isinstance(summary, dict):
        raise InterviewReviewModelError(
            "invalid summary shape", validation_category="invalid_structure"
        )
    if set(summary) != _SUMMARY_FIELDS:
        raise InterviewReviewModelError(
            "invalid summary fields", validation_category="unexpected_field"
        )
    summary_text = summary.get("text")
    summary_refs = _validate_text_and_refs(summary_text, summary.get("evidence_refs"), snapshot)
    if not summary_refs and summary_text != INTERVIEW_REVIEW_UNGROUNDED_SUMMARY_V1:
        raise InterviewReviewModelError(
            "summary requires evidence", validation_category="missing_evidence_ref"
        )

    normalized: dict[str, Any] = {
        "summary": {"text": summary_text, "evidence_refs": summary_refs},
        "observations": [],
        "clarifications": [],
        "practice_focuses": [],
        "next_questions": [],
    }
    seen_ids: set[str] = set()
    for field in ("observations", "clarifications", "practice_focuses", "next_questions"):
        items = payload[field]
        if not isinstance(items, list) or len(items) > MAX_REVIEW_ITEMS:
            raise InterviewReviewModelError(
                f"{field} exceeds the item limit", validation_category="invalid_structure"
            )
        for item in items:
            expected_fields = _QUESTION_FIELDS if field in {"clarifications", "next_questions"} else _OBSERVATION_FIELDS
            if not isinstance(item, dict):
                raise InterviewReviewModelError(
                    f"invalid {field} item shape", validation_category="invalid_structure"
                )
            if set(item) != expected_fields:
                raise InterviewReviewModelError(
                    f"invalid {field} item fields", validation_category="unexpected_field"
                )
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip() or item_id in seen_ids:
                raise InterviewReviewModelError(
                    "review item ids must be unique non-empty strings",
                    validation_category="invalid_structure",
                )
            seen_ids.add(item_id)
            text_key = "question" if field in {"clarifications", "next_questions"} else "text"
            text_value = item.get(text_key)
            refs = _validate_text_and_refs(text_value, item.get("evidence_refs"), snapshot)
            if not refs and text_value not in INTERVIEW_REVIEW_UNGROUNDED_QUESTIONS_V1:
                raise InterviewReviewModelError(
                    f"{field} item requires evidence",
                    validation_category="missing_evidence_ref",
                )
            normalized[field].append(
                {"id": item_id, text_key: text_value, "evidence_refs": refs}
            )

    if _note_is_empty(note):
        if (
            summary_text != INTERVIEW_REVIEW_UNGROUNDED_SUMMARY_V1
            or summary_refs
            or normalized["observations"]
            or normalized["practice_focuses"]
        ):
            raise InterviewReviewModelError(
                "empty review notes cannot produce observations",
                validation_category="missing_evidence_ref",
            )
        for field in ("clarifications", "next_questions"):
            if any(item["evidence_refs"] for item in normalized[field]):
                raise InterviewReviewModelError(
                    "empty review notes cannot have evidence",
                    validation_category="invalid_structure",
                )

    return normalized


def generate_interview_review_proposal(
    model: ChatModel,
    snapshot: dict[str, Any],
    *,
    on_diagnostic: InterviewReviewDiagnosticSink | None = None,
) -> dict[str, Any]:
    system = _interview_review_system()
    prompt = _interview_review_prompt(snapshot)
    response_format = (
        INTERVIEW_REVIEW_RESPONSE_FORMAT
        if bool(getattr(model, "supports_json_schema", False))
        else None
    )
    started_at = perf_counter()
    provider_request_id = ""
    last_validation_category = "unverifiable"
    for attempt in range(2):
        user = (
            prompt
            if attempt == 0
            else _interview_review_repair_prompt(snapshot, last_validation_category)
        )
        try:
            assistant = _complete_interview_review_model(
                model,
                [Message(role="system", content=system), Message(role="user", content=user)],
                response_format,
            )
            provider_request_id = str(assistant.provider_blocks.get("request_id") or "")
        except Exception as exc:
            _emit_diagnostic(
                on_diagnostic,
                failure_category="provider_error",
                repair_attempted=attempt > 0,
                retry_count=attempt,
                duration_ms=_elapsed_ms(started_at),
                provider_request_id=provider_request_id,
            )
            raise InterviewReviewModelError(
                "model provider request failed",
                failure_category="provider_error",
                validation_category="provider_error",
                retry_count=attempt,
                duration_ms=_elapsed_ms(started_at),
                provider_request_id=provider_request_id,
            ) from exc
        try:
            payload = parse_json_reply(
                assistant.content,
                allow_fenced=False,
                reject_non_finite=True,
                reject_duplicate_keys=True,
            )
            return validate_interview_review(payload, snapshot)
        except InterviewReviewModelError as exc:
            last_validation_category = exc.validation_category
            if attempt == 0:
                continue
            break
        except (TypeError, ValueError, RuntimeError):
            last_validation_category = "invalid_json"
            if attempt == 0:
                continue
            break
    safe_empty = safe_empty_interview_review_proposal()
    validated_safe_empty = validate_interview_review(safe_empty, snapshot)
    _emit_diagnostic(
        on_diagnostic,
        failure_category=last_validation_category,
        repair_attempted=True,
        retry_count=1,
        duration_ms=_elapsed_ms(started_at),
        provider_request_id=provider_request_id,
    )
    return validated_safe_empty


def _emit_diagnostic(
    sink: InterviewReviewDiagnosticSink | None,
    *,
    failure_category: str,
    repair_attempted: bool,
    retry_count: int,
    duration_ms: int,
    provider_request_id: str,
) -> None:
    if sink is None:
        return
    sink(
        {
            "failure_category": failure_category,
            "repair_attempted": repair_attempted,
            "retry_count": retry_count,
            "duration_ms": duration_ms,
            "provider_request_id": provider_request_id,
        }
    )


def _complete_interview_review_model(
    model: ChatModel,
    messages: list[Message],
    response_format: dict[str, Any] | None,
) -> Any:
    if response_format is None:
        return model.complete(messages, [])
    return model.complete(messages, [], response_format=response_format)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _validate_text_and_refs(
    text_value: Any, raw_refs: Any, snapshot: dict[str, Any]
) -> list[dict[str, str]]:
    if not isinstance(text_value, str) or not text_value.strip():
        raise InterviewReviewModelError(
            "review text must be a non-empty string", validation_category="invalid_structure"
        )
    if not isinstance(raw_refs, list) or len(raw_refs) > MAX_EVIDENCE_REFS:
        raise InterviewReviewModelError(
            "evidence_refs exceeds the limit", validation_category="invalid_structure"
        )
    refs: list[dict[str, str]] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, dict):
            raise InterviewReviewModelError(
                "invalid evidence reference shape", validation_category="invalid_structure"
            )
        if set(raw_ref) != {"source", "path", "excerpt"}:
            raise InterviewReviewModelError(
                "invalid evidence reference fields", validation_category="unexpected_field"
            )
        if raw_ref.get("source") != "interview_note":
            raise InterviewReviewModelError(
                "invalid evidence source", validation_category="unknown_evidence_ref"
            )
        path = raw_ref.get("path")
        excerpt = raw_ref.get("excerpt")
        if (
            not isinstance(path, str)
            or path not in _NOTE_EVIDENCE_PATHS
            or not isinstance(excerpt, str)
            or not excerpt.strip()
        ):
            raise InterviewReviewModelError(
                "invalid evidence reference", validation_category="unknown_evidence_ref"
            )
        value = snapshot["note"].get(path[1:])
        if not isinstance(value, str) or excerpt not in value:
            raise InterviewReviewModelError(
                "evidence excerpt does not match snapshot",
                validation_category="unknown_evidence_ref",
            )
        refs.append({"source": "interview_note", "path": path, "excerpt": excerpt})
    return refs


def _note_is_empty(note: dict[str, str]) -> bool:
    return all(not note[field].strip() for field in _NOTE_FIELDS[4:])


def _assert_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise InterviewReviewModelError(
            "non-finite JSON value", validation_category="invalid_json"
        )
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite_json(item)


def _model_value(model: Any, field: str) -> Any:
    return getattr(model, field, None)


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.isoformat().replace("+00:00", "Z")
    return value


def _interview_review_system() -> str:
    empty_example = json.dumps(
        {
            "summary": {
                "text": INTERVIEW_REVIEW_UNGROUNDED_SUMMARY_V1,
                "evidence_refs": [],
            },
            "observations": [],
            "clarifications": [
                {
                    "id": "clarification-1",
                    "question": INTERVIEW_REVIEW_UNGROUNDED_QUESTIONS_V1[0],
                    "evidence_refs": [],
                }
            ],
            "practice_focuses": [],
            "next_questions": [],
        },
        ensure_ascii=False,
    )
    return """You review one user's saved interview note. Return raw JSON only, never Markdown fences.
Use only the supplied frozen note fields as evidence. Event metadata is context only and is never evidence.
Do not infer ability, weakness, interviewer judgment, external facts, or anything not written in the note.
The exact top-level fields are summary, observations, clarifications, practice_focuses, next_questions.
summary is an object with exactly text and evidence_refs; it is never a string.
Every observation and practice_focus is an object with exactly id, text, and evidence_refs.
Every clarification and next_question is an object with exactly id, question, and evidence_refs.
Each of the four arrays has at most 10 items, and every evidence_refs array has at most 5 items.
Summary, observations, and practice_focuses require non-empty evidence_refs unless using the exact safe summary.
Every question needs evidence_refs unless it is one exact fixed safe question supplied by the contract.
Each evidence reference must be exactly {"source":"interview_note","path":"...","excerpt":"..."}.
Return only the strict JSON object matching the contract and never add fields.
If you cannot produce a cited item, return this exact safe JSON shape:
""" + empty_example


def _interview_review_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "Generate an evidence-gated interview review proposal from this frozen snapshot. "
        "Use exact contiguous excerpts from note fields and return a safe empty proposal when needed.\n"
        "Verified evidence candidates (copy excerpts only from these values):\n"
        f"{_evidence_candidates_prompt(snapshot)}"
        f"Frozen snapshot:\n{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}"
    )


def _interview_review_repair_prompt(snapshot: dict[str, Any], failure_category: str) -> str:
    return (
        "The previous output failed safe validation. "
        f"Machine-readable failure category: {failure_category}. "
        "Return only raw JSON for the same frozen snapshot and contract. Do not explain, "
        "repeat the invalid output, add fields, or use new sources.\n"
        "If no cited item can be produced, return the exact safe empty proposal from the system contract.\n"
        "Verified evidence candidates (copy excerpts only from these values):\n"
        f"{_evidence_candidates_prompt(snapshot)}"
        f"Frozen snapshot:\n{json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}"
    )


def _evidence_candidates_prompt(snapshot: dict[str, Any]) -> str:
    note = snapshot.get("note")
    if not isinstance(note, dict):
        return "- no valid note evidence candidates\n"
    return "".join(
        f"- {path}: {json.dumps(note.get(path[1:]), ensure_ascii=False)}\n"
        for path in sorted(_NOTE_EVIDENCE_PATHS)
    )
