from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable

from offerpilot.ai.tool_runtime.contracts import TransientToolRuntimeValue
from offerpilot.ai.types import Assistant
from offerpilot.context_projector.contracts import FrozenModelSurface, ProjectionError


@dataclass(frozen=True)
class ModelCallSurfaceBinding:
    model_call_id: str
    runtime_surface_fingerprint: str
    exposed_tool_names: frozenset[str]
    provider_candidate_count: int

    @classmethod
    def from_surface(cls, surface: FrozenModelSurface) -> ModelCallSurfaceBinding:
        return cls(
            surface.model_call_id,
            surface.runtime_surface_fingerprint,
            frozenset(tool.name for tool in surface.tools),
            surface.provider_candidate_count,
        )

    def validate_response(
        self,
        response: BoundProviderResponse,
        *,
        attempt_validator: Callable[[str], bool],
    ) -> Assistant:
        assistant = self.validate_provenance(
            response,
            attempt_validator=attempt_validator,
        )
        for call in assistant.tool_calls:
            if call.name not in self.exposed_tool_names:
                raise ProjectionError("unknown_tool")
        return assistant

    def validate_provenance(
        self,
        response: BoundProviderResponse,
        *,
        attempt_validator: Callable[[str], bool],
    ) -> Assistant:
        if response.model_call_id != self.model_call_id:
            raise ProjectionError("provider_response_model_call_mismatch")
        if response.runtime_surface_fingerprint != self.runtime_surface_fingerprint:
            raise ProjectionError("provider_response_surface_mismatch")
        if response.candidate_ordinal >= self.provider_candidate_count:
            raise ProjectionError("provider_response_candidate_mismatch")
        if not attempt_validator(response.provider_attempt_id):
            raise ProjectionError("provider_response_attempt_mismatch")
        return response.response


class _BoundResponseSerializationGuard:
    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise TypeError("transient tool runtime value cannot be serialized")


_BOUND_RESPONSE_SERIALIZATION_GUARD = _BoundResponseSerializationGuard()


@dataclass(frozen=True, repr=False)
class BoundProviderResponse(TransientToolRuntimeValue):
    model_call_id: str
    candidate_ordinal: int
    provider_attempt_id: str
    runtime_surface_fingerprint: str
    response: Assistant = field(repr=False)
    _serialization_guard: object = field(
        default=_BOUND_RESPONSE_SERIALIZATION_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            type(self.model_call_id) is not str
            or not self.model_call_id
            or type(self.candidate_ordinal) is not int
            or self.candidate_ordinal < 0
            or type(self.provider_attempt_id) is not str
            or not self.provider_attempt_id
            or type(self.runtime_surface_fingerprint) is not str
            or len(self.runtime_surface_fingerprint) != 64
            or not isinstance(self.response, Assistant)
        ):
            raise ProjectionError("invalid_bound_provider_response")

    def __repr__(self) -> str:
        return "<BoundProviderResponse transient>"
