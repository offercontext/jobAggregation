from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from offerpilot.ai.types import Assistant, Message, ToolCall
from offerpilot.ai.write_operations import WriteOperationCoordinator
from offerpilot.api import create_app
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import ChatMessage, WriteOperation
from offerpilot.pilot_runtime.composition import (
    _ContinuationModelResolver,
    _PolicyCatalogResolver,
    _SourceAdapter,
)
from offerpilot.pilot_runtime.contracts import ConfirmationRequest
from offerpilot.pilot_runtime.service import (
    PilotRuntime,
    ResolvedModel,
    _ContinuationActivationRequest,
)
from offerpilot.repositories.chat import (
    ChatRepository,
    ConversationScopeMutationSnapshot,
)


class _CutoverModel:
    def __init__(self, origin: ToolCall, *, final_reply: str = "cutover complete") -> None:
        self.origin = origin
        self.final_reply = final_reply
        self.calls: list[tuple[list[Message], list[object]]] = []

    def complete(self, messages: list[Message], tools: list[object]) -> Assistant:
        self.calls.append((messages, tools))
        if len(self.calls) == 1:
            return Assistant(tool_calls=[self.origin])
        if len(self.calls) == 2:
            return Assistant(content=self.final_reply)
        raise AssertionError("unexpected Provider call")


class _StreamingCutoverModel(_CutoverModel):
    def stream_complete(
        self,
        messages: list[Message],
        tools: list[object],
        on_delta: object,
    ) -> Assistant:
        assert callable(on_delta)
        self.calls.append((messages, tools))
        if len(self.calls) == 1:
            return Assistant(tool_calls=[self.origin])
        if len(self.calls) != 2:
            raise AssertionError("unexpected Provider call")
        on_delta("fresh ")
        on_delta("continuation")
        return Assistant(content="fresh continuation")


class _RepeatingReadModel:
    def __init__(self, origin: ToolCall) -> None:
        self.origin = origin
        self.calls = 0

    def complete(self, _messages: list[Message], _tools: list[object]) -> Assistant:
        self.calls += 1
        if self.calls == 1:
            return Assistant(tool_calls=[self.origin])
        return Assistant(
            tool_calls=[
                ToolCall(
                    id=f"post-terminal-read-{self.calls}",
                    name="list_applications",
                    args="{}",
                )
            ]
        )


def _application(client: TestClient, company: str, *, status: str = "applied") -> dict[str, Any]:
    response = client.post(
        "/api/applications",
        json={
            "company_name": company,
            "position_name": "安全边界工程师",
            "status": status,
        },
    )
    assert response.status_code == 201
    return response.json()


def _propose(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/chat",
        json={"message": "执行需要确认的写入", "conversation_id": 0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "confirmation_required"
    return body


def _confirm(
    client: TestClient,
    endpoint: str,
    pending: dict[str, Any],
    *,
    edited_args: dict[str, Any] | None = None,
) -> object:
    payload: dict[str, Any] = {
        "conversation_id": pending["conversation_id"],
        "approved": True,
        "confirmation_token": pending["pending_action"]["confirmation_token"],
    }
    if edited_args is not None:
        payload["edited_args"] = edited_args
    return client.post(endpoint, json=payload)


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
def test_edited_confirmation_projects_effective_call_but_preserves_proposal(tmp_path, endpoint):
    origin = ToolCall("edited-origin", "update_application_status", '{"id":1,"status":"interview"}')
    model = _CutoverModel(origin)
    app = create_app(data_dir=tmp_path, chat_model=model)
    with TestClient(app) as client:
        application = _application(client, "筱哲的面试示例")
        assert application["id"] == 1
        pending = _propose(client)
        response = _confirm(client, endpoint, pending, edited_args={"status": "offer"})
        assert response.status_code == 200
        assert len(model.calls) == 2
        calls = [call for message in model.calls[1][0] for call in message.tool_calls if call.id == origin.id]
        assert len(calls) == 1
        assert json.loads(calls[0].args) == {"id": 1, "status": "offer"}
        notices = [message for message in model.calls[1][0]
                   if message.role == "system" and "用户主动修改并批准" in message.content]
        assert len(notices) == 1
        assert origin.id in notices[0].content
        assert "不要自行恢复原提案" in notices[0].content
        assert "以对应工具结果为准" in notices[0].content
        assert "不要再拿旧请求做差异核对" in notices[0].content
        assert pending['pending_action']['confirmation_token'] not in notices[0].content
        assert not any("用户主动修改并批准" in message.content for message in model.calls[0][0])
        assert client.get('/api/applications/1').json()['status'] == 'offer'
        with session_factory_for_data_dir(tmp_path)() as session:
            stored = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == pending['conversation_id'], ChatMessage.role == 'assistant')).all()
            proposals = [json.loads(message.tool_calls) for message in stored if message.tool_calls]
            assert any('interview' in json.dumps(calls) for calls in proposals)
            operations = session.scalars(select(WriteOperation)).all()
            assert len(operations) == 1
            assert operations[0].status == 'committed'
            all_messages = session.scalars(select(ChatMessage)).all()
            assert not any("用户主动修改并批准" in message.content for message in all_messages)


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
def test_confirmation_explicit_null_edited_args_remains_422(
    tmp_path: Any,
    endpoint: str,
) -> None:
    client = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False)

    response = client.post(
        endpoint,
        json={
            "conversation_id": 1,
            "approved": True,
            "confirmation_token": "0" * 64,
            "edited_args": None,
        },
    )

    assert response.status_code == 422
    assert "edited_args must be a JSON object" in response.text


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
@pytest.mark.parametrize(
    ("field", "proposed", "approved"),
    [("signing_bonus", 10000, 8000), ("perks", "每周远程办公两天", "每周远程办公一天")],
)
def test_offer_user_edit_notice_is_transient_and_replay_does_not_reexecute(
    tmp_path, endpoint, field, proposed, approved,
):
    origin = ToolCall("offer-user-edit", "update_offer", json.dumps({"id": 1, field: proposed}))
    model = _CutoverModel(origin)
    with TestClient(create_app(data_dir=tmp_path, chat_model=model)) as client:
        application = _application(client, "筱哲的远帆科技")
        offer = client.post("/api/offers", json={
            "application_id": application["id"], "company_name": "远帆科技",
            "position_name": "AI工程师", "base_monthly": 24000, "months_per_year": 16,
        })
        assert offer.status_code == 201
        assert offer.json()["id"] == 1
        pending = _propose(client)
        response = _confirm(client, endpoint, pending, edited_args={field: approved})
        assert response.status_code == 200
        assert len(model.calls) == 2
        messages = model.calls[1][0]
        notices = [m for m in messages if m.role == "system" and "用户主动修改并批准" in m.content]
        assert len(notices) == 1
        assert str(proposed) not in notices[0].content
        assert str(approved) not in notices[0].content
        assert "不构成新的写入授权" in notices[0].content
        effective = [c for m in messages for c in m.tool_calls if c.id == origin.id]
        assert len(effective) == 1
        assert json.loads(effective[0].args)[field] == approved
        assert client.get("/api/offers/1").json()[field] == approved
        replay = client.post(endpoint, json={
            "conversation_id": pending["conversation_id"],
            "operation_id": pending["pending_action"]["operation_id"],
            "confirmation_token": pending["pending_action"]["confirmation_token"],
            "approved": True, "edited_args": {field: approved},
        })
        assert replay.status_code == 200
        assert len(model.calls) == 2
        with session_factory_for_data_dir(tmp_path)() as session:
            operations = list(session.scalars(select(WriteOperation)))
            assert len(operations) == 1
            assert operations[0].status == "committed"
            stored = list(session.scalars(select(ChatMessage)))
            assert not any("用户主动修改并批准" in m.content for m in stored)
            persisted = [c for m in stored if m.tool_calls for c in json.loads(m.tool_calls)]
            original = next(c for c in persisted if c["id"] == origin.id)
            assert original["args"][field] == proposed


@pytest.mark.parametrize("edits", [None, {}, {"status": "interview"}])
def test_unchanged_confirmation_does_not_claim_user_changed_values(tmp_path, edits):
    model = _CutoverModel(ToolCall("unchanged", "update_application_status", '{"id":1,"status":"interview"}'))
    with TestClient(create_app(data_dir=tmp_path, chat_model=model)) as client:
        _application(client, "筱哲原样确认")
        pending = _propose(client)
        response = _confirm(client, "/api/chat/confirm", pending, edited_args=edits)
        assert response.status_code == 200
        assert len(model.calls) == 2
        assert not any("用户主动修改并批准" in m.content for m in model.calls[1][0])


def test_edit_notice_is_mandatory_budgeted_without_reexecuting_committed_write(tmp_path, monkeypatch):
    from offerpilot.ai.tool_runtime.contracts import materialize_provider_payloads
    from offerpilot.context_projector import budget
    from offerpilot.context_projector.contracts import ProjectionError, canonical_json
    from offerpilot.context_projector.projector import ModelSurfaceProjector

    original_project = ModelSurfaceProjector.project
    blocked = []

    def project(self, request):
        notices = [m for c in request.contributors if c.name == "active_control"
                   for m in c.messages if "用户主动修改并批准" in m.content]
        if not notices:
            return original_project(self, request)
        assert len(notices) == 1
        without = replace(request, contributors=tuple(
            replace(c, messages=tuple(m for m in c.messages if m not in notices))
            if c.name == "active_control" else c for c in request.contributors
        ))
        mandatory = tuple(m for c in without.contributors
                          if c.name in {"static_policy", "active_control", "current_request"}
                          for m in c.messages)
        cap = len(budget.canonical_messages(mandatory)) + len(canonical_json(
            materialize_provider_payloads(request.selection.provider_contracts)
        )) + 64
        with monkeypatch.context() as context:
            context.setattr(budget, "PRODUCT_INPUT_CAP", cap)
            original_project(self, without)  # Without the notice, the same input fits.
            with pytest.raises(ProjectionError) as failure:
                original_project(self, request)
            assert failure.value.code == "mandatory_surface_over_budget"
        blocked.append(True)
        raise failure.value

    model = _CutoverModel(ToolCall("budget-edit", "update_application_status", '{"id":1,"status":"interview"}'))
    monkeypatch.setattr(ModelSurfaceProjector, "project", project)
    with TestClient(create_app(data_dir=tmp_path, chat_model=model)) as client:
        _application(client, "筱哲预算验证")
        pending = _propose(client)
        response = _confirm(client, "/api/chat/confirm", pending, edited_args={"status": "offer"})
        assert response.status_code == 200
        assert blocked == [True]
        assert len(model.calls) == 1  # No continuation Provider call, no projection fallback.
        assert client.get("/api/applications/1").json()["status"] == "offer"
        with session_factory_for_data_dir(tmp_path)() as session:
            operations = list(session.scalars(select(WriteOperation)))
            assert len(operations) == 1 and operations[0].status == "committed"


def _sse_events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in raw.strip().split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        event_name = ""
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        assert event_name
        events.append((event_name, json.loads("\n".join(data_lines))))
    return events


def test_streaming_continuation_negotiates_delta_from_fresh_segment_model(
    tmp_path: Any,
) -> None:
    origin = ToolCall(
        id="streaming-cutover-origin",
        name="update_application_status",
        args=json.dumps({"id": 1, "status": "interview"}),
    )
    model = _StreamingCutoverModel(origin)
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application = _application(client, "Streaming Cutover")
    assert application["id"] == 1
    pending = _propose(client)

    response = _confirm(client, "/api/chat/confirm/stream", pending)

    assert getattr(response, "status_code") == 200
    events = _sse_events(getattr(response, "text"))
    event_names = [name for name, _payload in events]
    assert event_names[0] == "meta"
    assert events[0][1]["data"]["supports_delta"] is True
    assert [payload["data"] for name, payload in events if name == "assistant_delta"] == [
        {"delta": "fresh "},
        {"delta": "continuation"},
    ]
    assert len(model.calls) == 2


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
def test_real_driver_activates_one_fresh_post_terminal_segment(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """The real Driver crosses Approval -> fresh Segment exactly once.

    This test deliberately exercises the production composition root.  It also
    changes the canonical Conversation generation/scope only after the origin
    terminal owns delivery, proving that Source, policy, and Provider all see
    the post-terminal snapshot rather than a request/pre-approval capture.
    """

    origin = ToolCall(
        id="cutover-origin",
        name="update_application_status",
        args=json.dumps({"id": 1, "status": "interview"}),
    )
    model = _CutoverModel(origin)
    app = create_app(data_dir=tmp_path, chat_model=model)
    client = TestClient(app)
    first = _application(client, "Origin Company")
    fresh_scope = _application(client, "Fresh Scope Company")
    assert first["id"] == 1
    pending = _propose(client)
    conversation_id = int(pending["conversation_id"])
    operation_id = str(pending["pending_action"]["operation_id"])
    confirmation_token = str(pending["pending_action"]["confirmation_token"])
    sessions = session_factory_for_data_dir(tmp_path)
    chat = ChatRepository(sessions)
    before = chat.get_conversation(conversation_id)
    assert before is not None

    stages: Counter[str] = Counter()
    stage_requests: list[object] = []
    terminal_snapshots: list[tuple[object, ...]] = []
    source_scopes: list[tuple[object, ...]] = []
    loaded_sources: list[object] = []
    source_errors: list[str] = []
    original_load_conversation = PilotRuntime._load_confirmation_conversation
    original_load_source = _SourceAdapter.load
    original_policy = _PolicyCatalogResolver.resolve
    original_model_resolve = _ContinuationModelResolver.resolve
    mutated = False

    def load_fresh_conversation(self: PilotRuntime, candidate_id: int) -> object | None:
        nonlocal mutated
        if not mutated:
            mutated = True
            changed = chat.patch_conversation_with_scope(
                candidate_id,
                {},
                ConversationScopeMutationSnapshot(
                    context_type="application",
                    context_ref=str(fresh_scope["id"]),
                    mode="general",
                ),
                expected_scope_revision=0,
            )
            assert changed is not None
        return original_load_conversation(self, candidate_id)

    def load_source(
        self: _SourceAdapter,
        conversation: object,
        request: object,
        *,
        attachments: tuple[object, ...] = (),
        page_context: object | None = None,
        pending_tool_call_id: str = "",
    ) -> object:
        if type(request) is _ContinuationActivationRequest:
            stages["source"] += 1
            stage_requests.append(request)
            with sessions() as session:
                operation = session.get(WriteOperation, operation_id)
                assert operation is not None
                terminal_snapshots.append(
                    (
                        operation.status,
                        operation.delivery_status,
                        operation.delivery_generation,
                        operation.delivery_owner_token_fingerprint,
                        operation.delivery_lease_expires_at,
                    )
                )
            source_scopes.append(
                (
                    getattr(conversation, "context_type"),
                    getattr(conversation, "context_ref"),
                    getattr(conversation, "scope_revision"),
                    getattr(conversation, "updated_at"),
                )
            )
        try:
            value = original_load_source(
                self,
                conversation,
                request,  # type: ignore[arg-type]
                attachments=attachments,
                page_context=page_context,
                pending_tool_call_id=pending_tool_call_id,
            )
        except BaseException as exc:
            source_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        if type(request) is _ContinuationActivationRequest:
            loaded_sources.append(value)
        return value

    def resolve_policy(
        self: _PolicyCatalogResolver,
        request: object,
        conversation: object,
        source: object,
        segment: object,
    ) -> object:
        if type(request) is _ContinuationActivationRequest:
            stages["policy"] += 1
            stage_requests.append(request)
            assert getattr(source, "scope_revision") == 1
        return original_policy(self, request, conversation, source, segment)  # type: ignore[arg-type]

    def resolve_model(
        self: _ContinuationModelResolver,
        request: object,
        conversation: object,
        policy: object | None = None,
    ) -> ResolvedModel:
        if type(request) is _ContinuationActivationRequest:
            stages["model_resolve"] += 1
            stage_requests.append(request)
            assert getattr(conversation, "scope_revision") == 1
        return original_model_resolve(self, request, conversation, policy)  # type: ignore[arg-type]

    monkeypatch.setattr(PilotRuntime, "_load_confirmation_conversation", load_fresh_conversation)
    monkeypatch.setattr(_SourceAdapter, "load", load_source)
    monkeypatch.setattr(_PolicyCatalogResolver, "resolve", resolve_policy)
    monkeypatch.setattr(_ContinuationModelResolver, "resolve", resolve_model)

    phase_counts: Counter[str] = Counter()
    runtime = app.state.pilot_runtime
    runtime._dependencies = replace(
        runtime._dependencies,
        phase_sink=lambda name: phase_counts.update([name]),
    )
    response = _confirm(client, endpoint, pending)

    assert getattr(response, "status_code") == 200
    if endpoint.endswith("/stream"):
        stream_events = _sse_events(getattr(response, "text"))
        assert stream_events[0][0] == "meta"
        assert stream_events[0][1]["data"]["supports_delta"] is False
        assert all(name != "assistant_delta" for name, _payload in stream_events)
    assert source_errors == []
    assert stages == {"source": 1, "policy": 1, "model_resolve": 1}
    assert phase_counts["validate"] == 1
    assert len(terminal_snapshots) == 1
    assert terminal_snapshots[0][:3] == ("committed", "pending", 1)
    assert isinstance(terminal_snapshots[0][3], str) and terminal_snapshots[0][3]
    assert isinstance(terminal_snapshots[0][4], int)
    assert source_scopes[0][:3] == (
        "application",
        str(fresh_scope["id"]),
        1,
    )
    assert source_scopes[0][3] != before.updated_at
    assert len(model.calls) == 2

    provider_messages = model.calls[1][0]
    origin_results = [
        message
        for message in provider_messages
        if message.role == "tool" and message.tool_call_id == origin.id
    ]
    assert len(origin_results) == 1
    proposals = [
        call
        for message in provider_messages
        if message.role == "assistant"
        for call in message.tool_calls
        if call.id == origin.id
    ]
    assert len(proposals) == 1
    assert json.loads(proposals[0].args) == {"id": first["id"], "status": "interview"}
    assert any("Fresh Scope Company" in message.content for message in provider_messages)
    assert confirmation_token not in repr(provider_messages)
    assert stage_requests
    assert all(type(request) is _ContinuationActivationRequest for request in stage_requests)
    assert all(not isinstance(request, ConfirmationRequest) for request in stage_requests)
    assert all(not hasattr(request, "confirmation_token") for request in stage_requests)
    assert all(not hasattr(request, "edited_args") for request in stage_requests)
    assert client.get(f"/api/applications/{first['id']}").json()["status"] == "interview"


def _corrupt_origin_proposal(
    sessions: object,
    conversation_id: int,
    tool_call_id: str,
    corruption: str,
) -> None:
    with sessions() as session:  # type: ignore[operator]
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .where(ChatMessage.role == "assistant")
                .order_by(ChatMessage.id.asc())
            )
        )
        proposal = next(message for message in messages if tool_call_id in message.tool_calls)
        if corruption == "missing":
            session.execute(delete(ChatMessage).where(ChatMessage.id == proposal.id))
        elif corruption == "duplicate":
            session.add(
                ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=proposal.content,
                    tool_calls=proposal.tool_calls,
                    provider_blocks=proposal.provider_blocks,
                )
            )
        else:
            raw_calls = json.loads(proposal.tool_calls)
            assert len(raw_calls) == 1
            if corruption == "conflicting-name":
                raw_calls[0]["name"] = "create_application"
            elif corruption == "conflicting-args":
                raw_calls[0]["args"] = json.dumps({"id": 999, "status": "offer"})
            else:
                raise AssertionError(f"unknown corruption: {corruption}")
            session.execute(
                update(ChatMessage)
                .where(ChatMessage.id == proposal.id)
                .values(tool_calls=json.dumps(raw_calls))
            )
        session.commit()


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
@pytest.mark.parametrize(
    "corruption",
    ("missing", "duplicate", "conflicting-name", "conflicting-args"),
)
def test_origin_proposal_corruption_fails_closed_without_provider_or_executor_rerun(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    corruption: str,
) -> None:
    origin = ToolCall(
        id="corrupt-origin",
        name="update_application_status",
        args=json.dumps({"id": 1, "status": "offer"}),
    )
    model = _CutoverModel(origin)
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    application = _application(client, "Corruption Target")
    assert application["id"] == 1
    pending = _propose(client)
    conversation_id = int(pending["conversation_id"])
    sessions = session_factory_for_data_dir(tmp_path)
    _corrupt_origin_proposal(sessions, conversation_id, origin.id, corruption)

    executor_calls = 0
    original_execute = WriteOperationCoordinator.execute_primary

    def execute_once(self: WriteOperationCoordinator, *args: object, **kwargs: object) -> object:
        nonlocal executor_calls
        executor_calls += 1
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(WriteOperationCoordinator, "execute_primary", execute_once)
    first = _confirm(client, endpoint, pending)
    second = _confirm(client, endpoint, pending)

    assert getattr(first, "status_code") in {200, 409, 503}
    assert getattr(second, "status_code") in {200, 409, 503}
    assert len(model.calls) == 1, "post-terminal Provider must remain at Provider0"
    assert executor_calls == 1, "terminal replay must not rerun the origin executor"
    operation_id = str(pending["pending_action"]["operation_id"])
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        assert operation.status == "committed"
    assert client.get(f"/api/applications/{application['id']}").json()["status"] == "offer"


@pytest.mark.parametrize("endpoint", ("/api/chat/confirm", "/api/chat/confirm/stream"))
def test_fresh_segment_honors_non_default_max_iterations(
    tmp_path: Any,
    endpoint: str,
) -> None:
    """RED until the resolved Segment max_iter crosses the approval cutover."""

    origin = ToolCall(
        id="bounded-origin",
        name="update_application_status",
        args=json.dumps({"id": 1, "status": "offer"}),
    )
    model = _RepeatingReadModel(origin)
    app = create_app(data_dir=tmp_path, chat_model=model)
    client = TestClient(app, raise_server_exceptions=False)
    application = _application(client, "Iteration Target")
    assert application["id"] == 1
    pending = _propose(client)

    runtime = app.state.pilot_runtime
    original_resolver = runtime._dependencies.continuation_model_resolver
    assert original_resolver is not None

    class LimitedResolver:
        def resolve(
            self,
            request: object,
            conversation: object,
            policy: object | None = None,
        ) -> ResolvedModel:
            resolved = original_resolver.resolve(request, conversation, policy)  # type: ignore[attr-defined,arg-type]
            return replace(resolved, max_iter=2)

    runtime._dependencies = replace(
        runtime._dependencies,
        continuation_model_resolver=LimitedResolver(),
    )
    _confirm(client, endpoint, pending)

    assert model.calls - 1 == 2
