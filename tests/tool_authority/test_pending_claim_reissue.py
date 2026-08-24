from __future__ import annotations

from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import (
    AuthorityFactory,
    AuthorityPhaseError,
    PendingAuthorityClaim,
)
from tests.tool_authority.test_hardening import SHA, SHA_B, _prepared, _scope, _segment
from tests.tool_authority.test_pending_claim import _harness, _sibling_pending_claim


def _captured_sources(harness: Any) -> tuple[object, object]:
    lifecycle = harness.factory._claim_lifecycle(harness.claim)
    assert lifecycle.authority is not None
    assert lifecycle.prepared is not None
    return lifecycle.authority, lifecycle.prepared


def _reissue_captured_sources(
    harness: Any,
    authority: object,
    prepared: object,
    *,
    pending: PendingAction | None = None,
) -> PendingAuthorityClaim:
    source = harness.pending if pending is None else pending
    return harness.factory.issue_pending_claim(
        authority,  # type: ignore[arg-type]
        prepared=prepared,  # type: ignore[arg-type]
        pending=source,
        operation_id=source.operation_id,
        tool_call_id=source.tool_call_id,
        tool_name=source.tool_name,
        arguments_digest=source.arguments_digest,
        pending_action_revision=source.pending_action_revision,
        pending_confirmation_claim_id=source.pending_confirmation_claim_id,
    )


def _registered_clone(harness: Any) -> PendingAction:
    source = harness.pending
    clone = PendingAction(
        source.tool_call_id,
        source.tool_name,
        source.args,
        source.human,
        source.operation_id,
    )
    clone.bind_typed_proposal_identity(
        conversation_id=harness.conversation_id,
        pending_action_revision=source.pending_action_revision,
        pending_confirmation_claim_id=source.pending_confirmation_claim_id,
        arguments_digest=source.arguments_digest,
    )
    harness.factory.register_pending(
        clone,
        conversation_id=harness.conversation_id,
        operation_id=source.operation_id,
        tool_call_id=source.tool_call_id,
        tool_name=source.tool_name,
        pending_action_revision=source.pending_action_revision,
        pending_confirmation_claim_id=source.pending_confirmation_claim_id,
        arguments_digest=source.arguments_digest,
    )
    return clone


def _registered_cross_scope_pending(factory: AuthorityFactory) -> PendingAction:
    pending = PendingAction(
        "call-cross-segment",
        "update_application_status",
        "{}",
        "same proposal",
        "operation-cross-segment",
    )
    pending.bind_typed_proposal_identity(
        conversation_id=11,
        pending_action_revision=1,
        pending_confirmation_claim_id=pending.operation_id,
        arguments_digest=SHA,
    )
    factory.register_pending(
        pending,
        conversation_id=11,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        pending_action_revision=1,
        pending_confirmation_claim_id=pending.operation_id,
        arguments_digest=SHA,
    )
    return pending


def _issue_cross_scope_claim(
    factory: AuthorityFactory,
    authority: object,
    prepared: object,
    pending: PendingAction,
) -> PendingAuthorityClaim:
    return factory.issue_pending_claim(
        authority,  # type: ignore[arg-type]
        prepared=prepared,  # type: ignore[arg-type]
        pending=pending,
        operation_id=pending.operation_id,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        arguments_digest=SHA,
        pending_confirmation_claim_id=pending.operation_id,
    )


def _finalized_source_count(harness: Any, authority: object) -> int:
    return len(
        harness.factory._finalized_pending_claim_keys.get(id(authority), set())
    )


def test_active_pending_proposal_cannot_issue_from_a_cloned_pending(tmp_path: Path) -> None:
    harness = _harness(tmp_path, segment_id="reissue-active-clone")
    authority, prepared = _captured_sources(harness)
    clone = _registered_clone(harness)
    try:
        with pytest.raises(AuthorityPhaseError):
            _reissue_captured_sources(
                harness,
                authority,
                prepared,
                pending=clone,
            )
    finally:
        harness.close()


@pytest.mark.parametrize(
    "finalization", ("consumed", "cas_loser", "revoked", "cancelled")
)
def test_finalized_pending_proposal_sources_cannot_issue_another_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalization: str,
) -> None:
    harness = _harness(tmp_path, segment_id=f"reissue-{finalization}")
    authority, prepared = _captured_sources(harness)
    clone = _registered_clone(harness)
    try:
        if finalization == "consumed":
            harness.factory.mark_in_flight(harness.claim)
            harness.factory.consume(harness.claim)
        elif finalization == "cas_loser":
            blocker = PendingAction(
                "blocker",
                "display_pending_notice",
                "{}",
                "blocker",
            )
            assert harness.chat.set_pending_action(harness.conversation_id, blocker)
            assert not harness.chat.set_pending_action(
                harness.conversation_id,
                harness.pending,
                pending_authority_claim=harness.claim,
            )
        elif finalization == "revoked":
            harness.factory.revoke(harness.claim)
        else:
            def cancel(*_args: object, **_kwargs: object) -> None:
                raise CancelledError

            monkeypatch.setattr(harness.chat, "persist_typed_pending", cancel)
            with pytest.raises(CancelledError):
                harness.chat.persist_pending_action(
                    harness.conversation_id,
                    harness.pending,
                    [],
                    pending_authority_claim=harness.claim,
                )

        assert harness.factory.claim_state(harness.claim) is None
        assert _finalized_source_count(harness, authority) == 1
        with pytest.raises(AuthorityPhaseError):
            _reissue_captured_sources(
                harness,
                authority,
                prepared,
                pending=clone,
            )
    finally:
        harness.close()


def test_revoke_race_cannot_issue_equivalent_cloned_pending_claims(tmp_path: Path) -> None:
    harness = _harness(tmp_path, segment_id="reissue-concurrent-clones")
    authority, prepared = _captured_sources(harness)
    clones = tuple(_registered_clone(harness) for _ in range(8))

    def issue(clone: PendingAction) -> str:
        try:
            _reissue_captured_sources(
                harness,
                authority,
                prepared,
                pending=clone,
            )
        except AuthorityPhaseError:
            return "rejected"
        return "issued"

    try:
        with ThreadPoolExecutor(max_workers=9) as pool:
            revoke = pool.submit(harness.factory.revoke, harness.claim)
            attempts = tuple(pool.submit(issue, clone) for clone in clones)
            revoke.result()
            assert {attempt.result() for attempt in attempts} == {"rejected"}
        assert _finalized_source_count(harness, authority) == 1
    finally:
        harness.close()


@pytest.mark.parametrize("first_state", ("active", "finalized"))
def test_equivalent_proposal_cannot_cross_segment_authorities(first_state: str) -> None:
    with AuthorityFactory() as factory:
        first = _segment(factory)
        second = factory.create_segment_authority(
            conversation_id=11,
            conversation_scope_revision=0,
            segment_id="segment-cloned-proposal",
            trusted_scope=_scope(),
            capabilities=frozenset({"applications.read", "applications.write"}),
            capability_profile_fingerprint=SHA,
            binding_policy_fingerprint=SHA_B,
        )
        prepared_first = _prepared(
            factory,
            first,
            tool_call_id="call-cross-segment",
            tool_name="update_application_status",
            kind="write",
        )
        prepared_second = _prepared(
            factory,
            second,
            tool_call_id="call-cross-segment",
            tool_name="update_application_status",
            kind="write",
        )

        first_pending = _registered_cross_scope_pending(factory)
        second_pending = _registered_cross_scope_pending(factory)
        first_claim = _issue_cross_scope_claim(
            factory, first, prepared_first, first_pending
        )
        if first_state == "finalized":
            factory.revoke(first_claim)

        with pytest.raises(AuthorityPhaseError):
            _issue_cross_scope_claim(
                factory,
                second,
                prepared_second,
                second_pending,
            )
        if first_state == "finalized":
            factory.revoke_authority(second)
            assert len(factory._finalized_pending_claim_keys.get(id(first), set())) == 1
            assert id(second) not in factory._finalized_pending_claim_keys


def test_equivalent_semantics_are_scoped_to_independent_factories() -> None:
    with AuthorityFactory() as first_factory, AuthorityFactory() as second_factory:
        first_authority = _segment(first_factory)
        second_authority = _segment(second_factory)
        first_prepared = _prepared(
            first_factory,
            first_authority,
            tool_call_id="call-cross-segment",
            tool_name="update_application_status",
            kind="write",
        )
        second_prepared = _prepared(
            second_factory,
            second_authority,
            tool_call_id="call-cross-segment",
            tool_name="update_application_status",
            kind="write",
        )
        first_pending = _registered_cross_scope_pending(first_factory)
        second_pending = _registered_cross_scope_pending(second_factory)

        first_claim = _issue_cross_scope_claim(
            first_factory,
            first_authority,
            first_prepared,
            first_pending,
        )
        second_claim = _issue_cross_scope_claim(
            second_factory,
            second_authority,
            second_prepared,
            second_pending,
        )

        assert first_factory.claim_state(first_claim) == "issued"
        assert second_factory.claim_state(second_claim) == "issued"


def test_finalized_source_tombstone_does_not_block_a_distinct_live_proposal(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, segment_id="reissue-distinct")
    distinct_pending, distinct_claim = _sibling_pending_claim(harness)
    try:
        harness.factory.revoke(harness.claim)

        assert distinct_pending is not harness.pending
        assert harness.factory.claim_state(distinct_claim) == "issued"
    finally:
        harness.close()


def test_finalized_source_tombstone_is_not_global_across_segments(tmp_path: Path) -> None:
    first = _harness(tmp_path, segment_id="reissue-first-segment")
    second: Any | None = None
    try:
        first.factory.revoke(first.claim)
        second = _harness(tmp_path, segment_id="reissue-second-segment")

        assert second.factory.claim_state(second.claim) == "issued"
    finally:
        if second is not None:
            second.close()
        first.close()


@pytest.mark.parametrize("cleanup", ("authority", "factory"))
def test_finalized_source_tombstone_is_cleaned_with_its_authority_lifetime(
    tmp_path: Path,
    cleanup: str,
) -> None:
    harness = _harness(tmp_path, segment_id=f"reissue-cleanup-{cleanup}")
    authority, prepared = _captured_sources(harness)
    clone = _registered_clone(harness)
    try:
        harness.factory.revoke(harness.claim)
        assert _finalized_source_count(harness, authority) == 1
        with pytest.raises(AuthorityPhaseError):
            _reissue_captured_sources(
                harness,
                authority,
                prepared,
                pending=clone,
            )

        if cleanup == "authority":
            harness.factory.revoke_authority(authority)  # type: ignore[arg-type]
        else:
            harness.factory.close()

        assert _finalized_source_count(harness, authority) == 0
        assert id(clone) not in harness.factory._pending
    finally:
        harness.close()
