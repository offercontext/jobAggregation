from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from offerpilot.agent_runtime.events import update_digest_in_chunks
from offerpilot.context_projector.contracts import CONTRIBUTOR_ORDER, RuntimeSurfaceAudit
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_NAMES

MANIFEST_SCHEMA_VERSION = 2
MANIFEST_BYTE_CAP = 65_536
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STATUSES = {"ready", "not_applicable", "disabled", "unavailable"}
MANIFEST_SIGNAL_VALUES = (
    "trusted_page",
    "trusted_attachment",
    "lexical_application",
    "lexical_event",
    "lexical_note",
    "lexical_offer",
    "lexical_resume",
    "lexical_jd",
    "fallback_all_tools",
    "structural_workspace",
    "structural_application",
    "structural_calendar",
    "structural_notes",
    "structural_offers",
    "structural_resumes",
    "attachment_resume",
    "attachment_job_description",
    "attachment_image",
    "attachment_document",
    "scope_workspace",
    "scope_global",
    "scope_application",
    "scope_mode",
    "control_pending",
    "control_confirmation",
    "control_read_chain",
    "control_delivery",
    "history_recent",
    "history_relevant",
    "history_orphan_compat",
    "catalog_dependency_closure",
    "catalog_domain_union",
)
_SIGNALS = frozenset(MANIFEST_SIGNAL_VALUES)


class ManifestV2ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedSurfaceManifestV2:
    manifest_json: str
    manifest_digest: str
    fingerprint_key_id: str


def _check_budget(budget_check: Callable[[], None] | None) -> None:
    if budget_check is not None:
        budget_check()


def _canonical(
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    _check_budget(budget_check)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _check_budget(budget_check)
    return rendered


def _identity(
    secret: bytes,
    domain: bytes,
    value: str,
    *,
    budget_check: Callable[[], None] | None = None,
) -> str:
    _check_budget(budget_check)
    digest = hmac.new(secret, digestmod=hashlib.sha256)
    _check_budget(budget_check)
    update_digest_in_chunks(
        digest,
        domain + b"\0",
        budget_check=budget_check,
    )
    update_digest_in_chunks(digest, value, budget_check=budget_check)
    _check_budget(budget_check)
    fingerprint = digest.hexdigest()
    _check_budget(budget_check)
    return fingerprint


def _build_manifest_payload(
    audit: RuntimeSurfaceAudit,
    *,
    key_id: str,
    secret: bytes,
    provider_identities: tuple[str, ...],
    signals: tuple[str, ...],
    budget_check: Callable[[], None] | None,
) -> dict[str, object]:
    _check_budget(budget_check)
    providers: list[str] = []
    _check_budget(budget_check)
    for item in provider_identities:
        _check_budget(budget_check)
        providers.append(
            _identity(
                secret,
                b"offerpilot-surface-provider-v2",
                item,
                budget_check=budget_check,
            )
        )
        _check_budget(budget_check)

    contributors: list[dict[str, str]] = []
    _check_budget(budget_check)
    for name, status in audit.contributor_statuses:
        _check_budget(budget_check)
        contributors.append({"name": name, "status": status})
        _check_budget(budget_check)
    _check_budget(budget_check)

    history_groups: list[str] = []
    _check_budget(budget_check)
    for item in audit.selected_history_group_ids:
        _check_budget(budget_check)
        history_groups.append(
            _identity(
                secret,
                b"offerpilot-surface-history-v2",
                item,
                budget_check=budget_check,
            )
        )
        _check_budget(budget_check)
    _check_budget(budget_check)

    tools: list[str] = []
    _check_budget(budget_check)
    for item in audit.selected_tool_names:
        _check_budget(budget_check)
        tools.append(item)
        _check_budget(budget_check)
    _check_budget(budget_check)

    sources: list[dict[str, object]] = []
    _check_budget(budget_check)
    for source in audit.source_records:
        _check_budget(budget_check)
        chunks: list[dict[str, object]] = []
        _check_budget(budget_check)
        for chunk in source.chunks:
            _check_budget(budget_check)
            chunks.append(
                {
                    "path_hmac": _identity(
                        secret,
                        b"offerpilot-surface-chunk-v2",
                        chunk.path,
                        budget_check=budget_check,
                    ),
                    "ordinal": chunk.ordinal,
                    "total": chunk.total,
                    "truncated": chunk.truncated,
                    "original_bytes": chunk.original_bytes,
                    "original_codepoints": chunk.original_codepoints,
                }
            )
            _check_budget(budget_check)
        _check_budget(budget_check)
        sources.append(
            {
                "source_hmac": _identity(
                    secret,
                    b"offerpilot-surface-source-v2",
                    f"{source.kind}:{source.revision_identity}",
                    budget_check=budget_check,
                ),
                "content_revision_fingerprint": source.content_revision_fingerprint,
                "chunks": chunks,
            }
        )
        _check_budget(budget_check)
    _check_budget(budget_check)
    if not sources:
        _check_budget(budget_check)
        for index, fingerprint in enumerate(audit.source_fingerprints):
            _check_budget(budget_check)
            sources.append(
                {
                    "source_hmac": _identity(
                        secret,
                        b"offerpilot-surface-source-v2",
                        f"{index}:{fingerprint}",
                        budget_check=budget_check,
                    ),
                    "content_revision_fingerprint": fingerprint,
                    "chunks": [],
                }
            )
            _check_budget(budget_check)
        _check_budget(budget_check)

    effective_signals = signals or audit.signals
    _check_budget(budget_check)
    signal_values: list[str] = []
    _check_budget(budget_check)
    for signal in effective_signals:
        _check_budget(budget_check)
        signal_values.append(signal)
        _check_budget(budget_check)
    _check_budget(budget_check)

    _check_budget(budget_check)
    manifest: dict[str, object] = {
        "manifest_schema_version": 2,
        "budget_policy_version": audit.budget_policy_version,
        "providers": providers,
        "contributors": contributors,
        "history_groups": history_groups,
        "tools": tools,
        "sources": sources,
        "signals": signal_values,
        "counts": {
            "estimated_input_units": audit.estimated_input_units,
            "canonical_message_bytes": audit.canonical_message_bytes,
            "canonical_tool_bytes": audit.canonical_tool_bytes,
        },
        "truncated": audit.truncated,
        "fingerprint_key_id": key_id,
    }
    _check_budget(budget_check)
    return manifest


def prepare_surface_manifest_v2(
    audit: RuntimeSurfaceAudit,
    *,
    key_id: str,
    secret: bytes,
    provider_identities: tuple[str, ...],
    signals: tuple[str, ...] = (),
    budget_check: Callable[[], None] | None = None,
) -> PreparedSurfaceManifestV2:
    """Failing helper; callers/recorders must catch all failures (journal is fail-open)."""
    _check_budget(budget_check)
    manifest = _build_manifest_payload(
        audit,
        key_id=key_id,
        secret=secret,
        provider_identities=provider_identities,
        signals=signals,
        budget_check=budget_check,
    )
    _check_budget(budget_check)
    rendered = _canonical(manifest, budget_check=budget_check)
    _check_budget(budget_check)
    encoded = rendered.encode("utf-8")
    _check_budget(budget_check)
    validate_surface_manifest_v2(rendered, budget_check=budget_check)
    _check_budget(budget_check)
    digest = hashlib.sha256()
    update_digest_in_chunks(digest, encoded, budget_check=budget_check)
    _check_budget(budget_check)
    manifest_digest = digest.hexdigest()
    _check_budget(budget_check)
    return PreparedSurfaceManifestV2(
        rendered,
        manifest_digest,
        key_id,
    )


def validate_surface_manifest_v2(
    value: str,
    *,
    budget_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _check_budget(budget_check)
    encoded = value.encode("utf-8")
    _check_budget(budget_check)
    if len(encoded) > MANIFEST_BYTE_CAP:
        raise ManifestV2ValidationError("manifest exceeds 64 KiB")
    _check_budget(budget_check)
    try:
        manifest = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise ManifestV2ValidationError("invalid manifest JSON") from None
    _check_budget(budget_check)
    required = {
        "manifest_schema_version",
        "budget_policy_version",
        "providers",
        "contributors",
        "history_groups",
        "tools",
        "sources",
        "signals",
        "counts",
        "truncated",
        "fingerprint_key_id",
    }
    _check_budget(budget_check)
    canonical_manifest = _canonical(manifest, budget_check=budget_check)
    _check_budget(budget_check)
    if type(manifest) is not dict or set(manifest) != required or canonical_manifest != value:
        raise ManifestV2ValidationError("invalid manifest shape or canonical form")
    if manifest["manifest_schema_version"] != 2:
        raise ManifestV2ValidationError("invalid manifest version")
    if manifest["budget_policy_version"] != "model-surface-budget-v1":
        raise ManifestV2ValidationError("invalid budget policy")
    _hash_array(manifest["providers"], 8, budget_check=budget_check)
    _hash_array(manifest["history_groups"], 32, budget_check=budget_check)
    providers = manifest["providers"]
    if not providers:
        raise ManifestV2ValidationError("empty provider chain")
    contributors = manifest["contributors"]
    if type(contributors) is not list or len(contributors) != 10:
        raise ManifestV2ValidationError("invalid contributors")
    _check_budget(budget_check)
    for item in contributors:
        _check_budget(budget_check)
        if (
            type(item) is not dict
            or set(item) != {"name", "status"}
            or type(item["name"]) is not str
            or _SAFE_NAME.fullmatch(item["name"]) is None
            or type(item["status"]) is not str
            or item["status"] not in _STATUSES
        ):
            raise ManifestV2ValidationError("invalid contributor")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if tuple(item["name"] for item in contributors) != CONTRIBUTOR_ORDER:
        raise ManifestV2ValidationError("invalid contributor order")
    tools = manifest["tools"]
    if type(tools) is not list or len(tools) > 25:
        raise ManifestV2ValidationError("invalid tools")
    _check_budget(budget_check)
    for item in tools:
        _check_budget(budget_check)
        if type(item) is not str or _SAFE_NAME.fullmatch(item) is None:
            raise ManifestV2ValidationError("invalid tool")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if len(set(tools)) != len(tools):
        raise ManifestV2ValidationError("invalid tools")
    _check_budget(budget_check)
    for item in tools:
        _check_budget(budget_check)
        if item not in MODEL_TOOL_NAMES:
            raise ManifestV2ValidationError("unapproved tool")
        _check_budget(budget_check)
    _check_budget(budget_check)
    signals = manifest["signals"]
    if type(signals) is not list or len(signals) > 32:
        raise ManifestV2ValidationError("invalid signals")
    _check_budget(budget_check)
    for signal in signals:
        _check_budget(budget_check)
        if type(signal) is not str:
            raise ManifestV2ValidationError("invalid signal")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if len(set(signals)) != len(signals):
        raise ManifestV2ValidationError("invalid signals")
    _check_budget(budget_check)
    for signal in signals:
        _check_budget(budget_check)
        if signal not in _SIGNALS:
            raise ManifestV2ValidationError("invalid signal")
        _check_budget(budget_check)
    _check_budget(budget_check)
    sources = manifest["sources"]
    if type(sources) is not list or len(sources) > 8:
        raise ManifestV2ValidationError("invalid sources")
    total_chunks = 0
    _check_budget(budget_check)
    for source in sources:
        _check_budget(budget_check)
        if type(source) is not dict or set(source) != {
            "source_hmac",
            "content_revision_fingerprint",
            "chunks",
        }:
            raise ManifestV2ValidationError("invalid source")
        if not _is_hex64(
            source["source_hmac"], budget_check=budget_check
        ) or not _is_hex64(
            source["content_revision_fingerprint"], budget_check=budget_check
        ):
            raise ManifestV2ValidationError("invalid source fingerprint")
        chunks = source["chunks"]
        if type(chunks) is not list or len(chunks) > 32:
            raise ManifestV2ValidationError("invalid chunks")
        total_chunks += len(chunks)
        seen_ordinals: set[int] = set()
        _check_budget(budget_check)
        for chunk in chunks:
            _check_budget(budget_check)
            if type(chunk) is not dict or set(chunk) != {
                "path_hmac",
                "ordinal",
                "total",
                "truncated",
                "original_bytes",
                "original_codepoints",
            }:
                raise ManifestV2ValidationError("invalid chunk")
            if not _is_hex64(chunk["path_hmac"], budget_check=budget_check):
                raise ManifestV2ValidationError("invalid chunk identity")
            if (
                any(
                    type(chunk[key]) is not int or chunk[key] < 0
                    for key in ("ordinal", "total", "original_bytes", "original_codepoints")
                )
                or type(chunk["truncated"]) is not bool
            ):
                raise ManifestV2ValidationError("invalid chunk counts")
            if (
                chunk["ordinal"] < 1
                or chunk["total"] != len(chunks)
                or chunk["ordinal"] > chunk["total"]
            ):
                raise ManifestV2ValidationError("invalid chunk ordinal")
            seen_ordinals.add(chunk["ordinal"])
            _check_budget(budget_check)
        _check_budget(budget_check)
        if seen_ordinals != set(range(1, len(chunks) + 1)):
            raise ManifestV2ValidationError("invalid chunk ordinal")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if total_chunks > 64:
        raise ManifestV2ValidationError("too many chunks")
    counts = manifest["counts"]
    if type(counts) is not dict or set(counts) != {
        "estimated_input_units",
        "canonical_message_bytes",
        "canonical_tool_bytes",
    }:
        raise ManifestV2ValidationError("invalid counts")
    _check_budget(budget_check)
    for item in counts.values():
        _check_budget(budget_check)
        if type(item) is not int or item < 0:
            raise ManifestV2ValidationError("invalid count")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if type(manifest["truncated"]) is not bool:
        raise ManifestV2ValidationError("invalid truncated flag")
    try:
        key_id = str(UUID(manifest["fingerprint_key_id"]))
    except (TypeError, ValueError, AttributeError):
        raise ManifestV2ValidationError("invalid key id") from None
    if key_id != manifest["fingerprint_key_id"]:
        raise ManifestV2ValidationError("invalid key id")
    if "logical_input_fingerprint" in manifest:
        raise ManifestV2ValidationError("logical fingerprint must not be duplicated")
    _check_budget(budget_check)
    return manifest


def _hash_array(
    value: object,
    maximum: int,
    *,
    budget_check: Callable[[], None] | None = None,
) -> None:
    _check_budget(budget_check)
    if type(value) is not list or len(value) > maximum:
        raise ManifestV2ValidationError("invalid identity array")
    _check_budget(budget_check)
    for item in value:
        _check_budget(budget_check)
        if not _is_hex64(item, budget_check=budget_check):
            raise ManifestV2ValidationError("invalid identity")
        _check_budget(budget_check)
    _check_budget(budget_check)
    if len(set(value)) != len(value):
        raise ManifestV2ValidationError("duplicate identity")
    _check_budget(budget_check)


def _is_hex64(
    value: object,
    *,
    budget_check: Callable[[], None] | None = None,
) -> bool:
    _check_budget(budget_check)
    result = type(value) is str and _HEX64.fullmatch(value) is not None
    _check_budget(budget_check)
    return result
