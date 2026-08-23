from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "offerpilot"
AI = SRC / "ai"

_LEGACY_AGENT_SYMBOLS = {
    "InMemorySaver",
    "LangGraphAgentRunner",
    "SqliteSaver",
    "StateGraph",
    "_CONFIRMATION_LOCKS",
    "_FALLBACK_CONFIRMATION_CLAIMS",
    "_GraphState",
    "_compile_graph",
    "_resume_without_checkpoint",
    "_runtime_resume_after_confirm",
    "resume_after_confirm",
    "resume_after_confirm_fn",
}
_LOOP_FORBIDDEN_IMPORT_PREFIXES = (
    "fastapi",
    "starlette",
    "offerpilot.chat_transport",
    "offerpilot.pilot_runtime",
    "offerpilot.repositories",
)
_OLD_PATH_SWITCH_FRAGMENTS = {
    "agent_loop_enabled",
    "dual_agent",
    "dual_loop",
    "legacy_agent_fallback",
    "old_loop_fallback",
    "shadow_agent",
    "shadow_loop",
}


class GateViolation(AssertionError):
    pass


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _imports(tree: ast.AST) -> set[str]:
    result = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    result.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    return result


def _symbols(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            result.add(node.id)
        elif isinstance(node, ast.Attribute):
            result.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.add(node.name)
        elif isinstance(node, ast.alias):
            result.add(node.asname or node.name.rsplit(".", 1)[-1])
    return result


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise GateViolation(f"function:{name}")
    return matches[0]


def _validate_common_source(source: str) -> None:
    tree = _tree(source)
    if any(module == "langgraph" or module.startswith("langgraph.") for module in _imports(tree)):
        raise GateViolation("legacy:langgraph-import")
    found = _symbols(tree) & _LEGACY_AGENT_SYMBOLS
    if found:
        raise GateViolation(f"legacy:symbol:{sorted(found)[0]}")
    lowered = {name.lower() for name in _symbols(tree)}
    for fragment in sorted(_OLD_PATH_SWITCH_FRAGMENTS):
        if any(fragment in name for name in lowered):
            raise GateViolation(f"cutover-switch:{fragment}")


def _validate_driver_protocol(source: str) -> None:
    tree = _tree(source)
    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentDriver"
    ]
    if len(classes) != 1:
        raise GateViolation("driver:missing")
    methods = {
        node.name
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    if methods != {"execute"}:
        raise GateViolation(f"driver:methods:{sorted(methods)}")


def _validate_agent_loop_boundary(source: str) -> None:
    tree = _tree(source)
    for module in sorted(_imports(tree)):
        if any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in _LOOP_FORBIDDEN_IMPORT_PREFIXES
        ):
            raise GateViolation(f"loop:forbidden-import:{module}")
    if "LegacyDeterministic" in _symbols(tree):
        raise GateViolation("loop:deterministic-adapter")
    if _calls_named(tree, "BoundProviderResponse"):
        raise GateViolation("provenance:direct-bound-construction")

    emit = _function(tree, "_emit")
    if len(emit.args.args) < 2:
        raise GateViolation("event-sink:open")
    annotation = emit.args.args[-1].annotation
    if not isinstance(annotation, ast.Name) or annotation.id != "AgentLoopEvent":
        raise GateViolation("event-sink:open")
    for node in ast.walk(emit):
        if isinstance(node, ast.Dict):
            raise GateViolation("event-sink:legacy-mapping")


def _validate_runtime_driver_entry(source: str) -> None:
    tree = _tree(source)
    method = _function(tree, "_run_driver")
    names = _symbols(method)
    if names & {"_callable", "_invoke", "getattr", "hasattr"}:
        raise GateViolation("runtime:reflective-driver-entry")
    execute_calls = _calls_named(method, "execute")
    if len(execute_calls) != 1:
        raise GateViolation("runtime:execute-count")


def _validate_agent_event_adapter(source: str) -> None:
    tree = _tree(source)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_AgentEventAdapter"
    ]
    if len(classes) != 1:
        raise GateViolation("event-adapter:missing")
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == "emit"
    ]
    if len(methods) != 1 or len(methods[0].args.args) < 2:
        raise GateViolation("event-adapter:open")
    annotation = methods[0].args.args[-1].annotation
    if not isinstance(annotation, ast.Name) or annotation.id != "AgentLoopEvent":
        raise GateViolation("event-adapter:open")


def _validate_bound_response_owner(path: Path, source: str) -> None:
    constructors = _calls_named(_tree(source), "BoundProviderResponse")
    if constructors and path != SRC / "context_projector" / "gateway.py":
        raise GateViolation(f"provenance:constructor-owner:{path.name}")


def test_production_cutover_and_boundary_gates() -> None:
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        _validate_common_source(source)
        _validate_bound_response_owner(path, source)

    contracts = (AI / "agent_contracts.py").read_text(encoding="utf-8")
    loop = (AI / "agent_loop.py").read_text(encoding="utf-8")
    runtime = (SRC / "pilot_runtime" / "service.py").read_text(encoding="utf-8")
    composition = (SRC / "pilot_runtime" / "composition.py").read_text(encoding="utf-8")
    _validate_driver_protocol(contracts)
    _validate_agent_loop_boundary(loop)
    _validate_runtime_driver_entry(runtime)
    _validate_agent_event_adapter(composition)


def test_agent_loop_dependency_and_composition_cutover_are_closed() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert not any(str(item).split("[", 1)[0].startswith("langgraph") for item in dependencies)

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "langgraph"' not in lock
    assert 'name = "langgraph-checkpoint-sqlite"' not in lock

    api = (SRC / "api.py").read_text(encoding="utf-8")
    composition = (SRC / "pilot_runtime" / "composition.py").read_text(encoding="utf-8")
    assert "build_pilot_runtime(" in api
    assert composition.count("agent_driver=cast(Any, driver)") == 1
    for forbidden in (
        "resume_after_confirm_fn",
        "run_turn_fn",
        "_runtime_resume_after_confirm",
        "_agent_checkpoint_path",
    ):
        assert forbidden not in api


@pytest.mark.parametrize(
    ("validator", "source", "code"),
    [
        (_validate_common_source, "import langgraph\n", "legacy:langgraph-import"),
        (_validate_common_source, "StateGraph = object()\n", "legacy:symbol:StateGraph"),
        (
            _validate_common_source,
            "shadow_loop_enabled = True\n",
            "cutover-switch:shadow_loop",
        ),
        (
            _validate_driver_protocol,
            "class AgentDriver(Protocol):\n"
            "    def execute(self, invocation): ...\n"
            "    def run_turn(self, messages): ...\n",
            "driver:methods",
        ),
        (
            _validate_agent_loop_boundary,
            "from fastapi import Request\n"
            "def _emit(invocation, event: AgentLoopEvent): pass\n",
            "loop:forbidden-import:fastapi",
        ),
        (
            _validate_agent_loop_boundary,
            "def _emit(invocation, event: object): pass\n",
            "event-sink:open",
        ),
        (
            _validate_agent_loop_boundary,
            "def _emit(invocation, event: AgentLoopEvent):\n"
            "    sink.emit({'type': 'assistant_delta'})\n",
            "event-sink:legacy-mapping",
        ),
        (
            _validate_agent_loop_boundary,
            "def _emit(invocation, event: AgentLoopEvent): pass\n"
            "response = BoundProviderResponse(None, '', '', 0, '')\n",
            "provenance:direct-bound-construction",
        ),
        (
            _validate_runtime_driver_entry,
            "def _run_driver(driver, invocation):\n"
            "    execute = getattr(driver, 'execute')\n"
            "    return execute(invocation)\n",
            "runtime:reflective-driver-entry",
        ),
        (
            _validate_runtime_driver_entry,
            "def _run_driver(driver, invocation):\n"
            "    return driver.run_turn(invocation)\n",
            "runtime:execute-count",
        ),
        (
            _validate_agent_event_adapter,
            "class _AgentEventAdapter:\n"
            "    def emit(self, event: object): pass\n",
            "event-adapter:open",
        ),
    ],
)
def test_negative_fixtures_prove_each_gate_rejects(
    validator: object,
    source: str,
    code: str,
) -> None:
    with pytest.raises(GateViolation, match=code):
        validator(source)  # type: ignore[operator]


def test_negative_fixture_proves_bound_response_owner_is_enforced() -> None:
    source = "response = BoundProviderResponse(None, '', '', 0, '')\n"
    with pytest.raises(GateViolation, match="provenance:constructor-owner"):
        _validate_bound_response_owner(AI / "agent_loop.py", source)
