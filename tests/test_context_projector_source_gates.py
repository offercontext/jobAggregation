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
    assert isinstance(body[0], ast.Expr) and body[0].value is validation, (
        f"{node.name} validation must be the first unconditional operation"
    )
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
        keyword.value for keyword in delegates[0].keywords if keyword.arg == "invocation_identity"
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
    forwarded = statement.value if isinstance(statement, (ast.Expr, ast.Return)) else None
    assert forwarded is delegates[0], f"{node.name} Gateway delegate must be the unconditional body"


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


def _class_fields(path: Path, class_name: str) -> dict[str, ast.expr | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        statement.target.id: statement.annotation
        for statement in definition.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }


def _annotation_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    return {
        item.id if isinstance(item, ast.Name) else item.attr
        for item in ast.walk(node)
        if isinstance(item, (ast.Name, ast.Attribute))
    }


def test_selector_consumes_only_same_bundle_discovery_and_authority_views() -> None:
    path = ROOT / "context_projector" / "selector.py"
    source = path.read_text(encoding="utf-8")
    function = _functions(path)[("", "select_tools")]
    positional = [argument.arg for argument in function.args.args]

    assert positional == ["discovery_view", "authority_view", "trusted_signals"]
    assert not function.args.kwonlyargs
    assert _annotation_name(function.returns) == "ToolSelectionResult"
    assert {
        "ToolDiscoveryMetadataView",
        "ToolAuthorityMetadataView",
        "ToolSelectionSignals",
    }.issubset(
        {name for argument in function.args.args for name in _annotation_names(argument.annotation)}
    )
    for forbidden in (
        "MODEL_TOOL_NAMES",
        "MODEL_TOOL_CATALOG",
        "DEPENDENCY_POLICY_V1",
        "require_dependency_policy_v1",
        "_DOMAIN_TOOLS",
        "_DEPENDENCIES",
        "_LEXICAL_RULES",
        "_PAGE_DOMAINS",
        "_ATTACHMENT_DOMAINS",
    ):
        assert forbidden not in source
    view_validator = _functions(path)[("", "_require_same_bundle_views")]
    attributes = {
        node.attr
        for owner in (function, view_validator)
        for node in ast.walk(owner)
        if isinstance(node, ast.Attribute)
    }
    assert {
        "bundle_instance_token",
        "ordered_entries",
        "policy",
        "entries",
    }.issubset(attributes)
    calls = [_terminal(call.func) for call in ast.walk(function) if isinstance(call, ast.Call)]
    assert calls.count("_issue_tool_selection_result") == 1
    issuer = _functions(path)[("", "_issue_tool_selection_result")]
    issuer_calls = [_terminal(call.func) for call in ast.walk(issuer) if isinstance(call, ast.Call)]
    assert issuer_calls.count("materialize_provider_payloads") == 1
    assert issuer_calls.count("sha256_hex") == 1


def test_segment_gate_and_projector_consume_tool_selection_result_directly() -> None:
    agent_loop = ROOT / "ai" / "agent_loop.py"
    projector = ROOT / "context_projector" / "projector.py"
    gate_fields = _class_fields(agent_loop, "SegmentSurfaceGate")
    request_fields = _class_fields(projector, "ProjectionRequest")

    assert _annotation_names(gate_fields["selection"]) == {"ToolSelectionResult"}
    assert _annotation_names(request_fields["selection"]) == {"ToolSelectionResult"}
    assert {
        "provider_tools",
        "authority_surface",
        "preselected_tools",
    }.isdisjoint(request_fields)

    project_surface = _functions(agent_loop)[("_LoopServices", "project_model_surface")]
    assert [argument.arg for argument in project_surface.args.args] == ["self", "messages"]
    request_calls = [
        call
        for call in ast.walk(project_surface)
        if isinstance(call, ast.Call) and _terminal(call.func) == "ProjectionRequest"
    ]
    assert len(request_calls) == 1
    request_keywords = {keyword.arg for keyword in request_calls[0].keywords}
    assert "selection" in request_keywords
    assert {
        "provider_tools",
        "authority_surface",
        "preselected_tools",
    }.isdisjoint(request_keywords)

    project = _functions(projector)[("ModelSurfaceProjector", "project")]
    project_calls = {
        _terminal(call.func) for call in ast.walk(project) if isinstance(call, ast.Call)
    }
    assert not {
        "select_tools",
        "intersect_authority_surface",
        "provider_contracts",
        "resolve",
    }.intersection(project_calls)
    project_attributes = {
        node.attr for node in ast.walk(project) if isinstance(node, ast.Attribute)
    }
    assert {
        "provider_contracts",
        "provider_envelope_fingerprint",
        "selected_names",
    }.issubset(project_attributes)
    assert not {
        "tools",
        "names",
        "envelope_fingerprint",
        "fallback_all",
        "domains",
    }.intersection(project_attributes)

    projector_source = projector.read_text(encoding="utf-8")
    for forbidden in (
        "MODEL_TOOL_CATALOG",
        "ToolCatalog",
        "DEPENDENCY_POLICY_V1",
        "DependencyPolicyV1",
    ):
        assert forbidden not in projector_source


def test_authority_intersection_delegates_final_selection_issuance_to_selector() -> None:
    authority_path = ROOT / "context_projector" / "authority_surface.py"
    selector_path = ROOT / "context_projector" / "selector.py"
    function = _functions(authority_path)[("", "intersect_authority_surface")]
    calls = [item for item in ast.walk(function) if isinstance(item, ast.Call)]
    call_names = [_terminal(call.func) for call in calls]

    assert ("", "_issue_tool_selection_result") in _functions(selector_path)
    assert call_names.count("_issue_tool_selection_result") == 1
    assert not {
        "materialize_provider_payloads",
        "canonical_json",
        "sha256_hex",
    }.intersection(call_names)
    assert not any(
        _terminal(call.func) == "dict"
        and call.args
        and isinstance(call.args[0], ast.Call)
        and _terminal(call.args[0].func) == "zip"
        for call in calls
    ), "Authority must filter aligned contracts, not rebuild them through a name mapping"


def test_agent_loop_surface_capture_passes_validated_bundle_provider_view() -> None:
    path = ROOT / "ai" / "agent_loop.py"
    function = _functions(path)[("_LoopServices", "capture_model_input")]
    capture_calls = [
        call
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and _terminal(call.func) == "capture_surface"
    ]

    assert len(capture_calls) == 1
    provider_keywords = [
        keyword.value for keyword in capture_calls[0].keywords if keyword.arg == "provider_view"
    ]
    assert len(provider_keywords) == 1
    provider_value = provider_keywords[0]
    assert isinstance(provider_value, ast.Attribute) and provider_value.attr == "provider_view"
    if isinstance(provider_value.value, ast.Name):
        gate_name = provider_value.value.id
        validated_gate_assignments = [
            statement
            for statement in ast.walk(function)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and _terminal(statement.value.func) == "_require_surface_gate"
            and (
                (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == gate_name
                        for target in statement.targets
                    )
                )
                or (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == gate_name
                )
            )
        ]
        assert len(validated_gate_assignments) == 1
    else:
        assert isinstance(provider_value.value, ast.Call)
        assert _terminal(provider_value.value.func) == "_require_surface_gate"


def test_surface_selection_matcher_never_rebuilds_names_or_fingerprint() -> None:
    path = ROOT / "ai" / "agent_loop.py"
    function = _functions(path)[("", "_surface_selection_matches")]
    arguments = function.args.args

    assert [argument.arg for argument in arguments] == ["left", "right"]
    assert all(
        _annotation_name(argument.annotation) == "ToolSelectionResult" for argument in arguments
    )
    calls = {_terminal(call.func) for call in ast.walk(function) if isinstance(call, ast.Call)}
    assert not {
        "materialize_provider_payloads",
        "canonical_json",
        "sha256_hex",
        "provider_contracts",
        "resolve",
    }.intersection(calls)
    attributes = {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)}
    assert "selected_names" in attributes
    assert "provider_envelope_fingerprint" in attributes
    assert not {"names", "envelope_fingerprint", "tools"}.intersection(attributes)


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
            if isinstance(node, ast.Import) and any(
                alias.name == "litellm" for alias in node.names
            ):
                imports.append(path.relative_to(ROOT.parent).as_posix())
            if isinstance(node, ast.ImportFrom) and node.module == "litellm":
                imports.append(path.relative_to(ROOT.parent).as_posix())
    assert Counter(imports) == Counter(
        {"offerpilot/ai/client.py": 1, "offerpilot/knowledge/provider.py": 1}
    )
    cli = (ROOT / "cli.py").read_text(encoding="utf-8")
    cli_tree = ast.parse(cli)
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "litellm" for node in ast.walk(cli_tree)
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
