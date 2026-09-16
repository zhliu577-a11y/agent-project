"""Default semantic-memory policy."""

from core.memory import (
    MemoryPolicy,
    MemoryQuery,
    MemoryRecord,
    MemoryWriteDecision,
    normalize_memory_text,
    record_is_expired,
)


def _is_expired(record: MemoryRecord) -> bool:
    return record_is_expired(record)


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
