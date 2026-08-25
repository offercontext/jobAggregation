from __future__ import annotations

import hashlib
from types import MappingProxyType

import pytest

from offerpilot.ai.tool_runtime.metadata import (
    canonical_json_bytes,
    canonical_sha256,
    freeze_json,
    materialize_json,
)


def test_freeze_json_snapshots_and_deep_freezes_nested_values() -> None:
    source = {
        "items": [{"enabled": True, "ordinal": 1}],
        "label": "投递",
    }

    frozen = freeze_json(source)
    source["items"][0]["enabled"] = False
    source["items"].append({"enabled": False, "ordinal": 2})
    source["label"] = "changed"

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["items"], tuple)
    assert isinstance(frozen["items"][0], MappingProxyType)
    assert materialize_json(frozen) == {
        "items": [{"enabled": True, "ordinal": 1}],
        "label": "投递",
    }
    with pytest.raises(TypeError):
        frozen["extra"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["items"][0]["enabled"] = False  # type: ignore[index]


def test_materialize_json_returns_independent_deep_mutable_copies() -> None:
    frozen = freeze_json({"items": [{"values": [1, 2]}]})

    first = materialize_json(frozen)
    second = materialize_json(frozen)
    first["items"][0]["values"].append(3)

    assert first == {"items": [{"values": [1, 2, 3]}]}
    assert second == {"items": [{"values": [1, 2]}]}
    assert materialize_json(frozen) == {"items": [{"values": [1, 2]}]}


def test_canonical_json_is_compact_sorted_utf8_and_sha256_is_prefixed() -> None:
    frozen = freeze_json(
        {
            "z": [True, None, 1, 1.5],
            "a": "值",
        }
    )
    expected = b'{"a":"\xe5\x80\xbc","z":[true,null,1,1.5]}'

    assert canonical_json_bytes(frozen) == expected
    assert canonical_sha256(frozen) == "sha256:" + hashlib.sha256(expected).hexdigest()


def test_canonical_json_does_not_normalize_unicode() -> None:
    composed = freeze_json({"text": "\u00e9"})
    decomposed = freeze_json({"text": "e\u0301"})

    assert materialize_json(composed)["text"] != materialize_json(decomposed)["text"]
    assert canonical_json_bytes(composed) != canonical_json_bytes(decomposed)
    assert canonical_sha256(composed) != canonical_sha256(decomposed)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_freeze_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        freeze_json({"value": value})


@pytest.mark.parametrize("key", (1, True, None, ("tuple",)))
def test_freeze_json_requires_exact_string_mapping_keys(key: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        freeze_json({key: "value"})


def test_json_booleans_remain_distinct_from_integers() -> None:
    frozen = freeze_json({"boolean": True, "integer": 1})
    materialized = materialize_json(frozen)

    assert type(materialized["boolean"]) is bool
    assert type(materialized["integer"]) is int
    assert canonical_json_bytes(frozen) == b'{"boolean":true,"integer":1}'


@pytest.mark.parametrize("value", (object(), b"bytes", {"items": {1, 2}}))
def test_freeze_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        freeze_json(value)
