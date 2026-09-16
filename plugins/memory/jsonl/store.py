# plugins/memory/jsonl/store.py —— 长期记忆插件示例：JSONL 文件后端
#
# 笔记存 <MEMORY_DATA_DIR>/memory.jsonl（默认项目根 .memory/，已 gitignore），
# 跨会话保留：remember 写入、recall 搜索、forget 删除。
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.memory import MemoryNote, MemoryStore, note_from_dict, note_to_dict


class JsonlMemoryStore(MemoryStore):
    """把语义笔记按 JSONL 存在单文件里；删除时全量重写。"""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "memory.jsonl"

    async def _read_all(self) -> list[MemoryNote]:
        if not self._path.exists():
            return []
        notes: list[MemoryNote] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            notes.append(note_from_dict(json.loads(line)))
        return notes

    async def _write_all(self, notes: list[MemoryNote]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(note_to_dict(note), ensure_ascii=False) for note in notes)
        self._path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    async def list_notes(self) -> list[MemoryNote]:
        return await self._read_all()

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        note = MemoryNote(
            id=uuid4().hex[:12],
            content=content,
            tags=list(tags),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        notes = await self._read_all()
        notes.append(note)
        await self._write_all(notes)
        return note

    async def delete_note(self, note_id: str) -> bool:
        notes = await self._read_all()
        kept = [note for note in notes if note.id != note_id]
        if len(kept) == len(notes):
            return False
        await self._write_all(kept)
        return True

    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        notes = await self._read_all()
        target = next((note for note in notes if note.id == note_id), None)
        if target is None:
            return None
        if content is not None:
            target.content = content
        if tags is not None:
            target.tags = list(tags)
        await self._write_all(notes)
        return target

    async def save_record(self, record: MemoryNote) -> None:
        notes = await self._read_all()
        for index, note in enumerate(notes):
            if note.id == record.id:
                notes[index] = record
                break
        else:
            notes.append(record)
        await self._write_all(notes)

    async def search_notes(self, query: str) -> list[MemoryNote]:
        lowered = query.lower()
        return [
            note
            for note in await self._read_all()
            if not lowered
            or lowered in note.content.lower()
            or any(lowered in tag.lower() for tag in note.tags)
        ]


def create_store(plugin_dir):
    """插件工厂：返回 JSONL 长期记忆存储（数据目录读 MEMORY_DATA_DIR）。"""
    default_dir = Path(plugin_dir).resolve().parents[2] / ".memory"
    data_dir = Path(os.getenv("MEMORY_DATA_DIR", str(default_dir)))
    return JsonlMemoryStore(data_dir)
