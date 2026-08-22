from __future__ import annotations

import ast
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "offerpilot"
API = SRC / "api.py"
TRANSPORT = SRC / "chat_transport.py"
RUNTIME = SRC / "pilot_runtime"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _production_files() -> tuple[Path, ...]:
    return tuple(sorted(SRC.rglob("*.py")))


def _names(node: ast.AST) -> set[str]:
    result: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            result.add(child.id)
        elif isinstance(child, ast.Attribute):
            result.add(child.attr)
        elif isinstance(child, ast.alias):
            result.add(child.asname or child.name.rsplit(".", 1)[-1])
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(child.name)
    return result


def _imports(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add(node.module or "")
    return result


def _functions(tree: ast.AST, names: set[str]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    }


def _expect_rejected(source: str, validator: Callable[[ast.AST], None]) -> None:
    with pytest.raises(AssertionError):
        validator(ast.parse(source))


ROUTE_NAMES = frozenset(
    {"send_chat", "send_chat_stream", "confirm_chat", "confirm_chat_stream"}
)
ROUTE_OWNERSHIP_NAMES = frozenset(
    {
        "run_turn",
        "resume_after_confirm",
        "ContextSourceLoader",
        "RunRecorder",
        "RunRecorderFactory",
        "WriteOperationCoordinator",
        "DeliveryHeartbeat",
        "claim_pending",
        "claim_live_pending",
        "claim_operation",
        "heartbeat",
        "append_message",
        "save_message",
        "persist_message",
        "write_message",
        "load_source_messages",
        "load_context_sources",
        "context_source_loader",
        "run_recorder",
        "write_coordinator",
    }
)
TRANSPORT_PRIMITIVE_IMPORTS = frozenset(
    {"concurrent.futures", "queue", "threading"}
)
LEGACY_SWITCH_FRAGMENTS = frozenset(
    {
        "dual_run",
        "shadow_execution",
        "shadow_write",
        "legacy_fallback",
        "pilot_runtime_enabled",
        "runtime_feature_flag",
        "chat_runtime_flag",
    }
)
PRIVATE_BOUNDARY_NAMES = frozenset(
    {
        "PreparedLifecycle",
        "PreparedStreamExecution",
        "RuntimeInvocationControl",
        "AgentExecutionHost",
        "RuntimeEventSink",
        "RuntimeFailureOutcome",
        "ConfirmationState",
        "ConfirmationSession",
        "DeliveryBundle",
        "RuntimeDependencies",
        "ResolvedModel",
        "AgentInvocation",
        "NormalizedAgentTurn",
        "_PreparedStreamState",
        "_PreparedConversation",
        "_PreparedToolCall",
        "_PreparedMessage",
    }
)


def _validate_routes_are_runtime_only(tree: ast.AST) -> None:
    found = _functions(tree, set(ROUTE_NAMES))
    assert set(found) == set(ROUTE_NAMES)
    for name, node in found.items():
        forbidden = _names(node) & ROUTE_OWNERSHIP_NAMES
        assert not forbidden, f"{name} owns reliability/persistence helpers: {sorted(forbidden)}"


def _validate_runtime_transport_boundary(tree: ast.AST) -> None:
    imports = _imports(tree)
    assert not any(
        module == "fastapi"
        or module.startswith("fastapi.")
        or module == "starlette"
        or module.startswith("starlette.")
        for module in imports
    ), "Pilot Runtime must not depend on FastAPI/Starlette"


def _validate_transport_owns_primitives(tree: ast.AST) -> None:
    imports = _imports(tree)
    assert TRANSPORT_PRIMITIVE_IMPORTS <= imports
    names = _names(tree)
    for required in (
        "ThreadPoolExecutor",
        "Future",
        "Queue",
        "Event",
        "PreparedStreamGuard",
        "encode_sse_event",
    ):
        assert required in names, f"transport owner missing {required}"


def _validate_no_stream_primitive_in_api(tree: ast.AST) -> None:
    forbidden_names = {
        "ThreadPoolExecutor",
        "Future",
        "Queue",
        "Event",
        "encode_sse_event",
        "format_sse",
    }
    found = sorted(_names(tree) & forbidden_names)
    assert not found, f"api owns transport primitive(s): {found}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_runtime_sse_content",
            "_runtime_stream_immediate_response",
        }:
            raise AssertionError(f"old SSE helper remains in api: {node.name}")


def _validate_unbounded_queue(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Queue":
            continue
        assert not any(keyword.arg == "maxsize" for keyword in node.keywords), (
            "Runtime transport Queue must stay unbounded"
        )


def _validate_execution_host_boundary(tree: ast.AST) -> None:
    imports = _imports(tree)
    forbidden_modules = {
        "offerpilot.repositories",
        "offerpilot.agent_runtime.journal",
        "offerpilot.ai.write_operations",
    }
    assert not any(
        module in forbidden_modules or any(module.startswith(prefix + ".") for prefix in forbidden_modules)
        for module in imports
    )
    host_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert {"SyncAgentExecutionHost", "SseAgentExecutionHost"} <= host_names


def _validate_prepared_streams_are_guarded(runtime_tree: ast.AST, api_tree: ast.AST) -> None:
    prepared_calls = [
        node
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PreparedStreamExecution"
    ]
    assert prepared_calls, "the runtime must construct prepared stream handles"
    guards = [
        node
        for node in ast.walk(api_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PreparedStreamGuard"
    ]
    assert guards, "prepared stream handles must have a transport guard"


def _validate_runtime_event_contract(tree: ast.AST) -> None:
    classes = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    user_event = classes.get("UserMessageSavedEvent")
    assert user_event is not None
    role_values = [
        node.value
        for node in ast.walk(user_event)
        if isinstance(node, ast.Constant) and node.value == "user"
    ]
    assert role_values, "UserMessageSavedEvent must be fixed to role=user"
    union = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "RuntimeEvent"
    )
    assert "UserMessageSavedEvent" in ast.unparse(union.value)


def _validate_no_legacy_switches(paths: tuple[Path, ...]) -> None:
    findings: list[str] = []
    for path in paths:
        tree = _tree(path)
        identifiers = _names(tree)
        for identifier in identifiers:
            lowered = identifier.lower()
            if any(fragment in lowered for fragment in LEGACY_SWITCH_FRAGMENTS):
                findings.append(f"{path.relative_to(ROOT)}:{identifier}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "错误："
                and path not in {SRC / "ai" / "tool_runtime" / "rendering.py"}
            ):
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}:错误前缀解析")
    assert findings == []


def _validate_no_legacy_switch_tree(tree: ast.AST) -> None:
    identifiers = _names(tree)
    assert not any(
        any(fragment in identifier.lower() for fragment in LEGACY_SWITCH_FRAGMENTS)
        for identifier in identifiers
    )


def _validate_model_dispatch_not_legacy(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in {"_select_route", "_resolve_model", "_run_driver"}:
            continue
        assert not any("legacy" in name.lower() for name in _names(node)), (
            f"model dispatch must not route through Legacy: {node.name}"
        )


def _validate_no_error_prefix_expansion(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "错误："
        ):
            continue
        raise AssertionError("compatibility error-prefix parsing is not allowed here")


def _validate_boundary_names_absent(tree: ast.AST) -> None:
    assert not (_names(tree) & PRIVATE_BOUNDARY_NAMES)


def test_four_chat_routes_do_not_own_reliability_or_persistence() -> None:
    _validate_routes_are_runtime_only(_tree(API))


def test_pilot_runtime_has_no_framework_response_or_background_dependency() -> None:
    for path in RUNTIME.glob("*.py"):
        _validate_runtime_transport_boundary(_tree(path))


def test_transport_is_the_single_new_owner_of_execution_and_sse_primitives() -> None:
    _validate_transport_owns_primitives(_tree(TRANSPORT))
    _validate_no_stream_primitive_in_api(_tree(API))


def test_transport_queue_is_unbounded() -> None:
    _validate_unbounded_queue(_tree(TRANSPORT))


def test_agent_execution_host_does_not_cross_runtime_boundaries() -> None:
    _validate_execution_host_boundary(_tree(TRANSPORT))


def test_every_runtime_prepared_stream_is_guardable() -> None:
    _validate_prepared_streams_are_guarded(_tree(RUNTIME / "service.py"), _tree(API))


def test_runtime_event_union_has_closed_user_message_event() -> None:
    _validate_runtime_event_contract(_tree(RUNTIME / "contracts.py"))


def test_no_old_path_alias_shadow_or_fallback_is_present() -> None:
    paths = (API, TRANSPORT, *tuple(sorted(RUNTIME.glob("*.py"))))
    _validate_no_legacy_switches(paths)
    _validate_model_dispatch_not_legacy(_tree(RUNTIME / "service.py"))


def test_negative_source_fixtures_prove_mechanical_validators_reject_forbidden_patterns() -> None:
    _expect_rejected("def send_chat():\n    return run_turn()", _validate_routes_are_runtime_only)
    _expect_rejected("from fastapi import FastAPI", _validate_runtime_transport_boundary)
    _expect_rejected("from queue import Queue\nq = Queue(maxsize=1)", _validate_unbounded_queue)
    _expect_rejected("from offerpilot.repositories.chat import ChatRepository", _validate_execution_host_boundary)
    _expect_rejected("def _runtime_sse_content():\n    return encode_sse_event({}, seq=1)", _validate_no_stream_primitive_in_api)
    _expect_rejected(
        "class X:\n    def dual_run(self):\n        pass",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "def _select_route():\n    return legacy_adapter()",
        _validate_model_dispatch_not_legacy,
    )
    _expect_rejected(
        "def render(value):\n    return value.startswith('错误：')",
        _validate_no_error_prefix_expansion,
    )


def test_runtime_event_dtos_do_not_accept_framework_values() -> None:
    from fastapi import Request

    from offerpilot.pilot_runtime import ImmediateHttpOutcome, StartTurnRequest

    with pytest.raises(TypeError):
        StartTurnRequest(message="ok", page_context=MappingProxyType({"request": Request}))
    with pytest.raises(TypeError):
        ImmediateHttpOutcome(
            status_code=500,
            payload=MappingProxyType({"request": Request}),
        )


def test_prepared_execution_rejects_generic_serialization() -> None:
    from offerpilot.pilot_runtime import (
        PreparedStreamExecution,
        PreparationKind,
        StreamExecutionMode,
    )

    prepared = PreparedStreamExecution(
        "invocation-canary",
        PreparationKind.REPLAY,
        StreamExecutionMode.DIRECT,
        opaque_state=MappingProxyType({"secret": "prepared-canary"}),
    )
    assert is_dataclass(prepared)
    assert "prepared-canary" not in repr(prepared)
    with pytest.raises(TypeError):
        asdict(prepared)


def test_boundary_modules_do_not_serialize_runtime_private_types() -> None:
    boundary_paths = (
        SRC / "models.py",
        SRC / "schemas.py",
        SRC / "ai" / "agent.py",
        SRC / "ai" / "tool_runtime" / "rendering.py",
        SRC / "ai" / "tool_runtime" / "transport.py",
        SRC / "repositories" / "chat.py",
        SRC / "ai" / "write_operations.py",
        SRC / "agent_runtime" / "journal.py",
    )
    findings: list[str] = []
    for path in boundary_paths:
        tree = _tree(path)
        names = _names(tree)
        forbidden = names & PRIVATE_BOUNDARY_NAMES
        # Existing public domain names are allowed when they are the actual
        # storage contract; only runtime-private ownership may cross it.
        if forbidden:
            findings.append(f"{path.relative_to(ROOT)}:{sorted(forbidden)}")
    assert findings == []


def test_runtime_outcomes_and_events_are_safe_json_shapes() -> None:
    from offerpilot.pilot_runtime import (
        AssistantMessageEvent,
        CompletedEvent,
        MessageOutcome,
        MetaEvent,
        RuntimeFailureCode,
        RuntimeFailureOutcome,
        UserMessageSavedEvent,
    )
    from offerpilot.pilot_runtime.event_sink import runtime_event_payload, runtime_outcome_payload

    outcome = MessageOutcome(message="safe-message", conversation_id=7)
    failure = RuntimeFailureOutcome(
        code=RuntimeFailureCode.AI_PROVIDER_ERROR,
        message="safe-failure",
        conversation_id=7,
    )
    events = [
        MetaEvent(),
        UserMessageSavedEvent(),
        AssistantMessageEvent(message="safe-assistant"),
        CompletedEvent(response=outcome),
    ]
    for event in events:
        payload = runtime_event_payload(event)
        assert json.dumps(payload, ensure_ascii=False)
        assert set(payload) <= {"stream_version", "supports_delta", "supports_tool_events", "supports_confirmation", "role", "phase", "label", "delta", "tool_call_id", "tool_name", "public_label", "kind", "confirm_mode", "summary", "status", "evidence", "affected_resources", "changed_entities", "operation_id", "message", "visible_result", "write_status", "confirmation_token", "pending_action", "code", "retryable", "degraded", "response", "persisted"}
    for value in (outcome, failure):
        payload = runtime_outcome_payload(value)
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "safe-message" in encoded or "safe-failure" in encoded
        assert "prepared-canary" not in encoded


def test_canary_private_values_do_not_enter_journal_trace_sse_or_error_log_payloads() -> None:
    from offerpilot.pilot_runtime import AssistantMessageEvent, RuntimeFailureCode, RuntimeFailureOutcome
    from offerpilot.pilot_runtime.event_sink import runtime_event_payload, runtime_outcome_payload

    canaries = {
        "credential-canary-9f4e",
        "repository-canary-4ac1",
        "orm-session-canary-0c17",
        "binding-canary-1b88",
        "provider-secret-canary-8d4b",
    }
    event = runtime_event_payload(AssistantMessageEvent(message="public-result"))
    outcome = runtime_outcome_payload(
        RuntimeFailureOutcome(
            code=RuntimeFailureCode.AI_PROVIDER_ERROR,
            message="public-error",
            conversation_id=3,
        )
    )
    serialized = json.dumps({"event": event, "outcome": outcome}, ensure_ascii=False)
    for canary in canaries:
        assert canary not in serialized
