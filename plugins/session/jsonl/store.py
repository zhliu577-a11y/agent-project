# plugins/session/jsonl/store.py —— 会话存储插件示例：JSONL 文件后端
#
# 每个会话一个 <SESSION_ID>.jsonl，每条消息一行 JSON；数据目录由
# SESSION_DATA_DIR 指定（默认项目根 .sessions/，已 gitignore）。
import json
import os
from pathlib import Path

from core.session import SessionStore
from core.types import Message, message_from_dict, message_to_dict


class JsonlSessionStore(SessionStore):
    """把消息历史按 JSONL 全量写入 <data_dir>/<session_id>.jsonl。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _path(self, session_id: str) -> Path:
        return self._data_dir / f"{session_id}.jsonl"

    async def load(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            messages.append(message_from_dict(json.loads(line)))
        return messages

    async def save(self, session_id: str, messages: list[Message]) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(
            json.dumps(message_to_dict(message), ensure_ascii=False) for message in messages
        )
        path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    def _checkpoint_path(self, session_id: str) -> Path:
        return self._data_dir / f"{session_id}.checkpoint.json"

    async def load_checkpoint(self, session_id: str) -> dict | None:
        path = self._checkpoint_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    async def save_checkpoint(self, session_id: str, snapshot: dict) -> None:
        path = self._checkpoint_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    async def delete_checkpoint(self, session_id: str) -> None:
        self._checkpoint_path(session_id).unlink(missing_ok=True)


def create_store(plugin_dir):
    """插件工厂：返回 JSONL 会话存储（数据目录读 SESSION_DATA_DIR）。"""
    default_dir = Path(plugin_dir).resolve().parents[2] / ".sessions"
    data_dir = Path(os.getenv("SESSION_DATA_DIR", str(default_dir)))
    return JsonlSessionStore(data_dir)
