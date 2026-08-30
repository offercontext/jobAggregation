"""Closed metadata catalogs for the two Product Actions and two compensations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal, NoReturn, cast

from offerpilot.product_actions.contracts import (
    PRODUCT_ACTION_COMPENSATION_NAMES,
    PRODUCT_ACTION_NAMES,
    HistoricalStoryRouteProof,
    JSONValue,
    ProductActionProofRegistryV1,
    ProductActionRouteProof,
    canonical_product_action_json,
)


_SPEC_SEAL_SENTINEL = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ProductActionSpecV1:
    ordinal: int
    action_name: str
    route_source: str
    capabilities: tuple[str, ...]
    binding_policy: str
    route_payload_contract: str
    decision_payload_decoder: str
    safe_presentation: str
    result_codec: Literal["product_action_json_v1"]
    write_contract: str
    compensation_binding: str
    undo_policy: Literal["required"]
    _identity_seal: object = field(default=_SPEC_SEAL_SENTINEL, init=False, repr=False)

    def __post_init__(self) -> None:
        identity = self._identity()
        if self._identity_seal is _SPEC_SEAL_SENTINEL:
            object.__setattr__(self, "_identity_seal", identity)
        elif self._identity_seal != identity:
            raise ValueError("Product Action spec integrity drift")

    def _identity(self) -> tuple[object, ...]:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 2:
            raise ValueError("Product Action spec ordinal is invalid")
        if self.action_name not in PRODUCT_ACTION_NAMES:
            raise ValueError("Product Action spec name is invalid")
        if type(self.capabilities) is not tuple or len(self.capabilities) != 1:
            raise ValueError("Product Action capability declaration is invalid")
        if self.result_codec != "product_action_json_v1" or self.undo_policy != "required":
            raise ValueError("Product Action write contract is invalid")
        if self.compensation_binding not in PRODUCT_ACTION_COMPENSATION_NAMES:
            raise ValueError("Product Action compensation binding is invalid")
        values = (
            self.ordinal,
            self.action_name,
            self.route_source,
            self.capabilities,
            self.binding_policy,
            self.route_payload_contract,
            self.decision_payload_decoder,
            self.safe_presentation,
            self.result_codec,
            self.write_contract,
            self.compensation_binding,
            self.undo_policy,
        )
        if any(type(value) is not str or not value for value in values[1:] if value is not self.capabilities):
            raise ValueError("Product Action spec text is invalid")
        return values

    def _ensure_integrity(self) -> None:
        if self._identity_seal != self._identity():
            raise ValueError("Product Action spec integrity drift")

    def projection(self) -> dict[str, object]:
        self._ensure_integrity()
        return {
            "ordinal": self.ordinal,
            "action_name": self.action_name,
            "route_source": self.route_source,
            "capabilities": list(self.capabilities),
            "binding_policy": self.binding_policy,
            "route_payload_contract": self.route_payload_contract,
            "decision_payload_decoder": self.decision_payload_decoder,
            "safe_presentation": self.safe_presentation,
            "result_codec": self.result_codec,
            "write_contract": self.write_contract,
            "compensation_binding": self.compensation_binding,
            "undo_policy": self.undo_policy,
        }


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ProductActionCompensationSpecV1:
    ordinal: int
    compensation_name: str
    parent_action_name: str
    owner: str
    capability: str
    result_contract: Literal["compensation_json_v1"]
    undo_policy: Literal["not_applicable"]
    _identity_seal: object = field(default=_SPEC_SEAL_SENTINEL, init=False, repr=False)

    def __post_init__(self) -> None:
        identity = self._identity()
        if self._identity_seal is _SPEC_SEAL_SENTINEL:
            object.__setattr__(self, "_identity_seal", identity)
        elif self._identity_seal != identity:
            raise ValueError("Product Action compensation spec integrity drift")

    def _identity(self) -> tuple[object, ...]:
        identity = (
            self.ordinal,
            self.compensation_name,
            self.parent_action_name,
            self.owner,
            self.capability,
            self.result_contract,
            self.undo_policy,
        )
        if (
            type(self.ordinal) is not int
            or not 1 <= self.ordinal <= 2
            or self.compensation_name not in PRODUCT_ACTION_COMPENSATION_NAMES
            or self.parent_action_name not in PRODUCT_ACTION_NAMES
            or self.result_contract != "compensation_json_v1"
            or self.undo_policy != "not_applicable"
            or any(type(value) is not str or not value for value in identity[1:])
        ):
            raise ValueError("Product Action compensation spec is invalid")
        return identity

    def _ensure_integrity(self) -> None:
        if self._identity_seal != self._identity():
            raise ValueError("Product Action compensation spec integrity drift")

    def projection(self) -> dict[str, object]:
        self._ensure_integrity()
        return {
            "ordinal": self.ordinal,
            "compensation_name": self.compensation_name,
            "parent_action_name": self.parent_action_name,
            "owner": self.owner,
            "capability": self.capability,
            "result_contract": self.result_contract,
            "undo_policy": self.undo_policy,
        }


_ACTION_SPECS = (
    ProductActionSpecV1(
        ordinal=1,
        action_name="confirm_interview_story",
        route_source="interview_story_owner",
        capabilities=("stories.write",),
        binding_policy="canonical_story_owner_v1",
        route_payload_contract="story_proposal_route_v1",
        decision_payload_decoder="confirm_interview_story_decision_v1",
        safe_presentation="confirm_interview_story_safe_v1",
        result_codec="product_action_json_v1",
        write_contract="interview_story_write_v1",
        compensation_binding="undo:confirm_interview_story",
        undo_policy="required",
    ),
    ProductActionSpecV1(
        ordinal=2,
        action_name="save_review_readiness_signal",
        route_source="review_readiness_focus_owner",
        capabilities=("application.interview_readiness_feedback.write",),
        binding_policy="canonical_interview_review_owner_v1",
        route_payload_contract="review_focus_route_v1",
        decision_payload_decoder="save_review_readiness_signal_decision_v1",
        safe_presentation="save_review_readiness_signal_safe_v1",
        result_codec="product_action_json_v1",
        write_contract="readiness_signal_write_v1",
        compensation_binding="undo:save_review_readiness_signal",
        undo_policy="required",
    ),
)

_COMPENSATION_SPECS = (
    ProductActionCompensationSpecV1(
        ordinal=1,
        compensation_name="undo:confirm_interview_story",
        parent_action_name="confirm_interview_story",
        owner="interview_story_owner",
        capability="stories.write",
        result_contract="compensation_json_v1",
        undo_policy="not_applicable",
    ),
    ProductActionCompensationSpecV1(
        ordinal=2,
        compensation_name="undo:save_review_readiness_signal",
        parent_action_name="save_review_readiness_signal",
        owner="review_readiness_signal_owner",
        capability="application.interview_readiness_feedback.write",
        result_contract="compensation_json_v1",
        undo_policy="not_applicable",
    ),
)


class ProductActionCatalogV1:
    __slots__ = ("_registry", "_ordered_specs", "_fingerprint", "_integrity_seal")
    _registry: ProductActionProofRegistryV1
    _ordered_specs: tuple[ProductActionSpecV1, ...]
    _fingerprint: str
    _integrity_seal: tuple[
        ProductActionProofRegistryV1,
        tuple[ProductActionSpecV1, ...],
        str,
    ]

    def __init__(self, registry: ProductActionProofRegistryV1) -> None:
        if type(registry) is not ProductActionProofRegistryV1:
            raise TypeError("Product Action Catalog requires an exact proof Registry")
        projection = {
            "schema_version": 1,
            "contract": "product-action-catalog-v1",
            "actions": [spec.projection() for spec in _ACTION_SPECS],
        }
        fingerprint = "sha256:" + hashlib.sha256(
            canonical_product_action_json(cast(JSONValue, projection)).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_ordered_specs", _ACTION_SPECS)
        object.__setattr__(self, "_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "_integrity_seal",
            (registry, _ACTION_SPECS, fingerprint),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action Catalog is sealed")

    def _ensure_integrity(self) -> None:
        for spec in self._ordered_specs:
            spec._ensure_integrity()
        projection = {
            "schema_version": 1,
            "contract": "product-action-catalog-v1",
            "actions": [spec.projection() for spec in self._ordered_specs],
        }
        fingerprint = "sha256:" + hashlib.sha256(
            canonical_product_action_json(cast(JSONValue, projection)).encode("utf-8")
        ).hexdigest()
        if self._integrity_seal != (
            self._registry,
            self._ordered_specs,
            self._fingerprint,
        ) or fingerprint != self._fingerprint:
            raise ValueError("Product Action Catalog integrity drift")

    @property
    def fingerprint(self) -> str:
        self._ensure_integrity()
        return self._fingerprint

    @property
    def ordered_specs(self) -> tuple[ProductActionSpecV1, ...]:
        self._ensure_integrity()
        return self._ordered_specs

    def names(self) -> tuple[str, ...]:
        self._ensure_integrity()
        return PRODUCT_ACTION_NAMES

    def metadata_projection(self) -> dict[str, object]:
        self._ensure_integrity()
        return {
            "schema_version": 1,
            "contract": "product-action-catalog-v1",
            "actions": [spec.projection() for spec in self._ordered_specs],
        }

    def resolve(
        self,
        proof: ProductActionRouteProof,
        *,
        expected_binding: tuple[object, ...],
    ) -> ProductActionSpecV1:
        self._ensure_integrity()
        action_name, binding = self._registry._issued_identity(proof)
        if binding != expected_binding:
            raise ValueError("Product Action proof binding mismatch")
        self._registry.require_issued(
            proof,
            proof_type=ProductActionRouteProof,
            action_name=action_name,
            expected_binding=expected_binding,
        )
        for spec in self._ordered_specs:
            if spec.action_name == action_name:
                return spec
        raise ValueError("Product Action proof action is outside the Catalog")

    def resolve_historical_story(
        self,
        proof: HistoricalStoryRouteProof,
        *,
        expected_binding: tuple[object, ...],
    ) -> ProductActionSpecV1:
        self._ensure_integrity()
        action_name, binding = self._registry._issued_identity(proof)
        if binding != expected_binding:
            raise ValueError("Historical Story proof binding mismatch")
        self._registry.require_issued(
            proof,
            proof_type=HistoricalStoryRouteProof,
            action_name="confirm_interview_story",
            expected_binding=expected_binding,
        )
        if action_name != "confirm_interview_story":
            raise ValueError("Historical Story proof action mismatch")
        return self._ordered_specs[0]


class ProductActionCompensationCatalogV1:
    __slots__ = ("_ordered_specs", "_integrity_seal")
    _ordered_specs: tuple[ProductActionCompensationSpecV1, ...]
    _integrity_seal: tuple[ProductActionCompensationSpecV1, ...]

    def __init__(self) -> None:
        object.__setattr__(self, "_ordered_specs", _COMPENSATION_SPECS)
        object.__setattr__(self, "_integrity_seal", _COMPENSATION_SPECS)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action Compensation Catalog is sealed")

    def _ensure_integrity(self) -> None:
        if self._integrity_seal is not self._ordered_specs:
            raise ValueError("Product Action Compensation Catalog integrity drift")
        for spec in self._ordered_specs:
            spec._ensure_integrity()

    @property
    def ordered_specs(self) -> tuple[ProductActionCompensationSpecV1, ...]:
        self._ensure_integrity()
        return self._ordered_specs

    def names(self) -> tuple[str, ...]:
        self._ensure_integrity()
        return PRODUCT_ACTION_COMPENSATION_NAMES

    def resolve_metadata(self, name: str) -> ProductActionCompensationSpecV1 | None:
        self._ensure_integrity()
        return next((spec for spec in self._ordered_specs if spec.compensation_name == name), None)


__all__ = [
    "ProductActionCatalogV1",
    "ProductActionCompensationCatalogV1",
    "ProductActionCompensationSpecV1",
    "ProductActionSpecV1",
]
