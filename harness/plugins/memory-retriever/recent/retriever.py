"""Recency-first memory retriever with a rebuildable derived view."""

from core.memory import (
    MemoryQuery,
    MemoryRecord,
    MemoryRetriever,
    record_is_expired,
)


def _timestamp(record: MemoryRecord) -> str:
    return record.updated_at or record.created_at


class RecentMemoryRetriever(MemoryRetriever):
    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []

    def _sort(self) -> None:
        self._records.sort(key=lambda record: (_timestamp(record), record.id), reverse=True)

    async def rebuild(self, records: list[MemoryRecord]) -> None:
        self._records = list(records)
        self._sort()

    async def add(self, record: MemoryRecord) -> None:
        self._records = [item for item in self._records if item.id != record.id]
        self._records.append(record)
        self._sort()

    async def remove(self, record_id: str) -> None:
        self._records = [record for record in self._records if record.id != record_id]

    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecord]:
        needle = query.text.casefold()
        matches: list[MemoryRecord] = []
        for record in self._records:
            if record.status != "active" or record_is_expired(record):
                continue
            if query.scope is not None and record.scope != query.scope:
                continue
            if query.owner_id is not None and record.owner_id != query.owner_id:
                continue
            if query.agent_id is not None and record.agent_id != query.agent_id:
                continue
            if query.tenant_id is not None and record.tenant_id != query.tenant_id:
                continue
            if query.kinds and record.kind not in query.kinds:
                continue
            if needle and needle not in record.content.casefold():
                if not any(needle in tag.casefold() for tag in record.tags):
                    continue
            matches.append(record)
        return matches[: query.limit]


def create_retriever(plugin_dir) -> MemoryRetriever:
    return RecentMemoryRetriever()
