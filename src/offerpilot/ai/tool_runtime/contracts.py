from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
import math
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    NoReturn,
    SupportsIndex,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

from offerpilot.ai.tool_runtime.policy_types import UndoPolicy as UndoPolicy

if TYPE_CHECKING:
    from offerpilot.ai.tool_authority.contracts import AuthorityInstanceToken, PreparedInstanceToken
    from offerpilot.ai.tool_runtime.catalog import SegmentToolSpecHandle
    from offerpilot.ai.tool_runtime.context import ToolExecutionContext
    from offerpilot.ai.tool_runtime.metadata import (
        ResolverImplementationBinding,
        ToolPresentationBindingV1,
        ToolSurfaceMetadataV1,
        UndoBuilderBinding,
    )


if TYPE_CHECKING:
    AuthorityInstanceTokenLike: TypeAlias = AuthorityInstanceToken
    PreparedInstanceTokenLike: TypeAlias = PreparedInstanceToken
    SegmentToolSpecHandleLike: TypeAlias = SegmentToolSpecHandle
else:
    # Resolve annotations safely while the leaf authority module imports this
    # runtime module.  Static type checkers still see the opaque handle type;
    # runtime callers cannot use this alias to construct a token.
    AuthorityInstanceTokenLike: TypeAlias = Any
    PreparedInstanceTokenLike: TypeAlias = Any
    SegmentToolSpecHandleLike: TypeAlias = Any


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
BindingContractKind: TypeAlias = Literal[
    "none",
    "enforce_if_bound",
    "scoped_collection",
    "optional_target",
    "non_application_only",
]
BindingEntityKind: TypeAlias = Literal["application", "resume"]
BindingResolverId: TypeAlias = Literal[
    "application_identity_arg",
    "application_event_parent",
    "note_application_parent",
    "offer_application_parent",
    "resume_identity_arg",
    "jd_analysis_application_parent",
]

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
_PREPARED_REPLACEMENT_SENTINEL = object()


class _FrozenProviderMapping(Mapping[str, object]):
    """Provider-only immutable object node.

    This intentionally is neither ``dict`` nor ``MappingProxyType``.  The
    generic metadata JSON materializer therefore cannot turn Provider query
    nodes into mutable JSON by accident.
    """

    __slots__ = ("_entries",)
    _entries: tuple[tuple[str, object], ...]

    def __init__(self, entries: tuple[tuple[str, object], ...]) -> None:
        object.__setattr__(self, "_entries", entries)

    def __getitem__(self, key: str) -> object:
        for candidate, value in self._entries:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("Provider JSON query nodes are immutable")


class _FrozenProviderSequence(Sequence[object]):
    """Provider-only immutable array node."""

    __slots__ = ("_values",)
    _values: tuple[object, ...]

    def __init__(self, values: tuple[object, ...]) -> None:
        object.__setattr__(self, "_values", values)

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence) or isinstance(other, (str, bytes, bytearray)):
            return False
        return len(self) == len(other) and all(left == right for left, right in zip(self, other))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("Provider JSON query nodes are immutable")


def _freeze_provider_json(value: object, *, active: set[int]) -> object:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("provider JSON numbers must be finite")
        return value
    if type(value) is str:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("provider JSON strings must contain valid Unicode")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic provider payload mappings are not supported")
        active.add(identity)
        try:
            snapshot: list[tuple[str, object]] = []
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("provider payload keys must be exact strings")
                if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                    raise ValueError("provider JSON keys must contain valid Unicode")
                snapshot.append((key, _freeze_provider_json(item, active=active)))
            return _FrozenProviderMapping(tuple(snapshot))
        finally:
            active.remove(identity)
    if type(value) in {list, tuple, _FrozenProviderSequence}:
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic provider payload sequences are not supported")
        active.add(identity)
        try:
            sequence = cast(Sequence[object], value)
            return _FrozenProviderSequence(
                tuple(_freeze_provider_json(item, active=active) for item in sequence)
            )
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported provider JSON value: {type(value).__name__}")


def _provider_content_snapshot(value: object) -> object:
    if type(value) is _FrozenProviderMapping:
        return (
            "object",
            tuple((key, _provider_content_snapshot(item)) for key, item in value._entries),
        )
    if type(value) is _FrozenProviderSequence:
        return (
            "array",
            tuple(_provider_content_snapshot(item) for item in value._values),
        )
    if value is None or type(value) in {bool, int, float, str}:
        return (type(value).__name__, value)
    raise TypeError("Provider JSON query node has an invalid internal value")


def _provider_identity_snapshot(value: object) -> object:
    if type(value) is _FrozenProviderMapping:
        return (
            id(value),
            id(value._entries),
            tuple((key, _provider_identity_snapshot(item)) for key, item in value._entries),
        )
    if type(value) is _FrozenProviderSequence:
        return (
            id(value),
            id(value._values),
            tuple(_provider_identity_snapshot(item) for item in value._values),
        )
    if value is None or type(value) in {bool, int, float, str}:
        return None
    raise TypeError("Provider JSON query node has an invalid internal value")


def _provider_nodes_equal(left: object, right: object) -> bool:
    return _provider_content_snapshot(left) == _provider_content_snapshot(right)


@dataclass(frozen=True, init=False)
class ProviderToolContract:
    name: str
    description: str
    _payload_snapshot: object = field(repr=False)
    _parameters_snapshot: object = field(repr=False)
    _payload_content_seal: object = field(init=False, repr=False, compare=False)
    _parameters_content_seal: object = field(init=False, repr=False, compare=False)
    _payload_identity_seal: object = field(init=False, repr=False, compare=False)
    _parameters_identity_seal: object = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        payload: Mapping[str, Any],
        name: str,
        description: str,
        parameters: Mapping[str, Any],
    ) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "_payload_snapshot", payload)
        object.__setattr__(self, "_parameters_snapshot", parameters)
        self.__post_init__()

    @property
    def payload(self) -> Mapping[str, Any]:
        self._ensure_provider_integrity()
        snapshot = object.__getattribute__(self, "_payload_snapshot")
        if type(snapshot) is not _FrozenProviderMapping:
            raise ValueError("Provider contract integrity drift")
        return cast(Mapping[str, Any], snapshot)

    @payload.setter
    def payload(self, value: Mapping[str, Any]) -> None:
        # The setter exists solely so adversarial ``object.__setattr__`` probes
        # can replace the sealed component and be rejected by Catalog integrity.
        object.__setattr__(self, "_payload_snapshot", value)

    @property
    def parameters(self) -> Mapping[str, Any]:
        self._ensure_provider_integrity()
        snapshot = object.__getattribute__(self, "_parameters_snapshot")
        if type(snapshot) is not _FrozenProviderMapping:
            raise ValueError("Provider contract integrity drift")
        return cast(Mapping[str, Any], snapshot)

    @parameters.setter
    def parameters(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_parameters_snapshot", value)

    def __post_init__(self) -> None:
        seal_fields = (
            "_payload_content_seal",
            "_parameters_content_seal",
            "_payload_identity_seal",
            "_parameters_identity_seal",
        )
        seal_presence = tuple(hasattr(self, name) for name in seal_fields)
        if any(seal_presence):
            if not all(seal_presence):
                raise ValueError("Provider contract integrity drift")
            self._ensure_provider_integrity()
            payload = object.__getattribute__(self, "_payload_snapshot")
            parameters = object.__getattribute__(self, "_parameters_snapshot")
            if type(payload) is not _FrozenProviderMapping:
                raise ValueError("Provider contract integrity drift")
            if type(parameters) is not _FrozenProviderMapping:
                raise ValueError("Provider contract integrity drift")
            function = payload.get("function")
            if type(function) is not _FrozenProviderMapping:
                raise ValueError("Provider contract integrity drift")
            if function.get("name") != self.name:
                raise ValueError("Provider contract integrity drift")
            if function.get("description") != self.description:
                raise ValueError("Provider contract integrity drift")
            if function.get("parameters") is not parameters:
                raise ValueError("Provider contract integrity drift")
            return

        raw_payload = object.__getattribute__(self, "_payload_snapshot")
        raw_parameters = object.__getattribute__(self, "_parameters_snapshot")
        payload = _freeze_provider_json(raw_payload, active=set())
        parameters = _freeze_provider_json(raw_parameters, active=set())
        if type(payload) is not _FrozenProviderMapping:
            raise TypeError("provider payload must be an object")
        if type(parameters) is not _FrozenProviderMapping:
            raise TypeError("provider parameter schema must be an object")
        function = payload.get("function")
        if payload.get("type") != "function" or type(function) is not _FrozenProviderMapping:
            raise ValueError("invalid provider tool envelope")
        if function.get("name") != self.name:
            raise ValueError("provider tool name mismatch")
        if function.get("description") != self.description:
            raise ValueError("provider tool description mismatch")
        payload_parameters = function.get("parameters")
        if type(payload_parameters) is not _FrozenProviderMapping:
            raise TypeError("provider parameter schema must be an object")
        if not _provider_nodes_equal(payload_parameters, parameters):
            raise ValueError("provider tool parameters mismatch")
        object.__setattr__(self, "_payload_snapshot", payload)
        object.__setattr__(self, "_parameters_snapshot", payload_parameters)
        object.__setattr__(
            self,
            "_payload_content_seal",
            _provider_content_snapshot(payload),
        )
        object.__setattr__(
            self,
            "_parameters_content_seal",
            _provider_content_snapshot(payload_parameters),
        )
        object.__setattr__(
            self,
            "_payload_identity_seal",
            _provider_identity_snapshot(payload),
        )
        object.__setattr__(
            self,
            "_parameters_identity_seal",
            _provider_identity_snapshot(payload_parameters),
        )

    def _ensure_provider_integrity(self) -> None:
        payload = object.__getattribute__(self, "_payload_snapshot")
        parameters = object.__getattribute__(self, "_parameters_snapshot")
        try:
            current = (
                _provider_content_snapshot(payload),
                _provider_content_snapshot(parameters),
                _provider_identity_snapshot(payload),
                _provider_identity_snapshot(parameters),
            )
        except TypeError as exc:
            raise ValueError("Provider contract integrity drift") from exc
        expected = (
            object.__getattribute__(self, "_payload_content_seal"),
            object.__getattribute__(self, "_parameters_content_seal"),
            object.__getattribute__(self, "_payload_identity_seal"),
            object.__getattribute__(self, "_parameters_identity_seal"),
        )
        if current != expected:
            raise ValueError("Provider contract integrity drift")
        if type(payload) is not _FrozenProviderMapping:
            raise ValueError("Provider contract integrity drift")
        if type(parameters) is not _FrozenProviderMapping:
            raise ValueError("Provider contract integrity drift")
        function = payload.get("function")
        if payload.get("type") != "function" or type(function) is not _FrozenProviderMapping:
            raise ValueError("Provider contract integrity drift")
        if function.get("name") != object.__getattribute__(self, "name"):
            raise ValueError("Provider contract integrity drift")
        if function.get("description") != object.__getattribute__(self, "description"):
            raise ValueError("Provider contract integrity drift")
        if function.get("parameters") is not parameters:
            raise ValueError("Provider contract integrity drift")

    def __deepcopy__(self, memo: dict[int, object]) -> ProviderToolContract:
        del memo
        self._ensure_provider_integrity()
        return self


def _decode_provider_json(value: object) -> JSONValue:
    if type(value) is _FrozenProviderMapping:
        return {key: _decode_provider_json(item) for key, item in value._entries}
    if type(value) is _FrozenProviderSequence:
        return [_decode_provider_json(item) for item in value._values]
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JSONValue, value)
    raise TypeError("Provider payload is not an immutable Provider JSON node")


def materialize_provider_payloads(
    contracts: Sequence[ProviderToolContract],
) -> list[dict[str, JSONValue]]:
    """Return fresh ordinary JSON payloads through the sole Provider decoder."""

    materialized: list[dict[str, JSONValue]] = []
    for contract in contracts:
        if type(contract) is not ProviderToolContract:
            raise TypeError("Provider materialization requires exact contracts")
        payload = _decode_provider_json(contract.payload)
        if type(payload) is not dict:
            raise TypeError("Provider payload must be an object")
        materialized.append(payload)
    return materialized


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


@dataclass(frozen=True, slots=True)
class BindingContract:
    """Closed V1 binding strategy declared by one Typed Tool.

    The entity kind is explicit rather than inferred from resolver callables or
    tool names.  ``none`` and ``non_application_only`` intentionally carry no
    entity kind because they cannot declare target resolvers.
    """

    kind: BindingContractKind = "none"
    entity_kind: BindingEntityKind | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "none",
            "enforce_if_bound",
            "scoped_collection",
            "optional_target",
            "non_application_only",
        }:
            raise ValueError("unknown binding contract kind")
        if self.kind in {"none", "non_application_only"}:
            if self.entity_kind is not None:
                raise ValueError("unbound binding contract cannot declare an entity kind")
        elif self.entity_kind not in {"application", "resume"}:
            raise ValueError("bound binding contract requires an entity kind")


@dataclass(frozen=True, slots=True)
class BindingResolverSpec(Generic[ArgsT]):
    """Stable resolver metadata plus its request-scoped implementation.

    The metadata is the authority identity.  The callable is deliberately
    excluded from equality/repr/fingerprint; it is only the execution hook.
    """

    resolver_id: BindingResolverId
    entity_kind: BindingEntityKind
    arg_path: str
    presence: Literal["required", "optional"]
    identity_type: Literal["positive_int64"]
    resolve: Callable[[ArgsT, "ToolExecutionContext"], Any] = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.resolver_id not in {
            "application_identity_arg",
            "application_event_parent",
            "note_application_parent",
            "offer_application_parent",
            "resume_identity_arg",
            "jd_analysis_application_parent",
        }:
            raise ValueError("unknown binding resolver id")
        if self.entity_kind not in {"application", "resume"}:
            raise ValueError("unknown binding resolver entity kind")
        if type(self.arg_path) is not str or not self.arg_path or not self.arg_path.isidentifier():
            raise ValueError("binding resolver arg_path must be a single typed-args field")
        if self.presence not in {"required", "optional"}:
            raise ValueError("unknown binding resolver presence")
        if self.identity_type != "positive_int64":
            raise ValueError("unknown binding resolver identity type")
        if not callable(self.resolve):
            raise TypeError("binding resolver implementation must be callable")

    def __call__(self, args: ArgsT, context: "ToolExecutionContext") -> Any:
        """Keep the existing Pipeline call site source-compatible."""

        return self.resolve(args, context)


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
BindingResolver: TypeAlias = (
    Callable[[ArgsT, "ToolExecutionContext"], Any] | BindingResolverSpec[ArgsT]
)
SuccessRenderer: TypeAlias = Callable[[ResultT], str]
ResultMetadataProjector: TypeAlias = Callable[[ResultT], ToolResultMetadata]
ConfirmationDescription: TypeAlias = Callable[[ArgsT], str]
SchemaFailureRenderer: TypeAlias = Callable[[Mapping[str, JSONValue], str], str | None]


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
    metadata: ToolSurfaceMetadataV1
    resolver_bindings: tuple[ResolverImplementationBinding, ...]
    undo_builder_binding: UndoBuilderBinding | None
    decoder: ToolDecoder[ArgsT] = field(repr=False, compare=False)
    executor: ToolExecutor[ArgsT, ResultT] = field(repr=False, compare=False)
    presentation: ToolPresentationBindingV1 = field(repr=False, compare=False)
    preflight: ToolCheck[ArgsT] | None = field(default=None, repr=False, compare=False)
    mutable_validator: ToolCheck[ArgsT] | None = field(default=None, repr=False, compare=False)
    declared_failure_categories: frozenset[FailureCategory] = field(default_factory=frozenset)
    exception_map: tuple[ToolExceptionMapping, ...] = field(default_factory=tuple)
    success_renderer: SuccessRenderer[ResultT] | None = field(
        default=None, repr=False, compare=False
    )
    result_metadata_projector: ResultMetadataProjector[ResultT] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    schema_failure_renderer: SchemaFailureRenderer | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def name(self) -> str:
        return self.contract.name


@dataclass(frozen=True, slots=True, repr=False)
class PreparedToolCall(TransientToolRuntimeValue, Generic[ArgsT, ResultT]):
    tool_call_id: str
    spec: ToolSpec[ArgsT, ResultT] = field(repr=False)
    arguments: Mapping[str, JSONValue] = field(repr=False)
    typed_args: ArgsT = field(repr=False)
    arguments_digest: str
    contract_fingerprint: str
    binding: BindingAudit
    spec_handle: SegmentToolSpecHandleLike = field(
        repr=False,
        compare=False,
    )
    pending_identity: object | None = field(default=None, repr=False, compare=False)
    pending_action_revision: int | None = None
    journal_started_draft: object | None = field(default=None, repr=False, compare=False)
    # The two opaque registry tokens are attached only by the controlled
    # factory-backed prepare port.  Optional construction defaults allow that
    # factory to allocate the Prepared object before its identity token exists;
    # the Pipeline never returns a Prepared value until both are exact.
    authority_instance_token: AuthorityInstanceTokenLike | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    prepared_instance_token: PreparedInstanceTokenLike | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _replacement_guard: object = field(
        default=_PREPARED_REPLACEMENT_SENTINEL,
        init=True,
        repr=False,
        compare=False,
        kw_only=True,
    )
    _serialization_guard: object = field(
        default=_TRANSIENT_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._replacement_guard is not _PREPARED_REPLACEMENT_SENTINEL:
            # Legacy Tool Pipeline still enriches an unbound Prepared value
            # once with its journal draft.  Authority-bound values are sealed
            # and cannot enter that compatibility path.
            if self.authority_instance_token is not None:
                raise TypeError("transient tool runtime value cannot be replaced")
        object.__setattr__(self, "_replacement_guard", object())

    def __getstate__(self) -> NoReturn:
        # ``dataclass(slots=True)`` synthesizes a state reader for frozen
        # instances unless the concrete class closes that serialization port.
        raise self._serialization_error()


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
