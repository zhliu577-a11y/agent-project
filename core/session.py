# core/session.py —— 会话存储接口（短期记忆的后端契约）
from abc import ABC, abstractmethod
from typing import Any

from core.types import Message


class SessionStore(ABC):
    """会话存储插件必须实现的接口：按 session_id 存取消息历史。"""

    @abstractmethod
    async def load(self, session_id: str) -> list[Message]:
        """读取会话历史；没有该会话时返回空列表。"""
        ...

    @abstractmethod
    async def save(self, session_id: str, messages: list[Message]) -> None:
        """保存会话历史（覆盖语义，由实现决定是否全量覆写）。"""
        ...

    @abstractmethod
    async def load_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """读取最近一次运行的轻量状态快照；没有则返回 None。"""
        ...

    @abstractmethod
    async def save_checkpoint(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """保存轻量状态快照（turn/stop_reason/state 等，JSON 安全）。"""
        ...

    @abstractmethod
    async def delete_checkpoint(self, session_id: str) -> None:
        """删除快照（清空会话时一并清理）。"""
        ...
