# tests/test_config.py —— 配置管理：config.json + 环境变量覆盖
import pytest

from core.config import AppConfig

_CONFIG_KEYS = (
    "AGENT_MODEL",
    "SESSION_STORE",
    "SESSION_ID",
    "MEMORY_STORE",
    "EMBEDDING_PROVIDER",
    "CONTEXT_MAX_TOKENS",
    "CONTEXT_STRATEGY",
)


def _clean_env(monkeypatch) -> None:
    for key in _CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_config_file_values_and_env_override(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        '{"model": "openai", "session": {"store": "inmemory", "id": "cli"},'
        ' "memory": {"store": "jsonl"}, "embedding": {"provider": "debug"},'
        ' "context": {"maxTokens": 12345, "strategy": "compact"}}',
        encoding="utf-8",
    )
    config = AppConfig.load(cfg_path)
    assert config.model == "openai"
    assert config.session_store == "inmemory"
    assert config.session_id == "cli"
    assert config.memory_store == "jsonl"
    assert config.context_max_tokens == 12345
    assert config.context_strategy == "compact"

    monkeypatch.setenv("AGENT_MODEL", "deepseek")
    monkeypatch.setenv("CONTEXT_STRATEGY", "tail-window")
    config = AppConfig.load(cfg_path)
    assert config.model == "deepseek"  # 环境变量覆盖 config.json
    assert config.context_strategy == "tail-window"


def test_config_missing_file_uses_defaults(tmp_path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    config = AppConfig.load(tmp_path / "not-exists.json")
    assert config.model == "deepseek"
    assert config.session_store == "jsonl"
    assert config.memory_store == "sqlite"
    assert config.context_max_tokens == 20000
    assert config.context_strategy == "tail-window"


def test_config_invalid_type_fails_fast(tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"context": {"maxTokens": "many"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="maxTokens"):
        AppConfig.load(cfg_path)

    cfg_path.write_text('{"context": {"strategy": 123}}', encoding="utf-8")
    with pytest.raises(ValueError, match="strategy"):
        AppConfig.load(cfg_path)
