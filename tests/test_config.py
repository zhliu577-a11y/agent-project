# tests/test_config.py - merged config directory and env overrides
import json
from pathlib import Path

import pytest

from config import DEFAULT_CONFIG_PATH, AppConfig

_CONFIG_KEYS = (
    "AGENT_MODEL",
    "SESSION_STORE",
    "SESSION_ID",
    "MEMORY_STORE",
    "EMBEDDING_PROVIDER",
    "CONTEXT_MAX_TOKENS",
    "CONTEXT_STRATEGY",
    "MCP_PRELOAD",
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


def test_config_directory_merges_shared_and_section_files(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    _write(
        config_dir / "config.json",
        {
            "model": "legacy",
            "session": {"store": "jsonl", "id": "shared"},
            "memory": {"store": "sqlite"},
            "embedding": {"provider": "debug"},
            "context": {"maxTokens": 1000, "strategy": "tail-window"},
            "mcp": {"preload": ["time"]},
        },
    )
    _write(config_dir / "model.json", {"model": "openai"})
    _write(config_dir / "session.json", {"store": "inmemory", "id": "cli"})
    _write(config_dir / "memory.json", {"store": "jsonl"})
    _write(config_dir / "embedding.json", {"provider": "openai-embedding"})
    _write(config_dir / "context.json", {"maxTokens": 12345, "strategy": "compact"})
    _write(config_dir / "mcp.json", {"preload": ["time", "math"]})

    config = AppConfig.load(config_dir)

    assert config.model == "openai"
    assert config.session_store == "inmemory"
    assert config.session_id == "cli"
    assert config.memory_store == "jsonl"
    assert config.embedding_provider == "openai-embedding"
    assert config.context_max_tokens == 12345
    assert config.context_strategy == "compact"
    assert config.mcp_preload == ("time", "math")
    assert config.config_dir == config_dir.resolve()

    monkeypatch.setenv("AGENT_MODEL", "deepseek")
    monkeypatch.setenv("CONTEXT_STRATEGY", "tail-window")
    monkeypatch.setenv("MCP_PRELOAD", "filesystem")
    config = AppConfig.load(config_dir)
    assert config.model == "deepseek"
    assert config.context_strategy == "tail-window"
    assert config.mcp_preload == ("filesystem",)


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
    assert config.session_store == "jsonl"
    assert config.memory_store == "sqlite"
    assert config.context_max_tokens == 20000
    assert config.context_strategy == "tail-window"
    assert config.mcp_preload == ()


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
