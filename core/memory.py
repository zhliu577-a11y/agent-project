"""Long-term memory contracts and versioned records."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class MemoryRecord:
    """One durable semantic-memory record.

    ``MemoryNote`` remains an alias for compatibility with existing plugins.
    New fields are optional so old JSONL and SQLite databases keep loading.
    """

    id: str
    content: str
    tags: list[str]
    created_at: str
    kind: str = "fact"
    scope: str = "user"
    owner_id: str = ""
    source: str = "explicit"
    confidence: float = 1.0
    importance: float = 0.5
    status: str = "active"
    updated_at: str = ""
    last_accessed_at: str = ""
    expires_at: str | None = None
    supersedes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


MemoryNote = MemoryRecord


@dataclass(frozen=True)
class MemoryQuery:
    """A policy and index query over semantic memory."""

    text: str = ""
    scope: str | None = None
    owner_id: str | None = None
    kinds: tuple[str, ...] = ()
    limit: int = 20
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class MemoryRecallPort(Protocol):
    """Read-only memory view consumed by context assembly."""

    async def context_records(
        self,
        query: str,
        *,
        scope: str | None = None,
        owner_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return context-ready records without exposing write operations."""
        ...


def record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "content": record.content,
        "tags": list(record.tags),
        "created_at": record.created_at,
        "kind": record.kind,
        "scope": record.scope,
        "owner_id": record.owner_id,
        "source": record.source,
        "confidence": record.confidence,
        "importance": record.importance,
        "status": record.status,
        "updated_at": record.updated_at,
        "last_accessed_at": record.last_accessed_at,
        "expires_at": record.expires_at,
        "supersedes": record.supersedes,
        "metadata": dict(record.metadata),
    }


def record_from_dict(raw: dict[str, Any]) -> MemoryRecord:
    created_at = str(raw.get("created_at") or _now())
    return MemoryRecord(
        id=str(raw["id"]),
        content=str(raw.get("content", "")),
        tags=[str(item) for item in raw.get("tags", [])],
        created_at=created_at,
        kind=str(raw.get("kind", "fact")),
        scope=str(raw.get("scope", "user")),
        owner_id=str(raw.get("owner_id", "")),
        source=str(raw.get("source", "explicit")),
        confidence=float(raw.get("confidence", 1.0)),
        importance=float(raw.get("importance", 0.5)),
        status=str(raw.get("status", "active")),
        updated_at=str(raw.get("updated_at", "")),
        last_accessed_at=str(raw.get("last_accessed_at", "")),
        expires_at=(str(raw["expires_at"]) if raw.get("expires_at") is not None else None),
        supersedes=(str(raw["supersedes"]) if raw.get("supersedes") is not None else None),
        metadata=dict(raw.get("metadata", {})),
    )


def note_to_dict(note: MemoryNote) -> dict[str, Any]:
    return record_to_dict(note)


def note_from_dict(raw: dict[str, Any]) -> MemoryNote:
    return record_from_dict(raw)


class MemoryStore(ABC):
    """Persistence contract for durable semantic memory."""

    @abstractmethod
    async def list_notes(self) -> list[MemoryNote]:
        """Return all records in stable creation order."""
        ...

    @abstractmethod
    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        """Create and return one record."""
        ...

    @abstractmethod
    async def delete_note(self, note_id: str) -> bool:
        """Delete one record; return False when it does not exist."""
        ...

    @abstractmethod
    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        """Update content or tags; return None when the record is absent."""
        ...

    @abstractmethod
    async def search_notes(self, query: str) -> list[MemoryNote]:
        """Return candidate records for a query."""
        ...

    async def save_record(self, record: MemoryRecord) -> None:
        """Persist extended record fields when the backend supports them."""
        return None


class MemoryIndex(ABC):
    """Replaceable derived-memory index.

    The index never owns a memory record. It can be rebuilt from a
    ``MemoryStore`` and must tolerate candidates that are no longer present.
    """

    @abstractmethod
    async def rebuild(self, records: list[MemoryRecord]) -> None: ...

    @abstractmethod
    async def add(self, record: MemoryRecord) -> None: ...

    @abstractmethod
    async def remove(self, record_id: str) -> None: ...

    @abstractmethod
    async def search(self, query: MemoryQuery) -> list[MemoryRecord]: ...


class MemoryPolicy(ABC):
    """Replaceable semantic-memory policy for writes and recall ranking."""

    def prepare_write(
        self,
        record: MemoryRecord,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> MemoryRecord | None:
        """Return a record to persist, or None to reject the candidate."""
        return record

    @abstractmethod
    def rank(
        self,
        query: MemoryQuery,
        candidates: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        """Rank or filter candidate records."""
        ...
