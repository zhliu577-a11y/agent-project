# core/memory.py —— 长期记忆接口（语义笔记，跨会话）
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoryNote:
    id: str
    content: str
    tags: list[str]
    created_at: str


def note_to_dict(note: MemoryNote) -> dict[str, Any]:
    return {
        "id": note.id,
        "content": note.content,
        "tags": note.tags,
        "created_at": note.created_at,
    }


def note_from_dict(raw: dict[str, Any]) -> MemoryNote:
    return MemoryNote(
        id=raw["id"],
        content=raw.get("content", ""),
        tags=list(raw.get("tags", [])),
        created_at=raw.get("created_at", ""),
    )


class MemoryStore(ABC):
    """长期记忆存储插件必须实现的接口：语义笔记的增删查。"""

    @abstractmethod
    async def list_notes(self) -> list[MemoryNote]:
        """返回全部笔记（按时间正序）。"""
        ...

    @abstractmethod
    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        """新增一条笔记，返回带 id/时间的完整记录。"""
        ...

    @abstractmethod
    async def delete_note(self, note_id: str) -> bool:
        """删除指定笔记；不存在返回 False。"""
        ...

    @abstractmethod
    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        """更新笔记正文/标签（None 表示该字段不变）；不存在返回 None。"""
        ...

    @abstractmethod
    async def search_notes(self, query: str) -> list[MemoryNote]:
        """按内容/标签做不区分大小写的子串搜索；空 query 返回全部。"""
        ...
