"""In-memory session backend with the same revision semantics as JSONL."""

from __future__ import annotations

import asyncio
import copy

from core.session import (
    SessionMetadata,
    SessionSnapshot,
    SessionStore,
    ensure_revision,
    validate_session_id,
)
from core.types import Message, message_from_dict, message_to_dict


class InMemorySessionStore(SessionStore):
    """Keep sessions in process memory; useful for tests and ephemeral runs."""

    supports_revisioning = True

    def __init__(self) -> None:
        self._data: dict[str, list[Message]] = {}
        self._metadata: dict[str, SessionMetadata] = {}
        self._checkpoints: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_id(self, session_id: str) -> str:
        return validate_session_id(session_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def load(self, session_id: str) -> list[Message]:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            return [_copy(message) for message in self._data.get(session_id, [])]

    async def get(self, session_id: str, limit: int | None = None) -> list[Message]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        messages = await self.load(session_id)
        if limit is None or limit == 0:
            return messages
        return messages[-limit:]

    async def save(self, session_id: str, messages: list[Message]) -> None:
        await self.replace(session_id, messages, expected_revision=None)

    async def append(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            metadata = self._metadata.get(session_id, SessionMetadata())
            ensure_revision(expected_revision, metadata.revision)
            current = self._data.get(session_id, [])
            updated = [*current, *[_copy(message) for message in messages]]
            self._data[session_id] = updated
            next_metadata = metadata.advanced(message_count=len(updated))
            self._metadata[session_id] = next_metadata
            return next_metadata

    async def replace(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            metadata = self._metadata.get(session_id, SessionMetadata())
            ensure_revision(expected_revision, metadata.revision)
            current = [_copy(message) for message in messages]
            self._data[session_id] = current
            next_metadata = metadata.advanced(message_count=len(current))
            self._metadata[session_id] = next_metadata
            return next_metadata

    async def clear(self, session_id: str) -> SessionMetadata:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            metadata = self._metadata.get(session_id, SessionMetadata())
            self._data[session_id] = []
            self._checkpoints.pop(session_id, None)
            next_metadata = metadata.advanced(message_count=0)
            self._metadata[session_id] = next_metadata
            return next_metadata

    async def load_metadata(self, session_id: str) -> SessionMetadata:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            messages = self._data.get(session_id, [])
            metadata = self._metadata.get(session_id, SessionMetadata())
            if metadata.message_count != len(messages):
                metadata = SessionMetadata(
                    revision=max(metadata.revision, 1 if messages else 0),
                    message_count=len(messages),
                    created_at=metadata.created_at,
                    updated_at=metadata.updated_at,
                    title=metadata.title,
                    owner=metadata.owner,
                    parent_id=metadata.parent_id,
                    labels=metadata.labels,
                    extra=metadata.extra,
                )
                self._metadata[session_id] = metadata
            return metadata

    async def save_metadata(self, session_id: str, metadata: SessionMetadata) -> None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            self._metadata[session_id] = copy.deepcopy(metadata)

    async def list_sessions(self) -> list[tuple[str, SessionMetadata]]:
        session_ids = set(self._data) | set(self._metadata)
        sessions = [
            (session_id, copy.deepcopy(self._metadata.get(session_id, SessionMetadata())))
            for session_id in session_ids
        ]
        sessions.sort(key=lambda item: (item[1].updated_at, item[0]), reverse=True)
        return sessions

    async def delete(self, session_id: str) -> None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            self._data.pop(session_id, None)
            self._metadata.pop(session_id, None)
            self._checkpoints.pop(session_id, None)

    async def snapshot(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> SessionSnapshot:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            messages = [_copy(message) for message in self._data.get(session_id, [])]
            selected = messages if limit is None or limit == 0 else messages[-limit:]
            return SessionSnapshot(
                session_id=session_id,
                messages=selected,
                metadata=self._metadata.get(session_id, SessionMetadata()),
                checkpoint=copy.deepcopy(self._checkpoints.get(session_id)),
            )

    async def load_checkpoint(self, session_id: str) -> dict | None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            return copy.deepcopy(self._checkpoints.get(session_id))

    async def save_checkpoint(self, session_id: str, snapshot: dict) -> None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            self._checkpoints[session_id] = copy.deepcopy(snapshot)

    async def delete_checkpoint(self, session_id: str) -> None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            self._checkpoints.pop(session_id, None)


def _copy(message: Message) -> Message:
    return message_from_dict(message_to_dict(message))


def create_store(plugin_dir, context=None):
    return InMemorySessionStore()
