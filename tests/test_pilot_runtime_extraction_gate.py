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


_TASK12_AGENT_RUNTIME_PATHS = {
    "ai/agent_loop.py",
    "chat_transport.py",
    "context_projector/contracts.py",
    "context_projector/loader.py",
    "context_projector/manifest.py",
    "context_projector/projector.py",
    "context_projector/selector.py",
    "pilot_runtime/composition.py",
    "pilot_runtime/service.py",
}
_TASK12_SIGNAL_SYMBOLS = {
    "ConfirmedReadinessContributorPort",
    "InterviewReadinessSignal",
    "InterviewReadinessSignalEvidence",
    "InterviewReadinessSignalVersion",
    "PreparationReadinessSelectionLoader",
    "load_canonical_readiness_signal",
}


def _task12_call_terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _task12_callable_return_bindings(
    *scopes: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, str], dict[str, ast.Dict]]:
    strings: dict[str, str] = {}
    mappings: dict[str, ast.Dict] = {}

    def string_value(node: ast.AST) -> str | None:
        return (
            node.value
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            else strings.get(node.id)
            if isinstance(node, ast.Name)
            else None
        )

    def mapping_value(node: ast.AST) -> ast.Dict | None:
        if isinstance(node, ast.Dict):
            return node
        return mappings.get(node.id) if isinstance(node, ast.Name) else None

    for scope in scopes:
        for statement in scope.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                text = string_value(statement.value)
                mapping = mapping_value(statement.value)
                if text is None:
                    strings.pop(target.id, None)
                else:
                    strings[target.id] = text
                if mapping is None:
                    mappings.pop(target.id, None)
                else:
                    mappings[target.id] = mapping
    return strings, mappings


def _task12_callable_return_options(
    node: ast.AST,
    *,
    key: str | None = None,
    all_values: bool = False,
    strings: dict[str, str] | None = None,
    mappings: dict[str, ast.Dict] | None = None,
) -> tuple[ast.AST, ...]:
    known_strings = strings or {}
    known_mappings = mappings or {}

    def literal_string(value: ast.AST | None) -> str | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return known_strings.get(value.id) if isinstance(value, ast.Name) else None

    def mapping_value(value: ast.AST) -> ast.Dict | None:
        if isinstance(value, ast.Dict):
            return value
        return known_mappings.get(value.id) if isinstance(value, ast.Name) else None

    if isinstance(node, ast.IfExp):
        return (
            *_task12_callable_return_options(
                node.body,
                key=key,
                all_values=all_values,
                strings=known_strings,
                mappings=known_mappings,
            ),
            *_task12_callable_return_options(
                node.orelse,
                key=key,
                all_values=all_values,
                strings=known_strings,
                mappings=known_mappings,
            ),
        )
    if isinstance(node, ast.BoolOp):
        return tuple(
            option
            for item in node.values
            for option in _task12_callable_return_options(
                item,
                key=key,
                all_values=all_values,
                strings=known_strings,
                mappings=known_mappings,
            )
        )
    if key is not None:
        mapping = mapping_value(node)
        if mapping is None:
            return ()
        return tuple(
            option
            for item_key, item in zip(mapping.keys, mapping.values, strict=True)
            if literal_string(item_key) == key
            for option in _task12_callable_return_options(
                item,
                strings=known_strings,
                mappings=known_mappings,
            )
        )
    if all_values:
        mapping = mapping_value(node)
        if mapping is None:
            return ()
        return tuple(
            option
            for item in mapping.values
            for option in _task12_callable_return_options(
                item,
                strings=known_strings,
                mappings=known_mappings,
            )
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "pop", "setdefault"}
        and node.args
    ):
        selected_key = literal_string(node.args[0])
        mapping = mapping_value(node.func.value)
        if selected_key is not None and mapping is not None:
            selected = _task12_callable_return_options(
                mapping,
                key=selected_key,
                strings=known_strings,
                mappings=known_mappings,
            )
            if selected:
                return selected
            if len(node.args) > 1:
                return _task12_callable_return_options(
                    node.args[1],
                    strings=known_strings,
                    mappings=known_mappings,
                )
            return ()
    if isinstance(node, ast.Subscript):
        selected_key = literal_string(node.slice)
        mapping = mapping_value(node.value)
        if selected_key is not None and mapping is not None:
            return _task12_callable_return_options(
                mapping,
                key=selected_key,
                strings=known_strings,
                mappings=known_mappings,
            )
    return (node,)


def _task12_subtree_has_constant(
    node: ast.AST,
    expected: str,
    bindings: dict[str, str],
) -> bool:
    return any(_constant_string(child, bindings) == expected for child in ast.walk(node))


def _task12_statements_assign_constant(
    statements: list[ast.stmt],
    expected: str,
    bindings: dict[str, str],
) -> bool:
    return any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and _task12_constant_string(node.value, bindings) == expected
        for statement in statements
        for node in ast.walk(statement)
    )


def _task12_constant_string(node: ast.AST, bindings: dict[str, str]) -> str | None:
    direct = _constant_string(node, bindings)
    if direct is not None:
        return direct
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and not node.args
        and not node.keywords
    ):
        value = _task12_constant_string(node.func.value, bindings)
        if value is None:
            return None
        if node.func.attr == "lower":
            return value.lower()
        if node.func.attr == "upper":
            return value.upper()
        if node.func.attr == "casefold":
            return value.casefold()
        if node.func.attr == "strip":
            return value.strip()
    if isinstance(node, ast.Call):
        terminal = _task12_call_terminal(node.func)
        if terminal is not None:
            return bindings.get(f"{terminal}()")
    return None


def _task12_decorator_path(decorator: ast.AST) -> ast.AST | None:
    if not isinstance(decorator, ast.Call):
        return None
    if decorator.args:
        return decorator.args[0]
    return next(
        (
            keyword.value
            for keyword in decorator.keywords
            if keyword.arg == "path"
        ),
        None,
    )


def _task12_readiness_dicts(
    tree: ast.Module,
    bindings: dict[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    dictionaries: dict[str, dict[str, str]] = {}
    factories: dict[str, dict[str, str]] = {}

    def value(node: ast.AST) -> dict[str, str] | None:
        if isinstance(node, ast.Name):
            return dictionaries.get(node.id)
        if isinstance(node, ast.Call) and _task12_call_terminal(node.func) == "dict":
            result = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    spread = value(keyword.value)
                    if spread is not None:
                        result.update(spread)
                    continue
                resolved = _task12_constant_string(keyword.value, bindings)
                if resolved is not None:
                    result[keyword.arg] = resolved
            return result
        if isinstance(node, ast.Call):
            return factories.get(_task12_call_terminal(node.func) or "")
        if not isinstance(node, ast.Dict):
            return None
        result: dict[str, str] = {}
        for key, item in zip(node.keys, node.values, strict=True):
            if key is None:
                spread = value(item)
                if spread is not None:
                    result.update(spread)
                continue
            resolved_key = _task12_constant_string(key, bindings)
            resolved_value = _task12_constant_string(item, bindings)
            if resolved_key is not None and resolved_value is not None:
                result[resolved_key] = resolved_value
        return result

    for _ in range(12):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returned_values = [
                    value(returned.value)
                    for returned in ast.walk(node)
                    if isinstance(returned, ast.Return) and returned.value is not None
                ]
                returned_values = [item for item in returned_values if item is not None]
                if len(returned_values) == 1 and factories.get(node.name) != returned_values[0]:
                    factories[node.name] = returned_values[0]
                    changed = True
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            resolved = value(node.value)
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and dictionaries.get(target.id) != resolved:
                    dictionaries[target.id] = resolved
                    changed = True
        if not changed:
            break
    return dictionaries, factories


def _task12_has_confirmed_memory_disabled_guard(
    validator: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: dict[str, str],
) -> bool:
    def confirmed_status(node: ast.AST, loop_names: set[str]) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "status" and isinstance(
            node.value, ast.Subscript
        ):
            return (
                isinstance(node.value.value, ast.Name)
                and node.value.value.id == "contributors"
                and (
                    _task12_subtree_has_constant(
                        node.value.slice, "confirmed_memory", bindings
                    )
                    or any(
                        isinstance(child, ast.Name) and child.id in loop_names
                        for child in ast.walk(node.value.slice)
                    )
                )
            )
        if isinstance(node, ast.Name):
            return any(
                isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and assignment.value is not None
                and any(
                    isinstance(target, ast.Name) and target.id == node.id
                    for target in (
                        assignment.targets
                        if isinstance(assignment, ast.Assign)
                        else [assignment.target]
                    )
                )
                and confirmed_status(assignment.value, loop_names)
                for assignment in ast.walk(validator)
            )
        return False

    def is_guard(node: ast.If, loop_names: set[str]) -> bool:
        if not isinstance(node, ast.If) or not any(
            isinstance(child, ast.Raise)
            for statement in node.body
            for child in ast.walk(statement)
        ):
            return False
        test = node.test
        negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
        if negated:
            test = test.operand
        mentions_confirmed = _task12_subtree_has_constant(
            test, "confirmed_memory", bindings
        ) or any(isinstance(child, ast.Name) and child.id in loop_names for child in ast.walk(test))
        comparisons = [
            child
            for child in ast.walk(test)
            if isinstance(child, ast.Compare)
            and len(child.ops) == 1
            and len(child.comparators) == 1
        ]
        rejects_non_disabled = any(
            (
                (
                    _task12_constant_string(compare.left, bindings) == "disabled"
                    and confirmed_status(compare.comparators[0], loop_names)
                )
                or (
                    _task12_constant_string(compare.comparators[0], bindings) == "disabled"
                    and confirmed_status(compare.left, loop_names)
                )
            )
            and (
                isinstance(compare.ops[0], (ast.IsNot, ast.NotEq, ast.NotIn))
                if not negated
                else isinstance(compare.ops[0], (ast.Is, ast.Eq, ast.In))
            )
            for compare in comparisons
        )
        return mentions_confirmed and rejects_non_disabled

    guard_index: int | None = None
    for index, statement in enumerate(validator.body):
        if isinstance(statement, ast.If) and is_guard(statement, set()):
            guard_index = index
            break
        if isinstance(statement, (ast.For, ast.AsyncFor)) and isinstance(
            statement.target, ast.Name
        ) and _task12_subtree_has_constant(
            statement.iter, "confirmed_memory", bindings
        ):
            if any(
                isinstance(candidate, ast.If)
                and is_guard(candidate, {statement.target.id})
                for candidate in statement.body
            ):
                guard_index = index
                break
    if guard_index is None:
        return False
    if any(
        isinstance(node, ast.Return)
        for statement in validator.body[: guard_index + 1]
        for node in ast.walk(statement)
    ):
        return False
    for statement in validator.body[:guard_index]:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.If) and statement.body and all(
            isinstance(item, ast.Raise) for item in statement.body
        ) and not statement.orelse:
            continue
        return False
    return True


def _task12_scope_queries_signal(
    scope: ast.AST,
    aliases: dict[str, str],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    string_bindings: dict[str, str] | None = None,
    seen: frozenset[str] = frozenset(),
) -> bool:
    functions = functions or {}
    string_bindings = string_bindings or {}
    parents = {
        child: parent
        for parent in ast.walk(scope)
        for child in ast.iter_child_nodes(parent)
    }

    def reachable(node: ast.AST) -> bool:
        child = node
        parent = parents.get(child)
        while parent is not None:
            if (
                isinstance(parent, ast.If)
                and isinstance(parent.test, ast.Constant)
                and isinstance(parent.test.value, bool)
            ):
                if child in parent.body and not parent.test.value:
                    return False
                if child in parent.orelse and parent.test.value:
                    return False
            child = parent
            parent = parents.get(parent)
        return True

    signal_names = {
        node.id
        for node in ast.walk(scope)
        if isinstance(node, ast.Name)
        and (_qualified_symbol(node, aliases) or "").rsplit(".", 1)[-1]
        in _TASK12_SIGNAL_SYMBOLS
    }
    statement_names: set[str] = set()

    def query_expression(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in statement_names
        raw_values = {
            raw
            for child in ast.walk(node)
            if (raw := _task12_constant_string(child, string_bindings)) is not None
        }
        if any(
            "interview_readiness_signals" in raw.casefold()
            and " ".join(raw.casefold().split()).startswith(("select ", "with "))
            for raw in raw_values
        ):
            return True
        raw = _task12_constant_string(node, string_bindings)
        if raw is not None:
            normalized = " ".join(raw.casefold().split())
            if "interview_readiness_signals" in normalized and normalized.startswith(
                ("select ", "with ")
            ):
                return True
        if not isinstance(node, ast.Call):
            return False
        terminal = _task12_call_terminal(node.func) or ""
        if terminal == "load" and any(
            (_qualified_symbol(child, aliases) or "").rsplit(".", 1)[-1]
            == "PreparationReadinessSelectionLoader"
            for child in ast.walk(node)
            if isinstance(child, (ast.Name, ast.Attribute))
        ):
            return True
        if terminal in {"get", "query", "select"} and any(
            isinstance(child, ast.Name) and child.id in signal_names
            for child in ast.walk(node)
        ):
            return True
        if isinstance(node.func, ast.Attribute) and query_expression(node.func.value):
            return True
        return any(
            isinstance(child, ast.Name) and child.id in statement_names
            for child in ast.walk(node)
        )

    for _ in range(8):
        changed = False
        for node in ast.walk(scope):
            if not reachable(node):
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not query_expression(node.value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in statement_names:
                    statement_names.add(target.id)
                    changed = True
        if not changed:
            break
    for node in ast.walk(scope):
        if not reachable(node):
            continue
        if not isinstance(node, ast.Call):
            continue
        terminal = _task12_call_terminal(node.func) or ""
        if terminal in {"get", "query", "select"} and query_expression(node):
            return True
        if terminal in {"execute", "scalar", "scalars"} and any(
            query_expression(value)
            for value in (*node.args, *(item.value for item in node.keywords))
        ):
            return True
        if terminal == "load" and query_expression(node):
            return True
        callee = functions.get(terminal)
        if callee is not None and terminal not in seen and _task12_scope_queries_signal(
            callee,
            aliases,
            functions,
            string_bindings,
            seen | {terminal},
        ):
            return True
    return False


def _task12_runtime_boundary_path(name: str) -> bool:
    return name == "chat_transport.py" or name.startswith(
        ("agent_runtime/", "ai/", "context_projector/", "pilot_runtime/")
    )


def _task12_chat_haru_cross_source_query(sources: dict[str, str]) -> bool:
    trees = {name: ast.parse(source, filename=name) for name, source in sources.items()}
    aliases = {name: _module_binding_aliases(tree) for name, tree in trees.items()}
    functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    classes: set[tuple[str, str]] = set()
    closure_owners: dict[tuple[str, str], tuple[str, str]] = {}
    nested_callables: dict[tuple[str, str, str], tuple[str, str]] = {}

    def register_function(
        module: str,
        symbol: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        functions[module][symbol] = node
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nested_symbol = f"{symbol}.<locals>.{statement.name}"
            nested = (module, nested_symbol)
            closure_owners[nested] = (module, symbol)
            nested_callables[module, symbol, statement.name] = nested
            register_function(module, nested_symbol, statement)

    for name, tree in trees.items():
        functions[name] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                register_function(name, node.name, node)
            elif isinstance(node, ast.ClassDef):
                classes.add((name, node.name))
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        register_function(
                            name, f"{node.name}.{member.name}", member
                        )

    def target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for module in trees:
            dotted = (
                module.removesuffix("/__init__.py")
                if module.endswith("/__init__.py")
                else module.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), module, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def follow_export(
        called: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str]:
        if called in seen:
            return called
        module, symbol = called
        resolved = aliases[module].get(symbol)
        nested = target(resolved) if resolved is not None else None
        return follow_export(nested, seen | {called}) if nested is not None else called

    def call_target(
        module: str,
        node: ast.AST,
        instances: dict[str, tuple[str, str]] | None = None,
        scope_aliases: dict[str, str] | None = None,
        callable_bindings: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        instances = instances or {}
        callable_bindings = callable_bindings or {}
        if isinstance(node, ast.Name) and node.id in callable_bindings:
            return callable_bindings[node.id]
        if isinstance(node, ast.Await):
            return call_target(
                module, node.value, instances, scope_aliases, callable_bindings
            )
        if isinstance(node, ast.Call):
            factory = call_target(
                module,
                node.func,
                instances,
                scope_aliases,
                callable_bindings,
            )
            return (
                returned_callable(factory)
                or returned_instance(factory)
                or factory
                if factory is not None
                else None
            )
        if isinstance(node, ast.Subscript):
            key = (
                node.slice.value
                if isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                else None
            )
            if key is None or not isinstance(node.value, ast.Call):
                return None
            factory = call_target(
                module,
                node.value.func,
                instances,
                scope_aliases,
                callable_bindings,
            )
            return (
                returned_callable(factory, key=key)
                if factory is not None
                else None
            )
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in instances:
                constructor = instances[node.value.id]
                return constructor[0], f"{constructor[1]}.{node.attr}"
            if isinstance(node.value, ast.Call):
                factory = call_target(
                    module,
                    node.value.func,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
                constructor = (
                    returned_instance(factory) if factory is not None else None
                ) or factory
            else:
                constructor = call_target(
                    module,
                    node.value,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
            if constructor is not None:
                constructor = follow_export(constructor)
                return constructor[0], f"{constructor[1]}.{node.attr}"
        resolved = _qualified_symbol(node, scope_aliases or aliases[module]) or ""
        if resolved in callable_bindings:
            return callable_bindings[resolved]
        called = target(resolved)
        if called is not None:
            return follow_export(called)
        terminal = resolved.rsplit(".", 1)[-1]
        return (
            (module, terminal)
            if terminal in functions[module] or (module, terminal) in classes
            else None
        )

    def returned_instance(
        called: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        called = follow_export(called)
        if called in seen:
            return None
        function = functions.get(called[0], {}).get(called[1])
        if function is None:
            return called if called in classes else None
        scope_aliases = _scope_binding_aliases(aliases[called[0]], function)
        candidates: set[tuple[str, str]] = set()
        local_instances: dict[str, tuple[str, str]] = {}
        for assignment in ast.walk(function):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, ast.Call
            ):
                continue
            candidate = call_target(
                called[0], assignment.value.func, scope_aliases=scope_aliases
            )
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name) and candidate in classes:
                    local_instances[assignment_target.id] = candidate
        for returned in ast.walk(function):
            if not isinstance(returned, ast.Return) or returned.value is None:
                continue
            if isinstance(returned.value, ast.Name) and returned.value.id in local_instances:
                candidates.add(local_instances[returned.value.id])
                continue
            if not isinstance(returned.value, ast.Call):
                continue
            candidate = call_target(
                called[0], returned.value.func, scope_aliases=scope_aliases
            )
            if candidate in classes:
                candidates.add(candidate)
            elif candidate is not None:
                nested = returned_instance(candidate, seen | {called})
                if nested is not None:
                    candidates.add(nested)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def returned_callable(
        called: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
        *,
        key: str | None = None,
    ) -> tuple[str, str] | None:
        candidates = returned_callables(called, seen, key=key)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def returned_callables(
        called: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
        *,
        key: str | None = None,
        all_values: bool = False,
    ) -> set[tuple[str, str]]:
        called = follow_export(called)
        if called in seen:
            return set()
        function = functions.get(called[0], {}).get(called[1])
        if function is None:
            return set()
        scope_aliases = _scope_binding_aliases(aliases[called[0]], function)
        strings, mappings = _task12_callable_return_bindings(
            trees[called[0]],
            function,
        )
        candidates: set[tuple[str, str]] = set()
        for returned in ast.walk(function):
            if not isinstance(returned, ast.Return) or returned.value is None:
                continue
            for option in _task12_callable_return_options(
                returned.value,
                key=key,
                all_values=all_values,
                strings=strings,
                mappings=mappings,
            ):
                candidate = (
                    nested_callables.get((called[0], called[1], option.id))
                    if isinstance(option, ast.Name)
                    else None
                ) or call_target(
                    called[0], option, scope_aliases=scope_aliases
                )
                if candidate is not None and candidate not in (seen | {called}):
                    candidates.add(candidate)
        return candidates

    caller_string_cache: dict[str, dict[str, str]] = {}

    def known_caller_key(module: str, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if not isinstance(node, ast.Name):
            return None
        strings = caller_string_cache.get(module)
        if strings is None:
            strings = _task12_callable_return_bindings(trees[module])[0]
            caller_string_cache[module] = strings
        return strings.get(node.id)

    def iterable_options(
        module: str,
        node: ast.AST,
        instances: dict[str, tuple[str, str]],
        scope_aliases: dict[str, str],
        callable_bindings: dict[str, tuple[str, str]],
    ) -> set[tuple[str, str]]:
        if isinstance(node, ast.Call):
            terminal = _task12_call_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(
                    module,
                    node.args[0],
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"items", "popitem", "values"}
            ):
                owner_node = (
                    node.func.value.func
                    if isinstance(node.func.value, ast.Call)
                    else node.func.value
                )
                owners = call_options(
                    module,
                    owner_node,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
                return {
                    candidate
                    for owner in owners
                    for candidate in returned_callables(owner, all_values=True)
                }
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return {
                candidate
                for generator in node.generators
                for candidate in iterable_options(
                    module,
                    generator.iter,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
            }
        return set()

    def call_options(
        module: str,
        node: ast.AST,
        instances: dict[str, tuple[str, str]],
        scope_aliases: dict[str, str],
        callable_bindings: dict[str, tuple[str, str]],
    ) -> set[tuple[str, str]]:
        if isinstance(node, ast.IfExp):
            return call_options(
                module, node.body, instances, scope_aliases, callable_bindings
            ) | call_options(
                module, node.orelse, instances, scope_aliases, callable_bindings
            )
        if isinstance(node, ast.Call):
            terminal = _task12_call_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(
                    module,
                    node.args[0],
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"items", "popitem", "values"}
            ):
                return iterable_options(
                    module,
                    node,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop", "setdefault"}
                and node.args
            ):
                selected_key = known_caller_key(module, node.args[0])
                if selected_key is None:
                    return set()
                owner_node = (
                    node.func.value.func
                    if isinstance(node.func.value, ast.Call)
                    else node.func.value
                )
                owners = call_options(
                    module,
                    owner_node,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
                selected = {
                    candidate
                    for owner in owners
                    for candidate in returned_callables(
                        owner, key=selected_key
                    )
                }
                if selected:
                    return selected
                if len(node.args) > 1:
                    return call_options(
                        module,
                        node.args[1],
                        instances,
                        scope_aliases,
                        callable_bindings,
                    )
                return set()
            factories = call_options(
                module, node.func, instances, scope_aliases, callable_bindings
            )
            return {
                candidate
                for factory in factories
                for candidate in (
                    returned_callables(factory)
                    or ({factory} if factory not in classes else set())
                )
            }
        if isinstance(node, ast.Subscript):
            key = known_caller_key(module, node.slice)
            if key is not None and isinstance(node.value, ast.Call):
                owners = call_options(
                    module,
                    node.value.func,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
                return {
                    candidate
                    for owner in owners
                    for candidate in returned_callables(owner, key=key)
                }
            if isinstance(node.slice, ast.Constant) and isinstance(
                node.slice.value, int
            ):
                if (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr in {"items", "popitem"}
                    and node.slice.value != 1
                ):
                    return set()
                return iterable_options(
                    module,
                    node.value,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
        resolved = call_target(
            module, node, instances, scope_aliases, callable_bindings
        )
        return {resolved} if resolved is not None else set()

    api = trees.get("api.py")
    if api is None:
        return False
    pending: list[
        tuple[str, str, tuple[tuple[str, tuple[str, str]], ...]]
    ] = []
    string_maps = {module: _string_bindings(tree) for module, tree in trees.items()}
    for _ in range(16):
        changed = False
        for module, module_aliases in aliases.items():
            for local, resolved in module_aliases.items():
                called = target(resolved)
                visited: set[tuple[str, str]] = set()
                while called is not None and called not in visited:
                    visited.add(called)
                    value = string_maps[called[0]].get(called[1])
                    if value is not None:
                        if string_maps[module].get(local) != value:
                            string_maps[module][local] = value
                            changed = True
                        break
                    nested = aliases[called[0]].get(called[1])
                    called = target(nested) if nested is not None else None
        if not changed:
            break
    api_bindings = string_maps["api.py"]
    for node in functions["api.py"].values():
        if (
            any(fragment in node.name.casefold() for fragment in ("chat", "haru", "pilot"))
            or any(
                (route_node := _task12_decorator_path(decorator)) is not None
                and (
                    path := _task12_constant_string(route_node, api_bindings)
                ) is not None
                and any(
                    prefix in path
                    for prefix in ("/api/chat", "/api/haru", "/api/pilot")
                )
                for decorator in node.decorator_list
            )
        ):
            pending.append(("api.py", node.name, tuple()))
    includes_router = any(
        isinstance(call, ast.Call)
        and _task12_call_terminal(call.func) == "include_router"
        for call in ast.walk(api)
    )
    router_prefixes: dict[tuple[str, str], str] = {}
    for module, module_tree in trees.items():
        for assignment in module_tree.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, ast.Call
            ) or (_task12_call_terminal(assignment.value.func) or "") != "APIRouter":
                continue
            prefix = next(
                (
                    _task12_constant_string(keyword.value, string_maps[module])
                    for keyword in assignment.value.keywords
                    if keyword.arg == "prefix"
                ),
                "",
            ) or ""
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name):
                    router_prefixes[module, assignment_target.id] = prefix

    def resolve_router(module: str, node: ast.AST) -> tuple[str, str] | None:
        resolved = _qualified_symbol(node, aliases[module]) or ""
        local = (module, resolved.rsplit(".", 1)[-1])
        if local in router_prefixes:
            return local
        imported = target(resolved)
        if imported is None:
            return None
        imported = follow_export(imported)
        return imported

    def join_path(*parts: str) -> str:
        values = [part.strip("/") for part in parts if part and part != "/"]
        return f"/{'/'.join(values)}" if values else "/"

    included_routers: dict[tuple[str, str], set[str]] = {}
    router_pending: list[tuple[str, str, str]] = []
    if includes_router:
        for include in (node for node in ast.walk(api) if isinstance(node, ast.Call)):
            if (
                (_task12_call_terminal(include.func) or "") != "include_router"
                or not include.args
            ):
                continue
            router = resolve_router("api.py", include.args[0])
            if router is None:
                continue
            prefix = next(
                (
                    _task12_constant_string(keyword.value, string_maps["api.py"])
                    for keyword in include.keywords
                    if keyword.arg == "prefix"
                ),
                "",
            ) or ""
            router_pending.append((router[0], router[1], prefix))
    while router_pending:
        module, symbol, parent_prefix = router_pending.pop()
        prefixes = included_routers.setdefault((module, symbol), set())
        if parent_prefix in prefixes:
            continue
        prefixes.add(parent_prefix)
        own_prefix = router_prefixes.get((module, symbol), "")
        for include in (
            node for node in ast.walk(trees[module]) if isinstance(node, ast.Call)
        ):
            if (
                (_task12_call_terminal(include.func) or "") != "include_router"
                or not isinstance(include.func, ast.Attribute)
                or (_task12_call_terminal(include.func.value) or "") != symbol
                or not include.args
            ):
                continue
            child = resolve_router(module, include.args[0])
            if child is None:
                continue
            prefix = next(
                (
                    _task12_constant_string(keyword.value, string_maps[module])
                    for keyword in include.keywords
                    if keyword.arg == "prefix"
                ),
                "",
            ) or ""
            router_pending.append(
                (
                    child[0],
                    child[1],
                    join_path(parent_prefix, own_prefix, prefix),
                )
            )
    registration_modules = {
        module: trees[module]
        for module in {"api.py", *(module for module, _symbol in included_routers)}
    }
    for module, module_tree in registration_modules.items():
        for name, function in functions[module].items():
            if "." in name:
                continue
            matched_route = False
            for decorator in function.decorator_list:
                route_node = _task12_decorator_path(decorator)
                path = (
                    _task12_constant_string(route_node, string_maps[module])
                    if route_node is not None
                    else None
                )
                if path is None:
                    continue
                receiver = (
                    _task12_call_terminal(decorator.func.value)
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    else None
                )
                candidates = (
                    {
                        join_path(
                            parent_prefix,
                            router_prefixes.get((module, receiver), ""),
                            path,
                        )
                        for parent_prefix in included_routers.get(
                            (module, receiver or ""), set()
                        )
                    }
                    if receiver is not None
                    else {path}
                )
                if any(
                    candidate.startswith(prefix)
                    for candidate in candidates
                    for prefix in ("/api/chat", "/api/haru", "/api/pilot")
                ):
                    matched_route = True
                    break
            if matched_route:
                pending.append((module, name, tuple()))
        for registration in (
            node for node in ast.walk(module_tree) if isinstance(node, ast.Call)
        ):
            if (_task12_call_terminal(registration.func) or "") not in {
                "api_route",
                "add_api_route",
            }:
                continue
            route_node = (
                registration.args[0]
                if registration.args
                else next(
                    (
                        keyword.value
                        for keyword in registration.keywords
                        if keyword.arg == "path"
                    ),
                    None,
                )
            )
            if route_node is None:
                continue
            path = _task12_constant_string(
                route_node, string_maps[module]
            )
            receiver = (
                _task12_call_terminal(registration.func.value)
                if isinstance(registration.func, ast.Attribute)
                else None
            )
            candidates = (
                {
                    join_path(
                        parent_prefix,
                        router_prefixes.get((module, receiver), ""),
                        path or "",
                    )
                    for parent_prefix in included_routers.get(
                        (module, receiver or ""), set()
                    )
                }
                if receiver is not None and module != "api.py"
                else {path or ""}
            )
            if path is None or not any(
                candidate.startswith(prefix)
                for candidate in candidates
                for prefix in ("/api/chat", "/api/haru", "/api/pilot")
            ):
                continue
            handler = (
                registration.args[1]
                if len(registration.args) > 1
                else next(
                    (
                        keyword.value
                        for keyword in registration.keywords
                        if keyword.arg in {"endpoint", "route"}
                    ),
                    None,
                )
            )
            if handler is None:
                continue
            for nested in (
                node for node in ast.walk(handler) if isinstance(node, ast.Call)
            ):
                called = call_target(module, nested.func)
                if called is not None:
                    pending.append((called[0], called[1], tuple()))
    seen: set[
        tuple[str, str, tuple[tuple[str, tuple[str, str]], ...]]
    ] = set()
    while pending:
        module, symbol, frozen_bindings = pending.pop()
        state = (module, symbol, frozen_bindings)
        if state in seen:
            continue
        seen.add(state)
        callable_bindings = dict(frozen_bindings)
        function = functions[module].get(symbol)
        if function is None:
            continue
        scope_aliases = _scope_binding_aliases(aliases[module], function)
        if _task12_scope_queries_signal(
            function,
            scope_aliases,
            functions[module],
            _string_bindings(trees[module]),
        ):
            return True
        instances: dict[str, tuple[str, str]] = {}
        for _ in range(8):
            changed = False
            for assignment in ast.walk(function):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                    assignment.value, ast.Call
                ):
                    continue
                constructor = call_target(
                    module,
                    assignment.value,
                    instances,
                    scope_aliases,
                    callable_bindings,
                )
                if constructor is None:
                    continue
                targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                for assignment_target in targets:
                    if isinstance(assignment_target, ast.Name) and instances.get(
                        assignment_target.id
                    ) != constructor:
                        resolved = follow_export(constructor)
                        if resolved in classes:
                            instances[assignment_target.id] = resolved
                            changed = True
                        elif resolved[1] in functions.get(resolved[0], {}):
                            if callable_bindings.get(assignment_target.id) != resolved:
                                callable_bindings[assignment_target.id] = resolved
                                changed = True
            if not changed:
                break

        def bindings_before(node: ast.AST) -> dict[str, tuple[str, str]]:
            current = dict(callable_bindings)
            line = getattr(node, "lineno", float("inf"))
            for statement in function.body:
                if getattr(statement, "lineno", 0) >= line:
                    break
                if not isinstance(
                    statement, (ast.Assign, ast.AnnAssign)
                ) or statement.value is None:
                    continue
                candidate = call_target(
                    module,
                    statement.value,
                    instances,
                    scope_aliases,
                    current,
                )
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for assignment_target in targets:
                    binding_name = _qualified_symbol(
                        assignment_target, scope_aliases
                    )
                    if not binding_name:
                        continue
                    if candidate is None:
                        current.pop(binding_name, None)
                    else:
                        current[binding_name] = candidate
            return current

        comprehension_calls: dict[int, set[tuple[str, str]]] = {}
        for comprehension in (
            node
            for node in ast.walk(function)
            if isinstance(
                node,
                (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp),
            )
        ):
            expressions = (
                (comprehension.key, comprehension.value)
                if isinstance(comprehension, ast.DictComp)
                else (comprehension.elt,)
            )
            for generator in comprehension.generators:
                candidates = iterable_options(
                    module,
                    generator.iter,
                    instances,
                    scope_aliases,
                    bindings_before(generator.iter),
                )
                names = {
                    item.id
                    for item in ast.walk(generator.target)
                    if isinstance(item, ast.Name)
                }
                for expression_node in expressions:
                    for call in (
                        item
                        for item in ast.walk(expression_node)
                        if isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Name)
                        and item.func.id in names
                    ):
                        comprehension_calls.setdefault(id(call), set()).update(
                            candidates
                        )
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            call_bindings = bindings_before(call)
            called_options = comprehension_calls.get(id(call))
            mapping_dispatch = False
            if called_options is None:
                mapping_dispatch = (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr
                    in {"get", "items", "pop", "popitem", "setdefault", "values"}
                )
                called_options = call_options(
                    module,
                    call if mapping_dispatch else call.func,
                    instances,
                    scope_aliases,
                    call_bindings,
                )
            if not called_options and not mapping_dispatch:
                called_options = call_options(
                    module, call, instances, scope_aliases, call_bindings
                )
            for called in called_options:
                if called[1] not in functions.get(called[0], {}):
                    continue
                callee = functions[called[0]][called[1]]
                parameters = (*callee.args.posonlyargs, *callee.args.args)
                if "." in called[1] and parameters and parameters[0].arg in {
                    "cls",
                    "self",
                }:
                    parameters = parameters[1:]
                next_bindings: dict[str, tuple[str, str]] = {}
                closure_owner = closure_owners.get(called)
                if closure_owner is not None:
                    owner = functions[closure_owner[0]][closure_owner[1]]
                    owner_parameters = (
                        *owner.args.posonlyargs,
                        *owner.args.args,
                    )
                    owner_call = (
                        call.func if isinstance(call.func, ast.Call) else call
                    )
                    for parameter, argument in zip(
                        owner_parameters,
                        owner_call.args,
                        strict=False,
                    ):
                        candidate = call_target(
                            module,
                            argument,
                            instances,
                            scope_aliases,
                            call_bindings,
                        )
                        if candidate is not None:
                            next_bindings[parameter.arg] = candidate
                    owner_aliases = _scope_binding_aliases(
                        aliases[closure_owner[0]], owner
                    )
                    for statement in owner.body:
                        if not isinstance(
                            statement, (ast.Assign, ast.AnnAssign)
                        ) or statement.value is None:
                            continue
                        candidate = call_target(
                            closure_owner[0],
                            statement.value,
                            scope_aliases=owner_aliases,
                            callable_bindings=next_bindings,
                        )
                        assignment_targets = (
                            statement.targets
                            if isinstance(statement, ast.Assign)
                            else [statement.target]
                        )
                        for assignment_target in assignment_targets:
                            binding_name = _qualified_symbol(
                                assignment_target, owner_aliases
                            )
                            if not binding_name:
                                continue
                            if candidate is None:
                                next_bindings.pop(binding_name, None)
                            else:
                                next_bindings[binding_name] = candidate
                if (
                    "." in called[1]
                    and ".<locals>." not in called[1]
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Call)
                ):
                    class_name = called[1].split(".", 1)[0]
                    constructor = call_target(
                        module,
                        call.func.value.func,
                        instances,
                        scope_aliases,
                        call_bindings,
                    )
                    initializer = functions.get(called[0], {}).get(
                        f"{class_name}.__init__"
                    )
                    if constructor == (called[0], class_name) and initializer:
                        init_parameters = (
                            *initializer.args.posonlyargs,
                            *initializer.args.args,
                        )[1:]
                        init_bindings: dict[str, tuple[str, str]] = {}
                        for parameter, argument in zip(
                            init_parameters,
                            call.func.value.args,
                            strict=False,
                        ):
                            candidate = call_target(
                                module,
                                argument,
                                instances,
                                scope_aliases,
                                call_bindings,
                            )
                            if candidate is not None:
                                init_bindings[parameter.arg] = candidate
                        for assignment in ast.walk(initializer):
                            if not isinstance(assignment, ast.Assign):
                                continue
                            if not isinstance(assignment.value, ast.Name):
                                continue
                            candidate = init_bindings.get(assignment.value.id)
                            if candidate is None:
                                continue
                            for assignment_target in assignment.targets:
                                if (
                                    isinstance(assignment_target, ast.Attribute)
                                    and isinstance(assignment_target.value, ast.Name)
                                    and assignment_target.value.id == "self"
                                ):
                                    next_bindings[
                                        f"self.{assignment_target.attr}"
                                    ] = candidate
                if (
                    "." in called[1]
                    and ".<locals>." not in called[1]
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                ):
                    receiver = call.func.value.id
                    prefix = f"{receiver}."
                    for binding_name, candidate in call_bindings.items():
                        if binding_name.startswith(prefix):
                            next_bindings[
                                f"self.{binding_name[len(prefix):]}"
                            ] = candidate
                for parameter, argument in zip(parameters, call.args, strict=False):
                    candidate = call_target(
                        module,
                        argument,
                        instances,
                        scope_aliases,
                        call_bindings,
                    )
                    if candidate is not None:
                        next_bindings[parameter.arg] = candidate
                for keyword in call.keywords:
                    if keyword.arg is None:
                        continue
                    candidate = call_target(
                        module,
                        keyword.value,
                        instances,
                        scope_aliases,
                        call_bindings,
                    )
                    if candidate is not None:
                        next_bindings[keyword.arg] = candidate
                next_state = (
                    called[0],
                    called[1],
                    tuple(sorted(next_bindings.items())),
                )
                if next_state not in seen:
                    pending.append(next_state)
    return False


def _task12_readiness_runtime_violations(sources: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for name, source in sources.items():
        tree = ast.parse(source, filename=name)
        aliases = _module_binding_aliases(tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        scoped_alias_cache: dict[int, dict[str, str]] = {}

        def aliases_for(node: ast.AST) -> dict[str, str]:
            owner: ast.AST | None = node
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return aliases
            return scoped_alias_cache.setdefault(
                id(owner), _scope_binding_aliases(aliases, owner)
            )
        bindings = _string_bindings(tree)
        expressions: dict[str, ast.AST] = {}
        string_callables: dict[str, ast.AST] = {}
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                string_callables[item.name] = item
            elif isinstance(item, ast.ClassDef):
                for member in item.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        string_callables[f"{item.name}.{member.name}"] = member
            elif isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        expressions[target.id] = item.value
                        if isinstance(item.value, ast.Lambda):
                            string_callables[target.id] = item.value

        expression_scope_cache: dict[int, dict[str, ast.AST]] = {}

        def owner_scope(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
            owner: ast.AST | None = node
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            return owner if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else None

        def scoped_expressions(
            scope: ast.FunctionDef | ast.AsyncFunctionDef | None,
        ) -> dict[str, ast.AST]:
            if scope is None:
                return expressions
            if id(scope) in expression_scope_cache:
                return expression_scope_cache[id(scope)]
            values = dict(expressions)

            def collect(
                statements: list[ast.stmt], state: dict[str, ast.AST]
            ) -> None:
                for item in statements:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                state[target.id] = item.value
                    elif isinstance(item, ast.If):
                        if isinstance(item.test, ast.Constant) and isinstance(
                            item.test.value, bool
                        ):
                            collect(
                                item.body if item.test.value else item.orelse,
                                state,
                            )
                        else:
                            before = dict(state)
                            body_state = dict(before)
                            else_state = dict(before)
                            collect(item.body, body_state)
                            collect(item.orelse, else_state)
                            for name in body_state.keys() | else_state.keys():
                                body_value = body_state.get(name, before.get(name))
                                else_value = else_state.get(name, before.get(name))
                                if body_value is None or else_value is None:
                                    continue
                                if ast.dump(body_value) == ast.dump(else_value):
                                    state[name] = body_value
                                else:
                                    state[name] = ast.IfExp(
                                        test=item.test,
                                        body=body_value,
                                        orelse=else_value,
                                    )
                    elif isinstance(
                        item, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)
                    ):
                        collect(item.body, state)

            collect(scope.body, values)
            expression_scope_cache[id(scope)] = values
            return values

        def callable_key(node: ast.AST, active_expressions: dict[str, ast.AST]) -> str:
            if isinstance(node, ast.Name):
                alias = active_expressions.get(node.id)
                return alias.id if isinstance(alias, ast.Name) else node.id
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                return f"{node.value.id}.{node.attr}"
            return _task12_call_terminal(node) or ""

        def possible_strings(
            node: ast.AST,
            seen: frozenset[str] = frozenset(),
            scope: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
        ) -> set[str]:
            scope = scope or owner_scope(node)
            active_expressions = scoped_expressions(scope)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"casefold", "lower", "strip", "upper"}
                and (direct := _task12_constant_string(node, bindings)) is not None
            ):
                return {direct}
            if (
                isinstance(node, ast.Name)
                and node.id not in seen
                and (value := active_expressions.get(node.id)) is not None
            ):
                return possible_strings(value, seen | {node.id}, scope)
            if not isinstance(node, ast.Call):
                direct = _task12_constant_string(node, bindings)
                if direct is not None:
                    return {direct}
            if isinstance(node, ast.Name) and node.id not in seen:
                return set()
            if isinstance(node, ast.IfExp):
                return possible_strings(node.body, seen, scope) | possible_strings(
                    node.orelse, seen, scope
                )
            if not isinstance(node, ast.Call):
                return {
                    value
                    for child in ast.iter_child_nodes(node)
                    for value in possible_strings(child, seen, scope)
                }
            terminal = callable_key(node.func, active_expressions)
            if terminal in seen:
                return set()
            callee = string_callables.get(terminal)
            if callee is None and isinstance(node.func, ast.Name):
                alias = active_expressions.get(node.func.id)
                visited_aliases: set[str] = set()
                while isinstance(alias, ast.Name) and alias.id not in visited_aliases:
                    visited_aliases.add(alias.id)
                    terminal = alias.id
                    callee = string_callables.get(alias.id)
                    if callee is not None:
                        break
                    alias = active_expressions.get(alias.id)
                if isinstance(alias, ast.Lambda):
                    callee = alias
            if isinstance(callee, ast.Lambda):
                return possible_strings(callee.body, seen | {terminal}, scope)
            if isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return {
                    value
                    for returned in ast.walk(callee)
                    if isinstance(returned, ast.Return) and returned.value is not None
                    for value in possible_strings(
                        returned.value, seen | {terminal}, callee
                    )
                }
            return set()
        for _ in range(8):
            changed = False
            for assignment in ast.walk(tree):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                    continue
                resolved = _task12_constant_string(assignment.value, bindings)
                if resolved is None:
                    continue
                targets = (
                    assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and bindings.get(target.id) != resolved:
                        bindings[target.id] = resolved
                        changed = True
            if not changed:
                break
        for _ in range(12):
            changed = False
            for function in (
                item
                for item in tree.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                values = {
                    value
                    for returned in ast.walk(function)
                    if isinstance(returned, ast.Return) and returned.value is not None
                    if (value := _task12_constant_string(returned.value, bindings)) is not None
                }
                if len(values) == 1:
                    value = next(iter(values))
                    key = f"{function.name}()"
                    if bindings.get(key) != value:
                        bindings[key] = value
                        changed = True
            if not changed:
                break
        dictionaries, factories = _task12_readiness_dicts(tree, bindings)
        imports = _imports(tree)
        resolved_names = _resolved_names(tree, aliases)
        confirmed_objects: set[str] = set()
        while True:
            changed = False
            for assignment in ast.walk(tree):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                    continue
                is_confirmed = (
                    isinstance(assignment.value, ast.Call)
                    and (_task12_call_terminal(assignment.value.func) or "")
                    == "ContributorResult"
                    and any(
                        possible_strings(keyword.value) == {"confirmed_memory"}
                        for keyword in assignment.value.keywords
                        if keyword.arg == "name"
                    )
                ) or (
                    isinstance(assignment.value, ast.Name)
                    and assignment.value.id in confirmed_objects
                )
                if not is_confirmed:
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in confirmed_objects:
                        confirmed_objects.add(target.id)
                        changed = True
            if not changed:
                break

        def lowlevel_status_target(target: ast.AST, names: set[str]) -> bool:
            return (
                isinstance(target, ast.Subscript)
                and "status" in possible_strings(target.slice)
                and isinstance(target.value, ast.Call)
                and (_task12_call_terminal(target.value.func) or "")
                == "__getattribute__"
                and len(target.value.args) >= 2
                and isinstance(target.value.args[0], ast.Name)
                and target.value.args[0].id in names
                and "__dict__" in possible_strings(target.value.args[1])
            )

        if _task12_runtime_boundary_path(name) and (
            "offerpilot.review_readiness.contributor" in imports
            or "ConfirmedReadinessContributorPort" in resolved_names
        ):
            findings.append(f"confirmed-memory:registered:{name}")

        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and "ready" in possible_strings(node.value)
                and any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in confirmed_objects
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            ):
                findings.append(f"confirmed-memory:ready:{name}")
            if (
                isinstance(node, ast.If)
                and _task12_subtree_has_constant(node.test, "confirmed_memory", bindings)
                and _task12_statements_assign_constant(node.body, "ready", bindings)
            ):
                findings.append(f"confirmed-memory:ready:{name}")
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and "ready" in possible_strings(node.value)
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    _task12_subtree_has_constant(target, "confirmed_memory", bindings)
                    for target in targets
                ) or any(
                    lowlevel_status_target(target, confirmed_objects)
                    for target in targets
                ):
                    findings.append(f"confirmed-memory:ready:{name}")
            if not isinstance(node, ast.Call):
                continue
            node_aliases = aliases_for(node)
            terminal = (_qualified_symbol(node.func, node_aliases) or "").rsplit(".", 1)[-1]
            if (
                terminal in {"__setattr__", "setattr"}
                and len(node.args) >= 3
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in confirmed_objects
                and "status" in possible_strings(node.args[1])
                and "ready" in possible_strings(node.args[2])
            ):
                findings.append(f"confirmed-memory:ready:{name}")
            if (
                terminal == "update"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "__dict__"
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id in confirmed_objects
                and any(
                    (
                        isinstance(argument, ast.Dict)
                        and any(
                            "status" in possible_strings(key)
                            and "ready" in possible_strings(value)
                            for key, value in zip(
                                argument.keys, argument.values, strict=True
                            )
                            if key is not None
                        )
                    )
                    for argument in node.args
                )
            ):
                findings.append(f"confirmed-memory:ready:{name}")
            if (
                terminal == "replace"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in confirmed_objects
                and any(
                    keyword.arg == "status"
                    and "ready" in possible_strings(keyword.value)
                    for keyword in node.keywords
                )
            ):
                findings.append(f"confirmed-memory:ready:{name}")
            helper = string_callables.get(terminal)
            if isinstance(helper, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = (*helper.args.posonlyargs, *helper.args.args)
                bound_arguments = {
                    parameter.arg: argument
                    for parameter, argument in zip(parameters, node.args, strict=False)
                }
                bound_arguments.update(
                    {
                        keyword.arg: keyword.value
                        for keyword in node.keywords
                        if keyword.arg is not None
                    }
                )
                bound_values = {
                    parameter: possible_strings(argument)
                    for parameter, argument in bound_arguments.items()
                }
                for parameter in parameters:
                    argument = bound_arguments.get(parameter.arg)
                    if not (
                        isinstance(argument, ast.Name)
                        and argument.id in confirmed_objects
                    ):
                        continue
                    parameter_aliases = {parameter.arg}
                    alias_changed = True
                    while alias_changed:
                        alias_changed = False
                        for assignment in ast.walk(helper):
                            if not isinstance(
                                assignment, (ast.Assign, ast.AnnAssign)
                            ) or not isinstance(assignment.value, ast.Name):
                                continue
                            if assignment.value.id not in parameter_aliases:
                                continue
                            targets = (
                                assignment.targets
                                if isinstance(assignment, ast.Assign)
                                else [assignment.target]
                            )
                            for target in targets:
                                if (
                                    isinstance(target, ast.Name)
                                    and target.id not in parameter_aliases
                                ):
                                    parameter_aliases.add(target.id)
                                    alias_changed = True
                    def helper_values(value: ast.AST) -> set[str]:
                        return (
                            bound_values.get(value.id, set())
                            if isinstance(value, ast.Name)
                            else possible_strings(value, scope=helper)
                        )

                    helper_ready = any(
                        isinstance(assignment, (ast.Assign, ast.AnnAssign))
                        and assignment.value is not None
                        and "ready"
                        in (
                            bound_values.get(assignment.value.id, set())
                            if isinstance(assignment.value, ast.Name)
                            else possible_strings(assignment.value, scope=helper)
                        )
                        and any(
                            (
                                isinstance(target, ast.Attribute)
                                and target.attr == "status"
                                and isinstance(target.value, ast.Name)
                                and target.value.id in parameter_aliases
                            )
                            or lowlevel_status_target(target, parameter_aliases)
                            for target in (
                                assignment.targets
                                if isinstance(assignment, ast.Assign)
                                else [assignment.target]
                            )
                        )
                        for assignment in ast.walk(helper)
                    ) or any(
                        isinstance(call, ast.Call)
                        and (_task12_call_terminal(call.func) or "")
                        in {"__setattr__", "setattr"}
                        and len(call.args) >= 3
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id in parameter_aliases
                        and "status" in helper_values(call.args[1])
                        and "ready" in helper_values(call.args[2])
                        for call in ast.walk(helper)
                    ) or any(
                        isinstance(call, ast.Call)
                        and (_task12_call_terminal(call.func) or "") == "update"
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Attribute)
                        and call.func.value.attr == "__dict__"
                        and isinstance(call.func.value.value, ast.Name)
                        and call.func.value.value.id in parameter_aliases
                        and any(
                            isinstance(argument, ast.Dict)
                            and any(
                                "status" in helper_values(key)
                                and "ready" in helper_values(value)
                                for key, value in zip(
                                    argument.keys, argument.values, strict=True
                                )
                                if key is not None
                            )
                            for argument in call.args
                        )
                        for call in ast.walk(helper)
                    )
                    if helper_ready:
                        findings.append(f"confirmed-memory:ready:{name}")
            if terminal == "ContributorResult":
                values = [
                    _task12_constant_string(value, bindings)
                    for value in (*node.args, *(item.value for item in node.keywords))
                ]
                expanded: dict[str, str] = {}
                for keyword in node.keywords:
                    if keyword.arg is not None:
                        continue
                    if isinstance(keyword.value, ast.Name):
                        expanded.update(dictionaries.get(keyword.value.id, {}))
                    elif (
                        isinstance(keyword.value, ast.Call)
                        and _task12_call_terminal(keyword.value.func) == "dict"
                    ):
                        for item in keyword.value.keywords:
                            resolved_value = _task12_constant_string(item.value, bindings)
                            if item.arg is not None and resolved_value is not None:
                                expanded[item.arg] = resolved_value
                    elif isinstance(keyword.value, ast.Call):
                        expanded.update(
                            factories.get(_task12_call_terminal(keyword.value.func) or "", {})
                        )
                    elif isinstance(keyword.value, ast.Dict):
                        for key, value in zip(
                            keyword.value.keys, keyword.value.values, strict=True
                        ):
                            resolved_key = (
                                _task12_constant_string(key, bindings)
                                if key is not None
                                else None
                            )
                            resolved_value = _task12_constant_string(value, bindings)
                            if resolved_key is not None and resolved_value is not None:
                                expanded[resolved_key] = resolved_value
                values.extend(expanded.values())
                possible_values = {
                    value
                    for argument in (*node.args, *(item.value for item in node.keywords))
                    for value in possible_strings(argument)
                }
                if "confirmed_memory" in {*values, *possible_values} and "ready" in {
                    *values,
                    *possible_values,
                }:
                    findings.append(f"confirmed-memory:ready:{name}")
            if terminal in {"register", "register_contributor"} and any(
                (_qualified_symbol(value, node_aliases) or "").rsplit(".", 1)[-1]
                == "ConfirmedReadinessContributorPort"
                for value in (*node.args, *(item.value for item in node.keywords))
            ):
                findings.append(f"confirmed-memory:registered:{name}")

        query_scopes: list[ast.AST] = []
        if _task12_runtime_boundary_path(name):
            query_scopes.append(tree)
        elif name == "api.py":
            query_scopes.extend(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (
                    "chat" in node.name.casefold()
                    or "haru" in node.name.casefold()
                    or "pilot" in node.name.casefold()
                    or any(
                        (route_node := _task12_decorator_path(decorator)) is not None
                        and isinstance(route_node, ast.Constant)
                        and isinstance(route_node.value, str)
                        and (
                            "/api/chat" in route_node.value
                            or "/api/haru" in route_node.value
                            or "/api/pilot" in route_node.value
                        )
                        for decorator in node.decorator_list
                    )
                )
            )
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if any(
            _task12_scope_queries_signal(scope, aliases_for(scope), functions, bindings)
            for scope in query_scopes
        ):
            findings.append(f"chat-haru:signal-query:{name}")

        if name == "context_projector/projector.py":
            validator = next(
                (
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "_validate_contributors"
                ),
                None,
            )
            if validator is None or not _task12_has_confirmed_memory_disabled_guard(
                validator, bindings
            ):
                findings.append(
                    "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
                )
    if _task12_chat_haru_cross_source_query(sources):
        findings.append("chat-haru:signal-query:api.py")
    return sorted(set(findings))


def test_future_readiness_contributor_is_absent_from_agent_runtime_call_graph() -> None:
    future_symbol = "ConfirmedReadinessContributorPort"
    future_module = "offerpilot.review_readiness.contributor"
    paths = (
        SRC / "ai" / "agent_loop.py",
        SRC / "context_projector" / "projector.py",
        SRC / "context_projector" / "selector.py",
        RUNTIME / "composition.py",
        RUNTIME / "service.py",
    )

    findings: list[str] = []
    for path in paths:
        tree = _tree(path)
        imports = _imports(tree)
        if future_module in imports or future_symbol in _resolved_names(tree):
            findings.append(str(path.relative_to(ROOT)))

    assert findings == []


def test_task12_readiness_runtime_gate_rejects_ready_registration_and_signal_queries() -> None:
    safe = {
        "context_projector/projector.py": """
def _validate_contributors(contributors):
    for name in ("confirmed_memory", "knowledge_context", "older_conversation_summary"):
        if contributors[name].status != "disabled":
            raise ValueError(name)
""",
        "ai/agent_loop.py": """
def contributors(names):
    result = []
    for name in names:
        if name in {"confirmed_memory", "knowledge_context", "older_conversation_summary"}:
            status = "disabled"
        else:
            status = "ready"
        result.append(ContributorResult(name, status, ()))
    return result
""",
        "api.py": """
def _load_chat_source_messages():
    return load_chat_messages()
""",
    }
    assert _task12_readiness_runtime_violations(safe) == []

    ready_alias = {
        "ai/agent_loop.py": """
READY = "ready"
MEMORY = "confirmed_memory"
def build():
    status = READY
    return ContributorResult(MEMORY, status, ())
"""
    }
    assert _task12_readiness_runtime_violations(ready_alias) == [
        "confirmed-memory:ready:ai/agent_loop.py"
    ]

    ready_spread = {
        "ai/agent_loop.py": '''
MEMORY = "confirmed_memory"
READY = "READY".lower()
def build():
    values = {"name": MEMORY, "status": READY, "messages": ()}
    return ContributorResult(**values)
''',
    }
    assert _task12_readiness_runtime_violations(ready_spread) == [
        "confirmed-memory:ready:ai/agent_loop.py"
    ]
    direct_ready_spread = {
        "ai/agent_loop.py": '''
def build():
    return ContributorResult(
        **dict(name="confirmed_memory", status="READY".casefold(), messages=())
    )
''',
    }
    assert _task12_readiness_runtime_violations(direct_ready_spread) == [
        "confirmed-memory:ready:ai/agent_loop.py"
    ]
    aliased_ready_spread = {
        "ai/agent_loop.py": '''
from offerpilot.context_projector.contracts import ContributorResult as Result
def build():
    values = {"name": "confirmed_memory", "status": "ready", "messages": ()}
    return Result(**values)
''',
    }
    assert _task12_readiness_runtime_violations(aliased_ready_spread) == [
        "confirmed-memory:ready:ai/agent_loop.py"
    ]
    helper_ready_spread = {
        "pilot_runtime/new_adapter.py": '''
from offerpilot.context_projector.contracts import ContributorResult as Result
def values():
    return dict(name="confirmed_memory", status="ready", messages=())
def factory():
    return values()
def build():
    return Result(**factory())
''',
    }
    assert _task12_readiness_runtime_violations(helper_ready_spread) == [
        "confirmed-memory:ready:pilot_runtime/new_adapter.py"
    ]
    helper_status_ready = {
        "pilot_runtime/new_adapter.py": '''
def status():
    return "READY".casefold()
def build():
    return ContributorResult(name="confirmed_memory", status=status(), messages=())
''',
    }
    assert _task12_readiness_runtime_violations(helper_status_ready) == [
        "confirmed-memory:ready:pilot_runtime/new_adapter.py"
    ]

    registered = {
        "pilot_runtime/composition.py": """
from offerpilot.review_readiness.contributor import ConfirmedReadinessContributorPort as Port
def compose(registry):
    registry.register("confirmed_memory", Port)
"""
    }
    findings = _task12_readiness_runtime_violations(registered)
    assert "confirmed-memory:registered:pilot_runtime/composition.py" in findings

    signal_query = {
        "api.py": """
def _load_chat_source_messages(session):
    model = InterviewReadinessSignal
    return session.scalars(select(model))
"""
    }
    assert _task12_readiness_runtime_violations(signal_query) == ["chat-haru:signal-query:api.py"]

    signal_query_any_chat_function = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def send_chat(session):
    statement = select(Signal)
    return session.execute(statement)
''',
    }
    assert _task12_readiness_runtime_violations(signal_query_any_chat_function) == [
        "chat-haru:signal-query:api.py"
    ]

    signal_query_haru_route = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
@app.post("/api/pilot/context")
def prepare_context(session):
    return session.scalars(select(Signal))
''',
    }
    assert _task12_readiness_runtime_violations(signal_query_haru_route) == [
        "chat-haru:signal-query:api.py"
    ]
    shared_raw_signal_query = {
        "api.py": '''
SQL = "SELECT * FROM interview_readiness_signals WHERE id = :id"
def shared(session):
    return session.execute(text(SQL))
def helper(session):
    return shared(session)
@app.post("/api/chat/context")
def unrelated_name(session):
    return helper(session)
''',
    }
    assert _task12_readiness_runtime_violations(shared_raw_signal_query) == [
        "chat-haru:signal-query:api.py"
    ]
    cross_source_signal_query = {
        "api.py": '''
from offerpilot.services.context import load_context
@app.post("/api/chat/context")
def route(session):
    return load_context(session)
''',
        "services/context.py": '''
from offerpilot.models import InterviewReadinessSignal as S
def load_context(session):
    return session.scalars(select(S))
''',
    }
    assert _task12_readiness_runtime_violations(cross_source_signal_query) == [
        "chat-haru:signal-query:api.py"
    ]

    missing_validator = {"context_projector/projector.py": "def project(): return None\n"}
    assert _task12_readiness_runtime_violations(missing_validator) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]

    reversed_validator = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    if contributors["confirmed_memory"].status == "disabled":
        raise ValueError("wrong direction")
''',
    }
    assert _task12_readiness_runtime_violations(reversed_validator) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]
    equivalent_validator = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    for name in ("confirmed_memory",):
        if not contributors[name].status == "disabled":
            raise ValueError(name)
''',
    }
    assert _task12_readiness_runtime_violations(equivalent_validator) == []
    nested_unused_validator = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    def unused():
        if contributors["confirmed_memory"].status != "disabled":
            raise ValueError("unused")
    return contributors
''',
    }
    assert _task12_readiness_runtime_violations(nested_unused_validator) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]
    unreachable_guard = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    return contributors
    if contributors["confirmed_memory"].status != "disabled":
        raise ValueError("late")
''',
    }
    assert _task12_readiness_runtime_violations(unreachable_guard) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]
    late_guard = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    consume(contributors)
    for name in ("confirmed_memory",):
        if contributors[name].status != "disabled":
            raise ValueError(name)
    return contributors
''',
    }
    assert _task12_readiness_runtime_violations(late_guard) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]


def test_task12_readiness_runtime_production_gate() -> None:
    paths = tuple(
        path
        for path in sorted(SRC.rglob("*.py"))
        if (relative := path.relative_to(SRC).as_posix()) == "api.py"
        or _task12_runtime_boundary_path(relative)
    )
    sources = {path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8") for path in paths}
    assert _task12_readiness_runtime_violations(sources) == []


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


def _module_binding_aliases(tree: ast.Module) -> dict[str, str]:
    return _binding_aliases(
        ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
            ],
            type_ignores=[],
        )
    )


def _scope_binding_aliases(
    module_aliases: dict[str, str],
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, str]:
    nodes: list[ast.AST] = list(function.body)
    scoped: list[ast.AST] = []
    while nodes:
        node = nodes.pop()
        scoped.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes.extend(ast.iter_child_nodes(node))
    aliases = dict(module_aliases)
    for node in scoped:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{module}.{item.name}"

    def root(node: ast.AST) -> str | None:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    for _ in range(8):
        changed = False
        for node in scoped:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
                node.value, (ast.Name, ast.Attribute)
            ):
                continue
            source = _qualified_symbol(node.value, aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and root(node.value) != target.id
                    and source is not None
                    and aliases.get(target.id) != source
                ):
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
        from tests.tool_authority.test_pending_claim import (
            create_primary_with_typed_route,
        )

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
            with pytest.raises((WriteOperationError, TypeError, ValueError)):
                ledger_repository.create_primary(
                    ledger_session,
                    route_handle=object(),
                    operation_id=str(uuid4()),
                    conversation_id=conversation_id,
                    tool_call_id="call-private-ledger",
                    tool_name=prepared,  # type: ignore[arg-type]
                    pending_action_revision=1,
                    pending_confirmation_claim_id="invalid-private-claim",
                    arguments_digest="sha256:" + "0" * 64,
                    proposal_fingerprint="hmac-sha256:" + "a" * 64,
                    confirmation_token_fingerprint="hmac-sha256:" + "b" * 64,
                )
            ledger_operation = create_primary_with_typed_route(
                ledger_repository,
                ledger_session,
                operation_id=str(uuid4()),
                conversation_id=conversation_id,
                tool_call_id="call-public-ledger",
                tool_name="create_application",
                raw_args="{}",
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


def test_task12_fourth_review_confirmed_memory_and_cross_source_probes() -> None:
    possible_ready = {
        "pilot_runtime/new_adapter.py": '''
status = lambda enabled: "ready" if enabled else "disabled"
alias = status
def build(enabled):
    return ContributorResult(name="confirmed_memory", status=alias(enabled), messages=())
'''
    }
    assert _task12_readiness_runtime_violations(possible_ready) == [
        "confirmed-memory:ready:pilot_runtime/new_adapter.py"
    ]

    cross_source = {
        "api.py": '''
from offerpilot.services import Loader
CHAT_ROUTE = "/api/chat/context"
@app.post(CHAT_ROUTE)
def route(session):
    loader = Loader(session)
    return loader.load()
''',
        "services/__init__.py": "from offerpilot.services.loader import Loader\n",
        "services/loader.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
class Loader:
    def __init__(self, session): self.session = session
    def load(self): return self.session.scalars(select(Signal))
''',
    }
    assert "chat-haru:signal-query:api.py" in _task12_readiness_runtime_violations(
        cross_source
    )

    compound_guard = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    """Validate disabled placeholders."""
    for name in ("confirmed_memory", "knowledge_context", "older_conversation_summary"):
        status = contributors[name].status
        if name in {"confirmed_memory", "knowledge_context", "older_conversation_summary"} and status != "disabled":
            raise ValueError(name)
    return contributors
'''
    }
    assert _task12_readiness_runtime_violations(compound_guard) == []

    post_guard_mutation = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors):
    for name in ("confirmed_memory", "knowledge_context", "older_conversation_summary"):
        if contributors[name].status != "disabled":
            raise ValueError(name)
    contributors["confirmed_memory"].status = "ready"
    return contributors
'''
    }
    findings = _task12_readiness_runtime_violations(post_guard_mutation)
    assert "confirmed-memory:ready:context_projector/projector.py" in findings


def test_task12_fifth_review_qualified_status_guard_and_route_constant_probes() -> None:
    qualified_safe = {
        "pilot_runtime/new_adapter.py": '''
class Confirmed:
    @staticmethod
    def status(): return "disabled"
class Other:
    @staticmethod
    def status(): return "ready"
def build():
    return ContributorResult(name="confirmed_memory", status=Confirmed.status(), messages=())
'''
    }
    assert _task12_readiness_runtime_violations(qualified_safe) == []

    contextual_safe = {
        "pilot_runtime/new_adapter.py": '''
def unrelated():
    status = "ready"
    return status
def build():
    status = "disabled"
    return ContributorResult(name="confirmed_memory", status=status, messages=())
'''
    }
    assert _task12_readiness_runtime_violations(contextual_safe) == []

    local_lambda_ready = {
        "pilot_runtime/new_adapter.py": '''
def build(enabled):
    status = lambda: "ready" if enabled else "disabled"
    alias = status
    return ContributorResult(name="confirmed_memory", status=alias(), messages=())
'''
    }
    assert _task12_readiness_runtime_violations(local_lambda_ready) == [
        "confirmed-memory:ready:pilot_runtime/new_adapter.py"
    ]

    wrong_object_guard = {
        "context_projector/projector.py": '''
def _validate_contributors(contributors, other):
    for name in ("confirmed_memory",):
        if name == "confirmed_memory" and other.status != "disabled":
            raise ValueError(name)
    return contributors
'''
    }
    assert _task12_readiness_runtime_violations(wrong_object_guard) == [
        "confirmed-memory:missing-disabled-guard:context_projector/projector.py"
    ]

    imported_route = {
        "api.py": '''
from offerpilot.routes import CHAT_ROUTE as CONTEXT_ROUTE
from offerpilot.services import load_context
@app.post(CONTEXT_ROUTE)
def context(session): return load_context(session)
''',
        "routes/__init__.py": "from offerpilot.routes.paths import CHAT_ROUTE\n",
        "routes/paths.py": "CHAT_ROUTE = '/api/chat/context'\n",
        "services/__init__.py": "from offerpilot.services.context import load_context\n",
        "services/context.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load_context(session): return session.scalars(select(Signal))
''',
    }
    assert "chat-haru:signal-query:api.py" in _task12_readiness_runtime_violations(
        imported_route
    )


def test_task12_sixth_review_latest_assignment_and_direct_chat_route_probes() -> None:
    latest_safe = {
        "pilot_runtime/new_adapter.py": '''
def build():
    status = "ready"
    status = "disabled"
    return ContributorResult(name="confirmed_memory", status=status, messages=())
'''
    }
    assert _task12_readiness_runtime_violations(latest_safe) == []

    direct_route = {
        "api.py": '''
CHAT_ROUTE = "/api/chat/context"
from offerpilot.services import load_context
@app.post(CHAT_ROUTE)
def context(session): return load_context(session)
''',
        "services/__init__.py": "from offerpilot.services.context import load_context\n",
        "services/context.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load_context(session): return session.scalars(select(Signal))
''',
    }
    assert _task12_readiness_runtime_violations(direct_route) == [
        "chat-haru:signal-query:api.py"
    ]


def test_task12_seventh_review_confirmed_branch_join_and_direct_route_query_probes() -> None:
    branch_ready = {
        "pilot/confirmed.py": '''
def build(enabled):
    status = "disabled"
    if enabled:
        status = "ready"
    else:
        status = "disabled"
    return ContributorResult(name="confirmed_memory", status=status, items=[])
'''
    }
    assert any(
        "confirmed-memory:ready" in finding
        for finding in _task12_readiness_runtime_violations(branch_ready)
    )

    branch_disabled = {
        "pilot/confirmed.py": '''
def build(enabled):
    status = "disabled"
    if enabled:
        status = "disabled"
    else:
        status = "disabled"
    return ContributorResult(name="confirmed_memory", status=status, items=[])
'''
    }
    assert not any(
        "confirmed-memory:ready" in finding
        for finding in _task12_readiness_runtime_violations(branch_disabled)
    )

    direct_chat_route = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
CHAT_ROUTE = "/api/chat/context"
@app.post(CHAT_ROUTE)
def neutral(session):
    return session.scalars(select(Signal))
'''
    }
    assert _task12_chat_haru_cross_source_query(direct_chat_route)

    non_chat_route = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
PUBLIC_ROUTE = "/api/public/context"
@app.post(PUBLIC_ROUTE)
def neutral(session):
    return session.scalars(select(Signal))
'''
    }
    assert not _task12_chat_haru_cross_source_query(non_chat_route)


def test_task12_eighth_review_keyword_route_and_factory_loader_probes() -> None:
    keyword_route = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
CHAT_ROUTE = "/api/chat/context"
@app.post(path=CHAT_ROUTE)
def route(session): return session.scalars(select(Signal))
'''
    }
    assert _task12_chat_haru_cross_source_query(keyword_route)
    factory_loader = {
        "api.py": '''
from offerpilot.services import make_loader
@app.post(path="/api/haru/context")
def route(session): return make_loader(session).load()
''',
        "services.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
class Loader:
    def __init__(self, session): self.session = session
    def load(self): return self.session.scalars(select(Signal))
def make_loader(session): return Loader(session)
''',
    }
    assert _task12_chat_haru_cross_source_query(factory_loader)
    dead_query = {
        "api.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
@app.post(path="/api/chat/context")
def route(session):
    if False: return session.scalars(select(Signal))
    return []
'''
    }
    assert not _task12_chat_haru_cross_source_query(dead_query)


def test_task12_eighth_review_confirmed_object_alias_upgrade_probe() -> None:
    upgraded = {
        "pilot_runtime/contributor.py": '''
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    alias = result
    alias.status = "ready"
    return result
'''
    }
    assert "confirmed-memory:ready:pilot_runtime/contributor.py" in (
        _task12_readiness_runtime_violations(upgraded)
    )
    safe = {
        "pilot_runtime/contributor.py": '''
def build():
    result = ContributorResult(name="other", status="disabled", items=[])
    alias = result
    alias.status = "ready"
    return result
'''
    }
    assert _task12_readiness_runtime_violations(safe) == []


def test_task12_ninth_review_route_factory_and_confirmed_helper_probes() -> None:
    routes = {
        "api.py": '''
from offerpilot.routes import router
app.include_router(router)
''',
        "routes.py": '''
from offerpilot.services import make
router.add_api_route("/api/chat/context", lambda s: make(s).load(), methods=["POST"])
''',
        "services.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
class Loader:
    async def load(self): return self.session.scalars(select(Signal))
def make(session):
    value = Loader()
    value.session = session
    return value
''',
    }
    assert _task12_chat_haru_cross_source_query(routes)
    decorated_routes = {
        "api.py": "from offerpilot.routes import router\napp.include_router(router)\n",
        "routes.py": '''
from offerpilot.services import load
@router.api_route(path="/api/chat/context", methods=["POST"])
def chat(session): return load(session)
''',
        "services.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
''',
    }
    assert _task12_chat_haru_cross_source_query(decorated_routes)
    confirmed = {"pilot_runtime/x.py": '''
def upgrade(value):
    alias = value
    alias.status = "ready"
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    upgrade(value=result)
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" in _task12_readiness_runtime_violations(confirmed)
    safe_confirmed = {"pilot_runtime/x.py": '''
def upgrade(value): value.status = "ready"
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    other = ContributorResult(name="other", status="disabled", items=[])
    upgrade(other)
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" not in (
        _task12_readiness_runtime_violations(safe_confirmed)
    )
    safe_route = {
        "api.py": "from offerpilot.routes import router\napp.include_router(router)\n",
        "routes.py": 'router.add_api_route("/api/public", lambda: None, methods=["GET"])\n',
    }
    assert not _task12_chat_haru_cross_source_query(safe_route)


def test_task12_tenth_review_confirmed_chat_callback_and_router_prefix_probes() -> None:
    confirmed_cases = (
        '''def mutate(value): setattr(value, "status", "ready")
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    mutate(result)
    return result
''',
        '''def mutate(value, status): value.status = status
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    mutate(result, "ready")
    return result
''',
        '''from dataclasses import replace
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    return replace(result, status="ready")
''',
    )
    for source in confirmed_cases:
        assert "confirmed-memory:ready:pilot_runtime/x.py" in (
            _task12_readiness_runtime_violations({"pilot_runtime/x.py": source})
        )
    safe_confirmed = {"pilot_runtime/x.py": '''
from dataclasses import replace
def mutate(value, status): value.status = status
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    other = ContributorResult(name="other", status="disabled", items=[])
    mutate(other, "ready")
    return replace(result, status="disabled")
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" not in (
        _task12_readiness_runtime_violations(safe_confirmed)
    )

    callback = {
        "api.py": '''
from offerpilot.routes import router
app.include_router(router, prefix="/api")
''',
        "routes.py": '''
from offerpilot.services import load_context, run
router = APIRouter(prefix="/chat")
@router.post("/context")
def context(session): return run(load_context, session)
''',
        "services.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load_context(session): return session.scalars(select(Signal))
def run(callback, session): return callback(session)
''',
    }
    assert _task12_chat_haru_cross_source_query(callback)
    safe_callback = {
        **callback,
        "services.py": '''
def load_context(session): return session.get(PublicRow, 1)
def run(callback, session): return callback(session)
''',
    }
    assert not _task12_chat_haru_cross_source_query(safe_callback)


def test_task12_eleventh_review_direct_confirmed_setattr_probe() -> None:
    direct_ready = {"pilot_runtime/x.py": '''
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    setattr(result, "status", "ready")
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" in (
        _task12_readiness_runtime_violations(direct_ready)
    )
    safe_direct = {"pilot_runtime/x.py": '''
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    other = ContributorResult(name="other", status="disabled", items=[])
    setattr(other, "status", "ready")
    setattr(result, "status", "disabled")
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" not in (
        _task12_readiness_runtime_violations(safe_direct)
    )


def test_task12_twelfth_review_confirmed_low_level_mutation_probes() -> None:
    cases = (
        '''def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    object.__setattr__(result, "status", "ready")
    return result
''',
        '''ATTRIBUTE = "sta" + "tus"
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    setattr(result, ATTRIBUTE, "ready")
    return result
''',
        '''def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    result.__dict__.update({"status": "ready"})
    return result
''',
        '''ATTRIBUTE = "status"
def mutate(value, attribute):
    object.__setattr__(value, attribute, "ready")
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    mutate(result, ATTRIBUTE)
    return result
''',
        '''def mutate(value):
    value.__dict__.update({"status": "ready"})
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    mutate(result)
    return result
''',
    )
    for source in cases:
        assert "confirmed-memory:ready:pilot_runtime/x.py" in (
            _task12_readiness_runtime_violations({"pilot_runtime/x.py": source})
        )
    safe = {"pilot_runtime/x.py": '''
ATTRIBUTE = "state"
def mutate(value):
    object.__setattr__(value, "status", "ready")
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    other = ContributorResult(name="other", status="disabled", items=[])
    object.__setattr__(result, "status", "disabled")
    setattr(result, ATTRIBUTE, "ready")
    result.__dict__.update({"status": "disabled"})
    mutate(other)
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" not in (
        _task12_readiness_runtime_violations(safe)
    )


def test_task12_twelfth_review_chat_returned_callable_dispatch_probes() -> None:
    route_bodies = (
        "return factory()(session)",
        "fn = factory()\n    return fn(session)",
        'return choices()["load"](session)',
    )
    for body in route_bodies:
        sources = {
            "api.py": f'''
from offerpilot.shared import choices, factory
@app.post("/api/chat/context")
def chat_context(session):
    {body}
''',
            "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def factory(): return load
def choices(): return {"load": load}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources)
        safe = {
            **sources,
            "shared.py": '''
def load(session): return session.get(PublicRow, 1)
def factory(): return load
def choices(): return {"load": load}
''',
        }
        assert not _task12_chat_haru_cross_source_query(safe)


def test_task12_thirteenth_review_chat_conditional_callable_union_probes() -> None:
    route_bodies = (
        "return factory(enabled)(session)",
        'return choices(enabled).get("load")(session)',
    )
    for body in route_bodies:
        sources = {
            "api.py": f'''
from offerpilot.shared import choices, factory
@app.post("/api/chat/context")
def chat_context(session, enabled):
    {body}
''',
            "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def factory(enabled):
    if enabled: return load
    return public
def choices(enabled): return {"load": load if enabled else public}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources)
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def load(session): return session.scalars(select(Signal))",
                "def load(session): return session.get(PublicRow, 1)",
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe)


def test_task12_thirteenth_review_confirmed_getattribute_dict_store_probes() -> None:
    cases = (
        '''def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    object.__getattribute__(result, "__dict__")["status"] = "ready"
    return result
''',
        '''def mutate(value, status):
    object.__getattribute__(value, "__dict__")["status"] = status
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    mutate(result, "ready")
    return result
''',
    )
    for source in cases:
        assert "confirmed-memory:ready:pilot_runtime/x.py" in (
            _task12_readiness_runtime_violations({"pilot_runtime/x.py": source})
        )
    safe = {"pilot_runtime/x.py": '''
def mutate(value, status):
    object.__getattribute__(value, "__dict__")["status"] = status
def build():
    result = ContributorResult(name="confirmed_memory", status="disabled", items=[])
    other = ContributorResult(name="other", status="disabled", items=[])
    object.__getattribute__(result, "__dict__")["status"] = "disabled"
    mutate(other, "ready")
    return result
'''}
    assert "confirmed-memory:ready:pilot_runtime/x.py" not in (
        _task12_readiness_runtime_violations(safe)
    )


def test_task12_fourteenth_review_chat_return_expression_callable_union_probes() -> None:
    return_expressions = (
        "return {'load': load}.get('load')",
        "return load if enabled else public",
    )
    for returned in return_expressions:
        sources = {
            "api.py": '''
from offerpilot.shared import factory
@app.post("/api/chat/context")
def chat_context(session, enabled):
    return factory(enabled)(session)
''',
            "shared.py": f'''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def factory(enabled): {returned}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources)
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def load(session): return session.scalars(select(Signal))",
                "def load(session): return session.get(PublicRow, 1)",
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe)


def test_task12_fifteenth_review_chat_known_key_mapping_callable_probes() -> None:
    factories = (
        (
            '''KEY = "load"
def factory(enabled):
    choices = {"load": load}
    return choices.pop(KEY, public)
''',
            '''KEY = "missing"
def factory(enabled):
    choices = {"load": load}
    return choices.pop(KEY, public)
''',
        ),
        (
            '''KEY = "load"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, load)
''',
            '''KEY = "load"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, public)
''',
        ),
        (
            '''KEY = "load"
def factory(enabled): return {"load": load, "public": public}[KEY]
''',
            '''KEY = "public"
def factory(enabled): return {"load": load, "public": public}[KEY]
''',
        ),
    )
    for factory_source, safe_factory_source in factories:
        sources = {
            "api.py": '''
from offerpilot.shared import factory
@app.post("/api/chat/context")
def chat_context(session, enabled):
    return factory(enabled)(session)
''',
            "shared.py": f'''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
{factory_source}''',
        }
        assert _task12_chat_haru_cross_source_query(sources)
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                factory_source,
                safe_factory_source,
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe)


def test_task12_sixteenth_review_chat_caller_mapping_dispatch_probes() -> None:
    route_expressions = (
        "handlers().pop('load', public)(session)",
        "handlers().setdefault('load', public)(session)",
        "handlers()[KEY](session)",
    )
    for expression in route_expressions:
        sources = {
            "api.py": f'''
from offerpilot.shared import handlers, public
KEY = "load"
@app.post("/api/chat/context")
def chat_context(session):
    return {expression}
''',
            "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {"load": load, "public": public}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources)
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"load": load',
                '"load": public',
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe)

    missing_default = {
        "api.py": '''
from offerpilot.shared import handlers, public
@app.post("/api/chat/context")
def chat_context(session):
    return handlers().pop("missing", public)(session)
''',
        "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {"load": load}
''',
    }
    assert not _task12_chat_haru_cross_source_query(missing_default)


def test_task12_seventeenth_review_chat_mapping_value_iteration_probes() -> None:
    route_expressions = (
        "next(iter(handlers().values()))(session)",
        "list(handlers().values())[0](session)",
        "next(iter(handlers().items()))[1](session)",
        "[item(session) for item in handlers().values()]",
        "handlers().popitem()[1](session)",
    )
    for expression in route_expressions:
        sources = {
            "api.py": f'''
from offerpilot.shared import handlers
@app.post("/api/chat/context")
def chat_context(session):
    return {expression}
''',
            "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {"load": load, "public": public}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources), expression
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"load": load',
                '"load": public',
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe), expression


def test_task12_eighteenth_review_chat_mapping_parameter_propagation_probes() -> None:
    cases = (
        (
            "pick(handlers())(session)",
            "def pick(mapping): return next(iter(mapping.values()))",
        ),
        (
            "pick(handlers())(session)",
            'def pick(mapping): return mapping.get("load")',
        ),
        (
            "pick(handlers)(session)",
            "def pick(factory): return next(iter(factory().values()))",
        ),
    )
    for expression, picker in cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import handlers, pick
@app.post("/api/chat/context")
def chat_context(session):
    return {expression}
''',
            "shared.py": f'''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {{"load": load, "public": public}}
{picker}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"load": load',
                '"load": public',
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe), picker


def test_task12_nineteenth_review_chat_constructor_and_closure_mapping_probes() -> None:
    cases = (
        (
            "Picker(handlers()).pick()(session)",
            '''class Picker:
    def __init__(self, mapping): self.mapping = mapping
    def pick(self): return self.mapping.get("load")''',
        ),
        (
            "factory(handlers())(session)",
            '''def factory(mapping):
    def pick(session): return mapping.get("load")(session)
    return pick''',
        ),
    )
    for expression, picker in cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import Picker, factory, handlers
@app.post("/api/chat/context")
def chat_context(session):
    return {expression}
''',
            "shared.py": f'''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {{"load": load, "public": public}}
{picker}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"load": load',
                '"load": public',
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe), picker


def test_task12_twentieth_review_chat_post_init_and_closure_alias_probes() -> None:
    cases = (
        (
            '''picker = Picker()
    picker.mapping = handlers()
    return picker.pick()(session)''',
            '''class Picker:
    def pick(self): return self.mapping.get("load")''',
        ),
        (
            "return factory(handlers())(session)",
            '''def factory(source):
    mapping = source
    def pick(session): return mapping.get("load")(session)
    return pick''',
        ),
    )
    for route_body, implementation in cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import Picker, factory, handlers
@app.post("/api/chat/context")
def chat_context(session):
    {route_body}
''',
            "shared.py": f'''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {{"load": load}}
{implementation}
''',
        }
        assert _task12_chat_haru_cross_source_query(sources), implementation
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"load": load',
                '"load": public',
            ),
        }
        assert not _task12_chat_haru_cross_source_query(safe), implementation

    safe_rebind = {
        "api.py": '''
from offerpilot.shared import Picker, factory, handlers, public_handlers
@app.post("/api/chat/context")
def chat_context(session):
    picker = Picker()
    picker.mapping = handlers()
    picker.mapping = public_handlers()
    picker.pick()(session)
    return factory(handlers())(session)
''',
        "shared.py": '''
from offerpilot.models import InterviewReadinessSignal as Signal
def load(session): return session.scalars(select(Signal))
def public(session): return session.get(PublicRow, 1)
def handlers(): return {"load": load}
def public_handlers(): return {"load": public}
class Picker:
    def pick(self): return self.mapping.get("load")
def factory(source):
    mapping = source
    mapping = public_handlers()
    def pick(session): return mapping.get("load")(session)
    return pick
''',
    }
    assert not _task12_chat_haru_cross_source_query(safe_rebind)
