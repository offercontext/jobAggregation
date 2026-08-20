from __future__ import annotations

import ast
from pathlib import Path


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
BUDGET_NAMES = frozenset(
    {"ActiveWorkBudget", "OperationLease", "SafeClockAdapter", "JournalBudgetExhausted"}
)
STALE_JOURNAL_NAMES = frozenset(
    {"_segment_deadline", "_segment_started_at", "_disposition_attempted"}
)

# These are the non-Journal product surfaces that must not know about the
# Journal's active-work budget implementation.  Keep this list explicit so a
# new runtime module cannot silently become part of the contract by filename.
ARCHITECTURE_BOUNDARY_FILES = (
    ROOT / "src/offerpilot/ai/agent.py",  # Graph + pending action state
    ROOT / "src/offerpilot/ai/write_operations.py",  # write-operation ledger
    ROOT / "src/offerpilot/repositories/chat.py",  # chat + pending state
    ROOT / "src/offerpilot/models.py",
    ROOT / "src/offerpilot/schemas.py",
    ROOT / "src/offerpilot/api.py",
    ROOT / "src/offerpilot/repositories/json_contract.py",  # API serialization
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _repository_call(node: ast.Call) -> ast.Attribute | None:
    function = node.func
    if not isinstance(function, ast.Attribute):
        return None
    receiver = function.value
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "self"
        and receiver.attr == "repository"
    ):
        return function
    return None


def _methods(tree: ast.AST, names: set[str]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    }


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


def test_journal_repository_calls_are_budget_bound() -> None:
    tree = _module(JOURNAL_PATH)
    observed: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = _repository_call(node)
        if function is None or function.attr not in JOURNAL_REPOSITORY_API:
            continue
        observed.append((function.attr, node.lineno))
        keywords = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        assert {"deadline", "safe_clock"} <= keywords, (
            f"journal repository call {function.attr} at line {node.lineno} "
            "must pass deadline and safe_clock"
        )

    assert observed, "Journal must contain budget-bound repository calls"


def test_bound_append_event_is_owned_by_safe_recorder() -> None:
    tree = _module(JOURNAL_PATH)
    calls: list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]] = []
    method: ast.FunctionDef | ast.AsyncFunctionDef | None = None

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_stack.append(node)
            if node.name == "append_prepared_event_bound":
                nonlocal method
                method = node
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.function_stack.append(node)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "append_event_bound":
                owner = next(
                    (
                        candidate
                        for candidate in reversed(self.function_stack)
                        if candidate.name == "append_prepared_event_bound"
                    ),
                    None,
                )
                calls.append((node, owner))
            self.generic_visit(node)

    Visitor().visit(tree)
    assert method is not None, "SafeRunRecorder.append_prepared_event_bound is required"
    assert len(calls) == 1, "append_event_bound must have exactly one Journal call site"
    call, owner = calls[0]
    assert owner is method, "append_event_bound must be called by append_prepared_event_bound"
    assert _repository_call(call) is not None, "append_event_bound must use the Journal repository"
    assert not {keyword.arg for keyword in call.keywords if keyword.arg} & {
        "deadline",
        "safe_clock",
    }

    forbidden = _node_names(method) & BOUND_FORBIDDEN_NAMES
    assert not forbidden, f"bound append path owns no Journal transaction machinery: {sorted(forbidden)}"
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "pragma" not in node.value.lower(), "bound append path must not issue PRAGMA"

    method_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin_nested", "append_event_bound"}
    ]
    assert any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "begin_nested"
        for node in method_calls
    ), "bound append path must retain begin_nested ownership"
    assert any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "append_event_bound"
        for node in method_calls
    ), "bound append path must call append_event_bound"
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
        ):
            assert node.func.attr == "begin_nested", (
                "bound append path may only use the caller session's begin_nested"
            )


def test_journal_and_repository_have_no_stale_budget_paths() -> None:
    journal_tree = _module(JOURNAL_PATH)
    repository_tree = _module(REPOSITORY_PATH)

    stale = _node_names(journal_tree) & STALE_JOURNAL_NAMES
    assert not stale, f"stale Journal budget paths remain: {sorted(stale)}"

    clock_calls = [
        node
        for node in ast.walk(repository_tree)
        if isinstance(node, ast.Call)
        and any(keyword.arg == "clock" for keyword in node.keywords)
    ]
    assert not clock_calls, "repository calls must use deadline and safe_clock, never clock"

    class ClockVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[ast.AST] = []

        def generic_visit(self, node: ast.AST) -> None:
            self.stack.append(node)
            super().generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "clock"
            ):
                parent = self.stack[-1] if self.stack else None
                assert (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Name)
                    and parent.func.id == "ActiveWorkBudget"
                ), "direct self.clock() is only allowed while constructing ActiveWorkBudget"
            self.generic_visit(node)

    ClockVisitor().visit(journal_tree)


def test_product_surfaces_do_not_import_journal_budget_types() -> None:
    for path in ARCHITECTURE_BOUNDARY_FILES:
        assert path.is_file(), f"boundary file disappeared: {path}"
        names = _node_names(_module(path))
        assert not names & BUDGET_NAMES, (
            f"{path.relative_to(ROOT)} must not reference Journal budget types: "
            f"{sorted(names & BUDGET_NAMES)}"
        )


def test_append_event_bound_has_no_deadline_protocol() -> None:
    tree = _module(REPOSITORY_PATH)
    methods = _methods(tree, {"append_event_bound"})
    method = methods.get("append_event_bound")
    assert method is not None
    argument_names = {argument.arg for argument in method.args.args}
    argument_names.update(argument.arg for argument in method.args.kwonlyargs)
    assert not argument_names & {"deadline", "safe_clock"}

    forbidden = _node_names(method) & BOUND_FORBIDDEN_NAMES
    assert not forbidden, (
        "repository append_event_bound must remain a pure caller-owned bound protocol: "
        f"{sorted(forbidden)}"
    )
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "pragma" not in node.value.lower()


def test_journal_owned_repository_signatures_are_explicitly_budget_bound() -> None:
    tree = _module(REPOSITORY_PATH)
    methods = _methods(tree, set(JOURNAL_REPOSITORY_API))
    assert set(methods) == JOURNAL_REPOSITORY_API
    for name, method in methods.items():
        keyword_names = {argument.arg for argument in method.args.kwonlyargs}
        assert {"deadline", "safe_clock"} <= keyword_names, (
            f"{name} must explicitly declare deadline and safe_clock"
        )
        assert method.args.vararg is None and method.args.kwarg is None, (
            f"{name} must not hide Journal budget arguments behind *args/**kwargs"
        )
