from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import inspect
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import update

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.types import Message
from offerpilot.ai.write_operations import (
    WriteOperationRepository,
    load_or_create_ledger_key,
)
from offerpilot.db import init_database
from offerpilot.models import Conversation, WriteOperation
from offerpilot.repositories.chat import ChatRepository
from tests.tool_authority.test_pending_claim import (
    clarification_pending_route,
    legacy_pending_route,
)


def _set_pending(repo: ChatRepository, conversation_id: int, pending: PendingAction) -> bool:
    with clarification_pending_route(pending, conversation_id) as route_handle:
        return repo.set_pending_action(
            conversation_id,
            pending,
            route_handle=route_handle,
        )


def _persist_pending(
    repo: ChatRepository,
    conversation_id: int,
    pending: PendingAction,
    messages: list[dict[str, str]],
) -> bool:
    with clarification_pending_route(pending, conversation_id) as route_handle:
        return repo.persist_pending_action(
            conversation_id,
            pending,
            messages,
            route_handle=route_handle,
        )


def _replace_legacy_pending(
    repo: ChatRepository,
    conversation_id: int,
    expected: PendingAction,
    replacement: PendingAction,
    tool_message: Message,
    undo: dict[str, object] | None,
    **kwargs: object,
):
    with legacy_pending_route(
        replacement,
        conversation_id,
        source="jd_clarification",
    ) as route_handle:
        return repo.replace_pending_confirmation(
            conversation_id,
            expected,
            replacement,
            tool_message,
            undo,
            route_handle=route_handle,
            **kwargs,
        )


def _persist_continuation(
    repo: ChatRepository,
    conversation_id: int,
    generation: datetime | None,
    messages: list[dict[str, str]],
    *,
    pending: PendingAction | None = None,
):
    if pending is None:
        return repo.persist_confirmation_continuation(
            conversation_id,
            generation,
            messages,
            route_handle=None,
        )
    with clarification_pending_route(pending, conversation_id) as route_handle:
        return repo.persist_confirmation_continuation(
            conversation_id,
            generation,
            messages,
            pending=pending,
            route_handle=route_handle,
        )


@pytest.mark.parametrize(
    "method_name",
    (
        "set_pending_action",
        "persist_pending_action",
        "replace_pending_confirmation",
        "persist_confirmation_continuation",
        "set_pending_clarification",
    ),
)
def test_task11_pending_writes_require_an_exact_transient_route_handle(method_name):
    parameters = inspect.signature(getattr(ChatRepository, method_name)).parameters

    assert "route_handle" in parameters
    assert parameters["route_handle"].default is inspect.Parameter.empty


def test_task11_persists_only_approved_route_identity_primitives():
    forbidden = {
        "route_handle",
        "bundle_instance_token",
        "segment_catalog_token",
        "legacy_route_proof",
    }

    assert forbidden.isdisjoint(PendingAction.__dataclass_fields__)
    assert forbidden.isdisjoint(WriteOperation.__table__.columns.keys())


def test_archive_update_distinguishes_missing_pending_and_success(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    pending_conversation = repo.create_conversation("pending")
    active_conversation = repo.create_conversation("active")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    assert _set_pending(repo, pending_conversation.id, pending) is True

    missing = repo.update_conversation_for_archive(
        999_999, {"archived_at": datetime.now(timezone.utc)}
    )
    blocked = repo.update_conversation_for_archive(
        pending_conversation.id, {"archived_at": datetime.now(timezone.utc)}
    )
    updated = repo.update_conversation_for_archive(
        active_conversation.id, {"archived_at": datetime.now(timezone.utc)}
    )

    assert missing.status == "not_found"
    assert missing.conversation is None
    assert blocked.status == "pending"
    assert blocked.conversation is None
    assert updated.status == "updated"
    assert updated.conversation is not None
    assert updated.conversation.archived_at is not None


def test_archive_and_pending_creation_are_mutually_exclusive_under_race(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("race")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    barrier = Barrier(2)

    def archive():
        barrier.wait()
        return repo.update_conversation_for_archive(
            conversation.id, {"archived_at": datetime.now(timezone.utc)}
        ).status

    def create_pending():
        barrier.wait()
        return _set_pending(repo, conversation.id, pending)

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_result = pool.submit(archive)
        pending_result = pool.submit(create_pending)
        archive_status = archive_result.result()
        pending_created = pending_result.result()

    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert (archive_status == "updated") != pending_created
    assert not (stored.archived_at is not None and repo.get_pending_action(conversation.id))


def test_pending_action_cannot_be_added_after_archive(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("archived")
    archived = repo.update_conversation_for_archive(
        conversation.id, {"archived_at": datetime.now(timezone.utc)}
    )

    created = _set_pending(
        repo,
        conversation.id,
        PendingAction("write-1", "display_pending_notice", '{"id":1}', "update"),
    )

    assert archived.status == "updated"
    assert created is False
    assert repo.get_pending_action(conversation.id) is None


def test_reading_old_typed_pending_never_lazy_backfills_ledger(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    repo = ChatRepository(session_factory)
    conversation = repo.create_conversation("old-unbound-typed")
    operation_id = str(uuid4())
    with session_factory() as session:
        stored = session.get(Conversation, conversation.id)
        assert stored is not None
        stored.pending_tool_call_id = "old-call"
        stored.pending_operation_id = operation_id
        stored.pending_tool_name = "update_application_status"
        stored.pending_args = '{"id":1,"status":"offer"}'
        stored.pending_human = "update"
        session.commit()

    pending = repo.get_pending_action(conversation.id)

    assert pending is not None
    assert pending.operation_id == operation_id
    with session_factory() as session:
        assert session.get(WriteOperation, operation_id) is None


def test_pending_action_and_proposal_messages_are_atomic_when_archived(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("archived")
    archived = repo.update_conversation_for_archive(
        conversation.id, {"archived_at": datetime.now(timezone.utc)}
    )
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")

    persisted = _persist_pending(
        repo,
        conversation.id,
        pending,
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": '[{"id":"write-1"}]',
                "tool_call_id": "",
                "provider_blocks": "",
            }
        ],
    )

    assert archived.status == "updated"
    assert persisted is False
    assert repo.get_pending_action(conversation.id) is None
    assert repo.list_messages(conversation.id) == []


def test_resolve_pending_confirmation_atomically_persists_result_and_clears_state(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(repo, conversation.id, pending)
    with clarification_pending_route(pending, conversation.id) as route_handle:
        repo.set_pending_clarification(
            conversation.id,
            pending,
            "clarify",
            route_handle=route_handle,
        )
    repo.set_last_write_undo(conversation.id, {"kind": "previous"})
    tool_message = Message(role="tool", content='{"id":1,"status":"offer"}', tool_call_id="write-1")
    undo = {"kind": "update_application_status", "application_id": 1}

    resolved = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        tool_message,
        undo,
    )
    replayed = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        tool_message,
        undo,
    )

    assert resolved is not None
    assert replayed is None
    assert repo.get_pending_action(conversation.id) is None
    assert repo.get_pending_clarification(conversation.id) is None
    assert repo.get_last_write_undo(conversation.id) == undo
    messages = repo.list_messages(conversation.id)
    assert [(message.role, message.tool_call_id) for message in messages] == [("tool", "write-1")]


def test_pending_confirmation_claim_is_durable_private_and_single_winner(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(repo, conversation.id, pending)

    assert repo.claim_pending_confirmation(conversation.id, pending, "claim-one") is True
    assert repo.claim_pending_confirmation(conversation.id, pending, "claim-two") is False
    assert repo.get_pending_action(conversation.id) == pending

    wrong = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content="wrong", tool_call_id="write-1"),
        {},
        claim_id="claim-two",
    )
    resolved = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content='{"id":1}', tool_call_id="write-1"),
        {},
        claim_id="claim-one",
    )

    assert wrong is None
    assert resolved is not None
    assert repo.get_pending_action(conversation.id) is None
    assert [message.content for message in repo.list_messages(conversation.id)] == ['{"id":1}']


def test_pending_confirmation_claim_never_rewrites_provider_tool_call_id(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    provider_id = "\x1eofferpilot-confirmation-claim:provider:owned"
    pending = PendingAction(
        provider_id,
        "display_pending_notice",
        '{"id":1}',
        "update",
    )
    _set_pending(repo, conversation.id, pending)

    assert repo.claim_pending_confirmation(conversation.id, pending, "claim-one") is True
    assert repo.get_pending_action(conversation.id) == pending
    stored = repo.get_conversation(conversation.id)
    assert stored is not None
    assert stored.pending_tool_call_id == provider_id
    assert stored.pending_confirmation_claim_id == "claim-one"


def test_generic_pending_mutations_cannot_clear_or_replace_active_claim(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    replacement = PendingAction("write-2", "display_pending_notice", '{"id":2}', "new")
    _set_pending(repo, conversation.id, pending)
    assert repo.claim_pending_confirmation(conversation.id, pending, "claim-one") is True

    repo.clear_pending_action(conversation.id)
    assert _set_pending(repo, conversation.id, replacement) is False
    assert _persist_pending(repo, conversation.id, replacement, []) is False

    assert repo.get_pending_action(conversation.id) == pending
    assert (
        repo.resolve_pending_confirmation(
            conversation.id,
            pending,
            Message(role="tool", content="unclaimed", tool_call_id="write-1"),
            {},
        )
        is None
    )
    assert repo.get_pending_action(conversation.id) == pending


def test_pending_confirmation_claim_has_one_winner_across_repository_instances(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    first = ChatRepository(session_factory)
    second = ChatRepository(session_factory)
    conversation = first.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(first, conversation.id, pending)
    barrier = Barrier(2)

    def claim(repo: ChatRepository, claim_id: str) -> bool:
        barrier.wait()
        return repo.claim_pending_confirmation(conversation.id, pending, claim_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = (
            pool.submit(claim, first, "claim-one"),
            pool.submit(claim, second, "claim-two"),
        )

    assert sum(result.result() for result in results) == 1
    assert first.get_pending_action(conversation.id) == pending


def test_stale_pending_confirmation_claim_can_be_recovered_after_process_loss(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    repo = ChatRepository(session_factory)
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(repo, conversation.id, pending)
    assert repo.claim_pending_confirmation(conversation.id, pending, "abandoned") is True

    with session_factory() as session:
        session.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(
                pending_confirmation_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=16)
            )
        )
        session.commit()

    assert repo.claim_pending_confirmation(conversation.id, pending, "recovery") is True
    assert (
        repo.resolve_pending_confirmation(
            conversation.id,
            pending,
            Message(role="tool", content="abandoned", tool_call_id="write-1"),
            {},
            claim_id="abandoned",
        )
        is None
    )
    assert (
        repo.resolve_pending_confirmation(
            conversation.id,
            pending,
            Message(role="tool", content="recovered", tool_call_id="write-1"),
            {},
            claim_id="recovery",
        )
        is not None
    )


def test_empty_pending_confirmation_claim_id_is_rejected_without_clearing_pending(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(repo, conversation.id, pending)

    with pytest.raises(ValueError, match="non-empty"):
        repo.resolve_pending_confirmation(
            conversation.id,
            pending,
            Message(role="tool", content="invalid", tool_call_id="write-1"),
            {},
            claim_id="",
        )

    assert repo.get_pending_action(conversation.id) == pending
    assert repo.list_messages(conversation.id) == []


def test_resolve_pending_confirmation_cas_does_not_clear_newer_pending(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    expected = PendingAction("write-1", "display_pending_notice", '{"id":1}', "first")
    newer = PendingAction("write-2", "display_pending_notice", '{"id":2}', "second")
    _set_pending(repo, conversation.id, newer)

    resolved = repo.resolve_pending_confirmation(
        conversation.id,
        expected,
        Message(role="tool", content='{"id":1}', tool_call_id="write-1"),
        {},
    )

    assert resolved is None
    assert repo.get_pending_action(conversation.id) == newer
    assert repo.list_messages(conversation.id) == []


def test_replace_pending_confirmation_rejects_missing_parent_ownership(tmp_path):
    sessions = init_database(tmp_path / "data.db")
    repo = ChatRepository(
        sessions,
        WriteOperationRepository(sessions, load_or_create_ledger_key(tmp_path, sessions)),
    )
    conversation = repo.create_conversation("confirm")
    expected = PendingAction(
        "write-1",
        "save_application_jd_version",
        '{"jd_text":"old"}',
        "old",
        str(uuid4()),
    )
    replacement = PendingAction(
        "write-2",
        "save_application_jd_version",
        '{"jd_text":"old","retry":true}',
        "retry",
        str(uuid4()),
    )
    with legacy_pending_route(expected, conversation.id, source="jd_clarification") as route_handle:
        repo.set_pending_action(conversation.id, expected, route_handle=route_handle)
    repo.set_last_write_undo(conversation.id, {"kind": "old"})

    with pytest.raises(TypeError, match="parent ownership"):
        _replace_legacy_pending(
            repo,
            conversation.id,
            expected,
            replacement,
            Message(
                role="tool",
                content="错误：application_jd_stale_current_version",
                tool_call_id="write-1",
            ),
            {},
            terminal_assistant_content="请重新确认岗位资料。",
        )

    assert repo.get_pending_action(conversation.id) == expected
    assert repo.get_last_write_undo(conversation.id) == {"kind": "old"}
    assert repo.list_messages(conversation.id) == []


def test_replace_pending_confirmation_missing_parent_does_not_overwrite_newer_card(tmp_path):
    sessions = init_database(tmp_path / "data.db")
    repo = ChatRepository(
        sessions,
        WriteOperationRepository(sessions, load_or_create_ledger_key(tmp_path, sessions)),
    )
    conversation = repo.create_conversation("confirm")
    expected = PendingAction(
        "write-1",
        "save_application_jd_version",
        '{"jd_text":"old"}',
        "old",
        str(uuid4()),
    )
    newer = PendingAction("write-3", "other", '{"value":1}', "newer")
    replacement = PendingAction(
        "write-2",
        "save_application_jd_version",
        '{"jd_text":"retry"}',
        "retry",
        str(uuid4()),
    )
    _set_pending(repo, conversation.id, newer)

    with pytest.raises(TypeError, match="parent ownership"):
        _replace_legacy_pending(
            repo,
            conversation.id,
            expected,
            replacement,
            Message(role="tool", content="error", tool_call_id="write-1"),
            {},
        )

    assert repo.get_pending_action(conversation.id) == newer
    assert repo.list_messages(conversation.id) == []


def test_resolve_pending_confirmation_preserves_existing_undo(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    previous = {"kind": "create_application", "application_id": 9}
    _set_pending(repo, conversation.id, pending)
    repo.set_last_write_undo(conversation.id, previous)

    resolved = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content="rejected", tool_call_id="write-1"),
        None,
    )

    assert resolved is not None
    assert repo.get_last_write_undo(conversation.id) == previous


def test_resolve_pending_confirmation_clears_existing_undo(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "update")
    _set_pending(repo, conversation.id, pending)
    repo.set_last_write_undo(conversation.id, {"kind": "old"})

    resolved = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content="ambiguous failure", tool_call_id="write-1"),
        {},
    )

    assert resolved is not None
    assert repo.get_last_write_undo(conversation.id) is None


def test_clear_last_write_undo_if_matches_preserves_newer_undo(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    old = {"kind": "update_application_status", "application_id": 1}
    newer = {"kind": "create_application", "application_id": 2}
    repo.set_last_write_undo(conversation.id, newer)

    stale_clear = repo.clear_last_write_undo_if_matches(conversation.id, old)
    matching_clear = repo.clear_last_write_undo_if_matches(conversation.id, newer)

    assert stale_clear is False
    assert matching_clear is True
    assert repo.get_last_write_undo(conversation.id) is None


def test_confirmation_continuation_survives_generated_title_update(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "first")
    _set_pending(repo, conversation.id, pending)
    generation = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content='{"ok":true}', tool_call_id="write-1"),
        {"kind": "undo"},
    )
    repo.apply_generated_title(conversation.id, "Generated title")

    persisted = _persist_continuation(
        repo,
        conversation.id,
        generation,
        [
            {
                "role": "assistant",
                "content": "next",
                "tool_calls": "",
                "tool_call_id": "",
                "provider_blocks": "",
            }
        ],
    )

    assert persisted is not None
    assert repo.get_conversation(conversation.id).title == "Generated title"
    assert [message.content for message in repo.list_messages(conversation.id)] == [
        '{"ok":true}',
        "next",
    ]


def test_confirmation_continuation_rejects_stale_conversation_generation(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "first")
    _set_pending(repo, conversation.id, pending)
    generation = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content='{"ok":true}', tool_call_id="write-1"),
        {"kind": "undo"},
    )
    repo.append_message(conversation.id, "user", content="newer activity")
    persisted = _persist_continuation(
        repo,
        conversation.id,
        generation,
        [
            {
                "role": "assistant",
                "content": "stale continuation",
                "tool_calls": '[{"id":"write-2"}]',
                "tool_call_id": "",
                "provider_blocks": "",
            }
        ],
    )

    assert persisted is None
    assert repo.get_pending_action(conversation.id) is None
    assert [message.content for message in repo.list_messages(conversation.id)] == [
        '{"ok":true}',
        "newer activity",
    ]


def test_confirmation_continuation_rejects_chained_pending_without_parent_ownership(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "first")
    chained = PendingAction("write-2", "display_pending_notice", '{"id":2}', "second")
    _set_pending(repo, conversation.id, pending)
    generation = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content='{"ok":true}', tool_call_id="write-1"),
        {"kind": "undo"},
    )
    messages = [
        {
            "role": "assistant",
            "content": "next",
            "tool_calls": '[{"id":"write-2"}]',
            "tool_call_id": "",
            "provider_blocks": "",
        }
    ]

    with pytest.raises(TypeError, match="parent ownership"):
        _persist_continuation(
            repo,
            conversation.id,
            generation,
            messages,
            pending=chained,
        )

    assert repo.get_pending_action(conversation.id) is None
    assert [message.content for message in repo.list_messages(conversation.id)] == [
        '{"ok":true}',
    ]


def test_confirmation_continuation_cannot_create_pending_after_archive(tmp_path):
    repo = ChatRepository(init_database(tmp_path / "data.db"))
    conversation = repo.create_conversation("confirm")
    pending = PendingAction("write-1", "display_pending_notice", '{"id":1}', "first")
    chained = PendingAction("write-2", "display_pending_notice", '{"id":2}', "second")
    _set_pending(repo, conversation.id, pending)
    generation = repo.resolve_pending_confirmation(
        conversation.id,
        pending,
        Message(role="tool", content='{"ok":true}', tool_call_id="write-1"),
        {"kind": "undo"},
    )
    archived = repo.update_conversation_for_archive(
        conversation.id, {"archived_at": datetime.now(timezone.utc)}
    )

    with pytest.raises(TypeError, match="parent ownership"):
        _persist_continuation(
            repo,
            conversation.id,
            generation,
            [{"role": "assistant", "content": "next", "tool_calls": "", "tool_call_id": ""}],
            pending=chained,
        )

    assert archived.status == "updated"
    assert repo.get_pending_action(conversation.id) is None
