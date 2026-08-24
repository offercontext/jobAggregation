from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from typing import Any

import pytest

from offerpilot.ai.tool_authority.fingerprint import (
    SCOPE_HMAC_DOMAIN,
    authorization_scope_fingerprint,
    canonical_json,
    canonical_scope_envelope,
    constant_time_equal,
    sha256_fingerprint,
)


SHA = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _scope(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "conversation_id": 11,
        "conversation_scope_revision": 0,
        "context_type": "application",
        "context_ref": 37,
        "mode": "general",
        "capability_profile_id": "agent_typed_v1",
        "capability_policy_version": "capability-policy-v1",
        "binding_policy_version": "binding-policy-v1",
        "capability_profile_fingerprint": SHA,
        "binding_policy_fingerprint": SHA_B,
    }
    value.update(overrides)
    return value


def test_canonical_json_is_exact_and_rejects_non_finite_values() -> None:
    value = {"界": "e\u0301", "z": [1, True, None]}
    expected = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert canonical_json(value) == expected
    assert canonical_json({"text": "é"}) != canonical_json({"text": "e\u0301"})
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"value": float("inf")})


def test_public_sha256_is_independently_reproducible() -> None:
    value = {"schema": "example", "text": "中文"}
    expected = "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    assert sha256_fingerprint(value) == expected


def test_scope_envelope_canonicalizes_application_string_and_integer_refs() -> None:
    integer = canonical_scope_envelope(**_scope(context_ref=37))
    string = canonical_scope_envelope(**_scope(context_ref="37"))
    assert integer == string
    assert integer["context_ref"] == 37


@pytest.mark.parametrize("context_type", ["workspace", "global", "mode"])
def test_non_application_scope_refs_are_canonical_null(context_type: str) -> None:
    envelope = canonical_scope_envelope(
        **_scope(context_type=context_type, context_ref=None)
    )
    assert envelope["context_ref"] is None


@pytest.mark.parametrize("context_ref", [True, False, 1.0, "01", "+1", " 1", "1 ", "0", -1, ""])
def test_application_ref_rejects_noncanonical_values(context_ref: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_scope_envelope(**_scope(context_ref=context_ref))


def test_scope_hmac_uses_exact_domain_and_excludes_dependency_policy_version() -> None:
    key = b"ledger-secret"
    envelope = canonical_scope_envelope(**_scope())
    expected = "hmac-sha256:" + hmac.new(
        key,
        SCOPE_HMAC_DOMAIN.encode("ascii")
        + b"\0"
        + canonical_json(envelope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert authorization_scope_fingerprint(key, **_scope()) == expected
    source = inspect.getsource(authorization_scope_fingerprint)
    assert "dependency_policy_version" not in source


def test_every_scope_field_is_hmac_bound_and_mode_is_case_sensitive() -> None:
    baseline = authorization_scope_fingerprint(b"ledger-secret", **_scope())
    fields = tuple(_scope())
    for field in fields:
        changed = _scope()
        value = changed[field]
        if isinstance(value, int):
            changed[field] = value + 1
        elif field == "mode":
            changed[field] = "GENERAL"
        elif field == "context_type":
            changed[field] = "workspace"
            changed["context_ref"] = None
        elif field.endswith("fingerprint"):
            changed[field] = value[:-1] + ("b" if value[-1] != "b" else "a")
        else:
            changed[field] = value + "-changed"
        assert authorization_scope_fingerprint(b"ledger-secret", **changed) != baseline


def test_scope_hmac_is_domain_separated_and_strictly_prefixed() -> None:
    baseline = authorization_scope_fingerprint(b"ledger-secret", **_scope())
    assert baseline.startswith("hmac-sha256:")
    assert len(baseline) == len("hmac-sha256:") + 64
    assert baseline == authorization_scope_fingerprint(b"ledger-secret", **_scope())
    assert authorization_scope_fingerprint(b"other-secret", **_scope()) != baseline
    with pytest.raises((TypeError, ValueError)):
        authorization_scope_fingerprint(b"", **_scope())


def test_fixed_length_constant_time_compare_rejects_length_and_prefix_mismatch() -> None:
    value = authorization_scope_fingerprint(b"ledger-secret", **_scope())
    assert constant_time_equal(value, value) is True
    assert constant_time_equal(value, value[:-1]) is False
    assert constant_time_equal(value, "x" + value[1:]) is False
    assert constant_time_equal(value, object()) is False  # type: ignore[arg-type]


def test_base_exception_propagates_from_key_provider() -> None:
    class ExplosiveKey:
        @property
        def secret(self) -> bytes:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        authorization_scope_fingerprint(ExplosiveKey(), **_scope())  # type: ignore[arg-type]
