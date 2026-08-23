from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from copy import deepcopy
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from litellm import completion

from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.ai.tool_runtime.contracts import ProviderToolContract
from offerpilot.config import AIProviderProfile, Config
from offerpilot.context_projector.binding import BoundProviderResponse
from offerpilot.context_projector.budget import (
    ADAPTER_REQUEST_BODY_BYTE_CAP,
    DEFAULT_OUTPUT_RESERVE,
    PROVIDER_FRAMING_RESERVE,
    ProviderBudget,
)
from offerpilot.context_projector.contracts import FrozenModelSurface, ProjectionError
from offerpilot.context_projector.gateway import (
    AgentProviderGatewaySession,
    FrozenProviderCandidate,
    FrozenProviderExecutionChain,
    SingleCandidateAgentTransport,
    normalize_provider_endpoint,
)


class ProviderCallError(RuntimeError):
    """A provider failure with safe, non-content diagnostics."""

    def __init__(self, diagnostic: dict[str, Any]):
        self.diagnostic = diagnostic
        super().__init__("AI provider request failed")


class ConfiguredAIClient:
    def __init__(self, config: Config, on_provider_event: Callable[[str, str], None] | None = None):
        self._providers = config.ordered_provider_profiles()
        self._on_provider_event = on_provider_event
        if not any(provider.enabled and provider.api_key for provider in self._providers):
            raise ValueError("AI is not configured: run `oc config` to set your API key")
        try:
            chain = FrozenProviderExecutionChain.freeze(self._providers)
        except ProjectionError as exc:
            raise ValueError(f"AI provider configuration is invalid: {exc.code}") from exc
        self._agent_gateway = AgentProviderGatewaySession(
            chain,
            SingleCandidateAgentTransport(
                self._complete_with_frozen_candidate,
                self._stream_with_frozen_candidate,
            ),
        )

    @property
    def supports_json_schema(self) -> bool:
        return any(provider.supports_json_schema for provider in self._candidate_providers())

    @property
    def agent_provider_budgets(self) -> tuple[ProviderBudget, ...]:
        return self._agent_gateway.budgets

    @property
    def agent_provider_manifest_identities(self) -> tuple[str, ...]:
        return self._agent_gateway.manifest_identities

    def preflight_agent_surface(self, surface: FrozenModelSurface, *, stream: bool) -> None:
        self._agent_gateway.preflight(surface, stream=stream)

    def complete_agent_surface(
        self,
        surface: FrozenModelSurface,
        *,
        before_attempt: Callable[[], None] | None = None,
    ) -> BoundProviderResponse:
        return self._agent_gateway.complete(surface, before_attempt=before_attempt)

    def stream_agent_surface(
        self,
        surface: FrozenModelSurface,
        on_delta: Callable[[str], None],
        *,
        before_attempt: Callable[[], None] | None = None,
    ) -> BoundProviderResponse:
        return self._agent_gateway.stream_deferred(
            surface,
            on_delta,
            before_attempt=before_attempt,
        )

    def consume_agent_provider_attempt(self, attempt_id: str) -> bool:
        return self._agent_gateway.consume_attempt(attempt_id)

    def _complete_with_frozen_candidate(
        self,
        candidate: FrozenProviderCandidate,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None,
    ) -> Assistant:
        return self._complete_with_provider(
            _profile_from_frozen_candidate(candidate),
            messages,
            tools,
            response_format,
            force_api_base=True,
        )

    def _stream_with_frozen_candidate(
        self,
        candidate: FrozenProviderCandidate,
        messages: list[Message],
        tools: list[ProviderToolContract],
        on_delta: Callable[[str], None],
    ) -> Assistant:
        return self._stream_with_provider(
            _profile_from_frozen_candidate(candidate),
            messages,
            tools,
            on_delta,
            force_api_base=True,
        )

    def complete(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        last_error: Exception | None = None
        providers = self._candidate_providers()
        for index, provider in enumerate(providers):
            if not provider.api_key:
                continue
            correlation_id = uuid.uuid4().hex[:16]
            started = time.perf_counter()
            try:
                assistant = self._complete_with_provider(
                    provider, messages, tools, response_format
                )
                provider_blocks = assistant.provider_blocks if isinstance(assistant.provider_blocks, dict) else {}
                _try_audit_provider_result(
                    provider,
                    status="success",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    http_status=None,
                    provider_request_id_hash=_hash_request_id(provider_blocks.get("request_id")),
                )
                if index > 0:
                    self._emit("INFO", f"AI fallback provider {provider.id} succeeded")
                return assistant
            except Exception as exc:
                last_error = exc
                _try_audit_provider_result(
                    provider,
                    status="error",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    http_status=_provider_status_code(exc),
                    provider_request_id_hash=_provider_request_id(exc),
                    failure_category=_classify_provider_failure(exc),
                )
                if index + 1 < len(providers):
                    self._emit(
                        "WARNING",
                        f"AI provider {provider.id} failed; trying fallback {providers[index + 1].id}",
                    )
                    continue
                diagnostic = _provider_failure_diagnostic(
                    provider,
                    exc,
                    correlation_id=correlation_id,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                self._emit(
                    "WARNING",
                    "ai_provider_failure "
                    + json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":")),
                )
                raise ProviderCallError(diagnostic) from exc
        if last_error is not None:
            raise last_error
        raise ValueError("AI is not configured: run `oc config` to set your API key")

    def stream_complete(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        on_delta: Callable[[str], None],
    ) -> Assistant:
        last_error: Exception | None = None
        providers = self._candidate_providers()
        for index, provider in enumerate(providers):
            if not provider.api_key:
                continue
            emitted_delta = False

            def emit_delta(text: str) -> None:
                nonlocal emitted_delta
                emitted_delta = True
                on_delta(text)

            try:
                assistant = self._stream_with_provider(provider, messages, tools, emit_delta)
                if index > 0:
                    self._emit("INFO", f"AI fallback provider {provider.id} succeeded")
                return assistant
            except Exception as exc:
                last_error = exc
                if emitted_delta:
                    raise
                if index + 1 < len(providers):
                    self._emit(
                        "WARNING",
                        f"AI provider {provider.id} failed; trying fallback {providers[index + 1].id}",
                    )
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise ValueError("AI is not configured: run `oc config` to set your API key")

    def _candidate_providers(self) -> list[AIProviderProfile]:
        return [provider for provider in self._providers if provider.enabled]

    def _complete_with_provider(
        self,
        provider: AIProviderProfile,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None = None,
        *,
        force_api_base: bool = False,
    ) -> Assistant:
        payload: dict[str, Any] = {
            "model": _litellm_model(provider),
            "messages": [_openai_message(message) for message in messages],
            "api_key": provider.api_key,
        }
        api_base = (
            provider.base_url.rstrip("/") if force_api_base else _litellm_api_base(provider)
        )
        if api_base:
            payload["api_base"] = api_base
        if tools:
            payload["tools"] = [_openai_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if response_format is not None and provider.supports_json_schema:
            payload["response_format"] = response_format

        _adapter_preflight_payload(provider, payload)
        _try_audit_provider_endpoint(provider.base_url)
        _try_audit_provider_request(provider, payload)
        response = completion(**payload)
        message = _first_choice_message(response)
        calls = []
        for call in _get(message, "tool_calls") or []:
            function = _get(call, "function") or {}
            calls.append(
                ToolCall(
                    id=str(_get(call, "id") or ""),
                    name=str(_get(function, "name") or ""),
                    args=str(_get(function, "arguments") or "{}"),
                )
            )
        provider_blocks = _provider_blocks(message)
        request_id = _get(response, "id")
        if request_id:
            provider_blocks["request_id"] = str(request_id)
        return Assistant(
            content=str(_get(message, "content") or ""),
            tool_calls=calls,
            provider_blocks=provider_blocks,
        )

    def _stream_with_provider(
        self,
        provider: AIProviderProfile,
        messages: list[Message],
        tools: list[ProviderToolContract],
        on_delta: Callable[[str], None],
        *,
        force_api_base: bool = False,
    ) -> Assistant:
        payload: dict[str, Any] = {
            "model": _litellm_model(provider),
            "messages": [_openai_message(message) for message in messages],
            "api_key": provider.api_key,
            "stream": True,
        }
        api_base = (
            provider.base_url.rstrip("/") if force_api_base else _litellm_api_base(provider)
        )
        if api_base:
            payload["api_base"] = api_base
        if tools:
            payload["tools"] = [_openai_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"

        _adapter_preflight_payload(provider, payload)
        _try_audit_provider_endpoint(provider.base_url)
        _try_audit_provider_request(provider, payload)
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        provider_blocks: dict[str, Any] = {}
        for chunk in completion(**payload):
            delta = _first_choice_delta(chunk)
            piece = _get(delta, "content")
            if piece:
                text = str(piece)
                content_parts.append(text)
                on_delta(text)
            reasoning_content = _get(delta, "reasoning_content")
            if reasoning_content:
                provider_blocks["reasoning_content"] = str(provider_blocks.get("reasoning_content") or "") + str(
                    reasoning_content
                )
            for raw_call in _get(delta, "tool_calls") or []:
                index = int(_get(raw_call, "index") or 0)
                current = tool_calls.setdefault(index, {"id": "", "name": "", "args": ""})
                call_id = _get(raw_call, "id")
                if call_id:
                    current["id"] = str(call_id)
                function = _get(raw_call, "function") or {}
                name = _get(function, "name")
                if name:
                    current["name"] = str(name)
                arguments = _get(function, "arguments")
                if arguments:
                    current["args"] = str(current["args"]) + str(arguments)

        calls = [
            ToolCall(
                id=str(raw["id"]),
                name=str(raw["name"]),
                args=str(raw["args"] or "{}"),
            )
            for _, raw in sorted(tool_calls.items())
            if raw.get("name")
        ]
        return Assistant(
            content="".join(content_parts),
            tool_calls=calls,
            provider_blocks=provider_blocks,
        )

    def _emit(self, level: str, message: str) -> None:
        if self._on_provider_event is not None:
            self._on_provider_event(level, message)


def _litellm_model(provider: AIProviderProfile) -> str:
    if "/" in provider.model:
        return provider.model
    if provider.provider in {"openai", "openai_compatible", "litellm_proxy"}:
        return f"openai/{provider.model}"
    if provider.provider:
        return f"{provider.provider}/{provider.model}"
    return provider.model


def _profile_from_frozen_candidate(candidate: FrozenProviderCandidate) -> AIProviderProfile:
    return AIProviderProfile(
        id=candidate.provider_id,
        provider=candidate.provider_kind,
        api_key=candidate.credential,
        base_url=candidate.endpoint,
        model=candidate.model,
        enabled=True,
        context_window=candidate.context_window,
        max_output_tokens=candidate.output_reserve,
        supports_json_schema=candidate.supports_json_schema,
    )


def _audit_provider_endpoint(base_url: str) -> None:
    path = os.getenv("OFFERPILOT_PROVIDER_AUDIT_FILE")
    if not path:
        return
    parsed = urlparse(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with open(path, "a", encoding="utf-8") as audit:
        audit.write(
            json.dumps(
                {"kind": "provider_egress", "scheme": parsed.scheme, "host": parsed.hostname, "port": port},
                ensure_ascii=True,
            )
            + "\n"
        )


def _try_audit_provider_endpoint(base_url: str) -> None:
    try:
        _audit_provider_endpoint(base_url)
    except Exception:
        return


def _audit_provider_request(provider: AIProviderProfile, payload: dict[str, Any]) -> None:
    path = os.getenv("OFFERPILOT_PROVIDER_REQUEST_AUDIT_FILE")
    if not path:
        return
    parsed = urlparse(provider.base_url)
    messages = payload.get("messages", [])
    tools = payload.get("tools", [])
    response_format = payload.get("response_format")
    provider_payload = {
        key: value for key, value in payload.items() if key not in {"api_key", "api_base"}
    }
    canonical_input = {
        "messages": messages,
        "tools": tools,
        "response_format": response_format,
    }
    record = {
        "kind": "provider_request_metadata",
        "operation": os.getenv("OFFERPILOT_FULL_VERIFY_OPERATION") or "unclassified",
        "provider_id": provider.id,
        "provider_type": provider.provider,
        "endpoint": {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        },
        "model": provider.model,
        "litellm_model": payload.get("model"),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "message_bytes": _json_bytes(messages),
        "request_body_bytes": _json_bytes(provider_payload),
        "request_body_scope": "serialized_provider_payload_without_auth_or_endpoint",
        "input_fingerprint_sha256": _sha256_json(canonical_input),
        "schema_fingerprint_sha256": _sha256_json(response_format or ""),
        "response_mode": "json_schema" if response_format is not None else "text_json",
        "explicit_max_tokens": payload.get("max_tokens")
        if payload.get("max_tokens") is not None
        else payload.get("max_completion_tokens"),
        "explicit_timeout_seconds": payload.get("timeout"),
    }
    with open(path, "a", encoding="utf-8") as audit:
        audit.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def _try_audit_provider_request(provider: AIProviderProfile, payload: dict[str, Any]) -> None:
    try:
        _audit_provider_request(provider, payload)
    except Exception:
        return


def _audit_provider_result(
    provider: AIProviderProfile,
    *,
    status: str,
    elapsed_ms: int,
    http_status: int | None,
    provider_request_id_hash: str,
    failure_category: str | None = None,
) -> None:
    path = os.getenv("OFFERPILOT_FULL_VERIFY_OPERATION_AUDIT_FILE")
    if not path:
        return
    record = {
        "kind": "provider_request_result",
        "operation": os.getenv("OFFERPILOT_FULL_VERIFY_OPERATION") or "unclassified",
        "provider_id": provider.id,
        "provider_type": provider.provider,
        "model": provider.model,
        "status": status,
        "elapsed_ms": max(0, int(elapsed_ms)),
        "http_status": http_status,
        "provider_request_id_hash": provider_request_id_hash,
        "failure_category": failure_category,
    }
    with open(path, "a", encoding="utf-8") as audit:
        audit.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")


def _try_audit_provider_result(
    provider: AIProviderProfile,
    *,
    status: str,
    elapsed_ms: int,
    http_status: int | None,
    provider_request_id_hash: str,
    failure_category: str | None = None,
) -> None:
    try:
        _audit_provider_result(
            provider,
            status=status,
            elapsed_ms=elapsed_ms,
            http_status=http_status,
            provider_request_id_hash=provider_request_id_hash,
            failure_category=failure_category,
        )
    except Exception:
        return


def _hash_request_id(value: Any) -> str:
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _adapter_preflight_payload(provider: AIProviderProfile, payload: dict[str, Any]) -> None:
    frozen_endpoint = normalize_provider_endpoint(provider.base_url)
    actual_base = str(payload.get("api_base") or "")
    if actual_base and normalize_provider_endpoint(actual_base) != frozen_endpoint:
        raise ProjectionError("provider_endpoint_changed")
    public_payload = {
        key: value for key, value in payload.items() if key not in {"api_key", "api_base"}
    }
    if _json_bytes(public_payload) > ADAPTER_REQUEST_BODY_BYTE_CAP:
        raise ProjectionError("adapter_request_body_byte_cap_exceeded")
    input_units = _json_bytes(payload.get("messages", [])) + _json_bytes(payload.get("tools", []))
    context_window = 32_768 if provider.context_window == 0 else provider.context_window
    output_reserve = (
        DEFAULT_OUTPUT_RESERVE
        if provider.max_output_tokens == 0
        else provider.max_output_tokens
    )
    budget = ProviderBudget(
        context_window=context_window,
        output_reserve=output_reserve,
        framing_reserve=PROVIDER_FRAMING_RESERVE,
    )
    if input_units > budget.input_limit:
        raise ProjectionError("adapter_context_window_exceeded")


def _provider_failure_diagnostic(
    provider: AIProviderProfile,
    error: Exception,
    *,
    correlation_id: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    failure_category = _classify_provider_failure(error)
    provider_request_id = _provider_request_id(error)
    return {
        "provider_id": provider.id,
        "failure_category": failure_category,
        "http_status": _provider_status_code(error),
        "timeout": failure_category == "network_timeout",
        "elapsed_ms": max(0, elapsed_ms),
        "correlation_id": correlation_id,
        "provider_request_id": provider_request_id,
    }


def _classify_provider_failure(error: Exception) -> str:
    error_name = type(error).__name__.lower()
    status = _provider_status_code(error)
    if "timeout" in error_name or isinstance(error, TimeoutError):
        return "network_timeout"
    if status is not None and status >= 500:
        return "provider_http_5xx"
    if "proxy" in error_name or "connecterror" in error_name:
        return "proxy_failure"
    if any(
        marker in error_name
        for marker in ("remoteprotocol", "incompleteread", "responseclosed", "readerror")
    ):
        return "response_lost"
    return "provider_exception"


def _provider_status_code(error: Exception) -> int | None:
    candidates: list[Any] = [
        getattr(error, "status_code", None),
        getattr(error, "http_status", None),
    ]
    response = getattr(error, "response", None)
    if response is not None:
        candidates.append(getattr(response, "status_code", None))
    for candidate in candidates:
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _provider_request_id(error: Exception) -> str:
    values = [getattr(error, "request_id", None), getattr(error, "requestId", None)]
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("x-request-id", "request-id"):
            try:
                values.append(headers.get(name))
            except AttributeError:
                pass
    for value in values:
        if value:
            return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return ""


def _litellm_api_base(provider: AIProviderProfile) -> str:
    if provider.provider == "anthropic":
        return ""
    return provider.base_url.rstrip("/")


def _first_choice_message(response: Any) -> Any:
    choices = _get(response, "choices") or []
    if not choices:
        return {}
    return _get(choices[0], "message") or {}


def _first_choice_delta(response: Any) -> Any:
    choices = _get(response, "choices") or []
    if not choices:
        return {}
    return _get(choices[0], "delta") or {}


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _openai_message(message: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role}
    if message.role == "user":
        images = message.provider_blocks.get("images") or []
        if images:
            content_parts: list[dict[str, Any]] = [
                {"type": "text", "text": message.content}
            ]
            for url in images:
                content_parts.append(
                    {"type": "image_url", "image_url": {"url": url}}
                )
            out["content"] = content_parts
        else:
            out["content"] = message.content
    else:
        out["content"] = message.content
    if message.role == "assistant":
        reasoning_content = message.provider_blocks.get("reasoning_content")
        if reasoning_content is not None:
            out["reasoning_content"] = reasoning_content
    if message.tool_call_id:
        out["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.args},
            }
            for call in message.tool_calls
        ]
    return out


def _provider_blocks(message: Any) -> dict[str, Any]:
    blocks: dict[str, Any] = {}
    reasoning_content = _get(message, "reasoning_content")
    if reasoning_content is not None:
        blocks["reasoning_content"] = reasoning_content
    return blocks


def _openai_tool(tool: ProviderToolContract) -> dict[str, Any]:
    return deepcopy(dict(tool.payload))
