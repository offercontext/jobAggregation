from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.types import Message
from offerpilot.ai.workflows import parse_json_reply

ALLOWED_PATH_PREFIXES = (
    ("career_intent", "target_roles"),
    ("experience",),
    ("projects",),
    ("skills",),
    ("raw_text",),
)
EVIDENCE_SOURCES = {"resume", "evidence_bundle", "user_assertion"}
PROPOSAL_FIELDS = {"summary", "changes"}
CHANGE_FIELDS = {"id", "path", "before", "after", "rationale", "evidence_refs"}
EVIDENCE_REF_FIELDS = {"source", "path", "excerpt"}


class MaterialProposalModelError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        failure_category: str = "invalid_change_shape",
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_category = failure_category
        self.diagnostic = diagnostic or {}


class _MaterialProposalProviderError(Exception):
    pass


class _MaterialProposalFormatError(Exception):
    def __init__(self, failure_category: str) -> None:
        super().__init__(failure_category)
        self.failure_category = failure_category


@dataclass(frozen=True)
class ValidatedProposal:
    proposal: dict[str, Any]
    content: dict[str, Any]


MaterialProposalDiagnosticSink = Callable[[dict[str, Any]], None]


def validate_material_proposal(
    payload: dict[str, Any], source_snapshot: dict[str, Any]
) -> ValidatedProposal:
    if not isinstance(payload, dict):
        raise MaterialProposalModelError("model output must be a JSON object")
    if set(payload) != PROPOSAL_FIELDS:
        raise MaterialProposalModelError("top-level fields must be exactly summary and changes")
    summary = payload.get("summary")
    changes = payload.get("changes")
    if not isinstance(summary, str) or not summary.strip():
        raise MaterialProposalModelError("summary must be a non-empty string")
    if not isinstance(changes, list):
        raise MaterialProposalModelError("changes must be an array")

    resume = source_snapshot.get("resume")
    if not isinstance(resume, dict) or not isinstance(resume.get("content_json"), dict):
        raise MaterialProposalModelError("source resume content is unavailable")
    content = copy.deepcopy(resume["content_json"])
    normalized_changes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: list[tuple[str, ...]] = []
    for raw_change in changes:
        if not isinstance(raw_change, dict):
            raise MaterialProposalModelError("each change must be an object")
        if set(raw_change) != CHANGE_FIELDS:
            raise MaterialProposalModelError("change fields must be exactly the defined contract")
        change_id = raw_change.get("id")
        path = raw_change.get("path")
        before = raw_change.get("before")
        after = raw_change.get("after")
        rationale = raw_change.get("rationale")
        refs = raw_change.get("evidence_refs")
        if (
            not isinstance(change_id, str)
            or not isinstance(path, str)
            or not isinstance(before, str)
            or not isinstance(after, str)
            or not isinstance(rationale, str)
        ):
            raise MaterialProposalModelError("change fields must be strings")
        if not change_id or change_id in seen_ids:
            raise MaterialProposalModelError("change ids must be non-empty and unique")
        if not after.strip():
            raise MaterialProposalModelError("change after must be non-empty")
        pointer = _parse_allowed_pointer(path)
        if pointer in seen_paths or any(_overlaps(pointer, other) for other in seen_paths):
            raise MaterialProposalModelError("change paths must not overlap")
        current = _get_pointer(resume["content_json"], pointer)
        if not isinstance(current, str) or current != before:
            raise MaterialProposalModelError("change before does not match the frozen resume")
        if not isinstance(refs, list) or not refs:
            raise MaterialProposalModelError("each change needs evidence_refs")
        for ref in refs:
            _validate_evidence_ref(ref, source_snapshot, pointer)
        _set_pointer(content, pointer, after)
        seen_ids.add(change_id)
        seen_paths.append(pointer)
        normalized_changes.append(
            {
                "id": change_id,
                "path": path,
                "before": before,
                "after": after,
                "rationale": rationale,
                "evidence_refs": [dict(ref) for ref in refs],
            }
        )

    return ValidatedProposal(
        proposal={"summary": summary.strip(), "changes": normalized_changes},
        content=content,
    )


def generate_material_proposal(
    model: ChatModel,
    source_snapshot: dict[str, Any],
    instructions: str,
    *,
    on_diagnostic: MaterialProposalDiagnosticSink | None = None,
) -> ValidatedProposal:
    system = _material_proposal_system()
    prompt = _material_proposal_prompt(source_snapshot, instructions)
    repair_category = ""
    started_at = perf_counter()
    failure_categories: list[str] = []
    structure_summaries: list[dict[str, Any]] = []
    provider_request_id_hash = ""
    for attempt in range(2):
        payload_for_diagnostic: Any = None
        user = prompt if attempt == 0 else _material_proposal_repair_prompt(
            source_snapshot,
            instructions,
            repair_category,
        )
        try:
            try:
                assistant = model.complete(
                    [Message(role="system", content=system), Message(role="user", content=user)],
                    [],
                )
            except Exception as exc:
                raise _MaterialProposalProviderError() from exc
            provider_request_id_hash = _hash_provider_request_id(
                assistant.provider_blocks.get("request_id")
            )
            result = _parse_material_json(assistant.content)
            payload_for_diagnostic = result
            structure_summaries.append(_structure_summary(result))
            validated = validate_material_proposal(result, source_snapshot)
            diagnostic = _material_diagnostic(
                source_snapshot,
                payload=result,
                structure_summaries=structure_summaries,
                failure_categories=failure_categories,
                repair_attempted=attempt > 0,
                retry_count=attempt,
                duration_ms=_elapsed_ms(started_at),
                provider_request_id_hash=provider_request_id_hash,
            )
            _emit_diagnostic(on_diagnostic, diagnostic)
            return validated
        except _MaterialProposalProviderError as exc:
            failure_categories.append("provider_error")
            diagnostic = _material_diagnostic(
                source_snapshot,
                payload=None,
                structure_summaries=structure_summaries,
                failure_categories=failure_categories,
                repair_attempted=attempt > 0,
                retry_count=attempt,
                duration_ms=_elapsed_ms(started_at),
                provider_request_id_hash=provider_request_id_hash,
            )
            _emit_diagnostic(on_diagnostic, diagnostic)
            raise MaterialProposalModelError(
                "model provider request failed",
                failure_category="provider_error",
                diagnostic=diagnostic,
            ) from exc
        except _MaterialProposalFormatError as exc:
            repair_category = exc.failure_category
            failure_categories.append(repair_category)
        except MaterialProposalModelError as exc:
            repair_category = _model_failure_category(str(exc))
            failure_categories.append(repair_category)

        if len(failure_categories) > 2:
            failure_categories = failure_categories[:2]

    diagnostic = _material_diagnostic(
        source_snapshot,
        payload=payload_for_diagnostic,
        structure_summaries=structure_summaries,
        failure_categories=failure_categories,
        repair_attempted=True,
        retry_count=1,
        duration_ms=_elapsed_ms(started_at),
        provider_request_id_hash=provider_request_id_hash,
    )
    _emit_diagnostic(on_diagnostic, diagnostic)
    raise MaterialProposalModelError(
        "model output could not be verified",
        failure_category=failure_categories[-1] if failure_categories else "invalid_change_shape",
        diagnostic=diagnostic,
    )


def _parse_material_json(content: str) -> Any:
    try:
        return parse_json_reply(
            content,
            allow_fenced=False,
            reject_non_finite=True,
        )
    except Exception as exc:
        raise _MaterialProposalFormatError("invalid_json") from exc


def _model_failure_category(message: str) -> str:
    lowered = message.lower()
    if "evidence" in lowered or "excerpt" in lowered:
        return "invalid_evidence_reference"
    if "limit" in lowered or "maximum" in lowered:
        return "field_limit"
    if "source" in lowered or "before" in lowered or "does not exist" in lowered:
        return "source_validation"
    if "top-level" in lowered:
        return "invalid_top_level_shape"
    return "invalid_change_shape"


def _material_diagnostic(
    source_snapshot: dict[str, Any],
    *,
    payload: Any,
    structure_summaries: list[dict[str, Any]],
    failure_categories: list[str],
    repair_attempted: bool,
    retry_count: int,
    duration_ms: int,
    provider_request_id_hash: str,
) -> dict[str, Any]:
    safe_categories = [
        item
        for item in failure_categories[:2]
        if item in {
            "provider_error",
            "invalid_json",
            "invalid_top_level_shape",
            "invalid_change_shape",
            "invalid_evidence_reference",
            "source_validation",
            "field_limit",
        }
    ]
    return {
        "failure_category": safe_categories[-1] if safe_categories else None,
        "failure_categories": safe_categories,
        "structure_summaries": structure_summaries[:2],
        "evidence_counts": _evidence_counts(source_snapshot, payload),
        "repair_attempted": repair_attempted,
        "retry_count": max(0, min(retry_count, 1)),
        "duration_ms": max(0, duration_ms),
        "provider_request_id_hash": provider_request_id_hash,
    }


def _emit_diagnostic(
    sink: MaterialProposalDiagnosticSink | None,
    diagnostic: dict[str, Any],
) -> None:
    if sink is None:
        return
    try:
        sink(diagnostic)
    except Exception:
        return


def _structure_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "payload_type": _structure_value_type(payload),
            "top_level_keys": [],
            "fields": {},
        }
    raw_keys = [key for key in payload if isinstance(key, str)]
    fields: dict[str, Any] = {}
    for raw_key in sorted(raw_keys)[:32]:
        key = _safe_structure_key(raw_key)
        value = payload[raw_key]
        shape: dict[str, Any] = {"type": _structure_value_type(value)}
        if isinstance(value, list):
            shape["length"] = len(value)
            shape["item_types"] = [_structure_value_type(item) for item in value[:8]]
            shape["item_key_sets"] = [_structure_item_key_set(item) for item in value[:8]]
        elif isinstance(value, dict):
            shape["keys"] = sorted(
                _safe_structure_key(item) for item in value if isinstance(item, str)
            )[:32]
        fields[key] = shape
    return {
        "payload_type": "object",
        "top_level_keys": sorted(_safe_structure_key(key) for key in raw_keys)[:32],
        "fields": fields,
    }


def _structure_item_key_set(value: Any) -> list[str] | None:
    if not isinstance(value, dict):
        return None
    return sorted(
        _safe_structure_key(key) for key in value if isinstance(key, str)
    )[:32]


def _structure_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unsupported"


def _safe_structure_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./-]{1,64}", value):
        return value
    return "<unsafe-key>"


def _evidence_counts(source_snapshot: dict[str, Any], payload: Any) -> dict[str, Any]:
    bundle = source_snapshot.get("latest_evidence_bundle")
    assertions = source_snapshot.get("user_assertions")
    changes = payload.get("changes") if isinstance(payload, dict) else None
    change_count = len(changes) if isinstance(changes, list) else 0
    evidence_ref_count = 0
    changes_with_refs = 0
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            refs = change.get("evidence_refs")
            if isinstance(refs, list):
                evidence_ref_count += len(refs)
                if refs:
                    changes_with_refs += 1
    return {
        "available": {
            "resume_string_leaves": _count_string_leaves(
                source_snapshot.get("resume", {}).get("content_json")
                if isinstance(source_snapshot.get("resume"), dict)
                else None
            ),
            "evidence_bundle_present": isinstance(bundle, dict),
            "user_assertions": len(assertions) if isinstance(assertions, list) else 0,
        },
        "proposal": {
            "changes": change_count,
            "changes_with_evidence_refs": changes_with_refs,
            "evidence_refs": evidence_ref_count,
        },
    }


def _count_string_leaves(value: Any) -> int:
    count = 0
    pending = [value]
    while pending and count < 10000:
        current = pending.pop()
        if isinstance(current, str):
            count += 1
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return count


def _hash_provider_request_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _parse_allowed_pointer(path: str) -> tuple[str, ...]:
    if not path.startswith("/") or path == "/":
        raise MaterialProposalModelError("path is not allowed")
    parts = tuple(_decode_pointer_part(part) for part in path[1:].split("/"))
    if parts == ("raw_text",):
        return parts
    if len(parts) == 3 and parts[0] in {"experience", "projects"} and _is_canonical_index(parts[1]):
        if parts[2] != "highlights":
            raise MaterialProposalModelError("path is not allowed")
        raise MaterialProposalModelError("highlight index is required")
    if len(parts) == 4 and parts[0] in {"experience", "projects"}:
        if parts[2] != "highlights" or not _is_canonical_index(parts[1]) or not _is_canonical_index(parts[3]):
            raise MaterialProposalModelError("path is not allowed")
        return parts
    if len(parts) == 3 and parts[:2] == ("career_intent", "target_roles"):
        if not _is_canonical_index(parts[2]):
            raise MaterialProposalModelError("path is not allowed")
        return parts
    if len(parts) == 2 and parts[0] == "skills" and _is_canonical_index(parts[1]):
        return parts
    raise MaterialProposalModelError("path is not allowed")


def _decode_pointer_part(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "~":
            result.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise MaterialProposalModelError("path contains an invalid escape")
        result.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _is_canonical_index(value: str) -> bool:
    return value == "0" or (
        bool(value)
        and value[0] != "0"
        and all("0" <= character <= "9" for character in value)
    )


def _overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return left[: len(right)] == right or right[: len(left)] == left


def _get_pointer(root: Any, pointer: tuple[str, ...]) -> Any:
    value = root
    for part in pointer:
        if isinstance(value, dict):
            if part not in value:
                raise MaterialProposalModelError("path does not exist")
            value = value[part]
        elif isinstance(value, list) and _is_canonical_index(part):
            index = int(part)
            if index >= len(value):
                raise MaterialProposalModelError("path does not exist")
            value = value[index]
        else:
            raise MaterialProposalModelError("path does not exist")
    return value


def _set_pointer(root: Any, pointer: tuple[str, ...], value: str) -> None:
    parent = _get_pointer(root, pointer[:-1])
    key = pointer[-1]
    if isinstance(parent, dict) and key in parent:
        parent[key] = value
    elif isinstance(parent, list) and _is_canonical_index(key) and int(key) < len(parent):
        parent[int(key)] = value
    else:
        raise MaterialProposalModelError("path does not exist")


def _validate_evidence_ref(ref: Any, snapshot: dict[str, Any], change_pointer: tuple[str, ...]) -> None:
    if not isinstance(ref, dict):
        raise MaterialProposalModelError("evidence reference must be an object")
    if set(ref) != EVIDENCE_REF_FIELDS:
        raise MaterialProposalModelError("evidence reference fields must be exactly source, path, and excerpt")
    source = ref.get("source")
    path = ref.get("path")
    excerpt = ref.get("excerpt")
    if source not in EVIDENCE_SOURCES or not isinstance(path, str) or not isinstance(excerpt, str):
        raise MaterialProposalModelError("evidence reference is invalid")
    if not excerpt.strip():
        raise MaterialProposalModelError("evidence excerpt must be non-empty")
    if source == "resume":
        pointer = _parse_pointer(path)
        value = _get_pointer(snapshot["resume"]["content_json"], pointer)
    elif source == "user_assertion":
        pointer = _parse_assertion_pointer(path)
        value = _get_pointer(snapshot, pointer)
    else:
        bundle = snapshot.get("latest_evidence_bundle")
        if not isinstance(bundle, dict) or not isinstance(bundle.get("snapshot"), dict):
            raise MaterialProposalModelError("evidence bundle reference is unavailable")
        value = _get_pointer(bundle["snapshot"], _parse_evidence_bundle_pointer(path))
    if not isinstance(value, str) or value != excerpt:
        raise MaterialProposalModelError("evidence excerpt does not match the cited source")


def _parse_assertion_pointer(path: str) -> tuple[str, ...]:
    if not path.startswith("/user_assertions/"):
        raise MaterialProposalModelError("user assertion path is not allowed")
    parts = _parse_pointer(path)
    if len(parts) != 3 or parts[0] != "user_assertions" or not _is_canonical_index(parts[1]) or parts[2] != "text":
        raise MaterialProposalModelError("user assertion path is not allowed")
    return parts


def _parse_evidence_bundle_pointer(path: str) -> tuple[str, ...]:
    parts = _parse_pointer(path)
    if len(parts) < 3 or parts[:2] != ("resume", "content_json"):
        raise MaterialProposalModelError(
            "evidence bundle references must point to confirmed resume content"
        )
    return parts


def _parse_pointer(path: str) -> tuple[str, ...]:
    if not path.startswith("/") or path == "/":
        raise MaterialProposalModelError("evidence path is not allowed")
    return tuple(_decode_pointer_part(part) for part in path[1:].split("/"))


def _material_proposal_system() -> str:
    return """You are an evidence-gated resume editor. Return raw JSON only, never Markdown fences.
The top-level object must be exactly {"summary": string, "changes": array}.
Each change must contain string fields: id, path, before, after, rationale.
Each change must contain a non-empty evidence_refs array. Each evidence_refs item must be exactly
{"source": string, "path": string, "excerpt": string}.
Allowed change paths are only:
/raw_text
/skills/<index>
/career_intent/target_roles/<index>
/experience/<index>/highlights/<index>
/projects/<index>/highlights/<index>
The path must exist in the supplied editable-field inventory, and before must equal that field's
current value exactly. after must be a non-empty string.
Evidence rules:
- source=resume may cite only a relative path in resume content_json.
- source=evidence_bundle may cite only /resume/content_json/... in the confirmed bundle snapshot.
- source=user_assertion may cite only /user_assertions/<index>/text.
- Every excerpt must be a non-empty string exactly equal to the frozen snapshot value at its path.
The JD only determines rewrite direction; it is never candidate evidence. Do not invent numbers,
dates, employers, roles, technologies, responsibilities, or outcomes. User assertions are supplied
by the candidate but are not platform-verified facts.

Valid empty proposal:
{"summary":"No safe evidence-backed changes are available.","changes":[]}

Valid single-change proposal:
{"summary":"Make the existing API work more specific.","changes":[{"id":"change-1","path":"/experience/0/highlights/0","before":"Built APIs","after":"Built FastAPI APIs","rationale":"Clarify an existing candidate statement.","evidence_refs":[{"source":"resume","path":"/experience/0/highlights/0","excerpt":"Built APIs"}]}]}"""


def _material_proposal_prompt(source_snapshot: dict[str, Any], instructions: str) -> str:
    return (
        "Create a reviewable proposal from this frozen source snapshot. Empty changes are "
        "valid when no safe evidence-backed edit exists. Use only the allowed paths and "
        "the exact JSON shape described by the system message.\n"
        "Editable string fields and exact current before values:\n"
        f"{_editable_field_inventory(source_snapshot)}\n"
        f"User instructions: {instructions.strip()}\n"
        f"Frozen source snapshot:\n{json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True)}"
    )


def _material_proposal_repair_prompt(
    source_snapshot: dict[str, Any],
    instructions: str,
    failure_category: str,
) -> str:
    return (
        "Repair the previous material proposal attempt. The safe failure category is "
        f"{failure_category}. Return only raw JSON that follows the established contract; "
        "do not explain the repair, do not include Markdown fences, and do not repeat invalid "
        "field shapes. Use an empty changes array if no safe evidence-backed edit can be made.\n"
        "Editable string fields and exact current before values:\n"
        f"{_editable_field_inventory(source_snapshot)}\n"
        f"User instructions: {instructions.strip()}\n"
        f"Frozen source snapshot:\n{json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True)}"
    )


def _editable_field_inventory(source_snapshot: dict[str, Any]) -> str:
    resume = source_snapshot.get("resume")
    content = resume.get("content_json") if isinstance(resume, dict) else None
    if not isinstance(content, dict):
        return "(none)"

    fields: list[str] = []

    def add(path: str, value: Any) -> None:
        if isinstance(value, str):
            fields.append(f"{path} -> {value}")

    add("/raw_text", content.get("raw_text"))
    skills = content.get("skills")
    if isinstance(skills, list):
        for index, value in enumerate(skills):
            add(f"/skills/{index}", value)

    career_intent = content.get("career_intent")
    target_roles = career_intent.get("target_roles") if isinstance(career_intent, dict) else None
    if isinstance(target_roles, list):
        for index, value in enumerate(target_roles):
            add(f"/career_intent/target_roles/{index}", value)

    for section in ("experience", "projects"):
        entries = content.get(section)
        if not isinstance(entries, list):
            continue
        for item_index, entry in enumerate(entries):
            highlights = entry.get("highlights") if isinstance(entry, dict) else None
            if isinstance(highlights, list):
                for highlight_index, value in enumerate(highlights):
                    add(f"/{section}/{item_index}/highlights/{highlight_index}", value)

    return "\n".join(fields) if fields else "(none)"
