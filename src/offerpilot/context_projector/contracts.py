from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

from offerpilot.ai.tool_runtime.contracts import ProviderToolContract
from offerpilot.ai.types import Message, ToolCall

ContributorStatus = Literal["ready", "not_applicable", "disabled", "unavailable"]

CONTRIBUTOR_ORDER = (
    "static_policy",
    "current_scope",
    "active_control",
    "request_page_context",
    "request_attachments",
    "conversation_history",
    "current_request",
    "confirmed_memory",
    "knowledge_context",
    "older_conversation_summary",
)

_DIAGNOSTIC_KEYS = frozenset(
    {
        "present",
        "truncated",
        "item_count",
        "group_count",
        "chunk_count",
        "omitted_count",
        "fallback_all_tools",
        "legacy_orphan_count",
    }
)


class ProjectionError(RuntimeError):
    """A safe model-surface construction failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value: Any) -> bytes:
    """Serialize only JSON primitives with stable, cross-process semantics."""

    def validate(item: Any, path: str) -> None:
        if item is None or type(item) in {str, bool, int}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ProjectionError("non_canonical_number")
            return
        if type(item) is list or type(item) is tuple:
            for index, child in enumerate(item):
                validate(child, f"{path}[{index}]")
            return
        if type(item) is dict or isinstance(item, MappingProxyType):
            for key, child in item.items():
                if type(key) is not str:
                    raise ProjectionError("non_string_json_key")
                validate(child, f"{path}.{key}")
            return
        raise ProjectionError("non_primitive_source_value")

    validate(value, "$")
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectionError("canonicalization_failed") from exc
    return rendered.encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class SourceChunk:
    path: str
    ordinal: int
    total: int
    text: str
    truncated: bool
    original_bytes: int
    original_codepoints: int

    def __post_init__(self) -> None:
        if not self.path or self.ordinal < 1 or self.total < self.ordinal:
            raise ProjectionError("invalid_source_chunk")
        if self.original_bytes < 0 or self.original_codepoints < 0:
            raise ProjectionError("invalid_source_chunk")


@dataclass(frozen=True)
class FrozenSource:
    kind: str
    status: ContributorStatus
    revision_identity: str
    content_revision_fingerprint: str
    canonical_content: bytes = field(repr=False)
    chunks: tuple[SourceChunk, ...] = ()

    @classmethod
    def present(
        cls,
        *,
        kind: str,
        revision_identity: str,
        content: Any,
        chunks: tuple[SourceChunk, ...] = (),
    ) -> FrozenSource:
        raw = canonical_json(content)
        return cls(kind, "ready", revision_identity, sha256_hex(raw), raw, chunks)

    def __post_init__(self) -> None:
        if not self.kind or self.status not in {
            "ready",
            "not_applicable",
            "disabled",
            "unavailable",
        }:
            raise ProjectionError("invalid_source")
        if self.status == "ready":
            if not self.revision_identity or len(self.content_revision_fingerprint) != 64:
                raise ProjectionError("invalid_source_revision")
            if sha256_hex(self.canonical_content) != self.content_revision_fingerprint:
                raise ProjectionError("source_fingerprint_mismatch")


@dataclass(frozen=True)
class ContributorResult:
    name: str
    status: ContributorStatus
    messages: tuple[FrozenMessage, ...] = ()
    diagnostics: Mapping[str, bool | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in CONTRIBUTOR_ORDER:
            raise ProjectionError("unknown_contributor")
        if self.status not in {"ready", "not_applicable", "disabled", "unavailable"}:
            raise ProjectionError("invalid_contributor_status")
        if self.status != "ready" and self.messages:
            raise ProjectionError("inactive_contributor_has_content")
        clean: dict[str, bool | int] = {}
        for key, value in self.diagnostics.items():
            if key not in _DIAGNOSTIC_KEYS or type(value) not in {bool, int}:
                raise ProjectionError("invalid_diagnostic")
            if type(value) is int and (value < 0 or value > 1_000_000):
                raise ProjectionError("invalid_diagnostic")
            clean[key] = value
        object.__setattr__(self, "diagnostics", MappingProxyType(clean))


@dataclass(frozen=True)
class FrozenToolCall:
    id: str
    name: str
    args: str


@dataclass(frozen=True)
class FrozenMessage:
    role: str
    content: str = ""
    tool_calls: tuple[FrozenToolCall, ...] = ()
    tool_call_id: str = ""
    provider_blocks_json: bytes = b"{}"
    source_message_id: int = 0

    @classmethod
    def freeze(cls, message: Message, *, source_message_id: int = 0) -> FrozenMessage:
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise ProjectionError("invalid_message_role")
        blocks = canonical_json(message.provider_blocks)
        return cls(
            role=message.role,
            content=message.content,
            tool_calls=tuple(
                FrozenToolCall(call.id, call.name, call.args) for call in message.tool_calls
            ),
            tool_call_id=message.tool_call_id,
            provider_blocks_json=blocks,
            source_message_id=source_message_id,
        )

    def thaw(self) -> Message:
        blocks = json.loads(self.provider_blocks_json.decode("utf-8"))
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=[ToolCall(call.id, call.name, call.args) for call in self.tool_calls],
            tool_call_id=self.tool_call_id,
            provider_blocks=blocks,
        )

    def canonical_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            value["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            value["tool_calls"] = [
                {"id": call.id, "name": call.name, "args": call.args} for call in self.tool_calls
            ]
        blocks = json.loads(self.provider_blocks_json.decode("utf-8"))
        if blocks:
            value["provider_blocks"] = blocks
        return value


@dataclass(frozen=True)
class RuntimeSurfaceAudit:
    budget_policy_version: str
    contributor_statuses: tuple[tuple[str, ContributorStatus], ...]
    selected_history_group_ids: tuple[str, ...]
    selected_tool_names: tuple[str, ...]
    source_fingerprints: tuple[str, ...]
    estimated_input_units: int
    canonical_message_bytes: int
    canonical_tool_bytes: int
    truncated: bool
    source_records: tuple[RuntimeSourceAudit, ...] = ()
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeSourceAudit:
    kind: str
    revision_identity: str
    content_revision_fingerprint: str
    chunks: tuple[SourceChunk, ...]


@dataclass(frozen=True)
class FrozenModelSurface:
    model_call_id: str
    messages: tuple[FrozenMessage, ...]
    tools: tuple[ProviderToolContract, ...]
    runtime_surface_fingerprint: str
    provider_candidate_count: int
    audit: RuntimeSurfaceAudit = field(repr=False)
    provider_surface_build_identity: object | None = field(default=None, repr=False, compare=False)

    def thaw_messages(self) -> list[Message]:
        return [message.thaw() for message in self.messages]
