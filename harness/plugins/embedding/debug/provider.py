# plugins/embedding/debug/provider.py —— 嵌入提供方示例：确定性哈希
#
# 不调用任何外部服务：把字符散列到固定维度并做 L2 归一化。
# 向量能反映“字符重叠度”，可离线跑通语义检索链路，但本身无语义。
import math

from core.embedding import EmbeddingProvider

_DIMENSION = 32


class DebugEmbeddingProvider(EmbeddingProvider):
    """确定性字符袋向量：同文本结果完全一致，便于测试。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [0.0] * _DIMENSION
        for ch in text:
            values[ord(ch) % _DIMENSION] += 1.0
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values


def create_provider(plugin_dir):
    """插件工厂：返回确定性调试嵌入（无配置、无副作用）。"""
    return DebugEmbeddingProvider()
