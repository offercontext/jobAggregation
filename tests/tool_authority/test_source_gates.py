from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "offerpilot"

_FORBIDDEN_SYMBOLS = {
    "ExecutionAuthorization",
    "_project_injected_surface",
    "injected_surface_v1",
    "model_tool_context",
    "typed_to_legacy",
    "typed_catalog_drift",
}
_CONTEXT_CONSTRUCTION_OWNERS = {
    SRC / "pilot_runtime" / "composition.py",
}
_CONTEXT_CONSTRUCTION_FUNCTIONS = {"resolve", "resolve_approval_context"}


class SourceGateViolation(AssertionError):
    pass


def _tree(source: str, *, filename: str = "<gate>") -> ast.Module:
    return ast.parse(source, filename=filename)


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        before = tuple(sorted(aliases.items()))
        if before in seen:
            break
        seen.add(before)
        changed = False
        for node in ast.walk(tree):
            bindings: tuple[tuple[str, str], ...] = ()
            if isinstance(node, ast.ImportFrom):
                bindings = tuple((item.asname or item.name, item.name) for item in node.names)
            elif isinstance(node, ast.Import):
                bindings = tuple(
                    (
                        item.asname or item.name.split(".", 1)[0],
                        item.name.rsplit(".", 1)[-1],
                    )
                    for item in node.names
                )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                terminal = _terminal(node.value)
                if terminal is not None:
                    resolved = aliases.get(terminal, terminal)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    bindings = tuple(
                        (target.id, resolved) for target in targets if isinstance(target, ast.Name)
                    )
            for local, source in bindings:
                if aliases.get(local) != source:
                    aliases[local] = source
                    changed = True
        if not changed:
            break
    return aliases


def _resolved_terminal(node: ast.AST, aliases: dict[str, str]) -> str | None:
    terminal = _terminal(node)
    return aliases.get(terminal, terminal) if terminal is not None else None


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        before = tuple(sorted(bindings.items()))
        if before in seen:
            break
        seen.add(before)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            resolved = _constant_string(node.value, bindings)
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


def _reflected_attribute(
    node: ast.Call,
    aliases: dict[str, str],
    strings: dict[str, str],
) -> tuple[ast.AST, str] | None:
    terminal = _resolved_terminal(node.func, aliases)
    if terminal in {"getattr", "setattr"} and len(node.args) >= 2:
        field = _constant_string(node.args[1], strings)
        return None if field is None else (node.args[0], field)
    if terminal == "__getattribute__" and isinstance(node.func, ast.Attribute):
        if len(node.args) >= 2:
            receiver, field_node = node.args[0], node.args[1]
        elif node.args:
            receiver, field_node = node.func.value, node.args[0]
        else:
            return None
        field = _constant_string(field_node, strings)
        return None if field is None else (receiver, field)
    if terminal == "__setattr__" and isinstance(node.func, ast.Attribute):
        if len(node.args) >= 3:
            receiver, field_node = node.args[0], node.args[1]
        elif len(node.args) >= 2:
            receiver, field_node = node.func.value, node.args[0]
        else:
            return None
        field = _constant_string(field_node, strings)
        return None if field is None else (receiver, field)
    return None


def _call_terminal(
    node: ast.Call,
    aliases: dict[str, str],
    strings: dict[str, str],
) -> str | None:
    terminal = _resolved_terminal(node.func, aliases)
    if terminal is not None:
        return terminal
    if not isinstance(node.func, ast.Call):
        return None
    if _resolved_terminal(node.func.func, aliases) != "getattr" or len(node.func.args) < 2:
        return None
    symbol = node.func.args[1]
    return _constant_string(symbol, strings)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bound_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for item in target.elts for name in _bound_names(item)}
    return set()


def _value_aliases(tree: ast.AST, roots: set[str]) -> set[str]:
    aliases = set(roots)
    while True:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if _root_name(node.value) not in aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
        if not changed:
            break
    return aliases


def _request_private_field(
    node: ast.AST,
    *,
    request_roots: set[str] | None = None,
) -> str | None:
    sensitive = {"capabilities", "capability", "current_binding", "current_bindings"}
    if request_roots is None:
        request_roots = {
            "body",
            "input",
            "page_context",
            "payload",
            "request",
            "request_payload",
        }
    if isinstance(node, ast.Attribute):
        root = _root_name(node.value)
        if root in request_roots and node.attr in sensitive:
            return node.attr
    if isinstance(node, ast.Subscript):
        root = _root_name(node.value)
        key = node.slice
        if root in request_roots and isinstance(key, ast.Constant) and key.value in sensitive:
            return str(key.value)
    if (
        isinstance(node, ast.Call)
        and _terminal(node.func) in {"get", "getattr"}
        and len(node.args) >= 1
    ):
        if _terminal(node.func) == "getattr" and len(node.args) >= 2:
            owner, key = node.args[0], node.args[1]
        elif isinstance(node.func, ast.Attribute):
            owner, key = node.func.value, node.args[0]
        else:
            return None
        if (
            _root_name(owner) in request_roots
            and isinstance(key, ast.Constant)
            and key.value in sensitive
        ):
            return str(key.value)
    return None


def _iterates_whole_tool_capability(node: ast.AST, aliases: dict[str, str]) -> bool:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id) == "ToolCapability"
    if isinstance(node, ast.Call):
        return any(
            isinstance(argument, ast.Name)
            and aliases.get(argument.id, argument.id) == "ToolCapability"
            for argument in node.args
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return any(
            isinstance(generator.iter, ast.Name)
            and aliases.get(generator.iter.id, generator.iter.id) == "ToolCapability"
            for generator in node.generators
        )
    return False


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _direct_method_owner(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> tuple[str | None, str]:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Lambda):
            return None, "<lambda>"
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent = parents.get(current)
            owner = (
                parent.name
                if isinstance(parent, ast.ClassDef)
                else "<module>"
                if isinstance(parent, ast.Module)
                else None
            )
            return owner, current.name
        current = parents.get(current)
    return None, "<module>"


def _statically_unreachable(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    owner: ast.FunctionDef,
) -> bool:
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if parent is not owner:
                return True
        if isinstance(parent, ast.If) and isinstance(parent.test, ast.Constant):
            truth = bool(parent.test.value)
            if current in parent.body and not truth:
                return True
            if current in parent.orelse and truth:
                return True
        if isinstance(parent, ast.While) and isinstance(parent.test, ast.Constant):
            if current in parent.body and not bool(parent.test.value):
                return True
        for branch_name in ("body", "orelse", "finalbody"):
            branch = getattr(parent, branch_name, None)
            if not isinstance(branch, list) or current not in branch:
                continue
            position = branch.index(current)
            if any(isinstance(item, (ast.Raise, ast.Return)) for item in branch[:position]):
                return True
        current = parent
    return False


def _nested_in_other_callable(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    owner: ast.FunctionDef,
) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return current is not owner
    return False


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _terminal(child.func) == name
    ]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(found) != 1:
        raise SourceGateViolation(f"function:{name}")
    return found[0]


def _validate_global_forbidden_paths(path: Path, source: str) -> None:
    tree = _tree(source, filename=str(path))
    aliases = _import_aliases(tree)
    strings = _string_bindings(tree)
    parents = _parent_map(tree)
    request_roots = _value_aliases(
        tree,
        {
            "body",
            "input",
            "page_context",
            "payload",
            "request",
            "request_payload",
        },
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_SYMBOLS:
            raise SourceGateViolation(f"forbidden-symbol:{node.id}:{path.name}")
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SYMBOLS:
            raise SourceGateViolation(f"forbidden-attribute:{node.attr}:{path.name}")
        if (
            isinstance(node, ast.Call)
            and _call_terminal(node, aliases, strings) in {"frozenset", "set", "tuple"}
            and node.args
            and _iterates_whole_tool_capability(node.args[0], aliases)
        ):
            raise SourceGateViolation(f"automatic-capability-grant:{path.name}")
        if (
            isinstance(node, ast.Call)
            and _call_terminal(node, aliases, strings) == "ToolExecutionContext"
            and (
                path not in _CONTEXT_CONSTRUCTION_OWNERS
                or _enclosing_function(node, parents) not in _CONTEXT_CONSTRUCTION_FUNCTIONS
            )
        ):
            raise SourceGateViolation(f"context-construction-owner:{path.name}")
        if isinstance(node, ast.Call):
            dynamic_terminal = _call_terminal(node, aliases, strings)
            if dynamic_terminal in _FORBIDDEN_SYMBOLS:
                raise SourceGateViolation(
                    f"forbidden-dynamic-symbol:{dynamic_terminal}:{path.name}"
                )
        if (field := _request_private_field(node, request_roots=request_roots)) is not None:
            raise SourceGateViolation(f"request-derived-authority:{field}:{path.name}")

        # The old bridge was an instance attribute on a value named
        # ``resolved``/``resolved_model``.  Checking only the impossible class
        # spelling ``ResolvedModel.tool_context`` leaves the real regression
        # unguarded.
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "tool_context"
            and _root_name(node.value) in {"resolved", "resolved_model"}
        ):
            raise SourceGateViolation(f"resolved-model-tool-context:{path.name}")


def _call_line(
    function: ast.FunctionDef,
    name: str,
    *,
    reject_conditional: bool = False,
) -> int:
    calls: list[ast.Call] = []
    parents = _parent_map(function)

    class OuterBodyVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.conditional_depth = 0

        def visit_Call(self, node: ast.Call) -> None:
            if _terminal(node.func) == name:
                if reject_conditional and self.conditional_depth:
                    raise SourceGateViolation(f"conditional-call:{function.name}:{name}")
                if _statically_unreachable(node, parents, function):
                    raise SourceGateViolation(f"unreachable-call:{function.name}:{name}")
                calls.append(node)
            self.generic_visit(node)

        def _visit_conditional(self, node: ast.AST) -> None:
            self.conditional_depth += 1
            self.generic_visit(node)
            self.conditional_depth -= 1

        visit_If = _visit_conditional
        visit_For = _visit_conditional
        visit_While = _visit_conditional
        visit_Match = _visit_conditional
        visit_IfExp = _visit_conditional
        visit_ListComp = _visit_conditional
        visit_SetComp = _visit_conditional
        visit_DictComp = _visit_conditional
        visit_GeneratorExp = _visit_conditional

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                for statement in node.body:
                    self.visit(statement)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            del node

    OuterBodyVisitor().visit(function)
    if not calls:
        raise SourceGateViolation(f"missing-call:{function.name}:{name}")
    return min(call.lineno for call in calls)


def _validate_pipeline_order(source: str) -> None:
    tree = _tree(source)
    prepare = _function(tree, "prepare_call")
    if not (
        _call_line(prepare, "require_authority_phase", reject_conditional=True)
        < _call_line(prepare, "resolve", reject_conditional=True)
        < _call_line(prepare, "require_authority_spec", reject_conditional=True)
        < _call_line(prepare, "_require_entry_capabilities", reject_conditional=True)
        < _call_line(prepare, "_pre_resolver_entry_scope_policy", reject_conditional=True)
        < _call_line(prepare, "_audit_entry_bindings", reject_conditional=True)
        < _call_line(prepare, "prepare_tool_call", reject_conditional=True)
    ):
        raise SourceGateViolation("prepare-order")

    execute = _function(tree, "execute_prepared")
    if not (
        _call_line(execute, "require_prepared_route", reject_conditional=True)
        < _call_line(execute, "require_authority_phase", reject_conditional=True)
        < _call_line(execute, "require_execution_claim_transaction")
        < _call_line(execute, "begin_prepared_execution", reject_conditional=True)
    ):
        raise SourceGateViolation("execution-authority-order")

    execute_read = _function(tree, "_execute_read")
    if not (
        _call_line(execute_read, "_require_entry_capabilities", reject_conditional=True)
        < _call_line(execute_read, "_audit_entry_bindings", reject_conditional=True)
        < _call_line(execute_read, "executor", reject_conditional=True)
    ):
        raise SourceGateViolation("read-order")

    claimed_write = _function(tree, "_execute_claimed_write")
    if not (
        _call_line(claimed_write, "claim_lifecycle", reject_conditional=True)
        < _call_line(claimed_write, "_typed_args_digest", reject_conditional=True)
        < _call_line(claimed_write, "executor", reject_conditional=True)
    ):
        raise SourceGateViolation("claimed-write-order")


def _validate_provider_identity_keywords(source: str) -> None:
    tree = _tree(source)
    complete_model = _function(tree, "complete_model")
    aliases = _import_aliases(tree)
    parents = _parent_map(tree)
    provider_calls = {
        "preflight",
        "complete_surface",
        "stream_surface",
    }
    found: set[str] = set()
    for node in ast.walk(complete_model):
        if not isinstance(node, ast.Call):
            continue
        terminal = _resolved_terminal(node.func, aliases)
        if terminal not in provider_calls:
            continue
        if _nested_in_other_callable(node, parents, complete_model):
            raise SourceGateViolation(f"provider-invocation-nested:{terminal}")
        if _statically_unreachable(node, parents, complete_model):
            continue
        found.add(terminal)
        identities = [
            keyword.value for keyword in node.keywords if keyword.arg == "invocation_identity"
        ]
        if (
            len(identities) != 1
            or not isinstance(identities[0], ast.Name)
            or identities[0].id != "invocation_identity"
        ):
            raise SourceGateViolation(f"provider-invocation-identity:{_terminal(node.func)}")
    if found != provider_calls:
        raise SourceGateViolation(f"provider-invocation-paths:{sorted(found)}")


def _validate_chat_read_paths(source: str) -> None:
    tree = _tree(source)
    mutating_calls = {
        "add",
        "commit",
        "create_primary",
        "delete",
        "flush",
        "merge",
        "persist_typed_pending",
    }
    mutating_fragments = ("backfill", "create_", "insert", "persist_", "replace_", "update_")
    for name in ("get_conversation", "list_conversations", "get_pending_action"):
        function = _function(tree, name)
        found = {
            terminal
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            if (terminal := _terminal(call.func)) is not None
            if terminal in mutating_calls
            or any(fragment in terminal for fragment in mutating_fragments)
        }
        if found:
            raise SourceGateViolation(f"lazy-ledger-backfill:{name}:{sorted(found)[0]}")


def _validate_typed_pending_routes(source: str) -> None:
    tree = _tree(source)
    persist = _function(tree, "persist_typed_pending")
    argument_names = {argument.arg for argument in persist.args.args}
    if "pending_authority_claim" not in argument_names:
        raise SourceGateViolation("typed-pending-claim-parameter")
    if not _calls(persist, "_validate_typed_pending"):
        raise SourceGateViolation("typed-pending-claim-validation")
    validation_line = _call_line(persist, "_validate_typed_pending")
    for call in (item for item in ast.walk(persist) if isinstance(item, ast.Call)):
        terminal = _terminal(call.func) or ""
        if "legacy" in terminal:
            raise SourceGateViolation("typed-pending-legacy-fallback")
        if terminal in {"add", "commit", "create_primary", "flush", "merge"} and (
            call.lineno < validation_line
        ):
            raise SourceGateViolation("typed-pending-validation-order")


def _validate_scope_write_routes(source: str) -> None:
    tree = _tree(source)
    create = _function(tree, "create_conversation")
    create_arguments = {item.arg for item in (*create.args.args, *create.args.kwonlyargs)}
    assert {"mode", "context_type", "context_ref"}.issubset(create_arguments)
    create_source = ast.unparse(create)
    if "_SCOPE_ARGUMENT_MISSING" not in create_source:
        raise SourceGateViolation("generic-create-scope-guard")

    update = _function(tree, "update_conversation")
    guard_line = _call_line(update, "intersection")
    setattr_line = _call_line(update, "setattr")
    if guard_line >= setattr_line:
        raise SourceGateViolation("generic-update-scope-guard")

    patch = _function(tree, "patch_conversation_with_scope")
    patch_names = {item.arg for item in (*patch.args.args, *patch.args.kwonlyargs)}
    if not {"mutation", "expected_scope_revision"}.issubset(patch_names):
        raise SourceGateViolation("scope-cas-port")


def _validate_no_transient_generic_serializers(source: str) -> None:
    tree = _tree(source)
    aliases = _import_aliases(tree)
    transient_names = {
        "ApplicationScopeConstraint",
        "ApprovalExecutionAuthority",
        "AuthorityCallIdentity",
        "ExecutionClaim",
        "PendingAuthorityClaim",
        "PreparedToolCall",
        "SegmentExecutionAuthority",
        "ToolExecutionAuthority",
        "TransientToolRuntimeValue",
        "TrustedLedgerOmittedTokenProof",
    }
    forbidden_calls = {"asdict", "copy", "deepcopy", "dumps", "replace", "checkpoint"}

    def callable_nodes(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[ast.AST]:
        result: list[ast.AST] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if node is function:
                    result.append(node)
                    for statement in node.body:
                        self.visit(statement)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Lambda(self, node: ast.Lambda) -> None:
                del node

            def generic_visit(self, node: ast.AST) -> None:
                result.append(node)
                super().generic_visit(node)

        Visitor().visit(function)
        return result

    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    nodes_by_function = {id(function): callable_nodes(function) for function in functions}
    parents = _parent_map(tree)

    def serializer_result_is_returned(
        call: ast.Call,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        current: ast.AST = call
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.Return):
                return True
            if isinstance(current, (ast.Assign, ast.AnnAssign)):
                return False
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current is not function
        return False

    serializer_helpers: dict[str, dict[str, int | None]] = {}
    for function in functions:
        nodes = nodes_by_function[id(function)]
        positional = (*function.args.posonlyargs, *function.args.args)
        parameters = {item.arg for item in (*positional, *function.args.kwonlyargs)}
        parameter_sources = {parameter: {parameter} for parameter in parameters}
        while True:
            changed = False
            for assignment in nodes:
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                    continue
                if assignment.value is None:
                    continue
                sources = {
                    source
                    for item in ast.walk(assignment.value)
                    if isinstance(item, ast.Name)
                    for source in parameter_sources.get(item.id, set())
                }
                if not sources:
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    previous = parameter_sources.get(target.id, set())
                    updated = previous | sources
                    if updated != previous:
                        parameter_sources[target.id] = updated
                        changed = True
            if not changed:
                break
        serialized_parameters: set[str] = set()
        for call in nodes:
            if (
                not isinstance(call, ast.Call)
                or _resolved_terminal(call.func, aliases) not in forbidden_calls
                or not call.args
                or not serializer_result_is_returned(call, function)
            ):
                continue
            serialized_parameters.update(
                source
                for item in ast.walk(call.args[0])
                if isinstance(item, ast.Name)
                for source in parameter_sources.get(item.id, set())
            )
        if serialized_parameters:
            serializer_helpers[function.name] = {
                parameter: next(
                    (
                        index
                        for index, candidate in enumerate(positional)
                        if candidate.arg == parameter
                    ),
                    None,
                )
                for parameter in serialized_parameters
            }

    def annotation_is_transient(annotation: ast.AST | None) -> bool:
        return annotation is not None and any(
            isinstance(item, ast.Name) and item.id in transient_names
            for item in ast.walk(annotation)
        )

    def contains_bare_tainted_name(argument: ast.AST, tainted: set[str]) -> bool:
        return any(
            isinstance(item, ast.Name)
            and item.id in tainted
            and not (
                isinstance(parents.get(item), ast.Attribute) and parents[item].value is item  # type: ignore[union-attr]
            )
            for item in ast.walk(argument)
        )

    for function in functions:
        nodes = nodes_by_function[id(function)]
        tainted = {
            parameter.arg
            for parameter in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
            if annotation_is_transient(parameter.annotation)
        }
        tainted.update(
            node.target.id
            for node in nodes
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and annotation_is_transient(node.annotation)
        )
        while True:
            changed = False
            for assignment in nodes:
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                    continue
                if assignment.value is None:
                    continue
                value_tainted = contains_bare_tainted_name(
                    assignment.value,
                    tainted,
                ) or any(
                    isinstance(item, ast.Call)
                    and _resolved_terminal(item.func, aliases) in transient_names
                    for item in ast.walk(assignment.value)
                )
                if not value_tainted:
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True
            if not changed:
                break

        def argument_is_tainted(argument: ast.AST) -> bool:
            return contains_bare_tainted_name(argument, tainted) or any(
                isinstance(item, ast.Call)
                and _resolved_terminal(item.func, aliases) in transient_names
                for item in ast.walk(argument)
            )

        for call in nodes:
            if not isinstance(call, ast.Call):
                continue
            terminal = _resolved_terminal(call.func, aliases)
            if terminal in forbidden_calls and call.args:
                if argument_is_tainted(call.args[0]):
                    raise SourceGateViolation(
                        f"transient-generic-serialization:{function.name}:{terminal}"
                    )
            parameters = serializer_helpers.get(terminal or "")
            if not parameters:
                continue
            for parameter, index in parameters.items():
                if index is not None and index < len(call.args):
                    if argument_is_tainted(call.args[index]):
                        raise SourceGateViolation(
                            "transient-generic-serialization:"
                            f"{function.name}:{terminal}:{parameter}"
                        )
                if any(
                    keyword.arg == parameter and argument_is_tainted(keyword.value)
                    for keyword in call.keywords
                ):
                    raise SourceGateViolation(
                        f"transient-generic-serialization:{function.name}:{terminal}:{parameter}"
                    )


def _validate_authority_callsite_ownership(paths: dict[Path, str]) -> None:
    owners = {
        "create_approved_write_execute_identity": {
            SRC / "ai" / "tool_authority" / "composition.py",
            SRC / "ai" / "write_operations.py",
        },
        "create_read_execution_identity": {
            SRC / "ai" / "agent_loop.py",
            SRC / "ai" / "tool_authority" / "composition.py",
        },
        "create_typed_pending_identity": {
            SRC / "ai" / "agent_loop.py",
            SRC / "ai" / "tool_authority" / "composition.py",
        },
        "issue_pending_claim": {
            SRC / "ai" / "agent_loop.py",
            SRC / "ai" / "tool_authority" / "composition.py",
        },
        "persist_typed_pending": {
            SRC / "repositories" / "chat.py",
        },
    }
    for path, source in paths.items():
        tree = _tree(source, filename=str(path))
        aliases = _import_aliases(tree)
        parents = _parent_map(tree)
        sensitive_dispatch = set(owners) | {
            "complete_surface",
            "execute_prepared",
            "preflight",
            "select_tools",
            "stream_surface",
        }
        for mapping in (item for item in ast.walk(tree) if isinstance(item, ast.Dict)):
            dynamic_values = {
                _resolved_terminal(value, aliases) for value in mapping.values if value is not None
            }
            if dynamic_values.intersection(sensitive_dispatch):
                raise SourceGateViolation("dynamic-authority-dispatch")
        approval_aliases = _value_aliases(
            tree,
            {"approval", "approval_authority", "approval_context"},
        )
        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            terminal = _resolved_terminal(call.func, aliases)
            if terminal in owners and path not in owners[terminal]:
                raise SourceGateViolation(f"authority-call-owner:{terminal}:{path.name}")
            if terminal == "persist_typed_pending":
                has_claim = len(call.args) >= 4 or any(
                    keyword.arg == "pending_authority_claim" for keyword in call.keywords
                )
                if not has_claim:
                    raise SourceGateViolation("typed-pending-call-without-claim")
            if terminal in {
                "complete_surface",
                "create_read_execution_identity",
                "create_typed_pending_identity",
                "issue_pending_claim",
                "preflight",
                "select_tools",
                "stream_surface",
            }:
                for argument in (*call.args, *(item.value for item in call.keywords)):
                    if _root_name(argument) in approval_aliases:
                        raise SourceGateViolation("approval-authority-provider")
            if terminal == "create_approved_write_execute_identity" and any(
                isinstance(argument, ast.Name) and argument.id in {"segment", "segment_authority"}
                for argument in (*call.args, *(item.value for item in call.keywords))
            ):
                raise SourceGateViolation("segment-approved-write")
            if terminal == "execute_prepared":
                function = _enclosing_function(call, parents)
                keyword_names = {item.arg for item in call.keywords}
                if path == SRC / "ai" / "write_operations.py":
                    if "execution_claim" not in keyword_names:
                        raise SourceGateViolation("typed-write-without-execution-claim")
                elif path == SRC / "ai" / "agent_loop.py":
                    if function == "_bootstrap_approved":
                        if "confirmation_claimer" not in keyword_names:
                            raise SourceGateViolation("approved-write-without-claim-port")
                    elif function == "_dispatch":
                        if "call_identity" not in keyword_names:
                            raise SourceGateViolation("read-without-call-identity")
                    else:
                        raise SourceGateViolation(f"execute-prepared-owner:{function or 'module'}")
                elif path != SRC / "ai" / "tool_runtime" / "pipeline.py":
                    raise SourceGateViolation(f"execute-prepared-owner:{path.name}")


def _validate_scoped_repository_ports(path: Path, source: str) -> None:
    tree = _tree(source, filename=str(path))
    allowed_self_helpers = {"_require_scoped", "_visible_note_statement"}
    for function in (
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef)
        and item.name.endswith("_scoped")
        and item.name not in {"_require_scoped", "bind_scoped"}
    ):
        parameters = (*function.args.args, *function.args.kwonlyargs)
        constraint = [item for item in parameters if item.arg == "constraint"]
        if len(constraint) != 1:
            raise SourceGateViolation(f"scoped-constraint-required:{function.name}")
        if not _calls(function, "_require_scoped"):
            raise SourceGateViolation(f"scoped-guard-required:{function.name}")
        indirect_self_calls: set[str] = set()
        for assignment in (
            item
            for item in ast.walk(function)
            if isinstance(item, (ast.Assign, ast.AnnAssign))
            and item.value is not None
            and isinstance(item.value, ast.Attribute)
            and _root_name(item.value.value) == "self"
            and item.value.attr not in allowed_self_helpers
        ):
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            indirect_self_calls.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
        for call in (item for item in ast.walk(function) if isinstance(item, ast.Call)):
            direct_allowed_helper = (
                isinstance(call.func, ast.Attribute)
                and _root_name(call.func.value) == "self"
                and call.func.attr in allowed_self_helpers
            )
            if not direct_allowed_helper and any(
                isinstance(item, ast.Name) and item.id == "self" for item in ast.walk(call.func)
            ):
                raise SourceGateViolation(f"scoped-unscoped-fallback:{function.name}:reflection")
            if (
                isinstance(call.func, ast.Attribute)
                and _root_name(call.func.value) == "self"
                and call.func.attr not in allowed_self_helpers
            ):
                raise SourceGateViolation(
                    f"scoped-unscoped-fallback:{function.name}:{call.func.attr}"
                )
            if isinstance(call.func, ast.Name) and call.func.id in indirect_self_calls:
                raise SourceGateViolation(
                    f"scoped-unscoped-fallback:{function.name}:{call.func.id}"
                )
            if (
                isinstance(call.func, ast.Call)
                and _terminal(call.func.func) == "getattr"
                and len(call.func.args) >= 2
                and _root_name(call.func.args[0]) == "self"
            ):
                symbol = call.func.args[1]
                if not isinstance(symbol, ast.Constant) or symbol.value not in allowed_self_helpers:
                    raise SourceGateViolation(f"scoped-unscoped-fallback:{function.name}:getattr")
            if _terminal(call.func) == "filter" and call.args:
                predicate = call.args[0]
                names = {item.id for item in ast.walk(predicate) if isinstance(item, ast.Name)}
                attributes = {
                    item.attr for item in ast.walk(predicate) if isinstance(item, ast.Attribute)
                }
                if names.intersection(
                    {"allowed_id", "allowed_identities", "application_id", "constraint"}
                ) or attributes.intersection(
                    {"allowed_identities", "application_id", "id", "parent_id"}
                ):
                    raise SourceGateViolation(f"scoped-python-post-filter:{function.name}")
        for comprehension in (
            item
            for item in ast.walk(function)
            if isinstance(
                item,
                (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp),
            )
            and item.generators
        ):
            for generator in comprehension.generators:
                for condition in generator.ifs:
                    names = {item.id for item in ast.walk(condition) if isinstance(item, ast.Name)}
                    attributes = {
                        item.attr for item in ast.walk(condition) if isinstance(item, ast.Attribute)
                    }
                    if names.intersection(
                        {
                            "allowed_id",
                            "allowed_identities",
                            "application_id",
                            "constraint",
                        }
                    ) or attributes.intersection(
                        {"allowed_identities", "application_id", "id", "parent_id"}
                    ):
                        raise SourceGateViolation(f"scoped-python-post-filter:{function.name}")
        for loop in (item for item in ast.walk(function) if isinstance(item, ast.For)):
            for condition in (item for item in ast.walk(loop) if isinstance(item, ast.If)):
                names = {item.id for item in ast.walk(condition.test) if isinstance(item, ast.Name)}
                attributes = {
                    item.attr
                    for item in ast.walk(condition.test)
                    if isinstance(item, ast.Attribute)
                }
                if names.intersection(
                    {"allowed_id", "allowed_identities", "application_id", "constraint"}
                ) or attributes.intersection(
                    {"allowed_identities", "application_id", "id", "parent_id"}
                ):
                    raise SourceGateViolation(f"scoped-python-post-filter:{function.name}")
        unscoped_name = function.name.removesuffix("_scoped")
        if _calls(function, unscoped_name):
            raise SourceGateViolation(f"scoped-unscoped-fallback:{function.name}")


def _validate_binding_resolver_purity(source: str) -> None:
    tree = _tree(source)
    resolver_names = {
        "_authority_binding_resolution",
        "resolve_identity_argument",
        "resolve_parent_application",
    }
    forbidden_calls = {
        "Session",
        "begin",
        "commit",
        "delete",
        "execute",
        "executor",
        "flush",
        "get_session",
        "open",
        "post",
        "request",
        "rollback",
        "write",
    }
    forbidden_roots = {
        "client",
        "db",
        "engine",
        "http",
        "httpx",
        "keyring",
        "path",
        "provider",
        "repo",
        "repository",
        "requests",
        "session",
    }
    for name in resolver_names:
        function = _function(tree, name)
        root_aliases = _value_aliases(function, set(forbidden_roots))
        found = {
            _terminal(call.func) or "unknown"
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and (_terminal(call.func) in forbidden_calls or _root_name(call.func) in root_aliases)
        }
        if found:
            raise SourceGateViolation(f"binding-resolver-side-effect:{name}:{sorted(found)[0]}")


def _validate_dependency_policy_and_autoapprove(source: str) -> None:
    tree = _tree(source)
    aliases = _import_aliases(tree)
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _resolved_terminal(node.func, aliases) == "DependencyPolicyV1"
    ]
    if constructors:
        raise SourceGateViolation("copied-dependency-policy")

    autoapprove_aliases = {"auto_approve"}

    def contains_autoapprove(node: ast.AST) -> bool:
        return any(
            (
                isinstance(item, ast.Name)
                and (
                    aliases.get(item.id, item.id) == "auto_approve"
                    or item.id in autoapprove_aliases
                )
            )
            or (isinstance(item, ast.Attribute) and item.attr == "auto_approve")
            or (
                isinstance(item, ast.Subscript)
                and isinstance(item.slice, ast.Constant)
                and item.slice.value == "auto_approve"
            )
            or (
                isinstance(item, ast.Call)
                and _resolved_terminal(item.func, aliases) == "getattr"
                and len(item.args) >= 2
                and isinstance(item.args[1], ast.Constant)
                and item.args[1].value == "auto_approve"
            )
            for item in ast.walk(node)
        )

    while True:
        changed = False
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            if assignment.value is None or not contains_autoapprove(assignment.value):
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in autoapprove_aliases:
                    autoapprove_aliases.add(target.id)
                    changed = True
        if not changed:
            break

    controlled_regions: list[tuple[ast.AST, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and contains_autoapprove(node.test):
            controlled_regions.append(tuple(node.body))
        elif isinstance(node, ast.IfExp) and contains_autoapprove(node.test):
            controlled_regions.append((node.body,))
        elif isinstance(node, ast.Match) and contains_autoapprove(node.subject):
            controlled_regions.extend(tuple(case.body) for case in node.cases)
        elif isinstance(
            node,
            (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp),
        ) and any(
            contains_autoapprove(condition)
            for generator in node.generators
            for condition in generator.ifs
        ):
            controlled_regions.append((node,))

    for region in controlled_regions:
        body_calls = {
            _resolved_terminal(item.func, aliases)
            for statement in region
            for item in ast.walk(statement)
            if isinstance(item, ast.Call)
        }
        if body_calls.intersection(
            {"execute_prepared", "issue_execution_claim", "persist_typed_pending"}
        ):
            raise SourceGateViolation("auto-approve-authority-bypass")


def _validate_segment_lease_ownership(paths: dict[Path, str]) -> None:
    ownership = {
        "_open_segment_tool_catalog_lease": (
            SRC / "ai" / "tool_runtime" / "metadata.py",
            frozenset({("ToolMetadataBundleV1", "open_segment_lease")}),
        ),
        "_register_segment_lease": (
            SRC / "ai" / "tool_runtime" / "metadata.py",
            frozenset({("ToolMetadataBundleV1", "open_segment_lease")}),
        ),
        "_require_registered_segment_lease": (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            frozenset(
                {
                    ("SegmentToolCatalogLease", "resolve"),
                    ("SegmentToolCatalogLease", "require_catalog"),
                    ("SegmentToolCatalogLease", "require_spec"),
                    ("SegmentToolCatalogLease", "_require_issued_spec_identity"),
                }
            ),
        ),
        "_require_issued_spec_identity": (
            SRC / "ai" / "tool_authority" / "composition.py",
            frozenset({("AuthorityFactory", "_resolve_registered_route")}),
        ),
        "_require_issued_view_integrity": (
            SRC / "ai" / "tool_authority" / "composition.py",
            frozenset({("AuthorityFactory", "_resolve_registered_route")}),
        ),
        "_revoke_segment_lease": (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            frozenset({("SegmentToolCatalogLease", "close")}),
        ),
    }

    class LeaseCallVisitor(ast.NodeVisitor):
        def __init__(
            self,
            path: Path,
            aliases: dict[str, str],
            parents: dict[ast.AST, ast.AST],
        ) -> None:
            self.path = path
            self.aliases = aliases
            self.parents = parents

        def visit_Call(self, node: ast.Call) -> None:
            terminal = _resolved_terminal(node.func, self.aliases)
            if terminal in ownership:
                expected_path, expected_owners = ownership[terminal]
                owner = _direct_method_owner(node, self.parents)
                if self.path != expected_path or owner not in expected_owners:
                    raise SourceGateViolation(f"segment-lease-owner:{terminal}:{self.path}:{owner}")
            self.generic_visit(node)

    for path, source in paths.items():
        tree = _tree(source, filename=str(path))
        aliases = _import_aliases(tree)
        strings = _string_bindings(tree)
        parents = _parent_map(tree)
        LeaseCallVisitor(path, aliases, parents).visit(tree)

        ownership_callable_names: dict[str, set[str]] = {}

        def ownership_methods(node: ast.AST) -> set[str]:
            if isinstance(node, ast.Name):
                return set(ownership_callable_names.get(node.id, set()))
            if isinstance(node, ast.Attribute) and node.attr in ownership:
                return {node.attr}
            if isinstance(node, ast.Call):
                reflected = _reflected_attribute(node, aliases, strings)
                if reflected is not None:
                    return {reflected[1]} if reflected[1] in ownership else set()
                return set()
            if isinstance(node, (ast.Subscript, ast.Starred, ast.NamedExpr)):
                return ownership_methods(node.value)
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                return set().union(*(ownership_methods(item) for item in node.elts))
            if isinstance(node, ast.Dict):
                return set().union(
                    *(
                        ownership_methods(item)
                        for item in (*node.keys, *node.values)
                        if item is not None
                    )
                )
            if isinstance(node, ast.IfExp):
                return ownership_methods(node.body) | ownership_methods(node.orelse)
            if isinstance(node, ast.BoolOp):
                return set().union(*(ownership_methods(item) for item in node.values))
            return set()

        while True:
            changed = False
            for node in ast.walk(tree):
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    value = node.iter
                    targets = [node.target]
                elif isinstance(node, ast.comprehension):
                    value = node.iter
                    targets = [node.target]
                if value is None:
                    continue
                methods = ownership_methods(value)
                if not methods:
                    continue
                for target in targets:
                    for name in _bound_names(target):
                        before = len(ownership_callable_names.get(name, set()))
                        ownership_callable_names.setdefault(name, set()).update(methods)
                        if len(ownership_callable_names[name]) != before:
                            changed = True
            if not changed:
                break

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            owner = _direct_method_owner(node, parents)
            for method in ownership_methods(node.func):
                expected_path, expected_owners = ownership[method]
                if path != expected_path or owner not in expected_owners:
                    raise SourceGateViolation(f"segment-lease-owner:{method}:{path}:{owner}")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            reflected = _reflected_attribute(node, aliases, strings)
            if reflected is not None and reflected[1] == "spec_handle":
                raise SourceGateViolation(f"dynamic-prepared-spec-handle:{path}:{node.lineno}")

        if path not in {
            SRC / "ai" / "agent_loop.py",
            SRC / "ai" / "tool_runtime" / "pipeline.py",
        }:
            continue

        raw_catalog_names = {"ToolCatalog", "catalog", "tool_catalog"}

        def annotation_is_raw_catalog(annotation: ast.AST | None) -> bool:
            if annotation is None:
                return False
            names = {item.id for item in ast.walk(annotation) if isinstance(item, ast.Name)}
            return "ToolCatalog" in names

        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                if annotation_is_raw_catalog(node.annotation):
                    raw_catalog_names.add(node.arg)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if annotation_is_raw_catalog(node.annotation):
                    raw_catalog_names.add(node.target.id)

        def is_raw_catalog_receiver(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in raw_catalog_names
            if isinstance(node, ast.Attribute):
                return node.attr in {"catalog", "dispatch_catalog", "typed_catalog"} or (
                    node.attr != "catalog_lease" and is_raw_catalog_receiver(node.value)
                )
            if isinstance(node, ast.Subscript):
                return is_raw_catalog_receiver(node.value)
            if isinstance(node, ast.Starred):
                return is_raw_catalog_receiver(node.value)
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                return any(is_raw_catalog_receiver(item) for item in node.elts)
            if isinstance(node, ast.Dict):
                return any(
                    is_raw_catalog_receiver(item)
                    for item in (*node.keys, *node.values)
                    if item is not None
                )
            if isinstance(node, ast.IfExp):
                return is_raw_catalog_receiver(node.body) or is_raw_catalog_receiver(node.orelse)
            if isinstance(node, ast.BoolOp):
                return any(is_raw_catalog_receiver(item) for item in node.values)
            if isinstance(node, ast.NamedExpr):
                return is_raw_catalog_receiver(node.value)
            if isinstance(node, ast.Call):
                terminal = _resolved_terminal(node.func, aliases)
                if terminal in {"list", "set", "tuple", "frozenset"}:
                    return any(is_raw_catalog_receiver(item) for item in node.args)
                if terminal == "cast" and len(node.args) >= 2:
                    return is_raw_catalog_receiver(node.args[1])
                reflected = _reflected_attribute(node, aliases, strings)
                if reflected is not None:
                    return reflected[1] in {"catalog", "dispatch_catalog", "typed_catalog"}
            return False

        while True:
            changed = False
            for node in ast.walk(tree):
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    value = node.iter
                    targets = [node.target]
                elif isinstance(node, ast.comprehension):
                    value = node.iter
                    targets = [node.target]
                if value is None or not is_raw_catalog_receiver(value):
                    continue
                for target in targets:
                    new_names = _bound_names(target).difference(raw_catalog_names)
                    if new_names:
                        raw_catalog_names.update(new_names)
                        changed = True
            if not changed:
                break

        raw_resolve_aliases: set[str] = set()

        def is_raw_resolve(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in raw_resolve_aliases
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "resolve"
                and is_raw_catalog_receiver(node.value)
            ):
                return True
            if isinstance(node, ast.Call):
                reflected = _reflected_attribute(node, aliases, strings)
                return (
                    reflected is not None
                    and is_raw_catalog_receiver(reflected[0])
                    and reflected[1] == "resolve"
                )
            if isinstance(node, ast.Subscript):
                return is_raw_resolve(node.value)
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                return any(is_raw_resolve(item) for item in node.elts)
            if isinstance(node, ast.Dict):
                return any(
                    is_raw_resolve(item) for item in (*node.keys, *node.values) if item is not None
                )
            if isinstance(node, ast.IfExp):
                return is_raw_resolve(node.body) or is_raw_resolve(node.orelse)
            if isinstance(node, ast.BoolOp):
                return any(is_raw_resolve(item) for item in node.values)
            if isinstance(node, ast.NamedExpr):
                return is_raw_resolve(node.value)
            return False

        while True:
            changed = False
            for node in ast.walk(tree):
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    value = node.value
                    targets = [node.target]
                if value is None or not is_raw_resolve(value):
                    continue
                for target in targets:
                    new_names = _bound_names(target).difference(raw_resolve_aliases)
                    if new_names:
                        raw_resolve_aliases.update(new_names)
                        changed = True
            if not changed:
                break

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if is_raw_resolve(node.func):
                raise SourceGateViolation(f"raw-tool-catalog-resolve:{path}:{node.lineno}")


def _validate_raw_authority_metadata_access(paths: dict[Path, str]) -> None:
    guarded_paths = {
        SRC / "ai" / "agent_loop.py",
        SRC / "ai" / "tool_authority" / "composition.py",
        SRC / "ai" / "tool_runtime" / "pipeline.py",
    }
    forbidden_fields = {
        "binding",
        "confirmation_policy",
        "operation",
        "required_capabilities",
    }

    for path in guarded_paths.intersection(paths):
        tree = _tree(paths[path], filename=str(path))
        aliases = _import_aliases(tree)
        strings = _string_bindings(tree)
        raw_metadata_names: set[str] = set()

        def is_raw_metadata(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in raw_metadata_names
            if isinstance(node, ast.Attribute):
                return node.attr == "metadata"
            if isinstance(node, ast.Subscript):
                return is_raw_metadata(node.value)
            if isinstance(node, ast.Starred):
                return is_raw_metadata(node.value)
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                return any(is_raw_metadata(item) for item in node.elts)
            if isinstance(node, ast.Dict):
                return any(
                    is_raw_metadata(item) for item in (*node.keys, *node.values) if item is not None
                )
            if isinstance(node, ast.IfExp):
                return is_raw_metadata(node.body) or is_raw_metadata(node.orelse)
            if isinstance(node, ast.BoolOp):
                return any(is_raw_metadata(item) for item in node.values)
            if isinstance(node, ast.NamedExpr):
                return is_raw_metadata(node.value)
            if isinstance(node, ast.Call):
                terminal = _resolved_terminal(node.func, aliases)
                if terminal in {"asdict", "dict", "list", "set", "tuple", "vars"}:
                    return any(is_raw_metadata(item) for item in node.args)
                if terminal == "cast" and len(node.args) >= 2:
                    return is_raw_metadata(node.args[1])
                reflected = _reflected_attribute(node, aliases, strings)
                if reflected is not None:
                    return reflected[1] == "metadata"
            return False

        while True:
            changed = False
            for node in ast.walk(tree):
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    value = node.iter
                    targets = [node.target]
                elif isinstance(node, ast.comprehension):
                    value = node.iter
                    targets = [node.target]
                if value is None or not is_raw_metadata(value):
                    continue
                for target in targets:
                    new_names = _bound_names(target).difference(raw_metadata_names)
                    if new_names:
                        raw_metadata_names.update(new_names)
                        changed = True
            if not changed:
                break

        def semantic_access(node: ast.AST) -> tuple[ast.AST, str] | None:
            field: str | None = None
            receiver: ast.AST | None = None
            if isinstance(node, ast.Attribute):
                receiver = node.value
                field = node.attr
            elif isinstance(node, ast.Subscript):
                receiver = node.value
                field = _constant_string(node.slice, strings)
            elif isinstance(node, ast.Call):
                reflected = _reflected_attribute(node, aliases, strings)
                if reflected is not None:
                    receiver, field = reflected
                elif isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
                    receiver = node.func.value
                    field = _constant_string(node.args[0], strings)
            return None if receiver is None or field is None else (receiver, field)

        for node in ast.walk(tree):
            access = semantic_access(node)
            if access is not None and access[1] in forbidden_fields and is_raw_metadata(access[0]):
                raise SourceGateViolation(
                    f"raw-tool-spec-metadata-semantic:{access[1]}:{path}:{node.lineno}"
                )

        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Lambda):
                continue
            parameters = (*call.func.args.posonlyargs, *call.func.args.args)
            tainted_parameters = {
                parameter.arg
                for parameter, argument in zip(parameters, call.args, strict=False)
                if is_raw_metadata(argument)
            }
            if not tainted_parameters:
                continue
            for node in ast.walk(call.func.body):
                access = semantic_access(node)
                if (
                    access is not None
                    and access[1] in forbidden_fields
                    and isinstance(access[0], ast.Name)
                    and access[0].id in tainted_parameters
                ):
                    raise SourceGateViolation(
                        f"raw-tool-spec-metadata-semantic:{access[1]}:{path}:{node.lineno}"
                    )


def _validate_legacy_context_helpers_unreachable(paths: dict[Path, str]) -> None:
    context_path = SRC / "ai" / "tool_runtime" / "context.py"
    context_module = "offerpilot.ai.tool_runtime.context"
    runtime_module = "offerpilot.ai.tool_runtime"
    helper_internal_callers = {
        "require_capabilities": frozenset({"evaluate_context"}),
        "pre_resolver_scope_policy": frozenset({"evaluate_context"}),
        "audit_bindings": frozenset({"evaluate_context"}),
        "evaluate_context": frozenset(),
    }

    for path, source in paths.items():
        tree = _tree(source, filename=str(path))
        aliases = _import_aliases(tree)
        strings = _string_bindings(tree)
        helper_bindings: dict[str, set[str]] = {}
        module_bindings: set[str] = set()

        def dotted_name(node: ast.AST) -> str | None:
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                prefix = dotted_name(node.value)
                return None if prefix is None else f"{prefix}.{node.attr}"
            return None

        def is_context_module(node: ast.AST) -> bool:
            return (isinstance(node, ast.Name) and node.id in module_bindings) or dotted_name(
                node
            ) == context_module

        if path == context_path:
            helper_bindings.update((name, {name}) for name in helper_internal_callers)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module in {context_module, runtime_module}:
                    for item in node.names:
                        if item.name in helper_internal_callers:
                            helper_bindings.setdefault(item.asname or item.name, set()).add(
                                item.name
                            )
                if node.module == runtime_module:
                    module_bindings.update(
                        item.asname or item.name for item in node.names if item.name == "context"
                    )
            elif isinstance(node, ast.Import):
                module_bindings.update(
                    item.asname
                    for item in node.names
                    if item.name == context_module and item.asname is not None
                )

        def helpers_from_value(node: ast.AST) -> set[str]:
            if isinstance(node, ast.Name):
                return set(helper_bindings.get(node.id, set()))
            if (
                isinstance(node, ast.Attribute)
                and is_context_module(node.value)
                and node.attr in helper_internal_callers
            ):
                return {node.attr}
            if isinstance(node, ast.Call):
                reflected = _reflected_attribute(node, aliases, strings)
                if reflected is not None and is_context_module(reflected[0]):
                    return {reflected[1]} if reflected[1] in helper_internal_callers else set()
                return set()
            if isinstance(node, (ast.Subscript, ast.Starred, ast.NamedExpr)):
                return helpers_from_value(node.value)
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                return set().union(*(helpers_from_value(item) for item in node.elts))
            if isinstance(node, ast.Dict):
                return set().union(
                    *(
                        helpers_from_value(item)
                        for item in (*node.keys, *node.values)
                        if item is not None
                    )
                )
            if isinstance(node, ast.IfExp):
                return helpers_from_value(node.body) | helpers_from_value(node.orelse)
            if isinstance(node, ast.BoolOp):
                return set().union(*(helpers_from_value(item) for item in node.values))
            return set()

        while True:
            changed = False
            for node in ast.walk(tree):
                value: ast.AST | None = None
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    value = node.value
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    value = node.value
                    targets = [node.target]
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    value = node.iter
                    targets = [node.target]
                elif isinstance(node, ast.comprehension):
                    value = node.iter
                    targets = [node.target]
                if value is None:
                    continue
                if is_context_module(value):
                    for target in targets:
                        new_names = _bound_names(target).difference(module_bindings)
                        if new_names:
                            module_bindings.update(new_names)
                            changed = True

                helpers = helpers_from_value(value)
                if not helpers:
                    continue
                for target in targets:
                    for name in _bound_names(target):
                        before = len(helper_bindings.get(name, set()))
                        helper_bindings.setdefault(name, set()).update(helpers)
                        if len(helper_bindings[name]) != before:
                            changed = True
            if not changed:
                break

        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            helpers = helpers_from_value(node.func)
            if not helpers:
                continue
            owner = _direct_method_owner(node, parents)
            for helper in sorted(helpers):
                if path == context_path and owner in {
                    ("<module>", caller) for caller in helper_internal_callers[helper]
                }:
                    continue
                raise SourceGateViolation(
                    f"legacy-context-helper-caller:{helper}:{path}:{owner}:{node.lineno}"
                )


def test_scoped_authority_source_gates_hold_across_production() -> None:
    production = {path: path.read_text(encoding="utf-8") for path in sorted(SRC.rglob("*.py"))}
    for path, source in production.items():
        _validate_global_forbidden_paths(path, source)

    _validate_pipeline_order(
        (SRC / "ai" / "tool_runtime" / "pipeline.py").read_text(encoding="utf-8")
    )
    _validate_provider_identity_keywords((SRC / "ai" / "agent_loop.py").read_text(encoding="utf-8"))
    chat = (SRC / "repositories" / "chat.py").read_text(encoding="utf-8")
    _validate_chat_read_paths(chat)
    _validate_typed_pending_routes(chat)
    _validate_scope_write_routes(chat)
    _validate_authority_callsite_ownership(production)
    _validate_segment_lease_ownership(production)
    _validate_raw_authority_metadata_access(production)
    _validate_legacy_context_helpers_unreachable(production)
    for repository_name in (
        "application_events.py",
        "applications.py",
        "jd.py",
        "notes.py",
        "offers.py",
    ):
        path = SRC / "repositories" / repository_name
        _validate_scoped_repository_ports(path, production[path])
    _validate_binding_resolver_purity(production[SRC / "ai" / "tool_specs" / "common.py"])
    for path, source in production.items():
        _validate_no_transient_generic_serializers(source)
        if path != SRC / "context_projector" / "selector.py":
            _validate_dependency_policy_and_autoapprove(source)

    from offerpilot.ai.tool_runtime.legacy import LEGACY_DETERMINISTIC_NAMES
    from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG

    assert LEGACY_DETERMINISTIC_NAMES.isdisjoint(
        contract.name for contract in MODEL_TOOL_CATALOG.provider_contracts()
    )


@pytest.mark.parametrize(
    ("validator", "source", "code"),
    [
        (
            lambda source: _validate_global_forbidden_paths(SRC / "api.py", source),
            "caps = frozenset(ToolCapability)\n",
            "automatic-capability-grant",
        ),
        (
            lambda source: _validate_global_forbidden_paths(SRC / "api.py", source),
            "context = ToolExecutionContext(authority=value)\n",
            "context-construction-owner",
        ),
        (
            _validate_provider_identity_keywords,
            "def complete_model(surface):\n"
            "    preflight(surface, invocation_identity=identity)\n"
            "    stream_surface(surface, invocation_identity=identity)\n"
            "    return complete_surface(surface)\n",
            "provider-invocation-identity",
        ),
        (
            _validate_provider_identity_keywords,
            "def complete_model(surface, request):\n"
            "    preflight(surface, invocation_identity=request)\n"
            "    stream_surface(surface, invocation_identity=request)\n"
            "    return complete_surface(surface, invocation_identity=request)\n",
            "provider-invocation-identity",
        ),
        (
            _validate_chat_read_paths,
            "def get_conversation(): pass\n"
            "def list_conversations(): pass\n"
            "def get_pending_action(repo):\n    repo.create_primary()\n",
            "lazy-ledger-backfill",
        ),
        (
            _validate_typed_pending_routes,
            "def persist_typed_pending(session, pending):\n    return session\n",
            "typed-pending-claim-parameter",
        ),
        (
            _validate_typed_pending_routes,
            "def persist_typed_pending(session, pending, pending_authority_claim):\n"
            "    if bypass:\n        return persist_legacy_pending(session, pending)\n"
            "    _validate_typed_pending(pending_authority_claim)\n",
            "typed-pending-legacy-fallback",
        ),
        (
            _validate_no_transient_generic_serializers,
            "from dataclasses import replace\n"
            "def dump(value: PendingAuthorityClaim): return replace(value)\n",
            "transient-generic-serialization",
        ),
        (
            _validate_binding_resolver_purity,
            "def _authority_binding_resolution(): pass\n"
            "def resolve_identity_argument(): pass\n"
            "def resolve_parent_application():\n    Session().execute(query)\n",
            "binding-resolver-side-effect",
        ),
        (
            _validate_dependency_policy_and_autoapprove,
            "def dispatch(auto_approve):\n"
            "    if auto_approve:\n        return execute_prepared(value)\n",
            "auto-approve-authority-bypass",
        ),
    ],
)
def test_negative_source_fixtures_prove_gates_are_live(
    validator: object,
    source: str,
    code: str,
) -> None:
    with pytest.raises(SourceGateViolation, match=code):
        validator(source)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("path", "source", "code"),
    [
        (
            SRC / "api.py",
            "def issue(): return _open_segment_tool_catalog_lease()\n",
            "segment-lease-owner",
        ),
        (
            SRC / "api.py",
            "def issue():\n    issuer = _open_segment_tool_catalog_lease\n    return issuer()\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "metadata.py",
            "def bypass(token, lease): return token._register_segment_lease(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "metadata.py",
            "def bypass(token, lease):\n"
            "    register = token._register_segment_lease\n"
            "    return register(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            "def bypass(token, lease): return token._require_registered_segment_lease(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            "def bypass(token, lease):\n"
            "    require = token._require_registered_segment_lease\n"
            "    return require(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "api.py",
            "def bypass(lease, handle): return lease._require_issued_spec_identity(handle)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "api.py",
            "def bypass(lease, handle):\n"
            "    require = lease._require_issued_spec_identity\n"
            "    return require(handle)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "api.py",
            "def bypass(token, view): return token._require_issued_view_integrity(view, object)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "api.py",
            "def bypass(token, view):\n"
            "    require = token._require_issued_view_integrity\n"
            "    return require(view, object)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            "def resolve(token, lease): return token._revoke_segment_lease(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "tool_runtime" / "catalog.py",
            "def resolve(token, lease):\n"
            "    revoke = token._revoke_segment_lease\n"
            "    return revoke(lease)\n",
            "segment-lease-owner",
        ),
        (
            SRC / "ai" / "agent_loop.py",
            "def dispatch(invocation, name): return invocation.catalog.resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def dispatch(catalog, name): return catalog.resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def dispatch(catalog, name):\n"
            "    route_registry = catalog\n"
            "    return route_registry.resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "agent_loop.py",
            "def dispatch(invocation, name):\n"
            "    route_registry = invocation.catalog\n"
            "    resolve = route_registry.resolve\n"
            "    return resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def dispatch(catalog, name):\n    return getattr(catalog, 'resolve')(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def dispatch(catalog, name):\n"
            "    holders = [catalog]\n"
            "    return holders[0].resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def dispatch(catalog, name):\n"
            "    route_registry, = (catalog,)\n"
            "    return route_registry.resolve(name)\n",
            "raw-tool-catalog-resolve",
        ),
        (
            SRC / "ai" / "tool_runtime" / "pipeline.py",
            "def attach(prepared, handle): object.__setattr__(prepared, 'spec_handle', handle)\n",
            "dynamic-prepared-spec-handle",
        ),
        (
            SRC / "ai" / "agent_loop.py",
            "def read(prepared): return getattr(prepared, 'spec_handle')\n",
            "dynamic-prepared-spec-handle",
        ),
    ],
)
def test_segment_lease_ownership_negative_fixtures_are_live(
    path: Path,
    source: str,
    code: str,
) -> None:
    with pytest.raises(SourceGateViolation, match=code):
        _validate_segment_lease_ownership({path: source})


def test_segment_lease_ownership_allows_exact_lease_resolve() -> None:
    source = (
        "def dispatch(catalog_lease: SegmentToolCatalogLease, name):\n"
        "    lease = catalog_lease\n"
        "    return lease.resolve(name)\n"
    )
    _validate_segment_lease_ownership({SRC / "ai" / "tool_runtime" / "pipeline.py": source})


def test_segment_lease_ownership_allows_exact_catalog_owner() -> None:
    source = (
        "class SegmentToolCatalogLease:\n"
        "    def require_catalog(self, token, lease):\n"
        "        return token._require_registered_segment_lease(lease)\n"
    )
    _validate_segment_lease_ownership({SRC / "ai" / "tool_runtime" / "catalog.py": source})


@pytest.mark.parametrize(
    "source",
    (
        "def bypass(token, lease):\n    return getattr(token, '_register_segment_lease')(lease)\n",
        "def bypass(token, lease):\n"
        "    calls = [token._register_segment_lease]\n"
        "    return calls[0](lease)\n",
    ),
)
def test_segment_lease_ownership_rejects_reflective_and_container_aliases(
    source: str,
) -> None:
    with pytest.raises(SourceGateViolation, match="segment-lease-owner"):
        _validate_segment_lease_ownership({SRC / "api.py": source})


@pytest.mark.parametrize(
    "source",
    (
        "def bypass(token, lease):\n"
        "    if (call := token._register_segment_lease):\n"
        "        return call(lease)\n",
        "def bypass(token, lease):\n"
        "    for call in [token._register_segment_lease]:\n"
        "        return call(lease)\n",
    ),
)
def test_segment_lease_ownership_rejects_control_flow_aliases(source: str) -> None:
    with pytest.raises(SourceGateViolation, match="segment-lease-owner"):
        _validate_segment_lease_ownership({SRC / "api.py": source})


def test_segment_lease_ownership_rejects_nested_same_name_owner() -> None:
    source = (
        "class AuthorityFactory:\n"
        "    def outer(self, lease, handle):\n"
        "        def _resolve_registered_route():\n"
        "            return lease._require_issued_spec_identity(handle)\n"
        "        return _resolve_registered_route()\n"
    )
    with pytest.raises(SourceGateViolation, match="segment-lease-owner"):
        _validate_segment_lease_ownership(
            {SRC / "ai" / "tool_authority" / "composition.py": source}
        )


def test_segment_lease_ownership_rejects_lambda_nested_in_exact_owner() -> None:
    source = (
        "class AuthorityFactory:\n"
        "    def _resolve_registered_route(self, lease, handle):\n"
        "        return (lambda: lease._require_issued_spec_identity(handle))()\n"
    )
    with pytest.raises(SourceGateViolation, match="segment-lease-owner"):
        _validate_segment_lease_ownership(
            {SRC / "ai" / "tool_authority" / "composition.py": source}
        )


def test_segment_lease_ownership_allows_exact_authority_owner() -> None:
    source = (
        "class AuthorityFactory:\n"
        "    def _resolve_registered_route(self, lease, handle):\n"
        "        return lease._require_issued_spec_identity(handle)\n"
    )
    _validate_segment_lease_ownership({SRC / "ai" / "tool_authority" / "composition.py": source})


def test_segment_lease_ownership_allows_unrelated_reflective_and_container_calls() -> None:
    source = (
        "def allowed(token, value):\n"
        "    reflected = getattr(token, 'public_method')\n"
        "    calls = [reflected]\n"
        "    return calls[0](value)\n"
    )
    _validate_segment_lease_ownership({SRC / "api.py": source})


def test_dynamic_prepared_spec_handle_rejects_a_string_alias() -> None:
    source = (
        "FIELD = 'spec_' + 'handle'\n"
        "def attach(prepared, handle):\n"
        "    object.__setattr__(prepared, FIELD, handle)\n"
    )
    with pytest.raises(SourceGateViolation, match="dynamic-prepared-spec-handle"):
        _validate_segment_lease_ownership({SRC / "api.py": source})


def test_dynamic_prepared_spec_handle_rejects_bound_setattr() -> None:
    source = "def attach(prepared, handle):\n    prepared.__setattr__('spec_handle', handle)\n"
    with pytest.raises(SourceGateViolation, match="dynamic-prepared-spec-handle"):
        _validate_segment_lease_ownership({SRC / "api.py": source})


def test_raw_catalog_resolve_rejects_bound_getattribute() -> None:
    source = "def dispatch(catalog, name):\n    return catalog.__getattribute__('resolve')(name)\n"
    with pytest.raises(SourceGateViolation, match="raw-tool-catalog-resolve"):
        _validate_segment_lease_ownership({SRC / "ai" / "tool_runtime" / "pipeline.py": source})


def test_dynamic_prepared_field_gate_allows_an_unrelated_string_alias() -> None:
    source = (
        "FIELD = 'journal_started_draft'\n"
        "def attach(prepared, value):\n"
        "    object.__setattr__(prepared, FIELD, value)\n"
    )
    _validate_segment_lease_ownership({SRC / "api.py": source})


@pytest.mark.parametrize(
    "source",
    [
        "def decide(spec): return spec.metadata.operation\n",
        (
            "def decide(spec):\n"
            "    metadata = spec.metadata\n"
            "    return getattr(metadata, 'confirmation_policy')\n"
        ),
        (
            "def decide(spec):\n"
            "    holders = [spec.metadata]\n"
            "    return holders[0].required_capabilities\n"
        ),
    ],
)
def test_raw_authority_metadata_negative_fixtures_are_live(source: str) -> None:
    with pytest.raises(SourceGateViolation, match="raw-tool-spec-metadata-semantic"):
        _validate_raw_authority_metadata_access(
            {SRC / "ai" / "tool_authority" / "composition.py": source}
        )


def test_agent_loop_raw_authority_metadata_is_gated() -> None:
    source = "def decide(spec): return spec.metadata.operation\n"
    with pytest.raises(SourceGateViolation, match="raw-tool-spec-metadata-semantic"):
        _validate_raw_authority_metadata_access({SRC / "ai" / "agent_loop.py": source})


@pytest.mark.parametrize(
    "source",
    (
        "def decide(spec):\n    return spec.metadata.__getattribute__('operation')\n",
        "def decide(spec):\n    return (lambda value: value.operation)(spec.metadata)\n",
    ),
)
def test_agent_loop_raw_authority_metadata_rejects_reflection_and_lambda_flow(
    source: str,
) -> None:
    with pytest.raises(SourceGateViolation, match="raw-tool-spec-metadata-semantic"):
        _validate_raw_authority_metadata_access({SRC / "ai" / "agent_loop.py": source})


def test_agent_loop_raw_non_authority_metadata_field_remains_allowed() -> None:
    source = "def project(spec): return spec.metadata.editable_fields\n"
    _validate_raw_authority_metadata_access({SRC / "ai" / "agent_loop.py": source})


@pytest.mark.parametrize(
    "source",
    [
        (
            "from offerpilot.ai.tool_runtime.context import "
            "evaluate_context as evaluate\n"
            "def dispatch(spec, args, context):\n"
            "    call = evaluate\n"
            "    return call(spec, args, context)\n"
        ),
        (
            "from offerpilot.ai.tool_runtime import context as runtime_context\n"
            "def dispatch(spec, context):\n"
            "    policy = runtime_context\n"
            "    return policy.require_capabilities(spec, context)\n"
        ),
        (
            "import offerpilot.ai.tool_runtime.context\n"
            "def dispatch(spec, args, context):\n"
            "    return offerpilot.ai.tool_runtime.context.evaluate_context(\n"
            "        spec, args, context\n"
            "    )\n"
        ),
    ],
)
def test_legacy_context_helper_gate_rejects_external_alias_callers(source: str) -> None:
    with pytest.raises(SourceGateViolation, match="legacy-context-helper-caller"):
        _validate_legacy_context_helpers_unreachable({SRC / "api.py": source})


def test_legacy_context_helper_gate_allows_only_exact_internal_call_graph() -> None:
    context_path = SRC / "ai" / "tool_runtime" / "context.py"
    source = (
        "def require_capabilities(spec, context): return None\n"
        "def pre_resolver_scope_policy(spec, context): return None\n"
        "def audit_bindings(spec, args, context): return audit, True\n"
        "def evaluate_context(spec, args, context):\n"
        "    require_capabilities(spec, context)\n"
        "    pre_resolver_scope_policy(spec, context)\n"
        "    return audit_bindings(spec, args, context)\n"
    )
    _validate_legacy_context_helpers_unreachable({context_path: source})

    with pytest.raises(SourceGateViolation, match="legacy-context-helper-caller"):
        _validate_legacy_context_helpers_unreachable(
            {
                context_path: source + "def bypass(spec, context):\n"
                "    return require_capabilities(spec, context)\n"
            }
        )


def test_legacy_context_helper_gate_rejects_lambda_nested_in_exact_owner() -> None:
    context_path = SRC / "ai" / "tool_runtime" / "context.py"
    source = (
        "def require_capabilities(spec, context): return None\n"
        "def evaluate_context(spec, context):\n"
        "    return (lambda: require_capabilities(spec, context))()\n"
    )
    with pytest.raises(SourceGateViolation, match="legacy-context-helper-caller"):
        _validate_legacy_context_helpers_unreachable({context_path: source})


def test_legacy_context_helper_gate_ignores_unrelated_local_spelling() -> None:
    source = (
        "def evaluate_context(value): return value\n"
        "def dispatch(value): return evaluate_context(value)\n"
    )
    _validate_legacy_context_helpers_unreachable({SRC / "api.py": source})


def test_legacy_context_helper_gate_rejects_reflective_callers() -> None:
    source = (
        "import offerpilot.ai.tool_runtime.context as context_module\n"
        "def bypass(spec, context):\n"
        "    helper = getattr(context_module, 'require_capabilities')\n"
        "    return helper(spec, context)\n"
    )
    with pytest.raises(SourceGateViolation, match="legacy-context-helper-caller"):
        _validate_legacy_context_helpers_unreachable({SRC / "api.py": source})


@pytest.mark.parametrize(
    "source",
    (
        "import offerpilot.ai.tool_runtime.context as context_module\n"
        "def bypass(spec, args, context):\n"
        "    helper = context_module.__getattribute__('evaluate_context')\n"
        "    return helper(spec, args, context)\n",
        "from offerpilot.ai.tool_runtime.context import evaluate_context\n"
        "def bypass(spec, args, context):\n"
        "    (helper,) = (evaluate_context,)\n"
        "    return helper(spec, args, context)\n",
        "from offerpilot.ai.tool_runtime.context import evaluate_context\n"
        "def bypass(spec, args, context):\n"
        "    if (helper := evaluate_context):\n"
        "        return helper(spec, args, context)\n",
    ),
)
def test_legacy_context_helper_gate_rejects_dunder_and_control_flow_aliases(
    source: str,
) -> None:
    with pytest.raises(SourceGateViolation, match="legacy-context-helper-caller"):
        _validate_legacy_context_helpers_unreachable({SRC / "api.py": source})


def test_legacy_context_helper_gate_allows_unrelated_reflective_callers() -> None:
    source = (
        "import offerpilot.ai.tool_runtime.context as context_module\n"
        "def allowed(value):\n"
        "    helper = getattr(context_module, 'scope_access_denied')\n"
        "    return helper(value)\n"
    )
    _validate_legacy_context_helpers_unreachable({SRC / "api.py": source})


@pytest.mark.parametrize(
    "source",
    [
        "def f(request): return frozenset(request.capabilities)\n",
        "def f(payload): return payload.get('current_bindings')\n",
        "from x import ToolExecutionContext as Ctx\ndef f(): return Ctx()\n",
        "def f(resolved): return resolved.tool_context\n",
    ],
)
def test_global_negative_fixtures_cover_aliases_and_request_derived_authority(
    source: str,
) -> None:
    with pytest.raises(SourceGateViolation):
        _validate_global_forbidden_paths(SRC / "api.py", source)


@pytest.mark.parametrize(
    "source",
    [
        "def f(payload):\n    proposal = payload\n    return proposal.get('capabilities')\n",
        "def f():\n    Ctx = ToolExecutionContext\n    return Ctx(authority=value)\n",
    ],
)
def test_global_negative_fixtures_cover_local_value_aliases(source: str) -> None:
    with pytest.raises(SourceGateViolation):
        _validate_global_forbidden_paths(SRC / "api.py", source)


def test_callsite_owner_and_claim_negative_fixtures_are_live() -> None:
    fake_path = SRC / "api.py"
    with pytest.raises(SourceGateViolation, match="authority-call-owner"):
        _validate_authority_callsite_ownership(
            {fake_path: "def bypass(factory): return factory.issue_pending_claim()\n"}
        )
    with pytest.raises(SourceGateViolation, match="typed-pending-call-without-claim"):
        _validate_authority_callsite_ownership(
            {
                SRC / "repositories" / "chat.py": (
                    "def route(repo, session, conversation, pending):\n"
                    "    repo.persist_typed_pending(session, conversation, pending)\n"
                )
            }
        )
    with pytest.raises(SourceGateViolation, match="approval-authority-provider"):
        _validate_authority_callsite_ownership(
            {
                SRC / "ai" / "agent_loop.py": (
                    "def invoke(model, approval_authority):\n"
                    "    model.complete_surface(surface, approval_authority)\n"
                )
            }
        )
    with pytest.raises(SourceGateViolation, match="authority-call-owner"):
        _validate_authority_callsite_ownership(
            {
                SRC / "api.py": (
                    "def bypass(factory):\n"
                    "    issue = factory.issue_pending_claim\n"
                    "    return issue()\n"
                )
            }
        )
    with pytest.raises(SourceGateViolation, match="typed-write-without-execution-claim"):
        _validate_authority_callsite_ownership(
            {
                SRC / "ai" / "write_operations.py": (
                    "def dispatch(prepared, context):\n"
                    "    return execute_prepared(prepared, context)\n"
                )
            }
        )
    with pytest.raises(SourceGateViolation, match="segment-approved-write"):
        _validate_authority_callsite_ownership(
            {
                SRC / "ai" / "write_operations.py": (
                    "def dispatch(factory, segment_authority):\n"
                    "    return factory.create_approved_write_execute_identity("
                    "segment_authority)\n"
                )
            }
        )
    with pytest.raises(SourceGateViolation, match="approval-authority-provider"):
        _validate_authority_callsite_ownership(
            {
                SRC / "ai" / "agent_loop.py": (
                    "def invoke(model, approval_context):\n"
                    "    authority = approval_context\n"
                    "    return model.complete_surface(surface, authority)\n"
                )
            }
        )


def test_scoped_repository_negative_fixtures_reject_fallback_and_post_filter() -> None:
    for source in (
        "class Repo:\n"
        "    def list_items_scoped(self, constraint):\n"
        "        self._require_scoped(constraint)\n"
        "        return self.list_items()\n",
        "class Repo:\n"
        "    def list_items_scoped(self, constraint):\n"
        "        self._require_scoped(constraint)\n"
        "        return [row for row in rows if row.application_id in constraint.allowed_identities]\n",
    ):
        with pytest.raises(SourceGateViolation):
            _validate_scoped_repository_ports(SRC / "repositories" / "fake.py", source)
    with pytest.raises(SourceGateViolation, match="scoped-unscoped-fallback"):
        _validate_scoped_repository_ports(
            SRC / "repositories" / "fake.py",
            "class Repo:\n"
            "    def list_items_scoped(self, constraint):\n"
            "        self._require_scoped(constraint)\n"
            "        return self._all_items()\n",
        )
    with pytest.raises(SourceGateViolation, match="scoped-unscoped-fallback"):
        _validate_scoped_repository_ports(
            SRC / "repositories" / "fake.py",
            "class Repo:\n"
            "    def list_items_scoped(self, constraint):\n"
            "        self._require_scoped(constraint)\n"
            "        fetch = self.list_items\n"
            "        return fetch()\n",
        )
    with pytest.raises(SourceGateViolation, match="scoped-python-post-filter"):
        _validate_scoped_repository_ports(
            SRC / "repositories" / "fake.py",
            "class Repo:\n"
            "    def list_items_scoped(self, constraint):\n"
            "        self._require_scoped(constraint)\n"
            "        return tuple(row for row in rows "
            "if row.application_id in constraint.allowed_identities)\n",
        )


def test_binding_resolver_gate_rejects_repository_indirection() -> None:
    source = (
        "def _authority_binding_resolution(): pass\n"
        "def resolve_identity_argument(repo):\n"
        "    return repo.get_application_scoped(1)\n"
        "def resolve_parent_application(): pass\n"
    )
    with pytest.raises(SourceGateViolation, match="binding-resolver-side-effect"):
        _validate_binding_resolver_purity(source)


def test_autoapprove_gate_rejects_attribute_derived_bypass() -> None:
    with pytest.raises(SourceGateViolation, match="auto-approve-authority-bypass"):
        _validate_dependency_policy_and_autoapprove(
            "def dispatch(config):\n"
            "    if config.auto_approve:\n"
            "        return execute_prepared(value)\n"
        )


def test_pipeline_order_gate_does_not_accept_calls_hidden_in_nested_helpers() -> None:
    source = (
        "def prepare_call():\n"
        "    def fake():\n"
        "        require_authority_phase(); resolve(); require_authority_spec()\n"
        "        _require_entry_capabilities(); _pre_resolver_entry_scope_policy()\n"
        "        _audit_entry_bindings(); prepare_tool_call()\n"
        "    return fake\n"
        "def execute_prepared():\n"
        "    def fake():\n"
        "        require_prepared_route(); require_authority_phase()\n"
        "        require_execution_claim_transaction(); begin_prepared_execution()\n"
        "    return fake\n"
        "def _execute_read():\n"
        "    def fake():\n"
        "        _require_entry_capabilities(); _audit_entry_bindings(); executor()\n"
        "    return fake\n"
        "def _execute_claimed_write():\n"
        "    def fake():\n"
        "        claim_lifecycle(); _typed_args_digest(); executor()\n"
        "    return fake\n"
    )
    with pytest.raises(SourceGateViolation, match="missing-call"):
        _validate_pipeline_order(source)


def test_pipeline_order_gate_rejects_conditional_authority_calls() -> None:
    source = (
        "def prepare_call():\n"
        "    require_authority_phase()\n"
        "    resolve()\n"
        "    require_authority_spec()\n"
        "    if False: _require_entry_capabilities()\n"
        "    _pre_resolver_entry_scope_policy()\n"
        "    _audit_entry_bindings()\n"
        "    prepare_tool_call()\n"
        "def execute_prepared():\n"
        "    require_prepared_route()\n"
        "    require_authority_phase()\n"
        "    require_execution_claim_transaction()\n"
        "    begin_prepared_execution()\n"
        "def _execute_read():\n"
        "    _require_entry_capabilities()\n"
        "    _audit_entry_bindings()\n"
        "    executor()\n"
        "def _execute_claimed_write():\n"
        "    claim_lifecycle()\n"
        "    _typed_args_digest()\n"
        "    executor()\n"
    )
    with pytest.raises(SourceGateViolation, match="conditional-call"):
        _validate_pipeline_order(source)


def test_global_gate_rejects_reflective_context_construction() -> None:
    source = (
        "name = 'ToolExecutionContext'\n"
        "def f(module): return getattr(module, name)(authority=value)\n"
    )
    with pytest.raises(SourceGateViolation, match="context-construction-owner"):
        _validate_global_forbidden_paths(SRC / "api.py", source)


def test_context_factory_allowlist_does_not_allow_arbitrary_composition_construction() -> None:
    source = "def unrelated(): return ToolExecutionContext(authority=value)\n"
    with pytest.raises(SourceGateViolation, match="context-construction-owner"):
        _validate_global_forbidden_paths(
            SRC / "pilot_runtime" / "composition.py",
            source,
        )


def test_scope_write_gate_rejects_generic_scope_mutation_without_cas_guards() -> None:
    source = (
        "def create_conversation(title, mode, context_type, context_ref):\n"
        "    return Conversation(mode=mode, context_type=context_type, context_ref=context_ref)\n"
        "def update_conversation(values):\n"
        "    values.intersection(scope)\n"
        "    setattr(conversation, key, value)\n"
        "def patch_conversation_with_scope(mutation, expected_scope_revision): pass\n"
    )
    with pytest.raises(SourceGateViolation, match="generic-create-scope-guard"):
        _validate_scope_write_routes(source)


def test_dependency_policy_gate_rejects_per_surface_policy_copies() -> None:
    with pytest.raises(SourceGateViolation, match="copied-dependency-policy"):
        _validate_dependency_policy_and_autoapprove(
            "policy = DependencyPolicyV1(version='copied')\n"
        )


def test_transient_serialization_gate_rejects_unannotated_constructor_flow() -> None:
    source = (
        "import json\n"
        "def dump():\n"
        "    claim = PendingAuthorityClaim(...)\n"
        "    return json.dumps(claim)\n"
    )
    with pytest.raises(SourceGateViolation, match="transient-generic-serialization"):
        _validate_no_transient_generic_serializers(source)


def test_provider_gate_rejects_alias_calls_and_ignores_dead_compliant_decoys() -> None:
    source = (
        "def complete_model(surface):\n"
        "    pf = preflight\n"
        "    complete = complete_surface\n"
        "    stream = stream_surface\n"
        "    pf(surface)\n"
        "    complete(surface)\n"
        "    stream(surface)\n"
        "    if False:\n"
        "        preflight(surface, invocation_identity=invocation_identity)\n"
        "        complete_surface(surface, invocation_identity=invocation_identity)\n"
        "        stream_surface(surface, invocation_identity=invocation_identity)\n"
    )
    with pytest.raises(SourceGateViolation, match="provider-invocation-identity"):
        _validate_provider_identity_keywords(source)


def test_alias_resolution_has_no_fixed_depth_authority_escape() -> None:
    source = (
        "def bypass(factory):\n"
        "    a1 = a2\n"
        "    a2 = a3\n"
        "    a3 = a4\n"
        "    a4 = a5\n"
        "    a5 = a6\n"
        "    a6 = factory.issue_pending_claim\n"
        "    return a1()\n"
    )
    with pytest.raises(SourceGateViolation, match="authority-call-owner"):
        _validate_authority_callsite_ownership({SRC / "api.py": source})

    context_source = (
        source.replace(
            "def bypass(factory):",
            "def bypass():",
        )
        .replace(
            "a6 = factory.issue_pending_claim",
            "a6 = ToolExecutionContext",
        )
        .replace(
            "return a1()",
            "return a1(authority=value)",
        )
    )
    with pytest.raises(SourceGateViolation, match="context-construction-owner"):
        _validate_global_forbidden_paths(SRC / "api.py", context_source)


@pytest.mark.parametrize(
    "source",
    [
        "class Repo:\n"
        "    def list_items_scoped(self, constraint):\n"
        "        self._require_scoped(constraint)\n"
        "        return getattr(type(self), 'list_' + 'items')(self)\n",
        "class Repo:\n"
        "    def list_items_scoped(self, constraint):\n"
        "        self._require_scoped(constraint)\n"
        "        return list(filter(\n"
        "            lambda row: row.application_id in constraint.allowed_identities,\n"
        "            rows,\n"
        "        ))\n",
    ],
)
def test_scoped_repository_gate_rejects_reflective_fallback_and_filter(source: str) -> None:
    with pytest.raises(SourceGateViolation):
        _validate_scoped_repository_ports(SRC / "repositories" / "fake.py", source)


def test_transient_serialization_gate_rejects_container_wrapping() -> None:
    source = (
        "import json\n"
        "def dump(claim: PendingAuthorityClaim):\n"
        "    return json.dumps({'claim': claim}, default=str)\n"
    )
    with pytest.raises(SourceGateViolation, match="transient-generic-serialization"):
        _validate_no_transient_generic_serializers(source)


def test_pipeline_order_gate_rejects_required_calls_after_return() -> None:
    source = (
        "def prepare_call():\n"
        "    require_authority_phase()\n"
        "    resolve()\n"
        "    require_authority_spec()\n"
        "    _require_entry_capabilities()\n"
        "    return denied\n"
        "    _pre_resolver_entry_scope_policy()\n"
        "    _audit_entry_bindings()\n"
        "    prepare_tool_call()\n"
        "def execute_prepared():\n"
        "    require_prepared_route()\n"
        "    require_authority_phase()\n"
        "    return denied\n"
        "    require_execution_claim_transaction()\n"
        "    begin_prepared_execution()\n"
        "def _execute_read():\n"
        "    _require_entry_capabilities()\n"
        "    return denied\n"
        "    _audit_entry_bindings()\n"
        "    executor()\n"
        "def _execute_claimed_write():\n"
        "    claim_lifecycle()\n"
        "    return denied\n"
        "    _typed_args_digest()\n"
        "    executor()\n"
    )
    with pytest.raises(SourceGateViolation, match="unreachable-call"):
        _validate_pipeline_order(source)


def test_binding_resolver_gate_rejects_repository_value_alias() -> None:
    source = (
        "def _authority_binding_resolution(): pass\n"
        "def resolve_identity_argument(repo):\n"
        "    storage = repo\n"
        "    return storage.get_application_scoped(1)\n"
        "def resolve_parent_application(): pass\n"
    )
    with pytest.raises(SourceGateViolation, match="binding-resolver-side-effect"):
        _validate_binding_resolver_purity(source)


def test_dependency_policy_gate_rejects_constructor_import_alias() -> None:
    source = (
        "from policy import DependencyPolicyV1 as Policy\n"
        "def build(): return Policy(version='copied')\n"
    )
    with pytest.raises(SourceGateViolation, match="copied-dependency-policy"):
        _validate_dependency_policy_and_autoapprove(source)


@pytest.mark.parametrize(
    "source",
    [
        "def dispatch(config):\n"
        "    enabled = bool(config.auto_approve)\n"
        "    if enabled:\n"
        "        return execute_prepared(value)\n",
        "def dispatch(config):\n"
        "    match config.auto_approve:\n"
        "        case True:\n"
        "            return execute_prepared(value)\n",
        "def dispatch(config, values):\n"
        "    return [execute_prepared(value) for value in values "
        "if config.auto_approve]\n",
    ],
)
def test_autoapprove_gate_rejects_alias_match_and_comprehension(source: str) -> None:
    with pytest.raises(SourceGateViolation, match="auto-approve-authority-bypass"):
        _validate_dependency_policy_and_autoapprove(source)


def test_provider_gate_rejects_nested_closure_invocation() -> None:
    source = (
        "def complete_model(surface):\n"
        "    preflight(surface, invocation_identity=invocation_identity)\n"
        "    complete_surface(surface, invocation_identity=invocation_identity)\n"
        "    stream_surface(surface, invocation_identity=invocation_identity)\n"
        "    def bypass():\n"
        "        return preflight(surface)\n"
        "    return bypass()\n"
    )
    with pytest.raises(SourceGateViolation, match="provider-invocation-nested"):
        _validate_provider_identity_keywords(source)


def test_authority_callsite_gate_rejects_dynamic_dictionary_dispatch() -> None:
    source = (
        "def bypass(factory):\n"
        "    ports = {'claim': factory.issue_pending_claim}\n"
        "    return ports['claim']()\n"
    )
    with pytest.raises(SourceGateViolation, match="dynamic-authority-dispatch"):
        _validate_authority_callsite_ownership({SRC / "api.py": source})


def test_transient_serialization_gate_rejects_indirect_helper_flow() -> None:
    source = (
        "import json\n"
        "def serialize(value):\n"
        "    return json.dumps(value, default=str)\n"
        "def dump(claim: PendingAuthorityClaim):\n"
        "    payload = {'claim': claim}\n"
        "    return serialize(payload)\n"
    )
    with pytest.raises(SourceGateViolation, match="transient-generic-serialization"):
        _validate_no_transient_generic_serializers(source)
