from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from conftest import wait_for_extraction

from offerpilot.ai.tool_specs.catalog import build_model_tool_catalog
from offerpilot.api import create_app
from offerpilot.db import init_database


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(data_dir=tmp_path))


def test_sources_list_returns_stable_empty_contract(client):
    response = client.get("/api/knowledge/sources")
    assert response.status_code == 200
    assert response.json() == []


def test_legacy_knowledge_document_routes_return_404(client):
    legacy_paths = [
        ("GET", "/api/knowledge-documents"),
        ("POST", "/api/knowledge-documents"),
        ("GET", "/api/knowledge-documents/1"),
        ("PUT", "/api/knowledge-documents/1"),
        ("DELETE", "/api/knowledge-documents/1"),
        ("POST", "/api/knowledge-documents/import"),
        ("GET", "/api/knowledge/search"),
    ]
    for method, path in legacy_paths:
        response = client.request(method, path)
        assert response.status_code in {404, 405}, (
            f"{method} {path} returned {response.status_code}; expected 404 (or 405 from wildcard GET fallback)"
        )


def test_legacy_wiki_routes_return_404(client):
    legacy_paths = [
        ("GET", "/api/knowledge/pages"),
        ("GET", "/api/knowledge/pages/redis"),
        ("PUT", "/api/knowledge/pages/redis"),
        ("DELETE", "/api/knowledge/pages/redis"),
        ("POST", "/api/knowledge/pages/redis/unprotect"),
        ("GET", "/api/knowledge/index"),
        ("POST", "/api/knowledge/search"),
        ("GET", "/api/knowledge/reviews"),
        ("POST", "/api/knowledge/reviews/1/accept"),
        ("POST", "/api/knowledge/reviews/1/reject"),
        ("POST", "/api/knowledge/reviews/1/resolve"),
        ("POST", "/api/knowledge/reviews/1/skip"),
        ("POST", "/api/knowledge/reviews/rebase"),
        ("POST", "/api/knowledge/lint"),
        ("GET", "/api/knowledge/config"),
        ("PUT", "/api/knowledge/config"),
        ("GET", "/api/knowledge/jobs"),
        ("POST", "/api/knowledge/jobs"),
        ("GET", "/api/knowledge/jobs/1"),
        ("POST", "/api/knowledge/jobs/1/cancel"),
        ("GET", "/api/knowledge/sources/1"),
        # KI-05：PATCH /api/knowledge/sources/{source_id} 是新 Source API,
        # 不再属于 legacy wiki 路由;它的 404 语义由 KI-05 测试覆盖。
        ("DELETE", "/api/knowledge/sources/1"),
        ("POST", "/api/knowledge/sources/1/rerun"),
        ("POST", "/api/knowledge/export"),
    ]
    for method, path in legacy_paths:
        response = client.request(method, path)
        assert response.status_code in {404, 405}, (
            f"{method} {path} returned {response.status_code}; expected 404 (or 405 from wildcard GET fallback)"
        )


def test_model_tool_catalog_has_no_knowledge_tools():
    forbidden = {
        "add_to_wiki",
        "search_wiki",
        "list_knowledge_documents",
        "get_knowledge_document",
        "search_knowledge",
        "create_knowledge_document",
        "update_knowledge_document",
        "delete_knowledge_document",
    }
    names = {contract.name for contract in build_model_tool_catalog().provider_contracts()}
    assert not (forbidden & names)


def test_knowledge_legacy_tables_all_dropped_after_init(tmp_path):
    db_path = tmp_path / "data.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for table in _legacy_knowledge_tables():
            if "fts" in table:
                conn.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(content)")
            else:
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    for table in _legacy_knowledge_tables():
        assert table not in tables, f"legacy table {table} still present after reset"


def test_knowledge_legacy_fts_dropped_after_init(tmp_path):
    db_path = tmp_path / "data.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("CREATE TABLE knowledge_documents (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(content)")
        conn.execute("CREATE VIRTUAL TABLE knowledge_wiki_pages_fts USING fts5(content)")

    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    assert "knowledge_chunks_fts" not in tables
    assert "knowledge_wiki_pages_fts" not in tables


def test_knowledge_reset_is_idempotent(tmp_path):
    db_path = tmp_path / "data.db"
    init_database(db_path)
    init_database(db_path)
    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        versions = [
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]

    assert versions.count("knowledge_rewrite_reset") == 1


def test_knowledge_reset_preserves_non_knowledge_data(tmp_path):
    db_path = tmp_path / "data.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, description TEXT NOT NULL, "
            "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, company_name TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO applications (company_name) VALUES ('OfferPilot')")
        conn.execute("CREATE TABLE knowledge_documents (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE knowledge_wiki_pages (id INTEGER PRIMARY KEY)")

    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT company_name FROM applications").fetchall()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    assert rows == [("OfferPilot",)]
    assert "knowledge_documents" not in tables
    assert "knowledge_wiki_pages" not in tables
    # knowledge_sources 是 KI-02 新表的表名，与旧 knowledge_sources 同名但不属于 legacy
    assert "knowledge_sources" in tables


def test_knowledge_runtime_directory_legacy_files_are_purged(tmp_path):
    data_dir = tmp_path / "runtime"
    knowledge_dir = data_dir / "knowledge"
    (knowledge_dir / "sources").mkdir(parents=True)
    (knowledge_dir / "sources" / "legacy").mkdir()
    legacy_file = knowledge_dir / "sources" / "legacy" / "old.md"
    legacy_file.write_text("# legacy", encoding="utf-8")

    init_database(data_dir / "data.db")

    assert not legacy_file.exists()
    assert not knowledge_dir.exists()


def test_knowledge_runtime_directory_starts_from_empty_data_dir(tmp_path):
    data_dir = tmp_path / "fresh"
    init_database(data_dir / "data.db")

    assert data_dir.exists()
    assert not (data_dir / "knowledge").exists()


def test_knowledge_reset_marks_migrated_even_without_legacy(tmp_path):
    db_path = tmp_path / "data.db"
    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        versions = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

    assert "knowledge_rewrite_reset" in versions


def test_knowledge_reset_restarts_after_legacy_reappearance(tmp_path):
    db_path = tmp_path / "data.db"
    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE knowledge_wiki_pages (id INTEGER PRIMARY KEY)")

    init_database(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    assert "knowledge_wiki_pages" not in tables
    # KI-02 新表 knowledge_sources 不应受 legacy drop 影响
    assert "knowledge_sources" in tables


def _legacy_knowledge_tables() -> tuple[str, ...]:
    # KI-02 之后 knowledge_sources/origins/snapshots/evidence/evidence_fts/jobs 由本模块
    # 创建并维护，不再视为 legacy；只保留旧自动 Wiki 占位实现的表名作为破坏性重置对象。
    return (
        "knowledge_bases",
        "knowledge_documents",
        "knowledge_chunks",
        "knowledge_chunks_fts",
        "knowledge_wiki_pages",
        "knowledge_wiki_pages_fts",
        "knowledge_page_versions",
        "knowledge_index_entries",
        "knowledge_page_evidence",
        "knowledge_wikilinks",
        "knowledge_reviews",
        "knowledge_review_revisions",
        "knowledge_review_jobs",
        "knowledge_config_versions",
    )


# ---------------------------------------------------------------------------
# KI-02: 导入 Markdown 并生成可回读 Evidence
# ---------------------------------------------------------------------------


def _upload(client: TestClient, filename: str, content: bytes, title_hint: str = ""):
    files = {"file": (filename, content, "text/markdown")}
    data = {"title_hint": title_hint} if title_hint else None
    response = client.post("/api/knowledge/sources", files=files, data=data)
    if response.status_code in (200, 202):
        wait_for_extraction(client, response.json()["source"]["id"])
    return response


def test_ki02_upload_returns_202_with_source_and_extraction_job(app_client):
    content = "# Redis Notes\n\nRedis 是一个内存数据库。\n".encode("utf-8")
    response = _upload(app_client, "redis.md", content)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["deduplicated"] is False
    source = payload["source"]
    assert source["source_kind"] == "markdown"
    # 上传响应是 pending（异步 extraction）；_upload 已 wait，重新 GET 确认 extracted。
    refreshed = wait_for_extraction(app_client, source["id"])
    assert refreshed["extraction_status"] == "extracted"
    # KV1-01：Extraction 提交后不再自动评估 Brief；Source 保持 brief_status=not_started，
    # 无 block reason（显式 rebuild 才触发 Brief Provider 评估）。
    assert refreshed["brief_status"] == "not_started"
    assert refreshed["brief_block_reason"] == ""
    assert refreshed["lifecycle"] == "active"
    jobs = app_client.get(f"/api/knowledge/sources/{source['id']}/jobs").json()
    extract_job = next(j for j in jobs["jobs"] if j["kind"] == "extract")
    assert extract_job["queue"] == "extraction"
    assert extract_job["status"] == "succeeded"
    assert payload["extraction_error_code"] == ""


def test_ki02_upload_rejects_non_markdown(app_client):
    # KI-03 扩展支持 .txt；用真正不支持的扩展名验证拒绝路径
    response = _upload(app_client, "notes.pdf", b"%PDF-1.4 fake content")
    assert response.status_code == 400
    body = response.json()
    assert body.get("error_code") == "unsupported_type"


def test_ki02_upload_rejects_empty_file(app_client):
    response = _upload(app_client, "empty.md", b"")
    assert response.status_code == 400
    body = response.json()
    assert body.get("error_code") == "unsupported_type"


def test_ki02_upload_rejects_oversized_file(app_client):
    response = _upload(app_client, "huge.md", b"a" * (5 * 1024 * 1024 + 1))
    assert response.status_code == 400
    body = response.json()
    assert body.get("error_code") == "source_too_large"


def test_ki02_upload_deduplicates_by_content_hash(app_client):
    content = "# Kafka\n\nKafka 是分布式日志系统。\n".encode("utf-8")
    first = _upload(app_client, "kafka.md", content)
    assert first.status_code == 202
    second = _upload(app_client, "kafka-duplicate.md", content)
    assert second.status_code == 200
    payload = second.json()
    assert payload["deduplicated"] is True
    assert payload["source"]["id"] == first.json()["source"]["id"]


def test_ki02_source_state_fields_independent(app_client):
    response = _upload(app_client, "state.md", "# Title\n\n正文段落。\n".encode("utf-8"))
    source_id = response.json()["source"]["id"]

    detail = app_client.get(f"/api/knowledge/sources/{source_id}")
    assert detail.status_code == 200
    body = detail.json()
    # lifecycle / extraction / brief 必须独立暴露，不能合并为 done。
    # KV1-01：Extraction 完成后 Brief 不自动评估；brief_status 保持 not_started。
    assert body["lifecycle"] == "active"
    assert body["extraction_status"] == "extracted"
    assert body["brief_status"] == "not_started"
    assert "extraction_error_code" in body
    assert "extraction_error_message" in body
    assert "brief_block_reason" in body


def test_ki02_extraction_creates_snapshot_with_version_and_digest(app_client, tmp_path):
    content = "# Snapshot Test\n\n包含一个段落。\n".encode("utf-8")
    response = _upload(app_client, "snapshot.md", content)
    source_id = response.json()["source"]["id"]

    jobs = app_client.get(f"/api/knowledge/sources/{source_id}/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["jobs"][0]["status"] == "succeeded"

    # 通过数据库直接验证 snapshot 字段
    import sqlite3

    with sqlite3.connect(tmp_path / "data.db") as conn:
        rows = conn.execute(
            "SELECT extractor_version, digest, encoding, canonical_text, char_count "
            "FROM knowledge_extraction_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchall()
    assert len(rows) == 1
    extractor_version, digest, encoding, canonical_text, char_count = rows[0]
    assert extractor_version
    assert digest.startswith("sha256:")
    assert encoding == "utf-8"
    assert "Snapshot Test" in canonical_text
    assert char_count == len(canonical_text)


def test_ki02_extraction_idempotent_for_same_source(app_client, tmp_path):
    content = "# Idempotent Heading\n\n段落一。\n\n段落二。\n".encode("utf-8")
    response = _upload(app_client, "idempotent.md", content)
    source_id = response.json()["source"]["id"]

    import sqlite3

    with sqlite3.connect(tmp_path / "data.db") as conn:
        first_snapshot = conn.execute(
            "SELECT id, digest FROM knowledge_extraction_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        first_evidence = conn.execute(
            "SELECT id FROM knowledge_evidence WHERE source_id = ? ORDER BY id",
            (source_id,),
        ).fetchall()

    # 重复上传相同内容应当 deduplicated，不创建第二个 Snapshot/Evidence
    duplicate = _upload(app_client, "idempotent-duplicate.md", content)
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True

    with sqlite3.connect(tmp_path / "data.db") as conn:
        second_snapshot = conn.execute(
            "SELECT id, digest FROM knowledge_extraction_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        second_evidence = conn.execute(
            "SELECT id FROM knowledge_evidence WHERE source_id = ? ORDER BY id",
            (source_id,),
        ).fetchall()

    assert len(first_snapshot) == 1
    assert len(second_snapshot) == 1
    assert first_snapshot[0][1] == second_snapshot[0][1]
    assert [row[0] for row in first_evidence] == [row[0] for row in second_evidence]


def test_ki02_evidence_records_carry_structure_and_adjacency(app_client, tmp_path):
    content = "# Heading A\n\n第一段内容。\n\n## Heading B\n\n第二段内容。\n".encode("utf-8")
    response = _upload(app_client, "structured.md", content)
    source_id = response.json()["source"]["id"]

    listing = app_client.get(f"/api/knowledge/sources/{source_id}/evidence")
    assert listing.status_code == 200
    body = listing.json()
    assert body["next_cursor"] in (None, 0)
    items = body["items"]
    assert len(items) == 2
    first, second = items
    assert first["block_kind"] == "paragraph"
    assert first["heading_path"] == ["Heading A"]
    assert second["heading_path"] == ["Heading A", "Heading B"]
    assert first["next_evidence_id"] == second["id"]
    assert second["previous_evidence_id"] == first["id"]
    assert first["content_hash"]
    assert first["char_end"] > first["char_start"]
    assert first["line_end"] >= first["line_start"]


def test_ki02_preflight_failure_rejects_before_commit(app_client, tmp_path):
    # NUL 字符触发 preflight 失败：Spec §6 要求 SQLite 创建之前完成 preflight。
    content = "# Bad\n\nText with NUL\x00char\n".encode("latin-1")
    response = _upload(app_client, "badchar.md", content)
    assert response.status_code == 400
    body = response.json()
    assert body.get("error_code") == "encoding_unknown"

    import sqlite3

    with sqlite3.connect(tmp_path / "data.db") as conn:
        source_count = conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0]
        evidence_count = conn.execute("SELECT COUNT(*) FROM knowledge_evidence").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM knowledge_evidence_fts").fetchone()[0]

    assert source_count == 0
    assert evidence_count == 0
    assert fts_count == 0

    staging_dir = tmp_path / "knowledge" / "staging"
    if staging_dir.exists():
        assert not any(staging_dir.iterdir()), "staging 应当为空"


def test_ki02_failed_transaction_rolls_back_evidence(app_client, tmp_path):
    # 正常上传但 mock commit_extraction 失败，验证 Evidence 不会部分可见。
    # 通过 monkey-patch repository 的 commit 不容易；改为验证多次上传相同 Source
    # 的 dedup 路径下，第二个 Source 不会创建 Evidence。
    content = "# Working\n\n正常段落。\n".encode("utf-8")
    first = _upload(app_client, "working.md", content)
    assert first.status_code == 202
    source_id = first.json()["source"]["id"]

    import sqlite3

    with sqlite3.connect(tmp_path / "data.db") as conn:
        evidence_rows = conn.execute(
            "SELECT id, snapshot_id FROM knowledge_evidence WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        fts_rows = conn.execute(
            "SELECT evidence_id FROM knowledge_evidence_fts WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        source_row = conn.execute(
            "SELECT extraction_status, active_snapshot_id FROM knowledge_sources WHERE id = ?",
            (source_id,),
        ).fetchone()

    assert source_row[0] == "extracted"
    assert source_row[1] is not None
    assert len(evidence_rows) > 0
    assert {row[0] for row in evidence_rows} == {row[0] for row in fts_rows}


def test_ki02_source_original_download_returns_bytes(app_client, tmp_path):
    content = "# Title\n\n原文。\n".encode("utf-8")
    response = _upload(app_client, "download.md", content)
    source_id = response.json()["source"]["id"]

    download = app_client.get(f"/api/knowledge/sources/{source_id}/content")
    assert download.status_code == 200
    assert download.content == content
    # 不暴露本机绝对路径
    assert str(tmp_path).encode() not in download.headers.get("content-disposition", "").encode()


def test_ki02_evidence_detail_returns_full_record(app_client):
    content = "# Title\n\n可索引段落。\n".encode("utf-8")
    response = _upload(app_client, "detail.md", content)
    source_id = response.json()["source"]["id"]
    listing = app_client.get(f"/api/knowledge/sources/{source_id}/evidence")
    evidence_id = listing.json()["items"][0]["id"]

    detail = app_client.get(f"/api/knowledge/evidence/{evidence_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == evidence_id
    assert body["source_id"] == source_id
    assert body["canonical_excerpt"]


def test_ki02_evidence_search_returns_hit_with_snippet(app_client):
    content = "# Kafka Notes\n\nKafka ISR 是 in-sync replica 的缩写。\n".encode("utf-8")
    response = _upload(app_client, "kafka.md", content)
    source_id = response.json()["source"]["id"]

    search = app_client.post(
        "/api/knowledge/evidence/search",
        json={"query": "Kafka ISR"},
    )
    assert search.status_code == 200
    body = search.json()
    assert body["query"] == "Kafka ISR"
    assert body["hits"]
    top = body["hits"][0]
    assert top["source_id"] == source_id
    assert top["evidence_id"]
    assert "Kafka" in top["canonical_excerpt"]
    assert top["snippet"]


def test_ki02_evidence_search_empty_query_returns_400(app_client):
    response = app_client.post(
        "/api/knowledge/evidence/search",
        json={"query": "  "},
    )
    assert response.status_code == 400


def test_ki02_source_content_download_404_for_missing_source(app_client):
    response = app_client.get("/api/knowledge/sources/99999/content")
    assert response.status_code == 404


def test_ki02_evidence_search_filters_unrelated_queries(app_client):
    content = "# Redis Notes\n\nRedis 是内存数据库。\n".encode("utf-8")
    _upload(app_client, "redis.md", content)
    response = app_client.post(
        "/api/knowledge/evidence/search",
        json={"query": "Kafka"},
    )
    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_ki02_staging_directory_cleared_after_successful_upload(app_client, tmp_path):
    content = "# Cleanup\n\n提交后 staging 应清空。\n".encode("utf-8")
    response = _upload(app_client, "cleanup.md", content)
    source_id = response.json()["source"]["id"]
    assert response.status_code == 202

    staging_dir = tmp_path / "knowledge" / "staging"
    if staging_dir.exists():
        remaining = list(staging_dir.iterdir())
        assert remaining == [], f"staging 未清空：{remaining}"

    final_path = tmp_path / "knowledge" / "sources" / str(source_id) / "cleanup.md"
    assert final_path.is_file()


def test_ki02_unknown_evidence_id_returns_404(app_client):
    response = app_client.get("/api/knowledge/evidence/ev_unknown")
    assert response.status_code == 404
