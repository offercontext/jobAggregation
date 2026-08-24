from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

from offerpilot.ai.control import AgentLoopControlError
from offerpilot.ai.tool_runtime.contracts import ProviderToolContract
from offerpilot.ai.tool_authority.composition import require_authority_phase
from offerpilot.ai.tool_authority.contracts import (
    AuthorityUse,
    ProviderInvocationIdentity,
    ProviderSurfaceBuildIdentity,
    SegmentExecutionAuthority,
)
from offerpilot.ai.types import Assistant, Message
from offerpilot.config import AIProviderProfile
from offerpilot.context_projector.binding import BoundProviderResponse, ModelCallSurfaceBinding
from offerpilot.context_projector.budget import (
    ADAPTER_REQUEST_BODY_BYTE_CAP,
    DEFAULT_OUTPUT_RESERVE,
    PROVIDER_FRAMING_RESERVE,
    ProviderBudget,
    canonical_messages,
    conservative_units,
)
from offerpilot.context_projector.contracts import (
    FrozenModelSurface,
    ProjectionError,
    canonical_json,
    sha256_hex,
)

ENDPOINT_NORMALIZATION_VERSION = "provider-endpoint-v1"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")


def normalize_provider_endpoint(value: str) -> str:
    if not value or "\\" in value or _CONTROL.search(value):
        raise ProjectionError("invalid_provider_endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProjectionError("invalid_provider_endpoint") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ProjectionError("invalid_provider_endpoint_scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ProjectionError("invalid_provider_endpoint_authority")
    if parsed.query or parsed.fragment or "%" in parsed.hostname:
        raise ProjectionError("invalid_provider_endpoint_components")
    path = parsed.path.rstrip("/")
    if (
        any(segment in {".", ".."} for segment in path.split("/"))
        or "//" in path
        or _PATH.fullmatch(path) is None
    ):
        raise ProjectionError("invalid_provider_endpoint_path")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    elif _HOST.fullmatch(host) is None:
        raise ProjectionError("invalid_provider_endpoint_authority")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), authority, path, "", ""))


@dataclass(frozen=True)
class FrozenProviderCandidate:
    provider_id: str
    provider_kind: str
    model: str
    endpoint: str
    context_window: int
    output_reserve: int
    supports_json_schema: bool
    credential: str = field(repr=False)

    @classmethod
    def freeze(cls, profile: AIProviderProfile) -> FrozenProviderCandidate:
        context_window = 32_768 if profile.context_window == 0 else profile.context_window
        output_reserve = (
            DEFAULT_OUTPUT_RESERVE if profile.max_output_tokens == 0 else profile.max_output_tokens
        )
        # Constructing the budget here validates every component before any
        # frozen chain or credential-bearing candidate can escape.
        ProviderBudget(context_window=context_window, output_reserve=output_reserve)
        return cls(
            provider_id=profile.id,
            provider_kind=profile.provider,
            model=profile.model,
            endpoint=normalize_provider_endpoint(profile.base_url),
            context_window=context_window,
            output_reserve=output_reserve,
            supports_json_schema=profile.supports_json_schema,
            credential=profile.api_key,
        )

    def budget(self) -> ProviderBudget:
        return ProviderBudget(
            context_window=self.context_window,
            output_reserve=self.output_reserve,
            framing_reserve=PROVIDER_FRAMING_RESERVE,
        )


@dataclass(frozen=True)
class FrozenProviderExecutionChain:
    candidates: tuple[FrozenProviderCandidate, ...]
    chain_fingerprint: str

    @classmethod
    def freeze(cls, profiles: list[AIProviderProfile]) -> FrozenProviderExecutionChain:
        candidates = tuple(
            FrozenProviderCandidate.freeze(profile)
            for profile in profiles
            if profile.enabled and profile.api_key
        )
        if not candidates:
            raise ProjectionError("provider_chain_empty")
        public = [
            {
                "provider_id": candidate.provider_id,
                "provider_kind": candidate.provider_kind,
                "model": candidate.model,
                "endpoint": candidate.endpoint,
                "context_window": candidate.context_window,
                "output_reserve": candidate.output_reserve,
                "supports_json_schema": candidate.supports_json_schema,
            }
            for candidate in candidates
        ]
        return cls(candidates, sha256_hex(canonical_json(public)))


CompleteOne = Callable[
    [FrozenProviderCandidate, list[Message], list[ProviderToolContract], dict[str, Any] | None],
    Assistant,
]
StreamOne = Callable[
    [FrozenProviderCandidate, list[Message], list[ProviderToolContract], Callable[[str], None]],
    Assistant,
]


class SingleCandidateAgentTransport:
    def __init__(self, complete_one: CompleteOne, stream_one: StreamOne):
        self._complete = complete_one
        self._stream = stream_one

    def complete_one(
        self,
        candidate: FrozenProviderCandidate,
        surface: FrozenModelSurface,
        response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        self._preflight(candidate, surface, response_format, stream=False)
        return self._complete(
            candidate, surface.thaw_messages(), list(surface.tools), response_format
        )

    def stream_one(
        self,
        candidate: FrozenProviderCandidate,
        surface: FrozenModelSurface,
        on_delta: Callable[[str], None],
    ) -> Assistant:
        self._preflight(candidate, surface, None, stream=True)
        return self._stream(candidate, surface.thaw_messages(), list(surface.tools), on_delta)

    @staticmethod
    def _preflight(
        candidate: FrozenProviderCandidate,
        surface: FrozenModelSurface,
        response_format: dict[str, Any] | None,
        *,
        stream: bool,
    ) -> None:
        normalized = normalize_provider_endpoint(candidate.endpoint)
        if normalized != candidate.endpoint:
            raise ProjectionError("provider_endpoint_changed")
        body = canonical_json(
            {
                "model": candidate.model,
                "messages": [message.canonical_value() for message in surface.messages],
                "tools": [dict(tool.payload) for tool in surface.tools],
                "response_format": response_format,
                "stream": stream,
            }
        )
        if len(body) > ADAPTER_REQUEST_BODY_BYTE_CAP:
            raise ProjectionError("adapter_request_body_byte_cap_exceeded")
        estimated = conservative_units(canonical_messages(surface.messages)) + conservative_units(
            canonical_json([dict(tool.payload) for tool in surface.tools])
        )
        if estimated > candidate.budget().input_limit:
            raise ProjectionError("adapter_context_window_exceeded")


class AgentProviderGatewaySession:
    def __init__(
        self, chain: FrozenProviderExecutionChain, transport: SingleCandidateAgentTransport
    ):
        self._chain = chain
        self._transport = transport
        self._attempts: set[str] = set()
        self._attempt_lock = Lock()

    def _begin_authorized_attempt(
        self,
        invocation_identity: ProviderInvocationIdentity,
        candidate_ordinal: int,
    ) -> str:
        context = invocation_identity.tool_context
        factory = getattr(context, "authority_factory", None)
        if factory is None:
            raise ProjectionError("provider_invocation_context_invalid")
        attempt_id = factory.issue_provider_attempt(
            invocation_identity,
            candidate_ordinal=candidate_ordinal,
        )
        if type(attempt_id) is not str or not attempt_id:
            raise ProjectionError("provider_attempt_identity_invalid")
        with self._attempt_lock:
            self._attempts.add(attempt_id)
        return attempt_id

    def bind_provider_surface(
        self,
        *,
        authority: SegmentExecutionAuthority,
        build_identity: ProviderSurfaceBuildIdentity,
        surface: FrozenModelSurface,
        model_call_surface_binding: ModelCallSurfaceBinding,
    ) -> ProviderInvocationIdentity:
        if type(authority) is not SegmentExecutionAuthority:
            raise ProjectionError("segment_authority_required")
        require_authority_phase(authority, AuthorityUse.PROVIDER_SURFACE_BUILD, build_identity)
        if surface.provider_surface_build_identity is not build_identity:
            raise ProjectionError("provider_surface_build_identity_mismatch")
        context = build_identity.tool_context
        factory = getattr(context, "authority_factory", None)
        if factory is None or getattr(context, "authority", None) is not authority:
            raise ProjectionError("provider_invocation_context_invalid")
        factory.register_frozen_surface(
            surface,
            surface_fingerprint=surface.runtime_surface_fingerprint,
            candidate_count=surface.provider_candidate_count,
            authority=authority,
            build_identity=build_identity,
        )
        factory.register_model_call_surface_binding(
            model_call_surface_binding,
            surface=surface,
            surface_fingerprint=surface.runtime_surface_fingerprint,
            authority=authority,
            build_identity=build_identity,
        )
        factory.register_gateway_session(
            self,
            authority,
            build_identity=build_identity,
            surface=surface,
            surface_fingerprint=surface.runtime_surface_fingerprint,
            model_call_surface_binding=model_call_surface_binding,
        )
        invocation = factory.create_provider_invocation_identity(
            build_identity,
            surface=surface,
            surface_fingerprint=surface.runtime_surface_fingerprint,
            model_call_surface_binding=model_call_surface_binding,
            gateway_session=self,
        )
        if type(invocation) is not ProviderInvocationIdentity:
            raise ProjectionError("provider_invocation_identity_required")
        return invocation

    def _validate_invocation(
        self,
        surface: FrozenModelSurface,
        invocation_identity: ProviderInvocationIdentity,
    ) -> None:
        if type(invocation_identity) is not ProviderInvocationIdentity:
            raise ProjectionError("provider_invocation_identity_required")
        context = invocation_identity.tool_context
        authority = getattr(context, "authority", None)
        if type(authority) is not SegmentExecutionAuthority:
            raise ProjectionError("segment_authority_required")
        require_authority_phase(authority, AuthorityUse.PROVIDER_INVOKE, invocation_identity)
        if (
            invocation_identity.surface is not surface
            or invocation_identity.gateway_session is not self
            or invocation_identity.model_call_id != surface.model_call_id
            or invocation_identity.surface_fingerprint != surface.runtime_surface_fingerprint
            or type(invocation_identity.model_call_surface_binding) is not ModelCallSurfaceBinding
        ):
            raise ProjectionError("provider_invocation_identity_mismatch")

    def _discard_attempt(self, attempt_id: str) -> None:
        with self._attempt_lock:
            self._attempts.discard(attempt_id)

    def consume_attempt(self, attempt_id: str) -> bool:
        if not attempt_id:
            return False
        with self._attempt_lock:
            if attempt_id not in self._attempts:
                return False
            self._attempts.remove(attempt_id)
            return True

    @property
    def budgets(self) -> tuple[ProviderBudget, ...]:
        return tuple(candidate.budget() for candidate in self._chain.candidates)

    @property
    def manifest_identities(self) -> tuple[str, ...]:
        return tuple(
            f"{candidate.provider_id}:{candidate.provider_kind}:{candidate.model}:{candidate.endpoint}"
            for candidate in self._chain.candidates
        )

    def preflight(
        self,
        surface: FrozenModelSurface,
        *,
        invocation_identity: ProviderInvocationIdentity,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> None:
        self._validate_invocation(surface, invocation_identity)
        # The projector used the minimum budget of this exact frozen chain, so
        # one deterministic preflight is sufficient before model.requested.
        SingleCandidateAgentTransport._preflight(
            self._chain.candidates[0], surface, response_format, stream=stream
        )

    def complete(
        self,
        surface: FrozenModelSurface,
        response_format: dict[str, Any] | None = None,
        *,
        invocation_identity: ProviderInvocationIdentity,
        before_attempt: Callable[[], None] | None = None,
    ) -> BoundProviderResponse:
        self._validate_invocation(surface, invocation_identity)
        last_error: Exception | None = None
        for ordinal, candidate in enumerate(self._chain.candidates):
            if before_attempt is not None:
                before_attempt()
            attempt_id = self._begin_authorized_attempt(invocation_identity, ordinal)
            try:
                response = self._transport.complete_one(candidate, surface, response_format)
                return BoundProviderResponse(
                    surface.model_call_id,
                    ordinal,
                    attempt_id,
                    surface.runtime_surface_fingerprint,
                    response,
                    invocation_identity,
                    cast(
                        ModelCallSurfaceBinding,
                        invocation_identity.model_call_surface_binding,
                    ),
                )
            except AgentLoopControlError:
                self._discard_attempt(attempt_id)
                raise
            except Exception as exc:
                self._discard_attempt(attempt_id)
                last_error = exc
            except BaseException:
                self._discard_attempt(attempt_id)
                raise
        assert last_error is not None
        raise last_error

    def stream(
        self,
        surface: FrozenModelSurface,
        on_delta: Callable[[str], None],
        *,
        invocation_identity: ProviderInvocationIdentity,
        before_attempt: Callable[[], None] | None = None,
    ) -> BoundProviderResponse:
        self._validate_invocation(surface, invocation_identity)
        last_error: Exception | None = None
        for ordinal, candidate in enumerate(self._chain.candidates):
            if before_attempt is not None:
                before_attempt()
            attempt_id = self._begin_authorized_attempt(invocation_identity, ordinal)
            visible = False

            def emit(value: str) -> None:
                nonlocal visible
                if value:
                    visible = True
                    on_delta(value)

            try:
                response = self._transport.stream_one(candidate, surface, emit)
                return BoundProviderResponse(
                    surface.model_call_id,
                    ordinal,
                    attempt_id,
                    surface.runtime_surface_fingerprint,
                    response,
                    invocation_identity,
                    cast(
                        ModelCallSurfaceBinding,
                        invocation_identity.model_call_surface_binding,
                    ),
                )
            except AgentLoopControlError:
                self._discard_attempt(attempt_id)
                raise
            except Exception as exc:
                self._discard_attempt(attempt_id)
                if visible:
                    raise
                last_error = exc
            except BaseException:
                self._discard_attempt(attempt_id)
                raise
        assert last_error is not None
        raise last_error

    def stream_deferred(
        self,
        surface: FrozenModelSurface,
        on_delta: Callable[[str], None],
        *,
        invocation_identity: ProviderInvocationIdentity,
        before_attempt: Callable[[], None] | None = None,
    ) -> BoundProviderResponse:
        """Stream with provider-attempt-local delta buffers.

        This variant is for the Agent Loop surface path.  A candidate's
        partial output is committed to ``on_delta`` only after that candidate
        has returned a bound response, so a provider failure can fall back
        without leaking the failed candidate's deltas.  The commit is outside
        the provider try/except boundary: a callback/sink failure is a
        completed-attempt transport failure and must never trigger another
        provider attempt.  ``stream`` intentionally retains its historical
        visible-delta/no-fallback semantics for ordinary callers.
        """
        self._validate_invocation(surface, invocation_identity)
        last_error: Exception | None = None
        for ordinal, candidate in enumerate(self._chain.candidates):
            if before_attempt is not None:
                before_attempt()
            attempt_id = self._begin_authorized_attempt(invocation_identity, ordinal)
            deferred: list[str] = []

            def defer(value: str) -> None:
                if value:
                    deferred.append(value)

            try:
                response = self._transport.stream_one(candidate, surface, defer)
                bound = BoundProviderResponse(
                    surface.model_call_id,
                    ordinal,
                    attempt_id,
                    surface.runtime_surface_fingerprint,
                    response,
                    invocation_identity,
                    cast(
                        ModelCallSurfaceBinding,
                        invocation_identity.model_call_surface_binding,
                    ),
                )
            except AgentLoopControlError:
                self._discard_attempt(attempt_id)
                raise
            except Exception as exc:
                self._discard_attempt(attempt_id)
                last_error = exc
                continue
            except BaseException:
                self._discard_attempt(attempt_id)
                raise

            try:
                for value in deferred:
                    on_delta(value)
            except BaseException:
                self._discard_attempt(attempt_id)
                raise
            return bound
        assert last_error is not None
        raise last_error
