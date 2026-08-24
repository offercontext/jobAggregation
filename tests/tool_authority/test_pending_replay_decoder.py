from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

import offerpilot.ai.pending_replay as pending_replay
from offerpilot.ai.pending_replay import (
    PendingReplayArgsDecoderV1,
    PendingReplayIntegrityError,
)


def _raw_with_exact_size(size: int) -> str:
    envelope_size = len('{"value":""}'.encode())
    return '{"value":"' + ("a" * (size - envelope_size)) + '"}'


def _nested_object(depth: int) -> str:
    value: object = 0
    for _ in range(depth):
        value = {"item": value}
    return json.dumps(value, separators=(",", ":"))


def _assert_integrity_error(raw: str) -> None:
    with pytest.raises(PendingReplayIntegrityError):
        PendingReplayArgsDecoderV1().decode(raw)


def test_decoder_applies_byte_cap_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def counted_loads(raw: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return json.loads(raw, **kwargs)

    monkeypatch.setattr(pending_replay, "_json_loads", counted_loads)
    accepted = _raw_with_exact_size(65_536)
    assert len(accepted.encode("utf-8")) == 65_536
    assert PendingReplayArgsDecoderV1().decode(accepted)["value"] == "a" * 65_524
    assert calls == 1

    rejected = _raw_with_exact_size(65_537)
    assert len(rejected.encode("utf-8")) == 65_537
    _assert_integrity_error(rejected)
    assert calls == 1


def test_decoder_enforces_depth_32_boundary() -> None:
    assert PendingReplayArgsDecoderV1().decode(_nested_object(32))
    _assert_integrity_error(_nested_object(33))


def test_decoder_enforces_aggregate_member_and_element_boundary() -> None:
    accepted = json.dumps({"items": [None] * 2_047}, separators=(",", ":"))
    rejected = json.dumps({"items": [None] * 2_048}, separators=(",", ":"))
    assert PendingReplayArgsDecoderV1().decode(accepted)
    _assert_integrity_error(rejected)


def test_decoder_enforces_aggregate_decoded_key_and_string_utf8_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoded = {"é": ("界" * 21_844) + "aa"}
    assert sum(len(item.encode("utf-8")) for item in decoded.keys()) + len(
        decoded["é"].encode("utf-8")
    ) == 65_536
    monkeypatch.setattr(pending_replay, "_json_loads", lambda raw, **kwargs: decoded)
    assert PendingReplayArgsDecoderV1().decode("{}") == decoded

    decoded = {"é": ("界" * 21_844) + "aaa"}
    _assert_integrity_error("{}")


def test_decoder_rejects_duplicate_keys() -> None:
    _assert_integrity_error('{"same":1,"same":2}')


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_decoder_rejects_non_finite_numbers(constant: str) -> None:
    _assert_integrity_error('{"number":' + constant + "}")


@pytest.mark.parametrize(
    "number",
    [
        "1e400",  # Outside the finite range used by canonical JSON.
        "1e-400",  # Underflows and cannot round-trip through canonical JSON.
        "9007199254740993.0",  # Loses precision as a canonical JSON float.
        "-0",  # Aliases canonical integer zero.
        "1e0",
        "1.00",
        "1e+0",
        "0.10",
        "1e-7",
        "1E-7",
        "10e-8",
        "0e999",
    ],
)
def test_decoder_rejects_numeric_range_and_canonical_mismatch(number: str) -> None:
    _assert_integrity_error('{"number":' + number + "}")


def test_decoder_accepts_exact_canonical_json_numbers() -> None:
    assert PendingReplayArgsDecoderV1().decode(
        '{"integer":9007199254740993,"decimal":0.1,"exponent":1e-07}'
    ) == {
        "integer": 9_007_199_254_740_993,
        "decimal": 0.1,
        "exponent": 1e-7,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":"\\ud800"}',
        '{"value":1} trailing',
        "[]",
        "42",
        '"scalar"',
        "null",
    ],
)
def test_decoder_rejects_surrogate_trailing_and_non_mapping_root(raw: str) -> None:
    _assert_integrity_error(raw)


@pytest.mark.parametrize("failure", [RecursionError("deep"), RuntimeError("boom")])
def test_decoder_maps_recursion_and_ordinary_decode_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def fail(raw: str, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(pending_replay, "_json_loads", fail)
    _assert_integrity_error("{}")


def test_decoder_does_not_capture_base_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class StopDecode(BaseException):
        pass

    def stop(raw: str, **kwargs: object) -> object:
        raise StopDecode

    monkeypatch.setattr(pending_replay, "_json_loads", stop)
    with pytest.raises(StopDecode):
        PendingReplayArgsDecoderV1().decode("{}")


def test_integrity_error_never_exposes_pending_arguments() -> None:
    secret = "private-pending-argument"
    with pytest.raises(PendingReplayIntegrityError) as caught:
        PendingReplayArgsDecoderV1().decode('{"value":"' + secret)
    assert secret not in str(caught.value)


def test_decoder_rejects_non_string_input() -> None:
    decode = PendingReplayArgsDecoderV1().decode
    _assert_callable_rejects_non_string(decode)


def _assert_callable_rejects_non_string(decode: Callable[[Any], object]) -> None:
    with pytest.raises(PendingReplayIntegrityError):
        decode(b"{}")
