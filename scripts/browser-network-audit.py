from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse
from urllib.request import urlopen

import websockets
from websockets.exceptions import ConnectionClosed


_CDP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def record_response_payload_metadata(
    record: dict[str, object], payload: object, request_url: str | None = None
) -> None:
    """Copy only safe identifiers and statuses from a JSON response into a record."""
    if isinstance(payload, list):
        proposal_ids = [
            item["id"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]
        version_ids = [
            item["id"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]
        source_kinds = sorted(
            {
                item["source_kind"]
                for item in payload
                if isinstance(item, dict) and item.get("source_kind") in {"ui", "pilot"}
            }
        )
        if proposal_ids:
            record["response_proposal_ids"] = proposal_ids
        if version_ids:
            record["response_jd_version_ids"] = version_ids
        if source_kinds:
            record["response_source_kinds"] = source_kinds
        return
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("error_code"), str):
        record["response_error_code"] = payload["error_code"]
    if isinstance(payload.get("attempt_status"), str):
        record["response_attempt_status"] = payload["attempt_status"]
    if payload.get("source_kind") in {"ui", "pilot"}:
        record["response_source_kind"] = payload["source_kind"]
    if isinstance(payload.get("retry_after_ms"), int):
        record["response_retry_after_ms"] = payload["retry_after_ms"]
    if isinstance(payload.get("jd_version_id"), int):
        record["response_jd_version_id"] = payload["jd_version_id"]
    if isinstance(payload.get("id"), int):
        record["response_proposal_id"] = payload["id"]
        if isinstance(payload.get("version_number"), int):
            record["response_jd_version_id"] = payload["id"]
    current = payload.get("current")
    if isinstance(current, dict) and isinstance(current.get("id"), int):
        record["response_jd_version_id"] = current["id"]
        if current.get("source_kind") in {"ui", "pilot"}:
            record["response_source_kind"] = current["source_kind"]
    brief = payload.get("brief")
    if isinstance(brief, dict) and isinstance(brief.get("proposal_id"), int):
        record["response_confirmed_proposal_id"] = brief["proposal_id"]
    if isinstance(payload.get("proposal_id"), int):
        record["response_confirmed_proposal_id"] = payload["proposal_id"]
    request_path = urlparse(request_url).path if isinstance(request_url, str) else ""
    story_detail_segments = request_path.strip("/").split("/")
    is_story_detail = (
        len(story_detail_segments) == 3
        and story_detail_segments[:2] == ["api", "interview-stories"]
        and story_detail_segments[2].isascii()
        and story_detail_segments[2].isdigit()
    )
    if is_story_detail and isinstance(payload.get("id"), int):
        record["response_story_id"] = payload["id"]
        version = payload.get("version")
        if isinstance(version, dict) and isinstance(version.get("id"), int):
            record["response_story_current_version_id"] = version["id"]
    if isinstance(payload.get("story_id"), int):
        record["response_story_id"] = payload["story_id"]
    if isinstance(payload.get("version_id"), int):
        record["response_story_version_id"] = payload["version_id"]
    stages = payload.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, dict) and isinstance(stage.get("jd_version_id"), int):
                record["response_jd_version_id"] = stage["jd_version_id"]
                break


_STORY_INTERACTION_ACTIONS = frozenset(
    {
        "ui-library",
        "ui-source-picker",
        "ui-generate",
        "ui-confirm",
        "pilot-entry",
        "pilot-source-picker",
        "pilot-generate",
        "pilot-confirm",
    }
)
_STORY_INTERACTION_OBSERVER = """
(() => {
  const actions = new Set(%s);
  window.__offerpilotStoryAuditSteps = [];
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target.closest('[data-story-audit]') : null;
    const action = target?.getAttribute('data-story-audit');
    if (action && actions.has(action)) window.__offerpilotStoryAuditSteps.push(action);
  }, true);
})();
""" % json.dumps(sorted(_STORY_INTERACTION_ACTIONS))


class BrowserAudit:
    def __init__(
        self,
        websocket: websockets.ClientConnection,
        output: Path,
        stop_file: Path,
        diagnostic: Path | None = None,
        flush_file: Path | None = None,
        flushed_file: Path | None = None,
        private_context_file: Path | None = None,
    ) -> None:
        self.websocket = websocket
        self.output = output
        self.stop_file = stop_file
        self.diagnostic = diagnostic
        self.flush_file = flush_file
        self.flushed_file = flushed_file
        self.private_context_file = private_context_file
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self.target_sessions: dict[str, str] = {}
        self.pending_targets: dict[str, tuple[str, object]] = {}
        self.owned_targets: set[str] = set()
        self.main_target_id: str | None = None
        self.main_session_id: str | None = None
        self.main_network_ready = asyncio.Event()
        self.reader_error: BaseException | None = None
        self.handle: TextIO | None = None
        self.network_ready_targets: set[str] = set()
        self.request_records: dict[tuple[str, str], dict[str, object]] = {}
        self.response_tasks: set[asyncio.Task[None]] = set()
        self.response_finished: dict[tuple[str, str], asyncio.Event] = {}
        self.failure_category: str | None = None
        self.close_code: int | None = None
        self.last_event = "startup"
        self.started_at = time.monotonic()

    def _set_failure(self, category: str, error: BaseException | None = None) -> None:
        if self.failure_category is None:
            self.failure_category = category
        if error is not None and self.reader_error is None:
            self.reader_error = error

    def _retain_private_triage_context(self, payload: dict[str, object], post_data: str) -> None:
        if self.private_context_file is None or self.private_context_file.exists():
            return
        try:
            self.private_context_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.private_context_file.with_suffix(
                self.private_context_file.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(
                    {
                        "kind": "triage_request_context",
                        "payload": payload,
                        "raw_post_data": post_data,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(self.private_context_file)
        except BaseException as exc:
            self._set_failure("private_context_write_error", exc)

    def _write_diagnostic(self) -> None:
        if self.diagnostic is None:
            return
        self.diagnostic.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "passed" if self.failure_category is None else "failed",
            "failure_category": self.failure_category,
            "stop_requested": self.stop_file.exists(),
            "ready": self.main_network_ready.is_set(),
            "main_target_id": self.main_target_id,
            "main_session_id": self.main_session_id,
            "network_ready_target_count": len(self.network_ready_targets),
            "request_count": len(self.request_records),
            "response_count": sum(
                1
                for record in self.request_records.values()
                if "response_status" in record
            ),
            "last_event": self.last_event,
            "elapsed_ms": max(0, int((time.monotonic() - self.started_at) * 1000)),
        }
        if self.close_code is not None:
            payload["close_code"] = self.close_code
        self.diagnostic.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )

    def finish_response_task(self, task: asyncio.Task[None]) -> None:
        self.response_tasks.discard(task)
        if task.cancelled() or self.reader_error is not None:
            return
        try:
            error = task.exception()
        except (asyncio.CancelledError, BaseException):
            error = RuntimeError("CDP response capture failed")
        if error is not None:
            self.reader_error = RuntimeError("CDP response capture failed")

    async def send(
        self,
        method: str,
        params: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        command_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self.pending[command_id] = future
        message: dict[str, object] = {"id": command_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        try:
            await self.websocket.send(json.dumps(message))
            response = await future
        except BaseException:
            self.pending.pop(command_id, None)
            raise
        if "error" in response:
            raise RuntimeError(f"CDP command failed: {method}")
        return response

    async def reader(self) -> None:
        try:
            async for raw in self.websocket:
                message = json.loads(raw)
                self.last_event = str(message.get("method") or "command_response")
                command_id = message.get("id")
                if isinstance(command_id, int) and command_id in self.pending:
                    future = self.pending.pop(command_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                if message.get("method") == "Target.attachedToTarget":
                    await self.attached(message["params"])
                elif message.get("method") == "Target.detachedFromTarget":
                    params = message.get("params")
                    target_id = params.get("targetId") if isinstance(params, dict) else None
                    session_id = params.get("sessionId") if isinstance(params, dict) else None
                    if (
                        not self.stop_file.exists()
                        and (target_id == self.main_target_id or session_id == self.main_session_id)
                    ):
                        self._set_failure(
                            "dedicated_target_detached",
                            RuntimeError("dedicated browser target detached before audit stop"),
                        )
                elif message.get("method") == "Target.targetDestroyed":
                    params = message.get("params")
                    target_id = params.get("targetId") if isinstance(params, dict) else None
                    if not self.stop_file.exists() and target_id == self.main_target_id:
                        self._set_failure(
                            "dedicated_target_destroyed",
                            RuntimeError("dedicated browser target destroyed before audit stop"),
                        )
                elif message.get("method") == "Network.requestWillBeSent":
                    await self.record_request(message)
                elif message.get("method") == "Network.responseReceived":
                    params = message.get("params")
                    request_id = params.get("requestId") if isinstance(params, dict) else None
                    session_id = message.get("sessionId")
                    if isinstance(session_id, str) and isinstance(request_id, str):
                        self.response_finished.setdefault((session_id, request_id), asyncio.Event())
                    task = asyncio.create_task(self.record_response(message))
                    self.response_tasks.add(task)
                    task.add_done_callback(self.finish_response_task)
                elif message.get("method") in {"Network.loadingFinished", "Network.loadingFailed"}:
                    params = message.get("params")
                    request_id = params.get("requestId") if isinstance(params, dict) else None
                    session_id = message.get("sessionId")
                    if isinstance(session_id, str) and isinstance(request_id, str):
                        finished = self.response_finished.setdefault((session_id, request_id), asyncio.Event())
                        finished.set()
            if not self.stop_file.exists():
                self._set_failure(
                    "cdp_connection_closed",
                    RuntimeError("CDP connection closed before audit stop"),
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, ConnectionClosed):
                self.close_code = exc.code
                self._set_failure("cdp_connection_closed", exc)
            else:
                self._set_failure("cdp_reader_error", exc)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(exc)

    async def attached(self, params: object) -> None:
        self.last_event = "Target.attachedToTarget"
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        target_info = params.get("targetInfo")
        if not isinstance(session_id, str) or not isinstance(target_info, dict):
            return
        target_id = target_info.get("targetId")
        opener_id = target_info.get("openerId")
        if not isinstance(target_id, str):
            return
        if self.main_target_id is None:
            self.pending_targets[target_id] = (session_id, target_info)
            return
        owned = target_id == self.main_target_id or opener_id == self.main_target_id or opener_id in self.owned_targets
        if not owned:
            asyncio.create_task(self.send("Target.detachFromTarget", {"sessionId": session_id}))
            return
        self.pending_targets.pop(target_id, None)
        self.target_sessions[target_id] = session_id
        if target_id == self.main_target_id:
            self.main_session_id = session_id
        asyncio.create_task(self.enable_target(target_id, session_id))

    async def enable_target(self, target_id: str, session_id: str) -> None:
        try:
            await self.send("Network.enable", session_id=session_id)
            await self.send("Page.enable", session_id=session_id)
            await self.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _STORY_INTERACTION_OBSERVER},
                session_id=session_id,
            )
            await self.send("Runtime.runIfWaitingForDebugger", session_id=session_id)
            self.owned_targets.add(target_id)
            self.network_ready_targets.add(target_id)
            if target_id == self.main_target_id:
                self.main_network_ready.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            category = "network_enable_rejected" if "Network.enable" in str(exc) else "target_enable_error"
            self._set_failure(category, exc)

    async def keep_browser_session(self) -> None:
        """Keep the browser-level CDP session active during manual acceptance."""
        try:
            while not self.stop_file.exists():
                await asyncio.wait_for(self.send("Browser.getVersion"), timeout=10.0)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            category = "cdp_connection_closed" if isinstance(exc, ConnectionClosed) else "cdp_keepalive_error"
            self._set_failure(category, exc)

    async def flush_response_tasks(self) -> None:
        while self.response_tasks:
            await asyncio.gather(*tuple(self.response_tasks), return_exceptions=True)
            await asyncio.sleep(0)
        if self.flushed_file is not None:
            self.flushed_file.parent.mkdir(parents=True, exist_ok=True)
            self.flushed_file.touch()

    async def record_story_interactions(self) -> None:
        """Persist only allowlisted user-action names from the owned CDP page."""

        if self.handle is None or self.main_target_id is None or self.main_session_id is None:
            raise RuntimeError("story interaction audit has no dedicated target")
        response = await self.send(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify(window.__offerpilotStoryAuditSteps || [])",
                "returnByValue": True,
            },
            self.main_session_id,
        )
        result = response.get("result")
        value = result.get("result") if isinstance(result, dict) else None
        raw_steps = value.get("value") if isinstance(value, dict) else None
        try:
            parsed = json.loads(raw_steps) if isinstance(raw_steps, str) else None
        except json.JSONDecodeError as exc:
            raise RuntimeError("story interaction audit payload is invalid") from exc
        if not isinstance(parsed, list) or any(
            not isinstance(item, str) or item not in _STORY_INTERACTION_ACTIONS
            for item in parsed
        ):
            raise RuntimeError("story interaction audit payload is invalid")
        self.handle.write(
            json.dumps(
                {
                    "kind": "browser_story_interactions",
                    "observed_at_ns": time.time_ns(),
                    "target_id": self.main_target_id,
                    "session_id": self.main_session_id,
                    "steps": parsed,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.handle.flush()

    async def record_request(self, message: dict[str, object]) -> None:
        self.last_event = "Network.requestWillBeSent"
        session_id = message.get("sessionId")
        if not isinstance(session_id, str) or session_id not in self.target_sessions.values():
            return
        target_id = next(
            (candidate for candidate, candidate_session in self.target_sessions.items() if candidate_session == session_id),
            None,
        )
        params = message.get("params")
        request = params.get("request") if isinstance(params, dict) else None
        method = request.get("method") if isinstance(request, dict) else None
        url = request.get("url") if isinstance(request, dict) else None
        if target_id and isinstance(method, str) and isinstance(url, str) and self.handle is not None:
            record: dict[str, object] = {
                "kind": "browser_request",
                "observed_at_ns": time.time_ns(),
                "target_id": target_id,
                "session_id": session_id,
                "method": method,
                "url": url,
            }
            post_data = request.get("postData") if isinstance(request, dict) else None
            if isinstance(post_data, str):
                try:
                    payload = json.loads(post_data)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    if (
                        method == "POST"
                        and re.search(r"/api/applications/\d+/opportunity-fit-reviews$", url)
                        and payload.get("schema_version") == 2
                    ):
                        self._retain_private_triage_context(payload, post_data)
                    request_context: dict[str, object] = {}
                    route_match = re.search(r"/api/applications/(\d+)(?:/|$)", url)
                    route_application_id = int(route_match.group(1)) if route_match else None
                    if route_application_id is not None:
                        # The application identity in a bound URL is authoritative.
                        # Keep a payload copy separately so the harness can reject
                        # a request that tries to cross-bind another application.
                        request_context["application_id"] = route_application_id
                    request_context["payload_sha256"] = hashlib.sha256(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
                    for key in (
                        "application_id",
                        "resume_id",
                        "event_id",
                        "jd_version_id",
                        "expected_current_version_id",
                    ):
                        if isinstance(payload.get(key), int):
                            if key == "application_id":
                                if route_application_id is None:
                                    request_context[key] = payload[key]
                                else:
                                    request_context["payload_application_id"] = payload[key]
                            else:
                                request_context[key] = payload[key]
                    if isinstance(payload.get("jd_text"), str):
                        request_context["jd_text_sha256"] = hashlib.sha256(
                            payload["jd_text"].encode("utf-8")
                        ).hexdigest()
                    for key in ("idempotency_key", "confirmation_key", "confirmation_token"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            request_context[f"{key}_sha256"] = hashlib.sha256(
                                value.encode("utf-8")
                            ).hexdigest()
                    selected_blocks = payload.get("selected_blocks")
                    if isinstance(selected_blocks, list):
                        request_context["selected_blocks_sha256"] = hashlib.sha256(
                            json.dumps(
                                selected_blocks,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                    headers = request.get("headers") if isinstance(request, dict) else None
                    entrypoint = None
                    if isinstance(headers, dict):
                        entrypoint = headers.get("X-OfferPilot-Entrypoint", headers.get("x-offerpilot-entrypoint"))
                    if isinstance(entrypoint, str) and entrypoint in {"ui", "pilot"}:
                        request_context["entrypoint"] = entrypoint
                    elif url.endswith("/api/interview-story-proposals"):
                        request_context["entrypoint"] = "ui"
                    elif url.endswith("/api/pilot/interview-story-proposals"):
                        request_context["entrypoint"] = "pilot"
                    if request_context:
                        record["request_context"] = request_context
            self.handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.handle.flush()
            request_params = message.get("params")
            request_id = request_params.get("requestId") if isinstance(request_params, dict) else None
            if isinstance(request_id, str):
                self.request_records[(session_id, request_id)] = record

    async def record_response(self, message: dict[str, object]) -> None:
        self.last_event = "Network.responseReceived"
        session_id = message.get("sessionId")
        params = message.get("params")
        if not isinstance(session_id, str) or not isinstance(params, dict):
            return
        request_id = params.get("requestId")
        response = params.get("response")
        if not isinstance(request_id, str) or not isinstance(response, dict):
            return
        record = self.request_records.get((session_id, request_id))
        if record is None:
            return
        status = response.get("status")
        if isinstance(status, (int, float)):
            record["response_status"] = int(status)
        request_url = record.get("url")
        if not isinstance(request_url, str) or not urlparse(request_url).path.startswith("/api/"):
            # Static assets can exceed Chrome's default CDP message ceiling.
            # They have no workflow metadata and must never have their body
            # fetched or retained by this redacted audit.
            record["response_body_status"] = "not_requested"
        else:
            try:
                headers = response.get("headers")
                content_type = ""
                if isinstance(headers, dict):
                    raw_content_type = headers.get(
                        "content-type", headers.get("Content-Type")
                    )
                    if isinstance(raw_content_type, str):
                        content_type = raw_content_type.split(";", 1)[0].strip().lower()
                mime_type = response.get("mimeType")
                if content_type == "text/event-stream" or mime_type == "text/event-stream":
                    record["response_body_status"] = "not_requested"
                    return self._write_response_record(session_id, record)
                finished = self.response_finished.setdefault((session_id, request_id), asyncio.Event())
                body_ready = True
                try:
                    await asyncio.wait_for(finished.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    body_ready = False
                if not body_ready:
                    raise RuntimeError("response body did not finish before audit timeout")
                body_result = await self.send("Network.getResponseBody", {"requestId": request_id}, session_id)
                body = body_result.get("result")
                body_text = body.get("body") if isinstance(body, dict) else None
                payload = json.loads(body_text) if isinstance(body_text, str) else None
                if isinstance(payload, (dict, list)):
                    record_response_payload_metadata(record, payload, request_url)
                record["response_body_status"] = "captured"
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Keep the audit fail-closed without storing an exception message or
                # response body.  A response capture failure means the auditor can
                # no longer prove the browser contract, including if CDP itself
                # disconnected while a body was being read.
                record["response_body_status"] = "unavailable"
                if isinstance(exc, ConnectionClosed):
                    self.close_code = exc.code
                    self._set_failure("cdp_connection_closed", exc)
                elif isinstance(exc, (RuntimeError, asyncio.TimeoutError, json.JSONDecodeError, TypeError)):
                    self._set_failure("response_body_unavailable", exc)
                else:
                    self._set_failure(
                        "response_audit_error",
                        RuntimeError("CDP response capture failed"),
                    )
        self._write_response_record(session_id, record)

    def _write_response_record(
        self, session_id: str, record: dict[str, object]
    ) -> None:
        if self.handle is not None:
            response_record = {
                "kind": "browser_response",
                "observed_at_ns": time.time_ns(),
                "target_id": record.get("target_id"),
                "session_id": session_id,
                "method": record.get("method"),
                "url": record.get("url"),
                "response_status": record.get("response_status"),
            }
            if isinstance(record.get("request_context"), dict):
                response_record["request_context"] = record["request_context"]
            for key in (
                "response_error_code",
                "response_attempt_status",
                "response_source_kind",
                "response_source_kinds",
                "response_proposal_id",
                "response_proposal_ids",
                "response_confirmed_proposal_id",
                "response_jd_version_id",
                "response_jd_version_ids",
                "response_story_id",
                "response_story_current_version_id",
                "response_story_version_id",
                "response_body_status",
            ):
                if key in record:
                    response_record[key] = record[key]
            if "response_retry_after_ms" in record:
                response_record["response_retry_after_ms"] = record["response_retry_after_ms"]
            self.handle.write(json.dumps(response_record, ensure_ascii=False) + "\n")
            self.handle.flush()

    async def run(self, base_url: str, ready_file: Path, ready_timeout_seconds: float) -> None:
        reader_task = asyncio.create_task(self.reader())
        keepalive_task: asyncio.Task[None] | None = None
        try:
            await self.send("Target.setDiscoverTargets", {"discover": True})
            await self.send("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
            })
            created = await self.send("Target.createTarget", {"url": "about:blank"})
            result = created.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("targetId"), str):
                raise RuntimeError("CDP did not create the dedicated browser target")
            self.main_target_id = result["targetId"]
            pending_targets = list(self.pending_targets.items())
            self.pending_targets.clear()
            for target_id, (session_id, _target_info) in pending_targets:
                if target_id == self.main_target_id:
                    self.target_sessions[target_id] = session_id
                    self.main_session_id = session_id
                    asyncio.create_task(self.enable_target(target_id, session_id))
                else:
                    asyncio.create_task(self.send("Target.detachFromTarget", {"sessionId": session_id}))
            if self.main_target_id in self.network_ready_targets:
                self.main_session_id = self.target_sessions.get(self.main_target_id)
                self.owned_targets.add(self.main_target_id)
                self.main_network_ready.set()
            deadline = time.monotonic() + ready_timeout_seconds
            while not self.main_network_ready.is_set() and time.monotonic() < deadline:
                if self.reader_error is not None:
                    raise self.reader_error
                await asyncio.sleep(0.05)
            if not self.main_network_ready.is_set() or self.main_session_id is None:
                if self.failure_category is None:
                    self._set_failure("ready_timeout")
                raise RuntimeError("dedicated browser target did not complete Network.enable")
            with self.output.open("w", encoding="utf-8") as handle:
                self.handle = handle
                await asyncio.wait_for(
                    self.send("Page.navigate", {"url": base_url}, self.main_session_id),
                    timeout=10.0,
                )
                ready_file.write_text(
                    json.dumps(
                        {
                            "target_id": self.main_target_id,
                            "session_id": self.main_session_id,
                        },
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                self.last_event = "browser_ready"
                keepalive_task = asyncio.create_task(self.keep_browser_session())
                while not self.stop_file.exists():
                    if self.reader_error is not None:
                        raise self.reader_error
                    if self.flush_file is not None and self.flush_file.exists():
                        self.flush_file.unlink()
                        await self.flush_response_tasks()
                    await asyncio.sleep(0.1)
                if self.reader_error is not None:
                    raise self.reader_error
                await self.flush_response_tasks()
                await self.record_story_interactions()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self.failure_category is None:
                self._set_failure("cdp_runtime_error", exc)
            raise
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
                await asyncio.gather(keepalive_task, return_exceptions=True)
            await self.flush_response_tasks()
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            self._write_diagnostic()


def browser_websocket_url(debugging_url: str) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{debugging_url.rstrip('/')}/json/version", timeout=5) as response:
                version = json.load(response)
            websocket_url = version.get("webSocketDebuggerUrl")
            if isinstance(websocket_url, str) and websocket_url:
                return websocket_url
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("browser-level CDP endpoint did not become available")


def write_startup_diagnostic(path: Path | None, category: str) -> None:
    if path is None or path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_category": category,
                "stop_requested": False,
                "ready": False,
                "main_target_id": None,
                "main_session_id": None,
                "network_ready_target_count": 0,
                "request_count": 0,
                "response_count": 0,
                "last_event": "startup",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )


async def main_async(args: argparse.Namespace) -> None:
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        websocket_url = browser_websocket_url(args.debugging_url)
        async with websockets.connect(
            websocket_url,
            open_timeout=5,
            max_size=_CDP_MAX_MESSAGE_BYTES,
        ) as websocket:
            audit = BrowserAudit(
                websocket,
                args.audit,
                args.stop_file,
                args.diagnostic_file,
                args.flush_file,
                args.flushed_file,
                args.private_context_file,
            )
            await audit.run(args.expected_url, args.ready_file, args.ready_timeout_seconds)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        if isinstance(exc, ConnectionClosed):
            category = "cdp_connection_closed"
        elif isinstance(exc, asyncio.TimeoutError):
            category = "cdp_command_timeout"
        else:
            category = "cdp_startup_error"
        write_startup_diagnostic(args.diagnostic_file, category)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debugging-url", required=True)
    parser.add_argument("--expected-url", required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--diagnostic-file", type=Path, default=None)
    parser.add_argument("--flush-file", type=Path, default=None)
    parser.add_argument("--flushed-file", type=Path, default=None)
    parser.add_argument("--private-context-file", type=Path, default=None)
    parser.add_argument("--ready-timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
