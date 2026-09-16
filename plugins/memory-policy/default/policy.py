"""Default semantic-memory policy."""

from datetime import UTC, datetime

from core.memory import MemoryPolicy, MemoryQuery, MemoryRecord


def _is_expired(record: MemoryRecord) -> bool:
    if not record.expires_at:
        return False
    try:
        expires = datetime.fromisoformat(record.expires_at)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


class DefaultMemoryPolicy(MemoryPolicy):
    def prepare_write(
        self,
        record: MemoryRecord,
        *,
        context=None,
    ) -> MemoryRecord | None:
        if record.status in {"deleted", "expired"}:
            return None
        return record

    def rank(
        self,
        query: MemoryQuery,
        candidates: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        active = [
            record for record in candidates if record.status == "active" and not _is_expired(record)
        ]
        active.sort(
            key=lambda record: (
                record.importance,
                record.confidence,
                record.updated_at or record.created_at,
            ),
            reverse=True,
        )
        return active[: query.limit]


def create_policy(plugin_dir) -> MemoryPolicy:
    return DefaultMemoryPolicy()
