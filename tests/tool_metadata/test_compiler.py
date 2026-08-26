from __future__ import annotations

import ast
import copy
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import importlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offerpilot.ai import client as ai_client
from offerpilot.ai.client import ConfiguredAIClient, ProviderCallError
from offerpilot.ai.tool_authority import AuthorityFactory, TrustedContextScope
from offerpilot.ai.tool_runtime.catalog import (
    ToolCatalog,
    compile_tool_metadata_manifest,
)
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import ToolSpec, ToolSuccess
from offerpilot.ai.tool_runtime.metadata import (
    ToolMetadataBundleV1,
    WriteOperationMetadataV1,
    freeze_json,
    materialize_json,
)
from offerpilot.ai.tool_runtime.pipeline import prepare_call
from offerpilot.ai.tool_runtime.policy_types import UndoPolicy
from offerpilot.ai.tool_runtime.transport import project_transport_event
from offerpilot.ai.tool_specs.catalog import MODEL_TOOL_CATALOG
from offerpilot.ai.types import Message, ToolCall
from offerpilot.config import Config

from .factories import (
    compose_synthetic_bundle,
    presentation_binding,
    read_metadata,
    resolver_descriptor,
    synthetic_tool_spec,
)
from .golden import load_asset


REQUIRED_UNDO = {
    "create_application",
    "update_application_status",
    "create_application_event",
    "add_note",
}

ROOT = Path(__file__).parents[2]
PRODUCTION_ROOT = ROOT / "src" / "offerpilot"
TEST_ROOT = ROOT / "tests"


class _ExecutionProbe:
    def __init__(self, counters: dict[str, int]) -> None:
        self._counters = counters

    def __call__(self, args: object, context: object) -> object:
        del args, context
        self._counters["executor"] += 1
        raise AssertionError("executor must not run after Catalog integrity drift")


class _RepositoryProbe:
    def __init__(self, session_factory: object, counters: dict[str, int]) -> None:
        self._session_factory = session_factory
        self._counters = counters

    def __getattr__(self, name: str) -> object:
        del name
        self._counters["repository"] += 1
        raise AssertionError("repository must not run after Catalog integrity drift")


class _PreflightRepositoryProbe:
    def __init__(self, counters: dict[str, int]) -> None:
        self._counters = counters

    def __call__(self, args: object, context: object) -> None:
        del args, context
        self._counters["repository"] += 1
        return None


class _RecorderProbe:
    recording_status = "healthy"

    def append_event(self, event: object) -> None:
        del event
        return None


class _PipelineProbe:
    def __init__(self, counters: dict[str, int]) -> None:
        self.factory = AuthorityFactory()
        authority = self.factory.create_segment_authority(
            conversation_id=1,
            conversation_scope_revision=0,
            segment_id="task-4-catalog-integrity-probe",
            trusted_scope=TrustedContextScope("workspace", None, "general"),
            capabilities=frozenset({"applications.read"}),
        )
        session_factory = object()
        repository = _RepositoryProbe(session_factory, counters)
        self.context = ToolExecutionContext(
            authority=authority,
            applications=cast(Any, repository),
            events=cast(Any, repository),
            notes=cast(Any, repository),
            offers=cast(Any, repository),
            resumes=cast(Any, repository),
            jd_analyses=cast(Any, repository),
            run_recorder=cast(Any, _RecorderProbe()),
        )
        runner = object()
        surface = object()
        binding = object()
        gateway = object()
        self.factory.register_runner_invocation(runner, authority=authority)
        self.factory.register_tool_execution_context(self.context, authority=authority)
        build = self.factory.create_provider_surface_build_identity(
            authority,
            runner_invocation=runner,
            tool_context=self.context,
            model_call_id="task-4-catalog-integrity-probe",
        )
        fingerprint = "sha256:" + "a" * 64
        self.factory.register_frozen_surface(
            surface,
            surface_fingerprint=fingerprint,
            candidate_count=1,
            authority=authority,
            build_identity=build,
        )
        self.factory.register_model_call_surface_binding(
            binding,
            surface=surface,
            surface_fingerprint=fingerprint,
            authority=authority,
            build_identity=build,
        )
        self.factory.register_gateway_session(
            gateway,
            authority=authority,
            build_identity=build,
            surface=surface,
            surface_fingerprint=fingerprint,
            model_call_surface_binding=binding,
        )
        self.invocation = self.factory.create_provider_invocation_identity(
            build,
            surface=surface,
            surface_fingerprint=fingerprint,
            model_call_surface_binding=binding,
            gateway_session=gateway,
        )

    def prepare_identity(self, call: ToolCall) -> object:
        attempt = self.factory.issue_provider_attempt(
            self.invocation,
            candidate_ordinal=0,
        )
        return self.factory.create_new_turn_prepare_identity(
            self.invocation,
            attempt_id=attempt,
            candidate_ordinal=0,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_digest=_arguments_digest(call.args),
        )

    def close(self) -> None:
        self.factory.close()


def _arguments_digest(raw: str) -> str:
    value = json.loads(raw)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _catalog(specs: tuple[ToolSpec[Any, Any], ...]) -> ToolCatalog:
    return ToolCatalog(specs, expected_names=tuple(spec.name for spec in specs))


def _replacement(value: object) -> object:
    if type(value) is tuple:
        return tuple(list(cast(tuple[object, ...], value)))
    if isinstance(value, Mapping):
        return dict(cast(Mapping[object, object], value))
    return copy.deepcopy(value)


def _replace_catalog_component(catalog: ToolCatalog, component: str) -> None:
    attribute = {
        "order": "_ordered",
        "registry": "_specs",
        "validator": "_validators",
        "authority_manifest": "_authority_manifest",
        "integrity_cache": "_integrity_snapshots",
    }[component]
    original = getattr(catalog, attribute)
    replacement = _replacement(original)
    assert replacement == original
    assert replacement is not original
    object.__setattr__(catalog, attribute, replacement)


def _terminal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _annotation_mentions_provider(node: ast.AST | None) -> bool:
    return node is not None and "ProviderToolContract" in ast.unparse(node)


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in node.elts))
    return set()


def _contains_name(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node))


class _ProviderProvenanceGate:
    """Small assignment-aware taint pass for Provider query values."""

    _MUTABLE_SINKS = frozenset(
        {
            "_canonical_contract_fingerprint",
            "_contract_fingerprint",
            "canonical_json",
            "canonical_json_bytes",
            "copy",
            "deepcopy",
            "dict",
            "dumps",
            "extend",
            "list",
            "loads",
            "materialize_json",
            "setdefault",
            "set",
            "update",
            "append",
        }
    )

    def __init__(self, path: Path, tree: ast.Module) -> None:
        self.path = path
        self.tree = tree
        self.contract_names: set[str] = set()
        self.contract_collection_names: set[str] = set()
        self.derived_names: set[str] = set()
        self.violations: list[str] = []
        self.allowed_materializer_lines: set[int] = set()
        if path == PRODUCTION_ROOT / "ai" / "tool_runtime" / "contracts.py":
            for node in tree.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "materialize_provider_payloads"
                ):
                    self.allowed_materializer_lines.update(
                        item.lineno for item in ast.walk(node) if hasattr(item, "lineno")
                    )

    def run(self) -> list[str]:
        self._seed_contract_names()
        self._propagate_aliases()
        self._check_mutable_sinks()
        return self.violations

    def _seed_contract_names(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
                for argument in arguments:
                    if _annotation_mentions_provider(argument.annotation):
                        annotation = ast.unparse(argument.annotation)
                        if annotation == "ProviderToolContract":
                            self.contract_names.add(argument.arg)
                        else:
                            self.contract_collection_names.add(argument.arg)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if _annotation_mentions_provider(node.annotation):
                    annotation = ast.unparse(node.annotation)
                    if annotation == "ProviderToolContract":
                        self.contract_names.add(node.target.id)
                    else:
                        self.contract_collection_names.add(node.target.id)
            elif isinstance(node, (ast.Assign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                target_names = set().union(*(_target_names(item) for item in targets))
                if isinstance(value, ast.Call) and _terminal(value.func) == "provider_contracts":
                    self.contract_collection_names.update(target_names)
                if isinstance(value, ast.Attribute) and value.attr == "contract":
                    self.contract_names.update(target_names)

        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.For, ast.comprehension)) and self._is_collection(
                    node.iter
                ):
                    before = len(self.contract_names)
                    self.contract_names.update(_target_names(node.target))
                    changed |= len(self.contract_names) != before

    def _is_collection(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.contract_collection_names
        if isinstance(node, ast.Call) and _terminal(node.func) == "provider_contracts":
            return True
        return isinstance(node, ast.Attribute) and node.attr in {
            "provider_tools",
            "tools",
        }

    def _is_provider_query(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute) or node.attr not in {"parameters", "payload"}:
            return False
        value = node.value
        if isinstance(value, ast.Name) and value.id in self.contract_names:
            return True
        if isinstance(value, ast.Attribute) and value.attr == "contract":
            return True
        return isinstance(value, ast.Subscript) and self._is_collection(value.value)

    def _is_derived(self, node: ast.AST) -> bool:
        return self._is_provider_query(node) or _contains_name(node, self.derived_names)

    def _propagate_aliases(self) -> None:
        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Assign):
                    targets = set().union(*(_target_names(item) for item in node.targets))
                    value = node.value
                elif isinstance(node, ast.AnnAssign):
                    targets = _target_names(node.target)
                    value = node.value
                elif isinstance(node, ast.NamedExpr):
                    targets = _target_names(node.target)
                    value = node.value
                else:
                    continue
                if value is not None and self._is_derived(value):
                    before = len(self.derived_names)
                    self.derived_names.update(targets)
                    changed |= len(self.derived_names) != before

    def _check_mutable_sinks(self) -> None:
        for node in ast.walk(self.tree):
            if getattr(node, "lineno", None) in self.allowed_materializer_lines:
                continue
            if isinstance(node, ast.Call):
                terminal = _terminal(node.func)
                derived_argument = any(
                    self._is_derived(argument)
                    for argument in (*node.args, *(item.value for item in node.keywords))
                )
                receiver_is_derived = isinstance(node.func, ast.Attribute) and self._is_derived(
                    node.func.value
                )
                if terminal in self._MUTABLE_SINKS and (derived_argument or receiver_is_derived):
                    self.violations.append(
                        f"{self.path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        f" Provider value reaches {terminal}()"
                    )
            elif isinstance(node, (ast.Dict, ast.List, ast.Set)):
                if self._is_derived(node):
                    self.violations.append(
                        f"{self.path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        " Provider value reaches mutable literal construction"
                    )
            elif isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp)):
                if self._is_derived(node):
                    self.violations.append(
                        f"{self.path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        " Provider value reaches mutable comprehension construction"
                    )


def _provider_gate_paths() -> tuple[Path, ...]:
    production = tuple(PRODUCTION_ROOT.rglob("*.py"))
    affected_tests = tuple(
        path
        for path in TEST_ROOT.rglob("*.py")
        if path != Path(__file__)
        and (
            "ProviderToolContract" in path.read_text(encoding="utf-8")
            or ".contract.payload" in path.read_text(encoding="utf-8")
            or ".contract.parameters" in path.read_text(encoding="utf-8")
        )
    )
    return production + affected_tests


def _provider_baseline_payloads() -> list[dict[str, Any]]:
    fixture = (
        Path(__file__).parents[1] / "fixtures" / "tool_pipeline" / "provider_manifest_30c944f.json"
    )
    value = json.loads(fixture.read_bytes().decode("utf-8"))
    return copy.deepcopy(value["tools"])


def _patch_provider_verifier(monkeypatch: pytest.MonkeyPatch, verifier: Any) -> Any:
    catalog_module = importlib.import_module("offerpilot.ai.tool_specs.catalog")
    protocol_module = importlib.import_module("offerpilot.ai.tool_runtime.protocol_seals")
    monkeypatch.setattr(protocol_module, "verify_provider_boundary", verifier)
    monkeypatch.setattr(catalog_module, "verify_provider_boundary", verifier, raising=False)
    return catalog_module


def test_compiler_exposes_exact_ordered_25_typed_specs() -> None:
    catalog = MODEL_TOOL_CATALOG
    specs = catalog.specs
    assert len(specs) == 25
    assert len({spec.name for spec in specs}) == 25
    assert tuple(spec.name for spec in specs) == tuple(
        item["provider_name"]
        for item in load_asset("tool_metadata_manifest_v1.json")["typed_tools"]
    )
    assert all(spec.metadata.provider_visibility.value == "model_eligible" for spec in specs)


def test_production_catalog_verifies_complete_ordered_provider_boundary_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_module = importlib.import_module("offerpilot.ai.tool_runtime.protocol_seals")
    original_verifier = protocol_module.verify_provider_boundary
    captured: list[tuple[dict[str, Any], ...]] = []
    catalog_module = importlib.import_module("offerpilot.ai.tool_specs.catalog")
    published_before = catalog_module.MODEL_TOOL_CATALOG

    def spy_verifier(*args: Any, **kwargs: Any) -> None:
        payloads = args[0] if args else kwargs.get("payloads")
        assert payloads is not None
        captured.append(tuple(copy.deepcopy(payloads)))
        assert catalog_module.MODEL_TOOL_CATALOG is published_before
        original_verifier(*args, **kwargs)

    _patch_provider_verifier(monkeypatch, spy_verifier)
    result = catalog_module.build_model_tool_catalog()

    assert isinstance(result, ToolCatalog)
    assert len(captured) == 1
    assert list(captured[0]) == _provider_baseline_payloads()


def test_production_catalog_does_not_publish_after_provider_seal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_module = importlib.import_module("offerpilot.ai.tool_specs.catalog")
    published_before = catalog_module.MODEL_TOOL_CATALOG

    def reject_provider_boundary(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise ValueError("provider boundary seal rejected")

    _patch_provider_verifier(monkeypatch, reject_provider_boundary)
    with pytest.raises(ValueError, match="provider|boundary|seal|rejected"):
        catalog_module.build_model_tool_catalog()
    assert catalog_module.MODEL_TOOL_CATALOG is published_before


def test_generic_compiler_rejects_duplicate_names_and_missing_order() -> None:
    one = synthetic_tool_spec("synthetic_one")
    two = synthetic_tool_spec("synthetic_two")

    with pytest.raises((TypeError, ValueError), match="name|order|duplicate"):
        _catalog((one, one))
    with pytest.raises((TypeError, ValueError), match="name|order|duplicate"):
        ToolCatalog((one, two), expected_names=("synthetic_two", "synthetic_one"))


@pytest.mark.parametrize(
    "metadata",
    (
        replace(read_metadata(), dependencies=("synthetic_two",)),
        replace(read_metadata(), dependencies=("synthetic_one",)),
    ),
)
def test_generic_compiler_rejects_self_unknown_and_dependency_cycles(
    metadata: object,
) -> None:
    first = synthetic_tool_spec("synthetic_one", metadata=metadata)  # type: ignore[arg-type]
    second = synthetic_tool_spec("synthetic_two", metadata=read_metadata())
    if first.metadata.dependencies == ("synthetic_two",):
        second = synthetic_tool_spec(
            "synthetic_two",
            metadata=replace(read_metadata(), dependencies=("synthetic_one",)),
        )
    with pytest.raises((TypeError, ValueError), match="depend|cycle|self|unknown"):
        _catalog((first, second))

    unknown = synthetic_tool_spec(
        "synthetic_unknown",
        metadata=replace(read_metadata(), dependencies=("does_not_exist",)),
    )
    with pytest.raises((TypeError, ValueError), match="depend|unknown"):
        _catalog((unknown,))

    legacy = synthetic_tool_spec(
        "synthetic_legacy_reference",
        metadata=replace(
            read_metadata(),
            dependencies=("save_application_jd_version",),
        ),
    )
    with pytest.raises((TypeError, ValueError), match="Legacy|legacy|depend|unknown"):
        _catalog((legacy,))


def test_compiler_enforces_read_write_union_and_operation_projection() -> None:
    read_spec = synthetic_tool_spec("synthetic_read")
    bad_read = replace(
        read_spec,
        metadata=replace(read_spec.metadata, confirmation_policy="required"),
    )
    with pytest.raises((TypeError, ValueError), match="read|confirmation|operation"):
        _catalog((bad_read,))

    write_spec = synthetic_tool_spec("synthetic_write", metadata=read_metadata())
    bad_write = replace(
        write_spec,
        metadata=replace(write_spec.metadata, operation=WriteOperationMetadataV1()),
    )
    with pytest.raises((TypeError, ValueError), match="write|confirmation|operation"):
        _catalog((bad_write,))

    manifest = MODEL_TOOL_CATALOG.authority_manifest
    for spec, projected in zip(MODEL_TOOL_CATALOG.specs, manifest["tools"]):
        expected_kind = (
            "write" if isinstance(spec.metadata.operation, WriteOperationMetadataV1) else "read"
        )
        assert projected["kind"] == expected_kind
        assert projected["kind"] != "transactional_write"


def test_compiler_seals_provider_payload_and_runtime_callable_identity() -> None:
    spec = synthetic_tool_spec(
        "synthetic_bound",
        metadata=read_metadata(resolver_descriptors=(resolver_descriptor(),)),
    )
    catalog = _catalog((spec,))
    assert catalog.resolve(spec.name) is spec

    mutated_payload = catalog.materialize_provider_payloads()[0]
    mutated_payload["function"]["description"] = "mutated"  # type: ignore[index]
    object.__setattr__(spec.contract, "payload", mutated_payload)
    with pytest.raises((TypeError, ValueError), match="integrity|payload|provider|seal"):
        catalog.resolve(spec.name)

    fresh = synthetic_tool_spec(
        "synthetic_bound_again",
        metadata=read_metadata(resolver_descriptors=(resolver_descriptor(),)),
    )
    fresh_catalog = _catalog((fresh,))
    object.__setattr__(
        fresh.resolver_bindings[0], "resolve", presentation_binding().success_summary_projector
    )
    with pytest.raises((TypeError, ValueError), match="integrity|callable|identity|seal"):
        fresh_catalog.resolve(fresh.name)


def test_provider_queries_are_distinct_recursively_immutable_nodes() -> None:
    spec = synthetic_tool_spec("synthetic_materialization")
    payload = spec.contract.payload
    parameters = spec.contract.parameters
    function = payload["function"]
    payload_parameters = function["parameters"]  # type: ignore[index]
    properties = parameters["properties"]
    required = parameters["required"]

    for node in (payload, parameters, function, payload_parameters, properties, required):
        assert not isinstance(node, (dict, list))

    with pytest.raises(TypeError):
        payload["new"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        function["description"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        properties["new"] = {"type": "string"}  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        required.append("new")  # type: ignore[union-attr]

    for query in (payload, parameters):
        with pytest.raises(TypeError, match="Provider|provider|immutable|snapshot"):
            materialize_json(cast(Any, query))


def test_sole_provider_materializer_is_fresh_detached_and_catalog_delegates() -> None:
    contracts_module = importlib.import_module("offerpilot.ai.tool_runtime.contracts")
    operation = getattr(contracts_module, "materialize_provider_payloads", None)
    assert callable(operation), "the sole Provider materializer must be a module operation"

    spec = synthetic_tool_spec("synthetic_materialization")
    catalog = _catalog((spec,))
    first = operation((spec.contract,))
    second = operation((spec.contract,))
    delegated = catalog.materialize_provider_payloads()

    assert first == second == delegated
    assert first is not second
    assert first[0] is not second[0]
    assert first[0]["function"] is not second[0]["function"]
    assert first[0]["function"]["parameters"] is not second[0]["function"]["parameters"]

    first[0]["function"]["description"] = "mutated materialization"
    first[0]["function"]["parameters"]["required"].append("new")
    assert second[0]["function"]["description"] != "mutated materialization"
    assert "new" not in second[0]["function"]["parameters"]["required"]
    assert spec.contract.payload["function"]["description"] != "mutated materialization"


@pytest.mark.parametrize("component", ("payload", "parameters"))
def test_same_content_provider_component_replacement_fails_before_real_adapter(
    component: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = synthetic_tool_spec("synthetic_provider_adapter")
    catalog = _catalog((spec,))
    replacement = (
        catalog.materialize_provider_payloads()[0]
        if component == "payload"
        else copy.deepcopy(catalog.validator_for(spec.name).schema)
    )
    original = getattr(spec.contract, component)
    assert replacement == original
    assert replacement is not original
    object.__setattr__(spec.contract, component, replacement)

    provider_calls = 0

    def completion_probe(**kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        del kwargs
        provider_calls += 1
        return {"choices": [{"message": {"content": "unexpected"}}]}

    monkeypatch.setattr(ai_client, "completion", completion_probe)
    client = ConfiguredAIClient(Config(api_key="task-4-provider-probe"))
    with pytest.raises(ProviderCallError) as captured:
        client.complete(
            [Message(role="user", content="must fail before Provider")],
            [spec.contract],
        )
    assert isinstance(captured.value.__cause__, ValueError)
    assert "integrity" in str(captured.value.__cause__).lower()
    assert provider_calls == 0


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("name", "tampered_provider_name"),
        ("description", "tampered Provider description"),
    ),
)
def test_provider_envelope_scalar_drift_fails_before_real_adapter(
    field: str,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = synthetic_tool_spec("synthetic_provider_scalar")
    object.__setattr__(spec.contract, field, replacement)
    provider_calls = 0

    def completion_probe(**kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        del kwargs
        provider_calls += 1
        return {"choices": [{"message": {"content": "unexpected"}}]}

    monkeypatch.setattr(ai_client, "completion", completion_probe)
    client = ConfiguredAIClient(Config(api_key="task-4-provider-scalar-probe"))
    with pytest.raises(ProviderCallError) as captured:
        client.complete(
            [Message(role="user", content="must fail before Provider")],
            [spec.contract],
        )
    assert isinstance(captured.value.__cause__, ValueError)
    assert "integrity" in str(captured.value.__cause__).lower()
    assert provider_calls == 0


@pytest.mark.parametrize(
    "component",
    ("order", "registry", "validator", "authority_manifest", "integrity_cache"),
)
def test_catalog_topology_replacement_fails_at_materialization_and_real_pipeline(
    component: str,
) -> None:
    counters = {"repository": 0, "executor": 0}
    executor = _ExecutionProbe(counters)
    spec = replace(
        synthetic_tool_spec("synthetic_catalog_integrity"),
        executor=executor,
        preflight=_PreflightRepositoryProbe(counters),
    )
    catalog = _catalog((spec,))
    source = compose_synthetic_bundle()
    manifest = dict(cast(dict[str, object], source["manifest"]))
    manifest["typed_tools"] = (spec.name,)
    bundle = ToolMetadataBundleV1(
        typed_catalog=catalog,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], source["legacy_boundary"]),
        compensation=cast(dict[str, object], source["compensation"]),
    )
    lease = bundle.open_segment_lease()
    probe = _PipelineProbe(counters)
    probe.factory.bind_segment_tool_catalog(
        probe.context.authority,
        authority_metadata_view=bundle.authority_view(),
        catalog_lease=lease,
    )
    _replace_catalog_component(catalog, component)

    materialization_rejected = False
    try:
        catalog.materialize_provider_payloads()
    except (TypeError, ValueError) as exc:
        assert "integrity" in str(exc).lower() or "seal" in str(exc).lower()
        materialization_rejected = True

    call = ToolCall(id="catalog-integrity", name=spec.name, args='{"id":1}')
    pipeline_rejected = False
    try:
        try:
            prepare_call(
                lease,
                probe.context,
                call,
                call_identity=probe.prepare_identity(call),
            )
        except (TypeError, ValueError) as exc:
            assert "integrity" in str(exc).lower() or "seal" in str(exc).lower()
            pipeline_rejected = True
    finally:
        probe.close()

    assert materialization_rejected
    assert pipeline_rejected
    assert counters == {"repository": 0, "executor": 0}


def test_manifest_same_content_projection_replacement_fails_before_to_dict() -> None:
    manifest = compile_tool_metadata_manifest(MODEL_TOOL_CATALOG.specs)
    original = manifest._projection  # type: ignore[attr-defined]
    replacement = freeze_json(materialize_json(original))
    assert replacement == original
    assert replacement is not original
    object.__setattr__(manifest, "_projection", replacement)

    with pytest.raises((TypeError, ValueError), match="integrity|Manifest|manifest|seal"):
        manifest.to_dict()


def test_provider_materialization_source_gate_tracks_aliases_and_single_operation() -> None:
    contracts_path = PRODUCTION_ROOT / "ai" / "tool_runtime" / "contracts.py"
    catalog_path = PRODUCTION_ROOT / "ai" / "tool_runtime" / "catalog.py"
    contracts_tree = ast.parse(contracts_path.read_text(encoding="utf-8"))
    catalog_tree = ast.parse(catalog_path.read_text(encoding="utf-8"))

    module_operations = [
        node
        for node in contracts_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "materialize_provider_payloads"
    ]
    catalog_classes = [
        node
        for node in catalog_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ToolCatalog"
    ]
    assert len(module_operations) == 1
    assert len(catalog_classes) == 1
    catalog_delegates = [
        node
        for node in catalog_classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "materialize_provider_payloads"
    ]
    assert len(catalog_delegates) == 1
    delegate_calls = [
        node
        for node in ast.walk(catalog_delegates[0])
        if isinstance(node, ast.Call) and _terminal(node.func) == "materialize_provider_payloads"
    ]
    assert len(delegate_calls) == 1

    provider_contract = next(
        node
        for node in contracts_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProviderToolContract"
    )
    assert not {
        "materialize_payload",
        "materialize_parameters",
    }.intersection(
        node.name
        for node in provider_contract.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    dict_subclasses = {
        node.name
        for node in contracts_tree.body
        if isinstance(node, ast.ClassDef) and any(_terminal(base) == "dict" for base in node.bases)
    }
    provider_query_methods = [
        node
        for node in provider_contract.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"payload", "parameters"}
    ]
    assert not any(
        isinstance(node, ast.Name) and node.id in dict_subclasses
        for method in provider_query_methods
        for node in ast.walk(method)
    )

    operation = module_operations[0]
    decoder_calls = [
        _terminal(node.func)
        for node in ast.walk(operation)
        if isinstance(node, ast.Call)
        and (_terminal(node.func) or "").startswith("_")
        and "provider" in (_terminal(node.func) or "").lower()
    ]
    assert len(set(decoder_calls)) == 1
    decoder_name = decoder_calls[0]
    for node in ast.walk(contracts_tree):
        if not isinstance(node, ast.Call) or _terminal(node.func) != decoder_name:
            continue
        ancestors = [
            owner
            for owner in ast.walk(contracts_tree)
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node in tuple(ast.walk(owner))
        ]
        assert any(
            owner.name in {decoder_name, "materialize_provider_payloads"} for owner in ancestors
        )

    private_provider_names = {
        "_payload_snapshot",
        "_parameters_snapshot",
        decoder_name,
    }
    violations: list[str] = []
    for path in _provider_gate_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path != contracts_path:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name in private_provider_names for alias in node.names
                ):
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        " imports private Provider state"
                    )
                elif isinstance(node, ast.Name) and node.id in private_provider_names:
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        f" accesses private Provider state {node.id}"
                    )
                elif isinstance(node, ast.Attribute) and node.attr in private_provider_names:
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                        f" accesses private Provider state {node.attr}"
                    )
        violations.extend(_ProviderProvenanceGate(path, tree).run())

    required_materializer_callers = (
        PRODUCTION_ROOT / "ai" / "client.py",
        PRODUCTION_ROOT / "context_projector" / "gateway.py",
        PRODUCTION_ROOT / "context_projector" / "projector.py",
        PRODUCTION_ROOT / "context_projector" / "selector.py",
    )
    for path in required_materializer_callers:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.Call) and _terminal(node.func) == "materialize_provider_payloads"
            for node in ast.walk(tree)
        ), path.relative_to(ROOT).as_posix()

    assert violations == []


def test_catalog_precompiles_schema_once_and_returns_detached_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_module = importlib.import_module("offerpilot.ai.tool_runtime.catalog")
    original_compile = catalog_module.compile_tool_schema
    compiled: list[dict[str, Any]] = []

    def spy_compile(schema: Any) -> Any:
        compiled.append(copy.deepcopy(schema))
        return original_compile(schema)

    monkeypatch.setattr(catalog_module, "compile_tool_schema", spy_compile)
    spec = synthetic_tool_spec("synthetic_precompiled_schema")
    catalog = _catalog((spec,))
    first = catalog.validator_for(spec.name)
    first.schema["description"] = "mutated detached validator"
    second = catalog.validator_for(spec.name)

    assert len(compiled) == 1
    assert "description" not in second.schema


def test_exact_four_required_undo_bindings_and_complete_presentation_bindings() -> None:
    specs = MODEL_TOOL_CATALOG.specs
    actual_required = {
        spec.name
        for spec in specs
        if isinstance(spec.metadata.operation, WriteOperationMetadataV1)
        and spec.metadata.operation.undo_policy is UndoPolicy.REQUIRED
    }
    assert actual_required == REQUIRED_UNDO
    for spec in specs:
        assert spec.presentation is not None
        assert spec.presentation.implementation_id
        assert spec.presentation.confirmation_description.__name__ != "<lambda>"
        assert spec.presentation.pending_details_projector.__name__ != "<lambda>"
        assert spec.presentation.success_summary_projector.__name__ != "<lambda>"
        if spec.name in REQUIRED_UNDO:
            assert spec.undo_builder_binding is not None
            assert spec.undo_builder_binding.descriptor is spec.metadata.operation
            assert spec.undo_builder_binding.implementation_id == (
                spec.metadata.operation.undo_builder_id
            )
            assert inspect.isfunction(spec.undo_builder_binding.capture_seed)
            assert inspect.isfunction(spec.undo_builder_binding.build_undo)
            assert spec.undo_builder_binding.capture_seed.__name__ != "<lambda>"
            assert spec.undo_builder_binding.build_undo.__name__ != "<lambda>"
        else:
            assert spec.undo_builder_binding is None


def test_transport_result_projection_remains_publicly_compatible() -> None:
    spec = synthetic_tool_spec("synthetic_transport")
    outcome = ToolSuccess(result={"ok": True})
    record = SimpleNamespace(
        prepared=SimpleNamespace(tool_call_id="transport-call"),
        outcome=outcome,
    )

    assert project_transport_event(spec, record) == {
        "tool_call_id": "transport-call",
        "tool_name": "synthetic_transport",
        "status": "success",
        "summary": "{'ok': True}",
        "evidence": [],
        "affected_resources": [],
        "changed_entities": [],
    }
