from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offerpilot.ai.tool_runtime.contracts import (
    materialize_provider_payloads,
)
from offerpilot.context_projector.budget import (
    BUDGET_POLICY_VERSION,
    CANONICAL_MESSAGES_BYTE_CAP,
    COMBINED_SURFACE_BYTE_CAP,
    PROVIDER_TOOLS_BYTE_CAP,
    ProviderBudget,
    canonical_messages,
    conservative_units,
    optional_shares,
    surface_input_limit,
)
from offerpilot.context_projector.contracts import (
    CONTRIBUTOR_ORDER,
    ContributorResult,
    FrozenMessage,
    FrozenModelSurface,
    FrozenSource,
    ProjectionError,
    RuntimeSurfaceAudit,
    RuntimeSourceAudit,
    canonical_json,
    sha256_hex,
)
from offerpilot.context_projector.history import (
    TurnGroup,
    group_history,
    rank_history,
    validate_message_integrity,
)
from offerpilot.context_projector.selector import ToolSelectionResult, ToolSelectionSignals


@dataclass(frozen=True)
class ProjectionRequest:
    model_call_id: str
    contributors: tuple[ContributorResult, ...]
    history: tuple[FrozenMessage, ...]
    tool_signals: ToolSelectionSignals
    provider_budgets: tuple[ProviderBudget, ...]
    selection: ToolSelectionResult
    sources: tuple[FrozenSource, ...] = ()
    provider_surface_build_identity: object | None = None


class ModelSurfaceProjector:
    """Pure, deterministic model-surface projection."""

    def project(self, request: ProjectionRequest) -> FrozenModelSurface:
        if not request.model_call_id:
            raise ProjectionError("model_call_id_required")
        if len(request.sources) > 8 or sum(len(source.chunks) for source in request.sources) > 64:
            raise ProjectionError("source_audit_limit_exceeded")
        if any(len(source.chunks) > 32 for source in request.sources):
            raise ProjectionError("source_chunk_limit_exceeded")
        contributors = self._validate_contributors(request.contributors)
        selection = request.selection
        if type(selection) is not ToolSelectionResult:
            raise ProjectionError("tool_selection_result_required")
        selection._ensure_integrity()
        if len(selection.provider_envelope_fingerprint) != 64:
            raise ProjectionError("preselected_surface_mismatch")
        tool_payloads = materialize_provider_payloads(selection.provider_contracts)
        tool_bytes = canonical_json(tool_payloads)
        if len(tool_bytes) > PROVIDER_TOOLS_BYTE_CAP:
            raise ProjectionError("provider_tools_byte_cap_exceeded")

        input_limit = surface_input_limit(request.provider_budgets)
        mandatory = self._mandatory_messages(contributors)
        mandatory_bytes = canonical_messages(mandatory)
        fixed_assembly_cost = 64
        remainder = (
            input_limit
            - len(tool_bytes)
            - conservative_units(mandatory_bytes)
            - fixed_assembly_cost
        )
        if remainder < 0 and any(message.role == "tool" for message in mandatory):
            without_tool_results = tuple(
                message for message in mandatory if message.role != "tool"
            )
            baseline_remainder = (
                input_limit
                - len(tool_bytes)
                - conservative_units(canonical_messages(without_tool_results))
                - fixed_assembly_cost
            )
            if baseline_remainder >= 0:
                raise ProjectionError("mandatory_tool_result_over_budget")
        shares, shared_pool = optional_shares(remainder)

        scope_messages = self._messages_for(contributors, "current_scope") + self._messages_for(
            contributors, "request_page_context"
        )
        attachment_messages = self._messages_for(contributors, "request_attachments")
        groups = group_history(request.history, legacy_orphan_compat=True)
        ranked_groups = rank_history(groups, current_request=request.tool_signals.current_request)
        selected_scope, remaining_scope, unused_scope = self._fit_items(
            scope_messages, shares["scope"], self._message_cost
        )
        selected_attachments, remaining_attachments, unused_attachments = self._fit_items(
            attachment_messages, shares["attachments"], self._message_cost
        )
        selected_groups, remaining_groups, unused_history = self._fit_items(
            ranked_groups, shares["history"], self._group_cost
        )
        shared = shared_pool + unused_scope + unused_attachments + unused_history
        shared_selected, _ = self._round_robin_shared(
            (remaining_scope, remaining_attachments, remaining_groups), shared
        )
        selected_scope = (*selected_scope, *shared_selected[0])
        selected_attachments = (*selected_attachments, *shared_selected[1])
        # Policy/data envelopes are security-atomic. A partially fitted request
        # envelope is omitted instead of exposing untrusted data without its
        # adjacent fixed policy (or a policy that describes absent data).
        page_messages = self._messages_for(contributors, "request_page_context")
        if page_messages and not all(message in selected_scope for message in page_messages):
            selected_scope = tuple(
                message for message in selected_scope if message not in page_messages
            )
        if attachment_messages and not all(
            message in selected_attachments for message in attachment_messages
        ):
            selected_attachments = ()
        selected_groups = (*selected_groups, *shared_selected[2])
        selected_groups = tuple(
            sorted(
                selected_groups, key=lambda group: (group.first_message_id, group.last_message_id)
            )
        )
        selected_history = tuple(message for group in selected_groups for message in group.messages)

        # Contributor order controls assembly. History is inserted at its declared slot.
        assembled: list[FrozenMessage] = []
        for name in CONTRIBUTOR_ORDER:
            if name == "current_scope":
                assembled.extend(selected_scope)
            elif name == "request_page_context":
                continue
            elif name == "request_attachments":
                assembled.extend(selected_attachments)
            elif name == "conversation_history":
                assembled.extend(selected_history)
            else:
                assembled.extend(self._messages_for(contributors, name))
        messages = tuple(assembled)
        validate_message_integrity(messages)
        message_bytes = canonical_messages(messages)
        combined = len(message_bytes) + len(tool_bytes)
        if len(message_bytes) > CANONICAL_MESSAGES_BYTE_CAP:
            raise ProjectionError("canonical_messages_byte_cap_exceeded")
        if combined > COMBINED_SURFACE_BYTE_CAP:
            raise ProjectionError("combined_surface_byte_cap_exceeded")
        estimated = conservative_units(message_bytes) + conservative_units(tool_bytes)
        for budget in request.provider_budgets:
            if estimated > budget.input_limit:
                raise ProjectionError("provider_context_window_exceeded")

        canonical_surface = canonical_json(
            {
                "budget_policy_version": BUDGET_POLICY_VERSION,
                "messages": [message.canonical_value() for message in messages],
                "tools": tool_payloads,
            }
        )
        fingerprint = "sha256:" + sha256_hex(canonical_surface)
        audit = RuntimeSurfaceAudit(
            budget_policy_version=BUDGET_POLICY_VERSION,
            contributor_statuses=tuple(
                (name, contributors[name].status) for name in CONTRIBUTOR_ORDER
            ),
            selected_history_group_ids=tuple(group.group_id for group in selected_groups),
            selected_tool_names=selection.selected_names,
            source_fingerprints=tuple(
                source.content_revision_fingerprint for source in request.sources
            ),
            estimated_input_units=estimated,
            canonical_message_bytes=len(message_bytes),
            canonical_tool_bytes=len(tool_bytes),
            truncated=(
                len(selected_scope) != len(scope_messages)
                or len(selected_attachments) != len(attachment_messages)
                or len(selected_groups) != len(groups)
            ),
            source_records=tuple(
                RuntimeSourceAudit(
                    source.kind,
                    source.revision_identity,
                    source.content_revision_fingerprint,
                    source.chunks,
                )
                for source in request.sources
            ),
            signals=(
                ("fallback_all_tools",)
                if selection.full_catalog_fallback
                else ("catalog_domain_union", "catalog_dependency_closure")
            ),
        )
        return FrozenModelSurface(
            model_call_id=request.model_call_id,
            messages=messages,
            tools=selection.provider_contracts,
            runtime_surface_fingerprint=fingerprint,
            provider_candidate_count=len(request.provider_budgets),
            audit=audit,
            provider_surface_build_identity=request.provider_surface_build_identity,
        )

    @staticmethod
    def _validate_contributors(
        raw: tuple[ContributorResult, ...],
    ) -> dict[str, ContributorResult]:
        if tuple(item.name for item in raw) != CONTRIBUTOR_ORDER:
            raise ProjectionError("contributor_order_mismatch")
        contributors = {item.name: item for item in raw}
        for disabled in ("confirmed_memory", "knowledge_context", "older_conversation_summary"):
            if contributors[disabled].status != "disabled":
                raise ProjectionError("deferred_contributor_not_disabled")
        for mandatory in ("static_policy", "current_request"):
            if contributors[mandatory].status != "ready" or not contributors[mandatory].messages:
                raise ProjectionError("mandatory_contributor_unavailable")
        return contributors

    @staticmethod
    def _messages_for(
        contributors: dict[str, ContributorResult], name: str
    ) -> tuple[FrozenMessage, ...]:
        contributor = contributors[name]
        return contributor.messages if contributor.status == "ready" else ()

    def _mandatory_messages(
        self, contributors: dict[str, ContributorResult]
    ) -> tuple[FrozenMessage, ...]:
        return (
            *self._messages_for(contributors, "static_policy"),
            *self._messages_for(contributors, "active_control"),
            *self._messages_for(contributors, "current_request"),
        )

    @staticmethod
    def _fit_items(items: tuple[Any, ...], budget: int, cost_of: Any) -> tuple[Any, Any, int]:
        selected: list[Any] = []
        remaining: list[Any] = []
        used = 0
        for item in items:
            cost = cost_of(item)
            if used + cost <= budget:
                selected.append(item)
                used += cost
            else:
                remaining.append(item)
        return tuple(selected), tuple(remaining), max(0, budget - used)

    @staticmethod
    def _message_cost(message: FrozenMessage) -> int:
        return len(canonical_json(message.canonical_value())) + 1

    @staticmethod
    def _group_cost(group: TurnGroup) -> int:
        return len(group.canonical_bytes) + 1

    @classmethod
    def _round_robin_shared(
        cls, queues: tuple[tuple[Any, ...], ...], budget: int
    ) -> tuple[tuple[tuple[Any, ...], ...], int]:
        pending = [list(queue) for queue in queues]
        selected: list[list[Any]] = [[] for _ in queues]
        costs = (cls._message_cost, cls._message_cost, cls._group_cost)
        while any(pending):
            for index, queue in enumerate(pending):
                if not queue:
                    continue
                item = queue.pop(0)
                cost = costs[index](item)
                if cost <= budget:
                    selected[index].append(item)
                    budget -= cost
        return tuple(tuple(items) for items in selected), budget
