"""Future-only validation asset for a possible confirmed-readiness contributor.

This module deliberately has no OfferPilot imports and exposes no runtime proof,
registration, contributor result, source loader, or Provider payload constructor.
"""

from __future__ import annotations

import json
import re
from typing import Final, NoReturn, final


_FIELDS: Final = frozenset(
    {
        "ordered_version_ids",
        "application_id",
        "target_application_event_id",
        "resume_id",
        "selection_fingerprint",
    }
)
_MAX_SELECTIONS: Final = 8
_MAX_SQLITE_ID: Final = 9_223_372_036_854_775_807
_SELECTION_FINGERPRINT: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ConfirmedReadinessContributorValidationError(ValueError):
    """A synthetic future-port fixture is not in the closed canonical shape."""


@final
class ConfirmedReadinessContributorPort:
    """Non-instantiable validator for the reviewed future bridge schema only."""

    def __new__(cls) -> NoReturn:
        del cls
        raise TypeError("ConfirmedReadinessContributorPort is validator-only")

    @staticmethod
    def validate_synthetic(value: object) -> bytes:
        """Validate and canonicalize a synthetic fixture without issuing a proof."""

        if type(value) is not dict or set(value) != _FIELDS:
            raise ConfirmedReadinessContributorValidationError("invalid synthetic shape")

        payload = value
        ordered_ids = payload["ordered_version_ids"]
        if type(ordered_ids) is not list or len(ordered_ids) > _MAX_SELECTIONS:
            raise ConfirmedReadinessContributorValidationError("invalid ordered version ids")
        if any(not _is_id(item) for item in ordered_ids) or len(set(ordered_ids)) != len(
            ordered_ids
        ):
            raise ConfirmedReadinessContributorValidationError("invalid ordered version ids")

        for field in ("application_id", "target_application_event_id", "resume_id"):
            if not _is_id(payload[field]):
                raise ConfirmedReadinessContributorValidationError(f"invalid {field}")

        fingerprint = payload["selection_fingerprint"]
        if type(fingerprint) is not str or _SELECTION_FINGERPRINT.fullmatch(fingerprint) is None:
            raise ConfirmedReadinessContributorValidationError("invalid selection fingerprint")

        canonical = {
            "ordered_version_ids": list(ordered_ids),
            "application_id": payload["application_id"],
            "target_application_event_id": payload["target_application_event_id"],
            "resume_id": payload["resume_id"],
            "selection_fingerprint": fingerprint,
        }
        return json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _is_id(value: object) -> bool:
    return type(value) is int and 0 < value <= _MAX_SQLITE_ID
