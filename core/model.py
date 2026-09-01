# core/model.py —— 模型适配器接口
from abc import ABC, abstractmethod

from core.types import Message, ModelResponse


class ModelAdapter(ABC):
    """所有模型后端（OpenAI、DeepSeek、本地 vLLM 等）都要实现此接口。"""

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
    ) -> ModelResponse:
        """把消息历史和工具清单发给模型，返回模型的回复。"""
        ...
