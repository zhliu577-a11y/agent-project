# core/embedding.py —— 嵌入提供方接口（把文本变成向量，供语义检索使用）
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """embedding 插件必须实现的接口：批量把文本转成向量。"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """输入一批文本，返回同序的向量列表。"""
        ...

    async def embed_one(self, text: str) -> list[float]:
        """单条文本向量（默认实现：走批量接口）。"""
        vectors = await self.embed([text])
        return vectors[0]
