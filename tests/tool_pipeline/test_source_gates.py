from __future__ import annotations

import ast
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "offerpilot"
AI = SRC / "ai"

BANNED_SYMBOLS = {
    "application_jd_version_tool_registry",
    "application_tool_registry",
    "event_tool_registry",
    "jd_tool_registry",
    "note_tool_registry",
    "offer_tool_registry",
    "offerpilot_tool_registry",
    "resume_tool_registry",
    "_execute_tool",
    "_model_visible_tools",
}
BANNED_DICT_PROTOCOL_KEYS = {"handler", "model_visible", "validate", "write"}
BANNED_SWITCH_FRAGMENTS = {
    "dual_run",
    "legacy_fallback",
    "shadow_execution",
    "shadow_write",
    "tool_pipeline_enabled",
}


def _production_files() -> tuple[Path, ...]:
    return tuple(sorted(SRC.rglob("*.py")))


def _pipeline_production_files() -> tuple[Path, ...]:
    return (*tuple(sorted(AI.rglob("*.py"))), SRC / "api.py")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> set[str]:
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    modules.update(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    return modules


def _literal_protocol_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def test_deleted_registry_symbols_and_modules_cannot_return() -> None:
    assert not (AI / "tools.py").exists()
    assert not (ROOT / "tests" / "tool_pipeline" / "compat_registry.py").exists()
    assert not (ROOT / "tests" / "test_ai_tools.py").exists()
    findings: list[str] = []
    for path in _production_files():
        for node in ast.walk(_tree(path)):
            symbol = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Name)):
                symbol = node.name if hasattr(node, "name") else node.id
            elif isinstance(node, ast.Attribute):
                symbol = node.attr
            if symbol in BANNED_SYMBOLS:
                findings.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{symbol}")
    assert findings == []


def test_agent_provider_and_api_have_no_dict_handler_protocol() -> None:
    findings: list[str] = []
    for path in (AI / "agent_loop.py", AI / "agent_contracts.py", AI / "client.py", SRC / "api.py"):
        for node in ast.walk(_tree(path)):
            key = _literal_protocol_key(node)
            if key in BANNED_DICT_PROTOCOL_KEYS:
                findings.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{key}")
    assert findings == []


def test_agent_tests_use_typed_tool_factory_not_legacy_dict_protocol() -> None:
    # The old monolithic Agent test module was deleted with the implementation.
    # New loop tests live under tests/agent_loop and use typed ToolSpec factories.
    assert not (ROOT / "tests" / "test_ai_agent.py").exists()
    for path in sorted((ROOT / "tests" / "agent_loop").glob("*.py")):
        findings: list[str] = []
        for node in ast.walk(_tree(path)):
            key = _literal_protocol_key(node)
            if key in BANNED_DICT_PROTOCOL_KEYS:
                findings.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{key}")
            if isinstance(node, ast.Dict):
                for key_node in node.keys:
                    if (
                        isinstance(key_node, ast.Constant)
                        and key_node.value in BANNED_DICT_PROTOCOL_KEYS
                    ):
                        findings.append(
                            f"{path.relative_to(ROOT)}:{getattr(key_node, 'lineno', 0)}:{key_node.value}"
                        )
        assert findings == []


def test_runtime_dependency_direction_and_legacy_dispatch_are_closed() -> None:
    for path in (AI / "tool_runtime").rglob("*.py"):
        assert not any(
            module.startswith("offerpilot.ai.tool_specs")
            for module in _imported_modules(_tree(path))
        ), path
    for path in (AI / "agent_loop.py", AI / "agent_contracts.py", AI / "confirmation.py"):
        agent_imports = _imported_modules(_tree(path))
        assert "offerpilot.ai.tool_runtime.legacy" not in agent_imports
        assert "offerpilot.ai.tool_specs.legacy" not in agent_imports


def test_compatibility_string_inspection_is_confined_to_renderer() -> None:
    findings: list[str] = []
    renderer = AI / "tool_runtime" / "rendering.py"
    for path in _production_files():
        for node in ast.walk(_tree(path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"startswith", "removeprefix"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "错误："
            ):
                continue
            if path != renderer or node.func.attr != "startswith":
                findings.append(
                    f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}:{node.func.attr}"
                )
    assert findings == []


def test_no_shadow_dual_run_or_hidden_pipeline_switch_exists() -> None:
    findings: list[str] = []
    for path in _pipeline_production_files():
        tree = _tree(path)
        identifiers = {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers.update(
            node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        )
        identifiers.update(
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        for identifier in identifiers:
            for fragment in BANNED_SWITCH_FRAGMENTS:
                if fragment in identifier:
                    findings.append(f"{path.relative_to(ROOT)}:{identifier}")
    assert findings == []


def test_provider_builder_accepts_only_provider_contract() -> None:
    tree = _tree(AI / "client.py")
    builder = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_openai_tool"
    )
    parameter = builder.args.args[0]
    assert isinstance(parameter.annotation, ast.Name)
    assert parameter.annotation.id == "ProviderToolContract"


def test_jsonschema_is_exactly_pinned_in_project_and_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert dependencies.count("jsonschema==4.26.0") == 1

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "jsonschema"\nversion = "4.26.0"' in lock
    assert '{ name = "jsonschema", specifier = "==4.26.0" }' in lock
