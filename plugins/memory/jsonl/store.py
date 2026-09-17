# plugins/memory/jsonl/store.py —— 长期记忆插件示例：JSONL 文件后端
#
# 笔记存 <MEMORY_DATA_DIR>/memory.jsonl（默认项目根 .memory/，已 gitignore），
# 跨会话保留：remember 写入、recall 搜索、forget 删除。
import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.memory import MemoryNote, MemoryStore, note_from_dict, note_to_dict

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Provide a cross-process lock using the host platform's file primitive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on Unix CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - platform branch
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - platform branch
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonlMemoryStore(MemoryStore):
    """把语义笔记按 JSONL 存在单文件里；删除时全量重写。"""

    supports_atomic_commit = True

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "memory.jsonl"
        self._lock_path = data_dir / ".memory.lock"
        self._lock = asyncio.Lock()

    def _read_all_unlocked(self) -> list[MemoryNote]:
        if not self._path.exists():
            return []
        notes: list[MemoryNote] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            notes.append(note_from_dict(json.loads(line)))
        return notes

    def _write_all_unlocked(self, notes: list[MemoryNote]) -> None:
        payload = "\n".join(json.dumps(note_to_dict(note), ensure_ascii=False) for note in notes)
        _atomic_write_text(self._path, payload + ("\n" if payload else ""))

    async def list_notes(self) -> list[MemoryNote]:
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                return self._read_all_unlocked()

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        note = self.create_record(content, tags)
        await self.commit_records([note])
        return note

    async def delete_note(self, note_id: str) -> bool:
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                notes = self._read_all_unlocked()
                kept = [note for note in notes if note.id != note_id]
                if len(kept) == len(notes):
                    return False
                self._write_all_unlocked(kept)
                return True

    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        if content is None and tags is None:
            raise ValueError("update_note requires content or tags")
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                notes = self._read_all_unlocked()
                target = next((note for note in notes if note.id == note_id), None)
                if target is None:
                    return None
                if content is not None:
                    target.content = content
                if tags is not None:
                    target.tags = list(tags)
                target.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
                self._write_all_unlocked(notes)
                return target

    async def save_record(self, record: MemoryNote) -> None:
        await self.commit_records([record])

    async def touch_records(
        self,
        records: list[MemoryNote],
        *,
        accessed_at: str,
    ) -> None:
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                notes = self._read_all_unlocked()
                by_id = {note.id: note for note in notes}
                changed = False
                for record in records:
                    current = by_id.get(record.id)
                    if current is None:
                        continue
                    current.last_accessed_at = accessed_at
                    current.metadata["accessCount"] = (
                        int(current.metadata.get("accessCount", 0)) + 1
                    )
                    changed = True
                if changed:
                    self._write_all_unlocked(notes)
                for record in records:
                    if record.id in by_id:
                        record.last_accessed_at = accessed_at
                        record.metadata["accessCount"] = (
                            int(record.metadata.get("accessCount", 0)) + 1
                        )

    async def commit_records(
        self,
        records: list[MemoryNote],
        *,
        delete_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                notes = self._read_all_unlocked()
                indexes = {note.id: index for index, note in enumerate(notes)}
                for record in records:
                    index = indexes.get(record.id)
                    if index is None:
                        indexes[record.id] = len(notes)
                        notes.append(record)
                    else:
                        notes[index] = record
                for record_id in delete_ids:
                    index = indexes.pop(record_id, None)
                    if index is not None:
                        notes.pop(index)
                        indexes = {note.id: index for index, note in enumerate(notes)}
                self._write_all_unlocked(notes)

    async def search_notes(self, query: str) -> list[MemoryNote]:
        async with self._lock:
            with _exclusive_file_lock(self._lock_path):
                lowered = query.lower()
                return [
                    note
                    for note in self._read_all_unlocked()
                    if not lowered
                    or lowered in note.content.lower()
                    or any(lowered in tag.lower() for tag in note.tags)
                ]


def create_store(plugin_dir):
    """插件工厂：返回 JSONL 长期记忆存储（数据目录读 MEMORY_DATA_DIR）。"""
    default_dir = Path(plugin_dir).resolve().parents[2] / ".memory"
    data_dir = Path(os.getenv("MEMORY_DATA_DIR", str(default_dir)))
    return JsonlMemoryStore(data_dir)
