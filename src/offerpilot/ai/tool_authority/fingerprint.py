"""Canonical public and Ledger-bound fingerprints for scoped authority.

This module intentionally accepts a Ledger key as an argument.  It never loads
keys, reads files, or consults a Journal/Provider.  Scope data is reduced to a
small canonical envelope before HMAC so entity bodies, arguments, credentials,
timestamps, ORM values, and dependency policy cannot accidentally enter the
authorization identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Final

from .policy import AuthorityPolicyError


SCOPE_HMAC_DOMAIN: Final = "write-operation-authorization-scope-v1"
SCOPE_HMAC_PREFIX: Final = "hmac-sha256:"
PUBLIC_SHA256_PREFIX: Final = "sha256:"
_MAX_INT64: Final = 2**63 - 1
_SCOPE_TYPES: Final[frozenset[str]] = frozenset(
    {"workspace", "global", "application", "mode"}
)


def canonical_json(value: Any) -> str:
    """Return the exact canonical JSON representation used by fingerprints."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_fingerprint(value: Any) -> str:
    """Hash canonical UTF-8 JSON with the public SHA-256 prefix."""

    return PUBLIC_SHA256_PREFIX + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_int64(value: object, field_name: str, *, nonnegative: bool = False) -> int:
    lower = 0 if nonnegative else 1
    if type(value) is not int or value < lower or value > _MAX_INT64:
        range_name = "non-negative" if nonnegative else "positive"
        raise AuthorityPolicyError(f"{field_name} must be a {range_name} signed int64")
    return value


def _canonical_application_ref(value: object) -> int:
    if type(value) is int:
        return _require_int64(value, "context_ref")
    if type(value) is not str or not value or not value.isascii():
        raise AuthorityPolicyError("application context_ref must be a canonical decimal integer")
    if not value.isdecimal() or value.startswith("0"):
        raise AuthorityPolicyError("application context_ref must be a canonical decimal integer")
    converted = int(value, 10)
    return _require_int64(converted, "context_ref")


def _canonical_mode(value: object) -> str:
    if type(value) is not str or not value:
        raise AuthorityPolicyError("mode must be a non-empty string")
    if len(value) > 64 or len(value.encode("utf-8")) > 256:
        raise AuthorityPolicyError("mode exceeds its lexical bound")
    if value != value.strip() or any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise AuthorityPolicyError("mode contains an invalid character")
    return value


def _canonical_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or not value.startswith(PUBLIC_SHA256_PREFIX):
        raise AuthorityPolicyError(f"{field_name} must be a public sha256 fingerprint")
    digest = value[len(PUBLIC_SHA256_PREFIX) :]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AuthorityPolicyError(f"{field_name} must be lowercase hexadecimal")
    return value


def canonical_scope_envelope(
    *,
    conversation_id: int,
    conversation_scope_revision: int,
    context_type: str,
    context_ref: int | str | None,
    mode: str,
    capability_profile_id: str,
    capability_policy_version: str,
    binding_policy_version: str,
    capability_profile_fingerprint: str,
    binding_policy_fingerprint: str,
) -> dict[str, object]:
    """Canonicalize exactly the fields covered by the scope HMAC."""

    _require_int64(conversation_id, "conversation_id")
    _require_int64(conversation_scope_revision, "conversation_scope_revision", nonnegative=True)
    if type(context_type) is not str or context_type not in _SCOPE_TYPES:
        raise AuthorityPolicyError("context_type is not a canonical scope type")
    if context_type == "application":
        canonical_ref: int | None = _canonical_application_ref(context_ref)
    else:
        if context_ref is not None:
            raise AuthorityPolicyError("non-application scope context_ref must be null")
        canonical_ref = None
    _canonical_mode(mode)
    for value, name in (
        (capability_profile_id, "capability_profile_id"),
        (capability_policy_version, "capability_policy_version"),
        (binding_policy_version, "binding_policy_version"),
    ):
        if type(value) is not str or not value:
            raise AuthorityPolicyError(f"{name} must be a non-empty string")
    _canonical_sha256(capability_profile_fingerprint, "capability_profile_fingerprint")
    _canonical_sha256(binding_policy_fingerprint, "binding_policy_fingerprint")
    return {
        "conversation_id": conversation_id,
        "conversation_scope_revision": conversation_scope_revision,
        "context_type": context_type,
        "context_ref": canonical_ref,
        "mode": mode,
        "capability_profile_id": capability_profile_id,
        "capability_policy_version": capability_policy_version,
        "binding_policy_version": binding_policy_version,
        "capability_profile_fingerprint": capability_profile_fingerprint,
        "binding_policy_fingerprint": binding_policy_fingerprint,
    }


def _ledger_secret(key: object) -> bytes:
    if type(key) is bytes:
        if not key:
            raise AuthorityPolicyError("Ledger HMAC key must not be empty")
        return key
    # LedgerKeyDomain is intentionally not imported here: authority fingerprint
    # remains independent from persistence.  Reading ``secret`` may itself
    # raise BaseException, which must propagate unchanged.
    secret = getattr(key, "secret")
    if type(secret) is not bytes or not secret:
        raise AuthorityPolicyError("Ledger HMAC key must expose non-empty bytes")
    return secret


def hmac_fingerprint(key: object, domain: str, value: Any) -> str:
    """Compute a domain-separated Ledger-style HMAC over canonical JSON."""

    if type(domain) is not str or not domain or not domain.isascii():
        raise AuthorityPolicyError("HMAC domain must be non-empty ASCII")
    secret = _ledger_secret(key)
    encoded = canonical_json(value).encode("utf-8")
    digest = hmac.new(
        secret,
        domain.encode("ascii") + b"\0" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return SCOPE_HMAC_PREFIX + digest


def authorization_scope_fingerprint(
    key: object,
    *,
    conversation_id: int,
    conversation_scope_revision: int,
    context_type: str,
    context_ref: int | str | None,
    mode: str,
    capability_profile_id: str,
    capability_policy_version: str,
    binding_policy_version: str,
    capability_profile_fingerprint: str,
    binding_policy_fingerprint: str,
) -> str:
    """Compute the persisted Typed primary authorization scope fingerprint."""

    envelope = canonical_scope_envelope(
        conversation_id=conversation_id,
        conversation_scope_revision=conversation_scope_revision,
        context_type=context_type,
        context_ref=context_ref,
        mode=mode,
        capability_profile_id=capability_profile_id,
        capability_policy_version=capability_policy_version,
        binding_policy_version=binding_policy_version,
        capability_profile_fingerprint=capability_profile_fingerprint,
        binding_policy_fingerprint=binding_policy_fingerprint,
    )
    return hmac_fingerprint(key, SCOPE_HMAC_DOMAIN, envelope)


def validate_hmac_fingerprint(value: object) -> str:
    if type(value) is not str or not value.startswith(SCOPE_HMAC_PREFIX):
        raise AuthorityPolicyError("value must be a canonical hmac-sha256 fingerprint")
    digest = value[len(SCOPE_HMAC_PREFIX) :]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AuthorityPolicyError("HMAC fingerprint must be lowercase hexadecimal")
    return value


def constant_time_equal(left: object, right: object) -> bool:
    """Compare equal-length fingerprint bytes without a length timing branch."""

    if type(left) is not str or type(right) is not str or len(left) != len(right):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


scope_fingerprint = authorization_scope_fingerprint
compute_scope_fingerprint = authorization_scope_fingerprint
canonical_scope = canonical_scope_envelope


__all__ = [
    "PUBLIC_SHA256_PREFIX",
    "SCOPE_HMAC_DOMAIN",
    "SCOPE_HMAC_PREFIX",
    "authorization_scope_fingerprint",
    "canonical_json",
    "canonical_scope",
    "canonical_scope_envelope",
    "compute_scope_fingerprint",
    "constant_time_equal",
    "hmac_fingerprint",
    "scope_fingerprint",
    "sha256_fingerprint",
    "validate_hmac_fingerprint",
]
