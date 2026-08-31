# core/tool.py —— 工具接口：所有工具插件都要实现这个抽象类
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
    def parameters(self) -> dict: ...      # JSON Schema

    @abstractmethod
    def execute(self, **kwargs) -> Any: ...