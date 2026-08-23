from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import gc
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import socket
import tempfile
import threading
import time
from typing import Any, Callable, cast

import httpx
import uvicorn
from sqlalchemy import delete, func, select

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.tool_runtime.contracts import ProviderToolContract
from offerpilot.ai.interview_preparation_proposals import (
    InterviewPreparationModelError,
    validate_interview_preparation,
)
from offerpilot.ai.opportunity_fit_reviews import (
    OpportunityFitModelError,
    build_source_snapshot,
    validate_deep_review_v2,
    validate_triage_v2,
)
from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import (
    Application,
    ApplicationJDVersion,
    ApplicationEvidenceBundle,
    ApplicationEvent,
    ApplicationMaterialKit,
    ChatMessage,
    Conversation,
    InterviewKnowledgeCaptureAttempt,
    InterviewNote,
    InterviewReviewProposal,
    InterviewPreparationProposal,
    InterviewStory,
    InterviewStoryProposalAttempt,
    InterviewStoryUserAssertion,
    InterviewStoryVersion,
    InterviewStoryVersionEvidenceLink,
    KnowledgeCapturedSourceMetadata,
    KnowledgeEvidence,
    KnowledgeExtractionSnapshot,
    KnowledgeNote,
    KnowledgeNoteEvidence,
    KnowledgeNoteVersion,
    KnowledgeSource,
    MaterialRevisionProposal,
    MockInterviewAttempt,
    MockInterviewFeedbackProposal,
    MockInterviewReviewDraft,
    MockInterviewTurn,
    OpportunityFitReview,
    OpportunityFitReviewSession,
    OpportunityFitReviewStage,
    Question,
    Resume,
    Wakeup,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text
from offerpilot.repositories.interview_stories import InterviewStoriesRepository
from offerpilot.reliability.policy import get_recovery_policy
from offerpilot.reliability.trace import read_mock_interview_traces


def _response_recovery_policy(response: Any) -> Any:
    """Look up the recovery contract entry behind a JSON error response."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return get_recovery_policy(str(payload.get("error_code") or ""))


@dataclass(frozen=True)
class SmokeStep:
    name: str
    detail: str


@dataclass(frozen=True)
class SmokeReport:
    ok: bool
    steps: list[SmokeStep]


class SmokeServerShutdownError(RuntimeError):
    pass


class _MaterialProposalSmokeContractError(RuntimeError):
    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class _FullVerifyHttpClient:
    def __init__(self, client: httpx.Client):
        self._client = client

    def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._call("get", *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._call("post", *args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._call("put", *args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._call("patch", *args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._call("delete", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        path = str(args[0] if args else kwargs.get("url", "")).split("?", 1)[0]
        try:
            response = cast(httpx.Response, getattr(self._client, method)(*args, **kwargs))
        except Exception as exc:
            _append_full_verify_operation(
                {
                    "kind": "api_request",
                    "operation": os.environ.get("OFFERPILOT_FULL_VERIFY_OPERATION")
                    or "unclassified",
                    "method": method.upper(),
                    "path": path,
                    "http_status": None,
                    "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                    "response_attempt_status": None,
                    "error_category": _smoke_exception_category(exc),
                }
            )
            raise
        attempt_status = None
        if response.status_code in {200, 201, 202}:
            try:
                body = response.json()
                candidate = body.get("attempt_status") if isinstance(body, dict) else None
                if candidate in {"generating", "provider_unknown", "ready"}:
                    attempt_status = candidate
            except (ValueError, TypeError):
                pass
        _append_full_verify_operation(
            {
                "kind": "api_request",
                "operation": os.environ.get("OFFERPILOT_FULL_VERIFY_OPERATION")
                or "unclassified",
                "method": method.upper(),
                "path": path,
                "http_status": response.status_code,
                "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "response_attempt_status": attempt_status,
                "error_category": None,
            }
        )
        return response


def _append_full_verify_operation(record: dict[str, Any]) -> None:
    path = os.environ.get("OFFERPILOT_FULL_VERIFY_OPERATION_AUDIT_FILE", "").strip()
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception:
        return


@contextmanager
def _full_verify_client(base_url: str, *, timeout_seconds: float = 60.0) -> Any:
    with httpx.Client(base_url=base_url, timeout=timeout_seconds) as client:
        yield _FullVerifyHttpClient(client)


class _SmokeChatModel(ChatModel):
    def __init__(self, application_id: int):
        self._application_id = application_id

    def complete(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        if messages and messages[-1].role == "tool":
            return Assistant(content="smoke complete")
        user_message = _latest_user_message(messages)
        if "create application card regression" in user_message:
            return Assistant(
                tool_calls=[
                    ToolCall(
                        id="smoke-create-application-card",
                        name="create_application",
                        args=json.dumps(
                            {"company_name": "牛客网", "position_name": "agent开发", "status": "applied"},
                            ensure_ascii=False,
                        ),
                    )
                ]
            )
        if "create event card regression" in user_message:
            return Assistant(
                tool_calls=[
                    ToolCall(
                        id="smoke-create-event-card",
                        name="create_application_event",
                        args=json.dumps(
                            {
                                "application_id": self._application_id,
                                "event_type": "written_test",
                                "subtype": "assessment",
                                "scheduled_at": "2026-07-10T19:00:00+08:00",
                                "duration_minutes": 30,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            )
        return Assistant(
            tool_calls=[
                ToolCall(
                    id="smoke-write-1",
                    name="update_application_status",
                    args=json.dumps({"id": self._application_id, "status": "offer"}),
                )
            ]
        )


class _MutableSmokeChatModel(ChatModel):
    def __init__(self) -> None:
        self.application_id: int | None = None

    def complete(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        if self.application_id is None:
            raise RuntimeError("smoke application id was not initialized")
        if messages and messages[-1].role == "tool":
            return Assistant(content="http smoke complete")
        user_message = _latest_user_message(messages)
        if "create application card regression" in user_message:
            return Assistant(
                tool_calls=[
                    ToolCall(
                        id="http-smoke-create-application-card",
                        name="create_application",
                        args=json.dumps(
                            {"company_name": "牛客网", "position_name": "agent开发", "status": "applied"},
                            ensure_ascii=False,
                        ),
                    )
                ]
            )
        if "create event card regression" in user_message:
            return Assistant(
                tool_calls=[
                    ToolCall(
                        id="http-smoke-create-event-card",
                        name="create_application_event",
                        args=json.dumps(
                            {
                                "application_id": self.application_id,
                                "event_type": "written_test",
                                "subtype": "assessment",
                                "scheduled_at": "2026-07-10T19:00:00+08:00",
                                "duration_minutes": 30,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            )
        return Assistant(
            tool_calls=[
                ToolCall(
                    id="http-smoke-write-1",
                    name="update_application_status",
                    args=json.dumps({"id": self.application_id, "status": "offer"}),
                )
            ]
        )


class _InterviewStorySmokeChatModel(ChatModel):
    """Deterministic local-only model used by the Story API acceptance path."""

    def complete(
        self,
        messages: list[Message],
        tools: list[ProviderToolContract],
        response_format: dict[str, Any] | None = None,
    ) -> Assistant:
        del tools, response_format
        prompt = messages[-1].content if messages else ""
        marker = "catalog: "
        if marker not in prompt:
            raise RuntimeError("story smoke model received no evidence catalog")
        catalog = json.loads(prompt.split(marker, 1)[1])
        if not isinstance(catalog, list) or not catalog or not isinstance(catalog[0], dict):
            raise RuntimeError("story smoke model received an invalid evidence catalog")
        source = catalog[0]
        reference = {
            key: source[key]
            for key in (
                "source_kind",
                "source_stable_id",
                "source_version_or_snapshot",
                "source_path",
                "excerpt",
            )
        }
        payload = {
            "title": {"text": "筱哲的延迟排查故事", "evidence_refs": [reference]},
            "blocks": [
                {
                    "kind": "situation",
                    "text": "一次线上延迟排查场景。",
                    "fact_mode": "evidence_backed",
                    "evidence_refs": [reference],
                }
            ],
            "capability_labels": [{"text": "问题定位", "evidence_refs": [reference]}],
            "applicable_questions": [
                {"text": "请介绍一次延迟排查经历。", "evidence_refs": [reference]}
            ],
            "fact_gap_codes": ["missing_result"],
        }
        return Assistant(content=json.dumps(payload, ensure_ascii=False))


def _latest_user_message(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _assert_create_application_card(pending_body: dict[str, Any]) -> None:
    if pending_body.get("type") != "confirmation_required":
        raise RuntimeError("create application card smoke did not request confirmation")
    action = pending_body.get("pending_action", {})
    if action.get("tool_name") != "create_application":
        raise RuntimeError("create application card smoke requested the wrong tool")
    if action.get("human") != "新建投递：牛客网 - agent开发":
        raise RuntimeError("create application card smoke lost the human-readable title")
    expected_args = {"company_name": "牛客网", "position_name": "agent开发", "status": "applied"}
    if action.get("args") != expected_args:
        raise RuntimeError("create application card smoke lost the proposed application fields")
    if action.get("target") != {
        "id": "application-draft-牛客网-agent开发",
        "kind": "application",
        "title": "牛客网",
        "meta": "agent开发 · applied",
        "source": "pending_action",
    }:
        raise RuntimeError("create application card smoke lost target record details")
    if action.get("proposed_changes") != [
        {"field": "company_name", "before": "", "after": "牛客网"},
        {"field": "position_name", "before": "", "after": "agent开发"},
        {"field": "status", "before": "", "after": "applied"},
    ]:
        raise RuntimeError("create application card smoke lost proposed record changes")


def _assert_create_event_card(pending_body: dict[str, Any], application_id: int) -> None:
    if pending_body.get("type") != "confirmation_required":
        raise RuntimeError("create event card smoke did not request confirmation")
    action = pending_body.get("pending_action", {})
    if action.get("tool_name") != "create_application_event":
        raise RuntimeError("create event card smoke requested the wrong tool")
    if action.get("human") != "新建日程：笔试 · 2026-07-10 19:00 · 30 分钟":
        raise RuntimeError("create event card smoke lost the human-readable schedule title")
    if action.get("target") != {
        "id": f"application-event-draft-{application_id}",
        "kind": "application_event",
        "title": "笔试",
        "meta": "2026-07-10 19:00 · 30 分钟",
        "source": "pending_action",
    }:
        raise RuntimeError("create event card smoke lost target schedule details")
    if action.get("proposed_changes") != [
        {"field": "event_type", "before": "", "after": "written_test"},
        {"field": "subtype", "before": "", "after": "assessment"},
        {"field": "scheduled_at", "before": "", "after": "2026-07-10T19:00:00+08:00"},
        {"field": "duration_minutes", "before": "", "after": 30},
    ]:
        raise RuntimeError("create event card smoke lost proposed schedule changes")
    evidence = action.get("evidence") or []
    if not evidence or evidence[0].get("id") != f"application-{application_id}":
        raise RuntimeError("create event card smoke lost application evidence")


def _reject_pending_chat_action(client: Any, pending_body: dict[str, Any], step: str) -> None:
    rejected = client.post(
        "/api/chat/confirm",
        json={
            "conversation_id": pending_body["conversation_id"],
            "approved": False,
            "confirmation_token": pending_body["pending_action"]["confirmation_token"],
        },
    )
    _assert_status(rejected.status_code, 200, step)


def run_core_smoke(data_dir: Path, static_dir: Path | None = None) -> SmokeReport:
    from fastapi.testclient import TestClient

    steps: list[SmokeStep] = []
    data_dir.mkdir(parents=True, exist_ok=True)

    client = TestClient(create_app(data_dir=data_dir, static_dir=static_dir))
    health = client.get("/api/health")
    _assert_status(health.status_code, 200, "health")
    steps.append(SmokeStep("health", "api health returned ok"))

    if static_dir is not None:
        spa = client.get("/applications/smoke")
        _assert_status(spa.status_code, 200, "spa")
        if "root" not in spa.text:
            raise RuntimeError("spa fallback did not serve index.html")
        steps.append(SmokeStep("spa", "spa fallback served index.html"))

    created = client.post(
        "/api/applications",
        json={"company_name": "Smoke Co", "position_name": "Backend", "status": "applied"},
    )
    _assert_status(created.status_code, 201, "create_application")
    application = created.json()
    application_id = int(application["id"])
    steps.append(SmokeStep("create_application", f"created application #{application_id}"))

    chat_client = TestClient(
        create_app(
            data_dir=data_dir,
            static_dir=static_dir,
            chat_model=_SmokeChatModel(application_id),
        )
    )
    pending = chat_client.post(
        "/api/chat",
        json={"message": "move this application to offer", "conversation_id": 0},
    )
    _assert_status(pending.status_code, 200, "chat_pending")
    pending_body = pending.json()
    if pending_body.get("type") != "confirmation_required":
        raise RuntimeError("chat did not request confirmation")
    before_confirm = client.get(f"/api/applications/{application_id}").json()
    if before_confirm["status"] != "applied":
        raise RuntimeError("write tool mutated before confirmation")
    steps.append(SmokeStep("chat_pending", "write action paused for confirmation"))

    confirmed = chat_client.post(
        "/api/chat/confirm",
        json={
            "conversation_id": pending_body["conversation_id"],
            "approved": True,
            "confirmation_token": pending_body["pending_action"]["confirmation_token"],
        },
    )
    _assert_status(confirmed.status_code, 200, "confirm_action")
    after_confirm = client.get(f"/api/applications/{application_id}").json()
    if after_confirm["status"] != "offer":
        raise RuntimeError("confirmed write did not update application")
    steps.append(SmokeStep("confirm_action", "confirmed write updated application"))

    conversations = chat_client.get("/api/chat/conversations").json()
    if conversations[0]["pending_action"] is not None:
        raise RuntimeError("pending action was not cleared")
    steps.append(SmokeStep("pending_cleared", "pending action cleared after confirmation"))

    _run_chat_card_regression_smoke(chat_client, steps, application_id)

    return SmokeReport(ok=True, steps=steps)


def run_http_smoke(
    data_dir: Path,
    static_dir: Path | None = None,
    *,
    real_ai: bool = False,
) -> SmokeReport:
    prefix = "offerpilot-real-ai-verify-" if real_ai else "offerpilot-local-verify-"
    previous_operation = os.environ.get("OFFERPILOT_FULL_VERIFY_OPERATION")
    previous_stage = os.environ.get("OFFERPILOT_FULL_VERIFY_ACTIVE_STAGE")
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as temp_dir:
        isolated_data_dir = Path(temp_dir)
        if real_ai:
            _copy_real_ai_config(data_dir, isolated_data_dir)
        completed = False
        try:
            report = _run_http_smoke(isolated_data_dir, static_dir=static_dir, real_ai=real_ai)
            if real_ai:
                _assert_real_ai_smoke_data_clean(isolated_data_dir)
            completed = True
            return report
        except BaseException as exc:
            if real_ai:
                _persist_full_verify_inner_diagnostic(
                    isolated_data_dir,
                    status="failed",
                    error=exc,
                )
            raise
        finally:
            if real_ai and completed:
                _persist_full_verify_inner_diagnostic(isolated_data_dir, status="passed")
            if previous_operation is None:
                os.environ.pop("OFFERPILOT_FULL_VERIFY_OPERATION", None)
            else:
                os.environ["OFFERPILOT_FULL_VERIFY_OPERATION"] = previous_operation
            if previous_stage is None:
                os.environ.pop("OFFERPILOT_FULL_VERIFY_ACTIVE_STAGE", None)
            else:
                os.environ["OFFERPILOT_FULL_VERIFY_ACTIVE_STAGE"] = previous_stage


def _full_verify_report_dir() -> Path | None:
    raw = os.environ.get("OFFERPILOT_FULL_VERIFY_REPORT_DIR", "").strip()
    return Path(raw) if raw else None


def _set_full_verify_operation(operation: str) -> None:
    os.environ["OFFERPILOT_FULL_VERIFY_OPERATION"] = operation
    if operation != "cleanup":
        os.environ["OFFERPILOT_FULL_VERIFY_ACTIVE_STAGE"] = operation


def _safe_smoke_config_summary(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "config_path": str(path),
            "offerpilot_data": str(data_dir),
            "provider": "",
            "model": "",
        }
    if not isinstance(raw, dict):
        return {
            "config_path": str(path),
            "offerpilot_data": str(data_dir),
            "provider": "",
            "model": "",
        }
    providers = raw.get("providers")
    active_id = raw.get("active_provider_id")
    profile = None
    if isinstance(providers, list):
        profile = next(
            (
                item
                for item in providers
                if isinstance(item, dict) and item.get("id") == active_id
            ),
            None,
        )
        if profile is None:
            profile = next((item for item in providers if isinstance(item, dict)), None)
    if not isinstance(profile, dict):
        profile = raw
    return {
        "config_path": str(path),
        "offerpilot_data": str(data_dir),
        "active_provider_id": str(active_id or profile.get("id") or "default"),
        "provider": str(profile.get("provider") or ""),
        "model": str(profile.get("model") or raw.get("model") or ""),
        "supports_json_schema": bool(profile.get("supports_json_schema", False)),
    }


def _safe_inner_log_diagnostics(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "logs" / "offerpilot.log"
    if not path.is_file():
        return []
    diagnostics: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        message = entry["message"]
        if message.startswith("interview_preparation_generation "):
            category_match = re.search(r"(?:^| )category=([A-Za-z0-9_.-]+)", message)
            categories_match = re.search(r"(?:^| )failure_categories=(\[[^ ]*\])", message)
            summaries_match = re.search(r"(?:^| )structure_summaries=(\[[^ ]*\])", message)
            repair_match = re.search(r"(?:^| )repair_attempted=(true|false)", message)
            retry_match = re.search(r"(?:^| )retry_count=(\d+)", message)
            duration_match = re.search(r"(?:^| )duration_ms=(\d+)", message)
            request_id_match = re.search(
                r"(?:^| )provider_request_id_hash=([0-9a-f]{12,64})", message
            )
            diagnostic: dict[str, Any] = {
                "kind": "interview_preparation",
                "failure_category": category_match.group(1) if category_match else None,
                "failure_categories": [],
                "structure_summaries": [],
                "repair_attempted": repair_match.group(1) == "true" if repair_match else False,
                "retry_count": int(retry_match.group(1)) if retry_match else 0,
                "duration_ms": int(duration_match.group(1)) if duration_match else 0,
                "provider_request_id_hash": request_id_match.group(1) if request_id_match else "",
            }
            if categories_match:
                try:
                    categories = json.loads(categories_match.group(1))
                    if isinstance(categories, list):
                        diagnostic["failure_categories"] = [
                            value
                            for value in categories[:4]
                            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)
                        ]
                except json.JSONDecodeError:
                    pass
            if summaries_match:
                try:
                    summaries = json.loads(summaries_match.group(1))
                    if isinstance(summaries, list):
                        diagnostic["structure_summaries"] = summaries[:2]
                except json.JSONDecodeError:
                    pass
            diagnostics.append(diagnostic)
        elif message.startswith("material_proposal_generation "):
            category_match = re.search(r"(?:^| )category=([A-Za-z0-9_.-]+)", message)
            categories_match = re.search(r"(?:^| )failure_categories=(\[[^ ]*\])", message)
            summaries_match = re.search(r"(?:^| )structure_summaries=(\[[^ ]*\])", message)
            evidence_match = re.search(r"(?:^| )evidence_counts=(\{[^ ]*\})", message)
            repair_match = re.search(r"(?:^| )repair_attempted=(true|false)", message)
            retry_match = re.search(r"(?:^| )retry_count=(\d+)", message)
            duration_match = re.search(r"(?:^| )duration_ms=(\d+)", message)
            request_id_match = re.search(
                r"(?:^| )provider_request_id_hash=([0-9a-f]{12,64})", message
            )
            diagnostic = {
                "kind": "material_proposal",
                "failure_category": category_match.group(1) if category_match else None,
                "failure_categories": [],
                "structure_summaries": [],
                "evidence_counts": {},
                "repair_attempted": repair_match.group(1) == "true" if repair_match else False,
                "retry_count": int(retry_match.group(1)) if retry_match else 0,
                "duration_ms": int(duration_match.group(1)) if duration_match else 0,
                "provider_request_id_hash": request_id_match.group(1) if request_id_match else "",
            }
            if categories_match:
                try:
                    categories = json.loads(categories_match.group(1))
                    if isinstance(categories, list):
                        diagnostic["failure_categories"] = [
                            value
                            for value in categories[:4]
                            if isinstance(value, str)
                            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)
                        ]
                except json.JSONDecodeError:
                    pass
            if summaries_match:
                try:
                    summaries = json.loads(summaries_match.group(1))
                    if isinstance(summaries, list):
                        diagnostic["structure_summaries"] = summaries[:2]
                except json.JSONDecodeError:
                    pass
            if evidence_match:
                try:
                    evidence_counts = json.loads(evidence_match.group(1))
                    if isinstance(evidence_counts, dict):
                        diagnostic["evidence_counts"] = evidence_counts
                except json.JSONDecodeError:
                    pass
            diagnostics.append(diagnostic)
        elif message.startswith("ai_provider_failure "):
            try:
                payload = json.loads(message.removeprefix("ai_provider_failure "))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                diagnostics.append(
                    {
                        "kind": "provider",
                        "failure_category": payload.get("failure_category"),
                        "http_status": payload.get("http_status"),
                        "timeout": payload.get("timeout"),
                        "elapsed_ms": payload.get("elapsed_ms"),
                        "provider_request_id_hash": payload.get("provider_request_id", ""),
                    }
                )
    return diagnostics[-16:]


def _smoke_exception_category(error: BaseException | None) -> str | None:
    if error is None:
        return None
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "timeout" in name or "timeout" in text:
        return "network_timeout"
    if "provider" in name or "provider" in text:
        return "provider_error"
    if "evidence" in text or "proposal" in text or "contract" in text:
        return "contract_or_quality"
    return "process_error"


def _persist_full_verify_inner_diagnostic(
    data_dir: Path,
    *,
    status: str,
    error: BaseException | None = None,
) -> None:
    report_dir = _full_verify_report_dir()
    if report_dir is None:
        return
    try:
        diagnostics = _safe_inner_log_diagnostics(data_dir)
        material_response_diagnostic = _read_material_proposal_smoke_diagnostic(report_dir)
        if material_response_diagnostic:
            diagnostics.append(material_response_diagnostic)
        generation_diagnostic = next(
            (
                diagnostic
                for diagnostic in reversed(diagnostics)
                if diagnostic.get("kind") == "material_proposal"
            ),
            {},
        )
        failure_categories = [
            category
            for diagnostic in diagnostics
            for category in [diagnostic.get("failure_category")]
            if isinstance(category, str) and category
        ]
        failure_category = failure_categories[-1] if failure_categories else None
        last = diagnostics[-1] if diagnostics else {}
        payload = {
            "status": status,
            "stage": os.environ.get("OFFERPILOT_FULL_VERIFY_ACTIVE_STAGE")
            or "real_ai_http_verify",
            "exception_category": _smoke_exception_category(error),
            "failure_category": failure_category,
            "failure_categories": list(dict.fromkeys(failure_categories))[:4],
            "structure_summaries": last.get("structure_summaries", []),
            "evidence_counts": last.get("evidence_counts", {}),
            "repair_attempted": last.get("repair_attempted", False),
            "retry_count": last.get("retry_count", 0),
            "duration_ms": last.get("duration_ms") or generation_diagnostic.get("duration_ms", 0),
            "provider_request_id_hash": last.get("provider_request_id_hash")
            or generation_diagnostic.get("provider_request_id_hash", ""),
            "diagnostics": diagnostics,
            "config": _safe_smoke_config_summary(data_dir),
        }
        if material_response_diagnostic:
            payload["material_proposal_diagnostic"] = material_response_diagnostic
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "full-verify-inner-diagnostic.json").write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Never mask the real verify result with diagnostic persistence failure.
        return


def _read_material_proposal_smoke_diagnostic(report_dir: Path) -> dict[str, Any]:
    path = report_dir / "material-proposal-diagnostic.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_http_smoke(
    data_dir: Path,
    static_dir: Path | None = None,
    *,
    real_ai: bool = False,
) -> SmokeReport:
    steps: list[SmokeStep] = []
    smoke_resume_ids: list[int] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    _set_full_verify_operation("bootstrap")

    _run_unconfigured_chat_smoke(static_dir, steps)

    local_model = None if real_ai else _MutableSmokeChatModel()
    app = create_app(data_dir=data_dir, static_dir=static_dir, chat_model=local_model)
    with _running_server(app) as base_url:
        with _full_verify_client(
            base_url,
            timeout_seconds=180.0 if real_ai else 60.0,
        ) as client:
            health = client.get("/api/health")
            _assert_status(health.status_code, 200, "http_health")
            steps.append(SmokeStep("http_health", "GET /api/health returned ok"))

            settings = client.get("/api/settings")
            _assert_status(settings.status_code, 200, "http_settings")
            settings_body = settings.json()
            if real_ai and not bool(settings_body.get("has_api_key")):
                raise RuntimeError("real-ai profile requires a configured API key")
            steps.append(SmokeStep("http_settings", "GET /api/settings returned current AI settings"))

            if static_dir is not None:
                spa = client.get("/applications/smoke")
                _assert_status(spa.status_code, 200, "http_spa")
                if "root" not in spa.text:
                    raise RuntimeError("http_spa did not serve index.html")
                steps.append(SmokeStep("http_spa", "GET /applications/smoke served the SPA fallback"))

            marker = str(int(time.time() * 1000))
            company = f"AI HTTP Smoke {marker}"
            created = client.post(
                "/api/applications",
                json={
                    "company_name": company,
                    "position_name": "Verification Engineer",
                    "status": "applied",
                    "source": "smoke",
                },
            )
            _assert_status(created.status_code, 201, "http_create_application")
            application_id = int(created.json()["id"])
            if local_model is not None:
                local_model.application_id = application_id
            steps.append(SmokeStep("http_create_application", f"POST /api/applications created #{application_id}"))

            try:
                listed = client.get("/api/applications", params={"status": "applied"})
                _assert_status(listed.status_code, 200, "http_list_applications")
                if not any(item.get("id") == application_id for item in listed.json()):
                    raise RuntimeError("created application was not returned by list endpoint")
                steps.append(SmokeStep("http_list_applications", "GET /api/applications returned created record"))

                _run_resume_http_smoke(client, steps)
                _run_application_event_http_smoke(client, steps, application_id)

                if real_ai:
                    _run_named_real_ai_smoke_stage(
                        "interview_preparation",
                        lambda: _run_real_ai_interview_preparation_smoke(
                            client, steps, application_id, smoke_resume_ids
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "material_proposal",
                        lambda: _run_real_ai_material_proposal_smoke(
                            client, steps, application_id, smoke_resume_ids
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "opportunity_fit",
                        lambda: _run_real_ai_opportunity_fit_smoke(
                            client, steps, application_id, smoke_resume_ids
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "interview_review",
                        lambda: _run_real_ai_interview_review_smoke(
                            client, steps, application_id
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "interview_knowledge_capture",
                        lambda: _run_real_ai_interview_knowledge_capture_smoke(
                            client, steps, application_id
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "mock_interview",
                        lambda: _run_real_ai_mock_interview_smoke(
                            client, steps, application_id, smoke_resume_ids, data_dir
                        ),
                    )
                    _run_named_real_ai_smoke_stage(
                        "write_smoke",
                        lambda: _run_real_ai_write_smoke(
                            client, steps, company, application_id
                        ),
                    )
                else:
                    _run_local_proposal_terminal_smoke(
                        client, steps, application_id, data_dir, smoke_resume_ids
                    )
                    _run_application_outcome_http_smoke(
                        client, steps, application_id, smoke_resume_ids
                    )
                    _run_deterministic_chat_smoke(client, steps, application_id)
                    _run_chat_card_regression_smoke(client, steps, application_id, step_prefix="http_")
            finally:
                _set_full_verify_operation("cleanup")
                cleanup = client.delete(f"/api/applications/{application_id}")
                _assert_status(cleanup.status_code, 200, "http_cleanup")
                deleted_application = client.get(f"/api/applications/{application_id}")
                _assert_status(deleted_application.status_code, 404, "http_cleanup_visibility")
                if real_ai:
                    _cleanup_real_ai_smoke_records(data_dir, application_id, smoke_resume_ids)
                steps.append(SmokeStep("http_cleanup", f"deleted smoke application #{application_id}"))

    return SmokeReport(ok=True, steps=steps)


def _run_named_real_ai_smoke_stage(stage: str, operation: Callable[[], None]) -> None:
    _set_full_verify_operation(stage)
    started = time.monotonic()
    try:
        operation()
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        raise RuntimeError(
            f"real-ai smoke stage {stage} failed after {elapsed_ms} ms: {type(exc).__name__}"
        ) from exc


def _run_real_ai_interview_preparation_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    resume_ids: list[int] | None = None,
    cases: list[str] | None = None,
) -> None:
    resume_raw_text = "Built reliable API services; input_snapshot is a literal term here."
    resume_content_json = {
        "raw_text": resume_raw_text,
        "experience": [{"highlights": ["Built reliable API services", "Led a migration."]}],
    }
    resume = client.post(
        "/api/resumes",
        json={
            "title": "AI Interview Preparation Smoke Resume",
            "text": resume_raw_text,
            "content_json": resume_content_json,
        },
    )
    _assert_status(resume.status_code, 201, "http_interview_preparation_resume")
    resume_id = int(resume.json()["id"])
    if resume_ids is not None:
        resume_ids.append(resume_id)
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application_id,
            "event_type": "interview",
            "subtype": "technical",
            "scheduled_at": "2026-07-24T10:00:00Z",
            "duration_minutes": 45,
        },
    )
    _assert_status(event.status_code, 201, "http_interview_preparation_event")
    event_body = event.json()
    event_id = int(event_body["id"])
    scheduled_at = event_body.get("scheduled_at")
    if isinstance(scheduled_at, str) and scheduled_at:
        try:
            scheduled_at = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).replace(
                tzinfo=None
            ).isoformat()
        except ValueError:
            pass
    event_snapshot = {
        "id": event_id,
        "application_id": application_id,
        "event_type": event_body.get("event_type", "interview"),
        "subtype": event_body.get("subtype", "technical"),
        "round": event_body.get("round", 0),
        "scheduled_at": scheduled_at,
        "duration_minutes": event_body.get("duration_minutes", 45),
        "status": event_body.get("status", "todo"),
    }
    cases = cases or [
        "Build reliable Python services and explain operational tradeoffs.",
        "Design an API migration with safe rollback and observability.",
        "Review distributed systems failure handling and testing practices.",
    ]
    verified_non_empty = 0
    for index, jd_text in enumerate(cases, start=1):
        jd_version_id = _save_application_jd_version(
            client,
            application_id,
            jd_text,
            f"smoke-interview-prep-jd-{index:04d}",
        )
        jd_snapshot_metadata: dict[str, object] = {}
        get_jd_version = getattr(client, "get", None)
        if callable(get_jd_version):
            jd_version_response = get_jd_version(
                f"/api/applications/{application_id}/job-description/versions/{jd_version_id}"
            )
            _assert_status(jd_version_response.status_code, 200, "http_interview_preparation_jd_version")
            jd_version_body = jd_version_response.json()
            if not isinstance(jd_version_body, dict):
                raise RuntimeError("interview preparation smoke JD version response was invalid")
            version_number = jd_version_body.get("version_number")
            content_sha256 = jd_version_body.get("content_sha256")
            if (
                type(version_number) is not int
                or version_number <= 0
                or not isinstance(content_sha256, str)
                or not content_sha256
            ):
                raise RuntimeError("interview preparation smoke JD version metadata was invalid")
            jd_snapshot_metadata = {
                "version_id": jd_version_id,
                "version_number": version_number,
                "content_sha256": content_sha256,
            }
        request_payload = {
            "event_id": event_id,
            "resume_id": resume_id,
            "jd_version_id": jd_version_id,
            "knowledge_selections": [],
            "user_assertions": [f"I led preparation case {index}."],
            "idempotency_key": f"interview-preparation-smoke-{index}",
        }
        snapshot = {
            "event": event_snapshot,
            "jd": {"text": jd_text, **jd_snapshot_metadata},
            "jd_version_id": jd_version_id,
            "resume": {"id": resume_id, "content_json": resume_content_json},
            "knowledge_evidence": [],
            "user_assertions": request_payload["user_assertions"],
        }
        pending_retries = 0
        while True:
            response = client.post(
                f"/api/applications/{application_id}/interview-preparation-proposals",
                json=request_payload,
            )
            body = response.json()
            _assert_interview_preparation_smoke_response_safe(body)
            if response.status_code != 202:
                if response.status_code not in {200, 201}:
                    raise RuntimeError(
                        f"http_interview_preparation_proposal_{index} returned status "
                        f"{response.status_code}, expected 200 or 201"
                    )
                _validate_interview_preparation_proposal_response(
                    body,
                    application_id=application_id,
                    event_id=event_id,
                    resume_id=resume_id,
                    snapshot=snapshot,
                )
                break
            _validate_interview_preparation_pending_response(
                body, request_payload, application_id=application_id, event_id=event_id
            )
            if pending_retries >= 3:
                raise RuntimeError("interview preparation smoke pending result did not complete")
            time.sleep(min(body["retry_after_ms"], 30_000) / 1000)
            pending_retries += 1
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("proposal"), dict):
            raise RuntimeError("interview preparation smoke response did not contain a proposal")
        if body.get("proposal_status") == "normal":
            proposal = body["proposal"]
            if any(
                isinstance(items, list)
                and any(isinstance(item, dict) and item.get("evidence_refs") for item in items)
                for items in proposal.values()
            ):
                verified_non_empty += 1
    if verified_non_empty < 1:
        raise RuntimeError("interview preparation smoke returned no evidence-backed non-empty proposal")
    steps.append(
        SmokeStep(
            "http_interview_preparation_proposal",
            "real AI returned safe interview preparation results with at least one cited result",
        )
    )


def _assert_interview_preparation_smoke_response_safe(body: object) -> None:
    if not isinstance(body, dict):
        raise RuntimeError("interview preparation smoke response was not an object")


def _validate_interview_preparation_proposal_response(
    body: dict[str, object],
    *,
    application_id: int,
    event_id: int,
    resume_id: int,
    snapshot: dict[str, Any],
) -> None:
    expected_fields = {
        "id",
        "application_id",
        "event_id",
        "resume_id",
        "jd_version_id",
        "attempt_status",
        "proposal_status",
        "source_fingerprint",
        "source_status",
        "source_states",
        "proposal",
        "proposal_hash",
        "created_at",
    }
    if set(body) != expected_fields:
        raise RuntimeError("interview preparation smoke proposal response fields were invalid")
    if body["attempt_status"] != "ready":
        raise RuntimeError("interview preparation smoke returned an invalid terminal status")
    if body["proposal_status"] not in {"normal", "safe_empty"}:
        raise RuntimeError("interview preparation smoke returned an invalid proposal status")
    if any(
        type(body[field]) is not int or body[field] != expected
        for field, expected in (
        ("application_id", application_id),
        ("event_id", event_id),
        ("resume_id", resume_id),
        ("jd_version_id", snapshot.get("jd_version_id")),
    )
    ):
        raise RuntimeError("interview preparation smoke proposal response ownership was invalid")
    if type(body["id"]) is not int or body["id"] <= 0:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    if any(
        not isinstance(body[field], str) or not body[field]
        for field in ("source_fingerprint", "proposal_hash")
    ):
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    if not isinstance(body["source_status"], str) or body["source_status"] not in {
        "current",
        "not_checked",
        "source_changed",
    }:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    if not isinstance(body["source_states"], dict):
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    expected_states = {
        "event": "current",
        "resume": "current",
        "jd": "current",
        "knowledge": "current",
    }
    if body["source_states"] != expected_states:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    expected_source_status = "source_changed" if "source_changed" in expected_states.values() else "not_checked"
    if body["source_status"] != expected_source_status:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    expected_source_fingerprint = sha256_text(canonical_json(snapshot))
    if body["source_fingerprint"] != expected_source_fingerprint:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    created_at = body["created_at"]
    if not isinstance(created_at, str) or not created_at:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("interview preparation smoke terminal metadata was invalid") from exc

    proposal = body["proposal"]
    if not isinstance(proposal, dict) or set(proposal) != {
        "preparation_directions",
        "story_prompts",
        "review_points",
        "interviewer_questions",
        "items_to_clarify",
    }:
        raise RuntimeError("interview preparation smoke proposal structure was invalid")
    for items in proposal.values():
        if not isinstance(items, list):
            raise RuntimeError("interview preparation smoke proposal structure was invalid")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"id", "text", "evidence_refs"}:
                raise RuntimeError("interview preparation smoke proposal structure was invalid")
            if not isinstance(item["id"], str) or not isinstance(item["text"], str):
                raise RuntimeError("interview preparation smoke proposal structure was invalid")
            evidence_refs = item["evidence_refs"]
            if not isinstance(evidence_refs, list) or not evidence_refs:
                raise RuntimeError("interview preparation smoke proposal structure was invalid")
            for evidence_ref in evidence_refs:
                if not isinstance(evidence_ref, dict) or set(evidence_ref) != {
                    "source",
                    "path",
                    "excerpt",
                }:
                    raise RuntimeError("interview preparation smoke proposal structure was invalid")
                if not all(isinstance(evidence_ref[field], str) for field in ("source", "path", "excerpt")):
                    raise RuntimeError("interview preparation smoke proposal structure was invalid")
    try:
        validate_interview_preparation(proposal, snapshot)
    except InterviewPreparationModelError as exc:
        raise RuntimeError("interview preparation smoke evidence was not traceable") from exc
    item_count = sum(len(items) for items in proposal.values())
    if body["proposal_status"] == "safe_empty" and item_count != 0:
        raise RuntimeError("interview preparation smoke proposal status was not empty")
    if body["proposal_status"] == "normal" and item_count == 0:
        raise RuntimeError("interview preparation smoke returned an empty proposal")
    if body["proposal_hash"] != sha256_text(canonical_json(proposal)):
        raise RuntimeError("interview preparation smoke terminal metadata was invalid")


def _validate_interview_preparation_pending_response(
    body: dict[str, object],
    request_payload: dict[str, object],
    *,
    application_id: int,
    event_id: int,
) -> None:
    expected_fields = {
        "attempt_status",
        "application_id",
        "event_id",
        "idempotency_key",
        "generation_revision",
        "retry_after_ms",
    }
    if set(body) != expected_fields:
        raise RuntimeError("interview preparation smoke pending response fields were invalid")
    if body["attempt_status"] not in {"generating", "provider_unknown"}:
        raise RuntimeError("interview preparation smoke returned an invalid pending status")
    if body["application_id"] != application_id:
        raise RuntimeError("interview preparation smoke pending application did not match")
    if body["event_id"] != event_id:
        raise RuntimeError("interview preparation smoke pending event did not match")
    if body["idempotency_key"] != request_payload.get("idempotency_key"):
        raise RuntimeError("interview preparation smoke pending key did not match")
    for field in ("generation_revision", "retry_after_ms"):
        value = body[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("interview preparation smoke pending timing fields were invalid")


def _save_application_jd_version(
    client: Any,
    application_id: int,
    jd_text: str,
    idempotency_key: str,
) -> int:
    expected_current_version_id: int | None = None
    get_current = getattr(client, "get", None)
    if callable(get_current):
        current_response = get_current(f"/api/applications/{application_id}/job-description")
        if current_response.status_code == 200:
            current_body = current_response.json()
            current = current_body.get("current") if isinstance(current_body, dict) else None
            current_id = current.get("id") if isinstance(current, dict) else None
            if current_id is not None:
                if type(current_id) is not int or current_id <= 0:
                    raise RuntimeError("current application JD version response was invalid")
                expected_current_version_id = current_id
        elif current_response.status_code != 404:
            _assert_status(current_response.status_code, 200, "application_jd_current")
    response = client.post(
        f"/api/applications/{application_id}/job-description/versions",
        json={
            "jd_text": jd_text,
            "source_url": None,
            "expected_current_version_id": expected_current_version_id,
            "idempotency_key": idempotency_key,
        },
    )
    _assert_status(response.status_code, 201, "application_jd_source_version")
    body = response.json()
    version_id = body.get("id") if isinstance(body, dict) else None
    if type(version_id) is not int or version_id <= 0:
        raise RuntimeError("application JD version response was invalid")
    return version_id


def _run_real_ai_material_proposal_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    resume_ids: list[int] | None = None,
) -> None:
    anchor_resume_id: int | None = None
    resume_id: int | None = None
    try:
        anchor = client.post(
            "/api/resumes",
            json={
                "title": "AI Material Proposal Smoke Anchor",
                "text": "",
                "content_json": {},
            },
        )
        _assert_status(anchor.status_code, 201, "http_material_proposal_resume_anchor")
        anchor_resume_id = int(anchor.json()["id"])
        if resume_ids is not None:
            resume_ids.append(anchor_resume_id)

        created_resume = client.post(
            "/api/resumes",
            json={
                "title": "AI Material Proposal Smoke Resume",
                "text": "Built API services. Led migration.",
                "content_json": {
                    "experience": [{"highlights": ["Built API services", "Led migration"]}],
                    "skills": ["Python"],
                    "raw_text": "Built API services. Led migration.",
                },
            },
        )
        _assert_status(created_resume.status_code, 201, "http_material_proposal_resume")
        resume_id = int(created_resume.json()["id"])
        if resume_ids is not None:
            resume_ids.append(resume_id)

        jd_version_id = _save_application_jd_version(
            client,
            application_id,
            "Evidence QA Engineer: build reliable API quality workflows.",
            "smoke-material-jd-0001",
        )

        kit = client.post(
            f"/api/applications/{application_id}/material-kit/generate",
            json={
                "resume_id": resume_id,
                "jd_version_id": jd_version_id,
            },
        )
        _assert_status(kit.status_code, 201, "http_material_proposal_kit")

        proposal = client.post(
            f"/api/applications/{application_id}/material-revision-proposals",
            json={
                "instructions": "Prefer only safe evidence-backed changes.",
                "user_assertions": ["I led the migration."],
            },
        )
        _assert_status(proposal.status_code, 201, "http_material_proposal")
        body = proposal.json()
        _validate_material_proposal_smoke_response(body)
        steps.append(
            SmokeStep(
                "http_material_proposal",
                "real AI returned a verified material proposal",
            )
        )
    finally:
        del anchor_resume_id, resume_id


def _run_real_ai_opportunity_fit_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    resume_ids: list[int] | None = None,
) -> None:
    anchor_resume_id: int | None = None
    resume_id: int | None = None
    try:
        anchor = client.post(
            "/api/resumes",
            json={"title": "AI Opportunity Fit Smoke Anchor", "text": "", "content_json": {}},
        )
        _assert_status(anchor.status_code, 201, "http_opportunity_fit_resume_anchor")
        anchor_resume_id = int(anchor.json()["id"])
        if resume_ids is not None:
            resume_ids.append(anchor_resume_id)

        created_resume = client.post(
            "/api/resumes",
            json={
                "title": "AI Opportunity Fit Smoke Resume",
                "text": "Built API services and led migration.",
                "content_json": {
                    "raw_text": "Built API services and led migration.",
                    "skills": ["Python"],
                },
            },
        )
        _assert_status(created_resume.status_code, 201, "http_opportunity_fit_resume")
        resume_id = int(created_resume.json()["id"])
        if resume_ids is not None:
            resume_ids.append(resume_id)

        application_response = client.get(f"/api/applications/{application_id}")
        _assert_status(application_response.status_code, 200, "http_opportunity_fit_application")
        application = application_response.json()
        resume_payload = created_resume.json()
        if not isinstance(application, dict) or not isinstance(resume_payload, dict):
            raise RuntimeError("opportunity fit smoke source response was invalid")
        jd_version_id = _save_application_jd_version(
            client,
            application_id,
            "Build reliable API quality workflows.",
            "smoke-opportunity-jd-0001",
        )
        snapshot = build_source_snapshot(
            application_id=application_id,
            company_name=str(application.get("company_name") or ""),
            position_name=str(application.get("position_name") or ""),
            resume_id=resume_id,
            resume_title=str(resume_payload.get("title") or "AI Opportunity Fit Smoke Resume"),
            resume_content={
                "raw_text": "Built API services and led migration.",
                "skills": ["Python"],
            },
            jd_text="Build reliable API quality workflows.",
            jd_source_label="Smoke pasted JD",
            candidate_assertions=["I led the migration."],
        )
        triage_payload = {
            "schema_version": 2,
            "resume_id": resume_id,
            "jd_version_id": jd_version_id,
            "jd_source_label": "Smoke pasted JD",
            "candidate_assertions": ["I led the migration."],
            "idempotency_key": "f36f6d0b-1d1e-4e9a-aec1-9fef6b2f3b90",
        }
        review = client.post(
            f"/api/applications/{application_id}/opportunity-fit-reviews",
            json=triage_payload,
        )
        _assert_status(review.status_code, 201, "http_opportunity_fit_review")
        body = review.json()
        _validate_opportunity_fit_v2_stage_response(
            body,
            application_id=application_id,
            resume_id=resume_id,
            expected_stage="triage",
            expected_status="ready",
            snapshot=snapshot,
        )
        review_id = body["review_id"]
        stage_id = body["stage_id"]
        confirmation_token = body.get("confirmation_token")
        if not isinstance(confirmation_token, str) or not confirmation_token:
            raise RuntimeError("opportunity fit smoke response did not contain a confirmation token")
        confirmed = client.post(
            f"/api/applications/{application_id}/opportunity-fit-reviews/{review_id}/triage/{stage_id}/confirm",
            json={"confirmation_token": confirmation_token},
        )
        _assert_status(confirmed.status_code, 200, "http_opportunity_fit_triage_confirm")
        _validate_opportunity_fit_v2_stage_response(
            confirmed.json(),
            application_id=application_id,
            resume_id=resume_id,
            expected_stage="triage",
            expected_status="confirmed",
            snapshot=snapshot,
        )
        deep_review = client.post(
            f"/api/applications/{application_id}/opportunity-fit-reviews/{review_id}/deep-review",
            json={
                "schema_version": 2,
                "resume_id": resume_id,
                "jd_source_label": "Smoke pasted JD",
                "candidate_assertions": ["I led the migration."],
                "idempotency_key": "4f9b6b33-4f1f-4cb6-87f4-ef5a9c5c9d8b",
                "parent_triage_stage_id": stage_id,
            },
        )
        _assert_status(deep_review.status_code, 201, "http_opportunity_fit_deep_review")
        _validate_opportunity_fit_v2_stage_response(
            deep_review.json(),
            application_id=application_id,
            resume_id=resume_id,
            expected_stage="deep_review",
            expected_status="ready",
            expected_parent_stage_id=stage_id,
            snapshot=snapshot,
        )
        history = client.get(
            f"/api/applications/{application_id}/opportunity-fit-reviews/{review_id}?schema_version=2"
        )
        _assert_status(history.status_code, 200, "http_opportunity_fit_history")
        history_body = history.json()
        if (
            not isinstance(history_body, dict)
            or history_body.get("schema_version") != 2
            or not isinstance(history_body.get("stages"), list)
            or [stage.get("stage") for stage in history_body["stages"] if isinstance(stage, dict)]
            != ["triage", "deep_review"]
        ):
            raise RuntimeError("opportunity fit smoke history was invalid")
        steps.append(
            SmokeStep(
                "http_opportunity_fit_review",
                "real AI returned a verified opportunity fit triage",
            )
        )
        steps.append(
            SmokeStep(
                "http_opportunity_fit_deep_review",
                "real AI returned a verified opportunity fit deep review",
            )
        )
    finally:
        del anchor_resume_id, resume_id


def _validate_opportunity_fit_v2_stage_response(
    body: object,
    *,
    application_id: int,
    resume_id: int,
    expected_stage: str,
    expected_status: str,
    snapshot: dict[str, Any],
    expected_parent_stage_id: int | None = None,
) -> None:
    if not isinstance(body, dict) or "source_snapshot_json" in body:
        raise RuntimeError("opportunity fit v2 smoke response leaked frozen source data")
    expected_fields = {
        "id",
        "review_id",
        "stage_id",
        "application_id",
        "resume_id",
        "jd_version_id",
        "stage",
        "schema_version",
        "stage_status",
        "parent_triage_stage_id",
        "idempotency_key",
        "source_fingerprint_sha256",
        "proposal_sha256",
        "created_at",
        "proposal",
    }
    allowed_fields = expected_fields | ({"confirmation_token"} if expected_stage == "triage" and expected_status == "ready" else set())
    if set(body) != allowed_fields:
        raise RuntimeError("opportunity fit v2 smoke response fields were invalid")
    if any(
        type(body[field]) is not int or body[field] != expected
        for field, expected in (("application_id", application_id), ("resume_id", resume_id))
    ):
        raise RuntimeError("opportunity fit v2 smoke response ownership was invalid")
    if (
        type(body["id"]) is not int
        or body["id"] <= 0
        or type(body["review_id"]) is not int
        or body["review_id"] <= 0
        or body["stage_id"] != body["id"]
        or body["schema_version"] != 2
        or body["stage"] != expected_stage
        or body["stage_status"] != expected_status
        or body["parent_triage_stage_id"] != expected_parent_stage_id
    ):
        raise RuntimeError("opportunity fit v2 smoke response metadata was invalid")
    if not isinstance(body["idempotency_key"], str) or not body["idempotency_key"]:
        raise RuntimeError("opportunity fit v2 smoke response metadata was invalid")
    if not all(
        isinstance(body[field], str) and len(body[field]) == 64
        for field in ("source_fingerprint_sha256", "proposal_sha256")
    ):
        raise RuntimeError("opportunity fit v2 smoke response hashes were invalid")
    if not isinstance(body["created_at"], str) or not body["created_at"]:
        raise RuntimeError("opportunity fit v2 smoke response metadata was invalid")
    proposal = body["proposal"]
    if not isinstance(proposal, dict):
        raise RuntimeError("opportunity fit v2 smoke response did not contain a proposal")
    try:
        validator = validate_triage_v2 if expected_stage == "triage" else validate_deep_review_v2
        validator(proposal, snapshot)
    except OpportunityFitModelError as exc:
        raise RuntimeError("opportunity fit v2 smoke evidence was not traceable") from exc
    if body["source_fingerprint_sha256"] != sha256_text(canonical_json(snapshot)):
        raise RuntimeError("opportunity fit v2 smoke source fingerprint was invalid")
    if body["proposal_sha256"] != sha256_text(canonical_json(proposal)):
        raise RuntimeError("opportunity fit v2 smoke proposal hash was invalid")


def _run_real_ai_interview_review_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
) -> None:
    cases = [
        {
            "marker": "SMOKE_PRIVATE_INTERVIEW_QUESTION",
            "questions": "SMOKE_PRIVATE_INTERVIEW_QUESTION: explain the migration rollback plan",
            "self_reflection": "I gave a concise answer after clarifying the constraint.",
            "difficulty_points": "I needed more time to structure the tradeoff.",
            "mood": "focused",
        },
        {
            "marker": "SMOKE_PRIVATE_INTERVIEW_REFLECTION",
            "questions": "Explain the cache invalidation strategy.",
            "self_reflection": "SMOKE_PRIVATE_INTERVIEW_REFLECTION: I omitted the failure mode initially.",
            "difficulty_points": "I needed more time to structure the tradeoff.",
            "mood": "nervous",
        },
        {
            "marker": "SMOKE_PRIVATE_INTERVIEW_DIFFICULTY",
            "questions": "How would you debug a slow query?",
            "self_reflection": "I asked for a moment to organize the answer.",
            "difficulty_points": "SMOKE_PRIVATE_INTERVIEW_DIFFICULTY: prioritizing the first diagnostic step",
            "mood": "focused",
        },
    ]
    verified_non_empty = 0
    for index, case in enumerate(cases, start=1):
        event = client.post(
            "/api/application-events",
            json={
                "application_id": application_id,
                "event_type": "interview",
                "subtype": "technical",
                "round": index,
                "scheduled_at": f"2026-07-22T{9 + index:02d}:00:00+08:00",
                "duration_minutes": 45,
                "location": "SMOKE_PRIVATE_LOCATION",
            },
        )
        _assert_status(event.status_code, 201, f"http_interview_review_event_{index}")
        event_body = event.json()
        event_id = event_body.get("id") if isinstance(event_body, dict) else None
        if not isinstance(event_id, int):
            raise RuntimeError("interview review smoke did not return an event id")

        note = client.post(
            f"/api/applications/{application_id}/notes",
            json={
                "company": "AI Interview Review Smoke",
                "position": "Verification Engineer",
                "round": "technical",
                "date": "2026-07-22",
                "questions": case["questions"],
                "self_reflection": case["self_reflection"],
                "difficulty_points": case["difficulty_points"],
                "mood": case["mood"],
                "application_event_id": event_id,
            },
        )
        _assert_status(note.status_code, 201, f"http_interview_review_note_{index}")
        note_body = note.json()
        note_id = note_body.get("id") if isinstance(note_body, dict) else None
        if not isinstance(note_id, int):
            raise RuntimeError("interview review smoke did not return a note id")

        proposal = client.post(
            f"/api/notes/{note_id}/interview-review-proposals",
            json={"idempotency_key": f"interview-review-smoke-{index}"},
        )
        _assert_status(proposal.status_code, 201, f"http_interview_review_proposal_{index}")
        body = proposal.json()
        if not isinstance(body, dict) or not isinstance(body.get("proposal"), dict):
            raise RuntimeError("interview review smoke response did not contain a verified proposal")
        serialized = json.dumps(body, ensure_ascii=False)
        cited = _validate_interview_review_smoke_evidence(
            body["proposal"],
            case["marker"],
            {
                "/questions": case["questions"],
                "/self_reflection": case["self_reflection"],
                "/difficulty_points": case["difficulty_points"],
                "/mood": case["mood"],
            },
        )
        if cited:
            verified_non_empty += 1
        if "SMOKE_PRIVATE_LOCATION" in serialized:
            raise RuntimeError("interview review smoke response leaked frozen source data")
        if "input_snapshot_json" in body or "input_snapshot" in body:
            raise RuntimeError("interview review smoke response exposed the input snapshot")
    if verified_non_empty < 1:
        raise RuntimeError("interview review smoke returned no evidence-backed non-empty proposal")
    steps.append(
        SmokeStep(
            "http_interview_review_proposal",
            "real AI returned three safe interview review proposals with at least one cited result",
        )
    )


def _run_real_ai_interview_knowledge_capture_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
) -> None:
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application_id,
            "event_type": "interview",
            "subtype": "knowledge-capture",
            "scheduled_at": "2026-07-24T10:00:00+08:00",
            "duration_minutes": 45,
        },
    )
    _assert_status(event.status_code, 201, "http_interview_knowledge_event")
    event_id = event.json().get("id")
    if not isinstance(event_id, int):
        raise RuntimeError("interview knowledge smoke did not return an event id")
    note = client.post(
        f"/api/applications/{application_id}/notes",
        json={
            "company": "AI Interview Knowledge Smoke",
            "position": "Verification Engineer",
            "round": "technical",
            "date": "2026-07-24",
            "questions": "I explained the rollback plan and the observable safety signal.",
            "self_reflection": "I should have stated the tradeoff before the implementation detail.",
            "difficulty_points": "I needed a moment to structure the tradeoff.",
            "mood": "focused",
            "application_event_id": event_id,
        },
    )
    _assert_status(note.status_code, 201, "http_interview_knowledge_note")
    note_id = note.json().get("id")
    if not isinstance(note_id, int):
        raise RuntimeError("interview knowledge smoke did not return a note id")
    selected = [
        {
            "fragment_id": "smoke-question",
            "path": "/questions",
            "start": 2,
            "end": 63,
            "text": "explained the rollback plan and the observable safety signal.",
        },
        {
            "fragment_id": "smoke-reflection",
            "path": "/self_reflection",
            "start": 0,
            "end": 67,
            "text": "I should have stated the tradeoff before the implementation detail.",
        },
    ]
    preview = client.post(
        f"/api/notes/{note_id}/knowledge-capture/preview",
        json={"attempt_key": "real-ai-interview-knowledge", "mode": "ai", "selected_fragments": selected},
    )
    _assert_status(preview.status_code, 200, "http_interview_knowledge_preview")
    preview_body = preview.json()
    if not isinstance(preview_body, dict) or not isinstance(preview_body.get("preview"), dict):
        raise RuntimeError("interview knowledge smoke preview was not an object")
    if "input_snapshot" in preview_body or "source_fields" in preview_body:
        raise RuntimeError("interview knowledge smoke exposed the input snapshot")
    confirm = client.post(
        f"/api/notes/{note_id}/knowledge-capture/confirm",
        json={
            "attempt_key": preview_body["attempt_key"],
            "note_fingerprint": preview_body["note_fingerprint"],
            "title": "Interview rollback reflection",
            "blocks": preview_body["preview"].get("blocks", []),
        },
    )
    _assert_status(confirm.status_code, 201, "http_interview_knowledge_confirm")
    confirmed = client.get("/api/knowledge/notes")
    _assert_status(confirmed.status_code, 200, "http_interview_knowledge_history")
    if not confirmed.json().get("items"):
        raise RuntimeError("interview knowledge smoke did not return confirmed history")
    steps.append(
        SmokeStep(
            "http_interview_knowledge_capture",
            "real AI preview was reviewed and confirmed into frozen interview knowledge",
        )
    )


def _run_real_ai_mock_interview_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    resume_ids: list[int],
    data_dir: Path,
) -> None:
    """Exercise bounded user restarts and one complete two-turn interview flow."""
    outcomes: list[str] = []
    resume = client.post(
        "/api/resumes",
        json={
            "title": "Mock Interview Smoke Resume",
            "text": "Built a Python service and explained the rollback plan.",
            "content_json": {"raw_text": "Built a Python service and explained the rollback plan."},
        },
    )
    _assert_status(resume.status_code, 201, "http_mock_interview_resume")
    resume_id = int(resume.json()["id"])
    resume_ids.append(resume_id)
    event = client.post(
        "/api/application-events",
        json={
            "application_id": application_id,
            "event_type": "interview",
            "subtype": "mock-interview",
            "scheduled_at": "2026-07-28T10:00:00Z",
            "duration_minutes": 30,
        },
    )
    _assert_status(event.status_code, 201, "http_mock_interview_event")
    event_id = int(event.json()["id"])
    jd_version_id = _save_application_jd_version(
        client,
        application_id,
        "需要能够维护 Python 服务并说明可靠性取舍。",
        "smoke-mock-interview-jd-0001",
    )
    baseline = _capture_real_ai_browser_domain_baseline(
        data_dir, application_id, [event_id], [resume_id]
    )
    base = f"/api/applications/{application_id}/events/{event_id}/mock-interview/attempts"

    for index in range(3):
        def _record_failed_attempt(
            stage: str,
            error_code: str,
            attempt_id: int | None,
            attempt_retention: str,
        ) -> None:
            if attempt_retention.startswith("retained") and attempt_id is None:
                raise RuntimeError("retained mock interview failure omitted attempt_id")
            _assert_real_ai_browser_no_cross_domain_writes(
                data_dir, application_id, baseline, [event_id], [resume_id]
            )
            diagnostic_stage = "feedback" if stage == "feedback" else "question"
            diagnostic = (
                _latest_mock_interview_failure_diagnostic(data_dir, attempt_id, diagnostic_stage)
                if attempt_id is not None
                else None
            )
            if (
                attempt_id is None
                or diagnostic is None
                or diagnostic.get("kind") not in {"provider", "contract"}
                or not diagnostic.get("category")
            ):
                raise RuntimeError("mock interview failure diagnostic missing")
            outcomes.append(
                f"attempt_{index + 1}:{stage}:{error_code}:"
                f"{diagnostic['category'] or 'unknown'}"
            )

        def reconcile_step(
            response: Any, stage: str, replay: Callable[[], Any] | None = None
        ) -> tuple[Any, bool]:
            """Apply the recovery contract once: bounded same-key replay, else restart."""
            policy = _response_recovery_policy(response)
            if policy is None:
                return response, False
            error_code = str(response.json().get("error_code"))
            if (
                policy.disposition == "retry_same_key"
                and policy.preserve_idempotency_key
                and policy.provider_retry_allowed
                and replay is not None
            ):
                reconciled = replay()
                if reconciled.status_code in {200, 201}:
                    outcomes.append(f"attempt_{index + 1}:{stage}:reconciled_same_key")
                    return reconciled, False
                response = reconciled
                replay_policy = _response_recovery_policy(response)
                if replay_policy is not None:
                    policy = replay_policy
                    error_code = str(response.json().get("error_code"))
            if policy.disposition in {"terminal_no_retry", "retry_same_key", "restart_new_attempt"}:
                body = response.json()
                raw_attempt_id = body.get("attempt_id") if isinstance(body, dict) else None
                attempt_id = raw_attempt_id if isinstance(raw_attempt_id, int) else None
                _record_failed_attempt(
                    stage,
                    error_code,
                    attempt_id,
                    policy.attempt_retention,
                )
                return response, True
            raise RuntimeError(
                f"real-ai mock interview smoke cannot apply disposition "
                f"{policy.disposition!r} for {error_code}"
            )

        start_payload = {
            "resume_id": resume_id,
            "jd_version_id": jd_version_id,
            "attempt_idempotency_key": f"real-ai-mock-attempt-{index}",
            "initial_question_idempotency_key": f"real-ai-mock-question-{index}",
        }
        start, restart = reconcile_step(
            client.post(base, json=start_payload),
            "start",
            lambda: client.post(base, json=start_payload),
        )
        if restart:
            continue
        _assert_status(start.status_code, 201, f"http_mock_interview_start_{index}")
        started = start.json()
        attempt_id = int(started["attempt_id"])
        answer = client.post(
            f"{base}/{attempt_id}/turns",
            json={
                "turn_no": 1,
                "answer_text": "我负责过 Python 服务的发布与回滚，并用指标确认风险。",
                "turn_idempotency_key": f"real-ai-mock-answer-{index}",
            },
        )
        _assert_status(answer.status_code, 200, f"http_mock_interview_answer_{index}")
        question_payload = {"question_idempotency_key": f"real-ai-mock-question-2-{index}"}
        question, restart = reconcile_step(
            client.post(f"{base}/{attempt_id}/turns/2/question", json=question_payload),
            "question_2",
            lambda: client.post(f"{base}/{attempt_id}/turns/2/question", json=question_payload),
        )
        if restart:
            continue
        _assert_status(question.status_code, 201, f"http_mock_interview_question_2_{index}")
        answer_two = client.post(
            f"{base}/{attempt_id}/turns",
            json={
                "turn_no": 2,
                "answer_text": "我确认了回滚后的服务指标与数据一致性。",
                "turn_idempotency_key": f"real-ai-mock-answer-2-{index}",
            },
        )
        _assert_status(answer_two.status_code, 200, f"http_mock_interview_answer_2_{index}")
        finish_payload = {"feedback_idempotency_key": f"real-ai-mock-feedback-{index}"}
        finish, restart = reconcile_step(
            client.post(f"{base}/{attempt_id}/finish", json=finish_payload),
            "feedback",
            lambda: client.post(f"{base}/{attempt_id}/finish", json=finish_payload),
        )
        if restart:
            continue
        _assert_status(finish.status_code, 201, f"http_mock_interview_finish_{index}")
        body = finish.json()
        if set(body) != {"proposal_id", "proposal_status", "proposal_hash", "proposal", "operation_id"}:
            raise RuntimeError("mock interview feedback response exposed non-public fields")
        if not isinstance(body.get("operation_id"), str) or not body["operation_id"]:
            raise RuntimeError("mock interview feedback response missed the trace operation_id")
        if body["proposal_status"] not in {"normal", "safe_empty"}:
            raise RuntimeError("mock interview feedback returned an invalid status")
        selected_block = _first_mock_interview_feedback_block(body["proposal"])
        if selected_block is None:
            deleted = client.delete(f"{base}/{attempt_id}")
            _assert_status(deleted.status_code, 200, f"http_mock_interview_cleanup_{index}_safe_empty")
            _assert_mock_interview_failed_attempt_clean(
                data_dir, application_id, event_id, resume_id, attempt_id, baseline
            )
            outcomes.append(f"attempt_{index + 1}:safe_empty")
            continue
        draft = client.post(
            f"{base}/{attempt_id}/review-drafts",
            json={
                "proposal_id": body["proposal_id"],
                "confirmation_idempotency_key": f"real-ai-mock-confirm-{index}",
                "selected_blocks": [selected_block],
            },
        )
        _assert_status(draft.status_code, 201, f"http_mock_interview_confirm_{index}")
        history = client.get(base)
        _assert_status(history.status_code, 200, f"http_mock_interview_history_{index}")
        history_items = history.json().get("items")
        if not isinstance(history_items, list) or not any(
            item.get("attempt_id") == attempt_id
            and isinstance(item.get("turns"), list)
            and len(item["turns"]) >= 2
            and isinstance(item.get("review_draft"), dict)
            and item["review_draft"].get("status") == "confirmed"
            for item in history_items
            if isinstance(item, dict)
        ):
            raise RuntimeError("mock interview feedback history was empty")
        _assert_mock_interview_attempt_context(
            data_dir, attempt_id, application_id, event_id, resume_id
        )
        _assert_real_ai_browser_no_cross_domain_writes(
            data_dir, application_id, baseline, [event_id], [resume_id]
        )
        outcomes.append(f"attempt_{index + 1}:success")
        break
    if not outcomes or not outcomes[-1].endswith(":success"):
        raise RuntimeError(
            "mock interview real-ai attempts did not complete: "
            + json.dumps(outcomes, ensure_ascii=True, separators=(",", ":"))
        )
    steps.append(
        SmokeStep(
            "http_mock_interview",
            "bounded mock-interview attempts: "
            + json.dumps(outcomes, ensure_ascii=True, separators=(",", ":")),
        )
    )
    _assert_mock_interview_traces_sanitized(data_dir, body["operation_id"])


def _assert_mock_interview_traces_sanitized(data_dir: Path, success_operation_id: str) -> None:
    """Every provider operation must leave a complete, redacted trace envelope."""
    traces = read_mock_interview_traces(data_dir)
    if not traces:
        raise RuntimeError("mock interview real-ai smoke produced no trace envelopes")
    allowed_stages = {"question", "feedback"}
    allowed_outcomes = {"success", "success_after_repair", "unverifiable", "provider_error"}
    forbidden_fragments = (
        "需要能够维护",
        "我负责过",
        "我确认了",
        "API key",
        "real-ai-mock-attempt-",
        "real-ai-mock-feedback-",
    )
    for trace in traces:
        if trace.get("validator_stage") not in allowed_stages:
            raise RuntimeError("mock interview trace envelope has an unknown validator stage")
        if trace.get("provider_outcome") not in allowed_outcomes:
            raise RuntimeError("mock interview trace envelope has an unknown provider outcome")
        if not str(trace.get("idempotency_key_hash", "")).startswith("idem-"):
            raise RuntimeError("mock interview trace envelope leaked an unhashed idempotency key")
        if trace.get("final_disposition") == "success" and trace.get("response_error_code"):
            raise RuntimeError("mock interview trace envelope mixed success with an error code")
        serialized = json.dumps(trace, ensure_ascii=False)
        for fragment in forbidden_fragments:
            if fragment in serialized:
                raise RuntimeError("mock interview trace envelope leaked sensitive content")
    if not any(
        trace.get("operation_id") == success_operation_id
        and trace.get("validator_stage") == "feedback"
        and trace.get("provider_outcome") in {"success", "success_after_repair"}
        for trace in traces
    ):
        raise RuntimeError("the successful feedback operation is missing from the trace envelopes")


def run_mock_interview_real_ai_smoke(
    source_data_dir: Path,
    static_dir: Path | None = None,
) -> SmokeReport:
    """Run only the Mock Interview real-AI smoke in an isolated data directory."""
    steps: list[SmokeStep] = []
    with tempfile.TemporaryDirectory(
        prefix="offerpilot-mock-interview-real-ai-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        isolated_data_dir = Path(temp_dir)
        _copy_real_ai_config(source_data_dir, isolated_data_dir)
        app = create_app(data_dir=isolated_data_dir, static_dir=static_dir)
        application_id: int | None = None
        resume_ids: list[int] = []
        cleanup_safe = True
        try:
            with _running_server(app) as base_url:
                with httpx.Client(base_url=base_url, timeout=90.0) as client:
                    settings = client.get("/api/settings")
                    _assert_status(settings.status_code, 200, "mock_interview_real_ai_settings")
                    if not bool(settings.json().get("has_api_key")):
                        raise RuntimeError("mock-interview real-ai smoke requires a configured API key")
                    created = client.post(
                        "/api/applications",
                        json={
                            "company_name": "Mock Interview Real AI Smoke",
                            "position_name": "Verification Engineer",
                            "status": "interview",
                            "source": "smoke",
                        },
                    )
                    _assert_status(created.status_code, 201, "mock_interview_real_ai_application")
                    application_id = int(created.json()["id"])
                    _run_real_ai_mock_interview_smoke(
                        client, steps, application_id, resume_ids, isolated_data_dir
                    )
        except SmokeServerShutdownError:
            cleanup_safe = False
            raise
        finally:
            if cleanup_safe:
                _dispose_smoke_app_database(app)
                if application_id is not None:
                    _cleanup_real_ai_smoke_records(isolated_data_dir, application_id, resume_ids)
                _assert_real_ai_smoke_data_clean(isolated_data_dir)
        return SmokeReport(ok=True, steps=steps)


def run_offer_negotiation_real_ai_smoke(
    source_data_dir: Path,
    static_dir: Path | None = None,
) -> SmokeReport:
    """Run the Offer negotiation real-AI API flow in an isolated data directory."""
    steps: list[SmokeStep] = []
    with tempfile.TemporaryDirectory(
        prefix="offerpilot-offer-negotiation-real-ai-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        isolated_data_dir = Path(temp_dir)
        _copy_real_ai_config(source_data_dir, isolated_data_dir)
        app = create_app(data_dir=isolated_data_dir, static_dir=static_dir)
        cleanup_safe = True
        try:
            with _running_server(app) as base_url:
                with httpx.Client(base_url=base_url, timeout=120.0) as client:
                    settings = client.get("/api/settings")
                    _assert_status(settings.status_code, 200, "offer_negotiation_real_ai_settings")
                    if not bool(settings.json().get("has_api_key")):
                        raise RuntimeError("offer negotiation real-ai smoke requires a configured API key")
                    first = client.post(
                        "/api/offers",
                        json={
                            "company_name": "星云数据",
                            "position_name": "后端工程师",
                            "base_monthly": 28000,
                            "months_per_year": 12,
                            "signing_bonus": 0,
                        },
                    )
                    second = client.post(
                        "/api/offers",
                        json={
                            "company_name": "远山科技",
                            "position_name": "平台工程师",
                            "base_monthly": 30000,
                            "months_per_year": 12,
                            "signing_bonus": 0,
                        },
                    )
                    _assert_status(first.status_code, 201, "offer_negotiation_real_ai_first_offer")
                    _assert_status(second.status_code, 201, "offer_negotiation_real_ai_second_offer")
                    offer_id = int(first.json()["id"])
                    second_offer_id = int(second.json()["id"])
                    dimension = client.post(
                        "/api/offers/comparison-dimensions", json={"label": "通勤"}
                    )
                    _assert_status(dimension.status_code, 201, "offer_negotiation_real_ai_dimension")
                    dimension_id = int(dimension.json()["id"])
                    for target_id, value in ((offer_id, "地铁35分钟"), (second_offer_id, "公交50分钟")):
                        saved = client.put(
                            f"/api/offers/{target_id}/comparison-values/{dimension_id}",
                            json={"value_text": value},
                        )
                        _assert_status(saved.status_code, 200, "offer_negotiation_real_ai_dimension_value")

                    chat_before = _chat_domain_counts(isolated_data_dir)
                    payload = {
                        "idempotency_key": "offer-real-ai-000001",
                        "dimension_ids": [dimension_id],
                        "goal": "争取明确入职时间",
                        "concerns": "通勤安排",
                        "scenario": "与招聘方电话沟通",
                    }
                    preview = client.post(
                        f"/api/offers/{offer_id}/negotiation/preview",
                        json={key: payload[key] for key in ("dimension_ids", "goal", "concerns", "scenario")},
                    )
                    _assert_status(preview.status_code, 200, "offer_negotiation_real_ai_preview")
                    preview_body = preview.json()
                    source_fingerprint = preview_body.get("source_fingerprint")
                    if not isinstance(source_fingerprint, str) or not source_fingerprint:
                        raise RuntimeError("offer negotiation real-ai preview had no source fingerprint")
                    payload["source_fingerprint"] = source_fingerprint
                    result = client.post(f"/api/offers/{offer_id}/negotiation/proposals", json=payload)
                    if result.status_code == 502 and result.json().get("error_code") == "offer_negotiation_provider_error":
                        result = client.post(f"/api/offers/{offer_id}/negotiation/proposals", json=payload)
                    if result.status_code not in {200, 201}:
                        code = result.json().get("error_code", "unknown")
                        category = "unknown"
                        try:
                            for entry in client.get("/api/logs?limit=20").json().get("entries", []):
                                message = str(entry.get("message", ""))
                                if message.startswith("offer_negotiation_diagnostic "):
                                    category = str(json.loads(message.split(" ", 1)[1]).get("failure_category") or "unknown")
                                    break
                        except (ValueError, TypeError, httpx.HTTPError):
                            category = "unknown"
                        raise RuntimeError(
                            f"offer negotiation real-ai proposal failed: {result.status_code}:{code}:{category}"
                        )
                    proposal_body = result.json()
                    if proposal_body.get("proposal_status") != "normal":
                        raise RuntimeError("offer negotiation real-ai returned safe_empty")
                    proposal_id = int(proposal_body["id"])
                    proposal = proposal_body.get("proposal") or {}
                    block_ids = [
                        item["id"]
                        for field in ("communication_goals", "clarification_questions", "talking_points", "preparation_checks")
                        for item in proposal.get(field, [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    ]
                    if not block_ids:
                        raise RuntimeError("offer negotiation real-ai returned no confirmable blocks")
                    confirmed = client.post(
                        f"/api/offer-negotiation/proposals/{proposal_id}/confirm",
                        json={
                            "confirmation_key": "offer-confirm-000001",
                            "selected_blocks": block_ids[:2],
                            "edited_content": {},
                        },
                    )
                    _assert_status(confirmed.status_code, 201, "offer_negotiation_real_ai_confirm")
                    replay = client.get(f"/api/offer-negotiation/proposals/{proposal_id}")
                    _assert_status(replay.status_code, 200, "offer_negotiation_real_ai_history")
                    if replay.json().get("brief") is None:
                        raise RuntimeError("offer negotiation real-ai history has no confirmed Brief")
                    updated = client.put(
                        f"/api/offers/{offer_id}",
                        json={
                            "company_name": "星云数据",
                            "position_name": "后端工程师",
                            "base_monthly": 28000,
                            "months_per_year": 12,
                            "signing_bonus": 0,
                            "notes": "source changed after confirmation",
                        },
                    )
                    _assert_status(updated.status_code, 200, "offer_negotiation_real_ai_source_change")
                    changed = client.get(f"/api/offer-negotiation/proposals/{proposal_id}")
                    _assert_status(changed.status_code, 200, "offer_negotiation_real_ai_changed_history")
                    if changed.json().get("source_changed") is not True:
                        raise RuntimeError("offer negotiation real-ai history did not mark source_changed")
                    if _chat_domain_counts(isolated_data_dir) != chat_before:
                        raise RuntimeError("offer negotiation real-ai wrote Chat data")
                    steps.append(SmokeStep("http_offer_negotiation", "isolated Offer negotiation API flow passed"))
        except SmokeServerShutdownError:
            cleanup_safe = False
            raise
        finally:
            if cleanup_safe:
                _dispose_smoke_app_database(app)
        return SmokeReport(ok=True, steps=steps)


def run_application_jd_smoke(
    source_data_dir: Path,
    static_dir: Path | None = None,
    *,
    real_ai: bool = False,
) -> SmokeReport:
    """Verify the isolated Application-bound JD version contract.

    This smoke deliberately keeps the JD save path independent from Provider calls.  The
    optional real_ai flag only controls which configuration is copied into the isolated
    directory; downstream consumer runs belong to the browser harness and are reported
    separately so a Provider failure cannot make a database-only contract pass.
    """
    from fastapi.testclient import TestClient

    steps: list[SmokeStep] = []
    prefix = "offerpilot-application-jd-real-ai-" if real_ai else "offerpilot-application-jd-local-"
    with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
        isolated_data_dir = Path(temp_dir)
        if real_ai:
            _copy_real_ai_config(source_data_dir, isolated_data_dir)
        app = create_app(data_dir=isolated_data_dir, static_dir=static_dir)
        client = TestClient(app).__enter__()
        application = client.post(
            "/api/applications",
            json={"company_name": "筱哲案例公司", "position_name": "后端工程师", "status": "applied"},
        )
        _assert_status(application.status_code, 201, "application_jd_application")
        application_id = int(application.json()["id"])
        first = client.post(
            f"/api/applications/{application_id}/job-description/versions",
            json={
                "jd_text": "负责 API 设计与服务稳定性建设。",
                "source_url": "https://example.invalid/jd/1",
                "expected_current_version_id": None,
                "idempotency_key": "application-jd-smoke-0001",
            },
        )
        _assert_status(first.status_code, 201, "application_jd_first_save")
        first_id = int(first.json()["id"])
        second = client.post(
            f"/api/applications/{application_id}/job-description/versions",
            json={
                "jd_text": "负责 API 设计、服务稳定性建设与团队协作。",
                "source_url": "https://example.invalid/jd/2",
                "expected_current_version_id": first_id,
                "idempotency_key": "application-jd-smoke-0002",
            },
        )
        _assert_status(second.status_code, 201, "application_jd_second_save")
        second_id = int(second.json()["id"])
        current = client.get(f"/api/applications/{application_id}/job-description")
        _assert_status(current.status_code, 200, "application_jd_current")
        if current.json().get("current", {}).get("id") != second_id:
            raise RuntimeError("application JD current version did not advance")
        history = client.get(f"/api/applications/{application_id}/job-description/versions")
        _assert_status(history.status_code, 200, "application_jd_history")
        if any("jd_text" in item for item in history.json()):
            raise RuntimeError("application JD history leaked full text")
        steps.extend([
            SmokeStep("application_jd_v1", "created UI-owned JD version 1"),
            SmokeStep("application_jd_v2", "created UI-owned JD version 2 with current-version CAS"),
            SmokeStep("application_jd_history", "history returned metadata and preview without full text"),
        ])
        verification_factory = session_factory_for_data_dir(isolated_data_dir)
        try:
            with verification_factory() as session:
                if len(list(session.scalars(select(ApplicationJDVersion)))) != 2:
                    raise RuntimeError("isolated smoke did not retain exactly two JD versions")
        finally:
            verification_engine = verification_factory.kw.get("bind")
            if verification_engine is not None:
                verification_engine.dispose()
        deleted = client.delete(f"/api/applications/{application_id}")
        _assert_status(deleted.status_code, 200, "application_jd_cleanup")
        client.__exit__(None, None, None)
        cleanup_factory = session_factory_for_data_dir(isolated_data_dir)
        try:
            with cleanup_factory() as session:
                # Application deletion is a soft delete in the existing API, so the
                # version rows need explicit smoke cleanup.  Do this before disposing
                # the app engine; otherwise a second, undisposed engine can keep the
                # temporary SQLite file locked on Windows.
                session.execute(
                    delete(ApplicationJDVersion).where(
                        ApplicationJDVersion.application_id == application_id
                    )
                )
                session.commit()
                if list(session.scalars(select(ApplicationJDVersion))):
                    raise RuntimeError("application JD cleanup left version rows")
        finally:
            cleanup_engine = cleanup_factory.kw.get("bind")
            if cleanup_engine is not None:
                cleanup_engine.dispose()
        _dispose_smoke_app_database(app)
        steps.append(SmokeStep("application_jd_cleanup", "temporary Application and JD versions were removed"))
    return SmokeReport(ok=True, steps=steps)


def run_interview_story_smoke(
    source_data_dir: Path,
    static_dir: Path | None = None,
    *,
    real_ai: bool = False,
) -> SmokeReport:
    """Run isolated Interview Story API verification.

    This intentionally verifies only the Story aggregate.  It is not a
    replacement for full ``verify`` or browser/CDP release evidence.
    """

    prefix = "offerpilot-interview-story-real-ai-" if real_ai else "offerpilot-interview-story-local-"
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as temp_dir:
        isolated_data_dir = Path(temp_dir)
        if real_ai:
            _copy_real_ai_config(source_data_dir, isolated_data_dir)
        app = create_app(
            data_dir=isolated_data_dir,
            static_dir=static_dir,
            chat_model=None if real_ai else _InterviewStorySmokeChatModel(),
        )
        seed: dict[str, int] | None = None
        cleanup_safe = True
        try:
            seed = _seed_interview_story_smoke_context(isolated_data_dir)
            steps: list[SmokeStep] = []
            with _running_server(app) as base_url:
                with httpx.Client(base_url=base_url, timeout=120.0) as client:
                    settings = client.get("/api/settings")
                    _assert_status(settings.status_code, 200, "story_smoke_settings")
                    if real_ai and not bool(settings.json().get("has_api_key")):
                        raise RuntimeError("interview story real-ai smoke requires a configured API key")
                    _run_interview_story_http_smoke(
                        client, isolated_data_dir, seed, steps, exercise_recovery=not real_ai
                    )
            return SmokeReport(ok=True, steps=steps)
        except SmokeServerShutdownError:
            cleanup_safe = False
            raise
        finally:
            if cleanup_safe:
                _dispose_smoke_app_database(app)
                if seed is not None:
                    _cleanup_interview_story_smoke_records(isolated_data_dir, seed)
                _assert_interview_story_smoke_data_clean(isolated_data_dir)


def _seed_interview_story_smoke_context(data_dir: Path) -> dict[str, int]:
    """Create all non-Story source records before taking Story-domain actions."""

    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            application = Application(
                company_name="星云数据",
                position_name="后端工程师",
                status="interview",
                source="smoke",
            )
            session.add(application)
            session.flush()
            event = ApplicationEvent(
                application_id=application.id,
                event_type="interview",
                subtype="technical",
                scheduled_at=datetime.now(timezone.utc),
                duration_minutes=45,
                status="done",
            )
            resume = Resume(
                name="筱哲",
                title="后端工程师简历",
                content_json=json.dumps({"项目": {"内容": "负责延迟排查和风险同步"}}, ensure_ascii=False),
            )
            session.add_all([event, resume])
            session.flush()
            note = InterviewNote(
                application_id=application.id,
                application_event_id=event.id,
                company="星云数据",
                position="后端工程师",
                questions="如何排查线上延迟？",
                self_reflection="我先确认指标，再同步风险。",
                difficulty_points="需要补充量化结果。",
                mood="平静",
            )
            attempt = MockInterviewAttempt(
                application_id=application.id,
                event_id=event.id,
                resume_id=resume.id,
                idempotency_key="story-smoke-mock-attempt",
                input_snapshot_json="{}",
                source_fingerprint="story-smoke-mock",
                attempt_status="feedback_ready",
                transcript_fingerprint="story-smoke-transcript",
                completed_at=datetime.now(timezone.utc),
            )
            session.add_all([note, attempt])
            session.flush()
            turn = MockInterviewTurn(
                attempt_id=attempt.id,
                turn_no=1,
                question_idempotency_key="story-smoke-question",
                turn_idempotency_key="story-smoke-answer",
                question_text="请介绍一次线上问题排查。",
                answer_text="我通过分段定位解决了延迟问题。",
                turn_status="answered",
            )
            session.add(turn)
            session.commit()
            return {
                "application_id": application.id,
                "event_id": event.id,
                "resume_id": resume.id,
                "note_id": note.id,
                "mock_attempt_id": attempt.id,
            }
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _run_interview_story_http_smoke(
    client: httpx.Client,
    data_dir: Path,
    seed: dict[str, int],
    steps: list[SmokeStep],
    *,
    exercise_recovery: bool,
) -> None:
    note_source = {
        "source_kind": "interview_note",
        "source_id": seed["note_id"],
        "source_path": "/questions",
        "excerpt": "如何排查线上延迟？",
    }
    manual_content = {
        "title": "筱哲的线上延迟排查",
        "blocks": [{"kind": "situation", "text": "线上出现延迟", "fact_mode": "evidence_backed"}],
        "capability_labels": ["问题定位"],
        "applicable_questions": ["请介绍一次线上问题排查。"],
        "fact_gap_codes": ["missing_result"],
    }
    manual_links = [
        {"target_kind": "title", "target_id": "title", **note_source},
        {"target_kind": "block", "target_id": "situation_001", **note_source},
        {"target_kind": "capability_label", "target_id": "capability_001", **note_source},
        {"target_kind": "applicable_question", "target_id": "question_001", **note_source},
    ]
    manual = client.post(
        "/api/interview-stories",
        json={
            "content": manual_content,
            "evidence_links": manual_links,
            "selections": [
                {"source_kind": "interview_note", "source_id": seed["note_id"], "path": "/questions"}
            ],
            "assertions": [],
            "expected_current_version_id": None,
            "idempotency_key": "story-manual-smoke-00001",
        },
    )
    _assert_status(manual.status_code, 201, "story_manual_create")
    story = manual.json()
    story_id = int(story["id"])
    archived = client.post(
        f"/api/interview-stories/{story_id}/archive",
        json={"expected_story_revision": story["story_revision"]},
    )
    _assert_status(archived.status_code, 200, "story_manual_archive")
    restored = client.post(
        f"/api/interview-stories/{story_id}/restore",
        json={"expected_story_revision": archived.json()["story_revision"]},
    )
    _assert_status(restored.status_code, 200, "story_manual_restore")
    steps.append(SmokeStep("story_manual_lifecycle", "manual Story archive and restore passed"))

    selections = [
        {"source_kind": "interview_note", "source_id": seed["note_id"], "path": "/questions"},
        {"source_kind": "resume_version", "source_id": seed["resume_id"], "path": "/content_json/项目/内容"},
        {"source_kind": "mock_turn", "source_id": seed["mock_attempt_id"], "path": "/turns/001/answer"},
    ]
    chat_before = _chat_domain_counts(data_dir)
    ui = _create_and_confirm_story_proposal(
        client,
        endpoint="/api/interview-story-proposals",
        idempotency_key="story-ui-smoke-000001",
        confirmation_token="story-ui-confirm-0001",
        story=restored.json(),
        selections=selections,
        assertions=["我确认这是我亲自负责的排查经历。"],
    )
    steps.append(SmokeStep("story_ui_proposal_confirm", f"UI proposal {ui['attempt_id']} confirmed"))
    updated_story = client.get(f"/api/interview-stories/{story_id}")
    _assert_status(updated_story.status_code, 200, "story_after_ui_confirm")
    pilot = _create_and_confirm_story_proposal(
        client,
        endpoint="/api/pilot/interview-story-proposals",
        idempotency_key="story-pilot-smoke-0001",
        confirmation_token="story-pilot-confirm-01",
        story=updated_story.json(),
        selections=[{"source_kind": "interview_note", "source_id": seed["note_id"], "path": "/questions"}],
        assertions=["我确认这是我亲自负责的排查经历。"],
        entry_context={"review_note_id": seed["note_id"]},
    )
    if ui["attempt_id"] == pilot["attempt_id"]:
        raise RuntimeError("Story UI and Pilot reused the same attempt")
    steps.append(SmokeStep("story_pilot_proposal_confirm", f"Pilot proposal {pilot['attempt_id']} confirmed"))
    if _chat_domain_counts(data_dir) != chat_before:
        raise RuntimeError("Story smoke wrote Chat data")
    steps.append(SmokeStep("story_chat_isolation", "Story UI and Pilot wrappers wrote no Chat data"))

    if exercise_recovery:
        latest_story = client.get(f"/api/interview-stories/{story_id}")
        _assert_status(latest_story.status_code, 200, "story_recovery_current_story")
        versions_before_terminal = client.get(f"/api/interview-stories/{story_id}/versions")
        _assert_status(versions_before_terminal.status_code, 200, "story_terminal_versions_before")
        version_count_before_terminal = len(versions_before_terminal.json())
        recovery_payload = {
            "target_story_id": story_id,
            "expected_current_version_id": latest_story.json()["current_version_id"],
            "expected_story_revision": latest_story.json()["story_revision"],
            "selections": selections,
            "assertions": ["我确认这是我亲自负责的排查经历。"],
            "idempotency_key": "story-provider-unknown-0001",
        }
        session_factory = session_factory_for_data_dir(data_dir)
        try:
            repository = InterviewStoriesRepository(session_factory)
            claim = repository.claim_proposal(
                target_story_id=recovery_payload["target_story_id"],
                expected_current_version_id=recovery_payload["expected_current_version_id"],
                expected_story_revision=recovery_payload["expected_story_revision"],
                selections=selections,
                assertions=recovery_payload["assertions"],
                idempotency_key=recovery_payload["idempotency_key"],
                entrypoint="ui",
            )
            if not claim.should_call_provider or not repository.mark_provider_unknown(
                attempt_id=claim.attempt_id,
                generation_revision=claim.generation_revision,
                provider_call_token=claim.provider_call_token,
                category="provider_unknown",
            ):
                raise RuntimeError("Story smoke could not seed provider-unknown recovery")
            live_lease = client.post("/api/interview-story-proposals", json=recovery_payload)
            _assert_status(live_lease.status_code, 202, "story_provider_unknown_live_lease")
            if live_lease.json().get("id") != claim.attempt_id or live_lease.json().get("attempt_status") != "provider_unknown":
                raise RuntimeError("Story live provider-unknown lease did not retain the original attempt")
            with session_factory() as session:
                attempt = session.get(InterviewStoryProposalAttempt, claim.attempt_id)
                if attempt is None:
                    raise RuntimeError("Story provider-unknown attempt is missing")
                attempt.provider_lease_until = datetime(1970, 1, 1)
                session.commit()
            recovered = client.post("/api/interview-story-proposals", json=recovery_payload)
            _assert_status(recovered.status_code, 201, "story_provider_unknown_replay")
            if recovered.json().get("id") != claim.attempt_id or recovered.json().get("attempt_status") != "ready":
                raise RuntimeError("Story provider-unknown replay did not retain the original attempt")

            terminal_key = "story-unverifiable-00001"
            terminal_claim = repository.claim_proposal(
                target_story_id=recovery_payload["target_story_id"],
                expected_current_version_id=recovery_payload["expected_current_version_id"],
                expected_story_revision=recovery_payload["expected_story_revision"],
                selections=selections,
                assertions=recovery_payload["assertions"],
                idempotency_key=terminal_key,
                entrypoint="ui",
            )
            if not terminal_claim.should_call_provider or not repository.mark_contract_failed(
                attempt_id=terminal_claim.attempt_id,
                generation_revision=terminal_claim.generation_revision,
                provider_call_token=terminal_claim.provider_call_token,
                category="invalid_evidence_shape",
            ):
                raise RuntimeError("Story smoke could not seed terminal contract failure")
            terminal_payload = dict(recovery_payload, idempotency_key=terminal_key)
            terminal = client.post("/api/interview-story-proposals", json=terminal_payload)
            if terminal.status_code != 502 or terminal.json().get("error_code") != "story_unverifiable":
                raise RuntimeError("Story terminal contract failure was not stable")
            versions_after_terminal = client.get(f"/api/interview-stories/{story_id}/versions")
            _assert_status(versions_after_terminal.status_code, 200, "story_terminal_versions_after")
            if len(versions_after_terminal.json()) != version_count_before_terminal:
                raise RuntimeError("Story terminal contract failure created a Version")
            steps.append(SmokeStep("story_provider_unknown_recovery", "original Story key replayed once"))
            steps.append(SmokeStep("story_unverifiable_terminal", "terminal Story failure created no Version"))
        finally:
            bind = session_factory.kw.get("bind")
            if bind is not None:
                bind.dispose()

    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            note = session.get(InterviewNote, seed["note_id"])
            if note is None:
                raise RuntimeError("story smoke note disappeared")
            note.questions = "如何排查线上延迟并同步风险？"
            session.commit()
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()
    history = client.get(f"/api/interview-stories/{story_id}/versions/{pilot['version_id']}")
    _assert_status(history.status_code, 200, "story_changed_history")
    if not any(item.get("state") == "changed" for item in history.json().get("source_states", [])):
        raise RuntimeError("Story history did not derive changed source state")
    steps.append(SmokeStep("story_source_changed", "frozen Story history derived source changed"))


def _create_and_confirm_story_proposal(
    client: httpx.Client,
    *,
    endpoint: str,
    idempotency_key: str,
    confirmation_token: str,
    story: dict[str, Any],
    selections: list[dict[str, Any]],
    assertions: list[str],
    entry_context: dict[str, int] | None = None,
) -> dict[str, int]:
    payload: dict[str, Any] = {
        "target_story_id": story["id"],
        "expected_current_version_id": story["current_version_id"],
        "expected_story_revision": story["story_revision"],
        "selections": selections,
        "assertions": assertions,
        "idempotency_key": idempotency_key,
    }
    if entry_context is not None:
        payload["entry_context"] = entry_context
    created = client.post(endpoint, json=payload)
    if created.status_code not in {200, 201}:
        code = created.json().get("error_code", "unknown")
        raise RuntimeError(f"Story proposal did not become ready: {created.status_code}:{code}")
    body = created.json()
    if body.get("attempt_status") != "ready" or not isinstance(body.get("proposal"), dict):
        raise RuntimeError("Story proposal did not return a confirmable draft")
    attempt_id = int(body["id"])
    proposal_content = body["proposal"]["content"]
    editable_content = {
        "title": proposal_content["title"]["text"],
        "blocks": [
            {key: block[key] for key in ("kind", "text", "fact_mode")}
            for block in proposal_content["blocks"]
        ],
        "capability_labels": [item["text"] for item in proposal_content["capability_labels"]],
        "applicable_questions": [item["text"] for item in proposal_content["applicable_questions"]],
        "fact_gap_codes": proposal_content["fact_gap_codes"],
    }
    client_links = [
        {
            "target_kind": link["target_kind"],
            "target_id": link["target_id"],
            "source_kind": link["source_kind"],
            "source_id": link["source_stable_id"],
            "source_path": link["source_path"],
            "excerpt": link["excerpt"],
            "text_location": link["text_location"],
        }
        for link in body["proposal"]["evidence_links"]
    ]
    confirmed = client.post(
        f"/api/interview-story-proposals/{attempt_id}/confirm",
        json={
            "confirmation_token": confirmation_token,
            "content": editable_content,
            "evidence_links": client_links,
            "expected_current_version_id": story["current_version_id"],
            "expected_story_revision": story["story_revision"],
        },
    )
    if confirmed.status_code != 201:
        raise RuntimeError(
            "story_proposal_confirm returned "
            f"{confirmed.status_code}: {confirmed.text[:200]!r}"
        )
    replay = client.post(
        f"/api/interview-story-proposals/{attempt_id}/confirm",
        json={
            "confirmation_token": confirmation_token,
            "content": editable_content,
            "evidence_links": client_links,
            "expected_current_version_id": story["current_version_id"],
            "expected_story_revision": story["story_revision"],
        },
    )
    _assert_status(replay.status_code, 200, "story_proposal_confirm_replay")
    return {"attempt_id": attempt_id, "version_id": int(confirmed.json()["version_id"])}


def _cleanup_interview_story_smoke_records(data_dir: Path, seed: dict[str, int]) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            version_ids = select(InterviewStoryVersion.id)
            session.execute(delete(InterviewStoryVersionEvidenceLink).where(
                InterviewStoryVersionEvidenceLink.story_version_id.in_(version_ids)
            ))
            session.execute(delete(InterviewStoryUserAssertion).where(
                InterviewStoryUserAssertion.story_version_id.in_(version_ids)
            ))
            session.execute(delete(InterviewStoryProposalAttempt))
            session.execute(delete(InterviewStoryVersion))
            session.execute(delete(InterviewStory))
            session.execute(delete(MockInterviewTurn).where(MockInterviewTurn.attempt_id == seed["mock_attempt_id"]))
            session.execute(delete(MockInterviewAttempt).where(MockInterviewAttempt.id == seed["mock_attempt_id"]))
            session.execute(delete(InterviewNote).where(InterviewNote.id == seed["note_id"]))
            session.execute(delete(ApplicationEvent).where(ApplicationEvent.id == seed["event_id"]))
            session.execute(delete(Resume).where(Resume.id == seed["resume_id"]))
            session.execute(delete(Application).where(Application.id == seed["application_id"]))
            session.commit()
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _assert_interview_story_smoke_data_clean(data_dir: Path) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            for model in (
                InterviewStory,
                InterviewStoryVersion,
                InterviewStoryVersionEvidenceLink,
                InterviewStoryUserAssertion,
                InterviewStoryProposalAttempt,
            ):
                if int(session.scalar(select(func.count()).select_from(model)) or 0) != 0:
                    raise RuntimeError("isolated Story smoke cleanup left Story records")
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _chat_domain_counts(data_dir: Path) -> dict[str, int]:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            return {
                "conversations": int(session.scalar(select(func.count()).select_from(Conversation)) or 0),
                "messages": int(session.scalar(select(func.count()).select_from(ChatMessage)) or 0),
            }
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _mock_interview_attempt_ids(data_dir: Path, application_id: int, event_id: int) -> list[int]:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            return list(session.scalars(
                select(MockInterviewAttempt.id).where(
                    MockInterviewAttempt.application_id == application_id,
                    MockInterviewAttempt.event_id == event_id,
                )
            ).all())
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _mock_interview_attempt_state(
    data_dir: Path,
    application_id: int,
    event_id: int,
    resume_id: int,
    attempt_id: int,
) -> str:
    """Return a safe lifecycle state and reject orphaned or misbound rows."""
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            attempt = session.get(MockInterviewAttempt, attempt_id)
            if attempt is None:
                for model in (
                    MockInterviewTurn,
                    MockInterviewFeedbackProposal,
                    MockInterviewReviewDraft,
                ):
                    child_count = session.scalar(
                        select(func.count()).select_from(model).where(model.attempt_id == attempt_id)
                    )
                    if child_count:
                        raise RuntimeError(
                            f"mock interview attempt {attempt_id} left orphaned {model.__name__} rows"
                        )
                return "deleted"
            if (
                attempt.application_id != application_id
                or attempt.event_id != event_id
                or attempt.resume_id != resume_id
            ):
                raise RuntimeError("mock interview browser attempt changed its source context")
            if attempt.attempt_status not in {
                "generating_question",
                "awaiting_answer",
                "provider_unknown",
                "generating_feedback",
                "feedback_ready",
                "source_conflict",
                "contract_failed",
                "confirmed",
            }:
                raise RuntimeError("mock interview browser attempt has an unknown lifecycle state")
            return f"retained:{attempt.attempt_status}"
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _assert_mock_interview_attempt_restart_state(kind: str, category: str, state: str) -> None:
    """Reject a cleanup/retention result that contradicts the recovery contract.

    The expected retention comes from contracts/recovery-policy.v1.json.  The
    diagnostic category is significant because source/transcript conflicts may
    be observed while finalizing either a provider or contract failure.
    """
    if kind not in {"provider", "contract"}:
        raise RuntimeError("missing mock interview Attempt failure diagnostic")
    categories = {item.strip() for item in category.split(",") if item.strip()}
    if "source_conflict" in categories:
        error_code = "mock_interview_source_conflict"
        expected_state = "retained:source_conflict"
        label = "source-conflict"
    elif "transcript_conflict" in categories:
        error_code = "mock_interview_transcript_conflict"
        expected_state = None
        label = "transcript-conflict"
    elif kind == "provider":
        error_code = "mock_interview_provider_error"
        expected_state = "retained:provider_unknown"
        label = "provider-unknown"
    else:
        error_code = "mock_interview_unverifiable"
        expected_state = "retained:contract_failed"
        label = "retained-terminal"
    policy = get_recovery_policy(error_code)
    if policy is None:
        raise RuntimeError(f"recovery contract is missing {error_code}")
    if policy.attempt_retention.startswith("retained"):
        if not state.startswith("retained:") or (
            expected_state is not None and state != expected_state
        ):
            raise RuntimeError(
                f"A {label} Attempt did not match its contract-mandated retained state."
            )
        return
    if policy.attempt_retention == "absent":
        if state != "deleted":
            raise RuntimeError(f"An absent Attempt was unexpectedly retained as {state}.")
        return
    raise RuntimeError(
        f"recovery contract defines unsupported attempt retention {policy.attempt_retention!r} "
        f"for {error_code}"
    )


def _select_mock_interview_browser_success(
    history_items: list[dict[str, Any]], attempt_ids: list[int]
) -> dict[str, Any] | None:
    """Select a confirmed browser result by its Attempt ID, never by list order."""
    allowed_ids = {int(attempt_id) for attempt_id in attempt_ids}
    for item in history_items:
        if not isinstance(item, dict):
            continue
        raw_attempt_id = item.get("attempt_id")
        if raw_attempt_id is None:
            continue
        try:
            attempt_id = int(raw_attempt_id)
        except (TypeError, ValueError):
            continue
        draft = item.get("review_draft")
        turns = item.get("turns")
        if (
            attempt_id in allowed_ids
            and isinstance(turns, list)
            and len(turns) >= 2
            and item.get("proposal_status") == "normal"
            and isinstance(draft, dict)
            and draft.get("status") == "confirmed"
        ):
            return item
    return None


def _mock_interview_browser_failure_diagnostics(
    history_items: list[dict[str, Any]], attempt_ids: list[int]
) -> list[str]:
    """Return only stable Attempt/status diagnostics for failed browser attempts."""
    by_id = {
        int(item["attempt_id"]): item
        for item in history_items
        if isinstance(item, dict)
        and str(item.get("attempt_id", "")).isdigit()
    }
    diagnostics: list[str] = []
    for attempt_id in attempt_ids:
        item = by_id.get(int(attempt_id), {})
        status = item.get("proposal_status")
        if not isinstance(status, str) or not status:
            status = "unverifiable"
        diagnostics.append(f"attempt_{int(attempt_id)}:{status}")
    return diagnostics


def _assert_mock_interview_attempt_context(
    data_dir: Path,
    attempt_id: int,
    application_id: int,
    event_id: int,
    resume_id: int,
) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            attempt = session.get(MockInterviewAttempt, attempt_id)
            if attempt is None or (
                attempt.application_id != application_id
                or attempt.event_id != event_id
                or attempt.resume_id != resume_id
            ):
                raise RuntimeError("mock interview history changed its source context")
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _latest_mock_interview_failure_diagnostic(
    data_dir: Path, attempt_id: int, stage: str | None = None
) -> dict[str, str] | None:
    log_path = data_dir / "logs" / "offerpilot.log"
    if not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            log_row = json.loads(line)
        except json.JSONDecodeError:
            log_row = None
        message = log_row.get("message") if isinstance(log_row, dict) else line
        if not isinstance(message, str):
            continue
        for kind in ("provider", "contract"):
            marker = f"mock_interview_{kind}_failure "
            if marker not in message:
                continue
            try:
                payload = json.loads(message.split(marker, 1)[1])
            except (IndexError, json.JSONDecodeError):
                continue
            try:
                logged_attempt_id = int(payload.get("attempt_id"))
            except (TypeError, ValueError):
                continue
            if logged_attempt_id != attempt_id or (stage is not None and payload.get("stage") != stage):
                continue
            categories = payload.get("failure_categories")
            if isinstance(categories, list) and all(isinstance(item, str) for item in categories):
                category = ",".join(categories[:2])
            else:
                raw_category = payload.get("failure_category")
                category = raw_category if isinstance(raw_category, str) else ""
            return {"kind": kind, "category": category}
    return None


def _latest_mock_interview_failure_category(
    data_dir: Path, stage: str, attempt_id: int | None = None
) -> str:
    if attempt_id is None:
        return ""
    diagnostic = _latest_mock_interview_failure_diagnostic(data_dir, attempt_id, stage)
    return diagnostic["category"] if diagnostic is not None else ""


def _assert_mock_interview_failed_attempt_clean(
    data_dir: Path,
    application_id: int,
    event_id: int,
    resume_id: int,
    attempt_id: int,
    baseline: dict[str, Any],
) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            if session.get(MockInterviewAttempt, attempt_id) is not None:
                raise RuntimeError("mock interview failed attempt was not deleted")
            for model in (MockInterviewTurn, MockInterviewFeedbackProposal, MockInterviewReviewDraft):
                count = session.scalar(
                    select(func.count()).select_from(model).where(model.attempt_id == attempt_id)
                )
                if count:
                    raise RuntimeError(f"mock interview failed attempt left {model.__name__} rows")
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()
    _assert_real_ai_browser_no_cross_domain_writes(
        data_dir, application_id, baseline, [event_id], [resume_id]
    )


def _first_mock_interview_feedback_block(proposal: Any) -> dict[str, Any] | None:
    if not isinstance(proposal, dict):
        return None
    for field in ("strengths", "practice_points", "follow_up_questions", "next_practice_steps"):
        items = proposal.get(field)
        if isinstance(items, list):
            for item in items:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and isinstance(item.get("text"), str)
                    and isinstance(item.get("evidence_refs"), list)
                ):
                    return {
                        "id": item["id"],
                        "text": item["text"],
                        "evidence_refs": item["evidence_refs"],
                    }
    return None


def _validate_interview_review_smoke_evidence(
    proposal: Any,
    marker: str,
    expected_excerpts: dict[str, str] | None = None,
) -> bool:
    if not isinstance(proposal, dict):
        raise RuntimeError("interview review smoke proposal was not an object")
    has_verified_evidence = False
    for field in ("summary", "observations", "clarifications", "practice_focuses", "next_questions"):
        values = proposal.get(field)
        items = values if isinstance(values, list) else [values]
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("interview review smoke proposal item was not an object")
            refs = item.get("evidence_refs", [])
            if not isinstance(refs, list):
                raise RuntimeError("interview review smoke evidence refs were not an array")
            for ref in refs:
                if (
                    not isinstance(ref, dict)
                    or set(ref) != {"source", "path", "excerpt"}
                    or ref.get("source") != "interview_note"
                    or ref.get("path") not in {"/questions", "/self_reflection", "/difficulty_points", "/mood"}
                    or not isinstance(ref.get("excerpt"), str)
                    or not ref["excerpt"]
                ):
                    raise RuntimeError("interview review smoke returned an invalid evidence reference")
                if expected_excerpts is not None and ref["excerpt"] not in expected_excerpts.get(
                    str(ref["path"]), ""
                ):
                    raise RuntimeError("interview review smoke evidence excerpt did not match note")
                if marker in ref["excerpt"] or expected_excerpts is not None:
                    has_verified_evidence = True
    return has_verified_evidence


def _copy_real_ai_config(source_data_dir: Path, isolated_data_dir: Path) -> None:
    source_config = source_data_dir / "config.json"
    if source_config.is_file():
        shutil.copyfile(source_config, isolated_data_dir / "config.json")


def _cleanup_real_ai_smoke_records(
    data_dir: Path,
    application_id: int,
    resume_ids: list[int],
) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            session.execute(
                delete(MaterialRevisionProposal).where(
                    MaterialRevisionProposal.application_id == application_id
                )
            )
            note_ids = select(InterviewNote.id).where(InterviewNote.application_id == application_id)
            session.execute(
                delete(InterviewReviewProposal).where(InterviewReviewProposal.note_id.in_(note_ids))
            )
            session.execute(
                delete(InterviewPreparationProposal).where(
                    InterviewPreparationProposal.application_id == application_id
                )
            )
            captured_note_ids = list(
                session.scalars(
                    select(InterviewNote.id).where(InterviewNote.application_id == application_id)
                )
            )
            captured_source_ids = list(
                session.scalars(
                    select(KnowledgeCapturedSourceMetadata.source_id).where(
                        KnowledgeCapturedSourceMetadata.origin_note_id.in_(captured_note_ids)
                    )
                )
            )
            captured_version_ids = list(
                session.scalars(
                    select(KnowledgeNoteVersion.id).where(
                        KnowledgeNoteVersion.source_id.in_(captured_source_ids)
                    )
                )
            )
            captured_knowledge_note_ids = list(
                session.scalars(
                    select(KnowledgeNoteVersion.note_id).where(
                        KnowledgeNoteVersion.id.in_(captured_version_ids)
                    )
                )
            )
            session.execute(delete(KnowledgeNoteEvidence).where(KnowledgeNoteEvidence.note_version_id.in_(captured_version_ids)))
            session.execute(delete(KnowledgeNoteVersion).where(KnowledgeNoteVersion.id.in_(captured_version_ids)))
            session.execute(delete(KnowledgeNote).where(KnowledgeNote.id.in_(captured_knowledge_note_ids)))
            session.execute(delete(KnowledgeEvidence).where(KnowledgeEvidence.source_id.in_(captured_source_ids)))
            session.execute(delete(KnowledgeExtractionSnapshot).where(KnowledgeExtractionSnapshot.source_id.in_(captured_source_ids)))
            session.execute(delete(KnowledgeCapturedSourceMetadata).where(KnowledgeCapturedSourceMetadata.source_id.in_(captured_source_ids)))
            session.execute(delete(KnowledgeSource).where(KnowledgeSource.id.in_(captured_source_ids)))
            session.execute(delete(InterviewKnowledgeCaptureAttempt).where(InterviewKnowledgeCaptureAttempt.note_id.in_(captured_note_ids)))
            session.execute(delete(InterviewNote).where(InterviewNote.application_id == application_id))
            attempt_ids = list(
                session.scalars(
                    select(MockInterviewAttempt.id).where(
                        MockInterviewAttempt.application_id == application_id
                    )
                )
            )
            proposal_ids = list(
                session.scalars(
                    select(MockInterviewFeedbackProposal.id).where(
                        MockInterviewFeedbackProposal.attempt_id.in_(attempt_ids)
                    )
                )
            )
            if proposal_ids:
                session.execute(
                    delete(MockInterviewReviewDraft).where(
                        MockInterviewReviewDraft.proposal_id.in_(proposal_ids)
                    )
                )
            if attempt_ids:
                session.execute(
                    delete(MockInterviewFeedbackProposal).where(
                        MockInterviewFeedbackProposal.id.in_(proposal_ids)
                    )
                )
                session.execute(
                    delete(MockInterviewTurn).where(MockInterviewTurn.attempt_id.in_(attempt_ids))
                )
                session.execute(
                    delete(MockInterviewAttempt).where(MockInterviewAttempt.id.in_(attempt_ids))
                )
            session.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id == application_id))
            session.execute(delete(Question).where(Question.application_id == application_id))
            session.execute(
                delete(ApplicationMaterialKit).where(
                    ApplicationMaterialKit.application_id == application_id
                )
            )
            v2_stages = list(
                session.scalars(
                    select(OpportunityFitReviewStage).where(
                        OpportunityFitReviewStage.application_id == application_id
                    )
                )
            )
            while v2_stages:
                parent_stage_ids = {
                    stage.parent_triage_stage_id
                    for stage in v2_stages
                    if stage.parent_triage_stage_id is not None
                }
                leaf_stages = [stage for stage in v2_stages if stage.id not in parent_stage_ids]
                if not leaf_stages:
                    raise RuntimeError("cannot order deletion of Opportunity Fit v2 stages")
                for stage in leaf_stages:
                    session.delete(stage)
                session.flush()
                leaf_ids = {stage.id for stage in leaf_stages}
                v2_stages = [stage for stage in v2_stages if stage.id not in leaf_ids]
            session.execute(
                delete(OpportunityFitReviewSession).where(
                    OpportunityFitReviewSession.application_id == application_id
                )
            )
            session.execute(
                delete(OpportunityFitReview).where(
                    OpportunityFitReview.application_id == application_id
                )
            )
            session.execute(
                delete(ApplicationEvidenceBundle).where(
                    ApplicationEvidenceBundle.application_id == application_id
                )
            )
            session.execute(delete(Application).where(Application.id == application_id))
            if resume_ids:
                session.execute(delete(Resume).where(Resume.id.in_(resume_ids)))
            session.commit()
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _cleanup_real_ai_browser_records(
    data_dir: Path,
    application_id: int,
    resume_ids: list[int],
) -> None:
    """Remove only the synthetic records created by the isolated browser harness."""
    _cleanup_real_ai_smoke_records(data_dir, application_id, resume_ids)


def _capture_real_ai_browser_domain_baseline(
    data_dir: Path,
    application_id: int,
    event_ids: list[int] | None = None,
    resume_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Capture scoped data that the interview-preparation browser flow must not mutate."""
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise RuntimeError(f"browser smoke application {application_id} is missing")
            selected_event_ids = sorted(event_ids or [])
            selected_resume_ids = sorted(resume_ids or [])
            event_snapshots = {
                str(event_id): _real_ai_browser_event_snapshot(session.get(ApplicationEvent, event_id))
                for event_id in selected_event_ids
            }
            resume_snapshots = {
                str(resume_id): _real_ai_browser_resume_snapshot(session.get(Resume, resume_id))
                for resume_id in selected_resume_ids
            }
            return {
                "application_snapshot_hash": sha256_text(
                    canonical_json(_real_ai_browser_application_snapshot(application))
                ),
                "application_count": session.scalar(select(func.count()).select_from(Application)),
                "material_kit_count": session.scalar(
                    select(func.count())
                    .select_from(ApplicationMaterialKit)
                    .where(ApplicationMaterialKit.application_id == application_id)
                ),
                "material_proposal_count": session.scalar(
                    select(func.count())
                    .select_from(MaterialRevisionProposal)
                    .where(MaterialRevisionProposal.application_id == application_id)
                ),
                "opportunity_fit_count": session.scalar(
                    select(func.count())
                    .select_from(OpportunityFitReview)
                    .where(OpportunityFitReview.application_id == application_id)
                ),
                "opportunity_fit_session_count": session.scalar(
                    select(func.count())
                    .select_from(OpportunityFitReviewSession)
                    .where(OpportunityFitReviewSession.application_id == application_id)
                ),
                "opportunity_fit_stage_count": session.scalar(
                    select(func.count())
                    .select_from(OpportunityFitReviewStage)
                    .where(OpportunityFitReviewStage.application_id == application_id)
                ),
                "question_count": session.scalar(
                    select(func.count()).select_from(Question).where(Question.application_id == application_id)
                ),
                "reminder_count": session.scalar(select(func.count()).select_from(Wakeup)),
                "knowledge_note_count": session.scalar(select(func.count()).select_from(KnowledgeNote)),
                "knowledge_source_count": session.scalar(select(func.count()).select_from(KnowledgeSource)),
                "knowledge_evidence_count": session.scalar(select(func.count()).select_from(KnowledgeEvidence)),
                "knowledge_capture_attempt_count": session.scalar(
                    select(func.count()).select_from(InterviewKnowledgeCaptureAttempt)
                ),
                "interview_event_count": session.scalar(
                    select(func.count())
                    .select_from(ApplicationEvent)
                    .where(ApplicationEvent.application_id == application_id)
                ),
                "event_snapshot_hash": sha256_text(canonical_json(event_snapshots)),
                "resume_count": session.scalar(
                    select(func.count()).select_from(Resume).where(Resume.deleted_at.is_(None))
                ),
                "resume_snapshot_hash": sha256_text(canonical_json(resume_snapshots)),
                # Memory has no persistent model/table in the current product schema.
                "memory_count": 0,
            }
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()


def _assert_real_ai_browser_no_cross_domain_writes(
    data_dir: Path,
    application_id: int,
    baseline: dict[str, Any],
    event_ids: list[int] | None = None,
    resume_ids: list[int] | None = None,
) -> None:
    current = _capture_real_ai_browser_domain_baseline(data_dir, application_id, event_ids, resume_ids)
    differences = {
        key: {"before": baseline.get(key), "after": value}
        for key, value in current.items()
        if baseline.get(key) != value
    }
    if differences:
        raise RuntimeError(
            "real-ai browser flow created cross-domain writes: "
            + json.dumps(differences, ensure_ascii=False, sort_keys=True)
        )


def _real_ai_browser_event_snapshot(event: ApplicationEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "id": event.id,
        "application_id": event.application_id,
        "event_type": event.event_type,
        "subtype": event.subtype,
        "tags": event.tags,
        "round": event.round,
        "scheduled_at": event.scheduled_at.isoformat() if event.scheduled_at else None,
        "duration_minutes": event.duration_minutes,
        "location": event.location,
        "notes": event.notes,
        "remind_at": event.remind_at.isoformat() if event.remind_at else None,
        "status": event.status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _real_ai_browser_application_snapshot(application: Application) -> dict[str, Any]:
    return {
        "id": application.id,
        "company_name": application.company_name,
        "position_name": application.position_name,
        "job_url": application.job_url,
        "status": application.status,
        "source": application.source,
        "notes": application.notes,
        "applied_at": application.applied_at.isoformat() if application.applied_at else None,
        "first_pending_at": application.first_pending_at.isoformat()
        if application.first_pending_at
        else None,
        "first_applied_at": application.first_applied_at.isoformat()
        if application.first_applied_at
        else None,
        "first_written_test_at": application.first_written_test_at.isoformat()
        if application.first_written_test_at
        else None,
        "first_interview_at": application.first_interview_at.isoformat()
        if application.first_interview_at
        else None,
        "first_offer_at": application.first_offer_at.isoformat() if application.first_offer_at else None,
        "closed_reason": application.closed_reason,
        "closed_at": application.closed_at.isoformat() if application.closed_at else None,
        "deleted_at": application.deleted_at.isoformat() if application.deleted_at else None,
        "created_at": application.created_at.isoformat() if application.created_at else None,
        "updated_at": application.updated_at.isoformat() if application.updated_at else None,
    }


def _real_ai_browser_resume_snapshot(resume: Resume | None) -> dict[str, Any] | None:
    if resume is None:
        return None
    return {
        "id": resume.id,
        "name": resume.name,
        "file_path": resume.file_path,
        "title": resume.title,
        "parsed_data": resume.parsed_data,
        "parse_status": resume.parse_status,
        "parent_resume_id": resume.parent_resume_id,
        "source": resume.source,
        "source_file_path": resume.source_file_path,
        "content_json": resume.content_json,
        "is_master": resume.is_master,
        "deleted_at": resume.deleted_at.isoformat() if resume.deleted_at else None,
        "created_at": resume.created_at.isoformat() if resume.created_at else None,
    }


def _assert_real_ai_smoke_data_clean(data_dir: Path) -> None:
    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            active_resume_count = session.scalar(
                select(func.count()).select_from(Resume).where(Resume.deleted_at.is_(None))
            )
            active_master_count = session.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.deleted_at.is_(None))
                .where(Resume.is_master.is_(True))
            )
            material_kit_count = session.scalar(select(func.count()).select_from(ApplicationMaterialKit))
            proposal_count = session.scalar(select(func.count()).select_from(MaterialRevisionProposal))
            interview_note_count = session.scalar(select(func.count()).select_from(InterviewNote))
            interview_event_count = session.scalar(
                select(func.count()).select_from(ApplicationEvent).where(ApplicationEvent.event_type == "interview")
            )
            interview_proposal_count = session.scalar(select(func.count()).select_from(InterviewReviewProposal))
            interview_preparation_proposal_count = session.scalar(
                select(func.count()).select_from(InterviewPreparationProposal)
            )
            question_count = session.scalar(select(func.count()).select_from(Question))
            mock_attempt_count = session.scalar(select(func.count()).select_from(MockInterviewAttempt))
            mock_turn_count = session.scalar(select(func.count()).select_from(MockInterviewTurn))
            mock_proposal_count = session.scalar(
                select(func.count()).select_from(MockInterviewFeedbackProposal)
            )
            mock_draft_count = session.scalar(select(func.count()).select_from(MockInterviewReviewDraft))
            reminder_count = session.scalar(select(func.count()).select_from(Wakeup))
            opportunity_fit_review_count = session.scalar(
                select(func.count()).select_from(OpportunityFitReview)
            )
            opportunity_fit_session_count = session.scalar(
                select(func.count()).select_from(OpportunityFitReviewSession)
            )
            opportunity_fit_stage_count = session.scalar(
                select(func.count()).select_from(OpportunityFitReviewStage)
            )
            application_count = session.scalar(select(func.count()).select_from(Application))
            evidence_bundle_count = session.scalar(
                select(func.count()).select_from(ApplicationEvidenceBundle)
            )
            captured_knowledge_count = session.scalar(
                select(func.count()).select_from(KnowledgeNote).where(
                    KnowledgeNote.origin_kind == "confirmed_interview_capture"
                )
            )
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()

    if active_resume_count != 0:
        raise RuntimeError("real-ai smoke left active resumes")
    if active_master_count != 0:
        raise RuntimeError("real-ai smoke left active master resumes")
    if material_kit_count != 0:
        raise RuntimeError("real-ai smoke left material kits")
    if proposal_count != 0:
        raise RuntimeError("real-ai smoke left material proposals")
    if interview_note_count != 0:
        raise RuntimeError("real-ai smoke left interview notes")
    if interview_event_count != 0:
        raise RuntimeError("real-ai smoke left interview events")
    if interview_proposal_count != 0:
        raise RuntimeError("real-ai smoke left interview review proposals")
    if interview_preparation_proposal_count != 0:
        raise RuntimeError("real-ai smoke left interview preparation proposals")
    if question_count != 0:
        raise RuntimeError("real-ai smoke left questions")
    if mock_attempt_count != 0 or mock_turn_count != 0 or mock_proposal_count != 0 or mock_draft_count != 0:
        raise RuntimeError("real-ai smoke left mock interview records")
    if reminder_count != 0:
        raise RuntimeError("real-ai smoke left reminders")
    if opportunity_fit_review_count != 0:
        raise RuntimeError("real-ai smoke left opportunity fit reviews")
    if opportunity_fit_session_count != 0:
        raise RuntimeError("real-ai smoke left opportunity fit v2 sessions")
    if opportunity_fit_stage_count != 0:
        raise RuntimeError("real-ai smoke left opportunity fit v2 stages")
    if application_count != 0:
        raise RuntimeError("real-ai smoke left applications")
    if evidence_bundle_count != 0:
        raise RuntimeError("real-ai smoke left evidence bundles")
    if captured_knowledge_count != 0:
        raise RuntimeError("real-ai smoke left confirmed interview knowledge")


def _material_proposal_smoke_failure(
    body: object,
    *,
    failure_category: str,
    message: str,
) -> _MaterialProposalSmokeContractError:
    diagnostic = {
        "kind": "material_proposal_response",
        "failure_category": failure_category,
        "failure_categories": [failure_category],
        "structure_summaries": [_material_response_structure_summary(body)],
        "evidence_counts": _material_response_evidence_counts(body),
        "repair_attempted": False,
        "retry_count": 0,
    }
    _persist_material_proposal_smoke_diagnostic(diagnostic)
    return _MaterialProposalSmokeContractError(message, diagnostic)


def _material_response_structure_summary(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"payload_type": _material_response_value_type(body), "top_level_keys": [], "fields": {}}
    fields: dict[str, Any] = {}
    for key in sorted(item for item in body if isinstance(item, str))[:32]:
        value = body[key]
        shape: dict[str, Any] = {"type": _material_response_value_type(value)}
        if isinstance(value, list):
            shape["length"] = len(value)
            shape["item_types"] = [_material_response_value_type(item) for item in value[:8]]
            shape["item_key_sets"] = [
                sorted(item_key for item_key in item if isinstance(item_key, str))[:32]
                if isinstance(item, dict)
                else None
                for item in value[:8]
            ]
        elif isinstance(value, dict):
            shape["keys"] = sorted(item for item in value if isinstance(item, str))[:32]
        fields[key] = shape
    return {
        "payload_type": "object",
        "top_level_keys": sorted(item for item in body if isinstance(item, str))[:32],
        "fields": fields,
    }


def _material_response_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unsupported"


def _material_response_evidence_counts(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    changes = body.get("changes")
    change_count = len(changes) if isinstance(changes, list) else 0
    evidence_ref_count = 0
    changes_with_refs = 0
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            refs = change.get("evidence_refs")
            if isinstance(refs, list):
                evidence_ref_count += len(refs)
                if refs:
                    changes_with_refs += 1
    source = body.get("source")
    assertions = source.get("user_assertions") if isinstance(source, dict) else None
    return {
        "proposal": {
            "changes": change_count,
            "changes_with_evidence_refs": changes_with_refs,
            "evidence_refs": evidence_ref_count,
        },
        "available": {
            "user_assertions": len(assertions) if isinstance(assertions, list) else 0,
            "evidence_bundle_present": bool(
                isinstance(source, dict) and source.get("latest_evidence_bundle") is not None
            ),
        },
    }


def _persist_material_proposal_smoke_diagnostic(diagnostic: dict[str, Any]) -> None:
    report_dir = _full_verify_report_dir()
    if report_dir is None:
        return
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "material-proposal-diagnostic.json").write_text(
            json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except Exception:
        return


def _validate_material_proposal_smoke_response(body: object) -> None:
    if not isinstance(body, dict):
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_root_shape",
            message="material proposal response was not an object",
        )
    expected_root = {
        "id",
        "application_id",
        "material_kit_id",
        "jd_version_id",
        "source_resume_id",
        "status",
        "summary",
        "proposal_sha256",
        "result_resume_id",
        "created_at",
        "changes",
        "source",
        "accepted_change_ids",
        "accepted_at",
        "rejected_at",
    }
    if set(body) != expected_root:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_root_shape",
            message="material proposal response leaked frozen source data",
        )
    changes = body.get("changes")
    if not isinstance(changes, list):
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_change_shape",
            message="material proposal response did not contain changes",
        )
    for change in changes:
        if not isinstance(change, dict) or set(change) != {
            "id",
            "path",
            "before",
            "after",
            "rationale",
            "evidence_refs",
        }:
            raise _material_proposal_smoke_failure(
                body,
                failure_category="response_change_shape",
                message="material proposal response leaked frozen source data",
            )
        refs = change.get("evidence_refs")
        if not isinstance(refs, list):
            raise _material_proposal_smoke_failure(
                body,
                failure_category="response_evidence_reference_shape",
                message="material proposal response leaked frozen source data",
            )
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"source", "path", "excerpt"}:
                raise _material_proposal_smoke_failure(
                    body,
                    failure_category="response_evidence_reference_shape",
                    message="material proposal response leaked frozen source data",
                )

    source = body.get("source")
    if not isinstance(source, dict) or set(source) != {
        "application",
        "material_kit",
        "resume",
        "latest_evidence_bundle",
        "user_assertions",
    }:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source data",
        )
    if not isinstance(source.get("application"), dict) or set(source["application"]) != {
        "id",
        "company_name",
        "position_name",
    }:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source data",
        )
    if not isinstance(source.get("material_kit"), dict) or set(source["material_kit"]) != {
        "id",
        "jd_version_id",
        "jd_excerpt",
    }:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source data",
        )
    if type(source["material_kit"].get("jd_version_id")) is not int or source["material_kit"]["jd_version_id"] <= 0:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_validation",
            message="material proposal response had invalid JD version",
        )
    if not isinstance(source.get("resume"), dict) or set(source["resume"]) != {"id", "title"}:
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source data",
        )
    bundle = source.get("latest_evidence_bundle")
    if bundle is not None and (not isinstance(bundle, dict) or set(bundle) != {"id", "bundle_sha256"}):
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source source data",
        )
    assertions = source.get("user_assertions")
    if not isinstance(assertions, list) or any(
        not isinstance(item, dict) or set(item) != {"id", "text"} for item in assertions
    ):
        raise _material_proposal_smoke_failure(
            body,
            failure_category="response_source_shape",
            message="material proposal response leaked frozen source data",
        )


def _run_unconfigured_chat_smoke(static_dir: Path | None, steps: list[SmokeStep]) -> None:
    with tempfile.TemporaryDirectory(prefix="offerpilot-smoke-unconfigured-", ignore_cleanup_errors=True) as temp_dir:
        app = create_app(data_dir=Path(temp_dir), static_dir=static_dir)
        with _running_server(app) as base_url:
            with httpx.Client(base_url=base_url, timeout=30.0) as client:
                response = client.post("/api/chat", json={"message": "hello", "conversation_id": 0})
                _assert_status(response.status_code, 503, "http_unconfigured_chat")
                if "AI is not configured" not in response.json().get("error", ""):
                    raise RuntimeError("unconfigured chat did not return a clear AI setup error")
                steps.append(SmokeStep("http_unconfigured_chat", "POST /api/chat returned setup guidance without API key"))
        del app
        gc.collect()


def _run_resume_http_smoke(client: httpx.Client, steps: list[SmokeStep]) -> None:
    empty_content = {
        "career_intent": {"target_roles": [], "target_locations": []},
        "contact": {},
        "education": [],
        "experience": [],
        "projects": [],
        "skills": [],
        "raw_text": "",
    }
    created = client.post(
        "/api/resumes",
        json={"title": "HTTP Smoke Resume Draft", "source": "dialog", "content_json": empty_content},
    )
    _assert_status(created.status_code, 201, "http_resume_crud")
    resume_id = int(created.json()["id"])
    try:
        updated = client.patch(f"/api/resumes/{resume_id}", json={"title": "HTTP Smoke Resume Draft Updated"})
        _assert_status(updated.status_code, 200, "http_resume_crud")
        fetched = client.get(f"/api/resumes/{resume_id}")
        _assert_status(fetched.status_code, 200, "http_resume_crud")
        if fetched.json()["title"] != "HTTP Smoke Resume Draft Updated":
            raise RuntimeError("resume update was not reflected by get endpoint")
        listed = client.get("/api/resumes")
        _assert_status(listed.status_code, 200, "http_resume_crud")
        if not any(item.get("id") == resume_id for item in listed.json()):
            raise RuntimeError("created resume was not returned by list endpoint")
    finally:
        deleted = client.delete(f"/api/resumes/{resume_id}")
        _assert_status(deleted.status_code, 200, "http_resume_crud")
    steps.append(SmokeStep("http_resume_crud", "resume create, update, read, list, and delete endpoints worked"))


def _run_application_event_http_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
) -> None:
    created = client.post(
        "/api/application-events",
        json={
            "application_id": application_id,
            "event_type": "interview",
            "scheduled_at": "2026-07-10T10:00:00Z",
            "duration_minutes": 30,
            "notes": "http smoke",
        },
    )
    _assert_status(created.status_code, 201, "http_application_event_crud")
    event_id = int(created.json()["id"])
    try:
        updated = client.put(
            f"/api/application-events/{event_id}",
            json={
                "application_id": application_id,
                "event_type": "interview",
                "scheduled_at": "2026-07-11T10:00:00Z",
                "duration_minutes": 45,
                "notes": "http smoke updated",
            },
        )
        _assert_status(updated.status_code, 200, "http_application_event_crud")
        fetched = client.get(f"/api/application-events/{event_id}")
        _assert_status(fetched.status_code, 200, "http_application_event_crud")
        if fetched.json()["duration_minutes"] != 45:
            raise RuntimeError("application event update was not reflected by get endpoint")
        listed = client.get("/api/application-events", params={"application_id": application_id})
        _assert_status(listed.status_code, 200, "http_application_event_crud")
        if not any(item.get("id") == event_id for item in listed.json()):
            raise RuntimeError("created application event was not returned by list endpoint")
    finally:
        deleted = client.delete(f"/api/application-events/{event_id}")
        _assert_status(deleted.status_code, 200, "http_application_event_crud")
    steps.append(
        SmokeStep(
            "http_application_event_crud",
            "application event create, update, read, list, and delete endpoints worked",
        )
    )


def _run_local_proposal_terminal_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    data_dir: Path,
    resume_ids: list[int],
) -> None:
    """Exercise the five proposal terminal contracts without an external provider.

    The local profile intentionally uses a chat model that cannot produce proposal
    JSON.  This keeps the smoke deterministic while proving that each flow has its
    own safe contract-failure behavior and that knowledge capture can fall back to
    a direct, user-confirmed preview.
    """
    resume_content = {
        "raw_text": "Built local smoke services.",
        "skills": ["Python"],
        "experience": [],
        "projects": [],
        "career_intent": {"target_roles": []},
    }
    resume_response = client.post(
        "/api/resumes",
        json={
            "title": "Local proposal terminal smoke resume",
            "text": resume_content["raw_text"],
            "content_json": resume_content,
        },
    )
    _assert_status(resume_response.status_code, 201, "http_proposal_terminal_resume")
    resume_id = int(resume_response.json()["id"])
    resume_ids.append(resume_id)
    jd_version_id = _save_application_jd_version(
        client,
        application_id,
        "Build reliable local smoke services.",
        "smoke-terminal-jd-0001",
    )

    session_factory = session_factory_for_data_dir(data_dir)
    try:
        with session_factory() as session:
            session.add(
                ApplicationMaterialKit(
                    application_id=application_id,
                    resume_id=resume_id,
                    jd_snapshot="Build reliable local smoke services.",
                    jd_version_id=jd_version_id,
                    content_json=canonical_json({"summary": "local smoke kit"}),
                )
            )
            session.commit()
    finally:
        bind = session_factory.kw.get("bind")
        if bind is not None:
            bind.dispose()

    material_response = client.post(
        f"/api/applications/{application_id}/material-revision-proposals",
        json={"instructions": "Highlight reliability.", "user_assertions": []},
    )
    _assert_status(material_response.status_code, 502, "http_material_contract_failure")
    if material_response.json().get("error_code") != "material_proposal_unverifiable":
        raise RuntimeError("material contract failure used the wrong error code")
    if client.get(f"/api/applications/{application_id}/material-revision-proposals").json():
        raise RuntimeError("material contract failure wrote a proposal")

    opportunity_response = client.post(
        f"/api/applications/{application_id}/opportunity-fit-reviews",
        json={
            "schema_version": 2,
            "resume_id": resume_id,
            "jd_version_id": jd_version_id,
            "jd_source_label": "local smoke",
            "candidate_assertions": [],
            "idempotency_key": "f3a1bd2e-6f1f-4dd4-bc5f-1a0c9be4f001",
        },
    )
    _assert_status(opportunity_response.status_code, 502, "http_opportunity_contract_failure")
    if opportunity_response.json().get("error_code") != "opportunity_fit_unverifiable":
        raise RuntimeError("opportunity contract failure used the wrong error code")
    if client.get(f"/api/applications/{application_id}/opportunity-fit-reviews").json():
        raise RuntimeError("opportunity contract failure wrote a review")

    event_response = client.post(
        "/api/application-events",
        json={
            "application_id": application_id,
            "event_type": "interview",
            "subtype": "terminal-matrix",
            "scheduled_at": "2026-07-24T10:00:00Z",
            "duration_minutes": 30,
        },
    )
    _assert_status(event_response.status_code, 201, "http_proposal_terminal_event")
    event_id = int(event_response.json()["id"])
    note_response = client.post(
        f"/api/applications/{application_id}/notes",
        json={
            "company": "Local smoke",
            "position": "Verification Engineer",
            "round": "technical",
            "date": "2026-07-24",
            "questions": "I explained the rollback plan.",
            "self_reflection": "I should state the tradeoff earlier.",
            "difficulty_points": "Structuring the answer was difficult.",
            "mood": "focused",
            "application_event_id": event_id,
        },
    )
    _assert_status(note_response.status_code, 201, "http_proposal_terminal_note")
    note_id = int(note_response.json()["id"])

    review_response = client.post(
        f"/api/notes/{note_id}/interview-review-proposals",
        json={"idempotency_key": "local-proposal-matrix-review-01"},
    )
    _assert_status(review_response.status_code, 201, "http_interview_review_safe_empty")
    review_body = review_response.json()
    if review_body.get("proposal", {}).get("observations") != []:
        raise RuntimeError("interview review contract failure was not a safe empty result")
    review_replay = client.post(
        f"/api/notes/{note_id}/interview-review-proposals",
        json={"idempotency_key": "local-proposal-matrix-review-01"},
    )
    _assert_status(review_replay.status_code, 200, "http_interview_review_safe_empty_replay")

    selected_fragment = {
        "fragment_id": "local-question",
        "path": "/questions",
        "start": 0,
        "end": len("I explained the rollback plan."),
        "text": "I explained the rollback plan.",
    }
    no_evidence = client.post(
        f"/api/notes/{note_id}/knowledge-capture/preview",
        json={"attempt_key": "local-proposal-matrix-empty", "mode": "ai", "selected_fragments": []},
    )
    _assert_status(no_evidence.status_code, 422, "http_knowledge_no_evidence")
    knowledge_response = client.post(
        f"/api/notes/{note_id}/knowledge-capture/preview",
        json={
            "attempt_key": "local-proposal-matrix-knowledge-01",
            "mode": "ai",
            "selected_fragments": [selected_fragment],
        },
    )
    _assert_status(knowledge_response.status_code, 200, "http_knowledge_safe_empty")
    if knowledge_response.json().get("preview_status") != "safe_empty":
        raise RuntimeError("knowledge contract failure was not a safe empty preview")
    direct_response = client.post(
        f"/api/notes/{note_id}/knowledge-capture/preview",
        json={
            "attempt_key": "local-proposal-matrix-knowledge-01",
            "mode": "direct",
            "selected_fragments": [selected_fragment],
        },
    )
    _assert_status(direct_response.status_code, 200, "http_knowledge_direct_fallback")
    if direct_response.json().get("preview_status") != "direct_ready":
        raise RuntimeError("knowledge direct fallback was not available")

    preparation_response = client.post(
        f"/api/applications/{application_id}/interview-preparation-proposals",
        json={
            "event_id": event_id,
            "resume_id": resume_id,
            "jd_version_id": jd_version_id,
            "knowledge_selections": [],
            "user_assertions": [],
            "idempotency_key": "local-proposal-matrix-prep-01",
        },
    )
    _assert_status(preparation_response.status_code, 201, "http_interview_preparation_safe_empty")
    if preparation_response.json().get("proposal_status") != "safe_empty":
        raise RuntimeError("interview preparation contract failure was not a safe empty result")
    steps.append(
        SmokeStep(
            "http_proposal_terminal_matrix",
            "local profile covered contract failures, safe-empty results, no-evidence rejection, replay, and direct knowledge fallback",
        )
    )


def _run_chat_card_regression_smoke(
    client: Any,
    steps: list[SmokeStep],
    application_id: int,
    *,
    step_prefix: str = "",
) -> None:
    create_application_step = f"{step_prefix}chat_create_application_card"
    create_application_pending = client.post(
        "/api/chat",
        json={"message": "create application card regression", "conversation_id": 0},
    )
    _assert_status(create_application_pending.status_code, 200, create_application_step)
    _assert_create_application_card(create_application_pending.json())
    _reject_pending_chat_action(client, create_application_pending.json(), create_application_step)
    steps.append(SmokeStep(create_application_step, "create application confirmation card kept key fields"))

    create_event_step = f"{step_prefix}chat_create_event_card"
    create_event_pending = client.post(
        "/api/chat",
        json={"message": "create event card regression", "conversation_id": 0},
    )
    _assert_status(create_event_pending.status_code, 200, create_event_step)
    _assert_create_event_card(create_event_pending.json(), application_id)
    _reject_pending_chat_action(client, create_event_pending.json(), create_event_step)
    steps.append(SmokeStep(create_event_step, "create event confirmation card kept schedule details"))


def _run_deterministic_chat_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
) -> None:
    pending = client.post(
        "/api/chat",
        json={"message": "move this application to offer", "conversation_id": 0},
    )
    _assert_status(pending.status_code, 200, "http_chat_pending")
    pending_body = pending.json()
    if pending_body.get("type") != "confirmation_required":
        raise RuntimeError("http chat did not request confirmation")
    before_confirm = client.get(f"/api/applications/{application_id}").json()
    if before_confirm["status"] != "applied":
        raise RuntimeError("http write tool mutated before confirmation")
    steps.append(SmokeStep("http_chat_pending", "POST /api/chat paused write action for confirmation"))

    confirmed = client.post(
        "/api/chat/confirm",
        json={
            "conversation_id": pending_body["conversation_id"],
            "approved": True,
            "confirmation_token": pending_body["pending_action"]["confirmation_token"],
        },
    )
    _assert_status(confirmed.status_code, 200, "http_confirm_action")
    after_confirm = client.get(f"/api/applications/{application_id}").json()
    if after_confirm["status"] != "offer":
        raise RuntimeError("http confirmed write did not update application")
    steps.append(SmokeStep("http_confirm_action", "POST /api/chat/confirm updated application"))

    conversations = client.get("/api/chat/conversations")
    _assert_status(conversations.status_code, 200, "http_pending_cleared")
    if conversations.json()[0]["pending_action"] is not None:
        raise RuntimeError("http pending action was not cleared")
    steps.append(SmokeStep("http_pending_cleared", "pending action cleared after confirmation"))


def _run_application_outcome_http_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    application_id: int,
    smoke_resume_ids: list[int],
) -> None:
    resume = client.post(
        "/api/resumes",
        json={
            "title": "筱哲 · Outcome Smoke",
            "source": "dialog",
            "content_json": {"raw_text": "负责企业级 AI 应用交付与知识库质量建设。"},
        },
    )
    _assert_status(resume.status_code, 201, "http_application_outcome_resume")
    resume_id = int(resume.json()["id"])
    smoke_resume_ids.append(resume_id)
    jd_version_id = _save_application_jd_version(
        client,
        application_id,
        "负责企业级 AI 应用研发、系统设计与知识库质量建设。",
        "outcome-smoke-jd-0001",
    )
    snapshot_payload = {
        "resume_id": resume_id,
        "jd_version_id": jd_version_id,
        "material_kit_id": None,
        "submitted_at": "2026-08-12T09:30:00+08:00",
        "note": "官网投递",
        "idempotency_key": "outcome-smoke-snapshot-0001",
    }
    snapshot_url = f"/api/applications/{application_id}/submission-snapshots"
    created = client.post(snapshot_url, json=snapshot_payload)
    replay = client.post(snapshot_url, json=snapshot_payload)
    _assert_status(created.status_code, 201, "http_application_outcome_snapshot")
    _assert_status(replay.status_code, 200, "http_application_outcome_snapshot_replay")
    if replay.json().get("id") != created.json().get("id"):
        raise RuntimeError("application outcome snapshot replay created a duplicate")

    pending = client.post(
        "/api/chat",
        json={
            "message": "确认记录本次面试结果",
            "conversation_id": 0,
            "context_type": "application",
            "context_ref": str(application_id),
            "pilot_action": {
                "type": "application_outcome_record",
                "snapshotId": int(created.json()["id"]),
                "eventId": None,
                "stage": "interview",
                "result": "advanced",
                "feedbackText": "项目讲解清楚，系统设计还可深入。",
                "reflectionText": "容量估算缺少量级依据。",
                "nextActionText": "完成一轮容量估算专项练习。",
                "feedbackTags": ["communication", "system_design"],
                "occurredAt": "2026-08-12T15:00:00+08:00",
            },
        },
    )
    _assert_status(pending.status_code, 200, "http_application_outcome_pilot_pending")
    pending_body = pending.json()
    if pending_body.get("type") != "confirmation_required":
        raise RuntimeError("application outcome Pilot action skipped confirmation")
    if pending_body.get("pending_action", {}).get("tool_name") != "record_application_outcome":
        raise RuntimeError("application outcome Pilot action returned the wrong tool")
    confirmed = client.post(
        "/api/chat/confirm",
        json={
            "conversation_id": pending_body["conversation_id"],
            "approved": True,
            "confirmation_token": pending_body["pending_action"]["confirmation_token"],
        },
    )
    _assert_status(confirmed.status_code, 200, "http_application_outcome_pilot_confirm")
    summary = client.get(f"/api/applications/{application_id}/outcome-summary")
    _assert_status(summary.status_code, 200, "http_application_outcome_summary")
    body = summary.json()
    if body.get("total") != 1 or body.get("result_counts", {}).get("advanced") != 1:
        raise RuntimeError("application outcome summary did not include the confirmed result")
    steps.append(
        SmokeStep(
            "http_application_outcome_feedback",
            "UI snapshot replayed and provider-free Pilot outcome was confirmed",
        )
    )


def _run_real_ai_write_smoke(
    client: httpx.Client,
    steps: list[SmokeStep],
    company: str,
    application_id: int,
) -> None:
    prompt = (
        "Verification smoke: use the update_application_status tool to change the existing "
        f"application for {company} with id {application_id} to status offer. "
        "Do not create any other records."
    )
    pending = client.post("/api/chat", json={"message": prompt, "conversation_id": 0})
    _assert_status(pending.status_code, 200, "http_chat_pending")
    pending_body = pending.json()
    conversation_id = int(pending_body["conversation_id"])
    if pending_body.get("type") == "confirmation_required":
        if pending_body.get("pending_action", {}).get("tool_name") != "update_application_status":
            raise RuntimeError("real-ai smoke requested an unexpected pending tool")
        steps.append(SmokeStep("http_chat_pending", "real AI requested write confirmation"))
        confirmed = client.post(
            "/api/chat/confirm",
            json={
                "conversation_id": conversation_id,
                "approved": True,
                "confirmation_token": pending_body["pending_action"]["confirmation_token"],
            },
        )
        _assert_status(confirmed.status_code, 200, "http_confirm_action")
    else:
        steps.append(SmokeStep("http_chat_pending", "real AI completed without pending confirmation"))

    updated = client.get(f"/api/applications/{application_id}")
    _assert_status(updated.status_code, 200, "http_confirm_action")
    if updated.json()["status"] != "offer":
        raise RuntimeError("real-ai smoke did not update the application to offer")
    steps.append(SmokeStep("http_confirm_action", "real AI write updated application"))

    conversations = client.get("/api/chat/conversations")
    _assert_status(conversations.status_code, 200, "http_pending_cleared")
    match = next((item for item in conversations.json() if item["id"] == conversation_id), None)
    if match is None:
        raise RuntimeError("real-ai smoke conversation was not listed")
    if match["pending_action"] is not None:
        raise RuntimeError("real-ai smoke pending action was not cleared")
    steps.append(SmokeStep("http_pending_cleared", "real AI conversation has no pending action"))


@contextmanager
def _running_server(app: Any) -> Any:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        _stop_smoke_server(server, thread, app)
        raise RuntimeError("http smoke server did not become ready")
    try:
        yield base_url
    finally:
        _stop_smoke_server(server, thread, app)


def _stop_smoke_server(server: Any, thread: Any, app: Any) -> None:
    shutdown_deadline = time.monotonic() + 30
    server.should_exit = True
    thread.join(timeout=5)
    if thread.is_alive():
        remaining = max(0.0, shutdown_deadline - time.monotonic())
        if remaining > 0:
            thread.join(timeout=remaining)
    if thread.is_alive():
        raise SmokeServerShutdownError("HTTP smoke server did not stop before database disposal")
    _dispose_smoke_app_database(app)


def _dispose_smoke_app_database(app: Any) -> None:
    runtime = getattr(app.state, "knowledge_runtime", None)
    if runtime is not None:
        stopped = runtime.stop(timeout=10)
        if not stopped:
            # Do not dispose the engine or remove the isolated directory while a
            # worker can still touch SQLite. The second call intentionally waits
            # for the worker's safe exit instead of performing best-effort cleanup.
            stopped = runtime.stop(timeout=None)
        if not stopped or runtime.running:
            raise RuntimeError("Knowledge worker did not stop before database disposal")
    engine = getattr(app.state, "db_engine", None)
    if engine is not None:
        engine.dispose()
    journal_engine = getattr(app.state, "journal_db_engine", None)
    if journal_engine is not None and journal_engine is not engine:
        journal_engine.dispose()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_status(actual: int, expected: int, step: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{step} returned status {actual}, expected {expected}")
