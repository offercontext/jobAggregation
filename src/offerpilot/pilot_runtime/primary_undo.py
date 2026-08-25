"""Transaction-bound helpers for constructing required primary-write Undo payloads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, NoReturn, cast

from sqlalchemy.orm import Session, SessionTransaction

from offerpilot.ai.tool_runtime.contracts import (
    ToolExecutionRecord,
    TransientToolRuntimeValue,
)
from offerpilot.ai.tool_runtime.metadata import (
    FrozenJSONObject,
    FrozenJSONValue,
    UndoBuilderBinding,
    canonical_json_bytes,
    freeze_json,
)
from offerpilot.ai.tool_runtime.policy_types import UndoPolicy


class _PrimaryUndoCheckpoint(TransientToolRuntimeValue):
    """One-shot in-memory handoff between the seed and payload callbacks."""

    __slots__ = (
        "__binding",
        "__consumed",
        "__integrity_seal",
        "__seed",
        "__session",
        "__transaction",
    )

    def __init__(
        self,
        binding: UndoBuilderBinding,
        session: Session,
        transaction: SessionTransaction,
        seed: object,
    ) -> None:
        object.__setattr__(self, "_PrimaryUndoCheckpoint__binding", binding)
        object.__setattr__(self, "_PrimaryUndoCheckpoint__session", session)
        object.__setattr__(self, "_PrimaryUndoCheckpoint__transaction", transaction)
        object.__setattr__(self, "_PrimaryUndoCheckpoint__seed", seed)
        object.__setattr__(self, "_PrimaryUndoCheckpoint__consumed", False)
        object.__setattr__(
            self,
            "_PrimaryUndoCheckpoint__integrity_seal",
            _checkpoint_integrity_snapshot(binding, session, transaction, seed),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("primary Undo checkpoints are immutable")


def _require_binding(binding: object) -> UndoBuilderBinding:
    if type(binding) is not UndoBuilderBinding:
        raise TypeError("primary Undo requires an exact builder binding")
    binding._ensure_integrity()
    descriptor = binding.descriptor
    if descriptor.undo_policy is not UndoPolicy.REQUIRED:
        raise ValueError("primary Undo requires required-Undo metadata")
    if descriptor.undo_builder_id != binding.implementation_id:
        raise ValueError("primary Undo builder identity does not match its descriptor")
    return binding


def _require_outer_transaction(
    session: object,
    transaction: object,
) -> tuple[Session, SessionTransaction]:
    if not isinstance(session, Session) or not isinstance(transaction, SessionTransaction):
        raise TypeError("primary Undo requires a SQLAlchemy Session transaction")
    if (
        transaction.session is not session
        or transaction.parent is not None
        or transaction.nested
        or not transaction.is_active
        or not session.is_active
        or session.get_transaction() is not transaction
        or not session.in_transaction()
    ):
        raise ValueError("primary Undo requires the exact active outer transaction")
    return session, transaction


def _require_binding_record(
    binding: UndoBuilderBinding,
    record: object,
) -> ToolExecutionRecord[Any, Any]:
    if type(record) is not ToolExecutionRecord:
        raise TypeError("primary Undo requires an exact ToolExecutionRecord")
    typed_record = record
    spec = typed_record.prepared.spec
    if (
        spec.undo_builder_binding is not binding
        or spec.metadata.operation is not binding.descriptor
    ):
        raise ValueError("primary Undo checkpoint does not match the execution record")
    return typed_record


def _current_nested_transaction(session: Session) -> SessionTransaction | None:
    nested = session.get_nested_transaction()
    if nested is None:
        return None
    if (
        not isinstance(nested, SessionTransaction)
        or nested.session is not session
        or not nested.nested
        or not nested.is_active
    ):
        raise ValueError("primary Undo nested transaction is no longer active")
    return nested


def _revalidate_after_callback(
    binding: UndoBuilderBinding,
    session: Session,
    transaction: SessionTransaction,
    nested_transaction: SessionTransaction | None,
    record: ToolExecutionRecord[Any, Any] | None = None,
) -> None:
    _require_binding(binding)
    _require_outer_transaction(session, transaction)
    if _current_nested_transaction(session) is not nested_transaction:
        raise ValueError("primary Undo callback changed the nested transaction")
    if record is not None:
        _require_binding_record(binding, record)


def _snapshot_seed(seed: object) -> object:
    if seed is None:
        return None
    try:
        # Preserve the exact identity of an already-frozen seed: domain
        # builders are allowed to bind that object by identity.
        canonical_json_bytes(cast(FrozenJSONValue, seed))
    except (TypeError, ValueError):
        # Existing bindings may return an ordinary JSON tree.  Freeze it at
        # capture time so later caller mutation cannot alter the checkpoint.
        return freeze_json(cast(Any, seed))
    return seed


def _checkpoint_integrity_snapshot(
    binding: object,
    session: object,
    transaction: object,
    seed: object,
) -> tuple[object, ...]:
    seed_digest = hashlib.sha256(canonical_json_bytes(cast(FrozenJSONValue, seed))).hexdigest()
    return (
        id(binding),
        id(session),
        id(transaction),
        id(seed),
        seed_digest,
    )


def _clear_checkpoint(checkpoint: _PrimaryUndoCheckpoint) -> None:
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__consumed", True)
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__binding", None)
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__session", None)
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__transaction", None)
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__seed", None)
    object.__setattr__(checkpoint, "_PrimaryUndoCheckpoint__integrity_seal", None)


def capture_primary_undo(
    binding: UndoBuilderBinding,
    session: Session,
    transaction: SessionTransaction,
    context: object,
    args: object,
) -> object:
    """Capture one required-Undo seed inside an exact caller-owned transaction."""

    exact_binding = _require_binding(binding)
    exact_session, exact_transaction = _require_outer_transaction(session, transaction)
    nested_transaction = _current_nested_transaction(exact_session)
    try:
        seed = exact_binding.capture_seed(context, args)
    except BaseException:
        _revalidate_after_callback(
            exact_binding,
            exact_session,
            exact_transaction,
            nested_transaction,
        )
        raise
    _revalidate_after_callback(
        exact_binding,
        exact_session,
        exact_transaction,
        nested_transaction,
    )
    return _PrimaryUndoCheckpoint(
        exact_binding,
        exact_session,
        exact_transaction,
        _snapshot_seed(seed),
    )


def build_primary_undo(
    checkpoint: object,
    session: Session,
    transaction: SessionTransaction,
    record: ToolExecutionRecord[Any, Any],
) -> FrozenJSONObject:
    """Consume a checkpoint once and return a recursively frozen Undo mapping."""

    if type(checkpoint) is not _PrimaryUndoCheckpoint:
        raise TypeError("primary Undo requires an exact checkpoint")

    consumed = object.__getattribute__(
        checkpoint,
        "_PrimaryUndoCheckpoint__consumed",
    )
    if consumed is not False:
        _clear_checkpoint(checkpoint)
        raise ValueError("primary Undo checkpoint has already been consumed")
    binding = cast(
        UndoBuilderBinding,
        object.__getattribute__(checkpoint, "_PrimaryUndoCheckpoint__binding"),
    )
    captured_session = object.__getattribute__(
        checkpoint,
        "_PrimaryUndoCheckpoint__session",
    )
    captured_transaction = object.__getattribute__(
        checkpoint,
        "_PrimaryUndoCheckpoint__transaction",
    )
    seed = object.__getattribute__(checkpoint, "_PrimaryUndoCheckpoint__seed")
    integrity_seal = object.__getattribute__(
        checkpoint,
        "_PrimaryUndoCheckpoint__integrity_seal",
    )
    try:
        current_integrity = _checkpoint_integrity_snapshot(
            binding,
            captured_session,
            captured_transaction,
            seed,
        )
    except (TypeError, ValueError):
        current_integrity = None
    _clear_checkpoint(checkpoint)

    if current_integrity != integrity_seal:
        raise ValueError("primary Undo checkpoint integrity drift")

    if session is not captured_session or transaction is not captured_transaction:
        raise ValueError("primary Undo checkpoint belongs to another transaction")

    exact_binding = _require_binding(binding)
    exact_session, exact_transaction = _require_outer_transaction(session, transaction)
    exact_record = _require_binding_record(exact_binding, record)
    nested_transaction = _current_nested_transaction(exact_session)
    try:
        payload = exact_binding.build_undo(seed, exact_record)
    except BaseException:
        _revalidate_after_callback(
            exact_binding,
            exact_session,
            exact_transaction,
            nested_transaction,
            exact_record,
        )
        raise
    _revalidate_after_callback(
        exact_binding,
        exact_session,
        exact_transaction,
        nested_transaction,
        exact_record,
    )

    if not isinstance(payload, Mapping):
        raise TypeError("primary Undo builder must return a JSON mapping")
    frozen = freeze_json(payload)
    if len(canonical_json_bytes(frozen)) > exact_binding.descriptor.undo_bytes:
        raise ValueError("primary Undo payload exceeds the 65536-byte (64 KiB) budget")
    return frozen
