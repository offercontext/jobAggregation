from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any, Literal, NoReturn, SupportsIndex, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    ApprovedWritePrepareCallIdentity,
    AuthorityFactory,
    AuthorityPhaseError,
    AuthorityUse,
    require_authority_phase,
    require_authority_spec,
)
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_authority.visibility import (
    AuthorityApplicationVisibilityError,
    AuthorityApplicationVisibilityQuery,
)
from offerpilot.ai.pending_replay import (
    PendingReplayArgsDecoderV1,
    PendingReplayIntegrityError,
)
from offerpilot.ai.tool_runtime.context import audit_bindings, pre_resolver_scope_policy
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    JSONValue,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    TRANSACTIONAL_TYPED_WRITE_NAMES,
    UndoPolicy,
)
from offerpilot.ai.tool_runtime.pipeline import execute_prepared
from offerpilot.ai.tool_runtime.rendering import render_compatibility
from offerpilot.ai.tool_runtime.journal import project_tool_started_bound
from offerpilot.ai.tool_runtime.transport import project_transport_event
from offerpilot.ai.tool_runtime.validation import (
    ArgumentValidationError,
    canonical_json,
    parse_arguments,
)
from offerpilot.models import (
    ChatMessage,
    Conversation,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.context_projector.loader import WORK_DEADLINE_SECONDS, database_coordinator


TYPED_WRITE_OPERATION_NAMES = tuple(sorted(TRANSACTIONAL_TYPED_WRITE_NAMES))
LEGACY_WRITE_OPERATION_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
COMPENSATION_OPERATION_NAMES = (
    "undo:update_application_status",
    "undo:create_application",
    "undo:create_application_event",
    "undo:add_note",
)
REQUIRED_UNDO_OPERATION_NAMES = frozenset(
    {
        "create_application",
        "update_application_status",
        "create_application_event",
        "add_note",
    }
)
WRITE_OPERATION_NAMES = frozenset(
    (*TYPED_WRITE_OPERATION_NAMES, *LEGACY_WRITE_OPERATION_NAMES, *COMPENSATION_OPERATION_NAMES)
)

LEDGER_KEY_FILENAME = "write-operation-ledger.key"
DELIVERY_OWNER_LEASE_SECONDS = 120
DELIVERY_OWNER_HEARTBEAT_SECONDS = 30
COMPENSATION_OPERATION_NAMESPACE = UUID("4079900d-84a6-5cff-aa63-65089c4ccccd")
_TERMINAL_STATUSES = frozenset({"committed", "failed", "rejected"})


class WriteOperationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class LedgerKeyDomain:
    key_id: str
    secret: bytes


class _Transient:
    __slots__ = ()

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("transient write operation value cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("transient write operation value cannot be serialized")


class DeliveryOwnership(_Transient):
    __slots__ = ("operation_id", "generation", "__raw_token", "fingerprint")

    def __init__(
        self, operation_id: str, generation: int, raw_token: bytes, fingerprint: str
    ) -> None:
        self.operation_id = operation_id
        self.generation = generation
        self.__raw_token = raw_token
        self.fingerprint = fingerprint

    @property
    def raw_token(self) -> bytes:
        return self.__raw_token

    def __repr__(self) -> str:
        return (
            "DeliveryOwnership(operation_id="
            f"{self.operation_id!r}, generation={self.generation!r}, "
            f"fingerprint={self.fingerprint!r})"
        )

    def public_identity(self) -> dict[str, str | int]:
        """Return the only representation safe for logs, events, and transport."""

        return {
            "operation_id": self.operation_id,
            "generation": self.generation,
            "fingerprint": self.fingerprint,
        }


class DeliveryHeartbeat(_Transient):
    def __init__(
        self, repository: "WriteOperationRepository", ownership: DeliveryOwnership
    ) -> None:
        self._repository = repository
        self._ownership = ownership
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> "DeliveryHeartbeat":
        self._thread.start()
        return self

    def fence(self) -> bool:
        return not self._stop.is_set() and self._repository.heartbeat(self._ownership)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            while not self._stop.wait(DELIVERY_OWNER_HEARTBEAT_SECONDS):
                if not self._repository.heartbeat(self._ownership):
                    self._stop.set()
                    return
        except Exception:
            self._stop.set()


@dataclass(frozen=True, slots=True, repr=False)
class LedgerPendingPointer:
    """Bounded Conversation projection used before a confirmation decision.

    Deliberately absent are Pending arguments, human text, generation fields,
    and every lazy ORM relationship.  This is an identity/CAS carrier only.
    """

    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    pending_confirmation_claim_id: str


@dataclass(frozen=True, slots=True, repr=False)
class LedgerOperationPreheader:
    """One Ledger row plus its exact, bounded owning-Conversation pointer."""

    operation: WriteOperation
    pending_pointer: LedgerPendingPointer | None


@dataclass(frozen=True)
class TerminalPayload:
    status: Literal["committed", "failed", "rejected"]
    result_contract: str
    result_json: str
    visible_result: str
    transport_json: str
    undo_json: str | None
    failure_category: str | None
    failure_code: str | None
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedPendingReplay(_Transient):
    """One operation-owned Pending snapshot verified after delivery topology."""

    adapter_kind: Literal["typed", "legacy_deterministic"]
    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    raw_args: str = field(repr=False)
    human: str
    confirmation_token_fingerprint: str = field(repr=False)
    decoded_args: dict[str, JSONValue] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationCommitted(_Transient):
    operation_id: str
    payload: TerminalPayload
    ownership: DeliveryOwnership | None
    replayed: bool = False


@dataclass(frozen=True)
class OperationFailed(_Transient):
    operation_id: str
    payload: TerminalPayload
    ownership: DeliveryOwnership | None
    replayed: bool = False


@dataclass(frozen=True)
class OperationReplay(_Transient):
    operation_id: str
    payload: TerminalPayload
    delivery_status: str
    delivery_generation: int
    delivery_lease_expires_at: int | None
    delivery_outcome: str | None = None
    final_message: str | None = None
    replayed: bool = True
    chained_pending: VerifiedPendingReplay | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OperationUnknown(_Transient):
    operation_id: str
    code: str
    retryable: bool


OperationExecution = OperationCommitted | OperationFailed | OperationReplay | OperationUnknown


def load_or_create_ledger_key(
    data_dir: Path,
    session_factory: sessionmaker[Session],
) -> LedgerKeyDomain:
    key_path = data_dir.resolve() / LEDGER_KEY_FILENAME
    if key_path.exists():
        return _read_ledger_key(key_path)
    with session_factory() as session:
        existing = session.scalar(select(func.count()).select_from(WriteOperation)) or 0
    if existing:
        raise WriteOperationError("operation_unavailable")
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = key_path.with_name(f".{LEDGER_KEY_FILENAME}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        if key_path.exists():
            return _read_ledger_key(key_path)
        raise WriteOperationError("operation_unavailable") from exc
    temp_path = key_path.with_name(f".{LEDGER_KEY_FILENAME}.{uuid4().hex}.tmp")
    try:
        os.close(lock_fd)
        if key_path.exists():
            return _read_ledger_key(key_path)
        with session_factory() as session:
            existing = session.scalar(select(func.count()).select_from(WriteOperation)) or 0
        if existing:
            raise WriteOperationError("operation_unavailable")
        domain = LedgerKeyDomain(str(uuid4()), secrets.token_bytes(32))
        encoded = base64.urlsafe_b64encode(domain.secret).decode("ascii").rstrip("=")
        payload = canonical_json({"schema_version": 1, "key_id": domain.key_id, "secret": encoded})
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(temp_path, flags, 0o600)
        try:
            os.write(fd, (payload + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, key_path)
        if os.name != "nt":
            os.chmod(key_path, 0o600)
        return domain
    except WriteOperationError:
        raise
    except Exception as exc:
        raise WriteOperationError("operation_unavailable") from exc
    finally:
        try:
            temp_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_ledger_key(path: Path) -> LedgerKeyDomain:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "key_id", "secret"}:
            raise ValueError
        key_id = str(UUID(cast(str, payload["key_id"])))
        if payload["schema_version"] != 1 or payload["key_id"] != key_id:
            raise ValueError
        raw = cast(str, payload["secret"])
        secret = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        if len(secret) != 32:
            raise ValueError
        if os.name != "nt":
            os.chmod(path, 0o600)
        return LedgerKeyDomain(key_id, secret)
    except Exception as exc:
        raise WriteOperationError("operation_unavailable") from exc


def ledger_fingerprint(key: LedgerKeyDomain, domain: str, value: JSONValue | bytes) -> str:
    encoded = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    digest = hmac.new(
        key.secret,
        domain.encode("ascii") + b"\0" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return "hmac-sha256:" + digest


def operation_request_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_id: str,
    tool_call_id: str,
    approved: bool,
    edited_args_present: bool,
    edited_args: Mapping[str, JSONValue] | None,
    rejection_feedback_present: bool,
    rejection_feedback: str,
    confirmation_token_fingerprint: str,
    proposal_fingerprint: str,
) -> str:
    value: dict[str, JSONValue] = {
        "request_kind": "confirmation_v1",
        "operation_id": operation_id,
        "tool_call_id": tool_call_id,
        "decision": "approved" if approved else "rejected",
        "edited_args_present": edited_args_present,
        "edited_args": dict(edited_args or {}) if edited_args_present else None,
        "rejection_feedback_present": rejection_feedback_present,
        "rejection_feedback": rejection_feedback if rejection_feedback_present else None,
        "confirmation_token_fingerprint": confirmation_token_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
    }
    return ledger_fingerprint(key, "write-operation-request-v1", value)


def compensation_operation_id(parent_operation_id: str, compensation_kind: str) -> str:
    parent = str(UUID(parent_operation_id))
    if compensation_kind not in COMPENSATION_OPERATION_NAMES:
        raise ValueError("unsupported compensation kind")
    return str(uuid5(COMPENSATION_OPERATION_NAMESPACE, parent + ":" + compensation_kind))


def compensation_kind_for_undo(undo_kind: str) -> str:
    mapping = {
        "update_application_status": "undo:update_application_status",
        "delete_application": "undo:create_application",
        "delete_application_event": "undo:create_application_event",
        "delete_note": "undo:add_note",
    }
    try:
        return mapping[undo_kind]
    except KeyError as exc:
        raise ValueError("unsupported compensation kind") from exc


def _json_value(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite result")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__table__"):
        return {
            str(column.name): _json_value(getattr(value, column.name))
            for column in value.__table__.columns
        }
    raise ValueError("unsupported result value")


def build_terminal_payload(
    *,
    status: Literal["committed", "failed", "rejected"],
    result_contract: str,
    result: Any,
    visible_result: str,
    transport: Mapping[str, Any] | None,
    undo: Mapping[str, Any] | None,
    failure_category: str | None,
    failure_code: str | None,
    budgets: tuple[int, int, int, int] = (512 * 1024, 256 * 1024, 128 * 1024, 64 * 1024),
) -> TerminalPayload:
    result_json = canonical_json(_json_value(result))
    transport_json = canonical_json(_json_value(dict(transport or {})))
    undo_json = canonical_json(_json_value(dict(undo))) if undo is not None else None
    encoded = (
        result_json.encode("utf-8"),
        visible_result.encode("utf-8"),
        transport_json.encode("utf-8"),
        undo_json.encode("utf-8") if undo_json is not None else b"",
    )
    if any(len(item) > limit for item, limit in zip(encoded, budgets)):
        raise WriteOperationError("operation_result_too_large")
    envelope: dict[str, JSONValue] = {
        "status": status,
        "result_contract": result_contract,
        "result_json": json.loads(result_json),
        "visible_result": visible_result,
        "transport_json": json.loads(transport_json),
        "undo_json": json.loads(undo_json) if undo_json is not None else None,
        "failure_category": failure_category,
        "failure_code": failure_code,
    }
    canonical = canonical_json(envelope).encode("utf-8")
    if len(canonical) > 1024 * 1024:
        raise WriteOperationError("operation_result_too_large")
    return TerminalPayload(
        status=status,
        result_contract=result_contract,
        result_json=result_json,
        visible_result=visible_result,
        transport_json=transport_json,
        undo_json=undo_json,
        failure_category=failure_category,
        failure_code=failure_code,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def payload_from_operation(operation: WriteOperation) -> TerminalPayload:
    if operation.status not in _TERMINAL_STATUSES:
        raise WriteOperationError("operation_not_committed", retryable=True)
    payload = build_terminal_payload(
        status=cast(Any, operation.status),
        result_contract=operation.result_contract or "",
        result=json.loads(operation.result_json or "null"),
        visible_result=operation.visible_result or "",
        transport=json.loads(operation.transport_json or "{}"),
        undo=json.loads(operation.undo_json) if operation.undo_json is not None else None,
        failure_category=operation.failure_category,
        failure_code=operation.failure_code,
    )
    if not hmac.compare_digest(payload.digest, operation.terminal_payload_sha256 or ""):
        raise WriteOperationError("operation_integrity_error")
    return payload


def _undo_digest(undo_json: str | None) -> str | None:
    if undo_json is None:
        return None
    canonical = canonical_json(json.loads(undo_json)).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _chained_manifest(operation: WriteOperation | None) -> JSONValue:
    if operation is None:
        return None
    return {
        "operation_id": operation.id,
        "tool_call_id": operation.tool_call_id,
        "tool_name": operation.tool_name,
        "proposal_fingerprint": operation.proposal_fingerprint,
    }


def _pending_confirmation_token(
    tool_call_id: str,
    tool_name: str,
    canonical_arguments: Mapping[str, JSONValue],
) -> str:
    identity = json.dumps(
        [tool_call_id, tool_name, canonical_json(dict(canonical_arguments))],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _legacy_pending_confirmation_token(
    tool_call_id: str,
    tool_name: str,
    raw_arguments: str,
) -> tuple[str, JSONValue]:
    """Run the exact closed Legacy token codec and retain its decoded value."""

    try:
        decoded = cast(JSONValue, json.loads(raw_arguments))
        canonical_arguments = json.dumps(
            decoded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        decoded = raw_arguments
        canonical_arguments = raw_arguments
    identity = json.dumps(
        [tool_call_id, tool_name, canonical_arguments],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest(), decoded


def _typed_pending_confirmation_human(
    tool_name: str,
    arguments: Mapping[str, JSONValue],
) -> str:
    """Render a replay card without Catalog, schema, Authority, or Tool access."""

    values = dict(arguments)
    if tool_name == "create_application":
        return (
            f"新建投递：{values.get('company_name', '')} - "
            f"{values.get('position_name', '')}"
        )
    if tool_name == "update_application_status":
        return (
            f"将投递 #{values.get('id', '')} 的状态改为 "
            f"{values.get('status', '')}"
        )
    if tool_name == "create_application_event":
        labels = {
            "written_test": "笔试",
            "interview": "面试",
            "offer_step": "Offer 进展",
            "deadline": "截止",
            "custom": "自定义",
        }
        title = labels.get(str(values.get("event_type") or ""), "日程")
        raw_time = str(values.get("scheduled_at") or "")
        try:
            parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            shown_time = parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            shown_time = raw_time
        duration = values.get("duration_minutes")
        shown_duration = f"{duration} 分钟" if duration not in (None, "") else ""
        details = " · ".join(
            value for value in (title, shown_time, shown_duration) if value
        )
        return f"新建日程：{details}" if details else "新建日程"
    if tool_name == "add_note":
        details = " · ".join(
            str(values.get(key) or "").strip()
            for key in ("company", "position", "round")
            if str(values.get(key) or "").strip()
        )
        return f"新增复盘：{details}" if details else "新增复盘"
    actions = {
        "update_application_event": "更新日程",
        "delete_application_event": "删除日程",
        "update_note": "更新复盘",
        "delete_note": "删除复盘",
        "update_offer": "更新 Offer",
        "save_offer_assessment": "保存 Offer 评估",
        "resume_update_career_intent": "更新简历求职意向",
        "resume_rewrite_highlight": "改写简历亮点",
    }
    action = actions.get(tool_name)
    if action is None:
        raise WriteOperationError("operation_delivery_unknown", retryable=True)
    return f"{action} #{values.get('id', '')}"


def _verified_pending_confirmation_human(
    adapter_kind: Literal["typed", "legacy_deterministic"],
    tool_name: str,
    decoded_args: Mapping[str, JSONValue] | None,
) -> str:
    if adapter_kind == "typed":
        if decoded_args is None:
            raise WriteOperationError("operation_integrity_error")
        return _typed_pending_confirmation_human(tool_name, decoded_args)
    if tool_name != "save_application_jd_version":
        raise WriteOperationError("operation_delivery_unknown", retryable=True)
    return "请确认将这份岗位资料保存到当前投递。"


def _valid_delivery_messages(
    operation: WriteOperation,
    messages: Sequence[ChatMessage],
) -> bool:
    if (
        len(messages) < 2
        or [item.delivery_ordinal for item in messages] != list(range(len(messages)))
        or messages[0].role != "tool"
        or messages[0].tool_call_id != operation.tool_call_id
        or messages[0].delivery_kind != "origin_tool_result"
        or any(
            item.operation_id != operation.id or item.conversation_id != operation.conversation_id
            for item in messages
        )
    ):
        return False
    return all(
        item.delivery_kind == "continuation_message"
        and (
            (item.role == "assistant" and item.tool_call_id == "")
            or (item.role == "tool" and item.tool_call_id != "")
        )
        for item in messages[1:]
    )


class WriteOperationRepository:
    def __init__(self, session_factory: sessionmaker[Session], key: LedgerKeyDomain):
        self.session_factory = session_factory
        self.key = key
        bind = session_factory.kw.get("bind")
        database = getattr(getattr(bind, "url", None), "database", None)
        self._database_gate = database_coordinator(str(database)) if database else None

    def get(self, operation_id: str) -> WriteOperation | None:
        with self.session_factory() as session:
            return session.get(WriteOperation, operation_id)

    def operation_preheader(
        self,
        *,
        conversation_id: int,
        operation_id: str | None,
    ) -> LedgerOperationPreheader:
        """Load the confirmation identity without selecting Pending content.

        An explicit operation id is always the first database read.  When the
        id is omitted, the only bootstrap read is the five-column Conversation
        pointer projection; the Ledger is never scanned or guessed.
        """

        with self.session_factory() as session:
            operation: WriteOperation | None = None
            if operation_id is not None:
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if operation.conversation_id != conversation_id:
                    raise WriteOperationError("operation_identity_conflict")
                if operation.status in _TERMINAL_STATUSES:
                    return LedgerOperationPreheader(operation, None)

            pointer_row = session.execute(
                select(
                    Conversation.id,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                    Conversation.pending_confirmation_claim_id,
                ).where(Conversation.id == conversation_id)
            ).one_or_none()
            if pointer_row is None:
                raise WriteOperationError("operation_unavailable")
            pointer = LedgerPendingPointer(
                conversation_id=int(pointer_row.id),
                operation_id=str(pointer_row.pending_operation_id or ""),
                tool_call_id=str(pointer_row.pending_tool_call_id or ""),
                tool_name=str(pointer_row.pending_tool_name or ""),
                pending_confirmation_claim_id=str(
                    pointer_row.pending_confirmation_claim_id or ""
                ),
            )
            if operation is None:
                if not pointer.operation_id:
                    raise WriteOperationError("stale_pending_action")
                operation = session.get(WriteOperation, pointer.operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if operation.conversation_id != conversation_id:
                    raise WriteOperationError("operation_identity_conflict")
            return LedgerOperationPreheader(operation, pointer)

    def create_primary(
        self,
        session: Session,
        *,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        adapter_kind: Literal["typed", "legacy_deterministic"],
        proposal_fingerprint: str,
        confirmation_token_fingerprint: str,
        authorization_scope_fingerprint: str | None = None,
        agent_run_id: str | None = None,
    ) -> WriteOperation:
        if tool_name not in (*TYPED_WRITE_OPERATION_NAMES, *LEGACY_WRITE_OPERATION_NAMES):
            raise WriteOperationError("operation_not_transactional")
        operation = WriteOperation(
            id=str(UUID(operation_id)),
            operation_role="primary",
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            adapter_kind=adapter_kind,
            status="proposed",
            fingerprint_key_id=self.key.key_id,
            proposal_fingerprint=proposal_fingerprint,
            confirmation_token_fingerprint=confirmation_token_fingerprint,
            authorization_scope_fingerprint=authorization_scope_fingerprint,
            delivery_status="pending",
            delivery_generation=0,
        )
        session.add(operation)
        self.append_transition(session, operation.id, 1, "proposed")
        session.flush()
        return operation

    @staticmethod
    def append_transition(
        session: Session,
        operation_id: str,
        seq: int,
        state: str,
    ) -> None:
        session.add(
            WriteOperationTransition(
                id=str(uuid4()), operation_id=operation_id, seq=seq, state=state
            )
        )

    def replay(
        self,
        operation: WriteOperation,
        request_fingerprint: str,
    ) -> OperationReplay:
        if operation.fingerprint_key_id != self.key.key_id:
            raise WriteOperationError("operation_unavailable")
        if not operation.operation_request_fingerprint or not hmac.compare_digest(
            operation.operation_request_fingerprint, request_fingerprint
        ):
            raise WriteOperationError("operation_input_conflict")
        payload = payload_from_operation(operation)
        final_message = None
        chained_pending = None
        if operation.delivery_status in {"completed", "failed"}:
            try:
                with self.session_factory() as session:
                    current = session.get(WriteOperation, operation.id)
                    if current is None:
                        raise WriteOperationError(
                            "operation_delivery_unknown", retryable=True
                        )
                    final_message, chained_pending = self._verify_delivery(session, current)
            except WriteOperationError:
                raise
            except Exception as exc:
                raise WriteOperationError(
                    "operation_delivery_unknown", retryable=True
                ) from exc
        return OperationReplay(
            operation.id,
            payload,
            operation.delivery_status,
            operation.delivery_generation,
            operation.delivery_lease_expires_at,
            operation.delivery_outcome,
            final_message,
            chained_pending=chained_pending,
        )

    def _verify_delivery(
        self, session: Session, operation: WriteOperation
    ) -> tuple[str, VerifiedPendingReplay | None]:
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.operation_id == operation.id)
                .order_by(ChatMessage.delivery_ordinal.asc())
            )
        )
        if len(messages) != operation.delivery_message_count or not _valid_delivery_messages(
            operation, messages
        ):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        manifest_messages: list[JSONValue] = [
            {
                "role": item.role,
                "content": item.content,
                "tool_calls": item.tool_calls,
                "tool_call_id": item.tool_call_id,
                "provider_blocks": item.provider_blocks,
                "delivery_kind": item.delivery_kind,
                "delivery_ordinal": item.delivery_ordinal,
            }
            for item in messages
        ]
        child = (
            session.get(WriteOperation, operation.delivery_next_operation_id)
            if operation.delivery_next_operation_id
            else None
        )
        adapter_kind: Literal["typed", "legacy_deterministic"] | None = None
        if operation.delivery_outcome == "chained_pending":
            if (
                child is None
                or operation.operation_role != "primary"
                or child.operation_role != "primary"
                or child.status != "proposed"
                or child.conversation_id != operation.conversation_id
            ):
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            if operation.adapter_kind == "typed" and child.adapter_kind == "typed":
                adapter_kind = "typed"
            elif (
                operation.adapter_kind == "legacy_deterministic"
                and child.adapter_kind == "legacy_deterministic"
                and operation.tool_name == "save_application_jd_version"
                and child.tool_name == "save_application_jd_version"
            ):
                adapter_kind = "legacy_deterministic"
            else:
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
        elif operation.delivery_next_operation_id is not None:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        manifest: dict[str, JSONValue] = {
            "operation_id": operation.id,
            "status": operation.status,
            "terminal_payload_sha256": operation.terminal_payload_sha256,
            "delivery_generation": operation.delivery_generation,
            "messages": manifest_messages,
            "outcome": operation.delivery_outcome,
            "failure_code": operation.delivery_failure_code,
            "next_operation_id": operation.delivery_next_operation_id,
            "old_pending_disposition": (
                "replaced" if operation.delivery_outcome == "chained_pending" else "cleared"
            ),
            "validated_undo_digest": _undo_digest(operation.undo_json),
            "chained_operation": _chained_manifest(child),
        }
        digest = "sha256:" + hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, operation.delivery_manifest_sha256 or ""):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        final = next(
            (item.content for item in reversed(messages) if item.role == "assistant"), None
        )
        if final is None:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        if adapter_kind is None:
            return final, None

        assert child is not None
        pending_row = session.execute(
            select(
                Conversation.id,
                Conversation.pending_operation_id,
                Conversation.pending_tool_call_id,
                Conversation.pending_tool_name,
                Conversation.pending_args,
                Conversation.pending_human,
            )
            .where(Conversation.id == operation.conversation_id)
            .where(Conversation.pending_operation_id == child.id)
        ).one_or_none()
        if (
            pending_row is None
            or pending_row.pending_operation_id != child.id
            or pending_row.pending_tool_call_id != child.tool_call_id
            or pending_row.pending_tool_name != child.tool_name
        ):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)

        decoded_args: dict[str, JSONValue] | None = None
        if adapter_kind == "typed":
            try:
                decoded_args = PendingReplayArgsDecoderV1().decode(pending_row.pending_args)
            except PendingReplayIntegrityError as exc:
                raise WriteOperationError("operation_integrity_error") from exc
            expected_proposal = ledger_fingerprint(
                self.key, "write-operation-proposal-v1", decoded_args
            )
            if not hmac.compare_digest(
                expected_proposal, child.proposal_fingerprint or ""
            ):
                raise WriteOperationError("operation_integrity_error")
            token = _pending_confirmation_token(
                child.tool_call_id or "", child.tool_name, decoded_args
            )
            expected_token = ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                token.encode("ascii"),
            )
            if not hmac.compare_digest(
                expected_token, child.confirmation_token_fingerprint or ""
            ):
                raise WriteOperationError("operation_integrity_error")

        if adapter_kind == "legacy_deterministic":
            token, legacy_arguments = _legacy_pending_confirmation_token(
                child.tool_call_id or "",
                child.tool_name,
                pending_row.pending_args,
            )
            expected_proposal = ledger_fingerprint(
                self.key, "write-operation-proposal-v1", legacy_arguments
            )
            if not hmac.compare_digest(
                expected_proposal, child.proposal_fingerprint or ""
            ):
                raise WriteOperationError("operation_integrity_error")
            expected_token = ledger_fingerprint(
                self.key,
                "write-operation-confirmation-token-v1",
                token.encode("ascii"),
            )
            if not hmac.compare_digest(
                expected_token, child.confirmation_token_fingerprint or ""
            ):
                raise WriteOperationError("operation_integrity_error")

        expected_human = _verified_pending_confirmation_human(
            adapter_kind,
            child.tool_name,
            decoded_args,
        )
        if not _constant_time_text_equal(expected_human, pending_row.pending_human):
            raise WriteOperationError("operation_integrity_error")

        return final, VerifiedPendingReplay(
            adapter_kind=adapter_kind,
            conversation_id=int(pending_row.id),
            operation_id=child.id,
            tool_call_id=child.tool_call_id or "",
            tool_name=child.tool_name,
            raw_args=pending_row.pending_args,
            human=expected_human,
            confirmation_token_fingerprint=child.confirmation_token_fingerprint or "",
            decoded_args=decoded_args,
        )

    def prepare_owner(self, operation_id: str, generation: int = 1) -> DeliveryOwnership:
        raw = secrets.token_bytes(32)
        fingerprint = ledger_fingerprint(self.key, "write-operation-delivery-owner-v1", raw)
        return DeliveryOwnership(operation_id, generation, raw, fingerprint)

    def complete_delivery(
        self,
        session: Session,
        ownership: DeliveryOwnership,
        *,
        outcome: Literal["final_response", "chained_pending", "fallback"],
        next_operation_id: str | None = None,
        failure_code: str | None = None,
    ) -> bool:
        expected = ledger_fingerprint(
            self.key, "write-operation-delivery-owner-v1", ownership.raw_token
        )
        if not hmac.compare_digest(expected, ownership.fingerprint):
            return False
        operation = session.get(WriteOperation, ownership.operation_id)
        sqlite_now = session.scalar(select(func.unixepoch("now")))
        if (
            operation is None
            or operation.delivery_status != "pending"
            or operation.delivery_generation != ownership.generation
            or not hmac.compare_digest(
                operation.delivery_owner_token_fingerprint or "", ownership.fingerprint
            )
            or operation.delivery_lease_expires_at is None
            or sqlite_now is None
            or operation.delivery_lease_expires_at <= int(sqlite_now)
        ):
            return False
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.operation_id == operation.id)
                .order_by(ChatMessage.delivery_ordinal.asc())
            )
        )
        if not _valid_delivery_messages(operation, messages):
            raise WriteOperationError("operation_delivery_unknown")
        manifest_messages: list[JSONValue] = [
            {
                "role": item.role,
                "content": item.content,
                "tool_calls": item.tool_calls,
                "tool_call_id": item.tool_call_id,
                "provider_blocks": item.provider_blocks,
                "delivery_kind": item.delivery_kind,
                "delivery_ordinal": item.delivery_ordinal,
            }
            for item in messages
        ]
        child = session.get(WriteOperation, next_operation_id) if next_operation_id else None
        if outcome == "chained_pending":
            pending_identity = session.execute(
                select(
                    Conversation.id,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                ).where(Conversation.id == operation.conversation_id)
            ).one_or_none()
            if (
                child is None
                or child.operation_role != "primary"
                or child.status != "proposed"
                or child.conversation_id != operation.conversation_id
                or pending_identity is None
                or pending_identity.pending_operation_id != child.id
                or pending_identity.pending_tool_call_id != child.tool_call_id
                or pending_identity.pending_tool_name != child.tool_name
            ):
                raise WriteOperationError("operation_delivery_unknown")
        else:
            conversation_id = session.scalar(
                select(Conversation.id)
                .where(Conversation.id == operation.conversation_id)
                .where(Conversation.pending_operation_id != operation.id)
            )
            if conversation_id is None or next_operation_id is not None:
                raise WriteOperationError("operation_delivery_unknown")
        manifest: dict[str, JSONValue] = {
            "operation_id": operation.id,
            "status": operation.status,
            "terminal_payload_sha256": operation.terminal_payload_sha256,
            "delivery_generation": ownership.generation,
            "messages": manifest_messages,
            "outcome": outcome,
            "failure_code": failure_code,
            "next_operation_id": next_operation_id,
            "old_pending_disposition": ("replaced" if outcome == "chained_pending" else "cleared"),
            "validated_undo_digest": _undo_digest(operation.undo_json),
            "chained_operation": _chained_manifest(child),
        }
        digest = "sha256:" + hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        operation.delivery_status = "failed" if outcome == "fallback" else "completed"
        operation.delivery_failure_code = failure_code if outcome == "fallback" else None
        operation.delivery_outcome = outcome
        operation.delivery_message_count = len(messages)
        operation.delivery_manifest_sha256 = digest
        operation.delivery_next_operation_id = next_operation_id
        operation.delivery_owner_token_fingerprint = None
        operation.delivery_lease_expires_at = None
        operation.delivered_at = datetime.now(timezone.utc)
        operation.updated_at = datetime.now(timezone.utc)
        session.flush()
        return True

    def heartbeat(self, ownership: DeliveryOwnership) -> bool:
        expected = ledger_fingerprint(
            self.key, "write-operation-delivery-owner-v1", ownership.raw_token
        )
        if not hmac.compare_digest(expected, ownership.fingerprint):
            return False
        try:
            gate = (
                self._database_gate.writer(monotonic() + WORK_DEADLINE_SECONDS)
                if self._database_gate is not None
                else nullcontext()
            )
            with gate:
                with self.session_factory() as session:
                    result = session.execute(
                        update(WriteOperation)
                        .where(WriteOperation.id == ownership.operation_id)
                        .where(WriteOperation.delivery_status == "pending")
                        .where(WriteOperation.delivery_generation == ownership.generation)
                        .where(
                            WriteOperation.delivery_owner_token_fingerprint
                            == ownership.fingerprint
                        )
                        .where(WriteOperation.delivery_lease_expires_at > func.unixepoch("now"))
                        .values(
                            delivery_lease_expires_at=func.unixepoch("now")
                            + DELIVERY_OWNER_LEASE_SECONDS
                        )
                    )
                    session.commit()
                    return getattr(result, "rowcount", 0) == 1
        except Exception:
            return False

    def converge_expired_delivery(self, operation_id: str) -> OperationReplay | OperationUnknown:
        try:
            with self.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                now_epoch = int(session.scalar(select(func.unixepoch("now"))) or 0)
                operation = session.get(WriteOperation, operation_id)
                if operation is None or operation.status not in _TERMINAL_STATUSES:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_result_unknown", True)
                payload = payload_from_operation(operation)
                if operation.conversation_id is None:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if operation.delivery_status != "pending":
                    session.rollback()
                    return OperationReplay(
                        operation.id,
                        payload,
                        operation.delivery_status,
                        operation.delivery_generation,
                        operation.delivery_lease_expires_at,
                    )
                if (
                    operation.delivery_lease_expires_at is not None
                    and operation.delivery_lease_expires_at > now_epoch
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_pending", True)
                messages = list(
                    session.scalars(
                        select(ChatMessage)
                        .where(ChatMessage.operation_id == operation.id)
                        .order_by(ChatMessage.delivery_ordinal.asc())
                    )
                )
                if len(messages) > 1:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if messages and (
                    messages[0].delivery_ordinal != 0
                    or messages[0].role != "tool"
                    or messages[0].tool_call_id != (operation.tool_call_id or "")
                    or messages[0].delivery_kind != "origin_tool_result"
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                if not messages:
                    session.add(
                        ChatMessage(
                            conversation_id=operation.conversation_id,
                            role="tool",
                            content=payload.visible_result,
                            tool_call_id=operation.tool_call_id or "",
                            operation_id=operation.id,
                            delivery_kind="origin_tool_result",
                            delivery_ordinal=0,
                        )
                    )
                session.add(
                    ChatMessage(
                        conversation_id=operation.conversation_id,
                        role="assistant",
                        content=(
                            "操作已提交，但后续说明生成失败。"
                            if operation.status == "committed"
                            else "操作未完成，请查看工具结果后重试。"
                        ),
                        operation_id=operation.id,
                        delivery_kind="continuation_message",
                        delivery_ordinal=1,
                    )
                )
                takeover = self.prepare_owner(operation.id, operation.delivery_generation + 1)
                operation.delivery_generation = takeover.generation
                operation.delivery_owner_token_fingerprint = takeover.fingerprint
                operation.delivery_lease_expires_at = now_epoch + DELIVERY_OWNER_LEASE_SECONDS
                conversation_update = (
                    update(Conversation)
                    .where(Conversation.id == operation.conversation_id)
                    .where(Conversation.pending_operation_id == operation.id)
                    .values(
                        pending_operation_id="",
                        pending_tool_call_id="",
                        pending_tool_name="",
                        pending_args="",
                        pending_human="",
                        pending_confirmation_claim_id="",
                        pending_confirmation_claimed_at=None,
                    )
                )
                if operation.status == "committed" and operation.undo_json:
                    conversation_update = conversation_update.values(
                        last_write_undo_json=operation.undo_json,
                        last_write_operation_id=operation.id,
                    )
                elif operation.status != "rejected":
                    conversation_update = conversation_update.values(
                        last_write_undo_json="",
                        last_write_operation_id="",
                    )
                session.execute(conversation_update)
                session.flush()
                if not self.complete_delivery(
                    session,
                    takeover,
                    outcome="fallback",
                    failure_code="operation_delivery_failed",
                ):
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_delivery_unknown", False)
                session.commit()
                return OperationReplay(
                    operation.id,
                    payload,
                    "failed",
                    takeover.generation,
                    None,
                )
        except OperationalError:
            return OperationUnknown(operation_id, "operation_busy", True)


UndoSeedBuilder = Callable[[PreparedToolCall[Any, Any], ToolExecutionContext], Mapping[str, Any]]
UndoBuilder = Callable[
    [PreparedToolCall[Any, Any], ToolExecutionRecord[Any, Any], Mapping[str, Any]],
    Mapping[str, Any] | None,
]
LegacyExecutor = Callable[[Session], str]
CompensationExecutor = Callable[[Session, Mapping[str, Any]], str]


class _ClaimedWriteFailure(Exception):
    def __init__(self, record: ToolExecutionRecord[Any, Any]) -> None:
        super().__init__("claimed write failed")
        self.record = record


@dataclass(frozen=True)
class _LockedPendingIdentity:
    raw_args: str
    arguments_digest: str
    pending_action_revision: int


def _constant_time_text_equal(left: object, right: object) -> bool:
    if type(left) is not str or type(right) is not str or len(left) != len(right):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _locked_pending_identity(
    tool_call_id: str,
    tool_name: str,
    raw_args: str,
) -> _LockedPendingIdentity:
    try:
        arguments = parse_arguments(raw_args)
        normalized_args = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        arguments_digest = "sha256:" + hashlib.sha256(
            canonical_json(cast(JSONValue, arguments)).encode("utf-8")
        ).hexdigest()
    except (ArgumentValidationError, TypeError, UnicodeError, ValueError) as exc:
        raise WriteOperationError("operation_identity_conflict") from exc
    revision_payload = json.dumps(
        {
            "args": normalized_args,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    revision = int.from_bytes(hashlib.sha256(revision_payload).digest()[:8], "big") & (
        (1 << 63) - 1
    )
    return _LockedPendingIdentity(raw_args, arguments_digest, revision)


def pending_action_identity(
    tool_call_id: str,
    tool_name: str,
    raw_args: str,
) -> tuple[str, int]:
    """Return the exact digest/revision pair used by the locked approval atom."""

    locked = _locked_pending_identity(tool_call_id, tool_name, raw_args)
    return locked.arguments_digest, locked.pending_action_revision


def compensation_request_fingerprint(
    key: LedgerKeyDomain,
    *,
    operation_id: str,
    parent_operation_id: str,
    compensation_kind: str,
    conversation_id: int,
) -> str:
    return ledger_fingerprint(
        key,
        "write-operation-request-v1",
        {
            "request_kind": "compensation_v1",
            "operation_id": operation_id,
            "parent_operation_id": parent_operation_id,
            "compensation_kind": compensation_kind,
            "conversation_id": conversation_id,
        },
    )


class WriteOperationCoordinator:
    def __init__(self, repository: WriteOperationRepository):
        self.repository = repository

    def execute_primary(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        prepared: PreparedToolCall[Any, Any],
        context: ToolExecutionContext,
        prepare_identity: ApprovedWritePrepareCallIdentity,
        request_fingerprint: str,
        undo_seed_builder: UndoSeedBuilder | None = None,
        undo_builder: UndoBuilder | None = None,
        approval_decided_callback: Callable[[object | None], None] | None = None,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        owner = self.repository.prepare_owner(operation_id)
        try:
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                outer_transaction = session.get_transaction()
                if outer_transaction is None:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    session.rollback()
                    return OperationUnknown(operation_id, "operation_result_unknown", True), None
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay, None
                authority = context.authority
                if type(authority) is not ApprovalExecutionAuthority:
                    raise WriteOperationError("operation_identity_conflict")
                if type(prepare_identity) is not ApprovedWritePrepareCallIdentity:
                    raise WriteOperationError("operation_identity_conflict")
                factory = context.authority_factory
                conversation = session.get(Conversation, conversation_id)
                if conversation is None:
                    raise WriteOperationError("operation_identity_conflict")
                locked_pending = self._verify_primary(
                    session,
                    operation,
                    conversation,
                    conversation_id,
                    prepared,
                    authority,
                    prepare_identity,
                    request_fingerprint,
                    factory,
                )
                bound_context = context.bind(session)
                try:
                    scope_failure = pre_resolver_scope_policy(
                        prepared.spec, bound_context
                    )
                    if scope_failure is not None:
                        raise WriteOperationError("scope_access_denied")
                    _binding, binding_allowed = audit_bindings(
                        prepared.spec,
                        prepared.typed_args,
                        bound_context,
                    )
                except WriteOperationError:
                    raise
                except Exception as exc:
                    raise WriteOperationError(
                        "operation_not_committed", retryable=True
                    ) from exc
                if not binding_allowed:
                    raise WriteOperationError("scope_access_denied")
                if prepared.spec.mutable_validator is not None:
                    failure = prepared.spec.mutable_validator(prepared.typed_args, bound_context)
                    try:
                        factory.register_execution_transaction(
                            session,
                            outer_transaction,
                            authority=authority,
                        )
                    except AuthorityPhaseError as exc:
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    if failure is not None:
                        claimed = session.execute(
                            update(Conversation)
                            .where(Conversation.id == conversation_id)
                            .where(Conversation.pending_operation_id == operation_id)
                            .where(Conversation.pending_tool_call_id == prepared.tool_call_id)
                            .where(Conversation.pending_tool_name == prepared.spec.name)
                            .where(Conversation.pending_args == locked_pending.raw_args)
                            .where(Conversation.pending_confirmation_claim_id == "")
                            .values(
                                pending_confirmation_claim_id=operation_id,
                                pending_confirmation_claimed_at=datetime.now(timezone.utc),
                            )
                        )
                        if getattr(claimed, "rowcount", 0) != 1:
                            raise WriteOperationError("operation_identity_conflict")
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = ledger_fingerprint(
                            self.repository.key,
                            "write-operation-input-v1",
                            {"arguments_digest": locked_pending.arguments_digest},
                        )
                        self.repository.append_transition(session, operation_id, 2, "approved")
                        self.repository.append_transition(session, operation_id, 3, "claimed")
                        if approval_decided_callback is not None:
                            try:
                                approval_decided_callback(session)
                            except Exception:
                                pass
                        return self._commit_failure(
                            session,
                            operation,
                            prepared,
                            failure,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                        )
                try:
                    undo_seed = (
                        undo_seed_builder(prepared, bound_context)
                        if undo_seed_builder is not None
                        else {}
                    )
                    factory.register_execution_transaction(
                        session,
                        outer_transaction,
                        authority=authority,
                    )
                except AuthorityPhaseError as exc:
                    raise WriteOperationError(
                        "operation_not_committed", retryable=True
                    ) from exc
                except Exception as exc:
                    raise WriteOperationError(
                        "operation_not_committed", retryable=True
                    ) from exc
                claimed = session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .where(Conversation.pending_operation_id == operation_id)
                    .where(Conversation.pending_tool_call_id == prepared.tool_call_id)
                    .where(Conversation.pending_tool_name == prepared.spec.name)
                    .where(Conversation.pending_args == locked_pending.raw_args)
                    .where(Conversation.pending_confirmation_claim_id == "")
                    .values(
                        pending_confirmation_claim_id=operation_id,
                        pending_confirmation_claimed_at=datetime.now(timezone.utc),
                    )
                )
                if getattr(claimed, "rowcount", 0) != 1:
                    raise WriteOperationError("operation_identity_conflict")
                self.repository.append_transition(session, operation_id, 2, "approved")
                self.repository.append_transition(session, operation_id, 3, "claimed")
                execution_claim = None
                try:
                    execution_claim = factory.issue_execution_claim(
                        authority,
                        prepared=prepared,
                        pending=prepared.pending_identity,
                        operation_id=operation_id,
                        tool_call_id=prepared.tool_call_id,
                        tool_name=prepared.spec.name,
                        effective_args_digest=locked_pending.arguments_digest,
                        pending_action_revision=locked_pending.pending_action_revision,
                        session=session,
                        transaction=outer_transaction,
                    )
                    execute_identity = factory.create_approved_write_execute_identity(
                        prepare_identity,
                        prepared=prepared,
                        execution_claim=execution_claim,
                    )
                except AuthorityPhaseError as exc:
                    if execution_claim is not None and factory.claim_state(execution_claim) is not None:
                        factory.revoke(execution_claim)
                    raise WriteOperationError("operation_identity_conflict") from exc
                if approval_decided_callback is not None:
                    try:
                        approval_decided_callback(session)
                    except Exception:
                        pass
                executor_started = False
                started_recorded = False

                def mark_execution_stage(stage: str) -> None:
                    nonlocal executor_started
                    if stage == "executor":
                        executor_started = True

                try:
                    with session.begin_nested():
                        started_recorded = project_tool_started_bound(
                            bound_context.run_recorder, session, prepared
                        )
                        dispatched = execute_prepared(
                            prepared,
                            bound_context,
                            call_identity=execute_identity,
                            execution_claim=execution_claim,
                            locked_effective_args_digest=locked_pending.arguments_digest,
                            stage_sink=mark_execution_stage,
                        )
                        if isinstance(dispatched.outcome, ToolFailure):
                            raise _ClaimedWriteFailure(
                                ToolExecutionRecord(
                                    prepared,
                                    dispatched.outcome,
                                    True,
                                    operation_id,
                                    False,
                                    False,
                                    None,
                                    None,
                                    started_recorded,
                                )
                            )
                        result = dispatched.outcome.result
                        record = ToolExecutionRecord(
                            prepared, dispatched.outcome, True, operation_id, False
                        )
                        undo = (
                            undo_builder(prepared, record, undo_seed)
                            if undo_builder is not None
                            else None
                        )
                        write_contract = prepared.spec.write_contract
                        if write_contract is None:
                            raise WriteOperationError("operation_not_transactional")
                        if write_contract.undo_policy is UndoPolicy.REQUIRED and not undo:
                            raise WriteOperationError("operation_projection_failed")
                        if write_contract.undo_policy is UndoPolicy.NONE and undo is not None:
                            raise WriteOperationError("operation_projection_failed")
                        visible = render_compatibility(prepared.spec, record.outcome)
                        transport = project_transport_event(prepared.spec, record)
                        payload = build_terminal_payload(
                            status="committed",
                            result_contract=write_contract.result_contract,
                            result=result,
                            visible_result=visible,
                            transport=transport,
                            undo=undo,
                            failure_category=None,
                            failure_code=None,
                            budgets=(
                                write_contract.result_bytes,
                                write_contract.visible_bytes,
                                write_contract.transport_bytes,
                                write_contract.undo_bytes,
                            ),
                        )
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = ledger_fingerprint(
                            self.repository.key,
                            "write-operation-input-v1",
                            {"arguments_digest": locked_pending.arguments_digest},
                        )
                        self._set_terminal(operation, payload, owner)
                        self.repository.append_transition(session, operation_id, 4, "committed")
                        session.flush()
                        record = ToolExecutionRecord(
                            prepared,
                            record.outcome,
                            True,
                            operation_id,
                            False,
                            True,
                            payload.visible_result,
                            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
                            started_recorded,
                        )
                except _ClaimedWriteFailure as exc:
                    failure = cast(ToolFailure, exc.record.outcome)
                    try:
                        return self._commit_failure(
                            session,
                            operation,
                            prepared,
                            failure,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            record=exc.record,
                        )
                    except Exception:
                        return self._commit_projection_failure(
                            session,
                            operation,
                            prepared,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            started_recorded=started_recorded,
                        )
                except Exception as exc:
                    if executor_started:
                        return self._commit_projection_failure(
                            session,
                            operation,
                            prepared,
                            request_fingerprint,
                            owner,
                            arguments_digest=locked_pending.arguments_digest,
                            started_recorded=started_recorded,
                        )
                    if isinstance(exc, WriteOperationError):
                        raise
                    failure = _map_exception(prepared, exc)
                    if failure.category == "internal_error":
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    record = ToolExecutionRecord(
                        prepared,
                        failure,
                        False,
                        operation_id,
                        False,
                        False,
                        None,
                        None,
                        started_recorded,
                    )
                    committed_failure = self._commit_failure(
                        session,
                        operation,
                        prepared,
                        failure,
                        request_fingerprint,
                        owner,
                        arguments_digest=locked_pending.arguments_digest,
                        record=record,
                    )
                    return committed_failure
                finally:
                    if factory.claim_state(execution_claim) is not None:
                        factory.revoke(execution_claim)
                try:
                    session.commit()
                except OperationalError:
                    return (
                        self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_result_unknown",
                            proposed_code="operation_not_committed",
                        ),
                        None,
                    )
                return OperationCommitted(operation_id, payload, owner), record
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable), None
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation_id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_busy",
                ),
                None,
            )
        except BaseException:
            raise

    def reject_primary(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        request_fingerprint: str,
        visible_result: str,
        confirmation_token: str | None = None,
    ) -> OperationExecution:
        owner = self.repository.prepare_owner(operation_id)
        try:
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                outer_transaction = session.get_transaction()
                if outer_transaction is None:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay
                if operation.conversation_id is None:
                    raise WriteOperationError("operation_unavailable")
                if (
                    operation.conversation_id != conversation_id
                    or operation.tool_call_id != tool_call_id
                    or operation.tool_name != tool_name
                ):
                    raise WriteOperationError("operation_identity_conflict")
                pointer_row = session.execute(
                    select(
                        Conversation.id,
                        Conversation.pending_operation_id,
                        Conversation.pending_tool_call_id,
                        Conversation.pending_tool_name,
                        Conversation.pending_confirmation_claim_id,
                    ).where(Conversation.id == conversation_id)
                ).one_or_none()
                if pointer_row is None:
                    raise WriteOperationError("operation_unavailable")
                pointer = LedgerPendingPointer(
                    conversation_id=int(pointer_row.id),
                    operation_id=str(pointer_row.pending_operation_id or ""),
                    tool_call_id=str(pointer_row.pending_tool_call_id or ""),
                    tool_name=str(pointer_row.pending_tool_name or ""),
                    pending_confirmation_claim_id=str(
                        pointer_row.pending_confirmation_claim_id or ""
                    ),
                )
                if (
                    pointer.operation_id != operation_id
                    or pointer.tool_call_id != tool_call_id
                    or pointer.tool_name != tool_name
                    or pointer.pending_confirmation_claim_id != ""
                ):
                    raise WriteOperationError("operation_identity_conflict")

                if confirmation_token is not None:
                    try:
                        token_bytes = confirmation_token.encode("ascii")
                    except UnicodeEncodeError as exc:
                        raise WriteOperationError("invalid_confirmation") from exc
                    supplied_fingerprint = ledger_fingerprint(
                        self.repository.key,
                        "write-operation-confirmation-token-v1",
                        token_bytes,
                    )
                    if not hmac.compare_digest(
                        supplied_fingerprint,
                        operation.confirmation_token_fingerprint or "",
                    ):
                        raise WriteOperationError("operation_input_conflict")

                payload = build_terminal_payload(
                    status="rejected",
                    result_contract="rejection_json_v1",
                    result={"message": visible_result},
                    visible_result=visible_result,
                    transport={
                        "status": "cancelled",
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                    },
                    undo=None,
                    failure_category=None,
                    failure_code=None,
                )

                factory = AuthorityFactory()
                factory.register_operation(operation)
                pointer_revision = int.from_bytes(
                    hashlib.sha256(
                        canonical_json(
                            {
                                "conversation_id": pointer.conversation_id,
                                "operation_id": pointer.operation_id,
                                "tool_call_id": pointer.tool_call_id,
                                "tool_name": pointer.tool_name,
                                "pending_confirmation_claim_id": (
                                    pointer.pending_confirmation_claim_id
                                ),
                            }
                        ).encode("utf-8")
                    ).digest()[:8],
                    "big",
                ) & ((1 << 63) - 1)
                pointer_digest = "sha256:" + hashlib.sha256(
                    canonical_json(
                        {
                            "operation_id": pointer.operation_id,
                            "tool_call_id": pointer.tool_call_id,
                            "tool_name": pointer.tool_name,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                factory.register_pending(
                    pointer,
                    conversation_id=pointer.conversation_id,
                    operation_id=pointer.operation_id,
                    tool_call_id=pointer.tool_call_id,
                    tool_name=pointer.tool_name,
                    pending_action_revision=pointer_revision,
                    pending_confirmation_claim_id=(
                        pointer.pending_confirmation_claim_id
                    ),
                    arguments_digest=pointer_digest,
                    effective_args_digest=pointer_digest,
                )
                factory.register_transaction(outer_transaction)
                proof = factory.issue_omitted_token_proof(
                    operation,
                    pending_pointer=pointer,
                    transaction=outer_transaction,
                )

                expected_adapter_kind = operation.adapter_kind
                expected_proposal_fingerprint = operation.proposal_fingerprint
                expected_confirmation_token_fingerprint = (
                    operation.confirmation_token_fingerprint
                )
                expected_authorization_scope_fingerprint = (
                    operation.authorization_scope_fingerprint
                )
                expected_fingerprint_key_id = operation.fingerprint_key_id

                def reject_cas() -> None:
                    cleared = session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .where(Conversation.pending_operation_id == operation_id)
                        .where(Conversation.pending_tool_call_id == tool_call_id)
                        .where(Conversation.pending_tool_name == tool_name)
                        .where(Conversation.pending_confirmation_claim_id == "")
                        .values(
                            pending_operation_id="",
                            pending_tool_call_id="",
                            pending_tool_name="",
                            pending_confirmation_claim_id="",
                            pending_confirmation_claimed_at=None,
                            pending_args="",
                            pending_human="",
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    if getattr(cleared, "rowcount", 0) != 1:
                        raise WriteOperationError("operation_identity_conflict")
                    now = datetime.now(timezone.utc)
                    terminalized = session.execute(
                        update(WriteOperation)
                        .where(WriteOperation.id == operation_id)
                        .where(WriteOperation.operation_role == "primary")
                        .where(WriteOperation.status == "proposed")
                        .where(WriteOperation.conversation_id == conversation_id)
                        .where(WriteOperation.adapter_kind == expected_adapter_kind)
                        .where(WriteOperation.tool_call_id == tool_call_id)
                        .where(WriteOperation.tool_name == tool_name)
                        .where(
                            WriteOperation.proposal_fingerprint
                            == expected_proposal_fingerprint
                        )
                        .where(
                            WriteOperation.confirmation_token_fingerprint
                            == expected_confirmation_token_fingerprint
                        )
                        .where(
                            WriteOperation.authorization_scope_fingerprint
                            == expected_authorization_scope_fingerprint
                        )
                        .where(
                            WriteOperation.fingerprint_key_id
                            == expected_fingerprint_key_id
                        )
                        .where(WriteOperation.operation_request_fingerprint.is_(None))
                        .values(
                            status=payload.status,
                            operation_request_fingerprint=request_fingerprint,
                            result_contract=payload.result_contract,
                            result_json=payload.result_json,
                            visible_result=payload.visible_result,
                            transport_json=payload.transport_json,
                            undo_json=payload.undo_json,
                            terminal_payload_sha256=payload.digest,
                            failure_category=payload.failure_category,
                            failure_code=payload.failure_code,
                            rejected_at=now,
                            delivery_status="pending",
                            delivery_generation=owner.generation,
                            delivery_owner_token_fingerprint=owner.fingerprint,
                            delivery_lease_expires_at=cast(
                                Any,
                                func.unixepoch("now")
                                + DELIVERY_OWNER_LEASE_SECONDS,
                            ),
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(terminalized, "rowcount", 0) != 1:
                        raise WriteOperationError("operation_identity_conflict")
                    session.expire(operation)
                    session.refresh(operation)
                    self.repository.append_transition(session, operation_id, 2, "rejected")
                    session.add_all(
                        (
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="tool",
                                content=canonical_json(
                                    {
                                        "status": "cancelled",
                                        "message": "用户取消了该操作，未执行。",
                                    }
                                ),
                                tool_call_id=tool_call_id,
                                operation_id=operation_id,
                                delivery_kind="origin_tool_result",
                                delivery_ordinal=0,
                            ),
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=visible_result,
                                operation_id=operation_id,
                                delivery_kind="continuation_message",
                                delivery_ordinal=1,
                            ),
                        )
                    )
                    session.flush()
                    if not self.repository.complete_delivery(
                        session,
                        owner,
                        outcome="final_response",
                    ):
                        raise WriteOperationError("operation_delivery_unknown")

                try:
                    with factory.claim_lifecycle(proof):
                        reject_cas()
                        session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                finally:
                    factory.close()
                return OperationFailed(operation_id, payload, None)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_result_unknown",
                proposed_code="operation_busy",
            )

    def execute_legacy(
        self,
        *,
        operation_id: str,
        conversation_id: int,
        tool_call_id: str,
        tool_name: str,
        input_fingerprint: str,
        request_fingerprint: str,
        executor: LegacyExecutor,
    ) -> OperationExecution:
        owner = self.repository.prepare_owner(operation_id)
        try:
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay
                if (
                    operation.conversation_id != conversation_id
                    or operation.tool_call_id != tool_call_id
                    or operation.tool_name != tool_name
                    or operation.adapter_kind != "legacy_deterministic"
                ):
                    raise WriteOperationError("operation_identity_conflict")
                claimed = session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .where(Conversation.pending_operation_id == operation_id)
                    .where(Conversation.pending_tool_call_id == tool_call_id)
                    .where(Conversation.pending_tool_name == tool_name)
                    .values(
                        pending_confirmation_claim_id=operation_id,
                        pending_confirmation_claimed_at=datetime.now(timezone.utc),
                    )
                )
                if getattr(claimed, "rowcount", 0) != 1:
                    raise WriteOperationError("operation_identity_conflict")
                self.repository.append_transition(session, operation_id, 2, "approved")
                self.repository.append_transition(session, operation_id, 3, "claimed")
                try:
                    with session.begin_nested():
                        visible = executor(session)
                        payload = build_terminal_payload(
                            status="committed",
                            result_contract="legacy_string_v1",
                            result={"value": visible},
                            visible_result=visible,
                            transport={
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "status": "success",
                                "summary": visible[:500],
                            },
                            undo=None,
                            failure_category=None,
                            failure_code=None,
                        )
                        operation.operation_request_fingerprint = request_fingerprint
                        operation.input_fingerprint = input_fingerprint
                        self._set_terminal(operation, payload, owner)
                        self.repository.append_transition(session, operation_id, 4, "committed")
                        session.flush()
                except ValueError as exc:
                    code = str(exc)
                    if not code.isascii() or not code or len(code) > 128:
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    payload = build_terminal_payload(
                        status="failed",
                        result_contract="legacy_string_v1",
                        result={"code": code},
                        visible_result="错误：" + code,
                        transport={
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "status": "error",
                            "summary": "",
                        },
                        undo=None,
                        failure_category="conflict",
                        failure_code=code,
                    )
                    operation.operation_request_fingerprint = request_fingerprint
                    operation.input_fingerprint = input_fingerprint
                    self._set_terminal(operation, payload, owner)
                    self.repository.append_transition(session, operation_id, 4, "failed")
                try:
                    session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                if payload.status == "committed":
                    return OperationCommitted(operation_id, payload, owner)
                return OperationFailed(operation_id, payload, owner)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_result_unknown",
                proposed_code="operation_busy",
            )
        except Exception:
            return OperationUnknown(operation_id, "operation_not_committed", True)

    def execute_compensation(
        self,
        *,
        parent_operation_id: str,
        conversation_id: int,
        compensation_kind: str,
        executor: CompensationExecutor,
    ) -> OperationExecution:
        operation_id = compensation_operation_id(parent_operation_id, compensation_kind)
        request_fingerprint = compensation_request_fingerprint(
            self.repository.key,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            compensation_kind=compensation_kind,
            conversation_id=conversation_id,
        )
        try:
            # Persist the deterministic proposal separately so a commit-unknown
            # execution can never be retried by the same request.
            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    parent = session.get(WriteOperation, parent_operation_id)
                    if (
                        parent is None
                        or parent.operation_role != "primary"
                        or parent.status != "committed"
                        or parent.conversation_id != conversation_id
                        or parent.undo_json is None
                    ):
                        raise WriteOperationError("operation_identity_conflict")
                    parent_payload = payload_from_operation(parent)
                    undo = json.loads(parent.undo_json)
                    expected_kind = compensation_kind_for_undo(str(undo.get("kind") or ""))
                    if compensation_kind != expected_kind:
                        raise WriteOperationError("operation_identity_conflict")
                    operation = WriteOperation(
                        id=operation_id,
                        operation_role="compensation",
                        parent_operation_id=parent_operation_id,
                        parent_terminal_payload_sha256=parent_payload.digest,
                        conversation_id=conversation_id,
                        tool_call_id=None,
                        tool_name=compensation_kind,
                        adapter_kind="compensation",
                        status="proposed",
                        fingerprint_key_id=self.repository.key.key_id,
                        operation_request_fingerprint=request_fingerprint,
                        delivery_status="pending",
                        delivery_generation=0,
                    )
                    session.add(operation)
                    self.repository.append_transition(session, operation_id, 1, "proposed")
                    try:
                        session.commit()
                    except OperationalError:
                        return self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_not_committed",
                            proposed_code="operation_not_committed",
                        )
                else:
                    self._verify_compensation(operation, request_fingerprint)
                    if operation.status in _TERMINAL_STATUSES:
                        replay = self.repository.replay(operation, request_fingerprint)
                        session.rollback()
                        return replay
                    try:
                        session.commit()
                    except OperationalError:
                        return self._reconcile_commit_unknown(
                            operation_id,
                            request_fingerprint,
                            absent_code="operation_not_committed",
                            proposed_code="operation_not_committed",
                        )

            with self.repository.session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    raise WriteOperationError("operation_result_unknown", retryable=True)
                self._verify_compensation(operation, request_fingerprint)
                if operation.status in _TERMINAL_STATUSES:
                    replay = self.repository.replay(operation, request_fingerprint)
                    session.rollback()
                    return replay
                parent = session.get(WriteOperation, parent_operation_id)
                if (
                    parent is None
                    or parent.operation_role != "primary"
                    or parent.status != "committed"
                    or parent.conversation_id != conversation_id
                    or parent.undo_json is None
                ):
                    raise WriteOperationError("operation_integrity_error")
                parent_payload = payload_from_operation(parent)
                undo = cast(dict[str, Any], json.loads(parent.undo_json))
                if (
                    operation.parent_terminal_payload_sha256 != parent_payload.digest
                    or compensation_kind != compensation_kind_for_undo(str(undo.get("kind") or ""))
                ):
                    raise WriteOperationError("operation_integrity_error")
                input_fingerprint = ledger_fingerprint(
                    self.repository.key,
                    "write-operation-input-v1",
                    {
                        "operation_request_fingerprint": request_fingerprint,
                        "parent_terminal_payload_sha256": parent_payload.digest,
                        "undo": undo,
                    },
                )
                self.repository.append_transition(session, operation_id, 2, "approved")
                self.repository.append_transition(session, operation_id, 3, "claimed")
                try:
                    with session.begin_nested():
                        message = executor(session, undo)
                except ValueError as exc:
                    code = str(exc)
                    if not code.isascii() or not code or len(code) > 128:
                        raise WriteOperationError(
                            "operation_not_committed", retryable=True
                        ) from exc
                    payload = build_terminal_payload(
                        status="failed",
                        result_contract="compensation_json_v1",
                        result={"code": code},
                        visible_result="当前记录已被修改，无法安全撤销。",
                        transport={},
                        undo=None,
                        failure_category="conflict",
                        failure_code=code,
                    )
                else:
                    payload = build_terminal_payload(
                        status="committed",
                        result_contract="compensation_json_v1",
                        result={"message": message},
                        visible_result=message,
                        transport={},
                        undo=None,
                        failure_category=None,
                        failure_code=None,
                    )
                operation.input_fingerprint = input_fingerprint
                self._set_compensation_terminal(operation, payload)
                self.repository.append_transition(session, operation_id, 4, payload.status)
                if payload.status == "committed":
                    session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .where(Conversation.last_write_operation_id == parent_operation_id)
                        .values(last_write_operation_id="", last_write_undo_json="")
                    )
                try:
                    session.commit()
                except OperationalError:
                    return self._reconcile_commit_unknown(
                        operation_id,
                        request_fingerprint,
                        absent_code="operation_result_unknown",
                        proposed_code="operation_not_committed",
                    )
                if payload.status == "committed":
                    return OperationCommitted(operation_id, payload, None)
                return OperationFailed(operation_id, payload, None)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return self._reconcile_commit_unknown(
                operation_id,
                request_fingerprint,
                absent_code="operation_busy",
                proposed_code="operation_busy",
            )
        except Exception:
            return OperationUnknown(operation_id, "operation_not_committed", True)

    def _reconcile_commit_unknown(
        self,
        operation_id: str,
        request_fingerprint: str,
        *,
        absent_code: str,
        proposed_code: str,
    ) -> OperationExecution:
        """Resolve a lost COMMIT response from authoritative Ledger state."""
        try:
            with self.repository.session_factory() as session:
                operation = session.get(WriteOperation, operation_id)
                if operation is None:
                    return OperationUnknown(operation_id, absent_code, True)
                if operation.status not in _TERMINAL_STATUSES:
                    return OperationUnknown(operation_id, proposed_code, True)
                return self.repository.replay(operation, request_fingerprint)
        except WriteOperationError as exc:
            return OperationUnknown(operation_id, exc.code, exc.retryable)
        except OperationalError:
            return OperationUnknown(operation_id, "operation_result_unknown", True)

    def _verify_compensation(self, operation: WriteOperation, request_fingerprint: str) -> None:
        if (
            operation.operation_role != "compensation"
            or operation.adapter_kind != "compensation"
            or operation.fingerprint_key_id != self.repository.key.key_id
            or not operation.operation_request_fingerprint
            or not hmac.compare_digest(operation.operation_request_fingerprint, request_fingerprint)
        ):
            raise WriteOperationError("operation_input_conflict")

    @staticmethod
    def _set_compensation_terminal(operation: WriteOperation, payload: TerminalPayload) -> None:
        operation.status = payload.status
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.undo_json = None
        operation.terminal_payload_sha256 = payload.digest
        operation.failure_category = payload.failure_category
        operation.failure_code = payload.failure_code
        now = datetime.now(timezone.utc)
        if payload.status == "committed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.committed_at = now
        else:
            operation.approved_at = now
            operation.claimed_at = now
            operation.failed_at = now
        operation.delivery_status = "not_applicable"
        operation.delivery_outcome = "none"
        operation.delivery_message_count = 0
        operation.delivery_generation = 0
        operation.delivered_at = now
        operation.updated_at = now

    def _verify_primary(
        self,
        session: Session,
        operation: WriteOperation,
        conversation: Conversation,
        conversation_id: int,
        prepared: PreparedToolCall[Any, Any],
        authority: ApprovalExecutionAuthority,
        prepare_identity: ApprovedWritePrepareCallIdentity,
        request_fingerprint: str,
        factory: Any,
    ) -> _LockedPendingIdentity:
        if operation.conversation_id is None:
            raise WriteOperationError("operation_unavailable")
        if operation.conversation_id != conversation_id or conversation.id != conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        if operation.authorization_scope_fingerprint is None:
            raise WriteOperationError("authorization_scope_unbound")
        locked_proposal = _locked_pending_identity(
            conversation.pending_tool_call_id,
            conversation.pending_tool_name,
            conversation.pending_args,
        )
        try:
            require_authority_phase(
                authority,
                AuthorityUse.APPROVED_WRITE_PREPARE,
                prepare_identity,
            )
            require_authority_spec(
                authority,
                AuthorityUse.APPROVED_WRITE_PREPARE,
                prepared.spec,
            )
            persisted_values = parse_arguments(conversation.pending_args)
            prepared_values = cast(dict[str, JSONValue], dict(prepared.arguments))
            prepared_arguments = canonical_json(cast(JSONValue, prepared_values))
            persisted_arguments = canonical_json(cast(JSONValue, persisted_values))
            effective = _locked_pending_identity(
                prepared.tool_call_id,
                prepared.spec.name,
                prepared_arguments,
            )
            prepared_pending_token = factory.pending_token(prepared.pending_identity)
        except (
            ArgumentValidationError,
            AuthorityPhaseError,
            TypeError,
            ValueError,
        ) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if (
            operation.operation_role != "primary"
            or operation.adapter_kind != "typed"
            or operation.status != "proposed"
            or operation.fingerprint_key_id != self.repository.key.key_id
            or operation.tool_call_id != prepared.tool_call_id
            or operation.tool_name != prepared.spec.name
            or conversation.pending_operation_id != operation.id
            or conversation.pending_tool_call_id != prepared.tool_call_id
            or conversation.pending_tool_name != prepared.spec.name
            or authority.operation_id != operation.id
            or authority.conversation_id != conversation_id
            or authority.tool_call_id != prepared.tool_call_id
            or authority.tool_name != prepared.spec.name
            or prepare_identity.operation_id != operation.id
            or prepare_identity.tool_call_id != prepared.tool_call_id
            or prepare_identity.tool_name != prepared.spec.name
            or prepared.pending_action_revision != effective.pending_action_revision
            or authority.pending_action_revision != effective.pending_action_revision
            or prepare_identity.pending_action_revision != effective.pending_action_revision
            or prepared_pending_token is not authority.pending_identity
            or prepare_identity.pending_identity is not authority.pending_identity
            or not _constant_time_text_equal(
                prepared.arguments_digest, effective.arguments_digest
            )
            or not _constant_time_text_equal(
                authority.effective_args_digest, effective.arguments_digest
            )
            or not _constant_time_text_equal(
                prepare_identity.effective_args_digest, effective.arguments_digest
            )
            or not _constant_time_text_equal(
                operation.proposal_fingerprint,
                ledger_fingerprint(
                    self.repository.key,
                    "write-operation-proposal-v1",
                    persisted_values,
                ),
            )
        ):
            raise WriteOperationError("operation_identity_conflict")
        no_edit_request = operation_request_fingerprint(
            self.repository.key,
            operation_id=operation.id,
            tool_call_id=prepared.tool_call_id,
            approved=True,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=operation.confirmation_token_fingerprint or "",
            proposal_fingerprint=operation.proposal_fingerprint or "",
        )
        edited_request = operation_request_fingerprint(
            self.repository.key,
            operation_id=operation.id,
            tool_call_id=prepared.tool_call_id,
            approved=True,
            edited_args_present=True,
            edited_args=prepared_values,
            rejection_feedback_present=False,
            rejection_feedback="",
            confirmation_token_fingerprint=operation.confirmation_token_fingerprint or "",
            proposal_fingerprint=operation.proposal_fingerprint or "",
        )
        if not (
            (
                _constant_time_text_equal(prepared_arguments, persisted_arguments)
                and _constant_time_text_equal(request_fingerprint, no_edit_request)
            )
            or _constant_time_text_equal(request_fingerprint, edited_request)
        ):
            raise WriteOperationError("operation_input_conflict")
        try:
            scope = authority.trusted_scope
            context_ref: int | None = None
            if conversation.context_type == "application":
                context_ref = int(conversation.context_ref)
            if (
                authority.conversation_scope_revision != conversation.scope_revision
                or scope.context_type != conversation.context_type
                or scope.context_ref != context_ref
                or scope.mode != conversation.mode
            ):
                raise WriteOperationError("authorization_scope_changed")
            locked_scope_fingerprint = authorization_scope_fingerprint(
                self.repository.key,
                conversation_id=conversation.id,
                conversation_scope_revision=conversation.scope_revision,
                context_type=conversation.context_type,
                context_ref=context_ref,
                mode=conversation.mode,
                capability_profile_id=authority.capability_profile_id,
                capability_policy_version=authority.capability_policy_version,
                binding_policy_version=authority.binding_policy_version,
                capability_profile_fingerprint=authority.capability_profile_fingerprint,
                binding_policy_fingerprint=authority.binding_policy_fingerprint,
            )
        except WriteOperationError:
            raise
        except Exception as exc:
            raise WriteOperationError(
                "operation_not_committed", retryable=True
            ) from exc
        if not _constant_time_text_equal(
            operation.authorization_scope_fingerprint,
            locked_scope_fingerprint,
        ):
            raise WriteOperationError("authorization_scope_changed")
        if context_ref is not None:
            try:
                active_parent = AuthorityApplicationVisibilityQuery().execute_on_session(
                    session, context_ref
                )
            except AuthorityApplicationVisibilityError as exc:
                raise WriteOperationError(
                    "operation_not_committed", retryable=True
                ) from exc
            if active_parent is None:
                raise WriteOperationError("authorization_scope_unavailable")
        return _LockedPendingIdentity(
            locked_proposal.raw_args,
            effective.arguments_digest,
            effective.pending_action_revision,
        )

    def _commit_failure(
        self,
        session: Session,
        operation: WriteOperation,
        prepared: PreparedToolCall[Any, Any],
        failure: ToolFailure,
        request_fingerprint: str,
        owner: DeliveryOwnership,
        *,
        arguments_digest: str,
        record: ToolExecutionRecord[Any, Any] | None = None,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        if failure.category == "internal_error" and (
            record is None or not record.execution_started
        ):
            raise WriteOperationError("operation_not_committed", retryable=True)
        # A validator failure discovered inside the ledger transaction already
        # owns a durable terminal and must flow through this request's delivery.
        resolved = record or ToolExecutionRecord(
            prepared, failure, False, operation.id, False, True
        )
        visible = render_compatibility(prepared.spec, failure)
        payload = build_terminal_payload(
            status="failed",
            result_contract="typed_json_v1",
            result={"category": failure.category, "code": failure.code},
            visible_result=visible,
            transport=project_transport_event(prepared.spec, resolved),
            undo=None,
            failure_category=failure.category,
            failure_code=failure.code,
        )
        operation.operation_request_fingerprint = request_fingerprint
        operation.input_fingerprint = ledger_fingerprint(
            self.repository.key,
            "write-operation-input-v1",
            {"arguments_digest": arguments_digest},
        )
        self._set_terminal(operation, payload, owner)
        self.repository.append_transition(session, operation.id, 4, "failed")
        try:
            session.commit()
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation.id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_not_committed",
                ),
                None,
            )
        persisted = ToolExecutionRecord(
            prepared,
            resolved.outcome,
            resolved.execution_started,
            operation.id,
            False,
            True,
            payload.visible_result,
            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
            resolved.journal_started_recorded,
        )
        return OperationFailed(operation.id, payload, owner), persisted

    def _commit_projection_failure(
        self,
        session: Session,
        operation: WriteOperation,
        prepared: PreparedToolCall[Any, Any],
        request_fingerprint: str,
        owner: DeliveryOwnership,
        *,
        arguments_digest: str,
        started_recorded: bool,
    ) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]:
        """Durably terminalize after dispatch without invoking fallible projections again."""

        failure = ToolFailure("internal_error", "operation_projection_failed")
        payload = build_terminal_payload(
            status="failed",
            result_contract="typed_json_v1",
            result={"category": failure.category, "code": failure.code},
            visible_result="错误：操作结果处理失败，操作未提交。",
            transport={
                "status": "error",
                "tool_call_id": prepared.tool_call_id,
                "tool_name": prepared.spec.name,
                "category": failure.category,
                "code": failure.code,
            },
            undo=None,
            failure_category=failure.category,
            failure_code=failure.code,
        )
        operation.operation_request_fingerprint = request_fingerprint
        operation.input_fingerprint = ledger_fingerprint(
            self.repository.key,
            "write-operation-input-v1",
            {"arguments_digest": arguments_digest},
        )
        self._set_terminal(operation, payload, owner)
        self.repository.append_transition(session, operation.id, 4, "failed")
        try:
            session.commit()
        except OperationalError:
            return (
                self._reconcile_commit_unknown(
                    operation.id,
                    request_fingerprint,
                    absent_code="operation_result_unknown",
                    proposed_code="operation_not_committed",
                ),
                None,
            )
        record = ToolExecutionRecord(
            prepared,
            failure,
            True,
            operation.id,
            False,
            True,
            payload.visible_result,
            cast(dict[str, JSONValue], json.loads(payload.transport_json)),
            started_recorded,
        )
        return OperationFailed(operation.id, payload, owner), record

    @staticmethod
    def _set_terminal(
        operation: WriteOperation,
        payload: TerminalPayload,
        owner: DeliveryOwnership,
    ) -> None:
        operation.status = payload.status
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.undo_json = payload.undo_json
        operation.terminal_payload_sha256 = payload.digest
        operation.failure_category = payload.failure_category
        operation.failure_code = payload.failure_code
        now = datetime.now(timezone.utc)
        if payload.status == "committed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.committed_at = now
        elif payload.status == "failed":
            operation.approved_at = now
            operation.claimed_at = now
            operation.failed_at = now
        elif payload.status == "rejected":
            operation.rejected_at = now
        operation.delivery_status = "pending"
        operation.delivery_generation = owner.generation
        operation.delivery_owner_token_fingerprint = owner.fingerprint
        operation.delivery_lease_expires_at = cast(
            Any, func.unixepoch("now") + DELIVERY_OWNER_LEASE_SECONDS
        )
        operation.updated_at = now


def _map_exception(
    prepared: PreparedToolCall[Any, Any],
    error: Exception,
) -> ToolFailure:
    for mapping in prepared.spec.exception_map:
        if isinstance(error, mapping.exception_type):
            detail = ""
            if mapping.compatibility_detail is not None:
                try:
                    detail = mapping.compatibility_detail(error)
                except Exception:
                    detail = ""
            return ToolFailure(mapping.category, mapping.code, detail)
    return ToolFailure("internal_error", "executor_exception")
