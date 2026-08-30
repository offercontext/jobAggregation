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
        if terminal != "_record_migration":
            continue
        if any(_literal_string(argument) == MIGRATION for argument in node.args):
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
    model_names = {"ApplicationEvent"}
    delete_names = {"delete"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "offerpilot.models":
            model_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "ApplicationEvent"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            delete_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "delete"
            )

    violations: list[str] = []

    class DeleteVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            owner = self.functions[-1] if self.functions else ""
            direct_sql_delete = (
                isinstance(node.func, ast.Name)
                and node.func.id in delete_names
                and bool(node.args)
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in model_names
            )
            orm_delete = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete"
                and bool(node.args)
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"event", "application_event"}
            )
            approved = (
                path.as_posix().endswith("repositories/application_events.py")
                and owner == "_delete_application_event_owned"
            )
            if (direct_sql_delete or orm_delete) and not approved:
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
            self.generic_visit(node)

    DeleteVisitor().visit(tree)
    return violations


def _source_violations() -> list[str]:
    violations: list[str] = []
    db_tree = _parse(SRC / "db.py")
    product_action_dir = SRC / "product_actions"
    product_action_modules = {
        name: _parse(product_action_dir / name) for name in PRODUCT_ACTION_MODULES
    }
    notes_tree = _parse(SRC / "repositories" / "notes.py")
    practice_tree = _parse(SRC / "repositories" / "adaptive_interview_practice.py")

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


def test_application_event_delete_owner_detector_rejects_direct_sql_and_orm_paths() -> None:
    direct = ast.parse(
        "def delete_event(session):\n    session.execute(delete(ApplicationEvent))\n"
    )
    orm = ast.parse("def delete_event(session, event):\n    session.delete(event)\n")
    owner = ast.parse(
        "def _delete_application_event_owned(session):\n"
        "    session.execute(delete(ApplicationEvent))\n"
    )
    arbitrary = ROOT / "src" / "offerpilot" / "other.py"
    approved = ROOT / "src" / "offerpilot" / "repositories" / "application_events.py"

    assert _application_event_delete_violations(arbitrary, direct)
    assert _application_event_delete_violations(arbitrary, orm)
    assert _application_event_delete_violations(approved, owner) == []


def test_application_event_hard_delete_has_one_production_owner() -> None:
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        tree = _parse(path)
        if tree is not None:
            violations.extend(_application_event_delete_violations(path, tree))
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


def test_review_to_readiness_production_cutover_gate() -> None:
    # Intentional RED until Tasks 1-8 remove every named production gap.
    assert _source_violations() == []
