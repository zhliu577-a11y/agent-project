"""Lexical index for derived memory candidates."""

from core.memory import MemoryIndex, MemoryQuery, MemoryRecord, record_is_expired


def _is_expired(record: MemoryRecord) -> bool:
    return record_is_expired(record)


class LexicalMemoryIndex(MemoryIndex):
    """A replaceable, non-owning index rebuilt from MemoryStore records."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    async def rebuild(self, records: list[MemoryRecord]) -> None:
        self._records = {record.id: record for record in records}

    async def add(self, record: MemoryRecord) -> None:
        self._records[record.id] = record

    async def remove(self, record_id: str) -> None:
        self._records.pop(record_id, None)

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        needle = query.text.casefold()
        matches: list[MemoryRecord] = []
        for record in self._records.values():
            if record.status != "active" or _is_expired(record):
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


def create_index(plugin_dir) -> MemoryIndex:
    return LexicalMemoryIndex()
