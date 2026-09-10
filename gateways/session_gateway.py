# gateways/session_gateway.py —— 会话网关：短期记忆的读写入口
#
# 持有激活的 SessionStore 插件与当前会话 id，为 main/chat 提供
# 历史加载与保存；存储后端来自 plugins/session/*（SESSION_STORE 选定）。
from typing import Any

from core.session import SessionStore
from core.types import Message


class SessionGateway:
    """会话网关：按 session_id 存取消息历史（短期记忆）。"""

    def __init__(self, store: SessionStore, session_id: str = "default") -> None:
        self._store = store
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    async def load_history(self) -> list[Message]:
        """读取本会话历史；没有历史时返回空列表。"""
        return await self._store.load(self._session_id)

    async def save_history(self, messages: list[Message]) -> None:
        """保存本会话历史（不含 system 消息，由调用方裁剪）。"""
        await self._store.save(self._session_id, messages)

    async def load_checkpoint(self) -> dict[str, Any] | None:
        return await self._store.load_checkpoint(self._session_id)

    async def save_checkpoint(self, snapshot: dict[str, Any]) -> None:
        await self._store.save_checkpoint(self._session_id, snapshot)

    async def delete_checkpoint(self) -> None:
        await self._store.delete_checkpoint(self._session_id)
