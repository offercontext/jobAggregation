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


def _binding_aliases(tree: ast.AST) -> dict[str, str]:
    """Resolve import and simple assignment aliases to their source symbol."""

    aliases: dict[str, str] = {}

    def qualified(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = qualified(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    for _ in range(3):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".", 1)[0]
                    source = alias.name if alias.asname else alias.name.split(".", 1)[0]
                    if aliases.get(local) != source:
                        aliases[local] = source
                        changed = True
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    source = f"{module}.{alias.name}" if module else alias.name
                    if aliases.get(local) != source:
                        aliases[local] = source
                        changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                source = qualified(value)
                if source is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if aliases.get(target.id) != source:
                        aliases[target.id] = source
                        changed = True
        if not changed:
            break
    return aliases


def _qualified_symbol(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_symbol(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _resolved_names(tree: ast.AST, aliases: dict[str, str] | None = None) -> set[str]:
    if aliases is None:
        aliases = _binding_aliases(tree)
    result = _names(tree)
    for local, source in aliases.items():
        if local in result:
            result.add(source.rsplit(".", 1)[-1])
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Name, ast.Attribute)):
            source = _qualified_symbol(node, aliases)
            if source:
                result.add(source.rsplit(".", 1)[-1])
    return result


def _call_terminal(node: ast.Call, aliases: dict[str, str]) -> str | None:
    source = _qualified_symbol(node.func, aliases)
    return source.rsplit(".", 1)[-1] if source else None


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
ROUTE_TRANSPORT_NAMES = frozenset(
    {
        "SyncAgentExecutionHost",
        "SseAgentExecutionHost",
        "PreparedStreamGuard",
        "InMemoryRuntimeInvocationControl",
        "ThreadPoolExecutor",
        "Future",
        "Queue",
        "Event",
        "runtime_sse_content",
        "runtime_sse_envelope",
        "prepared_stream_metadata",
        "build_guarded_streaming_response",
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
        "ImmediateHttpOutcome",
        "MessageOutcome",
        "ConfirmationRequiredOutcome",
        "OperationPendingOutcome",
        "OperationReplayOutcome",
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
    aliases = _binding_aliases(tree)
    for name, node in found.items():
        forbidden = _resolved_names(node, aliases) & (ROUTE_OWNERSHIP_NAMES | ROUTE_TRANSPORT_NAMES)
        assert not forbidden, f"{name} owns reliability/persistence helpers: {sorted(forbidden)}"
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            terminal = _call_terminal(child, aliases)
            if terminal in ROUTE_OWNERSHIP_NAMES | ROUTE_TRANSPORT_NAMES:
                raise AssertionError(f"{name} directly constructs/owns {terminal}")


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
    names = _resolved_names(tree)
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
        "SyncAgentExecutionHost",
        "SseAgentExecutionHost",
        "PreparedStreamGuard",
        "InMemoryRuntimeInvocationControl",
        "runtime_sse_content",
        "runtime_sse_envelope",
        "prepared_stream_metadata",
        "build_guarded_streaming_response",
    }
    found = sorted(_resolved_names(tree) & forbidden_names)
    assert not found, f"api owns transport primitive(s): {found}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_runtime_sse_content",
            "_runtime_stream_immediate_response",
        }:
            raise AssertionError(f"old SSE helper remains in api: {node.name}")


def _validate_unbounded_queue(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_terminal(node, aliases) != "Queue":
            continue
        assert not node.args and not any(keyword.arg == "maxsize" for keyword in node.keywords), (
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
    forbidden_names = {
        "Pending",
        "PendingActionPayload",
        "Ledger",
        "WriteOperationCoordinator",
        "RunRecorder",
        "AgentRunRepository",
    }
    forbidden_attrs = {
        "pending",
        "ledger",
        "journal",
        "repository",
        "repositories",
        "repos",
        "session",
        "write_coordinator",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("AgentExecutionHost"):
            continue
        names = _resolved_names(node, _binding_aliases(tree))
        assert not names.intersection(forbidden_names), (
            f"{node.name} crosses persistence boundary: "
            f"{sorted(names.intersection(forbidden_names))}"
        )
        attrs = {
            child.attr.lower()
            for child in ast.walk(node)
            if isinstance(child, ast.Attribute)
        }
        assert not attrs.intersection(forbidden_attrs), (
            f"{node.name} holds forbidden boundary attributes: "
            f"{sorted(attrs.intersection(forbidden_attrs))}"
        )


def _validate_prepared_streams_are_guarded(runtime_tree: ast.AST, api_tree: ast.AST) -> None:
    runtime_aliases = _binding_aliases(runtime_tree)
    prepared_calls = [
        node
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call)
        and _call_terminal(node, runtime_aliases) == "PreparedStreamExecution"
    ]
    assert prepared_calls, "the runtime must construct prepared stream handles"
    aliases = _binding_aliases(api_tree)
    guarded_functions = 0
    for function in ast.walk(api_tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        prepared_targets: list[str] = []
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            if _call_terminal(node.value, aliases) != "prepare_stream":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    prepared_targets.append(target.id)
        if not prepared_targets:
            continue
        assert len(prepared_targets) == len(set(prepared_targets)), (
            "a prepared handle cannot be rebound before its guard"
        )
        guards_by_target: dict[str, int] = {target: 0 for target in prepared_targets}
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or _call_terminal(node, aliases) != "PreparedStreamGuard":
                continue
            prepared_arg = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "prepared"),
                None,
            )
            if not isinstance(prepared_arg, ast.Name) or prepared_arg.id not in guards_by_target:
                raise AssertionError("PreparedStreamGuard must consume a prepared handle")
            guards_by_target[prepared_arg.id] += 1
        assert all(count == 1 for count in guards_by_target.values()), (
            "each prepared stream handle must have exactly one data-flow guard"
        )
        guarded_functions += 1
    assert guarded_functions, "prepared stream handles must have a transport guard"


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
        identifiers = _resolved_names(tree)
        for identifier in identifiers:
            lowered = identifier.lower()
            if any(fragment in lowered for fragment in LEGACY_SWITCH_FRAGMENTS):
                findings.append(f"{path.relative_to(ROOT)}:{identifier}")
        for node in ast.walk(tree):
            if _is_error_prefix_call(node, tree) and path not in {
                SRC / "ai" / "tool_runtime" / "rendering.py"
            }:
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}:错误前缀解析")
    assert findings == []


def _validate_no_legacy_switch_tree(tree: ast.AST) -> None:
    identifiers = _resolved_names(tree)
    assert not any(
        any(fragment in identifier.lower() for fragment in LEGACY_SWITCH_FRAGMENTS)
        for identifier in identifiers
    )


def _validate_model_dispatch_not_legacy(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in {"_select_route", "_resolve_model", "_run_driver"}:
            continue
        assert not any("legacy" in name.lower() for name in _resolved_names(node, aliases)), (
            f"model dispatch must not route through Legacy: {node.name}"
        )


def _validate_no_error_prefix_expansion(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if _is_error_prefix_call(node, tree):
            raise AssertionError("compatibility error-prefix parsing is not allowed here")


def _error_prefix_aliases(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or value.value != "错误：":
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        result.update(target.id for target in targets if isinstance(target, ast.Name))
    return result


def _is_error_prefix_call(node: ast.AST, tree: ast.AST) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        and node.args
    ):
        return False
    prefix = node.args[0]
    return (
        isinstance(prefix, ast.Constant)
        and prefix.value == "错误："
    ) or (isinstance(prefix, ast.Name) and prefix.id in _error_prefix_aliases(tree))


def _validate_boundary_names_absent(tree: ast.AST) -> None:
    assert not (_names(tree) & PRIVATE_BOUNDARY_NAMES)


def _validate_no_generic_asdict_boundary(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    forbidden = PRIVATE_BOUNDARY_NAMES | {
        "MessageOutcome",
        "ConfirmationRequiredOutcome",
        "OperationPendingOutcome",
        "OperationReplayOutcome",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_terminal(node, aliases) != "asdict":
            continue
        assert not (_resolved_names(node, aliases) & forbidden), (
            "runtime-private DTOs/outcomes must use explicit projections, not asdict"
        )


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
    _validate_prepared_streams_are_guarded(_tree(RUNTIME / "service.py"), _tree(TRANSPORT))


def test_runtime_event_union_has_closed_user_message_event() -> None:
    _validate_runtime_event_contract(_tree(RUNTIME / "contracts.py"))


def test_no_old_path_alias_shadow_or_fallback_is_present() -> None:
    paths = (API, TRANSPORT, *tuple(sorted(RUNTIME.glob("*.py"))))
    _validate_no_legacy_switches(paths)
    _validate_model_dispatch_not_legacy(_tree(RUNTIME / "service.py"))


def test_negative_source_fixtures_prove_mechanical_validators_reject_forbidden_patterns() -> None:
    _expect_rejected(
        "def send_chat():\n    return run_turn()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "from offerpilot.legacy import run_turn as execute\n"
        "def send_chat():\n    return execute()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "from offerpilot.chat_transport import SyncAgentExecutionHost as Host\n"
        "def send_chat():\n    return Host()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected("from fastapi import FastAPI", _validate_runtime_transport_boundary)
    _expect_rejected("from queue import Queue\nq = Queue(maxsize=1)", _validate_unbounded_queue)
    _expect_rejected("import queue\nq = queue.Queue(1)", _validate_unbounded_queue)
    _expect_rejected("from queue import Queue as Q\nq = Q(1)", _validate_unbounded_queue)
    _expect_rejected("from queue import Queue as Q\nq = Q(maxsize=1)", _validate_unbounded_queue)
    _expect_rejected(
        "from concurrent.futures import ThreadPoolExecutor as Pool, Future as F\n"
        "from queue import Queue as Q\nfrom threading import Event as Stop\n"
        "Pool(); F(); Q(); Stop()",
        _validate_no_stream_primitive_in_api,
    )
    _expect_rejected(
        "from offerpilot.chat_transport import encode_sse_event as Emit\nEmit({}, seq=1)",
        _validate_no_stream_primitive_in_api,
    )
    _expect_rejected("from offerpilot.repositories.chat import ChatRepository", _validate_execution_host_boundary)
    _expect_rejected(
        "class SyncAgentExecutionHost:\n"
        "    def __init__(self, pending):\n        self.pending = pending\n",
        _validate_execution_host_boundary,
    )
    _expect_rejected(
        "from offerpilot.models import Pending as P\n"
        "class SseAgentExecutionHost:\n"
        "    def __init__(self):\n        self.pending_store = P\n",
        _validate_execution_host_boundary,
    )
    _expect_rejected("def _runtime_sse_content():\n    return encode_sse_event({}, seq=1)", _validate_no_stream_primitive_in_api)
    _expect_rejected(
        "class X:\n    def dual_run(self):\n        pass",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "from old_path import dual_run as execute\nexecute()",
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
    _expect_rejected(
        "ERROR_PREFIX = '错误：'\n"
        "def render(value):\n    return value.startswith(ERROR_PREFIX)",
        _validate_no_error_prefix_expansion,
    )
    _expect_rejected(
        "from dataclasses import asdict\n"
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "asdict(RuntimeFailureOutcome(...))",
        _validate_no_generic_asdict_boundary,
    )
    _expect_rejected(
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "def send_chat_stream(runtime):\n"
        "    first = runtime.prepare_stream()\n"
        "    second = runtime.prepare_stream()\n"
        "    return Guard(prepared=first)",
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse("PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"),
            tree,
        ),
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
        _validate_boundary_names_absent(tree)
        _validate_no_generic_asdict_boundary(tree)
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


def test_canary_private_values_do_not_enter_journal_trace_sse_or_error_log_payloads(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from uuid import uuid4

    from offerpilot.agent_runtime.events import JournalEventValidationError, prepare_event
    from offerpilot.agent_runtime.journal import (
        EventInput,
        RunRecorderFactory,
        TerminalDisposition,
    )
    from offerpilot.agent_runtime.keyring import JournalKeyDomain
    from offerpilot.agent_runtime.trace import reconstruct_agent_run
    from offerpilot.ai.agent import PendingAction
    from offerpilot.ai.write_operations import LedgerKeyDomain, WriteOperationRepository
    from offerpilot.chat_transport import (
        SyncAgentExecutionHost,
        encode_sse_event,
        prepared_stream_metadata,
        runtime_sse_envelope,
    )
    from offerpilot.db import init_database
    from offerpilot.diagnostics import append_log_entry, read_recent_log_entries
    from offerpilot.models import ChatMessage, Conversation
    from offerpilot.pilot_runtime import (
        CompletedEvent,
        MessageOutcome,
        PreparationKind,
        PreparedLifecycle,
        PreparedStreamExecution,
        RuntimeFailureCode,
        RuntimeFailureOutcome,
        StartTurnRequest,
        StreamExecutionMode,
        ToolCallEvent,
        ToolResultEvent,
    )
    from offerpilot.pilot_runtime.errors import RuntimeTransportAborted
    from offerpilot.pilot_runtime.event_sink import (
        CallableRuntimeEventSink,
        InMemoryRuntimeInvocationControl,
        runtime_event_payload,
        runtime_outcome_payload,
    )
    from offerpilot.repositories.agent_runs import AgentRunRepository, StartRunCommand

    sentinel = "pilot-runtime-private-canary-6f4b"

    class _PrivateCanary:
        def __repr__(self) -> str:
            return sentinel

    nested_private = MappingProxyType(
        {"nested": MappingProxyType({"internal": _PrivateCanary()})}
    )
    # Tool event and outcome constructors reject an ORM/framework/provider value
    # before it can reach any public serializer.
    with pytest.raises(TypeError):
        ToolCallEvent(
            tool_call_id="call-private",
            tool_name="get_offer",
            args_summary=nested_private,
        )
    with pytest.raises(TypeError):
        ToolResultEvent(
            tool_call_id="call-private",
            tool_name="get_offer",
            status="success",
            summary="public result",
            evidence=(nested_private,),
        )
    with pytest.raises(TypeError):
        MessageOutcome(message="public outcome", undo=nested_private)
    with pytest.raises(TypeError):
        RuntimeFailureOutcome(
            code=RuntimeFailureCode.AI_PROVIDER_ERROR,
            message="public failure",
            pending_action=_PrivateCanary(),  # type: ignore[arg-type]
        )

    # Every internal category is deliberately kept inside the opaque prepared
    # state.  The transport may read only its public envelope attributes.
    session_factory = init_database(tmp_path / "privacy-boundary.db")
    session = session_factory()
    journal_repository = AgentRunRepository(session_factory)
    ledger_repository = WriteOperationRepository(
        session_factory,
        LedgerKeyDomain("ledger-key", b"l" * 32),
    )
    private_state = SimpleNamespace(
        conversation=SimpleNamespace(
            conversation_id=7,
            context_type="workspace",
            context_ref="",
            mode="general",
        ),
        conversation_id=7,
        internal={
            "lifecycle": PreparedLifecycle(),
            "invocation_control": InMemoryRuntimeInvocationControl(),
            "execution_host": SyncAgentExecutionHost(timeout_seconds=1),
            "event_sink": CallableRuntimeEventSink(lambda _event: None),
            "exception": RuntimeTransportAborted(sentinel),
            "credential": JournalKeyDomain(sentinel, sentinel.encode()),
            "repository": journal_repository,
            "ledger": ledger_repository,
            "orm": ChatMessage(content=sentinel, role="assistant", conversation_id=7),
            "session": session,
            "binding_audit": SimpleNamespace(provider_secret=sentinel),
            "outcome": RuntimeFailureOutcome(
                code=RuntimeFailureCode.AI_PROVIDER_ERROR,
                message="public failure",
                conversation_id=7,
            ),
            "pending": PendingAction(
                tool_call_id="call-private",
                tool_name="get_offer",
                args=sentinel,
                human="public pending",
            ),
        },
    )
    prepared = PreparedStreamExecution(
        invocation_id="privacy-prepared",
        preparation_kind=PreparationKind.REPLAY,
        execution_mode=StreamExecutionMode.DIRECT,
        opaque_state=private_state,
    )
    request = StartTurnRequest(message="public request")
    try:
        assert sentinel not in repr(prepared)
        with pytest.raises(TypeError):
            asdict(prepared)
        metadata = prepared_stream_metadata(prepared, request)
        assert metadata == (7, "workspace", "", "general")
        assert sentinel not in json.dumps(metadata)

        public_args = MappingProxyType(
            {"filters": MappingProxyType({"status": "open", "count": 1})}
        )
        public_evidence = (MappingProxyType({"id": "offer-1", "kind": "offer"}),)
        tool_call = ToolCallEvent(
            tool_call_id="call-public",
            tool_name="get_offer",
            summary="public tool call",
            args_summary=public_args,
        )
        tool_result = ToolResultEvent(
            tool_call_id="call-public",
            tool_name="get_offer",
            status="success",
            summary="public tool result",
            evidence=public_evidence,
            affected_resources=public_evidence,
        )
        outcome = MessageOutcome(message="public outcome", conversation_id=7)
        event_payload = {
            "tool_call": runtime_event_payload(tool_call),
            "tool_result": runtime_event_payload(tool_result),
            "completed": runtime_event_payload(CompletedEvent(response=outcome)),
            "outcome": runtime_outcome_payload(outcome),
        }
        envelope = runtime_sse_envelope(
            run_id="transport-private",
            conversation_id=7,
            context_type="workspace",
            context_ref="",
            mode="general",
        )
        sse = "".join(
            encode_sse_event(event, seq=index, run_id="transport-private", envelope=envelope)
            for index, event in enumerate((tool_call, tool_result, CompletedEvent(response=outcome)), 1)
        )
        assert "public tool call" in sse
        assert "public tool result" in sse

        # Journal accepts only the closed fact projection.  A nested private
        # value is rejected; the real recorder/trace path stores safe facts.
        run_id = str(uuid4())
        segment_id = str(uuid4())
        with session_factory() as seed:
            conversation = Conversation(title="privacy")
            seed.add(conversation)
            seed.flush()
            conversation_id = int(conversation.id)
            seed.commit()
        run_started = prepare_event(
            event_type="run.started",
            execution_segment_id=segment_id,
            facts={
                "agent_run_id": run_id,
                "origin_kind": "user_message",
                "conversation_id": conversation_id,
                "context_type": "workspace",
                "transport_mode": "sync",
            },
        )
        segment_started = prepare_event(
            event_type="segment.started",
            execution_segment_id=segment_id,
            facts={
                "request_kind": "initial",
                "transport_mode": "sync",
                "execution_path": "model_turn",
                "transport_run_id": None,
            },
        )
        key = JournalKeyDomain("11111111-1111-4111-8111-111111111111", b"j" * 32)
        command = StartRunCommand(
            run_id=run_id,
            conversation_id=conversation_id,
            input_message_id=None,
            origin_kind="user_message",
            initial_context_type="workspace",
            initial_context_entity_id=None,
            initial_context_ref_fingerprint=None,
            fingerprint_key_id=key.key_id,
            initial_transport_mode="sync",
            initial_route_kind="model",
            run_started=run_started,
            segment_started=segment_started,
        )
        recorder = RunRecorderFactory(
            journal_repository,
            key=key,
            enabled=True,
        ).start_run(
            command
        )
        assert recorder.run_id == run_id, getattr(recorder, "diagnostics", None)
        with pytest.raises(JournalEventValidationError):
            prepare_event(
                event_type="tool.proposed",
                execution_segment_id=segment_id,
                facts={
                    "tool_call_id": "call-private",
                    "tool_name": "get_offer",
                    "tool_kind": "read",
                    "args_shape_digest": "sha256:" + "a" * 64,
                    "proposal_outcome": "execution_allowed",
                    "private": sentinel,
                },
            )
        recorder.append_event(
            EventInput(
                event_type="tool.proposed",
                facts={
                    "tool_call_id": "call-public",
                    "tool_name": "get_offer",
                    "tool_kind": "read",
                    "args_shape_digest": "sha256:" + "a" * 64,
                    "proposal_outcome": "execution_allowed",
                },
                source_ref_type="tool_call",
                source_ref_id="call-public",
            )
        )
        recorder.append_event(
            EventInput(
                event_type="tool.completed",
                facts={
                    "tool_call_id": "call-public",
                    "tool_name": "get_offer",
                    "outcome": "completed",
                    "result_shape_digest": "sha256:" + "b" * 64,
                },
                source_ref_type="tool_call",
                source_ref_id="call-public",
            )
        )
        recorder.finish(TerminalDisposition(status="completed"))
        trace = reconstruct_agent_run(
            journal_repository,
            run_id,
            as_of=datetime.now(timezone.utc),
            stale_after=timedelta(minutes=5),
        )
        trace_blob = json.dumps(asdict(trace), ensure_ascii=False, default=str)

        append_log_entry(
            tmp_path,
            "ERROR",
            repr(private_state.internal["exception"]),
        )
        append_log_entry(
            tmp_path,
            "ERROR",
            "runtime_failure "
            + json.dumps(runtime_outcome_payload(
                RuntimeFailureOutcome(
                    code=RuntimeFailureCode.AI_PROVIDER_ERROR,
                    message="public failure",
                    conversation_id=7,
                )
            ), ensure_ascii=False),
        )
        log_blob = json.dumps(read_recent_log_entries(tmp_path), ensure_ascii=False)
        serialized = json.dumps(
            {"events": event_payload, "sse": sse, "trace": trace_blob, "logs": log_blob},
            ensure_ascii=False,
        )
        assert sentinel not in serialized
        assert set(event_payload["tool_call"]) == {
            "tool_call_id",
            "tool_name",
            "public_label",
            "kind",
            "confirm_mode",
            "summary",
            "args_summary",
        }
        assert trace.segments and trace.segments[0].tools
    finally:
        session.close()
