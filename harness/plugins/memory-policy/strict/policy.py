"""Strict semantic-memory policy."""

from core.memory import (
    MemoryPolicy,
    MemoryQuery,
    MemoryRecord,
    MemoryWriteDecision,
    normalize_memory_text,
    record_is_expired,
)

_MIN_CONTENT_CHARS = 8
_MIN_CONFIDENCE = 0.6
_MIN_IMPORTANCE = 0.2


def _is_expired(record: MemoryRecord) -> bool:
    return record_is_expired(record)


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

    def reconcile(
        self,
        candidate: MemoryRecord,
        existing: list[MemoryRecord],
    ) -> MemoryWriteDecision:
        normalized = normalize_memory_text(candidate.content)
        for record in existing:
            if record.id == candidate.id or record.status != "active":
                continue
            if (
                record.scope == candidate.scope
                and record.owner_id == candidate.owner_id
                and record.agent_id == candidate.agent_id
                and record.tenant_id == candidate.tenant_id
                and normalize_memory_text(record.content) == normalized
            ):
                return MemoryWriteDecision(
                    action="skip",
                    target_id=record.id,
                    reason="duplicate-content",
                )
        if candidate.supersedes:
            target = next(
                (record for record in existing if record.id == candidate.supersedes),
                None,
            )
            if target is not None and target.status == "active":
                return MemoryWriteDecision(
                    action="supersede",
                    target_id=target.id,
                    reason="explicit-supersedes",
                )
        return MemoryWriteDecision()

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
