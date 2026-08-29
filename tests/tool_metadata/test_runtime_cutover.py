from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.models import ChatMessage, WriteOperation
from offerpilot.repositories.chat import ChatRepository


ROOT = Path(__file__).parents[2]
PRODUCTION_ROOT = ROOT / "src" / "offerpilot"

TASK11_PRODUCTION_FILES = (
    PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy.py",
    PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy_proof.py",
    PRODUCTION_ROOT / "ai" / "tool_specs" / "legacy.py",
    PRODUCTION_ROOT / "ai" / "tool_specs" / "applications.py",
    PRODUCTION_ROOT / "ai" / "tool_specs" / "notes.py",
    PRODUCTION_ROOT / "ai" / "tool_specs" / "application_events.py",
    PRODUCTION_ROOT / "ai" / "confirmation.py",
    PRODUCTION_ROOT / "ai" / "write_operations.py",
    PRODUCTION_ROOT / "repositories" / "chat.py",
    PRODUCTION_ROOT / "pilot_runtime" / "service.py",
    PRODUCTION_ROOT / "pilot_runtime" / "continuation.py",
    PRODUCTION_ROOT / "pilot_runtime" / "persistence.py",
    PRODUCTION_ROOT / "pilot_runtime" / "deterministic.py",
    PRODUCTION_ROOT / "pilot_runtime" / "legacy_route.py",
    PRODUCTION_ROOT / "pilot_runtime" / "composition.py",
    PRODUCTION_ROOT / "ai" / "agent_loop.py",
    PRODUCTION_ROOT / "chat_transport.py",
    PRODUCTION_ROOT / "api.py",
)


def _sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in TASK11_PRODUCTION_FILES}


def _method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"missing {class_name}.{method_name}")


_RAW_ADAPTER_FIELDS = {"name", "editable_fields", "describe", "validate", "execute"}
_STATIC_ADAPTER_SPEC_FIELDS = _RAW_ADAPTER_FIELDS | {
    "ordinal",
    "chained_policy",
    "initial_route_sources",
    "presentation",
}


def _annotation_text(value: ast.expr | None) -> str:
    return "" if value is None else ast.unparse(value)


def _terminal_symbol(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _legacy_compatibility_violations(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    forbidden_imports = {
        "ServerLoadedPending",
        "LegacyProofDeterministicCatalog",
        "LegacyDeterministicAdapter",
    }
    forbidden_alias_sources = forbidden_imports | {"LegacyDeterministicCatalog"}
    role_bindings: dict[str, set[str]] = {}

    def roles_from_value(value: ast.AST) -> set[str]:
        if isinstance(value, ast.Name):
            if value.id in forbidden_alias_sources:
                return {value.id}
            return set(role_bindings.get(value.id, set()))
        if isinstance(value, ast.Attribute):
            return {value.attr} if value.attr in forbidden_alias_sources else set()
        if isinstance(value, (ast.Subscript, ast.Starred, ast.NamedExpr)):
            return roles_from_value(value.value)
        if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
            return set().union(*(roles_from_value(item) for item in value.elts))
        if isinstance(value, ast.Dict):
            return set().union(
                *(
                    roles_from_value(item)
                    for item in (*value.keys, *value.values)
                    if item is not None
                )
            )
        if isinstance(value, ast.IfExp):
            return roles_from_value(value.body) | roles_from_value(value.orelse)
        if isinstance(value, ast.BoolOp):
            return set().union(*(roles_from_value(item) for item in value.values))
        if isinstance(value, ast.Lambda):
            return roles_from_value(value.body)
        return set()

    def target_names(value: ast.AST) -> set[str]:
        return {item.id for item in ast.walk(value) if isinstance(item, ast.Name)}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in forbidden_alias_sources:
                    role_bindings.setdefault(imported.asname or imported.name, set()).add(
                        imported.name
                    )

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
            roles = roles_from_value(value)
            if not roles:
                continue
            for target in targets:
                for name in target_names(target):
                    before = len(role_bindings.get(name, set()))
                    role_bindings.setdefault(name, set()).update(roles)
                    changed = changed or len(role_bindings[name]) != before
        if not changed:
            break

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in forbidden_imports:
                    violations.append(f"forbidden import {imported.name}")
                if imported.name == "LegacyDeterministicCatalog" and imported.asname:
                    violations.append("forbidden alias of LegacyDeterministicCatalog")
        elif isinstance(node, (ast.Assign, ast.NamedExpr)):
            symbol = _terminal_symbol(node.value)
            if symbol in forbidden_alias_sources:
                violations.append(f"forbidden alias of {symbol}")
            if isinstance(node.value, ast.Lambda):
                captured = _terminal_symbol(node.value.body)
                if captured in forbidden_alias_sources:
                    violations.append(f"forbidden capture of {captured}")
        elif isinstance(node, ast.AnnAssign):
            annotation = _annotation_text(node.annotation)
            if "Callable" in annotation and "LegacyDeterministicCatalog" in annotation:
                violations.append("forbidden Legacy Catalog factory dependency")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                annotation = _annotation_text(argument.annotation)
                if "Callable" in annotation and "LegacyDeterministicCatalog" in annotation:
                    violations.append("forbidden Legacy Catalog factory parameter")
        elif isinstance(node, ast.Call):
            roles = roles_from_value(node.func)
            forbidden_constructors = roles & forbidden_alias_sources
            if forbidden_constructors:
                violations.append(
                    "forbidden direct construction of " + ", ".join(sorted(forbidden_constructors))
                )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "LegacyDeterministicCatalog" in _annotation_text(node.returns):
                violations.append("forbidden Legacy Catalog factory return")
            continue
        if not isinstance(node, ast.ClassDef):
            continue
        base_roles = set().union(*(roles_from_value(base) for base in node.bases))
        if base_roles & forbidden_alias_sources:
            violations.append(f"forbidden Legacy compatibility subclass {node.name}")
        methods = {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "resolve_server_loaded" in methods and node.name != "LegacyDeterministicCatalog":
            violations.append(f"second Legacy Catalog role {node.name}")
        fields = {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        if node.name == "LegacyDeterministicAdapterSpec":
            if not _STATIC_ADAPTER_SPEC_FIELDS <= fields:
                violations.append("incomplete static Legacy Adapter Spec shape")
        elif _RAW_ADAPTER_FIELDS <= fields:
            violations.append(f"raw Legacy Adapter shape {node.name}")
    return violations


def _prepared_input_boundary_violations(
    source: str,
    *,
    filename: str,
    allow_exact_owner: bool = False,
) -> list[str]:
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    exact_consumer_file = (PRODUCTION_ROOT / "ai" / "write_operations.py").resolve()
    allow_exact_consumer = Path(filename).resolve() == exact_consumer_file
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name == "LegacyPreparedInputPort" and imported.asname:
                    violations.append("aliased Legacy prepared-input Port import")
        elif isinstance(node, (ast.Assign, ast.NamedExpr)):
            if _terminal_symbol(node.value) == "LegacyPreparedInputPort":
                violations.append("aliased Legacy prepared-input Port")

    exact_owner_methods: set[ast.AST] = set()
    exact_consumer_methods: set[ast.AST] = set()
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        is_exact_owner = class_node.name == "LegacyPreparedInputPort"
        if is_exact_owner and not allow_exact_owner:
            violations.append("second Legacy prepared-input Port owner")
        if any(_terminal_symbol(base) == "LegacyPreparedInputPort" for base in class_node.bases):
            violations.append(f"Legacy prepared-input Port subclass {class_node.name}")
        for method in (
            item
            for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            if is_exact_owner:
                exact_owner_methods.add(method)
            if (
                allow_exact_consumer
                and class_node.name == "WriteOperationCoordinator"
                and method.name == "execute_legacy"
            ):
                exact_consumer_methods.add(method)
            parameter_names = {
                argument.arg
                for argument in (
                    *method.args.posonlyargs,
                    *method.args.args,
                    *method.args.kwonlyargs,
                )
            }
            if method.name == "execute_legacy" and (
                "input_fingerprint" in parameter_names or method.args.kwarg is not None
            ):
                violations.append("caller-supplied Legacy input fingerprint path")
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if function in exact_owner_methods or function in exact_consumer_methods:
            continue
        forwarded_require = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "require"
            and (
                {keyword.arg for keyword in call.keywords if keyword.arg is not None}
                >= {"operation_id", "tool_call_id", "tool_name"}
                or any(keyword.arg is None for keyword in call.keywords)
            )
            for call in ast.walk(function)
        )
        if forwarded_require:
            violations.append(f"forwarding prepared-input façade {function.name}")
    return violations


_SPECIAL_SUCCESS_TEXT = ("创建成功", "保存成功", "已保存")
_RAW_SUCCESS_RESULT_KEYS = {
    "application_id",
    "application_event_id",
    "company",
    "company_name",
    "id",
    "note_id",
    "position",
    "position_name",
    "round",
}


def _success_presentation_selection_violations(source: str, *, filename: str) -> list[str]:
    """Find service-owned success presentation, including simple helper indirection."""

    tree = ast.parse(source, filename=filename)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    relevant_names = {
        node.name
        for node in functions
        if any(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and any(marker in item.value for marker in _SPECIAL_SUCCESS_TEXT)
            for item in ast.walk(node)
        )
    }
    while True:
        callees = {
            called
            for node in functions
            if node.name in relevant_names
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            if (called := _terminal_symbol(item.func)) is not None
        }
        callers = {
            node.name
            for node in functions
            if any(
                isinstance(item, ast.Call) and _terminal_symbol(item.func) in relevant_names
                for item in ast.walk(node)
            )
        }
        function_names = {node.name for node in functions}
        expanded = relevant_names | callers | (callees & function_names)
        if expanded == relevant_names:
            break
        relevant_names = expanded

    violations: list[str] = []
    for node in functions:
        if node.name not in relevant_names:
            continue
        if any(
            isinstance(item, ast.Attribute) and item.attr == "tool_name" for item in ast.walk(node)
        ):
            violations.append(f"{node.name} selects success presentation from tool_name")
        for item in ast.walk(node):
            key: object | None = None
            if (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "get"
                and item.args
                and isinstance(item.args[0], ast.Constant)
            ):
                key = item.args[0].value
            elif isinstance(item, ast.Subscript) and isinstance(item.slice, ast.Constant):
                key = item.slice.value
            if key in _RAW_SUCCESS_RESULT_KEYS:
                violations.append(
                    f"{node.name} selects success presentation from raw result key {key!r}"
                )
    return violations


def test_all_operation_bearing_pending_repository_entries_require_a_route_handle() -> None:
    required = (
        "set_pending_action",
        "persist_pending_action",
        "replace_pending_confirmation",
        "persist_confirmation_continuation",
    )
    for method_name in required:
        parameters = inspect.signature(getattr(ChatRepository, method_name)).parameters
        assert "route_handle" in parameters, (
            f"ChatRepository.{method_name} must require an exact Task 11 route handle"
        )
        assert parameters["route_handle"].default is inspect.Parameter.empty


def test_transient_route_handles_and_raw_runtime_values_are_not_persisted() -> None:
    pending_fields = frozenset(PendingAction.__dataclass_fields__)
    message_columns = frozenset(ChatMessage.__table__.columns.keys())
    operation_columns = frozenset(WriteOperation.__table__.columns.keys())
    forbidden = {
        "route_handle",
        "bundle_instance_token",
        "segment_catalog_token",
        "pending_claim_instance_token",
        "legacy_route_proof",
        "raw_exception",
        "raw_result",
    }
    assert pending_fields.isdisjoint(forbidden)
    assert message_columns.isdisjoint(forbidden)
    assert operation_columns.isdisjoint(forbidden)


def test_task11_removes_old_name_classification_and_runtime_facades() -> None:
    sources = _sources()
    forbidden = (
        "TRANSACTIONAL_TYPED_WRITE_NAMES",
        "TYPED_WRITE_OPERATION_NAMES",
        "LEGACY_WRITE_OPERATION_NAMES",
        "COMPENSATION_OPERATION_NAMES",
        "REQUIRED_UNDO_OPERATION_NAMES",
        "WRITE_OPERATION_NAMES",
        "_pending_adapter_kind",
        "_chained_adapter_kind",
        "_valid_chained_topology_values",
        "_pending_action_details",
        "_legacy_catalog",
        "_legacy_adapter",
        "editable_fields_for_tool",
        "_undo_seed_for_pending",
        "_build_write_undo",
        "_CREATED_RECORD_FINGERPRINT_FIELDS",
        "ServerLoadedPending",
        "LegacyProofDeterministicCatalog",
        "legacy_catalog_factory",
        "build_legacy_deterministic_catalog",
        "_prepend_write_success",
        "_last_successful_tool_payload",
        "class LegacyDeterministicAdapter:",
        "LegacyDeterministicAdapter(",
        'parent_operation_name == "save_application_jd_version"',
    )
    combined = "\n".join(sources.values())
    for marker in forbidden:
        assert marker not in combined, f"Task 11 legacy facade remains reachable: {marker}"
    compatibility_violations = [
        f"{path.relative_to(ROOT).as_posix()}: {violation}"
        for path, source in sources.items()
        for violation in _legacy_compatibility_violations(source, filename=str(path))
    ]
    assert compatibility_violations == []


def test_prepared_input_port_has_one_exact_owner_and_no_caller_fingerprint() -> None:
    expected_owner = PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy_proof.py"
    assert expected_owner in TASK11_PRODUCTION_FILES

    definitions: list[Path] = []
    boundary_violations: list[str] = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "LegacyPreparedInputPort":
                    definitions.append(path)
        boundary_violations.extend(
            f"{path.relative_to(ROOT).as_posix()}: {violation}"
            for violation in _prepared_input_boundary_violations(
                source,
                filename=str(path),
                allow_exact_owner=path == expected_owner,
            )
        )

    assert definitions == [expected_owner]
    assert boundary_violations == []

    coordinator_path = PRODUCTION_ROOT / "ai" / "write_operations.py"
    coordinator_tree = ast.parse(
        coordinator_path.read_text(encoding="utf-8"),
        filename=str(coordinator_path),
    )
    execute_legacy = _method(coordinator_tree, "WriteOperationCoordinator", "execute_legacy")
    parameters = {
        argument.arg
        for argument in (
            *execute_legacy.args.posonlyargs,
            *execute_legacy.args.args,
            *execute_legacy.args.kwonlyargs,
        )
    }
    assert "input_fingerprint" not in parameters
    assert execute_legacy.args.kwarg is None


@pytest.mark.parametrize(
    "source",
    (
        "from runtime import LegacyPreparedInputPort as PreparedInputFacade",
        "Compat = LegacyPreparedInputPort",
        "class Compat(LegacyPreparedInputPort):\n    pass",
        (
            "class PreparedInputFacade:\n"
            "    def __init__(self, inner: LegacyPreparedInputPort):\n"
            "        self.inner = inner\n"
            "    def require(self, prepared, *, operation_id, tool_call_id, tool_name):\n"
            "        return self.inner.require(prepared, operation_id=operation_id, "
            "tool_call_id=tool_call_id, tool_name=tool_name)\n"
        ),
        (
            "class RenamedFacade:\n"
            "    def project(self, value):\n"
            "        return self.inner.require(value, operation_id='op', "
            "tool_call_id='call', tool_name='tool')\n"
        ),
        (
            "def project(inner, value):\n"
            "    return inner.require(value, operation_id='op', "
            "tool_call_id='call', tool_name='tool')\n"
        ),
        (
            "class OtherCoordinator:\n"
            "    def execute_legacy(self, inner, value):\n"
            "        return inner.require(value, operation_id='op', "
            "tool_call_id='call', tool_name='tool')\n"
        ),
        (
            "class WriteOperationCoordinator:\n"
            "    def execute_legacy(self, inner, value):\n"
            "        return inner.require(value, operation_id='op', "
            "tool_call_id='call', tool_name='tool')\n"
        ),
        ("def project(inner, value, identity):\n    return inner.require(value, **identity)\n"),
        (
            "class WriteOperationCoordinator:\n"
            "    def execute_legacy(self, operation_id, **kwargs):\n"
            "        return kwargs.get('input_fingerprint')\n"
        ),
        (
            "class WriteOperationCoordinator:\n"
            "    def execute_legacy(self, operation_id, input_fingerprint):\n"
            "        return input_fingerprint\n"
        ),
    ),
)
def test_prepared_input_boundary_gate_rejects_facades_and_fingerprint_backdoors(
    source: str,
) -> None:
    assert _prepared_input_boundary_violations(source, filename="negative_fixture.py")


def test_service_does_not_select_success_presentation_from_name_or_raw_result() -> None:
    path = PRODUCTION_ROOT / "pilot_runtime" / "service.py"
    source = path.read_text(encoding="utf-8")
    assert _success_presentation_selection_violations(source, filename=str(path)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "def renamed_success(pending, result):\n"
            "    selected = pending\n"
            "    name = selected.tool_name\n"
            "    if name == 'create_application':\n"
            "        return '创建成功'\n"
            "    return ''\n"
        ),
        (
            "def render_success(record_id):\n"
            "    return f'保存成功：记录 #{record_id} 已保存'\n"
            "def renamed_success(result):\n"
            "    payload = result\n"
            "    return render_success(payload.get('note_id'))\n"
        ),
        (
            "def renamed_success(result):\n"
            "    payload = result\n"
            "    return f\"创建成功：日程 #{payload['application_event_id']} 已保存\"\n"
        ),
        (
            "def pick_company(result):\n"
            "    payload = result\n"
            "    return payload.get('company_name')\n"
            "def renamed_success(result):\n"
            "    return f'创建成功：{pick_company(result)} 已保存'\n"
        ),
    ),
)
def test_service_presentation_gate_rejects_aliases_and_raw_result_dataflow(source: str) -> None:
    assert _success_presentation_selection_violations(source, filename="negative_fixture.py")


@pytest.mark.parametrize(
    "source",
    (
        "from offerpilot.ai.tool_runtime.legacy import LegacyDeterministicAdapter as RawAdapter",
        ("from offerpilot.ai.tool_runtime.legacy import LegacyDeterministicCatalog as Compat"),
        "RawAdapter = LegacyDeterministicAdapter",
        "Compat = lr.LegacyDeterministicCatalog",
        "class Compat(LegacyDeterministicCatalog):\n    pass",
        "class Compat(lr.LegacyDeterministicCatalog):\n    pass",
        "class OtherCatalog:\n    def resolve_server_loaded(self, proof):\n        return proof",
        (
            "class RenamedAdapter:\n"
            "    name: str\n    editable_fields: tuple\n    describe: object\n"
            "    validate: object\n    execute: object"
        ),
        (
            "class Dependencies:\n"
            "    catalog_builder: Callable[[object], LegacyDeterministicCatalog]"
        ),
        "def make_catalog(repo: object) -> LegacyDeterministicCatalog:\n    raise RuntimeError",
        "factory = lambda: lr.LegacyDeterministicCatalog",
        "factory = lambda: lr.LegacyDeterministicCatalog(proof_consumer_port=None)",
        ("def make():\n    return lr.LegacyDeterministicCatalog(proof_consumer_port=None)"),
        (
            "items = [lr.LegacyDeterministicCatalog]\n"
            "Compat = items[0]\n"
            "class Other(Compat):\n    pass"
        ),
    ),
)
def test_legacy_compatibility_gate_rejects_alias_subclass_shape_and_dead_factory(
    source: str,
) -> None:
    assert _legacy_compatibility_violations(source, filename="negative_fixture.py")


def test_legacy_compatibility_gate_accepts_the_single_proof_catalog_shape() -> None:
    source = (
        "class LegacyDeterministicAdapterSpec:\n"
        "    ordinal: int\n    name: str\n    editable_fields: tuple\n"
        "    chained_policy: str\n    initial_route_sources: tuple\n"
        "    describe: object\n    validate: object\n    presentation: object\n"
        "    execute: object\n"
        "class LegacyDeterministicCatalog:\n"
        "    @classmethod\n"
        "    def _create(cls) -> LegacyDeterministicCatalog:\n"
        "        return cls()\n"
        "    def resolve_server_loaded(self, proof: LegacyRouteProof):\n"
        "        return proof"
    )
    assert _legacy_compatibility_violations(source, filename="positive_fixture.py") == []


def test_legacy_server_loaded_catalog_accepts_only_proof_not_pending_or_name() -> None:
    path = PRODUCTION_ROOT / "ai" / "tool_runtime" / "legacy.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = _method(tree, "LegacyDeterministicCatalog", "resolve_server_loaded")
    parameters = [argument.arg for argument in method.args.args]
    assert parameters == ["self", "proof"]
    assert method.args.vararg is None
    assert method.args.kwarg is None


def test_runtime_cutover_has_no_typed_to_legacy_fallback_or_shadow_execution() -> None:
    sources = _sources()
    combined = "\n".join(sources.values()).casefold()
    forbidden = (
        "typed_to_legacy",
        "typed-to-legacy",
        "shadow_execute",
        "shadow_execution",
        "dual_registry",
        "legacy_fallback",
        "metadata_fallback",
    )
    assert all(marker not in combined for marker in forbidden)


def test_terminal_replay_and_reject_do_not_resolve_or_issue_runtime_routes() -> None:
    path = PRODUCTION_ROOT / "pilot_runtime" / "continuation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    terminal = _method(tree, "ConfirmationCoordinator", "terminal_replay")
    reject = _method(tree, "ConfirmationCoordinator", "reject")
    forbidden_calls = {
        "prepare_server_loaded",
        "locked_recheck",
        "bind_claim",
        "issue_after_claim",
        "resolve_server_loaded",
        "open_segment_lease",
        "prepare_call",
        "executor",
    }
    for method in (terminal, reject):
        called = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called.isdisjoint(forbidden_calls)
