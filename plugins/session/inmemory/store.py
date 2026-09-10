# plugins/session/inmemory/store.py —— 会话存储插件示例：内存后端
#
# 演示“换存储 = 换插件目录 + 改 SESSION_STORE”。本后端不落盘，
# 进程重启即失，适合测试或临时会话。
import copy

from core.session import SessionStore
from core.types import Message, message_from_dict, message_to_dict


class InMemorySessionStore(SessionStore):
    """把历史放在进程内字典里；通过序列化做拷贝，避免调用方意外改坏数据。"""

    def __init__(self) -> None:
        self._data: dict[str, list[Message]] = {}
        self._checkpoints: dict[str, dict] = {}

    async def load(self, session_id: str) -> list[Message]:
        return [_copy(m) for m in self._data.get(session_id, [])]

    async def save(self, session_id: str, messages: list[Message]) -> None:
        self._data[session_id] = [_copy(m) for m in messages]

    async def load_checkpoint(self, session_id: str) -> dict | None:
        return copy.deepcopy(self._checkpoints.get(session_id))

    async def save_checkpoint(self, session_id: str, snapshot: dict) -> None:
        self._checkpoints[session_id] = copy.deepcopy(snapshot)

    async def delete_checkpoint(self, session_id: str) -> None:
        self._checkpoints.pop(session_id, None)


def _copy(message: Message) -> Message:
    return message_from_dict(message_to_dict(message))


def create_store(plugin_dir):
    """插件工厂：返回内存会话存储（无配置、无副作用）。"""
    return InMemorySessionStore()
