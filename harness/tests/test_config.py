# tests/test_config.py - merged config directory and env overrides
import json
from pathlib import Path

import pytest

from config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    clear_local_mcp_preload,
    load_local_mcp_preload,
    save_local_mcp_preload,
    save_model_config_overlay,
)

_CONFIG_KEYS = (
    "EVENT_TRANSPORT",
    "EVENT_HANDLER_TIMEOUT",
    "RETRY_POLICY",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_MAX_DELAY",
    "RETRY_TOTAL_TIMEOUT",
    "AGENT_MODEL",
    "SESSION_STORE",
    "SESSION_ID",
    "SESSION_COMPACTION",
    "MEMORY_STORE",
    "MEMORY_INDEX",
    "MEMORY_RETRIEVER",
    "MEMORY_POLICY",
    "MEMORY_EXTRACTOR",
    "MEMORY_SCOPE",
    "MEMORY_OWNER_ID",
    "MEMORY_AGENT_ID",
    "MEMORY_TENANT_ID",
    "EMBEDDING_PROVIDER",
    "CONTEXT_MAX_TOKENS",
    "CONTEXT_STRATEGY",
    "MCP_PRELOAD",
    "SKILL_PRELOAD",
    "SKILL_MAX_CONTENT_BYTES",
    "SKILL_MAX_PRELOAD_BYTES",
    "SKILL_MAX_RESOURCES",
    "SKILL_MAX_RESOURCE_BYTES",
    "SKILL_MAX_RESOURCE_TOTAL_BYTES",
    "SKILL_MAX_DESCRIPTION_BYTES",
    "SKILL_MAX_RESOURCE_DESCRIPTION_BYTES",
    "SKILL_MAX_LISTING_BYTES",
    "SKILL_AGENT",
    "SKILL_NAME_ONLY",
    "MODEL_FALLBACK",
    "MODEL_KEEP_WARM",
    "MODEL_ROUTER",
)


def _clean_env(monkeypatch) -> None:
    for key in _CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_default_config_path_points_to_config_directory() -> None:
    assert DEFAULT_CONFIG_PATH == Path(__file__).resolve().parents[1] / "config" / "config.json"


def test_repository_default_context_is_memory_aware(monkeypatch) -> None:
    _clean_env(monkeypatch)

    config = AppConfig.load()

    assert config.context_strategy == "memory-tail-window"


def test_config_directory_merges_shared_and_section_files(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {
            "model": "legacy",
            "event_transport": {"provider": "custom-transport"},
            "session": {"store": "jsonl", "id": "shared"},
            "memory": {"store": "sqlite"},
            "embedding": {"provider": "debug"},
            "context": {"maxTokens": 1000, "strategy": "tail-window"},
            "mcp": {"preload": ["time"]},
            "skill": {"preload": ["code-review"]},
        },
    )
    _write(config_dir / "model.json", {"model": "openai"})
    _write(config_dir / "event.json", {"handlerTimeout": 2.5})
    _write(config_dir / "event_transport.json", {"provider": "in-process"})
    _write(
        config_dir / "retry.json",
        {
            "default": "transient",
            "maxAttempts": 4,
            "maxDelay": 1.5,
            "totalTimeout": 12.0,
            "operations": {"model.complete": "no-retry"},
        },
    )
    _write(
        config_dir / "session.json",
        {"store": "inmemory", "id": "cli", "compaction": "rolling-summary"},
    )
    _write(
        config_dir / "memory.json",
        {
            "store": "jsonl",
            "index": "lexical",
            "policy": "default",
            "extractor": "explicit",
            "scope": "agent",
            "ownerId": "owner-a",
            "agentId": "agent-a",
            "tenantId": "tenant-a",
        },
    )
    _write(config_dir / "embedding.json", {"provider": "openai-embedding"})
    _write(config_dir / "context.json", {"maxTokens": 12345, "strategy": "compact"})
    _write(config_dir / "mcp.json", {"preload": ["time", "math"]})
    _write(
        config_dir / "skill.json",
        {
            "preload": ["code-review"],
            "maxContentBytes": 1000,
            "maxPreloadBytes": 2000,
            "maxResources": 4,
            "maxResourceBytes": 500,
            "maxResourceTotalBytes": 1500,
            "maxDescriptionBytes": 120,
            "maxResourceDescriptionBytes": 60,
            "maxListingBytes": 240,
            "agent": "reviewer",
            "permissions": {
                "*": "allow",
                "dangerous-*": "deny",
            },
            "agents": {
                "reviewer": {
                    "permissions": {
                        "dangerous-*": "ask",
                    }
                }
            },
            "nameOnly": ["very-long-*"],
        },
    )

    config = AppConfig.load(config_dir)

    assert config.model == "openai"
    assert config.event_transport == "in-process"
    assert config.event_handler_timeout == 2.5
    assert config.retry_policy == "transient"
    assert config.retry_operation_policies == (("model.complete", "no-retry"),)
    assert config.retry_max_attempts == 4
    assert config.retry_max_delay == 1.5
    assert config.retry_total_timeout == 12.0
    assert config.session_store == "inmemory"
    assert config.session_id == "cli"
    assert config.session_compaction == "rolling-summary"
    assert config.memory_store == "jsonl"
    assert config.memory_index == "lexical"
    assert config.memory_policy == "default"
    assert config.memory_extractor == "explicit"
    assert config.memory_scope == "agent"
    assert config.memory_owner_id == "owner-a"
    assert config.memory_agent_id == "agent-a"
    assert config.memory_tenant_id == "tenant-a"
    assert config.embedding_provider == "openai-embedding"
    assert config.context_max_tokens == 12345
    assert config.context_strategy == "compact"
    assert config.mcp_preload == ("time", "math")
    assert config.skill_preload == ("code-review",)
    assert config.skill_max_content_bytes == 1000
    assert config.skill_max_preload_bytes == 2000
    assert config.skill_max_resources == 4
    assert config.skill_max_resource_bytes == 500
    assert config.skill_max_resource_total_bytes == 1500
    assert config.skill_max_description_bytes == 120
    assert config.skill_max_resource_description_bytes == 60
    assert config.skill_max_listing_bytes == 240
    assert config.skill_agent == "reviewer"
    assert config.skill_permissions == (
        ("*", "allow"),
        ("dangerous-*", "deny"),
    )
    assert config.skill_agent_permissions == (("reviewer", (("dangerous-*", "ask"),)),)
    assert config.skill_name_only == ("very-long-*",)
    assert config.config_dir == config_dir.resolve()

    monkeypatch.setenv("AGENT_MODEL", "deepseek")
    monkeypatch.setenv("EVENT_TRANSPORT", "custom-transport")
    monkeypatch.setenv("EVENT_HANDLER_TIMEOUT", "3.5")
    monkeypatch.setenv("RETRY_POLICY", "no-retry")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("RETRY_MAX_DELAY", "0.5")
    monkeypatch.setenv("RETRY_TOTAL_TIMEOUT", "9.0")
    monkeypatch.setenv("CONTEXT_STRATEGY", "tail-window")
    monkeypatch.setenv("SESSION_COMPACTION", "")
    monkeypatch.setenv("MEMORY_INDEX", "")
    monkeypatch.setenv("MEMORY_POLICY", "strict")
    monkeypatch.setenv("MEMORY_EXTRACTOR", "off")
    monkeypatch.setenv("MEMORY_SCOPE", "user")
    monkeypatch.setenv("MEMORY_OWNER_ID", "owner-b")
    monkeypatch.setenv("MEMORY_AGENT_ID", "agent-b")
    monkeypatch.setenv("MEMORY_TENANT_ID", "tenant-b")
    monkeypatch.setenv("MCP_PRELOAD", "filesystem")
    monkeypatch.setenv("SKILL_PRELOAD", "commit-message")
    monkeypatch.setenv("SKILL_MAX_CONTENT_BYTES", "3000")
    monkeypatch.setenv("SKILL_MAX_DESCRIPTION_BYTES", "700")
    monkeypatch.setenv("SKILL_MAX_LISTING_BYTES", "900")
    monkeypatch.setenv("SKILL_AGENT", "reviewer")
    monkeypatch.setenv("SKILL_NAME_ONLY", "very-long-*")
    config = AppConfig.load(config_dir)
    assert config.model == "deepseek"
    assert config.event_transport == "custom-transport"
    assert config.event_handler_timeout == 3.5
    assert config.retry_policy == "no-retry"
    assert config.retry_max_attempts == 5
    assert config.retry_max_delay == 0.5
    assert config.retry_total_timeout == 9.0
    assert config.context_strategy == "tail-window"
    assert config.session_compaction is None
    assert config.memory_index is None
    assert config.memory_policy == "strict"
    assert config.memory_extractor == "off"
    assert config.memory_scope == "user"
    assert config.memory_owner_id == "owner-b"
    assert config.memory_agent_id == "agent-b"
    assert config.memory_tenant_id == "tenant-b"
    assert config.mcp_preload == ("filesystem",)
    assert config.skill_preload == ("commit-message",)
    assert config.skill_max_content_bytes == 3000
    assert config.skill_max_description_bytes == 700
    assert config.skill_max_listing_bytes == 900
    assert config.skill_agent == "reviewer"
    assert config.skill_name_only == ("very-long-*",)


def test_plugin_config_is_loaded_by_kind_and_name(tmp_path) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {})
    _write(config_dir / "plugins" / "hook" / "permission.json", {"default": "deny"})
    _write(config_dir / "plugins" / "skill" / "quality.json", {"feature": "on"})
    config = AppConfig.load(config_dir)

    assert config.plugin_config("hook", "permission") == {"default": "deny"}
    assert config.plugin_config("hook", "missing") == {}
    # Package contribution names fall back to the package-level config.
    assert config.plugin_config("skill", "quality--lint") == {"feature": "on"}
    with pytest.raises(ValueError, match="invalid plugin config kind"):
        config.plugin_config("../hook", "permission")


def test_local_model_config_overrides_tracked_plugin_config(tmp_path) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {})
    _write(
        config_dir / "plugins" / "model" / "deepseek.json",
        {"baseUrl": "https://tracked.example", "model": "tracked-model"},
    )
    save_model_config_overlay(
        config_dir,
        "deepseek",
        {
            "apiKey": "local-secret",
            "baseUrl": "http://127.0.0.1:9000/v1",
            "model": "local-model",
        },
    )

    config = AppConfig.load(config_dir)

    assert config.plugin_config("model", "deepseek") == {
        "baseUrl": "http://127.0.0.1:9000/v1",
        "model": "local-model",
        "apiKey": "local-secret",
    }
    assert (tmp_path / "data" / "model-configs.json").is_file()


def test_local_mcp_preload_overrides_tracked_config_and_env(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {})
    _write(config_dir / "mcp.json", {"preload": ["time"]})
    monkeypatch.setenv("MCP_PRELOAD", "math")

    assert load_local_mcp_preload(config_dir) is None
    assert AppConfig.load(config_dir).mcp_preload == ("math",)

    saved = save_local_mcp_preload(config_dir, ["filesystem", "time"])
    assert saved == ("filesystem", "time")
    assert load_local_mcp_preload(config_dir) == ("filesystem", "time")
    assert AppConfig.load(config_dir).mcp_preload == ("filesystem", "time")

    clear_local_mcp_preload(config_dir)
    assert load_local_mcp_preload(config_dir) is None
    assert AppConfig.load(config_dir).mcp_preload == ("math",)


def test_memory_retriever_config_and_legacy_index_compatibility(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {"memory": {"store": "sqlite", "retriever": "lexical"}},
    )

    config = AppConfig.load(config_dir)

    assert config.memory_index is None
    assert config.memory_retriever == "lexical"


def test_memory_legacy_index_disables_default_retriever(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {"memory": {"store": "sqlite", "index": "recent"}},
    )

    config = AppConfig.load(config_dir)

    assert config.memory_index == "recent"
    assert config.memory_retriever is None


def test_memory_index_and_retriever_cannot_be_configured_together(
    tmp_path,
    monkeypatch,
) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {"memory": {"store": "sqlite", "retriever": "lexical"}},
    )
    monkeypatch.setenv("MEMORY_INDEX", "recent")

    with pytest.raises(ValueError, match="not both"):
        AppConfig.load(config_dir)


def test_model_routing_config_is_loaded_and_env_overrides_names(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {"model": "deepseek"},
    )
    _write(
        config_dir / "model.json",
        {
            "model": "deepseek",
            "router": "static",
            "fallback": ["openai"],
            "keepWarm": ["deepseek"],
            "routes": {
                "coding": ["deepseek", "openai"],
                "summary": ["openai"],
            },
        },
    )

    config = AppConfig.load(config_dir)

    assert config.model_router == "static"
    assert config.model_fallback == ("openai",)
    assert config.model_keep_warm == ("deepseek",)
    assert dict(config.model_routes) == {
        "coding": ("deepseek", "openai"),
        "summary": ("openai",),
    }

    monkeypatch.setenv("MODEL_ROUTER", "custom")
    monkeypatch.setenv("MODEL_FALLBACK", "openai,deepseek")
    monkeypatch.setenv("MODEL_KEEP_WARM", "openai")
    config = AppConfig.load(config_dir)
    assert config.model_router == "custom"
    assert config.model_fallback == ("openai", "deepseek")
    assert config.model_keep_warm == ("openai",)


def test_model_routing_rejects_unknown_shape(tmp_path) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {"model": "deepseek"})
    _write(config_dir / "model.json", {"model": "deepseek", "routes": ["coding"]})

    with pytest.raises(ValueError, match="model.routes"):
        AppConfig.load(config_dir)


def test_config_missing_file_uses_defaults(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config = AppConfig.load(tmp_path / "not-exists.json")
    assert config.model == "deepseek"
    assert config.event_transport == "in-process"
    assert config.event_handler_timeout is None
    assert config.retry_policy is None
    assert config.retry_operation_policies == ()
    assert config.retry_max_attempts == 3
    assert config.retry_max_delay == 2.0
    assert config.retry_total_timeout == 30.0
    assert config.session_store == "jsonl"
    assert config.session_compaction is None
    assert config.memory_store == "sqlite"
    assert config.memory_retriever == "store-native"
    assert config.memory_extractor == "explicit"
    assert config.memory_scope == "user"
    assert config.memory_owner_id == ""
    assert config.memory_agent_id == ""
    assert config.memory_tenant_id == ""
    assert config.context_max_tokens == 20000
    assert config.context_strategy == "tail-window"
    assert config.mcp_preload == ()
    assert config.skill_preload == ()
    assert config.skill_max_content_bytes == 262144
    assert config.skill_max_preload_bytes == 524288
    assert config.skill_max_resources == 32
    assert config.skill_max_resource_bytes == 262144
    assert config.skill_max_resource_total_bytes == 1048576
    assert config.skill_max_description_bytes == 512
    assert config.skill_max_resource_description_bytes == 256
    assert config.skill_max_listing_bytes == 8192
    assert config.skill_agent == "default"
    assert config.skill_permissions == ()
    assert config.skill_agent_permissions == ()
    assert config.skill_name_only == ()


def test_config_invalid_type_fails_fast(tmp_path) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {"context": {"maxTokens": "many"}})
    with pytest.raises(ValueError, match="maxTokens"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"context": {"strategy": 123}})
    with pytest.raises(ValueError, match="strategy"):
        AppConfig.load(config_dir)


def test_config_rejects_invalid_mcp_preload(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"

    _write(config_dir / "config.json", {"mcp": {"preload": "time"}})
    with pytest.raises(ValueError, match="mcp.preload"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"mcp": {"preload": ["bad name"]}})
    with pytest.raises(ValueError, match="invalid MCP plugin name"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"mcp": {"preload": ["time", "time"]}})
    with pytest.raises(ValueError, match="duplicate"):
        AppConfig.load(config_dir)

    monkeypatch.setenv("MCP_PRELOAD", "time,,math")
    _write(config_dir / "config.json", {"mcp": {"preload": ["time"]}})
    with pytest.raises(ValueError, match="MCP_PRELOAD"):
        AppConfig.load(config_dir)


def test_retry_config_rejects_invalid_values(tmp_path) -> None:
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {"retry": {"maxAttempts": 0}})
    with pytest.raises(ValueError, match="retry.maxAttempts"):
        AppConfig.load(config_dir)

    _write(
        config_dir / "config.json",
        {"retry": {"operations": {"model.complete": "bad name"}}},
    )
    with pytest.raises(ValueError, match="retry.operations"):
        AppConfig.load(config_dir)


def test_config_rejects_invalid_skill_preload_and_budgets(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"

    _write(config_dir / "config.json", {"skill": {"preload": "code-review"}})
    with pytest.raises(ValueError, match="skill.preload"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"skill": {"preload": ["bad name"]}})
    with pytest.raises(ValueError, match="invalid skill plugin name"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"skill": {"preload": ["one", "one"]}})
    with pytest.raises(ValueError, match="duplicate skill plugin name"):
        AppConfig.load(config_dir)

    monkeypatch.setenv("SKILL_PRELOAD", "one,,two")
    _write(config_dir / "config.json", {"skill": {"preload": ["one"]}})
    with pytest.raises(ValueError, match="SKILL_PRELOAD"):
        AppConfig.load(config_dir)

    monkeypatch.delenv("SKILL_PRELOAD")
    _write(config_dir / "config.json", {"skill": {"maxContentBytes": 0}})
    with pytest.raises(ValueError, match="maxContentBytes"):
        AppConfig.load(config_dir)

    _write(config_dir / "config.json", {"skill": {"maxDescriptionBytes": 0}})
    with pytest.raises(ValueError, match="maxDescriptionBytes"):
        AppConfig.load(config_dir)


def test_config_rejects_invalid_skill_permissions(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {"skill": {"permissions": {"*": "maybe"}}},
    )
    with pytest.raises(ValueError, match="skill.permissions"):
        AppConfig.load(config_dir)

    _write(
        config_dir / "config.json",
        {"skill": {"agents": {"reviewer": {"permissions": {"*": "deny"}}}}},
    )
    config = AppConfig.load(config_dir)
    assert config.skill_agent_permissions == (("reviewer", (("*", "deny"),)),)


def test_config_rejects_invalid_session_compaction(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(config_dir / "config.json", {"session": {"compaction": 123}})

    with pytest.raises(ValueError, match="session.compaction"):
        AppConfig.load(config_dir)
