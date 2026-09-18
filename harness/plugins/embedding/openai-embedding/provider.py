# plugins/embedding/openai-embedding/provider.py —— 嵌入提供方：OpenAI API
#
# 通过 OpenAI Embeddings 接口把文本变成真实语义向量；
# 实例化时读取 OPENAI_API_KEY / OPENAI_EMBEDDING_MODEL 等环境变量。
import os

from openai import AsyncOpenAI

from core.embedding import EmbeddingProvider


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """把一批文本转成 text-embedding-3-small（可配）向量。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY，请在 .env 中配置后再运行")
        self._model = model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=float(os.getenv("OPENAI_TIMEOUT", "60")),
            max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "3")),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=self._model, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]


def create_provider(plugin_dir):
    """插件工厂：返回 OpenAI 嵌入（实例化时读取 OPENAI_* 环境变量）。"""
    return OpenAIEmbeddingProvider()
