"""Strict, bounded decoding for persisted Typed pending arguments.

The decoder is deliberately independent from Tool schemas and execution
authority.  It only turns an already topology-validated Ledger payload into a
bounded JSON object suitable for an integrity fingerprint check.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Final, NoReturn, cast

from offerpilot.ai.tool_runtime.contracts import JSONValue


_MAX_ENCODED_BYTES: Final = 65_536
_MAX_CONTAINER_DEPTH: Final = 32
_MAX_AGGREGATE_MEMBERS: Final = 2_048
_MAX_AGGREGATE_STRING_BYTES: Final = 65_536
_INTEGRITY_MESSAGE: Final = "pending replay arguments failed integrity validation"

# Kept as a module seam so limit/error behavior can be tested without admitting
# an alternate decoder through the production constructor.
_json_loads = json.loads


class PendingReplayIntegrityError(ValueError):
    """A persisted pending argument payload cannot be replayed safely."""

    def __init__(self) -> None:
        super().__init__(_INTEGRITY_MESSAGE)


def _integrity_error() -> NoReturn:
    raise PendingReplayIntegrityError


def _object_pairs(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            _integrity_error()
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    del value
    _integrity_error()


def _parse_integer(raw: str) -> int:
    value = int(raw, 10)
    # Python's canonical JSON represents integers using this exact spelling.
    # In particular, JSON's otherwise valid ``-0`` must not alias ``0``.
    if str(value) != raw:
        _integrity_error()
    return value


def _parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        _integrity_error()
    try:
        source = Decimal(raw)
        canonical_raw = json.dumps(value, allow_nan=False)
        canonical = Decimal(canonical_raw)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise PendingReplayIntegrityError from exc
    # Do not accept underflow or precision loss that would cause a different
    # number to be covered by the canonical Ledger fingerprint.  Exact spelling
    # also matters: persisted Typed proposals use this same canonical encoder,
    # so aliases such as ``1e0`` and ``1.00`` are integrity failures.
    if source != canonical or raw != canonical_raw:
        _integrity_error()
    return value


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise PendingReplayIntegrityError from exc


def _validate_decoded(value: object) -> dict[str, JSONValue]:
    if type(value) is not dict:
        _integrity_error()

    member_count = 0
    string_bytes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if type(current) is dict:
            if depth > _MAX_CONTAINER_DEPTH:
                _integrity_error()
            current_mapping = cast(dict[object, object], current)
            member_count += len(current_mapping)
            if member_count > _MAX_AGGREGATE_MEMBERS:
                _integrity_error()
            for key, item in current_mapping.items():
                if type(key) is not str:
                    _integrity_error()
                string_bytes += _utf8_size(key)
                if string_bytes > _MAX_AGGREGATE_STRING_BYTES:
                    _integrity_error()
                if type(item) in {dict, list}:
                    stack.append((item, depth + 1))
                else:
                    stack.append((item, depth))
            continue
        if type(current) is list:
            if depth > _MAX_CONTAINER_DEPTH:
                _integrity_error()
            current_list = cast(list[object], current)
            member_count += len(current_list)
            if member_count > _MAX_AGGREGATE_MEMBERS:
                _integrity_error()
            for item in current_list:
                if type(item) in {dict, list}:
                    stack.append((item, depth + 1))
                else:
                    stack.append((item, depth))
            continue
        if type(current) is str:
            string_bytes += _utf8_size(current)
            if string_bytes > _MAX_AGGREGATE_STRING_BYTES:
                _integrity_error()
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float:
            if not math.isfinite(current):
                _integrity_error()
        else:
            _integrity_error()

    validated = cast(dict[str, JSONValue], value)
    # Exercise the exact canonical encoder used by current fingerprints.  This
    # closes values (notably excessively large integers) that the parser can
    # materialize but the canonical JSON implementation cannot represent.
    json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return validated


class PendingReplayArgsDecoderV1:
    """Decode one persisted JSON object under fixed replay resource limits."""

    def decode(self, raw: str) -> dict[str, JSONValue]:
        if type(raw) is not str:
            _integrity_error()
        try:
            if _utf8_size(raw) > _MAX_ENCODED_BYTES:
                _integrity_error()
            decoded = _json_loads(
                raw,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
                parse_int=_parse_integer,
                parse_float=_parse_float,
            )
            return _validate_decoded(decoded)
        except PendingReplayIntegrityError:
            raise
        except Exception as exc:
            # JSON decode errors, recursion exhaustion, integer conversion
            # limits, and other ordinary decoder/limit failures have one typed
            # integrity outcome.  BaseException remains cancellation-safe.
            raise PendingReplayIntegrityError from exc
