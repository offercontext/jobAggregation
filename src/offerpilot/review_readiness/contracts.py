"""Closed, bounded contracts for Review-to-Readiness V1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import UUID

from offerpilot.product_actions.contracts import JSONValue


CandidateProjectionState: TypeAlias = Literal[
    "ready",
    "already_confirmed",
    "legacy_requires_regeneration",
    "source_changed",
    "source_missing",
    "not_eligible",
    "unavailable",
]


class ReviewReadinessContractError(ValueError):
    """A request or trusted source violates the closed readiness contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessEvidenceV1:
    ordinal: int
    source_path: str
    excerpt: str
    excerpt_sha256: str
    source_field_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessCandidateV1:
    application_id: int
    event_id: int
    note_id: int
    proposal_id: int
    proposal_schema_version: Literal[2]
    focus_id: str
    statement_text: str
    source_note_revision: int
    source_note_fingerprint: str
    source_proposal_hash: str
    candidate_fingerprint: str
    evidence: tuple[ReadinessEvidenceV1, ...]


@dataclass(frozen=True, slots=True, repr=False)
class CandidateProjectionV1:
    state: CandidateProjectionState
    note_id: int
    proposal_id: int
    candidates: tuple[ReadinessCandidateV1, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class SignalProposalRequestV1:
    proposal_id: int
    focus_id: str
    expected_note_revision: int
    expected_candidate_fingerprint: str
    idempotency_key: str
    user_note: str


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessSignalWriteResultV1:
    signal_id: int
    signal_version_id: int
    signal_revision: int


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessSignalAggregateV1:
    signal_id: int
    application_id: int
    source_event_id: int | None
    source_note_id: int | None
    source_proposal_id: int | None
    focus_id: str
    current_version_id: int
    signal_revision: int
    version_number: int
    disposition: str
    statement_text: str
    user_note: str
    source_note_revision: int
    source_note_fingerprint: str
    source_proposal_hash: str
    candidate_fingerprint: str
    domain_idempotency_key: str
    write_operation_id: str
    evidence: tuple[ReadinessEvidenceV1, ...]


_SIGNAL_REQUEST_FIELDS = {
    "proposal_id",
    "focus_id",
    "expected_note_revision",
    "expected_candidate_fingerprint",
    "idempotency_key",
    "user_note",
}


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ReviewReadinessContractError(f"{field}_invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ReviewReadinessContractError(f"{field}_invalid")
    return value


def _uuid(value: object, field: str) -> str:
    if type(value) is not str:
        raise ReviewReadinessContractError(f"{field}_invalid")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ReviewReadinessContractError(f"{field}_invalid") from exc
    if value != canonical:
        raise ReviewReadinessContractError(f"{field}_invalid")
    return canonical


def _focus_id(value: object) -> str:
    if type(value) is not str:
        raise ReviewReadinessContractError("focus_id_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewReadinessContractError("focus_id_invalid") from exc
    if (
        not 1 <= len(encoded) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ReviewReadinessContractError("focus_id_invalid")
    return value


def validate_user_note(value: object) -> str:
    if type(value) is not str:
        raise ReviewReadinessContractError("user_note_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewReadinessContractError("user_note_invalid") from exc
    if len(value) > 500 or len(encoded) > 2_048:
        raise ReviewReadinessContractError("user_note_too_large")
    return value


def decode_signal_proposal_request(
    value: dict[str, JSONValue],
) -> SignalProposalRequestV1:
    if type(value) is not dict or set(value) != _SIGNAL_REQUEST_FIELDS:
        raise ReviewReadinessContractError("product_action_invalid_request")
    return SignalProposalRequestV1(
        proposal_id=_positive_int(value["proposal_id"], "proposal_id"),
        focus_id=_focus_id(value["focus_id"]),
        expected_note_revision=_positive_int(
            value["expected_note_revision"],
            "expected_note_revision",
        ),
        expected_candidate_fingerprint=_sha256(
            value["expected_candidate_fingerprint"],
            "expected_candidate_fingerprint",
        ),
        idempotency_key=_uuid(value["idempotency_key"], "idempotency_key"),
        user_note=validate_user_note(value["user_note"]),
    )


__all__ = [
    "CandidateProjectionState",
    "CandidateProjectionV1",
    "ReadinessCandidateV1",
    "ReadinessEvidenceV1",
    "ReadinessSignalAggregateV1",
    "ReadinessSignalWriteResultV1",
    "ReviewReadinessContractError",
    "SignalProposalRequestV1",
    "decode_signal_proposal_request",
    "validate_user_note",
]
