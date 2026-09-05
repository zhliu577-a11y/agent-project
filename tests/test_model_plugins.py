# tests/test_model_plugins.py —— 仓库自带的模型插件示例（不联网、不读 .env）
from pathlib import Path

import pytest

from plugins.loader import load_model_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _by_name() -> dict[str, object]:
    return {plugin.manifest.name: plugin for plugin in load_model_plugins(REPO_PLUGINS)}


def test_repo_contains_deepseek_and_openai_model_plugins() -> None:
    assert {"deepseek", "openai"} <= set(_by_name())


def test_openai_plugin_is_lazy_and_requires_openai_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plugin = _by_name()["openai"]
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        plugin.create()


def test_deepseek_plugin_is_lazy_and_requires_deepseek_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    plugin = _by_name()["deepseek"]
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        plugin.create()
