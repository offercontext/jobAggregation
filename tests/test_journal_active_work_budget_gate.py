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
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }
    for child in ast.walk(node):
        if isinstance(child, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            target = child.name
            if isinstance(target, str):
                names.add(target)
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
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                assert name not in _target_names(node.optional_vars), (
                    f"{name} must not be shadowed by a with target"
                )
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            raise AssertionError(f"{name} must not be shadowed by an except target")
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


def _validate_journal_repository_calls(tree: ast.AST) -> None:
    parents = _parents(tree)
    calls = _direct_repository_calls(tree)
    allowed = JOURNAL_REPOSITORY_API | {"append_event_bound"}

    for node in ast.walk(tree):
        if not _is_self_attribute(node, "repository"):
            continue
        if isinstance(node.ctx, ast.Store):
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
        if isinstance(node, ast.Attribute) and node.attr == "__getattribute__":
            raise AssertionError("dynamic self.repository access is forbidden")
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
        if _is_self_attribute(node, "clock"):
            parent = parents.get(node)
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
            if isinstance(node.func, ast.Attribute) and node.func.attr == "__getattribute__":
                raise AssertionError("dynamic self clock access is forbidden")

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
    assert append_call.args and isinstance(append_call.args[0], ast.Name)
    assert append_call.args[0].id == "session", (
        "append_event_bound must receive the exact session name first"
    )
    assert all(keyword.arg is not None for keyword in append_call.keywords)
    assert not any(isinstance(argument, ast.Starred) for argument in append_call.args)
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
    allowed_helpers = {"_existing_event", "_required_run", "_insert_event"}
    for node in ast.walk(method):
        if not isinstance(node, ast.Name) or node.id != "session":
            continue
        parent = parents.get(node)
        allowed_helper_argument = (
            isinstance(parent, ast.Call)
            and parent.args
            and parent.args[0] is node
            and isinstance(parent.func, ast.Attribute)
            and _is_self_name(parent.func.value)
            and parent.func.attr in allowed_helpers
        )
        assert allowed_helper_argument, (
            "append_event_bound must not own or manipulate the caller session"
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
BUDGET_MODULE_SUFFIX = "agent_runtime.budget"


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
    agent_runtime_aliases = {
        alias.asname or alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "offerpilot.agent_runtime"
    }
    agent_runtime_aliases.update(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        if node.level > 0 or node.module == "offerpilot"
        for alias in node.names
        if alias.name == "agent_runtime"
    )
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
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "budget"
            and isinstance(node.value, ast.Name)
            and node.value.id in agent_runtime_aliases
        ):
            raise AssertionError("dynamic agent_runtime.budget access is forbidden")
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


def test_journal_repository_calls_are_budget_bound() -> None:
    tree = _module(JOURNAL_PATH)
    _top_level_class(tree, "SafeRunRecorder")
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
    repository = _top_level_class(tree, "AgentRunRepository")
    _validate_bound_repository_implementation(
        _class_method(repository, "append_event_bound")
    )


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
    "source",
    (
        "from other import SafeRunRecorder as SafeRunRecorder\nclass SafeRunRecorder:\n    pass\n",
        "for SafeRunRecorder in items:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "SafeRunRecorder += alias\nclass SafeRunRecorder:\n    pass\n",
        "with context as SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "try:\n    pass\nexcept Exception as SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "def SafeRunRecorder():\n    pass\nclass SafeRunRecorder:\n    pass\n",
        "def outer():\n    def SafeRunRecorder():\n        pass\nclass SafeRunRecorder:\n    pass\n",
        "class SafeRunRecorder:\n    pass\nclass SafeRunRecorder:\n    pass\n",
    ),
)
def test_mutations_reject_module_level_class_rebinding_and_duplicates(source: str) -> None:
    _expect_rejected(source, lambda tree: _top_level_class(tree, "SafeRunRecorder"))


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
    ),
)
def test_mutations_reject_repository_alias_unknown_none_and_clock_paths(source: str) -> None:
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
    ),
)
def test_mutations_reject_clock_alias_call_and_dynamic_access(source: str) -> None:
    _expect_rejected(source, _validate_self_clock_access)


def test_approved_clock_constructor_argument_is_accepted() -> None:
    _validate_self_clock_access(
        ast.parse("def run(self):\n    return ActiveWorkBudget(0.1, self.clock)\n")
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
        "from .. import agent_runtime as runtime\nruntime.budget\n",
        "from offerpilot import agent_runtime as runtime\nruntime.budget\n",
        "import importlib\nimportlib.import_module('offerpilot.agent_runtime.budget')\n",
    ),
)
def test_mutations_reject_budget_import_alias_star_and_dynamic_import(source: str) -> None:
    _expect_rejected(source, _validate_boundary_module)
