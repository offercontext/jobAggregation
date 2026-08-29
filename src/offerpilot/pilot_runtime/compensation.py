"""Exact, Bundle-bound handlers for the four trusted compensation operations."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from types import MappingProxyType
from typing import Literal, NoReturn, TypeAlias, cast

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.orm import Session

from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue
from offerpilot.ai.tool_runtime.metadata import (
    BundleInstanceToken,
    CompensationHandlerBindingV1,
    CompensationMetadataView,
    FrozenJSONObject,
    FrozenJSONValue,
)
from offerpilot.ai.tool_runtime.policy_types import CompensationKind, UndoPayloadKind
from offerpilot.application_status import normalize_application_status
from offerpilot.models import (
    APPLICATION_FOREIGN_KEY_MODELS,
    Application,
    ApplicationEvent,
    InterviewNote,
)


UndoPayloadValidator: TypeAlias = Callable[[FrozenJSONObject], None]
CompensationExecutor: TypeAlias = Callable[[Session, FrozenJSONObject], str]
_HandlerIdentity: TypeAlias = tuple[object, ...]

_UNDO_CONTRACT_VERSION = "write-undo-payload-v1"
_COMPONENT_BINDING_SENTINEL = object()
_REGISTRY_CONSTRUCTION_SEAL = object()
_PORT_BINDING_SENTINEL = object()


class CompensationConflictError(ValueError):
    """The primary write no longer has the exact state required for safe Undo."""


class _RuntimeAsdictGuard:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("runtime compensation value cannot be serialized")


_RUNTIME_ASDICT_GUARD = _RuntimeAsdictGuard()


def _require_named_module_function(value: object, field_name: str) -> Callable[..., object]:
    if not inspect.isfunction(value):
        raise TypeError(f"{field_name} must be a named module-level function")
    callback = cast(Callable[..., object], value)
    if (
        callback.__name__ == "<lambda>"
        or "<locals>" in callback.__qualname__
        or callback.__module__ != __name__
    ):
        raise TypeError(f"{field_name} must be a named module-level function")
    return callback


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class CompensationHandlerSpec(TransientToolRuntimeValue):
    ordinal: int
    undo_payload_kind: UndoPayloadKind
    compensation_operation_kind: CompensationKind
    adapter_kind: Literal["compensation"]
    result_contract: Literal["compensation_json_v1"]
    undo_contract_version: str
    handler_id: str
    validate_undo_payload: UndoPayloadValidator = field(repr=False, compare=False)
    execute: CompensationExecutor = field(repr=False, compare=False)
    _identity_seal: object = field(default=None, init=False, repr=False, compare=False)
    _serialization_guard: object = field(
        default=_RUNTIME_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        current = self._validated_identity()
        if self._identity_seal is None:
            object.__setattr__(self, "_identity_seal", current)
        elif not _identity_matches(self._identity_seal, current):
            raise ValueError("Compensation handler identity seal mismatch")

    def _validated_identity(self) -> _HandlerIdentity:
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("Compensation handler ordinal must be positive")
        if type(self.undo_payload_kind) is not UndoPayloadKind:
            raise TypeError("Compensation handler requires an exact UndoPayloadKind")
        if type(self.compensation_operation_kind) is not CompensationKind:
            raise TypeError("Compensation handler requires an exact CompensationKind")
        if self.adapter_kind != "compensation":
            raise ValueError("Compensation handler adapter kind must be compensation")
        if self.result_contract != "compensation_json_v1":
            raise ValueError("Compensation handler result contract is invalid")
        if self.undo_contract_version != _UNDO_CONTRACT_VERSION:
            raise ValueError("Compensation handler Undo contract version is invalid")
        if type(self.handler_id) is not str or not self.handler_id:
            raise ValueError("Compensation handler id must be non-empty text")
        validator = _require_named_module_function(
            self.validate_undo_payload,
            "Compensation Undo validator",
        )
        executor = _require_named_module_function(
            self.execute,
            "Compensation executor",
        )
        return (
            self.ordinal,
            self.undo_payload_kind,
            self.compensation_operation_kind,
            self.adapter_kind,
            self.result_contract,
            self.undo_contract_version,
            self.handler_id,
            validator,
            validator.__qualname__,
            executor,
            executor.__qualname__,
        )

    def _ensure_integrity(self) -> None:
        if not _identity_matches(self._identity_seal, self._validated_identity()):
            raise ValueError("Compensation handler identity seal mismatch")


def _identity_matches(sealed: object, current: _HandlerIdentity) -> bool:
    if type(sealed) is not tuple or len(sealed) != len(current):
        return False
    return all(
        expected == actual if type(expected) in {str, int} else expected is actual
        for expected, actual in zip(sealed, current)
    )


def _binding_snapshot(binding: CompensationHandlerBindingV1) -> tuple[object, ...]:
    return (
        binding.ordinal,
        binding.compensation_kind,
        binding.handler_id,
    )


def _require_frozen_json(value: object, field_name: str) -> FrozenJSONValue:
    if type(value) is MappingProxyType:
        mapping = cast(MappingProxyType[str, FrozenJSONValue], value)
        for key, item in mapping.items():
            if type(key) is not str:
                raise TypeError(f"{field_name} keys must be exact strings")
            _require_frozen_json(item, field_name)
        return mapping
    if type(value) is tuple:
        for tuple_item in cast(tuple[object, ...], value):
            _require_frozen_json(tuple_item, field_name)
        return cast(FrozenJSONValue, value)
    if value is None or type(value) in {bool, int, float, str}:
        return cast(FrozenJSONValue, value)
    raise TypeError(f"{field_name} must be a recursively immutable JSON snapshot")


def _require_object(
    value: object,
    field_name: str,
    exact_keys: tuple[str, ...],
) -> FrozenJSONObject:
    _require_frozen_json(value, field_name)
    if type(value) is not MappingProxyType:
        raise TypeError(f"{field_name} must be an immutable JSON object")
    result = cast(FrozenJSONObject, value)
    if len(result) != len(exact_keys) or set(result) != set(exact_keys):
        raise ValueError(f"{field_name} has an invalid exact shape")
    return result


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exact text")
    return value


def _require_positive_id(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    text = _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime or null") from exc
    return parsed


def _require_status_payload(undo: object) -> FrozenJSONObject:
    payload = _require_object(
        undo,
        "update-application-status Undo payload",
        ("kind", "label", "application_id", "before", "expected_after"),
    )
    if payload["kind"] != UndoPayloadKind.UPDATE_APPLICATION_STATUS.value:
        raise ValueError("Undo payload kind does not match the status handler")
    if payload["label"] != "撤销更新投递状态":
        raise ValueError("Undo payload label does not match the status handler")
    _require_positive_id(payload["application_id"], "application id")
    for name in ("before", "expected_after"):
        values = _require_object(payload[name], name, ("status", "closed_reason"))
        _require_text(values["status"], f"{name}.status")
        _require_text(values["closed_reason"], f"{name}.closed_reason")
    return payload


def _require_delete_application_payload(undo: object) -> FrozenJSONObject:
    payload = _require_object(
        undo,
        "delete-application Undo payload",
        ("kind", "label", "application_id", "expected_after"),
    )
    if payload["kind"] != UndoPayloadKind.DELETE_APPLICATION.value:
        raise ValueError("Undo payload kind does not match the application handler")
    if payload["label"] != "撤销新建投递":
        raise ValueError("Undo payload label does not match the application handler")
    _require_positive_id(payload["application_id"], "application id")
    expected = _require_object(
        payload["expected_after"],
        "expected application",
        (
            "company_name",
            "position_name",
            "job_url",
            "status",
            "source",
            "notes",
            "applied_at",
            "closed_reason",
            "updated_at",
        ),
    )
    for name in (
        "company_name",
        "position_name",
        "job_url",
        "status",
        "source",
        "notes",
        "closed_reason",
    ):
        _require_text(expected[name], f"expected application {name}")
    _require_optional_datetime(expected["applied_at"], "expected application applied_at")
    _require_optional_datetime(expected["updated_at"], "expected application updated_at")
    return payload


def _require_delete_event_payload(undo: object) -> FrozenJSONObject:
    payload = _require_object(
        undo,
        "delete-application-event Undo payload",
        ("kind", "label", "application_event_id", "expected_after"),
    )
    if payload["kind"] != UndoPayloadKind.DELETE_APPLICATION_EVENT.value:
        raise ValueError("Undo payload kind does not match the application-event handler")
    if payload["label"] != "撤销新建日程":
        raise ValueError("Undo payload label does not match the application-event handler")
    _require_positive_id(payload["application_event_id"], "application event id")
    expected = _require_object(
        payload["expected_after"],
        "expected application event",
        (
            "application_id",
            "event_type",
            "subtype",
            "tags",
            "round",
            "scheduled_at",
            "duration_minutes",
            "location",
            "notes",
            "remind_at",
            "status",
        ),
    )
    _require_positive_id(expected["application_id"], "expected event application id")
    for name in ("event_type", "subtype", "location", "notes", "status"):
        _require_text(expected[name], f"expected application event {name}")
    tags = expected["tags"]
    if type(tags) is not tuple or any(type(item) is not str for item in tags):
        raise TypeError("expected application event tags must be an immutable text array")
    for name in ("round", "duration_minutes"):
        if type(expected[name]) is not int:
            raise TypeError(f"expected application event {name} must be an integer")
    _require_optional_datetime(expected["scheduled_at"], "expected event scheduled_at")
    _require_optional_datetime(expected["remind_at"], "expected event remind_at")
    return payload


def _require_delete_note_payload(undo: object) -> FrozenJSONObject:
    payload = _require_object(
        undo,
        "delete-note Undo payload",
        ("kind", "label", "note_id", "expected_after"),
    )
    if payload["kind"] != UndoPayloadKind.DELETE_NOTE.value:
        raise ValueError("Undo payload kind does not match the note handler")
    if payload["label"] != "撤销保存复盘":
        raise ValueError("Undo payload label does not match the note handler")
    _require_positive_id(payload["note_id"], "note id")
    expected = _require_object(
        payload["expected_after"],
        "expected note",
        (
            "application_id",
            "company",
            "position",
            "round",
            "date",
            "questions",
            "self_reflection",
            "difficulty_points",
            "mood",
        ),
    )
    application_id = expected["application_id"]
    if application_id is not None:
        _require_positive_id(application_id, "expected note application id")
    for name in (
        "company",
        "position",
        "round",
        "date",
        "questions",
        "self_reflection",
        "difficulty_points",
        "mood",
    ):
        _require_text(expected[name], f"expected note {name}")
    return payload


def validate_update_application_status_undo(undo: FrozenJSONObject) -> None:
    _require_status_payload(undo)


def execute_update_application_status_undo(
    session: Session,
    undo: FrozenJSONObject,
) -> str:
    payload = _require_status_payload(undo)
    before = cast(FrozenJSONObject, payload["before"])
    expected = cast(FrozenJSONObject, payload["expected_after"])
    result = session.execute(
        update(Application)
        .where(Application.id == cast(int, payload["application_id"]))
        .where(Application.deleted_at.is_(None))
        .where(Application.status == normalize_application_status(cast(str, expected["status"])))
        .where(Application.closed_reason == cast(str, expected["closed_reason"]))
        .values(
            status=normalize_application_status(cast(str, before["status"])),
            closed_reason=cast(str, before["closed_reason"]),
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        raise CompensationConflictError("undo_conflict")
    return "已撤销最近一次 AI 写入：投递状态已恢复。"


def validate_delete_application_undo(undo: FrozenJSONObject) -> None:
    _require_delete_application_payload(undo)


def execute_delete_application_undo(session: Session, undo: FrozenJSONObject) -> str:
    payload = _require_delete_application_payload(undo)
    expected = cast(FrozenJSONObject, payload["expected_after"])
    statement = (
        update(Application)
        .where(Application.id == cast(int, payload["application_id"]))
        .where(Application.deleted_at.is_(None))
        .where(Application.company_name == expected["company_name"])
        .where(Application.position_name == expected["position_name"])
        .where(Application.job_url == expected["job_url"])
        .where(Application.status == expected["status"])
        .where(Application.source == expected["source"])
        .where(Application.notes == expected["notes"])
        .where(
            Application.applied_at
            == _require_optional_datetime(expected["applied_at"], "expected application applied_at")
        )
        .where(Application.closed_reason == expected["closed_reason"])
        .where(
            Application.updated_at
            == _require_optional_datetime(expected["updated_at"], "expected application updated_at")
        )
    )
    for dependency_model in APPLICATION_FOREIGN_KEY_MODELS:
        statement = statement.where(
            ~exists().where(dependency_model.application_id == payload["application_id"])
        )
    result = session.execute(statement.values(deleted_at=datetime.now(timezone.utc)))
    if getattr(result, "rowcount", 0) != 1:
        raise CompensationConflictError("undo_conflict")
    return "已撤销最近一次 AI 写入：新建投递已删除。"


def validate_delete_application_event_undo(undo: FrozenJSONObject) -> None:
    _require_delete_event_payload(undo)


def execute_delete_application_event_undo(
    session: Session,
    undo: FrozenJSONObject,
) -> str:
    payload = _require_delete_event_payload(undo)
    expected = cast(FrozenJSONObject, payload["expected_after"])
    tags = cast(tuple[str, ...], expected["tags"])
    statement = (
        delete(ApplicationEvent)
        .where(ApplicationEvent.id == cast(int, payload["application_event_id"]))
        .where(ApplicationEvent.application_id == expected["application_id"])
        .where(ApplicationEvent.event_type == expected["event_type"])
        .where(ApplicationEvent.subtype == expected["subtype"])
        .where(ApplicationEvent._tags == json.dumps(list(tags), ensure_ascii=False))
        .where(ApplicationEvent.round == expected["round"])
        .where(
            ApplicationEvent.scheduled_at
            == _require_optional_datetime(expected["scheduled_at"], "expected event scheduled_at")
        )
        .where(ApplicationEvent.duration_minutes == expected["duration_minutes"])
        .where(ApplicationEvent.location == expected["location"])
        .where(ApplicationEvent.notes == expected["notes"])
        .where(ApplicationEvent.status == expected["status"])
    )
    remind_at = _require_optional_datetime(expected["remind_at"], "expected event remind_at")
    statement = (
        statement.where(ApplicationEvent.remind_at.is_(None))
        if remind_at is None
        else statement.where(ApplicationEvent.remind_at == remind_at)
    )
    result = session.execute(statement)
    if getattr(result, "rowcount", 0) != 1:
        raise CompensationConflictError("undo_conflict")
    return "已撤销最近一次 AI 写入：新建日程已删除。"


def validate_delete_note_undo(undo: FrozenJSONObject) -> None:
    _require_delete_note_payload(undo)


def execute_delete_note_undo(session: Session, undo: FrozenJSONObject) -> str:
    payload = _require_delete_note_payload(undo)
    expected = cast(FrozenJSONObject, payload["expected_after"])
    statement = (
        delete(InterviewNote)
        .where(InterviewNote.id == cast(int, payload["note_id"]))
        .where(InterviewNote.company == expected["company"])
        .where(InterviewNote.position == expected["position"])
        .where(InterviewNote.round == expected["round"])
        .where(InterviewNote.date == expected["date"])
        .where(InterviewNote.questions == expected["questions"])
        .where(InterviewNote.self_reflection == expected["self_reflection"])
        .where(InterviewNote.difficulty_points == expected["difficulty_points"])
        .where(InterviewNote.mood == expected["mood"])
    )
    application_id = expected["application_id"]
    statement = (
        statement.where(InterviewNote.application_id.is_(None))
        if application_id is None
        else statement.where(InterviewNote.application_id == application_id)
    )
    visible_application = exists(
        select(Application.id)
        .where(Application.id == InterviewNote.application_id)
        .where(Application.deleted_at.is_(None))
    )
    statement = statement.where(or_(InterviewNote.application_id.is_(None), visible_application))
    result = session.execute(statement)
    if getattr(result, "rowcount", 0) != 1:
        raise CompensationConflictError("undo_conflict")
    return "已撤销最近一次 AI 写入：复盘记录已删除。"


_ORDERED_SPECS = (
    CompensationHandlerSpec(
        ordinal=1,
        undo_payload_kind=UndoPayloadKind.UPDATE_APPLICATION_STATUS,
        compensation_operation_kind=CompensationKind.UNDO_UPDATE_APPLICATION_STATUS,
        adapter_kind="compensation",
        result_contract="compensation_json_v1",
        undo_contract_version=_UNDO_CONTRACT_VERSION,
        handler_id="update_application_status_restore_handler_v1",
        validate_undo_payload=validate_update_application_status_undo,
        execute=execute_update_application_status_undo,
    ),
    CompensationHandlerSpec(
        ordinal=2,
        undo_payload_kind=UndoPayloadKind.DELETE_APPLICATION,
        compensation_operation_kind=CompensationKind.UNDO_CREATE_APPLICATION,
        adapter_kind="compensation",
        result_contract="compensation_json_v1",
        undo_contract_version=_UNDO_CONTRACT_VERSION,
        handler_id="create_application_delete_handler_v1",
        validate_undo_payload=validate_delete_application_undo,
        execute=execute_delete_application_undo,
    ),
    CompensationHandlerSpec(
        ordinal=3,
        undo_payload_kind=UndoPayloadKind.DELETE_APPLICATION_EVENT,
        compensation_operation_kind=CompensationKind.UNDO_CREATE_APPLICATION_EVENT,
        adapter_kind="compensation",
        result_contract="compensation_json_v1",
        undo_contract_version=_UNDO_CONTRACT_VERSION,
        handler_id="create_application_event_delete_handler_v1",
        validate_undo_payload=validate_delete_application_event_undo,
        execute=execute_delete_application_event_undo,
    ),
    CompensationHandlerSpec(
        ordinal=4,
        undo_payload_kind=UndoPayloadKind.DELETE_NOTE,
        compensation_operation_kind=CompensationKind.UNDO_ADD_NOTE,
        adapter_kind="compensation",
        result_contract="compensation_json_v1",
        undo_contract_version=_UNDO_CONTRACT_VERSION,
        handler_id="add_note_delete_handler_v1",
        validate_undo_payload=validate_delete_note_undo,
        execute=execute_delete_note_undo,
    ),
)


class CompensationHandlerHandle(TransientToolRuntimeValue):
    """Opaque metadata binding; it intentionally carries no execution method."""

    __slots__ = ("_binding", "_registry_token", "_integrity_seal")
    _binding: CompensationHandlerBindingV1
    _registry_token: object
    _integrity_seal: tuple[object, object]

    def __new__(
        cls,
        seal: object | None = None,
        registry_token: object | None = None,
        binding: CompensationHandlerBindingV1 | None = None,
    ) -> CompensationHandlerHandle:
        if seal is not _REGISTRY_CONSTRUCTION_SEAL:
            raise TypeError("Compensation handler handles are Registry-created")
        return object.__new__(cls)

    def __init__(
        self,
        seal: object | None = None,
        registry_token: object | None = None,
        binding: CompensationHandlerBindingV1 | None = None,
    ) -> None:
        if seal is not _REGISTRY_CONSTRUCTION_SEAL:
            raise TypeError("Compensation handler handles are Registry-created")
        if registry_token is None or type(binding) is not CompensationHandlerBindingV1:
            raise TypeError("Compensation handler handle provenance is invalid")
        object.__setattr__(self, "_registry_token", registry_token)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_integrity_seal", (registry_token, binding))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Compensation handler handles are sealed")

    def _require_registry(self, registry_token: object) -> CompensationHandlerBindingV1:
        if (
            type(self._integrity_seal) is not tuple
            or len(self._integrity_seal) != 2
            or self._integrity_seal[0] is not self._registry_token
            or self._integrity_seal[1] is not self._binding
        ):
            raise ValueError("Compensation handler handle integrity drift")
        if registry_token is not self._registry_token:
            raise ValueError("Compensation handler Registry provenance mismatch")
        return self._binding


class CompensationHandlerRegistry(TransientToolRuntimeValue):
    __slots__ = (
        "_binding_to_spec",
        "_bundle_instance_token",
        "_compensation_view",
        "_operation_port_token",
        "_operation_port_token_seal",
        "_operation_port",
        "_operation_port_seal",
        "_ordered_specs",
        "_registry_token",
        "_lock",
        "_integrity_seal",
    )
    _binding_to_spec: tuple[tuple[CompensationHandlerBindingV1, CompensationHandlerSpec], ...]
    _bundle_instance_token: BundleInstanceToken
    _compensation_view: CompensationMetadataView
    _operation_port_token: object
    _operation_port_token_seal: object
    _operation_port: object
    _operation_port_seal: object
    _ordered_specs: tuple[CompensationHandlerSpec, ...]
    _registry_token: object
    _lock: RLock
    _integrity_seal: tuple[object, ...]

    def __new__(
        cls,
        seal: object | None = None,
        ordered_specs: tuple[CompensationHandlerSpec, ...] | None = None,
        compensation_view: CompensationMetadataView | None = None,
    ) -> CompensationHandlerRegistry:
        del ordered_specs, compensation_view
        if seal is not _REGISTRY_CONSTRUCTION_SEAL:
            raise TypeError("Compensation handler registries are Components-created")
        return object.__new__(cls)

    def __init__(
        self,
        seal: object | None = None,
        ordered_specs: tuple[CompensationHandlerSpec, ...] | None = None,
        compensation_view: CompensationMetadataView | None = None,
    ) -> None:
        if seal is not _REGISTRY_CONSTRUCTION_SEAL:
            raise TypeError("Compensation handler registries are Components-created")
        if ordered_specs is None or type(compensation_view) is not CompensationMetadataView:
            raise TypeError("Compensation handler Registry construction is invalid")
        bindings = compensation_view.ordered_handler_bindings
        object.__setattr__(self, "_ordered_specs", ordered_specs)
        object.__setattr__(self, "_compensation_view", compensation_view)
        object.__setattr__(self, "_bundle_instance_token", compensation_view.bundle_instance_token)
        object.__setattr__(self, "_registry_token", object())
        object.__setattr__(self, "_operation_port_token", _PORT_BINDING_SENTINEL)
        object.__setattr__(self, "_operation_port_token_seal", _PORT_BINDING_SENTINEL)
        object.__setattr__(self, "_operation_port", _PORT_BINDING_SENTINEL)
        object.__setattr__(self, "_operation_port_seal", _PORT_BINDING_SENTINEL)
        object.__setattr__(self, "_lock", RLock())
        object.__setattr__(
            self,
            "_binding_to_spec",
            tuple(zip(bindings, ordered_specs)),
        )
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                id(ordered_specs),
                tuple(id(spec) for spec in ordered_specs),
                id(compensation_view),
                id(compensation_view.bundle_instance_token),
                id(self._registry_token),
                id(self._lock),
                id(self._binding_to_spec),
                tuple(
                    (id(binding), _binding_snapshot(binding), id(spec))
                    for binding, spec in self._binding_to_spec
                ),
            ),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Compensation handler Registry components are sealed")

    def _ensure_integrity(self) -> None:
        try:
            bindings = self._compensation_view.ordered_handler_bindings
            if (
                self._compensation_view.bundle_instance_token is not self._bundle_instance_token
                or self._operation_port_token is not self._operation_port_token_seal
                or self._operation_port is not self._operation_port_seal
                or len(bindings) != len(self._ordered_specs)
                or len(self._binding_to_spec) != len(self._ordered_specs)
            ):
                raise ValueError("Compensation Registry provenance drift")
            for expected_binding, expected_spec, pair in zip(
                bindings,
                self._ordered_specs,
                self._binding_to_spec,
            ):
                binding, spec = pair
                if binding is not expected_binding or spec is not expected_spec:
                    raise ValueError("Compensation Registry binding drift")
                binding.__post_init__()
                spec._ensure_integrity()
            current = (
                id(self._ordered_specs),
                tuple(id(spec) for spec in self._ordered_specs),
                id(self._compensation_view),
                id(self._bundle_instance_token),
                id(self._registry_token),
                id(self._lock),
                id(self._binding_to_spec),
                tuple(
                    (id(binding), _binding_snapshot(binding), id(spec))
                    for binding, spec in self._binding_to_spec
                ),
            )
            if current != self._integrity_seal:
                raise ValueError("Compensation Registry integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Compensation Registry integrity drift") from exc

    @property
    def compensation_view(self) -> CompensationMetadataView:
        self._ensure_integrity()
        return self._compensation_view

    @property
    def bundle_instance_token(self) -> BundleInstanceToken:
        self._ensure_integrity()
        return self._bundle_instance_token

    @property
    def registry_token(self) -> object:
        self._ensure_integrity()
        return self._registry_token

    def bind_operation_port(self, port: object, port_token: object) -> None:
        self._ensure_integrity()
        from offerpilot.ai.tool_runtime.metadata import ToolOperationMetadataPort

        if type(port) is not ToolOperationMetadataPort:
            raise TypeError("Compensation Registry requires an exact Operation Port")
        if type(port_token) is not object or port_token is _PORT_BINDING_SENTINEL:
            raise TypeError("Operation Port token must be opaque")
        with self._lock:
            if self._operation_port_token is not _PORT_BINDING_SENTINEL:
                raise ValueError("Compensation Registry is already bound to an Operation Port")
            object.__setattr__(self, "_operation_port", port)
            object.__setattr__(self, "_operation_port_seal", port)
            object.__setattr__(self, "_operation_port_token", port_token)
            object.__setattr__(self, "_operation_port_token_seal", port_token)

    def require_operation_port(self, port_token: object) -> None:
        self._ensure_integrity()
        if self._operation_port_token is _PORT_BINDING_SENTINEL:
            raise ValueError("Compensation Registry has no bound Operation Port")
        if port_token is not self._operation_port_token:
            raise ValueError("Compensation Operation Port provenance mismatch")

    def bind_handler(
        self,
        binding: CompensationHandlerBindingV1,
    ) -> CompensationHandlerHandle:
        self._ensure_integrity()
        if type(binding) is not CompensationHandlerBindingV1:
            raise TypeError("handler binding has the wrong runtime type")
        for issued_binding, _spec in self._binding_to_spec:
            if binding is issued_binding:
                return CompensationHandlerHandle(
                    _REGISTRY_CONSTRUCTION_SEAL,
                    self._registry_token,
                    issued_binding,
                )
        raise ValueError("Compensation handler binding provenance mismatch")

    def require_handler_handle(
        self,
        handler_handle: CompensationHandlerHandle,
    ) -> CompensationHandlerBindingV1:
        self._ensure_integrity()
        if type(handler_handle) is not CompensationHandlerHandle:
            raise TypeError("Compensation handler handle has the wrong runtime type")
        binding = handler_handle._require_registry(self._registry_token)
        if not any(binding is issued for issued, _spec in self._binding_to_spec):
            raise ValueError("Compensation handler binding provenance mismatch")
        return binding

    def resolve(self, compensation_handle: object) -> CompensationHandlerSpec:
        from offerpilot.ai.tool_runtime.metadata import CompensationHandle

        self._ensure_integrity()
        if type(compensation_handle) is not CompensationHandle:
            raise TypeError("Compensation Registry requires an exact CompensationHandle")
        require_registry = getattr(compensation_handle, "_require_registry", None)
        if not callable(require_registry):
            raise TypeError("Compensation handle has no Registry proof")
        proof = require_registry(self._registry_token)
        if type(proof) is not tuple or len(proof) != 2:
            raise TypeError("Compensation handle Registry proof has an invalid shape")
        port_token, handler_handle = proof
        self.require_operation_port(port_token)
        require_active = getattr(self._operation_port, "require_active_compensation", None)
        if not callable(require_active):
            raise ValueError("Compensation Operation Port validator is unavailable")
        require_active(compensation_handle)
        binding = self.require_handler_handle(handler_handle)
        for issued_binding, spec in self._binding_to_spec:
            if binding is issued_binding:
                spec._ensure_integrity()
                return spec
        raise ValueError("Compensation handle binding provenance mismatch")


class CompensationHandlerComponents(TransientToolRuntimeValue):
    __slots__ = (
        "_binding_state",
        "_binding_state_seal",
        "_lock",
        "_ordered_specs",
        "_integrity_seal",
    )
    _binding_state: object
    _binding_state_seal: object
    _lock: RLock
    _ordered_specs: tuple[CompensationHandlerSpec, ...]
    _integrity_seal: tuple[object, ...]

    def __init__(self, ordered_specs: tuple[CompensationHandlerSpec, ...]) -> None:
        if type(ordered_specs) is not tuple:
            raise TypeError("Compensation handler specs must be an exact tuple")
        if len(ordered_specs) != len(_ORDERED_SPECS) or any(
            actual is not expected for actual, expected in zip(ordered_specs, _ORDERED_SPECS)
        ):
            raise ValueError("Compensation handler specs must be the exact sealed four")
        for spec in ordered_specs:
            spec._ensure_integrity()
        object.__setattr__(self, "_ordered_specs", ordered_specs)
        object.__setattr__(self, "_binding_state", _COMPONENT_BINDING_SENTINEL)
        object.__setattr__(self, "_binding_state_seal", _COMPONENT_BINDING_SENTINEL)
        object.__setattr__(self, "_lock", RLock())
        object.__setattr__(
            self,
            "_integrity_seal",
            (
                id(ordered_specs),
                tuple(id(spec) for spec in ordered_specs),
                id(self._lock),
            ),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Compensation handler Components are sealed")

    def _ensure_integrity(self) -> None:
        try:
            if self._binding_state is not self._binding_state_seal:
                raise ValueError("Compensation handler Components binding drift")
            for actual, expected in zip(self._ordered_specs, _ORDERED_SPECS):
                if actual is not expected:
                    raise ValueError("Compensation handler Components spec drift")
                actual._ensure_integrity()
            current = (
                id(self._ordered_specs),
                tuple(id(spec) for spec in self._ordered_specs),
                id(self._lock),
            )
            if current != self._integrity_seal:
                raise ValueError("Compensation handler Components integrity drift")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Compensation handler Components integrity drift") from exc

    def metadata_projection(self) -> dict[str, object]:
        self._ensure_integrity()
        return {
            "ordered_handler_bindings": [
                {
                    "ordinal": spec.ordinal,
                    "compensation_kind": spec.compensation_operation_kind.value,
                    "handler_id": spec.handler_id,
                }
                for spec in self._ordered_specs
            ]
        }

    def bind(self, compensation_view: CompensationMetadataView) -> CompensationHandlerRegistry:
        self._ensure_integrity()
        if type(compensation_view) is not CompensationMetadataView:
            raise TypeError("Components require an exact CompensationMetadataView")
        bindings = compensation_view.ordered_handler_bindings
        if len(bindings) != len(self._ordered_specs):
            raise ValueError("Compensation metadata has the wrong handler cardinality")
        for binding, spec in zip(bindings, self._ordered_specs):
            spec._ensure_integrity()
            if (
                binding.ordinal != spec.ordinal
                or binding.compensation_kind != spec.compensation_operation_kind.value
                or binding.handler_id != spec.handler_id
            ):
                raise ValueError("Compensation metadata does not exactly bind its handler")
        with self._lock:
            if self._binding_state is not _COMPONENT_BINDING_SENTINEL:
                raise ValueError("Compensation handler Components are already Bundle-bound")
            registry = CompensationHandlerRegistry(
                _REGISTRY_CONSTRUCTION_SEAL,
                self._ordered_specs,
                compensation_view,
            )
            object.__setattr__(self, "_binding_state", registry)
            object.__setattr__(self, "_binding_state_seal", registry)
            return registry


def prepare_compensation_handler_components() -> CompensationHandlerComponents:
    """Return static handler components ready for one exact Bundle binding."""

    return CompensationHandlerComponents(_ORDERED_SPECS)


__all__ = [
    "CompensationConflictError",
    "CompensationHandlerComponents",
    "CompensationHandlerHandle",
    "CompensationHandlerRegistry",
    "CompensationHandlerSpec",
    "prepare_compensation_handler_components",
]
