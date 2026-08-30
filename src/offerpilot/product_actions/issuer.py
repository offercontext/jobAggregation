"""Deterministic, source-bound Product Action proposal identity issuer."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal, NoReturn, Protocol, SupportsIndex, cast
from uuid import UUID, uuid5

from offerpilot.ai.write_operations import LedgerKeyDomain, ledger_fingerprint
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    PRODUCT_ACTION_NAMES,
    DecodedProductActionRouteV1,
    HistoricalStoryRouteProof,
    ProductActionContractError,
    ProductActionIntegrityError,
    ProductActionName,
    ProductActionProofRegistryV1,
    ProductActionRequestOrigin,
    ProductActionRouteProof,
    canonical_product_action_json,
    decode_product_action_route_payload,
    materialize_frozen_json,
    require_product_action_uuid,
    tagged_optional,
)


PRODUCT_ACTION_OPERATION_NAMESPACE = UUID("2dba24b4-8d35-5c48-8c40-99b9fa5e84f2")
PRODUCT_ACTION_CALL_NAMESPACE = UUID("60f6c6ce-1d72-5e28-9f54-9d3a2b4d41e2")


class LedgerKeyProfileStoreV1:
    """Bounded key-profile lookup; active rotation never rewrites stored identities."""

    __slots__ = ("_profiles", "_active_key_id", "_lock", "_integrity_seal")
    _profiles: dict[str, LedgerKeyDomain]
    _active_key_id: str
    _lock: RLock
    _integrity_seal: tuple[dict[str, LedgerKeyDomain], RLock]

    def __init__(
        self,
        profiles: tuple[LedgerKeyDomain, ...],
        *,
        active_key_id: str,
    ) -> None:
        if type(profiles) is not tuple or not profiles:
            raise TypeError("Ledger key profiles must be a non-empty exact tuple")
        normalized: dict[str, LedgerKeyDomain] = {}
        for profile in profiles:
            if type(profile) is not LedgerKeyDomain:
                raise TypeError("Ledger key profile has the wrong type")
            key_id = require_product_action_uuid(profile.key_id, "fingerprint_key_id")
            if type(profile.secret) is not bytes or len(profile.secret) != 32:
                raise ProductActionContractError("ledger_key_secret_invalid")
            if key_id in normalized:
                raise ProductActionContractError("duplicate_ledger_key_profile")
            normalized[key_id] = profile
        active = require_product_action_uuid(active_key_id, "active_key_id")
        if active not in normalized:
            raise ProductActionContractError("missing_active_key_profile")
        lock = RLock()
        object.__setattr__(self, "_profiles", normalized)
        object.__setattr__(self, "_active_key_id", active)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_integrity_seal", (normalized, lock))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Ledger key profile store is sealed")

    def __repr__(self) -> str:
        return f"<LedgerKeyProfileStoreV1 profiles={len(self._profiles)}>"

    def _ensure_integrity(self) -> None:
        if self._integrity_seal != (self._profiles, self._lock):
            raise ProductActionIntegrityError("key_profile_store_integrity")
        if self._active_key_id not in self._profiles:
            raise ProductActionIntegrityError("missing_key_profile")

    def active(self) -> LedgerKeyDomain:
        self._ensure_integrity()
        with self._lock:
            return self._profiles[self._active_key_id]

    def resolve(self, key_id: str) -> LedgerKeyDomain:
        self._ensure_integrity()
        normalized = require_product_action_uuid(key_id, "fingerprint_key_id")
        try:
            return self._profiles[normalized]
        except KeyError as exc:
            raise ProductActionIntegrityError("missing_key_profile") from exc

    def activate(self, key_id: str) -> None:
        self._ensure_integrity()
        normalized = require_product_action_uuid(key_id, "fingerprint_key_id")
        with self._lock:
            if normalized not in self._profiles:
                raise ProductActionIntegrityError("missing_key_profile")
            object.__setattr__(self, "_active_key_id", normalized)


class PreparedProductActionProposalV1:
    """Pre-transaction identity bundle. Its raw token is deliberately repr-hidden."""

    __slots__ = (
        "schema_version",
        "action_name",
        "request_origin",
        "source_kind",
        "source_id",
        "source_revision",
        "route_payload_json",
        "route_payload_fingerprint",
        "semantic_claim_fingerprint",
        "authorization_scope_fingerprint",
        "route_binding_fingerprint",
        "request_idempotency_fingerprint",
        "proposal_fingerprint",
        "historical_request_token_fingerprint",
        "operation_id",
        "action_call_id",
        "fingerprint_key_id",
        "confirmation_token_fingerprint",
        "transition_id",
        "created_at",
        "route_proof",
        "proof_binding",
        "_confirmation_token",
        "_integrity_seal",
    )
    schema_version: int
    action_name: ProductActionName
    request_origin: ProductActionRequestOrigin
    source_kind: Literal["story_proposal", "review_focus"]
    source_id: int
    source_revision: int
    route_payload_json: str
    route_payload_fingerprint: str
    semantic_claim_fingerprint: str | None
    authorization_scope_fingerprint: str
    route_binding_fingerprint: str
    request_idempotency_fingerprint: str
    proposal_fingerprint: str
    historical_request_token_fingerprint: str | None
    operation_id: str
    action_call_id: str
    fingerprint_key_id: str
    confirmation_token_fingerprint: str
    transition_id: str
    created_at: datetime
    route_proof: ProductActionRouteProof | HistoricalStoryRouteProof
    proof_binding: tuple[object, ...]
    _confirmation_token: str
    _integrity_seal: tuple[object, ...]

    def __init__(self, **values: object) -> None:
        expected = set(self.__slots__) - {"_integrity_seal"}
        if set(values) != expected:
            raise TypeError("Prepared Product Action identity has an invalid exact shape")
        for name in expected:
            object.__setattr__(self, name, values[name])
        object.__setattr__(
            self,
            "_integrity_seal",
            tuple(getattr(self, name) for name in self.__slots__ if name != "_integrity_seal"),
        )

    def __repr__(self) -> str:
        return (
            "PreparedProductActionProposalV1("
            f"operation_id={self.operation_id!r}, action_name={self.action_name!r})"
        )

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("Prepared Product Action identity cannot be serialized or copied")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        self._serialization_error()

    def __getstate__(self) -> NoReturn:
        self._serialization_error()

    def __copy__(self) -> NoReturn:
        self._serialization_error()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        self._serialization_error()

    @property
    def confirmation_token(self) -> str:
        return self._confirmation_token

    def _ensure_integrity(self) -> None:
        if self._integrity_seal != tuple(
            getattr(self, name) for name in self.__slots__ if name != "_integrity_seal"
        ):
            raise ProductActionIntegrityError("prepared_identity_integrity")

    def identity_projection(self) -> dict[str, str | None]:
        return {
            "operation_id": self.operation_id,
            "action_call_id": self.action_call_id,
            "route_payload_fingerprint": self.route_payload_fingerprint,
            "semantic_claim_fingerprint": self.semantic_claim_fingerprint,
            "authorization_scope_fingerprint": self.authorization_scope_fingerprint,
            "route_binding_fingerprint": self.route_binding_fingerprint,
            "request_idempotency_fingerprint": self.request_idempotency_fingerprint,
            "proposal_fingerprint": self.proposal_fingerprint,
            "confirmation_token_fingerprint": self.confirmation_token_fingerprint,
            "historical_request_token_fingerprint": self.historical_request_token_fingerprint,
        }


def _route_payload(route: DecodedProductActionRouteV1) -> dict[str, Any]:
    return cast(dict[str, Any], materialize_frozen_json(route.payload))


def _hmac(key: LedgerKeyDomain, domain: str, value: dict[str, Any]) -> str:
    return ledger_fingerprint(key, domain, cast(Any, value))


def _operation_identity(
    action_name: ProductActionName,
    payload: dict[str, Any],
) -> str:
    if action_name == "save_review_readiness_signal":
        return cast(str, payload["domain_idempotency_key"])
    return canonical_product_action_json(
        {
            "attempt_id": payload["attempt_id"],
            "generation_revision": payload["generation_revision"],
            "product_action_generation": payload["product_action_generation"],
            "proposal_hash": payload["proposal_hash"],
        }
    )


def _canonical_request_identity(
    action_name: ProductActionName,
    request_origin: ProductActionRequestOrigin,
    payload: dict[str, Any],
    historical_request_token_fingerprint: str | None,
) -> dict[str, Any]:
    if action_name == "save_review_readiness_signal":
        return {
            "kind": "signal",
            "idempotency_key": payload["domain_idempotency_key"],
        }
    result = {
        "kind": "historical_story" if request_origin == "historical_story_bridge" else "story",
        "attempt_id": payload["attempt_id"],
        "generation_revision": payload["generation_revision"],
        "product_action_generation": payload["product_action_generation"],
        "proposal_hash": payload["proposal_hash"],
    }
    if request_origin == "historical_story_bridge":
        result["historical_request_token_fingerprint"] = historical_request_token_fingerprint
    return result


def _scope_envelope(
    catalog_fingerprint: str,
    action_name: ProductActionName,
    payload: dict[str, Any],
    capabilities: tuple[str, ...],
) -> dict[str, Any]:
    if action_name == "save_review_readiness_signal":
        return {
            "catalog_fingerprint": catalog_fingerprint,
            "action_name": action_name,
            "application_id": payload["application_id"],
            "event_id": payload["event_id"],
            "note_id": payload["note_id"],
            "proposal_id": payload["proposal_id"],
            "focus_id": payload["focus_id"],
            "expected_note_revision": payload["expected_note_revision"],
            "expected_source_fingerprint": payload["expected_source_fingerprint"],
            "expected_proposal_hash": payload["expected_proposal_hash"],
            "expected_candidate_fingerprint": payload["expected_candidate_fingerprint"],
            "capabilities": list(capabilities),
        }
    return {
        "catalog_fingerprint": catalog_fingerprint,
        "action_name": action_name,
        "attempt_id": payload["attempt_id"],
        "generation_revision": payload["generation_revision"],
        "product_action_generation": payload["product_action_generation"],
        "proposal_hash": payload["proposal_hash"],
        "source_fingerprint": payload["source_fingerprint"],
        "target_story_id": tagged_optional(payload["target_story_id"]),
        "expected_current_version_id": tagged_optional(payload["expected_current_version_id"]),
        "expected_story_revision": tagged_optional(payload["expected_story_revision"]),
        "capabilities": list(capabilities),
    }


def _owner_and_scope(
    action_name: ProductActionName,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if action_name == "save_review_readiness_signal":
        return (
            {
                "kind": "review_readiness_focus_owner",
                "application_id": payload["application_id"],
                "note_id": payload["note_id"],
            },
            {"kind": "application", "id": payload["application_id"]},
        )
    return (
        {"kind": "interview_story_owner", "attempt_id": payload["attempt_id"]},
        {"kind": "story", "id": tagged_optional(payload["target_story_id"])},
    )


def _confirmation_token(
    key: LedgerKeyDomain,
    *,
    catalog_fingerprint: str,
    action_name: ProductActionName,
    operation_id: str,
    action_call_id: str,
    proposal_fingerprint: str,
    route_binding_fingerprint: str,
    historical_request_token_fingerprint: str | None,
) -> str:
    envelope = {
        "catalog_fingerprint": catalog_fingerprint,
        "action_name": action_name,
        "operation_id": operation_id,
        "action_call_id": action_call_id,
        "proposal_fingerprint": proposal_fingerprint,
        "route_binding_fingerprint": route_binding_fingerprint,
        "historical_request_token_fingerprint": tagged_optional(
            historical_request_token_fingerprint
        ),
    }
    return hmac.new(
        key.secret,
        b"product-action-confirmation-token-v1\0"
        + canonical_product_action_json(cast(Any, envelope)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _derive_persisted_proposal_identity(
    key: LedgerKeyDomain,
    *,
    catalog_fingerprint: str,
    action_name: ProductActionName,
    request_origin: ProductActionRequestOrigin,
    operation_id: str,
    action_call_id: str,
    route_payload_fingerprint: str,
    route_binding_fingerprint: str,
    authorization_scope_fingerprint: str,
    semantic_claim_fingerprint: str | None,
    historical_request_token_fingerprint: str | None,
) -> tuple[str, str]:
    """Rebuild the terminal-safe keyed chain without cleared route plaintext."""

    proposal_fingerprint = _hmac(
        key,
        "product-action-proposal-v1",
        {
            "operation_id": operation_id,
            "action_call_id": action_call_id,
            "catalog_fingerprint": catalog_fingerprint,
            "action_name": action_name,
            "request_origin": request_origin,
            "route_payload_fingerprint": route_payload_fingerprint,
            "route_binding_fingerprint": route_binding_fingerprint,
            "authorization_scope_fingerprint": authorization_scope_fingerprint,
            "semantic_claim_fingerprint": tagged_optional(semantic_claim_fingerprint),
            "historical_request_token_fingerprint": tagged_optional(
                historical_request_token_fingerprint
            ),
        },
    )
    confirmation_token = _confirmation_token(
        key,
        catalog_fingerprint=catalog_fingerprint,
        action_name=action_name,
        operation_id=operation_id,
        action_call_id=action_call_id,
        proposal_fingerprint=proposal_fingerprint,
        route_binding_fingerprint=route_binding_fingerprint,
        historical_request_token_fingerprint=historical_request_token_fingerprint,
    )
    confirmation_token_fingerprint = ledger_fingerprint(
        key,
        "write-operation-confirmation-token-v1",
        confirmation_token.encode("ascii"),
    )
    return proposal_fingerprint, confirmation_token_fingerprint


def _derive_historical_request_token_fingerprint(
    key: LedgerKeyDomain,
    *,
    attempt_id: int,
    generation_revision: int,
    proposal_hash: str,
    legacy_confirmation_token: str,
) -> str:
    return _hmac(
        key,
        "historical-story-request-token-v1",
        {
            "attempt_id": attempt_id,
            "generation_revision": generation_revision,
            "proposal_hash": proposal_hash,
            "legacy_confirmation_token": legacy_confirmation_token,
        },
    )


def _derive(
    *,
    route: DecodedProductActionRouteV1,
    catalog: ProductActionCatalogV1,
    key: LedgerKeyDomain,
    historical_request_token_fingerprint: str | None,
) -> dict[str, Any]:
    payload = _route_payload(route)
    spec = next(
        spec for spec in catalog.ordered_specs if spec.action_name == route.action_name
    )
    catalog_fingerprint = catalog.fingerprint
    route_payload_fingerprint = _hmac(
        key,
        "product-action-route-payload-v1",
        {
            "schema_version": 1,
            "catalog_fingerprint": catalog_fingerprint,
            "action_name": route.action_name,
            "request_origin": route.request_origin,
            "source_kind": route.source_kind,
            "source_id": route.source_id,
            "source_revision": route.source_revision,
            "exact_route_payload": payload,
        },
    )
    semantic_claim_fingerprint = (
        _hmac(
            key,
            "product-action-semantic-claim-v1",
            {
                "action_name": "save_review_readiness_signal",
                "application_id": payload["application_id"],
                "proposal_id": payload["proposal_id"],
                "focus_id": payload["focus_id"],
            },
        )
        if route.action_name == "save_review_readiness_signal"
        else None
    )
    canonical_operation_identity = _operation_identity(route.action_name, payload)
    operation_id = str(
        uuid5(
            PRODUCT_ACTION_OPERATION_NAMESPACE,
            f"{route.action_name}:{route.request_origin}:{canonical_operation_identity}",
        )
    )
    action_call_id = str(uuid5(PRODUCT_ACTION_CALL_NAMESPACE, operation_id.lower()))
    authorization_scope_fingerprint = _hmac(
        key,
        "product-action-scope-v1",
        _scope_envelope(
            catalog_fingerprint,
            route.action_name,
            payload,
            spec.capabilities,
        ),
    )
    canonical_owner, application_scope = _owner_and_scope(route.action_name, payload)
    route_binding_fingerprint = _hmac(
        key,
        "product-action-route-binding-v1",
        {
            "catalog_fingerprint": catalog_fingerprint,
            "action_name": route.action_name,
            "request_origin": route.request_origin,
            "source_kind": route.source_kind,
            "source_id": route.source_id,
            "source_revision": route.source_revision,
            "canonical_owner": canonical_owner,
            "application_scope": application_scope,
            "route_payload_fingerprint": route_payload_fingerprint,
            "semantic_claim_fingerprint": tagged_optional(semantic_claim_fingerprint),
            "historical_request_token_fingerprint": tagged_optional(
                historical_request_token_fingerprint
            ),
        },
    )
    request_idempotency_fingerprint = _hmac(
        key,
        "product-action-proposal-request-v1",
        {
            "schema_version": 1,
            "action_name": route.action_name,
            "request_origin": route.request_origin,
            "canonical_request_identity": _canonical_request_identity(
                route.action_name,
                route.request_origin,
                payload,
                historical_request_token_fingerprint,
            ),
            "operation_id": operation_id,
            "action_call_id": action_call_id,
            "route_payload_fingerprint": route_payload_fingerprint,
            "route_binding_fingerprint": route_binding_fingerprint,
            "semantic_claim_fingerprint": tagged_optional(semantic_claim_fingerprint),
            "historical_request_token_fingerprint": tagged_optional(
                historical_request_token_fingerprint
            ),
        },
    )
    proposal_fingerprint = _hmac(
        key,
        "product-action-proposal-v1",
        {
            "operation_id": operation_id,
            "action_call_id": action_call_id,
            "catalog_fingerprint": catalog_fingerprint,
            "action_name": route.action_name,
            "request_origin": route.request_origin,
            "route_payload_fingerprint": route_payload_fingerprint,
            "route_binding_fingerprint": route_binding_fingerprint,
            "authorization_scope_fingerprint": authorization_scope_fingerprint,
            "semantic_claim_fingerprint": tagged_optional(semantic_claim_fingerprint),
            "historical_request_token_fingerprint": tagged_optional(
                historical_request_token_fingerprint
            ),
        },
    )
    confirmation_token = _confirmation_token(
        key,
        catalog_fingerprint=catalog_fingerprint,
        action_name=route.action_name,
        operation_id=operation_id,
        action_call_id=action_call_id,
        proposal_fingerprint=proposal_fingerprint,
        route_binding_fingerprint=route_binding_fingerprint,
        historical_request_token_fingerprint=historical_request_token_fingerprint,
    )
    return {
        "route_payload_fingerprint": route_payload_fingerprint,
        "semantic_claim_fingerprint": semantic_claim_fingerprint,
        "authorization_scope_fingerprint": authorization_scope_fingerprint,
        "route_binding_fingerprint": route_binding_fingerprint,
        "request_idempotency_fingerprint": request_idempotency_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
        "operation_id": operation_id,
        "action_call_id": action_call_id,
        "confirmation_token": confirmation_token,
        "confirmation_token_fingerprint": ledger_fingerprint(
            key,
            "write-operation-confirmation-token-v1",
            confirmation_token.encode("ascii"),
        ),
    }


def _proof_binding(values: dict[str, Any], route: DecodedProductActionRouteV1) -> tuple[object, ...]:
    return (
        route.action_name,
        route.request_origin,
        route.source_kind,
        route.source_id,
        route.source_revision,
        values["operation_id"],
        values["action_call_id"],
        values["route_payload_fingerprint"],
        values["route_binding_fingerprint"],
        values["request_idempotency_fingerprint"],
    )


def _prepared_from_derived_identity(
    *,
    route: DecodedProductActionRouteV1,
    key: LedgerKeyDomain,
    values: dict[str, Any],
    historical_request_token_fingerprint: str | None,
    proof: ProductActionRouteProof | HistoricalStoryRouteProof,
    binding: tuple[object, ...],
) -> PreparedProductActionProposalV1:
    return PreparedProductActionProposalV1(
        schema_version=1,
        action_name=route.action_name,
        request_origin=route.request_origin,
        source_kind=route.source_kind,
        source_id=route.source_id,
        source_revision=route.source_revision,
        route_payload_json=route.canonical_json,
        route_payload_fingerprint=values["route_payload_fingerprint"],
        semantic_claim_fingerprint=values["semantic_claim_fingerprint"],
        authorization_scope_fingerprint=values["authorization_scope_fingerprint"],
        route_binding_fingerprint=values["route_binding_fingerprint"],
        request_idempotency_fingerprint=values["request_idempotency_fingerprint"],
        proposal_fingerprint=values["proposal_fingerprint"],
        historical_request_token_fingerprint=historical_request_token_fingerprint,
        operation_id=values["operation_id"],
        action_call_id=values["action_call_id"],
        fingerprint_key_id=key.key_id,
        confirmation_token_fingerprint=values["confirmation_token_fingerprint"],
        transition_id=str(
            uuid5(PRODUCT_ACTION_CALL_NAMESPACE, values["operation_id"] + ":transition:1")
        ),
        created_at=datetime.now(timezone.utc),
        route_proof=proof,
        proof_binding=binding,
        _confirmation_token=values["confirmation_token"],
    )


class _ProductActionBundle(Protocol):
    operation: Any
    route: Any


class _BaseActionIssuer:
    __slots__ = ("_catalog", "_registry", "_key_profiles", "_action_name", "_integrity_seal")
    _catalog: ProductActionCatalogV1
    _registry: ProductActionProofRegistryV1
    _key_profiles: LedgerKeyProfileStoreV1
    _action_name: ProductActionName
    _integrity_seal: tuple[
        ProductActionCatalogV1,
        ProductActionProofRegistryV1,
        LedgerKeyProfileStoreV1,
        ProductActionName,
    ]

    def __init__(
        self,
        catalog: ProductActionCatalogV1,
        registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
        *,
        action_name: ProductActionName,
    ) -> None:
        if (
            type(catalog) is not ProductActionCatalogV1
            or type(registry) is not ProductActionProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or catalog._registry is not registry
            or action_name not in PRODUCT_ACTION_NAMES
        ):
            raise TypeError("Product Action issuer composition is invalid")
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_key_profiles", key_profiles)
        object.__setattr__(self, "_action_name", action_name)
        object.__setattr__(
            self,
            "_integrity_seal",
            (catalog, registry, key_profiles, action_name),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action issuer is sealed")

    def _ensure_integrity(self) -> None:
        if self._integrity_seal != (
            self._catalog,
            self._registry,
            self._key_profiles,
            self._action_name,
        ) or self._catalog._registry is not self._registry:
            raise ProductActionIntegrityError("issuer_integrity")

    def _prepare(
        self,
        *,
        route_payload_raw: bytes,
    ) -> PreparedProductActionProposalV1:
        self._ensure_integrity()
        route = decode_product_action_route_payload(
            route_payload_raw,
            action_name=self._action_name,
            request_origin="current",
        )
        selected_key = self._key_profiles.active()
        values = _derive(
            route=route,
            catalog=self._catalog,
            key=selected_key,
            historical_request_token_fingerprint=None,
        )
        binding = _proof_binding(values, route)
        proof = cast(
            ProductActionRouteProof,
            self._registry._issue(
                ProductActionRouteProof,
                action_name=route.action_name,
                binding=binding,
            ),
        )
        return _prepared_from_derived_identity(
            route=route,
            key=selected_key,
            values=values,
            historical_request_token_fingerprint=None,
            proof=proof,
            binding=binding,
        )

    def recover_confirmation_token(self, bundle: _ProductActionBundle) -> str:
        self._ensure_integrity()
        operation = bundle.operation
        route = bundle.route
        if (
            operation.tool_name != self._action_name
            or route.action_name != self._action_name
            or operation.status != "proposed"
            or route.route_payload_json is None
            or route.terminalized_at is not None
        ):
            raise ProductActionIntegrityError("action_identity_mismatch")
        key = self._key_profiles.resolve(operation.fingerprint_key_id)
        token = _confirmation_token(
            key,
            catalog_fingerprint=self._catalog.fingerprint,
            action_name=cast(ProductActionName, operation.tool_name),
            operation_id=operation.id,
            action_call_id=cast(str, operation.tool_call_id),
            proposal_fingerprint=cast(str, operation.proposal_fingerprint),
            route_binding_fingerprint=route.route_binding_fingerprint,
            historical_request_token_fingerprint=route.historical_request_token_fingerprint,
        )
        expected = ledger_fingerprint(
            key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        if not hmac.compare_digest(expected, operation.confirmation_token_fingerprint):
            raise ProductActionIntegrityError("confirmation_token_integrity")
        return token


class ReviewReadinessActionIssuer(_BaseActionIssuer):
    __slots__ = ()

    def __init__(
        self,
        catalog: ProductActionCatalogV1,
        registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
    ) -> None:
        super().__init__(
            catalog,
            registry,
            key_profiles,
            action_name="save_review_readiness_signal",
        )

    def prepare(self, *, route_payload_raw: bytes) -> PreparedProductActionProposalV1:
        return self._prepare(route_payload_raw=route_payload_raw)


class InterviewStoryActionIssuer(_BaseActionIssuer):
    __slots__ = ()

    def __init__(
        self,
        catalog: ProductActionCatalogV1,
        registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
    ) -> None:
        super().__init__(
            catalog,
            registry,
            key_profiles,
            action_name="confirm_interview_story",
        )

    def prepare(
        self,
        *,
        route_payload_raw: bytes,
        request_origin: Literal["current", "historical_story_bridge"] = "current",
        historical_confirmation_token: str | None = None,
    ) -> PreparedProductActionProposalV1:
        if request_origin != "current" or historical_confirmation_token is not None:
            raise ProductActionContractError("historical_story_bridge_repository_only")
        return self._prepare(route_payload_raw=route_payload_raw)

def validate_prepared_product_action(
    prepared: PreparedProductActionProposalV1,
    *,
    catalog: ProductActionCatalogV1,
    key_profiles: LedgerKeyProfileStoreV1,
    require_route_proof: bool = True,
) -> None:
    if type(prepared) is not PreparedProductActionProposalV1:
        raise TypeError("Product Action Repository requires an exact prepared identity")
    if type(prepared.schema_version) is not int:
        raise ProductActionContractError("schema_version_exact_integer")
    if prepared.schema_version != 1:
        raise ProductActionContractError("schema_version_invalid")
    for field in ("source_id", "source_revision"):
        value = getattr(prepared, field)
        if type(value) is not int:
            raise ProductActionContractError(f"{field}_exact_integer")
        if value < 1:
            raise ProductActionContractError(f"{field}_positive_integer")
    prepared._ensure_integrity()
    require_product_action_uuid(prepared.operation_id, "operation_id")
    require_product_action_uuid(prepared.action_call_id, "action_call_id")
    require_product_action_uuid(prepared.transition_id, "transition_id")
    key = key_profiles.resolve(prepared.fingerprint_key_id)
    route = decode_product_action_route_payload(
        prepared.route_payload_json.encode("utf-8"),
        action_name=prepared.action_name,
        request_origin=prepared.request_origin,
    )
    if route.source_identity != (
        prepared.source_kind,
        prepared.source_id,
        prepared.source_revision,
    ):
        raise ProductActionIntegrityError("prepared_source_identity")
    derived = _derive(
        route=route,
        catalog=catalog,
        key=key,
        historical_request_token_fingerprint=prepared.historical_request_token_fingerprint,
    )
    for field in (
        "route_payload_fingerprint",
        "semantic_claim_fingerprint",
        "authorization_scope_fingerprint",
        "route_binding_fingerprint",
        "request_idempotency_fingerprint",
        "proposal_fingerprint",
        "operation_id",
        "action_call_id",
        "confirmation_token_fingerprint",
    ):
        if derived[field] != getattr(prepared, field):
            raise ProductActionIntegrityError(f"prepared_{field}")
    if derived["confirmation_token"] != prepared.confirmation_token:
        raise ProductActionIntegrityError("prepared_confirmation_token")
    if require_route_proof:
        if prepared.request_origin == "historical_story_bridge":
            catalog.resolve_historical_story(
                cast(HistoricalStoryRouteProof, prepared.route_proof),
                expected_binding=prepared.proof_binding,
            )
        else:
            catalog.resolve(
                cast(ProductActionRouteProof, prepared.route_proof),
                expected_binding=prepared.proof_binding,
            )


__all__ = [
    "InterviewStoryActionIssuer",
    "LedgerKeyProfileStoreV1",
    "PRODUCT_ACTION_CALL_NAMESPACE",
    "PRODUCT_ACTION_OPERATION_NAMESPACE",
    "PreparedProductActionProposalV1",
    "ReviewReadinessActionIssuer",
    "validate_prepared_product_action",
]
