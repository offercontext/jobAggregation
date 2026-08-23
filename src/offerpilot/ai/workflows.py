from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.repositories.jd import JDAnalysesRepository, JDAnalysisCreate
from offerpilot.repositories.notes import NotesRepository
from offerpilot.repositories.questions import QuestionCreate, QuestionsRepository, question_hash
from offerpilot.repositories.resumes import ResumeMatchCreate, ResumesRepository


@dataclass
class JDAnalysisResult:
    id: int
    application_id: int | None
    jd_source: str
    result: dict[str, Any]


@dataclass
class ResumeMatchResult:
    id: int
    resume_id: int
    application_id: int | None
    result: dict[str, Any]


@dataclass
class GeneratedQuestionsResult:
    count: int
    skipped: int
    questions: list[Any]


def analyze_jd(
    model: ChatModel,
    repo: JDAnalysesRepository,
    jd_text: str,
    jd_url: str = "",
    application_id: int | None = None,
) -> JDAnalysisResult:
    if jd_url:
        if not jd_text.strip():
            raise ValueError("jd_text_required")
        raise ValueError("jd_url_not_supported")
    if not jd_text.strip():
        raise ValueError("jd_text_required")
    source = "text"
    text = jd_text

    result = complete_json(model, system=structured_ai_system(), user=jd_analysis_prompt(text))
    analysis = repo.create(
        JDAnalysisCreate(
            application_id=application_id,
            jd_source=source,
            jd_text=text,
            result=json.dumps(result, ensure_ascii=False),
        )
    )
    return JDAnalysisResult(
        id=analysis.id,
        application_id=application_id,
        jd_source=source,
        result=result,
    )


def match_resume(
    model: ChatModel,
    repo: ResumesRepository,
    resume_id: int,
    jd_text: str,
    jd_url: str = "",
    application_id: int | None = None,
) -> ResumeMatchResult:
    resume = repo.get(resume_id)
    if resume is None:
        raise ValueError("Resume not found")
    if not resume.parsed_data:
        raise ValueError("Resume has no text content")

    if jd_url:
        if not jd_text.strip():
            raise ValueError("jd_text_required")
        raise ValueError("jd_url_not_supported")
    if not jd_text.strip():
        raise ValueError("jd_text_required")
    text = jd_text

    result = complete_json(
        model,
        system=structured_ai_system(),
        user=resume_match_prompt(resume.parsed_data, text),
    )
    match = repo.create_match(
        ResumeMatchCreate(
            resume_id=resume_id,
            application_id=application_id,
            jd_text=text,
            result=json.dumps(result, ensure_ascii=False),
        )
    )
    return ResumeMatchResult(
        id=match.id,
        resume_id=resume_id,
        application_id=application_id,
        result=result,
    )


def generate_questions(
    model: ChatModel,
    questions: QuestionsRepository,
    notes: NotesRepository,
    source: str = "notes",
    topic: str = "",
    application_id: int = 0,
    count: int = 8,
) -> GeneratedQuestionsResult:
    source = source.strip() or "notes"
    saved_application_id: int | None = None
    if source == "notes":
        note_rows = notes.list(application_id=application_id) if application_id > 0 else notes.list()
        label = "面试复盘真题"
        context_text = "\n\n".join(note.questions.strip() for note in note_rows if note.questions.strip())
        source_type = "ai_notes"
        saved_application_id = application_id if application_id > 0 else None
    else:
        raise ValueError("不支持的来源类型")
    if not context_text.strip():
        raise ValueError("所选来源没有可用于生成题目的内容")

    result = complete_json(
        model,
        system=structured_ai_system(),
        user=questions_prompt(label, context_text, clamp_question_count(count)),
    )
    saved, skipped = persist_generated_questions(
        questions,
        result.get("questions", []),
        source_type=source_type,
        application_id=saved_application_id,
        topic=topic,
    )
    return GeneratedQuestionsResult(count=len(saved), skipped=skipped, questions=saved)


def complete_json(
    model: ChatModel,
    system: str,
    user: str,
    *,
    strict_json: bool = False,
) -> dict[str, Any]:
    try:
        assistant = model.complete(
            [Message(role="system", content=system), Message(role="user", content=user)],
            [],
        )
        return parse_json_reply(
            assistant.content,
            allow_fenced=not strict_json,
            reject_non_finite=strict_json,
        )
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def parse_json_reply(
    reply: str,
    *,
    allow_fenced: bool = True,
    reject_non_finite: bool = False,
    reject_duplicate_keys: bool = False,
) -> dict[str, Any]:
    text = reply.strip()
    if not allow_fenced and (text.startswith("```") or text.endswith("```")):
        raise RuntimeError("AI response must be raw JSON without Markdown fences")
    if allow_fenced and text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :].strip()
        fence = text.rfind("```")
        if fence >= 0:
            text = text[:fence].strip()
    json_options: dict[str, Any] = {}
    if reject_non_finite:
        json_options["parse_constant"] = _reject_non_finite_json_constant
    if reject_duplicate_keys:
        json_options["object_pairs_hook"] = _reject_duplicate_json_keys
    value = json.loads(text, **json_options)
    if not isinstance(value, dict):
        raise RuntimeError("AI response must be a JSON object")
    return value


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def structured_ai_system() -> str:
    return (
        "你是一名专业的招聘求职分析师。只输出 JSON，不要使用 markdown 代码块。"
        "所有文字使用简体中文，数组字段为空时返回 []。"
    )


def jd_analysis_prompt(jd_text: str) -> str:
    return f"""请分析以下岗位描述（JD），输出如下 JSON：
{{
  "summary": "一句话总结这个岗位",
  "requirements": ["关键要求点，每条一句话"],
  "tech_stack": ["涉及的技术栈/工具"],
  "experience_years": "要求的年限，如 3-5 年，无要求填 不限",
  "education": "学历要求，如 本科及以上，无要求填 不限",
  "highlights": ["这个岗位吸引人的亮点"],
  "suggestions": ["针对求职者的准备建议，每条一句话"]
}}

JD 内容：
{truncate_for_prompt(jd_text)}"""


def resume_match_prompt(resume_text: str, jd_text: str) -> str:
    return f"""请对比以下简历和岗位 JD，评估匹配度，输出如下 JSON：
{{
  "match_score": 0到100的整数匹配度,
  "matched": ["简历中与 JD 匹配的点"],
  "gaps": ["简历中相对 JD 缺失或薄弱的点"],
  "suggestions": ["针对这份 JD 该如何优化简历/补足能力的建议"],
  "summary": "一句话总评"
}}

简历内容：
{truncate_for_prompt(resume_text)}

JD 内容：
{truncate_for_prompt(jd_text)}"""


def questions_prompt(source_label: str, context_text: str, count: int) -> str:
    return f"""你是一名资深技术面试官。请基于以下【{source_label}】设计 {count} 道面试题。
严格输出如下 JSON，不要输出多余文字：
{{
  "questions": [
    {{
      "category": "分类",
      "difficulty": "easy|medium|hard",
      "question": "题目",
      "reference_answer": "参考答案要点",
      "tags": ["关键词"]
    }}
  ]
}}

材料内容：
{truncate_for_prompt(context_text)}"""


def persist_generated_questions(
    repo: QuestionsRepository,
    generated: Any,
    source_type: str,
    application_id: int | None,
    topic: str = "",
) -> tuple[list[Any], int]:
    if not isinstance(generated, list):
        return [], 0
    seen = set(repo.hashes())
    to_create: list[QuestionCreate] = []
    skipped = 0
    for item in generated:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or "").strip()
        if not text:
            continue
        digest = question_hash(text)
        if digest in seen:
            skipped += 1
            continue
        seen.add(digest)
        tags_value = item.get("tags") or []
        tags = [str(tag) for tag in tags_value] if isinstance(tags_value, list) else []
        to_create.append(
            QuestionCreate(
                application_id=application_id,
                topic=topic,
                category=str(item.get("category") or "").strip(),
                difficulty=normalize_difficulty(str(item.get("difficulty") or "medium")),
                question=text,
                reference_answer=str(item.get("reference_answer") or "").strip(),
                tags=tags,
                source_type=source_type,
                status="new",
            )
        )
    return repo.bulk_create(to_create), skipped


def normalize_difficulty(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"easy", "简单"}:
        return "easy"
    if normalized in {"hard", "困难", "难"}:
        return "hard"
    return "medium"


def clamp_question_count(count: int) -> int:
    if count <= 0:
        return 8
    return min(count, 20)


def truncate_for_prompt(value: str, max_chars: int = 12000) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...(已截断)"
