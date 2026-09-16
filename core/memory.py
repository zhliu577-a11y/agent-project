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
    agent_id: str = ""
    tenant_id: str = ""
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
    agent_id: str | None = None
    tenant_id: str | None = None
    kinds: tuple[str, ...] = ()
    limit: int = 20
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryIdentity:
    """Trusted identity assigned to memory reads and writes."""

    scope: str = "user"
    owner_id: str = ""
    agent_id: str = ""
    tenant_id: str = ""


@dataclass(frozen=True)
class MemoryWriteDecision:
    """Lifecycle action selected by a memory policy."""

    action: str = "insert"
    target_id: str | None = None
    reason: str = ""


def normalize_memory_text(value: str) -> str:
    """Normalize content for exact duplicate detection."""
    return " ".join(value.split()).casefold()


def record_is_expired(record: MemoryRecord, *, now: datetime | None = None) -> bool:
    """Return whether a record has an invalid or elapsed expiration time."""
    if not record.expires_at:
        return False
    try:
        expires = datetime.fromisoformat(record.expires_at)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= (now or datetime.now(UTC))


@runtime_checkable
class MemoryRecallPort(Protocol):
    """Read-only memory view consumed by context assembly."""

    async def context_records(
        self,
        query: str,
        *,
        scope: str | None = None,
        owner_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
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
        "agent_id": record.agent_id,
        "tenant_id": record.tenant_id,
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
        agent_id=str(raw.get("agent_id", "")),
        tenant_id=str(raw.get("tenant_id", "")),
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


class MemoryRetriever(ABC):
    """Turn a memory query into candidate records.

    A retriever may query storage directly, search a derived index, call a
    vector service, or combine several sources. Optional maintenance methods
    let retrievers keep derived state in sync with MemoryGateway writes.
    """

    @abstractmethod
    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Return candidate records for policy ranking."""

    async def rebuild(self, records: list[MemoryRecord]) -> None:
        """Rebuild optional derived state from durable records."""
        return None

    async def add(self, record: MemoryRecord) -> None:
        """Observe a durable record write."""
        return None

    async def remove(self, record_id: str) -> None:
        """Observe a durable record removal."""
        return None


class StoreMemoryRetriever(MemoryRetriever):
    """Default retriever that delegates to the storage backend's search."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecord]:
        return await self._store.search_notes(query.text)


class LegacyMemoryIndexRetriever(MemoryRetriever):
    """Adapt a v1 MemoryIndex plugin to the retriever contract."""

    def __init__(self, index: MemoryIndex) -> None:
        self._index = index

    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecord]:
        return await self._index.search(query)

    async def rebuild(self, records: list[MemoryRecord]) -> None:
        await self._index.rebuild(records)

    async def add(self, record: MemoryRecord) -> None:
        await self._index.add(record)

    async def remove(self, record_id: str) -> None:
        await self._index.remove(record_id)


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

    def reconcile(
        self,
        candidate: MemoryRecord,
        existing: list[MemoryRecord],
    ) -> MemoryWriteDecision:
        """Choose how a candidate should be reconciled with existing records."""
        return MemoryWriteDecision()

    @abstractmethod
    def rank(
        self,
        query: MemoryQuery,
        candidates: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        """Rank or filter candidate records."""
        ...
