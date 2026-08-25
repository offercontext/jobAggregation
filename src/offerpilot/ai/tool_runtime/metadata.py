"""Immutable JSON snapshots and canonical metadata fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias, cast, overload


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


__all__ = [
    "FrozenJSONArray",
    "FrozenJSONObject",
    "FrozenJSONValue",
    "JSONScalar",
    "JSONValue",
    "canonical_json_bytes",
    "canonical_sha256",
    "freeze_json",
    "materialize_json",
]
