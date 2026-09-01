from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import struct
from contextlib import suppress
import time
import zlib

import pytest


_AUDIT_PATH = Path(__file__).parents[1] / "scripts" / "browser-network-audit.py"
_HARNESS_PATH = Path(__file__).parents[1] / "scripts" / "interview-story-real-ai-browser-harness.ps1"
_HARNESS_TIMEOUT_SECONDS = 180
_HARNESS_DIAGNOSTIC_TAIL_CHARS = 8_000
_SPEC = importlib.util.spec_from_file_location("browser_network_audit", _AUDIT_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
BrowserAudit = _MODULE.BrowserAudit


class _WebSocket:
    async def send(self, message: str) -> None:
        del message


class _ScriptedWebSocket:
    def __init__(self, *, attach_target: str | None = "story-target", reject_network: bool = False) -> None:
        self.attach_target = attach_target
        self.reject_network = reject_network
        self.messages: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

    async def send(self, message: str) -> None:
        command = json.loads(message)
        command_id = command["id"]
        method = command["method"]
        if method == "Target.createTarget":
            if self.attach_target is not None:
                await self.messages.put({
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": "story-session",
                        "targetInfo": {"targetId": self.attach_target, "type": "page"},
                    },
                })
            await self.messages.put({"id": command_id, "result": {"targetId": "story-target"}})
            return
        if method == "Network.enable" and self.reject_network:
            await self.messages.put({"id": command_id, "error": {"message": "rejected"}})
            return
        if method == "Runtime.evaluate":
            await self.messages.put({"id": command_id, "result": {"result": {"value": "[]"}}})
            return
        await self.messages.put({"id": command_id, "result": {}})

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)


def _request(url: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "sessionId": "story-session",
        "params": {
            "requestId": "request-1",
            "request": {"method": "POST", "url": url, "postData": json.dumps(payload)},
        },
    }


def _process_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _run_harness(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_HARNESS_PATH),
        *args,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=_HARNESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        cleanup_detail = ""
        try:
            cleanup = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            cleanup_detail = (
                f"taskkill returncode={cleanup.returncode}\n"
                f"{cleanup.stdout}{cleanup.stderr}"
            )
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            cleanup_detail = f"taskkill failed: {type(cleanup_error).__name__}: {cleanup_error}"
        if process.poll() is None:
            process.kill()
        final_stdout, final_stderr = process.communicate()
        stdout = final_stdout or _process_output_text(exc.output)
        stderr = final_stderr or _process_output_text(exc.stderr)
        raise AssertionError(
            f"Interview Story browser harness timed out after {_HARNESS_TIMEOUT_SECONDS} seconds.\n"
            f"stdout tail:\n{stdout[-_HARNESS_DIAGNOSTIC_TAIL_CHARS:]}\n"
            f"stderr tail:\n{stderr[-_HARNESS_DIAGNOSTIC_TAIL_CHARS:]}\n"
            f"cleanup:\n{cleanup_detail[-_HARNESS_DIAGNOSTIC_TAIL_CHARS:]}"
        ) from exc
    assert process.returncode is not None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def test_run_harness_terminates_the_process_tree_and_reports_output_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutProcess:
        pid = 4242
        returncode: int | None = None

        def __init__(self) -> None:
            self.communicate_timeouts: list[float | None] = []
            self.killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.communicate_timeouts.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(
                    ["powershell"], timeout, output="partial stdout", stderr="partial stderr"
                )
            return "final stdout", "final stderr"

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = 1

    process = TimedOutProcess()
    popen_calls: list[tuple[object, ...]] = []
    taskkill_calls: list[list[str]] = []

    def fake_popen(*popen_args: object, **_popen_kwargs: object) -> TimedOutProcess:
        popen_calls.append(popen_args)
        return process

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        assert command == ["taskkill", "/PID", "4242", "/T", "/F"]
        process.returncode = 1
        return subprocess.CompletedProcess(command, 0, "terminated", "")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AssertionError, match="timed out after 180 seconds") as caught:
        _run_harness("-ValidateAudit")

    assert len(popen_calls) == 1
    assert taskkill_calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert process.communicate_timeouts == [180, None]
    assert process.killed is False
    assert "final stdout" in str(caught.value)
    assert "final stderr" in str(caught.value)


def _write_gray_png(path: Path, *, width: int = 1455, height: int = 1200) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    scanlines = (b"\x00" + b"\xff" * width) * height
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + chunk(b"IEND", b"")
    )


def _story_interaction_record(*, target_id: str = "story-target", session_id: str = "story-session") -> dict[str, object]:
    return {
        "kind": "browser_story_interactions",
        "observed_at_ns": 9_999_000,
        "target_id": target_id,
        "session_id": session_id,
        "steps": [
            "ui-library",
            "ui-source-picker",
            "ui-generate",
            "ui-confirm",
            "pilot-entry",
            "pilot-source-picker",
            "pilot-generate",
            "pilot-confirm",
        ],
    }


def _complete_story_audit_records(base_url: str) -> list[dict[str, object]]:
    ui_context = {"entrypoint": "ui", "idempotency_key_sha256": "ui-key", "payload_sha256": "ui-payload"}
    pilot_context = {"entrypoint": "pilot", "idempotency_key_sha256": "pilot-key", "payload_sha256": "pilot-payload"}
    records: list[dict[str, object]] = [
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 11},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/11/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 101, "response_story_version_id": 201},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/101", "response_status": 200, "response_body_status": "captured", "response_story_id": 101, "response_story_current_version_id": 201},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 12},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/12/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 102, "response_story_version_id": 202},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/102", "response_status": 200, "response_body_status": "captured", "response_story_id": 102, "response_story_current_version_id": 202},
    ]
    for index, record in enumerate(records, 1):
        record["observed_at_ns"] = 1_000_000 + index * 1_000
        record["target_id"] = "story-target"
        record["session_id"] = "story-session"
    interactions = _story_interaction_record()
    interactions["observed_at_ns"] = 1_000_000 + (len(records) + 1) * 1_000
    records.append(interactions)
    return records


def _write_audit(
    path: Path,
    records: list[dict[str, object]],
    *,
    include_interactions: bool = True,
) -> None:
    output_records = list(records)
    if include_interactions and not any(record.get("kind") == "browser_story_interactions" for record in output_records):
        output_records.append(_story_interaction_record())
    path.write_text("\n".join(json.dumps(record) for record in output_records) + "\n", encoding="utf-8")


def test_story_browser_harness_rejects_a_network_only_pilot_flow_without_dedicated_ui_actions(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:8123"
    records = [record for record in _complete_story_audit_records(base_url) if record["kind"] != "browser_story_interactions"]
    audit_path = tmp_path / "network-only-story-audit.jsonl"
    _write_audit(audit_path, records, include_interactions=False)

    result = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )

    assert result.returncode != 0
    assert "dedicated Story interaction audit" in (result.stdout + result.stderr)


def test_browser_audit_derives_story_entrypoint_and_hashes_retry_tokens(tmp_path: Path) -> None:
    async def run() -> list[dict[str, object]]:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")
        audit.target_sessions["story-target"] = "story-session"
        handle = io.StringIO()
        audit.handle = handle
        await audit.record_request(
            _request(
                "http://127.0.0.1:9999/api/interview-story-proposals",
                {"idempotency_key": "story-ui-audit-key-0001"},
            )
        )
        await audit.record_request(
            _request(
                "http://127.0.0.1:9999/api/pilot/interview-story-proposals",
                {"idempotency_key": "story-pilot-audit-key-01"},
            )
        )
        await audit.record_request(
            _request(
                "http://127.0.0.1:9999/api/interview-story-proposals/8/confirm",
                {"confirmation_token": "story-confirm-audit-01"},
            )
        )
        return [json.loads(line) for line in handle.getvalue().splitlines()]

    records = asyncio.run(run())

    assert records[0]["request_context"]["entrypoint"] == "ui"
    assert records[1]["request_context"]["entrypoint"] == "pilot"
    assert "idempotency_key_sha256" in records[0]["request_context"]
    assert "confirmation_token_sha256" in records[2]["request_context"]


def test_browser_audit_waits_for_loading_finished_before_capturing_workflow_response(tmp_path: Path) -> None:
    async def run() -> list[dict[str, object]]:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")
        audit.target_sessions["story-target"] = "story-session"
        handle = io.StringIO()
        audit.handle = handle
        await audit.record_request(
            _request(
                "http://127.0.0.1:9999/api/interview-story-proposals",
                {"idempotency_key": "story-ui-audit-key-0001"},
            )
        )
        sent = asyncio.Event()

        async def response_body(*_args, **_kwargs):
            sent.set()
            return {"result": {"body": json.dumps({"id": 8, "attempt_status": "ready"})}}

        audit.send = response_body  # type: ignore[method-assign]
        response = {
            "sessionId": "story-session",
            "params": {"requestId": "request-1", "response": {"status": 201}},
        }
        audit.response_finished[("story-session", "request-1")] = asyncio.Event()
        task = asyncio.create_task(audit.record_response(response))
        await asyncio.sleep(0)
        assert not sent.is_set()
        audit.response_finished[("story-session", "request-1")].set()
        await task
        return [json.loads(line) for line in handle.getvalue().splitlines()]

    records = asyncio.run(run())

    assert records[-1]["response_body_status"] == "captured"
    assert records[-1]["response_proposal_id"] == 8


def test_browser_audit_records_current_story_version_without_retaining_story_content(tmp_path: Path) -> None:
    async def run() -> list[dict[str, object]]:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")
        audit.target_sessions["story-target"] = "story-session"
        handle = io.StringIO()
        audit.handle = handle
        await audit.record_request(
            _request("http://127.0.0.1:9999/api/interview-stories/101", {})
        )

        async def response_body(*_args, **_kwargs):
            return {
                "result": {
                    "body": json.dumps(
                        {"id": 101, "version": {"id": 201, "content": {"title": "do not retain"}}}
                    )
                }
            }

        audit.send = response_body  # type: ignore[method-assign]
        audit.response_finished[("story-session", "request-1")] = asyncio.Event()
        audit.response_finished[("story-session", "request-1")].set()
        await audit.record_response(
            {
                "sessionId": "story-session",
                "params": {"requestId": "request-1", "response": {"status": 200}},
            }
        )
        return [json.loads(line) for line in handle.getvalue().splitlines()]

    record = asyncio.run(run())[-1]

    assert record["response_story_id"] == 101
    assert record["response_story_current_version_id"] == 201
    assert "do not retain" not in json.dumps(record, ensure_ascii=False)


def test_browser_audit_never_reads_non_api_response_bodies(tmp_path: Path) -> None:
    async def run() -> tuple[list[str], list[dict[str, object]]]:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")
        audit.target_sessions["story-target"] = "story-session"
        handle = io.StringIO()
        audit.handle = handle
        await audit.record_request(
            _request("http://127.0.0.1:9999/assets/application.js", {})
        )
        calls: list[str] = []

        async def send(method: str, *_args, **_kwargs):
            calls.append(method)
            return {"result": {"body": "{}"}}

        audit.send = send  # type: ignore[method-assign]
        audit.response_finished[("story-session", "request-1")] = asyncio.Event()
        audit.response_finished[("story-session", "request-1")].set()
        await audit.record_response(
            {
                "sessionId": "story-session",
                "params": {"requestId": "request-1", "response": {"status": 200}},
            }
        )
        return calls, [json.loads(line) for line in handle.getvalue().splitlines()]

    calls, records = asyncio.run(run())

    assert calls == []
    assert records[-1]["response_body_status"] == "not_requested"


def test_browser_audit_marks_cdp_response_capture_failures_as_audit_errors(tmp_path: Path) -> None:
    async def run() -> list[dict[str, object]]:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")
        audit.target_sessions["story-target"] = "story-session"
        handle = io.StringIO()
        audit.handle = handle
        await audit.record_request(
            _request(
                "http://127.0.0.1:9999/api/interview-story-proposals",
                {"idempotency_key": "story-ui-audit-key-0001"},
            )
        )

        async def disconnected(*_args, **_kwargs):
            raise ConnectionError("CDP disconnected")

        audit.send = disconnected  # type: ignore[method-assign]
        audit.response_finished[("story-session", "request-1")] = asyncio.Event()
        audit.response_finished[("story-session", "request-1")].set()
        await audit.record_response(
            {
                "sessionId": "story-session",
                "params": {"requestId": "request-1", "response": {"status": 201}},
            }
        )
        assert isinstance(audit.reader_error, RuntimeError)
        return [json.loads(line) for line in handle.getvalue().splitlines()]

    records = asyncio.run(run())

    assert records[-1]["response_body_status"] == "unavailable"


def test_browser_audit_marks_unexpected_response_task_errors_as_audit_errors(tmp_path: Path) -> None:
    async def run() -> None:
        audit = BrowserAudit(_WebSocket(), tmp_path / "audit.jsonl", tmp_path / "stop")

        async def unexpected() -> None:
            raise ValueError("response task failed")

        task = asyncio.create_task(unexpected())
        audit.response_tasks.add(task)
        task.add_done_callback(audit.finish_response_task)
        with suppress(ValueError):
            await task
        await asyncio.sleep(0)
        assert isinstance(audit.reader_error, RuntimeError)

    asyncio.run(run())


def test_browser_audit_requires_the_dedicated_target_to_finish_network_enable(tmp_path: Path) -> None:
    async def run() -> None:
        websocket = _ScriptedWebSocket(attach_target="unowned-target")
        audit = BrowserAudit(websocket, tmp_path / "audit.jsonl", tmp_path / "stop")
        with pytest.raises(RuntimeError, match="dedicated browser target did not complete Network.enable"):
            await asyncio.wait_for(
                audit.run("http://127.0.0.1:9999", tmp_path / "ready", 0.05),
                timeout=1,
            )

    asyncio.run(run())


def test_browser_audit_fails_closed_when_network_enable_is_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        websocket = _ScriptedWebSocket(reject_network=True)
        audit = BrowserAudit(websocket, tmp_path / "audit.jsonl", tmp_path / "stop")
        with pytest.raises(RuntimeError, match="Network.enable"):
            await asyncio.wait_for(
                audit.run("http://127.0.0.1:9999", tmp_path / "ready", 0.5),
                timeout=1,
            )

    asyncio.run(run())


def test_browser_audit_ready_file_binds_the_dedicated_target_and_session(tmp_path: Path) -> None:
    async def run() -> dict[str, object]:
        websocket = _ScriptedWebSocket()
        ready = tmp_path / "browser-network.ready"
        stop = tmp_path / "browser-network.stop"
        stop.touch()
        audit = BrowserAudit(websocket, tmp_path / "audit.jsonl", stop)
        await asyncio.wait_for(
            audit.run("http://127.0.0.1:9999", ready, 0.5),
            timeout=1,
        )
        return json.loads(ready.read_text(encoding="utf-8"))

    ready = asyncio.run(run())

    assert ready == {"target_id": "story-target", "session_id": "story-session"}


def test_story_browser_harness_requires_story_flow_records_from_its_dedicated_target(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    records = _complete_story_audit_records(base_url)
    audit_path = tmp_path / "dedicated-target-audit.jsonl"
    _write_audit(audit_path, records)

    accepted = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    records[-1]["target_id"] = "unrelated-target"
    _write_audit(audit_path, records)
    rejected = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert rejected.returncode != 0
    assert "dedicated CDP target" in (rejected.stdout + rejected.stderr)

    records = _complete_story_audit_records(base_url)
    records[5]["method"] = "GET"
    _write_audit(audit_path, records)
    wrong_method = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert wrong_method.returncode != 0
    assert "ready ui Story proposal" in (wrong_method.stdout + wrong_method.stderr)


def test_story_browser_harness_requires_explicit_active_provider_configuration(tmp_path: Path) -> None:
    inactive_config = tmp_path / "inactive-provider-config.json"
    inactive_config.write_text(
        json.dumps({"providers": [{"id": "unselected", "enabled": True, "base_url": "https://provider.example"}]}),
        encoding="utf-8",
    )
    rejected = _run_harness("-ValidateProviderConfig", "-ProviderConfigPath", str(inactive_config))
    assert rejected.returncode != 0
    assert "active_provider_id" in (rejected.stdout + rejected.stderr)

    selected_config = tmp_path / "selected-provider-config.json"
    selected_config.write_text(
        json.dumps(
            {
                "active_provider_id": "primary",
                "fallback_provider_ids": ["fallback"],
                "providers": [
                    {"id": "primary", "enabled": True, "base_url": "https://provider.example"},
                    {"id": "fallback", "enabled": True, "base_url": "https://fallback.example"},
                    {"id": "unrelated", "enabled": True, "base_url": "https://unrelated.example"},
                ],
            }
        ),
        encoding="utf-8",
    )
    accepted = _run_harness("-ValidateProviderConfig", "-ProviderConfigPath", str(selected_config))
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def test_story_browser_harness_treats_attempts_as_call_audit_not_connect_count(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    browser_audit = tmp_path / "browser-audit.jsonl"
    _write_audit(browser_audit, _complete_story_audit_records(base_url))
    provider_audit = tmp_path / "provider-audit.jsonl"
    provider_audit.write_text(
        "\n".join(
            json.dumps({"kind": "provider_proxy_connect", "scheme": "https", "host": "provider.example", "port": 443, "status": "connected", "observed_at_ns": timestamp})
            for timestamp in (1_005_500, 1_013_200, 1_013_800)
        ) + "\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "providers.json"
    allowlist.write_text(json.dumps([{"Tuple": "https://provider.example:443"}]), encoding="utf-8")
    attempt_audit = tmp_path / "attempts.json"
    attempt_audit.write_text(
        json.dumps(
            [
                {"id": 11, "entrypoint": "ui", "attempt_status": "confirmed", "repair_count": 0, "confirmed_story_id": 101, "confirmed_story_version_id": 201},
                {"id": 12, "entrypoint": "pilot", "attempt_status": "confirmed", "repair_count": 1, "confirmed_story_id": 102, "confirmed_story_version_id": 202},
            ]
        ),
        encoding="utf-8",
    )

    accepted = _run_harness(
        "-ValidateProviderEgress", "-ProviderAuditPath", str(provider_audit), "-ProviderAllowlistPath", str(allowlist),
        "-BrowserAuditPath", str(browser_audit), "-StoryAttemptAuditPath", str(attempt_audit), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    # CONNECT records are transport tunnels, not model calls. A single model
    # call may reconnect inside the allowlisted request window, so changing the
    # persisted repair count must not make an otherwise contained audit fail.
    attempts_without_repair = json.loads(attempt_audit.read_text(encoding="utf-8"))
    attempts_without_repair[1]["repair_count"] = 0
    attempt_audit.write_text(json.dumps(attempts_without_repair), encoding="utf-8")
    reconnected = _run_harness(
        "-ValidateProviderEgress", "-ProviderAuditPath", str(provider_audit), "-ProviderAllowlistPath", str(allowlist),
        "-BrowserAuditPath", str(browser_audit), "-StoryAttemptAuditPath", str(attempt_audit), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert reconnected.returncode == 0, reconnected.stdout + reconnected.stderr


def test_story_browser_harness_requires_distinct_confirmed_stories_for_ui_and_pilot(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    audit_path = tmp_path / "persisted-attempt-audit.jsonl"
    _write_audit(audit_path, _complete_story_audit_records(base_url))
    attempt_audit = tmp_path / "attempts.json"
    attempt_audit.write_text(
        json.dumps(
            [
                {"id": 11, "entrypoint": "ui", "attempt_status": "confirmed", "repair_count": 0, "confirmed_story_id": 101, "confirmed_story_version_id": 201},
                {"id": 12, "entrypoint": "pilot", "attempt_status": "confirmed", "repair_count": 0, "confirmed_story_id": 101, "confirmed_story_version_id": 201},
            ]
        ),
        encoding="utf-8",
    )
    rejected = _run_harness(
        "-ValidateAttemptPersistence", "-BrowserAuditPath", str(audit_path), "-StoryAttemptAuditPath", str(attempt_audit),
        "-ExpectedBaseUrl", base_url, "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert rejected.returncode != 0
    assert "distinct confirmed Story" in (rejected.stdout + rejected.stderr)


def test_story_browser_harness_validates_each_entrypoint_sequence_and_auditor_exit(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    ui_context = {"entrypoint": "ui", "idempotency_key_sha256": "ui-key"}
    pilot_context = {"entrypoint": "pilot", "idempotency_key_sha256": "pilot-key"}
    audit_records = [
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 11},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/11/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 101, "response_story_version_id": 201},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/101", "response_status": 200, "response_body_status": "captured", "response_story_id": 101, "response_story_current_version_id": 201},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 12},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/12/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 102, "response_story_version_id": 202},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/102", "response_status": 200, "response_body_status": "captured", "response_story_id": 102, "response_story_current_version_id": 202},
    ]
    for index, record in enumerate(audit_records, 1):
        record["observed_at_ns"] = 1_000_000 + index * 1_000
    audit_path = tmp_path / "story-audit.jsonl"
    _write_audit(audit_path, audit_records)

    success = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert success.returncode == 0, success.stderr

    missing_pilot_source = [
        record for record in audit_records
        if not str(record.get("url", "")).startswith(f"{base_url}/api/interview-story-sources?")
    ]
    _write_audit(audit_path, missing_pilot_source)
    failure = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert failure.returncode != 0
    assert "source picker" in (failure.stdout + failure.stderr)

    failed_source_response = [dict(record) for record in audit_records]
    pilot_source_response = next(
        record for record in failed_source_response
        if record["kind"] == "browser_response" and record["url"].startswith(f"{base_url}/api/interview-story-sources?")
    )
    pilot_source_response["response_status"] = 500
    _write_audit(audit_path, failed_source_response)
    failed_read = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert failed_read.returncode != 0
    assert "source picker" in (failed_read.stdout + failed_read.stderr)

    auditor_failure = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url, "-AuditorExitCode", "7"
    )
    assert auditor_failure.returncode != 0
    assert "Browser auditor failed with exit code 7" in (auditor_failure.stdout + auditor_failure.stderr)

    chat_write = [*audit_records, {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/chat"}]
    _write_audit(audit_path, chat_write)
    chat_failure = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert chat_failure.returncode != 0
    assert "chat writes" in (chat_failure.stdout + chat_failure.stderr)

    wrong_history_version = [dict(record) for record in audit_records]
    history_response = next(
        record for record in wrong_history_version
        if record["kind"] == "browser_response" and record["url"] == f"{base_url}/api/interview-stories/101"
    )
    history_response["response_story_current_version_id"] = 999
    _write_audit(audit_path, wrong_history_version)
    mismatch = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert mismatch.returncode != 0
    assert "confirmed Story version" in (mismatch.stdout + mismatch.stderr)


def test_story_browser_harness_does_not_require_a_redundant_library_read_before_pilot(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    records = _complete_story_audit_records(base_url)
    records = [
        record for record in records
        if record.get("url") != f"{base_url}/api/interview-stories?status=active&query="
        or record["observed_at_ns"] < 1_009_000
    ]
    for index, record in enumerate(records, 1):
        record["observed_at_ns"] = 1_000_000 + index * 1_000
    audit_path = tmp_path / "story-pilot-source-only-audit.jsonl"
    _write_audit(audit_path, records)

    accepted = _run_harness(
        "-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def test_story_browser_harness_allows_one_same_key_provider_retry_but_rejects_semantic_replay(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    ui_context = {
        "entrypoint": "ui",
        "idempotency_key_sha256": "ui-key",
        "payload_sha256": "ui-payload",
    }
    pilot_context = {
        "entrypoint": "pilot",
        "idempotency_key_sha256": "pilot-key",
        "payload_sha256": "pilot-payload",
    }
    records = [
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context, "response_status": 502, "response_body_status": "captured", "response_error_code": "story_provider_error", "response_proposal_id": 11},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals", "request_context": ui_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 11},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/11/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 101, "response_story_version_id": 201},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/101", "response_status": 200, "response_body_status": "captured", "response_story_id": 101, "response_story_current_version_id": 201},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query="},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories?status=active&query=", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4"},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-story-sources?review_note_id=4", "response_status": 200, "response_body_status": "captured"},
        {"kind": "browser_request", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/pilot/interview-story-proposals", "request_context": pilot_context, "response_status": 201, "response_body_status": "captured", "response_proposal_id": 12},
        {"kind": "browser_response", "method": "POST", "url": f"{base_url}/api/interview-story-proposals/12/confirm", "response_status": 201, "response_body_status": "captured", "response_story_id": 102, "response_story_version_id": 202},
        {"kind": "browser_response", "method": "GET", "url": f"{base_url}/api/interview-stories/102", "response_status": 200, "response_body_status": "captured", "response_story_id": 102, "response_story_current_version_id": 202},
    ]
    for index, record in enumerate(records, 1):
        record["observed_at_ns"] = 1_000_000 + index * 1_000
    audit_path = tmp_path / "story-retry-audit.jsonl"
    _write_audit(audit_path, records)

    provider_retry = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert provider_retry.returncode == 0, provider_retry.stdout + provider_retry.stderr

    semantic_replay = [dict(record) for record in records]
    semantic_replay[5]["response_error_code"] = "story_unverifiable"
    _write_audit(audit_path, semantic_replay)
    rejected = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert rejected.returncode != 0
    assert "story_provider_error" in (rejected.stdout + rejected.stderr)

    wrong_attempt = [dict(record) for record in records]
    wrong_attempt[5]["response_proposal_id"] = 99
    _write_audit(audit_path, wrong_attempt)
    mismatched_attempt = _run_harness("-ValidateAudit", "-AuditPath", str(audit_path), "-ExpectedBaseUrl", base_url)
    assert mismatched_attempt.returncode != 0
    assert "Attempt" in (mismatched_attempt.stdout + mismatched_attempt.stderr)


def test_story_browser_harness_attributes_provider_egress_to_a_proven_user_retry(tmp_path: Path) -> None:
    base_url = "http://127.0.0.1:9999"
    browser_audit = tmp_path / "browser-audit.jsonl"
    records = _complete_story_audit_records(base_url)
    ui_request = dict(records[4])
    ui_provider_error = dict(records[5])
    ui_provider_error.update({
        "response_status": 502,
        "response_error_code": "story_provider_error",
    })
    ui_ready = dict(records[5])
    records[5] = ui_provider_error
    records.insert(6, ui_request)
    records.insert(7, ui_ready)
    for index, record in enumerate(records, 1):
        record["observed_at_ns"] = 1_000_000 + index * 1_000
    _write_audit(browser_audit, records)

    provider_audit = tmp_path / "provider-audit.jsonl"
    provider_audit.write_text(
        "\n".join(
            json.dumps({
                "kind": "provider_proxy_connect",
                "scheme": "https",
                "host": "provider.example",
                "port": 443,
                "status": "connected",
                "observed_at_ns": timestamp,
            })
            for timestamp in (1_005_500, 1_007_500, 1_015_500)
        ) + "\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "providers.json"
    allowlist.write_text(json.dumps([{"Tuple": "https://provider.example:443"}]), encoding="utf-8")
    attempt_audit = tmp_path / "attempts.json"
    attempt_audit.write_text(
        json.dumps([
            {"id": 11, "entrypoint": "ui", "attempt_status": "confirmed", "repair_count": 0, "confirmed_story_id": 101, "confirmed_story_version_id": 201},
            {"id": 12, "entrypoint": "pilot", "attempt_status": "confirmed", "repair_count": 0, "confirmed_story_id": 102, "confirmed_story_version_id": 202},
        ]),
        encoding="utf-8",
    )

    accepted = _run_harness(
        "-ValidateProviderEgress", "-ProviderAuditPath", str(provider_audit), "-ProviderAllowlistPath", str(allowlist),
        "-BrowserAuditPath", str(browser_audit), "-StoryAttemptAuditPath", str(attempt_audit), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    semantic_retry = [dict(record) for record in records]
    semantic_retry[5]["response_error_code"] = "story_unverifiable"
    _write_audit(browser_audit, semantic_retry)
    rejected = _run_harness(
        "-ValidateProviderEgress", "-ProviderAuditPath", str(provider_audit), "-ProviderAllowlistPath", str(allowlist),
        "-BrowserAuditPath", str(browser_audit), "-StoryAttemptAuditPath", str(attempt_audit), "-ExpectedBaseUrl", base_url,
        "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
    )
    assert rejected.returncode != 0
    assert "story_provider_error" in (rejected.stdout + rejected.stderr)


def test_story_browser_harness_allows_transport_reconnects_but_rejects_uncontained_egress(tmp_path: Path) -> None:
    audit = tmp_path / "provider-audit.jsonl"
    browser_audit = tmp_path / "browser-audit.jsonl"
    attempt_audit = tmp_path / "attempt-audit.json"
    allowlist = tmp_path / "providers.json"
    allowlist.write_text(json.dumps([{"Tuple": "https://provider.example:443"}]), encoding="utf-8")
    base_url = "http://127.0.0.1:9999"
    _write_audit(browser_audit, _complete_story_audit_records(base_url))
    attempt_audit.write_text(
        json.dumps(
            [
                {"id": 11, "entrypoint": "ui", "attempt_status": "confirmed", "repair_count": 1, "confirmed_story_id": 101, "confirmed_story_version_id": 201},
                {"id": 12, "entrypoint": "pilot", "attempt_status": "confirmed", "repair_count": 1, "confirmed_story_id": 102, "confirmed_story_version_id": 202},
            ]
        ),
        encoding="utf-8",
    )
    normal_and_repaired = [
        {"kind": "provider_proxy_connect", "scheme": "https", "host": "provider.example", "port": 443, "status": "connected", "observed_at_ns": timestamp}
        for timestamp in (1_005_200, 1_005_800, 1_013_200, 1_013_800)
    ]
    audit.write_text("\n".join(json.dumps(record) for record in normal_and_repaired) + "\n", encoding="utf-8")

    def validate() -> subprocess.CompletedProcess[str]:
        return _run_harness(
            "-ValidateProviderEgress", "-ProviderAuditPath", str(audit), "-ProviderAllowlistPath", str(allowlist),
            "-BrowserAuditPath", str(browser_audit), "-StoryAttemptAuditPath", str(attempt_audit),
            "-ExpectedBaseUrl", base_url, "-ExpectedTargetId", "story-target", "-ExpectedSessionId", "story-session",
        )

    accepted = validate()
    assert accepted.returncode == 0, accepted.stderr

    # One CONNECT tunnel may carry both the initial request and the bounded
    # format-repair request through HTTP keep-alive. Persisted repair_count is
    # the call audit; the proxy proves the destination and transport window.
    reused_tunnels = [normal_and_repaired[0], normal_and_repaired[2]]
    audit.write_text("\n".join(json.dumps(record) for record in reused_tunnels) + "\n", encoding="utf-8")
    reused = validate()
    assert reused.returncode == 0, reused.stderr
    audit.write_text("\n".join(json.dumps(record) for record in normal_and_repaired) + "\n", encoding="utf-8")

    # DNS hostnames and schemes are case-insensitive. The browser-proxy audit
    # must not reject an otherwise allowlisted real Provider connection merely
    # because the configured URL and CONNECT host differ in case.
    allowlist.write_text(json.dumps([{"Tuple": "HTTPS://PROVIDER.EXAMPLE:443"}]), encoding="utf-8")
    case_insensitive = validate()
    assert case_insensitive.returncode == 0, case_insensitive.stderr
    allowlist.write_text(json.dumps([{"Tuple": "https://provider.example:443"}]), encoding="utf-8")

    # The local proxy may reject unrelated library telemetry before it can leave
    # the host. A rejected CONNECT is evidence of containment, not Provider
    # egress; an unknown *connected* endpoint remains a hard failure.
    rejected_foreign = [
        *normal_and_repaired,
        {"kind": "provider_proxy_connect", "scheme": "https", "host": "telemetry.example", "port": 443, "status": "rejected", "observed_at_ns": 1_010_000},
    ]
    audit.write_text("\n".join(json.dumps(record) for record in rejected_foreign) + "\n", encoding="utf-8")
    contained = validate()
    assert contained.returncode == 0, contained.stderr

    connected_foreign = [
        normal_and_repaired[0],
        normal_and_repaired[2],
        {"kind": "provider_proxy_connect", "scheme": "https", "host": "telemetry.example", "port": 443, "status": "connected", "observed_at_ns": 1_010_000},
    ]
    audit.write_text("\n".join(json.dumps(record) for record in connected_foreign) + "\n", encoding="utf-8")
    escaped = validate()
    assert escaped.returncode != 0
    assert "outside the configured candidate allowlist" in (escaped.stdout + escaped.stderr)

    audit.write_text("\n".join(json.dumps(record) for record in normal_and_repaired) + "\n", encoding="utf-8")

    audit.write_text("\n".join(json.dumps(record) for record in [*normal_and_repaired, normal_and_repaired[0]]) + "\n", encoding="utf-8")
    reconnected = validate()
    assert reconnected.returncode == 0, reconnected.stderr

    ui_overflow = [
        {"kind": "provider_proxy_connect", "scheme": "https", "host": "provider.example", "port": 443, "status": "connected", "observed_at_ns": timestamp}
        for timestamp in (1_005_100, 1_005_500, 1_005_900, 1_013_500)
    ]
    audit.write_text("\n".join(json.dumps(record) for record in ui_overflow) + "\n", encoding="utf-8")
    reconnected_ui = validate()
    assert reconnected_ui.returncode == 0, reconnected_ui.stderr

    outside_request_window = [
        *reused_tunnels,
        {"kind": "provider_proxy_connect", "scheme": "https", "host": "provider.example", "port": 443, "status": "connected", "observed_at_ns": 999_999},
    ]
    audit.write_text("\n".join(json.dumps(record) for record in outside_request_window) + "\n", encoding="utf-8")
    uncontained = validate()
    assert uncontained.returncode != 0
    output = "".join((uncontained.stdout + uncontained.stderr).split())
    assert "correlatedtoexactlyoneUIorPilotStoryrequest" in output


def test_story_browser_harness_records_a_single_viewport_screenshot_matrix(tmp_path: Path) -> None:
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    names = [
        "01-story-library.png", "02-source-picker.png", "03-source-preview.png", "04-generated-draft.png",
        "05-confirmation.png", "06-history.png", "07-source-changed.png", "08-pilot-entry.png",
        "09-pilot-source-choice.png", "10-pilot-history.png",
    ]
    for name in names:
        _write_gray_png(screenshots / name)
    manifest = tmp_path / "matrix.json"

    result = _run_harness(
        "-ValidateScreenshotMatrix", "-ScreenshotDirectory", str(screenshots), "-ScreenshotManifestPath", str(manifest)
    )

    assert result.returncode == 0, result.stderr
    matrix = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(matrix) == 10
    assert all(item["width"] == 1455 and item["height"] == 1200 and len(item["sha256"]) == 64 for item in matrix)


def test_story_browser_harness_starts_audited_chromium_before_honoring_completion_signal(tmp_path: Path) -> None:
    source_data = tmp_path / "configured-data"
    source_data.mkdir()
    (source_data / "config.json").write_text(
        json.dumps(
            {
                "active_provider_id": "browser-harness-stub",
                "providers": [
                    {
                        "id": "browser-harness-stub",
                        "enabled": True,
                        "base_url": "https://provider.example",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    completion_signal = tmp_path / "complete.signal"
    completion_signal.touch()
    session_state = tmp_path / "story-browser-session.json"
    environment = dict(os.environ)
    environment["OFFERPILOT_DATA"] = str(source_data)

    result = _run_harness(
        "-CompletionSignalPath", str(completion_signal), "-SessionStatePath", str(session_state), env=environment,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Dedicated browser target is ready" in output
    assert "Browser did not execute one UI and one Pilot Story proposal sequence" in output
    state = json.loads(session_state.read_text(encoding="utf-8"))
    assert state["base_url"].startswith("http://127.0.0.1:")
    assert state["cdp_url"].startswith("http://127.0.0.1:")
    assert not Path(state["temp_data_path"]).exists()


def _run_faulted_chromium_startup(
    tmp_path: Path,
    *fault_args: str,
    with_completion_signal: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, set[str], float]:
    source_data = tmp_path / "configured-data"
    source_data.mkdir()
    (source_data / "config.json").write_text(
        json.dumps(
            {
                "active_provider_id": "browser-harness-stub",
                "providers": [
                    {
                        "id": "browser-harness-stub",
                        "enabled": True,
                        "base_url": "https://provider.example",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    completion_signal = tmp_path / "complete.signal"
    if with_completion_signal:
        completion_signal.touch()
    session_state = tmp_path / "story-browser-session.json"
    cleanup_audit = tmp_path / "cleanup-audit.json"
    environment = dict(os.environ)
    environment["OFFERPILOT_DATA"] = str(source_data)
    before = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}

    started = time.monotonic()
    result = _run_harness(
        "-CompletionSignalPath",
        str(completion_signal),
        "-SessionStatePath",
        str(session_state),
        "-CleanupAuditPath",
        str(cleanup_audit),
        *fault_args,
        env=environment,
    )
    return result, session_state, cleanup_audit, before, time.monotonic() - started


def _chromium_attempt_records(output: str) -> list[tuple[int, str, int, str]]:
    return [
        (int(attempt), outcome, int(port), profile)
        for attempt, outcome, port, profile in re.findall(
            r"attempt=(\d+); outcome=(exited|timed_out); port=(\d+); profile=([^;\r\n]+)",
            output,
        )
    ]


def test_story_browser_harness_retries_an_exited_chromium_with_a_new_port_and_profile(tmp_path: Path) -> None:
    result, session_state, _cleanup_audit, before, _elapsed = _run_faulted_chromium_startup(
        tmp_path,
        "-ForceChromiumStartupExitFailuresForTest",
        "1",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Dedicated browser target is ready" in output
    assert "Browser did not execute one UI and one Pilot Story proposal sequence" in output
    assert "exit_code=73" in output
    assert "forced Chromium startup exit" in output
    attempts = _chromium_attempt_records(output)
    assert len(attempts) == 1
    assert attempts[0][0:2] == (1, "exited")
    assert "cleanup=complete" in output
    state = json.loads(session_state.read_text(encoding="utf-8"))
    assert state["chromium_startup_attempt"] == 2
    assert state["browser_profile"].endswith("browser-profile-attempt-2")
    assert int(state["cdp_url"].rsplit(":", 1)[1]) != attempts[0][2]
    assert state["browser_profile"] != attempts[0][3]
    assert not Path(state["temp_data_path"]).exists()
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before


def test_story_browser_harness_aggregates_three_exited_chromium_attempts(tmp_path: Path) -> None:
    result, session_state, cleanup_audit, before, _elapsed = _run_faulted_chromium_startup(
        tmp_path,
        "-ForceChromiumStartupExitFailuresForTest",
        "3",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Dedicated Chromium CDP endpoint did not become ready after three bounded attempts" in output
    assert "Dedicated browser target is ready" not in output
    attempts = _chromium_attempt_records(output)
    assert [attempt for attempt, _outcome, _port, _profile in attempts] == [1, 2, 3]
    assert all(outcome == "exited" for _attempt, outcome, _port, _profile in attempts)
    assert len({port for _attempt, _outcome, port, _profile in attempts}) == 3
    assert len({profile for _attempt, _outcome, _port, profile in attempts}) == 3
    assert output.count("exit_code=73") == 3
    assert output.count("cleanup=complete") == 3
    assert not session_state.exists()
    cleanup = json.loads(cleanup_audit.read_text(encoding="utf-8"))
    assert all(record["exited"] is True for record in cleanup["processes"])
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before


def test_story_browser_harness_retries_after_a_fixed_chromium_cdp_deadline(tmp_path: Path) -> None:
    result, session_state, _cleanup_audit, before, elapsed = _run_faulted_chromium_startup(
        tmp_path,
        "-ForceChromiumStartupTimeoutFailuresForTest",
        "1",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Dedicated browser target is ready" in output
    attempts = _chromium_attempt_records(output)
    assert len(attempts) == 1
    assert attempts[0][0:2] == (1, "timed_out")
    assert "deadline_seconds=20" in output
    assert "cleanup=complete" in output
    readiness_elapsed_match = re.search(r"readiness_elapsed_ms=(\d+)", output)
    assert readiness_elapsed_match is not None
    assert 18_000 <= int(readiness_elapsed_match.group(1)) <= 25_000
    assert elapsed >= 18
    state = json.loads(session_state.read_text(encoding="utf-8"))
    assert state["chromium_startup_attempt"] == 2
    assert int(state["cdp_url"].rsplit(":", 1)[1]) != attempts[0][2]
    assert state["browser_profile"] != attempts[0][3]
    assert not Path(state["temp_data_path"]).exists()
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before


def test_story_browser_harness_rejects_a_cdp_response_that_arrives_after_the_deadline(tmp_path: Path) -> None:
    result, session_state, _cleanup_audit, before, _elapsed = _run_faulted_chromium_startup(
        tmp_path,
        "-ForceChromiumStartupLateReadyFailuresForTest",
        "1",
        "-ChromiumStartupDeadlineMillisecondsForTest",
        "200",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Dedicated browser target is ready" in output
    attempts = _chromium_attempt_records(output)
    assert len(attempts) == 1
    assert attempts[0][0:2] == (1, "timed_out")
    assert "deadline_milliseconds=200" in output
    assert "forced Chromium late-ready CDP stub accepted request" in output
    elapsed_match = re.search(r"readiness_elapsed_ms=(\d+)", output)
    assert elapsed_match is not None
    assert 100 <= int(elapsed_match.group(1)) <= 1_500
    state = json.loads(session_state.read_text(encoding="utf-8"))
    assert state["chromium_startup_attempt"] == 2
    assert int(state["cdp_url"].rsplit(":", 1)[1]) != attempts[0][2]
    assert state["browser_profile"] != attempts[0][3]
    assert not Path(state["temp_data_path"]).exists()
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before


def test_story_browser_harness_fails_closed_when_failed_chromium_cleanup_is_uncertain(tmp_path: Path) -> None:
    result, session_state, cleanup_audit, before, _elapsed = _run_faulted_chromium_startup(
        tmp_path,
        "-ForceChromiumStartupExitFailuresForTest",
        "1",
        "-ForceChromiumStartupCleanupFailureForTest",
    )

    output = result.stdout + result.stderr
    normalized_output = " ".join(output.split())
    assert result.returncode != 0
    assert "Chromium startup attempt 1 cleanup failed; refusing another launch" in normalized_output
    assert "Forced Chromium startup cleanup failure" in normalized_output
    attempts = _chromium_attempt_records(output)
    assert len(attempts) == 1
    assert attempts[0][0:2] == (1, "exited")
    assert "exit_code=73" in output
    assert "diagnostic=forced Chromium startup exit" in output
    assert "cleanup=failed" in output
    assert "attempt=2" not in output
    assert "Dedicated browser target is ready" not in output
    assert not session_state.exists()
    cleanup = json.loads(cleanup_audit.read_text(encoding="utf-8"))
    assert all(record["exited"] is True for record in cleanup["processes"])
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before


def test_story_browser_harness_keeps_a_startup_auditor_handle_for_outer_cleanup() -> None:
    source = _HARNESS_PATH.read_text(encoding="utf-8")

    assert "function Start-BrowserAuditor([string]$cdpUrl, [string]$expectedUrl, [ref]$trackedAuditor)" in source
    assert "$trackedAuditor.Value = $process" in source
    assert "Start-BrowserAuditor \"http://127.0.0.1:$cdpPort\" $baseUrl ([ref]$auditor)" in source


def test_story_browser_harness_cleans_all_local_resources_when_auditor_startup_fails(tmp_path: Path) -> None:
    source_data = tmp_path / "configured-data"
    source_data.mkdir()
    (source_data / "config.json").write_text(
        json.dumps(
            {
                "active_provider_id": "browser-harness-stub",
                "providers": [
                    {
                        "id": "browser-harness-stub",
                        "enabled": True,
                        "base_url": "https://provider.example",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    environment = dict(os.environ)
    environment["OFFERPILOT_DATA"] = str(source_data)
    cleanup_audit = tmp_path / "cleanup-audit.json"

    result = _run_harness(
        "-ForceAuditorStartupCleanupFailureForTest",
        "-CleanupAuditPath",
        str(cleanup_audit),
        env=environment,
    )

    assert result.returncode != 0
    assert "Forced browser auditor startup cleanup failure" in (result.stdout + result.stderr)
    cleanup = json.loads(cleanup_audit.read_text(encoding="utf-8"))
    assert {record["label"] for record in cleanup["processes"]} == {
        "browser auditor",
        "dedicated browser",
        "isolated service",
        "provider proxy",
    }
    assert all(record["exited"] is True for record in cleanup["processes"])
    after = {path.name for path in Path(os.environ["TEMP"]).glob("offerpilot-interview-story-*")}
    assert after == before
