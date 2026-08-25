from __future__ import annotations

from offerpilot.ai.tool_runtime.catalog import ToolCatalog, authority_manifest_for_specs
from offerpilot.ai.tool_runtime.protocol_seals import verify_provider_boundary
from offerpilot.ai.tool_specs.application_events import application_event_specs
from offerpilot.ai.tool_specs.applications import application_specs
from offerpilot.ai.tool_specs.jd_analyses import jd_analysis_specs
from offerpilot.ai.tool_specs.notes import note_specs
from offerpilot.ai.tool_specs.offers import offer_specs
from offerpilot.ai.tool_specs.resumes import resume_specs


def build_model_tool_catalog() -> ToolCatalog:
    specs = (
        *application_specs(),
        *application_event_specs(),
        *note_specs(),
        *offer_specs(),
        *resume_specs(),
        *jd_analysis_specs(),
    )
    names = tuple(spec.name for spec in specs)
    authority_manifest = authority_manifest_for_specs(specs, strict=True)
    catalog = ToolCatalog(
        specs,
        expected_names=names,
        authority_manifest=authority_manifest,
    )
    verify_provider_boundary(catalog.materialize_provider_payloads())
    return catalog


MODEL_TOOL_CATALOG = build_model_tool_catalog()
MODEL_TOOL_NAMES = tuple(spec.name for spec in MODEL_TOOL_CATALOG.specs)


__all__ = ["MODEL_TOOL_CATALOG", "MODEL_TOOL_NAMES", "build_model_tool_catalog"]
