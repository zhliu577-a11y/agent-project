# plugins/memory/sqlite/store.py —— 长期记忆插件：SQLite 后端（生产方向）
#
# 特性：
# - 单文件 SQLite（默认 .memory/memory.db，MEMORY_DB_PATH 可覆盖）；
# - WAL 模式 + busy_timeout，读写在异步/多线程场景下更稳；
# - 参数化 SQL，杜绝注入；每次操作独立连接，提交/回滚由事务保证；
# - tags 以 JSON 数组文本存储，LIKE 搜索（内容或标签）。
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.memory import MemoryNote, MemoryStore, note_from_dict, note_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record     TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_created_at ON memory_notes (created_at);
"""


class SqliteMemoryStore(MemoryStore):
    """把语义笔记存在 SQLite 里：事务写入、可扩展、可并发。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_notes)")}
        if "record" not in columns:
            conn.execute("ALTER TABLE memory_notes ADD COLUMN record TEXT")
        return conn

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> MemoryNote:
        if "record" in row.keys() and row["record"]:
            return note_from_dict(json.loads(row["record"]))
        return MemoryNote(
            id=row["id"],
            content=row["content"],
            tags=json.loads(row["tags"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def list_notes(self) -> list[MemoryNote]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, content, tags, created_at, record"
                " FROM memory_notes ORDER BY created_at, id"
            ).fetchall()
            return [self._row_to_note(row) for row in rows]
        finally:
            conn.close()

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        note = MemoryNote(
            id=uuid4().hex[:12],
            content=content,
            tags=list(tags),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memory_notes (id, content, tags, created_at, record)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        note.id,
                        note.content,
                        json.dumps(note.tags, ensure_ascii=False),
                        note.created_at,
                        json.dumps(note_to_dict(note), ensure_ascii=False),
                    ),
                )
            return note
        finally:
            conn.close()

    async def delete_note(self, note_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
            return cursor.rowcount > 0
        finally:
            conn.close()

    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        assignments: list[str] = []
        params: list[object] = []
        if content is not None:
            assignments.append("content = ?")
            params.append(content)
        if tags is not None:
            assignments.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if not assignments:
            raise ValueError("update_note 至少要提供 content 或 tags 之一")
        params.append(note_id)
        sql = f"UPDATE memory_notes SET {', '.join(assignments)} WHERE id = ?"

        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, content, tags, created_at, record FROM memory_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                return None
            note = self._row_to_note(row)
            if content is not None:
                note.content = content
            if tags is not None:
                note.tags = list(tags)
            conn.execute(
                "UPDATE memory_notes SET record = ? WHERE id = ?",
                (json.dumps(note_to_dict(note), ensure_ascii=False), note_id),
            )
            return note
        finally:
            conn.close()

    async def save_record(self, record: MemoryNote) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE memory_notes SET content = ?, tags = ?, created_at = ?, record = ?"
                    " WHERE id = ?",
                    (
                        record.content,
                        json.dumps(record.tags, ensure_ascii=False),
                        record.created_at,
                        json.dumps(note_to_dict(record), ensure_ascii=False),
                        record.id,
                    ),
                )
        finally:
            conn.close()

    async def search_notes(self, query: str) -> list[MemoryNote]:
        conn = self._connect()
        try:
            if not query:
                rows = conn.execute(
                    "SELECT id, content, tags, created_at, record"
                    " FROM memory_notes ORDER BY created_at, id"
                ).fetchall()
            else:
                pattern = f"%{self._escape_like(query)}%"
                rows = conn.execute(
                    "SELECT id, content, tags, created_at, record FROM memory_notes"
                    " WHERE content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'"
                    " ORDER BY created_at, id",
                    (pattern, pattern),
                ).fetchall()
            return [self._row_to_note(row) for row in rows]
        finally:
            conn.close()


def create_store(plugin_dir):
    """插件工厂：返回 SQLite 长期记忆存储（路径读 MEMORY_DB_PATH）。"""
    default_db = Path(plugin_dir).resolve().parents[2] / ".memory" / "memory.db"
    db_path = Path(os.getenv("MEMORY_DB_PATH", str(default_db)))
    return SqliteMemoryStore(db_path)
