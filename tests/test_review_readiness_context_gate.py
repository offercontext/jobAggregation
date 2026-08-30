from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from offerpilot.review_readiness.contributor import (
    ConfirmedReadinessContributorPort,
    ConfirmedReadinessContributorValidationError,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "offerpilot"
CONTRIBUTOR = SRC / "review_readiness" / "contributor.py"
SELECTION_LOADER = SRC / "review_readiness" / "preparation_selection.py"
PREPARATION_OWNER = SRC / "repositories" / "interview_preparation_proposals.py"
_SELECTION_MODULE_KEY = "review_readiness/preparation_selection.py"
_PREPARATION_OWNER_KEY = "repositories/interview_preparation_proposals.py"

_SYNTHETIC_SELECTION = {
    "ordered_version_ids": [11, 17, 23],
    "application_id": 3,
    "target_application_event_id": 29,
    "resume_id": 31,
    "selection_fingerprint": "sha256:" + "a" * 64,
}


def _production_python_files() -> tuple[Path, ...]:
    return tuple(sorted(SRC.rglob("*.py")))


def _imports_and_names(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return imports, names


def _constant_text(node: ast.AST, bindings: dict[str, str] | None = None) -> str | None:
    bindings = bindings or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_text(node.left, bindings)
        right = _constant_text(node.right, bindings)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _constant_text(value.value, bindings)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _resolved_constant_strings(tree: ast.AST) -> set[str]:
    bindings: dict[str, str] = {}
    while True:
        before = dict(bindings)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            resolved = _constant_text(node.value, bindings)
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = resolved
        if bindings == before:
            break
    values = set(bindings.values())
    for node in ast.walk(tree):
        resolved = _constant_text(node, bindings)
        if resolved is not None:
            values.add(resolved)
    return values


def _future_contributor_findings(sources: dict[str, str]) -> list[str]:
    module = "offerpilot.review_readiness.contributor"
    parent_module = "offerpilot.review_readiness"
    symbol = "ConfirmedReadinessContributorPort"
    findings: list[str] = []
    for name, source in sources.items():
        if name == "review_readiness/contributor.py":
            continue
        tree = ast.parse(source, filename=name)
        imports: set[str] = set()
        imported_names: set[str] = set()
        attributes: set[str] = set()
        strings = _resolved_constant_strings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                imported_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                attributes.add(node.attr)
        if (
            module in imports
            or symbol in imported_names
            or module in strings
            or symbol in strings
            or (
                parent_module in imports
                and ("contributor" in imported_names or "contributor" in attributes or "contributor" in strings)
            )
        ):
            findings.append(name)
    return findings


def _selection_loader_findings(sources: dict[str, str]) -> list[str]:
    """Close the future loader around one direct owner-local snapshot load site."""

    findings: list[str] = []
    loader_exists = _SELECTION_MODULE_KEY in sources
    for name, source in sources.items():
        if name == _SELECTION_MODULE_KEY:
            continue
        tree = ast.parse(source, filename=name)
        imports: set[str] = set()
        symbols: set[str] = set()
        strings = _resolved_constant_strings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
                symbols.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                symbols.add(node.id)
            elif isinstance(node, ast.Attribute):
                symbols.add(node.attr)
        reaches_loader = (
            "offerpilot.review_readiness.preparation_selection" in imports
            or "PreparationReadinessSelectionLoader" in symbols
            or any(
                "review_readiness.preparation_selection" in value
                or "PreparationReadinessSelectionLoader" in value
                for value in strings
            )
        )
        if reaches_loader and name != _PREPARATION_OWNER_KEY:
            findings.append(f"outside-owner:{name}")

    if not loader_exists:
        if _PREPARATION_OWNER_KEY in sources:
            owner_tree = ast.parse(
                sources[_PREPARATION_OWNER_KEY], filename=_PREPARATION_OWNER_KEY
            )
            if "PreparationReadinessSelectionLoader" in {
                node.id for node in ast.walk(owner_tree) if isinstance(node, ast.Name)
            }:
                findings.append("owner-references-absent-loader")
        return findings

    owner_source = sources.get(_PREPARATION_OWNER_KEY)
    if owner_source is None:
        return [*findings, "missing-owner"]
    owner_tree = ast.parse(owner_source, filename=_PREPARATION_OWNER_KEY)
    constructor_aliases: set[str] = set()
    direct_import_count = 0
    for node in owner_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == (
            "offerpilot.review_readiness.preparation_selection"
        ):
            for alias in node.names:
                if alias.name == "PreparationReadinessSelectionLoader":
                    direct_import_count += 1
                    constructor_aliases.add(alias.asname or alias.name)
    if direct_import_count != 1 or len(constructor_aliases) != 1:
        findings.append("loader-direct-import-count")
        return sorted(set(findings))

    parents = {
        child: parent
        for parent in ast.walk(owner_tree)
        for child in ast.iter_child_nodes(parent)
    }

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent
            parent = parents.get(parent)
        return None

    constructors: list[tuple[ast.Call, str, ast.FunctionDef | ast.AsyncFunctionDef | None]] = []
    for node in ast.walk(owner_tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Name) and node.func.id in constructor_aliases
        ):
            continue
        assignment = parents.get(node)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is not node:
            findings.append("loader-constructor-not-assigned")
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constructors.append((node, target.id, enclosing_function(node)))
            else:
                findings.append("loader-not-owner-local")

    if len(constructors) != 1:
        findings.append("loader-constructor-count")
        return sorted(set(findings))
    constructor, owned_name, owner_scope = constructors[0]
    if owner_scope is None:
        findings.append("loader-module-global")

    load_calls: list[ast.Call] = []
    for node in ast.walk(owner_tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == owned_name
            ):
                load_calls.append(node)
                continue
            if node is constructor:
                continue
            if any(
                isinstance(child, ast.Name) and child.id == owned_name
                for argument in (*node.args, *(item.value for item in node.keywords))
                for child in ast.walk(argument)
            ):
                findings.append("loader-escaped-through-call")
        elif isinstance(node, ast.Return) and node.value is not None:
            returned = node.value
            returns_load_result = (
                isinstance(returned, ast.Call)
                and isinstance(returned.func, ast.Attribute)
                and returned.func.attr == "load"
                and isinstance(returned.func.value, ast.Name)
                and returned.func.value.id == owned_name
            )
            if not returns_load_result and any(
                isinstance(child, ast.Name) and child.id == owned_name
                for child in ast.walk(returned)
            ):
                findings.append("loader-returned")
        elif isinstance(node, (ast.Yield, ast.YieldFrom)) and node.value is not None:
            if any(
                isinstance(child, ast.Name) and child.id == owned_name
                for child in ast.walk(node.value)
            ):
                findings.append("loader-returned")
        elif isinstance(node, ast.NamedExpr):
            if any(
                isinstance(child, ast.Name) and child.id == owned_name
                for child in ast.walk(node.value)
            ):
                findings.append("loader-aliased")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if any(
                isinstance(child, ast.Name) and child.id == owned_name
                for child in ast.walk(node.value)
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(not isinstance(target, ast.Name) for target in targets):
                    findings.append("loader-stored-outside-local")
                elif node.value is not constructor:
                    findings.append("loader-aliased")
    if len(load_calls) != 1:
        findings.append("loader-snapshot-load-count")
    elif enclosing_function(load_calls[0]) is not owner_scope:
        findings.append("loader-cross-scope-use")
    return sorted(set(findings))


def test_future_contributor_port_validates_only_the_closed_synthetic_schema() -> None:
    assert ConfirmedReadinessContributorPort.validate_synthetic(
        _SYNTHETIC_SELECTION
    ) == json.dumps(
        _SYNTHETIC_SELECTION,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    invalid = (
        {**_SYNTHETIC_SELECTION, "extra": "not allowed"},
        {**_SYNTHETIC_SELECTION, "ordered_version_ids": [11, 11]},
        {**_SYNTHETIC_SELECTION, "ordered_version_ids": [11, True]},
        {**_SYNTHETIC_SELECTION, "ordered_version_ids": list(range(1, 10))},
        {**_SYNTHETIC_SELECTION, "application_id": True},
        {**_SYNTHETIC_SELECTION, "target_application_event_id": 0},
        {**_SYNTHETIC_SELECTION, "resume_id": "31"},
        {**_SYNTHETIC_SELECTION, "selection_fingerprint": "hmac-sha256:" + "a" * 64},
    )
    for value in invalid:
        with pytest.raises(ConfirmedReadinessContributorValidationError):
            ConfirmedReadinessContributorPort.validate_synthetic(value)


def test_future_contributor_port_cannot_construct_or_issue_a_runtime_proof() -> None:
    with pytest.raises(TypeError):
        ConfirmedReadinessContributorPort()
    assert not hasattr(ConfirmedReadinessContributorPort, "issue")
    assert not hasattr(ConfirmedReadinessContributorPort, "create_proof")
    assert not hasattr(ConfirmedReadinessContributorPort, "to_contributor_result")


def test_future_contributor_asset_is_leaf_only_and_production_unreachable() -> None:
    contributor_imports, contributor_names = _imports_and_names(CONTRIBUTOR)
    assert not any(name == "offerpilot" or name.startswith("offerpilot.") for name in contributor_imports)
    assert "ContributorResult" not in contributor_names

    sources = {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in _production_python_files()
    }
    assert _future_contributor_findings(sources) == []


@pytest.mark.parametrize(
    "source",
    (
        "from offerpilot.review_readiness import contributor\n",
        "import offerpilot.review_readiness as rr\nvalue = rr.contributor\n",
        "import importlib\nvalue = importlib.import_module("
        "'offerpilot.review_readiness.contributor')\n",
        "value = __import__('offerpilot.review_readiness.contributor')\n",
        "import offerpilot.review_readiness as rr\n"
        "value = getattr(rr, 'contributor')\n",
        "value = getattr(object(), 'ConfirmedReadinessContributorPort')\n",
        "import importlib\nvalue = importlib.import_module("
        "'offerpilot.review_readiness.' + 'contributor')\n",
        "import offerpilot.review_readiness as rr\n"
        "value = getattr(rr, 'contrib' + 'utor')\n",
        "value = getattr(object(), 'ConfirmedReadiness' + 'ContributorPort')\n",
        "import importlib\nprefix = 'offerpilot.review_readiness.'\n"
        "value = importlib.import_module(prefix + 'contributor')\n",
    ),
)
def test_future_contributor_unreachability_gate_rejects_import_bypasses(source: str) -> None:
    assert _future_contributor_findings({"bypass.py": source}) == ["bypass.py"]


def test_preparation_selection_loader_has_exactly_one_future_owner() -> None:
    sources = {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in _production_python_files()
    }
    assert _selection_loader_findings(sources) == []


def test_preparation_selection_loader_gate_rejects_alias_escape_and_multiple_loads() -> None:
    loader = "class PreparationReadinessSelectionLoader: pass\n"
    valid_owner = (
        "from offerpilot.review_readiness.preparation_selection import "
        "PreparationReadinessSelectionLoader as Loader\n"
        "def load_selection(session):\n"
        "    loader = Loader(session)\n"
        "    return loader.load()\n"
    )
    assert _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: valid_owner,
        }
    ) == []

    escaped = valid_owner.replace(
        "return loader.load()", "forward(loader)\n    return loader.load()"
    )
    assert "loader-escaped-through-call" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: escaped,
            "api.py": "def forward(value):\n    return value.load()\n",
        }
    )
    duplicated = valid_owner.replace("return loader.load()", "loader.load()\n    return loader.load()")
    assert "loader-snapshot-load-count" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: duplicated,
        }
    )

    module_global = (
        "from offerpilot.review_readiness.preparation_selection import "
        "PreparationReadinessSelectionLoader as Loader\n"
        "loader = Loader(None)\n"
        "def load_selection():\n"
        "    return loader.load()\n"
    )
    assert "loader-module-global" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: module_global,
        }
    )
    cross_scope = (
        "from offerpilot.review_readiness.preparation_selection import "
        "PreparationReadinessSelectionLoader as Loader\n"
        "def build(session):\n"
        "    loader = Loader(session)\n"
        "def consume():\n"
        "    return loader.load()\n"
    )
    assert "loader-cross-scope-use" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: cross_scope,
        }
    )
    shadow = (
        "class PreparationReadinessSelectionLoader:\n"
        "    pass\n"
        "def load_selection(session):\n"
        "    loader = PreparationReadinessSelectionLoader(session)\n"
        "    return loader.load()\n"
    )
    assert "loader-direct-import-count" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: shadow,
        }
    )
    dynamic_outside_owner = (
        "import importlib\n"
        "prefix = 'offerpilot.review_readiness.'\n"
        "importlib.import_module(prefix + 'preparation_selection')\n"
    )
    assert _selection_loader_findings(
        {"outside.py": dynamic_outside_owner}
    ) == ["outside-owner:outside.py"]
    escaped_alias = valid_owner.replace(
        "return loader.load()",
        "alias = loader\n    registry.append(alias)\n    return loader.load()",
    )
    assert "loader-aliased" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: escaped_alias,
        }
    )
    duplicate_import = valid_owner.replace(
        "def load_selection(session):",
        "from offerpilot.review_readiness.preparation_selection import "
        "PreparationReadinessSelectionLoader as Loader\n"
        "def load_selection(session):",
    )
    assert "loader-direct-import-count" in _selection_loader_findings(
        {
            _SELECTION_MODULE_KEY: loader,
            _PREPARATION_OWNER_KEY: duplicate_import,
        }
    )


def test_agent_chat_runtime_has_zero_readiness_signal_query_surface() -> None:
    agent_paths = (
        SRC / "ai" / "agent_loop.py",
        SRC / "context_projector" / "contracts.py",
        SRC / "context_projector" / "loader.py",
        SRC / "context_projector" / "manifest.py",
        SRC / "context_projector" / "projector.py",
        SRC / "pilot_runtime" / "composition.py",
        SRC / "pilot_runtime" / "service.py",
        SRC / "chat_transport.py",
    )
    forbidden = {
        "InterviewReadinessSignal",
        "InterviewReadinessSignalVersion",
        "InterviewReadinessSignalEvidence",
        "PreparationReadinessSelectionLoader",
        "ConfirmedReadinessContributorPort",
    }
    findings: dict[str, list[str]] = {}
    for path in agent_paths:
        imports, names = _imports_and_names(path)
        found = sorted(forbidden.intersection(names) | forbidden.intersection(imports))
        if found:
            findings[str(path.relative_to(ROOT))] = found
    assert findings == {}


def test_workspace_application_chat_and_haru_source_loader_cannot_query_signals() -> None:
    api = SRC / "api.py"
    source = api.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(api))
    loader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_load_chat_source_messages"
    )
    loader_source = ast.get_source_segment(source, loader)
    assert loader_source is not None
    lowered = loader_source.lower()
    assert "interview_readiness_signal" not in lowered
    assert "preparationreadinessselectionloader" not in lowered
    assert "review_readiness.preparation_selection" not in lowered
