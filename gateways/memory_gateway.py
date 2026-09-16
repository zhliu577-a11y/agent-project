# gateways/memory_gateway.py —— 长期记忆网关：语义笔记的读写入口
#
# 持有激活的 MemoryStore 插件；模型通过 remember / recall / forget
# 三个内核工具访问，工具同样过权限/审计钩子。
from typing import Any

from core.errors import ToolError, boundary
from core.events import Event, EventPublisher
from core.memory import MemoryNote, MemoryStore
from core.tool import Tool
from core.tracing import current_trace_id


class MemoryGateway:
    """长期记忆网关：跨会话语义笔记的增删查。"""

    def __init__(self, store: MemoryStore, events: EventPublisher | None = None) -> None:
        self._store = store
        self._events = events

    async def _emit(self, name: str, **payload: object) -> None:
        if self._events is None:
            return
        await self._events.publish(
            Event(name=name, payload=dict(payload), trace_id=current_trace_id())
        )

    @boundary("写入长期记忆失败", fallback=ToolError)
    async def remember(self, content: str, tags: list[str] | None = None) -> MemoryNote:
        note = await self._store.add_note(content, tags or [])
        await self._emit("memory.write", note_id=note.id, tags=note.tags)
        return note

    @boundary("检索长期记忆失败", fallback=ToolError)
    async def recall(self, query: str = "") -> list[MemoryNote]:
        return await self._store.search_notes(query)

    @boundary("删除长期记忆失败", fallback=ToolError)
    async def forget(self, note_id: str) -> bool:
        removed = await self._store.delete_note(note_id)
        if removed:
            await self._emit("memory.delete", note_id=note_id)
        return removed

    @boundary("更新长期记忆失败", fallback=ToolError)
    async def update(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        note = await self._store.update_note(note_id, content, tags)
        if note is not None:
            await self._emit("memory.update", note_id=note.id, tags=note.tags)
        return note


def _tags_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": "标签，便于后续按主题检索",
    }


class RememberTool(Tool):
    name = "remember"
    description = (
        "把值得跨会话长期记住的事实、偏好或结论写入长期记忆；"
        "适合用户偏好、项目约定、重要结论，不要记临时过程。"
    )

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容"},
                "tags": _tags_schema(),
            },
            "required": ["content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        note = await self._gateway.remember(kwargs["content"], kwargs.get("tags"))
        return f"已记住 [{note.id}]: {note.content}"


class RecallTool(Tool):
    name = "recall"
    description = (
        "从长期记忆里搜索相关内容（匹配正文或标签，不区分大小写）；"
        "不传 query 返回全部笔记。返回条目带 id，可用于 forget。"
    )

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词，可省略"}},
        }

    async def execute(self, **kwargs: Any) -> str:
        notes = await self._gateway.recall(kwargs.get("query", ""))
        if not notes:
            return "（长期记忆里没有匹配的笔记）"
        return "\n".join(
            f"- [{note.id}] {note.content}"
            + (f"  [tags: {'、'.join(note.tags)}]" if note.tags else "")
            for note in notes
        )


class ForgetTool(Tool):
    name = "forget"
    description = "按 recall 返回的笔记 id 删除一条长期记忆。"

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"note_id": {"type": "string", "description": "要删除的笔记 id"}},
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        removed = await self._gateway.forget(kwargs["note_id"])
        return f"已删除笔记 {kwargs['note_id']}" if removed else f"未找到笔记 {kwargs['note_id']}"


class UpdateNoteTool(Tool):
    name = "update_note"
    description = (
        "更新一条已有记忆笔记的正文或标签（note_id 来自 recall 输出）；"
        "content 与 tags 至少提供一项，未提供的字段保持不变。"
    )

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "要更新的笔记 id"},
                "content": {"type": "string", "description": "新的正文（可省略）"},
                "tags": _tags_schema(),
            },
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        note_id = kwargs["note_id"]
        content = kwargs.get("content")
        tags = kwargs.get("tags")
        if content is None and tags is None:
            return "请至少提供 content 或 tags 之一"
        note = await self._gateway.update(note_id, content, tags)
        if note is None:
            return f"未找到笔记 {note_id}"
        return f"已更新 [{note.id}]: {note.content}"
