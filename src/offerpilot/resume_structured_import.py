from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MAX_SOURCE_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_STRING_BYTES = 8 * 1024
MAX_ARRAY_ITEMS = 100
MAX_FIELDS = 1000

_SCALAR_PATHS = {
    "contact.name",
    "contact.email",
    "contact.phone",
    "contact.location",
}
_INDEX = r"(0|[1-9][0-9]*)"
_CAREER_RE = re.compile(rf"career_intent\.(target_roles|target_locations)\.{_INDEX}")
_EDUCATION_RE = re.compile(rf"education\.{_INDEX}\.(school|degree|major|start_date|end_date)")
_EXPERIENCE_RE = re.compile(rf"experience\.{_INDEX}\.(company|title|start_date|end_date)")
_PROJECT_RE = re.compile(rf"projects\.{_INDEX}\.(name|role|start_date|end_date)")
_HIGHLIGHT_RE = re.compile(rf"(experience|projects)\.{_INDEX}\.highlights\.{_INDEX}")
_SKILL_RE = re.compile(rf"skills\.{_INDEX}")


class ResumeStructureError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StructuredField:
    path: str
    value: str
    evidence: str

    def json(self) -> dict[str, str]:
        return {"path": self.path, "value": self.value, "evidence": self.evidence}


def raw_text_sha256(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def source_text(resume: Any) -> str:
    content = resume_content(resume)
    raw = content.get("raw_text")
    if isinstance(raw, str):
        return raw
    parsed_data = getattr(resume, "parsed_data", "")
    return parsed_data if isinstance(parsed_data, str) else ""


def resume_content(resume: Any) -> dict[str, Any]:
    value = getattr(resume, "content_json", "{}")
    if isinstance(value, dict):
        _validate_existing_content_shape(value)
        return value
    if not isinstance(value, str):
        raise ResumeStructureError(
            "resume_structure_invalid_source", "简历内容格式无效，无法安全分类。"
        )
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ResumeStructureError(
            "resume_structure_invalid_source", "简历内容格式无效，无法安全分类。"
        ) from exc
    if not isinstance(parsed, dict):
        raise ResumeStructureError(
            "resume_structure_invalid_source", "简历内容格式无效，无法安全分类。"
        )
    _validate_existing_content_shape(parsed)
    return parsed


def require_preview_source(resume: Any) -> str:
    if str(getattr(resume, "source", "")) != "upload":
        raise ResumeStructureError(
            "resume_structure_source_not_supported",
            "仅上传的 PDF 简历支持 AI 分类。",
        )
    raw_text = source_text(resume)
    if not raw_text.strip():
        raise ResumeStructureError(
            "resume_structure_empty_source",
            "未提取到可分类的简历文字，请重新上传文本型 PDF。",
        )
    if len(raw_text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ResumeStructureError(
            "resume_structure_input_too_large",
            "简历文字超过分类上限。",
            413,
        )
    return raw_text


def source_fingerprint(resume: Any) -> str:
    content_json = _json_value(getattr(resume, "content_json", "{}"))
    frozen = {
        "version": 1,
        "resume_id": int(getattr(resume, "id")),
        "source": str(getattr(resume, "source", "")),
        "source_file_path": str(getattr(resume, "source_file_path", "") or ""),
        "parse_status": str(getattr(resume, "parse_status", "") or ""),
        "parsed_data": str(getattr(resume, "parsed_data", "") or ""),
        "content_json": content_json,
    }
    canonical = json.dumps(
        frozen,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def decode_structured_output(payload: Any, raw_text: str) -> tuple[StructuredField, ...]:
    if not isinstance(payload, dict) or set(payload) != {"fields"}:
        raise ResumeStructureError(
            "resume_structure_invalid_output",
            "AI 返回的分类结果无效，请重试。",
            502,
        )
    try:
        return validate_structured_fields(payload["fields"], raw_text)
    except ResumeStructureError as exc:
        if exc.code == "resume_structure_output_too_large":
            raise ResumeStructureError(exc.code, exc.message, 502) from exc
        raise ResumeStructureError(
            "resume_structure_invalid_output",
            "AI 返回的分类结果无效，请重试。",
            502,
        ) from exc


def validate_structured_fields(raw_fields: Any, raw_text: str) -> tuple[StructuredField, ...]:
    if not isinstance(raw_fields, list):
        raise ResumeStructureError(
            "resume_structure_invalid_fields", "分类字段必须是数组。"
        )
    if not raw_fields:
        raise ResumeStructureError("resume_structure_no_fields", "请至少选择一个分类字段。")
    if len(raw_fields) > MAX_FIELDS:
        raise ResumeStructureError(
            "resume_structure_output_too_large", "分类字段数量超过上限。", 413
        )
    try:
        output_size = len(
            json.dumps(
                {"fields": raw_fields},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ResumeStructureError(
            "resume_structure_invalid_fields", "分类字段格式无效。"
        ) from exc
    if output_size > MAX_OUTPUT_BYTES:
        raise ResumeStructureError(
            "resume_structure_output_too_large", "分类结果超过大小上限。", 413
        )

    fields: list[StructuredField] = []
    seen: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict) or set(item) != {"path", "value", "evidence"}:
            raise ResumeStructureError(
                "resume_structure_invalid_fields", "分类字段包含未知或缺失属性。"
            )
        path = item["path"]
        value = item["value"]
        evidence = item["evidence"]
        if not all(isinstance(part, str) for part in (path, value, evidence)):
            raise ResumeStructureError(
                "resume_structure_invalid_fields", "分类字段必须使用字符串。"
            )
        if not _path_parts(path):
            raise ResumeStructureError(
                "resume_structure_invalid_fields", "分类字段路径不受支持。"
            )
        if path in seen:
            raise ResumeStructureError(
                "resume_structure_duplicate_fields", "分类字段路径不能重复。"
            )
        seen.add(path)
        if (
            len(value.encode("utf-8")) > MAX_STRING_BYTES
            or len(evidence.encode("utf-8")) > MAX_STRING_BYTES
        ):
            raise ResumeStructureError(
                "resume_structure_output_too_large", "分类字段文字超过上限。", 413
            )
        if not value.strip() or not evidence.strip():
            raise ResumeStructureError(
                "resume_structure_invalid_fields", "分类值和原文依据不能为空。"
            )
        if evidence not in raw_text:
            raise ResumeStructureError(
                "resume_structure_invalid_evidence", "原文依据不在当前简历中。"
            )
        if value not in evidence:
            raise ResumeStructureError(
                "resume_structure_unsupported_value", "分类值必须由原文依据直接支持。"
            )
        fields.append(StructuredField(path, value, evidence))

    _validate_contiguous_indexes(fields)
    return tuple(fields)


def apply_structured_fields(
    current_content: Mapping[str, Any],
    fields: Sequence[StructuredField],
) -> dict[str, Any]:
    result = deepcopy(dict(current_content))

    contact_fields = [field for field in fields if field.path.startswith("contact.")]
    if contact_fields:
        contact = result.get("contact")
        if not isinstance(contact, dict):
            contact = {} if _empty_scalar(contact) else None
        if contact is not None:
            for field in contact_fields:
                key = field.path.split(".")[1]
                if _empty_scalar(contact.get(key)):
                    contact[key] = field.value
            result["contact"] = contact

    career = result.get("career_intent")
    if not isinstance(career, dict):
        career = {} if _empty_scalar(career) else None
    if career is not None:
        for array_name in ("target_roles", "target_locations"):
            candidates = _array_values(fields, f"career_intent.{array_name}")
            if candidates and _empty_array(career.get(array_name)):
                career[array_name] = candidates
        if any(field.path.startswith("career_intent.") for field in fields):
            result["career_intent"] = career

    for section in ("education", "experience", "projects"):
        section_fields = [field for field in fields if field.path.startswith(f"{section}.")]
        if section_fields and _empty_array(result.get(section)):
            result[section] = _object_array(section, section_fields)

    skills = _array_values(fields, "skills")
    if skills and _empty_array(result.get("skills")):
        result["skills"] = skills
    return result


def fields_json(fields: Iterable[StructuredField]) -> list[dict[str, str]]:
    return [field.json() for field in fields]


def _path_parts(path: str) -> tuple[str, ...] | None:
    if path in _SCALAR_PATHS:
        return tuple(path.split("."))
    for pattern in (
        _CAREER_RE,
        _EDUCATION_RE,
        _EXPERIENCE_RE,
        _PROJECT_RE,
        _HIGHLIGHT_RE,
        _SKILL_RE,
    ):
        match = pattern.fullmatch(path)
        if match is not None:
            indexes = [int(part) for part in match.groups() if part.isdigit()]
            if all(index < MAX_ARRAY_ITEMS for index in indexes):
                return tuple(path.split("."))
    return None


def _validate_contiguous_indexes(fields: Sequence[StructuredField]) -> None:
    index_sets: dict[tuple[str, ...], set[int]] = {}
    for field in fields:
        parts = field.path.split(".")
        if parts[0] == "career_intent":
            index_sets.setdefault(tuple(parts[:2]), set()).add(int(parts[2]))
        elif parts[0] == "skills":
            index_sets.setdefault(("skills",), set()).add(int(parts[1]))
        elif parts[0] in {"education", "experience", "projects"}:
            index_sets.setdefault((parts[0],), set()).add(int(parts[1]))
            if len(parts) == 4:
                index_sets.setdefault((parts[0], parts[1], "highlights"), set()).add(
                    int(parts[3])
                )
    for indexes in index_sets.values():
        if indexes != set(range(len(indexes))):
            raise ResumeStructureError(
                "resume_structure_sparse_fields", "数组字段索引必须从 0 连续排列。"
            )


def _array_values(fields: Sequence[StructuredField], prefix: str) -> list[str]:
    values: list[tuple[int, str]] = []
    prefix_parts = prefix.split(".")
    for field in fields:
        parts = field.path.split(".")
        if parts[:-1] == prefix_parts and parts[-1].isdigit():
            values.append((int(parts[-1]), field.value))
    return [value for _, value in sorted(values)]


def _object_array(section: str, fields: Sequence[StructuredField]) -> list[dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for field in fields:
        parts = field.path.split(".")
        index = int(parts[1])
        item = by_index.setdefault(index, {})
        if len(parts) == 3:
            item[parts[2]] = field.value
        elif parts[2] == "highlights":
            highlights = item.setdefault("highlights", [])
            highlights.append((int(parts[3]), field.value))
    result: list[dict[str, Any]] = []
    for index in sorted(by_index):
        item = by_index[index]
        if isinstance(item.get("highlights"), list):
            item["highlights"] = [value for _, value in sorted(item["highlights"])]
        result.append(item)
    return result


def _empty_array(value: Any) -> bool:
    return value is None or (isinstance(value, list) and not value)


def _empty_scalar(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return value


def _validate_existing_content_shape(content: dict[str, Any]) -> None:
    def invalid() -> ResumeStructureError:
        return ResumeStructureError(
            "resume_structure_invalid_source", "简历内容格式无效，无法安全分类。"
        )

    career = content.get("career_intent")
    if career is not None:
        if not isinstance(career, dict):
            raise invalid()
        for name in ("target_roles", "target_locations"):
            if name in career and (
                not isinstance(career[name], list)
                or not all(isinstance(item, str) for item in career[name])
            ):
                raise invalid()
    contact = content.get("contact")
    if contact is not None and not isinstance(contact, dict):
        raise invalid()
    for name in ("education", "experience", "projects"):
        value = content.get(name)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, dict) for item in value)
        ):
            raise invalid()
    skills = content.get("skills")
    if skills is not None and (
        not isinstance(skills, list) or not all(isinstance(item, str) for item in skills)
    ):
        raise invalid()
    if "raw_text" in content and not isinstance(content["raw_text"], str):
        raise invalid()
