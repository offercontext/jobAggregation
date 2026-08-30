from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.write_operations import LedgerKeyDomain
from offerpilot.db import init_database
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import ProductActionProofRegistryV1
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    LedgerKeyProfileStoreV1,
    ReviewReadinessActionIssuer,
)


KEY_ONE = LedgerKeyDomain(
    "11111111-1111-4111-8111-111111111111",
    b"1" * 32,
)
KEY_TWO = LedgerKeyDomain(
    "22222222-2222-4222-8222-222222222222",
    b"2" * 32,
)


def raw_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def signal_route(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "application_id": 41,
        "event_id": 42,
        "note_id": 43,
        "proposal_id": 44,
        "proposal_schema_version": 2,
        "focus_id": "focus-中文-1",
        "expected_note_revision": 5,
        "expected_source_fingerprint": "sha256:" + "1" * 64,
        "expected_proposal_hash": "sha256:" + "2" * 64,
        "expected_candidate_fingerprint": "sha256:" + "3" * 64,
        "user_note": "下次重点追问边界条件",
        "domain_idempotency_key": "33333333-3333-4333-8333-333333333333",
    }
    value.update(overrides)
    return value


def story_route(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "attempt_id": 51,
        "generation_revision": 6,
        "proposal_hash": "sha256:" + "4" * 64,
        "source_fingerprint": "sha256:" + "5" * 64,
        "target_story_id": 52,
        "expected_current_version_id": 53,
        "expected_story_revision": 7,
        "product_action_generation": 8,
    }
    value.update(overrides)
    return value


@pytest.fixture
def product_core() -> tuple[
    ProductActionCatalogV1,
    ProductActionProofRegistryV1,
    LedgerKeyProfileStoreV1,
    ReviewReadinessActionIssuer,
    InterviewStoryActionIssuer,
]:
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)
    profiles = LedgerKeyProfileStoreV1((KEY_ONE, KEY_TWO), active_key_id=KEY_ONE.key_id)
    return (
        catalog,
        registry,
        profiles,
        ReviewReadinessActionIssuer(catalog, registry, profiles),
        InterviewStoryActionIssuer(catalog, registry, profiles),
    )


@pytest.fixture
def product_database(tmp_path: Path) -> Any:
    return init_database(tmp_path / "product-actions.sqlite3")
