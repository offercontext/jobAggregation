"""Closed control and product failure categories for Pilot Runtime.

The runtime deliberately keeps transport-control exceptions separate from product
outcomes.  They are control-flow markers only; their string and repr forms never
include a caller-provided reason.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, NoReturn
from typing import final


if TYPE_CHECKING:
    class StrEnum(str, Enum):
        pass
else:
    try:
        from enum import StrEnum
    except ImportError:  # pragma: no cover - Python 3.10 compatibility
        class StrEnum(str, Enum):
            def __str__(self) -> str:
                return self.value


@final
class RuntimeCancelled(Exception):
    """The invocation was cancelled by the user or client lifecycle."""

    def __init__(self, _reason: object | None = None) -> None:
        super().__init__("runtime cancelled")

    def __repr__(self) -> str:
        return "RuntimeCancelled()"

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")


@final
class RuntimeTransportAborted(Exception):
    """The transport can no longer receive a runtime result."""

    def __init__(self, _reason: object | None = None) -> None:
        super().__init__("runtime transport aborted")

    def __repr__(self) -> str:
        return "RuntimeTransportAborted()"

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")


@final
class RuntimeAgentTimedOut(Exception):
    """The Agent execution deadline elapsed before the invocation completed."""

    def __init__(self, _reason: object | None = None) -> None:
        super().__init__("runtime agent timed out")

    def __repr__(self) -> str:
        return "RuntimeAgentTimedOut()"

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("runtime control exceptions cannot be serialized")


@final
class ModelUnconfiguredError(Exception):
    """Closed resolver signal for an intentionally absent model configuration."""

    def __init__(self, message: str = "model unconfigured") -> None:
        super().__init__(message)

    def __repr__(self) -> str:
        return "ModelUnconfiguredError()"


class RuntimeFailureCode(StrEnum):
    """Closed failure codes emitted by the four Chat routes.

    The set is deliberately limited to Chat's direct route responses plus the
    deterministic legacy and write-ledger codes those routes can surface.
    Endpoint-only JD/undo codes do not belong to this runtime contract.
    """

    PENDING_CONFIRMATION_REQUIRED = "pending_confirmation_required"
    SOURCE_LOAD_FAILED = "source_load_failed"
    CHAT_AGENT_TIMEOUT = "chat_agent_timeout"
    AI_PROVIDER_ERROR = "ai_provider_error"
    # Model configuration is an internal classification of the closed
    # provider-error route outcome; keep the public failure-code set closed.
    MODEL_UNCONFIGURED = AI_PROVIDER_ERROR
    CONVERSATION_ARCHIVED = "conversation_archived"
    STALE_PENDING_ACTION = "stale_pending_action"
    CONFIRMATION_IN_PROGRESS = "confirmation_in_progress"
    INVALID_CONFIRMATION = "invalid_confirmation"
    OPERATION_IDENTITY_CONFLICT = "operation_identity_conflict"
    OPERATION_UNAVAILABLE = "operation_unavailable"
    OPERATION_RESULT_UNKNOWN = "operation_result_unknown"
    OPERATION_INPUT_CONFLICT = "operation_input_conflict"
    OPERATION_DELIVERY_PENDING = "operation_delivery_pending"
    OPERATION_DELIVERY_UNKNOWN = "operation_delivery_unknown"
    OPERATION_DELIVERY_FAILED = "operation_delivery_failed"
    OPERATION_INTEGRITY_ERROR = "operation_integrity_error"
    OPERATION_NOT_COMMITTED = "operation_not_committed"
    OPERATION_NOT_TRANSACTIONAL = "operation_not_transactional"
    OPERATION_PROJECTION_FAILED = "operation_projection_failed"
    OPERATION_RESULT_TOO_LARGE = "operation_result_too_large"
    OPERATION_BUSY = "operation_busy"
    OPERATION_FAILED = "operation_failed"
    APPLICATION_JD_INVALID_REQUEST = "application_jd_invalid_request"
    APPLICATION_JD_STALE_CURRENT_VERSION = "application_jd_stale_current_version"
    APPLICATION_JD_IDEMPOTENCY_CONFLICT = "application_jd_idempotency_conflict"
    APPLICATION_JD_NOT_FOUND = "application_jd_not_found"
    APPLICATION_ARCHIVE_IDEMPOTENCY_CONFLICT = "application_archive_idempotency_conflict"
    APPLICATION_ARCHIVE_SOURCE_CONFLICT = "application_archive_source_conflict"
    APPLICATION_ARCHIVE_INVALID_REQUEST = "application_archive_invalid_request"
    APPLICATION_OUTCOME_IDEMPOTENCY_CONFLICT = "application_outcome_idempotency_conflict"
    APPLICATION_OUTCOME_SOURCE_CONFLICT = "application_outcome_source_conflict"
    APPLICATION_OUTCOME_INVALID_REQUEST = "application_outcome_invalid_request"
    APPLICATION_NOT_FOUND = "application_not_found"
    RESUME_NOT_FOUND = "resume_not_found"


__all__ = [
    "RuntimeAgentTimedOut",
    "RuntimeCancelled",
    "RuntimeFailureCode",
    "RuntimeTransportAborted",
    "ModelUnconfiguredError",
]
