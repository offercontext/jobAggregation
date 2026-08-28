from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import importlib
import inspect
import json
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, NoReturn, Protocol, TypeVar, cast

from offerpilot.ai.tool_runtime.contracts import JSONValue, TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.metadata import (
    BundleInstanceToken,
    LegacyAdapterBindingV1,
    LegacyDeterministicBoundaryV1,
)
from offerpilot.ai.tool_runtime.protocol_seals import verify_legacy_boundary


if TYPE_CHECKING:
    from offerpilot.ai.tool_runtime.legacy_proof import (
        LegacyRouteProof,
        LegacyRouteProofConsumerPort,
    )


LEGACY_DETERMINISTIC_NAMES = frozenset(
    {
        "save_application_jd_version",
        "create_application_submission_snapshot",
        "record_application_outcome",
    }
)


class _LegacyArgumentPreparationPort(Protocol):
    @property
    def editable_fields(self) -> tuple[Mapping[str, object], ...]: ...

    def describe(self, encoded_args: str) -> str: ...

    def validate(self, encoded_args: str) -> str: ...


class LegacyArgumentPreparationError(ValueError):
    """Caller-correctable Legacy confirmation argument failure."""


class LegacyRouteSourceV1(str, Enum):
    """Closed server-owned origins for Legacy deterministic routing."""

    JD_CLARIFICATION = "jd_clarification"
    JD_DETERMINISTIC_ACTION = "jd_deterministic_action"
    SUBMISSION_SNAPSHOT_ACTION = "submission_snapshot_action"
    OUTCOME_RECORDING_ACTION = "outcome_recording_action"
    CONFIRMATION_RESUME = "confirmation_resume"


_DIRECT_ROUTE_SOURCES = frozenset(
    {
        LegacyRouteSourceV1.JD_CLARIFICATION,
        LegacyRouteSourceV1.JD_DETERMINISTIC_ACTION,
        LegacyRouteSourceV1.SUBMISSION_SNAPSHOT_ACTION,
        LegacyRouteSourceV1.OUTCOME_RECORDING_ACTION,
    }
)


class LegacyExecutionContextPort(Protocol):
    """Narrow structural view consumed by static Legacy executors."""

    @property
    def session(self) -> object: ...

    @property
    def jd_service(self) -> object: ...

    @property
    def outcomes_repository(self) -> object: ...


class LegacyReadContextPort(Protocol):
    """Narrow structural annotation for exact runtime-checked presentation context."""

    def application_summary(self, application_id: int) -> Mapping[str, object] | None: ...

    def jd_version_number(self, application_id: int, version_id: int) -> int | None: ...


LegacyAdapterDescribe = Callable[[str], str]
LegacyAdapterValidate = Callable[[str], str]
LegacyAdapterExecute = Callable[[str, LegacyExecutionContextPort], str]
LegacyPendingDetailsProjector = Callable[
    [str, LegacyReadContextPort],
    Mapping[str, object],
]
LegacySuccessSummaryProjector = Callable[[str, LegacyReadContextPort], str]


class _LegacyStaticAsdictGuard:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("transient tool runtime value cannot be serialized")


_LEGACY_STATIC_ASDICT_GUARD = _LegacyStaticAsdictGuard()


def _callable_identity(value: object, field_name: str) -> Callable[..., object]:
    if not inspect.isfunction(value):
        raise TypeError(f"Legacy {field_name} must be a module-level named function")
    name = getattr(value, "__name__", None)
    qualname = getattr(value, "__qualname__", None)
    closure = getattr(value, "__closure__", None)
    if (
        type(name) is not str
        or not name
        or name == "<lambda>"
        or type(qualname) is not str
        or "<locals>" in qualname
        or closure is not None
    ):
        raise TypeError(f"Legacy {field_name} must be a module-level named callable")
    return cast(Callable[..., object], value)


def _freeze_legacy_json(value: JSONValue) -> JSONValue:
    if isinstance(value, Mapping):
        frozen: dict[str, JSONValue] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("Legacy editable-field keys must be exact strings")
            frozen[key] = _freeze_legacy_json(child)
        return cast(JSONValue, MappingProxyType(frozen))
    if type(value) in {list, tuple}:
        sequence = cast(Sequence[JSONValue], value)
        return cast(JSONValue, tuple(_freeze_legacy_json(child) for child in sequence))
    if value is None or type(value) in {bool, int, float, str}:
        return value
    raise TypeError("Legacy editable fields must contain JSON values")


def _editable_field_snapshot(
    fields: tuple[Mapping[str, JSONValue], ...],
) -> tuple[tuple[tuple[str, object], ...], ...]:
    def snapshot_value(value: object) -> object:
        if isinstance(value, Mapping):
            return tuple((key, snapshot_value(child)) for key, child in value.items())
        if type(value) in {list, tuple}:
            return tuple(snapshot_value(child) for child in cast(Sequence[object], value))
        return value

    return tuple(
        tuple((key, snapshot_value(value)) for key, value in descriptor.items())
        for descriptor in fields
    )


def _legacy_structure_identity_snapshot(value: object) -> object:
    if isinstance(value, Mapping):
        return (
            "mapping",
            value,
            id(value),
            tuple(
                (key, _legacy_structure_identity_snapshot(child)) for key, child in value.items()
            ),
        )
    if type(value) is tuple:
        items = cast(tuple[object, ...], value)
        return (
            "tuple",
            value,
            id(value),
            tuple(_legacy_structure_identity_snapshot(item) for item in items),
        )
    return (type(value), value)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LegacyPresentationBindingV1(TransientToolRuntimeValue):
    """Sealed runtime-only Legacy confirmation and visible projection callbacks."""

    _serialization_guard: object = field(
        default=_LEGACY_STATIC_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )
    implementation_id: str
    confirmation_description: LegacyAdapterDescribe = field(repr=False, compare=False)
    pending_details_projector: LegacyPendingDetailsProjector = field(
        repr=False,
        compare=False,
    )
    success_summary_projector: LegacySuccessSummaryProjector = field(
        repr=False,
        compare=False,
    )
    _identity_snapshot: tuple[object, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.implementation_id) is not str or not self.implementation_id:
            raise TypeError("Legacy presentation implementation id must be exact text")
        description = _callable_identity(
            self.confirmation_description,
            "presentation confirmation callback",
        )
        pending = _callable_identity(
            self.pending_details_projector,
            "presentation pending projector",
        )
        success = _callable_identity(
            self.success_summary_projector,
            "presentation success projector",
        )
        object.__setattr__(
            self,
            "_identity_snapshot",
            (self.implementation_id, description, pending, success),
        )

    def require_integrity(self) -> None:
        try:
            description = _callable_identity(
                self.confirmation_description,
                "presentation confirmation callback",
            )
            pending = _callable_identity(
                self.pending_details_projector,
                "presentation pending projector",
            )
            success = _callable_identity(
                self.success_summary_projector,
                "presentation success projector",
            )
            seal = self._identity_snapshot
            if (
                type(self.implementation_id) is not str
                or not self.implementation_id
                or type(seal) is not tuple
                or len(seal) != 4
                or seal[0] != self.implementation_id
                or seal[1] is not description
                or seal[2] is not pending
                or seal[3] is not success
            ):
                raise ValueError("Legacy presentation identity seal mismatch")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy presentation integrity drift") from exc


@dataclass(frozen=True, slots=True, repr=False)
class LegacyPendingPresentationV1(TransientToolRuntimeValue):
    """Frozen public Pending-card projection from one exact Legacy route."""

    human: str
    editable_fields: tuple[Mapping[str, JSONValue], ...]
    details: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if type(self.human) is not str:
            raise TypeError("Legacy Pending presentation human must be exact text")
        if type(self.editable_fields) is not tuple:
            raise TypeError("Legacy Pending editable fields must be an exact tuple")
        frozen_fields: list[Mapping[str, JSONValue]] = []
        for descriptor in self.editable_fields:
            if not isinstance(descriptor, Mapping):
                raise TypeError("Legacy Pending editable-field descriptor must be an object")
            frozen = _freeze_legacy_json(cast(JSONValue, dict(descriptor)))
            if not isinstance(frozen, Mapping):
                raise TypeError("Legacy Pending editable-field descriptor must be an object")
            frozen_fields.append(cast(Mapping[str, JSONValue], frozen))
        frozen_details = _freeze_legacy_json(cast(JSONValue, dict(self.details)))
        if not isinstance(frozen_details, Mapping):
            raise TypeError("Legacy Pending details must be an object")
        object.__setattr__(self, "editable_fields", tuple(frozen_fields))
        object.__setattr__(self, "details", cast(Mapping[str, JSONValue], frozen_details))


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LegacyDeterministicAdapterSpec(TransientToolRuntimeValue):
    """Immutable static Legacy behavior without captured repositories."""

    _serialization_guard: object = field(
        default=_LEGACY_STATIC_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )
    ordinal: int
    name: str
    editable_fields: tuple[Mapping[str, JSONValue], ...]
    chained_policy: Literal["same_adapter_only", "forbidden"]
    initial_route_sources: tuple[LegacyRouteSourceV1, ...]
    describe: LegacyAdapterDescribe = field(repr=False, compare=False)
    validate: LegacyAdapterValidate = field(repr=False, compare=False)
    presentation: LegacyPresentationBindingV1 = field(repr=False, compare=False)
    execute: LegacyAdapterExecute = field(repr=False, compare=False)
    _identity_snapshot: tuple[object, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("Legacy Adapter ordinal must be a positive integer")
        if type(self.name) is not str or not self.name:
            raise TypeError("Legacy Adapter name must be non-empty exact text")
        if self.chained_policy not in {"same_adapter_only", "forbidden"}:
            raise ValueError("Legacy Adapter chained policy is invalid")
        if type(self.editable_fields) is not tuple:
            raise TypeError("Legacy editable fields must be an exact tuple")
        frozen_fields: list[Mapping[str, JSONValue]] = []
        for descriptor in self.editable_fields:
            if not isinstance(descriptor, Mapping):
                raise TypeError("Legacy editable-field descriptor must be a mapping")
            frozen = _freeze_legacy_json(cast(JSONValue, dict(descriptor)))
            if not isinstance(frozen, Mapping):
                raise TypeError("Legacy editable-field descriptor must be an object")
            field_name = frozen.get("field")
            if type(field_name) is not str or not field_name:
                raise ValueError("Legacy editable-field descriptor requires a field name")
            frozen_fields.append(cast(Mapping[str, JSONValue], frozen))
        object.__setattr__(self, "editable_fields", tuple(frozen_fields))

        if type(self.initial_route_sources) is not tuple or not self.initial_route_sources:
            raise TypeError("Legacy initial route sources must be a non-empty exact tuple")
        if any(type(source) is not LegacyRouteSourceV1 for source in self.initial_route_sources):
            raise TypeError("Legacy initial route source has the wrong type")
        if len(set(self.initial_route_sources)) != len(self.initial_route_sources) or any(
            source not in _DIRECT_ROUTE_SOURCES for source in self.initial_route_sources
        ):
            raise ValueError("Legacy initial route sources must be unique direct sources")

        describe = _callable_identity(self.describe, "describe callback")
        validate = _callable_identity(self.validate, "validate callback")
        if type(self.presentation) is not LegacyPresentationBindingV1:
            raise TypeError("Legacy Adapter requires an exact presentation binding")
        self.presentation.require_integrity()
        execute = _callable_identity(self.execute, "execute callback")
        snapshot = (
            self.ordinal,
            self.name,
            _legacy_structure_identity_snapshot(self.editable_fields),
            self.chained_policy,
            self.initial_route_sources,
            id(self.initial_route_sources),
            describe,
            validate,
            self.presentation,
            execute,
        )
        object.__setattr__(self, "_identity_snapshot", snapshot)

    def require_integrity(self) -> None:
        try:
            current = (
                self.ordinal,
                self.name,
                _legacy_structure_identity_snapshot(self.editable_fields),
                self.chained_policy,
                self.initial_route_sources,
                id(self.initial_route_sources),
                _callable_identity(self.describe, "describe callback"),
                _callable_identity(self.validate, "validate callback"),
                self.presentation,
                _callable_identity(self.execute, "execute callback"),
            )
            if type(self.presentation) is not LegacyPresentationBindingV1:
                raise TypeError("Legacy Adapter requires an exact presentation binding")
            self.presentation.require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy Adapter integrity drift") from exc
        if current != self._identity_snapshot:
            raise ValueError("Legacy Adapter integrity drift")


class LegacyStaticAdapterCatalogV1(TransientToolRuntimeValue):
    """The unpublished exact static Adapter catalog used by final composition."""

    __slots__ = ("_ordered_adapters", "_catalog_token", "_integrity_seal")
    _ordered_adapters: tuple[LegacyDeterministicAdapterSpec, ...]
    _catalog_token: object
    _integrity_seal: tuple[object, ...]

    def __init__(self, adapters: Sequence[LegacyDeterministicAdapterSpec]) -> None:
        if hasattr(self, "_integrity_seal"):
            raise TypeError("Legacy static Catalog is already initialized")
        ordered = tuple(adapters)
        if not ordered or any(type(item) is not LegacyDeterministicAdapterSpec for item in ordered):
            raise TypeError("Legacy static Catalog requires exact Adapter Specs")
        if tuple(item.ordinal for item in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError("Legacy static Catalog requires contiguous ordinals")
        names = tuple(item.name for item in ordered)
        if len(set(names)) != len(names):
            raise ValueError("Legacy static Catalog requires unique names")
        routed = tuple(source for item in ordered for source in item.initial_route_sources)
        if len(set(routed)) != len(routed) or frozenset(routed) != _DIRECT_ROUTE_SOURCES:
            raise ValueError("Legacy static Catalog requires the four exact direct routes")
        for item in ordered:
            item.require_integrity()
        catalog_token = object()
        object.__setattr__(self, "_ordered_adapters", ordered)
        object.__setattr__(self, "_catalog_token", catalog_token)
        object.__setattr__(
            self,
            "_integrity_seal",
            (ordered, id(ordered), tuple(id(item) for item in ordered), catalog_token),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy static Catalog is sealed")

    @property
    def ordered_adapters(self) -> tuple[LegacyDeterministicAdapterSpec, ...]:
        self.require_integrity()
        return self._ordered_adapters

    @property
    def catalog_instance_token(self) -> object:
        self.require_integrity()
        return self._catalog_token

    def require_integrity(self) -> None:
        try:
            current = (
                self._ordered_adapters,
                id(self._ordered_adapters),
                tuple(id(item) for item in self._ordered_adapters),
                self._catalog_token,
            )
            if current != self._integrity_seal:
                raise ValueError("Legacy static Catalog integrity drift")
            for item in self._ordered_adapters:
                item.require_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy static Catalog integrity drift") from exc


_LEGACY_INITIAL_VALUE_SEAL = object()
_LEGACY_PROOF_CATALOG_SEAL = object()
_LegacyRegistryT = TypeVar("_LegacyRegistryT")


class _LegacyInitialOpaqueValue(TransientToolRuntimeValue, Generic[_LegacyRegistryT]):
    __slots__ = ("_registry", "_identity", "_integrity_seal")
    _registry: _LegacyRegistryT
    _identity: object
    _integrity_seal: tuple[object, object]

    def __init__(
        self,
        seal: object | None = None,
        *,
        registry: _LegacyRegistryT,
    ) -> None:
        if seal is not _LEGACY_INITIAL_VALUE_SEAL:
            raise TypeError("Legacy initial route values are factory-created")
        identity = object()
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_integrity_seal", (registry, identity))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy initial route value is sealed")

    def _ensure_integrity(self, registry: object) -> None:
        try:
            if (
                self._registry is not registry
                or type(self._integrity_seal) is not tuple
                or len(self._integrity_seal) != 2
                or self._integrity_seal[0] is not self._registry
                or self._integrity_seal[1] is not self._identity
            ):
                raise ValueError(
                    "Legacy initial route token/lease/handle has the wrong Registry or container"
                )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "Legacy initial route token/lease/handle has the wrong Registry or container"
            ) from exc


class RuntimeRequestOwnerLease(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ()

    def close(self) -> None:
        try:
            self._registry._close_owner(self)
        except BaseException:
            try:
                self._registry._emergency_close_owner(self)
            except BaseException:
                pass

    def __enter__(self) -> RuntimeRequestOwnerLease:
        self._registry._require_open_owner(self)
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False


class LegacyInitialRequestLease(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ()

    def close(self) -> None:
        try:
            self._registry._close_child(self)
        except BaseException:
            try:
                self._registry._emergency_close_child(self)
            except BaseException:
                pass

    def __enter__(self) -> LegacyInitialRequestLease:
        self._registry._require_open_child(self)
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Literal[False]:
        del exc_type, exc, traceback
        self.close()
        return False


class ServerDeterministicInvocationToken(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ()


class LegacyAdapterRouteHandle(_LegacyInitialOpaqueValue[object]):
    """Common route-authority handle reserved for initial and proof origins."""

    __slots__ = ()


def _create_legacy_proof_route_handle(
    *,
    registry_identity: object,
) -> LegacyAdapterRouteHandle:
    """Create the proof consumer's opaque exact route handle.

    Authorization remains in the proof Registry.  The handle retains only the
    Registry's caller-owned opaque identity plus its own opaque identity; it
    never receives an Adapter, callable, arguments, or execution context.
    """

    if type(registry_identity) is not object:
        raise TypeError("Legacy proof route handle requires an opaque Registry identity")
    return LegacyAdapterRouteHandle(
        _LEGACY_INITIAL_VALUE_SEAL,
        registry=registry_identity,
    )


def _legacy_proof_consumer_port_type() -> type[object]:
    proof_module = importlib.import_module("offerpilot.ai.tool_runtime.legacy_proof")
    consumer_type = getattr(proof_module, "LegacyRouteProofConsumerPort", None)
    if not isinstance(consumer_type, type):
        raise TypeError("Legacy proof consumer Port type is unavailable")
    return cast(type[object], consumer_type)


class LegacyDeterministicCatalog(TransientToolRuntimeValue):
    """Unpublished exact Catalog boundary for confirmation-resume proofs."""

    __slots__ = (
        "_proof_consumer_port",
        "_bundle_instance_token",
        "_catalog_instance_token",
        "_integrity_seal",
    )
    _proof_consumer_port: LegacyRouteProofConsumerPort
    _bundle_instance_token: BundleInstanceToken
    _catalog_instance_token: object
    _integrity_seal: tuple[object, object, object]

    def __init__(
        self,
        seal: object | None = None,
        *,
        proof_consumer_port: LegacyRouteProofConsumerPort,
        bundle_instance_token: BundleInstanceToken,
        catalog_instance_token: object,
    ) -> None:
        if seal is not _LEGACY_PROOF_CATALOG_SEAL:
            raise TypeError("Legacy proof Catalog is factory-created")
        if hasattr(self, "_integrity_seal"):
            raise TypeError("Legacy proof Catalog is already initialized")
        if type(proof_consumer_port) is not _legacy_proof_consumer_port_type():
            raise TypeError("Legacy proof Catalog requires the exact proof consumer Port")
        if type(bundle_instance_token) is not BundleInstanceToken:
            raise TypeError("Legacy proof Catalog requires an exact Bundle instance token")
        if type(catalog_instance_token) is not object:
            raise TypeError("Legacy proof Catalog requires an opaque Catalog instance token")
        if proof_consumer_port.bundle_instance_token is not bundle_instance_token:
            raise ValueError("Legacy proof Catalog Bundle token does not match its consumer Port")
        if proof_consumer_port.catalog_instance_token is not catalog_instance_token:
            raise ValueError("Legacy proof Catalog instance token does not match its consumer Port")
        object.__setattr__(self, "_proof_consumer_port", proof_consumer_port)
        object.__setattr__(self, "_bundle_instance_token", bundle_instance_token)
        object.__setattr__(self, "_catalog_instance_token", catalog_instance_token)
        object.__setattr__(
            self,
            "_integrity_seal",
            (proof_consumer_port, bundle_instance_token, catalog_instance_token),
        )

    @classmethod
    def _create(
        cls,
        *,
        proof_consumer_port: LegacyRouteProofConsumerPort,
        bundle_instance_token: BundleInstanceToken,
        catalog_instance_token: object,
    ) -> LegacyDeterministicCatalog:
        return cls(
            _LEGACY_PROOF_CATALOG_SEAL,
            proof_consumer_port=proof_consumer_port,
            bundle_instance_token=bundle_instance_token,
            catalog_instance_token=catalog_instance_token,
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy proof Catalog is sealed")

    def _ensure_integrity(self) -> None:
        try:
            current = (
                self._proof_consumer_port,
                self._bundle_instance_token,
                self._catalog_instance_token,
            )
            if (
                type(self._proof_consumer_port) is not _legacy_proof_consumer_port_type()
                or type(self._bundle_instance_token) is not BundleInstanceToken
                or type(self._catalog_instance_token) is not object
                or type(self._integrity_seal) is not tuple
                or len(self._integrity_seal) != len(current)
                or any(
                    expected is not actual
                    for expected, actual in zip(self._integrity_seal, current)
                )
                or self._proof_consumer_port.bundle_instance_token
                is not self._bundle_instance_token
                or self._proof_consumer_port.catalog_instance_token
                is not self._catalog_instance_token
            ):
                raise ValueError("Legacy proof Catalog integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy proof Catalog integrity drift") from exc

    @property
    def proof_consumer_port(self) -> LegacyRouteProofConsumerPort:
        self._ensure_integrity()
        return self._proof_consumer_port

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._ensure_integrity()
        return self._bundle_instance_token

    @property
    def catalog_instance_token(self) -> object:
        self._ensure_integrity()
        return self._catalog_instance_token

    def resolve_server_loaded(
        self,
        proof: LegacyRouteProof,
    ) -> LegacyAdapterRouteHandle:
        self._ensure_integrity()
        return cast(LegacyAdapterRouteHandle, self._proof_consumer_port.consume(proof))


class RuntimeRequestOwnerLeaseFactory(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ()

    def open(self) -> RuntimeRequestOwnerLease:
        self._ensure_integrity(self._registry)
        return self._registry._open_owner(self)


class LegacyInitialRouteIssuer(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ("_source", "_binding", "_issuer_integrity_seal")
    _source: LegacyRouteSourceV1
    _binding: LegacyAdapterBindingV1
    _issuer_integrity_seal: tuple[LegacyRouteSourceV1, LegacyAdapterBindingV1]

    def __init__(
        self,
        seal: object | None = None,
        *,
        registry: _LegacyInitialRouteRegistry,
        source: LegacyRouteSourceV1,
        binding: LegacyAdapterBindingV1,
    ) -> None:
        super().__init__(seal, registry=registry)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_issuer_integrity_seal", (source, binding))

    def _ensure_issuer_integrity(self) -> None:
        self._ensure_integrity(self._registry)
        if (
            type(self._issuer_integrity_seal) is not tuple
            or len(self._issuer_integrity_seal) != 2
            or self._issuer_integrity_seal[0] is not self._source
            or self._issuer_integrity_seal[1] is not self._binding
        ):
            raise ValueError("Legacy initial issuer integrity drift")

    def open_request_lease(
        self,
        owner: RuntimeRequestOwnerLease,
    ) -> LegacyInitialRequestLease:
        self._ensure_issuer_integrity()
        return self._registry._open_child(self, owner)

    def issue(
        self,
        lease: LegacyInitialRequestLease,
    ) -> ServerDeterministicInvocationToken:
        self._ensure_issuer_integrity()
        return self._registry._issue(self, lease)


class LegacyInitialRoutePort(_LegacyInitialOpaqueValue["_LegacyInitialRouteRegistry"]):
    __slots__ = ("_bundle_instance_token", "_port_integrity_seal")
    _bundle_instance_token: object
    _port_integrity_seal: object

    def __init__(
        self,
        seal: object | None = None,
        *,
        registry: _LegacyInitialRouteRegistry,
        bundle_instance_token: object,
    ) -> None:
        super().__init__(seal, registry=registry)
        object.__setattr__(self, "_bundle_instance_token", bundle_instance_token)
        object.__setattr__(self, "_port_integrity_seal", bundle_instance_token)

    def _ensure_port_integrity(self) -> None:
        self._ensure_integrity(self._registry)
        if self._port_integrity_seal is not self._bundle_instance_token:
            raise ValueError("Legacy initial route Port integrity drift")

    @property
    def bundle_instance_token(self) -> object:
        self._ensure_port_integrity()
        return self._bundle_instance_token

    @property
    def registry_token(self) -> object:
        self._ensure_port_integrity()
        return self._registry.registry_token

    def resolve_initial(
        self,
        token: ServerDeterministicInvocationToken,
    ) -> LegacyAdapterRouteHandle:
        self._ensure_port_integrity()
        return self._registry._resolve_initial(self, token)

    def require_route(self, handle: object) -> LegacyAdapterBindingV1:
        self._ensure_port_integrity()
        return self._registry._require_route(self, handle)

    def project_pending(
        self,
        handle: LegacyAdapterRouteHandle,
        *,
        encoded_args: str,
        context: LegacyReadContextPort,
    ) -> LegacyPendingPresentationV1:
        self._ensure_port_integrity()
        return self._registry._project_pending(
            self,
            handle,
            encoded_args=encoded_args,
            context=context,
        )


@dataclass(eq=False)
class _OwnerState:
    owner: RuntimeRequestOwnerLease
    status: Literal["open", "closing", "closed"] = "open"
    children: set[LegacyInitialRequestLease] = field(default_factory=set)


@dataclass(eq=False)
class _ChildState:
    lease: LegacyInitialRequestLease
    owner: RuntimeRequestOwnerLease
    issuer: LegacyInitialRouteIssuer
    source: LegacyRouteSourceV1
    binding: LegacyAdapterBindingV1
    status: Literal["open", "closing", "closed"] = "open"
    token: ServerDeterministicInvocationToken | None = None


@dataclass(eq=False)
class _TokenState:
    token: ServerDeterministicInvocationToken
    lease: LegacyInitialRequestLease
    issuer: LegacyInitialRouteIssuer
    source: LegacyRouteSourceV1
    binding: LegacyAdapterBindingV1
    status: Literal["issued", "resolved", "revoked"] = "issued"
    handle: LegacyAdapterRouteHandle | None = None


@dataclass(eq=False)
class _HandleState:
    handle: LegacyAdapterRouteHandle
    token: ServerDeterministicInvocationToken
    lease: LegacyInitialRequestLease
    issuer: LegacyInitialRouteIssuer
    source: LegacyRouteSourceV1
    binding: LegacyAdapterBindingV1
    catalog: LegacyStaticAdapterCatalogV1
    legacy_boundary: LegacyDeterministicBoundaryV1
    runtime_container_token: object
    origin: Literal["initial", "proof"]
    live: bool = True


class _LegacyInitialRouteRegistry(TransientToolRuntimeValue):
    __slots__ = (
        "_catalog",
        "_legacy_boundary",
        "_runtime_container_token",
        "_registry_token",
        "_lock",
        "_owner_factory",
        "_port",
        "_issuers",
        "_owners",
        "_children",
        "_tokens",
        "_handles",
        "_integrity_seal",
        "_topology_seal",
    )
    _catalog: LegacyStaticAdapterCatalogV1
    _legacy_boundary: LegacyDeterministicBoundaryV1
    _runtime_container_token: object
    _registry_token: object
    _lock: RLock
    _owner_factory: RuntimeRequestOwnerLeaseFactory | None
    _port: LegacyInitialRoutePort | None
    _issuers: Mapping[LegacyRouteSourceV1, LegacyInitialRouteIssuer]
    _owners: dict[RuntimeRequestOwnerLease, _OwnerState]
    _children: dict[LegacyInitialRequestLease, _ChildState]
    _tokens: dict[ServerDeterministicInvocationToken, _TokenState]
    _handles: dict[LegacyAdapterRouteHandle, _HandleState]
    _integrity_seal: tuple[object, ...]
    _topology_seal: tuple[object, object, object] | None

    def __init__(
        self,
        *,
        catalog: LegacyStaticAdapterCatalogV1,
        legacy_boundary: LegacyDeterministicBoundaryV1,
        runtime_container_token: object,
    ) -> None:
        registry_token = object()
        lock = RLock()
        owners: dict[RuntimeRequestOwnerLease, _OwnerState] = {}
        children: dict[LegacyInitialRequestLease, _ChildState] = {}
        tokens: dict[ServerDeterministicInvocationToken, _TokenState] = {}
        handles: dict[LegacyAdapterRouteHandle, _HandleState] = {}
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_legacy_boundary", legacy_boundary)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_owner_factory", None)
        object.__setattr__(self, "_port", None)
        object.__setattr__(self, "_issuers", {})
        object.__setattr__(self, "_owners", owners)
        object.__setattr__(self, "_children", children)
        object.__setattr__(self, "_tokens", tokens)
        object.__setattr__(self, "_handles", handles)
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                catalog,
                legacy_boundary,
                runtime_container_token,
                registry_token,
                lock,
                owners,
                children,
                tokens,
                handles,
            ),
        )
        object.__setattr__(self, "_topology_seal", None)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy initial route Registry is sealed")

    @property
    def registry_token(self) -> object:
        self._ensure_integrity()
        return self._registry_token

    def _ensure_integrity(self) -> None:
        try:
            current = (
                self._catalog,
                self._legacy_boundary,
                self._runtime_container_token,
                self._registry_token,
                self._lock,
                self._owners,
                self._children,
                self._tokens,
                self._handles,
            )
            if len(self._integrity_seal) != len(current) or any(
                expected is not actual for expected, actual in zip(self._integrity_seal, current)
            ):
                raise ValueError("Legacy initial route Registry integrity drift")
            if self._topology_seal is not None and (
                self._topology_seal[0] is not self._owner_factory
                or self._topology_seal[1] is not self._port
                or self._topology_seal[2] is not self._issuers
            ):
                raise ValueError("Legacy initial route Registry topology drift")
            self._catalog.require_integrity()
            # Public view access invokes the owning Bundle-token integrity gate.
            _ = self._legacy_boundary.ordered_adapter_bindings
            _ = self._legacy_boundary.initial_route_bindings
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy initial route Registry integrity drift") from exc

    def _finalize(
        self,
        *,
        owner_factory: RuntimeRequestOwnerLeaseFactory,
        port: LegacyInitialRoutePort,
        issuers: Mapping[LegacyRouteSourceV1, LegacyInitialRouteIssuer],
    ) -> None:
        with self._lock:
            self._ensure_integrity()
            if self._owner_factory is not None or self._port is not None or self._issuers:
                raise ValueError("Legacy initial route Registry is already finalized")
            frozen_issuers = MappingProxyType(dict(issuers))
            object.__setattr__(self, "_owner_factory", owner_factory)
            object.__setattr__(self, "_port", port)
            object.__setattr__(self, "_issuers", frozen_issuers)
            object.__setattr__(
                self,
                "_topology_seal",
                (owner_factory, port, frozen_issuers),
            )

    def _open_owner(
        self,
        factory: RuntimeRequestOwnerLeaseFactory,
    ) -> RuntimeRequestOwnerLease:
        with self._lock:
            self._ensure_integrity()
            factory._ensure_integrity(self)
            if factory is not self._owner_factory:
                raise ValueError("Legacy request owner factory has the wrong container")
            owner = RuntimeRequestOwnerLease(_LEGACY_INITIAL_VALUE_SEAL, registry=self)
            self._owners[owner] = _OwnerState(owner)
            return owner

    def _owner_state(self, owner: object) -> _OwnerState:
        if type(owner) is not RuntimeRequestOwnerLease:
            raise TypeError("Legacy request owner lease has the wrong exact type")
        owner._ensure_integrity(self)
        state = self._owners.get(owner)
        if state is None or state.owner is not owner:
            raise ValueError("Legacy request owner lease has the wrong container")
        return state

    def _require_open_owner(self, owner: RuntimeRequestOwnerLease) -> None:
        with self._lock:
            self._ensure_integrity()
            state = self._owner_state(owner)
            if state.status != "open":
                raise ValueError("Legacy request owner lease is closed")

    def _open_child(
        self,
        issuer: LegacyInitialRouteIssuer,
        owner: RuntimeRequestOwnerLease,
    ) -> LegacyInitialRequestLease:
        with self._lock:
            self._ensure_integrity()
            issuer._ensure_issuer_integrity()
            expected = self._issuers.get(issuer._source)
            if expected is not issuer:
                raise ValueError("Legacy initial issuer has the wrong source or container")
            owner_state = self._owner_state(owner)
            if owner_state.status != "open":
                raise ValueError("Legacy request owner lease is closed")
            lease = LegacyInitialRequestLease(_LEGACY_INITIAL_VALUE_SEAL, registry=self)
            state = _ChildState(
                lease,
                owner,
                issuer,
                issuer._source,
                issuer._binding,
            )
            self._children[lease] = state
            owner_state.children.add(lease)
            return lease

    def _child_state(self, lease: object) -> _ChildState:
        if type(lease) is not LegacyInitialRequestLease:
            raise TypeError("Legacy initial request lease has the wrong exact type")
        lease._ensure_integrity(self)
        state = self._children.get(lease)
        if state is None or state.lease is not lease:
            raise ValueError("Legacy initial request lease has the wrong container")
        return state

    def _require_open_child(self, lease: LegacyInitialRequestLease) -> None:
        with self._lock:
            self._ensure_integrity()
            state = self._child_state(lease)
            owner = self._owner_state(state.owner)
            if state.status != "open" or owner.status != "open":
                raise ValueError("Legacy initial request lease is closed")

    def _issue(
        self,
        issuer: LegacyInitialRouteIssuer,
        lease: LegacyInitialRequestLease,
    ) -> ServerDeterministicInvocationToken:
        with self._lock:
            self._ensure_integrity()
            issuer._ensure_issuer_integrity()
            state = self._child_state(lease)
            owner = self._owner_state(state.owner)
            if state.status != "open" or owner.status != "open":
                raise ValueError("Legacy initial request lease is closed")
            if state.issuer is not issuer or self._issuers.get(state.source) is not issuer:
                raise ValueError("Legacy initial issuer and source lease do not match")
            if state.binding is not issuer._binding:
                raise ValueError("Legacy initial issuer binding integrity drift")
            if state.token is not None:
                raise ValueError("Legacy initial request lease already issued a token")
            token = ServerDeterministicInvocationToken(
                _LEGACY_INITIAL_VALUE_SEAL,
                registry=self,
            )
            token_state = _TokenState(
                token,
                lease,
                issuer,
                state.source,
                state.binding,
            )
            state.token = token
            self._tokens[token] = token_state
            return token

    def _token_state(self, token: object) -> _TokenState:
        if type(token) is not ServerDeterministicInvocationToken:
            raise TypeError("Legacy initial route requires an exact token")
        token._ensure_integrity(self)
        state = self._tokens.get(token)
        if state is None or state.token is not token:
            raise ValueError("Legacy initial token has the wrong Port, Registry, or container")
        return state

    def _resolve_initial(
        self,
        port: LegacyInitialRoutePort,
        token: ServerDeterministicInvocationToken,
    ) -> LegacyAdapterRouteHandle:
        with self._lock:
            self._ensure_integrity()
            port._ensure_port_integrity()
            if port is not self._port:
                raise ValueError("Legacy initial token has the wrong Port")
            token_state = self._token_state(token)
            child = self._child_state(token_state.lease)
            owner = self._owner_state(child.owner)
            token_state.issuer._ensure_issuer_integrity()
            if child.status != "open" or owner.status != "open":
                raise ValueError("Legacy initial token belongs to a closed lease")
            if token_state.status == "resolved":
                raise ValueError("Legacy initial token was already resolved once")
            if token_state.status != "issued":
                raise ValueError("Legacy initial token is revoked")
            if (
                token_state.issuer is not child.issuer
                or token_state.source is not child.source
                or token_state.binding is not child.binding
                or token_state.binding is not token_state.issuer._binding
                or self._issuers.get(token_state.source) is not token_state.issuer
            ):
                raise ValueError("Legacy initial token issuer or source integrity drift")
            handle = LegacyAdapterRouteHandle(_LEGACY_INITIAL_VALUE_SEAL, registry=self)
            handle_state = _HandleState(
                handle,
                token,
                token_state.lease,
                token_state.issuer,
                token_state.source,
                token_state.binding,
                self._catalog,
                self._legacy_boundary,
                self._runtime_container_token,
                "initial",
            )
            token_state.status = "resolved"
            token_state.handle = handle
            self._handles[handle] = handle_state
            return handle

    def _require_route(
        self,
        port: LegacyInitialRoutePort,
        handle: object,
    ) -> LegacyAdapterBindingV1:
        with self._lock:
            self._ensure_integrity()
            port._ensure_port_integrity()
            if port is not self._port:
                raise ValueError("Legacy route handle has the wrong Port")
            if type(handle) is not LegacyAdapterRouteHandle:
                raise TypeError("Legacy route requires an exact route handle")
            handle._ensure_integrity(self)
            state = self._handles.get(handle)
            if state is None or state.handle is not handle:
                raise ValueError("Legacy route handle has the wrong Registry or container")
            child = self._child_state(state.lease)
            owner = self._owner_state(child.owner)
            token = self._token_state(state.token)
            state.issuer._ensure_issuer_integrity()
            if (
                not state.live
                or child.status != "open"
                or owner.status != "open"
                or token.status != "resolved"
                or token.handle is not handle
                or state.origin != "initial"
                or state.issuer is not child.issuer
                or state.issuer is not token.issuer
                or state.source is not child.source
                or state.source is not token.source
                or state.binding is not child.binding
                or state.binding is not token.binding
                or state.binding is not state.issuer._binding
                or state.catalog is not self._catalog
                or state.legacy_boundary is not self._legacy_boundary
                or state.runtime_container_token is not self._runtime_container_token
            ):
                raise ValueError("Legacy route handle is revoked or its lease is closed")
            if all(
                candidate is not state.binding
                for candidate in self._legacy_boundary.ordered_adapter_bindings
            ):
                raise ValueError("Legacy route handle binding is outside the Bundle")
            return state.binding

    def _project_pending(
        self,
        port: LegacyInitialRoutePort,
        handle: LegacyAdapterRouteHandle,
        *,
        encoded_args: str,
        context: LegacyReadContextPort,
    ) -> LegacyPendingPresentationV1:
        if type(encoded_args) is not str:
            raise TypeError("Legacy Pending presentation requires exact encoded arguments")
        with self._lock:
            binding = self._require_route(port, handle)
            adapters = self._catalog.ordered_adapters
            bindings = self._legacy_boundary.ordered_adapter_bindings
            matches = tuple(
                adapter for adapter, candidate in zip(adapters, bindings) if candidate is binding
            )
            if len(matches) != 1:
                raise ValueError("Legacy route presentation is outside the exact Bundle")
            adapter = matches[0]
            adapter.require_integrity()
            validation_error = adapter.validate(encoded_args)
            if validation_error:
                raise ValueError(validation_error)
            presentation = adapter.presentation
            presentation.require_integrity()
            human = presentation.confirmation_description(encoded_args)
            details = presentation.pending_details_projector(encoded_args, context)
            if not isinstance(details, Mapping):
                raise TypeError("Legacy Pending details projector must return an object")
            return LegacyPendingPresentationV1(
                human=human,
                editable_fields=adapter.editable_fields,
                details=cast(Mapping[str, JSONValue], details),
            )

    def _revoke_child_locked(self, state: _ChildState) -> None:
        if state.status == "closed":
            return
        state.status = "closing"
        if state.token is not None:
            token = self._tokens.get(state.token)
            if token is not None:
                token.status = "revoked"
                if token.handle is not None:
                    handle = self._handles.get(token.handle)
                    if handle is not None:
                        handle.live = False
                    self._handles.pop(token.handle, None)
                self._tokens.pop(state.token, None)
        owner = self._owners.get(state.owner)
        if owner is not None:
            owner.children.discard(state.lease)
        self._children.pop(state.lease, None)
        state.status = "closed"

    def _close_child(self, lease: LegacyInitialRequestLease) -> None:
        with self._lock:
            self._ensure_integrity()
            state = self._child_state(lease)
            self._revoke_child_locked(state)

    def _close_owner(self, owner: RuntimeRequestOwnerLease) -> None:
        with self._lock:
            self._ensure_integrity()
            state = self._owner_state(owner)
            if state.status == "closed":
                return
            state.status = "closing"
            for lease in tuple(state.children):
                child = self._children.get(lease)
                if child is not None:
                    self._revoke_child_locked(child)
            state.status = "closed"
            self._owners.pop(owner, None)

    def _sealed_cleanup_maps(
        self,
    ) -> tuple[
        RLock,
        dict[RuntimeRequestOwnerLease, _OwnerState],
        dict[LegacyInitialRequestLease, _ChildState],
        dict[ServerDeterministicInvocationToken, _TokenState],
        dict[LegacyAdapterRouteHandle, _HandleState],
    ]:
        seal = object.__getattribute__(self, "_integrity_seal")
        if type(seal) is not tuple or len(seal) != 9:
            raise ValueError("Legacy initial route cleanup seal is unavailable")
        lock = cast(RLock, seal[4])
        owners = cast(dict[RuntimeRequestOwnerLease, _OwnerState], seal[5])
        children = cast(dict[LegacyInitialRequestLease, _ChildState], seal[6])
        tokens = cast(dict[ServerDeterministicInvocationToken, _TokenState], seal[7])
        handles = cast(dict[LegacyAdapterRouteHandle, _HandleState], seal[8])
        if not all(type(value) is dict for value in (owners, children, tokens, handles)):
            raise ValueError("Legacy initial route cleanup maps are unavailable")
        return lock, owners, children, tokens, handles

    @staticmethod
    def _emergency_revoke_child_locked(
        state: _ChildState,
        owners: dict[RuntimeRequestOwnerLease, _OwnerState],
        children: dict[LegacyInitialRequestLease, _ChildState],
        tokens: dict[ServerDeterministicInvocationToken, _TokenState],
        handles: dict[LegacyAdapterRouteHandle, _HandleState],
    ) -> None:
        state.status = "closing"
        if state.token is not None:
            token = tokens.pop(state.token, None)
            if token is not None:
                token.status = "revoked"
                if token.handle is not None:
                    handle = handles.pop(token.handle, None)
                    if handle is not None:
                        handle.live = False
        owner = owners.get(state.owner)
        if owner is not None:
            owner.children.discard(state.lease)
        children.pop(state.lease, None)
        state.status = "closed"

    def _emergency_close_child(self, lease: LegacyInitialRequestLease) -> None:
        lease._ensure_integrity(self)
        lock, owners, children, tokens, handles = self._sealed_cleanup_maps()
        with lock:
            state = children.get(lease)
            if state is None or state.lease is not lease:
                return
            self._emergency_revoke_child_locked(
                state,
                owners,
                children,
                tokens,
                handles,
            )

    def _emergency_close_owner(self, owner: RuntimeRequestOwnerLease) -> None:
        owner._ensure_integrity(self)
        lock, owners, children, tokens, handles = self._sealed_cleanup_maps()
        with lock:
            state = owners.get(owner)
            if state is None or state.owner is not owner:
                return
            state.status = "closing"
            for lease in tuple(state.children):
                child = children.get(lease)
                if child is not None:
                    self._emergency_revoke_child_locked(
                        child,
                        owners,
                        children,
                        tokens,
                        handles,
                    )
            state.status = "closed"
            owners.pop(owner, None)


class LegacyInitialRouteComponents(TransientToolRuntimeValue):
    __slots__ = (
        "_catalog",
        "_legacy_boundary",
        "_runtime_container_token",
        "_owner_lease_factory",
        "_initial_route_port",
        "_issuers",
        "_integrity_seal",
    )
    _catalog: LegacyStaticAdapterCatalogV1
    _legacy_boundary: LegacyDeterministicBoundaryV1
    _runtime_container_token: object
    _owner_lease_factory: RuntimeRequestOwnerLeaseFactory
    _initial_route_port: LegacyInitialRoutePort
    _issuers: Mapping[LegacyRouteSourceV1, LegacyInitialRouteIssuer]
    _integrity_seal: tuple[object, ...]

    def __init__(
        self,
        seal: object | None = None,
        *,
        catalog: LegacyStaticAdapterCatalogV1,
        legacy_boundary: LegacyDeterministicBoundaryV1,
        runtime_container_token: object,
        owner_lease_factory: RuntimeRequestOwnerLeaseFactory,
        initial_route_port: LegacyInitialRoutePort,
        issuers: Mapping[LegacyRouteSourceV1, LegacyInitialRouteIssuer],
    ) -> None:
        if seal is not _LEGACY_INITIAL_VALUE_SEAL:
            raise TypeError("Legacy initial route components are factory-created")
        frozen_issuers = MappingProxyType(dict(issuers))
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_legacy_boundary", legacy_boundary)
        object.__setattr__(self, "_runtime_container_token", runtime_container_token)
        object.__setattr__(self, "_owner_lease_factory", owner_lease_factory)
        object.__setattr__(self, "_initial_route_port", initial_route_port)
        object.__setattr__(self, "_issuers", frozen_issuers)
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                catalog,
                legacy_boundary,
                runtime_container_token,
                owner_lease_factory,
                initial_route_port,
                frozen_issuers,
            ),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Legacy initial route components are sealed")

    def _ensure_integrity(self) -> None:
        try:
            current = (
                self._catalog,
                self._legacy_boundary,
                self._runtime_container_token,
                self._owner_lease_factory,
                self._initial_route_port,
                self._issuers,
            )
            if (
                type(self._integrity_seal) is not tuple
                or len(self._integrity_seal) != len(current)
                or any(
                    expected is not actual
                    for expected, actual in zip(self._integrity_seal, current)
                )
            ):
                raise ValueError("Legacy initial route component integrity drift")
            self._catalog.require_integrity()
            _ = self._legacy_boundary.ordered_adapter_bindings
            self._owner_lease_factory._ensure_integrity(self._owner_lease_factory._registry)
            self._initial_route_port._ensure_port_integrity()
            for issuer in self._issuers.values():
                issuer._ensure_issuer_integrity()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Legacy initial route component integrity drift") from exc

    @property
    def catalog(self) -> LegacyStaticAdapterCatalogV1:
        self._ensure_integrity()
        return self._catalog

    @property
    def runtime_container_token(self) -> object:
        self._ensure_integrity()
        return self._runtime_container_token

    @property
    def owner_lease_factory(self) -> RuntimeRequestOwnerLeaseFactory:
        self._ensure_integrity()
        return self._owner_lease_factory

    @property
    def initial_route_port(self) -> LegacyInitialRoutePort:
        self._ensure_integrity()
        return self._initial_route_port

    def initial_issuer_for(self, source: LegacyRouteSourceV1) -> LegacyInitialRouteIssuer:
        self._ensure_integrity()
        if type(source) is not LegacyRouteSourceV1 or source not in _DIRECT_ROUTE_SOURCES:
            raise ValueError("confirmation resume has no Legacy initial source issuer")
        issuer = self._issuers.get(source)
        if issuer is None:
            raise ValueError("Legacy initial source issuer is unavailable")
        return issuer


def build_unpublished_legacy_initial_route_components(
    *,
    catalog: LegacyStaticAdapterCatalogV1,
    legacy_boundary: LegacyDeterministicBoundaryV1,
    runtime_container_token: object,
) -> LegacyInitialRouteComponents:
    """Build the complete unpublished initial-route capability graph."""

    if type(catalog) is not LegacyStaticAdapterCatalogV1:
        raise TypeError("Legacy initial components require the exact static Catalog")
    if type(legacy_boundary) is not LegacyDeterministicBoundaryV1:
        raise TypeError("Legacy initial components require the exact Bundle boundary")
    if type(runtime_container_token) is not object:
        raise TypeError("Legacy runtime container token must be caller-owned and opaque")
    catalog.require_integrity()
    adapters = catalog.ordered_adapters
    bindings = legacy_boundary.ordered_adapter_bindings
    if len(adapters) != len(bindings) or any(
        adapter.ordinal != binding.ordinal
        or adapter.name != binding.name
        or adapter.chained_policy != binding.chained_policy
        for adapter, binding in zip(adapters, bindings)
    ):
        raise ValueError("Legacy static Catalog does not match the Bundle boundary")
    verify_legacy_boundary(
        tuple(adapter.name for adapter in adapters),
        "forbidden",
        "legacy_deterministic",
    )

    binding_by_ordinal = {binding.ordinal: binding for binding in bindings}
    expected_routes = tuple(
        (source.value, adapter.ordinal)
        for adapter in adapters
        for source in adapter.initial_route_sources
    )
    actual_routes = tuple(
        (route.route_source, route.adapter_ordinal)
        for route in legacy_boundary.initial_route_bindings
    )
    if actual_routes != expected_routes:
        raise ValueError("Legacy initial route sources do not match the Bundle boundary")

    registry = _LegacyInitialRouteRegistry(
        catalog=catalog,
        legacy_boundary=legacy_boundary,
        runtime_container_token=runtime_container_token,
    )
    owner_factory = RuntimeRequestOwnerLeaseFactory(
        _LEGACY_INITIAL_VALUE_SEAL,
        registry=registry,
    )
    port = LegacyInitialRoutePort(
        _LEGACY_INITIAL_VALUE_SEAL,
        registry=registry,
        bundle_instance_token=legacy_boundary.bundle_instance_token,
    )
    issuers: dict[LegacyRouteSourceV1, LegacyInitialRouteIssuer] = {}
    for adapter in adapters:
        binding = binding_by_ordinal[adapter.ordinal]
        for source in adapter.initial_route_sources:
            issuers[source] = LegacyInitialRouteIssuer(
                _LEGACY_INITIAL_VALUE_SEAL,
                registry=registry,
                source=source,
                binding=binding,
            )
    if frozenset(issuers) != _DIRECT_ROUTE_SOURCES:
        raise ValueError("Legacy initial route issuer matrix is incomplete")
    registry._finalize(
        owner_factory=owner_factory,
        port=port,
        issuers=issuers,
    )
    components = LegacyInitialRouteComponents(
        _LEGACY_INITIAL_VALUE_SEAL,
        catalog=catalog,
        legacy_boundary=legacy_boundary,
        runtime_container_token=runtime_container_token,
        owner_lease_factory=owner_factory,
        initial_route_port=port,
        issuers=issuers,
    )
    components._ensure_integrity()
    return components


def _decode_legacy_arguments_object(encoded_args: object) -> dict[str, JSONValue]:
    if type(encoded_args) is not str:
        raise TypeError("Legacy arguments must be exact encoded text")
    try:
        decoded = json.loads(encoded_args)
    except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("pending arguments must be valid JSON") from exc
    if type(decoded) is not dict:
        raise ValueError("pending arguments must be a valid JSON object")
    try:
        json.dumps(decoded, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("pending arguments must contain finite JSON values") from exc
    return cast(dict[str, JSONValue], decoded)


def prepare_legacy_arguments(
    adapter: _LegacyArgumentPreparationPort,
    encoded_args: str,
    edited_args: Mapping[str, JSONValue] | None,
    *,
    validate_unedited: bool = False,
) -> tuple[str, str]:
    if edited_args is None:
        if validate_unedited:
            validation_error = adapter.validate(encoded_args)
            if validation_error:
                raise LegacyArgumentPreparationError(validation_error)
        return encoded_args, adapter.describe(encoded_args)
    try:
        value = _decode_legacy_arguments_object(encoded_args)
    except ValueError as exc:
        raise LegacyArgumentPreparationError(str(exc)) from exc
    editable = {
        str(field["field"]): field
        for field in adapter.editable_fields
        if isinstance(field.get("field"), str)
    }
    forbidden = sorted(str(key) for key in edited_args if key not in editable)
    if forbidden:
        raise LegacyArgumentPreparationError("non-editable fields: " + ", ".join(forbidden))
    effective = {**value, **edited_args}
    try:
        encoded = json.dumps(
            effective,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LegacyArgumentPreparationError(
            "edited arguments must contain finite JSON values"
        ) from exc
    validation_error = adapter.validate(encoded)
    if validation_error:
        raise LegacyArgumentPreparationError(validation_error)
    return encoded, adapter.describe(encoded)


__all__ = [
    "LEGACY_DETERMINISTIC_NAMES",
    "LegacyArgumentPreparationError",
    "LegacyAdapterRouteHandle",
    "LegacyDeterministicAdapterSpec",
    "LegacyDeterministicCatalog",
    "LegacyExecutionContextPort",
    "LegacyInitialRequestLease",
    "LegacyInitialRouteComponents",
    "LegacyInitialRouteIssuer",
    "LegacyInitialRoutePort",
    "LegacyPendingPresentationV1",
    "LegacyPresentationBindingV1",
    "LegacyReadContextPort",
    "LegacyRouteSourceV1",
    "LegacyStaticAdapterCatalogV1",
    "RuntimeRequestOwnerLease",
    "RuntimeRequestOwnerLeaseFactory",
    "ServerDeterministicInvocationToken",
    "TransientToolRuntimeValue",
    "build_unpublished_legacy_initial_route_components",
    "prepare_legacy_arguments",
]
