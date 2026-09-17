"""Vector-backed semantic memory with an injected embedding provider."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from core.embedding import EmbeddingProvider
from core.memory import MemoryNote, MemoryStore, note_from_dict, note_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    embedding  TEXT NOT NULL,
    record     TEXT,
    last_accessed_at TEXT,
    access_count     INTEGER DEFAULT 0
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

    supports_atomic_commit = True

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
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_notes)")}
        migrated = False
        if "record" not in columns:
            conn.execute("ALTER TABLE memory_notes ADD COLUMN record TEXT")
            migrated = True
        if "last_accessed_at" not in columns:
            conn.execute("ALTER TABLE memory_notes ADD COLUMN last_accessed_at TEXT")
            migrated = True
        if "access_count" not in columns:
            conn.execute("ALTER TABLE memory_notes ADD COLUMN access_count INTEGER")
            migrated = True
        if migrated:
            SqliteVectorMemoryStore._backfill_access_columns(conn)
        return conn

    @staticmethod
    def _backfill_access_columns(conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, record, last_accessed_at, access_count FROM memory_notes")
        with conn:
            for row in rows.fetchall():
                try:
                    record = note_from_dict(json.loads(row["record"])) if row["record"] else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    record = None
                access_count = row["access_count"]
                last_accessed_at = row["last_accessed_at"]
                if record is not None:
                    if access_count is None:
                        access_count = int(record.metadata.get("accessCount", 0))
                    if last_accessed_at is None:
                        last_accessed_at = record.last_accessed_at
                conn.execute(
                    "UPDATE memory_notes SET access_count = ?, last_accessed_at = ? WHERE id = ?",
                    (int(access_count or 0), last_accessed_at or "", row["id"]),
                )

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> MemoryNote:
        if "record" in row.keys() and row["record"]:
            note = note_from_dict(json.loads(row["record"]))
        else:
            note = MemoryNote(
                id=row["id"],
                content=row["content"],
                tags=json.loads(row["tags"]),
                created_at=row["created_at"],
            )
        if "access_count" in row.keys() and row["access_count"] is not None:
            note.metadata["accessCount"] = int(row["access_count"])
        if "last_accessed_at" in row.keys() and row["last_accessed_at"]:
            note.last_accessed_at = row["last_accessed_at"]
        return note

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    async def list_notes(self) -> list[MemoryNote]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, content, tags, created_at, embedding, record,"
                " last_accessed_at, access_count"
                " FROM memory_notes ORDER BY created_at, id"
            ).fetchall()
            return [self._row_to_note(row) for row in rows]
        finally:
            conn.close()

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        note = self.create_record(content, tags)
        await self.commit_records([note])
        return note

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

        async with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(sql, params)
                    if cursor.rowcount == 0:
                        return None
                    row = conn.execute(
                        "SELECT id, content, tags, created_at, embedding, record,"
                        " last_accessed_at, access_count"
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
                    embedding = row["embedding"]
                    conn.execute(
                        "UPDATE memory_notes SET record = ?, embedding = ? WHERE id = ?",
                        (
                            json.dumps(note_to_dict(note), ensure_ascii=False),
                            embedding,
                            note_id,
                        ),
                    )
                    return note
            finally:
                conn.close()

    async def save_record(self, record: MemoryNote) -> None:
        await self.commit_records([record])

    @staticmethod
    def _upsert_unlocked(
        conn: sqlite3.Connection,
        record: MemoryNote,
        embedding: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_notes"
            " (id, content, tags, created_at, embedding, record, last_accessed_at, access_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " content = excluded.content,"
            " tags = excluded.tags,"
            " created_at = excluded.created_at,"
            " embedding = excluded.embedding,"
            " record = excluded.record,"
            " last_accessed_at = excluded.last_accessed_at,"
            " access_count = excluded.access_count",
            (
                record.id,
                record.content,
                json.dumps(record.tags, ensure_ascii=False),
                record.created_at,
                embedding,
                json.dumps(note_to_dict(record), ensure_ascii=False),
                record.last_accessed_at,
                int(record.metadata.get("accessCount", 0)),
            ),
        )

    async def touch_records(
        self,
        records: list[MemoryNote],
        *,
        accessed_at: str,
    ) -> None:
        async with self._lock:
            conn = self._connect()
            try:
                with conn:
                    for record in records:
                        conn.execute(
                            "UPDATE memory_notes"
                            " SET last_accessed_at = ?,"
                            " access_count = COALESCE(access_count, 0) + 1"
                            " WHERE id = ?",
                            (accessed_at, record.id),
                        )
            finally:
                conn.close()
        for record in records:
            record.last_accessed_at = accessed_at
            record.metadata["accessCount"] = int(record.metadata.get("accessCount", 0)) + 1

    async def commit_records(
        self,
        records: list[MemoryNote],
        *,
        delete_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        async with self._lock:
            conn = self._connect()
            try:
                embeddings: dict[str, str] = {}
                for record in records:
                    row = conn.execute(
                        "SELECT content, embedding FROM memory_notes WHERE id = ?",
                        (record.id,),
                    ).fetchone()
                    if row is not None and row["content"] == record.content:
                        embeddings[record.id] = row["embedding"]
                    else:
                        embeddings[record.id] = json.dumps(
                            await self._provider.embed_one(record.content)
                        )
                with conn:
                    for record in records:
                        self._upsert_unlocked(conn, record, embeddings[record.id])
                    for record_id in delete_ids:
                        conn.execute("DELETE FROM memory_notes WHERE id = ?", (record_id,))
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
                "SELECT id, content, tags, created_at, embedding, record,"
                " last_accessed_at, access_count FROM memory_notes"
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
                "SELECT id, content, tags, created_at, embedding, record,"
                " last_accessed_at, access_count FROM memory_notes"
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
