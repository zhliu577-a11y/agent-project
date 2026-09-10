# plugins/memory/vector/store.py —— 长期记忆插件：向量后端（语义检索）
#
# 在 SQLite 里额外保存每条的 embedding 向量：
# - 写入/更新正文时用 EmbeddingProvider 生成向量；
# - 检索：query 也转向量 → 余弦相似度排序取 Top-K；
#   最佳相似度低于阈值时自动回退“子串/标签”字面搜索；
# - 嵌入来源由 EMBEDDING_PROVIDER 选择（debug=离线测试；openai-embedding=真实语义）。
import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.embedding import EmbeddingProvider
from core.memory import MemoryNote, MemoryStore
from plugins.loader import DEFAULT_PLUGINS_DIR, load_embedding_plugins

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    embedding  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vector_created_at ON memory_notes (created_at);
"""


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SqliteVectorMemoryStore(MemoryStore):
    """SQLite 持久化 + 内存余弦排序：笔记规模下兼顾简单与语义检索。"""

    def __init__(
        self,
        db_path: Path,
        provider: EmbeddingProvider,
        similarity_threshold: float,
        top_k: int,
    ) -> None:
        self._db_path = db_path
        self._provider = provider
        self._threshold = similarity_threshold
        self._top_k = top_k

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        return conn

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> MemoryNote:
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
                "SELECT id, content, tags, created_at FROM memory_notes ORDER BY created_at, id"
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
        embedding = json.dumps(await self._provider.embed_one(content))
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memory_notes (id, content, tags, created_at, embedding)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        note.id,
                        note.content,
                        json.dumps(note.tags, ensure_ascii=False),
                        note.created_at,
                        embedding,
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
            assignments.append("embedding = ?")
            params.append(json.dumps(await self._provider.embed_one(content)))
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
                "SELECT id, content, tags, created_at FROM memory_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            return self._row_to_note(row) if row is not None else None
        finally:
            conn.close()

    async def search_notes(self, query: str) -> list[MemoryNote]:
        if not query:
            return await self.list_notes()
        query_vector = await self._provider.embed_one(query)
        semantic = await self._semantic_candidates(query_vector)
        if semantic:
            return semantic
        return await self._lexical_fallback(query)

    async def _semantic_candidates(self, query_vector: list[float]) -> list[MemoryNote]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, content, tags, created_at, embedding FROM memory_notes"
            ).fetchall()
        finally:
            conn.close()

        scored: list[tuple[float, MemoryNote]] = []
        for row in rows:
            try:
                vector = json.loads(row["embedding"])
            except json.JSONDecodeError:
                continue
            score = _cosine(query_vector, vector)
            if score >= self._threshold:
                scored.append((score, self._row_to_note(row)))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [note for _, note in scored[: self._top_k]]

    async def _lexical_fallback(self, query: str) -> list[MemoryNote]:
        conn = self._connect()
        try:
            pattern = f"%{self._escape_like(query)}%"
            rows = conn.execute(
                "SELECT id, content, tags, created_at FROM memory_notes"
                " WHERE content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'"
                " ORDER BY created_at, id",
                (pattern, pattern),
            ).fetchall()
            return [self._row_to_note(row) for row in rows]
        finally:
            conn.close()


def _load_provider() -> EmbeddingProvider:
    name = os.getenv("EMBEDDING_PROVIDER", "debug")
    plugins = load_embedding_plugins(DEFAULT_PLUGINS_DIR)
    plugin = next((p for p in plugins if p.manifest.name == name), None)
    if plugin is None:
        available = [p.manifest.name for p in plugins]
        raise RuntimeError(f"未知的嵌入提供方: {name}，可用: {available}")
    return plugin.create()


def create_store(plugin_dir):
    """插件工厂：返回向量记忆存储（DB 路径读 MEMORY_VECTOR_DB_PATH）。"""
    default_db = Path(plugin_dir).resolve().parents[2] / ".memory" / "vector.db"
    db_path = Path(os.getenv("MEMORY_VECTOR_DB_PATH", str(default_db)))
    threshold = float(os.getenv("VECTOR_SIMILARITY_THRESHOLD", "0.2"))
    top_k = int(os.getenv("VECTOR_TOP_K", "3"))
    return SqliteVectorMemoryStore(db_path, _load_provider(), threshold, top_k)
