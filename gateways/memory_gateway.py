"""Semantic-memory transaction boundary."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.errors import ToolError, boundary
from core.events import Event, EventPublisher
from core.memory import (
    LegacyMemoryIndexRetriever,
    MemoryIdentity,
    MemoryIndex,
    MemoryNote,
    MemoryPolicy,
    MemoryQuery,
    MemoryRetriever,
    MemoryStore,
    StoreMemoryRetriever,
    record_is_expired,
    record_to_dict,
)
from core.tool import Tool
from core.tracing import current_trace_id

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryGateway:
    """Coordinate durable records, derived indexes, and recall policy."""

    def __init__(
        self,
        store: MemoryStore,
        events: EventPublisher | None = None,
        *,
        retriever: MemoryRetriever | None = None,
        # Deprecated: retained so existing memory-index plugins keep working.
        index: MemoryIndex | None = None,
        policy: MemoryPolicy | None = None,
        default_scope: str = "user",
        default_owner_id: str = "",
        default_agent_id: str = "",
        default_tenant_id: str = "",
        allowed_scopes: set[str] | frozenset[str] | None = None,
        allowed_owner_ids: set[str] | frozenset[str] | None = None,
        allowed_agent_ids: set[str] | frozenset[str] | None = None,
        allowed_tenant_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        if not default_scope:
            raise ValueError("default_scope must not be empty")
        if retriever is not None and index is not None:
            raise ValueError("pass either retriever or legacy index, not both")
        self._store = store
        self._retriever = (
            retriever
            or (LegacyMemoryIndexRetriever(index) if index is not None else None)
            or StoreMemoryRetriever(store)
        )
        self._policy = policy
        self._events = events
        self._default_scope = default_scope
        self._default_owner_id = default_owner_id
        self._default_agent_id = default_agent_id
        self._default_tenant_id = default_tenant_id
        self._allowed_scopes = frozenset(allowed_scopes) if allowed_scopes is not None else None
        self._allowed_owner_ids = (
            frozenset(allowed_owner_ids) if allowed_owner_ids is not None else None
        )
        self._allowed_agent_ids = (
            frozenset(allowed_agent_ids) if allowed_agent_ids is not None else None
        )
        self._allowed_tenant_ids = (
            frozenset(allowed_tenant_ids) if allowed_tenant_ids is not None else None
        )

    @property
    def identity(self) -> MemoryIdentity:
        return MemoryIdentity(
            scope=self._default_scope,
            owner_id=self._default_owner_id,
            agent_id=self._default_agent_id,
            tenant_id=self._default_tenant_id,
        )

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    def _authorize(
        self,
        scope: str,
        owner_id: str,
        agent_id: str = "",
        tenant_id: str = "",
    ) -> None:
        if self._allowed_scopes is not None and scope not in self._allowed_scopes:
            raise PermissionError(f"memory scope is not allowed: {scope}")
        if (
            self._allowed_owner_ids is not None
            and owner_id
            and owner_id not in self._allowed_owner_ids
        ):
            raise PermissionError(f"memory owner is not allowed: {owner_id}")
        if self._allowed_agent_ids is not None and agent_id not in self._allowed_agent_ids:
            raise PermissionError(f"memory agent is not allowed: {agent_id}")
        if self._allowed_tenant_ids is not None and tenant_id not in self._allowed_tenant_ids:
            raise PermissionError(f"memory tenant is not allowed: {tenant_id}")

    async def _authorize_record(self, note_id: str) -> None:
        record = next(
            (item for item in await self._store.list_notes() if item.id == note_id),
            None,
        )
        if record is None:
            return
        self._authorize(
            record.scope,
            record.owner_id,
            record.agent_id,
            record.tenant_id,
        )

    async def _emit(self, name: str, **payload: object) -> None:
        if self._events is None:
            return
        await self._events.publish(
            Event(name=name, payload=dict(payload), trace_id=current_trace_id())
        )

    @boundary("写入长期记忆失败", fallback=ToolError)
    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        *,
        kind: str = "fact",
        scope: str | None = None,
        owner_id: str | None = None,
        source: str = "explicit",
        confidence: float = 1.0,
        importance: float = 0.5,
        expires_at: str | None = None,
        supersedes: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ) -> MemoryNote:
        requested_scope = scope or self._default_scope
        requested_owner_id = self._default_owner_id if owner_id is None else owner_id
        requested_agent_id = self._default_agent_id if agent_id is None else agent_id
        requested_tenant_id = self._default_tenant_id if tenant_id is None else tenant_id
        self._authorize(
            requested_scope,
            requested_owner_id,
            requested_agent_id,
            requested_tenant_id,
        )

        note = await self._store.add_note(content, tags or [])
        note.kind = kind
        note.scope = requested_scope
        note.owner_id = requested_owner_id
        note.agent_id = requested_agent_id
        note.tenant_id = requested_tenant_id
        note.source = source
        note.confidence = confidence
        note.importance = importance
        note.expires_at = expires_at
        note.supersedes = supersedes
        note.metadata = dict(metadata or {})
        note.updated_at = note.created_at

        if self._policy is not None:
            candidate = self._policy.prepare_write(note)
            if candidate is None:
                await self._store.delete_note(note.id)
                return note
            note = candidate
            try:
                self._authorize(
                    note.scope,
                    note.owner_id,
                    note.agent_id,
                    note.tenant_id,
                )
            except Exception:
                await self._store.delete_note(note.id)
                raise

        try:
            existing = [record for record in await self._store.list_notes() if record.id != note.id]
            decision = self._policy.reconcile(note, existing) if self._policy is not None else None
            if decision is not None and decision.action == "skip":
                await self._store.delete_note(note.id)
                await self._emit(
                    "memory.skipped",
                    note_id=note.id,
                    target_id=decision.target_id,
                    reason=decision.reason,
                )
                target = next(
                    (record for record in existing if record.id == decision.target_id),
                    None,
                )
                return target or note
            if decision is not None and decision.action == "update":
                target = next(
                    (record for record in existing if record.id == decision.target_id),
                    None,
                )
                if target is not None:
                    updated = self._merge_record(target, note)
                    await self._store.save_record(updated)
                    await self._retriever.add(updated)
                    await self._store.delete_note(note.id)
                    await self._emit(
                        "memory.update",
                        note_id=updated.id,
                        reason=decision.reason,
                        tags=updated.tags,
                    )
                    return updated
            if decision is not None and decision.action == "supersede":
                target = next(
                    (record for record in existing if record.id == decision.target_id),
                    None,
                )
                if target is not None:
                    target.status = "superseded"
                    target.updated_at = _now()
                    await self._store.save_record(target)
                    await self._retriever.remove(target.id)
                    note.supersedes = target.id
            await self._store.save_record(note)
            await self._retriever.add(note)
        except Exception:
            await self._store.delete_note(note.id)
            await self._retriever.remove(note.id)
            raise
        await self._emit("memory.write", note_id=note.id, tags=note.tags, scope=note.scope)
        return note

    @staticmethod
    def _merge_record(target: MemoryNote, candidate: MemoryNote) -> MemoryNote:
        target.content = candidate.content
        target.tags = list(candidate.tags)
        target.kind = candidate.kind
        target.source = candidate.source
        target.confidence = candidate.confidence
        target.importance = candidate.importance
        target.expires_at = candidate.expires_at
        target.metadata = dict(candidate.metadata)
        target.updated_at = _now()
        return target

    @boundary("检索长期记忆失败", fallback=ToolError)
    async def recall(
        self,
        query: str = "",
        *,
        scope: str | None = None,
        owner_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        kinds: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[MemoryNote]:
        requested_scope = scope or self._default_scope
        requested_owner_id = self._default_owner_id if owner_id is None else owner_id
        requested_agent_id = self._default_agent_id if agent_id is None else agent_id
        requested_tenant_id = self._default_tenant_id if tenant_id is None else tenant_id
        request = MemoryQuery(
            text=query,
            scope=requested_scope,
            owner_id=requested_owner_id,
            agent_id=requested_agent_id,
            tenant_id=requested_tenant_id,
            kinds=kinds,
            limit=limit,
        )
        self._authorize(
            requested_scope,
            requested_owner_id,
            requested_agent_id,
            requested_tenant_id,
        )
        candidates = await self._retriever.retrieve(request)
        candidates = [
            record
            for record in candidates
            if (request.scope is None or record.scope == request.scope)
            and (request.owner_id is None or record.owner_id == request.owner_id)
            and (request.agent_id is None or record.agent_id == request.agent_id)
            and (request.tenant_id is None or record.tenant_id == request.tenant_id)
            and (not request.kinds or record.kind in request.kinds)
        ]
        if self._policy is not None:
            candidates = self._policy.rank(request, list(candidates))

        accessed_at = _now()
        for record in candidates:
            record.last_accessed_at = accessed_at
            access_count = int(record.metadata.get("accessCount", 0)) + 1
            record.metadata["accessCount"] = access_count
            try:
                await self._store.save_record(record)
                await self._retriever.add(record)
            except Exception as exc:
                logger.warning("memory access statistics update failed: %s", exc)
        return candidates[:limit]

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
        records = await self.recall(
            query,
            scope=scope,
            owner_id=owner_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            limit=limit,
        )
        return [record_to_dict(record) for record in records]

    async def maintain(self) -> dict[str, int]:
        """Mark expired records and remove them from the derived index."""
        records = await self._store.list_notes()
        expired = [
            record for record in records if record.status == "active" and record_is_expired(record)
        ]
        for record in expired:
            previous = record.status
            record.status = "expired"
            record.updated_at = _now()
            try:
                await self._store.save_record(record)
                await self._retriever.remove(record.id)
            except Exception:
                record.status = previous
                logger.exception("memory expiration maintenance failed: %s", record.id)
        if expired:
            await self._emit("memory.expired", count=len(expired))
        return {"checked": len(records), "expired": len(expired)}

    @boundary("删除长期记忆失败", fallback=ToolError)
    async def forget(self, note_id: str) -> bool:
        await self._authorize_record(note_id)
        removed = await self._store.delete_note(note_id)
        if removed:
            await self._retriever.remove(note_id)
        if removed:
            await self._emit("memory.delete", note_id=note_id)
        return removed

    @boundary("更新长期记忆失败", fallback=ToolError)
    async def update(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        await self._authorize_record(note_id)
        note = await self._store.update_note(note_id, content, tags)
        if note is not None:
            await self._retriever.add(note)
        if note is not None:
            await self._emit("memory.update", note_id=note.id, tags=note.tags)
        return note


def _tags_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tags used for later recall.",
    }


class RememberTool(Tool):
    name = "remember"
    description = (
        "Store a durable fact, preference, decision, or constraint for later sessions. "
        "Do not store temporary turn state."
    )

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content to remember."},
                "tags": _tags_schema(),
                "kind": {
                    "type": "string",
                    "enum": ["fact", "preference", "decision", "constraint", "observation"],
                },
            },
            "required": ["content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        note = await self._gateway.remember(
            kwargs["content"],
            kwargs.get("tags"),
            kind=kwargs.get("kind", "fact"),
        )
        return f"已记住 [{note.id}]: {note.content}"


class RecallTool(Tool):
    name = "recall"
    description = "Search durable memory by content or tags."

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text; empty means all."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        notes = await self._gateway.recall(kwargs.get("query", ""))
        if not notes:
            return "（长期记忆里没有匹配的笔记）"
        return "\n".join(
            f"- [{note.id}] {note.content}"
            + (f"  [tags: {', '.join(note.tags)}]" if note.tags else "")
            for note in notes
        )


class ForgetTool(Tool):
    name = "forget"
    description = "Delete one durable memory record by id."

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"note_id": {"type": "string", "description": "Record id."}},
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        removed = await self._gateway.forget(kwargs["note_id"])
        return f"已删除笔记 {kwargs['note_id']}" if removed else f"未找到笔记 {kwargs['note_id']}"


class UpdateNoteTool(Tool):
    name = "update_note"
    description = "Update the content or tags of one existing memory record."

    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "Record id."},
                "content": {"type": "string", "description": "New content."},
                "tags": _tags_schema(),
            },
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        note_id = kwargs["note_id"]
        content = kwargs.get("content")
        tags = kwargs.get("tags")
        if content is None and tags is None:
            return "请至少提供 content 或 tags 之一"
        note = await self._gateway.update(note_id, content, tags)
        if note is None:
            return f"未找到笔记 {note_id}"
        return f"已更新 [{note.id}]: {note.content}"
