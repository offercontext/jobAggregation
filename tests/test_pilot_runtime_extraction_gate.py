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

    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        before = tuple(sorted(aliases.items()))
        if before in seen:
            break
        seen.add(before)
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


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    """Resolve literal-string aliases used by dynamic compatibility escapes."""

    bindings: dict[str, str] = {}
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        before = tuple(sorted(bindings.items()))
        if before in seen:
            break
        seen.add(before)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            resolved = _constant_string(value, bindings)
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != resolved:
                    bindings[target.id] = resolved
                    changed = True
        if not changed:
            break
    return bindings


def _constant_string(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, bindings)
        right = _constant_string(node.right, bindings)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                part = _constant_string(value.value, bindings)
                if part is None:
                    return None
                parts.append(part)
            else:
                return None
        return "".join(parts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
    ):
        separator = _constant_string(node.func.value, bindings)
        values = node.args[0]
        if separator is None or not isinstance(values, (ast.List, ast.Tuple)):
            return None
        parts = [_constant_string(value, bindings) for value in values.elts]
        if any(part is None for part in parts):
            return None
        return separator.join(part for part in parts if part is not None)
    return None


def _constant_bindings(tree: ast.AST) -> dict[str, object]:
    """Resolve the small literal subset needed for reachability checks."""

    bindings: dict[str, object] = {}
    seen: set[tuple[tuple[str, object], ...]] = set()
    while True:
        before = tuple(sorted(bindings.items()))
        if before in seen:
            break
        seen.add(before)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and (
                value.value is None or isinstance(value.value, (bool, int, float, str))
            ):
                resolved = value.value
            elif isinstance(value, ast.Name) and value.id in bindings:
                resolved = bindings[value.id]
            else:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != resolved:
                    bindings[target.id] = resolved
                    changed = True
        if not changed:
            break
    return bindings


def _dynamic_call_terminal(
    node: ast.Call,
    aliases: dict[str, str],
    bindings: dict[str, str],
) -> str | None:
    """Resolve direct calls and calls through ``getattr(value, name)``."""

    terminal = _call_terminal(node, aliases)
    if terminal is not None:
        return terminal
    if not isinstance(node.func, ast.Call):
        return None
    symbol = node.func.args[1] if len(node.func.args) >= 2 else None
    if _call_terminal(node.func, aliases) != "getattr" or symbol is None:
        return None
    resolved = _constant_string(symbol, bindings)
    return resolved.rsplit(".", 1)[-1] if resolved is not None else None


def _dynamic_getattr_terminal(
    node: ast.AST,
    aliases: dict[str, str],
    bindings: dict[str, str],
) -> str | None:
    """Resolve the method name returned by a ``getattr`` expression."""

    if not isinstance(node, ast.Call) or _call_terminal(node, aliases) != "getattr":
        return None
    if len(node.args) < 2:
        return None
    symbol = node.args[1]
    resolved = _constant_string(symbol, bindings)
    return resolved.rsplit(".", 1)[-1] if resolved is not None else None


def _callable_aliases(
    tree: ast.AST,
    aliases: dict[str, str],
    bindings: dict[str, str],
) -> dict[str, str]:
    """Propagate reviewed callable aliases returned by dynamic ``getattr``."""

    resolved = dict(aliases)
    callable_terminals = {
        "prepare_stream",
        "PreparedStreamGuard",
        "build_guarded_streaming_response",
    }
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        before = tuple(sorted(resolved.items()))
        if before in seen:
            break
        seen.add(before)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            source = _dynamic_getattr_terminal(node.value, resolved, bindings)
            if source is None:
                qualified = _qualified_symbol(node.value, resolved)
                if qualified is None or qualified.rsplit(".", 1)[-1] not in callable_terminals:
                    continue
                source = qualified
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and resolved.get(target.id) != source:
                    resolved[target.id] = source
                    changed = True
        if not changed:
            break
    return resolved


def _dynamic_attribute_strings(
    tree: ast.AST,
    *,
    bindings: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    """Return symbols reached by ``getattr(value, name)`` in a source tree."""

    bindings = _string_bindings(tree) if bindings is None else bindings
    aliases = _binding_aliases(tree) if aliases is None else aliases
    result: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and _call_terminal(node, aliases) == "getattr"
            and len(node.args) >= 2
        ):
            continue
        symbol = node.args[1]
        resolved = _constant_string(symbol, bindings)
        if resolved is not None:
            result.add(resolved)
    return result


def _legacy_string_values(
    tree: ast.AST,
    *,
    bindings: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    """Find exact/embedded legacy switch strings, including dynamic aliases."""

    values = set(
        _dynamic_attribute_strings(
            tree,
            bindings=bindings,
            aliases=aliases,
        )
    )
    values.update(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(fragment in node.value.lower() for fragment in LEGACY_SWITCH_FRAGMENTS)
    )
    return values


def _functions(tree: ast.AST, names: set[str]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    }


def _expect_rejected(source: str, validator: Callable[[ast.AST], None]) -> None:
    with pytest.raises(AssertionError):
        validator(ast.parse(source))


ROUTE_NAMES = frozenset({"send_chat", "send_chat_stream", "confirm_chat", "confirm_chat_stream"})
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
        # Persistence/reliability nouns are forbidden in Route bodies even
        # when they are imported or reached through a local alias.
        "Pending",
        "PendingAction",
        "PendingActionPayload",
        "Ledger",
        "LedgerKeyDomain",
        "Journal",
        "JournalKeyDomain",
        "AgentRunRepository",
        "WriteOperationRepository",
        "ChatRepository",
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
TRANSPORT_PRIMITIVE_IMPORTS = frozenset({"concurrent.futures", "queue", "threading"})
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
TRANSIENT_SECURITY_NAMES = frozenset(
    {
        "SegmentExecutionAuthority",
        "ApprovalExecutionAuthority",
        "ProviderSurfaceBuildIdentity",
        "ProviderInvocationIdentity",
        "NewTurnPrepareCallIdentity",
        "ReadExecutionCallIdentity",
        "TypedPendingCallIdentity",
        "ApprovedWritePrepareCallIdentity",
        "ApprovedWriteExecuteCallIdentity",
        "ApplicationScopeConstraint",
        "AuthorityCallIdentity",
        "BindingTargetResolution",
        "PreparedToolCall",
        "PendingAuthorityClaim",
        "ExecutionClaim",
        "ToolExecutionAuthority",
        "ToolExecutionContext",
        "SegmentSurfaceGate",
        "BoundProviderResponse",
        "TransientToolRuntimeValue",
        "TrustedContextScope",
        "TrustedLedgerOmittedTokenProof",
        "ToolMetadataBundleV1",
        "BundleInstanceToken",
        "ProviderToolMetadataView",
        "ToolDiscoveryMetadataView",
        "ToolAuthorityMetadataView",
        "ToolOperationMetadataView",
        "LegacyDeterministicBoundaryV1",
        "CompensationMetadataView",
        "SegmentToolCatalogLease",
        "SegmentCatalogToken",
        "SegmentToolSpecHandle",
    }
)

CURRENT_METADATA_SECURITY_NAMES = frozenset(
    {
        "ToolMetadataBundleV1",
        "BundleInstanceToken",
        "ProviderToolMetadataView",
        "ToolDiscoveryMetadataView",
        "ToolAuthorityMetadataView",
        "ToolOperationMetadataView",
        "LegacyDeterministicBoundaryV1",
        "CompensationMetadataView",
        "SegmentToolCatalogLease",
        "SegmentCatalogToken",
        "SegmentToolSpecHandle",
    }
)

# This is deliberately a fixed semantic marker set, not an allowlist of
# source paths.  The production scan walks every Python source file and only
# applies the old-Chat/Runtime switch rules to files that actually contain one
# of these reviewed runtime symbols.  Unrelated config names such as
# ``legacy_fallback`` therefore do not become a false positive.
CHAT_RUNTIME_SEMANTIC_NAMES = frozenset(
    {
        *ROUTE_NAMES,
        "PilotRuntime",
        "RuntimeEvent",
        "RuntimeOutcome",
        "PreparedStreamExecution",
        "PreparedStreamGuard",
        "RuntimeTransportContext",
        "runtime_sse_content",
        "runtime_stream_response",
        "execute_runtime_sync",
    }
)
CHAT_RUNTIME_SEMANTIC_MARKERS = tuple(CHAT_RUNTIME_SEMANTIC_NAMES)


def _validate_routes_are_runtime_only(tree: ast.AST) -> None:
    found = _functions(tree, set(ROUTE_NAMES))
    assert set(found) == set(ROUTE_NAMES)
    aliases = _binding_aliases(tree)
    string_bindings = _string_bindings(tree)
    for name, node in found.items():
        forbidden = _resolved_names(node, aliases) & (ROUTE_OWNERSHIP_NAMES | ROUTE_TRANSPORT_NAMES)
        assert not forbidden, f"{name} owns reliability/persistence helpers: {sorted(forbidden)}"
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            terminal = _call_terminal(child, aliases)
            if terminal in ROUTE_OWNERSHIP_NAMES | ROUTE_TRANSPORT_NAMES:
                raise AssertionError(f"{name} directly constructs/owns {terminal}")
            if _call_terminal(child, aliases) == "getattr" and len(child.args) >= 2:
                dynamic_names = _dynamic_attribute_strings(
                    node,
                    bindings=string_bindings,
                    aliases=aliases,
                )
                forbidden_dynamic = dynamic_names & (ROUTE_OWNERSHIP_NAMES | ROUTE_TRANSPORT_NAMES)
                assert not forbidden_dynamic, (
                    f"{name} reaches forbidden helper through getattr: {sorted(forbidden_dynamic)}"
                )


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
    dynamic_found = sorted(_dynamic_attribute_strings(tree) & forbidden_names)
    assert not dynamic_found, f"api reaches transport primitive through getattr: {dynamic_found}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_runtime_sse_content",
            "_runtime_stream_immediate_response",
        }:
            raise AssertionError(f"old SSE helper remains in api: {node.name}")


def _validate_unbounded_queue(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    bindings = _string_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _dynamic_call_terminal(node, aliases, bindings) != "Queue":
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
        module in forbidden_modules
        or any(module.startswith(prefix + ".") for prefix in forbidden_modules)
        for module in imports
    )
    host_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert {"SyncAgentExecutionHost", "SseAgentExecutionHost"} <= host_names
    string_bindings = _string_bindings(tree)
    forbidden_names = {
        "Pending",
        "PendingAction",
        "PendingActionPayload",
        "Ledger",
        "Journal",
        "JournalKeyDomain",
        "WriteOperationCoordinator",
        "RunRecorder",
        "AgentRunRepository",
        "WriteOperationRepository",
        "ChatRepository",
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
        aliases = _binding_aliases(tree)
        names = _resolved_names(node, aliases)
        dynamic_names = _dynamic_attribute_strings(
            node,
            bindings=string_bindings,
            aliases=aliases,
        )
        assert not names.intersection(forbidden_names), (
            f"{node.name} crosses persistence boundary: "
            f"{sorted(names.intersection(forbidden_names))}"
        )
        assert not dynamic_names.intersection(forbidden_names), (
            f"{node.name} reaches persistence boundary through getattr: "
            f"{sorted(dynamic_names.intersection(forbidden_names))}"
        )
        attrs = {child.attr.lower() for child in ast.walk(node) if isinstance(child, ast.Attribute)}
        attrs.update(
            value.lower()
            for value in _dynamic_attribute_strings(
                node,
                bindings=string_bindings,
                aliases=_binding_aliases(tree),
            )
            if isinstance(value, str)
        )
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
    string_bindings = _string_bindings(api_tree)
    callable_aliases = _callable_aliases(api_tree, aliases, string_bindings)

    def dead_branch(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
        constant_bindings = _constant_bindings(api_tree)

        def constant_truth(value: ast.AST) -> bool | None:
            if isinstance(value, ast.Constant) and (
                value.value is None or isinstance(value.value, (bool, int, float, str))
            ):
                return bool(value.value)
            if isinstance(value, ast.Name) and value.id in constant_bindings:
                return bool(constant_bindings[value.id])
            return None

        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.If):
                truth = constant_truth(parent.test)
                if truth is not None:
                    if current in parent.body and not truth:
                        return True
                    if current in parent.orelse and truth:
                        return True
            if isinstance(parent, ast.While):
                truth = constant_truth(parent.test)
                if current in parent.body and truth is False:
                    return True
            current = parent
        return False

    def nested_in_function(
        node: ast.AST,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        parents: dict[ast.AST, ast.AST],
    ) -> bool:
        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent is not function
            current = parent
        return False

    def immediate_branch(
        node: ast.AST,
        prepared_targets: set[str],
        parents: dict[ast.AST, ast.AST],
    ) -> bool:
        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.If) and current in parent.body:
                test = parent.test
                if (
                    isinstance(test, ast.Call)
                    and _call_terminal(test, aliases) == "isinstance"
                    and len(test.args) >= 2
                    and isinstance(test.args[0], ast.Name)
                    and test.args[0].id in prepared_targets
                    and "ImmediateHttpOutcome" in _resolved_names(test.args[1], aliases)
                ):
                    return True
            current = parent
        return False

    def conditional_path(
        node: ast.AST,
        parents: dict[ast.AST, ast.AST],
    ) -> bool:
        constant_bindings = _constant_bindings(api_tree)

        def constant_truth(value: ast.AST) -> bool | None:
            if isinstance(value, ast.Constant) and (
                value.value is None or isinstance(value.value, (bool, int, float, str))
            ):
                return bool(value.value)
            if isinstance(value, ast.Name) and value.id in constant_bindings:
                return bool(constant_bindings[value.id])
            return None

        current = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.If):
                truth = constant_truth(parent.test)
                if truth is None:
                    return True
                if current in parent.body and truth:
                    current = parent
                    continue
                if current in parent.orelse and not truth:
                    current = parent
                    continue
                return True
            if isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
                return True
            current = parent
        return False

    def assigned_names(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        else:
            return ()
        return tuple(target.id for target in targets if isinstance(target, ast.Name))

    def prepared_argument(guard_call: ast.Call) -> ast.expr | None:
        keyword = next(
            (keyword.value for keyword in guard_call.keywords if keyword.arg == "prepared"),
            None,
        )
        if keyword is not None:
            return keyword
        # The reviewed transport contract also allows Guard(prepared, ...).
        return guard_call.args[0] if guard_call.args else None

    def response_guard_value(response: ast.Call) -> ast.expr | None:
        return next(
            (keyword.value for keyword in response.keywords if keyword.arg == "guard"),
            None,
        )

    def returned_guard_response(
        node: ast.AST,
        aliases: dict[str, str],
    ) -> tuple[ast.Return, ast.Call, ast.expr] | None:
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            return None
        if _call_terminal(node.value, aliases) != "build_guarded_streaming_response":
            return None
        guard_value = response_guard_value(node.value)
        if guard_value is None:
            raise AssertionError("guarded response must receive a Guard result")
        return node, node.value, guard_value

    guarded_functions = 0
    for function in ast.walk(api_tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parents = {
            child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
        }
        scoped_nodes = [
            node for node in ast.walk(function) if not nested_in_function(node, function, parents)
        ]
        prepared_calls_in_function = [
            node
            for node in scoped_nodes
            if isinstance(node, ast.Call)
            and _dynamic_call_terminal(node, callable_aliases, string_bindings) == "prepare_stream"
        ]
        if not prepared_calls_in_function:
            continue
        prepared_assignments: list[ast.Assign | ast.AnnAssign] = []
        prepared_targets: list[str] = []
        for prepared_call in prepared_calls_in_function:
            parent = parents.get(prepared_call)
            if not (
                isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is prepared_call
            ):
                raise AssertionError("PreparedStreamExecution must be owned by a guarded handle")
            targets = assigned_names(parent)
            if not targets:
                raise AssertionError("prepared stream handle must have a named owner")
            prepared_assignments.append(parent)
            prepared_targets.extend(targets)
        assert prepared_targets, "prepared stream handles must have a named owner"
        assert len(prepared_targets) == len(set(prepared_targets)), (
            "a prepared handle cannot be rebound before its guard"
        )
        prepared_target_set = set(prepared_targets)
        prepare_assignment_ids = {id(node) for node in prepared_assignments}
        for node in scoped_nodes:
            if id(node) in prepare_assignment_ids:
                continue
            if prepared_target_set.intersection(assigned_names(node)):
                raise AssertionError("prepared handle was rebound before its guard")

        guards_by_target: dict[str, int] = {target: 0 for target in prepared_targets}
        guard_targets: dict[str, str] = {}
        guard_assignments: dict[str, ast.Assign | ast.AnnAssign] = {}
        guard_calls: list[tuple[ast.Call, str, str | None]] = []
        for node in scoped_nodes:
            if not isinstance(node, ast.Call):
                continue
            if (
                _dynamic_call_terminal(node, callable_aliases, string_bindings)
                != "PreparedStreamGuard"
            ):
                continue
            assert not dead_branch(node, parents), "dead-branch PreparedStreamGuard is not a guard"
            assert not conditional_path(node, parents), (
                "PreparedStreamGuard must be reachable on every stream path"
            )
            prepared_arg = prepared_argument(node)
            if not isinstance(prepared_arg, ast.Name) or prepared_arg.id not in guards_by_target:
                raise AssertionError("PreparedStreamGuard must consume a prepared handle")
            guards_by_target[prepared_arg.id] += 1
            parent = parents.get(node)
            owner_name: str | None = None
            if isinstance(parent, ast.Assign):
                targets = [target for target in parent.targets if isinstance(target, ast.Name)]
                assert len(targets) == 1, "a PreparedStreamGuard must have one owner"
                owner_name = targets[0].id
                assert owner_name not in guard_targets, "guard variable was rebound"
                guard_targets[owner_name] = prepared_arg.id
                guard_assignments[owner_name] = parent
            elif isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
                owner_name = parent.target.id
                assert owner_name not in guard_targets, "guard variable was rebound"
                guard_targets[owner_name] = prepared_arg.id
                guard_assignments[owner_name] = parent
            elif isinstance(parent, ast.keyword):
                owner = parents.get(parent)
                if not (
                    parent.arg == "guard"
                    and isinstance(owner, ast.Call)
                    and _call_terminal(owner, callable_aliases)
                    == "build_guarded_streaming_response"
                ):
                    raise AssertionError("PreparedStreamGuard result is not response-owned")
            else:
                if not (
                    isinstance(parent, ast.Call)
                    and _call_terminal(parent, callable_aliases)
                    == "build_guarded_streaming_response"
                ):
                    raise AssertionError("PreparedStreamGuard result is not response-owned")
            guard_calls.append((node, prepared_arg.id, owner_name))
        assert all(count == 1 for count in guards_by_target.values()), (
            "each prepared stream handle must have exactly one data-flow guard"
        )

        for node in scoped_nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            for target in assigned_names(node):
                if target in guard_targets and node is not guard_assignments[target]:
                    raise AssertionError("guard variable was rebound before response return")

        response_returns: dict[str, ast.Return] = {}
        direct_guard_ids: set[int] = set()
        returned_responses: list[tuple[ast.Return, ast.Call, ast.expr]] = []
        for returned in scoped_nodes:
            result = returned_guard_response(returned, callable_aliases)
            if result is None:
                continue
            return_node, response, guard_value = result
            returned_responses.append(result)
            if isinstance(guard_value, ast.Name):
                response_returns[guard_value.id] = return_node
            elif isinstance(guard_value, ast.Call):
                direct_guard_ids.add(id(guard_value))

        for guard_call, _prepared_name, owner_name in guard_calls:
            if owner_name is not None:
                assert owner_name in response_returns, (
                    "each PreparedStreamGuard must flow into the returned guarded response"
                )
            else:
                assert id(guard_call) in direct_guard_ids, (
                    "each PreparedStreamGuard must flow into the returned guarded response"
                )

        first_prepare_line = min(node.lineno for node in prepared_assignments)
        returned_response_ids = {
            id(return_node) for return_node, _response, _guard_value in returned_responses
        }
        for node in scoped_nodes:
            if not isinstance(node, (ast.Return, ast.Raise)):
                continue
            if node.lineno <= first_prepare_line or dead_branch(node, parents):
                continue
            if isinstance(node, ast.Return) and id(node) in returned_response_ids:
                if conditional_path(node, parents):
                    raise AssertionError("guarded response return is not reachable on every path")
                continue
            if immediate_branch(node, prepared_target_set, parents):
                continue
            raise AssertionError("prepared stream has an unguarded reachable return path")
        guarded_functions += 1
    assert guarded_functions, "prepared stream handles must have a transport guard"


def _validate_runtime_event_contract(tree: ast.AST) -> None:
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    user_event = classes.get("UserMessageSavedEvent")
    assert user_event is not None
    fields = [
        node.target.id
        for node in user_event.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert fields == ["role"], "UserMessageSavedEvent may expose only role"
    role_field = next(
        node
        for node in user_event.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "role"
    )
    assert isinstance(role_field.value, ast.Constant) and role_field.value.value == "user"
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
        aliases = _binding_aliases(tree)
        string_bindings = _string_bindings(tree)
        error_prefix_aliases = _error_prefix_aliases(tree, bindings=string_bindings)
        semantic_names = _resolved_names(tree, aliases)
        # Walk every production file, but scope old-path findings to files
        # that are demonstrably part of Chat/Pilot Runtime.  This keeps an
        # unrelated configuration knob named ``legacy_fallback`` from being
        # mistaken for a second Chat route.
        in_chat_runtime_scope = (
            path in {API, TRANSPORT}
            or path.parent == RUNTIME
            or bool(
                (
                    semantic_names
                    | _dynamic_attribute_strings(
                        tree,
                        bindings=string_bindings,
                        aliases=aliases,
                    )
                )
                & CHAT_RUNTIME_SEMANTIC_NAMES
            )
        )
        if not in_chat_runtime_scope:
            continue
        identifiers = semantic_names
        for identifier in identifiers:
            lowered = identifier.lower()
            if any(fragment in lowered for fragment in LEGACY_SWITCH_FRAGMENTS):
                findings.append(f"{path.relative_to(ROOT)}:{identifier}")
        for value in _legacy_string_values(
            tree,
            bindings=string_bindings,
            aliases=aliases,
        ):
            lowered = value.lower()
            if any(fragment in lowered for fragment in LEGACY_SWITCH_FRAGMENTS):
                findings.append(f"{path.relative_to(ROOT)}:{value}")
        for node in ast.walk(tree):
            if _is_error_prefix_call(
                node,
                tree,
                aliases=aliases,
                prefix_aliases=error_prefix_aliases,
                bindings=string_bindings,
            ) and path not in {SRC / "ai" / "tool_runtime" / "rendering.py"}:
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}:错误前缀解析")
    assert findings == []


def _validate_no_legacy_switch_tree(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    identifiers = _resolved_names(tree, aliases)
    assert not any(
        any(fragment in identifier.lower() for fragment in LEGACY_SWITCH_FRAGMENTS)
        for identifier in identifiers
    )
    assert not any(
        any(fragment in value.lower() for fragment in LEGACY_SWITCH_FRAGMENTS)
        for value in _legacy_string_values(
            tree,
            bindings=_string_bindings(tree),
            aliases=aliases,
        )
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
    aliases = _binding_aliases(tree)
    bindings = _string_bindings(tree)
    prefix_aliases = _error_prefix_aliases(tree, bindings=bindings)
    for node in ast.walk(tree):
        if _is_error_prefix_call(
            node,
            tree,
            aliases=aliases,
            prefix_aliases=prefix_aliases,
            bindings=bindings,
        ):
            raise AssertionError("compatibility error-prefix parsing is not allowed here")


def _error_prefix_aliases(
    tree: ast.AST,
    *,
    bindings: dict[str, str] | None = None,
) -> set[str]:
    bindings = _string_bindings(tree) if bindings is None else bindings
    return {name for name, value in bindings.items() if value == "错误："}


def _is_error_prefix_call(
    node: ast.AST,
    tree: ast.AST,
    *,
    aliases: dict[str, str] | None = None,
    prefix_aliases: set[str] | None = None,
    bindings: dict[str, str] | None = None,
) -> bool:
    aliases = _binding_aliases(tree) if aliases is None else aliases
    bindings = _string_bindings(tree) if bindings is None else bindings
    prefix_aliases = (
        _error_prefix_aliases(tree, bindings=bindings) if prefix_aliases is None else prefix_aliases
    )
    if not (isinstance(node, ast.Call) and node.args):
        return False
    function_name = _dynamic_call_terminal(node, aliases, bindings)
    if function_name != "startswith":
        return False
    prefix = node.args[0]
    return (isinstance(prefix, ast.Constant) and prefix.value == "错误：") or (
        isinstance(prefix, ast.Name) and prefix.id in prefix_aliases
    )


def _validate_boundary_names_absent(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    resolved = _resolved_names(tree, aliases)
    dynamic = _dynamic_attribute_strings(tree, aliases=aliases)
    assert not ((resolved | dynamic) & PRIVATE_BOUNDARY_NAMES)


def _validate_no_generic_asdict_boundary(tree: ast.AST) -> None:
    aliases = _binding_aliases(tree)
    forbidden = (
        PRIVATE_BOUNDARY_NAMES
        | TRANSIENT_SECURITY_NAMES
        | {
            "MessageOutcome",
            "ConfirmationRequiredOutcome",
            "OperationPendingOutcome",
            "OperationReplayOutcome",
        }
    )

    def direct_forbidden(value: ast.AST) -> bool:
        return bool(_resolved_names(value, aliases) & forbidden)

    def asdict_argument(node: ast.Call) -> ast.expr | None:
        if _call_terminal(node, aliases) == "asdict":
            return node.args[0] if node.args else None
        if not (
            isinstance(node.func, ast.Call)
            and _call_terminal(node.func, aliases) == "getattr"
            and len(node.func.args) >= 2
            and isinstance(node.func.args[1], ast.Constant)
            and node.func.args[1].value == "asdict"
        ):
            return None
        return node.args[0] if node.args else None

    def target_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return tuple(target.id for target in targets if isinstance(target, ast.Name))

    # Taint only values with a concrete runtime-private origin.  A generic
    # logger/serializer parameter remains valid until a private value is
    # actually passed into it; this avoids banning ordinary ``asdict(value)``
    # helpers solely because they are generic.
    tainted: set[str] = set()
    for _ in range(4):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                if isinstance(node, ast.AnnAssign) and (
                    _resolved_names(node.annotation, aliases) & forbidden
                ):
                    for target in target_names(node):
                        if target not in tainted:
                            tainted.add(target)
                            changed = True
                continue
            value_tainted = direct_forbidden(value) or any(
                isinstance(child, ast.Name) and child.id in tainted for child in ast.walk(value)
            )
            if value_tainted:
                for target in target_names(node):
                    if target not in tainted:
                        tainted.add(target)
                        changed = True
        if not changed:
            break

    asdict_parameter_names: dict[str, dict[str, int | None]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional_parameters = (*function.args.posonlyargs, *function.args.args)
        parameters = {
            parameter.arg for parameter in (*positional_parameters, *function.args.kwonlyargs)
        }
        parameter_aliases = {parameter: parameter for parameter in parameters}
        for _ in range(3):
            changed = False
            for assignment in ast.walk(function):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                    continue
                value = assignment.value
                if not isinstance(value, ast.Name) or value.id not in parameter_aliases:
                    continue
                root = parameter_aliases[value.id]
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in parameter_aliases:
                        parameter_aliases[target.id] = root
                        changed = True
            if not changed:
                break
        found = {
            parameter_aliases[argument.id]
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and (argument := asdict_argument(node)) is not None
            and isinstance(argument, ast.Name)
            and argument.id in parameter_aliases
        }
        if found:
            asdict_parameter_names[function.name] = {
                parameter: next(
                    (
                        index
                        for index, candidate in enumerate(positional_parameters)
                        if candidate.arg == parameter
                    ),
                    None,
                )
                for parameter in found
            }
        annotations = ast.unparse(function.args) if function.args else ""
        if found and any(name in annotations for name in forbidden):
            raise AssertionError("private runtime values cannot be annotated into generic asdict")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        argument = asdict_argument(node)
        if argument is None:
            continue
        assert not direct_forbidden(argument), (
            "runtime-private DTOs/outcomes must use explicit projections, not asdict"
        )
        if isinstance(argument, ast.Name):
            assert argument.id not in tainted, (
                "runtime-private DTOs/outcomes must use explicit projections, not asdict"
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = _call_terminal(node, aliases)
        parameters = asdict_parameter_names.get(function_name or "")
        if not parameters:
            continue
        # Parameter-to-argument mapping is intentionally conservative: a
        # positional parameter or an exact keyword name is enough to prove
        # the private value reaches the helper.
        for parameter, index in parameters.items():
            if index is not None and index < len(node.args):
                argument = node.args[index]
                if isinstance(argument, ast.Name) and argument.id in tainted:
                    raise AssertionError(
                        "runtime-private DTOs/outcomes cannot flow into generic asdict helpers"
                    )
        for keyword in node.keywords:
            if (
                keyword.arg in parameters
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id in tainted
            ):
                raise AssertionError(
                    "runtime-private DTOs/outcomes cannot flow into generic asdict helpers"
                )


def _validate_no_transient_generic_serializers(tree: ast.AST) -> None:
    """Reject generic serializers whose parameter is a transient contract.

    This is deliberately function-local.  The older whole-module taint pass is
    tuned for ``asdict`` and would otherwise confuse ordinary JSON ``dumps``
    parameters in an unrelated function with an authority value elsewhere in
    the same module.
    """

    aliases = _binding_aliases(tree)
    serializers = {"asdict", "checkpoint", "copy", "deepcopy", "dumps", "replace"}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        transient_parameters = {
            parameter.arg
            for parameter in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
            if parameter.annotation is not None
            and bool(_resolved_names(parameter.annotation, aliases) & TRANSIENT_SECURITY_NAMES)
        }
        if not transient_parameters:
            transient_parameters = set()
        parameter_aliases = set(transient_parameters)
        while True:
            changed = False
            for assignment in ast.walk(function):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                    continue
                if assignment.value is None:
                    continue
                value = assignment.value
                direct_constructor = (
                    _call_terminal(value, aliases) if isinstance(value, ast.Call) else None
                )
                if not (
                    direct_constructor in TRANSIENT_SECURITY_NAMES
                    or (isinstance(value, ast.Name) and value.id in parameter_aliases)
                ):
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in parameter_aliases:
                        parameter_aliases.add(target.id)
                        changed = True
            if not changed:
                break
        for call in ast.walk(function):
            argument = call.args[0] if isinstance(call, ast.Call) and call.args else None
            direct_constructor = (
                _call_terminal(argument, aliases) if isinstance(argument, ast.Call) else None
            )
            if (
                isinstance(call, ast.Call)
                and _call_terminal(call, aliases) in serializers
                and argument is not None
                and (
                    any(
                        isinstance(item, ast.Name) and item.id in parameter_aliases
                        for item in ast.walk(argument)
                    )
                    or any(
                        isinstance(item, ast.Call)
                        and _call_terminal(item, aliases) in TRANSIENT_SECURITY_NAMES
                        for item in ast.walk(argument)
                    )
                    or direct_constructor in TRANSIENT_SECURITY_NAMES
                )
            ):
                raise AssertionError("transient authority values cannot reach a generic serializer")


def _validate_no_asdict_in_extraction_scope(tree: ast.AST) -> None:
    """Extraction modules must never invoke generic dataclass serialization."""

    aliases = _binding_aliases(tree)
    bindings = _string_bindings(tree)
    asdict_aliases = {
        name for name, source in aliases.items() if source.rsplit(".", 1)[-1] == "asdict"
    }
    asdict_aliases.add("asdict")

    def dynamic_getattr_is_asdict(value: ast.AST) -> bool:
        if not (
            isinstance(value, ast.Call)
            and _call_terminal(value, aliases) == "getattr"
            and len(value.args) >= 2
        ):
            return False
        symbol = value.args[1]
        return _constant_string(symbol, bindings) == "asdict"

    while True:
        changed = False
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            value = assignment.value
            is_alias = (isinstance(value, ast.Name) and value.id in asdict_aliases) or (
                isinstance(value, ast.Call)
                and (
                    _dynamic_call_terminal(value, aliases, bindings) == "asdict"
                    or dynamic_getattr_is_asdict(value)
                )
            )
            if not is_alias:
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in asdict_aliases:
                    asdict_aliases.add(target.id)
                    changed = True
        if not changed:
            break
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _dynamic_call_terminal(node, aliases, bindings) == "asdict" or (
            isinstance(node.func, ast.Name) and node.func.id in asdict_aliases
        ):
            raise AssertionError(
                "Pilot Runtime extraction scope must use explicit public projections"
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
    # The validator must consume the complete production inventory; it then
    # applies the fixed Chat/Runtime semantic scope internally.
    _validate_no_legacy_switches(_production_files())
    _validate_model_dispatch_not_legacy(_tree(RUNTIME / "service.py"))


def test_unrelated_config_legacy_fallback_is_outside_chat_runtime_semantics() -> None:
    config = SRC / "config.py"
    assert "legacy_fallback" in _names(_tree(config))
    _validate_no_legacy_switches((config,))


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
    _expect_rejected(
        "name = 'runtime_sse_content'\ngetattr(transport, name)",
        _validate_no_stream_primitive_in_api,
    )
    _expect_rejected(
        "from offerpilot.repositories.chat import ChatRepository", _validate_execution_host_boundary
    )
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
    _expect_rejected(
        "class SyncAgentExecutionHost:\n"
        "    def __init__(self, value):\n"
        "        self.store = getattr(value, 'journal')\n",
        _validate_execution_host_boundary,
    )
    _expect_rejected(
        "def _runtime_sse_content():\n    return encode_sse_event({}, seq=1)",
        _validate_no_stream_primitive_in_api,
    )
    _expect_rejected(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class UserMessageSavedEvent:\n"
        "    role: str = 'user'\n"
        "    internal: object = None\n"
        "RuntimeEvent: object = UserMessageSavedEvent\n",
        _validate_runtime_event_contract,
    )
    _expect_rejected(
        "class X:\n    def dual_run(self):\n        pass",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "from old_path import dual_run as execute\nexecute()",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "switch_name = 'dual_run'\ngetattr(runtime, switch_name)()",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "from builtins import getattr as fetch\n"
        "switch_name = 'dual_run'\nfetch(runtime, switch_name)()",
        _validate_no_legacy_switch_tree,
    )
    _expect_rejected(
        "from offerpilot.models import Journal as J\n"
        "def send_chat():\n    return J()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
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
        "ERROR_PREFIX = '错误：'\ndef render(value):\n    return value.startswith(ERROR_PREFIX)",
        _validate_no_error_prefix_expansion,
    )
    _expect_rejected(
        "ERROR_PREFIX = '错误：'\n"
        "ALIAS = ERROR_PREFIX\n"
        "def render(value):\n    return value.startswith(ALIAS)",
        _validate_no_error_prefix_expansion,
    )
    _expect_rejected(
        "ERROR_PREFIX = '错误：'\n"
        "def render(value):\n"
        "    starts = value.startswith\n"
        "    return starts(ERROR_PREFIX)",
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
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )
    _expect_rejected(
        "def send_chat():\n"
        "    method_name = 'run_turn'\n"
        "    return getattr(runtime, method_name)()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "method_name = 'run_turn'\n"
        "def send_chat():\n    return getattr(runtime, method_name)()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "from builtins import getattr as fetch\n"
        "method_name = 'run_turn'\n"
        "def send_chat():\n    return fetch(runtime, method_name)()\n"
        "def send_chat_stream():\n    return None\n"
        "def confirm_chat():\n    return None\n"
        "def confirm_chat_stream():\n    return None\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome as Failure\n"
        "def persist(value):\n"
        "    return getattr(value, 'RuntimeFailureOutcome')\n",
        _validate_boundary_names_absent,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "from dataclasses import asdict\n"
        "outcome = RuntimeFailureOutcome(...)\n"
        "alias = outcome\n"
        "asdict(alias)",
        _validate_no_generic_asdict_boundary,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "from dataclasses import asdict\n"
        "outcome: RuntimeFailureOutcome\n"
        "asdict(outcome)",
        _validate_no_generic_asdict_boundary,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "outcome = RuntimeFailureOutcome(...)\n"
        "getattr(dataclasses, 'asdict')(outcome)",
        _validate_no_generic_asdict_boundary,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "from dataclasses import asdict\n"
        "def dump(value):\n"
        "    return asdict(value)\n"
        "outcome = RuntimeFailureOutcome(...)\n"
        "dump(outcome)",
        _validate_no_generic_asdict_boundary,
    )
    _expect_rejected(
        "from offerpilot.pilot_runtime import RuntimeFailureOutcome\n"
        "from dataclasses import asdict\n"
        "def dump(value):\n"
        "    alias = value\n"
        "    return asdict(alias)\n"
        "outcome = RuntimeFailureOutcome(...)\n"
        "dump(outcome)",
        _validate_no_generic_asdict_boundary,
    )
    positional_guard_source = (
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    prepared = runtime.prepare_stream()\n"
        "    guard = Guard(prepared, on_execute=lambda: None)\n"
        "    return build_guarded_streaming_response((), guard=guard)\n"
    )
    _validate_prepared_streams_are_guarded(
        ast.parse(
            "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
        ),
        ast.parse(positional_guard_source),
    )
    _expect_rejected(
        positional_guard_source.replace(
            "    guard = Guard(prepared, on_execute=lambda: None)\n",
            "    if False:\n        guard = Guard(prepared, on_execute=lambda: None)\n",
        ),
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )
    _expect_rejected(
        positional_guard_source.replace(
            "    guard = Guard(prepared, on_execute=lambda: None)\n",
            "    return None\n    guard = Guard(prepared, on_execute=lambda: None)\n",
        ),
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
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
        SRC / "ai" / "agent_contracts.py",
        SRC / "ai" / "agent_loop.py",
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


def test_generic_log_serializer_is_not_rejected_without_private_value_flow() -> None:
    tree = ast.parse(
        "from dataclasses import asdict\n"
        "def append_log_entry(value):\n"
        "    return asdict(value)\n"
        "append_log_entry({'public': 1})\n"
    )
    _validate_no_generic_asdict_boundary(tree)


def test_allowlisted_production_call_sites_do_not_asdict_private_runtime_values() -> None:
    allowlisted_production = (API, TRANSPORT, *tuple(sorted(RUNTIME.glob("*.py"))))
    for path in allowlisted_production:
        _validate_no_generic_asdict_boundary(_tree(path))


def test_production_does_not_asdict_transient_authority_or_claim_values() -> None:
    for path in _production_files():
        tree = _tree(path)
        _validate_no_generic_asdict_boundary(tree)
        _validate_no_transient_generic_serializers(tree)


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import asdict\n"
        "def dump(value: TransientToolRuntimeValue): return asdict(value)\n",
        "from dataclasses import replace\n"
        "def dump(value: PendingAuthorityClaim): return replace(value)\n",
        "from copy import deepcopy\ndef dump(value: ExecutionClaim): return deepcopy(value)\n",
        "import pickle\n"
        "def checkpoint(value: TrustedLedgerOmittedTokenProof): "
        "return pickle.dumps(value)\n",
        "import json\n"
        "def checkpoint():\n"
        "    claim = PendingAuthorityClaim(...)\n"
        "    return json.dumps(claim)\n",
    ],
)
def test_transient_security_values_cannot_reach_generic_serializers(
    source: str,
) -> None:
    with pytest.raises(AssertionError):
        _validate_no_transient_generic_serializers(ast.parse(source))


def test_current_metadata_security_types_are_fixed_transient_markers() -> None:
    assert CURRENT_METADATA_SECURITY_NAMES <= TRANSIENT_SECURITY_NAMES


@pytest.mark.parametrize("security_name", sorted(CURRENT_METADATA_SECURITY_NAMES))
def test_current_metadata_security_values_cannot_reach_generic_serializers(
    security_name: str,
) -> None:
    source = (
        "import json\n"
        f"def checkpoint(value: {security_name}):\n"
        "    return json.dumps({'private': value}, default=str)\n"
    )
    with pytest.raises(AssertionError):
        _validate_no_transient_generic_serializers(ast.parse(source))


def test_api_transport_and_persistence_do_not_reference_metadata_security_values() -> None:
    boundary_paths = (
        API,
        TRANSPORT,
        RUNTIME / "persistence.py",
        SRC / "repositories" / "chat.py",
        SRC / "models.py",
        SRC / "schemas.py",
    )
    for path in boundary_paths:
        tree = _tree(path)
        aliases = _binding_aliases(tree)
        dynamic = _dynamic_attribute_strings(tree, aliases=aliases)
        assert not ((_resolved_names(tree, aliases) | dynamic) & CURRENT_METADATA_SECURITY_NAMES), (
            f"{path.relative_to(ROOT)} leaks metadata security values"
        )
        _validate_no_generic_asdict_boundary(tree)
        _validate_no_transient_generic_serializers(tree)


def test_extraction_scope_has_no_generic_asdict_calls() -> None:
    allowlisted_production = (API, TRANSPORT, *tuple(sorted(RUNTIME.glob("*.py"))))
    for path in allowlisted_production:
        _validate_no_asdict_in_extraction_scope(_tree(path))


def test_asdict_comment_is_not_a_call_site() -> None:
    _validate_no_asdict_in_extraction_scope(
        ast.parse("# asdict(values[0]) must remain only a comment\nvalue = {'public': 1}\n")
    )


def test_task11_negative_fixtures_cover_dynamic_and_reachability_bypasses() -> None:
    _expect_rejected(
        "import queue\ngetattr(queue, 'Queue')(1)\n",
        _validate_unbounded_queue,
    )
    _expect_rejected(
        "import queue\nname = 'Queue'\ngetattr(queue, name)(1)\n",
        _validate_unbounded_queue,
    )
    _expect_rejected(
        "class SyncAgentExecutionHost:\n"
        "    def __init__(self, value):\n"
        "        self.store = getattr(value, 'AgentRunRepository')\n",
        _validate_execution_host_boundary,
    )
    _expect_rejected(
        "class SseAgentExecutionHost:\n"
        "    def __init__(self, value):\n"
        "        name = 'PendingAction'\n"
        "        self.store = getattr(value, name)\n",
        _validate_execution_host_boundary,
    )
    _expect_rejected(
        "def render(value):\n    return getattr(value, 'startswith')('错误：')\n",
        _validate_no_error_prefix_expansion,
    )
    _expect_rejected(
        "def render(value):\n"
        "    method_name = 'startswith'\n"
        "    return getattr(value, method_name)('错误：')\n",
        _validate_no_error_prefix_expansion,
    )
    _expect_rejected(
        "from dataclasses import asdict\nvalues = [object()]\nasdict(values[0])\n",
        _validate_no_asdict_in_extraction_scope,
    )
    _expect_rejected(
        "from dataclasses import asdict\ndump = asdict\nvalues = [object()]\ndump(values[0])\n",
        _validate_no_asdict_in_extraction_scope,
    )
    _expect_rejected(
        "import dataclasses\n"
        "name = 'asdict'\n"
        "values = [object()]\n"
        "getattr(dataclasses, name)(values[0])\n",
        _validate_no_asdict_in_extraction_scope,
    )
    _expect_rejected(
        "import dataclasses\n"
        "name = 'asdict'\n"
        "dump = getattr(dataclasses, name)\n"
        "values = [object()]\n"
        "dump(values[0])\n",
        _validate_no_asdict_in_extraction_scope,
    )
    _expect_rejected(
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    prepared = runtime.prepare_stream()\n"
        "    if condition:\n"
        "        guard = Guard(prepared)\n"
        "        return build_guarded_streaming_response((), guard=guard)\n"
        "    return Response()\n",
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )
    _expect_rejected(
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    prepared = runtime.prepare_stream()\n"
        "    guard = Guard(prepared)\n"
        "    guard = None\n"
        "    return build_guarded_streaming_response((), guard=guard)\n",
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )
    _expect_rejected(
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    prepared = runtime.prepare_stream()\n"
        "    guard = Guard(prepared)\n"
        "    return None\n",
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )
    _expect_rejected(
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    truth = False\n"
        "    prepared = runtime.prepare_stream()\n"
        "    if truth:\n"
        "        guard = Guard(prepared)\n"
        "    return build_guarded_streaming_response((), guard=guard)\n",
        lambda tree: _validate_prepared_streams_are_guarded(
            ast.parse(
                "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
            ),
            tree,
        ),
    )


def test_task11_positive_fixtures_keep_dynamic_helpers_scoped() -> None:
    _validate_unbounded_queue(ast.parse("import queue\ngetattr(queue, 'Queue')()\n"))
    _validate_unbounded_queue(ast.parse("import queue\nname = 'Queue'\ngetattr(queue, name)()\n"))
    _validate_execution_host_boundary(
        ast.parse(
            "class SyncAgentExecutionHost:\n"
            "    def __init__(self, value):\n"
            "        self.store = getattr(value, 'public_store')\n"
            "class SseAgentExecutionHost:\n"
            "    def __init__(self, value):\n"
            "        self.store = getattr(value, 'public_store')\n"
        )
    )
    _validate_no_error_prefix_expansion(
        ast.parse("def render(value):\n    return getattr(value, 'endswith')('错误：')\n")
    )
    _validate_no_asdict_in_extraction_scope(
        ast.parse("# asdict(values[0])\nvalue = {'public': 1}\n")
    )
    dynamic_prepare_source = (
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    method_name = 'prepare_stream'\n"
        "    prepared = getattr(runtime, method_name)()\n"
        "    guard = Guard(prepared)\n"
        "    return build_guarded_streaming_response((), guard=guard)\n"
    )
    _validate_prepared_streams_are_guarded(
        ast.parse(
            "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
        ),
        ast.parse(dynamic_prepare_source),
    )


def test_task11_prepared_gate_rejects_dynamic_prepare_aliases() -> None:
    runtime_tree = ast.parse(
        "PreparedStreamExecution('id', PreparationKind.REPLAY, StreamExecutionMode.DIRECT, {})"
    )
    valid_then_dynamic = (
        "from offerpilot.chat_transport import PreparedStreamGuard as Guard\n"
        "from offerpilot.chat_transport import build_guarded_streaming_response\n"
        "def send_chat_stream(runtime):\n"
        "    prepared = runtime.prepare_stream()\n"
        "    guard = Guard(prepared)\n"
        "    hidden = getattr(runtime, 'prepare_stream')()\n"
        "    return build_guarded_streaming_response((), guard=guard)\n"
    )
    _expect_rejected(
        valid_then_dynamic,
        lambda tree: _validate_prepared_streams_are_guarded(runtime_tree, tree),
    )
    _expect_rejected(
        valid_then_dynamic.replace(
            "hidden = getattr(runtime, 'prepare_stream')()\n",
            "method_name = 'prepare_stream'\n    hidden = getattr(runtime, method_name)()\n",
        ),
        lambda tree: _validate_prepared_streams_are_guarded(runtime_tree, tree),
    )
    _expect_rejected(
        valid_then_dynamic.replace(
            "hidden = getattr(runtime, 'prepare_stream')()\n",
            "prepare = getattr(runtime, 'prepare_stream')\n    hidden = prepare()\n",
        ),
        lambda tree: _validate_prepared_streams_are_guarded(runtime_tree, tree),
    )


def test_computed_reflection_does_not_escape_extraction_gates() -> None:
    _expect_rejected(
        "import dataclasses\n"
        "def dump(value):\n"
        "    return getattr(dataclasses, ''.join(['as', 'dict']))(value)\n",
        _validate_no_asdict_in_extraction_scope,
    )
    _expect_rejected(
        "def send_chat(runtime):\n"
        "    return getattr(runtime, 'append_' + 'message')('x')\n"
        "def send_chat_stream(runtime): return runtime.run()\n"
        "def confirm_chat(runtime): return runtime.run()\n"
        "def confirm_chat_stream(runtime): return runtime.run()\n",
        _validate_routes_are_runtime_only,
    )
    _expect_rejected(
        "class SyncAgentExecutionHost:\n"
        "    def __init__(self, value):\n"
        "        self.store = getattr(value, 'Chat' + 'Repository')\n"
        "class SseAgentExecutionHost: pass\n",
        _validate_execution_host_boundary,
    )


def test_transient_container_does_not_escape_generic_serializer_gate() -> None:
    source = (
        "import json\n"
        "def dump(claim: PendingAuthorityClaim):\n"
        "    return json.dumps({'claim': claim}, default=str)\n"
    )
    with pytest.raises(AssertionError):
        _validate_no_transient_generic_serializers(ast.parse(source))


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
        assert set(payload) <= {
            "stream_version",
            "supports_delta",
            "supports_tool_events",
            "supports_confirmation",
            "role",
            "phase",
            "label",
            "delta",
            "tool_call_id",
            "tool_name",
            "public_label",
            "kind",
            "confirm_mode",
            "summary",
            "status",
            "evidence",
            "affected_resources",
            "changed_entities",
            "operation_id",
            "message",
            "visible_result",
            "write_status",
            "confirmation_token",
            "pending_action",
            "code",
            "retryable",
            "degraded",
            "response",
            "persisted",
        }
    for value in (outcome, failure):
        payload = runtime_outcome_payload(value)
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "safe-message" in encoded or "safe-failure" in encoded
        assert "prepared-canary" not in encoded


def test_canary_private_values_do_not_enter_journal_trace_sse_or_error_log_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    from offerpilot.ai.agent_contracts import PendingAction
    from offerpilot.ai.write_operations import LedgerKeyDomain, WriteOperationRepository
    from offerpilot.chat_transport import (
        SyncAgentExecutionHost,
        prepared_stream_metadata,
        runtime_sse_content,
        runtime_sse_envelope,
    )
    from offerpilot.db import init_database
    from offerpilot.diagnostics import read_recent_log_entries
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
    from offerpilot.repositories.chat import ChatRepository
    from sqlalchemy.exc import SQLAlchemyError

    sentinel = "pilot-runtime-private-canary-6f4b"

    import offerpilot.agent_runtime.trace as trace_module
    import offerpilot.chat_transport as transport_module
    import offerpilot.diagnostics as diagnostics_module

    captured_sse: list[tuple[object, str]] = []
    original_encode_sse_event = transport_module.encode_sse_event

    def capture_sse_event(*args: object, **kwargs: object) -> str:
        encoded = original_encode_sse_event(*args, **kwargs)
        if args:
            captured_sse.append((args[0], encoded))
        return encoded

    monkeypatch.setattr(transport_module, "encode_sse_event", capture_sse_event)
    captured_traces: list[object] = []
    original_reconstruct = trace_module.reconstruct_agent_run

    def capture_trace(*args: object, **kwargs: object) -> object:
        trace = original_reconstruct(*args, **kwargs)
        captured_traces.append(trace)
        return trace

    monkeypatch.setattr(trace_module, "reconstruct_agent_run", capture_trace)
    captured_log_calls: list[tuple[Path, str, str]] = []
    original_append_log_entry = diagnostics_module.append_log_entry

    def capture_log_entry(data_dir: Path, level: str, message: str) -> None:
        captured_log_calls.append((data_dir, level, message))
        original_append_log_entry(data_dir, level, message)

    monkeypatch.setattr(diagnostics_module, "append_log_entry", capture_log_entry)

    class _PrivateCanary:
        def __repr__(self) -> str:
            return sentinel

    nested_private = MappingProxyType({"nested": MappingProxyType({"internal": _PrivateCanary()})})
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
            evidence=(MappingProxyType({"nested": nested_private}),),
        )
    with pytest.raises(TypeError):
        MessageOutcome(
            message="public outcome",
            undo=MappingProxyType({"nested": nested_private}),
        )
    with pytest.raises(TypeError):
        RuntimeFailureOutcome(
            code=RuntimeFailureCode.AI_PROVIDER_ERROR,
            message="public failure",
            pending_action=_PrivateCanary(),  # type: ignore[arg-type]
        )
    from offerpilot.pilot_runtime import PendingActionPayload

    with pytest.raises(TypeError):
        PendingActionPayload(
            tool_name="get_offer",
            operation_id="operation-private",
            human="public pending",
            args=MappingProxyType({"nested": nested_private}),
            confirmation_token="token-private",
        )

    # Every internal category is deliberately kept inside the opaque prepared
    # state.  The transport may read only its public envelope attributes.
    session_factory = init_database(tmp_path / "privacy-boundary.db")
    session = session_factory()
    journal_repository = AgentRunRepository(session_factory)
    ledger_repository = WriteOperationRepository(
        session_factory,
        LedgerKeyDomain("22222222-2222-4222-8222-222222222222", b"l" * 32),
    )

    class _SpyJournalRepository:
        """Capture the real Journal repository calls without changing storage."""

        def __init__(self, delegate: AgentRunRepository) -> None:
            self._delegate = delegate
            self.appended: list[object] = []
            self.dispositions: list[object] = []

        def append_event(self, *args: object, **kwargs: object) -> object:
            if len(args) >= 2:
                self.appended.append(args[1])
            return self._delegate.append_event(*args, **kwargs)  # type: ignore[arg-type]

        def converge_disposition(self, *args: object, **kwargs: object) -> object:
            if len(args) >= 2:
                self.dispositions.append(args[1])
            return self._delegate.converge_disposition(*args, **kwargs)  # type: ignore[arg-type]

        def __getattr__(self, name: str) -> object:
            return getattr(self._delegate, name)

    spy_journal = _SpyJournalRepository(journal_repository)
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
            transport_module.encode_sse_event(
                event,
                seq=index,
                run_id="transport-private",
                envelope=envelope,
            )
            for index, event in enumerate(
                (tool_call, tool_result, CompletedEvent(response=outcome)), 1
            )
        )
        assert "public tool call" in sse
        assert "public tool result" in sse

        stream_outcomes: list[object] = []

        class _Runtime:
            def execute_prepared_stream(self, prepared_value: object, **kwargs: object) -> object:
                del prepared_value
                sink = kwargs["event_sink"]
                assert hasattr(sink, "emit")
                sink.emit(tool_call)  # type: ignore[union-attr]
                sink.emit(tool_result)  # type: ignore[union-attr]
                return outcome

        stream_control = InMemoryRuntimeInvocationControl()
        streamed = "".join(
            runtime_sse_content(
                _Runtime(),
                prepared,
                stream_control,
                None,
                "transport-private-runtime",
                envelope,
                stream_outcomes.append,
            )
        )
        assert stream_outcomes == [outcome]
        assert "public tool call" in streamed
        assert "public tool result" in streamed
        assert captured_sse and all(sentinel not in payload for _, payload in captured_sse)

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

        # Exercise the real Loop/ORM/Pending/Ledger boundaries.  The opaque
        # prepared handle is allowed to exist in Runtime state, but each
        # domain surface either rejects it or receives only its public string
        # projection.
        from offerpilot.ai.agent_contracts import AgentAssistantDelta
        from offerpilot.ai.write_operations import WriteOperationError

        for _internal_name, internal_value in private_state.internal.items():
            with pytest.raises(TypeError):
                json.dumps({"internal": internal_value})

        loop_event = AgentAssistantDelta(delta="public delta")
        with pytest.raises(TypeError):
            json.dumps({"event": loop_event})

        chat_repository = ChatRepository(session_factory)
        with pytest.raises((SQLAlchemyError, TypeError, ValueError)):
            chat_repository.append_message(
                conversation_id,
                "assistant",
                content=prepared,  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError):
            PendingAction(
                tool_call_id="call-private-boundary",
                tool_name="get_offer",
                args=prepared,  # type: ignore[arg-type]
                human="public pending",
            )
        assert chat_repository.get_pending_action(conversation_id) is None

        with session_factory() as ledger_session:
            with pytest.raises(WriteOperationError):
                ledger_repository.create_primary(
                    ledger_session,
                    operation_id=str(uuid4()),
                    conversation_id=conversation_id,
                    tool_call_id="call-private-ledger",
                    tool_name=prepared,  # type: ignore[arg-type]
                    adapter_kind="typed",
                    proposal_fingerprint="hmac-sha256:" + "a" * 64,
                    confirmation_token_fingerprint="hmac-sha256:" + "b" * 64,
                )
            ledger_operation = ledger_repository.create_primary(
                ledger_session,
                operation_id=str(uuid4()),
                conversation_id=conversation_id,
                tool_call_id="call-public-ledger",
                tool_name="create_application",
                adapter_kind="typed",
                proposal_fingerprint="hmac-sha256:" + "c" * 64,
                confirmation_token_fingerprint="hmac-sha256:" + "d" * 64,
                authorization_scope_fingerprint="hmac-sha256:" + "e" * 64,
            )
            ledger_session.commit()
            assert ledger_operation.tool_name == "create_application"

        safe_message = chat_repository.append_message(
            conversation_id,
            "assistant",
            content=outcome.message,
        )
        assert safe_message.content == outcome.message
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
            spy_journal,
            key=key,
            enabled=True,
            segment_budget_seconds=10.0,
            disposition_budget_seconds=2.0,
        ).start_run(command)
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
        with pytest.raises(JournalEventValidationError):
            prepare_event(
                event_type="tool.proposed",
                execution_segment_id=segment_id,
                facts={
                    "tool_call_id": "call-private-nested",
                    "tool_name": "get_offer",
                    "tool_kind": "read",
                    "args_shape_digest": MappingProxyType({"nested": nested_private}),
                    "proposal_outcome": "execution_allowed",
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
        trace = trace_module.reconstruct_agent_run(
            spy_journal,
            run_id,
            as_of=datetime.now(timezone.utc),
            stale_after=timedelta(minutes=5),
        )
        trace_blob = json.dumps(asdict(trace), ensure_ascii=False, default=str)

        import offerpilot.pilot_runtime.composition as composition_module

        composition_module._append_log(
            tmp_path,
            "ERROR",
            repr(private_state.internal["exception"]),
        )
        composition_module._append_log(
            tmp_path,
            "ERROR",
            "runtime_failure "
            + json.dumps(
                runtime_outcome_payload(
                    RuntimeFailureOutcome(
                        code=RuntimeFailureCode.AI_PROVIDER_ERROR,
                        message="public failure",
                        conversation_id=7,
                    )
                ),
                ensure_ascii=False,
            ),
        )
        log_blob = json.dumps(read_recent_log_entries(tmp_path), ensure_ascii=False)
        serialized = json.dumps(
            {"events": event_payload, "sse": sse, "trace": trace_blob, "logs": log_blob},
            ensure_ascii=False,
        )
        assert sentinel not in serialized
        assert captured_traces == [trace]
        assert spy_journal.appended
        assert all(sentinel not in repr(item) for item in spy_journal.appended)
        assert captured_log_calls
        assert all(sentinel not in message for _, _, message in captured_log_calls)
        assert all(sentinel not in encoded for _, encoded in captured_sse)
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
        session_factory.kw["bind"].dispose()
