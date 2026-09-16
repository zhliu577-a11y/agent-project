"""Vector-backed semantic memory with an injected embedding provider."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.embedding import EmbeddingProvider
from core.memory import MemoryNote, MemoryStore, note_from_dict, note_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    record     TEXT
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
    """SQLite persistence plus in-process cosine ranking."""

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
                "SELECT id, content, tags, created_at, embedding, record"
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
        embedding = json.dumps(await self._provider.embed_one(content))
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memory_notes (id, content, tags, created_at, embedding, record)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        note.id,
                        note.content,
                        json.dumps(note.tags, ensure_ascii=False),
                        note.created_at,
                        embedding,
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
            assignments.append("embedding = ?")
            params.append(json.dumps(await self._provider.embed_one(content)))
        if tags is not None:
            assignments.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if not assignments:
            raise ValueError("update_note requires content or tags")
        params.append(note_id)
        sql = f"UPDATE memory_notes SET {', '.join(assignments)} WHERE id = ?"

        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, content, tags, created_at, embedding, record"
                " FROM memory_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                return None
            note = self._row_to_note(row)
            if content is not None:
                note.content = content
            if tags is not None:
                note.tags = list(tags)
            note.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
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
                "SELECT id, content, tags, created_at, embedding, record FROM memory_notes"
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
                "SELECT id, content, tags, created_at, embedding, record FROM memory_notes"
                " WHERE content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'"
                " ORDER BY created_at, id",
                (pattern, pattern),
            ).fetchall()
            return [self._row_to_note(row) for row in rows]
        finally:
            conn.close()


def create_store(plugin_dir, context=None, embedding: EmbeddingProvider | None = None):
    """Create vector memory with the host-provided embedding service."""
    if embedding is None:
        raise ValueError("vector memory requires an injected embedding provider")
    plugin_config = dict(getattr(context, "config", {}) or {})
    default_db = Path(plugin_dir).resolve().parents[2] / ".memory" / "vector.db"
    db_path = Path(
        plugin_config.get(
            "dbPath",
            os.getenv("MEMORY_VECTOR_DB_PATH", str(default_db)),
        )
    )
    threshold = float(
        plugin_config.get(
            "similarityThreshold",
            os.getenv("VECTOR_SIMILARITY_THRESHOLD", "0.2"),
        )
    )
    top_k = int(plugin_config.get("topK", os.getenv("VECTOR_TOP_K", "3")))
    return SqliteVectorMemoryStore(db_path, embedding, threshold, top_k)
