from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
from dataclasses import replace
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from offerpilot.ai.tool_runtime.contracts import (
    BindingContract,
    BindingResolverSpec,
    FailureCategory,
    ProviderToolContract,
    REQUIRED_UNDO_TOOL_NAMES,
    TRANSACTIONAL_TYPED_WRITE_NAMES,
    ToolSpec,
    UndoPolicy,
    WriteContract,
)
from offerpilot.ai.tool_runtime.validation import compile_tool_schema
from offerpilot.ai.tool_authority.policy import EXPECTED_CAPABILITY_SET, validate_startup_policy


_FAILURE_CATEGORIES: frozenset[str] = frozenset(
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


def authority_manifest_for_specs(
    specs: Sequence[ToolSpec[Any, Any]],
    *,
    strict: bool = True,
) -> dict[str, object]:
    """Project ToolSpec authority metadata into the canonical manifest shape.

    This is a structural projection, not a second matrix: the runtime specs
    remain the source of values and the policy module supplies the pinned
    fingerprint.  In strict mode a bare legacy resolver is rejected instead
    of being inferred from a function name or signature.
    """

    tools: list[dict[str, object]] = []
    for ordinal, spec in enumerate(specs, start=1):
        if not isinstance(spec.binding_contract, BindingContract):
            raise ValueError("tool binding contract metadata is required")
        resolvers: list[dict[str, object]] = []
        for resolver in spec.binding_resolvers:
            if not isinstance(resolver, BindingResolverSpec):
                if strict:
                    raise ValueError("binding resolver metadata is required")
                continue
            resolvers.append(
                {
                    "resolver_id": resolver.resolver_id,
                    "entity_kind": resolver.entity_kind,
                    "arg_path": resolver.arg_path,
                    "presence": resolver.presence,
                    "identity_type": resolver.identity_type,
                }
            )
        tools.append(
            {
                "ordinal": ordinal,
                "name": spec.name,
                "kind": spec.kind,
                "confirmation_policy": spec.confirmation_policy,
                "required_capabilities": [str(capability) for capability in spec.required_capabilities],
                "binding": {
                    "kind": spec.binding_contract.kind,
                    "entity_kind": spec.binding_contract.entity_kind,
                },
                "resolvers": resolvers,
            }
        )
    return {"schema_version": 1, "tools": tools}


def _resolver_integrity_snapshot(resolver: object) -> tuple[object, ...]:
    if isinstance(resolver, BindingResolverSpec):
        return (
            "descriptor",
            resolver,
            resolver.resolve,
            resolver.resolver_id,
            resolver.entity_kind,
            resolver.arg_path,
            resolver.presence,
            resolver.identity_type,
        )
    return ("callable", resolver)


def _spec_integrity_snapshot(spec: ToolSpec[Any, Any]) -> tuple[object, ...]:
    if not isinstance(spec.binding_contract, BindingContract):
        raise ValueError("tool binding contract metadata is required")
    return (
        spec.contract,
        copy.deepcopy(spec.contract.payload),
        spec.contract.name,
        spec.contract.description,
        copy.deepcopy(spec.contract.parameters),
        spec.kind,
        spec.confirmation_policy,
        spec.required_capabilities,
        spec.binding_contract,
        (spec.binding_contract.kind, spec.binding_contract.entity_kind),
        tuple(_resolver_integrity_snapshot(resolver) for resolver in spec.binding_resolvers),
    )


def _resolver_integrity_matches(
    current: tuple[tuple[object, ...], ...],
    expected: tuple[tuple[object, ...], ...],
) -> bool:
    if len(current) != len(expected):
        return False
    for current_item, expected_item in zip(current, expected):
        if current_item[0] != expected_item[0] or current_item[1] is not expected_item[1]:
            return False
        if current_item[0] == "descriptor":
            if current_item[2] is not expected_item[2] or current_item[3:] != expected_item[3:]:
                return False
    return True


class ToolCatalog:
    def __init__(
        self,
        specs: Sequence[ToolSpec[Any, Any]],
        *,
        expected_names: Sequence[str],
        authority_manifest: Mapping[str, object] | None = None,
    ) -> None:
        ordered = tuple(self._with_write_contract(spec) for spec in specs)
        expected = tuple(expected_names)
        names = tuple(spec.name for spec in ordered)
        if names != expected or len(set(names)) != len(names):
            raise ValueError("tool catalog names/order mismatch")
        strict_authority = authority_manifest is not None or len(expected) == 25
        for spec in ordered:
            self._validate_spec(spec, strict_authority=strict_authority)
        projected_manifest: dict[str, object] | None = None
        if strict_authority:
            projected_manifest = authority_manifest_for_specs(ordered, strict=True)
            if authority_manifest is not None and projected_manifest != dict(authority_manifest):
                raise ValueError("authority manifest drift")
            try:
                validate_startup_policy(projected_manifest)
            except ValueError as exc:
                raise ValueError("authority policy drift") from exc
        self._ordered = ordered
        self._specs = {spec.name: spec for spec in ordered}
        self._integrity_snapshots = {
            id(spec): _spec_integrity_snapshot(spec) for spec in ordered
        }
        self._validator_schemas = {
            spec.name: copy.deepcopy(spec.contract.parameters) for spec in ordered
        }
        for schema in self._validator_schemas.values():
            compile_tool_schema(copy.deepcopy(schema))
        self._authority_manifest = projected_manifest

    def _ensure_integrity(self) -> None:
        for spec in self._ordered:
            expected = self._integrity_snapshots.get(id(spec))
            if expected is None:
                raise ValueError("tool catalog integrity drift")
            current = _spec_integrity_snapshot(spec)
            if current[0] is not expected[0] or current[1:8] != expected[1:8]:
                raise ValueError("tool catalog integrity drift")
            if current[8] is not expected[8] or current[9] != expected[9]:
                raise ValueError("tool catalog integrity drift")
            if not _resolver_integrity_matches(
                cast(tuple[tuple[object, ...], ...], current[10]),
                cast(tuple[tuple[object, ...], ...], expected[10]),
            ):
                raise ValueError("tool catalog integrity drift")

    @staticmethod
    def _with_write_contract(spec: ToolSpec[Any, Any]) -> ToolSpec[Any, Any]:
        if spec.kind == "write" and spec.write_contract is None and spec.name in TRANSACTIONAL_TYPED_WRITE_NAMES:
            return replace(
                spec,
                write_contract=WriteContract(
                    undo_policy=UndoPolicy.REQUIRED if spec.name in REQUIRED_UNDO_TOOL_NAMES else UndoPolicy.NONE
                ),
            )
        return spec

    @staticmethod
    def _validate_spec(spec: ToolSpec[Any, Any], *, strict_authority: bool = False) -> None:
        if spec.kind == "read" and spec.confirmation_policy != "none":
            raise ValueError("read tool cannot require confirmation")
        if spec.kind == "read" and spec.write_contract is not None:
            raise ValueError("read tool cannot declare a write contract")
        if spec.kind == "write" and spec.confirmation_policy != "required":
            raise ValueError("write tool must require confirmation")
        if spec.kind == "write" and spec.write_contract is None:
            raise ValueError("write tool must declare a write contract")
        if not set(spec.declared_failure_categories).issubset(_FAILURE_CATEGORIES):
            raise ValueError("tool declares unsupported failure category")
        for mapping in spec.exception_map:
            category: FailureCategory = mapping.category
            if category not in spec.declared_failure_categories:
                raise ValueError("exception mapping category is not declared")
        if not isinstance(spec.binding_contract, BindingContract):
            raise ValueError("tool binding contract metadata is required")
        if not strict_authority:
            return
        capabilities = {str(capability) for capability in spec.required_capabilities}
        if not capabilities or not capabilities.issubset(EXPECTED_CAPABILITY_SET):
            raise ValueError("unknown capability")
        resolvers = spec.binding_resolvers
        if any(not isinstance(resolver, BindingResolverSpec) for resolver in resolvers):
            raise ValueError("binding resolver metadata is required")
        resolver_specs = tuple(
            resolver for resolver in resolvers if isinstance(resolver, BindingResolverSpec)
        )
        contract = spec.binding_contract
        if contract.kind in {"none", "non_application_only"}:
            if contract.entity_kind is not None or resolver_specs:
                raise ValueError("unbound binding contract cannot declare resolvers")
        else:
            if contract.entity_kind is None:
                raise ValueError("bound binding contract requires an entity kind")
            if contract.kind == "scoped_collection" and len(resolver_specs) > 1:
                raise ValueError("scoped collection accepts at most one resolver")
            if contract.kind == "optional_target" and len(resolver_specs) > 1:
                raise ValueError("optional target accepts at most one resolver")
            for resolver in resolver_specs:
                if resolver.entity_kind != contract.entity_kind:
                    raise ValueError("mixed binding resolver entity kinds")
            if contract.kind == "scoped_collection" and resolver_specs:
                if resolver_specs[0].presence != "optional":
                    raise ValueError("scoped collection resolver must be optional")
        for resolver in resolver_specs:
            if resolver.resolver_id not in {
                "application_identity_arg",
                "application_event_parent",
                "note_application_parent",
                "offer_application_parent",
                "resume_identity_arg",
                "jd_analysis_application_parent",
            }:
                raise ValueError("unknown binding resolver id")
            if resolver.identity_type != "positive_int64":
                raise ValueError("unknown binding resolver identity type")

    def resolve(self, name: str) -> ToolSpec[Any, Any] | None:
        self._ensure_integrity()
        return self._specs.get(name)

    def validator_for(self, name: str) -> Draft202012Validator:
        self._ensure_integrity()
        return compile_tool_schema(copy.deepcopy(self._validator_schemas[name]))

    def provider_contracts(self) -> tuple[ProviderToolContract, ...]:
        self._ensure_integrity()
        return tuple(copy.deepcopy(spec.contract) for spec in self._ordered)

    def write_names(self) -> frozenset[str]:
        self._ensure_integrity()
        return frozenset(spec.name for spec in self._ordered if spec.kind == "write")

    @property
    def specs(self) -> tuple[ToolSpec[Any, Any], ...]:
        self._ensure_integrity()
        return self._ordered

    @property
    def authority_manifest(self) -> dict[str, object]:
        self._ensure_integrity()
        if self._authority_manifest is None:
            return authority_manifest_for_specs(self._ordered, strict=False)
        return copy.deepcopy(self._authority_manifest)
