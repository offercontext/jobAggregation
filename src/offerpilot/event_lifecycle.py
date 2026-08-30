from __future__ import annotations

from typing import Literal, TypeAlias


EventLifecycleV1: TypeAlias = Literal[
    "scheduled",
    "in_progress",
    "completed",
    "cancelled",
    "unknown",
]

_EVENT_LIFECYCLE_V1: dict[str, EventLifecycleV1] = {
    "todo": "scheduled",
    "pending": "scheduled",
    "scheduled": "scheduled",
    "in_progress": "in_progress",
    "done": "completed",
    "completed": "completed",
    "cancelled": "cancelled",
    "deleted": "cancelled",
    "soft_deleted": "cancelled",
}


def classify_event_lifecycle_v1(status: object) -> EventLifecycleV1:
    """Classify the shared V1 alias fixture only; never infer from timestamps."""

    if type(status) is not str:
        return "unknown"
    return _EVENT_LIFECYCLE_V1.get(status, "unknown")


__all__ = ["EventLifecycleV1", "classify_event_lifecycle_v1"]
