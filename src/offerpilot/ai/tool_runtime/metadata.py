"""Immutable JSON snapshots and canonical metadata fingerprints."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, NoReturn, TypeAlias, cast, overload

from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingEntityKind,
    BindingResolverId,
    ProviderToolContract,
    TransientToolRuntimeValue,
)
from offerpilot.ai.tool_runtime.policy_types import (
    CompensationKind,
    OperationKind,
    ProviderVisibility,
    ToolCapability,
    ToolDomain,
    UndoPayloadKind,
    UndoPolicy,
)


JSONScalar: TypeAlias = None | bool | int | float | str
FrozenJSONValue: TypeAlias = (
    JSONScalar
    | tuple["FrozenJSONValue", ...]
    | MappingProxyType[str, "FrozenJSONValue"]
)
FrozenJSONObject: TypeAlias = MappingProxyType[str, FrozenJSONValue]
FrozenJSONArray: TypeAlias = tuple[FrozenJSONValue, ...]
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


@overload
def freeze_json(value: Mapping[str, object]) -> FrozenJSONObject: ...


@overload
def freeze_json(value: list[object] | tuple[object, ...]) -> FrozenJSONArray: ...


@overload
def freeze_json(value: JSONScalar) -> JSONScalar: ...


def freeze_json(value: object) -> FrozenJSONValue:
    """Take a deep immutable snapshot of an exact JSON-compatible value."""

    return _freeze_json(value, active=set())


def _freeze_json(value: object, *, active: set[int]) -> FrozenJSONValue:
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            _require_valid_unicode(value)
        return cast(JSONScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON mappings are not supported")
        active.add(identity)
        try:
            snapshot: dict[str, FrozenJSONValue] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("JSON mapping keys must be exact strings")
                _require_valid_unicode(key)
                snapshot[key] = _freeze_json(item, active=active)
            return MappingProxyType(snapshot)
        finally:
            active.remove(identity)

    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON sequences are not supported")
        active.add(identity)
        try:
            sequence = cast(list[object] | tuple[object, ...], value)
            return tuple(_freeze_json(item, active=active) for item in sequence)
        finally:
            active.remove(identity)

    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _require_valid_unicode(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("JSON strings must contain valid Unicode scalar values")


@overload
def materialize_json(value: FrozenJSONObject) -> dict[str, JSONValue]: ...


@overload
def materialize_json(value: FrozenJSONArray) -> list[JSONValue]: ...


@overload
def materialize_json(value: JSONScalar) -> JSONScalar: ...


def materialize_json(value: FrozenJSONValue) -> JSONValue:
    """Return a fresh mutable JSON tree from an immutable snapshot."""

    return _materialize_frozen_json(value)


def _materialize_frozen_json(value: FrozenJSONValue) -> JSONValue:
    if type(value) is MappingProxyType:
        materialized: dict[str, JSONValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("frozen JSON mapping keys must be exact strings")
            _require_valid_unicode(key)
            materialized[key] = _materialize_frozen_json(item)
        return materialized
    if type(value) is tuple:
        return [_materialize_frozen_json(item) for item in value]
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            _require_valid_unicode(value)
        return cast(JSONScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("frozen JSON numbers must be finite")
        return value
    raise TypeError("value is not an immutable JSON snapshot")


def canonical_json_bytes(value: FrozenJSONValue) -> bytes:
    """Serialize metadata as compact, sorted, non-normalizing UTF-8 JSON."""

    materialized = materialize_json(value)
    return json.dumps(
        materialized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: FrozenJSONValue) -> str:
    """Return the versioned digest form used by metadata contracts."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


_RESOLVER_IDS = frozenset(
    {
        "application_identity_arg",
        "application_event_parent",
        "note_application_parent",
        "offer_application_parent",
        "resume_identity_arg",
        "jd_analysis_application_parent",
    }
)
_DOMAIN_ORDINAL = {value: ordinal for ordinal, value in enumerate(ToolDomain)}
_CAPABILITY_ORDINAL = {value: ordinal for ordinal, value in enumerate(ToolCapability)}
_IDENTITY_SNAPSHOT_SENTINEL = object()
_MAX_STATIC_TEXT_BYTES = 256


def _require_static_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    if len(value.encode("utf-8")) > _MAX_STATIC_TEXT_BYTES:
        raise ValueError(f"{field_name} exceeds the static metadata byte limit")
    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _require_exact_tuple(value: object, field_name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    return value


def _require_json_scalar(value: object, field_name: str) -> JSONScalar:
    if value is None or type(value) in {bool, int}:
        return cast(JSONScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must be a finite JSON scalar")
        return value
    if type(value) is str:
        _require_valid_unicode(value)
        return value
    raise TypeError(f"{field_name} must be an exact JSON scalar")


def _has_type_aware_duplicates(values: tuple[object, ...]) -> bool:
    seen: set[tuple[type[object], object]] = set()
    for value in values:
        key = (type(value), value)
        if key in seen:
            return True
        seen.add(key)
    return False


@dataclass(frozen=True, slots=True)
class BindingResolverDescriptorV1:
    resolver_id: BindingResolverId
    entity_kind: BindingEntityKind
    arg_path: str
    presence: Literal["required", "optional"]
    identity_type: Literal["positive_int64"]

    def __post_init__(self) -> None:
        self._ensure_valid()

    def _ensure_valid(self) -> None:
        if type(self.resolver_id) is not str or self.resolver_id not in _RESOLVER_IDS:
            raise ValueError("unknown binding resolver id")
        if type(self.entity_kind) is not str or self.entity_kind not in {
            "application",
            "resume",
        }:
            raise ValueError("unknown binding resolver entity kind")
        _require_static_text(self.arg_path, "binding resolver arg_path")
        if not self.arg_path.isidentifier():
            raise ValueError("binding resolver arg_path must be one typed-args field")
        if type(self.presence) is not str or self.presence not in {"required", "optional"}:
            raise ValueError("unknown binding resolver presence")
        if type(self.identity_type) is not str or self.identity_type != "positive_int64":
            raise ValueError("unknown binding resolver identity type")


@dataclass(frozen=True, slots=True)
class ToolBindingMetadataV1:
    contract: BindingContract
    resolver_descriptors: tuple[BindingResolverDescriptorV1, ...] = ()

    def __post_init__(self) -> None:
        self._ensure_valid()

    def _ensure_valid(self) -> None:
        if type(self.contract) is not BindingContract:
            raise TypeError("binding metadata requires an exact BindingContract")
        descriptors = _require_exact_tuple(
            self.resolver_descriptors, "binding resolver descriptors"
        )
        if any(type(value) is not BindingResolverDescriptorV1 for value in descriptors):
            raise TypeError("binding resolver descriptor has the wrong type")
        for descriptor in self.resolver_descriptors:
            descriptor._ensure_valid()
        if _has_type_aware_duplicates(descriptors):
            raise ValueError("binding resolver descriptors must be unique")

        kind = self.contract.kind
        if type(kind) is not str or kind not in {
            "none",
            "enforce_if_bound",
            "scoped_collection",
            "optional_target",
            "non_application_only",
        }:
            raise ValueError("unknown binding contract kind")
        if self.contract.entity_kind is not None and type(self.contract.entity_kind) is not str:
            raise TypeError("binding contract entity kind must be exact text or null")
        if kind in {"none", "non_application_only"}:
            if self.resolver_descriptors:
                raise ValueError("unbound binding contract cannot declare resolvers")
            return
        entity_kind = self.contract.entity_kind
        if entity_kind not in {"application", "resume"}:
            raise ValueError("bound binding contract requires an entity kind")
        if kind in {"scoped_collection", "optional_target"}:
            if len(self.resolver_descriptors) > 1:
                raise ValueError(f"{kind} accepts at most one resolver")
            if self.resolver_descriptors and self.resolver_descriptors[0].presence != "optional":
                raise ValueError(f"{kind} resolver presence must be optional")
        for descriptor in self.resolver_descriptors:
            if descriptor.entity_kind != entity_kind:
                raise ValueError("binding resolver entity kind does not match its contract")


EditableValueType: TypeAlias = Literal[
    "string", "long_text", "enum", "datetime", "number", "boolean"
]


@dataclass(frozen=True, slots=True)
class EditableFieldMetadataV1:
    field: str
    value_type: EditableValueType
    options: tuple[JSONScalar, ...] | None
    clearable: bool
    clear_value: JSONScalar

    def __post_init__(self) -> None:
        self._ensure_valid()

    def _ensure_valid(self) -> None:
        _require_static_text(self.field, "editable field")
        if type(self.value_type) is not str or self.value_type not in {
            "string",
            "long_text",
            "enum",
            "datetime",
            "number",
            "boolean",
        }:
            raise ValueError("unknown editable field value type")
        if type(self.clearable) is not bool:
            raise TypeError("editable field clearable must be an exact bool")
        _require_json_scalar(self.clear_value, "editable field clear_value")
        if not self.clearable and self.clear_value is not None:
            raise ValueError("non-clearable editable field must use a null clear_value")

        if self.value_type == "enum":
            options = _require_exact_tuple(self.options, "editable enum options")
            if not options:
                raise ValueError("editable enum options must be non-empty")
            for option in options:
                _require_json_scalar(option, "editable enum option")
            if _has_type_aware_duplicates(options):
                raise ValueError("editable enum options must be unique")
        elif self.options is not None:
            raise ValueError("only enum editable fields may declare options")

    def to_compat_descriptor(self) -> dict[str, JSONValue]:
        """Project the one canonical descriptor into the existing sparse UI shape."""

        self._ensure_valid()
        descriptor: dict[str, JSONValue] = {
            "field": self.field,
            "type": self.value_type,
        }
        if self.options is not None:
            descriptor["options"] = list(self.options)
        if self.clearable:
            descriptor["clearable"] = True
            descriptor["clear_value"] = self.clear_value
        return descriptor


@dataclass(frozen=True, slots=True)
class ReadOperationMetadataV1:
    kind: OperationKind = OperationKind.READ

    def __post_init__(self) -> None:
        self._ensure_valid()

    def _ensure_valid(self) -> None:
        if type(self.kind) is not OperationKind or self.kind is not OperationKind.READ:
            raise ValueError("read operation kind must be exactly read")


_UNDO_COMPENSATION_PAIRS = {
    UndoPayloadKind.DELETE_APPLICATION: CompensationKind.UNDO_CREATE_APPLICATION,
    UndoPayloadKind.UPDATE_APPLICATION_STATUS: CompensationKind.UNDO_UPDATE_APPLICATION_STATUS,
    UndoPayloadKind.DELETE_APPLICATION_EVENT: CompensationKind.UNDO_CREATE_APPLICATION_EVENT,
    UndoPayloadKind.DELETE_NOTE: CompensationKind.UNDO_ADD_NOTE,
}


@dataclass(frozen=True, slots=True)
class WriteOperationMetadataV1:
    kind: OperationKind = OperationKind.TRANSACTIONAL_WRITE
    adapter_kind: Literal["typed"] = "typed"
    result_contract: Literal["typed_json_v1"] = "typed_json_v1"
    result_bytes: int = 512 * 1024
    visible_bytes: int = 256 * 1024
    transport_bytes: int = 128 * 1024
    undo_bytes: int = 64 * 1024
    undo_policy: UndoPolicy = UndoPolicy.NONE
    undo_payload_kind: UndoPayloadKind | None = None
    compensation_kind: CompensationKind | None = None
    undo_contract_version: str | None = None
    undo_builder_id: str | None = None
    undo_seed_phase: Literal["none", "before_execute"] | None = None

    def __post_init__(self) -> None:
        self._ensure_valid()

    def _ensure_valid(self) -> None:
        if (
            type(self.kind) is not OperationKind
            or self.kind is not OperationKind.TRANSACTIONAL_WRITE
        ):
            raise ValueError("write operation kind must be exactly transactional_write")
        if type(self.adapter_kind) is not str or self.adapter_kind != "typed":
            raise ValueError("Typed write operation adapter_kind must be typed")
        if type(self.result_contract) is not str or self.result_contract != "typed_json_v1":
            raise ValueError("Typed write result contract must be typed_json_v1")
        maxima = (512 * 1024, 256 * 1024, 128 * 1024, 64 * 1024)
        budgets = (
            self.result_bytes,
            self.visible_bytes,
            self.transport_bytes,
            self.undo_bytes,
        )
        if any(
            type(value) is not int or not 1 <= value <= maximum
            for value, maximum in zip(budgets, maxima)
        ):
            raise ValueError("write operation byte budget exceeds the V1 ledger limit")
        if type(self.undo_policy) is not UndoPolicy:
            raise TypeError("write operation requires the exact UndoPolicy type")

        undo_values = (
            self.undo_payload_kind,
            self.compensation_kind,
            self.undo_contract_version,
            self.undo_builder_id,
            self.undo_seed_phase,
        )
        if self.undo_policy is UndoPolicy.NONE:
            if any(value is not None for value in undo_values):
                raise ValueError("undo_policy=none cannot declare Undo metadata")
            return

        if self.undo_policy is not UndoPolicy.REQUIRED:
            raise ValueError("unknown write operation Undo policy")
        if type(self.undo_payload_kind) is not UndoPayloadKind:
            raise TypeError("required Undo must declare an exact payload kind")
        if type(self.compensation_kind) is not CompensationKind:
            raise TypeError("required Undo must declare an exact compensation kind")
        if _UNDO_COMPENSATION_PAIRS[self.undo_payload_kind] is not self.compensation_kind:
            raise ValueError("required Undo payload and compensation kinds do not match")
        _require_static_text(self.undo_contract_version, "Undo contract version")
        _require_static_text(self.undo_builder_id, "Undo builder implementation id")
        if type(self.undo_seed_phase) is not str or self.undo_seed_phase not in {
            "none",
            "before_execute",
        }:
            raise ValueError("required Undo has an unknown seed phase")
        if (
            self.undo_payload_kind is UndoPayloadKind.UPDATE_APPLICATION_STATUS
            and self.undo_seed_phase != "before_execute"
        ):
            raise ValueError("status Undo must capture its seed before execution")
        if (
            self.undo_payload_kind is not UndoPayloadKind.UPDATE_APPLICATION_STATUS
            and self.undo_seed_phase != "none"
        ):
            raise ValueError("delete Undo payloads use the none seed phase")


ToolOperationMetadataV1: TypeAlias = ReadOperationMetadataV1 | WriteOperationMetadataV1


@dataclass(frozen=True, slots=True)
class ToolSurfaceMetadataV1:
    domains: tuple[ToolDomain, ...]
    dependencies: tuple[str, ...]
    provider_visibility: ProviderVisibility
    required_capabilities: tuple[ToolCapability, ...]
    binding: ToolBindingMetadataV1
    confirmation_policy: Literal["none", "required"]
    editable_fields: tuple[EditableFieldMetadataV1, ...]
    operation: ToolOperationMetadataV1
    metadata_version: Literal["tool-surface-metadata-v1"] = "tool-surface-metadata-v1"

    def __post_init__(self) -> None:
        self._ensure_shape()

    def _ensure_shape(self) -> None:
        if type(self.metadata_version) is not str or self.metadata_version != (
            "tool-surface-metadata-v1"
        ):
            raise ValueError("unknown tool surface metadata version")
        domains = _require_exact_tuple(self.domains, "tool domains")
        if any(type(value) is not ToolDomain for value in domains):
            raise TypeError("tool domains require the exact ToolDomain type")
        dependencies = _require_exact_tuple(self.dependencies, "tool dependencies")
        for dependency in dependencies:
            _require_static_text(dependency, "tool dependency")
        capabilities = _require_exact_tuple(
            self.required_capabilities, "required capabilities"
        )
        if any(type(value) is not ToolCapability for value in capabilities):
            raise TypeError("required capabilities require the exact ToolCapability type")
        if type(self.provider_visibility) is not ProviderVisibility or (
            self.provider_visibility is not ProviderVisibility.MODEL_ELIGIBLE
        ):
            raise ValueError("Typed metadata visibility must be model_eligible")
        if type(self.binding) is not ToolBindingMetadataV1:
            raise TypeError("tool metadata requires exact binding metadata")
        self.binding._ensure_valid()
        if type(self.confirmation_policy) is not str or self.confirmation_policy not in {
            "none",
            "required",
        }:
            raise ValueError("unknown confirmation policy")
        editable_fields = _require_exact_tuple(self.editable_fields, "editable fields")
        if any(type(value) is not EditableFieldMetadataV1 for value in editable_fields):
            raise TypeError("editable fields require exact metadata descriptors")
        for editable in self.editable_fields:
            editable._ensure_valid()
        if type(self.operation) is ReadOperationMetadataV1:
            self.operation._ensure_valid()
        elif type(self.operation) is WriteOperationMetadataV1:
            self.operation._ensure_valid()
        else:
            raise TypeError("tool operation metadata must be one exact V1 union member")


class _RuntimeAsdictGuard:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("runtime binding cannot be serialized")


_RUNTIME_ASDICT_GUARD = _RuntimeAsdictGuard()


def _named_callable_identity(value: object, field_name: str) -> tuple[Callable[..., object], str, str]:
    if not inspect.isfunction(value):
        raise TypeError(f"{field_name} must be a named function, not a lambda or partial")
    callback = cast(Callable[..., object], value)
    name = getattr(callback, "__name__", None)
    qualified_name = getattr(callback, "__qualname__", None)
    module = getattr(callback, "__module__", None)
    if (
        type(name) is not str
        or not name
        or name == "<lambda>"
        or type(qualified_name) is not str
        or "<locals>" in qualified_name
        or "<lambda>" in qualified_name
        or type(module) is not str
        or not module
    ):
        raise TypeError(f"{field_name} must be a stable named implementation")
    return (callback, module, qualified_name)


def _identity_snapshot_matches(
    snapshot: object,
    current: tuple[object, ...],
) -> bool:
    if type(snapshot) is not tuple or len(snapshot) != len(current):
        return False
    for expected, actual in zip(snapshot, current):
        if type(expected) is str:
            if expected != actual:
                return False
        elif expected is not actual:
            return False
    return True


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ResolverImplementationBinding(TransientToolRuntimeValue):
    descriptor: BindingResolverDescriptorV1
    implementation_id: str
    resolve: Callable[..., object] = field(repr=False, compare=False)
    _identity_snapshot: object = field(
        default=_IDENTITY_SNAPSHOT_SENTINEL,
        repr=False,
        compare=False,
        kw_only=True,
    )
    _serialization_guard: object = field(
        default=_RUNTIME_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.descriptor) is not BindingResolverDescriptorV1:
            raise TypeError("resolver binding requires an exact descriptor")
        self.descriptor._ensure_valid()
        _require_static_text(self.implementation_id, "resolver implementation id")
        callback, module, qualified_name = _named_callable_identity(
            self.resolve, "resolver implementation"
        )
        if self._identity_snapshot is _IDENTITY_SNAPSHOT_SENTINEL:
            object.__setattr__(
                self,
                "_identity_snapshot",
                (
                    self.descriptor,
                    self.implementation_id,
                    callback,
                    module,
                    qualified_name,
                ),
            )

    def _ensure_integrity(self) -> None:
        if type(self.descriptor) is not BindingResolverDescriptorV1:
            raise TypeError("resolver binding requires an exact descriptor")
        self.descriptor._ensure_valid()
        _require_static_text(self.implementation_id, "resolver implementation id")
        callback, module, qualified_name = _named_callable_identity(
            self.resolve, "resolver implementation"
        )
        current = (
            self.descriptor,
            self.implementation_id,
            callback,
            module,
            qualified_name,
        )
        if not _identity_snapshot_matches(self._identity_snapshot, current):
            raise ValueError("resolver implementation identity seal mismatch")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class UndoBuilderBinding(TransientToolRuntimeValue):
    descriptor: WriteOperationMetadataV1
    implementation_id: str
    capture_seed: Callable[..., object] = field(repr=False, compare=False)
    build_undo: Callable[..., object] = field(repr=False, compare=False)
    _identity_snapshot: object = field(
        default=_IDENTITY_SNAPSHOT_SENTINEL,
        repr=False,
        compare=False,
        kw_only=True,
    )
    _serialization_guard: object = field(
        default=_RUNTIME_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.descriptor) is not WriteOperationMetadataV1:
            raise TypeError("Undo builder binding requires an exact operation descriptor")
        _require_static_text(self.implementation_id, "Undo builder implementation id")
        seed = _named_callable_identity(self.capture_seed, "Undo seed implementation")
        build = _named_callable_identity(self.build_undo, "Undo builder implementation")
        if self._identity_snapshot is _IDENTITY_SNAPSHOT_SENTINEL:
            object.__setattr__(
                self,
                "_identity_snapshot",
                (self.descriptor, self.implementation_id, *seed, *build),
            )

    def _ensure_integrity(self) -> None:
        if type(self.descriptor) is not WriteOperationMetadataV1:
            raise TypeError("Undo builder binding requires an exact operation descriptor")
        self.descriptor._ensure_valid()
        _require_static_text(self.implementation_id, "Undo builder implementation id")
        seed = _named_callable_identity(self.capture_seed, "Undo seed implementation")
        build = _named_callable_identity(self.build_undo, "Undo builder implementation")
        current = (self.descriptor, self.implementation_id, *seed, *build)
        if not _identity_snapshot_matches(self._identity_snapshot, current):
            raise ValueError("Undo builder implementation identity seal mismatch")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ToolPresentationBindingV1(TransientToolRuntimeValue):
    implementation_id: str
    confirmation_description: Callable[..., object] = field(repr=False, compare=False)
    pending_details_projector: Callable[..., object] = field(repr=False, compare=False)
    success_summary_projector: Callable[..., object] = field(repr=False, compare=False)
    _identity_snapshot: object = field(
        default=_IDENTITY_SNAPSHOT_SENTINEL,
        repr=False,
        compare=False,
        kw_only=True,
    )
    _serialization_guard: object = field(
        default=_RUNTIME_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _require_static_text(self.implementation_id, "presentation implementation id")
        description = _named_callable_identity(
            self.confirmation_description, "confirmation description"
        )
        pending = _named_callable_identity(
            self.pending_details_projector, "pending details projector"
        )
        success = _named_callable_identity(
            self.success_summary_projector, "success summary projector"
        )
        if self._identity_snapshot is _IDENTITY_SNAPSHOT_SENTINEL:
            object.__setattr__(
                self,
                "_identity_snapshot",
                (self.implementation_id, *description, *pending, *success),
            )

    def _ensure_integrity(self) -> None:
        _require_static_text(self.implementation_id, "presentation implementation id")
        description = _named_callable_identity(
            self.confirmation_description, "confirmation description"
        )
        pending = _named_callable_identity(
            self.pending_details_projector, "pending details projector"
        )
        success = _named_callable_identity(
            self.success_summary_projector, "success summary projector"
        )
        current = (self.implementation_id, *description, *pending, *success)
        if not _identity_snapshot_matches(self._identity_snapshot, current):
            raise ValueError("presentation implementation identity seal mismatch")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ValidatedToolSpecComponents(TransientToolRuntimeValue):
    provider_contract: ProviderToolContract = field(repr=False)
    metadata: ToolSurfaceMetadataV1 = field(repr=False)
    resolver_bindings: tuple[ResolverImplementationBinding, ...] = field(repr=False)
    undo_builder_binding: UndoBuilderBinding | None = field(repr=False)
    presentation: ToolPresentationBindingV1 = field(repr=False)
    _serialization_guard: object = field(
        default=_RUNTIME_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )


def _require_unique(values: tuple[object, ...], field_name: str) -> None:
    if _has_type_aware_duplicates(values):
        raise ValueError(f"{field_name} contains duplicate values; entries must be unique")


def validate_tool_spec_components(
    *,
    provider_contract: ProviderToolContract,
    metadata: ToolSurfaceMetadataV1,
    resolver_bindings: tuple[ResolverImplementationBinding, ...],
    undo_builder_binding: UndoBuilderBinding | None | object,
    presentation: ToolPresentationBindingV1 | None | object,
) -> ValidatedToolSpecComponents:
    """Validate standalone final components before the Task 4 ToolSpec cutover."""

    if type(provider_contract) is not ProviderToolContract:
        raise TypeError("tool components require an exact Provider contract")
    provider_contract.__post_init__()
    _require_static_text(provider_contract.name, "Provider tool name")
    if type(metadata) is not ToolSurfaceMetadataV1:
        raise TypeError("tool components require exact surface metadata")
    metadata._ensure_shape()

    if not metadata.domains:
        raise ValueError("model-eligible tool metadata requires at least one domain")
    _require_unique(cast(tuple[object, ...], metadata.domains), "tool domains")
    if tuple(_DOMAIN_ORDINAL[value] for value in metadata.domains) != tuple(
        sorted(_DOMAIN_ORDINAL[value] for value in metadata.domains)
    ):
        raise ValueError("tool domains are not in canonical order")

    _require_unique(cast(tuple[object, ...], metadata.dependencies), "tool dependencies")
    if metadata.dependencies != tuple(sorted(metadata.dependencies)):
        raise ValueError("tool dependencies are not in canonical Unicode order")
    if provider_contract.name in metadata.dependencies:
        raise ValueError("tool dependency cannot refer to its own Provider name")

    _require_unique(
        cast(tuple[object, ...], metadata.required_capabilities),
        "required capabilities",
    )
    if len(metadata.required_capabilities) != 1:
        raise ValueError("V1 tool metadata requires exactly one capability")
    if tuple(_CAPABILITY_ORDINAL[value] for value in metadata.required_capabilities) != tuple(
        sorted(_CAPABILITY_ORDINAL[value] for value in metadata.required_capabilities)
    ):
        raise ValueError("required capabilities are not in canonical order")

    editable_names = tuple(value.field for value in metadata.editable_fields)
    _require_unique(cast(tuple[object, ...], editable_names), "editable fields")
    properties = provider_contract.parameters.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("Provider parameter schema has no top-level properties")
    for editable in metadata.editable_fields:
        if editable.field not in properties:
            raise ValueError("editable field is absent from the Provider parameter schema")

    bindings = _require_exact_tuple(resolver_bindings, "resolver bindings")
    descriptors = metadata.binding.resolver_descriptors
    if len(bindings) != len(descriptors):
        raise ValueError("resolver binding count does not match descriptor ordinals")
    implementation_ids: list[object] = []
    for descriptor, binding in zip(descriptors, resolver_bindings):
        if type(binding) is not ResolverImplementationBinding:
            raise TypeError("resolver binding has the wrong runtime type")
        binding._ensure_integrity()
        if binding.descriptor is not descriptor:
            raise ValueError("resolver binding descriptor identity mismatch")
        implementation_ids.append(binding.implementation_id)
    _require_unique(tuple(implementation_ids), "resolver implementation identities")

    if type(presentation) is not ToolPresentationBindingV1:
        raise TypeError("tool presentation binding is required")
    presentation._ensure_integrity()

    operation = metadata.operation
    if type(operation) is ReadOperationMetadataV1:
        if metadata.confirmation_policy != "none":
            raise ValueError("read metadata cannot require confirmation")
        if undo_builder_binding is not None:
            raise ValueError("read metadata cannot declare an Undo builder binding")
        typed_undo_builder: UndoBuilderBinding | None = None
    elif type(operation) is WriteOperationMetadataV1:
        if metadata.confirmation_policy != "required":
            raise ValueError("transactional write metadata must require confirmation")
        if operation.undo_policy is UndoPolicy.NONE:
            if undo_builder_binding is not None:
                raise ValueError("non-required Undo cannot declare a builder binding")
            typed_undo_builder = None
        else:
            if type(undo_builder_binding) is not UndoBuilderBinding:
                raise TypeError("required Undo builder binding is missing")
            undo_builder_binding._ensure_integrity()
            if undo_builder_binding.descriptor is not operation:
                raise ValueError("Undo builder descriptor identity mismatch")
            if undo_builder_binding.implementation_id != operation.undo_builder_id:
                raise ValueError("Undo builder implementation id mismatch")
            typed_undo_builder = undo_builder_binding
    else:
        raise TypeError("unknown tool operation union member")

    return ValidatedToolSpecComponents(
        provider_contract=provider_contract,
        metadata=metadata,
        resolver_bindings=resolver_bindings,
        undo_builder_binding=typed_undo_builder,
        presentation=presentation,
    )


__all__ = [
    "FrozenJSONArray",
    "FrozenJSONObject",
    "FrozenJSONValue",
    "JSONScalar",
    "JSONValue",
    "BindingResolverDescriptorV1",
    "EditableFieldMetadataV1",
    "EditableValueType",
    "ReadOperationMetadataV1",
    "ResolverImplementationBinding",
    "ToolBindingMetadataV1",
    "ToolOperationMetadataV1",
    "ToolPresentationBindingV1",
    "ToolSurfaceMetadataV1",
    "UndoBuilderBinding",
    "ValidatedToolSpecComponents",
    "WriteOperationMetadataV1",
    "canonical_json_bytes",
    "canonical_sha256",
    "freeze_json",
    "materialize_json",
    "validate_tool_spec_components",
]
