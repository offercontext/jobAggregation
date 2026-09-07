from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from offerpilot.ai.types import Assistant
from offerpilot.ai.resume_structured_import import generate_structured_fields
from offerpilot.api import create_app
from offerpilot.config import AIProviderProfile, Config
from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.db import init_database
from offerpilot.repositories.resumes import ResumesRepository
from offerpilot.repositories.resumes import ResumeCreate
from offerpilot.resume_structured_import import (
    ResumeStructureError,
    apply_structured_fields,
    raw_text_sha256,
    require_preview_source,
    source_fingerprint,
    validate_structured_fields,
)


RAW = """陈晨
邮箱 chen@example.com
求职意向 后端工程师 上海
教育经历 复旦大学 软件工程 2018-09 2022-06
工作经历 星云科技 后端工程师 2022-07 至今
负责 Python API 开发
项目经历 OfferPilot 项目负责人
技能 Python FastAPI
"""


@pytest.mark.parametrize("content", [{"raw_text": 1}, {"contact": []}])
def test_decoded_source_rejects_malformed_known_fields(content: dict) -> None:
    resume = SimpleNamespace(source="upload", content_json=content, parsed_data=RAW)
    with pytest.raises(ResumeStructureError, match="resume_structure_invalid_source"):
        require_preview_source(resume)


class ReplyModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        return Assistant(content=self.reply)


def _fields() -> list[dict[str, str]]:
    return [
        {"path": "contact.name", "value": "陈晨", "evidence": "陈晨"},
        {
            "path": "contact.email",
            "value": "chen@example.com",
            "evidence": "邮箱 chen@example.com",
        },
        {
            "path": "career_intent.target_roles.0",
            "value": "后端工程师",
            "evidence": "求职意向 后端工程师 上海",
        },
        {
            "path": "education.0.school",
            "value": "复旦大学",
            "evidence": "教育经历 复旦大学 软件工程 2018-09 2022-06",
        },
        {
            "path": "education.0.major",
            "value": "软件工程",
            "evidence": "教育经历 复旦大学 软件工程 2018-09 2022-06",
        },
        {
            "path": "experience.0.company",
            "value": "星云科技",
            "evidence": "工作经历 星云科技 后端工程师 2022-07 至今",
        },
        {
            "path": "experience.0.highlights.0",
            "value": "负责 Python API 开发",
            "evidence": "负责 Python API 开发",
        },
        {
            "path": "projects.0.name",
            "value": "OfferPilot",
            "evidence": "项目经历 OfferPilot 项目负责人",
        },
        {"path": "skills.0", "value": "Python", "evidence": "技能 Python FastAPI"},
    ]


def _upload_resume(client: TestClient, *, content: dict | None = None) -> dict:
    raw_text = RAW if content is None else str(content.get("raw_text") or "")
    created = client.post(
        "/api/resumes",
        json={
            "title": "上传简历",
            "text": raw_text,
            "content_json": content or {"raw_text": RAW},
        },
    ).json()
    response = client.patch(
        f"/api/resumes/{created['id']}",
        json={"source": "upload"},
    )
    assert response.status_code == 200
    return response.json()


def test_contract_validates_supported_fields_and_builds_nested_content() -> None:
    validated = validate_structured_fields(_fields(), RAW)
    merged = apply_structured_fields(
        {
            "raw_text": RAW,
            "contact": {"phone": "13800000000", "legacy": "keep"},
            "education": [],
            "experience": [{"company": "已有公司"}],
            "unknown": {"keep": True},
        },
        validated,
    )

    assert merged["contact"] == {
        "phone": "13800000000",
        "legacy": "keep",
        "name": "陈晨",
        "email": "chen@example.com",
    }
    assert merged["education"][0]["school"] == "复旦大学"
    assert merged["experience"] == [{"company": "已有公司"}]
    assert merged["skills"] == ["Python"]
    assert merged["unknown"] == {"keep": True}
    assert merged["raw_text"] == RAW


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ([{"path": "contact.linkedin", "value": "x", "evidence": "x"}], "resume_structure_invalid_fields"),
        ([{"path": "education.0.company", "value": "星云科技", "evidence": "星云科技"}], "resume_structure_invalid_fields"),
        ([{"path": "experience.0.school", "value": "复旦大学", "evidence": "复旦大学"}], "resume_structure_invalid_fields"),
        ([{"path": "projects.0.title", "value": "后端工程师", "evidence": "后端工程师"}], "resume_structure_invalid_fields"),
        ([{"path": "skills.00", "value": "Python", "evidence": "Python"}], "resume_structure_invalid_fields"),
        ([{"path": "skills.０", "value": "Python", "evidence": "Python"}], "resume_structure_invalid_fields"),
        ([{"path": "skills.0", "value": " ", "evidence": " "}], "resume_structure_invalid_fields"),
        ([{"path": "skills.1", "value": "Python", "evidence": "Python"}], "resume_structure_sparse_fields"),
        (
            [
                {"path": "skills.0", "value": "Python", "evidence": "Python"},
                {"path": "skills.0", "value": "Python", "evidence": "Python"},
            ],
            "resume_structure_duplicate_fields",
        ),
        ([{"path": "skills.0", "value": "Rust", "evidence": "Python"}], "resume_structure_unsupported_value"),
        ([{"path": "skills.0", "value": "Python", "evidence": "not present"}], "resume_structure_invalid_evidence"),
        ([], "resume_structure_no_fields"),
    ],
)
def test_contract_rejects_invalid_candidates(fields: list[dict[str, str]], code: str) -> None:
    with pytest.raises(ResumeStructureError) as raised:
        validate_structured_fields(fields, RAW)
    assert raised.value.code == code


def test_contract_rejects_string_and_aggregate_limits() -> None:
    with pytest.raises(ResumeStructureError) as raised:
        validate_structured_fields(
            [{"path": "skills.0", "value": "x" * 8193, "evidence": "x" * 8193}],
            "x" * 8193,
        )
    assert raised.value.code == "resume_structure_output_too_large"


def test_preview_is_read_only_and_confirmation_preserves_source_and_existing_arrays(tmp_path) -> None:
    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    original_content = {
        "raw_text": RAW,
        "contact": {"phone": "13800000000", "legacy": "keep"},
        "experience": [{"company": "已有公司", "unknown": "keep"}],
        "unknown": {"keep": True},
    }
    resume = _upload_resume(client, content=original_content)

    preview = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={})

    assert preview.status_code == 200
    assert preview.json()["resume_id"] == resume["id"]
    assert preview.json()["fields"] == _fields()
    assert model.calls == 1
    assert client.get(f"/api/resumes/{resume['id']}").json()["content_json"] == original_content

    confirmed = client.post(
        f"/api/resumes/{resume['id']}/structure-confirm",
        json={
            "source_fingerprint": preview.json()["source_fingerprint"],
            "fields": preview.json()["fields"],
        },
    )

    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["content_json"]["contact"]["phone"] == "13800000000"
    assert result["content_json"]["contact"]["name"] == "陈晨"
    assert result["content_json"]["experience"] == original_content["experience"]
    assert result["content_json"]["unknown"] == {"keep": True}
    assert result["content_json"]["raw_text"] == RAW
    assert result["parsed_data"] == RAW
    assert result["source"] == "upload"
    assert result["content_json"]["import_review"] == {
        "version": 1,
        "raw_text_sha256": raw_text_sha256(RAW),
    }

    replay = client.post(
        f"/api/resumes/{resume['id']}/structure-confirm",
        json={
            "source_fingerprint": preview.json()["source_fingerprint"],
            "fields": preview.json()["fields"],
        },
    )
    assert replay.status_code == 409
    assert replay.json()["error_code"] == "resume_structure_source_conflict"


def test_preview_rejects_before_provider_for_empty_nonupload_and_oversize(tmp_path) -> None:
    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    empty = _upload_resume(client, content={"raw_text": ""})
    manual = client.post("/api/resumes", json={"title": "manual", "text": RAW}).json()
    huge = _upload_resume(client, content={"raw_text": "中" * (128 * 1024)})

    empty_result = client.post(f"/api/resumes/{empty['id']}/structure-preview", json={})
    manual_result = client.post(f"/api/resumes/{manual['id']}/structure-preview", json={})
    huge_result = client.post(f"/api/resumes/{huge['id']}/structure-preview", json={})

    assert empty_result.status_code == 422
    assert empty_result.json()["error_code"] == "resume_structure_empty_source"
    assert manual_result.status_code == 422
    assert manual_result.json()["error_code"] == "resume_structure_source_not_supported"
    assert huge_result.status_code == 413
    assert huge_result.json()["error_code"] == "resume_structure_input_too_large"
    assert model.calls == 0


def test_preview_rejects_malformed_or_invented_model_output_without_write(tmp_path) -> None:
    model = ReplyModel('{"fields":[{"path":"skills.0","value":"Rust","evidence":"Python"}]}')
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)

    response = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={})

    assert response.status_code == 502
    assert response.json() == {
        "error": "AI 返回的分类结果无效，请重试。",
        "error_code": "resume_structure_invalid_output",
    }
    assert client.get(f"/api/resumes/{resume['id']}").json()["content_json"] == {"raw_text": RAW}


def test_preview_provider_failure_does_not_leak_exception(tmp_path) -> None:
    class FailingModel:
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret-key-value")

    client = TestClient(create_app(data_dir=tmp_path, chat_model=FailingModel()))
    resume = _upload_resume(client)

    response = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={})

    assert response.status_code == 502
    assert response.json()["error_code"] == "resume_structure_provider_failed"
    assert "secret-key-value" not in response.text


def test_configured_model_budget_checks_every_fallback_before_provider() -> None:
    model = ConfiguredAIClient(
        Config(
            active_provider_id="large",
            fallback_provider_ids=["small"],
            providers=[
                AIProviderProfile(
                    id="large",
                    provider="openai",
                    api_key="test",
                    base_url="https://example.test/v1",
                    model="large",
                    context_window=32768,
                    max_output_tokens=4096,
                ),
                AIProviderProfile(
                    id="small",
                    provider="openai",
                    api_key="test",
                    base_url="https://example.test/v1",
                    model="small",
                    context_window=2048,
                    max_output_tokens=512,
                ),
            ],
        )
    )

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provider must not be called")

    model.complete = forbidden  # type: ignore[method-assign]
    with pytest.raises(ResumeStructureError) as raised:
        generate_structured_fields(model, RAW)
    assert raised.value.code == "resume_structure_model_budget_exceeded"


def test_configured_model_budget_failure_is_safely_classified() -> None:
    model = ConfiguredAIClient(Config(api_key="test"))
    model._agent_gateway = object()  # type: ignore[assignment]

    with pytest.raises(ResumeStructureError) as raised:
        generate_structured_fields(model, RAW)

    assert raised.value.code == "resume_structure_ai_configuration_invalid"
    assert "agent_gateway" not in raised.value.message


def test_generation_propagates_base_exception() -> None:
    class CancelledModel:
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        generate_structured_fields(CancelledModel(), RAW)


def test_preview_requires_configured_ai_only_after_source_validation(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    resume = _upload_resume(client)

    response = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={})

    assert response.status_code == 503
    assert response.json()["error_code"] == "resume_structure_ai_not_configured"


def test_routes_reject_unknown_or_malformed_request_fields(tmp_path) -> None:
    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)

    preview = client.post(
        f"/api/resumes/{resume['id']}/structure-preview", json={"unexpected": True}
    )
    confirm = client.post(
        f"/api/resumes/{resume['id']}/structure-confirm", json=[]
    )

    assert preview.status_code == 422
    assert preview.json()["error_code"] == "resume_structure_invalid_request"
    assert confirm.status_code == 422
    assert confirm.json()["error_code"] == "resume_structure_invalid_request"
    assert model.calls == 0


def test_confirmation_rejects_stale_preview_after_manual_edit_without_partial_write(tmp_path) -> None:
    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)
    preview = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={}).json()
    changed = {"raw_text": RAW, "contact": {"phone": "new"}, "unknown": "preserve"}
    assert client.patch(f"/api/resumes/{resume['id']}", json={"content_json": changed}).status_code == 200

    response = client.post(
        f"/api/resumes/{resume['id']}/structure-confirm",
        json={"source_fingerprint": preview["source_fingerprint"], "fields": preview["fields"]},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "resume_structure_source_conflict"
    assert client.get(f"/api/resumes/{resume['id']}").json()["content_json"] == changed


def test_preview_rejects_source_change_while_provider_is_running(tmp_path) -> None:
    class BlockingModel(ReplyModel):
        def __init__(self) -> None:
            super().__init__(json.dumps({"fields": _fields()}, ensure_ascii=False))
            self.entered = Event()
            self.release = Event()

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.entered.set()
            assert self.release.wait(5)
            return super().complete(messages, tools)

    model = BlockingModel()
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post,
            f"/api/resumes/{resume['id']}/structure-preview",
            json={},
        )
        assert model.entered.wait(5)
        assert client.patch(
            f"/api/resumes/{resume['id']}", json={"source": "manual"}
        ).status_code == 200
        model.release.set()
        response = future.result(timeout=5)

    assert response.status_code == 409
    assert response.json()["error_code"] == "resume_structure_source_conflict"


def test_repository_confirmation_allows_exactly_one_concurrent_winner(tmp_path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    resume = _upload_resume(client)
    factory = init_database(tmp_path / "data.db")
    repo = ResumesRepository(factory)
    row = repo.get(resume["id"])
    assert row is not None
    fingerprint = source_fingerprint(row)

    barrier = Barrier(2)

    def confirm() -> str:
        barrier.wait(timeout=5)
        try:
            repo.merge_structured_import(resume["id"], fingerprint, _fields())
        except ResumeStructureError as exc:
            return exc.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))

    assert sorted(results) == ["ok", "resume_structure_source_conflict"]


def test_repository_confirmation_rejects_delete_and_source_change(tmp_path) -> None:
    factory = init_database(tmp_path / "data.db")
    repo = ResumesRepository(factory)
    deleted = repo.create(
        ResumeCreate(
            title="deleted",
            source="upload",
            parsed_data=RAW,
            content_json={"raw_text": RAW},
        )
    )
    deleted_fingerprint = source_fingerprint(deleted)
    repo.delete(deleted.id)
    with pytest.raises(ResumeStructureError) as deleted_error:
        repo.merge_structured_import(deleted.id, deleted_fingerprint, _fields())
    assert deleted_error.value.code == "resume_structure_not_found"

    changed = repo.create(
        ResumeCreate(
            title="changed",
            source="upload",
            parsed_data=RAW,
            content_json={"raw_text": RAW},
        )
    )
    changed_fingerprint = source_fingerprint(changed)
    repo.update(changed.id, {"source": "manual"})
    with pytest.raises(ResumeStructureError) as changed_error:
        repo.merge_structured_import(changed.id, changed_fingerprint, _fields())
    assert changed_error.value.code == "resume_structure_source_conflict"


def test_repository_structured_merge_rejects_caller_owned_session(tmp_path) -> None:
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        bound = ResumesRepository(factory, session)
        with pytest.raises(RuntimeError, match="owned session"):
            bound.merge_structured_import(1, "0" * 64, _fields())


def test_repository_structured_merge_rolls_back_when_commit_fails(tmp_path) -> None:
    from sqlalchemy import event

    factory = init_database(tmp_path / "data.db")
    repo = ResumesRepository(factory)
    resume = repo.create(
        ResumeCreate(
            title="rollback",
            source="upload",
            parsed_data=RAW,
            content_json={"raw_text": RAW, "unknown": "keep"},
        )
    )
    fingerprint = source_fingerprint(resume)

    def fail_commit(session):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic commit failure")

    event.listen(factory.class_, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            repo.merge_structured_import(resume.id, fingerprint, _fields())
    finally:
        event.remove(factory.class_, "before_commit", fail_commit)

    reloaded = repo.get(resume.id)
    assert reloaded is not None
    assert json.loads(reloaded.content_json) == {"raw_text": RAW, "unknown": "keep"}


def test_confirmation_commit_failure_returns_safe_fixed_error(tmp_path) -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)
    preview = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={}).json()

    def fail_commit(session):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"secret {RAW}")

    event.listen(Session, "before_commit", fail_commit)
    try:
        response = client.post(
            f"/api/resumes/{resume['id']}/structure-confirm",
            json={
                "source_fingerprint": preview["source_fingerprint"],
                "fields": preview["fields"],
            },
        )
    finally:
        event.remove(Session, "before_commit", fail_commit)

    assert response.status_code == 500
    assert response.json() == {
        "error": "分类结果保存失败，请重新读取简历确认状态。",
        "error_code": "resume_structure_save_failed",
    }
    assert RAW not in response.text
    reloaded = client.get(f"/api/resumes/{resume['id']}").json()
    assert reloaded["content_json"] == {"raw_text": RAW}


def test_invalid_persisted_content_is_rejected_without_provider_or_overwrite(tmp_path) -> None:
    model = ReplyModel(json.dumps({"fields": _fields()}, ensure_ascii=False))
    client = TestClient(create_app(data_dir=tmp_path, chat_model=model))
    resume = _upload_resume(client)
    factory = init_database(tmp_path / "data.db")
    with factory() as session:
        from offerpilot.models import Resume

        row = session.get(Resume, resume["id"])
        assert row is not None
        row.content_json = "not-json"
        session.commit()

    response = client.post(f"/api/resumes/{resume['id']}/structure-preview", json={})

    assert response.status_code == 422
    assert response.json()["error_code"] == "resume_structure_invalid_source"
    assert model.calls == 0
    with factory() as session:
        from offerpilot.models import Resume

        row = session.get(Resume, resume["id"])
        assert row is not None
        assert row.content_json == "not-json"


@pytest.mark.parametrize(
    "malformed",
    [
        {"raw_text": RAW, "contact": []},
        {"raw_text": RAW, "career_intent": []},
        {"raw_text": RAW, "education": {}},
        {"raw_text": RAW, "experience": ["wrong"]},
        {"raw_text": RAW, "projects": "wrong"},
        {"raw_text": RAW, "skills": [1]},
        {"raw_text": 1},
    ],
)
def test_known_persisted_section_shape_is_rejected_without_provider(
    malformed: dict,
) -> None:
    resume = SimpleNamespace(
        source="upload",
        content_json=json.dumps(malformed, ensure_ascii=False),
        parsed_data=RAW,
    )
    with pytest.raises(ResumeStructureError) as raised:
        require_preview_source(resume)
    assert raised.value.code == "resume_structure_invalid_source"


def test_complete_json_strict_mode_rejects_duplicate_keys() -> None:
    from offerpilot.ai.workflows import complete_json

    model = ReplyModel('{"fields":[],"fields":[]}')
    with pytest.raises(RuntimeError):
        complete_json(model, system="s", user="u", strict_json=True)
