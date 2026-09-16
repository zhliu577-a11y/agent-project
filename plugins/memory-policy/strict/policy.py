"""Strict semantic-memory policy."""

from datetime import UTC, datetime

from core.memory import MemoryPolicy, MemoryQuery, MemoryRecord

_MIN_CONTENT_CHARS = 8
_MIN_CONFIDENCE = 0.6
_MIN_IMPORTANCE = 0.2


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


class StrictMemoryPolicy(MemoryPolicy):
    """Reject weak writes and prefer high-confidence recall candidates."""

    def prepare_write(
        self,
        record: MemoryRecord,
        *,
        context=None,
    ) -> MemoryRecord | None:
        if record.status != "active" or _is_expired(record):
            return None
        if len(record.content.strip()) < _MIN_CONTENT_CHARS:
            return None
        if record.confidence < _MIN_CONFIDENCE:
            return None
        if record.importance < _MIN_IMPORTANCE:
            return None
        return record

    def rank(
        self,
        query: MemoryQuery,
        candidates: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        accepted = [
            record
            for record in candidates
            if record.status == "active"
            and not _is_expired(record)
            and record.confidence >= _MIN_CONFIDENCE
            and record.importance >= _MIN_IMPORTANCE
        ]
        accepted.sort(
            key=lambda record: (
                record.confidence,
                record.importance,
                record.updated_at or record.created_at,
            ),
            reverse=True,
        )
        return accepted[: query.limit]


def create_policy(plugin_dir) -> MemoryPolicy:
    return StrictMemoryPolicy()
