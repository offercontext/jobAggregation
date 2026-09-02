"""Whole-boundary protocol seals for Provider and Legacy tool surfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


APPROVED_PROVIDER_TOOL_BOUNDARY_V1 = (
    "sha256:db60a499a2c76fa46e769214cbdce1488bb86b2f67941d6ae961f25ed997e98b"
)
APPROVED_PROVIDER_TOOL_BOUNDARY_V2 = (
    "sha256:96da7cd82b470da433ec43220000000c6dd4a38c1efff9a8738e0ca880aec7b9"
)
APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1 = (
    "sha256:7d1d6b7e6cf3953b1a17e7a81c9d5655b5366de25b263ea6bd89a646c2f2a580"
)
_APPROVED_LEGACY_BOUNDARY_INPUT_V1 = (
    (
        "save_application_jd_version",
        "create_application_submission_snapshot",
        "record_application_outcome",
    ),
    "forbidden",
    "legacy_deterministic",
)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verify_provider_boundary(
    payloads: Sequence[Mapping[str, Any]],
    *,
    expected_digest: str = APPROVED_PROVIDER_TOOL_BOUNDARY_V1,
) -> None:
    """Verify the complete ordered Provider payload boundary as one unit."""

    if expected_digest not in {
        APPROVED_PROVIDER_TOOL_BOUNDARY_V1,
        APPROVED_PROVIDER_TOOL_BOUNDARY_V2,
    }:
        raise ValueError("provider boundary expected digest is not approved")
    ordered = list(payloads)
    expected_count = 25 if expected_digest == APPROVED_PROVIDER_TOOL_BOUNDARY_V1 else 26
    if len(ordered) != expected_count:
        raise ValueError(
            f"provider boundary must contain exactly {expected_count} ordered tools"
        )
    actual = _canonical_digest(
        {"schema": "provider-tool-boundary-v1", "ordered_tools": ordered}
    )
    if actual != expected_digest:
        raise ValueError("provider boundary seal digest mismatch")


def verify_legacy_boundary(
    ordered_names: Sequence[str],
    provider_visibility: str,
    adapter_kind: str,
    *,
    expected_digest: str = APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1,
) -> None:
    """Verify the complete ordered Legacy deterministic boundary as one unit."""

    if expected_digest != APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1:
        raise ValueError("legacy boundary expected digest is not approved")
    names = list(ordered_names)
    if len(names) != 3 or len(set(names)) != 3:
        raise ValueError("legacy boundary must contain exactly three unique adapters")
    actual = _canonical_digest(
        {
            "schema": "legacy-deterministic-boundary-v1",
            "ordered_names": names,
            "provider_visibility": provider_visibility,
            "adapter_kind": adapter_kind,
        }
    )
    if actual != expected_digest:
        raise ValueError("legacy boundary seal digest mismatch")


def approved_legacy_boundary_input() -> tuple[tuple[str, ...], str, str]:
    """Return the approved whole-boundary input, never a lookup registry."""

    ordered_names, provider_visibility, adapter_kind = _APPROVED_LEGACY_BOUNDARY_INPUT_V1
    verify_legacy_boundary(ordered_names, provider_visibility, adapter_kind)
    return tuple(ordered_names), provider_visibility, adapter_kind


__all__ = [
    "APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1",
    "APPROVED_PROVIDER_TOOL_BOUNDARY_V1",
    "APPROVED_PROVIDER_TOOL_BOUNDARY_V2",
    "approved_legacy_boundary_input",
    "verify_legacy_boundary",
    "verify_provider_boundary",
]
