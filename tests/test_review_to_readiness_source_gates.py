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

        def discard(self, names: set[str]) -> None:
            self.models.difference_update(names)
            self.delete_factories.difference_update(names)
            self.tables.difference_update(names)
            self.queries.difference_update(names)
            self.rows.difference_update(names)

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

        def is_event_row_source(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in lineage.rows
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                return False
            if node.func.attr == "get":
                return bool(node.args) and is_event_model(node.args[0])
            if node.func.attr == "scalar":
                return bool(node.args) and select_targets_event(node.args[0])
            if node.func.attr in {"scalar_one", "scalar_one_or_none"}:
                return select_targets_event(node.func.value)
            if node.func.attr in {"first", "one", "one_or_none"}:
                return query_targets_event(node.func.value)
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
                )
                for matches, known_names in target_sets:
                    if matches and not names.issubset(known_names):
                        known_names.update(names)
                        changed = True
            for candidate in nodes:
                if not isinstance(candidate, (ast.For, ast.AsyncFor)):
                    continue
                iterator = candidate.iter
                is_event_scalar_iter = (
                    isinstance(iterator, ast.Call)
                    and isinstance(iterator.func, ast.Attribute)
                    and iterator.func.attr == "scalars"
                    and bool(iterator.args)
                    and select_targets_event(iterator.args[0])
                )
                if not is_event_scalar_iter:
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


def _unsafe_product_action_http_bodies(tree: ast.AST | None) -> list[str]:
    """Reject normalization/coercion before the duplicate-aware raw decoder."""

    if tree is None:
        return []
    violations: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator_text = " ".join(ast.unparse(item) for item in node.decorator_list)
        if not any(marker in decorator_text for marker in PRODUCT_ACTION_ROUTE_MARKERS):
            continue
        decorator_casefold = decorator_text.casefold()
        if ".post(" not in decorator_casefold and ".patch(" not in decorator_casefold:
            continue
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
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
            and (
                isinstance(child.func, ast.Name)
                and child.func.id == RAW_DECODER_NAME
                or isinstance(child.func, ast.Attribute)
                and child.func.attr == RAW_DECODER_NAME
            )
        ]
        if body_or_model_parameter or not has_raw_request or len(decoder_calls) != 1:
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

        def discard(self, names: set[str]) -> None:
            self.models.difference_update(names)
            self.tables.difference_update(names)
            self.queries.difference_update(names)
            self.rows.difference_update(names)

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
                return bool(node.args) and select_targets_note(node.args[0])
            if node.func.attr in {"scalar_one", "scalar_one_or_none"}:
                return select_targets_note(node.func.value)
            if node.func.attr in {"first", "one", "one_or_none"}:
                return query_targets_note(node.func.value)
            return False

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
                )
                for matches, known_names in target_sets:
                    if matches and not names.issubset(known_names):
                        known_names.update(names)
                        changed = True

        owner = (
            scope.name
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            else ""
        )
        uses_revision_helper = any(
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == "_revisioned_note_values"
            for candidate in nodes
        )
        approved_sql_owner = uses_revision_helper and (
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
                direct_update = (
                    isinstance(candidate.func, (ast.Name, ast.Attribute))
                    and (
                        (isinstance(candidate.func, ast.Name) and candidate.func.id == "update")
                        or (
                            isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr == "update"
                        )
                    )
                    and bool(candidate.args)
                    and is_note_model(candidate.args[0])
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
                    (direct_update and not approved_sql_owner)
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
    raw_text_delete = ast.parse(
        "def delete_event(connection):\n"
        "    connection.exec_driver_sql('DELETE FROM application_events WHERE id = 1')\n"
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
    assert _application_event_delete_violations(arbitrary, raw_text_delete)
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
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"

    assert _application_event_delete_violations(arbitrary, cross_function_row) == []
    assert _application_event_delete_violations(arbitrary, unrelated_event_parameter) == []
    assert _application_event_delete_violations(arbitrary, interview_note_row) == []


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
    approved_owner = ast.parse(
        "def update(session):\n"
        "    session.execute(\n"
        "        update(InterviewNote).values(**_revisioned_note_values({'questions': 'changed'}))\n"
        "    )\n"
    )
    unrevisioned_owner = ast.parse(
        "def update(session):\n"
        "    session.execute(update(InterviewNote).values(questions='changed'))\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"
    notes_owner = ROOT / "src" / "offerpilot" / "repositories" / "notes.py"

    assert _interview_note_mutation_violations(arbitrary, direct_content_assignment)
    assert _interview_note_mutation_violations(arbitrary, direct_binding_assignment)
    assert _interview_note_mutation_violations(arbitrary, direct_bulk_update)
    assert _interview_note_mutation_violations(arbitrary, table_update)
    assert _interview_note_mutation_violations(arbitrary, query_update)
    assert _interview_note_mutation_violations(notes_owner, approved_owner) == []
    assert _interview_note_mutation_violations(notes_owner, unrevisioned_owner)


def test_interview_note_mutation_detector_avoids_read_and_unrelated_writes() -> None:
    safe = ast.parse(
        "def inspect(session, other):\n"
        "    row = session.get(InterviewNote, 1)\n"
        "    observed = row.questions\n"
        "    other.questions = 'unrelated'\n"
        "    created = InterviewNote(questions='new')\n"
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
