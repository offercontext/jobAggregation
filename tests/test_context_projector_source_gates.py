from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from offerpilot.ai.provider_boundaries import (
    NON_AGENT_PROVIDER_CALL_MANIFEST,
    RAW_PROVIDER_BOUNDARIES,
)

ROOT = Path(__file__).resolve().parents[1] / "src" / "offerpilot"


def _annotation_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _require_provider_invocation_parameter(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    arguments = (*node.args.args, *node.args.kwonlyargs)
    matches = [item for item in arguments if item.arg == "invocation_identity"]
    assert len(matches) == 1, f"{node.name} must require invocation_identity"
    assert _annotation_name(matches[0].annotation) == "ProviderInvocationIdentity", node.name
    assert matches[0] in node.args.kwonlyargs, (
        f"{node.name} invocation_identity must be keyword-only"
    )
    assert not {
        "authority",
        "build_identity",
        "model_call_surface_binding",
        "provider_surface_build_identity",
    }.intersection(item.arg for item in arguments), (
        f"{node.name} accepts alternate provider provenance"
    )


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _require_gateway_runtime_validation(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
    validations = [item for item in calls if _terminal(item.func) == "_validate_invocation"]
    assert len(validations) == 1, f"{node.name} must validate invocation exactly once"
    validation = validations[0]
    assert len(validation.args) == 2
    assert all(isinstance(item, ast.Name) for item in validation.args)
    assert [item.id for item in validation.args if isinstance(item, ast.Name)] == [
        "surface",
        "invocation_identity",
    ]
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert body, f"{node.name} provider path is missing"
    assert (
        isinstance(body[0], ast.Expr)
        and body[0].value is validation
    ), f"{node.name} validation must be the first unconditional operation"
    network_calls = [
        item
        for item in calls
        if _terminal(item.func) in {"complete_one", "stream_one", "_preflight"}
    ]
    assert network_calls, f"{node.name} provider path is missing"
    assert validation.lineno < min(item.lineno for item in network_calls), (
        f"{node.name} validates after Provider access"
    )


def _require_client_identity_forwarding(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
    delegates = [
        item
        for item in calls
        if _terminal(item.func) in {"preflight", "complete", "stream_deferred"}
    ]
    assert len(delegates) == 1, f"{node.name} must have one Gateway delegate"
    identity = [
        keyword.value
        for keyword in delegates[0].keywords
        if keyword.arg == "invocation_identity"
    ]
    assert len(identity) == 1
    assert isinstance(identity[0], ast.Name) and identity[0].id == "invocation_identity"
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert len(body) == 1
    statement = body[0]
    forwarded = (
        statement.value
        if isinstance(statement, (ast.Expr, ast.Return))
        else None
    )
    assert forwarded is delegates[0], (
        f"{node.name} Gateway delegate must be the unconditional body"
    )


def _functions(path: Path) -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    class_name = ""

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            nonlocal class_name
            previous, class_name = class_name, node.name
            self.generic_visit(node)
            class_name = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            result[(class_name, node.name)] = node
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return result


def test_fixed_non_agent_manifest_has_13_functions_and_18_calls() -> None:
    assert len(NON_AGENT_PROVIDER_CALL_MANIFEST) == 13
    assert sum(count for _, _, count in NON_AGENT_PROVIDER_CALL_MANIFEST) == 18
    for relative, function_name, expected_calls in NON_AGENT_PROVIDER_CALL_MANIFEST:
        node = _functions(ROOT.parent / relative)[("", function_name)]
        actual = sum(
            isinstance(call.func, ast.Attribute)
            and call.func.attr in {"complete", "stream_complete"}
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
        assert actual == expected_calls, (relative, function_name)


def test_raw_provider_boundary_manifest_resolves_exactly_five_functions() -> None:
    assert len(RAW_PROVIDER_BOUNDARIES) == 5
    for relative, class_name, function_name in RAW_PROVIDER_BOUNDARIES:
        assert (class_name, function_name) in _functions(ROOT.parent / relative)


def test_litellm_imports_are_confined_and_cli_uses_knowledge_factory() -> None:
    imports: list[str] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "litellm" for alias in node.names):
                imports.append(path.relative_to(ROOT.parent).as_posix())
            if isinstance(node, ast.ImportFrom) and node.module == "litellm":
                imports.append(path.relative_to(ROOT.parent).as_posix())
    assert Counter(imports) == Counter(
        {"offerpilot/ai/client.py": 1, "offerpilot/knowledge/provider.py": 1}
    )
    cli = (ROOT / "cli.py").read_text(encoding="utf-8")
    cli_tree = ast.parse(cli)
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "litellm"
        for node in ast.walk(cli_tree)
    )
    assert "build_knowledge_brief_provider_client" in cli


def test_provider_boundary_functions_have_no_dynamic_import_or_generic_http_call() -> None:
    forbidden_call_names = {"__import__", "import_module"}
    for relative, class_name, function_name in RAW_PROVIDER_BOUNDARIES:
        node = _functions(ROOT.parent / relative)[(class_name, function_name)]
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            if isinstance(call.func, ast.Name):
                assert call.func.id not in forbidden_call_names
            elif isinstance(call.func, ast.Attribute):
                assert call.func.attr not in forbidden_call_names
                if isinstance(call.func.value, ast.Name) and call.func.value.id in {
                    "httpx",
                    "requests",
                    "aiohttp",
                }:
                    assert call.func.attr not in {"request", "post", "get"}


def test_agent_provider_adapters_require_the_exact_invocation_identity_contract() -> None:
    gateway = _functions(ROOT / "context_projector" / "gateway.py")
    client = _functions(ROOT / "ai" / "client.py")
    for class_name, function_name in (
        ("AgentProviderGatewaySession", "preflight"),
        ("AgentProviderGatewaySession", "complete"),
        ("AgentProviderGatewaySession", "stream"),
        ("AgentProviderGatewaySession", "stream_deferred"),
    ):
        node = gateway[(class_name, function_name)]
        _require_provider_invocation_parameter(node)
        _require_gateway_runtime_validation(node)
    for function_name in (
        "preflight_agent_surface",
        "complete_agent_surface",
        "stream_agent_surface",
    ):
        node = client[("ConfiguredAIClient", function_name)]
        _require_provider_invocation_parameter(node)
        _require_client_identity_forwarding(node)


def test_provider_identity_parameter_gate_rejects_missing_or_broad_annotations() -> None:
    for source in (
        "def complete(surface): pass\n",
        "def complete(surface, *, invocation_identity: object): pass\n",
        "def complete(surface, invocation_identity: ProviderInvocationIdentity): pass\n",
        "def complete(surface, build_identity, *, "
        "invocation_identity: ProviderInvocationIdentity): pass\n",
    ):
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef)
        with pytest.raises(AssertionError):
            _require_provider_invocation_parameter(function)


def test_provider_runtime_validation_gate_rejects_late_or_missing_identity_checks() -> None:
    sources = (
        "def complete(self, surface, *, invocation_identity):\n"
        "    return self.complete_one(surface)\n",
        "def complete(self, surface, *, invocation_identity):\n"
        "    value = self.complete_one(surface)\n"
        "    self._validate_invocation(surface, invocation_identity)\n"
        "    return value\n",
        "def complete(self, surface, *, invocation_identity):\n"
        "    if False:\n"
        "        self._validate_invocation(surface, invocation_identity)\n"
        "    return self.complete_one(surface)\n",
    )
    for source in sources:
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef)
        with pytest.raises(AssertionError):
            _require_gateway_runtime_validation(function)


def test_provider_client_forwarding_gate_rejects_alternate_identity_value() -> None:
    function = ast.parse(
        "def complete_agent_surface(self, surface, *, invocation_identity):\n"
        "    return self.gateway.complete(surface, invocation_identity=request)\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)
    with pytest.raises(AssertionError):
        _require_client_identity_forwarding(function)


def test_provider_client_forwarding_gate_rejects_dead_delegate() -> None:
    function = ast.parse(
        "def complete_agent_surface(self, surface, *, invocation_identity):\n"
        "    if False:\n"
        "        return self.gateway.complete(\n"
        "            surface, invocation_identity=invocation_identity)\n"
        "    return self.raw.complete(surface)\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)
    with pytest.raises(AssertionError):
        _require_client_identity_forwarding(function)
