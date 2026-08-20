from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest


def _expect_rejected(source: str, validator: Callable[[ast.AST], None]) -> None:
    with pytest.raises(AssertionError):
        validator(ast.parse(source))


ROOT = Path(__file__).resolve().parents[1]
JOURNAL_PATH = ROOT / "src/offerpilot/agent_runtime/journal.py"
REPOSITORY_PATH = ROOT / "src/offerpilot/repositories/agent_runs.py"

JOURNAL_REPOSITORY_API = frozenset(
    {
        "create_run_and_initial_segment",
        "attach_input_message",
        "start_segment",
        "append_event",
        "capture_context",
        "converge_disposition",
        "mark_degraded",
        "find_waiting_run",
    }
)
BOUND_FORBIDDEN_NAMES = frozenset(
    {
        "_configure_deadline",
        "_progress_handler",
        "progress",
        "progress_handler",
        "set_progress_handler",
        "session_factory",
        "_session_guard",
        "_journal_transaction",
        "journal_session_factory",
        "JournalSessionFactory",
        "JournalSession",
        "journal_session",
        "commit",
        "rollback",
        "close",
        "invalidate",
    }
)
STALE_JOURNAL_NAMES = frozenset(
    {"_segment_deadline", "_segment_started_at", "_disposition_attempted"}
)

# These are the non-Journal product surfaces that must not know about the
# Journal's active-work budget implementation.  Keep this list explicit so a
# new runtime module cannot silently become part of the contract by filename.
ARCHITECTURE_BOUNDARY_FILES = (
    ROOT / "src/offerpilot/ai/agent.py",  # Graph + pending action state
    ROOT / "src/offerpilot/ai/deterministic_actions.py",  # Pending action parser
    ROOT / "src/offerpilot/ai/write_operations.py",  # write-operation ledger
    ROOT / "src/offerpilot/ai/tool_runtime/contracts.py",  # tool/API contracts
    ROOT / "src/offerpilot/ai/tool_runtime/rendering.py",  # API serialization
    ROOT / "src/offerpilot/ai/tool_runtime/transport.py",  # transport serialization
    ROOT / "src/offerpilot/repositories/chat.py",  # chat + pending state
    ROOT / "src/offerpilot/models.py",
    ROOT / "src/offerpilot/schemas.py",
    ROOT / "src/offerpilot/api.py",
    ROOT / "src/offerpilot/repositories/json_contract.py",  # API serialization
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _node_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}

    class ParentVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[ast.AST] = []

        def generic_visit(self, node: ast.AST) -> None:
            if self.stack:
                result[node] = self.stack[-1]
            self.stack.append(node)
            super().generic_visit(node)
            self.stack.pop()

    ParentVisitor().visit(tree)
    return result


def _is_direct_constructor_method(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            class_node = parents.get(current)
            return (
                current.name == "__init__"
                and isinstance(class_node, ast.ClassDef)
                and current in class_node.body
            )
        current = parents.get(current)
    return False


def _is_constructor_attribute_write(
    node: ast.AST, attribute: str, parents: dict[ast.AST, ast.AST]
) -> bool:
    if not _is_self_attribute(node, attribute) or not isinstance(node.ctx, ast.Store):
        return False
    assignment = parents.get(node)
    if isinstance(assignment, ast.Assign):
        exact_target = len(assignment.targets) == 1 and assignment.targets[0] is node
    elif isinstance(assignment, ast.AnnAssign):
        exact_target = assignment.target is node and assignment.value is not None
    else:
        exact_target = False
    return exact_target and _is_direct_constructor_method(assignment, parents)


def _class_body_bound_names(class_node: ast.ClassDef) -> set[str]:
    names: set[str] = set()

    class BoundNameVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if isinstance(node.name, str):
                names.add(node.name)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                names.add(alias.asname or alias.name)
                if alias.name == "*":
                    names.add("*")

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if isinstance(node.name, str):
                names.add(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if isinstance(node.name, str):
                names.add(node.name)
            self.generic_visit(node)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if isinstance(node.rest, str):
                names.add(node.rest)
            self.generic_visit(node)

    visitor = BoundNameVisitor()
    for statement in class_node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visitor.visit(statement)
    return names


def _is_module_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return False
        current = parents.get(current)
    return True


def _target_names(node: ast.AST) -> set[str]:
    names = {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del))
    }
    for child in ast.walk(node):
        if isinstance(child, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            target = child.name
            if isinstance(target, str):
                names.add(target)
        elif isinstance(child, ast.MatchMapping) and isinstance(child.rest, str):
            names.add(child.rest)
    return names


def _top_level_class(tree: ast.AST, name: str) -> ast.ClassDef:
    assert isinstance(tree, ast.Module)
    parents = _parents(tree)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one {name} class"
    result = matches[0]
    assert result in tree.body, f"{name} must not be nested or shadowed"
    assert not result.decorator_list, f"{name} must not be decorated"
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    ), f"{name} must not be shadowed by a function"
    for node in ast.walk(tree):
        if not _is_module_scope(node, parents):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            raise AssertionError(f"{name} must not be shadowed by a function")
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                assert bound_name != name, f"{name} must not be shadowed by an import"
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "*", f"{name} must not be shadowed by a star import"
                bound_name = alias.asname or alias.name
                assert bound_name != name, f"{name} must not be shadowed by an import"
        elif isinstance(
            node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)
        ):
            assert name not in _target_names(node), f"{name} must not be shadowed by a target"
        elif isinstance(node, ast.Delete):
            assert name not in _target_names(node), f"{name} must not be deleted"
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                assert name not in _target_names(node.optional_vars), (
                    f"{name} must not be shadowed by a with target"
                )
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            raise AssertionError(f"{name} must not be shadowed by an except target")
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            raise AssertionError(f"{name} must not be shadowed by a match capture")
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            raise AssertionError(f"{name} must not be shadowed by a match mapping rest capture")
    return result


def _class_method(
    class_node: ast.ClassDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    direct = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    all_matches = [
        node
        for node in ast.walk(class_node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(direct) == 1 and len(all_matches) == 1, (
        f"expected exactly one direct {class_node.name}.{name} method"
    )
    assert not direct[0].decorator_list, (
        f"validated {class_node.name}.{name} method must not be decorated"
    )
    class_body_names = _class_body_bound_names(class_node)
    assert name not in class_body_names and "*" not in class_body_names, (
        f"validated {class_node.name}.{name} method must not be rebound in its class"
    )
    return direct[0]


def _is_self_attribute(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _direct_repository_method(node: ast.AST) -> ast.Attribute | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if _is_self_attribute(node.func.value, "repository"):
        return node.func
    return None


def _direct_repository_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if _direct_repository_method(node) is not None
    ]


def _contains_none_literal(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant) and child.value is None
        for child in ast.walk(node)
    )


def _is_exact_lease_attribute(node: ast.AST, attribute: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == "lease"
    )


def _validate_external_method_rebindings(tree: ast.AST) -> None:
    parents = _parents(tree)
    protected_methods = {
        "AgentRunRepository": JOURNAL_REPOSITORY_API | {"append_event_bound"},
        "SafeRunRecorder": frozenset(
            {
                "start_segment",
                "attach_input_message",
                "capture_context",
                "append_event",
                "append_prepared_event_bound",
                "resume",
                "suspend",
                "finish",
                "abandon",
                "mark_degraded",
            }
        ),
    }

    def protected_attribute(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.attr in protected_methods.get(node.value.id, ())
        )

    for node in ast.walk(tree):
        if not _is_module_scope(node, parents):
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            assert not protected_attribute(node), (
                "validated Journal class methods must not be rebound externally"
            )
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"setattr", "delattr"} or len(node.args) < 2:
            continue
        class_name = (
            node.args[0].id if isinstance(node.args[0], ast.Name) else None
        )
        method_name = (
            node.args[1].value
            if isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            else None
        )
        if class_name in protected_methods and method_name in protected_methods[class_name]:
            raise AssertionError(
                "validated Journal class methods must not be changed with setattr/delattr"
            )


def _validate_journal_repository_calls(tree: ast.AST) -> None:
    _validate_external_method_rebindings(tree)
    parents = _parents(tree)
    calls = _direct_repository_calls(tree)
    allowed = JOURNAL_REPOSITORY_API | {"append_event_bound"}

    for node in ast.walk(tree):
        if not _is_self_attribute(node, "repository"):
            continue
        if isinstance(node.ctx, ast.Store):
            assert _is_constructor_attribute_write(node, "repository", parents), (
                "self.repository may only be initialized in a constructor"
            )
            continue
        method_attr = parents.get(node)
        if (
            isinstance(method_attr, ast.Call)
            and isinstance(method_attr.func, ast.Name)
            and method_attr.func.id == "SafeRunRecorder"
            and (
                node in method_attr.args
                or any(keyword.value is node for keyword in method_attr.keywords)
            )
        ):
            continue
        assert isinstance(method_attr, ast.Attribute) and method_attr.value is node, (
            "self.repository may only be used as the receiver of a direct method call"
        )
        call = parents.get(method_attr)
        assert isinstance(call, ast.Call) and call.func is method_attr, (
            "repository aliases, walrus assignments, and chained access are forbidden"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "getattr" and node.args and _is_self_name(node.args[0]):
                raise AssertionError("dynamic self.repository access is forbidden")
            if node.func.id == "vars" and node.args and _is_self_name(node.args[0]):
                raise AssertionError("dynamic self mapping access is forbidden")
            if node.func.id == "setattr" and node.args and _is_self_name(node.args[0]):
                raise AssertionError("dynamic self.repository writes are forbidden")
        if isinstance(node, ast.Attribute) and node.attr == "__getattribute__":
            raise AssertionError("dynamic self.repository access is forbidden")
        if isinstance(node, ast.Attribute) and node.attr == "__setattr__":
            raise AssertionError("dynamic self.repository writes are forbidden")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and node.args
            and _is_self_name(node.args[0])
        ):
            raise AssertionError("dynamic self.repository access is forbidden")
        if isinstance(node, ast.Attribute) and _is_self_attribute(node, "__dict__"):
            raise AssertionError("dynamic self.__dict__ repository access is forbidden")

    for call in calls:
        function = _direct_repository_method(call)
        assert function is not None
        assert function.attr in allowed, f"unknown Journal repository method: {function.attr}"
        keyword_names = [keyword.arg for keyword in call.keywords]
        assert all(keyword_name is not None for keyword_name in keyword_names), (
            "Journal repository calls must not forward dynamic keyword arguments"
        )
        assert not any(isinstance(argument, ast.Starred) for argument in call.args), (
            "Journal repository calls must not forward dynamic positional arguments"
        )
        assert "clock" not in keyword_names, "Journal repository calls must not pass clock"
        if function.attr == "append_event_bound":
            assert "deadline" not in keyword_names
            assert "safe_clock" not in keyword_names
            continue

        for required in ("deadline", "safe_clock"):
            values = [
                keyword.value
                for keyword in call.keywords
                if keyword.arg == required
            ]
            assert len(values) == 1, f"{function.attr} must pass exactly one {required}"
            assert not _contains_none_literal(values[0]), (
                f"{function.attr} must pass a non-None {required}"
            )
            expected_attribute = (
                "work_deadline" if required == "deadline" else "safe_clock"
            )
            assert _is_exact_lease_attribute(values[0], expected_attribute), (
                f"{function.attr} must pass lease.{expected_attribute} exactly"
            )


def _validate_bound_call_ownership(tree: ast.Module) -> None:
    recorder = _top_level_class(tree, "SafeRunRecorder")
    method = _class_method(recorder, "append_prepared_event_bound")
    append_calls = [
        node
        for node in _direct_repository_calls(tree)
        if _direct_repository_method(node).attr == "append_event_bound"  # type: ignore[union-attr]
    ]
    all_append_calls = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append_event_bound"
        )
    ]
    assert len(all_append_calls) == len(append_calls), (
        "append_event_bound must be called through self.repository"
    )
    assert len(append_calls) == 1, "append_event_bound must have one Journal call site"
    assert id(append_calls[0]) in {id(node) for node in ast.walk(method)}, (
        "append_event_bound must be owned by SafeRunRecorder.append_prepared_event_bound"
    )


def _is_self_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _validate_self_clock_access(tree: ast.AST) -> None:
    parents = _parents(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in {"clock", "_clock"}
        ):
            attribute = node.attr
            parent = parents.get(node)
            if isinstance(node.ctx, ast.Store):
                assert _is_constructor_attribute_write(node, attribute, parents), (
                    f"self.{attribute} may only be initialized in a constructor"
                )
                continue
            assert (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "ActiveWorkBudget"
                and (
                    node in parent.args
                    or any(keyword.value is node for keyword in parent.keywords)
                )
            ), "self.clock is only allowed as a direct ActiveWorkBudget argument"

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if node.args and _is_self_name(node.args[0]):
                    raise AssertionError("dynamic self clock access is forbidden")
            if isinstance(node.func, ast.Name) and node.func.id == "vars":
                if node.args and _is_self_name(node.args[0]):
                    raise AssertionError("dynamic self mapping access is forbidden")
            if isinstance(node.func, ast.Name) and node.func.id == "setattr":
                if node.args and _is_self_name(node.args[0]):
                    raise AssertionError("dynamic self clock writes are forbidden")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "__getattribute__":
                raise AssertionError("dynamic self clock access is forbidden")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "__setattr__":
                raise AssertionError("dynamic self clock writes are forbidden")

        if isinstance(node, ast.Attribute):
            if _is_self_attribute(node, "__dict__"):
                raise AssertionError("dynamic self.__dict__ access is forbidden")
            if _is_self_attribute(node, "__getattribute__"):
                raise AssertionError("dynamic self clock access is forbidden")


def _validate_bound_method(method: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    parents = _parents(method)
    assert not any(isinstance(node, ast.AsyncWith) for node in ast.walk(method))
    assert sum(isinstance(node, ast.With) for node in ast.walk(method)) == 1, (
        "bound path must have exactly one context manager"
    )
    with_contexts: list[tuple[ast.With, ast.withitem]] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            context = item.context_expr
            if (
                isinstance(context, ast.Call)
                and isinstance(context.func, ast.Attribute)
                and context.func.attr == "begin_nested"
                and isinstance(context.func.value, ast.Name)
                and context.func.value.id == "session"
                and not context.args
                and not context.keywords
                and item.optional_vars is None
            ):
                with_contexts.append((node, item))
    assert len(with_contexts) == 1, "bound path must have exactly one with session.begin_nested()"
    bound_with, item = with_contexts[0]
    assert len(bound_with.items) == 1 and bound_with.items[0] is item

    _validate_journal_repository_calls(method)
    repository_calls = _direct_repository_calls(method)
    assert len(repository_calls) == 1, "bound path must have exactly one repository call"
    append_calls = [
        call
        for call in repository_calls
        if _direct_repository_method(call).attr == "append_event_bound"  # type: ignore[union-attr]
    ]
    assert len(append_calls) == 1, "bound path must have exactly one append_event_bound call"
    append_call = append_calls[0]
    assert len(append_call.args) == 3, (
        "append_event_bound must receive exactly three positional arguments"
    )
    assert isinstance(append_call.args[0], ast.Name)
    assert append_call.args[0].id == "session", (
        "append_event_bound must receive the exact session name first"
    )
    assert _is_self_attribute(append_call.args[1], "run_id"), (
        "append_event_bound must receive self.run_id second"
    )
    assert isinstance(append_call.args[2], ast.Name)
    assert append_call.args[2].id == "draft", (
        "append_event_bound must receive draft third"
    )
    assert not append_call.keywords, "append_event_bound must not receive keywords"
    append_statement = parents.get(append_call)
    assert isinstance(append_statement, ast.Expr)
    assert parents.get(append_statement) is bound_with
    assert append_statement in bound_with.body, (
        "append_event_bound must be a direct begin_nested statement"
    )

    for node in ast.walk(method):
        if not isinstance(node, ast.Name) or node.id != "session":
            continue
        parent = parents.get(node)
        if isinstance(node.ctx, ast.Store):
            raise AssertionError("session aliases and reassignment are forbidden")
        begin_attr = (
            parent
            if isinstance(parent, ast.Attribute) and parent.value is node
            else None
        )
        begin_call = parents.get(begin_attr) if begin_attr is not None else None
        begin_parent = parents.get(begin_call) if begin_call is not None else None
        allowed_begin = (
            begin_attr is not None
            and begin_attr.attr == "begin_nested"
            and isinstance(begin_call, ast.Call)
            and begin_call.func is begin_attr
            and isinstance(begin_parent, ast.withitem)
            and begin_parent.context_expr is begin_call
        )
        allowed_append = (
            isinstance(parent, ast.Call)
            and node in parent.args
            and parent.args
            and parent.args[0] is node
            and isinstance(node, ast.Name)
            and _direct_repository_method(parent) is not None
            and _direct_repository_method(parent).attr == "append_event_bound"  # type: ignore[union-attr]
        )
        assert allowed_begin or allowed_append, "only exact bound session uses are allowed"

    forbidden = _node_names(method) & BOUND_FORBIDDEN_NAMES
    assert not forbidden, f"bound path owns no Journal transaction machinery: {sorted(forbidden)}"
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in BOUND_FORBIDDEN_NAMES
            assert "pragma" not in node.value.lower(), "bound path must not issue PRAGMA"


def _all_argument_names(method: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        argument.arg
        for argument in (
            *method.args.posonlyargs,
            *method.args.args,
            *method.args.kwonlyargs,
        )
    }


def _validate_bound_signature(method: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    names = _all_argument_names(method)
    assert not names & {"deadline", "safe_clock", "clock"}
    assert method.args.vararg is None and method.args.kwarg is None


def _validate_owned_signature(method: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    names = _all_argument_names(method)
    assert {"deadline", "safe_clock"} <= {
        argument.arg for argument in method.args.kwonlyargs
    }
    assert "clock" not in names
    assert method.args.vararg is None and method.args.kwarg is None


def _validate_bound_repository_session(method: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    parents = _parents(method)
    required_helpers = {"_existing_event", "_required_run", "_insert_event"}
    helper_calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_self_name(node.func.value)
            and node.func.attr in required_helpers
        ):
            assert node.args and isinstance(node.args[0], ast.Name)
            assert node.args[0].id == "session", (
                f"{node.func.attr} must receive the caller session first"
            )
            helper_calls.setdefault(node.func.attr, []).append(node)
        if not isinstance(node, ast.Name) or node.id != "session":
            continue
        parent = parents.get(node)
        allowed_helper_argument = (
            isinstance(parent, ast.Call)
            and parent.args
            and parent.args[0] is node
            and isinstance(parent.func, ast.Attribute)
            and _is_self_name(parent.func.value)
            and parent.func.attr in required_helpers
        )
        assert allowed_helper_argument, (
            "append_event_bound must not own or manipulate the caller session"
        )
    assert set(helper_calls) == required_helpers
    assert all(len(calls) == 1 for calls in helper_calls.values()), (
        "append_event_bound must use each required helper exactly once"
    )


def _validate_bound_repository_implementation(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    _validate_bound_signature(method)
    _validate_bound_repository_session(method)
    forbidden = _node_names(method) & BOUND_FORBIDDEN_NAMES
    assert not forbidden, f"repository bound path owns no transaction machinery: {sorted(forbidden)}"
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in BOUND_FORBIDDEN_NAMES
            assert "pragma" not in node.value.lower()


def _validate_repository_module(tree: ast.Module) -> None:
    _validate_external_method_rebindings(tree)
    repository = _top_level_class(tree, "AgentRunRepository")
    _validate_bound_repository_implementation(
        _class_method(repository, "append_event_bound")
    )


PUBLIC_BUDGET_API = frozenset(
    {
        "ActiveWorkBudget",
        "JournalBudgetExhausted",
        "JournalDeadlineExceeded",
        "MonotonicSample",
        "OperationLease",
        "SafeClockAdapter",
        "JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS",
        "JOURNAL_OPERATION_HARD_CAP_SECONDS",
        "JOURNAL_OPERATION_CLEANUP_RESERVE_SECONDS",
        "JOURNAL_DISPOSITION_BUDGET_SECONDS",
        "JOURNAL_SQLITE_PROGRESS_STEPS",
        "JOURNAL_DEFAULT_BUSY_TIMEOUT_MS",
    }
)
PROTECTED_OWNERSHIP_SYMBOLS = PUBLIC_BUDGET_API | frozenset(
    {"AgentRunRepository", "SafeRunRecorder"}
)
BUDGET_MODULE_SUFFIX = "agent_runtime.budget"


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                aliases[bound_name] = alias.name if alias.asname else bound_name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "agent_runtime":
                    continue
                bound_name = alias.asname or alias.name
                if node.level > 0:
                    aliases[bound_name] = "agent_runtime"
                else:
                    aliases[bound_name] = f"{node.module or ''}.agent_runtime".lstrip(".")
    return aliases


def _resolve_dotted_name(value: str, aliases: dict[str, str]) -> str:
    root, _, suffix = value.partition(".")
    target = aliases.get(root, root)
    return f"{target}.{suffix}" if suffix else target


def _is_budget_module_reference(value: str) -> bool:
    normalized = value.lstrip(".")
    return (
        normalized.endswith(BUDGET_MODULE_SUFFIX)
        or normalized == "budget"
        or normalized.endswith(".budget") and "agent_runtime" in normalized
    )


def _validate_boundary_module(tree: ast.AST) -> None:
    names = _node_names(tree) & PUBLIC_BUDGET_API
    assert not names, f"product boundary references Journal budget API: {sorted(names)}"
    module_aliases = _module_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(_is_budget_module_reference(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not (
                _is_budget_module_reference(module)
                or (node.level > 0 and module in {"budget", "agent_runtime.budget"})
                or (
                    module.endswith("agent_runtime")
                    and any(alias.name in {"budget", "*"} for alias in node.names)
                )
            )
        elif isinstance(node, ast.Attribute):
            dotted = _dotted_name(node)
            resolved = _resolve_dotted_name(dotted, module_aliases) if dotted else None
            assert not (
                (dotted and _is_budget_module_reference(dotted))
                or (resolved and _is_budget_module_reference(resolved))
            ), "dynamic agent_runtime.budget access is forbidden"
        elif isinstance(node, ast.Call):
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if function_name in {"__import__", "import_module"}:
                assert not any(
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and _is_budget_module_reference(argument.value)
                    for argument in node.args[:1]
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not _is_budget_module_reference(node.value)
            assert node.value not in PUBLIC_BUDGET_API


def _validate_protected_symbol_scopes(tree: ast.AST) -> None:
    assert isinstance(tree, ast.Module)
    parents = _parents(tree)
    disallowed_scope_types = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
        ast.ClassDef,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.comprehension,
    )

    def in_disallowed_scope(node: ast.AST) -> bool:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, disallowed_scope_types):
                return True
            current = parents.get(current)
        return False

    def assert_module_binding(node: ast.AST, symbol: str, reason: str) -> None:
        if symbol not in PROTECTED_OWNERSHIP_SYMBOLS:
            return
        approved_class = (
            isinstance(node, ast.ClassDef)
            and node.name == "SafeRunRecorder"
            and node in tree.body
        )
        approved_import = isinstance(node, (ast.Import, ast.ImportFrom)) and node in tree.body
        assert approved_class or approved_import, (
            f"protected ownership symbol {symbol} has an unauthorized {reason}"
        )

    def import_bindings(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname or alias.name.split(".", 1)[0], alias.name)
                for alias in node.names
            ]
        return [
            (alias.asname or alias.name, alias.name)
            for alias in node.names
        ]

    def approved_protected_import(
        node: ast.Import | ast.ImportFrom, bound_name: str, imported_name: str
    ) -> bool:
        if not isinstance(node, ast.ImportFrom):
            return False
        imported_symbol = imported_name.rsplit(".", 1)[-1]
        if bound_name != imported_symbol:
            return False
        module = node.module or ""
        if imported_symbol in PUBLIC_BUDGET_API:
            return (
                (node.level == 0 and module == "offerpilot.agent_runtime.budget")
                or (node.level > 0 and module in {"budget", "agent_runtime.budget"})
            )
        if imported_symbol == "AgentRunRepository":
            return (
                (node.level == 0 and module == "offerpilot.repositories.agent_runs")
                or (node.level > 0 and module in {"repositories.agent_runs", "agent_runs"})
            )
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            direct_module_import = node in tree.body
            for bound_name, imported_name in import_bindings(node):
                if imported_name == "*":
                    raise AssertionError("star imports cannot establish ownership symbols")
                imported_symbol = imported_name.rsplit(".", 1)[-1]
                protected_import = (
                    bound_name in PROTECTED_OWNERSHIP_SYMBOLS
                    or imported_symbol in PROTECTED_OWNERSHIP_SYMBOLS
                    or _is_budget_module_reference(imported_name)
                    or (
                        isinstance(node, ast.ImportFrom)
                        and _is_budget_module_reference(node.module or "")
                    )
                )
                if protected_import:
                    assert not in_disallowed_scope(node), (
                        "protected ownership imports are not allowed in local scopes"
                    )
                    assert direct_module_import, (
                        "protected ownership imports are module-level only"
                    )
                    assert approved_protected_import(node, bound_name, imported_name), (
                        "protected ownership imports must use approved module bindings"
                    )
                    assert_module_binding(node, bound_name, "import")

        if isinstance(node, ast.arg) and node.arg in PROTECTED_OWNERSHIP_SYMBOLS:
            raise AssertionError(f"protected ownership parameter is rebound: {node.arg}")

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in PROTECTED_OWNERSHIP_SYMBOLS:
                continue
            assert_module_binding(node, node.name, "definition")

        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id in PROTECTED_OWNERSHIP_SYMBOLS:
                raise AssertionError(f"protected ownership name is rebound: {node.id}")

        if isinstance(node, ast.ExceptHandler) and isinstance(node.name, str):
            if node.name in PROTECTED_OWNERSHIP_SYMBOLS:
                raise AssertionError(f"protected ownership exception target: {node.name}")

        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and isinstance(node.name, str):
            if node.name in PROTECTED_OWNERSHIP_SYMBOLS:
                raise AssertionError(f"protected ownership match target: {node.name}")

        if isinstance(node, ast.MatchMapping) and isinstance(node.rest, str):
            if node.rest in PROTECTED_OWNERSHIP_SYMBOLS:
                raise AssertionError(f"protected ownership mapping target: {node.rest}")


def test_journal_repository_calls_are_budget_bound() -> None:
    tree = _module(JOURNAL_PATH)
    _top_level_class(tree, "SafeRunRecorder")
    _validate_protected_symbol_scopes(tree)
    _validate_journal_repository_calls(tree)
    _validate_self_clock_access(tree)
    observed = [
        call
        for call in _direct_repository_calls(tree)
        if _direct_repository_method(call).attr in JOURNAL_REPOSITORY_API  # type: ignore[union-attr]
    ]
    assert observed, "Journal must contain budget-bound repository calls"


def test_bound_append_event_is_owned_by_safe_recorder() -> None:
    tree = _module(JOURNAL_PATH)
    recorder = _top_level_class(tree, "SafeRunRecorder")
    _validate_bound_call_ownership(tree)
    _validate_bound_method(_class_method(recorder, "append_prepared_event_bound"))


def test_journal_and_repository_have_no_stale_budget_paths() -> None:
    journal_tree = _module(JOURNAL_PATH)
    repository_tree = _module(REPOSITORY_PATH)

    stale = _node_names(journal_tree) & STALE_JOURNAL_NAMES
    assert not stale, f"stale Journal budget paths remain: {sorted(stale)}"

    _validate_journal_repository_calls(journal_tree)
    _validate_self_clock_access(journal_tree)
    assert not any(
        keyword.arg == "clock"
        for call in ast.walk(repository_tree)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
    ), "repository calls must use deadline and safe_clock, never clock"


def test_product_surfaces_do_not_import_journal_budget_types() -> None:
    for path in ARCHITECTURE_BOUNDARY_FILES:
        assert path.is_file(), f"boundary file disappeared: {path}"
        _validate_boundary_module(_module(path))


def test_append_event_bound_has_no_deadline_protocol() -> None:
    tree = _module(REPOSITORY_PATH)
    _validate_repository_module(tree)


def test_repository_gate_rejects_post_class_method_rebinding() -> None:
    source = REPOSITORY_PATH.read_text(encoding="utf-8")
    source += "\nAgentRunRepository.append_event_bound = replacement\n"
    _expect_rejected(source, _validate_repository_module)


def test_journal_owned_repository_signatures_are_explicitly_budget_bound() -> None:
    tree = _module(REPOSITORY_PATH)
    repository = _top_level_class(tree, "AgentRunRepository")
    methods = {
        name: _class_method(repository, name) for name in JOURNAL_REPOSITORY_API
    }
    assert set(methods) == JOURNAL_REPOSITORY_API
    for method in methods.values():
        _validate_owned_signature(method)


def test_mutations_reject_nested_class_shadowing() -> None:
    _expect_rejected(
        """
class SafeRunRecorder:
    def outer(self):
        class SafeRunRecorder:
            pass
""",
        lambda tree: _top_level_class(tree, "SafeRunRecorder"),
    )
    _expect_rejected(
        """
def factory():
    class AgentRunRepository:
        pass
""",
        lambda tree: _top_level_class(tree, "AgentRunRepository"),
    )
    _expect_rejected(
        """
class AgentRunRepository:
    def outer(self):
        def append_event_bound(self, session, run_id, draft):
            pass
""",
        lambda tree: _class_method(
            _top_level_class(tree, "AgentRunRepository"), "append_event_bound"
        ),
    )


@pytest.mark.parametrize(
    ("source", "class_name"),
    (
        ("@decorator\nclass SafeRunRecorder:\n    pass\n", "SafeRunRecorder"),
        ("@decorator\nclass AgentRunRepository:\n    pass\n", "AgentRunRepository"),
    ),
)
def test_mutations_reject_target_class_decorators(source: str, class_name: str) -> None:
    _expect_rejected(source, lambda tree: _top_level_class(tree, class_name))


@pytest.mark.parametrize(
    ("source", "class_name", "method_name"),
    (
        (
            "class SafeRunRecorder:\n"
            "    @decorator\n"
            "    def append_prepared_event_bound(self, session, draft):\n"
            "        pass\n",
            "SafeRunRecorder",
            "append_prepared_event_bound",
        ),
        (
            "class AgentRunRepository:\n"
            "    @decorator\n"
            "    def append_event_bound(self, session, run_id, draft):\n"
            "        pass\n",
            "AgentRunRepository",
            "append_event_bound",
        ),
    ),
)
def test_mutations_reject_validated_method_decorators(
    source: str, class_name: str, method_name: str
) -> None:
    _expect_rejected(
        source,
        lambda tree: _class_method(_top_level_class(tree, class_name), method_name),
    )


@pytest.mark.parametrize(
    ("source", "class_name", "method_name"),
    (
        (
            "class SafeRunRecorder:\n"
            "    def append_prepared_event_bound(self, session, draft):\n"
            "        pass\n"
            "    append_prepared_event_bound = replacement\n",
            "SafeRunRecorder",
            "append_prepared_event_bound",
        ),
        (
            "class AgentRunRepository:\n"
            "    append_event_bound = replacement\n"
            "    def append_event_bound(self, session, run_id, draft):\n"
            "        pass\n",
            "AgentRunRepository",
            "append_event_bound",
        ),
        (
            "class AgentRunRepository:\n"
            "    def append_event_bound(self, session, run_id, draft):\n"
            "        pass\n"
            "    append_event_bound: object\n",
            "AgentRunRepository",
            "append_event_bound",
        ),
        (
            "class SafeRunRecorder:\n"
            "    match value:\n"
            "        case {**append_prepared_event_bound}:\n"
            "            pass\n"
            "    def append_prepared_event_bound(self, session, draft):\n"
            "        pass\n",
            "SafeRunRecorder",
            "append_prepared_event_bound",
        ),
        (
            "class AgentRunRepository:\n"
            "    match value:\n"
            "        case {**append_event_bound}:\n"
            "            pass\n"
            "    def append_event_bound(self, session, run_id, draft):\n"
            "        pass\n",
            "AgentRunRepository",
            "append_event_bound",
        ),
        (
            "class SafeRunRecorder:\n"
            "    del append_prepared_event_bound\n"
            "    def append_prepared_event_bound(self, session, draft):\n"
            "        pass\n",
            "SafeRunRecorder",
            "append_prepared_event_bound",
        ),
        (
            "class AgentRunRepository:\n"
            "    del append_event_bound\n"
            "    def append_event_bound(self, session, run_id, draft):\n"
            "        pass\n",
            "AgentRunRepository",
            "append_event_bound",
        ),
    ),
)
def test_mutations_reject_validated_method_class_rebinding(
    source: str, class_name: str, method_name: str
) -> None:
    _expect_rejected(
        source,
        lambda tree: _class_method(_top_level_class(tree, class_name), method_name),
    )


@pytest.mark.parametrize(
    "source",
    (
        "from other import SafeRunRecorder as SafeRunRecorder\nclass SafeRunRecorder:\n    pass\n",
        "for SafeRunRecorder in items:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "SafeRunRecorder += alias\nclass SafeRunRecorder:\n    pass\n",
        "with context as SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "try:\n    pass\nexcept Exception as SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "match value:\n    case SafeRunRecorder:\n        pass\nclass SafeRunRecorder:\n    pass\n",
        "match value:\n    case _ as SafeRunRecorder:\n        pass\nclass SafeRunRecorder:\n    pass\n",
        "match value:\n    case {**SafeRunRecorder}:\n        pass\nclass SafeRunRecorder:\n    pass\n",
        "def SafeRunRecorder():\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "def outer():\n    def SafeRunRecorder():\n        pass\nclass SafeRunRecorder:\n    pass\n",
        "class SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
    ),
)
def test_mutations_reject_module_level_class_rebinding_and_duplicates(source: str) -> None:
    _expect_rejected(source, lambda tree: _top_level_class(tree, "SafeRunRecorder"))


@pytest.mark.parametrize(
    ("source", "class_name"),
    (
        (
            "class SafeRunRecorder:\n"
            "    pass\n"
            "del SafeRunRecorder\n",
            "SafeRunRecorder",
        ),
        (
            "class AgentRunRepository:\n"
            "    pass\n"
            "del AgentRunRepository\n",
            "AgentRunRepository",
        ),
    ),
)
def test_mutations_reject_module_level_class_deletion(
    source: str, class_name: str
) -> None:
    _expect_rejected(source, lambda tree: _top_level_class(tree, class_name))


@pytest.mark.parametrize(
    "source",
    (
        "AgentRunRepository.append_event_bound = replacement\n",
        "SafeRunRecorder.append_prepared_event_bound = replacement\n",
        "AgentRunRepository.append_event = replacement\n",
        "SafeRunRecorder.append_event = replacement\n",
        "AgentRunRepository.append_event_bound: object = replacement\n",
        "SafeRunRecorder.append_prepared_event_bound += replacement\n",
        "del AgentRunRepository.append_event_bound\n",
        "del SafeRunRecorder.append_prepared_event_bound\n",
        "setattr(AgentRunRepository, 'append_event_bound', replacement)\n",
        "delattr(SafeRunRecorder, 'append_prepared_event_bound')\n",
    ),
)
def test_mutations_reject_external_method_rebinding(source: str) -> None:
    _expect_rejected(source, _validate_journal_repository_calls)


def test_mutation_rejects_synthetic_external_method_walrus_target() -> None:
    target = ast.Attribute(
        value=ast.Name(id="AgentRunRepository", ctx=ast.Load()),
        attr="append_event_bound",
        ctx=ast.Store(),
    )
    tree = ast.Module(
        body=[
            ast.Expr(
                value=ast.NamedExpr(
                    target=target,
                    value=ast.Name(id="replacement", ctx=ast.Load()),
                )
            )
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(tree)
    with pytest.raises(AssertionError):
        _validate_journal_repository_calls(tree)


@pytest.mark.parametrize(
    "source",
    (
        "def run():\n    ActiveWorkBudget = replacement\n",
        "def run():\n    (ActiveWorkBudget := replacement)\n",
        "def run():\n    from evil import ActiveWorkBudget\n",
        "def run(ActiveWorkBudget):\n    pass\n",
        "def run():\n    del ActiveWorkBudget\n",
        "def run():\n    for ActiveWorkBudget in values:\n        pass\n",
        "def run():\n    with context as ActiveWorkBudget:\n        pass\n",
        "def run():\n    try:\n        pass\n    except Exception as ActiveWorkBudget:\n        pass\n",
        "def run():\n    match value:\n        case _ as ActiveWorkBudget:\n            pass\n",
        "def run():\n    match value:\n        case {**ActiveWorkBudget}:\n            pass\n",
        "def run():\n    return [value for ActiveWorkBudget in values]\n",
        "def run():\n    return (lambda ActiveWorkBudget: ActiveWorkBudget)(value)\n",
        "class Wrapper:\n    ActiveWorkBudget = replacement\n",
        "def run():\n    SafeRunRecorder = replacement\n",
        "def run():\n    OperationLease = replacement\n",
        "def run():\n    SafeClockAdapter = replacement\n",
        "def run():\n    JournalBudgetExhausted = replacement\n",
        "def run():\n    JournalDeadlineExceeded = replacement\n",
        "def run():\n    MonotonicSample = replacement\n",
        "ActiveWorkBudget = replacement\n",
        "if enabled:\n    from evil import ActiveWorkBudget\n",
        "from evil import ActiveWorkBudget\n",
    ),
)
def test_mutations_reject_protected_symbol_scope_rebinding(source: str) -> None:
    _expect_rejected(source, _validate_protected_symbol_scopes)


def test_approved_module_level_protected_bindings_are_accepted() -> None:
    _validate_protected_symbol_scopes(
        ast.parse(
            "from offerpilot.agent_runtime.budget import ActiveWorkBudget\n"
            "class SafeRunRecorder:\n"
            "    pass\n"
        )
    )


@pytest.mark.parametrize(
    "source",
    (
        "def run(self):\n    repo = self.repository\n    repo.append_event(x)\n",
        "def run(self):\n    (repo := self.repository).append_event(x)\n",
        "def run(self):\n    self.repository.new_method(x)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=None, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=None)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock, clock=clock)\n",
        "def run(self):\n    self.repository.append_event(x, **kwargs)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock, **kwargs)\n",
        "def run(self):\n    self.__getattribute__('repository').append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=0.1, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=other.work_deadline, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=clock)\n",
        "def run(self):\n    self.repository = other_repository\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    setattr(self, 'repository', other_repository)\n    self.repository.append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock)\n",
    ),
)
def test_mutations_reject_repository_alias_unknown_none_and_clock_paths(source: str) -> None:
    _expect_rejected(source, _validate_journal_repository_calls)


def test_approved_constructor_repository_initialization_is_accepted() -> None:
    _validate_journal_repository_calls(
        ast.parse(
            "class SafeRunRecorder:\n"
            "    def __init__(self, repository):\n"
            "        self.repository = repository\n"
        )
    )


@pytest.mark.parametrize(
    "source",
    (
        "def run(self):\n    return vars(self)['repository'].append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock)\n",
        "def run(self):\n    state = vars(self)\n    return state['repository'].append_event(x, deadline=lease.work_deadline, safe_clock=lease.safe_clock)\n",
    ),
)
def test_mutations_reject_dynamic_repository_mapping_access(source: str) -> None:
    _expect_rejected(source, _validate_journal_repository_calls)


@pytest.mark.parametrize(
    "source",
    (
        "def run(self):\n    return self.clock()\n",
        "def run(self):\n    clock = self.clock\n",
        "def run(self):\n    return getattr(self, 'clock')\n",
        "def run(self):\n    return getattr(self, key)\n",
        "def run(self):\n    return self.__dict__['clock']\n",
        "def run(self):\n    return self.__getattribute__('clock')\n",
        "def run(self):\n    return self._clock()\n",
        "def run(self):\n    clock = self._clock\n",
        "def run(self):\n    return self.__dict__['_clock']\n",
    ),
)
def test_mutations_reject_clock_alias_call_and_dynamic_access(source: str) -> None:
    _expect_rejected(source, _validate_self_clock_access)


@pytest.mark.parametrize(
    "source",
    (
        "def run(self):\n    return vars(self)['clock']()\n",
        "def run(self):\n    state = vars(self)\n    return state['clock']\n",
        "def run(self):\n    return vars(self)['_clock']()\n",
        "def run(self):\n    state = vars(self)\n    return state['_clock']\n",
    ),
)
def test_mutations_reject_dynamic_clock_mapping_access(source: str) -> None:
    _expect_rejected(source, _validate_self_clock_access)


def test_approved_clock_constructor_argument_is_accepted() -> None:
    _validate_self_clock_access(
        ast.parse("def run(self):\n    return ActiveWorkBudget(0.1, self.clock)\n")
    )
    _validate_self_clock_access(
        ast.parse("def run(self):\n    return ActiveWorkBudget(0.1, self._clock)\n")
    )


def test_approved_constructor_clock_initialization_is_accepted() -> None:
    _validate_self_clock_access(
        ast.parse(
            "class SafeRunRecorder:\n"
            "    def __init__(self, clock):\n"
            "        self._clock = clock\n"
            "        self.clock = clock\n"
        )
    )


@pytest.mark.parametrize(
    "source",
    (
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin():
            self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        db = session
        with session.begin_nested():
            self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            session.execute('SELECT 1')
            self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested().connection():
            self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            with other_context:
                self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(db, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(*args, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(session, self.run_id, draft, **kwargs)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(session, self.run_id, draft, extra)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(
                session, self.run_id, draft, extra=extra
            )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(
                session, self.run_id, draft, *args
            )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(
                session, run_id, draft
            )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(
                session, self.run_id, other_draft
            )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            def nested():
                self.repository.append_event_bound(session, self.run_id, draft)
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            callback = lambda: self.repository.append_event_bound(
                session, self.run_id, draft
            )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            class Nested:
                def run(inner):
                    self.repository.append_event_bound(
                        session, self.run_id, draft
                    )
""",
        """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(session, self.run_id, draft)
            self.repository.append_event(
                draft, deadline=lease.work_deadline, safe_clock=lease.safe_clock
            )
""",
    ),
)
def test_mutations_reject_bound_session_alias_chain_and_other_calls(source: str) -> None:
    _expect_rejected(
        source,
        lambda tree: _validate_bound_method(
            _class_method(_top_level_class(tree, "SafeRunRecorder"), "append_prepared_event_bound")
        ),
    )


def test_mutation_rejects_append_bound_outside_owned_recorder_method() -> None:
    source = """
class SafeRunRecorder:
    def append_prepared_event_bound(self, session, draft):
        with session.begin_nested():
            self.repository.append_event_bound(session, self.run_id, draft)
    def other(self, session, draft):
        self.repository.append_event_bound(session, self.run_id, draft)
"""
    _expect_rejected(source, _validate_bound_call_ownership)


@pytest.mark.parametrize(
    "source",
    (
        "def append_event_bound(self, session, run_id, draft):\n    session.begin()\n",
        "def append_event_bound(self, session, run_id, draft):\n    db = session\n    self._insert_event(db, run_id, draft)\n",
        "def append_event_bound(self, session, run_id, draft):\n    with session.begin_nested():\n        self._insert_event(session, run_id, draft)\n",
    ),
)
def test_mutations_reject_repository_bound_session_ownership(source: str) -> None:
    wrapped = f"class AgentRunRepository:\n    {source.replace(chr(10), chr(10) + '    ')}"
    _expect_rejected(
        wrapped,
        lambda tree: _validate_bound_repository_implementation(
            _class_method(
                _top_level_class(tree, "AgentRunRepository"), "append_event_bound"
            )
        ),
    )


@pytest.mark.parametrize(
    "source",
    (
        "def append_event_bound(self, session, run_id, draft):\n    self._existing_event(other_session, run_id, draft)\n    self._required_run(other_session, run_id)\n    return self._insert_event(other_session, run_id, draft)\n",
        "def append_event_bound(self, session, run_id, draft):\n    self._existing_event(session, run_id, draft)\n    return self._insert_event(session, run_id, draft)\n",
        "def append_event_bound(self, session, run_id, draft):\n    self._existing_event(session, run_id, draft)\n    self._required_run(other_session, run_id)\n    return self._insert_event(session, run_id, draft)\n",
        "def append_event_bound(self, session, run_id, draft):\n    self._existing_event(session, run_id, draft)\n    self._required_run(session, run_id)\n    return self._insert_event(other_session, run_id, draft)\n",
    ),
)
def test_mutations_reject_missing_or_non_session_bound_helper_arguments(source: str) -> None:
    wrapped = f"class AgentRunRepository:\n    {source.replace(chr(10), chr(10) + '    ')}"
    _expect_rejected(
        wrapped,
        lambda tree: _validate_bound_repository_implementation(
            _class_method(
                _top_level_class(tree, "AgentRunRepository"), "append_event_bound"
            )
        ),
    )


@pytest.mark.parametrize(
    "source",
    (
        "def append_event_bound(self, /, clock, session, run_id, draft):\n    pass\n",
        "def append_event_bound(self, session, run_id, draft, *, deadline=None):\n    pass\n",
        "def append_event_bound(self, session, run_id, draft, *args):\n    pass\n",
        "def append_event_bound(self, session, run_id, draft, **kwargs):\n    pass\n",
    ),
)
def test_mutations_reject_bound_signature_clock_posonly_deadline_and_varargs(source: str) -> None:
    wrapped = f"class AgentRunRepository:\n    {source.replace(chr(10), chr(10) + '    ')}"
    _expect_rejected(
        wrapped,
        lambda tree: _validate_bound_signature(
            _class_method(_top_level_class(tree, "AgentRunRepository"), "append_event_bound")
        ),
    )


@pytest.mark.parametrize(
    "source",
    (
        "from offerpilot.agent_runtime.budget import ActiveWorkBudget as Budget\n",
        "from offerpilot.agent_runtime.budget import *\n",
        "from offerpilot.agent_runtime import budget as budget\n",
        "import offerpilot.agent_runtime.budget as budget\n",
        "import offerpilot.agent_runtime as runtime\nruntime.budget\n",
        "import offerpilot\nofferpilot.agent_runtime.budget\n",
        "import offerpilot.agent_runtime\nofferpilot.agent_runtime.budget\n",
        "from .. import agent_runtime as runtime\nruntime.budget\n",
        "from offerpilot import agent_runtime as runtime\nruntime.budget\n",
        "import importlib\nimportlib.import_module('offerpilot.agent_runtime.budget')\n",
    ),
)
def test_mutations_reject_budget_import_alias_star_and_dynamic_import(source: str) -> None:
    _expect_rejected(source, _validate_boundary_module)
