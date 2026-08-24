from __future__ import annotations

from asyncio import CancelledError
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.tool_authority import AuthorityPhaseError, PendingAuthorityClaim
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
) -> PendingAuthorityClaim:
    return harness.factory.issue_pending_claim(
        authority,  # type: ignore[arg-type]
        prepared=prepared,  # type: ignore[arg-type]
        pending=harness.pending,
        operation_id=harness.pending.operation_id,
        tool_call_id=harness.pending.tool_call_id,
        tool_name=harness.pending.tool_name,
        arguments_digest=harness.pending.arguments_digest,
        pending_action_revision=harness.pending.pending_action_revision,
        pending_confirmation_claim_id=harness.pending.pending_confirmation_claim_id,
    )


def _finalized_source_count(harness: Any, authority: object) -> int:
    return len(
        harness.factory._finalized_pending_claim_keys.get(id(authority), set())
    )


@pytest.mark.parametrize("finalization", ("cas_loser", "revoked", "cancelled"))
def test_finalized_pending_proposal_sources_cannot_issue_another_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalization: str,
) -> None:
    harness = _harness(tmp_path, segment_id=f"reissue-{finalization}")
    authority, prepared = _captured_sources(harness)
    try:
        if finalization == "cas_loser":
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
            _reissue_captured_sources(harness, authority, prepared)
    finally:
        harness.close()


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
    authority, _prepared = _captured_sources(harness)
    try:
        harness.factory.revoke(harness.claim)
        assert _finalized_source_count(harness, authority) == 1

        if cleanup == "authority":
            harness.factory.revoke_authority(authority)  # type: ignore[arg-type]
        else:
            harness.factory.close()

        assert _finalized_source_count(harness, authority) == 0
    finally:
        harness.close()
