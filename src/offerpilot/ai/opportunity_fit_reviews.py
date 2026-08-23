from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply
from offerpilot.repositories.json_contract import canonical_json, sha256_text


class OpportunityFitModelError(ValueError):
    def __init__(self, message: str, *, failure_category: str = "invalid_change_shape") -> None:
        super().__init__(message)
        self.failure_category = failure_category


@dataclass(frozen=True)
class ValidatedOpportunityOutput:
    payload: dict[str, Any]


_TRIAGE_FIELDS = {
    "recommendation",
    "hard_constraints",
    "fit_signals",
    "gaps",
    "deadline",
    "next_questions",
}
_HARD_CONSTRAINT_FIELDS = {"id", "requirement", "status", "explanation", "evidence_refs"}
_FIT_SIGNAL_FIELDS = {"id", "statement", "evidence_refs"}
_GAP_FIELDS = {"id", "requirement", "kind", "candidate_status", "evidence_refs"}
_DEADLINE_FIELDS = {"status", "text", "evidence_refs"}
_DEEP_FIELDS = {
    "strengths",
    "gaps_to_address",
    "questions_to_clarify",
    "recommended_path",
    "next_actions",
}
_REVIEW_ITEM_FIELDS = {"id", "statement", "evidence_refs"}
_ACTION_FIELDS = {"id", "label", "kind"}
_EVIDENCE_REF_FIELDS = {"source", "path", "excerpt"}
_RECOMMENDATIONS = {"advance", "hold", "decline"}
_HARD_STATUSES = {"met", "unmet", "unknown"}
_GAP_KINDS = {"required", "preferred"}
_GAP_STATUSES = {"met", "unmet", "unknown"}
_DEADLINE_STATUSES = {"stated", "not_stated"}
_DEEP_PATHS = {"prepare_materials", "clarify_first", "do_not_pursue"}
_ACTION_KINDS = {"open_material_kit", "add_assertion", "record_deadline"}
_V2_SOURCE = {
    "kind": "opportunity_fit",
    "contract_version": "opportunity_fit.v2",
    "snapshot_version": "1",
}
_V2_QUESTION_TEXT = {
    "opportunity_fit.question.v1.jd_success_criteria": "请确认该岗位最重要的成功标准是什么？",
    "opportunity_fit.question.v1.team_expectations": "请确认该岗位团队希望优先解决的问题是什么？",
    "opportunity_fit.question.v1.interview_process": "请确认面试流程、参与者和后续安排是什么？",
    "opportunity_fit.question.v1.missing_candidate_detail": "请补充当前资料中尚未说明、但你希望核对的信息。",
}
_V2_SAFE_EMPTY_TEXT = "当前资料不足，无法给出可验证的继续建议"
_V2_SAFE_EMPTY_RATIONALE = "当前没有足够的冻结证据支持具体建议。"
_V2_MAX_ITEMS = {"conditions": 8, "risks": 8, "questions": 6, "next_steps": 8}
_V2_REPAIR_INSTRUCTIONS = {
    "invalid_json": "Return valid JSON with no Markdown fences or commentary.",
    "duplicate_json_key": "Emit each JSON object key exactly once.",
    "root_object_invalid": "Return one JSON object as the root value, not an array or scalar.",
    "missing_field": "Include every required field and no optional substitute field.",
    "unexpected_field": "Remove every field not explicitly listed in the v2 contract.",
    "invalid_field_type": (
        "Use strings for text, rationale, id, source, path, and excerpt; arrays for lists; "
        "and objects for summary, items, and every evidence reference. An evidence_refs item "
        "must be exactly {source, path, excerpt}, never a string."
    ),
    "empty_value": "Replace blank or whitespace-only required strings with exact snapshot-backed values.",
    "quantity_limit": "Keep within the stated array and evidence_refs limits.",
    "evidence_source_invalid": "Use only the allowed evidence source values and never invent a source.",
    "evidence_path_invalid": (
        "Use exactly /jd_text for JD, a relative string-leaf path such as /raw_text or /skills/0 "
        "for resume, and /user_assertions/<index>/text for user assertions."
    ),
    "evidence_excerpt_invalid": "Copy each excerpt as a non-empty contiguous substring of its referenced snapshot value.",
    "missing_evidence_ref": "Add a valid evidence reference to every concrete item, or use the exact safe-empty response.",
    "semantic_invalid": "Return the exact v2 structure and safe-empty semantics; never add a decision or unsupported claim.",
}


def build_source_snapshot(
    *,
    application_id: int,
    company_name: str,
    position_name: str,
    resume_id: int | None,
    resume_title: str,
    resume_content: dict[str, Any],
    jd_text: str,
    jd_source_label: str,
    candidate_assertions: list[str],
) -> dict[str, Any]:
    resume_json = canonical_json(resume_content)
    jd_hash = sha256_text(jd_text)
    return {
        "schema_version": 1,
        "application": {
            "id": application_id,
            "company_name": company_name,
            "position_name": position_name,
        },
        "resume": {
            "id": resume_id,
            "title": resume_title,
            "content_json": copy.deepcopy(resume_content),
            "sha256": sha256_text(resume_json),
        },
        "jd": {
            "source_label": jd_source_label,
            "text": jd_text,
            "sha256": jd_hash,
        },
        "candidate_assertions": [
            {"index": index, "text": text}
            for index, text in enumerate(candidate_assertions)
        ],
    }


def validate_triage(payload: dict[str, Any], snapshot: dict[str, Any]) -> ValidatedOpportunityOutput:
    _require_exact_fields(payload, _TRIAGE_FIELDS, "triage")
    recommendation = _require_enum(payload.get("recommendation"), _RECOMMENDATIONS, "recommendation")

    hard_constraints = _require_list(payload.get("hard_constraints"), "hard_constraints")
    for item in hard_constraints:
        _require_exact_fields(item, _HARD_CONSTRAINT_FIELDS, "hard constraint")
        _require_non_empty_string(item.get("id"), "hard constraint id")
        _require_non_empty_string(item.get("requirement"), "hard constraint requirement")
        status = _require_enum(item.get("status"), _HARD_STATUSES, "hard constraint status")
        _require_string(item.get("explanation"), "hard constraint explanation")
        refs = _validate_refs(
            item.get("evidence_refs"),
            snapshot,
            allow_jd=True,
            require_jd=True,
            require_candidate=status != "unknown",
        )
        if status != "unknown" and not refs:
            raise OpportunityFitModelError("known hard constraints need evidence_refs")

    fit_signals = _require_list(payload.get("fit_signals"), "fit_signals")
    for item in fit_signals:
        _require_exact_fields(item, _FIT_SIGNAL_FIELDS, "fit signal")
        _require_non_empty_string(item.get("id"), "fit signal id")
        _require_non_empty_string(item.get("statement"), "fit signal statement")
        _validate_refs(item.get("evidence_refs"), snapshot, allow_jd=False, require=True)

    gaps = _require_list(payload.get("gaps"), "gaps")
    for item in gaps:
        _require_exact_fields(item, _GAP_FIELDS, "gap")
        _require_non_empty_string(item.get("id"), "gap id")
        _require_non_empty_string(item.get("requirement"), "gap requirement")
        _require_enum(item.get("kind"), _GAP_KINDS, "gap kind")
        _require_enum(item.get("candidate_status"), _GAP_STATUSES, "gap candidate_status")
        _validate_refs(
            item.get("evidence_refs"),
            snapshot,
            allow_jd=True,
            require_jd=True,
            require=item.get("candidate_status") != "unknown",
            require_candidate=item.get("candidate_status") != "unknown",
        )

    deadline = payload.get("deadline")
    if not isinstance(deadline, dict):
        raise OpportunityFitModelError("deadline must be an object")
    _require_exact_fields(deadline, _DEADLINE_FIELDS, "deadline")
    deadline_status = _require_enum(deadline.get("status"), _DEADLINE_STATUSES, "deadline status")
    deadline_text = _require_string(deadline.get("text"), "deadline text")
    deadline_refs = _validate_refs(deadline.get("evidence_refs"), snapshot, allow_jd=True)
    if deadline_status == "stated" and (not deadline_text.strip() or not deadline_refs):
        raise OpportunityFitModelError("stated deadline needs text and evidence_refs")
    if deadline_status == "not_stated" and (deadline_text.strip() or deadline_refs):
        raise OpportunityFitModelError("not_stated deadline must be empty")

    next_questions = _require_string_list(payload.get("next_questions"), "next_questions")
    if recommendation == "advance" and any(item.get("status") == "unmet" for item in hard_constraints):
        raise OpportunityFitModelError("advance cannot contain unmet hard constraints")
    if recommendation == "hold" and not next_questions and not any(
        item.get("status") == "unknown" for item in hard_constraints
    ):
        raise OpportunityFitModelError("hold needs unresolved questions")
    if recommendation == "decline" and not any(
        item.get("status") == "unmet" and item.get("evidence_refs") for item in hard_constraints
    ) and not any(item.get("candidate_status") == "unmet" for item in gaps):
        raise OpportunityFitModelError("decline needs a referenced blocker")

    validated = copy.deepcopy(payload)
    validated["summary"] = _derive_triage_summary(validated)
    return ValidatedOpportunityOutput(payload=validated)


def _derive_triage_summary(payload: dict[str, Any]) -> dict[str, Any]:
    refs: list[dict[str, str]] = []
    for section in ("hard_constraints", "fit_signals", "gaps"):
        for item in payload[section]:
            refs.extend(item["evidence_refs"])
    refs.extend(payload["deadline"]["evidence_refs"])
    unique_refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        ref_key = (ref["source"], ref["path"], ref["excerpt"])
        if ref_key not in seen:
            seen.add(ref_key)
            unique_refs.append(ref)
    has_candidate_evidence = any(ref["source"] != "jd" for ref in unique_refs)
    if has_candidate_evidence:
        text = f"Evidence-backed review recommendation: {payload['recommendation']}."
    elif unique_refs:
        text = "The role requirements are referenced, but no candidate evidence is available; clarify before proceeding."
    else:
        text = "No source-backed candidate conclusion is available; clarify before proceeding."
    return {"text": text, "evidence_refs": unique_refs}


def validate_deep_review(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
) -> ValidatedOpportunityOutput:
    _require_exact_fields(payload, _DEEP_FIELDS, "deep review")
    strengths = _require_list(payload.get("strengths"), "strengths")
    for item in strengths:
        _require_exact_fields(item, _REVIEW_ITEM_FIELDS, "strengths")
        _require_non_empty_string(item.get("id"), "strengths id")
        _require_non_empty_string(item.get("statement"), "strengths statement")
        _validate_refs(
            item.get("evidence_refs"),
            snapshot,
            allow_jd=False,
            require=True,
        )

    gaps = _require_list(payload.get("gaps_to_address"), "gaps_to_address")
    for item in gaps:
        _require_exact_fields(item, _REVIEW_ITEM_FIELDS, "gaps_to_address")
        _require_non_empty_string(item.get("id"), "gaps_to_address id")
        _require_non_empty_string(item.get("statement"), "gaps_to_address statement")
        _validate_refs(
            item.get("evidence_refs"),
            snapshot,
            allow_jd=True,
            require=True,
        )

    questions = _require_list(payload.get("questions_to_clarify"), "questions_to_clarify")
    for item in questions:
        _require_exact_fields(item, _REVIEW_ITEM_FIELDS, "question")
        _require_non_empty_string(item.get("id"), "question id")
        _require_non_empty_string(item.get("statement"), "question statement")
        _validate_refs(item.get("evidence_refs"), snapshot, allow_jd=True)

    _require_enum(payload.get("recommended_path"), _DEEP_PATHS, "recommended_path")
    actions = _require_list(payload.get("next_actions"), "next_actions")
    for item in actions:
        _require_exact_fields(item, _ACTION_FIELDS, "next action")
        _require_non_empty_string(item.get("id"), "next action id")
        _require_non_empty_string(item.get("label"), "next action label")
        _require_enum(item.get("kind"), _ACTION_KINDS, "next action kind")

    return ValidatedOpportunityOutput(payload=copy.deepcopy(payload))


def validate_triage_v2(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedOpportunityOutput:
    return _validate_v2_stage(payload, snapshot, expected_stage="triage")


def validate_deep_review_v2(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> ValidatedOpportunityOutput:
    return _validate_v2_stage(payload, snapshot, expected_stage="deep_review")


def _validate_v2_stage(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    expected_stage: str,
) -> ValidatedOpportunityOutput:
    if not isinstance(payload, dict):
        raise OpportunityFitModelError(
            "opportunity fit v2 must be an object",
            failure_category="root_object_invalid",
        )
    _v2_require_exact_fields(
        payload,
        {
            "schema_version",
            "stage",
            "source",
            "summary",
            "conditions",
            "risks",
            "questions",
            "next_steps",
        },
        "opportunity fit v2",
    )
    if payload.get("schema_version") != 2 or payload.get("stage") != expected_stage:
        raise OpportunityFitModelError("opportunity fit v2 stage is invalid", failure_category="semantic_invalid")
    if payload.get("source") != _V2_SOURCE:
        raise OpportunityFitModelError("opportunity fit v2 source is invalid", failure_category="semantic_invalid")

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise OpportunityFitModelError("summary must be an object", failure_category="invalid_field_type")
    _v2_require_exact_fields(summary, {"text", "rationale", "evidence_refs"}, "summary")
    summary_text = _v2_require_non_empty_string(summary.get("text"), "summary text")
    _v2_require_bounded_string(summary.get("rationale"), "summary rationale")
    summary_refs = _validate_v2_refs(summary.get("evidence_refs"), snapshot)
    safe_empty = summary_text == _V2_SAFE_EMPTY_TEXT
    if not summary_refs and not safe_empty:
        raise OpportunityFitModelError("summary needs evidence_refs", failure_category="missing_evidence_ref")
    if safe_empty and summary.get("rationale") != _V2_SAFE_EMPTY_RATIONALE:
        raise OpportunityFitModelError(
            "safe empty summary rationale is invalid", failure_category="semantic_invalid"
        )
    if safe_empty and summary_refs:
        raise OpportunityFitModelError(
            "safe empty summary cannot cite evidence", failure_category="semantic_invalid"
        )

    ids: set[str] = set()
    for field in ("conditions", "risks", "next_steps"):
        items = _v2_require_items(payload.get(field), field, _V2_MAX_ITEMS[field])
        if field in {"conditions", "risks"} and not items and not safe_empty:
            raise OpportunityFitModelError(
                f"normal v2 output needs non-empty {field}", failure_category="semantic_invalid"
            )
        for item in items:
            _v2_require_exact_fields(item, {"id", "text", "rationale", "evidence_refs"}, field)
            item_id = _v2_require_non_empty_string(item.get("id"), f"{field} id")
            if item_id in ids:
                raise OpportunityFitModelError(
                    "v2 item ids must be globally unique", failure_category="semantic_invalid"
                )
            ids.add(item_id)
            _v2_require_bounded_string(item.get("text"), f"{field} text")
            _v2_require_bounded_string(item.get("rationale"), f"{field} rationale")
            refs = _validate_v2_refs(item.get("evidence_refs"), snapshot)
            if not refs:
                raise OpportunityFitModelError(
                    f"{field} items need evidence_refs", failure_category="missing_evidence_ref"
                )

    questions = _v2_require_items(payload.get("questions"), "questions", _V2_MAX_ITEMS["questions"])
    if safe_empty and any(payload.get(field) for field in ("conditions", "risks", "questions", "next_steps")):
        raise OpportunityFitModelError(
            "safe empty output must contain only empty arrays", failure_category="semantic_invalid"
        )
    question_ids: set[str] = set()
    for item in questions:
        _v2_require_exact_fields(item, {"question_id", "text", "evidence_refs"}, "question")
        question_id = _v2_require_non_empty_string(item.get("question_id"), "question_id")
        if question_id in question_ids:
            raise OpportunityFitModelError("question_id must be unique", failure_category="semantic_invalid")
        if question_id in ids:
            raise OpportunityFitModelError(
                "v2 item ids must be globally unique", failure_category="semantic_invalid"
            )
        question_ids.add(question_id)
        ids.add(question_id)
        if question_id in _V2_QUESTION_TEXT:
            if item.get("text") != _V2_QUESTION_TEXT[question_id]:
                raise OpportunityFitModelError("fixed question text is invalid", failure_category="semantic_invalid")
            _validate_v2_refs(item.get("evidence_refs"), snapshot)
        else:
            _v2_require_bounded_string(item.get("text"), "question text")
            if not _validate_v2_refs(item.get("evidence_refs"), snapshot):
                raise OpportunityFitModelError(
                    "contextual questions need evidence_refs", failure_category="missing_evidence_ref"
                )

    validated = copy.deepcopy(payload)
    validated["summary"]["evidence_refs"] = summary_refs
    return ValidatedOpportunityOutput(payload=validated)


def _v2_require_items(value: Any, field: str, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OpportunityFitModelError(f"{field} must be an array of objects", failure_category="invalid_field_type")
    items = value
    if len(items) > maximum:
        raise OpportunityFitModelError(f"{field} exceeds maximum length", failure_category="quantity_limit")
    return items


def _v2_require_bounded_string(value: Any, label: str, maximum: int = 1000) -> str:
    result = _v2_require_string(value, label)
    if not result.strip():
        raise OpportunityFitModelError(f"{label} must be non-empty", failure_category="empty_value")
    if len(result) > maximum:
        raise OpportunityFitModelError(f"{label} exceeds maximum length", failure_category="quantity_limit")
    return result


def _validate_v2_refs(refs: Any, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(refs, list):
        raise OpportunityFitModelError("evidence_refs must be an array", failure_category="invalid_field_type")
    if len(refs) > 4:
        raise OpportunityFitModelError("evidence_refs exceeds maximum length", failure_category="quantity_limit")
    validated: list[dict[str, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise OpportunityFitModelError("evidence reference must be an object", failure_category="invalid_field_type")
        _v2_require_exact_fields(ref, _EVIDENCE_REF_FIELDS, "evidence reference")
        source = _v2_require_string(ref.get("source"), "evidence source")
        path = _v2_require_string(ref.get("path"), "evidence path")
        excerpt = _v2_require_non_empty_string(ref.get("excerpt"), "evidence excerpt")
        if source == "jd":
            if path != "/jd_text":
                raise OpportunityFitModelError("v2 JD evidence path is invalid", failure_category="evidence_path_invalid")
            expected = _snapshot_value(snapshot, "jd", "text")
        elif source == "resume":
            try:
                expected = _resume_value(snapshot, path)
            except OpportunityFitModelError as exc:
                raise OpportunityFitModelError(
                    "v2 resume evidence path is invalid", failure_category="evidence_path_invalid"
                ) from exc
        elif source == "user_assertion":
            try:
                expected = _assertion_value(snapshot, path)
            except OpportunityFitModelError as exc:
                raise OpportunityFitModelError(
                    "v2 user assertion evidence path is invalid", failure_category="evidence_path_invalid"
                ) from exc
        else:
            raise OpportunityFitModelError("unsupported evidence source", failure_category="evidence_source_invalid")
        if excerpt not in expected:
            raise OpportunityFitModelError(
                "evidence excerpt does not match snapshot", failure_category="evidence_excerpt_invalid"
            )
        validated.append({"source": source, "path": path, "excerpt": excerpt})
    return validated


def _v2_require_exact_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise OpportunityFitModelError(f"{label} must be an object", failure_category="invalid_field_type")
    fields = set(value)
    if expected - fields:
        raise OpportunityFitModelError(f"{label} has missing fields", failure_category="missing_field")
    if fields - expected:
        raise OpportunityFitModelError(f"{label} has unexpected fields", failure_category="unexpected_field")


def _v2_require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise OpportunityFitModelError(f"{label} must be a string", failure_category="invalid_field_type")
    return value


def _v2_require_non_empty_string(value: Any, label: str) -> str:
    result = _v2_require_string(value, label)
    if not result.strip():
        raise OpportunityFitModelError(f"{label} must be non-empty", failure_category="empty_value")
    return result


def generate_triage(model: ChatModel, snapshot: dict[str, Any]) -> ValidatedOpportunityOutput:
    return _generate(
        model,
        _triage_system(),
        _triage_prompt(snapshot),
        lambda payload: validate_triage(payload, snapshot),
    )


def generate_deep_review(
    model: ChatModel,
    snapshot: dict[str, Any],
    triage: dict[str, Any],
) -> ValidatedOpportunityOutput:
    return _generate(
        model,
        _deep_review_system(),
        _deep_review_prompt(snapshot, triage),
        lambda payload: validate_deep_review(payload, snapshot),
    )


def generate_triage_v2(model: ChatModel, snapshot: dict[str, Any]) -> ValidatedOpportunityOutput:
    return _generate(
        model,
        _v2_system("triage"),
        _v2_prompt(snapshot, None),
        lambda payload: validate_triage_v2(payload, snapshot),
        v2=True,
    )


def generate_deep_review_v2(
    model: ChatModel,
    snapshot: dict[str, Any],
    triage: dict[str, Any],
) -> ValidatedOpportunityOutput:
    return _generate(
        model,
        _v2_system("deep_review"),
        _v2_prompt(snapshot, triage),
        lambda payload: validate_deep_review_v2(payload, snapshot),
        v2=True,
    )


def _generate(
    model: ChatModel,
    system: str,
    prompt: str,
    validator: Callable[[dict[str, Any]], ValidatedOpportunityOutput],
    *,
    v2: bool = False,
) -> ValidatedOpportunityOutput:
    repair_category = "invalid_json" if v2 else "invalid_change_shape"
    for attempt in range(2):
        user = prompt
        if attempt == 1:
            if v2:
                repair_instruction = (
                    "Return only raw JSON matching the contract. Do not return Markdown, explanations, "
                    "the previous response, or new facts. Use only the same frozen snapshot. "
                    f"{_V2_REPAIR_INSTRUCTIONS.get(repair_category, _V2_REPAIR_INSTRUCTIONS['semantic_invalid'])} "
                    f"If evidence is insufficient, use the fixed safe-empty response: summary.text must be "
                    f"{json.dumps(_V2_SAFE_EMPTY_TEXT, ensure_ascii=False)}, summary.rationale must be "
                    f"{json.dumps(_V2_SAFE_EMPTY_RATIONALE, ensure_ascii=False)}, and all arrays must be empty."
                )
            else:
                repair_instruction = (
                    "Return only raw JSON matching the contract. Do not return Markdown, explanations, "
                    "the previous response, or new facts. If evidence is insufficient, use unknown or "
                    "an empty array. Never use JD evidence as a candidate fact and never put a JD "
                    "reference in fit_signals."
                )
            user = f"{prompt}\n\nRepair category: {repair_category}. {repair_instruction}"
        try:
            assistant = model.complete(
                [Message(role="system", content=system), Message(role="user", content=user)],
                [],
            )
        except Exception as exc:
            raise OpportunityFitModelError(
                "model provider request failed",
                failure_category="provider_error",
            ) from exc
        try:
            parsed = parse_json_reply(
                assistant.content,
                allow_fenced=False,
                reject_non_finite=True,
                reject_duplicate_keys=True,
            )
        except Exception as exc:
            repair_category = _json_failure_category(exc) if v2 else "invalid_change_shape"
            if attempt == 0:
                continue
            raise OpportunityFitModelError(
                "model output could not be verified",
                failure_category=repair_category,
            ) from exc
        try:
            return validator(parsed)
        except OpportunityFitModelError as exc:
            repair_category = exc.failure_category
            if attempt == 0:
                continue
            raise OpportunityFitModelError(
                "model output could not be verified",
                failure_category=repair_category,
            ) from exc
    raise OpportunityFitModelError(
        "model output could not be verified",
        failure_category=repair_category,
    )


def _json_failure_category(error: Exception) -> str:
    message = str(error)
    if "duplicate JSON key" in message:
        return "duplicate_json_key"
    if "must be a JSON object" in message:
        return "root_object_invalid"
    return "invalid_json"


def _validate_refs(
    refs: Any,
    snapshot: dict[str, Any],
    *,
    allow_jd: bool,
    require: bool = False,
    require_candidate: bool = False,
    require_jd: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(refs, list):
        raise OpportunityFitModelError("evidence_refs must be an array")
    if require and not refs:
        raise OpportunityFitModelError("evidence_refs must be non-empty")
    validated: list[dict[str, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise OpportunityFitModelError("evidence reference must be an object")
        _require_exact_fields(ref, {"source", "path", "excerpt"}, "evidence reference")
        source = _require_string(ref.get("source"), "evidence source")
        path = _require_string(ref.get("path"), "evidence path")
        excerpt = _require_string(ref.get("excerpt"), "evidence excerpt")
        if not excerpt.strip():
            raise OpportunityFitModelError("evidence excerpt must be non-empty")
        if source == "jd":
            if not allow_jd or path != "/text":
                raise OpportunityFitModelError("JD cannot be candidate evidence")
            expected = _snapshot_value(snapshot, "jd", "text")
        elif source == "resume":
            expected = _resume_value(snapshot, path)
        elif source == "user_assertion":
            expected = _assertion_value(snapshot, path)
        else:
            raise OpportunityFitModelError("unsupported evidence source")
        if expected != excerpt:
            raise OpportunityFitModelError("evidence excerpt does not match snapshot")
        validated.append({"source": source, "path": path, "excerpt": excerpt})
    if require_candidate and not any(ref["source"] != "jd" for ref in validated):
        raise OpportunityFitModelError("candidate conclusion needs resume or user_assertion evidence")
    if require_jd and not any(ref["source"] == "jd" for ref in validated):
        raise OpportunityFitModelError("role requirement needs JD evidence")
    return validated


def _snapshot_value(snapshot: dict[str, Any], section: str, key: str) -> str:
    value = snapshot.get(section)
    if not isinstance(value, dict) or not isinstance(value.get(key), str):
        raise OpportunityFitModelError("snapshot evidence source is unavailable")
    result = value[key]
    assert isinstance(result, str)
    return result


def _resume_value(snapshot: dict[str, Any], path: str) -> str:
    resume = snapshot.get("resume")
    if not isinstance(resume, dict) or not isinstance(resume.get("content_json"), dict):
        raise OpportunityFitModelError("resume snapshot is unavailable")
    if not path.startswith("/") or path.startswith("/content_json"):
        raise OpportunityFitModelError("resume evidence path is invalid")
    return _pointer_string(resume["content_json"], path)


def _assertion_value(snapshot: dict[str, Any], path: str) -> str:
    if not path.startswith("/user_assertions/") or not path.endswith("/text"):
        raise OpportunityFitModelError("user assertion evidence path is invalid")
    parts = path.split("/")
    if len(parts) != 4 or not parts[2].isdigit():
        raise OpportunityFitModelError("user assertion evidence path is invalid")
    assertions = snapshot.get("candidate_assertions")
    if not isinstance(assertions, list):
        raise OpportunityFitModelError("candidate assertions are unavailable")
    index = int(parts[2])
    if index < 0 or index >= len(assertions):
        raise OpportunityFitModelError("user assertion evidence path is invalid")
    item = assertions[index]
    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
        raise OpportunityFitModelError("user assertion evidence is invalid")
    result = item["text"]
    assert isinstance(result, str)
    return result


def _pointer_string(value: Any, path: str) -> str:
    current = value
    for raw_part in path.split("/")[1:]:
        if raw_part == "" or raw_part == "-" or (raw_part.isdigit() and str(int(raw_part)) != raw_part):
            raise OpportunityFitModelError("evidence path is invalid")
        if isinstance(current, dict):
            if raw_part not in current:
                raise OpportunityFitModelError("evidence path does not exist")
            current = current[raw_part]
        elif isinstance(current, list) and raw_part.isdigit():
            index = int(raw_part)
            if index >= len(current):
                raise OpportunityFitModelError("evidence path does not exist")
            current = current[index]
        else:
            raise OpportunityFitModelError("evidence path does not point to a string")
    if not isinstance(current, str):
        raise OpportunityFitModelError("evidence excerpt must point to a string")
    return current


def _require_exact_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise OpportunityFitModelError(f"{label} fields are invalid")


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise OpportunityFitModelError(f"{label} must be a string")
    return value


def _require_non_empty_string(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if not result.strip():
        raise OpportunityFitModelError(f"{label} must be non-empty")
    return result


def _require_enum(value: Any, choices: set[str], label: str) -> str:
    result = _require_string(value, label)
    if result not in choices:
        raise OpportunityFitModelError(f"{label} is invalid")
    return result


def _require_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OpportunityFitModelError(f"{label} must be an array of objects")
    return value


def _require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OpportunityFitModelError(f"{label} must be an array of strings")
    if any(not item.strip() for item in value):
        raise OpportunityFitModelError(f"{label} cannot contain empty strings")
    return value


def _triage_system() -> str:
    return (
        "You are a job opportunity decision analyst. Return raw JSON only: no Markdown, "
        "explanation, scores, probabilities, URLs, or platform claims. The exact top-level "
        "schema is {recommendation:advance|hold|decline, "
        "hard_constraints:array, fit_signals:array, gaps:array, deadline:object, "
        "next_questions:array}; do not add fields. Each hard constraint is exactly "
        "{id:string, requirement:string, status:met|unmet|unknown, explanation:string, evidence_refs:array}. "
        "Each fit signal is exactly {id:string, statement:string, evidence_refs:array}. "
        "Each gap is exactly {id:string, requirement:string, kind:required|preferred, "
        "candidate_status:met|unmet|unknown, evidence_refs:array}. Deadline is exactly "
        "{status:stated|not_stated, text:string, evidence_refs:array}. "
        "Each evidence ref is exactly {source:jd|resume|user_assertion, path:string, excerpt:string}. "
        "For source jd, path must be exactly /text and excerpt must equal the entire frozen JD text; "
        "do not use /jd/text. For source resume, "
        "path is relative to resume content_json and must not start with /content_json. "
        "JD is only a job requirement and analysis direction; never use it as candidate fact. "
        "Candidate facts require exact resume or user_assertion refs. User assertions must be "
        "treated as user-provided and not externally verified. Excerpts must be copied exactly "
        "from the frozen snapshot. Every hard constraint and gap must include at least one JD "
        "ref for the role requirement; when its candidate status is known, also include a "
        "resume or user_assertion ref. Use empty evidence_refs only for unresolved questions "
        "that are not stated as constraints or gaps. If there is no safe candidate evidence, "
        "use unknown rather than inventing a candidate fact. The server derives the visible "
        "summary from these validated, referenced entries; do not return a summary field. "
        "If unsure, return empty fit_signals rather than citing JD as candidate evidence."
    )


def _v2_system(stage: str) -> str:
    return (
        f"You are an evidence-gated opportunity fit analyst for the {stage} stage. "
        "Return raw JSON only. The exact top-level fields are schema_version, stage, source, "
        "summary, conditions, risks, questions, next_steps. schema_version must be 2 and "
        f"stage must be {stage}. Never return recommendation, score, probability, advance, "
        "hold, or decline. source must be exactly "
        '{"kind":"opportunity_fit","contract_version":"opportunity_fit.v2",'
        '"snapshot_version":"1"}. summary is exactly '
        '{"text":string,"rationale":string,"evidence_refs":array}; every condition, risk, '
        'or next_step is exactly {"id":string,"text":string,"rationale":string,"evidence_refs":array}; '
        'every question is exactly {"question_id":string,"text":string,"evidence_refs":array}. '
        'Every evidence_refs array contains objects exactly like '
        '{"source":"resume","path":"/raw_text","excerpt":"exact contiguous text"}; '
        "never put a plain string in evidence_refs and never omit source, path, or excerpt. "
        "Use at most 8 conditions, 8 risks, 6 questions, 8 next_steps, and 4 evidence_refs per item. "
        "All strings must be non-blank. Every concrete summary, condition, risk, and next_step needs "
        "at least one exact evidence reference. Evidence source is exactly jd, resume, or user_assertion. "
        "JD evidence uses exactly /jd_text; resume evidence uses a relative JSON path such as /raw_text "
        "or /skills/0 and never starts with /content_json; user_assertion evidence uses exactly "
        "/user_assertions/<index>/text. These paths resolve to string leaves; excerpts must be non-empty "
        "contiguous substrings copied exactly from the frozen "
        "snapshot. Fixed questions use their question_id and server-defined text. If evidence is "
        f"insufficient, return summary.text exactly {json.dumps(_V2_SAFE_EMPTY_TEXT, ensure_ascii=False)}, "
        f"summary.rationale exactly {json.dumps(_V2_SAFE_EMPTY_RATIONALE, ensure_ascii=False)}, "
        "and all four arrays empty. Never return recommendation, score, probability, advance, hold, "
        "decline, or any extra field."
    )


def _v2_prompt(snapshot: dict[str, Any], triage: dict[str, Any] | None) -> str:
    parent = f"\nConfirmed Triage: {json.dumps(triage, ensure_ascii=False, sort_keys=True)}" if triage else ""
    return (
        "Use only this frozen snapshot and the confirmed Triage when present. "
        "Do not use old AI recommendations, outside pages, or hidden context. "
        f"Snapshot: {json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}{parent}"
    )


def _deep_review_system() -> str:
    return (
        "You are a job opportunity deep-review analyst. Return raw JSON only: no Markdown, "
        "explanation, scores, probabilities, URLs, or platform claims. Use only the supplied "
        "frozen Triage and source snapshot; never introduce facts. The exact top-level schema is "
        "{strengths:array, gaps_to_address:array, questions_to_clarify:array, "
        "recommended_path:prepare_materials|clarify_first|do_not_pursue, next_actions:array}; "
        "do not add fields. Every review item is exactly {id:string, statement:string, "
        "evidence_refs:array}; every action is exactly {id:string, label:string, "
        "kind:open_material_kit|add_assertion|record_deadline}. Each evidence ref is exactly "
        "{source:jd|resume|user_assertion, path:string, excerpt:string}; excerpts must match "
        "the frozen snapshot exactly. JD refs must use path /text and are not candidate evidence; it may appear in a "
        "gaps_to_address must contain at least one evidence ref; a JD ref may only restate a "
        "job requirement. User assertions remain user-provided and not externally verified."
    )


def _triage_prompt(snapshot: dict[str, Any]) -> str:
    paths = _editable_snapshot_paths(snapshot)
    return (
        "Analyze this frozen source snapshot. Keep candidate facts separate from job requirements. "
        "When no candidate evidence supports a conclusion, use unknown or an empty array. The "
        "server derives the visible summary from validated evidence; do not return a summary field. "
        "A valid uncertain result has this shape (replace only with snapshot-backed values): "
        '{"recommendation":"hold","hard_constraints":[],"fit_signals":[],"gaps":[],"deadline":{"status":"not_stated","text":"","evidence_refs":[]},"next_questions":["..."]}.\n'
        f"Snapshot: {json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}\n"
        f"Available evidence paths: {json.dumps(paths, ensure_ascii=False)}"
    )


def _deep_review_prompt(snapshot: dict[str, Any], triage: dict[str, Any]) -> str:
    return (
        "Deep-review the existing Triage using only the frozen snapshot. If there is no safe "
        "candidate-backed strength, return an empty strengths array. A gap_to_address may cite "
        "JD only to restate a job requirement and must not claim a candidate fact. A valid empty review has this "
        'shape: {"strengths":[],"gaps_to_address":[],"questions_to_clarify":[],"recommended_path":"clarify_first","next_actions":[]}.\n'
        f"Snapshot: {json.dumps(snapshot, ensure_ascii=False, sort_keys=True)}\n"
        f"Triage: {json.dumps(triage, ensure_ascii=False, sort_keys=True)}"
    )


def _editable_snapshot_paths(snapshot: dict[str, Any]) -> list[str]:
    result: list[str] = []
    resume = snapshot.get("resume")
    content = resume.get("content_json") if isinstance(resume, dict) else None
    if isinstance(content, dict):
        result.extend(_walk_string_paths(content))
    jd = snapshot.get("jd")
    if isinstance(jd, dict) and isinstance(jd.get("text"), str):
        result.append("/jd/text")
    assertions = snapshot.get("candidate_assertions")
    if isinstance(assertions, list):
        result.extend(
            f"/user_assertions/{index}/text"
            for index, item in enumerate(assertions)
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return result


def _walk_string_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(_walk_string_paths(child, f"{prefix}/{key}"))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_walk_string_paths(child, f"{prefix}/{index}"))
        return paths
    return [prefix] if isinstance(value, str) else []
