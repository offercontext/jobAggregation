from __future__ import annotations

import ast
import itertools
import math
from pathlib import Path
import posixpath
import re


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


def _task12_callable_return_bindings(
    *scopes: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, str], dict[str, ast.Dict]]:
    strings: dict[str, str] = {}
    mappings: dict[str, ast.Dict] = {}

    def string_value(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return strings.get(node.id) if isinstance(node, ast.Name) else None

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

    def static_key(value: ast.AST | None) -> str | None:
        direct = _literal_string(value) if value is not None else None
        if direct is not None:
            return direct
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
            if _literal_string(item_key) == key
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
        selected_key = static_key(node.args[0])
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
        selected_key = static_key(node.slice)
        mapping = mapping_value(node.value)
        if selected_key is not None and mapping is not None:
            return _task12_callable_return_options(
                mapping,
                key=selected_key,
                strings=known_strings,
                mappings=known_mappings,
            )
    return (node,)


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


_TASK12_PRIVACY_CANARY = "review-readiness-private-canary-8b93"
_TASK12_HTTP_UNDO_MANIFEST = {
    ("post", "/api/chat/undo-last-write"),
    (
        "post",
        "/api/applications/{application_id}/readiness-signals/{signal_id}/undo",
    ),
    ("post", "/api/interview-stories/{story_id}/product-action-undo"),
}
_TASK12_PROVIDER_PATHS = (
    "agent_runtime/",
    "ai/",
    "chat_transport.py",
    "context_projector/",
    "pilot_runtime/",
)
_TASK12_PRODUCT_ACTION_EXECUTION_PATHS = (
    "product_actions/",
    "review_readiness/repository.py",
    "repositories/interview_stories.py",
)


def _task12_tree(name: str, source: str) -> ast.Module:
    return ast.parse(source, filename=name)


def _task12_terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _task12_import_aliases(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local = item.asname or item.name.split(".", 1)[0]
                aliases[local] = item.name
                modules.add(item.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules.add(module)
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = (
                        f"{module}.{item.name}" if module else item.name
                    )
    return aliases, modules


def _task12_module_import_aliases(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    return _task12_import_aliases(
        ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ],
            type_ignores=[],
        )
    )


def _task12_scope_aliases(
    module_aliases: dict[str, str],
    scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> dict[str, str]:
    aliases = dict(module_aliases)
    positional = (*scope.args.posonlyargs, *scope.args.args)
    default_by_name = {
        parameter.arg: default
        for parameter, default in zip(
            positional[-len(scope.args.defaults) :],
            scope.args.defaults,
            strict=False,
        )
    }
    default_by_name.update(
        {
            parameter.arg: default
            for parameter, default in zip(
                scope.args.kwonlyargs, scope.args.kw_defaults, strict=True
            )
            if default is not None
        }
    )
    for parameter in (*positional, *scope.args.kwonlyargs):
        default = default_by_name.get(parameter.arg)
        resolved = (
            _task12_resolved_name(default, aliases)
            if isinstance(default, (ast.Name, ast.Attribute))
            else None
        )
        if resolved is None:
            aliases.pop(parameter.arg, None)
        else:
            aliases[parameter.arg] = resolved
    if isinstance(scope, ast.Lambda):
        return aliases
    nodes: list[ast.AST] = list(scope.body)
    scoped: list[ast.AST] = []
    while nodes:
        node = nodes.pop()
        scoped.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes.extend(ast.iter_child_nodes(node))
    local_tree = ast.Module(
        body=[node for node in scoped if isinstance(node, (ast.Import, ast.ImportFrom))],
        type_ignores=[],
    )
    aliases.update(_task12_import_aliases(local_tree)[0])

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
            resolved = _task12_resolved_name(node.value, aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and root(node.value) != target.id
                    and resolved is not None
                    and aliases.get(target.id) != resolved
                ):
                    aliases[target.id] = resolved
                    changed = True
        if not changed:
            break
    return aliases


def _task12_node_reachable(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> bool:
    child = node
    parent = parents.get(child)
    while parent is not None:
        if (
            isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(child, ast.stmt)
            and child in parent.body
        ):
            index = parent.body.index(child)
            if any(isinstance(item, (ast.Return, ast.Raise)) for item in parent.body[:index]):
                return False
        if (
            isinstance(parent, ast.If)
            and isinstance(parent.test, ast.Constant)
            and isinstance(parent.test.value, bool)
        ):
            if child in parent.body and not parent.test.value:
                return False
            if child in parent.orelse and parent.test.value:
                return False
        if isinstance(parent, ast.match_case):
            match_node = parents.get(parent)
            if isinstance(match_node, ast.Match) and isinstance(
                match_node.subject, ast.Constant
            ):
                selected: ast.match_case | None = None
                for case in match_node.cases:
                    pattern = case.pattern
                    matches = (
                        isinstance(pattern, ast.MatchAs)
                        and pattern.pattern is None
                    ) or (
                        isinstance(pattern, ast.MatchValue)
                        and isinstance(pattern.value, ast.Constant)
                        and pattern.value.value == match_node.subject.value
                    ) or (
                        isinstance(pattern, ast.MatchSingleton)
                        and pattern.value == match_node.subject.value
                    )
                    guard_allows = case.guard is None or (
                        isinstance(case.guard, ast.Constant)
                        and case.guard.value is True
                    )
                    if matches and guard_allows:
                        selected = case
                        break
                if selected is not None and parent is not selected:
                    return False
        child = parent
        parent = parents.get(parent)
    return True


def _task12_resolved_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _task12_resolved_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _task12_text(
    node: ast.AST,
    strings: dict[str, str],
    *,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id not in seen:
        return strings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _task12_text(node.left, strings, seen=seen)
        right = _task12_text(node.right, strings, seen=seen)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _task12_text(value.value, strings, seen=seen)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _task12_string_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for _ in range(8):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = (*node.args.posonlyargs, *node.args.args)
                for parameter, default in zip(
                    positional[-len(node.args.defaults) :],
                    node.args.defaults,
                    strict=False,
                ):
                    value = _task12_text(default, bindings)
                    if value is not None and bindings.get(parameter.arg) != value:
                        bindings[parameter.arg] = value
                        changed = True
                for parameter, default in zip(
                    node.args.kwonlyargs, node.args.kw_defaults, strict=True
                ):
                    if default is None:
                        continue
                    value = _task12_text(default, bindings)
                    if value is not None and bindings.get(parameter.arg) != value:
                        bindings[parameter.arg] = value
                        changed = True
            if isinstance(node, ast.Call):
                callee = functions.get(_task12_terminal(node.func) or "")
                if callee is not None:
                    positional = (*callee.args.posonlyargs, *callee.args.args)
                    for parameter, argument in zip(positional, node.args, strict=False):
                        value = _task12_text(argument, bindings)
                        if value is not None and bindings.get(parameter.arg) != value:
                            bindings[parameter.arg] = value
                            changed = True
                    keyword_parameters = {
                        parameter.arg
                        for parameter in (*positional, *callee.args.kwonlyargs)
                    }
                    for keyword in node.keywords:
                        if keyword.arg not in keyword_parameters:
                            continue
                        value = _task12_text(keyword.value, bindings)
                        if value is not None and bindings.get(keyword.arg) != value:
                            bindings[keyword.arg] = value
                            changed = True
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            value = _task12_text(node.value, bindings)
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != value:
                    bindings[target.id] = value
                    changed = True
        if not changed:
            break
    return bindings


def _task12_eval_strings(
    node: ast.AST,
    bindings: dict[str, set[str]],
    functions: dict[str, set[str]],
) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, ()))
    if isinstance(node, ast.Starred):
        return _task12_eval_strings(node.value, bindings, functions)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return {
            value for item in node.elts for value in _task12_eval_strings(item, bindings, functions)
        }
    if isinstance(node, ast.Dict):
        return {
            value
            for item in (*node.keys, *node.values)
            if item is not None
            for value in _task12_eval_strings(item, bindings, functions)
        }
    if isinstance(node, ast.Call):
        terminal = _task12_terminal(node.func)
        if terminal in functions:
            return set(functions[terminal])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _task12_eval_strings(node.left, bindings, functions)
        right = _task12_eval_strings(node.right, bindings, functions)
        if len(left) == len(right) == 1:
            return {next(iter(left)) + next(iter(right))}
        return left | right
    return set()


def _task12_value_bindings(
    tree: ast.Module,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    bindings: dict[str, set[str]] = {}
    functions: dict[str, set[str]] = {}
    for _ in range(12):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                values = {
                    value
                    for returned in ast.walk(node)
                    if isinstance(returned, ast.Return) and returned.value is not None
                    for value in _task12_eval_strings(returned.value, bindings, functions)
                }
                if values and functions.get(node.name) != values:
                    functions[node.name] = values
                    changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                values = _task12_eval_strings(node.value, bindings, functions)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Name)
                        and values
                        and bindings.get(target.id) != values
                    ):
                        bindings[target.id] = values
                        changed = True
        if not changed:
            break
    return bindings, functions


def _task12_provider_violations(name: str, tree: ast.Module) -> list[str]:
    if not any(name == prefix or name.startswith(prefix) for prefix in _TASK12_PROVIDER_PATHS):
        return []
    aliases, modules = _task12_import_aliases(tree)
    if any(
        module == "offerpilot.product_actions" or module.startswith("offerpilot.product_actions.")
        for module in modules
    ):
        return [f"provider:product-action-import:{name}"]

    bindings, functions = _task12_value_bindings(tree)
    registered: set[str] = set()
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    scoped_aliases: dict[int, dict[str, str]] = {}
    scoped_parameters: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if not _task12_node_reachable(node, parents):
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            owner = parents.get(owner)
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if id(owner) not in scoped_aliases:
                scoped_aliases[id(owner)] = _task12_scope_aliases(aliases, owner)
            node_aliases = scoped_aliases[id(owner)]
        else:
            node_aliases = aliases
        shadowed_parameters = (
            scoped_parameters.setdefault(
                id(owner),
                {
                    parameter.arg
                    for parameter in (
                        *owner.args.posonlyargs,
                        *owner.args.args,
                        *owner.args.kwonlyargs,
                    )
                },
            )
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            else set()
        )
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            registered.update(_task12_eval_strings(node.value, bindings, functions))
        elif isinstance(node, ast.Call):
            for value in (*node.args, *(item.value for item in node.keywords)):
                registered.update(_task12_eval_strings(value, bindings, functions))
        elif (
            isinstance(node, (ast.Name, ast.Attribute))
            and not (isinstance(node, ast.Name) and node.id in shadowed_parameters)
            and (
            (_task12_resolved_name(node, node_aliases) or "").rsplit(".", 1)[-1]
            in {*PRODUCT_ACTIONS, *PRODUCT_ACTION_COMPENSATIONS}
            )
        ):
            registered.add(
                (_task12_resolved_name(node, node_aliases) or "").rsplit(".", 1)[-1]
            )
    if set(PRODUCT_ACTIONS + PRODUCT_ACTION_COMPENSATIONS) & registered:
        return [f"provider:product-action-name:{name}"]
    return []


def _task12_product_action_domain_violations(
    name: str,
    tree: ast.Module,
    *,
    scope: ast.AST | None = None,
) -> list[str]:
    if not any(name.startswith(prefix) for prefix in _TASK12_PRODUCT_ACTION_EXECUTION_PATHS):
        return []
    if scope is None:
        scoped_functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        for function in scoped_functions:
            if _task12_product_action_domain_violations(
                name, tree, scope=function
            ):
                return [f"product-action:chat-journal-write:{name}"]
        scope = ast.Module(
            body=[
                node
                for node in tree.body
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            ],
            type_ignores=[],
        )
    module_aliases, _modules = _task12_module_import_aliases(tree)
    aliases = (
        _task12_scope_aliases(module_aliases, scope)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        else module_aliases
    )
    strings = _task12_string_bindings(tree)
    scan = scope or tree
    parents = {
        child: parent
        for parent in ast.walk(scan)
        for child in ast.iter_child_nodes(parent)
    }
    forbidden_models = {
        "AgentContextSnapshot",
        "AgentEvent",
        "AgentRun",
        "ChatMessage",
        "Conversation",
        "PendingAction",
        "PendingToolCall",
        "PreparedSurfaceManifestV2",
        "SurfaceManifestV2",
        "ToolMessage",
    }
    write_helpers = {
        "append_context_snapshot",
        "append_event",
        "append_message",
        "capture_context",
        "create_pending",
        "record_agent_run",
        "prepare_context_snapshot",
        "prepare_surface_manifest_v2",
        "set_pending_action",
        "start_run",
        "write_journal",
    }
    repository_writers = {
        "create_conversation",
        "create_run_and_initial_segment",
    }
    tainted_values: set[str] = set()
    journal_tables = (
        "agent_context_snapshots",
        "agent_events",
        "agent_runs",
        "chat_messages",
        "conversations",
        "pending_actions",
        "pending_tool_calls",
    )

    def model_name(node: ast.AST) -> str:
        return (_task12_resolved_name(node, aliases) or "").rsplit(".", 1)[-1]

    def expression_is_write(node: ast.AST) -> bool:
        for child in ast.walk(node):
            text_value = _task12_text(child, strings)
            if text_value is None:
                continue
            normalized = " ".join(text_value.casefold().split())
            if any(
                normalized.startswith(f"{verb} {table}")
                or normalized.startswith(f"{verb} into {table}")
                or normalized.startswith(f"{verb} from {table}")
                for verb in ("delete", "insert", "update")
                for table in journal_tables
            ):
                return True
        if isinstance(node, ast.Name):
            return node.id in tainted_values
        if isinstance(node, ast.Call):
            terminal = model_name(node.func)
            if terminal in forbidden_models or terminal in write_helpers:
                return True
            if terminal in {"delete", "insert", "update"} and any(
                model_name(child) in forbidden_models
                for child in ast.walk(node)
                if isinstance(child, (ast.Name, ast.Attribute))
            ):
                return True
            if isinstance(node.func, ast.Attribute) and expression_is_write(node.func.value):
                return True
        return any(
            isinstance(child, ast.Name) and child.id in tainted_values
            for child in ast.walk(node)
        )

    for _ in range(8):
        changed = False
        for node in ast.walk(scan):
            if not _task12_node_reachable(node, parents):
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not expression_is_write(node.value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in tainted_values:
                    tainted_values.add(target.id)
                    changed = True
        if not changed:
            break

    for node in ast.walk(scan):
        if not _task12_node_reachable(node, parents):
            continue
        if not isinstance(node, ast.Call):
            continue
        terminal = model_name(node.func)
        receiver = (
            _task12_resolved_name(node.func.value, aliases).casefold()
            if isinstance(node.func, ast.Attribute)
            and _task12_resolved_name(node.func.value, aliases) is not None
            else ""
        )
        semantic_finish = terminal == "finish" and any(
            fragment in receiver for fragment in ("journal", "recorder")
        )
        if (
            terminal in forbidden_models
            or terminal in write_helpers
            or terminal in repository_writers
            or semantic_finish
        ):
            return [f"product-action:chat-journal-write:{name}"]
        if terminal in {"delete", "insert", "update"} and expression_is_write(node):
            return [f"product-action:chat-journal-write:{name}"]
        if terminal in {"add", "add_all", "execute", "flush"} and any(
            expression_is_write(value)
            for value in (*node.args, *(item.value for item in node.keywords))
        ):
            return [f"product-action:chat-journal-write:{name}"]

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    receiver_tags: dict[tuple[str, str], set[str]] = {}
    for function in functions.values():
        for parameter in (*function.args.posonlyargs, *function.args.args):
            tags = {
                tag
                for tag in ("chat", "agent_runs", "journal", "recorder")
                if tag in parameter.arg.casefold()
            }
            if tags:
                receiver_tags[function.name, parameter.arg] = tags
    while True:
        changed = False
        for function in functions.values():
            local_tags = {
                parameter.arg: receiver_tags.get((function.name, parameter.arg), set())
                for parameter in (*function.args.posonlyargs, *function.args.args)
            }
            for call in (
                node for node in ast.walk(function) if isinstance(node, ast.Call)
            ):
                callee = functions.get(_task12_terminal(call.func) or "")
                if callee is None:
                    continue
                for parameter, argument in zip(
                    (*callee.args.posonlyargs, *callee.args.args), call.args, strict=False
                ):
                    if not isinstance(argument, ast.Name):
                        continue
                    target = receiver_tags.setdefault((callee.name, parameter.arg), set())
                    before = len(target)
                    target.update(local_tags.get(argument.id, set()))
                    changed = changed or len(target) != before
        if not changed:
            break
    for function in functions.values():
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute) or not isinstance(
                call.func.value, ast.Name
            ):
                continue
            terminal = call.func.attr
            tags = receiver_tags.get((function.name, call.func.value.id), set())
            if terminal in repository_writers or (
                terminal == "finish" and {"journal", "recorder"} & tags
            ):
                return [f"product-action:chat-journal-write:{name}"]
    return []


def _task12_provider_cross_module_violation(trees: dict[str, ast.Module]) -> bool:
    aliases = {module: _task12_module_import_aliases(tree)[0] for module, tree in trees.items()}
    functions: dict[
        tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef
    ] = {}
    classes: set[tuple[str, str]] = set()
    closure_owners: dict[tuple[str, str], tuple[str, str]] = {}
    nested_callables: dict[tuple[str, str, str], tuple[str, str]] = {}

    def register_function(
        module: str,
        symbol: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        functions[module, symbol] = node
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nested_symbol = f"{symbol}.<locals>.{statement.name}"
            nested = (module, nested_symbol)
            closure_owners[nested] = (module, symbol)
            nested_callables[module, symbol, statement.name] = nested
            register_function(module, nested_symbol, statement)

    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                register_function(module, node.name, node)
            elif isinstance(node, ast.ClassDef):
                classes.add((module, node.name))
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        register_function(
                            module, f"{node.name}.{member.name}", member
                        )

    def target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def call_target(
        module: str,
        node: ast.AST,
        scope_aliases: dict[str, str],
        callable_parameters: dict[str, tuple[str, str]],
    ) -> tuple[str, str] | None:
        if isinstance(node, ast.Name) and node.id in callable_parameters:
            return callable_parameters[node.id]
        if isinstance(node, ast.Call):
            factory = call_target(
                module, node.func, scope_aliases, callable_parameters
            )
            return (
                returned_callable(factory) or factory
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
                module, node.value.func, scope_aliases, callable_parameters
            )
            return (
                returned_callable(factory, key=key)
                if factory is not None
                else None
            )
        resolved = _task12_resolved_name(node, scope_aliases) or ""
        if resolved in callable_parameters:
            return callable_parameters[resolved]
        if isinstance(node, ast.Attribute):
            owner_node = (
                node.value.func if isinstance(node.value, ast.Call) else node.value
            )
            owner = call_target(
                module, owner_node, scope_aliases, callable_parameters
            )
            if owner is not None:
                return owner[0], f"{owner[1]}.{node.attr}"
        imported = target(resolved)
        if imported is not None:
            return imported
        terminal = resolved.rsplit(".", 1)[-1]
        local = (module, terminal)
        return local if local in functions or local in classes else None

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
        if called in seen:
            return set()
        function = functions.get(called)
        if function is None:
            return set()
        scope_aliases = _task12_scope_aliases(aliases[called[0]], function)
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
                ) or call_target(called[0], option, scope_aliases, {})
                if candidate is not None and candidate not in (seen | {called}):
                    candidates.add(candidate)
        return candidates

    caller_string_cache: dict[str, dict[str, str]] = {}

    def known_caller_key(module: str, node: ast.AST) -> str | None:
        direct = _literal_string(node)
        if direct is not None:
            return direct
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
        scope_aliases: dict[str, str],
        callable_parameters: dict[str, tuple[str, str]],
    ) -> set[tuple[str, str]]:
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(
                    module,
                    node.args[0],
                    scope_aliases,
                    callable_parameters,
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
                    scope_aliases,
                    callable_parameters,
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
                    scope_aliases,
                    callable_parameters,
                )
            }
        return set()

    def call_options(
        module: str,
        node: ast.AST,
        scope_aliases: dict[str, str],
        callable_parameters: dict[str, tuple[str, str]],
    ) -> set[tuple[str, str]]:
        if isinstance(node, ast.IfExp):
            return call_options(
                module, node.body, scope_aliases, callable_parameters
            ) | call_options(
                module, node.orelse, scope_aliases, callable_parameters
            )
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(
                    module,
                    node.args[0],
                    scope_aliases,
                    callable_parameters,
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"items", "popitem", "values"}
            ):
                return iterable_options(
                    module,
                    node,
                    scope_aliases,
                    callable_parameters,
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
                    scope_aliases,
                    callable_parameters,
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
                        scope_aliases,
                        callable_parameters,
                    )
                return set()
            factories = call_options(
                module, node.func, scope_aliases, callable_parameters
            )
            return {
                candidate
                for factory in factories
                for candidate in (returned_callables(factory) or {factory})
            }
        if isinstance(node, ast.Subscript):
            key = known_caller_key(module, node.slice)
            if key is not None and isinstance(node.value, ast.Call):
                owners = call_options(
                    module,
                    node.value.func,
                    scope_aliases,
                    callable_parameters,
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
                    scope_aliases,
                    callable_parameters,
                )
        resolved = call_target(
            module, node, scope_aliases, callable_parameters
        )
        return {resolved} if resolved is not None else set()

    pending: list[tuple[str, str, tuple[tuple[str, tuple[str, str]], ...]]] = []
    root_scopes: list[tuple[str, ast.Module]] = []
    for module, tree in trees.items():
        if not any(
            module == prefix or module.startswith(prefix)
            for prefix in _TASK12_PROVIDER_PATHS
        ):
            continue
        root_scopes.append(
            (
                module,
                ast.Module(
                    body=[
                        node
                        for node in tree.body
                        if not isinstance(
                            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                        )
                    ],
                    type_ignores=[],
                ),
            )
        )
    seen: set[tuple[str, str, tuple[tuple[str, tuple[str, str]], ...]]] = set()
    action_names = {*PRODUCT_ACTIONS, *PRODUCT_ACTION_COMPENSATIONS}

    def scan(
        module: str,
        scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
        callable_parameters: dict[str, tuple[str, str]],
    ) -> bool:
        parents = {
            child: parent
            for parent in ast.walk(scope)
            for child in ast.iter_child_nodes(parent)
        }
        scope_aliases = (
            _task12_scope_aliases(aliases[module], scope)
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            else aliases[module]
        )
        assignment_events = sorted(
            (
                assignment
                for assignment in ast.walk(scope)
                if isinstance(assignment, (ast.Assign, ast.AnnAssign))
                and assignment.value is not None
                and _task12_node_reachable(assignment, parents)
                and not any(
                    isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and owner is not scope
                    for owner in itertools.takewhile(
                        lambda owner: owner is not None,
                        itertools.accumulate(
                            itertools.repeat(assignment),
                            lambda current, _unused: parents.get(current),
                        ),
                    )
                )
            ),
            key=lambda assignment: getattr(assignment, "lineno", 0),
        )

        def bindings_before(node: ast.AST) -> dict[str, tuple[str, str]]:
            current = dict(callable_parameters)
            line = getattr(node, "lineno", math.inf)
            for assignment in assignment_events:
                if getattr(assignment, "lineno", 0) >= line:
                    break
                candidate = call_target(
                    module,
                    assignment.value,
                    scope_aliases,
                    current,
                )
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for assignment_target in targets:
                    binding_name = _task12_resolved_name(
                        assignment_target, scope_aliases
                    )
                    if not binding_name:
                        continue
                    if candidate is None:
                        current.pop(binding_name, None)
                    else:
                        current[binding_name] = candidate
            return current
        parameters = {
            parameter.arg
            for parameter in (
                *scope.args.posonlyargs,
                *scope.args.args,
                *scope.args.kwonlyargs,
            )
        } if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else set()
        comprehension_calls: dict[int, set[tuple[str, str]]] = {}
        for comprehension in (
            node
            for node in ast.walk(scope)
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
        for node in ast.walk(scope):
            if not _task12_node_reachable(node, parents):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                and owner is not scope
            ):
                continue
            if isinstance(node, ast.Constant) and node.value in action_names:
                return True
            if isinstance(node, (ast.Name, ast.Attribute)):
                if isinstance(node, ast.Name) and node.id in parameters:
                    continue
                resolved = _task12_resolved_name(node, scope_aliases) or ""
                if resolved.rsplit(".", 1)[-1] in action_names:
                    return True
            if isinstance(node, ast.Call):
                node_parameters = bindings_before(node)
                called_options = comprehension_calls.get(id(node))
                if called_options is None:
                    mapping_dispatch = (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr
                        in {"get", "items", "pop", "popitem", "setdefault", "values"}
                    )
                    called_options = call_options(
                        module,
                        node if mapping_dispatch else node.func,
                        scope_aliases,
                        node_parameters,
                    )
                    if not called_options and not mapping_dispatch:
                        called_options = call_options(
                            module, node, scope_aliases, node_parameters
                        )
                for called in called_options:
                    if called not in functions:
                        continue
                    callee = functions[called]
                    positional = (*callee.args.posonlyargs, *callee.args.args)
                    if "." in called[1] and positional and positional[0].arg in {
                        "cls",
                        "self",
                    }:
                        positional = positional[1:]
                    next_parameters: dict[str, tuple[str, str]] = {}
                    closure_owner = closure_owners.get(called)
                    if closure_owner is not None:
                        owner = functions[closure_owner]
                        owner_parameters = (
                            *owner.args.posonlyargs,
                            *owner.args.args,
                        )
                        owner_call = (
                            node.func
                            if isinstance(node.func, ast.Call)
                            else node
                        )
                        for parameter, argument in zip(
                            owner_parameters,
                            owner_call.args,
                            strict=False,
                        ):
                            candidate = call_target(
                                module,
                                argument,
                                scope_aliases,
                                node_parameters,
                            )
                            if candidate is not None:
                                next_parameters[parameter.arg] = candidate
                        owner_aliases = _task12_scope_aliases(
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
                                owner_aliases,
                                next_parameters,
                            )
                            assignment_targets = (
                                statement.targets
                                if isinstance(statement, ast.Assign)
                                else [statement.target]
                            )
                            for assignment_target in assignment_targets:
                                binding_name = (
                                    assignment_target.id
                                    if isinstance(assignment_target, ast.Name)
                                    else _task12_resolved_name(
                                        assignment_target, owner_aliases
                                    )
                                )
                                if not binding_name:
                                    continue
                                if candidate is None:
                                    next_parameters.pop(binding_name, None)
                                else:
                                    next_parameters[binding_name] = candidate
                    if (
                        "." in called[1]
                        and ".<locals>." not in called[1]
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Call)
                    ):
                        class_name = called[1].split(".", 1)[0]
                        constructor = call_target(
                            module,
                            node.func.value.func,
                            scope_aliases,
                            node_parameters,
                        )
                        initializer = functions.get(
                            (called[0], f"{class_name}.__init__")
                        )
                        if constructor == (called[0], class_name) and initializer:
                            init_parameters = (
                                *initializer.args.posonlyargs,
                                *initializer.args.args,
                            )[1:]
                            init_bindings: dict[str, tuple[str, str]] = {}
                            for parameter, argument in zip(
                                init_parameters,
                                node.func.value.args,
                                strict=False,
                            ):
                                candidate = call_target(
                                    module,
                                    argument,
                                    scope_aliases,
                                    node_parameters,
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
                                        next_parameters[
                                            f"self.{assignment_target.attr}"
                                        ] = candidate
                    if (
                        "." in called[1]
                        and ".<locals>." not in called[1]
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                    ):
                        receiver = node.func.value.id
                        for binding_name, candidate in node_parameters.items():
                            prefix = f"{receiver}."
                            if binding_name.startswith(prefix):
                                next_parameters[
                                    f"self.{binding_name[len(prefix):]}"
                                ] = candidate
                    for parameter, argument in zip(positional, node.args, strict=False):
                        candidate = call_target(
                            module, argument, scope_aliases, node_parameters
                        )
                        if candidate is not None:
                            next_parameters[parameter.arg] = candidate
                    for keyword in node.keywords:
                        if keyword.arg is None:
                            continue
                        candidate = call_target(
                            module, keyword.value, scope_aliases, node_parameters
                        )
                        if candidate is not None:
                            next_parameters[keyword.arg] = candidate
                    pending.append(
                        (
                            called[0],
                            called[1],
                            tuple(sorted(next_parameters.items())),
                        )
                    )
        return False

    for module, root in root_scopes:
        if scan(module, root, {}):
            return True
    while pending:
        module, symbol, frozen = pending.pop()
        state = (module, symbol, frozen)
        if state in seen:
            continue
        seen.add(state)
        if scan(module, functions[module, symbol], dict(frozen)):
            return True
    return False


def _task12_product_action_reachable_domain_violation(
    trees: dict[str, ast.Module],
) -> bool:
    callables: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: set[tuple[str, str]] = set()
    imports: dict[str, dict[str, str]] = {}
    for module, tree in trees.items():
        imports[module] = _task12_module_import_aliases(tree)[0]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callables[module, node.name] = node
            elif isinstance(node, ast.ClassDef):
                classes.add((module, node.name))
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        callables[module, f"{node.name}.{member.name}"] = member

    imported_target_cache: dict[str, tuple[str, str] | None] = {}

    def imported_target(resolved: str) -> tuple[str, str] | None:
        if resolved in imported_target_cache:
            return imported_target_cache[resolved]
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        result = (
            (matches[-1][1], matches[-1][2])
            if (matches := sorted(matches))
            else None
        )
        imported_target_cache[resolved] = result
        return result

    def follow_export(
        target: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str]:
        module, symbol = target
        if target in seen or not symbol:
            return target
        resolved = imports[module].get(symbol)
        if resolved is None:
            return target
        nested = imported_target(resolved)
        return follow_export(nested, seen | {target}) if nested is not None else target

    def call_target(
        module: str,
        node: ast.AST,
        local_aliases: dict[str, str] | None = None,
        instances: dict[str, tuple[str, str]] | None = None,
        callable_bindings: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        active_aliases = local_aliases or imports[module]
        instances = instances or {}
        callable_bindings = callable_bindings or {}
        if isinstance(node, ast.Await):
            return call_target(
                module, node.value, active_aliases, instances, callable_bindings
            )
        if isinstance(node, ast.Name) and node.id in callable_bindings:
            return callable_bindings[node.id]
        if isinstance(node, ast.Call):
            factory = call_target(
                module, node.func, active_aliases, instances, callable_bindings
            )
            return (
                returned_callable(factory) or returned_instance(factory)
                if factory is not None
                else None
            )
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in instances:
                owner = instances[node.value.id]
                return owner[0], f"{owner[1]}.{node.attr}"
            if isinstance(node.value, ast.Call):
                factory = call_target(
                    module,
                    node.value.func,
                    active_aliases,
                    instances,
                    callable_bindings,
                )
                owner = (
                    returned_instance(factory) if factory is not None else None
                ) or factory
            else:
                owner = call_target(
                    module,
                    node.value,
                    active_aliases,
                    instances,
                    callable_bindings,
                )
            if owner is not None:
                owner = follow_export(owner)
                return owner[0], f"{owner[1]}.{node.attr}"
        resolved = _task12_resolved_name(node, active_aliases) or ""
        target = imported_target(resolved)
        if target is not None:
            return follow_export(target)
        terminal = resolved.rsplit(".", 1)[-1]
        return (
            (module, terminal)
            if (module, terminal) in callables or (module, terminal) in classes
            else None
        )

    def returned_instance(
        target: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        if target in seen:
            return None
        body = callables.get(target)
        if body is None:
            return target if target in classes else None
        local_aliases = _task12_scope_aliases(imports[target[0]], body)
        resolved: set[tuple[str, str]] = set()
        local_instances: dict[str, tuple[str, str]] = {}
        for assignment in ast.walk(body):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, ast.Call
            ):
                continue
            candidate = call_target(target[0], assignment.value.func, local_aliases)
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name) and candidate in classes:
                    local_instances[assignment_target.id] = candidate
        for returned in ast.walk(body):
            if not isinstance(returned, ast.Return) or returned.value is None:
                continue
            if isinstance(returned.value, ast.Name) and returned.value.id in local_instances:
                resolved.add(local_instances[returned.value.id])
                continue
            if not isinstance(returned.value, ast.Call):
                continue
            candidate = call_target(target[0], returned.value.func, local_aliases)
            if candidate in classes:
                resolved.add(candidate)
            elif candidate is not None:
                nested = returned_instance(candidate, seen | {target})
                if nested is not None:
                    resolved.add(nested)
        return next(iter(resolved)) if len(resolved) == 1 else None

    def returned_callable(
        target: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        if target in seen:
            return None
        body = callables.get(target)
        if body is None:
            return None
        local_aliases = _task12_scope_aliases(imports[target[0]], body)

        def candidates(node: ast.AST) -> set[tuple[str, str]]:
            if isinstance(node, ast.IfExp):
                return candidates(node.body) | candidates(node.orelse)
            expression = node.func if isinstance(node, ast.Call) else node
            resolved = call_target(target[0], expression, local_aliases)
            if resolved is None or resolved in (seen | {target}):
                return set()
            if isinstance(node, ast.Call):
                returned = returned_callable(resolved, seen | {target})
                return {returned} if returned is not None else set()
            return {resolved}

        values = [
            returned.value
            for returned in ast.walk(body)
            if isinstance(returned, ast.Return) and returned.value is not None
        ]
        resolved = {candidate for value in values for candidate in candidates(value)}
        return next(iter(resolved)) if len(resolved) == 1 else None

    pending = [
        (module, symbol, tuple())
        for module, symbol in callables
        if any(module.startswith(prefix) for prefix in _TASK12_PRODUCT_ACTION_EXECUTION_PATHS)
    ]
    seen: set[tuple[str, str, tuple[tuple[str, tuple[str, str]], ...]]] = set()
    root_modules = {module for module, _symbol, _bindings in pending}
    while pending:
        module, symbol, frozen_bindings = pending.pop()
        state = (module, symbol, frozen_bindings)
        if state in seen:
            continue
        seen.add(state)
        callable_bindings = dict(frozen_bindings)
        body = callables[module, symbol]
        if module not in root_modules:
            if _task12_product_action_domain_violations(
                "product_actions/reachable.py",
                trees[module],
                scope=body,
            ):
                return True
        local_aliases = _task12_scope_aliases(imports[module], body)
        instances: dict[str, tuple[str, str]] = {}
        for _ in range(8):
            changed = False
            for assignment in ast.walk(body):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                    continue
                targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                for assignment_target in targets:
                    if not isinstance(assignment_target, ast.Name):
                        continue
                    if isinstance(assignment.value, (ast.Name, ast.Attribute)):
                        resolved = _task12_resolved_name(assignment.value, local_aliases)
                        if resolved is not None and local_aliases.get(assignment_target.id) != resolved:
                            local_aliases[assignment_target.id] = resolved
                            changed = True
                    elif isinstance(assignment.value, ast.Call):
                        owner = call_target(
                            module,
                            assignment.value.func,
                            local_aliases,
                            instances,
                            callable_bindings,
                        )
                        if owner is not None and instances.get(assignment_target.id) != owner:
                            instances[assignment_target.id] = follow_export(owner)
                            changed = True
            if not changed:
                break
        for call in (node for node in ast.walk(body) if isinstance(node, ast.Call)):
            target = call_target(
                module, call.func, local_aliases, instances, callable_bindings
            )
            if target is None or target not in callables:
                continue
            callee = callables[target]
            parameters = [
                parameter.arg
                for parameter in (
                    *callee.args.posonlyargs,
                    *callee.args.args,
                    *callee.args.kwonlyargs,
                )
                if parameter.arg not in {"self", "cls"}
            ]
            next_bindings: dict[str, tuple[str, str]] = {}
            for parameter, argument in zip(parameters, call.args, strict=False):
                resolved_argument = call_target(
                    module,
                    argument,
                    local_aliases,
                    instances,
                    callable_bindings,
                )
                if resolved_argument is not None:
                    next_bindings[parameter] = resolved_argument
            for keyword in call.keywords:
                if keyword.arg is None or keyword.arg not in parameters:
                    continue
                resolved_argument = call_target(
                    module,
                    keyword.value,
                    local_aliases,
                    instances,
                    callable_bindings,
                )
                if resolved_argument is not None:
                    next_bindings[keyword.arg] = resolved_argument
            next_state = (target[0], target[1], tuple(sorted(next_bindings.items())))
            if next_state not in seen:
                pending.append(next_state)
    return False


def _task12_dict_value(
    node: ast.AST,
    dictionaries: dict[str, dict[str, str]],
    functions: dict[str, dict[str, str]],
    strings: dict[str, str],
) -> dict[str, str] | None:
    if isinstance(node, ast.Name):
        return dictionaries.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _task12_dict_value(node.left, dictionaries, functions, strings)
        right = _task12_dict_value(node.right, dictionaries, functions, strings)
        if left is None or right is None:
            return None
        return {**left, **right}
    if isinstance(node, ast.Call):
        terminal = _task12_terminal(node.func)
        if terminal == "dict":
            result: dict[str, str] = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    spread = _task12_dict_value(
                        keyword.value, dictionaries, functions, strings
                    )
                    if spread is None:
                        return None
                    result.update(spread)
                    continue
                resolved = _task12_text(keyword.value, strings)
                if resolved is not None:
                    result[keyword.arg] = resolved
            return result
        return functions.get(terminal or "")
    if not isinstance(node, ast.Dict):
        return None
    result: dict[str, str] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            spread = _task12_dict_value(value, dictionaries, functions, strings)
            if spread is None:
                return None
            result.update(spread)
            continue
        resolved_key = _task12_text(key, strings)
        resolved_value = _task12_text(value, strings)
        if resolved_key is None or resolved_value is None:
            continue
        result[resolved_key] = resolved_value
    return result


def _task12_dict_bindings(
    tree: ast.Module,
    strings: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    strings = dict(strings) if strings is not None else _task12_string_bindings(tree)
    dictionaries: dict[str, dict[str, str]] = {}
    functions: dict[str, dict[str, str]] = {}
    for _ in range(12):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returns = [
                    returned.value
                    for returned in ast.walk(node)
                    if isinstance(returned, ast.Return) and returned.value is not None
                ]
                if len(returns) == 1:
                    value = _task12_dict_value(returns[0], dictionaries, functions, strings)
                    if value is not None and functions.get(node.name) != value:
                        functions[node.name] = value
                        changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and (key := _task12_text(target.slice, strings)) is not None
                        and (resolved := _task12_text(node.value, strings)) is not None
                    ):
                        mapping = dictionaries.setdefault(target.value.id, {})
                        if mapping.get(key) != resolved:
                            mapping[key] = resolved
                            changed = True
                value = _task12_dict_value(node.value, dictionaries, functions, strings)
                if value is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and dictionaries.get(target.id) != value:
                        dictionaries[target.id] = dict(value)
                        changed = True
            elif (
                isinstance(node, ast.AugAssign)
                and isinstance(node.op, ast.BitOr)
                and isinstance(node.target, ast.Name)
            ):
                value = _task12_dict_value(node.value, dictionaries, functions, strings)
                if value:
                    mapping = dictionaries.setdefault(node.target.id, {})
                    before = dict(mapping)
                    mapping.update(value)
                    changed = changed or mapping != before
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"setdefault", "update"}
                and isinstance(node.func.value, ast.Name)
            ):
                update: dict[str, str] = {}
                if node.func.attr == "setdefault" and len(node.args) >= 2:
                    key = _task12_text(node.args[0], strings)
                    value = _task12_text(node.args[1], strings)
                    if key is not None and value is not None:
                        update[key] = value
                else:
                    for argument in node.args:
                        value = _task12_dict_value(argument, dictionaries, functions, strings)
                        if value:
                            update.update(value)
                    for keyword in node.keywords:
                        if keyword.arg is not None and (
                            resolved := _task12_text(keyword.value, strings)
                        ) is not None:
                            update[keyword.arg] = resolved
                if update:
                    mapping = dictionaries.setdefault(node.func.value.id, {})
                    before = dict(mapping)
                    if node.func.attr == "setdefault":
                        for key, value in update.items():
                            mapping.setdefault(key, value)
                    else:
                        mapping.update(update)
                    changed = changed or mapping != before
        if not changed:
            break
    return dictionaries, functions


def _task12_parent_writer_violations(name: str, tree: ast.Module) -> list[str]:
    aliases, _modules = _task12_module_import_aliases(tree)

    def bind_alias(target: ast.AST, value: ast.AST) -> bool:
        if isinstance(target, ast.Name) and isinstance(value, (ast.Name, ast.Attribute)):
            resolved = _task12_resolved_name(value, aliases)
            if resolved is not None and aliases.get(target.id) != resolved:
                aliases[target.id] = resolved
                return True
            return False
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(
            value, (ast.Tuple, ast.List)
        ):
            changed = False
            for child_target, child_value in zip(
                target.elts, value.elts, strict=False
            ):
                changed = bind_alias(child_target, child_value) or changed
            return changed
        return False

    for _ in range(8):
        changed = False
        for assignment in tree.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                changed = bind_alias(target, assignment.value) or changed
        if not changed:
            break
    strings = _task12_string_bindings(tree)
    parameter_names = {
        parameter.arg
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for parameter in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    strings = {
        key: value for key, value in strings.items() if key not in parameter_names
    }
    dictionaries, functions = _task12_dict_bindings(tree, strings)
    local_functions = {
        function.name: function
        for function in tree.body
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expressions: dict[str, ast.AST] = {}
    for assignment in ast.walk(tree):
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                expressions[target.id] = assignment.value
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def sql_parent_shape(
        node: ast.AST,
        seen: frozenset[str] = frozenset(),
    ) -> tuple[bool, bool]:
        if isinstance(node, ast.Name) and node.id not in seen:
            mapping = dictionaries.get(node.id, {})
            nested = expressions.get(node.id)
            nested_shape = (
                sql_parent_shape(nested, seen | {node.id})
                if nested is not None
                else (False, False)
            )
            return (
                nested_shape[0],
                nested_shape[1] or mapping.get("adapter_kind") == "product_action",
            )
        text_value = _task12_text(node, strings)
        has_insert = bool(
            text_value
            and "insert into write_operations" in " ".join(text_value.casefold().split())
        )
        mapping = _task12_dict_value(node, dictionaries, functions, strings)
        has_adapter = bool(mapping and mapping.get("adapter_kind") == "product_action")
        adapter_placeholders: set[str] = set()
        for child in ast.walk(node):
            sql = _task12_text(child, strings)
            if sql is None:
                continue
            match = re.search(
                r"insert\s+into\s+write_operations\s*\(([^)]*)\)\s*"
                r"values\s*\(([^)]*)\)",
                " ".join(sql.casefold().split()),
            )
            if match is None:
                continue
            columns = [item.strip().strip('\"`[]') for item in match.group(1).split(",")]
            placeholders = [item.strip().removeprefix(":") for item in match.group(2).split(",")]
            for column, placeholder in zip(columns, placeholders, strict=False):
                if column == "adapter_kind":
                    adapter_placeholders.add(placeholder)
        if adapter_placeholders:
            for child in ast.walk(node):
                candidate = _task12_dict_value(child, dictionaries, functions, strings)
                if candidate and any(
                    candidate.get(placeholder) == "product_action"
                    for placeholder in adapter_placeholders
                ):
                    has_adapter = True
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"execute", "executemany", "exec_driver_sql"} and node.args:
                sql = _task12_text(node.args[0], strings)
                match = (
                    re.search(
                        r"insert\s+into\s+write_operations\s*\(([^)]*)\)\s*"
                        r"values\s*\(([^)]*)\)",
                        " ".join(sql.casefold().split()),
                    )
                    if sql is not None
                    else None
                )
                if match is not None:
                    columns = [
                        item.strip().strip('\"`[]')
                        for item in match.group(1).split(",")
                    ]
                    try:
                        adapter_index = columns.index("adapter_kind")
                    except ValueError:
                        adapter_index = -1

                    def positional_rows(value: ast.AST) -> list[ast.AST]:
                        if isinstance(value, (ast.Tuple, ast.List)):
                            if value.elts and all(
                                isinstance(item, (ast.Tuple, ast.List))
                                for item in value.elts
                            ):
                                return list(value.elts)
                            return [value]
                        if isinstance(value, ast.Name) and value.id in expressions:
                            return positional_rows(expressions[value.id])
                        if isinstance(value, ast.Call):
                            generator = local_functions.get(_task12_terminal(value.func) or "")
                            if generator is not None:
                                return [
                                    yielded.value
                                    for yielded in ast.walk(generator)
                                    if isinstance(yielded, ast.Yield)
                                    and isinstance(yielded.value, (ast.Tuple, ast.List))
                                ]
                        return []

                    if adapter_index >= 0 and len(node.args) >= 2:
                        for row in positional_rows(node.args[1]):
                            assert isinstance(row, (ast.Tuple, ast.List))
                            if adapter_index < len(row.elts) and _task12_text(
                                row.elts[adapter_index], strings
                            ) == "product_action":
                                has_adapter = True
                                has_insert = True
            if terminal == "bindparam" and node.args:
                key = _task12_text(node.args[0], strings)
                values = [
                    *node.args[1:],
                    *(item.value for item in node.keywords if item.arg == "value"),
                ]
                has_adapter = has_adapter or (
                    key == "adapter_kind"
                    and any(_task12_text(value, strings) == "product_action" for value in values)
                )
            if terminal == "bindparams":
                has_adapter = has_adapter or any(
                    item.arg == "adapter_kind"
                    and _task12_text(item.value, strings) == "product_action"
                    for item in node.keywords
                )
        for child in ast.iter_child_nodes(node):
            child_insert, child_adapter = sql_parent_shape(child, seen)
            has_insert = has_insert or child_insert
            has_adapter = has_adapter or child_adapter
        return has_insert, has_adapter

    def owner(node: ast.AST) -> tuple[str | None, str | None]:
        function_name: str | None = None
        class_name: str | None = None
        parent = parents.get(node)
        while parent is not None:
            if function_name is None and isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                function_name = parent.name
            elif class_name is None and isinstance(parent, ast.ClassDef):
                class_name = parent.name
            parent = parents.get(parent)
        return class_name, function_name

    writer_count = 0
    for node in ast.walk(tree):
        if not _task12_node_reachable(node, parents):
            continue
        if not isinstance(node, ast.Call):
            continue
        parent = parents.get(node)
        function_scope: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_scope = parent
                break
            parent = parents.get(parent)
        call_aliases = (
            _task12_scope_aliases(aliases, function_scope)
            if function_scope is not None
            else aliases
        )
        terminal = (_task12_resolved_name(node.func, call_aliases) or "").rsplit(".", 1)[-1]
        writes_parent = False
        if terminal == "WriteOperation":
            values: dict[str, str] = {}
            for argument in node.args:
                positional = _task12_dict_value(
                    argument, dictionaries, functions, strings
                )
                if positional:
                    values.update(positional)
            for keyword in node.keywords:
                if keyword.arg is None:
                    spread = _task12_dict_value(keyword.value, dictionaries, functions, strings)
                    if spread:
                        values.update(spread)
                else:
                    resolved = _task12_text(keyword.value, strings)
                    if resolved is not None:
                        values[keyword.arg] = resolved
            writes_parent = values.get("adapter_kind") == "product_action"
            if not writes_parent:
                writes_parent = any(
                    keyword.arg == "adapter_kind"
                    and isinstance(keyword.value, ast.Name)
                    and any(
                        isinstance(assignment, (ast.Assign, ast.AnnAssign))
                        and assignment.value is not None
                        and _task12_text(assignment.value, strings) == "product_action"
                        and any(
                            isinstance(target, ast.Name)
                            and target.id == keyword.value.id
                            for target in (
                                assignment.targets
                                if isinstance(assignment, ast.Assign)
                                else [assignment.target]
                            )
                        )
                        for assignment in ast.walk(function_scope or tree)
                    )
                    for keyword in node.keywords
                )
        elif (
            terminal == "partial"
            and node.args
            and (_task12_resolved_name(node.args[0], call_aliases) or "").rsplit(
                ".", 1
            )[-1]
            == "WriteOperation"
        ):
            writes_parent = any(
                keyword.arg == "adapter_kind"
                and _task12_text(keyword.value, strings) == "product_action"
                for keyword in node.keywords
            )
        elif terminal in {"insert", "values"} and any(
            isinstance(child, ast.Name)
            and (call_aliases.get(child.id, child.id)).rsplit(".", 1)[-1] == "WriteOperation"
            for child in ast.walk(node)
        ):
            values = {}
            for candidate in ast.walk(node):
                if not isinstance(candidate, ast.Call):
                    continue
                for argument in candidate.args:
                    positional = _task12_dict_value(
                        argument, dictionaries, functions, strings
                    )
                    if positional:
                        values.update(positional)
                for keyword in candidate.keywords:
                    if keyword.arg is None:
                        spread = _task12_dict_value(keyword.value, dictionaries, functions, strings)
                        if spread:
                            values.update(spread)
                    else:
                        resolved = _task12_text(keyword.value, strings)
                        if resolved is not None:
                            values[keyword.arg] = resolved
            writes_parent = values.get("adapter_kind") == "product_action"
        if not writes_parent:
            sql_values = [_task12_text(argument, strings) for argument in node.args]
            writes_parent = any(
                value is not None
                and "insert into write_operations" in " ".join(value.casefold().split())
                and "product_action" in value.casefold()
                for value in sql_values
            )
        if not writes_parent and terminal in {
            "execute",
            "executemany",
            "exec_driver_sql",
        }:
            has_insert, has_adapter = sql_parent_shape(node)
            writes_parent = has_insert and has_adapter
        if not writes_parent:
            continue
        writer_count += 1
        class_name, function_name = owner(node)
        if not (
            name == "product_actions/repository.py"
            and class_name == "ProductActionProposalRepository"
            and function_name == "_publish_bundle_in_session_unclaimed"
        ):
            return [f"ledger:unsealed-parent-writer:{name}"]
    if name == "product_actions/repository.py" and writer_count > 1:
        return [f"ledger:unsealed-parent-writer:{name}"]
    return []


def _task12_parent_cross_module_violations(
    trees: dict[str, ast.Module],
) -> list[str]:
    imports = {module: _task12_module_import_aliases(tree)[0] for module, tree in trees.items()}
    module_strings = {
        module: _task12_string_bindings(tree) for module, tree in trees.items()
    }
    module_dicts = {
        module: _task12_dict_bindings(tree, module_strings[module])
        for module, tree in trees.items()
    }
    callables = {
        (module, node.name): node
        for module, tree in trees.items()
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for module, tree in trees.items():
        callables[module, "<module>"] = ast.FunctionDef(
            name="<module>",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                statement
                for statement in tree.body
                if not isinstance(
                    statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            ],
            decorator_list=[],
        )

    imported_target_cache: dict[str, tuple[str, str] | None] = {}

    def imported_target(resolved: str) -> tuple[str, str] | None:
        if resolved in imported_target_cache:
            return imported_target_cache[resolved]
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        result = (matches[-1][1], matches[-1][2]) if matches else None
        imported_target_cache[resolved] = result
        return result

    def follow(target: tuple[str, str], seen: frozenset[tuple[str, str]] = frozenset()) -> tuple[str, str]:
        if target in seen:
            return target
        resolved = imports[target[0]].get(target[1])
        nested = imported_target(resolved) if resolved is not None else None
        return follow(nested, seen | {target}) if nested is not None else target

    def call_target(
        module: str,
        node: ast.AST,
        scope_aliases: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        resolved = _task12_resolved_name(node, scope_aliases or imports[module]) or ""
        imported = imported_target(resolved)
        if imported is not None:
            return follow(imported)
        terminal = resolved.rsplit(".", 1)[-1]
        return (module, terminal) if (module, terminal) in callables else None

    def default_environment(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        positional = (*function.args.posonlyargs, *function.args.args)
        for parameter, default in zip(
            positional[-len(function.args.defaults) :],
            function.args.defaults,
            strict=False,
        ):
            if (resolved := _task12_text(default, {})) is not None:
                result[parameter.arg] = resolved
        for parameter, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults, strict=True
        ):
            if default is not None and (resolved := _task12_text(default, {})) is not None:
                result[parameter.arg] = resolved
        return result

    pending = [
        (module, symbol, tuple(sorted(default_environment(function).items())))
        for (module, symbol), function in callables.items()
    ]
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    findings: set[str] = set()
    while pending:
        module, symbol, frozen = pending.pop()
        state = (module, symbol, frozen)
        if state in seen:
            continue
        seen.add(state)
        environment = default_environment(callables[module, symbol])
        environment.update(frozen)
        body = callables[module, symbol]
        parameter_names = {
            parameter.arg
            for parameter in (
                *body.args.posonlyargs,
                *body.args.args,
                *body.args.kwonlyargs,
            )
        }
        scope_aliases = _task12_scope_aliases(imports[module], body)
        parents = {
            child: parent
            for parent in ast.walk(body)
            for child in ast.iter_child_nodes(parent)
        }
        possible_product_fields: set[str] = set()

        def in_runtime_branch(node: ast.AST) -> bool:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.Match, ast.Try, ast.TryStar)):
                    return True
                if isinstance(parent, ast.If) and not (
                    isinstance(parent.test, ast.Constant)
                    and isinstance(parent.test.value, bool)
                ):
                    return True
                parent = parents.get(parent)
            return False

        def value(node: ast.AST) -> str | None:
            if isinstance(node, ast.Name):
                if node.id in parameter_names:
                    return environment.get(node.id)
                return environment.get(node.id, module_strings[module].get(node.id))
            return _task12_text(node, environment)

        def mapping(node: ast.AST) -> dict[str, str]:
            if isinstance(node, ast.Name):
                prefix = f"{node.id}."
                return {
                    key.removeprefix(prefix): item
                    for key, item in environment.items()
                    if key.startswith(prefix)
                }
            local_dicts, local_factories = module_dicts[module]
            resolved = _task12_dict_value(
                node, local_dicts, local_factories, module_strings[module]
            )
            if resolved is not None:
                return resolved
            if isinstance(node, ast.Call):
                target = call_target(module, node.func, scope_aliases)
                if target is not None:
                    target_dicts, target_factories = module_dicts[target[0]]
                    options = [
                        resolved
                        for returned in ast.walk(callables.get(target, ast.Pass()))
                        if isinstance(returned, ast.Return)
                        and returned.value is not None
                        if (
                            resolved := _task12_dict_value(
                                returned.value,
                                target_dicts,
                                target_factories,
                                module_strings[target[0]],
                            )
                        )
                        is not None
                    ]
                    if options:
                        return next(
                            (
                                option
                                for option in options
                                if option.get("adapter_kind") == "product_action"
                            ),
                            options[0],
                        )
                    return dict(target_factories.get(target[1], {}))
            return {}

        for assignment in ast.walk(body):
            if not _task12_node_reachable(assignment, parents):
                continue
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name):
                    if (resolved := value(assignment.value)) is not None:
                        environment[assignment_target.id] = resolved
                    if isinstance(assignment.value, ast.Dict):
                        for key, item in zip(
                            assignment.value.keys, assignment.value.values, strict=True
                        ):
                            resolved_key = _task12_text(key, environment) if key is not None else None
                            resolved_item = value(item)
                            if resolved_key is not None and resolved_item is not None:
                                environment[
                                    f"{assignment_target.id}.{resolved_key}"
                                ] = resolved_item
                elif (
                    isinstance(assignment_target, ast.Subscript)
                    and isinstance(assignment_target.value, ast.Name)
                    and (resolved_item := value(assignment.value)) is not None
                ):
                    keys: set[str] = set()
                    if isinstance(assignment_target.slice, ast.Constant) and isinstance(
                        assignment_target.slice.value, str
                    ):
                        keys.add(assignment_target.slice.value)
                    elif isinstance(assignment_target.slice, ast.Name):
                        if (
                            resolved_key := environment.get(assignment_target.slice.id)
                        ) is not None:
                            keys.add(resolved_key)
                        else:
                            keys.add("adapter_kind")
                    for resolved_key in keys:
                        environment[
                            f"{assignment_target.value.id}.{resolved_key}"
                        ] = resolved_item
                        if resolved_item == "product_action" and in_runtime_branch(
                            assignment
                        ):
                            possible_product_fields.add(
                                f"{assignment_target.value.id}.{resolved_key}"
                            )

        aliases = scope_aliases
        for call in (
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call) and _task12_node_reachable(node, parents)
        ):
            terminal = (_task12_resolved_name(call.func, aliases) or "").rsplit(".", 1)[-1]
            if terminal in {"execute", "executemany", "exec_driver_sql"} and len(
                call.args
            ) >= 2:
                sql = value(call.args[0])
                match = (
                    re.search(
                        r"insert\s+into\s+write_operations\s*\(([^)]*)\)",
                        " ".join(sql.casefold().split()),
                    )
                    if sql is not None
                    else None
                )
                params = call.args[1]
                if match is not None and isinstance(params, (ast.Tuple, ast.List)):
                    columns = [
                        item.strip().strip('\"`[]')
                        for item in match.group(1).split(",")
                    ]
                    if "adapter_kind" in columns:
                        index = columns.index("adapter_kind")
                        rows = (
                            params.elts
                            if params.elts
                            and all(isinstance(item, (ast.Tuple, ast.List)) for item in params.elts)
                            else [params]
                        )
                        if any(
                            isinstance(row, (ast.Tuple, ast.List))
                            and index < len(row.elts)
                            and value(row.elts[index]) == "product_action"
                            for row in rows
                        ):
                            findings.add(f"ledger:unsealed-parent-writer:{module}")
            if terminal == "WriteOperation" and any(
                keyword.arg == "adapter_kind" and value(keyword.value) == "product_action"
                for keyword in call.keywords
            ) or (
                terminal == "WriteOperation"
                and any(
                    keyword.arg is None
                    and isinstance(keyword.value, ast.Name)
                    and environment.get(
                        f"{keyword.value.id}.adapter_kind"
                    ) == "product_action"
                    or (
                        keyword.arg is None
                        and isinstance(keyword.value, ast.Name)
                        and f"{keyword.value.id}.adapter_kind"
                        in possible_product_fields
                    )
                    for keyword in call.keywords
                )
            ):
                if not (
                    module == "product_actions/repository.py"
                    and symbol == "_publish_bundle_in_session_unclaimed"
                ):
                    findings.add(f"ledger:unsealed-parent-writer:{module}")
            if (
                terminal == "values"
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Call)
                and (_task12_terminal(call.func.value.func) or "") == "insert"
                and any(
                    (_task12_resolved_name(argument, aliases) or "").rsplit(".", 1)[-1]
                    == "WriteOperation"
                    for argument in call.func.value.args
                )
            ):
                values: dict[str, str] = {}
                for argument in call.args:
                    values.update(mapping(argument))
                for keyword in call.keywords:
                    if keyword.arg is None:
                        values.update(mapping(keyword.value))
                    elif (resolved := value(keyword.value)) is not None:
                        values[keyword.arg] = resolved
                if values.get("adapter_kind") == "product_action":
                    findings.add(f"ledger:unsealed-parent-writer:{module}")
            target = call_target(module, call.func, scope_aliases)
            if target is None or target not in callables:
                continue
            callee = callables[target]
            parameters = [
                parameter.arg
                for parameter in (
                    *callee.args.posonlyargs,
                    *callee.args.args,
                    *callee.args.kwonlyargs,
                )
                if parameter.arg not in {"self", "cls"}
            ]
            next_environment: dict[str, str] = {}
            for parameter, argument in zip(parameters, call.args, strict=False):
                if (resolved := value(argument)) is not None:
                    next_environment[parameter] = resolved
                next_environment.update(
                    {
                        f"{parameter}.{key}": item
                        for key, item in mapping(argument).items()
                    }
                )
            for keyword in call.keywords:
                if keyword.arg is None:
                    expanded = mapping(keyword.value)
                    if callee.args.kwarg is not None:
                        next_environment.update(
                            {
                                f"{callee.args.kwarg.arg}.{key}": item
                                for key, item in expanded.items()
                            }
                        )
                    else:
                        next_environment.update(expanded)
                elif keyword.arg in parameters and (
                    resolved := value(keyword.value)
                ) is not None:
                    next_environment[keyword.arg] = resolved
                elif (
                    keyword.arg is not None
                    and callee.args.kwarg is not None
                    and (resolved := value(keyword.value)) is not None
                ):
                    next_environment[
                        f"{callee.args.kwarg.arg}.{keyword.arg}"
                    ] = resolved
            pending.append((target[0], target[1], tuple(sorted(next_environment.items()))))
    return sorted(findings)


def _task12_cross_module_string_bindings(
    trees: dict[str, ast.Module],
) -> dict[str, dict[str, str]]:
    bindings = {module: _task12_string_bindings(tree) for module, tree in trees.items()}
    imports = {module: _task12_module_import_aliases(tree)[0] for module, tree in trees.items()}

    def target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    for _ in range(16):
        changed = False
        for module, aliases in imports.items():
            for local, resolved in aliases.items():
                called = target(resolved)
                visited: set[tuple[str, str]] = set()
                while called is not None and called not in visited:
                    visited.add(called)
                    value = bindings[called[0]].get(called[1]) or bindings[
                        called[0]
                    ].get(f"{called[1]}()")
                    if value is not None:
                        if bindings[module].get(local) != value:
                            bindings[module][local] = value
                            changed = True
                        if bindings[module].get(f"{local}()") != value:
                            bindings[module][f"{local}()"] = value
                            changed = True
                        break
                    nested = imports[called[0]].get(called[1])
                    called = target(nested) if nested is not None else None
            for assignment in trees[module].body:
                if isinstance(assignment, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    returned_values = {
                        value
                        for returned in ast.walk(assignment)
                        if isinstance(returned, ast.Return) and returned.value is not None
                        if (value := _task12_text(returned.value, bindings[module])) is not None
                    }
                    if len(returned_values) == 1:
                        value = next(iter(returned_values))
                        key = f"{assignment.name}()"
                        if bindings[module].get(key) != value:
                            bindings[module][key] = value
                            changed = True
                    continue
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                    continue
                value = _task12_text(assignment.value, bindings[module])
                if value is None:
                    continue
                targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                for assignment_target in targets:
                    if isinstance(assignment_target, ast.Name) and bindings[module].get(
                        assignment_target.id
                    ) != value:
                        bindings[module][assignment_target.id] = value
                        changed = True
        if not changed:
            break
    return bindings


def _task12_routes(
    tree: ast.Module,
    external_strings: dict[str, str] | None = None,
    *,
    router_terminal: str | None = None,
) -> tuple[set[tuple[str, str]], bool]:
    assignments: dict[str, ast.AST] = {
        name: ast.Constant(value=value)
        for name, value in (external_strings or {}).items()
    }
    callables: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callables[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    callables[f"{node.name}.{member.name}"] = member
                elif isinstance(member, (ast.Assign, ast.AnnAssign)) and member.value is not None:
                    targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            assignments[f"{node.name}.{target.id}"] = member.value
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
                    if isinstance(node.value, ast.Lambda):
                        callables[target.id] = node.value

    def callable_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if not isinstance(node, ast.Attribute):
            return ""
        if isinstance(node.value, ast.Name):
            base = class_name(node.value)
            assigned = assignments.get(node.value.id)
            if isinstance(assigned, ast.Call):
                base = class_name(assigned)
            return f"{base}.{node.attr}"
        if isinstance(node.value, ast.Call):
            base = _task12_terminal(node.value.func) or ""
            return f"{base}.{node.attr}" if base else node.attr
        return node.attr

    def class_name(node: ast.AST, seen: frozenset[str] = frozenset()) -> str:
        if isinstance(node, ast.Call):
            return class_name(node.func, seen)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return node.id
            assigned = assignments.get(node.id)
            if isinstance(assigned, ast.Name):
                return class_name(assigned, seen | {node.id})
            return node.id
        return _task12_terminal(node) or ""

    def evaluate(
        node: ast.AST,
        environment: dict[str, set[str]] | None = None,
        seen: frozenset[str] = frozenset(),
    ) -> set[str]:
        environment = environment or {}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            if node.id in environment:
                return environment[node.id]
            if node.id in assignments and node.id not in seen:
                return evaluate(assignments[node.id], environment, seen | {node.id})
            return set()
        if isinstance(node, ast.Attribute):
            owner = class_name(node.value)
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                owner_values = environment.get("__class__", set())
                owner = next(iter(owner_values), owner)
            assignment = assignments.get(f"{owner}.{node.attr}")
            if assignment is not None:
                return evaluate(assignment, environment, seen | {f"{owner}.{node.attr}"})
            property_name = f"{owner}.{node.attr}"
            property_node = callables.get(property_name)
            if property_node is not None and property_name not in seen:
                local = dict(environment)
                local["__class__"] = {owner}
                return {
                    value
                    for returned in ast.walk(property_node)
                    if isinstance(returned, ast.Return) and returned.value is not None
                    for value in evaluate(
                        returned.value, local, seen | {property_name}
                    )
                }
            return set()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = evaluate(node.left, environment, seen)
            right = evaluate(node.right, environment, seen)
            return {prefix + suffix for prefix in left for suffix in right}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            templates = evaluate(node.left, environment, seen)
            if isinstance(node.right, (ast.Tuple, ast.List)):
                parts = [
                    evaluate(item, environment, seen) for item in node.right.elts
                ]
                selections = list(itertools.product(*parts)) if all(parts) else []
            else:
                values = evaluate(node.right, environment, seen)
                selections = [(value,) for value in values]
            results: set[str] = set()
            for template in templates:
                for selection in selections:
                    operand: object = selection if len(selection) != 1 else selection[0]
                    try:
                        results.add(template % operand)
                    except (TypeError, ValueError):
                        continue
            return results
        if isinstance(node, ast.JoinedStr):
            values = {""}
            for part in node.values:
                resolved = (
                    {str(part.value)}
                    if isinstance(part, ast.Constant)
                    else evaluate(part.value, environment, seen)
                    if isinstance(part, ast.FormattedValue)
                    else set()
                )
                if not resolved:
                    return set()
                values = {prefix + suffix for prefix in values for suffix in resolved}
            return values
        if not isinstance(node, ast.Call):
            return set()
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and (
                _task12_resolved_name(node.func.value, _task12_module_import_aliases(tree)[0])
                or ""
            ).rsplit(".", 1)[-1]
            == "posixpath"
        ):
            parts = [evaluate(argument, environment, seen) for argument in node.args]
            if not parts or any(not part for part in parts):
                return set()
            return {
                posixpath.join(*selection)
                for selection in itertools.product(*parts)
            }
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join":
            separators = evaluate(node.func.value, environment, seen)
            if len(node.args) != 1 or not isinstance(
                node.args[0], (ast.Tuple, ast.List)
            ):
                return set()
            parts = [evaluate(item, environment, seen) for item in node.args[0].elts]
            if not separators or any(not part for part in parts):
                return set()
            return {
                separator.join(selection)
                for separator in separators
                for selection in itertools.product(*parts)
            }
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            templates = evaluate(node.func.value, environment, seen)
            positional_values = [
                evaluate(argument, environment, seen) for argument in node.args
            ]
            keyword_values = {
                keyword.arg: evaluate(keyword.value, environment, seen)
                for keyword in node.keywords
                if keyword.arg is not None
            }
            if (
                not templates
                or any(not values for values in positional_values)
                or any(not values for values in keyword_values.values())
            ):
                return set()
            results: set[str] = set()
            positional_products = list(itertools.product(*positional_values)) or [()]
            keyword_names = tuple(keyword_values)
            keyword_products = (
                list(
                    itertools.product(
                        *(keyword_values[name] for name in keyword_names)
                    )
                )
                or [()]
            )
            for template in templates:
                for positional in positional_products:
                    for keyword_selection in keyword_products:
                        try:
                            results.add(
                                template.format(
                                    *positional,
                                    **dict(zip(keyword_names, keyword_selection, strict=True)),
                                )
                            )
                        except (IndexError, KeyError, ValueError):
                            continue
            return results
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format_map":
            templates = evaluate(node.func.value, environment, seen)
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Dict):
                return set()
            options: dict[str, set[str]] = {}
            for key, value in zip(
                node.args[0].keys, node.args[0].values, strict=True
            ):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    return set()
                options[key.value] = evaluate(value, environment, seen)
            if any(not values for values in options.values()):
                return set()
            names = tuple(options)
            results: set[str] = set()
            for template in templates:
                for selection in itertools.product(*(options[name] for name in names)):
                    try:
                        results.add(template.format_map(dict(zip(names, selection, strict=True))))
                    except (KeyError, ValueError):
                        continue
            return results
        if isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
            bases = evaluate(node.func.value, environment, seen)
            if len(node.args) not in {2, 3}:
                return set()
            old_values = evaluate(node.args[0], environment, seen)
            new_values = evaluate(node.args[1], environment, seen)
            counts = (
                {
                    node.args[2].value
                    if isinstance(node.args[2], ast.Constant)
                    and isinstance(node.args[2].value, int)
                    else None
                }
                if len(node.args) == 3
                else {-1}
            )
            return {
                base.replace(old, new, count)
                for base in bases
                for old in old_values
                for new in new_values
                for count in counts
                if count is not None
            }
        callee_name = callable_name(node.func)
        callee = callables.get(callee_name)
        if callee is None:
            external = assignments.get(f"{callee_name}()")
            return evaluate(external, environment, seen) if external is not None else set()
        if callee_name in seen:
            return set()
        arguments = callee.args
        positional = (*arguments.posonlyargs, *arguments.args)
        if positional and positional[0].arg in {"self", "cls"}:
            positional = positional[1:]
        local: dict[str, set[str]] = {}
        if "." in callee_name:
            local["__class__"] = {callee_name.rsplit(".", 1)[0]}
        for parameter, argument in zip(positional, node.args, strict=False):
            local[parameter.arg] = evaluate(argument, environment, seen)
        for keyword in node.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = evaluate(keyword.value, environment, seen)
        if isinstance(callee, ast.Lambda):
            return evaluate(callee.body, local, seen | {callee_name})
        for _ in range(8):
            changed = False
            for assignment in ast.walk(callee):
                if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                    continue
                resolved = evaluate(assignment.value, local, seen | {callee_name})
                if not resolved:
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and local.get(target.id) != resolved:
                        local[target.id] = resolved
                        changed = True
            if not changed:
                break
        return {
            value
            for returned in ast.walk(callee)
            if isinstance(returned, ast.Return) and returned.value is not None
            for value in evaluate(returned.value, local, seen | {callee_name})
        }

    def reachable_fragments(
        node: ast.AST,
        seen: frozenset[str] = frozenset(),
    ) -> set[str]:
        fragments = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        for call in (child for child in ast.walk(node) if isinstance(child, ast.Call)):
            name = callable_name(call.func)
            callee = callables.get(name)
            if callee is not None and name not in seen:
                fragments.update(reachable_fragments(callee, seen | {name}))
        return fragments

    routes: set[tuple[str, str]] = set()
    unprovable_related = False
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        terminal = (_task12_terminal(call.func) or "").casefold()
        if terminal not in {"api_route", "add_api_route"}:
            continue
        if (
            router_terminal is not None
            and isinstance(call.func, ast.Attribute)
            and (_task12_terminal(call.func.value) or "") != router_terminal
        ):
            continue
        route_node = (
            call.args[0]
            if call.args
            else next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "path"
                ),
                None,
            )
        )
        if route_node is None:
            continue
        paths = evaluate(route_node)
        methods = {
            value.casefold()
            for keyword in call.keywords
            if keyword.arg == "methods"
            for value in _task12_eval_strings(keyword.value, {}, {})
        }
        if not methods:
            methods = {"get"}
        routes.update((method, path) for method in methods for path in paths)
        if not paths:
            fragments = reachable_fragments(route_node)
            if any(
                (
                    "post" in methods
                    and fragment.startswith("/api/write-operations/")
                )
                or fragment.startswith("/api/stories")
                for fragment in fragments
            ):
                unprovable_related = True
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            method = (_task12_terminal(decorator.func) or "").casefold()
            if method not in {"delete", "get", "patch", "post", "put"}:
                continue
            if (
                router_terminal is not None
                and isinstance(decorator.func, ast.Attribute)
                and (_task12_terminal(decorator.func.value) or "")
                != router_terminal
            ):
                continue
            route_node = (
                decorator.args[0]
                if decorator.args
                else next(
                    (
                        keyword.value
                        for keyword in decorator.keywords
                        if keyword.arg == "path"
                    ),
                    None,
                )
            )
            if route_node is None:
                continue
            paths = evaluate(route_node)
            routes.update((method, path) for path in paths)
            if not paths:
                fragments = reachable_fragments(route_node)
                if any(
                    (
                        method == "post"
                        and fragment.startswith("/api/write-operations/")
                    )
                    or fragment.startswith("/api/stories")
                    for fragment in fragments
                ):
                    unprovable_related = True
    return routes, unprovable_related


def _task12_included_router_routes(
    trees: dict[str, ast.Module],
    external_strings: dict[str, dict[str, str]],
) -> tuple[set[tuple[str, str]], bool]:
    imports = {
        module: _task12_module_import_aliases(tree)[0]
        for module, tree in trees.items()
    }
    router_prefixes: dict[tuple[str, str], str] = {}
    for module, tree in trees.items():
        strings = external_strings.get(module, {})
        for assignment in tree.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, ast.Call
            ) or (_task12_terminal(assignment.value.func) or "") != "APIRouter":
                continue
            prefix = next(
                (
                    _task12_text(keyword.value, strings)
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
            for target_node in targets:
                if isinstance(target_node, ast.Name):
                    router_prefixes[module, target_node.id] = prefix

    def imported_target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def resolve_router(
        module: str,
        node: ast.AST,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        resolved = _task12_resolved_name(node, imports[module]) or ""
        local = (module, resolved.rsplit(".", 1)[-1])
        if local in router_prefixes:
            return local
        imported = imported_target(resolved)
        if imported is None or imported in seen:
            return None
        nested_alias = imports[imported[0]].get(imported[1])
        if nested_alias is not None:
            nested = imported_target(nested_alias)
            if nested is not None:
                return resolve_router(
                    nested[0],
                    ast.Name(id=nested[1]),
                    seen | {imported},
                )
        return imported

    def join(*parts: str) -> str:
        values = [part.strip("/") for part in parts if part and part != "/"]
        return f"/{'/'.join(values)}" if values else "/"

    api = trees.get("api.py")
    if api is None:
        return set(), False
    pending: list[tuple[str, str, str]] = []
    for call in (node for node in ast.walk(api) if isinstance(node, ast.Call)):
        if (_task12_terminal(call.func) or "") != "include_router" or not call.args:
            continue
        router = resolve_router("api.py", call.args[0])
        if router is None:
            continue
        prefix = next(
            (
                _task12_text(keyword.value, external_strings.get("api.py", {}))
                for keyword in call.keywords
                if keyword.arg == "prefix"
            ),
            "",
        ) or ""
        pending.append((router[0], router[1], prefix))

    routes: set[tuple[str, str]] = set()
    unprovable = False
    seen: set[tuple[str, str, str]] = set()
    while pending:
        module, symbol, parent_prefix = pending.pop()
        state = (module, symbol, parent_prefix)
        if state in seen:
            continue
        seen.add(state)
        own_prefix = router_prefixes.get((module, symbol), "")
        nested_routes, nested_unprovable = _task12_routes(
            trees[module],
            external_strings.get(module),
            router_terminal=symbol,
        )
        routes.update(
            (method, join(parent_prefix, own_prefix, path))
            for method, path in nested_routes
        )
        unprovable = unprovable or nested_unprovable
        for call in (
            node for node in ast.walk(trees[module]) if isinstance(node, ast.Call)
        ):
            if (
                (_task12_terminal(call.func) or "") != "include_router"
                or not isinstance(call.func, ast.Attribute)
                or (_task12_terminal(call.func.value) or "") != symbol
                or not call.args
            ):
                continue
            child = resolve_router(module, call.args[0])
            if child is None:
                continue
            include_prefix = next(
                (
                    _task12_text(keyword.value, external_strings.get(module, {}))
                    for keyword in call.keywords
                    if keyword.arg == "prefix"
                ),
                "",
            ) or ""
            pending.append(
                (
                    child[0],
                    child[1],
                    join(parent_prefix, own_prefix, include_prefix),
                )
            )
    return routes, unprovable


def _task12_preparation_v1_reaches_v2(
    tree: ast.Module,
    trees: dict[str, ast.Module] | None = None,
) -> bool:
    module_trees = dict(trees or {})
    root_module = "repositories/interview_preparation_proposals.py"
    module_trees.setdefault(root_module, tree)
    forbidden = {
        "PreparationReadinessSelectionLoader",
        "_build_v2_snapshot",
        "_load_readiness_selection",
        "_load_v2_selection",
    }
    callables: dict[
        tuple[str, str],
        ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ] = {}
    imports: dict[str, dict[str, str]] = {}
    raw_aliases: dict[tuple[str, str], ast.AST] = {}

    def bind_raw(module: str, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if isinstance(value, ast.Lambda):
                callables[module, target.id] = value
            else:
                raw_aliases[module, target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(
            value, (ast.Tuple, ast.List)
        ):
            for child_target, child_value in zip(target.elts, value.elts, strict=False):
                bind_raw(module, child_target, child_value)

    for module, module_tree in module_trees.items():
        imports[module] = _task12_module_import_aliases(module_tree)[0]
        for node in module_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callables[module, node.name] = node
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        callables[module, f"{node.name}.{member.name}"] = member
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bind_raw(module, target, node.value)

    def imported_target(module: str, resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in module_trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            if normalized == dotted:
                matches.append((len(dotted), candidate, ""))
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def resolve(
        module: str,
        node: ast.AST,
        local_aliases: dict[str, ast.AST],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        if isinstance(node, ast.Call):
            return resolve(module, node.func, local_aliases, seen)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
            owner = resolve(module, node.value.func, local_aliases, seen)
            if owner is not None:
                owner = returned_owner(owner, seen) or owner
                return owner[0], f"{owner[1]}.{node.attr}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assigned_owner = local_aliases.get(node.value.id) or raw_aliases.get(
                (module, node.value.id)
            )
            if isinstance(assigned_owner, ast.Call):
                owner = resolve(module, assigned_owner.func, local_aliases, seen)
                if owner is not None:
                    owner = returned_owner(owner, seen) or owner
                    return owner[0], f"{owner[1]}.{node.attr}"
        if isinstance(node, ast.Name) and node.id in local_aliases:
            return resolve(module, local_aliases[node.id], local_aliases, seen)
        if isinstance(node, ast.Name) and (module, node.id) in raw_aliases:
            key = (module, node.id)
            if key in seen:
                return None
            return resolve(
                module,
                raw_aliases[key],
                local_aliases,
                seen | {key},
            )
        resolved = _task12_resolved_name(node, imports[module])
        if resolved is None:
            return None
        imported = imported_target(module, resolved)
        if imported is not None:
            target_module, symbol = imported
            if symbol and (target_module, symbol) in raw_aliases:
                return resolve(
                    target_module,
                    raw_aliases[target_module, symbol],
                    {},
                    seen | {(target_module, symbol)},
                )
            head, separator, tail = symbol.partition(".")
            if separator and (target_module, head) in raw_aliases:
                owner = resolve(
                    target_module,
                    raw_aliases[target_module, head],
                    {},
                    seen | {(target_module, head)},
                )
                if owner is not None:
                    return owner[0], f"{owner[1]}.{tail}"
            return imported
        if (module, resolved) in callables:
            return module, resolved
        terminal = resolved.rsplit(".", 1)[-1]
        if (module, terminal) in callables:
            return module, terminal
        return module, resolved

    def returned_owner(
        target: tuple[str, str],
        seen: frozenset[tuple[str, str]],
    ) -> tuple[str, str] | None:
        if target in seen:
            return None
        body = callables.get(target)
        if body is None:
            return None
        local: dict[str, ast.AST] = {}
        for assignment in ast.walk(body):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name):
                    local[assignment_target.id] = assignment.value
        owners: set[tuple[str, str]] = set()
        for returned in ast.walk(body):
            if not isinstance(returned, ast.Return) or returned.value is None:
                continue
            value = returned.value.func if isinstance(returned.value, ast.Call) else returned.value
            resolved = resolve(target[0], value, local, seen | {target})
            if resolved is not None:
                owners.add(resolved)
        return next(iter(owners)) if len(owners) == 1 else None

    root = callables.get((root_module, "_build_v1_snapshot"))
    if root is None:
        return False
    pending: list[tuple[str, str, ast.AST]] = [(root_module, "_build_v1_snapshot", root)]
    visited: set[tuple[str, str]] = set()
    while pending:
        module, symbol, body = pending.pop()
        if (module, symbol) in visited:
            continue
        visited.add((module, symbol))
        local_aliases: dict[str, ast.AST] = {}

        def bind_local(target: ast.AST, value: ast.AST) -> None:
            if isinstance(target, ast.Name):
                local_aliases[target.id] = value
            elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                value, (ast.Tuple, ast.List)
            ):
                for child_target, child_value in zip(
                    target.elts, value.elts, strict=False
                ):
                    bind_local(child_target, child_value)

        for node in ast.walk(body):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bind_local(target, node.value)
        for node in ast.walk(body):
            if isinstance(node, (ast.Name, ast.Attribute)):
                resolved = resolve(module, node, local_aliases)
                if resolved is not None and resolved[1].rsplit(".", 1)[-1] in forbidden:
                    return True
            if isinstance(node, ast.Return) and node.value is not None:
                returned = resolve(module, node.value, local_aliases)
                if returned is not None and returned in callables:
                    pending.append((returned[0], returned[1], callables[returned]))
            if not isinstance(node, ast.Call):
                continue
            resolved = resolve(module, node.func, local_aliases)
            if resolved is None:
                continue
            target_module, target_symbol = resolved
            if target_symbol.rsplit(".", 1)[-1] in forbidden:
                return True
            target = callables.get((target_module, target_symbol))
            if target is not None:
                pending.append((target_module, target_symbol, target))
            elif isinstance(node.func, ast.Name):
                alias = local_aliases.get(node.func.id)
                if isinstance(alias, ast.Lambda):
                    pending.append((module, f"{symbol}:{node.func.id}", alias))
    return False


def _task12_compatibility_switch(name: str, tree: ast.Module) -> bool:
    if not any(
        name.startswith(prefix) for prefix in (*_TASK12_PRODUCT_ACTION_EXECUTION_PATHS, "api.py")
    ):
        return False
    forbidden_fragments = (
        "dual_write",
        "fallback_facade",
        "fallback_executor",
        "feature_flag",
        "legacy_executor",
        "legacy_fallback",
        "shadow_write",
    )
    fallback_constructors = {
        "ProductActionCatalogV1",
        "ProductActionCompensationCatalogV1",
        "ProductActionCompensationProofRegistryV1",
        "ProductActionProofRegistryV1",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and any(
            isinstance(child, ast.Call)
            and (_task12_terminal(child.func) or "") in fallback_constructors
            for child in ast.walk(node)
        ):
            return True
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute, ast.FunctionDef, ast.ClassDef)):
            continue
        value = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else node.name
        ).casefold()
        if any(fragment in value for fragment in forbidden_fragments) or (
            "legacy" in value and "fallback" in value
        ):
            return True

    functions: dict[str, ast.AST] = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        call = next(
            (
                member
                for member in class_node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name == "__call__"
            ),
            None,
        )
        if call is not None:
            functions[class_node.name] = call
    for assignment in ast.walk(tree):
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
            assignment.value, ast.Lambda
        ):
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                functions[target.id] = assignment.value
    summaries: dict[str, tuple[bool, bool]] = {
        function: (False, False) for function in functions
    }

    def markers(node: ast.AST) -> tuple[bool, bool]:
        product_action = False
        legacy = False
        for child in ast.walk(node):
            if isinstance(child, (ast.Name, ast.Attribute)):
                value = (_task12_terminal(child) or "").casefold()
                product_action = (
                    product_action
                    or "product_action" in value
                    or (_task12_terminal(child) or "")
                    in {*PRODUCT_ACTIONS, *PRODUCT_ACTION_COMPENSATIONS}
                    or value
                    in {
                        "productactioncatalogv1",
                        "productactioncompensationcatalogv1",
                        "productactioncompensationproofregistryv1",
                        "productactionproofregistryv1",
                    }
                )
                legacy = legacy or (
                    "legacy" in value and (_task12_terminal(child) or "") in summaries
                ) or (_task12_terminal(child) or "") in {
                    "confirm_interview_story_proposal",
                    "save_review_readiness_signal_proposal",
                }
                nested_product, nested_legacy = summaries.get(
                    _task12_terminal(child) or "", (False, False)
                )
                product_action = product_action or nested_product
                legacy = legacy or nested_legacy
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                product_action = product_action or child.value in {
                    *PRODUCT_ACTIONS,
                    *PRODUCT_ACTION_COMPENSATIONS,
                }
            if isinstance(child, ast.Call):
                terminal = _task12_terminal(child.func) or ""
                nested_product, nested_legacy = summaries.get(terminal, (False, False))
                product_action = product_action or nested_product
                legacy = legacy or nested_legacy or terminal in {
                    "confirm_interview_story_proposal",
                    "save_review_readiness_signal_proposal",
                }
        return product_action, legacy

    while True:
        changed = False
        for function, node in functions.items():
            summary = markers(node)
            if summaries[function] != summary:
                summaries[function] = summary
                changed = True
        if not changed:
            break
    for node in ast.walk(tree):
        if isinstance(node, (ast.BoolOp, ast.Dict)):
            product_action, legacy = markers(node)
            if product_action and legacy:
                return True
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(
            isinstance(item.context_expr, ast.Call)
            and (_task12_terminal(item.context_expr.func) or "") == "suppress"
            for item in node.items
        ):
            enclosing = next(
                (
                    function
                    for function in functions.values()
                    if any(child is node for child in ast.walk(function))
                ),
                node,
            )
            product_action, legacy = markers(enclosing)
            if product_action and legacy:
                return True
        if isinstance(node, ast.IfExp):
            left = markers(node.body)
            right = markers(node.orelse)
        elif isinstance(node, ast.If) and node.orelse:
            left = markers(ast.Module(body=node.body, type_ignores=[]))
            right = markers(ast.Module(body=node.orelse, type_ignores=[]))
        elif isinstance(node, ast.Match) and len(node.cases) > 1:
            summaries = [
                markers(ast.Module(body=case.body, type_ignores=[]))
                for case in node.cases
            ]
            if any(
                (one[0] and two[1]) or (one[1] and two[0])
                for one in summaries
                for two in summaries
            ):
                return True
            continue
        elif isinstance(node, ast.Try) and node.handlers:
            left = markers(ast.Module(body=node.body, type_ignores=[]))
            right = markers(
                ast.Module(
                    body=[item for handler in node.handlers for item in handler.body],
                    type_ignores=[],
                )
            )
        else:
            continue
        if (left[0] and right[1]) or (left[1] and right[0]):
            return True
    return False


def _task12_cross_module_compatibility_switch(trees: dict[str, ast.Module]) -> set[str]:
    imports = {module: _task12_module_import_aliases(tree)[0] for module, tree in trees.items()}
    assignments: dict[tuple[str, str], ast.AST] = {}
    callables: dict[
        tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef
    ] = {}
    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callables[module, node.name] = node
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.Assign, ast.AnnAssign)) and member.value is not None:
                        targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                assignments[module, f"{node.name}.{target.id}"] = member.value
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        assignments[module, target.id] = node.value

    def imported_target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def reference(
        module: str,
        node: ast.AST,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str] | None:
        if isinstance(node, ast.Call):
            return reference(module, node.func, seen)
        if isinstance(node, ast.Attribute):
            owner = reference(module, node.value, seen)
            return (owner[0], f"{owner[1]}.{node.attr}") if owner is not None else None
        if not isinstance(node, ast.Name):
            return None
        imported = imported_target(imports[module].get(node.id, node.id))
        target = imported or (module, node.id)
        if target in seen:
            return target
        assigned = assignments.get(target)
        return reference(target[0], assigned, seen | {target}) if assigned is not None else target

    marker_cache: dict[tuple[str, str], tuple[bool, bool]] = {}
    marker_active: set[tuple[str, str]] = set()

    def markers(
        module: str,
        node: ast.AST,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[bool, bool]:
        product = False
        legacy = False
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                product = product or child.value in {
                    *PRODUCT_ACTIONS,
                    *PRODUCT_ACTION_COMPENSATIONS,
                }
                continue
            if not isinstance(child, (ast.Name, ast.Attribute, ast.Call)):
                continue
            expression = child.func if isinstance(child, ast.Call) else child
            target = reference(module, expression)
            terminal = (target[1] if target is not None else (_task12_terminal(expression) or "")).rsplit(".", 1)[-1]
            product = product or terminal in {
                *PRODUCT_ACTIONS,
                *PRODUCT_ACTION_COMPENSATIONS,
                "ProductActionCatalogV1",
                "ProductActionCompensationCatalogV1",
                "ProductActionCompensationProofRegistryV1",
                "ProductActionProofRegistryV1",
            }
            legacy = legacy or terminal in {
                "confirm_interview_story_proposal",
                "save_review_readiness_signal_proposal",
            }
            if (
                target is not None
                and target not in seen
                and target in assignments
            ):
                nested_product, nested_legacy = target_markers(target)
                product = product or nested_product
                legacy = legacy or nested_legacy
            if target is not None and target not in seen and target in callables:
                nested_product, nested_legacy = target_markers(target)
                product = product or nested_product
                legacy = legacy or nested_legacy
        return product, legacy

    def target_markers(target: tuple[str, str]) -> tuple[bool, bool]:
        if target in marker_cache:
            return marker_cache[target]
        if target in marker_active:
            return False, False
        marker_active.add(target)
        node = assignments.get(target) or callables.get(target)
        result = (
            markers(target[0], node, frozenset({target}))
            if node is not None
            else (False, False)
        )
        marker_active.remove(target)
        marker_cache[target] = result
        return result

    switch_cache: dict[tuple[str, str], bool] = {}
    switch_active: set[tuple[str, str]] = set()

    def callable_has_switch(
        target: tuple[str, str],
    ) -> bool:
        if target in switch_cache:
            return switch_cache[target]
        if target in switch_active or target not in callables:
            return False
        switch_active.add(target)
        body = callables[target]
        for index, statement in enumerate(body.body):
            if not isinstance(statement, ast.If):
                continue
            left = markers(
                target[0], ast.Module(body=statement.body, type_ignores=[])
            )
            alternate = statement.orelse or body.body[index + 1 :]
            right = markers(
                target[0], ast.Module(body=alternate, type_ignores=[])
            )
            if (left[0] and right[1]) or (left[1] and right[0]):
                switch_active.remove(target)
                switch_cache[target] = True
                return True
        for call in (node for node in ast.walk(body) if isinstance(node, ast.Call)):
            nested = reference(target[0], call.func)
            if nested is not None and callable_has_switch(nested):
                switch_active.remove(target)
                switch_cache[target] = True
                return True
        switch_active.remove(target)
        switch_cache[target] = False
        return False

    findings: set[str] = set()
    for module, tree in trees.items():
        if not any(module.startswith(prefix) for prefix in _TASK12_PRODUCT_ACTION_EXECUTION_PATHS):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp):
                left = markers(module, node.body)
                right = markers(module, node.orelse)
                if (left[0] and right[1]) or (left[1] and right[0]):
                    findings.add(f"cutover:compatibility-switch:{module}")
            elif isinstance(node, ast.Call):
                target = reference(module, node.func)
                if target is not None and callable_has_switch(target):
                    findings.add(f"cutover:compatibility-switch:{module}")
    return findings


_TASK12_PRIVACY_SINKS = {
    "agentcontextsnapshot": "AgentContextSnapshot",
    "agentevent": "AgentEvent",
    "append_event": "append_event",
    "append_log_entry": "append_log_entry",
    "error_response": "error_response",
    "event": "Event",
    "httpexception": "HTTPException",
    "journalentry": "JournalEntry",
    "jsonresponse": "JSONResponse",
    "preparedsnapshot": "PreparedSnapshot",
    "preparedsurfacemanifestv2": "PreparedSurfaceManifestV2",
    "print": "print",
    "productactiondecisionresultv1": "ProductActionDecisionResultV1",
    "response": "Response",
    "snapshot": "Snapshot",
    "surfacemanifestv2": "SurfaceManifestV2",
    "terminal_result": "terminal_result",
    "transport": "transport",
    "undo": "undo",
    "visible_result": "visible_result",
    "writeoperation": "WriteOperation",
}


def _task12_taint_sink_findings(
    tree: ast.Module,
    *,
    seed_names: set[str],
    seed_literals: set[str],
    sinks: dict[str, str],
    structural_sinks: dict[str, str],
) -> set[str]:
    module_binding_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
    ]
    aliases, _modules = _task12_import_aliases(
        ast.Module(body=module_binding_nodes, type_ignores=[])
    )
    for _ in range(8):
        changed = False
        for assignment in tree.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, (ast.Name, ast.Attribute)
            ):
                continue
            resolved = _task12_resolved_name(assignment.value, aliases)
            if resolved is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
        if not changed:
            break
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    class_methods = {
        (class_node.name, member.name): member
        for class_node in classes.values()
        for member in class_node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    function_identities = {
        id(function): f"module:{name}" for name, function in functions.items()
    }
    function_identities.update(
        {
            id(function): f"class:{class_name}.{method_name}"
            for (class_name, method_name), function in class_methods.items()
        }
    )
    instance_scopes: list[dict[str, str]] = [{}]
    alias_scopes: list[dict[str, str]] = [aliases]
    module_strings, _module_mappings = _task12_callable_return_bindings(tree)
    string_scopes: list[dict[str, str]] = [module_strings]
    nested_function_scopes: list[
        dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    ] = [{}]
    closure_environments: dict[int, dict[str, bool]] = {}
    active_shape_calls: set[str] = set()

    def function_aliases(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> dict[str, str]:
        local = dict(aliases)
        nodes: list[ast.AST] = list(function.body)
        scoped: list[ast.AST] = []
        while nodes:
            node = nodes.pop()
            scoped.append(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            nodes.extend(ast.iter_child_nodes(node))
        for node in scoped:
            if isinstance(node, ast.Import):
                for item in node.names:
                    local[item.asname or item.name.split(".", 1)[0]] = item.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for item in node.names:
                    if item.name != "*":
                        local[item.asname or item.name] = f"{module}.{item.name}"
        for _ in range(8):
            changed = False
            for node in scoped:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
                    node.value, (ast.Name, ast.Attribute)
                ):
                    continue
                resolved = _task12_resolved_name(node.value, local)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and resolved is not None and local.get(
                        target.id
                    ) != resolved:
                        local[target.id] = resolved
                        changed = True
            if not changed:
                break
        return local
    module_environment: dict[str, bool] = {name: True for name in seed_names}
    findings: set[str] = set()
    structural_displays = set(structural_sinks.values())
    active_calls: set[tuple[str, tuple[tuple[str, bool], ...]]] = set()
    return_cache: dict[tuple[str, tuple[tuple[str, bool], ...]], bool] = {}

    def root_names(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return root_names(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            return {name for item in node.elts for name in root_names(item)}
        return set()

    def assign_target(target: ast.AST, value: bool, environment: dict[str, bool]) -> None:
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, (str, int)):
                environment[f"{target.value.id}.{key.value}"] = value
                return
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            environment[f"{target.value.id}.{target.attr}"] = value
            return
        if isinstance(target, ast.Name):
            environment[target.id] = value
            return
        for name in root_names(target):
            environment[name] = value

    def shaped_values(node: ast.AST, environment: dict[str, bool]) -> dict[str, bool]:
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return {
                str(index): expression(item, environment)
                for index, item in enumerate(node.elts)
            }
        if isinstance(node, ast.Dict):
            result: dict[str, bool] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    result.update(shaped_values(value, environment))
                elif isinstance(key, ast.Constant) and isinstance(key.value, (str, int)):
                    result[str(key.value)] = expression(value, environment)
            return result
        if isinstance(node, ast.Name):
            prefix = f"{node.id}."
            combined = {**module_environment, **environment}
            return {
                key.removeprefix(prefix): value
                for key, value in combined.items()
                if key.startswith(prefix)
            }
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"deque", "list", "set", "tuple"} and node.args:
                return shaped_values(node.args[0], environment)
            callee = functions.get(terminal)
            if callee is not None and terminal not in active_shape_calls:
                call_environment: dict[str, bool] = {}
                parameters = (*callee.args.posonlyargs, *callee.args.args)
                for parameter, argument in zip(parameters, node.args, strict=False):
                    call_environment[parameter.arg] = expression(argument, environment)
                for keyword in node.keywords:
                    if keyword.arg is not None:
                        call_environment[keyword.arg] = expression(keyword.value, environment)
                active_shape_calls.add(terminal)
                result: dict[str, bool] = {}
                for returned in ast.walk(callee):
                    if isinstance(returned, ast.Return) and returned.value is not None:
                        result.update(shaped_values(returned.value, call_environment))
                active_shape_calls.remove(terminal)
                return result
        return {}

    def record_shape(
        target: ast.AST,
        value: ast.AST,
        environment: dict[str, bool],
    ) -> None:
        if not isinstance(target, ast.Name):
            return
        prefix = f"{target.id}."
        for key in [key for key in environment if key.startswith(prefix)]:
            del environment[key]
        for key, tainted in shaped_values(value, environment).items():
            environment[f"{target.id}.{key}"] = tainted

    def constructor_fields(
        call: ast.Call,
        class_name: str,
        environment: dict[str, bool],
    ) -> dict[str, bool]:
        initializer = class_methods.get((class_name, "__init__"))
        if initializer is None:
            return {}
        local: dict[str, bool] = {}
        parameters = [
            parameter
            for parameter in (*initializer.args.posonlyargs, *initializer.args.args)
            if parameter.arg not in {"self", "cls"}
        ]
        for parameter, argument in zip(parameters, call.args, strict=False):
            local[parameter.arg] = expression(argument, environment)
        for keyword in call.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = expression(keyword.value, environment)
        fields: dict[str, bool] = {}
        for assignment in ast.walk(initializer):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    fields[target.attr] = expression(assignment.value, local)
        return fields

    def expression(
        node: ast.AST | None,
        environment: dict[str, bool],
    ) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and node.value in seed_literals
        if isinstance(node, ast.Name):
            return environment.get(node.id, module_environment.get(node.id, False))
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and isinstance(
                node.slice, ast.Constant
            ) and isinstance(node.slice.value, (str, int)):
                exact = f"{node.value.id}.{node.slice.value}"
                if exact in environment:
                    return environment[exact]
                if exact in module_environment:
                    return module_environment[exact]
            if isinstance(node.slice, ast.Constant) and isinstance(
                node.slice.value, (str, int)
            ):
                shaped = shaped_values(node.value, environment)
                key = str(node.slice.value)
                if key in shaped:
                    return shaped[key]
            return expression(node.value, environment)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                exact = f"{node.value.id}.{node.attr}"
                if exact in environment:
                    return environment[exact]
                if exact in module_environment:
                    return module_environment[exact]
            return expression(node.value, environment)
        if isinstance(node, ast.Dict):
            tainted = False
            for key, value in zip(node.keys, node.values, strict=True):
                item_tainted = (
                    any(shaped_values(value, environment).values())
                    if key is None
                    else expression(value, environment)
                )
                tainted = tainted or item_tainted or (
                    key is not None and expression(key, environment)
                )
                if (
                    item_tainted
                    and isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and (display := structural_sinks.get(key.value.casefold())) is not None
                ):
                    findings.add(display)
            return tainted
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return any(expression(value, environment) for value in node.elts)
        if isinstance(node, ast.Call):
            raw_terminal = _task12_terminal(node.func) or ""
            resolved = _task12_resolved_name(node.func, alias_scopes[-1]) or raw_terminal
            terminal = resolved.rsplit(".", 1)[-1]
            if raw_terminal == "getattr" and len(node.args) >= 2:
                attribute = _literal_string(node.args[1])
                if attribute is None and isinstance(node.args[1], ast.Name):
                    attribute = string_scopes[-1].get(node.args[1].id)
                if attribute is not None:
                    fields = shaped_values(node.args[0], environment)
                    if attribute in fields:
                        return fields[attribute]
                return expression(node.args[0], environment)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
            ):
                shaped = shaped_values(node.func.value, environment)
                key = node.args[0]
                if isinstance(key, ast.Constant) and isinstance(
                    key.value, (str, int)
                ) and str(key.value) in shaped:
                    return shaped[str(key.value)]
                return expression(node.func.value, environment)
            if terminal == "vars" and node.args:
                return expression(node.args[0], environment) or any(
                    shaped_values(node.args[0], environment).values()
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {
                    "__copy__",
                    "copy",
                    "items",
                    "values",
                    "view",
                }
                and not node.args
                and not node.keywords
            ):
                return expression(node.func.value, environment)
            values = [expression(value, environment) for value in node.args]
            keyword_values = [
                (item.arg, expression(item.value, environment)) for item in node.keywords
            ]
            callee = nested_function_scopes[-1].get(raw_terminal) or functions.get(
                raw_terminal
            )
            receiver_fields: dict[str, bool] = {}
            method_call = False
            if isinstance(node.func, ast.Attribute):
                class_name: str | None = None
                if isinstance(node.func.value, ast.Name):
                    class_name = instance_scopes[-1].get(node.func.value.id)
                    if class_name is not None:
                        prefix = f"{node.func.value.id}."
                        receiver_fields = {
                            key.removeprefix(prefix): value
                            for key, value in environment.items()
                            if key.startswith(prefix)
                        }
                elif (
                    isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id in classes
                ):
                    class_name = node.func.value.func.id
                    receiver_fields = constructor_fields(
                        node.func.value, class_name, environment
                    )
                if class_name is not None:
                    callee = class_methods.get((class_name, node.func.attr))
                    method_call = callee is not None
            display = sinks.get(terminal.casefold()) if callee is None else None
            if display is not None and (any(values) or any(value for _, value in keyword_values)):
                findings.add(display)
                for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                    for key, item in shaped_values(argument, environment).items():
                        structural = structural_sinks.get(key.casefold())
                        if item and structural is not None:
                            findings.add(structural)
            if raw_terminal == "setattr" and len(node.args) >= 3 and values[2]:
                attribute = node.args[1]
                if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                    structural = structural_sinks.get(attribute.value.casefold())
                    if structural is not None:
                        findings.add(structural)
            if raw_terminal == "__setitem__" and len(node.args) >= 2:
                if isinstance(node.func, ast.Attribute):
                    assign_target(node.func.value, values[1], environment)
                    if (
                        isinstance(node.func.value, ast.Name)
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, (str, int))
                    ):
                        environment[
                            f"{node.func.value.id}.{node.args[0].value}"
                        ] = values[1]
                    if (
                        values[1]
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and (
                            structural := structural_sinks.get(
                                node.args[0].value.casefold()
                            )
                        )
                        is not None
                    ):
                        findings.add(structural)
                return values[1]
            if callee is not None:
                call_environment: dict[str, bool] = {
                    f"self.{key}": value for key, value in receiver_fields.items()
                }
                positional = (*callee.args.posonlyargs, *callee.args.args)
                if method_call and positional and positional[0].arg in {"self", "cls"}:
                    positional = positional[1:]
                regular_values: list[bool] = []
                starred_values: list[bool] = []
                for argument, value in zip(node.args, values, strict=True):
                    if isinstance(argument, ast.Starred):
                        expanded = shaped_values(argument.value, environment)
                        numeric = sorted(
                            (
                                (int(key), item)
                                for key, item in expanded.items()
                                if key.isdigit()
                            ),
                            key=lambda item: item[0],
                        )
                        if numeric:
                            regular_values.extend(item for _index, item in numeric)
                        else:
                            starred_values.append(value)
                    else:
                        regular_values.append(value)
                for parameter, value in zip(positional, regular_values, strict=False):
                    call_environment[parameter.arg] = value
                if callee.args.vararg is not None:
                    call_environment[callee.args.vararg.arg] = any(
                        (*regular_values[len(positional) :], *starred_values)
                    )
                named = {parameter.arg for parameter in (*positional, *callee.args.kwonlyargs)}
                extra_keywords: list[bool] = []
                for keyword_node, (keyword, value) in zip(
                    node.keywords, keyword_values, strict=True
                ):
                    if keyword is None:
                        expanded = shaped_values(keyword_node.value, environment)
                        for expanded_name, expanded_value in expanded.items():
                            if expanded_name in named:
                                call_environment[expanded_name] = expanded_value
                            else:
                                extra_keywords.append(expanded_value)
                    elif keyword in named:
                        call_environment[keyword] = value
                    else:
                        extra_keywords.append(value)
                if callee.args.kwarg is not None:
                    call_environment[callee.args.kwarg.arg] = any(extra_keywords)
                returned = execute(callee, call_environment)
                for parameter, argument in zip(positional, node.args, strict=False):
                    if call_environment.get(parameter.arg, False):
                        assign_target(argument, True, environment)
                return returned
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear"
            ):
                assign_target(node.func.value, False, environment)
                for root in root_names(node.func.value):
                    prefix = f"{root}."
                    for key in [key for key in environment if key.startswith(prefix)]:
                        del environment[key]
                return False
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "discard",
                "popleft",
                "pop",
            }:
                shaped = shaped_values(node.func.value, environment)
                if node.func.attr == "discard":
                    if any(values):
                        if shaped:
                            removed_key = next(
                                (key for key, value in shaped.items() if value), None
                            )
                            if removed_key is not None and isinstance(
                                node.func.value, ast.Name
                            ):
                                environment.pop(
                                    f"{node.func.value.id}.{removed_key}", None
                                )
                            assign_target(
                                node.func.value,
                                any(
                                    value
                                    for key, value in shaped.items()
                                    if key != removed_key
                                ),
                                environment,
                            )
                        else:
                            assign_target(node.func.value, False, environment)
                    return False
                removed = False
                if shaped and isinstance(node.func.value, ast.Name):
                    if node.func.attr == "popleft":
                        numeric = [int(key) for key in shaped if key.isdigit()]
                        key = str(min(numeric)) if numeric else next(iter(shaped))
                    elif node.args and isinstance(node.args[0], ast.Constant):
                        key = str(node.args[0].value)
                    else:
                        numeric = [int(key) for key in shaped if key.isdigit()]
                        key = str(max(numeric)) if numeric else next(reversed(shaped))
                    removed = shaped.get(key, False)
                    environment.pop(f"{node.func.value.id}.{key}", None)
                    assign_target(
                        node.func.value,
                        any(value for item, value in shaped.items() if item != key),
                        environment,
                    )
                else:
                    removed = expression(node.func.value, environment)
                    assign_target(node.func.value, False, environment)
                return removed
            if terminal == "heappush" and len(node.args) >= 2 and values[1]:
                assign_target(node.args[0], True, environment)
                if isinstance(node.args[0], ast.Name):
                    prefix = f"{node.args[0].id}."
                    existing = [
                        int(key.removeprefix(prefix))
                        for key in environment
                        if key.startswith(prefix)
                        and key.removeprefix(prefix).isdigit()
                    ]
                    for index in existing:
                        environment[f"{prefix}{index}"] = True
                    environment[f"{prefix}{max(existing, default=-1) + 1}"] = True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "append",
                "appendleft",
                "extend",
                "insert",
            }:
                if isinstance(node.func.value, ast.Name):
                    prefix = f"{node.func.value.id}."
                    existing = {
                        int(key.removeprefix(prefix))
                        for key in environment
                        if key.startswith(prefix)
                        and key.removeprefix(prefix).isdigit()
                    }
                    next_index = max(existing, default=-1) + 1
                    insertion_index: int | None = None
                    if node.func.attr == "appendleft":
                        insertion_index = 0
                    elif (
                        node.func.attr == "insert"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, int)
                    ):
                        insertion_index = max(0, node.args[0].value)
                    if insertion_index is not None:
                        for index in sorted(existing, reverse=True):
                            if index >= insertion_index:
                                environment[f"{prefix}{index + 1}"] = environment.pop(
                                    f"{prefix}{index}"
                                )
                        environment[f"{prefix}{insertion_index}"] = values[-1]
                    appended = (
                        shaped_values(node.args[0], environment)
                        if node.func.attr == "extend" and node.args
                        else {}
                    )
                    if insertion_index is not None:
                        pass
                    elif appended:
                        for offset, item in sorted(
                            (
                                (int(key), item)
                                for key, item in appended.items()
                                if key.isdigit()
                            )
                        ):
                            environment[f"{prefix}{next_index + offset}"] = item
                    elif node.args:
                        environment[f"{prefix}{next_index}"] = values[-1]
                    assign_target(
                        node.func.value,
                        any(
                            value
                            for key, value in environment.items()
                            if key.startswith(prefix)
                            and key.removeprefix(prefix).isdigit()
                        ),
                        environment,
                    )
                elif any(values) or any(value for _, value in keyword_values):
                    assign_target(node.func.value, True, environment)
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"add", "setdefault", "update"}
                and (any(values) or any(value for _, value in keyword_values))
            ):
                assign_target(node.func.value, True, environment)
            return any(values) or any(value for _, value in keyword_values)
        return any(expression(child, environment) for child in ast.iter_child_nodes(node))

    def execute_statements(statements: list[ast.stmt], environment: dict[str, bool]) -> bool:
        returned = False
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested_function_scopes[-1][statement.name] = statement
                closure_environments[id(statement)] = dict(environment)
                function_identities.setdefault(
                    id(statement),
                    f"nested:{getattr(statement, 'lineno', 0)}:{statement.name}",
                )
                continue
            if isinstance(statement, ast.ClassDef):
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                before_findings = set(findings)
                value = expression(statement.value, environment)
                findings.difference_update(
                    (findings - before_findings) & structural_displays
                )
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                for target in targets:
                    assign_target(target, value, environment)
                    record_shape(target, statement.value, environment)
                    if isinstance(target, ast.Subscript) and isinstance(
                        target.value, ast.Name
                    ):
                        prefix = f"{target.value.id}."
                        environment[target.value.id] = any(
                            item
                            for key, item in environment.items()
                            if key.startswith(prefix)
                        )
                    if (
                        isinstance(target, ast.Name)
                        and isinstance(statement.value, ast.Call)
                        and isinstance(statement.value.func, ast.Name)
                        and statement.value.func.id in classes
                    ):
                        class_name = statement.value.func.id
                        instance_scopes[-1][target.id] = class_name
                        for field, tainted in constructor_fields(
                            statement.value, class_name, environment
                        ).items():
                            environment[f"{target.id}.{field}"] = tainted
                    if isinstance(target, ast.Attribute) and value:
                        display = structural_sinks.get(target.attr.casefold())
                        if display is not None:
                            findings.add(display)
            elif isinstance(statement, ast.AugAssign):
                before_findings = set(findings)
                value = expression(statement.value, environment)
                findings.difference_update(
                    (findings - before_findings) & structural_displays
                )
                assign_target(
                    statement.target,
                    expression(statement.target, environment) or value,
                    environment,
                )
                if isinstance(statement.op, ast.BitOr) and isinstance(
                    statement.target, ast.Name
                ):
                    for key, item in shaped_values(
                        statement.value, environment
                    ).items():
                        environment[f"{statement.target.id}.{key}"] = item
            elif isinstance(statement, ast.Expr):
                expression(statement.value, environment)
            elif isinstance(statement, ast.Return):
                value = expression(statement.value, environment)
                for key, item in shaped_values(statement.value, environment).items():
                    structural = structural_sinks.get(key.casefold())
                    if item and structural is not None:
                        findings.add(structural)
                returned = returned or value
            elif isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With)):
                branches = [statement.body]
                if isinstance(statement, ast.If):
                    expression(statement.test, environment)
                    if isinstance(statement.test, ast.Constant) and isinstance(
                        statement.test.value, bool
                    ):
                        branches = [
                            statement.body if statement.test.value else statement.orelse
                        ]
                    else:
                        branches.append(statement.orelse)
                for branch in branches:
                    branch_environment = dict(environment)
                    returned = returned or execute_statements(branch, branch_environment)
                    for name, value in branch_environment.items():
                        environment[name] = environment.get(name, False) or value
            else:
                for child in ast.iter_child_nodes(statement):
                    if isinstance(child, ast.expr):
                        expression(child, environment)
        return returned

    def execute(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        environment: dict[str, bool],
    ) -> bool:
        captured = closure_environments.get(id(function))
        if captured is not None:
            passed = dict(environment)
            environment.clear()
            environment.update(captured)
            environment.update(passed)
        parameters = (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
        for parameter in parameters:
            environment.setdefault(parameter.arg, parameter.arg in seed_names)
        if function.args.vararg is not None:
            environment.setdefault(
                function.args.vararg.arg, function.args.vararg.arg in seed_names
            )
        if function.args.kwarg is not None:
            environment.setdefault(function.args.kwarg.arg, function.args.kwarg.arg in seed_names)
        key = (
            function_identities.get(id(function), f"function:{function.name}"),
            tuple(sorted(environment.items())),
        )
        if key in return_cache:
            return return_cache[key]
        if key in active_calls:
            return any(environment.values())
        active_calls.add(key)
        instance_scopes.append({})
        alias_scopes.append(function_aliases(function))
        function_strings, _function_mappings = _task12_callable_return_bindings(
            tree,
            function,
        )
        string_scopes.append(function_strings)
        nested_function_scopes.append({})
        try:
            returned = execute_statements(function.body, environment)
        finally:
            nested_function_scopes.pop()
            string_scopes.pop()
            alias_scopes.pop()
            instance_scopes.pop()
        active_calls.remove(key)
        return_cache[key] = returned
        return returned

    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            before_findings = set(findings)
            value = expression(statement.value, module_environment)
            findings.difference_update(
                (findings - before_findings) & structural_displays
            )
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                assign_target(target, value, module_environment)
                record_shape(target, statement.value, module_environment)
    for function in functions.values():
        execute(
            function,
            {
                parameter.arg: parameter.arg in seed_names
                for parameter in (
                    *function.args.posonlyargs,
                    *function.args.args,
                    *function.args.kwonlyargs,
                )
            },
        )
    execute_statements(tree.body, module_environment)
    if "transport" in findings:
        findings.discard("visible")
    return findings


def _task12_cross_module_taint_findings(
    sources: dict[str, str],
    *,
    origin_seed_names: set[str],
    seed_literals: set[str],
    sinks: dict[str, str],
    structural_sinks: dict[str, str],
) -> set[tuple[str, str]]:
    trees = {name: _task12_tree(name, source) for name, source in sources.items()}
    imports = {name: _task12_module_import_aliases(tree)[0] for name, tree in trees.items()}
    callables: dict[
        tuple[str, str],
        ast.FunctionDef | ast.AsyncFunctionDef,
    ] = {}
    owners: dict[tuple[str, str], ast.ClassDef | None] = {}
    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callables[module, node.name] = node
                owners[module, node.name] = None
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        callables[module, f"{node.name}.{member.name}"] = member
                        owners[module, f"{node.name}.{member.name}"] = node

    def imported_target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def follow_export(
        target: tuple[str, str],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[str, str]:
        if target in seen:
            return target
        module, symbol = target
        resolved = imports[module].get(symbol)
        nested = imported_target(resolved) if resolved is not None else None
        return follow_export(nested, seen | {target}) if nested is not None else target

    def target(
        module: str,
        node: ast.AST,
        scope_aliases: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
            constructor = target(module, node.value.func, scope_aliases)
            if constructor is not None:
                constructor = follow_export(constructor)
                return constructor[0], f"{constructor[1]}.{node.attr}"
        resolved = _task12_resolved_name(node, scope_aliases or imports[module]) or ""
        imported = imported_target(resolved)
        if imported is not None:
            return follow_export(imported)
        terminal = resolved.rsplit(".", 1)[-1]
        return (module, terminal) if (module, terminal) in callables else None

    findings: set[tuple[str, str]] = set()
    for origin in trees:
        initial = [
            (origin, symbol, frozenset(origin_seed_names & {arg.arg for arg in (*body.args.posonlyargs, *body.args.args, *body.args.kwonlyargs)}))
            for module, symbol in callables
            if module == origin
            for body in [callables[module, symbol]]
        ]
        pending = initial
        seen: set[tuple[str, str, frozenset[str]]] = set()
        while pending:
            module, symbol, seeded = pending.pop()
            state = (module, symbol, seeded)
            if state in seen:
                continue
            seen.add(state)
            body = callables[module, symbol]
            scope_aliases = _task12_scope_aliases(imports[module], body)
            import_nodes = [
                node
                for node in trees[module].body
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
            ]
            owner = owners[module, symbol]
            scoped_body: ast.stmt = (
                ast.ClassDef(
                    name=owner.name,
                    bases=[],
                    keywords=[],
                    body=[body],
                    decorator_list=[],
                )
                if owner is not None
                else body
            )
            scoped_tree = ast.Module(body=[*import_nodes, scoped_body], type_ignores=[])
            local_findings = _task12_taint_sink_findings(
                scoped_tree,
                seed_names=set(seeded),
                seed_literals=seed_literals if module == origin else set(),
                sinks=sinks,
                structural_sinks=structural_sinks,
            )
            if module != origin:
                findings.update((origin, display) for display in local_findings)

            environment = {name: name in seeded for name in seeded}
            strings, _mappings = _task12_callable_return_bindings(
                trees[module],
                body,
            )

            def shaped(node: ast.AST) -> dict[str, bool]:
                if isinstance(node, (ast.List, ast.Tuple)):
                    return {
                        str(index): tainted(value)
                        for index, value in enumerate(node.elts)
                    }
                if isinstance(node, ast.Dict):
                    result: dict[str, bool] = {}
                    for key, value in zip(node.keys, node.values, strict=True):
                        if key is None:
                            result.update(shaped(value))
                        elif isinstance(key, ast.Constant) and isinstance(
                            key.value, (str, int)
                        ):
                            result[str(key.value)] = tainted(value)
                    return result
                if isinstance(node, ast.Name):
                    prefix = f"{node.id}."
                    return {
                        key.removeprefix(prefix): value
                        for key, value in environment.items()
                        if key.startswith(prefix)
                    }
                return {}

            def tainted(node: ast.AST | None) -> bool:
                if node is None:
                    return False
                if isinstance(node, ast.Constant):
                    return isinstance(node.value, str) and node.value in seed_literals
                if isinstance(node, ast.Name):
                    return environment.get(node.id, False)
                if isinstance(node, ast.Subscript):
                    if isinstance(node.value, ast.Name) and isinstance(
                        node.slice, ast.Constant
                    ) and isinstance(node.slice.value, (str, int)):
                        exact = f"{node.value.id}.{node.slice.value}"
                        if exact in environment:
                            return environment[exact]
                    return tainted(node.value)
                if isinstance(node, ast.Call):
                    if (_task12_terminal(node.func) or "") == "getattr" and len(
                        node.args
                    ) >= 2:
                        attribute = _literal_string(node.args[1])
                        if attribute is None and isinstance(node.args[1], ast.Name):
                            attribute = strings.get(node.args[1].id)
                        if attribute is not None:
                            fields = shaped(node.args[0])
                            if attribute in fields:
                                return fields[attribute]
                        return tainted(node.args[0])
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and node.args
                    ):
                        values = shaped(node.func.value)
                        key = node.args[0]
                        if isinstance(key, ast.Constant) and isinstance(
                            key.value, (str, int)
                        ) and str(key.value) in values:
                            return values[str(key.value)]
                        return tainted(node.func.value)
                    if (_task12_terminal(node.func) or "") == "vars" and node.args:
                        return tainted(node.args[0]) or any(
                            shaped(node.args[0]).values()
                        )
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in {
                            "__copy__",
                            "copy",
                            "items",
                            "values",
                            "view",
                        }
                        and not node.args
                        and not node.keywords
                    ):
                        return tainted(node.func.value)
                    positional_values: list[bool] = []
                    for argument in node.args:
                        if isinstance(argument, ast.Starred):
                            expanded = shaped(argument.value)
                            positional_values.extend(
                                value
                                for _index, value in sorted(
                                    (
                                        (int(key), value)
                                        for key, value in expanded.items()
                                        if key.isdigit()
                                    )
                                )
                            )
                        else:
                            positional_values.append(tainted(argument))
                    called = target(module, node.func, scope_aliases)
                    if called is not None and called in callables:
                        callee = callables[called]
                        parameters = [
                            argument.arg
                            for argument in (*callee.args.posonlyargs, *callee.args.args)
                            if argument.arg not in {"self", "cls"}
                        ]
                        named_values = dict(
                            zip(parameters, positional_values, strict=False)
                        )
                        for keyword in node.keywords:
                            if keyword.arg is None:
                                named_values.update(shaped(keyword.value))
                            else:
                                named_values[keyword.arg] = tainted(keyword.value)
                        seeded_parameters = frozenset(
                            parameter for parameter in parameters if named_values.get(parameter)
                        )
                        pending.append((called[0], called[1], seeded_parameters))
                    return any(positional_values) or any(
                        tainted(keyword.value) for keyword in node.keywords
                    )
                return any(tainted(child) for child in ast.iter_child_nodes(node))

            for statement in ast.walk(body):
                if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                    value = tainted(statement.value)
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for assignment_target in targets:
                        if isinstance(assignment_target, ast.Name):
                            environment[assignment_target.id] = value
                            prefix = f"{assignment_target.id}."
                            for key in [key for key in environment if key.startswith(prefix)]:
                                del environment[key]
                            for key, item in shaped(statement.value).items():
                                environment[f"{assignment_target.id}.{key}"] = item
                        elif (
                            isinstance(assignment_target, ast.Subscript)
                            and isinstance(assignment_target.value, ast.Name)
                            and isinstance(assignment_target.slice, ast.Constant)
                            and isinstance(assignment_target.slice.value, (str, int))
                        ):
                            environment[
                                f"{assignment_target.value.id}.{assignment_target.slice.value}"
                            ] = value
                        elif (
                            isinstance(assignment_target, ast.Attribute)
                            and isinstance(assignment_target.value, ast.Name)
                        ):
                            environment[
                                f"{assignment_target.value.id}.{assignment_target.attr}"
                            ] = value
                elif isinstance(statement, ast.Call):
                    tainted(statement)
    return findings


def _privacy_canary_violations(sources: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for name, source in sources.items():
        tree = _task12_tree(name, source)
        findings.extend(
            f"{name}:{sink}"
            for sink in _task12_taint_sink_findings(
                tree,
                seed_names=set(),
                seed_literals={_TASK12_PRIVACY_CANARY},
                sinks=_TASK12_PRIVACY_SINKS,
                structural_sinks={
                    "error": "error",
                    "event": "event",
                    "http": "http",
                    "journal": "journal",
                    "manifest": "manifest",
                    "result": "result",
                    "snapshot": "snapshot",
                    "terminal_result": "terminal_result",
                    "transport": "transport",
                    "undo": "undo",
                    "visible": "visible",
                },
            )
        )
    cross_module = _task12_cross_module_taint_findings(
        sources,
        origin_seed_names=set(),
        seed_literals={_TASK12_PRIVACY_CANARY},
        sinks=_TASK12_PRIVACY_SINKS,
        structural_sinks={
            "error": "error",
            "event": "event",
            "http": "http",
            "journal": "journal",
            "manifest": "manifest",
            "result": "result",
            "snapshot": "snapshot",
            "terminal_result": "terminal_result",
            "transport": "transport",
            "undo": "undo",
            "visible": "visible",
        },
    )
    findings.extend(f"{origin}:{sink}" for origin, sink in cross_module)
    return sorted(set(findings))


def _task12_previous_title_reaches_public_sink(
    tree: ast.Module,
    *,
    allow_story_parent_undo: bool = False,
) -> bool:
    structural = {
        "error": "error",
        "event": "event",
        "http": "http",
        "journal": "journal",
        "manifest": "manifest",
        "result": "result",
        "result_json": "result_json",
        "snapshot": "snapshot",
        "terminal": "terminal",
        "terminal_result": "terminal_result",
        "transport": "transport",
        "transport_json": "transport_json",
        "undo": "undo",
        "undo_json": "undo_json",
        "visible": "visible",
        "visible_result": "visible_result",
    }
    exact_sinks = dict(_TASK12_PRIVACY_SINKS)
    if allow_story_parent_undo:
        exact_sinks.pop("writeoperation", None)
        structural.pop("undo", None)
        structural.pop("undo_json", None)
    findings = _task12_taint_sink_findings(
            tree,
            seed_names={"previous_title"},
            seed_literals=set(),
            sinks=exact_sinks,
            structural_sinks=structural,
        )
    if findings or not allow_story_parent_undo:
        return bool(findings)
    aliases, _modules = _task12_module_import_aliases(tree)
    for _ in range(8):
        changed = False
        for assignment in tree.body:
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
                assignment.value, (ast.Name, ast.Attribute)
            ):
                continue
            resolved = _task12_resolved_name(assignment.value, aliases)
            if resolved is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
        if not changed:
            break
    tainted_names = {"previous_title"}
    for _ in range(8):
        changed = False
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            if not any(
                isinstance(child, ast.Name) and child.id in tainted_names
                for child in ast.walk(assignment.value)
            ):
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in tainted_names:
                    tainted_names.add(target.id)
                    changed = True
        if not changed:
            break
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        owner = parents.get(call)
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = parents.get(owner)
        call_aliases = (
            _task12_scope_aliases(aliases, owner)
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            else aliases
        )
        terminal = (_task12_resolved_name(call.func, call_aliases) or "").rsplit(".", 1)[-1]
        if terminal != "WriteOperation" or not any(
            isinstance(child, ast.Name) and child.id in tainted_names
            for child in ast.walk(call)
        ):
            continue
        values = {
            keyword.arg: _task12_text(keyword.value, {})
            for keyword in call.keywords
            if keyword.arg is not None
        }
        if not (
            values.get("operation_role") == "primary"
            and values.get("adapter_kind") == "product_action"
            and values.get("tool_name") == "confirm_interview_story"
            and any(keyword.arg == "undo_json" for keyword in call.keywords)
        ):
            return True
    return False


def _task12_previous_title_cross_module_origins(sources: dict[str, str]) -> set[str]:
    structural = {
        "error": "error",
        "event": "event",
        "http": "http",
        "journal": "journal",
        "manifest": "manifest",
        "result": "result",
        "result_json": "result_json",
        "snapshot": "snapshot",
        "terminal": "terminal",
        "terminal_result": "terminal_result",
        "transport": "transport",
        "transport_json": "transport_json",
        "undo": "undo",
        "undo_json": "undo_json",
        "visible": "visible",
        "visible_result": "visible_result",
    }
    return {
        origin
        for origin, _sink in _task12_cross_module_taint_findings(
            sources,
            origin_seed_names={"previous_title"},
            seed_literals=set(),
            sinks=_TASK12_PRIVACY_SINKS,
            structural_sinks=structural,
        )
    }


def _task12_previous_title_sources_reach_public_sink(sources: dict[str, str]) -> bool:
    return bool(_task12_previous_title_cross_module_origins(sources))


def _task12_story_smoke_violations(source: str) -> list[str]:
    tree = ast.parse(source, filename="smoke.py")
    helper = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_create_and_confirm_story_proposal"
        ),
        None,
    )
    if helper is None:
        return ["smoke:missing-story-helper"]
    findings: list[str] = []
    arguments = {
        item.arg
        for item in (
            *helper.args.posonlyargs,
            *helper.args.args,
            *helper.args.kwonlyargs,
        )
    }
    active_strings = {
        node.value
        for node in ast.walk(helper)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    if (
        "confirmation_token" in arguments
        or {
            "story-ui-confirm-0001",
            "story-pilot-confirm-01",
        }
        & active_strings
    ):
        findings.append("smoke:client-generated-token")

    calls = [node for node in ast.walk(helper) if isinstance(node, ast.Call)]
    if any(
        "/api/interview-story-proposals/" in ast.unparse(call) and "/confirm" in ast.unparse(call)
        for call in calls
    ):
        findings.append("smoke:legacy-confirm-route")

    identity_fields = {
        node.value
        for node in ast.walk(helper)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    if not {
        "product_action",
        "operation_id",
        "action_call_id",
        "confirmation_token",
    }.issubset(identity_fields):
        findings.append("smoke:missing-server-product-action-identity")

    decision_calls = [
        call
        for call in calls
        if (
            ("/api/product-actions/" in ast.unparse(call) and "/decisions" in ast.unparse(call))
            or (
                bool(call.args)
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "decision_url"
            )
        )
    ]
    decision_calls.sort(key=lambda call: (call.lineno, call.col_offset))
    request_values = [
        ast.dump(keyword.value, include_attributes=False)
        for call in decision_calls
        for keyword in call.keywords
        if keyword.arg == "json"
    ]
    if len(decision_calls) != 2 or len(set(request_values)) != 1:
        findings.append("smoke:missing-exact-decision-replay")
    parents = {
        child: parent for parent in ast.walk(helper) for child in ast.iter_child_nodes(parent)
    }
    if decision_calls and not isinstance(parents.get(decision_calls[0]), ast.Expr):
        findings.append("smoke:first-decision-response-consumed")

    strict_token_absence_checks = sum(
        1
        for node in ast.walk(helper)
        if isinstance(node, ast.Compare)
        and any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops)
        and any(
            isinstance(child, ast.Constant) and child.value == "confirmation_token"
            for child in ast.walk(node)
        )
    )
    if strict_token_absence_checks < 2:
        findings.append("smoke:missing-token-absence-check")
    if not {
        "story_generic_proposed_recovery",
        "story_source_owner_recovery",
        "story_source_owner_replay",
    }.issubset(active_strings):
        findings.append("smoke:missing-source-bound-recovery")
    required_nonowner_scans = {
        "story_before_decision_http": ("story_before_decision", "privacy_canary"),
        "story_pending_generic_http": ("generic_proposed_body", "privacy_canary"),
        "story_after_source_owner_replay_http": (
            "story_after_source_replay",
            "privacy_canary",
        ),
        "story_source_owner_http_previous_title": (
            "source_owner_body",
            "previous_title_canary",
        ),
        "story_source_replay_http_previous_title": (
            "source_replay_body",
            "previous_title_canary",
        ),
        "story_pending_generic_http_previous_title": (
            "generic_proposed_body",
            "previous_title_canary",
        ),
        "story_after_decision_http": ("story_after_decision", "privacy_canary"),
        "story_after_decision_http_previous_title": (
            "story_after_decision",
            "previous_title_canary",
        ),
        "story_recovery_http": ("recovery_body", "privacy_canary"),
        "story_recovery_http_previous_title": (
            "recovery_body",
            "previous_title_canary",
        ),
        "story_terminal_http": ("replay_body", "privacy_canary"),
        "story_terminal_http_previous_title": (
            "replay_body",
            "previous_title_canary",
        ),
        "story_replay_recovery_http": (
            "replay_recovery_body",
            "privacy_canary",
        ),
        "story_replay_recovery_http_previous_title": (
            "replay_recovery_body",
            "previous_title_canary",
        ),
        "story_visible_http": ("story_after_replay", "privacy_canary"),
        "story_visible_http_previous_title": (
            "story_after_replay",
            "previous_title_canary",
        ),
    }
    observed_nonowner_scans: set[str] = set()
    for call in calls:
        if (
            _task12_terminal(call.func) != "_assert_task12_privacy_canary_absent"
            or len(call.args) < 3
            or not isinstance(call.args[1], ast.Name)
            or not isinstance(call.args[2], ast.Constant)
            or not isinstance(call.args[2].value, str)
        ):
            continue
        expected = required_nonowner_scans.get(call.args[2].value)
        if expected is None or call.args[1].id != expected[1]:
            continue
        roots = {
            child.id for child in ast.walk(call.args[0]) if isinstance(child, ast.Name)
        }
        if expected[0] in roots:
            observed_nonowner_scans.add(call.args[2].value)
    if set(required_nonowner_scans) != observed_nonowner_scans:
        findings.append("smoke:missing-nonowner-http-canary-scan")

    if not (
        any(isinstance(node, ast.Constant) and node.value == 26 for node in ast.walk(helper))
        and {
            "confirm_interview_story",
            "save_review_readiness_signal",
        }.issubset(active_strings)
        and "provider_names" in {node.id for node in ast.walk(helper) if isinstance(node, ast.Name)}
    ):
        findings.append("smoke:missing-provider-26-check")
    if "real_ai_model_provider_counters_unobservable" not in active_strings:
        findings.append("smoke:missing-real-ai-exclusion")
    privacy_canary_from_server_token = any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "privacy_canary"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
        and isinstance(node.value, ast.Subscript)
        and any(
            isinstance(child, ast.Constant) and child.value == "confirmation_token"
            for child in ast.walk(node.value)
        )
        for node in ast.walk(helper)
    )
    if not privacy_canary_from_server_token or not any(
        isinstance(node, ast.Call)
        and _task12_terminal(node.func) == "_assert_task12_privacy_canary_absent"
        for node in ast.walk(helper)
    ):
        findings.append("smoke:missing-runtime-privacy-canary")
    story_runner = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_run_interview_story_http_smoke"
        ),
        None,
    )
    if story_runner is None or not any(
        isinstance(node, ast.Call)
        and _task12_terminal(node.func)
        == "_assert_task12_runtime_privacy_canary_absent"
        for node in ast.walk(story_runner)
    ):
        findings.append("smoke:missing-runtime-persistence-privacy-scan")
    runtime_manifest_exact = any(
        isinstance(node, ast.Compare)
        and any(
            isinstance(child, ast.Tuple)
            and [
                item.value
                for item in child.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, int)
            ]
            == [3, 5, 2, 2]
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )
    if not runtime_manifest_exact:
        findings.append("smoke:missing-runtime-exact-manifests")

    names = {node.id for node in ast.walk(helper) if isinstance(node, ast.Name)}
    if not {
        "counters_observable",
        "model_calls_before",
        "provider_calls_before",
        "story_after_decision",
        "story_after_replay",
    }.issubset(names):
        findings.append("smoke:missing-decision-replay-invariants")
    return sorted(set(findings))


def _task12_registry_callgraph_counts(
    trees: dict[str, ast.Module],
) -> dict[str, int]:
    """Count registry construction along executable module-root call paths."""
    registry_types = {
        "ProductActionCatalogV1",
        "ProductActionCompensationCatalogV1",
        "ProductActionCompensationProofRegistryV1",
        "ProductActionProofRegistryV1",
    }

    def range_count(node: ast.AST) -> int | None:
        if not isinstance(node, ast.Call) or (_task12_terminal(node.func) or "") != "range":
            return None
        values = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, int)
        ]
        if len(values) != len(node.args) or not 1 <= len(values) <= 3:
            return None
        return len(range(*values))
    imports = {
        module: _task12_module_import_aliases(tree)[0]
        for module, tree in trees.items()
    }
    callables: dict[
        tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef
    ] = {}
    closure_owners: dict[tuple[str, str], tuple[str, str]] = {}
    nested_callables: dict[tuple[str, str, str], tuple[str, str]] = {}

    def register_callable(
        module: str,
        symbol: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        callables[module, symbol] = node
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nested_symbol = f"{symbol}.<locals>.{statement.name}"
            nested = (module, nested_symbol)
            closure_owners[nested] = (module, symbol)
            nested_callables[module, symbol, statement.name] = nested
            register_callable(module, nested_symbol, statement)

    for module, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                register_callable(module, node.name, node)
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        register_callable(
                            module, f"{node.name}.{member.name}", member
                        )

    imported_target_cache: dict[str, tuple[str, str] | None] = {}

    def imported_target(resolved: str) -> tuple[str, str] | None:
        if resolved in imported_target_cache:
            return imported_target_cache[resolved]
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        result = (matches[-1][1], matches[-1][2]) if matches else None
        imported_target_cache[resolved] = result
        return result

    module_assignments: dict[str, dict[str, ast.AST]] = {}
    for module, tree in trees.items():
        assignments: dict[str, ast.AST] = {}
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = statement.value
        module_assignments[module] = assignments

    CallableTarget = tuple[str, str]
    registry_module = "<registry>"
    returned_target_cache: dict[
        tuple[
            CallableTarget,
            str | None,
            bool,
            tuple[tuple[str, CallableTarget], ...],
        ],
        frozenset[CallableTarget],
    ] = {}

    def resolve(
        module: str,
        node: ast.AST,
        bindings: dict[str, CallableTarget],
        assignments: dict[str, ast.AST],
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> CallableTarget | None:
        if isinstance(node, ast.Call):
            if (_task12_terminal(node.func) or "") == "partial" and node.args:
                return resolve(module, node.args[0], bindings, assignments, seen)
            target = resolve(module, node.func, bindings, assignments, seen)
            if target is None or target[0] == registry_module:
                return target
            returned = returned_target(target, bindings, seen)
            return returned or target
        if isinstance(node, ast.Subscript):
            key = (
                node.slice.value
                if isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                else None
            )
            if key is None:
                return None
            if isinstance(node.value, ast.Call):
                owner = resolve(
                    module, node.value.func, bindings, assignments, seen
                )
                if owner is not None:
                    return returned_target(owner, bindings, seen, key=key)
            return None
        if isinstance(node, ast.Attribute):
            resolved_binding = _task12_resolved_name(node, imports[module]) or ""
            if resolved_binding in bindings:
                return bindings[resolved_binding]
            owner_node = node.value.func if isinstance(node.value, ast.Call) else node.value
            owner = resolve(module, owner_node, bindings, assignments, seen)
            if owner is not None and owner[0] != registry_module:
                return owner[0], f"{owner[1]}.{node.attr}"
            resolved = _task12_resolved_name(node, imports[module]) or ""
            return imported_target(resolved)
        if not isinstance(node, ast.Name):
            return None
        if node.id in bindings:
            return bindings[node.id]
        key = (module, node.id)
        if node.id in assignments and key not in seen:
            return resolve(
                module,
                assignments[node.id],
                bindings,
                assignments,
                seen | {key},
            )
        resolved = imports[module].get(node.id, node.id)
        terminal = resolved.rsplit(".", 1)[-1]
        if terminal in registry_types:
            return registry_module, terminal
        imported = imported_target(resolved)
        if imported is not None:
            imported_key = (imported[0], imported[1])
            nested = imports[imported[0]].get(imported[1])
            if nested is not None and imported_key not in seen:
                nested_target = resolve(
                    imported[0],
                    ast.Name(id=imported[1]),
                    {},
                    module_assignments[imported[0]],
                    seen | {imported_key},
                )
                if nested_target is not None:
                    return nested_target
            return imported
        if (module, node.id) in callables:
            return module, node.id
        return None

    def returned_target(
        target: CallableTarget,
        bindings: dict[str, CallableTarget],
        seen: frozenset[tuple[str, str]],
        *,
        key: str | None = None,
    ) -> CallableTarget | None:
        candidates = returned_targets(target, bindings, seen, key=key)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def returned_targets(
        target: CallableTarget,
        bindings: dict[str, CallableTarget],
        seen: frozenset[tuple[str, str]],
        *,
        key: str | None = None,
        all_values: bool = False,
    ) -> set[CallableTarget]:
        if target in seen:
            return set()
        cache_key = (target, key, all_values, tuple(sorted(bindings.items())))
        if cache_key in returned_target_cache:
            return set(returned_target_cache[cache_key])
        function = callables.get(target)
        if function is None:
            returned_target_cache[cache_key] = frozenset()
            return set()
        assignments = dict(module_assignments[target[0]])
        strings, mappings = _task12_callable_return_bindings(
            trees[target[0]],
            function,
        )
        for assignment in ast.walk(function):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for assignment_target in targets:
                if isinstance(assignment_target, ast.Name):
                    assignments[assignment_target.id] = assignment.value
        candidates: set[CallableTarget] = set()
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
                    nested_callables.get((target[0], target[1], option.id))
                    if isinstance(option, ast.Name)
                    else None
                ) or resolve(
                    target[0],
                    option,
                    bindings,
                    assignments,
                    seen | {target},
                )
                if candidate is not None:
                    candidates.add(candidate)
        returned_target_cache[cache_key] = frozenset(candidates)
        return candidates

    def iterable_options(
        module: str,
        node: ast.AST,
        bindings: dict[str, CallableTarget],
        assignments: dict[str, ast.AST],
    ) -> set[CallableTarget]:
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(module, node.args[0], bindings, assignments)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"items", "popitem", "values"}
            ):
                owner_node = (
                    node.func.value.func
                    if isinstance(node.func.value, ast.Call)
                    else node.func.value
                )
                owners = callable_options(
                    module,
                    owner_node,
                    bindings,
                    assignments,
                )
                return {
                    candidate
                    for owner in owners
                    for candidate in returned_targets(
                        owner,
                        bindings,
                        frozenset(),
                        all_values=True,
                    )
                }
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return {
                candidate
                for generator in node.generators
                for candidate in iterable_options(
                    module,
                    generator.iter,
                    bindings,
                    assignments,
                )
            }
        return set()

    def callable_options(
        module: str,
        node: ast.AST,
        bindings: dict[str, CallableTarget],
        assignments: dict[str, ast.AST],
    ) -> set[CallableTarget]:
        if isinstance(node, ast.IfExp):
            return callable_options(
                module, node.body, bindings, assignments
            ) | callable_options(module, node.orelse, bindings, assignments)
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            if terminal in {"iter", "list", "next", "set", "tuple"} and node.args:
                return iterable_options(module, node.args[0], bindings, assignments)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"items", "popitem", "values"}
            ):
                return iterable_options(module, node, bindings, assignments)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                owner_node = (
                    node.func.value.func
                    if isinstance(node.func.value, ast.Call)
                    else node.func.value
                )
                owners = callable_options(
                    module, owner_node, bindings, assignments
                )
                return {
                    candidate
                    for owner in owners
                    for candidate in returned_targets(
                        owner,
                        bindings,
                        frozenset(),
                        key=node.args[0].value,
                    )
                }
            bases = callable_options(module, node.func, bindings, assignments)
            return {
                candidate
                for base in bases
                for candidate in (
                    returned_targets(base, bindings, frozenset()) or {base}
                )
            }
        if isinstance(node, ast.Subscript):
            key = (
                node.slice.value
                if isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                else None
            )
            if key is not None and isinstance(node.value, ast.Call):
                owners = callable_options(
                    module, node.value.func, bindings, assignments
                )
                return {
                    candidate
                    for owner in owners
                    for candidate in returned_targets(
                        owner, bindings, frozenset(), key=key
                    )
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
                return iterable_options(module, node.value, bindings, assignments)
        resolved = resolve(module, node, bindings, assignments)
        return {resolved} if resolved is not None else set()

    def add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        result = dict(left)
        for registry, count in right.items():
            result[registry] = result.get(registry, 0) + count
        return result

    def maximum(*values: dict[str, int]) -> dict[str, int]:
        return {
            registry: max(value.get(registry, 0) for value in values)
            for registry in registry_types
            if any(value.get(registry, 0) for value in values)
        }

    def scale(value: dict[str, int], factor: int) -> dict[str, int]:
        return {registry: count * factor for registry, count in value.items()}

    cache: dict[
        tuple[CallableTarget, tuple[tuple[str, CallableTarget], ...]], dict[str, int]
    ] = {}
    active: set[
        tuple[CallableTarget, tuple[tuple[str, CallableTarget], ...]]
    ] = set()

    def expression_counts(
        module: str,
        node: ast.AST | None,
        bindings: dict[str, CallableTarget],
        assignments: dict[str, ast.AST],
    ) -> dict[str, int]:
        if node is None:
            return {}
        if isinstance(node, ast.IfExp):
            return add(
                expression_counts(module, node.test, bindings, assignments),
                maximum(
                    expression_counts(module, node.body, bindings, assignments),
                    expression_counts(module, node.orelse, bindings, assignments),
                ),
            )
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            element = node.key if isinstance(node, ast.DictComp) else node.elt
            iterable_counts: dict[str, int] = {}
            resolved_iteration = False
            for generator in node.generators:
                candidates = iterable_options(
                    module,
                    generator.iter,
                    bindings,
                    assignments,
                )
                if not candidates:
                    continue
                resolved_iteration = True
                names = {
                    item.id
                    for item in ast.walk(generator.target)
                    if isinstance(item, ast.Name)
                }
                for candidate in candidates:
                    nested_bindings = dict(bindings)
                    nested_bindings.update({name: candidate for name in names})
                    candidate_counts = expression_counts(
                        module,
                        element,
                        nested_bindings,
                        assignments,
                    )
                    if isinstance(node, ast.DictComp):
                        candidate_counts = add(
                            candidate_counts,
                            expression_counts(
                                module,
                                node.value,
                                nested_bindings,
                                assignments,
                            ),
                        )
                    iterable_counts = add(iterable_counts, candidate_counts)
            if resolved_iteration:
                return iterable_counts
            nested = expression_counts(module, element, bindings, assignments)
            if isinstance(node, ast.DictComp):
                nested = add(
                    nested,
                    expression_counts(module, node.value, bindings, assignments),
                )
            factor = 1
            for generator in node.generators:
                factor *= range_count(generator.iter) or 2
            return scale(nested, factor)
        if isinstance(node, ast.Call):
            terminal = _task12_terminal(node.func) or ""
            mapping_dispatch = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"get", "items", "pop", "popitem", "setdefault", "values"}
            )
            iterable_wrapper = (
                terminal in {"iter", "list", "next", "set", "tuple"}
                and bool(node.args)
            )
            counts: dict[str, int] = {}
            if not mapping_dispatch and not iterable_wrapper:
                for argument in node.args:
                    counts = add(
                        counts,
                        expression_counts(module, argument, bindings, assignments),
                    )
                for keyword in node.keywords:
                    counts = add(
                        counts,
                        expression_counts(module, keyword.value, bindings, assignments),
                    )
            if (_task12_terminal(node.func) or "") == "partial":
                return counts
            targets = callable_options(
                module,
                node if mapping_dispatch or iterable_wrapper else node.func,
                bindings,
                assignments,
            )
            if not targets:
                return counts
            alternatives: list[dict[str, int]] = []
            for target in targets:
                if target[0] == registry_module:
                    alternatives.append({target[1]: 1})
                    continue
                callee = callables.get(target)
                if callee is None:
                    alternatives.append({})
                    continue
                parameters = [
                    parameter.arg
                    for parameter in (
                        *callee.args.posonlyargs,
                        *callee.args.args,
                        *callee.args.kwonlyargs,
                    )
                    if parameter.arg not in {"self", "cls"}
                ]
                binding_call = node.func if isinstance(node.func, ast.Call) else node
                method_function = (
                    binding_call.func
                    if isinstance(binding_call.func, ast.Attribute)
                    else None
                )
                next_bindings: dict[str, CallableTarget] = {}
                closure_owner = closure_owners.get(target)
                if closure_owner is not None:
                    owner = callables[closure_owner]
                    owner_parameters = [
                        parameter.arg
                        for parameter in (
                            *owner.args.posonlyargs,
                            *owner.args.args,
                            *owner.args.kwonlyargs,
                        )
                        if parameter.arg not in {"self", "cls"}
                    ]
                    for parameter, argument in zip(
                        owner_parameters,
                        binding_call.args,
                        strict=False,
                    ):
                        resolved = resolve(
                            module, argument, bindings, assignments
                        )
                        if resolved is not None:
                            next_bindings[parameter] = resolved
                    owner_assignments = dict(
                        module_assignments[closure_owner[0]]
                    )
                    for statement in owner.body:
                        if not isinstance(
                            statement, (ast.Assign, ast.AnnAssign)
                        ) or statement.value is None:
                            continue
                        resolved = resolve(
                            closure_owner[0],
                            statement.value,
                            next_bindings,
                            owner_assignments,
                        )
                        assignment_targets = (
                            statement.targets
                            if isinstance(statement, ast.Assign)
                            else [statement.target]
                        )
                        for assignment_target in assignment_targets:
                            if not isinstance(assignment_target, ast.Name):
                                continue
                            owner_assignments[assignment_target.id] = statement.value
                            if resolved is None:
                                next_bindings.pop(assignment_target.id, None)
                            else:
                                next_bindings[assignment_target.id] = resolved
                if (
                    "." in target[1]
                    and ".<locals>." not in target[1]
                    and isinstance(method_function, ast.Attribute)
                    and isinstance(method_function.value, ast.Call)
                ):
                    class_name = target[1].split(".", 1)[0]
                    constructor = resolve(
                        module,
                        method_function.value.func,
                        bindings,
                        assignments,
                    )
                    initializer = callables.get(
                        (target[0], f"{class_name}.__init__")
                    )
                    if constructor == (target[0], class_name) and initializer:
                        init_parameters = [
                            parameter.arg
                            for parameter in (
                                *initializer.args.posonlyargs,
                                *initializer.args.args,
                            )[1:]
                        ]
                        init_bindings: dict[str, CallableTarget] = {}
                        for parameter, argument in zip(
                            init_parameters,
                            method_function.value.args,
                            strict=False,
                        ):
                            resolved = resolve(
                                module, argument, bindings, assignments
                            )
                            if resolved is not None:
                                init_bindings[parameter] = resolved
                        for assignment in ast.walk(initializer):
                            if not isinstance(assignment, ast.Assign):
                                continue
                            if not isinstance(assignment.value, ast.Name):
                                continue
                            resolved = init_bindings.get(assignment.value.id)
                            if resolved is None:
                                continue
                            for assignment_target in assignment.targets:
                                if (
                                    isinstance(assignment_target, ast.Attribute)
                                    and isinstance(assignment_target.value, ast.Name)
                                    and assignment_target.value.id == "self"
                                ):
                                    next_bindings[
                                        f"self.{assignment_target.attr}"
                                    ] = resolved
                if (
                    "." in target[1]
                    and ".<locals>." not in target[1]
                    and isinstance(method_function, ast.Attribute)
                    and isinstance(method_function.value, ast.Name)
                ):
                    receiver = method_function.value.id
                    prefix = f"{receiver}."
                    for binding_name, resolved in bindings.items():
                        if binding_name.startswith(prefix):
                            next_bindings[
                                f"self.{binding_name[len(prefix):]}"
                            ] = resolved
                for parameter, argument in zip(
                    parameters,
                    binding_call.args,
                    strict=False,
                ):
                    resolved = resolve(module, argument, bindings, assignments)
                    if resolved is not None:
                        next_bindings[parameter] = resolved
                for keyword in binding_call.keywords:
                    if keyword.arg in parameters:
                        resolved = resolve(module, keyword.value, bindings, assignments)
                        if resolved is not None:
                            next_bindings[keyword.arg] = resolved
                alternatives.append(callable_counts(target, next_bindings))
            return add(counts, maximum(*alternatives))
        counts: dict[str, int] = {}
        for child in ast.iter_child_nodes(node):
            counts = add(
                counts,
                expression_counts(module, child, bindings, assignments),
            )
        return counts

    def statements_counts(
        module: str,
        statements: list[ast.stmt],
        bindings: dict[str, CallableTarget],
        inherited_assignments: dict[str, ast.AST],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        assignments = dict(inherited_assignments)
        local_bindings = dict(bindings)
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                counts = add(
                    counts,
                    expression_counts(
                        module, statement.value, local_bindings, assignments
                    ),
                )
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target_node in targets:
                    binding_name = _task12_resolved_name(
                        target_node, imports[module]
                    )
                    if not binding_name:
                        continue
                    if isinstance(target_node, ast.Name):
                        assignments[target_node.id] = statement.value
                    resolved = resolve(
                        module, statement.value, local_bindings, assignments
                    )
                    if resolved is None:
                        local_bindings.pop(binding_name, None)
                    else:
                        local_bindings[binding_name] = resolved
                continue
            if isinstance(statement, ast.Return):
                return add(
                    counts,
                    expression_counts(
                        module, statement.value, local_bindings, assignments
                    ),
                )
            if isinstance(statement, ast.If):
                test = expression_counts(
                    module, statement.test, local_bindings, assignments
                )
                if isinstance(statement.test, ast.Constant) and isinstance(
                    statement.test.value, bool
                ):
                    selected = statement.body if statement.test.value else statement.orelse
                    branch = statements_counts(
                        module, selected, local_bindings, assignments
                    )
                else:
                    branch = maximum(
                        statements_counts(
                            module, statement.body, local_bindings, assignments
                        ),
                        statements_counts(
                            module, statement.orelse, local_bindings, assignments
                        ),
                    )
                counts = add(counts, add(test, branch))
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                factor = range_count(statement.iter) or 2
                counts = add(
                    counts,
                    scale(
                        statements_counts(
                            module, statement.body, local_bindings, assignments
                        ),
                        factor,
                    ),
                )
                counts = add(
                    counts,
                    statements_counts(
                        module, statement.orelse, local_bindings, assignments
                    ),
                )
                continue
            if isinstance(statement, (ast.Try, ast.TryStar)):
                alternatives = [
                    statements_counts(
                        module, statement.body, local_bindings, assignments
                    ),
                    *(
                        statements_counts(
                            module, handler.body, local_bindings, assignments
                        )
                        for handler in statement.handlers
                    ),
                ]
                counts = add(counts, maximum(*alternatives))
                counts = add(
                    counts,
                    statements_counts(
                        module,
                        [*statement.orelse, *statement.finalbody],
                        local_bindings,
                        assignments,
                    ),
                )
                continue
            counts = add(
                counts,
                expression_counts(module, statement, local_bindings, assignments),
            )
        return counts

    def callable_counts(
        target: CallableTarget,
        bindings: dict[str, CallableTarget],
    ) -> dict[str, int]:
        key = (target, tuple(sorted(bindings.items())))
        if key in cache:
            return cache[key]
        if key in active:
            return {}
        function = callables.get(target)
        if function is None:
            return {}
        active.add(key)
        result = statements_counts(
            target[0],
            function.body,
            bindings,
            module_assignments[target[0]],
        )
        active.remove(key)
        cache[key] = result
        return result

    total: dict[str, int] = {}
    for module, tree in trees.items():
        total = add(
            total,
            statements_counts(module, tree.body, {}, module_assignments[module]),
        )
    return total


def _task12_backend_violations(
    sources: dict[str, str],
    *,
    exact_manifest: bool = False,
) -> list[str]:
    violations: list[str] = []
    trees = {
        name: _task12_tree(name, source) for name, source in sources.items() if name.endswith(".py")
    }
    for name, tree in trees.items():
        violations.extend(_task12_provider_violations(name, tree))
        violations.extend(_task12_product_action_domain_violations(name, tree))
        violations.extend(_task12_parent_writer_violations(name, tree))
        if _task12_compatibility_switch(name, tree):
            violations.append(f"cutover:compatibility-switch:{name}")
        previous_title_present = any(
            (isinstance(node, ast.Name) and node.id == "previous_title")
            or (isinstance(node, ast.Constant) and node.value == "previous_title")
            for node in ast.walk(tree)
        )
        if previous_title_present and (
            name
            not in {
                "repositories/interview_stories.py",
                "product_actions/compensation.py",
                "product_actions/coordinator.py",
            }
            or _task12_previous_title_reaches_public_sink(
                tree,
                allow_story_parent_undo=name == "repositories/interview_stories.py",
            )
        ):
            violations.append(f"privacy:previous-title:{name}")

    if _task12_product_action_reachable_domain_violation(trees):
        violations.append("product-action:reachable-chat-journal-write")
    if _task12_provider_cross_module_violation(trees):
        violations.append("provider:product-action-cross-module")
    cross_strings = _task12_cross_module_string_bindings(trees)
    violations.extend(_task12_parent_cross_module_violations(trees))
    violations.extend(_task12_cross_module_compatibility_switch(trees))

    violations.extend(
        f"privacy:previous-title:{origin}"
        for origin in _task12_previous_title_cross_module_origins(sources)
    )

    preparation = trees.get("repositories/interview_preparation_proposals.py")
    if preparation is not None and _task12_preparation_v1_reaches_v2(preparation, trees):
        violations.append("preparation:v1-reaches-v2")

    registry_types = (
        "ProductActionCatalogV1",
        "ProductActionCompensationCatalogV1",
        "ProductActionCompensationProofRegistryV1",
        "ProductActionProofRegistryV1",
    )
    registry_counts = dict.fromkeys(registry_types, 0)
    registry_factories: dict[tuple[str, str], list[str]] = {}
    registry_call_sites: dict[tuple[str, str], int] = {}
    conditional_factory_call_sites: set[tuple[str, int, str]] = set()
    conditional_registry_sites: set[tuple[int, str]] = set()

    def range_count(node: ast.AST) -> int | None:
        if not isinstance(node, ast.Call) or (_task12_terminal(node.func) or "") != "range":
            return None
        values = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, int)
        ]
        if len(values) != len(node.args) or not 1 <= len(values) <= 3:
            return None
        return len(range(*values))

    def call_multiplicity(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> int:
        result = 1
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.For, ast.AsyncFor)):
                result *= range_count(parent.iter) or 2
            elif isinstance(parent, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                result *= math.prod(
                    range_count(generator.iter) or 2 for generator in parent.generators
                )
            parent = parents.get(parent)
        return result

    reachable_functions: dict[str, set[str]] = {}
    for module, tree in trees.items():
        local_functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        reachable = {
            function.name
            for function in local_functions.values()
            if function.decorator_list or function.name == "create_app"
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            owner = parents.get(call)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            terminal = _task12_terminal(call.func) or ""
            if owner is None and terminal in local_functions:
                reachable.add(terminal)
        changed = True
        while changed:
            changed = False
            for function_name in tuple(reachable):
                for call in (
                    node
                    for node in ast.walk(local_functions[function_name])
                    if isinstance(node, ast.Call)
                ):
                    terminal = _task12_terminal(call.func) or ""
                    if terminal in local_functions and terminal not in reachable:
                        reachable.add(terminal)
                        changed = True
        reachable_functions[module] = reachable
    for module, tree in trees.items():
        aliases, _modules = _task12_import_aliases(tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            terminal = (_task12_resolved_name(node.func, aliases) or "").rsplit(".", 1)[-1]
            if terminal in registry_counts:
                owner = parents.get(node)
                while owner is not None and not isinstance(
                    owner, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    owner = parents.get(owner)
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    registry_factories.setdefault((module, owner.name), []).extend(
                        [terminal] * call_multiplicity(node, parents)
                    )
                else:
                    conditional = parents.get(node)
                    while conditional is not None and not isinstance(
                        conditional, ast.IfExp
                    ):
                        conditional = parents.get(conditional)
                    site = (id(conditional), terminal)
                    if conditional is None or site not in conditional_registry_sites:
                        registry_counts[terminal] += call_multiplicity(node, parents)
                        if conditional is not None:
                            conditional_registry_sites.add(site)
            elif isinstance(node.func, ast.Name):
                owner = parents.get(node)
                while owner is not None and not isinstance(
                    owner, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    owner = parents.get(owner)
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    owner.name not in reachable_functions[module]
                ):
                    continue
                conditional = parents.get(node)
                while conditional is not None and not isinstance(conditional, ast.IfExp):
                    conditional = parents.get(conditional)
                site = (module, id(conditional), node.func.id)
                if conditional is None or site not in conditional_factory_call_sites:
                    registry_call_sites[module, node.func.id] = (
                        registry_call_sites.get((module, node.func.id), 0)
                        + call_multiplicity(node, parents)
                    )
                    if conditional is not None:
                        conditional_factory_call_sites.add(site)
    registry_imports = {
        module: _task12_module_import_aliases(tree)[0]
        for module, tree in trees.items()
    }

    def registry_import_target(resolved: str) -> tuple[str, str] | None:
        normalized = resolved.removeprefix("offerpilot.")
        matches: list[tuple[int, str, str]] = []
        for candidate in trees:
            dotted = (
                candidate.removesuffix("/__init__.py")
                if candidate.endswith("/__init__.py")
                else candidate.removesuffix(".py")
            ).replace("/", ".")
            prefix = f"{dotted}."
            if normalized.startswith(prefix):
                matches.append((len(dotted), candidate, normalized[len(prefix) :]))
        matches.sort()
        return (matches[-1][1], matches[-1][2]) if matches else None

    def registry_symbol(
        resolved: str,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> str:
        target = registry_import_target(resolved)
        if target is None or target in seen:
            return resolved.removeprefix("offerpilot.")
        nested = registry_imports[target[0]].get(target[1])
        return (
            registry_symbol(nested, seen | {target})
            if nested is not None
            else resolved.removeprefix("offerpilot.")
        )

    for (module, factory), registries in registry_factories.items():
        invocations = registry_call_sites.get((module, factory), 0)
        dotted_module = module.removesuffix(".py").removesuffix("/__init__").replace(
            "/", "."
        )
        for caller_module, caller_tree in trees.items():
            caller_aliases = _task12_module_import_aliases(caller_tree)[0]
            caller_parents = {
                child: parent
                for parent in ast.walk(caller_tree)
                for child in ast.iter_child_nodes(parent)
            }
            imported_sites: set[int] = set()
            for call in ast.walk(caller_tree):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Name)
                    or registry_symbol(caller_aliases.get(call.func.id, ""))
                    != f"{dotted_module}.{factory}"
                ):
                    continue
                owner = caller_parents.get(call)
                while owner is not None and not isinstance(
                    owner, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    owner = caller_parents.get(owner)
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    owner.name not in reachable_functions[caller_module]
                ):
                    continue
                conditional = caller_parents.get(call)
                while conditional is not None and not isinstance(conditional, ast.IfExp):
                    conditional = caller_parents.get(conditional)
                if conditional is None or id(conditional) not in imported_sites:
                    invocations += call_multiplicity(call, caller_parents)
                    if conditional is not None:
                        imported_sites.add(id(conditional))
        if factory == "create_app":
            invocations = 1
        for registry in registries:
            registry_counts[registry] += invocations
    if any(registry_counts.values()):
        for registry, count in registry_counts.items():
            if count != 1:
                violations.append(f"composition:registry-count:{registry}")
    for registry, count in _task12_registry_callgraph_counts(trees).items():
        if count > 1:
            violations.append(f"composition:registry-count:{registry}")

    api_tree = trees.get("api.py")
    if api_tree is not None:
        route_bindings = cross_strings
        routes, unprovable_related = _task12_routes(
            api_tree, route_bindings.get("api.py")
        )
        if any(
            isinstance(call, ast.Call)
            and _task12_terminal(call.func) == "include_router"
            for call in ast.walk(api_tree)
        ):
            nested_routes, nested_unprovable = _task12_included_router_routes(
                trees, route_bindings
            )
            routes.update(nested_routes)
            unprovable_related = unprovable_related or nested_unprovable
        if unprovable_related:
            violations.append("http:unprovable-related-route")
        if any(
            path.startswith("/api/write-operations/") and path.endswith("/undo")
            for _method, path in routes
        ):
            violations.append("http:generic-operation-undo")
        if any(path == "/api/stories" or path.startswith("/api/stories/") for _, path in routes):
            violations.append("http:stories-alias")
        if exact_manifest:
            actual_undo = {
                (method, path) for method, path in routes if method == "post" and "undo" in path
            }
            if actual_undo != _TASK12_HTTP_UNDO_MANIFEST:
                violations.append("http:undo-manifest")

    violations.extend(
        f"privacy:canary:{finding}" for finding in _privacy_canary_violations(sources)
    )
    return sorted(set(violations))


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


def test_task12_backend_gate_negative_fixtures_cover_alias_spread_and_helpers() -> None:
    safe = {
        "ai/tool_specs/catalog.py": '''
"""confirm_interview_story is deliberately not a Provider Tool."""
EXPLANATION = "save_review_readiness_signal remains product-owned"
def unrelated():
    return EXPLANATION
''',
        "api.py": """
@app.post("/api/chat/undo-last-write")
def chat_undo(): pass
@app.post("/api/applications/{application_id}/readiness-signals/{signal_id}/undo")
def signal_undo(): pass
@app.post("/api/interview-stories/{story_id}/product-action-undo")
def story_undo(): pass
""",
        "product_actions/repository.py": """
class ProductActionProposalRepository:
    def _publish_bundle_in_session_unclaimed(self, session):
        row = WriteOperation(adapter_kind="product_action")
        session.add(row)
""",
        "repositories/interview_preparation_proposals.py": """
def _build_v1_snapshot():
    return _build_legacy_snapshot()
def _build_legacy_snapshot():
    return {}
def _build_v2_snapshot():
    return PreparationReadinessSelectionLoader().load()
""",
    }
    assert _task12_backend_violations(safe, exact_manifest=True) == []

    provider_import = {
        "ai/tool_specs/catalog.py": (
            "from offerpilot.product_actions.catalog import ProductActionCatalogV1\n"
        )
    }
    assert "provider:product-action-import:ai/tool_specs/catalog.py" in (
        _task12_backend_violations(provider_import)
    )

    provider_registration = {
        "context_projector/selector.py": """
PRIMARY = ("confirm_interview_story",)
def action_names():
    alias = PRIMARY
    return (*alias, "save_review_readiness_signal")
REGISTERED_TOOLS = (*action_names(),)
"""
    }
    assert "provider:product-action-name:context_projector/selector.py" in (
        _task12_backend_violations(provider_registration)
    )

    provider_neutral_surface = {
        "ai/provider_boundaries.py": '''
PROVIDER_SURFACE = ("confirm_interview_story",)
''',
    }
    assert "provider:product-action-name:ai/provider_boundaries.py" in (
        _task12_backend_violations(provider_neutral_surface)
    )
    provider_neutral_reference = {
        "ai/provider_boundaries.py": '''
PROVIDER_SURFACE = (confirm_interview_story,)
''',
    }
    assert "provider:product-action-name:ai/provider_boundaries.py" in (
        _task12_backend_violations(provider_neutral_reference)
    )

    provider_boundary_paths = {
        "ai/tool_authority/policy.py": '''
def product_action_names():
    return ("confirm_interview_story",)
PROVIDER_TOOL_NAMES = (*product_action_names(),)
''',
        "ai/provider_boundaries.py": (
            "from offerpilot.product_actions.catalog import ProductActionCatalogV1 as Catalog\n"
        ),
    }
    boundary_findings = _task12_backend_violations(provider_boundary_paths)
    assert "provider:product-action-name:ai/tool_authority/policy.py" in boundary_findings
    assert "provider:product-action-import:ai/provider_boundaries.py" in boundary_findings

    forbidden_domain_write = {
        "product_actions/coordinator.py": """
from offerpilot.models import Conversation as C
def execute(session):
    pending = C(title="forbidden")
    session.add(pending)
"""
    }
    assert "product-action:chat-journal-write:product_actions/coordinator.py" in (
        _task12_backend_violations(forbidden_domain_write)
    )

    readonly_conversation_source = '''
from offerpilot.models import Conversation as C
def read_only(session):
    return session.scalar(select(C).where(C.id == 1))
'''
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py",
        _task12_tree("product_actions/coordinator.py", readonly_conversation_source),
    ) == []

    journal_writes = {
        "product_actions/coordinator.py": '''
from offerpilot.models import AgentEvent as EventRow, AgentContextSnapshot as SnapshotRow
def write(session):
    event = EventRow(payload_json="{}")
    session.add(event)
    statement = insert(SnapshotRow).values(manifest_json="{}")
    session.execute(statement)
''',
    }
    assert "product-action:chat-journal-write:product_actions/coordinator.py" in (
        _task12_backend_violations(journal_writes)
    )

    raw_journal_write = {
        "product_actions/coordinator.py": '''
SQL = "INSERT INTO agent_context_snapshots (manifest_json) VALUES (:manifest_json)"
def write(session):
    statement = text(SQL)
    session.execute(statement, {"manifest_json": "{}"})
''',
    }
    assert "product-action:chat-journal-write:product_actions/coordinator.py" in (
        _task12_backend_violations(raw_journal_write)
    )

    semantic_repository_writes = {
        "product_actions/coordinator.py": '''
def finish_execution(chat, agent_runs, recorder):
    return persist(chat, agent_runs, recorder)
def persist(first, second, third):
    first.create_conversation()
    second.create_run_and_initial_segment()
    third.finish()
''',
    }
    assert "product-action:chat-journal-write:product_actions/coordinator.py" in (
        _task12_backend_violations(semantic_repository_writes)
    )
    semantic_repository_read = '''
def inspect(chat):
    return load(chat)
def load(repository):
    return repository.get_conversation()
'''
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py",
        _task12_tree("product_actions/coordinator.py", semantic_repository_read),
    ) == []
    cross_module_repository_write = {
        "product_actions/coordinator.py": '''
from offerpilot.review_readiness.bridge import persist
def execute(chat):
    return persist(chat)
''',
        "review_readiness/bridge.py": '''
def persist(repository):
    return repository.create_conversation()
''',
    }
    assert "product-action:reachable-chat-journal-write" in (
        _task12_backend_violations(cross_module_repository_write)
    )
    cross_module_aliased_model_write = {
        "product_actions/coordinator.py": '''
from offerpilot.shared.writer import persist
def execute(session):
    return persist(session)
''',
        "shared/writer.py": '''
from offerpilot.models import AgentEvent as E
def persist(session):
    row = E(payload_json="{}")
    session.add(row)
''',
    }
    assert "product-action:reachable-chat-journal-write" in (
        _task12_backend_violations(cross_module_aliased_model_write)
    )

    parent_spread = {
        "product_actions/repository.py": """
def shape():
    return {"adapter_kind": "product_action"}
class ProductActionProposalRepository:
    def another_publisher(self, session):
        values = shape()
        session.add(WriteOperation(**values))
"""
    }
    assert "ledger:unsealed-parent-writer:product_actions/repository.py" in (
        _task12_backend_violations(parent_spread)
    )
    core_parent = {
        "other.py": """
from offerpilot.models import WriteOperation as WO
def publish(session):
    values = {"adapter_kind": "product_action"}
    session.execute(insert(WO).values(**values))
"""
    }
    assert "ledger:unsealed-parent-writer:other.py" in (_task12_backend_violations(core_parent))

    parameterized_parent = {
        "other.py": '''
from sqlalchemy import text as sql_text
KIND = "adapter_kind"
ACTION = "product_action"
def publish(session):
    values = {KIND: ACTION}
    statement = sql_text(
        "INSERT INTO write_operations (adapter_kind) VALUES (:adapter_kind)"
    ).bindparams(**values)
    session.execute(statement)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(parameterized_parent)
    )
    dict_constructor_parent = {
        "other.py": '''
SQL = "INSERT INTO write_operations (adapter_kind) VALUES (:adapter_kind)"
ACTION = "product_action"
def publish(session):
    params = dict(adapter_kind=ACTION)
    session.execute(text(SQL), params)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(dict_constructor_parent)
    )
    bindparam_parent = {
        "other.py": '''
SQL = "INSERT INTO write_operations (adapter_kind) VALUES (:adapter_kind)"
ACTION = "product_action"
def publish(session):
    statement = text(SQL).bindparams(bindparam("adapter_kind", ACTION))
    session.execute(statement)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(bindparam_parent)
    )
    renamed_parameter_parent = {
        "other.py": '''
from offerpilot.models import WriteOperation as Operation
def publish(session, kind="product_action"):
    payload = {"adapter_kind": kind}
    row = Operation(**payload)
    session.add(row)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(renamed_parameter_parent)
    )
    flowed_parameter_parent = {
        "other.py": '''
from offerpilot.models import WriteOperation as Operation
def publish(session, kind):
    payload = {"adapter_kind": kind}
    session.add(Operation(**payload))
def execute(session):
    publish(session, "product_action")
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(flowed_parameter_parent)
    )
    positional_values_parent = {
        "other.py": '''
from offerpilot.models import WriteOperation as Operation
def publish(session):
    kind = "product_action"
    payload = {"adapter_kind": kind}
    statement = insert(Operation).values(payload)
    session.execute(statement)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(positional_values_parent)
    )
    assigned_constructor_alias = {
        "other.py": '''
Alias = WriteOperation
def publish(session):
    session.add(Alias(adapter_kind="product_action"))
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(assigned_constructor_alias)
    )
    renamed_sql_placeholder = {
        "other.py": '''
SQL = "INSERT INTO write_operations (adapter_kind) VALUES (:kind)"
def publish(session):
    data = {"kind": "product_action"}
    session.execute(text(SQL), data)
''',
    }
    assert "ledger:unsealed-parent-writer:other.py" in (
        _task12_backend_violations(renamed_sql_placeholder)
    )
    sealed_parameterized_parent = {
        "product_actions/repository.py": '''
SQL = "INSERT INTO write_operations (adapter_kind) VALUES (:adapter_kind)"
class ProductActionProposalRepository:
    def _publish_bundle_in_session_unclaimed(self, session):
        session.execute(text(SQL), {"adapter_kind": "product_action"})
''',
    }
    assert _task12_parent_writer_violations(
        "product_actions/repository.py",
        _task12_tree(
            "product_actions/repository.py",
            sealed_parameterized_parent["product_actions/repository.py"],
        ),
    ) == []

    preparation_helper_escape = {
        "repositories/interview_preparation_proposals.py": """
def _build_v1_snapshot():
    helper = _load_selection
    return helper()
def _load_selection():
    return PreparationReadinessSelectionLoader().load()
"""
    }
    assert "preparation:v1-reaches-v2" in (_task12_backend_violations(preparation_helper_escape))

    preparation_module_alias = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.review_readiness.preparation_selection import (
    PreparationReadinessSelectionLoader as Loader,
)
V2 = _build_v2_snapshot
ALIAS = V2
def _build_v1_snapshot():
    return helper()
def helper():
    return ALIAS(), Loader().load()
def _build_v2_snapshot():
    return {}
''',
    }
    assert "preparation:v1-reaches-v2" in (
        _task12_backend_violations(preparation_module_alias)
    )

    preparation_cross_module = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.review_readiness.preparation_bridge import build as helper
def _build_v1_snapshot():
    return helper()
''',
        "review_readiness/preparation_bridge.py": '''
from offerpilot.review_readiness.preparation_selection import PreparationReadinessSelectionLoader
class Bridge:
    @staticmethod
    def build():
        load = lambda: PreparationReadinessSelectionLoader().load()
        return load()
build = Bridge.build
''',
    }
    assert "preparation:v1-reaches-v2" in (
        _task12_backend_violations(preparation_cross_module)
    )
    preparation_instance_method = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.review_readiness.preparation_bridge import H
def _build_v1_snapshot():
    return H().load()
''',
        "review_readiness/preparation_bridge.py": '''
from offerpilot.review_readiness.preparation_selection import PreparationReadinessSelectionLoader
class H:
    def load(self):
        return PreparationReadinessSelectionLoader().load()
''',
    }
    assert "preparation:v1-reaches-v2" in (
        _task12_backend_violations(preparation_instance_method)
    )

    compatibility_switch = {
        "product_actions/coordinator.py": """
review_readiness_shadow_write = True
def execute():
    if review_readiness_shadow_write:
        return legacy_story_confirm_fallback()
"""
    }
    findings = _task12_backend_violations(compatibility_switch)
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in findings

    independent_switches = {
        "product_actions/coordinator.py": '''
FEATURE_FLAG = True
def legacy_fallback(request):
    return request
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(independent_switches)
    )
    fallback_constructor = {
        "product_actions/compensation.py": '''
def seal(handler, catalog=None):
    return Sealed(handler, catalog=catalog or ProductActionCompensationCatalogV1())
''',
    }
    assert "cutover:compatibility-switch:product_actions/compensation.py" in (
        _task12_backend_violations(fallback_constructor)
    )
    neutral_compatibility_switch = {
        "product_actions/coordinator.py": '''
USE_NEW_ACTION = True
def product_path():
    return ProductActionCatalogV1()
def old_path():
    return confirm_interview_story_proposal()
def execute():
    handler = product_path if USE_NEW_ACTION else old_path
    return handler()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(neutral_compatibility_switch)
    )
    closure_compatibility_switch = {
        "product_actions/coordinator.py": '''
SWITCH = True
def execute():
    def first():
        return ProductActionCatalogV1()
    second = lambda: confirm_interview_story_proposal()
    selected = first if SWITCH else second
    return selected()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(closure_compatibility_switch)
    )

    double_registry = {
        "api.py": """
from offerpilot.product_actions.catalog import ProductActionCatalogV1 as Catalog
first = Catalog()
second = Catalog()
""",
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(double_registry)
    )


def test_task12_http_manifest_is_exact_and_rejects_aliases() -> None:
    generic = {
        "api.py": """
UNDO = "/api/write-operations/{id}/undo"
@app.post(UNDO)
def undo(): pass
"""
    }
    findings = _task12_backend_violations(generic, exact_manifest=True)
    assert "http:generic-operation-undo" in findings

    story_alias = {
        "api.py": """
PREFIX = "/api/stories"
@app.get(PREFIX + "/{story_id}")
def story(): pass
"""
    }
    assert "http:stories-alias" in _task12_backend_violations(story_alias)

    hidden_routes = {
        "api.py": '''
join = lambda prefix, suffix: prefix + suffix
def generic_route():
    return join("/api/write-operations/", "{id}/undo")
def story_route():
    prefix = "/api/" + "stories"
    return prefix + "/{story_id}"
@app.post(generic_route())
def undo(): pass
@app.get(story_route())
def story(): pass
''',
    }
    hidden_findings = _task12_backend_violations(hidden_routes)
    assert "http:generic-operation-undo" in hidden_findings
    assert "http:stories-alias" in hidden_findings

    dynamic_related_route = {
        "api.py": '''
@app.post("/api/write-operations/" + suffix)
def route(): pass
''',
    }
    assert "http:unprovable-related-route" in (
        _task12_backend_violations(dynamic_related_route)
    )
    class_routes = {
        "api.py": '''
class R:
    @staticmethod
    def generic():
        return "/api/write-operations/" + "{id}/undo"
    @classmethod
    def story(cls):
        return "/api/" + "stories/{story_id}"
@app.post(R().generic())
def undo(): pass
@app.get(R.story())
def story(): pass
''',
    }
    class_findings = _task12_backend_violations(class_routes)
    assert "http:generic-operation-undo" in class_findings
    assert "http:stories-alias" in class_findings
    dynamic_class_route = {
        "api.py": '''
class R:
    def path(self, suffix):
        return "/api/write-operations/" + suffix
@app.post(R().path(suffix))
def undo(): pass
''',
    }
    assert "http:unprovable-related-route" in (
        _task12_backend_violations(dynamic_class_route)
    )


def test_task12_privacy_canary_and_previous_title_are_sink_scoped() -> None:
    dead_canary = {
        "product_actions/coordinator.py": ('EXPLANATION = "review-readiness-private-canary-8b93"\n')
    }
    assert _privacy_canary_violations(dead_canary) == []

    sink_names = (
        "Snapshot",
        "Event",
        "JournalEntry",
        "PreparedSnapshot",
        "ProductActionDecisionResultV1",
        "Response",
        "SurfaceManifestV2",
        "JSONResponse",
        "error_response",
        "append_log_entry",
        "terminal_result",
        "visible_result",
        "transport",
        "undo",
        "print",
    )
    for sink in sink_names:
        fixture = {
            "fixture.py": f"""
CANARY = "review-readiness-private-canary-8b93"
alias = CANARY
def leak():
    return {sink}(payload={{"nested": [alias]}})
"""
        }
        assert _privacy_canary_violations(fixture) == [f"fixture.py:{sink}"]

    wrapped_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def wrapper(value):
    return {"nested": [value]}
def forward(secret):
    holder = object()
    holder.payload = {}
    holder.payload["value"] = wrapper(secret)
    return holder
def leak(value):
    forwarded = forward(value)
    return error_response(500, forwarded.payload)
leak(CANARY)
''',
    }
    assert _privacy_canary_violations(wrapped_canary) == ["fixture.py:error_response"]

    scope_safe_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def wrapper(value):
    return {"nested": value}
def keep_private(secret):
    return wrapper(secret)
def unrelated(value):
    return JSONResponse({"public": value})
keep_private(CANARY)
unrelated("public")
''',
    }
    assert _privacy_canary_violations(scope_safe_canary) == []

    variadic_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def wrapper(*items, **named):
    return {"items": items, "named": named}
def leak(secret):
    return terminal_result(wrapper("public", value=secret))
leak(CANARY)
''',
    }
    assert _privacy_canary_violations(variadic_canary) == ["fixture.py:terminal_result"]

    context_sensitive_variadic = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def wrapper(*items, **named):
    return {"items": items, "named": named}
def private(secret):
    return wrapper(secret)
def public(value):
    return JSONResponse(wrapper(value=value))
private(CANARY)
public("public")
''',
    }
    assert _privacy_canary_violations(context_sensitive_variadic) == []

    starred_and_keyword_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def positional(public, secret):
    return terminal_result(secret)
def keyword(public, secret):
    return JSONResponse(secret)
items = ["public", CANARY]
payload = {"public": "public", "secret": CANARY}
positional(*items)
keyword(**payload)
''',
    }
    assert _privacy_canary_violations(starred_and_keyword_canary) == [
        "fixture.py:JSONResponse",
        "fixture.py:terminal_result",
    ]

    precise_keyword_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def response(secret, public):
    return JSONResponse(public)
payload = {"secret": CANARY, "public": "public"}
response(**payload)
''',
    }
    assert _privacy_canary_violations(precise_keyword_canary) == []

    mutated_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
payload = {}
payload.update({"secret": CANARY})
terminal_result(payload)
''',
    }
    assert _privacy_canary_violations(mutated_canary) == ["fixture.py:terminal_result"]

    structural_canary = {
        "fixture.py": '''
CANARY = "review-readiness-private-canary-8b93"
def leak(value):
    response = {"transport": {"visible": value}}
    return response
leak(CANARY)
''',
    }
    assert _privacy_canary_violations(structural_canary) == ["fixture.py:transport"]

    previous_title_http = {
        "api.py": """
def response(previous_title):
    return JSONResponse({"previous_title": previous_title})
"""
    }
    assert "privacy:previous-title:api.py" in (_task12_backend_violations(previous_title_http))
    previous_title_terminal = {
        "product_actions/compensation.py": """
def render_compensation(previous_title):
    return visible_result({"previous_title": previous_title})
"""
    }
    assert "privacy:previous-title:product_actions/compensation.py" in (
        _task12_backend_violations(previous_title_terminal)
    )

    previous_title_wrapped = {
        "product_actions/compensation.py": '''
def wrapper(value):
    return {"nested": value}
def render_compensation(previous_title):
    payload = wrapper(previous_title)
    return append_log_entry(payload)
''',
    }
    assert "privacy:previous-title:product_actions/compensation.py" in (
        _task12_backend_violations(previous_title_wrapped)
    )

    previous_title_transport = {
        "product_actions/compensation.py": '''
def render_compensation(previous_title):
    return {"transport": {"visible": previous_title}}
''',
    }
    assert "privacy:previous-title:product_actions/compensation.py" in (
        _task12_backend_violations(previous_title_transport)
    )

    previous_title_parent_only = {
        "repositories/interview_stories.py": '''
def publish(previous_title):
    undo_json = {"kind": "story_parent", "previous_title": previous_title}
    return WriteOperation(
        operation_role="primary",
        adapter_kind="product_action",
        tool_name="confirm_interview_story",
        undo_json=undo_json,
    )
''',
    }
    assert "privacy:previous-title:repositories/interview_stories.py" not in (
        _task12_backend_violations(previous_title_parent_only)
    )
    previous_title_wrong_operation_field = {
        "repositories/interview_stories.py": '''
def publish(previous_title):
    return WriteOperation(
        operation_role="primary",
        adapter_kind="product_action",
        tool_name="confirm_interview_story",
        result_json={"previous_title": previous_title},
    )
''',
    }
    assert "privacy:previous-title:repositories/interview_stories.py" in (
        _task12_backend_violations(previous_title_wrong_operation_field)
    )
    previous_title_unsealed_undo = {
        "repositories/interview_stories.py": '''
def publish(previous_title):
    return WriteOperation(undo_json={"previous_title": previous_title})
''',
    }
    assert "privacy:previous-title:repositories/interview_stories.py" in (
        _task12_backend_violations(previous_title_unsealed_undo)
    )


def test_task12_signal_decision_runtime_keeps_privacy_canary_out_of_public_sinks(
    tmp_path: Path,
) -> None:
    import json

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from offerpilot.api import create_app
    from offerpilot.db import session_factory_for_data_dir
    from offerpilot.models import AgentContextSnapshot, AgentEvent, WriteOperation
    from offerpilot.review_readiness.candidates import project_readiness_candidates
    from tests.review_readiness_support import seed_review_candidate

    canary = _TASK12_PRIVACY_CANARY
    session_factory = session_factory_for_data_dir(tmp_path)
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    client = TestClient(create_app(data_dir=tmp_path))
    proposed = client.post(
        f"/api/interview-notes/{seeded['note_id']}/readiness-focus-actions",
        content=json.dumps(
            {
                "proposal_id": seeded["proposal_id"],
                "focus_id": seeded["focus_id"],
                "expected_note_revision": seeded["note_revision"],
                "expected_candidate_fingerprint": candidate.candidate_fingerprint,
                "idempotency_key": "55555555-5555-4555-8555-555555555555",
                "user_note": canary,
            }
        ).encode(),
        headers={"content-type": "application/json"},
    )
    assert proposed.status_code == 201
    proposed_body = proposed.json()
    assert canary not in json.dumps(proposed_body)
    operation_id = proposed_body["operation_id"]
    decided = client.post(
        f"/api/product-actions/{operation_id}/decisions",
        json={
            "confirmation_token": proposed_body["confirmation_token"],
            "decision": "approve",
        },
    )
    assert decided.status_code == 200
    recovery = client.get(f"/api/product-actions/{operation_id}")
    invalid = client.post(
        f"/api/product-actions/{operation_id}/decisions",
        json={"confirmation_token": canary, "decision": "approve"},
    )
    for response in (decided, recovery, invalid):
        assert canary not in response.text

    with session_factory() as session:
        snapshots = session.scalars(select(AgentContextSnapshot)).all()
        events = session.scalars(select(AgentEvent)).all()
        operations = session.scalars(select(WriteOperation)).all()
        surfaces = [
            *(row.manifest_json for row in snapshots),
            *(row.payload_json for row in events),
            *(
                value
                for row in operations
                for value in (
                    row.result_json,
                    row.visible_result,
                    row.transport_json,
                    row.undo_json,
                    row.failure_code,
                )
                if value is not None
            ),
        ]
    assert all(canary not in value for value in surfaces)


def test_task12_backend_production_gate() -> None:
    sources = {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
    }
    assert _task12_backend_violations(sources, exact_manifest=True) == []


def test_task12_story_smoke_uses_server_identity_and_exact_decision_replay() -> None:
    smoke_source = (SRC / "smoke.py").read_text(encoding="utf-8")
    assert _task12_story_smoke_violations(smoke_source) == []
    for surface in (
        "story_before_decision_http",
        "story_pending_generic_http",
        "story_after_source_owner_replay_http",
        "story_source_owner_http_previous_title",
        "story_source_replay_http_previous_title",
        "story_pending_generic_http_previous_title",
        "story_after_decision_http",
        "story_after_decision_http_previous_title",
        "story_recovery_http",
        "story_recovery_http_previous_title",
        "story_terminal_http",
        "story_terminal_http_previous_title",
        "story_replay_recovery_http",
        "story_replay_recovery_http_previous_title",
        "story_visible_http",
        "story_visible_http_previous_title",
    ):
        missing_nonowner_scan = smoke_source.replace(
            f'"{surface}"', f'"removed_{surface}"', 1
        )
        assert "smoke:missing-nonowner-http-canary-scan" in (
            _task12_story_smoke_violations(missing_nonowner_scan)
        )

    client_token = """
def _create_and_confirm_story_proposal(client, confirmation_token):
    confirmation_token = "story-ui-confirm-0001"
    return client.post("/api/interview-story-proposals/1/confirm")
"""
    findings = _task12_story_smoke_violations(client_token)
    assert "smoke:client-generated-token" in findings
    assert "smoke:legacy-confirm-route" in findings

    missing_replay = """
def _create_and_confirm_story_proposal(client):
    body = client.post("/api/interview-story-proposals").json()
    action = body["product_action"]
    decision = {"confirmation_token": action["confirmation_token"], "decision": "approve"}
    return client.post(f"/api/product-actions/{action['operation_id']}/decisions", json=decision)
"""
    assert "smoke:missing-exact-decision-replay" in (_task12_story_smoke_violations(missing_replay))

    consumes_first_response = '''
def _create_and_confirm_story_proposal(client):
    body = client.post("/api/interview-story-proposals").json()
    action = body["product_action"]
    decision_request = {
        "confirmation_token": action["confirmation_token"],
        "decision": "approve",
    }
    decision_url = f"/api/product-actions/{action['operation_id']}/decisions"
    first = client.post(decision_url, json=decision_request)
    first_body = first.json()
    replay = client.post(decision_url, json=decision_request)
    return replay.json(), first_body
'''
    assert "smoke:first-decision-response-consumed" in (
        _task12_story_smoke_violations(consumes_first_response)
    )

    missing_privacy_and_tools = '''
def _create_and_confirm_story_proposal(client):
    body = client.post("/api/interview-story-proposals").json()
    action = body["product_action"]
    decision_request = {
        "confirmation_token": action["confirmation_token"],
        "decision": "approve",
    }
    decision_url = f"/api/product-actions/{action['operation_id']}/decisions"
    client.post(decision_url, json=decision_request)
    replay = client.post(decision_url, json=decision_request)
    return replay.json()
'''
    smoke_findings = _task12_story_smoke_violations(missing_privacy_and_tools)
    assert "smoke:missing-token-absence-check" in smoke_findings
    assert "smoke:missing-provider-26-check" in smoke_findings
    assert "smoke:missing-real-ai-exclusion" in smoke_findings


def test_task12_fourth_review_module_graph_and_parent_probes() -> None:
    reachable_writer = {
        "product_actions/coordinator.py": '''
from offerpilot.shared import Writer
def decide(session):
    writer = Writer(session)
    writer.persist()
''',
        "shared/__init__.py": "from offerpilot.shared.writer import Writer\n",
        "shared/writer.py": '''
from offerpilot.models import AgentEvent as E
class Writer:
    def __init__(self, session): self.session = session
    def persist(self): self.session.add(E(kind="decision"))
''',
    }
    assert _task12_product_action_reachable_domain_violation(
        {name: _task12_tree(name, source) for name, source in reachable_writer.items()}
    )

    parent_sources = {
        "mutated.py": '''
from offerpilot.models import WriteOperation
payload = {}
payload["adapter_kind"] = "product_action"
extra = {}
extra.update(payload)
row = WriteOperation(**extra)
''',
        "destructured.py": '''
from offerpilot.models import WriteOperation
Alias, ignored = WriteOperation, object
row = Alias(adapter_kind="product_action")
''',
        "quoted.py": '''
SQL = 'INSERT INTO write_operations ("adapter_kind") VALUES (:kind)'
def write(session):
    session.execute(SQL, {"kind": "product_action"})
''',
    }
    for name, source in parent_sources.items():
        assert _task12_parent_writer_violations(name, _task12_tree(name, source)) == [
            f"ledger:unsealed-parent-writer:{name}"
        ]


def test_task12_fourth_review_http_v1_and_compat_probes() -> None:
    route_tree = _task12_tree(
        "api.py",
        '''
class Routes:
    ROOT = "/api/write-operations/"
    @property
    def undo(self):
        return self.ROOT + "{operation_id}/undo"
Alias = Routes
@app.post(Alias().undo)
def hidden_undo(): pass
''',
    )
    routes, unprovable = _task12_routes(route_tree)
    assert ("post", "/api/write-operations/{operation_id}/undo") in routes
    assert not unprovable

    safe_get = _task12_tree(
        "api.py",
        '''
suffix = runtime_suffix()
@app.get("/api/write-operations/" + suffix)
def inspect(): pass
''',
    )
    assert _task12_routes(safe_get)[1] is False

    v1_sources = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.bridge import factory
def _build_v1_snapshot():
    first, second = factory(), object()
    return first.loader()
''',
        "bridge.py": '''
from offerpilot.v2 import PreparationReadinessSelectionLoader
class Holder:
    @property
    def loader(self): return PreparationReadinessSelectionLoader().load
def factory(): return Holder()
''',
        "v2.py": "class PreparationReadinessSelectionLoader:\n    def load(self): return None\n",
    }
    v1_trees = {name: _task12_tree(name, source) for name, source in v1_sources.items()}
    assert _task12_preparation_v1_reaches_v2(
        v1_trees["repositories/interview_preparation_proposals.py"], v1_trees
    )

    compat = _task12_tree(
        "product_actions/coordinator.py",
        '''
class Current:
    def __call__(self): return ProductActionCatalogV1()
class Previous:
    def __call__(self): return confirm_interview_story_proposal()
handler = Current() if USE_NEW_ACTION else Previous()
handler()
''',
    )
    assert _task12_compatibility_switch("product_actions/coordinator.py", compat)


def test_task12_fourth_review_precise_and_cross_module_taint_probes() -> None:
    safe = {
        "safe.py": f'''
def route():
    payload = {{"secret": "{_TASK12_PRIVACY_CANARY}", "public": "ok"}}
    items = ["{_TASK12_PRIVACY_CANARY}", "ok"]
    return JSONResponse({{"value": payload["public"], "item": items[1]}})
'''
    }
    assert _privacy_canary_violations(safe) == []

    aliased = {
        "aliased.py": f'''
from starlette.responses import JSONResponse as Imported
Sink = Imported
def route():
    payload = {{"public": "ok"}}
    payload["secret"] = "{_TASK12_PRIVACY_CANARY}"
    result = {{**payload}}
    return Sink(result)
'''
    }
    assert _privacy_canary_violations(aliased) == ["aliased.py:JSONResponse"]

    crossed = {
        "entry.py": f'''from offerpilot.shared import leak\ndef route(): return leak("{_TASK12_PRIVACY_CANARY}")\n''',
        "shared/__init__.py": "from offerpilot.shared.sink import leak\n",
        "shared/sink.py": "def leak(value): return JSONResponse({'error': value})\n",
    }
    assert "entry.py:JSONResponse" in _privacy_canary_violations(crossed)
    cross_safe = {
        "entry.py": f'''from offerpilot.shared import leak\ndef route():\n    payload = {{"secret": "{_TASK12_PRIVACY_CANARY}", "public": "ok"}}\n    return leak(payload["public"])\n''',
        "shared/__init__.py": "from offerpilot.shared.sink import leak\n",
        "shared/sink.py": "def leak(value): return JSONResponse({'result': value})\n",
    }
    assert _privacy_canary_violations(cross_safe) == []


def test_task12_fourth_review_previous_title_alias_and_exact_sink_probes() -> None:
    aliased = _task12_tree(
        "stories/repository.py",
        '''
from offerpilot.models import WriteOperation as Imported
Alias = Imported
def write(previous_title):
    row = Alias(adapter_kind="other")
    setattr(row, "transport_json", previous_title)
''',
    )
    assert _task12_previous_title_reaches_public_sink(aliased)

    safe = _task12_tree(
        "stories/repository.py",
        '''
from offerpilot.models import WriteOperation
def write(previous_title):
    allowed = WriteOperation(operation_role="primary", adapter_kind="product_action",
        tool_name="confirm_interview_story", undo_json={"previous_title": previous_title})
    unrelated = WriteOperation(result_json="public")
    return allowed, unrelated
''',
    )
    assert not _task12_previous_title_reaches_public_sink(
        safe, allow_story_parent_undo=True
    )

    crossed = {
        "stories/repository.py": "from offerpilot.shared import publish\ndef write(previous_title): return publish(previous_title)\n",
        "shared/__init__.py": "from offerpilot.shared.sink import publish\n",
        "shared/sink.py": "def publish(value): return JSONResponse({'result': value})\n",
    }
    assert _task12_previous_title_sources_reach_public_sink(crossed)


def test_task12_fifth_review_higher_order_and_cross_module_writer_probes() -> None:
    higher_order = {
        "product_actions/coordinator.py": '''
from offerpilot.shared import invoke, persist, writer
def decide(session):
    writer()(session)
    invoke(persist, session)
''',
        "shared/__init__.py": '''
from offerpilot.shared.writer import invoke, persist, writer
''',
        "shared/writer.py": '''
from offerpilot.models import AgentEvent
def writer(): return persist
def invoke(callback, session): return callback(session)
def persist(session): session.add(AgentEvent(kind="decision"))
''',
    }
    trees = {name: _task12_tree(name, source) for name, source in higher_order.items()}
    assert _task12_product_action_reachable_domain_violation(trees)

    lexical_writer = _task12_tree(
        "product_actions/lexical.py",
        '''
def forbidden(session):
    from offerpilot.models import AgentEvent as Model
    session.add(Model(kind="decision"))
def unrelated():
    from offerpilot.safe import PublicModel as Model
    return Model()
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/lexical.py", lexical_writer
    )

    parent_cross_module = {
        "other.py": '''
from offerpilot.shared import create_parent
def write_keyword(): return create_parent(kind="product_action")
def write_positional(): return create_parent("product_action")
''',
        "shared/__init__.py": "from offerpilot.shared.parent import create_parent\n",
        "shared/parent.py": '''
from offerpilot.models import WriteOperation
def create_parent(kind): return WriteOperation(adapter_kind=kind)
''',
    }
    assert "ledger:unsealed-parent-writer:shared/parent.py" in _task12_backend_violations(
        parent_cross_module
    )


def test_task12_fifth_review_cross_module_route_v1_and_compat_probes() -> None:
    imported_route = {
        "api.py": '''
from offerpilot.routes import OPERATION_UNDO as ROUTE
@app.post(ROUTE)
def hidden(): pass
''',
        "routes/__init__.py": "from offerpilot.routes.paths import OPERATION_UNDO\n",
        "routes/paths.py": '''
ROOT = "/api/write-operations/"
OPERATION_UNDO = ROOT + "{operation_id}/undo"
''',
    }
    assert "http:generic-operation-undo" in _task12_backend_violations(imported_route)

    v1_sources = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.bridge import primary
def _build_v1_snapshot(): return primary.loader()
''',
        "bridge.py": '''
from offerpilot.v2 import PreparationReadinessSelectionLoader
class Holder:
    @property
    def loader(self): return PreparationReadinessSelectionLoader().load
primary, secondary = Holder(), object()
''',
        "v2.py": "class PreparationReadinessSelectionLoader:\n    def load(self): pass\n",
    }
    v1_trees = {name: _task12_tree(name, source) for name, source in v1_sources.items()}
    assert _task12_preparation_v1_reaches_v2(
        v1_trees["repositories/interview_preparation_proposals.py"], v1_trees
    )

    compat_sources = {
        "product_actions/coordinator.py": '''
from offerpilot.bridge import handlers
def choose():
    handler = handlers.current if USE_NEW_ACTION else handlers.previous
    return handler()
''',
        "bridge.py": '''
class Handlers:
    current = ProductActionCatalogV1
    previous = confirm_interview_story_proposal
handlers = Handlers()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(compat_sources)
    )


def test_task12_fifth_review_privacy_shapes_instances_and_scope_probes() -> None:
    helper_safe = {
        "safe.py": f'''
def payload(): return {{"secret": "{_TASK12_PRIVACY_CANARY}", "public": "ok"}}
def route(): return JSONResponse(payload()["public"])
'''
    }
    assert _privacy_canary_violations(helper_safe) == []
    helper_leak = {
        "leak.py": f'''
def payload(value): return {{"secret": value, "public": "ok"}}
def route(): return JSONResponse(payload("{_TASK12_PRIVACY_CANARY}")["secret"])
'''
    }
    assert _privacy_canary_violations(helper_leak) == ["leak.py:JSONResponse"]

    instance_leak = {
        "instance.py": f'''
class Box:
    def __init__(self, value): self.value = value
    def emit(self): return JSONResponse({{"result": self.value}})
def route(): return Box("{_TASK12_PRIVACY_CANARY}").emit()
'''
    }
    instance_findings = _privacy_canary_violations(instance_leak)
    assert "instance.py:JSONResponse" in instance_findings
    assert "instance.py:result" in instance_findings

    append_leak = {
        "append.py": f'''
def route():
    values = []
    values.append("{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''
    }
    assert _privacy_canary_violations(append_leak) == ["append.py:JSONResponse"]

    lexical_safe = {
        "scope.py": f'''
from offerpilot.safe import SafeSink as Sink
def route(): return Sink("{_TASK12_PRIVACY_CANARY}")
def unrelated():
    from starlette.responses import JSONResponse as Sink
    return Sink("ok")
'''
    }
    assert _privacy_canary_violations(lexical_safe) == []

    contextual_safe = {
        "context.py": f'''
def identity(value): return value
def route(): return JSONResponse(identity("ok"))
def unrelated(): return identity("{_TASK12_PRIVACY_CANARY}")
'''
    }
    assert _privacy_canary_violations(contextual_safe) == []


def test_task12_fifth_review_previous_title_instance_flow_probe() -> None:
    tree = _task12_tree(
        "stories/repository.py",
        '''
class Box:
    def __init__(self, value): self.value = value
    def publish(self): return WriteOperation(transport_json=self.value)
def write(previous_title): return Box(previous_title).publish()
''',
    )
    assert _task12_previous_title_reaches_public_sink(tree)


def test_task12_sixth_review_recursive_callable_parent_kwargs_and_route_helper() -> None:
    higher_order = {
        "product_actions/coordinator.py": '''
from offerpilot.shared import choose
def decide(session): return choose(True)(session)
''',
        "shared/__init__.py": "from offerpilot.shared.writer import choose\n",
        "shared/writer.py": '''
from offerpilot.models import AgentEvent
def choose(enabled):
    alias = persist
    return alias if enabled else choose(enabled)
def persist(session): session.add(AgentEvent(kind="decision"))
''',
    }
    assert _task12_product_action_reachable_domain_violation(
        {name: _task12_tree(name, source) for name, source in higher_order.items()}
    )

    parent_kwargs = {
        "entry.py": '''
from offerpilot.shared import relay
def write(): return relay(kind="product_action")
''',
        "shared/__init__.py": "from offerpilot.shared.parent import relay\n",
        "shared/parent.py": '''
from offerpilot.models import WriteOperation
def create(*, kind): return WriteOperation(adapter_kind=kind)
def relay(**kwargs): return create(**kwargs)
''',
    }
    assert "ledger:unsealed-parent-writer:shared/parent.py" in _task12_backend_violations(
        parent_kwargs
    )

    helper_route = {
        "api.py": '''
from offerpilot.routes import operation_undo as route
@app.post(route())
def hidden(): pass
''',
        "routes/__init__.py": "from offerpilot.routes.paths import operation_undo\n",
        "routes/paths.py": '''
ROOT = "/api/write-operations/"
def operation_undo(): return ROOT + "{operation_id}/undo"
''',
    }
    assert "http:generic-operation-undo" in _task12_backend_violations(helper_route)


def test_task12_sixth_review_v1_property_and_compat_cycle_probes() -> None:
    v1_sources = {
        "repositories/interview_preparation_proposals.py": '''
from offerpilot.bridge import holder
def _build_v1_snapshot(): return holder.loader()
''',
        "bridge.py": '''
from offerpilot.v2 import PreparationReadinessSelectionLoader
def execute_loader(): return PreparationReadinessSelectionLoader().load()
class Holder:
    @property
    def loader(self): return execute_loader
holder = Holder()
''',
        "v2.py": "class PreparationReadinessSelectionLoader:\n    def load(self): pass\n",
    }
    trees = {name: _task12_tree(name, source) for name, source in v1_sources.items()}
    assert _task12_preparation_v1_reaches_v2(
        trees["repositories/interview_preparation_proposals.py"], trees
    )

    compat = {
        "product_actions/coordinator.py": '''
from offerpilot.bridge import current, previous
def choose():
    handler = current if USE_NEW_ACTION else previous
    return handler()
''',
        "bridge.py": '''
def cycle(): return cycle()
def current(): return ProductActionCatalogV1() or cycle()
def previous(): return confirm_interview_story_proposal() or cycle()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(compat)
    )


def test_task12_sixth_review_privacy_aggregate_qualified_cache_and_kills() -> None:
    aggregate = {
        "aggregate.py": f'''
def append_value(values, value): values.append(value)
def route():
    values = []
    append_value(values, "{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''
    }
    assert _privacy_canary_violations(aggregate) == ["aggregate.py:JSONResponse"]

    qualified_safe = {
        "qualified.py": f'''
class Safe:
    def __init__(self, value): self.value = value
    def emit(self): return JSONResponse("ok")
class Other:
    def __init__(self, value): self.value = value
    def emit(self): return JSONResponse(self.value)
def route(): return Safe("{_TASK12_PRIVACY_CANARY}").emit()
def unrelated(): return Other("ok").emit()
'''
    }
    assert _privacy_canary_violations(qualified_safe) == []

    killed = {
        "killed.py": f'''
def route():
    value = "{_TASK12_PRIVACY_CANARY}"
    value = "public"
    payload = ["{_TASK12_PRIVACY_CANARY}"]
    payload.clear()
    payload = []
    if False:
        value = "{_TASK12_PRIVACY_CANARY}"
    return JSONResponse({{"value": value, "payload": payload}})
'''
    }
    assert _privacy_canary_violations(killed) == []


def test_task12_sixth_review_previous_title_qualified_cache_and_kills() -> None:
    qualified = _task12_tree(
        "stories/repository.py",
        '''
class Safe:
    def __init__(self, value): self.value = value
    def publish(self): return WriteOperation(result_json="public")
class Other:
    def __init__(self, value): self.value = value
    def publish(self): return WriteOperation(result_json=self.value)
def write(previous_title): return Safe(previous_title).publish()
def unrelated(): return Other("public").publish()
''',
    )
    assert not _task12_previous_title_reaches_public_sink(qualified)

    killed = _task12_tree(
        "stories/repository.py",
        '''
def write(previous_title):
    previous_title = "public"
    return WriteOperation(result_json=previous_title)
''',
    )
    assert not _task12_previous_title_reaches_public_sink(killed)


def test_task12_sixth_review_lexical_flow_negative_probes() -> None:
    safe_shadow = _task12_tree(
        "product_actions/coordinator.py",
        '''
from offerpilot.models import AgentEvent as Model
def safe(session):
    from offerpilot.safe import PublicModel as Model
    session.add(Model())
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py", safe_shadow
    ) == []

    parent_context = {
        "safe.py": '''
from offerpilot.models import WriteOperation
def build(kind): return WriteOperation(adapter_kind=kind)
def unrelated(kind): return kind
def route():
    build("other")
    unrelated("product_action")
'''
    }
    assert _task12_backend_violations(parent_context) == []


def test_task12_seventh_review_parent_mapping_and_constant_branch_probes() -> None:
    literal_spread = {
        "entry.py": '''
from offerpilot.shared import relay
def write(): return relay(**{"kind": "product_action"})
''',
        "shared/__init__.py": "from offerpilot.shared.parent import relay\n",
        "shared/parent.py": '''
from offerpilot.models import WriteOperation
def create(*, kind): return WriteOperation(adapter_kind=kind)
def relay(**opts): return create(**opts)
''',
    }
    assert "ledger:unsealed-parent-writer:shared/parent.py" in _task12_backend_violations(
        literal_spread
    )

    conditional = {
        "entry.py": '''
from offerpilot.shared import create
def write(enabled): return create(enabled)
''',
        "shared/__init__.py": "from offerpilot.shared.parent import create\n",
        "shared/parent.py": '''
from offerpilot.models import WriteOperation
def create(enabled):
    opts = {}
    if enabled:
        opts["adapter_kind"] = "product_action"
    return WriteOperation(**opts)
''',
    }
    assert "ledger:unsealed-parent-writer:shared/parent.py" in _task12_backend_violations(
        conditional
    )

    unreachable = {
        "safe.py": '''
from offerpilot.models import WriteOperation
def write():
    if False:
        return WriteOperation(adapter_kind="product_action")
    return None
'''
    }
    assert _task12_backend_violations(unreachable) == []
    reachable = {
        "safe.py": '''
from offerpilot.models import WriteOperation
def write():
    if True:
        return WriteOperation(adapter_kind="product_action")
'''
    }
    assert "ledger:unsealed-parent-writer:safe.py" in _task12_backend_violations(
        reachable
    )


def test_task12_seventh_review_compat_literal_and_pa_lexical_probes() -> None:
    compat = {
        "product_actions/coordinator.py": '''
from offerpilot.bridge import current, previous
def choose(): return current() if USE_NEW_ACTION else previous()
''',
        "bridge.py": '''
def current(): return "confirm_interview_story"
def previous(): return confirm_interview_story_proposal()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(compat)
    )

    safe_shadow = _task12_tree(
        "product_actions/coordinator.py",
        '''
from offerpilot.models import AgentEvent as Model
from offerpilot.safe import PublicModel
def safe(session, Model=PublicModel):
    callback = lambda Model=PublicModel: Model()
    def inner(Model=PublicModel): return Model()
    session.add(Model())
    return callback(), inner()
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py", safe_shadow
    ) == []

    unsafe_capture = _task12_tree(
        "product_actions/coordinator.py",
        '''
from offerpilot.models import AgentEvent as Model
def unsafe(session):
    def inner(): session.add(Model(kind="decision"))
    return inner()
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py", unsafe_capture
    )

    false_pa = _task12_tree(
        "product_actions/coordinator.py",
        '''
from offerpilot.models import AgentEvent
def safe(session):
    if False:
        session.add(AgentEvent(kind="decision"))
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py", false_pa
    ) == []
    true_pa = _task12_tree(
        "product_actions/coordinator.py",
        '''
from offerpilot.models import AgentEvent
def unsafe(session):
    if True:
        session.add(AgentEvent(kind="decision"))
''',
    )
    assert _task12_product_action_domain_violations(
        "product_actions/coordinator.py", true_pa
    )


def test_task12_seventh_review_privacy_spread_and_nested_capture_probes() -> None:
    spread = {
        "spread.py": f'''
def respond(first, second): return JSONResponse(second)
def route():
    values = ["public"]
    values.append("{_TASK12_PRIVACY_CANARY}")
    return respond(*values)
'''
    }
    assert _privacy_canary_violations(spread) == ["spread.py:JSONResponse"]

    safe_spread = {
        "spread.py": f'''
def respond(first, second): return JSONResponse(second)
def route():
    values = ["{_TASK12_PRIVACY_CANARY}"]
    values.append("public")
    return respond(*values)
'''
    }
    assert _privacy_canary_violations(safe_spread) == []

    nested = {
        "nested.py": f'''
def route():
    secret = "{_TASK12_PRIVACY_CANARY}"
    def publish(): return JSONResponse({{"result": secret}})
    return publish()
'''
    }
    findings = _privacy_canary_violations(nested)
    assert "nested.py:JSONResponse" in findings
    assert "nested.py:result" in findings

    previous_nested = _task12_tree(
        "stories/repository.py",
        '''
def write(previous_title):
    def publish(): return WriteOperation(result_json=previous_title)
    return publish()
''',
    )
    assert _task12_previous_title_reaches_public_sink(previous_nested)

    previous_safe = _task12_tree(
        "stories/repository.py",
        '''
def write(previous_title):
    public = "safe"
    def publish(): return WriteOperation(result_json=public)
    return publish()
''',
    )
    assert not _task12_previous_title_reaches_public_sink(previous_safe)


def test_task12_eighth_review_parent_cfg_and_positional_sql_probes() -> None:
    branch_join = {
        "entry.py": "from offerpilot.shared import write\ndef route(enabled): return write(enabled)\n",
        "shared.py": '''
from offerpilot.models import WriteOperation
def write(enabled):
    opts = {}
    if enabled:
        opts["adapter_kind"] = "product_action"
    else:
        opts["adapter_kind"] = "typed"
    return WriteOperation(**opts)
''',
    }
    assert "ledger:unsealed-parent-writer:shared.py" in _task12_backend_violations(
        branch_join
    )
    safe_join = {
        "safe.py": '''
from offerpilot.models import WriteOperation
def write(enabled):
    opts = {}
    if enabled:
        opts["adapter_kind"] = "typed"
    else:
        opts["adapter_kind"] = "compensation"
    return WriteOperation(**opts)
'''
    }
    assert _task12_backend_violations(safe_join) == []
    for method, placeholder, params in (
        ("execute", "?", '("op", "product_action")'),
        ("execute", "%s", '("op", "product_action")'),
        ("executemany", "?", '[("op", "typed"), ("op2", "product_action")]'),
    ):
        source = {
            "raw.py": f'''
def write(cursor):
    cursor.{method}("INSERT INTO write_operations (operation_id, adapter_kind) VALUES ({placeholder}, {placeholder})", {params})
'''
        }
        assert "ledger:unsealed-parent-writer:raw.py" in _task12_backend_violations(
            source
        )
    safe_sql = {
        "raw.py": '''
def write(cursor):
    cursor.executemany("INSERT INTO other_rows (operation_id, adapter_kind) VALUES (?, ?)", [("op", "product_action")])
'''
    }
    assert _task12_backend_violations(safe_sql) == []


def test_task12_eighth_review_factory_callgraphs_and_compat_early_return_probes() -> None:
    factory_writer = {
        "product_actions/runner.py": '''
from offerpilot.shared import make_writer
def run(session): return make_writer().persist(session)
''',
        "shared.py": '''
from offerpilot.models import AgentEvent
class Writer:
    def persist(self, session): session.add(AgentEvent(kind="decision"))
def make_writer(): return Writer()
''',
    }
    assert "product-action:reachable-chat-journal-write" in _task12_backend_violations(
        factory_writer
    )
    safe_factory = {
        "product_actions/runner.py": '''
from offerpilot.shared import make_writer
def run(session): return make_writer().persist(session)
''',
        "shared.py": '''
class Writer:
    def persist(self, session): return session.get(PublicRow, 1)
def make_writer(): return Writer()
''',
    }
    assert _task12_backend_violations(safe_factory) == []
    early_return = {
        "product_actions/coordinator.py": '''
from offerpilot.bridge import choose
def route(enabled): return choose(enabled)
''',
        "bridge.py": '''
def choose(enabled):
    if enabled:
        return "confirm_interview_story"
    return confirm_interview_story_proposal()
''',
    }
    assert "cutover:compatibility-switch:product_actions/coordinator.py" in (
        _task12_backend_violations(early_return)
    )


def test_task12_eighth_review_keyword_routes_registry_and_provider_cfg() -> None:
    for route, expected in (
        ("/api/stories", "http:stories-alias"),
        ("/api/write-operations/{operation_id}/undo", "http:generic-operation-undo"),
    ):
        sources = {"api.py": f'@app.post(path="{route}")\ndef route(): return None\n'}
        assert expected in _task12_backend_violations(sources)
    safe_route = {"api.py": '@app.get(path="/api/public")\ndef route(): return None\n'}
    assert _task12_backend_violations(safe_route) == []
    duplicated_factory = {
        "api.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
first = make()
second = make()
'''
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(duplicated_factory)
    )
    unreachable_factory = {
        "api.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def unused(): return ProductActionCatalogV1()
'''
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(unreachable_factory)
    )
    provider_false = _task12_tree(
        "ai/tool_authority/safe.py",
        'def build(register):\n    if False: register("confirm_interview_story")\n',
    )
    assert _task12_provider_violations("ai/tool_authority/safe.py", provider_false) == []
    provider_shadow = _task12_tree(
        "ai/tool_authority/safe.py",
        '''
from offerpilot.safe import confirm_interview_story
def build(confirm_interview_story): return register(confirm_interview_story)
''',
    )
    assert _task12_provider_violations(
        "ai/tool_authority/safe.py", provider_shadow
    ) == []
    provider_unsafe = _task12_tree(
        "ai/tool_authority/unsafe.py",
        '''
from offerpilot.safe import confirm_interview_story
def build(): return register(confirm_interview_story)
''',
    )
    assert _task12_provider_violations(
        "ai/tool_authority/unsafe.py", provider_unsafe
    )


def test_task12_eighth_review_privacy_set_add_probe() -> None:
    unsafe = {"set.py": f'''
def route():
    values = set()
    values.add("{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(unsafe) == ["set.py:JSONResponse"]
    safe = {"set.py": f'''
def route():
    values = {{"{_TASK12_PRIVACY_CANARY}"}}
    values.clear()
    values.add("public")
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(safe) == []


def test_task12_ninth_review_cfg_sql_factory_provider_registry_privacy_probes() -> None:
    parent_cfg = {"x.py": '''
from offerpilot.models import WriteOperation
def write(value):
    match value:
        case 1: kind = "product_action"
        case _: kind = "typed"
    try: return WriteOperation(adapter_kind=kind)
    except ValueError: return None
'''}
    assert "ledger:unsealed-parent-writer:x.py" in _task12_backend_violations(parent_cfg)
    parent_mapping_cfg = {"x.py": '''
from offerpilot.models import WriteOperation
def write(value):
    opts = {}
    match value:
        case 1: opts["adapter_kind"] = "product_action"
        case _: opts["adapter_kind"] = "typed"
    try: return WriteOperation(**opts)
    except ValueError: return None
'''}
    assert "ledger:unsealed-parent-writer:x.py" in (
        _task12_backend_violations(parent_mapping_cfg)
    )
    partial_sql = {"x.py": '''
from functools import partial
from offerpilot.models import WriteOperation
make = partial(WriteOperation, adapter_kind="product_action")
def rows(): yield ("id", "product_action")
def write(cursor):
    make()
    cursor.executemany("INSERT INTO write_operations (operation_id, adapter_kind) VALUES (?, ?)", rows())
'''}
    assert "ledger:unsealed-parent-writer:x.py" in _task12_backend_violations(partial_sql)

    factories = {
        "product_actions/run.py": "from offerpilot.shared import make\nasync def run(s): return await make().persist(s)\n",
        "shared.py": '''
from offerpilot.models import AgentEvent
class Writer:
    async def persist(self, session): session.add(AgentEvent(kind="x"))
def make():
    value = Writer()
    return value
''',
    }
    assert "product-action:reachable-chat-journal-write" in _task12_backend_violations(factories)
    compat = {"product_actions/run.py": '''
def choose(value):
    try:
        match value:
            case 1: return "confirm_interview_story"
            case _: return confirm_interview_story_proposal()
    except ValueError: return confirm_interview_story_proposal()
'''}
    assert "cutover:compatibility-switch:product_actions/run.py" in _task12_backend_violations(compat)

    provider = {
        "ai/tool_authority/root.py": "from offerpilot.shared import expose\ndef build(): return expose()\nTOOLS = build()\n",
        "shared.py": 'def expose(): return "confirm_interview_story"\n',
    }
    assert any(item.startswith("provider:") for item in _task12_backend_violations(provider))
    provider_dead = _task12_tree("ai/tool_authority/root.py", 'def build():\n    return None\n    expose("confirm_interview_story")\n')
    assert _task12_provider_violations("ai/tool_authority/root.py", provider_dead) == []

    imported_factory = {
        "api.py": "from offerpilot.shared import make\na=make()\nb=make()\n",
        "shared.py": "from offerpilot.product_actions.catalog import ProductActionCatalogV1\ndef make(): return ProductActionCatalogV1()\n",
    }
    assert "composition:registry-count:ProductActionCatalogV1" in _task12_backend_violations(imported_factory)
    exclusive = {"api.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
catalog = ProductActionCatalogV1() if enabled else ProductActionCatalogV1()
'''}
    assert "composition:registry-count:ProductActionCatalogV1" not in _task12_backend_violations(exclusive)

    unsafe = {"x.py": f'''
from collections import deque
import heapq
def route():
    values = deque()
    values.appendleft("{_TASK12_PRIVACY_CANARY}")
    heapq.heappush(values, "{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''}
    assert "x.py:JSONResponse" in _privacy_canary_violations(unsafe)
    safe = {"x.py": f'''
def route():
    values = {{"{_TASK12_PRIVACY_CANARY}"}}
    values.discard("{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(safe) == []


def test_task12_ninth_review_generator_cross_sql_and_router_registration_probes() -> None:
    generator = {"x.py": '''
def rows(): yield ("id", "product_action")
def write(cursor):
    cursor.executemany("INSERT INTO write_operations (operation_id, adapter_kind) VALUES (?, ?)", rows())
'''}
    assert "ledger:unsealed-parent-writer:x.py" in _task12_backend_violations(generator)
    safe_generator = {"x.py": '''
def rows(): yield ("id", "typed")
def write(cursor):
    cursor.executemany("INSERT INTO write_operations (operation_id, adapter_kind) VALUES (?, ?)", rows())
'''}
    assert _task12_backend_violations(safe_generator) == []
    cross_sql = {
        "entry.py": '''
from offerpilot.shared import write
SQL = "INSERT INTO write_operations (operation_id, adapter_kind) VALUES (?, ?)"
def route(cursor): return write(cursor, SQL, "product_action")
''',
        "shared.py": '''
def write(cursor, sql, kind): return cursor.execute(sql, ("id", kind))
''',
    }
    assert "ledger:unsealed-parent-writer:shared.py" in _task12_backend_violations(cross_sql)
    for registration in (
        'app.api_route("/api/stories", methods=["POST"])(handler)',
        'app.add_api_route("/api/write-operations/{id}/undo", handler, methods=["POST"])',
        '@app.api_route(path="/api/stories", methods=["POST"])\ndef handler(): return None',
        'app.add_api_route(path="/api/write-operations/{id}/undo", endpoint=handler, methods=["POST"])',
    ):
        assert _task12_backend_violations({"api.py": registration})
    included = {
        "api.py": "from offerpilot.routes import router\napp.include_router(router)\n",
        "routes.py": 'router.add_api_route("/api/stories", handler, methods=["POST"])\n',
    }
    assert "http:stories-alias" in _task12_backend_violations(included)
    assert _task12_backend_violations(
        {"api.py": 'app.add_api_route("/api/public", handler, methods=["GET"])'}
    ) == []
    assert _task12_backend_violations(
        {
            "api.py": '@app.api_route(path="/api/public", methods=["GET"])\ndef handler(): return None'
        }
    ) == []


def test_task12_ninth_review_exact_negative_and_container_kill_probes() -> None:
    safe_parent = {"safe.py": '''
from offerpilot.models import WriteOperation
def write(value):
    match value:
        case 1: kind = "typed"
        case _: kind = "typed"
    try: return WriteOperation(adapter_kind=kind)
    except ValueError: return WriteOperation(adapter_kind="typed")
'''}
    assert _task12_backend_violations(safe_parent) == []
    safe_parent_mapping = {"safe.py": '''
from offerpilot.models import WriteOperation
def write(value):
    opts = {}
    match value:
        case 1: opts["adapter_kind"] = "typed"
        case _: opts["adapter_kind"] = "typed"
    try: return WriteOperation(**opts)
    except ValueError: return WriteOperation(adapter_kind="typed")
'''}
    assert _task12_backend_violations(safe_parent_mapping) == []
    safe_compat = {"product_actions/run.py": '''
def choose(value):
    try:
        match value:
            case 1: return confirm_interview_story_proposal()
            case _: return confirm_interview_story_proposal()
    except ValueError: return confirm_interview_story_proposal()
'''}
    assert "cutover:compatibility-switch:product_actions/run.py" not in (
        _task12_backend_violations(safe_compat)
    )
    safe_provider = {
        "ai/tool_authority/root.py": "from offerpilot.shared import expose\ndef build(): return expose()\nTOOLS = build()\n",
        "shared.py": 'def expose(): return "public_tool"\n',
    }
    assert not any(
        item.startswith("provider:") for item in _task12_backend_violations(safe_provider)
    )
    dead_cross_provider = {
        "ai/tool_authority/root.py": '''
from offerpilot.shared import expose
def build():
    return None
    expose()
''',
        "shared.py": 'def expose(): return "confirm_interview_story"\n',
    }
    assert not any(
        item.startswith("provider:")
        for item in _task12_backend_violations(dead_cross_provider)
    )
    referenced_provider = {
        "ai/tool_authority/root.py": "from offerpilot.shared import expose\ndef build(): return expose()\nTOOLS = build()\n",
        "shared.py": '''
from offerpilot.product_actions import confirm_interview_story
def expose(): return confirm_interview_story
''',
    }
    assert "provider:product-action-cross-module" in (
        _task12_backend_violations(referenced_provider)
    )
    exclusive_imported_factory = {
        "api.py": "from offerpilot.shared import make\ncatalog = make() if enabled else make()\n",
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(exclusive_imported_factory)
    )
    unused_imported_factory = {
        "api.py": "from offerpilot.shared import make\ndef unused():\n    make()\n    make()\n",
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(unused_imported_factory)
    )
    reachable_imported_factory = {
        "api.py": "from offerpilot.shared import make\ndef build():\n    make()\n    make()\nbuild()\n",
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(reachable_imported_factory)
    )
    heap_only = {"heap.py": f'''
import heapq
def route():
    values = []
    heapq.heappush(values, "{_TASK12_PRIVACY_CANARY}")
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(heap_only) == ["heap.py:JSONResponse"]
    appendleft_spread = {"deque.py": f'''
from collections import deque
def respond(first, second): return JSONResponse(first)
def route():
    values = deque(["public"])
    values.appendleft("{_TASK12_PRIVACY_CANARY}")
    return respond(*values)
'''}
    assert _privacy_canary_violations(appendleft_spread) == ["deque.py:JSONResponse"]
    safe_pop = {"pop.py": f'''
def route():
    values = ["public", "{_TASK12_PRIVACY_CANARY}"]
    values.pop()
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(safe_pop) == []
    unsafe_pop = {"pop.py": f'''
def route():
    values = ["{_TASK12_PRIVACY_CANARY}", "public"]
    values.pop()
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(unsafe_pop) == ["pop.py:JSONResponse"]
    unrelated_discard = {"discard.py": f'''
def route():
    values = {{"{_TASK12_PRIVACY_CANARY}", "public"}}
    values.discard("public")
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(unrelated_discard) == ["discard.py:JSONResponse"]


def test_task12_tenth_review_parent_provider_and_compatibility_probes() -> None:
    for body in (
        'opts = {}\n    opts |= {"adapter_kind": "product_action"}',
        'opts = {}\n    opts.setdefault("adapter_kind", "product_action")',
        'base = {}\n    opts = base | {"adapter_kind": "product_action"}',
    ):
        sources = {"x.py": f'''
from offerpilot.models import WriteOperation
def write():
    {body}
    return WriteOperation(**opts)
'''}
        assert "ledger:unsealed-parent-writer:x.py" in _task12_backend_violations(
            sources
        )
    safe_parent = {"x.py": '''
from offerpilot.models import WriteOperation
def write():
    opts = {}
    opts |= {"adapter_kind": "typed"}
    opts.setdefault("adapter_kind", "typed")
    opts = opts | {"adapter_kind": "typed"}
    return WriteOperation(**opts)
'''}
    assert _task12_backend_violations(safe_parent) == []
    constant_match = {"x.py": '''
from offerpilot.models import WriteOperation
def write():
    opts = {}
    match 1:
        case 2: opts["adapter_kind"] = "product_action"
        case 1: opts["adapter_kind"] = "typed"
    return WriteOperation(**opts)
'''}
    assert _task12_backend_violations(constant_match) == []
    reachable_match = {"x.py": constant_match["x.py"].replace(
        'case 2: opts["adapter_kind"] = "product_action"',
        'case 2: opts["adapter_kind"] = "typed"',
    ).replace(
        'case 1: opts["adapter_kind"] = "typed"',
        'case 1: opts["adapter_kind"] = "product_action"',
    )}
    assert "ledger:unsealed-parent-writer:x.py" in _task12_backend_violations(
        reachable_match
    )

    module_composition = {
        "ai/tool_authority/root.py": '''
from offerpilot.product_actions import confirm_interview_story
TOOLS = compose(confirm_interview_story)
'''
    }
    assert any(
        item.startswith("provider:")
        for item in _task12_backend_violations(module_composition)
    )
    class_method = {
        "ai/tool_authority/root.py": "from offerpilot.shared import Builder\nTOOLS = Builder().tools()\n",
        "shared.py": '''
from offerpilot.product_actions import confirm_interview_story
class Builder:
    def tools(self): return [confirm_interview_story]
''',
    }
    assert "provider:product-action-cross-module" in _task12_backend_violations(
        class_method
    )
    callback = {
        "ai/tool_authority/root.py": '''
from offerpilot.shared import expose, invoke
TOOLS = invoke(expose)
''',
        "shared.py": '''
def expose(): return "confirm_interview_story"
def invoke(callback): return callback()
''',
    }
    assert "provider:product-action-cross-module" in _task12_backend_violations(
        callback
    )
    unused = {
        "ai/tool_authority/root.py": '''
from offerpilot.shared import expose
def unused(): return expose()
TOOLS = compose("public_tool")
''',
        "shared.py": 'def expose(): return "confirm_interview_story"\n',
    }
    assert not any(
        item.startswith("provider:") for item in _task12_backend_violations(unused)
    )

    for selector in (
        '''handlers = {"new": confirm_interview_story, "old": confirm_interview_story_proposal}
    return handlers[key]()''',
        '''with suppress(ValueError):
        if enabled: return confirm_interview_story()
    return confirm_interview_story_proposal()''',
        '''selected = enabled and confirm_interview_story or confirm_interview_story_proposal
    return selected()''',
    ):
        compat = {"product_actions/run.py": f"def choose(key=None, enabled=False):\n    {selector}\n"}
        assert "cutover:compatibility-switch:product_actions/run.py" in (
            _task12_backend_violations(compat)
        )
    safe_compat = {"product_actions/run.py": '''
def choose(enabled):
    selected = enabled and confirm_interview_story or save_review_readiness_signal
    return selected()
'''}
    assert "cutover:compatibility-switch:product_actions/run.py" not in (
        _task12_backend_violations(safe_compat)
    )


def test_task12_tenth_review_router_registry_and_privacy_probes() -> None:
    prefixed = {
        "api.py": '''
from offerpilot.routes import router
app.include_router(router, prefix="/api")
''',
        "routes.py": '''
router = APIRouter(prefix="/stories")
@router.post("")
def create(): return None
''',
    }
    assert "http:stories-alias" in _task12_backend_violations(prefixed)
    included_only = {
        "api.py": '''
from offerpilot.good_routes import router
app.include_router(router)
''',
        "good_routes.py": '''
router = APIRouter(prefix="/public")
@router.get("")
def read(): return None
''',
        "bad_routes.py": '''
router = APIRouter(prefix="/api/stories")
@router.post("")
def create(): return None
''',
    }
    assert _task12_backend_violations(included_only) == []

    loop_registry = {
        "api.py": '''
from offerpilot.shared import make
for _ in range(2): make()
''',
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(loop_registry)
    )
    comprehension_registry = {
        **loop_registry,
        "api.py": "from offerpilot.shared import make\nitems = [make() for _ in range(2)]\n",
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(comprehension_registry)
    )
    reexport_registry = {
        "api.py": "from offerpilot.registry_pkg import make\nfirst=make()\nsecond=make()\n",
        "registry_pkg/__init__.py": "from offerpilot.registry_pkg.factory import make\n",
        "registry_pkg/factory.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" in (
        _task12_backend_violations(reexport_registry)
    )
    single_registry = {
        **loop_registry,
        "api.py": "from offerpilot.shared import make\nfor _ in range(1): make()\n",
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(single_registry)
    )

    for mutation in (
        f'payload |= {{"result": "{_TASK12_PRIVACY_CANARY}"}}',
        f'payload.__setitem__("result", "{_TASK12_PRIVACY_CANARY}")',
    ):
        leaking = {"privacy.py": f'''
def route():
    payload = {{}}
    {mutation}
    return JSONResponse(payload)
'''}
        assert "privacy.py:JSONResponse" in _privacy_canary_violations(leaking)
    overwritten = {"privacy.py": f'''
def route():
    payload = {{}}
    payload |= {{"result": "{_TASK12_PRIVACY_CANARY}"}}
    payload["result"] = "public"
    return JSONResponse(payload)
'''}
    assert _privacy_canary_violations(overwritten) == []
    safe_popleft = {"privacy.py": f'''
from collections import deque
def route():
    values = deque(["{_TASK12_PRIVACY_CANARY}", "public"])
    values.popleft()
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(safe_popleft) == []
    unsafe_popleft = {"privacy.py": f'''
from collections import deque
def route():
    values = deque(["public", "{_TASK12_PRIVACY_CANARY}"])
    values.popleft()
    return JSONResponse(values)
'''}
    assert _privacy_canary_violations(unsafe_popleft) == ["privacy.py:JSONResponse"]


def test_task12_eleventh_review_parent_compat_and_route_constant_probes() -> None:
    cross_module_parent = {
        "api.py": '''
from offerpilot.shared import make, publish
publish(make())
''',
        "shared.py": '''
from offerpilot.models import WriteOperation
def make(): return {"adapter_kind": "product_action"}
def publish(payload): return WriteOperation(**payload)
''',
    }
    assert "ledger:unsealed-parent-writer:shared.py" in _task12_backend_violations(
        cross_module_parent
    )
    insert_factory_parent = {
        "api.py": '''
from offerpilot.shared import make, publish
publish(session, **make())
''',
        "shared.py": '''
from offerpilot.models import WriteOperation
from sqlalchemy import insert
def make(): return {"adapter_kind": "product_action"}
def publish(session, **kwargs):
    return session.execute(insert(WriteOperation).values(**kwargs))
''',
    }
    assert "ledger:unsealed-parent-writer:shared.py" in _task12_backend_violations(
        insert_factory_parent
    )
    safe_parent = {
        **insert_factory_parent,
        "shared.py": insert_factory_parent["shared.py"].replace(
            '"product_action"', '"typed"'
        ),
    }
    assert not any(
        item.startswith("ledger:unsealed-parent-writer")
        for item in _task12_backend_violations(safe_parent)
    )

    early_return = {
        "product_actions/run.py": '''
from offerpilot.shared import choose
ACTIVE = choose(ENABLED)
''',
        "shared.py": '''
def choose(enabled):
    if enabled:
        return confirm_interview_story()
    return confirm_interview_story_proposal()
''',
    }
    assert "cutover:compatibility-switch:product_actions/run.py" in (
        _task12_backend_violations(early_return)
    )
    safe_early_return = {
        **early_return,
        "shared.py": early_return["shared.py"].replace(
            "confirm_interview_story_proposal", "save_review_readiness_signal"
        ),
    }
    assert "cutover:compatibility-switch:product_actions/run.py" not in (
        _task12_backend_violations(safe_early_return)
    )

    for route_expression in (
        "''.join(('/api', '/stories'))",
        "'{}/stories'.format(PREFIX)",
    ):
        route = {"api.py": f'''\nPREFIX = "/api"\n@app.post({route_expression})\ndef create(): return None\n'''}
        assert "http:stories-alias" in _task12_backend_violations(route)
    safe_routes = {
        "api.py": '''
PREFIX = "/api"
@app.post("".join((PREFIX, "/public")))
def create(): return None
@app.get("{}/stories-readonly".format(PREFIX))
def read(): return None
'''
    }
    assert _task12_backend_violations(safe_routes) == []


def test_task12_eleventh_review_registry_and_container_copy_taint_probes() -> None:
    registry_cases = (
        {
            "api.py": '''
from offerpilot.shared import invoke, make
invoke(make)
invoke(make)
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
def invoke(factory): return factory()
''',
        },
        {
            "api.py": '''
from functools import partial
from offerpilot.product_actions.catalog import ProductActionCatalogV1
make = partial(ProductActionCatalogV1)
first = make()
second = make()
'''
        },
        {
            "api.py": '''
from offerpilot.shared import Builder
first = Builder().make()
second = Builder().make()
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class Builder:
    def make(self): return ProductActionCatalogV1()
''',
        },
        {
            "api.py": '''
from offerpilot.shared import Builder
first = Builder.make()
second = Builder.make()
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class Builder:
    @classmethod
    def make(cls): return ProductActionCatalogV1()
''',
        },
    )
    for sources in registry_cases:
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        )
    for sources in registry_cases:
        safe_registry = {
            **sources,
            "api.py": sources["api.py"].replace("second = make()\n", "").replace(
                "invoke(make)\ninvoke(make)\n", "invoke(make)\n"
            ).replace(
                "second = Builder().make()\n", ""
            ).replace(
                "second = Builder.make()\n", ""
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe_registry)
        )

    for copied in (
        "forwarded = payload.copy()",
        "forwarded = list(payload.values())",
    ):
        canary = {"privacy.py": f'''
def route():
    payload = {{"secret": "{_TASK12_PRIVACY_CANARY}"}}
    {copied}
    return JSONResponse(forwarded)
'''}
        assert _privacy_canary_violations(canary) == ["privacy.py:JSONResponse"]
        previous_title = {"repositories/interview_stories.py": f'''
def route(previous_title):
    payload = {{"secret": previous_title}}
    {copied}
    return JSONResponse(forwarded)
'''}
        assert "privacy:previous-title:repositories/interview_stories.py" in _task12_backend_violations(
            previous_title
        )
    safe_copy = {"privacy.py": f'''
def route():
    private = {{"secret": "{_TASK12_PRIVACY_CANARY}"}}
    forwarded = {{"public": "safe"}}.copy()
    return JSONResponse(forwarded)
'''}
    assert _privacy_canary_violations(safe_copy) == []
    safe_previous_title_copy = {"repositories/interview_stories.py": '''
def route(previous_title):
    private = {"secret": previous_title}
    forwarded = {"public": "safe"}.copy()
    return JSONResponse(forwarded)
'''}
    assert "privacy:previous-title:repositories/interview_stories.py" not in _task12_backend_violations(
        safe_previous_title_copy
    )


def test_task12_twelfth_review_registry_callable_return_and_dispatch_probes() -> None:
    returned_callable = {
        "api.py": '''
from offerpilot.shared import factory
first = factory()()
second = factory()()
''',
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def factory(): return ProductActionCatalogV1
''',
    }
    dict_dispatch = {
        "api.py": '''
from offerpilot.shared import handlers
first = handlers()["make"]()
second = handlers()["make"]()
''',
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
def make(): return ProductActionCatalogV1()
def handlers(): return {"make": make}
''',
    }
    for sources in (returned_callable, dict_dispatch):
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "api.py": sources["api.py"].replace("second = factory()()\n", "").replace(
                'second = handlers()["make"]()\n', ""
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        )
    typed = {
        **dict_dispatch,
        "shared.py": '''
class TypedCatalog: pass
def make(): return TypedCatalog()
def handlers(): return {"make": make}
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(typed)
    )

    provider_cases = (
        {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import factory
TOOLS = factory()()
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def factory(): return action
''',
        },
        {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import handlers
TOOLS = handlers()["action"]()
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def handlers(): return {"action": action}
''',
        },
    )
    for sources in provider_cases:
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        )
        safe_provider = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"confirm_interview_story"', '"public_tool"'
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe_provider)
        )


def test_task12_twelfth_review_route_constants_parent_union_and_copy_taint() -> None:
    route_expressions = (
        '"/api/%s" % "stories"',
        '"{prefix}/stories".format_map({"prefix": "/api"})',
        '"/api/x".replace("/x", "/stories")',
        'posixpath.join("/api", "stories")',
    )
    for expression in route_expressions:
        sources = {
            "api.py": f'''\nimport posixpath\n@app.post({expression})\ndef create(): return None\n'''
        }
        assert "http:stories-alias" in _task12_backend_violations(sources)
    safe_expressions = (
        '"/api/%s" % "public"',
        '"{prefix}/public".format_map({"prefix": "/api"})',
        '"/api/x".replace("/x", "/public")',
        'posixpath.join("/api", "public")',
    )
    for expression in safe_expressions:
        sources = {
            "api.py": f'''\nimport posixpath\n@app.post({expression})\ndef create(): return None\n'''
        }
        assert _task12_backend_violations(sources) == []

    parent_union = {
        "api.py": '''
from offerpilot.shared import make, publish
publish(**make(enabled))
''',
        "shared.py": '''
from offerpilot.models import WriteOperation
def make(enabled):
    match enabled:
        case True: return {"adapter_kind": "product_action"}
        case _:
            try: return {"adapter_kind": "typed"}
            except ValueError: return {"adapter_kind": "typed"}
def publish(**payload): return WriteOperation(**payload)
''',
    }
    assert "ledger:unsealed-parent-writer:shared.py" in _task12_backend_violations(
        parent_union
    )
    typed_parent = {
        **parent_union,
        "shared.py": parent_union["shared.py"].replace(
            '"product_action"', '"typed"'
        ),
    }
    assert not any(
        item.startswith("ledger:unsealed-parent-writer")
        for item in _task12_backend_violations(typed_parent)
    )

    transformations = (
        "forwarded = list(payload.items())",
        "forwarded = payload.__copy__()",
        "forwarded = {key: value for key, value in payload.items()}",
        "forwarded = deepcopy(payload)",
    )
    for transformed in transformations:
        canary = {"privacy.py": f'''
from copy import deepcopy
def route():
    payload = {{"secret": "{_TASK12_PRIVACY_CANARY}"}}
    {transformed}
    return JSONResponse(forwarded)
'''}
        assert _privacy_canary_violations(canary) == ["privacy.py:JSONResponse"]
        previous_title = {"repositories/interview_stories.py": f'''
from copy import deepcopy
def route(previous_title):
    payload = {{"secret": previous_title}}
    {transformed}
    return JSONResponse(forwarded)
'''}
        assert "privacy:previous-title:repositories/interview_stories.py" in (
            _task12_backend_violations(previous_title)
        )
    safe_kill = {"privacy.py": f'''
def route():
    payload = {{"secret": "{_TASK12_PRIVACY_CANARY}"}}
    payload.clear()
    forwarded = list(payload.items())
    return JSONResponse(forwarded)
'''}
    assert _privacy_canary_violations(safe_kill) == []


def test_task12_thirteenth_review_conditional_callable_three_domain_probes() -> None:
    registry_cases = (
        {
            "api.py": '''
from offerpilot.shared import factory
first = factory(enabled)()
second = factory(enabled)()
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def factory(enabled): return make if enabled else typed
''',
        },
        {
            "api.py": '''
from offerpilot.shared import choices
first = choices(enabled).get("make")()
second = choices(enabled).get("make")()
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def choices(enabled): return {"make": make if enabled else typed}
''',
        },
    )
    for sources in registry_cases:
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        )
        single = {
            **sources,
            "api.py": sources["api.py"].replace(
                "second = factory(enabled)()\n", ""
            ).replace('second = choices(enabled).get("make")()\n', ""),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(single)
        )
        typed = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(typed)
        )

    provider_cases = (
        {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import factory
TOOLS = factory(enabled)()
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def factory(enabled):
    if enabled: return action
    return public
''',
        },
        {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import choices
TOOLS = choices(enabled).get("action")()
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def choices(enabled): return {"action": action if enabled else public}
''',
        },
    )
    for sources in provider_cases:
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"confirm_interview_story"', '"public_tool"'
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        )


def test_task12_thirteenth_review_mapping_get_and_vars_copy_taint_probes() -> None:
    canary_get = {"privacy.py": f'''
def route():
    payload = {{"a": "{_TASK12_PRIVACY_CANARY}", "b": "public"}}
    return JSONResponse(payload.get("a"))
'''}
    assert _privacy_canary_violations(canary_get) == ["privacy.py:JSONResponse"]
    safe_get = {
        "privacy.py": canary_get["privacy.py"].replace(
            'payload.get("a")', 'payload.get("b")'
        )
    }
    assert _privacy_canary_violations(safe_get) == []
    previous_get = {"repositories/interview_stories.py": '''
def route(previous_title):
    payload = {"a": previous_title, "b": "public"}
    return JSONResponse(payload.get("a"))
'''}
    assert "privacy:previous-title:repositories/interview_stories.py" in (
        _task12_backend_violations(previous_get)
    )

    vars_copy = {"privacy.py": f'''
class Holder: pass
def route():
    holder = Holder()
    holder.secret = "{_TASK12_PRIVACY_CANARY}"
    return JSONResponse(vars(holder).copy())
'''}
    assert _privacy_canary_violations(vars_copy) == ["privacy.py:JSONResponse"]
    safe_vars = {
        "privacy.py": vars_copy["privacy.py"].replace(
            f'holder.secret = "{_TASK12_PRIVACY_CANARY}"',
            'holder.public = "public"',
        )
    }
    assert _privacy_canary_violations(safe_vars) == []
    previous_vars = {"repositories/interview_stories.py": '''
class Holder: pass
def route(previous_title):
    holder = Holder()
    holder.secret = previous_title
    return JSONResponse(vars(holder).copy())
'''}
    assert "privacy:previous-title:repositories/interview_stories.py" in (
        _task12_backend_violations(previous_vars)
    )


def test_task12_fourteenth_review_return_expression_callable_union_probes() -> None:
    registry_return_expressions = (
        "return {'make': make}.get('make')",
        "return make if enabled else fallback",
    )
    for returned in registry_return_expressions:
        sources = {
            "api.py": '''
from offerpilot.shared import factory
first = factory(enabled)()
second = factory(enabled)()
''',
            "shared.py": f'''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def fallback(): return TypedCatalog()
def factory(enabled): {returned}
''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        )

    provider_return_expressions = (
        "return {'action': action}.get('action')",
        "return action if enabled else public",
    )
    for returned in provider_return_expressions:
        sources = {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import factory
TOOLS = factory(enabled)()
''',
            "shared.py": f'''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def factory(enabled): {returned}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"confirm_interview_story"', '"public_tool"'
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        )


def test_task12_fifteenth_review_known_key_mapping_callable_probes() -> None:
    registry_factories = (
        (
            '''KEY = "make"
def factory(enabled):
    choices = {"make": make}
    return choices.pop(KEY, fallback)
''',
            '''KEY = "missing"
def factory(enabled):
    choices = {"make": make}
    return choices.pop(KEY, fallback)
''',
        ),
        (
            '''KEY = "make"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, make)
''',
            '''KEY = "make"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, fallback)
''',
        ),
        (
            '''KEY = "make"
def factory(enabled): return {"make": make, "typed": fallback}[KEY]
''',
            '''KEY = "typed"
def factory(enabled): return {"make": make, "typed": fallback}[KEY]
''',
        ),
    )
    for factory_source, safe_factory_source in registry_factories:
        sources = {
            "api.py": '''
from offerpilot.shared import factory
first = factory(enabled)()
second = factory(enabled)()
''',
            "shared.py": f'''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def fallback(): return TypedCatalog()
{factory_source}''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                factory_source,
                safe_factory_source,
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        )

    provider_factories = (
        (
            '''KEY = "action"
def factory(enabled):
    choices = {"action": action}
    return choices.pop(KEY, public)
''',
            '''KEY = "missing"
def factory(enabled):
    choices = {"action": action}
    return choices.pop(KEY, public)
''',
        ),
        (
            '''KEY = "action"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, action)
''',
            '''KEY = "action"
def factory(enabled):
    choices = {}
    return choices.setdefault(KEY, public)
''',
        ),
        (
            '''KEY = "action"
def factory(enabled): return {"action": action, "public": public}[KEY]
''',
            '''KEY = "public"
def factory(enabled): return {"action": action, "public": public}[KEY]
''',
        ),
    )
    for factory_source, safe_factory_source in provider_factories:
        sources = {
            "ai/tool_authority/root.py": '''
from offerpilot.shared import factory
TOOLS = factory(enabled)()
''',
            "shared.py": f'''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
{factory_source}''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                factory_source,
                safe_factory_source,
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        )


def test_task12_fifteenth_review_getattr_taint_probes() -> None:
    canary = {"privacy.py": f'''
ATTR = "a"
def route():
    holder = Holder()
    holder.a = "{_TASK12_PRIVACY_CANARY}"
    holder.b = "public"
    return JSONResponse(getattr(holder, ATTR))
'''}
    assert _privacy_canary_violations(canary) == ["privacy.py:JSONResponse"]
    safe = {
        "privacy.py": canary["privacy.py"].replace('ATTR = "a"', 'ATTR = "b"')
    }
    assert _privacy_canary_violations(safe) == []
    previous_title = {"repositories/interview_stories.py": '''
def route(previous_title):
    attr = "a"
    holder = Holder()
    holder.a = previous_title
    holder.b = "public"
    return JSONResponse(getattr(holder, attr))
'''}
    assert "privacy:previous-title:repositories/interview_stories.py" in (
        _task12_backend_violations(previous_title)
    )


def test_task12_sixteenth_review_provider_caller_mapping_dispatch_probes() -> None:
    provider_roots = (
        "TOOLS = handlers().pop('action', public)()",
        "TOOLS = handlers().setdefault('action', public)()",
        "KEY = 'action'\nTOOLS = handlers()[KEY]()",
    )
    for root in provider_roots:
        sources = {
            "ai/tool_authority/root.py": f'''
from offerpilot.shared import handlers, public
{root}
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {"action": action, "public": public}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        )
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"action": action',
                '"action": public',
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        )

    missing_default = {
        "ai/tool_authority/root.py": '''
from offerpilot.shared import handlers, public
TOOLS = handlers().pop("missing", public)()
''',
        "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {"action": action}
''',
    }
    assert not any(
        item.startswith("provider:")
        for item in _task12_backend_violations(missing_default)
    )


def test_task12_seventeenth_review_mapping_value_iteration_dispatch_probes() -> None:
    provider_expressions = (
        "TOOLS = next(iter(handlers().values()))()",
        "TOOLS = list(handlers().values())[0]()",
        "TOOLS = next(iter(handlers().items()))[1]()",
        "TOOLS = [item() for item in handlers().values()]",
        "TOOLS = handlers().popitem()[1]()",
    )
    for expression in provider_expressions:
        sources = {
            "ai/tool_authority/root.py": f'''
from offerpilot.shared import handlers
{expression}
''',
            "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {"action": action, "public": public}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        ), expression
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"action": action',
                '"action": public',
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        ), expression

    registry_expressions = (
        "next(iter(handlers().values()))()",
        "list(handlers().values())[0]()",
        "next(iter(handlers().items()))[1]()",
        "[item() for item in handlers().values()]",
        "handlers().popitem()[1]()",
    )
    for expression in registry_expressions:
        sources = {
            "api.py": f'''
from offerpilot.shared import handlers
first = {expression}
second = {expression}
''',
            "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def handlers(): return {"make": make, "typed": typed}
''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        ), expression
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        ), expression


def test_task12_eighteenth_review_mapping_parameter_propagation_probes() -> None:
    provider_cases = (
        (
            "TOOLS = pick(handlers())()",
            "def pick(mapping): return next(iter(mapping.values()))",
        ),
        (
            "TOOLS = pick(handlers())()",
            'def pick(mapping): return mapping.get("action")',
        ),
        (
            "TOOLS = pick(handlers)()",
            "def pick(factory): return next(iter(factory().values()))",
        ),
    )
    for root, picker in provider_cases:
        sources = {
            "ai/tool_authority/root.py": f'''
from offerpilot.shared import handlers, pick
{root}
''',
            "shared.py": f'''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {{"action": action, "public": public}}
{picker}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        ), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"action": action',
                '"action": public',
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        ), picker

    registry_cases = (
        (
            "pick(handlers())()",
            "def pick(mapping): return next(iter(mapping.values()))",
        ),
        (
            "pick(handlers())()",
            'def pick(mapping): return mapping.get("make")',
        ),
        (
            "pick(handlers)()",
            "def pick(factory): return next(iter(factory().values()))",
        ),
    )
    for expression, picker in registry_cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import handlers, pick
first = {expression}
second = {expression}
''',
            "shared.py": f'''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def handlers(): return {{"make": make, "typed": typed}}
{picker}
''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        ), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        ), picker


def test_task12_nineteenth_review_constructor_and_closure_mapping_probes() -> None:
    provider_cases = (
        (
            "Picker(handlers()).pick()()",
            '''class Picker:
    def __init__(self, mapping): self.mapping = mapping
    def pick(self): return self.mapping.get("action")''',
        ),
        (
            "factory(handlers())()",
            '''def factory(mapping):
    def pick(): return mapping.get("action")
    return pick''',
        ),
    )
    for expression, picker in provider_cases:
        sources = {
            "ai/tool_authority/root.py": f'''
from offerpilot.shared import Picker, factory, handlers
TOOLS = {expression}
''',
            "shared.py": f'''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {{"action": action, "public": public}}
{picker}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        ), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"action": action',
                '"action": public',
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        ), picker

    registry_cases = (
        (
            "Picker(handlers()).pick()",
            '''class Picker:
    def __init__(self, mapping): self.mapping = mapping
    def pick(self): return self.mapping.get("make")''',
        ),
        (
            "factory(handlers())()",
            '''def factory(mapping):
    def pick(): return mapping.get("make")
    return pick''',
        ),
    )
    for expression, picker in registry_cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import Picker, factory, handlers
first = {expression}
second = {expression}
''',
            "shared.py": f'''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def handlers(): return {{"make": make, "typed": typed}}
{picker}
''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        ), picker
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        ), picker


def test_task12_twentieth_review_post_init_and_closure_alias_probes() -> None:
    provider_cases = (
        (
            '''picker = Picker()
picker.mapping = handlers()
TOOLS = picker.pick()()''',
            '''class Picker:
    def pick(self): return self.mapping.get("action")''',
        ),
        (
            "TOOLS = factory(handlers())()",
            '''def factory(source):
    mapping = source
    def pick(): return mapping.get("action")
    return pick''',
        ),
    )
    for root, implementation in provider_cases:
        sources = {
            "ai/tool_authority/root.py": f'''
from offerpilot.shared import Picker, factory, handlers
{root}
''',
            "shared.py": f'''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {{"action": action}}
{implementation}
''',
        }
        assert "provider:product-action-cross-module" in (
            _task12_backend_violations(sources)
        ), implementation
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                '"action": action',
                '"action": public',
            ),
        }
        assert not any(
            item.startswith("provider:")
            for item in _task12_backend_violations(safe)
        ), implementation

    provider_safe_rebind = {
        "ai/tool_authority/root.py": '''
from offerpilot.shared import Picker, factory, handlers, public_handlers
picker = Picker()
picker.mapping = handlers()
picker.mapping = public_handlers()
TOOLS = picker.pick()()
TOOLS_FROM_CLOSURE = factory(handlers())()
''',
        "shared.py": '''
def action(): return "confirm_interview_story"
def public(): return "public_tool"
def handlers(): return {"action": action}
def public_handlers(): return {"action": public}
class Picker:
    def pick(self): return self.mapping.get("action")
def factory(source):
    mapping = source
    mapping = public_handlers()
    def pick(): return mapping.get("action")
    return pick
''',
    }
    assert not any(
        item.startswith("provider:")
        for item in _task12_backend_violations(provider_safe_rebind)
    )

    registry_cases = (
        (
            '''picker = Picker()
picker.mapping = handlers()
first = picker.pick()()
second = picker.pick()()''',
            '''class Picker:
    def pick(self): return self.mapping.get("make")''',
        ),
        (
            '''first = factory(handlers())()
second = factory(handlers())()''',
            '''def factory(source):
    mapping = source
    def pick(): return mapping.get("make")
    return pick''',
        ),
    )
    for root, implementation in registry_cases:
        sources = {
            "api.py": f'''
from offerpilot.shared import Picker, factory, handlers
{root}
''',
            "shared.py": f'''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def handlers(): return {{"make": make}}
{implementation}
''',
        }
        assert "composition:registry-count:ProductActionCatalogV1" in (
            _task12_backend_violations(sources)
        ), implementation
        safe = {
            **sources,
            "shared.py": sources["shared.py"].replace(
                "def make(): return ProductActionCatalogV1()",
                "def make(): return TypedCatalog()",
            ),
        }
        assert "composition:registry-count:ProductActionCatalogV1" not in (
            _task12_backend_violations(safe)
        ), implementation

    registry_safe_rebind = {
        "api.py": '''
from offerpilot.shared import Picker, factory, handlers, typed_handlers
picker = Picker()
picker.mapping = handlers()
picker.mapping = typed_handlers()
first = picker.pick()()
second = picker.pick()()
third = factory(handlers())()
fourth = factory(handlers())()
''',
        "shared.py": '''
from offerpilot.product_actions.catalog import ProductActionCatalogV1
class TypedCatalog: pass
def make(): return ProductActionCatalogV1()
def typed(): return TypedCatalog()
def handlers(): return {"make": make}
def typed_handlers(): return {"make": typed}
class Picker:
    def pick(self): return self.mapping.get("make")
def factory(source):
    mapping = source
    mapping = typed_handlers()
    def pick(): return mapping.get("make")
    return pick
''',
    }
    assert "composition:registry-count:ProductActionCatalogV1" not in (
        _task12_backend_violations(registry_safe_rebind)
    )
