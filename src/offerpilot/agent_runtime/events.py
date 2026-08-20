from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import UUID

from offerpilot.agent_runtime.keyring import JournalKeyDomain

_DIGEST_CHUNK_BYTES = 4096


class _ByteDigest(Protocol):
    def update(self, data: bytes, /) -> None:
        ...


def iter_fixed_byte_chunks(
    value: str | bytes,
    *,
    chunk_size: int = _DIGEST_CHUNK_BYTES,
) -> Iterator[bytes]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk size must be a positive integer")
    if type(value) is str:
        encoded = value.encode("utf-8")
    elif type(value) is bytes:
        encoded = value
    else:
        raise TypeError("digest input must be text or bytes")

    for offset in range(0, len(encoded), chunk_size):
        chunk = encoded[offset : offset + chunk_size]
        if len(chunk) > chunk_size:
            raise RuntimeError("digest byte chunk exceeds configured limit")
        yield chunk


def update_digest_in_chunks(
    digest: _ByteDigest,
    value: str | bytes,
    *,
    budget_check: Callable[[], None] | None = None,
    chunk_size: int = _DIGEST_CHUNK_BYTES,
) -> None:
    for chunk in iter_fixed_byte_chunks(value, chunk_size=chunk_size):
        if budget_check is not None:
            budget_check()
        digest.update(chunk)
        if budget_check is not None:
            budget_check()


_CONTEXT_TYPES = {"workspace", "global", "application", "mode"}
_INTEGER_REFERENCE_TYPES = {
    "application",
    "application_event",
    "attachment",
    "conversation",
    "message",
    "snapshot",
}
_UUID_REFERENCE_TYPES = {
    "agent_run",
    "context_snapshot",
    "execution_segment",
    "model_call",
    "transport_run",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_FORBIDDEN_KEYS = {
    "answer",
    "api_key",
    "args",
    "attachment_name",
    "confirmation_token",
    "content",
    "context_ref",
    "exception",
    "idempotency_key",
    "jd_text",
    "message",
    "model_id",
    "output",
    "prompt",
    "provider_url",
    "resume_text",
    "secret",
    "text",
    "token",
    "tool_result",
    "url",
}
_FACT_KEYS: dict[str, set[str]] = {
    "run.started": {"agent_run_id", "origin_kind", "conversation_id", "context_type", "transport_mode"},
    "segment.started": {"request_kind", "transport_mode", "execution_path", "transport_run_id"},
    "segment.finished": {"outcome", "terminal_run_status"},
    "route.selected": {"route_kind", "route_reason_code"},
    "context.captured": {"snapshot_id", "snapshot_key", "manifest_digest", "logical_input_fingerprint"},
    "model.requested": {
        "snapshot_id",
        "provider_kind",
        "model_id_fingerprint",
        "supports_tools",
        "supports_json_schema",
        "stream",
        "tools_count",
        "response_format_kind",
    },
    "model.completed": {"assistant_kind", "tool_call_count", "finish_category"},
    "model.failed": {"failure_category", "provider_outcome"},
    "tool.proposed": {"tool_call_id", "tool_name", "tool_kind", "args_shape_digest", "proposal_outcome"},
    "tool.started": {"tool_call_id", "tool_name", "result_contract"},
    "tool.completed": {"tool_call_id", "tool_name", "outcome", "result_shape_digest"},
    "tool.failed": {"tool_call_id", "tool_name", "failure_category"},
    "approval.requested": {"tool_call_id", "confirmation_mode", "pending_identity_fingerprint"},
    "approval.decided": {
        "confirmation_attempt_id",
        "decision",
        "tool_call_id",
        "original_input_fingerprint",
        "decided_input_fingerprint",
    },
    "run.waiting_confirmation": {"tool_call_id"},
    "run.resumed": {"confirmation_attempt_id", "tool_call_id"},
    "assistant.persisted": {"message_id", "message_kind"},
    "run.completed": {"agent_run_id", "status", "failure_code"},
    "run.failed": {"agent_run_id", "status", "failure_code"},
    "run.cancelled": {"agent_run_id", "status", "failure_code"},
    "run.timed_out": {"agent_run_id", "status", "failure_code"},
}
_TELEMETRY_KEYS = {"duration_ms", "retry_count", "item_count", "byte_count"}
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_CANONICAL_STRING_BYTES = 1_048_576
_TOOL_NAMES = {
    "add_note",
    "compare_offers",
    "create_application",
    "create_application_event",
    "create_application_submission_snapshot",
    "delete_application_event",
    "delete_note",
    "get_application",
    "get_application_event",
    "get_jd_analysis",
    "get_offer",
    "get_resume",
    "list_application_events",
    "list_applications",
    "list_jd_analyses",
    "list_notes",
    "list_offers",
    "list_resume_matches",
    "list_resumes",
    "record_application_outcome",
    "resume_rewrite_highlight",
    "resume_update_career_intent",
    "save_application_jd_version",
    "save_offer_assessment",
    "update_application_event",
    "update_application_status",
    "update_note",
    "update_offer",
}
_FAILURE_CATEGORIES = {
    "cancelled",
    "invalid_response",
    "network_error",
    "provider_error",
    "response_lost",
    "timeout",
    "tool_error",
    "unknown",
}
_EVENT_ENUM_VALUES: dict[str, dict[str, set[object]]] = {
    "run.started": {
        "origin_kind": {"user_message", "pilot_action", "system"},
        "context_type": {"workspace", "global", "application", "mode", "unknown"},
        "transport_mode": {"sync", "stream"},
    },
    "segment.started": {
        "request_kind": {"initial", "confirmation", "pending_replay"},
        "transport_mode": {"sync", "stream"},
        "execution_path": {
            "model_turn",
            "deterministic_action",
            "agent_resume",
            "deterministic_confirmation",
        },
    },
    "segment.finished": {
        "outcome": {"completed", "suspended", "failed", "cancelled", "timed_out", "noop"},
        "terminal_run_status": {"completed", "failed", "cancelled", "timed_out", None},
    },
    "route.selected": {
        "route_kind": {"model", "deterministic"},
        "route_reason_code": {
            "model_default",
            "deterministic_action_match",
            "pending_action_replay",
        },
    },
    "model.requested": {
        "provider_kind": {"openai", "openai_compatible", "litellm_proxy", "anthropic"},
        "response_format_kind": {"text", "json_object", "json_schema", "unknown"},
    },
    "model.completed": {
        "assistant_kind": {"empty", "mixed", "text", "tool_calls"},
        "finish_category": {"content_filter", "length", "stop", "tool_calls", "unknown"},
    },
    "model.failed": {
        "failure_category": set(_FAILURE_CATEGORIES),
        "provider_outcome": {"cancelled", "error", "network_error", "timeout", "unknown"},
    },
    "tool.proposed": {
        "tool_kind": {"read", "write"},
        "proposal_outcome": {"confirmation_required", "execution_allowed"},
    },
    "tool.started": {"result_contract": {"legacy_string_v1"}},
    "tool.completed": {"outcome": {"completed"}},
    "tool.failed": {"failure_category": set(_FAILURE_CATEGORIES)},
    "approval.requested": {"confirmation_mode": {"required"}},
    "approval.decided": {"decision": {"approved", "edited", "rejected"}},
    "assistant.persisted": {"message_kind": {"assistant", "tool"}},
    "run.completed": {"status": {"completed"}},
    "run.failed": {"status": {"failed"}},
    "run.cancelled": {"status": {"cancelled"}},
    "run.timed_out": {"status": {"timed_out"}},
}


class JournalEventValidationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedContextIdentity:
    context_type: Literal["workspace", "global", "application", "mode", "unknown"]
    entity_id: int | str | None
    ref_fingerprint: str | None


@dataclass(frozen=True)
class ContextManifestInput:
    conversation_message_ids: tuple[int, ...]
    tool_names: tuple[str, ...]
    attachment_refs: tuple[Mapping[str, object], ...]
    domain_source_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class PreparedSnapshot:
    manifest_schema_version: int
    manifest_json: str
    manifest_digest: str
    logical_input_fingerprint: str
    fingerprint_key_id: str


@dataclass(frozen=True)
class EventDraft:
    event_type: str
    schema_version: int
    execution_segment_id: str
    model_step: int | None
    model_call_id: str | None
    source_ref_type: str | None
    source_ref_id: str | None
    fingerprint_key_id: str | None
    payload_json: str
    payload_digest: str
    fact_digest: str
    dedupe_key: str


def _check_budget(budget_check: Callable[[], None] | None) -> None:
    if budget_check is not None:
        budget_check()


def _canonical_value(
    value: object,
    *,
    active: set[int],
    depth: int,
    nodes: list[int],
    budget_check: Callable[[], None] | None,
) -> object:
    if budget_check is not None:
        budget_check()
    nodes[0] += 1
    if nodes[0] > 100_000 or depth > 128:
        raise JournalEventValidationError("journal value exceeds canonicalization budget")
    if type(value) is str:
        encoded_bytes = 0
        for encoded in iter_fixed_byte_chunks(value):
            if budget_check is not None:
                budget_check()
            if budget_check is not None:
                budget_check()
            encoded_bytes += len(encoded)
            if encoded_bytes > _MAX_CANONICAL_STRING_BYTES:
                raise JournalEventValidationError(
                    "journal string exceeds canonicalization budget"
                )
        return value
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise JournalEventValidationError("non-finite number is not valid journal JSON")
        return value
    if type(value) not in {dict, list, tuple}:
        raise JournalEventValidationError("unsupported journal JSON value")
    identity = id(value)
    if identity in active:
        raise JournalEventValidationError("cyclic journal JSON value")
    active.add(identity)
    try:
        if type(value) is dict:
            object_value = cast(dict[object, object], value)
            for key in object_value:
                if budget_check is not None:
                    budget_check()
                if type(key) is not str:
                    raise JournalEventValidationError(
                        "journal JSON object keys must be plain strings"
                    )
            string_object = cast(dict[str, object], object_value)
            result: dict[str, object] = {}
            for key in sorted(string_object):
                result[key] = _canonical_value(
                    string_object[key],
                    active=active,
                    depth=depth + 1,
                    nodes=nodes,
                    budget_check=budget_check,
                )
            return result
        sequence_value = cast(list[object] | tuple[object, ...], value)
        return [
            _canonical_value(
                item,
                active=active,
                depth=depth + 1,
                nodes=nodes,
                budget_check=budget_check,
            )
            for item in sequence_value
        ]
    finally:
        active.remove(identity)


def canonical_json(
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    normalized = _canonical_value(
        value,
        active=set(),
        depth=0,
        nodes=[0],
        budget_check=budget_check,
    )
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    _check_budget(budget_check)
    chunks: list[str] = []
    for chunk in encoder.iterencode(normalized):
        _check_budget(budget_check)
        chunks.append(chunk)
    _check_budget(budget_check)
    rendered = "".join(chunks)
    _check_budget(budget_check)
    return rendered


def _canonical_uuid(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = str(UUID(value))
    except ValueError:
        return None
    return parsed if parsed == value else None


def _hmac_fingerprint(
    key: JournalKeyDomain,
    domain: bytes,
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    payload = canonical_json(value, budget_check=budget_check)
    _check_budget(budget_check)
    digest = hmac.new(key.secret, digestmod=hashlib.sha256)
    _check_budget(budget_check)
    update_digest_in_chunks(digest, domain, budget_check=budget_check)
    update_digest_in_chunks(digest, payload, budget_check=budget_check)
    _check_budget(budget_check)
    fingerprint = digest.hexdigest()
    _check_budget(budget_check)
    return fingerprint


def pending_identity_fingerprint(
    key: JournalKeyDomain,
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    return _hmac_fingerprint(
        key,
        b"offerpilot-agent-pending-v1\0",
        value,
        budget_check=budget_check,
    )


def model_id_fingerprint(
    key: JournalKeyDomain,
    value: str,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    return _hmac_fingerprint(
        key,
        b"offerpilot-agent-model-v1\0",
        value,
        budget_check=budget_check,
    )


def normalize_context_identity(
    context_type: object,
    context_ref: object,
    *,
    application_visible: Callable[[int], bool],
    key: JournalKeyDomain,
    budget_check: Callable[[], None] | None = None,
) -> NormalizedContextIdentity:
    if budget_check is not None:
        budget_check()
    normalized_type = context_type.strip().lower() if type(context_type) is str else "unknown"
    if normalized_type not in _CONTEXT_TYPES:
        return NormalizedContextIdentity(
            context_type="unknown",
            entity_id=None,
            ref_fingerprint=_hmac_fingerprint(
                key,
                b"offerpilot-agent-context-v1\0",
                [normalized_type, context_ref if type(context_ref) is str else None],
                budget_check=budget_check,
            ),
        )
    if normalized_type in {"workspace", "global"}:
        return NormalizedContextIdentity(normalized_type, None, None)  # type: ignore[arg-type]
    if normalized_type == "application":
        parsed: int | None = None
        if type(context_ref) is str and re.fullmatch(r"[1-9][0-9]{0,17}", context_ref):
            parsed = int(context_ref)
        if parsed is not None:
            visible = application_visible(parsed)
            if budget_check is not None:
                budget_check()
            if visible:
                return NormalizedContextIdentity("application", parsed, None)
        return NormalizedContextIdentity("application", None, None)
    return NormalizedContextIdentity(
        "mode",
        None,
        _hmac_fingerprint(
            key,
            b"offerpilot-agent-context-v1\0",
            ["mode", context_ref if type(context_ref) is str else None],
            budget_check=budget_check,
        ),
    )


def normalize_source_reference(source_type: object, source_id: object) -> tuple[str | None, str | None]:
    if type(source_type) is not str:
        return None, None
    if source_type in _INTEGER_REFERENCE_TYPES:
        if type(source_id) is int and source_id > 0:
            return source_type, str(source_id)
        return None, None
    if source_type in _UUID_REFERENCE_TYPES:
        normalized = _canonical_uuid(source_id)
        return (source_type, normalized) if normalized is not None else (None, None)
    if source_type == "tool_call":
        if type(source_id) is str and _SAFE_NAME.fullmatch(source_id):
            return source_type, source_id
        return None, None
    if source_type == "operation":
        if type(source_id) is str and _OPERATION_ID.fullmatch(source_id):
            return source_type, source_id
        return None, None
    return None, None


def _ordered_digest(
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    canonical = canonical_json(value, budget_check=budget_check)
    _check_budget(budget_check)
    encoded = canonical.encode("utf-8")
    _check_budget(budget_check)
    digest = hashlib.sha256()
    update_digest_in_chunks(digest, encoded, budget_check=budget_check)
    _check_budget(budget_check)
    result = digest.hexdigest()
    _check_budget(budget_check)
    return "sha256:" + result


def _normalize_manifest_ref(
    value: Mapping[str, object],
    *,
    budget_check: Callable[[], None] | None = None,
) -> dict[str, object]:
    if budget_check is not None:
        budget_check()
    if type(value) is not dict or not set(value).issubset({"id", "revision", "kind", "path_category"}):
        raise JournalEventValidationError("invalid manifest reference shape")
    result: dict[str, object] = {}
    identifier = value.get("id")
    if type(identifier) is int and identifier > 0:
        result["id"] = identifier
    elif (normalized := _canonical_uuid(identifier)) is not None:
        result["id"] = normalized
    else:
        raise JournalEventValidationError("invalid manifest reference id")
    revision = value.get("revision")
    if revision is not None:
        if type(revision) is not int or revision < 0:
            raise JournalEventValidationError("invalid manifest revision")
        result["revision"] = revision
    for field in ("kind", "path_category"):
        item = value.get(field)
        if item is not None:
            if type(item) is not str or _SAFE_NAME.fullmatch(item) is None:
                raise JournalEventValidationError("invalid manifest label")
            result[field] = item
    return result


def prepare_context_snapshot(
    logical_input: object,
    manifest: ContextManifestInput,
    *,
    key: JournalKeyDomain,
    budget_check: Callable[[], None] | None = None,
) -> PreparedSnapshot:
    messages = manifest.conversation_message_ids
    for message_id in messages:
        _check_budget(budget_check)
        if type(message_id) is not int or message_id <= 0:
            raise JournalEventValidationError("invalid conversation message id")
    _check_budget(budget_check)
    tools = manifest.tool_names
    for tool_name in tools:
        _check_budget(budget_check)
        if type(tool_name) is not str or _SAFE_NAME.fullmatch(tool_name) is None:
            raise JournalEventValidationError("invalid tool name")
    _check_budget(budget_check)
    attachments = [
        _normalize_manifest_ref(item, budget_check=budget_check)
        for item in manifest.attachment_refs
    ]
    _check_budget(budget_check)
    sources = [
        _normalize_manifest_ref(item, budget_check=budget_check)
        for item in manifest.domain_source_refs
    ]
    _check_budget(budget_check)
    manifest_payload = {
        "manifest_schema_version": 1,
        "conversation": {
            "message_count": len(messages),
            "first_message_id": messages[0] if messages else None,
            "last_message_id": messages[-1] if messages else None,
            "ordered_ids_digest": _ordered_digest(messages, budget_check=budget_check),
            "included_recent_message_ids": list(messages[-16:]),
        },
        "tools": {
            "count": len(tools),
            "ordered_names_digest": _ordered_digest(tools, budget_check=budget_check),
            "included_names": list(tools[:32]),
        },
        "attachments": {
            "count": len(attachments),
            "ordered_refs_digest": _ordered_digest(
                attachments,
                budget_check=budget_check,
            ),
            "included_refs": attachments[:16],
        },
        "domain_sources": {
            "count": len(sources),
            "ordered_refs_digest": _ordered_digest(sources, budget_check=budget_check),
            "included_refs": sources[:32],
        },
    }
    _check_budget(budget_check)
    manifest_json = canonical_json(manifest_payload, budget_check=budget_check)
    _check_budget(budget_check)
    manifest_bytes = manifest_json.encode("utf-8")
    _check_budget(budget_check)
    if len(manifest_bytes) > 16_384:
        raise JournalEventValidationError("manifest exceeds 16 KiB")
    _check_budget(budget_check)
    logical_json = canonical_json(logical_input, budget_check=budget_check)
    _check_budget(budget_check)
    logical_digest = hmac.new(key.secret, digestmod=hashlib.sha256)
    _check_budget(budget_check)
    update_digest_in_chunks(
        logical_digest,
        b"offerpilot-agent-input-v1\0",
        budget_check=budget_check,
    )
    update_digest_in_chunks(logical_digest, logical_json, budget_check=budget_check)
    _check_budget(budget_check)
    logical_input_fingerprint = logical_digest.hexdigest()
    _check_budget(budget_check)
    manifest_digest = hashlib.sha256()
    update_digest_in_chunks(manifest_digest, manifest_bytes, budget_check=budget_check)
    _check_budget(budget_check)
    manifest_fingerprint = manifest_digest.hexdigest()
    _check_budget(budget_check)
    prepared = PreparedSnapshot(
        manifest_schema_version=1,
        manifest_json=manifest_json,
        manifest_digest=manifest_fingerprint,
        logical_input_fingerprint=logical_input_fingerprint,
        fingerprint_key_id=key.key_id,
    )
    _check_budget(budget_check)
    return prepared


def validate_context_manifest_json(manifest_json: str) -> dict[str, object]:
    """Return a canonical, privacy-bounded v1/v2 manifest or raise a safe error."""

    try:
        manifest = json.loads(manifest_json)
    except (json.JSONDecodeError, TypeError):
        raise JournalEventValidationError("invalid context manifest") from None
    if type(manifest) is dict and manifest.get("manifest_schema_version") == 2:
        from offerpilot.context_projector.manifest import (
            ManifestV2ValidationError,
            validate_surface_manifest_v2,
        )

        try:
            return cast(dict[str, object], validate_surface_manifest_v2(manifest_json))
        except ManifestV2ValidationError as exc:
            raise JournalEventValidationError(str(exc)) from None
    if type(manifest) is not dict or set(manifest) != {
        "manifest_schema_version",
        "conversation",
        "tools",
        "attachments",
        "domain_sources",
    }:
        raise JournalEventValidationError("invalid context manifest shape")
    if manifest["manifest_schema_version"] != 1 or canonical_json(manifest) != manifest_json:
        raise JournalEventValidationError("context manifest is not canonical")
    if len(manifest_json.encode("utf-8")) > 16_384:
        raise JournalEventValidationError("manifest exceeds 16 KiB")

    conversation = manifest["conversation"]
    if type(conversation) is not dict or set(conversation) != {
        "message_count",
        "first_message_id",
        "last_message_id",
        "ordered_ids_digest",
        "included_recent_message_ids",
    }:
        raise JournalEventValidationError("invalid conversation manifest")
    message_count = conversation["message_count"]
    included_messages = conversation["included_recent_message_ids"]
    if type(message_count) is not int or message_count < 0 or type(included_messages) is not list:
        raise JournalEventValidationError("invalid conversation manifest count")
    if len(included_messages) > 16 or len(included_messages) > message_count:
        raise JournalEventValidationError("invalid conversation manifest sample")
    if any(type(item) is not int or item <= 0 for item in included_messages):
        raise JournalEventValidationError("invalid conversation manifest message")
    for field in ("first_message_id", "last_message_id"):
        value = conversation[field]
        if value is not None and (type(value) is not int or value <= 0):
            raise JournalEventValidationError("invalid conversation manifest boundary")
    if message_count == 0 and (
        conversation["first_message_id"] is not None
        or conversation["last_message_id"] is not None
        or included_messages
    ):
        raise JournalEventValidationError("invalid empty conversation manifest")
    if message_count > 0 and (
        conversation["first_message_id"] is None or conversation["last_message_id"] is None
    ):
        raise JournalEventValidationError("missing conversation manifest boundary")
    _validate_ordered_digest(conversation["ordered_ids_digest"])

    tools = manifest["tools"]
    if type(tools) is not dict or set(tools) != {
        "count",
        "ordered_names_digest",
        "included_names",
    }:
        raise JournalEventValidationError("invalid tools manifest")
    included_names = tools["included_names"]
    _validate_manifest_count(tools["count"], included_names, maximum=32)
    if any(type(item) is not str or _SAFE_NAME.fullmatch(item) is None for item in included_names):
        raise JournalEventValidationError("invalid tools manifest name")
    _validate_ordered_digest(tools["ordered_names_digest"])

    for section_name, maximum in (("attachments", 16), ("domain_sources", 32)):
        section = manifest[section_name]
        if type(section) is not dict or set(section) != {
            "count",
            "ordered_refs_digest",
            "included_refs",
        }:
            raise JournalEventValidationError("invalid reference manifest")
        included_refs = section["included_refs"]
        _validate_manifest_count(section["count"], included_refs, maximum=maximum)
        if any(type(item) is not dict or _normalize_manifest_ref(item) != item for item in included_refs):
            raise JournalEventValidationError("invalid reference manifest item")
        _validate_ordered_digest(section["ordered_refs_digest"])
    return cast(dict[str, object], manifest)


def _validate_manifest_count(count: object, included: object, *, maximum: int) -> None:
    if type(count) is not int or count < 0 or type(included) is not list:
        raise JournalEventValidationError("invalid manifest count")
    if len(included) > maximum or len(included) > count:
        raise JournalEventValidationError("invalid manifest sample size")


def _validate_ordered_digest(value: object) -> None:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise JournalEventValidationError("invalid ordered manifest digest")


def _dedupe_key(
    event_type: str,
    segment_id: str,
    model_call_id: str | None,
    facts: dict[str, object],
) -> str:
    if event_type == "run.started" or event_type.startswith("run.") and event_type in {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.timed_out",
    }:
        identity = facts.get("agent_run_id")
    elif event_type in {"segment.started", "segment.finished", "route.selected"}:
        identity = segment_id
    elif event_type == "context.captured":
        identity = facts.get("snapshot_id")
    elif event_type.startswith("model."):
        identity = model_call_id
    elif event_type == "tool.proposed" or event_type == "run.waiting_confirmation":
        identity = facts.get("tool_call_id")
    elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
        identity = f"{segment_id}:{facts.get('tool_call_id')}"
    elif event_type == "approval.requested":
        identity = facts.get("tool_call_id")
    elif event_type in {"approval.decided", "run.resumed"}:
        identity = facts.get("confirmation_attempt_id")
    elif event_type == "assistant.persisted":
        identity = facts.get("message_id")
    else:
        identity = None
    if identity is None or type(identity) not in {str, int}:
        raise JournalEventValidationError("event is missing its stable dedupe identity")
    return f"{event_type}:{identity}"


def prepare_event(
    *,
    event_type: str,
    execution_segment_id: str,
    facts: dict[str, object],
    telemetry: dict[str, object] | None = None,
    model_step: int | None = None,
    model_call_id: str | None = None,
    source_ref_type: str | None = None,
    source_ref_id: object = None,
    fingerprint_key_id: str | None = None,
    budget_check: Callable[[], None] | None = None,
) -> EventDraft:
    if budget_check is not None:
        budget_check()
    if event_type not in _FACT_KEYS or type(facts) is not dict:
        raise JournalEventValidationError("unsupported journal event type")
    if set(facts) != _FACT_KEYS[event_type] or set(facts) & _FORBIDDEN_KEYS:
        raise JournalEventValidationError("journal event contains unknown or sensitive facts")
    telemetry = {} if telemetry is None else telemetry
    if type(telemetry) is not dict or not set(telemetry).issubset(_TELEMETRY_KEYS):
        raise JournalEventValidationError("journal event contains unknown telemetry")
    for field, value in facts.items():
        if budget_check is not None:
            budget_check()
        _validate_fact_value(event_type, field, value)
    for value in telemetry.values():
        if budget_check is not None:
            budget_check()
        if type(value) not in {int, float}:
            raise JournalEventValidationError("invalid journal telemetry value")
        numeric_value = cast(int | float, value)
        if not math.isfinite(numeric_value) or numeric_value < 0:
            raise JournalEventValidationError("invalid journal telemetry value")
    segment = _canonical_uuid(execution_segment_id)
    if segment is None:
        raise JournalEventValidationError("invalid execution segment id")
    if model_step is not None and (type(model_step) is not int or model_step <= 0):
        raise JournalEventValidationError("invalid model step")
    call = _canonical_uuid(model_call_id) if model_call_id is not None else None
    if model_call_id is not None and call is None:
        raise JournalEventValidationError("invalid model call id")
    if fingerprint_key_id is not None and _canonical_uuid(fingerprint_key_id) is None:
        raise JournalEventValidationError("invalid fingerprint key id")
    contains_hmac = any(field.endswith("_fingerprint") for field in facts)
    if contains_hmac != (fingerprint_key_id is not None):
        raise JournalEventValidationError("journal HMAC facts require exactly one key domain id")
    if event_type == "segment.started":
        transport_mode = facts["transport_mode"]
        transport_run_id = facts["transport_run_id"]
        if transport_mode == "sync" and transport_run_id is not None:
            raise JournalEventValidationError("sync segment cannot carry transport run id")
        if transport_mode == "stream" and _canonical_uuid(transport_run_id) is None:
            raise JournalEventValidationError("stream segment requires transport run id")
    normalized_type: str | None = None
    normalized_id: str | None = None
    if source_ref_type is not None or source_ref_id is not None:
        normalized_type, normalized_id = normalize_source_reference(source_ref_type, source_ref_id)
        if normalized_type is None:
            raise JournalEventValidationError("invalid source reference")
    _check_budget(budget_check)
    payload = {"facts": facts, "telemetry": telemetry}
    _check_budget(budget_check)
    payload_json = canonical_json(payload, budget_check=budget_check)
    _check_budget(budget_check)
    payload_bytes = payload_json.encode("utf-8")
    _check_budget(budget_check)
    if len(payload_bytes) > 4096:
        raise JournalEventValidationError("event payload exceeds 4 KiB")
    _check_budget(budget_check)
    fact_envelope = {
        "event_type": event_type,
        "schema_version": 1,
        "execution_segment_id": segment,
        "model_step": model_step,
        "model_call_id": call,
        "source_ref_type": normalized_type,
        "source_ref_id": normalized_id,
        "facts": facts,
    }
    _check_budget(budget_check)
    fact_bytes = canonical_json(fact_envelope, budget_check=budget_check).encode("utf-8")
    _check_budget(budget_check)
    payload_digest = hashlib.sha256()
    update_digest_in_chunks(payload_digest, payload_bytes, budget_check=budget_check)
    _check_budget(budget_check)
    payload_fingerprint = payload_digest.hexdigest()
    _check_budget(budget_check)
    fact_digest = hashlib.sha256()
    update_digest_in_chunks(fact_digest, fact_bytes, budget_check=budget_check)
    _check_budget(budget_check)
    fact_fingerprint = fact_digest.hexdigest()
    _check_budget(budget_check)
    draft = EventDraft(
        event_type=event_type,
        schema_version=1,
        execution_segment_id=segment,
        model_step=model_step,
        model_call_id=call,
        source_ref_type=normalized_type,
        source_ref_id=normalized_id,
        fingerprint_key_id=fingerprint_key_id,
        payload_json=payload_json,
        payload_digest=payload_fingerprint,
        fact_digest=fact_fingerprint,
        dedupe_key=_dedupe_key(event_type, segment, call, facts),
    )
    _check_budget(budget_check)
    return draft


def validate_event_draft(draft: EventDraft) -> EventDraft:
    """Revalidate a prepared event before it crosses the Repository boundary."""

    try:
        payload = json.loads(draft.payload_json)
        if type(payload) is not dict or set(payload) != {"facts", "telemetry"}:
            raise JournalEventValidationError("invalid prepared journal event payload")
        facts = payload["facts"]
        telemetry = payload["telemetry"]
        if type(facts) is not dict or type(telemetry) is not dict:
            raise JournalEventValidationError("invalid prepared journal event payload")
        source_ref_id: object = draft.source_ref_id
        if draft.source_ref_type in _INTEGER_REFERENCE_TYPES:
            if type(source_ref_id) is not str or not source_ref_id.isascii() or not source_ref_id.isdigit():
                raise JournalEventValidationError("invalid prepared integer reference")
            source_ref_id = int(source_ref_id)
        rebuilt = prepare_event(
            event_type=draft.event_type,
            execution_segment_id=draft.execution_segment_id,
            facts=facts,
            telemetry=telemetry,
            model_step=draft.model_step,
            model_call_id=draft.model_call_id,
            source_ref_type=draft.source_ref_type,
            source_ref_id=source_ref_id,
            fingerprint_key_id=draft.fingerprint_key_id,
        )
    except (JournalEventValidationError, KeyError, TypeError, ValueError):
        raise JournalEventValidationError("invalid prepared journal event") from None
    if rebuilt != draft:
        raise JournalEventValidationError("prepared journal event fields differ")
    return draft


def _validate_fact_value(event_type: str, field: str, value: object) -> None:
    enum_values = _EVENT_ENUM_VALUES.get(event_type, {}).get(field)
    if enum_values is not None:
        if type(value) not in {str, type(None)} or value not in enum_values:
            raise JournalEventValidationError("invalid journal enum fact")
        return
    if field in {"agent_run_id", "snapshot_id"}:
        if _canonical_uuid(value) is None:
            raise JournalEventValidationError("invalid journal UUID fact")
        return
    if field == "transport_run_id":
        if value is not None and _canonical_uuid(value) is None:
            raise JournalEventValidationError("invalid journal transport UUID fact")
        return
    if field == "confirmation_attempt_id":
        if _canonical_uuid(value) is None:
            raise JournalEventValidationError("invalid confirmation attempt id")
        return
    if field in {"conversation_id", "message_id"}:
        if type(value) is not int or value <= 0:
            raise JournalEventValidationError("invalid journal database id fact")
        return
    if field in {"tool_call_count", "tools_count"}:
        if type(value) is not int or value < 0:
            raise JournalEventValidationError("invalid journal count fact")
        return
    if field in {"supports_tools", "supports_json_schema", "stream"}:
        if type(value) is not bool:
            raise JournalEventValidationError("invalid journal boolean fact")
        return
    if field in {"args_shape_digest", "result_shape_digest"}:
        if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise JournalEventValidationError("invalid journal shape digest fact")
        return
    if field.endswith("_digest") or field.endswith("_fingerprint"):
        if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
            raise JournalEventValidationError("invalid journal digest fact")
        return
    if field == "snapshot_key":
        if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise JournalEventValidationError("invalid snapshot key")
        return
    if field == "tool_name":
        if value not in _TOOL_NAMES:
            raise JournalEventValidationError("unknown tool name")
        return
    if field == "tool_call_id":
        if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise JournalEventValidationError("invalid tool call id")
        return
    if field in {"failure_category", "failure_code"}:
        if value is not None and value not in _FAILURE_CATEGORIES:
            raise JournalEventValidationError("invalid failure category")
        return
    raise JournalEventValidationError("journal fact has no strict validator")
