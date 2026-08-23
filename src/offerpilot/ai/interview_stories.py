from __future__ import annotations

import hashlib
import json
from time import perf_counter
from typing import Any, Callable, cast

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply
from offerpilot.repositories.interview_stories import (
    StorySourceSnapshot,
    StoryValidationError,
    canonical_story_content,
    validate_story_evidence_links,
)


_TOP_LEVEL_FIELDS = {
    "title",
    "blocks",
    "capability_labels",
    "applicable_questions",
    "fact_gap_codes",
}
_REF_FIELDS = {
    "source_kind",
    "source_stable_id",
    "source_version_or_snapshot",
    "source_path",
    "excerpt",
}
_SHAPE_CATEGORIES = {
    "invalid_json",
    "invalid_shape",
    "invalid_evidence_shape",
}
_BLOCK_KINDS = {"situation", "task", "action", "result", "reflection"}
_MAX_BLOCKS = 12
_MAX_SHORT_ITEMS = 12
_MAX_FACT_GAPS = 8
_MAX_TEXT_CHARS = 4_000
_MAX_SHORT_TEXT_CHARS = 300

INTERVIEW_STORY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_TOP_LEVEL_FIELDS),
    "properties": {
        "title": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "evidence_refs"],
            "properties": {
                "text": {"type": "string", "maxLength": 200},
                "evidence_refs": {"type": "array", "maxItems": 5, "items": {"$ref": "#/$defs/ref"}},
            },
        },
        "blocks": {
            "type": "array",
            "maxItems": _MAX_BLOCKS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "text", "fact_mode", "evidence_refs"],
                "properties": {
                    "kind": {"enum": sorted(_BLOCK_KINDS)},
                    "text": {"type": "string", "maxLength": _MAX_TEXT_CHARS},
                    "fact_mode": {"enum": ["evidence_backed", "user_view"]},
                    "evidence_refs": {"type": "array", "maxItems": 5, "items": {"$ref": "#/$defs/ref"}},
                },
            },
        },
        "capability_labels": {
            "type": "array",
            "maxItems": _MAX_SHORT_ITEMS,
            "items": {"$ref": "#/$defs/short_item"},
        },
        "applicable_questions": {
            "type": "array",
            "maxItems": _MAX_SHORT_ITEMS,
            "items": {"$ref": "#/$defs/short_item"},
        },
        "fact_gap_codes": {
            "type": "array",
            "maxItems": _MAX_FACT_GAPS,
            "items": {"const": "missing_result"},
        },
    },
    "$defs": {
        "short_item": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "evidence_refs"],
            "properties": {
                "text": {"type": "string", "maxLength": _MAX_SHORT_TEXT_CHARS},
                "evidence_refs": {"type": "array", "maxItems": 5, "items": {"$ref": "#/$defs/ref"}},
            },
        },
        "ref": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_REF_FIELDS),
            "properties": {
                "source_kind": {"enum": ["resume_version", "interview_note", "mock_turn", "user_assertion"]},
                "source_stable_id": {"type": "string", "minLength": 1},
                "source_version_or_snapshot": {"type": "string", "minLength": 1},
                "source_path": {"type": "string", "minLength": 1},
                "excerpt": {"type": "string", "minLength": 1, "maxLength": 800},
            },
        },
    },
}

INTERVIEW_STORY_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "interview_story_proposal", "strict": True, "schema": INTERVIEW_STORY_JSON_SCHEMA},
}


class StoryProposalError(ValueError):
    def __init__(self, category: str, message: str = "story proposal is not verifiable") -> None:
        super().__init__(message)
        self.category = category
        self.provider_request_id = ""
        self.repair_count = 0
        self.elapsed_ms = 0
        self.http_status: int | None = None
        self.timeout = False


class StoryProviderError(StoryProposalError):
    def __init__(self) -> None:
        super().__init__("provider_error", "story provider request failed")


StoryDiagnosticSink = Callable[[dict[str, Any]], None]


def safe_empty_interview_story_proposal() -> dict[str, Any]:
    return {
        "proposal_status": "safe_empty",
        "content": {
            "title": {"id": "title", "text": ""},
            "blocks": [],
            "capability_labels": [],
            "applicable_questions": [],
            "fact_gap_codes": [],
        },
        "evidence_links": [],
    }


def validate_interview_story_proposal(
    payload: Any, snapshot: StorySourceSnapshot
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise StoryProposalError("invalid_shape")
    if _is_exact_safe_empty(payload):
        return safe_empty_interview_story_proposal()
    try:
        content, link_inputs = _normalize_raw_proposal(payload)
        links = validate_story_evidence_links(content, link_inputs, snapshot)
    except StoryValidationError as exc:
        message = str(exc)
        if "exceeds limit" in message:
            raise StoryProposalError("limit_exceeded") from exc
        if "evidence link shape" in message:
            raise StoryProposalError("invalid_evidence_shape") from exc
        if any(token in message for token in ("shape", "object", "array", "string", "extra fields")):
            raise StoryProposalError("invalid_shape") from exc
        if any(token in message for token in ("excerpt", "source")):
            category = "excerpt_mismatch" if "excerpt" in message else "unknown_evidence_ref"
            raise StoryProposalError(category) from exc
        raise StoryProposalError("semantic_contract") from exc
    return {
        "proposal_status": "normal",
        "content": content,
        "evidence_links": [item.as_dict() for item in links],
    }


def generate_interview_story_proposal(
    model: ChatModel,
    snapshot: StorySourceSnapshot,
    *,
    on_diagnostic: StoryDiagnosticSink | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    last_category = "invalid_json"
    request_id = ""
    for call_index in range(2):
        prompt = _generation_prompt(snapshot) if call_index == 0 else _repair_prompt(last_category, snapshot)
        try:
            assistant = model.complete(
                [Message(role="system", content=_system_prompt()), Message(role="user", content=prompt)],
                [],
                response_format=INTERVIEW_STORY_RESPONSE_FORMAT
                if getattr(model, "supports_json_schema", False) is True
                else None,
            )
        except Exception as exc:
            provider_error = StoryProviderError()
            diagnostic = getattr(exc, "diagnostic", {})
            info = diagnostic if isinstance(diagnostic, dict) else {}
            provider_error.provider_request_id = _redacted_request_id(
                info.get("provider_request_id", getattr(exc, "provider_request_id", ""))
            )
            raw_status = info.get("http_status", info.get("status_code", getattr(exc, "status_code", None)))
            provider_error.http_status = raw_status if isinstance(raw_status, int) else None
            provider_error.timeout = bool(info.get("timeout", False)) or isinstance(exc, TimeoutError)
            provider_error.repair_count = call_index
            provider_error.elapsed_ms = _elapsed_ms(started)
            _diagnose(on_diagnostic, provider_error, repair_attempted=call_index > 0)
            raise provider_error from exc
        request_id = _redacted_request_id(getattr(assistant, "provider_blocks", {}).get("request_id"))
        try:
            parsed = parse_json_reply(
                assistant.content,
                allow_fenced=False,
                reject_non_finite=True,
                reject_duplicate_keys=True,
            )
            result = validate_interview_story_proposal(parsed, snapshot)
            _diagnose(
                on_diagnostic,
                StoryProposalError("ok"),
                repair_attempted=call_index > 0,
                request_id=request_id,
                elapsed_ms=_elapsed_ms(started),
                repair_count=call_index,
            )
            return result
        except StoryProposalError as exc:
            last_category = exc.category
        except (TypeError, ValueError, RuntimeError) as exc:
            last_category = "invalid_json" if "duplicate" not in str(exc).lower() else "invalid_shape"
        if last_category not in _SHAPE_CATEGORIES:
            semantic_error = StoryProposalError(last_category)
            semantic_error.provider_request_id = request_id
            semantic_error.repair_count = call_index
            semantic_error.elapsed_ms = _elapsed_ms(started)
            _diagnose(on_diagnostic, semantic_error, repair_attempted=call_index > 0)
            raise semantic_error
    safe_empty_error = StoryProposalError(last_category)
    safe_empty_error.provider_request_id = request_id
    safe_empty_error.repair_count = 1
    safe_empty_error.elapsed_ms = _elapsed_ms(started)
    _diagnose(on_diagnostic, safe_empty_error, repair_attempted=True)
    return safe_empty_interview_story_proposal()


def _normalize_raw_proposal(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    title = payload.get("title")
    if not isinstance(title, dict) or set(title) != {"text", "evidence_refs"}:
        raise StoryProposalError("invalid_shape")
    blocks = payload.get("blocks")
    labels = payload.get("capability_labels")
    questions = payload.get("applicable_questions")
    gaps = payload.get("fact_gap_codes")
    if not all(isinstance(value, list) for value in (blocks, labels, questions, gaps)):
        raise StoryProposalError("invalid_shape")
    title_object = cast(dict[str, Any], title)
    blocks_list = cast(list[Any], blocks)
    labels_list = cast(list[Any], labels)
    questions_list = cast(list[Any], questions)
    gaps_list = cast(list[Any], gaps)
    if len(blocks_list) > _MAX_BLOCKS or len(labels_list) > _MAX_SHORT_ITEMS or len(questions_list) > _MAX_SHORT_ITEMS or len(gaps_list) > _MAX_FACT_GAPS:
        raise StoryProposalError("limit_exceeded")
    content: dict[str, Any] = {
        "title": title_object.get("text"),
        "blocks": [],
        "capability_labels": [],
        "applicable_questions": [],
        "fact_gap_codes": gaps_list,
    }
    ref_rows: list[tuple[str, int, Any]] = [("title", 0, title_object.get("evidence_refs"))]
    for block in blocks_list:
        if not isinstance(block, dict) or set(block) != {"kind", "text", "fact_mode", "evidence_refs"}:
            raise StoryProposalError("invalid_shape")
        if not isinstance(block.get("text"), str) or len(block["text"]) > _MAX_TEXT_CHARS:
            raise StoryProposalError("invalid_shape" if not isinstance(block.get("text"), str) else "limit_exceeded")
        content["blocks"].append({key: block[key] for key in ("kind", "text", "fact_mode")})
        ref_rows.append(("block", len(content["blocks"]) - 1, block.get("evidence_refs")))
    for field, target_kind, entries in (
        ("capability_labels", "capability_label", labels_list),
        ("applicable_questions", "applicable_question", questions_list),
    ):
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"text", "evidence_refs"}:
                raise StoryProposalError("invalid_shape")
            if not isinstance(entry.get("text"), str) or len(entry["text"]) > _MAX_SHORT_TEXT_CHARS:
                raise StoryProposalError("invalid_shape" if not isinstance(entry.get("text"), str) else "limit_exceeded")
            content[field].append(entry["text"])
            ref_rows.append((target_kind, len(content[field]) - 1, entry.get("evidence_refs")))
    try:
        canonical = canonical_story_content(content)
    except StoryValidationError as exc:
        raise StoryProposalError("semantic_contract") from exc
    if not isinstance(canonical["title"]["text"], str) or not canonical["title"]["text"].strip():
        raise StoryProposalError("semantic_contract")
    has_result = any(block["kind"] == "result" for block in canonical["blocks"])
    if has_result != (canonical["fact_gap_codes"] == []):
        raise StoryProposalError("semantic_contract")
    links: list[dict[str, Any]] = []
    target_ids = {
        ("title", 0): canonical["title"]["id"],
        **{("block", index): item["id"] for index, item in enumerate(canonical["blocks"])},
        **{("capability_label", index): item["id"] for index, item in enumerate(canonical["capability_labels"])},
        **{("applicable_question", index): item["id"] for index, item in enumerate(canonical["applicable_questions"])},
    }
    for target_kind, index, refs in ref_rows:
        if not isinstance(refs, list) or not refs:
            raise StoryProposalError("invalid_shape")
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != _REF_FIELDS:
                raise StoryProposalError("invalid_evidence_shape")
            if not all(isinstance(ref.get(field), str) for field in _REF_FIELDS):
                raise StoryProposalError("invalid_evidence_shape")
        if len(refs) > 5:
            raise StoryProposalError("limit_exceeded")
        for ref in refs:
            links.append({"target_kind": target_kind, "target_id": target_ids[(target_kind, index)], **ref})
    return canonical, links


def _is_exact_safe_empty(payload: dict[str, Any]) -> bool:
    return payload == {
        "title": {"text": "", "evidence_refs": []},
        "blocks": [],
        "capability_labels": [],
        "applicable_questions": [],
        "fact_gap_codes": [],
    }


def _evidence_catalog(snapshot: StorySourceSnapshot) -> list[dict[str, str]]:
    return [
        {
            "source_kind": source["source_kind"],
            "source_stable_id": source["source_stable_id"],
            "source_version_or_snapshot": source["source_version_or_snapshot"],
            "source_path": source["path"],
            "excerpt": source["excerpt"],
        }
        for source in snapshot.sources
    ]


def _system_prompt() -> str:
    return (
        "Return only strict JSON. Create an interview Story only from the explicit evidence catalog. "
        "Every non-empty title, STAR block, capability label, and applicable question needs an exact evidence reference. "
        "Reflection must use fact_mode=user_view; all other STAR blocks must use fact_mode=evidence_backed. "
        "If no result can be evidenced, omit result and use only fact_gap_codes=[\"missing_result\"]. "
        "Do not infer facts, rankings, scores, applications, jobs, knowledge, memory, or chat content. "
        + json.dumps(INTERVIEW_STORY_JSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    )


def _generation_prompt(snapshot: StorySourceSnapshot) -> str:
    return "Generate one strictly evidence-gated interview Story from this catalog: " + json.dumps(
        _evidence_catalog(snapshot), ensure_ascii=False, separators=(",", ":")
    )


def _repair_prompt(category: str, snapshot: StorySourceSnapshot) -> str:
    return (
        "The previous response failed strict JSON validation with category "
        + category
        + ". Return only the required JSON shape. Evidence references must contain exactly "
        "source_kind, source_stable_id, source_version_or_snapshot, source_path, excerpt. "
        "Use only this same frozen evidence catalog; do not add fields or explanations. "
        + json.dumps(_evidence_catalog(snapshot), ensure_ascii=False, separators=(",", ":"))
    )


def _redacted_request_id(value: object) -> str:
    raw = str(value or "")
    return "" if not raw else "request-redacted-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _diagnose(
    sink: StoryDiagnosticSink | None,
    error: StoryProposalError,
    *,
    repair_attempted: bool,
    request_id: str | None = None,
    elapsed_ms: int | None = None,
    repair_count: int | None = None,
) -> None:
    if sink is None:
        return
    sink(
        {
            "failure_category": error.category,
            "repair_attempted": repair_attempted,
            "repair_count": error.repair_count if repair_count is None else repair_count,
            "elapsed_ms": error.elapsed_ms if elapsed_ms is None else elapsed_ms,
            "provider_request_id": error.provider_request_id if request_id is None else request_id,
            "http_status": error.http_status,
            "timeout": error.timeout,
        }
    )
