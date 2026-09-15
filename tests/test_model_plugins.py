# tests/test_model_plugins.py —— 仓库自带的模型插件示例（不联网、不读 .env）
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.types import Message
from plugins.context import PluginContext
from plugins.loader import load_model_plugins
from plugins.model.deepseek.model import create_model as create_deepseek
from plugins.model.sub2api.model import create_model as create_sub2api

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _by_name() -> dict[str, object]:
    return {plugin.manifest.name: plugin for plugin in load_model_plugins(REPO_PLUGINS)}


def test_repo_contains_deepseek_and_openai_model_plugins() -> None:
    assert {"deepseek", "openai", "sub2api"} <= set(_by_name())


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


@pytest.mark.asyncio
async def test_deepseek_model_reads_plugin_config_and_closes_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("plugins.model.deepseek.model.AsyncOpenAI", FakeClient)
    context = PluginContext.create(
        name="deepseek",
        kind="model",
        directory=Path("."),
        config={
            "apiKey": "config-key",
            "baseUrl": "https://example.invalid",
            "model": "configured-model",
            "timeout": 12,
            "maxRetries": 1,
        },
    )

    model = create_deepseek(Path("."), context=context)
    await model.stop()

    assert captured["client"] == {
        "api_key": "config-key",
        "base_url": "https://example.invalid",
        "timeout": 12.0,
        "max_retries": 1,
    }
    assert model._model == "configured-model"
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_sub2api_model_reads_config_and_closes_responses_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs

        async def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("plugins.model.sub2api.model.AsyncOpenAI", FakeClient)
    context = PluginContext.create(
        name="sub2api",
        kind="model",
        directory=Path("."),
        config={
            "apiKey": "config-key",
            "baseUrl": "http://gateway.invalid/v1",
            "model": "deepseek-v4-flash",
            "timeout": 12,
            "maxRetries": 1,
            "disableResponseStorage": False,
        },
    )

    model = create_sub2api(Path("."), context=context)
    await model.stop()

    assert captured["client"] == {
        "api_key": "config-key",
        "base_url": "http://gateway.invalid/v1",
        "timeout": 12.0,
        "max_retries": 1,
    }
    assert model._model == "deepseek-v4-flash"
    assert model._disable_response_storage is False
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_sub2api_complete_uses_responses_stream(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured["payload"] = kwargs

            async def events():
                yield SimpleNamespace(type="response.output_text.delta", delta="hello")

            return events()

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.responses = FakeResponses()

        async def close(self) -> None:
            return None

    monkeypatch.setattr("plugins.model.sub2api.model.AsyncOpenAI", FakeClient)
    context = PluginContext.create(
        name="sub2api",
        kind="model",
        directory=Path("."),
        config={
            "apiKey": "key",
            "baseUrl": "http://gateway.invalid/v1",
            "model": "deepseek-v4-flash",
            "disableResponseStorage": True,
        },
    )
    model = create_sub2api(Path("."), context=context)

    response = await model.complete(
        [Message(role="system", content="rules"), Message(role="user", content="hi")],
        [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ],
    )

    assert response.content == "hello"
    payload = captured["payload"]
    assert payload["instructions"] == "rules"
    assert payload["input"] == [{"role": "user", "content": "hi"}]
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["tools"] == [
        {"type": "function", "name": "echo", "parameters": {"type": "object"}}
    ]
