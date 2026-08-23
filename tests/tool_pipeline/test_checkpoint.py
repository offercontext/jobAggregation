from __future__ import annotations

import ast
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "offerpilot"
AI = SRC / "ai"

_FORBIDDEN_LEGACY_NAMES = {
    "LangGraphAgentRunner",
    "StateGraph",
    "InMemorySaver",
    "SqliteSaver",
    "_GraphState",
    "_resume_without_checkpoint",
    "_FALLBACK_CONFIRMATION_CLAIMS",
    "_CONFIRMATION_LOCKS",
    "_CONFIRMATION_STATE_GUARD",
}
_FORBIDDEN_DEPENDENCIES = {
    "langgraph",
    "langgraph-checkpoint-sqlite",
}


def _production_files() -> tuple[Path, ...]:
    return tuple(sorted(SRC.rglob("*.py")))


def _names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
    return names


def test_langgraph_dependencies_are_removed_from_project_and_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    dependency_names = {
        entry.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0]
        for entry in dependencies
    }
    assert not dependency_names & _FORBIDDEN_DEPENDENCIES

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for dependency in _FORBIDDEN_DEPENDENCIES:
        assert f'name = "{dependency}"' not in lock


def test_legacy_agent_module_and_tests_are_deleted() -> None:
    assert not (AI / "agent.py").exists()
    assert not (ROOT / "tests" / "test_ai_agent.py").exists()


def test_production_has_no_langgraph_graph_or_process_fallback_symbols() -> None:
    findings: list[str] = []
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        if "langgraph" in source.lower():
            findings.append(f"{path.relative_to(ROOT)}:langgraph")
        names = _names(ast.parse(source, filename=str(path)))
        for symbol in sorted(names & _FORBIDDEN_LEGACY_NAMES):
            findings.append(f"{path.relative_to(ROOT)}:{symbol}")
    assert findings == []


def test_agent_loop_is_the_only_model_execution_owner() -> None:
    loop = AI / "agent_loop.py"
    assert loop.exists()
    source = loop.read_text(encoding="utf-8")
    assert "class AgentLoopRunner" in source
    assert "while " in source
    assert "def run(" in source


def test_api_has_no_checkpoint_path_or_resume_entrypoint() -> None:
    source = (SRC / "api.py").read_text(encoding="utf-8")
    assert "_agent_checkpoint_path" not in source
    assert "agent-checkpoints.sqlite" not in source
    assert "resume_after_confirm" not in source
    assert "run_turn" not in source
