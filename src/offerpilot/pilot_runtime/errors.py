"""Closed control and product failure categories for Pilot Runtime.

The runtime deliberately keeps transport-control exceptions separate from product
outcomes.  They are control-flow markers only; their string and repr forms never
include a caller-provided reason.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING
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


@final
class RuntimeTransportAborted(Exception):
    """The transport can no longer receive a runtime result."""

    def __init__(self, _reason: object | None = None) -> None:
        super().__init__("runtime transport aborted")

    def __repr__(self) -> str:
        return "RuntimeTransportAborted()"


class RuntimeFailureCategory(StrEnum):
    """Safe, finite product/runtime failure categories.

    These values are intentionally coarse.  The runtime may retain a more
    detailed internal cause, but only one of these categories crosses the
    contract boundary.
    """

    VALIDATION = "validation_error"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    PENDING_CONFIRMATION_REQUIRED = "pending_confirmation_required"
    SOURCE_LOAD_FAILED = "source_load_failed"
    PROVIDER_ERROR = "provider_error"
    AGENT_TIMEOUT = "chat_agent_timeout"
    OPERATION_PENDING = "operation_pending"
    OPERATION_REPLAY = "operation_replay"
    UNKNOWN = "runtime_error"


# Names used by later transport/continuation tasks remain aliases of the same
# closed enum rather than introducing a second, divergent failure vocabulary.
ProductFailureCategory = RuntimeFailureCategory
RuntimeFailureCode = RuntimeFailureCategory


__all__ = [
    "ProductFailureCategory",
    "RuntimeCancelled",
    "RuntimeFailureCategory",
    "RuntimeFailureCode",
    "RuntimeTransportAborted",
]
