from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, cast

from offerpilot.ai.tool_runtime.contracts import (
    ProviderToolContract,
    materialize_provider_payloads,
)
from offerpilot.ai.tool_runtime.metadata import (
    BundleInstanceToken,
    ToolAuthorityEntryV1,
    ToolAuthorityMetadataView,
    ToolDiscoveryEntryV1,
    ToolDiscoveryMetadataView,
)
from offerpilot.ai.tool_runtime.policy_types import ToolDomain
from offerpilot.context_projector.contracts import ProjectionError, canonical_json, sha256_hex

SELECTOR_VERSION = "tool-surface-selector-v1"
_DISCOVERY_POLICY_VERSION = "tool-discovery-policy-v1"
_TOOL_SELECTION_RESULT_CONSTRUCTION_SEAL = object()


class ToolSelectionFallbackReason(str, Enum):
    NO_TRUSTED_SIGNAL = "no_trusted_signal"
    DECLARED_AMBIGUOUS_INPUT = "declared_ambiguous_input"


class ToolSelectionDiagnosticKind(str, Enum):
    PAGE_DOMAIN_COUNT = "page_domain_count"
    ATTACHMENT_DOMAIN_COUNT = "attachment_domain_count"
    LEXICAL_DOMAIN_COUNT = "lexical_domain_count"
    TRUSTED_DOMAIN_COUNT = "trusted_domain_count"


@dataclass(frozen=True, slots=True)
class ToolSelectionDiagnostic:
    kind: ToolSelectionDiagnosticKind
    count: int

    def __post_init__(self) -> None:
        if type(self.kind) is not ToolSelectionDiagnosticKind:
            raise TypeError("selection diagnostics require a closed kind")
        if type(self.count) is not int or self.count < 0:
            raise ValueError("selection diagnostic count must be non-negative")


@dataclass(frozen=True, slots=True)
class ToolSelectionSignals:
    page_kind: str = "workspace"
    attachment_kinds: tuple[str, ...] = ()
    current_request: str = ""
    trusted_domains: tuple[str, ...] = ()
    version: str = SELECTOR_VERSION
    declared_ambiguous_input: bool = False


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ToolSelectionResult:
    provider_contracts: tuple[ProviderToolContract, ...]
    provider_envelope_fingerprint: str
    selected_names: tuple[str, ...]
    selected_domains: tuple[ToolDomain, ...]
    dependency_closure: tuple[str, ...]
    full_catalog_fallback: bool
    fallback_reason: ToolSelectionFallbackReason | None
    diagnostics: tuple[ToolSelectionDiagnostic, ...]
    _bundle_instance_token: BundleInstanceToken = field(repr=False, compare=False)
    _integrity_seal: tuple[object, ...] = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        provider_contracts: tuple[ProviderToolContract, ...],
        provider_envelope_fingerprint: str,
        selected_names: tuple[str, ...],
        selected_domains: tuple[ToolDomain, ...],
        dependency_closure: tuple[str, ...],
        full_catalog_fallback: bool,
        fallback_reason: ToolSelectionFallbackReason | None,
        diagnostics: tuple[ToolSelectionDiagnostic, ...],
        _bundle_instance_token: BundleInstanceToken,
        _seal: object,
    ) -> None:
        if _seal is not _TOOL_SELECTION_RESULT_CONSTRUCTION_SEAL:
            raise TypeError("ToolSelectionResult is Selector-issued")
        object.__setattr__(self, "provider_contracts", provider_contracts)
        object.__setattr__(
            self,
            "provider_envelope_fingerprint",
            provider_envelope_fingerprint,
        )
        object.__setattr__(self, "selected_names", selected_names)
        object.__setattr__(self, "selected_domains", selected_domains)
        object.__setattr__(self, "dependency_closure", dependency_closure)
        object.__setattr__(self, "full_catalog_fallback", full_catalog_fallback)
        object.__setattr__(self, "fallback_reason", fallback_reason)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "_bundle_instance_token", _bundle_instance_token)
        self._validate_and_seal()

    def _validate_and_seal(self) -> None:
        if type(self.provider_contracts) is not tuple or not self.provider_contracts:
            raise TypeError("selection Provider contracts must be a non-empty exact tuple")
        for contract in self.provider_contracts:
            if type(contract) is not ProviderToolContract:
                raise TypeError("selection requires exact Provider contracts")
            contract._ensure_provider_integrity()
        if type(self.selected_names) is not tuple or not self.selected_names:
            raise TypeError("selection names must be a non-empty exact tuple")
        if tuple(contract.name for contract in self.provider_contracts) != self.selected_names:
            raise ValueError("selection Provider contracts do not match selected names")
        if type(self.selected_domains) is not tuple or any(
            type(domain) is not ToolDomain for domain in self.selected_domains
        ):
            raise TypeError("selection domains require exact ToolDomain values")
        if type(self.dependency_closure) is not tuple:
            raise TypeError("selection dependency closure must be an exact tuple")
        if self.dependency_closure != self.selected_names:
            raise ValueError("selection names must equal the ordered dependency closure")
        if type(self.full_catalog_fallback) is not bool:
            raise TypeError("selection fallback flag must be exact bool")
        if (
            self.fallback_reason is not None
            and type(self.fallback_reason) is not ToolSelectionFallbackReason
        ):
            raise TypeError("selection fallback reason must be closed")
        if self.full_catalog_fallback != (self.fallback_reason is not None):
            raise ValueError("selection fallback reason does not match fallback state")
        if type(self.diagnostics) is not tuple or any(
            type(value) is not ToolSelectionDiagnostic for value in self.diagnostics
        ):
            raise TypeError("selection diagnostics must be an exact tuple")
        for diagnostic in self.diagnostics:
            if type(diagnostic.kind) is not ToolSelectionDiagnosticKind:
                raise TypeError("selection diagnostics require a closed kind")
            if type(diagnostic.count) is not int or diagnostic.count < 0:
                raise ValueError("selection diagnostic count must be non-negative")
        if type(self._bundle_instance_token) is not BundleInstanceToken:
            raise TypeError("selection requires exact Bundle provenance")
        if (
            type(self.provider_envelope_fingerprint) is not str
            or len(self.provider_envelope_fingerprint) != 64
        ):
            raise ValueError("selection Provider fingerprint is invalid")
        object.__setattr__(self, "_integrity_seal", self._integrity_snapshot())

    def _integrity_snapshot(self) -> tuple[object, ...]:
        return (
            id(self.provider_contracts),
            tuple(id(contract) for contract in self.provider_contracts),
            self.provider_envelope_fingerprint,
            self.selected_names,
            self.selected_domains,
            self.dependency_closure,
            self.full_catalog_fallback,
            self.fallback_reason,
            id(self.diagnostics),
            tuple(
                (id(diagnostic), diagnostic.kind, diagnostic.count)
                for diagnostic in self.diagnostics
            ),
            id(self._bundle_instance_token),
        )

    def _ensure_integrity(self) -> None:
        try:
            self._bundle_instance_token._ensure_integrity()
            for contract in self.provider_contracts:
                contract._ensure_provider_integrity()
            if self._integrity_snapshot() != self._integrity_seal:
                raise ValueError("selection result integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProjectionError("tool_selection_integrity_drift") from exc

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._ensure_integrity()
        return self._bundle_instance_token

    # AuthorityFactory still seals these immutable facts through Task 9.  The
    # dataclass contract remains the new result shape; Task 10 removes these
    # read-only seal aliases when Authority consumes the result directly.
    @property
    def tools(self) -> tuple[ProviderToolContract, ...]:
        self._ensure_integrity()
        return self.provider_contracts

    @property
    def names(self) -> tuple[str, ...]:
        self._ensure_integrity()
        return self.selected_names

    @property
    def domains(self) -> tuple[str, ...]:
        self._ensure_integrity()
        return tuple(domain.value for domain in self.selected_domains)

    @property
    def envelope_fingerprint(self) -> str:
        self._ensure_integrity()
        return self.provider_envelope_fingerprint

    @property
    def fallback_all(self) -> bool:
        self._ensure_integrity()
        return self.full_catalog_fallback


def _policy_rows(
    policy: Mapping[str, object],
    field_name: str,
    key_name: str,
) -> dict[str, tuple[ToolDomain, ...]]:
    raw_rows = policy.get(field_name)
    if type(raw_rows) is not tuple:
        raise ProjectionError("invalid_discovery_policy")
    result: dict[str, tuple[ToolDomain, ...]] = {}
    for raw_row in raw_rows:
        if type(raw_row) is not MappingProxyType:
            raise ProjectionError("invalid_discovery_policy")
        row = cast(Mapping[str, object], raw_row)
        key = row.get(key_name)
        raw_domains = row.get("domains")
        if type(key) is not str or type(raw_domains) is not tuple or key in result:
            raise ProjectionError("invalid_discovery_policy")
        try:
            domains = tuple(ToolDomain(value) for value in raw_domains)
        except (TypeError, ValueError) as exc:
            raise ProjectionError("invalid_discovery_policy") from exc
        if len(set(domains)) != len(domains):
            raise ProjectionError("invalid_discovery_policy")
        result[key] = domains
    return result


def _lexical_policy(
    policy: Mapping[str, object],
) -> tuple[tuple[ToolDomain, tuple[str, ...]], ...]:
    raw_rules = policy.get("lexical_rules")
    if type(raw_rules) is not tuple:
        raise ProjectionError("invalid_discovery_policy")
    rules: list[tuple[ToolDomain, tuple[str, ...]]] = []
    seen: set[ToolDomain] = set()
    for raw_rule in raw_rules:
        if type(raw_rule) is not MappingProxyType:
            raise ProjectionError("invalid_discovery_policy")
        rule = cast(Mapping[str, object], raw_rule)
        raw_domain = rule.get("domain")
        raw_terms = rule.get("terms")
        if (
            type(raw_domain) is not str
            or type(raw_terms) is not tuple
            or any(type(term) is not str or not term for term in raw_terms)
        ):
            raise ProjectionError("invalid_discovery_policy")
        try:
            domain = ToolDomain(raw_domain)
        except ValueError as exc:
            raise ProjectionError("invalid_discovery_policy") from exc
        if domain in seen:
            raise ProjectionError("invalid_discovery_policy")
        seen.add(domain)
        rules.append((domain, cast(tuple[str, ...], raw_terms)))
    return tuple(rules)


def _validate_signals(signals: ToolSelectionSignals) -> None:
    if type(signals) is not ToolSelectionSignals:
        raise ProjectionError("trusted_tool_selection_signals_required")
    if signals.version != SELECTOR_VERSION:
        raise ProjectionError("unsupported_selector_version")
    if type(signals.page_kind) is not str:
        raise ProjectionError("unknown_page_kind")
    if type(signals.attachment_kinds) is not tuple or any(
        type(value) is not str for value in signals.attachment_kinds
    ):
        raise ProjectionError("unknown_attachment_kind")
    if type(signals.current_request) is not str:
        raise ProjectionError("invalid_current_request")
    if type(signals.trusted_domains) is not tuple or any(
        type(value) is not str for value in signals.trusted_domains
    ):
        raise ProjectionError("unknown_trusted_domain")
    if type(signals.declared_ambiguous_input) is not bool:
        raise ProjectionError("invalid_ambiguity_signal")


def _ordered_closure(
    initially_selected: set[str],
    entries_by_name: Mapping[str, object],
    catalog_names: tuple[str, ...],
) -> tuple[str, ...]:
    selected = set(initially_selected)
    pending = list(initially_selected)
    while pending:
        name = pending.pop()
        entry = entries_by_name.get(name)
        if entry is None:
            raise ProjectionError("unknown_selected_tool")
        dependencies = getattr(entry, "dependencies", None)
        if type(dependencies) is not tuple:
            raise ProjectionError("invalid_discovery_dependency")
        for dependency in dependencies:
            if type(dependency) is not str or dependency not in entries_by_name:
                raise ProjectionError("tool_dependency_missing")
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return tuple(name for name in catalog_names if name in selected)


def _require_same_bundle_views(
    discovery_view: ToolDiscoveryMetadataView,
    authority_view: ToolAuthorityMetadataView,
) -> tuple[
    BundleInstanceToken,
    tuple[ToolDiscoveryEntryV1, ...],
    Mapping[str, ToolAuthorityEntryV1],
]:
    try:
        if (
            type(discovery_view) is not ToolDiscoveryMetadataView
            or type(authority_view) is not ToolAuthorityMetadataView
        ):
            raise ProjectionError("exact_metadata_views_required")
        discovery_token = discovery_view.bundle_instance_token
        authority_token = authority_view.bundle_instance_token
        if discovery_token is not authority_token:
            raise ProjectionError("cross_Bundle_metadata_views")
        discovery_token._ensure_integrity()
        entries = discovery_view.ordered_entries
        authority_entries = authority_view.entries
    except ProjectionError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProjectionError("metadata_Bundle_integrity_failure") from exc

    catalog_names = tuple(entry.provider_name for entry in entries)
    if not catalog_names or len(set(catalog_names)) != len(catalog_names):
        raise ProjectionError("provider_catalog_mismatch")
    if tuple(authority_entries) != catalog_names or any(
        authority_entries[name].ordinal != ordinal
        for ordinal, name in enumerate(catalog_names, start=1)
    ):
        raise ProjectionError("authority_discovery_view_mismatch")
    return discovery_token, entries, authority_entries


def _issue_tool_selection_result(
    discovery_view: ToolDiscoveryMetadataView,
    authority_view: ToolAuthorityMetadataView,
    *,
    provider_contracts: tuple[ProviderToolContract, ...],
    selected_names: tuple[str, ...],
    selected_domains: tuple[ToolDomain, ...],
    dependency_closure: tuple[str, ...],
    full_catalog_fallback: bool,
    fallback_reason: ToolSelectionFallbackReason | None,
    diagnostics: tuple[ToolSelectionDiagnostic, ...],
) -> ToolSelectionResult:
    """Issue a selection only from contracts proven to belong to one Bundle."""

    bundle_token, raw_entries, _ = _require_same_bundle_views(discovery_view, authority_view)
    if (
        type(provider_contracts) is not tuple
        or type(selected_names) is not tuple
        or not provider_contracts
        or len(provider_contracts) != len(selected_names)
        or len(set(selected_names)) != len(selected_names)
    ):
        raise ProjectionError("invalid_tool_surface")

    selected_index = 0
    for entry in raw_entries:
        entry_name = entry.provider_name
        if selected_index >= len(selected_names) or entry_name != selected_names[selected_index]:
            continue
        if entry.provider_contract is not provider_contracts[selected_index]:
            raise ProjectionError("selection_Bundle_provenance_mismatch")
        selected_index += 1
    if selected_index != len(selected_names):
        raise ProjectionError("selection_Bundle_provenance_mismatch")

    catalog_names = tuple(entry.provider_name for entry in raw_entries)
    entries_by_name = {entry.provider_name: entry for entry in raw_entries}
    expected_closure = _ordered_closure(
        set(selected_names),
        entries_by_name,
        catalog_names,
    )
    if dependency_closure != selected_names or dependency_closure != expected_closure:
        raise ProjectionError("invalid_tool_dependency_closure")

    envelopes = materialize_provider_payloads(provider_contracts)
    provider_envelope_fingerprint = sha256_hex(canonical_json(envelopes))
    return ToolSelectionResult(
        provider_contracts=provider_contracts,
        provider_envelope_fingerprint=provider_envelope_fingerprint,
        selected_names=selected_names,
        selected_domains=selected_domains,
        dependency_closure=dependency_closure,
        full_catalog_fallback=full_catalog_fallback,
        fallback_reason=fallback_reason,
        diagnostics=diagnostics,
        _bundle_instance_token=bundle_token,
        _seal=_TOOL_SELECTION_RESULT_CONSTRUCTION_SEAL,
    )


def select_tools(
    discovery_view: ToolDiscoveryMetadataView,
    authority_view: ToolAuthorityMetadataView,
    trusted_signals: ToolSelectionSignals,
) -> ToolSelectionResult:
    _validate_signals(trusted_signals)
    _, entries, authority_entries = _require_same_bundle_views(
        discovery_view,
        authority_view,
    )
    policy = discovery_view.policy.projection

    catalog_names = tuple(entry.provider_name for entry in entries)
    if not catalog_names or len(set(catalog_names)) != len(catalog_names):
        raise ProjectionError("provider_catalog_mismatch")
    entries_by_name = {entry.provider_name: entry for entry in entries}
    if len(entries_by_name) != len(entries):
        raise ProjectionError("provider_catalog_mismatch")

    if policy.get("selector_version") != SELECTOR_VERSION:
        raise ProjectionError("unsupported_selector_version")
    if policy.get("discovery_policy_version") != _DISCOVERY_POLICY_VERSION:
        raise ProjectionError("unsupported_discovery_policy_version")
    if (
        policy.get("no_signal_behavior") != "full_typed_catalog"
        or policy.get("invalid_input_behavior") != "fail_closed"
    ):
        raise ProjectionError("unsupported_discovery_policy")
    page_domains = _policy_rows(policy, "page_domains", "page_kind")
    attachment_domains = _policy_rows(
        policy,
        "attachment_domains",
        "attachment_kind",
    )
    lexical_rules = _lexical_policy(policy)
    if trusted_signals.page_kind not in page_domains:
        raise ProjectionError("unknown_page_kind")
    if any(kind not in attachment_domains for kind in trusted_signals.attachment_kinds):
        raise ProjectionError("unknown_attachment_kind")
    try:
        trusted_domains = tuple(ToolDomain(value) for value in trusted_signals.trusted_domains)
    except ValueError as exc:
        raise ProjectionError("unknown_trusted_domain") from exc

    page_selected = page_domains[trusted_signals.page_kind]
    attachment_selected = tuple(
        domain for kind in trusted_signals.attachment_kinds for domain in attachment_domains[kind]
    )
    normalized_request = trusted_signals.current_request.casefold()[:16_384]
    lexical_selected = tuple(
        domain
        for domain, terms in lexical_rules
        if any(term in normalized_request for term in terms)
    )
    selected_domain_set = {
        *trusted_domains,
        *page_selected,
        *attachment_selected,
        *lexical_selected,
    }
    selected_domains = tuple(sorted(selected_domain_set, key=lambda value: value.value))
    diagnostics = (
        ToolSelectionDiagnostic(
            ToolSelectionDiagnosticKind.PAGE_DOMAIN_COUNT,
            len(set(page_selected)),
        ),
        ToolSelectionDiagnostic(
            ToolSelectionDiagnosticKind.ATTACHMENT_DOMAIN_COUNT,
            len(set(attachment_selected)),
        ),
        ToolSelectionDiagnostic(
            ToolSelectionDiagnosticKind.LEXICAL_DOMAIN_COUNT,
            len(set(lexical_selected)),
        ),
        ToolSelectionDiagnostic(
            ToolSelectionDiagnosticKind.TRUSTED_DOMAIN_COUNT,
            len(set(trusted_domains)),
        ),
    )

    fallback_reason: ToolSelectionFallbackReason | None = None
    if trusted_signals.declared_ambiguous_input:
        fallback_reason = ToolSelectionFallbackReason.DECLARED_AMBIGUOUS_INPUT
    elif not selected_domains:
        fallback_reason = ToolSelectionFallbackReason.NO_TRUSTED_SIGNAL

    if fallback_reason is not None:
        selected_names = catalog_names
    else:
        initially_selected = {
            entry.provider_name
            for entry in entries
            if any(domain in selected_domain_set for domain in entry.domains)
        }
        selected_names = _ordered_closure(
            initially_selected,
            entries_by_name,
            catalog_names,
        )
    if not selected_names:
        raise ProjectionError("invalid_tool_surface")
    selected_name_set = set(selected_names)
    for name in selected_names:
        dependencies = entries_by_name[name].dependencies
        if any(dependency not in selected_name_set for dependency in dependencies):
            raise ProjectionError("tool_dependency_not_closed")

    provider_contracts = tuple(
        entry.provider_contract for entry in entries if entry.provider_name in selected_name_set
    )
    return _issue_tool_selection_result(
        discovery_view,
        authority_view,
        provider_contracts=provider_contracts,
        selected_names=selected_names,
        selected_domains=selected_domains,
        dependency_closure=selected_names,
        full_catalog_fallback=fallback_reason is not None,
        fallback_reason=fallback_reason,
        diagnostics=diagnostics,
    )


__all__ = [
    "SELECTOR_VERSION",
    "ToolSelectionDiagnostic",
    "ToolSelectionDiagnosticKind",
    "ToolSelectionFallbackReason",
    "ToolSelectionResult",
    "ToolSelectionSignals",
    "select_tools",
]
