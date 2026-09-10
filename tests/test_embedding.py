# tests/test_embedding.py —— 嵌入提供方插件（不联网）
from pathlib import Path

import pytest

from plugins.embedding.debug.provider import DebugEmbeddingProvider
from plugins.loader import load_embedding_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


@pytest.mark.asyncio
async def test_debug_provider_is_deterministic_and_normalized() -> None:
    provider = DebugEmbeddingProvider()
    first = await provider.embed_one("项目用 ruff")
    second = await provider.embed_one("项目用 ruff")
    assert first == second  # 确定性
    assert len(first) == 32

    similarity = sum(a * b for a, b in zip(first, second, strict=True))
    assert abs(similarity - 1.0) < 1e-9  # 同文本余弦 ≈ 1


def test_repo_offers_debug_and_openai_embedding_providers() -> None:
    names = {plugin.manifest.name for plugin in load_embedding_plugins(REPO_PLUGINS)}
    assert {"debug", "openai-embedding"} <= names


def test_openai_embedding_provider_is_lazy_and_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    plugin = next(
        p for p in load_embedding_plugins(REPO_PLUGINS) if p.manifest.name == "openai-embedding"
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        plugin.create()
