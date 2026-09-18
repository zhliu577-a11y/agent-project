"""Session gateway: transaction boundary for short-term conversation memory."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from core.compaction import CompactionPolicy, CompactionRequest, CompactionResult
from core.context import request_tokens, valid_tool_call_sequence
from core.errors import boundary
from core.events import Event, EventPublisher
from core.session import (
    SessionMetadata,
    SessionSnapshot,
    SessionStore,
    validate_session_id,
)
from core.tracing import current_trace_id
from core.types import Message


class SessionGateway:
    """Coordinate session reads, compaction, atomic writes, and checkpoints."""

    def __init__(
        self,
        store: SessionStore,
        session_id: str = "default",
        *,
        compaction: CompactionPolicy | None = None,
        max_tokens: int = 20000,
        events: EventPublisher | None = None,
    ) -> None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        self._store = store
        self._session_id = validate_session_id(session_id)
        self._compaction = compaction
        self._max_tokens = max_tokens
        self._events = events
        self._metadata = SessionMetadata()
        self._checkpoint: dict[str, Any] | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def revision(self) -> int:
        return self._metadata.revision

    @property
    def metadata(self) -> SessionMetadata:
        return self._metadata

    @property
    def checkpoint(self) -> dict[str, Any] | None:
        return dict(self._checkpoint) if self._checkpoint is not None else None

    def _expected_revision(self) -> int | None:
        if getattr(self._store, "supports_revisioning", False):
            return self._metadata.revision
        return None

    def _normalize_metadata(
        self,
        metadata: SessionMetadata,
        *,
        base_revision: int,
        message_count: int,
    ) -> SessionMetadata:
        if getattr(self._store, "supports_revisioning", False):
            return metadata
        return replace(
            metadata,
            revision=base_revision + 1,
            message_count=message_count,
        )

    async def _emit(self, name: str, **payload: object) -> None:
        if self._events is None:
            return
        await self._events.publish(
            Event(
                name=name,
                payload=dict(payload),
                trace_id=current_trace_id(),
                session_id=self._session_id,
            )
        )

    async def _refresh(self) -> None:
        metadata = await self._store.load_metadata(self._session_id)
        checkpoint = await self._store.load_checkpoint(self._session_id)
        self._metadata = metadata
        self._checkpoint = checkpoint
        self._loaded = True

    @boundary("读取会话失败")
    async def load_snapshot(self, *, limit: int | None = None) -> SessionSnapshot:
        """Load a consistent session view and remember its base revision."""
        async with self._lock:
            snapshot = await self._store.snapshot(self._session_id, limit=limit)
            self._metadata = snapshot.metadata
            self._checkpoint = snapshot.checkpoint
            self._loaded = True
        await self._emit(
            "session.loaded",
            revision=snapshot.metadata.revision,
            messages=len(snapshot.messages),
        )
        return snapshot

    @boundary("读取会话历史失败")
    async def load_history(self, *, limit: int | None = None) -> list[Message]:
        """Read history while refreshing revision and checkpoint state."""
        return list((await self.load_snapshot(limit=limit)).messages)

    @boundary("追加会话历史失败")
    async def append_messages(self, messages: list[Message]) -> SessionMetadata:
        """Append messages using the current revision as the base."""
        async with self._lock:
            if not self._loaded:
                await self._refresh()
            metadata = await self._store.append(
                self._session_id,
                list(messages),
                expected_revision=self._expected_revision(),
            )
            metadata = self._normalize_metadata(
                metadata,
                base_revision=self._metadata.revision,
                message_count=self._metadata.message_count + len(messages),
            )
            self._metadata = metadata
        await self._emit(
            "session.appended",
            revision=metadata.revision,
            messages=len(messages),
        )
        return metadata

    @boundary("保存会话历史失败")
    async def save_history(self, messages: list[Message]) -> SessionMetadata:
        """Replace history without applying compaction.

        This remains for compatibility; new runtime code should use
        ``commit_turn``.
        """
        async with self._lock:
            if not self._loaded:
                await self._refresh()
            metadata = await self._store.replace(
                self._session_id,
                list(messages),
                expected_revision=self._expected_revision(),
            )
            metadata = self._normalize_metadata(
                metadata,
                base_revision=self._metadata.revision,
                message_count=len(messages),
            )
            self._metadata = metadata
        await self._emit(
            "session.replaced",
            revision=metadata.revision,
            messages=len(messages),
        )
        return metadata

    @boundary("提交会话轮次失败")
    async def commit_turn(
        self,
        messages: list[Message],
        checkpoint: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        """Compact if needed, then atomically replace history and checkpoint."""
        async with self._lock:
            if not self._loaded:
                await self._refresh()

            base_revision = self._metadata.revision
            committed = list(messages)
            compaction: CompactionResult | None = None
            if self._compaction is not None:
                compaction = await self._compaction.compact(
                    CompactionRequest(
                        session_id=self._session_id,
                        messages=list(committed),
                        max_tokens=self._max_tokens,
                        revision=base_revision,
                        checkpoint=dict(checkpoint or {}),
                        metadata=self._metadata.to_dict(),
                    )
                )
                self._validate_compaction(compaction, original=committed)
                if compaction.compacted:
                    committed = list(compaction.messages)

            metadata = await self._store.replace(
                self._session_id,
                committed,
                expected_revision=self._expected_revision(),
            )
            metadata = self._normalize_metadata(
                metadata,
                base_revision=base_revision,
                message_count=len(committed),
            )
            checkpoint_payload = dict(checkpoint or {})
            checkpoint_payload.update(
                {
                    "session_revision": metadata.revision,
                    "message_count": len(committed),
                }
            )
            await self._store.save_checkpoint(self._session_id, checkpoint_payload)
            self._metadata = metadata
            self._checkpoint = checkpoint_payload

        if compaction is not None and compaction.compacted:
            await self._emit(
                "session.compacted",
                revision=metadata.revision,
                before=len(messages),
                after=len(committed),
                summary_chars=len(compaction.summary),
            )
        await self._emit(
            "session.committed",
            revision=metadata.revision,
            messages=len(committed),
        )
        return SessionSnapshot(
            session_id=self._session_id,
            messages=list(committed),
            metadata=metadata,
            checkpoint=checkpoint_payload,
        )

    @boundary("读取 checkpoint 失败")
    async def load_checkpoint(self) -> dict[str, Any] | None:
        checkpoint = await self._store.load_checkpoint(self._session_id)
        self._checkpoint = checkpoint
        return checkpoint

    @boundary("保存 checkpoint 失败")
    async def save_checkpoint(self, snapshot: dict[str, Any]) -> None:
        payload = dict(snapshot)
        await self._store.save_checkpoint(self._session_id, payload)
        self._checkpoint = payload
        await self._emit(
            "session.checkpoint_written",
            revision=self._metadata.revision,
        )

    @boundary("删除 checkpoint 失败")
    async def delete_checkpoint(self) -> None:
        await self._store.delete_checkpoint(self._session_id)
        self._checkpoint = None

    @boundary("清空会话失败")
    async def clear(self) -> SessionMetadata:
        async with self._lock:
            metadata = await self._store.clear(self._session_id)
            metadata = self._normalize_metadata(
                metadata,
                base_revision=self._metadata.revision,
                message_count=0,
            )
            self._metadata = metadata
            self._checkpoint = None
            self._loaded = True
        await self._emit("session.cleared", revision=metadata.revision)
        return metadata

    @boundary("failed to update session title")
    async def set_title(self, title: str) -> SessionMetadata:
        """Update the display title without changing the message count."""
        normalized = " ".join(title.split())
        if len(normalized) > 200:
            normalized = normalized[:197].rstrip() + "..."
        async with self._lock:
            if not self._loaded:
                await self._refresh()
            metadata = self._metadata.advanced(
                message_count=self._metadata.message_count,
                title=normalized,
            )
            await self._store.save_metadata(self._session_id, metadata)
            self._metadata = metadata
        await self._emit("session.title_updated", revision=metadata.revision)
        return metadata

    @boundary("failed to delete session")
    async def delete(self) -> None:
        """Delete the whole session through the store contract."""
        async with self._lock:
            await self._store.delete(self._session_id)
            self._metadata = SessionMetadata()
            self._checkpoint = None
            self._loaded = True
        await self._emit("session.deleted")

    @staticmethod
    def _validate_compaction(result: CompactionResult, *, original: list[Message]) -> None:
        if not isinstance(result, CompactionResult):
            raise TypeError(
                f"compaction policy must return CompactionResult, got {type(result).__name__}"
            )
        if result.compacted and not result.messages:
            raise ValueError("compaction policy returned an empty replacement history")
        if not all(isinstance(message, Message) for message in result.messages):
            raise TypeError("compaction policy returned a non-Message value")
        if not valid_tool_call_sequence(result.messages):
            raise ValueError("compaction policy returned orphaned tool call messages")
        if request_tokens(result.messages, []) > request_tokens(original, []):
            raise ValueError("compaction policy increased the persisted token count")
