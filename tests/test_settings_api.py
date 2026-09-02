import base64
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from offerpilot.ai import client as ai_client
from offerpilot.api import create_app
from offerpilot.agent_runtime.keyring import JOURNAL_KEY_FILENAME, load_or_create_journal_key
from offerpilot.config import AIProviderProfile, Config, load_config, save_config
from offerpilot.diagnostics import read_recent_log_entries


def test_get_settings_hides_api_key(tmp_path):
    save_config(tmp_path, Config(api_key="sk-secret"))
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "api_key" not in response.json()


def test_get_settings_exposes_version_and_data_directory_without_secrets(tmp_path):
    save_config(tmp_path, Config(api_key="sk-secret", auth_token="auth-secret"))
    body = TestClient(create_app(data_dir=tmp_path)).get("/api/settings").json()

    assert body["version"] == "0.1.0"
    assert body["data_dir"] == str(tmp_path.resolve())
    assert "sk-secret" not in str(body)
    assert "auth-secret" not in str(body)


def test_put_settings_preserves_blank_api_key(tmp_path):
    save_config(tmp_path, Config(api_key="sk-secret"))
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": True,
            "base_url": "https://example.test/v1",
            "model": "model",
            "api_key": "",
            "runtime_mode": "server",
            "auth_enabled": True,
            "log_level": "DEBUG",
        },
    )

    assert response.status_code == 200
    assert response.json()["chat_auto_approve_writes"] is False
    assert response.json()["runtime_mode"] == "server"
    assert response.json()["auth_enabled"] is True
    assert response.json()["log_level"] == "DEBUG"
    assert load_config(tmp_path).api_key == "sk-secret"


def test_put_settings_preserves_confirmation_secret_and_backup_redacts_it(tmp_path):
    secret = "persistent-confirmation-secret"
    save_config(tmp_path, Config(confirmation_secret=secret))
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={"active_provider_id": "default", "chat_auto_approve_writes": False},
    )

    assert response.status_code == 200
    assert load_config(tmp_path).confirmation_secret == secret

    backup = client.get("/api/settings/backup")
    assert backup.status_code == 200
    assert secret not in backup.text

    archive = client.get("/api/backups/export")
    assert archive.status_code == 200
    with ZipFile(BytesIO(archive.content)) as bundle:
        assert secret not in bundle.read("config.json").decode("utf-8")


def test_put_settings_preserves_onboarding_reopen_preference(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))
    reopened = client.patch("/api/onboarding", json={"force_open": True})
    assert reopened.status_code == 200

    response = client.put(
        "/api/settings",
        json={"chat_auto_approve_writes": True},
    )

    assert response.status_code == 200
    assert client.get("/api/onboarding").json()["force_open"] is True


def test_get_settings_exposes_provider_profiles_without_keys(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="deepseek",
            providers=[
                AIProviderProfile(
                    id="deepseek",
                    label="DeepSeek",
                    provider="openai_compatible",
                    api_key="sk-deepseek",
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat",
                    context_window=131_072,
                    max_output_tokens=8_192,
                )
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/settings")

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider == {
        "id": "deepseek",
        "label": "DeepSeek",
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "enabled": True,
        "supports_json_schema": False,
        "context_window": 131_072,
        "max_output_tokens": 8_192,
        "has_api_key": True,
    }
    assert "api_key" not in provider


def test_provider_budget_round_trips_settings_and_backup(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "active_provider_id": "default",
            "providers": [
                {
                    "id": "default",
                    "label": "Default",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o",
                    "enabled": True,
                    "context_window": 262_144,
                    "max_output_tokens": 16_384,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["providers"][0]["context_window"] == 262_144
    assert response.json()["providers"][0]["max_output_tokens"] == 16_384
    stored = load_config(tmp_path).active_provider()
    assert stored.context_window == 262_144
    assert stored.max_output_tokens == 16_384
    backup = client.get("/api/settings/backup").json()["providers"][0]
    assert backup["context_window"] == 262_144
    assert backup["max_output_tokens"] == 16_384


@pytest.mark.parametrize(
    ("context_window", "max_output_tokens"),
    [
        (None, 4_096),
        (0, 4_096),
        (-1, 4_096),
        ("32768", 4_096),
        (32_768, None),
        (32_768, 0),
        (32_768, -1),
        (32_768, "4096"),
        (4_096, 4_096),
    ],
)
def test_put_settings_rejects_incomplete_enabled_provider_budget(
    tmp_path, context_window, max_output_tokens
):
    client = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False)
    provider = {
        "id": "default",
        "label": "Default",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "enabled": True,
    }
    if context_window is not None:
        provider["context_window"] = context_window
    if max_output_tokens is not None:
        provider["max_output_tokens"] = max_output_tokens

    response = client.put(
        "/api/settings",
        json={"active_provider_id": "default", "providers": [provider]},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "启用、默认或 Fallback 模型供应商必须填写有效的上下文窗口和单次最大输出"


def test_put_settings_allows_incomplete_disabled_provider_budget(tmp_path):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "active_provider_id": "default",
            "providers": [
                {
                    "id": "default",
                    "label": "Default",
                    "provider": "openai",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                },
                {
                    "id": "disabled",
                    "label": "Disabled",
                    "provider": "openai",
                    "enabled": False,
                    "context_window": 0,
                    "max_output_tokens": 0,
                }
            ],
        },
    )

    assert response.status_code == 200
    disabled = next(
        provider for provider in response.json()["providers"] if provider["id"] == "disabled"
    )
    assert disabled["context_window"] == 0
    assert disabled["max_output_tokens"] == 0


@pytest.mark.parametrize(
    ("active_provider_id", "fallback_provider_ids"),
    [
        ("disabled", []),
        ("default", ["disabled"]),
    ],
)
def test_put_settings_rejects_incomplete_active_or_fallback_provider_budget(
    tmp_path, active_provider_id, fallback_provider_ids
):
    client = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False)

    response = client.put(
        "/api/settings",
        json={
            "active_provider_id": active_provider_id,
            "fallback_provider_ids": fallback_provider_ids,
            "providers": [
                {
                    "id": "default",
                    "label": "Default",
                    "provider": "openai",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                },
                {
                    "id": "disabled",
                    "label": "Disabled",
                    "provider": "openai",
                    "enabled": False,
                    "context_window": 0,
                    "max_output_tokens": 0,
                },
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "启用、默认或 Fallback 模型供应商必须填写有效的上下文窗口和单次最大输出"


@pytest.mark.parametrize(
    "selection_payload",
    [
        {"active_provider_id": "disabled"},
        {"fallback_provider_ids": ["disabled"]},
    ],
)
def test_legacy_put_rejects_selecting_incomplete_active_or_fallback_provider(
    tmp_path, selection_payload
):
    save_config(
        tmp_path,
        Config(
            active_provider_id="default",
            providers=[
                AIProviderProfile(
                    id="default",
                    label="Default",
                    provider="openai",
                    enabled=True,
                    context_window=128_000,
                    max_output_tokens=4_096,
                ),
                AIProviderProfile(
                    id="disabled",
                    label="Disabled",
                    provider="openai",
                    enabled=False,
                    context_window=0,
                    max_output_tokens=0,
                ),
            ],
        ),
    )

    response = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False).put(
        "/api/settings", json=selection_payload
    )

    assert response.status_code == 400
    assert response.json()["error"] == "启用、默认或 Fallback 模型供应商必须填写有效的上下文窗口和单次最大输出"


def test_legacy_put_rejects_reordering_incomplete_fallback_providers(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="default",
            fallback_provider_ids=["first", "second"],
            providers=[
                AIProviderProfile(
                    id="default",
                    label="Default",
                    provider="openai",
                    enabled=True,
                    context_window=128_000,
                    max_output_tokens=4_096,
                ),
                AIProviderProfile(
                    id="first",
                    label="First",
                    provider="openai",
                    enabled=False,
                    context_window=0,
                    max_output_tokens=0,
                ),
                AIProviderProfile(
                    id="second",
                    label="Second",
                    provider="openai",
                    enabled=False,
                    context_window=0,
                    max_output_tokens=0,
                ),
            ],
        ),
    )

    response = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False).put(
        "/api/settings", json={"fallback_provider_ids": ["second", "first"]}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "启用、默认或 Fallback 模型供应商必须填写有效的上下文窗口和单次最大输出"


@pytest.mark.parametrize("raw_capability", ["false", "0", 1, None])
def test_put_settings_treats_non_boolean_json_schema_capability_as_disabled(
    tmp_path, raw_capability
):
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": False,
            "active_provider_id": "default",
            "providers": [
                {
                    "id": "default",
                    "label": "Default",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                    "supports_json_schema": raw_capability,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["providers"][0]["supports_json_schema"] is False
    assert load_config(tmp_path).active_provider().supports_json_schema is False


def test_provider_json_schema_capability_round_trips_settings_and_backup(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="structured",
            providers=[
                AIProviderProfile(
                    id="structured",
                    label="Structured",
                    provider="openai_compatible",
                    api_key="sk-structured",
                    base_url="https://example.test/v1",
                    model="structured-model",
                    supports_json_schema=True,
                )
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    initial = client.get("/api/settings")

    assert initial.status_code == 200
    assert initial.json()["providers"][0]["supports_json_schema"] is True
    initial_backup = client.get("/api/settings/backup")
    assert initial_backup.status_code == 200
    assert initial_backup.json()["providers"][0]["supports_json_schema"] is True

    updated = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": False,
            "active_provider_id": "structured",
            "providers": [
                {
                    "id": "structured",
                    "label": "Structured",
                    "provider": "openai_compatible",
                    "base_url": "https://example.test/v1",
                    "model": "structured-model",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                    "supports_json_schema": False,
                }
            ],
        },
    )

    assert updated.status_code == 200
    assert updated.json()["providers"][0]["supports_json_schema"] is False
    assert load_config(tmp_path).active_provider().supports_json_schema is False
    backup = client.get("/api/settings/backup")
    assert backup.status_code == 200
    assert backup.json()["providers"][0]["supports_json_schema"] is False


def test_put_settings_preserves_blank_provider_api_key(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="default",
            providers=[
                AIProviderProfile(
                    id="default",
                    label="Default",
                    provider="openai",
                    api_key="sk-existing",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o",
                )
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": False,
            "active_provider_id": "default",
            "providers": [
                {
                    "id": "default",
                    "label": "Default",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "api_key": "",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                }
            ],
        },
    )

    assert response.status_code == 200
    cfg = load_config(tmp_path)
    assert cfg.active_provider().api_key == "sk-existing"
    assert cfg.active_provider().model == "gpt-4o-mini"


def test_put_settings_accepts_new_provider_profile(tmp_path):
    save_config(tmp_path, Config(api_key="sk-openai"))
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": False,
            "active_provider_id": "openrouter",
            "providers": [
                {
                    "id": "openrouter",
                    "label": "OpenRouter",
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "openai/gpt-4o",
                    "api_key": "sk-openrouter",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                }
            ],
        },
    )

    assert response.status_code == 200
    cfg = load_config(tmp_path)
    assert cfg.active_provider().id == "openrouter"
    assert cfg.active_provider().api_key == "sk-openrouter"


def test_put_settings_without_providers_preserves_existing_provider_profiles(tmp_path):
    save_config(
        tmp_path,
        Config(
            chat_auto_approve_writes=False,
            active_provider_id="deepseek",
            providers=[
                AIProviderProfile(
                    id="openai",
                    label="OpenAI",
                    provider="openai",
                    api_key="sk-openai",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o",
                    context_window=128_000,
                    max_output_tokens=4_096,
                ),
                AIProviderProfile(
                    id="deepseek",
                    label="DeepSeek",
                    provider="openai_compatible",
                    api_key="sk-deepseek",
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat",
                    context_window=64_000,
                    max_output_tokens=8_192,
                ),
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": True,
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
    )

    assert response.status_code == 200
    cfg = load_config(tmp_path)
    assert cfg.chat_auto_approve_writes is False
    assert cfg.active_provider_id == "deepseek"
    assert [(profile.id, profile.api_key) for profile in cfg.providers] == [
        ("openai", "sk-openai"),
        ("deepseek", "sk-deepseek"),
    ]
    assert [(profile.context_window, profile.max_output_tokens) for profile in cfg.providers] == [
        (128_000, 4_096),
        (64_000, 8_192),
    ]


def test_put_settings_persists_ordered_fallback_providers(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="openai",
            providers=[
                AIProviderProfile(id="openai", label="OpenAI", api_key="sk-openai"),
                AIProviderProfile(id="openrouter", label="OpenRouter", api_key="sk-openrouter"),
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.put(
        "/api/settings",
        json={
            "chat_auto_approve_writes": False,
            "active_provider_id": "openai",
            "fallback_provider_ids": ["openrouter"],
            "providers": [
                {
                    "id": "openai",
                    "label": "OpenAI",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o",
                    "api_key": "",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                },
                {
                    "id": "openrouter",
                    "label": "OpenRouter",
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "openai/gpt-4o",
                    "api_key": "",
                    "enabled": True,
                    "context_window": 128_000,
                    "max_output_tokens": 4_096,
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["fallback_provider_ids"] == ["openrouter"]
    assert load_config(tmp_path).fallback_provider_ids == ["openrouter"]


def test_provider_connection_test_uses_saved_profile(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(ai_client, "completion", fake_completion)
    save_config(
        tmp_path,
        Config(
            active_provider_id="openai",
            providers=[
                AIProviderProfile(
                    id="openai",
                    label="OpenAI",
                    provider="openai",
                    api_key="sk-openai",
                    model="gpt-4o-mini",
                    context_window=128_000,
                    max_output_tokens=4_096,
                )
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post("/api/settings/providers/test", json={"provider_id": "openai"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["provider_id"] == "openai"
    assert body["model"] == "gpt-4o-mini"
    assert body["latency_ms"] >= 0
    assert body["message"] == "连接成功"
    assert captured["api_key"] == "sk-openai"


def test_provider_connection_test_rejects_incomplete_budget_without_network(
    monkeypatch, tmp_path
):
    calls: list[dict[str, object]] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(ai_client, "completion", fake_completion)
    save_config(
        tmp_path,
        Config(
            active_provider_id="openai",
            providers=[
                AIProviderProfile(
                    id="openai",
                    label="OpenAI",
                    provider="openai",
                    api_key="sk-openai",
                    model="gpt-4o-mini",
                    context_window=0,
                    max_output_tokens=0,
                )
            ],
        ),
    )

    response = TestClient(create_app(data_dir=tmp_path)).post(
        "/api/settings/providers/test", json={"provider_id": "openai"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "请先填写有效的上下文窗口和单次最大输出",
    }
    assert calls == []


def test_provider_connection_test_masks_secret_and_logs_failure(monkeypatch, tmp_path):
    def fake_completion(**_kwargs):
        raise RuntimeError("invalid key sk-secret-value")

    monkeypatch.setattr(ai_client, "completion", fake_completion)
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post(
        "/api/settings/providers/test",
        json={
            "provider": {
                "id": "draft",
                "label": "Draft",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "api_key": "sk-secret-value",
                "enabled": True,
                "context_window": 128_000,
                "max_output_tokens": 4_096,
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "sk-secret-value" not in body["error"]
    assert read_recent_log_entries(tmp_path, limit=1)[0]["level"] == "ERROR"


def test_provider_connection_test_reuses_saved_key_for_draft_profile(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(ai_client, "completion", fake_completion)
    save_config(
        tmp_path,
        Config(
            active_provider_id="openai",
            providers=[
                AIProviderProfile(
                    id="openai",
                    label="OpenAI",
                    provider="openai",
                    api_key="sk-openai",
                    model="gpt-4o",
                    context_window=128_000,
                    max_output_tokens=4_096,
                )
            ],
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.post(
        "/api/settings/providers/test",
        json={
            "provider": {
                "id": "openai",
                "label": "OpenAI",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key": "",
                "enabled": True,
                "context_window": 128_000,
                "max_output_tokens": 4_096,
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model"] == "gpt-4o-mini"
    assert captured["api_key"] == "sk-openai"


def test_settings_backup_omits_plaintext_api_keys(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="openai",
            fallback_provider_ids=["openrouter"],
            providers=[
                AIProviderProfile(id="openai", label="OpenAI", api_key="sk-openai"),
                AIProviderProfile(id="openrouter", label="OpenRouter", api_key="sk-openrouter"),
            ],
            auth_enabled=True,
            auth_token="local-secret",
            log_level="DEBUG",
        ),
    )
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/settings/backup", headers={"X-OfferPilot-Token": "local-secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["active_provider_id"] == "openai"
    assert body["fallback_provider_ids"] == ["openrouter"]
    assert body["providers"][0]["has_api_key"] is True
    assert "api_key" not in body["providers"][0]
    assert "sk-openai" not in response.text
    assert "sk-openrouter" not in response.text


def test_backup_export_returns_local_data_archive_without_credentials(tmp_path):
    from io import BytesIO
    import zipfile

    save_config(tmp_path, Config(api_key="sk-secret", auth_token="local-secret"))
    (tmp_path / "offerpilot.log").write_text("log line\n", encoding="utf-8")
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/backups/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "offerpilot-backup-" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert {"config.json", "data.db", "offerpilot.log"}.issubset(archive.namelist())
        config = archive.read("config.json").decode("utf-8")
        assert "sk-secret" not in config
        assert "local-secret" not in config
        assert json.loads(config)["api_key"] == ""
        assert json.loads(config)["auth_token"] == ""


def test_settings_and_backup_exports_exclude_journal_key_domain(tmp_path):
    domain = load_or_create_journal_key(tmp_path)
    assert domain is not None
    key_path = tmp_path / JOURNAL_KEY_FILENAME
    original_key_bytes = key_path.read_bytes()
    client = TestClient(create_app(data_dir=tmp_path))

    settings = client.get("/api/settings")
    settings_backup = client.get("/api/settings/backup")
    archive_response = client.get("/api/backups/export")

    assert settings.status_code == 200
    assert settings_backup.status_code == 200
    for response in (settings, settings_backup):
        assert JOURNAL_KEY_FILENAME not in response.text
        assert domain.key_id not in response.text
        assert base64.urlsafe_b64encode(domain.secret).decode("ascii").rstrip("=") not in response.text
    with ZipFile(BytesIO(archive_response.content)) as archive:
        assert JOURNAL_KEY_FILENAME not in archive.namelist()
        assert domain.key_id not in "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in archive.namelist()
            if name != "data.db"
        )
    assert key_path.read_bytes() == original_key_bytes


def test_backup_export_excludes_interrupted_journal_key_temp_files(tmp_path):
    secret = "journal-secret-canary"
    (tmp_path / f".agent-journal-key.json.{'a' * 32}.tmp").write_text(
        secret,
        encoding="utf-8",
    )
    (tmp_path / ".agent-journal-key.json.lock").write_text("", encoding="utf-8")

    response = TestClient(create_app(data_dir=tmp_path)).get("/api/backups/export")

    with ZipFile(BytesIO(response.content)) as archive:
        assert all("agent-journal-key.json" not in name for name in archive.namelist())
        assert secret.encode() not in response.content


def test_settings_reports_a_keyed_enabled_fallback_as_available(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="primary",
            fallback_provider_ids=["fallback"],
            providers=[
                AIProviderProfile(id="primary", label="Primary", api_key="", enabled=True),
                AIProviderProfile(id="fallback", label="Fallback", api_key="sk-fallback", enabled=True),
            ],
        ),
    )

    response = TestClient(create_app(data_dir=tmp_path)).get("/api/settings")

    assert response.status_code == 200
    assert response.json()["has_api_key"] is True


def test_settings_does_not_report_a_disabled_keyed_fallback_as_available(tmp_path):
    save_config(
        tmp_path,
        Config(
            active_provider_id="primary",
            fallback_provider_ids=["fallback"],
            providers=[
                AIProviderProfile(id="primary", label="Primary", api_key="", enabled=True),
                AIProviderProfile(id="fallback", label="Fallback", api_key="sk-fallback", enabled=False),
            ],
        ),
    )

    response = TestClient(create_app(data_dir=tmp_path)).get("/api/settings")

    assert response.status_code == 200
    assert response.json()["has_api_key"] is False
