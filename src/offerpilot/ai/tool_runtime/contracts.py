from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Literal, NoReturn, SupportsIndex, TypeAlias, TypeVar

if TYPE_CHECKING:
    from offerpilot.ai.tool_runtime.context import ToolExecutionContext


JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
ToolKind: TypeAlias = Literal["read", "write"]
ConfirmationPolicy: TypeAlias = Literal["none", "required"]
FailureCategory: TypeAlias = Literal[
    "validation_error",
    "permission_denied",
    "confirmation_rejected",
    "stale_state",
    "conflict",
    "not_found",
    "provider_error",
    "internal_error",
]
BindingStatus: TypeAlias = Literal["matched", "mismatched", "unbound", "unavailable"]

ArgsT = TypeVar("ArgsT")
ResultT = TypeVar("ResultT")


class TransientToolRuntimeValue:
    """A request-scoped value that must never enter a checkpoint or generic payload."""

    __slots__ = ()

    def __repr__(self) -> str:
        # Transient values may contain opaque identities and trusted scope
        # primitives.  A stable type-only representation prevents accidental
        # object-address/field leakage in diagnostics.
        return f"<{type(self).__name__}>"

    @staticmethod
    def _serialization_error() -> TypeError:
        return TypeError("transient tool runtime value cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise self._serialization_error()

    def __getstate__(self) -> NoReturn:
        raise self._serialization_error()

    def __copy__(self) -> NoReturn:
        raise self._serialization_error()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise self._serialization_error()

    def to_json(self) -> NoReturn:
        raise self._serialization_error()


class _TransientAsdictGuard:
    """Private field sentinel closing dataclasses.asdict for transient DTOs."""

    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("transient tool runtime value cannot be serialized")


_TRANSIENT_ASDICT_GUARD = _TransientAsdictGuard()


@dataclass(frozen=True)
class ProviderToolContract:
    payload: Mapping[str, JSONValue] = field(repr=False)
    name: str
    description: str
    parameters: Mapping[str, JSONValue] = field(repr=False)

    def __post_init__(self) -> None:
        function = self.payload.get("function")
        if self.payload.get("type") != "function" or not isinstance(function, Mapping):
            raise ValueError("invalid provider tool envelope")
        if function.get("name") != self.name:
            raise ValueError("provider tool name mismatch")
        if function.get("description") != self.description:
            raise ValueError("provider tool description mismatch")
        if function.get("parameters") != self.parameters:
            raise ValueError("provider tool parameters mismatch")


@dataclass(frozen=True)
class BindingAudit:
    status: BindingStatus
    target_count: int
    entity_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.target_count < 0:
            raise ValueError("binding target_count must be non-negative")


@dataclass(frozen=True)
class BindingTarget:
    entity_kind: str
    identity: int | str | None = field(repr=False)
    available: bool

    def __post_init__(self) -> None:
        if not self.entity_kind:
            raise ValueError("binding target entity_kind is required")
        if self.available != (self.identity is not None):
            raise ValueError("binding target availability is inconsistent")


@dataclass(frozen=True)
class ToolFailure(TransientToolRuntimeValue):
    category: FailureCategory
    code: str
    compatibility_detail: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class ToolSuccess(TransientToolRuntimeValue, Generic[ResultT]):
    result: ResultT = field(repr=False)


ToolOutcome: TypeAlias = ToolSuccess[ResultT] | ToolFailure


@dataclass(frozen=True)
class ToolResultMetadata:
    evidence: tuple[Mapping[str, JSONValue], ...] = ()
    affected_resources: tuple[Mapping[str, JSONValue], ...] = ()
    changed_entities: tuple[Mapping[str, JSONValue], ...] = ()


@dataclass(frozen=True)
class ToolExceptionMapping:
    exception_type: type[Exception]
    category: FailureCategory
    code: str
    compatibility_detail: Callable[[Exception], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


ToolDecoder: TypeAlias = Callable[[Mapping[str, JSONValue]], ArgsT]
ToolCheck: TypeAlias = Callable[[ArgsT, "ToolExecutionContext"], ToolFailure | None]
ToolExecutor: TypeAlias = Callable[[ArgsT, "ToolExecutionContext"], ResultT]
BindingResolver: TypeAlias = Callable[[ArgsT, "ToolExecutionContext"], BindingTarget]
SuccessRenderer: TypeAlias = Callable[[ResultT], str]
ResultMetadataProjector: TypeAlias = Callable[[ResultT], ToolResultMetadata]
ConfirmationDescription: TypeAlias = Callable[[ArgsT], str]
SchemaFailureRenderer: TypeAlias = Callable[[Mapping[str, JSONValue], str], str | None]


class UndoPolicy(str, Enum):
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True)
class WriteContract:
    adapter_kind: Literal["typed", "legacy_deterministic", "compensation"] = "typed"
    result_contract: Literal["typed_json_v1", "legacy_string_v1", "compensation_json_v1"] = (
        "typed_json_v1"
    )
    undo_policy: UndoPolicy = UndoPolicy.NONE
    result_bytes: int = 512 * 1024
    visible_bytes: int = 256 * 1024
    transport_bytes: int = 128 * 1024
    undo_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        maxima = (512 * 1024, 256 * 1024, 128 * 1024, 64 * 1024)
        values = (self.result_bytes, self.visible_bytes, self.transport_bytes, self.undo_bytes)
        if any(value <= 0 or value > maximum for value, maximum in zip(values, maxima)):
            raise ValueError("write contract byte budget exceeds ledger limit")


TRANSACTIONAL_TYPED_WRITE_NAMES = frozenset(
    {
        "create_application",
        "update_application_status",
        "create_application_event",
        "update_application_event",
        "delete_application_event",
        "add_note",
        "update_note",
        "delete_note",
        "update_offer",
        "save_offer_assessment",
        "resume_update_career_intent",
        "resume_rewrite_highlight",
    }
)
REQUIRED_UNDO_TOOL_NAMES = frozenset(
    {"create_application", "update_application_status", "create_application_event", "add_note"}
)


@dataclass(frozen=True)
class ToolSpec(Generic[ArgsT, ResultT]):
    contract: ProviderToolContract
    kind: ToolKind
    decoder: ToolDecoder[ArgsT] = field(repr=False, compare=False)
    executor: ToolExecutor[ArgsT, ResultT] = field(repr=False, compare=False)
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    binding_resolvers: tuple[BindingResolver[ArgsT], ...] = field(default_factory=tuple)
    confirmation_policy: ConfirmationPolicy = "none"
    editable_fields: tuple[Mapping[str, JSONValue], ...] = field(default_factory=tuple)
    preflight: ToolCheck[ArgsT] | None = field(default=None, repr=False, compare=False)
    mutable_validator: ToolCheck[ArgsT] | None = field(default=None, repr=False, compare=False)
    declared_failure_categories: frozenset[FailureCategory] = field(default_factory=frozenset)
    exception_map: tuple[ToolExceptionMapping, ...] = field(default_factory=tuple)
    success_renderer: SuccessRenderer[ResultT] | None = field(
        default=None, repr=False, compare=False
    )
    result_metadata: ResultMetadataProjector[ResultT] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    confirmation_description: ConfirmationDescription[ArgsT] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    schema_failure_renderer: SchemaFailureRenderer | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    write_contract: WriteContract | None = None

    @property
    def name(self) -> str:
        return self.contract.name


@dataclass(frozen=True)
class PreparedToolCall(TransientToolRuntimeValue, Generic[ArgsT, ResultT]):
    tool_call_id: str
    spec: ToolSpec[ArgsT, ResultT] = field(repr=False)
    arguments: Mapping[str, JSONValue] = field(repr=False)
    typed_args: ArgsT = field(repr=False)
    arguments_digest: str
    contract_fingerprint: str
    binding: BindingAudit
    pending_identity: object | None = field(default=None, repr=False, compare=False)
    pending_action_revision: int | None = None
    journal_started_draft: object | None = field(default=None, repr=False, compare=False)
    # The authority token is intentionally opaque and excluded from every
    # provider/ledger/payload representation.  It remains optional until the
    # authority-bound pipeline cutover, while already making prepared calls
    # produced by that pipeline non-serializable and identity-bound.
    authority_instance_token: object | None = field(default=None, repr=False, compare=False)
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class ConfirmationRequired(TransientToolRuntimeValue, Generic[ArgsT, ResultT]):
    prepared: PreparedToolCall[ArgsT, ResultT] = field(repr=False)


@dataclass(frozen=True)
class ReadyToExecute(TransientToolRuntimeValue, Generic[ArgsT, ResultT]):
    prepared: PreparedToolCall[ArgsT, ResultT] = field(repr=False)


PreparedCallResult: TypeAlias = (
    ConfirmationRequired[ArgsT, ResultT] | ReadyToExecute[ArgsT, ResultT] | ToolFailure
)


@dataclass(frozen=True)
class ExecutionAuthorization(TransientToolRuntimeValue):
    pending_identity: object = field(repr=False, compare=False)
    pending_action_revision: int
    tool_call_id: str
    tool_name: str
    arguments_digest: str
    operation_id: str = ""


@dataclass(frozen=True)
class ToolExecutionRecord(TransientToolRuntimeValue, Generic[ArgsT, ResultT]):
    prepared: PreparedToolCall[ArgsT, ResultT] = field(repr=False)
    outcome: ToolSuccess[ResultT] | ToolFailure = field(repr=False)
    execution_started: bool
    operation_id: str = ""
    replayed: bool = False
    terminal_persisted: bool = False
    persisted_visible_result: str | None = None
    persisted_transport: Mapping[str, JSONValue] | None = field(
        default=None, repr=False, compare=False
    )
    journal_started_recorded: bool = False


def heterogeneous_spec(spec: ToolSpec[ArgsT, ResultT]) -> ToolSpec[Any, Any]:
    """Confine heterogeneous Catalog erasure to one reviewed boundary."""

    return spec
