# core/model.py —— 模型适配器接口（异步）
from abc import ABC, abstractmethod
from collections.abc import Callable

from core.types import Message, ModelResponse


class ModelAdapter(ABC):
    """所有模型后端（OpenAI、DeepSeek、本地 vLLM 等）都要实现此接口。"""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
        on_token: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """把消息历史和工具清单发给模型，返回模型的回复。

        on_token：可选回调；模型以流式返回纯文本时，每个增量都会调用它。
        模型决定调用工具时不会触发（工具调用增量静默收集）。
        """
        ...
