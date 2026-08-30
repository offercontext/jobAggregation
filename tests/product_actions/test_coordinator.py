from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from offerpilot.db import init_database
from offerpilot.models import (
    InterviewNote,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.coordinator import (
    ProductActionCoordinatorError,
)
from offerpilot.product_actions.contracts import ProductActionIntegrityError
from offerpilot.review_readiness.candidates import project_readiness_candidates

from tests.review_readiness_support import seed_review_candidate
from tests.test_review_readiness_repository import _coordinator


def test_signal_proposal_replays_and_conflicting_input_is_rejected(tmp_path) -> None:
    session_factory = init_database(tmp_path / "coordinator.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "user_note": "",
    }

    first = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    replay = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )

    assert first.created is True
    assert replay.created is False
    assert first.operation_id == replay.operation_id
    assert first.confirmation_token == replay.confirmation_token

    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**request, "user_note": "different"},
        )
    assert error.value.code == "product_action_idempotency_conflict"


def test_reject_is_terminal_without_source_recheck(tmp_path) -> None:
    session_factory = init_database(tmp_path / "reject.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "44444444-4444-4444-8444-444444444444",
            "user_note": "",
        },
    )

    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )
    replay = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert rejected.status == "rejected"
    assert replay.status == "rejected"
    assert replay.replayed is True
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        route = session.get(ProductActionProposal, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None
    assert operation.input_fingerprint is None
    assert operation.result_contract == "rejection_json_v1"
    assert operation.undo_json is None
    assert operation.delivery_status == "not_applicable"
    assert operation.delivery_outcome == "none"
    assert route is not None and route.route_payload_json is None
    assert route.terminalized_at is not None
    assert [(item.seq, item.state) for item in transitions] == [
        (1, "proposed"),
        (2, "rejected"),
    ]


def test_approve_terminal_replay_and_committed_focus_locator_create_no_ledger(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "approve-replay.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "66666666-6666-4666-8666-666666666666",
        "user_note": "下次先讲清约束。",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    replay = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(WriteOperation))
    located = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **request,
            "idempotency_key": "77777777-7777-4777-8777-777777777777",
        },
    )
    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(WriteOperation))

    assert decided.direct_commit is True
    assert replay.replayed is True
    assert replay.direct_commit is False
    assert replay.result == decided.result
    assert located.status == "already_confirmed"
    assert located.confirmation_token is None
    assert after == before == 1


def test_modify_replay_is_bound_to_exact_edited_payload(tmp_path) -> None:
    session_factory = init_database(tmp_path / "modify-replay.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "88888888-8888-4888-8888-888888888888",
            "user_note": "old",
        },
    )
    exact = {
        "confirmation_token": proposed.confirmation_token,
        "decision": "modify",
        "edited_payload": {"user_note": "new"},
    }

    assert coordinator.decide(operation_id=proposed.operation_id, request=exact).status == (
        "committed"
    )
    assert coordinator.decide(
        operation_id=proposed.operation_id,
        request=exact,
    ).replayed is True
    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                **exact,
                "edited_payload": {"user_note": "different"},
            },
        )
    assert error.value.code == "product_action_request_conflict"


def test_capability_denial_short_circuits_before_any_sql_or_projection(tmp_path) -> None:
    session_factory = init_database(tmp_path / "capability.sqlite3")
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    def forbidden_projector(_note_id, _proposal_id, _session):
        raise AssertionError("candidate projection must not run")

    event.listen(engine, "before_cursor_execute", observe)
    try:
        coordinator = _coordinator(
            session_factory,
            capability_check=lambda _capability: False,
            candidate_projector=forbidden_projector,
        )
        with pytest.raises(ProductActionCoordinatorError) as error:
            coordinator.propose_readiness_signal(
                note_id=1,
                request={
                    "proposal_id": 1,
                    "focus_id": "focus-1",
                    "expected_note_revision": 1,
                    "expected_candidate_fingerprint": "sha256:" + "0" * 64,
                    "idempotency_key": "99999999-9999-4999-8999-999999999999",
                    "user_note": "",
                },
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert error.value.code == "review_readiness_not_found"
    assert statements == []


def test_reject_never_rechecks_source_or_capability(tmp_path) -> None:
    session_factory = init_database(tmp_path / "reject-zero.sqlite3")
    seeded = seed_review_candidate(session_factory)
    calls = {"candidate": 0, "capability": 0}

    def candidate_projector(note_id, proposal_id, session):
        calls["candidate"] += 1
        return project_readiness_candidates(note_id, proposal_id, session)

    def capability_check(capability):
        calls["capability"] += 1
        return capability == "application.interview_readiness_feedback.write"

    coordinator = _coordinator(
        session_factory,
        capability_check=capability_check,
        candidate_projector=candidate_projector,
    )
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "user_note": "",
        },
    )
    calls.update(candidate=0, capability=0)

    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert rejected.status == "rejected"
    assert calls == {"candidate": 0, "capability": 0}


def test_primary_input_fingerprint_is_cross_process_canonical_golden() -> None:
    script = textwrap.dedent(
        """
        from offerpilot.ai.write_operations import LedgerKeyDomain, ledger_fingerprint

        key = LedgerKeyDomain(
            "11111111-1111-4111-8111-111111111111",
            b"1" * 32,
        )
        envelope = {
            "operation_request_fingerprint": "hmac-sha256:" + "1" * 64,
            "authorization_scope_fingerprint": "hmac-sha256:" + "2" * 64,
            "effective_payload_sha256": "sha256:" + "3" * 64,
        }
        print(ledger_fingerprint(key, "product-action-input-v1", envelope))
        """
    )
    expected = (
        "hmac-sha256:"
        "88e78c9289de272d2452a011664ffcc5a2408cb35389e75cd237df632f72fa1f"
    )

    outputs = []
    for seed in ("1", "911"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout.strip())

    assert outputs == [expected, expected]


def test_proposal_commit_unknown_rebuilds_once_with_fresh_proof(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-unknown.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    original_commit = Session.commit
    attempts = 0

    def fail_before_first_commit(session):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_before_first_commit)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "user_note": "",
        },
    )

    assert attempts == 2
    assert proposed.status == "proposed"
    assert proposed.confirmation_token is not None


def test_decision_commit_unknown_reconciles_terminal_without_reexecution(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "decision-unknown.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "user_note": "",
        },
    )
    original_commit = Session.commit
    attempts = 0

    def commit_then_lose_result(session):
        nonlocal attempts
        attempts += 1
        original_commit(session)
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )

    monkeypatch.setattr(Session, "commit", commit_then_lose_result)
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )

    assert attempts == 1
    assert decided.status == "committed"
    assert decided.replayed is True
    assert decided.direct_commit is False
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1


def test_twenty_same_key_proposals_converge_on_one_operation(tmp_path) -> None:
    session_factory = init_database(tmp_path / "twenty.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "user_note": "",
    }

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = tuple(
            pool.map(
                lambda _index: coordinator.propose_readiness_signal(
                    note_id=seeded["note_id"],
                    request=request,
                ),
                range(20),
            )
        )

    assert len({item.operation_id for item in results}) == 1
    assert len({item.confirmation_token for item in results}) == 1
    assert sum(item.created for item in results) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1


def test_semantic_loser_is_not_persisted_and_can_win_after_rejection(tmp_path) -> None:
    session_factory = init_database(tmp_path / "semantic-loser.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    common = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "user_note": "",
    }
    winner = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **common,
            "idempotency_key": "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    loser_request = {
        **common,
        "idempotency_key": "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }

    with pytest.raises(ProductActionCoordinatorError) as conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request=loser_request,
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1
    coordinator.decide(
        operation_id=winner.operation_id,
        request={
            "confirmation_token": winner.confirmation_token,
            "decision": "reject",
        },
    )
    accepted = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request=loser_request,
    )

    assert conflict.value.code == "review_readiness_action_in_progress"
    assert accepted.created is True
    assert accepted.operation_id != winner.operation_id


def test_source_change_blocks_approve_but_never_blocks_reject(tmp_path) -> None:
    session_factory = init_database(tmp_path / "source-change-reject.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "user_note": "",
        },
    )
    with session_factory() as session:
        session.execute(
            update(InterviewNote)
            .where(InterviewNote.id == seeded["note_id"])
            .values(content_revision=InterviewNote.content_revision + 1)
        )
        session.commit()

    with pytest.raises(ProductActionCoordinatorError) as stale:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert stale.value.code == "review_readiness_source_changed"
    assert operation is not None and operation.status == "proposed"
    assert rejected.status == "rejected"


def test_executor_base_exception_rolls_back_and_propagates(tmp_path, monkeypatch) -> None:
    session_factory = init_database(tmp_path / "base-exception.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "12121212-1212-4212-8212-121212121212",
            "user_note": "",
        },
    )

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        coordinator._readiness_repository,
        "create_signal_in_session",
        interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )

    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]
    assert coordinator._proof_registry._records == {}


def test_terminal_load_recomputes_primary_input_fingerprint(tmp_path) -> None:
    session_factory = init_database(tmp_path / "input-tamper.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "13131313-1313-4313-8313-131313131313",
            "user_note": "",
        },
    )
    coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    with session_factory.kw["bind"].begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"
        )
    with session_factory() as session:
        session.execute(
            update(WriteOperation)
            .where(WriteOperation.id == proposed.operation_id)
            .values(input_fingerprint="hmac-sha256:" + "0" * 64)
        )
        session.commit()

    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.get_state(proposed.operation_id)
    assert error.value.code == "product_action_input_fingerprint"
