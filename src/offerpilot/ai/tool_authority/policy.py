"""Closed Capability Profile and Binding policy for Typed Tools.

The policy module is deliberately pure.  It does not import repositories,
ToolSpec implementations, the Provider, or the Pilot Runtime.  The authority
manifest is supplied by the composition/catalog boundary and is validated
before it can be used to build a Provider surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, TypeAlias, cast


PROFILE_ID: Final = "agent_typed_v1"
CAPABILITY_POLICY_VERSION: Final = "capability-policy-v1"
BINDING_POLICY_VERSION: Final = "binding-policy-v1"
BINDING_AGGREGATION_VERSION: Final = "binding-aggregation-v1"
APPLICATION_COLLECTION_SCOPE_VERSION: Final = "application-collection-scope-v1"
SCOPE_DENIAL_VERSION: Final = "scope-denial-v1"
DEPENDENCY_POLICY_VERSION: Final = "dependency-policy-v1"

EXPECTED_CAPABILITIES: Final[tuple[str, ...]] = (
    "applications.read",
    "applications.write",
    "application_events.read",
    "application_events.write",
    "notes.read",
    "notes.write",
    "offers.read",
    "offers.write",
    "resumes.read",
    "resumes.write",
    "jd_analyses.read",
)

EXPECTED_CAPABILITY_SET: Final[frozenset[str]] = frozenset(EXPECTED_CAPABILITIES)

CAPABILITY_PROFILE_FINGERPRINT: Final = (
    "sha256:be49ef8b3335740931fc3dee690948d87fed499921cd9c6f4f8586c786ec7487"
)
BINDING_POLICY_FINGERPRINT: Final = (
    "sha256:0cb3e67abade3b5e986414267facd41d49f866050f90a97d50aec32d54320ca7"
)

# Names used by the characterization tests and by downstream composition code.
REVIEWED_CAPABILITY_FINGERPRINT: Final = CAPABILITY_PROFILE_FINGERPRINT
REVIEWED_BINDING_FINGERPRINT: Final = BINDING_POLICY_FINGERPRINT

BindingContractKind: TypeAlias = Literal[
    "none",
    "enforce_if_bound",
    "scoped_collection",
    "optional_target",
    "non_application_only",
]
BindingState: TypeAlias = Literal["resolved", "omitted", "detached", "unavailable"]
BindingStatus: TypeAlias = Literal["matched", "mismatched", "unbound", "unavailable"]
BindingReason: TypeAlias = Literal[
    "binding_mismatched",
    "binding_detached",
    "binding_unavailable",
    "scope_forbidden",
]

_BINDING_KINDS: Final[frozenset[str]] = frozenset(
    {"none", "enforce_if_bound", "scoped_collection", "optional_target", "non_application_only"}
)
_ENTITY_KINDS: Final[frozenset[str | None]] = frozenset({None, "application", "resume"})
_RESOLVER_PRESENCE: Final[frozenset[str]] = frozenset({"required", "optional"})
_TOOL_KINDS: Final[frozenset[str]] = frozenset({"read", "write"})
_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"ordinal", "name", "kind", "confirmation_policy", "required_capabilities", "binding", "resolvers"}
)
_BINDING_KEYS: Final[frozenset[str]] = frozenset({"kind", "entity_kind"})
_RESOLVER_KEYS: Final[frozenset[str]] = frozenset(
    {"resolver_id", "entity_kind", "arg_path", "presence", "identity_type"}
)


class AuthorityPolicyError(ValueError):
    """Raised when a closed authority policy cannot be trusted."""


PolicyValidationError = AuthorityPolicyError


def _canonical_json(value: object) -> str:
    """Serialize a JSON-compatible value using the public canonical contract."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_nonempty_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise AuthorityPolicyError(f"{field_name} must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AuthorityPolicyError(f"{field_name} contains a control character")
    return value


def _normalize_capability(value: object) -> str:
    if type(value) is str:
        return value
    if isinstance(value, Enum) and type(value.value) is str:
        return value.value
    raise AuthorityPolicyError("capability is not a closed V1 capability")


def _normalize_capabilities(values: Iterable[object]) -> tuple[str, ...]:
    try:
        normalized = tuple(_normalize_capability(value) for value in values)
    except TypeError as exc:
        raise AuthorityPolicyError("capabilities must be iterable") from exc
    if normalized != EXPECTED_CAPABILITIES:
        raise AuthorityPolicyError("capability profile is not the reviewed closed V1 profile")
    return normalized


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """Explicit capability set; enum additions never enter it implicitly."""

    profile_id: str
    capabilities: tuple[str, ...]
    capability_policy_version: str = CAPABILITY_POLICY_VERSION

    @classmethod
    def from_capabilities(
        cls,
        capabilities: Iterable[object],
        *,
        profile_id: str = PROFILE_ID,
        capability_policy_version: str = CAPABILITY_POLICY_VERSION,
    ) -> "CapabilityProfile":
        normalized = _normalize_capabilities(capabilities)
        return cls(
            profile_id=profile_id,
            capabilities=normalized,
            capability_policy_version=capability_policy_version,
        )

    @property
    def fingerprint(self) -> str:
        return capability_profile_fingerprint(self)


AGENT_TYPED_V1_PROFILE: Final[CapabilityProfile] = CapabilityProfile(
    profile_id=PROFILE_ID,
    capabilities=EXPECTED_CAPABILITIES,
    capability_policy_version=CAPABILITY_POLICY_VERSION,
)
capability_profile = AGENT_TYPED_V1_PROFILE


def capability_profile_input(profile: CapabilityProfile = AGENT_TYPED_V1_PROFILE) -> dict[str, object]:
    """Return the exact public canonical input for a capability profile."""

    return {
        "schema": "capability-profile-v1",
        "profile_id": profile.profile_id,
        "capabilities": list(profile.capabilities),
    }


def capability_profile_fingerprint(
    profile: CapabilityProfile = AGENT_TYPED_V1_PROFILE,
) -> str:
    """Compute the public SHA-256 digest for a profile without mutating it."""

    return _sha256_fingerprint(capability_profile_input(profile))


def validate_capability_profile(
    profile: CapabilityProfile = AGENT_TYPED_V1_PROFILE,
    expected_fingerprint: str | None = None,
) -> CapabilityProfile:
    """Validate profile identity, closed capability membership, version and digest."""

    if type(profile) is not CapabilityProfile:
        raise AuthorityPolicyError("capability profile has an invalid type")
    if profile.profile_id != PROFILE_ID:
        raise AuthorityPolicyError("unknown capability profile")
    if profile.capability_policy_version != CAPABILITY_POLICY_VERSION:
        raise AuthorityPolicyError("unknown capability policy version")
    if type(profile.capabilities) is not tuple:
        raise AuthorityPolicyError("capabilities must be an immutable tuple")
    if any(type(capability) is not str for capability in profile.capabilities):
        raise AuthorityPolicyError("capabilities must be canonical strings")
    if profile.capabilities != EXPECTED_CAPABILITIES:
        raise AuthorityPolicyError("capability profile contents are not reviewed")
    digest = capability_profile_fingerprint(profile)
    expected = CAPABILITY_PROFILE_FINGERPRINT if expected_fingerprint is None else expected_fingerprint
    if digest != expected:
        raise AuthorityPolicyError("capability profile fingerprint drift")
    return profile


@dataclass(frozen=True, slots=True)
class BindingResolution:
    """Pure-policy representation of one resolver result and its presence."""

    entity_kind: Literal["application", "resume"]
    state: BindingState
    identity: int | None = None
    presence: Literal["required", "optional"] = "required"


@dataclass(frozen=True, slots=True)
class BindingDecision:
    status: BindingStatus
    allowed: bool
    reason: BindingReason | None


def _positive_identity(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        raise AuthorityPolicyError(f"{field_name} must be a positive signed int64")
    return value


def _resolution_parts(value: object) -> tuple[str, BindingState, int | None, str]:
    if isinstance(value, BindingResolution):
        entity_kind: object = value.entity_kind
        state: object = value.state
        identity: object = value.identity
        presence: object = value.presence
    elif isinstance(value, Mapping):
        entity_kind = value.get("entity_kind")
        state = value.get("state")
        identity = value.get("identity")
        presence = value.get("presence", "required")
    else:
        entity_kind = getattr(value, "entity_kind", None)
        state = getattr(value, "state", None)
        identity = getattr(value, "identity", None)
        presence = getattr(value, "presence", "required")
    if entity_kind not in {"application", "resume"}:
        raise AuthorityPolicyError("binding resolution has an unknown entity kind")
    if state not in {"resolved", "omitted", "detached", "unavailable"}:
        raise AuthorityPolicyError("binding resolution has an unknown state")
    if presence not in {"required", "optional"}:
        raise AuthorityPolicyError("binding resolution has an unknown presence")
    if state == "resolved":
        identity = _positive_identity(identity, "binding identity")
    elif identity is not None:
        raise AuthorityPolicyError("only resolved bindings may carry an identity")
    return entity_kind, cast(BindingState, state), identity, presence


def decide_binding(
    contract_kind: str,
    *,
    scope_bound: bool,
    bound_identities: Iterable[int],
    resolutions: Sequence[object],
) -> BindingDecision:
    """Apply the V1 Binding truth table before resolver/repository execution."""

    if contract_kind not in _BINDING_KINDS:
        raise AuthorityPolicyError("unknown binding contract kind")
    if type(scope_bound) is not bool:
        raise AuthorityPolicyError("scope_bound must be boolean")
    identities = frozenset(_positive_identity(value, "bound identity") for value in bound_identities)
    parts = tuple(_resolution_parts(value) for value in resolutions)
    entity_kinds = {part[0] for part in parts}
    if len(entity_kinds) > 1:
        raise AuthorityPolicyError("mixed binding entity kinds are not supported in V1")

    if contract_kind in {"none", "non_application_only"}:
        if parts:
            raise AuthorityPolicyError(f"{contract_kind} cannot declare resolvers")
        if contract_kind == "non_application_only" and scope_bound:
            return BindingDecision("mismatched", False, "scope_forbidden")
        return BindingDecision("unbound", True, None)

    # An unbound workspace/global/mode scope keeps the existing repository
    # semantics.  It must not turn resolver failures into a scope denial.
    if not scope_bound:
        return BindingDecision("unbound", True, None)

    for _entity_kind, state, identity, _presence in parts:
        if state == "resolved" and identity not in identities:
            return BindingDecision("mismatched", False, "binding_mismatched")

    for _entity_kind, state, _identity, presence in parts:
        if state == "omitted" and presence == "optional":
            continue
        if state != "resolved":
            reason: BindingReason = (
                "binding_detached" if state == "detached" else "binding_unavailable"
            )
            return BindingDecision("unavailable", False, reason)

    return BindingDecision("matched", True, None)


binding_decision = decide_binding
evaluate_binding = decide_binding


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AuthorityPolicyError(f"{field_name} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], field_name: str) -> None:
    if frozenset(value.keys()) != expected or not all(type(key) is str for key in value):
        raise AuthorityPolicyError(f"{field_name} has an invalid shape")


def _validate_manifest(manifest: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    _exact_keys(manifest, frozenset({"schema_version", "tools"}), "manifest")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise AuthorityPolicyError("unknown authority manifest schema version")
    tools_value = manifest.get("tools")
    if type(tools_value) is not list or len(tools_value) != 25:
        raise AuthorityPolicyError("authority manifest must contain exactly 25 tools")
    tools: list[Mapping[str, object]] = []
    names: set[str] = set()
    binding_kinds: set[str] = set()
    entity_kinds: set[str | None] = set()
    for index, raw_tool in enumerate(cast(list[object], tools_value), start=1):
        tool = _mapping(raw_tool, f"tools[{index - 1}]")
        _exact_keys(tool, _MANIFEST_KEYS, f"tools[{index - 1}]")
        if type(tool.get("ordinal")) is not int or tool.get("ordinal") != index:
            raise AuthorityPolicyError("authority manifest ordinals are not contiguous")
        name = _require_nonempty_text(tool.get("name"), "tool name")
        if name in names:
            raise AuthorityPolicyError("authority manifest has duplicate tool names")
        names.add(name)
        kind = tool.get("kind")
        if kind not in _TOOL_KINDS:
            raise AuthorityPolicyError("authority manifest has an unknown tool kind")
        if tool.get("confirmation_policy") != ("required" if kind == "write" else "none"):
            raise AuthorityPolicyError("tool confirmation policy does not match tool kind")
        required = tool.get("required_capabilities")
        if type(required) is not list or len(required) != 1:
            raise AuthorityPolicyError("each Typed Tool must declare one capability")
        capability = required[0]
        if type(capability) is not str or capability not in EXPECTED_CAPABILITY_SET:
            raise AuthorityPolicyError("tool requires an unknown capability")
        binding = _mapping(tool.get("binding"), f"tools[{index - 1}].binding")
        _exact_keys(binding, _BINDING_KEYS, f"tools[{index - 1}].binding")
        binding_kind = binding.get("kind")
        entity_kind = binding.get("entity_kind")
        if binding_kind not in _BINDING_KINDS or entity_kind not in _ENTITY_KINDS:
            raise AuthorityPolicyError("tool binding contract is not closed")
        binding_kinds.add(binding_kind)
        entity_kinds.add(entity_kind)
        resolvers_value = tool.get("resolvers")
        if type(resolvers_value) is not list:
            raise AuthorityPolicyError("tool resolvers must be an array")
        resolvers = cast(list[object], resolvers_value)
        if binding_kind in {"none", "non_application_only"} and resolvers:
            raise AuthorityPolicyError("unbound binding contracts cannot declare resolvers")
        if binding_kind not in {"none", "non_application_only"} and entity_kind is None:
            raise AuthorityPolicyError("bound binding contracts need an entity kind")
        resolver_entity_kinds: set[str] = set()
        for raw_resolver in resolvers:
            resolver = _mapping(raw_resolver, "binding resolver")
            _exact_keys(resolver, _RESOLVER_KEYS, "binding resolver")
            resolver_id = _require_nonempty_text(resolver.get("resolver_id"), "resolver_id")
            arg_path = _require_nonempty_text(resolver.get("arg_path"), "arg_path")
            resolver_entity = resolver.get("entity_kind")
            if resolver_entity not in {"application", "resume"}:
                raise AuthorityPolicyError("resolver entity kind is not closed")
            if resolver_entity != entity_kind:
                raise AuthorityPolicyError("mixed binding entity kinds are not supported")
            if resolver.get("presence") not in _RESOLVER_PRESENCE:
                raise AuthorityPolicyError("resolver presence is not closed")
            if resolver.get("identity_type") != "positive_int64":
                raise AuthorityPolicyError("resolver identity type is not closed")
            resolver_entity_kinds.add(resolver_entity)
            del resolver_id, arg_path
        if len(resolver_entity_kinds) > 1:
            raise AuthorityPolicyError("mixed binding resolver entity kinds are not supported")
        tools.append(tool)
    if binding_kinds != set(_BINDING_KINDS) or entity_kinds != {None, "application", "resume"}:
        raise AuthorityPolicyError("authority manifest does not cover the reviewed V1 matrix")
    return tools


def binding_policy_input(
    manifest: Mapping[str, object],
    *,
    aggregation_rule_version: str = BINDING_AGGREGATION_VERSION,
    collection_scope_version: str = APPLICATION_COLLECTION_SCOPE_VERSION,
    public_denial_version: str = SCOPE_DENIAL_VERSION,
) -> dict[str, object]:
    """Build the exact semantic binding-policy fingerprint input."""

    tools = _validate_manifest(manifest)
    projection: list[dict[str, object]] = []
    for tool in tools:
        binding = cast(Mapping[str, object], tool["binding"])
        projection.append(
            {
                "name": tool["name"],
                "tool_kind": tool["kind"],
                "confirmation_policy": tool["confirmation_policy"],
                "required_capabilities": list(cast(list[object], tool["required_capabilities"])),
                "contract_kind": binding["kind"],
                "entity_kind_or_null": binding["entity_kind"],
                "resolvers": [
                    dict(cast(Mapping[str, object], resolver))
                    for resolver in cast(list[object], tool["resolvers"])
                ],
            }
        )
    return {
        "schema": BINDING_POLICY_VERSION,
        "aggregation_rule_version": aggregation_rule_version,
        "collection_scope_rule_version": collection_scope_version,
        "public_denial_rule_version": public_denial_version,
        "tools": projection,
    }


def binding_policy_fingerprint(
    manifest: Mapping[str, object],
    *,
    aggregation_rule_version: str = BINDING_AGGREGATION_VERSION,
    collection_scope_version: str = APPLICATION_COLLECTION_SCOPE_VERSION,
    public_denial_version: str = SCOPE_DENIAL_VERSION,
) -> str:
    return _sha256_fingerprint(
        binding_policy_input(
            manifest,
            aggregation_rule_version=aggregation_rule_version,
            collection_scope_version=collection_scope_version,
            public_denial_version=public_denial_version,
        )
    )


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    capability_profile: CapabilityProfile
    capability_profile_fingerprint: str
    binding_policy_fingerprint: str
    capability_policy_version: str
    binding_policy_version: str
    binding_aggregation_version: str
    collection_scope_version: str
    public_denial_version: str
    dependency_policy_version: str


def _reviewed_policy_values(
    expected_policy: Mapping[str, object] | None,
) -> tuple[str, str]:
    """Validate the read-only golden shape and return its two pinned digests."""

    if expected_policy is None:
        return CAPABILITY_PROFILE_FINGERPRINT, BINDING_POLICY_FINGERPRINT
    _exact_keys(
        expected_policy,
        frozenset({"schema_version", "capability_profile", "binding_policy"}),
        "expected_policy",
    )
    if type(expected_policy.get("schema_version")) is not int or expected_policy.get("schema_version") != 1:
        raise AuthorityPolicyError("unknown policy golden schema version")
    capability = _mapping(expected_policy.get("capability_profile"), "expected_policy.capability_profile")
    _exact_keys(
        capability,
        frozenset({"capabilities", "fingerprint", "profile_id", "schema"}),
        "expected_policy.capability_profile",
    )
    if (
        capability.get("profile_id") != PROFILE_ID
        or capability.get("schema") != "capability-profile-v1"
        or capability.get("capabilities") != list(EXPECTED_CAPABILITIES)
        or type(capability.get("fingerprint")) is not str
        or capability.get("fingerprint") != CAPABILITY_PROFILE_FINGERPRINT
    ):
        raise AuthorityPolicyError("capability profile golden drift")
    binding = _mapping(expected_policy.get("binding_policy"), "expected_policy.binding_policy")
    _exact_keys(
        binding,
        frozenset(
            {
                "aggregation_rule_version",
                "collection_scope_rule_version",
                "fingerprint",
                "public_denial_rule_version",
                "schema",
            }
        ),
        "expected_policy.binding_policy",
    )
    if (
        binding.get("schema") != BINDING_POLICY_VERSION
        or binding.get("aggregation_rule_version") != BINDING_AGGREGATION_VERSION
        or binding.get("collection_scope_rule_version") != APPLICATION_COLLECTION_SCOPE_VERSION
        or binding.get("public_denial_rule_version") != SCOPE_DENIAL_VERSION
        or type(binding.get("fingerprint")) is not str
        or binding.get("fingerprint") != BINDING_POLICY_FINGERPRINT
    ):
        raise AuthorityPolicyError("binding policy golden drift")
    return CAPABILITY_PROFILE_FINGERPRINT, BINDING_POLICY_FINGERPRINT


def validate_startup_policy(
    manifest: Mapping[str, object],
    *,
    expected_policy: Mapping[str, object] | None = None,
    profile: CapabilityProfile = AGENT_TYPED_V1_PROFILE,
    binding_aggregation_version: str = BINDING_AGGREGATION_VERSION,
    collection_scope_version: str = APPLICATION_COLLECTION_SCOPE_VERSION,
    public_denial_version: str = SCOPE_DENIAL_VERSION,
    dependency_policy_version: str = DEPENDENCY_POLICY_VERSION,
) -> PolicySnapshot:
    """Validate all immutable policy identities before Provider visibility."""

    expected_capability, expected_binding = _reviewed_policy_values(expected_policy)
    if (
        binding_aggregation_version != BINDING_AGGREGATION_VERSION
        or collection_scope_version != APPLICATION_COLLECTION_SCOPE_VERSION
        or public_denial_version != SCOPE_DENIAL_VERSION
        or dependency_policy_version != DEPENDENCY_POLICY_VERSION
    ):
        raise AuthorityPolicyError("binding policy rule version drift")
    validate_capability_profile(profile, expected_capability)
    actual_binding = binding_policy_fingerprint(
        manifest,
        aggregation_rule_version=binding_aggregation_version,
        collection_scope_version=collection_scope_version,
        public_denial_version=public_denial_version,
    )
    if actual_binding != expected_binding:
        raise AuthorityPolicyError("binding policy fingerprint drift")
    return PolicySnapshot(
        capability_profile=profile,
        capability_profile_fingerprint=expected_capability,
        binding_policy_fingerprint=actual_binding,
        capability_policy_version=CAPABILITY_POLICY_VERSION,
        binding_policy_version=BINDING_POLICY_VERSION,
        binding_aggregation_version=binding_aggregation_version,
        collection_scope_version=collection_scope_version,
        public_denial_version=public_denial_version,
        dependency_policy_version=dependency_policy_version,
    )


validate_policy = validate_startup_policy


__all__ = [
    "AGENT_TYPED_V1_PROFILE",
    "APPLICATION_COLLECTION_SCOPE_VERSION",
    "BINDING_AGGREGATION_VERSION",
    "BINDING_POLICY_FINGERPRINT",
    "BINDING_POLICY_VERSION",
    "BindingContractKind",
    "BindingDecision",
    "BindingReason",
    "BindingResolution",
    "BindingState",
    "BindingStatus",
    "CAPABILITY_POLICY_VERSION",
    "CAPABILITY_PROFILE_FINGERPRINT",
    "CapabilityProfile",
    "DEPENDENCY_POLICY_VERSION",
    "EXPECTED_CAPABILITIES",
    "EXPECTED_CAPABILITY_SET",
    "PROFILE_ID",
    "PolicySnapshot",
    "PolicyValidationError",
    "REVIEWED_BINDING_FINGERPRINT",
    "REVIEWED_CAPABILITY_FINGERPRINT",
    "SCOPE_DENIAL_VERSION",
    "AuthorityPolicyError",
    "binding_decision",
    "binding_policy_fingerprint",
    "binding_policy_input",
    "capability_profile",
    "capability_profile_fingerprint",
    "capability_profile_input",
    "decide_binding",
    "evaluate_binding",
    "validate_capability_profile",
    "validate_policy",
    "validate_startup_policy",
]
