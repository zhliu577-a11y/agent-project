# core/tool.py —— 工具接口：所有工具插件都要实现这个异步接口
from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]: ...      # JSON Schema

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any: ...
