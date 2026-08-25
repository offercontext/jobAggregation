"""Leaf policy enums shared by tool metadata and execution authority.

This module intentionally depends only on the Python standard library.  Keep
repository, ORM, provider, authority-composition, and runtime imports out of
this leaf so policy values have one stable identity without import cycles.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import cast


class _PolicyValue(str, Enum):
    def __str__(self) -> str:
        return cast(str, self.value)


@unique
class ToolDomain(_PolicyValue):
    APPLICATIONS = "applications"
    EVENTS = "events"
    JD = "jd"
    NOTES = "notes"
    OFFERS = "offers"
    RESUMES = "resumes"


@unique
class ToolCapability(_PolicyValue):
    APPLICATIONS_READ = "applications.read"
    APPLICATIONS_WRITE = "applications.write"
    APPLICATION_EVENTS_READ = "application_events.read"
    APPLICATION_EVENTS_WRITE = "application_events.write"
    NOTES_READ = "notes.read"
    NOTES_WRITE = "notes.write"
    OFFERS_READ = "offers.read"
    OFFERS_WRITE = "offers.write"
    RESUMES_READ = "resumes.read"
    RESUMES_WRITE = "resumes.write"
    JD_ANALYSES_READ = "jd_analyses.read"


@unique
class ProviderVisibility(_PolicyValue):
    MODEL_ELIGIBLE = "model_eligible"


@unique
class LegacyBoundaryVisibility(_PolicyValue):
    FORBIDDEN = "forbidden"


@unique
class OperationKind(_PolicyValue):
    READ = "read"
    TRANSACTIONAL_WRITE = "transactional_write"


@unique
class UndoPolicy(_PolicyValue):
    NONE = "none"
    REQUIRED = "required"


@unique
class UndoPayloadKind(_PolicyValue):
    DELETE_APPLICATION = "delete_application"
    UPDATE_APPLICATION_STATUS = "update_application_status"
    DELETE_APPLICATION_EVENT = "delete_application_event"
    DELETE_NOTE = "delete_note"


@unique
class CompensationKind(_PolicyValue):
    UNDO_CREATE_APPLICATION = "undo:create_application"
    UNDO_UPDATE_APPLICATION_STATUS = "undo:update_application_status"
    UNDO_CREATE_APPLICATION_EVENT = "undo:create_application_event"
    UNDO_ADD_NOTE = "undo:add_note"


__all__ = [
    "CompensationKind",
    "LegacyBoundaryVisibility",
    "OperationKind",
    "ProviderVisibility",
    "ToolCapability",
    "ToolDomain",
    "UndoPayloadKind",
    "UndoPolicy",
]
