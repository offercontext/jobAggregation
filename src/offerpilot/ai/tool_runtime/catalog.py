from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from offerpilot.ai.tool_authority.policy import validate_startup_policy
from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    FailureCategory,
    ProviderToolContract,
    ToolSpec,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    BindingResolverDescriptorV1,
    EditableFieldMetadataV1,
    FrozenJSONObject,
    ReadOperationMetadataV1,
    ResolverImplementationBinding,
    ToolBindingMetadataV1,
    ToolPresentationBindingV1,
    ToolSurfaceMetadataV1,
    UndoBuilderBinding,
    WriteOperationMetadataV1,
    freeze_json,
    materialize_json,
    validate_tool_spec_components,
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
from offerpilot.ai.tool_runtime.protocol_seals import approved_legacy_boundary_input
from offerpilot.ai.tool_runtime.validation import compile_tool_schema


_FAILURE_CATEGORIES = frozenset(
    {
        "validation_error",
        "permission_denied",
        "confirmation_rejected",
        "stale_state",
        "conflict",
        "not_found",
        "provider_error",
        "internal_error",
    }
)
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_DISCOVERY_POLICY = freeze_json(
    {
        "selector_version": "tool-surface-selector-v1",
        "discovery_policy_version": "tool-discovery-policy-v1",
        "page_domains": [
            {"page_kind": "workspace", "domains": []},
            {"page_kind": "applications", "domains": ["applications"]},
            {
                "page_kind": "application",
                "domains": ["applications", "events", "jd", "notes", "offers", "resumes"],
            },
            {"page_kind": "calendar", "domains": ["applications", "events"]},
            {"page_kind": "notes", "domains": ["applications", "notes"]},
            {"page_kind": "offers", "domains": ["applications", "offers"]},
            {"page_kind": "resumes", "domains": ["jd", "resumes"]},
        ],
        "attachment_domains": [
            {"attachment_kind": "resume", "domains": ["resumes"]},
            {"attachment_kind": "job_description", "domains": ["applications", "jd"]},
            {"attachment_kind": "image", "domains": []},
            {"attachment_kind": "document", "domains": []},
        ],
        "lexical_rules": [
            {
                "domain": "applications",
                "terms": ["投递", "申请", "application", "company", "岗位", "职位", "改成 offer"],
            },
            {
                "domain": "events",
                "terms": ["日程", "面试时间", "笔试", "deadline", "event", "提醒"],
            },
            {"domain": "notes", "terms": ["复盘", "笔记", "note", "记录"]},
            {"domain": "offers", "terms": ["offer", "薪资", "谈薪", "待遇", "比较"]},
            {"domain": "resumes", "terms": ["简历", "resume", "经历", "求职意向"]},
            {"domain": "jd", "terms": ["jd", "职位描述", "job description", "匹配分析"]},
        ],
        "no_signal_behavior": "full_typed_catalog",
        "invalid_input_behavior": "fail_closed",
    }
)
_LEGACY_PREPUBLICATION_POLICY = freeze_json(
    {
        "chained_policies": ["same_adapter_only", "forbidden", "forbidden"],
        "initial_route_bindings": [
            {"route_source": "jd_clarification", "adapter_ordinal": 1},
            {"route_source": "jd_deterministic_action", "adapter_ordinal": 1},
            {"route_source": "submission_snapshot_action", "adapter_ordinal": 2},
            {"route_source": "outcome_recording_action", "adapter_ordinal": 3},
        ],
    }
)
_WRITE_BUDGETS = MappingProxyType(
    {
        "result_bytes": 512 * 1024,
        "visible_bytes": 256 * 1024,
        "transport_bytes": 128 * 1024,
        "undo_bytes": 64 * 1024,
    }
)
_COMPENSATION_OPERATION_ORDER = (
    CompensationKind.UNDO_UPDATE_APPLICATION_STATUS.value,
    CompensationKind.UNDO_CREATE_APPLICATION.value,
    CompensationKind.UNDO_CREATE_APPLICATION_EVENT.value,
    CompensationKind.UNDO_ADD_NOTE.value,
)


def _external_operation_kind(metadata: ToolSurfaceMetadataV1) -> str:
    return "write" if type(metadata.operation) is WriteOperationMetadataV1 else "read"


def _legacy_manifest_boundary() -> dict[str, object]:
    ordered_names, provider_visibility, adapter_kind = approved_legacy_boundary_input()
    internal_policy = cast(dict[str, object], materialize_json(_LEGACY_PREPUBLICATION_POLICY))
    return {
        "boundary_version": "legacy-deterministic-boundary-v1",
        "provider_visibility": provider_visibility,
        "adapter_kind": adapter_kind,
        "ordered_names": list(ordered_names),
        **internal_policy,
    }


def _ordered_compensations(
    values: Sequence[tuple[str | None, int, str]],
) -> tuple[str, ...]:
    if len(values) != 4:
        raise ValueError("compensation bindings do not match the closed V1 set")
    actual: set[CompensationKind] = set()
    for _phase, _ordinal, value in values:
        try:
            kind = CompensationKind(value)
        except ValueError as exc:
            raise ValueError("compensation binding kind is invalid") from exc
        if kind in actual:
            raise ValueError("compensation bindings must be unique")
        actual.add(kind)
    ordered = sorted(
        values,
        key=lambda item: (0 if item[0] == "before_execute" else 1, item[1]),
    )
    actual_order = tuple(value for _phase, _ordinal, value in ordered)
    if actual_order != _COMPENSATION_OPERATION_ORDER:
        raise ValueError("compensation bindings do not match the closed V1 order")
    return _COMPENSATION_OPERATION_ORDER


def _resolver_projection(binding: ResolverImplementationBinding) -> dict[str, object]:
    descriptor = binding.descriptor
    return {
        "resolver_id": descriptor.resolver_id,
        "entity_kind": descriptor.entity_kind,
        "arg_path": descriptor.arg_path,
        "presence": descriptor.presence,
        "identity_type": descriptor.identity_type,
    }


def authority_manifest_for_specs(
    specs: Sequence[ToolSpec[Any, Any]],
    *,
    strict: bool = True,
) -> dict[str, object]:
    """Project canonical metadata into the unchanged Authority Manifest V1."""

    tools: list[dict[str, object]] = []
    for ordinal, spec in enumerate(specs, start=1):
        if strict:
            _validate_spec(spec)
        metadata = spec.metadata
        tools.append(
            {
                "ordinal": ordinal,
                "name": spec.name,
                "kind": _external_operation_kind(metadata),
                "confirmation_policy": metadata.confirmation_policy,
                "required_capabilities": [value.value for value in metadata.required_capabilities],
                "binding": {
                    "kind": metadata.binding.contract.kind,
                    "entity_kind": metadata.binding.contract.entity_kind,
                },
                "resolvers": [_resolver_projection(binding) for binding in spec.resolver_bindings],
            }
        )
    return {"schema_version": 1, "tools": tools}


def _canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _structure_identity_snapshot(value: object) -> object:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return (
            "dict",
            id(mapping),
            tuple(
                (
                    _structure_identity_snapshot(key),
                    _structure_identity_snapshot(item),
                )
                for key, item in mapping.items()
            ),
        )
    if type(value) is list:
        list_values = cast(list[object], value)
        return (
            "list",
            id(list_values),
            tuple(_structure_identity_snapshot(item) for item in list_values),
        )
    if type(value) is tuple:
        tuple_values = cast(tuple[object, ...], value)
        return (
            "tuple",
            id(tuple_values),
            tuple(_structure_identity_snapshot(item) for item in tuple_values),
        )
    if type(value) is frozenset:
        members = cast(frozenset[object], value)
        return (
            "frozenset",
            id(members),
            frozenset(_structure_identity_snapshot(item) for item in members),
        )
    return (type(value), value)


def _editable_projection(value: EditableFieldMetadataV1) -> dict[str, object]:
    return {
        "field": value.field,
        "value_type": value.value_type,
        "options": None if value.options is None else list(value.options),
        "clearable": value.clearable,
        "clear_value": value.clear_value,
    }


def _operation_projection(value: object) -> dict[str, object]:
    if type(value) is ReadOperationMetadataV1:
        return {"kind": OperationKind.READ.value}
    if type(value) is not WriteOperationMetadataV1:
        raise TypeError("tool operation metadata has an unknown union member")
    operation = value
    return {
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


def _metadata_projection(metadata: ToolSurfaceMetadataV1) -> dict[str, object]:
    return {
        "domains": [value.value for value in metadata.domains],
        "dependencies": list(metadata.dependencies),
        "provider_visibility": metadata.provider_visibility.value,
        "required_capabilities": [value.value for value in metadata.required_capabilities],
        "binding": {
            "contract": {
                "kind": metadata.binding.contract.kind,
                "entity_kind": metadata.binding.contract.entity_kind,
            },
            "resolver_descriptors": [
                {
                    "resolver_id": descriptor.resolver_id,
                    "entity_kind": descriptor.entity_kind,
                    "arg_path": descriptor.arg_path,
                    "presence": descriptor.presence,
                    "identity_type": descriptor.identity_type,
                }
                for descriptor in metadata.binding.resolver_descriptors
            ],
        },
        "confirmation_policy": metadata.confirmation_policy,
        "editable_fields": [_editable_projection(value) for value in metadata.editable_fields],
        "operation": _operation_projection(metadata.operation),
    }


def _validate_spec(spec: ToolSpec[Any, Any]) -> None:
    if type(spec) is not ToolSpec:
        raise TypeError("catalog requires exact ToolSpec values")
    validate_tool_spec_components(
        provider_contract=spec.contract,
        metadata=spec.metadata,
        resolver_bindings=spec.resolver_bindings,
        undo_builder_binding=spec.undo_builder_binding,
        presentation=spec.presentation,
    )
    if not set(spec.declared_failure_categories).issubset(_FAILURE_CATEGORIES):
        raise ValueError("tool declares unsupported failure category")
    for mapping in spec.exception_map:
        category: FailureCategory = mapping.category
        if category not in spec.declared_failure_categories:
            raise ValueError("exception mapping category is not declared")


def build_tool_spec(
    *,
    contract: ProviderToolContract,
    domains: tuple[ToolDomain, ...],
    dependencies: tuple[str, ...],
    required_capability: ToolCapability,
    binding_contract: BindingContract,
    resolver_bindings: tuple[ResolverImplementationBinding, ...],
    confirmation_policy: str,
    editable_fields: tuple[EditableFieldMetadataV1, ...],
    operation: ReadOperationMetadataV1 | WriteOperationMetadataV1,
    undo_builder_binding: UndoBuilderBinding | None,
    presentation: ToolPresentationBindingV1,
    decoder: Any,
    executor: Any,
    preflight: Any = None,
    mutable_validator: Any = None,
    declared_failure_categories: frozenset[FailureCategory] = frozenset(),
    exception_map: tuple[Any, ...] = (),
    success_renderer: Any = None,
    result_metadata_projector: Any = None,
    schema_failure_renderer: Any = None,
) -> ToolSpec[Any, Any]:
    """Construct one final ToolSpec from its explicit declaration-site values."""

    metadata = ToolSurfaceMetadataV1(
        domains=domains,
        dependencies=dependencies,
        provider_visibility=ProviderVisibility.MODEL_ELIGIBLE,
        required_capabilities=(required_capability,),
        binding=ToolBindingMetadataV1(
            contract=binding_contract,
            resolver_descriptors=tuple(binding.descriptor for binding in resolver_bindings),
        ),
        confirmation_policy=cast(Any, confirmation_policy),
        editable_fields=editable_fields,
        operation=operation,
    )
    if undo_builder_binding is not None and undo_builder_binding.descriptor is not operation:
        raise ValueError("Undo binding must use the exact operation descriptor")
    return ToolSpec(
        contract=contract,
        metadata=metadata,
        resolver_bindings=resolver_bindings,
        undo_builder_binding=undo_builder_binding,
        decoder=decoder,
        executor=executor,
        presentation=presentation,
        preflight=preflight,
        mutable_validator=mutable_validator,
        declared_failure_categories=declared_failure_categories,
        exception_map=exception_map,
        success_renderer=success_renderer,
        result_metadata_projector=result_metadata_projector,
        schema_failure_renderer=schema_failure_renderer,
    )


def _validate_dependencies(specs: Sequence[ToolSpec[Any, Any]]) -> None:
    names = tuple(spec.name for spec in specs)
    known = set(names)
    graph: dict[str, tuple[str, ...]] = {}
    for spec in specs:
        dependencies = spec.metadata.dependencies
        if spec.name in dependencies:
            raise ValueError("tool dependency cannot refer to itself")
        for dependency in dependencies:
            if dependency not in known:
                raise ValueError("tool dependency refers to an unknown Typed tool")
        graph[spec.name] = dependencies

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("tool dependency graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)


def _spec_integrity_snapshot(spec: ToolSpec[Any, Any]) -> tuple[object, ...]:
    return (
        id(spec.contract),
        id(spec.contract.payload),
        id(spec.contract.parameters),
        spec.contract.name,
        spec.contract.description,
        id(spec.metadata),
        _metadata_projection(spec.metadata),
        tuple(
            (id(binding), id(binding.descriptor), binding.implementation_id, id(binding.resolve))
            for binding in spec.resolver_bindings
        ),
        (
            None
            if spec.undo_builder_binding is None
            else (
                id(spec.undo_builder_binding),
                id(spec.undo_builder_binding.descriptor),
                spec.undo_builder_binding.implementation_id,
                id(spec.undo_builder_binding.capture_seed),
                id(spec.undo_builder_binding.build_undo),
            )
        ),
        id(spec.decoder),
        id(spec.executor),
        None if spec.preflight is None else id(spec.preflight),
        None if spec.mutable_validator is None else id(spec.mutable_validator),
        spec.declared_failure_categories,
        tuple(
            (
                id(mapping),
                id(mapping.exception_type),
                mapping.category,
                mapping.code,
                None if mapping.compatibility_detail is None else id(mapping.compatibility_detail),
            )
            for mapping in spec.exception_map
        ),
        None if spec.success_renderer is None else id(spec.success_renderer),
        (None if spec.result_metadata_projector is None else id(spec.result_metadata_projector)),
        id(spec.presentation),
        spec.presentation.implementation_id,
        id(spec.presentation.confirmation_description),
        id(spec.presentation.pending_details_projector),
        id(spec.presentation.success_summary_projector),
        None if spec.schema_failure_renderer is None else id(spec.schema_failure_renderer),
    )


class ToolCatalog:
    _ordered: tuple[ToolSpec[Any, Any], ...]
    _specs: dict[str, ToolSpec[Any, Any]]
    _integrity_snapshots: dict[int, tuple[object, ...]]
    _validators: dict[str, Draft202012Validator]
    _authority_manifest: dict[str, object]
    _catalog_seal: tuple[object, ...]

    __slots__ = (
        "_ordered",
        "_specs",
        "_integrity_snapshots",
        "_validators",
        "_authority_manifest",
        "_catalog_seal",
    )

    def __init__(
        self,
        specs: Sequence[ToolSpec[Any, Any]],
        *,
        expected_names: Sequence[str],
        authority_manifest: Mapping[str, object] | None = None,
    ) -> None:
        ordered = tuple(specs)
        expected = tuple(expected_names)
        names = tuple(spec.name for spec in ordered)
        if not names or names != expected or len(set(names)) != len(names):
            raise ValueError("tool catalog names/order mismatch")
        for spec in ordered:
            _validate_spec(spec)
        _validate_dependencies(ordered)

        strict_authority = authority_manifest is not None or len(ordered) == 25
        projected_manifest = authority_manifest_for_specs(ordered, strict=True)
        if authority_manifest is not None and projected_manifest != dict(authority_manifest):
            raise ValueError("authority manifest drift")
        if strict_authority:
            try:
                validate_startup_policy(projected_manifest)
            except ValueError as exc:
                raise ValueError("authority policy drift") from exc

        provider_payloads = materialize_provider_payloads(tuple(spec.contract for spec in ordered))
        if type(provider_payloads) is not list or len(provider_payloads) != len(ordered):
            raise ValueError("Provider materialization cardinality drift")
        validators: dict[str, Draft202012Validator] = {}
        for spec, payload in zip(ordered, provider_payloads):
            if type(payload) is not dict:
                raise TypeError("Provider materializer returned a non-object envelope")
            function = payload.get("function")
            if type(function) is not dict:
                raise TypeError("Provider materializer returned an invalid function envelope")
            parameters = function.get("parameters")
            if type(parameters) is not dict:
                raise TypeError("Provider materializer returned an invalid parameter schema")
            validators[spec.name] = compile_tool_schema(parameters)

        integrity_snapshots = {id(spec): _spec_integrity_snapshot(spec) for spec in ordered}
        specs_by_name = {spec.name: spec for spec in ordered}
        object.__setattr__(self, "_ordered", ordered)
        object.__setattr__(self, "_specs", specs_by_name)
        object.__setattr__(self, "_integrity_snapshots", integrity_snapshots)
        object.__setattr__(self, "_validators", validators)
        object.__setattr__(self, "_authority_manifest", projected_manifest)
        object.__setattr__(self, "_catalog_seal", self._topology_snapshot())

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError(f"ToolCatalog component {name} is sealed")
        object.__setattr__(self, name, value)

    def _topology_snapshot(self) -> tuple[object, ...]:
        return (
            id(self._ordered),
            tuple(id(spec) for spec in self._ordered),
            id(self._specs),
            tuple((name, id(spec)) for name, spec in self._specs.items()),
            id(self._validators),
            tuple(
                (
                    name,
                    id(validator),
                    _structure_identity_snapshot(validator.schema),
                )
                for name, validator in self._validators.items()
            ),
            id(self._authority_manifest),
            _structure_identity_snapshot(self._authority_manifest),
            id(self._integrity_snapshots),
            _structure_identity_snapshot(self._integrity_snapshots),
        )

    def _ensure_integrity(self) -> None:
        try:
            if self._topology_snapshot() != self._catalog_seal:
                raise ValueError("tool catalog topology integrity drift")
            for spec in self._ordered:
                expected = self._integrity_snapshots.get(id(spec))
                current = _spec_integrity_snapshot(spec)
                if expected is None or current != expected:
                    raise ValueError("tool catalog component integrity drift")
            _validate_dependencies(self._ordered)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("tool catalog integrity drift") from exc

    def resolve(self, name: str) -> ToolSpec[Any, Any] | None:
        self._ensure_integrity()
        return self._specs.get(name)

    def validator_for(self, name: str) -> Draft202012Validator:
        self._ensure_integrity()
        validator = self._validators[name]
        return validator.evolve(schema=copy.deepcopy(validator.schema))

    def provider_contracts(self) -> tuple[ProviderToolContract, ...]:
        self._ensure_integrity()
        return tuple(spec.contract for spec in self._ordered)

    def materialize_provider_payloads(self) -> list[dict[str, Any]]:
        """Materialize the ordered Provider boundary through its sole deep-copy API."""

        self._ensure_integrity()
        return cast(
            list[dict[str, Any]],
            materialize_provider_payloads(tuple(spec.contract for spec in self._ordered)),
        )

    def write_names(self) -> frozenset[str]:
        self._ensure_integrity()
        return frozenset(
            spec.name
            for spec in self._ordered
            if type(spec.metadata.operation) is WriteOperationMetadataV1
        )

    @property
    def specs(self) -> tuple[ToolSpec[Any, Any], ...]:
        self._ensure_integrity()
        return self._ordered

    @property
    def authority_manifest(self) -> dict[str, object]:
        self._ensure_integrity()
        return copy.deepcopy(self._authority_manifest)


class ToolMetadataManifestV1:
    """Deeply immutable exact V1 metadata projection."""

    _projection: FrozenJSONObject
    _projection_identity: int
    _projection_fingerprint: str

    __slots__ = ("_projection", "_projection_identity", "_projection_fingerprint")

    def __init__(self, projection: Mapping[str, object]) -> None:
        validate_tool_metadata_manifest(projection)
        frozen = freeze_json(projection)
        object.__setattr__(self, "_projection", frozen)
        object.__setattr__(self, "_projection_identity", id(frozen))
        object.__setattr__(
            self,
            "_projection_fingerprint",
            _canonical_fingerprint(materialize_json(frozen)),
        )

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError(f"ToolMetadataManifestV1 component {name} is sealed")
        object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        if id(self._projection) != self._projection_identity:
            raise ValueError("Manifest projection integrity drift")
        projection = materialize_json(self._projection)
        if _canonical_fingerprint(projection) != self._projection_fingerprint:
            raise ValueError("Manifest projection integrity drift")
        validate_tool_metadata_manifest(projection)
        return cast(dict[str, Any], projection)


def compile_tool_metadata_manifest(
    specs: Sequence[ToolSpec[Any, Any]],
) -> ToolMetadataManifestV1:
    ordered = tuple(specs)
    if len(ordered) != 25:
        raise ValueError("production metadata manifest requires exactly 25 Typed tools")
    for spec in ordered:
        _validate_spec(spec)
    _validate_dependencies(ordered)
    provider_payloads = materialize_provider_payloads(tuple(spec.contract for spec in ordered))
    if type(provider_payloads) is not list or len(provider_payloads) != len(ordered):
        raise ValueError("Provider materialization cardinality drift")

    typed_tools: list[dict[str, object]] = []
    compensation_bindings: list[tuple[str | None, int, str]] = []
    for ordinal, (spec, provider_payload) in enumerate(zip(ordered, provider_payloads), start=1):
        operation = spec.metadata.operation
        if type(operation) is WriteOperationMetadataV1 and operation.compensation_kind is not None:
            if operation.compensation_kind.value != f"undo:{spec.name}":
                raise ValueError("required Undo compensation does not match its Provider tool")
            compensation_bindings.append(
                (operation.undo_seed_phase, ordinal, operation.compensation_kind.value)
            )
        typed_tools.append(
            {
                "ordinal": ordinal,
                "provider_name": spec.name,
                "provider_contract_fingerprint": _canonical_fingerprint(provider_payload),
                **_metadata_projection(spec.metadata),
            }
        )
    projection: dict[str, object] = {
        "schema_version": 1,
        "metadata_version": "tool-surface-metadata-v1",
        "catalog_profile": "agent_typed_v1",
        "typed_tools": typed_tools,
        "discovery_policy": materialize_json(_DISCOVERY_POLICY),
        "legacy_boundary": _legacy_manifest_boundary(),
        "compensation_operation_order": list(_ordered_compensations(compensation_bindings)),
    }
    return ToolMetadataManifestV1(projection)


def _require_keys(value: object, expected: tuple[str, ...], field_name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be an object")
    if tuple(value.keys()) != expected:
        raise ValueError(f"{field_name} has unexpected keys or key order")
    return cast(dict[str, Any], value)


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    if len(value.encode("utf-8")) > 256:
        raise ValueError(f"{field_name} exceeds the static metadata byte limit")
    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{field_name} contains invalid control characters")
    return value


def _require_array(value: object, field_name: str) -> list[Any]:
    if type(value) is not list:
        raise TypeError(f"{field_name} must be an array")
    return value


def _has_exact_duplicates(values: Sequence[object]) -> bool:
    seen: set[tuple[type[object], object]] = set()
    for value in values:
        try:
            key = (type(value), value)
            if key in seen:
                return True
            seen.add(key)
        except TypeError as exc:
            raise TypeError("manifest arrays may contain only hashable scalar identities") from exc
    return False


def _validate_closed_projection(value: object, expected: object, field_name: str) -> None:
    if type(expected) is dict:
        expected_mapping = cast(dict[str, Any], expected)
        actual = _require_keys(value, tuple(expected_mapping), field_name)
        for key, expected_item in expected_mapping.items():
            _validate_closed_projection(actual[key], expected_item, f"{field_name}.{key}")
        return
    if type(expected) is list:
        expected_array = expected
        actual_array = _require_array(value, field_name)
        if len(actual_array) != len(expected_array):
            raise ValueError(f"{field_name} has invalid cardinality")
        for ordinal, (actual_item, expected_item) in enumerate(zip(actual_array, expected_array)):
            _validate_closed_projection(actual_item, expected_item, f"{field_name}[{ordinal}]")
        return
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{field_name} does not match the closed V1 projection")


def _validate_domain_values(values: object, *, allow_empty: bool) -> None:
    domains = _require_array(values, "domains")
    if not allow_empty and not domains:
        raise ValueError("domains have an invalid shape")
    for value in domains:
        if type(value) is not str:
            raise TypeError("domains must contain exact text values")
        try:
            ToolDomain(value)
        except ValueError as exc:
            raise ValueError("domains contain unknown values") from exc
    if _has_exact_duplicates(domains):
        raise ValueError("domains contain unknown or duplicate values")
    if tuple(domains) != tuple(sorted(domains)):
        raise ValueError("domains are not in canonical order")


def _validate_binding(value: object) -> None:
    binding = _require_keys(value, ("contract", "resolver_descriptors"), "binding")
    raw_contract = _require_keys(binding["contract"], ("kind", "entity_kind"), "binding contract")
    contract = BindingContract(
        kind=cast(Any, raw_contract["kind"]),
        entity_kind=cast(Any, raw_contract["entity_kind"]),
    )
    raw_descriptors = _require_array(binding["resolver_descriptors"], "resolver descriptors")
    descriptors: list[BindingResolverDescriptorV1] = []
    for raw_value in raw_descriptors:
        raw = _require_keys(
            raw_value,
            ("resolver_id", "entity_kind", "arg_path", "presence", "identity_type"),
            "resolver descriptor",
        )
        descriptor = BindingResolverDescriptorV1(
            resolver_id=cast(Any, raw["resolver_id"]),
            entity_kind=cast(Any, raw["entity_kind"]),
            arg_path=cast(Any, raw["arg_path"]),
            presence=cast(Any, raw["presence"]),
            identity_type=cast(Any, raw["identity_type"]),
        )
        descriptors.append(descriptor)

    ToolBindingMetadataV1(
        contract=contract,
        resolver_descriptors=tuple(descriptors),
    )


def _validate_editable_fields(value: object) -> None:
    raw_fields = _require_array(value, "editable fields")
    fields: list[str] = []
    for raw_value in raw_fields:
        raw = _require_keys(
            raw_value,
            ("field", "value_type", "options", "clearable", "clear_value"),
            "editable field",
        )
        raw_options = raw["options"]
        options = (
            None
            if raw_options is None
            else tuple(_require_array(raw_options, "editable enum options"))
        )
        editable = EditableFieldMetadataV1(
            field=cast(Any, raw["field"]),
            value_type=cast(Any, raw["value_type"]),
            options=cast(Any, options),
            clearable=cast(Any, raw["clearable"]),
            clear_value=cast(Any, raw["clear_value"]),
        )
        fields.append(editable.field)
    if len(fields) != len(set(fields)):
        raise ValueError("editable field names must be unique")


def _validate_operation(
    value: object,
    confirmation_policy: object,
    provider_name: str,
    ordinal: int,
    compensation_bindings: list[tuple[str | None, int, str]],
) -> None:
    if type(confirmation_policy) is not str or confirmation_policy not in {
        "none",
        "required",
    }:
        raise ValueError("confirmation policy is invalid")
    if type(value) is not dict:
        raise TypeError("operation must be an object")
    operation_value = cast(dict[str, Any], value)
    operation_kind = operation_value.get("kind")
    if operation_kind == OperationKind.READ.value:
        _require_keys(operation_value, ("kind",), "read operation")
        if confirmation_policy != "none":
            raise ValueError("read operation cannot require confirmation")
        return
    if operation_kind != OperationKind.TRANSACTIONAL_WRITE.value:
        raise ValueError("operation discriminator is invalid")

    operation = _require_keys(
        operation_value,
        (
            "kind",
            "adapter_kind",
            "result_contract",
            "result_bytes",
            "visible_bytes",
            "transport_bytes",
            "undo_bytes",
            "undo_policy",
            "undo_payload_kind",
            "compensation_kind",
            "undo_contract_version",
            "undo_builder_id",
            "undo_seed_phase",
        ),
        "write operation",
    )
    if confirmation_policy != "required":
        raise ValueError("write operation must require confirmation")
    if type(operation["adapter_kind"]) is not str or operation["adapter_kind"] != "typed":
        raise ValueError("write operation adapter kind is invalid")
    if (
        type(operation["result_contract"]) is not str
        or operation["result_contract"] != "typed_json_v1"
    ):
        raise ValueError("write operation result contract is invalid")
    for field_name, expected in _WRITE_BUDGETS.items():
        if type(operation[field_name]) is not int or operation[field_name] != expected:
            raise ValueError(f"write operation {field_name} is not the exact V1 budget")

    undo_policy = operation["undo_policy"]
    if type(undo_policy) is not str:
        raise TypeError("Undo policy must be exact text")
    try:
        policy = UndoPolicy(undo_policy)
    except ValueError as exc:
        raise ValueError("Undo policy is invalid") from exc
    undo_fields = (
        "undo_payload_kind",
        "compensation_kind",
        "undo_contract_version",
        "undo_builder_id",
        "undo_seed_phase",
    )
    if policy is UndoPolicy.NONE:
        if any(operation[field] is not None for field in undo_fields):
            raise ValueError("Undo nullable shape is invalid")
        return

    payload_kind = operation["undo_payload_kind"]
    if type(payload_kind) is not str:
        raise TypeError("required Undo payload kind must be exact text")
    try:
        typed_payload_kind = UndoPayloadKind(payload_kind)
    except ValueError as exc:
        raise ValueError("required Undo payload kind is invalid") from exc
    compensation_kind = operation["compensation_kind"]
    if type(compensation_kind) is not str:
        raise TypeError("required Undo compensation kind must be exact text")
    try:
        typed_compensation_kind = CompensationKind(compensation_kind)
    except ValueError as exc:
        raise ValueError("required Undo compensation kind is invalid") from exc
    if typed_compensation_kind.value != f"undo:{provider_name}":
        raise ValueError("required Undo compensation does not match its Provider tool")
    undo_contract_version = operation["undo_contract_version"]
    undo_builder_id = operation["undo_builder_id"]
    undo_seed_phase = operation["undo_seed_phase"]
    if any(
        type(item) is not str for item in (undo_contract_version, undo_builder_id, undo_seed_phase)
    ):
        raise TypeError("required Undo projection members must be exact text")
    WriteOperationMetadataV1(
        result_bytes=cast(int, operation["result_bytes"]),
        visible_bytes=cast(int, operation["visible_bytes"]),
        transport_bytes=cast(int, operation["transport_bytes"]),
        undo_bytes=cast(int, operation["undo_bytes"]),
        undo_policy=policy,
        undo_payload_kind=typed_payload_kind,
        compensation_kind=typed_compensation_kind,
        undo_contract_version=cast(str, undo_contract_version),
        undo_builder_id=cast(str, undo_builder_id),
        undo_seed_phase=cast(Any, undo_seed_phase),
    )
    if undo_contract_version != "write-undo-payload-v1":
        raise ValueError("required Undo contract version is invalid")
    builder_suffix = (
        "restore" if typed_payload_kind is UndoPayloadKind.UPDATE_APPLICATION_STATUS else "delete"
    )
    if undo_builder_id != f"{provider_name}_{builder_suffix}_v1":
        raise ValueError("required Undo builder does not match its Provider tool")
    compensation_bindings.append(
        (
            cast(str, undo_seed_phase),
            ordinal,
            typed_compensation_kind.value,
        )
    )


def validate_tool_metadata_manifest(value: object) -> None:
    if isinstance(value, ToolMetadataManifestV1):
        value = value.to_dict()
    manifest = _require_keys(
        value,
        (
            "schema_version",
            "metadata_version",
            "catalog_profile",
            "typed_tools",
            "discovery_policy",
            "legacy_boundary",
            "compensation_operation_order",
        ),
        "metadata manifest",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("metadata manifest schema version mismatch")
    if (
        type(manifest["metadata_version"]) is not str
        or manifest["metadata_version"] != "tool-surface-metadata-v1"
    ):
        raise ValueError("metadata manifest version mismatch")
    if (
        type(manifest["catalog_profile"]) is not str
        or manifest["catalog_profile"] != "agent_typed_v1"
    ):
        raise ValueError("metadata manifest catalog profile mismatch")

    typed_tools = _require_array(manifest["typed_tools"], "Typed tools")
    if len(typed_tools) != 25:
        raise ValueError("metadata manifest must contain exactly 25 Typed tools")
    typed_keys = (
        "ordinal",
        "provider_name",
        "provider_contract_fingerprint",
        "domains",
        "dependencies",
        "provider_visibility",
        "required_capabilities",
        "binding",
        "confirmation_policy",
        "editable_fields",
        "operation",
    )
    names: list[str] = []
    dependency_graph: dict[str, tuple[str, ...]] = {}
    compensation_bindings: list[tuple[str | None, int, str]] = []
    for expected_ordinal, raw_tool in enumerate(typed_tools, start=1):
        tool = _require_keys(raw_tool, typed_keys, "Typed tool")
        if type(tool["ordinal"]) is not int or tool["ordinal"] != expected_ordinal:
            raise ValueError("Typed tool ordinals must be contiguous")
        name = _require_text(tool["provider_name"], "Typed Provider name")
        names.append(name)
        fingerprint = tool["provider_contract_fingerprint"]
        if type(fingerprint) is not str or _SHA256_PATTERN.fullmatch(fingerprint) is None:
            raise ValueError("Provider contract fingerprint is invalid")
        _validate_domain_values(tool["domains"], allow_empty=False)
        dependencies = _require_array(tool["dependencies"], "tool dependencies")
        for dependency in dependencies:
            _require_text(dependency, "tool dependency")
        if _has_exact_duplicates(dependencies):
            raise ValueError("tool dependencies must be a unique array")
        if tuple(dependencies) != tuple(sorted(dependencies)):
            raise ValueError("tool dependencies are not in canonical order")
        dependency_graph[name] = tuple(cast(list[str], dependencies))
        if (
            type(tool["provider_visibility"]) is not str
            or tool["provider_visibility"] != ProviderVisibility.MODEL_ELIGIBLE.value
        ):
            raise ValueError("Typed Provider visibility is invalid")
        capabilities = _require_array(tool["required_capabilities"], "required capabilities")
        if len(capabilities) != 1:
            raise ValueError("required capabilities have an invalid shape")
        capability = capabilities[0]
        if type(capability) is not str:
            raise ValueError("required capabilities have an invalid shape")
        try:
            ToolCapability(capability)
        except ValueError as exc:
            raise ValueError("required capabilities have an invalid shape") from exc

        _validate_binding(tool["binding"])
        _validate_editable_fields(tool["editable_fields"])
        _validate_operation(
            tool["operation"],
            tool["confirmation_policy"],
            name,
            expected_ordinal,
            compensation_bindings,
        )

    if len(names) != len(set(names)):
        raise ValueError("Typed Provider names must be unique")
    known = set(names)
    for name, declared_dependencies in dependency_graph.items():
        for dependency in declared_dependencies:
            if dependency == name:
                raise ValueError("Typed dependency cannot refer to self")
            if dependency not in known:
                raise ValueError("Typed dependency is unknown")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("Typed dependency graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependency_graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)

    _validate_closed_projection(
        manifest["discovery_policy"],
        materialize_json(_DISCOVERY_POLICY),
        "discovery policy",
    )
    _validate_closed_projection(
        manifest["legacy_boundary"],
        _legacy_manifest_boundary(),
        "Legacy boundary",
    )
    compensation = _require_array(
        manifest["compensation_operation_order"], "compensation operation order"
    )
    expected_compensation = list(_ordered_compensations(compensation_bindings))
    if any(type(item) is not str for item in compensation) or compensation != expected_compensation:
        raise ValueError("compensation operation order is invalid")


__all__ = [
    "ToolCatalog",
    "ToolMetadataManifestV1",
    "authority_manifest_for_specs",
    "build_tool_spec",
    "compile_tool_metadata_manifest",
    "validate_tool_metadata_manifest",
]
