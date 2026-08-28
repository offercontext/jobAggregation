from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from offerpilot.ai.tool_specs.common import resolve_identity_argument, resolve_parent_application


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "offerpilot"

_FORBIDDEN_SYMBOLS = frozenset(
    {
        "MODEL_TOOL_NAMES",
        "MODEL_TOOL_CATALOG",
        "LEGACY_DETERMINISTIC_NAMES",
        "TRANSACTIONAL_TYPED_WRITE_NAMES",
        "REQUIRED_UNDO_TOOL_NAMES",
        "TYPED_WRITE_OPERATION_NAMES",
        "LEGACY_WRITE_OPERATION_NAMES",
        "COMPENSATION_OPERATION_NAMES",
        "REQUIRED_UNDO_OPERATION_NAMES",
        "WRITE_OPERATION_NAMES",
        "DEPENDENCY_POLICY_V1",
        "BindingResolverSpec",
        "BindingResolver",
        "BindingTarget",
        "UNAVAILABLE",
        "_UnavailableBindingTarget",
        "aggregate_binding",
        "require_capabilities",
        "pre_resolver_scope_policy",
        "audit_bindings",
        "evaluate_context",
        "_pending_adapter_kind",
        "_chained_adapter_kind",
        "_with_write_contract",
        "_with_runtime_metadata",
        "editable_fields_for_tool",
        "_undo_seed_for_pending",
        "_build_write_undo",
        "_CREATED_RECORD_FINGERPRINT_FIELDS",
        "legacy_catalog_factory",
        "build_legacy_deterministic_catalog",
        "_legacy_catalog",
        "_legacy_adapter",
    }
)
_CLASSIFICATION_FIELDS = frozenset(
    {
        "adapter_kind",
        "binding",
        "confirmation_policy",
        "operation",
        "required_capabilities",
        "undo_policy",
    }
)
_LEGACY_NAMES = frozenset(
    {
        "save_application_jd_version",
        "create_application_submission_snapshot",
        "record_application_outcome",
    }
)
_TYPED_NAMES = frozenset(
    item["name"]
    for item in json.loads(
        (ROOT / "tests" / "fixtures" / "tool_authority" / "authority_manifest_v1.json").read_text(
            encoding="utf-8"
        )
    )["tools"]
)
_COMPENSATION_NAMES = frozenset(
    {
        "undo:create_application",
        "undo:update_application_status",
        "undo:create_application_event",
        "undo:add_note",
    }
)
_KNOWN_OPERATION_NAMES = _TYPED_NAMES | _LEGACY_NAMES | _COMPENSATION_NAMES
_CLASSIFICATION_LITERAL_ALLOWLIST = frozenset(
    {
        SRC / "ai" / "tool_specs" / "legacy.py",
        SRC / "ai" / "tool_runtime" / "policy_types.py",
        SRC / "ai" / "tool_runtime" / "protocol_seals.py",
    }
)
_LEGACY_PROOF_FORBIDDEN_IMPORTS = (
    "offerpilot.agent_runtime.journal",
    "offerpilot.agent_runtime.keyring",
    "offerpilot.ai.write_operations",
    "offerpilot.models",
    "offerpilot.pilot_runtime",
    "offerpilot.repositories",
)
_FINAL_COMPONENT_FACTORIES = frozenset(
    {
        "build_unpublished_legacy_initial_route_components",
        "build_unpublished_legacy_confirmation_components",
    }
)
_EXECUTION_CALLS = frozenset(
    {
        "execute_prepared",
        "execute_legacy",
        "resolve_server_loaded",
    }
)
_FEATURE_ROLE_FRAGMENTS = (
    "dual_registry",
    "fallback_catalog",
    "metadata_fallback",
    "shadow_execution",
    "shadow_tool",
    "tool_metadata_enabled",
    "tool_legacy_fallback",
    "typed_to_legacy",
    "use_global_catalog",
)


class DeletionGateViolation(AssertionError):
    pass


def _production_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in sorted(SRC.rglob("*.py"))}


def _string_value(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_value(node.left, bindings)
        right = _string_value(node.right, bindings)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        separator = _string_value(node.func.value, bindings)
        parts = [_string_value(item, bindings) for item in node.args[0].elts]
        if separator is not None and all(part is not None for part in parts):
            return separator.join(part for part in parts if part is not None)
    return None


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        state = tuple(sorted(result.items()))
        if state in seen:
            return result
        seen.add(state)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            value = _string_value(node.value, result)
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and result.get(target.id) != value:
                    result[target.id] = value
                    changed = True
        if not changed:
            return result


def _terminal_name(node: ast.AST, strings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
    ):
        return _string_value(node.args[1], strings)
    return None


def _call_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    strings = _string_bindings(tree)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
    seen: set[tuple[tuple[str, str], ...]] = set()
    while True:
        state = tuple(sorted(aliases.items()))
        if state in seen:
            return aliases, strings
        seen.add(state)
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            source = _terminal_name(node.value, strings)
            if source is None:
                continue
            source = aliases.get(source, source)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != source:
                    aliases[target.id] = source
                    changed = True
        if not changed:
            return aliases, strings


def _call_name(
    call: ast.Call,
    aliases: dict[str, str],
    strings: dict[str, str],
) -> str | None:
    name = _terminal_name(call.func, strings)
    return None if name is None else aliases.get(name, name)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _symbols(tree: ast.AST) -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            found.add((node.attr, node.lineno))
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add((node.name, node.lineno))
        elif isinstance(node, ast.alias):
            found.add((node.name.rsplit(".", 1)[-1], node.lineno))
    return found


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _exact_symbol_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    violations = [
        f"{_display_path(path)}:{line}:{symbol}"
        for symbol, line in sorted(_symbols(tree))
        if symbol in _FORBIDDEN_SYMBOLS
    ]
    aliases, strings = _call_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        reflected = _call_name(node, aliases, strings)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
        ):
            reflected = _string_value(node.args[1], strings)
        if reflected in _FORBIDDEN_SYMBOLS:
            violations.append(f"{_display_path(path)}:{node.lineno}:reflected:{reflected}")
    return violations


def _literal_strings(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _classification_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    parents = _parents(tree)
    aliases, strings = _call_aliases(tree)
    violations: list[str] = []

    reflection_helpers: set[str] = set()
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        parameter_names = {
            item.arg
            for item in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
        }
        for call in (item for item in ast.walk(function) if isinstance(item, ast.Call)):
            direct_reflection = (
                isinstance(call.func, ast.Name)
                and call.func.id in {"getattr", "hasattr"}
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Name)
                and call.args[1].id in parameter_names
            )
            mapping_reflection = (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id in parameter_names
            )
            if direct_reflection or mapping_reflection:
                reflection_helpers.add(function.name)
                break

    def smoke_assertion_is_allowed(node: ast.AST) -> bool:
        if path != SRC / "smoke.py" or not isinstance(node, ast.Compare):
            return False
        owner: ast.AST | None = node
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        expected_by_owner = {
            "_assert_create_application_card": "create_application",
            "_assert_create_event_card": "create_application_event",
            "_run_application_outcome_http_smoke": "record_application_outcome",
            "_run_real_ai_write_smoke": "update_application_status",
        }
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        expected = expected_by_owner.get(owner.name)
        if (
            expected is None
            or len(node.ops) != 1
            or not isinstance(node.ops[0], ast.NotEq)
            or len(node.comparators) != 1
            or not isinstance(node.comparators[0], ast.Constant)
            or node.comparators[0].value != expected
            or not isinstance(node.left, ast.Call)
            or not isinstance(node.left.func, ast.Attribute)
            or node.left.func.attr != "get"
            or not node.left.args
            or not isinstance(node.left.args[0], ast.Constant)
            or node.left.args[0].value != "tool_name"
        ):
            return False
        assertion = parents.get(node)
        if not isinstance(assertion, ast.If) or assertion.test is not node:
            return False
        return (
            len(assertion.body) == 1
            and isinstance(assertion.body[0], ast.Raise)
            and isinstance(assertion.body[0].exc, ast.Call)
            and isinstance(assertion.body[0].exc.func, ast.Name)
            and assertion.body[0].exc.func.id == "RuntimeError"
        )

    def literal_is_allowed(node: ast.AST) -> bool:
        smoke_owner: ast.AST | None = node
        while smoke_owner is not None:
            if smoke_assertion_is_allowed(smoke_owner):
                return True
            smoke_owner = parents.get(smoke_owner)
        if path in _CLASSIFICATION_LITERAL_ALLOWLIST:
            return True
        if path.parent == SRC / "ai" / "tool_specs":
            current: ast.AST | None = node
            while current is not None:
                if isinstance(current, ast.Call):
                    call_name = _call_name(current, aliases, strings)
                    if call_name in {
                        "LegacyDeterministicAdapterSpec",
                        "build_tool_spec",
                        "provider_contract",
                    }:
                        return True
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if current.name in {
                        "_build_add_note_undo",
                        "_build_create_application_event_undo",
                        "_build_update_application_status_undo",
                    }:
                        dictionary = parents.get(node)
                        if isinstance(dictionary, ast.Dict):
                            for key, value in zip(dictionary.keys, dictionary.values):
                                if (
                                    value is node
                                    and isinstance(key, ast.Constant)
                                    and key.value == "kind"
                                ):
                                    return True
                    break
                current = parents.get(current)
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.Assign, ast.AnnAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                names = {
                    item.id
                    for target in targets
                    for item in ast.walk(target)
                    if isinstance(item, ast.Name)
                }
                module_level = isinstance(parents.get(current), ast.Module)
                if (
                    path == SRC / "agent_runtime" / "events.py"
                    and names == {"_TOOL_NAMES"}
                    and module_level
                ):
                    return True
                if (
                    path
                    in {
                        SRC / "pilot_runtime" / "service.py",
                        SRC / "pilot_runtime" / "persistence.py",
                    }
                    and names == {"_USER_FACING_TOOL_NAMES"}
                    and module_level
                ):
                    return True
                return False
            current = parents.get(current)
        return False

    def classified_names(node: ast.AST) -> set[str]:
        return {
            item.value
            for item in ast.walk(node)
            if isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            and item.value in _KNOWN_OPERATION_NAMES
            and not literal_is_allowed(item)
        }

    for node in ast.walk(tree):
        if isinstance(node, (ast.Dict, ast.DictComp, ast.List, ast.Set, ast.Tuple)):
            names = classified_names(node)
            if len(names) >= 2:
                violations.append(f"name-set:{node.lineno}:{sorted(names)[:3]}")
        if isinstance(node, (ast.Compare, ast.MatchValue, ast.MatchMapping)):
            names = classified_names(node)
            if names:
                violations.append(f"name-switch:{node.lineno}:{sorted(names)[0]}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
        ):
            prefixes = _literal_strings(node.args[0])
            receiver = ast.unparse(node.func.value).casefold()
            if prefixes & {"legacy", "typed", "undo:", "create_", "update_"} and any(
                marker in receiver for marker in ("adapter", "operation", "tool", "name")
            ):
                violations.append(f"string-prefix-routing:{node.lineno}")
        if (
            isinstance(node, ast.Call)
            and _call_name(node, aliases, strings) in {"getattr", "hasattr", *reflection_helpers}
            and len(node.args) >= 2
            and _string_value(node.args[1], strings) in _CLASSIFICATION_FIELDS
        ):
            violations.append(f"reflective-classification:{node.lineno}")
    return violations


def _construction_and_factory_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    aliases, strings = _call_aliases(tree)
    parents = _parents(tree)
    violations: list[str] = []
    composition = SRC / "pilot_runtime" / "composition.py"
    catalog_module = SRC / "ai" / "tool_specs" / "catalog.py"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node, aliases, strings)
        owner = _enclosing_function(node, parents)
        owner_name = None if owner is None else owner.name
        if name == "ToolMetadataBundleV1" and not (
            path == composition and owner_name == "build_production_tool_metadata_components"
        ):
            violations.append(f"bundle-constructor-owner:{node.lineno}")
        elif name == "ToolCatalog" and not (
            path == catalog_module and owner_name == "build_model_tool_catalog"
        ):
            violations.append(f"second-typed-catalog:{node.lineno}")
        elif name == "build_model_tool_catalog" and not (
            path == composition and owner_name == "build_production_tool_metadata_components"
        ):
            violations.append(f"typed-catalog-factory-owner:{node.lineno}")
        elif name in _FINAL_COMPONENT_FACTORIES and not (
            path == composition and owner_name == "build_production_tool_metadata_components"
        ):
            violations.append(f"legacy-component-factory-owner:{name}:{node.lineno}")
    return violations


def _provider_registry_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    catalog_owner = SRC / "ai" / "tool_runtime" / "catalog.py"
    metadata_owner = SRC / "ai" / "tool_runtime" / "metadata.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if {"resolve", "provider_contracts"} <= methods and not (
                path == catalog_owner and node.name == "ToolCatalog"
            ):
                violations.append(f"second-provider-registry:{node.name}:{node.lineno}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "provider_contracts" and path not in {
                catalog_owner,
                metadata_owner,
            }:
                violations.append(f"raw-provider-catalog-query:{node.lineno}")
            if (
                node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and (
                    any(
                        marker in node.func.value.id.casefold()
                        for marker in ("provider_contract", "provider_tool")
                    )
                    or (
                        "provider" in node.func.value.id.casefold()
                        and any(
                            marker in node.func.value.id.casefold()
                            for marker in ("catalog", "registry")
                        )
                    )
                )
            ):
                violations.append(f"provider-registry-fallback:{node.lineno}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if (
                node.func.id == "dict"
                and node.args
                and isinstance(node.args[0], ast.Attribute)
                and node.args[0].attr == "payload"
            ):
                violations.append(f"provider-payload-dict:{node.lineno}")
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = {
                item.id
                for target in targets
                for item in ast.walk(target)
                if isinstance(item, ast.Name)
            }
            if any(
                "provider" in name.casefold()
                and any(role in name.casefold() for role in ("dict", "map", "registry"))
                for name in target_names
            ) or any(
                marker in ast.unparse(node.value).casefold()
                for marker in ("provider_contract", "provider_tool")
            ):
                if not isinstance(node.value, (ast.Dict, ast.DictComp)):
                    continue
                violations.append(f"provider-dict-registry:{node.lineno}")
        if isinstance(node, ast.DictComp) and node.generators:
            generator = node.generators[0]
            iterator = ast.unparse(generator.iter).casefold()
            target_names = {
                item.id for item in ast.walk(generator.target) if isinstance(item, ast.Name)
            }
            key_owner = None
            if (
                isinstance(node.key, ast.Attribute)
                and node.key.attr == "name"
                and isinstance(node.key.value, ast.Name)
            ):
                key_owner = node.key.value.id
            value_is_target = isinstance(node.value, ast.Name) and node.value.id in target_names
            if (
                key_owner in target_names
                and value_is_target
                and any(marker in iterator for marker in ("contract", "provider", "tool"))
            ):
                violations.append(f"provider-dict-registry:{node.lineno}")
    return violations


def _fallback_and_shadow_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    lowered_symbols = {symbol.casefold() for symbol, _line in _symbols(tree)}
    violations = [
        f"feature-or-shadow-role:{fragment}"
        for fragment in _FEATURE_ROLE_FRAGMENTS
        if any(fragment in symbol for symbol in lowered_symbols)
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            fallback_roles: set[str] = set()
            for value in node.values:
                rendered = ast.unparse(value).casefold()
                if not any(marker in rendered for marker in (".get(", ".resolve(", "execute")):
                    continue
                if "legacy" in rendered:
                    fallback_roles.add("legacy")
                if any(marker in rendered for marker in ("typed", "provider", "catalog")):
                    fallback_roles.add("current")
            if fallback_roles == {"legacy", "current"}:
                violations.append(f"typed-to-legacy-fallback:{node.lineno}")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for statement_list in (
            node.body,
            *(
                child.body
                for child in ast.walk(node)
                if isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.With))
            ),
        ):
            called = {
                call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
                for statement in statement_list
                for call in ast.walk(statement)
                if isinstance(call, ast.Call) and isinstance(call.func, (ast.Attribute, ast.Name))
            }
            if len(called & _EXECUTION_CALLS) >= 2:
                violations.append(f"double-or-fallback-execution:{node.name}:{node.lineno}")
                break
    dispatcher_paths = {
        SRC / "ai" / "agent_loop.py",
        SRC / "ai" / "client.py",
    }
    if path in dispatcher_paths:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "offerpilot.ai.tool_runtime.legacy"
                or any(item.name.startswith("Legacy") for item in node.names)
            ):
                violations.append(f"dispatcher-legacy-import:{node.lineno}")
    return violations


def _confirmation_preheader_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_preheader"
    ):
        aliases, strings = _call_aliases(function)
        direct_port_calls = 0
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and node.value == "load_operation_preheader":
                violations.append(f"compatibility-preheader-alias:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "operation",
                "pending_pointer",
            }:
                violations.append(f"preheader-wrapper-projection:{node.attr}:{node.lineno}")
            if isinstance(node, ast.Subscript):
                key = _string_value(node.slice, strings)
                if key in {"operation", "pending_pointer"}:
                    violations.append(f"preheader-wrapper-projection:{key}:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node, aliases, strings)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "operation_preheader":
                direct_port_calls += 1
            if name == "load_operation_preheader":
                violations.append(f"compatibility-preheader-alias:{node.lineno}")
            if name in {"_callable", "_invoke"}:
                violations.append(f"dynamic-preheader-port:{name}:{node.lineno}")
            if name in {"get", "_operation"}:
                violations.append(f"full-operation-fallback:{name}:{node.lineno}")
            if name == "LedgerOperationPreheader":
                violations.append(f"preheader-wrapper-reconstruction:{node.lineno}")
            if (
                name == "isinstance"
                and len(node.args) >= 2
                and "LedgerOperationPreheader" in ast.unparse(node.args[1])
            ):
                violations.append(f"non-exact-preheader-check:{node.lineno}")
            if name == "_attribute" and len(node.args) >= 2:
                field = _string_value(node.args[1], strings)
                if field in {"operation", "pending_pointer"}:
                    violations.append(f"preheader-wrapper-projection:{field}:{node.lineno}")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
                field = _string_value(node.args[0], strings)
                if field in {"operation", "pending_pointer"}:
                    violations.append(f"preheader-wrapper-projection:{field}:{node.lineno}")
        if direct_port_calls != 1:
            violations.append(
                f"exact-operation-preheader-call-count:{function.lineno}:{direct_port_calls}"
            )
    return violations


def _annotation(node: ast.arg) -> str:
    return "" if node.annotation is None else ast.unparse(node.annotation)


def _legacy_proof_only_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    proof_catalogs = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LegacyDeterministicCatalog"
    ]
    if path.name == "legacy_proof.py" or proof_catalogs:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(
                    module == prefix or module.startswith(prefix + ".")
                    for prefix in _LEGACY_PROOF_FORBIDDEN_IMPORTS
                ):
                    violations.append(f"legacy-proof-import:{module}:{node.lineno}")
    forbidden_capture_fragments = ("keyring", "ledger", "model", "repository", "service", "session")
    for catalog in proof_catalogs:
        for node in ast.walk(catalog):
            if isinstance(node, ast.arg):
                rendered = f"{node.arg} {_annotation(node)}".casefold()
                if any(fragment in rendered for fragment in forbidden_capture_fragments):
                    violations.append(f"proof-catalog-forbidden-capture:{node.arg}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and any(
                fragment in node.attr.casefold() for fragment in forbidden_capture_fragments
            ):
                violations.append(f"proof-catalog-forbidden-query:{node.attr}:{node.lineno}")
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        methods = {item.name: item for item in class_node.body if isinstance(item, ast.FunctionDef)}
        method = methods.get("resolve_server_loaded")
        if method is None:
            continue
        if class_node.name != "LegacyDeterministicCatalog":
            violations.append(f"second-proof-catalog:{class_node.name}:{class_node.lineno}")
            continue
        args = (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs)
        if (
            [item.arg for item in args] != ["self", "proof"]
            or method.args.vararg
            or method.args.kwarg
        ):
            violations.append(f"proof-catalog-open-signature:{method.lineno}")
        elif "LegacyRouteProof" not in _annotation(args[1]):
            violations.append(f"proof-catalog-non-proof-annotation:{method.lineno}")
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        proof_names = {
            item.arg
            for item in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
            if "LegacyRouteProof" in _annotation(item)
        }
        while True:
            changed = False
            for node in ast.walk(function):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                is_proof = (isinstance(node.value, ast.Name) and node.value.id in proof_names) or (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "issue_after_claim"
                )
                if not is_proof:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in proof_names:
                        proof_names.add(target.id)
                        changed = True
            if not changed:
                break
        aliases, strings = _call_aliases(function)
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if _call_name(call, aliases, strings) != "resolve_server_loaded":
                continue
            if (
                len(call.args) != 1
                or call.keywords
                or not isinstance(call.args[0], ast.Name)
                or call.args[0].id not in proof_names
            ):
                violations.append(f"non-proof-catalog-call:{function.name}:{call.lineno}")
    return violations


def _initial_route_violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        token_names: set[str] = set()
        handle_names: set[str] = set()
        while True:
            changed = False
            for node in ast.walk(function):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                target_names = {target.id for target in targets if isinstance(target, ast.Name)}
                if isinstance(node.value, ast.Name):
                    if node.value.id in token_names:
                        before = len(token_names)
                        token_names.update(target_names)
                        changed = changed or len(token_names) != before
                    if node.value.id in handle_names:
                        before = len(handle_names)
                        handle_names.update(target_names)
                        changed = changed or len(handle_names) != before
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                    if node.value.func.attr == "issue":
                        before = len(token_names)
                        token_names.update(target_names)
                        changed = changed or len(token_names) != before
                    elif node.value.func.attr == "resolve_initial":
                        before = len(handle_names)
                        handle_names.update(target_names)
                        changed = changed or len(handle_names) != before
            if not changed:
                break
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "resolve_initial" and (
                len(call.args) != 1
                or call.keywords
                or not isinstance(call.args[0], ast.Name)
                or call.args[0].id not in token_names
            ):
                violations.append(f"initial-route-non-token:{function.name}:{call.lineno}")
            suspicious_route_argument = bool(
                call.func.attr in {"execute", "require_route"}
                and call.args
                and isinstance(call.args[0], ast.Name)
                and any(
                    marker in call.args[0].id.casefold()
                    for marker in ("pending", "source", "tool", "name")
                )
            )
            receiver_is_initial = "initial" in ast.unparse(call.func.value).casefold()
            exact_composite_probe = bool(
                path == SRC / "pilot_runtime" / "legacy_route.py"
                and function.name == "require_route"
                and isinstance(call.func.value, ast.Attribute)
                and call.func.value.attr == "_initial_route_port"
            )
            route_handle_required = bool(
                call.func.attr == "require_route"
                and receiver_is_initial
                and not exact_composite_probe
            )
            if (route_handle_required or suspicious_route_argument) and (
                not call.args
                or not isinstance(call.args[0], ast.Name)
                or call.args[0].id not in handle_names
            ):
                violations.append(f"initial-route-non-handle:{function.name}:{call.lineno}")
    return violations


def _golden_writer_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    temporary_names: set[str] = {"tmp_path"}
    while True:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            value_names = {item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)}
            if not value_names.intersection(temporary_names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in (item.id for item in ast.walk(target) if isinstance(item, ast.Name)):
                    if name not in temporary_names:
                        temporary_names.add(name)
                        changed = True
        if not changed:
            break
    golden_names: set[str] = set()
    while True:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            rendered = ast.unparse(node.value).casefold()
            value_aliases_golden = (
                isinstance(node.value, ast.Name) and node.value.id in golden_names
            ) or any(marker in rendered for marker in ("fixture", "golden"))
            if not value_aliases_golden:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in (item.id for item in ast.walk(target) if isinstance(item, ast.Name)):
                    if name not in golden_names and name not in temporary_names:
                        golden_names.add(name)
                        changed = True
        if not changed:
            break

    def aliases_golden(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in golden_names) or any(
            marker in ast.unparse(node).casefold() for marker in ("fixture", "golden")
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "write_bytes",
            "write_text",
        }:
            if aliases_golden(node.func.value):
                violations.append(f"golden-writer:{node.lineno}")
        if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            mode = "r"
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            if any(flag in mode for flag in "wax+") and aliases_golden(node.args[0]):
                violations.append(f"golden-open-writer:{node.lineno}")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "dump"
            and len(node.args) >= 2
            and aliases_golden(node.args[1])
        ):
            violations.append(f"golden-json-writer:{node.lineno}")
    return violations


def test_task12_deleted_symbols_and_equivalent_global_roles_are_absent() -> None:
    violations = [
        violation
        for path, source in _production_sources().items()
        for violation in _exact_symbol_violations(path, source)
    ]
    assert violations == []


def test_task12_has_no_name_based_or_reflective_classification() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path, source in _production_sources().items()
        for violation in _classification_violations(path, source)
    ]
    assert violations == []


def test_task12_bundle_catalog_and_legacy_component_ownership_is_closed() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path, source in _production_sources().items()
        for violation in _construction_and_factory_violations(path, source)
    ]
    assert violations == []


def test_task12_provider_has_no_dict_second_registry_or_raw_catalog_query() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path, source in _production_sources().items()
        for violation in _provider_registry_violations(path, source)
    ]
    assert violations == []


def test_task12_has_no_fallback_feature_flag_shadow_or_double_execution() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path, source in _production_sources().items()
        for violation in _fallback_and_shadow_violations(path, source)
    ]
    assert violations == []


def test_task12_confirmation_uses_only_exact_bounded_preheader_port() -> None:
    path = SRC / "pilot_runtime" / "continuation.py"

    assert (
        _confirmation_preheader_violations(
            path,
            path.read_text(encoding="utf-8"),
        )
        == []
    )


def test_task12_legacy_catalog_is_proof_only_and_initial_routes_are_token_only() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path, source in _production_sources().items()
        for violation in (
            *_legacy_proof_only_violations(path, source),
            *_initial_route_violations(path, source),
        )
    ]
    assert violations == []


def test_binding_resolution_without_exact_authority_port_fails_closed() -> None:
    class ResolverContextWithoutAuthorityPort:
        pass

    try:
        result = resolve_identity_argument(
            {"application_id": 7},
            ResolverContextWithoutAuthorityPort(),
            entity_kind="application",
            arg_path="application_id",
            presence="required",
        )
    except (AttributeError, TypeError, ValueError):
        return
    target_shaped = all(hasattr(result, name) for name in ("entity_kind", "identity", "available"))
    pytest.fail(
        "missing authority-bound resolution port returned a value; "
        f"old target shape={target_shaped}"
    )


def test_parent_resolution_uses_only_the_exact_resolver_context_method() -> None:
    class ForbiddenOptionalPort:
        def resolve_parent_identity(self, entity_kind: str, identity: int) -> tuple[str, int]:
            del entity_kind, identity
            raise AssertionError("optional binding_resolver_port compatibility path was used")

    class ExactResolverContext:
        binding_resolver_port = ForbiddenOptionalPort()

        def resolve_parent_identity(
            self,
            entity_kind: str,
            identity: int,
        ) -> tuple[str, int]:
            assert entity_kind == "application"
            assert identity == 9
            return ("resolved", 41)

        def binding_target_resolution(
            self,
            *,
            entity_kind: str,
            state: str,
            identity: int | None,
        ) -> tuple[str, str, int | None]:
            return (entity_kind, state, identity)

    assert resolve_parent_application(
        {"id": 9},
        ExactResolverContext(),
        arg_path="id",
        entity_kind="application",
    ) == ("application", "resolved", 41)


def test_golden_and_fixture_helpers_are_read_only() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{violation}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        for violation in _golden_writer_violations(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


@pytest.mark.parametrize(
    ("validator", "source", "code"),
    (
        (
            lambda source: _exact_symbol_violations(Path("fixture.py"), source),
            "from old import BindingTarget as Target\nvalue = Target('application', 1, True)\n",
            "BindingTarget",
        ),
        (
            lambda source: _exact_symbol_violations(Path("fixture.py"), source),
            "name = 'MODEL_' + 'TOOL_CATALOG'\nvalue = getattr(module, name)\n",
            "MODEL_TOOL_CATALOG",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "ROUTES = {'create_application', 'update_application_status'}\n",
            "name-set",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "_TOOL_NAMES = {'create_application', 'update_application_status'}\n",
            "name-set",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "_USER_FACING_TOOL_NAMES = "
            "{'create_application': 'Create', 'update_application_status': 'Update'}\n",
            "name-set",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "def route(tool_name):\n    return tool_name.startswith('create_')\n",
            "string-prefix-routing",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "def route(spec):\n    return getattr(spec, 'adapter_kind')\n",
            "reflective-classification",
        ),
        (
            lambda source: _classification_violations(Path("fixture.py"), source),
            "def _attribute(value, name):\n"
            "    return getattr(value, name)\n"
            "def route(operation):\n"
            "    return _attribute(operation, 'adapter_kind')\n",
            "reflective-classification",
        ),
        (
            lambda source: _provider_registry_violations(Path("fixture.py"), source),
            "registry = {contract.name: contract for contract in provider_contracts}\n",
            "provider-dict-registry",
        ),
        (
            lambda source: _provider_registry_violations(Path("fixture.py"), source),
            "def build(contracts):\n"
            "    registry = {item.name: item for item in contracts}\n"
            "    return registry\n",
            "provider-dict-registry",
        ),
        (
            lambda source: _provider_registry_violations(Path("fixture.py"), source),
            "class OtherCatalog:\n"
            "    def resolve(self, name): pass\n"
            "    def provider_contracts(self): pass\n",
            "second-provider-registry",
        ),
        (
            lambda source: _provider_registry_violations(Path("fixture.py"), source),
            "payload = dict(contract.payload)\n",
            "provider-payload-dict",
        ),
        (
            lambda source: _fallback_and_shadow_violations(Path("fixture.py"), source),
            "def dispatch(provider_registry, legacy_registry, name):\n"
            "    return provider_registry.get(name) or legacy_registry.get(name)\n",
            "typed-to-legacy-fallback",
        ),
        (
            lambda source: _fallback_and_shadow_violations(Path("fixture.py"), source),
            "def dispatch(prepared, legacy):\n"
            "    first = execute_prepared(prepared)\n"
            "    second = execute_legacy(legacy)\n"
            "    return first, second\n",
            "double-or-fallback-execution",
        ),
        (
            lambda source: _golden_writer_violations(source),
            "GOLDEN.write_text(rendered, encoding='utf-8')\n",
            "golden-writer",
        ),
        (
            lambda source: _golden_writer_violations(source),
            "target = GOLDEN\ntarget.write_text(rendered, encoding='utf-8')\n",
            "golden-writer",
        ),
    ),
)
def test_negative_fixtures_prove_task12_gates_reject_equivalent_roles(
    validator: object,
    source: str,
    code: str,
) -> None:
    violations = validator(source)  # type: ignore[operator]
    assert any(code in violation for violation in violations)


@pytest.mark.parametrize(
    ("source", "code"),
    (
        (
            "class ConfirmationCoordinator:\n"
            "    def _preheader(self, repository):\n"
            "        return repository.load_operation_preheader()\n",
            "compatibility-preheader-alias",
        ),
        (
            "class ConfirmationCoordinator:\n"
            "    def _preheader(self, repository):\n"
            "        value = repository.operation_preheader()\n"
            "        return LedgerOperationPreheader(\n"
            "            value['operation'], value['pending_pointer']\n"
            "        )\n",
            "preheader-wrapper-projection",
        ),
        (
            "class ConfirmationCoordinator:\n"
            "    def _preheader(self, repository):\n"
            "        value = repository.operation_preheader()\n"
            "        return LedgerOperationPreheader(\n"
            "            value.operation, value.pending_pointer\n"
            "        )\n",
            "preheader-wrapper-projection",
        ),
        (
            "class ConfirmationCoordinator:\n"
            "    def _preheader(self, repository):\n"
            "        operation = repository.get('operation-id')\n"
            "        return LedgerOperationPreheader(operation, None)\n",
            "full-operation-fallback",
        ),
    ),
)
def test_negative_fixtures_reject_confirmation_preheader_compatibility_roles(
    source: str,
    code: str,
) -> None:
    violations = _confirmation_preheader_violations(Path("fixture.py"), source)

    assert any(code in violation for violation in violations)


def test_smoke_classification_allowlist_is_exact_owner_and_assertion_only() -> None:
    allowed = (
        "def _assert_create_application_card(action):\n"
        "    if action.get('tool_name') != 'create_application':\n"
        "        raise RuntimeError('wrong tool')\n"
    )
    assert _classification_violations(SRC / "smoke.py", allowed) == []

    wrong_owner = allowed.replace("_assert_create_application_card", "run_smoke")
    assert _classification_violations(SRC / "smoke.py", wrong_owner)

    routing_body = allowed.replace(
        "raise RuntimeError('wrong tool')",
        "return execute_create_application()",
    )
    assert _classification_violations(SRC / "smoke.py", routing_body)


def test_tool_specs_allow_only_static_declarations_not_name_switches() -> None:
    source = "def route(tool_name):\n    return tool_name == 'create_application'\n"

    assert _classification_violations(
        SRC / "ai" / "tool_specs" / "applications.py",
        source,
    )


@pytest.mark.parametrize(
    "source",
    (
        "class OtherCatalog:\n    def resolve_server_loaded(self, proof): return proof\n",
        "class LegacyDeterministicCatalog:\n"
        "    def resolve_server_loaded(self, pending: PendingAction): return pending\n",
        "def route(catalog, pending):\n    return catalog.resolve_server_loaded(pending)\n",
        "def route(catalog, tool_name):\n    return catalog.resolve_server_loaded(tool_name)\n",
    ),
)
def test_negative_fixtures_prove_legacy_catalog_is_proof_only(source: str) -> None:
    assert _legacy_proof_only_violations(Path("fixture.py"), source)


def test_legacy_proof_catalog_cannot_capture_or_query_a_repository() -> None:
    source = (
        "from offerpilot.repositories.chat import ChatRepository\n"
        "class LegacyDeterministicCatalog:\n"
        "    def __init__(self, repository: ChatRepository):\n"
        "        self.repository = repository\n"
        "    def resolve_server_loaded(self, proof: LegacyRouteProof):\n"
        "        return self.repository.get(proof.tool_name)\n"
    )

    assert _legacy_proof_only_violations(
        SRC / "ai" / "tool_runtime" / "legacy.py",
        source,
    )


@pytest.mark.parametrize(
    "source",
    (
        "def route(initial_port, pending):\n    return initial_port.resolve_initial(pending)\n",
        "def route(initial_port, source):\n    return initial_port.resolve_initial(source)\n",
        "def route(initial_port):\n    return initial_port.resolve_initial('tool_name')\n",
        "def route(port, pending):\n    return port.require_route(pending)\n",
        "def route(port, pending):\n    return port.execute(pending)\n",
    ),
)
def test_negative_fixtures_prove_initial_route_requires_exact_token(source: str) -> None:
    assert _initial_route_violations(Path("fixture.py"), source)


def test_negative_fixtures_prove_bundle_and_factory_owner_gate_is_exact() -> None:
    source = (
        "from x import ToolMetadataBundleV1 as Bundle\n"
        "from x import build_unpublished_legacy_confirmation_components as factory\n"
        "def build():\n"
        "    bundle = Bundle()\n"
        "    return factory(bundle)\n"
    )
    violations = _construction_and_factory_violations(Path("fixture.py"), source)
    assert any("bundle-constructor-owner" in item for item in violations)
    assert any("legacy-component-factory-owner" in item for item in violations)
