from __future__ import annotations

import ast
import importlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import CheckConstraint

from offerpilot.ai.tool_runtime import metadata as metadata_module
from offerpilot.ai.tool_runtime.catalog import compile_tool_metadata_manifest
from offerpilot.ai.tool_runtime.metadata import ToolMetadataBundleV1
from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.models import WriteOperation


_TEST_TOOL_CATALOG = build_model_tool_catalog()

BASELINE_COMMIT = "0c10e05e256eb757d5f89a8b009dcea193f2fc78"
MANIFEST_CONSTRAINT = "ck_write_operations_manifest"
UNDO_POLICY_CONSTRAINT = "ck_write_operations_undo_policy"
UNDO_BYTES_CONSTRAINT = "ck_write_operations_undo_bytes"

# Independently transcribed from the fixed baseline.  These literals are deliberately not
# derived from the current model, Bundle, Operation Port, or another production registry.
BASELINE_MANIFEST_SQL = (
    "(operation_role = 'primary' AND adapter_kind = 'typed' AND tool_name IN "
    "('create_application','update_application_status','create_application_event',"
    "'update_application_event','delete_application_event','add_note','update_note',"
    "'delete_note','update_offer','save_offer_assessment','resume_update_career_intent',"
    "'resume_rewrite_highlight')) OR "
    "(operation_role = 'primary' AND adapter_kind = 'legacy_deterministic' AND tool_name IN "
    "('save_application_jd_version','create_application_submission_snapshot',"
    "'record_application_outcome')) OR "
    "(operation_role = 'compensation' AND adapter_kind = 'compensation' AND tool_name IN "
    "('undo:update_application_status','undo:create_application',"
    "'undo:create_application_event','undo:add_note'))"
)
BASELINE_UNDO_POLICY_SQL = (
    "status <> 'committed' OR "
    "(operation_role = 'primary' AND tool_name IN "
    "('create_application','update_application_status','create_application_event','add_note') "
    "AND undo_json IS NOT NULL) OR "
    "((operation_role = 'compensation' OR tool_name NOT IN "
    "('create_application','update_application_status','create_application_event','add_note')) "
    "AND undo_json IS NULL)"
)
BASELINE_UNDO_BYTES_SQL = "undo_json IS NULL OR length(CAST(undo_json AS BLOB)) <= 65536"

TYPED_WRITE_NAMES = (
    "create_application",
    "update_application_status",
    "create_application_event",
    "update_application_event",
    "delete_application_event",
    "add_note",
    "update_note",
    "delete_note",
    "update_offer",
    "save_offer_assessment",
    "resume_update_career_intent",
    "resume_rewrite_highlight",
)
LEGACY_WRITE_NAMES = (
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome",
)
COMPENSATION_NAMES = (
    "undo:update_application_status",
    "undo:create_application",
    "undo:create_application_event",
    "undo:add_note",
)
REQUIRED_UNDO_NAMES = (
    "create_application",
    "update_application_status",
    "create_application_event",
    "add_note",
)

PublishedRoute = tuple[str, str, str]


def _normalized_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _current_checks() -> dict[str, str]:
    return {
        str(constraint.name): _normalized_sql(str(constraint.sqltext))
        for constraint in WriteOperation.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _all_published_routes() -> frozenset[PublishedRoute]:
    return frozenset(
        [
            *(("primary", "typed", name) for name in TYPED_WRITE_NAMES),
            *(("primary", "legacy_deterministic", name) for name in LEGACY_WRITE_NAMES),
            *(("compensation", "compensation", name) for name in COMPENSATION_NAMES),
        ]
    )


def _create_published_schema(
    connection: sqlite3.Connection,
    *,
    manifest_sql: str,
    undo_policy_sql: str,
    undo_bytes_sql: str,
) -> None:
    connection.execute(
        f"""
        CREATE TABLE published_write_operations (
            operation_role TEXT NOT NULL,
            adapter_kind TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            undo_json TEXT,
            CONSTRAINT {MANIFEST_CONSTRAINT} CHECK ({manifest_sql}),
            CONSTRAINT {UNDO_POLICY_CONSTRAINT} CHECK ({undo_policy_sql}),
            CONSTRAINT {UNDO_BYTES_CONSTRAINT} CHECK ({undo_bytes_sql})
        )
        """
    )


def _accepted(
    connection: sqlite3.Connection,
    route: PublishedRoute,
    *,
    status: str = "proposed",
    undo_json: str | None = None,
) -> bool:
    try:
        connection.execute(
            "INSERT INTO published_write_operations "
            "(operation_role, adapter_kind, tool_name, status, undo_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (*route, status, undo_json),
        )
    except sqlite3.IntegrityError:
        return False
    connection.execute("DELETE FROM published_write_operations")
    return True


@pytest.fixture
def published_schemas() -> Iterable[tuple[sqlite3.Connection, sqlite3.Connection]]:
    checks = _current_checks()
    baseline = sqlite3.connect(":memory:")
    current = sqlite3.connect(":memory:")
    _create_published_schema(
        baseline,
        manifest_sql=BASELINE_MANIFEST_SQL,
        undo_policy_sql=BASELINE_UNDO_POLICY_SQL,
        undo_bytes_sql=BASELINE_UNDO_BYTES_SQL,
    )
    _create_published_schema(
        current,
        manifest_sql=checks[MANIFEST_CONSTRAINT],
        undo_policy_sql=checks[UNDO_POLICY_CONSTRAINT],
        undo_bytes_sql=checks[UNDO_BYTES_CONSTRAINT],
    )
    try:
        yield baseline, current
    finally:
        baseline.close()
        current.close()


def test_published_operation_constraint_names_and_sql_match_fixed_baseline() -> None:
    checks = _current_checks()

    assert BASELINE_COMMIT == "0c10e05e256eb757d5f89a8b009dcea193f2fc78"
    assert checks[MANIFEST_CONSTRAINT] == _normalized_sql(BASELINE_MANIFEST_SQL)
    assert checks[UNDO_POLICY_CONSTRAINT] == _normalized_sql(BASELINE_UNDO_POLICY_SQL)
    assert checks[UNDO_BYTES_CONSTRAINT] == _normalized_sql(BASELINE_UNDO_BYTES_SQL)


@pytest.mark.parametrize("route", sorted(_all_published_routes()))
def test_baseline_and_current_sqlite_schemas_accept_exact_published_routes(
    published_schemas: tuple[sqlite3.Connection, sqlite3.Connection],
    route: PublishedRoute,
) -> None:
    baseline, current = published_schemas

    assert _accepted(baseline, route)
    assert _accepted(current, route)


@pytest.mark.parametrize(
    "route",
    [
        ("primary", "typed", "get_application"),
        ("primary", "typed", "unknown_operation"),
        ("primary", "typed", "Create_Application"),
        ("primary", "legacy_deterministic", TYPED_WRITE_NAMES[0]),
        ("primary", "typed", LEGACY_WRITE_NAMES[0]),
        ("primary", "compensation", COMPENSATION_NAMES[0]),
        ("compensation", "compensation", TYPED_WRITE_NAMES[0]),
        ("primary", "compensation", "unknown_operation"),
        ("compensation", "typed", COMPENSATION_NAMES[0]),
        ("compensation", "legacy_deterministic", LEGACY_WRITE_NAMES[0]),
    ],
)
def test_baseline_and_current_sqlite_schemas_reject_the_same_invalid_routes(
    published_schemas: tuple[sqlite3.Connection, sqlite3.Connection],
    route: PublishedRoute,
) -> None:
    baseline, current = published_schemas

    assert not _accepted(baseline, route)
    assert not _accepted(current, route)


@pytest.mark.parametrize("tool_name", REQUIRED_UNDO_NAMES)
def test_required_undo_policy_is_byte_for_byte_compatible(
    published_schemas: tuple[sqlite3.Connection, sqlite3.Connection],
    tool_name: str,
) -> None:
    baseline, current = published_schemas
    route = ("primary", "typed", tool_name)

    assert not _accepted(baseline, route, status="committed")
    assert not _accepted(current, route, status="committed")
    assert _accepted(baseline, route, status="committed", undo_json="{}")
    assert _accepted(current, route, status="committed", undo_json="{}")


@pytest.mark.parametrize(
    "route",
    [
        ("primary", "typed", "update_note"),
        ("primary", "legacy_deterministic", LEGACY_WRITE_NAMES[0]),
        ("compensation", "compensation", COMPENSATION_NAMES[0]),
    ],
)
def test_non_required_routes_forbid_committed_undo(
    published_schemas: tuple[sqlite3.Connection, sqlite3.Connection],
    route: PublishedRoute,
) -> None:
    baseline, current = published_schemas

    assert _accepted(baseline, route, status="committed")
    assert _accepted(current, route, status="committed")
    assert not _accepted(baseline, route, status="committed", undo_json="{}")
    assert not _accepted(current, route, status="committed", undo_json="{}")


def test_published_payload_limits_use_sqlite_blob_length_semantics(
    published_schemas: tuple[sqlite3.Connection, sqlite3.Connection],
) -> None:
    baseline, current = published_schemas
    route = ("primary", "typed", "create_application")
    at_limit = "界" * (65535 // len("界".encode("utf-8")))
    over_limit = at_limit + "界"

    assert len(at_limit) < 65536
    assert len(at_limit.encode("utf-8")) == 65535
    assert len(over_limit.encode("utf-8")) == 65538
    for connection in (baseline, current):
        assert connection.execute(
            "SELECT length(?), length(CAST(? AS BLOB))", (over_limit, over_limit)
        ).fetchone() == (len(over_limit), 65538)
        assert _accepted(connection, route, undo_json=at_limit)
        assert not _accepted(connection, route, undo_json=over_limit)


def _required_attr(value: object, name: str) -> Any:
    result = getattr(value, name, None)
    assert result is not None, f"Task 6 Operation Port API is missing: {name}"
    return result


class _LegacyIssuerProbe:
    def __init__(self, boundary: object) -> None:
        self.bundle_instance_token = boundary.bundle_instance_token  # type: ignore[attr-defined]
        self.registry_token = object()
        self.route_handle = object()
        self.binding = boundary.ordered_adapter_bindings[0]  # type: ignore[attr-defined]

    def require_route(self, route_handle: object) -> object:
        if route_handle is not self.route_handle:
            raise ValueError("Legacy route provenance mismatch")
        return self.binding


def test_operation_port_projection_equals_published_sqlite_allow_set() -> None:
    port_type = _required_attr(metadata_module, "ToolOperationMetadataPort")
    compensation_module = importlib.import_module("offerpilot.pilot_runtime.compensation")
    prepare_components = _required_attr(
        compensation_module,
        "prepare_compensation_handler_components",
    )
    components = prepare_components()
    manifest = compile_tool_metadata_manifest(_TEST_TOOL_CATALOG.specs)
    projection = manifest.to_dict()
    bundle = ToolMetadataBundleV1(
        typed_catalog=_TEST_TOOL_CATALOG,
        manifest=manifest,
        legacy_boundary=cast(dict[str, object], projection["legacy_boundary"]),
        compensation=components.metadata_projection(),
    )
    registry = components.bind(bundle.compensation_view())
    legacy_issuer = _LegacyIssuerProbe(bundle.legacy_boundary())
    port = port_type(
        operation_view=bundle.operation_view(),
        legacy_boundary=bundle.legacy_boundary(),
        compensation_view=bundle.compensation_view(),
        compensation_registry=registry,
        legacy_route_issuer_port=legacy_issuer,
    )

    typed_entries = tuple(port.typed_primary_entries)
    legacy_entries = tuple(port.legacy_primary_entries)
    compensation_entries = tuple(port.compensation_entries)
    actual_routes = frozenset(
        (entry.operation_role, entry.adapter_kind, entry.operation_name)
        for entry in (*typed_entries, *legacy_entries, *compensation_entries)
        if entry.result_contract is not None
    )

    assert len(typed_entries) == 25
    assert actual_routes == _all_published_routes()


def test_completed_production_bundle_operation_port_equals_published_allow_set() -> None:
    composition = importlib.import_module("offerpilot.pilot_runtime.composition")
    factory = getattr(composition, "build_production_tool_metadata_components", None)
    assert callable(factory), "Task 9 must expose the production metadata component factory"
    legacy_route = importlib.import_module("offerpilot.pilot_runtime.legacy_route")
    verifier_builder = getattr(
        legacy_route,
        "build_legacy_pending_identity_verifier_port",
        None,
    )
    assert callable(verifier_builder)
    verifier = verifier_builder(
        backend=SimpleNamespace(
            read_snapshot=lambda *_args, **_kwargs: {},
            locked_recheck=lambda *_args, **_kwargs: {},
            claim_cas=lambda *_args, **_kwargs: {},
        ),
        ledger_key=SimpleNamespace(
            key_id="00000000-0000-0000-0000-000000000001",
            secret=b"k" * 32,
        ),
    )

    components = factory(pending_identity_verifier_port=verifier)
    port = components.operation_port
    bundle = components.bundle
    actual_routes = frozenset(
        (entry.operation_role, entry.adapter_kind, entry.operation_name)
        for entry in (
            *port.typed_primary_entries,
            *port.legacy_primary_entries,
            *port.compensation_entries,
        )
        if entry.result_contract is not None
    )

    assert port.bundle_instance_token is bundle.bundle_instance_token
    assert port.bundle_instance_token is bundle.operation_view().bundle_instance_token
    assert port.bundle_instance_token is bundle.legacy_boundary().bundle_instance_token
    assert port.bundle_instance_token is bundle.compensation_view().bundle_instance_token
    assert actual_routes == _all_published_routes()


def test_coordinator_and_repository_do_not_parse_published_check_sql() -> None:
    project_root = Path(__file__).resolve().parents[2]
    owners = (
        project_root / "src" / "offerpilot" / "ai" / "write_operations.py",
        project_root / "src" / "offerpilot" / "repositories" / "chat.py",
    )
    forbidden_text = {
        MANIFEST_CONSTRAINT.casefold(),
        UNDO_POLICY_CONSTRAINT.casefold(),
        "sqlite_master",
        "pragma table_info",
        "create table write_operations",
    }

    for path in owners:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        strings = {
            node.value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "CheckConstraint" not in names, path
        assert "sqltext" not in names, path
        assert all(marker not in literal for marker in forbidden_text for literal in strings), path
