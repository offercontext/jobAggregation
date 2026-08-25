"""Immutable JSON snapshots and canonical metadata fingerprints."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NoReturn, SupportsIndex, TypeAlias, cast, overload

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

if TYPE_CHECKING:
    from offerpilot.ai.tool_runtime.catalog import (
        SegmentToolCatalogLease,
        ToolCatalog,
        ToolMetadataManifestV1,
    )


JSONScalar: TypeAlias = None | bool | int | float | str
FrozenJSONValue: TypeAlias = (
    JSONScalar | tuple["FrozenJSONValue", ...] | MappingProxyType[str, "FrozenJSONValue"]
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
_BUNDLE_VALUE_CONSTRUCTION_SEAL = object()


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


def _require_sha256_fingerprint(value: object, field_name: str) -> str:
    fingerprint = _require_static_text(value, field_name)
    digest = fingerprint.removeprefix("sha256:")
    if (
        not fingerprint.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{field_name} must be a canonical sha256 fingerprint")
    return fingerprint


def _require_positive_ordinal(value: object, field_name: str = "tool ordinal") -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_canonical_domains(
    value: object,
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[ToolDomain, ...]:
    domains = _require_exact_tuple(value, field_name)
    if any(type(domain) is not ToolDomain for domain in domains):
        raise TypeError(f"{field_name} requires exact ToolDomain values")
    if not allow_empty and not domains:
        raise ValueError(f"{field_name} must be non-empty")
    if _has_type_aware_duplicates(domains):
        raise ValueError(f"{field_name} must not contain duplicates")
    typed_domains = cast(tuple[ToolDomain, ...], domains)
    if tuple(_DOMAIN_ORDINAL[domain] for domain in typed_domains) != tuple(
        sorted(_DOMAIN_ORDINAL[domain] for domain in typed_domains)
    ):
        raise ValueError(f"{field_name} must use canonical domain order")
    return typed_domains


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
        capabilities = _require_exact_tuple(self.required_capabilities, "required capabilities")
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


@dataclass(frozen=True, slots=True)
class ToolDiscoveryEntryV1:
    """Static, callable-free facts required by discovery and dependency closure."""

    ordinal: int
    provider_name: str
    provider_contract: ProviderToolContract
    domains: tuple[ToolDomain, ...]
    dependencies: tuple[str, ...]
    provider_visibility: ProviderVisibility

    def __post_init__(self) -> None:
        _require_positive_ordinal(self.ordinal)
        _require_static_text(self.provider_name, "discovery Provider name")
        if type(self.provider_contract) is not ProviderToolContract:
            raise TypeError("discovery entry requires an exact Provider contract")
        self.provider_contract._ensure_provider_integrity()
        if self.provider_contract.name != self.provider_name:
            raise ValueError("discovery entry Provider identity mismatch")
        _require_canonical_domains(
            self.domains,
            "discovery domains",
            allow_empty=False,
        )
        dependencies = _require_exact_tuple(self.dependencies, "discovery dependencies")
        for dependency in dependencies:
            _require_static_text(dependency, "discovery dependency")
        if _has_type_aware_duplicates(dependencies):
            raise ValueError("discovery dependencies must be unique")
        if dependencies != tuple(sorted(cast(tuple[str, ...], dependencies))):
            raise ValueError("discovery dependencies must use canonical order")
        if self.provider_name in dependencies:
            raise ValueError("discovery entry cannot depend on itself")
        if (
            type(self.provider_visibility) is not ProviderVisibility
            or self.provider_visibility is not ProviderVisibility.MODEL_ELIGIBLE
        ):
            raise ValueError("discovery entry visibility must be model_eligible")


@dataclass(frozen=True, slots=True)
class ToolDiscoveryPolicyV1:
    """Deeply frozen discovery policy projection for a generic 1..N Catalog.

    Production composition supplies the exact closed V1 Manifest projection.
    Synthetic Catalogs deliberately keep their smaller explicit projection;
    this primitive never invents missing production policy values.
    """

    projection: FrozenJSONObject

    def __init__(self, projection: Mapping[str, object]) -> None:
        frozen = freeze_json(projection)
        if type(frozen) is not MappingProxyType or not frozen:
            raise ValueError("discovery policy must be a non-empty JSON object")
        object.__setattr__(self, "projection", frozen)


@dataclass(frozen=True, slots=True)
class ToolAuthorityEntryV1:
    """Static authority facts; resolver implementations deliberately stay on ToolSpec."""

    ordinal: int
    provider_name: str
    provider_visibility: ProviderVisibility
    required_capabilities: tuple[ToolCapability, ...]
    binding: ToolBindingMetadataV1
    confirmation_policy: Literal["none", "required"]
    operation_kind: OperationKind

    def __post_init__(self) -> None:
        _require_positive_ordinal(self.ordinal)
        _require_static_text(self.provider_name, "authority Provider name")
        if (
            type(self.provider_visibility) is not ProviderVisibility
            or self.provider_visibility is not ProviderVisibility.MODEL_ELIGIBLE
        ):
            raise ValueError("authority entry visibility must be model_eligible")
        capabilities = _require_exact_tuple(
            self.required_capabilities,
            "authority capabilities",
        )
        if len(capabilities) != 1 or any(
            type(capability) is not ToolCapability for capability in capabilities
        ):
            raise ValueError("authority entry requires exactly one capability")
        if type(self.binding) is not ToolBindingMetadataV1:
            raise TypeError("authority entry requires exact binding metadata")
        self.binding._ensure_valid()
        if type(self.confirmation_policy) is not str or self.confirmation_policy not in {
            "none",
            "required",
        }:
            raise ValueError("authority entry has an unknown confirmation policy")
        if type(self.operation_kind) is not OperationKind:
            raise TypeError("authority entry requires an exact operation kind")
        if (self.operation_kind is OperationKind.READ) != (self.confirmation_policy == "none"):
            raise ValueError("authority operation and confirmation policy do not match")


@dataclass(frozen=True, slots=True)
class ToolOperationEntryV1:
    """Static operation and confirmation projection without runtime handlers."""

    ordinal: int
    provider_name: str
    confirmation_policy: Literal["none", "required"]
    editable_fields: tuple[EditableFieldMetadataV1, ...]
    operation: ToolOperationMetadataV1

    def __post_init__(self) -> None:
        _require_positive_ordinal(self.ordinal)
        _require_static_text(self.provider_name, "operation Provider name")
        if type(self.confirmation_policy) is not str or self.confirmation_policy not in {
            "none",
            "required",
        }:
            raise ValueError("operation entry has an unknown confirmation policy")
        editable_fields = _require_exact_tuple(
            self.editable_fields,
            "operation editable fields",
        )
        if any(type(value) is not EditableFieldMetadataV1 for value in editable_fields):
            raise TypeError("operation editable fields require exact descriptors")
        for editable in self.editable_fields:
            editable._ensure_valid()
        if type(self.operation) is ReadOperationMetadataV1:
            self.operation._ensure_valid()
            if self.confirmation_policy != "none" or self.editable_fields:
                raise ValueError("read operation projection cannot require confirmation")
        elif type(self.operation) is WriteOperationMetadataV1:
            self.operation._ensure_valid()
            if self.confirmation_policy != "required":
                raise ValueError("write operation projection must require confirmation")
        else:
            raise TypeError("operation entry requires an exact V1 operation descriptor")


@dataclass(frozen=True, slots=True)
class LegacyAdapterBindingV1:
    ordinal: int
    name: str
    provider_visibility: Literal["forbidden"]
    adapter_kind: Literal["legacy_deterministic"]
    chained_policy: str | None

    def __post_init__(self) -> None:
        _require_positive_ordinal(self.ordinal, "Legacy adapter ordinal")
        _require_static_text(self.name, "Legacy adapter name")
        if self.provider_visibility != "forbidden":
            raise ValueError("Legacy adapter visibility must be forbidden")
        if self.adapter_kind != "legacy_deterministic":
            raise ValueError("Legacy adapter kind must be legacy_deterministic")
        if self.chained_policy is not None:
            _require_static_text(self.chained_policy, "Legacy chained policy")


@dataclass(frozen=True, slots=True)
class LegacyInitialRouteBindingV1:
    route_source: str
    adapter_ordinal: int

    def __post_init__(self) -> None:
        _require_static_text(self.route_source, "Legacy initial route source")
        _require_positive_ordinal(self.adapter_ordinal, "Legacy route adapter ordinal")


@dataclass(frozen=True, slots=True)
class CompensationHandlerBindingV1:
    ordinal: int
    compensation_kind: str
    handler_id: str

    def __post_init__(self) -> None:
        _require_positive_ordinal(self.ordinal, "Compensation handler ordinal")
        _require_static_text(self.compensation_kind, "Compensation operation")
        _require_static_text(self.handler_id, "Compensation handler id")


class BundleInstanceToken(TransientToolRuntimeValue):
    """Opaque process-local provenance for one complete metadata Bundle."""

    __slots__ = ("_issued", "_lock", "_sealed", "_integrity_seal")
    _issued: Mapping[type[object], tuple[object, object]]
    _lock: RLock
    _sealed: bool
    _integrity_seal: tuple[object, ...] | None

    def __new__(cls, seal: object | None = None) -> "BundleInstanceToken":
        if seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Bundle instance tokens are factory-created")
        return object.__new__(cls)

    def __init__(self, seal: object | None = None) -> None:
        if seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Bundle instance tokens are factory-created")
        if hasattr(self, "_issued"):
            raise TypeError("Bundle instance token is already initialized")
        object.__setattr__(self, "_issued", {})
        object.__setattr__(self, "_lock", RLock())
        object.__setattr__(self, "_sealed", False)
        object.__setattr__(self, "_integrity_seal", None)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Bundle instance token components are sealed")

    def _register_view(self, view: object) -> None:
        with self._lock:
            if self._sealed or type(self._issued) is not dict:
                raise ValueError("Bundle instance token registry is finalized")
            issued = cast(dict[type[object], tuple[object, object]], self._issued)
            view_type = type(view)
            if view_type in issued:
                raise ValueError(f"{view_type.__name__} was already issued")
            issued[view_type] = (view, _bundle_view_integrity_snapshot(view))

    def _finalize_views(self) -> None:
        with self._lock:
            if self._sealed or type(self._issued) is not dict:
                raise ValueError("Bundle instance token registry is already finalized")
            issued = MappingProxyType(dict(self._issued))
            object.__setattr__(self, "_issued", issued)
            object.__setattr__(self, "_sealed", True)
            object.__setattr__(
                self,
                "_integrity_seal",
                (
                    id(issued),
                    tuple(
                        (view_type, id(view), snapshot)
                        for view_type, (view, snapshot) in issued.items()
                    ),
                    id(self._lock),
                ),
            )

    def _ensure_integrity(self) -> None:
        try:
            if not self._sealed or type(self._issued) is not MappingProxyType:
                raise ValueError("Bundle instance token registry is not finalized")
            current = (
                id(self._issued),
                tuple(
                    (view_type, id(view), snapshot)
                    for view_type, (view, snapshot) in self._issued.items()
                ),
                id(self._lock),
            )
            if current != self._integrity_seal:
                raise ValueError("Bundle instance token integrity drift")
            for view, expected_snapshot in self._issued.values():
                if _bundle_view_integrity_snapshot(view) != expected_snapshot:
                    raise ValueError("metadata Bundle view integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Bundle instance token integrity drift") from exc

    def _require_view(self, view: object, expected_type: type[object]) -> None:
        with self._lock:
            self._ensure_integrity()
            issued = self._issued.get(expected_type)
            if type(view) is not expected_type or issued is None or issued[0] is not view:
                raise TypeError("metadata view was not issued by this Bundle token")
            try:
                current = _bundle_view_integrity_snapshot(view)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("metadata view integrity drift") from exc
            if current != issued[1]:
                raise ValueError("metadata view integrity drift")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise self._serialization_error()


def _require_bundle_instance_token(value: object) -> BundleInstanceToken:
    if type(value) is not BundleInstanceToken:
        raise TypeError("view requires a factory-issued BundleInstanceToken")
    return value


class _SealedBundleView(TransientToolRuntimeValue):
    __slots__ = ()

    def __getattribute__(self, name: str) -> object:
        if not name.startswith("_"):
            token = cast(
                BundleInstanceToken,
                object.__getattribute__(self, "bundle_instance_token"),
            )
            token._require_view(self, type(self))
        return object.__getattribute__(self, name)


def _freeze_entry_mapping(
    value: object,
    *,
    entry_type: type[ToolAuthorityEntryV1] | type[ToolOperationEntryV1],
    field_name: str,
) -> MappingProxyType[str, ToolAuthorityEntryV1 | ToolOperationEntryV1]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen: dict[str, ToolAuthorityEntryV1 | ToolOperationEntryV1] = {}
    ordinals: list[int] = []
    for name, entry in value.items():
        if type(name) is not str:
            raise TypeError(f"{field_name} keys must be exact strings")
        if type(entry) is not entry_type:
            raise TypeError(f"{field_name} values have the wrong entry type")
        if entry.provider_name != name:
            raise ValueError(f"{field_name} key does not match its Provider name")
        if name in frozen:
            raise ValueError(f"{field_name} Provider names must be unique")
        frozen[name] = entry
        ordinals.append(entry.ordinal)
    if tuple(ordinals) != tuple(range(1, len(ordinals) + 1)):
        raise ValueError(f"{field_name} must preserve contiguous Catalog ordinals")
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class ProviderToolMetadataView(_SealedBundleView):
    ordered_contracts: tuple[ProviderToolContract, ...]
    provider_boundary_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        ordered_contracts: tuple[ProviderToolContract, ...],
        provider_boundary_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Provider metadata views are Bundle-created")
        contracts = _require_exact_tuple(ordered_contracts, "Provider contracts")
        if not contracts or any(type(value) is not ProviderToolContract for value in contracts):
            raise TypeError("Provider view requires exact Provider contracts")
        typed_contracts = cast(tuple[ProviderToolContract, ...], contracts)
        names: list[str] = []
        for contract in typed_contracts:
            contract._ensure_provider_integrity()
            names.append(contract.name)
        if len(set(names)) != len(names):
            raise ValueError("Provider view contracts must have unique names")
        object.__setattr__(self, "ordered_contracts", typed_contracts)
        object.__setattr__(
            self,
            "provider_boundary_fingerprint",
            _require_sha256_fingerprint(
                provider_boundary_fingerprint,
                "Provider boundary fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class ToolDiscoveryMetadataView(_SealedBundleView):
    ordered_entries: tuple[ToolDiscoveryEntryV1, ...]
    policy: ToolDiscoveryPolicyV1
    discovery_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        ordered_entries: tuple[ToolDiscoveryEntryV1, ...],
        policy: ToolDiscoveryPolicyV1,
        discovery_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Discovery metadata views are Bundle-created")
        entries = _require_exact_tuple(ordered_entries, "discovery entries")
        if not entries or any(type(value) is not ToolDiscoveryEntryV1 for value in entries):
            raise TypeError("Discovery view requires exact discovery entries")
        typed_entries = cast(tuple[ToolDiscoveryEntryV1, ...], entries)
        if tuple(entry.ordinal for entry in typed_entries) != tuple(
            range(1, len(typed_entries) + 1)
        ):
            raise ValueError("discovery entries must preserve contiguous Catalog ordinals")
        if len({entry.provider_name for entry in typed_entries}) != len(typed_entries):
            raise ValueError("discovery entries must have unique Provider names")
        known_names = {entry.provider_name for entry in typed_entries}
        if any(
            dependency not in known_names
            for entry in typed_entries
            for dependency in entry.dependencies
        ):
            raise ValueError("discovery dependency points outside the Typed view")
        if type(policy) is not ToolDiscoveryPolicyV1:
            raise TypeError("Discovery view requires an exact V1 policy")
        object.__setattr__(self, "ordered_entries", typed_entries)
        object.__setattr__(self, "policy", policy)
        object.__setattr__(
            self,
            "discovery_fingerprint",
            _require_sha256_fingerprint(discovery_fingerprint, "discovery fingerprint"),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class ToolAuthorityMetadataView(_SealedBundleView):
    entries: Mapping[str, ToolAuthorityEntryV1]
    authority_manifest_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        entries: Mapping[str, ToolAuthorityEntryV1],
        authority_manifest_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Authority metadata views are Bundle-created")
        frozen = _freeze_entry_mapping(
            entries,
            entry_type=ToolAuthorityEntryV1,
            field_name="authority entries",
        )
        object.__setattr__(self, "entries", cast(Mapping[str, ToolAuthorityEntryV1], frozen))
        object.__setattr__(
            self,
            "authority_manifest_fingerprint",
            _require_sha256_fingerprint(
                authority_manifest_fingerprint,
                "Authority Manifest fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class ToolOperationMetadataView(_SealedBundleView):
    entries: Mapping[str, ToolOperationEntryV1]
    operation_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        entries: Mapping[str, ToolOperationEntryV1],
        operation_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Operation metadata views are Bundle-created")
        frozen = _freeze_entry_mapping(
            entries,
            entry_type=ToolOperationEntryV1,
            field_name="operation entries",
        )
        object.__setattr__(self, "entries", cast(Mapping[str, ToolOperationEntryV1], frozen))
        object.__setattr__(
            self,
            "operation_fingerprint",
            _require_sha256_fingerprint(operation_fingerprint, "operation fingerprint"),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class LegacyDeterministicBoundaryV1(_SealedBundleView):
    ordered_adapter_bindings: tuple[LegacyAdapterBindingV1, ...]
    initial_route_bindings: tuple[LegacyInitialRouteBindingV1, ...]
    legacy_boundary_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        ordered_adapter_bindings: tuple[LegacyAdapterBindingV1, ...],
        initial_route_bindings: tuple[LegacyInitialRouteBindingV1, ...],
        legacy_boundary_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Legacy boundary views are Bundle-created")
        adapter_bindings = _require_exact_tuple(
            ordered_adapter_bindings,
            "Legacy adapter bindings",
        )
        if not adapter_bindings or any(
            type(binding) is not LegacyAdapterBindingV1 for binding in adapter_bindings
        ):
            raise TypeError("Legacy boundary requires exact adapter bindings")
        typed_adapters = cast(tuple[LegacyAdapterBindingV1, ...], adapter_bindings)
        if tuple(binding.ordinal for binding in typed_adapters) != tuple(
            range(1, len(typed_adapters) + 1)
        ):
            raise ValueError("Legacy adapter bindings require contiguous ordinals")
        route_bindings = _require_exact_tuple(
            initial_route_bindings,
            "Legacy initial route bindings",
        )
        if any(type(binding) is not LegacyInitialRouteBindingV1 for binding in route_bindings):
            raise TypeError("Legacy boundary requires exact initial route bindings")
        typed_routes = cast(tuple[LegacyInitialRouteBindingV1, ...], route_bindings)
        if any(binding.adapter_ordinal > len(typed_adapters) for binding in typed_routes):
            raise ValueError("Legacy initial route points outside the adapter boundary")
        object.__setattr__(self, "ordered_adapter_bindings", typed_adapters)
        object.__setattr__(self, "initial_route_bindings", typed_routes)
        object.__setattr__(
            self,
            "legacy_boundary_fingerprint",
            _require_sha256_fingerprint(
                legacy_boundary_fingerprint,
                "Legacy boundary fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False, init=False)
class CompensationMetadataView(_SealedBundleView):
    ordered_handler_bindings: tuple[CompensationHandlerBindingV1, ...]
    compensation_fingerprint: str
    bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)

    def __init__(
        self,
        ordered_handler_bindings: tuple[CompensationHandlerBindingV1, ...],
        compensation_fingerprint: str,
        bundle_instance_token: BundleInstanceToken,
        *,
        _seal: object,
    ) -> None:
        if _seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Compensation metadata views are Bundle-created")
        handler_bindings = _require_exact_tuple(
            ordered_handler_bindings,
            "Compensation handler bindings",
        )
        if not handler_bindings or any(
            type(binding) is not CompensationHandlerBindingV1 for binding in handler_bindings
        ):
            raise TypeError("Compensation view requires exact handler bindings")
        typed_handlers = cast(
            tuple[CompensationHandlerBindingV1, ...],
            handler_bindings,
        )
        if tuple(binding.ordinal for binding in typed_handlers) != tuple(
            range(1, len(typed_handlers) + 1)
        ):
            raise ValueError("Compensation handlers require contiguous ordinals")
        object.__setattr__(self, "ordered_handler_bindings", typed_handlers)
        object.__setattr__(
            self,
            "compensation_fingerprint",
            _require_sha256_fingerprint(
                compensation_fingerprint,
                "Compensation fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "bundle_instance_token",
            _require_bundle_instance_token(bundle_instance_token),
        )


_BundleView: TypeAlias = (
    ProviderToolMetadataView
    | ToolDiscoveryMetadataView
    | ToolAuthorityMetadataView
    | ToolOperationMetadataView
    | LegacyDeterministicBoundaryV1
    | CompensationMetadataView
)


def _bundle_view_integrity_snapshot(value: object) -> object:
    if type(value) is ProviderToolContract:
        value._ensure_provider_integrity()
        return (
            ProviderToolContract,
            id(value),
            value.name,
            value.description,
            id(value.payload),
            id(value.parameters),
        )
    if type(value) is BundleInstanceToken:
        return (BundleInstanceToken, id(value))
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value),
            id(value),
            tuple(
                (
                    descriptor.name,
                    _bundle_view_integrity_snapshot(
                        object.__getattribute__(value, descriptor.name)
                    ),
                )
                for descriptor in fields(value)
            ),
        )
    if isinstance(value, Mapping):
        return (
            type(value),
            id(value),
            tuple(
                (
                    _bundle_view_integrity_snapshot(key),
                    _bundle_view_integrity_snapshot(item),
                )
                for key, item in value.items()
            ),
        )
    if type(value) is tuple:
        return (
            tuple,
            id(value),
            tuple(_bundle_view_integrity_snapshot(item) for item in value),
        )
    if value is None or type(value) in {bool, int, float, str}:
        return (type(value), value)
    if isinstance(
        value,
        (
            ToolDomain,
            ToolCapability,
            ProviderVisibility,
            OperationKind,
            UndoPolicy,
            UndoPayloadKind,
            CompensationKind,
        ),
    ):
        return (type(value), value.value)
    raise TypeError(f"unsupported Bundle view component: {type(value).__name__}")


class _BundleViewFactory(TransientToolRuntimeValue):
    """One-shot issuer and exact-identity registry used by the future Bundle."""

    __slots__ = ("_bundle_instance_token",)
    _bundle_instance_token: BundleInstanceToken

    def __new__(cls, seal: object | None = None) -> "_BundleViewFactory":
        if seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Bundle view factories are composition-created")
        return object.__new__(cls)

    def __init__(self, seal: object | None = None) -> None:
        if seal is not _BUNDLE_VALUE_CONSTRUCTION_SEAL:
            raise TypeError("Bundle view factories are composition-created")
        object.__setattr__(
            self,
            "_bundle_instance_token",
            BundleInstanceToken(_BUNDLE_VALUE_CONSTRUCTION_SEAL),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Bundle view factory components are sealed")

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        return cast(BundleInstanceToken, object.__getattribute__(self, "_bundle_instance_token"))

    def _register(self, view: _BundleView) -> _BundleView:
        self.bundle_instance_token._register_view(view)
        return view

    def require_issued(self, view: object, expected_type: type[_BundleView]) -> None:
        self.bundle_instance_token._require_view(view, expected_type)

    def finalize(self) -> None:
        self.bundle_instance_token._finalize_views()

    def provider_view(
        self,
        *,
        ordered_contracts: tuple[ProviderToolContract, ...],
        provider_boundary_fingerprint: str,
    ) -> ProviderToolMetadataView:
        return cast(
            ProviderToolMetadataView,
            self._register(
                ProviderToolMetadataView(
                    ordered_contracts,
                    provider_boundary_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )

    def discovery_view(
        self,
        *,
        ordered_entries: tuple[ToolDiscoveryEntryV1, ...],
        policy: ToolDiscoveryPolicyV1,
        discovery_fingerprint: str,
    ) -> ToolDiscoveryMetadataView:
        return cast(
            ToolDiscoveryMetadataView,
            self._register(
                ToolDiscoveryMetadataView(
                    ordered_entries,
                    policy,
                    discovery_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )

    def authority_view(
        self,
        *,
        entries: Mapping[str, ToolAuthorityEntryV1],
        authority_manifest_fingerprint: str,
    ) -> ToolAuthorityMetadataView:
        return cast(
            ToolAuthorityMetadataView,
            self._register(
                ToolAuthorityMetadataView(
                    entries,
                    authority_manifest_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )

    def operation_view(
        self,
        *,
        entries: Mapping[str, ToolOperationEntryV1],
        operation_fingerprint: str,
    ) -> ToolOperationMetadataView:
        return cast(
            ToolOperationMetadataView,
            self._register(
                ToolOperationMetadataView(
                    entries,
                    operation_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )

    def legacy_boundary(
        self,
        *,
        ordered_adapter_bindings: tuple[LegacyAdapterBindingV1, ...],
        initial_route_bindings: tuple[LegacyInitialRouteBindingV1, ...],
        legacy_boundary_fingerprint: str,
    ) -> LegacyDeterministicBoundaryV1:
        return cast(
            LegacyDeterministicBoundaryV1,
            self._register(
                LegacyDeterministicBoundaryV1(
                    ordered_adapter_bindings,
                    initial_route_bindings,
                    legacy_boundary_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )

    def compensation_view(
        self,
        *,
        ordered_handler_bindings: tuple[CompensationHandlerBindingV1, ...],
        compensation_fingerprint: str,
    ) -> CompensationMetadataView:
        return cast(
            CompensationMetadataView,
            self._register(
                CompensationMetadataView(
                    ordered_handler_bindings,
                    compensation_fingerprint,
                    self.bundle_instance_token,
                    _seal=_BUNDLE_VALUE_CONSTRUCTION_SEAL,
                )
            ),
        )


def _new_bundle_view_factory() -> _BundleViewFactory:
    return _BundleViewFactory(_BUNDLE_VALUE_CONSTRUCTION_SEAL)


def _require_bundle_manifest_names(
    projection: Mapping[str, object],
    expected_names: tuple[str, ...],
) -> None:
    if projection.get("schema_version") != 1:
        raise ValueError("Bundle Manifest has an unknown schema version")
    if projection.get("metadata_version") != "tool-surface-metadata-v1":
        raise ValueError("Bundle Manifest has an unknown metadata version")
    typed_tools = projection.get("typed_tools")
    if not isinstance(typed_tools, (list, tuple)):
        raise TypeError("Bundle Manifest typed_tools must be an ordered sequence")
    names: list[str] = []
    for entry in typed_tools:
        if type(entry) is str:
            names.append(entry)
            continue
        if isinstance(entry, Mapping) and type(entry.get("provider_name")) is str:
            names.append(cast(str, entry["provider_name"]))
            continue
        raise TypeError("Bundle Manifest typed_tools entries have an invalid shape")
    if tuple(names) != expected_names:
        raise ValueError("Bundle Manifest and Typed Catalog names/order mismatch")


def _bundle_operation_projection(entry: ToolOperationEntryV1) -> dict[str, object]:
    operation = entry.operation
    projection: dict[str, object] = {
        "ordinal": entry.ordinal,
        "provider_name": entry.provider_name,
        "confirmation_policy": entry.confirmation_policy,
        "editable_fields": [
            {
                "field": editable.field,
                "value_type": editable.value_type,
                "options": None if editable.options is None else list(editable.options),
                "clearable": editable.clearable,
                "clear_value": editable.clear_value,
            }
            for editable in entry.editable_fields
        ],
    }
    if type(operation) is ReadOperationMetadataV1:
        projection["operation"] = {"kind": operation.kind.value}
        return projection
    if type(operation) is not WriteOperationMetadataV1:
        raise TypeError("Bundle operation projection has an invalid descriptor")
    projection["operation"] = {
        "kind": operation.kind.value,
        "adapter_kind": operation.adapter_kind,
        "result_contract": operation.result_contract,
        "result_bytes": operation.result_bytes,
        "visible_bytes": operation.visible_bytes,
        "transport_bytes": operation.transport_bytes,
        "undo_bytes": operation.undo_bytes,
        "undo_policy": operation.undo_policy.value,
        "undo_payload_kind": (
            None if operation.undo_payload_kind is None else operation.undo_payload_kind.value
        ),
        "compensation_kind": (
            None if operation.compensation_kind is None else operation.compensation_kind.value
        ),
        "undo_contract_version": operation.undo_contract_version,
        "undo_builder_id": operation.undo_builder_id,
        "undo_seed_phase": operation.undo_seed_phase,
    }
    return projection


def _legacy_view_projection(
    value: Mapping[str, object],
    *,
    production: bool,
) -> tuple[
    tuple[LegacyAdapterBindingV1, ...],
    tuple[LegacyInitialRouteBindingV1, ...],
]:
    frozen = freeze_json(value)
    if type(frozen) is not MappingProxyType:
        raise TypeError("Legacy boundary must be a JSON object")
    projection = cast(dict[str, object], materialize_json(frozen))
    if production:
        expected_keys = (
            "boundary_version",
            "provider_visibility",
            "adapter_kind",
            "ordered_names",
            "chained_policies",
            "initial_route_bindings",
        )
        if tuple(projection) != expected_keys:
            raise ValueError("production Legacy boundary has an invalid exact shape")
        raw_names = projection["ordered_names"]
        visibility = projection["provider_visibility"]
        adapter_kind = projection["adapter_kind"]
        raw_policies = projection["chained_policies"]
        raw_routes = projection["initial_route_bindings"]
    else:
        if tuple(projection) != ("visibility", "ordered_adapters"):
            raise ValueError("generic Legacy boundary has an invalid exact shape")
        raw_names = projection["ordered_adapters"]
        visibility = projection["visibility"]
        adapter_kind = "legacy_deterministic"
        raw_policies = None
        raw_routes = []
    if not isinstance(raw_names, list) or not raw_names:
        raise ValueError("Legacy boundary requires ordered adapter names")
    names: list[str] = []
    for name in raw_names:
        names.append(_require_static_text(name, "Legacy adapter name"))
    if len(set(names)) != len(names):
        raise ValueError("Legacy adapter names must be unique")
    if type(visibility) is not str:
        raise TypeError("Legacy boundary visibility must be exact text")
    if type(adapter_kind) is not str:
        raise TypeError("Legacy adapter kind must be exact text")
    if raw_policies is None:
        policies: list[object] = [None] * len(names)
    elif isinstance(raw_policies, list) and len(raw_policies) == len(names):
        policies = raw_policies
    else:
        raise ValueError("Legacy chained policies do not match adapter order")
    adapter_bindings = tuple(
        LegacyAdapterBindingV1(
            ordinal=ordinal,
            name=name,
            provider_visibility=cast(Literal["forbidden"], visibility),
            adapter_kind=cast(Literal["legacy_deterministic"], adapter_kind),
            chained_policy=(None if policy is None else cast(str, policy)),
        )
        for ordinal, (name, policy) in enumerate(zip(names, policies), start=1)
    )
    if not isinstance(raw_routes, list):
        raise TypeError("Legacy initial route bindings must be an ordered sequence")
    route_bindings: list[LegacyInitialRouteBindingV1] = []
    for route in raw_routes:
        if not isinstance(route, Mapping):
            raise TypeError("Legacy initial route binding must be an object")
        route_bindings.append(
            LegacyInitialRouteBindingV1(
                route_source=_require_static_text(
                    route.get("route_source"),
                    "Legacy initial route source",
                ),
                adapter_ordinal=cast(int, route.get("adapter_ordinal")),
            )
        )
    return adapter_bindings, tuple(route_bindings)


def _compensation_view_projection(
    value: Mapping[str, object],
    *,
    production: bool,
) -> tuple[CompensationHandlerBindingV1, ...]:
    frozen = freeze_json(value)
    if type(frozen) is not MappingProxyType:
        raise TypeError("Compensation projection must be a JSON object")
    projection = cast(dict[str, object], materialize_json(frozen))
    if production:
        if tuple(projection) != ("ordered_handler_bindings",):
            raise ValueError("production Compensation projection has an invalid exact shape")
        existing = projection["ordered_handler_bindings"]
        if not isinstance(existing, list):
            raise TypeError("Compensation handler bindings must be an ordered sequence")
        if any(not isinstance(item, Mapping) for item in existing):
            raise TypeError("Compensation handler binding must be an object")
        return tuple(
            CompensationHandlerBindingV1(
                ordinal=cast(int, cast(Mapping[str, object], item).get("ordinal")),
                compensation_kind=_require_static_text(
                    cast(Mapping[str, object], item).get("compensation_kind"),
                    "Compensation operation",
                ),
                handler_id=_require_static_text(
                    cast(Mapping[str, object], item).get("handler_id"),
                    "Compensation handler id",
                ),
            )
            for item in existing
        )
    if tuple(projection) != ("ordered_operations", "handler_ids"):
        raise ValueError("generic Compensation projection has an invalid exact shape")
    operations = projection["ordered_operations"]
    handler_ids = projection["handler_ids"]
    if not isinstance(operations, list) or not isinstance(handler_ids, list):
        raise TypeError("Compensation projection requires ordered operations and handlers")
    if not operations or len(operations) != len(handler_ids):
        raise ValueError("Compensation operations and handlers do not match")
    bindings: list[CompensationHandlerBindingV1] = []
    for ordinal, (operation, handler_id) in enumerate(
        zip(operations, handler_ids),
        start=1,
    ):
        bindings.append(
            CompensationHandlerBindingV1(
                ordinal=ordinal,
                compensation_kind=_require_static_text(
                    operation,
                    "Compensation operation",
                ),
                handler_id=_require_static_text(handler_id, "Compensation handler id"),
            )
        )
    return tuple(bindings)


def _canonical_legacy_view_projection(
    adapter_bindings: tuple[LegacyAdapterBindingV1, ...],
    route_bindings: tuple[LegacyInitialRouteBindingV1, ...],
) -> dict[str, object]:
    return {
        "ordered_adapter_bindings": [
            {
                "ordinal": binding.ordinal,
                "name": binding.name,
                "provider_visibility": binding.provider_visibility,
                "adapter_kind": binding.adapter_kind,
                "chained_policy": binding.chained_policy,
            }
            for binding in adapter_bindings
        ],
        "initial_route_bindings": [
            {
                "route_source": binding.route_source,
                "adapter_ordinal": binding.adapter_ordinal,
            }
            for binding in route_bindings
        ],
    }


def _canonical_compensation_view_projection(
    bindings: tuple[CompensationHandlerBindingV1, ...],
) -> dict[str, object]:
    return {
        "ordered_handler_bindings": [
            {
                "ordinal": binding.ordinal,
                "compensation_kind": binding.compensation_kind,
                "handler_id": binding.handler_id,
            }
            for binding in bindings
        ]
    }


class _BundleLeaseIssuerState:
    __slots__ = ("lock", "generation")

    def __init__(self) -> None:
        self.lock = RLock()
        self.generation = 0


class ToolMetadataBundleV1(TransientToolRuntimeValue):
    """One immutable metadata composition with exact process-local provenance."""

    _typed_catalog: ToolCatalog
    _view_factory: _BundleViewFactory
    _provider_view: ProviderToolMetadataView
    _discovery_view: ToolDiscoveryMetadataView
    _authority_view: ToolAuthorityMetadataView
    _operation_view: ToolOperationMetadataView
    _legacy_boundary_view: LegacyDeterministicBoundaryV1
    _compensation_view: CompensationMetadataView
    _bundle_fingerprint: str
    _lease_state: _BundleLeaseIssuerState
    _integrity_seal: tuple[object, ...]

    __slots__ = (
        "_typed_catalog",
        "_view_factory",
        "_provider_view",
        "_discovery_view",
        "_authority_view",
        "_operation_view",
        "_legacy_boundary_view",
        "_compensation_view",
        "_bundle_fingerprint",
        "_lease_state",
        "_integrity_seal",
    )

    def __init__(
        self,
        *,
        typed_catalog: ToolCatalog,
        manifest: ToolMetadataManifestV1 | Mapping[str, object],
        legacy_boundary: Mapping[str, object],
        compensation: Mapping[str, object],
    ) -> None:
        if hasattr(self, "_integrity_seal"):
            raise TypeError("ToolMetadataBundleV1 is already initialized and sealed")
        from offerpilot.ai.tool_runtime.catalog import (
            ToolCatalog,
            ToolMetadataManifestV1,
            compile_tool_metadata_manifest,
        )

        if type(typed_catalog) is not ToolCatalog:
            raise TypeError("Bundle requires an exact ToolCatalog")
        specs = typed_catalog.specs
        names = tuple(spec.name for spec in specs)
        if type(manifest) is ToolMetadataManifestV1:
            production_manifest = True
            manifest_projection = manifest.to_dict()
            bundle_fingerprint = manifest.fingerprint
            if compile_tool_metadata_manifest(specs).to_dict() != manifest_projection:
                raise ValueError("Bundle Manifest does not match complete Typed Catalog metadata")
        elif isinstance(manifest, Mapping):
            production_manifest = False
            frozen_manifest = freeze_json(manifest)
            if type(frozen_manifest) is not MappingProxyType:
                raise TypeError("Bundle Manifest must be a JSON object")
            manifest_projection = cast(
                dict[str, object],
                materialize_json(frozen_manifest),
            )
            bundle_fingerprint = canonical_sha256(frozen_manifest)
        else:
            raise TypeError("Bundle requires an exact Manifest or generic test projection")
        _require_bundle_manifest_names(manifest_projection, names)
        policy_projection = manifest_projection.get("discovery_policy")
        if not isinstance(policy_projection, Mapping):
            raise TypeError("Bundle Manifest discovery policy must be an object")
        policy = ToolDiscoveryPolicyV1(policy_projection)

        provider_contracts = typed_catalog.provider_contracts()
        provider_payloads = typed_catalog.materialize_provider_payloads()
        if len(provider_contracts) != len(specs) or len(provider_payloads) != len(specs):
            raise ValueError("Bundle Provider projection cardinality mismatch")
        provider_fingerprint = canonical_sha256(
            freeze_json(
                {
                    "schema": "provider-tool-boundary-v1",
                    "ordered_tools": provider_payloads,
                }
            )
        )

        discovery_entries = tuple(
            ToolDiscoveryEntryV1(
                ordinal=ordinal,
                provider_name=spec.name,
                provider_contract=spec.contract,
                domains=spec.metadata.domains,
                dependencies=spec.metadata.dependencies,
                provider_visibility=spec.metadata.provider_visibility,
            )
            for ordinal, spec in enumerate(specs, start=1)
        )
        discovery_fingerprint = canonical_sha256(
            freeze_json(
                {
                    "ordered_entries": [
                        {
                            "ordinal": entry.ordinal,
                            "provider_name": entry.provider_name,
                            "provider_contract_fingerprint": canonical_sha256(freeze_json(payload)),
                            "domains": [domain.value for domain in entry.domains],
                            "dependencies": list(entry.dependencies),
                            "provider_visibility": entry.provider_visibility.value,
                        }
                        for entry, payload in zip(discovery_entries, provider_payloads)
                    ],
                    "policy": materialize_json(policy.projection),
                }
            )
        )

        authority_entries = {
            spec.name: ToolAuthorityEntryV1(
                ordinal=ordinal,
                provider_name=spec.name,
                provider_visibility=spec.metadata.provider_visibility,
                required_capabilities=spec.metadata.required_capabilities,
                binding=spec.metadata.binding,
                confirmation_policy=spec.metadata.confirmation_policy,
                operation_kind=spec.metadata.operation.kind,
            )
            for ordinal, spec in enumerate(specs, start=1)
        }
        authority_projection = typed_catalog.authority_manifest
        authority_fingerprint = canonical_sha256(freeze_json(authority_projection))

        operation_entries = {
            spec.name: ToolOperationEntryV1(
                ordinal=ordinal,
                provider_name=spec.name,
                confirmation_policy=spec.metadata.confirmation_policy,
                editable_fields=spec.metadata.editable_fields,
                operation=spec.metadata.operation,
            )
            for ordinal, spec in enumerate(specs, start=1)
        }
        operation_fingerprint = canonical_sha256(
            freeze_json(
                [_bundle_operation_projection(entry) for entry in operation_entries.values()]
            )
        )

        if production_manifest:
            manifest_legacy = manifest_projection.get("legacy_boundary")
            if not isinstance(manifest_legacy, Mapping) or canonical_json_bytes(
                freeze_json(manifest_legacy)
            ) != canonical_json_bytes(freeze_json(legacy_boundary)):
                raise ValueError("actual Legacy boundary does not match the exact Manifest")
        adapter_bindings, route_bindings = _legacy_view_projection(
            legacy_boundary,
            production=production_manifest,
        )
        compensation_bindings = _compensation_view_projection(
            compensation,
            production=production_manifest,
        )
        if production_manifest:
            expected_compensations = manifest_projection.get("compensation_operation_order")
            actual_compensations = [binding.compensation_kind for binding in compensation_bindings]
            if expected_compensations != actual_compensations:
                raise ValueError(
                    "actual Compensation handlers do not match the exact Manifest order"
                )
        legacy_fingerprint = canonical_sha256(
            freeze_json(_canonical_legacy_view_projection(adapter_bindings, route_bindings))
        )
        compensation_fingerprint = canonical_sha256(
            freeze_json(_canonical_compensation_view_projection(compensation_bindings))
        )

        factory = _new_bundle_view_factory()
        provider_view = factory.provider_view(
            ordered_contracts=provider_contracts,
            provider_boundary_fingerprint=provider_fingerprint,
        )
        discovery_view = factory.discovery_view(
            ordered_entries=discovery_entries,
            policy=policy,
            discovery_fingerprint=discovery_fingerprint,
        )
        authority_view = factory.authority_view(
            entries=authority_entries,
            authority_manifest_fingerprint=authority_fingerprint,
        )
        operation_view = factory.operation_view(
            entries=operation_entries,
            operation_fingerprint=operation_fingerprint,
        )
        legacy_view = factory.legacy_boundary(
            ordered_adapter_bindings=adapter_bindings,
            initial_route_bindings=route_bindings,
            legacy_boundary_fingerprint=legacy_fingerprint,
        )
        compensation_view = factory.compensation_view(
            ordered_handler_bindings=compensation_bindings,
            compensation_fingerprint=compensation_fingerprint,
        )
        factory.finalize()

        object.__setattr__(self, "_typed_catalog", typed_catalog)
        object.__setattr__(self, "_view_factory", factory)
        object.__setattr__(self, "_provider_view", provider_view)
        object.__setattr__(self, "_discovery_view", discovery_view)
        object.__setattr__(self, "_authority_view", authority_view)
        object.__setattr__(self, "_operation_view", operation_view)
        object.__setattr__(self, "_legacy_boundary_view", legacy_view)
        object.__setattr__(self, "_compensation_view", compensation_view)
        object.__setattr__(self, "_bundle_fingerprint", bundle_fingerprint)
        object.__setattr__(self, "_lease_state", _BundleLeaseIssuerState())
        object.__setattr__(self, "_integrity_seal", self._integrity_snapshot())

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError(f"ToolMetadataBundleV1 component {name} is sealed")
        object.__setattr__(self, name, value)

    def _integrity_snapshot(self) -> tuple[object, ...]:
        bundle_token = self._view_factory.bundle_instance_token
        bundle_token._ensure_integrity()
        return (
            id(self._typed_catalog),
            id(self._view_factory),
            id(bundle_token),
            id(self._provider_view),
            id(self._discovery_view),
            id(self._authority_view),
            id(self._operation_view),
            id(self._legacy_boundary_view),
            id(self._compensation_view),
            self._bundle_fingerprint,
            id(self._lease_state),
        )

    def _ensure_integrity(self) -> None:
        try:
            _ = self._typed_catalog.specs
            if self._integrity_snapshot() != self._integrity_seal:
                raise ValueError("metadata Bundle integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("metadata Bundle integrity drift") from exc

    @property
    def bundle_fingerprint(self) -> str:
        self._ensure_integrity()
        return self._bundle_fingerprint

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._ensure_integrity()
        return self._view_factory.bundle_instance_token

    def provider_view(self) -> ProviderToolMetadataView:
        self._ensure_integrity()
        self._view_factory.require_issued(self._provider_view, ProviderToolMetadataView)
        return self._provider_view

    def discovery_view(self) -> ToolDiscoveryMetadataView:
        self._ensure_integrity()
        self._view_factory.require_issued(self._discovery_view, ToolDiscoveryMetadataView)
        return self._discovery_view

    def authority_view(self) -> ToolAuthorityMetadataView:
        self._ensure_integrity()
        self._view_factory.require_issued(self._authority_view, ToolAuthorityMetadataView)
        return self._authority_view

    def operation_view(self) -> ToolOperationMetadataView:
        self._ensure_integrity()
        self._view_factory.require_issued(self._operation_view, ToolOperationMetadataView)
        return self._operation_view

    def legacy_boundary(self) -> LegacyDeterministicBoundaryV1:
        self._ensure_integrity()
        self._view_factory.require_issued(
            self._legacy_boundary_view,
            LegacyDeterministicBoundaryV1,
        )
        return self._legacy_boundary_view

    def compensation_view(self) -> CompensationMetadataView:
        self._ensure_integrity()
        self._view_factory.require_issued(self._compensation_view, CompensationMetadataView)
        return self._compensation_view

    def open_segment_lease(self) -> SegmentToolCatalogLease:
        from offerpilot.ai.tool_runtime.catalog import _open_segment_tool_catalog_lease

        self._ensure_integrity()
        with self._lease_state.lock:
            self._ensure_integrity()
            generation = self._lease_state.generation + 1
            self._lease_state.generation = generation
            return _open_segment_tool_catalog_lease(
                catalog=self._typed_catalog,
                bundle_instance_token=self.bundle_instance_token,
                generation=generation,
            )


class _RuntimeAsdictGuard:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("runtime binding cannot be serialized")


_RUNTIME_ASDICT_GUARD = _RuntimeAsdictGuard()


def _named_callable_identity(
    value: object, field_name: str
) -> tuple[Callable[..., object], str, str]:
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
    "BundleInstanceToken",
    "CompensationHandlerBindingV1",
    "CompensationMetadataView",
    "FrozenJSONArray",
    "FrozenJSONObject",
    "FrozenJSONValue",
    "JSONScalar",
    "JSONValue",
    "BindingResolverDescriptorV1",
    "EditableFieldMetadataV1",
    "EditableValueType",
    "LegacyDeterministicBoundaryV1",
    "LegacyAdapterBindingV1",
    "LegacyInitialRouteBindingV1",
    "ProviderToolMetadataView",
    "ReadOperationMetadataV1",
    "ResolverImplementationBinding",
    "ToolAuthorityEntryV1",
    "ToolAuthorityMetadataView",
    "ToolBindingMetadataV1",
    "ToolDiscoveryEntryV1",
    "ToolDiscoveryMetadataView",
    "ToolDiscoveryPolicyV1",
    "ToolOperationMetadataV1",
    "ToolOperationEntryV1",
    "ToolOperationMetadataView",
    "ToolMetadataBundleV1",
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
