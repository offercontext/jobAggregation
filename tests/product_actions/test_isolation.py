from __future__ import annotations

import ast
import json
from pathlib import Path

from offerpilot.ai.tool_runtime.contracts import ToolFailure
from offerpilot.ai.tool_runtime.pipeline import Rejected, prepare_call
from offerpilot.ai.types import ToolCall
from offerpilot.product_actions.contracts import (
    PRODUCT_ACTION_COMPENSATION_NAMES,
    PRODUCT_ACTION_NAMES,
)
from tests.tool_pipeline.test_pipeline import Recorder, _lease, _runtime, _spec


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOT = ROOT / "src" / "offerpilot" / "product_actions"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_product_action_modules_are_isolated_from_agent_provider_legacy_chat_and_journal() -> None:
    forbidden_import_fragments = (
        "tool_specs",
        "tool_runtime.catalog",
        "legacy",
        "pilot_runtime.compensation",
        "provider",
        "chat",
        "journal",
    )
    forbidden_symbols = {
        "ToolCatalog",
        "LegacyDeterministicCatalog",
        "WriteOperationRepository",
        "WriteOperationCoordinator",
        "ChatMessage",
        "Conversation",
        "AgentRun",
        "JournalEvent",
    }

    present_modules = {path.name for path in PRODUCT_ROOT.glob("*.py")}
    task3_modules = {
        "__init__.py",
        "contracts.py",
        "catalog.py",
        "issuer.py",
        "repository.py",
    }
    assert task3_modules <= present_modules
    assert present_modules <= task3_modules | {"coordinator.py", "compensation.py"}
    for path in PRODUCT_ROOT.glob("*.py"):
        imports = _imports(path)
        assert not any(
            fragment in imported
            for imported in imports
            for fragment in forbidden_import_fragments
        ), path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert names.isdisjoint(forbidden_symbols), path


def test_provider_pipeline_returns_both_product_names_as_unknown_without_executor(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    runtime = _runtime(tmp_path, recorder)
    try:
        spec = _spec()
        lease = _lease(runtime, spec)
        for index, action_name in enumerate(PRODUCT_ACTION_NAMES):
            trace: list[str] = []
            call = ToolCall(
                id=f"product-action-{index}",
                name=action_name,
                args="{}",
            )
            result = prepare_call(
                lease,
                runtime.context,
                call,
                call_identity=runtime.prepare_identity(call),
                stage_sink=trace.append,
            )
            assert isinstance(result, Rejected)
            assert result.failure == ToolFailure(
                "validation_error",
                "unknown_tool",
                f'未知工具 "{action_name}"',
            )
            assert trace == ["authority.prelookup", "catalog.lookup"]
        assert recorder.events == []
    finally:
        runtime.close()


def test_product_actions_never_enter_agent_metadata_or_compensation_fixtures() -> None:
    manifest = json.loads(
        (ROOT / "tests" / "fixtures" / "tool_metadata" / "tool_metadata_manifest_current.json")
        .read_text(encoding="utf-8")
    )
    provider_names = tuple(item["provider_name"] for item in manifest["typed_tools"])
    legacy_names = tuple(manifest["legacy_boundary"]["ordered_names"])
    agent_compensations = tuple(manifest["compensation_operation_order"])

    assert len(provider_names) == 26
    assert len(legacy_names) == 3
    assert len(agent_compensations) == 5
    assert set(PRODUCT_ACTION_NAMES).isdisjoint(provider_names)
    assert set(PRODUCT_ACTION_NAMES).isdisjoint(legacy_names)
    assert set(PRODUCT_ACTION_COMPENSATION_NAMES).isdisjoint(agent_compensations)


def _function(path: Path, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owner = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def test_all_three_chat_delivery_ownership_paths_explicitly_exclude_product_actions() -> None:
    path = ROOT / "src" / "offerpilot" / "ai" / "write_operations.py"
    for method_name in ("complete_delivery", "heartbeat", "converge_expired_delivery"):
        method = _function(path, "WriteOperationRepository", method_name)
        source = ast.get_source_segment(path.read_text(encoding="utf-8"), method) or ""
        assert "adapter_kind" in source
        assert "product_action" in source


def test_historical_runtime_classification_is_exactly_25_3_4_plus_2_2() -> None:
    fixture = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "review_readiness"
            / "review_to_readiness_baseline_c5a020c.json"
        ).read_text(encoding="utf-8")
    )
    assert (
        fixture["provider_tools"],
        fixture["legacy_deterministic"],
        fixture["agent_compensations"],
        len(PRODUCT_ACTION_NAMES),
        len(PRODUCT_ACTION_COMPENSATION_NAMES),
    ) == (25, 3, 4, 2, 2)
