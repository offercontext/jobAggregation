from __future__ import annotations

import json
from time import perf_counter
from typing import Any, Callable

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply
from offerpilot.knowledge.interview_capture import CanonicalFragment


MAX_TITLE_CHARS = 120
MAX_BLOCKS = 20
MAX_BLOCK_TEXT_CHARS = 2000
MAX_EVIDENCE_REFS_PER_BLOCK = 5
SAFE_EMPTY_PREVIEW: dict[str, Any] = {"title": "", "blocks": []}

INTERVIEW_KNOWLEDGE_PREVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "blocks"],
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "blocks": {
            "type": "array",
            "maxItems": MAX_BLOCKS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["block_id", "text", "evidence_refs"],
                "properties": {
                    "block_id": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_BLOCK_TEXT_CHARS},
                    "evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_EVIDENCE_REFS_PER_BLOCK,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["fragment_id", "excerpt"],
                            "properties": {
                                "fragment_id": {"type": "string", "minLength": 1},
                                "excerpt": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
    },
}

INTERVIEW_KNOWLEDGE_PREVIEW_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "interview_knowledge_capture_preview",
        "strict": True,
        "schema": INTERVIEW_KNOWLEDGE_PREVIEW_JSON_SCHEMA,
    },
}

DiagnosticSink = Callable[[dict[str, Any]], None]


class InterviewKnowledgePreviewError(ValueError):
    def __init__(self, category: str, message: str | None = None) -> None:
        super().__init__(message or category)
        self.category = category


class InterviewKnowledgeProviderError(RuntimeError):
    pass


def _fragment_map(fragments: list[CanonicalFragment]) -> dict[str, CanonicalFragment]:
    return {fragment.fragment_id: fragment for fragment in fragments}


def validate_interview_knowledge_preview(
    payload: Any, fragments: list[CanonicalFragment]
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InterviewKnowledgePreviewError("invalid_structure")
    if set(payload) != {"title", "blocks"}:
        raise InterviewKnowledgePreviewError("unexpected_field")
    title = payload.get("title")
    blocks = payload.get("blocks")
    if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS:
        raise InterviewKnowledgePreviewError("limit_exceeded")
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise InterviewKnowledgePreviewError("limit_exceeded")
    fragment_map = _fragment_map(fragments)
    normalized_blocks: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    for raw_block in blocks:
        if not isinstance(raw_block, dict):
            raise InterviewKnowledgePreviewError("invalid_structure")
        if set(raw_block) != {"block_id", "text", "evidence_refs"}:
            raise InterviewKnowledgePreviewError("unexpected_field")
        block_id = raw_block.get("block_id")
        text_value = raw_block.get("text")
        refs = raw_block.get("evidence_refs")
        if (
            not isinstance(block_id, str)
            or not block_id
            or block_id in block_ids
            or not isinstance(text_value, str)
        ):
            raise InterviewKnowledgePreviewError("invalid_structure")
        if not text_value or len(text_value) > MAX_BLOCK_TEXT_CHARS:
            raise InterviewKnowledgePreviewError("limit_exceeded")
        if not isinstance(refs, list) or not refs or len(refs) > MAX_EVIDENCE_REFS_PER_BLOCK:
            raise InterviewKnowledgePreviewError("missing_evidence_ref" if not refs else "limit_exceeded")
        block_ids.add(block_id)
        normalized_refs: list[dict[str, str]] = []
        for raw_ref in refs:
            if not isinstance(raw_ref, dict) or set(raw_ref) != {"fragment_id", "excerpt"}:
                raise InterviewKnowledgePreviewError("unexpected_field")
            fragment_id = raw_ref.get("fragment_id")
            excerpt = raw_ref.get("excerpt")
            if not isinstance(fragment_id, str) or fragment_id not in fragment_map:
                raise InterviewKnowledgePreviewError("unknown_evidence_ref")
            if not isinstance(excerpt, str) or excerpt != fragment_map[fragment_id].text:
                raise InterviewKnowledgePreviewError("excerpt_mismatch")
            normalized_refs.append({"fragment_id": fragment_id, "excerpt": excerpt})
        normalized_blocks.append(
            {"block_id": block_id, "text": text_value, "evidence_refs": normalized_refs}
        )
    return {"title": title, "blocks": normalized_blocks}


def _system_prompt() -> str:
    return (
        "你只根据用户选中的面试原始片段生成可编辑笔记预览。严格只输出 JSON，"
        "不要 Markdown，不要补造事实，不要输出能力判断、弱点、练习、Memory 或外部事实。"
        "每个内容块必须引用所给 fragment_id，并逐字复制 excerpt；没有可靠建议时返回 "
        '{"title":"","blocks":[]}。'
    )


def _user_prompt(fragments: list[CanonicalFragment], failure_category: str | None = None) -> str:
    payload = [
        {"fragment_id": item.fragment_id, "path": item.path, "text": item.text}
        for item in fragments
    ]
    repair = ""
    if failure_category:
        repair = (
            f"上一次输出未通过机器校验，失败类别为 {failure_category}。"
            "只返回符合既定契约的 raw JSON，不要解释失败原因。"
        )
    return (
        "请整理以下已选原始片段。输出字段只能是 title 和 blocks；每个 block 只能有 "
        "block_id、text、evidence_refs，evidence_refs 必须引用现有 fragment_id 且 excerpt 逐字相等。"
        f"{repair}\n冻结片段：{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _emit(sink: DiagnosticSink | None, **values: Any) -> None:
    if sink is not None:
        sink(values)


def generate_interview_knowledge_preview(
    model: ChatModel,
    fragments: list[CanonicalFragment],
    *,
    on_diagnostic: DiagnosticSink | None = None,
) -> dict[str, Any]:
    response_format = (
        INTERVIEW_KNOWLEDGE_PREVIEW_RESPONSE_FORMAT
        if getattr(model, "supports_json_schema", False) is True
        else None
    )
    started = perf_counter()
    last_category = "invalid_json"
    for attempt in range(2):
        messages = [
            Message(role="system", content=_system_prompt()),
            Message(
                role="user",
                content=_user_prompt(fragments, last_category if attempt else None),
            ),
        ]
        try:
            if response_format is None:
                assistant = model.complete(messages, [])
            else:
                assistant = model.complete(messages, [], response_format=response_format)
        except Exception as exc:
            _emit(
                on_diagnostic,
                failure_category="provider_error",
                repair_attempted=attempt > 0,
                retry_count=attempt,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
            raise InterviewKnowledgeProviderError() from exc
        try:
            payload = parse_json_reply(
                assistant.content,
                allow_fenced=False,
                reject_non_finite=True,
                reject_duplicate_keys=True,
            )
            result = validate_interview_knowledge_preview(payload, fragments)
            _emit(
                on_diagnostic,
                failure_category="",
                repair_attempted=attempt > 0,
                retry_count=attempt,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
            return result
        except InterviewKnowledgePreviewError as exc:
            last_category = exc.category
        except (TypeError, ValueError, RuntimeError):
            last_category = "invalid_json"
    _emit(
        on_diagnostic,
        failure_category=last_category,
        repair_attempted=True,
        retry_count=1,
        duration_ms=max(0, int((perf_counter() - started) * 1000)),
    )
    return dict(SAFE_EMPTY_PREVIEW)
