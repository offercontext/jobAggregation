from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "offerpilot"
MIGRATION = "0029_review_to_readiness_feedback"
PRODUCT_ACTIONS = (
    "confirm_interview_story",
    "save_review_readiness_signal",
)
PRODUCT_ACTION_COMPENSATIONS = (
    "undo:confirm_interview_story",
    "undo:save_review_readiness_signal",
)
PRODUCT_ACTION_MODULES = frozenset(
    {
        "__init__.py",
        "contracts.py",
        "catalog.py",
        "issuer.py",
        "repository.py",
        "coordinator.py",
        "compensation.py",
    }
)
RAW_DECODER_NAME = "decode_product_action_request_v1"
PRODUCT_ACTION_ROUTE_MARKERS = (
    "readiness-focus-actions",
    "product-actions",
    "product-action-undo",
    "/decisions",
)


def _parse(path: Path) -> ast.Module | None:
    if not path.is_file():
        return None
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_string_sequence(tree: ast.AST | None, name: str) -> tuple[str, ...] | None:
    if tree is None:
        return None
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            continue
        items = tuple(_literal_string(item) for item in value.elts)
        if all(item is not None for item in items):
            return tuple(item for item in items if item is not None)
    return None


def _catalog_exposes_independent_names(
    tree: ast.AST | None,
    *,
    class_name: str,
    names_binding: str,
    forbidden_binding: str,
) -> bool:
    if tree is None:
        return False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        references = {
            child.id for child in ast.walk(node) if isinstance(child, ast.Name)
        }
        has_behavior = any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            for member in node.body
        )
        return (
            has_behavior
            and names_binding in references
            and forbidden_binding not in references
        )
    return False


def _product_action_violations(
    module_trees: dict[str, ast.Module | None],
) -> list[str]:
    modules_complete = set(module_trees) == PRODUCT_ACTION_MODULES and all(
        module_trees[name] is not None for name in PRODUCT_ACTION_MODULES
    )
    contracts = module_trees.get("contracts.py")
    catalog = module_trees.get("catalog.py")
    primary_exact = (
        _literal_string_sequence(contracts, "PRODUCT_ACTION_NAMES") == PRODUCT_ACTIONS
        and _catalog_exposes_independent_names(
            catalog,
            class_name="ProductActionCatalogV1",
            names_binding="PRODUCT_ACTION_NAMES",
            forbidden_binding="PRODUCT_ACTION_COMPENSATION_NAMES",
        )
    )
    compensation_exact = (
        _literal_string_sequence(contracts, "PRODUCT_ACTION_COMPENSATION_NAMES")
        == PRODUCT_ACTION_COMPENSATIONS
        and _catalog_exposes_independent_names(
            catalog,
            class_name="ProductActionCompensationCatalogV1",
            names_binding="PRODUCT_ACTION_COMPENSATION_NAMES",
            forbidden_binding="PRODUCT_ACTION_NAMES",
        )
    )
    violations: list[str] = []
    if not modules_complete or not primary_exact:
        violations.append("ledger:missing-product-action")
    if not modules_complete or not compensation_exact:
        violations.append("ledger:missing-product-action-compensation")
    return violations


def _has_registered_migration(tree: ast.AST | None) -> bool:
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        terminal = None
        if isinstance(node.func, ast.Name):
            terminal = node.func.id
        elif isinstance(node.func, ast.Attribute):
            terminal = node.func.attr
        if terminal == "_record_migration" and any(
            _literal_string(argument) == MIGRATION for argument in node.args
        ):
            return True
        if terminal != "execute" or not node.args:
            continue
        statement = _literal_string(node.args[0])
        if statement is None or "insert into schema_migrations" not in " ".join(
            statement.casefold().split()
        ):
            continue
        if any(
            isinstance(item, ast.Constant) and item.value == MIGRATION
            for argument in node.args[1:]
            for item in ast.walk(argument)
        ):
            return True
    return False


def _has_self_committing_story_call(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "confirm_attempt":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "confirm_attempt":
            return True
    return False


def _reachable_practice_functions(
    tree: ast.AST | None,
    root_name: str,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
    if tree is None:
        return ()
    module_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    repository_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_functions[node.name] = node
        elif isinstance(node, ast.ClassDef) and node.name == "AdaptivePracticeRepository":
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    repository_methods[member.name] = member

    root = repository_methods.get(root_name)
    if root is None:
        return ()
    reachable: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        reachable.append(current)
        for child in ast.walk(current):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                helper = module_functions.get(child.func.id)
            elif (
                isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id
                in {"self", "cls", "AdaptivePracticeRepository"}
            ):
                helper = repository_methods.get(child.func.attr)
            else:
                helper = None
            if helper is not None and id(helper) not in seen:
                pending.append(helper)
    return tuple(reachable)


def _proposal_drives_practice_start(tree: ast.AST | None) -> bool:
    reachable = _reachable_practice_functions(tree, "start")
    if not reachable:
        return False
    root_parameters = {
        argument.arg
        for argument in (
            *reachable[0].args.posonlyargs,
            *reachable[0].args.args,
            *reachable[0].args.kwonlyargs,
        )
    }
    return "proposal_id" in root_parameters or any(
        isinstance(child, ast.Name) and child.id == "InterviewReviewProposal"
        for member in reachable
        for child in ast.walk(member)
    )


def _proposal_drives_practice_recommendations(tree: ast.AST | None) -> bool:
    reachable = _reachable_practice_functions(tree, "list_recommendations")

    class RuntimeModelAccess(ast.NodeVisitor):
        found = False

        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if node.id == "InterviewReviewProposal":
                self.found = True

        def visit_arg(self, node: ast.arg) -> None:
            return

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
            self.visit(node.target)
            if node.value is not None:
                self.visit(node.value)

        def _visit_function(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> None:
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
            for statement in node.body:
                self.visit(statement)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._visit_function(node)

    for member in reachable:
        visitor = RuntimeModelAccess()
        visitor.visit(member)
        if visitor.found:
            return True
    return False


def _proposal_drives_practice(tree: ast.AST | None) -> bool:
    return _proposal_drives_practice_start(tree) or _proposal_drives_practice_recommendations(tree)


def _has_note_revision_helper(tree: ast.AST | None) -> bool:
    if tree is None:
        return False
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_revisioned_note_values":
            continue
        string_literals = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        has_revision_source = any(
            isinstance(child, ast.Attribute)
            and child.attr == "content_revision"
            and isinstance(child.value, ast.Name)
            and child.value.id == "InterviewNote"
            for child in ast.walk(node)
        )
        has_increment = any(
            isinstance(child, ast.BinOp)
            and isinstance(child.op, ast.Add)
            and isinstance(child.right, ast.Constant)
            and type(child.right.value) is int
            and child.right.value == 1
            for child in ast.walk(node)
        )
        has_timestamp = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "current_timestamp"
            for child in ast.walk(node)
        )
        return (
            {"content_revision", "updated_at"}.issubset(string_literals)
            and has_revision_source
            and has_increment
            and has_timestamp
        )
    return False


def _application_event_delete_violations(path: Path, tree: ast.Module) -> list[str]:
    def assigned_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Starred):
            return assigned_names(target.value)
        if isinstance(target, (ast.List, ast.Tuple)):
            return {
                name
                for element in target.elts
                for name in assigned_names(element)
            }
        return set()

    def assignment_parts(
        node: ast.AST,
    ) -> tuple[tuple[ast.expr, ...], ast.expr | None]:
        if isinstance(node, ast.Assign):
            return tuple(node.targets), node.value
        if isinstance(node, ast.AnnAssign):
            return (node.target,), node.value
        if isinstance(node, ast.NamedExpr):
            return (node.target,), node.value
        return (), None

    violations: list[str] = []
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    class Lineage:
        def __init__(self, parent: Lineage | None = None) -> None:
            self.models = set(parent.models) if parent is not None else {"ApplicationEvent"}
            self.delete_factories = set(parent.delete_factories) if parent is not None else {"delete"}
            self.tables = set(parent.tables) if parent is not None else set()
            self.queries = set(parent.queries) if parent is not None else set()
            self.rows = set(parent.rows) if parent is not None else set()
            self.selection_results = (
                set(parent.selection_results) if parent is not None else set()
            )
            self.scalar_collections = (
                set(parent.scalar_collections) if parent is not None else set()
            )

        def discard(self, names: set[str]) -> None:
            self.models.difference_update(names)
            self.delete_factories.difference_update(names)
            self.tables.difference_update(names)
            self.queries.difference_update(names)
            self.rows.difference_update(names)
            self.selection_results.difference_update(names)
            self.scalar_collections.difference_update(names)

    def local_nodes(scope: ast.AST) -> list[ast.AST]:
        nodes: list[ast.AST] = []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, scope_types):
                continue
            nodes.append(candidate)
            pending.extend(ast.iter_child_nodes(candidate))
        return nodes

    def child_scopes(scope: ast.AST) -> list[ast.AST]:
        children: list[ast.AST] = []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, scope_types):
                children.append(candidate)
                continue
            pending.extend(ast.iter_child_nodes(candidate))
        return children

    def scope_arguments(scope: ast.AST) -> tuple[ast.arg, ...]:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return (
                *scope.args.posonlyargs,
                *scope.args.args,
                *scope.args.kwonlyargs,
            )
        return ()

    def local_bindings(scope: ast.AST, nodes: list[ast.AST]) -> set[str]:
        names = {argument.arg for argument in scope_arguments(scope)}
        for candidate in nodes:
            targets, _value = assignment_parts(candidate)
            names.update(
                name
                for target in targets
                for name in assigned_names(target)
            )
            if isinstance(candidate, (ast.For, ast.AsyncFor)):
                names.update(assigned_names(candidate.target))
            elif isinstance(candidate, (ast.With, ast.AsyncWith)):
                for item in candidate.items:
                    if item.optional_vars is not None:
                        names.update(assigned_names(item.optional_vars))
            elif isinstance(candidate, ast.ExceptHandler) and candidate.name is not None:
                names.add(candidate.name)
            elif isinstance(candidate, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in candidate.names)
        names.update(
            child.name
            for child in child_scopes(scope)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        return names

    def analyze_scope(scope: ast.AST, inherited: Lineage | None) -> None:
        nodes = local_nodes(scope)
        lineage = Lineage(inherited)
        lineage.discard(local_bindings(scope, nodes))

        for candidate in nodes:
            if not isinstance(candidate, ast.ImportFrom):
                continue
            if candidate.module == "offerpilot.models":
                lineage.models.update(
                    alias.asname or alias.name
                    for alias in candidate.names
                    if alias.name == "ApplicationEvent"
                )
            elif candidate.module == "sqlalchemy":
                lineage.delete_factories.update(
                    alias.asname or alias.name
                    for alias in candidate.names
                    if alias.name == "delete"
                )

        def is_event_model(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.models
            if isinstance(node, ast.Attribute):
                return node.attr == "ApplicationEvent"
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "aliased")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "aliased")
                )
                and bool(node.args)
                and is_event_model(node.args[0])
            )

        def is_delete_factory(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.delete_factories
            return isinstance(node, ast.Attribute) and node.attr == "delete"

        def is_event_table(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.tables
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "__table__"
                and is_event_model(node.value)
            ):
                return True
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"alias", "table_valued"}
                and is_event_table(node.func.value)
            )

        def query_targets_event(node: ast.AST) -> bool:
            if isinstance(node, ast.Name) and node.id in lineage.queries:
                return True
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "query"
                and any(is_event_model(argument) for argument in child.args)
                for child in ast.walk(node)
            )

        def select_targets_event(node: ast.AST) -> bool:
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, (ast.Name, ast.Attribute))
                and (
                    (isinstance(child.func, ast.Name) and child.func.id == "select")
                    or (isinstance(child.func, ast.Attribute) and child.func.attr == "select")
                )
                and any(is_event_model(argument) for argument in child.args)
                for child in ast.walk(node)
            )

        def selection_result_targets_event(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.selection_results
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    (
                        node.func.attr in {"execute", "scalars"}
                        and bool(node.args)
                        and select_targets_event(node.args[0])
                    )
                    or (
                        node.func.attr in {"unique", "yield_per"}
                        and selection_result_targets_event(node.func.value)
                    )
                )
            )

        def scalar_collection_targets_event(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.scalar_collections
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    (
                        node.func.attr == "scalars"
                        and (
                            (
                                bool(node.args)
                                and select_targets_event(node.args[0])
                            )
                            or selection_result_targets_event(node.func.value)
                        )
                    )
                    or (
                        node.func.attr in {"all", "unique", "yield_per"}
                        and scalar_collection_targets_event(node.func.value)
                    )
                )
            )

        def is_event_row_source(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.rows
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                return False
            if node.func.attr == "get":
                return bool(node.args) and is_event_model(node.args[0])
            if node.func.attr == "scalar":
                return (
                    bool(node.args) and select_targets_event(node.args[0])
                ) or (
                    not node.args
                    and selection_result_targets_event(node.func.value)
                )
            if node.func.attr in {"scalar_one", "scalar_one_or_none"}:
                return selection_result_targets_event(node.func.value)
            if node.func.attr in {"first", "one", "one_or_none"}:
                return query_targets_event(
                    node.func.value
                ) or scalar_collection_targets_event(node.func.value)
            return False

        def contains_raw_event_delete(node: ast.Call) -> bool:
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "exec_driver_sql"}
            ):
                return False
            return any(
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "delete from application_events"
                in " ".join(child.value.lower().split())
                .replace('"', "")
                .replace("`", "")
                .replace("[", "")
                .replace("]", "")
                for child in ast.walk(node)
            )

        def annotation_targets_event(annotation: ast.expr | None) -> bool:
            return annotation is not None and any(
                is_event_model(child) for child in ast.walk(annotation)
            )

        lineage.rows.update(
            argument.arg
            for argument in scope_arguments(scope)
            if annotation_targets_event(argument.annotation)
        )
        lineage.rows.update(
            name
            for candidate in nodes
            if isinstance(candidate, ast.AnnAssign)
            and annotation_targets_event(candidate.annotation)
            for name in assigned_names(candidate.target)
        )

        changed = True
        while changed:
            changed = False
            for candidate in nodes:
                targets, value = assignment_parts(candidate)
                if value is None:
                    continue
                names = {
                    name
                    for target in targets
                    for name in assigned_names(target)
                }
                target_sets: tuple[tuple[bool, set[str]], ...] = (
                    (is_event_model(value), lineage.models),
                    (is_delete_factory(value), lineage.delete_factories),
                    (is_event_table(value), lineage.tables),
                    (query_targets_event(value), lineage.queries),
                    (is_event_row_source(value), lineage.rows),
                    (
                        selection_result_targets_event(value),
                        lineage.selection_results,
                    ),
                    (
                        scalar_collection_targets_event(value),
                        lineage.scalar_collections,
                    ),
                )
                for matches, known_names in target_sets:
                    if matches and not names.issubset(known_names):
                        known_names.update(names)
                        changed = True
            for candidate in nodes:
                if not isinstance(candidate, (ast.For, ast.AsyncFor)):
                    continue
                if not scalar_collection_targets_event(candidate.iter):
                    continue
                names = assigned_names(candidate.target)
                if not names.issubset(lineage.rows):
                    lineage.rows.update(names)
                    changed = True

        owner = (
            scope.name
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            else ""
        )
        approved_owner = (
            path.as_posix().endswith("repositories/application_events.py")
            and owner == "_delete_application_event_owned"
        )
        for candidate in nodes:
            if not isinstance(candidate, ast.Call):
                continue
            direct_sql_delete = (
                is_delete_factory(candidate.func)
                and bool(candidate.args)
                and is_event_model(candidate.args[0])
            )
            table_delete = (
                isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "delete"
                and is_event_table(candidate.func.value)
            )
            query_delete = (
                isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "delete"
                and query_targets_event(candidate.func.value)
            )
            orm_delete = (
                isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "delete"
                and bool(candidate.args)
                and is_event_row_source(candidate.args[0])
            )
            raw_sql_delete = contains_raw_event_delete(candidate)
            if (
                direct_sql_delete
                or table_delete
                or query_delete
                or orm_delete
                or raw_sql_delete
            ) and not approved_owner:
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{candidate.lineno}"
                )

        for child in child_scopes(scope):
            analyze_scope(child, lineage)

    analyze_scope(tree, None)
    return violations


def _call_terminal(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_request_call(node: ast.Call, method: str) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "request"
        and not node.args
        and not node.keywords
    )


def _is_awaited_request_body(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and _is_request_call(node.value, "body")
    )


def _raw_body_binding(statement: ast.stmt) -> str | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and _is_awaited_request_body(statement.value)
    ):
        return statement.targets[0].id
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.value is not None
        and _is_awaited_request_body(statement.value)
    ):
        return statement.target.id
    return None


def _loads_any_name(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(child, ast.Name)
        and child.id in names
        and isinstance(child.ctx, ast.Load)
        for child in ast.walk(node)
    )


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _controlled_derivation(node: ast.AST, names: set[str]) -> bool:
    forbidden = (
        ast.Await,
        ast.Call,
        ast.GeneratorExp,
        ast.Lambda,
        ast.ListComp,
        ast.NamedExpr,
        ast.SetComp,
        ast.Yield,
        ast.YieldFrom,
        ast.DictComp,
    )
    return (
        _loads_any_name(node, names)
        and _loaded_names(node) <= names
        and not any(isinstance(child, forbidden) for child in ast.walk(node))
    )


def _simple_assignment(
    statement: ast.stmt,
) -> tuple[ast.Name, ast.AST] | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0], statement.value
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.value is not None
    ):
        return statement.target, statement.value
    return None


def _direct_controlled_call(
    node: ast.AST,
    controlled_names: set[str],
    context_id_names: set[str],
) -> ast.Call | None:
    value = node.value if isinstance(node, ast.Await) else node
    if isinstance(value, ast.Call):
        arguments = (*value.args, *(item.value for item in value.keywords))
        allowed_names = controlled_names | context_id_names
        forbidden = (
            ast.Await,
            ast.Call,
            ast.GeneratorExp,
            ast.Lambda,
            ast.ListComp,
            ast.NamedExpr,
            ast.SetComp,
            ast.Yield,
            ast.YieldFrom,
            ast.DictComp,
        )
        has_controlled_argument = any(
            _loads_any_name(argument, controlled_names) for argument in arguments
        )
        all_arguments_are_closed = all(
            (
                bool(_loaded_names(argument))
                and _loaded_names(argument) <= allowed_names
                and not any(
                    isinstance(child, forbidden) for child in ast.walk(argument)
                )
            )
            or isinstance(argument, ast.Constant)
            for argument in arguments
        )
        if has_controlled_argument and all_arguments_are_closed:
            return value
    return None


def _return_route_sink(
    statement: ast.Return,
    controlled_names: set[str],
    context_id_names: set[str],
) -> tuple[bool, ast.Call | None]:
    if statement.value is None:
        return False, None
    direct_call = _direct_controlled_call(
        statement.value,
        controlled_names,
        context_id_names,
    )
    if direct_call is not None:
        return True, direct_call
    value = statement.value.value if isinstance(statement.value, ast.Await) else statement.value
    if _controlled_derivation(value, controlled_names) or (
        isinstance(value, ast.Name) and value.id in controlled_names
    ):
        return True, None
    return False, None


def _decoder_flow_reaches_route_sink(
    statements: list[ast.stmt],
    *,
    decoder_result_name: str,
    raw_body_name: str | None,
    context_id_names: set[str],
) -> bool:
    controlled_names = {decoder_result_name}
    allowed_stores: set[int] = set()
    response_names: set[str] = set()
    returned_response_names: set[str] = set()
    route_sink_seen = False

    for statement in statements:
        if raw_body_name is not None and any(
            isinstance(child, ast.Name) and child.id == raw_body_name
            for child in ast.walk(statement)
        ):
            return False

        assignment = _simple_assignment(statement)
        assigned_sink = (
            _direct_controlled_call(
                assignment[1],
                controlled_names,
                context_id_names,
            )
            if assignment is not None
            else None
        )
        if assignment is not None and assigned_sink is not None:
            target, _value = assignment
            if target.id in controlled_names or target.id in response_names:
                return False
            response_names.add(target.id)
            allowed_stores.add(id(target))
        elif assignment is not None and _loads_any_name(
            assignment[1],
            controlled_names,
        ):
            target, value = assignment
            if target.id in controlled_names or not _controlled_derivation(
                value,
                controlled_names,
            ):
                return False
            controlled_names.add(target.id)
            allowed_stores.add(id(target))
        elif any(
            isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                _loads_any_name(value, controlled_names)
                for value in ast.iter_child_nodes(child)
            )
            for child in ast.walk(statement)
        ):
            return False

        allowed_sink_calls = {id(assigned_sink)} if assigned_sink is not None else set()
        for returned in (
            child for child in ast.walk(statement) if isinstance(child, ast.Return)
        ):
            if returned.value is not None:
                returned_response_names.update(
                    name
                    for name in response_names
                    if _loads_any_name(returned.value, {name})
                )
            is_sink, sink_call = _return_route_sink(
                returned,
                controlled_names,
                context_id_names,
            )
            if not is_sink:
                continue
            route_sink_seen = True
            if sink_call is not None:
                allowed_sink_calls.add(id(sink_call))

        for call in (child for child in ast.walk(statement) if isinstance(child, ast.Call)):
            if isinstance(call.func, ast.Attribute) and _loads_any_name(
                call.func.value,
                controlled_names,
            ):
                return False
            if any(
                _loads_any_name(argument, controlled_names)
                for argument in (*call.args, *(item.value for item in call.keywords))
            ) and id(call) not in allowed_sink_calls:
                return False

    guarded_names = controlled_names | response_names | context_id_names
    for statement in statements:
        for child in ast.walk(statement):
            if (
                isinstance(child, ast.Name)
                and child.id in guarded_names
                and not isinstance(child.ctx, ast.Load)
                and id(child) not in allowed_stores
            ):
                return False
            if (
                isinstance(child, (ast.Attribute, ast.Subscript))
                and not isinstance(child.ctx, ast.Load)
                and _loads_any_name(child, guarded_names)
            ):
                return False
    all_response_sinks_returned = (
        not response_names or response_names == returned_response_names
    )
    return all_response_sinks_returned and (
        route_sink_seen or bool(response_names)
    )


def _unsafe_product_action_http_bodies(tree: ast.AST | None) -> list[str]:
    """Reject normalization/coercion before the duplicate-aware raw decoder."""

    if tree is None:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator_text = " ".join(ast.unparse(item) for item in node.decorator_list)
        if not any(marker in decorator_text for marker in PRODUCT_ACTION_ROUTE_MARKERS):
            continue
        decorator_casefold = decorator_text.casefold()
        if ".post(" not in decorator_casefold and ".patch(" not in decorator_casefold:
            continue
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        context_id_names = {
            argument.arg
            for argument in arguments
            if argument.arg.endswith("_id") and argument.arg != "request"
        }
        defaults = (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        )
        has_raw_request = any(
            argument.arg == "request"
            and argument.annotation is not None
            and "Request" in ast.unparse(argument.annotation)
            for argument in arguments
        )
        body_or_model_parameter = any(
            argument.annotation is not None
            and (
                "dict" in ast.unparse(argument.annotation).casefold()
                or "BaseModel" in ast.unparse(argument.annotation)
            )
            for argument in arguments
        ) or any(
            isinstance(child, ast.Call)
            and (
                isinstance(child.func, ast.Name)
                and child.func.id == "Body"
                or isinstance(child.func, ast.Attribute)
                and child.func.attr == "Body"
            )
            for argument in arguments
            if argument.annotation is not None
            for child in ast.walk(argument.annotation)
        ) or any(
            isinstance(child, ast.Call)
            and (
                isinstance(child.func, ast.Name)
                and child.func.id == "Body"
                or isinstance(child.func, ast.Attribute)
                and child.func.attr == "Body"
            )
            for default in defaults
            for child in ast.walk(default)
        )
        decoder_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and _call_terminal(child) == RAW_DECODER_NAME
        ]
        if body_or_model_parameter or not has_raw_request or len(decoder_calls) != 1:
            violations.append(f"ui:unsafe-product-action-body:{node.name}")
            continue

        decoder_call = decoder_calls[0]
        decoder_statement_index = next(
            (
                index
                for index, statement in enumerate(node.body)
                if any(child is decoder_call for child in ast.walk(statement))
            ),
            None,
        )
        if decoder_statement_index is None:
            violations.append(f"ui:unsafe-product-action-body:{node.name}")
            continue

        raw_body_sources = {
            binding: index
            for index, statement in enumerate(node.body[:decoder_statement_index])
            if (binding := _raw_body_binding(statement)) is not None
        }
        decoder_input = decoder_call.args[0] if decoder_call.args else None
        bound_input_is_exclusive = False
        if isinstance(decoder_input, ast.Name) and decoder_input.id in raw_body_sources:
            source_index = raw_body_sources[decoder_input.id]
            bound_input_is_exclusive = not any(
                isinstance(child, ast.Name)
                and child.id == decoder_input.id
                for statement in node.body[source_index + 1 : decoder_statement_index]
                for child in ast.walk(statement)
            )
        decoder_uses_raw_body = (
            decoder_input is not None
            and (
                _is_awaited_request_body(decoder_input)
                or (
                    isinstance(decoder_input, ast.Name)
                    and bound_input_is_exclusive
                )
            )
        )
        context_ids_are_unchanged = not any(
            isinstance(child, ast.Name)
            and child.id in context_id_names
            and not isinstance(child.ctx, ast.Load)
            for statement in node.body
            for child in ast.walk(statement)
        )
        request_body_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and _is_request_call(child, "body")
        ]
        request_json_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and _is_request_call(child, "json")
        ]
        calls_before_decoder_are_safe = all(
            _raw_body_binding(statement) is not None
            or not any(isinstance(child, ast.Call) for child in ast.walk(statement))
            for statement in node.body[:decoder_statement_index]
        )

        decoder_result_name: str | None = None
        decoder_statement = node.body[decoder_statement_index]
        if (
            isinstance(decoder_statement, ast.Assign)
            and len(decoder_statement.targets) == 1
            and isinstance(decoder_statement.targets[0], ast.Name)
            and decoder_statement.value is decoder_call
        ):
            decoder_result_name = decoder_statement.targets[0].id
        elif (
            isinstance(decoder_statement, ast.AnnAssign)
            and isinstance(decoder_statement.target, ast.Name)
            and decoder_statement.value is decoder_call
        ):
            decoder_result_name = decoder_statement.target.id
        decoder_flow_is_safe = decoder_result_name is not None and (
            _decoder_flow_reaches_route_sink(
                node.body[decoder_statement_index + 1 :],
                decoder_result_name=decoder_result_name,
                raw_body_name=(
                    decoder_input.id if isinstance(decoder_input, ast.Name) else None
                ),
                context_id_names=context_id_names,
            )
        )
        if (
            not decoder_uses_raw_body
            or len(request_body_calls) != 1
            or request_json_calls
            or not calls_before_decoder_are_safe
            or not context_ids_are_unchanged
            or not decoder_flow_is_safe
        ):
            violations.append(f"ui:unsafe-product-action-body:{node.name}")
    return violations


def _interview_note_mutation_violations(path: Path, tree: ast.Module) -> list[str]:
    guarded_fields = {
        "application_event_id",
        "application_id",
        "company",
        "content",
        "content_revision",
        "date",
        "difficulty_points",
        "mood",
        "position",
        "questions",
        "round",
        "self_reflection",
        "updated_at",
    }
    scope_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    violations: list[str] = []

    def assigned_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Starred):
            return assigned_names(target.value)
        if isinstance(target, (ast.List, ast.Tuple)):
            return {
                name
                for element in target.elts
                for name in assigned_names(element)
            }
        return set()

    def assignment_parts(
        node: ast.AST,
    ) -> tuple[tuple[ast.expr, ...], ast.expr | None]:
        if isinstance(node, ast.Assign):
            return tuple(node.targets), node.value
        if isinstance(node, ast.AnnAssign):
            return (node.target,), node.value
        if isinstance(node, ast.NamedExpr):
            return (node.target,), node.value
        return (), None

    class Lineage:
        def __init__(self, parent: Lineage | None = None) -> None:
            self.models = set(parent.models) if parent is not None else {"InterviewNote"}
            self.tables = set(parent.tables) if parent is not None else set()
            self.queries = set(parent.queries) if parent is not None else set()
            self.rows = set(parent.rows) if parent is not None else set()
            self.selection_results = (
                set(parent.selection_results) if parent is not None else set()
            )
            self.scalar_collections = (
                set(parent.scalar_collections) if parent is not None else set()
            )

        def discard(self, names: set[str]) -> None:
            self.models.difference_update(names)
            self.tables.difference_update(names)
            self.queries.difference_update(names)
            self.rows.difference_update(names)
            self.selection_results.difference_update(names)
            self.scalar_collections.difference_update(names)

    def local_nodes(scope: ast.AST) -> list[ast.AST]:
        nodes: list[ast.AST] = []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, scope_types):
                continue
            nodes.append(candidate)
            pending.extend(ast.iter_child_nodes(candidate))
        return nodes

    def child_scopes(scope: ast.AST) -> list[ast.AST]:
        children: list[ast.AST] = []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, scope_types):
                children.append(candidate)
                continue
            pending.extend(ast.iter_child_nodes(candidate))
        return children

    def scope_arguments(scope: ast.AST) -> tuple[ast.arg, ...]:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return (
                *scope.args.posonlyargs,
                *scope.args.args,
                *scope.args.kwonlyargs,
            )
        return ()

    def local_bindings(scope: ast.AST, nodes: list[ast.AST]) -> set[str]:
        names = {argument.arg for argument in scope_arguments(scope)}
        for candidate in nodes:
            targets, _value = assignment_parts(candidate)
            names.update(
                name
                for target in targets
                for name in assigned_names(target)
            )
            if isinstance(candidate, (ast.For, ast.AsyncFor)):
                names.update(assigned_names(candidate.target))
            elif isinstance(candidate, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in candidate.names)
        names.update(
            child.name
            for child in child_scopes(scope)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        return names

    def analyze_scope(scope: ast.AST, inherited: Lineage | None) -> None:
        nodes = local_nodes(scope)
        lineage = Lineage(inherited)
        lineage.discard(local_bindings(scope, nodes))
        for candidate in nodes:
            if isinstance(candidate, ast.ImportFrom) and candidate.module == "offerpilot.models":
                lineage.models.update(
                    alias.asname or alias.name
                    for alias in candidate.names
                    if alias.name == "InterviewNote"
                )

        parent_by_id = {
            id(child): parent
            for parent in (scope, *nodes)
            for child in ast.iter_child_nodes(parent)
        }

        def source_position(node: ast.AST) -> tuple[int, int]:
            return (
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
            )

        assignments = [
            (
                source_position(candidate),
                {
                    name
                    for target in assignment_parts(candidate)[0]
                    for name in assigned_names(target)
                },
                assignment_parts(candidate)[1],
                candidate,
            )
            for candidate in nodes
            if assignment_parts(candidate)[0]
        ]
        assignments.sort(key=lambda assignment: assignment[0])

        def expression_cutoff(
            node: ast.AST,
            cutoff: tuple[int, int] | None,
        ) -> tuple[int, int]:
            if cutoff is not None:
                return cutoff
            current = node
            while id(current) in parent_by_id:
                parent = parent_by_id[id(current)]
                if assignment_parts(parent)[1] is not None:
                    return source_position(parent)
                current = parent
            return source_position(node)

        def latest_assignment(
            name: str,
            cutoff: tuple[int, int],
        ) -> tuple[tuple[int, int], ast.expr | None, ast.AST] | None:
            matches = [
                (position, value, candidate)
                for position, names, value, candidate in assignments
                if name in names and position < cutoff
            ]
            return matches[-1] if matches else None

        def root_bound_name(node: ast.AST) -> str | None:
            while isinstance(node, (ast.Attribute, ast.Subscript)):
                node = node.value
            return node.id if isinstance(node, ast.Name) else None

        def revision_values_were_mutated(
            name: str,
            since: tuple[int, int],
            cutoff: tuple[int, int],
        ) -> bool:
            for candidate in nodes:
                position = source_position(candidate)
                if not since < position < cutoff:
                    continue
                targets: tuple[ast.expr, ...] = ()
                if isinstance(candidate, ast.Assign):
                    targets = tuple(candidate.targets)
                elif isinstance(candidate, (ast.AnnAssign, ast.AugAssign)):
                    targets = (candidate.target,)
                elif isinstance(candidate, ast.Delete):
                    targets = tuple(candidate.targets)
                if any(
                    (
                        isinstance(candidate, ast.AugAssign)
                        or isinstance(target, (ast.Attribute, ast.Subscript))
                    )
                    and root_bound_name(target) == name
                    for target in targets
                ):
                    return True
                if not isinstance(candidate, ast.Call):
                    continue
                if (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr
                    in {
                        "__delitem__",
                        "__setitem__",
                        "clear",
                        "pop",
                        "popitem",
                        "setdefault",
                        "update",
                    }
                    and root_bound_name(candidate.func.value) == name
                ):
                    return True
                if (
                    isinstance(candidate.func, (ast.Name, ast.Attribute))
                    and (
                        (
                            isinstance(candidate.func, ast.Name)
                            and candidate.func.id
                            in {"delattr", "delitem", "setattr", "setitem"}
                        )
                        or (
                            isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr
                            in {
                                "__delitem__",
                                "__setitem__",
                                "clear",
                                "delattr",
                                "delitem",
                                "pop",
                                "popitem",
                                "setattr",
                                "setdefault",
                                "setitem",
                                "update",
                            }
                        )
                    )
                    and bool(candidate.args)
                    and root_bound_name(candidate.args[0]) == name
                ):
                    return True
            return False

        def is_note_model(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.models
            if isinstance(node, ast.Attribute):
                return node.attr == "InterviewNote"
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "aliased")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "aliased")
                )
                and bool(node.args)
                and is_note_model(node.args[0])
            )

        def is_note_table(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.tables
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "__table__"
                and is_note_model(node.value)
            ):
                return True
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"alias", "table_valued"}
                and is_note_table(node.func.value)
            )

        def query_targets_note(node: ast.AST) -> bool:
            if isinstance(node, ast.Name) and node.id in lineage.queries:
                return True
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "query"
                and any(is_note_model(argument) for argument in child.args)
                for child in ast.walk(node)
            )

        def select_targets_note(node: ast.AST) -> bool:
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, (ast.Name, ast.Attribute))
                and (
                    (isinstance(child.func, ast.Name) and child.func.id == "select")
                    or (isinstance(child.func, ast.Attribute) and child.func.attr == "select")
                )
                and any(is_note_model(argument) for argument in child.args)
                for child in ast.walk(node)
            )

        def selection_result_targets_note(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.selection_results
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    (
                        node.func.attr in {"execute", "scalars"}
                        and bool(node.args)
                        and select_targets_note(node.args[0])
                    )
                    or (
                        node.func.attr in {"unique", "yield_per"}
                        and selection_result_targets_note(node.func.value)
                    )
                )
            )

        def scalar_collection_targets_note(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.scalar_collections
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    (
                        node.func.attr == "scalars"
                        and (
                            (
                                bool(node.args)
                                and select_targets_note(node.args[0])
                            )
                            or selection_result_targets_note(node.func.value)
                        )
                    )
                    or (
                        node.func.attr in {"all", "unique", "yield_per"}
                        and scalar_collection_targets_note(node.func.value)
                    )
                )
            )

        def annotation_targets_note(annotation: ast.expr | None) -> bool:
            return annotation is not None and any(
                is_note_model(child) for child in ast.walk(annotation)
            )

        def is_note_row_source(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.rows
            if not isinstance(node, ast.Call):
                return False
            if isinstance(node.func, ast.Name) and node.func.id == "cast":
                return bool(node.args) and annotation_targets_note(node.args[0])
            if not isinstance(node.func, ast.Attribute):
                return False
            if node.func.attr == "get":
                return bool(node.args) and is_note_model(node.args[0])
            if node.func.attr == "scalar":
                return (
                    bool(node.args) and select_targets_note(node.args[0])
                ) or (
                    not node.args
                    and selection_result_targets_note(node.func.value)
                )
            if node.func.attr in {"scalar_one", "scalar_one_or_none"}:
                return selection_result_targets_note(node.func.value)
            if node.func.attr in {"first", "one", "one_or_none"}:
                return query_targets_note(
                    node.func.value
                ) or scalar_collection_targets_note(node.func.value)
            return False

        def is_revision_values(
            node: ast.AST,
            cutoff: tuple[int, int] | None = None,
            seen: frozenset[tuple[str, tuple[int, int]]] = frozenset(),
        ) -> bool:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_revisioned_note_values"
            ):
                return True
            if not isinstance(node, ast.Name):
                return False
            resolved_cutoff = expression_cutoff(node, cutoff)
            key = (node.id, resolved_cutoff)
            if key in seen:
                return False
            matching_assignments = [
                (position, value, candidate)
                for position, names, value, candidate in assignments
                if node.id in names
            ]
            if len(matching_assignments) != 1:
                return False
            position, value, candidate = matching_assignments[0]
            if (
                value is None
                or position >= resolved_cutoff
                or not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                or parent_by_id.get(id(candidate)) is not scope
                or revision_values_were_mutated(
                    node.id,
                    position,
                    resolved_cutoff,
                )
            ):
                return False
            return is_revision_values(value, position, seen | {key})

        def is_note_update_factory(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "update")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "update"
                    )
                )
                and bool(node.args)
                and is_note_model(node.args[0])
            )

        def is_note_update_statement(
            node: ast.AST,
            cutoff: tuple[int, int] | None = None,
            seen: frozenset[tuple[str, tuple[int, int]]] = frozenset(),
        ) -> bool:
            resolved_cutoff = expression_cutoff(node, cutoff)
            if isinstance(node, ast.Name):
                key = (node.id, resolved_cutoff)
                if key in seen:
                    return False
                assignment = latest_assignment(node.id, resolved_cutoff)
                if assignment is None or assignment[1] is None:
                    return False
                position, value, _candidate = assignment
                return is_note_update_statement(value, position, seen | {key})
            if is_note_update_factory(node):
                return True
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "execution_options",
                    "ordered_values",
                    "prefix_with",
                    "returning",
                    "values",
                    "where",
                    "with_dialect_options",
                }
                and is_note_update_statement(node.func.value, resolved_cutoff, seen)
            )

        def update_call_uses_revision_values(node: ast.Call) -> bool:
            return any(
                is_revision_values(argument, source_position(argument))
                for argument in node.args
            ) or any(
                keyword.arg is None
                and is_revision_values(
                    keyword.value,
                    source_position(keyword.value),
                )
                for keyword in node.keywords
            )

        def is_revisioned_note_update(
            node: ast.AST,
            cutoff: tuple[int, int] | None = None,
            seen: frozenset[tuple[str, tuple[int, int]]] = frozenset(),
        ) -> bool:
            resolved_cutoff = expression_cutoff(node, cutoff)
            if isinstance(node, ast.Name):
                key = (node.id, resolved_cutoff)
                if key in seen:
                    return False
                assignment = latest_assignment(node.id, resolved_cutoff)
                if assignment is None or assignment[1] is None:
                    return False
                position, value, _candidate = assignment
                return is_revisioned_note_update(value, position, seen | {key})
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                return False
            if (
                node.func.attr in {"values", "ordered_values"}
                and is_note_update_statement(node.func.value, resolved_cutoff)
            ):
                return update_call_uses_revision_values(node)
            return (
                node.func.attr
                in {
                    "execution_options",
                    "prefix_with",
                    "returning",
                    "where",
                    "with_dialect_options",
                }
                and is_revisioned_note_update(node.func.value, resolved_cutoff, seen)
            )

        lineage.rows.update(
            argument.arg
            for argument in scope_arguments(scope)
            if annotation_targets_note(argument.annotation)
        )
        lineage.rows.update(
            name
            for candidate in nodes
            if isinstance(candidate, ast.AnnAssign)
            and annotation_targets_note(candidate.annotation)
            for name in assigned_names(candidate.target)
        )

        changed = True
        while changed:
            changed = False
            for candidate in nodes:
                targets, value = assignment_parts(candidate)
                if value is None:
                    continue
                names = {
                    name
                    for target in targets
                    for name in assigned_names(target)
                }
                target_sets: tuple[tuple[bool, set[str]], ...] = (
                    (is_note_model(value), lineage.models),
                    (is_note_table(value), lineage.tables),
                    (query_targets_note(value), lineage.queries),
                    (is_note_row_source(value), lineage.rows),
                    (
                        selection_result_targets_note(value),
                        lineage.selection_results,
                    ),
                    (
                        scalar_collection_targets_note(value),
                        lineage.scalar_collections,
                    ),
                )
                for matches, known_names in target_sets:
                    if matches and not names.issubset(known_names):
                        known_names.update(names)
                        changed = True
            for candidate in nodes:
                if not isinstance(candidate, (ast.For, ast.AsyncFor)):
                    continue
                if not scalar_collection_targets_note(candidate.iter):
                    continue
                names = assigned_names(candidate.target)
                if not names.issubset(lineage.rows):
                    lineage.rows.update(names)
                    changed = True

        owner = (
            scope.name
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            else ""
        )
        approved_sql_owner = (
            (
                path.as_posix().endswith("repositories/notes.py")
                and owner in {"update", "update_note_scoped"}
            )
            or (
                path.as_posix().endswith("repositories/application_events.py")
                and owner == "_delete_application_event_owned"
            )
        )

        def contains_raw_note_update(node: ast.Call) -> bool:
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "exec_driver_sql"}
            ):
                return False
            return any(
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "update interview_notes"
                in " ".join(child.value.lower().split())
                .replace('"', "")
                .replace("`", "")
                .replace("[", "")
                .replace("]", "")
                for child in ast.walk(node)
            )

        for candidate in nodes:
            mutation = False
            if isinstance(candidate, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    candidate.targets
                    if isinstance(candidate, ast.Assign)
                    else [candidate.target]
                )
                mutation = any(
                    isinstance(target, ast.Attribute)
                    and target.attr in guarded_fields
                    and is_note_row_source(target.value)
                    for root_target in targets
                    for target in ast.walk(root_target)
                )
            elif isinstance(candidate, ast.Call):
                update_values_call = (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr in {"values", "ordered_values"}
                    and is_note_update_statement(candidate.func.value)
                )
                update_values_violation = update_values_call and (
                    not approved_sql_owner
                    or not update_call_uses_revision_values(candidate)
                )
                executes_update = (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr in {"execute", "scalar", "scalars"}
                    and bool(candidate.args)
                    and is_note_update_statement(candidate.args[0])
                )
                execute_violation = executes_update and (
                    not approved_sql_owner
                    or not is_revisioned_note_update(candidate.args[0])
                )
                table_update = (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr == "update"
                    and is_note_table(candidate.func.value)
                )
                query_update = (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr == "update"
                    and query_targets_note(candidate.func.value)
                )
                mapping_update = (
                    isinstance(candidate.func, ast.Attribute)
                    and candidate.func.attr == "bulk_update_mappings"
                    and bool(candidate.args)
                    and is_note_model(candidate.args[0])
                )
                setattr_update = (
                    isinstance(candidate.func, ast.Name)
                    and candidate.func.id == "setattr"
                    and len(candidate.args) >= 2
                    and is_note_row_source(candidate.args[0])
                    and isinstance(candidate.args[1], ast.Constant)
                    and candidate.args[1].value in guarded_fields
                )
                mutation = (
                    update_values_violation
                    or execute_violation
                    or table_update
                    or query_update
                    or mapping_update
                    or setattr_update
                    or contains_raw_note_update(candidate)
                )
            if mutation:
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{candidate.lineno}"
                )

        for child in child_scopes(scope):
            analyze_scope(child, lineage)

    analyze_scope(tree, None)
    return violations


def _writes_raw_product_action_parent(tree: ast.AST | None) -> bool:
    """Detect construction or SQL insertion of a Product Action Ledger parent."""

    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        terminal = None
        if isinstance(node.func, ast.Name):
            terminal = node.func.id
        elif isinstance(node.func, ast.Attribute):
            terminal = node.func.attr
        if terminal == "WriteOperation" and any(
            keyword.arg == "adapter_kind"
            and _literal_string(keyword.value) == "product_action"
            for keyword in node.keywords
        ):
            return True
        literals = " ".join(
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        ).casefold()
        normalized = " ".join(literals.split())
        if "insert into write_operations" in normalized and "product_action" in normalized:
            return True
        if terminal in {"insert", "values"} and any(
            isinstance(child, ast.Name) and child.id == "WriteOperation"
            for child in ast.walk(node)
        ) and any(
            keyword.arg == "adapter_kind"
            and _literal_string(keyword.value) == "product_action"
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Call)
            for keyword in candidate.keywords
        ):
            return True
    return False


def _source_violations() -> list[str]:
    violations: list[str] = []
    db_tree = _parse(SRC / "db.py")
    product_action_dir = SRC / "product_actions"
    product_action_modules = {
        name: _parse(product_action_dir / name) for name in PRODUCT_ACTION_MODULES
    }
    notes_tree = _parse(SRC / "repositories" / "notes.py")
    practice_tree = _parse(SRC / "repositories" / "adaptive_interview_practice.py")
    api_tree = _parse(SRC / "api.py")

    if not _has_registered_migration(db_tree):
        violations.append("migration:missing-0029")
    violations.extend(_product_action_violations(product_action_modules))
    if any(
        _has_self_committing_story_call(tree)
        for path in sorted(SRC.rglob("*.py"))
        if (tree := _parse(path)) is not None
    ):
        violations.append("story:self-committing-confirm")
    if _proposal_drives_practice(practice_tree):
        violations.append("practice:unconfirmed-proposal-source")
    if not _has_note_revision_helper(notes_tree):
        violations.append("note:missing-content-revision")
    violations.extend(_unsafe_product_action_http_bodies(api_tree))
    parent_owner = (SRC / "product_actions" / "repository.py").resolve()
    for path in sorted(SRC.rglob("*.py")):
        if path.resolve() == parent_owner:
            continue
        if _writes_raw_product_action_parent(_parse(path)):
            violations.append(
                "ledger:raw-product-action-parent:" + path.relative_to(SRC).as_posix()
            )
    return violations


def test_detectors_require_exact_ast_surfaces_not_comments_or_strings() -> None:
    pseudo = ast.parse(
        '''
"""0029_review_to_readiness_feedback confirm_attempt InterviewReviewProposal"""
PRODUCT_ACTION_NAMES = "confirm_interview_story save_review_readiness_signal"
# _record_migration(engine, "0029_review_to_readiness_feedback", "fake")
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id):
        return "proposal_id InterviewReviewProposal"
def _revisioned_note_values(values):
    return "content_revision updated_at current_timestamp"
'''
    )
    assert not _has_registered_migration(pseudo)
    assert _literal_string_sequence(pseudo, "PRODUCT_ACTION_NAMES") is None
    assert not _has_self_committing_story_call(pseudo)
    assert not _proposal_drives_practice(pseudo)
    assert not _has_note_revision_helper(pseudo)


def test_detectors_accept_only_the_approved_mechanical_shapes() -> None:
    exact = ast.parse(
        '''
PRODUCT_ACTION_NAMES = ("confirm_interview_story", "save_review_readiness_signal")
PRODUCT_ACTION_COMPENSATION_NAMES = (
    "undo:confirm_interview_story", "undo:save_review_readiness_signal"
)
_record_migration(engine, "0029_review_to_readiness_feedback", "approved")
cursor.execute(
    "INSERT INTO schema_migrations(version,description) VALUES (?,?)",
    ("0029_review_to_readiness_feedback", "approved"),
)
def route(repo):
    return repo.confirm_attempt()
class AdaptivePracticeRepository:
    def start(self, proposal_id):
        return InterviewReviewProposal
def _revisioned_note_values(values):
    return {
        **values,
        "content_revision": InterviewNote.content_revision + 1,
        "updated_at": func.current_timestamp(),
    }
'''
    )
    assert _has_registered_migration(exact)
    assert _has_self_committing_story_call(exact)
    assert _proposal_drives_practice(exact)
    assert _has_note_revision_helper(exact)

    atomic_raw_migration = ast.parse(
        '''
cursor.execute(
    "INSERT INTO schema_migrations(version,description) VALUES (?,?)",
    ("0029_review_to_readiness_feedback", "approved"),
)
'''
    )
    assert _has_registered_migration(atomic_raw_migration)


def test_application_event_delete_owner_detector_rejects_direct_sql_and_orm_paths() -> None:
    direct = ast.parse(
        "def delete_event(session):\n    session.execute(delete(ApplicationEvent))\n"
    )
    orm = ast.parse(
        "def delete_event(session, event: ApplicationEvent):\n"
        "    session.delete(event)\n"
    )
    loaded_row = ast.parse(
        "def delete_event(session):\n"
        "    row = session.get(ApplicationEvent, 1)\n"
        "    session.delete(row)\n"
    )
    assigned_model_alias = ast.parse(
        "EventRow = ApplicationEvent\n"
        "def delete_event(session):\n"
        "    session.execute(delete(EventRow))\n"
    )
    table_delete = ast.parse(
        "def delete_event(session):\n"
        "    session.execute(ApplicationEvent.__table__.delete())\n"
    )
    query_delete = ast.parse(
        "def delete_event(session):\n"
        "    session.query(ApplicationEvent).filter_by(id=1).delete()\n"
    )
    execute_scalar_delete = ast.parse(
        "def delete_event(session):\n"
        "    row = session.execute(select(ApplicationEvent)).scalar_one()\n"
        "    session.delete(row)\n"
    )
    scalars_first_delete = ast.parse(
        "def delete_event(session):\n"
        "    row = session.scalars(select(ApplicationEvent)).first()\n"
        "    session.delete(row)\n"
    )
    split_result_delete = ast.parse(
        "def delete_event(session):\n"
        "    result = session.execute(select(ApplicationEvent))\n"
        "    row = result.scalar_one()\n"
        "    session.delete(row)\n"
    )
    execute_scalars_delete = ast.parse(
        "def delete_event(session):\n"
        "    row = session.execute(select(ApplicationEvent)).scalars().first()\n"
        "    session.delete(row)\n"
    )
    split_execute_scalars_delete = ast.parse(
        "def delete_event(session):\n"
        "    result = session.execute(select(ApplicationEvent))\n"
        "    rows = result.scalars()\n"
        "    row = rows.first()\n"
        "    session.delete(row)\n"
    )
    result_scalar_delete = ast.parse(
        "def delete_event(session):\n"
        "    row = session.execute(select(ApplicationEvent)).scalar()\n"
        "    session.delete(row)\n"
    )
    split_result_scalar_delete = ast.parse(
        "def delete_event(session):\n"
        "    result = session.execute(select(ApplicationEvent))\n"
        "    row = result.scalar()\n"
        "    session.delete(row)\n"
    )
    result_scalars_all_delete = ast.parse(
        "def delete_event(session):\n"
        "    rows = session.execute(select(ApplicationEvent)).scalars().all()\n"
        "    for row in rows:\n"
        "        session.delete(row)\n"
    )
    result_indexed_scalars_delete = ast.parse(
        "def delete_event(session):\n"
        "    rows = session.execute(select(ApplicationEvent)).scalars(0).all()\n"
        "    for row in rows:\n"
        "        session.delete(row)\n"
    )
    split_result_indexed_scalars_delete = ast.parse(
        "def delete_event(session):\n"
        "    result = session.execute(select(ApplicationEvent))\n"
        "    scalar_rows = result.scalars(0)\n"
        "    rows = scalar_rows.all()\n"
        "    for row in rows:\n"
        "        session.delete(row)\n"
    )
    raw_text_delete = ast.parse(
        "def delete_event(connection):\n"
        "    connection.exec_driver_sql('DELETE FROM application_events WHERE id = 1')\n"
    )
    quoted_raw_text_delete = ast.parse(
        "def delete_event(connection):\n"
        "    connection.exec_driver_sql('DELETE FROM \"application_events\" WHERE id = 1')\n"
    )
    owner = ast.parse(
        "def _delete_application_event_owned(session):\n"
        "    session.execute(delete(ApplicationEvent))\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"
    approved = ROOT / "src" / "offerpilot" / "repositories" / "application_events.py"

    assert _application_event_delete_violations(arbitrary, direct)
    assert _application_event_delete_violations(arbitrary, orm)
    assert _application_event_delete_violations(arbitrary, loaded_row)
    assert _application_event_delete_violations(arbitrary, assigned_model_alias)
    assert _application_event_delete_violations(arbitrary, table_delete)
    assert _application_event_delete_violations(arbitrary, query_delete)
    assert _application_event_delete_violations(arbitrary, execute_scalar_delete)
    assert _application_event_delete_violations(arbitrary, scalars_first_delete)
    assert _application_event_delete_violations(arbitrary, split_result_delete)
    assert _application_event_delete_violations(arbitrary, execute_scalars_delete)
    assert _application_event_delete_violations(arbitrary, split_execute_scalars_delete)
    assert _application_event_delete_violations(arbitrary, result_scalar_delete)
    assert _application_event_delete_violations(arbitrary, split_result_scalar_delete)
    assert _application_event_delete_violations(arbitrary, result_scalars_all_delete)
    assert _application_event_delete_violations(arbitrary, result_indexed_scalars_delete)
    assert _application_event_delete_violations(
        arbitrary,
        split_result_indexed_scalars_delete,
    )
    assert _application_event_delete_violations(arbitrary, raw_text_delete)
    assert _application_event_delete_violations(arbitrary, quoted_raw_text_delete)
    assert _application_event_delete_violations(approved, owner) == []


def test_application_event_delete_owner_detector_keeps_lineage_in_lexical_scope() -> None:
    cross_function_row = ast.parse(
        "def load_event(session):\n"
        "    row = session.get(ApplicationEvent, 1)\n"
        "    return row\n"
        "def delete_untyped_row(session, row):\n"
        "    session.delete(row)\n"
    )
    unrelated_event_parameter = ast.parse(
        "def delete_unrelated(session, event):\n"
        "    session.delete(event)\n"
    )
    interview_note_row = ast.parse(
        "def delete_note(session):\n"
        "    row = session.get(InterviewNote, 1)\n"
        "    session.delete(row)\n"
    )
    unrelated_scalar_results = ast.parse(
        "def delete_note(session):\n"
        "    first = session.scalars(select(InterviewNote)).first()\n"
        "    session.delete(first)\n"
        "    result = session.execute(select(InterviewNote))\n"
        "    second = result.scalar_one()\n"
        "    session.delete(second)\n"
        "    third = session.execute(select(InterviewNote)).scalars().first()\n"
        "    session.delete(third)\n"
        "    other_result = session.execute(select(InterviewNote))\n"
        "    rows = other_result.scalars()\n"
        "    fourth = rows.first()\n"
        "    session.delete(fourth)\n"
        "    fifth = session.execute(select(InterviewNote)).scalar()\n"
        "    session.delete(fifth)\n"
        "    split = session.execute(select(InterviewNote))\n"
        "    sixth = split.scalar()\n"
        "    session.delete(sixth)\n"
        "    all_rows = session.execute(select(InterviewNote)).scalars().all()\n"
        "    for row in all_rows:\n"
        "        session.delete(row)\n"
        "    indexed = session.execute(select(InterviewNote))\n"
        "    scalar_rows = indexed.scalars(0)\n"
        "    for row in scalar_rows.all():\n"
        "        session.delete(row)\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"

    assert _application_event_delete_violations(arbitrary, cross_function_row) == []
    assert _application_event_delete_violations(arbitrary, unrelated_event_parameter) == []
    assert _application_event_delete_violations(arbitrary, interview_note_row) == []
    assert _application_event_delete_violations(arbitrary, unrelated_scalar_results) == []


def test_interview_note_mutation_detector_requires_revisioned_owners() -> None:
    direct_content_assignment = ast.parse(
        "def mutate(session):\n"
        "    row = session.get(InterviewNote, 1)\n"
        "    row.questions = 'changed'\n"
    )
    direct_binding_assignment = ast.parse(
        "def mutate(session, row: InterviewNote):\n"
        "    row.application_event_id = None\n"
    )
    direct_bulk_update = ast.parse(
        "def mutate(session):\n"
        "    session.execute(update(InterviewNote).values(questions='changed'))\n"
    )
    table_update = ast.parse(
        "def mutate(session):\n"
        "    session.execute(InterviewNote.__table__.update().values(application_id=1))\n"
    )
    query_update = ast.parse(
        "def mutate(session):\n"
        "    session.query(InterviewNote).update({'questions': 'changed'})\n"
    )
    scalars_one_assignment = ast.parse(
        "def mutate(session):\n"
        "    row = session.scalars(select(InterviewNote)).one()\n"
        "    row.questions = 'changed'\n"
    )
    scalar_iteration_assignment = ast.parse(
        "def mutate(session):\n"
        "    rows = session.scalars(select(InterviewNote))\n"
        "    for row in rows:\n"
        "        row.questions = 'changed'\n"
    )
    execute_scalars_assignment = ast.parse(
        "def mutate(session):\n"
        "    row = session.execute(select(InterviewNote)).scalars().one()\n"
        "    row.questions = 'changed'\n"
    )
    split_execute_scalars_assignment = ast.parse(
        "def mutate(session):\n"
        "    result = session.execute(select(InterviewNote))\n"
        "    rows = result.scalars()\n"
        "    for row in rows:\n"
        "        row.questions = 'changed'\n"
    )
    result_scalar_assignment = ast.parse(
        "def mutate(session):\n"
        "    row = session.execute(select(InterviewNote)).scalar()\n"
        "    row.questions = 'changed'\n"
    )
    split_result_scalar_assignment = ast.parse(
        "def mutate(session):\n"
        "    result = session.execute(select(InterviewNote))\n"
        "    row = result.scalar()\n"
        "    row.questions = 'changed'\n"
    )
    result_scalars_all_assignment = ast.parse(
        "def mutate(session):\n"
        "    rows = session.execute(select(InterviewNote)).scalars().all()\n"
        "    for row in rows:\n"
        "        row.questions = 'changed'\n"
    )
    result_indexed_scalars_assignment = ast.parse(
        "def mutate(session):\n"
        "    rows = session.execute(select(InterviewNote)).scalars(0).all()\n"
        "    for row in rows:\n"
        "        row.questions = 'changed'\n"
    )
    split_result_indexed_scalars_assignment = ast.parse(
        "def mutate(session):\n"
        "    result = session.execute(select(InterviewNote))\n"
        "    scalar_rows = result.scalars(0)\n"
        "    rows = scalar_rows.all()\n"
        "    for row in rows:\n"
        "        row.questions = 'changed'\n"
    )
    quoted_raw_update = ast.parse(
        "def mutate(connection):\n"
        "    connection.exec_driver_sql('UPDATE \"interview_notes\" SET questions = 1')\n"
    )
    approved_owner = ast.parse(
        "def update(session):\n"
        "    session.execute(\n"
        "        update(InterviewNote).values(**_revisioned_note_values({'questions': 'changed'}))\n"
        "    )\n"
    )
    approved_scoped_owner = ast.parse(
        "def update_note_scoped(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    statement = update(InterviewNote).where(InterviewNote.id == 1)\n"
        "    statement = statement.values(**values).returning(InterviewNote)\n"
        "    session.scalars(statement)\n"
    )
    approved_event_owner = ast.parse(
        "def _delete_application_event_owned(session):\n"
        "    session.execute(\n"
        "        update(InterviewNote).values(\n"
        "            **_revisioned_note_values({'application_event_id': None})\n"
        "        )\n"
        "    )\n"
    )
    unrevisioned_owner = ast.parse(
        "def update(session):\n"
        "    session.execute(update(InterviewNote).values(questions='changed'))\n"
    )
    mixed_owner = ast.parse(
        "def update(session):\n"
        "    session.execute(\n"
        "        update(InterviewNote).values(**_revisioned_note_values({'questions': 'ok'}))\n"
        "    )\n"
        "    session.execute(update(InterviewNote).values(questions='bypass'))\n"
    )
    reassigned_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'initial'})\n"
        "    values = {'questions': 'bypass'}\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    straight_line_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    reassigned_revisioned_statement = ast.parse(
        "def update(session):\n"
        "    statement = update(InterviewNote).values(\n"
        "        **_revisioned_note_values({'questions': 'initial'})\n"
        "    )\n"
        "    statement = update(InterviewNote).values(questions='bypass')\n"
        "    session.execute(statement)\n"
    )
    conditional_revision_values = ast.parse(
        "def update(session, condition):\n"
        "    values = {'questions': 'bypass'}\n"
        "    if condition:\n"
        "        values = _revisioned_note_values(values)\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    mutated_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    values['content_revision'] = 1\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    method_mutated_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    values.update({'content_revision': 1})\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    unbound_method_mutated_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    dict.__setitem__(values, 'content_revision', 1)\n"
        "    statement = update(InterviewNote).values(**values)\n"
        "    session.execute(statement)\n"
    )
    sink_mutated_revision_values = ast.parse(
        "def update(session):\n"
        "    values = _revisioned_note_values({'questions': 'changed'})\n"
        "    statement = update(InterviewNote).values(\n"
        "        questions=values.clear(),\n"
        "        **values,\n"
        "    )\n"
        "    session.execute(statement)\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"
    notes_owner = ROOT / "src" / "offerpilot" / "repositories" / "notes.py"
    events_owner = ROOT / "src" / "offerpilot" / "repositories" / "application_events.py"

    assert _interview_note_mutation_violations(arbitrary, direct_content_assignment)
    assert _interview_note_mutation_violations(arbitrary, direct_binding_assignment)
    assert _interview_note_mutation_violations(arbitrary, direct_bulk_update)
    assert _interview_note_mutation_violations(arbitrary, table_update)
    assert _interview_note_mutation_violations(arbitrary, query_update)
    assert _interview_note_mutation_violations(arbitrary, scalars_one_assignment)
    assert _interview_note_mutation_violations(arbitrary, scalar_iteration_assignment)
    assert _interview_note_mutation_violations(arbitrary, execute_scalars_assignment)
    assert _interview_note_mutation_violations(
        arbitrary,
        split_execute_scalars_assignment,
    )
    assert _interview_note_mutation_violations(arbitrary, result_scalar_assignment)
    assert _interview_note_mutation_violations(arbitrary, split_result_scalar_assignment)
    assert _interview_note_mutation_violations(arbitrary, result_scalars_all_assignment)
    assert _interview_note_mutation_violations(
        arbitrary,
        result_indexed_scalars_assignment,
    )
    assert _interview_note_mutation_violations(
        arbitrary,
        split_result_indexed_scalars_assignment,
    )
    assert _interview_note_mutation_violations(arbitrary, quoted_raw_update)
    assert _interview_note_mutation_violations(notes_owner, approved_owner) == []
    assert _interview_note_mutation_violations(notes_owner, approved_scoped_owner) == []
    assert _interview_note_mutation_violations(events_owner, approved_event_owner) == []
    assert _interview_note_mutation_violations(notes_owner, unrevisioned_owner)
    assert _interview_note_mutation_violations(notes_owner, mixed_owner)
    assert _interview_note_mutation_violations(notes_owner, reassigned_revision_values)
    assert _interview_note_mutation_violations(
        notes_owner,
        reassigned_revisioned_statement,
    )
    assert _interview_note_mutation_violations(
        notes_owner,
        conditional_revision_values,
    )
    assert _interview_note_mutation_violations(notes_owner, mutated_revision_values)
    assert _interview_note_mutation_violations(
        notes_owner,
        method_mutated_revision_values,
    )
    assert _interview_note_mutation_violations(
        notes_owner,
        unbound_method_mutated_revision_values,
    )
    assert _interview_note_mutation_violations(
        notes_owner,
        sink_mutated_revision_values,
    )
    assert (
        _interview_note_mutation_violations(
            notes_owner,
            straight_line_revision_values,
        )
        == []
    )


def test_interview_note_mutation_detector_avoids_read_and_unrelated_writes() -> None:
    safe = ast.parse(
        "def inspect(session, other):\n"
        "    row = session.get(InterviewNote, 1)\n"
        "    observed = row.questions\n"
        "    other.questions = 'unrelated'\n"
        "    created = InterviewNote(questions='new')\n"
        "    for event in session.scalars(select(ApplicationEvent)):\n"
        "        event.questions = 'unrelated'\n"
        "    event = session.execute(select(ApplicationEvent)).scalars().one()\n"
        "    event.questions = 'unrelated'\n"
        "    result = session.execute(select(ApplicationEvent))\n"
        "    rows = result.scalars()\n"
        "    for event in rows:\n"
        "        event.questions = 'unrelated'\n"
        "    scalar_event = session.execute(select(ApplicationEvent)).scalar()\n"
        "    scalar_event.questions = 'unrelated'\n"
        "    split = session.execute(select(ApplicationEvent))\n"
        "    split_event = split.scalar()\n"
        "    split_event.questions = 'unrelated'\n"
        "    all_events = session.execute(select(ApplicationEvent)).scalars().all()\n"
        "    for event in all_events:\n"
        "        event.questions = 'unrelated'\n"
        "    indexed = session.execute(select(ApplicationEvent))\n"
        "    scalar_events = indexed.scalars(0)\n"
        "    for event in scalar_events.all():\n"
        "        event.questions = 'unrelated'\n"
        "    return observed, created\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"

    assert _interview_note_mutation_violations(arbitrary, safe) == []


def test_application_event_hard_delete_has_one_production_owner() -> None:
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        tree = _parse(path)
        if tree is not None:
            violations.extend(_application_event_delete_violations(path, tree))
    assert violations == []


def test_interview_note_content_mutations_have_revisioned_production_owners() -> None:
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        tree = _parse(path)
        if tree is not None:
            violations.extend(_interview_note_mutation_violations(path, tree))
    assert violations == []


def test_product_action_gate_requires_all_modules_and_independent_catalogs() -> None:
    contracts = ast.parse(
        '''
PRODUCT_ACTION_NAMES = ("confirm_interview_story", "save_review_readiness_signal")
PRODUCT_ACTION_COMPENSATION_NAMES = (
    "undo:confirm_interview_story", "undo:save_review_readiness_signal"
)
'''
    )
    catalog = ast.parse(
        '''
class ProductActionCatalogV1:
    def names(self):
        return PRODUCT_ACTION_NAMES
class ProductActionCompensationCatalogV1:
    def names(self):
        return PRODUCT_ACTION_COMPENSATION_NAMES
'''
    )
    complete = {name: ast.parse("") for name in PRODUCT_ACTION_MODULES}
    complete["contracts.py"] = contracts
    complete["catalog.py"] = catalog
    assert _product_action_violations(complete) == []

    strings_only = dict(complete)
    strings_only["catalog.py"] = ast.parse(
        '''
PRIMARY = "ProductActionCatalogV1 confirm_interview_story save_review_readiness_signal"
COMPENSATION = "ProductActionCompensationCatalogV1 undo:confirm_interview_story"
'''
    )
    assert _product_action_violations(strings_only) == [
        "ledger:missing-product-action",
        "ledger:missing-product-action-compensation",
    ]

    missing_module = dict(complete)
    missing_module["coordinator.py"] = None
    assert _product_action_violations(missing_module) == [
        "ledger:missing-product-action",
        "ledger:missing-product-action-compensation",
    ]


def test_practice_gate_stays_red_when_only_start_is_replaced() -> None:
    legacy_scan = ast.parse(
        '''
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        proposals = session.scalars(select(InterviewReviewProposal))
        return [_proposal_focuses(proposal) for proposal in proposals]
'''
    )
    assert not _proposal_drives_practice_start(legacy_scan)
    assert _proposal_drives_practice_recommendations(legacy_scan)
    assert _proposal_drives_practice(legacy_scan)

    inline_focus_scan = ast.parse(
        '''
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        proposals = session.scalars(select(InterviewReviewProposal))
        return [proposal.practice_focuses for proposal in proposals]
'''
    )
    assert _proposal_drives_practice(inline_focus_scan)

    renamed_focus_helper = ast.parse(
        '''
def _review_weaknesses(session):
    proposals = session.scalars(select(InterviewReviewProposal))
    return [proposal.practice_focuses for proposal in proposals]
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        return _review_weaknesses(self._session_factory())
'''
    )
    assert _proposal_drives_practice(renamed_focus_helper)

    historical_plans_only = ast.parse(
        '''
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        return session.scalars(select(AdaptivePracticePlan))
'''
    )
    assert not _proposal_drives_practice(historical_plans_only)

    safe_plan_helper = ast.parse(
        '''
def _plan_focuses(plan):
    return plan.focuses
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        plans = session.scalars(select(AdaptivePracticePlan))
        return [_plan_focuses(plan) for plan in plans]
'''
    )
    assert not _proposal_drives_practice(safe_plan_helper)

    annotation_only = ast.parse(
        '''
def _format_plan(plan: InterviewReviewProposal):
    return plan.focuses
class AdaptivePracticeRepository:
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
    def list_recommendations(self):
        plans = session.scalars(select(AdaptivePracticePlan))
        return [_format_plan(plan) for plan in plans]
'''
    )
    assert not _proposal_drives_practice(annotation_only)


def test_practice_gate_follows_reachable_module_helpers_without_scanning_unrelated_helpers() -> None:
    extracted_legacy_scan = ast.parse(
        '''
def _legacy_recommendations(session):
    proposals = session.scalars(select(InterviewReviewProposal))
    return [_proposal_focuses(proposal) for proposal in proposals]
class AdaptivePracticeRepository:
    def list_recommendations(self):
        return _legacy_recommendations(self._session_factory())
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
'''
    )
    assert _proposal_drives_practice(extracted_legacy_scan)

    unreachable_legacy_helper = ast.parse(
        '''
def _unused_legacy_recommendations(session):
    proposals = session.scalars(select(InterviewReviewProposal))
    return [_proposal_focuses(proposal) for proposal in proposals]
class AdaptivePracticeRepository:
    def list_recommendations(self):
        return session.scalars(select(AdaptivePracticePlan))
    def start(self, readiness_signal_version_id, target_application_event_id):
        return readiness_signal_version_id, target_application_event_id
'''
    )
    assert not _proposal_drives_practice(unreachable_legacy_helper)


def test_product_action_http_gate_requires_raw_request_before_body_normalization() -> None:
    safe_get = ast.parse(
        '''
@router.get("/api/product-actions/{operation_id}")
async def get_product_action(operation_id: str):
    return service.load(operation_id)

@router.get("/api/interview-notes/{note_id}/readiness-focus-actions/{operation_id}")
async def get_readiness_action(note_id: int, operation_id: str):
    return service.load_owner(note_id, operation_id)

@router.get("/api/applications/{application_id}/product-actions/{operation_id}/rejection-control")
async def get_rejection_control(application_id: int, operation_id: str):
    return service.load_rejection(application_id, operation_id)
'''
    )
    assert _unsafe_product_action_http_bodies(safe_get) == []

    safe = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(safe) == []

    safe_bound_body = ast.parse(
        '''
@router.patch("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    raw_body = await request.body()
    payload = decode_product_action_request_v1(raw_body, contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(safe_bound_body) == []

    coerced_dict = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
def decide(operation_id: str, payload: dict = Body(...)):
    return decode_product_action_request_v1(payload, contract="decision")
'''
    )
    assert _unsafe_product_action_http_bodies(coerced_dict) == [
        "ui:unsafe-product-action-body:decide"
    ]

    raw_plus_coerced_model = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request, payload: DecisionBody = Body(...)):
    raw = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, raw, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(raw_plus_coerced_model) == [
        "ui:unsafe-product-action-body:decide"
    ]

    missing_decoder = ast.parse(
        '''
@router.post("/api/interview-notes/{note_id}/readiness-focus-actions")
async def propose(note_id: int, request: Request):
    return service.propose(await request.json())
'''
    )
    assert _unsafe_product_action_http_bodies(missing_decoder) == [
        "ui:unsafe-product-action-body:propose"
    ]

    forged_decoder_input = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(b"{}", contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(forged_decoder_input) == [
        "ui:unsafe-product-action-body:decide"
    ]

    parsed_before_raw_decode = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    await request.json()
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(parsed_before_raw_decode) == [
        "ui:unsafe-product-action-body:decide"
    ]

    business_before_raw_decode = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    operation_id = normalize_operation_id(operation_id)
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(business_before_raw_decode) == [
        "ui:unsafe-product-action-body:decide"
    ]

    ignored_decoder_result = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, {})
'''
    )
    assert _unsafe_product_action_http_bodies(ignored_decoder_result) == [
        "ui:unsafe-product-action-body:decide"
    ]

    overwritten_raw_body = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    raw_body = await request.body()
    raw_body = b"{}"
    payload = decode_product_action_request_v1(raw_body, contract="decision")
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(overwritten_raw_body) == [
        "ui:unsafe-product-action-body:decide"
    ]

    overwritten_decoder_result = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    payload = {}
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(overwritten_decoder_result) == [
        "ui:unsafe-product-action-body:decide"
    ]

    safe_controlled_derivation = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    decision = payload["decision"]
    return service.decide(operation_id, decision)
'''
    )
    assert _unsafe_product_action_http_bodies(safe_controlled_derivation) == []

    safe_service_result_then_return = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    response = service.decide(operation_id, payload)
    return response
'''
    )
    assert _unsafe_product_action_http_bodies(safe_service_result_then_return) == []

    audit_then_secondary_parse = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    raw_body = await request.body()
    payload = decode_product_action_request_v1(raw_body, contract="decision")
    audit(payload)
    normalized = json.loads(raw_body)
    return service.decide(operation_id, normalized)
'''
    )
    assert _unsafe_product_action_http_bodies(audit_then_secondary_parse) == [
        "ui:unsafe-product-action-body:decide"
    ]

    aliased_raw_then_secondary_parse = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    raw_body = await request.body()
    raw_alias = raw_body
    payload = decode_product_action_request_v1(raw_body, contract="decision")
    normalized = json.loads(raw_alias)
    return service.decide(operation_id, normalized, decoded=payload)
'''
    )
    assert _unsafe_product_action_http_bodies(aliased_raw_then_secondary_parse) == [
        "ui:unsafe-product-action-body:decide"
    ]

    parsed_request_with_decoder_bypass = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    parsed = parse_request(request)
    return service.decide(operation_id, parsed, decoded=payload)
'''
    )
    assert _unsafe_product_action_http_bodies(parsed_request_with_decoder_bypass) == [
        "ui:unsafe-product-action-body:decide"
    ]

    literal_body_with_decoder_bypass = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    return service.decide(operation_id, {"decision": "approve"}, decoded=payload)
'''
    )
    assert _unsafe_product_action_http_bodies(literal_body_with_decoder_bypass) == [
        "ui:unsafe-product-action-body:decide"
    ]

    mixed_decoder_and_uncontrolled_derivation = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    parsed = parse_request(request)
    normalized = payload or parsed
    return service.decide(operation_id, normalized)
'''
    )
    assert _unsafe_product_action_http_bodies(
        mixed_decoder_and_uncontrolled_derivation
    ) == ["ui:unsafe-product-action-body:decide"]

    audit_result_is_not_a_business_sink = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    audit_result = audit(payload)
    return service.decide(operation_id, {})
'''
    )
    assert _unsafe_product_action_http_bodies(audit_result_is_not_a_business_sink) == [
        "ui:unsafe-product-action-body:decide"
    ]

    mutated_decoder_result = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    payload.clear()
    return service.decide(operation_id, payload)
'''
    )
    assert _unsafe_product_action_http_bodies(mutated_decoder_result) == [
        "ui:unsafe-product-action-body:decide"
    ]

    mutated_derived_result = ast.parse(
        '''
@router.post("/api/product-actions/{operation_id}/decisions")
async def decide(operation_id: str, request: Request):
    payload = decode_product_action_request_v1(await request.body(), contract="decision")
    decision = payload["decision"]
    decision = normalize_decision(decision)
    return service.decide(operation_id, decision)
'''
    )
    assert _unsafe_product_action_http_bodies(mutated_derived_result) == [
        "ui:unsafe-product-action-body:decide"
    ]


def test_product_action_parent_ast_gate_rejects_orm_core_and_raw_sql_writers() -> None:
    assert _writes_raw_product_action_parent(
        ast.parse('WriteOperation(id="x", adapter_kind="product_action")')
    )
    assert _writes_raw_product_action_parent(
        ast.parse(
            'insert(WriteOperation).values(id="x", adapter_kind="product_action")'
        )
    )
    assert _writes_raw_product_action_parent(
        ast.parse(
            '''
session.execute(
    text("INSERT INTO write_operations(id,adapter_kind) VALUES (:id,'product_action')")
)
'''
        )
    )
    assert not _writes_raw_product_action_parent(
        ast.parse('WriteOperation(id="x", adapter_kind="typed")')
    )


def test_review_to_readiness_production_cutover_gate() -> None:
    # Intentional RED until Tasks 1-8 remove every named production gap.
    assert _source_violations() == []
